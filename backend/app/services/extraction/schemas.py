"""Unified intermediate schema shared by every source of page text (PyMuPDF's
native digital-text layer today; Tesseract OCR in a later phase) so
everything downstream of layout reconstruction -- field_parser.py, and any
future stage -- works identically regardless of which path produced its
input.

Coordinates are fractional ([0,1] of page_width/page_height), not absolute
points or pixels. PyMuPDF reports positions in PDF-point space; an OCR
engine reports positions in pixel space at whatever DPI the page was
rasterized at. Normalizing both into the same fractional space up front
means layout.py's column/row-grouping logic needs no unit-conversion
branching for whichever source produced it.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel


class BBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class TextSpan(BaseModel):
    text: str
    bbox: BBox
    font_size: Optional[float] = None
    is_bold: bool = False
    confidence: float = 1.0  # 1.0 for digital text; OCR word confidence / 100 later
    source: Literal["digital", "ocr"] = "digital"


class Line(BaseModel):
    """One visually-grouped run of text. Before layout reconstruction, this
    mirrors PyMuPDF's own line segmentation one-to-one. After
    `layout.reconstruct_reading_order()`, each Line instead represents one
    fully-merged logical row (label + value + range + flag, etc., which are
    frequently separate PyMuPDF lines in complex layouts -- see layout.py's
    docstring), with `spans` concatenated in left-to-right order."""
    spans: List[TextSpan]
    bbox: BBox
    column_index: Optional[int] = None
    reading_order: Optional[int] = None

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.spans).strip()

    @property
    def max_font_size(self) -> float:
        sizes = [s.font_size for s in self.spans if s.font_size is not None]
        return max(sizes) if sizes else 0.0


class PageLayout(BaseModel):
    page_number: int
    page_width: float
    page_height: float
    lines: List[Line]
    source: Literal["digital", "ocr"] = "digital"


class PageClassification(BaseModel):
    page_number: int
    path: Literal["digital", "scanned"]
    reason: str
