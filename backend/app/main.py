import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_reports, routes_observations, routes_loinc
from app.core.config import settings
from app.core.db import engine, SessionLocal
from app.core.migrate import run_migrations
from app.core.security import (
    configure_logging, rate_limit_general, require_api_key, security_middleware,
)
from app.services.loinc_loader import seed_loinc_table
from app.services import learned_mappings
from app.services.retrieval import warm_up_in_background
from app.services.worker import start_worker

configure_logging()
logger = logging.getLogger(__name__)


def _run_startup_migrations_and_seed() -> int:
    """The actual blocking work (Alembic + psycopg2 + a bulk DB seed),
    isolated into one plain synchronous function so it can be handed to a
    worker thread instead of running directly on the event-loop thread."""
    run_migrations(engine)
    db = SessionLocal()
    try:
        count = seed_loinc_table(db)
        learned_mappings.load(db)      # remembered LOINC picks -> memory
        return count
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
        configure_logging()   # Alembic resets the root logger; restore level, format and redaction
        logger.info("LOINC reference table ready: %s codes", count)
    except Exception:
        logger.exception("Startup migration/seeding failed")
        raise

    warm_up_in_background()   # build the in-memory LOINC index before the first upload needs it
    stop_worker = start_worker()
    yield
    stop_worker.set()


app = FastAPI(
    title="HealthPilot AI Lab Extraction & LOINC Coding Agent",
    version="0.1.0",
    lifespan=lifespan,
)

cors_kwargs = {
    "allow_methods": ["*"],
    "allow_headers": ["*"],
    "expose_headers": ["X-Request-ID", "Retry-After"],
}
if settings.cors_origins.strip() == "*":
    # Under W3C CORS spec, Access-Control-Allow-Origin cannot be literal "*" when
    # credentials mode is true. Setting allow_origin_regex dynamically echoes the
    # caller's Origin header in Access-Control-Allow-Origin.
    cors_kwargs["allow_origin_regex"] = r"^https?://.*"
    cors_kwargs["allow_credentials"] = True
else:
    cors_kwargs["allow_origins"] = settings.cors_origins_list
    cors_kwargs["allow_credentials"] = True

app.middleware("http")(security_middleware)
# Added last so CORS is the outermost layer: preflights and even 401/429
# responses still carry CORS headers the browser needs to read them.
app.add_middleware(CORSMiddleware, **cors_kwargs)

if not settings.api_key:
    logger.warning("API_KEY is not set: the API is open to anyone who can reach it")
if settings.cors_origins.strip() == "*":
    logger.warning("CORS_ORIGINS is '*': restrict it to your frontend origin in production")


@app.get("/health")
def health():
    return {"status": "ok"}


_protected = [Depends(require_api_key), Depends(rate_limit_general)]
app.include_router(routes_reports.router, dependencies=_protected)
app.include_router(routes_observations.router, dependencies=_protected)
app.include_router(routes_loinc.router, dependencies=_protected)
