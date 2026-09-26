from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.loinc import LoincCode
from app.schemas.api import LoincSearchResult

router = APIRouter(prefix="/loinc", tags=["loinc"])


@router.get("/search", response_model=List[LoincSearchResult])
def search_loinc(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=25, le=100),
    db: Session = Depends(get_db),
):
    like = f"%{q}%"
    return (
        db.query(LoincCode)
        .filter(
            (LoincCode.long_common_name.ilike(like))
            | (LoincCode.shortname.ilike(like))
            | (LoincCode.component.ilike(like))
            | (LoincCode.loinc_num.ilike(like))
        )
        .limit(limit)
        .all()
    )
