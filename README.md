# HealthPilot AI Lab Extraction & LOINC Coding Agent

Internship project submission for HealthPilot.ai. Extracts laboratory results
from lab reports (digital PDF, scanned image, plain text), normalizes test
names, maps each observation to a LOINC code with a confidence/review status,
stores everything in a structured, traceable data store, and exposes it via
API + a web UI — including batch upload, mid-job cancellation, a
human-in-the-loop review action, and full manual correction of results.

**The AI model is optional.** If it is disabled, unconfigured, slow, over
budget, or erroring, the same document is extracted by a fully local pipeline
(layout analysis, image clean-up, Tesseract OCR, rule-based parsing,
validation) and the result is clearly flagged as lower-accuracy for the user
to review and edit. No document depends on a paid API to produce output.

## Architecture

```
  Upload (PDF/PNG/JPG/TXT)  ──►  1. Enqueue: documents row, status=pending (the row IS the queue)
  single or batch                2. Worker thread: claims one document (compare-and-swap),
                                    heartbeat + stuck-job recovery, cooperative cancel
                                 3. pdf_utils: PDF -> text layer + page image; image -> page image;
                                    text -> text
                                            │
                      ┌─────────────────────┴──────────────────────┐
                      ▼                                            ▼
        4a. PRIMARY: LLM extraction                    4b. LOCAL FALLBACK (no API, no key)
        llm_client (Gemini)                        digital PDF  -> layout reconstruction
        one multimodal call per page                                    -> rule-based field parser
        25s per request, 45s total budget              plain text   -> field parser
        incl. retries; first failure that               scanned PDF /  -> preprocessing (rotation,
        will repeat (timeout, credits, bad              image           deskew, shadow, noise,
        key, outage) stops LLM attempts for                             sharpen) -> Tesseract OCR
        the rest of the document                                        (bounded retries) -> parser
                      │                                            │
                      │                              5. Validation (local): lost-decimal detection
                      │                                 (suggest, never silently change), garbled
                      │                                 units, junk-row removal, OCR label repair,
                      │                                 per-row confidence
                      └─────────────────────┬──────────────────────┘
                                            ▼
                      6. normalization + loinc_mapping
                         stage 1: alias-exact match against the full ~62k-code LOINC table
                                  + a small human-verified override list (no model)
                         stage 2: ranked lexical DB search
                         stage 3: LLM re-rank with specimen context -- SKIPPED in fallback mode
                                  (left needs_review with unverified suggestions, never guessed)
                                            ▼
                      7. Postgres (SQLAlchemy + Alembic): documents -> observations ->
                         loinc_codes/loinc_aliases; every observation keeps document_id,
                         page_number, extraction_source (llm | fallback | manual), is_edited
                                            ▼
                      8. REST API: /reports, /observations (full CRUD), /loinc, /fhir, /cancel, /review
                                            ▲
                                            │ CORS fetch
                      Static frontend (HTML/JS/Tailwind CDN): upload (single/batch), report list,
                      clinical + FHIR views, yellow "lower accuracy" banner, edit / add / delete
                      rows, one-click "use suggested value", cancel, inline LOINC review
```

## Key engineering decisions

