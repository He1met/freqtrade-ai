"""Scoped read-only ACL for atomic canary order recovery validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Final

from sqlalchemy import Connection, select, text

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
)
from app.canonical_v13.models import SCHEMA_METADATA_TABLE
from app.canonical_v13.role_mapping import CanonicalRoleMapping


ORDER_RECOVERY_EVIDENCE_ACL_UPGRADE_CONTRACT: Final = (
    "canonical-v13-order-recovery-evidence-acl-upgrade-v1"
)
ORDER_WRITER_RECOVERY_READ_DELTA: Final[tuple[str, ...]] = (
    "fills",
    "ledger_entries",
    "reconciliation_items",
)
_TARGET_PRIVILEGES: Final[tuple[str, ...]] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


class CanonicalOrderRecoveryEvidenceAclUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderRecoveryEvidenceAclUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    order_writer_privileges: dict[str, dict[str, bool]]
    repeat_noop: bool
    receipt_digest: str


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _privileges(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> dict[str, dict[str, bool]]:
    role = role_mapping.physical("canonical_order_writer")
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
            for privilege in _TARGET_PRIVILEGES
        }
        for table_name in ORDER_WRITER_RECOVERY_READ_DELTA
    }


def _manifest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise CanonicalOrderRecoveryEvidenceAclUpgradeBlocked(
            "BLOCKED_ORDER_RECOVERY_EVIDENCE_ACL_SCHEMA_METADATA"
        )
    return value


def _result(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    status: str,
    repeat_noop: bool,
) -> OrderRecoveryEvidenceAclUpgradeResult:
    payload = {
        "contract": ORDER_RECOVERY_EVIDENCE_ACL_UPGRADE_CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "order_writer_privileges": _privileges(
            connection, role_mapping=role_mapping
        ),
        "repeat_noop": repeat_noop,
    }
    return OrderRecoveryEvidenceAclUpgradeResult(
        **payload,
        receipt_digest=_digest(payload),
    )


def verify_order_recovery_evidence_acl_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> OrderRecoveryEvidenceAclUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalOrderRecoveryEvidenceAclUpgradeBlocked(
            "BLOCKED_POSTGRESQL_REQUIRED"
        )
    genesis = verify_canonical_genesis(connection)
    if not genesis.accepted or genesis.manifest_digest != CANONICAL_MANIFEST_DIGEST:
        raise CanonicalOrderRecoveryEvidenceAclUpgradeBlocked(
            "BLOCKED_ORDER_RECOVERY_EVIDENCE_ACL_CANONICAL_GENESIS"
        )
    privileges = _privileges(connection, role_mapping=role_mapping)
    previous = {
        table_name: {privilege: False for privilege in _TARGET_PRIVILEGES}
        for table_name in ORDER_WRITER_RECOVERY_READ_DELTA
    }
    accepted = {
        table_name: {
            privilege: privilege == "SELECT" for privilege in _TARGET_PRIVILEGES
        }
        for table_name in ORDER_WRITER_RECOVERY_READ_DELTA
    }
    if privileges == previous:
        return _result(
            connection,
            role_mapping=role_mapping,
            status="PREVIOUS_READY",
            repeat_noop=True,
        )
    if privileges == accepted:
        return _result(
            connection,
            role_mapping=role_mapping,
            status="ACCEPTED",
            repeat_noop=True,
        )
    raise CanonicalOrderRecoveryEvidenceAclUpgradeBlocked(
        "BLOCKED_PARTIAL_ORDER_RECOVERY_EVIDENCE_ACL_UPGRADE"
    )


def apply_order_recovery_evidence_acl_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> OrderRecoveryEvidenceAclUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_250_868},
    )
    before = verify_order_recovery_evidence_acl_upgrade(
        connection, role_mapping=role_mapping
    )
    if before.status == "ACCEPTED":
        return before
    role = role_mapping.physical("canonical_order_writer")
    for table_name in ORDER_WRITER_RECOVERY_READ_DELTA:
        connection.execute(
            text(
                f"GRANT SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
                f"TO {role}"
            )
        )
    verify_order_recovery_evidence_acl_upgrade(
        connection, role_mapping=role_mapping
    )
    accepted = _result(
        connection,
        role_mapping=role_mapping,
        status="ACCEPTED",
        repeat_noop=True,
    )
    payload = asdict(accepted)
    payload.update(status="UPGRADED", repeat_noop=False)
    payload.pop("receipt_digest")
    return OrderRecoveryEvidenceAclUpgradeResult(
        **payload,
        receipt_digest=_digest(payload),
    )


def rollback_order_recovery_evidence_acl_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> OrderRecoveryEvidenceAclUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_250_868},
    )
    before = verify_order_recovery_evidence_acl_upgrade(
        connection, role_mapping=role_mapping
    )
    if before.status == "PREVIOUS_READY":
        return before
    role = role_mapping.physical("canonical_order_writer")
    for table_name in ORDER_WRITER_RECOVERY_READ_DELTA:
        connection.execute(
            text(
                f"REVOKE SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
                f"FROM {role}"
            )
        )
    previous = verify_order_recovery_evidence_acl_upgrade(
        connection, role_mapping=role_mapping
    )
    payload = asdict(previous)
    payload.update(status="ROLLED_BACK", repeat_noop=False)
    payload.pop("receipt_digest")
    return OrderRecoveryEvidenceAclUpgradeResult(
        **payload,
        receipt_digest=_digest(payload),
    )


__all__ = [
    "CanonicalOrderRecoveryEvidenceAclUpgradeBlocked",
    "ORDER_RECOVERY_EVIDENCE_ACL_UPGRADE_CONTRACT",
    "ORDER_WRITER_RECOVERY_READ_DELTA",
    "OrderRecoveryEvidenceAclUpgradeResult",
    "apply_order_recovery_evidence_acl_upgrade",
    "rollback_order_recovery_evidence_acl_upgrade",
    "verify_order_recovery_evidence_acl_upgrade",
]
