"""Additive Phase 9 uniqueness upgrade and zero-row rollback contract.

The upgrade is intentionally separate from genesis so an accepted canonical database
can be reviewed, backed up, upgraded, and restored independently.  It never creates
Phase 9 business rows and refuses to change constraints once any affected lifecycle
has begun.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Final

from sqlalchemy import Connection, func, inspect, select, text

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import (
    DEPLOYMENTS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SIGNALS_TABLE,
)


UPGRADE_CONTRACT: Final = "canonical-v13-phase9-uniqueness-upgrade-v1"
PHASE9_UNIQUE_CONSTRAINTS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "deployment_approvals_qualification_unique": (
        "deployment_approvals",
        ("qualification_decision_id",),
    ),
    "deployments_approval_unique": (
        "deployments",
        ("deployment_approval_id",),
    ),
    "runtime_instances_deployment_unique": (
        "runtime_instances",
        ("deployment_id",),
    ),
    "runtime_receipts_receipt_digest_unique": (
        "runtime_receipts",
        ("receipt_digest",),
    ),
    "signals_runtime_target_digest_unique": (
        "signals",
        ("runtime_instance_id", "research_target_id", "signal_digest"),
    ),
}
_TABLES = {
    "deployment_approvals": DEPLOYMENT_APPROVALS_TABLE,
    "deployments": DEPLOYMENTS_TABLE,
    "runtime_instances": RUNTIME_INSTANCES_TABLE,
    "runtime_receipts": RUNTIME_RECEIPTS_TABLE,
    "signals": SIGNALS_TABLE,
}


class CanonicalPhase9SchemaUpgradeBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Phase9SchemaUpgradeResult:
    contract: str
    status: str
    present_constraints: tuple[str, ...]
    affected_row_counts: dict[str, int]
    destructive_row_operations: int
    repeat_noop: bool
    receipt_digest: str


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _row_counts(connection: Connection) -> dict[str, int]:
    return {
        name: int(
            connection.execute(select(func.count()).select_from(table)).scalar_one()
        )
        for name, table in _TABLES.items()
    }


def _present_constraints(connection: Connection) -> tuple[str, ...]:
    inspector = inspect(connection)
    observed: set[str] = set()
    for expected_name, (table_name, expected_columns) in (
        PHASE9_UNIQUE_CONSTRAINTS.items()
    ):
        for constraint in inspector.get_unique_constraints(
            table_name, schema=CANONICAL_BUSINESS_SCHEMA
        ):
            if constraint.get("name") != expected_name:
                continue
            if tuple(constraint.get("column_names") or ()) != expected_columns:
                raise CanonicalPhase9SchemaUpgradeBlocked(
                    "BLOCKED_PHASE9_CONSTRAINT_DRIFT",
                    f"{expected_name} has unexpected columns",
                )
            observed.add(expected_name)
    return tuple(sorted(observed))


def _result(
    *,
    status: str,
    present: tuple[str, ...],
    counts: dict[str, int],
    repeat_noop: bool,
) -> Phase9SchemaUpgradeResult:
    payload = {
        "contract": UPGRADE_CONTRACT,
        "status": status,
        "present_constraints": present,
        "affected_row_counts": counts,
        "destructive_row_operations": 0,
        "repeat_noop": repeat_noop,
    }
    return Phase9SchemaUpgradeResult(**payload, receipt_digest=_digest(payload))


def render_phase9_uniqueness_upgrade_sql() -> str:
    statements = [
        f"ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
        f"ADD CONSTRAINT {constraint_name} UNIQUE ({', '.join(columns)})"
        for constraint_name, (table_name, columns) in sorted(
            PHASE9_UNIQUE_CONSTRAINTS.items()
        )
    ]
    return ";\n".join(statements) + ";\n"


def render_phase9_uniqueness_rollback_sql() -> str:
    statements = [
        f"ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
        f"DROP CONSTRAINT {constraint_name}"
        for constraint_name, (table_name, _columns) in sorted(
            PHASE9_UNIQUE_CONSTRAINTS.items(), reverse=True
        )
    ]
    return ";\n".join(statements) + ";\n"


def verify_phase9_schema_upgrade(connection: Connection) -> Phase9SchemaUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_POSTGRESQL_REQUIRED", "Phase 9 schema verification requires PostgreSQL"
        )
    verification = verify_canonical_genesis(connection)
    if not verification.accepted:
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    present = _present_constraints(connection)
    counts = _row_counts(connection)
    expected = tuple(sorted(PHASE9_UNIQUE_CONSTRAINTS))
    if present not in ((), expected):
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_PARTIAL_PHASE9_SCHEMA_UPGRADE",
            f"present={present!r} expected={expected!r}",
        )
    return _result(
        status="ACCEPTED" if present == expected else "PREVIOUS_READY",
        present=present,
        counts=counts,
        repeat_noop=present == expected,
    )


def _require_zero_rows(counts: dict[str, int]) -> None:
    nonzero = {name: count for name, count in counts.items() if count}
    if nonzero:
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_PHASE9_SCHEMA_NONZERO_ROWS",
            f"affected rows must be zero: {nonzero!r}",
        )


def apply_phase9_schema_upgrade(connection: Connection) -> Phase9SchemaUpgradeResult:
    before = verify_phase9_schema_upgrade(connection)
    if before.status == "ACCEPTED":
        return before
    _require_zero_rows(before.affected_row_counts)
    for statement in render_phase9_uniqueness_upgrade_sql().split(";\n"):
        if statement.strip():
            connection.execute(text(statement))
    after = verify_phase9_schema_upgrade(connection)
    if after.status != "ACCEPTED":
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_PHASE9_SCHEMA_UPGRADE_INCOMPLETE",
            f"status={after.status}",
        )
    return _result(
        status="UPGRADED",
        present=after.present_constraints,
        counts=after.affected_row_counts,
        repeat_noop=False,
    )


def rollback_phase9_schema_upgrade(connection: Connection) -> Phase9SchemaUpgradeResult:
    before = verify_phase9_schema_upgrade(connection)
    if before.status == "PREVIOUS_READY":
        return before
    _require_zero_rows(before.affected_row_counts)
    for statement in render_phase9_uniqueness_rollback_sql().split(";\n"):
        if statement.strip():
            connection.execute(text(statement))
    after = verify_phase9_schema_upgrade(connection)
    if after.status != "PREVIOUS_READY":
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_PHASE9_SCHEMA_ROLLBACK_INCOMPLETE",
            f"status={after.status}",
        )
    return _result(
        status="ROLLED_BACK",
        present=after.present_constraints,
        counts=after.affected_row_counts,
        repeat_noop=False,
    )


__all__ = [
    "PHASE9_UNIQUE_CONSTRAINTS",
    "UPGRADE_CONTRACT",
    "CanonicalPhase9SchemaUpgradeBlocked",
    "Phase9SchemaUpgradeResult",
    "apply_phase9_schema_upgrade",
    "render_phase9_uniqueness_rollback_sql",
    "render_phase9_uniqueness_upgrade_sql",
    "rollback_phase9_schema_upgrade",
    "verify_phase9_schema_upgrade",
]
