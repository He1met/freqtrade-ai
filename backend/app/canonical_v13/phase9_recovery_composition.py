"""Server-derived production composition for Phase 9 recovery acceptance."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy import Connection, select

from app.canonical_v13.models import (
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    ORDERS_TABLE,
    RISK_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SIGNALS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.phase9_canary_policy import (
    validate_terminated_canary_risk_policy,
)
from app.canonical_v13.phase9_recovery_acceptance import (
    CanonicalPhase9RecoveryAcceptanceBlocked,
    Phase9RecoveryAcceptance,
    record_phase9_recovery_acceptance,
)
from app.canonical_v13.phase9_runtime_supervisor import (
    Phase9Lease,
    Phase9LifecycleReceipt,
    verify_lifecycle_receipt,
)


RECOVERY_ACTOR_IDENTITY = "canonical-phase9-recovery-operator"


class RecoverySupervisorEvidencePort(Protocol):
    """Read-only supervisor evidence boundary; it cannot launch or stop services."""

    def latest_lifecycle(
        self, *, service_key: str, action: str
    ) -> Phase9LifecycleReceipt: ...

    def launch_agent_loaded(self, service_key: str) -> bool: ...

    def file_lease(self, service_key: str) -> Phase9Lease | None: ...

    def process_alive(self, pid: int) -> bool: ...


def _blocked(code: str, detail: str) -> None:
    raise CanonicalPhase9RecoveryAcceptanceBlocked(code, detail)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        _blocked("BLOCKED_RECOVERY_TIMEZONE", "observed_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _exact_order_and_runtime(
    connection: Connection, qualification_decision_id: UUID
) -> tuple[UUID, str, UUID, str]:
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
    rows = (
        connection.execute(
            select(
                ORDERS_TABLE.c.id,
                ORDERS_TABLE.c.receipt_digest,
                ORDERS_TABLE.c.exchange_order_id,
                RUNTIME_INSTANCES_TABLE.c.id.label("runtime_instance_id"),
            )
            .select_from(exact_chain)
            .where(
                DEPLOYMENT_APPROVALS_TABLE.c.qualification_decision_id
                == qualification_decision_id
            )
        )
        .mappings()
        .all()
    )
    if (
        len(rows) != 1
        or rows[0]["receipt_digest"] is None
        or rows[0]["exchange_order_id"] is None
    ):
        _blocked(
            "BLOCKED_RECOVERY_EXACT_ORDER",
            "one accepted canonical Demo order is required",
        )
    policies = (
        connection.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.qualification_decision_id
                == qualification_decision_id,
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.status == "TERMINATED",
            )
        )
        .mappings()
        .all()
    )
    if len(policies) != 1:
        _blocked(
            "BLOCKED_RECOVERY_TERMINAL_POLICY",
            "one exact terminated canary policy is required",
        )
    terminal = validate_terminated_canary_risk_policy(
        connection, policy_id=policies[0]["id"]
    )
    return (
        rows[0]["id"],
        str(rows[0]["receipt_digest"]),
        rows[0]["runtime_instance_id"],
        terminal.termination_digest,
    )


def accept_phase9_recovery_soak(
    connection: Connection,
    *,
    qualification_decision_id: UUID,
    supervisor: RecoverySupervisorEvidencePort,
    observed_at: datetime,
) -> dict[str, object]:
    """Derive and persist D acceptance without accepting operator-supplied facts."""

    now = _utc(observed_at)
    restart = supervisor.latest_lifecycle(
        service_key="long_lived_runtime", action="RESTART"
    )
    recovery = supervisor.latest_lifecycle(
        service_key="long_lived_runtime", action="RECOVER"
    )
    writer_stop = supervisor.latest_lifecycle(
        service_key="order_writer", action="STOP"
    )
    order_replay = supervisor.latest_lifecycle(
        service_key="order_writer", action="ORDER_REPLAY"
    )
    for receipt in (restart, recovery, writer_stop, order_replay):
        verify_lifecycle_receipt(receipt)
    if (
        restart.status != "CONFIRMED"
        or recovery.status not in {"RECOVERED", "NO_OP"}
        or writer_stop.status != "STOPPED"
        or order_replay.status != "CONFIRMED"
        or order_replay.details.get("repeat_noop") is not True
        or order_replay.details.get("transport_mode") != "GET_ONLY"
        or not isinstance(order_replay.details.get("order_id"), str)
        or not isinstance(order_replay.details.get("order_receipt_digest"), str)
        or not (
            order_replay.observed_at
            <= writer_stop.observed_at
            <= restart.observed_at
            <= recovery.observed_at
            <= now
        )
    ):
        _blocked(
            "BLOCKED_RECOVERY_SUPERVISOR_SEQUENCE",
            "exact replay, writer stop, runtime restart, and recovery sequence "
            "is required",
        )
    if (
        restart.generation != recovery.generation
        or restart.plan_digest != recovery.plan_digest
    ):
        _blocked(
            "BLOCKED_RECOVERY_RUNTIME_LINEAGE",
            "restart and recovery must use one runtime generation and plan",
        )

    for service_key in ("long_lived_runtime", "order_writer"):
        if supervisor.launch_agent_loaded(service_key):
            _blocked(
                "BLOCKED_RECOVERY_LAUNCH_AGENT_LOADED",
                f"{service_key} LaunchAgent is still loaded",
            )
        lease = supervisor.file_lease(service_key)
        if lease is not None:
            state = "live holder" if supervisor.process_alive(lease.pid) else "orphan"
            _blocked(
                "BLOCKED_RECOVERY_FILE_LEASE_PRESENT",
                f"{service_key} {state} file lease remains",
            )

    (
        order_id,
        order_receipt_digest,
        runtime_instance_id,
        policy_termination_receipt_digest,
    ) = _exact_order_and_runtime(connection, qualification_decision_id)
    if (
        order_replay.details["order_id"] != str(order_id)
        or order_replay.details["order_receipt_digest"] != order_receipt_digest
    ):
        _blocked(
            "BLOCKED_RECOVERY_ORDER_REPLAY_DRIFT",
            "GET-only replay receipt differs from the exact persisted order",
        )
    latest_runtime = (
        connection.execute(
            select(RUNTIME_RECEIPTS_TABLE)
            .where(
                RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime_instance_id
            )
            .order_by(RUNTIME_RECEIPTS_TABLE.c.observed_at.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if (
        latest_runtime is None
        or latest_runtime["status"] != "HEALTHY"
        or latest_runtime["evidence_class"] != "PRODUCTION_DEMO_RUNTIME"
        or latest_runtime["order_writer_capability"] is not False
        or latest_runtime["receipt_digest"] is None
        or latest_runtime["observed_at"].astimezone(timezone.utc)
        < recovery.observed_at.astimezone(timezone.utc)
        or latest_runtime["observed_at"].astimezone(timezone.utc) > now
    ):
        _blocked(
            "BLOCKED_RECOVERY_RUNTIME_OBSERVABILITY",
            "latest healthy runtime observation must follow recovery",
        )

    evidence = Phase9RecoveryAcceptance(
        qualification_decision_id=qualification_decision_id,
        runtime_restart=restart,
        runtime_recovery=recovery,
        writer_stop=writer_stop,
        order_replay_receipt_digest=order_receipt_digest,
        policy_termination_receipt_digest=policy_termination_receipt_digest,
        observability_receipt_digest=str(latest_runtime["receipt_digest"]),
        active_supervisor_lease_count=0,
        zombie_process_count=0,
        observed_at=now,
    )
    return record_phase9_recovery_acceptance(
        connection,
        evidence=evidence,
        actor_identity=RECOVERY_ACTOR_IDENTITY,
    )


__all__ = [
    "RECOVERY_ACTOR_IDENTITY",
    "RecoverySupervisorEvidencePort",
    "accept_phase9_recovery_soak",
]
