import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, DateTime, Enum, LargeBinary, Text
from sqlalchemy.orm import relationship

from app.core.db import Base


class DocumentStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    complete = "complete"
    failed = "failed"


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

    observations = relationship(
        "Observation", back_populates="document", cascade="all, delete-orphan"
    )
