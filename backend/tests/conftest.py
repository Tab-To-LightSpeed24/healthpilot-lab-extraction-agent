import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base
from app.services.loinc_loader import seed_loinc_table

# Seeding the full ~62k-row LOINC table (plus several hundred thousand alias
# rows) takes real time -- fine once per app startup, but not fine if every
# individual test re-triggers it. So this seeds exactly ONCE per test
# session, and each test gets an isolated SAVEPOINT-backed transaction that's
# rolled back afterward, instead of a fresh from-scratch database per test.


@pytest.fixture(scope="session")
def _seeded_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    seed_loinc_table(session)
    session.close()
    return engine


@pytest.fixture()
def db_session(_seeded_engine):
    connection = _seeded_engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
