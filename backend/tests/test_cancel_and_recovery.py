"""Cancellation must work while waiting on the AI and when the worker is gone, a dead
worker's job must be recovered, and one-time builds must run once, not per worker."""
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fitz

from app.core.config import settings
from app.models.document import Document, DocumentStatus
from app.schemas.extraction import ExtractedTest, PageExtractionResult
from app.services import loinc_loader, worker
from app.services.normalization import build_alias_index
from tests.test_api import client, process_pending  # noqa: F401


def _pdf(n):
    d = fitz.open()
    for i in range(n):
        d.new_page().insert_text((72, 72), f"Hgb 13.{i} g/dL", fontsize=11)
    data = d.tobytes()
    d.close()
    return data


def _ok():
    return PageExtractionResult(tests=[ExtractedTest(
        original_test_name="Hemoglobin", value="13.5", unit="g/dL", extraction_confidence=0.9)], page_notes=None)


def _set(client, doc_id, **fields):
    s = client.test_session_factory()
    try:
        s.query(Document).filter(Document.id == doc_id).update(fields)
        s.commit()
    finally:
        s.close()


# ----------------------------------------------------------------- cancel

def test_cancel_is_noticed_while_waiting_on_a_slow_ai_call(client):
    """Used to be checked only between pages: a slow/hung call blocked cancellation."""
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(3), "application/pdf")}).json()["id"]
    release = threading.Event()

    def hang(image_png, text_layer):
        release.wait(timeout=20)          # simulates a provider call that takes forever
        return _ok()

    def cancel_soon():
        time.sleep(0.8)
        _set(client, doc_id, cancel_requested=True)

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=hang):
        process_pending(client, doc_id)
        elapsed = time.monotonic() - started
        release.set()
    detail = client.get(f"/reports/{doc_id}").json()
    assert detail["status"] == "cancelled"
    assert "Cancelled after" in detail["error_message"]
    assert elapsed < 6, f"cancel took {elapsed:.1f}s - it waited for the hung AI call"


def test_cancel_requested_before_processing_starts_skips_all_ai_calls(client):
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(3), "application/pdf")}).json()["id"]
    _set(client, doc_id, cancel_requested=True)
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=AssertionError("no call expected")) as m:
        process_pending(client, doc_id)
    assert client.get(f"/reports/{doc_id}").json()["status"] == "cancelled"
    m.assert_not_called()


def test_cancel_of_an_orphaned_processing_document_completes_immediately(client):
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(1), "application/pdf")}).json()["id"]
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    _set(client, doc_id, status=DocumentStatus.processing, updated_at=old)
    resp = client.post(f"/reports/{doc_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert "no longer running" in resp.json()["error_message"]


def test_cancel_of_a_live_processing_document_stays_cooperative(client):
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(1), "application/pdf")}).json()["id"]
    _set(client, doc_id, status=DocumentStatus.processing, updated_at=datetime.now(timezone.utc))
    body = client.post(f"/reports/{doc_id}/cancel").json()
    assert body["status"] == "processing" and body["cancel_requested"] is True


# --------------------------------------------------------------- recovery

def test_startup_recovery_requeues_a_job_whose_worker_died(client):
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(1), "application/pdf")}).json()["id"]
    _set(client, doc_id, status=DocumentStatus.processing, updated_at=datetime.now(timezone.utc) - timedelta(minutes=2))
    s = client.test_session_factory()
    try:
        assert worker.recover_stuck_documents(s, worker.STARTUP_STUCK_AFTER) == 1     # 2 min > 45 s
        assert worker.recover_stuck_documents(s) == 0                                 # periodic: still < 3 min
    finally:
        s.close()
    assert client.get(f"/reports/{doc_id}").json()["status"] == "pending"


def test_a_job_with_a_fresh_heartbeat_is_never_recovered(client):
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(1), "application/pdf")}).json()["id"]
    _set(client, doc_id, status=DocumentStatus.processing, updated_at=datetime.now(timezone.utc))
    s = client.test_session_factory()
    try:
        assert worker.recover_stuck_documents(s, worker.STARTUP_STUCK_AFTER) == 0
    finally:
        s.close()


def test_recovered_job_that_was_being_cancelled_ends_cancelled(client):
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(1), "application/pdf")}).json()["id"]
    _set(client, doc_id, status=DocumentStatus.processing, cancel_requested=True,
         updated_at=datetime.now(timezone.utc) - timedelta(minutes=10))
    s = client.test_session_factory()
    try:
        worker.recover_stuck_documents(s)
    finally:
        s.close()
    assert client.get(f"/reports/{doc_id}").json()["status"] == "cancelled"


# --------------------------------------------- memory: one-time builds once

def test_compact_alias_index_keeps_unique_drops_ambiguous():
    recs = [
        {"loinc_num": "1-1", "long_common_name": "Alpha test", "shortname": "ALPHA", "aliases": ["aa", "shared"]},
        {"loinc_num": "2-2", "long_common_name": "Beta test", "shortname": None, "aliases": ["bb", "shared", "aa"]},
        {"loinc_num": "3-3", "long_common_name": "Gamma test", "shortname": "GAMMA", "aliases": ["gamma", "gamma"]},
    ]
    idx = build_alias_index(iter(recs))                      # also accepts a one-shot stream
    assert idx["alpha test"]["loinc_num"] == "1-1" and idx["bb"]["loinc_num"] == "2-2"
    assert idx["gamma"]["loinc_num"] == "3-3"                # same code repeated is NOT ambiguity
    assert "shared" not in idx and "aa" not in idx            # two different codes -> dropped
    assert idx["alpha"]["canonical_name"] == "ALPHA"


def test_cold_alias_index_is_built_once_even_when_workers_race(monkeypatch):
    monkeypatch.setattr(loinc_loader, "_alias_index_cache", None)
    calls = {"n": 0}
    real = loinc_loader.build_alias_index

    def counting(rows):
        calls["n"] += 1
        time.sleep(0.2)                                        # widen the race window
        return real(rows)

    monkeypatch.setattr(loinc_loader, "build_alias_index", counting)
    results = []
    threads = [threading.Thread(target=lambda: results.append(loinc_loader.get_alias_index())) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert calls["n"] == 1
    assert all(r is results[0] for r in results)


def test_default_concurrency_fits_a_small_instance():
    assert 1 <= settings.worker_concurrency <= 2
