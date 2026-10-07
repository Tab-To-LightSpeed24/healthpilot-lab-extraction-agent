"""A long printed field must never fail a document (Postgres rejects over-long
VARCHARs, SQLite doesn't), and several documents must process side by side."""
import threading
import time
from unittest.mock import patch

import fitz
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base
from app.models.document import Document, DocumentStatus
from app.models.observation import Observation, clip_text
from app.schemas.extraction import ExtractedTest, PageExtractionResult
from app.services import worker
from tests.test_api import client, process_pending  # noqa: F401

LONG_RANGE = ("For Screening:\nDiabetes: >6.5%\nPre-Diabetes: 5.7% -\n6.4%\nNon-Diabetes: < 5.7%\n\n"
              "For Diabetic Patient:\nPoor Control : > 7.0 %\nGood Control : 6.0-7.0 %")  # ~170 chars (the real report)


def _pdf(n=1):
    d = fitz.open()
    for i in range(n):
        d.new_page().insert_text((72, 72), f"HbA1c 7.1 % page {i + 1}", fontsize=11)
    data = d.tobytes()
    d.close()
    return data


# ------------------------------------------------------------- long fields

def test_reference_range_is_unbounded_text_and_other_columns_were_widened():
    cols = Observation.__table__.columns
    assert type(cols["reference_range"].type).__name__ == "Text"
    assert cols["value"].type.length >= 512 and cols["specimen"].type.length >= 256
    assert cols["method"].type.length >= 512 and cols["flag"].type.length >= 64


def test_the_real_failing_reference_range_is_saved_intact(client):
    assert len(LONG_RANGE) > 128
    res = PageExtractionResult(tests=[ExtractedTest(
        original_test_name="HbA1c", value="7.10", unit="%", reference_range=LONG_RANGE,
        specimen="EDTA Blood", method="High Performance Liquid Chromatography", flag="H",
        extraction_confidence=0.99)], page_notes=None)
    doc_id = client.post("/reports", files={"file": ("a.pdf", _pdf(), "application/pdf")}).json()["id"]
    with patch("app.services.pipeline.llm_client.extract_page", return_value=res):
        process_pending(client, doc_id)
    detail = client.get(f"/reports/{doc_id}").json()
    assert detail["status"] == "complete"
    assert detail["observations"][0]["reference_range"] == LONG_RANGE


def test_overlong_free_text_is_clipped_not_fatal(db_session):
    doc = Document(filename="f" * 900, content_type="application/pdf", raw_content=b"x")
    db_session.add(doc)
    db_session.flush()
    db_session.add(Observation(document_id=doc.id, page_number=1, original_test_name="T" * 700,
                               value="9" * 2000, unit="u" * 500, specimen="s" * 900, method="m" * 2000,
                               timing="t" * 900, flag="F" * 300, suggested_value="v" * 900,
                               reference_range="r" * 20000))
    db_session.flush()   # would raise StringDataRightTruncation on Postgres without the guard
    o = db_session.query(Observation).filter_by(document_id=doc.id).one()
    assert len(doc.filename) == 512 and doc.filename.endswith("…")
    assert len(o.original_test_name) == 512 and len(o.value) == 512 and len(o.flag) == 64
    assert len(o.reference_range) == 20000   # Text: never clipped
    o.value = "z" * 1000                     # edits are guarded too
    db_session.flush()
    assert len(o.value) == 512


def test_clip_text_leaves_short_and_non_strings_alone():
    assert clip_text("abc", 10) == "abc" and clip_text(None, 10) is None and clip_text(5, 3) == 5
    assert clip_text("abcdef", 4) == "abc…"


def test_migration_head_widens_the_columns():
    from app.core.migrate import run_migrations
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    run_migrations(engine)
    cols = {c["name"]: c["type"] for c in inspect(engine).get_columns("observations")}
    assert type(cols["reference_range"]).__name__ in ("TEXT", "Text")
    assert getattr(cols["value"], "length", 0) >= 512


# ------------------------------------------------- documents side by side

def test_concurrency_is_one_on_sqlite_and_configurable_elsewhere():
    assert worker.effective_concurrency(4, "sqlite") == 1
    assert worker.effective_concurrency(4, "postgresql") == 4
    assert worker.effective_concurrency(0, "postgresql") == 1


def test_two_documents_process_at_the_same_time(tmp_path, monkeypatch):
    from app.services import pipeline
    engine = create_engine(f"sqlite:///{tmp_path / 'c.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine, tables=[Document.__table__, Observation.__table__])
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(pipeline, "SessionLocal", Session)

    ids = []
    s = Session()
    for name in ("a.pdf", "b.pdf"):
        d = Document(filename=name, content_type="application/pdf", raw_content=_pdf(4))
        s.add(d)
        s.flush()
        ids.append(d.id)
    s.commit()
    s.close()

    lock, state = threading.Lock(), {"now": 0, "peak": 0}
    overlap = threading.Event()

    def slow(image_png, text_layer):
        # Each document has 4 pages, so more than 4 calls in flight at once is only possible if the
        # two documents really run side by side. Wait (bounded) for that instead of racing a clock.
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            if state["now"] > 4:
                overlap.set()
        overlap.wait(timeout=8)
        with lock:
            state["now"] -= 1
        return PageExtractionResult(tests=[ExtractedTest(
            original_test_name="Hemoglobin", value="13.5", unit="g/dL", extraction_confidence=0.9)], page_notes=None)

    from app.services import retrieval
    from app.services.loinc_loader import get_alias_index
    retrieval.get_index()          # production builds these at startup; keep that
    get_alias_index()              # one-time cost out of the timed section
    started = time.monotonic()
    with patch("app.services.pipeline.llm_client.extract_page", side_effect=slow):
        threads = [threading.Thread(target=pipeline.process_document, args=(i,)) for i in ids]
        [t.start() for t in threads]
        [t.join() for t in threads]
    elapsed = time.monotonic() - started

    s = Session()
    docs = [s.get(Document, i) for i in ids]
    assert [d.status for d in docs] == [DocumentStatus.complete] * 2
    assert all(len(d.observations) == 4 for d in docs)
    assert state["peak"] > 4, "pages of BOTH documents should be in flight together"
    assert elapsed < 7.0, f"{elapsed:.1f}s - documents ran one after the other"
    s.close()


def test_two_workers_never_claim_the_same_document(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'q.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine, tables=[Document.__table__])
    Session = sessionmaker(bind=engine)
    s = Session()
    for k in range(6):
        s.add(Document(filename=f"{k}.pdf", content_type="application/pdf", raw_content=b"x"))
    s.commit()
    s.close()

    claimed, lock = [], threading.Lock()

    def grab():
        db = Session()
        try:
            while (doc_id := worker._claim_next_pending(db)) is not None:
                with lock:
                    claimed.append(doc_id)
        finally:
            db.close()

    threads = [threading.Thread(target=grab) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(claimed) == 6 and len(set(claimed)) == 6


def test_global_llm_slot_limit_is_a_real_semaphore():
    from app.services import llm_client
    assert llm_client._llm_slots._initial_value >= 1
