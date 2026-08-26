"""Reconciliation-writer-only TEST_SIMULATED lineage service."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from sqlalchemy import Connection, func, select

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    require_canonical_execution,
)
from app.canonical_v13.models import (
    FILLS_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    LEDGER_ENTRIES_TABLE,
    ORDERS_TABLE,
    RECONCILIATION_ITEMS_TABLE,
    RECONCILIATION_RUNS_TABLE,
    RISK_DECISIONS_TABLE,
)
from app.canonical_v13.phase9_canary_policy import validate_persisted_canary_probe_receipt


def _decimal(value: object, *, field: str, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        result = Decimal("NaN")
    if not result.is_finite() or (result <= 0 if positive else False):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_RECONCILIATION_NUMBER", f"{field} is invalid"
        )
    return result


def reconcile_simulated_chain(
    connection: Connection,
    *,
    order_id: UUID,
    fill_id: UUID,
    ledger_entry_id: UUID,
) -> UUID:
    effective = require_canonical_execution(connection)
    existing = effective.execute(
        select(RECONCILIATION_ITEMS_TABLE.c.reconciliation_run_id).where(
            RECONCILIATION_ITEMS_TABLE.c.order_id == order_id,
            RECONCILIATION_ITEMS_TABLE.c.fill_id == fill_id,
            RECONCILIATION_ITEMS_TABLE.c.ledger_entry_id == ledger_entry_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    fill = (
        effective.execute(
            select(FILLS_TABLE).where(
                FILLS_TABLE.c.id == fill_id, FILLS_TABLE.c.order_id == order_id
            )
        )
        .mappings()
        .one_or_none()
    )
    ledger = (
        effective.execute(
            select(LEDGER_ENTRIES_TABLE).where(
                LEDGER_ENTRIES_TABLE.c.id == ledger_entry_id,
                LEDGER_ENTRIES_TABLE.c.fill_id == fill_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if fill is None or ledger is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_RECONCILIATION_LINEAGE", "order/fill/ledger chain differs"
        )
    scope = {
        "order_id": str(order_id),
        "fill_id": str(fill_id),
        "ledger_entry_id": str(ledger_entry_id),
        "evidence_class": "TEST_SIMULATED",
    }
    run_id = uuid4()
    scope_digest = canonical_execution_digest(scope)
    now = datetime.now(timezone.utc)
    effective.execute(
        RECONCILIATION_RUNS_TABLE.insert().values(
            id=run_id,
            status="SUCCEEDED",
            scope_digest=scope_digest,
            receipt_digest=canonical_execution_digest(
                {"run_id": str(run_id), "scope_digest": scope_digest}
            ),
            created_at=now,
            completed_at=now,
        )
    )
    effective.execute(
        RECONCILIATION_ITEMS_TABLE.insert().values(
            id=uuid4(),
            reconciliation_run_id=run_id,
            order_id=order_id,
            fill_id=fill_id,
            ledger_entry_id=ledger_entry_id,
            item_type="ORDER_FILL_LEDGER_CHAIN",
            status="MATCHED",
            evidence_json=scope,
            evidence_digest=scope_digest,
        )
    )
    return run_id


def reconcile_production_demo_chain(
    connection: Connection,
    *,
    order_id: UUID,
    fill_id: UUID,
    ledger_entry_id: UUID,
    flat_probe_receipt_id: UUID | None = None,
    evaluated_at: datetime | None = None,
) -> UUID:
    effective = require_canonical_execution(connection)
    existing = effective.execute(
        select(RECONCILIATION_ITEMS_TABLE.c.reconciliation_run_id).where(
            RECONCILIATION_ITEMS_TABLE.c.order_id == order_id,
            RECONCILIATION_ITEMS_TABLE.c.fill_id == fill_id,
            RECONCILIATION_ITEMS_TABLE.c.ledger_entry_id == ledger_entry_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    order = (
        effective.execute(select(ORDERS_TABLE).where(ORDERS_TABLE.c.id == order_id))
        .mappings()
        .one_or_none()
    )
    fill = (
        effective.execute(
            select(FILLS_TABLE).where(
                FILLS_TABLE.c.id == fill_id, FILLS_TABLE.c.order_id == order_id
            )
        )
        .mappings()
        .one_or_none()
    )
    ledger = (
        effective.execute(
            select(LEDGER_ENTRIES_TABLE).where(
                LEDGER_ENTRIES_TABLE.c.id == ledger_entry_id,
                LEDGER_ENTRIES_TABLE.c.fill_id == fill_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        order is None
        or order["exchange_order_id"] is None
        or order["receipt_digest"] is None
        or order["demo_only"] is not True
        or order["allow_real_funds"] is not False
        or fill is None
        or fill["fill_json"].get("evidence_class") != "PRODUCTION_OKX_DEMO"
        or fill["fill_json"].get("exchange_order_id") != order["exchange_order_id"]
        or ledger is None
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_RECONCILIATION_LINEAGE",
            "Demo order/fill/ledger exact lineage is incomplete",
        )
    fill_payload = fill["fill_json"]
    side = fill_payload.get("side")
    size = _decimal(fill_payload.get("size"), field="fill.size", positive=True)
    amount = _decimal(ledger["amount"], field="ledger.amount")
    expected_amount = size if side == "buy" else -size
    if (
        side not in {"buy", "sell"}
        or abs(amount - expected_amount) > Decimal("0.000000000001")
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_RECONCILIATION_DIRECTION",
            "fill direction and canonical contract ledger differ",
        )
    flat_probe = None
    if side == "sell":
        if flat_probe_receipt_id is None:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_PRODUCTION_RECONCILIATION_FLAT_PROOF",
                "a fresh persisted flat private probe is required after close",
            )
        now = evaluated_at or datetime.now(timezone.utc)
        decision = effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
            )
        ).mappings().one()
        validate_persisted_canary_probe_receipt(
            effective,
            probe_receipt_id=flat_probe_receipt_id,
            evaluated_at=now,
            strategy_max_leverage=_decimal(
                decision["decision_json"].get("strategy_max_leverage"),
                field="strategy_max_leverage",
                positive=True,
            ),
        )
        flat_probe = effective.execute(
            select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
                EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id == flat_probe_receipt_id
            )
        ).mappings().one()
        ledger_net = Decimal(
            str(
                effective.execute(
                    select(func.coalesce(func.sum(LEDGER_ENTRIES_TABLE.c.amount), 0)).where(
                        LEDGER_ENTRIES_TABLE.c.asset == fill_payload.get("instrument")
                    )
                ).scalar_one()
            )
        )
        if ledger_net != 0:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_PRODUCTION_RECONCILIATION_POSITION_LEDGER",
                "post-close canonical contract ledger is not flat",
            )
    scope = {
        "contract": "canonical-v13-okx-demo-reconciliation-v1",
        "order_id": str(order_id),
        "order_receipt_digest": order["receipt_digest"],
        "exchange_order_id": order["exchange_order_id"],
        "fill_id": str(fill_id),
        "fill_receipt_digest": fill["receipt_digest"],
        "ledger_entry_id": str(ledger_entry_id),
        "ledger_entry_digest": ledger["entry_digest"],
        "evidence_class": "PRODUCTION_OKX_DEMO",
        "allow_real_funds": False,
        "side": side,
        "position_side": fill_payload.get("position_side"),
        "flat_probe_receipt_id": (
            str(flat_probe_receipt_id) if flat_probe_receipt_id is not None else None
        ),
        "flat_probe_receipt_digest": (
            flat_probe["receipt_digest"] if flat_probe is not None else None
        ),
    }
    run_id = uuid4()
    scope_digest = canonical_execution_digest(scope)
    now = datetime.now(timezone.utc)
    receipt_digest = canonical_execution_digest(
        {"run_id": str(run_id), "scope_digest": scope_digest}
    )
    effective.execute(
        RECONCILIATION_RUNS_TABLE.insert().values(
            id=run_id,
            status="SUCCEEDED",
            scope_digest=scope_digest,
            receipt_digest=receipt_digest,
            created_at=now,
            completed_at=now,
        )
    )
    effective.execute(
        RECONCILIATION_ITEMS_TABLE.insert().values(
            id=uuid4(),
            reconciliation_run_id=run_id,
            order_id=order_id,
            fill_id=fill_id,
            ledger_entry_id=ledger_entry_id,
            item_type="OKX_DEMO_ORDER_FILL_LEDGER_CHAIN",
            status="MATCHED",
            evidence_json=scope,
            evidence_digest=scope_digest,
        )
    )
    return run_id


__all__ = ["reconcile_production_demo_chain", "reconcile_simulated_chain"]
