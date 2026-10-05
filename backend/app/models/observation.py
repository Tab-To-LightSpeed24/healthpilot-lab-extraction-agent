import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    DateTime,
    Enum,
    Text,
    ForeignKey,
    JSON,
    Boolean,
)
import sqlalchemy as sa
from sqlalchemy.orm import relationship

from app.core.db import Base


class MappingStatus(str, enum.Enum):
    confirmed = "confirmed"
    needs_review = "needs_review"
    unmapped = "unmapped"


class Observation(Base):
    __tablename__ = "observations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id = Column(String(36), ForeignKey("documents.id"), nullable=False)
    page_number = Column(Integer, nullable=True)

    original_test_name = Column(String(512), nullable=False)
    normalized_test_name = Column(String(512), nullable=True)
    value = Column(String(128), nullable=True)
    unit = Column(String(64), nullable=True)
    reference_range = Column(String(128), nullable=True)
    specimen = Column(String(128), nullable=True)
    method = Column(String(256), nullable=True)
    timing = Column(String(128), nullable=True)
    flag = Column(String(32), nullable=True)  # e.g. H / L / Critical, as reported

    loinc_code = Column(String(32), nullable=True)
    loinc_display = Column(String(512), nullable=True)
    mapping_status = Column(Enum(MappingStatus), default=MappingStatus.unmapped, nullable=False)
    mapping_confidence = Column(Float, nullable=True)
    mapping_stage = Column(String(32), nullable=True)  # alias_exact | embedding_llm | none
    mapping_rationale = Column(Text, nullable=True)

    # Provenance: "llm" (model-extracted), "fallback" (local rule-based
    # parser, lower accuracy), or "manual" (row added by a user). `is_edited`
    # flips to True once a user corrects any field of an extracted row.
    extraction_source = Column(String(16), nullable=False, default="llm", server_default="llm")
    is_edited = Column(Boolean, nullable=False, default=False, server_default=sa.false())

    # Local validation findings (e.g. "possible lost decimal point") and, for
    # that case, the value it suggests. The stored `value` is never changed
    # automatically; a user applies the suggestion explicitly.
    validation_notes = Column(JSON, nullable=True)
    suggested_value = Column(String(128), nullable=True)

    extraction_confidence = Column(Float, nullable=True)
    raw_extraction = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    document = relationship("Document", back_populates="observations")
