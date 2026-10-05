"""The LLM is optional: whenever it is disabled, unconfigured, slow, or
failing, the document must still come out with extracted rows (flagged as
lower-accuracy) instead of failing. Every test here either mocks the LLM
client or points the real client at a local server -- none of them touches a
real provider or spends any credit.
"""
import http.server
import threading
import time
from unittest.mock import MagicMock, patch

import pymupdf as fitz
import pytest

from app.core.config import settings
from app.schemas.extraction import ExtractedTest, PageExtractionResult
from app.services import llm_client
from tests.test_api import client, process_pending  # noqa: F401  (fixtures/helpers shared with the API tests)

TABLE_LINES = [
    "Acme Lab Report",
    "WBC          6.8    10*3/uL   4.0-11.0",
    "Hemoglobin    13.9   g/dL      13.0-17.0",
    "Zorbotronin   5.5    pg/mL     1-9",
]


def _table_pdf(*pages: list[str]) -> bytes:
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for ln in lines:
            page.insert_text((72, y), ln, fontname="cour", fontsize=10)
            y += 16
    data = doc.tobytes()
    doc.close()
    return data


def _upload(client, name, data, content_type="application/pdf"):
    resp = client.post("/reports", files={"file": (name, data, content_type)})
    assert resp.status_code == 201
    doc_id = resp.json()["id"]
    process_pending(client, doc_id)
    return client.get(f"/reports/{doc_id}").json()


def _by_name(detail):
    return {o["original_test_name"]: o for o in detail["observations"]}


def test_llm_failure_diverts_to_fallback_and_flags_the_document(client):
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=RuntimeError("provider down")):
        detail = _upload(client, "r.pdf", _table_pdf(TABLE_LINES))

    assert detail["status"] == "complete"
    assert detail["used_fallback"] is True
    assert "provider down" in detail["fallback_reason"]
    rows = _by_name(detail)
    assert rows["WBC"]["value"] == "6.8" and rows["WBC"]["unit"] == "10*3/uL"
    assert rows["Hemoglobin"]["value"] == "13.9"
    for row in rows.values():
        assert row["extraction_source"] == "fallback"
        assert row["extraction_confidence"] <= settings.fallback_extraction_confidence


def test_fallback_mapping_makes_no_llm_call_and_never_invents_a_code(client):
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=RuntimeError("down")), \
         patch("app.services.loinc_mapping.llm_client.verify_mapping") as verify:
        detail = _upload(client, "r.pdf", _table_pdf(TABLE_LINES))

    verify.assert_not_called()
    rows = _by_name(detail)
    assert rows["WBC"]["mapping_status"] == "confirmed"  # deterministic alias match still works
    assert rows["WBC"]["mapping_stage"] == "alias_exact"
    unknown = rows["Zorbotronin"]
    assert unknown["mapping_status"] == "needs_review"
    assert unknown["loinc_code"] is None
    assert unknown["mapping_stage"] == "lexical_only"


def test_one_llm_failure_stops_llm_attempts_for_the_rest_of_the_document(client):
    two_pages = _table_pdf(TABLE_LINES, ["Page two", "Glucose      98    mg/dL    70-99"])
    mock = MagicMock(side_effect=TimeoutError("slow"))
    with patch("app.services.pipeline.llm_client.extract_page", mock):
        detail = _upload(client, "two.pdf", two_pages)

    assert mock.call_count == 1, "a dead provider must cost one bounded attempt per document, not one per page"
    assert detail["status"] == "complete"
    pages = {o["page_number"] for o in detail["observations"]}
    assert pages == {1, 2}


def test_malformed_model_output_only_diverts_that_one_page(client):
    ok = PageExtractionResult(
        tests=[ExtractedTest(original_test_name="Glucose", value="98", unit="mg/dL", extraction_confidence=0.95)],
        page_notes=None,
    )
    mock = MagicMock(side_effect=[ValueError("bad json"), ok])
    two_pages = _table_pdf(TABLE_LINES, ["Page two", "Glucose      98    mg/dL    70-99"])
    with patch("app.services.pipeline.llm_client.extract_page", mock), \
         patch("app.services.loinc_mapping.llm_client.verify_mapping", return_value={"chosen_loinc_num": None, "confidence": 0}):
        detail = _upload(client, "two.pdf", two_pages)

    assert mock.call_count == 2, "a malformed reply on one page must not disable the LLM for the next"
    by_page = {}
    for o in detail["observations"]:
        by_page.setdefault(o["page_number"], set()).add(o["extraction_source"])
    assert by_page == {1: {"fallback"}, 2: {"llm"}}
    assert detail["used_fallback"] is True


