import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import Base, normalize_database_url  # noqa: E402
from app.core.config import settings  # noqa: E402
import app.models  # noqa: E402,F401  (imported for its side effect: registers all models on Base.metadata)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The DB URL is owned by the app's own settings (env vars / .env), not
# alembic.ini, so local dev and Render both "just work" with whatever
# DATABASE_URL is already configured for the app -- UNLESS a caller already
# set a real URL on this Config object (app.core.migrate.run_migrations()
# does this, and so do this project's own tests, to target a specific
# engine rather than the process-wide default). Only fall back to settings
# when the config still has alembic.ini's unfilled placeholder, so plain
# `alembic upgrade head` from a terminal keeps working too.
_PLACEHOLDER_URL = "driver://user:pass@localhost/dbname"
if config.get_main_option("sqlalchemy.url") in (None, "", _PLACEHOLDER_URL):
    config.set_main_option("sqlalchemy.url", normalize_database_url(settings.database_url))

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
