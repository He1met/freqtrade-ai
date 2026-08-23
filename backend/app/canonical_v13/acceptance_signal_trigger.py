"""One-shot, control-plane scheduled Phase 9 acceptance signal evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    lock_execution_boundary,
    require_canonical_execution,
    require_identity,
)
from app.canonical_v13.models import (
    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
    RUNTIME_IMAGE_ACCEPTANCES_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SIGNALS_TABLE,
)
from app.canonical_v13.phase9_runtime_worker import (
    ReceiptSignerPort,
    ReceiptVerifierPort,
    RuntimeSignalCandidate,
    RuntimeWorkerReceipt,
    _digest as runtime_worker_digest,
    verify_runtime_worker_receipt,
)


SOURCE_KIND: Final = "ACCEPTANCE_SCHEDULED_TEST"
EXECUTION_TARGET: Final = "OKX_DEMO"
POSITION_POLICY: Final = "LONG_ONLY"
TRIGGER_TTL: Final = timedelta(minutes=2)
MAXIMUM_RUNTIME_RECEIPT_AGE: Final = timedelta(minutes=5)
MAXIMUM_SIGNAL_PERSISTENCE_LAG: Final = timedelta(seconds=30)


@dataclass(frozen=True)
class AcceptanceSignalTriggerReceipt:
    trigger_id: UUID
    source_kind: str
    scheduled_at: datetime
    expires_at: datetime
    request_digest: str
    receipt_digest: str
    repeat_noop: bool


@dataclass(frozen=True)
class AcceptanceSignalExecutionReceipt:
    trigger_id: UUID
    signal_id: UUID
    source_kind: str
    signal_digest: str
    worker_receipt_digest: str
    repeat_noop: bool


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ACCEPTANCE_TRIGGER_TIME", f"{field} must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    """Normalize database timestamps; SQLite drops timezone metadata in tests."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _next_closed_boundary(now: datetime) -> datetime:
    current = _utc(now, field="issued_at")
    minute = (current.minute // 15 + 1) * 15
    if minute == 60:
        return current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return current.replace(minute=minute, second=0, microsecond=0)


def _latest_runtime_receipt(connection: Connection, runtime_id: UUID):
    return (
        connection.execute(
            select(RUNTIME_RECEIPTS_TABLE)
            .where(RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime_id)
            .order_by(RUNTIME_RECEIPTS_TABLE.c.observed_at.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )


def issue_acceptance_signal_trigger(
    connection: Connection,
    *,
    qualification_decision_id: UUID,
    deployment_approval_id: UUID,
    deployment_id: UUID,
    runtime_instance_id: UUID,
    runtime_image_acceptance_id: UUID,
    actor_identity: str,
    idempotency_key: str,
    issued_at: datetime | None = None,
) -> AcceptanceSignalTriggerReceipt:
    """Issue one immutable server-scheduled trigger for one deployment."""

    effective = require_canonical_execution(connection)
    actor = require_identity(actor_identity, field="actor_identity", maximum=160)
    key = require_identity(idempotency_key, field="idempotency_key")
    now = _utc(issued_at or datetime.now(timezone.utc), field="issued_at")
    scheduled_at = _next_closed_boundary(now)
    expires_at = scheduled_at + TRIGGER_TTL
    request = {
        "contract": "canonical-v13-acceptance-signal-trigger-request-v1",
        "qualification_decision_id": str(qualification_decision_id),
        "deployment_approval_id": str(deployment_approval_id),
        "deployment_id": str(deployment_id),
        "runtime_instance_id": str(runtime_instance_id),
        "runtime_image_acceptance_id": str(runtime_image_acceptance_id),
        "actor_identity": actor,
        "idempotency_key": key,
        "source_kind": SOURCE_KIND,
        "execution_target": EXECUTION_TARGET,
        "allow_real_funds": False,
        "acceptance_only": True,
        "position_policy": POSITION_POLICY,
        "max_order_count": 1,
    }
    request_digest = canonical_execution_digest(request)
    lock_execution_boundary(effective, key=f"acceptance-trigger:{deployment_id}")
    existing = (
        effective.execute(
            select(ACCEPTANCE_SIGNAL_TRIGGERS_TABLE).where(
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.idempotency_key == key
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["request_digest"] != request_digest:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_ACCEPTANCE_TRIGGER_REPLAY_DRIFT",
                "idempotency key is bound to different trigger lineage",
            )
        return AcceptanceSignalTriggerReceipt(
            trigger_id=existing["id"],
            source_kind=existing["source_kind"],
            scheduled_at=_stored_utc(existing["scheduled_at"]),
            expires_at=_stored_utc(existing["expires_at"]),
            request_digest=existing["request_digest"],
            receipt_digest=existing["receipt_digest"],
            repeat_noop=True,
        )
    if effective.execute(
        select(ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.id).where(
            ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.deployment_id == deployment_id
        )
    ).scalar_one_or_none() is not None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ACCEPTANCE_TRIGGER_ALREADY_ISSUED",
            "one deployment can never reset or renew its acceptance trigger",
        )

    qualification = effective.execute(
        select(QUALIFICATION_DECISIONS_TABLE).where(
            QUALIFICATION_DECISIONS_TABLE.c.id == qualification_decision_id
        )
    ).mappings().one_or_none()
    approval = effective.execute(
        select(DEPLOYMENT_APPROVALS_TABLE).where(
            DEPLOYMENT_APPROVALS_TABLE.c.id == deployment_approval_id
        )
    ).mappings().one_or_none()
    deployment = effective.execute(
        select(DEPLOYMENTS_TABLE).where(DEPLOYMENTS_TABLE.c.id == deployment_id)
    ).mappings().one_or_none()
    runtime = effective.execute(
        select(RUNTIME_INSTANCES_TABLE).where(
            RUNTIME_INSTANCES_TABLE.c.id == runtime_instance_id
        )
    ).mappings().one_or_none()
    image = effective.execute(
        select(RUNTIME_IMAGE_ACCEPTANCES_TABLE).where(
            RUNTIME_IMAGE_ACCEPTANCES_TABLE.c.id == runtime_image_acceptance_id
        )
    ).mappings().one_or_none()
    receipt = _latest_runtime_receipt(effective, runtime_instance_id)
    observed_at = receipt["observed_at"] if receipt is not None else None
    if observed_at is not None and observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    exact = bool(
        qualification
        and qualification["status"] == "QUALIFIED"
        and approval
        and approval["status"] == "APPROVED"
        and approval["qualification_decision_id"] == qualification_decision_id
        and deployment
        and deployment["status"] == "ACTIVE"
        and deployment["deployment_approval_id"] == deployment_approval_id
        and deployment["strategy_version_id"] == qualification["strategy_version_id"]
        and deployment["configuration_bundle_id"] == qualification["configuration_bundle_id"]
        and deployment["configuration_bundle_digest"] == qualification["configuration_bundle_digest"]
        and deployment["market_snapshot_id"] == qualification["market_snapshot_id"]
        and deployment["market_snapshot_digest"] == qualification["market_snapshot_digest"]
        and deployment["demo_only"] is True
        and deployment["allow_real_funds"] is False
        and runtime
        and runtime["deployment_id"] == deployment_id
        and runtime["status"] == "HEALTHY"
        and runtime["order_writer_capability"] is False
        and image
        and image["image_manifest_digest"] == runtime["image_digest"]
        and image["demo_only"] is True
        and image["allow_real_funds"] is False
        and receipt
        and receipt["status"] == "HEALTHY"
        and receipt["evidence_class"] == "PRODUCTION_DEMO_RUNTIME"
        and observed_at is not None
        and observed_at <= now
        and now - observed_at <= MAXIMUM_RUNTIME_RECEIPT_AGE
    )
    if not exact:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ACCEPTANCE_TRIGGER_LINEAGE",
            "exact QUALIFIED/approved/ACTIVE Demo runtime image lineage is required",
        )
    receipt_payload = {
        "contract": "canonical-v13-acceptance-signal-trigger-receipt-v1",
        "request_digest": request_digest,
        "scheduled_at": scheduled_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "qualification_decision_digest": qualification["decision_digest"],
        "deployment_approval_digest": approval["approval_digest"],
        "deployment_capability_digest": deployment["capability_digest"],
        "runtime_launch_spec_digest": runtime["launch_spec_digest"],
        "runtime_image_receipt_digest": image["receipt_digest"],
        "configuration_bundle_digest": deployment["configuration_bundle_digest"],
        "market_snapshot_digest": deployment["market_snapshot_digest"],
    }
    receipt_digest = canonical_execution_digest(receipt_payload)
    trigger_id = uuid4()
    effective.execute(
        ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.insert().values(
            id=trigger_id,
            qualification_decision_id=qualification_decision_id,
            deployment_approval_id=deployment_approval_id,
            deployment_id=deployment_id,
            runtime_instance_id=runtime_instance_id,
            runtime_image_acceptance_id=runtime_image_acceptance_id,
            strategy_version_id=qualification["strategy_version_id"],
            research_target_id=qualification["research_target_id"],
            configuration_bundle_id=qualification["configuration_bundle_id"],
            configuration_bundle_digest=qualification["configuration_bundle_digest"],
            market_snapshot_id=qualification["market_snapshot_id"],
            market_snapshot_digest=qualification["market_snapshot_digest"],
            source_kind=SOURCE_KIND,
            execution_target=EXECUTION_TARGET,
            allow_real_funds=False,
            acceptance_only=True,
            position_policy=POSITION_POLICY,
            max_order_count=1,
            scheduled_at=scheduled_at,
            expires_at=expires_at,
            idempotency_key=key,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            created_at=now,
        )
    )
    return AcceptanceSignalTriggerReceipt(
        trigger_id=trigger_id,
        source_kind=SOURCE_KIND,
        scheduled_at=scheduled_at,
        expires_at=expires_at,
        request_digest=request_digest,
        receipt_digest=receipt_digest,
        repeat_noop=False,
    )


def build_acceptance_worker_receipt(
    connection: Connection,
    *,
    trigger_id: UUID,
    plan_digest: str,
    observed_at: datetime,
    signer: ReceiptSignerPort,
) -> RuntimeWorkerReceipt:
    effective = require_canonical_execution(connection)
    now = _utc(observed_at, field="observed_at")
    trigger = effective.execute(
        select(ACCEPTANCE_SIGNAL_TRIGGERS_TABLE).where(
            ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.id == trigger_id
        )
    ).mappings().one_or_none()
    if trigger is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ACCEPTANCE_TRIGGER_NOT_FOUND", str(trigger_id)
        )
    runtime = effective.execute(
        select(RUNTIME_INSTANCES_TABLE).where(
            RUNTIME_INSTANCES_TABLE.c.id == trigger["runtime_instance_id"]
        )
    ).mappings().one_or_none()
    deployment = effective.execute(
        select(DEPLOYMENTS_TABLE).where(
            DEPLOYMENTS_TABLE.c.id == trigger["deployment_id"]
        )
    ).mappings().one_or_none()
    approval = effective.execute(
        select(DEPLOYMENT_APPROVALS_TABLE).where(
            DEPLOYMENT_APPROVALS_TABLE.c.id == trigger["deployment_approval_id"]
        )
    ).mappings().one_or_none()
    qualification = effective.execute(
        select(QUALIFICATION_DECISIONS_TABLE).where(
            QUALIFICATION_DECISIONS_TABLE.c.id == trigger["qualification_decision_id"]
        )
    ).mappings().one_or_none()
    target = effective.execute(
        select(RESEARCH_TARGETS_TABLE).where(
            RESEARCH_TARGETS_TABLE.c.id == trigger["research_target_id"]
        )
    ).mappings().one_or_none()
    runtime_receipt = _latest_runtime_receipt(effective, trigger["runtime_instance_id"])
    runtime_observed_at = (
        _stored_utc(runtime_receipt["observed_at"])
        if runtime_receipt is not None
        else None
    )
    scheduled_at = _stored_utc(trigger["scheduled_at"])
    expires_at = _stored_utc(trigger["expires_at"])
    if (
        now < scheduled_at
        or now >= expires_at
        or runtime is None
        or runtime["status"] != "HEALTHY"
        or runtime["order_writer_capability"] is not False
        or deployment is None
        or deployment["status"] != "ACTIVE"
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or approval is None
        or qualification is None
        or target is None
        or runtime_receipt is None
        or runtime_receipt["status"] != "HEALTHY"
        or runtime_observed_at is None
        or runtime_observed_at > now
        or now - runtime_observed_at > MAXIMUM_RUNTIME_RECEIPT_AGE
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ACCEPTANCE_TRIGGER_NOT_EXECUTABLE",
            "trigger is early, expired, consumed, or its Demo runtime lineage is unhealthy",
        )
    signal_json = {
        "evidence_class": SOURCE_KIND,
        "source_kind": SOURCE_KIND,
        "natural_signal": False,
        "acceptance_only": True,
        "execution_target": EXECUTION_TARGET,
        "allow_real_funds": False,
        "position_policy": POSITION_POLICY,
        "max_order_count": 1,
        "acceptance_trigger_id": str(trigger_id),
        "acceptance_trigger_receipt_digest": trigger["receipt_digest"],
        "qualification_decision_id": str(qualification["id"]),
        "qualification_decision_digest": qualification["decision_digest"],
        "deployment_approval_id": str(approval["id"]),
        "deployment_approval_digest": approval["approval_digest"],
        "deployment_id": str(deployment["id"]),
        "runtime_instance_id": str(runtime["id"]),
        "strategy_version_id": str(trigger["strategy_version_id"]),
        "research_target_id": str(target["id"]),
        "configuration_bundle_id": str(trigger["configuration_bundle_id"]),
        "configuration_bundle_digest": trigger["configuration_bundle_digest"],
        "market_snapshot_id": str(trigger["market_snapshot_id"]),
        "market_snapshot_digest": trigger["market_snapshot_digest"],
        "runtime_receipt_digest": runtime_receipt["receipt_digest"],
        "deployment_capability_digest": deployment["capability_digest"],
        "runtime_launch_spec_digest": runtime["launch_spec_digest"],
        "market_evidence_id": f"acceptance-trigger:{trigger_id}",
        "market_evidence_digest": trigger["receipt_digest"],
        "instrument": target["instrument"],
        "evaluated_at": now.isoformat(),
        "evaluator_identity": "canonical-v13-acceptance-trigger-v1",
        "evaluation": {
            "direction": "LONG",
            "closed_candle": True,
            "scheduled_at": scheduled_at.isoformat(),
            "order_submission_enabled": False,
        },
    }
    candidate_payload = {
        "contract": "canonical-v13-acceptance-signal-candidate-v1",
        **signal_json,
    }
    candidate_digest = runtime_worker_digest(candidate_payload)
    candidate = RuntimeSignalCandidate(
        contract=str(candidate_payload["contract"]),
        qualification_decision_id=qualification["id"],
        qualification_decision_digest=qualification["decision_digest"],
        deployment_approval_id=approval["id"],
        deployment_approval_digest=approval["approval_digest"],
        deployment_id=deployment["id"],
        runtime_instance_id=runtime["id"],
        strategy_version_id=trigger["strategy_version_id"],
        research_target_id=target["id"],
        configuration_bundle_id=trigger["configuration_bundle_id"],
        configuration_bundle_digest=trigger["configuration_bundle_digest"],
        market_snapshot_id=trigger["market_snapshot_id"],
        market_snapshot_digest=trigger["market_snapshot_digest"],
        runtime_receipt_digest=runtime_receipt["receipt_digest"],
        deployment_capability_digest=deployment["capability_digest"],
        runtime_launch_spec_digest=runtime["launch_spec_digest"],
        market_evidence_id=signal_json["market_evidence_id"],
        market_evidence_digest=trigger["receipt_digest"],
        instrument=target["instrument"],
        evaluated_at=now,
        evaluator_identity="canonical-v13-acceptance-trigger-v1",
        signal_json=signal_json,
        candidate_digest=candidate_digest,
    )
    unsigned = {
        "contract": "canonical-v13-runtime-worker-receipt-v1",
        "stage": "SIGNAL_RISK_SHADOW",
        "status": "SIGNAL",
        "reason_code": "ACCEPTANCE_SCHEDULED_TEST_SIGNAL",
        "plan_digest": plan_digest,
        "runtime_instance_id": runtime["id"],
        "runtime_receipt_digest": runtime_receipt["receipt_digest"],
        "observed_at": now,
        "order_submission_enabled": False,
        "persistence_target": "canonical_signal_writer",
        "signal_candidate_digest": candidate_digest,
        "signer_key_id": signer.key_id,
        "signature_algorithm": signer.algorithm,
    }
    worker_receipt_digest = runtime_worker_digest(unsigned)
    return RuntimeWorkerReceipt(
        **unsigned,
        signal_candidate=candidate,
        receipt_digest=worker_receipt_digest,
        signature=signer.sign_digest(worker_receipt_digest),
    )


def persist_acceptance_signal(
    connection: Connection,
    *,
    trigger_id: UUID,
    worker_receipt: RuntimeWorkerReceipt,
    verifier: ReceiptVerifierPort,
    persisted_at: datetime | None = None,
) -> AcceptanceSignalExecutionReceipt:
    effective = require_canonical_execution(connection)
    now = _utc(persisted_at or datetime.now(timezone.utc), field="persisted_at")
    if not verify_runtime_worker_receipt(worker_receipt, verifier=verifier):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ACCEPTANCE_WORKER_RECEIPT", "signed worker receipt failed verification"
        )
    trigger = effective.execute(
        select(ACCEPTANCE_SIGNAL_TRIGGERS_TABLE).where(
            ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.id == trigger_id
        )
    ).mappings().one_or_none()
    candidate = worker_receipt.signal_candidate
    if trigger is None or candidate is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ACCEPTANCE_TRIGGER_NOT_FOUND", str(trigger_id)
        )
    lock_execution_boundary(effective, key=f"acceptance-trigger-consume:{trigger_id}")
    runtime = effective.execute(
        select(RUNTIME_INSTANCES_TABLE).where(
            RUNTIME_INSTANCES_TABLE.c.id == trigger["runtime_instance_id"]
        )
    ).mappings().one_or_none()
    deployment = effective.execute(
        select(DEPLOYMENTS_TABLE).where(
            DEPLOYMENTS_TABLE.c.id == trigger["deployment_id"]
        )
    ).mappings().one_or_none()
    target = effective.execute(
        select(RESEARCH_TARGETS_TABLE).where(
            RESEARCH_TARGETS_TABLE.c.id == trigger["research_target_id"]
        )
    ).mappings().one_or_none()
    runtime_receipt = _latest_runtime_receipt(
        effective, trigger["runtime_instance_id"]
    )
    runtime_observed_at = (
        _stored_utc(runtime_receipt["observed_at"])
        if runtime_receipt is not None
        else None
    )
    observed_at = _utc(worker_receipt.observed_at, field="worker_receipt.observed_at")
    if (
        now < _stored_utc(trigger["scheduled_at"])
        or now >= _stored_utc(trigger["expires_at"])
        or observed_at > now
        or now - observed_at > MAXIMUM_SIGNAL_PERSISTENCE_LAG
        or worker_receipt.contract != "canonical-v13-runtime-worker-receipt-v1"
        or worker_receipt.stage != "SIGNAL_RISK_SHADOW"
        or worker_receipt.status != "SIGNAL"
        or worker_receipt.reason_code != "ACCEPTANCE_SCHEDULED_TEST_SIGNAL"
        or worker_receipt.persistence_target != "canonical_signal_writer"
        or worker_receipt.runtime_instance_id != trigger["runtime_instance_id"]
        or runtime is None
        or runtime["status"] != "HEALTHY"
        or runtime["order_writer_capability"] is not False
        or deployment is None
        or deployment["status"] != "ACTIVE"
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or target is None
        or runtime_receipt is None
        or runtime_receipt["status"] != "HEALTHY"
        or runtime_observed_at is None
        or runtime_observed_at > now
        or now - runtime_observed_at > MAXIMUM_RUNTIME_RECEIPT_AGE
        or worker_receipt.runtime_receipt_digest != runtime_receipt["receipt_digest"]
        or worker_receipt.observed_at != candidate.evaluated_at
        or candidate.contract != "canonical-v13-acceptance-signal-candidate-v1"
        or candidate.qualification_decision_id
        != trigger["qualification_decision_id"]
        or candidate.deployment_approval_id != trigger["deployment_approval_id"]
        or candidate.deployment_id != trigger["deployment_id"]
        or candidate.runtime_instance_id != trigger["runtime_instance_id"]
        or candidate.strategy_version_id != trigger["strategy_version_id"]
        or candidate.research_target_id != trigger["research_target_id"]
        or candidate.configuration_bundle_id
        != trigger["configuration_bundle_id"]
        or candidate.configuration_bundle_digest
        != trigger["configuration_bundle_digest"]
        or candidate.market_snapshot_id != trigger["market_snapshot_id"]
        or candidate.market_snapshot_digest != trigger["market_snapshot_digest"]
        or candidate.runtime_receipt_digest != runtime_receipt["receipt_digest"]
        or candidate.deployment_capability_digest
        != deployment["capability_digest"]
        or candidate.runtime_launch_spec_digest != runtime["launch_spec_digest"]
        or candidate.market_evidence_id != f"acceptance-trigger:{trigger_id}"
        or candidate.market_evidence_digest != trigger["receipt_digest"]
        or candidate.instrument != target["instrument"]
        or candidate.evaluator_identity != "canonical-v13-acceptance-trigger-v1"
        or candidate.signal_json.get("source_kind") != SOURCE_KIND
        or candidate.signal_json.get("natural_signal") is not False
        or candidate.signal_json.get("acceptance_trigger_id") != str(trigger_id)
        or candidate.signal_json.get("acceptance_trigger_receipt_digest")
        != trigger["receipt_digest"]
        or candidate.signal_json.get("execution_target") != EXECUTION_TARGET
        or candidate.signal_json.get("allow_real_funds") is not False
        or candidate.signal_json.get("acceptance_only") is not True
        or candidate.signal_json.get("position_policy") != POSITION_POLICY
        or candidate.signal_json.get("max_order_count") != 1
        or candidate.signal_json.get("evaluation")
        != {
            "direction": "LONG",
            "closed_candle": True,
            "scheduled_at": _stored_utc(trigger["scheduled_at"]).isoformat(),
            "order_submission_enabled": False,
        }
        or worker_receipt.order_submission_enabled is not False
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ACCEPTANCE_SIGNAL_CONTRACT",
            "one-shot acceptance signal receipt or safety envelope drifted",
        )
    signal_digest = canonical_execution_digest(dict(candidate.signal_json))
    existing = effective.execute(
        select(SIGNALS_TABLE).where(SIGNALS_TABLE.c.acceptance_trigger_id == trigger_id)
    ).mappings().one_or_none()
    if existing is not None:
        if (
            existing["source_kind"] != SOURCE_KIND
            or existing["signal_digest"] != signal_digest
            or existing["signal_json"] != dict(candidate.signal_json)
            or existing["worker_receipt_digest"] != worker_receipt.receipt_digest
            or existing["worker_signer_key_id"] != worker_receipt.signer_key_id
            or existing["worker_signature_algorithm"]
            != worker_receipt.signature_algorithm
            or existing["worker_signature"] != worker_receipt.signature
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_ACCEPTANCE_SIGNAL_REPLAY_DRIFT",
                "consumed trigger is bound to different signal evidence",
            )
        return AcceptanceSignalExecutionReceipt(
            trigger_id=trigger_id,
            signal_id=existing["id"],
            source_kind=SOURCE_KIND,
            signal_digest=existing["signal_digest"],
            worker_receipt_digest=existing["worker_receipt_digest"],
            repeat_noop=True,
        )
    signal_id = uuid4()
    effective.execute(
        SIGNALS_TABLE.insert().values(
            id=signal_id,
            deployment_id=trigger["deployment_id"],
            runtime_instance_id=trigger["runtime_instance_id"],
            strategy_version_id=trigger["strategy_version_id"],
            research_target_id=trigger["research_target_id"],
            configuration_bundle_id=trigger["configuration_bundle_id"],
            configuration_bundle_digest=trigger["configuration_bundle_digest"],
            market_snapshot_id=trigger["market_snapshot_id"],
            market_snapshot_digest=trigger["market_snapshot_digest"],
            source_kind=SOURCE_KIND,
            acceptance_trigger_id=trigger_id,
            worker_receipt_digest=worker_receipt.receipt_digest,
            worker_signer_key_id=worker_receipt.signer_key_id,
            worker_signature_algorithm=worker_receipt.signature_algorithm,
            worker_signature=worker_receipt.signature,
            signal_json=dict(candidate.signal_json),
            signal_digest=signal_digest,
            created_at=now,
        )
    )
    return AcceptanceSignalExecutionReceipt(
        trigger_id=trigger_id,
        signal_id=signal_id,
        source_kind=SOURCE_KIND,
        signal_digest=signal_digest,
        worker_receipt_digest=worker_receipt.receipt_digest,
        repeat_noop=False,
    )