**LLM-first, with a complete local fallback.** A multimodal LLM is the
highest-quality extractor for arbitrary layouts, so it stays the primary path.
But depending on a paid, rate-limited, quota-limited API for *every* document
is a real operational risk — it happened here: an account with a tiny credit
balance returned `402` on every page and the UI showed nothing useful. So the
LLM call is bounded (25 s per request, 45 s total including retries), and the
first failure that would repeat (timeout, insufficient credits, bad key,
provider outage) stops LLM attempts for the rest of that document instead of
costing one timeout per page. A one-off malformed reply only diverts that one
page. Retries only happen for 429/5xx; billing/config errors fail immediately
with their *real* message (tenacity's `RetryError` wrapper used to hide it).
Setting `LLM_ENABLED=false` runs the app with zero API calls.

**The local pipeline (`backend/app/services/extraction/`)** is a set of small
stages with explicit input/output types, each independently tested:

| Stage | Module | What it does |
|---|---|---|
| Layout | `pdf_layout.py`, `layout.py` | Word/line geometry from PyMuPDF, then reading-order reconstruction. Plain `get_text()` jumbles multi-column reports (values separated from their own reference ranges); section headers anchor zones and strictly-overlapping lines merge into rows. |
| Parse | `field_parser.py` | Turns rows into `ExtractedTest` (label / value / unit / range / flag): padded rows, dot-leaders, single-space text, "label above value" cards, wrapped labels, specimen carried down a panel, qualifier values (`Negative`, `<0.5`). |
| Preprocess | `preprocessing.py` | Quarter-turn rotation (Tesseract OSD), shadow flattening, noise reduction, sharpening, contrast restoration, deskew (projection profile). Each step runs only if the defect is *measured*; thresholds come from measurements on generated scans. |
| OCR | `ocr.py`, `scanned.py` | Tesseract words grouped into column "phrases" in the same schema as digital text. Up to 4 runs per page, each timeout-capped, with a total page budget; the best candidate by an internal quality score wins. |
| Validate | `validation.py` | See below. |
| Orchestrate | `fallback.py`, `pipeline.py` | One extractor per document; scanned pages are OCR'd in a small background pool while earlier pages are saved; document-level time budget; cleanup on cancel. |

**Validation never silently changes a clinical value.** OCR's characteristic
failure is dropping a decimal point (`7.15` read as `715`). When a value is far
above its own printed reference range and exactly one decimal placement lands
near the range, the row is flagged, scored below the review threshold, and a
`suggested_value` is offered — the stored value stays as read until a human
applies it. Garbled units (`ror`, `o/at`) are flagged; unambiguous junk rows
from gauge graphics are dropped; a short label that is an OCR misread of a real
test name (`C02` → `CO2`) is repaired only on an exact LOINC-name match. The
junk filter is deliberately conservative — an earlier version keyed on the
deterministic alias index would have deleted real `pH` and `CO2` rows, because
that index intentionally omits ambiguous aliases (see `docs/ISSUES_AND_SOLUTIONS.md`).

**Fallback output is labelled and editable.** Fallback rows carry
`extraction_source="fallback"` and confidence ≤ 0.65 (below the 0.7 review
threshold used by the quality flags). The UI shows a yellow banner, yellow
"auto-parsed" badges, and a "no AI" tag in the report list. Users can add,
edit and delete rows (`/observations` CRUD); editing re-runs validation on the
corrected row, and a row that now passes is marked fully trusted. No CRUD call
ever uses an LLM — LOINC mapping for hand edits is alias-exact only.

**The full official LOINC table (~62k Laboratory/ACTIVE codes), not a small
curated subset.** Downloaded from loinc.org, filtered by
`scripts/build_loinc_data.py`, and committed as a derived ~26 MB CSV (the raw
~1 GB release is gitignored). At this scale a bare abbreviation like "Hgb" is
genuinely ambiguous in LOINC's own data, so `normalization.build_alias_index()`
only auto-resolves unambiguous aliases and a small hand-verified override file
supplies the extremely common cases — deliberately excluding anything
specimen-dependent (bare "Glucose"/"Protein"), which must go through
context-aware mapping so serum vs. urine resolves correctly.

**Three-stage LOINC mapping**, to avoid "superficial text similarity" and flag
uncertainty rather than guess: (1) deterministic alias-exact match, (2) ranked
lexical search ordered by LOINC's own `COMMON_TEST_RANK`, (3) LLM re-rank using
specimen/method context, which can return "no reliable candidate" →
`unmapped`. In fallback mode stage 3 is skipped: unmatched rows are left
`needs_review` with unverified suggestions shown as hints, never auto-assigned.

**Startup seeding is count-checked, not unconditional.** The LOINC tables
(~62k codes, ~1.7M aliases) are only reseeded when their row counts don't match
the CSV, with `TRUNCATE` and 20k-row chunks when they do. Always reseeding made
a real deploy hang for Render's whole 15-minute startup window (never bound a
port); a plain restart now takes under a second, while a partially-seeded
table is still detected and rebuilt.

