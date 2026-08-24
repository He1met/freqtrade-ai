"""Reversible PostgreSQL upgrade for the durable order dispatch state."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Final

from sqlalchemy import Connection, select, text

from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
)
from app.canonical_v13.models import ORDERS_TABLE, SCHEMA_METADATA_TABLE


CONTRACT: Final = "canonical-v13-order-dispatch-status-upgrade-v1"
CONSTRAINT_NAME: Final = "ck_orders_orders_status_values"
PREVIOUS_ORDER_STATUSES: Final[tuple[str, ...]] = (
    "INTENT_ACCEPTED",
    "RISK_ACCEPTED",
    "SUBMITTED",
    "ACCEPTED",
    "PARTIAL",
    "FILLED",
    "CANCELLED",
    "REJECTED",
)
ACCEPTED_ORDER_STATUSES: Final[tuple[str, ...]] = (
    "INTENT_ACCEPTED",
    "RISK_ACCEPTED",
    "SUBMITTED",
    "DISPATCHING",
    "ACCEPTED",
    "PARTIAL",
    "FILLED",
    "CANCELLED",
    "REJECTED",
)


class CanonicalOrderDispatchStatusUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderDispatchStatusUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    allowed_statuses: tuple[str, ...]
    order_count: int
    dispatching_order_count: int
    order_lineage_digest: str
    repeat_noop: bool
    receipt_digest: str


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _manifest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise CanonicalOrderDispatchStatusUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_STATUS_SCHEMA_METADATA"
        )
    return value


def _allowed_statuses(connection: Connection) -> tuple[str, ...]:
    definition = connection.execute(
        text(
            "SELECT pg_get_constraintdef(constraint_row.oid, true) "
            "FROM pg_constraint constraint_row "
            "JOIN pg_class table_row ON table_row.oid=constraint_row.conrelid "
            "JOIN pg_namespace namespace_row ON namespace_row.oid=table_row.relnamespace "
            "WHERE namespace_row.nspname=:schema AND table_row.relname='orders' "
            "AND constraint_row.conname=:constraint_name "
            "AND constraint_row.contype='c'"
        ),
        {
            "schema": CANONICAL_BUSINESS_SCHEMA,
            "constraint_name": CONSTRAINT_NAME,
        },
    ).scalar_one_or_none()
    if not isinstance(definition, str):
        raise CanonicalOrderDispatchStatusUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_STATUS_CONSTRAINT_UNSET"
        )
    return tuple(dict.fromkeys(re.findall(r"'([A-Z_]+)'", definition)))


def _lineage_digest(connection: Connection) -> str:
    rows = connection.execute(
        select(
            ORDERS_TABLE.c.id,
            ORDERS_TABLE.c.risk_decision_id,
            ORDERS_TABLE.c.status,
            ORDERS_TABLE.c.exchange_order_id,
            ORDERS_TABLE.c.request_digest,
            ORDERS_TABLE.c.receipt_digest,
        ).order_by(ORDERS_TABLE.c.created_at, ORDERS_TABLE.c.id)
    ).mappings()
    return _digest(
        [
            {
                key: str(value) if value is not None else None
                for key, value in row.items()
            }
            for row in rows
        ]
    )


def _result(
    connection: Connection, *, status: str, repeat_noop: bool
) -> OrderDispatchStatusUpgradeResult:
    allowed = _allowed_statuses(connection)
    order_count = int(
        connection.execute(select(text("count(*)")).select_from(ORDERS_TABLE)).scalar_one()
    )
    dispatching_count = int(
        connection.execute(
            select(text("count(*)"))
            .select_from(ORDERS_TABLE)
            .where(ORDERS_TABLE.c.status == "DISPATCHING")
        ).scalar_one()
    )
    payload = {
        "contract": CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "allowed_statuses": allowed,
        "order_count": order_count,
        "dispatching_order_count": dispatching_count,
        "order_lineage_digest": _lineage_digest(connection),
        "repeat_noop": repeat_noop,
    }
    return OrderDispatchStatusUpgradeResult(
        **payload,
        receipt_digest=_digest(payload),
    )


def verify_order_dispatch_status_upgrade(
    connection: Connection,
) -> OrderDispatchStatusUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalOrderDispatchStatusUpgradeBlocked(
            "BLOCKED_POSTGRESQL_REQUIRED"
        )
    if _manifest(connection) != CANONICAL_MANIFEST_DIGEST:
        raise CanonicalOrderDispatchStatusUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_STATUS_MANIFEST"
        )
    allowed = _allowed_statuses(connection)
    if allowed == PREVIOUS_ORDER_STATUSES:
        return _result(connection, status="PREVIOUS_READY", repeat_noop=True)
    if allowed != ACCEPTED_ORDER_STATUSES:
        raise CanonicalOrderDispatchStatusUpgradeBlocked(
            "BLOCKED_PARTIAL_ORDER_DISPATCH_STATUS_UPGRADE: "
            f"allowed_statuses={list(allowed)}"
        )
    invalid_count = int(
        connection.execute(
            text(
                f"SELECT count(*) FROM {CANONICAL_BUSINESS_SCHEMA}.orders "
                "WHERE status <> ALL(:allowed)"
            ),
            {"allowed": list(ACCEPTED_ORDER_STATUSES)},
        ).scalar_one()
    )
    if invalid_count:
        raise CanonicalOrderDispatchStatusUpgradeBlocked(
            f"BLOCKED_ORDER_DISPATCH_STATUS_ROWS: count={invalid_count}"
        )
    return _result(connection, status="ACCEPTED", repeat_noop=True)


def _replace_constraint(
    connection: Connection, *, allowed_statuses: tuple[str, ...]
) -> None:
    statuses = ", ".join(f"'{value}'" for value in allowed_statuses)
    connection.execute(
        text(
            f"ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.orders "
            f"DROP CONSTRAINT {CONSTRAINT_NAME}, "
            f"ADD CONSTRAINT {CONSTRAINT_NAME} CHECK (status IN ({statuses}))"
        )
    )


def apply_order_dispatch_status_upgrade(
    connection: Connection,
) -> OrderDispatchStatusUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_241_016},
    )
    before = verify_order_dispatch_status_upgrade(connection)
    if before.status == "ACCEPTED":
        return before
    _replace_constraint(connection, allowed_statuses=ACCEPTED_ORDER_STATUSES)
    verify_order_dispatch_status_upgrade(connection)
    return _result(connection, status="UPGRADED", repeat_noop=False)


def rollback_order_dispatch_status_upgrade(
    connection: Connection,
) -> OrderDispatchStatusUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_241_016},
    )
    before = verify_order_dispatch_status_upgrade(connection)
    if before.status == "PREVIOUS_READY":
        return before
    disallowed = int(
        connection.execute(
            text(
                f"SELECT count(*) FROM {CANONICAL_BUSINESS_SCHEMA}.orders "
                "WHERE status = 'DISPATCHING'"
            )
        ).scalar_one()
    )
    if disallowed:
        raise CanonicalOrderDispatchStatusUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_STATUS_ROLLBACK_IN_FLIGHT"
        )
    _replace_constraint(connection, allowed_statuses=PREVIOUS_ORDER_STATUSES)
    return _result(connection, status="ROLLED_BACK", repeat_noop=False)


__all__ = [
    "ACCEPTED_ORDER_STATUSES",
    "CONSTRAINT_NAME",
    "CanonicalOrderDispatchStatusUpgradeBlocked",
    "OrderDispatchStatusUpgradeResult",
    "PREVIOUS_ORDER_STATUSES",
    "apply_order_dispatch_status_upgrade",
    "rollback_order_dispatch_status_upgrade",
    "verify_order_dispatch_status_upgrade",
]
