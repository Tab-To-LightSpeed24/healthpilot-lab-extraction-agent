# Issues Faced & How They Were Solved

Personal reference for interview prep. This is a technical log of every real
problem hit while building the HealthPilot AI Lab Extraction & LOINC Coding
Agent, why it happened, how it was diagnosed, and what fixed it. Written at
the level of "explain this to another developer," not "explain this to
yourself" — so read it once and you should be able to talk through any of
these unprompted.

> **Note on naming:** Parts 1-5 describe events from when the extraction model was Gemini, so they refer to `gemini_client`, `GEMINI_API_KEY` and Gemini quotas. That module is now `llm_client.py` (OpenRouter, OpenAI-compatible) and the key is `OPENROUTER_API_KEY`. The accounts are kept as written because they are historically accurate.

The overarching theme, if an interviewer asks "what was the hardest part":
**almost nothing here was found by reading code carefully. Nearly everything
was found by running something real** — a test, a real deploy, a real API
call — and watching it fail in a way that a code review would not have
predicted. That's the actual story of this project.

---

## Part 1 — Environment & dependency setup

### 1.1 Pinned dependency versions didn't have wheels for the actual environment

**What happened:** `psycopg2-binary`, `PyMuPDF`, and `numpy`, all pinned to
specific versions in `requirements.txt`, failed to install on Windows +
Python 3.13. `psycopg2-binary` tried to compile from source (missing
`pg_config`), `PyMuPDF` tried to compile from source (missing Visual
Studio), and `numpy` installed but then crashed at import time with an
`OverflowError` in `getlimits.py`.

**Why:** Package maintainers publish prebuilt wheels for specific
Python-version/OS/architecture combinations. Python 3.13 was new enough that
the pinned versions predated wheels being published for it on Windows. Pip's
fallback — compiling from source — needs system-level build tools (a C
compiler, Postgres headers) that aren't installed by default.

**Fix:** Bumped each package to the next version that does publish a
Windows/Python 3.13 wheel (`psycopg2-binary==2.9.10`, `PyMuPDF==1.28.2`,
`numpy==2.1.3`), verified by actually running `pip install` and confirming
success, not by assuming a version bump would work.

**What this shows in an interview:** you don't guess about dependency
compatibility — you check by installing, and you fix root cause (get a real
wheel) rather than a workaround (install build tools, pin an old numpy that
happens to avoid the crash).

---

## Part 2 — Correctness bugs in the extraction/mapping pipeline

These were caught by tests and, later, by live evaluation runs against real
Gemini calls — not by reading the code.

### 2.1 Retrying errors that can never succeed on retry

**What happened:** A missing `GEMINI_API_KEY` (a configuration error) got
wrapped in the same `tenacity` retry decorator as genuine transient network
errors. Every failed call retried 3 times with exponential backoff
(2s → 4s → 8s), wasting up to ~24 seconds per failure for an error that was
never going to resolve itself.

**Why:** The retry decorator was applied broadly (`@retry(...)` on the whole
function) without distinguishing "this specific call failed due to network
flakiness, try again" from "this call can never succeed because the
configuration is wrong."

**Fix:** Introduced a `ConfigurationError` exception class and used
`tenacity`'s `retry_if_not_exception_type` to exclude it (and, later,
`ValueError` for bad-input cases) from the retry policy. Caught a second
instance of the exact same mistake later (see 2.2) via a dedicated test —
this became a pattern to actively watch for, not a one-off fix.

**What this shows:** retry logic needs to distinguish error *categories*,
not just wrap everything uniformly. This is a common source of wasted
latency in real production systems.

### 2.2 A text-only report would have crashed on first real use

**What happened:** Plain `.txt` file uploads have no rasterized page image
(`pdf_utils.load_pages` correctly returns `image_png=b""` for them), but
`gemini_client.extract_page()` always built its request with an image part
regardless.

**Why:** The function was written and tested against PDF/image inputs
first; the "plain text" code path existed in the ingestion layer but was
never actually exercised end-to-end before this.

