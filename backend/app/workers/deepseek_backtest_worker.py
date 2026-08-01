from __future__ import annotations

import argparse
import socket
import sys
import time
from threading import Event, Thread
from typing import Callable, Optional, Protocol
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
from app.services.research_full_chain_orchestrator import (
    ResearchFullChainBlocked,
    default_research_full_chain_orchestrator_factory,
)
from app.services.strategy_deployment_continuation import (
    StrategyDeploymentContinuationBlocked,
    default_strategy_deployment_continuation_factory,
)
from app.services.strategy_generation import (
    StrategyGenerationService,
    build_deepseek_single_provider_from_env,
)

ServiceFactory = Callable[[Session], DeepSeekBacktestLoopService]


class ApprovedCandidateContinuation(Protocol):
    """Continue one already-approved candidate without repeating research."""

    def run(self, job_id: int, lease_token: str) -> None: ...


ContinuationFactory = Callable[[Session], ApprovedCandidateContinuation]
ResearchChainFactory = Callable[[Session], object]


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
        continuation_factory: Optional[
            ContinuationFactory
        ] = default_strategy_deployment_continuation_factory,
        research_chain_factory: ResearchChainFactory = (
            default_research_full_chain_orchestrator_factory
        ),
        owner: Optional[str] = None,
        lease_seconds: int = 300,
        heartbeat_interval_seconds: Optional[float] = None,
    ) -> None:
        self.session_factory = session_factory
        self.service_factory = service_factory
        self.continuation_factory = continuation_factory
        self.research_chain_factory = research_chain_factory
        self.owner = owner or f"{socket.gethostname()}:{uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds or max(
            1.0, min(30.0, lease_seconds / 3)
        )

    def run_once(self) -> Optional[int]:
        with self.session_factory() as db:
            repository = ResearchJobRepository(db)
            repository.expire_stale()
            self.research_chain_factory(db).recover_one_stale()
            job = repository.claim_next(
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
            job = repository.get(job_id)
            if job is None or job.status != "RUNNING" or job.lease_token != lease_token:
                return
            chain = self.research_chain_factory(db)
            if job.cancel_requested:
                chain.terminalize_owned(
                    job_id,
                    lease_token,
                    status="CANCELLED",
                    stage="CANCELLED",
                    reason=job.error_message or "Cancelled by local operator.",
                    provider_completed=False,
                )
                return
            if job.stage == "SIGNAL":
                self._execute_approved_candidate_continuation(
                    repository,
                    job_id,
                    lease_token,
                    heartbeat,
                )
                return
            if job.stage == "PERSISTED_RESULT_RECOVERY":
                try:
                    chain.advance(
                        job_id,
                        lease_token,
                    )
                except ResearchFullChainBlocked as exc:
                    chain.terminalize_owned(
                        job_id,
                        lease_token,
                        status="BLOCKED",
                        stage="CANDIDATE_APPROVAL",
                        links={},
                        evidence_snapshot={
                            "status": "BLOCKED",
                            "acceptance_ready": False,
                            "failed_reason": redact_secret_text(str(exc))[:2000],
                        },
                        reason=redact_secret_text(str(exc))[:2000],
                        provider_completed=True,
                    )
                return
            payload = DeepSeekBacktestLoopRequest.model_validate(job.request_payload)
            try:
                chain.begin(job_id, lease_token)
            except ResearchFullChainBlocked as exc:
                chain.terminalize_owned(
                    job_id,
                    lease_token,
                    status="BLOCKED",
                    stage="GENERATION",
                    reason=redact_secret_text(str(exc))[:2000],
                    provider_completed=False,
                )
                return
            if payload.allow_real_call:
                if not repository.mark_provider_attempt(job_id, lease_token):
                    return

            try:
                response = self.service_factory(db).run(payload)
            except Exception as exc:
                if heartbeat.lease_lost.is_set():
                    return
                status = "STALE" if payload.allow_real_call else "FAILED"
                reason = redact_secret_text(str(exc))[:2000]
                chain.terminalize_owned(
                    job_id,
                    lease_token,
                    status=status,
                    stage="PROVIDER_OUTCOME_UNKNOWN" if status == "STALE" else "FAILED",
                    reason=reason,
                    evidence_snapshot={
                        "status": status,
                        "acceptance_ready": False,
                        "recovery_allowed": False,
                    },
                    provider_completed=False,
                )
                return

            if heartbeat.lease_lost.is_set():
                return
            current = repository.get(job_id)
            if current is not None and current.cancel_requested:
                chain.terminalize_owned(
                    job_id,
                    lease_token,
                    status="CANCELLED",
                    stage="CANCELLED",
                    reason=current.error_message or "Cancelled by local operator.",
                    provider_completed=payload.allow_real_call,
                )
                return
            if response.overall_status == "succeeded":
                try:
                    chain.checkpoint_response(job_id, lease_token, response)
                    chain.advance(job_id, lease_token, response)
                except ResearchFullChainBlocked as exc:
                    chain.terminalize_owned(
                        job_id,
                        lease_token,
                        status="BLOCKED",
                        stage="CANDIDATE_APPROVAL",
                        links={},
                        evidence_snapshot={
                            "status": "BLOCKED",
                            "acceptance_ready": False,
                            "failed_reason": redact_secret_text(str(exc))[:2000],
                        },
                        reason=redact_secret_text(str(exc))[:2000],
                        provider_completed=payload.allow_real_call,
                    )
                return
            reason = (
                response.evidence.blocked_reason
                or response.evidence.failed_reason
                or "Research execution did not succeed."
            )
            chain.terminalize_owned(
                job_id,
                lease_token,
                status=(
                    "BLOCKED"
                    if response.overall_status == "blocked"
                    else "FAILED"
                ),
                stage=(
                    "BLOCKED"
                    if response.overall_status == "blocked"
                    else "FAILED"
                ),
                reason=reason,
                links={},
                evidence_snapshot=response.evidence.model_dump(mode="json"),
                provider_completed=payload.allow_real_call,
            )

    def _execute_approved_candidate_continuation(
        self,
        repository: ResearchJobRepository,
        job_id: int,
        lease_token: str,
        heartbeat: _Heartbeat,
    ) -> None:
        """Dispatch SIGNAL without ever re-entering DeepSeek or backtesting."""

        if self.continuation_factory is None:
            self._complete_signal_failure(
                repository,
                job_id,
                lease_token,
                status="BLOCKED",
                reason=(
                    "Approved candidate continuation is not configured; "
                    "DeepSeek and backtesting were not repeated."
                ),
            )
            return
        try:
            self.continuation_factory(repository.db).run(job_id, lease_token)
        except StrategyDeploymentContinuationBlocked as exc:
            if heartbeat.lease_lost.is_set():
                return
            self._complete_signal_failure(
                repository,
                job_id,
                lease_token,
                status="BLOCKED",
                reason=redact_secret_text(str(exc))[:2000],
            )
            return
        except Exception as exc:
            if heartbeat.lease_lost.is_set():
                return
            self._complete_signal_failure(
                repository,
                job_id,
                lease_token,
                status="FAILED",
                reason=redact_secret_text(str(exc))[:2000],
            )
            return
        if heartbeat.lease_lost.is_set():
            return
        current = repository.get(job_id)
        if (
            current is not None
            and current.status == "RUNNING"
            and current.lease_token == lease_token
        ):
            self._complete_signal_failure(
                repository,
                job_id,
                lease_token,
                status="BLOCKED",
                reason=(
                    "Approved candidate continuation returned without a durable "
                    "terminal checkpoint."
                ),
            )

    def _complete_signal_failure(
        self,
        repository: ResearchJobRepository,
        job_id: int,
        lease_token: str,
        *,
        status: str,
        reason: str,
    ) -> None:
        job = repository.get(job_id)
        evidence = job.evidence_snapshot if job is not None else {}
        self.research_chain_factory(repository.db).terminalize_owned(
            job_id,
            lease_token,
            status=status,
            stage="SIGNAL",
            links={},
            evidence_snapshot={
                **evidence,
                "status": status,
                "acceptance_ready": False,
                "failed_reason": reason,
            },
            reason=reason,
            provider_completed=False,
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
