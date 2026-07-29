from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    ApprovedExecution,
    BacktestResult,
    BacktestRun,
    BacktestTask,
    ExchangeFill,
    ExchangeOrder,
    FullChainRun,
    FullChainSignalSnapshot,
    FullChainStageRun,
    ReconciliationRun,
    ResearchJob,
    ResearchJobAttempt,
    RiskDecision,
    Strategy,
    StrategyCandidateApproval,
    StrategyGenerationRun,
    StrategyScore,
    StrategyVersion,
    TradeIntent,
)
from app.models.execution_lineage import LOCAL_DRY_RUN_SCOPE_ID, OKX_DEMO_TARGET_ID
from app.models.full_chain import FULL_CHAIN_STAGES
from app.repositories.research_jobs import ResearchJobRepository
from app.schemas.dry_run_status import redact_dry_run_status_payload, redact_secret_text
from app.services.strategy_promotion import (
    StrategyPromotionBlocked,
    promotion_candidate_digest,
)


TERMINAL_CHAIN_STATUSES = {
    "SUCCESS",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
    "STALE",
}

OKX_DEMO_AUTO_APPROVAL_ACTOR = "system:okx-demo-auto-promotion"
OKX_DEMO_AUTO_APPROVAL_TTL = timedelta(minutes=5)

AUTHORITATIVE_RECONCILIATION_ID_KEYS = {
    "reconciliation_run",
    "exchange_events",
    "order_snapshots",
    "fill_snapshots",
    "position_snapshots",
    "account_snapshots",
    "repaired_exchange_orders",
    "recovery_batches",
    "recovery_grants",
    "reconciliation_state",
}

AUTHORITATIVE_RECONCILIATION_NONEMPTY_KEYS = {
    "reconciliation_run",
    "exchange_events",
    "order_snapshots",
    "fill_snapshots",
    "account_snapshots",
    "recovery_batches",
    "reconciliation_state",
}

STAGE_REQUIRED_IDS = {
    "GENERATION": {
        "strategy_generation_run_id",
        "strategy_id",
        "strategy_version_id",
    },
    "BACKTEST": {
        "backtest_run_id",
        "backtest_task_id",
        "backtest_result_id",
    },
    "SCORING": {"strategy_score_id"},
    "CANDIDATE_APPROVAL": {"candidate_approval_id"},
    "SIGNAL": {"signal_snapshot_id"},
    "RISK": {"trade_intent_id", "risk_decision_id", "approved_execution_id"},
    "EXECUTION": {"exchange_order_id"},
    "FILL": {"exchange_fill_id"},
    "RECONCILIATION": {"reconciliation_run_id"},
}


class FullChainBlocked(ValueError):
    """A chain checkpoint is incomplete, out of order, stale, or inconsistent."""


class FullChainConflict(ValueError):
    """A durable idempotency identity was reused with different input."""


