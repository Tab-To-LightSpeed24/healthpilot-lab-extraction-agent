"""The 'Processed in X s' stat: stored when a job ends, exposed by the API."""
import time
from unittest.mock import patch

import fitz
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from app.schemas.extraction import ExtractedTest, PageExtractionResult
from tests.test_api import client, process_pending  # noqa: F401


def _pdf():
    d = fitz.open()
    d.new_page().insert_text((72, 72), "Hgb 13.5 g/dL", fontsize=11)
    data = d.tobytes()
    d.close()
    return data


def _ok():
    return PageExtractionResult(tests=[ExtractedTest(
        original_test_name="Hemoglobin", value="13.5", unit="g/dL", extraction_confidence=0.9)], page_notes=None)


def test_unprocessed_document_has_no_time(client):
    doc = client.post("/reports", files={"file": ("a.pdf", _pdf(), "application/pdf")}).json()
    assert doc["processing_seconds"] is None
    assert client.get(f"/reports/{doc['id']}").json()["processing_seconds"] is None


def test_completed_document_reports_how_long_it_took(client):
    def slow(image_png, text_layer):
        time.sleep(0.7)
        return _ok()

    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(), "application/pdf")}).json()["id"]
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=slow):
        process_pending(client, doc_id)
    detail = client.get(f"/reports/{doc_id}").json()
    assert detail["status"] == "complete"
    assert 0.7 <= detail["processing_seconds"] < 10
    assert any(f"Finished in {detail['processing_seconds']:.1f}s" in e["msg"] for e in detail["progress"])
    assert client.get("/reports").json()[0]["processing_seconds"] == detail["processing_seconds"]


def test_failed_document_also_reports_its_time(client):
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(), "application/pdf")}).json()["id"]
    with patch("app.services.pipeline.pdf_utils.load_pages", side_effect=RuntimeError("unreadable")):
        from sqlalchemy.orm import Session
        with patch.object(Session, "rollback", lambda self: None):
            process_pending(client, doc_id)
    detail = client.get(f"/reports/{doc_id}").json()
    assert detail["status"] == "failed" and detail["processing_seconds"] is not None


def test_migration_adds_the_column():
    from app.core.migrate import run_migrations
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    run_migrations(engine)
    assert "processing_seconds" in {c["name"] for c in inspect(engine).get_columns("documents")}
