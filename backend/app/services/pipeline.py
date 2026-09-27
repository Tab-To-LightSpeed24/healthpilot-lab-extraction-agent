import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.document import Document, DocumentStatus
from app.models.observation import Observation, MappingStatus
from app.services import pdf_utils, gemini_client, loinc_mapping
from app.services.loinc_loader import get_alias_index
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


def process_document(document_id: str, db: Session | None = None) -> None:
    """Extracts + maps every page of a document. Runs on the worker thread
    (see app/services/worker.py) and owns its own DB session unless one is
    passed in (tests, or a caller that wants transactional control)."""
    owns_session = db is None
    db = db or SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            logger.error("process_document: document %s not found", document_id)
            return

        doc.status = DocumentStatus.processing
        _touch(db, doc)

        pages = pdf_utils.load_pages(doc.raw_content, doc.content_type)
        doc.num_pages = len(pages)
        _touch(db, doc)

        alias_index = get_alias_index()
        any_failure = False
        failure_reasons: list[str] = []
        pages_completed = 0

        for page in pages:
            if _is_cancel_requested(db, document_id):
                doc.status = DocumentStatus.cancelled
                doc.error_message = (
                    f"Cancelled after {pages_completed}/{len(pages)} page(s) by user request."
                )
                db.commit()
                logger.info("Document %s cancelled after %s pages", document_id, pages_completed)
                return

            try:
                result = gemini_client.extract_page(page.image_png, page.text)
            except Exception as exc:
                logger.exception(
                    "Extraction failed for document %s page %s", document_id, page.page_number
                )
                any_failure = True
                failure_reasons.append(f"page {page.page_number}: {exc}")
                pages_completed += 1
                continue

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
                    )
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
                    raw_extraction=test.model_dump(),
                )
                db.add(obs)
            pages_completed += 1
            _touch(db, doc)
            if _is_cancel_requested(db, document_id):
                doc.status = DocumentStatus.cancelled
                doc.error_message = (
                    f"Cancelled after {pages_completed}/{len(pages)} page(s) by user request."
                )
                db.commit()
                logger.info("Document %s cancelled after %s pages", document_id, pages_completed)
                return

        if _is_cancel_requested(db, document_id):
            doc.status = DocumentStatus.cancelled
            doc.error_message = (
                f"Cancelled after {pages_completed}/{len(pages)} page(s) by user request."
            )
            db.commit()
            logger.info("Document %s cancelled after %s pages", document_id, pages_completed)
            return

        doc.status = DocumentStatus.failed if any_failure and not doc.observations else DocumentStatus.complete
        if any_failure:
            reason_summary = "; ".join(failure_reasons)[:2000]
            doc.error_message = (
                reason_summary if doc.status == DocumentStatus.failed
                else f"One or more pages failed extraction; results may be incomplete. {reason_summary}"
            )
        db.commit()
    except Exception as exc:
        logger.exception("process_document crashed for %s", document_id)
        db.rollback()
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc:
            doc.status = DocumentStatus.failed
            doc.error_message = str(exc)
            db.commit()
    finally:
        if owns_session:
            db.close()
