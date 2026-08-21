"""Transactional Phase 9 schema/ACL upgrade with immutable audit receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Final
from uuid import uuid4

from sqlalchemy import Connection, func, inspect, select, text

from app.canonical_v13.genesis import (
    postgresql_acl_statements,
    postgresql_owner_table_grant_statements,
    verify_canonical_genesis,
)
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
)
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    DEPLOYMENTS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SCHEMA_METADATA_TABLE,
    SIGNALS_TABLE,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


UPGRADE_CONTRACT: Final = "canonical-v13-phase9-execution-schema-upgrade-v2"
PREVIOUS_CANONICAL_MANIFEST_DIGEST: Final = (
    "5f39082802ad9a284f6889702ddee4458d881c53009e77c24726466dcda2aec4"
)
PHASE9_EXTENSION_TABLES = (
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
)
PHASE9_EXTENSION_TABLE_NAMES: Final[tuple[str, ...]] = tuple(
    table.name for table in PHASE9_EXTENSION_TABLES
)
PHASE9_UNIQUE_CONSTRAINTS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "deployment_approvals_qualification_unique": (
        "deployment_approvals",
        ("qualification_decision_id",),
    ),
    "deployments_approval_unique": ("deployments", ("deployment_approval_id",)),
    "runtime_instances_deployment_unique": ("runtime_instances", ("deployment_id",)),
    "runtime_receipts_receipt_digest_unique": ("runtime_receipts", ("receipt_digest",)),
    "signals_runtime_target_digest_unique": (
        "signals",
        ("runtime_instance_id", "research_target_id", "signal_digest"),
    ),
}
_AFFECTED_EXISTING_TABLES = {
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
    present_extension_tables: tuple[str, ...]
    manifest_digest: str
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


def _manifest_digest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_PHASE9_SCHEMA_METADATA", "canonical manifest identity is missing"
        )
    return value


def _present_extension_tables(connection: Connection) -> tuple[str, ...]:
    observed = set(
        inspect(connection).get_table_names(schema=CANONICAL_BUSINESS_SCHEMA)
    )
    return tuple(sorted(observed.intersection(PHASE9_EXTENSION_TABLE_NAMES)))


def _present_constraints(connection: Connection) -> tuple[str, ...]:
    inspector = inspect(connection)
    observed: set[str] = set()
    for expected_name, (
        table_name,
        expected_columns,
    ) in PHASE9_UNIQUE_CONSTRAINTS.items():
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


def _row_counts(connection: Connection, *, present: tuple[str, ...]) -> dict[str, int]:
    counts = {
        name: int(
            connection.execute(select(func.count()).select_from(table)).scalar_one()
        )
        for name, table in _AFFECTED_EXISTING_TABLES.items()
    }
    present_set = set(present)
    for table in PHASE9_EXTENSION_TABLES:
        counts[table.name] = (
            int(
                connection.execute(select(func.count()).select_from(table)).scalar_one()
            )
            if table.name in present_set
            else 0
        )
    return counts


def _result(
    *,
    status: str,
    constraints: tuple[str, ...],
    extension_tables: tuple[str, ...],
    manifest_digest: str,
    counts: dict[str, int],
    repeat_noop: bool,
) -> Phase9SchemaUpgradeResult:
    payload = {
        "contract": UPGRADE_CONTRACT,
        "status": status,
        "present_constraints": constraints,
        "present_extension_tables": extension_tables,
        "manifest_digest": manifest_digest,
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
            "BLOCKED_POSTGRESQL_REQUIRED",
            "Phase 9 schema verification requires PostgreSQL",
        )
    constraints = _present_constraints(connection)
    extension_tables = _present_extension_tables(connection)
    manifest_digest = _manifest_digest(connection)
    counts = _row_counts(connection, present=extension_tables)
    expected_constraints = tuple(sorted(PHASE9_UNIQUE_CONSTRAINTS))
    expected_tables = tuple(sorted(PHASE9_EXTENSION_TABLE_NAMES))
    if (
        constraints == expected_constraints
        and extension_tables == expected_tables
        and manifest_digest == CANONICAL_MANIFEST_DIGEST
    ):
        verification = verify_canonical_genesis(connection)
        if not verification.accepted:
            raise CanonicalPhase9SchemaUpgradeBlocked(
                "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
            )
        return _result(
            status="ACCEPTED",
            constraints=constraints,
            extension_tables=extension_tables,
            manifest_digest=manifest_digest,
            counts=counts,
            repeat_noop=True,
        )
    if (
        constraints == ()
        and extension_tables == ()
        and manifest_digest == PREVIOUS_CANONICAL_MANIFEST_DIGEST
    ):
        return _result(
            status="PREVIOUS_READY",
            constraints=constraints,
            extension_tables=extension_tables,
            manifest_digest=manifest_digest,
            counts=counts,
            repeat_noop=True,
        )
    raise CanonicalPhase9SchemaUpgradeBlocked(
        "BLOCKED_PARTIAL_PHASE9_SCHEMA_UPGRADE",
        f"constraints={constraints!r} extension_tables={extension_tables!r} "
        f"manifest_digest={manifest_digest}",
    )


def _lock_upgrade_boundary(connection: Connection) -> None:
    names = (
        "schema_metadata",
        "deployment_approvals",
        "deployments",
        "runtime_instances",
        "runtime_receipts",
        "signals",
    )
    connection.execute(
        text(
            "LOCK TABLE "
            + ", ".join(f"{CANONICAL_BUSINESS_SCHEMA}.{name}" for name in names)
            + " IN ACCESS EXCLUSIVE MODE"
        )
    )


def _require_zero_rows(counts: dict[str, int]) -> None:
    nonzero = {name: count for name, count in counts.items() if count}
    if nonzero:
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_PHASE9_SCHEMA_NONZERO_ROWS",
            f"affected rows must be zero: {nonzero!r}",
        )


def _execute_statements(connection: Connection, sql: str) -> None:
    for statement in sql.split(";\n"):
        if statement.strip():
            connection.execute(text(statement))


def _append_audit(
    connection: Connection,
    *,
    event_type: str,
    actor_identity: str,
    before_digest: str,
    after_digest: str,
) -> None:
    evidence = {
        "contract": UPGRADE_CONTRACT,
        "event_type": event_type,
        "actor_identity": actor_identity,
        "before_manifest_digest": before_digest,
        "after_manifest_digest": after_digest,
        "extension_tables": sorted(PHASE9_EXTENSION_TABLE_NAMES),
        "constraints": sorted(PHASE9_UNIQUE_CONSTRAINTS),
        "ddl_digest": _digest(
            {
                "upgrade": render_phase9_uniqueness_upgrade_sql(),
                "rollback": render_phase9_uniqueness_rollback_sql(),
            }
        ),
        "destructive_row_operations": 0,
    }
    request_digest = _digest(evidence)
    receipt_digest = _digest(
        {"aggregate": "canonical-v13-phase9-schema", "request_digest": request_digest}
    )
    connection.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=uuid4(),
            event_type=event_type,
            aggregate_type="canonical_phase9_schema_upgrade",
            aggregate_id=after_digest,
            actor_identity=actor_identity,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            evidence_json=evidence,
            created_at=datetime.now(timezone.utc),
        )
    )


def apply_phase9_schema_upgrade(
    connection: Connection,
    *,
    actor_identity: str = "canonical-phase9-schema-operator",
    role_mapping: CanonicalRoleMapping | None = None,
) -> Phase9SchemaUpgradeResult:
    _lock_upgrade_boundary(connection)
    before = verify_phase9_schema_upgrade(connection)
    if before.status == "ACCEPTED":
        return before
    _require_zero_rows(before.affected_row_counts)
    for table in PHASE9_EXTENSION_TABLES:
        table.create(bind=connection, checkfirst=False)
    _execute_statements(connection, render_phase9_uniqueness_upgrade_sql())
    resolved = role_mapping or CanonicalRoleMapping.identity()
    owner = resolved.physical("canonical_schema_owner")
    for table_name in PHASE9_EXTENSION_TABLE_NAMES:
        connection.execute(
            text(
                f"ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} OWNER TO {owner}"
            )
        )
    for statement in postgresql_acl_statements(resolved):
        connection.execute(text(statement))
    for statement in postgresql_owner_table_grant_statements(resolved):
        connection.execute(text(statement))
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=CANONICAL_MANIFEST_DIGEST)
    )
    _append_audit(
        connection,
        event_type="PHASE9_SCHEMA_UPGRADED",
        actor_identity=actor_identity,
        before_digest=before.manifest_digest,
        after_digest=CANONICAL_MANIFEST_DIGEST,
    )
    after = verify_phase9_schema_upgrade(connection)
    return _result(
        status="UPGRADED",
        constraints=after.present_constraints,
        extension_tables=after.present_extension_tables,
        manifest_digest=after.manifest_digest,
        counts=after.affected_row_counts,
        repeat_noop=False,
    )


def rollback_phase9_schema_upgrade(
    connection: Connection,
    *,
    actor_identity: str = "canonical-phase9-schema-operator",
) -> Phase9SchemaUpgradeResult:
    _lock_upgrade_boundary(connection)
    before = verify_phase9_schema_upgrade(connection)
    if before.status == "PREVIOUS_READY":
        return before
    _require_zero_rows(before.affected_row_counts)
    _execute_statements(connection, render_phase9_uniqueness_rollback_sql())
    for table in reversed(PHASE9_EXTENSION_TABLES):
        table.drop(bind=connection, checkfirst=False)
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=PREVIOUS_CANONICAL_MANIFEST_DIGEST)
    )
    _append_audit(
        connection,
        event_type="PHASE9_SCHEMA_ROLLED_BACK",
        actor_identity=actor_identity,
        before_digest=before.manifest_digest,
        after_digest=PREVIOUS_CANONICAL_MANIFEST_DIGEST,
    )
    after = verify_phase9_schema_upgrade(connection)
    return _result(
        status="ROLLED_BACK",
        constraints=after.present_constraints,
        extension_tables=after.present_extension_tables,
        manifest_digest=after.manifest_digest,
        counts=after.affected_row_counts,
        repeat_noop=False,
    )


__all__ = [
    "PHASE9_EXTENSION_TABLE_NAMES",
    "PHASE9_UNIQUE_CONSTRAINTS",
    "PREVIOUS_CANONICAL_MANIFEST_DIGEST",
    "UPGRADE_CONTRACT",
    "CanonicalPhase9SchemaUpgradeBlocked",
    "Phase9SchemaUpgradeResult",
    "apply_phase9_schema_upgrade",
    "render_phase9_uniqueness_rollback_sql",
    "render_phase9_uniqueness_upgrade_sql",
    "rollback_phase9_schema_upgrade",
    "verify_phase9_schema_upgrade",
]
