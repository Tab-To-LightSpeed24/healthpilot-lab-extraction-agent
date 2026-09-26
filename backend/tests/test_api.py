import io
from unittest.mock import patch

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base
from app.core.db import get_db
from app.main import app
from app.services.loinc_loader import seed_loinc_table
from app.schemas.extraction import PageExtractionResult, ExtractedTest


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    seed_session = TestSession()
    seed_loinc_table(seed_session)
    seed_session.close()

    # The upload endpoint's background task (app.services.pipeline) opens its
    # own DB session independent of the request-scoped `get_db` dependency,
    # so it must be pointed at the same test engine explicitly.
    import app.services.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "SessionLocal", TestSession)

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    engine.dispose()


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_loinc_search_returns_seeded_codes(client):
    resp = client.get("/loinc/search", params={"q": "Hemoglobin A1c"})
    assert resp.status_code == 200
    results = resp.json()
    assert any(r["loinc_num"] == "4548-4" for r in results)


def test_upload_rejects_unsupported_content_type(client):
    resp = client.post(
        "/reports",
        files={"file": ("report.zip", b"PK\x03\x04fakezip", "application/zip")},
    )
    assert resp.status_code == 415


def test_upload_rejects_empty_file(client):
    resp = client.post(
        "/reports",
        files={"file": ("report.pdf", b"", "application/pdf")},
    )
    assert resp.status_code == 400


def test_full_upload_to_observation_flow_with_mocked_extraction(client):
    """End-to-end through the real HTTP API and real DB, with only the Gemini
    network call mocked out (no API key available in this test environment).
    Uses 'Hgb' and 'Glucose' which resolve via the deterministic alias table,
    so the embedding/LLM mapping stage is never invoked here either."""
    fake_result = PageExtractionResult(
        tests=[
            ExtractedTest(
                original_test_name="Hgb",
                value="13.5",
                unit="g/dL",
                reference_range="12.0-15.5",
                extraction_confidence=0.95,
            ),
            ExtractedTest(
                original_test_name="Glucose",
                value="95",
                unit="mg/dL",
                extraction_confidence=0.9,
            ),
        ],
        page_notes=None,
    )

    pdf_bytes = _make_pdf_bytes("Hgb 13.5 g/dL\nGlucose 95 mg/dL")

    with patch("app.services.pipeline.gemini_client.extract_page", return_value=fake_result):
        upload_resp = client.post(
            "/reports",
            files={"file": ("cbc_panel.pdf", pdf_bytes, "application/pdf")},
        )

    assert upload_resp.status_code == 201
    doc_id = upload_resp.json()["id"]

    detail_resp = client.get(f"/reports/{doc_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()

    assert detail["status"] == "complete"
    assert detail["num_pages"] == 1
    assert len(detail["observations"]) == 2

    by_name = {o["original_test_name"]: o for o in detail["observations"]}
    assert by_name["Hgb"]["loinc_code"] == "718-7"
    assert by_name["Hgb"]["mapping_status"] == "confirmed"
    assert by_name["Hgb"]["value"] == "13.5"
    assert by_name["Glucose"]["loinc_code"] == "2345-7"

    # traceability: every observation must point back to its source document/page
    for obs in detail["observations"]:
        assert obs["document_id"] == doc_id
        assert obs["page_number"] == 1

    obs_list_resp = client.get("/observations", params={"document_id": doc_id})
    assert obs_list_resp.status_code == 200
    assert len(obs_list_resp.json()) == 2

    filtered = client.get("/observations", params={"mapping_status": "confirmed"})
    assert len(filtered.json()) == 2


def test_document_not_found_returns_404(client):
    resp = client.get("/reports/does-not-exist")
    assert resp.status_code == 404


def test_extraction_failure_marks_document_failed_not_silently_complete(client):
    pdf_bytes = _make_pdf_bytes("Some page")
    with patch("app.services.pipeline.gemini_client.extract_page", side_effect=RuntimeError("Gemini API error")):
        upload_resp = client.post(
            "/reports",
            files={"file": ("bad.pdf", pdf_bytes, "application/pdf")},
        )
    doc_id = upload_resp.json()["id"]
    detail = client.get(f"/reports/{doc_id}").json()
    assert detail["status"] == "failed"
    assert detail["observations"] == []
    assert detail["error_message"], "a failed document must surface why, not silently show error_message=null"
    assert "Gemini API error" in detail["error_message"]
