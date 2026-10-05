"""Regression tests for the real bug found by reading the Apollo fixture:
naive `get_text("text")` dumping jumbled column order, separating a
label/value from its own reference range by hundreds of characters. These
tests assert the FIX directly and concretely: each row's label, value,
range, and flag -- which are frequently separate PyMuPDF line objects in
this real, complex layout -- must end up merged into the SAME reconstructed
row.
"""
from app.services.extraction.layout import reconstruct_reading_order
from app.services.extraction.pdf_layout import extract_pdf_layouts
from tests.extraction.conftest import sample_bytes


def _row_texts(page_number: int = 1):
    layouts = extract_pdf_layouts(sample_bytes("11_apollo_complex_layout_real.pdf"))
    layout = next(l for l in layouts if l.page_number == page_number)
    ordered = reconstruct_reading_order(layout)
    return [row.text for row in ordered.lines]


def _find_row_containing(rows, needle):
    matches = [r for r in rows if needle in r]
    assert matches, f"no reconstructed row contains {needle!r}; rows were: {rows}"
    return matches[0]


def test_mch_label_value_range_and_flag_merge_into_one_row():
    rows = _row_texts()
    row = _find_row_containing(rows, "MCH(Calculated)")
    assert "24" in row and "pg" in row
    assert "26 - 32 pg" in row
    assert "Normal" in row


def test_neutrophils_row_does_not_absorb_the_unrelated_blood_indices_column():
    rows = _row_texts()
    row = _find_row_containing(rows, "Neutrophils")
    assert "57" in row and "%" in row
    assert "MCH" not in row
    assert "Calculated" not in row


def test_hemoglobin_range_and_its_separately_positioned_unit_merge():
    """Regression for the specific real case where a range ('11.5 - 16.5')
    and its own unit ('gm/dl') are two vertically-stacked, non-overlapping
    PyMuPDF lines rather than one -- must still merge into Hemoglobin's row."""
    rows = _row_texts()
    row = _find_row_containing(rows, "Hemoglobin (Modified Cyanmethaemoglobin)")
    assert "10.9" in row
    assert "11.5 - 16.5" in row
    assert "Normal" in row


def test_patient_demographics_precede_the_two_column_panel_in_reading_order():
    """This is the literal bug found in the attached real document: a plain
    text dump put the Differential Leucocyte count percentages BEFORE the
    patient demographics, with their reference ranges stranded at the very
    end. Reading order must put demographics first."""
    rows = _row_texts()
    demographics_idx = next(i for i, r in enumerate(rows) if "Mrs. PAVITHRA M" in r)
    neutrophils_idx = next(i for i, r in enumerate(rows) if "Neutrophils" in r)
    assert demographics_idx < neutrophils_idx


def test_reading_order_and_column_index_are_populated():
    layouts = extract_pdf_layouts(sample_bytes("11_apollo_complex_layout_real.pdf"))
    ordered = reconstruct_reading_order(layouts[0])
    assert [row.reading_order for row in ordered.lines] == list(range(len(ordered.lines)))
    assert all(row.column_index is not None for row in ordered.lines)


def test_simple_single_column_pdf_still_reads_in_order():
    """Reading-order reconstruction must be a no-op (order-preserving) for
    the simple, already-single-column synthetic fixtures."""
    layouts = extract_pdf_layouts(sample_bytes("01_cbc_clean_digital.pdf"))
    ordered = reconstruct_reading_order(layouts[0])
    rows = [row.text for row in ordered.lines]
    wbc_idx = next(i for i, r in enumerate(rows) if r.startswith("WBC"))
    platelet_idx = next(i for i, r in enumerate(rows) if r.startswith("Platelet"))
    assert wbc_idx < platelet_idx
