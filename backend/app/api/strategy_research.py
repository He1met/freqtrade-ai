from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.strategy_research import StrategyResearchRepository
from app.schemas.strategy_research import StrategyResearchBatchRead, StrategyResearchCandidateRead


router = APIRouter(prefix="/api", tags=["strategy-research"])


@router.get("/strategy-research-batches", response_model=list[StrategyResearchBatchRead])
def list_strategy_research_batches(
    limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)
) -> list[StrategyResearchBatchRead]:
    return [
        StrategyResearchBatchRead.model_validate(batch)
        for batch in StrategyResearchRepository(db).list_batches(limit=limit)
    ]


@router.get("/strategy-research-candidates", response_model=list[StrategyResearchCandidateRead])
def list_strategy_research_candidates(
    status: Optional[Literal["QUALIFIED", "REJECTED", "VALIDATION_FAILED"]] = None,
    limit: int = Query(default=500, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[StrategyResearchCandidateRead]:
    return [
        StrategyResearchCandidateRead.model_validate(candidate)
        for candidate in StrategyResearchRepository(db).list_candidates(status=status, limit=limit)
    ]
