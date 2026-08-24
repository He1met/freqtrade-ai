"""Bounded schema and read-only ACL for one canary recovery approval."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Final

from sqlalchemy import Connection, inspect, select, text

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
)
from app.canonical_v13.models import SCHEMA_METADATA_TABLE
from app.canonical_v13.role_mapping import CanonicalRoleMapping


CONTRACT: Final = "canonical-v13-canary-recovery-approval-upgrade-v1"
TABLE: Final = "deployment_approvals"
RECOVERY_COLUMNS: Final[tuple[str, ...]] = (
    "approval_generation",
    "recovery_of_deployment_id",
    "recovery_order_id",
    "recovery_idempotency_key",
    "recovery_request_digest",
    "recovery_receipt_digest",
)
APPROVAL_WRITER_READ_DELTA: Final[tuple[str, ...]] = (
    "fills",
    "ledger_entries",
    "order_dispatch_outcome_receipts",
    "order_dispatch_receipts",
    "order_writer_leases",
    "runtime_instances",
    "signals",
    "trade_intents",
)
OLD_UNIQUE: Final = "deployment_approvals_qualification_unique"
NEW_CONSTRAINTS: Final[tuple[str, ...]] = (
    "deployment_approvals_generation_bounded",
    "deployment_approvals_qualification_generation_unique",
    "deployment_approvals_recovery_deployment_fk",
    "deployment_approvals_recovery_deployment_unique",
    "deployment_approvals_recovery_evidence_complete",
    "deployment_approvals_recovery_key_unique",
    "deployment_approvals_recovery_order_fk",
    "deployment_approvals_recovery_order_unique",
    "deployment_approvals_recovery_receipt_unique",
    "deployment_approvals_recovery_request_unique",
)
TARGET_PRIVILEGES: Final[tuple[str, ...]] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


class CanonicalCanaryRecoveryApprovalUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class CanaryRecoveryApprovalUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    approval_count: int
    generation_one_count: int
    generation_two_count: int
    lineage_digest: str
    approval_writer_privileges: dict[str, dict[str, bool]]
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
        raise CanonicalCanaryRecoveryApprovalUpgradeBlocked(
            "BLOCKED_CANARY_RECOVERY_SCHEMA_METADATA"
        )
    return value


def _column_names(connection: Connection) -> set[str]:
    return {
        str(row["name"])
        for row in inspect(connection).get_columns(
            TABLE, schema=CANONICAL_BUSINESS_SCHEMA
        )
    }


def _constraint_names(connection: Connection) -> set[str]:
    inspector = inspect(connection)
    rows = [
        *inspector.get_unique_constraints(TABLE, schema=CANONICAL_BUSINESS_SCHEMA),
        *inspector.get_check_constraints(TABLE, schema=CANONICAL_BUSINESS_SCHEMA),
        *inspector.get_foreign_keys(TABLE, schema=CANONICAL_BUSINESS_SCHEMA),
    ]
    return {str(row["name"]) for row in rows if row.get("name")}


def _privileges(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> dict[str, dict[str, bool]]:
    role = role_mapping.physical("canonical_approval_writer")
    return {
        table_name: {
            privilege: bool(
                connection.execute(
                    text("SELECT has_table_privilege(:role, :table, :privilege)"),
                    {
                        "role": role,
                        "table": f"{CANONICAL_BUSINESS_SCHEMA}.{table_name}",
                        "privilege": privilege,
                    },
                ).scalar_one()
            )
            for privilege in TARGET_PRIVILEGES
        }
        for table_name in APPROVAL_WRITER_READ_DELTA
    }


def _expected_privileges(enabled: bool) -> dict[str, dict[str, bool]]:
    return {
        table_name: {
            privilege: enabled and privilege == "SELECT"
            for privilege in TARGET_PRIVILEGES
        }
        for table_name in APPROVAL_WRITER_READ_DELTA
    }


def _state(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> str:
    columns = _column_names(connection)
    constraints = _constraint_names(connection)
    privileges = _privileges(connection, role_mapping=role_mapping)
    previous = (
        not (set(RECOVERY_COLUMNS) & columns)
        and OLD_UNIQUE in constraints
        and privileges == _expected_privileges(False)
    )
    schema_ready = (
        set(RECOVERY_COLUMNS).issubset(columns)
        and OLD_UNIQUE not in constraints
        and set(NEW_CONSTRAINTS).issubset(constraints)
    )
    if previous:
        return "PREVIOUS_READY"
    if schema_ready and privileges == _expected_privileges(False):
        return "SCHEMA_READY"
    if schema_ready and privileges == _expected_privileges(True):
        return "ACCEPTED"
    raise CanonicalCanaryRecoveryApprovalUpgradeBlocked(
        "BLOCKED_PARTIAL_CANARY_RECOVERY_APPROVAL_UPGRADE"
    )


def _counts(connection: Connection, *, state: str) -> tuple[int, int, int]:
    if state == "PREVIOUS_READY":
        count = int(
            connection.execute(
                text(f"SELECT count(*) FROM {CANONICAL_BUSINESS_SCHEMA}.{TABLE}")
            ).scalar_one()
        )
        return count, count, 0
    row = connection.execute(
        text(
            f"SELECT count(*) AS total, "
            "count(*) FILTER (WHERE approval_generation=1) AS generation_one, "
            "count(*) FILTER (WHERE approval_generation=2) AS generation_two "
            f"FROM {CANONICAL_BUSINESS_SCHEMA}.{TABLE}"
        )
    ).mappings().one()
    return int(row["total"]), int(row["generation_one"]), int(row["generation_two"])


def _lineage_digest(connection: Connection, *, state: str) -> str:
    recovery_fields = (
        "NULL::text AS approval_generation, NULL::text AS recovery_of_deployment_id, "
        "NULL::text AS recovery_order_id, NULL::text AS recovery_request_digest, "
        "NULL::text AS recovery_receipt_digest"
        if state == "PREVIOUS_READY"
        else "approval_generation::text, recovery_of_deployment_id::text, "
        "recovery_order_id::text, recovery_request_digest, recovery_receipt_digest"
    )
    rows = connection.execute(
        text(
            "SELECT id::text, qualification_decision_id::text, approval_digest, "
            f"{recovery_fields} FROM {CANONICAL_BUSINESS_SCHEMA}.{TABLE} "
            "ORDER BY created_at, id"
        )
    ).mappings()
    return _digest([dict(row) for row in rows])


def _result(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    status: str,
    repeat_noop: bool,
) -> CanaryRecoveryApprovalUpgradeResult:
    observed_state = "PREVIOUS_READY" if status == "PREVIOUS_READY" else "ACCEPTED"
    counts = _counts(connection, state=observed_state)
    payload = {
        "contract": CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "approval_count": counts[0],
        "generation_one_count": counts[1],
        "generation_two_count": counts[2],
        "lineage_digest": _lineage_digest(connection, state=observed_state),
        "approval_writer_privileges": _privileges(
            connection, role_mapping=role_mapping
        ),
        "repeat_noop": repeat_noop,
    }
    return CanaryRecoveryApprovalUpgradeResult(
        **payload, receipt_digest=_digest(payload)
    )


def verify_canary_recovery_approval_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> CanaryRecoveryApprovalUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalCanaryRecoveryApprovalUpgradeBlocked(
            "BLOCKED_POSTGRESQL_REQUIRED"
        )
    genesis = verify_canonical_genesis(connection)
    if not genesis.accepted or genesis.manifest_digest != CANONICAL_MANIFEST_DIGEST:
        raise CanonicalCanaryRecoveryApprovalUpgradeBlocked(
            "BLOCKED_CANARY_RECOVERY_CANONICAL_GENESIS"
        )
    state = _state(connection, role_mapping=role_mapping)
    counts = _counts(connection, state=state)
    if counts[0] != counts[1] + counts[2] or counts[2] > 1:
        raise CanonicalCanaryRecoveryApprovalUpgradeBlocked(
            "BLOCKED_CANARY_RECOVERY_GENERATION_COUNTS"
        )
    return _result(
        connection, role_mapping=role_mapping, status=state, repeat_noop=True
    )


def apply_canary_recovery_approval_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> CanaryRecoveryApprovalUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_241_204},
    )
    before = verify_canary_recovery_approval_upgrade(
        connection, role_mapping=role_mapping
    )
    if before.status == "ACCEPTED":
        return before
    schema = CANONICAL_BUSINESS_SCHEMA
    if before.status == "PREVIOUS_READY":
        connection.execute(
            text(
                f"ALTER TABLE {schema}.{TABLE} "
                "ADD COLUMN approval_generation integer DEFAULT 1, "
                "ADD COLUMN recovery_of_deployment_id uuid, "
                "ADD COLUMN recovery_order_id uuid, "
                "ADD COLUMN recovery_idempotency_key varchar(200), "
                "ADD COLUMN recovery_request_digest varchar(64), "
                "ADD COLUMN recovery_receipt_digest varchar(64)"
            )
        )
        connection.execute(
            text(
                f"ALTER TABLE {schema}.{TABLE} "
                "ALTER COLUMN approval_generation SET NOT NULL, "
                "ALTER COLUMN approval_generation DROP DEFAULT, "
                f"DROP CONSTRAINT {OLD_UNIQUE}, "
                "ADD CONSTRAINT deployment_approvals_generation_bounded "
                "CHECK (approval_generation > 0 AND approval_generation <= 2), "
                "ADD CONSTRAINT deployment_approvals_recovery_evidence_complete CHECK ("
                "(approval_generation=1 AND recovery_of_deployment_id IS NULL "
                "AND recovery_order_id IS NULL AND recovery_idempotency_key IS NULL "
                "AND recovery_request_digest IS NULL AND recovery_receipt_digest IS NULL) OR "
                "(approval_generation=2 AND recovery_of_deployment_id IS NOT NULL "
                "AND recovery_order_id IS NOT NULL AND recovery_idempotency_key IS NOT NULL "
                "AND recovery_request_digest IS NOT NULL AND recovery_receipt_digest IS NOT NULL)), "
                "ADD CONSTRAINT deployment_approvals_qualification_generation_unique "
                "UNIQUE (qualification_decision_id, approval_generation), "
                "ADD CONSTRAINT deployment_approvals_recovery_deployment_unique "
                "UNIQUE (recovery_of_deployment_id), "
                "ADD CONSTRAINT deployment_approvals_recovery_order_unique "
                "UNIQUE (recovery_order_id), "
                "ADD CONSTRAINT deployment_approvals_recovery_key_unique "
                "UNIQUE (recovery_idempotency_key), "
                "ADD CONSTRAINT deployment_approvals_recovery_request_unique "
                "UNIQUE (recovery_request_digest), "
                "ADD CONSTRAINT deployment_approvals_recovery_receipt_unique "
                "UNIQUE (recovery_receipt_digest), "
                "ADD CONSTRAINT deployment_approvals_recovery_deployment_fk "
                f"FOREIGN KEY (recovery_of_deployment_id) REFERENCES {schema}.deployments(id) "
                "ON DELETE RESTRICT, "
                "ADD CONSTRAINT deployment_approvals_recovery_order_fk "
                f"FOREIGN KEY (recovery_order_id) REFERENCES {schema}.orders(id) "
                "ON DELETE RESTRICT"
            )
        )
    role = role_mapping.physical("canonical_approval_writer")
    for table_name in APPROVAL_WRITER_READ_DELTA:
        connection.execute(
            text(f"GRANT SELECT ON TABLE {schema}.{table_name} TO {role}")
        )
    verify_canary_recovery_approval_upgrade(connection, role_mapping=role_mapping)
    accepted = _result(
        connection,
        role_mapping=role_mapping,
        status="ACCEPTED",
        repeat_noop=True,
    )
    payload = asdict(accepted)
    payload.update(status="UPGRADED", repeat_noop=False)
    payload.pop("receipt_digest")
    return CanaryRecoveryApprovalUpgradeResult(
        **payload, receipt_digest=_digest(payload)
    )


def rollback_canary_recovery_approval_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> CanaryRecoveryApprovalUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_241_204},
    )
    before = verify_canary_recovery_approval_upgrade(
        connection, role_mapping=role_mapping
    )
    if before.status == "PREVIOUS_READY":
        return before
    if before.generation_two_count:
        raise CanonicalCanaryRecoveryApprovalUpgradeBlocked(
            "BLOCKED_CANARY_RECOVERY_ROLLBACK_EVIDENCE_PRESENT"
        )
    schema = CANONICAL_BUSINESS_SCHEMA
    role = role_mapping.physical("canonical_approval_writer")
    for table_name in APPROVAL_WRITER_READ_DELTA:
        connection.execute(
            text(f"REVOKE SELECT ON TABLE {schema}.{table_name} FROM {role}")
        )
    connection.execute(
        text(
            f"ALTER TABLE {schema}.{TABLE} "
            + ", ".join(f"DROP CONSTRAINT {name}" for name in NEW_CONSTRAINTS)
            + f", ADD CONSTRAINT {OLD_UNIQUE} UNIQUE (qualification_decision_id), "
            + ", ".join(f"DROP COLUMN {name}" for name in reversed(RECOVERY_COLUMNS))
        )
    )
    previous = verify_canary_recovery_approval_upgrade(
        connection, role_mapping=role_mapping
    )
    payload = asdict(previous)
    payload.update(status="ROLLED_BACK", repeat_noop=False)
    payload.pop("receipt_digest")
    return CanaryRecoveryApprovalUpgradeResult(
        **payload, receipt_digest=_digest(payload)
    )


__all__ = [
    "APPROVAL_WRITER_READ_DELTA",
    "CanonicalCanaryRecoveryApprovalUpgradeBlocked",
    "CanaryRecoveryApprovalUpgradeResult",
    "apply_canary_recovery_approval_upgrade",
    "rollback_canary_recovery_approval_upgrade",
    "verify_canary_recovery_approval_upgrade",
]
