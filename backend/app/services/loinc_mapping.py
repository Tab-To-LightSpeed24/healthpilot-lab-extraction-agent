"""Three-stage LOINC mapping against the full ~62k-code Laboratory/ACTIVE
LOINC reference table (see app/services/loinc_loader.py for provenance).

1. Deterministic alias/exact match (normalization.lookup_exact) -> confirmed,
   high confidence, no model call. With the real official RELATEDNAMES2
   synonym lists (not a small hand-typed list), this stage alone now covers
   the large majority of common test names and abbreviations.
2. Lexical candidate shortlist: token-overlap search against the DB (name,
   component, and alias columns) -> top-k candidates. NOTE: this replaced an
   earlier embedding-similarity design. Embedding all ~62k candidate codes
   via the Gemini API (one call per code) is not viable on the available
   quota/time -- it would mean tens of thousands of extra API calls just to
   build a cache, on top of already-observed free-tier rate limiting.
   Lexical overlap is weaker for zero-word-overlap abbreviations (e.g. an
   abbreviation with no shared substring with its LOINC name), but stage 1's
   much larger real alias coverage absorbs most of exactly that case, and
   stage 3 still supplies clinical judgment on top of whatever stage 2 finds.
3. LLM re-ranks the candidates using full clinical context (specimen/method)
   and either confirms one, or says none are reliable -> needs_review /
   unmapped. This is what prevents "picked the closest string match" errors.
"""
import logging
import re
from typing import Optional

from sqlalchemy import case
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.loinc import LoincCode, LoincAlias
from app.services import gemini_client
from app.services.normalization import _clean

logger = logging.getLogger(__name__)

TOP_K = 6
MAX_QUERY_TOKENS = 6
ROWS_PER_TOKEN = 150
_UNRANKED = 10**9  # sort value standing in for "no COMMON_TEST_RANK on file"

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"in", "of", "by", "the", "a", "an", "and", "or", "test", "level", "specimen"}

# Ties are broken by this, ascending (lower = more commonly ordered), so a
# per-token LIMIT keeps the handful of routine tests instead of an arbitrary
# slice of the (often hundreds of) loosely-related codes sharing a word.
_RANK_SORT_KEY = case((LoincCode.common_test_rank > 0, LoincCode.common_test_rank), else_=_UNRANKED)


def _tokenize(text: str) -> list[str]:
    words = _WORD_RE.findall(text.lower())
    return [w for w in words if len(w) > 1 and w not in _STOPWORDS]


def _candidate_summary(code: LoincCode) -> dict:
    return {
        "loinc_num": code.loinc_num,
        "long_common_name": code.long_common_name,
        "component": code.component,
        "system": code.system,
    }


def _top_k_candidates(query_text: str, db: Session, k: int = TOP_K) -> list[dict]:
    """Ranks LOINC codes by how many query tokens appear somewhere in their
    name/component or a known alias -- a simple, dependency-free stand-in for
    semantic search that scales to the full table without embedding calls.

    A word like "glucose" alone matches hundreds of specialized-panel codes
    in the full table, so each token's SQL match is ordered by
    COMMON_TEST_RANK (most-commonly-ordered first) before the per-token
    LIMIT is applied -- otherwise the routine test (e.g. plain serum
    Glucose) can be truncated away entirely in favor of obscure variants
    that merely happen to sort first."""
    tokens = _tokenize(query_text)[:MAX_QUERY_TOKENS]
    if not tokens:
        return []

    hit_counts: dict[str, int] = {}
    best_rank: dict[str, int] = {}
    summaries: dict[str, dict] = {}

    def _record_hit(code: LoincCode):
        hit_counts[code.loinc_num] = hit_counts.get(code.loinc_num, 0) + 1
        rank = code.common_test_rank if code.common_test_rank > 0 else _UNRANKED
        best_rank[code.loinc_num] = min(rank, best_rank.get(code.loinc_num, _UNRANKED))
        summaries.setdefault(code.loinc_num, _candidate_summary(code))

    for token in tokens:
        like_pattern = f"%{token}%"

        for code in (
            db.query(LoincCode)
            .filter(
                LoincCode.long_common_name.ilike(like_pattern)
                | LoincCode.shortname.ilike(like_pattern)
                | LoincCode.component.ilike(like_pattern)
            )
            .order_by(_RANK_SORT_KEY)
            .limit(ROWS_PER_TOKEN)
        ):
            _record_hit(code)

        for code in (
            db.query(LoincCode)
            .join(LoincAlias, LoincAlias.loinc_num == LoincCode.loinc_num)
            .filter(LoincAlias.alias.ilike(like_pattern))
            .order_by(_RANK_SORT_KEY)
            .limit(ROWS_PER_TOKEN)
        ):
            _record_hit(code)

    ranked = sorted(hit_counts.keys(), key=lambda num: (-hit_counts[num], best_rank[num]))[:k]
    return [summaries[loinc_num] for loinc_num in ranked]


