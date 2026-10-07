"""LOINC mapping against the Laboratory/ACTIVE LOINC reference table.

1. Deterministic alias/exact match (normalization.lookup_exact) -> confirmed,
   high confidence, no model call.
2. Candidate shortlist from an IN-MEMORY index (app/services/retrieval.py):
   IDF-weighted token overlap over names + synonyms, with specimen, unit and
   "commonly ordered" signals. This replaced per-row SQL `ILIKE '%x%'` scans
   over the whole table, which took seconds per row.
3. Verification. A clearly-winning candidate is accepted locally
   (`retrieval_auto`); the remaining ambiguous rows are judged by ONE batched
   LLM call per page (llm_client.verify_mappings_batch) instead of one call per
   row, and the LLM may only choose from the supplied candidates. If the LLM is
   unavailable the row is left needs_review with unverified suggestions - a
   closest-string match is never auto-assigned without evidence.

`map_rows` is the page-level entry point used by the pipeline (database-free,
so it can run on worker threads); `map_observation` maps a single row.
"""
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.loinc import LoincCode
from app.services import learned_mappings, llm_client, retrieval
from app.services.normalization import _clean

logger = logging.getLogger(__name__)

TOP_K = 6


def _top_k_candidates(query_text: str, db: Optional[Session] = None, k: int = TOP_K) -> list[dict]:
    """Back-compatible text entry point ("name | specimen: X | method"); the
    work is done by the in-memory index and `db` is no longer used."""
    parts = [p.strip() for p in query_text.split("|")]
    name = parts[0] if parts else ""
    specimen = method = None
    for extra in parts[1:]:
        if extra.lower().startswith("specimen:"):
            specimen = extra.split(":", 1)[1].strip() or None
        elif extra:
            method = extra
    return [h.summary() for h in retrieval.get_index().search(name, specimen=specimen, method=method, k=k)]


def _result(name, code=None, display=None, status="needs_review", conf=None, stage="lexical_llm", why=None, **extra):
    out = {
        "normalized_test_name": name,
        "loinc_code": code,
        "loinc_display": display,
        "mapping_status": status,
        "mapping_confidence": conf,
        "mapping_stage": stage,
        "mapping_rationale": why,
    }
    out.update(extra)
    return out


def _exact_match(db: Optional[Session], alias_index: dict, original_test_name: str) -> Optional[dict]:
    exact = alias_index.get(_clean(original_test_name))
    if not exact:
        return None
    rec = retrieval.get_index().get(exact["loinc_num"])
    display = rec["long_common_name"] if rec else None
    if display is None and db is not None:
        row = db.query(LoincCode).filter(LoincCode.loinc_num == exact["loinc_num"]).first()
        display = row.long_common_name if row else None
    return _result(
        exact["canonical_name"], exact["loinc_num"], display, "confirmed", 0.98, "alias_exact",
        "Exact alias/name match against the official LOINC reference table.",
    )


def _unverified_hint(name: str, candidates: list[dict], why: str) -> dict:
    hints = ", ".join(f"{c['loinc_num']} ({c['long_common_name']})" for c in candidates[:3])
    return _result(name, stage="lexical_only", why=why + (f" Unverified suggestions: {hints}." if hints else ""))


def _clear_winner(hits: list) -> bool:
    if not hits or settings.mapping_llm_mode != "ambiguous":
        return False
    top = hits[0]
    if top.score < settings.retrieval_accept_score:
        return False
    return len(hits) < 2 or (top.score - hits[1].score) >= settings.retrieval_accept_margin * top.score