def read_acceptance_signal_execution(
    connection: Connection, *, trigger_id: UUID
) -> AcceptanceSignalExecutionReceipt | None:
    effective = require_canonical_execution(connection)
    row = effective.execute(
        select(SIGNALS_TABLE).where(SIGNALS_TABLE.c.acceptance_trigger_id == trigger_id)
    ).mappings().one_or_none()
    if row is None:
        return None
    if (
        row["source_kind"] != SOURCE_KIND
        or row["acceptance_trigger_id"] != trigger_id
        or row["signal_json"].get("source_kind") != SOURCE_KIND
        or row["signal_json"].get("natural_signal") is not False
        or row["signal_json"].get("acceptance_only") is not True
        or row["signal_json"].get("acceptance_trigger_id") != str(trigger_id)
        or canonical_execution_digest(dict(row["signal_json"]))
        != row["signal_digest"]
        or not isinstance(row["worker_receipt_digest"], str)
        or len(row["worker_receipt_digest"]) != 64
        or not isinstance(row["worker_signer_key_id"], str)
        or row["worker_signature_algorithm"] != "HMAC_SHA256_V1"
        or not isinstance(row["worker_signature"], str)
        or len(row["worker_signature"]) != 64
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ACCEPTANCE_SIGNAL_REPLAY_DRIFT",
            "persisted acceptance signal evidence is incomplete",
        )
    return AcceptanceSignalExecutionReceipt(
        trigger_id=trigger_id,
        signal_id=row["id"],
        source_kind=SOURCE_KIND,
        signal_digest=row["signal_digest"],
        worker_receipt_digest=row["worker_receipt_digest"],
        repeat_noop=True,
    )


__all__ = [
    "AcceptanceSignalExecutionReceipt",
    "AcceptanceSignalTriggerReceipt",
    "SOURCE_KIND",
    "build_acceptance_worker_receipt",
    "issue_acceptance_signal_trigger",
    "persist_acceptance_signal",
    "read_acceptance_signal_execution",
]
