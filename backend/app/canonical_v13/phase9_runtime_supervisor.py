"""Fail-closed lifecycle contracts for canonical Phase 9 macOS services.

This module contains no ``launchctl`` or exchange integration.  The operating-system
adapter lives in ``scripts/canonical_v13_phase9_service.py`` and is deliberately
injected here so tests can remain network-none and side-effect free.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
from app.canonical_v13.runtime_image_authority import (
    AcceptedRuntimeImage,
    verify_accepted_runtime_image,
)


class CanonicalPhase9SupervisorBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class OrderWriterCanaryAuthority:
    deployment_id: UUID
    deployment_capability_digest: str
    execution_canary_risk_policy_id: UUID
    execution_canary_risk_policy_digest: str
    attestation_id: UUID
    attestation_digest: str
    attestation_expires_at: datetime
    instrument_metadata_digest: str
    mark_price_snapshot_digest: str
    strategy_max_leverage: str
    effective_leverage: str
    position_policy: str


@dataclass(frozen=True)
class RuntimeImagePlanAuthority:
    acceptance_id: UUID
    image_manifest_digest: str
    image_config_digest: str
    acceptance_receipt_digest: str
    release_digest: str


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
    runtime_image_acceptance_id: UUID | None = None
    runtime_image_acceptance_receipt_digest: str | None = None
    runtime_image_config_digest: str | None = None
    order_writer_canary_authority: OrderWriterCanaryAuthority | None = None
    recovery_order_id: UUID | None = None


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
    runtime_image_acceptance_id: UUID | None = None
    runtime_image_acceptance_receipt_digest: str | None = None
    runtime_image_config_digest: str | None = None
    order_writer_canary_authority: OrderWriterCanaryAuthority | None = None
    recovery_order_id: UUID | None = None


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


class OrderWriterCanaryAuthorityPort(Protocol):
    """Revalidates the frozen authority against its current canonical sources."""

    def verify(
        self, authority: OrderWriterCanaryAuthority, *, observed_at: datetime
    ) -> bool: ...

    def verify_recovery(
        self,
        authority: OrderWriterCanaryAuthority,
        *,
        order_id: UUID,
        observed_at: datetime,
    ) -> bool: ...


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
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
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


def _require_digest(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_PLAN_DIGEST", f"{field} is not lowercase sha256"
        )
    return value


def runtime_image_plan_authority(
    accepted: AcceptedRuntimeImage,
) -> RuntimeImagePlanAuthority:
    accepted = verify_accepted_runtime_image(accepted)
    return RuntimeImagePlanAuthority(
        acceptance_id=accepted.acceptance_id,
        image_manifest_digest=accepted.image_manifest_digest,
        image_config_digest=accepted.image_config_digest,
        acceptance_receipt_digest=accepted.receipt_digest,
        release_digest=accepted.release_digest,
    )


def build_order_writer_canary_authority(
    *,
    deployment_id: UUID,
    deployment_capability_digest: str,
    execution_canary_risk_policy_id: UUID,
    execution_canary_risk_policy_digest: str,
    attestation_id: UUID,
    attestation_digest: str,
    attestation_expires_at: datetime,
    instrument_metadata_digest: str,
    mark_price_snapshot_digest: str,
    strategy_max_leverage: str,
    effective_leverage: str,
    position_policy: str = "LONG_ONLY",
) -> OrderWriterCanaryAuthority:
    """Build a secret-free, exact authority binding for one Demo canary writer."""

    for field, value in (
        ("deployment_id", deployment_id),
        ("execution_canary_risk_policy_id", execution_canary_risk_policy_id),
        ("attestation_id", attestation_id),
    ):
        if not isinstance(value, UUID):
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_ORDER_WRITER_CANARY_AUTHORITY", f"{field} must be a UUID"
            )
    for field, value in (
        ("deployment_capability_digest", deployment_capability_digest),
        ("execution_canary_risk_policy_digest", execution_canary_risk_policy_digest),
        ("attestation_digest", attestation_digest),
        ("instrument_metadata_digest", instrument_metadata_digest),
        ("mark_price_snapshot_digest", mark_price_snapshot_digest),
    ):
        _require_digest(value, field=field)
    try:
        strategy_cap = Decimal(str(strategy_max_leverage))
        leverage = Decimal(str(effective_leverage))
    except (InvalidOperation, TypeError, ValueError):
        strategy_cap = Decimal(0)
        leverage = Decimal(0)
    if (
        not strategy_cap.is_finite()
        or strategy_cap <= 0
        or not leverage.is_finite()
        or leverage <= 0
        or leverage > strategy_cap
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_ORDER_WRITER_EFFECTIVE_LEVERAGE",
            "effective leverage must be positive and no greater than the artifact cap",
        )
    if position_policy != "LONG_ONLY":
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_ORDER_WRITER_POSITION_POLICY", "position policy must be LONG_ONLY"
        )
    return OrderWriterCanaryAuthority(
        deployment_id=deployment_id,
        deployment_capability_digest=deployment_capability_digest,
        execution_canary_risk_policy_id=execution_canary_risk_policy_id,
        execution_canary_risk_policy_digest=execution_canary_risk_policy_digest,
        attestation_id=attestation_id,
        attestation_digest=attestation_digest,
        attestation_expires_at=_utc(attestation_expires_at),
        instrument_metadata_digest=instrument_metadata_digest,
        mark_price_snapshot_digest=mark_price_snapshot_digest,
        strategy_max_leverage=format(strategy_cap.normalize(), "f"),
        effective_leverage=format(leverage.normalize(), "f"),
        position_policy="LONG_ONLY",
    )


def require_current_order_writer_canary_authority(
    *,
    plan: Phase9LaunchPlan,
    observed_at: datetime,
    port: OrderWriterCanaryAuthorityPort | None,
) -> None:
    """Fail closed unless the exact frozen writer lineage is still authorized."""

    if plan.service_key != "order_writer":
        return
    if plan.recovery_order_id is not None:
        observed = _utc(observed_at)
        if plan.order_writer_canary_authority is None or port is None:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_ORDER_WRITER_RECOVERY_AUTHORITY",
                "exact recovery order and immutable canary verifier are required",
            )
        try:
            verified = port.verify_recovery(
                plan.order_writer_canary_authority,
                order_id=plan.recovery_order_id,
                observed_at=observed,
            )
        except Exception as exc:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_ORDER_WRITER_RECOVERY_AUTHORITY",
                f"recovery authority verification failed: {type(exc).__name__}",
            ) from exc
        if verified is not True:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_ORDER_WRITER_RECOVERY_AUTHORITY",
                "recovery order does not match the frozen writer authority",
            )
        return
    _require_current_order_writer_canary_authority(
        authority=plan.order_writer_canary_authority,
        observed_at=observed_at,
        port=port,
    )


def _require_current_order_writer_canary_authority(
    *,
    authority: OrderWriterCanaryAuthority | None,
    observed_at: datetime,
    port: OrderWriterCanaryAuthorityPort | None,
) -> None:
    observed = _utc(observed_at)
    if authority is None or port is None:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
            "exact canary lineage verifier is required",
        )
    try:
        verified = port.verify(authority, observed_at=observed)
    except Exception as exc:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
            f"authority verification failed: {type(exc).__name__}",
        ) from exc
    if verified is not True:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
            "current canonical authority does not match the frozen plan",
        )


def build_launch_plan(
    *,
    service_key: str,
    stage: str,
    generation: int,
    prepared_at: datetime,
    release_digest: str,
    deployment_id: UUID | None = None,
    deployment_capability_digest: str | None = None,
    runtime_image_authority: RuntimeImagePlanAuthority | None = None,
    order_writer_enabled: bool = False,
    order_writer_canary_authority: OrderWriterCanaryAuthority | None = None,
    recovery_order_id: UUID | None = None,
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
        (
            "image_digest",
            runtime_image_authority.image_manifest_digest
            if runtime_image_authority is not None
            else None,
        ),
        (
            "runtime_image_acceptance_receipt_digest",
            runtime_image_authority.acceptance_receipt_digest
            if runtime_image_authority is not None
            else None,
        ),
        (
            "runtime_image_config_digest",
            runtime_image_authority.image_config_digest
            if runtime_image_authority is not None
            else None,
        ),
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
        or runtime_image_authority is None
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_RUNTIME_PLAN_LINEAGE_UNSET",
            "runtime plan requires deployment, capability, and image digests",
        )
    if service_key == "order_writer" and runtime_image_authority is not None:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_ORDER_WRITER_PLAN_LINEAGE",
            "writer plan cannot receive the runtime image capability",
        )
    if service_key == "order_writer" and not order_writer_enabled:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_ORDER_WRITER_DISABLED", "writer requires an explicit canary enable"
        )
    if service_key == "order_writer":
        authority = order_writer_canary_authority
        if (
            not isinstance(authority, OrderWriterCanaryAuthority)
            or deployment_id is None
            or deployment_capability_digest is None
            or authority.deployment_id != deployment_id
            or authority.deployment_capability_digest
            != deployment_capability_digest
        ):
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
                "writer plan requires exact current deployment and canary authority",
            )
        rebuilt_authority = build_order_writer_canary_authority(
            **asdict(authority)
        )
        if rebuilt_authority != authority:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
                "writer canary authority is not canonical",
            )
        if recovery_order_id is not None and not isinstance(recovery_order_id, UUID):
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_ORDER_WRITER_RECOVERY_ORDER",
                "recovery order must be an exact UUID",
            )
    elif order_writer_canary_authority is not None or recovery_order_id is not None:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_RUNTIME_ORDER_WRITER_FORBIDDEN",
            "runtime identity cannot bind order writer authority or recovery order",
        )
    if service_key == "long_lived_runtime" and order_writer_enabled:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_RUNTIME_ORDER_WRITER_FORBIDDEN",
            "runtime identity cannot receive order capability",
        )
    spec = PHASE9_SERVICE_SPECS[service_key]
    resolved_id = plan_id or uuid4()
    resolved_at = _utc(prepared_at)
    if runtime_image_authority is not None:
        if not isinstance(runtime_image_authority.acceptance_id, UUID):
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_RUNTIME_IMAGE_ACCEPTANCE_ID",
                "accepted runtime image UUID is required",
            )
        if runtime_image_authority.release_digest != release_digest:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_RUNTIME_IMAGE_RELEASE_DRIFT",
                "accepted runtime image release differs from launch release",
            )
        image_digest = runtime_image_authority.image_manifest_digest
        runtime_image_config_digest = runtime_image_authority.image_config_digest
        runtime_image_acceptance_id = runtime_image_authority.acceptance_id
        runtime_image_acceptance_receipt_digest = (
            runtime_image_authority.acceptance_receipt_digest
        )
    else:
        image_digest = None
        runtime_image_config_digest = None
        runtime_image_acceptance_id = None
        runtime_image_acceptance_receipt_digest = None
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
        "runtime_image_acceptance_id": runtime_image_acceptance_id,
        "runtime_image_acceptance_receipt_digest": runtime_image_acceptance_receipt_digest,
        "runtime_image_config_digest": runtime_image_config_digest,
        "release_digest": release_digest,
        "generation": generation,
        "prepared_at": resolved_at,
        "demo_only": True,
        "allow_real_funds": False,
        "order_writer_enabled": order_writer_enabled,
        "order_writer_canary_authority": order_writer_canary_authority,
    }
    if recovery_order_id is not None:
        payload["recovery_order_id"] = recovery_order_id
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
        runtime_image_acceptance_id=runtime_image_acceptance_id,
        runtime_image_acceptance_receipt_digest=runtime_image_acceptance_receipt_digest,
        runtime_image_config_digest=runtime_image_config_digest,
        release_digest=release_digest,
        generation=generation,
        prepared_at=resolved_at,
        demo_only=True,
        allow_real_funds=False,
        order_writer_enabled=order_writer_enabled,
        order_writer_canary_authority=order_writer_canary_authority,
        recovery_order_id=recovery_order_id,
        plan_digest=_digest(payload),
    )


def verify_launch_plan(plan: Phase9LaunchPlan) -> None:
    authority = (
        RuntimeImagePlanAuthority(
            acceptance_id=plan.runtime_image_acceptance_id,
            image_manifest_digest=plan.image_digest,
            image_config_digest=plan.runtime_image_config_digest,
            acceptance_receipt_digest=plan.runtime_image_acceptance_receipt_digest,
            release_digest=plan.release_digest,
        )
        if plan.runtime_image_acceptance_id is not None
        and plan.image_digest is not None
        and plan.runtime_image_acceptance_receipt_digest is not None
        and plan.runtime_image_config_digest is not None
        else None
    )
    rebuilt = build_launch_plan(
        service_key=plan.service_key,
        stage=plan.stage,
        generation=plan.generation,
        prepared_at=plan.prepared_at,
        release_digest=plan.release_digest,
        deployment_id=plan.deployment_id,
        deployment_capability_digest=plan.deployment_capability_digest,
        runtime_image_authority=authority,
        order_writer_enabled=plan.order_writer_enabled,
        order_writer_canary_authority=plan.order_writer_canary_authority,
        recovery_order_id=plan.recovery_order_id,
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
        "ACCEPTED",
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
    authority_port: OrderWriterCanaryAuthorityPort | None = None,
) -> tuple[Phase9Lease, Phase9LifecycleReceipt]:
    """Claim the one service lease, fencing a dead and expired former owner."""

    verify_launch_plan(plan)
    resolved_now = _utc(now)
    require_current_order_writer_canary_authority(
        plan=plan, observed_at=resolved_now, port=authority_port
    )
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
        runtime_image_acceptance_id=plan.runtime_image_acceptance_id,
        runtime_image_acceptance_receipt_digest=plan.runtime_image_acceptance_receipt_digest,
        runtime_image_config_digest=plan.runtime_image_config_digest,
        order_writer_canary_authority=plan.order_writer_canary_authority,
        recovery_order_id=plan.recovery_order_id,
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
            "runtime_image_acceptance_id": str(plan.runtime_image_acceptance_id),
            "runtime_image_acceptance_receipt_digest": plan.runtime_image_acceptance_receipt_digest,
            "runtime_image_config_digest": plan.runtime_image_config_digest,
            **(
                {"recovery_order_id": str(plan.recovery_order_id)}
                if plan.recovery_order_id
                else {}
            ),
        },
    )


def heartbeat_lease(
    port: Phase9LeasePort,
    *,
    lease: Phase9Lease,
    holder_token: str,
    now: datetime,
    ttl: timedelta,
    authority_port: OrderWriterCanaryAuthorityPort | None = None,
) -> tuple[Phase9Lease, Phase9LifecycleReceipt]:
    resolved_now = _utc(now)
    token_digest = _digest({"holder_token": holder_token})
    current = port.read(lease.service_key)
    if current != lease or token_digest != lease.holder_token_digest:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_LEASE_FENCED", "lease owner or generation changed"
        )
    if lease.service_key == "order_writer":
        if lease.recovery_order_id is not None:
            if lease.order_writer_canary_authority is None or authority_port is None:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_ORDER_WRITER_RECOVERY_AUTHORITY",
                    "exact recovery order and immutable canary verifier are required",
                )
            try:
                verified = authority_port.verify_recovery(
                    lease.order_writer_canary_authority,
                    order_id=lease.recovery_order_id,
                    observed_at=resolved_now,
                )
            except Exception as exc:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_ORDER_WRITER_RECOVERY_AUTHORITY",
                    f"recovery authority verification failed: {type(exc).__name__}",
                ) from exc
            if verified is not True:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_ORDER_WRITER_RECOVERY_AUTHORITY",
                    "recovery order does not match the frozen writer authority",
                )
        else:
            _require_current_order_writer_canary_authority(
                authority=lease.order_writer_canary_authority,
                observed_at=resolved_now,
                port=authority_port,
            )
    if (
        resolved_now < lease.heartbeat_at
        or resolved_now >= lease.expires_at
        or ttl <= timedelta(0)
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_LEASE_EXPIRED", "heartbeat is late or non-monotonic"
        )
    renewed = replace(
        lease,
        heartbeat_at=resolved_now,
        expires_at=resolved_now + ttl,
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
            "runtime_image_acceptance_id": str(lease.runtime_image_acceptance_id),
            "runtime_image_acceptance_receipt_digest": lease.runtime_image_acceptance_receipt_digest,
            "runtime_image_config_digest": lease.runtime_image_config_digest,
            **(
                {"recovery_order_id": str(lease.recovery_order_id)}
                if lease.recovery_order_id
                else {}
            ),
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
        or lease.runtime_image_acceptance_id != plan.runtime_image_acceptance_id
        or lease.runtime_image_acceptance_receipt_digest
        != plan.runtime_image_acceptance_receipt_digest
        or lease.runtime_image_config_digest != plan.runtime_image_config_digest
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
        or running_receipt.details.get("runtime_image_acceptance_id")
        != str(plan.runtime_image_acceptance_id)
        or running_receipt.details.get("runtime_image_acceptance_receipt_digest")
        != plan.runtime_image_acceptance_receipt_digest
        or running_receipt.details.get("runtime_image_config_digest")
        != plan.runtime_image_config_digest
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


def build_production_runtime_stop_observation(
    *,
    plan: Phase9LaunchPlan,
    launch_spec: FrozenRuntimeLaunchSpec,
    runtime_instance_id: UUID,
    stop_receipt: Phase9LifecycleReceipt,
    observed_at: datetime,
    launch_agent_loaded: bool,
    holder_pid_alive: bool,
    lease: Phase9Lease | None,
    container_present: bool,
) -> RuntimeObservationReceipt:
    """Build STOPPED evidence only after every runtime holder is absent."""

    verify_launch_plan(plan)
    verify_lifecycle_receipt(stop_receipt)
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
        or stop_receipt.service_key != plan.service_key
        or stop_receipt.generation != plan.generation
        or stop_receipt.plan_digest != plan.plan_digest
        or stop_receipt.status != "STOPPED"
        or stop_receipt.action != "STOP"
        or stop_receipt.observed_at > observed
        or stop_receipt.details.get("label") != plan.launch_agent_label
        or launch_agent_loaded
        or holder_pid_alive
        or lease is not None
        or container_present
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PRODUCTION_RUNTIME_STOP_OBSERVATION",
            "exact STOP receipt and absent launchd, lease, and container are required",
        )
    return build_runtime_observation_receipt(
        runtime_instance_id=runtime_instance_id,
        launch_spec=launch_spec,
        status="STOPPED",
        observed_at=observed,
        evidence_class="PRODUCTION_DEMO_RUNTIME_STOP",
    )


__all__ = [
    "CanonicalPhase9SupervisorBlocked",
    "OrderWriterCanaryAuthority",
    "OrderWriterCanaryAuthorityPort",
    "Phase9LaunchPlan",
    "Phase9Lease",
    "Phase9LeasePort",
    "Phase9LifecycleReceipt",
    "ProcessProbePort",
    "RuntimeWorkerSupervisorPort",
    "RuntimeImagePlanAuthority",
    "build_launch_plan",
    "build_lifecycle_receipt",
    "build_order_writer_canary_authority",
    "build_production_runtime_observation",
    "build_production_runtime_stop_observation",
    "claim_lease",
    "heartbeat_lease",
    "release_lease",
    "require_current_order_writer_canary_authority",
    "runtime_image_plan_authority",
    "validate_supervised_worker_receipt",
    "verify_launch_plan",
    "verify_lifecycle_receipt",
]
