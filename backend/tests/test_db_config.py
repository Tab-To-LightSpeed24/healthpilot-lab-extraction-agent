from app.core.db import normalize_database_url


def test_normalizes_legacy_postgres_scheme():
    """Reproduces the real Render/Heroku gotcha: SQLAlchemy 2.x's default
    driver lookup rejects 'postgres://' and raises NoSuchModuleError."""
    url = "postgres://user:pass@host:5432/dbname"
    assert normalize_database_url(url) == "postgresql://user:pass@host:5432/dbname"


def test_leaves_already_correct_postgresql_scheme_untouched():
    url = "postgresql://user:pass@host:5432/dbname"
    assert normalize_database_url(url) == url


def test_leaves_sqlite_untouched():
    url = "sqlite:///./healthpilot.db"
    assert normalize_database_url(url) == url
