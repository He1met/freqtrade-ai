"""Additive acceptance-signal trigger schema and ACL upgrade."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Final

from sqlalchemy import Connection, func, inspect, select, text

from app.canonical_v13.genesis import postgresql_acl_statements, verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA, CANONICAL_MANIFEST_DIGEST
from app.canonical_v13.models import (
    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE,
    SCHEMA_METADATA_TABLE,
    SIGNALS_TABLE,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


PREVIOUS_ACCEPTANCE_TRIGGER_MANIFEST_DIGEST: Final = (
    "1363b302a9e52ea20543d041d1e7ada9ac4637f777862ffb5c70270637ae806e"
)
ACCEPTANCE_TRIGGER_UPGRADE_CONTRACT: Final = (
    "canonical-v13-acceptance-signal-trigger-upgrade-v1"
)
ACCEPTANCE_TRIGGER_GUARD_FUNCTION: Final = "guard_acceptance_signal_triggers_immutable"
ACCEPTANCE_TRIGGER_GUARD_TRIGGER: Final = "acceptance_signal_triggers_immutable"
ACCEPTANCE_SIGNAL_GUARD_FUNCTION: Final = "guard_acceptance_signals_immutable"
ACCEPTANCE_SIGNAL_GUARD_TRIGGER: Final = "acceptance_signals_immutable"
SIGNAL_COLUMNS: Final[tuple[str, ...]] = (
    "acceptance_trigger_id",
    "source_kind",
    "worker_receipt_digest",
    "worker_signature",
    "worker_signature_algorithm",
    "worker_signer_key_id",
)
SIGNAL_CONSTRAINTS: Final[tuple[str, ...]] = (
    "ck_signals_signals_source_kind_lineage",
    "ck_signals_worker_receipt_digest_digest_length",
    "ck_signals_worker_signature_digest_length",
    "fk_signals_acceptance_trigger_id_acceptance_signal_triggers",
    "uq_signals_acceptance_trigger_id",
)
ACCEPTANCE_SIGNAL_WRITER_READ_DELTA: Final[tuple[str, ...]] = (
    "deployment_approvals",
    "qualification_decisions",
    "research_targets",
    "runtime_image_acceptances",
)


class CanonicalAcceptanceSignalTriggerUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class AcceptanceSignalTriggerUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    trigger_table_present: bool
    immutability_trigger_present: bool
    signal_columns_present: tuple[str, ...]
    signal_constraints_present: tuple[str, ...]
    trigger_count: int
    acceptance_signal_count: int
    repeat_noop: bool
    receipt_digest: str


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def acceptance_signal_trigger_guard_statements() -> tuple[str, ...]:
    schema = CANONICAL_BUSINESS_SCHEMA
    return (
        f"""CREATE OR REPLACE FUNCTION {schema}.{ACCEPTANCE_TRIGGER_GUARD_FUNCTION}()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'canonical acceptance signal triggers are immutable';
        END $$""",
        f"DROP TRIGGER IF EXISTS {ACCEPTANCE_TRIGGER_GUARD_TRIGGER} "
        f"ON {schema}.acceptance_signal_triggers",
        f"""CREATE TRIGGER {ACCEPTANCE_TRIGGER_GUARD_TRIGGER}
        BEFORE UPDATE OR DELETE ON {schema}.acceptance_signal_triggers
        FOR EACH ROW EXECUTE FUNCTION
        {schema}.{ACCEPTANCE_TRIGGER_GUARD_FUNCTION}()""",
        f"""CREATE OR REPLACE FUNCTION {schema}.{ACCEPTANCE_SIGNAL_GUARD_FUNCTION}()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.source_kind = 'ACCEPTANCE_SCHEDULED_TEST'
             OR (TG_OP = 'UPDATE' AND NEW.source_kind = 'ACCEPTANCE_SCHEDULED_TEST')
          THEN
            RAISE EXCEPTION 'canonical acceptance signals are immutable';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$""",
        f"DROP TRIGGER IF EXISTS {ACCEPTANCE_SIGNAL_GUARD_TRIGGER} "
        f"ON {schema}.signals",
        f"""CREATE TRIGGER {ACCEPTANCE_SIGNAL_GUARD_TRIGGER}
        BEFORE UPDATE OR DELETE ON {schema}.signals
        FOR EACH ROW EXECUTE FUNCTION
        {schema}.{ACCEPTANCE_SIGNAL_GUARD_FUNCTION}()""",
    )


def install_acceptance_signal_trigger_guard(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    for statement in acceptance_signal_trigger_guard_statements():
        connection.execute(text(statement))


def _manifest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise CanonicalAcceptanceSignalTriggerUpgradeBlocked(
            "BLOCKED_ACCEPTANCE_TRIGGER_SCHEMA_METADATA"
        )
    return value


def _table_present(connection: Connection) -> bool:
    return "acceptance_signal_triggers" in inspect(connection).get_table_names(
        schema=CANONICAL_BUSINESS_SCHEMA
    )


def _signal_columns(connection: Connection) -> tuple[str, ...]:
    names = {
        str(column["name"])
        for column in inspect(connection).get_columns(
            "signals", schema=CANONICAL_BUSINESS_SCHEMA
        )
    }
    return tuple(sorted(names.intersection(SIGNAL_COLUMNS)))


def _signal_constraints(connection: Connection) -> tuple[str, ...]:
    names = set()
    inspector = inspect(connection)
    for item in inspector.get_foreign_keys("signals", schema=CANONICAL_BUSINESS_SCHEMA):
        if item.get("name"):
            names.add(str(item["name"]))
    for item in inspector.get_unique_constraints(
        "signals", schema=CANONICAL_BUSINESS_SCHEMA
    ):
        if item.get("name"):
            names.add(str(item["name"]))
    for item in inspector.get_check_constraints(
        "signals", schema=CANONICAL_BUSINESS_SCHEMA
    ):
        if item.get("name"):
            names.add(str(item["name"]))
    return tuple(sorted(names.intersection(SIGNAL_CONSTRAINTS)))


def _immutability_trigger_present(connection: Connection) -> bool:
    observed = set(
        connection.execute(
            text(
                "SELECT relation.relname, trigger.tgname FROM pg_catalog.pg_trigger trigger "
                "JOIN pg_catalog.pg_class relation ON relation.oid=trigger.tgrelid "
                "JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace "
                "WHERE namespace.nspname=:schema "
                "AND trigger.tgenabled='O' AND NOT trigger.tgisinternal "
                "AND trigger.tgname = ANY(:triggers)"
            ),
            {
                "schema": CANONICAL_BUSINESS_SCHEMA,
                "triggers": [
                    ACCEPTANCE_TRIGGER_GUARD_TRIGGER,
                    ACCEPTANCE_SIGNAL_GUARD_TRIGGER,
                ],
            },
        ).all()
    )
    return observed == {
        ("acceptance_signal_triggers", ACCEPTANCE_TRIGGER_GUARD_TRIGGER),
        ("signals", ACCEPTANCE_SIGNAL_GUARD_TRIGGER),
    }


def _result(
    connection: Connection, *, status: str, repeat_noop: bool
) -> AcceptanceSignalTriggerUpgradeResult:
    table_present = _table_present(connection)
    columns = _signal_columns(connection)
    trigger_count = (
        int(
            connection.execute(
                select(func.count()).select_from(ACCEPTANCE_SIGNAL_TRIGGERS_TABLE)
            ).scalar_one()
        )
        if table_present
        else 0
    )
    acceptance_count = (
        int(
            connection.execute(
                select(func.count())
                .select_from(SIGNALS_TABLE)
                .where(SIGNALS_TABLE.c.source_kind == "ACCEPTANCE_SCHEDULED_TEST")
            ).scalar_one()
        )
        if columns == tuple(sorted(SIGNAL_COLUMNS))
        else 0
    )
    payload = {
        "contract": ACCEPTANCE_TRIGGER_UPGRADE_CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "trigger_table_present": table_present,
        "immutability_trigger_present": (
            _immutability_trigger_present(connection) if table_present else False
        ),
        "signal_columns_present": columns,
        "signal_constraints_present": (
            _signal_constraints(connection) if columns else ()
        ),
        "trigger_count": trigger_count,
        "acceptance_signal_count": acceptance_count,
        "repeat_noop": repeat_noop,
    }
    return AcceptanceSignalTriggerUpgradeResult(
        **payload, receipt_digest=_digest(payload)
    )


def verify_acceptance_signal_trigger_upgrade(
    connection: Connection,
) -> AcceptanceSignalTriggerUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalAcceptanceSignalTriggerUpgradeBlocked(
            "BLOCKED_POSTGRESQL_REQUIRED"
        )
    table_present = _table_present(connection)
    columns = _signal_columns(connection)
    manifest = _manifest(connection)
    if (
        not table_present
        and not columns
        and manifest == PREVIOUS_ACCEPTANCE_TRIGGER_MANIFEST_DIGEST
    ):
        return _result(connection, status="PREVIOUS_READY", repeat_noop=True)
    if (
        table_present
        and _immutability_trigger_present(connection)
        and columns == tuple(sorted(SIGNAL_COLUMNS))
        and _signal_constraints(connection) == tuple(sorted(SIGNAL_CONSTRAINTS))
        and manifest == CANONICAL_MANIFEST_DIGEST
        and verify_canonical_genesis(connection).accepted
    ):
        return _result(connection, status="ACCEPTED", repeat_noop=True)
    raise CanonicalAcceptanceSignalTriggerUpgradeBlocked(
        "BLOCKED_PARTIAL_ACCEPTANCE_TRIGGER_UPGRADE"
    )


def apply_acceptance_signal_trigger_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> AcceptanceSignalTriggerUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1_308_202_608_230_819}
    )
    before = verify_acceptance_signal_trigger_upgrade(connection)
    if before.status == "ACCEPTED":
        return before
    schema = CANONICAL_BUSINESS_SCHEMA
    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.create(connection, checkfirst=False)
    install_acceptance_signal_trigger_guard(connection)
    connection.execute(
        text(
            f"ALTER TABLE {schema}.signals "
            "ADD COLUMN source_kind VARCHAR(40) NOT NULL DEFAULT 'NATURAL_STRATEGY_SIGNAL', "
            "ADD COLUMN acceptance_trigger_id UUID, "
            "ADD COLUMN worker_receipt_digest VARCHAR(64), "
            "ADD COLUMN worker_signer_key_id VARCHAR(160), "
            "ADD COLUMN worker_signature_algorithm VARCHAR(40), "
            "ADD COLUMN worker_signature VARCHAR(64), "
            "ADD CONSTRAINT fk_signals_acceptance_trigger_id_acceptance_signal_triggers FOREIGN KEY "
            f"(acceptance_trigger_id) REFERENCES {schema}.acceptance_signal_triggers(id) ON DELETE RESTRICT, "
            "ADD CONSTRAINT uq_signals_acceptance_trigger_id UNIQUE (acceptance_trigger_id), "
            "ADD CONSTRAINT ck_signals_worker_receipt_digest_digest_length "
            "CHECK (length(worker_receipt_digest) = 64), "
            "ADD CONSTRAINT ck_signals_worker_signature_digest_length "
            "CHECK (length(worker_signature) = 64), "
            "ADD CONSTRAINT ck_signals_signals_source_kind_lineage CHECK ("
            "(source_kind IN ('NATURAL_STRATEGY_SIGNAL', 'TEST_SIMULATED_FIXTURE') "
            "AND acceptance_trigger_id IS NULL AND worker_receipt_digest IS NULL "
            "AND worker_signer_key_id IS NULL AND worker_signature_algorithm IS NULL "
            "AND worker_signature IS NULL) OR "
            "(source_kind = 'ACCEPTANCE_SCHEDULED_TEST' AND acceptance_trigger_id IS NOT NULL "
            "AND worker_receipt_digest IS NOT NULL AND worker_signer_key_id IS NOT NULL "
            "AND worker_signature_algorithm IS NOT NULL AND worker_signature IS NOT NULL))"
        )
    )
    connection.execute(
        text(
            f"ALTER TABLE {schema}.signals ALTER COLUMN source_kind DROP DEFAULT"
        )
    )
    owner = role_mapping.physical("canonical_schema_owner")
    connection.execute(
        text(
            f"ALTER TABLE {schema}.acceptance_signal_triggers OWNER TO {owner}"
        )
    )
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=CANONICAL_MANIFEST_DIGEST)
    )
    for statement in postgresql_acl_statements(role_mapping):
        connection.execute(text(statement))
    verify_acceptance_signal_trigger_upgrade(connection)
    return _result(connection, status="UPGRADED", repeat_noop=False)


def rollback_acceptance_signal_trigger_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> AcceptanceSignalTriggerUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1_308_202_608_230_819}
    )
    before = verify_acceptance_signal_trigger_upgrade(connection)
    if before.status == "PREVIOUS_READY":
        return before
    if before.trigger_count or before.acceptance_signal_count:
        raise CanonicalAcceptanceSignalTriggerUpgradeBlocked(
            "BLOCKED_ACCEPTANCE_TRIGGER_ROLLBACK_EVIDENCE_NONZERO"
        )
    schema = CANONICAL_BUSINESS_SCHEMA
    signal_writer = role_mapping.physical("canonical_signal_writer")
    connection.execute(
        text(
            f"DROP TRIGGER {ACCEPTANCE_SIGNAL_GUARD_TRIGGER} ON {schema}.signals"
        )
    )
    connection.execute(
        text(
            f"ALTER TABLE {schema}.signals "
            "DROP CONSTRAINT ck_signals_signals_source_kind_lineage, "
            "DROP CONSTRAINT ck_signals_worker_signature_digest_length, "
            "DROP CONSTRAINT ck_signals_worker_receipt_digest_digest_length, "
            "DROP CONSTRAINT uq_signals_acceptance_trigger_id, "
            "DROP CONSTRAINT fk_signals_acceptance_trigger_id_acceptance_signal_triggers, "
            "DROP COLUMN worker_signature, DROP COLUMN worker_signature_algorithm, "
            "DROP COLUMN worker_signer_key_id, DROP COLUMN worker_receipt_digest, "
            "DROP COLUMN acceptance_trigger_id, DROP COLUMN source_kind"
        )
    )
    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.drop(connection, checkfirst=False)
    for table_name in ACCEPTANCE_SIGNAL_WRITER_READ_DELTA:
        connection.execute(
            text(
                f"REVOKE SELECT ON TABLE {schema}.{table_name} FROM {signal_writer}"
            )
        )
    connection.execute(
        text(
            f"DROP FUNCTION {schema}.{ACCEPTANCE_TRIGGER_GUARD_FUNCTION}()"
        )
    )
    connection.execute(
        text(
            f"DROP FUNCTION {schema}.{ACCEPTANCE_SIGNAL_GUARD_FUNCTION}()"
        )
    )
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=PREVIOUS_ACCEPTANCE_TRIGGER_MANIFEST_DIGEST)
    )
    return _result(connection, status="ROLLED_BACK", repeat_noop=False)


__all__ = [
    "ACCEPTANCE_SIGNAL_WRITER_READ_DELTA",
    "ACCEPTANCE_TRIGGER_UPGRADE_CONTRACT",
    "ACCEPTANCE_TRIGGER_GUARD_FUNCTION",
    "ACCEPTANCE_TRIGGER_GUARD_TRIGGER",
    "ACCEPTANCE_SIGNAL_GUARD_FUNCTION",
    "ACCEPTANCE_SIGNAL_GUARD_TRIGGER",
    "AcceptanceSignalTriggerUpgradeResult",
    "CanonicalAcceptanceSignalTriggerUpgradeBlocked",
    "PREVIOUS_ACCEPTANCE_TRIGGER_MANIFEST_DIGEST",
    "acceptance_signal_trigger_guard_statements",
    "apply_acceptance_signal_trigger_upgrade",
    "install_acceptance_signal_trigger_guard",
    "rollback_acceptance_signal_trigger_upgrade",
    "verify_acceptance_signal_trigger_upgrade",
]
