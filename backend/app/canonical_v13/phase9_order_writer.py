"""Durable single-writer saga for one canonical OKX_DEMO order.

Preparation and dispatch are deliberately separate.  ``prepare_demo_order`` commits
the immutable request before any transport may be called.  ``dispatch_demo_order``
opens its own transactions around the single POST; uncertain outcomes remain
``SUBMITTED`` and can only use the GET-only recovery path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

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
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    FILLS_TABLE,
    LEDGER_ENTRIES_TABLE,
    ORDERS_TABLE,
    ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE,
    ORDER_DISPATCH_RECEIPTS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
    RECONCILIATION_ITEMS_TABLE,
    RISK_DECISIONS_TABLE,
    SIGNALS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.order_service import CANONICAL_ORDER_WRITER_IDENTITY
from app.canonical_v13.phase9_okx_demo import (
    RedactedOkxDemoDispatchGuard,
    RedactedOkxDemoOrderAbsence,
    redacted_dispatch_guard_payload,
    redacted_order_absence_payload,
)
from app.canonical_v13.phase9_topology import PHASE9_SERVICE_SPECS


CANONICAL_ORDER_WRITER_PROCESS_IDENTITY = PHASE9_SERVICE_SPECS[
    "order_writer"
].process_identity
PRE_DISPATCH_CANCELLATION_CONTRACT = (
    "canonical-v13-pre-dispatch-cancellation-receipt-v1"
)


class DemoOrderTransport(Protocol):
    def dispatch_guard(
        self,
        *,
        instrument: str,
        limit_price: str,
        effective_leverage: str,
        minimum_size: str,
    ) -> RedactedOkxDemoDispatchGuard: ...

    def place(self, body: Mapping[str, str]) -> Mapping[str, Any]: ...

    def query(self, *, instrument: str, client_order_id: str) -> Mapping[str, Any]: ...

    def prove_absent(
        self, *, instrument: str, client_order_id: str
    ) -> RedactedOkxDemoOrderAbsence: ...


ConnectionFactory = Callable[[], AbstractContextManager[Connection]]


class CanonicalOrderRecoveryRequired(CanonicalExecutionChainBlocked):
    pass


@dataclass(frozen=True)
class PreparedDemoOrder:
    order_id: UUID
    request_digest: str
    lease_generation: int
    repeat_noop: bool


@dataclass(frozen=True)
class DispatchedDemoOrder:
    order_id: UUID
    exchange_order_id: str | None
    receipt_digest: str
    repeat_noop: bool
    status: str = "ACCEPTED"
    retry_authorized: bool = False


@dataclass(frozen=True)
class CancelledPreparedDemoOrder:
    order_id: UUID
    receipt_digest: str
    repeat_noop: bool
    status: str = "CANCELLED"


def _persisted_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _now(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_TIMEZONE", "order evaluation time must be timezone-aware"
        )
    return resolved.astimezone(timezone.utc)


def pre_dispatch_cancellation_receipt_digest(order: Mapping[str, object]) -> str:
    """Seal an exact local-only cancellation with no exchange claim or POST."""

    return canonical_execution_digest(
        {
            "contract": PRE_DISPATCH_CANCELLATION_CONTRACT,
            "order_id": str(order["id"]),
            "risk_decision_id": str(order["risk_decision_id"]),
            "request_digest": str(order["request_digest"]),
            "reason_code": "PRE_DISPATCH_NO_EXCHANGE_ACCESS",
            "exchange_access": "NONE",
            "order_submission_enabled": False,
        }
    )


def assert_pre_dispatch_downstream_absent(
    connection: Connection, *, order_id: UUID
) -> None:
    """Read-only control-plane fence for writers outside the order capability."""

    for table in (FILLS_TABLE, RECONCILIATION_ITEMS_TABLE):
        if connection.execute(
            select(table.c.order_id).where(table.c.order_id == order_id).limit(1)
        ).scalar_one_or_none() is not None:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_PRE_DISPATCH_SIDE_EFFECT_EVIDENCE", table.name
            )


def cancel_prepared_demo_order(
    connection: Connection, *, order_id: UUID
) -> CancelledPreparedDemoOrder:
    """Cancel an exact prepared order only when no dispatch boundary was crossed."""

    effective = require_canonical_execution(connection)
    lock_execution_boundary(effective, key=f"demo-order-cancel:{order_id}")
    order = (
        effective.execute(
            select(ORDERS_TABLE)
            .where(ORDERS_TABLE.c.id == order_id)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if order is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRE_DISPATCH_ORDER_NOT_FOUND", str(order_id)
        )
    expected_receipt = pre_dispatch_cancellation_receipt_digest(order)
    replay = order["status"] == "CANCELLED"
    if replay and (
        order["exchange_order_id"] is not None
        or order["receipt_digest"] != expected_receipt
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRE_DISPATCH_CANCELLATION_DRIFT", str(order_id)
        )
    if not replay and (
        order["status"] != "SUBMITTED"
        or order["exchange_order_id"] is not None
        or order["receipt_digest"] is not None
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRE_DISPATCH_ORDER_STATE", str(order["status"])
        )
    for table in (
        ORDER_DISPATCH_RECEIPTS_TABLE,
        ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE,
    ):
        if effective.execute(
            select(table.c.order_id).where(table.c.order_id == order_id).limit(1)
        ).scalar_one_or_none() is not None:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_PRE_DISPATCH_SIDE_EFFECT_EVIDENCE", table.name
            )
    deployment_status = effective.execute(
        select(DEPLOYMENTS_TABLE.c.status)
        .select_from(
            ORDERS_TABLE.join(
                RISK_DECISIONS_TABLE,
                RISK_DECISIONS_TABLE.c.id == ORDERS_TABLE.c.risk_decision_id,
            )
            .join(
                EXECUTION_RISK_RESERVATIONS_TABLE,
                EXECUTION_RISK_RESERVATIONS_TABLE.c.trade_intent_id
                == RISK_DECISIONS_TABLE.c.trade_intent_id,
            )
            .join(
                EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
                EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.id
                == EXECUTION_RISK_RESERVATIONS_TABLE.c.risk_budget_authorization_id,
            )
            .join(
                EXECUTION_CANARY_RISK_POLICIES_TABLE,
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id
                == EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.execution_canary_risk_policy_id,
            )
            .join(
                EXECUTION_ATTESTATIONS_TABLE,
                EXECUTION_ATTESTATIONS_TABLE.c.id
                == EXECUTION_CANARY_RISK_POLICIES_TABLE.c.execution_attestation_id,
            )
            .join(
                DEPLOYMENTS_TABLE,
                DEPLOYMENTS_TABLE.c.id
                == EXECUTION_ATTESTATIONS_TABLE.c.deployment_id,
            )
        )
        .where(ORDERS_TABLE.c.id == order_id)
    ).scalar_one_or_none()
    if deployment_status != "DISABLED":
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRE_DISPATCH_DEPLOYMENT_ACTIVE", str(deployment_status)
        )
    lease_status = effective.execute(
        select(ORDER_WRITER_LEASES_TABLE.c.status).where(
            ORDER_WRITER_LEASES_TABLE.c.execution_target == "OKX_DEMO"
        )
    ).scalar_one_or_none()
    if lease_status not in {None, "RELEASED", "EXPIRED"}:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRE_DISPATCH_WRITER_LEASE", str(lease_status)
        )
    if replay:
        return CancelledPreparedDemoOrder(order_id, expected_receipt, True)
    updated = effective.execute(
        ORDERS_TABLE.update()
        .where(
            ORDERS_TABLE.c.id == order_id,
            ORDERS_TABLE.c.status == "SUBMITTED",
            ORDERS_TABLE.c.exchange_order_id.is_(None),
            ORDERS_TABLE.c.receipt_digest.is_(None),
        )
        .values(status="CANCELLED", receipt_digest=expected_receipt)
    )
    if updated.rowcount != 1:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRE_DISPATCH_CANCELLATION_RACE", str(order_id)
        )
    return CancelledPreparedDemoOrder(order_id, expected_receipt, False)


def _exchange_body(
    order_request: Mapping[str, str], *, risk_decision_id: UUID
) -> dict[str, str]:
    persisted = {
        "instId",
        "tdMode",
        "side",
        "posSide",
        "ordType",
        "sz",
        "px",
    }
    observed = dict(order_request)
    if set(observed) == persisted:
        observed["clOrdId"] = f"v13{risk_decision_id.hex[:29]}"
    elif set(observed) != persisted | {"clOrdId"}:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_REQUEST_FIELDS", "order request field set is not allowlisted"
        )
    if (
        observed["tdMode"] != "isolated"
        or observed["side"] != "buy"
        or observed["posSide"] != "long"
        or observed["ordType"] != "limit"
        or not all(isinstance(value, str) and value for value in observed.values())
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_REQUEST_CONTRACT", "Demo order request is unsafe"
        )
    for value in observed.values():
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_ORDER_REQUEST_CONTRACT", "order request contains control data"
            )
    return observed


def _acquire_writer_lease(
    connection: Connection,
    *,
    holder_identity: str,
    holder_token_digest: str,
    now: datetime,
    lease_ttl: timedelta,
) -> int:
    lock_execution_boundary(connection, key="demo-order-writer-lease")
    if lease_ttl <= timedelta(0) or lease_ttl > timedelta(seconds=30):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_LEASE_TTL", "writer lease TTL must be within 0-30 seconds"
        )
    lease = (
        connection.execute(
            select(ORDER_WRITER_LEASES_TABLE).where(
                ORDER_WRITER_LEASES_TABLE.c.execution_target == "OKX_DEMO"
            )
        )
        .mappings()
        .one_or_none()
    )
    expires_at = now + lease_ttl
    if lease is None:
        generation = 1
        lease_digest = canonical_execution_digest(
            {
                "execution_target": "OKX_DEMO",
                "holder_identity": holder_identity,
                "holder_token_digest": holder_token_digest,
                "generation": generation,
                "expires_at": expires_at.isoformat(),
            }
        )
        connection.execute(
            ORDER_WRITER_LEASES_TABLE.insert().values(
                execution_target="OKX_DEMO",
                holder_identity=holder_identity,
                holder_token_digest=holder_token_digest,
                generation=generation,
                status="ACTIVE",
                expires_at=expires_at,
                lease_digest=lease_digest,
                created_at=now,
            )
        )
        return generation
    lease_expiry = _persisted_utc(lease["expires_at"])
    same_holder = (
        lease["holder_identity"] == holder_identity
        and lease["holder_token_digest"] == holder_token_digest
    )
    if lease["status"] == "ACTIVE" and lease_expiry > now and not same_holder:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_WRITER_LEASE_HELD",
            "another canonical order writer holds the database lease",
        )
    generation = (
        int(lease["generation"]) if same_holder else int(lease["generation"]) + 1
    )
    lease_digest = canonical_execution_digest(
        {
            "execution_target": "OKX_DEMO",
            "holder_identity": holder_identity,
            "holder_token_digest": holder_token_digest,
            "generation": generation,
            "expires_at": expires_at.isoformat(),
        }
    )
    connection.execute(
        ORDER_WRITER_LEASES_TABLE.update()
        .where(ORDER_WRITER_LEASES_TABLE.c.execution_target == "OKX_DEMO")
        .values(
            holder_identity=holder_identity,
            holder_token_digest=holder_token_digest,
            generation=generation,
            status="ACTIVE",
            expires_at=expires_at,
            lease_digest=lease_digest,
            created_at=now,
        )
    )
    return generation


def prepare_demo_order(
    connection: Connection,
    *,
    risk_decision_id: UUID,
    attestation_id: UUID,
    writer_identity: str,
    holder_identity: str,
    holder_token_digest: str,
    idempotency_key: str,
    order_request: Mapping[str, str],
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
    body = _exchange_body(order_request, risk_decision_id=risk_decision_id)
    lock_execution_boundary(effective, key=f"demo-order:{idempotency_key}")
    decision = (
        effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.id == risk_decision_id
            )
        )
        .mappings()
        .one_or_none()
    )
    intent = (
        effective.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.id == decision["trade_intent_id"]
            )
        )
        .mappings()
        .one_or_none()
        if decision is not None
        else None
    )
    reservation = (
        effective.execute(
            select(EXECUTION_RISK_RESERVATIONS_TABLE).where(
                EXECUTION_RISK_RESERVATIONS_TABLE.c.trade_intent_id == intent["id"]
            )
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
                == reservation["risk_budget_authorization_id"]
            )
        )
        .mappings()
        .one_or_none()
        if reservation is not None
        else None
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
    attestation = (
        effective.execute(
            select(EXECUTION_ATTESTATIONS_TABLE).where(
                EXECUTION_ATTESTATIONS_TABLE.c.id == attestation_id
            )
        )
        .mappings()
        .one_or_none()
    )
    deployment = (
        effective.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == attestation["deployment_id"]
            )
        )
        .mappings()
        .one_or_none()
        if attestation is not None
        else None
    )
    if (
        decision is None
        or decision["status"] != "RISK_ACCEPTED"
        or reservation is None
        or reservation["status"] != "RISK_ACCEPTED"
        or decision["decision_json"].get("reservation_digest")
        != reservation["reservation_digest"]
        or budget is None
        or policy is None
        or attestation is None
        or attestation["status"] != "READY"
        or deployment is None
        or deployment["status"] != "ACTIVE"
        or deployment["deployment_approval_id"] != budget["deployment_approval_id"]
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or attestation["execution_target"] != "OKX_DEMO"
        or attestation["permissions_json"]
        != {"read": True, "trade": True, "withdraw": False}
        or attestation["instrument"] != body["instId"]
        or policy["execution_attestation_id"] != attestation["id"]
        or policy["attestation_digest"] != attestation["attestation_digest"]
        or policy["receipt_digest"] != budget["source_receipt_digest"]
        or policy["status"] != "ACTIVE"
        or policy["position_policy"] != "LONG_ONLY"
        or budget["instrument"] != body["instId"]
        or intent["intent_json"].get("instrument") != body["instId"]
        or _exchange_body(
            intent["intent_json"].get("exchange_body", {}),
            risk_decision_id=risk_decision_id,
        )
        != body
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_AUTHORITY_LINEAGE",
            "risk, budget, attestation, deployment, and request must match exactly",
        )
    try:
        requested_size = Decimal(body["sz"])
        requested_price = Decimal(body["px"])
    except InvalidOperation:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_SIZE", "order size must be a canonical decimal"
        ) from None
    if (
        not requested_size.is_finite()
        or requested_size != Decimal(str(policy["minimum_contract_size"]))
        or not requested_price.is_finite()
        or requested_price != Decimal(str(policy["limit_price"]))
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_POLICY_DRIFT",
            "order size and active policy must match the frozen minimum canary",
        )
    if (
        _persisted_utc(attestation["observed_at"]) > now
        or _persisted_utc(attestation["observed_at"])
        >= _persisted_utc(attestation["expires_at"])
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_ATTESTATION_STALE",
            "valid immutable Demo attestation lineage is required",
        )
    request = {
        "contract": "canonical-v13-okx-demo-order-request-v1",
        "risk_decision_id": str(risk_decision_id),
        "risk_decision_digest": decision["decision_digest"],
        "reservation_digest": reservation["reservation_digest"],
        "authorization_digest": budget["authorization_digest"],
        "canary_risk_policy_id": str(policy["id"]),
        "canary_risk_policy_receipt_digest": policy["receipt_digest"],
        "attestation_id": str(attestation_id),
        "attestation_digest": attestation["attestation_digest"],
        "writer_identity": writer_identity,
        "idempotency_key": idempotency_key,
        "body": body,
        "demo_only": True,
        "allow_real_funds": False,
    }
    request_digest = canonical_execution_digest(request)
    existing = (
        effective.execute(
            select(ORDERS_TABLE).where(
                (ORDERS_TABLE.c.risk_decision_id == risk_decision_id)
                | (ORDERS_TABLE.c.idempotency_key == idempotency_key)
            )
        )
        .mappings()
        .one_or_none()
    )
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
            lease = effective.execute(
                select(ORDER_WRITER_LEASES_TABLE.c.generation).where(
                    ORDER_WRITER_LEASES_TABLE.c.execution_target == "OKX_DEMO"
                )
            ).scalar_one_or_none()
            generation = int(lease or 0)
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


def _safe_exchange_identity(
    payload: Mapping[str, Any], *, client_order_id: str
) -> tuple[str, dict[str, str]]:
    if payload.get("code") != "0" or set(payload).difference(
        {"code", "msg", "data", "inTime", "outTime"}
    ):
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RESPONSE_UNKNOWN", "exchange response envelope is unsafe"
        )
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping):
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RESPONSE_UNKNOWN", "exchange response identity is missing"
        )
    item = data[0]
    order_id = item.get("ordId")
    if (
        item.get("sCode") != "0"
        or item.get("clOrdId") != client_order_id
        or not isinstance(order_id, str)
        or not order_id
    ):
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RESPONSE_UNKNOWN", "exchange order identity did not match"
        )
    return order_id, {"ordId": order_id, "clOrdId": client_order_id, "sCode": "0"}


def _safe_post_result(
    payload: Mapping[str, Any], *, client_order_id: str
) -> tuple[str, str | None, dict[str, str]]:
    try:
        return (
            "ACCEPTED",
            *_safe_exchange_identity(payload, client_order_id=client_order_id),
        )
    except CanonicalOrderRecoveryRequired:
        pass
    if set(payload).difference({"code", "msg", "data", "inTime", "outTime"}):
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RESPONSE_UNKNOWN", "exchange response envelope is unsafe"
        )
    code = payload.get("code")
    data = payload.get("data")
    if (
        not isinstance(code, str)
        or not code.isdigit()
        or not isinstance(data, list)
    ):
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RESPONSE_UNKNOWN",
            "exchange rejection was not explicit and safely attributable",
        )
    if code != "0" and not data:
        return (
            "REJECTED",
            None,
            {
                "code": code,
                "ordId": "",
                "clOrdId": client_order_id,
                "sCode": "",
            },
        )
    if len(data) != 1 or not isinstance(data[0], Mapping):
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RESPONSE_UNKNOWN",
            "exchange rejection was not explicitly attributable",
        )
    item = data[0]
    s_code = item.get("sCode")
    order_id = item.get("ordId")
    if (
        item.get("clOrdId") != client_order_id
        or not isinstance(s_code, str)
        or not s_code.isdigit()
        or s_code == "0"
        or order_id not in (None, "")
    ):
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RESPONSE_UNKNOWN",
            "exchange rejection identity did not match the request",
        )
    return (
        "REJECTED",
        None,
        {
            "code": code,
            "ordId": "",
            "clOrdId": client_order_id,
            "sCode": s_code,
        },
    )


def _load_dispatch(
    connection: Connection, order_id: UUID
) -> tuple[dict[str, Any], dict[str, str]]:
    effective = require_canonical_execution(connection)
    order = (
        effective.execute(select(ORDERS_TABLE).where(ORDERS_TABLE.c.id == order_id))
        .mappings()
        .one_or_none()
    )
    if order is None:
        raise CanonicalExecutionChainBlocked("BLOCKED_ORDER_UNSET", str(order_id))
    if order["receipt_digest"] is not None and order["exchange_order_id"]:
        return dict(order), {}
    decision = (
        effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
            )
        )
        .mappings()
        .one()
    )
    intent = (
        effective.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.id == decision["trade_intent_id"]
            )
        )
        .mappings()
        .one()
    )
    body = intent["intent_json"].get("exchange_body")
    if not isinstance(body, Mapping):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_BODY_UNSET",
            "intent must persist the exact exchange_body before dispatch",
        )
    return dict(order), _exchange_body(
        body, risk_decision_id=UUID(str(decision["id"]))
    )


def _load_dispatch_guard_inputs(
    connection: Connection, order_id: UUID
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any], dict[str, Any]]:
    effective = require_canonical_execution(connection)
    order, body = _load_dispatch(effective, order_id)
    if order["receipt_digest"] is not None and order["exchange_order_id"]:
        return order, body, {}, {}
    if order["status"] != "SUBMITTED":
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_GET_ONLY_RECOVERY_REQUIRED",
            "order dispatch was already claimed and must not POST again",
        )
    decision = effective.execute(
        select(RISK_DECISIONS_TABLE).where(
            RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
        )
    ).mappings().one()
    reservation = effective.execute(
        select(EXECUTION_RISK_RESERVATIONS_TABLE).where(
            EXECUTION_RISK_RESERVATIONS_TABLE.c.trade_intent_id
            == decision["trade_intent_id"]
        )
    ).mappings().one()
    budget = effective.execute(
        select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE).where(
            EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.id
            == reservation["risk_budget_authorization_id"]
        )
    ).mappings().one()
    policy = effective.execute(
        select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
            EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id
            == budget["execution_canary_risk_policy_id"]
        )
    ).mappings().one()
    attestation = effective.execute(
        select(EXECUTION_ATTESTATIONS_TABLE).where(
            EXECUTION_ATTESTATIONS_TABLE.c.id == policy["execution_attestation_id"]
        )
    ).mappings().one()
    return dict(order), body, dict(policy), dict(attestation)


def _guard_payload(guard: RedactedOkxDemoDispatchGuard) -> dict[str, object]:
    return redacted_dispatch_guard_payload(guard)


def _outcome_receipt_digest(outcome: Mapping[str, Any]) -> str:
    return canonical_execution_digest(
        {
            "contract": "canonical-v13-order-dispatch-outcome-v1",
            "outcome_id": str(outcome["id"]),
            "order_id": str(outcome["order_id"]),
            "dispatch_claim_id": str(outcome["dispatch_claim_id"]),
            "claim_digest": outcome["claim_digest"],
            "client_order_id": outcome["client_order_id"],
            "exchange_order_id": outcome["exchange_order_id"],
            "safe_response_digest": outcome["safe_response_digest"],
            "outcome_mode": outcome["outcome_mode"],
            "recorded_at": _persisted_utc(outcome["recorded_at"]).isoformat(),
        }
    )


def _absence_resource_digest(
    *, resource: str, safe: Mapping[str, Any], observed_at: datetime, expires_at: datetime
) -> str:
    return canonical_execution_digest(
        {
            "execution_target": "OKX_DEMO",
            "resource": resource,
            "source": "okx_demo_rest",
            "authenticated": True,
            "observed_at": _persisted_utc(observed_at).isoformat(),
            "expires_at": _persisted_utc(expires_at).isoformat(),
            "facts": {
                "instrument": safe.get("instrument"),
                "client_order_id": safe.get("client_order_id"),
                "matching_order_count": 0,
            },
        }
    )


def _negative_outcome_is_exact(
    *,
    outcome: Mapping[str, Any],
    claim: Mapping[str, Any],
    body: Mapping[str, str],
) -> bool:
    safe = outcome["safe_response_json"]
    if not isinstance(safe, Mapping):
        return False
    try:
        observed_at = datetime.fromisoformat(str(safe["observed_at"]))
        expires_at = datetime.fromisoformat(str(safe["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    return (
        outcome["outcome_mode"] == "GET_NOT_FOUND"
        and outcome["dispatch_claim_id"] == claim["id"]
        and outcome["claim_digest"] == claim["claim_digest"]
        and outcome["client_order_id"] == body.get("clOrdId")
        and outcome["exchange_order_id"] is None
        and safe.get("contract") == "canonical-v13-okx-demo-order-absence-v1"
        and safe.get("execution_target") == "OKX_DEMO"
        and safe.get("instrument") == body.get("instId")
        and safe.get("client_order_id") == body.get("clOrdId")
        and safe.get("account_fingerprint_digest")
        == claim["account_fingerprint_digest"]
        and safe.get("credential_generation_digest")
        == claim["credential_generation_digest"]
        and safe.get("exact_order_result_code") == "51603"
        and safe.get("pending_order_match_count") == 0
        and safe.get("history_order_match_count") == 0
        and observed_at.tzinfo is not None
        and expires_at.tzinfo is not None
        and observed_at < expires_at
        and safe.get("pending_orders_digest")
        == _absence_resource_digest(
            resource="pending_orders",
            safe=safe,
            observed_at=observed_at,
            expires_at=expires_at,
        )
        and safe.get("orders_history_digest")
        == _absence_resource_digest(
            resource="orders_history",
            safe=safe,
            observed_at=observed_at,
            expires_at=expires_at,
        )
        and outcome["safe_response_digest"]
        == canonical_execution_digest(dict(safe))
        and outcome["receipt_digest"] == _outcome_receipt_digest(outcome)
    )


def terminal_rejected_canary_order_evidence(
    connection: Connection,
    *,
    order_id: UUID,
    deployment_id: UUID,
) -> dict[str, object]:
    """Validate one exhausted two-attempt canary without rewriting its history."""

    effective = require_canonical_execution(connection)
    lock_execution_boundary(effective, key=f"canary-recovery-order:{order_id}")
    order, body = _load_dispatch(effective, order_id)
    decision = effective.execute(
        select(RISK_DECISIONS_TABLE).where(
            RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
        )
    ).mappings().one()
    intent = effective.execute(
        select(TRADE_INTENTS_TABLE).where(
            TRADE_INTENTS_TABLE.c.id == decision["trade_intent_id"]
        )
    ).mappings().one()
    signal = effective.execute(
        select(SIGNALS_TABLE).where(SIGNALS_TABLE.c.id == intent["signal_id"])
    ).mappings().one()
    claims = effective.execute(
        select(ORDER_DISPATCH_RECEIPTS_TABLE)
        .where(ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == order_id)
        .order_by(ORDER_DISPATCH_RECEIPTS_TABLE.c.attempt_ordinal)
    ).mappings().all()
    outcomes = effective.execute(
        select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE)
        .where(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.order_id == order_id)
        .order_by(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.recorded_at)
    ).mappings().all()
    outcomes_by_claim = {row["dispatch_claim_id"]: row for row in outcomes}
    fills = effective.execute(
        select(FILLS_TABLE.c.id).where(FILLS_TABLE.c.order_id == order_id)
    ).scalars().all()
    ledger_entries = (
        effective.execute(
            select(LEDGER_ENTRIES_TABLE.c.id).where(
                LEDGER_ENTRIES_TABLE.c.fill_id.in_(fills)
            )
        ).scalars().all()
        if fills
        else []
    )
    reconciliation_items = effective.execute(
        select(RECONCILIATION_ITEMS_TABLE.c.id).where(
            RECONCILIATION_ITEMS_TABLE.c.order_id == order_id
        )
    ).scalars().all()
    exact_negative = bool(
        order["status"] == "REJECTED"
        and order["exchange_order_id"] is None
        and order["receipt_digest"] is None
        and signal["deployment_id"] == deployment_id
        and len(claims) == 2
        and [row["attempt_ordinal"] for row in claims] == [1, 2]
        and len(outcomes) == 2
        and all(
            claim["id"] in outcomes_by_claim
            and _negative_outcome_is_exact(
                outcome=outcomes_by_claim[claim["id"]], claim=claim, body=body
            )
            for claim in claims
        )
        and not fills
        and not ledger_entries
        and not reconciliation_items
    )
    if not exact_negative:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_RECOVERY_TERMINAL_EVIDENCE",
            "exact two-attempt rejected order with zero downstream effects required",
        )
    evidence = {
        "contract": "canonical-v13-terminal-rejected-canary-evidence-v1",
        "deployment_id": str(deployment_id),
        "order_id": str(order_id),
        "risk_decision_id": str(order["risk_decision_id"]),
        "request_digest": order["request_digest"],
        "claim_digests": [row["claim_digest"] for row in claims],
        "outcome_receipt_digests": [
            outcomes_by_claim[row["id"]]["receipt_digest"] for row in claims
        ],
        "status": "REJECTED",
        "fill_count": 0,
        "ledger_entry_count": 0,
        "reconciliation_item_count": 0,
    }
    return {**evidence, "evidence_digest": canonical_execution_digest(evidence)}


def _claim_dispatch(
    connection: Connection,
    *,
    order_id: UUID,
    holder_identity: str,
    holder_token_digest: str,
    lease_generation: int,
    guard: RedactedOkxDemoDispatchGuard,
    evaluated_at: datetime | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    effective = require_canonical_execution(connection)
    now = _now(evaluated_at)
    holder_identity = require_identity(holder_identity, field="holder_identity")
    if holder_identity != CANONICAL_ORDER_WRITER_PROCESS_IDENTITY:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_WRITER_PROCESS_IDENTITY", holder_identity
        )
    require_digest(holder_token_digest, field="holder_token_digest")
    if (
        not isinstance(lease_generation, int)
        or isinstance(lease_generation, bool)
        or lease_generation <= 0
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_WRITER_LEASE_GENERATION",
            "writer lease generation must be positive",
        )
    lock_execution_boundary(effective, key=f"demo-order-dispatch:{order_id}")
    order, body = _load_dispatch(effective, order_id)
    if order["receipt_digest"] is not None and order["exchange_order_id"]:
        return order, body
    decision = (
        effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
            )
        )
        .mappings()
        .one()
    )
    reservation = (
        effective.execute(
            select(EXECUTION_RISK_RESERVATIONS_TABLE).where(
                EXECUTION_RISK_RESERVATIONS_TABLE.c.trade_intent_id
                == decision["trade_intent_id"]
            )
        )
        .mappings()
        .one()
    )
    budget = (
        effective.execute(
            select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE).where(
                EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.id
                == reservation["risk_budget_authorization_id"]
            )
        )
        .mappings()
        .one()
    )
    policy = (
        effective.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id
                == budget["execution_canary_risk_policy_id"]
            )
        )
        .mappings()
        .one()
    )
    attestation = (
        effective.execute(
            select(EXECUTION_ATTESTATIONS_TABLE).where(
                EXECUTION_ATTESTATIONS_TABLE.c.id == policy["execution_attestation_id"]
            )
        )
        .mappings()
        .one()
    )
    probe = (
        effective.execute(
            select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
                EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id
                == policy["probe_receipt_id"]
            )
        )
        .mappings()
        .one()
    )
    if (
        policy["status"] != "ACTIVE"
        or policy["receipt_digest"] != budget["source_receipt_digest"]
        or attestation["status"] != "READY"
        or policy["attestation_digest"] != attestation["attestation_digest"]
        or _persisted_utc(attestation["observed_at"]) > now
        or _persisted_utc(attestation["observed_at"])
        >= _persisted_utc(attestation["expires_at"])
        or probe["execution_attestation_id"] != attestation["id"]
        or probe["receipt_digest"] is None
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_AUTHORITY_STALE",
            "fresh exact policy and immutable attestation lineage are required before POST",
        )
    if not isinstance(guard, RedactedOkxDemoDispatchGuard):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_GUARD_TYPE",
            "sealed typed dispatch guard is required before POST",
        )
    guard_payload = _guard_payload(guard)
    guard_digest = canonical_execution_digest(guard_payload)
    try:
        guard_limit = Decimal(guard.limit_price)
        guard_leverage = Decimal(guard.effective_leverage)
        guard_minimum = Decimal(guard.minimum_size)
        guard_maximum = Decimal(guard.maximum_buy_contracts)
        guard_long = Decimal(guard.long_contracts)
        guard_short = Decimal(guard.short_contracts)
    except InvalidOperation:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_GUARD_VALUE",
            "dispatch guard decimal facts are invalid",
        ) from None
    guard_times = (
        guard.positions_observed_at,
        guard.positions_expires_at,
        guard.pending_orders_observed_at,
        guard.pending_orders_expires_at,
        guard.maximum_order_quantity_observed_at,
        guard.maximum_order_quantity_expires_at,
        guard.leverage_observed_at,
        guard.leverage_expires_at,
        guard.observed_at,
        guard.expires_at,
    )
    if any(value.tzinfo is None for value in guard_times):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_GUARD_FRESHNESS",
            "dispatch guard times must be timezone-aware",
        )
    observed = max(
        _persisted_utc(guard.positions_observed_at),
        _persisted_utc(guard.pending_orders_observed_at),
        _persisted_utc(guard.maximum_order_quantity_observed_at),
        _persisted_utc(guard.leverage_observed_at),
    )
    resource_expiry = min(
        _persisted_utc(guard.positions_expires_at),
        _persisted_utc(guard.pending_orders_expires_at),
        _persisted_utc(guard.maximum_order_quantity_expires_at),
        _persisted_utc(guard.leverage_expires_at),
    )
    if (
        guard.execution_target != "OKX_DEMO"
        or guard.instrument != body["instId"]
        or guard.account_fingerprint_digest != attestation["account_fingerprint_digest"]
        or guard.credential_generation_digest
        != attestation["credential_generation_digest"]
        or guard_limit != Decimal(str(policy["limit_price"]))
        or guard_limit != Decimal(body["px"])
        or guard_leverage != Decimal(str(policy["effective_leverage"]))
        or guard_minimum != Decimal(str(policy["minimum_contract_size"]))
        or guard_maximum < guard_minimum
        or guard_long != 0
        or guard_short != 0
        or guard.active_position_count != 0
        or guard.pending_order_count != 0
        or not all(
            value.is_finite()
            for value in (
                guard_limit,
                guard_leverage,
                guard_minimum,
                guard_maximum,
                guard_long,
                guard_short,
            )
        )
        or guard_limit <= 0
        or guard_leverage <= 0
        or guard_minimum <= 0
        or guard_maximum < 0
        or observed != _persisted_utc(guard.observed_at)
        or _persisted_utc(guard.expires_at) > resource_expiry
        or observed > now + timedelta(seconds=5)
        or _persisted_utc(guard.expires_at) <= now
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_GUARD_DRIFT",
            "fresh flat exact-capacity guard must match policy and credential lineage",
        )
    resource_contracts = (
        (
            "positions",
            guard.positions_observed_at,
            guard.positions_expires_at,
            guard.positions_digest,
            {
                "instrument": guard.instrument,
                "margin_mode": "isolated",
                "long_contracts": "0",
                "short_contracts": "0",
                "active_position_count": 0,
            },
        ),
        (
            "pending_orders",
            guard.pending_orders_observed_at,
            guard.pending_orders_expires_at,
            guard.pending_orders_digest,
            {"instrument": guard.instrument, "pending_order_count": 0},
        ),
        (
            "maximum_order_quantity",
            guard.maximum_order_quantity_observed_at,
            guard.maximum_order_quantity_expires_at,
            guard.maximum_order_quantity_digest,
            {
                "instrument": guard.instrument,
                "margin_mode": "isolated",
                "limit_price": guard.limit_price,
                "effective_leverage": guard.effective_leverage,
                "maximum_buy_contracts": guard.maximum_buy_contracts,
            },
        ),
        (
            "leverage",
            guard.leverage_observed_at,
            guard.leverage_expires_at,
            guard.leverage_digest,
            {
                "instrument": guard.instrument,
                "account_fingerprint_digest": guard.account_fingerprint_digest,
                "long": guard.effective_leverage,
                "short": guard.current_short_leverage,
            },
        ),
    )
    for resource, resource_observed, resource_expires, supplied, facts in resource_contracts:
        expected = canonical_execution_digest(
            {
                "execution_target": "OKX_DEMO",
                "resource": resource,
                "source": "okx_demo_rest",
                "authenticated": True,
                "observed_at": _persisted_utc(resource_observed).isoformat(),
                "expires_at": _persisted_utc(resource_expires).isoformat(),
                "facts": facts,
            }
        )
        if supplied != expected:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_ORDER_DISPATCH_GUARD_RESOURCE_DIGEST",
                f"{resource} dispatch guard digest drifted",
            )
    if order["status"] != "SUBMITTED":
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_GET_ONLY_RECOVERY_REQUIRED",
            "order dispatch was already claimed and must not POST again",
        )
    claims = (
        effective.execute(
            select(ORDER_DISPATCH_RECEIPTS_TABLE)
            .where(ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == order_id)
            .order_by(ORDER_DISPATCH_RECEIPTS_TABLE.c.attempt_ordinal)
        )
        .mappings()
        .all()
    )
    outcomes = (
        effective.execute(
            select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE).where(
                ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.order_id == order_id
            )
        )
        .mappings()
        .all()
    )
    if not claims and not outcomes:
        attempt_ordinal = 1
    elif (
        len(claims) == 1
        and claims[0]["attempt_ordinal"] == 1
        and len(outcomes) == 1
        and _negative_outcome_is_exact(
            outcome=outcomes[0], claim=claims[0], body=body
        )
    ):
        attempt_ordinal = 2
    else:
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RETRY_EVIDENCE",
            "a second POST requires one exact first-attempt absence receipt",
        )
    lease = (
        effective.execute(
            select(ORDER_WRITER_LEASES_TABLE).where(
                ORDER_WRITER_LEASES_TABLE.c.execution_target == "OKX_DEMO"
            )
        )
        .mappings()
        .one_or_none()
    )
    expected_lease_digest = (
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
        or lease["lease_digest"] != expected_lease_digest
        or not (
            _persisted_utc(lease["created_at"])
            <= now
            < _persisted_utc(lease["expires_at"])
        )
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_WRITER_LEASE_FENCED",
            "exact fresh canonical writer lease is required before POST",
        )
    result = effective.execute(
        ORDERS_TABLE.update()
        .where(
            ORDERS_TABLE.c.id == order_id,
            ORDERS_TABLE.c.status == "SUBMITTED",
            ORDERS_TABLE.c.receipt_digest.is_(None),
        )
        .values(status="DISPATCHING")
    )
    if result.rowcount != 1:
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_GET_ONLY_RECOVERY_REQUIRED",
            "order dispatch claim lost and must not POST",
        )
    claim = {
        "contract": "canonical-v13-okx-demo-order-dispatch-claim-v1",
        "order_id": str(order_id),
        "attempt_ordinal": attempt_ordinal,
        "request_digest": order["request_digest"],
        "holder_identity": holder_identity,
        "holder_token_digest": holder_token_digest,
        "lease_generation": lease_generation,
        "lease_digest": lease["lease_digest"],
        "lease_acquired_at": _persisted_utc(lease["created_at"]).isoformat(),
        "lease_expires_at": _persisted_utc(lease["expires_at"]).isoformat(),
        "risk_decision_id": str(decision["id"]),
        "canary_risk_policy_id": str(policy["id"]),
        "probe_receipt_id": str(probe["id"]),
        "execution_attestation_id": str(attestation["id"]),
        "guard_digest": guard_digest,
        "claimed_at": now.isoformat(),
    }
    effective.execute(
        ORDER_DISPATCH_RECEIPTS_TABLE.insert().values(
            order_id=order_id,
            risk_decision_id=decision["id"],
            canary_risk_policy_id=policy["id"],
            probe_receipt_id=probe["id"],
            execution_attestation_id=attestation["id"],
            attempt_ordinal=attempt_ordinal,
            request_digest=order["request_digest"],
            holder_identity=holder_identity,
            holder_token_digest=holder_token_digest,
            lease_generation=lease_generation,
            lease_digest=lease["lease_digest"],
            lease_acquired_at=lease["created_at"],
            lease_expires_at=lease["expires_at"],
            account_fingerprint_digest=guard.account_fingerprint_digest,
            credential_generation_digest=guard.credential_generation_digest,
            limit_price=guard_limit,
            effective_leverage=guard_leverage,
            minimum_size=guard_minimum,
            maximum_buy_contracts=guard_maximum,
            long_contracts=guard_long,
            short_contracts=guard_short,
            active_position_count=guard.active_position_count,
            pending_order_count=guard.pending_order_count,
            positions_digest=guard.positions_digest,
            positions_observed_at=guard.positions_observed_at,
            positions_expires_at=guard.positions_expires_at,
            pending_orders_digest=guard.pending_orders_digest,
            pending_orders_observed_at=guard.pending_orders_observed_at,
            pending_orders_expires_at=guard.pending_orders_expires_at,
            maximum_order_quantity_digest=guard.maximum_order_quantity_digest,
            maximum_order_quantity_observed_at=guard.maximum_order_quantity_observed_at,
            maximum_order_quantity_expires_at=guard.maximum_order_quantity_expires_at,
            guard_leverage_digest=guard.leverage_digest,
            guard_leverage_observed_at=guard.leverage_observed_at,
            guard_leverage_expires_at=guard.leverage_expires_at,
            guard_observed_at=guard.observed_at,
            guard_expires_at=guard.expires_at,
            guard_json=guard_payload,
            guard_digest=guard_digest,
            claim_digest=canonical_execution_digest(claim),
            claimed_at=now,
        )
    )
    return {**order, "status": "DISPATCHING"}, body


def _persist_exchange_receipt(
    connection: Connection,
    *,
    order_id: UUID,
    exchange_order_id: str,
    safe_response: Mapping[str, str],
    outcome_mode: str,
) -> DispatchedDemoOrder:
    effective = require_canonical_execution(connection)
    lock_execution_boundary(effective, key=f"demo-order-receipt:{order_id}")
    if outcome_mode not in {"POST", "GET_RECOVERY"}:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_OUTCOME_MODE", str(outcome_mode)
        )
    order, body = _load_dispatch(effective, order_id)
    claims = (
        effective.execute(
            select(ORDER_DISPATCH_RECEIPTS_TABLE)
            .where(ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == order_id)
            .order_by(ORDER_DISPATCH_RECEIPTS_TABLE.c.attempt_ordinal)
        )
        .mappings()
        .all()
    )
    existing_outcomes = (
        effective.execute(
            select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE).where(
                ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.order_id == order_id
            )
        )
        .mappings()
        .all()
    )
    if not claims:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_CLAIM_UNSET", str(order_id)
        )
    accepted = [
        row
        for row in existing_outcomes
        if row["outcome_mode"] in {"POST", "GET_RECOVERY"}
    ]
    if len(accepted) > 1:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_OUTCOME_DRIFT", str(order_id)
        )
    claim = claims[-1]
    existing = accepted[0] if accepted else None
    safe = dict(safe_response)
    if safe != {
        "ordId": exchange_order_id,
        "clOrdId": safe.get("clOrdId"),
        "sCode": "0",
    } or not safe.get("clOrdId"):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_OUTCOME_RESPONSE", "safe exchange identity is invalid"
        )
    safe_response_digest = canonical_execution_digest(safe)
    if existing is not None:
        expected_outcome = _outcome_receipt_digest(existing)
        expected_order = canonical_execution_digest(
            {
                "contract": "canonical-v13-okx-demo-order-receipt-v2",
                "order_id": str(order_id),
                "request_digest": order["request_digest"],
                "dispatch_claim_digest": claim["claim_digest"],
                "dispatch_outcome_receipt_digest": expected_outcome,
            }
        )
        if (
            existing["receipt_digest"] != expected_outcome
            or existing["dispatch_claim_id"] != claim["id"]
            or existing["claim_digest"] != claim["claim_digest"]
            or existing["client_order_id"] != safe["clOrdId"]
            or existing["exchange_order_id"] != exchange_order_id
            or existing["safe_response_json"] != safe
            or existing["safe_response_digest"] != safe_response_digest
            or existing["outcome_mode"] != outcome_mode
            or order["exchange_order_id"] != exchange_order_id
            or order["receipt_digest"] != expected_order
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_ORDER_DISPATCH_OUTCOME_DRIFT", str(order_id)
            )
        return DispatchedDemoOrder(order_id, exchange_order_id, expected_order, True)
    if (
        order["receipt_digest"] is not None
        or order["status"] != "DISPATCHING"
        or any(row["dispatch_claim_id"] == claim["id"] for row in existing_outcomes)
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_OUTCOME_DRIFT", str(order_id)
        )
    client_order_id = body.get("clOrdId")
    if client_order_id != safe["clOrdId"]:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_OUTCOME_RESPONSE", "client order identity drifted"
        )
    outcome_id = uuid4()
    recorded_at = datetime.now(timezone.utc)
    outcome_receipt_digest = canonical_execution_digest(
        {
            "contract": "canonical-v13-order-dispatch-outcome-v1",
            "outcome_id": str(outcome_id),
            "order_id": str(order_id),
            "dispatch_claim_id": str(claim["id"]),
            "claim_digest": claim["claim_digest"],
            "client_order_id": client_order_id,
            "exchange_order_id": exchange_order_id,
            "safe_response_digest": safe_response_digest,
            "outcome_mode": outcome_mode,
            "recorded_at": recorded_at.isoformat(),
        }
    )
    effective.execute(
        ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.insert().values(
            id=outcome_id,
            order_id=order_id,
            dispatch_claim_id=claim["id"],
            claim_digest=claim["claim_digest"],
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            safe_response_json=safe,
            safe_response_digest=safe_response_digest,
            outcome_mode=outcome_mode,
            receipt_digest=outcome_receipt_digest,
            recorded_at=recorded_at,
        )
    )
    receipt_digest = canonical_execution_digest(
        {
            "contract": "canonical-v13-okx-demo-order-receipt-v2",
            "order_id": str(order_id),
            "request_digest": order["request_digest"],
            "dispatch_claim_digest": claim["claim_digest"],
            "dispatch_outcome_receipt_digest": outcome_receipt_digest,
        }
    )
    updated = effective.execute(
        ORDERS_TABLE.update()
        .where(ORDERS_TABLE.c.id == order_id, ORDERS_TABLE.c.receipt_digest.is_(None))
        .values(
            exchange_order_id=exchange_order_id,
            status="ACCEPTED",
            receipt_digest=receipt_digest,
        )
    )
    if updated.rowcount != 1:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_OUTCOME_DRIFT", str(order_id)
        )
    return DispatchedDemoOrder(order_id, exchange_order_id, receipt_digest, False)


def _persist_nonaccepted_outcome(
    connection: Connection,
    *,
    order_id: UUID,
    safe_response: Mapping[str, object],
    outcome_mode: str,
) -> DispatchedDemoOrder:
    effective = require_canonical_execution(connection)
    lock_execution_boundary(effective, key=f"demo-order-receipt:{order_id}")
    if outcome_mode not in {"GET_NOT_FOUND", "POST_REJECTED"}:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_OUTCOME_MODE", str(outcome_mode)
        )
    order, body = _load_dispatch(effective, order_id)
    claims = (
        effective.execute(
            select(ORDER_DISPATCH_RECEIPTS_TABLE)
            .where(ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == order_id)
            .order_by(ORDER_DISPATCH_RECEIPTS_TABLE.c.attempt_ordinal)
        )
        .mappings()
        .all()
    )
    if not claims:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_CLAIM_UNSET", str(order_id)
        )
    claim = claims[-1]
    existing = (
        effective.execute(
            select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE).where(
                ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.dispatch_claim_id
                == claim["id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    safe = dict(safe_response)
    safe_digest = canonical_execution_digest(safe)
    if outcome_mode == "GET_NOT_FOUND":
        try:
            evidence = RedactedOkxDemoOrderAbsence(
                execution_target=str(safe["execution_target"]),
                instrument=str(safe["instrument"]),
                client_order_id=str(safe["client_order_id"]),
                account_fingerprint_digest=str(safe["account_fingerprint_digest"]),
                credential_generation_digest=str(
                    safe["credential_generation_digest"]
                ),
                exact_order_result_code=str(safe["exact_order_result_code"]),
                pending_order_match_count=int(safe["pending_order_match_count"]),
                history_order_match_count=int(safe["history_order_match_count"]),
                pending_orders_digest=str(safe["pending_orders_digest"]),
                orders_history_digest=str(safe["orders_history_digest"]),
                observed_at=datetime.fromisoformat(str(safe["observed_at"])),
                expires_at=datetime.fromisoformat(str(safe["expires_at"])),
            )
        except (KeyError, TypeError, ValueError):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_ORDER_ABSENCE_EVIDENCE", "absence evidence is malformed"
            ) from None
        if (
            safe != redacted_order_absence_payload(evidence)
            or evidence.execution_target != "OKX_DEMO"
            or evidence.instrument != body.get("instId")
            or evidence.client_order_id != body.get("clOrdId")
            or evidence.account_fingerprint_digest
            != claim["account_fingerprint_digest"]
            or evidence.credential_generation_digest
            != claim["credential_generation_digest"]
            or evidence.exact_order_result_code != "51603"
            or evidence.pending_order_match_count != 0
            or evidence.history_order_match_count != 0
            or evidence.observed_at.tzinfo is None
            or evidence.expires_at.tzinfo is None
            or not evidence.observed_at < evidence.expires_at
            or evidence.pending_orders_digest
            != _absence_resource_digest(
                resource="pending_orders",
                safe=safe,
                observed_at=evidence.observed_at,
                expires_at=evidence.expires_at,
            )
            or evidence.orders_history_digest
            != _absence_resource_digest(
                resource="orders_history",
                safe=safe,
                observed_at=evidence.observed_at,
                expires_at=evidence.expires_at,
            )
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_ORDER_ABSENCE_EVIDENCE", "absence evidence drifted"
            )
    elif safe != {
        "code": safe.get("code"),
        "ordId": "",
        "clOrdId": body.get("clOrdId"),
        "sCode": safe.get("sCode"),
    } or not (
        isinstance(safe.get("code"), str)
        and str(safe["code"]).isdigit()
        and isinstance(safe.get("sCode"), str)
        and (
            (str(safe["code"]) != "0" and safe["sCode"] == "")
            or (str(safe["sCode"]).isdigit() and str(safe["sCode"]) != "0")
        )
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_REJECTION_EVIDENCE", "explicit rejection evidence drifted"
        )
    if existing is not None:
        if (
            existing["order_id"] != order_id
            or existing["claim_digest"] != claim["claim_digest"]
            or existing["client_order_id"] != body.get("clOrdId")
            or existing["exchange_order_id"] is not None
            or existing["safe_response_json"] != safe
            or existing["safe_response_digest"] != safe_digest
            or existing["outcome_mode"] != outcome_mode
            or existing["receipt_digest"] != _outcome_receipt_digest(existing)
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_ORDER_DISPATCH_OUTCOME_DRIFT", str(order_id)
            )
        status = (
            "RETRY_READY"
            if outcome_mode == "GET_NOT_FOUND" and claim["attempt_ordinal"] == 1
            else "REJECTED"
        )
        return DispatchedDemoOrder(
            order_id,
            None,
            existing["receipt_digest"],
            True,
            status=status,
            retry_authorized=status == "RETRY_READY",
        )
    if order["status"] != "DISPATCHING" or order["receipt_digest"] is not None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_OUTCOME_DRIFT", str(order_id)
        )
    recorded_at = datetime.now(timezone.utc)
    if outcome_mode == "GET_NOT_FOUND" and not (
        _persisted_utc(evidence.observed_at)
        <= recorded_at
        < _persisted_utc(evidence.expires_at)
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_ABSENCE_EVIDENCE", "absence evidence expired before commit"
        )
    outcome_id = uuid4()
    outcome = {
        "id": outcome_id,
        "order_id": order_id,
        "dispatch_claim_id": claim["id"],
        "claim_digest": claim["claim_digest"],
        "client_order_id": body["clOrdId"],
        "exchange_order_id": None,
        "safe_response_digest": safe_digest,
        "outcome_mode": outcome_mode,
        "recorded_at": recorded_at,
    }
    receipt_digest = _outcome_receipt_digest(outcome)
    effective.execute(
        ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.insert().values(
            **outcome,
            safe_response_json=safe,
            receipt_digest=receipt_digest,
        )
    )
    retry_ready = outcome_mode == "GET_NOT_FOUND" and claim["attempt_ordinal"] == 1
    updated = effective.execute(
        ORDERS_TABLE.update()
        .where(
            ORDERS_TABLE.c.id == order_id,
            ORDERS_TABLE.c.status == "DISPATCHING",
            ORDERS_TABLE.c.receipt_digest.is_(None),
        )
        .values(status="SUBMITTED" if retry_ready else "REJECTED")
    )
    if updated.rowcount != 1:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_OUTCOME_DRIFT", str(order_id)
        )
    return DispatchedDemoOrder(
        order_id,
        None,
        receipt_digest,
        False,
        status="RETRY_READY" if retry_ready else "REJECTED",
        retry_authorized=retry_ready,
    )


def _replay_persisted_exchange_receipt(
    connection: Connection, *, order_id: UUID
) -> DispatchedDemoOrder:
    effective = require_canonical_execution(connection)
    outcome = (
        effective.execute(
            select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE).where(
                ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.order_id == order_id,
                ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.outcome_mode.in_(
                    ("POST", "GET_RECOVERY")
                ),
            )
        )
        .mappings()
        .one_or_none()
    )
    if outcome is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_DISPATCH_OUTCOME_UNSET", str(order_id)
        )
    return _persist_exchange_receipt(
        effective,
        order_id=order_id,
        exchange_order_id=str(outcome["exchange_order_id"]),
        safe_response=dict(outcome["safe_response_json"]),
        outcome_mode=str(outcome["outcome_mode"]),
    )


def dispatch_demo_order(
    connection_factory: ConnectionFactory,
    *,
    order_id: UUID,
    transport: DemoOrderTransport,
    holder_identity: str,
    holder_token_digest: str,
    lease_generation: int,
    evaluated_at: datetime | None = None,
) -> DispatchedDemoOrder:
    with connection_factory() as connection:
        order, body, policy, _attestation = _load_dispatch_guard_inputs(
            connection, order_id
        )
    if order["receipt_digest"] is not None and order["exchange_order_id"]:
        with connection_factory() as connection:
            return _replay_persisted_exchange_receipt(
                connection, order_id=order_id
            )
    guard = transport.dispatch_guard(
        instrument=body["instId"],
        limit_price=str(policy["limit_price"]),
        effective_leverage=str(policy["effective_leverage"]),
        minimum_size=str(policy["minimum_contract_size"]),
    )
    with connection_factory() as connection:
        order, body = _claim_dispatch(
            connection,
            order_id=order_id,
            holder_identity=holder_identity,
            holder_token_digest=holder_token_digest,
            lease_generation=lease_generation,
            guard=guard,
            evaluated_at=evaluated_at,
        )
    try:
        payload = transport.place(body)
        result_status, exchange_order_id, safe_response = _safe_post_result(
            payload, client_order_id=body["clOrdId"]
        )
    except CanonicalOrderRecoveryRequired:
        raise
    except Exception as exc:
        raise CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RECOVERY_REQUIRED",
            f"order outcome is unknown: {type(exc).__name__}",
        ) from None
    with connection_factory() as connection:
        if result_status == "REJECTED":
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


def recover_demo_order_get_only(
    connection_factory: ConnectionFactory,
    *,
    order_id: UUID,
    transport: DemoOrderTransport,
) -> DispatchedDemoOrder:
    with connection_factory() as connection:
        order, body = _load_dispatch(connection, order_id)
        latest_claim_id = connection.execute(
            select(ORDER_DISPATCH_RECEIPTS_TABLE.c.id)
            .where(ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == order_id)
            .order_by(ORDER_DISPATCH_RECEIPTS_TABLE.c.attempt_ordinal.desc())
            .limit(1)
        ).scalar_one_or_none()
        nonaccepted = (
            connection.execute(
                select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE)
                .where(
                    ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.dispatch_claim_id
                    == latest_claim_id,
                    ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.outcome_mode.in_(
                        ("GET_NOT_FOUND", "POST_REJECTED")
                    ),
                )
                .order_by(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.recorded_at.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if nonaccepted is not None:
            return _persist_nonaccepted_outcome(
                connection,
                order_id=order_id,
                safe_response=dict(nonaccepted["safe_response_json"]),
                outcome_mode=str(nonaccepted["outcome_mode"]),
            )
    if order["receipt_digest"] is not None and order["exchange_order_id"]:
        with connection_factory() as connection:
            return _replay_persisted_exchange_receipt(
                connection, order_id=order_id
            )
    try:
        payload = transport.query(
            instrument=body["instId"], client_order_id=body["clOrdId"]
        )
        exchange_order_id, safe_response = _safe_exchange_identity(
            payload, client_order_id=body["clOrdId"]
        )
    except Exception:
        try:
            absence = transport.prove_absent(
                instrument=body["instId"], client_order_id=body["clOrdId"]
            )
        except Exception as exc:
            raise CanonicalOrderRecoveryRequired(
                "BLOCKED_ORDER_RECOVERY_INCOMPLETE",
                f"GET-only recovery did not prove identity or absence: {type(exc).__name__}",
            ) from None
        with connection_factory() as connection:
            return _persist_nonaccepted_outcome(
                connection,
                order_id=order_id,
                safe_response=redacted_order_absence_payload(absence),
                outcome_mode="GET_NOT_FOUND",
            )
    with connection_factory() as connection:
        return _persist_exchange_receipt(
            connection,
            order_id=order_id,
            exchange_order_id=exchange_order_id,
            safe_response=safe_response,
            outcome_mode="GET_RECOVERY",
        )


def release_demo_order_writer_lease(
    connection: Connection,
    *,
    holder_identity: str,
    holder_token_digest: str,
    evaluated_at: datetime | None = None,
) -> dict[str, object]:
    """Release only the exact active DB writer lease after the canary saga."""

    effective = require_canonical_execution(connection)
    now = _now(evaluated_at)
    holder_identity = require_identity(holder_identity, field="holder_identity")
    require_digest(holder_token_digest, field="holder_token_digest")
    lease = (
        effective.execute(
            select(ORDER_WRITER_LEASES_TABLE).where(
                ORDER_WRITER_LEASES_TABLE.c.execution_target == "OKX_DEMO"
            )
        )
        .mappings()
        .one_or_none()
    )
    if lease is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_WRITER_LEASE_UNSET", "no canonical writer lease exists"
        )
    if (
        lease["holder_identity"] != holder_identity
        or lease["holder_token_digest"] != holder_token_digest
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_WRITER_LEASE_FENCED", "writer holder identity changed"
        )
    if lease["status"] == "RELEASED":
        return {
            "status": "RELEASED",
            "generation": int(lease["generation"]),
            "lease_digest": lease["lease_digest"],
            "repeat_noop": True,
        }
    if lease["status"] != "ACTIVE":
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ORDER_WRITER_LEASE_STATE", str(lease["status"])
        )
    released_digest = canonical_execution_digest(
        {
            "execution_target": "OKX_DEMO",
            "holder_identity": holder_identity,
            "holder_token_digest": holder_token_digest,
            "generation": int(lease["generation"]),
            "expires_at": now.isoformat(),
        }
    )
    effective.execute(
        ORDER_WRITER_LEASES_TABLE.update()
        .where(
            ORDER_WRITER_LEASES_TABLE.c.execution_target == "OKX_DEMO",
            ORDER_WRITER_LEASES_TABLE.c.lease_digest == lease["lease_digest"],
            ORDER_WRITER_LEASES_TABLE.c.status == "ACTIVE",
        )
        .values(status="RELEASED", expires_at=now, lease_digest=released_digest)
    )
    return {
        "status": "RELEASED",
        "generation": int(lease["generation"]),
        "lease_digest": released_digest,
        "repeat_noop": False,
    }


__all__ = [
    "CanonicalOrderRecoveryRequired",
    "CANONICAL_ORDER_WRITER_PROCESS_IDENTITY",
    "CancelledPreparedDemoOrder",
    "DemoOrderTransport",
    "DispatchedDemoOrder",
    "PRE_DISPATCH_CANCELLATION_CONTRACT",
    "PreparedDemoOrder",
    "assert_pre_dispatch_downstream_absent",
    "cancel_prepared_demo_order",
    "dispatch_demo_order",
    "pre_dispatch_cancellation_receipt_digest",
    "prepare_demo_order",
    "release_demo_order_writer_lease",
    "recover_demo_order_get_only",
    "terminal_rejected_canary_order_evidence",
]