**Fix:** Made the image part conditional — only include it if `image_png` is
non-empty, otherwise send a text-only request. Added a unit test that
mocks the Gemini call and asserts the request contents differ correctly
between the two cases (no live API call needed to verify this).

**What this shows:** an untested code path is not a working code path, even
if it looks correct. This was caught by generating a real `.txt` fixture and
running it through the real ingestion function, not by reading the code.

### 2.3 The "urine glucose" bug — mapped to the wrong LOINC code entirely

**What happened:** A urinalysis report's "Glucose" line, extracted with
`specimen=null` because the report only stated "Specimen: Urine" once as a
section header (not repeated on every row), got mapped to the *serum*
Glucose LOINC code instead of the *urine* one.

**Why:** Two compounding issues. First, the extraction prompt asked the
model to fill in `specimen` per test row but never told it to propagate a
panel-level header down to rows that don't repeat it. Second, without a
specimen value, the downstream LLM mapping stage — which is explicitly
instructed "don't map on name similarity alone if specimen contradicts it"
— had no specimen signal to actually use, so it defaulted to the far more
common serum interpretation.

**Fix:** Rewrote the extraction prompt to explicitly instruct: copy a
section/page-level specimen context onto every row it covers, even if not
individually restated.

**Why this one matters most:** this is *exactly* the failure mode the
original spec explicitly warned against ("avoid superficial text
similarity"). It wasn't caught by any unit test — every individual function
was behaving "correctly" in isolation (extraction extracted what was
literally on the page; mapping correctly used the context it was given). It
was only caught by running a real document through the real, live pipeline
and checking the output against a known-correct answer. **This is the
strongest single example in this project of why evaluation against real
data matters more than unit tests for an LLM-based system** — unit tests
verify your code does what you told it to; only real evaluation tells you
if what you told it to do was actually right.

---

## Part 3 — Scaling the LOINC table from ~65 curated codes to the real ~62k official table

This transition surfaced several real, non-obvious data-quality issues that
only exist at scale.

### 3.1 Common abbreviations are genuinely ambiguous in the official data

**What happened:** After loading the full official LOINC table, the
deterministic alias-matching stage started resolving "Hgb" to the wrong
code. Investigating showed *why*: the official data legitimately lists
"Hgb" as a synonym for **three** different codes — routine Hemoglobin, a
rare Hemoglobin-A-by-electrophoresis assay, and MCHC.

**First attempted fix (and why it was wrong):** Tried breaking ties using
LOINC's own `COMMON_TEST_RANK` field (how often a code is actually ordered,
lower = more common) — pick whichever tied candidate is more commonly
ordered. This picked **MCHC** over routine Hemoglobin for "Hgb", because
MCHC happened to have a slightly lower (= more common) rank in the
aggregate data. `COMMON_TEST_RANK` measures *overall popularity of a code*,
not *which code a specific alias actually refers to* — those are different
things, and conflating them produced a confident, wrong answer.

**Actual fix:** When an alias is ambiguous across multiple genuinely
distinct codes with no reliable way to disambiguate at this stage, **drop
it from the deterministic index rather than guess**. It falls through to
the later stage that has the actual observation's specimen/value/unit
context to disambiguate correctly. A small, separately-maintained,
human-verified override list (`loinc_alias_overrides.json`) supplies the
correct answer for the ~30 most common cases (Hgb, WBC, SGOT/SGPT, etc.) —
deliberately excluding anything specimen-dependent (bare "Glucose",
"Protein"), since those need per-observation context, not a fixed answer.

**What this shows:** this is a real example of trying a plausible-sounding
heuristic, testing it, finding it produces a *worse* failure mode than not
having it, and reverting to a more conservative design. That's a stronger
interview story than "it worked the first time" — it shows you verify your
own assumptions rather than trusting that a reasonable-sounding idea is
automatically correct.

### 3.2 A naive "search the database" fallback can bury the right answer

**What happened:** For the ~62k-code table, "Glucose" alone matches hundreds
of specialized/rare test variants. The lexical search stage's per-token
`SELECT ... LIKE '%glucose%' LIMIT 150` returned whatever 150 rows the
database happened to return first (no explicit ordering) — which could
mean the single correct routine test ("Glucose in Serum or Plasma") never
even made it into the candidate list the LLM was asked to choose from.

