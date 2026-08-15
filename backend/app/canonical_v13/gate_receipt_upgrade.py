"""Additive, one-way upgrade from the accepted 46-table schema to v3 gates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Final
from uuid import uuid4

from sqlalchemy import Connection, inspect, select, text

from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
    READER_IDENTITIES,
    WRITER_IDENTITIES,
)
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    RESEARCH_GATE_ATTEMPTS_TABLE,
    RESEARCH_GATE_RECEIPTS_TABLE,
    SCHEMA_METADATA_TABLE,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


PREVIOUS_MANIFEST_DIGEST: Final = "8668da01999d0f19947d08b2934a05277e1cb998e4abb05a27d6022534f677d6"
PREVIOUS_GATE_MANIFEST_DIGEST: Final = "d5ade09cb4f33241a486ed001295baec81d8428eef39bcd2c7d7bfcede51b081"
UPGRADE_CONTRACT: Final = "canonical-v13-planless-gate-receipts-upgrade-v1"
GATE_GUARD_FUNCTION_NAMES: Final = (
    "guard_research_gate_attempts_lifecycle",
    "guard_research_gate_receipts_append_only",
    "guard_validation_plan_gate_receipts",
)


class CanonicalGateReceiptUpgradeBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class GateReceiptUpgradeResult:
    status: str
    previous_manifest_digest: str
    current_manifest_digest: str
    created_table_count: int
    added_column_count: int
    destructive_operation_count: int
    receipt_digest: str | None


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode()).hexdigest()


def _current_manifest(connection: Connection) -> str:
    return str(connection.execute(select(SCHEMA_METADATA_TABLE.c.manifest_digest)).scalar_one())


def _new_shape(connection: Connection) -> tuple[set[str], set[str]]:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names(schema=CANONICAL_BUSINESS_SCHEMA))
    columns = {
        item["name"]
        for item in inspector.get_columns("validation_plans", schema=CANONICAL_BUSINESS_SCHEMA)
    }
    return tables, columns


def _validation_dependency_grants(
    connection: Connection, validation_role: str
) -> set[tuple[str, str]]:
    return {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            text(
                """
                SELECT table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE table_schema=:schema AND grantee=:validation_role
                  AND table_name IN (
                    'configuration_profiles','configuration_versions',
                    'configuration_dependencies','market_profiles',
                    'market_profile_versions'
                  )
                """
            ),
            {
                "schema": CANONICAL_BUSINESS_SCHEMA,
                "validation_role": validation_role,
            },
        )
    }


def _repair_current_dependency_grants(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    actor_identity: str,
    observed_at: datetime | None,
) -> GateReceiptUpgradeResult:
    validation = role_mapping.physical("canonical_validation_writer")
    expected = {
        ("configuration_profiles", "SELECT"),
        ("configuration_versions", "SELECT"),
        ("configuration_dependencies", "SELECT"),
        ("market_profiles", "SELECT"),
        ("market_profile_versions", "SELECT"),
    }
    observed = _validation_dependency_grants(connection, validation)
    current_manifest = _current_manifest(connection)
    if observed == expected and current_manifest == CANONICAL_MANIFEST_DIGEST:
        return verify_gate_receipt_upgrade(connection)
    if not observed < expected:
        raise CanonicalGateReceiptUpgradeBlocked(
            "BLOCKED_GATE_RECEIPT_DEPENDENCY_ACL_DRIFT",
            "validation dependency grants differ from the reviewed repair state",
        )
    missing_tables = sorted(table for table, _privilege in expected - observed)
    for table_name in missing_tables:
        connection.execute(
            text(
                f"GRANT SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
                f"TO {validation}"
            )
        )
    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    previous_manifest = current_manifest
    payload = {
        "contract": UPGRADE_CONTRACT,
        "repair": "validation_gate_api_dependency_select_grants",
        "previous_manifest_digest": previous_manifest,
        "current_manifest_digest": CANONICAL_MANIFEST_DIGEST,
        "tables": missing_tables,
        "actor_identity": actor_identity,
        "applied_at": now.isoformat(),
    }
    request_digest = _digest({**payload, "applied_at": None})
    receipt_digest = _digest(
        {"request_digest": request_digest, "applied_at": now.isoformat()}
    )
    connection.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=uuid4(),
            event_type="CANONICAL_GATE_RECEIPT_DEPENDENCY_ACL_REPAIRED",
            aggregate_type="canonical_gate_receipt_upgrade",
            aggregate_id=UPGRADE_CONTRACT,
            actor_identity=actor_identity,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            evidence_json=payload,
            created_at=now,
        )
    )
    if previous_manifest == PREVIOUS_GATE_MANIFEST_DIGEST:
        connection.execute(
            SCHEMA_METADATA_TABLE.update().values(
                manifest_digest=CANONICAL_MANIFEST_DIGEST
            )
        )
    if _validation_dependency_grants(connection, validation) != expected:
        raise CanonicalGateReceiptUpgradeBlocked(
            "BLOCKED_GATE_RECEIPT_DEPENDENCY_ACL_REPAIR",
            "validation dependency grants did not converge",
        )
    verified = verify_gate_receipt_upgrade(connection)
    return GateReceiptUpgradeResult(
        verified.status,
        previous_manifest,
        CANONICAL_MANIFEST_DIGEST,
        0,
        0,
        0,
        receipt_digest,
    )


def verify_gate_receipt_upgrade(connection: Connection) -> GateReceiptUpgradeResult:
    manifest = _current_manifest(connection)
    tables, columns = _new_shape(connection)
    required_tables = {"research_gate_attempts", "research_gate_receipts"}
    required_columns = {"static_gate_receipt_id", "lookahead_gate_receipt_id"}
    if manifest != CANONICAL_MANIFEST_DIGEST or not required_tables <= tables or not required_columns <= columns:
        raise CanonicalGateReceiptUpgradeBlocked("BLOCKED_GATE_RECEIPT_UPGRADE_INCOMPLETE", "manifest/table/column evidence differs")
    validation_plan_indexes = {
        item["name"]
        for item in inspect(connection).get_indexes("validation_plans", schema=CANONICAL_BUSINESS_SCHEMA)
    }
    required_indexes = {
        "ix_validation_plans_static_gate_receipt_id",
        "ix_validation_plans_lookahead_gate_receipt_id",
    }
    if not required_indexes <= validation_plan_indexes:
        raise CanonicalGateReceiptUpgradeBlocked("BLOCKED_GATE_RECEIPT_INDEX_DRIFT", "validation receipt lookup indexes are required")
    if connection.dialect.name == "postgresql":
        triggers = int(connection.execute(text("""
            SELECT count(*) FROM pg_catalog.pg_trigger trigger
            JOIN pg_catalog.pg_class relation ON relation.oid=trigger.tgrelid
            JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace
            WHERE namespace.nspname=:schema AND NOT trigger.tgisinternal
              AND trigger.tgname IN ('research_gate_receipts_append_only','research_gate_attempts_lifecycle','validation_plans_gate_receipts')
        """), {"schema": CANONICAL_BUSINESS_SCHEMA}).scalar_one())
        if triggers != 3:
            raise CanonicalGateReceiptUpgradeBlocked("BLOCKED_GATE_RECEIPT_TRIGGER_DRIFT", "exact immutability/lifecycle triggers are required")
        function_facts = connection.execute(text("""
            SELECT count(*) AS function_count,
                   count(*) FILTER (WHERE procedure.proowner=namespace.nspowner) AS schema_owner_count
            FROM pg_catalog.pg_proc procedure
            JOIN pg_catalog.pg_namespace namespace ON namespace.oid=procedure.pronamespace
            WHERE namespace.nspname=:schema AND procedure.proname = ANY(:function_names)
        """), {"schema": CANONICAL_BUSINESS_SCHEMA, "function_names": list(GATE_GUARD_FUNCTION_NAMES)}).one()
        if tuple(int(value) for value in function_facts) != (3, 3):
            raise CanonicalGateReceiptUpgradeBlocked("BLOCKED_GATE_RECEIPT_FUNCTION_OWNER_DRIFT", "guard functions require the schema owner")
        function_acl = connection.execute(text("""
            SELECT count(*) AS grant_count,
                   count(*) FILTER (
                     WHERE acl.grantee=procedure.proowner AND acl.privilege_type='EXECUTE'
                   ) AS owner_execute_count
            FROM pg_catalog.pg_proc procedure
            JOIN pg_catalog.pg_namespace namespace ON namespace.oid=procedure.pronamespace
            CROSS JOIN LATERAL aclexplode(
              COALESCE(procedure.proacl, acldefault('f', procedure.proowner))
            ) acl
            WHERE namespace.nspname=:schema AND procedure.proname = ANY(:function_names)
        """), {"schema": CANONICAL_BUSINESS_SCHEMA, "function_names": list(GATE_GUARD_FUNCTION_NAMES)}).one()
        if tuple(int(value) for value in function_acl) != (3, 3):
            raise CanonicalGateReceiptUpgradeBlocked("BLOCKED_GATE_RECEIPT_FUNCTION_ACL_DRIFT", "guard function execution is owner-only")
    return GateReceiptUpgradeResult("ACCEPTED", PREVIOUS_MANIFEST_DIGEST, CANONICAL_MANIFEST_DIGEST, 2, 2, 0, None)


def gate_receipt_trigger_statements() -> tuple[str, ...]:
    schema = CANONICAL_BUSINESS_SCHEMA
    return (
        f"""
        CREATE OR REPLACE FUNCTION {schema}.guard_research_gate_receipts_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent {schema}.research_gate_attempts%%ROWTYPE;
        BEGIN
          IF TG_OP IN ('UPDATE','DELETE') THEN
            RAISE EXCEPTION 'canonical gate receipts are append-only';
          END IF;
          SELECT * INTO parent FROM {schema}.research_gate_attempts
            WHERE id=NEW.gate_attempt_id FOR UPDATE;
          IF NOT FOUND OR parent.status <> 'RUNNING' THEN
            RAISE EXCEPTION 'gate receipt requires one running parent attempt';
          END IF;
          IF NEW.strategy_version_id IS DISTINCT FROM parent.strategy_version_id
             OR NEW.research_target_id IS DISTINCT FROM parent.research_target_id
             OR NEW.artifact_digest IS DISTINCT FROM parent.artifact_digest
             OR NEW.release_commit IS DISTINCT FROM parent.release_commit
             OR NEW.executor_image_digest IS DISTINCT FROM parent.executor_image_digest
             OR NEW.worker_source_digest IS DISTINCT FROM parent.worker_source_digest
             OR NEW.target_snapshot_id IS DISTINCT FROM parent.target_snapshot_id
             OR NEW.target_snapshot_digest IS DISTINCT FROM parent.target_snapshot_digest
             OR NEW.window_snapshot_id IS DISTINCT FROM parent.window_snapshot_id
             OR NEW.window_snapshot_digest IS DISTINCT FROM parent.window_snapshot_digest
             OR NEW.market_profile_version_id IS DISTINCT FROM parent.market_profile_version_id
             OR NEW.market_profile_digest IS DISTINCT FROM parent.market_profile_digest
             OR NEW.configuration_bundle_id IS DISTINCT FROM parent.configuration_bundle_id
             OR NEW.configuration_bundle_digest IS DISTINCT FROM parent.configuration_bundle_digest
             OR NEW.market_snapshot_id IS DISTINCT FROM parent.market_snapshot_id
             OR NEW.market_snapshot_digest IS DISTINCT FROM parent.market_snapshot_digest THEN
            RAISE EXCEPTION 'gate receipt lineage differs from its parent attempt';
          END IF;
          IF (NEW.gate_type='STATIC' AND NEW.gate_contract_version <> 'canonical-v13-static-gate-receipt-v3')
             OR (NEW.gate_type='LOOKAHEAD' AND NEW.gate_contract_version <> 'canonical-v13-lookahead-gate-receipt-v3') THEN
            RAISE EXCEPTION 'gate receipt type contract drifted';
          END IF;
          IF NEW.gate_type='LOOKAHEAD' AND NOT EXISTS (
            SELECT 1 FROM {schema}.research_gate_receipts receipt
            WHERE receipt.gate_attempt_id=NEW.gate_attempt_id
              AND receipt.gate_type='STATIC' AND receipt.terminal_status='PASSED'
          ) THEN
            RAISE EXCEPTION 'lookahead receipt requires persisted static pass';
          END IF;
          RETURN NEW;
        END $$
        """,
        f"DROP TRIGGER IF EXISTS research_gate_receipts_append_only ON {schema}.research_gate_receipts",
        f"""CREATE TRIGGER research_gate_receipts_append_only BEFORE INSERT OR UPDATE OR DELETE
          ON {schema}.research_gate_receipts FOR EACH ROW EXECUTE FUNCTION {schema}.guard_research_gate_receipts_append_only()""",
        f"""
        CREATE OR REPLACE FUNCTION {schema}.guard_research_gate_attempts_lifecycle()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
          IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'PENDING'
               OR NEW.gate_contract_version <> 'canonical-v13-planless-gate-attempt-v3'
               OR NEW.writer_identity <> 'canonical_validation_writer'
               OR NEW.terminal_reason_code IS NOT NULL
               OR NEW.lease_token_digest IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
               OR NEW.started_at IS NOT NULL OR NEW.completed_at IS NOT NULL THEN
              RAISE EXCEPTION 'new gate attempt must be pristine pending v3 evidence';
            END IF;
            RETURN NEW;
          END IF;
          IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'canonical gate attempts cannot be deleted'; END IF;
          IF OLD.status IN ('PASSED','FAILED','BLOCKED') THEN RAISE EXCEPTION 'terminal gate attempts are immutable'; END IF;
          IF NOT (OLD.status = NEW.status OR OLD.status='PENDING' AND NEW.status='RUNNING' OR OLD.status='RUNNING' AND NEW.status IN ('PASSED','FAILED','BLOCKED')) THEN
            RAISE EXCEPTION 'illegal gate attempt transition';
          END IF;
          IF OLD.id IS DISTINCT FROM NEW.id OR OLD.strategy_version_id IS DISTINCT FROM NEW.strategy_version_id OR OLD.research_target_id IS DISTINCT FROM NEW.research_target_id
             OR OLD.artifact_digest IS DISTINCT FROM NEW.artifact_digest OR OLD.gate_contract_version IS DISTINCT FROM NEW.gate_contract_version
             OR OLD.release_commit IS DISTINCT FROM NEW.release_commit OR OLD.executor_image_digest IS DISTINCT FROM NEW.executor_image_digest
             OR OLD.worker_source_digest IS DISTINCT FROM NEW.worker_source_digest OR OLD.target_snapshot_id IS DISTINCT FROM NEW.target_snapshot_id
             OR OLD.target_snapshot_digest IS DISTINCT FROM NEW.target_snapshot_digest OR OLD.window_snapshot_id IS DISTINCT FROM NEW.window_snapshot_id
             OR OLD.window_snapshot_digest IS DISTINCT FROM NEW.window_snapshot_digest OR OLD.market_profile_version_id IS DISTINCT FROM NEW.market_profile_version_id
             OR OLD.market_profile_digest IS DISTINCT FROM NEW.market_profile_digest OR OLD.configuration_bundle_id IS DISTINCT FROM NEW.configuration_bundle_id
             OR OLD.configuration_bundle_digest IS DISTINCT FROM NEW.configuration_bundle_digest OR OLD.market_snapshot_id IS DISTINCT FROM NEW.market_snapshot_id
             OR OLD.market_snapshot_digest IS DISTINCT FROM NEW.market_snapshot_digest OR OLD.request_digest IS DISTINCT FROM NEW.request_digest
             OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key
             OR OLD.writer_identity IS DISTINCT FROM NEW.writer_identity OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
             RAISE EXCEPTION 'gate attempt lineage is immutable';
          END IF;
          IF OLD.status = 'PENDING' AND NEW.status = 'PENDING' AND NEW.started_at IS NOT NULL THEN
            RAISE EXCEPTION 'pending gate attempt cannot have started_at';
          END IF;
          IF OLD.status = 'RUNNING' AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
            RAISE EXCEPTION 'gate attempt started_at is immutable after claim';
          END IF;
          IF NEW.status IN ('PENDING','RUNNING') AND (NEW.terminal_reason_code IS NOT NULL OR NEW.completed_at IS NOT NULL) THEN
            RAISE EXCEPTION 'non-terminal gate attempt cannot contain terminal evidence';
          END IF;
          IF NEW.status IN ('PASSED','FAILED','BLOCKED') AND NEW.completed_at IS NULL THEN
            RAISE EXCEPTION 'terminal gate attempt requires completed_at';
          END IF;
          IF NEW.status = 'PENDING' AND (NEW.lease_token_digest IS NOT NULL OR NEW.lease_expires_at IS NOT NULL OR NEW.started_at IS NOT NULL) THEN
            RAISE EXCEPTION 'pending gate attempt cannot contain lease evidence';
          END IF;
          IF NEW.status = 'RUNNING' AND (NEW.lease_token_digest IS NULL OR NEW.lease_expires_at IS NULL OR NEW.started_at IS NULL) THEN
            RAISE EXCEPTION 'running gate attempt requires lease evidence';
          END IF;
          IF NEW.status IN ('PASSED','FAILED','BLOCKED') AND (NEW.lease_token_digest IS NOT NULL OR NEW.lease_expires_at IS NOT NULL) THEN
            RAISE EXCEPTION 'terminal gate attempt cannot retain a lease';
          END IF;
          IF NEW.status = 'PASSED' AND NEW.terminal_reason_code IS NOT NULL THEN
            RAISE EXCEPTION 'passed gate attempt cannot contain a terminal reason';
          END IF;
          IF NEW.status IN ('FAILED','BLOCKED') AND NEW.terminal_reason_code IS NULL THEN
            RAISE EXCEPTION 'non-passing terminal gate attempt requires a reason';
          END IF;
          RETURN NEW;
        END $$
        """,
        f"DROP TRIGGER IF EXISTS research_gate_attempts_lifecycle ON {schema}.research_gate_attempts",
        f"""CREATE TRIGGER research_gate_attempts_lifecycle BEFORE INSERT OR UPDATE OR DELETE
          ON {schema}.research_gate_attempts FOR EACH ROW EXECUTE FUNCTION {schema}.guard_research_gate_attempts_lifecycle()""",
        f"""
        CREATE OR REPLACE FUNCTION {schema}.guard_validation_plan_gate_receipts()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
          IF NEW.static_gate_receipt_id IS NULL OR NEW.lookahead_gate_receipt_id IS NULL THEN
            RAISE EXCEPTION 'validation plans require persisted v3 gate receipts';
          END IF;
          IF NOT EXISTS (
            SELECT 1
            FROM {schema}.research_gate_receipts static_receipt
            JOIN {schema}.research_gate_receipts lookahead_receipt
              ON lookahead_receipt.gate_attempt_id=static_receipt.gate_attempt_id
            JOIN {schema}.research_gate_attempts attempt
              ON attempt.id=static_receipt.gate_attempt_id
            WHERE static_receipt.id=NEW.static_gate_receipt_id
              AND lookahead_receipt.id=NEW.lookahead_gate_receipt_id
              AND static_receipt.gate_type='STATIC'
              AND lookahead_receipt.gate_type='LOOKAHEAD'
              AND static_receipt.terminal_status='PASSED'
              AND lookahead_receipt.terminal_status='PASSED'
              AND attempt.status='PASSED'
              AND NEW.strategy_version_id=attempt.strategy_version_id
              AND NEW.research_target_id=attempt.research_target_id
              AND NEW.configuration_bundle_id=attempt.configuration_bundle_id
              AND NEW.configuration_bundle_digest=attempt.configuration_bundle_digest
              AND NEW.market_snapshot_id=attempt.market_snapshot_id
              AND NEW.market_snapshot_digest=attempt.market_snapshot_digest
              AND NEW.window_snapshot_id=attempt.window_snapshot_id
          ) THEN
            RAISE EXCEPTION 'validation plan gate receipt lineage is not eligible';
          END IF;
          RETURN NEW;
        END $$
        """,
        f"DROP TRIGGER IF EXISTS validation_plans_gate_receipts ON {schema}.validation_plans",
        f"""CREATE TRIGGER validation_plans_gate_receipts BEFORE INSERT OR UPDATE
          ON {schema}.validation_plans FOR EACH ROW EXECUTE FUNCTION {schema}.guard_validation_plan_gate_receipts()""",
    )


def install_gate_receipt_triggers(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    # Execute one DDL command at a time: psycopg's extended protocol rejects
    # prepared statements that contain multiple commands.
    for statement in gate_receipt_trigger_statements():
        connection.exec_driver_sql(statement)


def apply_gate_receipt_upgrade(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    actor_identity: str,
    observed_at: datetime | None = None,
) -> GateReceiptUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalGateReceiptUpgradeBlocked("BLOCKED_POSTGRESQL_REQUIRED", "production upgrade requires PostgreSQL")
    if not actor_identity or actor_identity.strip() != actor_identity or len(actor_identity) > 160:
        raise CanonicalGateReceiptUpgradeBlocked("BLOCKED_GATE_RECEIPT_UPGRADE_ACTOR", "actor identity is invalid")
    current = _current_manifest(connection)
    if current in {CANONICAL_MANIFEST_DIGEST, PREVIOUS_GATE_MANIFEST_DIGEST}:
        return _repair_current_dependency_grants(
            connection,
            role_mapping=role_mapping,
            actor_identity=actor_identity,
            observed_at=observed_at,
        )
    if current != PREVIOUS_MANIFEST_DIGEST:
        raise CanonicalGateReceiptUpgradeBlocked("BLOCKED_GATE_RECEIPT_UPGRADE_BASE", "installed manifest is not the accepted predecessor")
    tables, columns = _new_shape(connection)
    if {"research_gate_attempts", "research_gate_receipts"} & tables or {"static_gate_receipt_id", "lookahead_gate_receipt_id"} & columns:
        raise CanonicalGateReceiptUpgradeBlocked("BLOCKED_GATE_RECEIPT_PARTIAL_UPGRADE", "partial schema mutation requires operator recovery")

    RESEARCH_GATE_ATTEMPTS_TABLE.create(connection, checkfirst=False)
    RESEARCH_GATE_RECEIPTS_TABLE.create(connection, checkfirst=False)
    schema = CANONICAL_BUSINESS_SCHEMA
    connection.execute(text(f"ALTER TABLE {schema}.validation_plans ADD COLUMN static_gate_receipt_id uuid REFERENCES {schema}.research_gate_receipts(id) ON DELETE RESTRICT"))
    connection.execute(text(f"ALTER TABLE {schema}.validation_plans ADD COLUMN lookahead_gate_receipt_id uuid REFERENCES {schema}.research_gate_receipts(id) ON DELETE RESTRICT"))
    connection.execute(text(f"CREATE INDEX ix_validation_plans_static_gate_receipt_id ON {schema}.validation_plans (static_gate_receipt_id)"))
    connection.execute(text(f"CREATE INDEX ix_validation_plans_lookahead_gate_receipt_id ON {schema}.validation_plans (lookahead_gate_receipt_id)"))
    owner = role_mapping.physical("canonical_schema_owner")
    validation = role_mapping.physical("canonical_validation_writer")
    api_reader = role_mapping.physical("canonical_api_reader")
    research_reader = role_mapping.physical("canonical_research_reader")
    scoring = role_mapping.physical("canonical_scoring_writer")
    for table_name in ("research_gate_attempts", "research_gate_receipts"):
        connection.execute(text(f"ALTER TABLE {schema}.{table_name} OWNER TO {owner}"))
        connection.execute(text(f"REVOKE ALL ON TABLE {schema}.{table_name} FROM PUBLIC"))
        connection.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {schema}.{table_name} TO {owner}"))
        connection.execute(text(f"GRANT SELECT ON TABLE {schema}.{table_name} TO {api_reader}, {research_reader}, {scoring}"))
    connection.execute(text(f"GRANT SELECT, INSERT, UPDATE ON TABLE {schema}.research_gate_attempts TO {validation}"))
    connection.execute(text(f"GRANT SELECT, INSERT ON TABLE {schema}.research_gate_receipts TO {validation}"))
    connection.execute(text(
        f"GRANT SELECT ON TABLE {schema}.configuration_profiles, "
        f"{schema}.configuration_versions, {schema}.configuration_dependencies, "
        f"{schema}.market_profiles, {schema}.market_profile_versions TO {validation}"
    ))
    install_gate_receipt_triggers(connection)
    guard_roles = tuple(
        role_mapping.physical(role)
        for role in (*WRITER_IDENTITIES, *READER_IDENTITIES)
    )
    for function_name in GATE_GUARD_FUNCTION_NAMES:
        qualified = f"{schema}.{function_name}()"
        connection.execute(text(f"REVOKE ALL PRIVILEGES ON FUNCTION {qualified} FROM PUBLIC"))
        for role in guard_roles:
            connection.execute(text(f"REVOKE ALL PRIVILEGES ON FUNCTION {qualified} FROM {role}"))
        connection.execute(text(f"GRANT EXECUTE ON FUNCTION {qualified} TO {owner}"))
        connection.execute(text(f"ALTER FUNCTION {qualified} OWNER TO {owner}"))
    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = {
        "contract": UPGRADE_CONTRACT,
        "previous_manifest_digest": PREVIOUS_MANIFEST_DIGEST,
        "current_manifest_digest": CANONICAL_MANIFEST_DIGEST,
        "created_tables": ["research_gate_attempts", "research_gate_receipts"],
        "added_columns": ["validation_plans.static_gate_receipt_id", "validation_plans.lookahead_gate_receipt_id"],
        "destructive_operations": [],
        "actor_identity": actor_identity,
        "applied_at": now.isoformat(),
    }
    request_digest = _digest({**payload, "applied_at": None})
    receipt_digest = _digest({"request_digest": request_digest, "applied_at": now.isoformat()})
    connection.execute(AUDIT_EVENTS_TABLE.insert().values(
        id=uuid4(), event_type="CANONICAL_GATE_RECEIPT_SCHEMA_UPGRADED", aggregate_type="canonical_gate_receipt_upgrade",
        aggregate_id=UPGRADE_CONTRACT, actor_identity=actor_identity, request_digest=request_digest,
        receipt_digest=receipt_digest, evidence_json=payload, created_at=now,
    ))
    connection.execute(SCHEMA_METADATA_TABLE.update().values(manifest_digest=CANONICAL_MANIFEST_DIGEST))
    verified = verify_gate_receipt_upgrade(connection)
    return GateReceiptUpgradeResult(verified.status, PREVIOUS_MANIFEST_DIGEST, CANONICAL_MANIFEST_DIGEST, 2, 2, 0, receipt_digest)


__all__ = ["CanonicalGateReceiptUpgradeBlocked", "GATE_GUARD_FUNCTION_NAMES", "GateReceiptUpgradeResult", "apply_gate_receipt_upgrade", "gate_receipt_trigger_statements", "install_gate_receipt_triggers", "verify_gate_receipt_upgrade"]
