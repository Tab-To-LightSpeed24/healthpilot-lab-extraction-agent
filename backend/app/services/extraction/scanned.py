"""Scanned/photographed page -> PageExtractionResult, with no LLM:
preprocess -> OCR -> rule-based parse, with bounded retries.

Why more than one attempt: measured on the generated fixtures, Tesseract is
sensitive to OCR scale in ways that don't track "bigger is better" -- a
1.995x vs 2.0x resize swung one complex page from 5/12 to 9/12 correct
values. So the first attempt (2x, psm 6, the best single setting measured)
is accepted immediately only if it looks healthy; otherwise alternate scales
are tried and the candidate with the best internal quality score wins. The
score uses no ground truth: more rows, rows that carry a unit and/or range
(structural completeness), weighted by mean OCR word confidence. On the
fixtures that picked 80% of values vs 79% for always-2x (oracle: 86%) --
a modest gain overall, concentrated on the hard complex-layout pages.

Bounds (time/cost/accuracy): at most MAX_RUNS Tesseract runs per page, each
capped by settings.ocr_page_timeout_seconds, and no new attempt starts once
the page's total OCR budget is spent. A binarized (adaptive-threshold)
variant is built by preprocessing but deliberately NOT used as a retry: on
speckled scans it made Tesseract run past the timeout in measurement.
"""
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

import cv2
import numpy as np

from app.core.config import settings
from app.schemas.extraction import PageExtractionResult
from app.services.extraction.field_parser import parse_page
from app.services.extraction.ocr import OcrUnavailable, ocr_layout
from app.services.extraction.preprocessing import preprocess
from app.services.extraction.schemas import PageLayout

logger = logging.getLogger(__name__)

# (scale factor, psm). First entry is the default; the rest are alternates.
ATTEMPTS = ((2.0, 6), (1.0, 6), (1.5, 6))
LAST_RESORT = (2.0, 4)  # only if no attempt recognised a single row
MAX_RUNS = 4
MAX_OCR_WIDTH_PX = 3600  # never feed Tesseract wider than this (speed)
# Tesseract's own footprint scales with pixel count. Measured in a 512MB
# container: a ~16MP page pushed the whole container to 496MB (16MB of
# headroom). Capping the OCR image keeps one page well inside the limit.
MAX_OCR_PIXELS = 8_000_000

HEALTHY_MEAN_CONFIDENCE = 0.80
HEALTHY_COMPLETE_ROW_FRACTION = 0.80


@dataclass
class _Candidate:
    scale: float
    psm: int
    layout: PageLayout
    result: PageExtractionResult
    mean_conf: float

    @property
    def complete_fraction(self) -> float:
        tests = self.result.tests
        return sum(bool(t.unit or t.reference_range) for t in tests) / len(tests) if tests else 0.0

    @property
    def score(self) -> float:
        return sum(
            0.5 + 0.25 * bool(t.unit) + 0.25 * bool(t.reference_range) for t in self.result.tests
        ) * self.mean_conf

    @property
    def healthy(self) -> bool:
        return (
            bool(self.result.tests)
            and self.mean_conf >= HEALTHY_MEAN_CONFIDENCE
            and self.complete_fraction >= HEALTHY_COMPLETE_ROW_FRACTION
        )


def _release_memory() -> None:
    """glibc keeps freed image buffers in its arenas, so resident memory stays
    high after each OCR run (measured: ~280-325MB oscillating in a container).
    malloc_trim hands them back. No-op where unavailable (Windows/macOS)."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _resized(gray: np.ndarray, scale: float) -> np.ndarray:
    h, w = gray.shape[:2]
    scale = min(scale, MAX_OCR_WIDTH_PX / w, (MAX_OCR_PIXELS / (w * h)) ** 0.5)
    if abs(scale - 1.0) < 0.02:
        return gray
    interp = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=interp)


def _run(gray: np.ndarray, page_number: int, scale: float, psm: int) -> _Candidate:
    resized = _resized(gray, scale)
    try:
        layout = ocr_layout(resized, page_number, psm=psm)
    finally:
        del resized
        _release_memory()
    spans = [s for line in layout.lines for s in line.spans]
    mean_conf = float(np.mean([s.confidence for s in spans])) if spans else 0.0
    return _Candidate(scale, psm, layout, parse_page(layout), mean_conf)


def extract_scanned_page(image_bytes: bytes, page_number: int) -> PageExtractionResult:
    """Raises OcrUnavailable if the engine is missing/disabled or no attempt
    recognised any test row; ValueError if the bytes aren't a decodable image."""
    prepared = preprocess(image_bytes)
    started = time.monotonic()
    budget = settings.ocr_page_timeout_seconds * 1.5

    candidates: List[_Candidate] = []
    last_error: Optional[Exception] = None
    plan = list(ATTEMPTS)
    runs = 0
    while plan and runs < MAX_RUNS:
        if runs and time.monotonic() - started > budget:
            logger.warning("OCR budget spent on page %s after %s run(s)", page_number, runs)
            break
        scale, psm = plan.pop(0)
        runs += 1
        try:
            cand = _run(prepared.gray, page_number, scale, psm)
        except OcrUnavailable as exc:
            last_error = exc
            logger.warning("OCR run (scale %s, psm %s) failed on page %s: %s", scale, psm, page_number, exc)
            if "not installed" in str(exc) or "disabled" in str(exc):
                raise
            continue
        candidates.append(cand)
        if cand.healthy:
            break
        if not plan and not any(c.result.tests for c in candidates) and LAST_RESORT:
            plan.append(LAST_RESORT)

    usable = [c for c in candidates if c.result.tests]
    if not usable:
        raise last_error or OcrUnavailable("OCR ran but no test rows were recognized")

    _release_memory()
    best = max(usable, key=lambda c: c.score)
    for t in best.result.tests:
        t.extraction_confidence = min(t.extraction_confidence, round(best.mean_conf, 2))
    notes = (
        f"OCR (scale {best.scale}x, psm {best.psm}, mean word confidence {best.mean_conf:.0%}, "
        f"{len(candidates)} run(s)); steps: {', '.join(prepared.steps)}"
    )
    return PageExtractionResult(tests=best.result.tests, page_notes=notes, method="ocr")
