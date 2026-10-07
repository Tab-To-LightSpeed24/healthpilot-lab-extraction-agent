from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ObservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    document_id: str
    page_number: Optional[int]
    original_test_name: str
    normalized_test_name: Optional[str]
    value: Optional[str]
    unit: Optional[str]
    reference_range: Optional[str]
    specimen: Optional[str]
    method: Optional[str]
    timing: Optional[str]
    flag: Optional[str]
    loinc_code: Optional[str]
    loinc_display: Optional[str]
    mapping_status: str
    mapping_confidence: Optional[float]
    mapping_stage: Optional[str]
    mapping_rationale: Optional[str]
    extraction_confidence: Optional[float]
    extraction_source: str = "llm"
    is_edited: bool = False
    validation_notes: Optional[List[str]] = None
    suggested_value: Optional[str] = None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    content_type: str
    uploaded_at: datetime
    num_pages: int
    status: str
    cancel_requested: bool = False
    error_message: Optional[str]
    used_fallback: bool = False
    fallback_reason: Optional[str] = None
    pages_done: int = 0
    current_step: Optional[str] = None


class QualitySummary(BaseModel):
    total_observations: int
    confirmed_count: int
    needs_review_count: int
    unmapped_count: int
    review_needed_ratio: Optional[float]
    possible_duplicate_test_names: List[str]
    low_confidence_extractions: List[dict]


class DocumentDetailOut(DocumentOut):
    progress: List[dict] = []

    @field_validator("progress", mode="before")
    @classmethod
    def _no_progress_yet(cls, v):
        return v or []  # a document that hasn't started processing has no feed
    observations: List[ObservationOut] = []
    quality: Optional[QualitySummary] = None


class ObservationReviewIn(BaseModel):
    """Human-in-the-loop correction of a needs_review/unmapped observation.

    `loinc_code=None` with `mapping_status="unmapped"` is how a reviewer
    records "I looked, and there genuinely is no correct LOINC code" as a
    deliberate, confirmed decision -- distinct from the system never having
    found one. Any other combination requires a real LOINC code (validated
    server-side against the reference table -- this endpoint cannot be used
    to fabricate a code any more than the automated pipeline can).
    """

    loinc_code: Optional[str] = None
    mapping_status: str = "confirmed"

    @field_validator("mapping_status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        allowed = {"confirmed", "needs_review", "unmapped"}
        if v not in allowed:
            raise ValueError(f"mapping_status must be one of {sorted(allowed)}")
        return v


class LoincSearchResult(BaseModel):
    loinc_num: str
    long_common_name: str
    shortname: Optional[str]
    component: Optional[str]
    system: Optional[str]
    example_units: Optional[str]


def _blank_to_none(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


class ObservationCreateIn(BaseModel):
    """A row added by hand (e.g. one the extractor missed). Never triggers an
    LLM call; LOINC mapping is deterministic alias matching only."""

    document_id: str
    original_test_name: str = Field(min_length=1, max_length=512)
    value: Optional[str] = Field(default=None, max_length=512)
    unit: Optional[str] = Field(default=None, max_length=128)
    reference_range: Optional[str] = Field(default=None, max_length=4000)
    specimen: Optional[str] = Field(default=None, max_length=256)
    method: Optional[str] = Field(default=None, max_length=512)
    timing: Optional[str] = Field(default=None, max_length=256)
    flag: Optional[str] = Field(default=None, max_length=64)
    page_number: Optional[int] = Field(default=None, ge=1)

    @field_validator("original_test_name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("original_test_name must not be blank")
        return v

    @field_validator("value", "unit", "reference_range", "specimen", "method", "timing", "flag")
    @classmethod
    def _blank(cls, v):
        return _blank_to_none(v)


class ObservationUpdateIn(BaseModel):
    """Partial edit: only fields present in the request body are changed
    (sending null/blank for an optional field clears it)."""

    original_test_name: Optional[str] = Field(default=None, min_length=1, max_length=512)
    value: Optional[str] = Field(default=None, max_length=512)
    unit: Optional[str] = Field(default=None, max_length=128)
    reference_range: Optional[str] = Field(default=None, max_length=4000)
    specimen: Optional[str] = Field(default=None, max_length=256)
    method: Optional[str] = Field(default=None, max_length=512)
    timing: Optional[str] = Field(default=None, max_length=256)
    flag: Optional[str] = Field(default=None, max_length=64)

    @field_validator("original_test_name")
    @classmethod
    def _name_not_blank(cls, v):
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("original_test_name must not be blank")
        return v

    @field_validator("value", "unit", "reference_range", "specimen", "method", "timing", "flag")
    @classmethod
    def _blank(cls, v):
        return _blank_to_none(v)
