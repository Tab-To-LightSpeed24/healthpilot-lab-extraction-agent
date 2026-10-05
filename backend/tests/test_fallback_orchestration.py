"""Phase 3: validation persisted through the pipeline, the document time
budget, and bounded background OCR. LLM is always disabled/mocked."""
import threading
import time
from unittest.mock import patch

import pymupdf as fitz
import pytest

from app.core.config import settings
from app.schemas.extraction import ExtractedTest, PageExtractionResult
from app.services.extraction import fallback as fb
from app.services.extraction.fallback import FallbackExtractor
from app.services.extraction.ocr import ocr_available
from app.services.pdf_utils import PageContent
from tests.extraction import scan_fixtures as sf
from tests.test_api import client, process_pending  # noqa: F401

needs_tesseract = pytest.mark.skipif(not ocr_available(), reason="Tesseract OCR engine not installed")


@pytest.fixture(autouse=True)
def llm_off(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", False)


def _upload(client, name, data, content_type):
    resp = client.post("/reports", files={"file": (name, data, content_type)})
    assert resp.status_code == 201
    process_pending(client, resp.json()["id"])
    return client.get(f"/reports/{resp.json()['id']}").json()


def _scanned_pdf(pages: int) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        png = sf.to_jpeg(sf.realistic_bad_scan(sf.render_pdf_page("07_multipage_panel.pdf", i % 2), 2.0))
        p = doc.new_page(width=595, height=842)
        p.insert_image(p.rect, stream=png)
    return doc.tobytes()


# ---- validation through the whole pipeline --------------------------------

def test_suspect_value_is_stored_unchanged_with_a_note_and_suggestion(client):
    text = b"WBC          715    10*3/uL   4 - 11\nHemoglobin   13.9   g/dL      13.0-17.0\n"
    detail = _upload(client, "r.txt", text, "text/plain")
    rows = {o["original_test_name"]: o for o in detail["observations"]}

    wbc = rows["WBC"]
    assert wbc["value"] == "715", "the stored value must never be silently changed"
    assert wbc["suggested_value"] == "7.15"
    assert any("lost decimal" in n for n in wbc["validation_notes"])
    assert wbc["extraction_confidence"] < 0.65

    clean = rows["Hemoglobin"]
    assert clean["suggested_value"] is None and not clean["validation_notes"]
    assert detail["quality"]["low_confidence_extractions"], "suspect rows must surface through the existing quality flags"


def test_applying_the_suggestion_is_an_explicit_edit_that_clears_it(client):
    detail = _upload(client, "r.txt", b"WBC          715    10*3/uL   4 - 11\n", "text/plain")
    obs = detail["observations"][0]
    resp = client.patch(f"/observations/{obs['id']}", json={"value": obs["suggested_value"]})
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["value"] == "7.15" and updated["suggested_value"] is None and updated["is_edited"] is True


# ---- document time budget --------------------------------------------------

def test_local_extraction_stops_when_the_document_time_budget_is_spent(client, monkeypatch):
    monkeypatch.setattr(settings, "fallback_document_budget_floor_seconds", 0.0)
    monkeypatch.setattr(settings, "fallback_seconds_per_page", 0.0)
    detail = _upload(client, "r.txt", b"WBC          7.2    10*3/uL   4 - 11\n", "text/plain")
    assert detail["status"] == "failed"
    assert "time budget" in detail["error_message"]


# ---- background OCR pool ---------------------------------------------------

def _page(n):
    return PageContent(page_number=n, text=None, image_png=b"x")


def test_prefetch_runs_ocr_off_the_calling_thread_and_extract_returns_it(monkeypatch):
    seen = {}

    def fake_ocr(page):
        seen[page.page_number] = threading.current_thread().name
        return PageExtractionResult(tests=[ExtractedTest(original_test_name=f"T{page.page_number}", value="1", extraction_confidence=0.5)])

    monkeypatch.setattr(FallbackExtractor, "_ocr", staticmethod(fake_ocr))
    ex = FallbackExtractor(b"png", "image/png")
    ex.prefetch([_page(1), _page(2)])
    try:
        names = [ex.extract(_page(n)).tests[0].original_test_name for n in (1, 2)]
    finally:
        ex.close()
    assert names == ["T1", "T2"]
    assert all(t.startswith("ocr") for t in seen.values()), seen


def test_ocr_pool_is_bounded_by_the_configured_worker_count(monkeypatch):
    """Memory is the constraint (one large-page OCR peaks ~424MB of a 512MB
    instance), so concurrency must never exceed ocr_max_workers."""
    monkeypatch.setattr(settings, "ocr_max_workers", 1)
    active, peak, lock = [0], [0], threading.Lock()

    def slow_ocr(page):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.15)
        with lock:
            active[0] -= 1
        return PageExtractionResult(tests=[])

    monkeypatch.setattr(FallbackExtractor, "_ocr", staticmethod(slow_ocr))
    ex = FallbackExtractor(b"png", "image/png")
    ex.prefetch([_page(i) for i in range(1, 5)])
    for i in range(1, 5):
        ex.extract(_page(i))
    ex.close()
    assert peak[0] == 1


def test_prefetch_can_be_disabled_and_extract_still_works(monkeypatch):
    monkeypatch.setattr(settings, "ocr_max_workers", 0)
    monkeypatch.setattr(FallbackExtractor, "_ocr", staticmethod(lambda p: PageExtractionResult(tests=[])))
    ex = FallbackExtractor(b"png", "image/png")
    ex.prefetch([_page(1)])
    assert ex._futures == {} and ex.extract(_page(1)).tests == []


def test_close_cancels_queued_work_and_is_idempotent(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocking_ocr(page):
        started.set()
        release.wait(2)
        return PageExtractionResult(tests=[])

    monkeypatch.setattr(FallbackExtractor, "_ocr", staticmethod(blocking_ocr))
    ex = FallbackExtractor(b"png", "image/png")
    ex.prefetch([_page(1), _page(2), _page(3)])
    started.wait(1)
    ex.close()
    release.set()
    ex.close()
    assert ex._futures == {} and ex._pool is None


@needs_tesseract
def test_scanned_multi_page_document_is_ocrd_in_the_background_pool(client):
    threads = []
    real = fb.extract_scanned_page

    def spy(image_bytes, page_number):
        threads.append(threading.current_thread().name)
        return real(image_bytes, page_number)

    with patch.object(fb, "extract_scanned_page", spy):
        detail = _upload(client, "scan.pdf", _scanned_pdf(3), "application/pdf")

    assert detail["status"] == "complete" and detail["num_pages"] == 3
    assert len(threads) == 3 and all(t.startswith("ocr") for t in threads), threads
    assert {o["page_number"] for o in detail["observations"]} >= {1, 2}


@needs_tesseract
def test_a_scanned_document_runs_end_to_end_with_pages_in_order(client):
    detail = _upload(client, "scan.pdf", _scanned_pdf(2), "application/pdf")
    assert detail["status"] == "complete"
    pages = [o["page_number"] for o in detail["observations"]]
    assert pages == sorted(pages)