class FullChainRepository:
    """Lease-fenced persistence for the single ResearchJob-to-OKX Demo chain."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def open_for_claimed_job(
        self,
        research_job_id: int,
        lease_token: str,
        *,
        now: Optional[datetime] = None,
    ) -> FullChainRun:
        current_time = now or datetime.now(timezone.utc)
        job, attempt = self._require_owned_job(research_job_id, lease_token, current_time)
        existing = self.db.scalars(
            select(FullChainRun).where(FullChainRun.research_job_id == research_job_id)
        ).first()
        if existing is not None:
            if existing.research_job_attempt_id != attempt.id:
                raise FullChainBlocked(
                    "full chain belongs to an earlier ResearchJob attempt; explicit "
                    "recovery is required before another external call"
                )
            return existing
        chain = FullChainRun(
            research_job_id=job.id,
            research_job_attempt_id=attempt.id,
            research_scope_id=LOCAL_DRY_RUN_SCOPE_ID,
            execution_target_id=OKX_DEMO_TARGET_ID,
            status="RUNNING",
            current_stage=FULL_CHAIN_STAGES[0],
            started_at=current_time,
        )
        self.db.add(chain)
        self.db.commit()
        self.db.refresh(chain)
        return chain

    def prepare_stage(
        self,
        chain_id: int,
        stage: str,
        lease_token: str,
        *,
        idempotency_key: str,
        input_snapshot: dict,
        now: Optional[datetime] = None,
    ) -> FullChainStageRun:
        current_time = now or datetime.now(timezone.utc)
        chain = self._require_active_chain(chain_id)
        self._require_owned_job(chain.research_job_id, lease_token, current_time)
        self._require_next_stage(chain, stage)
        if not idempotency_key.strip():
            raise FullChainBlocked("stage idempotency key is required")
        safe_input = _require_safe_snapshot(input_snapshot, "stage input")
        input_digest = _stable_digest(safe_input)
        idempotency_digest = _sha256(idempotency_key)
        existing = self.db.scalars(
            select(FullChainStageRun).where(
                FullChainStageRun.full_chain_run_id == chain.id,
                FullChainStageRun.stage == stage,
            )
        ).first()
        if existing is not None:
            if (
                existing.idempotency_key_digest != idempotency_digest
                or existing.input_digest != input_digest
            ):
                raise FullChainConflict(
                    "stage was already prepared with different idempotency or input"
                )
            if existing.status == "PREPARED":
                raise FullChainBlocked(
                    "stage has an unresolved PREPARED checkpoint; refusing to repeat "
                    "the external call without explicit recovery"
                )
            return existing
        checkpoint = FullChainStageRun(
            full_chain_run_id=chain.id,
            stage=stage,
            status="PREPARED",
            idempotency_key_digest=idempotency_digest,
            input_digest=input_digest,
            input_snapshot=safe_input,
            prepared_at=current_time,
        )
        chain.current_stage = stage
        self.db.add(checkpoint)
        self.db.commit()
        self.db.refresh(checkpoint)
        return checkpoint

    def complete_stage(
        self,
        chain_id: int,
        stage: str,
        lease_token: str,
        *,
        database_ids: dict[str, int],
        output_snapshot: dict,
        now: Optional[datetime] = None,
    ) -> FullChainStageRun:
        current_time = now or datetime.now(timezone.utc)
        chain = self._require_active_chain(chain_id)
        self._require_owned_job(chain.research_job_id, lease_token, current_time)
        checkpoint = self._prepared_stage(chain.id, stage)
        if checkpoint.status == "SUCCESS":
            if (
                checkpoint.database_ids != database_ids
                or checkpoint.output_snapshot != output_snapshot
            ):
                raise FullChainConflict("successful stage evidence cannot be rewritten")
            return checkpoint
        if checkpoint.status != "PREPARED":
            raise FullChainBlocked("only a PREPARED stage can be completed")
        safe_output = _require_safe_snapshot(output_snapshot, "stage output")
        normalized_ids = _require_database_ids(stage, database_ids)
        self._validate_database_lineage(chain, stage, normalized_ids)
        for key, value in normalized_ids.items():
            if hasattr(chain, key):
                setattr(chain, key, value)
        checkpoint.status = "SUCCESS"
        checkpoint.database_ids = normalized_ids
        checkpoint.output_snapshot = safe_output
        checkpoint.completed_at = current_time
        next_stage = _next_stage(stage)
        chain.current_stage = next_stage or stage
        if stage == "CANDIDATE_APPROVAL":
            chain.status = "APPROVED"
        elif stage in {"SIGNAL", "RISK", "EXECUTION", "FILL"}:
            chain.status = "EXECUTING"
        elif stage == "RECONCILIATION":
            chain.status = "RECONCILING"
        self.db.commit()
        self.db.refresh(checkpoint)
        return checkpoint

    def fail_stage(
        self,
        chain_id: int,
        stage: str,
        lease_token: str,
        *,
        status: str,
        error_code: str,
        error_message: str,
        now: Optional[datetime] = None,
    ) -> FullChainStageRun:
        if status not in {"FAILED", "BLOCKED", "CANCELLED", "STALE"}:
            raise ValueError("invalid terminal stage status")
        current_time = now or datetime.now(timezone.utc)
        chain = self._require_active_chain(chain_id)
        self._require_owned_job(chain.research_job_id, lease_token, current_time)
        checkpoint = self._prepared_stage(chain.id, stage)
        if checkpoint.status != "PREPARED":
            raise FullChainBlocked("only a PREPARED stage can be failed")
        checkpoint.status = status
        checkpoint.error_code = error_code[:80]
        checkpoint.error_message = redact_secret_text(error_message)[:2000]
        checkpoint.completed_at = current_time
        chain.status = status
        chain.terminal_reason = checkpoint.error_message
        chain.completed_at = current_time
        self.db.commit()
        self.db.refresh(checkpoint)
        return checkpoint

    def create_candidate_approval(
        self,
        chain_id: int,
        lease_token: str,
        *,
        requested_by: str,
        expires_at: datetime,
        now: Optional[datetime] = None,
    ) -> StrategyCandidateApproval:
        current_time = now or datetime.now(timezone.utc)
        chain = self._require_active_chain(chain_id)
        self._require_owned_job(chain.research_job_id, lease_token, current_time)
        checkpoint = self._prepared_stage(chain.id, "CANDIDATE_APPROVAL")
        if checkpoint.status != "PREPARED":
            raise FullChainBlocked("candidate approval stage is not PREPARED")
        if not requested_by.strip():
            raise FullChainBlocked("candidate approval requester is required")
        if expires_at <= current_time:
            raise FullChainBlocked("candidate approval must expire in the future")
        required = (
            chain.strategy_version_id,
            chain.backtest_result_id,
            chain.strategy_score_id,
        )
        if any(value is None for value in required):
            raise FullChainBlocked("candidate approval is missing scored research lineage")
        result = self.db.get(BacktestResult, chain.backtest_result_id)
        score = self.db.get(StrategyScore, chain.strategy_score_id)
        strategy_version = self.db.get(StrategyVersion, chain.strategy_version_id)
        if result is None or score is None or strategy_version is None:
            raise FullChainBlocked("candidate approval research records are missing")
        try:
            promotion, candidate_digest = promotion_candidate_digest(
                result, score, strategy_version
            )
        except StrategyPromotionBlocked as exc:
            raise FullChainBlocked("candidate promotion is blocked: {}".format(exc))
        existing = self.db.scalars(
            select(StrategyCandidateApproval).where(
                StrategyCandidateApproval.full_chain_run_id == chain.id
            )
        ).first()
        if existing is not None:
            if existing.candidate_digest != candidate_digest:
                raise FullChainConflict("candidate approval lineage cannot be rewritten")
            return existing
        approval = StrategyCandidateApproval(
            full_chain_run_id=chain.id,
            execution_target_id=OKX_DEMO_TARGET_ID,
            strategy_version_id=chain.strategy_version_id,
            backtest_result_id=chain.backtest_result_id,
            strategy_score_id=chain.strategy_score_id,
            candidate_digest=candidate_digest,
            promotion_policy_version=promotion["policy"]["policy_version"],
            promotion_evidence=promotion,
            status="PENDING",
            requested_by=requested_by[:160],
            requested_at=current_time,
            expires_at=expires_at,
        )
        chain.status = "AWAITING_APPROVAL"
        checkpoint.output_snapshot = {"promotion": promotion}
        self.db.add(approval)
        self.db.flush()
        chain.candidate_approval_id = approval.id
        waiting_evidence = {
            **chain_evidence_snapshot(chain),
            "status": "AWAITING_APPROVAL",
            "acceptance_ready": False,
            "candidate_approval_id": approval.id,
        }
        waiting_job = ResearchJobRepository(
            self.db
        ).wait_for_candidate_approval(
            chain.research_job_id,
            lease_token,
            evidence_snapshot=waiting_evidence,
            now=current_time,
            commit=False,
        )
        if waiting_job is None:
            self.db.rollback()
            raise FullChainBlocked(
                "ResearchJob could not release its lease for candidate approval"
            )
        self.db.commit()
        self.db.refresh(approval)
        return approval

    def decide_candidate(
        self,
        approval_id: int,
        *,
        decision: str,
        decided_by: str,
        reason: str,
        decision_evidence: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> StrategyCandidateApproval:
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValueError("candidate decision must be APPROVED or REJECTED")
        current_time = now or datetime.now(timezone.utc)
        approval = self.db.get(StrategyCandidateApproval, approval_id)
        if approval is None:
            raise FullChainBlocked("candidate approval does not exist")
        if approval.status != "PENDING":
            raise FullChainConflict("candidate approval decision is immutable")
        chain = self.db.get(FullChainRun, approval.full_chain_run_id)
        if chain is None or chain.status != "AWAITING_APPROVAL":
            raise FullChainBlocked("candidate approval chain is not awaiting a decision")
        checkpoint = self._prepared_stage(chain.id, "CANDIDATE_APPROVAL")
        if not decided_by.strip():
            raise FullChainBlocked("candidate approval decision actor is required")
        safe_reason = redact_secret_text(reason)[:2000]
        if not safe_reason.strip():
            raise FullChainBlocked("candidate approval decision reason is required")
        safe_decision_evidence = (
            _require_safe_snapshot(decision_evidence, "candidate decision")
            if decision_evidence is not None
            else {}
        )
        if _as_utc(approval.expires_at) <= _as_utc(current_time):
            approval.status = "EXPIRED"
            approval.decided_at = current_time
            approval.decision_reason = "Candidate approval expired before decision."
            checkpoint.status = "BLOCKED"
            checkpoint.error_code = "CANDIDATE_APPROVAL_EXPIRED"
            checkpoint.error_message = approval.decision_reason
            checkpoint.completed_at = current_time
            chain.status = "BLOCKED"
            chain.terminal_reason = approval.decision_reason
            chain.completed_at = current_time
            ResearchJobRepository(self.db).block_waiting_candidate_approval(
                chain.research_job_id,
                reason=approval.decision_reason,
                evidence_snapshot={
                    **chain_evidence_snapshot(chain),
                    "status": "BLOCKED",
                    "acceptance_ready": False,
                    "failed_reason": approval.decision_reason,
                },
                now=current_time,
                commit=False,
            )
            self.db.commit()
            raise FullChainBlocked("candidate approval expired")
        approval.status = decision
        approval.decided_by = decided_by[:160]
        approval.decision_reason = safe_reason
        approval.decided_at = current_time
        if decision == "REJECTED":
            checkpoint.status = "BLOCKED"
            checkpoint.error_code = "CANDIDATE_REJECTED"
            checkpoint.error_message = approval.decision_reason
            checkpoint.completed_at = current_time
            chain.status = "BLOCKED"
            chain.terminal_reason = approval.decision_reason
            chain.completed_at = current_time
            blocked = ResearchJobRepository(
                self.db
            ).block_waiting_candidate_approval(
                chain.research_job_id,
                reason=approval.decision_reason,
                evidence_snapshot={
                    **chain_evidence_snapshot(chain),
                    "status": "BLOCKED",
                    "acceptance_ready": False,
                    "failed_reason": approval.decision_reason,
                },
                now=current_time,
                commit=False,
            )
            if blocked is None:
                self.db.rollback()
                raise FullChainBlocked(
                    "ResearchJob candidate rejection transition failed"
                )
        else:
            checkpoint.status = "SUCCESS"
            checkpoint.database_ids = {"candidate_approval_id": approval.id}
            checkpoint.output_snapshot = {
                **checkpoint.output_snapshot,
                **safe_decision_evidence,
                "status": "APPROVED",
            }
            checkpoint.completed_at = current_time
            chain.status = "APPROVED"
            chain.current_stage = "SIGNAL"
            resumed = ResearchJobRepository(
                self.db
            ).resume_after_candidate_approval(
                chain.research_job_id,
                evidence_snapshot={
                    **chain_evidence_snapshot(chain),
                    "status": "PENDING",
                    "acceptance_ready": False,
                    "candidate_approval_id": approval.id,
                },
                commit=False,
            )
            if resumed is None:
                self.db.rollback()
                raise FullChainBlocked(
                    "ResearchJob candidate approval resume transition failed"
                )
        self.db.commit()
        self.db.refresh(approval)
        return approval

    def auto_approve_candidate(
        self,
        chain_id: int,
        lease_token: str,
        *,
        now: Optional[datetime] = None,
    ) -> StrategyCandidateApproval:
        """Approve one exact eligible candidate under the locked Demo policy.

        This is deliberately narrower than the general decision API: the target,
        actor, policy evidence and lifetime are fixed by code, and all existing
        promotion checks run before the decision can be persisted.
        """

        current_time = now or datetime.now(timezone.utc)
        chain = self._require_active_chain(chain_id)
        automation = get_settings().demo_automation_policy
        if (
            automation.enabled is not True
            or automation.automatic_candidate_approval is not True
            or automation.execution_target_id != OKX_DEMO_TARGET_ID
            or chain.execution_target_id != OKX_DEMO_TARGET_ID
            or automation.allow_live_trading is not False
            or automation.allow_real_funds is not False
        ):
            raise FullChainBlocked("automatic candidate approval is OKX_DEMO only")
        approval = self.create_candidate_approval(
            chain.id,
            lease_token,
            requested_by=OKX_DEMO_AUTO_APPROVAL_ACTOR,
            expires_at=current_time + OKX_DEMO_AUTO_APPROVAL_TTL,
            now=current_time,
        )
        promotion = approval.promotion_evidence
        policy = promotion.get("policy") if isinstance(promotion, dict) else None
        policy_version = (
            policy.get("policy_version")
            if isinstance(policy, dict)
            else None
        )
        if policy_version != approval.promotion_policy_version:
            raise FullChainBlocked("automatic approval policy evidence is invalid")
        return self.decide_candidate(
            approval.id,
            decision="APPROVED",
            decided_by=OKX_DEMO_AUTO_APPROVAL_ACTOR,
            reason=(
                "Automatically approved for OKX_DEMO after deterministic "
                "promotion gates passed."
            ),
            decision_evidence={
                "approval_mode": "AUTOMATIC",
                "decision_actor": OKX_DEMO_AUTO_APPROVAL_ACTOR,
                "automation_policy_schema_version": automation.schema_version,
                "candidate_digest": approval.candidate_digest,
                "promotion_policy_version": approval.promotion_policy_version,
                "hard_gates": {
                    "validated_strategy_version": True,
                    "positive_net_profit": True,
                    "drawdown_limit": True,
                    "minimum_trade_count": True,
                    "out_of_sample": True,
                    "walk_forward_market_states": True,
                    "net_of_costs": True,
                },
                "manual_confirmation_required": False,
                "execution_target_id": OKX_DEMO_TARGET_ID,
                "allow_real_funds": False,
            },
            now=current_time,
        )

    def revoke_candidate_approval(
        self,
        approval_id: int,
        *,
        revoked_by: str,
        reason: str,
        now: Optional[datetime] = None,
    ) -> StrategyCandidateApproval:
        """Revoke an approved candidate before any later chain stage can proceed."""

        current_time = now or datetime.now(timezone.utc)
        approval = self.db.get(StrategyCandidateApproval, approval_id)
        if approval is None:
            raise FullChainBlocked("candidate approval does not exist")
        if approval.status != "APPROVED":
            raise FullChainConflict("only an approved candidate can be revoked")
        if not revoked_by.strip():
            raise FullChainBlocked("candidate revocation actor is required")
        safe_reason = redact_secret_text(reason)[:2000]
        if not safe_reason.strip():
            raise FullChainBlocked("candidate revocation reason is required")
        chain = self.db.get(FullChainRun, approval.full_chain_run_id)
        if chain is None or chain.status in TERMINAL_CHAIN_STATUSES:
            raise FullChainBlocked("candidate approval chain is not active")

        approval.status = "REVOKED"
        approval.decided_by = revoked_by[:160]
        approval.decision_reason = safe_reason
        approval.decided_at = current_time
        chain.status = "BLOCKED"
        chain.terminal_reason = "Candidate approval revoked: {}".format(safe_reason)
        chain.completed_at = current_time
        self.db.commit()
        self.db.refresh(approval)
        return approval

    def record_signal(
        self,
        chain_id: int,
        lease_token: str,
        *,
        instrument_id: str,
        source_type: str,
        source_database_ids: dict,
        signal_snapshot: dict,
        observed_at: datetime,
        expires_at: datetime,
    ) -> FullChainSignalSnapshot:
        chain = self._require_active_chain(chain_id)
        self._require_owned_job(chain.research_job_id, lease_token, observed_at)
        checkpoint = self._prepared_stage(chain.id, "SIGNAL")
        if checkpoint.status != "PREPARED":
            raise FullChainBlocked("signal stage is not PREPARED")
        approval = (
            self.db.get(StrategyCandidateApproval, chain.candidate_approval_id)
            if chain.candidate_approval_id
            else None
        )
        if (
            approval is None
            or approval.status != "APPROVED"
            or _as_utc(approval.expires_at) <= _as_utc(observed_at)
        ):
            raise FullChainBlocked("a current approved candidate is required for a signal")
        self._require_current_candidate_approval(approval, chain, observed_at)
        if source_type not in {"database", "api_aggregate"}:
            raise FullChainBlocked("signal source must be database or api_aggregate")
        if (
            not source_database_ids
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in source_database_ids.values()
            )
        ):
            raise FullChainBlocked("signal source database IDs are required")
        if not instrument_id.strip():
            raise FullChainBlocked("signal instrument is required")
        if observed_at >= expires_at:
            raise FullChainBlocked("signal snapshot is stale or has an invalid lifetime")
        safe_snapshot = _require_safe_snapshot(signal_snapshot, "signal")
        digest = _stable_digest(
            {
                "candidate_digest": approval.candidate_digest,
                "instrument_id": instrument_id,
                "source_type": source_type,
                "source_database_ids": source_database_ids,
                "signal_snapshot": safe_snapshot,
                "observed_at": observed_at,
                "expires_at": expires_at,
            }
        )
        existing = self.db.scalars(
            select(FullChainSignalSnapshot).where(
                FullChainSignalSnapshot.full_chain_run_id == chain.id
            )
        ).first()
        if existing is not None:
            if existing.signal_digest != digest:
                raise FullChainConflict("signal snapshot is immutable")
            return existing
        signal = FullChainSignalSnapshot(
            full_chain_run_id=chain.id,
            candidate_approval_id=approval.id,
            execution_target_id=OKX_DEMO_TARGET_ID,
            instrument_id=instrument_id,
            signal_digest=digest,
            source_type=source_type,
            core_data=True,
            source_database_ids=source_database_ids,
            signal_snapshot=safe_snapshot,
            observed_at=observed_at,
            expires_at=expires_at,
        )
        self.db.add(signal)
        self.db.flush()
        chain.signal_snapshot_id = signal.id
        self.db.commit()
        self.db.refresh(signal)
        return signal

    def _require_current_candidate_approval(
        self,
        approval: StrategyCandidateApproval,
        chain: FullChainRun,
        now: datetime,
    ) -> None:
        """Fail closed if post-approval strategy or market evidence drifted."""

        result = self.db.get(BacktestResult, approval.backtest_result_id)
        score = self.db.get(StrategyScore, approval.strategy_score_id)
        version = self.db.get(StrategyVersion, approval.strategy_version_id)
        try:
            if result is None or score is None or version is None:
                raise StrategyPromotionBlocked("candidate research records are missing")
            evidence, digest = promotion_candidate_digest(result, score, version)
            if approval.promotion_policy_version != evidence["policy"]["policy_version"]:
                raise StrategyPromotionBlocked("promotion policy version changed")
            if approval.promotion_evidence != evidence or approval.candidate_digest != digest:
                raise StrategyPromotionBlocked("strategy or market evidence changed after approval")
        except StrategyPromotionBlocked as exc:
            reason = "Automatic promotion invalidation: {}".format(exc)
            approval.status = "REVOKED"
            approval.decided_by = "system:promotion-revalidation"
            approval.decision_reason = reason
            approval.decided_at = now
            checkpoint = self._prepared_stage(chain.id, "CANDIDATE_APPROVAL")
            checkpoint.status = "BLOCKED"
            checkpoint.error_code = "CANDIDATE_APPROVAL_STALE"
            checkpoint.error_message = reason
            checkpoint.completed_at = now
            checkpoint.output_snapshot = {
                **checkpoint.output_snapshot,
                "status": "REVOKED",
                "revalidation_reason": reason,
            }
            chain.status = "BLOCKED"
            chain.terminal_reason = reason
            chain.completed_at = now
            self.db.commit()
            raise FullChainBlocked(reason)

    def finalize_reconciliation(
        self,
        chain_id: int,
        lease_token: str,
        *,
        reconciliation_run_id: int,
        now: Optional[datetime] = None,
    ) -> FullChainRun:
        current_time = now or datetime.now(timezone.utc)
        chain = self._require_active_chain(chain_id)
        self._require_owned_job(chain.research_job_id, lease_token, current_time)
        checkpoint = self._prepared_stage(chain.id, "RECONCILIATION")
        if checkpoint.status != "PREPARED":
            raise FullChainBlocked("reconciliation stage is not PREPARED")
        required_links = {
            key: getattr(chain, key)
            for key in (
                "strategy_generation_run_id",
                "strategy_id",
                "strategy_version_id",
                "backtest_run_id",
                "backtest_task_id",
                "backtest_result_id",
                "strategy_score_id",
                "candidate_approval_id",
                "signal_snapshot_id",
                "trade_intent_id",
                "risk_decision_id",
                "approved_execution_id",
                "exchange_order_id",
                "exchange_fill_id",
            )
        }
        missing = sorted(key for key, value in required_links.items() if not value)
        if missing:
            raise FullChainBlocked(
                "reconciliation cannot complete with missing database IDs: "
                + ", ".join(missing)
            )
        self._validate_database_lineage(
            chain,
            "RECONCILIATION",
            {"reconciliation_run_id": reconciliation_run_id},
        )
        reconciliation = self.db.get(ReconciliationRun, reconciliation_run_id)
        authoritative_ids = require_authoritative_reconciliation(reconciliation)
        checkpoint.status = "SUCCESS"
        checkpoint.database_ids = {"reconciliation_run_id": reconciliation_run_id}
        checkpoint.output_snapshot = {
            "status": reconciliation.status,
            "acceptance_ready": True,
            "source_type": reconciliation.source_type,
            "core_data": reconciliation.core_data,
            "authoritative_database_ids": authoritative_ids,
        }
        checkpoint.completed_at = current_time
        chain.reconciliation_run_id = reconciliation_run_id
        chain.status = "SUCCESS"
        chain.current_stage = "RECONCILIATION"
        chain.completed_at = current_time
        self.db.commit()
        self.db.refresh(chain)
        return chain

    def _require_active_chain(self, chain_id: int) -> FullChainRun:
        chain = self.db.get(FullChainRun, chain_id)
        if chain is None:
            raise FullChainBlocked("full chain run does not exist")
        if chain.status in TERMINAL_CHAIN_STATUSES:
            raise FullChainBlocked("full chain run is terminal")
        if (
            chain.research_scope_id != LOCAL_DRY_RUN_SCOPE_ID
            or chain.execution_target_id != OKX_DEMO_TARGET_ID
        ):
            raise FullChainBlocked("full chain scope or execution target is invalid")
        return chain

    def _require_owned_job(
        self,
        job_id: int,
        lease_token: str,
        now: datetime,
    ) -> tuple[ResearchJob, ResearchJobAttempt]:
        job = self.db.get(ResearchJob, job_id)
        if (
            job is None
            or job.execution_scope_id != LOCAL_DRY_RUN_SCOPE_ID
            or job.job_type != "deepseek_backtest"
            or job.operation != "strategy_generation.deepseek_backtest_loop"
            or job.status != "RUNNING"
            or job.cancel_requested
            or job.lease_token != lease_token
            or job.lease_expires_at is None
            or _as_utc(job.lease_expires_at) <= _as_utc(now)
        ):
            raise FullChainBlocked("ResearchJob lease is absent, stale, cancelled, or not owned")
        attempt = self.db.scalars(
            select(ResearchJobAttempt)
            .where(
                ResearchJobAttempt.research_job_id == job.id,
                ResearchJobAttempt.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                ResearchJobAttempt.attempt_number == job.attempt_count,
                ResearchJobAttempt.status == "RUNNING",
            )
            .limit(1)
        ).first()
        if attempt is None:
            raise FullChainBlocked("active ResearchJobAttempt is missing")
        return job, attempt

    def _require_next_stage(self, chain: FullChainRun, stage: str) -> None:
        if stage not in FULL_CHAIN_STAGES:
            raise ValueError("unknown full-chain stage")
        successful = {
            row.stage
            for row in self.db.scalars(
                select(FullChainStageRun).where(
                    FullChainStageRun.full_chain_run_id == chain.id,
                    FullChainStageRun.status == "SUCCESS",
                )
            ).all()
        }
        stage_index = FULL_CHAIN_STAGES.index(stage)
        missing = [
            predecessor
            for predecessor in FULL_CHAIN_STAGES[:stage_index]
            if predecessor not in successful
        ]
        if missing:
            raise FullChainBlocked(
                "stage cannot start before successful predecessors: " + ", ".join(missing)
            )

    def _prepared_stage(self, chain_id: int, stage: str) -> FullChainStageRun:
        checkpoint = self.db.scalars(
            select(FullChainStageRun).where(
                FullChainStageRun.full_chain_run_id == chain_id,
                FullChainStageRun.stage == stage,
            )
        ).first()
        if checkpoint is None:
            raise FullChainBlocked("stage must be PREPARED before any result is recorded")
        return checkpoint

    def _validate_database_lineage(
        self,
        chain: FullChainRun,
        stage: str,
        ids: dict[str, int],
    ) -> None:
        entities: dict[str, tuple[type, int]] = {
            "strategy_generation_run_id": (StrategyGenerationRun, ids.get("strategy_generation_run_id", 0)),
            "strategy_id": (Strategy, ids.get("strategy_id", 0)),
            "strategy_version_id": (StrategyVersion, ids.get("strategy_version_id", 0)),
            "backtest_run_id": (BacktestRun, ids.get("backtest_run_id", 0)),
            "backtest_task_id": (BacktestTask, ids.get("backtest_task_id", 0)),
            "backtest_result_id": (BacktestResult, ids.get("backtest_result_id", 0)),
            "strategy_score_id": (StrategyScore, ids.get("strategy_score_id", 0)),
            "candidate_approval_id": (StrategyCandidateApproval, ids.get("candidate_approval_id", 0)),
            "signal_snapshot_id": (FullChainSignalSnapshot, ids.get("signal_snapshot_id", 0)),
            "trade_intent_id": (TradeIntent, ids.get("trade_intent_id", 0)),
            "risk_decision_id": (RiskDecision, ids.get("risk_decision_id", 0)),
            "approved_execution_id": (ApprovedExecution, ids.get("approved_execution_id", 0)),
            "exchange_order_id": (ExchangeOrder, ids.get("exchange_order_id", 0)),
            "exchange_fill_id": (ExchangeFill, ids.get("exchange_fill_id", 0)),
            "reconciliation_run_id": (ReconciliationRun, ids.get("reconciliation_run_id", 0)),
        }
        loaded = {}
        for key in ids:
            model, entity_id = entities[key]
            entity = self.db.get(model, entity_id)
            if entity is None:
                raise FullChainBlocked("{} does not identify a persisted row".format(key))
            loaded[key] = entity
        if stage == "GENERATION":
            generation = loaded["strategy_generation_run_id"]
            strategy = loaded["strategy_id"]
            version = loaded["strategy_version_id"]
            if (
                generation.execution_scope_id != LOCAL_DRY_RUN_SCOPE_ID
                or version.strategy_id != strategy.id
                or version.generation_run_id != generation.id
            ):
                raise FullChainBlocked("generation database lineage is inconsistent")
        elif stage == "BACKTEST":
            run = loaded["backtest_run_id"]
            task = loaded["backtest_task_id"]
            result = loaded["backtest_result_id"]
            if (
                run.execution_scope_id != LOCAL_DRY_RUN_SCOPE_ID
                or run.strategy_version_id != chain.strategy_version_id
                or task.backtest_run_id != run.id
                or result.backtest_run_id != run.id
                or result.backtest_task_id != task.id
            ):
                raise FullChainBlocked("backtest database lineage is inconsistent")
        elif stage == "SCORING":
            score = loaded["strategy_score_id"]
            if (
                score.strategy_version_id != chain.strategy_version_id
                or score.backtest_result_id != chain.backtest_result_id
            ):
                raise FullChainBlocked("score database lineage is inconsistent")
        elif stage == "CANDIDATE_APPROVAL":
            approval = loaded["candidate_approval_id"]
            if approval.full_chain_run_id != chain.id or approval.status != "APPROVED":
                raise FullChainBlocked("candidate is not approved for this chain")
        elif stage == "SIGNAL":
            signal = loaded["signal_snapshot_id"]
            if (
                signal.full_chain_run_id != chain.id
                or signal.candidate_approval_id != chain.candidate_approval_id
            ):
                raise FullChainBlocked("signal is not bound to the approved candidate")
        elif stage == "RISK":
            intent = loaded["trade_intent_id"]
            decision = loaded["risk_decision_id"]
            approval = loaded["approved_execution_id"]
            if (
                intent.execution_target_id != OKX_DEMO_TARGET_ID
                or intent.strategy_version_id != chain.strategy_version_id
                or intent.backtest_result_id != chain.backtest_result_id
                or intent.strategy_score_id != chain.strategy_score_id
                or decision.trade_intent_id != intent.id
                or decision.decision != "APPROVED"
                or approval.trade_intent_id != intent.id
            ):
                raise FullChainBlocked("OKX Demo risk approval lineage is inconsistent")
        elif stage == "EXECUTION":
            order = loaded["exchange_order_id"]
            if (
                order.execution_target_id != OKX_DEMO_TARGET_ID
                or order.trade_intent_id != chain.trade_intent_id
                or not order.exchange_order_id
            ):
                raise FullChainBlocked("exchange order acknowledgement is incomplete")
        elif stage == "FILL":
            fill = loaded["exchange_fill_id"]
            if (
                fill.execution_target_id != OKX_DEMO_TARGET_ID
                or fill.exchange_order_row_id != chain.exchange_order_id
            ):
                raise FullChainBlocked("exchange fill lineage is inconsistent")
        elif stage == "RECONCILIATION":
            reconciliation = loaded["reconciliation_run_id"]
            if reconciliation.execution_target_id != OKX_DEMO_TARGET_ID:
                raise FullChainBlocked("reconciliation target is not OKX Demo")


def _require_database_ids(stage: str, database_ids: dict[str, int]) -> dict[str, int]:
    required = STAGE_REQUIRED_IDS[stage]
    if set(database_ids) != required:
        missing = sorted(required - set(database_ids))
        extra = sorted(set(database_ids) - required)
        raise FullChainBlocked(
            "stage database IDs mismatch; missing={} extra={}".format(missing, extra)
        )
    normalized = {}
    for key, value in database_ids.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise FullChainBlocked("{} must be a positive database ID".format(key))
        normalized[key] = value
    return normalized


def chain_evidence_snapshot(chain: FullChainRun) -> dict:
    return {
        "full_chain_run_id": chain.id,
        "research_scope_id": chain.research_scope_id,
        "execution_target_id": chain.execution_target_id,
        "current_stage": chain.current_stage,
        "database_ids": {
            key: value
            for key in (
                "strategy_generation_run_id",
                "strategy_id",
                "strategy_version_id",
                "backtest_run_id",
                "backtest_task_id",
                "backtest_result_id",
                "strategy_score_id",
                "candidate_approval_id",
                "signal_snapshot_id",
                "trade_intent_id",
                "risk_decision_id",
                "approved_execution_id",
                "exchange_order_id",
                "exchange_fill_id",
                "reconciliation_run_id",
            )
            if (value := getattr(chain, key)) is not None
        },
    }


def require_authoritative_reconciliation(
    reconciliation: Optional[ReconciliationRun],
) -> dict:
    """Accept only #448's finalized, core, fully identified reconciliation row."""

    if reconciliation is None:
        raise FullChainBlocked("authoritative reconciliation row does not exist")
    if (
        reconciliation.execution_target_id != OKX_DEMO_TARGET_ID
        or reconciliation.status not in {"RECONCILED", "RECOVERED"}
        or reconciliation.source_type != "api_aggregate"
        or reconciliation.core_data is not True
        or reconciliation.artifact_status != "READY"
        or not reconciliation.artifact_path
        or not _is_sha256(reconciliation.artifact_sha256)
        or reconciliation.authoritative_observed_at is None
        or reconciliation.completed_at is None
    ):
        raise FullChainBlocked(
            "authoritative reconciliation is incomplete, non-core, or not finalized"
        )
    database_ids = reconciliation.database_ids
    if not isinstance(database_ids, dict):
        raise FullChainBlocked("authoritative reconciliation database IDs are missing")
    missing_keys = sorted(AUTHORITATIVE_RECONCILIATION_ID_KEYS - set(database_ids))
    if missing_keys:
        raise FullChainBlocked(
            "authoritative reconciliation database IDs are missing: "
            + ", ".join(missing_keys)
        )
    for key in AUTHORITATIVE_RECONCILIATION_ID_KEYS:
        values = database_ids[key]
        if (
            not isinstance(values, list)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in values
            )
        ):
            raise FullChainBlocked(
                "authoritative reconciliation database IDs are invalid: " + key
            )
        if key in AUTHORITATIVE_RECONCILIATION_NONEMPTY_KEYS and not values:
            raise FullChainBlocked(
                "authoritative reconciliation database IDs are empty: " + key
            )
    if database_ids["reconciliation_run"] != [reconciliation.id]:
        raise FullChainBlocked(
            "authoritative reconciliation database ID does not match its row"
        )
    if len(database_ids["reconciliation_state"]) != 1:
        raise FullChainBlocked(
            "authoritative reconciliation must identify one canonical state row"
        )
    summary = reconciliation.summary_snapshot
    if (
        not isinstance(summary, dict)
        or summary.get("execution_target") != OKX_DEMO_TARGET_ID
        or summary.get("status") != reconciliation.status
        or summary.get("source_type") != "api_aggregate"
        or summary.get("core_data") is not True
        or summary.get("database_ids") != database_ids
        or not summary.get("authoritative_observed_at")
    ):
        raise FullChainBlocked(
            "authoritative reconciliation summary does not match the database row"
        )
    return database_ids


def _require_safe_snapshot(snapshot: dict, label: str) -> dict:
    if not isinstance(snapshot, dict) or not snapshot:
        raise FullChainBlocked("{} snapshot must be non-empty".format(label))
    redacted = redact_dry_run_status_payload(snapshot)
    if redacted != snapshot:
        raise FullChainBlocked("{} snapshot contains secret-shaped data".format(label))
    return snapshot


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return _sha256(payload)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Optional[str]) -> bool:
    if value is None or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _next_stage(stage: str) -> Optional[str]:
    index = FULL_CHAIN_STAGES.index(stage)
    return FULL_CHAIN_STAGES[index + 1] if index + 1 < len(FULL_CHAIN_STAGES) else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
