from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.strategy import Strategy, StrategyVersion
from app.models.strategy_failure_reason import StrategyFailureReason
from app.schemas.strategy_failure_reason import (
    StrategyFailureReasonCreate,
    StrategyFailureReasonFilter,
    StrategyFailureReasonType,
    StrategyFailureStage,
)


class StrategyFailureReasonRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def record(self, payload: StrategyFailureReasonCreate) -> Optional[StrategyFailureReason]:
        strategy = self.db.get(Strategy, payload.strategy_id)
        version = self.db.get(StrategyVersion, payload.strategy_version_id)
        if strategy is None or version is None or version.strategy_id != strategy.id:
            return None

        reason = StrategyFailureReason(
            strategy_id=payload.strategy_id,
            strategy_version_id=payload.strategy_version_id,
            stage=payload.stage,
            reason_type=payload.reason_type,
            severity=payload.severity,
            message=payload.message,
            details=payload.details,
        )
        self.db.add(reason)
        self.db.commit()
        self.db.refresh(reason)
        return reason

    def list_for_strategy(
        self,
        strategy_id: int,
        stage: Optional[StrategyFailureStage] = None,
        reason_type: Optional[StrategyFailureReasonType] = None,
    ) -> list[StrategyFailureReason]:
        filters = StrategyFailureReasonFilter(
            strategy_id=strategy_id,
            stage=stage,
            reason_type=reason_type,
        )
        return self.list_reasons(filters)

    def list_for_version(
        self,
        strategy_version_id: int,
        stage: Optional[StrategyFailureStage] = None,
        reason_type: Optional[StrategyFailureReasonType] = None,
    ) -> list[StrategyFailureReason]:
        filters = StrategyFailureReasonFilter(
            strategy_version_id=strategy_version_id,
            stage=stage,
            reason_type=reason_type,
        )
        return self.list_reasons(filters)

    def list_reasons(self, filters: StrategyFailureReasonFilter) -> list[StrategyFailureReason]:
        statement = select(StrategyFailureReason)
        if filters.strategy_id is not None:
            statement = statement.where(StrategyFailureReason.strategy_id == filters.strategy_id)
        if filters.strategy_version_id is not None:
            statement = statement.where(
                StrategyFailureReason.strategy_version_id == filters.strategy_version_id
            )
        if filters.stage is not None:
            statement = statement.where(StrategyFailureReason.stage == filters.stage)
        if filters.reason_type is not None:
            statement = statement.where(StrategyFailureReason.reason_type == filters.reason_type)

        statement = statement.order_by(
            StrategyFailureReason.created_at.desc(),
            StrategyFailureReason.id.desc(),
        )
        return list(self.db.scalars(statement).all())
