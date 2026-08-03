from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Optional
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.full_chain import (
    FullChainRun,
    FullChainSignalSnapshot,
    StrategyCandidateApproval,
)
from app.models.strategy import Strategy, StrategyVersion
from app.models.strategy_deployment import SignalEvaluation, StrategyDeployment


TERMINAL_EVALUATION_STATUSES = {
    "NO_ACTION",
    "ACTIONABLE",
    "BLOCKED",
    "FAILED",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INSTRUMENT_RE = re.compile(r"^[A-Z0-9]{2,20}-[A-Z0-9]{2,20}-SWAP$")
_TIMEFRAME_RE = re.compile(r"^[1-9][0-9]{0,3}[mhdw]$")


class StrategyDeploymentBlocked(ValueError):
    """A deployment or evaluation cannot safely advance."""


class StrategyDeploymentConflict(ValueError):
    """An idempotent identity was reused with different immutable binding."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_sha256(value: str, field: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise StrategyDeploymentBlocked(f"{field} must be a lowercase SHA-256 digest")


class StrategyDeploymentRepository:
    """Durable OKX Demo deployments and their single-consumer candle queue."""

    def __init__(self, db: Session):
        self.db = db

    def get_deployment(self, deployment_id: int) -> Optional[StrategyDeployment]:
        return self.db.get(StrategyDeployment, deployment_id)

    def get_evaluation(self, evaluation_id: int) -> Optional[SignalEvaluation]:
        return self.db.get(SignalEvaluation, evaluation_id)

    def require_active_lease(
        self,
        evaluation_id: int,
        *,
        lease_token: str,
        fencing_sequence: int,
        now: Optional[datetime] = None,
        for_update: bool = False,
    ) -> tuple[SignalEvaluation, StrategyDeployment]:
        """Return one currently fenced runtime evaluation without committing.

        The raw research-job lease is deliberately not accepted here.  Every
        execution-chain mutation must prove the SignalEvaluation token and
        fencing sequence issued to the sole OKX runtime consumer.
        """

        if not lease_token:
            raise StrategyDeploymentBlocked("evaluation lease token is required")
        current_time = _as_utc(now or datetime.now(timezone.utc))
        statement = (
            select(SignalEvaluation, StrategyDeployment)
            .join(
                StrategyDeployment,
                StrategyDeployment.id == SignalEvaluation.deployment_id,
            )
            .where(SignalEvaluation.id == evaluation_id)
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.db.execute(statement).first()
        if row is None:
            raise StrategyDeploymentBlocked("signal evaluation is missing")
        evaluation, deployment = row
        if (
            evaluation.execution_target_id != "OKX_DEMO"
            or evaluation.status != "LEASED"
            or evaluation.lease_token != lease_token
            or evaluation.fencing_sequence != fencing_sequence
            or evaluation.lease_expires_at is None
            or _as_utc(evaluation.lease_expires_at) <= current_time
        ):
            raise StrategyDeploymentBlocked(
                "signal evaluation lease is absent, stale, or fenced"
            )
        if (
            deployment.execution_target_id != "OKX_DEMO"
            or deployment.status != "ACTIVE"
            or evaluation.instrument_id != deployment.instrument_id
            or evaluation.timeframe != deployment.timeframe
        ):
            raise StrategyDeploymentBlocked(
                "signal evaluation deployment is disabled or inconsistent"
            )
        return evaluation, deployment

    def publish(
        self,
        *,
        candidate_approval_id: int,
        instrument_id: str,
        timeframe: str,
        deployment_policy_digest: str,
        risk_policy_digest: Optional[str] = None,
        commit: bool = True,
        now: Optional[datetime] = None,
    ) -> StrategyDeployment:
        now = now or datetime.now(timezone.utc)
        _require_sha256(deployment_policy_digest, "deployment_policy_digest")
        if risk_policy_digest is not None:
            _require_sha256(risk_policy_digest, "risk_policy_digest")
        if not _INSTRUMENT_RE.fullmatch(instrument_id):
            raise StrategyDeploymentBlocked("instrument_id must be an OKX SWAP instrument")
        if not _TIMEFRAME_RE.fullmatch(timeframe):
            raise StrategyDeploymentBlocked("timeframe is invalid")

        approval = self.db.get(StrategyCandidateApproval, candidate_approval_id)
        if approval is None:
            raise StrategyDeploymentBlocked("candidate approval is missing")
        if approval.status != "APPROVED":
            raise StrategyDeploymentBlocked("candidate approval is not APPROVED")
        if _as_utc(approval.expires_at) <= _as_utc(now):
            raise StrategyDeploymentBlocked("candidate approval is expired")
        _require_sha256(approval.candidate_digest, "candidate_digest")

        chain = self.db.get(FullChainRun, approval.full_chain_run_id)
        version = self.db.get(StrategyVersion, approval.strategy_version_id)
        if chain is None or version is None:
            raise StrategyDeploymentBlocked("candidate lineage is incomplete")
        strategy = self.db.get(Strategy, version.strategy_id)
        if strategy is None:
            raise StrategyDeploymentBlocked("candidate strategy is missing")
        if (
            chain.candidate_approval_id != approval.id
            or chain.strategy_version_id != approval.strategy_version_id
            or chain.strategy_id != strategy.id
            or version.strategy_id != strategy.id
        ):
            raise StrategyDeploymentBlocked("candidate strategy lineage is inconsistent")

        existing = self.db.scalar(
            select(StrategyDeployment).where(
                StrategyDeployment.candidate_approval_id == approval.id
            )
        )
        immutable_binding = (
            "OKX_DEMO",
            approval.id,
            strategy.id,
            version.id,
            approval.candidate_digest,
            approval.promotion_policy_version,
            deployment_policy_digest,
            risk_policy_digest,
            instrument_id,
            timeframe,
        )
        if existing is not None:
            existing_binding = (
                existing.execution_target_id,
                existing.candidate_approval_id,
                existing.strategy_id,
                existing.strategy_version_id,
                existing.candidate_digest,
                existing.promotion_policy_version,
                existing.deployment_policy_digest,
                existing.risk_policy_digest,
                existing.instrument_id,
                existing.timeframe,
            )
            if existing_binding != immutable_binding:
                raise StrategyDeploymentConflict(
                    "candidate approval already has a different deployment binding"
                )
            return existing
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(text("SELECT pg_advisory_xact_lock(543000003)"))
        active_slots = set(
            self.db.scalars(
                select(StrategyDeployment.active_slot).where(
                    StrategyDeployment.execution_target_id == "OKX_DEMO",
                    StrategyDeployment.status == "ACTIVE",
                )
            ).all()
        )
        active_slot = next(
            (slot for slot in range(1, 4) if slot not in active_slots),
            None,
        )
        if active_slot is None:
            raise StrategyDeploymentConflict(
                "OKX_DEMO already has three ACTIVE strategy deployments"
            )

        deployment = StrategyDeployment(
            execution_target_id="OKX_DEMO",
            candidate_approval_id=approval.id,
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            candidate_digest=approval.candidate_digest,
            promotion_policy_version=approval.promotion_policy_version,
            deployment_policy_digest=deployment_policy_digest,
            risk_policy_digest=risk_policy_digest,
            instrument_id=instrument_id,
            timeframe=timeframe,
            status="ACTIVE",
            active_slot=active_slot,
            evidence_snapshot={
                "schema_version": "1",
                "execution_target_id": "OKX_DEMO",
                "candidate_approval_id": approval.id,
                "full_chain_run_id": approval.full_chain_run_id,
                "strategy_id": strategy.id,
                "strategy_version_id": version.id,
                "candidate_digest": approval.candidate_digest,
                "promotion_policy_version": approval.promotion_policy_version,
                "deployment_policy_digest": deployment_policy_digest,
                "risk_policy_digest": risk_policy_digest,
                "manual_confirmation_required": False,
                "allow_real_funds": False,
                "active_slot": active_slot,
            },
        )
        self.db.add(deployment)
        if not commit:
            # The caller owns the surrounding transaction.  In particular, do
            # not rollback or reinterpret an integrity failure here because
            # that would silently discard the caller's selection evidence.
            self.db.flush()
            return deployment
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            replay = self.db.scalar(
                select(StrategyDeployment).where(
                    StrategyDeployment.candidate_approval_id == approval.id
                )
            )
            if replay is None:
                active_count = self.db.scalar(
                    select(func.count(StrategyDeployment.id)).where(
                        StrategyDeployment.execution_target_id == "OKX_DEMO",
                        StrategyDeployment.status == "ACTIVE",
                    )
                )
                if int(active_count or 0) >= 3:
                    raise StrategyDeploymentConflict(
                        "OKX_DEMO already has three ACTIVE strategy deployments"
                    )
                raise
            replay_binding = (
                replay.execution_target_id,
                replay.candidate_approval_id,
                replay.strategy_id,
                replay.strategy_version_id,
                replay.candidate_digest,
                replay.promotion_policy_version,
                replay.deployment_policy_digest,
                replay.risk_policy_digest,
                replay.instrument_id,
                replay.timeframe,
            )
            if replay_binding != immutable_binding:
                raise StrategyDeploymentConflict(
                    "concurrent deployment binding conflicts with this request"
                )
            return replay
        self.db.refresh(deployment)
        return deployment

    def enqueue_evaluation(
        self,
        deployment_id: int,
        *,
        closed_candle_at: datetime,
    ) -> SignalEvaluation:
        deployment = self.db.get(StrategyDeployment, deployment_id)
        if deployment is None:
            raise StrategyDeploymentBlocked("strategy deployment is missing")
        if deployment.status != "ACTIVE":
            raise StrategyDeploymentBlocked("strategy deployment is disabled")
        closed_candle_at = _as_utc(closed_candle_at)

        identity = (
            SignalEvaluation.deployment_id == deployment.id,
            SignalEvaluation.instrument_id == deployment.instrument_id,
            SignalEvaluation.timeframe == deployment.timeframe,
            SignalEvaluation.closed_candle_at == closed_candle_at,
        )
        existing = self.db.scalar(select(SignalEvaluation).where(*identity))
        if existing is not None:
            return existing

        evaluation = SignalEvaluation(
            deployment_id=deployment.id,
            execution_target_id="OKX_DEMO",
            instrument_id=deployment.instrument_id,
            timeframe=deployment.timeframe,
            closed_candle_at=closed_candle_at,
            status="PENDING",
        )
        self.db.add(evaluation)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            replay = self.db.scalar(select(SignalEvaluation).where(*identity))
            if replay is None:
                raise
            return replay
        self.db.refresh(evaluation)
        return evaluation

    def claim_next(
        self,
        *,
        owner: str,
        lease_seconds: int,
        now: Optional[datetime] = None,
    ) -> Optional[SignalEvaluation]:
        if not owner.strip():
            raise StrategyDeploymentBlocked("lease owner is required")
        if lease_seconds < 1:
            raise StrategyDeploymentBlocked("lease_seconds must be positive")
        now = now or datetime.now(timezone.utc)
        self._expire_stale(now)

        active_lease = self.db.scalar(
            select(SignalEvaluation.id)
            .where(SignalEvaluation.status == "LEASED")
            .limit(1)
        )
        if active_lease is not None:
            self.db.commit()
            return None

        candidate = self.db.scalar(
            select(SignalEvaluation)
            .join(
                StrategyDeployment,
                StrategyDeployment.id == SignalEvaluation.deployment_id,
            )
            .where(
                SignalEvaluation.status == "PENDING",
                StrategyDeployment.status == "ACTIVE",
                StrategyDeployment.execution_target_id == "OKX_DEMO",
            )
            .order_by(
                SignalEvaluation.closed_candle_at.asc(),
                SignalEvaluation.id.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if candidate is None:
            self.db.commit()
            return None

        lease_token = uuid4().hex
        result = self.db.execute(
            update(SignalEvaluation)
            .where(
                SignalEvaluation.id == candidate.id,
                SignalEvaluation.status == "PENDING",
            )
            .values(
                status="LEASED",
                lease_owner=owner,
                lease_token=lease_token,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
                fencing_sequence=SignalEvaluation.fencing_sequence + 1,
                completed_at=None,
                error_code=None,
                error_message=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        try:
            self.db.commit()
        except IntegrityError:
            # The partial unique index is the final cross-process single-consumer
            # guard when two transactions select different pending rows.
            self.db.rollback()
            return None
        self.db.expire_all()
        claimed = self.db.get(SignalEvaluation, candidate.id)
        if claimed is None or claimed.lease_token != lease_token:
            raise StrategyDeploymentBlocked("claimed evaluation disappeared")
        return claimed

    def heartbeat(
        self,
        evaluation_id: int,
        *,
        lease_token: str,
        fencing_sequence: int,
        lease_seconds: int,
        now: Optional[datetime] = None,
    ) -> bool:
        if lease_seconds < 1:
            raise StrategyDeploymentBlocked("lease_seconds must be positive")
        now = now or datetime.now(timezone.utc)
        result = self.db.execute(
            update(SignalEvaluation)
            .where(
                SignalEvaluation.id == evaluation_id,
                SignalEvaluation.status == "LEASED",
                SignalEvaluation.lease_token == lease_token,
                SignalEvaluation.fencing_sequence == fencing_sequence,
                SignalEvaluation.lease_expires_at > now,
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        self.db.expire_all()
        return result.rowcount == 1

    def checkpoint_leased(
        self,
        evaluation_id: int,
        *,
        lease_token: str,
        fencing_sequence: int,
        input_digest: str,
        result_snapshot: dict,
        now: Optional[datetime] = None,
    ) -> SignalEvaluation:
        """Persist immutable evaluator output without terminating its lease.

        The checkpoint survives lease expiry and is therefore replayable by a
        later fencing sequence.  A different digest or payload is a hard
        conflict rather than permission to repeat exchange reads/evaluation.
        """

        _require_sha256(input_digest, "input_digest")
        if (
            not isinstance(result_snapshot, dict)
            or result_snapshot.get("checkpoint_schema") != "SIGNAL_EVALUATION_V1"
        ):
            raise StrategyDeploymentBlocked(
                "leased evaluation checkpoint schema is invalid"
            )
        evaluation, _ = self.require_active_lease(
            evaluation_id,
            lease_token=lease_token,
            fencing_sequence=fencing_sequence,
            now=now,
            for_update=True,
        )
        existing_snapshot = evaluation.result_snapshot or {}
        if evaluation.input_digest is not None:
            if (
                evaluation.input_digest != input_digest
                or existing_snapshot != result_snapshot
            ):
                raise StrategyDeploymentConflict(
                    "leased evaluation checkpoint is immutable"
                )
            self.db.commit()
            return evaluation
        evaluation.input_digest = input_digest
        evaluation.result_snapshot = result_snapshot
        self.db.commit()
        self.db.refresh(evaluation)
        return evaluation

    def renew_checkpoint_execution_authority(
        self,
        evaluation_id: int,
        *,
        lease_token: str,
        fencing_sequence: int,
        full_chain_run_id: int,
        signal_snapshot_id: int,
        now: Optional[datetime] = None,
    ) -> datetime:
        """Renew only the execution approval bounded by a fresh fenced lease.

        Recovery never extends beyond the immutable trusted signal expiry.
        """

        current_time = _as_utc(now or datetime.now(timezone.utc))
        evaluation, deployment = self.require_active_lease(
            evaluation_id,
            lease_token=lease_token,
            fencing_sequence=fencing_sequence,
            now=current_time,
            for_update=True,
        )
        chain = self.db.get(FullChainRun, full_chain_run_id)
        signal = self.db.get(FullChainSignalSnapshot, signal_snapshot_id)
        approval = (
            self.db.get(StrategyCandidateApproval, chain.candidate_approval_id)
            if chain is not None and chain.candidate_approval_id is not None
            else None
        )
        if (
            chain is None
            or chain.run_kind != "EXECUTION"
            or chain.signal_evaluation_id != evaluation.id
            or chain.strategy_id != deployment.strategy_id
            or chain.strategy_version_id != deployment.strategy_version_id
            or chain.signal_snapshot_id != signal_snapshot_id
            or signal is None
            or signal.full_chain_run_id != chain.id
            or signal.execution_target_id != "OKX_DEMO"
            or signal.core_data is not True
            or _as_utc(signal.expires_at) <= current_time
            or approval is None
            or approval.full_chain_run_id != chain.id
            or approval.status != "APPROVED"
            or approval.candidate_digest != deployment.candidate_digest
        ):
            raise StrategyDeploymentBlocked(
                "checkpoint execution authority is stale or inconsistent"
            )
        renewed_until = min(
            _as_utc(evaluation.lease_expires_at),
            _as_utc(signal.expires_at),
        )
        if renewed_until <= current_time:
            raise StrategyDeploymentBlocked(
                "checkpoint execution authority cannot be renewed"
            )
        approval.expires_at = renewed_until
        self.db.commit()
        return renewed_until

    def block_checkpoint_execution_chain(
        self,
        evaluation_id: int,
        *,
        lease_token: str,
        fencing_sequence: int,
        full_chain_run_id: int,
        reason: str,
        now: Optional[datetime] = None,
    ) -> FullChainRun:
        """Terminally fence a partially advanced chain after orchestration failure."""

        current_time = _as_utc(now or datetime.now(timezone.utc))
        evaluation, _ = self.require_active_lease(
            evaluation_id,
            lease_token=lease_token,
            fencing_sequence=fencing_sequence,
            now=current_time,
            for_update=True,
        )
        chain = self.db.get(FullChainRun, full_chain_run_id)
        if (
            chain is None
            or chain.run_kind != "EXECUTION"
            or chain.signal_evaluation_id != evaluation.id
            or chain.execution_target_id != "OKX_DEMO"
        ):
            raise StrategyDeploymentBlocked(
                "checkpoint execution chain binding is inconsistent"
            )
        if chain.status not in {
            "SUCCESS",
            "FAILED",
            "BLOCKED",
            "CANCELLED",
            "STALE",
        }:
            chain.status = "BLOCKED"
            chain.terminal_reason = reason[:2000]
            chain.completed_at = current_time
            self.db.commit()
            self.db.refresh(chain)
        return chain

    def complete(
        self,
        evaluation_id: int,
        *,
        lease_token: str,
        fencing_sequence: int,
        status: str,
        input_digest: str,
        result_snapshot: dict,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[SignalEvaluation]:
        if status not in TERMINAL_EVALUATION_STATUSES:
            raise StrategyDeploymentBlocked("evaluation terminal status is invalid")
        _require_sha256(input_digest, "input_digest")
        if status in {"BLOCKED", "FAILED"} and not (error_code and error_message):
            raise StrategyDeploymentBlocked(
                "BLOCKED and FAILED evaluations require error evidence"
            )
        if status in {"NO_ACTION", "ACTIONABLE"} and (error_code or error_message):
            raise StrategyDeploymentBlocked(
                "successful evaluation outcomes cannot contain error evidence"
            )
        forbidden_downstream_keys = {
            "full_chain_run_id",
            "signal_snapshot_id",
            "trade_intent_id",
            "approved_execution_id",
            "exchange_order_id",
        }
        if status == "NO_ACTION" and forbidden_downstream_keys.intersection(
            result_snapshot
        ):
            raise StrategyDeploymentBlocked(
                "NO_ACTION cannot bind FullChain signal or execution records"
            )
        now = now or datetime.now(timezone.utc)
        result = self.db.execute(
            update(SignalEvaluation)
            .where(
                SignalEvaluation.id == evaluation_id,
                SignalEvaluation.status == "LEASED",
                SignalEvaluation.lease_token == lease_token,
                SignalEvaluation.fencing_sequence == fencing_sequence,
                SignalEvaluation.lease_expires_at > now,
            )
            .values(
                status=status,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
                input_digest=input_digest,
                result_snapshot=result_snapshot,
                error_code=error_code,
                error_message=error_message,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        if result.rowcount != 1:
            return None
        self.db.expire_all()
        return self.db.get(SignalEvaluation, evaluation_id)

    def disable(
        self,
        deployment_id: int,
        *,
        reason: str,
        now: Optional[datetime] = None,
    ) -> Optional[StrategyDeployment]:
        reason = reason.strip()
        if not reason:
            raise StrategyDeploymentBlocked("disable reason is required")
        now = now or datetime.now(timezone.utc)
        deployment = self.db.get(StrategyDeployment, deployment_id)
        if deployment is None:
            return None
        if deployment.status == "DISABLED":
            return deployment

        deployment.status = "DISABLED"
        deployment.active_slot = None
        deployment.disabled_reason = reason
        deployment.disabled_at = now
        self.db.execute(
            update(SignalEvaluation)
            .where(
                SignalEvaluation.deployment_id == deployment.id,
                SignalEvaluation.status.in_(("PENDING", "LEASED")),
            )
            .values(
                status="BLOCKED",
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
                result_snapshot={
                    "status": "BLOCKED",
                    "reason": "DEPLOYMENT_DISABLED",
                },
                error_code="DEPLOYMENT_DISABLED",
                error_message=reason,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        self.db.expire_all()
        return self.db.get(StrategyDeployment, deployment_id)

    def expire_stale(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        count = self._expire_stale(now or datetime.now(timezone.utc))
        self.db.commit()
        self.db.expire_all()
        return count

    def _expire_stale(self, now: datetime) -> int:
        result = self.db.execute(
            update(SignalEvaluation)
            .where(
                SignalEvaluation.status == "LEASED",
                SignalEvaluation.lease_expires_at <= now,
                SignalEvaluation.deployment_id.in_(
                    select(StrategyDeployment.id).where(
                        StrategyDeployment.status == "ACTIVE"
                    )
                ),
            )
            .values(
                status="PENDING",
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
                error_code=None,
                error_message=None,
                completed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)
