# HealthPilot AI Lab Extraction & LOINC Coding Agent

Internship project submission for HealthPilot.ai. Extracts laboratory results
from lab reports (digital PDF, scanned image, plain text), normalizes test
names, maps each observation to a LOINC code with a confidence/review status,
stores everything in a structured, traceable data store, and exposes it via
API + a simple web UI.

## Architecture

```
                                ┌─────────────────────────┐
   Upload (PDF/PNG/JPG/TXT)     │   FastAPI backend        │
   ─────────────────────────►   │                          │
                                │  1. pdf_utils            │  digital PDF -> text layer + page image
                                │     (PyMuPDF)            │  scanned/image -> page image only
                                │           │              │  plain text -> text only
                                │           ▼              │
                                │  2. gemini_client         │  one multimodal call per page:
                                │     .extract_page()       │  image (+text layer if any) -> strict
                                │           │              │  JSON schema of {test, value, unit,
                                │           ▼              │  range, specimen, method, ...}
                                │  3. normalization +       │  stage 1: deterministic alias/synonym
                                │     loinc_mapping         │  lookup against curated LOINC subset
                                │           │              │  stage 2: embedding similarity search
                                │           │              │  stage 3: LLM re-ranks top-k candidates
                                │           │              │  using specimen/method context, or
                                │           │              │  declares "no reliable match"
                                │           ▼              │
                                │  4. Postgres/SQLite       │  documents -> observations -> loinc_codes
                                │     (SQLAlchemy)          │  every observation keeps document_id +
                                │                          │  page_number for traceability
                                │  5. REST API              │  /reports, /observations, /loinc/search
                                └─────────────────────────┘
                                             ▲
                                             │ CORS fetch
                                ┌─────────────────────────┐
                                │  Static frontend          │  upload form, report list, observation
                                │  (HTML/JS/Tailwind CDN)  │  table with confidence/review badges
                                └─────────────────────────┘
```

## Key engineering decisions (and why, given the time constraint)

**Multimodal LLM as the extraction engine for every input format**, instead of
a separate OCR (Tesseract) + layout-parsing pipeline. Every page — whether
from a digital PDF, a scanned image, or a photo — is rasterized and sent to
Gemini with a strict JSON schema and an explicit "never fabricate, use null if
absent" instruction. The PDF's extractable text layer (when present) is
included as a cross-reference. This collapses what would otherwise be
days of format-specific engineering into one well-tested prompt + schema,
at the cost of per-page API latency/cost and a dependency on the vision
model's OCR quality for genuinely bad scans.

**Three-stage LOINC mapping**, to satisfy the spec's explicit requirement to
avoid "superficial text similarity" and to flag uncertain mappings rather than
guess:
1. Deterministic alias/synonym exact match against a curated reference table
   (free, instant, no model call — e.g. "Hgb"/"SGOT"/"A1C" resolve directly).
2. Embedding similarity search over the same table to shortlist candidates
   for anything the alias table doesn't cover.
3. An LLM re-ranks those candidates using the observation's specimen/method
   context (so e.g. urine glucose is not mapped to serum glucose on name
   similarity alone), and can explicitly return "no reliable candidate" →
   `unmapped`, or a low-confidence pick → `needs_review`.

**Curated LOINC subset (~65 codes) instead of the full official LOINC table.**
The full table requires a licensed account/download from loinc.org; building
that ETL was not the highest-value use of the available time. The curated
subset covers the common panels (CBC, CMP/BMP, lipid, LFTs, thyroid, HbA1c,
coagulation, urinalysis, iron studies, cardiac markers) with real, verified
LOINC codes. The loader (`app/services/loinc_loader.py`) is a plain
JSON→Postgres ETL — swapping in the full LOINC.csv only requires writing an
equivalent loader for that file's columns; nothing else in the pipeline
changes. This is the single biggest known scope-cut in this submission.

**Frontend on Vercel, backend+DB on Render, not both on Vercel.** Vercel's
serverless functions are built for short request/response cycles; this
pipeline's per-page vision calls plus a persistent Postgres connection don't
fit that model well. The static frontend deploys to Vercel; the FastAPI
service (which needs to run continuously) deploys to Render with a managed
Postgres instance. The frontend calls the Render API over CORS.

