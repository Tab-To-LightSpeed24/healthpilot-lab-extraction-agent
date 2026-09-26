"""Deterministic first-pass name normalization.

This runs before any LLM/embedding call. If the original test name (case-
insensitively, punctuation-stripped) matches a known alias, we get a fast,
free, fully deterministic normalized name -- no model call needed. Anything
that doesn't match falls through to the mapping pipeline's embedding+LLM
stage using the original name as-is.
"""
import re
from typing import Optional

_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _clean(s: str) -> str:
    return _PUNCT_RE.sub(" ", s.lower()).strip()


def build_alias_index(loinc_records: list[dict]) -> dict[str, dict]:
    """Maps a cleaned alias string -> {loinc_num, canonical_name} for exact lookup."""
    index: dict[str, dict] = {}
    for rec in loinc_records:
        canonical = rec["shortname"] or rec["long_common_name"]
        for alias in rec.get("aliases", []) + [rec["long_common_name"], rec.get("shortname") or ""]:
            if not alias:
                continue
            key = _clean(alias)
            if key and key not in index:
                index[key] = {"loinc_num": rec["loinc_num"], "canonical_name": canonical}
    return index


def lookup_exact(original_test_name: str, alias_index: dict[str, dict]) -> Optional[dict]:
    return alias_index.get(_clean(original_test_name))
