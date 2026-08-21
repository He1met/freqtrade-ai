"""Persist exact Phase 9 recovery/duplicate-replay acceptance evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, func, select

from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    ORDERS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
    RISK_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SIGNALS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.phase9_canary_policy import (
    validate_terminated_canary_risk_policy,
)
from app.canonical_v13.phase9_runtime_supervisor import (
    Phase9LifecycleReceipt,
    verify_lifecycle_receipt,
)


class CanonicalPhase9RecoveryAcceptanceBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Phase9RecoveryAcceptance:
    qualification_decision_id: UUID
    runtime_restart: Phase9LifecycleReceipt
    runtime_recovery: Phase9LifecycleReceipt
    writer_stop: Phase9LifecycleReceipt
    order_replay_receipt_digest: str
    policy_termination_receipt_digest: str
    observability_receipt_digest: str
    active_supervisor_lease_count: int
    zombie_process_count: int
    observed_at: datetime


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CanonicalPhase9RecoveryAcceptanceBlocked(
                "BLOCKED_RECOVERY_TIMEZONE", "timezone-aware timestamps required"
            )
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            _jsonable(value),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _require_digest(name: str, value: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise CanonicalPhase9RecoveryAcceptanceBlocked(
            "BLOCKED_RECOVERY_DIGEST", f"{name} is not a canonical SHA-256 digest"
        )


def record_phase9_recovery_acceptance(
    connection: Connection,
    *,
    evidence: Phase9RecoveryAcceptance,
    actor_identity: str,
) -> dict[str, object]:
    if not actor_identity.strip():
        raise CanonicalPhase9RecoveryAcceptanceBlocked(
            "BLOCKED_RECOVERY_ACTOR", "actor identity is required"
        )
    for receipt in (
        evidence.runtime_restart,
        evidence.runtime_recovery,
        evidence.writer_stop,
    ):
        verify_lifecycle_receipt(receipt)
    if (
        evidence.runtime_restart.service_key != "long_lived_runtime"
        or evidence.runtime_restart.action != "RESTART"
        or evidence.runtime_restart.status != "CONFIRMED"
        or evidence.runtime_recovery.service_key != "long_lived_runtime"
        or evidence.runtime_recovery.action != "RECOVER"
        or evidence.runtime_recovery.status not in {"RECOVERED", "NO_OP"}
        or evidence.writer_stop.service_key != "order_writer"
        or evidence.writer_stop.action != "STOP"
        or evidence.writer_stop.status != "STOPPED"
    ):
        raise CanonicalPhase9RecoveryAcceptanceBlocked(
            "BLOCKED_RECOVERY_LIFECYCLE",
            "restart/recover/writer-stop receipts are incomplete",
        )
    generations = {
        evidence.runtime_restart.generation,
        evidence.runtime_recovery.generation,
    }
    if (
        len(generations) != 1
        or evidence.runtime_restart.plan_digest != evidence.runtime_recovery.plan_digest
    ):
        raise CanonicalPhase9RecoveryAcceptanceBlocked(
            "BLOCKED_RECOVERY_RUNTIME_LINEAGE",
            "runtime receipts are from different generations",
        )
    _require_digest("order_replay_receipt_digest", evidence.order_replay_receipt_digest)
    _require_digest(
        "policy_termination_receipt_digest",
        evidence.policy_termination_receipt_digest,
    )
    _require_digest(
        "observability_receipt_digest", evidence.observability_receipt_digest
    )
    if (
        evidence.active_supervisor_lease_count != 0
        or evidence.zombie_process_count != 0
    ):
        raise CanonicalPhase9RecoveryAcceptanceBlocked(
            "BLOCKED_RECOVERY_PROCESS_STATE",
            "supervisor leases and zombie processes must be zero",
        )
    active_db_leases = int(
        connection.execute(
            select(func.count())
            .select_from(ORDER_WRITER_LEASES_TABLE)
            .where(ORDER_WRITER_LEASES_TABLE.c.status == "ACTIVE")
        ).scalar_one()
    )
    exact_chain = (
        ORDERS_TABLE.join(
            RISK_DECISIONS_TABLE,
            RISK_DECISIONS_TABLE.c.id == ORDERS_TABLE.c.risk_decision_id,
        )
        .join(
            TRADE_INTENTS_TABLE,
            TRADE_INTENTS_TABLE.c.id == RISK_DECISIONS_TABLE.c.trade_intent_id,
        )
        .join(SIGNALS_TABLE, SIGNALS_TABLE.c.id == TRADE_INTENTS_TABLE.c.signal_id)
        .join(
            RUNTIME_INSTANCES_TABLE,
            RUNTIME_INSTANCES_TABLE.c.id == SIGNALS_TABLE.c.runtime_instance_id,
        )
        .join(
            DEPLOYMENTS_TABLE,
            DEPLOYMENTS_TABLE.c.id == RUNTIME_INSTANCES_TABLE.c.deployment_id,
        )
        .join(
            DEPLOYMENT_APPROVALS_TABLE,
            DEPLOYMENT_APPROVALS_TABLE.c.id
            == DEPLOYMENTS_TABLE.c.deployment_approval_id,
        )
    )
    exact_order_rows = (
        connection.execute(
            select(
                ORDERS_TABLE.c.receipt_digest,
                RUNTIME_INSTANCES_TABLE.c.id.label("runtime_instance_id"),
            )
            .select_from(exact_chain)
            .where(
                DEPLOYMENT_APPROVALS_TABLE.c.qualification_decision_id
                == evidence.qualification_decision_id
            )
        )
        .mappings()
        .all()
    )
    exact_orders = len(exact_order_rows)
    terminal_policies = (
        connection.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.qualification_decision_id
                == evidence.qualification_decision_id,
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.status == "TERMINATED",
            )
        )
        .mappings()
        .all()
    )
    terminal_policy = terminal_policies[0] if len(terminal_policies) == 1 else None
    terminal_receipt = (
        validate_terminated_canary_risk_policy(
            connection, policy_id=terminal_policy["id"]
        )
        if terminal_policy is not None
        else None
    )
    latest_runtime_receipt = None
    if exact_orders == 1:
        latest_runtime_receipt = connection.execute(
            select(RUNTIME_RECEIPTS_TABLE.c.receipt_digest)
            .where(
                RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id
                == exact_order_rows[0]["runtime_instance_id"]
            )
            .order_by(RUNTIME_RECEIPTS_TABLE.c.observed_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    if (
        active_db_leases
        or exact_orders != 1
        or terminal_receipt is None
        or terminal_receipt.termination_digest
        != evidence.policy_termination_receipt_digest
        or exact_order_rows[0]["receipt_digest"] != evidence.order_replay_receipt_digest
        or latest_runtime_receipt != evidence.observability_receipt_digest
    ):
        raise CanonicalPhase9RecoveryAcceptanceBlocked(
            "BLOCKED_RECOVERY_DATABASE_STATE",
            "writer lease, policy termination, exact order replay, or runtime "
            "observability evidence differs",
        )
    payload = {
        "contract": "canonical-v13-phase9-recovery-acceptance-v1",
        **_jsonable(asdict(evidence)),
        "active_db_writer_lease_count": active_db_leases,
        "exact_order_count": exact_orders,
        "actor_identity": actor_identity,
    }
    request_digest = _digest(payload)
    prior = (
        connection.execute(
            select(AUDIT_EVENTS_TABLE).where(
                AUDIT_EVENTS_TABLE.c.aggregate_type == "canonical_phase9_recovery",
                AUDIT_EVENTS_TABLE.c.aggregate_id
                == str(evidence.qualification_decision_id),
                AUDIT_EVENTS_TABLE.c.event_type == "PHASE9_RECOVERY_SOAK_ACCEPTED",
            )
        )
        .mappings()
        .one_or_none()
    )
    if prior is not None:
        if prior["request_digest"] != request_digest:
            raise CanonicalPhase9RecoveryAcceptanceBlocked(
                "BLOCKED_RECOVERY_REPLAY_DRIFT", "terminal acceptance already exists"
            )
        return {
            "status": "ACCEPTED",
            "receipt_digest": prior["receipt_digest"],
            "repeat_noop": True,
        }
    receipt_digest = _digest(
        {
            "event_type": "PHASE9_RECOVERY_SOAK_ACCEPTED",
            "request_digest": request_digest,
        }
    )
    connection.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=uuid4(),
            event_type="PHASE9_RECOVERY_SOAK_ACCEPTED",
            aggregate_type="canonical_phase9_recovery",
            aggregate_id=str(evidence.qualification_decision_id),
            actor_identity=actor_identity,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            evidence_json=payload,
            created_at=evidence.observed_at,
        )
    )
    return {
        "status": "ACCEPTED",
        "receipt_digest": receipt_digest,
        "repeat_noop": False,
    }


__all__ = [
    "CanonicalPhase9RecoveryAcceptanceBlocked",
    "Phase9RecoveryAcceptance",
    "record_phase9_recovery_acceptance",
]
