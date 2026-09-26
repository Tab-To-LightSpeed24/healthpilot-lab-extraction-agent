import csv
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.loinc import LoincCode, LoincAlias
from app.services.normalization import build_alias_index, _clean

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "loinc_lab_active.csv"
OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "data" / "loinc_alias_overrides.json"

_alias_index_cache: dict[str, dict] | None = None


def load_loinc_records() -> list[dict]:
    """Reads the ~62k Laboratory-class, ACTIVE LOINC codes derived from the
    official loinc.org release by scripts/build_loinc_data.py. See that
    script's docstring for provenance -- the raw ~1GB release itself is not
    checked into this repo, only this already-filtered, already-derived CSV
    is."""
    records = []
    with open(DATA_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            records.append({
                "loinc_num": row["loinc_num"],
                "long_common_name": row["long_common_name"],
                "shortname": row["shortname"] or None,
                "component": row["component"] or None,
                "property": row["property"] or None,
                "time_aspect": row["time_aspect"] or None,
                "system": row["system"] or None,
                "scale_type": row["scale_type"] or None,
                "method_type": row["method_type"] or None,
                "class": row["class"] or None,
                "example_units": row["example_units"] or None,
                "common_test_rank": int(row["common_test_rank"] or 0),
                "aliases": [a for a in row["aliases"].split("|") if a] if row["aliases"] else [],
            })
    return records


def seed_loinc_table(db: Session) -> int:
    """Bulk-inserts LoincCode rows only if the table is empty (idempotent --
    avoids re-inserting ~62k rows on every restart). Aliases are refreshed
    every call regardless, since regenerating loinc_lab_active.csv (e.g. a
    newer LOINC release, or a hand-added synonym) is the expected way to fix
    a mapping miss, and a stale persisted alias table would silently ignore
    that on an already-seeded database. Uses bulk_insert_mappings rather than
    one ORM object + db.add() per row -- at this row count (~62k codes,
    ~500k+ aliases) the per-row ORM path is minutes slower."""
    records = load_loinc_records()
    existing = db.query(LoincCode).count()

    if existing == 0:
        code_rows = [
            {
                "loinc_num": rec["loinc_num"],
                "long_common_name": rec["long_common_name"],
                "shortname": rec.get("shortname"),
                "component": rec.get("component"),
                "property": rec.get("property"),
                "time_aspect": rec.get("time_aspect"),
                "system": rec.get("system"),
                "scale_type": rec.get("scale_type"),
                "method_type": rec.get("method_type"),
                "class_": rec.get("class"),
                "example_units": rec.get("example_units"),
                "common_test_rank": rec.get("common_test_rank") or 0,
            }
            for rec in records
        ]
        db.bulk_insert_mappings(LoincCode, code_rows)
        db.commit()

    db.query(LoincAlias).delete()
    alias_rows = [
        {"loinc_num": rec["loinc_num"], "alias": alias}
        for rec in records
        for alias in rec.get("aliases", [])
    ]
    db.bulk_insert_mappings(LoincAlias, alias_rows)
    db.commit()

    return max(existing, len(records))


def load_alias_overrides() -> dict[str, str]:
    """Loads app/data/loinc_alias_overrides.json -> {cleaned alias: loinc_num}.
    See that file's own "_comment" field for why this exists."""
    with open(OVERRIDES_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    return {
        _clean(alias): loinc_num
        for loinc_num, aliases in raw.items()
        if loinc_num != "_comment"
        for alias in aliases
    }


def get_alias_index() -> dict[str, dict]:
    global _alias_index_cache
    if _alias_index_cache is None:
        records = load_loinc_records()
        index = build_alias_index(records)

        canonical_by_code = {rec["loinc_num"]: (rec["shortname"] or rec["long_common_name"]) for rec in records}
        for cleaned_alias, loinc_num in load_alias_overrides().items():
            if loinc_num in canonical_by_code:
                index[cleaned_alias] = {"loinc_num": loinc_num, "canonical_name": canonical_by_code[loinc_num]}

        _alias_index_cache = index
    return _alias_index_cache
