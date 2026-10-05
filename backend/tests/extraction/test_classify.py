from app.services.extraction.classify import classify_page
from app.services.extraction.pdf_layout import extract_pdf_layouts
from tests.extraction.conftest import sample_bytes


def test_classifies_clean_digital_pdf_as_digital():
    layouts = extract_pdf_layouts(sample_bytes("01_cbc_clean_digital.pdf"))
    classification = classify_page(layouts[0])
    assert classification.path == "digital"


def test_classifies_real_complex_layout_pdf_as_digital():
    layouts = extract_pdf_layouts(sample_bytes("11_apollo_complex_layout_real.pdf"))
    for layout in layouts:
        assert classify_page(layout).path == "digital"


def test_classifies_blank_page_as_scanned():
    from app.services.extraction.schemas import PageLayout

    empty = PageLayout(page_number=1, page_width=600, page_height=800, lines=[])
    assert classify_page(empty).path == "scanned"
