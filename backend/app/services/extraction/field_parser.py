"""Turns reading-order-corrected rows (app.services.extraction.layout) into
the same ExtractedTest/PageExtractionResult shape the LLM path produces
(app/schemas/extraction.py) -- this is the stable contract boundary, so
nothing downstream (loinc_mapping.py, pipeline.py persistence) needs to
change regardless of which path produced the data.

Parsing strategy, informed directly by the real test fixtures (both the
simple synthetic PDFs under eval/sample_reports/ and the real, complex
Apollo Hospitals fixture):

- A synthetic PDF's whole row is usually ONE PyMuPDF line with internal
  multi-space padding, e.g. "WBC          6.8    10*3/uL   4.0-11.0".
- The real Apollo fixture's row is usually SEVERAL separate PyMuPDF lines
  already merged into one by layout.py (label, value, range, range's own
  unit, flag each a distinct original line).

Both are unified by first splitting every merged row's spans on runs of 2+
whitespace characters into a flat token sequence, then assigning roles
(label -> value [+ embedded or adjacent unit] -> reference_range -> flag)
to that token sequence positionally, in left-to-right order. A token that
doesn't fit any required role (e.g. a bare gauge-widget tick-mark number
like Apollo's "3.7"/"5.6"/"7.5" scattered between the value and the actual
"3.7 - 5.6" range text) is simply never claimed by any role and has no
effect -- this is deliberate: nothing here tries to explicitly classify and
discard "noise" tokens, since the real reference range is always available
from its own distinctly-patterned token regardless.
"""
import re
from typing import List, Optional, Tuple

from app.schemas.extraction import ExtractedTest, PageExtractionResult
from app.services.extraction.schemas import Line, PageLayout

# Labels that are structurally value-shaped (e.g. "LRN : 12109545" has a
# numeric-leading second token) but are patient/report metadata, never a lab
# test. Checked as a prefix/substring match against the lowercased label.
_NON_TEST_LABEL_MARKERS = (
    "patient name", "patient:", "age/gender", "sex", "dob", "mrn", "lrn",
    "sample id", "icmr id", "refered by", "referred by", "location",
    "report date", "sample drawn date", "sample regd", "sample auth date",
)

# A label this long, or starting with one of these, is prose (an "Info-"
# explanation, an "Inference-" clinical note, a disclaimer) rather than a
# test name. 60 chars comfortably admits the longest real test name in the
# fixture set ("TPHA-TREPONEMA PALLIDIUM HEMAGGLUTINATION ASSAY", 47 chars)
# while rejecting the shortest real prose sentence observed (74 chars).
_MAX_LABEL_CHARS = 60
_PROSE_PREFIXES = ("info", "inference", "disclaimer", "note")

_QUALIFIER_VALUES = (
    "non-reactive", "reactive", "negative", "positive", "normal",
    "not detected", "detected", "trace", "equivocal", "borderline",
)
_FLAG_WORDS = ("normal", "high", "low", "critical", "abnormal", "h", "l")

_VALUE_PATTERN = re.compile(
    r"^(?P<num>[<>]?\s?\d+\.?\d*)\s*(?P<rest>.*)$|^(?P<qual>" + "|".join(_QUALIFIER_VALUES) + r")\b",
    re.IGNORECASE,
)
_RANGE_PATTERN = re.compile(r"\d+\.?\d*\s*-\s*\d+\.?\d*|[<>]\s*\d+\.?\d*|\(.+\)")
_UNIT_SHAPE = re.compile(r"^[A-Za-z%/°μu³\*\d\.\-]{1,20}$")
_SPECIMEN_PATTERN = re.compile(r"specimen\s*:?\s*([A-Za-z ]+)", re.IGNORECASE)


_TOKEN_SPLIT = re.compile(r"\s{2,}|\.{2,}")


