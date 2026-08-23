"""Canonical Phase 9 budget, attestation, and central-risk authorities.

The module has no exchange transport and never reads credential material.  It stores
only an operator-authorized Demo budget and redacted attestation digests, then makes
one atomic risk decision from the remaining budget.  Order submission is a separate
service and capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, func, select, text

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    lock_execution_boundary,
    require_canonical_execution,
    require_digest,
    require_identity,
)
from app.canonical_v13.models import (
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
    RISK_DECISIONS_TABLE,
    SIGNALS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.risk_service import (
    INTENT_MODE_EXECUTION,
    INTENT_MODE_SIGNAL_RISK_SHADOW,
)


EXECUTION_TARGET = "OKX_DEMO"
ATTESTATION_MAXIMUM_TTL = timedelta(seconds=60)
ACCEPTANCE_SIGNAL_SOURCE_KIND = "ACCEPTANCE_SCHEDULED_TEST"


@dataclass(frozen=True)
class RiskBudgetAuthorizationResult:
    authorization_id: UUID
    authorization_digest: str
    repeat_noop: bool


@dataclass(frozen=True)
class RedactedExecutionAttestationResult:
    attestation_id: UUID
    attestation_digest: str
    expires_at: datetime
    repeat_noop: bool


@dataclass(frozen=True)
class CentralRiskDecisionResult:
    risk_decision_id: UUID
    reservation_id: UUID
    status: str
    reason_code: str
    decision_digest: str
    repeat_noop: bool


@dataclass(frozen=True)
class ShadowRiskDecisionResult:
    risk_decision_id: UUID
    status: str
    reason_code: str
    decision_digest: str
    repeat_noop: bool


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_EXECUTION_TIMEZONE", f"{field} must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _persisted_utc(value: datetime, *, field: str) -> datetime:
    """Normalize a DB timestamp; SQLite fixtures discard timezone metadata."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_decimal(value: Decimal, *, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_EXECUTION_AMOUNT", f"{field} must be finite and positive"
        )
    return value


def shadow_signal_source_accepted(signal: Mapping[str, object]) -> bool:
    evidence = signal.get("signal_json")
    if not isinstance(evidence, Mapping):
        return False
    if (
        evidence.get("evidence_class") == "PRODUCTION_OKX_DEMO"
        and evidence.get("natural_signal") is True
    ):
        return True
    evaluation = evidence.get("evaluation")
    trigger_id = evidence.get("acceptance_trigger_id")
    return bool(
        signal.get("source_kind") == ACCEPTANCE_SIGNAL_SOURCE_KIND
        and signal.get("acceptance_trigger_id") is not None
        and str(signal["acceptance_trigger_id"]) == trigger_id
        and isinstance(signal.get("worker_receipt_digest"), str)
        and evidence.get("evidence_class") == ACCEPTANCE_SIGNAL_SOURCE_KIND
        and evidence.get("source_kind") == ACCEPTANCE_SIGNAL_SOURCE_KIND
        and evidence.get("natural_signal") is False
        and evidence.get("acceptance_only") is True
        and evidence.get("execution_target") == EXECUTION_TARGET
        and evidence.get("allow_real_funds") is False
        and evidence.get("position_policy") == "LONG_ONLY"
        and evidence.get("max_order_count") == 1
        and isinstance(evaluation, Mapping)
        and evaluation.get("direction") == "LONG"
        and evaluation.get("closed_candle") is True
        and evaluation.get("order_submission_enabled") is False
    )


