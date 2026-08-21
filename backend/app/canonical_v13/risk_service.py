"""Risk-writer-only TEST_SIMULATED intent and decision service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    lock_execution_boundary,
    require_canonical_execution,
    require_digest,
)
from app.canonical_v13.models import (
    RISK_DECISIONS_TABLE,
    SIGNALS_TABLE,
    TRADE_INTENTS_TABLE,
)


def create_simulated_intent(
    connection: Connection, *, signal_id: UUID, intent_json: Mapping[str, object]
) -> UUID:
    effective = require_canonical_execution(connection)
    if (
        effective.execute(
            select(SIGNALS_TABLE.c.id).where(SIGNALS_TABLE.c.id == signal_id)
        ).scalar_one_or_none()
        is None
    ):
        raise CanonicalExecutionChainBlocked("BLOCKED_SIGNAL_UNSET", str(signal_id))
    payload = dict(intent_json)
    if payload.get("evidence_class") != "TEST_SIMULATED":
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_REAL_INTENT_OUT_OF_SCOPE", "only isolated intents are allowed"
        )
    intent_digest = canonical_execution_digest(payload)
    existing = (
        effective.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.signal_id == signal_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if (
            existing["status"] != "INTENT_ACCEPTED"
            or existing["intent_json"] != payload
            or existing["intent_digest"] != intent_digest
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_INTENT_REPLAY_DRIFT", "persisted intent differs"
            )
        return existing["id"]
    intent_id = uuid4()
    effective.execute(
        TRADE_INTENTS_TABLE.insert().values(
            id=intent_id,
            signal_id=signal_id,
            status="INTENT_ACCEPTED",
            intent_json=payload,
            intent_digest=intent_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return intent_id


def create_production_demo_intent(
    connection: Connection, *, signal_id: UUID, intent_json: Mapping[str, object]
) -> UUID:
    """Create the exact Demo intent consumed by the central budget authority."""

    effective = require_canonical_execution(connection)
    signal = (
        effective.execute(select(SIGNALS_TABLE).where(SIGNALS_TABLE.c.id == signal_id))
        .mappings()
        .one_or_none()
    )
    if (
        signal is None
        or signal["signal_json"].get("evidence_class") != "PRODUCTION_OKX_DEMO"
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_SIGNAL_REQUIRED", str(signal_id)
        )
    payload = dict(intent_json)
    if (
        payload.get("contract") != "canonical-v13-demo-trade-intent-v1"
        or payload.get("execution_target") != "OKX_DEMO"
        or payload.get("allow_real_funds") is not False
        or payload.get("signal_digest") != signal["signal_digest"]
        or not isinstance(payload.get("instrument"), str)
        or not isinstance(payload.get("notional"), str)
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_INTENT_CONTRACT",
            "Demo intent must bind its source signal and explicit notional",
        )
    intent_digest = canonical_execution_digest(payload)
    lock_execution_boundary(effective, key=f"production-intent:{signal_id}")
    existing = (
        effective.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.signal_id == signal_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if (
            existing["status"] != "INTENT_ACCEPTED"
            or existing["intent_json"] != payload
            or existing["intent_digest"] != intent_digest
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_INTENT_REPLAY_DRIFT", "persisted production intent differs"
            )
        return existing["id"]
    intent_id = uuid4()
    effective.execute(
        TRADE_INTENTS_TABLE.insert().values(
            id=intent_id,
            signal_id=signal_id,
            status="INTENT_ACCEPTED",
            intent_json=payload,
            intent_digest=intent_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return intent_id


def decide_simulated_risk(
    connection: Connection,
    *,
    trade_intent_id: UUID,
    accepted: bool,
    policy_snapshot_digest: str,
) -> UUID:
    effective = require_canonical_execution(connection)
    require_digest(policy_snapshot_digest, field="policy_snapshot_digest")
    if (
        effective.execute(
            select(TRADE_INTENTS_TABLE.c.id).where(
                TRADE_INTENTS_TABLE.c.id == trade_intent_id,
                TRADE_INTENTS_TABLE.c.status == "INTENT_ACCEPTED",
            )
        ).scalar_one_or_none()
        is None
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_INTENT_UNSET", str(trade_intent_id)
        )
    payload = {
        "contract": "canonical-v13-simulated-risk-v1",
        "evidence_class": "TEST_SIMULATED",
        "policy_snapshot_digest": policy_snapshot_digest,
        "accepted": accepted,
    }
    decision_digest = canonical_execution_digest(payload)
    existing = (
        effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.trade_intent_id == trade_intent_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        expected_status = "RISK_ACCEPTED" if accepted else "REJECTED"
        if (
            existing["status"] != expected_status
            or existing["decision_json"] != payload
            or existing["decision_digest"] != decision_digest
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_RISK_REPLAY_DRIFT", "persisted risk decision differs"
            )
        return existing["id"]
    decision_id = uuid4()
    effective.execute(
        RISK_DECISIONS_TABLE.insert().values(
            id=decision_id,
            trade_intent_id=trade_intent_id,
            status="RISK_ACCEPTED" if accepted else "REJECTED",
            decision_json=payload,
            decision_digest=decision_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return decision_id


__all__ = [
    "create_production_demo_intent",
    "create_simulated_intent",
    "decide_simulated_risk",
]
