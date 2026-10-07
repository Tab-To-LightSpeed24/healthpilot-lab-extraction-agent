from datetime import datetime, timezone
from typing import List, Tuple
from urllib.parse import quote

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session, defer

from app.core.config import settings
from app.core.db import get_db
from app.core.security import rate_limit_uploads
from app.models.document import Document, DocumentStatus
from app.models.observation import Observation
from app.schemas.api import DocumentOut, DocumentDetailOut, QualitySummary
from app.services.fhir_export import build_fhir_bundle
from app.services.pdf_utils import DOCX_CONTENT_TYPE
from app.services.quality import compute_quality_flags

router = APIRouter(prefix="/reports", tags=["reports"])

ACCEPTED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "text/plain",
    DOCX_CONTENT_TYPE,
}

# Browsers/clients often label an upload application/octet-stream (or nothing);
# the extension is then the only reliable signal.
_EXTENSION_TYPES = {
    ".pdf": "application/pdf",
    ".docx": DOCX_CONTENT_TYPE,
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
}


def _resolve_content_type(file: UploadFile) -> str:
    declared = (file.content_type or "").lower()
    if declared in ("", "application/octet-stream"):
        name = (file.filename or "").lower()
        for ext, ctype in _EXTENSION_TYPES.items():
            if name.endswith(ext):
                return ctype
    return file.content_type


async def _read_and_validate(file: UploadFile) -> Tuple[bytes, str]:
    content_type = _resolve_content_type(file)
    if content_type not in ACCEPTED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported content type '{content_type}'. Accepted: {sorted(ACCEPTED_CONTENT_TYPES)}",
        )
    raw = await file.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb}MB limit.")
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    return raw, content_type


@router.post("", response_model=DocumentOut, status_code=201, dependencies=[Depends(rate_limit_uploads)])
async def upload_report(file: UploadFile = File(...), db: Session = Depends(get_db)):
    raw, content_type = await _read_and_validate(file)

    # Just enqueue: status=pending is picked up by the background worker
    # (app/services/worker.py), not fired directly from the request. This is
    # what makes the job durable -- the document row IS the queue entry, so
    # a crash between "queued" and "processed" just leaves it pending for
    # the worker to pick up on the next poll (or after a restart), instead
    # of the job vanishing with whatever process happened to be handling it.
    doc = Document(
        filename=file.filename or "unnamed",
        content_type=content_type,
        raw_content=raw,
        status=DocumentStatus.pending,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@router.post("/batch", response_model=List[DocumentOut], status_code=201, dependencies=[Depends(rate_limit_uploads)])
async def upload_reports_batch(files: List[UploadFile] = File(...), db: Session = Depends(get_db)):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    # Validate every file BEFORE persisting any of them, so a batch either
    # queues entirely or fails entirely -- no partial batch where the first
    # three files are already queued by the time file #4 turns out invalid.
    validated = [(f, await _read_and_validate(f)) for f in files]

    docs = [
        Document(filename=f.filename or "unnamed", content_type=ctype, raw_content=raw, status=DocumentStatus.pending)
        for f, (raw, ctype) in validated
    ]
    db.add_all(docs)
    db.commit()
    for doc in docs:
        db.refresh(doc)
    return docs


@router.get("", response_model=List[DocumentOut])
def list_reports(db: Session = Depends(get_db)):
    # The original upload (often megabytes) is never needed for the list, and
    # the UI polls this endpoint; deferring it keeps each poll cheap.
    return (
        db.query(Document)
        .options(defer(Document.raw_content))
        .order_by(Document.uploaded_at.desc())
        .all()
    )


@router.get("/{document_id}", response_model=DocumentDetailOut)
def get_report(document_id: str, db: Session = Depends(get_db)):
    doc = (
        db.query(Document)
        .options(defer(Document.raw_content))
        .filter(Document.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    detail = DocumentDetailOut.model_validate(doc)
    detail.quality = QualitySummary(**compute_quality_flags(doc.observations))
    return detail


@router.get("/{document_id}/file")
def get_report_file(document_id: str, db: Session = Depends(get_db)):
    """The original upload, served inline so the UI's document viewer can show
    it next to the extracted results."""
    row = (
        db.query(Document.filename, Document.content_type, Document.raw_content)
        .filter(Document.id == document_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return Response(
        content=row.raw_content,
        media_type=row.content_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(row.filename)}",
            "Cache-Control": "private, max-age=3600",
        },
    )


STALE_HEARTBEAT_SECONDS = 60


def _heartbeat_age_seconds(doc: Document) -> float:
    beat = doc.updated_at or doc.uploaded_at
    if beat is None:
        return 0.0
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - beat).total_seconds()


@router.post("/{document_id}/cancel", response_model=DocumentOut)
def cancel_report(document_id: str, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    if doc.status in (DocumentStatus.complete, DocumentStatus.failed, DocumentStatus.cancelled):
        raise HTTPException(status_code=409, detail=f"Cannot cancel a document that is already {doc.status.value}.")

    if doc.status == DocumentStatus.pending:
        # Never claimed by the worker yet -- cancel immediately, no need to
        # wait for a cooperative check that will never run.
        doc.status = DocumentStatus.cancelled
        doc.cancel_requested = True
        doc.error_message = "Cancelled by user request before processing started."
    elif _heartbeat_age_seconds(doc) > STALE_HEARTBEAT_SECONDS:
        # Marked "processing" but nothing has touched it for a while: the worker that
        # owned it died (crash / out-of-memory / redeploy). No one is left to notice a
        # cooperative flag, so finish the cancellation here instead of leaving it
        # stuck on "Cancelling" forever.
        doc.status = DocumentStatus.cancelled
        doc.cancel_requested = True
        doc.error_message = "Cancelled by user request (the processing worker was no longer running)."
    else:
        # Already claimed and running: the worker checks this flag every second while
        # waiting on the AI and between pages (see pipeline.process_document), so it
        # stops promptly instead of burning through the rest of the document's calls.
        doc.cancel_requested = True

    db.commit()
    db.refresh(doc)
    return doc


@router.get("/{document_id}/fhir")
def get_report_fhir(document_id: str, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    observations = (
        db.query(Observation)
        .filter(Observation.document_id == document_id)
        .order_by(Observation.page_number, Observation.created_at)
        .all()
    )
    bundle = build_fhir_bundle(doc, observations)
    return JSONResponse(content=bundle, media_type="application/fhir+json")
