"""OCR + scanned-page pipeline tests. Tesseract-dependent tests skip (rather
than silently pass) on a machine without the engine."""
import cv2
import pytest

from app.core.config import settings
from app.services.extraction import ocr
from app.services.extraction.ocr import OcrUnavailable, ocr_available, ocr_layout
from app.services.extraction.preprocessing import preprocess
from app.services.extraction.scanned import extract_scanned_page
from tests.extraction import scan_fixtures as sf
from tests.extraction.test_pipeline_digital_e2e import GOLD

needs_tesseract = pytest.mark.skipif(not ocr_available(), reason="Tesseract OCR engine not installed")

SIMPLE = [g for g in GOLD if g[0][:2] in ("01", "02", "03", "04", "06")]
APOLLO = [(n.replace("‎", ""), v) for n, v in next(g for g in GOLD if g[0][:2] == "11")[1]]

# Floors set from measured results (clean 92%, degraded 92% on these fixtures)
# with headroom for Tesseract version differences -- regression guards, not
# quality claims.
MIN_CLEAN_ACCURACY = 0.85
MIN_DEGRADED_ACCURACY = 0.80
# Complex coloured multi-column layout is genuinely hard for OCR (measured
# 3-9 of 12 depending on scale/noise); this only guards against total collapse.
MIN_COMPLEX_HITS = 3


def _accuracy(make_image, docs=SIMPLE):
    hits = total = 0
    for filename, expected in docs:
        result = extract_scanned_page(sf.to_png(make_image(sf.render_pdf_page(filename))), 1)
        got = {t.original_test_name: t.value for t in result.tests}
        for name, value in expected:
            total += 1
            hits += got.get(name) == value
    return hits / total


@needs_tesseract
def test_ocr_layout_returns_fractional_geometry_confidence_and_phrase_spans():
    gray = preprocess(sf.to_png(sf.render_pdf_page("01_cbc_clean_digital.pdf"))).gray
    layout = ocr_layout(gray, page_number=1)
    assert layout.source == "ocr" and layout.lines
    for line in layout.lines:
        assert 0 <= line.bbox.x0 < line.bbox.x1 <= 1 and 0 <= line.bbox.y0 < line.bbox.y1 <= 1
        for span in line.spans:
            assert span.source == "ocr" and 0 <= span.confidence <= 1
    hemoglobin = next(l for l in layout.lines if l.spans[0].text.startswith("Hemoglobin"))
    assert len(hemoglobin.spans) >= 3, "a table row must come back as separate column phrases, not one blob"
    assert [l.bbox.y0 for l in layout.lines] == sorted(l.bbox.y0 for l in layout.lines)


@needs_tesseract
def test_clean_scans_extract_with_high_accuracy():
    assert _accuracy(lambda img: img) >= MIN_CLEAN_ACCURACY


@needs_tesseract
def test_badly_degraded_scans_still_extract_with_good_accuracy():
    """4-degree skew + shadow + paper tint + blur + noise + JPEG damage, all at once."""
    assert _accuracy(lambda img: sf.realistic_bad_scan(img, angle_deg=4.0)) >= MIN_DEGRADED_ACCURACY


@needs_tesseract
def test_preprocessing_is_what_makes_degraded_scans_readable():
    """Same degraded image, OCR'd with and without the preprocessing stage:
    the cleaned version must be clearly better, or the stage isn't earning
    its keep."""
    def raw_accuracy():
        hits = total = 0
        for filename, expected in SIMPLE:
            bad = sf.realistic_bad_scan(sf.render_pdf_page(filename), angle_deg=4.0)
            gray = cv2.cvtColor(bad, cv2.COLOR_BGR2GRAY)
            try:
                got = {t.original_test_name: t.value for t in __import__(
                    "app.services.extraction.field_parser", fromlist=["parse_page"]
                ).parse_page(ocr_layout(gray, 1)).tests}
            except OcrUnavailable:
                got = {}
            for name, value in expected:
                total += 1
                hits += got.get(name) == value
        return hits / total

    raw = raw_accuracy()
    cleaned = _accuracy(lambda img: sf.realistic_bad_scan(img, angle_deg=4.0))
    assert cleaned >= raw + 0.15, f"preprocessing gain too small: raw {raw:.0%} -> cleaned {cleaned:.0%}"


@needs_tesseract
def test_complex_multi_column_report_scan_does_not_collapse():
    page = sf.render_pdf_page("11_apollo_complex_layout_real.pdf", 0)
    result = extract_scanned_page(sf.to_png(sf.realistic_bad_scan(page, angle_deg=3.0)), 1)
    got = {t.original_test_name.replace("‎", ""): t.value for t in result.tests}
    assert sum(got.get(n) == v for n, v in APOLLO) >= MIN_COMPLEX_HITS
    assert result.page_notes and "OCR" in result.page_notes


@needs_tesseract
@pytest.mark.parametrize("turns", [1, 2, 3])
def test_rotated_pages_are_read_end_to_end(turns):
    img = sf.rotate_90s(sf.render_pdf_page("01_cbc_clean_digital.pdf"), turns)
    names = {t.original_test_name for t in extract_scanned_page(sf.to_png(img), 1).tests}
    assert {"WBC", "Hemoglobin", "Platelet Count"} <= names


@needs_tesseract
def test_row_confidence_reflects_ocr_confidence():
    result = extract_scanned_page(sf.to_png(sf.render_pdf_page("02_cmp_clean_digital.pdf")), 1)
    assert result.tests and all(0 < t.extraction_confidence <= 1 for t in result.tests)


def test_missing_engine_is_reported_not_crashed(monkeypatch):
    monkeypatch.setattr(ocr, "find_tesseract", lambda: None)
    with pytest.raises(OcrUnavailable, match="not installed"):
        ocr.configure_tesseract()


def test_ocr_can_be_disabled_by_setting(monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", False)
    with pytest.raises(OcrUnavailable, match="disabled"):
        ocr.configure_tesseract()
    assert ocr_available() is False


@needs_tesseract
def test_a_hung_ocr_run_is_abandoned_at_the_timeout(monkeypatch):
    monkeypatch.setattr(settings, "ocr_page_timeout_seconds", 0.01)
    gray = preprocess(sf.to_png(sf.render_pdf_page("01_cbc_clean_digital.pdf"))).gray
    with pytest.raises(OcrUnavailable, match="timed out"):
        ocr_layout(gray, 1)


@needs_tesseract
def test_blank_page_yields_a_clear_error_not_an_empty_success():
    import numpy as np
    blank = sf.to_png(np.full((1200, 900, 3), 255, dtype=np.uint8))
    with pytest.raises(OcrUnavailable):
        extract_scanned_page(blank, 1)
