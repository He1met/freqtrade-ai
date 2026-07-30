"""Versioned PostgreSQL schema management for the local Freqtrade AI backend.

The application deliberately keeps migrations small and dependency-free.  PostgreSQL
DDL is transactional, so an upgrade either records the target version after every
contract check succeeds or leaves the database untouched.  Existing *non-empty*
unversioned databases are blocked instead of guessed at or rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
from typing import Iterable, Optional, Union

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine, URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import (
    AddConstraint,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)

from app import models  # noqa: F401 - imports every model into Base.metadata
from app.core.exceptions import ConfigurationError
from app.models.base import Base


LEGACY_SCHEMA_VERSION = "20260712_01"
PREVIOUS_SCHEMA_VERSION = "20260722_01"
TARGET_LINEAGE_BASE_VERSION = "20260723_01"
EARLY_TARGET_LINEAGE_VERSION = "20260727_01"
RISK_CHAIN_BASE_VERSION = "20260727_02"
RISK_CHAIN_HARDENING_BASE_VERSION = "20260727_03"
TRUSTED_SNAPSHOT_BASE_VERSION = "20260727_04"
ATTESTED_SESSION_BASE_VERSION = "20260727_05"
HMAC_ATTESTATION_BASE_VERSION = "20260727_06"
ATTESTATION_ACL_BASE_VERSION = "20260727_07"
ORDER_WRITER_BASE_VERSION = "20260727_08"
RECONCILIATION_BASE_VERSION = "20260727_09"
RUNTIME_RECOVERY_BASE_VERSION = "20260727_10"
FULL_CHAIN_BASE_VERSION = "20260727_11"
SOAK_BASE_VERSION = "20260727_12"
RUNTIME_APP_ACL_BASE_VERSION = "20260727_13"
FILL_SNAPSHOT_REPEAT_BASE_VERSION = "20260727_14"
RECONCILIATION_BATCH_FRESHNESS_BASE_VERSION = "20260728_15"
RECOVERY_WALL_CLOCK_BASE_VERSION = "20260728_16"
DUAL_SIDE_BASE_VERSION = "20260728_17"
STRATEGY_PROMOTION_BASE_VERSION = "20260728_18"
STRATEGY_DEPLOYMENT_BASE_VERSION = "20260729_19"
EXECUTION_FULL_CHAIN_BASE_VERSION = "20260729_20"
RECONCILIATION_INDEX_BASE_VERSION = "20260729_21"
SCHEMA_VERSION = "20260730_22"
VERSION_TABLE = "freqtrade_ai_schema_migrations"
ATTESTATION_PROOF_KEY_ENV = "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY"

RUNTIME_APPLICATION_TABLES = (
    "strategies",
    "strategy_versions",
    "strategy_generation_runs",
    "strategy_failure_reasons",
    "backtest_runs",
    "backtest_tasks",
    "backtest_results",
    "strategy_scores",
    "research_jobs",
    "research_job_attempts",
    "research_worker_control",
    "execution_manifests",
    "local_test_batches",
    "local_test_db_events",
    "full_chain_runs",
    "full_chain_stage_runs",
    "strategy_candidate_approvals",
    "full_chain_signal_snapshots",
    "strategy_deployments",
    "signal_evaluations",
    "risk_budgets",
)


ATTESTED_SESSION_FUNCTION_BODY = """
DECLARE
    attestation_key bytea;
    payload text;
    expected_signature bytea;
    supplied_signature bytea;
    created_timestamp timestamptz;
    expires_timestamp timestamptz;
BEGIN
    IF p_target <> 'OKX_DEMO'
       OR p_session_id !~ '^okx-demo-[0-9a-f]{48}$'
       OR p_fingerprint !~ '^[0-9a-f]{64}$'
       OR p_nonce !~ '^[0-9a-f]{64}$'
       OR p_signature !~ '^[0-9a-f]{64}$'
       OR p_created_micros >= p_expires_micros THEN
        RAISE EXCEPTION 'invalid attested session';
    END IF;
    created_timestamp :=
        TIMESTAMPTZ 'epoch' + p_created_micros * INTERVAL '1 microsecond';
    expires_timestamp :=
        TIMESTAMPTZ 'epoch' + p_expires_micros * INTERVAL '1 microsecond';
    IF expires_timestamp <= statement_timestamp() THEN
        RAISE EXCEPTION 'invalid attested session time window';
    END IF;
    SELECT hmac_key INTO attestation_key
    FROM SCHEMA_TOKEN.okx_demo_attestation_secrets
    WHERE secret_id = 'ACTIVE';
    IF NOT FOUND OR octet_length(attestation_key) <> 32 THEN
        RAISE EXCEPTION 'attestation proof key unavailable';
    END IF;
    payload := concat_ws(
        '|', p_session_id, p_target, p_fingerprint,
        p_created_micros::text, p_expires_micros::text, p_nonce
    );
    expected_signature := public.hmac(
        convert_to(payload, 'UTF8'), attestation_key, 'sha256'
    );
    supplied_signature := decode(p_signature, 'hex');
    IF expected_signature <> supplied_signature THEN
        RAISE EXCEPTION 'invalid attestation proof';
    END IF;
    INSERT INTO SCHEMA_TOKEN.okx_demo_attested_sessions (
        session_id, execution_target_id, pinned_fingerprint_sha256,
        capability_proof_digest, attestation_nonce, created_at, expires_at
    ) VALUES (
        p_session_id, p_target, p_fingerprint,
        encode(public.digest(convert_to(p_signature, 'UTF8'), 'sha256'), 'hex'),
        p_nonce, created_timestamp, expires_timestamp
    );
EXCEPTION
    WHEN unique_violation THEN
        RAISE EXCEPTION 'attested session replay or identity conflict';
END;
"""


TRUSTED_SNAPSHOT_FUNCTION_BODY = """
DECLARE
    bound_session SCHEMA_TOKEN.okx_demo_attested_sessions%ROWTYPE;
    inserted_id bigint;
BEGIN
    SELECT * INTO bound_session
    FROM SCHEMA_TOKEN.okx_demo_attested_sessions
    WHERE session_id = p_session_id
      AND capability_proof_digest =
          encode(public.digest(convert_to(p_proof, 'UTF8'), 'sha256'), 'hex')
      AND execution_target_id = 'OKX_DEMO'
      AND revoked_at IS NULL
      AND p_observed_at >= created_at
      AND p_observed_at < expires_at;
    IF NOT FOUND
       OR p_kind NOT IN ('instrument', 'market', 'account')
       OR p_snapshot_id !~ '^(instrument|market|account):[0-9a-f]{48}$'
       OR p_digest !~ '^[0-9a-f]{64}$'
       OR p_observed_at >= p_expires_at
       OR p_expires_at > bound_session.expires_at
       OR p_content->>'execution_target' <> 'OKX_DEMO'
       OR p_content->>'source' <> 'okx_demo_rest'
       OR p_content->>'resource' <> p_kind
       OR p_content->>'stale' <> 'false'
       OR (
           p_kind = 'account' AND (
               p_content->>'authenticated' <> 'true'
               OR p_content->>'pinned_account_fingerprint'
                  <> bound_session.pinned_fingerprint_sha256
           )
       ) THEN
        RAISE EXCEPTION 'invalid trusted snapshot capability';
    END IF;
    INSERT INTO SCHEMA_TOKEN.okx_demo_trusted_snapshots (
        snapshot_id, kind, execution_target_id, content_json, digest,
        source_type, core_data, attested_session_id,
        attestation_fingerprint_sha256, attested_session_expires_at,
        observed_at, expires_at
    ) VALUES (
        p_snapshot_id, p_kind, 'OKX_DEMO', p_content, p_digest,
        'api_aggregate', TRUE, p_session_id,
        bound_session.pinned_fingerprint_sha256,
        bound_session.expires_at, p_observed_at, p_expires_at
    )
    ON CONFLICT (snapshot_id) DO NOTHING
    RETURNING database_id INTO inserted_id;
    IF inserted_id IS NULL THEN
        SELECT database_id INTO inserted_id
        FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE snapshot_id = p_snapshot_id AND digest = p_digest
          AND attested_session_id = p_session_id;
    END IF;
    IF inserted_id IS NULL THEN
        RAISE EXCEPTION 'trusted snapshot identity conflict';
    END IF;
    RETURN inserted_id;
END;
"""


REVOKE_SESSION_FUNCTION_BODY = """
DECLARE
    affected_rows integer;
BEGIN
    IF p_reason NOT IN (
        'IDENTITY_DRIFT', 'EXPIRED', 'FACTORY_CLOSE', 'WRITE_FAILURE'
    )
       OR p_signature !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid attested session revocation';
    END IF;
    UPDATE SCHEMA_TOKEN.okx_demo_attested_sessions
    SET revoked_at =
            TIMESTAMPTZ 'epoch' + p_revoked_micros * INTERVAL '1 microsecond',
        revoke_reason = p_reason
    WHERE session_id = p_session_id
      AND capability_proof_digest =
          encode(public.digest(convert_to(p_signature, 'UTF8'), 'sha256'), 'hex')
      AND revoked_at IS NULL
      AND TIMESTAMPTZ 'epoch'
            + p_revoked_micros * INTERVAL '1 microsecond' >= created_at
      AND TIMESTAMPTZ 'epoch'
            + p_revoked_micros * INTERVAL '1 microsecond'
            <= statement_timestamp() + INTERVAL '5 minutes';
    GET DIAGNOSTICS affected_rows = ROW_COUNT;
    IF affected_rows = 0 AND NOT EXISTS (
        SELECT 1 FROM SCHEMA_TOKEN.okx_demo_attested_sessions
        WHERE session_id = p_session_id
          AND capability_proof_digest =
              encode(public.digest(convert_to(p_signature, 'UTF8'), 'sha256'), 'hex')
          AND revoked_at IS NOT NULL
          AND revoke_reason = p_reason
    ) THEN
        RAISE EXCEPTION 'attested session revocation rejected';
    END IF;
