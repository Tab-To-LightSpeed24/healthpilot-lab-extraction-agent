# Interview Prep Notes — HealthPilot AI Lab Extraction & LOINC Coding Agent

Personal study notes: what I built, why, what broke, and how I'd talk about
it in an interview. Written to actually understand the system, not just
recite it.

> **Note on naming:** Parts 1-5 describe events from when the extraction model was Gemini, so they refer to `gemini_client`, `GEMINI_API_KEY` and Gemini quotas. That module is now `llm_client.py`, which talks to Gemini through its OpenAI-compatible endpoint, and the key is `GEMINI_API_KEY` (the model is `GEMINI_MODEL`). The accounts are kept as written because they are historically accurate. Parts 1-8 describe the first build; **§9 describes the current version** (parallel processing, in-memory retrieval, hardening). The committed LOINC CSV now holds ~18k codes (the ~62k figures below are from an earlier, larger table).

---

## 1. The problem, in one paragraph

Lab reports arrive in wildly inconsistent formats — different labs, different
layouts, different abbreviations for the same test ("Hgb" vs "Hemoglobin" vs
"HGB"), sometimes scanned images instead of digital PDFs. The job is to turn
that mess into structured data: pull out each test, its value/unit/range,
and — the hard part — attach the correct LOINC code (the universal ID for
"what lab concept is this, really") so downstream systems can compare data
across labs reliably. The spec was explicit that this must never silently
guess a wrong code; uncertain mappings must be flagged for human review.

## 2. Build timeline (what I actually did, in order)

1. **Read the spec, scoped it against the deadline.** Full LOINC (60k+
   codes), a production OCR pipeline, a review-workflow UI, FHIR output —
   all listed as either core or stretch. Given ~24-31h, I made the call to
   build a smaller, *fully working and tested* core rather than a wider,
   shakier surface. That's the single biggest judgment call in this project
   and the one I'd defend hardest in an interview.
2. **Picked the architecture** (see §3) before writing code — multimodal LLM
   for extraction instead of OCR, three-stage LOINC mapping, Render+Vercel
   split for deployment.
3. **Backend core first**: SQLAlchemy models → curated LOINC reference data
   → normalization/alias matching → mapping pipeline → FastAPI routes. Wrote
   real tests at each stage before moving to the next (see §6 — this
   surfaced several real bugs early, cheaply).
4. **Frontend**: plain HTML/JS, no build step (since rewritten as ES modules
   with precompiled CSS), talks to the API via `fetch`. Deliberately not React — there was no complexity in this UI that
   justified a framework, and it removes an entire toolchain (npm/build/
   bundler) from the deployment risk surface given the deadline.
5. **Evaluation dataset**: wrote a generator script that produces real PDFs/
   PNG/TXT files (not fixtures) covering clean/messy layouts, synonyms,
   missing fields, multi-page, a deliberately-unmappable test, a simulated
   scan, and plain text — with hand-authored gold-standard expected LOINC
   codes to score against.
6. **Deployment**: Render (backend + managed Postgres, via a `render.yaml`
   Blueprint) and Vercel (static frontend). Both failed on first deploy for
   real, unrelated reasons (§5) — fixed by reading actual logs, not guessing.
7. **Live evaluation**: ran the real pipeline against all 10 sample reports
   through the deployed API with real Gemini calls. This is what caught the
   most interesting bug of the whole project (§5, "the urine glucose bug").

## 3. Architecture

```
Upload (PDF/PNG/JPG/TXT)  ->  documents row, status=pending   (the row IS the queue)
   |
   v
worker thread claims it (compare-and-swap), heartbeat, cancel checked per page
   |
   v
pdf_utils.load_pages()      PDF -> text layer + page image; image -> image; text -> text
   |
   +--> PRIMARY   llm_client.extract_page()   Gemini, 1 multimodal call per page, all pages
   |                of a document concurrently; 25s/request, 45s budget; a failure that will repeat
   |                (timeout, credits, key, outage) stops LLM use for the document
   |
   +--> FALLBACK  extraction/fallback.py      no API, no key
                    digital PDF : layout.py (reading order) -> field_parser.py
                    plain text  : field_parser.py
                    scanned/img : preprocessing.py -> ocr.py (Tesseract) -> field_parser.py
                  -> validation.py  (lost decimals, garbled units, junk rows, confidence)
   |
   v
normalization + loinc_mapping    stage 1 alias-exact, stage 2 in-memory retrieval index,
                                 stage 3 one batched LLM call per page (skipped in fallback mode)
   |
   v
Postgres (SQLAlchemy + Alembic)  documents -> observations -> loinc_codes
                                 observation.extraction_source: llm | fallback | manual
   |
   v
FastAPI (/reports, /observations CRUD, /fhir, /cancel, /review)
   --CORS--> static frontend: yellow "lower accuracy" banner, edit/add/delete rows
```

