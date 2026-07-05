from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.backtest import BacktestResult, BacktestRun, BacktestTask
from app.models.strategy import StrategyVersion
from app.schemas.backtest import (
    BacktestResultCreate,
    BacktestRunCreate,
    BacktestRunStatusUpdate,
    BacktestTaskCreate,
    BacktestTaskStatusUpdate,
)


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "blocked"}


class BacktestRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_run(self, payload: BacktestRunCreate) -> Optional[BacktestRun]:
        strategy_version = self.db.get(StrategyVersion, payload.strategy_version_id)
        if strategy_version is None:
            return None

        run = BacktestRun(
            strategy_version_id=payload.strategy_version_id,
            profile_name=payload.profile_name,
            config_snapshot=payload.config_snapshot,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def get_run(self, run_id: int) -> Optional[BacktestRun]:
        return self.db.get(BacktestRun, run_id)

    def list_runs(self, limit: int = 50) -> list[BacktestRun]:
        statement = (
            select(BacktestRun)
            .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())

    def update_run_status(
        self,
        run_id: int,
        payload: BacktestRunStatusUpdate,
    ) -> Optional[BacktestRun]:
        run = self.get_run(run_id)
        if run is None:
            return None

        run.status = payload.status
        if payload.status == "running" and run.started_at is None:
            run.started_at = datetime.now(timezone.utc)
        if payload.status in TERMINAL_STATUSES:
            run.completed_at = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(run)
        return run

    def create_task(self, run_id: int, payload: BacktestTaskCreate) -> Optional[BacktestTask]:
        run = self.get_run(run_id)
        if run is None:
            return None

        task = BacktestTask(
            backtest_run_id=run_id,
            pair=payload.pair,
            timeframe=payload.timeframe,
            config_path=payload.config_path,
        )
        run.requested_task_count += 1
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def get_task(self, task_id: int) -> Optional[BacktestTask]:
        return self.db.get(BacktestTask, task_id)

    def list_tasks(self, run_id: int) -> list[BacktestTask]:
        statement = (
            select(BacktestTask)
            .where(BacktestTask.backtest_run_id == run_id)
            .order_by(BacktestTask.created_at.asc(), BacktestTask.id.asc())
        )
        return list(self.db.scalars(statement).all())

    def list_all_tasks(self, limit: int = 100) -> list[BacktestTask]:
        statement = (
            select(BacktestTask)
            .order_by(BacktestTask.created_at.desc(), BacktestTask.id.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())

    def claim_next_pending_task(self, run_id: int) -> Optional[BacktestTask]:
        statement = (
            select(BacktestTask)
            .where(
                BacktestTask.backtest_run_id == run_id,
                BacktestTask.status == "pending",
            )
            .order_by(BacktestTask.created_at.asc(), BacktestTask.id.asc())
            .limit(1)
        )
        task = self.db.scalars(statement).first()
        if task is None:
            return None

        task.status = "running"
        task.started_at = datetime.now(timezone.utc)
        run = self.get_run(run_id)
        if run is not None and run.status == "pending":
            run.status = "running"
            run.started_at = task.started_at

        self.db.commit()
        self.db.refresh(task)
        return task

    def update_task_status(
        self,
        task_id: int,
        payload: BacktestTaskStatusUpdate,
    ) -> Optional[BacktestTask]:
        task = self.get_task(task_id)
        if task is None:
            return None

        task.status = payload.status
        if payload.status == "running" and task.started_at is None:
            task.started_at = datetime.now(timezone.utc)
        if payload.status in TERMINAL_STATUSES:
            task.completed_at = datetime.now(timezone.utc)
        if payload.result_path is not None:
            task.result_path = payload.result_path
        if payload.error_message is not None:
            task.error_message = payload.error_message

        self.db.commit()
        self.db.refresh(task)
        return task

    def save_result(
        self,
        task_id: int,
        payload: BacktestResultCreate,
    ) -> Optional[BacktestResult]:
        task = self.get_task(task_id)
        if task is None:
            return None

        result = task.result
        if result is None:
            result = BacktestResult(
                backtest_run_id=task.backtest_run_id,
                backtest_task_id=task.id,
                result_path=payload.result_path,
            )
            self.db.add(result)

        result.result_path = payload.result_path
        result.metrics_snapshot = payload.metrics_snapshot
        result.profit_total = payload.profit_total
        result.profit_pct = payload.profit_pct
        result.max_drawdown_pct = payload.max_drawdown_pct
        result.win_rate = payload.win_rate
        result.total_trades = payload.total_trades
        result.timerange = payload.timerange
        task.result_path = payload.result_path

        self.db.commit()
        self.db.refresh(result)
        return result

    def list_results(self, run_id: int) -> list[BacktestResult]:
        statement = (
            select(BacktestResult)
            .where(BacktestResult.backtest_run_id == run_id)
            .order_by(BacktestResult.created_at.asc(), BacktestResult.id.asc())
        )
        return list(self.db.scalars(statement).all())

    def list_all_results(self, limit: int = 100) -> list[BacktestResult]:
        statement = (
            select(BacktestResult)
            .order_by(BacktestResult.created_at.desc(), BacktestResult.id.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())