**A DB-backed durable job queue instead of Celery/RQ + Redis.** The
`documents.status` row *is* the queue entry; a worker thread claims one at a
time with a compare-and-swap update, and a heartbeat (`updated_at`) lets a
startup sweep requeue a job that was mid-`processing` when the process died.
A broker would add infrastructure and a failure mode for no durability benefit
at this scale (single instance, low volume).

**Cooperative mid-job cancellation.** `POST /reports/{id}/cancel` sets a flag
checked between pages (and, for background OCR, cancels queued pages), so a
runaway multi-page document can be stopped without killing the process.

**Human-in-the-loop review is a real, validated action.**
`PATCH /observations/{id}/review` confirms or corrects a mapping; a supplied
code is validated against the reference table server-side, or a reviewer can
explicitly record "no code applies".

**Frontend on Vercel, backend + DB on Render.** Vercel's serverless functions
suit short request/response cycles; this pipeline needs a persistent Postgres
connection and a long-lived worker thread. The static frontend picks its API
base from the hostname (localhost → local backend, otherwise the Render URL),
uses a 45 s fetch timeout, shows a banner if the backend is unreachable, and
logs failed API calls to the console.

**Alembic migrations, not `create_all()`.** Schema changes are versioned
migrations (cross-dialect via `batch_alter_table`). A pre-Alembic database with
tables but no history is stamped at the matching baseline before upgrading.

## What's real vs. mocked in testing

**No test in this repo claims a result it didn't actually produce.**

- `backend/tests/` has **249 tests, all passing at last run**. They use real PDF
  generation and parsing, a real SQLite database seeded with the real ~62k-code
  LOINC table, the real FastAPI app, the real queue/cancel/review/CRUD logic,
  and — for the scanned path — the **real Tesseract engine** (those tests skip,
  rather than silently pass, on a machine without it).
- The only mocked boundary is the LLM network call (`llm_client.extract_page` /
  `verify_mapping`), and every test that does so says so. Time-limit behaviour
  is tested for real: the OpenAI client is pointed at a local server that never
  answers in time, and the call is abandoned within the budget.
- Scan tests use **generated degraded scans with known injected defects** (skew
  angle, noise level, fading, shadow, blur, JPEG damage, 90°/180°/270° rotation;
  `tests/extraction/scan_fixtures.py`), asserting measured recovery — e.g.
  skew estimated within 0.4° of the injected angle — not just "it ran".
- `eval/run_eval.py` makes **no mocks**: it drives the live HTTP API and scores
  real output against `eval/gold_labels.json`. It spends real API credit, so it
  is only run deliberately.

**Measured results** (from actual runs on the fixtures; floors in the tests are
set below these):

| Path | Result |
|---|---|
| Digital PDFs, local parser (8 synthetic + the real multi-column Apollo report) | 47/47 gold values (100%), zero API calls |
| Degraded scans, **no** preprocessing | 0% of values read |
| Same degraded scans, **with** preprocessing | 92% |
| Clean scans (OCR digit misreads such as `130`→`180` remain) | 88% |
| Real Apollo complex coloured layout, as a scan | 8/12 clean, 5/12 degraded |
| Memory (512 MB Linux container, hard cap, 12 documents incl. 6 large pages) | no OOM kills; anonymous memory plateaus ≈ 293 MB; 126 MB at startup |

Real bugs were found and fixed through failing runs rather than review — see
`docs/ISSUES_AND_SOLUTIONS.md` for the full log (Part 6 covers the fallback,
OCR and validation work).

## Local setup

### Backend

```bash
cd backend
python -m venv venv
venv/Scripts/activate        # source venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env         # set GEMINI_API_KEY, or LLM_ENABLED=false for no-AI mode
uvicorn app.main:app --reload
```

