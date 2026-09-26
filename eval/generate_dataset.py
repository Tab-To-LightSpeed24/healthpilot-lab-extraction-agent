"""Generates the synthetic evaluation dataset described in eval/DATASET.md.

Each report is a real PDF or PNG file (not fixture text) written to
eval/sample_reports/, built with different layouts/formats so the pipeline is
genuinely exercised across digital-PDF, scanned-image, and messy-layout
inputs. eval/gold_labels.json holds the corresponding expected extraction +
LOINC mapping so eval/run_eval.py can score real pipeline output against it.
"""
import json
from pathlib import Path

import pymupdf as fitz

OUT_DIR = Path(__file__).resolve().parent / "sample_reports"
OUT_DIR.mkdir(exist_ok=True)

gold = []


def new_pdf():
    return fitz.open()


def add_page(doc, lines, y_start=72, line_height=16, fontsize=10, font="helv"):
    page = doc.new_page()
    y = y_start
    for line in lines:
        page.insert_text((50, y), line, fontsize=fontsize, fontname=font)
        y += line_height
    return page


def save(doc, name):
    path = OUT_DIR / name
    doc.save(str(path))
    doc.close()
    return path


# 1. Clean CBC panel, standard layout, digital PDF -----------------------------
doc = new_pdf()
add_page(doc, [
    "Acme Reference Laboratory",
    "Patient: Jane Doe   DOB: 1985-04-12   Report Date: 2026-09-20",
    "",
    "COMPLETE BLOOD COUNT (CBC)",
    "Test                      Result      Units       Reference Range",
    "WBC                      6.8         10*3/uL     4.0-11.0",
    "RBC                      4.7         10*6/uL     4.2-5.9",
    "Hemoglobin                13.9        g/dL        13.0-17.0",
    "Hematocrit                41.2        %           38.0-50.0",
    "Platelet Count            250         10*3/uL     150-400",
])
path = save(doc, "01_cbc_clean_digital.pdf")
gold.append({
    "file": path.name, "format": "digital_pdf", "quality": "clean",
    "expected": [
        {"original_test_name": "WBC", "value": "6.8", "unit": "10*3/uL", "loinc_code": "6690-2"},
        {"original_test_name": "RBC", "value": "4.7", "unit": "10*6/uL", "loinc_code": "789-8"},
        {"original_test_name": "Hemoglobin", "value": "13.9", "unit": "g/dL", "loinc_code": "718-7"},
        {"original_test_name": "Hematocrit", "value": "41.2", "unit": "%", "loinc_code": "4544-3"},
        {"original_test_name": "Platelet Count", "value": "250", "unit": "10*3/uL", "loinc_code": "777-3"},
    ],
})

# 2. Clean CMP panel ------------------------------------------------------------
doc = new_pdf()
add_page(doc, [
    "Metro Health Labs",
    "Patient: John Smith   MRN: 998211",
    "",
    "COMPREHENSIVE METABOLIC PANEL",
    "Sodium              140     mmol/L     136-145",
    "Potassium           4.1     mmol/L     3.5-5.1",
    "Chloride            101     mmol/L     98-107",
    "CO2                 25      mmol/L     22-29",
    "BUN                 15      mg/dL      7-20",
    "Creatinine          0.9     mg/dL      0.6-1.3",
    "Glucose             98      mg/dL      70-99",
])
path = save(doc, "02_cmp_clean_digital.pdf")
gold.append({
    "file": path.name, "format": "digital_pdf", "quality": "clean",
    "expected": [
        {"original_test_name": "Sodium", "value": "140", "unit": "mmol/L", "loinc_code": "2951-2"},
        {"original_test_name": "Potassium", "value": "4.1", "unit": "mmol/L", "loinc_code": "2823-3"},
        {"original_test_name": "Chloride", "value": "101", "unit": "mmol/L", "loinc_code": "2075-0"},
        {"original_test_name": "CO2", "value": "25", "unit": "mmol/L", "loinc_code": "2028-9"},
        {"original_test_name": "BUN", "value": "15", "unit": "mg/dL", "loinc_code": "3094-0"},
        {"original_test_name": "Creatinine", "value": "0.9", "unit": "mg/dL", "loinc_code": "2160-0"},
        {"original_test_name": "Glucose", "value": "98", "unit": "mg/dL", "loinc_code": "2345-7"},
    ],
})

