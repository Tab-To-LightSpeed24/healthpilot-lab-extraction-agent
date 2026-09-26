import io
import threading
from unittest.mock import patch

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.db import get_db
from app.main import app
from app.schemas.extraction import PageExtractionResult, ExtractedTest
from app.services.pipeline import process_document


@pytest.fixture()
def client(monkeypatch, _seeded_engine):
    # Reuses the session-scoped, already-seeded (~62k LOINC codes) engine
    # from conftest.py instead of re-seeding the full table per test -- see
    # that fixture's docstring for why that matters at this table size.
    # Each test gets its own connection + transaction, rolled back
    # afterward, for Document/Observation isolation between tests.
    connection = _seeded_engine.connect()
    transaction = connection.begin()
    TestSession = sessionmaker(bind=connection)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    # The app's own startup lifespan seeds LOINC into the *real* default
    # engine (untouched by dependency_overrides) on every TestClient(app)
    # construction. Nothing during tests reads from that engine, so that
    # seeding is pure wasted time here -- neutralize it.
    import app.main as main_module

    monkeypatch.setattr(main_module, "seed_loinc_table", lambda db: 0)

    # Uploads now only enqueue (status=pending); a real background worker
    # thread normally picks them up (see app/services/worker.py), but tests
    # drive processing explicitly and deterministically via
    # process_pending() below instead of racing a real thread against a
    # shared SQLite connection (which isn't safely shared across threads).
    # The worker's own claim/recovery logic has its own dedicated tests
    # (test_worker.py) that don't need a real thread either.
    monkeypatch.setattr(main_module, "start_worker", lambda: threading.Event())

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        c.test_session_factory = TestSession  # exposed for the process_pending() helper below
        yield c
    app.dependency_overrides.clear()
    transaction.rollback()
    connection.close()


