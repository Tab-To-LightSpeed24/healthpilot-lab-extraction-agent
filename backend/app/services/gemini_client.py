import json
import logging
from typing import List, Optional

import google.generativeai as genai
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.schemas.extraction import EXTRACTION_JSON_SCHEMA, PageExtractionResult

logger = logging.getLogger(__name__)

_configured = False

# Config errors (e.g. missing API key) will never succeed on retry, so they
# must not be wrapped by the @retry decorators below -- only transient
# network/API errors should be retried.
class ConfigurationError(RuntimeError):
    pass


def _ensure_configured():
    global _configured
    if not _configured:
        if not settings.gemini_api_key:
            raise ConfigurationError(
                "GEMINI_API_KEY is not set. Add it to backend/.env before processing documents."
            )
        genai.configure(api_key=settings.gemini_api_key)
        _configured = True


# Only transient network/API errors should be retried -- ConfigurationError
# (missing key) and ValueError (caller passed invalid input, e.g. no image
# and no text layer) are programmer/config errors that will never succeed on
# retry, so retrying them just adds latency for no benefit.
_retry_transient = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_not_exception_type((ConfigurationError, ValueError)),
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

Return ONLY the JSON object matching the required schema.
"""


@_retry_transient
def extract_page(image_png: bytes, text_layer: Optional[str]) -> PageExtractionResult:
    _ensure_configured()
    model = genai.GenerativeModel(settings.gemini_model)
    prompt = EXTRACTION_PROMPT.format(text_layer=text_layer or "(no text layer available)")

    # Plain-text reports have no rasterized page (image_png is empty) -- send
    # a text-only request rather than an invalid empty image part.
    contents = [prompt]
    if image_png:
        contents.append({"mime_type": "image/png", "data": image_png})
    elif not text_layer:
        raise ValueError("extract_page called with neither an image nor a text layer")

    response = model.generate_content(
        contents,
        generation_config={
            "response_mime_type": "application/json",
            "response_schema": EXTRACTION_JSON_SCHEMA,
            "temperature": 0.0,
        },
    )
    data = json.loads(response.text)
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
    _ensure_configured()
    model = genai.GenerativeModel(settings.gemini_model)
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
    response = model.generate_content(
        prompt,
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.0,
        },
    )
    return json.loads(response.text)


@_retry_transient
def embed_text(text: str) -> List[float]:
    _ensure_configured()
    result = genai.embed_content(model=settings.gemini_embedding_model, content=text)
    return result["embedding"]


def embed_texts_batch(texts: List[str]) -> List[List[float]]:
    return [embed_text(t) for t in texts]