def test_llm_disabled_by_setting_never_calls_the_llm(client, monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", False)
    boom = MagicMock(side_effect=AssertionError("LLM must not be called when disabled"))
    with patch("app.services.pipeline.llm_client.extract_page", boom):
        detail = _upload(client, "r.pdf", _table_pdf(TABLE_LINES))

    boom.assert_not_called()
    assert detail["status"] == "complete" and detail["used_fallback"] is True
    assert "disabled" in detail["fallback_reason"]
    assert _by_name(detail)["WBC"]["value"] == "6.8"


def test_missing_api_key_falls_back_instead_of_failing(client, monkeypatch):
    # Real client code path, no mock: no key configured -> ConfigurationError.
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(llm_client, "_client", None)
    detail = _upload(client, "r.pdf", _table_pdf(TABLE_LINES))

    assert detail["status"] == "complete" and detail["used_fallback"] is True
    assert "GEMINI_API_KEY" in detail["fallback_reason"]
    assert _by_name(detail)["Hemoglobin"]["value"] == "13.9"


def test_plain_text_report_falls_back_too(client):
    text = b"Iron Studies Panel\nIron        70    ug/dL    50-170\nFerritin    120   ng/mL    20-250\n"
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=RuntimeError("down")):
        detail = _upload(client, "r.txt", text, "text/plain")
    assert detail["status"] == "complete"
    assert _by_name(detail)["Ferritin"]["value"] == "120"


def test_scanned_input_with_llm_down_fails_with_both_reasons(client):
    doc = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    pix.clear_with(255)
    png = pix.tobytes("png")
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=RuntimeError("down")):
        detail = _upload(client, "scan.png", png, "image/png")

    assert detail["status"] == "failed"
    assert detail["observations"] == []
    assert "down" in detail["error_message"] and "OCR" in detail["error_message"]


def test_healthy_llm_path_is_unchanged_and_not_flagged(client):
    ok = PageExtractionResult(
        tests=[ExtractedTest(original_test_name="Hgb", value="13.5", unit="gm/dl", extraction_confidence=0.95)],
        page_notes=None,
    )
    with patch("app.services.pipeline.llm_client.extract_page", return_value=ok):
        detail = _upload(client, "r.pdf", _table_pdf(TABLE_LINES))

    assert detail["used_fallback"] is False and detail["fallback_reason"] is None
    assert [o["extraction_source"] for o in detail["observations"]] == ["llm"]


# --- real time-limit behaviour, against a local server that never answers in time ---

class _SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        time.sleep(4)

    def log_message(self, *args):
        pass


@pytest.fixture()
def slow_llm_server(monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(llm_client, "GEMINI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 0.3)
    monkeypatch.setattr(settings, "llm_page_budget_seconds", 0.1)
    monkeypatch.setattr(llm_client, "_client", None)
    yield
    server.shutdown()
    monkeypatch.setattr(llm_client, "_client", None)


def test_real_llm_call_is_abandoned_within_the_time_budget(slow_llm_server):
    start = time.monotonic()
    with pytest.raises(Exception) as exc_info:
        llm_client.extract_page(image_png=b"", text_layer="WBC 6.8 10*3/uL")
    elapsed = time.monotonic() - start

    assert "timeout" in type(exc_info.value).__name__.lower() or "timed out" in str(exc_info.value).lower()
    assert elapsed < 2.0, f"call should be abandoned at the budget, took {elapsed:.1f}s"


def test_slow_provider_end_to_end_still_returns_extracted_rows(client, slow_llm_server):
    start = time.monotonic()
    detail = _upload(client, "r.pdf", _table_pdf(TABLE_LINES))
    elapsed = time.monotonic() - start

    assert detail["status"] == "complete" and detail["used_fallback"] is True
    assert _by_name(detail)["WBC"]["value"] == "6.8"
    assert elapsed < 5.0
