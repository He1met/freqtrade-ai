from datetime import datetime, timedelta, timezone
from typing import Collection, Optional
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.backtest import BacktestResult, BacktestRun, BacktestTask
from app.models.execution_lineage import (
    LOCAL_DRY_RUN_SCOPE_ID,
    ResearchJobAttempt,
)
from app.models.full_chain import FullChainRun, FullChainStageRun
from app.models.research_job import ResearchJob, ResearchWorkerControl
from app.models.strategy import Strategy, StrategyVersion
from app.models.strategy_generation_run import StrategyGenerationRun
from app.models.strategy_score import StrategyScore
from app.repositories.execution_lineage import ensure_execution_scope_catalog

TERMINAL_JOB_STATUSES = {
    "SUCCESS",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
    "STALE",
}
RECOVERY_JOB_STAGES = {
    "CANDIDATE_APPROVED",
    "GENERATION_RETRY",
    "VALIDATION_RETRY",
    "PERSISTED_RESULT_RECOVERY",
    "SIGNAL_RECOVERY",
}


class ResearchJobLinkageBlocked(ValueError):
    """Completion evidence failed scope or relationship-chain validation."""


class ResearchJobRepository:
    """Database-fenced job queue with one global local execution lease."""

    def __init__(self, db: Session, execution_scope_id: str = LOCAL_DRY_RUN_SCOPE_ID) -> None:
        self.db = db
        self.execution_scope_id = execution_scope_id

    def _require_executable_scope(self) -> None:
        if self.execution_scope_id != LOCAL_DRY_RUN_SCOPE_ID:
            raise ValueError("non-executable or unknown research job scope is read-only")

    def get(self, job_id: int) -> Optional[ResearchJob]:
        statement = select(ResearchJob).where(
            ResearchJob.id == job_id,
            ResearchJob.execution_scope_id == self.execution_scope_id,
        )
        return self.db.scalars(statement).first()

    def list(self, limit: int = 100) -> list[ResearchJob]:
        statement = (
            select(ResearchJob)
            .where(ResearchJob.execution_scope_id == self.execution_scope_id)
            .order_by(ResearchJob.created_at.desc(), ResearchJob.id.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())

    def find_idempotent(
        self,
        operation: str,
        idempotency_key_digest: str,
    ) -> Optional[ResearchJob]:
        statement = select(ResearchJob).where(
            ResearchJob.execution_scope_id == self.execution_scope_id,
            ResearchJob.operation == operation,
            ResearchJob.idempotency_key_digest == idempotency_key_digest,
        )
        return self.db.scalars(statement).first()

    def create(
        self,
        *,
        job_type: str,
        operation: str,
        idempotency_key_digest: str,
        request_hash: str,
        request_payload: dict,
    ) -> ResearchJob:
        self._require_executable_scope()
        ensure_execution_scope_catalog(self.db)
        job = ResearchJob(
            execution_scope_id=self.execution_scope_id,
            job_type=job_type,
            operation=operation,
            idempotency_key_digest=idempotency_key_digest,
            request_hash=request_hash,
            request_payload=request_payload,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def create_or_get(
        self,
        *,
        job_type: str,
        operation: str,
        idempotency_key_digest: str,
        request_hash: str,
        request_payload: dict,
    ) -> tuple[ResearchJob, bool]:
        existing = self.find_idempotent(operation, idempotency_key_digest)
        if existing is not None:
            return existing, False
        try:
            return (
                self.create(
                    job_type=job_type,
                    operation=operation,
                    idempotency_key_digest=idempotency_key_digest,
                    request_hash=request_hash,
                    request_payload=request_payload,
                ),
                True,
            )
        except IntegrityError:
            self.db.rollback()
            existing = self.find_idempotent(operation, idempotency_key_digest)
            if existing is None:
                raise
            return existing, False

    def get_control(self) -> ResearchWorkerControl:
        control = self.db.get(ResearchWorkerControl, 1)
        if control is not None:
            return control
        self._require_executable_scope()
        try:
            control = ResearchWorkerControl(id=1, paused=False)
            self.db.add(control)
            self.db.commit()
            self.db.refresh(control)
            return control
        except IntegrityError:
            self.db.rollback()
            control = self.db.get(ResearchWorkerControl, 1)
            if control is None:
                raise
            return control

    def set_paused(self, paused: bool, reason: Optional[str]) -> ResearchWorkerControl:
        self._require_executable_scope()
        self.get_control()
        control = self.db.get(ResearchWorkerControl, 1)
        if control is None:
            raise RuntimeError("research worker control disappeared")
        control.paused = paused
        control.reason = reason if paused else None
        self.db.commit()
        self.db.refresh(control)
        return control

    def status_counts(self) -> dict[str, int]:
        statement = (
            select(ResearchJob.status, func.count(ResearchJob.id))
            .where(ResearchJob.execution_scope_id == self.execution_scope_id)
            .group_by(ResearchJob.status)
        )
        return {status: count for status, count in self.db.execute(statement).all()}

    def claim_next(
        self,
        *,
        owner: str,
        lease_seconds: int,
        now: Optional[datetime] = None,
        operations: Optional[Collection[str]] = None,
    ) -> Optional[ResearchJob]:
        self._require_executable_scope()
        current_time = now or datetime.now(timezone.utc)
        self.get_control()
        self.expire_stale(current_time)
        lease_token = uuid4().hex

        self.db.rollback()
        with self.db.begin():
            reservation = self.db.execute(
                update(ResearchWorkerControl)
                .where(
                    ResearchWorkerControl.id == 1,
                    ResearchWorkerControl.paused.is_(False),
                    ResearchWorkerControl.active_job_id.is_(None),
                )
                .values(active_job_id=0, active_lease_token=lease_token)
            )
            if reservation.rowcount != 1:
                return None

            statement = (
                select(ResearchJob)
                .where(
                    ResearchJob.execution_scope_id == self.execution_scope_id,
                    ResearchJob.status == "PENDING",
                    or_(
                        ResearchJob.attempt_count < ResearchJob.max_attempts,
                        and_(
                            ResearchJob.stage.in_(RECOVERY_JOB_STAGES),
                            ResearchJob.attempt_count >= 1,
                        ),
                    ),
                )
                .order_by(ResearchJob.created_at.asc(), ResearchJob.id.asc())
                .limit(1)
            )
            if operations is not None:
                operation_set = tuple(sorted(set(operations)))
                if not operation_set:
                    raise ValueError("operations must not be empty when provided")
                statement = statement.where(ResearchJob.operation.in_(operation_set))
            if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            job = self.db.scalars(statement).first()
            if job is None:
                self.db.execute(
                    update(ResearchWorkerControl)
                    .where(
                        ResearchWorkerControl.id == 1,
                        ResearchWorkerControl.active_job_id == 0,
                        ResearchWorkerControl.active_lease_token == lease_token,
                    )
                    .values(active_job_id=None, active_lease_token=None)
                )
                return None

            recovery_stage = job.stage
            resuming_existing_attempt = (
                recovery_stage in RECOVERY_JOB_STAGES
                and job.attempt_count >= 1
            )
            job.status = "RUNNING"
            job.stage = (
                "SIGNAL"
                if recovery_stage in {"CANDIDATE_APPROVED", "SIGNAL_RECOVERY"}
                else recovery_stage
                if resuming_existing_attempt
                else "GENERATION"
            )
            job.lease_owner = owner
            job.lease_token = lease_token
            job.heartbeat_at = current_time
            job.lease_expires_at = current_time + timedelta(seconds=lease_seconds)
            if resuming_existing_attempt:
                attempt = self.db.scalars(
                    select(ResearchJobAttempt)
                    .where(
                        ResearchJobAttempt.research_job_id == job.id,
                        ResearchJobAttempt.execution_scope_id
                        == self.execution_scope_id,
                        ResearchJobAttempt.attempt_number == job.attempt_count,
                        ResearchJobAttempt.status.in_(
                            {"AWAITING_APPROVAL", "STALE"}
                        ),
                    )
                    .limit(1)
                ).first()
                if attempt is None:
                    raise ResearchJobLinkageBlocked(
                        "recoverable job has no matching prior attempt"
                    )
                attempt.status = "RUNNING"
                attempt.completed_at = None
            else:
                job.attempt_count += 1
                self.db.add(
                    ResearchJobAttempt(
                        research_job_id=job.id,
                        attempt_number=job.attempt_count,
                        execution_scope_id=job.execution_scope_id,
                        status="RUNNING",
                        started_at=current_time,
                    )
                )
            job.started_at = job.started_at or current_time
            self.db.execute(
                update(ResearchWorkerControl)
                .where(
                    ResearchWorkerControl.id == 1,
                    ResearchWorkerControl.active_job_id == 0,
                    ResearchWorkerControl.active_lease_token == lease_token,
                )
                .values(active_job_id=job.id)
            )

        self.db.refresh(job)
        return job

    def checkpoint_research_result(
        self,
        job_id: int,
        lease_token: str,
        *,
        links: dict[str, Optional[int]],
        evidence_snapshot: dict,
        now: Optional[datetime] = None,
    ) -> Optional[ResearchJob]:
        """Persist Provider/backtest output before full-chain advancement."""

        self._require_executable_scope()
        current_time = now or datetime.now(timezone.utc)
        job = self.get(job_id)
        if (
            job is None
            or job.status != "RUNNING"
            or job.lease_token != lease_token
            or job.stage not in {"GENERATION", "GENERATION_RETRY", "PROVIDER_CALL"}
            or job.lease_expires_at is None
            or _as_utc(job.lease_expires_at)
            <= _as_utc(current_time)
        ):
            return None
        validated_links = self._validate_completion_links(job, links)
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job_id,
                ResearchJob.execution_scope_id == self.execution_scope_id,
                ResearchJob.status == "RUNNING",
                ResearchJob.lease_token == lease_token,
                ResearchJob.stage.in_(
                    {"GENERATION", "GENERATION_RETRY", "PROVIDER_CALL"}
                ),
                ResearchJob.lease_expires_at > current_time,
            )
            .values(
                **validated_links,
                provider_completed_at=current_time,
                stage="PERSISTED_RESULT",
                evidence_snapshot=evidence_snapshot,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        self.db.commit()
        return self.get(job_id)

    def prepare_stale_recovery(
        self,
        job_id: int,
        *,
        recovery_stage: str,
        commit: bool = True,
    ) -> Optional[ResearchJob]:
        """Requeue one stale attempt only under an explicit safe recovery mode."""

        self._require_executable_scope()
        if recovery_stage not in {
            "GENERATION_RETRY",
            "VALIDATION_RETRY",
            "PERSISTED_RESULT_RECOVERY",
            "SIGNAL_RECOVERY",
        }:
            raise ValueError("invalid research recovery stage")
        job = self.get(job_id)
        if job is None:
            return None
        recovery_guards = (
            (
                ResearchJob.provider_attempted_at.is_(None),
                ResearchJob.provider_completed_at.is_(None),
                ResearchJob.strategy_generation_run_id.is_(None),
                ResearchJob.strategy_id.is_(None),
                ResearchJob.strategy_version_id.is_(None),
                ResearchJob.backtest_run_id.is_(None),
                ResearchJob.backtest_task_id.is_(None),
                ResearchJob.backtest_result_id.is_(None),
                ResearchJob.strategy_score_id.is_(None),
            )
            if recovery_stage in {"GENERATION_RETRY", "VALIDATION_RETRY"}
            else (
                ResearchJob.provider_attempted_at.is_not(None),
                ResearchJob.provider_completed_at.is_not(None),
                ResearchJob.strategy_generation_run_id.is_not(None),
                ResearchJob.strategy_id.is_not(None),
                ResearchJob.strategy_version_id.is_not(None),
                ResearchJob.backtest_run_id.is_not(None),
                ResearchJob.backtest_task_id.is_not(None),
                ResearchJob.backtest_result_id.is_not(None),
                ResearchJob.strategy_score_id.is_not(None),
            )
            if recovery_stage == "PERSISTED_RESULT_RECOVERY"
            else (
                ResearchJob.strategy_generation_run_id.is_not(None),
                ResearchJob.strategy_id.is_not(None),
                ResearchJob.strategy_version_id.is_not(None),
                ResearchJob.backtest_run_id.is_not(None),
                ResearchJob.backtest_task_id.is_not(None),
                ResearchJob.backtest_result_id.is_not(None),
                ResearchJob.strategy_score_id.is_not(None),
            )
        )
        if recovery_stage == "PERSISTED_RESULT_RECOVERY":
            recovery_guards = (
                or_(
                    ResearchJob.provider_attempted_at.is_not(None),
                    and_(
                        ResearchJob.job_type == "formal_candidate_validation",
                        ResearchJob.operation
                        == "strategy_research.candidate_validation_queue_v1",
                        ResearchJob.provider_attempted_at.is_(None),
                    ),
                ),
                ResearchJob.provider_completed_at.is_not(None),
                ResearchJob.strategy_generation_run_id.is_not(None),
                ResearchJob.strategy_id.is_not(None),
                ResearchJob.strategy_version_id.is_not(None),
                ResearchJob.backtest_run_id.is_not(None),
                ResearchJob.backtest_task_id.is_not(None),
                ResearchJob.backtest_result_id.is_not(None),
                ResearchJob.strategy_score_id.is_not(None),
            )
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job_id,
                ResearchJob.execution_scope_id == self.execution_scope_id,
                ResearchJob.status == "STALE",
                ResearchJob.stage == "LEASE_EXPIRED",
                ResearchJob.lease_token.is_(None),
                *recovery_guards,
            )
            .values(
                status="PENDING",
                stage=recovery_stage,
                completed_at=None,
                error_message=None,
                evidence_snapshot={
                    **job.evidence_snapshot,
                    "status": "PENDING",
                    "acceptance_ready": False,
                    "recovery_stage": recovery_stage,
                },
            )
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return self.get(job_id)

    def heartbeat(
        self,
        job_id: int,
        lease_token: str,
        *,
        lease_seconds: int,
        now: Optional[datetime] = None,
    ) -> bool:
        self._require_executable_scope()
        current_time = now or datetime.now(timezone.utc)
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job_id,
                ResearchJob.execution_scope_id == self.execution_scope_id,
                ResearchJob.status == "RUNNING",
                ResearchJob.lease_token == lease_token,
                ResearchJob.lease_expires_at > current_time,
            )
            .values(
                heartbeat_at=current_time,
                lease_expires_at=current_time + timedelta(seconds=lease_seconds),
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        return result.rowcount == 1

    def mark_provider_attempt(
        self,
        job_id: int,
        lease_token: str,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        self._require_executable_scope()
        current_time = now or datetime.now(timezone.utc)
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job_id,
                ResearchJob.execution_scope_id == self.execution_scope_id,
                ResearchJob.status == "RUNNING",
                ResearchJob.lease_token == lease_token,
                ResearchJob.provider_attempted_at.is_(None),
                ResearchJob.cancel_requested.is_(False),
                ResearchJob.lease_expires_at > current_time,
            )
            .values(provider_attempted_at=current_time, stage="PROVIDER_CALL")
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        return result.rowcount == 1

    def complete(
        self,
        job_id: int,
        lease_token: str,
        *,
        status: str,
        stage: str,
        links: dict[str, Optional[int]],
        evidence_snapshot: dict,
        error_message: Optional[str],
        provider_completed: bool,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> Optional[ResearchJob]:
        self._require_executable_scope()
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError(f"invalid terminal job status: {status}")
        current_time = now or datetime.now(timezone.utc)
        job = self.get(job_id)
        if (
            job is None
            or job.status != "RUNNING"
            or job.lease_token != lease_token
            or job.lease_expires_at is None
            or _as_utc(job.lease_expires_at) <= _as_utc(current_time)
        ):
            return None
        validated_links = self._validate_completion_links(job, links)
        values = {
            "status": status,
            "stage": stage,
            "evidence_snapshot": evidence_snapshot,
            "error_message": error_message,
            "completed_at": current_time,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
        }
        if provider_completed:
            values["provider_completed_at"] = current_time
        values.update(validated_links)
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job_id,
                ResearchJob.execution_scope_id == self.execution_scope_id,
                ResearchJob.status == "RUNNING",
                ResearchJob.lease_token == lease_token,
                ResearchJob.lease_expires_at > current_time,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        attempt = self.db.scalars(
            select(ResearchJobAttempt)
            .where(
                ResearchJobAttempt.research_job_id == job_id,
                ResearchJobAttempt.execution_scope_id == self.execution_scope_id,
            )
            .order_by(ResearchJobAttempt.attempt_number.desc())
            .limit(1)
        ).first()
        if attempt is not None:
            attempt.status = status
            attempt.completed_at = current_time
            attempt.evidence_snapshot = evidence_snapshot
            attempt.error_message = error_message
        self._release_control(job_id, lease_token)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        self.db.refresh(job)
        return job

    def cancel(self, job_id: int, reason: str) -> Optional[ResearchJob]:
        self._require_executable_scope()
        job = self.get(job_id)
        if job is None:
            return None
        if job.status in {"PENDING", "AWAITING_APPROVAL"}:
            job.status = "CANCELLED"
            job.stage = "CANCELLED"
            job.error_message = reason
            job.cancel_requested = True
            job.evidence_snapshot = {
                "status": "CANCELLED",
                "acceptance_ready": False,
                "failed_reason": reason,
            }
            job.completed_at = datetime.now(timezone.utc)
        elif job.status == "RUNNING":
            job.cancel_requested = True
            job.error_message = reason
        self.db.commit()
        self.db.refresh(job)
        return job

    def wait_for_candidate_approval(
        self,
        job_id: int,
        lease_token: str,
        *,
        evidence_snapshot: dict,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> Optional[ResearchJob]:
        """Release the one worker lease while an operator reviews a candidate."""

        self._require_executable_scope()
        current_time = now or datetime.now(timezone.utc)
        job = self.get(job_id)
        if (
            job is None
            or job.status != "RUNNING"
            or job.lease_token != lease_token
            or job.cancel_requested
        ):
            return None
        attempt = self.db.scalars(
            select(ResearchJobAttempt)
            .where(
                ResearchJobAttempt.research_job_id == job_id,
                ResearchJobAttempt.execution_scope_id == self.execution_scope_id,
                ResearchJobAttempt.attempt_number == job.attempt_count,
                ResearchJobAttempt.status == "RUNNING",
            )
            .limit(1)
        ).first()
        if attempt is None:
            return None
        values = {
            "status": "AWAITING_APPROVAL",
            "stage": "CANDIDATE_APPROVAL",
            "evidence_snapshot": evidence_snapshot,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
        }
        if job.provider_attempted_at is not None:
            values["provider_completed_at"] = current_time
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job_id,
                ResearchJob.execution_scope_id == self.execution_scope_id,
                ResearchJob.status == "RUNNING",
                ResearchJob.lease_token == lease_token,
                ResearchJob.cancel_requested.is_(False),
            )
            .values(**values)
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        attempt.status = "AWAITING_APPROVAL"
        attempt.completed_at = current_time
        attempt.evidence_snapshot = evidence_snapshot
        self._release_control(job_id, lease_token)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return self.get(job_id)

    def resume_after_candidate_approval(
        self,
        job_id: int,
        *,
        evidence_snapshot: dict,
        commit: bool = True,
    ) -> Optional[ResearchJob]:
        """Make an approved waiting job claimable by the same unique queue."""

        self._require_executable_scope()
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job_id,
                ResearchJob.execution_scope_id == self.execution_scope_id,
                ResearchJob.status == "AWAITING_APPROVAL",
                ResearchJob.cancel_requested.is_(False),
                ResearchJob.lease_token.is_(None),
            )
            .values(
                status="PENDING",
                stage="CANDIDATE_APPROVED",
                evidence_snapshot=evidence_snapshot,
                error_message=None,
            )
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return self.get(job_id)

    def block_waiting_candidate_approval(
        self,
        job_id: int,
        *,
        reason: str,
        evidence_snapshot: dict,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> Optional[ResearchJob]:
        self._require_executable_scope()
        current_time = now or datetime.now(timezone.utc)
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job_id,
                ResearchJob.execution_scope_id == self.execution_scope_id,
                ResearchJob.status == "AWAITING_APPROVAL",
                ResearchJob.lease_token.is_(None),
            )
            .values(
                status="BLOCKED",
                stage="CANDIDATE_APPROVAL",
                evidence_snapshot=evidence_snapshot,
                error_message=reason,
                completed_at=current_time,
            )
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return self.get(job_id)

    def cancel_at_checkpoint(
        self,
        job_id: int,
        lease_token: str,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[ResearchJob]:
        self._require_executable_scope()
        job = self.get(job_id)
        if job is None or not job.cancel_requested:
            return None
        return self.complete(
            job_id,
            lease_token,
            status="CANCELLED",
            stage="CANCELLED",
            links={},
            evidence_snapshot={
                **job.evidence_snapshot,
                "status": "CANCELLED",
                "acceptance_ready": False,
                "failed_reason": job.error_message or "Cancelled by local operator.",
            },
            error_message=job.error_message or "Cancelled by local operator.",
            provider_completed=False,
            now=now,
        )

    def expire_stale(self, now: Optional[datetime] = None) -> Optional[ResearchJob]:
        self._require_executable_scope()
        current_time = now or datetime.now(timezone.utc)
        self.get_control()
        control = self.db.get(ResearchWorkerControl, 1)
        if control is None or control.active_job_id in {None, 0}:
            return None
        job = self.get(control.active_job_id)
        if (
            job is None
            or job.status != "RUNNING"
            or job.lease_token != control.active_lease_token
            or job.lease_expires_at is None
            or _as_utc(job.lease_expires_at) > _as_utc(current_time)
        ):
            return None
        lease_token = job.lease_token
        chain = self.db.scalar(
            select(FullChainRun).where(
                FullChainRun.research_job_id == job.id,
                FullChainRun.run_kind == "RESEARCH",
            )
        )
        checkpoint = (
            self.db.scalar(
                select(FullChainStageRun)
                .where(
                    FullChainStageRun.full_chain_run_id == chain.id,
                    FullChainStageRun.status == "PREPARED",
                )
                .order_by(FullChainStageRun.id.desc())
                .limit(1)
            )
            if chain is not None
            else None
        )
        previous_stage = job.stage
        previous_chain_status = chain.status if chain is not None else None
        stale_reason = (
            "Provider outcome is unknown after lease expiry; automatic retry is forbidden."
            if job.provider_attempted_at is not None and job.provider_completed_at is None
            else "Worker lease expired before a safe terminal checkpoint."
        )
        stale_evidence = {
            **job.evidence_snapshot,
            "status": "STALE",
            "acceptance_ready": False,
            "recovery_allowed": (
                previous_stage == "SIGNAL"
                or job.provider_attempted_at is None
                or job.provider_completed_at is not None
            ),
            "previous_stage": previous_stage,
            "failed_reason": stale_reason,
            **(
                {
                    "full_chain_run_id": chain.id,
                    "full_chain_status": "STALE",
                    "previous_full_chain_status": previous_chain_status,
                }
                if chain is not None
                else {}
            ),
        }
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job.id,
                ResearchJob.execution_scope_id == self.execution_scope_id,
                ResearchJob.status == "RUNNING",
                ResearchJob.lease_token == lease_token,
                ResearchJob.lease_expires_at <= current_time,
            )
            .values(
                status="STALE",
                stage="LEASE_EXPIRED",
                error_message=stale_reason,
                evidence_snapshot=stale_evidence,
                completed_at=current_time,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        attempt = self.db.scalars(
            select(ResearchJobAttempt)
            .where(
                ResearchJobAttempt.research_job_id == job.id,
                ResearchJobAttempt.execution_scope_id == self.execution_scope_id,
            )
            .order_by(ResearchJobAttempt.attempt_number.desc())
            .limit(1)
        ).first()
        if attempt is not None:
            attempt.status = "STALE"
            attempt.completed_at = current_time
            attempt.error_message = stale_reason
            attempt.evidence_snapshot = stale_evidence
        if chain is not None and chain.status not in {
            "SUCCESS",
            "FAILED",
            "BLOCKED",
            "CANCELLED",
            "STALE",
        }:
            chain.status = "STALE"
            chain.terminal_reason = stale_reason
            chain.completed_at = current_time
        if checkpoint is not None:
            checkpoint.status = "STALE"
            checkpoint.error_code = "RESEARCH_LEASE_EXPIRED"
            checkpoint.error_message = stale_reason
            checkpoint.completed_at = current_time
        self._release_control(job.id, lease_token)
        self.db.commit()
        return self.get(job.id)

    def _validate_completion_links(
        self,
        job: ResearchJob,
        links: dict[str, Optional[int]],
    ) -> dict[str, Optional[int]]:
        unknown_columns = sorted(set(links) - _LINK_COLUMNS)
        if unknown_columns:
            self._block_linkage(
                "unknown completion link columns: " + ", ".join(unknown_columns)
            )
        normalized = {key: links.get(key) for key in _LINK_COLUMNS if key in links}

        generation_run = self._linked_row(
            StrategyGenerationRun,
            normalized.get("strategy_generation_run_id"),
            "strategy_generation_run_id",
        )
        if (
            generation_run is not None
            and generation_run.execution_scope_id != job.execution_scope_id
        ):
            self._block_linkage("strategy generation run belongs to another scope")

        backtest_run = self._linked_row(
            BacktestRun,
            normalized.get("backtest_run_id"),
            "backtest_run_id",
        )
        if backtest_run is not None and backtest_run.execution_scope_id != job.execution_scope_id:
            self._block_linkage("backtest run belongs to another scope")

        backtest_task = self._linked_row(
            BacktestTask,
            normalized.get("backtest_task_id"),
            "backtest_task_id",
        )
        if backtest_task is not None:
            task_run = self.db.get(BacktestRun, backtest_task.backtest_run_id)
            if task_run is None or task_run.execution_scope_id != job.execution_scope_id:
                self._block_linkage("backtest task belongs to another or missing scope")
            if backtest_run is not None and backtest_task.backtest_run_id != backtest_run.id:
                self._block_linkage("backtest task does not belong to linked backtest run")

        backtest_result = self._linked_row(
            BacktestResult,
            normalized.get("backtest_result_id"),
            "backtest_result_id",
        )
        if backtest_result is not None:
            result_run = self.db.get(BacktestRun, backtest_result.backtest_run_id)
            result_task = self.db.get(BacktestTask, backtest_result.backtest_task_id)
            if (
                result_run is None
                or result_run.execution_scope_id != job.execution_scope_id
                or result_task is None
                or result_task.backtest_run_id != result_run.id
            ):
                self._block_linkage(
                    "backtest result has a missing, cross-scope, or inconsistent chain"
                )
            if backtest_run is not None and backtest_result.backtest_run_id != backtest_run.id:
                self._block_linkage("backtest result does not belong to linked backtest run")
            if backtest_task is not None and backtest_result.backtest_task_id != backtest_task.id:
                self._block_linkage("backtest result does not belong to linked backtest task")

        strategy = self._linked_row(
            Strategy,
            normalized.get("strategy_id"),
            "strategy_id",
        )
        strategy_version = self._linked_row(
            StrategyVersion,
            normalized.get("strategy_version_id"),
            "strategy_version_id",
        )
        if strategy_version is not None:
            if strategy is not None and strategy_version.strategy_id != strategy.id:
                self._block_linkage("strategy version does not belong to linked strategy")
            if (
                generation_run is not None
                and strategy_version.generation_run_id != generation_run.id
            ):
                self._block_linkage(
                    "strategy version does not belong to linked generation run"
                )
            if strategy_version.generation_run_id is not None:
                version_generation = self.db.get(
                    StrategyGenerationRun,
                    strategy_version.generation_run_id,
                )
                if (
                    version_generation is None
                    or version_generation.execution_scope_id != job.execution_scope_id
                ):
                    self._block_linkage(
                        "strategy version generation lineage is missing or cross-scope"
                    )

        strategy_score = self._linked_row(
            StrategyScore,
            normalized.get("strategy_score_id"),
            "strategy_score_id",
        )
        if strategy_score is not None:
            if strategy_score.backtest_result_id is None:
                self._block_linkage("strategy score has no provable backtest scope")
            if strategy is not None and strategy_score.strategy_id != strategy.id:
                self._block_linkage("strategy score does not belong to linked strategy")
            if (
                strategy_version is not None
                and strategy_score.strategy_version_id != strategy_version.id
            ):
                self._block_linkage(
                    "strategy score does not belong to linked strategy version"
                )
            if (
                backtest_result is not None
                and strategy_score.backtest_result_id != backtest_result.id
            ):
                self._block_linkage(
                    "strategy score does not belong to linked backtest result"
                )
            if strategy_score.backtest_result_id is not None:
                score_result = self.db.get(
                    BacktestResult,
                    strategy_score.backtest_result_id,
                )
                score_run = (
                    None
                    if score_result is None
                    else self.db.get(BacktestRun, score_result.backtest_run_id)
                )
                if (
                    score_result is None
                    or score_run is None
                    or score_run.execution_scope_id != job.execution_scope_id
                ):
                    self._block_linkage(
                        "strategy score backtest lineage is missing or cross-scope"
                    )
        return normalized

    def _linked_row(self, model, row_id: Optional[int], column: str):
        if row_id is None:
            return None
        if not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= 0:
            self._block_linkage(f"{column} must be a positive integer")
        row = self.db.get(model, row_id)
        if row is None:
            self._block_linkage(f"{column} references a missing row")
        return row

    def _block_linkage(self, reason: str) -> None:
        self.db.rollback()
        raise ResearchJobLinkageBlocked(f"BLOCKED completion linkage: {reason}")

    def _release_control(self, job_id: int, lease_token: str) -> None:
        self.db.execute(
            update(ResearchWorkerControl)
            .where(
                ResearchWorkerControl.id == 1,
                ResearchWorkerControl.active_job_id == job_id,
                ResearchWorkerControl.active_lease_token == lease_token,
            )
            .values(active_job_id=None, active_lease_token=None)
        )


_LINK_COLUMNS = {
    "strategy_generation_run_id",
    "strategy_id",
    "strategy_version_id",
    "backtest_run_id",
    "backtest_task_id",
    "backtest_result_id",
    "strategy_score_id",
}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