def authorize_demo_risk_budget(
    connection: Connection,
    *,
    deployment_approval_id: UUID,
    actor_identity: str,
    reason: str,
    policy_source_receipt_digest: str,
    evaluated_at: datetime | None = None,
) -> RiskBudgetAuthorizationResult:
    """Freeze only an existing exact-lineage canonical risk-policy receipt."""

    effective = require_canonical_execution(connection)
    actor_identity = require_identity(
        actor_identity, field="actor_identity", maximum=160
    )
    reason = require_identity(reason, field="reason", maximum=2000)
    require_digest(policy_source_receipt_digest, field="policy_source_receipt_digest")
    now = _utc(evaluated_at or datetime.now(timezone.utc), field="evaluated_at")
    approval = (
        effective.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id == deployment_approval_id
            )
        )
        .mappings()
        .one_or_none()
    )
    decision = (
        effective.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == approval["qualification_decision_id"]
            )
        )
        .mappings()
        .one_or_none()
        if approval is not None
        else None
    )
    if (
        approval is None
        or approval["status"] != "APPROVED"
        or decision is None
        or decision["status"] != "QUALIFIED"
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_APPROVED_QUALIFICATION_REQUIRED",
            "risk budget requires one exact approved QUALIFIED lineage",
        )
    source = (
        effective.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.qualification_decision_id
                == decision["id"],
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.deployment_approval_id
                == deployment_approval_id,
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.receipt_digest
                == policy_source_receipt_digest,
            )
        )
        .mappings()
        .one_or_none()
    )
    if source is None:
        raise CanonicalExecutionChainBlocked(
            "CANONICAL_PHASE9_RISK_BUDGET_SOURCE_UNSET",
            "an accepted exact-lineage canonical risk-policy receipt is required",
        )
    instrument = require_identity(source["instrument"], field="instrument", maximum=80)
    max_notional = _positive_decimal(
        Decimal(str(source["max_notional"])), field="max_notional"
    )
    max_order_count = source["max_order_count"]
    policy_digest = source["policy_digest"]
    expiry = _persisted_utc(source["expires_at"], field="expires_at")
    accepted_at = _persisted_utc(source["accepted_at"], field="accepted_at")
    require_digest(policy_digest, field="policy_digest")
    if (
        source["qualification_decision_id"] != decision["id"]
        or source["strategy_version_id"] != decision["strategy_version_id"]
        or source["research_target_id"] != decision["research_target_id"]
        or source["configuration_bundle_id"] != decision["configuration_bundle_id"]
        or source["configuration_bundle_digest"]
        != decision["configuration_bundle_digest"]
        or source["market_snapshot_id"] != decision["market_snapshot_id"]
        or source["market_snapshot_digest"] != decision["market_snapshot_digest"]
        or source["execution_target"] != EXECUTION_TARGET
        or source["allow_real_funds"] is not False
        or source["position_policy"] != "LONG_ONLY"
        or source["status"] != "ACTIVE"
        or source["receipt_digest"] != policy_source_receipt_digest
        or Decimal(str(source["strategy_max_leverage"])) <= 0
        or source["effective_leverage"]
        > min(source["strategy_max_leverage"], source["exchange_max_leverage"])
        or expiry - accepted_at != timedelta(minutes=30)
        or not isinstance(max_order_count, int)
        or isinstance(max_order_count, bool)
        or max_order_count != 1
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_RISK_POLICY_SOURCE_DRIFT",
            "risk-policy receipt does not bind the exact safe lineage",
        )
    if expiry <= now:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_RISK_BUDGET_EXPIRED", "risk-policy source has expired"
        )
    payload = {
        "contract": "canonical-v13-demo-risk-budget-authorization-v1",
        "execution_canary_risk_policy_id": str(source["id"]),
        "deployment_approval_id": str(deployment_approval_id),
        "approval_digest": approval["approval_digest"],
        "qualification_decision_id": str(decision["id"]),
        "qualification_decision_digest": decision["decision_digest"],
        "execution_target": EXECUTION_TARGET,
        "instrument": instrument,
        "max_notional": str(max_notional),
        "max_order_count": max_order_count,
        "position_policy": source["position_policy"],
        "strategy_max_leverage": str(source["strategy_max_leverage"]),
        "effective_leverage": str(source["effective_leverage"]),
        "actor_identity": actor_identity,
        "reason": reason,
        "policy_digest": policy_digest,
        "source_receipt_digest": policy_source_receipt_digest,
        "expires_at": expiry.isoformat(),
        "allow_real_funds": False,
    }
    digest = canonical_execution_digest(payload)
    lock_execution_boundary(
        effective, key=f"risk-budget-authorization:{deployment_approval_id}"
    )
    existing = (
        effective.execute(
            select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE).where(
                EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.deployment_approval_id
                == deployment_approval_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["authorization_digest"] != digest:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_RISK_BUDGET_REPLAY_DRIFT",
                "deployment approval already has a different frozen risk budget",
            )
        return RiskBudgetAuthorizationResult(existing["id"], digest, True)
    authorization_id = uuid4()
    effective.execute(
        EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.insert().values(
            id=authorization_id,
            execution_canary_risk_policy_id=source["id"],
            deployment_approval_id=deployment_approval_id,
            execution_target=EXECUTION_TARGET,
            instrument=instrument,
            max_notional=max_notional,
            max_order_count=max_order_count,
            actor_identity=actor_identity,
            reason=reason,
            policy_digest=policy_digest,
            source_receipt_digest=policy_source_receipt_digest,
            authorization_digest=digest,
            expires_at=expiry,
            created_at=now,
        )
    )
    return RiskBudgetAuthorizationResult(authorization_id, digest, False)


def record_redacted_demo_attestation(
    connection: Connection,
    *,
    deployment_id: UUID,
    instrument: str,
    account_fingerprint_digest: str,
    credential_generation_digest: str,
    permissions: Mapping[str, bool],
    observed_at: datetime,
    expires_at: datetime,
    evaluated_at: datetime | None = None,
) -> RedactedExecutionAttestationResult:
    """Persist safe attestation facts; raw credentials are not accepted as input."""

    effective = require_canonical_execution(connection)
    instrument = require_identity(instrument, field="instrument", maximum=80)
    require_digest(account_fingerprint_digest, field="account_fingerprint_digest")
    require_digest(credential_generation_digest, field="credential_generation_digest")
    observed = _utc(observed_at, field="observed_at")
    expiry = _utc(expires_at, field="expires_at")
    now = _utc(evaluated_at or datetime.now(timezone.utc), field="evaluated_at")
    observation_age = now - observed
    if (
        expiry <= observed
        or expiry - observed > ATTESTATION_MAXIMUM_TTL
        or not -timedelta(seconds=5) <= observation_age <= timedelta(seconds=15)
        or expiry <= now
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ATTESTATION_FRESHNESS",
            "attestation must be current and expire within 60 seconds",
        )
    safe_permissions = dict(permissions)
    if safe_permissions != {"read": True, "trade": True, "withdraw": False}:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ATTESTATION_PERMISSIONS",
            "permissions must be exactly read=true, trade=true, withdraw=false",
        )
    deployment = (
        effective.execute(
            select(DEPLOYMENTS_TABLE).where(DEPLOYMENTS_TABLE.c.id == deployment_id)
        )
        .mappings()
        .one_or_none()
    )
    if (
        deployment is None
        or deployment["status"] != "ACTIVE"
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ACTIVE_DEMO_DEPLOYMENT_REQUIRED", str(deployment_id)
        )
    payload = {
        "contract": "canonical-v13-redacted-okx-demo-attestation-v1",
        "deployment_id": str(deployment_id),
        "deployment_capability_digest": deployment["capability_digest"],
        "execution_target": EXECUTION_TARGET,
        "instrument": instrument,
        "account_fingerprint_digest": account_fingerprint_digest,
        "credential_generation_digest": credential_generation_digest,
        "permissions": safe_permissions,
        "simulated_trading": True,
        "allow_real_funds": False,
        "observed_at": observed.isoformat(),
        "expires_at": expiry.isoformat(),
    }
    digest = canonical_execution_digest(payload)
    lock_execution_boundary(effective, key=f"execution-attestation:{digest}")
    existing = (
        effective.execute(
            select(EXECUTION_ATTESTATIONS_TABLE).where(
                EXECUTION_ATTESTATIONS_TABLE.c.attestation_digest == digest
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        return RedactedExecutionAttestationResult(
            existing["id"],
            digest,
            _persisted_utc(existing["expires_at"], field="expires_at"),
            True,
        )
    attestation_id = uuid4()
    effective.execute(
        EXECUTION_ATTESTATIONS_TABLE.insert().values(
            id=attestation_id,
            deployment_id=deployment_id,
            execution_target=EXECUTION_TARGET,
            instrument=instrument,
            status="READY",
            account_fingerprint_digest=account_fingerprint_digest,
            credential_generation_digest=credential_generation_digest,
            permissions_json=safe_permissions,
            attestation_digest=digest,
            observed_at=observed,
            expires_at=expiry,
        )
    )
    return RedactedExecutionAttestationResult(attestation_id, digest, expiry, False)


def decide_central_demo_risk(
    connection: Connection,
    *,
    trade_intent_id: UUID,
    risk_budget_authorization_id: UUID,
    evaluated_at: datetime | None = None,
) -> CentralRiskDecisionResult:
    """Atomically derive ACCEPTED/REJECTED from the remaining frozen budget."""

    effective = require_canonical_execution(connection)
    now = _utc(evaluated_at or datetime.now(timezone.utc), field="evaluated_at")
    lock_execution_boundary(effective, key=f"central-risk-intent:{trade_intent_id}")
    existing = (
        effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.trade_intent_id == trade_intent_id,
                RISK_DECISIONS_TABLE.c.decision_mode == INTENT_MODE_EXECUTION,
            )
        )
        .mappings()
        .one_or_none()
    )
    existing_reservation = (
        effective.execute(
            select(EXECUTION_RISK_RESERVATIONS_TABLE).where(
                EXECUTION_RISK_RESERVATIONS_TABLE.c.trade_intent_id == trade_intent_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None or existing_reservation is not None:
        if existing is None or existing_reservation is None:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_RISK_REPLAY_INCOMPLETE",
                "decision/reservation pair is incomplete",
            )
        if (
            existing["decision_json"].get("reservation_digest")
            != existing_reservation["reservation_digest"]
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_RISK_REPLAY_DRIFT", "decision/reservation digest differs"
            )
        return CentralRiskDecisionResult(
            existing["id"],
            existing_reservation["id"],
            existing["status"],
            existing_reservation["reason_code"],
            existing["decision_digest"],
            True,
        )
    intent = (
        effective.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.id == trade_intent_id,
                TRADE_INTENTS_TABLE.c.status == "INTENT_ACCEPTED",
                TRADE_INTENTS_TABLE.c.intent_mode == INTENT_MODE_EXECUTION,
            )
        )
        .mappings()
        .one_or_none()
    )
    signal = (
        effective.execute(
            select(SIGNALS_TABLE).where(SIGNALS_TABLE.c.id == intent["signal_id"])
        )
        .mappings()
        .one_or_none()
        if intent is not None
        else None
    )
    budget = (
        effective.execute(
            select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE).where(
                EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.id
                == risk_budget_authorization_id
            )
        )
        .mappings()
        .one_or_none()
    )
    policy = (
        effective.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id
                == budget["execution_canary_risk_policy_id"]
            )
        )
        .mappings()
        .one_or_none()
        if budget is not None
        else None
    )
    deployment = (
        effective.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == signal["deployment_id"]
            )
        )
        .mappings()
        .one_or_none()
        if signal is not None
        else None
    )
    if (
        intent is None
        or signal is None
        or budget is None
        or policy is None
        or deployment is None
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_RISK_LINEAGE_INCOMPLETE",
            "intent/signal/budget/deployment is missing",
        )
    if (
        deployment["status"] != "ACTIVE"
        or deployment["deployment_approval_id"] != budget["deployment_approval_id"]
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or budget["execution_target"] != EXECUTION_TARGET
        or policy["status"] != "ACTIVE"
        or policy["deployment_approval_id"] != budget["deployment_approval_id"]
        or policy["receipt_digest"] != budget["source_receipt_digest"]
        or policy["position_policy"] != "LONG_ONLY"
        or policy["max_order_count"] != 1
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_RISK_LINEAGE_DRIFT",
            "budget is not bound to active Demo deployment",
        )
    requested_instrument = intent["intent_json"].get("instrument")
    exchange_body = intent["intent_json"].get("exchange_body")
    if not isinstance(exchange_body, Mapping):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_INTENT_EXCHANGE_BODY", "intent exchange_body is missing or invalid"
        )
    try:
        declared_notional = Decimal(str(intent["intent_json"].get("notional")))
        requested_size = Decimal(str(exchange_body.get("sz")))
        requested_price = Decimal(str(exchange_body.get("px")))
    except Exception:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_INTENT_NOTIONAL", "intent notional or exchange size is invalid"
        ) from None
    _positive_decimal(declared_notional, field="intent.notional")
    _positive_decimal(requested_size, field="exchange_body.sz")
    _positive_decimal(requested_price, field="exchange_body.px")
    requested_notional = (
        requested_size
        * Decimal(str(policy["contract_value"]))
        * Decimal(str(policy["mark_price"]))
    )
    if effective.dialect.name == "postgresql":
        effective.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"canonical-v13-risk-budget:{risk_budget_authorization_id}"},
        )
    used_notional, used_count = effective.execute(
        select(
            func.coalesce(
                func.sum(EXECUTION_RISK_RESERVATIONS_TABLE.c.requested_notional), 0
            ),
            func.count(),
        ).where(
            EXECUTION_RISK_RESERVATIONS_TABLE.c.risk_budget_authorization_id
            == risk_budget_authorization_id,
            EXECUTION_RISK_RESERVATIONS_TABLE.c.status == "RISK_ACCEPTED",
        )
    ).one()
    remaining_notional = Decimal(str(budget["max_notional"])) - Decimal(
        str(used_notional)
    )
    remaining_count = int(budget["max_order_count"]) - int(used_count)
    if _persisted_utc(budget["expires_at"], field="budget.expires_at") <= now:
        status, reason_code = "BLOCKED", "RISK_BUDGET_EXPIRED"
    elif (
        exchange_body.get("instId") != requested_instrument
        or exchange_body.get("tdMode") != "isolated"
        or exchange_body.get("side") != "buy"
        or exchange_body.get("posSide") != "long"
        or exchange_body.get("ordType") != "limit"
    ):
        status, reason_code = "REJECTED", "RISK_LONG_ONLY_POLICY"
    elif requested_instrument != budget["instrument"]:
        status, reason_code = "REJECTED", "RISK_INSTRUMENT_NOT_AUTHORIZED"
    elif declared_notional != requested_notional:
        status, reason_code = "REJECTED", "RISK_NOTIONAL_DECLARATION_DRIFT"
    elif requested_size != Decimal(str(policy["minimum_contract_size"])):
        status, reason_code = "REJECTED", "RISK_MINIMUM_CONTRACT_SIZE_REQUIRED"
    elif requested_price != Decimal(str(policy["limit_price"])):
        status, reason_code = "REJECTED", "RISK_FROZEN_LIMIT_PRICE_REQUIRED"
    elif requested_notional > remaining_notional:
        status, reason_code = "REJECTED", "RISK_NOTIONAL_BUDGET_EXHAUSTED"
    elif remaining_count <= 0:
        status, reason_code = "REJECTED", "RISK_ORDER_COUNT_BUDGET_EXHAUSTED"
    else:
        status, reason_code = "RISK_ACCEPTED", "RISK_BUDGET_RESERVED"
    reservation_payload = {
        "contract": "canonical-v13-demo-risk-reservation-v1",
        "risk_budget_authorization_id": str(risk_budget_authorization_id),
        "authorization_digest": budget["authorization_digest"],
        "trade_intent_id": str(trade_intent_id),
        "intent_digest": intent["intent_digest"],
        "requested_notional": str(requested_notional),
        "status": status,
        "reason_code": reason_code,
    }
    reservation_digest = canonical_execution_digest(reservation_payload)
    reservation_id = uuid4()
    effective.execute(
        EXECUTION_RISK_RESERVATIONS_TABLE.insert().values(
            id=reservation_id,
            risk_budget_authorization_id=risk_budget_authorization_id,
            trade_intent_id=trade_intent_id,
            status=status,
            requested_notional=requested_notional,
            reason_code=reason_code,
            reservation_digest=reservation_digest,
            created_at=now,
        )
    )
    decision_payload = {
        "contract": "canonical-v13-central-demo-risk-v1",
        "decision_mode": "EXECUTION",
        "order_submission_enabled": status == "RISK_ACCEPTED",
        "execution_authorized": status == "RISK_ACCEPTED",
        "reservation_id": str(reservation_id),
        "reservation_digest": reservation_digest,
        "risk_budget_authorization_id": str(risk_budget_authorization_id),
        "policy_digest": budget["policy_digest"],
        "status": status,
        "reason_code": reason_code,
        "allow_real_funds": False,
    }
    decision_digest = canonical_execution_digest(decision_payload)
    risk_decision_id = uuid4()
    effective.execute(
        RISK_DECISIONS_TABLE.insert().values(
            id=risk_decision_id,
            trade_intent_id=trade_intent_id,
            decision_mode=INTENT_MODE_EXECUTION,
            status=status,
            decision_json=decision_payload,
            decision_digest=decision_digest,
            created_at=now,
        )
    )
    return CentralRiskDecisionResult(
        risk_decision_id,
        reservation_id,
        status,
        reason_code,
        decision_digest,
        False,
    )