Runs on SQLite by default. The schema is created by Alembic migrations and the
LOINC table seeds on first startup (~62k codes plus ~1.7M aliases; allow
roughly 30 s the first time — subsequent starts are near-instant). Interactive
API docs at `http://127.0.0.1:8000/docs`.

**OCR needs the Tesseract engine** (the Docker image installs it for you). On
Windows: `winget install UB-Mannheim.TesseractOCR`; on Debian/Ubuntu:
`apt install tesseract-ocr`. It is auto-detected on `PATH` and in the usual
install folders, or set `TESSERACT_CMD`. Without it, digital PDFs and plain
text still work; scanned input fails with a clear "OCR engine not installed"
message.

Key settings (all in `backend/.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` / `GEMINI_MODEL` | — / `gemini-3.8-flash` | LLM provider (Gemini, via its OpenAI-compatible endpoint) |
| `LLM_ENABLED` | `true` | `false` = never call the LLM; local pipeline only |
| `LLM_REQUEST_TIMEOUT_SECONDS` / `LLM_PAGE_BUDGET_SECONDS` | `25` / `45` | Per-request cap / total budget incl. retries before diverting to fallback |
| `OCR_ENABLED`, `TESSERACT_CMD`, `OCR_PAGE_TIMEOUT_SECONDS` | `true`, auto, `40` | Local OCR |
| `OCR_MAX_WORKERS` | `1` | Background OCR threads. Keep 1 on 512 MB instances: one large-page OCR peaks ≈ 424 MB |

### Backend tests

```bash
cd backend
pytest -q                    # ~2 minutes; no network, no API credit
```

To write the degraded-scan fixtures to disk for manual/UI testing:
`python backend/scripts/generate_scan_fixtures.py` (output is gitignored).

### Frontend

```bash
cd frontend
python -m http.server 5500
```

Open `http://127.0.0.1:5500`. On localhost it talks to `http://localhost:8000`
(override on localhost only with `?api=http://localhost:8010`); on any other
host it uses the deployed backend URL set at the top of `frontend/js/api.js`.

The UI is a no-build ES-module app. The workspace shows the **original
document** (PDF, DOCX, PNG/JPEG, TXT; viewer libraries are vendored in
`frontend/vendor/`) beside the extracted result cards, with zoom, fit, rotate
and page navigation, a draggable splitter, and a mobile pane switch. While a
report is processing, a live console streams the server's progress feed.

### Full stack with Postgres (Docker)

```bash
GEMINI_API_KEY=your-key docker compose up --build
```

### Regenerating the LOINC reference data

```bash
python backend/scripts/build_loinc_data.py --source "Loinc_2.83/LoincTable/Loinc.csv"
```

### Database migrations

```bash
cd backend
alembic revision --autogenerate -m "describe the change"
alembic upgrade head   # applied automatically at app startup too
```

Current head adds extraction provenance (`extraction_source`, `is_edited`,
`documents.used_fallback`/`fallback_reason`) and validation output
(`validation_notes`, `suggested_value`), plus the live progress feed
(`documents.progress`, `documents.pages_done`).

## Deployment

- **Backend**: Render, via `render.yaml` (Blueprint): a free Postgres instance
  plus the API as a Docker web service. Set `GEMINI_API_KEY` in the Render
  dashboard (`sync: false`, never committed). The image installs Tesseract and
  sets `MALLOC_ARENA_MAX=2` to limit memory fragmentation. First start after a
  schema/seed change takes a couple of minutes (LOINC reseed); later starts are
  fast.
- **Frontend**: Vercel, project root `frontend/` (static, no build step).

## API summary

| Method | Path | Purpose |
|---|---|---|
| POST | `/reports` | Upload one report (multipart); enqueues it (`status=pending`) |
| POST | `/reports/batch` | Upload several at once; all-or-nothing validation |
| GET | `/reports` | List reports + status (`used_fallback` marks no-AI extractions) |
| GET | `/reports/{id}` | Detail + observations + computed quality summary |
| POST | `/reports/{id}/cancel` | Cancel a pending/processing report (cooperative) |
| GET | `/reports/{id}/file` | The original uploaded file (for the side-by-side viewer) |
| GET | `/reports/{id}/fhir` | The report as an HL7 FHIR R4 `Bundle` |
| GET | `/observations?document_id=&mapping_status=&q=` | Query observations across reports |
| POST | `/observations` | Add a row by hand (alias-exact LOINC mapping only; never calls an LLM) |
| GET | `/observations/{id}` | One observation |
| PATCH | `/observations/{id}` | Partial edit; renaming re-derives the code, re-validates the row |
| DELETE | `/observations/{id}` | Remove a row (204) |
| PATCH | `/observations/{id}/review` | Confirm/correct a LOINC mapping (server-validated) |
| GET | `/loinc/search?q=` | Terminology search over the full LOINC table |
| GET | `/health` | Health check |

Each observation includes `extraction_source` (`llm` / `fallback` / `manual`),
`is_edited`, `validation_notes` and `suggested_value`. Full schema at `/docs`.

## Constraints honored

- Never fabricates values or LOINC codes: the extraction prompt forbids it, the
  mapping pipeline returns `unmapped` rather than guessing, review/CRUD inputs
  are validated against the reference table, and the validation layer only
  *suggests* corrections to suspect numbers — it never rewrites them.
- Original values are preserved as strings (including `<0.1`, `Negative`)
  alongside the normalized name.
- `mapping_status` is always populated and actionable, not just visible.
- Every observation stores `document_id` and `page_number`; the original upload
  is retained in the database.
- Adding a new report format doesn't touch mapping or storage — only
  ingestion/extraction or the LOINC data would change.

## Stretch goals attempted

- Terminology search (`/loinc/search` + UI panel); containerized deployment.
- FHIR Observation output (`app/services/fhir_export.py`), as a pure transform
  over mapped rows — edits flow into it.
- Batch processing; human-in-the-loop review; full manual CRUD of results.
- Durable DB-backed job queue with crash recovery and cancellation.
- Automated quality/error detection: document-level flags (`quality.py`) plus
  per-row validation (lost decimals, garbled units, junk rows, confidence).
- Fully local, LLM-free extraction path including layout analysis, image
  clean-up and OCR, with automatic failover and a time-bounded LLM call.
- Not attempted: cross-upload duplicate detection, multilingual reports,
  handwriting recognition (explicitly out of scope).

## Known limitations

- **Fallback accuracy is real but bounded.** OCR still misreads digits, most
  visibly in large coloured values on complex layouts (e.g. `7.15` → `715`). A
  lost decimal is only caught when a reference range was also read; other digit
  errors (e.g. `6` → `1`) cannot be detected. That is why fallback output is
  flagged and editable instead of presented as authoritative.
- Footer-style rows laid out as several label columns above several value
  columns (e.g. the ESR row on the Apollo report) are not paired, and OCR of
  graphics-heavy pages produces some junk rows. The junk filter errs toward
  keeping rows (a junk label that equals a real LOINC abbreviation is kept).
- Scanned **handwriting** is out of scope. Scanned multi-column coloured reports
  are the weakest case.
- OCR concurrency is deliberately 1: memory, not CPU, is the constraint on a
  512 MB instance. Memory was verified in a Linux container (no OOM across 12
  documents); the container's reported peak (≈ 490 MB) includes reclaimable
  file cache.
- The worker is a single in-process thread — correct and durable at this scale,
  but scaling out would need a real broker (Celery/RQ + Redis).
- No LLM escalation for low-confidence local pages yet (it would spend API
  credit; the per-row confidence scores are the intended trigger).
- Single-tenant, no auth — out of scope per the spec.
- `eval/` has no genuinely degraded scans of *real* documents; the degraded
  scans in the tests are generated from clean ones.
