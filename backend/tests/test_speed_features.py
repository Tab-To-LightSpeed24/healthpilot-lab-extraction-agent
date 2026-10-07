"""Lazy cheap page images, text-only AI input, remembered LOINC picks, throttled progress
commits, and the stale-message fix."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import fitz
import pytest
from sqlalchemy import event

from app.core.config import settings
from app.models.document import Document
from app.schemas.extraction import ExtractedTest, PageExtractionResult
from app.services import learned_mappings, llm_client, loinc_mapping, pdf_utils, pipeline, worker
from app.services.loinc_loader import get_alias_index
from tests.test_api import client, process_pending  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_memory():
    learned_mappings.clear()
    yield
    learned_mappings.clear()


def _pdf(n=1, text="Hgb 13.5 g/dL"):
    d = fitz.open()
    for i in range(n):
        d.new_page().insert_text((72, 72), f"{text} page {i + 1}", fontsize=11)
    data = d.tobytes()
    d.close()
    return data


def _t(name, value="1", unit=None, specimen=None):
    return ExtractedTest(original_test_name=name, value=value, unit=unit, specimen=specimen, extraction_confidence=0.9)


# ------------------------------------------------------------- lazy images

def test_pdf_pages_know_their_text_but_render_nothing_up_front():
    pages = pdf_utils.load_pages(_pdf(5), "application/pdf")
    assert len(pages) == 5 and all("Hgb" in p.text for p in pages)
    assert all(p._png is None and p._jpeg is None for p in pages)      # nothing rendered yet


def test_llm_image_is_a_lower_resolution_jpeg_and_ocr_image_is_the_full_png():
    page = pdf_utils.load_pages(_pdf(1), "application/pdf")[0]
    jpeg = page.image_for_llm()
    assert jpeg[:2] == b"\xff\xd8"
    png = page.image_png
    assert png.startswith(b"\x89PNG")
    small, full = fitz.Pixmap(jpeg), fitz.Pixmap(png)
    assert small.width * small.height < full.width * full.height * 0.7       # ~45% fewer pixels to render/encode
    assert page.image_for_llm() is jpeg                                      # cached


def test_uploaded_photo_is_size_capped_for_the_model():
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 4000, 3000), False)
    pix.clear_with(200)
    page = pdf_utils.load_pages(pix.tobytes("png"), "image/png")[0]
    small = fitz.Pixmap(page.image_for_llm())
    assert max(small.width, small.height) <= pdf_utils.LLM_IMAGE_MAX_SIDE
    assert page.image_png.startswith(b"\x89PNG")                              # OCR still gets the full image


def test_text_pages_have_no_image():
    page = pdf_utils.load_pages(b"WBC 6.8", "text/plain")[0]
    assert page.image_for_llm() == b"" and page.image_png == b""


# ---------------------------------------------------------- what the AI gets

def test_auto_mode_sends_text_only_for_rich_text_pages_and_images_otherwise(monkeypatch):
    monkeypatch.setattr(settings, "llm_page_input", "auto")
    monkeypatch.setattr(settings, "llm_text_only_min_chars", 50)
    rich = pdf_utils.load_pages(_pdf(1, text="Hemoglobin 13.5 g/dL  " * 10), "application/pdf")[0]
    thin = pdf_utils.load_pages(_pdf(1, text="x"), "application/pdf")[0]
    assert pipeline._llm_inputs(rich)[0] == b""
    assert pipeline._llm_inputs(thin)[0][:2] == b"\xff\xd8"


def test_image_and_text_modes(monkeypatch):
    page = pdf_utils.load_pages(_pdf(1, text="Hemoglobin 13.5 g/dL  " * 30), "application/pdf")[0]
    monkeypatch.setattr(settings, "llm_page_input", "image")
    assert pipeline._llm_inputs(page)[0][:2] == b"\xff\xd8"
    monkeypatch.setattr(settings, "llm_page_input", "text")
    assert pipeline._llm_inputs(page)[0] == b""


def test_client_labels_jpeg_correctly_and_explains_text_only_requests(monkeypatch):
    client_mock = MagicMock()
    client_mock.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"tests": [], "page_notes": null}'))])
    monkeypatch.setattr(llm_client, "_get_client", lambda: client_mock)
    llm_client.extract_page(b"\xff\xd8\xff\xe0jpegdata", "WBC 6.8")
    parts = client_mock.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    llm_client.extract_page(b"", "WBC 6.8")
    parts = client_mock.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert len(parts) == 1 and "no page image is attached" in parts[0]["text"]


# ------------------------------------------------------- remembered picks

def test_remembered_pick_skips_the_ai_and_matching_is_specific():
    learned_mappings._cache[learned_mappings.make_key("Specific Gravity", "Urine", None)] = "2965-2"
    with patch.object(llm_client, "verify_mappings_batch") as batch:
        out = loinc_mapping.map_rows([_t("Specific  gravity", specimen="URINE")], get_alias_index())
    batch.assert_not_called()
    assert out[0]["mapping_stage"] == "learned" and out[0]["loinc_code"] == "2965-2"
    assert out[0]["mapping_status"] == "confirmed"
    # a different specimen or unit is NOT the same memory
    with patch.object(llm_client, "verify_mappings_batch", return_value=[{"chosen_loinc_num": None, "confidence": 0}]):
        other = loinc_mapping.map_rows([_t("Specific gravity", specimen="Serum")], get_alias_index())
    assert other[0]["mapping_stage"] != "learned"


def test_remember_never_lets_an_ai_pick_overwrite_a_human_one(db_session):
    row = ("Odd Assay", "Serum", "u/L", "1-1")
    learned_mappings.remember(db_session, [row], source="review", confidence=1.0)
    learned_mappings.remember(db_session, [("Odd Assay", "Serum", "u/L", "2-2")], source="llm", confidence=0.95)
    assert learned_mappings.lookup("Odd Assay", "Serum", "u/L") == "1-1"
    learned_mappings.remember(db_session, [("Odd Assay", "Serum", "u/L", "3-3")], source="review", confidence=1.0)
    assert learned_mappings.lookup("Odd Assay", "Serum", "u/L") == "3-3"      # a newer human choice wins


def test_remember_ignores_duplicates_in_one_call(db_session):
    n = learned_mappings.remember(db_session, [("A", None, None, "1-1"), ("a", None, None, "1-1")], "llm", 0.9)
    assert n == 1
    db_session.flush()


def test_confident_ai_picks_are_remembered_after_processing(client):
    res = PageExtractionResult(tests=[_t("Specific Gravity", "1.02", specimen="Urine")], page_notes=None)
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(), "application/pdf")}).json()["id"]
    pick = lambda items: [{"chosen_loinc_num": i["candidates"][0]["loinc_num"], "confidence": 0.95, "rationale": "x"} for i in items]
    with patch("app.services.pipeline.llm_client.extract_page", return_value=res), \
         patch("app.services.loinc_mapping.llm_client.verify_mappings_batch", side_effect=pick) as batch:
        process_pending(client, doc_id)
        assert batch.call_count == 1
        second = client.post("/reports", files={"file": ("b.pdf", _pdf(), "application/pdf")}).json()["id"]
        process_pending(client, second)
        assert batch.call_count == 1, "the second document must not need the AI for the same test"
    stages = {o["mapping_stage"] for o in client.get(f"/reports/{second}").json()["observations"]}
    assert stages == {"learned"}


def test_low_confidence_ai_picks_are_not_remembered(client):
    res = PageExtractionResult(tests=[_t("Specific Gravity", "1.02", specimen="Urine")], page_notes=None)
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(), "application/pdf")}).json()["id"]
    weak = lambda items: [{"chosen_loinc_num": i["candidates"][0]["loinc_num"], "confidence": 0.8, "rationale": "x"} for i in items]
    with patch("app.services.pipeline.llm_client.extract_page", return_value=res), \
         patch("app.services.loinc_mapping.llm_client.verify_mappings_batch", side_effect=weak):
        process_pending(client, doc_id)
    assert learned_mappings.lookup("Specific Gravity", "Urine", None) is None


def test_a_human_review_teaches_the_memory(client):
    res = PageExtractionResult(tests=[_t("Zorbo Assay", "5", unit="u/L", specimen="Serum")], page_notes=None)
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(), "application/pdf")}).json()["id"]
    with patch("app.services.pipeline.llm_client.extract_page", return_value=res), \
         patch("app.services.loinc_mapping.llm_client.verify_mappings_batch",
               return_value=[{"chosen_loinc_num": None, "confidence": 0.1}]):
        process_pending(client, doc_id)
    obs_id = client.get(f"/reports/{doc_id}").json()["observations"][0]["id"]
    r = client.patch(f"/observations/{obs_id}/review", json={"loinc_code": "2345-7", "mapping_status": "confirmed"})
    assert r.status_code == 200
    assert learned_mappings.lookup("Zorbo Assay", "Serum", "u/L") == "2345-7"


# ------------------------------------------------- progress commits, messages

def test_progress_commits_are_throttled_but_the_feed_is_complete(client):
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(8), "application/pdf")}).json()["id"]
    res = PageExtractionResult(tests=[_t("Hemoglobin", "13.5", "g/dL")], page_notes=None)
    commits = {"n": 0}
    session = client.test_session_factory()

    @event.listens_for(session, "before_commit")
    def _count(sess):
        if sess.new or sess.dirty or sess.deleted:      # only commits that actually write something
            commits["n"] += 1

    with patch("app.services.pipeline.llm_client.extract_page", return_value=res):
        pipeline.process_document(doc_id, db=session)
    session.close()
    detail = client.get(f"/reports/{doc_id}").json()
    msgs = [e["msg"] for e in detail["progress"]]
    assert detail["status"] == "complete" and detail["pages_done"] == 8
    assert msgs[0] == "Picked up from the queue" and msgs[-1].startswith("Finished in")
    assert sum("the AI found" in m for m in msgs) == 8                       # nothing was lost
    # Used to be ~6 commits per page (>45 for 8 pages); now about one per page plus a few.
    assert commits["n"] <= 8 + 8, commits


def test_a_retried_document_starts_without_the_old_recovery_note(client):
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(), "application/pdf")}).json()["id"]
    s = client.test_session_factory()
    s.query(Document).filter(Document.id == doc_id).update({"error_message": " [Recovered after an interrupted run; retrying.]"})
    s.commit()
    s.close()
    res = PageExtractionResult(tests=[_t("Hemoglobin", "13.5", "g/dL")], page_notes=None)
    with patch("app.services.pipeline.llm_client.extract_page", return_value=res):
        process_pending(client, doc_id)
    detail = client.get(f"/reports/{doc_id}").json()
    assert detail["status"] == "complete" and detail["error_message"] is None


# --------------------------------------------------------------- settings

def test_sqlite_concurrency_can_be_raised_for_benchmarks_only():
    assert worker.effective_concurrency(3, "sqlite") == 1
    assert worker.effective_concurrency(3, "sqlite", sqlite_cap=2) == 2
    assert worker.effective_concurrency(3, "postgresql", sqlite_cap=1) == 3
