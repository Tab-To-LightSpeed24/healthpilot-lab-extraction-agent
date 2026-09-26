"""Turns an uploaded file into a list of per-page (text, PNG image bytes) pairs.

Digital PDFs keep their extractable text layer (fed to Gemini alongside the
page image as a cross-check). Scanned PDFs and plain images have no text
layer, so `text` is None and the model relies on the rasterized image alone.
"""
from dataclasses import dataclass
from typing import List, Optional

import pymupdf as fitz  # PyMuPDF (the `fitz` module name is deprecated)

RENDER_DPI = 200


@dataclass
class PageContent:
    page_number: int  # 1-indexed
    text: Optional[str]
    image_png: bytes


IMAGE_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png"}


def load_pages(raw_content: bytes, content_type: str) -> List[PageContent]:
    if content_type in IMAGE_CONTENT_TYPES or content_type.startswith("image/"):
        filetype = "png" if "png" in content_type else "jpg"
        return [_page_from_image(raw_content, filetype)]

    if content_type == "application/pdf" or content_type == "application/octet-stream":
        return _pages_from_pdf(raw_content)

    if content_type.startswith("text/"):
        return [PageContent(page_number=1, text=raw_content.decode("utf-8", errors="ignore"), image_png=b"")]

    raise ValueError(f"Unsupported content type: {content_type}")


def _page_from_image(raw_content: bytes, filetype: str) -> PageContent:
    # Normalize through PyMuPDF so downstream always deals with PNG bytes.
    doc = fitz.open(stream=raw_content, filetype=filetype)
    pix = doc[0].get_pixmap()
    return PageContent(page_number=1, text=None, image_png=pix.tobytes("png"))


def _pages_from_pdf(raw_content: bytes) -> List[PageContent]:
    doc = fitz.open(stream=raw_content, filetype="pdf")
    pages: List[PageContent] = []
    zoom = RENDER_DPI / 72
    matrix = fitz.Matrix(zoom, zoom)
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        pix = page.get_pixmap(matrix=matrix)
        pages.append(
            PageContent(
                page_number=i + 1,
                text=text if text else None,
                image_png=pix.tobytes("png"),
            )
        )
    return pages
