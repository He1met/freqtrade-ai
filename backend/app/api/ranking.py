from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories import StrategyScoreRepository
from app.schemas import StrategyRankingEntry


router = APIRouter(prefix="/api", tags=["ranking"])


@router.get("/ranking", response_model=list[StrategyRankingEntry])
def list_strategy_ranking(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[StrategyRankingEntry]:
    return StrategyScoreRepository(db).list_ranking(limit=limit)
