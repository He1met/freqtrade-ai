"""Reconciliation-writer-only TEST_SIMULATED lineage service."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    require_canonical_execution,
)
from app.canonical_v13.models import (
    FILLS_TABLE,
    LEDGER_ENTRIES_TABLE,
    RECONCILIATION_ITEMS_TABLE,
    RECONCILIATION_RUNS_TABLE,
)


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
    fill = effective.execute(
        select(FILLS_TABLE).where(
            FILLS_TABLE.c.id == fill_id, FILLS_TABLE.c.order_id == order_id
        )
    ).mappings().one_or_none()
    ledger = effective.execute(
        select(LEDGER_ENTRIES_TABLE).where(
            LEDGER_ENTRIES_TABLE.c.id == ledger_entry_id,
            LEDGER_ENTRIES_TABLE.c.fill_id == fill_id,
        )
    ).mappings().one_or_none()
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


__all__ = ["reconcile_simulated_chain"]
