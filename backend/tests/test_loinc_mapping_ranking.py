"""Regression test for a real bug found while testing against the full 62k-
code LOINC table: a bare word like "glucose" matches hundreds of loosely
related specialized-panel codes, and without ranking, a per-token result cap
could truncate away the single routine test (plain serum Glucose) entirely.
Fixed by ordering each token's SQL match by LOINC's own COMMON_TEST_RANK
before applying the limit."""
from app.services.loinc_mapping import _top_k_candidates
from app.services.loinc_loader import seed_loinc_table


def test_common_routine_test_surfaces_ahead_of_obscure_variants(db_session):
    seed_loinc_table(db_session)

    candidates = _top_k_candidates("Glucose | specimen: Serum", db_session)
    codes = [c["loinc_num"] for c in candidates]

    assert "2345-7" in codes, "the routine serum Glucose code must not be truncated out of the shortlist"
    assert codes[0] == "2345-7", "the routine test should rank above obscure specialized-panel variants"


def test_specimen_specific_candidates_still_surface_for_urine(db_session):
    seed_loinc_table(db_session)

    candidates = _top_k_candidates("Glucose | specimen: Urine", db_session)
    codes = [c["loinc_num"] for c in candidates]

    assert "5792-7" in codes, "urine glucose candidate must be present so the LLM stage can pick it"
    assert codes[0] in {"5792-7", "53328-1"}, "a urine-specimen glucose code should rank first for a urine query"
