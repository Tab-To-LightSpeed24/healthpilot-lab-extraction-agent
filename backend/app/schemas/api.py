from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, ConfigDict


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


class DocumentDetailOut(DocumentOut):
    observations: List[ObservationOut] = []


class LoincSearchResult(BaseModel):
    loinc_num: str
    long_common_name: str
    shortname: Optional[str]
    component: Optional[str]
    system: Optional[str]
    example_units: Optional[str]