**Synchronous-per-request background processing (FastAPI `BackgroundTasks`)
instead of a task queue (Celery/RQ).** At this dataset's scale a queue would
be over-engineering; the upload endpoint returns immediately with `status:
pending`, the frontend polls, and the document transitions to
`processing → complete/failed`. Documented as the first thing to swap out
if the volume requirement changed.

## What's real vs. mocked in testing

Per an explicit instruction mid-build: **no test in this repo claims a result
it didn't actually produce.** Concretely:

- `backend/tests/` (25 tests, all passing at last run) exercise real code:
  real PDF generation/parsing via PyMuPDF, a real SQLite database, the real
  FastAPI app via `TestClient`, and the real 3-stage mapping control flow.
  The one thing they cannot exercise without cost/a key is the actual Gemini
  network call — those calls are mocked at that single boundary
  (`gemini_client.extract_page` / `verify_mapping` / `embed_text`), and every
  test that does so says so in its name/docstring.
- `eval/run_eval.py` makes **no mocks at all** — it drives the live HTTP API
  with a real `GEMINI_API_KEY` and scores real model output against
  `eval/gold_labels.json`. `eval/eval_results.md` is only ever generated by
  actually running this script; if that file is missing or stale, the numbers
  it would show are not yet known, not hidden or omitted.
- Real bugs were found and fixed during this build via actual failing test
  runs (not hypothetical review): a `RetryError` wrapping non-retryable
  config/input errors and wasting ~8–24s per failure, a SQLite
  in-memory-per-connection footgun that made background-task tests silently
  look like "document not found", and a would-be crash sending an empty
  image blob to Gemini for plain-text reports. All are fixed and covered by
  a regression test.

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

Runs on SQLite by default (no Postgres needed for local dev). Visit
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
| POST | `/reports` | Upload a report (multipart file); kicks off background processing |
| GET | `/reports` | List uploaded reports + status |
| GET | `/reports/{id}` | Report detail + all its observations |
| GET | `/observations?document_id=&mapping_status=&q=` | Query/filter observations across all reports |
| GET | `/loinc/search?q=` | Terminology search over the curated LOINC subset |
| GET | `/health` | Health check |

Full interactive schema at `/docs` (Swagger) once the backend is running.

## Constraints honored

- Never fabricates values or LOINC codes: the extraction prompt explicitly
  forbids inventing fields, and the mapping pipeline returns `unmapped`
  rather than guessing when no candidate is reliable.
- Original extracted values are preserved as-is (kept as strings, including
  qualifiers like "<0.1" or "Negative") alongside the normalized name.
- `mapping_status` (`confirmed` / `needs_review` / `unmapped`) is a first-class,
  always-populated field, distinguishing confirmed from uncertain mappings
  everywhere (API response, DB, and UI badges).
- Every observation stores `document_id` and `page_number` for source
  traceability, and the original uploaded file is retained in the database.
- Adding a new lab/report format doesn't require touching the mapping or
  storage layers — only `pdf_utils.load_pages` (ingestion) or the curated
  LOINC table (coverage) would need extending.

## Stretch goals attempted

- Terminology search interface (`/loinc/search` + UI panel): done.
- Containerized deployment (Dockerfile + docker-compose): done.
- FHIR Observation output, batch upload, human-in-the-loop review UI,
  duplicate detection, multilingual support: not attempted given the
  timeline — see "Curated LOINC subset" above for the one scope cut that
  was deliberate; these stretch goals were simply lower priority than a
  correct, tested core pipeline within the deadline.

## Known limitations

- LOINC coverage is the curated ~65-code subset, not the full official table
  (see rationale above).
- No genuinely degraded scanned images in the eval set (see `eval/DATASET.md`).
- Background processing is in-process (`BackgroundTasks`), not a durable job
  queue — a crash mid-processing leaves a document in `processing` rather
  than being retried automatically.
- Single-tenant, no auth — out of scope per the spec's focus on the
  extraction/mapping pipeline itself.
