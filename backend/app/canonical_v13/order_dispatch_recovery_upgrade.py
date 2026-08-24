"""Reversible PostgreSQL contract for one auditable same-order retry."""

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
from app.canonical_v13.models import SCHEMA_METADATA_TABLE


CONTRACT: Final = "canonical-v13-order-dispatch-recovery-upgrade-v1"
CLAIMS: Final = "order_dispatch_receipts"
OUTCOMES: Final = "order_dispatch_outcome_receipts"
ACCEPTED_CLAIM_CHECK: Final = "ck_order_dispatch_receipts_bounded_attempts"
ACCEPTED_CLAIM_UNIQUE: Final = "order_dispatch_receipts_order_attempt_unique"
ACCEPTED_OUTCOME_CHECK: Final = (
    "ck_order_dispatch_outcome_receipts_mode_identity"
)


class CanonicalOrderDispatchRecoveryUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderDispatchRecoveryUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    claim_count: int
    outcome_count: int
    maximum_attempt_ordinal: int
    negative_outcome_count: int
    rejected_outcome_count: int
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
        raise CanonicalOrderDispatchRecoveryUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_RECOVERY_SCHEMA_METADATA"
        )
    return value


def _constraints(connection: Connection, table_name: str) -> list[dict[str, object]]:
    rows = connection.execute(
        text(
            "SELECT constraint_row.conname, constraint_row.contype, "
            "pg_get_constraintdef(constraint_row.oid, true) AS definition, "
            "COALESCE(array_agg(attribute_row.attname ORDER BY key_row.ordinality) "
            "FILTER (WHERE attribute_row.attname IS NOT NULL), ARRAY[]::name[]) AS columns "
            "FROM pg_constraint constraint_row "
            "JOIN pg_class table_row ON table_row.oid=constraint_row.conrelid "
            "JOIN pg_namespace namespace_row ON namespace_row.oid=table_row.relnamespace "
            "LEFT JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY "
            "AS key_row(attnum, ordinality) ON true "
            "LEFT JOIN pg_attribute attribute_row ON attribute_row.attrelid=table_row.oid "
            "AND attribute_row.attnum=key_row.attnum "
            "WHERE namespace_row.nspname=:schema AND table_row.relname=:table_name "
            "GROUP BY constraint_row.conname, constraint_row.contype, constraint_row.oid"
        ),
        {"schema": CANONICAL_BUSINESS_SCHEMA, "table_name": table_name},
    ).mappings()
    return [dict(row) for row in rows]


def _exchange_order_nullable(connection: Connection) -> bool:
    value = connection.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema=:schema AND table_name=:table_name "
            "AND column_name='exchange_order_id'"
        ),
        {"schema": CANONICAL_BUSINESS_SCHEMA, "table_name": OUTCOMES},
    ).scalar_one_or_none()
    if value not in {"YES", "NO"}:
        raise CanonicalOrderDispatchRecoveryUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_RECOVERY_COLUMN_UNSET"
        )
    return value == "YES"


def _state(connection: Connection) -> str:
    claim_constraints = _constraints(connection, CLAIMS)
    outcome_constraints = _constraints(connection, OUTCOMES)
    claim_uniques = {
        tuple(row["columns"])
        for row in claim_constraints
        if row["contype"] == "u"
    }
    outcome_uniques = {
        tuple(row["columns"])
        for row in outcome_constraints
        if row["contype"] == "u"
    }
    claim_checks = " ".join(
        str(row["definition"])
        for row in claim_constraints
        if row["contype"] == "c" and "attempt_ordinal" in str(row["definition"])
    )
    outcome_checks = " ".join(
        str(row["definition"])
        for row in outcome_constraints
        if row["contype"] == "c" and "outcome_mode" in str(row["definition"])
    )
    outcome_modes = set(re.findall(r"'([A-Z_]+)'", outcome_checks))
    unchanged_uniques = (
        ("claim_digest",) in claim_uniques
        and ("dispatch_claim_id",) in outcome_uniques
        and ("exchange_order_id",) in outcome_uniques
        and ("receipt_digest",) in outcome_uniques
    )
    nullable = _exchange_order_nullable(connection)
    previous = (
        ("order_id",) in claim_uniques
        and ("order_id", "attempt_ordinal") not in claim_uniques
        and re.search(r"attempt_ordinal\s*=\s*1", claim_checks) is not None
        and ("order_id",) in outcome_uniques
        and ("client_order_id",) in outcome_uniques
        and not nullable
        and outcome_modes == {"POST", "GET_RECOVERY"}
        and unchanged_uniques
    )
    accepted = (
        ("order_id",) not in claim_uniques
        and ("order_id", "attempt_ordinal") in claim_uniques
        and re.search(r"attempt_ordinal\s*>=\s*1", claim_checks) is not None
        and re.search(r"attempt_ordinal\s*<=\s*2", claim_checks) is not None
        and ("order_id",) not in outcome_uniques
        and ("client_order_id",) not in outcome_uniques
        and nullable
        and outcome_modes
        == {"POST", "GET_RECOVERY", "GET_NOT_FOUND", "POST_REJECTED"}
        and unchanged_uniques
    )
    if previous:
        return "PREVIOUS_READY"
    if accepted:
        return "ACCEPTED"
    raise CanonicalOrderDispatchRecoveryUpgradeBlocked(
        "BLOCKED_PARTIAL_ORDER_DISPATCH_RECOVERY_UPGRADE"
    )


