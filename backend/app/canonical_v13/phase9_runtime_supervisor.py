"""Fail-closed lifecycle contracts for canonical Phase 9 macOS services.

This module contains no ``launchctl`` or exchange integration.  The operating-system
adapter lives in ``scripts/canonical_v13_phase9_service.py`` and is deliberately
injected here so tests can remain network-none and side-effect free.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Final, Mapping, Protocol
from uuid import UUID, uuid4

from app.canonical_v13.phase9_topology import PHASE9_SERVICE_SPECS
from app.canonical_v13.phase9_runtime_worker import RuntimeWorkerReceipt
from app.canonical_v13.runtime_contract import (
    FrozenRuntimeLaunchSpec,
    RuntimeObservationReceipt,
    build_runtime_observation_receipt,
)


class CanonicalPhase9SupervisorBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Phase9LaunchPlan:
    plan_id: UUID
    service_key: str
    stage: str
    launch_agent_label: str
    process_identity: str
    postgres_capability: str
    deployment_id: UUID | None
    deployment_capability_digest: str | None
    image_digest: str | None
    release_digest: str
    generation: int
    prepared_at: datetime
    demo_only: bool
    allow_real_funds: bool
    order_writer_enabled: bool
    plan_digest: str


@dataclass(frozen=True)
class Phase9LifecycleReceipt:
    contract: str
    receipt_id: UUID
    service_key: str
    action: str
    status: str
    plan_digest: str | None
    generation: int
    observed_at: datetime
    holder_token_digest: str | None
    details: Mapping[str, object]
    receipt_digest: str


@dataclass(frozen=True)
class Phase9Lease:
    service_key: str
    generation: int
    plan_digest: str
    release_digest: str
    deployment_id: UUID | None
    deployment_capability_digest: str | None
    image_digest: str | None
    holder_token_digest: str
    pid: int
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


class Phase9LeasePort(Protocol):
    def read(self, service_key: str) -> Phase9Lease | None: ...

    def claim(self, lease: Phase9Lease) -> None: ...

    def replace(self, expected: Phase9Lease, lease: Phase9Lease) -> None: ...

    def release(self, expected: Phase9Lease) -> None: ...


class ProcessProbePort(Protocol):
    def is_alive(self, pid: int) -> bool: ...


class RuntimeWorkerSupervisorPort(Protocol):
    """Injected worker boundary; implementations may route signed receipts onward."""

    def heartbeat(
        self, *, stage: str, plan_digest: str, observed_at: datetime
    ) -> RuntimeWorkerReceipt: ...

    def verify(self, receipt: RuntimeWorkerReceipt) -> bool: ...


_ALLOWED_STAGES: Final[Mapping[str, tuple[str, ...]]] = {
    "long_lived_runtime": ("NO_ORDER_SOAK", "SIGNAL_RISK_SHADOW", "OKX_DEMO_CANARY"),
    "order_writer": ("OKX_DEMO_CANARY",),
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_SUPERVISOR_TIMEZONE", "timezone-aware timestamp required"
        )
    return value.astimezone(timezone.utc)


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            _jsonable(value),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def build_launch_plan(
    *,
    service_key: str,
    stage: str,
    generation: int,
    prepared_at: datetime,
    release_digest: str,
    deployment_id: UUID | None = None,
    deployment_capability_digest: str | None = None,
    image_digest: str | None = None,
    order_writer_enabled: bool = False,
    plan_id: UUID | None = None,
) -> Phase9LaunchPlan:
    """Prepare a frozen launch plan without starting or contacting anything."""

    if service_key not in _ALLOWED_STAGES:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_SERVICE_KEY", f"unsupported service {service_key!r}"
        )
    if stage not in _ALLOWED_STAGES[service_key]:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_STAGE", f"{service_key} cannot run in {stage}"
        )
    if generation < 1:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_GENERATION", "generation must be positive"
        )
    for field, value in (
        ("release_digest", release_digest),
        ("deployment_capability_digest", deployment_capability_digest),
        ("image_digest", image_digest),
    ):
        if (field == "release_digest" or value is not None) and (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_PLAN_DIGEST", f"{field} is not lowercase sha256"
            )
    if service_key == "long_lived_runtime" and (
        deployment_id is None
        or deployment_capability_digest is None
        or image_digest is None
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_RUNTIME_PLAN_LINEAGE_UNSET",
            "runtime plan requires deployment, capability, and image digests",
        )
    if service_key == "order_writer" and (
        deployment_capability_digest is not None or image_digest is not None
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_ORDER_WRITER_PLAN_LINEAGE",
            "writer plan may bind deployment id but not runtime capability/image",
        )
    if service_key == "order_writer" and not order_writer_enabled:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_ORDER_WRITER_DISABLED", "writer requires an explicit canary enable"
        )
    if service_key == "long_lived_runtime" and order_writer_enabled:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_RUNTIME_ORDER_WRITER_FORBIDDEN",
            "runtime identity cannot receive order capability",
        )
    spec = PHASE9_SERVICE_SPECS[service_key]
    resolved_id = plan_id or uuid4()
    resolved_at = _utc(prepared_at)
    payload = {
        "contract": "canonical-v13-phase9-launch-plan-v1",
        "plan_id": resolved_id,
        "service_key": service_key,
        "stage": stage,
        "launch_agent_label": spec.launch_agent_label,
        "process_identity": spec.process_identity,
        "postgres_capability": spec.postgres_capability,
        "deployment_id": deployment_id,
        "deployment_capability_digest": deployment_capability_digest,
        "image_digest": image_digest,
        "release_digest": release_digest,
        "generation": generation,
        "prepared_at": resolved_at,
        "demo_only": True,
        "allow_real_funds": False,
        "order_writer_enabled": order_writer_enabled,
    }
    return Phase9LaunchPlan(
        plan_id=resolved_id,
        service_key=service_key,
        stage=stage,
        launch_agent_label=spec.launch_agent_label,
        process_identity=spec.process_identity,
        postgres_capability=str(spec.postgres_capability),
        deployment_id=deployment_id,
        deployment_capability_digest=deployment_capability_digest,
        image_digest=image_digest,
        release_digest=release_digest,
        generation=generation,
        prepared_at=resolved_at,
        demo_only=True,
        allow_real_funds=False,
        order_writer_enabled=order_writer_enabled,
        plan_digest=_digest(payload),
    )


def verify_launch_plan(plan: Phase9LaunchPlan) -> None:
    rebuilt = build_launch_plan(
        service_key=plan.service_key,
        stage=plan.stage,
        generation=plan.generation,
        prepared_at=plan.prepared_at,
        release_digest=plan.release_digest,
        deployment_id=plan.deployment_id,
        deployment_capability_digest=plan.deployment_capability_digest,
        image_digest=plan.image_digest,
        order_writer_enabled=plan.order_writer_enabled,
        plan_id=plan.plan_id,
    )
    if rebuilt != plan or not plan.demo_only or plan.allow_real_funds:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_PLAN_DRIFT", "launch plan does not match its frozen digest"
        )


def build_lifecycle_receipt(
    *,
    service_key: str,
    action: str,
    status: str,
    generation: int,
    observed_at: datetime,
    plan_digest: str | None = None,
    holder_token_digest: str | None = None,
    details: Mapping[str, object] | None = None,
    receipt_id: UUID | None = None,
) -> Phase9LifecycleReceipt:
    if service_key not in _ALLOWED_STAGES:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_SERVICE_KEY", f"unsupported service {service_key!r}"
        )
    if status not in {
        "PREPARED",
        "CONFIRMED",
        "RUNNING",
        "STOPPED",
        "RECOVERED",
        "NO_OP",
        "BLOCKED",
    }:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RECEIPT_STATUS", f"invalid status {status!r}"
        )
    resolved_id = receipt_id or uuid4()
    resolved_at = _utc(observed_at)
    resolved_details = dict(details or {})
    payload = {
        "contract": "canonical-v13-phase9-lifecycle-receipt-v1",
        "receipt_id": resolved_id,
        "service_key": service_key,
        "action": action,
        "status": status,
        "plan_digest": plan_digest,
        "generation": generation,
        "observed_at": resolved_at,
        "holder_token_digest": holder_token_digest,
        "details": resolved_details,
    }
    return Phase9LifecycleReceipt(
        contract=str(payload["contract"]),
        receipt_id=resolved_id,
        service_key=service_key,
        action=action,
        status=status,
        plan_digest=plan_digest,
        generation=generation,
        observed_at=resolved_at,
        holder_token_digest=holder_token_digest,
        details=resolved_details,
        receipt_digest=_digest(payload),
    )


def verify_lifecycle_receipt(receipt: Phase9LifecycleReceipt) -> None:
    rebuilt = build_lifecycle_receipt(
        service_key=receipt.service_key,
        action=receipt.action,
        status=receipt.status,
        generation=receipt.generation,
        observed_at=receipt.observed_at,
        plan_digest=receipt.plan_digest,
        holder_token_digest=receipt.holder_token_digest,
        details=receipt.details,
        receipt_id=receipt.receipt_id,
    )
    if rebuilt != receipt:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RECEIPT_DRIFT", "lifecycle receipt digest mismatch"
        )


def claim_lease(
    port: Phase9LeasePort,
    *,
    plan: Phase9LaunchPlan,
    holder_token: str,
    pid: int,
    now: datetime,
    ttl: timedelta,
    process_probe: ProcessProbePort,
) -> tuple[Phase9Lease, Phase9LifecycleReceipt]:
    """Claim the one service lease, fencing a dead and expired former owner."""

    verify_launch_plan(plan)
    resolved_now = _utc(now)
    if pid <= 1 or ttl <= timedelta(0) or len(holder_token) < 32:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_LEASE_INPUT", "pid, TTL, or holder token is invalid"
        )
    existing = port.read(plan.service_key)
    recovered = False
    if existing is not None:
        if existing.expires_at > resolved_now or process_probe.is_alive(existing.pid):
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_LEASE_HELD",
                f"generation={existing.generation} pid={existing.pid}",
            )
        port.release(existing)
        recovered = True
    lease = Phase9Lease(
        service_key=plan.service_key,
        generation=plan.generation,
        plan_digest=plan.plan_digest,
        release_digest=plan.release_digest,
        deployment_id=plan.deployment_id,
        deployment_capability_digest=plan.deployment_capability_digest,
        image_digest=plan.image_digest,
        holder_token_digest=_digest({"holder_token": holder_token}),
        pid=pid,
        acquired_at=resolved_now,
        heartbeat_at=resolved_now,
        expires_at=resolved_now + ttl,
    )
    port.claim(lease)
    return lease, build_lifecycle_receipt(
        service_key=plan.service_key,
        action="CLAIM_LEASE",
        status="RECOVERED" if recovered else "RUNNING",
        generation=plan.generation,
        observed_at=resolved_now,
        plan_digest=plan.plan_digest,
        holder_token_digest=lease.holder_token_digest,
        details={
            "pid": pid,
            "orphan_cleaned": recovered,
            "release_digest": plan.release_digest,
            "deployment_id": str(plan.deployment_id) if plan.deployment_id else None,
            "deployment_capability_digest": plan.deployment_capability_digest,
            "image_digest": plan.image_digest,
        },
    )


def heartbeat_lease(
    port: Phase9LeasePort,
    *,
    lease: Phase9Lease,
    holder_token: str,
    now: datetime,
    ttl: timedelta,
) -> tuple[Phase9Lease, Phase9LifecycleReceipt]:
    resolved_now = _utc(now)
    token_digest = _digest({"holder_token": holder_token})
    current = port.read(lease.service_key)
    if current != lease or token_digest != lease.holder_token_digest:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_LEASE_FENCED", "lease owner or generation changed"
        )
    if (
        resolved_now < lease.heartbeat_at
        or resolved_now >= lease.expires_at
        or ttl <= timedelta(0)
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_LEASE_EXPIRED", "heartbeat is late or non-monotonic"
        )
    renewed = Phase9Lease(
        **{
            **asdict(lease),
            "heartbeat_at": resolved_now,
            "expires_at": resolved_now + ttl,
        }
    )
    port.replace(lease, renewed)
    return renewed, build_lifecycle_receipt(
        service_key=lease.service_key,
        action="HEARTBEAT",
        status="RUNNING",
        generation=lease.generation,
        observed_at=resolved_now,
        plan_digest=lease.plan_digest,
        holder_token_digest=token_digest,
        details={
            "pid": lease.pid,
            "expires_at": renewed.expires_at,
            "release_digest": lease.release_digest,
            "deployment_id": str(lease.deployment_id) if lease.deployment_id else None,
            "deployment_capability_digest": lease.deployment_capability_digest,
            "image_digest": lease.image_digest,
        },
    )


def release_lease(
    port: Phase9LeasePort,
    *,
    lease: Phase9Lease,
    holder_token: str,
    now: datetime,
) -> Phase9LifecycleReceipt:
    token_digest = _digest({"holder_token": holder_token})
    if (
        port.read(lease.service_key) != lease
        or token_digest != lease.holder_token_digest
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_LEASE_FENCED", "lease owner or generation changed"
        )
    port.release(lease)
    return build_lifecycle_receipt(
        service_key=lease.service_key,
        action="RELEASE_LEASE",
        status="STOPPED",
        generation=lease.generation,
        observed_at=now,
        plan_digest=lease.plan_digest,
        holder_token_digest=token_digest,
        details={"pid": lease.pid},
    )


def validate_supervised_worker_receipt(
    *,
    plan: Phase9LaunchPlan,
    receipt: RuntimeWorkerReceipt,
    port: RuntimeWorkerSupervisorPort,
) -> None:
    """Accept only a signed worker receipt matching this exact runtime plan."""

    verify_launch_plan(plan)
    for field, value in (
        ("receipt_digest", receipt.receipt_digest),
        ("runtime_receipt_digest", receipt.runtime_receipt_digest),
    ):
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_WORKER_RECEIPT", f"{field} is not lowercase sha256"
            )
    if (
        plan.service_key != "long_lived_runtime"
        or receipt.stage != plan.stage
        or receipt.plan_digest != plan.plan_digest
        or receipt.status != "HEALTHY"
        or receipt.order_submission_enabled is not False
        or receipt.persistence_target != "canonical_signal_writer"
        or not receipt.signature
        or not port.verify(receipt)
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_WORKER_RECEIPT",
            "worker receipt is invalid, unsigned, or does not match the runtime plan",
        )
    if plan.stage == "NO_ORDER_SOAK" and (
        receipt.signal_candidate is not None
        or receipt.signal_candidate_digest is not None
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_NO_ORDER_SOAK_SIGNAL",
            "NO_ORDER_SOAK cannot produce a signal candidate",
        )


def build_production_runtime_observation(
    *,
    plan: Phase9LaunchPlan,
    launch_spec: FrozenRuntimeLaunchSpec,
    runtime_instance_id: UUID,
    lease: Phase9Lease,
    running_receipt: Phase9LifecycleReceipt,
    observed_at: datetime,
) -> RuntimeObservationReceipt:
    """Build production evidence only from an exact running plan and live lease."""

    verify_launch_plan(plan)
    verify_lifecycle_receipt(running_receipt)
    observed = _utc(observed_at)
    if (
        plan.service_key != "long_lived_runtime"
        or plan.deployment_id != launch_spec.deployment_id
        or plan.deployment_capability_digest != launch_spec.deployment_capability_digest
        or plan.image_digest != launch_spec.image_digest
        or plan.process_identity != launch_spec.runtime_identity
        or launch_spec.service_account != "canonical_runtime_reader"
        or launch_spec.order_writer_capability
        or not launch_spec.demo_only
        or launch_spec.allow_real_funds
        or lease.service_key != plan.service_key
        or lease.generation != plan.generation
        or lease.plan_digest != plan.plan_digest
        or lease.release_digest != plan.release_digest
        or lease.deployment_id != plan.deployment_id
        or lease.deployment_capability_digest != plan.deployment_capability_digest
        or lease.image_digest != plan.image_digest
        or lease.holder_token_digest != running_receipt.holder_token_digest
        or lease.pid <= 1
        or lease.heartbeat_at > observed
        or lease.expires_at <= observed
        or running_receipt.service_key != plan.service_key
        or running_receipt.generation != plan.generation
        or running_receipt.plan_digest != plan.plan_digest
        or running_receipt.status != "RUNNING"
        or running_receipt.action != "HEARTBEAT"
        or running_receipt.observed_at != lease.heartbeat_at
        or running_receipt.observed_at > observed
        or running_receipt.details.get("pid") != lease.pid
        or running_receipt.details.get("release_digest") != plan.release_digest
        or running_receipt.details.get("deployment_id") != str(plan.deployment_id)
        or running_receipt.details.get("deployment_capability_digest")
        != plan.deployment_capability_digest
        or running_receipt.details.get("image_digest") != plan.image_digest
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PRODUCTION_RUNTIME_OBSERVATION",
            "launch spec, release-bound plan, lifecycle, or live lease drifted",
        )
    return build_runtime_observation_receipt(
        runtime_instance_id=runtime_instance_id,
        launch_spec=launch_spec,
        status="HEALTHY",
        observed_at=observed,
        evidence_class="PRODUCTION_DEMO_RUNTIME",
    )


__all__ = [
    "CanonicalPhase9SupervisorBlocked",
    "Phase9LaunchPlan",
    "Phase9Lease",
    "Phase9LeasePort",
    "Phase9LifecycleReceipt",
    "ProcessProbePort",
    "RuntimeWorkerSupervisorPort",
    "build_launch_plan",
    "build_lifecycle_receipt",
    "build_production_runtime_observation",
    "claim_lease",
    "heartbeat_lease",
    "release_lease",
    "validate_supervised_worker_receipt",
    "verify_launch_plan",
    "verify_lifecycle_receipt",
]
