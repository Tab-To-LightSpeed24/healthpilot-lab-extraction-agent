import logging

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.document import Document, DocumentStatus
from app.models.observation import Observation, MappingStatus
from app.services import pdf_utils, gemini_client, loinc_mapping
from app.services.loinc_loader import get_alias_index

logger = logging.getLogger(__name__)


def process_document(document_id: str) -> None:
    """Runs synchronously in a background task/thread. Owns its own DB session
    since it may outlive the request that triggered it."""
    db: Session = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            logger.error("process_document: document %s not found", document_id)
            return

        doc.status = DocumentStatus.processing
        db.commit()

        pages = pdf_utils.load_pages(doc.raw_content, doc.content_type)
        doc.num_pages = len(pages)
        db.commit()

        alias_index = get_alias_index()
        any_failure = False
        failure_reasons: list[str] = []

        for page in pages:
            try:
                result = gemini_client.extract_page(page.image_png, page.text)
            except Exception as exc:
                logger.exception(
                    "Extraction failed for document %s page %s", document_id, page.page_number
                )
                any_failure = True
                failure_reasons.append(f"page {page.page_number}: {exc}")
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
                    unit=test.unit,
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
            db.commit()

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
        db.close()
