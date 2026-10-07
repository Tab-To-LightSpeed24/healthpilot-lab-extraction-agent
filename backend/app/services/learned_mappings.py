"""Remembered LOINC picks: skip the AI for a test name it has already settled.

A pick is remembered only when it was (a) a human review or (b) an AI choice with high
confidence. The key is name + specimen + unit (all normalised), so "Glucose / Serum / mg/dL"
and "Glucose / Urine / mg/dL" stay separate. Lookups read an in-memory dict (database-free,
safe on worker threads); writes happen on the main thread and update the dict at once.
A later human correction overwrites an earlier AI pick.
"""
import logging
import threading
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.learned_mapping import LearnedMapping
from app.services.normalization import _clean

logger = logging.getLogger(__name__)

_cache: dict[str, str] = {}
_lock = threading.Lock()


def make_key(name: Optional[str], specimen: Optional[str], unit: Optional[str]) -> str:
    return "|".join((_clean(name or ""), _clean(specimen or ""), _clean(unit or "")))[:512]


def lookup(name: Optional[str], specimen: Optional[str], unit: Optional[str]) -> Optional[str]:
    return _cache.get(make_key(name, specimen, unit))


def load(db: Session) -> int:
    """Reads every remembered pick into memory (called once at startup)."""
    rows = db.query(LearnedMapping.key, LearnedMapping.loinc_num).all()
    with _lock:
        _cache.clear()
        _cache.update({k: n for k, n in rows})
    logger.info("Learned mappings ready: %s entries", len(rows))
    return len(rows)


def clear() -> None:
    with _lock:
        _cache.clear()


def remember(db: Session, picks: Iterable[tuple], source: str, confidence: Optional[float]) -> int:
    """picks: (name, specimen, unit, loinc_num). Does not commit - the caller's commit does."""
    saved = 0
    seen: set[str] = set()
    for name, specimen, unit, loinc_num in picks:
        if not name or not loinc_num:
            continue
        key = make_key(name, specimen, unit)
        if key in seen:                 # the same test twice on one page: one row is enough
            continue
        seen.add(key)
        row = db.query(LearnedMapping).filter(LearnedMapping.key == key).first()
        if row is None:
            db.add(LearnedMapping(key=key, loinc_num=loinc_num, source=source, confidence=confidence))
        elif source == "review" or row.source != "review":
            row.loinc_num, row.source, row.confidence = loinc_num, source, confidence
        else:
            continue       # never let an AI pick overwrite a human's
        with _lock:
            _cache[key] = loinc_num
        saved += 1
    return saved