### Why LLM-first, but with a full local fallback?

Originally this section argued for a multimodal LLM *instead of* OCR, and that
is still the highest-quality path for arbitrary layouts. What changed is the
operational reality: a paid, rate-limited API as the *only* way to get output
is a single point of failure, and it failed here (a low-credit account returned
402 on every page). So the LLM is primary but bounded and optional, and a local
pipeline produces output whenever it isn't available. The trade-off is explicit:
fallback output is less accurate, so it is labelled (yellow banner, confidence
below the 0.7 review threshold) and fully editable instead of passed off as
authoritative.

### Why three mapping stages, not just "ask the LLM"?

Because the spec explicitly says: don't map on superficial text similarity,
and flag uncertainty instead of guessing. A single "ask an LLM to pick a
LOINC code" call has no ground truth to check itself against and no way to
say "I'm not sure" reliably. Splitting it:
- **Stage 1 (alias exact-match)** handles the common, unambiguous cases
  deterministically and for free — no model call, no chance of a *wrong*
  answer for something we already know for certain (e.g. "Hgb" → Hemoglobin).
- **Stage 2 (candidate retrieval; originally lexical SQL, now an in-memory index)**
  narrows a huge terminology space to a
  short, relevant candidate list instead of asking the LLM to pick from
  everything.
- **Stage 3 (LLM re-rank with context)** is where specimen/method context
  gets used to disambiguate — e.g. "Glucose" in a urine panel vs a serum
  panel is a *different* LOINC concept, and the LLM is explicitly told not
  to pick on name similarity alone if specimen contradicts it. Critically,
  it can return "no candidate is reliable" — which becomes `unmapped`,
  not a guess.

### Why Render (backend) + Vercel (frontend), not both on one platform?

Vercel's serverless functions are built for short request/response cycles
(seconds). This pipeline's per-page vision calls plus a persistent Postgres
connection don't fit that model — cold starts and execution-time limits
would fight the architecture. Render runs the FastAPI service as a normal
continuously-running container with a managed Postgres instance, which is
the natural fit. The frontend is static (no build step) and deploys
trivially to Vercel, calling the Render API over CORS.

## 4. Database schema

Three tables (SQLAlchemy models in `backend/app/models/`):

**`documents`**
| column | notes |
|---|---|
| id (PK, UUID) | |
| filename, content_type | |
| uploaded_at | |
| num_pages | filled in after ingestion, 0 until then |
| status | enum: pending → processing → complete / failed |
| error_message | populated on failure with the real underlying exception |
| raw_content | the original file bytes, stored directly (bytea) — see §5 for why |

**`observations`** (one row per extracted lab test)
| column | notes |
|---|---|
| id (PK, UUID), document_id (FK), page_number | traceability back to source |
| original_test_name, normalized_test_name | preserves what was printed AND what it resolved to |
| value, unit, reference_range, specimen, method, timing, flag | all nullable — a field not on the report is `null`, never guessed |
| loinc_code, loinc_display | null when unmapped |
| mapping_status | enum: confirmed / needs_review / unmapped — always populated |
| mapping_confidence, mapping_stage, mapping_rationale | which stage decided it and why, for auditability |
| extraction_confidence | the model's own confidence for that row |
| raw_extraction | the full raw JSON the model returned for that test, kept for debugging/audit |

**`loinc_codes`** + **`loinc_aliases`** — the curated reference table (~65
codes covering CBC, CMP/BMP, lipid, LFTs, thyroid, HbA1c, coagulation,
urinalysis, iron studies, cardiac markers), each with a list of known
synonyms/abbreviations for the alias-exact matching stage.

