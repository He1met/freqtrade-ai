from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.strategy_research import StrategyResearchBatch, StrategyResearchCandidate


class StrategyResearchRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_batch_by_run_id(self, run_id: str) -> Optional[StrategyResearchBatch]:
        statement = (
            select(StrategyResearchBatch)
            .options(selectinload(StrategyResearchBatch.candidates))
            .where(StrategyResearchBatch.run_id == run_id)
        )
        return self.db.scalars(statement).first()

    def list_batches(self, limit: int = 50) -> list[StrategyResearchBatch]:
        statement = (
            select(StrategyResearchBatch)
            .options(selectinload(StrategyResearchBatch.candidates))
            .order_by(StrategyResearchBatch.created_at.desc(), StrategyResearchBatch.id.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())

    def list_candidates(
        self, *, status: Optional[str] = None, limit: int = 500
    ) -> list[StrategyResearchCandidate]:
        statement = select(StrategyResearchCandidate).order_by(
            StrategyResearchCandidate.created_at.desc(), StrategyResearchCandidate.id.desc()
        )
        if status is not None:
            statement = statement.where(StrategyResearchCandidate.status == status)
        return list(self.db.scalars(statement.limit(limit)).all())

    def add_batch(self, batch: StrategyResearchBatch) -> StrategyResearchBatch:
        self.db.add(batch)
        self.db.commit()
        self.db.refresh(batch)
        return self.get_batch_by_run_id(batch.run_id) or batch
