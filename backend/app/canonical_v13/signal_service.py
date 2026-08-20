"""Signal-writer-only TEST_SIMULATED acceptance service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    require_canonical_execution,
)
from app.canonical_v13.models import DEPLOYMENTS_TABLE, RUNTIME_INSTANCES_TABLE, SIGNALS_TABLE


def record_simulated_signal(
    connection: Connection,
    *,
    deployment_id: UUID,
    runtime_instance_id: UUID,
    research_target_id: UUID,
    signal_json: Mapping[str, object],
) -> UUID:
    effective = require_canonical_execution(connection)
    deployment = effective.execute(
        select(DEPLOYMENTS_TABLE).where(DEPLOYMENTS_TABLE.c.id == deployment_id)
    ).mappings().one_or_none()
    runtime = effective.execute(
        select(RUNTIME_INSTANCES_TABLE).where(
            RUNTIME_INSTANCES_TABLE.c.id == runtime_instance_id
        )
    ).mappings().one_or_none()
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
            "BLOCKED_REAL_SIGNAL_OUT_OF_SCOPE", "only isolated signal fixtures are allowed"
        )
    payload = dict(signal_json)
    signal_digest = canonical_execution_digest(payload)
    existing = effective.execute(
        select(SIGNALS_TABLE).where(
            SIGNALS_TABLE.c.runtime_instance_id == runtime_instance_id,
            SIGNALS_TABLE.c.research_target_id == research_target_id,
            SIGNALS_TABLE.c.signal_digest == signal_digest,
        )
    ).mappings().one_or_none()
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
            signal_json=payload,
            signal_digest=signal_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return signal_id


__all__ = ["record_simulated_signal"]
