"""Validation layer tests. The error cases below are not invented: they are
the OCR misreads actually observed on the degraded Apollo scan (WBC 7.15 read
as 715, Hb 10.9 as 109, RBC 4.6 as 46, garbled units 'ror'/'omer'/'o/at',
and junk labels from gauge graphics)."""
import pytest

from app.schemas.extraction import ExtractedTest
from app.services.extraction.validation import (
    MIN_CONFIDENCE,
    suggest_decimal_fix,
    validate_row,
    validate_tests,
)


def _t(name="WBC Count", value="7.2", unit="10*3/uL", rng="4 - 11", flag=None, conf=0.65):
    return ExtractedTest(
        original_test_name=name, value=value, unit=unit, reference_range=rng, flag=flag, extraction_confidence=conf
    )


@pytest.mark.parametrize("value,rng,expected", [
    ("715", "4 - 11 10³/mm³", "7.15"),
    ("109", "11.5 - 16.5", "10.9"),
    ("46", "3.7 - 5.6", "4.6"),
])
def test_observed_lost_decimal_misreads_get_the_right_suggestion(value, rng, expected):
    assert suggest_decimal_fix(value, rng) == expected


@pytest.mark.parametrize("value,rng", [
    ("420", "150 - 450"),     # correct reading, inside its range
    ("13.9", "13.0-17.0"),    # correct
    ("12", "4 - 11"),         # abnormal but plausible, not a slip
    ("71.5", "4 - 11"),       # already has a decimal point: not a lost one
    ("715", None),            # no range -> no independent evidence
    ("715", "<5.0"),          # one-sided range can't bound it
    ("Negative", "4 - 11"),
])
def test_correct_or_unverifiable_values_get_no_suggestion(value, rng):
    assert suggest_decimal_fix(value, rng) is None


def test_validation_never_changes_the_stored_value():
    t = _t(value="715")
    validate_tests([t])
    assert t.value == "715" and t.suggested_value == "7.15"
    assert any("lost decimal" in n for n in t.validation_notes)


def test_a_clean_row_passes_untouched():
    t = _t(value="7.2", conf=0.65)
    kept, dropped = validate_tests([t])
    assert dropped == 0 and kept == [t]
    assert t.validation_notes == [] and t.suggested_value is None and t.extraction_confidence == 0.65


def test_suspect_rows_fall_below_the_existing_low_confidence_threshold():
    from app.services.quality import LOW_EXTRACTION_CONFIDENCE_THRESHOLD

    t = _t(value="715")
    validate_tests([t])
    assert MIN_CONFIDENCE <= t.extraction_confidence < LOW_EXTRACTION_CONFIDENCE_THRESHOLD


@pytest.mark.parametrize("unit", ["ror", "omer", "o/at", "po"])
def test_garbled_units_are_flagged(unit):
    assert any("not recognized" in n for n in validate_row(_t(unit=unit)).notes)


@pytest.mark.parametrize("unit", ["g/dL", "10*3/uL", "mm/hr", "%", "pg", "fl", "gm/dl", "Million/ul", None])
def test_real_units_and_missing_units_are_not_flagged(unit):
    assert not any("not recognized" in n for n in validate_row(_t(unit=unit)).notes)


def test_flag_that_contradicts_the_value_is_noted():
    notes = validate_row(_t(value="9", rng="4 - 5", flag="Normal")).notes
    assert any("disagrees" in n for n in notes)
    assert not validate_row(_t(value="4.5", rng="4 - 5", flag="Normal")).notes


from app.services.normalization import _clean  # alias_index keys are cleaned names

ALIASES = {_clean(n): {} for n in ("WBC", "pH", "Na+", "CO2", "TSH", "A1C")}


@pytest.mark.parametrize("label", ["eee Ol", "a a", "n.0", "NS", "rv"])
def test_junk_labels_are_dropped_for_ocr_rows_only(label):
    t = _t(name=label)
    assert validate_row(t, ALIASES, ocr=True).drop is True
    assert validate_row(t, ALIASES, ocr=False).drop is False


