"""Single-writer dispatch for independent continuous OKX_DEMO grants.

This module deliberately does not reuse the one-shot canary policy or budget.
Each accepted risk decision authorizes exactly one natural-signal open or the
matching position exit.  Durable request, fenced claim, POST, and GET-only
recovery reuse the already accepted Phase 9 order saga primitives.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.adapters.okx_demo.write_semantics import (
    OkxDemoPreDispatchBlocked,
    OkxDemoTransportError,
)
from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    lock_execution_boundary,
    require_canonical_execution,
    require_digest,
    require_identity,
)
from app.canonical_v13.models import (
    DEPLOYMENTS_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    ORDERS_TABLE,
    ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE,
    ORDER_DISPATCH_RECEIPTS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
    RISK_DECISIONS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.order_service import CANONICAL_ORDER_WRITER_IDENTITY
from app.canonical_v13.phase9_canary_policy import validate_persisted_canary_probe_receipt
from app.canonical_v13.phase9_okx_demo import (
    RedactedOkxDemoDispatchGuard,
    RedactedOkxDemoExitGuard,
    redacted_dispatch_guard_payload,
    redacted_exit_guard_payload,
)
from app.canonical_v13.phase9_order_writer import (
    CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
    CanonicalOrderRecoveryRequired,
    ConnectionFactory,
    DemoOrderTransport,
    DispatchedDemoOrder,
    PreparedDemoOrder,
    _acquire_writer_lease,
    _exchange_body,
    _load_dispatch,
    _negative_outcome_is_exact,
    _persist_exchange_receipt,
    _persist_nonaccepted_outcome,
    _persisted_utc,
    _replay_persisted_exchange_receipt,
    _safe_post_result,
)
from app.canonical_v13.risk_service import (
    INTENT_MODE_CONTINUOUS_OPEN,
    INTENT_MODE_POSITION_EXIT,
)


CONTINUOUS_MODES = {INTENT_MODE_CONTINUOUS_OPEN, INTENT_MODE_POSITION_EXIT}


class ContinuousDemoTransport(DemoOrderTransport, Protocol):
    def exit_guard(
        self,
        *,
        instrument: str,
        expected_contracts: str,
    ) -> RedactedOkxDemoExitGuard: ...


def _now(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_ORDER_TIME", "evaluation time must be timezone-aware"
        )
    return resolved.astimezone(timezone.utc)


def _utc_value(value: object, *, field: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_ORDER_TIME", f"{field} must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _decimal(value: object, *, field: str, positive: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        result = Decimal("NaN")
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_ORDER_NUMBER", f"{field} is invalid"
        )
    return result


def _authority(
    connection: Connection, *, risk_decision_id: UUID, attestation_id: UUID, now: datetime
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
    decision = connection.execute(
        select(RISK_DECISIONS_TABLE).where(RISK_DECISIONS_TABLE.c.id == risk_decision_id)
    ).mappings().one_or_none()
    intent = (
        connection.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.id == decision["trade_intent_id"]
            )
        ).mappings().one_or_none()
        if decision is not None
        else None
    )
    grant = decision["decision_json"] if decision is not None else None
    intent_json = intent["intent_json"] if intent is not None else None
    attestation = connection.execute(
        select(EXECUTION_ATTESTATIONS_TABLE).where(
            EXECUTION_ATTESTATIONS_TABLE.c.id == attestation_id
        )
    ).mappings().one_or_none()
    deployment = (
        connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == attestation["deployment_id"]
            )
        ).mappings().one_or_none()
        if attestation is not None
        else None
    )
    if (
        decision is None
        or intent is None
        or not isinstance(grant, Mapping)
        or not isinstance(intent_json, Mapping)
        or decision["status"] != "RISK_ACCEPTED"
        or intent["status"] != "INTENT_ACCEPTED"
        or decision["decision_mode"] not in CONTINUOUS_MODES
        or intent["intent_mode"] != decision["decision_mode"]
        or grant.get("decision_mode") != decision["decision_mode"]
        or grant.get("trade_intent_id") != str(intent["id"])
        or grant.get("intent_digest") != intent["intent_digest"]
        or grant.get("status") != "RISK_ACCEPTED"
        or grant.get("order_submission_enabled") is not True
        or grant.get("execution_authorized") is not True
        or grant.get("allow_real_funds") is not False
        or canonical_execution_digest(grant) != decision["decision_digest"]
        or grant.get("execution_attestation_id") != str(attestation_id)
        or attestation is None
        or attestation["status"] != "READY"
        or attestation["execution_target"] != "OKX_DEMO"
        or attestation["permissions_json"]
        != {"read": True, "trade": True, "withdraw": False}
        or deployment is None
        or deployment["status"] != "ACTIVE"
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or grant.get("deployment_id") != str(deployment["id"])
        or grant.get("deployment_capability_digest") != deployment["capability_digest"]
        or _persisted_utc(attestation["observed_at"]) > now
        or _persisted_utc(attestation["expires_at"]) <= now
        or _utc_value(grant.get("expires_at"), field="grant.expires_at") <= now
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_ORDER_AUTHORITY",
            "fresh exact per-signal Demo execution grant is required",
        )
    body = _exchange_body(
        intent_json.get("exchange_body", {}), risk_decision_id=risk_decision_id
    )
    action = grant.get("action")
    if (
        body["instId"] != attestation["instrument"]
        or intent_json.get("instrument") != body["instId"]
        or (decision["decision_mode"], action, body["side"], body["ordType"])
        not in {
            (INTENT_MODE_CONTINUOUS_OPEN, "OPEN_LONG", "buy", "market"),
            (INTENT_MODE_POSITION_EXIT, "CLOSE_LONG", "sell", "market"),
        }
        or body["posSide"] != "long"
        or body["tdMode"] != "isolated"
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_ORDER_BODY", "grant and exchange request differ"
        )
    return dict(decision), dict(intent), dict(grant), dict(attestation), body


def prepare_continuous_demo_order(
    connection: Connection,
    *,
    risk_decision_id: UUID,
    attestation_id: UUID,
    writer_identity: str,
    holder_identity: str,
    holder_token_digest: str,
    idempotency_key: str,
    evaluated_at: datetime | None = None,
    lease_ttl: timedelta = timedelta(seconds=10),
) -> PreparedDemoOrder:
    effective = require_canonical_execution(connection)
    now = _now(evaluated_at)
    if writer_identity != CANONICAL_ORDER_WRITER_IDENTITY:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_NON_CANONICAL_ORDER_WRITER", writer_identity
        )
    holder_identity = require_identity(holder_identity, field="holder_identity")
    if holder_identity != CANONICAL_ORDER_WRITER_PROCESS_IDENTITY:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_WRITER_PROCESS_IDENTITY", holder_identity
        )
    require_digest(holder_token_digest, field="holder_token_digest")
    idempotency_key = require_identity(idempotency_key, field="idempotency_key")
    decision, _intent, grant, attestation, body = _authority(
        effective,
        risk_decision_id=risk_decision_id,
        attestation_id=attestation_id,
        now=now,
    )
    size = _decimal(body["sz"], field="size")
    if (
        size != _decimal(grant["minimum_contract_size"], field="minimum_contract_size")
        or grant.get("max_order_count") != 1
        or (
            decision["decision_mode"] == INTENT_MODE_CONTINUOUS_OPEN
            and "px" in body
        )
        or (
            decision["decision_mode"] == INTENT_MODE_POSITION_EXIT
            and size
            != _decimal(
                grant.get("maximum_close_contracts"), field="maximum_close_contracts"
            )
        )
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_ORDER_LIMIT", "request exceeds the per-signal grant"
        )
    request = {
        "contract": "canonical-v13-continuous-demo-order-request-v1",
        "risk_decision_id": str(risk_decision_id),
        "risk_decision_digest": decision["decision_digest"],
        "execution_grant_digest": decision["decision_digest"],
        "execution_attestation_id": str(attestation_id),
        "execution_attestation_digest": attestation["attestation_digest"],
        "dispatch_mode": decision["decision_mode"],
        "writer_identity": writer_identity,
        "idempotency_key": idempotency_key,
        "body": body,
        "demo_only": True,
        "allow_real_funds": False,
    }
    request_digest = canonical_execution_digest(request)
    lock_execution_boundary(effective, key=f"continuous-order:{idempotency_key}")
    existing = effective.execute(
        select(ORDERS_TABLE).where(
            (ORDERS_TABLE.c.risk_decision_id == risk_decision_id)
            | (ORDERS_TABLE.c.idempotency_key == idempotency_key)
        )
    ).mappings().one_or_none()
    if existing is not None:
        if existing["request_digest"] != request_digest:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_ORDER_IDEMPOTENCY_DRIFT", idempotency_key
            )
        if (
            existing["status"] == "SUBMITTED"
            and existing["exchange_order_id"] is None
            and existing["receipt_digest"] is None
        ):
            generation = _acquire_writer_lease(
                effective,
                holder_identity=holder_identity,
                holder_token_digest=holder_token_digest,
                now=now,
                lease_ttl=lease_ttl,
            )
        else:
            generation = int(
                effective.execute(
                    select(ORDER_WRITER_LEASES_TABLE.c.generation).where(
                        ORDER_WRITER_LEASES_TABLE.c.execution_target == "OKX_DEMO"
                    )
                ).scalar_one_or_none()
                or 0
            )
        return PreparedDemoOrder(existing["id"], request_digest, generation, True)
    generation = _acquire_writer_lease(
        effective,
        holder_identity=holder_identity,
        holder_token_digest=holder_token_digest,
        now=now,
        lease_ttl=lease_ttl,
    )
    order_id = uuid4()
    effective.execute(
        ORDERS_TABLE.insert().values(
            id=order_id,
            risk_decision_id=risk_decision_id,
            writer_identity=writer_identity,
            idempotency_key=idempotency_key,
            exchange_order_id=None,
            status="SUBMITTED",
            demo_only=True,
            allow_real_funds=False,
            request_digest=request_digest,
            receipt_digest=None,
            created_at=now,
        )
    )
    return PreparedDemoOrder(order_id, request_digest, generation, False)


def _lease(
    connection: Connection,
    *,
    holder_identity: str,
    holder_token_digest: str,
    lease_generation: int,
    now: datetime,
) -> Mapping[str, object]:
    lease = connection.execute(
        select(ORDER_WRITER_LEASES_TABLE).where(
            ORDER_WRITER_LEASES_TABLE.c.execution_target == "OKX_DEMO"
        )
    ).mappings().one_or_none()
    expected = (
        canonical_execution_digest(
            {
                "execution_target": "OKX_DEMO",
                "holder_identity": lease["holder_identity"],
                "holder_token_digest": lease["holder_token_digest"],
                "generation": int(lease["generation"]),
                "expires_at": _persisted_utc(lease["expires_at"]).isoformat(),
            }
        )
        if lease is not None
        else None
    )
    if (
        lease is None
        or lease["status"] != "ACTIVE"
        or lease["holder_identity"] != holder_identity
        or lease["holder_token_digest"] != holder_token_digest
        or int(lease["generation"]) != lease_generation
        or lease["lease_digest"] != expected
        or not (_persisted_utc(lease["created_at"]) <= now < _persisted_utc(lease["expires_at"]))
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_WRITER_LEASE_FENCED", "fresh exact writer lease is required"
        )
    return lease


def _attempt(
    connection: Connection, *, order_id: UUID, body: Mapping[str, str]
) -> int:
    claims = connection.execute(
        select(ORDER_DISPATCH_RECEIPTS_TABLE)
        .where(ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == order_id)
        .order_by(ORDER_DISPATCH_RECEIPTS_TABLE.c.attempt_ordinal)
    ).mappings().all()
    outcomes = connection.execute(
        select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE).where(
            ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.order_id == order_id
        )
    ).mappings().all()
    if not claims and not outcomes:
        return 1
    if (
        len(claims) == 1
        and claims[0]["attempt_ordinal"] == 1
        and len(outcomes) == 1
        and _negative_outcome_is_exact(outcome=outcomes[0], claim=claims[0], body=body)
    ):
        return 2
    raise CanonicalOrderRecoveryRequired(
        "BLOCKED_ORDER_RETRY_EVIDENCE",
        "a second POST requires one exact first-attempt absence receipt",
    )


def _claim_continuous_dispatch(
    connection: Connection,
    *,
    order_id: UUID,
    holder_identity: str,
    holder_token_digest: str,
    lease_generation: int,
    guard: RedactedOkxDemoDispatchGuard | RedactedOkxDemoExitGuard,
    evaluated_at: datetime,
) -> tuple[dict[str, Any], dict[str, str]]:
    effective = require_canonical_execution(connection)
    now = _now(evaluated_at)
    lock_execution_boundary(effective, key=f"continuous-dispatch:{order_id}")
    order, body = _load_dispatch(effective, order_id)
    if order["receipt_digest"] is not None and order["exchange_order_id"]:
        return order, body
    decision = effective.execute(
        select(RISK_DECISIONS_TABLE).where(
            RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
        )
    ).mappings().one()
    grant = decision["decision_json"]
    attestation_id = UUID(str(grant["execution_attestation_id"]))
    _decision, _intent, grant, attestation, exact_body = _authority(
        effective,
        risk_decision_id=decision["id"],
        attestation_id=attestation_id,
        now=now,
    )
    if exact_body != body or order["status"] != "SUBMITTED":
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_GET_ONLY_RECOVERY_REQUIRED", "order was already claimed"
        )
    size = _decimal(body["sz"], field="size")
    if decision["decision_mode"] == INTENT_MODE_CONTINUOUS_OPEN:
        if not isinstance(guard, RedactedOkxDemoDispatchGuard):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_OPEN_GUARD", "sealed flat dispatch guard is required"
            )
        probe_id = UUID(str(grant["probe_receipt_id"]))
        facts = validate_persisted_canary_probe_receipt(
            effective,
            probe_receipt_id=probe_id,
            evaluated_at=now,
            strategy_max_leverage=Decimal(str(grant["strategy_max_leverage"])),
        )
        probe = effective.execute(
            select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
                EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id == probe_id
            )
        ).mappings().one()
        guard_payload = redacted_dispatch_guard_payload(guard)
        limit_price = _decimal(guard.limit_price, field="guard.limit_price")
        effective_leverage = _decimal(
            guard.effective_leverage, field="guard.effective_leverage"
        )
        minimum_size = _decimal(guard.minimum_size, field="guard.minimum_size")
        maximum_buy = _decimal(
            guard.maximum_buy_contracts, field="guard.maximum_buy_contracts"
        )
        if (
            probe["execution_attestation_id"] != attestation["id"]
            or probe["receipt_digest"] != grant["probe_receipt_digest"]
            or guard.account_fingerprint_digest != attestation["account_fingerprint_digest"]
            or guard.credential_generation_digest
            != attestation["credential_generation_digest"]
            or guard.execution_target != "OKX_DEMO"
            or guard.instrument != body["instId"]
            or Decimal(guard.long_contracts) != 0
            or Decimal(guard.short_contracts) != 0
            or guard.active_position_count != 0
            or guard.pending_order_count != 0
            or minimum_size != size
            or minimum_size != _decimal(facts["minimum_size"], field="probe.minimum_size")
            or limit_price != _decimal(grant["limit_price"], field="grant.limit_price")
            or effective_leverage
            != _decimal(grant["effective_leverage"], field="grant.effective_leverage")
            or maximum_buy < size
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_OPEN_GUARD_DRIFT", "fresh flat capacity drifted"
            )
        dispatch_values = {
            "canary_risk_policy_id": None,
            "probe_receipt_id": probe_id,
            "dispatch_mode": INTENT_MODE_CONTINUOUS_OPEN,
            "reference_price": limit_price,
            "limit_price": limit_price,
            "effective_leverage": effective_leverage,
            "minimum_size": minimum_size,
            "maximum_buy_contracts": maximum_buy,
            "maximum_close_contracts": None,
            "long_contracts": Decimal(guard.long_contracts),
            "short_contracts": Decimal(guard.short_contracts),
            "active_position_count": guard.active_position_count,
            "pending_order_count": guard.pending_order_count,
            "positions_digest": guard.positions_digest,
            "positions_observed_at": guard.positions_observed_at,
            "positions_expires_at": guard.positions_expires_at,
            "pending_orders_digest": guard.pending_orders_digest,
            "pending_orders_observed_at": guard.pending_orders_observed_at,
            "pending_orders_expires_at": guard.pending_orders_expires_at,
            "maximum_order_quantity_digest": guard.maximum_order_quantity_digest,
            "maximum_order_quantity_observed_at": guard.maximum_order_quantity_observed_at,
            "maximum_order_quantity_expires_at": guard.maximum_order_quantity_expires_at,
            "close_capacity_digest": None,
            "close_capacity_observed_at": None,
            "close_capacity_expires_at": None,
            "guard_leverage_digest": guard.leverage_digest,
            "guard_leverage_observed_at": guard.leverage_observed_at,
            "guard_leverage_expires_at": guard.leverage_expires_at,
            "guard_observed_at": guard.observed_at,
            "guard_expires_at": guard.expires_at,
        }
    else:
        if not isinstance(guard, RedactedOkxDemoExitGuard):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_EXIT_GUARD", "sealed one-position guard is required"
            )
        guard_payload = redacted_exit_guard_payload(guard)
        close_size = _decimal(guard.close_contracts, field="guard.close_contracts")
        reference_price = _decimal(guard.reference_price, field="guard.reference_price")
        effective_leverage = _decimal(
            guard.effective_leverage, field="guard.effective_leverage"
        )
        close_capacity = canonical_execution_digest(
            {
                "execution_target": "OKX_DEMO",
                "resource": "close_capacity",
                "source": "okx_demo_rest",
                "authenticated": True,
                "observed_at": _persisted_utc(guard.positions_observed_at).isoformat(),
                "expires_at": _persisted_utc(guard.positions_expires_at).isoformat(),
                "facts": {
                    "instrument": guard.instrument,
                    "margin_mode": "isolated",
                    "long_contracts": guard.long_contracts,
                    "maximum_close_contracts": guard.close_contracts,
                },
            }
        )
        if (
            guard.account_fingerprint_digest != attestation["account_fingerprint_digest"]
            or guard.credential_generation_digest
            != attestation["credential_generation_digest"]
            or guard.execution_target != "OKX_DEMO"
            or guard.instrument != body["instId"]
            or guard.simulated_trading is not True
            or guard.allow_real_funds is not False
            or guard.pending_order_count != 0
            or guard.active_position_count != 1
            or Decimal(guard.short_contracts) != 0
            or Decimal(guard.long_contracts) != close_size
            or Decimal(guard.available_contracts) != close_size
            or close_size != size
            or close_size
            != _decimal(grant["maximum_close_contracts"], field="maximum_close_contracts")
            or effective_leverage
            != _decimal(grant["effective_leverage"], field="effective_leverage")
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_EXIT_GUARD_DRIFT", "fresh close capacity drifted"
            )
        dispatch_values = {
            "canary_risk_policy_id": None,
            "probe_receipt_id": None,
            "dispatch_mode": INTENT_MODE_POSITION_EXIT,
            "reference_price": reference_price,
            "limit_price": None,
            "effective_leverage": effective_leverage,
            "minimum_size": close_size,
            "maximum_buy_contracts": None,
            "maximum_close_contracts": close_size,
            "long_contracts": close_size,
            "short_contracts": Decimal(guard.short_contracts),
            "active_position_count": guard.active_position_count,
            "pending_order_count": guard.pending_order_count,
            "positions_digest": guard.positions_digest,
            "positions_observed_at": guard.positions_observed_at,
            "positions_expires_at": guard.positions_expires_at,
            "pending_orders_digest": guard.pending_orders_digest,
            "pending_orders_observed_at": guard.pending_orders_observed_at,
            "pending_orders_expires_at": guard.pending_orders_expires_at,
            "maximum_order_quantity_digest": None,
            "maximum_order_quantity_observed_at": None,
            "maximum_order_quantity_expires_at": None,
            "close_capacity_digest": close_capacity,
            "close_capacity_observed_at": guard.positions_observed_at,
            "close_capacity_expires_at": guard.positions_expires_at,
            "guard_leverage_digest": guard.leverage_digest,
            "guard_leverage_observed_at": guard.leverage_observed_at,
            "guard_leverage_expires_at": guard.leverage_expires_at,
            "guard_observed_at": guard.observed_at,
            "guard_expires_at": guard.expires_at,
        }
    if (
        any(
            value.tzinfo is None
            for value in (
                dispatch_values["positions_observed_at"],
                dispatch_values["positions_expires_at"],
                dispatch_values["pending_orders_observed_at"],
                dispatch_values["pending_orders_expires_at"],
                dispatch_values["guard_leverage_observed_at"],
                dispatch_values["guard_leverage_expires_at"],
                dispatch_values["guard_observed_at"],
                dispatch_values["guard_expires_at"],
            )
        )
        or _persisted_utc(dispatch_values["guard_observed_at"]) > now + timedelta(seconds=5)
        or _persisted_utc(dispatch_values["guard_expires_at"]) <= now
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_GUARD_FRESHNESS", "guard is stale or malformed"
        )
    attempt = _attempt(effective, order_id=order_id, body=body)
    lease = _lease(
        effective,
        holder_identity=holder_identity,
        holder_token_digest=holder_token_digest,
        lease_generation=lease_generation,
        now=now,
    )
    updated = effective.execute(
        ORDERS_TABLE.update()
        .where(
            ORDERS_TABLE.c.id == order_id,
            ORDERS_TABLE.c.status == "SUBMITTED",
            ORDERS_TABLE.c.receipt_digest.is_(None),
        )
        .values(status="DISPATCHING")
    )
    if updated.rowcount != 1:
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_GET_ONLY_RECOVERY_REQUIRED", "dispatch claim was fenced"
        )
    guard_digest = canonical_execution_digest(guard_payload)
    claim = {
        "contract": "canonical-v13-continuous-order-dispatch-claim-v1",
        "order_id": str(order_id),
        "attempt_ordinal": attempt,
        "dispatch_mode": decision["decision_mode"],
        "request_digest": order["request_digest"],
        "holder_identity": holder_identity,
        "holder_token_digest": holder_token_digest,
        "lease_generation": lease_generation,
        "lease_digest": lease["lease_digest"],
        "lease_acquired_at": _persisted_utc(lease["created_at"]).isoformat(),
        "lease_expires_at": _persisted_utc(lease["expires_at"]).isoformat(),
        "risk_decision_id": str(decision["id"]),
        "execution_attestation_id": str(attestation["id"]),
        "guard_digest": guard_digest,
        "claimed_at": now.isoformat(),
    }
    effective.execute(
        ORDER_DISPATCH_RECEIPTS_TABLE.insert().values(
            order_id=order_id,
            risk_decision_id=decision["id"],
            execution_attestation_id=attestation["id"],
            attempt_ordinal=attempt,
            request_digest=order["request_digest"],
            holder_identity=holder_identity,
            holder_token_digest=holder_token_digest,
            lease_generation=lease_generation,
            lease_digest=lease["lease_digest"],
            lease_acquired_at=lease["created_at"],
            lease_expires_at=lease["expires_at"],
            account_fingerprint_digest=attestation["account_fingerprint_digest"],
            credential_generation_digest=attestation["credential_generation_digest"],
            guard_json=guard_payload,
            guard_digest=guard_digest,
            claim_digest=canonical_execution_digest(claim),
            claimed_at=now,
            **dispatch_values,
        )
    )
    return {**order, "status": "DISPATCHING"}, body


def dispatch_continuous_demo_order(
    connection_factory: ConnectionFactory,
    *,
    order_id: UUID,
    transport: ContinuousDemoTransport,
    holder_identity: str,
    holder_token_digest: str,
    lease_generation: int,
    evaluated_at: datetime | None = None,
) -> DispatchedDemoOrder:
    now = _now(evaluated_at)
    with connection_factory() as connection:
        order, body = _load_dispatch(connection, order_id)
        decision = connection.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
            )
        ).mappings().one()
        grant = decision["decision_json"]
    if order["receipt_digest"] is not None and order["exchange_order_id"]:
        with connection_factory() as connection:
            return _replay_persisted_exchange_receipt(connection, order_id=order_id)
    if decision["decision_mode"] == INTENT_MODE_CONTINUOUS_OPEN:
        guard: RedactedOkxDemoDispatchGuard | RedactedOkxDemoExitGuard = transport.dispatch_guard(
            instrument=body["instId"],
            limit_price=str(grant["limit_price"]),
            effective_leverage=str(grant["effective_leverage"]),
            minimum_size=str(grant["minimum_contract_size"]),
        )
    elif decision["decision_mode"] == INTENT_MODE_POSITION_EXIT:
        guard = transport.exit_guard(
            instrument=body["instId"],
            expected_contracts=str(grant["maximum_close_contracts"]),
        )
    else:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_ORDER_MODE", str(decision["decision_mode"])
        )
    try:
        transport.preflight_place(body)
    except OkxDemoPreDispatchBlocked as exc:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_PRE_DISPATCH", f"order POST did not start: {exc.safe_diagnostic}"
        ) from None
    claim_at = max(now, _persisted_utc(guard.observed_at))
    with connection_factory() as connection:
        _order, body = _claim_continuous_dispatch(
            connection,
            order_id=order_id,
            holder_identity=holder_identity,
            holder_token_digest=holder_token_digest,
            lease_generation=lease_generation,
            guard=guard,
            evaluated_at=claim_at,
        )
    try:
        payload = transport.place(body)
        status, exchange_order_id, safe_response = _safe_post_result(
            payload, client_order_id=body["clOrdId"]
        )
    except CanonicalOrderRecoveryRequired:
        raise
    except (OkxDemoPreDispatchBlocked, OkxDemoTransportError) as exc:
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RECOVERY_REQUIRED",
            f"order outcome is unknown: {getattr(exc, 'safe_diagnostic', type(exc).__name__)}",
        ) from None
    except Exception as exc:
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RECOVERY_REQUIRED",
            f"order outcome is unknown: {type(exc).__name__}",
        ) from None
    with connection_factory() as connection:
        if status == "REJECTED":
            return _persist_nonaccepted_outcome(
                connection,
                order_id=order_id,
                safe_response=safe_response,
                outcome_mode="POST_REJECTED",
            )
        if exchange_order_id is None:
            raise CanonicalOrderRecoveryRequired(
                "BLOCKED_ORDER_RESPONSE_UNKNOWN", "exchange order identity is missing"
            )
        return _persist_exchange_receipt(
            connection,
            order_id=order_id,
            exchange_order_id=exchange_order_id,
            safe_response=safe_response,
            outcome_mode="POST",
        )


__all__ = [
    "ContinuousDemoTransport",
    "dispatch_continuous_demo_order",
    "prepare_continuous_demo_order",
]
