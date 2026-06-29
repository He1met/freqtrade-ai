from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.strategy_generation_run import StrategyGenerationRun
from app.schemas.strategy_generation_run import (
    GenerationRunStatus,
    StrategyGenerationRunCreate,
    StrategyGenerationRunStatusUpdate,
)


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


class StrategyGenerationRunRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, payload: StrategyGenerationRunCreate) -> StrategyGenerationRun:
        run = StrategyGenerationRun(
            provider=payload.provider,
            model=payload.model,
            prompt_hash=payload.prompt_hash,
            prompt_summary=payload.prompt_summary,
            params_snapshot=payload.params_snapshot,
            requested_count=payload.requested_count,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def get(self, run_id: int) -> Optional[StrategyGenerationRun]:
        return self.db.get(StrategyGenerationRun, run_id)

    def list(self, status: Optional[GenerationRunStatus] = None) -> list[StrategyGenerationRun]:
        statement = select(StrategyGenerationRun).order_by(StrategyGenerationRun.created_at.desc())
        if status is not None:
            statement = statement.where(StrategyGenerationRun.status == status)
        return list(self.db.scalars(statement).all())

    def update_status(
        self,
        run_id: int,
        payload: StrategyGenerationRunStatusUpdate,
    ) -> Optional[StrategyGenerationRun]:
        run = self.get(run_id)
        if run is None:
            return None

        run.status = payload.status
        if payload.status == "running" and run.started_at is None:
            run.started_at = datetime.now(timezone.utc)
        if payload.status in TERMINAL_STATUSES:
            run.completed_at = datetime.now(timezone.utc)

        if payload.generated_count is not None:
            run.generated_count = payload.generated_count
        if payload.accepted_count is not None:
            run.accepted_count = payload.accepted_count
        if payload.failed_count is not None:
            run.failed_count = payload.failed_count
        if payload.error_message is not None:
            run.error_message = payload.error_message

        self.db.commit()
        self.db.refresh(run)
        return run
