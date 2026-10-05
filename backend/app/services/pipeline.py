import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.document import Document, DocumentStatus
from app.models.observation import Observation, MappingStatus
from app.services import pdf_utils, llm_client, loinc_mapping
from app.services.extraction.fallback import FallbackExtractor
from app.services.extraction.validation import validate_tests
from app.services.loinc_loader import get_alias_index, get_known_short_names
from app.services.normalization import standardize_unit

logger = logging.getLogger(__name__)


def _is_cancel_requested(db: Session, document_id: str) -> bool:
    """Re-reads just the cancel flag (not the whole ORM object) so a
    cooperative check between pages sees a cancellation requested by a
    different request/session, not a stale in-memory copy."""
    return bool(
        db.query(Document.cancel_requested).filter(Document.id == document_id).scalar()
    )


def _touch(db: Session, doc: Document) -> None:
    """Heartbeat: lets the worker's stuck-job sweep tell 'still actively
    being processed' apart from 'crashed mid-job and never came back'."""
    doc.updated_at = datetime.now(timezone.utc)
    db.commit()


MAX_PROGRESS_ENTRIES = 200


def _progress(db: Session, doc: Document, message: str, level: str = "info") -> None:
    """Appends one line to the document's live feed (shown in the UI while it
    processes) and commits, which doubles as the worker heartbeat."""
    log = list(doc.progress or [])
    log.append({"t": datetime.now(timezone.utc).isoformat(), "msg": message, "level": level})
    doc.progress = log[-MAX_PROGRESS_ENTRIES:]
    doc.updated_at = datetime.now(timezone.utc)
    db.commit()


