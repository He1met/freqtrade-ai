"""Approval-writer-only Demo deployment authorization service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.execution_common import lock_execution_boundary
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import (
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
)
from app.canonical_v13.phase9_order_writer import (
    terminal_rejected_canary_order_evidence,
)


class CanonicalDeploymentApprovalBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class DeploymentApprovalResult:
    deployment_approval_id: UUID
    approval_digest: str
    status: str


@dataclass(frozen=True)
class CanaryRecoveryApprovalResult:
    deployment_approval_id: UUID
    approval_digest: str
    approval_generation: int
    recovery_of_deployment_id: UUID
    recovery_order_id: UUID
    recovery_request_digest: str
    recovery_receipt_digest: str
    status: str
    repeat_noop: bool


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def deployment_approval_digest(
    *,
    approval_id: UUID,
    qualification_decision_id: UUID,
    decision: Mapping[str, object],
    actor_identity: str,
    reason: str,
) -> str:
    row = decision
    return _digest(
        {
            "contract": "canonical-v13-demo-deployment-approval-v1",
            "approval_id": str(approval_id),
            "qualification_decision_id": str(qualification_decision_id),
            "decision_digest": row["decision_digest"],
            "lineage": {
                key: str(row[key])
                for key in (
                    "strategy_version_id",
                    "research_target_id",
                    "configuration_bundle_id",
                    "market_snapshot_id",
                    "validation_plan_id",
                )
            },
            "lineage_digests": {
                key: row[key]
                for key in (
                    "configuration_bundle_digest",
                    "market_snapshot_digest",
                    "validation_plan_digest",
                )
            },
            "actor_identity": actor_identity,
            "reason": reason,
            "demo_only": True,
            "allow_real_funds": False,
        }
    )


def approve_demo_deployment(
    connection: Connection,
    *,
    qualification_decision_id: UUID,
    actor_identity: str,
    reason: str,
) -> DeploymentApprovalResult:
    effective = connection
    if connection.dialect.name == "sqlite":
        effective = connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalDeploymentApprovalBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    lock_execution_boundary(
        effective, key=f"deployment-approval:{qualification_decision_id}"
    )
    decision = (
        effective.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id == qualification_decision_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if decision is None or decision["status"] != "QUALIFIED":
        raise CanonicalDeploymentApprovalBlocked(
            "BLOCKED_QUALIFIED_DECISION_REQUIRED",
            "deployment approval requires immutable QUALIFIED evidence",
        )
    if (
        not actor_identity
        or actor_identity != actor_identity.strip()
        or not reason
        or reason != reason.strip()
    ):
        raise CanonicalDeploymentApprovalBlocked(
            "BLOCKED_APPROVAL_AUTHORITY_UNSET", "actor and reason are required"
        )
    existing = (
        effective.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.qualification_decision_id
                == qualification_decision_id,
                DEPLOYMENT_APPROVALS_TABLE.c.approval_generation == 1,
            )
        )
        .mappings()
        .one_or_none()
    )
    approval_id = existing["id"] if existing is not None else uuid4()
    approval_digest = deployment_approval_digest(
        approval_id=approval_id,
        qualification_decision_id=qualification_decision_id,
        decision=decision,
        actor_identity=actor_identity,
        reason=reason,
    )
    if existing is not None:
        if (
            existing["strategy_version_id"] != decision["strategy_version_id"]
            or existing["status"] != "APPROVED"
            or existing["actor_identity"] != actor_identity
            or existing["reason"] != reason
            or existing["approval_digest"] != approval_digest
        ):
            raise CanonicalDeploymentApprovalBlocked(
                "BLOCKED_APPROVAL_REPLAY_DRIFT",
                "qualification already has a different approval receipt",
            )
        return DeploymentApprovalResult(
            deployment_approval_id=approval_id,
            approval_digest=approval_digest,
            status="APPROVED",
        )
    effective.execute(
        DEPLOYMENT_APPROVALS_TABLE.insert().values(
            id=approval_id,
            strategy_version_id=decision["strategy_version_id"],
            qualification_decision_id=qualification_decision_id,
            approval_generation=1,
            status="APPROVED",
            actor_identity=actor_identity,
            reason=reason,
            approval_digest=approval_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return DeploymentApprovalResult(
        deployment_approval_id=approval_id,
        approval_digest=approval_digest,
        status="APPROVED",
    )


def approve_demo_canary_recovery(
    connection: Connection,
    *,
    qualification_decision_id: UUID,
    deployment_id: UUID,
    order_id: UUID,
    actor_identity: str,
    reason: str,
    idempotency_key: str,
) -> CanaryRecoveryApprovalResult:
    """Authorize one and only one fresh canary cycle after zero-side-effect rejection."""

    effective = connection
    if connection.dialect.name == "sqlite":
        effective = connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalDeploymentApprovalBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    for field, value in (
        ("actor_identity", actor_identity),
        ("reason", reason),
        ("idempotency_key", idempotency_key),
    ):
        if not value or value != value.strip():
            raise CanonicalDeploymentApprovalBlocked(
                "BLOCKED_CANARY_RECOVERY_AUTHORITY", f"{field} is required and trimmed"
            )
    lock_execution_boundary(
        effective, key=f"deployment-approval:{qualification_decision_id}"
    )
    lock_execution_boundary(effective, key=f"canary-recovery:{idempotency_key}")
    decision = effective.execute(
        select(QUALIFICATION_DECISIONS_TABLE).where(
            QUALIFICATION_DECISIONS_TABLE.c.id == qualification_decision_id
        )
    ).mappings().one_or_none()
    deployment = effective.execute(
        select(DEPLOYMENTS_TABLE).where(DEPLOYMENTS_TABLE.c.id == deployment_id)
    ).mappings().one_or_none()
    if decision is None or decision["status"] != "QUALIFIED":
        raise CanonicalDeploymentApprovalBlocked(
            "BLOCKED_QUALIFIED_DECISION_REQUIRED",
            "recovery approval requires immutable QUALIFIED evidence",
        )
    if deployment is None or deployment["status"] != "ACTIVE":
        raise CanonicalDeploymentApprovalBlocked(
            "BLOCKED_CANARY_RECOVERY_ACTIVE_DEPLOYMENT",
            "exact active source deployment required",
        )
    source_approval = effective.execute(
        select(DEPLOYMENT_APPROVALS_TABLE).where(
            DEPLOYMENT_APPROVALS_TABLE.c.id == deployment["deployment_approval_id"]
        )
    ).mappings().one_or_none()
    if (
        source_approval is None
        or source_approval["qualification_decision_id"] != qualification_decision_id
        or source_approval["approval_generation"] != 1
        or deployment["strategy_version_id"] != decision["strategy_version_id"]
    ):
        raise CanonicalDeploymentApprovalBlocked(
            "BLOCKED_CANARY_RECOVERY_SOURCE_LINEAGE",
            "source deployment must be generation-one exact qualification lineage",
        )
    nonterminal_runtime_count = len(
        effective.execute(
            select(RUNTIME_INSTANCES_TABLE.c.id).where(
                RUNTIME_INSTANCES_TABLE.c.deployment_id == deployment_id,
                RUNTIME_INSTANCES_TABLE.c.status != "STOPPED",
            )
        ).scalars().all()
    )
    active_writer_lease_count = len(
        effective.execute(
            select(ORDER_WRITER_LEASES_TABLE.c.execution_target).where(
                ORDER_WRITER_LEASES_TABLE.c.status == "ACTIVE"
            )
        ).scalars().all()
    )
    if nonterminal_runtime_count or active_writer_lease_count:
        raise CanonicalDeploymentApprovalBlocked(
            "BLOCKED_CANARY_RECOVERY_SERVICES_NOT_STOPPED",
            f"runtime={nonterminal_runtime_count} writer_lease={active_writer_lease_count}",
        )
    try:
        terminal = terminal_rejected_canary_order_evidence(
            effective, order_id=order_id, deployment_id=deployment_id
        )
    except Exception as exc:
        raise CanonicalDeploymentApprovalBlocked(
            "BLOCKED_CANARY_RECOVERY_TERMINAL_EVIDENCE", type(exc).__name__
        ) from None
    request = {
        "contract": "canonical-v13-canary-recovery-approval-request-v1",
        "qualification_decision_id": str(qualification_decision_id),
        "qualification_decision_digest": decision["decision_digest"],
        "source_approval_id": str(source_approval["id"]),
        "source_approval_digest": source_approval["approval_digest"],
        "recovery_of_deployment_id": str(deployment_id),
        "recovery_deployment_capability_digest": deployment["capability_digest"],
        "recovery_order_id": str(order_id),
        "terminal_evidence_digest": terminal["evidence_digest"],
        "actor_identity": actor_identity,
        "reason": reason,
        "idempotency_key": idempotency_key,
        "approval_generation": 2,
        "demo_only": True,
        "allow_real_funds": False,
        "max_recovery_generations": 1,
    }
    request_digest = _digest(request)
    existing = effective.execute(
        select(DEPLOYMENT_APPROVALS_TABLE).where(
            DEPLOYMENT_APPROVALS_TABLE.c.recovery_idempotency_key == idempotency_key
        )
    ).mappings().one_or_none()
    if existing is None:
        generation_two = effective.execute(
            select(DEPLOYMENT_APPROVALS_TABLE.c.id).where(
                DEPLOYMENT_APPROVALS_TABLE.c.qualification_decision_id
                == qualification_decision_id,
                DEPLOYMENT_APPROVALS_TABLE.c.approval_generation == 2,
            )
        ).scalar_one_or_none()
        if generation_two is not None:
            raise CanonicalDeploymentApprovalBlocked(
                "BLOCKED_CANARY_RECOVERY_ALREADY_AUTHORIZED", str(generation_two)
            )
        approval_id = uuid4()
    else:
        approval_id = existing["id"]
    approval_digest = _digest(
        {
            "contract": "canonical-v13-canary-recovery-approval-v1",
            "approval_id": str(approval_id),
            "request_digest": request_digest,
            "terminal_evidence_digest": terminal["evidence_digest"],
            "status": "APPROVED",
        }
    )
    receipt_digest = _digest(
        {
            "contract": "canonical-v13-canary-recovery-approval-receipt-v1",
            "approval_id": str(approval_id),
            "approval_digest": approval_digest,
            "request_digest": request_digest,
            "status": "APPROVED",
        }
    )
    if existing is not None:
        if (
            existing["qualification_decision_id"] != qualification_decision_id
            or existing["strategy_version_id"] != decision["strategy_version_id"]
            or existing["approval_generation"] != 2
            or existing["recovery_of_deployment_id"] != deployment_id
            or existing["recovery_order_id"] != order_id
            or existing["recovery_request_digest"] != request_digest
            or existing["recovery_receipt_digest"] != receipt_digest
            or existing["approval_digest"] != approval_digest
            or existing["actor_identity"] != actor_identity
            or existing["reason"] != reason
            or existing["status"] != "APPROVED"
        ):
            raise CanonicalDeploymentApprovalBlocked(
                "BLOCKED_CANARY_RECOVERY_REPLAY_DRIFT",
                "recovery key is bound to different immutable evidence",
            )
        repeat_noop = True
    else:
        effective.execute(
            DEPLOYMENT_APPROVALS_TABLE.insert().values(
                id=approval_id,
                strategy_version_id=decision["strategy_version_id"],
                qualification_decision_id=qualification_decision_id,
                approval_generation=2,
                recovery_of_deployment_id=deployment_id,
                recovery_order_id=order_id,
                recovery_idempotency_key=idempotency_key,
                recovery_request_digest=request_digest,
                recovery_receipt_digest=receipt_digest,
                status="APPROVED",
                actor_identity=actor_identity,
                reason=reason,
                approval_digest=approval_digest,
                created_at=datetime.now(timezone.utc),
            )
        )
        repeat_noop = False
    return CanaryRecoveryApprovalResult(
        deployment_approval_id=approval_id,
        approval_digest=approval_digest,
        approval_generation=2,
        recovery_of_deployment_id=deployment_id,
        recovery_order_id=order_id,
        recovery_request_digest=request_digest,
        recovery_receipt_digest=receipt_digest,
        status="APPROVED",
        repeat_noop=repeat_noop,
    )


__all__ = [
    "CanaryRecoveryApprovalResult",
    "CanonicalDeploymentApprovalBlocked",
    "DeploymentApprovalResult",
    "approve_demo_canary_recovery",
    "approve_demo_deployment",
    "deployment_approval_digest",
]
