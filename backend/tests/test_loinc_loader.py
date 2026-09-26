from app.models.loinc import LoincCode, LoincAlias
from app.services.loinc_loader import seed_loinc_table, load_loinc_records, get_alias_index
from app.services.normalization import build_alias_index, lookup_exact


def test_load_loinc_records_are_well_formed():
    records = load_loinc_records()
    assert len(records) >= 50000, "the full Laboratory/ACTIVE LOINC table should have tens of thousands of codes"

    seen_codes = set()
    for rec in records:
        assert rec["loinc_num"], "every record needs a loinc_num"
        assert rec["loinc_num"] not in seen_codes, f"duplicate loinc_num {rec['loinc_num']}"
        seen_codes.add(rec["loinc_num"])
        assert rec["long_common_name"], f"{rec['loinc_num']} missing long_common_name"
        assert isinstance(rec.get("aliases", []), list)


def test_seed_loinc_table_is_idempotent(db_session):
    count1 = seed_loinc_table(db_session)
    count2 = seed_loinc_table(db_session)  # should no-op the second time
    assert count1 == count2
    assert db_session.query(LoincCode).count() == count1
    assert db_session.query(LoincAlias).count() > count1  # most codes have >1 alias


def test_alias_index_resolves_common_synonyms():
    """Uses get_alias_index() (not build_alias_index() directly) because the
    human-verified override layer (app/data/loinc_alias_overrides.json) is
    merged in at that level -- see that file's own comment for why it's
    needed on top of the full official table."""
    index = get_alias_index()

    cases = {
        "Hgb": "718-7",
        "HGB": "718-7",
        "Hemoglobin": "718-7",
        "WBC": "6690-2",
        "A1C": "4548-4",
        "HbA1c": "4548-4",
        "Na+": "2951-2",
        "SGPT": "1742-6",
        "LDL-C": "13457-7",
    }
    for alias, expected_code in cases.items():
        match = lookup_exact(alias, index)
        assert match is not None, f"expected alias '{alias}' to resolve"
        assert match["loinc_num"] == expected_code, f"'{alias}' resolved to {match['loinc_num']}, expected {expected_code}"


def test_alias_index_does_not_match_unrelated_text():
    index = build_alias_index(load_loinc_records())
    assert lookup_exact("some random unrelated phrase", index) is None


def test_build_alias_index_drops_genuinely_ambiguous_aliases_rather_than_guess():
    """Regression test for a real finding: at full-table scale, common bare
    abbreviations ('WBC', 'SGPT', 'LDL-C', ...) are legitimately listed by
    LOINC's own RELATEDNAMES2 against MANY distinct, unrelated-enough codes.
    build_alias_index() (without the override layer) must refuse to guess
    among them rather than silently pick whichever it saw first."""
    index = build_alias_index(load_loinc_records())
    assert lookup_exact("WBC", index) is None
    assert lookup_exact("SGPT", index) is None


def test_bare_specimen_dependent_terms_are_never_alias_overridden():
    """Regression guard for the urine-glucose mismapping bug: bare 'Glucose'
    (and 'Protein') must NOT resolve via the deterministic alias-exact stage
    at all, in either the raw full-table index or the override-merged one --
    only the lexical-search+LLM stage has the specimen context needed to
    pick serum vs. urine correctly."""
    raw_index = build_alias_index(load_loinc_records())
    merged_index = get_alias_index()
    for term in ("Glucose", "Protein"):
        assert lookup_exact(term, raw_index) is None
        assert lookup_exact(term, merged_index) is None


def test_get_alias_index_is_cached_and_consistent():
    idx1 = get_alias_index()
    idx2 = get_alias_index()
    assert idx1 is idx2  # module-level cache should return the same object
