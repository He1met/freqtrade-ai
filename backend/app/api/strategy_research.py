from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.strategy_research_contract import matches_official_research_policy
from app.db.session import get_db
from app.repositories.strategy_research import StrategyResearchRepository
from app.schemas.strategy_research import (
    FormalResearchRunRead,
    StrategyResearchBatchRead,
    StrategyResearchCandidateRead,
)
from app.services.formal_strategy_research import (
    FormalStrategyResearchCoordinator,
    get_formal_strategy_research_coordinator,
)


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
    candidates = StrategyResearchRepository(db).list_candidates(status=status, limit=limit)
    if status == "QUALIFIED":
        candidates = [
            candidate
            for candidate in candidates
            if matches_official_research_policy(candidate.batch.selection_policy)
        ]
    return [
        StrategyResearchCandidateRead.model_validate(candidate).model_copy(
            update={"quality_contract": candidate.batch.selection_policy}
        )
        for candidate in candidates
    ]


@router.get("/strategy-research/formal-run", response_model=FormalResearchRunRead)
def get_formal_research_run(
    db: Session = Depends(get_db),
    coordinator: FormalStrategyResearchCoordinator = Depends(
        get_formal_strategy_research_coordinator
    ),
) -> FormalResearchRunRead:
    return coordinator.status(db)


@router.post("/strategy-research/formal-run", response_model=FormalResearchRunRead)
def start_formal_research_run(
    db: Session = Depends(get_db),
    coordinator: FormalStrategyResearchCoordinator = Depends(
        get_formal_strategy_research_coordinator
    ),
) -> FormalResearchRunRead:
    return coordinator.start(db, trigger="manual")
