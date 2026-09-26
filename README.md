# HealthPilot AI Lab Extraction & LOINC Coding Agent

Internship project submission for HealthPilot.ai. Extracts laboratory results
from lab reports (digital PDF, scanned image, plain text), normalizes test
names, maps each observation to a LOINC code with a confidence/review status,
stores everything in a structured, traceable data store, and exposes it via
API + a web UI — including a human-in-the-loop review action, batch upload,
and mid-job cancellation.

## Architecture

```
                                ┌───────────────────────────┐
   Upload (PDF/PNG/JPG/TXT)     │   FastAPI backend          │
   ─────────────────────────►   │                            │
   single or batch               │  1. Enqueue                │  status=pending row is the queue
                                │     (documents table)      │  entry itself -- no broker needed
                                │           │                │
                                │           ▼                │
                                │  2. Worker thread           │  polls for pending docs, claims one
                                │     (worker.py)             │  (CAS update), recovers jobs stuck
                                │           │                │  mid-run after a crash/restart
                                │           ▼                │
                                │  3. pdf_utils (PyMuPDF)     │  digital PDF -> text + page image
                                │           │                │  scanned/image -> page image only
                                │           ▼                │  plain text -> text only
                                │  4. gemini_client           │  one multimodal call per page;
                                │     .extract_page()         │  checks cancel_requested between
                                │           │                │  pages (cooperative cancellation)
                                │           ▼                │
                                │  5. normalization +         │  stage 1: alias-exact match against
                                │     loinc_mapping           │  the full ~62k-code LOINC table +
                                │           │                │  a small human-verified override list
                                │           │                │  stage 2: ranked lexical DB search
                                │           │                │  stage 3: LLM re-rank w/ specimen
                                │           │                │  context, or "no reliable match"
                                │           ▼                │
                                │  6. Postgres (SQLAlchemy    │  documents -> observations ->
                                │     + Alembic migrations)   │  loinc_codes/loinc_aliases; every
                                │                            │  observation keeps document_id +
                                │                            │  page_number for traceability
                                │  7. REST API                │  /reports, /observations, /loinc,
                                │                            │  /reports/{id}/fhir, /cancel, /review
                                └───────────────────────────┘
                                             ▲
                                             │ CORS fetch
                                ┌───────────────────────────┐
                                │  Static frontend            │  upload (single/batch), report list,
                                │  (HTML/JS/Tailwind CDN)    │  clinical + FHIR views, cancel button,
                                │                            │  inline review action, quality flags
                                └───────────────────────────┘
```

## Key engineering decisions

**Multimodal LLM as the extraction engine for every input format**, instead of
a separate OCR (Tesseract) + layout-parsing pipeline. Every page — whether
from a digital PDF, a scanned image, or a photo — is rasterized and sent to
Gemini with a strict JSON schema and an explicit "never fabricate, use null if
absent" instruction. The PDF's extractable text layer (when present) is
included as a cross-reference. This collapses what would otherwise be
days of format-specific engineering into one well-tested prompt + schema,
at the cost of per-page API latency/cost and a dependency on the vision
model's OCR quality for genuinely bad scans.

**The full official LOINC table (~62k Laboratory/ACTIVE codes), not a small
curated subset.** Downloaded from loinc.org under its own license, filtered
by `scripts/build_loinc_data.py` down to Laboratory-class, ACTIVE codes, and
committed as a derived ~26MB CSV (the raw ~1GB release itself is gitignored).
At this scale, a bare abbreviation like "Hgb" turns out to be a genuinely
ambiguous key in LOINC's own data — it's an official synonym for routine
Hemoglobin *and* a rare Hemoglobin-A-by-electrophoresis assay *and* MCHC.
`normalization.build_alias_index()` only auto-resolves an alias when it's
unambiguous in the real data; where it isn't, a small hand-verified override
file (`app/data/loinc_alias_overrides.json`, ~30 entries) supplies the
correct answer for extremely common cases — deliberately excluding anything
specimen-dependent (bare "Glucose"/"Protein"), which must still go through
context-aware mapping so serum vs. urine resolves correctly.

**Three-stage LOINC mapping**, to satisfy the spec's explicit requirement to
avoid "superficial text similarity" and to flag uncertain mappings rather than
guess:
1. Deterministic alias-exact match (free, instant, no model call) against the
   full table + override list above.
2. A ranked lexical search directly against the database for anything stage 1
   doesn't resolve. (An earlier embedding-similarity design was dropped:
   embedding all ~62k candidate codes via the Gemini API would mean tens of
   thousands of extra calls just to build a cache — not viable on the
   available quota. Each token match is ordered by LOINC's own
   `COMMON_TEST_RANK` before any limit is applied, so a common test isn't
   buried under hundreds of loosely-related specialized variants.)
