"""Fill-writer-only TEST_SIMULATED immutable receipt service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    require_canonical_execution,
    require_identity,
)
from app.canonical_v13.models import FILLS_TABLE, ORDERS_TABLE


def record_simulated_fill(
    connection: Connection,
    *,
    order_id: UUID,
    exchange_fill_id: str,
    fill_json: Mapping[str, object],
) -> UUID:
    effective = require_canonical_execution(connection)
    exchange_fill_id = require_identity(exchange_fill_id, field="exchange_fill_id")
    order = (
        effective.execute(select(ORDERS_TABLE).where(ORDERS_TABLE.c.id == order_id))
        .mappings()
        .one_or_none()
    )
    if (
        order is None
        or order["status"] != "ACCEPTED"
        or order["receipt_digest"] is None
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_ACCEPTED_ORDER_REQUIRED", str(order_id)
        )
    payload = dict(fill_json)
    if payload.get("evidence_class") != "TEST_SIMULATED":
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_REAL_FILL_OUT_OF_SCOPE", "only isolated fills are allowed"
        )
    receipt_digest = canonical_execution_digest(payload)
    existing = (
        effective.execute(
            select(FILLS_TABLE).where(
                FILLS_TABLE.c.exchange_fill_id == exchange_fill_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if (
            existing["order_id"] != order_id
            or existing["receipt_digest"] != receipt_digest
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_FILL_IDEMPOTENCY_DRIFT", exchange_fill_id
            )
        return existing["id"]
    fill_id = uuid4()
    effective.execute(
        FILLS_TABLE.insert().values(
            id=fill_id,
            order_id=order_id,
            exchange_fill_id=exchange_fill_id,
            fill_json=payload,
            receipt_digest=receipt_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return fill_id


def record_production_demo_fill(
    connection: Connection,
    *,
    order_id: UUID,
    exchange_fill_id: str,
    fill_json: Mapping[str, object],
) -> UUID:
    effective = require_canonical_execution(connection)
    exchange_fill_id = require_identity(exchange_fill_id, field="exchange_fill_id")
    order = (
        effective.execute(select(ORDERS_TABLE).where(ORDERS_TABLE.c.id == order_id))
        .mappings()
        .one_or_none()
    )
    payload = dict(fill_json)
    if (
        order is None
        or order["status"] not in {"ACCEPTED", "PARTIAL", "FILLED"}
        or order["receipt_digest"] is None
        or order["exchange_order_id"] is None
        or payload.get("evidence_class") != "PRODUCTION_OKX_DEMO"
        or payload.get("allow_real_funds") is not False
        or payload.get("exchange_order_id") != order["exchange_order_id"]
        or payload.get("exchange_fill_id") != exchange_fill_id
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_FILL_LINEAGE",
            "fill must bind the exact accepted Demo exchange order identity",
        )
    receipt_digest = canonical_execution_digest(
        {
            "contract": "canonical-v13-okx-demo-fill-v1",
            "order_id": str(order_id),
            "order_receipt_digest": order["receipt_digest"],
            "fill": payload,
        }
    )
    existing = (
        effective.execute(
            select(FILLS_TABLE).where(
                FILLS_TABLE.c.exchange_fill_id == exchange_fill_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if (
            existing["order_id"] != order_id
            or existing["receipt_digest"] != receipt_digest
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_FILL_IDEMPOTENCY_DRIFT", exchange_fill_id
            )
        return existing["id"]
    fill_id = uuid4()
    effective.execute(
        FILLS_TABLE.insert().values(
            id=fill_id,
            order_id=order_id,
            exchange_fill_id=exchange_fill_id,
            fill_json=payload,
            receipt_digest=receipt_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return fill_id


__all__ = ["record_production_demo_fill", "record_simulated_fill"]
