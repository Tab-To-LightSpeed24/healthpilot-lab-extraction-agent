"""Deterministic first-pass name normalization.

This runs before any LLM call. If the original test name (case-
insensitively, punctuation-stripped) matches a known alias, we get a fast,
free, fully deterministic normalized name -- no model call needed. Anything
that doesn't match falls through to the mapping pipeline's lexical-search+LLM
stage using the original name as-is.
"""
import re
from typing import Optional

_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _clean(s: str) -> str:
    return _PUNCT_RE.sub(" ", s.lower()).strip()


def build_alias_index(loinc_records: list[dict]) -> dict[str, dict]:
    """Maps a cleaned alias string -> {loinc_num, canonical_name} for exact
    lookup.

    At full-LOINC-table scale, a casual abbreviation like "Hgb" is
    legitimately listed as a synonym for several distinct codes (e.g. routine
    Hemoglobin vs. a rare Hemoglobin-A-by-electrophoresis assay, vs. MCHC) --
    this is real ambiguity in the official data, not a data-quality bug.

    An earlier version of this function broke such ties using LOINC's
    COMMON_TEST_RANK (how often a code is ordered overall). That measures
    aggregate popularity, not which code a given alias actually refers to --
    in practice it picked MCHC over routine Hemoglobin for "Hgb" simply
    because MCHC happens to be ordered marginally more often in aggregate.
    That's a worse failure mode than not resolving the alias at all, so this
    now takes the more conservative position that matches the rest of this
    project's approach to LOINC mapping: when an alias maps to more than one
    distinct LOINC code with no reliable way to disambiguate here, drop it
    from this deterministic stage entirely rather than guess. It falls
    through to the lexical-search+LLM mapping stage instead, which has the
    observation's actual value/unit/specimen to disambiguate with -- context
    this purely-textual stage does not have.
    """
    candidates: dict[str, set[str]] = {}
    entry_by_code: dict[str, dict] = {}
    for rec in loinc_records:
        canonical = rec["shortname"] or rec["long_common_name"]
        entry_by_code[rec["loinc_num"]] = {"loinc_num": rec["loinc_num"], "canonical_name": canonical}
        for alias in rec.get("aliases", []) + [rec["long_common_name"], rec.get("shortname") or ""]:
            if not alias:
                continue
            key = _clean(alias)
            if not key:
                continue
            candidates.setdefault(key, set()).add(rec["loinc_num"])

    return {
        key: entry_by_code[next(iter(codes))]
        for key, codes in candidates.items()
        if len(codes) == 1
    }


def lookup_exact(original_test_name: str, alias_index: dict[str, dict]) -> Optional[dict]:
    return alias_index.get(_clean(original_test_name))


# Groups of known variants keyed by their canonical spelling. Expressed as
# "canonical -> variants" (rather than a flat "variant -> canonical" map) so
# adding a new synonym for an existing unit is a one-line edit to a single
# group instead of a new top-level dict entry.
_UNIT_VARIANT_GROUPS: dict[str, tuple[str, ...]] = {
    "g/dL": ("gm/dl", "g/dl"),
    "mill/cmm": ("mill/mm3", "mill/mm³", "mill/cumm", "million/mm3", "million/mm³", "million/cumm"),
}
_UNIT_LOOKUP: dict[str, str] = {
    variant: canonical
    for canonical, variants in _UNIT_VARIANT_GROUPS.items()
    for variant in variants
}

_OCR_ARTIFACT_RE = re.compile(r"\[(H|L)\]|\[\s*\]")
_MICRO_SIGN_RE = re.compile(r"µ")


def standardize_unit(raw_unit: Optional[str]) -> Optional[str]:
    """Standardizes colloquial/OCR-noisy unit strings into a canonical form
    (e.g. "gm/dl" -> "g/dL", "mill/cumm" -> "mill/cmm"). Reports vary in how
    they print units for the same concept; this keeps stored/displayed units
    consistent without changing the underlying value."""
    if not isinstance(raw_unit, str):
        return None
    stripped = _OCR_ARTIFACT_RE.sub("", raw_unit).strip()
    if not stripped:
        return None
    stripped = _MICRO_SIGN_RE.sub("u", stripped)
    if stripped.lower() == "ul":
        return "uL"
    return _UNIT_LOOKUP.get(stripped.lower(), stripped)
