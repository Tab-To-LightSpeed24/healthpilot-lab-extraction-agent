"""In-memory LOINC candidate retrieval.

Replaces the per-row SQL `ILIKE '%token%'` scans (full-table scans over ~62k
codes and ~1.7M alias rows, repeated for every unmatched test) with an
inverted index held in memory: a query costs a few array additions, i.e.
well under a millisecond to a few milliseconds, and touches no database, so
it is safe to call from worker threads.

Scoring is IDF-weighted token overlap (rare words like "ferritin" count far
more than "serum"), with name-length normalisation (prefer the plain test
over a long specialised panel), a specimen/system match, a unit match, and a
small prior from LOINC's own COMMON_TEST_RANK. Alias words are indexed too so
abbreviations ("Hgb", "ALT") still reach their code.
"""
import logging
import math
import re
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.services.loinc_loader import _iter_loinc_rows

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"in", "of", "by", "the", "a", "an", "and", "or", "test", "level", "specimen", "with", "to", "for"}
_RANK_CEILING = 2000          # COMMON_TEST_RANK values above this get no prior
MAX_QUERY_TOKENS = 8
NAME_WEIGHT = 1.0
ALIAS_WEIGHT = 0.7


def tokenize(text: Optional[str]) -> list[str]:
    if not text:
        return []
    return [w for w in _WORD_RE.findall(text.lower()) if len(w) > 1 and w not in _STOPWORDS]


# Lab reports say "Serum"; LOINC's system column says "Ser", "Ser/Plas", "Bld"...
_SPECIMEN_SYNONYMS = {
    "serum": {"ser"}, "plasma": {"plas"}, "blood": {"bld"}, "whole": {"bld"}, "venous": {"bld"},
    "capillary": {"bld"}, "arterial": {"bld"}, "urine": {"urine", "ur"}, "feces": {"stool"},
    "faeces": {"stool"}, "cerebrospinal": {"csf"}, "synovial": {"synv"}, "amniotic": {"amnio"},
    "nasopharynx": {"nph"}, "vaginal": {"vag"}, "fluid": {"fld"},
}


def _specimen_tokens(specimen: Optional[str]) -> frozenset:
    toks = set(tokenize(specimen))
    for t in list(toks):
        toks |= _SPECIMEN_SYNONYMS.get(t, set())
    return frozenset(toks)


def _unit_key(unit: Optional[str]) -> str:
    return "".join(_WORD_RE.findall((unit or "").lower()))


@dataclass
class Hit:
    loinc_num: str
    long_common_name: str
    component: Optional[str]
    system: Optional[str]
    example_units: Optional[str]
    score: float

    def summary(self) -> dict:
        return {
            "loinc_num": self.loinc_num,
            "long_common_name": self.long_common_name,
            "component": self.component,
            "system": self.system,
        }


