# Speed Benchmark Report — multi-document processing

Date: 2026-10-08. Reproduce with the commands at the bottom. Raw data:
`eval/benchmarks/run_auto.json` (this run) and `eval/benchmarks/baseline_render_prod.json`
(the previous version's results, read from the deployed Render app).

## What was tested

> `docs/pdf&rendition=1.pdf` contains a real patient's identifying details, so it is **git-ignored and
> not in the repository** (and its extracted rows are redacted from the committed JSON). The benchmark
> used a local copy; to reproduce the full six-document run, place your own copy at that path or pass
> `--files`. The other five files are in the repository.

The scenario from the production report: **six PDFs uploaded at the same moment** — all taken from
`docs/` (no other documents were used):

| File | Pages |
|---|---|
| `docs/pdf&rendition=1.pdf` | 3 |
| `docs/sterling-accuris-pathology-sample-report-unlocked.pdf` | 19 |
| `docs/sample test docs/…-3.pdf`, `-6.pdf`, `-18.pdf`, `-19.pdf` | 1 each |

Setup: a fresh, isolated local server (SQLite, new database), the **real Gemini API**
(`gemini-3.8-flash`, `GEMINI_REASONING_EFFORT=low`), 2 document workers (as on Render), the default
page input mode `auto`, the one-time LOINC index warm-up finished before the upload, and the
"remembered mappings" table empty at the start.

**Allowance accounting (12 documents):** a first attempt uploaded all six documents but was aborted
by a bug in my benchmark harness (it polled fast enough to trip the server's own rate limiter, HTTP
429) and produced no data; those 6 documents count against the allowance. The harness was fixed and
validated with the AI switched off (no API calls), then the run below used the other 6 documents.
The server's hard cap of 800 LLM calls was never reached.

## Honest caveats — read before the numbers

* **Different hardware.** "Before" numbers were measured on Render's **free** plan (about 0.1 CPU);
  "now" numbers on a normal developer machine. So the speed-up in the first table is *not* purely
  due to the code. The CPU table further down is hardware-independent and is the fair measure of
  what the code changes saved. **Render has not been re-measured with this version** — do that after
  deploying.
* **The quality comparison is against the previous version's output, not ground truth.** There are
  no gold labels for these documents. Differences include normal run-to-run variation of the AI.
* Only the `auto` page-input mode was benchmarked with real calls (the allowance ran out); the
  `image`-only mode was not compared head-to-head.
* Server CPU/memory include the benchmark's own polling of the API.

## Results (this run, real Gemini calls)

Total wall time until all six documents were finished: **51.3 s**. Server CPU 5.19 s, peak memory 282 MB.

| Document | Pages | Rows | Processed in (s) | Wall from upload (s) | Slowest AI page (s) | LOINC stages |
|---|---|---|---|---|---|---|
| pdf&rendition=1.pdf | 3 | 21 | 15.7 | 16.9 | 10.1 | alias 7, AI-picked 14 |
| sterling.pdf | 19 | 102 | 24.8 | 50.8 | 15.5 | alias 14, remembered 20, AI-picked 60, local-accept 8 |
| sterling-3.pdf | 1 | 7 | 8.7 | 25.9 | 5.3 | alias 1, AI-picked 5, local-accept 1 |
| sterling-6.pdf | 1 | 3 | 5.7 | 16.9 | 3.2 | AI-picked 3 |
| sterling-18.pdf | 1 | 10 | 10.2 | 11.6 | 6.1 | alias 1, AI-picked 9 |
| sterling-19.pdf | 1 | 16 | 14.1 | 31.3 | 7.7 | AI-picked 16 |

"Wall from upload" includes time waiting in the queue: only two documents run at once
(`WORKER_CONCURRENCY=2`, the memory-safe setting for Render's free tier), so the other four wait for a
free worker. On a larger plan, raising the worker count removes most of that wait.

"Opened at" (time from pick-up until the pages were ready) was 0.01–0.15 s for every document;
on Render before it was 2–31 s.

### Before (Render free tier) vs now (this run)

| Document | Before (s) | Now (s) | Rows before / now | Rows reproduced | Same LOINC code |
|---|---|---|---|---|---|
| pdf&rendition=1.pdf | 54.5 | 15.7 | 21 / 21 | 21 / 21 | 20 / 21 |
| sterling.pdf (19 p) | 98.5 | 24.8 | 103 / 102 | 97 / 103 | 92 / 97 |
| sterling-3.pdf | 11.7 | 8.7 | 7 / 7 | 7 / 7 | 7 / 7 |
| sterling-6.pdf | 38.1 | 5.7 | 3 / 3 | 3 / 3 | 3 / 3 |
| sterling-18.pdf | 34.4 | 10.2 | 9 / 10 | 9 / 9 | 8 / 9 |
| sterling-19.pdf | 37.8 | 14.1 | 16 / 16 | 15 / 16 | 14 / 15 |
| **All six** | | | **159 / 159** | **152 / 159 (95.6%)** | **144 / 152 (94.7%)** |

"Rows reproduced" = same page, same printed value, and one name contains the other. What differed:
the 19-page report's narrative "Interpretation" row (not a test) was dropped, a few rows lost an
optional suffix in the name (e.g. "RBC Count Optical impedance" → "RBC Count" — same page and value), and
5 LOINC codes changed on borderline analytes (e.g. an HBsAg interpretation row, haemoglobin-variant
rows). These are within the accuracy trade-off you accepted (roughly 95% agreement with the previous
version), but they are differences a human reviewer should know about.

## Hardware-independent measurement: work done before the AI is even called

Same machine, same documents, no API calls (`eval/bench_prepare.py`). "Before" is what the previous
version did on opening a document (render every page to a 200-DPI PNG); "now" is reading the text and
preparing only what the AI needs (text-only for pages with a solid text layer, otherwise a 150-DPI JPEG).

| Document | Pages | CPU before (s) | CPU now (s) | Upload before (MB) | Upload now (MB) | Pages sent as image |
|---|---|---|---|---|---|---|
| pdf&rendition=1.pdf | 3 | 0.80 | 0.22 | 1.14 | 0.18 | 1/3 |
| sterling.pdf | 19 | 3.12 | 0.11 | 13.57 | 0.03 | 0/19 |
| sterling-3.pdf | 1 | 0.23 | 0.02 | 0.70 | 0.00 | 0/1 |
| sterling-6.pdf | 1 | 0.22 | 0.02 | 0.79 | 0.00 | 0/1 |
| sterling-18.pdf | 1 | 0.20 | 0.00 | 0.46 | 0.00 | 0/1 |
| sterling-19.pdf | 1 | 0.22 | 0.02 | 0.73 | 0.00 | 0/1 |
| **All six** | 26 | **4.80** | **0.38** | **17.39** | **0.22** | 1/26 |

That is **92% less CPU and 99% less upload**. On an instance with about a tenth of a CPU, 4.8 CPU-seconds
is roughly 48 s of wall-clock waiting (plausibly the 28–31 s "opening" stalls seen on Render), against
about 4 s now. This scaling is an estimate, not a measurement.

## What each change addressed, and the evidence

| Weak point found in production | Change | Evidence |
|---|---|---|
| 28–31 s spent rendering pages before work could start; small documents queued behind a global render lock | Pages render lazily (only when needed), as a smaller JPEG or not at all (text-only for text-rich digital pages); lock removed | CPU table above; "Opened at" 0.01–0.15 s |
| ~2 s per page of serial database saving on the 19-page document | Progress lines commit at most every ~0.8 s; one commit per page; redundant per-page cancel query removed | Unit test: 8 pages → ≤16 commits (the old code committed about six times per page by construction); not measured against Postgres |
| A second AI call per page just to pick LOINC codes (81 of 103 rows needed it) | Remembered picks (high-confidence AI picks and human reviews) skip the AI next time | In this run 20 rows of the 19-page document came from memory learned earlier in the same run; AI-picked rows 81 → 60 |
| 19 pages sent in two waves (limit 12) | `LLM_MAX_CONCURRENCY` default 12 → 20 | All 19 pages of the large document were requested at once |
| Stale "[Recovered after an interrupted run; retrying.]" yellow banner | The message is cleared when a job (re)starts; bracketed system notes are never shown as page problems | Unit test + UI logic |
| Cold-start stalls after a deploy | One-time LOINC indexes warm up in the background at startup (already in place) | Not re-measured on Render |

Not done / not measured: moving Render off the free plan (the biggest lever on that hardware — a
recommendation, not a code change); `GEMINI_MAPPING_MODEL` (a lighter model for the LOINC-picking call)
exists as an option but was **not tested**.

## Changes made after the benchmark run

Two small changes landed after the real-API run and could not be re-benchmarked with real calls (the
allowance was used up). Neither touches AI timing: (1) the worker's "has this been cancelled?" check
now ends its read transaction, and local SQLite uses WAL mode — found when a repeated concurrency test
occasionally stalled because an open read lock on SQLite blocked the other document's write (Postgres,
used in production, does not behave this way); (2) the header's hover breakdown was fixed to redraw once
the detail loads. After them the full test suite (359 tests, AI mocked) passed, and a smoke run of the
same six documents with the AI switched off completed all six with no locking errors.

## Reproduce

```bash
# previous-version baseline from the deployed app (read-only)
python eval/bench_speed.py snapshot-prod --out eval/benchmarks/baseline_render_prod.json
# the benchmark itself (real Gemini calls; uses backend/.env's key; bounded by --call-cap)
python eval/bench_speed.py run --label auto --env LLM_PAGE_INPUT=auto --out eval/benchmarks/run_auto.json
python eval/bench_speed.py report eval/benchmarks/run_auto.json --baseline eval/benchmarks/baseline_render_prod.json
# hardware-independent preparation cost (no API calls)
python eval/bench_prepare.py
```
