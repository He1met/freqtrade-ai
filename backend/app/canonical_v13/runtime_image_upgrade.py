"""Additive runtime-image authority migration over the accepted Phase 9 schema."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Final

from sqlalchemy import Connection, func, inspect, select, text

from app.canonical_v13.genesis import (
    postgresql_acl_statements,
    postgresql_owner_table_grant_statements,
    verify_canonical_genesis,
)
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA, CANONICAL_MANIFEST_DIGEST
from app.canonical_v13.models import RUNTIME_IMAGE_ACCEPTANCES_TABLE, SCHEMA_METADATA_TABLE
from app.canonical_v13.role_mapping import CanonicalRoleMapping


PREVIOUS_RUNTIME_IMAGE_MANIFEST_DIGEST: Final = (
    "f05b11c94158289c9a89271488e34a4cbcb8c1d66e6f50ba332abf879af541e3"
)
RUNTIME_IMAGE_UPGRADE_CONTRACT: Final = "canonical-v13-runtime-image-authority-upgrade-v1"
RUNTIME_IMAGE_GUARD_FUNCTION: Final = "guard_runtime_image_acceptances_append_only"
RUNTIME_IMAGE_GUARD_TRIGGER: Final = "runtime_image_acceptances_append_only"


class CanonicalRuntimeImageUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeImageUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    table_present: bool
    trigger_present: bool
    row_count: int
    repeat_noop: bool
    receipt_digest: str


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def runtime_image_trigger_statements() -> tuple[str, ...]:
    schema = CANONICAL_BUSINESS_SCHEMA
    return (
        f"""CREATE OR REPLACE FUNCTION {schema}.{RUNTIME_IMAGE_GUARD_FUNCTION}()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'canonical runtime image acceptances are append-only';
        END $$""",
        f"DROP TRIGGER IF EXISTS {RUNTIME_IMAGE_GUARD_TRIGGER} ON {schema}.runtime_image_acceptances",
        f"""CREATE TRIGGER {RUNTIME_IMAGE_GUARD_TRIGGER} BEFORE UPDATE OR DELETE
        ON {schema}.runtime_image_acceptances FOR EACH ROW
        EXECUTE FUNCTION {schema}.{RUNTIME_IMAGE_GUARD_FUNCTION}()""",
    )


def install_runtime_image_trigger(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    for statement in runtime_image_trigger_statements():
        connection.execute(text(statement))


def _manifest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise CanonicalRuntimeImageUpgradeBlocked("BLOCKED_RUNTIME_IMAGE_SCHEMA_METADATA")
    return value


def _trigger_present(connection: Connection) -> bool:
    if connection.dialect.name != "postgresql":
        return True
    return bool(
        connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger trigger "
                "JOIN pg_catalog.pg_class relation ON relation.oid=trigger.tgrelid "
                "JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace "
                "WHERE namespace.nspname=:schema AND relation.relname='runtime_image_acceptances' "
                "AND trigger.tgname=:trigger AND NOT trigger.tgisinternal)"
            ),
            {"schema": CANONICAL_BUSINESS_SCHEMA, "trigger": RUNTIME_IMAGE_GUARD_TRIGGER},
        ).scalar_one()
    )


def _result(connection: Connection, *, status: str, repeat_noop: bool) -> RuntimeImageUpgradeResult:
    present = inspect(connection).has_table(
        RUNTIME_IMAGE_ACCEPTANCES_TABLE.name, schema=CANONICAL_BUSINESS_SCHEMA
    )
    count = int(connection.execute(select(func.count()).select_from(RUNTIME_IMAGE_ACCEPTANCES_TABLE)).scalar_one()) if present else 0
    payload = {
        "contract": RUNTIME_IMAGE_UPGRADE_CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "table_present": present,
        "trigger_present": _trigger_present(connection) if present else False,
        "row_count": count,
        "repeat_noop": repeat_noop,
    }
    return RuntimeImageUpgradeResult(**payload, receipt_digest=_digest(payload))


def verify_runtime_image_upgrade(connection: Connection) -> RuntimeImageUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalRuntimeImageUpgradeBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    present = inspect(connection).has_table(
        RUNTIME_IMAGE_ACCEPTANCES_TABLE.name, schema=CANONICAL_BUSINESS_SCHEMA
    )
    manifest = _manifest(connection)
    if not present and manifest == PREVIOUS_RUNTIME_IMAGE_MANIFEST_DIGEST:
        return _result(connection, status="PREVIOUS_READY", repeat_noop=True)
    if present and manifest == CANONICAL_MANIFEST_DIGEST and _trigger_present(connection):
        verification = verify_canonical_genesis(connection)
        if verification.accepted:
            return _result(connection, status="ACCEPTED", repeat_noop=True)
    raise CanonicalRuntimeImageUpgradeBlocked("BLOCKED_PARTIAL_RUNTIME_IMAGE_UPGRADE")


def apply_runtime_image_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> RuntimeImageUpgradeResult:
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1_308_202_608_210_724})
    before = verify_runtime_image_upgrade(connection)
    if before.status == "ACCEPTED":
        return before
    RUNTIME_IMAGE_ACCEPTANCES_TABLE.create(bind=connection, checkfirst=False)
    owner = role_mapping.physical("canonical_schema_owner")
    qualified = f"{CANONICAL_BUSINESS_SCHEMA}.{RUNTIME_IMAGE_ACCEPTANCES_TABLE.name}"
    connection.execute(text(f"ALTER TABLE {qualified} OWNER TO {owner}"))
    install_runtime_image_trigger(connection)
    connection.execute(
        text(
            f"ALTER FUNCTION {CANONICAL_BUSINESS_SCHEMA}.{RUNTIME_IMAGE_GUARD_FUNCTION}() "
            f"OWNER TO {owner}"
        )
    )
    for statement in postgresql_acl_statements(role_mapping):
        connection.execute(text(statement))
    for statement in postgresql_owner_table_grant_statements(role_mapping):
        connection.execute(text(statement))
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=CANONICAL_MANIFEST_DIGEST)
    )
    verify_runtime_image_upgrade(connection)
    return _result(connection, status="UPGRADED", repeat_noop=False)


def rollback_runtime_image_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> RuntimeImageUpgradeResult:
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1_308_202_608_210_724})
    before = verify_runtime_image_upgrade(connection)
    if before.status == "PREVIOUS_READY":
        return before
    if before.row_count:
        raise CanonicalRuntimeImageUpgradeBlocked("BLOCKED_RUNTIME_IMAGE_ACCEPTANCES_NONZERO")
    schema = CANONICAL_BUSINESS_SCHEMA
    connection.execute(text(f"DROP TRIGGER {RUNTIME_IMAGE_GUARD_TRIGGER} ON {schema}.runtime_image_acceptances"))
    RUNTIME_IMAGE_ACCEPTANCES_TABLE.drop(bind=connection, checkfirst=False)
    connection.execute(text(f"DROP FUNCTION {schema}.{RUNTIME_IMAGE_GUARD_FUNCTION}()"))
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=PREVIOUS_RUNTIME_IMAGE_MANIFEST_DIGEST)
    )
    return _result(connection, status="ROLLED_BACK", repeat_noop=False)


__all__ = [
    "CanonicalRuntimeImageUpgradeBlocked",
    "PREVIOUS_RUNTIME_IMAGE_MANIFEST_DIGEST",
    "RUNTIME_IMAGE_GUARD_FUNCTION",
    "RUNTIME_IMAGE_GUARD_TRIGGER",
    "RuntimeImageUpgradeResult",
    "apply_runtime_image_upgrade",
    "install_runtime_image_trigger",
    "rollback_runtime_image_upgrade",
    "runtime_image_trigger_statements",
    "verify_runtime_image_upgrade",
]
