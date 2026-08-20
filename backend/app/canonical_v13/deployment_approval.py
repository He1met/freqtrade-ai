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
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import DEPLOYMENT_APPROVALS_TABLE, QUALIFICATION_DECISIONS_TABLE


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
    decision = effective.execute(
        select(QUALIFICATION_DECISIONS_TABLE).where(
            QUALIFICATION_DECISIONS_TABLE.c.id == qualification_decision_id
        )
    ).mappings().one_or_none()
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
    existing = effective.execute(
        select(DEPLOYMENT_APPROVALS_TABLE).where(
            DEPLOYMENT_APPROVALS_TABLE.c.qualification_decision_id
            == qualification_decision_id
        )
    ).mappings().one_or_none()
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


__all__ = [
    "CanonicalDeploymentApprovalBlocked",
    "DeploymentApprovalResult",
    "approve_demo_deployment",
    "deployment_approval_digest",
]