def _apply_verdict(name: str, candidates: list[dict], verdict: dict) -> dict:
    chosen = verdict.get("chosen_loinc_num")
    try:
        confidence = float(verdict.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    rationale = verdict.get("rationale")
    if not chosen:
        return _result(name, status="unmapped", conf=confidence, why=rationale or "No candidate judged reliable.")
    rec = next((c for c in candidates if c["loinc_num"] == chosen), None)
    if rec is None:
        return _result(name, conf=confidence, why="Model chose a LOINC code outside the candidate list; discarded.")
    status = "confirmed" if confidence >= settings.mapping_confidence_threshold else "needs_review"
    return _result(rec["long_common_name"], rec["loinc_num"], rec["long_common_name"], status, confidence, "lexical_llm", rationale)


def map_rows(tests: list, alias_index: dict[str, dict], use_llm: bool = True) -> list[dict]:
    """Maps every row of a page at once. Touches no database and makes at most
    one (batched) LLM request, so it is safe and fast on a worker thread."""
    index = retrieval.get_index()
    out: list[Optional[dict]] = [None] * len(tests)
    pending: list[tuple[int, list[dict]]] = []
    for i, t in enumerate(tests):
        exact = _exact_match(None, alias_index, t.original_test_name)
        if exact:
            out[i] = exact
            continue
        remembered = learned_mappings.lookup(t.original_test_name, t.specimen, t.unit)
        rec = index.get(remembered) if remembered else None
        if rec:
            out[i] = _result(rec["long_common_name"], rec["loinc_num"], rec["long_common_name"], "confirmed", 0.95,
                             "learned", "Same test name, specimen and unit as a mapping that was confirmed before.")
            continue
        try:
            hits = index.search(t.original_test_name, specimen=t.specimen, unit=t.unit,
                                method=t.method, value=t.value, k=TOP_K)
        except Exception:
            logger.exception("Candidate search failed")
            out[i] = _result(t.original_test_name, stage="error", why="Candidate search failed; needs manual review.")
            continue
        cands = [h.summary() for h in hits]
        if not use_llm or settings.mapping_llm_mode == "off":
            out[i] = _unverified_hint(t.original_test_name, cands,
                                      "No exact alias match and AI verification was unavailable; needs manual review.")
        elif _clear_winner(hits):
            top = hits[0]
            out[i] = _result(top.long_common_name, top.loinc_num, top.long_common_name, "confirmed", 0.85,
                             "retrieval_auto", "Clear best match among LOINC candidates (name, specimen and unit agree).")
        elif not cands:
            out[i] = _result(t.original_test_name, status="unmapped", conf=0.0,
                             why="No candidate LOINC concepts found.")
        else:
            pending.append((i, cands))

    if pending:
        items = [
            {"i": n, "original_name": tests[i].original_test_name, "value": tests[i].value, "unit": tests[i].unit,
             "specimen": tests[i].specimen, "method": tests[i].method, "candidates": cands}
            for n, (i, cands) in enumerate(pending)
        ]
        try:
            verdicts = llm_client.verify_mappings_batch(items)
        except Exception:
            logger.exception("Batched LLM mapping verification failed")
            for i, cands in pending:
                r = _unverified_hint(tests[i].original_test_name, cands,
                                     "LLM verification call failed; needs manual review.")
                r["mapping_stage"] = "error"
                r["llm_failed"] = True
                out[i] = r
        else:
            for (i, cands), verdict in zip(pending, verdicts):
                out[i] = _apply_verdict(tests[i].original_test_name, cands, verdict or {})
    return out  # type: ignore[return-value]


def map_observation(
    db: Session,
    alias_index: dict[str, dict],
    original_test_name: str,
    value: Optional[str] = None,
    unit: Optional[str] = None,
    specimen: Optional[str] = None,
    method: Optional[str] = None,
    timing: Optional[str] = None,
    use_llm: bool = True,
) -> dict:
    """Maps a single row (manual "Add row"). `use_llm=False` skips verification
    entirely (zero API calls): deterministic alias matches still confirm,
    everything else is left needs_review with the candidates offered only as
    unverified hints."""
    exact = _exact_match(db, alias_index, original_test_name)
    if exact:
        return exact

    query_text = " | ".join(
        filter(None, [original_test_name, f"specimen: {specimen}" if specimen else None, method])
    )
    try:
        candidates = _top_k_candidates(query_text, db)
    except Exception:
        logger.exception("Lexical candidate search failed")
        return _result(original_test_name, stage="error", why="Candidate search failed; needs manual review.")

    if not use_llm:
        return _unverified_hint(original_test_name, candidates,
                                "No exact alias match and AI verification was unavailable; needs manual review.")

    try:
        verdict = llm_client.verify_mapping(
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
        return _result(original_test_name, stage="error", why="LLM verification call failed; needs manual review.",
                       llm_failed=True)
    return _apply_verdict(original_test_name, candidates, verdict)
