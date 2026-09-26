import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.loinc import LoincCode, LoincAlias
from app.services.normalization import build_alias_index

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "loinc_subset.json"

_alias_index_cache: dict[str, dict] | None = None


def load_loinc_records() -> list[dict]:
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def seed_loinc_table(db: Session) -> int:
    """Idempotent: only inserts if the table is empty. Returns row count after seeding."""
    existing = db.query(LoincCode).count()
    if existing > 0:
        return existing

    records = load_loinc_records()
    for rec in records:
        row = LoincCode(
            loinc_num=rec["loinc_num"],
            long_common_name=rec["long_common_name"],
            shortname=rec.get("shortname"),
            component=rec.get("component"),
            property=rec.get("property"),
            time_aspect=rec.get("time_aspect"),
            system=rec.get("system"),
            scale_type=rec.get("scale_type"),
            method_type=rec.get("method_type"),
            class_=rec.get("class"),
            example_units=rec.get("example_units"),
        )
        db.add(row)
        for alias in rec.get("aliases", []):
            db.add(LoincAlias(loinc_num=rec["loinc_num"], alias=alias))
    db.commit()
    return len(records)


def get_alias_index() -> dict[str, dict]:
    global _alias_index_cache
    if _alias_index_cache is None:
        _alias_index_cache = build_alias_index(load_loinc_records())
    return _alias_index_cache
