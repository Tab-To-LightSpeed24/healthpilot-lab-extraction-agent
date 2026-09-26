# Evaluation Dataset Composition

10 synthetic lab reports, generated programmatically by `eval/generate_dataset.py`
(real PDF/PNG/TXT files, not hand-drawn mockups) into `eval/sample_reports/`.
Gold-standard expected extraction + LOINC codes are in `eval/gold_labels.json`.

Synthetic data was used instead of real patient reports for two reasons: (1)
real lab reports are PHI and cannot legally be used as a public test fixture,
and (2) synthetic generation lets every layout/edge case below be constructed
deliberately and gives us ground truth to score against, which real reports
scraped from the internet would not.

| # | File | Format | What it stresses |
|---|---|---|---|
| 1 | `01_cbc_clean_digital.pdf` | Digital PDF | Baseline clean CBC panel, standard tabular layout |
| 2 | `02_cmp_clean_digital.pdf` | Digital PDF | Baseline clean CMP/BMP panel |
| 3 | `03_lipid_messy_layout.pdf` | Digital PDF | Irregular spacing/dot-leaders, inline reference ranges in parentheses |
| 4 | `04_synonyms_abbreviations.pdf` | Digital PDF | Non-canonical test names ("Sugar (Fasting)", "SGOT", "SGPT", "A1C", "Na+") to exercise alias + embedding mapping |
| 5 | `05_thyroid_partial_fields.pdf` | Digital PDF | Missing unit/reference range on some rows; one purely qualitative result ("Normal") |
| 6 | `06_urinalysis.pdf` | Digital PDF | Specimen-context-dependent mapping (urine vs serum Glucose/Protein must resolve to different LOINC codes) |
| 7 | `07_multipage_panel.pdf` | Digital PDF (2 pages) | Page-level source traceability across a multi-page document |
| 8 | `08_unmapped_novel_test.pdf` | Digital PDF | Contains a test (IL-6) deliberately absent from the curated LOINC subset — must be flagged `needs_review`/`unmapped`, never silently mis-mapped |
| 9 | `09_coag_scanned_image.png` | Rasterized image (simulated scan) | No extractable text layer — pipeline must rely on the vision path only |
| 10 | `10_plain_text_report.txt` | Plain text | Non-PDF, non-image input format; also has no rasterized page (tests the text-only extraction code path) |

## Known limitation of this dataset

These are synthetic, cleanly-generated documents (except for being intentionally
messy in layout). We do not have genuinely low-quality scanned images (skewed,
noisy, handwritten annotations) because generating a *realistic* bad scan
without real source material is itself unreliable, and a fake "blurry filter"
would test our blur filter, not real-world OCR robustness. Case 9 stresses the
no-text-layer / vision-only code path but not actual scan noise. If more time
were available, the next addition would be a handful of real (de-identified)
scanned reports or photographs of printed reports.

## Running the evaluation

Requires a running backend with `GEMINI_API_KEY` set (real API calls, no mocking):

```bash
pip install -r eval/requirements.txt
python eval/generate_dataset.py   # regenerate sample files if needed
python eval/run_eval.py --api-base http://127.0.0.1:8000
```

This writes `eval/eval_results.md` with per-document and overall extraction/mapping
accuracy, scored against `gold_labels.json`. The script makes real HTTP calls to
the real API — it does not fabricate results.
