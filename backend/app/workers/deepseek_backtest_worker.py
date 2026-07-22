from __future__ import annotations

import argparse
import socket
import sys
import time
from threading import Event, Thread
from typing import Callable, Optional
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.adapters.freqtrade.backtest_runner import FreqtradeBacktestRunner
from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner
from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.db.session import SessionLocal
from app.repositories.research_jobs import ResearchJobRepository
from app.schemas.deepseek_backtest_loop import (
    DeepSeekBacktestLoopRequest,
    DeepSeekBacktestLoopResponse,
)
from app.schemas.dry_run_status import redact_secret_text
from app.services.deepseek_backtest_loop import DeepSeekBacktestLoopService
from app.services.strategy_generation import (
    StrategyGenerationService,
    build_deepseek_single_provider_from_env,
)


ServiceFactory = Callable[[Session], DeepSeekBacktestLoopService]


def default_service_factory(db: Session) -> DeepSeekBacktestLoopService:
    return DeepSeekBacktestLoopService(
        db,
        generation_service=StrategyGenerationService(
            db,
            provider=build_deepseek_single_provider_from_env(),
            file_manager=StrategyFileManager(),
        ),
        backtest_runner=FreqtradeBacktestRunner(FreqtradeCliRunner()),
    )


class _Heartbeat:
    def __init__(
        self,
        session_factory: sessionmaker,
        job_id: int,
        lease_token: str,
        lease_seconds: int,
        interval_seconds: float,
    ) -> None:
        self.session_factory = session_factory
        self.job_id = job_id
        self.lease_token = lease_token
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self.stop_event = Event()
        self.lease_lost = Event()
        self.thread = Thread(target=self._run, name=f"research-job-heartbeat-{job_id}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=max(2.0, self.interval_seconds + 1.0))

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            with self.session_factory() as db:
                renewed = ResearchJobRepository(db).heartbeat(
                    self.job_id,
                    self.lease_token,
                    lease_seconds=self.lease_seconds,
                )
            if not renewed:
                self.lease_lost.set()
                return


class DeepSeekBacktestWorker:
    """Single-process local worker with DB leases and fail-closed recovery."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker = SessionLocal,
        service_factory: ServiceFactory = default_service_factory,
        owner: Optional[str] = None,
        lease_seconds: int = 300,
        heartbeat_interval_seconds: Optional[float] = None,
    ) -> None:
        self.session_factory = session_factory
        self.service_factory = service_factory
        self.owner = owner or f"{socket.gethostname()}:{uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds or max(
            1.0, min(30.0, lease_seconds / 3)
        )

    def run_once(self) -> Optional[int]:
        with self.session_factory() as db:
            job = ResearchJobRepository(db).claim_next(
                owner=self.owner,
                lease_seconds=self.lease_seconds,
            )
            if job is None:
                return None
            job_id = job.id
            lease_token = job.lease_token
        if not lease_token:
            raise RuntimeError(f"research job {job_id} was claimed without a lease token")

        heartbeat = _Heartbeat(
            self.session_factory,
            job_id,
            lease_token,
            self.lease_seconds,
            self.heartbeat_interval_seconds,
        )
        heartbeat.start()
        try:
            self._execute(job_id, lease_token, heartbeat)
        finally:
            heartbeat.stop()
        return job_id

    def _execute(self, job_id: int, lease_token: str, heartbeat: _Heartbeat) -> None:
        with self.session_factory() as db:
            repository = ResearchJobRepository(db)
            cancelled = repository.cancel_at_checkpoint(job_id, lease_token)
            if cancelled is not None:
                return
            job = repository.get(job_id)
            if job is None or job.status != "RUNNING" or job.lease_token != lease_token:
                return
            payload = DeepSeekBacktestLoopRequest.model_validate(job.request_payload)
            if payload.allow_real_call:
                if not repository.mark_provider_attempt(job_id, lease_token):
                    return

            try:
                response = self.service_factory(db).run(payload)
            except Exception as exc:
                if heartbeat.lease_lost.is_set():
                    return
                repository.complete(
                    job_id,
                    lease_token,
                    status="FAILED",
                    stage="FAILED",
                    links={},
                    evidence_snapshot={
                        "status": "FAILED",
                        "acceptance_ready": False,
                        "failed_reason": "Worker execution failed without a safe application response.",
                    },
                    error_message=redact_secret_text(str(exc))[:2000],
                    provider_completed=False,
                )
                return

            if heartbeat.lease_lost.is_set():
                return
            if repository.cancel_at_checkpoint(job_id, lease_token) is not None:
                return
            self._persist_response(repository, job_id, lease_token, payload, response)

    @staticmethod
    def _persist_response(
        repository: ResearchJobRepository,
        job_id: int,
        lease_token: str,
        payload: DeepSeekBacktestLoopRequest,
        response: DeepSeekBacktestLoopResponse,
    ) -> None:
        status = {
            "succeeded": "SUCCESS",
            "failed": "FAILED",
            "blocked": "BLOCKED",
        }[response.overall_status]
        evidence = response.evidence.model_dump(mode="json")
        links = {
            key: evidence.get("ids", {}).get(key)
            for key in (
                "strategy_generation_run_id",
                "strategy_id",
                "strategy_version_id",
                "backtest_run_id",
                "backtest_task_id",
                "backtest_result_id",
                "strategy_score_id",
            )
        }
        reason = evidence.get("blocked_reason") or evidence.get("failed_reason")
        if status == "SUCCESS":
            required_ids = {
                "strategy_generation_run_id",
                "strategy_id",
                "strategy_version_id",
                "backtest_run_id",
                "backtest_task_id",
                "backtest_result_id",
                "strategy_score_id",
            }
            missing_ids = sorted(key for key in required_ids if not links.get(key))
            if not evidence.get("acceptance_ready") or missing_ids:
                status = "FAILED"
                reason = (
                    "Worker refused an incomplete SUCCESS response; missing reconciled database ids: "
                    + ", ".join(missing_ids or ["acceptance_ready"])
                )
                evidence = {
                    **evidence,
                    "status": "FAILED",
                    "acceptance_ready": False,
                    "failed_reason": reason,
                }
        repository.complete(
            job_id,
            lease_token,
            status=status,
            stage="COMPLETED" if status == "SUCCESS" else status,
            links=links,
            evidence_snapshot=evidence,
            error_message=redact_secret_text(reason)[:2000] if isinstance(reason, str) else None,
            provider_completed=payload.allow_real_call,
        )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local DB-backed DeepSeek backtest worker.")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit.")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--lease-seconds", type=int, default=300)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    worker = DeepSeekBacktestWorker(lease_seconds=max(5, args.lease_seconds))
    if args.once:
        worker.run_once()
        return 0
    try:
        while True:
            processed = worker.run_once()
            if processed is None:
                time.sleep(max(0.1, args.poll_interval))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
