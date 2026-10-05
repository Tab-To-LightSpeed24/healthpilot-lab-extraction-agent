"""Phase 1 top-level entry point: a complete, non-LLM extraction path for
digital (text-layer-present) PDF pages. Not yet wired into
app/services/pipeline.py (that integration, plus the scanned/OCR path and
retry/escalation orchestration, lands in later phases) -- this is the
standalone function exercised by tests/extraction/test_pipeline_digital_e2e.py
and usable directly for A/B comparison against the existing LLM path.
"""
from typing import List

from app.schemas.extraction import PageExtractionResult
from app.services.extraction.classify import classify_page
from app.services.extraction.field_parser import parse_page
from app.services.extraction.layout import reconstruct_reading_order
from app.services.extraction.pdf_layout import extract_pdf_layouts


def extract_digital_pdf(raw_pdf_bytes: bytes) -> List[PageExtractionResult]:
    """Returns one PageExtractionResult per page. A page classified
    "scanned" (no usable digital text layer) yields an empty result with a
    note -- there is no fallback extraction for it yet in this phase."""
    results: List[PageExtractionResult] = []
    for layout in extract_pdf_layouts(raw_pdf_bytes):
        classification = classify_page(layout)
        if classification.path != "digital":
            results.append(PageExtractionResult(tests=[], page_notes=classification.reason))
            continue
        ordered = reconstruct_reading_order(layout)
        results.append(parse_page(ordered))
    return results
