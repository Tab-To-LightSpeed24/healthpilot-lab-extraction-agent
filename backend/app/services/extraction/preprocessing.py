"""Image clean-up for scanned/photographed pages, ahead of OCR.

Pipeline (each step is conditional where doing it unconditionally can hurt):
  decode -> upscale if tiny -> grayscale -> orientation (0/90/180/270 via
  Tesseract OSD) -> illumination/shadow normalization -> denoise (only if
  measurably noisy) -> contrast stretch (only if measurably flat) ->
  deskew (projection-profile search, +/-15 deg) -> optional binarization.

Returns a PreprocessResult with BOTH the cleaned grayscale (what Tesseract
generally reads best) and an adaptive-threshold binary variant (a different
failure profile; the OCR stage can retry with it), plus a record of exactly
which steps ran and the measured defects, so behaviour is explainable and
testable.
"""
from dataclasses import dataclass, field
from typing import List, Optional

import cv2
import numpy as np

MAX_SKEW_DEG = 15.0
# Inputs above this (e.g. a 12MP phone photo) are downscaled first: every
# later step, and Tesseract, scales with pixel count, and memory is the
# binding constraint on the deployment target.
MAX_INPUT_PIXELS = 9_000_000
# Below this width the glyphs are too small for reliable OCR; upscale.
MIN_WIDTH_PX = 1000
# Tesseract's OSD needs a decent amount of text and is unreliable below this
# confidence; a wrong 90-degree "correction" is worse than none.
OSD_MIN_CONFIDENCE = 1.5
# Thresholds below were chosen from measurements on the generated fixtures
# (tests/extraction/scan_fixtures.py), not guessed:
#   mean |pixel - 3x3 median|:  clean 0.14 | blurred+noisy scan 1.3 | sigma-8 noise 2.4
#   Laplacian variance:         clean 387  | gaussian-blur 82 | faded 101 | bad scan 143
#   Otsu paper-vs-ink range:    clean 224  | faded 114 | bad scan 159
NOISE_RESIDUAL_THRESHOLD = 0.8
SHARPNESS_THRESHOLD = 250.0
# (Grey-level std and a percentile-based range were tried first and rejected:
# both are meaningless on sparse text pages that are mostly white paper.)
LOW_CONTRAST_RANGE = 150.0


@dataclass
class PreprocessResult:
    gray: np.ndarray
    steps: List[str] = field(default_factory=list)
    skew_deg: float = 0.0           # detected tilt of the input (CCW positive)
    rotation_deg: int = 0           # 90-degree-multiple correction applied (clockwise)
    noise_residual: float = 0.0
    contrast_range: float = 0.0


