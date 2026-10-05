import base64
import json
import logging
from typing import List, Optional

from openai import APIStatusError, OpenAI
from tenacity import retry, retry_if_exception, wait_exponential

from app.core.config import settings
from app.schemas.extraction import PageExtractionResult

logger = logging.getLogger(__name__)

# Gemini's OpenAI-compatible endpoint: lets us keep the OpenAI SDK.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

_client: Optional[OpenAI] = None

# Config errors (e.g. missing API key) will never succeed on retry, so they
# must not be wrapped by the @retry decorators below -- only transient
# network/API errors should be retried.
class ConfigurationError(RuntimeError):
    pass


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not settings.gemini_api_key:
            raise ConfigurationError(
                "GEMINI_API_KEY is not set. Add it to backend/.env before processing documents."
            )
        _client = OpenAI(
            base_url=GEMINI_BASE_URL,
            api_key=settings.gemini_api_key,
            # Bounded per-request wait; retries are owned by tenacity below
            # (with a wall-clock budget), not the SDK's own hidden retries.
            timeout=settings.llm_request_timeout_seconds,
            max_retries=0,
        )
    return _client


def _is_transient(exc: BaseException) -> bool:
    """ConfigurationError (missing key) and ValueError (caller passed invalid
    input, e.g. no image and no text layer) are programmer/config errors
    that will never succeed on retry. Same for most APIStatusError codes --
    a 402 (insufficient credits), 401 (bad key), 404 (unknown model), etc.
    describe a request that will fail identically every time; only 429 (rate
    limited) and 5xx (provider-side transient failure) are worth another
    attempt. Retrying the rest just adds latency with zero chance of
    success -- confirmed directly: a real 402 from a depleted provider
    credit balance was retried 3 times with exponential backoff before
    surfacing, for no benefit."""
    if isinstance(exc, (ConfigurationError, ValueError)):
        return False
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return True


# reraise=True: without it, tenacity raises its own RetryError whose str()
# is a useless "RetryError[<Future at 0x... state=finished raised
# APIStatusError>]" -- confirmed directly as the actual error_message a real
# document upload surfaced, making a real, well-described API failure (e.g.
# "402: insufficient credits") look like an opaque, untraceable hang to the
# caller. reraise=True re-raises the real underlying exception instead.
MAX_ATTEMPTS = 3


def _stop(retry_state) -> bool:
    """Give up after MAX_ATTEMPTS, or as soon as the wall-clock budget for
    this call is spent -- whichever comes first -- so a slow or flapping
    provider can never hold a page hostage; the caller then diverts the page
    to the local fallback. Read from settings at call time (not import
    time) so the budget is tunable and testable."""
    return (
        retry_state.attempt_number >= MAX_ATTEMPTS
        or retry_state.seconds_since_start >= settings.llm_page_budget_seconds
    )


_retry_transient = retry(
    stop=_stop,
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception(_is_transient),
    reraise=True,
)