def _tokenize_row(row: Line) -> List[str]:
    """Flattens a merged row's spans into tokens, splitting each span's text
    on runs of 2+ whitespace (handles single-span padded rows) or runs of
    2+ dots (a real, measured case: one fixture uses dot-leaders with only
    single spaces around them, e.g. "Total Cholesterol .......... 210
    mg/dL", which a whitespace-only split never breaks apart at all) while
    leaving naturally-short spans (the common case for the complex real
    fixture) untouched."""
    tokens: List[str] = []
    for span in row.spans:
        parts = _TOKEN_SPLIT.split(span.text.strip())
        tokens.extend(p.strip(" .") for p in parts if p.strip(" ."))
    return tokens


# Used only to vet rows that had no multi-space/dot separators at all (see
# _expand_single_token). Strict on purpose: a stray word like "of" in
# "Page 1 of 2" must not be accepted as a unit and produce a junk test row.
_KNOWN_UNIT = re.compile(
    r"^(?:[A-Za-zµμ%]+/[A-Za-z0-9µμ%\^\*]+|%|fl|pg|sec|s|"
    r"10[\*\^]\d+/\w+|x10[\*\^]?\d+/?\w*|mm/hr)$",
    re.IGNORECASE,
)
_SINGLE_TOKEN = re.compile(
    r"^(?P<label>.*?[A-Za-z\)\]].*?)\s+"
    r"(?P<value>[<>]?\d+(?:\.\d+)?|" + "|".join(_QUALIFIER_VALUES) + r")"
    r"(?:\s+(?P<tail>.*))?$",
    re.IGNORECASE,
)


def _expand_single_token(text: str) -> Optional[List[str]]:
    """For a row with no multi-space or dot-leader separators at all (e.g.
    plain text typed as "Iron 70 ug/dL 50-170"), recover label / value /
    unit / range from single-space word boundaries. Deliberately strict --
    returns None (row rejected, as before) unless the row carries a real
    unit, a range, or a qualifier value as corroborating evidence that it's
    a test result and not prose that happens to contain a number."""
    match = _SINGLE_TOKEN.match(text)
    if not match:
        return None
    label, value, tail = match.group("label").strip(), match.group("value"), (match.group("tail") or "").strip()
    is_qualifier = not value[0].isdigit() and value[0] not in "<>"
    words = tail.split()
    merged: List[str] = []
    i = 0
    while i < len(words):
        if i + 2 < len(words) + 0 and words[i + 1] == "-" and re.fullmatch(r"\d+\.?\d*", words[i])                 and re.fullmatch(r"\d+\.?\d*", words[i + 2]):
            merged.append(f"{words[i]} - {words[i + 2]}")
            i += 3
        else:
            merged.append(words[i])
            i += 1
    has_unit = bool(merged) and bool(_KNOWN_UNIT.match(merged[0]))
    has_range = any(_RANGE_PATTERN.search(w) for w in merged)
    if not (is_qualifier or has_unit or has_range):
        return None
    if merged and not has_unit and not has_range:
        return None
    return [label, value] + merged


_PURE_RANGE = re.compile(r"^(?:\d+\.?\d*\s*-\s*\d+\.?\d*|[<>]\s*\d+\.?\d*)$")


def _is_range_token(token: str) -> bool:
    return bool(_PURE_RANGE.match(token)) or (token.startswith("(") and token.endswith(")"))


def _is_label_token(token: str) -> bool:
    """A token that can open a new test record: alphabetic-leading, and not
    a value, range, flag, or recognizable unit."""
    return (
        bool(re.match(r"[A-Za-z]", token))
        and not _VALUE_PATTERN.match(token)
        and not _is_range_token(token)
        and not _looks_like_flag(token)
        and not _KNOWN_UNIT.match(token)
    )


def _is_label_like(token: str) -> bool:
    return bool(re.match(r"[A-Za-z(]", token)) and not _VALUE_PATTERN.match(token) and not _is_range_token(token)


