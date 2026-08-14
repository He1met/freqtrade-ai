"""Qualification-separated approval/deployment and injected runtime launcher."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Mapping, Protocol
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import (
    CONFIGURATION_BUNDLES_TABLE,
    DEPLOYMENTS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
)
from app.canonical_v13.runtime_contract import (
    FrozenRuntimeLaunchSpec,
    RuntimeObservationReceipt,
    frozen_runtime_launch_spec_digest,
)


class CanonicalDeploymentBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class RuntimeLauncherPort(Protocol):
    evidence_class: str

    def launch(self, spec: FrozenRuntimeLaunchSpec) -> RuntimeObservationReceipt:
        ...


@dataclass(frozen=True)
class DeploymentResult:
    deployment_id: UUID
    capability_digest: str
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


def _effective(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _require_canonical(connection: Connection) -> Connection:
    effective = _effective(connection)
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalDeploymentBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    return effective


def deployment_capability_digest(
    *,
    deployment_approval_id: UUID,
    approval_digest: str,
    decision: Mapping[str, object],
    bundle: Mapping[str, object],
) -> str:
    return _digest(
        {
            "contract": "canonical-v13-demo-deployment-v1",
            "approval_id": str(deployment_approval_id),
            "approval_digest": approval_digest,
            "qualification_decision_id": str(decision["id"]),
            "decision_digest": decision["decision_digest"],
            "strategy_version_id": str(decision["strategy_version_id"]),
            "configuration_bundle_id": str(bundle["id"]),
            "configuration_bundle_digest": bundle["bundle_digest"],
            "market_snapshot_id": str(bundle["market_snapshot_id"]),
            "market_snapshot_digest": bundle["market_snapshot_digest"],
            "demo_only": True,
            "allow_real_funds": False,
        }
    )


def create_demo_deployment(
    connection: Connection,
    *,
    deployment_approval_id: UUID,
) -> DeploymentResult:
    effective = _require_canonical(connection)
    approval = effective.execute(
        select(DEPLOYMENT_APPROVALS_TABLE).where(
            DEPLOYMENT_APPROVALS_TABLE.c.id == deployment_approval_id
        )
    ).mappings().one_or_none()
    decision = (
        effective.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == approval["qualification_decision_id"]
            )
        ).mappings().one_or_none()
        if approval is not None
        else None
    )
    if (
        approval is None
        or approval["status"] != "APPROVED"
        or decision is None
        or decision["status"] != "QUALIFIED"
    ):
        raise CanonicalDeploymentBlocked(
            "BLOCKED_ACTIVE_APPROVAL_REQUIRED", "approval/qualification gate failed"
        )
    bundle = effective.execute(
        select(CONFIGURATION_BUNDLES_TABLE).where(
            CONFIGURATION_BUNDLES_TABLE.c.id == decision["configuration_bundle_id"]
        )
    ).mappings().one_or_none()
    if (
        bundle is None
        or bundle["bundle_digest"] != decision["configuration_bundle_digest"]
        or bundle["market_snapshot_id"] != decision["market_snapshot_id"]
        or bundle["market_snapshot_digest"] != decision["market_snapshot_digest"]
    ):
        raise CanonicalDeploymentBlocked(
            "BLOCKED_DEPLOYMENT_LINEAGE_DRIFT", "qualified frozen bundle drifted"
        )
    capability_digest = deployment_capability_digest(
        deployment_approval_id=deployment_approval_id,
        approval_digest=approval["approval_digest"],
        decision=decision,
        bundle=bundle,
    )
    deployment_id = uuid4()
    effective.execute(
        DEPLOYMENTS_TABLE.insert().values(
            id=deployment_id,
            deployment_approval_id=deployment_approval_id,
            strategy_version_id=decision["strategy_version_id"],
            configuration_bundle_id=bundle["id"],
            configuration_bundle_digest=bundle["bundle_digest"],
            market_snapshot_id=bundle["market_snapshot_id"],
            market_snapshot_digest=bundle["market_snapshot_digest"],
            status="PENDING",
            demo_only=True,
            allow_real_funds=False,
            capability_digest=capability_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return DeploymentResult(
        deployment_id=deployment_id,
        capability_digest=capability_digest,
        status="PENDING",
    )


def launch_demo_runtime(
    connection: Connection,
    *,
    deployment_id: UUID,
    runtime_identity: str,
    image_digest: str,
    service_account: str,
    credential_reference: str,
    launcher: RuntimeLauncherPort,
) -> UUID:
    """Launch only through an injected port; simulator evidence never activates."""

    effective = _require_canonical(connection)
    deployment = effective.execute(
        select(DEPLOYMENTS_TABLE).where(DEPLOYMENTS_TABLE.c.id == deployment_id)
    ).mappings().one_or_none()
    if deployment is None or deployment["status"] != "PENDING":
        raise CanonicalDeploymentBlocked(
            "BLOCKED_DEPLOYMENT_NOT_PENDING", str(deployment_id)
        )
    approval = effective.execute(
        select(DEPLOYMENT_APPROVALS_TABLE).where(
            DEPLOYMENT_APPROVALS_TABLE.c.id
            == deployment["deployment_approval_id"]
        )
    ).mappings().one()
    decision = effective.execute(
        select(QUALIFICATION_DECISIONS_TABLE).where(
            QUALIFICATION_DECISIONS_TABLE.c.id
            == approval["qualification_decision_id"]
        )
    ).mappings().one()
    spec = FrozenRuntimeLaunchSpec(
        deployment_id=deployment_id,
        approval_id=approval["id"],
        qualification_decision_id=decision["id"],
        strategy_version_id=deployment["strategy_version_id"],
        configuration_bundle_id=deployment["configuration_bundle_id"],
        configuration_bundle_digest=deployment["configuration_bundle_digest"],
        market_snapshot_id=deployment["market_snapshot_id"],
        market_snapshot_digest=deployment["market_snapshot_digest"],
        deployment_capability_digest=deployment["capability_digest"],
        runtime_identity=runtime_identity,
        image_digest=image_digest,
        service_account=service_account,
        network_policy="DEMO_EXCHANGE_ONLY",
        credential_reference=credential_reference,
    )
    launch_digest = frozen_runtime_launch_spec_digest(spec)
    if launcher.evidence_class != "TEST_SIMULATED":
        raise CanonicalDeploymentBlocked(
            "BLOCKED_RUNTIME_LAUNCH_OUT_OF_SCOPE",
            "real service/credential launch requires separate authority",
        )
    receipt = launcher.launch(spec)
    if (
        receipt.launch_spec_digest != launch_digest
        or receipt.evidence_class != "TEST_SIMULATED"
        or receipt.order_writer_capability
    ):
        raise CanonicalDeploymentBlocked(
            "BLOCKED_RUNTIME_LAUNCH_RECEIPT_DRIFT", "launcher receipt is unsafe"
        )
    runtime_id = receipt.runtime_instance_id
    effective.execute(
        RUNTIME_INSTANCES_TABLE.insert().values(
            id=runtime_id,
            deployment_id=deployment_id,
            runtime_identity=runtime_identity,
            image_digest=image_digest,
            launch_spec_digest=launch_digest,
            service_account=service_account,
            network_policy=spec.network_policy,
            credential_reference=credential_reference,
            runtime_class=spec.runtime_class,
            filesystem_mode=spec.filesystem_mode,
            research_executor_capability=spec.research_executor_capability,
            status=receipt.status,
            order_writer_capability=False,
            created_at=datetime.now(timezone.utc),
        )
    )
    effective.execute(
        RUNTIME_RECEIPTS_TABLE.insert().values(
            id=uuid4(),
            runtime_instance_id=runtime_id,
            status=receipt.status,
            launch_spec_digest=receipt.launch_spec_digest,
            capability_digest=receipt.capability_digest,
            network_policy=receipt.network_policy,
            service_account=receipt.service_account,
            order_writer_capability=receipt.order_writer_capability,
            evidence_class=receipt.evidence_class,
            observation_json={
                "runtime_instance_id": str(receipt.runtime_instance_id),
                "launch_spec_digest": receipt.launch_spec_digest,
                "capability_digest": receipt.capability_digest,
                "status": receipt.status,
                "observed_at": receipt.observed_at.isoformat(),
                "network_policy": receipt.network_policy,
                "service_account": receipt.service_account,
                "order_writer_capability": receipt.order_writer_capability,
                "evidence_class": receipt.evidence_class,
            },
            observation_digest=receipt.observation_digest,
            receipt_digest=receipt.receipt_digest,
            observed_at=receipt.observed_at,
        )
    )
    # Simulator evidence deliberately leaves deployment PENDING. Only a separately
    # authorized production launcher receipt can justify ACTIVE.
    return runtime_id


__all__ = [
    "CanonicalDeploymentBlocked",
    "DeploymentResult",
    "RuntimeLauncherPort",
    "create_demo_deployment",
    "deployment_capability_digest",
    "launch_demo_runtime",
]
