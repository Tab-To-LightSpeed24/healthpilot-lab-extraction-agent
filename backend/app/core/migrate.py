"""Runs Alembic migrations programmatically at app startup.

Handles one specific one-time transition: this project ran on
Base.metadata.create_all() (no migration history) before Alembic was wired
up, so the already-deployed database (and any local dev DB from before this
change) has the tables but no `alembic_version` row. On first startup after
this change, such a database is stamped at the "initial schema" revision
(which was authored to exactly match what create_all() had already produced)
before upgrading to head -- so it picks up only the migrations that
represent real, not-yet-applied changes (e.g. adding common_test_rank),
instead of re-running CREATE TABLE against tables that already exist. Every
startup after that first one is an ordinary `alembic upgrade head` with a
real version history behind it.
"""
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
PRE_ALEMBIC_BASELINE_REVISION = "3c23182bed6b"  # the "initial schema" migration


def _alembic_config(engine: Engine) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", str(engine.url).replace("%", "%%"))
    return cfg


def run_migrations(engine: Engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    cfg = _alembic_config(engine)

    # Reuse a connection from the engine the rest of the app already
    # connects with successfully, rather than having Alembic build a second,
    # independent connection from a re-serialized URL string -- see
    # alembic/env.py's run_migrations_online() for why that matters.
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection

        if "alembic_version" not in table_names and "documents" in table_names:
            logger.info(
                "Pre-existing database with no migration history found; "
                "stamping at %s before upgrading.",
                PRE_ALEMBIC_BASELINE_REVISION,
            )
            command.stamp(cfg, PRE_ALEMBIC_BASELINE_REVISION)

        command.upgrade(cfg, "head")
