"""Scanned uploads through the whole pipeline with the LLM unavailable: the
document must still come out with rows (yellow-flagged), via local OCR."""
from unittest.mock import patch

import pymupdf as fitz
import pytest

from app.core.config import settings
from app.services.extraction import ocr
from app.services.extraction.ocr import ocr_available
from tests.extraction import scan_fixtures as sf
from tests.test_api import client, process_pending  # noqa: F401  (shared fixture/helper)

needs_tesseract = pytest.mark.skipif(not ocr_available(), reason="Tesseract OCR engine not installed")


def _upload(client, name, data, content_type):
    resp = client.post("/reports", files={"file": (name, data, content_type)})
    assert resp.status_code == 201
    process_pending(client, resp.json()["id"])
    return client.get(f"/reports/{resp.json()['id']}").json()


def _image_only_pdf(png: bytes, pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=png)
    data = doc.tobytes()
    doc.close()
    return data


def _rows(detail):
    return {o["original_test_name"]: o for o in detail["observations"]}


@pytest.fixture()
def llm_down():
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=RuntimeError("provider down")), \
         patch("app.services.loinc_mapping.llm_client.verify_mapping",
               side_effect=AssertionError("fallback must not call the LLM")):
        yield


@needs_tesseract
def test_scanned_image_upload_is_ocrd_and_flagged(client, llm_down):
    bad_scan = sf.realistic_bad_scan(sf.render_pdf_page("01_cbc_clean_digital.pdf"), angle_deg=4.0)
    detail = _upload(client, "scan.png", sf.to_png(bad_scan), "image/png")

    assert detail["status"] == "complete" and detail["used_fallback"] is True
    rows = _rows(detail)
    assert rows["Hemoglobin"]["value"] == "13.9"
    assert rows["Platelet Count"]["value"] == "250"
    for row in rows.values():
        assert row["extraction_source"] == "fallback"
        assert row["extraction_confidence"] <= settings.fallback_extraction_confidence
    assert rows["WBC"]["mapping_stage"] == "alias_exact"  # deterministic mapping still applies


@needs_tesseract
def test_image_only_pdf_goes_through_ocr(client, llm_down):
    scan = sf.realistic_bad_scan(sf.render_pdf_page("02_cmp_clean_digital.pdf"), angle_deg=-3.0)
    detail = _upload(client, "scanned.pdf", _image_only_pdf(sf.to_jpeg(scan)), "application/pdf")

    assert detail["status"] == "complete" and detail["num_pages"] == 1
    rows = _rows(detail)
    assert rows["Sodium"]["value"] == "140"
    assert rows["Creatinine"]["value"] == "0.9"


@needs_tesseract
def test_multi_page_scanned_pdf_reads_every_page(client, llm_down):
    page1 = sf.to_jpeg(sf.realistic_bad_scan(sf.render_pdf_page("07_multipage_panel.pdf", 0), 2.0))
    page2 = sf.to_jpeg(sf.realistic_bad_scan(sf.render_pdf_page("07_multipage_panel.pdf", 1), -2.0))
    doc = fitz.open()
    for png in (page1, page2):
        p = doc.new_page(width=595, height=842)
        p.insert_image(p.rect, stream=png)
    detail = _upload(client, "two.pdf", doc.tobytes(), "application/pdf")

    assert detail["num_pages"] == 2 and detail["status"] == "complete"
    pages = {o["page_number"] for o in detail["observations"]}
    assert pages == {1, 2}


def test_missing_ocr_engine_fails_with_a_clear_reason(client, llm_down, monkeypatch):
    monkeypatch.setattr(ocr, "find_tesseract", lambda: None)
    scan = sf.to_png(sf.render_pdf_page("01_cbc_clean_digital.pdf"))
    detail = _upload(client, "scan.png", scan, "image/png")

    assert detail["status"] == "failed" and detail["observations"] == []
    assert "provider down" in detail["error_message"]
    assert "Tesseract OCR engine is not installed" in detail["error_message"]


def test_ocr_disabled_by_setting_fails_with_a_clear_reason(client, llm_down, monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", False)
    detail = _upload(client, "scan.png", sf.to_png(sf.render_pdf_page("01_cbc_clean_digital.pdf")), "image/png")
    assert detail["status"] == "failed" and "OCR is disabled" in detail["error_message"]
