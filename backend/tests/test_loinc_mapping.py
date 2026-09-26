from unittest.mock import patch

from app.services import loinc_mapping
from app.services.loinc_loader import seed_loinc_table, get_alias_index


def test_map_observation_alias_exact_match_needs_no_model_call(db_session):
    seed_loinc_table(db_session)
    alias_index = get_alias_index()

    with patch.object(loinc_mapping.gemini_client, "embed_text") as mock_embed, \
         patch.object(loinc_mapping.gemini_client, "verify_mapping") as mock_verify:
        result = loinc_mapping.map_observation(
            db=db_session,
            alias_index=alias_index,
            original_test_name="Hgb",
            value="13.5",
            unit="g/dL",
        )

    assert result["mapping_status"] == "confirmed"
    assert result["mapping_stage"] == "alias_exact"
    assert result["loinc_code"] == "718-7"
    assert result["mapping_confidence"] >= 0.9
    mock_embed.assert_not_called()
    mock_verify.assert_not_called()


def test_map_observation_falls_back_to_embedding_llm_when_no_alias(db_session):
    seed_loinc_table(db_session)
    alias_index = get_alias_index()

    fake_candidates_reached = {}

    def fake_top_k(query_text, db, k=5):
        fake_candidates_reached["called"] = True
        return [
            {"loinc_num": "2345-7", "long_common_name": "Glucose [Mass/volume] in Serum or Plasma", "component": "Glucose", "system": "Ser/Plas"},
            {"loinc_num": "5792-7", "long_common_name": "Glucose [Mass/volume] in Urine by Test strip", "component": "Glucose", "system": "Urine"},
        ]

    with patch.object(loinc_mapping, "_top_k_candidates", side_effect=fake_top_k), \
         patch.object(loinc_mapping.gemini_client, "verify_mapping") as mock_verify:
        mock_verify.return_value = {
            "chosen_loinc_num": "2345-7",
            "confidence": 0.9,
            "rationale": "Serum specimen matches the fasting serum glucose concept.",
        }
        result = loinc_mapping.map_observation(
            db=db_session,
            alias_index=alias_index,
            original_test_name="Fasting Blood Sugar",  # not in alias table verbatim
            value="95",
            unit="mg/dL",
            specimen="Serum",
        )

    assert fake_candidates_reached.get("called") is True
    assert result["mapping_status"] == "confirmed"
    assert result["loinc_code"] == "2345-7"
    assert result["mapping_stage"] == "embedding_llm"


def test_map_observation_low_confidence_is_flagged_needs_review(db_session):
    seed_loinc_table(db_session)
    alias_index = get_alias_index()

    with patch.object(loinc_mapping, "_top_k_candidates", return_value=[
        {"loinc_num": "2345-7", "long_common_name": "Glucose [Mass/volume] in Serum or Plasma", "component": "Glucose", "system": "Ser/Plas"},
    ]), patch.object(loinc_mapping.gemini_client, "verify_mapping") as mock_verify:
        mock_verify.return_value = {"chosen_loinc_num": "2345-7", "confidence": 0.4, "rationale": "Weak match."}
        result = loinc_mapping.map_observation(
            db=db_session,
            alias_index=alias_index,
            original_test_name="Some Ambiguous Panel Value",
        )

    assert result["mapping_status"] == "needs_review"
    assert result["mapping_confidence"] == 0.4


def test_map_observation_no_reliable_candidate_is_unmapped(db_session):
    seed_loinc_table(db_session)
    alias_index = get_alias_index()

    with patch.object(loinc_mapping, "_top_k_candidates", return_value=[
        {"loinc_num": "2345-7", "long_common_name": "Glucose [Mass/volume] in Serum or Plasma", "component": "Glucose", "system": "Ser/Plas"},
    ]), patch.object(loinc_mapping.gemini_client, "verify_mapping") as mock_verify:
        mock_verify.return_value = {"chosen_loinc_num": None, "confidence": 0.1, "rationale": "No candidate is a reliable match."}
        result = loinc_mapping.map_observation(
            db=db_session,
            alias_index=alias_index,
            original_test_name="Completely Novel Assay XYZ",
        )

    assert result["mapping_status"] == "unmapped"
    assert result["loinc_code"] is None


def test_map_observation_embedding_failure_is_handled_gracefully_not_silently(db_session):
    seed_loinc_table(db_session)
    alias_index = get_alias_index()

    with patch.object(loinc_mapping, "_top_k_candidates", side_effect=RuntimeError("network down")):
        result = loinc_mapping.map_observation(
            db=db_session,
            alias_index=alias_index,
            original_test_name="Something Not In Alias Table",
        )

    assert result["mapping_status"] == "needs_review"
    assert result["loinc_code"] is None
    assert "review" in result["mapping_rationale"].lower()
