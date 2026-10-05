"""Reading-order reconstruction for complex, multi-column page layouts.

Motivated by a real, measured bug: PyMuPDF's plain `get_text("text")` dumps
spans in drawing order, which jumbles reading order across side-by-side
columns. On a real multi-column lab report (Apollo Hospitals format, used
as a committed test fixture -- see tests/extraction/test_layout_reading_order.py),
this produced unrelated values and reference ranges, separated by hundreds
of characters of unrelated text, from their own labels.

This went through two design iterations, both empirically tested against
the real fixture (not just reasoned about on paper):

1. First attempt: split each page globally into N columns by finding gaps
   in the x-coverage of all lines' bounding boxes. This FAILED a real test
   run: on the real fixture, the gap between one row's OWN label and its
   OWN value (~99pt, "MCH(Calculated)" to "24 pg") is bigger than the gap
   that actually separates that row's whole column-group from the
   unrelated next column over (~68pt, between that row's flag and
   "Neutrophils"). No single gap threshold can tell those two kinds of gap
   apart -- the "wrong" gap is smaller than the "right" one.

2. This version: use section headers as zone anchors instead of guessing
   from generic whitespace gaps. Headers are a much stronger, already-
   distinct signal in this document (font size >=12.0 vs body text's
   <=10.5, confirmed by direct measurement) and there are only a handful
   of them, unlike dozens of individual field-level x-positions. When two
   headers share the same y-band (e.g. "Blood Indices" and "Differential
   Leucocyte count" sit side by side at the same y), that's a reliable
   signal of two parallel zones from there down the page, split at the
   midpoint between the headers' own x-positions -- not at some fragile
   gap inferred from the row content itself.

Rows within a zone are then grouped the same way as before: single-linkage
clustering on the Y axis (a gap larger than ROW_BAND_Y_GAP_FRACTION starts
a new row), which correctly merges a row's label/value/range/flag even
when they are separate PyMuPDF line objects, including the case where a
range and its own unit are two vertically-stacked, non-overlapping lines.
"""
from typing import List, Optional, Tuple

from app.services.extraction.schemas import BBox, Line, PageLayout, TextSpan

# Lines merge into the same row only if their Y ranges genuinely OVERLAP
# (directly, or transitively through a chain of overlapping lines) -- no
# positive gap tolerance at all. A real measurement run proved a small
# tolerance can't work in general: the Apollo fixture's smallest
# merge-worthy gap (a reference range to its own unit on the next line
# down, ~1.9pt) is barely smaller than one of the simple synthetic
# fixtures' between-DIFFERENT-test-row gaps (~2.26pt) -- any single
# tolerance between them is razor-thin and not a real discriminator. Strict
# overlap turns out to be sufficient for every real case actually needed:
# the Apollo fixture's range-to-unit pair doesn't overlap each other
# directly, but both independently overlap that row's own label, so they
# still merge transitively; a synthetic PDF's cleanly-separated,
# non-overlapping test rows correctly never merge.
ROW_BAND_Y_GAP_FRACTION = 0.0

# A line's largest span font size inside this band is a section/panel
# header, not body text. Measured on the real fixture: body text tops out
# at 10.5pt; real headers are 12.0-13.5pt; BUT a naive lower-bound-only
# check also caught this document's big emphasized VALUE numbers
# (15.0-18.0pt, e.g. "10.9 gm/dl") as false headers -- confirmed directly
# by a real failing test run, not assumed. There's a clean measured gap
# between 13.5 (largest real header) and 15.0 (smallest such value), so an
# upper bound closes that gap. This threshold is tuned to this real
# document's specific font sizes; a different report template could need
# retuning -- a known limitation of a heuristic rather than a learned
# layout model.
HEADER_FONT_SIZE_MIN = 11.5
HEADER_FONT_SIZE_MAX = 14.0
# Headers are short titles; this guards against a large-font body paragraph
# (not observed in the fixture set, but a cheap safety margin) being
# mistaken for one.
MAX_HEADER_CHARS = 60


def _is_header_sized(line: Line) -> bool:
    size = line.max_font_size
    return HEADER_FONT_SIZE_MIN <= size <= HEADER_FONT_SIZE_MAX and len(line.text) <= MAX_HEADER_CHARS


def _true_headers(all_lines: List[Line]) -> List[Line]:
    """A header-sized line alone isn't enough: this document also uses the
    same 12.0pt font for a handful of test-row LABELS (e.g. "Hemoglobin
    (Modified Cyanmethaemoglobin)", "RBC Count(Optical)") -- confirmed
    directly by a real failing test run, not assumed, where one of these
    mislabeled "headers" fragmented a zone boundary right between a row's
    own label and its value. The real discriminator: a genuine section
    header's whole y-band (computed across ALL lines on the page, not
    scoped to any zone yet) contains ONLY other header-sized lines --
    "Blood Indices" and "Differential Leucocyte count" share a band with
    nothing else in it. A label like "Hemoglobin (...)" shares its band
    with its own value, which is NOT header-sized (18pt, above
    HEADER_FONT_SIZE_MAX) -- so that whole band is rejected."""
    bands = _band_lines_by_y(all_lines)
    headers: List[Line] = []
    for band in bands:
        if band and all(_is_header_sized(l) for l in band):
            headers.extend(band)
    return headers


