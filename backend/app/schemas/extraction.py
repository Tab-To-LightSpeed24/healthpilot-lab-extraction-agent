"""Pydantic schemas describing the JSON we ask the extraction model to return
for each page.

Kept intentionally flat and permissive (most fields nullable) because lab
report layouts vary enormously and the model must be free to say "not
present" rather than invent a value.
"""
from typing import Optional, List

from pydantic import BaseModel, Field


class ExtractedTest(BaseModel):
    original_test_name: str = Field(
        description="The test name exactly as printed on the report."
    )
    value: Optional[str] = Field(
        default=None, description="The reported result/value, as printed (keep as string to preserve formatting like '<0.1' or 'Positive')."
    )
    unit: Optional[str] = Field(default=None, description="Unit of measure, if printed.")
    reference_range: Optional[str] = Field(
        default=None, description="Reference/normal range as printed, if present."
    )
    specimen: Optional[str] = Field(
        default=None, description="Specimen type if stated (e.g. Serum, Whole Blood, Urine)."
    )
    method: Optional[str] = Field(
        default=None, description="Analytical method if stated (e.g. Immunoassay)."
    )
    timing: Optional[str] = Field(
        default=None, description="Timing context if stated (e.g. Fasting, Random, 24-hour)."
    )
    flag: Optional[str] = Field(
        default=None, description="Abnormal flag as printed (e.g. H, L, Critical), if any."
    )
    extraction_confidence: float = Field(
        ge=0.0, le=1.0,
        description="Model's own confidence that this row was read correctly from the page (0-1).",
    )
    # Filled by the local validation layer (app/services/extraction/validation.py),
    # never by the LLM.
    validation_notes: List[str] = Field(default_factory=list)
    suggested_value: Optional[str] = None


class PageExtractionResult(BaseModel):
    tests: List[ExtractedTest] = Field(default_factory=list)
    # "digital" | "text" | "ocr" for locally extracted pages; None for LLM output.
    method: Optional[str] = None
    page_notes: Optional[str] = Field(
        default=None,
        description="Anything unusual about this page worth flagging (poor scan quality, cut-off text, etc.)",
    )
