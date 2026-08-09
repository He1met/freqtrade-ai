from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.strategy_research import (
    MarketDataQualityReceipt,
    StrategyResearchAttemptEvent,
    StrategyResearchBatch,
    StrategyResearchCandidate,
)


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
        statement = (
            select(StrategyResearchCandidate)
            .options(selectinload(StrategyResearchCandidate.batch))
            .order_by(
                StrategyResearchCandidate.created_at.desc(),
                StrategyResearchCandidate.id.desc(),
            )
        )
        if status is not None:
            statement = statement.where(StrategyResearchCandidate.status == status)
        return list(self.db.scalars(statement.limit(limit)).all())

    def add_batch(self, batch: StrategyResearchBatch) -> StrategyResearchBatch:
        self.db.add(batch)
        self.db.commit()
        self.db.refresh(batch)
        return self.get_batch_by_run_id(batch.run_id) or batch

    def append_attempt_event(
        self, event: StrategyResearchAttemptEvent
    ) -> StrategyResearchAttemptEvent:
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        return event

    def list_attempt_events(self, limit: int = 100) -> list[StrategyResearchAttemptEvent]:
        statement = (
            select(StrategyResearchAttemptEvent)
            .order_by(
                StrategyResearchAttemptEvent.created_at.desc(),
                StrategyResearchAttemptEvent.id.desc(),
            )
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())

    def get_attempt_event(
        self, *, attempt_id: str, sequence: int
    ) -> Optional[StrategyResearchAttemptEvent]:
        return self.db.scalars(
            select(StrategyResearchAttemptEvent).where(
                StrategyResearchAttemptEvent.attempt_id == attempt_id,
                StrategyResearchAttemptEvent.sequence == sequence,
            )
        ).first()

    def append_market_data_quality_receipt(
        self, receipt: MarketDataQualityReceipt
    ) -> MarketDataQualityReceipt:
        existing = self.db.scalars(
            select(MarketDataQualityReceipt).where(
                MarketDataQualityReceipt.evidence_digest == receipt.evidence_digest
            )
        ).first()
        if existing is not None:
            return existing
        self.db.add(receipt)
        self.db.commit()
        self.db.refresh(receipt)
        return receipt

    def latest_market_data_quality_receipt(
        self, *, exchange: str, pair: str, timeframe: str
    ) -> Optional[MarketDataQualityReceipt]:
        statement = (
            select(MarketDataQualityReceipt)
            .where(
                MarketDataQualityReceipt.exchange == exchange,
                MarketDataQualityReceipt.pair == pair,
                MarketDataQualityReceipt.timeframe == timeframe,
            )
            .order_by(
                MarketDataQualityReceipt.inspected_at.desc(),
                MarketDataQualityReceipt.id.desc(),
            )
            .limit(1)
        )
        return self.db.scalars(statement).first()
