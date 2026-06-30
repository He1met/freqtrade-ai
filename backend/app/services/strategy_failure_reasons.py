from typing import Optional

from sqlalchemy.orm import Session

from app.models.strategy_failure_reason import StrategyFailureReason
from app.repositories.strategy_failure_reasons import StrategyFailureReasonRepository
from app.schemas.strategy_failure_reason import (
    StrategyFailureReasonCreate,
    StrategyFailureReasonType,
    StrategyFailureStage,
)


class StrategyFailureReasonService:
    def __init__(self, db: Session) -> None:
        self.repository = StrategyFailureReasonRepository(db)

    def record_failure(
        self,
        payload: StrategyFailureReasonCreate,
    ) -> Optional[StrategyFailureReason]:
        return self.repository.record(payload)

    def list_strategy_failures(
        self,
        strategy_id: int,
        stage: Optional[StrategyFailureStage] = None,
        reason_type: Optional[StrategyFailureReasonType] = None,
    ) -> list[StrategyFailureReason]:
        return self.repository.list_for_strategy(
            strategy_id=strategy_id,
            stage=stage,
            reason_type=reason_type,
        )

    def list_version_failures(
        self,
        strategy_version_id: int,
        stage: Optional[StrategyFailureStage] = None,
        reason_type: Optional[StrategyFailureReasonType] = None,
    ) -> list[StrategyFailureReason]:
        return self.repository.list_for_version(
            strategy_version_id=strategy_version_id,
            stage=stage,
            reason_type=reason_type,
        )