**Fix:** Ordered each token's match by `COMMON_TEST_RANK` (ascending, so
commonly-ordered tests come first) *before* applying the row limit. This is
the same field misused in 3.1, but used correctly here — as a tiebreaker
*within* an already lexically-relevant shortlist, not as an override for a
specific alias decision. Verified with a real measurement: before the fix,
the routine serum Glucose code was completely absent from the top-6
candidates; after, it ranked first.

**What this shows:** the same data field can be the right tool for one
problem and the wrong tool for an adjacent-looking one — the distinction
is subtle (ranking vs. deciding) and worth being able to articulate clearly.

### 3.3 Embedding-based semantic search was abandoned for a hard practical reason

**Original design:** stage 2 of the mapping pipeline used embedding
similarity search (precompute an embedding for every LOINC candidate,
compare cosine similarity to the observation).

**Why it was dropped:** at 62k codes, that means ~62,000 individual Gemini
embedding API calls just to build a cache, before a single document is even
processed. On a free-tier API quota (a real, hard constraint on this
project), that's not just slow — it's not viable at all.

**Replacement:** a token-overlap lexical search directly against the
database (see 3.2 for the tuning needed to make it actually work well).
Zero additional API calls, and — combined with the alias table now covering
the large majority of common cases — the LLM re-ranking stage (stage 3)
still supplies the clinical judgment that a pure string-match approach on
its own would lack.

**What this shows:** a "better" approach on paper (semantic search) isn't
better if it's not viable under your actual constraints (API quota, time).
Recognizing and stating that tradeoff explicitly is a sign of engineering
judgment, not a compromise to hide.

---

## Part 4 — Test infrastructure had to change shape when the data got bigger

### 4.1 Re-seeding 62k rows per test made the suite unusably slow

**What happened:** The original test fixture created a brand-new database
and called `seed_loinc_table()` fresh for *every single test*. Fine at ~65
hand-curated rows; at 62k rows plus several hundred thousand alias rows, the
test suite went from seconds to minutes, and a background-thread test
timed out entirely.

**Fix:** Restructured the fixture to seed the LOINC table **once per test
session** (a session-scoped engine), and give each individual test an
isolated SQL transaction that's rolled back afterward — so tests still
can't see each other's inserted `Document`/`Observation` rows, but nobody
pays the cost of re-seeding reference data that never changes between
tests.

**What this shows:** test infrastructure design has to scale with your data,
just like production code does. This wasn't a bug in the strict sense — the
old fixture was "correct" — but it stopped being *practical*, and
recognizing that distinction (correct vs. usable) is important.

### 4.2 SQLite in-memory databases are per-connection, not per-process

**What happened:** An early test using `sqlite:///:memory:` failed with
"document not found" immediately after a successful upload — looked like a
logic bug in the upload endpoint.

**Why:** Each new connection to `sqlite:///:memory:` gets its own separate,
empty in-memory database. The test's request-handling code and the
background-task code were opening *different* connections, and so were
talking to two different, unrelated empty databases.

**Fix:** Used SQLAlchemy's `StaticPool`, which forces all connections in a
test to share the exact same underlying connection.

**What this shows:** an error message ("not found") can point you toward
completely the wrong layer of the system if you don't understand the tool's
specific quirks (this is a well-known SQLite gotcha, not a Healthpilot-
specific one) — worth recognizing quickly rather than assuming your own
business logic is wrong first.

---

## Part 5 — The production deploy crash: a five-act diagnostic story

This is the single most involved debugging session in the project and the
best one to be able to narrate confidently — it demonstrates methodical
elimination of hypotheses under real time pressure, not a lucky guess.

**The setup:** after a large backend change (moving to the full LOINC
table, adding Alembic migrations, adding a background job queue), a deploy
to Render started crashing with `Exited with status 3` — and, critically,
**no Python traceback at all** in most of the failures. A crash with a
traceback is a puzzle; a crash with *no* traceback is a mystery, because
your normal tools (reading the stack trace) don't work.

