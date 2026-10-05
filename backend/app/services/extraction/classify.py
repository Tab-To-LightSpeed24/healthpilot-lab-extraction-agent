"""Stage 0: decide whether a page has a usable digital text layer.

Phase 1 implements only the "digital" path. A page classified "scanned"
here has no extraction path at all until a later phase adds OCR -- this
function's job is only to make that distinction explicit and testable, not
to handle the scanned case itself.
"""
from app.services.extraction.schemas import PageClassification, PageLayout

# Below this many extractable characters, treat the page as having no real
# text layer (e.g. a scanned/image-only PDF page PyMuPDF can't read text
# from) rather than attempting to parse what's likely noise.
MIN_DIGITAL_CHARS = 20


def classify_page(layout: PageLayout) -> PageClassification:
    total_chars = sum(len(line.text) for line in layout.lines)
    if total_chars >= MIN_DIGITAL_CHARS:
        return PageClassification(
            page_number=layout.page_number,
            path="digital",
            reason=f"{total_chars} extractable characters found",
        )
    return PageClassification(
        page_number=layout.page_number,
        path="scanned",
        reason=f"only {total_chars} extractable characters; likely image-based",
    )
