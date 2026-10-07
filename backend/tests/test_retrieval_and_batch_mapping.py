"""In-memory LOINC retrieval and batched mapping verification. No DB, no network."""
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.schemas.extraction import ExtractedTest
from app.services import llm_client, loinc_mapping, retrieval
from app.services.loinc_loader import get_alias_index


@pytest.fixture(scope="module")
def index():
    return retrieval.get_index()


def _t(name, value="1", unit=None, specimen=None, method=None):
    return ExtractedTest(original_test_name=name, value=value, unit=unit, specimen=specimen,
                         method=method, extraction_confidence=0.9)


# ------------------------------------------------------------- retrieval

def test_search_is_fast(index):
    index.search("Glucose", specimen="Serum", unit="mg/dL")          # warm
    started = time.perf_counter()
    for _ in range(50):
        index.search("Alanine aminotransferase", unit="U/L")
    assert (time.perf_counter() - started) / 50 < 0.05


def test_specimen_steers_the_ranking(index):
    serum = index.search("Glucose", specimen="Serum")[0]
    urine = index.search("Glucose", specimen="Urine")[0]
    assert "Serum" in serum.long_common_name or serum.system in ("Ser", "Ser/Plas")
    assert "Urine" in urine.long_common_name


def test_numeric_values_prefer_quantitative_codes_over_presence(index):
    hit = index.search("Glucose", specimen="Urine", value="95", unit="mg/dL")[0]
    assert "[Presence]" not in hit.long_common_name


def test_abbreviation_reaches_its_code_via_synonyms(index):
    codes = [h.loinc_num for h in index.search("Hgb", unit="g/dL")]
    assert "718-7" in codes


def test_nonsense_returns_nothing(index):
    assert index.search("Zzzqxv Blorptonin") == []
    assert index.search("") == []


def test_get_by_code(index):
    assert index.get("2345-7")["long_common_name"].startswith("Glucose")
    assert index.get("nope") is None


# ---------------------------------------------------- page-level mapping

def test_map_rows_alias_rows_need_no_model(index):
    with patch.object(llm_client, "verify_mappings_batch") as batch:
        out = loinc_mapping.map_rows([_t("Hemoglobin", "13.9", "g/dL"), _t("WBC", "6.8", "10*3/uL")], get_alias_index())
    batch.assert_not_called()
    assert [o["mapping_stage"] for o in out] == ["alias_exact", "alias_exact"]
    assert all(o["mapping_status"] == "confirmed" for o in out)


def test_ambiguous_rows_share_one_batched_call(index):
    rows = [_t("Specific Gravity"), _t("pH"), _t("Leukocyte Esterase"), _t("Hemoglobin", "13.9", "g/dL")]
    seen = {}

    def fake(items):
        seen["n"] = len(items)
        seen["names"] = [i["original_name"] for i in items]
        return [{"chosen_loinc_num": i["candidates"][0]["loinc_num"], "confidence": 0.9, "rationale": "ok"} for i in items]

    with patch.object(llm_client, "verify_mappings_batch", side_effect=fake) as batch:
        out = loinc_mapping.map_rows(rows, get_alias_index())
    assert batch.call_count == 1, "all ambiguous rows of a page must be verified in ONE request"
    assert seen["names"] == ["Specific Gravity", "pH", "Leukocyte Esterase"]
    assert out[3]["mapping_stage"] == "alias_exact"
    assert all(o["mapping_status"] == "confirmed" and o["mapping_stage"] == "lexical_llm" for o in out[:3])


def test_a_code_outside_the_candidate_list_is_discarded(index):
    with patch.object(llm_client, "verify_mappings_batch",
                      return_value=[{"chosen_loinc_num": "99999-9", "confidence": 0.99, "rationale": "x"}]):
        out = loinc_mapping.map_rows([_t("Specific Gravity")], get_alias_index())
    assert out[0]["loinc_code"] is None and out[0]["mapping_status"] == "needs_review"


def test_low_confidence_verdict_needs_review(index):
    with patch.object(llm_client, "verify_mappings_batch", side_effect=lambda items: [
            {"chosen_loinc_num": items[0]["candidates"][0]["loinc_num"], "confidence": 0.4, "rationale": "unsure"}]):
        out = loinc_mapping.map_rows([_t("pH")], get_alias_index())
    assert out[0]["mapping_status"] == "needs_review" and out[0]["loinc_code"]


