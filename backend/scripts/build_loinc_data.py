"""One-off ETL: derives our app's compact LOINC reference file from the full
official LOINC release downloaded from loinc.org.

The full release (~1GB, thousands of files under Loinc_2.83/) is NOT checked
into this repository -- only this script's output is. Re-run this whenever
the release is updated:

    python backend/scripts/build_loinc_data.py --source "Loinc_2.83/LoincTable/Loinc.csv"

Filters to Laboratory-class (CLASSTYPE=1), ACTIVE codes only, since that's
what this project's extraction pipeline maps against. Keeps original field
values unchanged (only selects a subset of rows/columns), per the LOINC
license's requirement not to alter field contents.

Also carries COMMON_TEST_RANK through: at full-table scale, the same
casual abbreviation (e.g. "Hgb") is legitimately listed in RELATEDNAMES2 for
several different specific LOINC codes (routine Hemoglobin vs. a rare
Hemoglobin-A-by-electrophoresis assay, for example) -- COMMON_TEST_RANK is
LOINC's own popularity signal, used by normalization.build_alias_index to
prefer the commonly-ordered code over an obscure one sharing the same
alias, rather than silently picking whichever happened to be seen first.

--core-only mode (recommended for Render free tier / 512 MB RAM hosts):
    Restricts output to codes with COMMON_TEST_RANK > 0.  LOINC assigns this
    rank to the roughly 3-4k tests that account for the vast majority of
    real-world lab orders.  Omitting the other ~58k rarely-ordered codes cuts
    the seeding time from minutes to seconds and keeps the in-process alias-
    index cache well inside the 512 MB cap.

    python backend/scripts/build_loinc_data.py \\
        --source "Loinc_2.83/LoincTable/Loinc.csv" --core-only
"""
import argparse
import csv
import sys
from pathlib import Path

OUTPUT_COLUMNS = [
    "loinc_num", "long_common_name", "shortname", "component", "property",
    "time_aspect", "system", "scale_type", "method_type", "class",
    "example_units", "common_test_rank", "aliases",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Path to the official Loinc.csv (full table)")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "app" / "data" / "loinc_lab_active.csv"),
    )
    parser.add_argument(
        "--core-only",
        action="store_true",
        default=False,
        help=(
            "Restrict output to codes with COMMON_TEST_RANK > 0 (LOINC's own "
            "'commonly ordered' signal). Cuts the table from ~62k codes to "
            "~3-4k codes, dramatically reducing memory usage at startup and in "
            "the in-process alias-index cache. Use this mode for production "
            "deployments on memory-constrained hosts (e.g. Render free tier, "
            "512 MB). The full set is still available by omitting this flag."
        ),
    )
    args = parser.parse_args()

    csv.field_size_limit(sys.maxsize)
    kept = 0
    seen_total = 0
    skipped_unranked = 0

    with open(args.source, encoding="utf-8-sig", newline="") as src, \
         open(args.out, "w", encoding="utf-8", newline="") as dst:
        reader = csv.DictReader(src)
        writer = csv.writer(dst)
        writer.writerow(OUTPUT_COLUMNS)

        for row in reader:
            seen_total += 1
            if row.get("CLASSTYPE") != "1" or row.get("STATUS") != "ACTIVE":
                continue

            # --core-only: keep only codes that LOINC itself marks as
            # "commonly ordered" (COMMON_TEST_RANK > 0).  This is the primary
            # lever for staying inside the 512 MB Render free-tier RAM cap.
            rank = int(row.get("COMMON_TEST_RANK") or 0)
            if args.core_only and rank == 0:
                skipped_unranked += 1
                continue

            aliases = [a.strip() for a in row.get("RELATEDNAMES2", "").split(";") if a.strip()]
            writer.writerow([
                row["LOINC_NUM"],
                row["LONG_COMMON_NAME"],
                row["SHORTNAME"],
                row["COMPONENT"],
                row["PROPERTY"],
                row["TIME_ASPCT"],
                row["SYSTEM"],
                row["SCALE_TYP"],
                row["METHOD_TYP"],
                row["CLASS"],
                row.get("EXAMPLE_UNITS", ""),
                rank,
                "|".join(aliases),
            ])
            kept += 1

    if args.core_only:
        print(
            f"Scanned {seen_total} LOINC rows, kept {kept} ranked Laboratory/ACTIVE codes "
            f"(skipped {skipped_unranked} unranked) -> {args.out}"
        )
    else:
        print(f"Scanned {seen_total} LOINC rows, kept {kept} Laboratory/ACTIVE rows -> {args.out}")


if __name__ == "__main__":
    main()
