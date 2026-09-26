from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, ConfigDict, field_validator


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


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    content_type: str
    uploaded_at: datetime
    num_pages: int
    status: str
    error_message: Optional[str]


class QualitySummary(BaseModel):
    total_observations: int
    confirmed_count: int
    needs_review_count: int
    unmapped_count: int
    review_needed_ratio: Optional[float]
    possible_duplicate_test_names: List[str]
    low_confidence_extractions: List[dict]


class DocumentDetailOut(DocumentOut):
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