# 3. Lipid panel with messy multi-column-ish spacing -----------------------------
doc = new_pdf()
add_page(doc, [
    "  LIPID  PANEL  --  QuickCare Diagnostics",
    "",
    "Total Cholesterol .......... 210 mg/dL      (Desirable: <200)",
    "HDL Cholesterol ............ 45  mg/dL      (Low: <40)",
    "LDL Cholesterol (calc) ..... 140 mg/dL      (Optimal: <100)",
    "Triglycerides .............. 130 mg/dL      (Normal: <150)",
], fontsize=9)
path = save(doc, "03_lipid_messy_layout.pdf")
gold.append({
    "file": path.name, "format": "digital_pdf", "quality": "messy_layout",
    "expected": [
        {"original_test_name": "Total Cholesterol", "value": "210", "unit": "mg/dL", "loinc_code": "2093-3"},
        {"original_test_name": "HDL Cholesterol", "value": "45", "unit": "mg/dL", "loinc_code": "2085-9"},
        {"original_test_name": "LDL Cholesterol (calc)", "value": "140", "unit": "mg/dL", "loinc_code": "13457-7"},
        {"original_test_name": "Triglycerides", "value": "130", "unit": "mg/dL", "loinc_code": "2571-8"},
    ],
})

# 4. Synonym/abbreviation-heavy report (tests alias + embedding mapping) --------
doc = new_pdf()
add_page(doc, [
    "Regional Path Associates - Chemistry Report",
    "",
    "Sugar (Fasting)      95    mg/dL     70-100",
    "SGOT                 22    U/L       10-40",
    "SGPT                 18    U/L       7-56",
    "A1C                  5.4   %         <5.7",
    "Na+                  138   mmol/L    136-145",
])
path = save(doc, "04_synonyms_abbreviations.pdf")
gold.append({
    "file": path.name, "format": "digital_pdf", "quality": "clean",
    "expected": [
        {"original_test_name": "Sugar (Fasting)", "value": "95", "unit": "mg/dL", "loinc_code": "2345-7"},
        {"original_test_name": "SGOT", "value": "22", "unit": "U/L", "loinc_code": "1920-8"},
        {"original_test_name": "SGPT", "value": "18", "unit": "U/L", "loinc_code": "1742-6"},
        {"original_test_name": "A1C", "value": "5.4", "unit": "%", "loinc_code": "4548-4"},
        {"original_test_name": "Na+", "value": "138", "unit": "mmol/L", "loinc_code": "2951-2"},
    ],
})

# 5. Thyroid panel with missing reference ranges/units for some rows ------------
doc = new_pdf()
add_page(doc, [
    "Endocrine Labs Inc",
    "",
    "TSH             2.1     uIU/mL    0.4-4.0",
    "Free T4         Normal",  # qualitative-only result, no unit/range
    "Free T3         3.1     pg/mL",  # no reference range given
])
path = save(doc, "05_thyroid_partial_fields.pdf")
gold.append({
    "file": path.name, "format": "digital_pdf", "quality": "partial_fields",
    "expected": [
        {"original_test_name": "TSH", "value": "2.1", "unit": "uIU/mL", "loinc_code": "3016-3"},
        {"original_test_name": "Free T4", "value": "Normal", "unit": None, "loinc_code": "3024-7"},
        {"original_test_name": "Free T3", "value": "3.1", "unit": "pg/mL", "loinc_code": "3053-6"},
    ],
})

# 6. Urinalysis panel with specimen context (tests specimen-aware mapping) ------
doc = new_pdf()
add_page(doc, [
    "Urinalysis Report - Specimen: Urine",
    "",
    "Specific Gravity     1.020",
    "pH                   6.0",
    "Protein              Negative",
    "Glucose              Negative",
    "Leukocyte Esterase   Negative",
])
path = save(doc, "06_urinalysis.pdf")
gold.append({
    "file": path.name, "format": "digital_pdf", "quality": "clean",
    "expected": [
        {"original_test_name": "Specific Gravity", "value": "1.020", "unit": None, "loinc_code": "5811-5"},
        {"original_test_name": "pH", "value": "6.0", "unit": None, "loinc_code": "5803-2"},
        {"original_test_name": "Protein", "value": "Negative", "unit": None, "loinc_code": "5804-0"},
        {"original_test_name": "Glucose", "value": "Negative", "unit": None, "loinc_code": "5792-7"},
        {"original_test_name": "Leukocyte Esterase", "value": "Negative", "unit": None, "loinc_code": "5799-2"},
    ],
})