def _is_rejected_label(label: str) -> bool:
    lower = label.lower()
    if len(label) > _MAX_LABEL_CHARS:
        return True
    if lower.startswith(_PROSE_PREFIXES):
        return True
    return any(marker in lower for marker in _NON_TEST_LABEL_MARKERS)


def _looks_like_unit(token: str) -> bool:
    if not _UNIT_SHAPE.match(token):
        return False
    if _RANGE_PATTERN.fullmatch(token):
        return False
    return token.lower() not in _FLAG_WORDS


def _looks_like_flag(token: str) -> bool:
    return token.lower() in _FLAG_WORDS


def _split_value_and_unit(token: str) -> Tuple[str, Optional[str]]:
    match = _VALUE_PATTERN.match(token)
    if not match:
        return token, None
    if match.group("qual"):
        return match.group("qual"), None
    value = match.group("num").replace(" ", "").rstrip(".")
    rest = match.group("rest").strip()
    return value, (rest or None)


def _find_value_index(tokens: List[str]) -> Optional[int]:
    for i, token in enumerate(tokens):
        if _VALUE_PATTERN.match(token):
            return i
    return None


def parse_row(row: Line) -> Optional[ExtractedTest]:
    tokens = _tokenize_row(row)
    if not tokens:
        return None
    if len(tokens) == 1:
        expanded = _expand_single_token(tokens[0])
        if expanded is None:
            return None
        tokens = expanded
    return _test_from_tokens(tokens)


def _test_from_tokens(tokens: List[str]) -> Optional[ExtractedTest]:
    label = tokens[0]
    if _is_rejected_label(label):
        return None

    remaining = tokens[1:]
    value_idx = _find_value_index(remaining)
    if value_idx is None:
        return None

    # A label wrapped over two lines ("MCV (Pulse height (Derived" / "from RBC
    # histogram))") arrives as extra text tokens before the value. Join them
    # when the label is visibly unfinished (unbalanced parenthesis) or the
    # extra text starts lowercase, as a continuation does.
    if value_idx > 0:
        extra = remaining[:value_idx]
        unbalanced = label.count("(") > label.count(")")
        if all(_is_label_like(t) for t in extra) and (unbalanced or extra[0][:1].islower()):
            label = " ".join([label, *extra])
            remaining = remaining[value_idx:]
            value_idx = 0

    value, unit = _split_value_and_unit(remaining[value_idx])

    reference_range: Optional[str] = None
    flag: Optional[str] = None
    for token in remaining[value_idx + 1:]:
        if reference_range is None and _RANGE_PATTERN.search(token):
            # Table rules / specks read by OCR as "=", "|", "_" stick to the front.
            reference_range = token.lstrip("=~_|—– ") or token
            continue
        if unit is None and _looks_like_unit(token):
            unit = token
            continue
        if flag is None and _looks_like_flag(token):
            flag = token
            continue

    return ExtractedTest(
        original_test_name=label,
        value=value,
        unit=unit,
        reference_range=reference_range,
        flag=flag,
        # Deterministic rule-based parse, not a model confidence score.
        # Phase 3a's validation layer computes a real per-row confidence
        # from OCR word confidence + pattern-match strength; this constant
        # is a placeholder until that lands.
        extraction_confidence=0.9,
    )


def parse_row_records(row: Line) -> List[ExtractedTest]:
    """Multi-record variant for OCR rows. Tesseract's line segmentation can
    join side-by-side columns into one line ("Neutrophils 57 % 40-80 %
    MCH 24 pg 26-32 pg"), so one row may hold several test records. Walks the
    tokens left to right: a label-like token opens a new record once the
    current one already has a value; before that it just extends the label.
    Numeric stray tokens (gauge ticks, OCR specks) never open a record."""
    tokens = _tokenize_row(row)
    if not tokens:
        return []
    if len(tokens) == 1:
        expanded = _expand_single_token(tokens[0])
        if expanded is None:
            return []
        tokens = expanded

    records: List[Tuple[str, List[str]]] = []
    label: Optional[str] = None
    rest: List[str] = []
    for tok in tokens:
        if label is None:
            if _is_label_token(tok):
                label, rest = tok, []
            continue
        if _is_label_token(tok):
            if _find_value_index(rest) is not None:
                records.append((label, rest))
                label, rest = tok, []
            elif not rest:
                label = f"{label} {tok}"
            else:
                rest.append(tok)
        else:
            rest.append(tok)
    if label is not None:
        records.append((label, rest))

    tests = [_test_from_tokens([lbl] + r) for lbl, r in records]
    return [t for t in tests if t is not None]


