"""Three-stage LOINC mapping.

1. Deterministic alias/exact match (normalization.lookup_exact) -> confirmed,
   high confidence, no model call.
2. Embedding similarity over the curated LOINC subset -> top-k candidates.
3. LLM re-ranks the candidates using full clinical context (specimen/method)
   and either confirms one, or says none are reliable -> needs_review /
   unmapped. This is what prevents "picked the closest string match" errors.
"""
import logging
from typing import Optional

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.loinc import LoincCode
from app.services import gemini_client
from app.services.normalization import _clean

logger = logging.getLogger(__name__)

TOP_K = 5

_embedding_cache: dict[str, np.ndarray] | None = None
_loinc_rows_cache: list[dict] | None = None


def _candidate_text(rec: dict) -> str:
    parts = [rec["long_common_name"]]
    if rec.get("component"):
        parts.append(rec["component"])
    if rec.get("system"):
        parts.append(f"specimen/system: {rec['system']}")
    return " | ".join(parts)


def _ensure_embeddings(db: Session):
    global _embedding_cache, _loinc_rows_cache
    if _embedding_cache is not None:
        return
    rows = db.query(LoincCode).all()
    _loinc_rows_cache = [
        {
            "loinc_num": r.loinc_num,
            "long_common_name": r.long_common_name,
            "component": r.component,
            "system": r.system,
        }
        for r in rows
    ]
    cache = {}
    for rec in _loinc_rows_cache:
        vec = gemini_client.embed_text(_candidate_text(rec))
        cache[rec["loinc_num"]] = np.array(vec, dtype=np.float32)
    _embedding_cache = cache


def _top_k_candidates(query_text: str, db: Session, k: int = TOP_K) -> list[dict]:
    _ensure_embeddings(db)
    query_vec = np.array(gemini_client.embed_text(query_text), dtype=np.float32)
    query_norm = np.linalg.norm(query_vec) or 1.0

    scored = []
    for rec in _loinc_rows_cache:
        vec = _embedding_cache[rec["loinc_num"]]
        sim = float(np.dot(query_vec, vec) / (query_norm * (np.linalg.norm(vec) or 1.0)))
        scored.append((sim, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [rec for _, rec in scored[:k]]


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
            "mapping_rationale": "Exact alias/name match against curated LOINC reference table.",
        }

    query_text = " | ".join(
        filter(None, [original_test_name, f"specimen: {specimen}" if specimen else None, method])
    )
    try:
        candidates = _top_k_candidates(query_text, db)
    except Exception:
        logger.exception("Embedding candidate search failed")
        return {
            "normalized_test_name": original_test_name,
            "loinc_code": None,
            "loinc_display": None,
            "mapping_status": "needs_review",
            "mapping_confidence": None,
            "mapping_stage": "error",
            "mapping_rationale": "Embedding search failed; needs manual review.",
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
            "mapping_stage": "embedding_llm",
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
            "mapping_stage": "embedding_llm",
            "mapping_rationale": "Model chose a LOINC code outside the candidate list; discarded.",
        }

    status = "confirmed" if confidence >= settings.mapping_confidence_threshold else "needs_review"
    return {
        "normalized_test_name": chosen_rec["long_common_name"],
        "loinc_code": chosen_rec["loinc_num"],
        "loinc_display": chosen_rec["long_common_name"],
        "mapping_status": status,
        "mapping_confidence": confidence,
        "mapping_stage": "embedding_llm",
        "mapping_rationale": rationale,
    }
