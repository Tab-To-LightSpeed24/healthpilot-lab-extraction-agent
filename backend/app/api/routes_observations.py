from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.observation import Observation
from app.schemas.api import ObservationOut

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
