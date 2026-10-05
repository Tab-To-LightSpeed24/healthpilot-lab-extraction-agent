"""Phase 3a: sanity checks and a real confidence score for rule-parsed /
OCR'd rows, aimed squarely at the failure modes MEASURED on the scan
fixtures rather than generic hygiene:

  * lost decimal point    "7.15" read as "715", "10.9" as "109", "4.6" as "46"
  * garbled unit          "ror", "omer", "o/at" (OCR mangling "10^3/mm^3", "g/dl")
  * junk rows             graphics/gauge noise read as text ("eee Ol", "NS")
  * value vs printed flag "109 ... Normal" against a printed 11.5-16.5 range

Design rules:
  * NEVER silently change a clinical value. A suspected lost decimal produces
    a `suggested_value` and a note; the stored value stays exactly what was
    read, and the UI offers a one-click apply.
  * Rows are dropped only when they are unambiguous junk (no real word in the
    label AND no deterministic alias match); everything doubtful is kept but
    flagged and scored low.
  * Confidence reuses the existing 0.7 low-confidence threshold from
    app/services/quality.py instead of inventing another cutoff.
"""
import re
from dataclasses import dataclass, field
from typing import Collection, Dict, List, Optional, Tuple

from app.schemas.extraction import ExtractedTest
from app.services.normalization import _UNIT_LOOKUP, _clean, standardize_unit

# Where a candidate corrected value must land, relative to the printed
# reference range [lo, hi], to be offered as a suggestion. Deliberately wider
# than the range itself: abnormal results legitimately fall outside it
# (e.g. a real Hb of 10.9 against 11.5-16.5).
SUGGESTION_BAND = (0.5, 2.0)
# The read value must be at least this many times the range's upper bound
# before a decimal slip is suspected (a value only a bit above range is just
# an abnormal result, not a misread).
SUSPECT_FACTOR_OVER_HIGH = 5.0

PENALTY_NOTE = 0.10
PENALTY_LOST_DECIMAL = 0.25
PENALTY_UNIT = 0.10
PENALTY_FLAG_MISMATCH = 0.10
MIN_CONFIDENCE = 0.20

_RANGE = re.compile(r"(\d+\.?\d*)\s*-\s*(\d+\.?\d*)")
_NUMBER = re.compile(r"^[<>]?\s?(\d+\.?\d*)$")
_POWER_OF_TEN = re.compile(r"^x?10[\*\^]?\d+$|^10[²³⁰-⁹]$|^10\^\d+$", re.IGNORECASE)
# Building blocks of real lab units. A compound unit ("g/dL", "10*3/uL",
# "mm/hr") is accepted only if BOTH sides are known pieces -- merely "letters
# slash letters" is not enough, or OCR garbage like "o/at" would pass.
_NUMERATORS = {
    "g", "mg", "ug", "ng", "pg", "fg", "kg", "mmol", "umol", "nmol", "pmol", "meq", "eq", "iu", "u", "mu", "miu",
    "mill", "million", "millions", "cells", "cell", "mm", "cm", "l", "dl", "ml", "ul", "fl", "sec", "s", "mosm",
    "ratio", "copies", "tests",
}
_DENOMINATORS = {
    "l", "dl", "ml", "ul", "cumm", "cmm", "mm3", "mm³", "cm3", "hr", "h", "min", "d", "day", "hpf", "lpf", "cell",
    "cells", "g", "mg", "kg", "mol", "mmol", "creat",
}
_KNOWN_UNIT_VALUES = {v.lower() for v in _UNIT_LOOKUP.values()} | {
    "%", "g", "mg", "ug", "ng", "pg", "fl", "sec", "s", "iu", "u", "mmol", "meq",
}


# Character pairs OCR routinely swaps in short labels ("CO2" -> "C02").
_CONFUSABLE = (("0", "O"), ("1", "l"), ("1", "I"), ("5", "S"), ("8", "B"))


def corrected_label(label: str, known_names: Optional[Collection[str]]) -> Optional[str]:
    """If `label` isn't a known alias but a confusable-character variant is,
    return that variant. Only ever returns an exact alias hit -- never a
    guess -- so a correction is as deterministic as the alias match itself."""
    if not known_names or _clean(label) in known_names:
        return None
    for a, b in _CONFUSABLE:
        for variant in (label.replace(a, b), label.replace(b, a)):
            if variant != label and _clean(variant) in known_names:
                return variant
    return None


@dataclass
class Validation:
    corrected_label: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    suggested_value: Optional[str] = None
    confidence: float = 0.0
    drop: bool = False


def _parse_range(text: Optional[str]) -> Optional[Tuple[float, float]]:
    if not text:
        return None
    m = _RANGE.search(text)
    if not m:
        return None
    lo, hi = float(m.group(1)), float(m.group(2))
    return (lo, hi) if lo < hi else None


