from pathlib import Path
from typing import Optional, Protocol

from sqlalchemy.orm import Session

from app.adapters.freqtrade.result_parser import FreqtradeResultParser
from app.models.backtest import BacktestTask
from app.repositories import BacktestRepository
from app.schemas import BacktestRunStatusUpdate, BacktestTaskStatusUpdate


class BacktestRunner(Protocol):
    def run_backtest(
        self,
        config_path: Path,
        strategy_name: str,
        result_path: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Path:
        ...


class BacktestTaskExecutionService:
    def __init__(
        self,
        db: Session,
        runner: BacktestRunner,
        parser: Optional[FreqtradeResultParser] = None,
    ) -> None:
        self.repository = BacktestRepository(db)
        self.runner = runner
        self.parser = parser or FreqtradeResultParser()

    def execute_next_pending(
        self,
        run_id: int,
        strategy_name: str,
        result_path: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Optional[BacktestTask]:
        task = self.repository.claim_next_pending_task(run_id)
        if task is None:
            return None
        return self._execute_claimed_task(task, strategy_name, result_path, timeout_seconds)

    def _execute_claimed_task(
        self,
        task: BacktestTask,
        strategy_name: str,
        result_path: Optional[Path],
        timeout_seconds: Optional[int],
    ) -> BacktestTask:
        try:
            if task.config_path is None:
                raise ValueError("Backtest task requires config_path before execution")
            produced_result_path = self.runner.run_backtest(
                Path(task.config_path),
                strategy_name,
                result_path=result_path,
                timeout_seconds=timeout_seconds,
            )
            parsed_result = self.parser.parse_backtest_result(
                produced_result_path,
                strategy_name=strategy_name,
            )
            self.repository.save_result(task.id, parsed_result)
            updated = self.repository.update_task_status(
                task.id,
                BacktestTaskStatusUpdate(
                    status="succeeded",
                    result_path=str(produced_result_path),
                ),
            )
        except Exception as exc:
            updated = self.repository.update_task_status(
                task.id,
                BacktestTaskStatusUpdate(status="failed", error_message=str(exc)),
            )
            self._refresh_run_status(task.backtest_run_id)
            if updated is None:
                raise
            return updated

        self._refresh_run_status(task.backtest_run_id)
        if updated is None:
            raise RuntimeError(f"Backtest task disappeared during execution: {task.id}")
        return updated

    def _refresh_run_status(self, run_id: int) -> None:
        tasks = self.repository.list_tasks(run_id)
        if not tasks:
            return
        statuses = {task.status for task in tasks}
        if "running" in statuses:
            self.repository.update_run_status(run_id, BacktestRunStatusUpdate(status="running"))
            return
        if "pending" in statuses:
            return
        if "failed" in statuses:
            self.repository.update_run_status(run_id, BacktestRunStatusUpdate(status="failed"))
            return
        if statuses == {"cancelled"}:
            self.repository.update_run_status(run_id, BacktestRunStatusUpdate(status="cancelled"))
            return
        self.repository.update_run_status(run_id, BacktestRunStatusUpdate(status="succeeded"))