# 7. Multi-page report: CBC on page 1, CMP on page 2 (tests page traceability) --
doc = new_pdf()
add_page(doc, [
    "Multi-Page Panel Report - Page 1 of 2",
    "COMPLETE BLOOD COUNT",
    "WBC          7.2    10*3/uL   4.0-11.0",
    "Hemoglobin   14.1    g/dL      13.0-17.0",
])
add_page(doc, [
    "Multi-Page Panel Report - Page 2 of 2",
    "COMPREHENSIVE METABOLIC PANEL",
    "Glucose      101    mg/dL     70-99",
    "Creatinine   1.0    mg/dL     0.6-1.3",
])
path = save(doc, "07_multipage_panel.pdf")
gold.append({
    "file": path.name, "format": "digital_pdf", "quality": "multipage",
    "expected": [
        {"original_test_name": "WBC", "value": "7.2", "unit": "10*3/uL", "loinc_code": "6690-2", "page_number": 1},
        {"original_test_name": "Hemoglobin", "value": "14.1", "unit": "g/dL", "loinc_code": "718-7", "page_number": 1},
        {"original_test_name": "Glucose", "value": "101", "unit": "mg/dL", "loinc_code": "2345-7", "page_number": 2},
        {"original_test_name": "Creatinine", "value": "1.0", "unit": "mg/dL", "loinc_code": "2160-0", "page_number": 2},
    ],
})

# 8. Report containing a test NOT in the curated LOINC subset (must flag review) -
doc = new_pdf()
add_page(doc, [
    "Specialty Diagnostics Panel",
    "",
    "Glucose                  92     mg/dL     70-99",
    "Interleukin-6 (IL-6)     3.2    pg/mL     <5.0",  # deliberately not in curated subset
])
path = save(doc, "08_unmapped_novel_test.pdf")
gold.append({
    "file": path.name, "format": "digital_pdf", "quality": "clean",
    "expected": [
        {"original_test_name": "Glucose", "value": "92", "unit": "mg/dL", "loinc_code": "2345-7"},
        {"original_test_name": "Interleukin-6 (IL-6)", "value": "3.2", "unit": "pg/mL", "loinc_code": None,
         "expect_mapping_status_in": ["needs_review", "unmapped"]},
    ],
})

# 9. Scanned/photographed report simulated as a rasterized PNG image ------------
doc = new_pdf()
add_page(doc, [
    "Coagulation Panel (scanned copy)",
    "PT      13.2   sec    11.0-13.5",
    "INR     1.0",
    "aPTT    29.0   sec    25.0-35.0",
], fontsize=12)
pix = doc[0].get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72))
png_path = OUT_DIR / "09_coag_scanned_image.png"
pix.save(str(png_path))
doc.close()
gold.append({
    "file": png_path.name, "format": "image_png", "quality": "scanned_simulated",
    "expected": [
        {"original_test_name": "PT", "value": "13.2", "unit": "sec", "loinc_code": "5902-2"},
        {"original_test_name": "INR", "value": "1.0", "unit": None, "loinc_code": "6301-6"},
        {"original_test_name": "aPTT", "value": "29.0", "unit": "sec", "loinc_code": "14979-9"},
    ],
})

# 10. Plain text report (non-PDF text format) -----------------------------------
txt_path = OUT_DIR / "10_plain_text_report.txt"
txt_path.write_text(
    "Iron Studies Panel\n"
    "Iron        70    ug/dL    50-170\n"
    "Ferritin    120   ng/mL    20-250\n"
    "TIBC        300   ug/dL    250-450\n",
    encoding="utf-8",
)
gold.append({
    "file": txt_path.name, "format": "text_plain", "quality": "clean",
    "expected": [
        {"original_test_name": "Iron", "value": "70", "unit": "ug/dL", "loinc_code": "2498-4"},
        {"original_test_name": "Ferritin", "value": "120", "unit": "ng/mL", "loinc_code": "2276-4"},
        {"original_test_name": "TIBC", "value": "300", "unit": "ug/dL", "loinc_code": "2500-7"},
    ],
})

with open(Path(__file__).resolve().parent / "gold_labels.json", "w", encoding="utf-8") as f:
    json.dump(gold, f, indent=2)

print(f"Generated {len(gold)} sample reports into {OUT_DIR}")