class RetrievalIndex:
    def __init__(self) -> None:
        self.nums: list[str] = []
        self.names: list[str] = []
        self.components: list[Optional[str]] = []
        self.systems: list[Optional[str]] = []
        self.units: list[Optional[str]] = []
        self._by_num: dict[str, int] = {}
        name_post: dict[str, list[int]] = {}
        alias_post: dict[str, list[int]] = {}
        name_len: list[int] = []
        sys_tokens: list[frozenset] = []
        unit_keys: list[str] = []
        ranks: list[int] = []
        presence: list[bool] = []

        for idx, row in enumerate(_iter_loinc_rows()):
            self.nums.append(row["loinc_num"])
            self.names.append(row["long_common_name"])
            self.components.append(row["component"])
            self.systems.append(row["system"])
            self.units.append(row["example_units"])
            self._by_num[row["loinc_num"]] = idx
            ranks.append(row["common_test_rank"])
            presence.append("[Presence]" in row["long_common_name"] or "[Identifier]" in row["long_common_name"])
            unit_keys.append(_unit_key(row["example_units"]))
            sys_tokens.append(frozenset(tokenize(row["system"])))

            name_toks = set(tokenize(" ".join(filter(None, [row["long_common_name"], row["shortname"], row["component"]]))))
            name_len.append(len(set(tokenize(row["long_common_name"]))) or 1)
            for t in name_toks:
                name_post.setdefault(t, []).append(idx)
            alias_toks: set[str] = set()
            for alias in row["aliases"]:
                alias_toks.update(tokenize(alias))
            for t in alias_toks - name_toks:
                alias_post.setdefault(t, []).append(idx)

        n = len(self.nums)
        self.n = n
        self._name_post = {t: np.asarray(v, dtype=np.int32) for t, v in name_post.items()}
        self._alias_post = {t: np.asarray(v, dtype=np.int32) for t, v in alias_post.items()}
        self._len_norm = (1.0 + 0.07 * np.asarray(name_len, dtype=np.float32))
        self._presence = np.asarray(presence, dtype=bool)
        self._sys_tokens = sys_tokens
        self._unit_keys = unit_keys
        r = np.asarray(ranks, dtype=np.float32)
        self._rank_prior = np.where((r > 0) & (r <= _RANK_CEILING), 1.0 - r / _RANK_CEILING, 0.0).astype(np.float32)
        logger.info("Retrieval index ready: %s codes, %s name tokens, %s alias-only tokens",
                    n, len(self._name_post), len(self._alias_post))

    # -------------------------------------------------------------- lookup
    def get(self, loinc_num: str) -> Optional[dict]:
        i = self._by_num.get(loinc_num)
        if i is None:
            return None
        return {"loinc_num": self.nums[i], "long_common_name": self.names[i],
                "component": self.components[i], "system": self.systems[i]}

    def _idf(self, t: str) -> tuple[float, Optional[np.ndarray], Optional[np.ndarray]]:
        a, b = self._name_post.get(t), self._alias_post.get(t)
        df = (0 if a is None else len(a)) + (0 if b is None else len(b))
        if df == 0:
            return 0.0, None, None
        return math.log(1.0 + self.n / df), a, b

    def search(self, name: str, specimen: Optional[str] = None, unit: Optional[str] = None,
               method: Optional[str] = None, value: Optional[str] = None, k: int = 6) -> list[Hit]:
        tokens = list(dict.fromkeys(tokenize(name)))[:MAX_QUERY_TOKENS]
        tokens += [t for t in dict.fromkeys(tokenize(method)) if t not in tokens][:2]
        tokens += [t for t in dict.fromkeys(tokenize(specimen)) if t not in tokens][:2]
        if not tokens:
            return []
        scores = np.zeros(self.n, dtype=np.float32)
        total_idf = 0.0
        for t in tokens:
            idf, a, b = self._idf(t)
            if idf <= 0:
                continue
            total_idf += idf
            if a is not None:
                scores[a] += idf * NAME_WEIGHT
            if b is not None:
                scores[b] += idf * ALIAS_WEIGHT
        if total_idf <= 0:
            return []
        scores /= self._len_norm
        scores *= (1.0 + 0.35 * self._rank_prior)
        # "[Presence]" codes describe qualitative results (positive/negative);
        # a numeric value or a unit means the quantitative code is meant.
        v = (value or "").strip().lower()
        qualitative = bool(v) and not any(ch.isdigit() for ch in v)
        if qualitative:
            scores[self._presence] *= 1.2
        else:
            scores[self._presence] *= 0.75 if (v or unit) else 0.9

        spec_tokens = _specimen_tokens(specimen)
        if spec_tokens:
            # Narrow to a workable shortlist first, then apply the (python-level) specimen/unit rules.
            pool = np.argpartition(scores, -min(300, self.n - 1))[-300:]
            for i in pool:
                st = self._sys_tokens[i]
                if st & spec_tokens:
                    scores[i] *= 1.6
                elif st:
                    scores[i] *= 0.55
        ukey = _unit_key(unit)
        if ukey:
            pool = np.argpartition(scores, -min(300, self.n - 1))[-300:]
            for i in pool:
                if self._unit_keys[i] and self._unit_keys[i] == ukey:
                    scores[i] *= 1.25

        kk = min(k, self.n)
        top = np.argpartition(scores, -kk)[-kk:]
        top = top[np.argsort(-scores[top], kind="stable")]
        return [
            Hit(self.nums[i], self.names[i], self.components[i], self.systems[i], self.units[i],
                float(scores[i] / total_idf))
            for i in top if scores[i] > 0
        ]


_index: Optional[RetrievalIndex] = None
_lock = threading.Lock()


def get_index() -> RetrievalIndex:
    """Built once per process (a few seconds), then shared by every thread."""
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                _index = RetrievalIndex()
    return _index


def _warm_all() -> None:
    # One after another (not in parallel) so peak memory stays low on a small instance.
    from app.services.loinc_loader import get_alias_index, get_known_short_names
    get_index()
    get_alias_index()
    get_known_short_names()


def warm_up_in_background() -> threading.Thread:
    t = threading.Thread(target=_warm_all, name="retrieval-warmup", daemon=True)
    t.start()
    return t
