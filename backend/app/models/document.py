import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, Float, String, Integer, DateTime, Enum, JSON, LargeBinary, Text
from sqlalchemy import event
from sqlalchemy.orm import relationship

from app.core.db import Base


class DocumentStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    complete = "complete"
    failed = "failed"
    cancelled = "cancelled"


class Document(Base):
    __tablename__ = "documents"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    filename = Column(String(512), nullable=False)
    content_type = Column(String(128), nullable=False)
    uploaded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    num_pages = Column(Integer, default=0)
    status = Column(Enum(DocumentStatus), default=DocumentStatus.pending, nullable=False)
    error_message = Column(Text, nullable=True)
    raw_content = Column(LargeBinary, nullable=False)

    # Durable-queue bookkeeping: the worker claims a pending row by flipping
    # it to `processing` and stamping `updated_at`; a document whose
    # `updated_at` goes stale while still `processing` (crash mid-job) is
    # detected and requeued on the next worker startup sweep. Cancellation is
    # cooperative: the worker checks `cancel_requested` between pages so a
    # runaway multi-page job (e.g. one that would burn through API quota) can
    # be stopped without killing the process.
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    cancel_requested = Column(Boolean, nullable=False, default=False)

    # True when at least one page was extracted by the local rule-based
    # fallback instead of the LLM; the UI shows a yellow "review this"
    # notice off this flag. `fallback_reason` is why the LLM path was
    # skipped or abandoned (disabled, timeout, credits, ...).
    # Live-progress feed for the UI: [{"t": iso timestamp, "msg": str, "level":
    # "info"|"warn"|"error"}], capped by the pipeline. `pages_done` drives the
    # progress bar.
    progress = Column(JSON, nullable=True)
    pages_done = Column(Integer, nullable=False, default=0, server_default="0")
    # Wall-clock seconds from pick-up to finish (includes opening/rendering the pages);
    # shown under the document name once processing has ended.
    processing_seconds = Column(Float, nullable=True)

    @property
    def current_step(self):
        return (self.progress[-1]["msg"] if self.progress else None)

    used_fallback = Column(Boolean, nullable=False, default=False)
    fallback_reason = Column(Text, nullable=True)

    observations = relationship(
        "Observation", back_populates="document", cascade="all, delete-orphan"
    )


def _clip_filename(mapper, connection, target) -> None:
    if isinstance(target.filename, str) and len(target.filename) > 512:
        target.filename = target.filename[:511] + "\u2026"


event.listen(Document, "before_insert", _clip_filename)