def test_junk_filter_is_deliberately_conservative_about_wordlike_garbage():
    """A 4+ letter token ("ciel nd" was observed on a real scan) is kept and
    left for confidence/the user -- dropping on dictionary guesses would
    risk deleting real, unusual test names."""
    assert validate_row(_t(name="ciel nd"), ALIASES, ocr=True).drop is False


@pytest.mark.parametrize("label", ["WBC", "pH", "Na+", "CO2", "TSH", "Hemoglobin", "Platelet Count", "A1C"])
def test_real_test_names_are_never_dropped_as_junk(label):
    assert validate_row(_t(name=label), ALIASES, ocr=True).drop is False
    if len(label) >= 3:  # with no name set at all, only abbreviation-shaped labels can be vouched for
        assert validate_row(_t(name=label), None, ocr=True).drop is False


def test_validate_tests_reports_how_many_rows_it_dropped():
    kept, dropped = validate_tests([_t(name="WBC"), _t(name="eee Ol"), _t(name="Hemoglobin")], ALIASES, ocr=True)
    assert [t.original_test_name for t in kept] == ["WBC", "Hemoglobin"] and dropped == 1


def test_real_observed_apollo_scan_row_is_fully_diagnosed():
    """The actual degraded-scan output for the WBC row."""
    t = _t(name="WBC Count(Optical(Light scatter)", value="715", unit="10°/mm*", rng="4 - 11", flag="Normal")
    v = validate_row(t, ocr=True)
    assert v.suggested_value == "7.15" and not v.drop
    assert len(v.notes) >= 2


def test_gauge_tick_numbers_parsed_as_a_unit_are_dropped_for_ocr_rows():
    """Observed on a real scan: label 'ciel', value 0.0, 'unit' 150.0."""
    t = _t(name="ciel", value="0.0", unit="150.0", rng=None)
    assert validate_row(t, ALIASES, ocr=True).drop is True
    assert validate_row(t, ALIASES, ocr=False).drop is False


@pytest.mark.parametrize("read,expected", [("C02", "CO2"), ("TSH", None), ("pH", None), ("Zzq", None)])
def test_confusable_character_labels_are_corrected_only_to_exact_alias_hits(read, expected):
    """Observed in a Linux Tesseract run: "CO2" read as "C02" (zero for O)."""
    from app.services.extraction.validation import corrected_label
    assert corrected_label(read, ALIASES) == expected


def test_a_misread_short_label_is_corrected_not_dropped_as_junk():
    t = _t(name="C02", value="25", unit="mmol/L", rng="22-29")
    kept, dropped = validate_tests([t], ALIASES, ocr=True)
    assert dropped == 0 and kept[0].original_test_name == "CO2"
    assert any("misread" in n for n in kept[0].validation_notes)


# ---- against the REAL LOINC-derived name set (the toy sets above can't catch
# a filter that deletes real tests; this is the regression for exactly that) ----

@pytest.mark.parametrize("label", ["CO2", "pH", "K", "Na", "Cl", "Mg", "BUN", "TSH", "aPTT", "eGFR", "INR", "PT", "HbA1c", "ALT"])
def test_real_short_test_names_survive_the_filter_with_the_real_name_set(label):
    from app.services.loinc_loader import get_known_short_names
    assert validate_row(_t(name=label), get_known_short_names(), ocr=True).drop is False


def test_real_name_set_corrects_the_observed_co2_misread():
    from app.services.loinc_loader import get_known_short_names
    kept, dropped = validate_tests([_t(name="C02", value="25", unit="mmol/L", rng="22-29")], get_known_short_names(), ocr=True)
    assert dropped == 0 and kept[0].original_test_name == "CO2"


@pytest.mark.parametrize("label", ["eee Ol", "a a", "n.0"])
def test_observed_junk_is_still_dropped_with_the_real_name_set(label):
    """(Junk that happens to equal a real LOINC abbreviation, e.g. "rv", is
    deliberately kept: the filter errs toward keeping rows.)"""
    from app.services.loinc_loader import get_known_short_names
    assert validate_row(_t(name=label), get_known_short_names(), ocr=True).drop is True
