"""Tesseract OCR -> the shared PageLayout schema.

Words from Tesseract are grouped into lines (its own line segmentation), and
within a line consecutive words are merged into one TextSpan "phrase" unless
the horizontal gap between them is wide. That mirrors what the digital path
produces for a table row ("Platelet Count" | "250" | "10*3/uL" | "150-400"
as separate fragments), which is what lets field_parser.py work unchanged on
OCR output.

Per-span confidence is the mean Tesseract word confidence / 100 (digital
text is 1.0), so downstream stages can see which fragments were shaky.
"""
import os
import shutil
from typing import List, Optional

import numpy as np

from app.core.config import settings
from app.services.extraction.schemas import BBox, Line, PageLayout, TextSpan

# Words below this Tesseract confidence are dropped as noise ("|" from table
# rules, speckle read as punctuation) rather than fed to the parser.
MIN_WORD_CONFIDENCE = 15.0
# A gap wider than this fraction of the line's typical word height starts a
# new span. A normal inter-word space is ~0.25-0.4 of that height; padded
# table columns are several times it.
SPAN_GAP_HEIGHT_RATIO = 0.8

_WINDOWS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
)


class OcrUnavailable(RuntimeError):
    pass


def find_tesseract() -> Optional[str]:
    if settings.tesseract_cmd and os.path.isfile(settings.tesseract_cmd):
        return settings.tesseract_cmd
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path
    return next((p for p in _WINDOWS_PATHS if os.path.isfile(p)), None)


def configure_tesseract() -> str:
    if not settings.ocr_enabled:
        raise OcrUnavailable("OCR is disabled (OCR_ENABLED=false)")
    import pytesseract

    cmd = find_tesseract()
    if cmd is None:
        raise OcrUnavailable(
            "Tesseract OCR engine is not installed (install it, or set TESSERACT_CMD)"
        )
    pytesseract.pytesseract.tesseract_cmd = cmd
    return cmd


def ocr_available() -> bool:
    try:
        configure_tesseract()
        return True
    except OcrUnavailable:
        return False


def ocr_layout(image: np.ndarray, page_number: int, psm: int = 6) -> PageLayout:
    """Runs Tesseract on a grayscale/binary image and returns a PageLayout
    (fractional bboxes, source="ocr")."""
    import pytesseract

    configure_tesseract()
    height, width = image.shape[:2]
    try:
        data = pytesseract.image_to_data(
            image,
            config=f"--oem 1 --psm {psm}",
            output_type=pytesseract.Output.DICT,
            timeout=settings.ocr_page_timeout_seconds,
        )
    except RuntimeError as exc:  # pytesseract raises RuntimeError on timeout
        raise OcrUnavailable(f"OCR timed out or failed: {exc}") from exc

    by_line: dict = {}
    for i, raw in enumerate(data["text"]):
        text = (raw or "").strip()
        conf = float(data["conf"][i])
        if not text or conf < MIN_WORD_CONFIDENCE:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        by_line.setdefault(key, []).append(
            (data["left"][i], data["top"][i], data["width"][i], data["height"][i], text, conf)
        )

    lines: List[Line] = []
    for words in by_line.values():
        words.sort(key=lambda w: w[0])
        typical_h = float(np.median([w[3] for w in words]))
        groups: List[list] = [[words[0]]]
        for prev, cur in zip(words, words[1:]):
            gap = cur[0] - (prev[0] + prev[2])
            if gap > SPAN_GAP_HEIGHT_RATIO * typical_h:
                groups.append([cur])
            else:
                groups[-1].append(cur)

        spans: List[TextSpan] = []
        for g in groups:
            x0 = min(w[0] for w in g)
            y0 = min(w[1] for w in g)
            x1 = max(w[0] + w[2] for w in g)
            y1 = max(w[1] + w[3] for w in g)
            spans.append(TextSpan(
                text=" ".join(w[4] for w in g),
                bbox=BBox(x0=x0 / width, y0=y0 / height, x1=x1 / width, y1=y1 / height),
                confidence=float(np.mean([w[5] for w in g])) / 100.0,
                source="ocr",
            ))
        lines.append(Line(
            spans=spans,
            bbox=BBox(
                x0=min(s.bbox.x0 for s in spans), y0=min(s.bbox.y0 for s in spans),
                x1=max(s.bbox.x1 for s in spans), y1=max(s.bbox.y1 for s in spans),
            ),
            column_index=0,
        ))

    lines.sort(key=lambda l: ((l.bbox.y0 + l.bbox.y1) / 2, l.bbox.x0))
    for i, line in enumerate(lines):
        line.reading_order = i

    return PageLayout(
        page_number=page_number, page_width=float(width), page_height=float(height),
        lines=lines, source="ocr",
    )
