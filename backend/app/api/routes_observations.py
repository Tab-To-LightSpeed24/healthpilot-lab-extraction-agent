from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.models.loinc import LoincCode
from app.models.observation import MappingStatus, Observation
from app.models.document import Document
from app.schemas.api import ObservationCreateIn, ObservationOut, ObservationReviewIn, ObservationUpdateIn
from app.schemas.extraction import ExtractedTest
from app.services.extraction.validation import validate_row
from app.services import learned_mappings
from app.services.loinc_loader import get_alias_index
from app.services.loinc_mapping import map_observation

router = APIRouter(prefix="/observations", tags=["observations"])


@router.get("", response_model=List[ObservationOut])
def list_observations(
    document_id: Optional[str] = None,
    mapping_status: Optional[str] = None,
    q: Optional[str] = Query(default=None, description="Free-text search over test name"),
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
):
    query = db.query(Observation)
    if document_id:
        query = query.filter(Observation.document_id == document_id)
    if mapping_status:
        query = query.filter(Observation.mapping_status == mapping_status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Observation.original_test_name.ilike(like))
            | (Observation.normalized_test_name.ilike(like))
            | (Observation.loinc_code.ilike(like))
        )
    return query.order_by(Observation.created_at.desc()).limit(limit).all()


def _apply_mapping(db: Session, obs: Observation) -> None:
    """Deterministic (alias-exact) mapping only -- hand edits never call an
    LLM. Anything without an exact alias match is left needs_review for the
    human to resolve via the review endpoint."""
    mapping = map_observation(
        db=db,
        alias_index=get_alias_index(),
        original_test_name=obs.original_test_name,
        value=obs.value,
        unit=obs.unit,
        specimen=obs.specimen,
        method=obs.method,
        timing=obs.timing,
        use_llm=False,
    )
    obs.normalized_test_name = mapping["normalized_test_name"]
    obs.loinc_code = mapping["loinc_code"]
    obs.loinc_display = mapping["loinc_display"]
    obs.mapping_status = MappingStatus(mapping["mapping_status"])
    obs.mapping_confidence = mapping["mapping_confidence"]
    obs.mapping_stage = mapping["mapping_stage"]
    obs.mapping_rationale = mapping["mapping_rationale"]


def _get_or_404(db: Session, observation_id: str) -> Observation:
    obs = db.query(Observation).filter(Observation.id == observation_id).first()
    if obs is None:
        raise HTTPException(status_code=404, detail="Observation not found")
    return obs


@router.post("", response_model=ObservationOut, status_code=201)
def create_observation(body: ObservationCreateIn, db: Session = Depends(get_db)):
    if db.query(Document.id).filter(Document.id == body.document_id).first() is None:
        raise HTTPException(status_code=404, detail="Document not found")
    obs = Observation(
        document_id=body.document_id,
        page_number=body.page_number,
        original_test_name=body.original_test_name,
        value=body.value,
        unit=body.unit,
        reference_range=body.reference_range,
        specimen=body.specimen,
        method=body.method,
        timing=body.timing,
        flag=body.flag,
        extraction_source="manual",
        is_edited=True,
        extraction_confidence=1.0,
    )
    _apply_mapping(db, obs)
    db.add(obs)
    db.commit()
    db.refresh(obs)
    return obs


@router.get("/{observation_id}", response_model=ObservationOut)
def get_observation(observation_id: str, db: Session = Depends(get_db)):
    return _get_or_404(db, observation_id)


@router.patch("/{observation_id}", response_model=ObservationOut)
def update_observation(observation_id: str, body: ObservationUpdateIn, db: Session = Depends(get_db)):
    obs = _get_or_404(db, observation_id)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields provided to update.")
    if "original_test_name" in changes and changes["original_test_name"] is None:
        raise HTTPException(status_code=400, detail="original_test_name cannot be cleared.")

    name_changed = "original_test_name" in changes and changes["original_test_name"] != obs.original_test_name
    for field, new_value in changes.items():
        setattr(obs, field, new_value)
    obs.is_edited = True
    # Re-validate the corrected row rather than leaving notes that describe the
    # OLD value. A row a human has fixed and that now passes is fully trusted;
    # one that still looks off keeps its (re-computed) flags and lower score.
    verdict = validate_row(
        ExtractedTest(
            original_test_name=obs.original_test_name, value=obs.value, unit=obs.unit,
            reference_range=obs.reference_range, flag=obs.flag,
            extraction_confidence=settings.fallback_extraction_confidence,
        )
    )
    obs.validation_notes = verdict.notes or None
    obs.suggested_value = verdict.suggested_value
    obs.extraction_confidence = verdict.confidence if verdict.notes else 1.0
    # A renamed test is a different test: re-derive its code instead of
    # leaving the old name's LOINC mapping attached. Other edits (value,
    # unit, ...) keep the existing mapping, including a human-confirmed one.
    if name_changed:
        _apply_mapping(db, obs)
    db.commit()
    db.refresh(obs)
    return obs


@router.delete("/{observation_id}", status_code=204)
def delete_observation(observation_id: str, db: Session = Depends(get_db)):
    obs = _get_or_404(db, observation_id)
    db.delete(obs)
    db.commit()


@router.patch("/{observation_id}/review", response_model=ObservationOut)
def review_observation(observation_id: str, body: ObservationReviewIn, db: Session = Depends(get_db)):
    obs = db.query(Observation).filter(Observation.id == observation_id).first()
    if obs is None:
        raise HTTPException(status_code=404, detail="Observation not found")

    if body.loinc_code is None:
        if body.mapping_status != "unmapped":
            raise HTTPException(
                status_code=400,
                detail="loinc_code is required unless mapping_status is 'unmapped' (confirming no code applies).",
            )
        obs.loinc_code = None
        obs.loinc_display = None
        obs.mapping_rationale = "Manually reviewed; confirmed no reliable LOINC mapping exists."
    else:
        loinc = db.query(LoincCode).filter(LoincCode.loinc_num == body.loinc_code).first()
        if loinc is None:
            # Never let a human correction silently introduce a code that
            # doesn't exist -- the same "don't fabricate" rule the automated
            # pipeline follows applies here too.
            raise HTTPException(status_code=400, detail=f"'{body.loinc_code}' is not a known LOINC code.")
        obs.loinc_code = loinc.loinc_num
        obs.loinc_display = loinc.long_common_name
        obs.mapping_rationale = "Manually reviewed and confirmed by user."

    obs.mapping_status = MappingStatus(body.mapping_status)
    obs.mapping_stage = "human_review"
    obs.mapping_confidence = 1.0

    if obs.loinc_code:      # a human's choice is remembered for the same name + specimen + unit
        learned_mappings.remember(db, [(obs.original_test_name, obs.specimen, obs.unit, obs.loinc_code)],
                                  source="review", confidence=1.0)
    db.commit()
    db.refresh(obs)
    return obs