3. An LLM re-ranks the shortlisted candidates using the observation's
   specimen/method context (so e.g. urine glucose is not mapped to serum
   glucose on name similarity alone), and can explicitly return "no reliable
   candidate" → `unmapped`, or a low-confidence pick → `needs_review`.

**A DB-backed durable job queue instead of Celery/RQ + Redis.** The
`documents.status` row *is* the queue entry — uploading just inserts a
`pending` row; a background worker thread polls, claims one at a time (a
compare-and-swap update, safe even if a second worker were ever added), and
processes it. A heartbeat (`updated_at`) lets a startup sweep detect a job
that was mid-`processing` when the process died and requeue it automatically
— the crash-recovery gap a plain `BackgroundTasks` approach has. A real
message broker would be new infrastructure and a new failure mode to
provision for no durability benefit not already covered by using the
database itself, at this project's scale (a single instance, low volume).
The trade-off made explicitly: this doesn't coordinate multiple worker
*processes* over a broker — nothing about the current deployment needs that
either.

**Cooperative mid-job cancellation.** `POST /reports/{id}/cancel` sets a flag
the worker checks between pages, so a runaway multi-page document can be
stopped without killing the process. This exists because of a real incident
during manual testing: a 19-page real-world report burned through the
remaining Gemini free-tier quota partway through, with no way to stop it —
this closes that gap directly.

**Human-in-the-loop review as a real, validated action, not just a read-only
badge.** `PATCH /observations/{id}/review` lets a reviewer confirm or correct
a `needs_review`/`unmapped` observation. A supplied LOINC code is validated
against the reference table server-side — the same "never fabricate a code"
rule the automated pipeline follows applies here too — or a reviewer can
explicitly confirm "no code applies," which is a distinct, deliberate outcome
from the system never having found one.

**Frontend on Vercel, backend+DB on Render, not both on Vercel.** Vercel's
serverless functions are built for short request/response cycles; this
pipeline's per-page vision calls, a persistent Postgres connection, and a
long-lived worker thread don't fit that model well. The static frontend
deploys to Vercel; the FastAPI service deploys to Render with a managed
Postgres instance. The frontend calls the Render API over CORS.

**Alembic migrations, not `create_all()`.** Schema changes (e.g. adding
`common_test_rank`, or the queue/cancellation columns) are versioned
migrations. Adopting Alembic mid-project onto an already-deployed database
needed one specific bootstrap step: a database with tables but no
`alembic_version` history gets stamped at the migration matching its actual
existing schema before upgrading, instead of either crashing (trying to
recreate existing tables) or silently skipping real pending changes.

## What's real vs. mocked in testing

Per an explicit instruction mid-build: **no test in this repo claims a result
it didn't actually produce.** Concretely:

- `backend/tests/` (63 tests, all passing at last run) exercise real code:
  real PDF generation/parsing via PyMuPDF, a real SQLite database seeded with
  the real ~62k-code LOINC table, the real FastAPI app via `TestClient`, the
  real 3-stage mapping control flow, the real queue-claim/recovery logic, and
  the real cancellation/review/batch endpoints. The one thing they cannot
  exercise without cost/a key is the actual Gemini network call — those calls
  are mocked at that single boundary (`gemini_client.extract_page` /
  `verify_mapping`), and every test that does so says so in its name/docstring.
- `eval/run_eval.py` makes **no mocks at all** — it drives the live HTTP API
  with a real `GEMINI_API_KEY` and scores real model output against
  `eval/gold_labels.json`.
- Real bugs were found and fixed during this build via actual failing runs,
  not hypothetical review — among others: a `RetryError` wrapping
  non-retryable config/input errors and wasting ~8–24s per failure; a SQLite
  in-memory-per-connection footgun; a crash sending an empty image blob to
  Gemini for plain-text reports; `pydantic-settings` JSON-decoding a
  list-typed `CORS_ORIGINS` env var and crashing Render's deploy; a legacy
  `postgres://` URL scheme SQLAlchemy 2.x rejects; a wrong Gemini model name
  caught only by a live call; a specimen-context-loss bug that mapped urine
  tests to serum LOINC codes, caught by the live evaluation run; a naive
  full-table alias match that picked MCHC over Hemoglobin for "Hgb"; and a
  naive lexical search that could bury the correct common test under
  hundreds of loosely-related specialized variants. All are fixed and
  covered by a regression test.

## Local setup

### Backend

```bash
cd backend
python -m venv venv
venv/Scripts/activate        # source venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env         # then fill in GEMINI_API_KEY
uvicorn app.main:app --reload
```