END;
"""


class SchemaMigrationBlocked(ConfigurationError):
    """Raised when a legacy database needs an explicit, data-preserving migration."""


@dataclass(frozen=True)
class SchemaReadiness:
    database_identity: str
    schema_version: Optional[str]
    ready: bool
    problems: tuple[str, ...]


def psql_database_url(database_url: str) -> str:
    """Return a libpq URL without the SQLAlchemy driver or embedded password."""

    try:
        url = make_url(database_url)
    except Exception as exc:  # SQLAlchemy normalizes several URL parsing errors.
        raise ConfigurationError("DATABASE_URL is not a valid SQLAlchemy URL.") from exc
    if not url.drivername.startswith("postgresql"):
        raise ConfigurationError("PostgreSQL DATABASE_URL is required for migrations.")
    return URL.create(
        drivername="postgresql",
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    ).render_as_string(hide_password=False)


def database_identity(engine: Engine) -> str:
    """Expose only dialect, host, port and database name for diagnostics."""

    url = engine.url
    host = url.host or "local"
    port = f":{url.port}" if url.port else ""
    database = url.database or "<default>"
    return f"{engine.dialect.name}://{host}{port}/{database}"


def _require_postgres(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        raise ConfigurationError(
            "Versioned migrations require PostgreSQL; refusing to treat a non-PostgreSQL "
            "database as a successful migration target."
        )


def _expected_tables() -> dict[str, object]:
    # ``strategies.current_version_id`` and ``strategy_versions.strategy_id`` form a
    # legitimate FK cycle, so ``sorted_tables`` emits a warning and is not needed for
    # read-only contract comparison.
    return {name: Base.metadata.tables[name] for name in sorted(Base.metadata.tables)}


def _expected_unique_columns(table: object) -> set[frozenset[str]]:
    unique_sets: set[frozenset[str]] = set()
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            unique_sets.add(frozenset(column.name for column in constraint.columns))
    for column in table.columns:
        if column.unique:
            unique_sets.add(frozenset((column.name,)))
    return unique_sets


def _expected_indexes(
    table: object,
) -> set[tuple[tuple[str, ...], bool, Optional[str]]]:
    return {_metadata_index_signature(index) for index in table.indexes if isinstance(index, Index)}


def _normalized_sql_definition(value: object) -> Optional[str]:
    if value is None:
        return None
    rendered = str(value).lower().replace('"', "")
    rendered = re.sub(
        r"::(?:character varying|text|boolean|integer|bigint|numeric)(?:\[\])?",
        "",
        rendered,
    )
    rendered = re.sub(r"\s+", "", rendered)
    rendered = re.sub(
        r"([a-z_][a-z0-9_]*)=any\(array\[(.*?)\]\)",
        r"\1in(\2)",
        rendered,
    )
    rendered = re.sub(
        r"\(([a-z_][a-z0-9_]*in\([^()]*\))\)",
        r"\1",
        rendered,
    )
    return rendered


def _normalized_index_definition(value: object) -> Optional[str]:
    rendered = _normalized_sql_definition(value)
    if rendered is None:
        return None
    rendered = re.sub(r"\(([a-z_][a-z0-9_]*)\)", r"\1", rendered)
    rendered = re.sub(
        r"([a-z_][a-z0-9_]*)=any\(\(?array\[(.*?)\]\)?\)",
        r"\1in(\2)",
        rendered,
    )
    while rendered.startswith("(") and rendered.endswith(")"):
        depth = 0
        closes_at_end = True
        for index, character in enumerate(rendered):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(rendered) - 1:
                    closes_at_end = False
                    break
        if not closes_at_end or depth != 0:
            break
        rendered = rendered[1:-1]
    return rendered


def _canonical_function_body(value: object, schema_name: str) -> str:
    """Normalize a PL/pgSQL body while retaining every security-relevant token."""

    rendered = str(value)
    rendered = rendered.replace('"{}"'.format(schema_name.replace('"', '""')), "SCHEMA_TOKEN")
    rendered = re.sub(r"\s+", " ", rendered).strip()
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _metadata_index_signature(index: Index) -> tuple[tuple[str, ...], bool, Optional[str]]:
    predicate = index.dialect_options["postgresql"].get("where")
    return (
        tuple(column.name for column in index.columns),
        bool(index.unique),
        _normalized_index_definition(predicate),
    )


def _inspected_index_signature(index: dict) -> tuple[tuple[str, ...], bool, Optional[str]]:
    dialect_options = index.get("dialect_options") or {}
    return (
        tuple(index.get("column_names") or ()),
        bool(index.get("unique", False)),
        _normalized_index_definition(dialect_options.get("postgresql_where")),
    )


CRITICAL_CHECK_DEFINITIONS = {
    "execution_scopes_known_contract_check",
    "trade_intents_okx_demo_target_check",
    "trade_intents_client_order_id_format_check",
    "trade_intents_status_check",
    "trade_intents_authorization_schema_check",
    "trade_intents_scope_contract_check",
    "trade_intents_intent_id_format_check",
    "trade_intents_canonical_hash_format_check",
    "trade_intents_policy_digest_format_check",
    "trade_intents_idempotency_digest_format_check",
    "trade_intents_side_check",
    "trade_intents_position_side_check",
    "trade_intents_margin_mode_check",
    "trade_intents_order_type_check",
    "trade_intents_order_combo_check",
    "risk_decisions_okx_demo_target_check",
    "risk_decisions_decision_check",
    "risk_decisions_authorization_schema_check",
    "risk_decisions_policy_digest_format_check",
    "risk_budgets_nonnegative_check",
    "approved_executions_okx_demo_target_check",
    "approved_executions_no_submission_check",
    "approved_executions_claim_required_check",
    "approved_executions_status_check",
    "approved_executions_approved_state_check",
    "approved_executions_reservation_check",
    "approved_executions_client_order_id_format_check",
    "approved_executions_intent_id_format_check",
    "approved_executions_authorization_schema_check",
    "approved_executions_canonical_hash_format_check",
    "approved_executions_policy_digest_format_check",
    "approved_executions_payload_hash_format_check",
    "okx_demo_attested_sessions_target_check",
    "okx_demo_attested_sessions_fingerprint_format_check",
    "okx_demo_attested_sessions_proof_format_check",
    "okx_demo_attested_sessions_time_check",
    "okx_demo_attestation_secrets_contract_check",
    "okx_demo_trusted_snapshots_kind_check",
    "okx_demo_trusted_snapshots_target_check",
    "okx_demo_trusted_snapshots_source_check",
    "okx_demo_trusted_snapshots_digest_format_check",
    "okx_demo_trusted_snapshots_fingerprint_format_check",
    "okx_demo_trusted_snapshots_time_check",
    "okx_order_writer_leases_target_check",
    "okx_order_writer_leases_digest_check",
    "okx_order_writer_leases_generation_check",
    "okx_order_writer_leases_time_check",
    "okx_order_write_attempts_target_check",
    "okx_order_write_attempts_operation_check",
    "okx_order_write_attempts_state_check",
    "okx_order_write_attempts_single_post_check",
    "okx_order_write_attempts_fencing_sequence_check",
    "okx_order_write_attempts_digest_check",
    "exchange_orders_okx_demo_target_check",
    "exchange_orders_client_order_id_format_check",
    "exchange_fills_okx_demo_target_check",
    "exchange_positions_okx_demo_target_check",
    "reconciliation_runs_okx_demo_target_check",
    "reconciliation_runs_status_check",
    "reconciliation_runs_artifact_status_check",
    "okx_demo_exchange_events_target_check",
    "okx_demo_exchange_events_source_check",
    "okx_demo_exchange_events_ws_sequence_check",
    "okx_demo_exchange_events_kind_check",
    "okx_demo_exchange_events_digest_check",
    "okx_demo_order_snapshots_target_check",
    "okx_demo_order_snapshots_status_check",
    "okx_demo_fill_snapshots_target_check",
    "okx_demo_position_snapshots_target_check",
    "okx_demo_account_snapshots_target_check",
    "okx_demo_reconciliation_states_target_check",
    "okx_demo_reconciliation_states_status_check",
    "okx_demo_recovery_batches_target_check",
    "okx_demo_recovery_batches_complete_check",
    "okx_demo_recovery_batches_time_check",
    "okx_demo_recovery_grants_target_check",
    "okx_demo_recovery_grants_action_check",
    "okx_demo_recovery_grants_status_check",
    "okx_demo_recovery_grants_digest_quantity_check",
    "execution_manifests_authorization_check",
}


def schema_problems(bind: Union[Connection, Engine]) -> list[str]:
    """Compare the live PostgreSQL schema to the SQLAlchemy metadata contract."""

    if isinstance(bind, Engine):
        with bind.connect() as connection:
            return schema_problems(connection)

    problems: list[str] = []
    if bind.dialect.name != "postgresql":
        return ["database dialect is not PostgreSQL"]

    schema_name = bind.execute(text("SELECT current_schema()")).scalar_one()
    inspector = inspect(bind)
    actual_table_names = set(inspector.get_table_names(schema=schema_name))
    for name, table in _expected_tables().items():
        if name not in actual_table_names:
            problems.append(f"missing table: {name}")
            continue

        expected_columns = {column.name for column in table.columns}
        inspected_columns = inspector.get_columns(name, schema=schema_name)
        actual_columns = {column["name"] for column in inspected_columns}
        for column in sorted(expected_columns - actual_columns):
            problems.append(f"missing column: {name}.{column}")
        for column in sorted(actual_columns - expected_columns):
            problems.append(f"unexpected column: {name}.{column}")
        actual_nullable = {
            column["name"]: bool(column.get("nullable", True))
            for column in inspected_columns
        }
        for column in table.columns:
            if column.name in actual_nullable and actual_nullable[column.name] != column.nullable:
                problems.append(
                    f"nullable mismatch: {name}.{column.name} "
                    f"is nullable={actual_nullable[column.name]}, expected {column.nullable}"
                )

        expected_fks = {
            (
                tuple(element.parent.name for element in constraint.elements),
                constraint.elements[0].column.table.schema or schema_name,
                constraint.elements[0].column.table.name,
                tuple(element.column.name for element in constraint.elements),
                (constraint.ondelete or "NO ACTION").upper(),
                (constraint.onupdate or "NO ACTION").upper(),
                bool(constraint.deferrable),
                (constraint.initially or "").upper() or None,
            )
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        # The recovery-grant parent is installed by the later #448 migration.
        # Keeping this FK out of the early #447 ORM CREATE prevents legacy
        # upgrades from referencing a table that does not exist yet; the
        # runtime recovery migration adds and verifies the real PostgreSQL FK.
        if name == "okx_order_write_attempts":
            expected_fks.add(
                (
                    ("recovery_grant_database_id",),
                    schema_name,
                    "okx_demo_recovery_grants",
                    ("database_id",),
                    "RESTRICT",
                    "NO ACTION",
                    False,
                    None,
                )
            )
        actual_fks = {
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key.get("referred_schema") or schema_name,
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
                (
                    (foreign_key.get("options") or {}).get("ondelete")
                    or "NO ACTION"
                ).upper(),
                (
                    (foreign_key.get("options") or {}).get("onupdate")
                    or "NO ACTION"
                ).upper(),
                bool(
                    (foreign_key.get("options") or {}).get(
                        "deferrable",
                        False,
                    )
                ),
                (
                    (foreign_key.get("options") or {}).get("initially")
                    or ""
                ).upper()
                or None,
            )
            for foreign_key in inspector.get_foreign_keys(name, schema=schema_name)
        }
        for foreign_key in sorted(expected_fks - actual_fks, key=str):
            problems.append(
                "missing foreign key: "
                f"{name}.({','.join(foreign_key[0])}) -> "
                f"{foreign_key[1]}.{foreign_key[2]}"
                f".({','.join(foreign_key[3])}) "
                f"ondelete={foreign_key[4]} onupdate={foreign_key[5]} "
                f"deferrable={foreign_key[6]} "
                f"initially={foreign_key[7] or '<none>'}"
            )
        for foreign_key in sorted(actual_fks - expected_fks, key=str):
            problems.append(
                "unexpected foreign key: "
                f"{name}.({','.join(foreign_key[0])}) -> "
                f"{foreign_key[1]}.{foreign_key[2]}"
                f".({','.join(foreign_key[3])}) "
                f"ondelete={foreign_key[4]} onupdate={foreign_key[5]} "
                f"deferrable={foreign_key[6]} "
                f"initially={foreign_key[7] or '<none>'}"
            )

        actual_unique = {
            frozenset(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(name, schema=schema_name)
            if constraint.get("column_names")
        }
        for columns in sorted(_expected_unique_columns(table) - actual_unique, key=sorted):
            problems.append(f"missing unique constraint: {name}({','.join(sorted(columns))})")
        for columns in sorted(actual_unique - _expected_unique_columns(table), key=sorted):
            problems.append(
                f"unexpected unique constraint: {name}({','.join(sorted(columns))})"
            )

        inspected_indexes = [
            index
            for index in inspector.get_indexes(name, schema=schema_name)
            if index.get("column_names") and not index.get("duplicates_constraint")
        ]
        actual_indexes = {_inspected_index_signature(index) for index in inspected_indexes}
        for columns, unique, predicate in sorted(
            _expected_indexes(table) - actual_indexes,
            key=str,
        ):
            problems.append(
                f"missing index: {name}({','.join(columns)}) "
                f"unique={unique} predicate={predicate or '<none>'}"
            )
        for columns, unique, predicate in sorted(actual_indexes, key=str):
            if unique and (columns, unique, predicate) not in _expected_indexes(table):
                problems.append(
                    f"unexpected unique index: {name}({','.join(columns)}) "
                    f"predicate={predicate or '<none>'}"
                )

        expected_checks = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
        }
        actual_check_definitions = {
            constraint.get("name"): _normalized_sql_definition(constraint.get("sqltext"))
            for constraint in inspector.get_check_constraints(name, schema=schema_name)
            if constraint.get("name")
        }
        for check_name in sorted(expected_checks - set(actual_check_definitions)):
            problems.append(f"missing check constraint: {name}.{check_name}")
        expected_check_definitions = {
            constraint.name: _normalized_sql_definition(
                constraint.sqltext.compile(dialect=bind.dialect)
            )
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
            and constraint.name in CRITICAL_CHECK_DEFINITIONS
        }
        for check_name, definition in expected_check_definitions.items():
            actual_definition = actual_check_definitions.get(check_name)
            if actual_definition is not None and actual_definition != definition:
                problems.append(
                    f"check definition mismatch: {name}.{check_name}"
                )
    trigger_rows = bind.execute(
        text(
            """
            SELECT trigger.tgname, pg_get_triggerdef(trigger.oid),
                   pg_get_functiondef(function.oid)
            FROM pg_trigger AS trigger
            JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN pg_proc AS function ON function.oid = trigger.tgfoid
            WHERE namespace.nspname = :schema_name AND NOT trigger.tgisinternal
            """
        ),
        {"schema_name": schema_name},
    ).all()
    trigger_definitions = {
        name: (
            _normalized_sql_definition(trigger_definition) or "",
            _normalized_sql_definition(function_definition) or "",
        )
        for name, trigger_definition, function_definition in trigger_rows
    }
    expected_trigger_fragments = {
        "trade_intents_active_approval_immutable": (
            "beforeupdateon",
            "activeapprovedintentisimmutable",
            "old.quantityisdistinctfromnew.quantity",
            "old.policy_digestisdistinctfromnew.policy_digest",
        ),
        "okx_demo_trusted_snapshots_immutable": (
            "beforedeleteorupdateon",
            "okx_demo_trusted_snapshots",
            "trustedsnapshotsareimmutable",
        ),
        "okx_demo_recovery_grants_guard": (
            "beforeupdateon",
            "invalidrecoverygranttransition",
            "old.grant_digestisdistinctfromnew.grant_digest",
            "old.status<>'active'",
        ),
        "exchange_orders_guard": (
            "beforeinsertorupdateon",
            "invalidexchangeordercreation",
            "invalidexchangeordertransition",
            "old.request_snapshot::jsonb",
            "old.exchange_order_idisnotnull",
        ),
    }
    for table_name in (
        "okx_demo_exchange_events",
        "okx_demo_order_snapshots",
        "okx_demo_fill_snapshots",
        "okx_demo_position_snapshots",
        "okx_demo_account_snapshots",
        "okx_demo_recovery_batches",
    ):
        expected_trigger_fragments[table_name + "_immutable"] = (
            "beforedeleteorupdateon",
            table_name,
            "reconciliationevidenceisimmutable",
        )
    for trigger_name, fragments in expected_trigger_fragments.items():
        definitions = trigger_definitions.get(trigger_name)
        if definitions is None:
            problems.append(f"missing trigger: {trigger_name}")
            continue
        combined = "".join(definitions)
        if any(fragment not in combined for fragment in fragments):
            problems.append(f"trigger definition mismatch: {trigger_name}")
    role_rows = bind.execute(
        text(
            "SELECT rolname, rolcanlogin, rolinherit, rolsuper, "
            "rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
            "FROM pg_roles "
            "WHERE rolname IN ('freqtrade', 'freqtrade_ai_attestor')"
        )
    ).all()
    roles = {
        row[0]: tuple(row[1:])
        for row in role_rows
    }
    if "freqtrade_ai_attestor" not in roles:
        problems.append("missing NOLOGIN role: freqtrade_ai_attestor")
    elif roles["freqtrade_ai_attestor"] != (
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    ):
        problems.append(
            "attestor role boundary mismatch: expected unprivileged "
            "NOLOGIN NOINHERIT"
        )
    if "freqtrade" not in roles:
        problems.append("missing runtime role: freqtrade")
    if {"freqtrade", "freqtrade_ai_attestor"}.issubset(roles):
        membership_boundary = bind.execute(
            text(
                """
                WITH RECURSIVE role_graph(roleid, member, visited) AS (
                    SELECT membership.roleid, membership.member,
                           ARRAY[membership.member, membership.roleid]::oid[]
                    FROM pg_auth_members AS membership
                    WHERE membership.member = (
                        SELECT oid FROM pg_roles WHERE rolname = 'freqtrade'
                    )
                    UNION ALL
                    SELECT membership.roleid, membership.member,
                           graph.visited || membership.roleid
                    FROM pg_auth_members AS membership
                    JOIN role_graph AS graph
                      ON membership.member = graph.roleid
                    WHERE NOT membership.roleid = ANY(graph.visited)
                ),
                attestor_graph(roleid, member, visited) AS (
                    SELECT membership.roleid, membership.member,
                           ARRAY[membership.member, membership.roleid]::oid[]
                    FROM pg_auth_members AS membership
                    WHERE membership.member = (
                        SELECT oid FROM pg_roles
                        WHERE rolname = 'freqtrade_ai_attestor'
                    )
                    UNION ALL
                    SELECT membership.roleid, membership.member,
                           graph.visited || membership.roleid
                    FROM pg_auth_members AS membership
                    JOIN attestor_graph AS graph
                      ON membership.member = graph.roleid
                    WHERE NOT membership.roleid = ANY(graph.visited)
                )
                SELECT
                    EXISTS (
                        SELECT 1 FROM role_graph
                        WHERE roleid = (
                            SELECT oid FROM pg_roles
                            WHERE rolname = 'freqtrade_ai_attestor'
                        )
                    ),
                    EXISTS (
                        SELECT 1
                        FROM attestor_graph AS graph
                        JOIN pg_roles AS role ON role.oid = graph.roleid
                        WHERE role.rolsuper
                           OR role.rolcreaterole
                           OR role.rolcreatedb
                           OR role.rolreplication
                           OR role.rolbypassrls
                    )
                """
            )
        ).one()
        if membership_boundary[0]:
            problems.append(
                "runtime role membership reaches attestor owner role"
            )
        if membership_boundary[1]:
            problems.append(
                "attestor role membership reaches a privileged role"
            )
        delegated_roles = bind.execute(
            text(
                """
                WITH RECURSIVE delegated(root_role, member, visited) AS (
                    SELECT owner.rolname, membership.member,
                           ARRAY[membership.roleid, membership.member]::oid[]
                    FROM pg_auth_members AS membership
                    JOIN pg_roles AS owner ON owner.oid = membership.roleid
                    WHERE owner.rolname IN (
                        'freqtrade', 'freqtrade_ai_attestor'
                    )
                    UNION ALL
                    SELECT delegated.root_role, membership.member,
                           delegated.visited || membership.member
                    FROM pg_auth_members AS membership
                    JOIN delegated ON membership.roleid = delegated.member
                    WHERE NOT membership.member = ANY(delegated.visited)
                )
                SELECT delegated.root_role, member.rolname
                FROM delegated
                JOIN pg_roles AS member ON member.oid = delegated.member
                ORDER BY delegated.root_role, member.rolname
                """
            )
        ).all()
        for root_role, member_role in delegated_roles:
            problems.append(
                "protected role has delegated member: {} -> {}".format(
                    member_role,
                    root_role,
                )
            )
        schema_security = bind.execute(
            text(
                """
                SELECT owner.rolname,
                       has_schema_privilege(
                           'freqtrade', namespace.oid, 'USAGE'
                       ),
                       has_schema_privilege(
                           'freqtrade', namespace.oid, 'CREATE'
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(
                               COALESCE(
                                   namespace.nspacl,
                                   acldefault('n', namespace.nspowner)
                               )
                           ) AS acl
                           WHERE acl.grantee = 0
                             AND acl.privilege_type = 'CREATE'
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(
                               COALESCE(
                                   namespace.nspacl,
                                   acldefault('n', namespace.nspowner)
                               )
                           ) AS acl
                           WHERE acl.grantee NOT IN (
                               0,
                               namespace.nspowner
                           )
                             AND acl.privilege_type = 'CREATE'
                       )
                FROM pg_namespace AS namespace
                JOIN pg_roles AS owner ON owner.oid = namespace.nspowner
                WHERE namespace.nspname = :schema_name
                """
            ),
            {"schema_name": schema_name},
        ).first()
        if schema_security is None:
            problems.append("active writer schema is missing")
        else:
            (
                _schema_owner,
                runtime_schema_usage,
                runtime_schema_create,
                public_schema_create,
                unexpected_schema_create,
            ) = schema_security
            if _schema_owner != "freqtrade_ai_attestor":
                problems.append(
                    "writer schema owner mismatch: owner={}".format(
                        _schema_owner
                    )
                )
            if not runtime_schema_usage:
                problems.append("runtime writer schema USAGE privilege missing")
            if runtime_schema_create:
                problems.append(
                    "runtime writer has CREATE on writer schema"
                )
            if public_schema_create:
                problems.append(
                    "PUBLIC CREATE privilege is not revoked on writer schema"
                )
            if unexpected_schema_create:
                problems.append(
                    "unexpected role has CREATE on writer schema"
                )
        column_security = bind.execute(
            text(
                """
                WITH RECURSIVE runtime_roles(roleid, visited) AS (
                    SELECT oid, ARRAY[oid]::oid[]
                    FROM pg_roles WHERE rolname = 'freqtrade'
                    UNION ALL
                    SELECT membership.roleid,
                           roles.visited || membership.roleid
                    FROM pg_auth_members AS membership
                    JOIN runtime_roles AS roles
                      ON membership.member = roles.roleid
                    WHERE NOT membership.roleid = ANY(roles.visited)
                )
                SELECT relation.relname, attribute.attname,
                       EXISTS (
                           SELECT 1 FROM runtime_roles
                           WHERE has_column_privilege(
                               runtime_roles.roleid, relation.oid,
                               attribute.attnum,
                               'INSERT,UPDATE,REFERENCES'
                           )
                       ),
                       EXISTS (
                           SELECT 1 FROM runtime_roles
                           WHERE relation.relname =
                                 'okx_demo_attestation_secrets'
                             AND has_column_privilege(
                                 runtime_roles.roleid, relation.oid,
                                 attribute.attnum, 'SELECT'
                             )
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(attribute.attacl) AS acl
                           WHERE acl.grantee = 0
                             AND acl.privilege_type IN (
                                 'SELECT', 'INSERT', 'UPDATE', 'REFERENCES'
                             )
                       )
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = relation.oid
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
                WHERE namespace.nspname = :schema_name
                  AND relation.relname IN (
                      'okx_demo_attested_sessions',
                      'okx_demo_attestation_secrets',
                      'okx_demo_trusted_snapshots'
                  )
                """
            ),
            {"schema_name": schema_name},
        ).all()
        for (
            table_name,
            column_name,
            runtime_can_mutate,
            runtime_can_read_secret,
            public_column_privilege,
        ) in column_security:
            if runtime_can_mutate:
                problems.append(
                    "runtime column DML is not revoked: {}.{}".format(
                        table_name, column_name
                    )
                )
            if runtime_can_read_secret:
                problems.append(
                    "runtime can read attestation secret column: {}.{}".format(
                        table_name, column_name
                    )
                )
            if public_column_privilege:
                problems.append(
                    "PUBLIC attestation column privilege is not revoked: "
                    "{}.{}".format(table_name, column_name)
                )
        server_version_num = int(
            bind.execute(text("SHOW server_version_num")).scalar_one()
        )
        unsafe_table_privileges = (
            "INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER"
            + (",MAINTAIN" if server_version_num >= 170000 else "")
        )
        writer_unsafe_table_privileges = (
            "DELETE,TRUNCATE,REFERENCES,TRIGGER"
            + (",MAINTAIN" if server_version_num >= 170000 else "")
        )
        table_security = bind.execute(
            text(
                """
                WITH RECURSIVE runtime_roles(roleid, visited) AS (
                    SELECT oid, ARRAY[oid]::oid[]
                    FROM pg_roles WHERE rolname = 'freqtrade'
                    UNION ALL
                    SELECT membership.roleid,
                           roles.visited || membership.roleid
                    FROM pg_auth_members AS membership
                    JOIN runtime_roles AS roles
                      ON membership.member = roles.roleid
                    WHERE NOT membership.roleid = ANY(roles.visited)
                )
                SELECT relation.relname, owner.rolname,
                       EXISTS (
                           SELECT 1 FROM runtime_roles
                           WHERE has_table_privilege(
                               runtime_roles.roleid, relation.oid,
                               :unsafe_privileges
                           )
                       ),
                       EXISTS (
                           SELECT 1 FROM runtime_roles
                           WHERE has_table_privilege(
                               runtime_roles.roleid, relation.oid, 'SELECT'
                           )
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(relation.relacl) AS acl
                           WHERE acl.grantee = 0
                             AND acl.privilege_type IN (
                                 'SELECT', 'INSERT', 'UPDATE', 'DELETE',
                                 'TRUNCATE', 'REFERENCES', 'TRIGGER', 'MAINTAIN'
                             )
                       )
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_roles AS owner ON owner.oid = relation.relowner
                WHERE namespace.nspname = :schema_name
                  AND relation.relname IN (
                      'okx_demo_attested_sessions',
                      'okx_demo_attestation_secrets',
                      'okx_demo_trusted_snapshots'
                  )
                """
            ),
            {
                "schema_name": schema_name,
                "unsafe_privileges": unsafe_table_privileges,
            },
        ).all()
        secured_tables = {
            name: (owner, can_write, can_read, public_privilege)
            for name, owner, can_write, can_read, public_privilege in table_security
        }
        for table_name in (
            "okx_demo_attested_sessions",
            "okx_demo_attestation_secrets",
            "okx_demo_trusted_snapshots",
        ):
            (
                owner,
                runtime_can_write,
                runtime_can_read,
                public_privilege,
            ) = secured_tables.get(
                table_name, (None, True, True, True)
            )
            if owner != "freqtrade_ai_attestor":
                problems.append(
                    "attestation owner mismatch: {} owner={}".format(
                        table_name, owner or "<missing>"
                    )
                )
            if runtime_can_write:
                problems.append(
                    "runtime reachable table privileges are not revoked: {}".format(
                        table_name
                    )
                )
            if public_privilege:
                problems.append(
                    "PUBLIC attestation table privilege is not revoked: {}".format(
                        table_name
                    )
                )
            if (
                table_name == "okx_demo_attestation_secrets"
                and runtime_can_read
            ):
                problems.append("runtime can read attestation secret table")
            if (
                table_name != "okx_demo_attestation_secrets"
                and not runtime_can_read
            ):
                problems.append(
                    "runtime attestation read privilege missing: {}".format(table_name)
                )
        writer_table_rows = bind.execute(
            text(
                """
                SELECT relation.relname, owner.rolname,
                       has_table_privilege(
                           'freqtrade', relation.oid, 'SELECT'
                       ),
                       has_table_privilege(
                           'freqtrade', relation.oid, 'INSERT'
                       ),
                       has_table_privilege(
                           'freqtrade', relation.oid, 'UPDATE'
                       ),
                       has_table_privilege(
                           'freqtrade', relation.oid,
                           :writer_unsafe_privileges
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(
                               COALESCE(
                                   relation.relacl,
                                   acldefault('r', relation.relowner)
                               )
                           ) AS acl
                           WHERE acl.grantee = 0
                             AND acl.privilege_type IN (
                                 'SELECT', 'INSERT', 'UPDATE', 'DELETE',
                                 'TRUNCATE', 'REFERENCES', 'TRIGGER',
                                 'MAINTAIN'
                             )
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(
                               COALESCE(
                                   relation.relacl,
                                   acldefault('r', relation.relowner)
                               )
                           ) AS acl
                           WHERE acl.grantee NOT IN (
                               0,
                               relation.relowner,
                               (SELECT oid FROM pg_roles
                                WHERE rolname = 'freqtrade')
                           )
                             AND acl.privilege_type IN (
                                 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
                                 'REFERENCES', 'TRIGGER', 'MAINTAIN'
                             )
                             OR (
                                 relation.relname IN (
                                     'execution_scopes',
                                     'trade_intents',
                                     'risk_decisions',
                                     'approved_executions',
                                     'exchange_orders',
                                     'exchange_fills',
                                     'exchange_positions'
                                 )
                                 AND acl.grantee NOT IN (
                                     0,
                                     relation.relowner,
                                     (SELECT oid FROM pg_roles
                                      WHERE rolname = 'freqtrade')
                                 )
                                 AND acl.privilege_type = 'SELECT'
                             )
                       )
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_roles AS owner ON owner.oid = relation.relowner
                WHERE namespace.nspname = :schema_name
                  AND relation.relname IN (
                      'execution_scopes',
                      'trade_intents',
                      'risk_decisions',
                      'approved_executions',
                      'exchange_orders',
                      'exchange_fills',
                      'exchange_positions',
                      'okx_order_writer_leases',
                      'okx_order_write_attempts',
                      'reconciliation_runs',
                      'okx_demo_exchange_events',
                      'okx_demo_order_snapshots',
                      'okx_demo_fill_snapshots',
                      'okx_demo_position_snapshots',
                      'okx_demo_account_snapshots',
                      'okx_demo_reconciliation_states',
                      'okx_demo_recovery_batches',
                      'okx_demo_recovery_grants'
                  )
                ORDER BY relation.relname
                """
            ),
            {
                "schema_name": schema_name,
                "writer_unsafe_privileges": writer_unsafe_table_privileges,
            },
        ).all()
        writer_tables = {
            row[0]: tuple(row[1:])
            for row in writer_table_rows
        }
        for table_name in (
            "execution_scopes",
            "trade_intents",
            "risk_decisions",
            "approved_executions",
            "exchange_orders",
            "exchange_fills",
            "exchange_positions",
            "okx_order_writer_leases",
            "okx_order_write_attempts",
            "reconciliation_runs",
            "okx_demo_exchange_events",
            "okx_demo_order_snapshots",
            "okx_demo_fill_snapshots",
            "okx_demo_position_snapshots",
            "okx_demo_account_snapshots",
            "okx_demo_reconciliation_states",
            "okx_demo_recovery_batches",
            "okx_demo_recovery_grants",
        ):
            (
                owner,
                can_select,
                can_insert,
                can_update,
                can_use_unsafe_dml,
                public_privilege,
                unexpected_writer,
            ) = writer_tables.get(
                table_name,
                (None, False, False, False, True, True, True),
            )
            if owner != "freqtrade_ai_attestor":
                problems.append(
                    "writer table owner mismatch: {} owner={}".format(
                        table_name,
                        owner or "<missing>",
                    )
                )
            update_required = table_name in {
                "okx_order_writer_leases",
                "okx_order_write_attempts",
            }
            insert_required = table_name not in {
                "execution_scopes",
                "trade_intents",
                "risk_decisions",
                "approved_executions",
                "exchange_orders",
                "exchange_fills",
                "exchange_positions",
            }
            if not (
                can_select
                and can_insert is insert_required
                and can_update is update_required
            ):
                problems.append(
                    "runtime writer DML privilege missing: {}".format(
                        table_name
                    )
                )
            if can_use_unsafe_dml:
                problems.append(
                    "runtime writer has unsafe DML privilege: {}".format(
                        table_name
                    )
                )
            if public_privilege:
                problems.append(
                    "PUBLIC writer table privilege is not revoked: {}".format(
                        table_name
                    )
                )
            if unexpected_writer:
                problems.append(
                    "unexpected role has writer table DML: {}".format(
                        table_name
                    )
                )
        writer_column_rows = bind.execute(
            text(
                """
                SELECT relation.relname, attribute.attname,
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(attribute.attacl) AS acl
                           WHERE acl.grantee = (
                               SELECT oid FROM pg_roles
                               WHERE rolname = 'freqtrade'
                           )
                             AND acl.privilege_type IN (
                                 'INSERT', 'UPDATE', 'REFERENCES'
                             )
                             AND NOT (
                                 relation.relname =
                                     'okx_demo_recovery_grants'
                                 AND attribute.attname IN (
                                     'status', 'consumed_at'
                                 )
                                 AND acl.privilege_type = 'UPDATE'
                             )
                             AND NOT (
                                 relation.relname = 'exchange_orders'
                                 AND (
                                     (
                                         attribute.attname IN (
                                             'execution_target_id',
                                             'trade_intent_id',
                                             'client_order_id',
                                             'exchange_order_id',
                                             'status',
                                             'request_snapshot',
                                             'response_snapshot'
                                         )
                                         AND acl.privilege_type = 'INSERT'
                                     )
                                     OR (
                                         attribute.attname IN (
                                             'exchange_order_id',
                                             'status',
                                             'response_snapshot',
                                             'updated_at'
                                         )
                                         AND acl.privilege_type = 'UPDATE'
                                     )
                                 )
                             )
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(attribute.attacl) AS acl
                           WHERE acl.grantee = 0
                             AND acl.privilege_type IN (
                                 'INSERT', 'UPDATE', 'REFERENCES'
                             )
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(attribute.attacl) AS acl
                           WHERE acl.grantee NOT IN (
                               0,
                               relation.relowner,
                               (SELECT oid FROM pg_roles
                                WHERE rolname = 'freqtrade')
                           )
                             AND acl.privilege_type IN (
                                 'INSERT', 'UPDATE', 'REFERENCES'
                             )
                       )
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = relation.oid
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
                WHERE namespace.nspname = :schema_name
                  AND relation.relname IN (
                      'execution_scopes',
                      'trade_intents',
                      'risk_decisions',
                      'approved_executions',
                      'exchange_orders',
                      'exchange_fills',
                      'exchange_positions',
                      'okx_order_writer_leases',
                      'okx_order_write_attempts',
                      'reconciliation_runs',
                      'okx_demo_exchange_events',
                      'okx_demo_order_snapshots',
                      'okx_demo_fill_snapshots',
                      'okx_demo_position_snapshots',
                      'okx_demo_account_snapshots',
                      'okx_demo_reconciliation_states',
                      'okx_demo_recovery_batches',
                      'okx_demo_recovery_grants'
                  )
                """
            ),
            {"schema_name": schema_name},
        ).all()
        for (
            table_name,
            column_name,
            runtime_column_dml,
            public_column_dml,
            unexpected_column_dml,
        ) in writer_column_rows:
            if runtime_column_dml:
                problems.append(
                    "runtime writer column DML is not revoked: {}.{}".format(
                        table_name,
                        column_name,
                    )
                )
            if public_column_dml:
                problems.append(
                    "PUBLIC writer column DML is not revoked: {}.{}".format(
                        table_name,
                        column_name,
                    )
                )
            if unexpected_column_dml:
                problems.append(
                    "unexpected role has writer column DML: {}.{}".format(
                        table_name,
                        column_name,
                    )
                )
        required_exchange_order_column_privileges = {
            ("execution_target_id", "INSERT"),
            ("trade_intent_id", "INSERT"),
            ("client_order_id", "INSERT"),
            ("exchange_order_id", "INSERT"),
            ("status", "INSERT"),
            ("request_snapshot", "INSERT"),
            ("response_snapshot", "INSERT"),
            ("exchange_order_id", "UPDATE"),
            ("status", "UPDATE"),
            ("response_snapshot", "UPDATE"),
            ("updated_at", "UPDATE"),
        }
        for column_name, privilege in sorted(
            required_exchange_order_column_privileges
        ):
            if not bind.execute(
                text(
                    "SELECT has_column_privilege("
                    "'freqtrade', 'exchange_orders', :column_name, "
                    ":privilege)"
                ),
                {"column_name": column_name, "privilege": privilege},
            ).scalar_one():
                problems.append(
                    "exchange order column privilege missing: {} {}".format(
                        column_name,
                        privilege,
                    )
                )
        writer_sequence = bind.execute(
            text(
                """
                SELECT relation.relname, owner.rolname,
                       has_sequence_privilege(
                           'freqtrade', relation.oid, 'USAGE'
                       ),
                       has_sequence_privilege(
                           'freqtrade', relation.oid, 'SELECT'
                       ),
                       has_sequence_privilege(
                           'freqtrade', relation.oid, 'UPDATE'
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(
                               COALESCE(
                                   relation.relacl,
                                   acldefault('S', relation.relowner)
                               )
                           ) AS acl
                           WHERE acl.grantee = 0
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(
                               COALESCE(
                                   relation.relacl,
                                   acldefault('S', relation.relowner)
                               )
                           ) AS acl
                           WHERE acl.grantee NOT IN (
                               0,
                               relation.relowner,
                               (SELECT oid FROM pg_roles
                                WHERE rolname = 'freqtrade')
                           )
                       )
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_roles AS owner ON owner.oid = relation.relowner
                WHERE namespace.nspname = :schema_name
                  AND relation.relkind = 'S'
                  AND relation.relname =
                      'okx_order_write_attempts_id_seq'
                """
            ),
            {"schema_name": schema_name},
        ).first()
        (
            sequence_name,
            sequence_owner,
            sequence_usage,
            sequence_select,
            sequence_update,
            sequence_public,
            sequence_unexpected,
        ) = writer_sequence or (
            None,
            None,
            False,
            False,
            True,
            True,
            True,
        )
        if (
            sequence_name is None
            or sequence_owner != "freqtrade_ai_attestor"
            or not sequence_usage
            or not sequence_select
            or sequence_update
            or sequence_public
            or sequence_unexpected
        ):
            problems.append("writer attempt sequence ACL mismatch")
        reconciliation_sequence_names = {
            "exchange_orders_id_seq",
            "reconciliation_runs_id_seq",
            "okx_demo_exchange_events_database_id_seq",
            "okx_demo_order_snapshots_database_id_seq",
            "okx_demo_fill_snapshots_database_id_seq",
            "okx_demo_position_snapshots_database_id_seq",
            "okx_demo_account_snapshots_database_id_seq",
            "okx_demo_reconciliation_states_database_id_seq",
            "okx_demo_recovery_batches_database_id_seq",
            "okx_demo_recovery_grants_database_id_seq",
        }
        reconciliation_sequences = bind.execute(
            text(
                """
                SELECT relation.relname, owner.rolname,
                       has_sequence_privilege(
                           'freqtrade', relation.oid, 'USAGE'
                       ),
                       has_sequence_privilege(
                           'freqtrade', relation.oid, 'SELECT'
                       ),
                       has_sequence_privilege(
                           'freqtrade', relation.oid, 'UPDATE'
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(
                               COALESCE(
                                   relation.relacl,
                                   acldefault('S', relation.relowner)
                               )
                           ) AS acl
                           WHERE acl.grantee = 0
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(
                               COALESCE(
                                   relation.relacl,
                                   acldefault('S', relation.relowner)
                               )
                           ) AS acl
                           WHERE acl.grantee NOT IN (
                               0,
                               relation.relowner,
                               (SELECT oid FROM pg_roles
                                WHERE rolname = 'freqtrade')
                           )
                       )
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_roles AS owner ON owner.oid = relation.relowner
                WHERE namespace.nspname = :schema_name
                  AND relation.relkind = 'S'
                  AND relation.relname::text =
                      ANY(CAST(:sequence_names AS text[]))
                """
            ),
            {
                "schema_name": schema_name,
                "sequence_names": sorted(reconciliation_sequence_names),
            },
        ).all()
        reconciliation_sequence_acl = {
            name: (
                owner,
                usage,
                can_select,
                can_update,
                public_privilege,
                unexpected_privilege,
            )
            for (
                name,
                owner,
                usage,
                can_select,
                can_update,
                public_privilege,
                unexpected_privilege,
            )
            in reconciliation_sequences
        }
        for expected_name in sorted(reconciliation_sequence_names):
            if reconciliation_sequence_acl.get(expected_name) != (
                "freqtrade_ai_attestor",
                True,
                True,
                False,
                False,
                False,
            ):
                problems.append(
                    "reconciliation sequence ACL mismatch: " + expected_name
                )
        secured_functions = bind.execute(
            text(
                """
                SELECT function.proname, owner.rolname, function.prosecdef,
                       function.proconfig,
                       has_function_privilege(
                           'freqtrade', function.oid, 'EXECUTE'
                       ),
                       EXISTS (
                           SELECT 1
                           FROM aclexplode(
                               COALESCE(
                                   function.proacl,
                                   acldefault('f', function.proowner)
                               )
                           ) AS acl
                           WHERE acl.grantee = 0
                             AND acl.privilege_type = 'EXECUTE'
                       ),
                       function.prosrc
                FROM pg_proc AS function
                JOIN pg_namespace AS namespace
                  ON namespace.oid = function.pronamespace
                JOIN pg_roles AS owner ON owner.oid = function.proowner
                WHERE namespace.nspname = :schema_name
                  AND function.proname IN (
                      'write_okx_demo_attested_session',
                      'write_okx_demo_trusted_snapshot',
                      'revoke_okx_demo_attested_session',
                      'finalize_okx_demo_reconciliation_run',
                      'apply_okx_demo_reconciliation_gate',
                      'freeze_okx_demo_reconciliation_gate'
                  )
                """
            ),
            {"schema_name": schema_name},
        ).all()
        function_security = {
            name: (
                owner,
                security_definer,
                config or [],
                runtime_can_execute,
                public_can_execute,
                source,
            )
            for (
                name,
                owner,
                security_definer,
                config,
                runtime_can_execute,
                public_can_execute,
                source,
            ) in secured_functions
        }
        expected_function_bodies = {
            "write_okx_demo_attested_session": ATTESTED_SESSION_FUNCTION_BODY,
            "write_okx_demo_trusted_snapshot": TRUSTED_SNAPSHOT_FUNCTION_BODY,
            "revoke_okx_demo_attested_session": REVOKE_SESSION_FUNCTION_BODY,
        }
        for function_name, expected_body in expected_function_bodies.items():
            (
                owner,
                security_definer,
                config,
                runtime_can_execute,
                public_can_execute,
                source,
            ) = function_security.get(
                function_name, (None, False, [], False, True, "")
            )
            if (
                owner != "freqtrade_ai_attestor"
                or security_definer is not True
                or "search_path=pg_catalog" not in config
                or runtime_can_execute is not True
                or public_can_execute is True
            ):
                problems.append(
                    "attestation function boundary mismatch: {}".format(
                        function_name
                    )
                )
            expected_hash = _canonical_function_body(
                expected_body.replace(
                    "SCHEMA_TOKEN",
                    '"{}"'.format(schema_name.replace('"', '""')),
                ),
                schema_name,
            )
            actual_hash = _canonical_function_body(source, schema_name)
            if actual_hash != expected_hash:
                problems.append(
                    "attestation function definition mismatch: {}".format(
                        function_name
                    )
                )
        reconciliation_function_fragments = {
            "finalize_okx_demo_reconciliation_run": (
                "artifact_status <> 'PENDING'",
                "invalid reconciliation run finalization",
                "artifact_status = 'READY'",
            ),
            "apply_okx_demo_reconciliation_gate": (
                "artifact_status <> 'READY'",
                "v_run.database_ids::jsonb -> 'reconciliation_run'",
                "v_batch.complete_streams::jsonb IS DISTINCT FROM",
                "v_batch.high_watermarks::jsonb",
                "opening_frozen = NOT v_unfrozen",
            ),
            "freeze_okx_demo_reconciliation_gate": (
                "p_status NOT IN ('STALE', 'UNKNOWN')",
                "opening_frozen = TRUE",
                "invalid reconciliation freeze transition",
            ),
        }
        for function_name, fragments in reconciliation_function_fragments.items():
            (
                owner,
                security_definer,
                config,
                runtime_can_execute,
                public_can_execute,
                source,
            ) = function_security.get(
                function_name, (None, False, [], False, True, "")
            )
            if (
                owner != "freqtrade_ai_attestor"
                or security_definer is not True
                or "search_path=pg_catalog" not in config
                or runtime_can_execute is not True
                or public_can_execute is True
                or any(fragment not in source for fragment in fragments)
            ):
                problems.append(
                    "reconciliation function boundary mismatch: {}".format(
                        function_name
                    )
                )
    if "freqtrade" in roles:
        problems.extend(_runtime_application_acl_problems(bind, schema_name))
    return problems


def _create_version_table(connection: Connection) -> None:
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (
                version VARCHAR(64) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )


def _current_version(connection: Connection) -> Optional[str]:
    return connection.execute(
        text(
            f"SELECT version FROM {VERSION_TABLE} "
            "ORDER BY applied_at DESC, version DESC LIMIT 1"
        )
    ).scalar_one_or_none()


def _nonempty_tables(connection: Connection, table_names: Iterable[str]) -> list[str]:
    nonempty: list[str] = []
    for table_name in table_names:
        exists = connection.execute(
            text(f'SELECT EXISTS (SELECT 1 FROM "{table_name}" LIMIT 1)')
        ).scalar_one()
        if exists:
            nonempty.append(table_name)
    return nonempty


def _drop_empty_legacy_tables(connection: Connection, table_names: Iterable[str]) -> None:
    # These are application-owned tables. CASCADE is safe only after the empty-table
    # guard above has verified that no user/runtime data can be discarded.
    for table_name in table_names:
        connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))


def _drop_retired_debug_table(connection: Connection) -> None:
    table_name = "debug_mvp_seed_payloads"
    schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
    if table_name not in inspect(connection).get_table_names(schema=schema_name):
        return
    if _nonempty_tables(connection, (table_name,)):
        raise SchemaMigrationBlocked(
            "Retired debug_mvp_seed_payloads contains rows. Reconcile and remove fixture data "
            "before upgrading; no changes were applied."
        )
    connection.execute(text(f'DROP TABLE "{table_name}"'))


def _add_execution_target_lineage(connection: Connection) -> None:
    """Add target lineage without inferring that historical rows belong to OKX Demo."""

    scope_table = Base.metadata.tables["execution_scopes"]
    scope_table.create(bind=connection, checkfirst=True)
    connection.execute(
        text(
            """
            INSERT INTO execution_scopes
                (scope_id, scope_kind, exchange_capable, executable,
                 exchange_writes, order_submission_authorized)
            VALUES
                ('OKX_DEMO', 'EXCHANGE_TARGET', TRUE, FALSE, FALSE, FALSE),
                ('LOCAL_DRY_RUN', 'NON_EXCHANGE', FALSE, TRUE, FALSE, FALSE),
                ('UNKNOWN_LEGACY', 'LEGACY', FALSE, FALSE, FALSE, FALSE)
            ON CONFLICT (scope_id) DO NOTHING
            """
        )
    )
    actual_scope_catalog = {
        tuple(row)
        for row in connection.execute(
            text(
                """
                SELECT scope_id, scope_kind, exchange_capable, executable,
                       exchange_writes, order_submission_authorized
                FROM execution_scopes
                WHERE scope_id IN (
                    'OKX_DEMO', 'LOCAL_DRY_RUN', 'UNKNOWN_LEGACY'
                )
                """
            )
        ).all()
    }
    expected_scope_catalog = {
        ("OKX_DEMO", "EXCHANGE_TARGET", True, False, False, False),
        ("LOCAL_DRY_RUN", "NON_EXCHANGE", False, True, False, False),
        ("UNKNOWN_LEGACY", "LEGACY", False, False, False, False),
    }
    if actual_scope_catalog != expected_scope_catalog:
        raise SchemaMigrationBlocked(
            "Execution scope catalog is missing or contract-mismatched"
        )

    schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
    table_names = set(inspect(connection).get_table_names(schema=schema_name))
    lineage_roots = (
        ("strategy_generation_runs", "strategy_generation_runs_scope_created_idx"),
        ("backtest_runs", "backtest_runs_scope_created_idx"),
        ("research_jobs", "research_jobs_scope_created_idx"),
    )
    for table_name, index_name in lineage_roots:
        if table_name not in table_names:
            continue
        connection.execute(
            text(
                f'ALTER TABLE "{table_name}" '
                "ADD COLUMN IF NOT EXISTS execution_scope_id VARCHAR(64)"
            )
        )
        connection.execute(
            text(
                f'UPDATE "{table_name}" SET execution_scope_id = :legacy '
                "WHERE execution_scope_id IS NULL"
            ),
            {"legacy": "UNKNOWN_LEGACY"},
        )
        connection.execute(
            text(
                f'ALTER TABLE "{table_name}" '
                "ALTER COLUMN execution_scope_id SET NOT NULL"
            )
        )
        fk_name = f"{table_name}_execution_scope_id_fkey"
        foreign_keys = inspect(connection).get_foreign_keys(table_name, schema=schema_name)
        if not any(foreign_key.get("name") == fk_name for foreign_key in foreign_keys):
            connection.execute(
                text(
                    f'ALTER TABLE "{table_name}" ADD CONSTRAINT "{fk_name}" '
                    "FOREIGN KEY (execution_scope_id) "
                    "REFERENCES execution_scopes(scope_id)"
                )
            )
        connection.execute(
            text(
                f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                f'ON "{table_name}" (execution_scope_id, created_at)'
            )
        )

    if "research_jobs" in table_names:
        legacy_unique_columns = {"operation", "idempotency_key_digest"}
        preparer = connection.dialect.identifier_preparer
        for constraint in inspect(connection).get_unique_constraints(
            "research_jobs", schema=schema_name
        ):
            if (
                set(constraint.get("column_names") or ()) == legacy_unique_columns
                and constraint.get("name")
            ):
                quoted_name = preparer.quote(constraint["name"])
                connection.execute(
                    text(
                        "ALTER TABLE research_jobs "
                        f"DROP CONSTRAINT {quoted_name}"
                    )
                )
        unique_names = {
            constraint.get("name")
            for constraint in inspect(connection).get_unique_constraints(
                "research_jobs", schema=schema_name
            )
        }
        if "research_jobs_scope_operation_idempotency_unique" not in unique_names:
            connection.execute(
                text(
                    "ALTER TABLE research_jobs ADD CONSTRAINT "
                    "research_jobs_scope_operation_idempotency_unique "
                    "UNIQUE (execution_scope_id, operation, idempotency_key_digest)"
                )
            )


def _upgrade_early_execution_target_lineage(connection: Connection) -> None:
    """Upgrade the published ``20260727_01`` contract without rebuilding data."""

    connection.execute(
        text(
            "ALTER TABLE execution_scopes "
            "DROP CONSTRAINT IF EXISTS execution_scopes_known_contract_check"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE execution_scopes "
            "ADD COLUMN IF NOT EXISTS exchange_capable BOOLEAN, "
            "ADD COLUMN IF NOT EXISTS order_submission_authorized BOOLEAN"
        )
    )
    connection.execute(
        text(
            """
            UPDATE execution_scopes
            SET exchange_capable = CASE scope_id
                    WHEN 'OKX_DEMO' THEN TRUE
                    ELSE FALSE
                END,
                executable = CASE scope_id
                    WHEN 'LOCAL_DRY_RUN' THEN TRUE
                    ELSE FALSE
                END,
                exchange_writes = FALSE,
                order_submission_authorized = FALSE
            """
        )
    )
    connection.execute(
        text(
            "ALTER TABLE execution_scopes "
            "ALTER COLUMN exchange_capable SET NOT NULL, "
            "ALTER COLUMN order_submission_authorized SET NOT NULL, "
            "ADD CONSTRAINT execution_scopes_known_contract_check CHECK ("
            "scope_id = 'OKX_DEMO' AND scope_kind = 'EXCHANGE_TARGET' "
            "AND exchange_capable = TRUE AND executable = FALSE "
            "AND exchange_writes = FALSE AND order_submission_authorized = FALSE OR "
            "scope_id = 'LOCAL_DRY_RUN' AND scope_kind = 'NON_EXCHANGE' "
            "AND exchange_capable = FALSE AND executable = TRUE "
            "AND exchange_writes = FALSE AND order_submission_authorized = FALSE OR "
            "scope_id = 'UNKNOWN_LEGACY' AND scope_kind = 'LEGACY' "
            "AND exchange_capable = FALSE AND executable = FALSE "
            "AND exchange_writes = FALSE AND order_submission_authorized = FALSE)"
        )
    )

    for table_name in ("trade_intents", "exchange_orders"):
        connection.execute(
            text(
                f"ALTER TABLE {table_name} "
                f"DROP CONSTRAINT IF EXISTS {table_name}_client_order_id_length_check, "
                f"DROP CONSTRAINT IF EXISTS {table_name}_client_order_id_format_check, "
                f"ADD CONSTRAINT {table_name}_client_order_id_format_check "
                "CHECK (client_order_id ~ '^[A-Za-z0-9]{1,32}$')"
            )
        )

    connection.execute(
        text(
            "ALTER TABLE execution_manifests "
            "DROP CONSTRAINT IF EXISTS execution_manifests_legacy_not_executable_check, "
            "DROP CONSTRAINT IF EXISTS execution_manifests_authorization_check"
        )
    )
    connection.execute(
        text(
            "UPDATE execution_manifests SET executable_evidence = FALSE "
            "WHERE execution_scope_id <> 'LOCAL_DRY_RUN' AND executable_evidence = TRUE"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE execution_manifests "
            "ADD CONSTRAINT execution_manifests_authorization_check "
            "CHECK (execution_scope_id = 'LOCAL_DRY_RUN' OR executable_evidence = FALSE)"
        )
    )


def _add_risk_chain(connection: Connection) -> None:
    """Add the #446 risk-chain columns and tables without rewriting old intents."""

    connection.execute(
        text(
            "ALTER TABLE IF EXISTS approved_executions "
            "DROP CONSTRAINT IF EXISTS approved_executions_intent_identity_fkey, "
            "DROP CONSTRAINT IF EXISTS approved_executions_decision_intent_fkey, "
            "DROP CONSTRAINT IF EXISTS approved_executions_payload_identity_fkey"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE trade_intents "
            "ADD COLUMN IF NOT EXISTS intent_id VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS canonical_hash VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS authorization_schema_version "
            "VARCHAR(16) NOT NULL DEFAULT 'LEGACY', "
            "ADD COLUMN IF NOT EXISTS idempotency_key_digest VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS approved_payload_hash VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS policy_digest VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS strategy_id BIGINT REFERENCES strategies(id), "
            "ADD COLUMN IF NOT EXISTS backtest_run_id BIGINT REFERENCES backtest_runs(id), "
            "ADD COLUMN IF NOT EXISTS backtest_result_id BIGINT REFERENCES backtest_results(id), "
            "ADD COLUMN IF NOT EXISTS strategy_score_id BIGINT REFERENCES strategy_scores(id), "
            "ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE trade_intents DROP CONSTRAINT IF EXISTS "
            "trade_intents_intent_id_key, "
            "ADD CONSTRAINT trade_intents_intent_id_key UNIQUE (intent_id)"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE trade_intents DROP CONSTRAINT IF EXISTS "
            "trade_intents_target_idempotency_unique, "
            "ADD CONSTRAINT trade_intents_target_idempotency_unique "
            "UNIQUE (execution_target_id, idempotency_key_digest), "
            "DROP CONSTRAINT IF EXISTS trade_intents_approval_identity_unique, "
            "ADD CONSTRAINT trade_intents_approval_identity_unique "
            "UNIQUE (id, intent_id, client_order_id, status), "
            "DROP CONSTRAINT IF EXISTS trade_intents_approved_payload_unique, "
            "ADD CONSTRAINT trade_intents_approved_payload_unique "
            "UNIQUE (id, authorization_schema_version, canonical_hash, "
            "policy_digest, approved_payload_hash)"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE risk_decisions "
            "ADD COLUMN IF NOT EXISTS authorization_schema_version "
            "VARCHAR(16) NOT NULL DEFAULT 'LEGACY', "
            "ADD COLUMN IF NOT EXISTS policy_digest VARCHAR(64), "
            "DROP CONSTRAINT IF EXISTS risk_decisions_id_intent_unique, "
            "ADD CONSTRAINT risk_decisions_id_intent_unique "
            "UNIQUE (id, trade_intent_id, decision, "
            "authorization_schema_version, policy_digest)"
        )
    )
    Base.metadata.tables["risk_budgets"].create(bind=connection, checkfirst=True)
    Base.metadata.tables["approved_executions"].create(bind=connection, checkfirst=True)


