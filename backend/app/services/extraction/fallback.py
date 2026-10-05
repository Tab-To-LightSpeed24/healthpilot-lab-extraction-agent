"""LLM-free extraction used whenever the LLM path is disabled, unavailable,
over its time budget, or erroring. Wraps the local pipeline stages
(layout reconstruction -> rule-based field parsing) behind one per-document
object so the main pipeline doesn't need to know which content types the
local path can and can't handle.

Supported: digital (text-layer) PDF pages, plain text, and -- via local
Tesseract OCR (see scanned.py) -- scanned PDF pages and image uploads. When a
page can't be read (no OCR engine installed, OCR timed out, nothing
recognizable), `extract` raises FallbackUnavailable with a message the
pipeline surfaces to the user as the reason the page produced no output,
rather than silently returning an empty (and misleadingly "successful")
result.
"""
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Dict, Iterable, Optional

from app.core.config import settings

from app.schemas.extraction import PageExtractionResult
from app.services.extraction.classify import classify_page
from app.services.extraction.field_parser import parse_page
from app.services.extraction.layout import reconstruct_reading_order
from app.services.extraction.ocr import OcrUnavailable
from app.services.extraction.pdf_layout import extract_pdf_layouts
from app.services.extraction.scanned import extract_scanned_page
from app.services.extraction.schemas import BBox, Line, PageLayout, TextSpan
from app.services.pdf_utils import DOCX_CONTENT_TYPE, PageContent


logger = logging.getLogger(__name__)


class FallbackUnavailable(RuntimeError):
    pass


def _layout_from_text(page_number: int, text: str) -> PageLayout:
    """Plain text has no geometry; give each non-empty line its own
    non-overlapping horizontal slice so the shared layout types still hold."""
    raw_lines = [ln for ln in text.splitlines() if ln.strip()]
    n = max(len(raw_lines), 1)
    lines = []
    for i, ln in enumerate(raw_lines):
        bbox = BBox(x0=0.0, y0=i / n, x1=1.0, y1=(i + 0.9) / n)
        lines.append(Line(spans=[TextSpan(text=ln, bbox=bbox)], bbox=bbox, reading_order=i))
    return PageLayout(page_number=page_number, page_width=1.0, page_height=1.0, lines=lines)


class FallbackExtractor:
    def __init__(self, raw_content: bytes, content_type: str):
        self._raw = raw_content
        self._content_type = content_type
        self._pdf_layouts: Optional[Dict[int, PageLayout]] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._futures: Dict[int, Future] = {}

    def _pdf_layout(self, page_number: int) -> PageLayout:
        if self._pdf_layouts is None:
            self._pdf_layouts = {l.page_number: l for l in extract_pdf_layouts(self._raw)}
        layout = self._pdf_layouts.get(page_number)
        if layout is None:
            raise FallbackUnavailable(f"page {page_number} not found in PDF")
        return layout

    @staticmethod
    def _ocr(page: PageContent) -> PageExtractionResult:
        if not page.image_png:
            raise FallbackUnavailable("no page image available for OCR")
        try:
            return extract_scanned_page(page.image_png, page.page_number)
        except OcrUnavailable as exc:
            raise FallbackUnavailable(str(exc)) from exc
        except ValueError as exc:
            raise FallbackUnavailable(f"OCR could not read the image: {exc}") from exc

    def _needs_ocr(self, page: PageContent) -> bool:
        if self._content_type.startswith("image/"):
            return True
        if self._content_type in ("application/pdf", "application/octet-stream"):
            try:
                return classify_page(self._pdf_layout(page.page_number)).path != "digital"
            except Exception:
                return False
        return False

    def prefetch(self, pages: Iterable[PageContent]) -> None:
        """Starts OCR for pages that need it in a small background pool, so a
        slow page overlaps with the caller mapping/saving earlier pages.
        Digital/text pages are cheap and stay synchronous. The pool is
        deliberately small (settings.ocr_max_workers, default 1): the work is
        CPU- and memory-heavy, not I/O-bound, so more threads would add
        memory pressure, not speed, on the target instance. DB writes stay on
        the caller's thread/session."""
        if settings.ocr_max_workers < 1:
            return
        for page in pages:
            if page.page_number in self._futures or not self._needs_ocr(page):
                continue
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=settings.ocr_max_workers, thread_name_prefix="ocr")
            self._futures[page.page_number] = self._pool.submit(self._timed_ocr, page)

    def close(self) -> None:
        for fut in self._futures.values():
            fut.cancel()
        self._futures.clear()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    @classmethod
    def _timed_ocr(cls, page: PageContent) -> PageExtractionResult:
        started = time.monotonic()
        try:
            return cls._ocr(page)
        finally:
            logger.info("OCR page %s took %.1fs", page.page_number, time.monotonic() - started)

    def _is_text(self) -> bool:
        return self._content_type.startswith("text/") or self._content_type == DOCX_CONTENT_TYPE

    def describe(self, page: PageContent) -> str:
        """One human-readable line for the live progress feed."""
        if self._is_text():
            return "reading the text locally"
        if self._needs_ocr(page):
            return "running OCR on the scanned page (this can take a few seconds)"
        return "reading the PDF text layer and layout locally"

    def extract(self, page: PageContent) -> PageExtractionResult:
        future = self._futures.pop(page.page_number, None)
        if future is not None:
            return future.result()
        if self._is_text():
            layout = _layout_from_text(page.page_number, page.text or "")
            return parse_page(layout).model_copy(update={"method": "text"})

        if self._content_type in ("application/pdf", "application/octet-stream"):
            layout = self._pdf_layout(page.page_number)
            classification = classify_page(layout)
            if classification.path != "digital":
                return self._ocr(page)  # scanned PDF page: no text layer, read the image
            return parse_page(reconstruct_reading_order(layout)).model_copy(update={"method": "digital"})

        if self._content_type.startswith("image/"):
            return self._ocr(page)

        raise FallbackUnavailable(f"local fallback cannot read '{self._content_type}'")
