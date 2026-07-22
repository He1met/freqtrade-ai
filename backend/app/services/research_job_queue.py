from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.research_job import ResearchJob
from app.repositories.research_jobs import ResearchJobRepository
from app.schemas.deepseek_backtest_loop import DeepSeekBacktestLoopRequest
from app.schemas.dry_run_status import redact_dry_run_status_payload, redact_secret_text
from app.schemas.research_job import ResearchJobRead, ResearchWorkerControlRead


DEEPSEEK_BACKTEST_OPERATION = "strategy_generation.deepseek_backtest_loop"


class ResearchJobConflict(ValueError):
    """An idempotency key was reused with a different request payload."""


class UnsafeResearchJobPayload(ValueError):
    """A request contains a secret-shaped value that must not be persisted."""


class ResearchJobQueueService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.jobs = ResearchJobRepository(db)

    def enqueue_deepseek_backtest(
        self,
        payload: DeepSeekBacktestLoopRequest,
        *,
        idempotency_key: str,
    ) -> ResearchJob:
        request_payload = payload.model_dump(mode="json")
        redacted = redact_dry_run_status_payload(request_payload)
        if redacted != request_payload:
            raise UnsafeResearchJobPayload(
                "Research job payload contains a secret-shaped value; use ENV references only."
            )
        request_hash = _stable_digest(request_payload)
        idempotency_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        job, _created = self.jobs.create_or_get(
            job_type="deepseek_backtest",
            operation=DEEPSEEK_BACKTEST_OPERATION,
            idempotency_key_digest=idempotency_digest,
            request_hash=request_hash,
            request_payload=request_payload,
        )
        if job.request_hash != request_hash:
            raise ResearchJobConflict(
                "Idempotency-Key was already used with a different research job payload."
            )
        return job

    def get(self, job_id: int) -> Optional[ResearchJobRead]:
        job = self.jobs.get(job_id)
        return None if job is None else ResearchJobRead.model_validate(job)

    def list(self, limit: int = 100) -> list[ResearchJobRead]:
        return [ResearchJobRead.model_validate(job) for job in self.jobs.list(limit=limit)]

    def cancel(self, job_id: int, reason: str) -> Optional[ResearchJobRead]:
        job = self.jobs.cancel(job_id, redact_secret_text(reason))
        return None if job is None else ResearchJobRead.model_validate(job)

    def pause(self, reason: str) -> ResearchWorkerControlRead:
        self.jobs.set_paused(True, redact_secret_text(reason))
        return self.worker_status()

    def resume(self) -> ResearchWorkerControlRead:
        self.jobs.set_paused(False, None)
        return self.worker_status()

    def worker_status(self) -> ResearchWorkerControlRead:
        control = self.jobs.get_control()
        counts = self.jobs.status_counts()
        return ResearchWorkerControlRead(
            paused=control.paused,
            reason=control.reason,
            updated_at=control.updated_at,
            active_job_id=control.active_job_id,
            pending_jobs=counts.get("PENDING", 0),
            running_jobs=counts.get("RUNNING", 0),
            stale_jobs=counts.get("STALE", 0),
        )


def _stable_digest(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
