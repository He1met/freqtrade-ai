from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.strategy_research_contract import official_research_policy
from app.models import ResearchJob
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.repositories.research_jobs import ResearchJobRepository
from app.services.qualified_demo_deployment_queue import (
    QualifiedDemoDeploymentQueueBlocked,
    validate_formal_research_ownership,
)


CANDIDATE_VALIDATION_OPERATION = "strategy_research.candidate_validation_queue_v1"
CANDIDATE_VALIDATION_JOB_TYPE = "formal_candidate_validation"
CANDIDATE_VALIDATION_QUEUE_CONTRACT = "formal-candidate-validation-queue-v1"


class StrategyCandidateValidationQueueBlocked(RuntimeError):
    """Generated candidate evidence is unsafe or cannot be queued idempotently."""


@dataclass(frozen=True)
class GeneratedCandidate:
    candidate_key: str
    source_path: str
    source_code_digest: str
    pair: str
    timeframe: str
    blueprint_evidence: dict[str, Any]
    validation_request: dict[str, Any] | None = None
    market_data_evidence: dict[str, Any] | None = None


class StrategyCandidateValidationQueueService:
    """Persist generated candidates before any Freqtrade validation is run."""

    def __init__(self, db: Session, *, canonical_root: Path) -> None:
        self.db = db
        self.canonical_root = canonical_root.resolve()

    def enqueue_generated(
        self,
        *,
        run_id: str,
        repository_commit: str,
        candidates: Iterable[GeneratedCandidate],
        ownership_evidence: dict[str, Any] | None,
        now: datetime | None = None,
        ownership_guard: Callable[[], bool] | None = None,
    ) -> tuple[ResearchJob, ...]:
        current = now or datetime.now(timezone.utc)
        try:
            validate_formal_research_ownership(
                ownership_evidence,
                canonical_root=self.canonical_root,
                now=current,
            )
        except QualifiedDemoDeploymentQueueBlocked as exc:
            raise StrategyCandidateValidationQueueBlocked(str(exc)) from exc
        if not run_id.strip() or len(repository_commit) != 40:
            raise StrategyCandidateValidationQueueBlocked(
                "GENERATED_CANDIDATE_IDENTITY_INVALID"
            )
        rows = tuple(candidates)
        if not rows or len({row.candidate_key for row in rows}) != len(rows):
            raise StrategyCandidateValidationQueueBlocked(
                "GENERATED_CANDIDATE_SET_EMPTY_OR_DUPLICATED"
            )
        if self.db.get_bind().dialect.name == "postgresql":
            if self.db.execute(text("SELECT current_user = 'freqtrade'")).scalar_one():
                raise StrategyCandidateValidationQueueBlocked(
                    "RUNTIME_ROLE_CANNOT_ENQUEUE_VALIDATION"
                )
            lock_key = int(hashlib.sha256(run_id.encode()).hexdigest()[:15], 16)
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
            )
        ensure_execution_scope_catalog(self.db)
        queued: list[ResearchJob] = []
        for candidate in sorted(rows, key=lambda item: item.candidate_key):
            self._validate_candidate(candidate)
            payload = {
                "queue_contract_version": CANDIDATE_VALIDATION_QUEUE_CONTRACT,
                "research_run_id": run_id,
                "repository_commit": repository_commit,
                "candidate_key": candidate.candidate_key,
                "source_path": candidate.source_path,
                "source_code_digest": candidate.source_code_digest,
                "pair": candidate.pair,
                "timeframe": candidate.timeframe,
                "blueprint_evidence": candidate.blueprint_evidence,
                "validation_request": candidate.validation_request,
                "market_data_evidence": candidate.market_data_evidence,
                "quality_contract": official_research_policy(),
                "execution_scope_id": "LOCAL_DRY_RUN",
                "execution_target_id": "OKX_DEMO",
                "allow_real_funds": False,
                "real_orders": False,
                "validation_only": True,
            }
            request_hash = _digest(payload)
            idempotency_digest = _digest(
                {
                    "operation": CANDIDATE_VALIDATION_OPERATION,
                    "research_run_id": run_id,
                    "candidate_key": candidate.candidate_key,
                    "source_code_digest": candidate.source_code_digest,
                }
            )
            existing = self.db.scalar(
                select(ResearchJob).where(
                    ResearchJob.execution_scope_id == "LOCAL_DRY_RUN",
                    ResearchJob.operation == CANDIDATE_VALIDATION_OPERATION,
                    ResearchJob.idempotency_key_digest == idempotency_digest,
                )
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise StrategyCandidateValidationQueueBlocked(
                        "VALIDATION_QUEUE_IDEMPOTENCY_CONFLICT"
                    )
                queued.append(existing)
                continue
            job = ResearchJob(
                execution_scope_id="LOCAL_DRY_RUN",
                job_type=CANDIDATE_VALIDATION_JOB_TYPE,
                operation=CANDIDATE_VALIDATION_OPERATION,
                idempotency_key_digest=idempotency_digest,
                request_hash=request_hash,
                request_payload=payload,
                status="PENDING",
                stage="GENERATED_QUEUED",
                max_attempts=2,
                evidence_snapshot={
                    "lifecycle_status": "GENERATED_QUEUED",
                    "reason_code": "AWAITING_VALIDATION_LEASE",
                    "backtest_started": False,
                    "qualified": False,
                    "deployment_eligible": False,
                    "allow_real_funds": False,
                    "real_orders": False,
                },
            )
            self.db.add(job)
            queued.append(job)
        if ownership_guard is not None and ownership_guard() is not True:
            self.db.rollback()
            raise StrategyCandidateValidationQueueBlocked(
                "RESEARCH_OWNERSHIP_LOST_BEFORE_QUEUE_COMMIT"
            )
        self.db.commit()
        for job in queued:
            self.db.refresh(job)
        return tuple(queued)

    def claim_next(
        self, *, owner: str, lease_seconds: int, now: datetime | None = None
    ) -> ResearchJob | None:
        return ResearchJobRepository(self.db).claim_next(
            owner=owner,
            lease_seconds=lease_seconds,
            now=now,
            operations={CANDIDATE_VALIDATION_OPERATION},
        )

    def retry_stale(self, job_id: int) -> ResearchJob | None:
        job = ResearchJobRepository(self.db).get(job_id)
        if job is None or job.operation != CANDIDATE_VALIDATION_OPERATION:
            raise StrategyCandidateValidationQueueBlocked(
                "VALIDATION_RETRY_JOB_MISSING_OR_WRONG_OPERATION"
            )
        return ResearchJobRepository(self.db).prepare_stale_recovery(
            job_id, recovery_stage="VALIDATION_RETRY"
        )

    def _validate_candidate(self, candidate: GeneratedCandidate) -> None:
        blueprint = candidate.blueprint_evidence
        source_path = (self.canonical_root / candidate.source_path).resolve()
        if (
            not candidate.candidate_key.strip()
            or len(candidate.source_code_digest) != 64
            or self.canonical_root not in source_path.parents
            or source_path.is_symlink()
            or not source_path.is_file()
            or hashlib.sha256(source_path.read_bytes()).hexdigest()
            != candidate.source_code_digest
            or candidate.pair
            not in {"BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"}
            or candidate.timeframe not in {"5m", "15m"}
            or not isinstance(blueprint, dict)
            or blueprint.get("exact_render_match") is not True
            or blueprint.get("source_code_digest") != candidate.source_code_digest
            or blueprint.get("rendered_code_digest") != candidate.source_code_digest
            or not isinstance(blueprint.get("blueprint"), dict)
            or blueprint["blueprint"].get("timeframe") != candidate.timeframe
        ):
            raise StrategyCandidateValidationQueueBlocked(
                "GENERATED_CANDIDATE_EVIDENCE_INVALID"
            )
        if candidate.validation_request is not None:
            from app.schemas.deepseek_backtest_loop import DeepSeekBacktestLoopRequest

            try:
                request = DeepSeekBacktestLoopRequest.model_validate(
                    candidate.validation_request
                )
            except ValueError as exc:
                raise StrategyCandidateValidationQueueBlocked(
                    "GENERATED_CANDIDATE_VALIDATION_REQUEST_INVALID"
                ) from exc
            profile = request.backtest_profile
            if (
                request.allow_real_call is not False
                or request.persisted_blueprint != blueprint.get("blueprint")
                or profile.get("pair") != candidate.pair
                or profile.get("timeframe") != candidate.timeframe
                or len(request.validation_windows) != 4
            ):
                raise StrategyCandidateValidationQueueBlocked(
                    "GENERATED_CANDIDATE_VALIDATION_REQUEST_INVALID"
                )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
