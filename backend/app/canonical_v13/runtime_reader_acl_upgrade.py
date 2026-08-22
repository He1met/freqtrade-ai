"""Additive, reversible runtime-reader qualification lineage ACL rollover."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Final
from uuid import uuid4

from sqlalchemy import Connection, func, select, text

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
)
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    SCHEMA_METADATA_TABLE,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


PREVIOUS_RUNTIME_READER_ACL_MANIFEST_DIGEST: Final = (
    "44c990ebddc1f04c10aefa26695a33c89147209a61aa00be7c16d13fe59e4ed6"
)
RUNTIME_READER_ACL_UPGRADE_CONTRACT: Final = (
    "canonical-v13-runtime-reader-qualification-acl-upgrade-v1"
)
_ADVISORY_LOCK_KEY: Final = 1_308_202_608_210_725
_TARGET_TABLE: Final = "qualification_decisions"
_PRIVILEGES: Final = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


class CanonicalRuntimeReaderAclUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeReaderAclUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    qualification_decision_count: int
    privileges: dict[str, bool]
    repeat_noop: bool
    receipt_digest: str


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _manifest_digest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise CanonicalRuntimeReaderAclUpgradeBlocked(
            "BLOCKED_RUNTIME_READER_ACL_SCHEMA_METADATA"
        )
    return value


def _privileges(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> dict[str, bool]:
    role = role_mapping.physical("canonical_runtime_reader")
    qualified = f"{CANONICAL_BUSINESS_SCHEMA}.{_TARGET_TABLE}"
    return {
        privilege: bool(
            connection.execute(
                text("SELECT has_table_privilege(:role, :table, :privilege)"),
                {"role": role, "table": qualified, "privilege": privilege},
            ).scalar_one()
        )
        for privilege in _PRIVILEGES
    }


def _result(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    status: str,
    repeat_noop: bool,
) -> RuntimeReaderAclUpgradeResult:
    payload = {
        "contract": RUNTIME_READER_ACL_UPGRADE_CONTRACT,
        "status": status,
        "manifest_digest": _manifest_digest(connection),
        "qualification_decision_count": int(
            connection.execute(
                select(func.count()).select_from(QUALIFICATION_DECISIONS_TABLE)
            ).scalar_one()
        ),
        "privileges": _privileges(connection, role_mapping=role_mapping),
        "repeat_noop": repeat_noop,
    }
    receipt_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"status", "repeat_noop"}
    }
    return RuntimeReaderAclUpgradeResult(
        **payload, receipt_digest=_digest(receipt_payload)
    )


def verify_runtime_reader_acl_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> RuntimeReaderAclUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalRuntimeReaderAclUpgradeBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    manifest_digest = _manifest_digest(connection)
    if manifest_digest not in {
        PREVIOUS_RUNTIME_READER_ACL_MANIFEST_DIGEST,
        CANONICAL_MANIFEST_DIGEST,
    }:
        raise CanonicalRuntimeReaderAclUpgradeBlocked(
            "BLOCKED_RUNTIME_READER_ACL_MANIFEST_DRIFT"
        )
    genesis = verify_canonical_genesis(
        connection,
        accepted_manifest_digests=(
            PREVIOUS_RUNTIME_READER_ACL_MANIFEST_DIGEST,
            CANONICAL_MANIFEST_DIGEST,
        ),
    )
    if not genesis.accepted:
        raise CanonicalRuntimeReaderAclUpgradeBlocked(
            "BLOCKED_RUNTIME_READER_ACL_WRONG_CANONICAL_DATABASE"
        )
    privileges = _privileges(connection, role_mapping=role_mapping)
    expected = {privilege: privilege == "SELECT" for privilege in _PRIVILEGES}
    if manifest_digest == CANONICAL_MANIFEST_DIGEST and privileges == expected:
        return _result(
            connection,
            role_mapping=role_mapping,
            status="ACCEPTED",
            repeat_noop=True,
        )
    if (
        manifest_digest == PREVIOUS_RUNTIME_READER_ACL_MANIFEST_DIGEST
        and not any(privileges.values())
    ):
        return _result(
            connection,
            role_mapping=role_mapping,
            status="PREVIOUS_READY",
            repeat_noop=True,
        )
    raise CanonicalRuntimeReaderAclUpgradeBlocked(
        "BLOCKED_PARTIAL_RUNTIME_READER_ACL_UPGRADE"
    )


def _append_audit(
    connection: Connection,
    *,
    actor_identity: str,
    event_type: str,
    before_digest: str,
    after_digest: str,
) -> None:
    evidence = {
        "contract": RUNTIME_READER_ACL_UPGRADE_CONTRACT,
        "event_type": event_type,
        "before_manifest_digest": before_digest,
        "after_manifest_digest": after_digest,
        "role": "canonical_runtime_reader",
        "table": _TARGET_TABLE,
        "grant": "SELECT",
        "destructive_row_operations": 0,
    }
    request_digest = _digest(evidence)
    receipt_digest = _digest(
        {"aggregate": RUNTIME_READER_ACL_UPGRADE_CONTRACT, "request": request_digest}
    )
    connection.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=uuid4(),
            event_type=event_type,
            aggregate_type="canonical_runtime_reader_acl_upgrade",
            aggregate_id=after_digest,
            actor_identity=actor_identity,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            evidence_json=evidence,
            created_at=datetime.now(timezone.utc),
        )
    )


def apply_runtime_reader_acl_upgrade(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    actor_identity: str,
) -> RuntimeReaderAclUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY}
    )
    before = verify_runtime_reader_acl_upgrade(connection, role_mapping=role_mapping)
    if before.status == "ACCEPTED":
        return before
    role = role_mapping.physical("canonical_runtime_reader")
    connection.execute(
        text(
            f"GRANT SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{_TARGET_TABLE} "
            f"TO {role}"
        )
    )
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=CANONICAL_MANIFEST_DIGEST)
    )
    _append_audit(
        connection,
        actor_identity=actor_identity,
        event_type="RUNTIME_READER_QUALIFICATION_ACL_UPGRADED",
        before_digest=before.manifest_digest,
        after_digest=CANONICAL_MANIFEST_DIGEST,
    )
    verify_runtime_reader_acl_upgrade(connection, role_mapping=role_mapping)
    return _result(
        connection,
        role_mapping=role_mapping,
        status="UPGRADED",
        repeat_noop=False,
    )


def rollback_runtime_reader_acl_upgrade(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    actor_identity: str,
) -> RuntimeReaderAclUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY}
    )
    before = verify_runtime_reader_acl_upgrade(connection, role_mapping=role_mapping)
    if before.status == "PREVIOUS_READY":
        return before
    role = role_mapping.physical("canonical_runtime_reader")
    connection.execute(
        text(
            f"REVOKE SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{_TARGET_TABLE} "
            f"FROM {role}"
        )
    )
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=PREVIOUS_RUNTIME_READER_ACL_MANIFEST_DIGEST)
    )
    _append_audit(
        connection,
        actor_identity=actor_identity,
        event_type="RUNTIME_READER_QUALIFICATION_ACL_ROLLED_BACK",
        before_digest=before.manifest_digest,
        after_digest=PREVIOUS_RUNTIME_READER_ACL_MANIFEST_DIGEST,
    )
    verify_runtime_reader_acl_upgrade(connection, role_mapping=role_mapping)
    return _result(
        connection,
        role_mapping=role_mapping,
        status="ROLLED_BACK",
        repeat_noop=False,
    )


__all__ = [
    "CanonicalRuntimeReaderAclUpgradeBlocked",
    "PREVIOUS_RUNTIME_READER_ACL_MANIFEST_DIGEST",
    "RUNTIME_READER_ACL_UPGRADE_CONTRACT",
    "RuntimeReaderAclUpgradeResult",
    "apply_runtime_reader_acl_upgrade",
    "rollback_runtime_reader_acl_upgrade",
    "verify_runtime_reader_acl_upgrade",
]