**Design choice**: storing the original file as a DB blob rather than on
disk/S3. Render's free-tier web services have an ephemeral filesystem (wiped
on every restart/redeploy), so anything written to local disk would vanish.
Storing it in Postgres (which persists) was the simplest way to keep
traceability without adding an external object-storage dependency under time
pressure. Trade-off I'd flag proactively: this doesn't scale well for large
files/volumes — S3/GCS + a URL reference is the right long-term answer.

## 5. Problems actually hit, and how I found/fixed them

I want to be able to talk through these concretely, since "what went wrong
and how did you debug it" is the question that actually distinguishes
someone who built this from someone who describes it.

1. **Dependency/platform mismatches (Windows + Python 3.13).** `psycopg2-
   binary`, `PyMuPDF`, and `numpy`'s pinned versions didn't have prebuilt
   wheels for this Python/OS combo — pip tried to compile from source and
   failed (missing `pg_config`, missing Visual Studio, a numpy longdouble
   overflow bug specific to 3.13). Fixed by bumping to versions with
   published wheels and re-verifying by actually installing, not assuming.

2. **SQLite in-memory-per-connection footgun in tests.** Each new DB session
   in a test opened a *separate* empty in-memory SQLite database, since
   `sqlite:///:memory:` is scoped per-connection, not per-process. Test
   failures looked like "document not found" even though the upload had just
   succeeded — the background task's DB session and the test's session were
   silently talking to two different databases. Fixed with SQLAlchemy's
   `StaticPool` so all connections in a test share one connection.

3. **Wasteful retries on non-retryable errors.** A missing API key or a bad
   function call (`ValueError`) was getting wrapped in `tenacity`'s retry
   decorator along with genuine transient network errors, so a config
   mistake would silently retry 3× with exponential backoff (~8-24s wasted)
   before finally surfacing. Fixed by explicitly excluding config/input
   errors from the retry policy — only real transient errors get retried.

4. **Text-only reports would have crashed.** Plain `.txt` uploads have no
   rasterized page image (`image_png == b""`), but the extraction call
   always built a request with an image part. Caught before it shipped by
   actually running a generated `.txt` fixture through the real ingestion
   code and inspecting the object, not just assuming the "multi-format"
   claim held.

5. **Two real production deploy failures** (Render), diagnosed from actual
   logs the user pasted, not guesswork:
   - `pydantic-settings` JSON-decodes environment variables for list-typed
     settings fields. `CORS_ORIGINS=*` isn't valid JSON, so the app crashed
     on startup. Fixed by keeping the field as a plain string and splitting
     it into a list in code, instead of typing it `list[str]` directly.
   - Render's Postgres connection string can use the legacy `postgres://`
     scheme; SQLAlchemy 2.x's driver lookup only recognizes `postgresql://`
     and raises `NoSuchModuleError` otherwise. Fixed with a small
     normalization function (and a regression test), applied proactively
     once I recognized the pattern, before it could bite in a different
     environment.

6. **Wrong Gemini model name.** `gemini-2.0-flash` returned `NotFound` for
   the live API key (model naming/availability had shifted). This one I
   couldn't diagnose from first principles — needed the real error from a
   live call. Switched to `gemini-2.5-flash`.

7. **The most interesting bug — "the urine glucose bug", found by the real
   evaluation run, not by code review:** A urinalysis report listed
   "Specimen: Urine" once as a section header, with individual rows below it
   (Glucose, Protein, etc.) that didn't repeat the word "Urine" on each line.
   My extraction prompt asked the model to fill in `specimen` for each row,
   but never told it to *propagate* a panel-level header down to rows that
   don't restate it — so `specimen` came back `null` for those rows, and the
   downstream LLM mapping stage (which is explicitly told "don't map on name
   similarity alone if specimen contradicts it") had no specimen signal to
   work with, and defaulted to the more common serum Glucose LOINC code
   instead of the urine one. This is *exactly* the failure mode the spec
   warns against, and it only showed up when I actually ran real documents
   through the real deployed pipeline and checked the output against known-
   correct answers — a code review would not have caught this, because
   every individual function was working "correctly" in isolation. Fixed by
   explicitly instructing the extraction prompt to copy section/panel-level
   specimen context onto every row it covers, plus added "Sugar"/"Blood
   Sugar" as recognized synonyms for Glucose (a second, smaller gap the same
   run surfaced).