def process_pending(client: TestClient, doc_id: str) -> None:
    """Test stand-in for 'the worker picked this job up and ran it' --
    processes the document synchronously and deterministically against the
    same isolated test-transaction connection the request handlers use,
    instead of depending on a real background thread's timing."""
    session = client.test_session_factory()
    try:
        process_document(doc_id, db=session)
    finally:
        session.close()


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
    """End-to-end through the real HTTP API and real DB, with the Gemini
    network calls mocked out (no API key available in this test environment).
    'Hgb' resolves via the deterministic alias-override layer (no model call
    at all). 'Glucose' is deliberately NOT alias-exact -- it's specimen-
    dependent (serum vs urine mean different LOINC codes), so it exercises
    the real lexical candidate search (against the real seeded full LOINC
    table, not mocked) followed by a mocked LLM re-rank decision."""
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
                specimen="Serum",
                extraction_confidence=0.9,
            ),
        ],
        page_notes=None,
    )

    pdf_bytes = _make_pdf_bytes("Hgb 13.5 g/dL\nGlucose 95 mg/dL")

    with patch("app.services.pipeline.gemini_client.extract_page", return_value=fake_result), \
         patch("app.services.loinc_mapping.gemini_client.verify_mapping") as mock_verify:
        mock_verify.return_value = {
            "chosen_loinc_num": "2345-7",
            "confidence": 0.93,
            "rationale": "Serum specimen matches the serum/plasma glucose concept.",
        }
        upload_resp = client.post(
            "/reports",
            files={"file": ("cbc_panel.pdf", pdf_bytes, "application/pdf")},
        )
        assert upload_resp.status_code == 201
        doc_id = upload_resp.json()["id"]
        process_pending(client, doc_id)

    detail_resp = client.get(f"/reports/{doc_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()

    assert detail["status"] == "complete"
    assert detail["num_pages"] == 1
    assert len(detail["observations"]) == 2

    by_name = {o["original_test_name"]: o for o in detail["observations"]}
    assert by_name["Hgb"]["loinc_code"] == "718-7"
    assert by_name["Hgb"]["mapping_status"] == "confirmed"
    assert by_name["Hgb"]["mapping_stage"] == "alias_exact"
    assert by_name["Hgb"]["value"] == "13.5"
    assert by_name["Glucose"]["loinc_code"] == "2345-7"
    assert by_name["Glucose"]["mapping_stage"] == "lexical_llm"

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
        process_pending(client, doc_id)
    detail = client.get(f"/reports/{doc_id}").json()
    assert detail["status"] == "failed"
    assert detail["observations"] == []
    assert detail["error_message"], "a failed document must surface why, not silently show error_message=null"
    assert "Gemini API error" in detail["error_message"]


def test_fhir_bundle_endpoint_reflects_real_mapping_state(client):
    fake_result = PageExtractionResult(
        tests=[
            ExtractedTest(original_test_name="Hgb", value="13.5", unit="gm/dl", extraction_confidence=0.95),
        ],
        page_notes=None,
    )
    pdf_bytes = _make_pdf_bytes("Hgb 13.5 gm/dl")

    with patch("app.services.pipeline.gemini_client.extract_page", return_value=fake_result):
        upload_resp = client.post("/reports", files={"file": ("cbc.pdf", pdf_bytes, "application/pdf")})
        doc_id = upload_resp.json()["id"]
        process_pending(client, doc_id)

    fhir_resp = client.get(f"/reports/{doc_id}/fhir")
    assert fhir_resp.status_code == 200
    bundle = fhir_resp.json()

    assert bundle["resourceType"] == "Bundle"
    obs_resources = [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Observation"]
    assert len(obs_resources) == 1
    obs = obs_resources[0]
    assert obs["code"]["coding"][0]["code"] == "718-7"
    # the raw OCR-style unit "gm/dl" must come out standardized in the FHIR output too
    assert obs["valueQuantity"] == {"value": 13.5, "unit": "g/dL"}


def test_fhir_bundle_for_missing_document_returns_404(client):
    resp = client.get("/reports/does-not-exist/fhir")
    assert resp.status_code == 404


def test_upload_only_enqueues_does_not_process_synchronously(client):
    """The durable-queue redesign's core behavior change: uploading must
    return immediately with status=pending, not silently process inline
    (which would defeat the point of a queue a worker can restart/retry
    against)."""
    pdf_bytes = _make_pdf_bytes("Hgb 13.5 g/dL")
    resp = client.post("/reports", files={"file": ("cbc.pdf", pdf_bytes, "application/pdf")})
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"


def test_cancel_pending_document_is_immediate(client):
    pdf_bytes = _make_pdf_bytes("Hgb 13.5 g/dL")
    doc_id = client.post("/reports", files={"file": ("cbc.pdf", pdf_bytes, "application/pdf")}).json()["id"]

    resp = client.post(f"/reports/{doc_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_cancel_processing_document_stops_pipeline_between_pages(client):
    """The actual point of cancellation: a multi-page document already
    running should stop partway through instead of burning through every
    remaining page's API call -- exactly the scenario that used up real
    quota on a 19-page real-world document during manual testing."""
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Hgb 13.{i} g/dL", fontsize=11)
    pdf_bytes = doc.tobytes()
    doc.close()

    doc_id = client.post("/reports", files={"file": ("multi.pdf", pdf_bytes, "application/pdf")}).json()["id"]

    calls = {"count": 0}

    def fake_extract(image_png, text_layer):
        calls["count"] += 1
        if calls["count"] == 2:
            # Simulate a cancellation arriving mid-job, via a second,
            # independent request-scoped session -- exactly how the real
            # HTTP endpoint would reach it while the worker is mid-page.
            cancel_session = client.test_session_factory()
            try:
                from app.models.document import Document
                cancel_session.query(Document).filter(Document.id == doc_id).update({"cancel_requested": True})
                cancel_session.commit()
            finally:
                cancel_session.close()
        return PageExtractionResult(
            tests=[ExtractedTest(original_test_name="Hgb", value="13.5", unit="g/dL", extraction_confidence=0.9)],
            page_notes=None,
        )

    with patch("app.services.pipeline.gemini_client.extract_page", side_effect=fake_extract):
        process_pending(client, doc_id)

    detail = client.get(f"/reports/{doc_id}").json()
    assert detail["status"] == "cancelled"
    assert calls["count"] == 2, "must stop before reaching the 3rd page's extraction call"
    assert "Cancelled after" in detail["error_message"]


def test_cancel_already_complete_document_is_rejected(client):
    fake_result = PageExtractionResult(
        tests=[ExtractedTest(original_test_name="Hgb", value="13.5", unit="g/dL", extraction_confidence=0.9)],
        page_notes=None,
    )
    pdf_bytes = _make_pdf_bytes("Hgb 13.5 g/dL")
    with patch("app.services.pipeline.gemini_client.extract_page", return_value=fake_result):
        doc_id = client.post("/reports", files={"file": ("cbc.pdf", pdf_bytes, "application/pdf")}).json()["id"]
        process_pending(client, doc_id)

    resp = client.post(f"/reports/{doc_id}/cancel")
    assert resp.status_code == 409


def test_batch_upload_enqueues_all_files(client):
    pdf1 = _make_pdf_bytes("Hgb 13.5 g/dL")
    pdf2 = _make_pdf_bytes("Glucose 95 mg/dL")

    resp = client.post(
        "/reports/batch",
        files=[
            ("files", ("a.pdf", pdf1, "application/pdf")),
            ("files", ("b.pdf", pdf2, "application/pdf")),
        ],
    )
    assert resp.status_code == 201
    docs = resp.json()
    assert len(docs) == 2
    assert all(d["status"] == "pending" for d in docs)
    assert {d["filename"] for d in docs} == {"a.pdf", "b.pdf"}


def test_batch_upload_rejects_entirely_if_any_file_invalid(client):
    """No partial batches: if file 2 of 2 is invalid, file 1 must not have
    been queued either."""
    good_pdf = _make_pdf_bytes("Hgb 13.5 g/dL")

    resp = client.post(
        "/reports/batch",
        files=[
            ("files", ("a.pdf", good_pdf, "application/pdf")),
            ("files", ("b.zip", b"not a report", "application/zip")),
        ],
    )
    assert resp.status_code == 415

    all_reports = client.get("/reports").json()
    assert not any(r["filename"] == "a.pdf" for r in all_reports)


def test_human_review_confirms_a_correct_loinc_code(client):
    fake_result = PageExtractionResult(
        tests=[ExtractedTest(original_test_name="Some Odd Assay", value="1.0", extraction_confidence=0.9)],
        page_notes=None,
    )
    pdf_bytes = _make_pdf_bytes("Some Odd Assay 1.0")
    with patch("app.services.pipeline.gemini_client.extract_page", return_value=fake_result), \
         patch("app.services.loinc_mapping.gemini_client.verify_mapping",
               return_value={"chosen_loinc_num": None, "confidence": 0.1, "rationale": "no match"}):
        doc_id = client.post("/reports", files={"file": ("odd.pdf", pdf_bytes, "application/pdf")}).json()["id"]
        process_pending(client, doc_id)

    obs_id = client.get(f"/reports/{doc_id}").json()["observations"][0]["id"]
    assert client.get(f"/reports/{doc_id}").json()["observations"][0]["mapping_status"] == "unmapped"

    resp = client.patch(f"/observations/{obs_id}/review", json={"loinc_code": "718-7"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["loinc_code"] == "718-7"
    assert body["mapping_status"] == "confirmed"
    assert body["mapping_stage"] == "human_review"


def test_human_review_rejects_a_fake_loinc_code(client):
    fake_result = PageExtractionResult(
        tests=[ExtractedTest(original_test_name="Hgb", value="13.5", unit="g/dL", extraction_confidence=0.9)],
        page_notes=None,
    )
    pdf_bytes = _make_pdf_bytes("Hgb 13.5 g/dL")
    with patch("app.services.pipeline.gemini_client.extract_page", return_value=fake_result):
        doc_id = client.post("/reports", files={"file": ("cbc.pdf", pdf_bytes, "application/pdf")}).json()["id"]
        process_pending(client, doc_id)

    obs_id = client.get(f"/reports/{doc_id}").json()["observations"][0]["id"]
    resp = client.patch(f"/observations/{obs_id}/review", json={"loinc_code": "NOT-A-REAL-CODE"})
    assert resp.status_code == 400


def test_human_review_can_confirm_genuinely_unmapped(client):
    """A reviewer explicitly deciding 'no LOINC code applies here' is a
    distinct, valid outcome -- not an error -- and must not require a code."""
    fake_result = PageExtractionResult(
        tests=[ExtractedTest(original_test_name="Some Odd Assay", value="1.0", extraction_confidence=0.9)],
        page_notes=None,
    )
    pdf_bytes = _make_pdf_bytes("Some Odd Assay 1.0")
    with patch("app.services.pipeline.gemini_client.extract_page", return_value=fake_result), \
         patch("app.services.loinc_mapping.gemini_client.verify_mapping",
               return_value={"chosen_loinc_num": None, "confidence": 0.1, "rationale": "no match"}):
        doc_id = client.post("/reports", files={"file": ("odd.pdf", pdf_bytes, "application/pdf")}).json()["id"]
        process_pending(client, doc_id)

    obs_id = client.get(f"/reports/{doc_id}").json()["observations"][0]["id"]
    resp = client.patch(f"/observations/{obs_id}/review", json={"loinc_code": None, "mapping_status": "unmapped"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["loinc_code"] is None
    assert body["mapping_status"] == "unmapped"
    assert body["mapping_stage"] == "human_review"


def test_human_review_requires_code_unless_confirming_unmapped(client):
    fake_result = PageExtractionResult(
        tests=[ExtractedTest(original_test_name="Hgb", value="13.5", unit="g/dL", extraction_confidence=0.9)],
        page_notes=None,
    )
    pdf_bytes = _make_pdf_bytes("Hgb 13.5 g/dL")
    with patch("app.services.pipeline.gemini_client.extract_page", return_value=fake_result):
        doc_id = client.post("/reports", files={"file": ("cbc.pdf", pdf_bytes, "application/pdf")}).json()["id"]
        process_pending(client, doc_id)

    obs_id = client.get(f"/reports/{doc_id}").json()["observations"][0]["id"]
    resp = client.patch(f"/observations/{obs_id}/review", json={"loinc_code": None, "mapping_status": "confirmed"})
    assert resp.status_code == 400


def test_quality_summary_reports_review_counts(client):
    fake_result = PageExtractionResult(
        tests=[
            ExtractedTest(original_test_name="Hgb", value="13.5", unit="g/dL", extraction_confidence=0.95),
            ExtractedTest(original_test_name="Totally Unknown Thing", value="1", extraction_confidence=0.4),
        ],
        page_notes=None,
    )
    pdf_bytes = _make_pdf_bytes("Hgb 13.5 g/dL\nTotally Unknown Thing 1")
    with patch("app.services.pipeline.gemini_client.extract_page", return_value=fake_result), \
         patch("app.services.loinc_mapping.gemini_client.verify_mapping",
               return_value={"chosen_loinc_num": None, "confidence": 0.0, "rationale": "no match"}):
        doc_id = client.post("/reports", files={"file": ("cbc.pdf", pdf_bytes, "application/pdf")}).json()["id"]
        process_pending(client, doc_id)

    detail = client.get(f"/reports/{doc_id}").json()
    quality = detail["quality"]
    assert quality["total_observations"] == 2
    assert quality["confirmed_count"] == 1
    assert quality["unmapped_count"] == 1
    assert len(quality["low_confidence_extractions"]) == 1
    assert quality["low_confidence_extractions"][0]["original_test_name"] == "Totally Unknown Thing"