### Act 1 — Missing files in the Docker image

The first crash *did* have a traceback: `Path doesn't exist: '/app/alembic'`.
The `Dockerfile` only ever copied the `app/` directory into the image, never
`alembic/` or `alembic.ini` — so the migration runner had no migration
scripts to actually run. **Fix:** added `COPY alembic ./alembic` and
`COPY alembic.ini .`. Straightforward once visible.

### Act 2 — A second connection path that had never been tested against real Postgres

The *next* crash had no traceback — just silence, a few seconds after
"stamping" the migration baseline. Reasoning: a clean Python exception
always produces a traceback (as Act 1 did); getting nothing suggests
something lower-level. The prime suspect: Alembic was building its own
brand-new database connection from a URL string that had been serialized
via `str(engine.url)` and round-tripped through Python's `ConfigParser` — a
code path that had genuinely never run against the real production
database before this exact migration. If Render's auto-generated Postgres
password contains characters that don't survive that round-trip cleanly,
this is exactly where it would silently break.

**Fix:** made Alembic reuse the *same* live database connection the rest of
the app already connects with successfully (via Alembic's documented
`Config.attributes["connection"]` pattern), eliminating the second,
untested connection path entirely.

**Verification:** this fix genuinely worked — the next deploy's logs showed
all three pending migrations complete successfully. (The overall crash
wasn't fixed yet, because there was more than one bug — see Act 3 — but this
confirms the fix was real, not incidental.)

### Act 3 — A real, measured out-of-memory crash

The next failure came with an explicit message from Render this time:
`Out of memory (used over 512Mi)`, right after migrations finished — i.e.,
during LOINC table seeding. Rather than guess at a fix, this was **measured
directly**: instrumented the actual seeding code with `psutil` to track
real peak memory. The result: **~1.98GB peak RSS** for the old
approach (materializing all ~62k code records and a separate flat list of
all ~1.7 million alias rows in memory before a single bulk insert) — nearly
4x Render's free-tier 512MB limit.

**Fix:** rewrote seeding to stream rows directly from the CSV and insert in
chunks of 3,000, so peak memory is bounded by chunk size regardless of table
size. Re-measured after the fix: **~8MB** peak delta above baseline — a
roughly 240x reduction, confirmed with the same measurement methodology,
not assumed.

### Act 4 — Ruling out three more plausible-but-wrong hypotheses, methodically