def map_observation(
    db: Session,
    alias_index: dict[str, dict],
    original_test_name: str,
    value: Optional[str] = None,
    unit: Optional[str] = None,
    specimen: Optional[str] = None,
    method: Optional[str] = None,
    timing: Optional[str] = None,
) -> dict:
    exact = alias_index.get(_clean(original_test_name))
    if exact:
        loinc = db.query(LoincCode).filter(LoincCode.loinc_num == exact["loinc_num"]).first()
        return {
            "normalized_test_name": exact["canonical_name"],
            "loinc_code": loinc.loinc_num,
            "loinc_display": loinc.long_common_name,
            "mapping_status": "confirmed",
            "mapping_confidence": 0.98,
            "mapping_stage": "alias_exact",
            "mapping_rationale": "Exact alias/name match against the official LOINC reference table.",
        }

    query_text = " | ".join(
        filter(None, [original_test_name, f"specimen: {specimen}" if specimen else None, method])
    )
    try:
        candidates = _top_k_candidates(query_text, db)
    except Exception:
        logger.exception("Lexical candidate search failed")
        return {
            "normalized_test_name": original_test_name,
            "loinc_code": None,
            "loinc_display": None,
            "mapping_status": "needs_review",
            "mapping_confidence": None,
            "mapping_stage": "error",
            "mapping_rationale": "Candidate search failed; needs manual review.",
        }

    try:
        verdict = gemini_client.verify_mapping(
            original_name=original_test_name,
            normalized_name=original_test_name,
            value=value,
            unit=unit,
            specimen=specimen,
            method=method,
            timing=timing,
            candidates=candidates,
        )
    except Exception:
        logger.exception("LLM mapping verification failed")
        return {
            "normalized_test_name": original_test_name,
            "loinc_code": None,
            "loinc_display": None,
            "mapping_status": "needs_review",
            "mapping_confidence": None,
            "mapping_stage": "error",
            "mapping_rationale": "LLM verification call failed; needs manual review.",
        }

    chosen = verdict.get("chosen_loinc_num")
    confidence = float(verdict.get("confidence") or 0.0)
    rationale = verdict.get("rationale")

    if not chosen:
        return {
            "normalized_test_name": original_test_name,
            "loinc_code": None,
            "loinc_display": None,
            "mapping_status": "unmapped",
            "mapping_confidence": confidence,
            "mapping_stage": "lexical_llm",
            "mapping_rationale": rationale or "No candidate judged reliable.",
        }

    chosen_rec = next((c for c in candidates if c["loinc_num"] == chosen), None)
    if chosen_rec is None:
        return {
            "normalized_test_name": original_test_name,
            "loinc_code": None,
            "loinc_display": None,
            "mapping_status": "needs_review",
            "mapping_confidence": confidence,
            "mapping_stage": "lexical_llm",
            "mapping_rationale": "Model chose a LOINC code outside the candidate list; discarded.",
        }

    status = "confirmed" if confidence >= settings.mapping_confidence_threshold else "needs_review"
    return {
        "normalized_test_name": chosen_rec["long_common_name"],
        "loinc_code": chosen_rec["loinc_num"],
        "loinc_display": chosen_rec["long_common_name"],
        "mapping_status": status,
        "mapping_confidence": confidence,
        "mapping_stage": "lexical_llm",
        "mapping_rationale": rationale,
    }