def decode_image(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode image bytes")
    return img


def measure_noise(gray: np.ndarray) -> float:
    return float(np.mean(np.abs(gray.astype(np.int16) - cv2.medianBlur(gray, 3).astype(np.int16))))


def normalize_illumination(gray: np.ndarray) -> np.ndarray:
    """Divides out the slowly-varying background (shadows, uneven lighting,
    paper tint) estimated with a large blur of a text-erased copy, then
    rescales to full range."""
    h, w = gray.shape
    k = max(31, (min(h, w) // 12) | 1)
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    background = cv2.GaussianBlur(background, (k, k), 0)
    norm = cv2.divide(gray, background, scale=255)
    return norm


def contrast_range(gray: np.ndarray) -> float:
    """Mean paper grey minus mean ink grey, split at the Otsu threshold --
    independent of how much of the page is ink."""
    t, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink, paper = gray[gray < t], gray[gray >= t]
    if ink.size == 0 or paper.size == 0:
        return 0.0
    return float(paper.mean() - ink.mean())


def sharpen(gray: np.ndarray) -> np.ndarray:
    soft = cv2.GaussianBlur(gray, (0, 0), 1.6)
    return cv2.addWeighted(gray, 2.0, soft, -1.0, 0)


def estimate_skew(gray: np.ndarray, max_angle: float = MAX_SKEW_DEG) -> float:
    """Tilt of the text lines in degrees (CCW positive), via the classical
    projection-profile method: the correct angle is the one at which text
    rows line up so the horizontal ink profile is sharpest. Large solid
    blocks (colour banners, rules) are masked out first so they don't
    dominate the profile."""
    h, w = gray.shape
    scale = 900.0 / max(h, w)
    small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else gray
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)

    n, labels, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    sh, sw = bw.shape
    keep = np.zeros(n, dtype=bool)
    for i in range(1, n):
        cw, ch, area = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT], stats[i, cv2.CC_STAT_AREA]
        if cw < sw * 0.25 and ch < sh * 0.08 and area > 2:
            keep[i] = True
    text_mask = (keep[labels]).astype(np.uint8) * 255
    if int(text_mask.sum()) == 0:
        return 0.0

    def sharpness(angle: float) -> float:
        m = cv2.getRotationMatrix2D((sw / 2, sh / 2), angle, 1.0)
        rotated = cv2.warpAffine(text_mask, m, (sw, sh), flags=cv2.INTER_LINEAR)
        profile = rotated.sum(axis=1, dtype=np.float64)
        return float(np.sum(np.diff(profile) ** 2))

    coarse = np.arange(-max_angle, max_angle + 1e-9, 0.5)
    best = max(coarse, key=sharpness)
    fine = np.arange(best - 0.5, best + 0.5 + 1e-9, 0.1)
    best = max(fine, key=sharpness)
    # `best` is the rotation that straightens the text; the input's tilt is
    # its negation.
    return float(-best)


def rotate_keep_size(img: np.ndarray, angle_deg: float) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def detect_rotation(gray: np.ndarray) -> Optional[int]:
    """Clockwise quarter-turn correction (0/90/180/270) from Tesseract's
    orientation detection, or None if Tesseract is unavailable or not
    confident. Imported lazily so preprocessing works (minus this step)
    without the OCR engine."""
    try:
        import pytesseract
        from app.services.extraction.ocr import configure_tesseract

        configure_tesseract()
        h, w = gray.shape
        scale = 1600.0 / max(h, w)
        probe = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
        osd = pytesseract.image_to_osd(probe, config="--psm 0", output_type=pytesseract.Output.DICT, timeout=20)
        if float(osd.get("orientation_conf", 0)) < OSD_MIN_CONFIDENCE:
            return None
        return int(osd["rotate"]) % 360
    except Exception:
        return None


def apply_quarter_turn(img: np.ndarray, clockwise_deg: int) -> np.ndarray:
    return {
        0: img,
        90: cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE),
        180: cv2.rotate(img, cv2.ROTATE_180),
        270: cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE),
    }[clockwise_deg]


def preprocess(image_bytes: bytes, detect_orientation: bool = True) -> PreprocessResult:
    img = decode_image(image_bytes)
    steps: List[str] = []

    h0, w0 = img.shape[:2]
    if h0 * w0 > MAX_INPUT_PIXELS:
        shrink = (MAX_INPUT_PIXELS / (h0 * w0)) ** 0.5
        img = cv2.resize(img, None, fx=shrink, fy=shrink, interpolation=cv2.INTER_AREA)
        steps.append(f"downscale x{shrink:.2f}")

    h, w = img.shape[:2]
    if w < MIN_WIDTH_PX:
        factor = MIN_WIDTH_PX / w
        img = cv2.resize(img, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        steps.append(f"upscale x{factor:.2f}")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    rotation = 0
    if detect_orientation:
        found = detect_rotation(gray)
        if found:
            gray = apply_quarter_turn(gray, found)
            rotation = found
            steps.append(f"orientation {found}deg")

    gray = normalize_illumination(gray)
    steps.append("illumination normalized")

    noise = measure_noise(gray)
    if noise > NOISE_RESIDUAL_THRESHOLD:
        gray = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=15)
        steps.append(f"denoise (residual {noise:.1f})")

    contrast = contrast_range(gray)
    if contrast < LOW_CONTRAST_RANGE:
        gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
        steps.append(f"contrast CLAHE (range {contrast:.0f})")

    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if sharpness < SHARPNESS_THRESHOLD:
        gray = sharpen(gray)
        steps.append(f"sharpen (laplacian var {sharpness:.0f})")

    skew = estimate_skew(gray)
    if abs(skew) >= 0.3:
        gray = rotate_keep_size(gray, -skew)
        steps.append(f"deskew {skew:+.1f}deg")

    return PreprocessResult(
        gray=gray, steps=steps, skew_deg=skew,
        rotation_deg=rotation, noise_residual=noise, contrast_range=contrast,
    )