Even after the OOM fix, the exact same silent, no-traceback crash still
happened — but now on a **different, faster code path** (a database that
was already fully migrated, where the migration step should do almost
nothing). This meant the OOM fix, while real and necessary, wasn't the
*whole* story. Rather than guess again, each remaining plausible cause was
tested and eliminated in turn, using Docker to reproduce the crash locally
against a real Postgres container (Docker had been unavailable earlier in
the project; once it became available, it was the right tool to stop
debugging blind against a cloud platform's opaque logs):

- **Memory, again** — reproduced the exact same crash with a `--memory=512m`
  Docker limit *and* with no memory limit at all. `docker inspect` confirmed
  `OOMKilled: false` both times. Ruled out conclusively, not just assumed.
- **uvloop** (the fast asyncio event loop uvicorn uses by default) —
  reproduced the exact same crash after forcing `--loop asyncio` (the
  plain, non-uvloop implementation). Ruled out.
- **Blocking the event loop** — the working theory for a while was that
  running blocking database/Alembic code directly on uvicorn's async
  event-loop thread was somehow the trigger. Moved the blocking work to a
  background thread via `asyncio.to_thread`. **Still crashed identically.**
  Ruled out.

This is the part of the story worth emphasizing most: **three plausible,
defensible hypotheses in a row, each tested directly rather than assumed,
each ruled out with real evidence.** That's the actual skill being
demonstrated — not guessing right the first time, but never shipping a fix
you haven't verified actually addresses the failure.

### Act 5 — The real root cause, found by forcing a real traceback

With the top-level Python exception handling clearly not the problem, the
next step was tracing the failure at the lowest level available:
`faulthandler` (Python's built-in fatal-crash tracer) plus explicitly
catching `SystemExit` around `uvicorn.run()` itself (not just inside the
app's own code). This finally surfaced it: **`uvicorn.run()` was calling
`sys.exit(3)` itself** whenever the app's startup lifespan raised *any*
exception — including exceptions that aren't a subclass of `Exception`
(uvicorn's own generic "the app didn't start" exit path), which is why nice
`except Exception: logger.exception(...)` blocks written earlier in the
project never printed anything: they were catching the *category* of error
correctly, but the actual failure was being reported through a different,
uvicorn-level path.

Running the exact migration/seeding call directly (bypassing uvicorn
entirely) revealed the real underlying error immediately, no special tools
needed: a **`psycopg2.errors.ForeignKeyViolation`**. The actual root cause
was two ordinary, unglamorous bugs:

1. **`seed_loinc_table()`'s idempotency check was too coarse.** It treated
   "the table has some rows" as "the table is fully, correctly seeded," and
   skipped re-inserting codes on that basis. An *earlier* interrupted
   startup (from the OOM crash in Act 3, before that fix) had left the table
   **partially** seeded — some codes present, most missing. The next
   startup saw a non-empty table, skipped code insertion, then tried to
   insert aliases for codes that had never actually been inserted →
   foreign key violation.
2. **One database column was genuinely too narrow.** `method_type` was
   `VARCHAR(128)`, but real LOINC data has method descriptions up to 134
   characters (verified against the *entire* dataset, not just the one
   value that happened to fail first, so a future LOINC release with an
   even longer value fails a local test instead of a live deploy).

**Fixes:** made seeding always fully delete-and-reinsert both tables from a
clean slate (cheap now that it streams/chunks instead of holding everything
in memory — correctness from a guaranteed-consistent state is worth more
than the small time saved by conditionally skipping it). Widened
`method_type` to 256 characters. Along the way, found and fixed one more
real bug this exposed: the widening migration used a plain
`op.alter_column(type_=...)`, which works on Postgres but has no equivalent
on SQLite (`near "ALTER": syntax error`) — every local dev/test run. Fixed
by wrapping it in Alembic's `batch_alter_table`, the standard
cross-dialect-safe pattern, and enabled `render_as_batch` for SQLite
connections generally so future auto-generated migrations do this
automatically.

**Final verification:** rebuilt the actual Docker image and ran it against
a real Postgres container deliberately left in the exact partially-seeded
state a real deploy had hit — confirmed failing before the fix, confirmed
fully healthy (migrations applied, ~62k codes + ~1.7M aliases reseeded,
`/health` and `/loinc/search` both returning real data) after it.

---

## Part 6 — Making the AI optional: fallback, OCR, validation, and what broke

Context: after deploying, manual testing on the live site kept hitting the same
wall — the LLM provider returning errors — and the UI gave no usable feedback.
The response was to make the AI *optional* rather than just fix that one error:
a complete local extraction path, automatic failover to it, a way for users to
correct the output, and a validation layer to catch OCR's characteristic
mistakes. Same theme as before: nearly everything below was found by running
something and looking at the real result.

### 6.1 "Stuck on Loading" was two separate problems

**Symptom.** The deployed frontend showed "Loading..." forever; uploads sat on
"Uploading...".

**What was actually going on.**
1. The Render backend never finished starting: a deploy log showed Alembic's
   connection line, then *five minutes of "no open ports detected"*, then a
   15-minute timeout. Nothing crashed — it was just slow.
2. The frontend had no timeout and no failure path for a request that never
   returns, so a dead backend looked identical to a slow one.

**Root cause of (1).** The previous fix for the partial-seed foreign-key crash
made `seed_loinc_table()` delete and re-insert *everything* on *every* start.
With ~62k codes and **1,694,886 alias rows** (counted directly from the CSV)
that is ~565 chunked commits over the network to Render's Postgres. It passes
instantly against local SQLite, which is why no local test showed it.

**Fix.** Compare row counts to the CSV first and skip when they match; use
`TRUNCATE` (not row-by-row `DELETE`) and 20k-row chunks when a rebuild is
needed. Measured against a real Postgres container: first full seed 139.8 s,
a restart with a correct table **0.6 s**. The corruption case was re-verified
against that same Postgres (stale row + empty alias table → rebuilt to exactly
62,148 / 1,694,886). For the frontend: a 45 s `AbortController` timeout on every
fetch, a visible "backend unreachable" banner, and `console.error` with the URL
on every failure.

**Lesson.** A fix for correctness (always resync) can introduce a performance
regression that only exists in production-shaped conditions. Measure the cost
of a "safe" change at real data size on a real network path.

### 6.2 Opaque errors: `RetryError[<Future at 0x... raised APIStatusError>]`

**Symptom.** A failed upload's `error_message` was that string — useless.

**Root cause.** `tenacity` wraps the final failure in its own `RetryError`
unless told otherwise, hiding the real one (a clear `402: insufficient
credits`). It was also retrying a 402 three times with exponential backoff, for
a request that can never succeed.

**Fix.** `reraise=True`, and an `_is_transient()` predicate: only 429 and 5xx
(plus network errors) are retried; 4xx billing/auth/model errors fail at once
with their real message.

**Also found here.** The OpenAI SDK defaults `max_tokens` to the model's full
context (65,536), which a low-balance OpenRouter key rejects *before* running
the request. An explicit cap fixed it — and OpenRouter later reported a separate
`in_flight_budget_exhausted` 402 for the same low-balance reason. That one
cannot be fixed in code, which is the argument for the whole fallback design.

### 6.3 Bounding the LLM call (time limit + circuit breaker)

A bound on one call isn't enough: a dead provider would still cost one timeout
*per page*. Design: 25 s per HTTP request, 45 s total including retries (the
SDK's own hidden retries are turned off so the budget is real); the first
failure that will repeat stops LLM attempts for the remainder of the document;
a `ValueError` (malformed JSON on one page) only diverts that page.

**Test with no mocks.** The real OpenAI client is pointed at a local HTTP server
that sleeps; the call must be abandoned inside the budget (it is, in well under
2 s with a small test budget). A second test runs a whole document through the
pipeline against that slow server and requires extracted rows to come back.

### 6.4 Layout analysis: three attempts, each killed by a real measurement

The attached real report (multi-column Apollo format) exposed that plain
`page.get_text()` jumbles reading order: percentages came out *before* the
patient demographics, with their reference ranges stranded at the end.

1. **Split the page into columns at the widest horizontal gap** — failed in the
   first real test run. Measuring the actual coordinates showed one row's own
   label→value gap (≈99 pt) is *larger* than the gap between two unrelated
   columns (≈68 pt). No threshold can separate a gap that is smaller from one
   that is larger.
2. **Anchor zones on section headers by font size** — better, but a debug print
   of the "headers" found showed the big emphasized *value* numbers (15–18 pt)
   being classified as headers too, because the check only had a lower bound.
   Adding an upper bound still wasn't enough: the row *labels* are 12 pt, the
   same as real headers. The real discriminator was that a genuine header's
   whole vertical band contains *only* header-sized text.
3. **Merge lines whose vertical gap is under a small tolerance** — worked on one
   fixture and silently merged unrelated rows in another, because the smallest
   merge-worthy gap in one document (1.9 pt) was nearly the same as the
   between-rows gap in a simple one (2.26 pt). Dropping tolerance entirely
   (merge only on genuine vertical *overlap*, transitively) turned out to be
   both simpler and correct for every real case.

Final: 47/47 gold values on all fixtures, zero API calls. Another real find in
the same pass: one fixture used dot-leaders with single spaces
(`Total Cholesterol .......... 210 mg/dL`), which a whitespace-only tokenizer
never split — 4 of 47 misses came from that one file.

**Lesson.** Each design was reasoned to be correct on paper and rejected by
actually running it on real data. Keep the failed attempts in the docstring —
they explain why the final design looks the way it does.

### 6.5 OCR preprocessing: thresholds from measurements, not intuition

Generated test scans with *known* injected defects (so the expected answer is
known) and measured. Results that changed the design:

- **Contrast measure.** Grey-level standard deviation said clean pages were
  "low contrast" (a mostly-white page always has tiny variance). A percentile
  version had the same flaw (ink is under 1% of pixels). Splitting at the Otsu
  threshold and comparing mean paper vs mean ink is independent of ink coverage.
- **Everything else** got thresholds from a measured table (noise residual
  0.14 clean vs 1.3–2.4 degraded; Laplacian variance 387 clean vs 82–143
  blurred/faded; paper-vs-ink range 224 clean vs 114 faded), and each step runs
  only if its defect is present. A clean page must come out untouched — that is
  a test.
- **Payoff.** Degraded scans read **0% of values without preprocessing and 92%
  with it** (clean scans: 88%). The test suite asserts that gap directly, so
  the stage has to keep earning its place.
- **Skew** is found by the classical projection-profile method; estimated
  within 0.4° of the injected angle at five angles. Quarter-turn pages use
  Tesseract's orientation detection (all of 90/180/270 verified).
- **Retry strategy backfired.** An adaptive-threshold (binarized) retry made
  Tesseract run past its timeout on speckled scans — the timeout guard worked,
  and the variant was dropped as a retry (and later removed entirely).

### 6.6 Tesseract is chaotic about scale

Two nearly identical resize factors (1.995× vs exactly 2.0×) took one complex
page from 5/12 to 9/12 correct values. So no single setting is "best".
Design: accept the default attempt if it looks healthy; otherwise try other
scales (max 4 runs, a total page budget) and keep the candidate with the best
*internal* quality score — more rows, rows with unit/range, weighted by OCR
confidence — which needs no ground truth. Measured: 80% vs 79% for always-2×
(oracle 86%): a modest gain concentrated on hard pages, honestly reported as
such.

### 6.7 Memory: I measured it wrong first, then right

The target is Render's 512 MB. Four stages of getting this right:

1. A first measurement said **545 MB** — over budget. It was wrong: the watcher
   was running while the *test-fixture generator* made float copies of a large
   image. Re-measuring only the code under test gave 221 MB (A4) / 281 MB
   (large page).
2. With the app and the LOINC alias index loaded in the same process (the real
   deployment shape) an A4 scan peaked at ~321 MB — acceptable.
3. Docker Desktop wasn't running during the first verification pass, so the
   Linux numbers were unverified; once it was, the large page hit **496 MB** in
   a 512 MB container — 16 MB of headroom. Fixes: cap OCR image pixels, downscale
   oversized inputs, `malloc_trim` after each OCR, and `MALLOC_ARENA_MAX=2`.
4. Repeated runs looked like a leak (410 → 426 → 463 MB). Splitting the cgroup
   into anonymous vs file memory showed anonymous memory *oscillating*
   (279–324 MB), not climbing — fragmentation, not a leak; and the high "peak"
   included reclaimable file cache. Final: 12 consecutive documents including
   six large pages under a hard 512 MB cap, zero OOM kills, anonymous memory
   plateauing at ≈ 293 MB.

This is also why OCR concurrency defaults to **1**: two parallel large-page OCRs
would exceed the limit, so "use more threads" would have been an outage.

### 6.8 A validation filter that would have deleted real tests

The junk-row filter first kept short labels only if they were in the
deterministic alias index. Unit tests with a small hand-made alias dict passed.
Checking against the *real* index showed `co2` and `ph` are not in it (the
index deliberately omits ambiguous aliases) — so it would have silently deleted
genuine `pH` and `CO2` rows from every scan. Replaced with a set of ~8k short
names from *all* LOINC names/aliases (0.5 s to build, <1 MB), plus an
abbreviation-shape check for names LOINC lacks (`INR`), and a regression test
that runs real test names through the real set. The same Linux run surfaced the
trigger: Tesseract read `CO2` as `C02`; the label is now repaired — but only
when a character-swapped variant is an exact known name, never a guess.

**Lesson.** A toy dictionary in a unit test can validate logic that is wrong
against real data. Test the filter that can *delete* data against the real
reference set.

### 6.9 Decimal-point loss: suggest, don't rewrite

OCR dropped decimals on big coloured values (`7.15`→`715`, `10.9`→`109`,
`4.6`→`46`; also `41` for `4.1` in a container run). The validator suggests a
fix only when the value is ≥ 5× the range's upper bound and exactly one
decimal placement lands in a band around the range. It *never* changes the
stored value — silently editing a clinical number is worse than leaving it
visibly suspect — the UI offers a one-click "Use 7.15". Observed limit: with
no range read there is no evidence, so no suggestion; other digit slips (`6`→`1`)
are undetectable. After a user edit the row is re-validated, so a stale
"possible lost decimal" note doesn't outlive the fix.

### 6.10 Frontend bugs found only by driving the real UI

- **Delete looked like it failed but succeeded.** `DELETE` returns 204 with no
  body and the shared `api()` helper always called `response.json()`, which
  throws on an empty body — so the request succeeded, the UI reported an
  error and never refreshed. Found because the network log showed a 204 while
  the screen still had the row. Fixed with one line (`status === 204 → null`).
- **Polling would wipe an open edit form.** The 2 s status poll re-renders the
  list; it now skips the re-render while a form is open (explicit saves force it).

### 6.11 Smaller things worth knowing

- **Empty `DATABASE_URL`** in `.env` made the app fall back to `healthpilot_dev.db`,
  a different file from the one inspected, which made a migrated database look
  un-migrated for a few minutes.
- **A 23 MB test PDF hit the 15 MB upload cap** because the fixture embedded PNG
  pages; real scanned PDFs embed JPEGs. (The cap itself worked.)
- **Shell heredocs mangled long patch scripts** (backticks/`$` and `\n` escapes);
  the reliable approach was writing scripts to files.

---

## How to talk about this in an interview

**"What was the hardest bug you hit?"** → the Act 1–5 story above. Lead with
the process (traceback → no traceback → measure, don't guess → eliminate
hypotheses one at a time → force a real traceback with lower-level tools →
find two ordinary root causes), not just the final one-line answer. The
process is what's actually being evaluated.

**"How do you debug something with no error message?"** → don't guess;
reproduce it somewhere you have more control (in this case, Docker locally
instead of a cloud platform's log stream), and use lower-level diagnostic
tools (`faulthandler`, explicit `BaseException` catches, direct
memory measurement) to force the system to reveal what it's normally
hiding.

**"How do you know a fix actually works, not just that the symptom went
away?"** → re-measure or re-reproduce the *original* failing scenario
specifically, not just "redeploy and hope." Several fixes in this project
were verified by rebuilding the real Docker image and running it against a
database deliberately left in the exact broken state that caused the
original failure.

**"Tell me about a time you were wrong."** → the `COMMON_TEST_RANK` alias
tie-break (3.1). A reasonable-sounding idea, implemented, tested against
the actual data, found to produce a worse failure mode than not having it,
and reverted in favor of a more conservative design. That's a stronger
answer than pretending every design decision was right the first time.

**"What did you do when the AI provider kept failing?"** → made the AI optional
instead of only fixing the one error (6.1-6.3): a time-bounded LLM call, a
circuit breaker so a dead provider costs one attempt per document, and a full
local pipeline behind it. Then said so honestly in the UI: yellow banner,
editable rows, confidence below the review threshold.

**"How did you decide the image-preprocessing thresholds?"** → generated scans
with known injected defects, measured each defect numerically on clean vs
degraded images, set thresholds between them, and wrote tests that assert
recovery against the known truth (6.5). The headline number comes from a test:
0% readable without preprocessing, 92% with.

**"How did you handle memory limits?"** → measured, and caught my own
measurement error (6.7): the first number was inflated by the test-fixture
generator. Verified in a real 512 MB Linux container, separated anonymous
memory from reclaimable cache to tell fragmentation from a leak, and set OCR
concurrency to 1 because memory, not CPU, was the constraint.

**"Why not automatically correct the OCR digit errors?"** → a silently edited
clinical value is worse than a visibly suspect one. The validator suggests
(`715` → `7.15`) and a human applies it (6.9).
