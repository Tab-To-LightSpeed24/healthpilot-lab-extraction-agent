from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings


def normalize_database_url(url: str) -> str:
    """Some providers (Render, Heroku) hand out `postgres://` connection
    strings, but SQLAlchemy 2.x's default driver lookup only recognizes
    `postgresql://` and raises NoSuchModuleError otherwise."""
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


_SQLITE_FALLBACK = "sqlite:///./healthpilot_dev.db"

DATABASE_URL = normalize_database_url(settings.database_url or _SQLITE_FALLBACK)
if DATABASE_URL.strip() == "":
    DATABASE_URL = _SQLITE_FALLBACK

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(DATABASE_URL, connect_args=connect_args)

if DATABASE_URL.startswith("sqlite") and ":memory:" not in DATABASE_URL:
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_wal(dbapi_connection, _record):
        # WAL: readers don't block the writer (and vice versa), which several document workers
        # sharing one local SQLite file need. Postgres (production) doesn't have this problem.
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
