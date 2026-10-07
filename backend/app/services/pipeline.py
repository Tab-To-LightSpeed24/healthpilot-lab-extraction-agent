import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.document import Document, DocumentStatus
from app.models.observation import Observation, MappingStatus
from app.services import learned_mappings, pdf_utils, llm_client, loinc_mapping
from app.services.extraction.fallback import FallbackExtractor
from app.services.extraction.validation import validate_tests
from app.services.loinc_loader import get_alias_index, get_known_short_names
from app.services.normalization import standardize_unit

logger = logging.getLogger(__name__)


def _is_cancel_requested(db: Session, document_id: str) -> bool:
    """Re-reads just the cancel flag (not the whole ORM object) so a
    cooperative check between pages sees a cancellation requested by a
    different request/session, not a stale in-memory copy."""
    flag = bool(
        db.query(Document.cancel_requested).filter(Document.id == document_id).scalar()
    )
    # End the read transaction: on SQLite an open read lock blocks every other writer's commit
    # (another document's worker could stall for the whole busy timeout). Any pending progress
    # changes are simply persisted - which is wanted anyway.
    db.commit()
    return flag


def _touch(db: Session, doc: Document) -> None:
    """Heartbeat: lets the worker's stuck-job sweep tell 'still actively
    being processed' apart from 'crashed mid-job and never came back'."""
    doc.updated_at = datetime.now(timezone.utc)
    db.commit()
    doc._last_commit = time.monotonic()


MAX_PROGRESS_ENTRIES = 200


COMMIT_EVERY_SECONDS = 0.8


def _progress(db: Session, doc: Document, message: str, level: str = "info", force: bool = False) -> None:
    """Appends one line to the document's live feed (shown in the UI while it processes).
    Commits - which doubles as the worker heartbeat - at most every ~0.8 s unless forced:
    each commit is a network round trip to the database, and a big document emits dozens of
    lines (that used to be ~40 s of pure waiting on a 19-page report). Page results, status
    changes and the end of the job always commit."""
    log = list(doc.progress or [])
    log.append({"t": datetime.now(timezone.utc).isoformat(), "msg": message, "level": level})
    doc.progress = log[-MAX_PROGRESS_ENTRIES:]
    doc.updated_at = datetime.now(timezone.utc)
    now = time.monotonic()
    if force or now - getattr(doc, "_last_commit", 0.0) >= COMMIT_EVERY_SECONDS:
        db.commit()
        doc._last_commit = now


