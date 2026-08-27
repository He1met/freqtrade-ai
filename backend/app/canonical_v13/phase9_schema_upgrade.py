"""Transactional Phase 9 schema/ACL upgrade with immutable audit receipts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Final
from uuid import uuid4

from sqlalchemy import Connection, func, inspect, select, text

from app.canonical_v13.canary_recovery_approval_upgrade import (
    canary_recovery_predecessor_indexes,
)
from app.canonical_v13.genesis import (
    postgresql_acl_statements,
    postgresql_owner_table_grant_statements,
    verify_canonical_genesis,
)
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
    CANONICAL_PREDECESSOR_RUNTIME_IDENTITY_INDEX,
    CANONICAL_TABLE_NAMES,
    READER_TABLE_ALLOWLIST,
    TABLE_MANIFEST_BY_NAME,
    WRITER_READ_ALLOWLIST,
    WRITER_TABLE_ALLOWLIST,
)
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE,
    ORDER_DISPATCH_RECEIPTS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SCHEMA_METADATA_TABLE,
    SIGNALS_TABLE,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping
from app.canonical_v13.runtime_image_upgrade import (
    PREVIOUS_RUNTIME_IMAGE_MANIFEST_DIGEST,
    RUNTIME_IMAGE_ACCEPTED_MANIFEST_DIGEST,
    RUNTIME_READER_ACL_ACCEPTED_MANIFEST_DIGEST,
)
from app.canonical_v13.gate_receipt_upgrade import GATE_GUARD_FUNCTION_NAMES

UPGRADE_CONTRACT: Final = "canonical-v13-phase9-execution-schema-upgrade-v4"
PREVIOUS_CANONICAL_MANIFEST_DIGEST: Final = (
    "5f39082802ad9a284f6889702ddee4458d881c53009e77c24726466dcda2aec4"
)
PHASE9_EXTENSION_TABLES = (
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
    ORDER_DISPATCH_RECEIPTS_TABLE,
    ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE,
)
PHASE9_EXTENSION_TABLE_NAMES: Final[tuple[str, ...]] = tuple(
    table.name for table in PHASE9_EXTENSION_TABLES
)
PREVIOUS_ACL_CONTRACT_DIGEST: Final = (
    "af302c492883a901798b7c86f4f0c9d457bd942037498571e0f8b962e1948263"
)
# Tables added after the frozen Phase 9 predecessor must not mutate that
# historical ACL contract or be mistaken for Phase 9 rollback drift.
_POST_PHASE9_TABLE_NAMES: Final[frozenset[str]] = frozenset(
    {"research_run_catalog"}
)
# Frozen #781 delta on tables which survive a Phase 9 rollback.  Extension-table
# grants disappear with their tables and therefore are deliberately absent.
PHASE9_SURVIVING_TABLE_GRANT_DELTA: Final[tuple[tuple[str, str, str], ...]] = (
    ("canonical_approval_writer", "audit_events", "SELECT"),
    ("canonical_approval_writer", "deployments", "SELECT"),
    ("canonical_approval_writer", "orders", "SELECT"),
    ("canonical_approval_writer", "reconciliation_items", "SELECT"),
    ("canonical_approval_writer", "reconciliation_runs", "SELECT"),
    ("canonical_approval_writer", "research_targets", "SELECT"),
    ("canonical_approval_writer", "risk_decisions", "SELECT"),
    ("canonical_approval_writer", "strategy_artifacts", "SELECT"),
    ("canonical_approval_writer", "strategy_versions", "SELECT"),
    ("canonical_control_writer", "deployment_approvals", "SELECT"),
    ("canonical_control_writer", "deployments", "SELECT"),
    ("canonical_control_writer", "fills", "SELECT"),
    ("canonical_control_writer", "ledger_entries", "SELECT"),
    ("canonical_control_writer", "orders", "SELECT"),
    ("canonical_control_writer", "reconciliation_items", "SELECT"),
    ("canonical_control_writer", "reconciliation_runs", "SELECT"),
    ("canonical_control_writer", "risk_decisions", "SELECT"),
    ("canonical_control_writer", "runtime_instances", "SELECT"),
    ("canonical_control_writer", "runtime_receipts", "SELECT"),
    ("canonical_control_writer", "signals", "SELECT"),
    ("canonical_control_writer", "trade_intents", "SELECT"),
    ("canonical_fill_writer", "risk_decisions", "SELECT"),
    ("canonical_fill_writer", "trade_intents", "SELECT"),
    ("canonical_order_writer", "deployments", "SELECT"),
    ("canonical_order_writer", "trade_intents", "SELECT"),
    ("canonical_risk_writer", "deployments", "SELECT"),
    ("canonical_risk_writer", "research_targets", "SELECT"),
    ("canonical_runtime_reader", "qualification_decisions", "SELECT"),
    ("canonical_signal_writer", "deployment_approvals", "SELECT"),
    ("canonical_signal_writer", "qualification_decisions", "SELECT"),
    ("canonical_signal_writer", "research_targets", "SELECT"),
    ("canonical_signal_writer", "runtime_receipts", "SELECT"),
)
PHASE9_DATABASE_CONNECT_DELTA: Final[tuple[str, ...]] = (
    "canonical_approval_writer",
    "canonical_deployment_writer",
    "canonical_signal_writer",
    "canonical_risk_writer",
    "canonical_order_writer",
    "canonical_fill_writer",
    "canonical_ledger_writer",
    "canonical_reconciliation_writer",
    "canonical_runtime_reader",
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
        if expected_name == "deployment_approvals_qualification_unique":
            recovery_matches = [
                constraint
                for constraint in inspector.get_unique_constraints(
                    table_name, schema=CANONICAL_BUSINESS_SCHEMA
                )
                if constraint.get("name")
                == "deployment_approvals_qualification_generation_unique"
                and tuple(constraint.get("column_names") or ())
                == ("qualification_decision_id", "approval_generation")
            ]
            if len(recovery_matches) == 1:
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


def _current_surviving_table_grants() -> set[tuple[str, str, str]]:
    surviving = (
        set(CANONICAL_TABLE_NAMES)
        - set(PHASE9_EXTENSION_TABLE_NAMES)
        - {"runtime_image_acceptances", "acceptance_signal_triggers"}
        - _POST_PHASE9_TABLE_NAMES
    )
    grants: dict[tuple[str, str], set[str]] = {}
    for writer, table_names in WRITER_TABLE_ALLOWLIST.items():
        if writer == "canonical_schema_owner":
            continue
        for table_name in table_names:
            if table_name in surviving:
                grants.setdefault((writer, table_name), set()).update(
                    TABLE_MANIFEST_BY_NAME[table_name].writer_privileges
                )
    for writer, table_names in WRITER_READ_ALLOWLIST.items():
        if writer == "canonical_schema_owner":
            continue
        for table_name in table_names:
            if table_name in surviving:
                grants.setdefault((writer, table_name), set()).add("SELECT")
    for reader, table_names in READER_TABLE_ALLOWLIST.items():
        for table_name in table_names:
            if table_name in surviving:
                grants.setdefault((reader, table_name), set()).add("SELECT")
    return {
        (role, table_name, privilege)
        for (role, table_name), privileges in grants.items()
        for privilege in privileges
    }


def _previous_acl_payload() -> dict[str, object]:
    surviving = tuple(
        sorted(
            set(CANONICAL_TABLE_NAMES)
            - set(PHASE9_EXTENSION_TABLE_NAMES)
            - {"runtime_image_acceptances", "acceptance_signal_triggers"}
            - _POST_PHASE9_TABLE_NAMES
        )
    )
    previous_grants = tuple(
        sorted(
            _current_surviving_table_grants() - set(PHASE9_SURVIVING_TABLE_GRANT_DELTA)
        )
    )
    return {
        "surviving_tables": surviving,
        "table_grants": previous_grants,
        "forbidden_connect_roles": tuple(sorted(PHASE9_DATABASE_CONNECT_DELTA)),
    }


def _resolve_role_mapping(
    connection: Connection, role_mapping: CanonicalRoleMapping | None
) -> CanonicalRoleMapping:
    if role_mapping is not None:
        return role_mapping
    owner = connection.execute(
        text(
            "SELECT pg_get_userbyid(nspowner) FROM pg_catalog.pg_namespace "
            "WHERE nspname=:schema"
        ),
        {"schema": CANONICAL_BUSINESS_SCHEMA},
    ).scalar_one_or_none()
    suffix = "schema_owner"
    if not isinstance(owner, str) or not owner.endswith(suffix):
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_PHASE9_ROLE_MAPPING",
            "explicit role mapping is required for a non-prefix schema owner",
        )
    return CanonicalRoleMapping.from_prefix(owner[: -len(suffix)])


def _verify_previous_acl(
    connection: Connection, *, role_mapping: CanonicalRoleMapping | None
) -> None:
    payload = _previous_acl_payload()
    if _digest(payload) != PREVIOUS_ACL_CONTRACT_DIGEST:
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_PREVIOUS_ACL_CONTRACT_DRIFT",
            "frozen predecessor ACL no longer matches the current Phase 9 delta",
        )
    resolved = _resolve_role_mapping(connection, role_mapping)
    owner = resolved.physical("canonical_schema_owner")
    actual = {
        (
            "PUBLIC" if row[0] is None else str(row[0]),
            str(row[1]),
            str(row[2]),
        )
        for row in connection.execute(
            text(
                """
                SELECT grantee.rolname, relation.relname, acl.privilege_type
                FROM pg_catalog.pg_class relation
                JOIN pg_catalog.pg_namespace namespace
                  ON namespace.oid=relation.relnamespace
                CROSS JOIN LATERAL aclexplode(
                    COALESCE(relation.relacl, acldefault('r', relation.relowner))
                ) acl
                LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid=acl.grantee
                WHERE namespace.nspname=:schema
                  AND relation.relkind IN ('r','p')
                  AND (grantee.rolname IS NULL OR grantee.rolname <> :owner)
                """
            ),
            {
                "schema": CANONICAL_BUSINESS_SCHEMA,
                "owner": owner,
            },
        )
        if str(row[1]) not in _POST_PHASE9_TABLE_NAMES
    }
    expected = {
        (resolved.physical(role), table_name, privilege)
        for role, table_name, privilege in payload["table_grants"]
    }
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_PREVIOUS_ACL_DRIFT",
            f"missing_table_grants={len(missing)} extra_table_grants={len(extra)}",
        )
    forbidden_connect = {
        resolved.physical(role) for role in PHASE9_DATABASE_CONNECT_DELTA
    }
    observed_connect = {
        str(value)
        for value in connection.execute(
            text(
                """
                SELECT grantee.rolname
                FROM pg_catalog.pg_database database
                CROSS JOIN LATERAL aclexplode(
                    COALESCE(database.datacl, acldefault('d', database.datdba))
                ) database_acl
                JOIN pg_catalog.pg_roles grantee ON grantee.oid=database_acl.grantee
                WHERE database.datname=current_database()
                  AND grantee.rolname = ANY(:roles)
                  AND database_acl.privilege_type='CONNECT'
                """
            ),
            {"roles": list(forbidden_connect)},
        ).scalars()
    }
    if observed_connect:
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_PREVIOUS_DATABASE_CONNECT_DRIFT",
            f"extra_phase9_connect={len(observed_connect)}",
        )


def render_phase9_acl_rollback_sql(
    role_mapping: CanonicalRoleMapping,
    *,
    database_name: str,
) -> str:
    if not database_name or '"' in database_name:
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_PHASE9_DATABASE_IDENTITY", "database name is not SQL-safe"
        )
    statements = [
        f"REVOKE {privilege} ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
        f"FROM {role_mapping.physical(role)}"
        for role, table_name, privilege in PHASE9_SURVIVING_TABLE_GRANT_DELTA
    ]
    statements.extend(
        f'REVOKE CONNECT ON DATABASE "{database_name}" FROM '
        f"{role_mapping.physical(role)}"
        for role in PHASE9_DATABASE_CONNECT_DELTA
    )
    return ";\n".join(statements) + ";\n"


def verify_phase9_schema_upgrade(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping | None = None,
) -> Phase9SchemaUpgradeResult:
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
        and manifest_digest
        in {
            RUNTIME_IMAGE_ACCEPTED_MANIFEST_DIGEST,
            RUNTIME_READER_ACL_ACCEPTED_MANIFEST_DIGEST,
            CANONICAL_MANIFEST_DIGEST,
        }
    ):
        verification = verify_canonical_genesis(
            connection,
            accepted_manifest_digests=(manifest_digest,),
            allowed_predecessor_indexes=(
                (
                    (CANONICAL_PREDECESSOR_RUNTIME_IDENTITY_INDEX,)
                    if manifest_digest != CANONICAL_MANIFEST_DIGEST
                    else ()
                )
                + canary_recovery_predecessor_indexes(connection)
            ),
        )
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
    runtime_image_present = inspect(connection).has_table(
        "runtime_image_acceptances", schema=CANONICAL_BUSINESS_SCHEMA
    )
    if (
        constraints == expected_constraints
        and extension_tables == expected_tables
        and manifest_digest == PREVIOUS_RUNTIME_IMAGE_MANIFEST_DIGEST
        and not runtime_image_present
    ):
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
        _verify_previous_acl(connection, role_mapping=role_mapping)
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
    names = [
        "schema_metadata",
        "deployment_approvals",
        "deployments",
        "runtime_instances",
        "runtime_receipts",
        "signals",
    ]
    # During rollback the extension tables already exist.  Lock them before the
    # zero-row check so a concurrent writer cannot insert after verification and
    # have its durable evidence dropped by the following DDL.
    inspector = inspect(connection)
    names.extend(
        name
        for name in PHASE9_EXTENSION_TABLE_NAMES
        if inspector.has_table(name, schema=CANONICAL_BUSINESS_SCHEMA)
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
    role_mapping: CanonicalRoleMapping,
    database_name: str,
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
                "acl_rollback": render_phase9_acl_rollback_sql(
                    role_mapping, database_name=database_name
                ),
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
    before = verify_phase9_schema_upgrade(connection, role_mapping=role_mapping)
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
    deferred_acl_fragments = (
        f"qualification_decisions TO {resolved.physical('canonical_runtime_reader')}",
        f"order_writer_leases TO {resolved.physical('canonical_deployment_writer')}",
        f"deployment_approvals TO {resolved.physical('canonical_signal_writer')}",
        f"qualification_decisions TO {resolved.physical('canonical_signal_writer')}",
        f"research_targets TO {resolved.physical('canonical_signal_writer')}",
    )
    for statement in postgresql_acl_statements(
        resolved,
        guard_function_names=GATE_GUARD_FUNCTION_NAMES,
    ):
        if (
            "runtime_image_acceptances" in statement
            or "acceptance_signal_triggers" in statement
            or any(
                fragment in statement for fragment in deferred_acl_fragments
            )
        ):
            continue
        connection.execute(text(statement))
    for statement in postgresql_owner_table_grant_statements(resolved):
        if (
            "runtime_image_acceptances" in statement
            or "acceptance_signal_triggers" in statement
        ):
            continue
        connection.execute(text(statement))
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=PREVIOUS_RUNTIME_IMAGE_MANIFEST_DIGEST)
    )
    _append_audit(
        connection,
        event_type="PHASE9_SCHEMA_UPGRADED",
        actor_identity=actor_identity,
        before_digest=before.manifest_digest,
        after_digest=PREVIOUS_RUNTIME_IMAGE_MANIFEST_DIGEST,
        role_mapping=resolved,
        database_name=str(
            connection.execute(text("SELECT current_database()")).scalar_one()
        ),
    )
    after = verify_phase9_schema_upgrade(connection, role_mapping=resolved)
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
    role_mapping: CanonicalRoleMapping | None = None,
) -> Phase9SchemaUpgradeResult:
    approval_columns = {
        str(row["name"])
        for row in inspect(connection).get_columns(
            "deployment_approvals", schema=CANONICAL_BUSINESS_SCHEMA
        )
    }
    if "approval_generation" in approval_columns:
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_CANARY_RECOVERY_APPROVAL_ROLLBACK_REQUIRED",
            "rollback canary recovery approval before the Phase 9 schema",
        )
    if inspect(connection).has_table(
        "runtime_image_acceptances", schema=CANONICAL_BUSINESS_SCHEMA
    ):
        raise CanonicalPhase9SchemaUpgradeBlocked(
            "BLOCKED_RUNTIME_IMAGE_ROLLBACK_REQUIRED",
            "rollback runtime image authority before the Phase 9 schema",
        )
    _lock_upgrade_boundary(connection)
    resolved = _resolve_role_mapping(connection, role_mapping)
    before = verify_phase9_schema_upgrade(connection, role_mapping=resolved)
    if before.status == "PREVIOUS_READY":
        return before
    _require_zero_rows(before.affected_row_counts)
    database_name = connection.execute(text("SELECT current_database()")).scalar_one()
    _execute_statements(
        connection,
        render_phase9_acl_rollback_sql(
            resolved,
            database_name=str(database_name),
        ),
    )
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
        role_mapping=resolved,
        database_name=str(database_name),
    )
    after = verify_phase9_schema_upgrade(connection, role_mapping=resolved)
    return _result(
        status="ROLLED_BACK",
        constraints=after.present_constraints,
        extension_tables=after.present_extension_tables,
        manifest_digest=after.manifest_digest,
        counts=after.affected_row_counts,
        repeat_noop=False,
    )


__all__ = [
    "PHASE9_DATABASE_CONNECT_DELTA",
    "PHASE9_EXTENSION_TABLE_NAMES",
    "PHASE9_SURVIVING_TABLE_GRANT_DELTA",
    "PHASE9_UNIQUE_CONSTRAINTS",
    "PREVIOUS_ACL_CONTRACT_DIGEST",
    "PREVIOUS_CANONICAL_MANIFEST_DIGEST",
    "UPGRADE_CONTRACT",
    "CanonicalPhase9SchemaUpgradeBlocked",
    "Phase9SchemaUpgradeResult",
    "apply_phase9_schema_upgrade",
    "render_phase9_acl_rollback_sql",
    "render_phase9_uniqueness_rollback_sql",
    "render_phase9_uniqueness_upgrade_sql",
    "rollback_phase9_schema_upgrade",
    "verify_phase9_schema_upgrade",
]
