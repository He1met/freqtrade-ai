from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.strategy_generation_run import StrategyGenerationRun
from app.models.execution_lineage import LOCAL_DRY_RUN_SCOPE_ID
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.schemas.strategy_generation_run import (
    GenerationRunStatus,
    StrategyGenerationRunCreate,
    StrategyGenerationRunStatusUpdate,
)


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


class StrategyGenerationRunRepository:
    def __init__(self, db: Session, execution_scope_id: str = LOCAL_DRY_RUN_SCOPE_ID) -> None:
        self.db = db
        self.execution_scope_id = execution_scope_id

    def create(self, payload: StrategyGenerationRunCreate) -> StrategyGenerationRun:
        ensure_execution_scope_catalog(self.db)
        if payload.execution_scope_id != self.execution_scope_id:
            raise ValueError("strategy generation scope does not match repository scope")
        run = StrategyGenerationRun(
            execution_scope_id=self.execution_scope_id,
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
        statement = select(StrategyGenerationRun).where(
            StrategyGenerationRun.id == run_id,
            StrategyGenerationRun.execution_scope_id == self.execution_scope_id,
        )
        return self.db.scalars(statement).first()

    def list(self, status: Optional[GenerationRunStatus] = None) -> list[StrategyGenerationRun]:
        statement = select(StrategyGenerationRun).order_by(StrategyGenerationRun.created_at.desc())
        statement = statement.where(
            StrategyGenerationRun.execution_scope_id == self.execution_scope_id
        )
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