def test_batch_failure_degrades_to_review_with_hints_and_flags_it(index):
    with patch.object(llm_client, "verify_mappings_batch", side_effect=RuntimeError("provider down")):
        out = loinc_mapping.map_rows([_t("Specific Gravity"), _t("pH")], get_alias_index())
    assert all(o["mapping_status"] == "needs_review" and o["llm_failed"] for o in out)
    assert all("Unverified suggestions" in o["mapping_rationale"] for o in out)


def test_without_llm_nothing_is_auto_assigned(index):
    with patch.object(llm_client, "verify_mappings_batch") as batch:
        out = loinc_mapping.map_rows([_t("Specific Gravity")], get_alias_index(), use_llm=False)
    batch.assert_not_called()
    assert out[0]["mapping_stage"] == "lexical_only" and out[0]["loinc_code"] is None


def test_unknown_test_is_unmapped_without_calling_the_model(index):
    with patch.object(llm_client, "verify_mappings_batch") as batch:
        out = loinc_mapping.map_rows([_t("Zzzqxv Blorptonin")], get_alias_index())
    batch.assert_not_called()
    assert out[0]["mapping_status"] == "unmapped"


def test_mode_off_never_calls_the_model(index, monkeypatch):
    monkeypatch.setattr(settings, "mapping_llm_mode", "off")
    with patch.object(llm_client, "verify_mappings_batch") as batch:
        out = loinc_mapping.map_rows([_t("pH")], get_alias_index())
    batch.assert_not_called()
    assert out[0]["mapping_status"] == "needs_review"


def test_clear_winner_is_accepted_locally_in_ambiguous_mode(index, monkeypatch):
    hit = lambda s: SimpleNamespace(score=s)
    monkeypatch.setattr(settings, "mapping_llm_mode", "ambiguous")
    assert loinc_mapping._clear_winner([hit(2.0), hit(1.0)])
    assert not loinc_mapping._clear_winner([hit(2.0), hit(1.9)])      # too close
    assert not loinc_mapping._clear_winner([hit(0.9), hit(0.1)])      # too weak
    monkeypatch.setattr(settings, "mapping_llm_mode", "all")
    assert not loinc_mapping._clear_winner([hit(2.0), hit(1.0)])


# ------------------------------------------------------- batched client

def _fake_response(payload):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])


def _items(n):
    return [{"i": k, "original_name": f"t{k}", "value": "1", "unit": None, "specimen": None, "method": None,
             "candidates": [{"loinc_num": f"{k}-0", "long_common_name": "x", "system": "Ser"}]} for k in range(n)]


def test_batch_client_makes_one_request_and_aligns_verdicts(monkeypatch):
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_response({"results": [
        {"i": 1, "chosen_loinc_num": "1-0", "confidence": 0.9, "rationale": "b"},
        {"i": 0, "chosen_loinc_num": "0-0", "confidence": 0.8, "rationale": "a"}]})
    monkeypatch.setattr(llm_client, "_get_client", lambda: client)
    out = llm_client.verify_mappings_batch(_items(2))
    assert client.chat.completions.create.call_count == 1
    assert [o["chosen_loinc_num"] for o in out] == ["0-0", "1-0"]      # re-ordered by "i"


def test_batch_client_fills_missing_verdicts_safely(monkeypatch):
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_response({"results": [
        {"i": 0, "chosen_loinc_num": "0-0", "confidence": 0.9}]})
    monkeypatch.setattr(llm_client, "_get_client", lambda: client)
    out = llm_client.verify_mappings_batch(_items(3))
    assert out[0]["chosen_loinc_num"] == "0-0"
    assert out[1]["chosen_loinc_num"] is None and out[2]["chosen_loinc_num"] is None


def test_batch_client_splits_large_inputs_into_chunks(monkeypatch):
    monkeypatch.setattr(settings, "mapping_batch_size", 4)
    client = MagicMock()

    def create(**kw):
        n = kw["messages"][0]["content"].count("candidates:")
        return _fake_response({"results": [{"i": 0, "chosen_loinc_num": None, "confidence": 0.0}] * 0})

    client.chat.completions.create.side_effect = create
    monkeypatch.setattr(llm_client, "_get_client", lambda: client)
    out = llm_client.verify_mappings_batch(_items(10))
    assert client.chat.completions.create.call_count == 3              # 4 + 4 + 2
    assert len(out) == 10


def test_batch_prompt_formats_and_guards_against_injection():
    p = llm_client.BATCH_MAPPING_PROMPT.format(items="[0] name='x'")
    assert "untrusted document" in p and "[0] name='x'" in p
    assert '"results"' in p


def test_batch_with_no_items_makes_no_request(monkeypatch):
    monkeypatch.setattr(llm_client, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("no request expected")))
    assert llm_client.verify_mappings_batch([]) == []
