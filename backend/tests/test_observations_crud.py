"""Manual create/read/update/delete of extracted rows -- how a user corrects
fallback (or LLM) output. None of these may ever call an LLM."""
from unittest.mock import patch

import pytest

from tests.test_api import client  # noqa: F401  (shared fixture)


@pytest.fixture()
def no_llm():
    with patch("app.services.loinc_mapping.llm_client.verify_mapping",
               side_effect=AssertionError("CRUD must never call the LLM")) as m:
        yield m


@pytest.fixture()
def doc_id(client):
    resp = client.post("/reports", files={"file": ("r.txt", b"Glucose 90 mg/dL", "text/plain")})
    return resp.json()["id"]


def _create(client, doc_id, **fields):
    body = {"document_id": doc_id, "original_test_name": "Hemoglobin", "value": "13.5", "unit": "g/dL"}
    body.update(fields)
    return client.post("/observations", json=body)


def test_create_manual_row_is_marked_manual_and_alias_mapped(client, doc_id, no_llm):
    resp = _create(client, doc_id)
    assert resp.status_code == 201
    row = resp.json()
    assert row["extraction_source"] == "manual" and row["is_edited"] is True
    assert row["extraction_confidence"] == 1.0
    assert row["loinc_code"] == "718-7" and row["mapping_status"] == "confirmed"
    assert row["mapping_stage"] == "alias_exact"

    detail = client.get(f"/reports/{doc_id}").json()
    assert [o["id"] for o in detail["observations"]] == [row["id"]]


def test_create_unknown_test_is_left_for_review_not_guessed(client, doc_id, no_llm):
    row = _create(client, doc_id, original_test_name="Zorbotronin level").json()
    assert row["loinc_code"] is None
    assert row["mapping_status"] == "needs_review"


def test_create_trims_and_blank_optional_fields_become_null(client, doc_id, no_llm):
    row = _create(client, doc_id, original_test_name="  Hemoglobin  ", unit="   ", flag="").json()
    assert row["original_test_name"] == "Hemoglobin"
    assert row["unit"] is None and row["flag"] is None


@pytest.mark.parametrize("body,status", [
    ({"original_test_name": "   "}, 422),
    ({"original_test_name": ""}, 422),
    ({"original_test_name": "x" * 513}, 422),
    ({"value": "v" * 513}, 422),
    ({"page_number": 0}, 422),
])
def test_create_validation(client, doc_id, no_llm, body, status):
    assert _create(client, doc_id, **body).status_code == status


def test_create_for_missing_document_is_404(client, no_llm):
    assert _create(client, "nope").status_code == 404


def test_get_one_and_missing(client, doc_id, no_llm):
    row = _create(client, doc_id).json()
    assert client.get(f"/observations/{row['id']}").json()["id"] == row["id"]
    assert client.get("/observations/missing").status_code == 404


def test_update_value_keeps_mapping_and_flags_edited(client, doc_id, no_llm):
    row = _create(client, doc_id).json()
    resp = client.patch(f"/observations/{row['id']}", json={"value": "14.2", "flag": "H"})
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["value"] == "14.2" and updated["flag"] == "H"
    assert updated["unit"] == "g/dL", "fields not in the request must be untouched"
    assert updated["loinc_code"] == "718-7"


def test_update_can_clear_an_optional_field(client, doc_id, no_llm):
    row = _create(client, doc_id).json()
    updated = client.patch(f"/observations/{row['id']}", json={"unit": None}).json()
    assert updated["unit"] is None


def test_rename_remaps_to_the_new_tests_code(client, doc_id, no_llm):
    row = _create(client, doc_id, original_test_name="Zorbotronin level").json()
    assert row["loinc_code"] is None
    updated = client.patch(f"/observations/{row['id']}", json={"original_test_name": "Hemoglobin"}).json()
    assert updated["loinc_code"] == "718-7" and updated["mapping_status"] == "confirmed"


def test_human_confirmed_mapping_survives_a_value_only_edit(client, doc_id, no_llm):
    row = _create(client, doc_id, original_test_name="Zorbotronin level").json()
    client.patch(f"/observations/{row['id']}/review", json={"loinc_code": "718-7", "mapping_status": "confirmed"})
    updated = client.patch(f"/observations/{row['id']}", json={"value": "1"}).json()
    assert updated["loinc_code"] == "718-7" and updated["mapping_stage"] == "human_review"


@pytest.mark.parametrize("body,status", [
    ({}, 400),
    ({"original_test_name": None}, 400),
    ({"original_test_name": "  "}, 422),
    ({"flag": "f" * 65}, 422),
])
def test_update_validation(client, doc_id, no_llm, body, status):
    row = _create(client, doc_id).json()
    assert client.patch(f"/observations/{row['id']}", json=body).status_code == status


def test_update_missing_is_404(client, no_llm):
    assert client.patch("/observations/missing", json={"value": "1"}).status_code == 404


def test_delete_removes_the_row(client, doc_id, no_llm):
    row = _create(client, doc_id).json()
    assert client.delete(f"/observations/{row['id']}").status_code == 204
    assert client.get(f"/observations/{row['id']}").status_code == 404
    assert client.get(f"/reports/{doc_id}").json()["observations"] == []
    assert client.delete(f"/observations/{row['id']}").status_code == 404


def test_edits_flow_into_the_fhir_export(client, doc_id, no_llm):
    row = _create(client, doc_id).json()
    client.patch(f"/observations/{row['id']}", json={"value": "15.1"})
    bundle = client.get(f"/reports/{doc_id}/fhir").json()
    obs = [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Observation"]
    assert len(obs) == 1
    assert obs[0]["valueQuantity"]["value"] == 15.1


def test_editing_a_suspect_value_revalidates_instead_of_keeping_stale_notes(client):
    resp = client.post("/reports", files={"file": ("r.txt", b"WBC          715    10*3/uL   4.0-11.0", "text/plain")})
    doc_id = resp.json()["id"]
    from tests.test_api import process_pending
    process_pending(client, doc_id)
    row = client.get(f"/reports/{doc_id}").json()["observations"][0]
    assert row["suggested_value"] == "7.15" and row["extraction_confidence"] < 0.7

    fixed = client.patch(f"/observations/{row['id']}", json={"value": "7.15"}).json()
    assert fixed["validation_notes"] is None and fixed["suggested_value"] is None
    assert fixed["extraction_confidence"] == 1.0, "a corrected row that now passes is fully trusted"

    still_bad = client.patch(f"/observations/{row['id']}", json={"value": "900"}).json()
    assert still_bad["suggested_value"] == "9" and still_bad["extraction_confidence"] < 0.7
