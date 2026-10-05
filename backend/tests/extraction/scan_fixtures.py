"""Builds realistic "bad scan" test images from the repo's clean documents,
with KNOWN injected defects (rotation angle, noise level, contrast loss,
shadow, JPEG damage, ...), so tests can assert measured recovery against
ground truth instead of eyeballing output.

Everything is deterministic (seeded RNG) and generated on demand; the same
functions back scripts/generate_scan_fixtures.py, which writes PNGs to disk
for manual/UI testing.
"""
from pathlib import Path

import cv2
import numpy as np
import pymupdf as fitz

SAMPLES = Path(__file__).resolve().parents[3] / "eval" / "sample_reports"
RENDER_DPI = 200


def render_pdf_page(filename: str, page_index: int = 0, dpi: int = RENDER_DPI) -> np.ndarray:
    """Rasterizes one page of a sample PDF to a BGR image (a perfectly clean 'scan')."""
    doc = fitz.open(SAMPLES / filename)
    pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    doc.close()
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def to_png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def to_jpeg(img: np.ndarray, quality: int = 80) -> bytes:
    """Scanned PDFs embed JPEGs; PNG-embedding makes multi-page fixtures huge."""
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buf.tobytes()


def rotate(img: np.ndarray, angle_deg: float) -> np.ndarray:
    """Rotates content by `angle_deg` (counter-clockwise positive), keeping the
    canvas size and filling exposed corners with white paper."""
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))


def rotate_90s(img: np.ndarray, quarter_turns_ccw: int) -> np.ndarray:
    return np.ascontiguousarray(np.rot90(img, quarter_turns_ccw))


def add_gaussian_noise(img: np.ndarray, sigma: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noisy = img.astype(np.float32) + rng.normal(0, sigma, img.shape)
    return np.clip(noisy, 0, 255).astype(np.uint8)


def add_salt_pepper(img: np.ndarray, amount: float = 0.01, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = img.copy()
    mask = rng.random(img.shape[:2])
    out[mask < amount / 2] = 0
    out[mask > 1 - amount / 2] = 255
    return out


def reduce_contrast(img: np.ndarray, factor: float = 0.45, floor: int = 110) -> np.ndarray:
    """Compresses dynamic range toward a grey band, like a faded photocopy."""
    return (img.astype(np.float32) * factor + floor).clip(0, 255).astype(np.uint8)


def add_shadow(img: np.ndarray, darkest: float = 0.5) -> np.ndarray:
    """Left-to-right illumination falloff (uneven lighting / page curl shadow)."""
    h, w = img.shape[:2]
    ramp = np.linspace(darkest, 1.0, w, dtype=np.float32)[None, :, None]
    return (img.astype(np.float32) * ramp).clip(0, 255).astype(np.uint8)


def add_paper_tint(img: np.ndarray, bgr_gain=(0.80, 0.92, 1.0)) -> np.ndarray:
    return (img.astype(np.float32) * np.array(bgr_gain, dtype=np.float32)).clip(0, 255).astype(np.uint8)


def blur(img: np.ndarray, ksize: int = 3) -> np.ndarray:
    return cv2.GaussianBlur(img, (ksize, ksize), 0)


def jpeg_damage(img: np.ndarray, quality: int = 30) -> np.ndarray:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def realistic_bad_scan(img: np.ndarray, angle_deg: float = 4.0, seed: int = 0) -> np.ndarray:
    """Several defects at once, in the order a real scan accumulates them."""
    out = rotate(img, angle_deg)
    out = add_shadow(out, 0.6)
    out = add_paper_tint(out)
    out = blur(out, 3)
    out = add_gaussian_noise(out, 8, seed)
    return jpeg_damage(out, 45)
