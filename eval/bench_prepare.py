"""Hardware-independent comparison of how much CPU work and upload each document needs BEFORE
the AI is even called: the old way (render every page to a 200-DPI PNG up front) vs the current
way (read text up front, render lazily, send text-only or a 150-DPI JPEG). No API calls.

  python eval/bench_prepare.py            (run from the repo root, with backend/venv)
"""
import base64
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import pymupdf as fitz  # noqa: E402

from app.services import pdf_utils, pipeline  # noqa: E402

sys.path.insert(0, str(ROOT / "eval"))
from bench_speed import DEFAULT_FILES  # noqa: E402


def old_way(raw):
    """What the previous version did when a document was opened: text + 200-DPI PNG of every page."""
    doc = fitz.open(stream=raw, filetype="pdf")
    zoom = 200 / 72
    out = []
    for page in doc:
        out.append((page.get_text("text"), page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")))
    return [len(base64.b64encode(png)) for _, png in out]


def new_way(raw):
    pages = pdf_utils.load_pages(raw, "application/pdf")
    sizes, images = [], 0
    for p in pages:
        image, text = pipeline._llm_inputs(p)
        images += bool(image)
        sizes.append(len(base64.b64encode(image)) + len((text or "").encode()))
    return sizes, images


def cpu(fn):
    c0 = time.process_time()
    out = fn()
    return out, time.process_time() - c0


rows, tot = [], [0.0, 0.0, 0, 0, 0, 0]
for f in DEFAULT_FILES:
    if not f.exists():
        print(f"(skipping {f.name}: not in the repository, it contains a real patient's details)")
        continue
    raw = f.read_bytes()
    old, t_old = cpu(lambda: old_way(raw))
    (new, imgs), t_new = cpu(lambda: new_way(raw))
    rows.append((f.name.replace("sterling-accuris-pathology-sample-report-unlocked", "sterling"), len(old), t_old, t_new,
                 sum(old) / 1e6, sum(new) / 1e6, imgs))
    tot[0] += t_old; tot[1] += t_new; tot[2] += sum(old); tot[3] += sum(new); tot[4] += len(old); tot[5] += imgs

print("| Document | Pages | CPU before (s) | CPU now (s) | Upload before (MB) | Upload now (MB) | Pages sent as image |")
print("|---|---|---|---|---|---|---|")
for n, pages, a, b, mb_a, mb_b, imgs in rows:
    print(f"| {n} | {pages} | {a:.2f} | {b:.2f} | {mb_a:.2f} | {mb_b:.2f} | {imgs}/{pages} |")
print(f"| **All six** | {tot[4]} | **{tot[0]:.2f}** | **{tot[1]:.2f}** | **{tot[2]/1e6:.2f}** | **{tot[3]/1e6:.2f}** | {tot[5]}/{tot[4]} |")