def _merge_bbox(lines: List[Line]) -> BBox:
    return BBox(
        x0=min(l.bbox.x0 for l in lines),
        y0=min(l.bbox.y0 for l in lines),
        x1=max(l.bbox.x1 for l in lines),
        y1=max(l.bbox.y1 for l in lines),
    )


def _band_lines_by_y(lines: List[Line]) -> List[List[Line]]:
    """Single-linkage clustering on the Y axis: two lines are in the same
    band if the gap between them (or a chain of lines connecting them)
    never exceeds ROW_BAND_Y_GAP_FRACTION."""
    ordered = sorted(lines, key=lambda l: l.bbox.y0)
    bands: List[List[Line]] = []
    current: List[Line] = []
    current_max_y1 = None
    for line in ordered:
        if current and (line.bbox.y0 - current_max_y1) > ROW_BAND_Y_GAP_FRACTION:
            bands.append(current)
            current = []
            current_max_y1 = None
        current.append(line)
        current_max_y1 = line.bbox.y1 if current_max_y1 is None else max(current_max_y1, line.bbox.y1)
    if current:
        bands.append(current)
    return bands


class _Zone:
    __slots__ = ("y_start", "y_end", "x_start", "x_end", "lines")

    def __init__(self, y_start: float, y_end: float, x_start: float, x_end: float):
        self.y_start = y_start
        self.y_end = y_end
        self.x_start = x_start
        self.x_end = x_end
        self.lines: List[Line] = []

    def contains(self, line: Line) -> bool:
        center_x = (line.bbox.x0 + line.bbox.x1) / 2
        return self.y_start <= line.bbox.y0 < self.y_end and self.x_start <= center_x < self.x_end


def _build_zones(all_lines: List[Line]) -> List[_Zone]:
    """Partitions the page into vertical regions anchored by section-header
    bands, with each region split horizontally at the midpoints between any
    headers that share that band (parallel sections side by side). The
    region above the first header (e.g. a document's demographics block,
    which precedes any section header) is a single full-width zone."""
    headers = _true_headers(all_lines)
    header_bands = sorted(_band_lines_by_y(headers), key=lambda band: min(h.bbox.y0 for h in band))

    zones: List[_Zone] = []
    band_starts = [min(h.bbox.y0 for h in band) for band in header_bands]
    region_starts = [0.0] + band_starts
    region_ends = band_starts + [1.0]

    for region_start, region_end, band in zip(
        region_starts, region_ends, [[]] + header_bands
    ):
        if not band:
            zones.append(_Zone(region_start, region_end, 0.0, 1.0))
            continue
        siblings = sorted(band, key=lambda l: l.bbox.x0)
        boundaries = [0.0] + [
            (a.bbox.x1 + b.bbox.x0) / 2 for a, b in zip(siblings, siblings[1:])
        ] + [1.0]
        for i in range(len(siblings)):
            zones.append(_Zone(region_start, region_end, boundaries[i], boundaries[i + 1]))

    return zones


def _assign_to_zone(line: Line, zones: List[_Zone]) -> _Zone:
    for zone in zones:
        if zone.contains(line):
            return zone
    return zones[-1]


def _merge_rows_in_zone(zone_lines: List[Line]) -> List[Line]:
    rows = []
    for band in _band_lines_by_y(zone_lines):
        ordered = sorted(band, key=lambda l: l.bbox.x0)
        spans: List[TextSpan] = [span for line in ordered for span in line.spans]
        rows.append(Line(spans=spans, bbox=_merge_bbox(ordered)))
    return rows


def reconstruct_reading_order(layout: PageLayout) -> PageLayout:
    """Returns a new PageLayout whose `lines` are fully-merged logical rows
    (each row's spans concatenated left-to-right from its constituent
    original lines), ordered zone-then-row, with `reading_order` and
    `column_index` populated."""
    zones = _build_zones(layout.lines)
    for line in layout.lines:
        _assign_to_zone(line, zones).lines.append(line)

    # Zones are already produced top-to-bottom, left-to-right by
    # _build_zones, and each zone's own rows are already y-ordered by
    # _merge_rows_in_zone -- so appending zone-by-zone (not re-sorting
    # globally by y afterward) gives "read zone A fully, then zone B fully"
    # order, which is how a human reads two independent side-by-side lists
    # that don't row-align (the real fixture's two columns have 5 and 4
    # rows respectively -- there is no natural single shared row-for-row
    # order between them).
    merged_rows: List[Line] = []
    for col_idx, zone in enumerate(zones):
        for row in _merge_rows_in_zone(zone.lines):
            row.column_index = col_idx
            merged_rows.append(row)

    for i, row in enumerate(merged_rows):
        row.reading_order = i

    return PageLayout(
        page_number=layout.page_number,
        page_width=layout.page_width,
        page_height=layout.page_height,
        lines=merged_rows,
        source=layout.source,
    )
