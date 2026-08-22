"""Additive deployment-rollover evidence migration over accepted Phase 9."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Final

from sqlalchemy import Connection, func, inspect, select, text

from app.canonical_v13.genesis import (
    postgresql_acl_statements,
    verify_canonical_genesis,
)
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
)
from app.canonical_v13.models import DEPLOYMENTS_TABLE, SCHEMA_METADATA_TABLE
from app.canonical_v13.role_mapping import CanonicalRoleMapping


PREVIOUS_DEPLOYMENT_ROLLOVER_MANIFEST_DIGEST: Final = (
    "8a66b6fec8b93cec236b2d9a36bfa84171bc7905be588275beeea25a09bc5eba"
)
DEPLOYMENT_ROLLOVER_UPGRADE_CONTRACT: Final = (
    "canonical-v13-deployment-rollover-upgrade-v1"
)
DEPLOYMENT_ROLLOVER_GUARD_FUNCTION: Final = "guard_deployments_disable_evidence"
DEPLOYMENT_ROLLOVER_GUARD_TRIGGER: Final = "deployments_disable_evidence_guard"
DEPLOYMENT_ROLLOVER_COLUMNS: Final[tuple[str, ...]] = (
    "disable_reason",
    "disable_receipt_digest",
    "disable_request_digest",
    "disabled_at",
    "disabled_by",
    "superseded_by_qualification_decision_id",
)
DEPLOYMENT_ROLLOVER_CONSTRAINTS: Final[tuple[str, ...]] = (
    "ck_deployments_deployments_disabled_evidence_complete",
    "ck_deployments_disable_receipt_digest_digest_length",
    "ck_deployments_disable_request_digest_digest_length",
    "deployments_disable_receipt_digest_unique",
    "deployments_superseding_qualification_fk",
)
DEPLOYMENT_ROLLOVER_INDEX: Final = (
    "ix_deployments_superseded_by_qualification_decision_id"
)


class CanonicalDeploymentRolloverUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentRolloverUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    columns_present: tuple[str, ...]
    constraints_present: tuple[str, ...]
    index_present: bool
    trigger_present: bool
    disabled_deployment_count: int
    repeat_noop: bool
    receipt_digest: str


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def deployment_rollover_trigger_statements() -> tuple[str, ...]:
    schema = CANONICAL_BUSINESS_SCHEMA
    return (
        f"""CREATE OR REPLACE FUNCTION {schema}.{DEPLOYMENT_ROLLOVER_GUARD_FUNCTION}()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status = 'DISABLED' THEN
            RAISE EXCEPTION 'disabled canonical deployments are immutable';
          END IF;
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.deployment_approval_id IS DISTINCT FROM OLD.deployment_approval_id
             OR NEW.strategy_version_id IS DISTINCT FROM OLD.strategy_version_id
             OR NEW.configuration_bundle_id IS DISTINCT FROM OLD.configuration_bundle_id
             OR NEW.configuration_bundle_digest IS DISTINCT FROM OLD.configuration_bundle_digest
             OR NEW.market_snapshot_id IS DISTINCT FROM OLD.market_snapshot_id
             OR NEW.market_snapshot_digest IS DISTINCT FROM OLD.market_snapshot_digest
             OR NEW.demo_only IS DISTINCT FROM OLD.demo_only
             OR NEW.allow_real_funds IS DISTINCT FROM OLD.allow_real_funds
             OR NEW.capability_digest IS DISTINCT FROM OLD.capability_digest
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'canonical deployment lineage is immutable';
          END IF;
          IF NEW.status = 'DISABLED' THEN
            IF OLD.status <> 'ACTIVE'
               OR NEW.disabled_at IS NULL
               OR NEW.disabled_by IS NULL
               OR NEW.disable_reason IS NULL
               OR NEW.superseded_by_qualification_decision_id IS NULL
               OR NEW.disable_request_digest IS NULL
               OR NEW.disable_receipt_digest IS NULL THEN
              RAISE EXCEPTION 'canonical deployment disable evidence is incomplete';
            END IF;
          ELSIF NEW.disabled_at IS NOT NULL
             OR NEW.disabled_by IS NOT NULL
             OR NEW.disable_reason IS NOT NULL
             OR NEW.superseded_by_qualification_decision_id IS NOT NULL
             OR NEW.disable_request_digest IS NOT NULL
             OR NEW.disable_receipt_digest IS NOT NULL THEN
            RAISE EXCEPTION 'canonical deployment disable evidence requires DISABLED status';
          END IF;
          RETURN NEW;
        END $$""",
        f"DROP TRIGGER IF EXISTS {DEPLOYMENT_ROLLOVER_GUARD_TRIGGER} "
        f"ON {schema}.deployments",
        f"""CREATE TRIGGER {DEPLOYMENT_ROLLOVER_GUARD_TRIGGER} BEFORE UPDATE
        ON {schema}.deployments FOR EACH ROW
        EXECUTE FUNCTION {schema}.{DEPLOYMENT_ROLLOVER_GUARD_FUNCTION}()""",
    )


def install_deployment_rollover_trigger(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    for statement in deployment_rollover_trigger_statements():
        connection.execute(text(statement))


def _manifest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise CanonicalDeploymentRolloverUpgradeBlocked(
            "BLOCKED_DEPLOYMENT_ROLLOVER_SCHEMA_METADATA"
        )
    return value


def _columns(connection: Connection) -> tuple[str, ...]:
    names = {
        str(column["name"])
        for column in inspect(connection).get_columns(
            "deployments", schema=CANONICAL_BUSINESS_SCHEMA
        )
    }
    return tuple(sorted(names.intersection(DEPLOYMENT_ROLLOVER_COLUMNS)))


def _trigger_present(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger trigger "
                "JOIN pg_catalog.pg_class relation ON relation.oid=trigger.tgrelid "
                "JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace "
                "WHERE namespace.nspname=:schema AND relation.relname='deployments' "
                "AND trigger.tgname=:trigger AND NOT trigger.tgisinternal)"
            ),
            {
                "schema": CANONICAL_BUSINESS_SCHEMA,
                "trigger": DEPLOYMENT_ROLLOVER_GUARD_TRIGGER,
            },
        ).scalar_one()
    )


def _constraints(connection: Connection) -> tuple[str, ...]:
    values = connection.execute(
        text(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_schema=:schema AND table_name='deployments' "
            "AND constraint_name = ANY(:names) ORDER BY constraint_name"
        ),
        {
            "schema": CANONICAL_BUSINESS_SCHEMA,
            "names": list(DEPLOYMENT_ROLLOVER_CONSTRAINTS),
        },
    ).scalars()
    return tuple(str(value) for value in values)


def _index_present(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_indexes "
                "WHERE schemaname=:schema AND tablename='deployments' "
                "AND indexname=:index_name)"
            ),
            {
                "schema": CANONICAL_BUSINESS_SCHEMA,
                "index_name": DEPLOYMENT_ROLLOVER_INDEX,
            },
        ).scalar_one()
    )


def _result(
    connection: Connection, *, status: str, repeat_noop: bool
) -> DeploymentRolloverUpgradeResult:
    columns = _columns(connection)
    disabled_count = (
        int(
            connection.execute(
                select(func.count())
                .select_from(DEPLOYMENTS_TABLE)
                .where(DEPLOYMENTS_TABLE.c.status == "DISABLED")
            ).scalar_one()
        )
        if columns == tuple(sorted(DEPLOYMENT_ROLLOVER_COLUMNS))
        else 0
    )
    payload = {
        "contract": DEPLOYMENT_ROLLOVER_UPGRADE_CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "columns_present": columns,
        "constraints_present": _constraints(connection) if columns else (),
        "index_present": _index_present(connection) if columns else False,
        "trigger_present": _trigger_present(connection) if columns else False,
        "disabled_deployment_count": disabled_count,
        "repeat_noop": repeat_noop,
    }
    return DeploymentRolloverUpgradeResult(**payload, receipt_digest=_digest(payload))


def verify_deployment_rollover_upgrade(
    connection: Connection,
) -> DeploymentRolloverUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalDeploymentRolloverUpgradeBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    columns = _columns(connection)
    manifest = _manifest(connection)
    if not columns and manifest == PREVIOUS_DEPLOYMENT_ROLLOVER_MANIFEST_DIGEST:
        return _result(connection, status="PREVIOUS_READY", repeat_noop=True)
    if (
        columns == tuple(sorted(DEPLOYMENT_ROLLOVER_COLUMNS))
        and _constraints(connection) == DEPLOYMENT_ROLLOVER_CONSTRAINTS
        and _index_present(connection)
        and manifest == CANONICAL_MANIFEST_DIGEST
        and _trigger_present(connection)
    ):
        verification = verify_canonical_genesis(connection)
        if verification.accepted:
            return _result(connection, status="ACCEPTED", repeat_noop=True)
    raise CanonicalDeploymentRolloverUpgradeBlocked(
        "BLOCKED_PARTIAL_DEPLOYMENT_ROLLOVER_UPGRADE"
    )


def apply_deployment_rollover_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> DeploymentRolloverUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1_308_202_608_220_724}
    )
    before = verify_deployment_rollover_upgrade(connection)
    if before.status == "ACCEPTED":
        return before
    schema = CANONICAL_BUSINESS_SCHEMA
    connection.execute(
        text(
            f"ALTER TABLE {schema}.deployments "
            "ADD COLUMN disabled_at TIMESTAMP WITH TIME ZONE, "
            "ADD COLUMN disabled_by VARCHAR(160), "
            "ADD COLUMN disable_reason TEXT, "
            "ADD COLUMN superseded_by_qualification_decision_id UUID, "
            "ADD COLUMN disable_request_digest VARCHAR(64), "
            "ADD COLUMN disable_receipt_digest VARCHAR(64), "
            "ADD CONSTRAINT deployments_superseding_qualification_fk "
            f"FOREIGN KEY (superseded_by_qualification_decision_id) REFERENCES {schema}.qualification_decisions(id) ON DELETE RESTRICT, "
            "ADD CONSTRAINT ck_deployments_deployments_disabled_evidence_complete CHECK ("
            "(status = 'DISABLED' AND disabled_at IS NOT NULL AND disabled_by IS NOT NULL "
            "AND disable_reason IS NOT NULL AND superseded_by_qualification_decision_id IS NOT NULL "
            "AND disable_request_digest IS NOT NULL AND disable_receipt_digest IS NOT NULL) OR "
            "(status <> 'DISABLED' AND disabled_at IS NULL AND disabled_by IS NULL "
            "AND disable_reason IS NULL AND superseded_by_qualification_decision_id IS NULL "
            "AND disable_request_digest IS NULL AND disable_receipt_digest IS NULL)), "
            "ADD CONSTRAINT ck_deployments_disable_request_digest_digest_length "
            "CHECK (length(disable_request_digest) = 64), "
            "ADD CONSTRAINT ck_deployments_disable_receipt_digest_digest_length "
            "CHECK (length(disable_receipt_digest) = 64), "
            "ADD CONSTRAINT deployments_disable_receipt_digest_unique UNIQUE (disable_receipt_digest)"
        )
    )
    connection.execute(
        text(
            f"CREATE INDEX {DEPLOYMENT_ROLLOVER_INDEX} "
            f"ON {schema}.deployments (superseded_by_qualification_decision_id)"
        )
    )
    install_deployment_rollover_trigger(connection)
    owner = role_mapping.physical("canonical_schema_owner")
    connection.execute(
        text(
            f"ALTER FUNCTION {schema}.{DEPLOYMENT_ROLLOVER_GUARD_FUNCTION}() "
            f"OWNER TO {owner}"
        )
    )
    deployment_writer_grant = (
        "order_writer_leases TO "
        f"{role_mapping.physical('canonical_deployment_writer')}"
    )
    for statement in postgresql_acl_statements(role_mapping):
        if deployment_writer_grant in statement:
            connection.execute(text(statement))
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=CANONICAL_MANIFEST_DIGEST)
    )
    verify_deployment_rollover_upgrade(connection)
    return _result(connection, status="UPGRADED", repeat_noop=False)


def rollback_deployment_rollover_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> DeploymentRolloverUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1_308_202_608_220_724}
    )
    before = verify_deployment_rollover_upgrade(connection)
    if before.status == "PREVIOUS_READY":
        return before
    if before.disabled_deployment_count:
        raise CanonicalDeploymentRolloverUpgradeBlocked(
            "BLOCKED_DISABLED_DEPLOYMENT_EVIDENCE_NONZERO"
        )
    schema = CANONICAL_BUSINESS_SCHEMA
    connection.execute(
        text(
            f"DROP TRIGGER {DEPLOYMENT_ROLLOVER_GUARD_TRIGGER} ON {schema}.deployments"
        )
    )
    connection.execute(
        text(f"DROP FUNCTION {schema}.{DEPLOYMENT_ROLLOVER_GUARD_FUNCTION}()")
    )
    connection.execute(
        text(
            f"ALTER TABLE {schema}.deployments "
            "DROP CONSTRAINT deployments_disable_receipt_digest_unique, "
            "DROP CONSTRAINT ck_deployments_disable_receipt_digest_digest_length, "
            "DROP CONSTRAINT ck_deployments_disable_request_digest_digest_length, "
            "DROP CONSTRAINT ck_deployments_deployments_disabled_evidence_complete, "
            "DROP CONSTRAINT deployments_superseding_qualification_fk, "
            "DROP COLUMN disable_receipt_digest, DROP COLUMN disable_request_digest, "
            "DROP COLUMN superseded_by_qualification_decision_id, "
            "DROP COLUMN disable_reason, DROP COLUMN disabled_by, DROP COLUMN disabled_at"
        )
    )
    connection.execute(
        text(
            f"REVOKE SELECT ON TABLE {schema}.order_writer_leases FROM "
            f"{role_mapping.physical('canonical_deployment_writer')}"
        )
    )
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=PREVIOUS_DEPLOYMENT_ROLLOVER_MANIFEST_DIGEST)
    )
    return _result(connection, status="ROLLED_BACK", repeat_noop=False)


__all__ = [
    "CanonicalDeploymentRolloverUpgradeBlocked",
    "DEPLOYMENT_ROLLOVER_COLUMNS",
    "DEPLOYMENT_ROLLOVER_CONSTRAINTS",
    "DEPLOYMENT_ROLLOVER_GUARD_FUNCTION",
    "DEPLOYMENT_ROLLOVER_GUARD_TRIGGER",
    "DeploymentRolloverUpgradeResult",
    "PREVIOUS_DEPLOYMENT_ROLLOVER_MANIFEST_DIGEST",
    "apply_deployment_rollover_upgrade",
    "deployment_rollover_trigger_statements",
    "install_deployment_rollover_trigger",
    "rollback_deployment_rollover_upgrade",
    "verify_deployment_rollover_upgrade",
]
