"""Turns an uploaded file into a list of per-page (text, image) pages.

Digital PDFs keep their extractable text layer. Page IMAGES are rendered lazily, on
first use, instead of all up front: opening a 19-page PDF used to render 10 MB of
200-DPI PNGs before anything else could start (31 s on a 0.1-CPU instance). Now the
text of every page is read immediately (cheap) and each page's image is produced only
when something actually needs it:

* `image_png`      - full-size PNG (200 DPI), used by the local OCR fallback;
* `image_for_llm()` - a smaller JPEG for the AI model (~3-4x less CPU and upload).

Scanned PDFs and plain images have no text layer, so `text` is None and the model
relies on the image alone.
"""
import threading
from typing import List, Optional

import pymupdf as fitz  # PyMuPDF (the `fitz` module name is deprecated)

RENDER_DPI = 200          # full quality: OCR
LLM_IMAGE_DPI = 150       # what the AI model gets (JPEG)
LLM_JPEG_QUALITY = 80
LLM_IMAGE_MAX_SIDE = 2000  # cap for uploaded photos / scans


class _PdfSource:
    """One open PDF shared by all its pages. PyMuPDF documents are not thread-safe,
    so renders on the same document take turns (renders of different documents don't)."""

    def __init__(self, raw: bytes) -> None:
        self._doc = fitz.open(stream=raw, filetype="pdf")
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._doc)

    def text(self, index: int) -> str:
        with self._lock:
            return self._doc[index].get_text("text").strip()

    def render(self, index: int, dpi: int, fmt: str) -> bytes:
        zoom = dpi / 72
        with self._lock:
            pix = self._doc[index].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            return pix.tobytes("jpeg", jpg_quality=LLM_JPEG_QUALITY) if fmt == "jpeg" else pix.tobytes("png")


class _ImageSource:
    """An uploaded PNG/JPEG: native-size PNG for OCR, a size-capped JPEG for the model."""

    def __init__(self, raw: bytes, filetype: str) -> None:
        self._raw, self._filetype = raw, filetype
        self._lock = threading.Lock()

    def render(self, index: int, dpi: int, fmt: str) -> bytes:
        with self._lock:
            doc = fitz.open(stream=self._raw, filetype=self._filetype)
            page = doc[0]
            if fmt == "png":
                return page.get_pixmap().tobytes("png")
            width, height = page.rect.width, page.rect.height
            scale = min(1.0, LLM_IMAGE_MAX_SIDE / max(width, height, 1))
            return page.get_pixmap(matrix=fitz.Matrix(scale, scale)).tobytes("jpeg", jpg_quality=LLM_JPEG_QUALITY)


class PageContent:
    def __init__(self, page_number: int, text: Optional[str], image_png: Optional[bytes] = b"", source=None) -> None:
        self.page_number = page_number      # 1-indexed
        self.text = text
        self._png = image_png
        self._jpeg: Optional[bytes] = None
        self._source = source

    @property
    def image_png(self) -> bytes:
        """Full-quality PNG; rendered on first access."""
        if self._png is None:
            self._png = self._source.render(self.page_number - 1, RENDER_DPI, "png") if self._source else b""
        return self._png

    def image_for_llm(self) -> bytes:
        """The (smaller) image sent to the AI model; empty for pure-text pages."""
        if self._jpeg is None:
            if self._source is not None:
                self._jpeg = self._source.render(self.page_number - 1, LLM_IMAGE_DPI, "jpeg")
            else:
                self._jpeg = self._png or b""
        return self._jpeg

    def __repr__(self) -> str:
        return f"PageContent(page_number={self.page_number}, text={'yes' if self.text else None}, lazy={self._source is not None})"


IMAGE_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png"}
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def load_pages(raw_content: bytes, content_type: str) -> List[PageContent]:
    if content_type in IMAGE_CONTENT_TYPES or content_type.startswith("image/"):
        filetype = "png" if "png" in content_type else "jpg"
        return [_page_from_image(raw_content, filetype)]

    if content_type == "application/pdf" or content_type == "application/octet-stream":
        return _pages_from_pdf(raw_content)

    if content_type == DOCX_CONTENT_TYPE:
        return [_page_from_docx(raw_content)]

    if content_type.startswith("text/"):
        return [PageContent(page_number=1, text=raw_content.decode("utf-8", errors="ignore"), image_png=b"")]

    raise ValueError(f"Unsupported content type: {content_type}")


def _page_from_docx(raw_content: bytes) -> PageContent:
    """A Word document has no fixed pages or page image, so it becomes one
    text "page": paragraphs in order, with each table row flattened to one
    line whose cells are separated by wide gaps (the same shape a padded
    text report has, so the table-row parser handles it unchanged)."""
    import io

    from docx import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        doc = DocxDocument(io.BytesIO(raw_content))
    except Exception as exc:
        raise ValueError(f"could not read the .docx file: {exc}") from exc

    lines = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, doc).text.strip()
            if text:
                lines.append(text)
        elif tag == "tbl":
            for row in Table(child, doc).rows:
                cells, previous = [], None
                for cell in row.cells:
                    text = " ".join(cell.text.split())
                    if text and cell._tc is not previous:  # merged cells repeat the same element
                        cells.append(text)
                    previous = cell._tc
                if cells:
                    lines.append("    ".join(cells))
    return PageContent(page_number=1, text="\n".join(lines) or None, image_png=b"")


def _page_from_image(raw_content: bytes, filetype: str) -> PageContent:
    # Validate now (so a corrupt upload fails at open time), render lazily.
    fitz.open(stream=raw_content, filetype=filetype)[0]
    return PageContent(page_number=1, text=None, image_png=None, source=_ImageSource(raw_content, filetype))


def _pages_from_pdf(raw_content: bytes) -> List[PageContent]:
    source = _PdfSource(raw_content)
    return [
        PageContent(page_number=i + 1, text=source.text(i) or None, image_png=None, source=source)
        for i in range(len(source))
    ]