EXTRACTION_PROMPT = """You are a clinical laboratory data extraction system.

You will be shown one page of a laboratory report (as an image, and possibly
also its extracted text layer below for cross-reference). Extract EVERY
distinct laboratory test result on this page.

Rules:
- Only report tests that are actually printed on this page. NEVER invent,
  guess, or hallucinate a test, value, unit, or range that is not visible.
- Preserve the value exactly as printed, including qualifiers like "<0.1",
  ">500", "Positive", "Negative", "Trace", "Not Detected".
- If a field (unit, reference range, specimen, method, timing, flag) is not
  present on the page, omit it (use null) rather than guessing.
- IMPORTANT: specimen/section context stated ONCE for a whole panel (e.g. a
  page header or section title like "Urinalysis - Specimen: Urine", or a
  panel titled "Serum Chemistry") applies to EVERY test row under it, even
  though it is only printed once. Copy that specimen value into the
  `specimen` field of each individual test row it covers -- do not leave
  `specimen` null just because it wasn't repeated on that specific row. This
  matters because the same test name (e.g. "Glucose" or "Protein") means a
  different LOINC concept in urine vs. serum/blood, so losing this context
  causes a wrong code to be assigned downstream.
- Do not include panel/section headers, patient demographics, or narrative
  text as if they were tests.
- Give each row an extraction_confidence between 0 and 1 reflecting how
  legible and unambiguous that specific row was on the page (lower for
  blurry scans, cut-off text, or handwriting).
- If the page is blank, is a cover/demographics page with no results, or you
  cannot read it, return an empty tests list and explain why in page_notes.

Extracted text layer (may be empty or unreliable for scanned pages):
---
{text_layer}
---

Respond with ONLY a JSON object of this exact shape (omit a field, i.e. use
null, rather than guessing a value that isn't printed):
{{"tests": [{{"original_test_name": "<string>", "value": "<string|null>",
  "unit": "<string|null>", "reference_range": "<string|null>",
  "specimen": "<string|null>", "method": "<string|null>",
  "timing": "<string|null>", "flag": "<string|null>",
  "extraction_confidence": <0.0-1.0>}}],
 "page_notes": "<string|null>"}}
"""


@_retry_transient
def extract_page(image_png: bytes, text_layer: Optional[str]) -> PageExtractionResult:
    client = _get_client()
    prompt = EXTRACTION_PROMPT.format(text_layer=text_layer or "(no text layer available)")

    # Plain-text reports have no rasterized page (image_png is empty) -- send
    # a text-only request rather than an invalid/empty image part.
    content: list = [{"type": "text", "text": prompt}]
    if image_png:
        b64 = base64.b64encode(image_png).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    elif not text_layer:
        raise ValueError("extract_page called with neither an image nor a text layer")

    response = client.chat.completions.create(
        model=settings.gemini_model,
        messages=[{"role": "user", "content": content}],
        response_format={"type": "json_object"},
        temperature=0.0,
        # Generous enough for a dense page of results (Gemini 2.5 models also
        # count internal "thinking" tokens against this cap).
        max_tokens=8192,
    )
    data = json.loads(response.choices[0].message.content)
    return PageExtractionResult.model_validate(data)


MAPPING_PROMPT = """You are assisting with LOINC coding of a laboratory observation.

Observation as extracted from the source report:
  Original test name: {original_name}
  Normalized name guess: {normalized_name}
  Value: {value}  Unit: {unit}
  Specimen: {specimen}  Method: {method}  Timing: {timing}

Candidate LOINC concepts (retrieved by semantic similarity, ranked, NOT
guaranteed correct):
{candidates}

Task: choose the single best-matching LOINC candidate for this observation,
or decide that none of the candidates is a reliable match.

Consider specimen/system and method as disambiguating context (e.g. serum vs
urine vs blood; calculated vs direct assay) -- do not pick a candidate on
name similarity alone if the specimen/system contradicts it.

Respond with ONLY a JSON object of the form:
{{"chosen_loinc_num": "<one of the candidate loinc_num values, or null>",
  "confidence": <0.0-1.0>,
  "rationale": "<one sentence>"}}
"""


@_retry_transient
def verify_mapping(
    original_name: str,
    normalized_name: str,
    value: Optional[str],
    unit: Optional[str],
    specimen: Optional[str],
    method: Optional[str],
    timing: Optional[str],
    candidates: List[dict],
) -> dict:
    client = _get_client()
    candidates_str = "\n".join(
        f"- loinc_num={c['loinc_num']} | {c['long_common_name']} | system={c.get('system')} | component={c.get('component')}"
        for c in candidates
    )
    prompt = MAPPING_PROMPT.format(
        original_name=original_name,
        normalized_name=normalized_name,
        value=value,
        unit=unit,
        specimen=specimen,
        method=method,
        timing=timing,
        candidates=candidates_str or "(no candidates found)",
    )
    response = client.chat.completions.create(
        model=settings.gemini_model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=2048,
    )
    return json.loads(response.choices[0].message.content)
