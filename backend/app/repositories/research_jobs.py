from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.research_job import ResearchJob, ResearchWorkerControl


TERMINAL_JOB_STATUSES = {
    "SUCCESS",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
    "STALE",
}


class ResearchJobRepository:
    """Database-fenced job queue with one global local execution lease."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, job_id: int) -> Optional[ResearchJob]:
        return self.db.get(ResearchJob, job_id)

    def list(self, limit: int = 100) -> list[ResearchJob]:
        statement = (
            select(ResearchJob)
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
        job = ResearchJob(
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
        statement = select(ResearchJob.status, func.count(ResearchJob.id)).group_by(ResearchJob.status)
        return {status: count for status, count in self.db.execute(statement).all()}

    def claim_next(
        self,
        *,
        owner: str,
        lease_seconds: int,
        now: Optional[datetime] = None,
    ) -> Optional[ResearchJob]:
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
                    ResearchJob.status == "PENDING",
                    ResearchJob.attempt_count < ResearchJob.max_attempts,
                )
                .order_by(ResearchJob.created_at.asc(), ResearchJob.id.asc())
                .limit(1)
            )
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

            job.status = "RUNNING"
            job.stage = "GENERATION"
            job.lease_owner = owner
            job.lease_token = lease_token
            job.heartbeat_at = current_time
            job.lease_expires_at = current_time + timedelta(seconds=lease_seconds)
            job.attempt_count += 1
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

    def heartbeat(
        self,
        job_id: int,
        lease_token: str,
        *,
        lease_seconds: int,
        now: Optional[datetime] = None,
    ) -> bool:
        current_time = now or datetime.now(timezone.utc)
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job_id,
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
        current_time = now or datetime.now(timezone.utc)
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job_id,
                ResearchJob.status == "RUNNING",
                ResearchJob.lease_token == lease_token,
                ResearchJob.provider_attempted_at.is_(None),
                ResearchJob.cancel_requested.is_(False),
            )
            .values(provider_attempted_at=current_time, stage="PROVIDER_CALL")
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
    ) -> Optional[ResearchJob]:
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError(f"invalid terminal job status: {status}")
        current_time = now or datetime.now(timezone.utc)
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
        values.update({key: value for key, value in links.items() if key in _LINK_COLUMNS})
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job_id,
                ResearchJob.status == "RUNNING",
                ResearchJob.lease_token == lease_token,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        self._release_control(job_id, lease_token)
        self.db.commit()
        return self.get(job_id)

    def cancel(self, job_id: int, reason: str) -> Optional[ResearchJob]:
        job = self.get(job_id)
        if job is None:
            return None
        if job.status == "PENDING":
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

    def cancel_at_checkpoint(
        self,
        job_id: int,
        lease_token: str,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[ResearchJob]:
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
        stale_reason = (
            "Provider outcome is unknown after lease expiry; automatic retry is forbidden."
            if job.provider_attempted_at is not None and job.provider_completed_at is None
            else "Worker lease expired before a safe terminal checkpoint."
        )
        result = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job.id,
                ResearchJob.status == "RUNNING",
                ResearchJob.lease_token == lease_token,
                ResearchJob.lease_expires_at <= current_time,
            )
            .values(
                status="STALE",
                stage="LEASE_EXPIRED",
                error_message=stale_reason,
                evidence_snapshot={
                    **job.evidence_snapshot,
                    "status": "STALE",
                    "acceptance_ready": False,
                    "failed_reason": stale_reason,
                },
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
        self._release_control(job.id, lease_token)
        self.db.commit()
        return self.get(job.id)

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