**The meta-point for the interview:** almost every one of these was found by
actually running something — a test, a real deploy, a real API call against
real sample data — not by reasoning about the code. That's the workflow I'd
describe if asked "how do you make sure AI-assisted or LLM-based systems are
actually correct."

## 6. Testing philosophy

No test in this repo claims a result it didn't actually produce.
- `backend/tests/` (359 tests at the latest run; 29 in the first build) exercise real code: real PDFs generated and
  parsed via PyMuPDF, a real SQLite DB, the real FastAPI app via
  `TestClient`, and the real 3-stage mapping control flow. The *only* thing
  mocked is the actual Gemini network call (one clearly-named boundary),
  because hitting the real API on every test run would be slow, flaky, and
  cost money for no additional signal about *our* code's correctness.
- `eval/run_eval.py` makes zero mocks — it drives the live deployed API with
  a real Gemini key and scores real model output against hand-authored gold
  labels (`eval/gold_labels.json`). `eval/eval_results.md` is only ever
  produced by actually running it.
- Real numbers from the first full live run (before the urine-glucose fix):
  **100% extraction accuracy, 93.3% LOINC mapping accuracy** across 9/10
  documents (1 hit a Gemini free-tier rate limit, unrelated to the code).
  The two mapping misses in that run *were* the urine-glucose bug and one
  missed synonym — i.e., the eval caught real, fixable issues rather than
  rubber-stamping a pass.

## 6b. Added after the initial build: FHIR Observation output

Originally scoped out (see §2/§8) as lower priority than a correct, tested
core pipeline. Added afterward once the core was live and evaluated, as a
pure transform layer (`app/services/fhir_export.py`,
`GET /reports/{id}/fhir`) over `Observation` rows that were *already*
mapped by the 3-stage pipeline — it reads already-resolved data and
reformats it into an HL7 FHIR R4 `Bundle`, so it cannot introduce a new
mapping error, only a serialization bug (which is exactly the class of bug
its own tests target: e.g. a value like `"<0.1"` must become a
`valueString`, never be silently truncated into a bare, misleading
`valueQuantity` of `0.1`). Mapping status/confidence ride along as FHIR
`extension` entries so an `unmapped` observation still exports honestly
(no invented LOINC coding) instead of being dropped or faked. This is a good
example to bring up if asked "what did you add after the first working
version" — it shows iterating on a live system rather than a single
one-shot build.

## 7. Known limitations (say these proactively, don't wait to be asked)

- **Fallback accuracy is bounded.** OCR still misreads digits, mainly large
  coloured values on complex layouts. A dropped decimal is caught only when the
  reference range was also read; other digit errors can't be detected. Measured:
  degraded scans 92% with preprocessing (0% without), clean scans 88%, the real
  multi-column Apollo report as a scan 8/12 clean and 5/12 degraded.
- Footer rows with several label columns over several value columns (e.g. ESR
  on the Apollo report) aren't paired; graphics-heavy scans produce some junk
  rows. The junk filter deliberately errs toward keeping rows.
- Handwriting is out of scope.
- OCR concurrency is 1 on purpose: memory (a large-page OCR peaks ~424 MB of a
  512 MB instance) is the constraint, verified in a Linux container.
- In-process worker threads over a database queue (a real broker would be needed
  to scale across machines).
- No LLM escalation for low-confidence local pages yet (it spends API credit).
- Single-tenant; only an optional shared API key protects the API (no accounts,
  audit log or encryption at rest).
- Original file stored as a DB blob, not object storage.

## 8. Questions I'd expect, and how I'd answer

**"Why didn't you use a smaller LOINC subset?"** I did initially (~65 codes),
then replaced it with the full ~62k Laboratory/ACTIVE table. That exposed real
ambiguity in the official data (a bare "Hgb" is a synonym for three different
codes), which is why only unambiguous aliases auto-resolve and a small
hand-verified override file covers the common cases.

**"How do you know the LLM doesn't hallucinate a lab value?"** The
extraction prompt explicitly forbids inventing fields and instructs the
model to return `null`/omit rather than guess; the mapping pipeline returns
`unmapped` rather than guessing a code when no candidate is reliable. Beyond
prompting, the eval run is the actual check — I scored real output against
known-correct answers rather than trusting the prompt's instructions alone.

