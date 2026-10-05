from app.services.extraction.field_parser import parse_page, parse_row
from app.services.extraction.layout import reconstruct_reading_order
from app.services.extraction.pdf_layout import extract_pdf_layouts
from app.services.extraction.schemas import BBox, Line, TextSpan
from tests.extraction.conftest import sample_bytes


def _line(*texts: str) -> Line:
    spans = [TextSpan(text=t, bbox=BBox(x0=0, y0=0, x1=1, y1=1)) for t in texts]
    return Line(spans=spans, bbox=BBox(x0=0, y0=0, x1=1, y1=1))


def test_parse_row_single_fragment_with_internal_padding():
    """The simple synthetic fixtures' whole row is one PyMuPDF line with
    internal multi-space padding."""
    row = _line("WBC                      6.8         10*3/uL     4.0-11.0")
    test = parse_row(row)
    assert test is not None
    assert test.original_test_name == "WBC"
    assert test.value == "6.8"
    assert test.unit == "10*3/uL"
    assert test.reference_range == "4.0-11.0"


def test_parse_row_multi_fragment_row_from_real_layout():
    """The real Apollo fixture's row is several separate original lines,
    already merged by layout.py before field_parser ever sees them."""
    row = _line("MCH(Calculated)", "24 pg", "26 - 32 pg", "Normal")
    test = parse_row(row)
    assert test is not None
    assert test.original_test_name == "MCH(Calculated)"
    assert test.value == "24"
    assert test.unit == "pg"
    assert test.reference_range == "26 - 32 pg"
    assert test.flag == "Normal"


def test_parse_row_qualifier_value_with_no_unit_or_range():
    row = _line("Free T4", "Normal")
    test = parse_row(row)
    assert test is not None
    assert test.value == "Normal"
    assert test.unit is None
    assert test.reference_range is None


def test_parse_row_qualifier_value_negative():
    row = _line("Protein", "Negative")
    test = parse_row(row)
    assert test is not None
    assert test.value == "Negative"


def test_parse_row_leading_qualifier_numeric_value():
    row = _line("RA FACTOR", "<11.2 IU/mL")
    test = parse_row(row)
    assert test is not None
    assert test.value == "<11.2"
    assert test.unit == "IU/mL"


def test_parse_row_rejects_patient_demographics_label():
    """Regression guard: 'LRN : 12109545' is structurally value-shaped
    (numeric-leading second token) but must never be emitted as a test."""
    row = _line("LRN", ": 12109545")
    assert parse_row(row) is None


def test_parse_row_rejects_prose_paragraph():
    row = _line("Info- Red Blood Count is the amount of Red Blood Cells per unit of blood")
    assert parse_row(row) is None


def test_parse_row_rejects_row_with_no_value():
    row = _line("Patient Name", ": Mrs. PAVITHRA M")
    assert parse_row(row) is None


def test_specimen_declared_once_propagates_to_every_row_on_the_page():
    """Mirrors the LLM path's explicit prompt rule for the same requirement
    (app/services/llm_client.py's EXTRACTION_PROMPT): a specimen stated once
    for a page applies to every test row beneath it."""
    layouts = extract_pdf_layouts(sample_bytes("06_urinalysis.pdf"))
    ordered = reconstruct_reading_order(layouts[0])
    result = parse_page(ordered)

    names = {t.original_test_name: t for t in result.tests}
    assert len(result.tests) >= 4
    for test in result.tests:
        assert test.specimen == "Urine", f"{test.original_test_name} did not inherit the page's specimen"


def test_parse_page_on_real_complex_layout_produces_expected_tests():
    layouts = extract_pdf_layouts(sample_bytes("11_apollo_complex_layout_real.pdf"))
    ordered = reconstruct_reading_order(layouts[0])
    result = parse_page(ordered)

    by_name = {t.original_test_name: t for t in result.tests}
    assert by_name["RBC Count‎(‎Optical)"].value == "4.6"
    assert by_name["MCH(Calculated)"].value == "24"
    assert by_name["MCH(Calculated)"].unit == "pg"
    assert by_name["Hemoglobin (Modified Cyanmethaemoglobin)"].value == "10.9"
    assert by_name["Neutrophils"].value == "57"


def _stacked_rows(*specs):
    """specs: (text, y0, y1) tuples -> rows in a single column, label-above-value style."""
    rows = []
    for text, y0, y1 in specs:
        box = BBox(x0=0.1, y0=y0, x1=0.5, y1=y1)
        rows.append(Line(spans=[TextSpan(text=text, bbox=box)], bbox=box, column_index=0))
    from app.services.extraction.schemas import PageLayout
    return PageLayout(page_number=1, page_width=1, page_height=1, lines=rows)


def test_label_row_followed_by_value_row_is_paired():
    layout = _stacked_rows(("RPR test for syphilis - BLOOD", 0.20, 0.215), ("Non-Reactive", 0.225, 0.24))
    tests = parse_page(layout).tests
    assert [(t.original_test_name, t.value) for t in tests] == [("RPR test for syphilis - BLOOD", "Non-Reactive")]


def test_wrapped_two_line_label_is_joined_before_pairing():
    layout = _stacked_rows(
        ("TPHA-TREPONEMA PALLIDIUM", 0.208, 0.220),
        ("HEMAGGLUTINATION ASSAY", 0.223, 0.235),
        ("NEGATIVE", 0.240, 0.252),
    )
    tests = parse_page(layout).tests
    assert tests[0].original_test_name == "TPHA-TREPONEMA PALLIDIUM HEMAGGLUTINATION ASSAY"
    assert tests[0].value == "NEGATIVE"


def test_distant_title_is_not_glued_onto_the_label_below_it():
    layout = _stacked_rows(
        ("ANTI CCP (CYCLIC CITRULLINATED PEPTIDE)", 0.173, 0.185),
        ("ANTI CCP (CYCLIC CITRULLINATED PEPTIDE):", 0.221, 0.233),
        ("<0.5", 0.238, 0.250),
    )
    tests = parse_page(layout).tests
    assert [(t.original_test_name, t.value) for t in tests] == [("ANTI CCP (CYCLIC CITRULLINATED PEPTIDE)", "<0.5")]


def test_value_row_with_unit_keeps_the_unit():
    layout = _stacked_rows(("RA FACTOR", 0.208, 0.220), ("<11.2  IU/mL", 0.225, 0.237))
    test = parse_page(layout).tests[0]
    assert (test.original_test_name, test.value, test.unit) == ("RA FACTOR", "<11.2", "IU/mL")


def test_prose_followed_by_a_number_row_is_not_paired():
    layout = _stacked_rows(("Info- An RF test is used to help diagnose rheumatoid", 0.20, 0.21), ("5 mg", 0.22, 0.23))
    assert parse_page(layout).tests == []


def test_real_apollo_pages_2_and_3_extract_their_stacked_cards():
    layouts = extract_pdf_layouts(sample_bytes("11_apollo_complex_layout_real.pdf"))
    p2 = parse_page(reconstruct_reading_order(layouts[1])).tests
    p3 = parse_page(reconstruct_reading_order(layouts[2])).tests
    assert [(t.original_test_name, t.value) for t in p2] == [("ANTI CCP (CYCLIC CITRULLINATED PEPTIDE)", "<0.5")]
    assert {(t.original_test_name, t.value) for t in p3} == {
        ("RPR test for syphilis - BLOOD", "Non-Reactive"),
        ("RA FACTOR", "<11.2"),
        ("TPHA-TREPONEMA PALLIDIUM HEMAGGLUTINATION ASSAY", "NEGATIVE"),
    }
