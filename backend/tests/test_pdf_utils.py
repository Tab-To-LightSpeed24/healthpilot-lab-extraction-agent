import pymupdf as fitz

from app.services.pdf_utils import load_pages


def _make_digital_pdf_bytes(page_texts: list[str]) -> bytes:
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def test_load_pages_digital_pdf_extracts_text_and_image_per_page():
    raw = _make_digital_pdf_bytes(["Hemoglobin 13.5 g/dL", "Glucose 95 mg/dL"])
    pages = load_pages(raw, "application/pdf")

    assert len(pages) == 2
    assert pages[0].page_number == 1
    assert pages[1].page_number == 2
    assert "Hemoglobin" in pages[0].text
    assert "13.5" in pages[0].text
    assert "Glucose" in pages[1].text
    # every page must have a rasterized image for the vision model, real PNG bytes
    for p in pages:
        assert p.image_png.startswith(b"\x89PNG")
        assert len(p.image_png) > 100


def test_load_pages_image_input_has_no_text_layer_but_has_image():
    # Render a page to a PNG first (simulates a scanned/photographed report).
    raw_pdf = _make_digital_pdf_bytes(["Scanned-looking page"])
    doc = fitz.open(stream=raw_pdf, filetype="pdf")
    png_bytes = doc[0].get_pixmap().tobytes("png")
    doc.close()

    pages = load_pages(png_bytes, "image/png")
    assert len(pages) == 1
    assert pages[0].text is None
    assert pages[0].image_png.startswith(b"\x89PNG")


def test_load_pages_rejects_unsupported_content_type():
    import pytest

    with pytest.raises(ValueError):
        load_pages(b"not a real file", "application/zip")


def test_load_pages_plain_text_report():
    raw = b"Hemoglobin: 13.5 g/dL\nGlucose: 95 mg/dL\n"
    pages = load_pages(raw, "text/plain")
    assert len(pages) == 1
    assert "Hemoglobin" in pages[0].text
