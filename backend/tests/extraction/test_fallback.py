import pymupdf as fitz
import pytest

from app.services.extraction.fallback import FallbackExtractor, FallbackUnavailable
from app.services.extraction.field_parser import parse_row
from app.services.extraction.schemas import BBox, Line, TextSpan
from app.services.pdf_utils import PageContent
from tests.extraction.conftest import sample_bytes


def _line(text: str) -> Line:
    box = BBox(x0=0, y0=0, x1=1, y1=1)
    return Line(spans=[TextSpan(text=text, bbox=box)], bbox=box)


def test_single_space_row_is_split_into_label_value_unit_range():
    test = parse_row(_line("Iron 70 ug/dL 50-170"))
    assert (test.original_test_name, test.value, test.unit, test.reference_range) == ("Iron", "70", "ug/dL", "50-170")


def test_single_space_row_keeps_spaced_range_and_flag():
    test = parse_row(_line("WBC 7.2 10*3/uL 4.0 - 11.0 H"))
    assert test.reference_range == "4.0 - 11.0"
    assert test.flag == "H"


def test_single_space_prose_with_a_number_is_not_mistaken_for_a_test():
    assert parse_row(_line("Multi-Page Panel Report - Page 1 of 2")) is None
    assert parse_row(_line("Some random sentence 5 times")) is None


def test_plain_text_report_is_parsed_without_any_llm():
    raw = sample_bytes("10_plain_text_report.txt")
    page = PageContent(page_number=1, text=raw.decode(), image_png=b"")
    result = FallbackExtractor(raw, "text/plain").extract(page)
    by_name = {t.original_test_name: t.value for t in result.tests}
    assert by_name == {"Iron": "70", "Ferritin": "120", "TIBC": "300"}


def test_digital_pdf_page_is_parsed_without_any_llm():
    raw = sample_bytes("01_cbc_clean_digital.pdf")
    page = PageContent(page_number=1, text="x", image_png=b"")
    result = FallbackExtractor(raw, "application/pdf").extract(page)
    assert {t.original_test_name for t in result.tests} >= {"WBC", "RBC", "Hemoglobin"}


def test_blank_scanned_pdf_page_raises_a_clear_unavailable_error():
    doc = fitz.open()
    doc.new_page()  # no text layer and nothing to OCR
    raw = doc.tobytes()
    page = PageContent(page_number=1, text=None, image_png=_blank_png())
    with pytest.raises(FallbackUnavailable, match="OCR"):
        FallbackExtractor(raw, "application/pdf").extract(page)


def test_undecodable_image_raises_a_clear_unavailable_error():
    with pytest.raises(FallbackUnavailable, match="OCR"):
        FallbackExtractor(b"png", "image/png").extract(PageContent(page_number=1, text=None, image_png=b"not an image"))


def test_image_without_page_image_is_unavailable():
    with pytest.raises(FallbackUnavailable, match="no page image"):
        FallbackExtractor(b"png", "image/png").extract(PageContent(page_number=1, text=None, image_png=b""))


def _blank_png() -> bytes:
    import numpy as np
    from tests.extraction.scan_fixtures import to_png
    return to_png(np.full((400, 600, 3), 255, dtype=np.uint8))


def test_ocr_junk_prefix_is_stripped_from_the_reference_range():
    box = BBox(x0=0, y0=0, x1=1, y1=1)
    row = Line(spans=[TextSpan(text=t, bbox=box) for t in ("WBC", "6.8", "10*3/uL", "= 4.0-11.0")], bbox=box)
    test = parse_row(row)
    assert test.reference_range == "4.0-11.0"


def test_label_wrapped_across_two_ocr_lines_is_rejoined():
    from app.services.extraction.field_parser import parse_page
    from app.services.extraction.schemas import PageLayout

    def row(*texts, y):
        box = BBox(x0=0.1, y0=y, x1=0.9, y1=y + 0.01)
        return Line(spans=[TextSpan(text=t, bbox=box, source="ocr") for t in texts], bbox=box, column_index=0)

    layout = PageLayout(page_number=1, page_width=1, page_height=1, source="ocr", lines=[
        row("MCV (Pulse height (Derived", y=0.30),
        row("from RBC histogram))", "83 fl", "75 - 95 fl", y=0.315),
    ])
    tests = parse_page(layout).tests
    assert [(t.original_test_name, t.value) for t in tests] == [("MCV (Pulse height (Derived from RBC histogram))", "83")]
