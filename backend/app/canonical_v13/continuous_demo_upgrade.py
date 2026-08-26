"""Reversible PostgreSQL upgrade for bounded continuous OKX_DEMO execution.

The upgrade keeps the proven one-shot canary rows immutable while adding two
independent decision modes and a mode-aware dispatch receipt shape.  It does not
add trading capability, an endpoint, a credential, or a real-funds path.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Final

from sqlalchemy import Connection, inspect, select, text

from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA, CANONICAL_MANIFEST_DIGEST
from app.canonical_v13.models import SCHEMA_METADATA_TABLE


CONTRACT: Final = "canonical-v13-bounded-continuous-demo-upgrade-v1"
INTENT_CHECK: Final = "ck_trade_intents_trade_intents_intent_mode_values"
DECISION_CHECK: Final = "ck_risk_decisions_risk_decisions_decision_mode_values"
OLD_DISPATCH_SHAPE: Final = "ck_order_dispatch_receipts_order_dispatch_receipts_flat_85c4"
# SQLAlchemy deterministically shortens overlength PostgreSQL identifiers with
# a hash suffix. Keep those exact persisted names so fresh metadata-created
# schemas and upgraded schemas verify identically.
DISPATCH_FRESHNESS: Final = "ck_order_dispatch_receipts_order_dispatch_receipts_guar_98d1"
DISPATCH_AUTHORITY_MODE: Final = "ck_order_dispatch_receipts_order_dispatch_receipts_auth_033f"
DISPATCH_MODE_SHAPE: Final = "ck_order_dispatch_receipts_order_dispatch_receipts_mode_shape"
DISPATCH_MODE_GUARD_FUNCTION: Final = "guard_order_dispatch_mode_immutable"
DISPATCH_MODE_GUARD_TRIGGER: Final = "order_dispatch_mode_immutable"
PREVIOUS_MODES: Final[tuple[str, ...]] = (
    "TEST_SIMULATED",
    "SIGNAL_RISK_SHADOW",
    "EXECUTION",
)
ACCEPTED_MODES: Final[tuple[str, ...]] = (
    *PREVIOUS_MODES,
    "CONTINUOUS_OPEN",
    "POSITION_EXIT",
)
ADDED_COLUMNS: Final[tuple[str, ...]] = (
    "dispatch_mode",
    "reference_price",
    "maximum_close_contracts",
    "close_capacity_digest",
    "close_capacity_observed_at",
    "close_capacity_expires_at",
)


class CanonicalContinuousDemoUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class ContinuousDemoUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    accepted_modes: tuple[str, ...]
    dispatch_columns_present: tuple[str, ...]
    dispatch_constraints_present: tuple[str, ...]
    dispatch_guard_trigger_present: bool
    intent_mode_counts: dict[str, int]
    decision_mode_counts: dict[str, int]
    dispatch_mode_counts: dict[str, int]
    lineage_digest: str
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
        raise CanonicalContinuousDemoUpgradeBlocked(
            "BLOCKED_CONTINUOUS_DEMO_SCHEMA_METADATA"
        )
    return value


def _columns(connection: Connection) -> dict[str, dict[str, object]]:
    return {
        str(item["name"]): dict(item)
        for item in inspect(connection).get_columns(
            "order_dispatch_receipts", schema=CANONICAL_BUSINESS_SCHEMA
        )
    }


def _constraint_names(connection: Connection) -> tuple[str, ...]:
    expected = {
        DISPATCH_AUTHORITY_MODE,
        DISPATCH_MODE_SHAPE,
        DISPATCH_FRESHNESS,
    }
    return tuple(
        sorted(
            str(item["name"])
            for item in inspect(connection).get_check_constraints(
                "order_dispatch_receipts", schema=CANONICAL_BUSINESS_SCHEMA
            )
            if item.get("name") in expected
        )
    )


def _check_modes(connection: Connection, table: str, constraint: str) -> tuple[str, ...]:
    definition = connection.execute(
        text(
            "SELECT pg_get_constraintdef(con.oid, true) FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid=con.conrelid "
            "JOIN pg_namespace ns ON ns.oid=rel.relnamespace "
            "WHERE ns.nspname=:schema AND rel.relname=:table "
            "AND con.conname=:constraint AND con.contype='c'"
        ),
        {"schema": CANONICAL_BUSINESS_SCHEMA, "table": table, "constraint": constraint},
    ).scalar_one_or_none()
    if not isinstance(definition, str):
        raise CanonicalContinuousDemoUpgradeBlocked(
            f"BLOCKED_CONTINUOUS_DEMO_MODE_CONSTRAINT:{table}"
        )
    return tuple(value for value in ACCEPTED_MODES if f"'{value}'" in definition)


def _trigger_present(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT count(*) FROM pg_trigger trigger "
                "JOIN pg_class rel ON rel.oid=trigger.tgrelid "
                "JOIN pg_namespace ns ON ns.oid=rel.relnamespace "
                "WHERE ns.nspname=:schema AND rel.relname='order_dispatch_receipts' "
                "AND trigger.tgname=:trigger AND trigger.tgenabled='O' "
                "AND NOT trigger.tgisinternal"
            ),
            {"schema": CANONICAL_BUSINESS_SCHEMA, "trigger": DISPATCH_MODE_GUARD_TRIGGER},
        ).scalar_one()
    )


def _trigger_exists(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT count(*) FROM pg_trigger trigger "
                "JOIN pg_class rel ON rel.oid=trigger.tgrelid "
                "JOIN pg_namespace ns ON ns.oid=rel.relnamespace "
                "WHERE ns.nspname=:schema AND rel.relname='order_dispatch_receipts' "
                "AND trigger.tgname=:trigger AND NOT trigger.tgisinternal"
            ),
            {"schema": CANONICAL_BUSINESS_SCHEMA, "trigger": DISPATCH_MODE_GUARD_TRIGGER},
        ).scalar_one()
    )


def _counts(connection: Connection, table: str, column: str, *, present: bool = True) -> dict[str, int]:
    if not present:
        return {}
    rows = connection.execute(
        text(
            f"SELECT {column}, count(*) FROM {CANONICAL_BUSINESS_SCHEMA}.{table} "
            f"GROUP BY {column} ORDER BY {column}"
        )
    ).all()
    return {str(mode): int(count) for mode, count in rows}


def _lineage_digest(connection: Connection, *, upgraded: bool) -> str:
    dispatch_columns = (
        "id, order_id, risk_decision_id, dispatch_mode, request_digest, claim_digest"
        if upgraded
        else "id, order_id, risk_decision_id, request_digest, claim_digest"
    )
    payload: dict[str, object] = {}
    for key, table, columns in (
        ("intents", "trade_intents", "id, signal_id, intent_mode, intent_digest"),
        ("decisions", "risk_decisions", "id, trade_intent_id, decision_mode, decision_digest"),
        ("dispatches", "order_dispatch_receipts", dispatch_columns),
    ):
        rows = connection.execute(
            text(
                f"SELECT {columns} FROM {CANONICAL_BUSINESS_SCHEMA}.{table} ORDER BY id"
            )
        ).mappings()
        payload[key] = [
            {name: str(value) if value is not None else None for name, value in row.items()}
            for row in rows
        ]
    return _digest(payload)


def _result(
    connection: Connection, *, status: str, repeat_noop: bool
) -> ContinuousDemoUpgradeResult:
    columns = _columns(connection)
    upgraded = set(ADDED_COLUMNS).issubset(columns)
    intent_modes = _check_modes(connection, "trade_intents", INTENT_CHECK)
    decision_modes = _check_modes(connection, "risk_decisions", DECISION_CHECK)
    payload = {
        "contract": CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "accepted_modes": intent_modes if intent_modes == decision_modes else (),
        "dispatch_columns_present": tuple(sorted(set(columns).intersection(ADDED_COLUMNS))),
        "dispatch_constraints_present": _constraint_names(connection),
        "dispatch_guard_trigger_present": _trigger_present(connection),
        "intent_mode_counts": _counts(connection, "trade_intents", "intent_mode"),
        "decision_mode_counts": _counts(connection, "risk_decisions", "decision_mode"),
        "dispatch_mode_counts": _counts(
            connection, "order_dispatch_receipts", "dispatch_mode", present=upgraded
        ),
        "lineage_digest": _lineage_digest(connection, upgraded=upgraded),
        "repeat_noop": repeat_noop,
    }
    return ContinuousDemoUpgradeResult(**payload, receipt_digest=_digest(payload))


def verify_continuous_demo_upgrade(connection: Connection) -> ContinuousDemoUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalContinuousDemoUpgradeBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    if _manifest(connection) != CANONICAL_MANIFEST_DIGEST:
        raise CanonicalContinuousDemoUpgradeBlocked("BLOCKED_CONTINUOUS_DEMO_MANIFEST")
    columns = _columns(connection)
    present = set(columns).intersection(ADDED_COLUMNS)
    intent_modes = _check_modes(connection, "trade_intents", INTENT_CHECK)
    decision_modes = _check_modes(connection, "risk_decisions", DECISION_CHECK)
    if not present and intent_modes == decision_modes == PREVIOUS_MODES:
        return _result(connection, status="PREVIOUS_READY", repeat_noop=True)
    fresh_schema_without_guard = (
        present == set(ADDED_COLUMNS)
        and intent_modes == decision_modes == ACCEPTED_MODES
        and set(_constraint_names(connection))
        == {DISPATCH_AUTHORITY_MODE, DISPATCH_MODE_SHAPE, DISPATCH_FRESHNESS}
        and not _trigger_exists(connection)
    )
    if fresh_schema_without_guard:
        return _result(connection, status="PREVIOUS_READY", repeat_noop=True)
    problems: list[str] = []
    if present != set(ADDED_COLUMNS):
        problems.append(f"dispatch_columns={sorted(present)}")
    if intent_modes != ACCEPTED_MODES:
        problems.append(f"intent_modes={list(intent_modes)}")
    if decision_modes != ACCEPTED_MODES:
        problems.append(f"decision_modes={list(decision_modes)}")
    expected_constraints = {
        DISPATCH_AUTHORITY_MODE,
        DISPATCH_MODE_SHAPE,
        DISPATCH_FRESHNESS,
    }
    if set(_constraint_names(connection)) != expected_constraints:
        problems.append(f"dispatch_constraints={list(_constraint_names(connection))}")
    if not _trigger_present(connection):
        problems.append("dispatch_mode_guard=missing")
    if present == set(ADDED_COLUMNS):
        nullable = {name: bool(columns[name]["nullable"]) for name in columns}
        if nullable["dispatch_mode"] or nullable["reference_price"]:
            problems.append("required_dispatch_columns_nullable")
        for name in (
            "canary_risk_policy_id",
            "probe_receipt_id",
            "limit_price",
            "maximum_buy_contracts",
            "maximum_order_quantity_digest",
            "maximum_order_quantity_observed_at",
            "maximum_order_quantity_expires_at",
        ):
            if not nullable[name]:
                problems.append(f"{name}=not_nullable")
    invalid_dispatch = _counts(
        connection,
        "order_dispatch_receipts",
        "dispatch_mode",
        present=present == set(ADDED_COLUMNS),
    )
    if set(invalid_dispatch).difference(
        {"CANARY_OPEN", "CONTINUOUS_OPEN", "POSITION_EXIT"}
    ):
        problems.append(f"invalid_dispatch_modes={sorted(invalid_dispatch)}")
    if problems:
        raise CanonicalContinuousDemoUpgradeBlocked(
            "BLOCKED_PARTIAL_CONTINUOUS_DEMO_UPGRADE: " + "; ".join(problems)
        )
    return _result(connection, status="ACCEPTED", repeat_noop=True)


def _replace_mode_check(
    connection: Connection, *, table: str, constraint: str, modes: tuple[str, ...]
) -> None:
    allowed = ",".join(f"'{value}'" for value in modes)
    column = "intent_mode" if table == "trade_intents" else "decision_mode"
    connection.execute(
        text(
            f"ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.{table} "
            f"DROP CONSTRAINT {constraint}, ADD CONSTRAINT {constraint} "
            f"CHECK ({column} IN ({allowed}))"
        )
    )


def apply_continuous_demo_upgrade(connection: Connection) -> ContinuousDemoUpgradeResult:
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1308202608261501})
    before = verify_continuous_demo_upgrade(connection)
    if before.status == "ACCEPTED":
        return before
    schema = CANONICAL_BUSINESS_SCHEMA
    fresh_schema = set(ADDED_COLUMNS).issubset(_columns(connection))
    if not fresh_schema:
        _replace_mode_check(connection, table="trade_intents", constraint=INTENT_CHECK, modes=ACCEPTED_MODES)
        _replace_mode_check(connection, table="risk_decisions", constraint=DECISION_CHECK, modes=ACCEPTED_MODES)
        connection.execute(
            text(
                f"ALTER TABLE {schema}.order_dispatch_receipts "
                "ADD COLUMN dispatch_mode VARCHAR(32), "
                "ADD COLUMN reference_price NUMERIC(36,18), "
                "ADD COLUMN maximum_close_contracts NUMERIC(36,18), "
                "ADD COLUMN close_capacity_digest VARCHAR(64), "
                "ADD COLUMN close_capacity_observed_at TIMESTAMPTZ, "
                "ADD COLUMN close_capacity_expires_at TIMESTAMPTZ"
            )
        )
        connection.execute(
            text(
                f"UPDATE {schema}.order_dispatch_receipts SET "
                "dispatch_mode='CANARY_OPEN', reference_price=limit_price"
            )
        )
        connection.execute(
            text(
                f"ALTER TABLE {schema}.order_dispatch_receipts "
                "ALTER COLUMN dispatch_mode SET NOT NULL, "
                "ALTER COLUMN reference_price SET NOT NULL, "
                "ALTER COLUMN canary_risk_policy_id DROP NOT NULL, "
                "ALTER COLUMN probe_receipt_id DROP NOT NULL, "
                "ALTER COLUMN limit_price DROP NOT NULL, "
                "ALTER COLUMN maximum_buy_contracts DROP NOT NULL, "
                "ALTER COLUMN maximum_order_quantity_digest DROP NOT NULL, "
                "ALTER COLUMN maximum_order_quantity_observed_at DROP NOT NULL, "
                "ALTER COLUMN maximum_order_quantity_expires_at DROP NOT NULL, "
                f"DROP CONSTRAINT {OLD_DISPATCH_SHAPE}, "
                f"DROP CONSTRAINT {DISPATCH_FRESHNESS}"
            )
        )
        connection.execute(
            text(
                f"ALTER TABLE {schema}.order_dispatch_receipts "
                f"ADD CONSTRAINT {DISPATCH_AUTHORITY_MODE} CHECK ("
                "(dispatch_mode='CANARY_OPEN' AND canary_risk_policy_id IS NOT NULL AND probe_receipt_id IS NOT NULL) OR "
                "(dispatch_mode='CONTINUOUS_OPEN' AND canary_risk_policy_id IS NULL AND probe_receipt_id IS NOT NULL) OR "
                "(dispatch_mode='POSITION_EXIT' AND canary_risk_policy_id IS NULL AND probe_receipt_id IS NULL)), "
                f"ADD CONSTRAINT {DISPATCH_MODE_SHAPE} CHECK (reference_price>0 AND effective_leverage>0 AND minimum_size>0 AND ("
                "(dispatch_mode IN ('CANARY_OPEN','CONTINUOUS_OPEN') AND long_contracts=0 AND short_contracts=0 "
                "AND active_position_count=0 AND pending_order_count=0 AND maximum_buy_contracts>=minimum_size "
                "AND maximum_close_contracts IS NULL AND limit_price=reference_price "
                "AND maximum_order_quantity_digest IS NOT NULL AND maximum_order_quantity_observed_at IS NOT NULL "
                "AND maximum_order_quantity_expires_at IS NOT NULL AND close_capacity_digest IS NULL "
                "AND close_capacity_observed_at IS NULL AND close_capacity_expires_at IS NULL) OR "
                "(dispatch_mode='POSITION_EXIT' AND long_contracts=minimum_size AND short_contracts=0 "
                "AND active_position_count=1 AND pending_order_count=0 AND maximum_buy_contracts IS NULL "
                "AND maximum_close_contracts=minimum_size AND limit_price IS NULL "
                "AND maximum_order_quantity_digest IS NULL AND maximum_order_quantity_observed_at IS NULL "
                "AND maximum_order_quantity_expires_at IS NULL AND close_capacity_digest IS NOT NULL "
                "AND close_capacity_observed_at IS NOT NULL AND close_capacity_expires_at IS NOT NULL))), "
                f"ADD CONSTRAINT {DISPATCH_FRESHNESS} CHECK ("
                "positions_expires_at>positions_observed_at AND pending_orders_expires_at>pending_orders_observed_at "
                "AND (maximum_order_quantity_observed_at IS NULL OR maximum_order_quantity_expires_at>maximum_order_quantity_observed_at) "
                "AND (close_capacity_observed_at IS NULL OR close_capacity_expires_at>close_capacity_observed_at) "
                "AND guard_leverage_expires_at>guard_leverage_observed_at AND guard_expires_at>guard_observed_at)"
            )
        )
    connection.execute(
        text(
            f"CREATE OR REPLACE FUNCTION {schema}.{DISPATCH_MODE_GUARD_FUNCTION}() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN IF NEW.dispatch_mode IS DISTINCT FROM OLD.dispatch_mode "
            "THEN RAISE EXCEPTION 'canonical dispatch_mode is immutable'; END IF; RETURN NEW; END; $$"
        )
    )
    connection.execute(
        text(
            f"CREATE TRIGGER {DISPATCH_MODE_GUARD_TRIGGER} BEFORE UPDATE ON {schema}.order_dispatch_receipts "
            f"FOR EACH ROW EXECUTE FUNCTION {schema}.{DISPATCH_MODE_GUARD_FUNCTION}()"
        )
    )
    verify_continuous_demo_upgrade(connection)
    return _result(connection, status="UPGRADED", repeat_noop=False)


def rollback_continuous_demo_upgrade(connection: Connection) -> ContinuousDemoUpgradeResult:
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1308202608261501})
    before = verify_continuous_demo_upgrade(connection)
    if before.status == "PREVIOUS_READY":
        return before
    schema = CANONICAL_BUSINESS_SCHEMA
    new_rows = int(
        connection.execute(
            text(
                f"SELECT (SELECT count(*) FROM {schema}.trade_intents WHERE intent_mode IN ('CONTINUOUS_OPEN','POSITION_EXIT')) + "
                f"(SELECT count(*) FROM {schema}.risk_decisions WHERE decision_mode IN ('CONTINUOUS_OPEN','POSITION_EXIT')) + "
                f"(SELECT count(*) FROM {schema}.order_dispatch_receipts WHERE dispatch_mode<>'CANARY_OPEN')"
            )
        ).scalar_one()
    )
    if new_rows:
        raise CanonicalContinuousDemoUpgradeBlocked(
            "BLOCKED_CONTINUOUS_DEMO_ROLLBACK_HAS_EXECUTION_LINEAGE"
        )
    connection.execute(text(f"DROP TRIGGER {DISPATCH_MODE_GUARD_TRIGGER} ON {schema}.order_dispatch_receipts"))
    connection.execute(text(f"DROP FUNCTION {schema}.{DISPATCH_MODE_GUARD_FUNCTION}()"))
    connection.execute(
        text(
            f"ALTER TABLE {schema}.order_dispatch_receipts "
            f"DROP CONSTRAINT {DISPATCH_AUTHORITY_MODE}, DROP CONSTRAINT {DISPATCH_MODE_SHAPE}, "
            f"DROP CONSTRAINT {DISPATCH_FRESHNESS}, "
            "ALTER COLUMN canary_risk_policy_id SET NOT NULL, ALTER COLUMN probe_receipt_id SET NOT NULL, "
            "ALTER COLUMN limit_price SET NOT NULL, ALTER COLUMN maximum_buy_contracts SET NOT NULL, "
            "ALTER COLUMN maximum_order_quantity_digest SET NOT NULL, "
            "ALTER COLUMN maximum_order_quantity_observed_at SET NOT NULL, "
            "ALTER COLUMN maximum_order_quantity_expires_at SET NOT NULL, "
            f"ADD CONSTRAINT {OLD_DISPATCH_SHAPE} CHECK (long_contracts=0 AND short_contracts=0 "
            "AND active_position_count=0 AND pending_order_count=0 AND maximum_buy_contracts>=minimum_size "
            "AND limit_price>0 AND effective_leverage>0), "
            f"ADD CONSTRAINT {DISPATCH_FRESHNESS} CHECK (positions_expires_at>positions_observed_at "
            "AND pending_orders_expires_at>pending_orders_observed_at "
            "AND maximum_order_quantity_expires_at>maximum_order_quantity_observed_at "
            "AND guard_leverage_expires_at>guard_leverage_observed_at AND guard_expires_at>guard_observed_at), "
            "DROP COLUMN dispatch_mode, DROP COLUMN reference_price, DROP COLUMN maximum_close_contracts, "
            "DROP COLUMN close_capacity_digest, DROP COLUMN close_capacity_observed_at, DROP COLUMN close_capacity_expires_at"
        )
    )
    _replace_mode_check(connection, table="trade_intents", constraint=INTENT_CHECK, modes=PREVIOUS_MODES)
    _replace_mode_check(connection, table="risk_decisions", constraint=DECISION_CHECK, modes=PREVIOUS_MODES)
    return _result(connection, status="ROLLED_BACK", repeat_noop=False)


__all__ = [
    "ACCEPTED_MODES",
    "CanonicalContinuousDemoUpgradeBlocked",
    "ContinuousDemoUpgradeResult",
    "apply_continuous_demo_upgrade",
    "rollback_continuous_demo_upgrade",
    "verify_continuous_demo_upgrade",
]
