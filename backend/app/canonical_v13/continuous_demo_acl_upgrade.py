"""Scoped additive reads required by bounded continuous OKX_DEMO services."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Final

from sqlalchemy import Connection, select, text

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA, CANONICAL_MANIFEST_DIGEST
from app.canonical_v13.models import SCHEMA_METADATA_TABLE
from app.canonical_v13.role_mapping import CanonicalRoleMapping
from app.canonical_v13.continuous_demo_upgrade import (
    CanonicalContinuousDemoUpgradeBlocked,
    verify_continuous_demo_upgrade,
)


CONTRACT: Final = "canonical-v13-bounded-continuous-demo-acl-upgrade-v1"
CONTINUOUS_RISK_WRITER_READ_DELTA: Final[tuple[str, ...]] = (
    "strategy_versions",
    "strategy_artifacts",
    "runtime_instances",
    "execution_canary_probe_receipts",
    "execution_attestations",
    "orders",
    "fills",
    "ledger_entries",
    "reconciliation_items",
    "order_writer_leases",
)
CONTINUOUS_RECONCILIATION_WRITER_READ_DELTA: Final[tuple[str, ...]] = (
    "risk_decisions",
    "execution_canary_probe_receipts",
    "execution_attestations",
    "deployments",
)
ROLE_DELTAS: Final = {
    "canonical_risk_writer": CONTINUOUS_RISK_WRITER_READ_DELTA,
    "canonical_reconciliation_writer": CONTINUOUS_RECONCILIATION_WRITER_READ_DELTA,
}
_PRIVILEGES: Final = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


class CanonicalContinuousDemoAclUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class ContinuousDemoAclUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    privileges: dict[str, dict[str, dict[str, bool]]]
    repeat_noop: bool
    receipt_digest: str


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _manifest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise CanonicalContinuousDemoAclUpgradeBlocked(
            "BLOCKED_CONTINUOUS_DEMO_ACL_SCHEMA_METADATA"
        )
    return value


def _privileges(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> dict[str, dict[str, dict[str, bool]]]:
    return {
        logical_role: {
            table_name: {
                privilege: bool(
                    connection.execute(
                        text("SELECT has_table_privilege(:role,:table,:privilege)"),
                        {
                            "role": role_mapping.physical(logical_role),
                            "table": f"{CANONICAL_BUSINESS_SCHEMA}.{table_name}",
                            "privilege": privilege,
                        },
                    ).scalar_one()
                )
                for privilege in _PRIVILEGES
            }
            for table_name in table_names
        }
        for logical_role, table_names in ROLE_DELTAS.items()
    }


def _result(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    status: str,
    repeat_noop: bool,
) -> ContinuousDemoAclUpgradeResult:
    payload = {
        "contract": CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "privileges": _privileges(connection, role_mapping=role_mapping),
        "repeat_noop": repeat_noop,
    }
    return ContinuousDemoAclUpgradeResult(**payload, receipt_digest=_digest(payload))


def verify_continuous_demo_acl_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> ContinuousDemoAclUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalContinuousDemoAclUpgradeBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    genesis = verify_canonical_genesis(connection)
    if not genesis.accepted or genesis.manifest_digest != CANONICAL_MANIFEST_DIGEST:
        raise CanonicalContinuousDemoAclUpgradeBlocked(
            "BLOCKED_CONTINUOUS_DEMO_ACL_CANONICAL_GENESIS"
        )
    try:
        schema_status = verify_continuous_demo_upgrade(connection).status
    except CanonicalContinuousDemoUpgradeBlocked as exc:
        raise CanonicalContinuousDemoAclUpgradeBlocked(
            "BLOCKED_CONTINUOUS_DEMO_ACL_SCHEMA"
        ) from exc
    observed = _privileges(connection, role_mapping=role_mapping)
    previous = {
        role: {
            table: {privilege: False for privilege in _PRIVILEGES}
            for table in tables
        }
        for role, tables in ROLE_DELTAS.items()
    }
    accepted = {
        role: {
            table: {
                privilege: privilege == "SELECT" for privilege in _PRIVILEGES
            }
            for table in tables
        }
        for role, tables in ROLE_DELTAS.items()
    }
    if observed == previous:
        return _result(
            connection,
            role_mapping=role_mapping,
            status="PREVIOUS_READY",
            repeat_noop=True,
        )
    if observed == accepted:
        if schema_status != "ACCEPTED":
            raise CanonicalContinuousDemoAclUpgradeBlocked(
                "BLOCKED_CONTINUOUS_DEMO_ACL_SCHEMA_REQUIRED"
            )
        return _result(
            connection,
            role_mapping=role_mapping,
            status="ACCEPTED",
            repeat_noop=True,
        )
    raise CanonicalContinuousDemoAclUpgradeBlocked(
        "BLOCKED_PARTIAL_CONTINUOUS_DEMO_ACL_UPGRADE"
    )


def apply_continuous_demo_acl_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> ContinuousDemoAclUpgradeResult:
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1308202608261502})
    before = verify_continuous_demo_acl_upgrade(connection, role_mapping=role_mapping)
    if before.status == "ACCEPTED":
        return before
    if verify_continuous_demo_upgrade(connection).status != "ACCEPTED":
        raise CanonicalContinuousDemoAclUpgradeBlocked(
            "BLOCKED_CONTINUOUS_DEMO_ACL_SCHEMA_REQUIRED"
        )
    for logical_role, tables in ROLE_DELTAS.items():
        role = role_mapping.physical(logical_role)
        for table_name in tables:
            connection.execute(
                text(
                    f"GRANT SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} TO {role}"
                )
            )
    accepted = verify_continuous_demo_acl_upgrade(connection, role_mapping=role_mapping)
    payload = asdict(accepted)
    payload.update(status="UPGRADED", repeat_noop=False)
    payload.pop("receipt_digest")
    return ContinuousDemoAclUpgradeResult(**payload, receipt_digest=_digest(payload))


def rollback_continuous_demo_acl_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> ContinuousDemoAclUpgradeResult:
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1308202608261502})
    before = verify_continuous_demo_acl_upgrade(connection, role_mapping=role_mapping)
    if before.status == "PREVIOUS_READY":
        return before
    schema = CANONICAL_BUSINESS_SCHEMA
    evidence_count = int(
        connection.execute(
            text(
                f"SELECT (SELECT count(*) FROM {schema}.trade_intents "
                "WHERE intent_mode IN ('CONTINUOUS_OPEN','POSITION_EXIT')) + "
                f"(SELECT count(*) FROM {schema}.risk_decisions "
                "WHERE decision_mode IN ('CONTINUOUS_OPEN','POSITION_EXIT')) + "
                f"(SELECT count(*) FROM {schema}.order_dispatch_receipts "
                "WHERE dispatch_mode IN ('CONTINUOUS_OPEN','POSITION_EXIT'))"
            )
        ).scalar_one()
    )
    if evidence_count:
        raise CanonicalContinuousDemoAclUpgradeBlocked(
            "BLOCKED_CONTINUOUS_DEMO_ACL_ROLLBACK_HAS_EXECUTION_LINEAGE"
        )
    for logical_role, tables in ROLE_DELTAS.items():
        role = role_mapping.physical(logical_role)
        for table_name in tables:
            connection.execute(
                text(
                    f"REVOKE SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} FROM {role}"
                )
            )
    previous = verify_continuous_demo_acl_upgrade(connection, role_mapping=role_mapping)
    payload = asdict(previous)
    payload.update(status="ROLLED_BACK", repeat_noop=False)
    payload.pop("receipt_digest")
    return ContinuousDemoAclUpgradeResult(**payload, receipt_digest=_digest(payload))


__all__ = [
    "CONTINUOUS_RECONCILIATION_WRITER_READ_DELTA",
    "CONTINUOUS_RISK_WRITER_READ_DELTA",
    "CanonicalContinuousDemoAclUpgradeBlocked",
    "ContinuousDemoAclUpgradeResult",
    "apply_continuous_demo_acl_upgrade",
    "rollback_continuous_demo_acl_upgrade",
    "verify_continuous_demo_acl_upgrade",
]
