"""Signal-writer-only TEST_SIMULATED acceptance service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    lock_execution_boundary,
    require_canonical_execution,
)
from app.canonical_v13.models import (
    DEPLOYMENTS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SIGNALS_TABLE,
)


def record_simulated_signal(
    connection: Connection,
    *,
    deployment_id: UUID,
    runtime_instance_id: UUID,
    research_target_id: UUID,
    signal_json: Mapping[str, object],
) -> UUID:
    effective = require_canonical_execution(connection)
    deployment = (
        effective.execute(
            select(DEPLOYMENTS_TABLE).where(DEPLOYMENTS_TABLE.c.id == deployment_id)
        )
        .mappings()
        .one_or_none()
    )
    runtime = (
        effective.execute(
            select(RUNTIME_INSTANCES_TABLE).where(
                RUNTIME_INSTANCES_TABLE.c.id == runtime_instance_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        deployment is None
        or runtime is None
        or runtime["deployment_id"] != deployment_id
        or runtime["order_writer_capability"] is not False
        or runtime["status"] != "HEALTHY"
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_SIGNAL_RUNTIME_LINEAGE", "runtime/deployment identity failed"
        )
    if signal_json.get("evidence_class") != "TEST_SIMULATED":
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_REAL_SIGNAL_OUT_OF_SCOPE",
            "only isolated signal fixtures are allowed",
        )
    payload = dict(signal_json)
    signal_digest = canonical_execution_digest(payload)
    existing = (
        effective.execute(
            select(SIGNALS_TABLE).where(
                SIGNALS_TABLE.c.runtime_instance_id == runtime_instance_id,
                SIGNALS_TABLE.c.research_target_id == research_target_id,
                SIGNALS_TABLE.c.signal_digest == signal_digest,
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if (
            existing["deployment_id"] != deployment_id
            or existing["strategy_version_id"] != deployment["strategy_version_id"]
            or existing["configuration_bundle_id"]
            != deployment["configuration_bundle_id"]
            or existing["configuration_bundle_digest"]
            != deployment["configuration_bundle_digest"]
            or existing["market_snapshot_id"] != deployment["market_snapshot_id"]
            or existing["market_snapshot_digest"]
            != deployment["market_snapshot_digest"]
            or existing["signal_json"] != payload
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_SIGNAL_REPLAY_DRIFT", "persisted signal lineage differs"
            )
        return existing["id"]
    signal_id = uuid4()
    effective.execute(
        SIGNALS_TABLE.insert().values(
            id=signal_id,
            deployment_id=deployment_id,
            runtime_instance_id=runtime_instance_id,
            strategy_version_id=deployment["strategy_version_id"],
            research_target_id=research_target_id,
            configuration_bundle_id=deployment["configuration_bundle_id"],
            configuration_bundle_digest=deployment["configuration_bundle_digest"],
            market_snapshot_id=deployment["market_snapshot_id"],
            market_snapshot_digest=deployment["market_snapshot_digest"],
            source_kind="TEST_SIMULATED_FIXTURE",
            acceptance_trigger_id=None,
            signal_json=payload,
            signal_digest=signal_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return signal_id


def record_production_demo_signal(
    connection: Connection,
    *,
    deployment_id: UUID,
    runtime_instance_id: UUID,
    research_target_id: UUID,
    signal_json: Mapping[str, object],
    evaluated_at: datetime | None = None,
    maximum_heartbeat_age: timedelta = timedelta(minutes=5),
    runtime_liveness_observed_at: datetime | None = None,
) -> UUID:
    """Persist one natural Demo signal from the healthy non-order runtime."""

    effective = require_canonical_execution(connection)
    now = evaluated_at or datetime.now(timezone.utc)
    if now.tzinfo is None or maximum_heartbeat_age <= timedelta(0):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_SIGNAL_TIME_POLICY", "signal time policy is invalid"
        )
    now = now.astimezone(timezone.utc)
    deployment = (
        effective.execute(
            select(DEPLOYMENTS_TABLE).where(DEPLOYMENTS_TABLE.c.id == deployment_id)
        )
        .mappings()
        .one_or_none()
    )
    runtime = (
        effective.execute(
            select(RUNTIME_INSTANCES_TABLE).where(
                RUNTIME_INSTANCES_TABLE.c.id == runtime_instance_id
            )
        )
        .mappings()
        .one_or_none()
    )
    receipt = (
        effective.execute(
            select(RUNTIME_RECEIPTS_TABLE)
            .where(RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime_instance_id)
            .order_by(RUNTIME_RECEIPTS_TABLE.c.observed_at.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    observed = receipt["observed_at"] if receipt is not None else None
    if observed is not None and observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    liveness_observed = runtime_liveness_observed_at
    if liveness_observed is not None:
        if liveness_observed.tzinfo is None:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_SIGNAL_TIME_POLICY", "runtime liveness must be timezone-aware"
            )
        liveness_observed = liveness_observed.astimezone(timezone.utc)
    payload = dict(signal_json)
    if (
        deployment is None
        or deployment["status"] != "ACTIVE"
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or runtime is None
        or runtime["deployment_id"] != deployment_id
        or runtime["status"] != "HEALTHY"
        or runtime["order_writer_capability"] is not False
        or receipt is None
        or receipt["status"] != "HEALTHY"
        or receipt["evidence_class"] != "PRODUCTION_DEMO_RUNTIME"
        or observed is None
        or observed > now
        or (
            liveness_observed is None
            and now - observed > maximum_heartbeat_age
        )
        or (
            liveness_observed is not None
            and (
                liveness_observed != now
                or payload.get("runtime_receipt_digest")
                != receipt["receipt_digest"]
            )
        )
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_SIGNAL_RUNTIME_LINEAGE",
            "ACTIVE Demo deployment and fresh production runtime receipt are required",
        )
    if (
        payload.get("evidence_class") != "PRODUCTION_OKX_DEMO"
        or payload.get("natural_signal") is not True
        or payload.get("allow_real_funds") is not False
        or payload.get("configuration_bundle_digest")
        != deployment["configuration_bundle_digest"]
        or payload.get("market_snapshot_digest") != deployment["market_snapshot_digest"]
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_SIGNAL_CONTRACT",
            "natural Demo signal must bind the exact frozen deployment lineage",
        )
    signal_digest = canonical_execution_digest(payload)
    lock_execution_boundary(
        effective,
        key=f"production-signal:{runtime_instance_id}:{research_target_id}:{signal_digest}",
    )
    existing = (
        effective.execute(
            select(SIGNALS_TABLE).where(
                SIGNALS_TABLE.c.runtime_instance_id == runtime_instance_id,
                SIGNALS_TABLE.c.research_target_id == research_target_id,
                SIGNALS_TABLE.c.signal_digest == signal_digest,
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if (
            existing["deployment_id"] != deployment_id
            or existing["strategy_version_id"] != deployment["strategy_version_id"]
            or existing["signal_json"] != payload
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_SIGNAL_REPLAY_DRIFT", "persisted production signal differs"
            )
        return existing["id"]
    signal_id = uuid4()
    effective.execute(
        SIGNALS_TABLE.insert().values(
            id=signal_id,
            deployment_id=deployment_id,
            runtime_instance_id=runtime_instance_id,
            strategy_version_id=deployment["strategy_version_id"],
            research_target_id=research_target_id,
            configuration_bundle_id=deployment["configuration_bundle_id"],
            configuration_bundle_digest=deployment["configuration_bundle_digest"],
            market_snapshot_id=deployment["market_snapshot_id"],
            market_snapshot_digest=deployment["market_snapshot_digest"],
            source_kind="NATURAL_STRATEGY_SIGNAL",
            acceptance_trigger_id=None,
            signal_json=payload,
            signal_digest=signal_digest,
            created_at=now,
        )
    )
    return signal_id


__all__ = ["record_production_demo_signal", "record_simulated_signal"]
