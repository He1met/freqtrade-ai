"""Risk-writer-only TEST_SIMULATED intent and decision service."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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

INTENT_MODE_TEST_SIMULATED = "TEST_SIMULATED"
INTENT_MODE_SIGNAL_RISK_SHADOW = "SIGNAL_RISK_SHADOW"
INTENT_MODE_EXECUTION = "EXECUTION"


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
    submitted_payload = dict(intent_json)
    if submitted_payload.get("intent_mode") not in (
        None,
        INTENT_MODE_TEST_SIMULATED,
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_INTENT_MODE",
            "intent payload mode must match the immutable intent mode",
        )
    payload = dict(submitted_payload)
    payload["intent_mode"] = INTENT_MODE_TEST_SIMULATED
    if payload.get("evidence_class") != "TEST_SIMULATED":
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_REAL_INTENT_OUT_OF_SCOPE", "only isolated intents are allowed"
        )
    intent_digest = canonical_execution_digest(payload)
    existing = (
        effective.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.signal_id == signal_id,
                TRADE_INTENTS_TABLE.c.intent_mode == INTENT_MODE_TEST_SIMULATED,
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        replay_payload = (
            submitted_payload
            if "intent_mode" not in existing["intent_json"]
            else payload
        )
        if (
            existing["status"] != "INTENT_ACCEPTED"
            or existing["intent_json"] != replay_payload
            or existing["intent_digest"] != canonical_execution_digest(replay_payload)
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
            intent_mode=INTENT_MODE_TEST_SIMULATED,
            status="INTENT_ACCEPTED",
            intent_json=payload,
            intent_digest=intent_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return intent_id


def create_production_demo_intent(
    connection: Connection,
    *,
    signal_id: UUID,
    intent_json: Mapping[str, object],
    intent_mode: str = INTENT_MODE_SIGNAL_RISK_SHADOW,
) -> UUID:
    """Create the exact Demo intent consumed by the central budget authority."""

    effective = require_canonical_execution(connection)
    signal = (
        effective.execute(select(SIGNALS_TABLE).where(SIGNALS_TABLE.c.id == signal_id))
        .mappings()
        .one_or_none()
    )
    production_natural = bool(
        signal
        and signal["source_kind"] == "NATURAL_STRATEGY_SIGNAL"
        and signal["acceptance_trigger_id"] is None
        and signal["signal_json"].get("evidence_class") == "PRODUCTION_OKX_DEMO"
        and signal["signal_json"].get("natural_signal") is True
    )
    acceptance_test = bool(
        signal
        and signal["source_kind"] == "ACCEPTANCE_SCHEDULED_TEST"
        and signal["acceptance_trigger_id"] is not None
        and signal["signal_json"].get("evidence_class")
        == "ACCEPTANCE_SCHEDULED_TEST"
        and signal["signal_json"].get("acceptance_only") is True
        and signal["signal_json"].get("natural_signal") is False
    )
    if signal is None or not (production_natural or acceptance_test):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_SIGNAL_REQUIRED", str(signal_id)
        )
    submitted_payload = dict(intent_json)
    if intent_mode not in {
        INTENT_MODE_SIGNAL_RISK_SHADOW,
        INTENT_MODE_EXECUTION,
    }:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_INTENT_MODE",
            "production intent_mode must be SIGNAL_RISK_SHADOW or EXECUTION",
        )
    if submitted_payload.get("intent_mode") not in (None, intent_mode):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_INTENT_MODE",
            "intent payload mode must match the immutable intent mode",
        )
    payload = dict(submitted_payload)
    payload["intent_mode"] = intent_mode
    exchange_body = payload.get("exchange_body")
    try:
        notional = Decimal(str(payload.get("notional")))
    except InvalidOperation:
        notional = Decimal("0")
    if (
        payload.get("contract") != "canonical-v13-demo-trade-intent-v1"
        or payload.get("execution_target") != "OKX_DEMO"
        or payload.get("allow_real_funds") is not False
        or payload.get("signal_digest") != signal["signal_digest"]
        or (
            acceptance_test
            and (
                payload.get("source_kind") != "ACCEPTANCE_SCHEDULED_TEST"
                or payload.get("acceptance_only") is not True
            )
        )
        or not isinstance(payload.get("instrument"), str)
        or not isinstance(payload.get("notional"), str)
        or not notional.is_finite()
        or notional <= 0
        or not isinstance(exchange_body, Mapping)
        or exchange_body.get("instId") != payload.get("instrument")
        or exchange_body.get("tdMode") != "isolated"
        or exchange_body.get("side") != "buy"
        or exchange_body.get("posSide") != "long"
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_INTENT_CONTRACT",
            "Demo intent must bind its signal, positive notional, and long-only order",
        )
    intent_digest = canonical_execution_digest(payload)
    lock_execution_boundary(
        effective, key=f"production-intent:{signal_id}:{intent_mode}"
    )
    existing = (
        effective.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.signal_id == signal_id,
                TRADE_INTENTS_TABLE.c.intent_mode == intent_mode,
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        replay_payload = (
            submitted_payload
            if "intent_mode" not in existing["intent_json"]
            else payload
        )
        if (
            existing["status"] != "INTENT_ACCEPTED"
            or existing["intent_json"] != replay_payload
            or existing["intent_digest"] != canonical_execution_digest(replay_payload)
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
            intent_mode=intent_mode,
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
                TRADE_INTENTS_TABLE.c.intent_mode == INTENT_MODE_TEST_SIMULATED,
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
        "decision_mode": INTENT_MODE_TEST_SIMULATED,
        "policy_snapshot_digest": policy_snapshot_digest,
        "accepted": accepted,
    }
    decision_digest = canonical_execution_digest(payload)
    existing = (
        effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.trade_intent_id == trade_intent_id,
                RISK_DECISIONS_TABLE.c.decision_mode == INTENT_MODE_TEST_SIMULATED,
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        expected_status = "RISK_ACCEPTED" if accepted else "REJECTED"
        replay_payload = (
            {key: value for key, value in payload.items() if key != "decision_mode"}
            if "decision_mode" not in existing["decision_json"]
            else payload
        )
        if (
            existing["status"] != expected_status
            or existing["decision_json"] != replay_payload
            or existing["decision_digest"]
            != canonical_execution_digest(replay_payload)
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
            decision_mode=INTENT_MODE_TEST_SIMULATED,
            status="RISK_ACCEPTED" if accepted else "REJECTED",
            decision_json=payload,
            decision_digest=decision_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return decision_id


__all__ = [
    "INTENT_MODE_EXECUTION",
    "INTENT_MODE_SIGNAL_RISK_SHADOW",
    "INTENT_MODE_TEST_SIMULATED",
    "create_production_demo_intent",
    "create_simulated_intent",
    "decide_simulated_risk",
]
