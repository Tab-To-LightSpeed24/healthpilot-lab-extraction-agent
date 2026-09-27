import csv
import json
import logging
from pathlib import Path
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.loinc import LoincCode, LoincAlias

logger = logging.getLogger(__name__)
from app.services.normalization import build_alias_index, _clean

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "loinc_lab_active.csv"
OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "data" / "loinc_alias_overrides.json"

_alias_index_cache: dict[str, dict] | None = None

# Render's free tier caps a web service at 512MB total. Materializing all
# ~62k codes and their ~1.7M aliases as two separate in-memory Python lists
# (one from load_loinc_records(), a second flat "alias_rows" list built from
# it) actually OOM-killed a real deploy -- confirmed directly in Render's own
# logs ("Out of memory (used over 512Mi)"), not a hypothetical concern.
# seed_loinc_table() below streams and inserts in chunks instead, so peak
# memory is bounded by CHUNK_SIZE regardless of table size.
#
# CHUNK_SIZE also controls how many DB round trips a real reseed takes. At
# ~1.7M alias rows, the previous value of 3000 meant ~565 commits -- fine
# against local SQLite on the same machine, but against a real networked
# Postgres (Render's free tier) each round trip's latency adds up: a real
# deploy hung for the full 15-minute platform timeout with the app never
# reaching uvicorn.run() at all, confirmed by Render's own deploy log
# showing "no open ports detected" for the entire window. Raised here to
# cut round trips by >10x; paired with the row-count short-circuit below so
# a normal restart (table already correct) does no bulk writes at all.
CHUNK_SIZE = 20000


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


def _count_source_rows() -> tuple[int, int]:
    """One cheap streaming pass over the CSV to get the row counts the DB
    should have if it's already fully and correctly seeded. Reading the
    26MB CSV this way takes low single-digit seconds; it's the DB round
    trips of an actual reseed that are expensive (see CHUNK_SIZE above), so
    doing this pass first to decide whether a reseed is even needed is a
    large net win whenever the table is already correct."""
    codes = 0
    aliases = 0
    for rec in _iter_loinc_rows():
        codes += 1
        aliases += len(rec["aliases"])
    return codes, aliases


def seed_loinc_table(db: Session) -> int:
    """Reseeds both LOINC tables from the CSV, but only when they don't
    already match it -- checked cheaply via row counts before touching the
    tables at all.

    This function used to unconditionally delete-and-reinsert both tables
    on every single startup, needed to fix a real prior bug: an earlier
    startup was interrupted partway through the chunked code inserts
    (before the memory fix above, an OOM kill), leaving loinc_codes with
    only *some* of the ~62k rows; the next startup's old "skip if non-empty"
    check saw existing > 0, skipped re-inserting codes, but still refreshed
    aliases -- which assumes every code in the CSV already exists -- and hit
    a real ForeignKeyViolation for every alias whose code was never actually
    inserted. Confirmed by reproducing it locally against a real Postgres
    left in exactly that partially-seeded state (see
    test_seed_loinc_table_recovers_from_a_partially_seeded_prior_run).

    Unconditionally resyncing fixed that, but introduced a new real bug:
    with ~1.7M alias rows, a full delete-and-reinsert against a real
    networked Postgres (not local SQLite) took long enough that a live
    Render deploy hung for the platform's entire 15-minute startup timeout,
    never reaching uvicorn.run() at all -- confirmed directly in Render's
    deploy log (repeated "no open ports detected" for the full window,
    then "Timed Out"). A plain restart with an already-correct table has no
    reason to pay that cost every time.

    Fix: compare actual row counts in each table against what the CSV
    should produce. A match means the table is already fully, correctly
    seeded (including recovering the FK-violation scenario above, since a
    partial seed's counts can never coincidentally match both tables at
    once) -- skip the resync entirely. A mismatch (empty, partial, stale,
    or corrupted) still triggers a full resync exactly as before, so the
    original correctness guarantee is unchanged; only the redundant-reseed
    cost on top of it is."""
    logger.info("Checking LOINC table...")
    expected_codes, expected_aliases = _count_source_rows()
    actual_codes = db.query(LoincCode).count()
    actual_aliases = db.query(LoincAlias).count()
    if actual_codes == expected_codes and actual_aliases == expected_aliases:
        logger.info("LOINC table already up-to-date (%s codes, %s aliases)", actual_codes, actual_aliases)
        return actual_codes
    logger.info(
        "LOINC table mismatch (db: %s codes / %s aliases, csv: %s codes / %s aliases). Reseeding...",
        actual_codes, actual_aliases, expected_codes, expected_aliases,
    )

    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        # SQLite has no TRUNCATE; a bare DELETE is fine at this table size
        # for local dev/tests, where this path isn't the bottleneck anyway.
        db.query(LoincAlias).delete()
        db.query(LoincCode).delete()
    else:
        db.execute(text("TRUNCATE TABLE loinc_aliases, loinc_codes"))
    db.commit()

    code_buffer: list[dict] = []
    codes_inserted = 0
    for rec in _iter_loinc_rows():
        code_buffer.append({
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
        codes_inserted += 1
        if len(code_buffer) >= CHUNK_SIZE:
            _flush(db, LoincCode, code_buffer)
            logger.info("  ... %s LOINC codes inserted", codes_inserted)
    _flush(db, LoincCode, code_buffer)
    logger.info("LOINC codes done: %s rows", codes_inserted)

    total = 0
    alias_buffer: list[dict] = []
    aliases_inserted = 0
    for rec in _iter_loinc_rows():
        total += 1
        for alias in rec["aliases"]:
            alias_buffer.append({"loinc_num": rec["loinc_num"], "alias": alias})
            aliases_inserted += 1
            if len(alias_buffer) >= CHUNK_SIZE:
                _flush(db, LoincAlias, alias_buffer)
                logger.info("  ... %s LOINC aliases inserted", aliases_inserted)
    _flush(db, LoincAlias, alias_buffer)
    logger.info("LOINC aliases done: %s rows. Seeding complete.", aliases_inserted)

    return total


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
