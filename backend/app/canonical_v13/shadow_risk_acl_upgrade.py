"""Scoped additive ACL required by the Phase 9 shadow-risk lineage read."""

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


SHADOW_RISK_ACL_UPGRADE_CONTRACT: Final = (
    "canonical-v13-shadow-risk-lineage-acl-upgrade-v1"
)
SHADOW_RISK_WRITER_READ_DELTA: Final[tuple[str, ...]] = (
    "deployment_approvals",
    "qualification_decisions",
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


class CanonicalShadowRiskAclUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class ShadowRiskAclUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    risk_writer_privileges: dict[str, dict[str, bool]]
    repeat_noop: bool
    receipt_digest: str


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _privileges(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> dict[str, dict[str, bool]]:
    role = role_mapping.physical("canonical_risk_writer")
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
        for table_name in SHADOW_RISK_WRITER_READ_DELTA
    }


def _manifest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise CanonicalShadowRiskAclUpgradeBlocked(
            "BLOCKED_SHADOW_RISK_ACL_SCHEMA_METADATA"
        )
    return value


def _result(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    status: str,
    repeat_noop: bool,
) -> ShadowRiskAclUpgradeResult:
    payload = {
        "contract": SHADOW_RISK_ACL_UPGRADE_CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "risk_writer_privileges": _privileges(
            connection, role_mapping=role_mapping
        ),
        "repeat_noop": repeat_noop,
    }
    return ShadowRiskAclUpgradeResult(
        **payload,
        receipt_digest=_digest(payload),
    )


def verify_shadow_risk_acl_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> ShadowRiskAclUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalShadowRiskAclUpgradeBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    genesis = verify_canonical_genesis(connection)
    if not genesis.accepted or genesis.manifest_digest != CANONICAL_MANIFEST_DIGEST:
        raise CanonicalShadowRiskAclUpgradeBlocked(
            "BLOCKED_SHADOW_RISK_ACL_CANONICAL_GENESIS"
        )
    privileges = _privileges(connection, role_mapping=role_mapping)
    previous = {
        table_name: {privilege: False for privilege in _TARGET_PRIVILEGES}
        for table_name in SHADOW_RISK_WRITER_READ_DELTA
    }
    accepted = {
        table_name: {
            privilege: privilege == "SELECT" for privilege in _TARGET_PRIVILEGES
        }
        for table_name in SHADOW_RISK_WRITER_READ_DELTA
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
    raise CanonicalShadowRiskAclUpgradeBlocked(
        "BLOCKED_PARTIAL_SHADOW_RISK_ACL_UPGRADE"
    )


def apply_shadow_risk_acl_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> ShadowRiskAclUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_240_825},
    )
    before = verify_shadow_risk_acl_upgrade(connection, role_mapping=role_mapping)
    if before.status == "ACCEPTED":
        return before
    role = role_mapping.physical("canonical_risk_writer")
    for table_name in SHADOW_RISK_WRITER_READ_DELTA:
        connection.execute(
            text(
                f"GRANT SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
                f"TO {role}"
            )
        )
    verify_shadow_risk_acl_upgrade(connection, role_mapping=role_mapping)
    accepted = _result(
        connection,
        role_mapping=role_mapping,
        status="ACCEPTED",
        repeat_noop=True,
    )
    payload = asdict(accepted)
    payload.update(status="UPGRADED", repeat_noop=False)
    payload.pop("receipt_digest")
    return ShadowRiskAclUpgradeResult(
        **payload,
        receipt_digest=_digest(payload),
    )


def rollback_shadow_risk_acl_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> ShadowRiskAclUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_240_825},
    )
    before = verify_shadow_risk_acl_upgrade(connection, role_mapping=role_mapping)
    if before.status == "PREVIOUS_READY":
        return before
    role = role_mapping.physical("canonical_risk_writer")
    for table_name in SHADOW_RISK_WRITER_READ_DELTA:
        connection.execute(
            text(
                f"REVOKE SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
                f"FROM {role}"
            )
        )
    previous = verify_shadow_risk_acl_upgrade(
        connection, role_mapping=role_mapping
    )
    payload = asdict(previous)
    payload.update(status="ROLLED_BACK", repeat_noop=False)
    payload.pop("receipt_digest")
    return ShadowRiskAclUpgradeResult(
        **payload,
        receipt_digest=_digest(payload),
    )


__all__ = [
    "CanonicalShadowRiskAclUpgradeBlocked",
    "SHADOW_RISK_ACL_UPGRADE_CONTRACT",
    "SHADOW_RISK_WRITER_READ_DELTA",
    "ShadowRiskAclUpgradeResult",
    "apply_shadow_risk_acl_upgrade",
    "rollback_shadow_risk_acl_upgrade",
    "verify_shadow_risk_acl_upgrade",
]
