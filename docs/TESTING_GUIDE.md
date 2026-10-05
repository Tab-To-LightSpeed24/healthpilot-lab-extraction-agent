# Testing Guide — HealthPilot AI Lab Extraction & LOINC Coding Agent

Live URLs:
- Frontend: https://healthpilot-lab-extraction-agent.vercel.app
- Backend API: https://healthpilot-api-2b2m.onrender.com
- API docs (Swagger): https://healthpilot-api-2b2m.onrender.com/docs

Sample reports to upload are in `eval/sample_reports/` in this repo
(10 files, see `eval/DATASET.md` for what each one tests).

## 1. One-time setup

Open the frontend URL. There is nothing to configure: the page picks the
backend automatically (localhost -> local backend, otherwise the Render URL).
If the backend is unreachable you will see an amber "Backend unreachable"
banner and a red message in the Reports card (never an endless "Loading...");
details are in the browser console (`[api]` / `[health]` lines).

Render's free tier spins down after ~15 min idle — the first request after
a period of inactivity can take 30-60s to wake up. If the reports list shows
"Failed to fetch" on first load, wait a few seconds and refresh.

## 2. Smoke test (no upload, ~30 seconds)

In the **LOINC Terminology Search** box, type `glucose`. You should see two
results (serum and urine Glucose, different LOINC codes) — confirms the
backend, database, and CORS are all wired correctly end-to-end.

## 3. Golden-path test

Upload `eval/sample_reports/01_cbc_clean_digital.pdf` (a clean CBC panel).
Expected: status flips `pending → processing → complete` within ~10-20s, and
the observation table fills in with 5 rows (WBC, RBC, Hemoglobin, Hematocrit,
Platelet Count), each with a green **confirmed** badge and a real LOINC code
(e.g. Hemoglobin → `718-7`).

## 4. Edge-case tests (what to look for)

| File | What to check |
|---|---|
| `03_lipid_messy_layout.pdf` | Despite dot-leaders and inline ranges, all 4 lipid values extract correctly |
| `04_synonyms_abbreviations.pdf` | Abbreviations like "SGOT"/"SGPT"/"A1C" still map to the right LOINC codes |
| `05_thyroid_partial_fields.pdf` | "Free T4 = Normal" (no unit/range) still gets extracted, not dropped |
| `06_urinalysis.pdf` | Confirms specimen-aware mapping — urine Glucose must NOT map to the serum Glucose LOINC code |
| `07_multipage_panel.pdf` | Each observation's **Page** column correctly shows 1 or 2 depending on which page it came from |
| `08_unmapped_novel_test.pdf` | The "Interleukin-6 (IL-6)" row should show a yellow **needs review** or red **unmapped** badge — NOT a confidently-confirmed made-up LOINC code. This is the most important one: it's the test for "never fabricate a mapping." |
| `09_coag_scanned_image.png` | A PNG image (simulated scan, no text layer) — with the AI on, confirms the vision path; with it off, the local OCR path |
| `10_plain_text_report.txt` | Plain `.txt` upload — confirms non-PDF/image formats are accepted |

## 5. No-AI (fallback) mode

The AI is optional. If it is unavailable, slow, or out of credit, documents
are extracted locally and flagged. To test this deliberately without spending
API credit, run the backend with `LLM_ENABLED=false` (or an empty
`OPENROUTER_API_KEY`) and upload any of the sample reports.

Expected:
- A yellow **Lower accuracy** banner on the report, a yellow **no AI** tag in
  the report list, and yellow **auto-parsed** badges on the rows.
- Digital PDFs and text files still extract (values match the sample's gold
  labels in `eval/gold_labels.json`). `11_apollo_complex_layout_real.pdf` is the
  hard multi-column case (3 pages).
- Scanned inputs work if Tesseract is installed. Generate degraded scans with
  `python backend/scripts/generate_scan_fixtures.py` (writes to
  `eval/sample_reports/scans/`: skewed, noisy, faded, sideways, upside-down) and
  upload them. Without Tesseract the report fails with a clear "OCR engine is
  not installed" reason.
- Rows whose value looks wrong show a note such as "possible lost decimal
  point (did you mean 7.15?)" with a **Use 7.15** button. The stored value
  never changes until you click it.
- LOINC codes: exact-alias rows are confirmed; everything else is
  **needs review** (the AI re-ranking step is skipped), never guessed.

### Editing results (works with or without the AI)

On any row: **Edit** (change fields, Save), **Delete**, and **+ Add row** above
the list. Renaming a test re-derives its LOINC code; editing a value re-checks
it. Expect rows you fixed to lose the yellow tint and show an **edited** badge,
and rows you add to show **added by you**. None of this ever calls the AI.

## 6. What "success" looks like overall

- Every uploaded report reaches `complete` (or `failed` with a clear
  `error_message` if something genuinely went wrong — never silently stuck).
- Every returned observation has a `mapping_status` badge — confirmed
  (green), needs_review (yellow), or unmapped (red) — never left blank.
- Clicking a low-confidence badge shows a rationale (hover tooltip) instead
  of just a bare code.
- Nothing in `08_unmapped_novel_test.pdf`'s IL-6 row should be a confidently
  green-badged LOINC code — if it is, that's a real bug (fabricated mapping),
  please flag it back to me.

## 7. Reporting back

If something looks wrong, the most useful thing to send back is:
1. Which file you uploaded.
2. A screenshot or copy of the observation row that looked wrong.
3. The `document_id` (visible in the URL bar of `/reports/{id}` if you hit
   the API directly, or I can look it up from the filename + timestamp).

I can also pull the real evaluation numbers (`eval/eval_results.md`) any time
by re-running `eval/run_eval.py` against the live URL — that's scored
automatically against known-correct answers, not eyeballed.
