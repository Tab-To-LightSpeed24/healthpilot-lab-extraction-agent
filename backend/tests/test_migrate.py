"""Regression tests for the Alembic bootstrap logic in app/core/migrate.py.

The critical scenario: this project ran on Base.metadata.create_all() (no
migration history) before Alembic was introduced, so the already-deployed
production database has the original tables but no alembic_version row.
run_migrations() must detect that and stamp the baseline revision before
upgrading, rather than either crashing (trying to re-create existing tables)
or silently skipping the real pending migration (e.g. adding
loinc_codes.common_test_rank)."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.core.migrate import run_migrations, BACKEND_DIR, PRE_ALEMBIC_BASELINE_REVISION


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def test_fresh_database_gets_full_schema_from_scratch(tmp_path):
    db_path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    run_migrations(engine)

    inspector = inspect(engine)
    assert {"documents", "observations", "loinc_codes", "loinc_aliases", "alembic_version"} <= set(
        inspector.get_table_names()
    )
    columns = {c["name"] for c in inspector.get_columns("loinc_codes")}
    assert "common_test_rank" in columns


def test_pre_existing_database_without_alembic_history_is_bootstrapped_correctly(tmp_path):
    """Reproduces the real production state: tables exist (created by the old
    Base.metadata.create_all() code path), but there's no alembic_version
    table at all, since Alembic didn't exist yet when they were created."""
    db_path = tmp_path / "existing.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    # Simulate: apply only the baseline migration (this is exactly what the
    # already-deployed schema looks like), then remove all trace of alembic
    # history to mimic a database that predates Alembic entirely.
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, PRE_ALEMBIC_BASELINE_REVISION)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))

    inspector = inspect(engine)
    assert "common_test_rank" not in {c["name"] for c in inspector.get_columns("loinc_codes")}

    run_migrations(engine)

    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("loinc_codes")}
    assert "common_test_rank" in columns, "the real pending migration must still be applied, not silently skipped"


def test_running_migrations_twice_is_a_safe_no_op(tmp_path):
    db_path = tmp_path / "idempotent.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    run_migrations(engine)
    run_migrations(engine)  # must not raise

    inspector = inspect(engine)
    assert "loinc_codes" in inspector.get_table_names()