def _counts(connection: Connection) -> tuple[int, int, int, int, int]:
    row = connection.execute(
        text(
            f"SELECT (SELECT count(*) FROM {CANONICAL_BUSINESS_SCHEMA}.{CLAIMS}) "
            "AS claim_count, "
            f"(SELECT count(*) FROM {CANONICAL_BUSINESS_SCHEMA}.{OUTCOMES}) "
            "AS outcome_count, "
            f"(SELECT COALESCE(max(attempt_ordinal),0) FROM "
            f"{CANONICAL_BUSINESS_SCHEMA}.{CLAIMS}) AS maximum_attempt_ordinal, "
            f"(SELECT count(*) FROM {CANONICAL_BUSINESS_SCHEMA}.{OUTCOMES} "
            "WHERE outcome_mode='GET_NOT_FOUND') AS negative_outcome_count, "
            f"(SELECT count(*) FROM {CANONICAL_BUSINESS_SCHEMA}.{OUTCOMES} "
            "WHERE outcome_mode='POST_REJECTED') AS rejected_outcome_count"
        )
    ).mappings().one()
    return (
        int(row["claim_count"]),
        int(row["outcome_count"]),
        int(row["maximum_attempt_ordinal"]),
        int(row["negative_outcome_count"]),
        int(row["rejected_outcome_count"]),
    )


def _lineage_digest(connection: Connection) -> str:
    rows = connection.execute(
        text(
            f"SELECT 'claim' AS kind, order_id::text, attempt_ordinal::text AS ordinal, "
            f"claim_digest AS digest FROM {CANONICAL_BUSINESS_SCHEMA}.{CLAIMS} "
            "UNION ALL "
            f"SELECT 'outcome', order_id::text, outcome_mode, receipt_digest "
            f"FROM {CANONICAL_BUSINESS_SCHEMA}.{OUTCOMES} "
            "ORDER BY kind, order_id, ordinal, digest"
        )
    ).mappings()
    return _digest([dict(row) for row in rows])


def _result(
    connection: Connection, *, status: str, repeat_noop: bool
) -> OrderDispatchRecoveryUpgradeResult:
    counts = _counts(connection)
    payload = {
        "contract": CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "claim_count": counts[0],
        "outcome_count": counts[1],
        "maximum_attempt_ordinal": counts[2],
        "negative_outcome_count": counts[3],
        "rejected_outcome_count": counts[4],
        "lineage_digest": _lineage_digest(connection),
        "repeat_noop": repeat_noop,
    }
    return OrderDispatchRecoveryUpgradeResult(
        **payload, receipt_digest=_digest(payload)
    )


def verify_order_dispatch_recovery_upgrade(
    connection: Connection,
) -> OrderDispatchRecoveryUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalOrderDispatchRecoveryUpgradeBlocked(
            "BLOCKED_POSTGRESQL_REQUIRED"
        )
    if _manifest(connection) != CANONICAL_MANIFEST_DIGEST:
        raise CanonicalOrderDispatchRecoveryUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_RECOVERY_MANIFEST"
        )
    state = _state(connection)
    counts = _counts(connection)
    if counts[2] > (1 if state == "PREVIOUS_READY" else 2):
        raise CanonicalOrderDispatchRecoveryUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_RECOVERY_ATTEMPT_COUNT"
        )
    if state == "PREVIOUS_READY" and (counts[3] or counts[4]):
        raise CanonicalOrderDispatchRecoveryUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_RECOVERY_PREVIOUS_ROWS"
        )
    return _result(connection, status=state, repeat_noop=True)


def _name(row: dict[str, object]) -> str:
    value = str(row["conname"])
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise CanonicalOrderDispatchRecoveryUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_RECOVERY_CONSTRAINT_NAME"
        )
    return value


def _find(
    rows: list[dict[str, object]],
    *,
    kind: str,
    columns: tuple[str, ...] | None = None,
    contains: str | None = None,
) -> str:
    matched = [
        row
        for row in rows
        if row["contype"] == kind
        and (columns is None or tuple(row["columns"]) == columns)
        and (contains is None or contains in str(row["definition"]))
    ]
    if len(matched) != 1:
        raise CanonicalOrderDispatchRecoveryUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_RECOVERY_CONSTRAINT_IDENTITY"
        )
    return _name(matched[0])