def _harden_risk_chain(connection: Connection) -> None:
    """Make risk failures auditable and enforce authorization invariants in SQL."""

    connection.execute(
        text(
            "ALTER TABLE approved_executions "
            "DROP CONSTRAINT IF EXISTS approved_executions_intent_identity_fkey, "
            "DROP CONSTRAINT IF EXISTS approved_executions_decision_intent_fkey, "
            "DROP CONSTRAINT IF EXISTS approved_executions_payload_identity_fkey"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE trade_intents "
            "ADD COLUMN IF NOT EXISTS authorization_schema_version "
            "VARCHAR(16) NOT NULL DEFAULT 'LEGACY', "
            "ADD COLUMN IF NOT EXISTS approved_payload_hash VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS policy_digest VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS reference_price NUMERIC(36, 18), "
            "ADD COLUMN IF NOT EXISTS leverage NUMERIC(18, 8), "
            "ADD COLUMN IF NOT EXISTS margin_mode VARCHAR(16), "
            "ADD COLUMN IF NOT EXISTS stop_loss NUMERIC(36, 18), "
            "ADD COLUMN IF NOT EXISTS take_profit NUMERIC(36, 18), "
            "ADD COLUMN IF NOT EXISTS reduce_only BOOLEAN, "
            "ALTER COLUMN instrument_id DROP NOT NULL, "
            "ALTER COLUMN side DROP NOT NULL, "
            "ALTER COLUMN position_side DROP NOT NULL, "
            "ALTER COLUMN order_type DROP NOT NULL, "
            "ALTER COLUMN quantity DROP NOT NULL, "
            "DROP CONSTRAINT IF EXISTS trade_intents_approval_identity_unique, "
            "DROP CONSTRAINT IF EXISTS trade_intents_status_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_intent_id_format_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_canonical_hash_format_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_policy_digest_format_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_idempotency_digest_format_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_risk_enum_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_side_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_position_side_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_margin_mode_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_order_type_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_order_combo_check, "
            "ADD CONSTRAINT trade_intents_approval_identity_unique "
            "UNIQUE (id, intent_id, client_order_id, status), "
            "DROP CONSTRAINT IF EXISTS trade_intents_approved_payload_unique, "
            "ADD CONSTRAINT trade_intents_approved_payload_unique "
            "UNIQUE (id, authorization_schema_version, canonical_hash, "
            "policy_digest, approved_payload_hash), "
            "ADD CONSTRAINT trade_intents_status_check CHECK "
            "(status IN ('PENDING_RISK', 'APPROVED', 'REJECTED', 'BLOCKED', 'EXPIRED')), "
            "ADD CONSTRAINT trade_intents_intent_id_format_check CHECK "
            "(intent_id IS NULL OR intent_id ~ '^[0-9a-f]{64}$'), "
            "ADD CONSTRAINT trade_intents_canonical_hash_format_check CHECK "
            "(canonical_hash IS NULL OR canonical_hash ~ '^[0-9a-f]{64}$'), "
            "ADD CONSTRAINT trade_intents_policy_digest_format_check CHECK "
            "(policy_digest IS NULL OR policy_digest ~ '^[0-9a-f]{64}$'), "
            "ADD CONSTRAINT trade_intents_idempotency_digest_format_check CHECK "
            "(idempotency_key_digest IS NULL OR "
            "idempotency_key_digest ~ '^[0-9a-f]{64}$'), "
            "ADD CONSTRAINT trade_intents_side_check CHECK "
            "(policy_digest IS NULL OR side IS NULL OR side IN ('buy', 'sell')), "
            "ADD CONSTRAINT trade_intents_position_side_check CHECK "
            "(policy_digest IS NULL OR position_side IS NULL "
            "OR position_side = 'net'), "
            "ADD CONSTRAINT trade_intents_margin_mode_check CHECK "
            "(policy_digest IS NULL OR margin_mode IS NULL "
            "OR margin_mode = 'isolated'), "
            "ADD CONSTRAINT trade_intents_order_type_check CHECK "
            "(policy_digest IS NULL OR order_type IS NULL "
            "OR order_type IN ('limit', 'market')), "
            "ADD CONSTRAINT trade_intents_order_combo_check CHECK ("
            "policy_digest IS NULL OR order_type IS NULL "
            "OR order_type = 'market' AND limit_price IS NULL "
            "OR order_type = 'limit' AND limit_price > 0)"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE risk_decisions "
            "ADD COLUMN IF NOT EXISTS authorization_schema_version "
            "VARCHAR(16) NOT NULL DEFAULT 'LEGACY', "
            "ADD COLUMN IF NOT EXISTS policy_digest VARCHAR(64), "
            "DROP CONSTRAINT IF EXISTS risk_decisions_id_intent_unique, "
            "DROP CONSTRAINT IF EXISTS risk_decisions_decision_check, "
            "ADD CONSTRAINT risk_decisions_id_intent_unique "
            "UNIQUE (id, trade_intent_id, decision, "
            "authorization_schema_version, policy_digest), "
            "ADD CONSTRAINT risk_decisions_decision_check CHECK "
            "(decision IN ('APPROVED', 'REJECTED', 'BLOCKED', 'EXPIRED'))"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE risk_budgets "
            "DROP CONSTRAINT IF EXISTS risk_budgets_nonnegative_check, "
            "ADD CONSTRAINT risk_budgets_nonnegative_check CHECK "
            "(reserved_notional >= 0 AND approved_positions >= 0)"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE approved_executions "
            "ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE', "
            "ADD COLUMN IF NOT EXISTS decision VARCHAR(32) NOT NULL DEFAULT 'APPROVED', "
            "ADD COLUMN IF NOT EXISTS intent_status VARCHAR(32) NOT NULL DEFAULT 'APPROVED', "
            "ADD COLUMN IF NOT EXISTS reserved_notional NUMERIC(36, 18), "
            "DROP CONSTRAINT IF EXISTS approved_executions_claim_required_check, "
            "DROP CONSTRAINT IF EXISTS approved_executions_status_check, "
            "DROP CONSTRAINT IF EXISTS approved_executions_client_order_id_format_check, "
            "DROP CONSTRAINT IF EXISTS approved_executions_approved_state_check, "
            "DROP CONSTRAINT IF EXISTS approved_executions_reservation_check, "
            "DROP CONSTRAINT IF EXISTS approved_executions_intent_id_format_check, "
            "DROP CONSTRAINT IF EXISTS approved_executions_intent_identity_fkey, "
            "DROP CONSTRAINT IF EXISTS approved_executions_decision_intent_fkey, "
            "ADD CONSTRAINT approved_executions_claim_required_check "
            "CHECK (claim_required = TRUE), "
            "ADD CONSTRAINT approved_executions_status_check "
            "CHECK (status IN ('ACTIVE', 'EXPIRED')), "
            "ADD CONSTRAINT approved_executions_approved_state_check "
            "CHECK (decision = 'APPROVED' AND intent_status = 'APPROVED'), "
            "ADD CONSTRAINT approved_executions_client_order_id_format_check "
            "CHECK (client_order_id ~ '^[A-Za-z0-9]{1,32}$'), "
            "ADD CONSTRAINT approved_executions_intent_id_format_check "
            "CHECK (intent_id ~ '^[0-9a-f]{64}$')"
        )
    )
    connection.execute(
        text(
            "UPDATE approved_executions AS approved "
            "SET reserved_notional = GREATEST("
            "COALESCE(intent.quantity * COALESCE(intent.limit_price, 1), 0.000000000000000001), "
            "0.000000000000000001) "
            "FROM trade_intents AS intent "
            "WHERE approved.trade_intent_id = intent.id "
            "AND approved.reserved_notional IS NULL"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE approved_executions "
            "ALTER COLUMN reserved_notional SET NOT NULL, "
            "ADD CONSTRAINT approved_executions_reservation_check "
            "CHECK (reserved_notional > 0)"
        )
    )


def _add_trusted_snapshot_boundary(connection: Connection) -> None:
    """Upgrade #446 to trusted registry inputs and immutable active approvals."""

    connection.execute(
        text(
            "ALTER TABLE approved_executions "
            "DROP CONSTRAINT IF EXISTS approved_executions_intent_identity_fkey, "
            "DROP CONSTRAINT IF EXISTS approved_executions_decision_intent_fkey, "
            "DROP CONSTRAINT IF EXISTS approved_executions_payload_identity_fkey"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE trade_intents "
            "ADD COLUMN IF NOT EXISTS authorization_schema_version "
            "VARCHAR(16) NOT NULL DEFAULT 'LEGACY', "
            "ADD COLUMN IF NOT EXISTS approved_payload_hash VARCHAR(64)"
        )
    )
    # No schema before 20260727_05 proves trusted-registry ownership or binds
    # the complete approved payload. Existing permissions are therefore
    # revoked and retained only as blocked legacy audit history.
    connection.execute(text("DELETE FROM approved_executions"))
    connection.execute(
        text(
            "UPDATE risk_budgets SET reserved_notional = 0, "
            "approved_positions = 0"
        )
    )
    connection.execute(
        text(
            "UPDATE trade_intents SET "
            "authorization_schema_version = 'LEGACY', "
            "policy_digest = NULL, "
            "approved_payload_hash = NULL, "
            "status = 'BLOCKED'"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE trade_intents "
            "DROP CONSTRAINT IF EXISTS trade_intents_approval_identity_unique, "
            "DROP CONSTRAINT IF EXISTS trade_intents_approved_payload_unique, "
            "DROP CONSTRAINT IF EXISTS trade_intents_status_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_authorization_schema_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_scope_contract_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_side_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_position_side_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_margin_mode_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_order_type_check, "
            "DROP CONSTRAINT IF EXISTS trade_intents_order_combo_check, "
            "ADD CONSTRAINT trade_intents_approval_identity_unique "
            "UNIQUE (id, intent_id, client_order_id, status), "
            "ADD CONSTRAINT trade_intents_approved_payload_unique "
            "UNIQUE (id, authorization_schema_version, canonical_hash, "
            "policy_digest, approved_payload_hash), "
            "ADD CONSTRAINT trade_intents_status_check CHECK "
            "(status IN ('UNKNOWN_LEGACY', 'PENDING_RISK', 'APPROVED', "
            "'REJECTED', 'BLOCKED', 'EXPIRED')), "
            "ADD CONSTRAINT trade_intents_authorization_schema_check CHECK "
            "(authorization_schema_version IN ('LEGACY', 'RISK_V1')), "
            "ADD CONSTRAINT trade_intents_scope_contract_check CHECK ("
            "authorization_" "schema_version = 'LEGACY' "
            "AND policy_digest IS NULL "
            "AND status IN ('UNKNOWN_LEGACY', 'BLOCKED') "
            "OR authorization_" "schema_version = 'RISK_V1' "
            "AND policy_digest IS NOT NULL "
            "AND canonical_hash IS NOT NULL "
            "AND idempotency_key_digest IS NOT NULL "
            "AND intent_id IS NOT NULL), "
            "ADD CONSTRAINT trade_intents_side_check CHECK ("
            "authorization_" "schema_version = 'LEGACY' OR status = 'BLOCKED' "
            "OR side IN ('buy', 'sell')), "
            "ADD CONSTRAINT trade_intents_position_side_check CHECK ("
            "authorization_" "schema_version = 'LEGACY' OR status = 'BLOCKED' "
            "OR position_side = 'long' AND "
            "(side = 'buy' AND reduce_only = FALSE OR "
            "side = 'sell' AND reduce_only = TRUE) "
            "OR position_side = 'short' AND "
            "(side = 'sell' AND reduce_only = FALSE OR "
            "side = 'buy' AND reduce_only = TRUE)), "
            "ADD CONSTRAINT trade_intents_margin_mode_check CHECK ("
            "authorization_" "schema_version = 'LEGACY' OR status = 'BLOCKED' "
            "OR margin_mode = 'isolated'), "
            "ADD CONSTRAINT trade_intents_order_type_check CHECK ("
            "authorization_" "schema_version = 'LEGACY' OR status = 'BLOCKED' "
            "OR order_type IN ('limit', 'market')), "
            "ADD CONSTRAINT trade_intents_order_combo_check CHECK ("
            "authorization_" "schema_version = 'LEGACY' OR status = 'BLOCKED' "
            "OR order_type = 'market' AND limit_price IS NULL "
            "OR order_type = 'limit' AND limit_price > 0)"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE risk_decisions "
            "ADD COLUMN IF NOT EXISTS authorization_schema_version "
            "VARCHAR(16) NOT NULL DEFAULT 'LEGACY', "
            "ADD COLUMN IF NOT EXISTS policy_digest VARCHAR(64)"
        )
    )
    connection.execute(
        text(
            "UPDATE risk_decisions AS decision SET "
            "authorization_schema_version = intent.authorization_schema_version, "
            "policy_digest = intent.policy_digest, "
            "decision = CASE WHEN intent.authorization_" "schema_version = 'LEGACY' "
            "THEN 'BLOCKED' ELSE decision.decision END "
            "FROM trade_intents AS intent "
            "WHERE decision.trade_intent_id = intent.id"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE risk_decisions "
            "DROP CONSTRAINT IF EXISTS risk_decisions_id_intent_unique, "
            "DROP CONSTRAINT IF EXISTS risk_decisions_authorization_schema_check, "
            "DROP CONSTRAINT IF EXISTS risk_decisions_policy_digest_format_check, "
            "ADD CONSTRAINT risk_decisions_id_intent_unique "
            "UNIQUE (id, trade_intent_id, decision, "
            "authorization_schema_version, policy_digest), "
            "ADD CONSTRAINT risk_decisions_authorization_schema_check CHECK ("
            "authorization_" "schema_version = 'RISK_V1' OR "
            "authorization_" "schema_version = 'LEGACY' AND decision = 'BLOCKED'), "
            "ADD CONSTRAINT risk_decisions_policy_digest_format_check CHECK ("
            "policy_digest IS NULL OR policy_digest ~ '^[0-9a-f]{64}$')"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE approved_executions "
            "ADD COLUMN IF NOT EXISTS authorization_schema_version VARCHAR(16), "
            "ADD COLUMN IF NOT EXISTS canonical_hash VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS policy_digest VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS approved_payload_hash VARCHAR(64)"
        )
    )
    connection.execute(
        text(
            "UPDATE approved_executions AS approved SET "
            "authorization_schema_version = intent.authorization_schema_version, "
            "canonical_hash = intent.canonical_hash, "
            "policy_digest = intent.policy_digest, "
            "approved_payload_hash = intent.approved_payload_hash "
            "FROM trade_intents AS intent "
            "WHERE approved.trade_intent_id = intent.id"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE approved_executions "
            "ALTER COLUMN authorization_schema_version SET NOT NULL, "
            "ALTER COLUMN canonical_hash SET NOT NULL, "
            "ALTER COLUMN policy_digest SET NOT NULL, "
            "ALTER COLUMN approved_payload_hash SET NOT NULL, "
            "DROP CONSTRAINT IF EXISTS approved_executions_authorization_schema_check, "
            "DROP CONSTRAINT IF EXISTS approved_executions_canonical_hash_format_check, "
            "DROP CONSTRAINT IF EXISTS approved_executions_policy_digest_format_check, "
            "DROP CONSTRAINT IF EXISTS approved_executions_payload_hash_format_check, "
            "ADD CONSTRAINT approved_executions_authorization_schema_check "
            "CHECK (authorization_" "schema_version = 'RISK_V1'), "
            "ADD CONSTRAINT approved_executions_canonical_hash_format_check "
            "CHECK (canonical_hash ~ '^[0-9a-f]{64}$'), "
            "ADD CONSTRAINT approved_executions_policy_digest_format_check "
            "CHECK (policy_digest ~ '^[0-9a-f]{64}$'), "
            "ADD CONSTRAINT approved_executions_payload_hash_format_check "
            "CHECK (approved_payload_hash ~ '^[0-9a-f]{64}$'), "
            "ADD CONSTRAINT approved_executions_intent_identity_fkey "
            "FOREIGN KEY (trade_intent_id, intent_id, client_order_id, intent_status) "
            "REFERENCES trade_intents(id, intent_id, client_order_id, status) "
            "ON DELETE CASCADE, "
            "ADD CONSTRAINT approved_executions_decision_intent_fkey "
            "FOREIGN KEY (risk_decision_id, trade_intent_id, decision, "
            "authorization_schema_version, policy_digest) "
            "REFERENCES risk_decisions(id, trade_intent_id, decision, "
            "authorization_schema_version, policy_digest) ON DELETE CASCADE, "
            "ADD CONSTRAINT approved_executions_payload_identity_fkey "
            "FOREIGN KEY (trade_intent_id, authorization_schema_version, "
            "canonical_hash, policy_digest, approved_payload_hash) "
            "REFERENCES trade_intents(id, authorization_schema_version, "
            "canonical_hash, policy_digest, approved_payload_hash) ON DELETE CASCADE"
        )
    )
    Base.metadata.tables["okx_demo_attested_sessions"].create(
        bind=connection, checkfirst=True
    )
    Base.metadata.tables["okx_demo_trusted_snapshots"].create(
        bind=connection, checkfirst=True
    )
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION prevent_active_approved_intent_mutation()
            RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM approved_executions
                    WHERE trade_intent_id = OLD.id AND status = 'ACTIVE'
                ) AND (
                    OLD.authorization_schema_version IS DISTINCT FROM NEW.authorization_schema_version
                    OR OLD.canonical_hash IS DISTINCT FROM NEW.canonical_hash
                    OR OLD.policy_digest IS DISTINCT FROM NEW.policy_digest
                    OR OLD.approved_payload_hash IS DISTINCT FROM NEW.approved_payload_hash
                    OR OLD.idempotency_key_digest IS DISTINCT FROM NEW.idempotency_key_digest
                    OR OLD.client_order_id IS DISTINCT FROM NEW.client_order_id
                    OR OLD.strategy_id IS DISTINCT FROM NEW.strategy_id
                    OR OLD.strategy_version_id IS DISTINCT FROM NEW.strategy_version_id
                    OR OLD.backtest_run_id IS DISTINCT FROM NEW.backtest_run_id
                    OR OLD.backtest_result_id IS DISTINCT FROM NEW.backtest_result_id
                    OR OLD.strategy_score_id IS DISTINCT FROM NEW.strategy_score_id
                    OR OLD.instrument_id IS DISTINCT FROM NEW.instrument_id
                    OR OLD.side IS DISTINCT FROM NEW.side
                    OR OLD.position_side IS DISTINCT FROM NEW.position_side
                    OR OLD.order_type IS DISTINCT FROM NEW.order_type
                    OR OLD.quantity IS DISTINCT FROM NEW.quantity
                    OR OLD.limit_price IS DISTINCT FROM NEW.limit_price
                    OR OLD.reference_price IS DISTINCT FROM NEW.reference_price
                    OR OLD.leverage IS DISTINCT FROM NEW.leverage
                    OR OLD.margin_mode IS DISTINCT FROM NEW.margin_mode
                    OR OLD.stop_loss IS DISTINCT FROM NEW.stop_loss
                    OR OLD.take_profit IS DISTINCT FROM NEW.take_profit
                    OR OLD.reduce_only IS DISTINCT FROM NEW.reduce_only
                    OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
                ) THEN
                    RAISE EXCEPTION 'active approved intent is immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            DROP TRIGGER IF EXISTS trade_intents_active_approval_immutable
                ON trade_intents;
            CREATE TRIGGER trade_intents_active_approval_immutable
                BEFORE UPDATE ON trade_intents
                FOR EACH ROW
                EXECUTE FUNCTION prevent_active_approved_intent_mutation();

            CREATE OR REPLACE FUNCTION prevent_trusted_snapshot_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'trusted snapshots are immutable';
            END;
            $$ LANGUAGE plpgsql;
            DROP TRIGGER IF EXISTS okx_demo_trusted_snapshots_immutable
                ON okx_demo_trusted_snapshots;
            CREATE TRIGGER okx_demo_trusted_snapshots_immutable
                BEFORE UPDATE OR DELETE ON okx_demo_trusted_snapshots
                FOR EACH ROW
                EXECUTE FUNCTION prevent_trusted_snapshot_mutation();
            """
        )
    )


def _require_attestation_admin(connection: Connection) -> None:
    is_admin = connection.execute(
        text(
            "SELECT rolsuper OR rolcreaterole FROM pg_roles "
            "WHERE rolname = current_user"
        )
    ).scalar_one()
    if not is_admin:
        raise SchemaMigrationBlocked(
            "Attestation hardening requires a one-time local PostgreSQL administrator."
        )


def _revoke_runtime_attestor_membership(connection: Connection) -> None:
    _require_attestation_admin(connection)
    role_rows = {
        row.rolname: row
        for row in connection.execute(
            text(
                """
                SELECT rolname, rolcanlogin, rolinherit, rolsuper,
                       rolcreaterole, rolcreatedb, rolreplication, rolbypassrls
                FROM pg_roles
                WHERE rolname IN ('freqtrade', 'freqtrade_ai_attestor')
                """
            )
        )
    }
    if set(role_rows) == {"freqtrade", "freqtrade_ai_attestor"}:
        attestor = role_rows["freqtrade_ai_attestor"]
        if (
            attestor.rolcanlogin
            or attestor.rolinherit
            or attestor.rolsuper
            or attestor.rolcreaterole
            or attestor.rolcreatedb
            or attestor.rolreplication
            or attestor.rolbypassrls
        ):
            connection.execute(
                text(
                    "ALTER ROLE freqtrade_ai_attestor "
                    "NOLOGIN NOINHERIT NOSUPERUSER NOCREATEROLE "
                    "NOCREATEDB NOREPLICATION NOBYPASSRLS"
                )
            )
    has_membership = connection.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_auth_members AS membership
                JOIN pg_roles AS granted
                  ON granted.oid = membership.roleid
                JOIN pg_roles AS member
                  ON member.oid = membership.member
                WHERE granted.rolname = 'freqtrade_ai_attestor'
                  AND member.rolname = 'freqtrade'
            )
            """
        )
    ).scalar_one()
    if has_membership:
        connection.execute(
            text("REVOKE freqtrade_ai_attestor FROM freqtrade")
        )


def _revoke_runtime_attestation_column_privileges(
    connection: Connection,
) -> None:
    schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
    quoted_schema = '"{}"'.format(schema_name.replace('"', '""'))
    existing_tables = set(inspect(connection).get_table_names(schema=schema_name))
    runtime_role_names = list(
        connection.execute(
            text(
                """
                WITH RECURSIVE runtime_roles(roleid, rolname, visited) AS (
                    SELECT oid, rolname, ARRAY[oid]::oid[]
                    FROM pg_roles WHERE rolname = 'freqtrade'
                    UNION ALL
                    SELECT membership.roleid, role.rolname,
                           roles.visited || membership.roleid
                    FROM pg_auth_members AS membership
                    JOIN runtime_roles AS roles
                      ON membership.member = roles.roleid
                    JOIN pg_roles AS role ON role.oid = membership.roleid
                    WHERE NOT membership.roleid = ANY(roles.visited)
                )
                SELECT DISTINCT rolname FROM runtime_roles
                WHERE rolname <> 'freqtrade_ai_attestor'
                """
            )
        ).scalars()
    )
    grantees = ["PUBLIC"] + [
        '"{}"'.format(role_name.replace('"', '""'))
        for role_name in runtime_role_names
    ]
    for table_name in (
        "okx_demo_attested_sessions",
        "okx_demo_attestation_secrets",
        "okx_demo_trusted_snapshots",
    ):
        if table_name not in existing_tables:
            continue
        quoted_columns = ", ".join(
            '"{}"'.format(column["name"].replace('"', '""'))
            for column in inspect(connection).get_columns(
                table_name, schema=schema_name
            )
        )
        if not quoted_columns:
            continue
        connection.execute(
            text(
                "REVOKE ALL ({}) ON {}.{} FROM {}; "
                "REVOKE ALL ON {}.{} FROM {}".format(
                    quoted_columns,
                    quoted_schema,
                    table_name,
                    ", ".join(grantees),
                    quoted_schema,
                    table_name,
                    ", ".join(grantees),
                )
            )
        )
        if table_name != "okx_demo_attestation_secrets":
            connection.execute(
                text(
                    "GRANT SELECT ON {}.{} TO freqtrade".format(
                        quoted_schema, table_name
                    )
                )
            )


def _required_attestation_proof_key() -> bytes:
    encoded_key = os.environ.get(ATTESTATION_PROOF_KEY_ENV, "")
    if not re.fullmatch(r"[0-9a-f]{64}", encoded_key):
        raise SchemaMigrationBlocked(
            "{} must contain exactly one lowercase 32-byte hex key for "
            "attestation hardening.".format(ATTESTATION_PROOF_KEY_ENV)
        )
    return bytes.fromhex(encoded_key)


def harden_attestation_access_boundary(engine: Engine) -> None:
    """Converge the proof key and remove unsafe runtime privileges."""

    with engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            raise SchemaMigrationBlocked(
                "Attestation hardening requires PostgreSQL."
            )
        _require_attestation_admin(connection)
        proof_key_bytes = _required_attestation_proof_key()
        current_key = connection.execute(
            text(
                "SELECT hmac_key FROM okx_demo_attestation_secrets "
                "WHERE secret_id IN ('ACTIVE')"
            )
        ).scalar_one_or_none()
        if current_key != proof_key_bytes:
            active_sessions = connection.execute(
                text(
                    "SELECT count(*) FROM okx_demo_attested_sessions "
                    "WHERE revoked_at IS NULL "
                    "AND expires_at > clock_timestamp()"
                )
            ).scalar_one()
            if active_sessions:
                raise SchemaMigrationBlocked(
                    "Attestation proof key rotation is blocked by an active session."
                )
            connection.execute(
                text(
                    "DELETE FROM okx_demo_attestation_secrets "
                    "WHERE secret_id IN ('ACTIVE')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO okx_demo_attestation_secrets "
                    "(secret_id, hmac_key) VALUES ('ACTIVE', :proof_key)"
                ),
                {"proof_key": proof_key_bytes},
            )
        converged = connection.execute(
            text(
                "SELECT hmac_key IS NOT DISTINCT FROM :proof_key "
                "FROM okx_demo_attestation_secrets "
                "WHERE secret_id IN ('ACTIVE')"
            ),
            {"proof_key": proof_key_bytes},
        ).scalar_one_or_none()
        if converged is not True:
            raise SchemaMigrationBlocked(
                "Attestation proof key convergence could not be verified."
            )
        _revoke_runtime_attestor_membership(connection)
        _revoke_runtime_attestation_column_privileges(connection)


def _add_approved_snapshot_lineage(connection: Connection) -> None:
    connection.execute(
        text(
            """
            ALTER TABLE approved_executions
                ADD COLUMN IF NOT EXISTS instrument_snapshot_id VARCHAR(80),
                ADD COLUMN IF NOT EXISTS market_snapshot_id VARCHAR(80),
                ADD COLUMN IF NOT EXISTS account_snapshot_id VARCHAR(80);
            UPDATE approved_executions AS approved
            SET instrument_snapshot_id =
                    intent.request_snapshot->'snapshot_evidence'
                        ->'instrument'->>'snapshot_id',
                market_snapshot_id =
                    intent.request_snapshot->'snapshot_evidence'
                        ->'market'->>'snapshot_id',
                account_snapshot_id =
                    intent.request_snapshot->'snapshot_evidence'
                        ->'account'->>'snapshot_id'
            FROM trade_intents AS intent
            WHERE intent.id = approved.trade_intent_id
              AND (
                  approved.instrument_snapshot_id IS NULL
                  OR approved.market_snapshot_id IS NULL
                  OR approved.account_snapshot_id IS NULL
              );
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM approved_executions AS approved
                    LEFT JOIN okx_demo_trusted_snapshots AS instrument
                      ON instrument.snapshot_id =
                         approved.instrument_snapshot_id
                     AND instrument.kind = 'instrument'
                    LEFT JOIN okx_demo_trusted_snapshots AS market
                      ON market.snapshot_id = approved.market_snapshot_id
                     AND market.kind = 'market'
                    LEFT JOIN okx_demo_trusted_snapshots AS account
                      ON account.snapshot_id = approved.account_snapshot_id
                     AND account.kind = 'account'
                    WHERE instrument.snapshot_id IS NULL
                       OR market.snapshot_id IS NULL
                       OR account.snapshot_id IS NULL
                ) THEN
                    RAISE EXCEPTION
                        'approved execution snapshot lineage is incomplete';
                END IF;
            END
            $$;
            ALTER TABLE approved_executions
                ALTER COLUMN instrument_snapshot_id SET NOT NULL,
                ALTER COLUMN market_snapshot_id SET NOT NULL,
                ALTER COLUMN account_snapshot_id SET NOT NULL,
                DROP CONSTRAINT IF EXISTS
                    approved_executions_instrument_snapshot_fkey,
                DROP CONSTRAINT IF EXISTS
                    approved_executions_market_snapshot_fkey,
                DROP CONSTRAINT IF EXISTS
                    approved_executions_account_snapshot_fkey,
                ADD CONSTRAINT approved_executions_instrument_snapshot_fkey
                    FOREIGN KEY (instrument_snapshot_id)
                    REFERENCES okx_demo_trusted_snapshots(snapshot_id)
                    ON DELETE RESTRICT,
                ADD CONSTRAINT approved_executions_market_snapshot_fkey
                    FOREIGN KEY (market_snapshot_id)
                    REFERENCES okx_demo_trusted_snapshots(snapshot_id)
                    ON DELETE RESTRICT,
                ADD CONSTRAINT approved_executions_account_snapshot_fkey
                    FOREIGN KEY (account_snapshot_id)
                    REFERENCES okx_demo_trusted_snapshots(snapshot_id)
                    ON DELETE RESTRICT;
            """
        )
    )


def _add_attested_session_boundary(connection: Connection) -> None:
    """Install the HMAC-rooted, least-privilege attestation boundary."""

    schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
    quoted_schema = '"{}"'.format(schema_name.replace('"', '""'))
    _require_attestation_admin(connection)
    proof_key_bytes = _required_attestation_proof_key()
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public"))
    connection.execute(
        text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'freqtrade_ai_attestor'
                ) THEN
                    CREATE ROLE freqtrade_ai_attestor
                        NOLOGIN NOINHERIT NOSUPERUSER NOCREATEROLE
                        NOCREATEDB NOREPLICATION NOBYPASSRLS;
                END IF;
            END
            $$;
            """
        )
    )
    _revoke_runtime_attestor_membership(connection)
    _revoke_runtime_attestation_column_privileges(connection)
    connection.execute(
        text(
            "DROP TRIGGER IF EXISTS okx_demo_trusted_snapshots_immutable "
            "ON okx_demo_trusted_snapshots"
        )
    )
    connection.execute(text("DELETE FROM approved_executions"))
    connection.execute(
        text(
            "UPDATE risk_budgets SET reserved_notional = 0, "
            "approved_positions = 0"
        )
    )
    connection.execute(text("DELETE FROM okx_demo_trusted_snapshots"))
    Base.metadata.tables["okx_demo_attested_sessions"].create(
        bind=connection, checkfirst=True
    )
    Base.metadata.tables["okx_demo_attestation_secrets"].create(
        bind=connection, checkfirst=True
    )
    connection.execute(text("DELETE FROM okx_demo_attested_sessions"))
    connection.execute(
        text(
            "ALTER TABLE okx_demo_attested_sessions "
            "ADD COLUMN IF NOT EXISTS attestation_nonce VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS revoke_reason VARCHAR(32), "
            "DROP CONSTRAINT IF EXISTS okx_demo_attested_sessions_nonce_unique, "
            "DROP CONSTRAINT IF EXISTS okx_demo_attested_sessions_time_check, "
            "ALTER COLUMN attestation_nonce SET NOT NULL, "
            "ADD CONSTRAINT okx_demo_attested_sessions_nonce_unique "
            "UNIQUE (attestation_nonce), "
            "ADD CONSTRAINT okx_demo_attested_sessions_time_check CHECK ("
            "created_at < expires_at AND ("
            "revoked_at IS NULL AND revoke_reason IS NULL OR "
            "revoked_at >= created_at AND revoke_reason IN ("
            "'IDENTITY_DRIFT', 'EXPIRED', 'FACTORY_CLOSE', 'WRITE_FAILURE')))"
        )
    )
    _add_approved_snapshot_lineage(connection)
    connection.execute(
        text(
            "INSERT INTO okx_demo_attestation_secrets (secret_id, hmac_key) "
            "VALUES ('ACTIVE', :proof_key_bytes) "
            "ON CONFLICT (secret_id) DO UPDATE SET hmac_key = EXCLUDED.hmac_key"
        ),
        {"proof_key_bytes": proof_key_bytes},
    )
    connection.execute(
        text(
            "ALTER TABLE okx_demo_trusted_snapshots "
            "ADD COLUMN IF NOT EXISTS attested_session_expires_at TIMESTAMPTZ, "
            "DROP CONSTRAINT IF EXISTS okx_demo_trusted_snapshots_session_fkey, "
            "DROP CONSTRAINT IF EXISTS okx_demo_trusted_snapshots_time_check"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE okx_demo_trusted_snapshots "
            "ALTER COLUMN attested_session_expires_at SET NOT NULL, "
            "ADD CONSTRAINT okx_demo_trusted_snapshots_time_check CHECK ("
            "observed_at < expires_at AND expires_at <= attested_session_expires_at), "
            "ADD CONSTRAINT okx_demo_trusted_snapshots_session_fkey FOREIGN KEY ("
            "attested_session_id, execution_target_id, "
            "attestation_fingerprint_sha256, attested_session_expires_at) "
            "REFERENCES okx_demo_attested_sessions("
            "session_id, execution_target_id, pinned_fingerprint_sha256, expires_at) "
            "ON DELETE RESTRICT"
        )
    )
    functions_sql = """
        DROP FUNCTION IF EXISTS SCHEMA_TOKEN.write_okx_demo_attested_session(
            text,text,text,text,timestamptz,timestamptz
        );
        CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.write_okx_demo_attested_session(
            p_session_id text,
            p_target text,
            p_fingerprint text,
            p_created_micros bigint,
            p_expires_micros bigint,
            p_nonce text,
            p_signature text
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$SESSION_BODY$$;

        CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.write_okx_demo_trusted_snapshot(
            p_session_id text,
            p_proof text,
            p_snapshot_id text,
            p_kind text,
            p_content jsonb,
            p_digest text,
            p_observed_at timestamptz,
            p_expires_at timestamptz
        ) RETURNS bigint
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$SNAPSHOT_BODY$$;

        CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.revoke_okx_demo_attested_session(
            p_session_id text,
            p_signature text,
            p_reason text,
            p_revoked_micros bigint
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$REVOKE_BODY$$;
    """
    functions_sql = (
        functions_sql.replace("SESSION_BODY", ATTESTED_SESSION_FUNCTION_BODY)
        .replace("SNAPSHOT_BODY", TRUSTED_SNAPSHOT_FUNCTION_BODY)
        .replace("REVOKE_BODY", REVOKE_SESSION_FUNCTION_BODY)
        .replace("SCHEMA_TOKEN", quoted_schema)
    )
    connection.execute(text(functions_sql))
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION prevent_trusted_snapshot_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'trusted snapshots are immutable';
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER okx_demo_trusted_snapshots_immutable
                BEFORE UPDATE OR DELETE ON okx_demo_trusted_snapshots
                FOR EACH ROW
                EXECUTE FUNCTION prevent_trusted_snapshot_mutation();
            """
        )
    )
    for table_name in (
        "okx_demo_attested_sessions",
        "okx_demo_attestation_secrets",
        "okx_demo_trusted_snapshots",
    ):
        quoted_columns = ", ".join(
            '"{}"'.format(column.name.replace('"', '""'))
            for column in Base.metadata.tables[table_name].columns
        )
        connection.execute(
            text(
                "ALTER TABLE {}.{} OWNER TO freqtrade_ai_attestor".format(
                    quoted_schema, table_name
                )
            )
        )
        connection.execute(
            text(
                "REVOKE ALL ({}) ON {}.{} FROM PUBLIC, freqtrade; "
                "REVOKE ALL ON {}.{} FROM PUBLIC, freqtrade".format(
                    quoted_columns,
                    quoted_schema,
                    table_name,
                    quoted_schema, table_name
                )
            )
        )
        if table_name != "okx_demo_attestation_secrets":
            connection.execute(
                text(
                    "GRANT SELECT ON {}.{} TO freqtrade".format(
                        quoted_schema, table_name
                    )
                )
            )
    connection.execute(
        text(
            "GRANT USAGE ON SCHEMA {} TO freqtrade, freqtrade_ai_attestor; "
            "ALTER FUNCTION {}.write_okx_demo_attested_session("
            "text,text,text,bigint,bigint,text,text) "
            "OWNER TO freqtrade_ai_attestor; "
            "ALTER FUNCTION {}.write_okx_demo_trusted_snapshot("
            "text,text,text,text,jsonb,text,timestamptz,timestamptz) "
            "OWNER TO freqtrade_ai_attestor; "
            "ALTER FUNCTION {}.revoke_okx_demo_attested_session("
            "text,text,text,bigint) OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON FUNCTION {}.write_okx_demo_attested_session("
            "text,text,text,bigint,bigint,text,text) FROM PUBLIC; "
            "REVOKE ALL ON FUNCTION {}.write_okx_demo_trusted_snapshot("
            "text,text,text,text,jsonb,text,timestamptz,timestamptz) FROM PUBLIC; "
            "REVOKE ALL ON FUNCTION {}.revoke_okx_demo_attested_session("
            "text,text,text,bigint) FROM PUBLIC; "
            "GRANT EXECUTE ON FUNCTION {}.write_okx_demo_attested_session("
            "text,text,text,bigint,bigint,text,text) TO freqtrade; "
            "GRANT EXECUTE ON FUNCTION {}.write_okx_demo_trusted_snapshot("
            "text,text,text,text,jsonb,text,timestamptz,timestamptz) TO freqtrade; "
            "GRANT EXECUTE ON FUNCTION {}.revoke_okx_demo_attested_session("
            "text,text,text,bigint) TO freqtrade".format(
                quoted_schema,
                quoted_schema,
                quoted_schema,
                quoted_schema,
                quoted_schema,
                quoted_schema,
                quoted_schema,
                quoted_schema,
                quoted_schema,
                quoted_schema,
            )
        )
    )


def _add_order_writer(connection: Connection) -> None:
    """Add the #447 single-writer lease and durable write-attempt journal."""

    schema_name, effective_schemas = connection.execute(
        text("SELECT current_schema(), current_schemas(false)")
    ).one()
    if not schema_name or list(effective_schemas or ()) != [schema_name]:
        raise SchemaMigrationBlocked(
            "Order-writer migration requires exactly one effective schema"
        )
    quote = connection.dialect.identifier_preparer.quote
    for table_name in ("okx_order_write_attempts", "okx_order_writer_leases"):
        qualified_table = "{}.{}".format(quote(schema_name), quote(table_name))
        exists = connection.execute(
            text("SELECT to_regclass(:table_name) IS NOT NULL"),
            {"table_name": qualified_table},
        ).scalar_one()
        if exists:
            row_count = connection.execute(
                text("SELECT count(*) FROM {}".format(qualified_table))
            ).scalar_one()
            if row_count:
                raise SchemaMigrationBlocked(
                    "Refusing to replace non-empty pre-release writer table: "
                    + table_name
                )
            try:
                connection.execute(text("DROP TABLE {}".format(qualified_table)))
            except SQLAlchemyError as exc:
                raise SchemaMigrationBlocked(
                    "Refusing to drop pre-release writer table with dependencies: "
                    + table_name
                ) from exc
    # Legacy worker-only schemas do not contain the #442 exchange-order parent
    # yet.  Create it before the writer journal so PostgreSQL can resolve the
    # journal's foreign key during the same atomic upgrade.
    Base.metadata.tables["exchange_orders"].create(
        bind=connection,
        checkfirst=True,
    )
    Base.metadata.tables["okx_order_writer_leases"].create(
        bind=connection,
        checkfirst=True,
    )
    Base.metadata.tables["okx_order_write_attempts"].create(
        bind=connection,
        checkfirst=True,
    )
    quoted_schema = quote(schema_name)
    connection.execute(
        text(
            "ALTER SCHEMA {} OWNER TO freqtrade_ai_attestor; "
            "REVOKE CREATE ON SCHEMA {} "
            "FROM PUBLIC, freqtrade, freqtrade_ai_attestor; "
            "GRANT USAGE ON SCHEMA {} "
            "TO freqtrade, freqtrade_ai_attestor".format(
                quoted_schema,
                quoted_schema,
                quoted_schema,
            )
        )
    )
    qualified_version_table = "{}.{}".format(
        quoted_schema,
        quote(VERSION_TABLE),
    )
    connection.execute(
        text(
            "REVOKE ALL ON TABLE {} FROM PUBLIC, freqtrade; "
            "GRANT SELECT ON TABLE {} TO freqtrade".format(
                qualified_version_table,
                qualified_version_table,
            )
        )
    )
    for table_name in ("okx_order_writer_leases", "okx_order_write_attempts"):
        qualified_table = "{}.{}".format(quoted_schema, quote(table_name))
        quoted_columns = ", ".join(
            quote(column.name)
            for column in Base.metadata.tables[table_name].columns
        )
        connection.execute(
            text(
                "ALTER TABLE {} OWNER TO freqtrade_ai_attestor; "
                "REVOKE ALL ({}) ON {} FROM PUBLIC, freqtrade; "
                "REVOKE ALL ON TABLE {} FROM PUBLIC, freqtrade; "
                "GRANT SELECT, INSERT, UPDATE ON TABLE {} TO freqtrade".format(
                    qualified_table,
                    quoted_columns,
                    qualified_table,
                    qualified_table,
                    qualified_table,
                )
            )
        )
    sequence_identity = connection.execute(
        text(
            "SELECT namespace.nspname, relation.relname "
            "FROM pg_class AS relation "
            "JOIN pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "WHERE relation.oid = pg_get_serial_sequence("
            ":qualified_table, 'id')::regclass"
        ),
        {
            "qualified_table": "{}.{}".format(
                schema_name,
                "okx_order_write_attempts",
            )
        },
    ).first()
    if sequence_identity is None:
        raise SchemaMigrationBlocked(
            "Order-writer attempt sequence is missing"
        )
    sequence_name = "{}.{}".format(
        quote(sequence_identity[0]),
        quote(sequence_identity[1]),
    )
    connection.execute(
        text(
            "ALTER SEQUENCE {} OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON SEQUENCE {} FROM PUBLIC, freqtrade; "
            "GRANT USAGE, SELECT ON SEQUENCE {} TO freqtrade".format(
                sequence_name,
                sequence_name,
                sequence_name,
            )
        )
    )


def _add_okx_demo_reconciliation(connection: Connection) -> None:
    """Install the append-only #448 evidence and fail-closed opening gate."""

    schema_name, effective_schemas = connection.execute(
        text("SELECT current_schema(), current_schemas(false)")
    ).one()
    if not schema_name or list(effective_schemas or ()) != [schema_name]:
        raise SchemaMigrationBlocked(
            "Reconciliation migration requires exactly one effective schema"
        )
    quote = connection.dialect.identifier_preparer.quote
    quoted_schema = quote(schema_name)
    # Incremental schemas can predate #448 entirely. Create the lineage parent
    # before any state/grant child table that references reconciliation_runs.
    Base.metadata.tables["reconciliation_runs"].create(
        bind=connection,
        checkfirst=True,
    )
    connection.execute(
        text(
            """
            ALTER TABLE reconciliation_runs
                ADD COLUMN IF NOT EXISTS database_ids JSONB NOT NULL
                    DEFAULT '{}'::jsonb,
                ADD COLUMN IF NOT EXISTS artifact_path TEXT,
                ADD COLUMN IF NOT EXISTS artifact_sha256 VARCHAR(64),
                ADD COLUMN IF NOT EXISTS artifact_status VARCHAR(16)
                    NOT NULL DEFAULT 'PENDING',
                ADD COLUMN IF NOT EXISTS authoritative_observed_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS source_type VARCHAR(32) NOT NULL
                    DEFAULT 'api_aggregate',
                ADD COLUMN IF NOT EXISTS core_data BOOLEAN NOT NULL DEFAULT TRUE,
                DROP CONSTRAINT IF EXISTS reconciliation_runs_status_check;
            ALTER TABLE reconciliation_runs
                DROP CONSTRAINT IF EXISTS
                    reconciliation_runs_artifact_status_check;
            UPDATE reconciliation_runs
            SET status = 'UNKNOWN'
            WHERE status NOT IN (
                'RECONCILED', 'DRIFTED', 'STALE', 'UNKNOWN', 'RECOVERED'
            );
            ALTER TABLE reconciliation_runs
                ADD CONSTRAINT reconciliation_runs_status_check CHECK (
                    status IN (
                        'RECONCILED', 'DRIFTED', 'STALE', 'UNKNOWN', 'RECOVERED'
                    )
                ),
                ADD CONSTRAINT reconciliation_runs_artifact_status_check CHECK (
                    artifact_status IN ('PENDING', 'READY')
                );
            """
        )
    )
    for table_name in (
        "okx_demo_recovery_batches",
        "okx_demo_exchange_events",
        "okx_demo_order_snapshots",
        "okx_demo_fill_snapshots",
        "okx_demo_position_snapshots",
        "okx_demo_account_snapshots",
        "okx_demo_reconciliation_states",
        "okx_demo_recovery_grants",
    ):
        Base.metadata.tables[table_name].create(bind=connection, checkfirst=True)
    _add_okx_demo_recovery_batch_index(connection)
    connection.execute(
        text(
            """
            INSERT INTO execution_scopes (
                scope_id, scope_kind, exchange_capable, executable,
                exchange_writes, order_submission_authorized
            ) VALUES
                (
                    'OKX_DEMO', 'EXCHANGE_TARGET',
                    TRUE, FALSE, FALSE, FALSE
                ),
                (
                    'LOCAL_DRY_RUN', 'NON_EXCHANGE',
                    FALSE, TRUE, FALSE, FALSE
                ),
                (
                    'UNKNOWN_LEGACY', 'LEGACY',
                    FALSE, FALSE, FALSE, FALSE
                )
            ON CONFLICT (scope_id) DO NOTHING;
            INSERT INTO okx_demo_reconciliation_states (
                execution_target_id, status, opening_frozen, block_reason
            ) VALUES (
                'OKX_DEMO', 'UNKNOWN', TRUE, 'RECONCILIATION_REQUIRED'
            )
            ON CONFLICT (execution_target_id) DO NOTHING
            """
        )
    )
    actual_scope_catalog = {
        tuple(row)
        for row in connection.execute(
            text(
                """
                SELECT scope_id, scope_kind, exchange_capable, executable,
                       exchange_writes, order_submission_authorized
                FROM execution_scopes
                WHERE scope_id IN (
                    'OKX_DEMO', 'LOCAL_DRY_RUN', 'UNKNOWN_LEGACY'
                )
                """
            )
        ).all()
    }
    expected_scope_catalog = {
        ("OKX_DEMO", "EXCHANGE_TARGET", True, False, False, False),
        ("LOCAL_DRY_RUN", "NON_EXCHANGE", False, True, False, False),
        ("UNKNOWN_LEGACY", "LEGACY", False, False, False, False),
    }
    if actual_scope_catalog != expected_scope_catalog:
        raise SchemaMigrationBlocked(
            "Execution scope catalog is missing or contract-mismatched"
        )
    execution_scopes_table = "{}.{}".format(
        quoted_schema,
        quote("execution_scopes"),
    )
    connection.execute(
        text(
            "ALTER TABLE {} OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON TABLE {} FROM PUBLIC, freqtrade; "
            "GRANT SELECT ON TABLE {} TO freqtrade".format(
                execution_scopes_table,
                execution_scopes_table,
                execution_scopes_table,
            )
        )
    )
    for projection_table_name in (
        "exchange_orders",
        "exchange_fills",
        "exchange_positions",
    ):
        projection_table = "{}.{}".format(
            quoted_schema,
            quote(projection_table_name),
        )
        connection.execute(
            text(
                "ALTER TABLE {} OWNER TO freqtrade_ai_attestor; "
                "REVOKE ALL ON TABLE {} FROM PUBLIC, freqtrade; "
                "GRANT SELECT ON TABLE {} TO freqtrade".format(
                    projection_table,
                    projection_table,
                    projection_table,
                )
            )
        )
        if projection_table_name == "exchange_orders":
            connection.execute(
                text(
                    "GRANT INSERT (execution_target_id, trade_intent_id, "
                    "client_order_id, exchange_order_id, status, request_snapshot, "
                    "response_snapshot) ON {} TO freqtrade; "
                    "GRANT UPDATE (exchange_order_id, status, "
                    "response_snapshot, updated_at) ON {} TO freqtrade".format(
                        projection_table,
                        projection_table,
                    )
                )
            )
    for lineage_table_name in (
        "trade_intents",
        "risk_decisions",
        "approved_executions",
    ):
        lineage_table = "{}.{}".format(
            quoted_schema,
            quote(lineage_table_name),
        )
        connection.execute(
            text(
                "ALTER TABLE {} OWNER TO freqtrade_ai_attestor; "
                "REVOKE ALL ON TABLE {} FROM PUBLIC, freqtrade; "
                "GRANT SELECT ON TABLE {} TO freqtrade".format(
                    lineage_table,
                    lineage_table,
                    lineage_table,
                )
            )
        )
    exchange_orders_sequence = connection.execute(
        text(
            "SELECT pg_get_serial_sequence("
            ":qualified_table, 'id')"
        ),
        {"qualified_table": "{}.exchange_orders".format(schema_name)},
    ).scalar_one()
    if not exchange_orders_sequence:
        raise SchemaMigrationBlocked("Exchange order sequence is missing")
    connection.execute(
        text(
            "ALTER SEQUENCE {} OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON SEQUENCE {} FROM PUBLIC, freqtrade; "
            "GRANT USAGE, SELECT ON SEQUENCE {} TO freqtrade".format(
                exchange_orders_sequence,
                exchange_orders_sequence,
                exchange_orders_sequence,
            )
        )
    )
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION guard_okx_demo_exchange_order()
            RETURNS trigger LANGUAGE plpgsql
            SECURITY DEFINER SET search_path = pg_catalog
            AS $$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.execution_target_id <> 'OKX_DEMO'
                       OR NEW.status <> 'PREPARED'
                       OR NEW.exchange_order_id IS NOT NULL
                       OR json_typeof(NEW.request_snapshot) <> 'object'
                       OR json_typeof(NEW.response_snapshot) <> 'object'
                       OR NOT EXISTS (
                           SELECT 1
                           FROM __SCHEMA__.trade_intents AS intent
                           WHERE intent.id = NEW.trade_intent_id
                             AND intent.execution_target_id = 'OKX_DEMO'
                             AND intent.client_order_id =
                                 NEW.client_order_id
                             AND intent.status = 'APPROVED'
                       )
                    THEN
                        RAISE EXCEPTION
                            'invalid exchange order creation';
                    END IF;
                    RETURN NEW;
                END IF;
                IF OLD.id IS DISTINCT FROM NEW.id
                   OR OLD.execution_target_id
                        IS DISTINCT FROM NEW.execution_target_id
                   OR OLD.trade_intent_id
                        IS DISTINCT FROM NEW.trade_intent_id
                   OR OLD.client_order_id
                        IS DISTINCT FROM NEW.client_order_id
                   OR OLD.request_snapshot::jsonb
                        IS DISTINCT FROM NEW.request_snapshot::jsonb
                   OR OLD.created_at IS DISTINCT FROM NEW.created_at
                   OR (
                       OLD.exchange_order_id IS NOT NULL
                       AND OLD.exchange_order_id
                            IS DISTINCT FROM NEW.exchange_order_id
                   )
                   OR NEW.status NOT IN (
                       'PREPARED', 'ACKNOWLEDGED', 'REJECTED',
                       'RECOVERY_REQUIRED', 'RESIDUAL_CLOSE_REQUIRED',
                       'RECONCILED', 'live', 'partially_filled',
                       'filled', 'canceled', 'mmp_canceled',
                       'position_zero', 'leverage_confirmed'
                   )
                   OR json_typeof(NEW.response_snapshot) <> 'object'
                   OR (
                       OLD.status IN (
                           'REJECTED', 'RECONCILED', 'filled',
                           'canceled', 'mmp_canceled', 'position_zero'
                       )
                       AND NEW.status IS DISTINCT FROM OLD.status
                   )
                THEN
                    RAISE EXCEPTION
                        'invalid exchange order transition';
                END IF;
                RETURN NEW;
            END
            $$;
            ALTER FUNCTION guard_okx_demo_exchange_order()
                OWNER TO freqtrade_ai_attestor;
            REVOKE ALL ON FUNCTION guard_okx_demo_exchange_order()
                FROM PUBLIC, freqtrade;
            DROP TRIGGER IF EXISTS exchange_orders_guard
                ON exchange_orders;
            CREATE TRIGGER exchange_orders_guard
                BEFORE INSERT OR UPDATE ON exchange_orders
                FOR EACH ROW EXECUTE FUNCTION
                    guard_okx_demo_exchange_order();
            """.replace("__SCHEMA__", quoted_schema)
        )
    )
    connection.execute(
        text(
            """
            GRANT SELECT, UPDATE ON __SCHEMA__.risk_budgets
                TO freqtrade_ai_attestor;
            CREATE OR REPLACE FUNCTION release_expired_okx_demo_approval(
                p_approval_id BIGINT
            ) RETURNS BOOLEAN
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = __SCHEMA__, pg_catalog
            AS $$
            DECLARE
                v_approval RECORD;
            BEGIN
                PERFORM pg_advisory_xact_lock(
                    hashtext('OKX_DEMO-risk-budget')
                );
                SELECT approved.*,
                       intent.expires_at AS intent_expires_at
                INTO v_approval
                FROM __SCHEMA__.approved_executions AS approved
                JOIN __SCHEMA__.trade_intents AS intent
                  ON intent.id = approved.trade_intent_id
                WHERE approved.id = p_approval_id
                FOR UPDATE;
                IF NOT FOUND THEN
                    RETURN FALSE;
                END IF;
                IF v_approval.execution_target_id <> 'OKX_DEMO'
                   OR v_approval.status <> 'ACTIVE'
                   OR (
                       v_approval.expires_at > statement_timestamp()
                       AND v_approval.intent_expires_at >
                           statement_timestamp()
                   )
                THEN
                    RAISE EXCEPTION
                        'approval is not safely releasable as expired';
                END IF;
                UPDATE __SCHEMA__.approved_executions
                SET status = 'EXPIRED',
                    evidence_snapshot = jsonb_set(
                        evidence_snapshot::jsonb,
                        '{invalidation_reason}',
                        '"authorization evidence expired"'::jsonb,
                        TRUE
                    )::json
                WHERE id = v_approval.id;
                UPDATE __SCHEMA__.risk_decisions
                SET evidence_snapshot = jsonb_set(
                        evidence_snapshot::jsonb,
                        '{reasons}',
                        '["authorization evidence expired"]'::jsonb,
                        TRUE
                    )::json
                WHERE id = v_approval.risk_decision_id
                  AND execution_target_id = 'OKX_DEMO'
                  AND decision = 'APPROVED';
                UPDATE __SCHEMA__.full_chain_runs
                SET status = 'BLOCKED',
                    terminal_reason = 'authorization evidence expired',
                    completed_at = statement_timestamp()
                WHERE approved_execution_id = v_approval.id
                  AND execution_target_id = 'OKX_DEMO';
                UPDATE __SCHEMA__.risk_budgets
                SET reserved_notional = greatest(
                        0, reserved_notional -
                           v_approval.reserved_notional
                    ),
                    approved_positions = greatest(
                        0, approved_positions - 1
                    )
                WHERE execution_target_id = 'OKX_DEMO';
                RETURN TRUE;
            END
            $$;
            ALTER FUNCTION release_expired_okx_demo_approval(BIGINT)
                OWNER TO freqtrade_ai_attestor;
            REVOKE ALL ON FUNCTION
                release_expired_okx_demo_approval(BIGINT)
                FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION
                release_expired_okx_demo_approval(BIGINT)
                TO freqtrade;
            """.replace("__SCHEMA__", quoted_schema)
        )
    )
    table_names = (
        "reconciliation_runs",
        "okx_demo_exchange_events",
        "okx_demo_order_snapshots",
        "okx_demo_fill_snapshots",
        "okx_demo_position_snapshots",
        "okx_demo_account_snapshots",
        "okx_demo_reconciliation_states",
        "okx_demo_recovery_batches",
        "okx_demo_recovery_grants",
    )
    for table_name in table_names:
        qualified_table = "{}.{}".format(quoted_schema, quote(table_name))
        quoted_columns = ", ".join(
            quote(column.name)
            for column in Base.metadata.tables[table_name].columns
        )
        runtime_privileges = "SELECT, INSERT"
        connection.execute(
            text(
                "ALTER TABLE {} OWNER TO freqtrade_ai_attestor; "
                "REVOKE ALL ({}) ON {} FROM PUBLIC, freqtrade; "
                "REVOKE ALL ON TABLE {} FROM PUBLIC, freqtrade; "
                "GRANT {} ON TABLE {} TO freqtrade".format(
                    qualified_table,
                    quoted_columns,
                    qualified_table,
                    qualified_table,
                    runtime_privileges,
                    qualified_table,
                )
            )
        )
        if table_name == "okx_demo_recovery_grants":
            connection.execute(
                text(
                    "GRANT UPDATE (status, consumed_at) ON {} "
                    "TO freqtrade".format(qualified_table)
                )
            )
        sequence_identity = connection.execute(
            text(
                "SELECT pg_get_serial_sequence(:qualified_table, 'database_id')"
                if table_name != "reconciliation_runs"
                else "SELECT pg_get_serial_sequence(:qualified_table, 'id')"
            ),
            {"qualified_table": "{}.{}".format(schema_name, table_name)},
        ).scalar_one()
        if not sequence_identity:
            raise SchemaMigrationBlocked(
                "Reconciliation sequence is missing: " + table_name
            )
        connection.execute(
            text(
                "ALTER SEQUENCE {} OWNER TO freqtrade_ai_attestor; "
                "REVOKE ALL ON SEQUENCE {} FROM PUBLIC, freqtrade; "
                "GRANT USAGE, SELECT ON SEQUENCE {} TO freqtrade".format(
                    sequence_identity,
                    sequence_identity,
                    sequence_identity,
                )
            )
        )
    immutable_tables = (
        "okx_demo_exchange_events",
        "okx_demo_order_snapshots",
        "okx_demo_fill_snapshots",
        "okx_demo_position_snapshots",
        "okx_demo_account_snapshots",
        "okx_demo_recovery_batches",
    )
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION reject_okx_demo_evidence_mutation()
            RETURNS trigger LANGUAGE plpgsql
            SECURITY DEFINER SET search_path = pg_catalog
            AS $$
            BEGIN
                RAISE EXCEPTION 'OKX Demo reconciliation evidence is immutable';
            END
            $$;
            ALTER FUNCTION reject_okx_demo_evidence_mutation()
                OWNER TO freqtrade_ai_attestor;
            REVOKE ALL ON FUNCTION reject_okx_demo_evidence_mutation()
                FROM PUBLIC, freqtrade;
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION finalize_okx_demo_reconciliation_run(
                p_run_id BIGINT,
                p_summary JSONB,
                p_database_ids JSONB,
                p_artifact_path TEXT,
                p_artifact_sha256 TEXT
            ) RETURNS BIGINT
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $$
            DECLARE
                v_run RECORD;
            BEGIN
                SELECT * INTO v_run
                FROM __SCHEMA__.reconciliation_runs
                WHERE id = p_run_id
                FOR UPDATE;
                IF NOT FOUND
                   OR v_run.execution_target_id <> 'OKX_DEMO'
                   OR v_run.artifact_status <> 'PENDING'
                   OR v_run.artifact_path IS NOT NULL
                   OR v_run.artifact_sha256 IS NOT NULL
                   OR v_run.database_ids::jsonb <> '{}'::jsonb
                   OR v_run.summary_snapshot::jsonb <> '{}'::jsonb
                   OR v_run.source_type <> 'api_aggregate'
                   OR v_run.core_data IS NOT TRUE
                   OR jsonb_typeof(p_summary) <> 'object'
                   OR jsonb_typeof(p_database_ids) <> 'object'
                   OR p_summary ->> 'execution_target' <> 'OKX_DEMO'
                   OR p_summary ->> 'status' <> v_run.status
                   OR p_summary ->> 'source_type' <> 'api_aggregate'
                   OR (p_summary ->> 'core_data')::boolean IS NOT TRUE
                   OR p_summary -> 'database_ids'
                        IS DISTINCT FROM p_database_ids
                   OR p_database_ids -> 'reconciliation_run'
                        IS DISTINCT FROM jsonb_build_array(p_run_id)
                   OR p_artifact_path !~
                        ('/okx-demo-reconciliation-' || p_run_id || E'\\.json$')
                   OR p_artifact_sha256 !~ '^[0-9a-f]{64}$'
                THEN
                    RAISE EXCEPTION
                        'invalid reconciliation run finalization';
                END IF;
                UPDATE __SCHEMA__.reconciliation_runs
                SET summary_snapshot = p_summary::json,
                    database_ids = p_database_ids::json,
                    artifact_path = p_artifact_path,
                    artifact_sha256 = p_artifact_sha256,
                    artifact_status = 'READY'
                WHERE id = p_run_id;
                RETURN p_run_id;
            END
            $$;
            ALTER FUNCTION finalize_okx_demo_reconciliation_run(
                BIGINT, JSONB, JSONB, TEXT, TEXT
            ) OWNER TO freqtrade_ai_attestor;
            REVOKE ALL ON FUNCTION finalize_okx_demo_reconciliation_run(
                BIGINT, JSONB, JSONB, TEXT, TEXT
            ) FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION finalize_okx_demo_reconciliation_run(
                BIGINT, JSONB, JSONB, TEXT, TEXT
            ) TO freqtrade;

            CREATE OR REPLACE FUNCTION apply_okx_demo_reconciliation_gate(
                p_run_id BIGINT
            ) RETURNS BIGINT
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $$
            DECLARE
                v_run RECORD;
                v_batch RECORD;
                v_state_id BIGINT;
                v_batch_id BIGINT;
                v_unfrozen BOOLEAN;
                v_reason TEXT;
                v_current_run_id BIGINT;
                v_current_completed_at TIMESTAMPTZ;
            BEGIN
                SELECT * INTO v_run
                FROM __SCHEMA__.reconciliation_runs
                WHERE id = p_run_id
                FOR SHARE;
                IF NOT FOUND
                   OR v_run.execution_target_id <> 'OKX_DEMO'
                   OR v_run.artifact_status <> 'READY'
                   OR v_run.status NOT IN (
                       'RECONCILED', 'RECOVERED', 'DRIFTED',
                       'STALE', 'UNKNOWN'
                   )
                   OR v_run.database_ids::jsonb -> 'reconciliation_run'
                        IS DISTINCT FROM jsonb_build_array(p_run_id)
                   OR v_run.summary_snapshot::jsonb ->> 'status'
                        <> v_run.status
                   OR v_run.summary_snapshot::jsonb -> 'database_ids'
                        IS DISTINCT FROM v_run.database_ids::jsonb
                THEN
                    RAISE EXCEPTION 'invalid reconciliation gate source';
                END IF;
                v_unfrozen := v_run.status IN ('RECONCILED', 'RECOVERED');
                IF v_unfrozen THEN
                    IF jsonb_typeof(
                           v_run.database_ids::jsonb -> 'recovery_batches'
                       ) <> 'array'
                       OR jsonb_array_length(
                           v_run.database_ids::jsonb -> 'recovery_batches'
                       ) <> 1
                       OR v_run.authoritative_observed_at IS NULL
                    THEN
                        RAISE EXCEPTION
                            'reconciliation gate source is not fresh';
                    END IF;
                    v_batch_id := (
                        v_run.database_ids::jsonb
                        -> 'recovery_batches' ->> 0
                    )::BIGINT;
                    SELECT * INTO v_batch
                    FROM __SCHEMA__.okx_demo_recovery_batches
                    WHERE database_id = v_batch_id
                      AND execution_target_id = 'OKX_DEMO'
                    FOR SHARE;
                    IF NOT FOUND
                       OR v_batch.authenticated IS NOT TRUE
                       OR v_batch.pagination_complete IS NOT TRUE
                       OR v_batch.complete_streams::jsonb IS DISTINCT FROM
                            '["ACCOUNT", "FILL", "ORDER", "POSITION"]'::jsonb
                       OR (
                           SELECT count(*)
                           FROM jsonb_object_keys(
                               v_batch.high_watermarks::jsonb
                           )
                       ) <> 4
                       OR NOT (
                           v_batch.high_watermarks::jsonb
                           ?& ARRAY[
                               'ACCOUNT', 'FILL', 'ORDER', 'POSITION'
                           ]
                       )
                       OR v_batch.event_count < 1
                       OR v_batch.completed_at
                            < clock_timestamp() - INTERVAL '2 minutes'
                       OR v_batch.completed_at > clock_timestamp()
                       OR v_batch.observed_at > v_batch.completed_at
                       OR NOT EXISTS (
                           SELECT 1
                           FROM __SCHEMA__.okx_demo_account_snapshots AS account
                           JOIN __SCHEMA__.okx_demo_exchange_events AS event
                             ON event.database_id =
                                account.event_database_id
                           WHERE event.recovery_batch_database_id = v_batch_id
                       )
                    THEN
                        RAISE EXCEPTION
                            'reconciliation gate baseline is incomplete';
                    END IF;
                END IF;
                v_reason := CASE
                    WHEN v_unfrozen THEN NULL
                    ELSE v_run.summary_snapshot::jsonb
                         -> 'findings' -> 0 ->> 'code'
                END;
                SELECT database_id, last_reconciliation_run_id
                INTO v_state_id, v_current_run_id
                FROM __SCHEMA__.okx_demo_reconciliation_states
                WHERE execution_target_id = 'OKX_DEMO'
                FOR UPDATE;
                IF v_state_id IS NULL THEN
                    RAISE EXCEPTION
                        'reconciliation gate state is missing';
                END IF;
                IF v_current_run_id IS NOT NULL THEN
                    SELECT completed_at INTO v_current_completed_at
                    FROM __SCHEMA__.reconciliation_runs
                    WHERE id = v_current_run_id;
                    IF v_current_completed_at > v_run.completed_at THEN
                        RAISE EXCEPTION
                            'reconciliation gate source is older than current';
                    END IF;
                END IF;
                UPDATE __SCHEMA__.okx_demo_reconciliation_states
                SET status = v_run.status,
                    opening_frozen = NOT v_unfrozen,
                    block_reason = v_reason,
                    last_event_observed_at =
                        v_run.authoritative_observed_at,
                    last_reconciliation_run_id = v_run.id
                WHERE database_id = v_state_id;
                RETURN v_state_id;
            END
            $$;
            ALTER FUNCTION apply_okx_demo_reconciliation_gate(BIGINT)
                OWNER TO freqtrade_ai_attestor;
            REVOKE ALL ON FUNCTION
                apply_okx_demo_reconciliation_gate(BIGINT) FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION
                apply_okx_demo_reconciliation_gate(BIGINT) TO freqtrade;

            CREATE OR REPLACE FUNCTION freeze_okx_demo_reconciliation_gate(
                p_status TEXT,
                p_reason TEXT,
                p_observed_at TIMESTAMPTZ
            ) RETURNS BIGINT
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $$
            DECLARE
                v_state_id BIGINT;
            BEGIN
                IF p_status NOT IN ('STALE', 'UNKNOWN')
                   OR p_reason IS NULL
                   OR length(p_reason) NOT BETWEEN 1 AND 240
                   OR p_observed_at IS NULL
                   OR p_observed_at > CURRENT_TIMESTAMP + INTERVAL '5 seconds'
                THEN
                    RAISE EXCEPTION
                        'invalid reconciliation freeze transition';
                END IF;
                UPDATE __SCHEMA__.okx_demo_reconciliation_states
                SET status = p_status,
                    opening_frozen = TRUE,
                    block_reason = p_reason,
                    last_event_observed_at = p_observed_at
                WHERE execution_target_id = 'OKX_DEMO'
                RETURNING database_id INTO v_state_id;
                IF v_state_id IS NULL THEN
                    RAISE EXCEPTION
                        'reconciliation gate state is missing';
                END IF;
                RETURN v_state_id;
            END
            $$;
            ALTER FUNCTION freeze_okx_demo_reconciliation_gate(
                TEXT, TEXT, TIMESTAMPTZ
            ) OWNER TO freqtrade_ai_attestor;
            REVOKE ALL ON FUNCTION freeze_okx_demo_reconciliation_gate(
                TEXT, TEXT, TIMESTAMPTZ
            ) FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION freeze_okx_demo_reconciliation_gate(
                TEXT, TEXT, TIMESTAMPTZ
            ) TO freqtrade;
            """.replace("__SCHEMA__", quoted_schema)
        )
    )
    for table_name in immutable_tables:
        connection.execute(
            text(
                "DROP TRIGGER IF EXISTS {}_immutable ON {}; "
                "CREATE TRIGGER {}_immutable "
                "BEFORE UPDATE OR DELETE ON {} "
                "FOR EACH ROW EXECUTE FUNCTION "
                "reject_okx_demo_evidence_mutation()".format(
                    table_name,
                    table_name,
                    table_name,
                    table_name,
                )
            )
        )
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION guard_okx_demo_recovery_grant_update()
            RETURNS trigger LANGUAGE plpgsql
            SECURITY DEFINER SET search_path = pg_catalog
            AS $$
            BEGIN
                IF OLD.execution_target_id IS DISTINCT FROM NEW.execution_target_id
                   OR OLD.reconciliation_run_id IS DISTINCT FROM NEW.reconciliation_run_id
                   OR OLD.exchange_order_row_id IS DISTINCT FROM NEW.exchange_order_row_id
                   OR OLD.grant_digest IS DISTINCT FROM NEW.grant_digest
                   OR OLD.action IS DISTINCT FROM NEW.action
                   OR OLD.instrument_id IS DISTINCT FROM NEW.instrument_id
                   OR OLD.position_side IS DISTINCT FROM NEW.position_side
                   OR OLD.max_quantity IS DISTINCT FROM NEW.max_quantity
                   OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
                   OR OLD.created_at IS DISTINCT FROM NEW.created_at
                   OR OLD.status <> 'ACTIVE'
                   OR NEW.status NOT IN ('CONSUMED', 'EXPIRED')
                   OR (
                       NEW.status = 'CONSUMED'
                       AND NEW.consumed_at IS NULL
                   )
                   OR (
                       NEW.status = 'EXPIRED'
                       AND NEW.consumed_at IS NOT NULL
                   ) THEN
                    RAISE EXCEPTION 'invalid recovery grant transition';
                END IF;
                RETURN NEW;
            END
            $$;
            ALTER FUNCTION guard_okx_demo_recovery_grant_update()
                OWNER TO freqtrade_ai_attestor;
            REVOKE ALL ON FUNCTION guard_okx_demo_recovery_grant_update()
                FROM PUBLIC, freqtrade;
            DROP TRIGGER IF EXISTS okx_demo_recovery_grants_guard
                ON okx_demo_recovery_grants;
            CREATE TRIGGER okx_demo_recovery_grants_guard
                BEFORE UPDATE ON okx_demo_recovery_grants
                FOR EACH ROW EXECUTE FUNCTION
                    guard_okx_demo_recovery_grant_update();
            """
        )
    )


def _add_okx_demo_runtime_recovery_binding(connection: Connection) -> None:
    """Bind every recovery grant to one durable writer attempt before POST."""

    schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
    preparer = connection.dialect.identifier_preparer
    quoted_schema = preparer.quote(schema_name)
    attempts = "{}.{}".format(
        quoted_schema, preparer.quote("okx_order_write_attempts")
    )
    connection.execute(
        text(
            """
            ALTER TABLE {attempts}
                ADD COLUMN IF NOT EXISTS recovery_grant_database_id BIGINT;
            ALTER TABLE {attempts}
                DROP CONSTRAINT IF EXISTS
                    okx_order_write_attempts_recovery_grant_database_id_fkey;
            ALTER TABLE {attempts}
                ADD CONSTRAINT
                    okx_order_write_attempts_recovery_grant_database_id_fkey
                FOREIGN KEY (recovery_grant_database_id)
                REFERENCES {schema}.okx_demo_recovery_grants(database_id)
                ON DELETE RESTRICT;
            ALTER TABLE {attempts}
                DROP CONSTRAINT IF EXISTS
                    okx_order_write_attempts_recovery_grant_database_id_key;
            ALTER TABLE {attempts}
                ADD CONSTRAINT
                    okx_order_write_attempts_recovery_grant_database_id_key
                UNIQUE (recovery_grant_database_id);
            """.format(attempts=attempts, schema=quoted_schema)
        )
    )
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION guard_okx_demo_exchange_order()
            RETURNS trigger LANGUAGE plpgsql
            SECURITY DEFINER SET search_path = pg_catalog
            AS $$
            DECLARE
                v_recovery_grant_id BIGINT;
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.client_order_id ~ '^rcv[0-9]{20}$' THEN
                        v_recovery_grant_id :=
                            substring(NEW.client_order_id from 4)::BIGINT;
                        IF NEW.execution_target_id <> 'OKX_DEMO'
                           OR NEW.status <> 'PREPARED'
                           OR NEW.exchange_order_id IS NOT NULL
                           OR json_typeof(NEW.request_snapshot) <> 'object'
                           OR json_typeof(NEW.response_snapshot) <> 'object'
                           OR NOT EXISTS (
                               SELECT 1
                               FROM __SCHEMA__.okx_demo_recovery_grants AS recovery_grant
                               JOIN __SCHEMA__.okx_demo_reconciliation_states AS state
                                 ON state.execution_target_id =
                                    recovery_grant.execution_target_id
                               JOIN __SCHEMA__.trade_intents AS intent
                                 ON intent.id = NEW.trade_intent_id
                               WHERE recovery_grant.database_id = v_recovery_grant_id
                                 AND recovery_grant.execution_target_id = 'OKX_DEMO'
                                 AND recovery_grant.action = 'REDUCE_ONLY'
                                 AND recovery_grant.status = 'ACTIVE'
                                 AND recovery_grant.expires_at > statement_timestamp()
                                 AND state.last_reconciliation_run_id =
                                     recovery_grant.reconciliation_run_id
                                 AND state.opening_frozen IS TRUE
                                 AND intent.execution_target_id = 'OKX_DEMO'
                                 AND intent.instrument_id =
                                     recovery_grant.instrument_id
                           )
                        THEN
                            RAISE EXCEPTION
                                'invalid recovery exchange order creation';
                        END IF;
                        RETURN NEW;
                    END IF;
                    IF NEW.execution_target_id <> 'OKX_DEMO'
                       OR NEW.status <> 'PREPARED'
                       OR NEW.exchange_order_id IS NOT NULL
                       OR json_typeof(NEW.request_snapshot) <> 'object'
                       OR json_typeof(NEW.response_snapshot) <> 'object'
                       OR NOT EXISTS (
                           SELECT 1
                           FROM __SCHEMA__.trade_intents AS intent
                           WHERE intent.id = NEW.trade_intent_id
                             AND intent.execution_target_id = 'OKX_DEMO'
                             AND intent.client_order_id =
                                 NEW.client_order_id
                             AND intent.status = 'APPROVED'
                       )
                    THEN
                        RAISE EXCEPTION 'invalid exchange order creation';
                    END IF;
                    RETURN NEW;
                END IF;
                IF OLD.id IS DISTINCT FROM NEW.id
                   OR OLD.execution_target_id IS DISTINCT FROM NEW.execution_target_id
                   OR OLD.trade_intent_id IS DISTINCT FROM NEW.trade_intent_id
                   OR OLD.client_order_id IS DISTINCT FROM NEW.client_order_id
                   OR OLD.request_snapshot::jsonb IS DISTINCT FROM NEW.request_snapshot::jsonb
                   OR OLD.created_at IS DISTINCT FROM NEW.created_at
                   OR (OLD.exchange_order_id IS NOT NULL
                       AND OLD.exchange_order_id IS DISTINCT FROM NEW.exchange_order_id)
                   OR NEW.status NOT IN (
                       'PREPARED', 'ACKNOWLEDGED', 'REJECTED',
                       'RECOVERY_REQUIRED', 'RESIDUAL_CLOSE_REQUIRED',
                       'RECONCILED', 'live', 'partially_filled',
                       'filled', 'canceled', 'mmp_canceled',
                       'position_zero', 'leverage_confirmed'
                   )
                   OR json_typeof(NEW.response_snapshot) <> 'object'
                   OR (OLD.status IN (
                       'REJECTED', 'RECONCILED', 'filled', 'canceled',
                       'mmp_canceled', 'position_zero'
                   ) AND NEW.status IS DISTINCT FROM OLD.status)
                THEN
                    RAISE EXCEPTION 'invalid exchange order transition';
                END IF;
                RETURN NEW;
            END
            $$;
            ALTER FUNCTION guard_okx_demo_exchange_order()
                OWNER TO freqtrade_ai_attestor;
            REVOKE ALL ON FUNCTION guard_okx_demo_exchange_order()
                FROM PUBLIC, freqtrade;
            """.replace("__SCHEMA__", quoted_schema)
        )
    )


def _add_full_chain(connection: Connection) -> None:
    """Install #450 tables and the lease-free operator approval state."""

    Base.metadata.create_all(bind=connection)
    _upgrade_strategy_candidate_approvals(connection)
    _upgrade_execution_full_chain(connection)
    connection.execute(
        text(
            """
            ALTER TABLE research_jobs
                DROP CONSTRAINT IF EXISTS research_jobs_status_check;
            ALTER TABLE research_jobs
                ADD CONSTRAINT research_jobs_status_check CHECK (
                    status IN (
                        'PENDING', 'RUNNING', 'AWAITING_APPROVAL', 'SUCCESS',
                        'FAILED', 'BLOCKED', 'CANCELLED', 'STALE'
                    )
                );
            """
        )
    )
    _add_okx_demo_soak(connection)


def _upgrade_strategy_candidate_approvals(connection: Connection) -> None:
    """Persist promotion proof with the approval and invalidate legacy proof.

    Existing approvals lacked a durable policy/evidence snapshot.  They are
    intentionally marked as legacy-unverified, which makes the next signal
    revalidation revoke them rather than silently allowing an old decision.
    """

    connection.execute(
        text(
            "ALTER TABLE strategy_candidate_approvals "
            "ADD COLUMN IF NOT EXISTS promotion_policy_version VARCHAR(80), "
            "ADD COLUMN IF NOT EXISTS promotion_evidence JSON"
        )
    )
    connection.execute(
        text(
            "UPDATE strategy_candidate_approvals "
            "SET promotion_policy_version = COALESCE(promotion_policy_version, 'legacy-unverified'), "
            "promotion_evidence = COALESCE(promotion_evidence, '{}'::json) "
            "WHERE promotion_policy_version IS NULL OR promotion_evidence IS NULL"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE strategy_candidate_approvals "
            "ALTER COLUMN promotion_policy_version SET NOT NULL, "
            "ALTER COLUMN promotion_evidence SET NOT NULL"
        )
    )


def _grant_runtime_application_acl(connection: Connection) -> None:
    """Grant the runtime role only the ordinary application-data ACL.

    OKX attestation, authorization, writer, exchange and reconciliation tables
    deliberately remain outside this allowlist because their narrower grants are
    installed and verified by their owning migrations.
    """

    schema_name, effective_schemas = connection.execute(
        text("SELECT current_schema(), current_schemas(false)")
    ).one()
    if not schema_name or list(effective_schemas or ()) != [schema_name]:
        raise SchemaMigrationBlocked(
            "Runtime application ACL migration requires exactly one effective schema"
        )
    existing_tables = set(inspect(connection).get_table_names(schema=schema_name))
    missing_tables = set(RUNTIME_APPLICATION_TABLES) - existing_tables
    if missing_tables:
        raise SchemaMigrationBlocked(
            "Runtime application ACL tables are missing: "
            + ", ".join(sorted(missing_tables))
        )
    quote = connection.dialect.identifier_preparer.quote
    quoted_schema = quote(schema_name)
    inspector = inspect(connection)
    for table_name in RUNTIME_APPLICATION_TABLES:
        qualified_table = "{}.{}".format(quoted_schema, quote(table_name))
        column_names = {
            column["name"]
            for column in inspector.get_columns(table_name, schema=schema_name)
        }
        quoted_columns = ", ".join(
            quote(column_name) for column_name in sorted(column_names)
        )
        connection.execute(
            text(
                "REVOKE ALL ({}) ON TABLE {} FROM PUBLIC, freqtrade; "
                "REVOKE ALL ON TABLE {} FROM PUBLIC, freqtrade; "
                "GRANT SELECT, INSERT, UPDATE ON TABLE {} TO freqtrade".format(
                    quoted_columns,
                    qualified_table,
                    qualified_table,
                    qualified_table,
                )
            )
        )
        sequence_identity = (
            connection.execute(
                text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
                {"table_name": "{}.{}".format(schema_name, table_name)},
            ).scalar_one()
            if "id" in column_names
            else None
        )
        if sequence_identity:
            connection.execute(
                text(
                    "REVOKE ALL ON SEQUENCE {} FROM PUBLIC, freqtrade; "
                    "GRANT USAGE, SELECT ON SEQUENCE {} TO freqtrade".format(
                        sequence_identity,
                        sequence_identity,
                    )
                )
            )


