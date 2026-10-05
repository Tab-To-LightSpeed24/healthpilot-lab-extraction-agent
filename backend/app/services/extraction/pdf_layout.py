"""Extracts a PageLayout (word/line-level bounding boxes + font metadata)
straight from a PyMuPDF Page object, for the non-LLM digital-text path.

Deliberately does NOT touch app/services/pdf_utils.py or its PageContent/
load_pages -- those keep serving the existing LLM path exactly as before,
unmodified. This is a new, independent entry point used only by the non-LLM
pipeline, opening the PDF itself rather than threading a shared Page handle
through pdf_utils (the two call sites' needs are different enough --
PageContent wants a flat text string + a rasterized PNG; this wants
per-span bounding boxes and font metadata -- that sharing would couple them
for no real benefit).
"""
from typing import List

import pymupdf as fitz

from app.services.extraction.schemas import BBox, Line, PageLayout, TextSpan

# PyMuPDF text span "flags" bitfield: bit 0 superscript, bit 1 italic,
# bit 2 serifed, bit 3 monospaced, bit 4 bold (value 16).
_BOLD_FLAG_BIT = 1 << 4


def _fractional_bbox(x0: float, y0: float, x1: float, y1: float, page_width: float, page_height: float) -> BBox:
    return BBox(
        x0=x0 / page_width, y0=y0 / page_height,
        x1=x1 / page_width, y1=y1 / page_height,
    )


def extract_page_layout(page: fitz.Page, page_number: int) -> PageLayout:
    page_width = page.rect.width
    page_height = page.rect.height
    raw = page.get_text("dict")

    lines: List[Line] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:  # 0 = text block; skip image blocks (type 1)
            continue
        for line in block.get("lines", []):
            spans: List[TextSpan] = []
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                sx0, sy0, sx1, sy1 = span["bbox"]
                spans.append(TextSpan(
                    text=text,
                    bbox=_fractional_bbox(sx0, sy0, sx1, sy1, page_width, page_height),
                    font_size=span.get("size"),
                    is_bold=bool(span.get("flags", 0) & _BOLD_FLAG_BIT),
                    confidence=1.0,
                    source="digital",
                ))
            if not spans:
                continue
            lx0, ly0, lx1, ly1 = line["bbox"]
            lines.append(Line(
                spans=spans,
                bbox=_fractional_bbox(lx0, ly0, lx1, ly1, page_width, page_height),
            ))

    return PageLayout(page_number=page_number, page_width=page_width, page_height=page_height, lines=lines, source="digital")


def extract_pdf_layouts(raw_pdf_bytes: bytes) -> List[PageLayout]:
    """Entry point for the non-LLM digital path: opens the PDF itself and
    returns one PageLayout per page."""
    doc = fitz.open(stream=raw_pdf_bytes, filetype="pdf")
    try:
        return [extract_page_layout(page, i + 1) for i, page in enumerate(doc)]
    finally:
        doc.close()