def _drop(connection: Connection, table_name: str, constraint_name: str) -> None:
    connection.execute(
        text(
            f'ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} '
            f'DROP CONSTRAINT "{constraint_name}"'
        )
    )


def apply_order_dispatch_recovery_upgrade(
    connection: Connection,
) -> OrderDispatchRecoveryUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_241_018},
    )
    before = verify_order_dispatch_recovery_upgrade(connection)
    if before.status == "ACCEPTED":
        return before
    claims = _constraints(connection, CLAIMS)
    outcomes = _constraints(connection, OUTCOMES)
    _drop(connection, CLAIMS, _find(claims, kind="u", columns=("order_id",)))
    _drop(connection, CLAIMS, _find(claims, kind="c", contains="attempt_ordinal"))
    connection.execute(
        text(
            f"ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.{CLAIMS} "
            f"ADD CONSTRAINT {ACCEPTED_CLAIM_CHECK} "
            "CHECK (attempt_ordinal BETWEEN 1 AND 2), "
            f"ADD CONSTRAINT {ACCEPTED_CLAIM_UNIQUE} "
            "UNIQUE (order_id, attempt_ordinal)"
        )
    )
    _drop(connection, OUTCOMES, _find(outcomes, kind="u", columns=("order_id",)))
    _drop(
        connection,
        OUTCOMES,
        _find(outcomes, kind="u", columns=("client_order_id",)),
    )
    _drop(connection, OUTCOMES, _find(outcomes, kind="c", contains="outcome_mode"))
    connection.execute(
        text(
            f"ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.{OUTCOMES} "
            "ALTER COLUMN exchange_order_id DROP NOT NULL, "
            f"ADD CONSTRAINT {ACCEPTED_OUTCOME_CHECK} CHECK ("
            "(outcome_mode IN ('POST','GET_RECOVERY') AND exchange_order_id IS NOT NULL) "
            "OR (outcome_mode IN ('GET_NOT_FOUND','POST_REJECTED') "
            "AND exchange_order_id IS NULL))"
        )
    )
    verify_order_dispatch_recovery_upgrade(connection)
    return _result(connection, status="UPGRADED", repeat_noop=False)


def rollback_order_dispatch_recovery_upgrade(
    connection: Connection,
) -> OrderDispatchRecoveryUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_241_018},
    )
    before = verify_order_dispatch_recovery_upgrade(connection)
    if before.status == "PREVIOUS_READY":
        return before
    counts = _counts(connection)
    if counts[2] > 1 or counts[3] or counts[4]:
        raise CanonicalOrderDispatchRecoveryUpgradeBlocked(
            "BLOCKED_ORDER_DISPATCH_RECOVERY_ROLLBACK_EVIDENCE_PRESENT"
        )
    claims = _constraints(connection, CLAIMS)
    outcomes = _constraints(connection, OUTCOMES)
    _drop(
        connection,
        CLAIMS,
        _find(claims, kind="u", columns=("order_id", "attempt_ordinal")),
    )
    _drop(connection, CLAIMS, _find(claims, kind="c", contains="attempt_ordinal"))
    connection.execute(
        text(
            f"ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.{CLAIMS} "
            "ADD CONSTRAINT uq_order_dispatch_receipts_order_id UNIQUE (order_id), "
            "ADD CONSTRAINT ck_order_dispatch_receipts_single_attempt "
            "CHECK (attempt_ordinal = 1)"
        )
    )
    _drop(connection, OUTCOMES, _find(outcomes, kind="c", contains="outcome_mode"))
    connection.execute(
        text(
            f"ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.{OUTCOMES} "
            "ALTER COLUMN exchange_order_id SET NOT NULL, "
            "ADD CONSTRAINT uq_order_dispatch_outcome_receipts_order_id "
            "UNIQUE (order_id), "
            "ADD CONSTRAINT uq_order_dispatch_outcome_receipts_client_order_id "
            "UNIQUE (client_order_id), "
            "ADD CONSTRAINT ck_order_dispatch_outcome_receipts_mode "
            "CHECK (outcome_mode IN ('POST','GET_RECOVERY'))"
        )
    )
    verify_order_dispatch_recovery_upgrade(connection)
    return _result(connection, status="ROLLED_BACK", repeat_noop=False)


__all__ = [
    "CanonicalOrderDispatchRecoveryUpgradeBlocked",
    "OrderDispatchRecoveryUpgradeResult",
    "apply_order_dispatch_recovery_upgrade",
    "rollback_order_dispatch_recovery_upgrade",
    "verify_order_dispatch_recovery_upgrade",
]
