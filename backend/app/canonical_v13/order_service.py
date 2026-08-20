"""Order-writer-only TEST_SIMULATED idempotent receipt service."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    require_canonical_execution,
    require_identity,
)
from app.canonical_v13.models import ORDERS_TABLE, RISK_DECISIONS_TABLE


CANONICAL_ORDER_WRITER_IDENTITY = "canonical_order_writer"


def record_simulated_order(
    connection: Connection,
    *,
    risk_decision_id: UUID,
    writer_identity: str,
    idempotency_key: str,
    outcome: str,
) -> UUID:
    effective = require_canonical_execution(connection)
    writer_identity = require_identity(writer_identity, field="writer_identity")
    if writer_identity != CANONICAL_ORDER_WRITER_IDENTITY:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_NON_CANONICAL_ORDER_WRITER",
            "only the canonical order writer identity may persist an order receipt",
        )
    idempotency_key = require_identity(idempotency_key, field="idempotency_key")
    decision = effective.execute(
        select(RISK_DECISIONS_TABLE).where(
            RISK_DECISIONS_TABLE.c.id == risk_decision_id
        )
    ).mappings().one_or_none()
    if decision is None or decision["status"] != "RISK_ACCEPTED":
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_RISK_ACCEPTANCE_REQUIRED", str(risk_decision_id)
        )
    if outcome not in {"ACCEPTED", "REJECTED", "UNCERTAIN"}:
        raise CanonicalExecutionChainBlocked("BLOCKED_ORDER_OUTCOME", outcome)
    request = {
        "contract": "canonical-v13-simulated-order-v1",
        "risk_decision_id": str(risk_decision_id),
        "writer_identity": writer_identity,
        "idempotency_key": idempotency_key,
        "demo_only": True,
        "allow_real_funds": False,
        "evidence_class": "TEST_SIMULATED",
    }
    request_digest = canonical_execution_digest(request)
    existing = effective.execute(
        select(ORDERS_TABLE).where(ORDERS_TABLE.c.idempotency_key == idempotency_key)
    ).mappings().one_or_none()
    if existing is not None:
        if existing["request_digest"] != request_digest:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_ORDER_IDEMPOTENCY_DRIFT", idempotency_key
            )
        return existing["id"]
    order_id = uuid4()
    status = "SUBMITTED" if outcome == "UNCERTAIN" else outcome
    receipt_digest = None
    if outcome != "UNCERTAIN":
        receipt_digest = canonical_execution_digest(
            {"request_digest": request_digest, "outcome": outcome}
        )
    effective.execute(
        ORDERS_TABLE.insert().values(
            id=order_id,
            risk_decision_id=risk_decision_id,
            writer_identity=writer_identity,
            idempotency_key=idempotency_key,
            exchange_order_id=None,
            status=status,
            demo_only=True,
            allow_real_funds=False,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return order_id


__all__ = ["CANONICAL_ORDER_WRITER_IDENTITY", "record_simulated_order"]
