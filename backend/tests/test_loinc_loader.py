from app.models.loinc import LoincCode, LoincAlias
from app.services.loinc_loader import seed_loinc_table, load_loinc_records, get_alias_index
from app.services.normalization import build_alias_index, lookup_exact


def test_load_loinc_records_are_well_formed():
    records = load_loinc_records()
    # The CSV is built with --core-only (COMMON_TEST_RANK > 0), targeting
    # LOINC's ~18k ranked laboratory codes rather than the full ~62k table.
    # 15 000 is a conservative floor that guards against accidental truncation
    # while remaining well below the core-set's actual size.
    assert len(records) >= 15000, (
        "core-only loinc_lab_active.csv should have at least 15k ranked Laboratory/ACTIVE codes"
    )

    seen_codes = set()
    for rec in records:
        assert rec["loinc_num"], "every record needs a loinc_num"
        assert rec["loinc_num"] not in seen_codes, f"duplicate loinc_num {rec['loinc_num']}"
        seen_codes.add(rec["loinc_num"])
        assert rec["long_common_name"], f"{rec['loinc_num']} missing long_common_name"
        assert isinstance(rec.get("aliases", []), list)


def test_no_csv_field_exceeds_its_model_column_length():
    """Regression test for a real production crash: a real LOINC method_type
    value ('Thromboelastography...post heparin neutralization', 134 chars)
    exceeded the old method_type VARCHAR(128) column, and the resulting
    DataError from a real deploy only surfaced by tracing the exception
    directly -- it was otherwise swallowed as a bare, traceback-free
    'exit status 3'. This checks every column against the real data instead
    of just the specific value that happened to break once, so a future
    LOINC release with an even longer field fails a test locally instead of
    a live deploy."""
    limits = {
        "long_common_name": 512, "shortname": 256, "component": 256,
        "property": 64, "time_aspect": 64, "system": 128,
        "scale_type": 64, "method_type": 256, "class": 128, "example_units": 128,
    }
    records = load_loinc_records()
    for field, limit in limits.items():
        too_long = [
            (rec["loinc_num"], len(rec[field]))
            for rec in records
            if rec.get(field) and len(rec[field]) > limit
        ]
        assert not too_long, f"{field} column (max {limit}) too narrow for: {too_long[:5]}"

    max_alias_len = max(len(a) for rec in records for a in rec.get("aliases", []))
    assert max_alias_len <= 256, f"an alias ({max_alias_len} chars) exceeds the LoincAlias.alias VARCHAR(256) column"


def test_seed_loinc_table_is_idempotent(db_session):
    count1 = seed_loinc_table(db_session)
    count2 = seed_loinc_table(db_session)  # should no-op the second time
    assert count1 == count2
    assert db_session.query(LoincCode).count() == count1
    assert db_session.query(LoincAlias).count() > count1  # most codes have >1 alias


def test_seed_loinc_table_recovers_from_a_partially_seeded_prior_run(db_session):
    """Regression test for a real production crash: an earlier startup was
    interrupted partway through inserting LoincCode rows, leaving the table
    non-empty but incomplete. The next startup saw existing rows and (in the
    old code) skipped re-inserting codes entirely, then hit a real
    ForeignKeyViolation refreshing aliases for codes that were never actually
    inserted. seed_loinc_table must fully resync from a clean slate every
    time, not conditionally trust that a non-empty table means fully seeded."""
    # Simulate the exact partial-seed state: some rows present, but not
    # matching the real CSV's full set at all (standing in for "only the
    # first few chunks committed before a crash"). db_session comes from a
    # shared, already-fully-seeded engine (see conftest.py), so clear it
    # first to set up that scenario explicitly rather than assume empty.
    db_session.query(LoincAlias).delete()
    db_session.query(LoincCode).delete()
    db_session.add(LoincCode(loinc_num="FAKE-STALE-1", long_common_name="Stale leftover row"))
    db_session.commit()
    assert db_session.query(LoincCode).count() == 1

    count = seed_loinc_table(db_session)  # must not raise

    # See test_load_loinc_records_are_well_formed for why 15k, not 50k.
    assert count >= 15000
    assert db_session.query(LoincCode).count() == count
    assert db_session.query(LoincCode).filter(LoincCode.loinc_num == "FAKE-STALE-1").first() is None, (
        "the stale row from the interrupted prior run must not survive a full resync"
    )
    assert db_session.query(LoincAlias).count() > 0


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
