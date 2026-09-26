import csv
import json
from pathlib import Path
from typing import Iterator

from sqlalchemy.orm import Session

from app.models.loinc import LoincCode, LoincAlias
from app.services.normalization import build_alias_index, _clean

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "loinc_lab_active.csv"
OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "data" / "loinc_alias_overrides.json"

_alias_index_cache: dict[str, dict] | None = None

# Render's free tier caps a web service at 512MB total. Materializing all
# ~62k codes and their ~500k+ aliases as two separate in-memory Python lists
# (one from load_loinc_records(), a second flat "alias_rows" list built from
# it) actually OOM-killed a real deploy -- confirmed directly in Render's own
# logs ("Out of memory (used over 512Mi)"), not a hypothetical concern.
# seed_loinc_table() below streams and inserts in small chunks instead, so
# peak memory is bounded by CHUNK_SIZE regardless of table size.
CHUNK_SIZE = 3000


def _iter_loinc_rows() -> Iterator[dict]:
    """Streams one row at a time straight from the CSV -- never holds the
    whole ~62k-row table in memory. Use this (not load_loinc_records()) for
    anything that runs at process startup, where peak memory actually
    matters; load_loinc_records() below remains for callers (get_alias_index,
    tests) that already need the full set in memory anyway and don't run
    under the same startup memory pressure."""
    with open(DATA_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            yield {
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
            }


def load_loinc_records() -> list[dict]:
    """Reads the ~62k Laboratory-class, ACTIVE LOINC codes derived from the
    official loinc.org release by scripts/build_loinc_data.py. See that
    script's docstring for provenance -- the raw ~1GB release itself is not
    checked into this repo, only this already-filtered, already-derived CSV
    is."""
    return list(_iter_loinc_rows())


def _flush(db: Session, model, buffer: list[dict]) -> None:
    if buffer:
        db.bulk_insert_mappings(model, buffer)
        db.commit()
        buffer.clear()


def seed_loinc_table(db: Session) -> int:
    """Streams the CSV and inserts in chunks (idempotent for LoincCode rows
    -- avoids re-inserting ~62k rows on every restart). Aliases are refreshed
    every call regardless, since regenerating loinc_lab_active.csv (e.g. a
    newer LOINC release, or a hand-added synonym) is the expected way to fix
    a mapping miss, and a stale persisted alias table would silently ignore
    that on an already-seeded database. The CSV is read twice (once for
    codes, once for aliases) rather than kept as one big in-memory list --
    trading a second cheap disk read for a much lower memory ceiling."""
    existing = db.query(LoincCode).count()
    total = 0

    if existing == 0:
        buffer: list[dict] = []
        for rec in _iter_loinc_rows():
            buffer.append({
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
            })
            if len(buffer) >= CHUNK_SIZE:
                _flush(db, LoincCode, buffer)
        _flush(db, LoincCode, buffer)

    db.query(LoincAlias).delete()
    db.commit()

    alias_buffer: list[dict] = []
    for rec in _iter_loinc_rows():
        total += 1
        for alias in rec["aliases"]:
            alias_buffer.append({"loinc_num": rec["loinc_num"], "alias": alias})
            if len(alias_buffer) >= CHUNK_SIZE:
                _flush(db, LoincAlias, alias_buffer)
    _flush(db, LoincAlias, alias_buffer)

    return max(existing, total)


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
