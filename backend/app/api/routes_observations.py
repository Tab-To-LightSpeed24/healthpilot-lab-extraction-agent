from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.loinc import LoincCode
from app.models.observation import MappingStatus, Observation
from app.schemas.api import ObservationOut, ObservationReviewIn

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

    db.commit()
    db.refresh(obs)
    return obs
