"""Preprocessing tests assert measured recovery against defects injected at
KNOWN strengths (see scan_fixtures.py) -- not just "it ran without error"."""
import cv2
import numpy as np
import pytest

from app.services.extraction import preprocessing as pp
from app.services.extraction.ocr import ocr_available
from tests.extraction import scan_fixtures as sf

needs_tesseract = pytest.mark.skipif(not ocr_available(), reason="Tesseract OCR engine not installed")


@pytest.fixture(scope="module")
def clean():
    return sf.render_pdf_page("02_cmp_clean_digital.pdf")


def _gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


@pytest.mark.parametrize("angle", [-8.0, -3.0, 2.0, 5.0, 10.0])
def test_skew_estimate_matches_the_injected_angle(clean, angle):
    estimated = pp.estimate_skew(pp.normalize_illumination(_gray(sf.rotate(clean, angle))))
    assert abs(estimated - angle) <= 0.4, f"injected {angle} deg, estimated {estimated:.2f}"


def test_straight_page_reports_no_skew(clean):
    assert abs(pp.estimate_skew(_gray(clean))) <= 0.3


def test_deskew_leaves_residual_tilt_under_half_a_degree(clean):
    result = pp.preprocess(sf.to_png(sf.rotate(clean, 6.0)), detect_orientation=False)
    assert abs(result.skew_deg - 6.0) <= 0.4
    assert abs(pp.estimate_skew(result.gray)) <= 0.5, "the corrected image must itself be nearly straight"
    assert any(step.startswith("deskew") for step in result.steps)


def test_clean_page_is_left_alone(clean):
    result = pp.preprocess(sf.to_png(clean), detect_orientation=False)
    applied = " ".join(result.steps)
    for unwanted in ("denoise", "sharpen", "deskew", "CLAHE", "upscale"):
        assert unwanted not in applied, f"{unwanted} must not run on an already-clean page: {result.steps}"


def test_noise_is_detected_and_reduced(clean):
    noisy = sf.add_gaussian_noise(clean, sigma=8)
    before = pp.measure_noise(pp.normalize_illumination(_gray(noisy)))
    result = pp.preprocess(sf.to_png(noisy), detect_orientation=False)
    assert any(step.startswith("denoise") for step in result.steps)
    assert pp.measure_noise(result.gray) < before / 2


def test_faded_page_gets_contrast_restored(clean):
    faded = sf.reduce_contrast(clean)
    result = pp.preprocess(sf.to_png(faded), detect_orientation=False)
    assert any("CLAHE" in step for step in result.steps)
    assert pp.contrast_range(result.gray) > pp.contrast_range(pp.normalize_illumination(_gray(faded))) + 20


def test_blurry_page_is_sharpened(clean):
    blurred = sf.blur(clean, 3)
    before = cv2.Laplacian(pp.normalize_illumination(_gray(blurred)), cv2.CV_64F).var()
    result = pp.preprocess(sf.to_png(blurred), detect_orientation=False)
    assert any(step.startswith("sharpen") for step in result.steps)
    assert cv2.Laplacian(result.gray, cv2.CV_64F).var() > before * 1.5


def test_uneven_lighting_is_flattened(clean):
    shadowed = sf.add_shadow(clean, darkest=0.5)
    g = _gray(shadowed)
    strip = lambda im, x0, x1: float(im[:, int(x0 * im.shape[1]):int(x1 * im.shape[1])].mean())
    before_ratio = strip(g, 0.0, 0.1) / strip(g, 0.9, 1.0)
    flat = pp.normalize_illumination(g)
    after_ratio = strip(flat, 0.0, 0.1) / strip(flat, 0.9, 1.0)
    assert before_ratio < 0.7, "fixture should be visibly shadowed"
    assert after_ratio > 0.95, f"paper brightness should be even after normalization, ratio {after_ratio:.2f}"


def test_tiny_image_is_upscaled():
    small = cv2.resize(sf.render_pdf_page("02_cmp_clean_digital.pdf"), None, fx=0.3, fy=0.3, interpolation=cv2.INTER_AREA)
    result = pp.preprocess(sf.to_png(small), detect_orientation=False)
    assert result.gray.shape[1] >= pp.MIN_WIDTH_PX
    assert any(step.startswith("upscale") for step in result.steps)


def test_undecodable_bytes_raise_value_error():
    with pytest.raises(ValueError):
        pp.preprocess(b"definitely not an image")


@needs_tesseract
@pytest.mark.parametrize("quarter_turns_ccw,expected_clockwise_fix", [(1, 90), (2, 180), (3, 270)])
def test_page_rotated_by_quarter_turns_is_turned_back_upright(clean, quarter_turns_ccw, expected_clockwise_fix):
    result = pp.preprocess(sf.to_png(sf.rotate_90s(clean, quarter_turns_ccw)))
    assert result.rotation_deg == expected_clockwise_fix
    # Upright means wider-than-tall text lines again: the corrected image has
    # the original page's portrait orientation.
    assert result.gray.shape[0] > result.gray.shape[1]


@needs_tesseract
def test_upright_page_is_not_spuriously_rotated(clean):
    assert pp.preprocess(sf.to_png(clean)).rotation_deg == 0