def _row_tests(row: Line) -> List[ExtractedTest]:
    if row.spans and row.spans[0].source == "ocr":
        return parse_row_records(row)
    test = parse_row(row)
    return [test] if test else []


def _detect_specimen(row: Line) -> Optional[str]:
    match = _SPECIMEN_PATTERN.search(row.text)
    return match.group(1).strip() if match else None


def parse_page(layout: PageLayout) -> PageExtractionResult:
    """`layout` must already be in reading order (the output of
    `layout.reconstruct_reading_order`), so specimen/section context
    propagates correctly: a specimen stated once for a panel applies to
    every test row beneath it until a new one is declared -- mirroring the
    LLM path's explicit prompt rule for the same requirement
    (app/services/llm_client.py's EXTRACTION_PROMPT)."""
    tests: List[ExtractedTest] = []
    current_specimen: Optional[str] = None
    pending: List[Line] = []  # consecutive label-only rows awaiting a value row beneath
    dangling: Optional[str] = None  # a label line left open ("... (Derived") awaiting its second line

    for row in layout.lines:
        specimen = _detect_specimen(row)
        if specimen:
            current_specimen = specimen

        row_tests = _row_tests(row)
        if not row_tests:
            paired, pending = _stacked_pair(row, pending)
            row_tests = [paired] if paired else []
            toks = _tokenize_row(row)
            dangling = toks[0] if len(toks) == 1 and toks[0].count("(") > toks[0].count(")") else None
        else:
            pending = []
            first = row_tests[0]
            if dangling and first.original_test_name[:1].islower():
                first.original_test_name = f"{dangling} {first.original_test_name}"
            dangling = None
        for test in row_tests:
            if test.specimen is None and current_specimen is not None:
                test.specimen = current_specimen
            tests.append(test)

    return PageExtractionResult(tests=tests, page_notes=None)


# A label wrapped over two lines sits closer than this (as a multiple of the
# line's own height) to the line above it; a separate title sits further away.
_STACK_GAP_FACTOR = 1.6


def _is_label_only(tokens: List[str]) -> bool:
    return (
        len(tokens) == 1
        and not _VALUE_PATTERN.match(tokens[0])
        and not _is_rejected_label(tokens[0])
        and len(tokens[0]) >= 2
    )


def _stacked_pair(row: Line, pending: List[Line]) -> Tuple[Optional[ExtractedTest], List[Line]]:
    """Handles "card" layouts where the label sits on one row and its value
    on the row directly beneath it (seen in the real Apollo fixture's pages
    2-3), including a label wrapped across two rows. Returns (test, new
    pending). Only pairs within the same column zone, and only a value-first
    row directly following label-only row(s), so ordinary prose and titles
    don't turn into spurious tests."""
    tokens = _tokenize_row(row)
    if not tokens:
        return None, []

    if pending and _VALUE_PATTERN.match(tokens[0]) and pending[-1].column_index == row.column_index:
        label = " ".join(p.text.strip().rstrip(":").strip() for p in pending)
        return _test_from_tokens([label] + tokens), []

    if _is_label_only(tokens):
        if pending and pending[-1].column_index == row.column_index:
            prev = pending[-1]
            height = max(prev.bbox.y1 - prev.bbox.y0, 1e-6)
            if (row.bbox.y0 - prev.bbox.y0) <= _STACK_GAP_FACTOR * height:
                return None, pending + [row]
        return None, [row]

    return None, []
