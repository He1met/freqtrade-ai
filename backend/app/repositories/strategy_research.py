from typing import Optional

from sqlalchemy import func, select
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

    def list_recent_attempt_event_chains(
        self, *, attempt_limit: int = 10
    ) -> list[list[StrategyResearchAttemptEvent]]:
        """Return complete event chains for the most recent distinct attempts."""

        recent = (
            select(
                StrategyResearchAttemptEvent.attempt_id.label("attempt_id"),
                func.max(StrategyResearchAttemptEvent.created_at).label("latest_at"),
                func.max(StrategyResearchAttemptEvent.id).label("latest_id"),
            )
            .group_by(StrategyResearchAttemptEvent.attempt_id)
            .order_by(
                func.max(StrategyResearchAttemptEvent.created_at).desc(),
                func.max(StrategyResearchAttemptEvent.id).desc(),
            )
            .limit(attempt_limit)
            .subquery()
        )
        attempt_ids = list(
            self.db.scalars(
                select(recent.c.attempt_id).order_by(
                    recent.c.latest_at.desc(), recent.c.latest_id.desc()
                )
            ).all()
        )
        if not attempt_ids:
            return []
        events = list(
            self.db.scalars(
                select(StrategyResearchAttemptEvent)
                .where(StrategyResearchAttemptEvent.attempt_id.in_(attempt_ids))
                .order_by(
                    StrategyResearchAttemptEvent.created_at.desc(),
                    StrategyResearchAttemptEvent.id.desc(),
                    StrategyResearchAttemptEvent.sequence.asc(),
                )
            ).all()
        )
        grouped = {attempt_id: [] for attempt_id in attempt_ids}
        for event in events:
            grouped[event.attempt_id].append(event)
        return [
            sorted(grouped[attempt_id], key=lambda event: (event.sequence, event.id))
            for attempt_id in attempt_ids
        ]

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

    def latest_quality_receipt(self) -> Optional[MarketDataQualityReceipt]:
        statement = (
            select(MarketDataQualityReceipt)
            .order_by(
                MarketDataQualityReceipt.inspected_at.desc(),
                MarketDataQualityReceipt.id.desc(),
            )
            .limit(1)
        )
        return self.db.scalars(statement).first()