def _short(text: str | None, limit: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def process_document(document_id: str, db: Session | None = None) -> None:
    """Extracts + maps every page of a document. Runs on the worker thread
    (see app/services/worker.py) and owns its own DB session unless one is
    passed in (tests, or a caller that wants transactional control)."""
    owns_session = db is None
    db = db or SessionLocal()
    fallback = None
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            logger.error("process_document: document %s not found", document_id)
            return

        doc.status = DocumentStatus.processing
        doc.progress = []
        doc.pages_done = 0
        _progress(db, doc, "Picked up from the queue")

        pages = pdf_utils.load_pages(doc.raw_content, doc.content_type)
        doc.num_pages = len(pages)
        _progress(db, doc, f"Opened {doc.filename}: {len(pages)} page(s) to read")

        alias_index = get_alias_index()
        any_failure = False
        failure_reasons: list[str] = []
        llm_notes: list[str] = []  # distinct reasons the AI path was skipped/abandoned
        pages_completed = 0

        # LLM extraction is optional. If it's disabled, or any page's attempt
        # fails for a reason that will likely repeat (timeout, credits, bad
        # key, provider outage), stop spending time on it for the rest of
        # this document and use the local rule-based fallback instead -- so a
        # dead/slow provider costs at most one bounded attempt per document,
        # not one per page. A ValueError (the model returned malformed JSON
        # for ONE page) only diverts that page.
        llm_available = settings.llm_enabled
        llm_skip_reason = None if llm_available else "AI extraction is disabled (LLM_ENABLED=false)"
        fallback = FallbackExtractor(doc.raw_content, doc.content_type)
        doc_started = time.monotonic()
        doc_budget = max(
            settings.fallback_document_budget_floor_seconds,
            settings.fallback_seconds_per_page * len(pages),
        )
        if not llm_available:
            _progress(db, doc, "AI extraction is turned off - using local extraction", "warn")
            fallback.prefetch(pages)  # LLM off: start OCR on scanned pages right away

        for page in pages:
            if _is_cancel_requested(db, document_id):
                doc.status = DocumentStatus.cancelled
                doc.error_message = (
                    f"Cancelled after {pages_completed}/{len(pages)} page(s) by user request."
                )
                _progress(db, doc, "Cancelled at your request", "warn")
                logger.info("Document %s cancelled after %s pages", document_id, pages_completed)
                return

            label = f"Page {page.page_number}/{len(pages)}"
            doc.pages_done = pages_completed
            result = None
            source = "llm"
            llm_error = None
            if llm_available:
                _progress(db, doc, f"{label}: sending to the AI model for extraction")
                try:
                    result = llm_client.extract_page(page.image_png, page.text)
                except Exception as exc:
                    logger.exception(
                        "LLM extraction failed for document %s page %s", document_id, page.page_number
                    )
                    llm_error = _describe(exc)
                    if isinstance(exc, ValueError):
                        _progress(db, doc, f"{label}: the AI returned unreadable output - reading this page locally", "warn")
                    else:
                        _progress(db, doc, f"AI unavailable ({_short(llm_error)}) - switching to local extraction", "warn")
                    if not isinstance(exc, ValueError):
                        llm_available = False
                        llm_skip_reason = llm_error
                        fallback.prefetch(p for p in pages if p.page_number > page.page_number)

            if result is None:
                source = "fallback"
                reason = llm_error or llm_skip_reason
                if reason and reason not in llm_notes:
                    llm_notes.append(reason)
                if time.monotonic() - doc_started > doc_budget:
                    any_failure = True
                    failure_reasons.append(
                        f"page {page.page_number}: local extraction time budget ({doc_budget:.0f}s) exceeded"
                    )
                    _progress(db, doc, f"{label}: skipped - local extraction time budget exceeded", "error")
                    pages_completed += 1
                    continue
                page_started = time.monotonic()
                _progress(db, doc, f"{label}: {fallback.describe(page)}")
                try:
                    result = fallback.extract(page)
                except Exception as fexc:
                    logger.warning("Fallback extraction unavailable for page %s: %s", page.page_number, fexc)
                    any_failure = True
                    failure_reasons.append(f"page {page.page_number}: local fallback unavailable ({fexc})")
                    _progress(db, doc, f"{label}: could not be read locally - {_short(str(fexc))}", "error")
                    pages_completed += 1
                    continue
                if not result.tests:
                    any_failure = True
                    failure_reasons.append(f"page {page.page_number}: local fallback found no recognizable test rows")
                    _progress(db, doc, f"{label}: no recognizable test rows found", "error")
                    pages_completed += 1
                    continue
                for t in result.tests:
                    t.extraction_confidence = min(
                        t.extraction_confidence, settings.fallback_extraction_confidence
                    )
                kept, dropped = validate_tests(result.tests, get_known_short_names(), ocr=(result.method == "ocr"))
                logger.info(
                    "Document %s page %s: fallback method=%s rows=%s dropped_junk=%s flagged=%s waited=%.1fs",
                    document_id, page.page_number, result.method, len(kept), dropped,
                    sum(bool(t.validation_notes) for t in kept), time.monotonic() - page_started,
                )
                result.tests = kept
                if not result.tests:
                    any_failure = True
                    failure_reasons.append(f"page {page.page_number}: local fallback found no recognizable test rows")
                    _progress(db, doc, f"{label}: no recognizable test rows found", "error")
                    pages_completed += 1
                    continue
                flagged = sum(bool(t.validation_notes) for t in kept)
                _progress(
                    db, doc,
                    f"{label}: found {len(kept)} test row(s)"
                    + (f"; validation flagged {flagged}" if flagged else "")
                    + (f", removed {dropped} junk row(s)" if dropped else ""),
                )
                if not doc.used_fallback:
                    doc.used_fallback = True
                    doc.fallback_reason = reason

            if source == "llm":
                _progress(db, doc, f"{label}: the AI found {len(result.tests)} test row(s)")
            _progress(db, doc, f"{label}: matching {len(result.tests)} row(s) to LOINC codes")
            for test in result.tests:
                try:
                    mapping = loinc_mapping.map_observation(
                        db=db,
                        alias_index=alias_index,
                        original_test_name=test.original_test_name,
                        value=test.value,
                        unit=test.unit,
                        specimen=test.specimen,
                        method=test.method,
                        timing=test.timing,
                        use_llm=(source == "llm" and llm_available),
                    )
                    if mapping.pop("llm_failed", False):
                        llm_available = False
                        llm_skip_reason = llm_skip_reason or "LLM mapping verification failed"
                except Exception:
                    logger.exception(
                        "Mapping failed for '%s' in document %s", test.original_test_name, document_id
                    )
                    mapping = {
                        "normalized_test_name": test.original_test_name,
                        "loinc_code": None,
                        "loinc_display": None,
                        "mapping_status": "needs_review",
                        "mapping_confidence": None,
                        "mapping_stage": "error",
                        "mapping_rationale": "Mapping pipeline raised an exception; needs manual review.",
                    }

                obs = Observation(
                    document_id=document_id,
                    page_number=page.page_number,
                    original_test_name=test.original_test_name,
                    normalized_test_name=mapping["normalized_test_name"],
                    value=test.value,
                    unit=standardize_unit(test.unit),
                    reference_range=test.reference_range,
                    specimen=test.specimen,
                    method=test.method,
                    timing=test.timing,
                    flag=test.flag,
                    loinc_code=mapping["loinc_code"],
                    loinc_display=mapping["loinc_display"],
                    mapping_status=MappingStatus(mapping["mapping_status"]),
                    mapping_confidence=mapping["mapping_confidence"],
                    mapping_stage=mapping["mapping_stage"],
                    mapping_rationale=mapping["mapping_rationale"],
                    extraction_confidence=test.extraction_confidence,
                    extraction_source=source,
                    validation_notes=test.validation_notes or None,
                    suggested_value=test.suggested_value,
                    raw_extraction=test.model_dump(),
                )
                db.add(obs)
            pages_completed += 1
            doc.pages_done = pages_completed
            _touch(db, doc)
            if _is_cancel_requested(db, document_id):
                doc.status = DocumentStatus.cancelled
                doc.error_message = (
                    f"Cancelled after {pages_completed}/{len(pages)} page(s) by user request."
                )
                _progress(db, doc, "Cancelled at your request", "warn")
                logger.info("Document %s cancelled after %s pages", document_id, pages_completed)
                return

        if _is_cancel_requested(db, document_id):
            doc.status = DocumentStatus.cancelled
            doc.error_message = (
                f"Cancelled after {pages_completed}/{len(pages)} page(s) by user request."
            )
            _progress(db, doc, "Cancelled at your request", "warn")
            logger.info("Document %s cancelled after %s pages", document_id, pages_completed)
            return

        doc.status = DocumentStatus.failed if any_failure and not doc.observations else DocumentStatus.complete
        if any_failure:
            ai_note = f"AI extraction unavailable: {'; '.join(llm_notes)}. " if llm_notes else ""
            reason_summary = (ai_note + "; ".join(failure_reasons))[:2000]
            doc.error_message = (
                reason_summary if doc.status == DocumentStatus.failed
                else f"One or more pages failed extraction; results may be incomplete. {reason_summary}"
            )
        doc.pages_done = len(pages)
        total_rows = len(doc.observations)
        if doc.status == DocumentStatus.failed:
            _progress(db, doc, "Finished without any extracted results", "error")
        else:
            _progress(
                db, doc,
                f"Finished - {total_rows} observation(s) extracted"
                + (" (some pages could not be read)" if any_failure else ""),
            )
        db.commit()
    except Exception as exc:
        logger.exception("process_document crashed for %s", document_id)
        db.rollback()
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc:
            doc.status = DocumentStatus.failed
            doc.error_message = str(exc)
            _progress(db, doc, f"Failed: {_short(str(exc))}", "error")
    finally:
        if fallback is not None:
            fallback.close()
        if owns_session:
            db.close()