def decide_signal_risk_shadow(
    connection: Connection,
    *,
    trade_intent_id: UUID,
    evaluated_at: datetime | None = None,
) -> ShadowRiskDecisionResult:
    """Evaluate a Demo intent without creating execution authority or a reservation.

    The accepted/rejected outcome is derived only from the immutable qualified target
    and the intent envelope.  A shadow acceptance is deliberately persisted with no
    budget or reservation and with both execution flags false, so it cannot be
    consumed by the order writer.
    """

    effective = require_canonical_execution(connection)
    now = _utc(evaluated_at or datetime.now(timezone.utc), field="evaluated_at")
    lock_execution_boundary(effective, key=f"shadow-risk-intent:{trade_intent_id}")
    existing = (
        effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.trade_intent_id == trade_intent_id,
                RISK_DECISIONS_TABLE.c.decision_mode
                == INTENT_MODE_SIGNAL_RISK_SHADOW,
            )
        )
        .mappings()
        .one_or_none()
    )
    intent = (
        effective.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.id == trade_intent_id,
                TRADE_INTENTS_TABLE.c.status == "INTENT_ACCEPTED",
                TRADE_INTENTS_TABLE.c.intent_mode
                == INTENT_MODE_SIGNAL_RISK_SHADOW,
            )
        )
        .mappings()
        .one_or_none()
    )
    signal = (
        effective.execute(
            select(SIGNALS_TABLE).where(SIGNALS_TABLE.c.id == intent["signal_id"])
        )
        .mappings()
        .one_or_none()
        if intent is not None
        else None
    )
    target = (
        effective.execute(
            select(RESEARCH_TARGETS_TABLE).where(
                RESEARCH_TARGETS_TABLE.c.id == signal["research_target_id"]
            )
        )
        .mappings()
        .one_or_none()
        if signal is not None
        else None
    )
    deployment = (
        effective.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == signal["deployment_id"]
            )
        )
        .mappings()
        .one_or_none()
        if signal is not None
        else None
    )
    approval = (
        effective.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id
                == deployment["deployment_approval_id"]
            )
        )
        .mappings()
        .one_or_none()
        if deployment is not None
        else None
    )
    qualification = (
        effective.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == approval["qualification_decision_id"]
            )
        )
        .mappings()
        .one_or_none()
        if approval is not None
        else None
    )
    if (
        intent is None
        or signal is None
        or target is None
        or deployment is None
        or approval is None
        or approval["status"] != "APPROVED"
        or qualification is None
        or qualification["status"] != "QUALIFIED"
        or qualification["research_target_id"] != target["id"]
        or qualification["strategy_version_id"] != signal["strategy_version_id"]
        or qualification["configuration_bundle_id"]
        != signal["configuration_bundle_id"]
        or qualification["configuration_bundle_digest"]
        != signal["configuration_bundle_digest"]
        or qualification["market_snapshot_id"] != signal["market_snapshot_id"]
        or qualification["market_snapshot_digest"]
        != signal["market_snapshot_digest"]
        or deployment["status"] != "ACTIVE"
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or not shadow_signal_source_accepted(signal)
        or signal["signal_json"].get("allow_real_funds") is not False
        or canonical_execution_digest(dict(signal["signal_json"]))
        != signal["signal_digest"]
        or canonical_execution_digest(dict(intent["intent_json"]))
        != intent["intent_digest"]
        or target["instrument"] != "BTC-USDT-SWAP"
        or target["pair"] != "BTC/USDT:USDT"
        or target["data_kind"] != "futures"
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_SHADOW_RISK_LINEAGE",
            "shadow risk requires the active exact qualified Demo target lineage",
        )
    body = intent["intent_json"].get("exchange_body")
    baseline_accepted = (
        isinstance(body, Mapping)
        and intent["intent_json"].get("execution_target") == EXECUTION_TARGET
        and intent["intent_json"].get("allow_real_funds") is False
        and intent["intent_json"].get("instrument") == target["instrument"]
        and body.get("instId") == target["instrument"]
        and body.get("tdMode") == "isolated"
        and body.get("side") == "buy"
        and body.get("posSide") == "long"
    )
    if not baseline_accepted:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_SHADOW_RISK_BASELINE",
            "shadow receipt requires one exact accepted long-only baseline intent",
        )
    baseline_body = dict(body)
    counterfactual_body = {
        **baseline_body,
        "side": "sell",
        "posSide": "short",
    }
    checks = [
        {
            "check_id": "EXACT_LONG_ONLY_BASELINE",
            "input_digest": canonical_execution_digest(baseline_body),
            "outcome": "ACCEPTED",
            "reason_code": "SHADOW_EXACT_TARGET_LONG_ONLY_ACCEPTED",
            "order_submission_enabled": False,
            "execution_authorized": False,
        },
        {
            "check_id": "LONG_ONLY_REJECTED_COUNTERFACTUAL",
            "input_digest": canonical_execution_digest(counterfactual_body),
            "outcome": "REJECTED",
            "reason_code": "SHADOW_SHORT_SELL_COUNTERFACTUAL_REJECTED",
            "order_submission_enabled": False,
            "execution_authorized": False,
        },
    ]
    status = "RISK_ACCEPTED"
    reason_code = "SHADOW_BASELINE_AND_COUNTERFACTUAL_VERIFIED"
    payload = {
        "contract": "canonical-v13-signal-risk-shadow-decision-v1",
        "decision_mode": "SIGNAL_RISK_SHADOW",
        "trade_intent_id": str(trade_intent_id),
        "intent_digest": intent["intent_digest"],
        "research_target_id": str(target["id"]),
        "research_target_digest": target["target_digest"],
        "checks": checks,
        "status": status,
        "reason_code": reason_code,
        "order_submission_enabled": False,
        "execution_authorized": False,
        "risk_budget_authorization_id": None,
        "reservation_id": None,
        "allow_real_funds": False,
    }
    decision_digest = canonical_execution_digest(payload)
    if existing is not None:
        if (
            existing["status"] != status
            or existing["decision_json"] != payload
            or existing["decision_digest"] != decision_digest
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_SHADOW_RISK_REPLAY_DRIFT",
                "persisted decision is not the exact non-executable shadow receipt",
            )
        return ShadowRiskDecisionResult(
            existing["id"], status, reason_code, decision_digest, True
        )
    decision_id = uuid4()
    effective.execute(
        RISK_DECISIONS_TABLE.insert().values(
            id=decision_id,
            trade_intent_id=trade_intent_id,
            decision_mode=INTENT_MODE_SIGNAL_RISK_SHADOW,
            status=status,
            decision_json=payload,
            decision_digest=decision_digest,
            created_at=now,
        )
    )
    return ShadowRiskDecisionResult(
        decision_id, status, reason_code, decision_digest, False
    )


__all__ = [
    "ATTESTATION_MAXIMUM_TTL",
    "CentralRiskDecisionResult",
    "RedactedExecutionAttestationResult",
    "RiskBudgetAuthorizationResult",
    "ShadowRiskDecisionResult",
    "authorize_demo_risk_budget",
    "decide_central_demo_risk",
    "decide_signal_risk_shadow",
    "record_redacted_demo_attestation",
    "shadow_signal_source_accepted",
]
