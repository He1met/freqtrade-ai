from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.strategy_research_contract import matches_official_research_policy
from app.core.strategy_research_diversity import RESEARCH_CANDIDATE_COUNT
from app.models import (
    MarketDataQualityReceipt,
    ResearchJob,
    StrategyResearchAttemptEvent,
    StrategyResearchBatch,
    StrategyResearchCandidate,
)


QUALIFIED_DEMO_DEPLOYMENT_OPERATION = (
    "strategy_research.qualified_demo_deployment_queue_v1"
)
QUALIFIED_DEMO_DEPLOYMENT_JOB_TYPE = "qualified_demo_deployment"
QUEUE_CONTRACT_VERSION = "qualified-demo-deployment-queue-v1"
OWNERSHIP_SCHEMA = "freqtrade-ai-formal-research-ownership-v1"
TERMINAL_RECEIPT_MAX_AGE = timedelta(minutes=10)
MARKET_DATA_RECEIPT_MAX_AGE = timedelta(hours=2)


class QualifiedDemoDeploymentQueueBlocked(RuntimeError):
    """Formal research evidence cannot safely enter the Demo deployment queue."""


@dataclass(frozen=True)
class QualifiedDemoDeploymentQueueResult:
    status: str
    reason_code: str
    queued_job_ids: tuple[int, ...]
    qualified_candidate_ids: tuple[int, ...]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_formal_research_ownership(
    evidence: dict[str, Any] | None,
    *,
    canonical_root: Path,
    now: datetime,
) -> None:
    if not isinstance(evidence, dict):
        raise QualifiedDemoDeploymentQueueBlocked(
            "OWNERSHIP_EVIDENCE_MISSING_OR_STALE"
        )
    confirmed_at = _parse_datetime(evidence.get("confirmed_at"))
    expires_at = _parse_datetime(evidence.get("expires_at"))
    if (
        evidence.get("schema_version") != OWNERSHIP_SCHEMA
        or evidence.get("scope") != "FORMAL_STRATEGY_RESEARCH"
        or evidence.get("canonical_root") != str(canonical_root.resolve())
        or not isinstance(evidence.get("owner_task_id"), str)
        or not evidence["owner_task_id"].strip()
        or confirmed_at is None
        or expires_at is None
        or confirmed_at > now
        or expires_at <= now
        or expires_at - confirmed_at > timedelta(minutes=30)
    ):
        raise QualifiedDemoDeploymentQueueBlocked(
            "OWNERSHIP_EVIDENCE_MISSING_OR_STALE"
        )


