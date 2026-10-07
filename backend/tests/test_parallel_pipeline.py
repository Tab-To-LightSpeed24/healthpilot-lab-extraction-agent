"""Pages of one document are sent to the LLM concurrently, not one after another."""
import time
from unittest.mock import patch

import fitz

from app.core.config import settings
from app.schemas.extraction import ExtractedTest, PageExtractionResult
from app.services import llm_client
from tests.test_api import client, process_pending  # noqa: F401


def _pdf(n_pages: int) -> bytes:
    doc = fitz.open()
    for i in range(n_pages):
        doc.new_page().insert_text((72, 72), f"Hgb 13.{i % 10} g/dL page {i + 1}", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _ok():
    return PageExtractionResult(
        tests=[ExtractedTest(original_test_name="Hemoglobin", value="13.5", unit="g/dL", extraction_confidence=0.9)],
        page_notes=None,
    )


def test_twelve_slow_pages_finish_in_roughly_one_pages_time(client):
    n, latency = 12, 1.0

    def slow(image_png, text_layer):
        time.sleep(latency)
        return _ok()

    doc_id = client.post("/reports", files={"file": ("big.pdf", _pdf(n), "application/pdf")}).json()["id"]
    started = time.monotonic()
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=slow):
        process_pending(client, doc_id)
    elapsed = time.monotonic() - started

    detail = client.get(f"/reports/{doc_id}").json()
    assert detail["status"] == "complete" and detail["pages_done"] == n
    assert sorted(o["page_number"] for o in detail["observations"]) == list(range(1, n + 1))
    assert elapsed < n * latency / 2, f"took {elapsed:.1f}s - pages are not running concurrently"


def test_concurrency_is_bounded_by_the_setting(client, monkeypatch):
    import threading
    monkeypatch.setattr(settings, "llm_max_concurrency", 3)
    lock, state = threading.Lock(), {"now": 0, "peak": 0}

    def tracked(image_png, text_layer):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.15)
        with lock:
            state["now"] -= 1
        return _ok()

    doc_id = client.post("/reports", files={"file": ("big.pdf", _pdf(9), "application/pdf")}).json()["id"]
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=tracked):
        process_pending(client, doc_id)
    assert 2 <= state["peak"] <= 3


def test_one_failed_page_does_not_lose_the_others(client):
    def flaky(image_png, text_layer):
        if text_layer and "page 2" in text_layer:
            raise ValueError("bad json")
        return _ok()

    doc_id = client.post("/reports", files={"file": ("big.pdf", _pdf(4), "application/pdf")}).json()["id"]
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=flaky):
        process_pending(client, doc_id)
    detail = client.get(f"/reports/{doc_id}").json()
    assert detail["status"] == "complete"
    sources = {o["page_number"]: o["extraction_source"] for o in detail["observations"]}
    assert sources[1] == sources[3] == sources[4] == "llm"
    assert sources.get(2) in ("fallback", None)


def test_the_call_cap_refuses_requests_beyond_the_budget(monkeypatch):
    import pytest
    monkeypatch.setattr(settings, "llm_call_cap", 2)
    monkeypatch.setattr(llm_client, "_calls_made", 0)
    llm_client._count_call()
    llm_client._count_call()
    with pytest.raises(llm_client.ConfigurationError, match="call cap"):
        llm_client._count_call()


def test_a_failed_mapping_call_does_not_demote_ai_read_pages_to_local_reading(client):
    """The AI read the pages fine; only LOINC verification failed. Those pages must
    stay AI-extracted (not be re-read by the local parser) and just need review."""
    doc_id = client.post("/reports", files={"file": ("big.pdf", _pdf(4), "application/pdf")}).json()["id"]
    odd = PageExtractionResult(
        tests=[ExtractedTest(original_test_name="Specific Gravity", value="1.02", extraction_confidence=0.9)],
        page_notes=None,
    )
    with patch("app.services.pipeline.llm_client.extract_page", return_value=odd), \
         patch("app.services.loinc_mapping.llm_client.verify_mappings_batch", side_effect=RuntimeError("quota")):
        process_pending(client, doc_id)
    detail = client.get(f"/reports/{doc_id}").json()
    assert detail["status"] == "complete"
    assert detail["used_fallback"] is False
    assert {o["extraction_source"] for o in detail["observations"]} == {"llm"}
    assert {o["mapping_status"] for o in detail["observations"]} == {"needs_review"}