Runs on SQLite by default (no Postgres needed for local dev) — schema is
created via Alembic migrations, and the LOINC table auto-seeds on first
startup (~62k rows; takes a few seconds). Visit
`http://127.0.0.1:8000/docs` for the interactive OpenAPI docs.

### Backend tests

```bash
cd backend
pytest -v
```

### Frontend

```bash
cd frontend
python -m http.server 5500
```

Open `http://127.0.0.1:5500`, paste the backend URL into the "Backend API
URL" box (top right) and Save.

### Full stack with Postgres (Docker)

```bash
GEMINI_API_KEY=your-key docker compose up --build
```

### Regenerating the LOINC reference data

If a newer LOINC release is downloaded from loinc.org:

```bash
python backend/scripts/build_loinc_data.py --source "Loinc_2.83/LoincTable/Loinc.csv"
```

### Database migrations

```bash
cd backend
alembic revision --autogenerate -m "describe the change"
alembic upgrade head   # applied automatically at app startup too
```

## Deployment

- **Backend**: Render, via `render.yaml` (Blueprint). Provisions a free
  Postgres instance and the API as a Docker web service. Set `GEMINI_API_KEY`
  in the Render dashboard (marked `sync: false` in the blueprint so it's
  never committed).
- **Frontend**: Vercel, pointed at `frontend/` as the project root (static,
  no build step). After the Render backend is live, open the deployed
  frontend and set its "Backend API URL" to the Render URL.

## API summary

| Method | Path | Purpose |
|---|---|---|
| POST | `/reports` | Upload a single report (multipart file); enqueues it (`status=pending`) |
| POST | `/reports/batch` | Upload multiple reports at once; all-or-nothing validation |
| GET | `/reports` | List uploaded reports + status |
| GET | `/reports/{id}` | Report detail + observations + computed quality summary |
| POST | `/reports/{id}/cancel` | Cancel a pending/processing report (cooperative, checked between pages) |
| GET | `/reports/{id}/fhir` | The report's observations as an HL7 FHIR R4 `Bundle` |
| GET | `/observations?document_id=&mapping_status=&q=` | Query/filter observations across all reports |
| PATCH | `/observations/{id}/review` | Human-in-the-loop: confirm or correct a mapping (server-validated) |
| GET | `/loinc/search?q=` | Terminology search over the full LOINC reference table |
| GET | `/health` | Health check |

Full interactive schema at `/docs` (Swagger) once the backend is running.

## Constraints honored

- Never fabricates values or LOINC codes: the extraction prompt explicitly
  forbids inventing fields, the mapping pipeline returns `unmapped` rather
  than guessing when no candidate is reliable, and the human-review endpoint
  validates any manually-entered code against the reference table too.
- Original extracted values are preserved as-is (kept as strings, including
  qualifiers like "<0.1" or "Negative") alongside the normalized name.
- `mapping_status` (`confirmed` / `needs_review` / `unmapped`) is a first-class,
  always-populated field, distinguishing confirmed from uncertain mappings
  everywhere (API response, DB, and UI badges) — and is now actionable, not
  just visible, via the review endpoint.
- Every observation stores `document_id` and `page_number` for source
  traceability, and the original uploaded file is retained in the database.
- Adding a new lab/report format doesn't require touching the mapping or
  storage layers — only `pdf_utils.load_pages` (ingestion) or the LOINC
  reference data (coverage) would need extending.

## Stretch goals attempted

- Terminology search interface (`/loinc/search` + UI panel): done.
- Containerized deployment (Dockerfile + docker-compose): done.
- FHIR Observation output (`GET /reports/{id}/fhir`, `app/services/fhir_export.py`):
  done, as a pure transform layer over already-mapped `Observation` rows.
- Batch processing (`POST /reports/batch`): done.
- Human-in-the-loop review workflow (`PATCH /observations/{id}/review` +
  inline UI action): done.
- A durable job queue with crash recovery and mid-job cancellation
  (`app/services/worker.py`): done, as a DB-backed queue rather than a
  message broker — see the rationale above.
- Automated quality/error detection beyond per-row confidence
  (`app/services/quality.py`): a modest but real version — duplicate-test
  detection per page and a document-level low-confidence-extraction flag,
  surfaced in the UI as chips.
- Duplicate observation *detection* across separate document uploads (as
  opposed to within one document, which is covered above), and multilingual
  report support: not attempted given the timeline.

## Known limitations

- No genuinely degraded scanned images in the eval set (see `eval/DATASET.md`).
- The worker is a single in-process background thread, not multiple worker
  processes coordinating over a broker — correct and durable at this
  project's current scale, but would need a real queue (Celery/RQ + Redis or
  similar) to scale beyond one instance.
- Single-tenant, no auth — out of scope per the spec's focus on the
  extraction/mapping pipeline itself.
