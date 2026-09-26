from app.services.normalization import standardize_unit


def test_normalizes_common_gm_dl_variants():
    assert standardize_unit("gm/dl") == "g/dL"
    assert standardize_unit("g/dl") == "g/dL"
    assert standardize_unit("G/DL") == "g/dL"


def test_normalizes_cell_count_variants():
    assert standardize_unit("mill/cumm") == "mill/cmm"
    assert standardize_unit("mill/mm3") == "mill/cmm"
    assert standardize_unit("million/cumm") == "mill/cmm"


def test_strips_ocr_flag_artifacts():
    assert standardize_unit("g/dL [H]") == "g/dL"
    assert standardize_unit("% [L]") == "%"


def test_passes_through_unrecognized_units_unchanged():
    assert standardize_unit("ng/mL") == "ng/mL"
    assert standardize_unit("mmol/L") == "mmol/L"


def test_handles_none_and_empty():
    assert standardize_unit(None) is None
    assert standardize_unit("") is None
    assert standardize_unit("   ") is None
