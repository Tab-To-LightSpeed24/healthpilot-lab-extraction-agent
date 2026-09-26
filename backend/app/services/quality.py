"""Document-level automated quality signals, computed from the observations
already produced -- not a new model call, just a second pass over data we
already trust, looking for patterns a single row's own confidence score
can't see by itself.
"""
from collections import Counter
from typing import Iterable

from app.models.observation import Observation

LOW_EXTRACTION_CONFIDENCE_THRESHOLD = 0.7


def compute_quality_flags(observations: Iterable[Observation]) -> dict:
    observations = list(observations)

    status_counts = Counter(
        o.mapping_status.value if hasattr(o.mapping_status, "value") else o.mapping_status
        for o in observations
    )

    # Same test name appearing more than once on the same page usually means
    # either a genuinely repeated measurement (rare) or the model
    # double-extracting one row -- worth a human glance either way.
    per_page_names = Counter((o.page_number, (o.original_test_name or "").strip().lower()) for o in observations)
    duplicate_candidates = sorted(
        {
            name
            for (page, name), count in per_page_names.items()
            if count > 1
        }
    )

    low_confidence = [
        {"observation_id": o.id, "original_test_name": o.original_test_name, "extraction_confidence": o.extraction_confidence}
        for o in observations
        if o.extraction_confidence is not None and o.extraction_confidence < LOW_EXTRACTION_CONFIDENCE_THRESHOLD
    ]

    total = len(observations)
    review_needed = status_counts.get("needs_review", 0) + status_counts.get("unmapped", 0)

    return {
        "total_observations": total,
        "confirmed_count": status_counts.get("confirmed", 0),
        "needs_review_count": status_counts.get("needs_review", 0),
        "unmapped_count": status_counts.get("unmapped", 0),
        "review_needed_ratio": round(review_needed / total, 3) if total else None,
        "possible_duplicate_test_names": duplicate_candidates,
        "low_confidence_extractions": low_confidence,
    }
