"""A minimal, durable, DB-backed job queue.

Why not Celery/RQ + Redis: at this project's scale (a single instance, low
volume), a message broker is infrastructure to provision and a new failure
mode to reason about, for no durability benefit we don't already get by
using the database itself as the queue -- job state (status, progress,
cancellation) lives in Postgres, which already survives a process restart.
The trade-off being made explicitly: this doesn't scale to multiple worker
processes coordinating over a broker, but nothing about this project's
current deployment does either.

Two pieces:
- `recover_stuck_documents`: run once at startup. A document left in
  `processing` whose heartbeat (`updated_at`) is stale must mean the process
  died mid-job (the crash-recovery gap called out as a known limitation
  before this was built) -- reset it to `pending` so the worker loop retries
  it, instead of it sitting stuck forever.
- `worker_loop`: runs continuously in a background thread (started from
  main.py's lifespan), claiming one pending document at a time and handing
  it to pipeline.process_document. A plain thread rather than an asyncio
  task deliberately, since the actual work (Gemini calls, sync DB queries)
  is blocking -- doing that on the asyncio event loop would freeze request
  handling.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.document import Document, DocumentStatus
from app.services.pipeline import process_document

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0
STUCK_AFTER = timedelta(minutes=10)


def recover_stuck_documents(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - STUCK_AFTER
    stuck = (
        db.query(Document)
        .filter(Document.status == DocumentStatus.processing, Document.updated_at < cutoff)
        .all()
    )
    for doc in stuck:
        logger.warning("Recovering stuck document %s (last heartbeat %s)", doc.id, doc.updated_at)
        doc.status = DocumentStatus.pending
        doc.error_message = (doc.error_message or "") + " [Recovered after an interrupted run; retrying.]"
        doc.updated_at = datetime.now(timezone.utc)
    if stuck:
        db.commit()
    return len(stuck)


def _claim_next_pending(db: Session) -> str | None:
    """Compare-and-swap claim: only succeeds if the row is still `pending`
    at the moment of the UPDATE, so two workers racing on the same row can't
    both start processing it."""
    candidate = (
        db.query(Document.id)
        .filter(Document.status == DocumentStatus.pending)
        .order_by(Document.uploaded_at.asc())
        .first()
    )
    if candidate is None:
        return None
    document_id = candidate[0]

    result = (
        db.query(Document)
        .filter(Document.id == document_id, Document.status == DocumentStatus.pending)
        .update(
            {"status": DocumentStatus.processing, "updated_at": datetime.now(timezone.utc)},
            synchronize_session=False,
        )
    )
    db.commit()
    return document_id if result == 1 else None


def worker_loop(stop_event: threading.Event) -> None:
    logger.info("Worker loop started")
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            document_id = _claim_next_pending(db)
        finally:
            db.close()

        if document_id is None:
            stop_event.wait(POLL_INTERVAL_SECONDS)
            continue

        logger.info("Worker claimed document %s", document_id)
        try:
            process_document(document_id)
        except Exception:
            logger.exception("Unhandled error processing document %s", document_id)


def start_worker() -> threading.Event:
    """Runs the startup recovery sweep, then starts the worker loop on a
    daemon thread. Returns the stop_event so callers (tests, graceful
    shutdown) can stop the loop."""
    db = SessionLocal()
    try:
        recovered = recover_stuck_documents(db)
        if recovered:
            logger.info("Recovered %s stuck document(s) at startup", recovered)
    finally:
        db.close()

    stop_event = threading.Event()
    thread = threading.Thread(target=worker_loop, args=(stop_event,), daemon=True)
    thread.start()
    return stop_event
