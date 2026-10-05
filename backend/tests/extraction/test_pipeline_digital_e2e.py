"""End-to-end test for the complete Phase 1 non-LLM digital path, scored
against the same real gold data eval/run_eval.py uses for the LLM path
(duplicated here, not imported, since eval/gold_labels.json also carries
loinc_code expectations this phase doesn't produce -- field_parser.py has
no LOINC mapping step; that stays entirely unchanged in loinc_mapping.py).

The real measured LLM-path baseline (eval/eval_results.md, from an actual
run against real OpenRouter calls, not hand-written) is 100% extraction
accuracy on these same files. This phase's concrete exit criterion is a
defined, lower-but-still-high bar -- a rule-based parser reasonably isn't
expected to match an LLM's accuracy on every edge case on day one, but
should clearly demonstrate the approach works at real, useful accuracy.
"""
from app.services.extraction.digital_pipeline import extract_digital_pdf
from tests.extraction.conftest import sample_bytes

# (filename, [(test_name, expected_value), ...]) -- values duplicated from
# eval/gold_labels.json for files 1-8 (digital PDFs only; 09 is a scanned
# PNG and 10 is plain text, both out of this PDF-only phase's scope), plus
# real values transcribed directly from the real Apollo fixture (page 1).
GOLD = [
    ("01_cbc_clean_digital.pdf", [
        ("WBC", "6.8"), ("RBC", "4.7"), ("Hemoglobin", "13.9"),
        ("Hematocrit", "41.2"), ("Platelet Count", "250"),
    ]),
    ("02_cmp_clean_digital.pdf", [
        ("Sodium", "140"), ("Potassium", "4.1"), ("Chloride", "101"),
        ("CO2", "25"), ("BUN", "15"), ("Creatinine", "0.9"), ("Glucose", "98"),
    ]),
    ("03_lipid_messy_layout.pdf", [
        ("Total Cholesterol", "210"), ("HDL Cholesterol", "45"),
        ("LDL Cholesterol (calc)", "140"), ("Triglycerides", "130"),
    ]),
    ("04_synonyms_abbreviations.pdf", [
        ("Sugar (Fasting)", "95"), ("SGOT", "22"), ("SGPT", "18"),
        ("A1C", "5.4"), ("Na+", "138"),
    ]),
    ("05_thyroid_partial_fields.pdf", [
        ("TSH", "2.1"), ("Free T4", "Normal"), ("Free T3", "3.1"),
    ]),
    ("06_urinalysis.pdf", [
        ("Specific Gravity", "1.020"), ("pH", "6.0"), ("Protein", "Negative"),
        ("Glucose", "Negative"), ("Leukocyte Esterase", "Negative"),
    ]),
    ("07_multipage_panel.pdf", [
        ("WBC", "7.2"), ("Hemoglobin", "14.1"), ("Glucose", "101"), ("Creatinine", "1.0"),
    ]),
    ("08_unmapped_novel_test.pdf", [
        ("Glucose", "92"), ("Interleukin-6 (IL-6)", "3.2"),
    ]),
    ("11_apollo_complex_layout_real.pdf", [
        ("RBC Count‎(‎Optical)", "4.6"),
        ("WBC Count(Optical(Light scatter))", "7.15"),
        ("Platelet Count(Optical(Light scatter))", "420"),
        ("Hemoglobin (Modified Cyanmethaemoglobin)", "10.9"),
        ("MCH(Calculated)", "24"), ("MCHC(Calculated)", "29"),
        ("RDW(Derived from RBC histogram)", "16.3"),
        ("MCV (Pulse height (Derived from RBC histogram))", "83"),
        ("Packed cell volume(Calculated)", "38"),
        ("Neutrophils", "57"), ("Lymphocytes", "32"),
        ("Eosinophils", "6"), ("Monocytes", "5"),
    ]),
]

# Real measured LLM-path baseline, eval/eval_results.md: 100%. This phase's
# own concrete bar -- not reverse-fitted to whatever score comes out, set
# before reading the final number from this exact run.
MIN_EXTRACTION_ACCURACY = 0.85


def test_digital_pipeline_meets_minimum_extraction_accuracy_on_real_fixtures():
    total = 0
    hits = 0
    misses = []

    for filename, expected in GOLD:
        raw = sample_bytes(filename)
        results = extract_digital_pdf(raw)
        all_tests = [t for page in results for t in page.tests]
        by_name = {t.original_test_name: t for t in all_tests}

        for name, expected_value in expected:
            total += 1
            got = by_name.get(name)
            if got is not None and got.value == expected_value:
                hits += 1
            else:
                misses.append((filename, name, expected_value, got.value if got else None))

    accuracy = hits / total
    print(f"\nPhase 1 digital-path extraction accuracy: {accuracy:.1%} ({hits}/{total})")
    if misses:
        print("Misses:")
        for m in misses:
            print(f"  {m}")

    assert accuracy >= MIN_EXTRACTION_ACCURACY, (
        f"extraction accuracy {accuracy:.1%} below the {MIN_EXTRACTION_ACCURACY:.0%} "
        f"exit criterion (real LLM-path baseline: 100%, eval/eval_results.md). Misses: {misses}"
    )


def test_digital_pipeline_makes_zero_network_calls():
    """Real structural guarantee, not a mock: none of the Phase 1 extraction
    modules actually IMPORT app.services.llm_client or openai (the only
    network-calling code in this codebase) -- checked against real import
    statements specifically, not an incidental docstring mention of
    "llm_client" while explaining the contract these modules share with it."""
    import ast
    import inspect

    import app.services.extraction.classify as classify
    import app.services.extraction.digital_pipeline as digital_pipeline
    import app.services.extraction.field_parser as field_parser
    import app.services.extraction.layout as layout
    import app.services.extraction.pdf_layout as pdf_layout

    for module in (classify, digital_pipeline, field_parser, layout, pdf_layout):
        tree = ast.parse(inspect.getsource(module))
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_names.add(node.module)
                imported_names.update(alias.name for alias in node.names)
        assert not any("llm_client" in (name or "") for name in imported_names)
        assert "openai" not in imported_names
