"""Ledger-writer-only TEST_SIMULATED immutable posting service."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    require_canonical_execution,
    require_identity,
)
from app.canonical_v13.models import FILLS_TABLE, LEDGER_ENTRIES_TABLE


def post_simulated_ledger_entry(
    connection: Connection,
    *,
    fill_id: UUID,
    entry_key: str,
    asset: str,
    amount: Decimal,
    entry_type: str,
) -> UUID:
    effective = require_canonical_execution(connection)
    entry_key = require_identity(entry_key, field="entry_key")
    asset = require_identity(asset, field="asset", maximum=24)
    entry_type = require_identity(entry_type, field="entry_type", maximum=40)
    if not amount.is_finite() or amount == 0:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_LEDGER_AMOUNT", "ledger amount must be finite and non-zero"
        )
    fill = (
        effective.execute(select(FILLS_TABLE).where(FILLS_TABLE.c.id == fill_id))
        .mappings()
        .one_or_none()
    )
    if fill is None:
        raise CanonicalExecutionChainBlocked("BLOCKED_FILL_UNSET", str(fill_id))
    payload = {
        "fill_id": str(fill_id),
        "fill_receipt_digest": fill["receipt_digest"],
        "entry_key": entry_key,
        "asset": asset,
        "amount": str(amount),
        "entry_type": entry_type,
        "evidence_class": "TEST_SIMULATED",
    }
    entry_digest = canonical_execution_digest(payload)
    existing = (
        effective.execute(
            select(LEDGER_ENTRIES_TABLE).where(
                LEDGER_ENTRIES_TABLE.c.entry_key == entry_key
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["fill_id"] != fill_id or existing["entry_digest"] != entry_digest:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_LEDGER_IDEMPOTENCY_DRIFT", entry_key
            )
        return existing["id"]
    entry_id = uuid4()
    effective.execute(
        LEDGER_ENTRIES_TABLE.insert().values(
            id=entry_id,
            fill_id=fill_id,
            entry_key=entry_key,
            asset=asset,
            amount=amount,
            entry_type=entry_type,
            entry_digest=entry_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return entry_id


def post_production_demo_ledger_entry(
    connection: Connection,
    *,
    fill_id: UUID,
    entry_key: str,
    asset: str,
    amount: Decimal,
    entry_type: str,
) -> UUID:
    effective = require_canonical_execution(connection)
    entry_key = require_identity(entry_key, field="entry_key")
    asset = require_identity(asset, field="asset", maximum=24)
    entry_type = require_identity(entry_type, field="entry_type", maximum=40)
    if not amount.is_finite() or amount == 0:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_LEDGER_AMOUNT", "ledger amount must be finite and non-zero"
        )
    fill = (
        effective.execute(select(FILLS_TABLE).where(FILLS_TABLE.c.id == fill_id))
        .mappings()
        .one_or_none()
    )
    if (
        fill is None
        or fill["fill_json"].get("evidence_class") != "PRODUCTION_OKX_DEMO"
        or fill["fill_json"].get("allow_real_funds") is not False
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_PRODUCTION_FILL_REQUIRED", str(fill_id)
        )
    payload = {
        "contract": "canonical-v13-okx-demo-ledger-entry-v1",
        "fill_id": str(fill_id),
        "fill_receipt_digest": fill["receipt_digest"],
        "entry_key": entry_key,
        "asset": asset,
        "amount": str(amount),
        "entry_type": entry_type,
        "evidence_class": "PRODUCTION_OKX_DEMO",
        "allow_real_funds": False,
    }
    entry_digest = canonical_execution_digest(payload)
    existing = (
        effective.execute(
            select(LEDGER_ENTRIES_TABLE).where(
                LEDGER_ENTRIES_TABLE.c.entry_key == entry_key
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["fill_id"] != fill_id or existing["entry_digest"] != entry_digest:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_LEDGER_IDEMPOTENCY_DRIFT", entry_key
            )
        return existing["id"]
    entry_id = uuid4()
    effective.execute(
        LEDGER_ENTRIES_TABLE.insert().values(
            id=entry_id,
            fill_id=fill_id,
            entry_key=entry_key,
            asset=asset,
            amount=amount,
            entry_type=entry_type,
            entry_digest=entry_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    return entry_id


__all__ = ["post_production_demo_ledger_entry", "post_simulated_ledger_entry"]