**"What would you do with another week?"** Escalate only low-confidence local
pages to the LLM (the per-row confidence scores are the trigger), collect real
degraded scans to decide whether a second OCR engine is justified, and pair the
multi-column footer rows. I'd also add cross-upload duplicate detection.

**"What happens when the AI provider is down?"** The call is bounded (25 s per
request, 45 s total); the first failure that will repeat stops LLM use for that
document; the rest is extracted locally and clearly flagged. Setting
`LLM_ENABLED=false` runs with zero API calls. Users can correct any row.

**"Walk me through what happens when I upload a file."** → walk the
architecture diagram in §3, end to end, naming the actual function at each
step (`load_pages` → `extract_page` or the `FallbackExtractor` → `validate_tests` →
`map_observation` → `Observation` row → API response).

## 9. Current version: speed, reliability and hardening (latest work)

**Why it changed.** A real 12-page report took minutes: pages were processed one
after another, every unmatched test ran slow SQL `ILIKE '%word%'` scans over the
alias table and then its own LLM call. The target was seconds, accepting a small
accuracy trade-off.

**What was done, in the order it was measured:**
1. *Pages in parallel* (`pipeline.py`): all pages are sent to Gemini at once
   (`LLM_MAX_CONCURRENCY`); only the main thread touches the database. Measured on a
   real 12-page document: still ~117 s, because LOINC matching was ~99 s of it.
2. *Fast matching* (`retrieval.py`, `loinc_mapping.py`): an in-memory inverted index
   (~1 ms per lookup) replaces the SQL scans, and ONE batched LLM call per page
   replaces one call per row. Matching runs inside each page's task. Measured:
   **~12 s** for the same document.
3. *Tuning from real runs*: batch replies were truncated JSON because the thinking
   model ran out of output budget, so the budget was raised,
   `GEMINI_REASONING_EFFORT=low` set, and the parser made lenient with a retry.
   An Alembic `fileConfig` call was silently disabling every application logger at
   startup (fixed with `disable_existing_loggers=False`), which had hidden the real
   error.

**Reliability fixes from production use:**
- A multi-line reference range (~170 chars) overflowed `VARCHAR(128)` on Postgres and
  failed a whole page (SQLite doesn't enforce lengths, so tests had passed). Columns
  were widened (`reference_range` → `TEXT`), over-long free text is clipped on write,
  and API input limits follow.
- Documents could stick in "processing"/"Cancelling" if the process died. Cancel is now
  checked every second while waiting on the AI; a document whose worker is gone is
  cancelled immediately; orphaned jobs are requeued after a restart (45 s) or by a
  periodic sweep (3 min), using a heartbeat refreshed every few seconds.
- Several documents run at once (`WORKER_CONCURRENCY`), bounded by global caps on LLM
  requests and OCR, one-document-at-a-time page rendering, and single-flight, compact
  builds of the LOINC lookup tables (cold-start peak memory 375 MB → 179 MB measured).

4. *Cheaper page preparation and fewer round trips* (from reading production feeds): opening
   a 19-page PDF rendered 10 MB of 200-DPI PNGs before anything else could start (31 s on the
   free Render CPU) and a global render lock made small documents wait behind it. Pages now
   render lazily; text-rich digital pages go to the model as text only, the rest as a 150-DPI
   JPEG (`LLM_PAGE_INPUT`); progress commits are throttled; AI LOINC picks with confidence ≥ 0.9
   and human reviews are remembered (`learned_mappings`) so repeats skip the AI. Measured
   (`docs/BENCHMARKS.md`): page preparation 4.80 → 0.38 CPU-seconds and 17.4 → 0.2 MB uploaded for
   the six benchmark documents; a real six-document run finished in 51 s with ~95% of rows
   matching the previous version.

**Hardening** (`app/core/security.py`): optional shared API key, per-client rate limits,
security headers, request ids, log redaction and a prompt-injection guard — a stopgap
until real per-user auth.

**Observability for users:** the live console streams the progress feed while a
document processes; when it ends, the header shows "Processed in X s"
(`documents.processing_seconds`).

**Honest limits:** the speed figures come from a fast machine; the
Render free tier is far slower per CPU. Accuracy under the faster mapping is only
indicatively measured (gold set of 40 rows: all 30 alias rows correct; for the other
10 the right code was always among the 6 candidates sent to the LLM).
