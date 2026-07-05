from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories import StrategyRepository
from app.schemas import StrategyRead, StrategyVersionRead


router = APIRouter(prefix="/api", tags=["strategies"])


@router.get("/strategies", response_model=list[StrategyRead])
def list_strategies(limit: int = 100, db: Session = Depends(get_db)) -> list[StrategyRead]:
    strategies = StrategyRepository(db).list(limit=limit)
    return [StrategyRead.model_validate(strategy) for strategy in strategies]


@router.get("/strategy-versions", response_model=list[StrategyVersionRead])
def list_strategy_versions(
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[StrategyVersionRead]:
    versions = StrategyRepository(db).list_versions(limit=limit)
    return [StrategyVersionRead.model_validate(version) for version in versions]
