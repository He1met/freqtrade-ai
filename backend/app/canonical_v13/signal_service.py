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
    candle_identity = _natural_signal_candle_identity(payload)
    if candle_identity is not None:
        lock_execution_boundary(
            effective,
            key=(
                f"production-signal-candle:{deployment_id}:"
                f"{research_target_id}:{candle_identity}"
            ),
        )
        candle_matches = [
            row
            for row in (
                effective.execute(
                    select(SIGNALS_TABLE)
                    .where(
                        SIGNALS_TABLE.c.deployment_id == deployment_id,
                        SIGNALS_TABLE.c.research_target_id == research_target_id,
                        SIGNALS_TABLE.c.source_kind == "NATURAL_STRATEGY_SIGNAL",
                    )
                    .order_by(SIGNALS_TABLE.c.created_at, SIGNALS_TABLE.c.id)
                )
                .mappings()
                .all()
            )
            if _natural_signal_candle_identity(row["signal_json"])
            == candle_identity
        ]
        if candle_matches:
            stable_projection = _natural_signal_stable_projection(payload)
            if any(
                row["strategy_version_id"] != deployment["strategy_version_id"]
                or row["configuration_bundle_id"]
                != deployment["configuration_bundle_id"]
                or row["configuration_bundle_digest"]
                != deployment["configuration_bundle_digest"]
                or row["market_snapshot_id"] != deployment["market_snapshot_id"]
                or row["market_snapshot_digest"]
                != deployment["market_snapshot_digest"]
                or _natural_signal_stable_projection(row["signal_json"])
                != stable_projection
                for row in candle_matches
            ):
                raise CanonicalExecutionChainBlocked(
                    "BLOCKED_SIGNAL_REPLAY_DRIFT",
                    "persisted natural candle signal semantics differ",
                )
            # Historical releases could append the same closed-candle signal on
            # every heartbeat because evaluated_at and market evidence changed.
            # Preserve those immutable rows, but converge every later replay on
            # the earliest exact semantic signal.
            return candle_matches[0]["id"]
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


def _natural_signal_candle_identity(payload: Mapping[str, object]) -> str | None:
    evaluation = payload.get("evaluation")
    if not isinstance(evaluation, Mapping):
        return None
    instrument = payload.get("instrument")
    candle_opened_at = evaluation.get("candle_opened_at")
    if (
        payload.get("natural_signal") is not True
        or evaluation.get("closed_candle") is not True
        or not isinstance(instrument, str)
        or not instrument
        or not isinstance(candle_opened_at, str)
        or not candle_opened_at
    ):
        return None
    return canonical_execution_digest(
        {
            "contract": "canonical-v13-natural-signal-candle-identity-v1",
            "instrument": instrument,
            "candle_opened_at": candle_opened_at,
        }
    )


def _natural_signal_stable_projection(
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    evaluation = payload.get("evaluation")
    evaluation_mapping = evaluation if isinstance(evaluation, Mapping) else {}
    return {
        "qualification_decision_id": payload.get("qualification_decision_id"),
        "qualification_decision_digest": payload.get(
            "qualification_decision_digest"
        ),
        "deployment_approval_id": payload.get("deployment_approval_id"),
        "deployment_approval_digest": payload.get("deployment_approval_digest"),
        "deployment_id": payload.get("deployment_id"),
        "strategy_version_id": payload.get("strategy_version_id"),
        "research_target_id": payload.get("research_target_id"),
        "configuration_bundle_id": payload.get("configuration_bundle_id"),
        "configuration_bundle_digest": payload.get(
            "configuration_bundle_digest"
        ),
        "market_snapshot_id": payload.get("market_snapshot_id"),
        "market_snapshot_digest": payload.get("market_snapshot_digest"),
        "instrument": payload.get("instrument"),
        "evaluator_identity": payload.get("evaluator_identity"),
        "evaluation": {
            field: evaluation_mapping.get(field)
            for field in (
                "direction",
                "closed_candle",
                "candle_opened_at",
                "volume",
                "effective_strategy_leverage",
                "artifact_digest",
            )
        },
    }


__all__ = ["record_production_demo_signal", "record_simulated_signal"]
