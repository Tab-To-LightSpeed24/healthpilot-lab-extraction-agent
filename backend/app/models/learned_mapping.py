from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Integer, String

from app.core.db import Base


class LearnedMapping(Base):
    """A LOINC pick that was confirmed (a high-confidence AI choice or a human review) for
    a given test name + specimen + unit, so the same combination never needs the AI again."""

    __tablename__ = "learned_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(512), nullable=False, unique=True, index=True)   # "name|specimen|unit", cleaned
    loinc_num = Column(String(32), nullable=False)
    source = Column(String(16), nullable=False)                          # llm | review
    confidence = Column(Float, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
