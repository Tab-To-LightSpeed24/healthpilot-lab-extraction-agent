from typing import List, Optional

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.models.document import Document, DocumentStatus
from app.schemas.api import DocumentOut, DocumentDetailOut
from app.services.pipeline import process_document

router = APIRouter(prefix="/reports", tags=["reports"])

ACCEPTED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "text/plain",
}


@router.post("", response_model=DocumentOut, status_code=201)
async def upload_report(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if file.content_type not in ACCEPTED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported content type '{file.content_type}'. Accepted: {sorted(ACCEPTED_CONTENT_TYPES)}",
        )

    raw = await file.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb}MB limit.")
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    doc = Document(
        filename=file.filename or "unnamed",
        content_type=file.content_type,
        raw_content=raw,
        status=DocumentStatus.pending,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    background_tasks.add_task(process_document, doc.id)

    return doc


@router.get("", response_model=List[DocumentOut])
def list_reports(db: Session = Depends(get_db)):
    return db.query(Document).order_by(Document.uploaded_at.desc()).all()


@router.get("/{document_id}", response_model=DocumentDetailOut)
def get_report(document_id: str, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc
