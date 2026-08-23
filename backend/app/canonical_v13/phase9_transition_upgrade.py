"""Additive, reversible Phase B-to-C intent and risk mode transition."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Final

from sqlalchemy import Connection, inspect, select, text

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA, CANONICAL_MANIFEST_DIGEST
from app.canonical_v13.models import SCHEMA_METADATA_TABLE


PHASE9_TRANSITION_UPGRADE_CONTRACT: Final = (
    "canonical-v13-phase9-shadow-execution-transition-upgrade-v1"
)
INTENT_MODE_COLUMN: Final = "intent_mode"
DECISION_MODE_COLUMN: Final = "decision_mode"
INTENT_MODE_VALUES: Final[tuple[str, ...]] = (
    "TEST_SIMULATED",
    "SIGNAL_RISK_SHADOW",
    "EXECUTION",
)
DECISION_MODE_VALUES: Final[tuple[str, ...]] = INTENT_MODE_VALUES
INTENT_MODE_UNIQUE: Final = "trade_intents_signal_mode_unique"
DECISION_MODE_UNIQUE: Final = "risk_decisions_intent_mode_unique"
INTENT_MODE_CHECK: Final = "ck_trade_intents_trade_intents_intent_mode_values"
DECISION_MODE_CHECK: Final = "ck_risk_decisions_risk_decisions_decision_mode_values"
INTENT_MODE_GUARD_FUNCTION: Final = "guard_trade_intent_mode_immutable"
INTENT_MODE_GUARD_TRIGGER: Final = "trade_intent_mode_immutable"
DECISION_MODE_GUARD_FUNCTION: Final = "guard_risk_decision_mode_immutable"
DECISION_MODE_GUARD_TRIGGER: Final = "risk_decision_mode_immutable"


class CanonicalPhase9TransitionUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Phase9TransitionUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    intent_mode_column_present: bool
    decision_mode_column_present: bool
    constraints_present: tuple[str, ...]
    immutability_triggers_present: tuple[str, ...]
    intent_mode_counts: dict[str, int]
    decision_mode_counts: dict[str, int]
    intent_lineage_digest: str
    decision_lineage_digest: str
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
        raise CanonicalPhase9TransitionUpgradeBlocked(
            "BLOCKED_PHASE9_TRANSITION_SCHEMA_METADATA"
        )
    return value


def _columns(connection: Connection, table_name: str) -> set[str]:
    return {
        str(column["name"])
        for column in inspect(connection).get_columns(
            table_name, schema=CANONICAL_BUSINESS_SCHEMA
        )
    }


def _constraints(connection: Connection) -> tuple[str, ...]:
    inspector = inspect(connection)
    names: set[str] = set()
    for table_name in ("trade_intents", "risk_decisions"):
        for item in inspector.get_unique_constraints(
            table_name, schema=CANONICAL_BUSINESS_SCHEMA
        ):
            if item.get("name"):
                names.add(str(item["name"]))
        for item in inspector.get_check_constraints(
            table_name, schema=CANONICAL_BUSINESS_SCHEMA
        ):
            if item.get("name"):
                names.add(str(item["name"]))
    expected = {
        INTENT_MODE_UNIQUE,
        DECISION_MODE_UNIQUE,
        INTENT_MODE_CHECK,
        DECISION_MODE_CHECK,
    }
    return tuple(sorted(names.intersection(expected)))


def _triggers(connection: Connection) -> tuple[str, ...]:
    rows = connection.execute(
        text(
            "SELECT trigger.tgname FROM pg_catalog.pg_trigger trigger "
            "JOIN pg_catalog.pg_class relation ON relation.oid=trigger.tgrelid "
            "JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace "
            "WHERE namespace.nspname=:schema AND trigger.tgenabled='O' "
            "AND NOT trigger.tgisinternal AND trigger.tgname = ANY(:names)"
        ),
        {
            "schema": CANONICAL_BUSINESS_SCHEMA,
            "names": [INTENT_MODE_GUARD_TRIGGER, DECISION_MODE_GUARD_TRIGGER],
        },
    ).scalars()
    return tuple(sorted(str(value) for value in rows))


def _mode_counts(
    connection: Connection, *, table_name: str, column_name: str, present: bool
) -> dict[str, int]:
    if not present:
        return {}
    rows = connection.execute(
        text(
            f"SELECT {column_name}, count(*) FROM {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
            f"GROUP BY {column_name} ORDER BY {column_name}"
        )
    ).all()
    return {str(mode): int(count) for mode, count in rows}


def _lineage_digest(
    connection: Connection, *, table_name: str, columns: tuple[str, ...]
) -> str:
    selected = ", ".join(columns)
    rows = connection.execute(
        text(
            f"SELECT {selected} FROM {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
            "ORDER BY id"
        )
    ).mappings()
    return _digest(
        [{key: str(value) for key, value in row.items()} for row in rows]
    )


def _result(
    connection: Connection, *, status: str, repeat_noop: bool
) -> Phase9TransitionUpgradeResult:
    intent_present = INTENT_MODE_COLUMN in _columns(connection, "trade_intents")
    decision_present = DECISION_MODE_COLUMN in _columns(
        connection, "risk_decisions"
    )
    payload = {
        "contract": PHASE9_TRANSITION_UPGRADE_CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "intent_mode_column_present": intent_present,
        "decision_mode_column_present": decision_present,
        "constraints_present": _constraints(connection),
        "immutability_triggers_present": _triggers(connection),
        "intent_mode_counts": _mode_counts(
            connection,
            table_name="trade_intents",
            column_name=INTENT_MODE_COLUMN,
            present=intent_present,
        ),
        "decision_mode_counts": _mode_counts(
            connection,
            table_name="risk_decisions",
            column_name=DECISION_MODE_COLUMN,
            present=decision_present,
        ),
        "intent_lineage_digest": _lineage_digest(
            connection,
            table_name="trade_intents",
            columns=(
                ("id", "signal_id", "intent_mode", "intent_digest")
                if intent_present
                else ("id", "signal_id", "intent_digest")
            ),
        ),
        "decision_lineage_digest": _lineage_digest(
            connection,
            table_name="risk_decisions",
            columns=(
                ("id", "trade_intent_id", "decision_mode", "decision_digest")
                if decision_present
                else ("id", "trade_intent_id", "decision_digest")
            ),
        ),
        "repeat_noop": repeat_noop,
    }
    return Phase9TransitionUpgradeResult(**payload, receipt_digest=_digest(payload))


def verify_phase9_transition_upgrade(
    connection: Connection,
) -> Phase9TransitionUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalPhase9TransitionUpgradeBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    intent_present = INTENT_MODE_COLUMN in _columns(connection, "trade_intents")
    decision_present = DECISION_MODE_COLUMN in _columns(connection, "risk_decisions")
    if not intent_present and not decision_present:
        return _result(connection, status="PREVIOUS_READY", repeat_noop=True)
    expected_constraints = tuple(
        sorted(
            (
                INTENT_MODE_UNIQUE,
                DECISION_MODE_UNIQUE,
                INTENT_MODE_CHECK,
                DECISION_MODE_CHECK,
            )
        )
    )
    expected_triggers = tuple(
        sorted((INTENT_MODE_GUARD_TRIGGER, DECISION_MODE_GUARD_TRIGGER))
    )
    result = _result(connection, status="ACCEPTED", repeat_noop=True)
    if (
        intent_present
        and decision_present
        and result.constraints_present == expected_constraints
        and result.immutability_triggers_present == expected_triggers
        and not set(result.intent_mode_counts).difference(INTENT_MODE_VALUES)
        and not set(result.decision_mode_counts).difference(DECISION_MODE_VALUES)
        and result.manifest_digest == CANONICAL_MANIFEST_DIGEST
        and verify_canonical_genesis(connection).accepted
    ):
        return result
    raise CanonicalPhase9TransitionUpgradeBlocked(
        "BLOCKED_PARTIAL_PHASE9_TRANSITION_UPGRADE"
    )


def _install_guards(connection: Connection) -> None:
    schema = CANONICAL_BUSINESS_SCHEMA
    statements = (
        f"""CREATE OR REPLACE FUNCTION {schema}.{INTENT_MODE_GUARD_FUNCTION}()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.intent_mode IS DISTINCT FROM OLD.intent_mode THEN
            RAISE EXCEPTION 'canonical intent_mode is immutable';
          END IF;
          RETURN NEW;
        END $$""",
        f"DROP TRIGGER IF EXISTS {INTENT_MODE_GUARD_TRIGGER} ON {schema}.trade_intents",
        f"""CREATE TRIGGER {INTENT_MODE_GUARD_TRIGGER}
        BEFORE UPDATE ON {schema}.trade_intents FOR EACH ROW
        EXECUTE FUNCTION {schema}.{INTENT_MODE_GUARD_FUNCTION}()""",
        f"""CREATE OR REPLACE FUNCTION {schema}.{DECISION_MODE_GUARD_FUNCTION}()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.decision_mode IS DISTINCT FROM OLD.decision_mode THEN
            RAISE EXCEPTION 'canonical decision_mode is immutable';
          END IF;
          RETURN NEW;
        END $$""",
        f"DROP TRIGGER IF EXISTS {DECISION_MODE_GUARD_TRIGGER} ON {schema}.risk_decisions",
        f"""CREATE TRIGGER {DECISION_MODE_GUARD_TRIGGER}
        BEFORE UPDATE ON {schema}.risk_decisions FOR EACH ROW
        EXECUTE FUNCTION {schema}.{DECISION_MODE_GUARD_FUNCTION}()""",
    )
    for statement in statements:
        connection.execute(text(statement))


def apply_phase9_transition_upgrade(
    connection: Connection,
) -> Phase9TransitionUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_240_901},
    )
    schema = CANONICAL_BUSINESS_SCHEMA
    intent_present = INTENT_MODE_COLUMN in _columns(connection, "trade_intents")
    decision_present = DECISION_MODE_COLUMN in _columns(connection, "risk_decisions")
    if intent_present != decision_present:
        raise CanonicalPhase9TransitionUpgradeBlocked(
            "BLOCKED_PARTIAL_PHASE9_TRANSITION_UPGRADE"
        )
    if intent_present and decision_present:
        try:
            before = verify_phase9_transition_upgrade(connection)
        except CanonicalPhase9TransitionUpgradeBlocked:
            _install_guards(connection)
            verify_phase9_transition_upgrade(connection)
            return _result(connection, status="UPGRADED", repeat_noop=False)
        return before
    before = verify_phase9_transition_upgrade(connection)
    if before.status != "PREVIOUS_READY":
        raise CanonicalPhase9TransitionUpgradeBlocked(
            "BLOCKED_PHASE9_TRANSITION_UPGRADE_STATE"
        )
    connection.execute(
        text(
            f"ALTER TABLE {schema}.trade_intents ADD COLUMN intent_mode VARCHAR(32), "
            "DROP CONSTRAINT uq_trade_intents_signal_id"
        )
    )
    connection.execute(
        text(
            f"ALTER TABLE {schema}.risk_decisions ADD COLUMN decision_mode VARCHAR(32), "
            "DROP CONSTRAINT uq_risk_decisions_trade_intent_id"
        )
    )
    connection.execute(
        text(
            f"UPDATE {schema}.risk_decisions SET decision_mode = CASE "
            "WHEN decision_json->>'decision_mode' IN "
            "('SIGNAL_RISK_SHADOW','EXECUTION') THEN decision_json->>'decision_mode' "
            "WHEN decision_json->>'evidence_class' = 'TEST_SIMULATED' "
            "OR decision_json->>'contract' = 'canonical-v13-simulated-risk-v1' "
            "THEN 'TEST_SIMULATED' ELSE NULL END"
        )
    )
    connection.execute(
        text(
            f"UPDATE {schema}.trade_intents AS intent SET intent_mode = COALESCE("
            "CASE WHEN intent.intent_json->>'intent_mode' IN "
            "('TEST_SIMULATED','SIGNAL_RISK_SHADOW','EXECUTION') "
            "THEN intent.intent_json->>'intent_mode' ELSE NULL END, "
            f"(SELECT decision.decision_mode FROM {schema}.risk_decisions AS decision "
            "WHERE decision.trade_intent_id=intent.id), "
            "CASE WHEN intent.intent_json->>'evidence_class'='TEST_SIMULATED' "
            "THEN 'TEST_SIMULATED' ELSE NULL END)"
        )
    )
    missing = connection.execute(
        text(
            f"SELECT (SELECT count(*) FROM {schema}.trade_intents WHERE intent_mode IS NULL), "
            f"(SELECT count(*) FROM {schema}.risk_decisions WHERE decision_mode IS NULL)"
        )
    ).one()
    if tuple(int(value) for value in missing) != (0, 0):
        raise CanonicalPhase9TransitionUpgradeBlocked(
            "BLOCKED_PHASE9_TRANSITION_BACKFILL_UNCLASSIFIED"
        )
    connection.execute(
        text(
            f"ALTER TABLE {schema}.trade_intents ALTER COLUMN intent_mode SET NOT NULL, "
            f"ADD CONSTRAINT {INTENT_MODE_CHECK} CHECK "
            "(intent_mode IN ('TEST_SIMULATED','SIGNAL_RISK_SHADOW','EXECUTION')), "
            f"ADD CONSTRAINT {INTENT_MODE_UNIQUE} UNIQUE (signal_id,intent_mode)"
        )
    )
    connection.execute(
        text(
            f"ALTER TABLE {schema}.risk_decisions ALTER COLUMN decision_mode SET NOT NULL, "
            f"ADD CONSTRAINT {DECISION_MODE_CHECK} CHECK "
            "(decision_mode IN ('TEST_SIMULATED','SIGNAL_RISK_SHADOW','EXECUTION')), "
            f"ADD CONSTRAINT {DECISION_MODE_UNIQUE} UNIQUE (trade_intent_id,decision_mode)"
        )
    )
    _install_guards(connection)
    verify_phase9_transition_upgrade(connection)
    return _result(connection, status="UPGRADED", repeat_noop=False)


def rollback_phase9_transition_upgrade(
    connection: Connection,
) -> Phase9TransitionUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": 1_308_202_608_240_901},
    )
    before = verify_phase9_transition_upgrade(connection)
    if before.status == "PREVIOUS_READY":
        return before
    schema = CANONICAL_BUSINESS_SCHEMA
    duplicate_counts = connection.execute(
        text(
            "SELECT "
            f"(SELECT count(*) FROM (SELECT signal_id FROM {schema}.trade_intents "
            "GROUP BY signal_id HAVING count(*)>1) duplicate_intents), "
            f"(SELECT count(*) FROM (SELECT trade_intent_id FROM {schema}.risk_decisions "
            "GROUP BY trade_intent_id HAVING count(*)>1) duplicate_decisions)"
        )
    ).one()
    if tuple(int(value) for value in duplicate_counts) != (0, 0):
        raise CanonicalPhase9TransitionUpgradeBlocked(
            "BLOCKED_PHASE9_TRANSITION_ROLLBACK_MULTI_MODE_EVIDENCE"
        )
    for statement in (
        f"DROP TRIGGER {INTENT_MODE_GUARD_TRIGGER} ON {schema}.trade_intents",
        f"DROP TRIGGER {DECISION_MODE_GUARD_TRIGGER} ON {schema}.risk_decisions",
        f"DROP FUNCTION {schema}.{INTENT_MODE_GUARD_FUNCTION}()",
        f"DROP FUNCTION {schema}.{DECISION_MODE_GUARD_FUNCTION}()",
        f"ALTER TABLE {schema}.trade_intents DROP CONSTRAINT {INTENT_MODE_UNIQUE}, "
        f"DROP CONSTRAINT {INTENT_MODE_CHECK}, DROP COLUMN intent_mode, "
        "ADD CONSTRAINT uq_trade_intents_signal_id UNIQUE (signal_id)",
        f"ALTER TABLE {schema}.risk_decisions DROP CONSTRAINT {DECISION_MODE_UNIQUE}, "
        f"DROP CONSTRAINT {DECISION_MODE_CHECK}, DROP COLUMN decision_mode, "
        "ADD CONSTRAINT uq_risk_decisions_trade_intent_id UNIQUE (trade_intent_id)",
    ):
        connection.execute(text(statement))
    return _result(connection, status="ROLLED_BACK", repeat_noop=False)


__all__ = [
    "CanonicalPhase9TransitionUpgradeBlocked",
    "DECISION_MODE_CHECK",
    "DECISION_MODE_COLUMN",
    "DECISION_MODE_GUARD_TRIGGER",
    "DECISION_MODE_UNIQUE",
    "INTENT_MODE_CHECK",
    "INTENT_MODE_COLUMN",
    "INTENT_MODE_GUARD_TRIGGER",
    "INTENT_MODE_UNIQUE",
    "PHASE9_TRANSITION_UPGRADE_CONTRACT",
    "Phase9TransitionUpgradeResult",
    "apply_phase9_transition_upgrade",
    "rollback_phase9_transition_upgrade",
    "verify_phase9_transition_upgrade",
]
