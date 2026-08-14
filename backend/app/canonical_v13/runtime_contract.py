"""Frozen long-lived Demo runtime launch and observation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import UUID


class CanonicalRuntimeContractBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class FrozenRuntimeLaunchSpec:
    deployment_id: UUID
    approval_id: UUID
    qualification_decision_id: UUID
    strategy_version_id: UUID
    configuration_bundle_id: UUID
    configuration_bundle_digest: str
    market_snapshot_id: UUID
    market_snapshot_digest: str
    deployment_capability_digest: str
    runtime_identity: str
    image_digest: str
    service_account: str
    network_policy: str
    runtime_class: str = "LONG_LIVED_TRADING_RUNTIME"
    filesystem_mode: str = "READ_ONLY"
    research_executor_capability: bool = False
    demo_only: bool = True
    allow_real_funds: bool = False
    credential_reference: str | None = None
    signal_writer_capability: bool = True
    order_writer_capability: bool = False


@dataclass(frozen=True)
class RuntimeObservationReceipt:
    runtime_instance_id: UUID
    launch_spec_digest: str
    capability_digest: str
    status: str
    observed_at: datetime
    network_policy: str
    service_account: str
    order_writer_capability: bool
    evidence_class: str
    observation_digest: str
    receipt_digest: str


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


def frozen_runtime_launch_spec_digest(spec: FrozenRuntimeLaunchSpec) -> str:
    if (
        not spec.demo_only
        or spec.allow_real_funds
        or spec.order_writer_capability
        or not spec.signal_writer_capability
        or spec.network_policy != "DEMO_EXCHANGE_ONLY"
        or spec.runtime_class != "LONG_LIVED_TRADING_RUNTIME"
        or spec.filesystem_mode != "READ_ONLY"
        or spec.research_executor_capability
        or spec.service_account != "canonical_runtime_reader"
        or not spec.runtime_identity
        or not spec.credential_reference
    ):
        raise CanonicalRuntimeContractBlocked(
            "BLOCKED_RUNTIME_CAPABILITY_DRIFT",
            "runtime must be Demo-only, signal-only, separately identified, and credential-referenced",
        )
    return _digest(
        {
            "contract": "canonical-v13-frozen-runtime-launch-v1",
            **{
                key: str(value) if isinstance(value, UUID) else value
                for key, value in spec.__dict__.items()
            },
        }
    )


def build_runtime_observation_receipt(
    *,
    runtime_instance_id: UUID,
    launch_spec: FrozenRuntimeLaunchSpec,
    status: str,
    observed_at: datetime,
    evidence_class: str,
) -> RuntimeObservationReceipt:
    if status not in {"STARTING", "HEALTHY", "DEGRADED", "FAILED", "STOPPED"}:
        raise CanonicalRuntimeContractBlocked(
            "BLOCKED_RUNTIME_STATUS", "runtime observation status is invalid"
        )
    if observed_at.tzinfo is None:
        raise CanonicalRuntimeContractBlocked(
            "BLOCKED_RUNTIME_TIMEZONE_UNSET", "observation requires timezone"
        )
    launch_digest = frozen_runtime_launch_spec_digest(launch_spec)
    observation = {
        "runtime_instance_id": str(runtime_instance_id),
        "launch_spec_digest": launch_digest,
        "capability_digest": launch_spec.deployment_capability_digest,
        "status": status,
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "network_policy": launch_spec.network_policy,
        "service_account": launch_spec.service_account,
        "order_writer_capability": launch_spec.order_writer_capability,
        "evidence_class": evidence_class,
    }
    observation_digest = _digest(observation)
    return RuntimeObservationReceipt(
        runtime_instance_id=runtime_instance_id,
        launch_spec_digest=launch_digest,
        capability_digest=launch_spec.deployment_capability_digest,
        status=status,
        observed_at=observed_at.astimezone(timezone.utc),
        network_policy=launch_spec.network_policy,
        service_account=launch_spec.service_account,
        order_writer_capability=launch_spec.order_writer_capability,
        evidence_class=evidence_class,
        observation_digest=observation_digest,
        receipt_digest=_digest(
            {"contract": "canonical-v13-runtime-observation-v1", **observation}
        ),
    )


def verify_runtime_observation_receipt(
    receipt: RuntimeObservationReceipt,
) -> bool:
    if receipt.observed_at.tzinfo is None:
        return False
    observation = {
        "runtime_instance_id": str(receipt.runtime_instance_id),
        "launch_spec_digest": receipt.launch_spec_digest,
        "capability_digest": receipt.capability_digest,
        "status": receipt.status,
        "observed_at": receipt.observed_at.astimezone(timezone.utc).isoformat(),
        "network_policy": receipt.network_policy,
        "service_account": receipt.service_account,
        "order_writer_capability": receipt.order_writer_capability,
        "evidence_class": receipt.evidence_class,
    }
    return (
        _digest(observation) == receipt.observation_digest
        and _digest(
            {"contract": "canonical-v13-runtime-observation-v1", **observation}
        )
        == receipt.receipt_digest
    )


def assess_runtime_observation(
    receipt: RuntimeObservationReceipt,
    *,
    evaluated_at: datetime,
    maximum_heartbeat_age: timedelta,
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    if not verify_runtime_observation_receipt(receipt):
        reasons.append("RUNTIME_RECEIPT_DIGEST_DRIFT")
    if receipt.evidence_class != "PRODUCTION_DEMO_RUNTIME":
        reasons.append("RUNTIME_EVIDENCE_NOT_PRODUCTION")
    if receipt.status != "HEALTHY":
        reasons.append("RUNTIME_NOT_HEALTHY")
    if receipt.order_writer_capability:
        reasons.append("RUNTIME_ORDER_WRITER_FORBIDDEN")
    if evaluated_at.tzinfo is None or maximum_heartbeat_age <= timedelta(0):
        reasons.append("RUNTIME_HEARTBEAT_POLICY_INVALID")
    elif receipt.observed_at.tzinfo is None:
        reasons.append("RUNTIME_HEARTBEAT_TIMEZONE_UNSET")
    elif evaluated_at - receipt.observed_at > maximum_heartbeat_age:
        reasons.append("RUNTIME_HEARTBEAT_STALE")
    elif receipt.observed_at > evaluated_at:
        reasons.append("RUNTIME_HEARTBEAT_IN_FUTURE")
    unique = tuple(dict.fromkeys(reasons))
    return ("BLOCKED" if unique else "READY", unique)


__all__ = [
    "CanonicalRuntimeContractBlocked",
    "FrozenRuntimeLaunchSpec",
    "RuntimeObservationReceipt",
    "assess_runtime_observation",
    "build_runtime_observation_receipt",
    "frozen_runtime_launch_spec_digest",
    "verify_runtime_observation_receipt",
]
