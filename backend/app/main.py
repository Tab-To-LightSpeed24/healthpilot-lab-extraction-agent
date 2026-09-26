import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_reports, routes_observations, routes_loinc
from app.core.config import settings
from app.core.db import engine, SessionLocal
from app.core.migrate import run_migrations
from app.services.loinc_loader import seed_loinc_table
from app.services.worker import start_worker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _run_startup_migrations_and_seed() -> int:
    """The actual blocking work (Alembic + psycopg2 + a bulk DB seed),
    isolated into one plain synchronous function so it can be handed to a
    worker thread instead of running directly on the event-loop thread."""
    run_migrations(engine)
    db = SessionLocal()
    try:
        return seed_loinc_table(db)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Deliberately explicit try/except with logger.exception (full traceback)
    # around startup, rather than trusting the default lifespan error
    # handling to surface it -- a real deploy once failed here with a bare
    # "Exited with status 3" and no traceback at all in the logs, which made
    # diagnosing it needlessly hard. This guarantees the next failure, if
    # any, is actually diagnosable from the logs alone.
    #
    # Also -- and this is what was actually causing that crash, confirmed by
    # reproducing it locally in Docker: this blocking migration/seeding work
    # was previously called directly on uvicorn's event-loop thread. Calling
    # it standalone (no event loop running) always worked; calling it inside
    # uvicorn's lifespan reliably died with no traceback at all, every time,
    # regardless of the loop implementation (uvloop or plain asyncio) or
    # available memory (reproduced with no memory limit at all). Running it
    # in a worker thread via asyncio.to_thread avoids running blocking
    # psycopg2/Alembic I/O directly on the event-loop thread altogether,
    # which is the correct pattern for blocking calls in an async app
    # regardless of the exact low-level cause.
    try:
        count = await asyncio.to_thread(_run_startup_migrations_and_seed)
        logger.info("LOINC reference table ready: %s codes", count)
    except Exception:
        logger.exception("Startup migration/seeding failed")
        raise

    stop_worker = start_worker()
    yield
    stop_worker.set()


app = FastAPI(
    title="HealthPilot AI Lab Extraction & LOINC Coding Agent",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(routes_reports.router)
app.include_router(routes_observations.router)
app.include_router(routes_loinc.router)
