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


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations(engine)
    db = SessionLocal()
    try:
        count = seed_loinc_table(db)
        logging.info("LOINC reference table ready: %s codes", count)
    finally:
        db.close()

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