def _short(text: str | None, limit: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


class _CancelledWhileWaiting(Exception):
    """The user cancelled while we were waiting on the AI."""


class _LlmSkipped(Exception):
    """A queued extraction that never started because the provider had already
    been judged unusable (or the job was cancelled)."""


def _llm_inputs(page) -> tuple:
    """(image bytes, text) for one page's AI request. A digital PDF page with a solid text
    layer is sent as text only (nothing to render or upload, so it is much faster); anything
    else gets a smaller JPEG. LLM_PAGE_INPUT = auto | image | text."""
    mode = settings.llm_page_input
    text = page.text
    if text and (mode == "text" or (mode == "auto" and len(text) >= settings.llm_text_only_min_chars)):
        return b"", text
    return page.image_for_llm(), text


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def process_document(document_id: str, db: Session | None = None) -> None:
    """Extracts + maps every page of a document. Runs on the worker thread
    (see app/services/worker.py) and owns its own DB session unless one is
    passed in (tests, or a caller that wants transactional control)."""
    owns_session = db is None
    db = db or SessionLocal()
    fallback = None
    job_started = time.monotonic()      # whole job, including opening/rendering the pages
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            logger.error("process_document: document %s not found", document_id)
            return

        doc.status = DocumentStatus.processing
        doc.progress = []
        doc.pages_done = 0
        doc.processing_seconds = None
        doc.error_message = None      # a retried/recovered job starts clean (no stale "[Recovered...]" note)
        _progress(db, doc, "Picked up from the queue", force=True)

        pages = pdf_utils.load_pages(doc.raw_content, doc.content_type)   # text only; images render lazily
        doc.num_pages = len(pages)
        _progress(db, doc, f"Opened {doc.filename}: {len(pages)} page(s) to read", force=True)

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

        # Fan-out: every page's LLM call is submitted at once and runs
        # concurrently (these are pure network calls - no DB access - so the
        # single main-thread session stays the only writer). The loop below
        # then consumes the results in page order, so everything downstream
        # (mapping, saving, progress, cancellation) behaves as before.
        llm_stop = threading.Event()
        extract_secs: dict[int, float] = {}
        map_page_secs: dict[int, float] = {}
        extract_futures: dict = {}
        pool = None
        map_secs = 0.0
        mapping_llm_ok = True

        def _extract_task(pg):
            if llm_stop.is_set():
                raise _LlmSkipped()
            t0 = time.monotonic()
            try:
                res = llm_client.extract_page(*_llm_inputs(pg))
            finally:
                extract_secs[pg.page_number] = time.monotonic() - t0
            # Map this page's rows here too (in-memory retrieval + at most one
            # batched LLM call), so matching overlaps with the other pages
            # still being read instead of queueing up behind them.
            m0 = time.monotonic()
            try:
                mappings = loinc_mapping.map_rows(res.tests, alias_index, use_llm=not llm_stop.is_set())
            except Exception:
                logger.exception("Page mapping failed for document %s page %s", document_id, pg.page_number)
                mappings = None
            map_page_secs[pg.page_number] = time.monotonic() - m0
            return res, mappings

        def _finish_cancelled():
            llm_stop.set()
            doc.status = DocumentStatus.cancelled
            doc.error_message = f"Cancelled after {pages_completed}/{len(pages)} page(s) by user request."
            _progress(db, doc, "Cancelled at your request", "warn")
            logger.info("Document %s cancelled after %s pages", document_id, pages_completed)

        def _await(future):
            """Waits for one page's AI result in 1-second slices so a cancel is noticed
            promptly and the heartbeat keeps proving this document is still alive."""
            last_beat = time.monotonic()
            while True:
                done, _ = futures_wait([future], timeout=1.0)
                if done:
                    return future.result()
                if _is_cancel_requested(db, document_id):
                    raise _CancelledWhileWaiting()
                if time.monotonic() - last_beat > 10:
                    _touch(db, doc)
                    last_beat = time.monotonic()

        if _is_cancel_requested(db, document_id):       # cancelled while queued/opening
            _finish_cancelled()
            return

        if llm_available and pages:
            pool = ThreadPoolExecutor(
                max_workers=max(1, min(settings.llm_max_concurrency, len(pages))),
                thread_name_prefix="extract",
            )
            extract_futures = {pg.page_number: pool.submit(_extract_task, pg) for pg in pages}
            _progress(db, doc, f"Sending {len(pages)} page(s) to the AI model in parallel")

        for page in pages:
            if _is_cancel_requested(db, document_id):
                llm_stop.set()
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
            prepared = None
            source = "llm"
            llm_error = None
            if llm_available:
                try:
                    future = extract_futures.get(page.page_number)
                    if future is not None:
                        result, prepared = _await(future)
                    else:
                        result = llm_client.extract_page(*_llm_inputs(page))
                except _CancelledWhileWaiting:
                    _finish_cancelled()
                    return
                except _LlmSkipped:
                    llm_available = False
                    llm_skip_reason = llm_skip_reason or "AI extraction was abandoned after an earlier failure"
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
                        llm_stop.set()
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
                took = extract_secs.get(page.page_number)
                _progress(db, doc, f"{label}: the AI found {len(result.tests)} test row(s)"
                          + (f" ({took:.1f}s)" if took is not None else ""))
            _progress(db, doc, f"{label}: matching {len(result.tests)} row(s) to LOINC codes")
            map_started = time.monotonic()
            if prepared is None:
                # Local (fallback) pages, or a page whose threaded mapping failed.
                try:
                    prepared = loinc_mapping.map_rows(
                        result.tests, alias_index, use_llm=(source == "llm" and mapping_llm_ok)
                    )
                except Exception:
                    logger.exception("Mapping failed for document %s page %s", document_id, page.page_number)
                    prepared = [None] * len(result.tests)
            for test, mapping in zip(result.tests, prepared):
                if mapping is None:
                    mapping = {
                        "normalized_test_name": test.original_test_name,
                        "loinc_code": None,
                        "loinc_display": None,
                        "mapping_status": "needs_review",
                        "mapping_confidence": None,
                        "mapping_stage": "error",
                        "mapping_rationale": "Mapping pipeline raised an exception; needs manual review.",
                    }
                if mapping.pop("llm_failed", False):
                    # Only mapping verification is unavailable; pages the AI
                    # already read stay AI-extracted (don't re-read them locally).
                    mapping_llm_ok = False

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
            map_secs += time.monotonic() - map_started
            try:
                learned_mappings.remember(
                    db,
                    [(t.original_test_name, t.specimen, t.unit, m["loinc_code"])
                     for t, m in zip(result.tests, prepared)
                     if m and m.get("mapping_stage") == "lexical_llm" and m.get("mapping_status") == "confirmed"
                     and m.get("loinc_code") and (m.get("mapping_confidence") or 0) >= settings.learned_min_confidence],
                    source="llm", confidence=settings.learned_min_confidence)
            except Exception:
                logger.exception("Could not remember mappings for document %s", document_id)
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

        doc.status = DocumentStatus.failed if any_failure and not doc.observations else DocumentStatus.complete
        if any_failure:
            ai_note = f"AI extraction unavailable: {'; '.join(llm_notes)}. " if llm_notes else ""
            reason_summary = (ai_note + "; ".join(failure_reasons))[:2000]
            doc.error_message = (
                reason_summary if doc.status == DocumentStatus.failed
                else f"One or more pages failed extraction; results may be incomplete. {reason_summary}"
            )
        doc.pages_done = len(pages)
        doc.processing_seconds = round(time.monotonic() - job_started, 1)
        total_rows = len(doc.observations)
        if doc.status == DocumentStatus.failed:
            _progress(db, doc, "Finished without any extracted results", "error")
        else:
            total_secs = doc.processing_seconds
            _progress(
                db, doc,
                f"Finished in {total_secs:.1f}s - {total_rows} observation(s) extracted"
                + (" (some pages could not be read)" if any_failure else "")
                + f" [slowest page {max(extract_secs.values(), default=0):.1f}s, LOINC matching {max(map_page_secs.values(), default=0):.1f}s longest page]",
            )
            logger.info(
                "Document %s timing: total=%.1fs pages=%s ai_max=%.1fs ai_sum=%.1fs mapping=%.1fs",
                document_id, total_secs, len(pages), max(extract_secs.values(), default=0),
                sum(extract_secs.values()), map_secs,
            )
        db.commit()
    except Exception as exc:
        logger.exception("process_document crashed for %s", document_id)
        db.rollback()
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc:
            doc.status = DocumentStatus.failed
            doc.error_message = str(exc)
            doc.processing_seconds = round(time.monotonic() - job_started, 1)
            _progress(db, doc, f"Failed: {_short(str(exc))}", "error")
    finally:
        try:
            db.commit()       # progress lines are committed lazily; make sure the last ones land
        except Exception:
            db.rollback()
        if "llm_stop" in locals():
            llm_stop.set()
        if "pool" in locals() and pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        if fallback is not None:
            fallback.close()
        if owns_session:
            db.close()