def _runtime_application_acl_problems(
    connection: Connection,
    schema_name: str,
) -> list[str]:
    """Return exact runtime ACL drift for the ordinary application allowlist."""

    problems = []
    server_version_num = int(
        connection.execute(text("SHOW server_version_num")).scalar_one()
    )
    unsafe_privileges = (
        "DELETE,TRUNCATE,REFERENCES,TRIGGER"
        + (",MAINTAIN" if server_version_num >= 170000 else "")
    )
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names(schema=schema_name))
    for table_name in RUNTIME_APPLICATION_TABLES:
        if table_name not in existing_tables:
            problems.append("runtime application ACL table missing: " + table_name)
            continue
        can_select, can_insert, can_update, can_unsafe = connection.execute(
            text(
                "SELECT "
                "has_table_privilege('freqtrade', :table_name, 'SELECT'), "
                "has_table_privilege('freqtrade', :table_name, 'INSERT'), "
                "has_table_privilege('freqtrade', :table_name, 'UPDATE'), "
                "has_table_privilege('freqtrade', :table_name, :unsafe)"
            ),
            {
                "table_name": "{}.{}".format(schema_name, table_name),
                "unsafe": unsafe_privileges,
            },
        ).one()
        if not (can_select and can_insert and can_update):
            problems.append(
                "runtime application DML privilege missing: " + table_name
            )
        if can_unsafe:
            problems.append(
                "runtime application unsafe privilege present: " + table_name
            )
        public_table_acl = connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_class AS relation
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    CROSS JOIN LATERAL aclexplode(
                        COALESCE(
                            relation.relacl,
                            acldefault('r', relation.relowner)
                        )
                    ) AS acl
                    WHERE namespace.nspname = :schema_name
                      AND relation.relname = :table_name
                      AND acl.grantee = 0
                      AND acl.privilege_type IN (
                          'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
                          'REFERENCES', 'TRIGGER', 'MAINTAIN'
                      )
                )
                """
            ),
            {"schema_name": schema_name, "table_name": table_name},
        ).scalar_one()
        if public_table_acl:
            problems.append(
                "PUBLIC runtime application table privilege present: "
                + table_name
            )
        explicit_column_acl = connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_class AS relation
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    JOIN pg_attribute AS attribute
                      ON attribute.attrelid = relation.oid
                     AND attribute.attnum > 0
                     AND NOT attribute.attisdropped
                    CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
                    WHERE namespace.nspname = :schema_name
                      AND relation.relname = :table_name
                      AND acl.grantee IN (
                          0,
                          (SELECT oid FROM pg_roles
                           WHERE rolname = 'freqtrade')
                      )
                )
                """
            ),
            {"schema_name": schema_name, "table_name": table_name},
        ).scalar_one()
        if explicit_column_acl:
            problems.append(
                "runtime application column privilege present: " + table_name
            )
        column_names = {
            column["name"]
            for column in inspector.get_columns(table_name, schema=schema_name)
        }
        sequence_identity = (
            connection.execute(
                text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
                {"table_name": "{}.{}".format(schema_name, table_name)},
            ).scalar_one()
            if "id" in column_names
            else None
        )
        if not sequence_identity:
            continue
        can_usage, can_sequence_select, can_sequence_update = connection.execute(
            text(
                "SELECT "
                "has_sequence_privilege('freqtrade', :sequence_name, 'USAGE'), "
                "has_sequence_privilege('freqtrade', :sequence_name, 'SELECT'), "
                "has_sequence_privilege('freqtrade', :sequence_name, 'UPDATE')"
            ),
            {"sequence_name": sequence_identity},
        ).one()
        if not (can_usage and can_sequence_select) or can_sequence_update:
            problems.append(
                "runtime application sequence privilege mismatch: "
                + sequence_identity
            )
        public_sequence_acl = connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_class AS sequence
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = sequence.relnamespace
                    CROSS JOIN LATERAL aclexplode(
                        COALESCE(
                            sequence.relacl,
                            acldefault('S', sequence.relowner)
                        )
                    ) AS acl
                    WHERE sequence.oid = to_regclass(:sequence_name)
                      AND acl.grantee = 0
                )
                """
            ),
            {"sequence_name": sequence_identity},
        ).scalar_one()
        if public_sequence_acl:
            problems.append(
                "PUBLIC runtime application sequence privilege present: "
                + sequence_identity
            )
    return problems


def _add_okx_demo_soak(connection: Connection) -> None:
    """Add #453 evidence tables without another runtime or database."""

    schema_name, effective_schemas = connection.execute(
        text("SELECT current_schema(), current_schemas(false)")
    ).one()
    if not schema_name or list(effective_schemas or ()) != [schema_name]:
        raise SchemaMigrationBlocked(
            "OKX Demo soak migration requires exactly one effective schema"
        )
    for table_name in (
        "okx_demo_soak_runs",
        "okx_demo_soak_probes",
        "okx_demo_soak_events",
    ):
        Base.metadata.tables[table_name].create(bind=connection, checkfirst=True)
    quote = connection.dialect.identifier_preparer.quote
    quoted_schema = quote(schema_name)
    for table_name in (
        "okx_demo_soak_runs",
        "okx_demo_soak_probes",
        "okx_demo_soak_events",
    ):
        qualified_table = "{}.{}".format(quoted_schema, quote(table_name))
        connection.execute(
            text(
                "ALTER TABLE {} OWNER TO freqtrade_ai_attestor; "
                "REVOKE ALL ON TABLE {} FROM PUBLIC, freqtrade; "
                "GRANT SELECT, INSERT{} ON TABLE {} TO freqtrade".format(
                    qualified_table,
                    qualified_table,
                    ", UPDATE" if table_name == "okx_demo_soak_runs" else "",
                    qualified_table,
                )
            )
        )
        sequence_identity = connection.execute(
            text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
            {"table_name": "{}.{}".format(schema_name, table_name)},
        ).scalar_one()
        if not sequence_identity:
            raise SchemaMigrationBlocked(
                "OKX Demo soak sequence is missing: " + table_name
            )
        connection.execute(
            text(
                "ALTER SEQUENCE {} OWNER TO freqtrade_ai_attestor; "
                "REVOKE ALL ON SEQUENCE {} FROM PUBLIC, freqtrade; "
                "GRANT USAGE, SELECT ON SEQUENCE {} TO freqtrade".format(
                    sequence_identity,
                    sequence_identity,
                    sequence_identity,
                )
            )
        )
    _add_strategy_deployment_queue(connection)
    _grant_runtime_application_acl(connection)


def _add_strategy_deployment_queue(connection: Connection) -> None:
    """Install the durable deployment and per-candle evaluation handoff."""

    Base.metadata.tables["strategy_deployments"].create(
        bind=connection,
        checkfirst=True,
    )
    Base.metadata.tables["signal_evaluations"].create(
        bind=connection,
        checkfirst=True,
    )


def _add_deferred_execution_foreign_keys(connection: Connection) -> None:
    """Restore metadata FKs deferred by the cyclic execution graph.

    ``full_chain_runs`` references ``signal_evaluations``, whose deployment
    ultimately references ``strategy_candidate_approvals`` and the originating
    full-chain run.  SQLAlchemy correctly defers this cycle when creating every
    table together, but ``create_all(checkfirst=True)`` cannot add those deferred
    constraints to tables that already existed before an incremental upgrade.
    Add only absent metadata constraints after every table in the cycle exists.
    """

    schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
    inspector = inspect(connection)
    for table_name in (
        "full_chain_runs",
        "strategy_candidate_approvals",
        "strategy_deployments",
        "signal_evaluations",
    ):
        table = Base.metadata.tables[table_name]
        actual_fks = {
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key.get("referred_schema") or schema_name,
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
                (
                    (foreign_key.get("options") or {}).get("ondelete")
                    or "NO ACTION"
                ).upper(),
                (
                    (foreign_key.get("options") or {}).get("onupdate")
                    or "NO ACTION"
                ).upper(),
                bool(
                    (foreign_key.get("options") or {}).get(
                        "deferrable",
                        False,
                    )
                ),
                (
                    (foreign_key.get("options") or {}).get("initially")
                    or ""
                ).upper()
                or None,
            )
            for foreign_key in inspector.get_foreign_keys(
                table_name,
                schema=schema_name,
            )
        }
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            signature = (
                tuple(element.parent.name for element in constraint.elements),
                constraint.elements[0].column.table.schema or schema_name,
                constraint.elements[0].column.table.name,
                tuple(element.column.name for element in constraint.elements),
                (constraint.ondelete or "NO ACTION").upper(),
                (constraint.onupdate or "NO ACTION").upper(),
                bool(constraint.deferrable),
                (constraint.initially or "").upper() or None,
            )
            if signature not in actual_fks:
                connection.execute(AddConstraint(constraint))


def _upgrade_execution_full_chain(connection: Connection) -> None:
    """Allow one lease-fenced execution chain per signal evaluation."""

    connection.execute(
        text(
            """
            ALTER TABLE full_chain_runs
                DROP CONSTRAINT IF EXISTS full_chain_runs_research_job_unique,
                ADD COLUMN IF NOT EXISTS run_kind VARCHAR(16),
                ADD COLUMN IF NOT EXISTS signal_evaluation_id BIGINT;
            UPDATE full_chain_runs
               SET run_kind = 'RESEARCH'
             WHERE run_kind IS NULL;
            ALTER TABLE full_chain_runs
                ALTER COLUMN run_kind SET DEFAULT 'RESEARCH',
                ALTER COLUMN run_kind SET NOT NULL,
                DROP CONSTRAINT IF EXISTS full_chain_runs_kind_binding_check,
                ADD CONSTRAINT full_chain_runs_kind_binding_check CHECK (
                    run_kind = 'RESEARCH' AND signal_evaluation_id IS NULL
                    OR run_kind = 'EXECUTION' AND signal_evaluation_id IS NOT NULL
                ),
                DROP CONSTRAINT IF EXISTS full_chain_runs_signal_evaluation_id_fkey,
                ADD CONSTRAINT full_chain_runs_signal_evaluation_id_fkey
                    FOREIGN KEY (signal_evaluation_id)
                    REFERENCES signal_evaluations(id) ON DELETE RESTRICT;
            CREATE UNIQUE INDEX IF NOT EXISTS full_chain_runs_research_job_unique
                ON full_chain_runs (research_job_id)
                WHERE run_kind = 'RESEARCH';
            CREATE UNIQUE INDEX IF NOT EXISTS full_chain_runs_signal_evaluation_unique
                ON full_chain_runs (signal_evaluation_id)
                WHERE run_kind = 'EXECUTION';
            """
        )
    )
    _add_deferred_execution_foreign_keys(connection)


def _upgrade_dual_side_trade_intents(connection: Connection) -> None:
    """Replace the legacy net-position constraint without rewriting lineage."""

    connection.execute(
        text(
            "ALTER TABLE trade_intents "
            "DROP CONSTRAINT IF EXISTS trade_intents_position_side_check, "
            "ADD CONSTRAINT trade_intents_position_side_check CHECK ("
            "authorization_" "schema_version = 'LEGACY' OR status = 'BLOCKED' "
            "OR position_side = 'long' AND "
            "(side = 'buy' AND reduce_only = FALSE OR "
            "side = 'sell' AND reduce_only = TRUE) "
            "OR position_side = 'short' AND "
            "(side = 'sell' AND reduce_only = FALSE OR "
            "side = 'buy' AND reduce_only = TRUE)"
            ")"
        )
    )


def _add_okx_demo_recovery_batch_index(connection: Connection) -> None:
    """Install the batch lookup used by every runtime reconciliation cycle."""

    schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
    if "okx_demo_exchange_events" not in inspect(connection).get_table_names(
        schema=schema_name
    ):
        return
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS "
            "okx_demo_exchange_events_batch_observed_idx "
            "ON okx_demo_exchange_events ("
            "execution_target_id, recovery_batch_database_id, "
            "observed_at, database_id)"
        )
    )


def upgrade_database(engine: Engine) -> str:
    """Upgrade a local PostgreSQL database atomically to ``SCHEMA_VERSION``.

    A schema created by the old unversioned SQL file is accepted only when every
    managed table is empty.  A non-empty legacy database needs a separately planned
    data migration, and this function raises before altering it.
    """

    _require_postgres(engine)
    expected_table_names = set(_expected_tables())
    try:
        with engine.begin() as connection:
            _create_version_table(connection)
            current_version = _current_version(connection)
            if current_version == SCHEMA_VERSION:
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Recorded schema version does not match ORM metadata: " + "; ".join(problems)
                    )
                return current_version
            supported_upgrade_versions = {
                LEGACY_SCHEMA_VERSION,
                PREVIOUS_SCHEMA_VERSION,
                TARGET_LINEAGE_BASE_VERSION,
                EARLY_TARGET_LINEAGE_VERSION,
                RISK_CHAIN_BASE_VERSION,
                RISK_CHAIN_HARDENING_BASE_VERSION,
                TRUSTED_SNAPSHOT_BASE_VERSION,
                ATTESTED_SESSION_BASE_VERSION,
                HMAC_ATTESTATION_BASE_VERSION,
                ATTESTATION_ACL_BASE_VERSION,
                ORDER_WRITER_BASE_VERSION,
                RECONCILIATION_BASE_VERSION,
                RUNTIME_RECOVERY_BASE_VERSION,
                FULL_CHAIN_BASE_VERSION,
                SOAK_BASE_VERSION,
                RUNTIME_APP_ACL_BASE_VERSION,
                FILL_SNAPSHOT_REPEAT_BASE_VERSION,
                RECONCILIATION_BATCH_FRESHNESS_BASE_VERSION,
                RECOVERY_WALL_CLOCK_BASE_VERSION,
                DUAL_SIDE_BASE_VERSION,
                STRATEGY_PROMOTION_BASE_VERSION,
                STRATEGY_DEPLOYMENT_BASE_VERSION,
                EXECUTION_FULL_CHAIN_BASE_VERSION,
                RECONCILIATION_INDEX_BASE_VERSION,
            }
            if current_version in supported_upgrade_versions:
                connection.execute(
                    text(
                        "ALTER TABLE IF EXISTS okx_demo_fill_snapshots "
                        "DROP CONSTRAINT IF EXISTS "
                        "okx_demo_fill_snapshots_fill_unique"
                    )
                )
                _add_okx_demo_recovery_batch_index(connection)
            if current_version == RECONCILIATION_INDEX_BASE_VERSION:
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Reconciliation index upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == FILL_SNAPSHOT_REPEAT_BASE_VERSION:
                _add_strategy_deployment_queue(connection)
                _grant_runtime_application_acl(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Fill snapshot repeat upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == RECONCILIATION_BATCH_FRESHNESS_BASE_VERSION:
                _add_okx_demo_reconciliation(connection)
                _add_strategy_deployment_queue(connection)
                _grant_runtime_application_acl(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Reconciliation batch freshness upgrade does not match "
                        "ORM metadata: " + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == RECOVERY_WALL_CLOCK_BASE_VERSION:
                _add_okx_demo_reconciliation(connection)
                _add_strategy_deployment_queue(connection)
                _grant_runtime_application_acl(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Recovery wall-clock upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == DUAL_SIDE_BASE_VERSION:
                _upgrade_dual_side_trade_intents(connection)
                _upgrade_strategy_candidate_approvals(connection)
                _add_strategy_deployment_queue(connection)
                _grant_runtime_application_acl(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Dual-side upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == STRATEGY_PROMOTION_BASE_VERSION:
                _upgrade_strategy_candidate_approvals(connection)
                _add_strategy_deployment_queue(connection)
                _grant_runtime_application_acl(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Strategy promotion upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == STRATEGY_DEPLOYMENT_BASE_VERSION:
                _add_strategy_deployment_queue(connection)
                _upgrade_execution_full_chain(connection)
                _grant_runtime_application_acl(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Strategy deployment queue upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == EXECUTION_FULL_CHAIN_BASE_VERSION:
                _upgrade_execution_full_chain(connection)
                _grant_runtime_application_acl(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Execution full-chain upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version in {
                LEGACY_SCHEMA_VERSION,
                PREVIOUS_SCHEMA_VERSION,
                TARGET_LINEAGE_BASE_VERSION,
            }:
                # The migration preserves runtime tables, adds any missing managed
                # tables, and removes only the retired debug table after proving it
                # is empty.
                _drop_retired_debug_table(connection)
                _add_execution_target_lineage(connection)
                Base.metadata.tables["trade_intents"].create(
                    bind=connection, checkfirst=True
                )
                Base.metadata.tables["risk_decisions"].create(
                    bind=connection, checkfirst=True
                )
                _add_risk_chain(connection)
                _harden_risk_chain(connection)
                _add_trusted_snapshot_boundary(connection)
                _add_attested_session_boundary(connection)
                _add_order_writer(connection)
                Base.metadata.create_all(bind=connection)
                _add_okx_demo_reconciliation(connection)
                _add_okx_demo_runtime_recovery_binding(connection)
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Incremental schema upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                    )
                return SCHEMA_VERSION
            if current_version == EARLY_TARGET_LINEAGE_VERSION:
                _upgrade_early_execution_target_lineage(connection)
                _add_risk_chain(connection)
                _harden_risk_chain(connection)
                _add_trusted_snapshot_boundary(connection)
                _add_attested_session_boundary(connection)
                _add_order_writer(connection)
                Base.metadata.create_all(bind=connection)
                _add_okx_demo_reconciliation(connection)
                _add_okx_demo_runtime_recovery_binding(connection)
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Early target-lineage schema upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == RISK_CHAIN_BASE_VERSION:
                _add_risk_chain(connection)
                _harden_risk_chain(connection)
                _add_trusted_snapshot_boundary(connection)
                _add_attested_session_boundary(connection)
                _add_order_writer(connection)
                Base.metadata.create_all(bind=connection)
                _add_okx_demo_reconciliation(connection)
                _add_okx_demo_runtime_recovery_binding(connection)
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Risk-chain schema upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == RISK_CHAIN_HARDENING_BASE_VERSION:
                _harden_risk_chain(connection)
                _add_trusted_snapshot_boundary(connection)
                _add_attested_session_boundary(connection)
                _add_order_writer(connection)
                Base.metadata.create_all(bind=connection)
                _add_okx_demo_reconciliation(connection)
                _add_okx_demo_runtime_recovery_binding(connection)
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Risk-chain hardening upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == TRUSTED_SNAPSHOT_BASE_VERSION:
                _add_trusted_snapshot_boundary(connection)
                _add_attested_session_boundary(connection)
                _add_order_writer(connection)
                Base.metadata.create_all(bind=connection)
                _add_okx_demo_reconciliation(connection)
                _add_okx_demo_runtime_recovery_binding(connection)
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Trusted snapshot boundary upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == ATTESTED_SESSION_BASE_VERSION:
                _add_attested_session_boundary(connection)
                _add_order_writer(connection)
                Base.metadata.create_all(bind=connection)
                _add_okx_demo_reconciliation(connection)
                _add_okx_demo_runtime_recovery_binding(connection)
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Attested session boundary upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == HMAC_ATTESTATION_BASE_VERSION:
                _add_attested_session_boundary(connection)
                _add_order_writer(connection)
                Base.metadata.create_all(bind=connection)
                _add_okx_demo_reconciliation(connection)
                _add_okx_demo_runtime_recovery_binding(connection)
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "HMAC attestation boundary upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == ATTESTATION_ACL_BASE_VERSION:
                _add_approved_snapshot_lineage(connection)
                _add_order_writer(connection)
                Base.metadata.create_all(bind=connection)
                _add_okx_demo_reconciliation(connection)
                _add_okx_demo_runtime_recovery_binding(connection)
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Approval snapshot lineage upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == ORDER_WRITER_BASE_VERSION:
                _add_order_writer(connection)
                Base.metadata.create_all(bind=connection)
                _add_okx_demo_reconciliation(connection)
                _add_okx_demo_runtime_recovery_binding(connection)
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Order-writer schema upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == RECONCILIATION_BASE_VERSION:
                Base.metadata.create_all(bind=connection)
                _add_okx_demo_reconciliation(connection)
                _add_okx_demo_runtime_recovery_binding(connection)
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Reconciliation schema upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == RUNTIME_RECOVERY_BASE_VERSION:
                Base.metadata.create_all(bind=connection)
                _add_okx_demo_reconciliation(connection)
                _add_okx_demo_runtime_recovery_binding(connection)
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Runtime recovery schema upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == FULL_CHAIN_BASE_VERSION:
                _add_full_chain(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Full-chain schema upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == SOAK_BASE_VERSION:
                _add_okx_demo_soak(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "OKX Demo soak schema upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == RUNTIME_APP_ACL_BASE_VERSION:
                _add_strategy_deployment_queue(connection)
                _grant_runtime_application_acl(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Runtime application ACL upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version is not None:
                raise SchemaMigrationBlocked(
                    f"Unsupported schema version {current_version!r}; expected {SCHEMA_VERSION!r}."
                )

            schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
            existing_tables = (
                set(inspect(connection).get_table_names(schema=schema_name)) & expected_table_names
            )
            if existing_tables:
                nonempty = _nonempty_tables(connection, existing_tables)
                if nonempty:
                    raise SchemaMigrationBlocked(
                        "Unversioned legacy schema contains data in "
                        f"{', '.join(sorted(nonempty))}. Create a backup and use an explicit "
                        "data-preserving migration; no changes were applied."
                    )
                _drop_empty_legacy_tables(connection, existing_tables)

            Base.metadata.create_all(bind=connection)
            _add_trusted_snapshot_boundary(connection)
            _add_attested_session_boundary(connection)
            _add_order_writer(connection)
            _add_okx_demo_reconciliation(connection)
            _add_okx_demo_runtime_recovery_binding(connection)
            _add_full_chain(connection)
            problems = schema_problems(connection)
            if problems:
                raise SchemaMigrationBlocked(
                    "Generated schema does not match ORM metadata: " + "; ".join(problems)
                )
            connection.execute(
                text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                {"version": SCHEMA_VERSION},
            )
    except SQLAlchemyError as exc:
        raise ConfigurationError(
            f"Database migration failed for {database_identity(engine)}: {exc.__class__.__name__}"
        ) from exc
    return SCHEMA_VERSION


def _verify_connection_schema(
    connection: Connection,
    *,
    require_runtime_role: bool,
) -> SchemaReadiness:
    """Verify one exact connection and its active writer schema."""

    identity = database_identity(connection.engine)
    if connection.dialect.name != "postgresql":
        return SchemaReadiness(identity, None, False, ("database dialect is not PostgreSQL",))
    try:
        (
            database_name,
            schema_name,
            search_path,
            effective_schemas,
            current_role,
        ) = connection.execute(
            text(
                "SELECT current_database(), current_schema(), "
                "current_setting('search_path'), current_schemas(false), "
                "current_user"
            )
        ).one()
        problems = []
        if require_runtime_role and current_role != "freqtrade":
            problems.append(
                "writer connection role mismatch: expected freqtrade got {}".format(
                    current_role
                )
            )
        expected_database = connection.engine.url.database
        if expected_database and database_name != expected_database:
            problems.append(
                "connection database identity mismatch: expected {} got {}".format(
                    expected_database,
                    database_name,
                )
            )
        if list(effective_schemas or ()) != [schema_name]:
            problems.append(
                "active search_path is not single-schema: {}".format(search_path)
            )
        if VERSION_TABLE not in inspect(connection).get_table_names(schema=schema_name):
            problems.append("migration version table is missing")
            return SchemaReadiness(identity, None, False, tuple(problems))
        version = _current_version(connection)
        problems.extend(schema_problems(connection))
    except SQLAlchemyError as exc:
        return SchemaReadiness(identity, None, False, (f"database query failed: {exc.__class__.__name__}",))
    if version != SCHEMA_VERSION:
        problems.append(f"schema version is {version or '<missing>'}, expected {SCHEMA_VERSION}")
    return SchemaReadiness(identity, version, not problems, tuple(problems))


def verify_connection_schema(connection: Connection) -> SchemaReadiness:
    """Verify the exact runtime connection used by a writer Session."""

    return _verify_connection_schema(
        connection,
        require_runtime_role=True,
    )


def verify_schema(engine: Engine) -> SchemaReadiness:
    """Return a non-secret readiness result; callers decide the HTTP/CLI failure mode."""

    if engine.dialect.name != "postgresql":
        return SchemaReadiness(
            database_identity(engine),
            None,
            False,
            ("database dialect is not PostgreSQL",),
        )
    try:
        with engine.connect() as connection:
            return _verify_connection_schema(
                connection,
                require_runtime_role=False,
            )
    except SQLAlchemyError as exc:
        return SchemaReadiness(
            database_identity(engine),
            None,
            False,
            (f"database query failed: {exc.__class__.__name__}",),
        )