def _number(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    m = _NUMBER.match(value.strip())
    return float(m.group(1)) if m else None


def _format_like(original: str, number: float) -> str:
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text


def suggest_decimal_fix(value: Optional[str], reference_range: Optional[str]) -> Optional[str]:
    """If `value` is far above the printed range's upper bound but exactly one
    decimal-point placement (divide by 10, 100 or 1000) lands in the plausible
    band around the range, return that value as a string; else None. Needs a
    parseable range -- without one there is no independent evidence."""
    rng = _parse_range(reference_range)
    n = _number(value)
    if rng is None or n is None or "." in (value or "").lstrip("<> "):
        return None
    lo, hi = rng
    if n < hi * SUSPECT_FACTOR_OVER_HIGH:
        return None
    band_lo, band_hi = lo * SUGGESTION_BAND[0], hi * SUGGESTION_BAND[1]
    fits = [k for k in (10, 100, 1000) if band_lo <= n / k <= band_hi]
    if len(fits) != 1:
        return None
    return _format_like(value, n / fits[0])


def _flag_conflicts(flag: Optional[str], n: Optional[float], rng: Optional[Tuple[float, float]]) -> bool:
    if not flag or n is None or rng is None:
        return False
    lo, hi = rng
    f = flag.strip().lower()
    if f in ("normal", "n") and not (lo <= n <= hi):
        return True
    if f in ("h", "high") and n <= hi:
        return True
    if f in ("l", "low") and n >= lo:
        return True
    return False


def _unit_recognized(unit: Optional[str]) -> bool:
    if not unit:
        return True  # a missing unit is not a garbled one
    std = (standardize_unit(unit) or "").strip().lower()
    if std in _KNOWN_UNIT_VALUES:
        return True
    if "/" not in std:
        return False
    num, _, den = std.partition("/")
    return (num in _NUMERATORS or bool(_POWER_OF_TEN.match(num))) and (
        den in _DENOMINATORS or bool(_POWER_OF_TEN.match(den))
    )


def _is_junk_label(label: str, known_names: Optional[Collection[str]]) -> bool:
    """True only for labels that are unambiguously not a test name. Kept
    (and left to confidence/the user) if it has a word-length token, is a
    known short LOINC name ("pH", "CO2", "K"), or merely LOOKS like a lab
    abbreviation ("INR" is absent from the LOINC name set but is real). A
    first version keyed on the deterministic alias index alone and would have
    deleted real "pH"/"CO2" rows -- that index deliberately omits ambiguous
    aliases."""
    words = re.findall(r"[A-Za-z]+", label)
    if any(len(w) >= 4 for w in words):
        return False
    if known_names is not None and _clean(label) in known_names:
        return False
    compact = label.replace("+", "").replace("-", "")
    abbreviation_shaped = " " not in label and 3 <= len(label) <= 6 and compact.isalnum() and any(c.isupper() for c in label)
    return not abbreviation_shaped


def validate_row(test: ExtractedTest, known_names: Optional[Collection[str]] = None, ocr: bool = False) -> Validation:
    v = Validation(confidence=test.extraction_confidence)
    n = _number(test.value)
    rng = _parse_range(test.reference_range)

    if ocr:
        fixed = corrected_label(test.original_test_name, known_names)
        if fixed:
            v.corrected_label = fixed
            v.notes.append(f"label '{test.original_test_name}' looks like an OCR misread of '{fixed}'; corrected")

    if ocr and not v.corrected_label and _is_junk_label(test.original_test_name, known_names):
        v.drop = True
        v.notes.append("label does not look like a test name")
        return v

    # A real unit is never a bare number. On OCR'd gauge widgets the tick-mark
    # sequence ("0.0  150.0  450.0") parses as value + "unit", which is how a
    # graphic becomes a phantom test row.
    if ocr and test.unit and re.fullmatch(r"[<>]?\d+\.?\d*", test.unit.strip()):
        v.drop = True
        v.notes.append("unit is a bare number (gauge/graphic noise)")
        return v

    if n is None and test.value is not None and not re.match(r"^[A-Za-z][A-Za-z \-]*$", test.value):
        v.notes.append(f"value '{test.value}' is not a number or a recognized result")
        v.confidence -= PENALTY_NOTE

    suggestion = suggest_decimal_fix(test.value, test.reference_range)
    if suggestion is not None:
        v.suggested_value = suggestion
        v.notes.append(
            f"value {test.value} is far above the printed range {test.reference_range}; "
            f"possible lost decimal point (did you mean {suggestion}?)"
        )
        v.confidence -= PENALTY_LOST_DECIMAL

    if not _unit_recognized(test.unit):
        v.notes.append(f"unit '{test.unit}' is not recognized (possible OCR error)")
        v.confidence -= PENALTY_UNIT

    if _flag_conflicts(test.flag, n, rng):
        v.notes.append(f"printed flag '{test.flag}' disagrees with value {test.value} vs range {test.reference_range}")
        v.confidence -= PENALTY_FLAG_MISMATCH

    v.confidence = round(max(MIN_CONFIDENCE, min(1.0, v.confidence)), 2)
    return v


def validate_tests(
    tests: List[ExtractedTest], known_names: Optional[Collection[str]] = None, ocr: bool = False
) -> Tuple[List[ExtractedTest], int]:
    """Annotates each test in place (confidence, validation_notes,
    suggested_value) and returns (kept tests, number dropped as junk)."""
    kept: List[ExtractedTest] = []
    dropped = 0
    for test in tests:
        v = validate_row(test, known_names, ocr=ocr)
        if v.drop:
            dropped += 1
            continue
        if v.corrected_label:
            test.original_test_name = v.corrected_label
        test.extraction_confidence = v.confidence
        test.validation_notes = v.notes
        test.suggested_value = v.suggested_value
        kept.append(test)
    return kept, dropped