class QualifiedDemoDeploymentQueueService:
    """Enqueue fresh QUALIFIED evidence for owner-mediated canonical continuation.

    Queueing grants no deployment, signal, risk, or order capability.  A separate
    canonical owner must revalidate the Blueprint and publish a StrategyDeployment
    before the unique runtime can observe natural closed-candle signals.
    """

    def __init__(self, db: Session, *, canonical_root: Path) -> None:
        self.db = db
        self.canonical_root = canonical_root.resolve()

    def enqueue_completed_batch(
        self,
        *,
        run_id: str,
        ownership_evidence: dict[str, Any] | None,
        now: datetime | None = None,
    ) -> QualifiedDemoDeploymentQueueResult:
        current = _as_utc(now or datetime.now(timezone.utc))
        batch = self.db.scalar(
            select(StrategyResearchBatch).where(StrategyResearchBatch.run_id == run_id)
        )
        if batch is None:
            raise QualifiedDemoDeploymentQueueBlocked("RESEARCH_BATCH_MISSING")

        if self.db.get_bind().dialect.name == "postgresql":
            if self.db.execute(text("SELECT current_user = 'freqtrade'")).scalar_one():
                raise QualifiedDemoDeploymentQueueBlocked(
                    "RUNTIME_ROLE_CANNOT_ENQUEUE_DEPLOYMENT"
                )
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": 543100000 + batch.id},
            )

        terminal = self.db.scalar(
            select(StrategyResearchAttemptEvent)
            .where(
                StrategyResearchAttemptEvent.batch_id == batch.id,
                StrategyResearchAttemptEvent.phase == "TERMINAL",
                StrategyResearchAttemptEvent.outcome == "COMPLETED",
            )
            .order_by(
                StrategyResearchAttemptEvent.created_at.desc(),
                StrategyResearchAttemptEvent.id.desc(),
            )
        )
        validate_formal_research_ownership(
            ownership_evidence, canonical_root=self.canonical_root, now=current
        )
        self._require_complete_batch(batch, terminal, current)

        qualified = tuple(
            sorted(
                (row for row in batch.candidates if row.status == "QUALIFIED"),
                key=lambda row: row.id,
            )
        )
        if not qualified:
            return QualifiedDemoDeploymentQueueResult(
                status="NO_ACTION",
                reason_code="NOT_QUEUED_NO_QUALIFIED",
                queued_job_ids=(),
                qualified_candidate_ids=(),
            )

        queue_rows: list[ResearchJob] = []
        for candidate in qualified:
            evidence = self._candidate_evidence(candidate, terminal, current)
            request_payload = {
                "queue_contract_version": QUEUE_CONTRACT_VERSION,
                "research_batch_id": batch.id,
                "research_run_id": batch.run_id,
                "research_candidate_id": candidate.id,
                "research_report_digest": batch.report_digest,
                "source_code_digest": candidate.code_digest,
                "terminal_attempt_event_id": terminal.id,
                "terminal_event_digest": terminal.event_digest,
                "market_data_quality_receipt_id": evidence["quality_receipt_id"],
                "market_data_quality_evidence_digest": evidence[
                    "quality_evidence_digest"
                ],
                "deployment_target": evidence["deployment_target"],
                "execution_scope_id": "LOCAL_DRY_RUN",
                "execution_target_id": "OKX_DEMO",
                "allow_real_funds": False,
                "real_orders": False,
                "signal_source": "NATURAL_CLOSED_CANDLE_ONLY",
                "no_action_is_terminal_success": True,
            }
            request_hash = _digest(request_payload)
            idempotency_digest = _digest(
                {
                    "operation": QUALIFIED_DEMO_DEPLOYMENT_OPERATION,
                    "candidate_id": candidate.id,
                    "report_digest": batch.report_digest,
                    "terminal_event_digest": terminal.event_digest,
                    "source_code_digest": candidate.code_digest,
                }
            )
            existing = self.db.scalar(
                select(ResearchJob).where(
                    ResearchJob.execution_scope_id == "LOCAL_DRY_RUN",
                    ResearchJob.operation == QUALIFIED_DEMO_DEPLOYMENT_OPERATION,
                    ResearchJob.idempotency_key_digest == idempotency_digest,
                )
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise QualifiedDemoDeploymentQueueBlocked(
                        "DEPLOYMENT_QUEUE_IDEMPOTENCY_CONFLICT"
                    )
                queue_rows.append(existing)
                continue
            row = ResearchJob(
                execution_scope_id="LOCAL_DRY_RUN",
                job_type=QUALIFIED_DEMO_DEPLOYMENT_JOB_TYPE,
                operation=QUALIFIED_DEMO_DEPLOYMENT_OPERATION,
                idempotency_key_digest=idempotency_digest,
                request_hash=request_hash,
                request_payload=request_payload,
                status="PENDING",
                stage="QUALIFIED_PENDING_CANONICAL_VALIDATION",
                max_attempts=1,
                evidence_snapshot={
                    "lifecycle_status": "QUEUED_FOR_CANONICAL_VALIDATION",
                    "reason_code": "QUALIFIED_DEMO_DEPLOYMENT_QUEUED",
                    "execution_target_id": "OKX_DEMO",
                    "allow_real_funds": False,
                    "real_orders": False,
                    "natural_no_action_allowed": True,
                },
            )
            self.db.add(row)
            queue_rows.append(row)

        self.db.commit()
        for row in queue_rows:
            self.db.refresh(row)
        return QualifiedDemoDeploymentQueueResult(
            status="QUEUED",
            reason_code="QUALIFIED_DEMO_DEPLOYMENT_QUEUED",
            queued_job_ids=tuple(row.id for row in queue_rows),
            qualified_candidate_ids=tuple(row.id for row in qualified),
        )

    def _require_complete_batch(
        self,
        batch: StrategyResearchBatch,
        terminal: StrategyResearchAttemptEvent | None,
        now: datetime,
    ) -> None:
        safety = batch.safety_snapshot or {}
        candidates = tuple(batch.candidates)
        terminal_created_at = (
            _as_utc(terminal.created_at) if terminal is not None else None
        )
        if (
            batch.status != "VALIDATED"
            or batch.report_schema_version
            != "freqtrade-ai-strategy-candidate-research-v2"
            or not matches_official_research_policy(batch.selection_policy)
            or safety.get("execution_target") != "OKX_DEMO"
            or safety.get("allow_real_funds") is not False
            or safety.get("real_orders") is not False
            or batch.requested_count != RESEARCH_CANDIDATE_COUNT
            or batch.generated_count != RESEARCH_CANDIDATE_COUNT
            or batch.persisted_count != RESEARCH_CANDIDATE_COUNT
            or len(candidates) != RESEARCH_CANDIDATE_COUNT
            or batch.qualified_count
            != sum(row.status == "QUALIFIED" for row in candidates)
            or batch.rejected_count
            != sum(
                row.status in {"REJECTED", "VALIDATION_FAILED"}
                for row in candidates
            )
            or any(
                row.status not in {"QUALIFIED", "REJECTED", "VALIDATION_FAILED"}
                for row in candidates
            )
            or terminal is None
            or terminal_created_at is None
            or terminal_created_at > now + timedelta(minutes=1)
            or now - terminal_created_at > TERMINAL_RECEIPT_MAX_AGE
            or terminal.requested_count != batch.requested_count
            or terminal.generated_count != batch.generated_count
            or terminal.persisted_count != batch.persisted_count
            or terminal.qualified_count != batch.qualified_count
            or terminal.rejected_count != batch.rejected_count
            or terminal.validated_count
            != terminal.qualified_count + terminal.rejected_count
        ):
            raise QualifiedDemoDeploymentQueueBlocked(
                "RESEARCH_EVIDENCE_MISSING_OR_STALE"
            )

    def _candidate_evidence(
        self,
        candidate: StrategyResearchCandidate,
        terminal: StrategyResearchAttemptEvent,
        now: datetime,
    ) -> dict[str, Any]:
        snapshot = candidate.evidence_snapshot or {}
        target = snapshot.get("deployment_target")
        blueprint = snapshot.get("canonical_blueprint_v2")
        if (
            candidate.validation_passed is not True
            or candidate.deployable_candidate is not True
            or candidate.rejection_reasons
            or not isinstance(target, dict)
            or target.get("pair") != candidate.pair
            or target.get("timeframe") != candidate.timeframe
            or not isinstance(blueprint, dict)
            or blueprint.get("exact_render_match") is not True
            or blueprint.get("source_code_digest") != candidate.code_digest
            or blueprint.get("rendered_code_digest") != candidate.code_digest
            or not isinstance(blueprint.get("blueprint"), dict)
        ):
            raise QualifiedDemoDeploymentQueueBlocked(
                "QUALIFIED_CANDIDATE_EVIDENCE_INCOMPLETE"
            )
        bindings = (terminal.evidence_snapshot or {}).get("market_data_bindings")
        matches = [
            item
            for item in bindings or []
            if isinstance(item, dict)
            and item.get("pair") == target.get("pair")
            and item.get("timeframe") == target.get("timeframe")
            and isinstance(item.get("receipt_id"), int)
        ]
        if len(matches) != 1:
            raise QualifiedDemoDeploymentQueueBlocked(
                "MARKET_DATA_EVIDENCE_MISSING_OR_STALE"
            )
        quality = self.db.get(MarketDataQualityReceipt, matches[0]["receipt_id"])
        if (
            quality is None
            or quality.status != "PASSED"
            or quality.pair != target.get("pair")
            or quality.timeframe != target.get("timeframe")
            or quality.file_sha256 != matches[0].get("sha256")
            or _as_utc(quality.inspected_at) > now + timedelta(minutes=1)
            or now - _as_utc(quality.inspected_at) > MARKET_DATA_RECEIPT_MAX_AGE
        ):
            raise QualifiedDemoDeploymentQueueBlocked(
                "MARKET_DATA_EVIDENCE_MISSING_OR_STALE"
            )
        return {
            "deployment_target": {
                "pair": target["pair"],
                "timeframe": target["timeframe"],
            },
            "quality_receipt_id": quality.id,
            "quality_evidence_digest": quality.evidence_digest,
        }
