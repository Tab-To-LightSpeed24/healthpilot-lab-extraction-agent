"""Unit tests for the durable DB-backed job queue (app/services/worker.py),
run single-threaded against the shared test DB session -- no real background
thread involved, since a SQLAlchemy connection isn't safely shared across
threads. The end-to-end wiring (a real thread actually picking up a real
upload) is verified manually against a running dev server; what's tested
here is the queue logic itself: claiming, and recovering a job whose worker
died mid-run."""
import uuid
from datetime import datetime, timedelta, timezone

from app.models.document import Document, DocumentStatus
from app.services.worker import _claim_next_pending, recover_stuck_documents


def _make_document(db_session, **overrides) -> Document:
    defaults = dict(
        id=str(uuid.uuid4()),
        filename="report.pdf",
        content_type="application/pdf",
        raw_content=b"fake",
        status=DocumentStatus.pending,
    )
    defaults.update(overrides)
    doc = Document(**defaults)
    db_session.add(doc)
    db_session.commit()
    return doc


def test_claim_next_pending_picks_oldest_first(db_session):
    older = _make_document(db_session, uploaded_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = _make_document(db_session, uploaded_at=datetime(2026, 1, 2, tzinfo=timezone.utc))

    claimed_id = _claim_next_pending(db_session)

    assert claimed_id == older.id
    db_session.refresh(older)
    assert older.status == DocumentStatus.processing
    db_session.refresh(newer)
    assert newer.status == DocumentStatus.pending


def test_claim_next_pending_returns_none_when_queue_empty(db_session):
    assert _claim_next_pending(db_session) is None


def test_claim_does_not_reclaim_an_already_processing_document(db_session):
    doc = _make_document(db_session, status=DocumentStatus.processing)
    assert _claim_next_pending(db_session) is None
    db_session.refresh(doc)
    assert doc.status == DocumentStatus.processing  # untouched


def test_recover_stuck_documents_requeues_stale_processing_jobs(db_session):
    """Reproduces the exact scenario this exists for: a process crashes
    mid-job, leaving a document in `processing` forever with no automatic
    retry -- the specific limitation flagged before this was built."""
    stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    stuck = _make_document(db_session, status=DocumentStatus.processing, updated_at=stale_cutoff)
    fresh = _make_document(db_session, status=DocumentStatus.processing, updated_at=datetime.now(timezone.utc))

    recovered_count = recover_stuck_documents(db_session)

    assert recovered_count == 1
    db_session.refresh(stuck)
    assert stuck.status == DocumentStatus.pending
    assert "Recovered" in stuck.error_message
    db_session.refresh(fresh)
    assert fresh.status == DocumentStatus.processing  # actively-running job left alone


def test_recover_stuck_documents_is_a_noop_when_nothing_is_stuck(db_session):
    _make_document(db_session, status=DocumentStatus.pending)
    _make_document(db_session, status=DocumentStatus.complete)

    assert recover_stuck_documents(db_session) == 0
