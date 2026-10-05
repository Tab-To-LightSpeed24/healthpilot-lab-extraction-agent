"""Writes realistic "bad scan" PNGs (known injected defects) to
eval/sample_reports/scans/ for manual / UI testing of the scanned-document
fallback. The same degradation functions back the automated tests
(tests/extraction/scan_fixtures.py), so what you upload by hand is exactly
what the tests exercise.

    python scripts/generate_scan_fixtures.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.extraction import scan_fixtures as sf  # noqa: E402

OUT = sf.SAMPLES / "scans"

CASES = [
    # (output name, source pdf, page, transform, description)
    ("cbc_skew4_noisy.png", "01_cbc_clean_digital.pdf", 0, lambda i: sf.realistic_bad_scan(i, 4.0),
     "4 deg skew + shadow + paper tint + blur + noise + JPEG damage"),
    ("cmp_skew_minus3.png", "02_cmp_clean_digital.pdf", 0, lambda i: sf.realistic_bad_scan(i, -3.0),
     "-3 deg skew + the same degradations"),
    ("lipid_faded.png", "03_lipid_messy_layout.pdf", 0, lambda i: sf.reduce_contrast(sf.rotate(i, 2.0)),
     "faded photocopy, 2 deg skew"),
    ("urinalysis_noisy.png", "06_urinalysis.pdf", 0, lambda i: sf.add_salt_pepper(sf.add_gaussian_noise(i, 10), 0.01),
     "gaussian + salt-and-pepper noise"),
    ("cbc_rotated_90.png", "01_cbc_clean_digital.pdf", 0, lambda i: sf.rotate_90s(i, 1),
     "page scanned sideways (90 deg)"),
    ("cbc_upside_down.png", "01_cbc_clean_digital.pdf", 0, lambda i: sf.rotate_90s(i, 2),
     "page scanned upside down (180 deg)"),
    ("apollo_complex_skew3.png", "11_apollo_complex_layout_real.pdf", 0, lambda i: sf.realistic_bad_scan(i, 3.0),
     "real multi-column coloured layout with gauges, degraded (hardest case)"),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, pdf, page, transform, description in CASES:
        img = transform(sf.render_pdf_page(pdf, page))
        (OUT / name).write_bytes(sf.to_png(img))
        manifest[name] = {"source": pdf, "page": page + 1, "defects": description}
        print(f"wrote {OUT / name}")
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
