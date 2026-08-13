"""Forward-only Strategy Platform V1.3 Task 1 migration primitives.

The schema installer is safe to call from the ordinary database upgrader.  The
real-data migrator is deliberately explicit: it requires a caller-supplied,
read-only market-data inventory and records every legacy mapping.  It never
downloads market data, invokes an exchange, runs a backtest, creates a signal,
or changes deployment/runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from app.models.base import Base
from app.services.strategy_platform_adapter_registry import (
    installed_adapter_manifest_digest,
    validate_declared_adapter_coverage,
)


TASK1_SCHEMA_VERSION = "20260813_47"
TASK1_MIGRATION_KEY = "strategy-platform-v13-task1-real-data-v1"
TASK1_ADVISORY_LOCK_KEY = 1_308_202_608_130_047

# These are all additive.  Existing tables are extended separately below.
STRATEGY_PLATFORM_V13_EXTENSION_TABLES = (
    "adapter_definitions",
    "strategy_source_definitions",
    "strategy_source_definition_versions",
    "trigger_source_definitions",
    "trigger_source_definition_versions",
    "timeframe_definitions",
    "timeframe_definition_versions",
    "research_target_config_sets",
    "research_target_configs",
    "strategy_family_definitions",
    "strategy_family_definition_versions",
    "provider_model_config_versions",
    "generation_profile_versions",
    "generation_profile_families",
    "scoring_profile_versions",
    "scoring_rules",
    "diversity_profile_versions",
    "diversity_rules",
    "worker_execution_profile_versions",
    "scheduler_profile_versions",
    "market_data_policy_versions",
    "evidence_freshness_profile_versions",
    "evidence_freshness_rules",
    "monitoring_profile_versions",
    "promotion_profile_versions",
    "promotion_rules",
    "risk_profile_versions",
    "risk_rules",
    "capacity_profile_versions",
    "runtime_profile_versions",
    "deployment_profile_versions",
    "market_data_profile_versions",
    "optimization_profile_versions",
    "ui_presentation_profile_versions",
    "research_profile_versions",
    "strategy_submissions",
    "strategy_runtime_instances",
    "strategy_position_ledger_entries",
    "strategy_position_reconciliation_items",
    "market_data_file_records",
    "market_data_update_jobs",
    "market_data_update_items",
    "optimization_runs",
    "optimization_trials",
    "strategy_platform_migration_runs",
    "strategy_platform_migration_table_snapshots",
    "strategy_platform_migration_entity_mappings",
    "strategy_platform_migration_conflicts",
)

LEGACY_ENTITY_TABLES = (
    "strategies",
    "strategy_versions",
    "strategy_generation_runs",
    "strategy_failure_reasons",
    "strategy_research_batches",
    "strategy_research_candidates",
    "research_jobs",
    "research_job_attempts",
    "strategy_research_candidate_bridge_events",
    "strategy_research_attempt_events",
    "market_data_quality_receipts",
    "backtest_runs",
    "backtest_tasks",
    "backtest_results",
    "strategy_validation_plans",
    "strategy_validation_windows",
    "strategy_scores",
    "strategy_candidate_approvals",
    "strategy_deployments",
    "full_chain_runs",
    "full_chain_stage_runs",
    "full_chain_signal_snapshots",
    "signal_evaluations",
    "trade_intents",
    "risk_decisions",
    "approved_executions",
    "exchange_orders",
    "exchange_fills",
    "exchange_positions",
    "reconciliation_runs",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_TIMERANGE_RE = re.compile(r"(?P<start>[0-9]{8})-(?P<end>[0-9]{8})")


class StrategyPlatformTask1Blocked(RuntimeError):
    """The controlled migration cannot prove a required safety/data fact."""


@dataclass(frozen=True)
class MarketFileEvidence:
    exchange: str
    market_type: str
    pair: str
    instrument_id: str
    timeframe: str
    data_kind: str
    absolute_path: str
    relative_path: str
    file_format: str
    size_bytes: int
    sha256: str
    row_count: int
    first_open_at: datetime
    last_open_at: datetime
    last_close_at: datetime
    expected_interval_seconds: int
    gap_count: int
    duplicate_count: int
    null_count: int
    freshness_status: str
    observed_at: datetime
    receipt_id: int | None = None
    source_receipt_digest: str | None = None
    classification_windows: Mapping[str, Mapping[str, Any]] | None = None
    inspected_at: datetime | None = None
    file_identity_digest: str | None = None
    source_identity_digest: str | None = None
    aggregate_receipt_digest: str | None = None
    migration_artifact_digest: str | None = None
    source_type: str | None = None
    source_receipt_path: str | None = None
    source_response_chain_digest: str | None = None
    quality_scope: str | None = None
    quality_decision: str | None = None
    freshness_basis: str | None = None
    out_of_order_count: int = 0
    misaligned_timestamp_count: int = 0
    invalid_ohlc_count: int = 0
    negative_volume_count: int = 0


@dataclass(frozen=True)
class Task1MigrationResult:
    migration_run_id: int
    strategy_target_count: int
    mapped_version_count: int
    mapped_plan_count: int
    mapped_window_count: int
    blocked_summary_count: int
    market_file_count: int
    unmapped_count: int
    conflict_count: int
    repeat_noop: bool
    source_snapshot_digest: str
    target_snapshot_digest: str
    report: Mapping[str, Any]


def _market_file_scan_payload(item: MarketFileEvidence) -> dict[str, Any]:
    """Canonical, independently recomputable evidence for one inspected file."""

    return {
        "contract": "strategy-platform-v13-market-file-scan-v1",
        "exchange": item.exchange,
        "market_type": item.market_type,
        "pair": item.pair,
        "instrument_id": item.instrument_id,
        "timeframe": item.timeframe,
        "data_kind": item.data_kind,
        # absolute_path is an observed locator only.  It is deliberately not
        # part of the canonical identity so the same verified artifact remains
        # idempotent after a physical data-root relocation.
        "relative_path": item.relative_path,
        "file_format": item.file_format,
        "size_bytes": item.size_bytes,
        "sha256": item.sha256,
        "row_count": item.row_count,
        "first_open_at": item.first_open_at,
        "last_open_at": item.last_open_at,
        "last_close_at": item.last_close_at,
        "expected_interval_seconds": item.expected_interval_seconds,
        "gap_count": item.gap_count,
        "duplicate_count": item.duplicate_count,
        "null_count": item.null_count,
        "freshness_status": item.freshness_status,
        "observed_at": item.observed_at,
        "source_receipt_digest": item.source_receipt_digest,
        "file_identity_digest": item.file_identity_digest,
        "source_identity_digest": item.source_identity_digest,
        "aggregate_receipt_digest": item.aggregate_receipt_digest,
        "migration_artifact_digest": item.migration_artifact_digest,
        "source_type": item.source_type,
        "source_receipt_path": item.source_receipt_path,
        "source_response_chain_digest": item.source_response_chain_digest,
        "quality_scope": item.quality_scope,
        "quality_decision": item.quality_decision,
        "freshness_basis": item.freshness_basis,
        "out_of_order_count": item.out_of_order_count,
        "misaligned_timestamp_count": item.misaligned_timestamp_count,
        "invalid_ohlc_count": item.invalid_ohlc_count,
        "negative_volume_count": item.negative_volume_count,
        "classification_windows": item.classification_windows or {},
    }


def _market_file_scan_digest(item: MarketFileEvidence) -> str:
    return canonical_digest(_market_file_scan_payload(item))


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        # Match configuration_management._sha256 exactly so every persisted
        # version and bundle can be independently recomputed by the owner API.
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _aware_utc(value).isoformat().replace("+00:00", "Z")
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_parameter(value: Any) -> str:
    return canonical_json(value)


def _quoted_schema(connection: Connection) -> str:
    schema = connection.execute(text("SELECT current_schema()" )).scalar_one()
    return connection.dialect.identifier_preparer.quote_schema(schema)


def _constraint_is_validated(
    connection: Connection,
    *,
    table_name: str,
    constraint_name: str,
) -> bool:
    value = connection.execute(
        text(
            "SELECT constraint_row.convalidated FROM pg_constraint constraint_row "
            "JOIN pg_class table_row ON table_row.oid=constraint_row.conrelid "
            "JOIN pg_namespace namespace_row ON namespace_row.oid=table_row.relnamespace "
            "WHERE namespace_row.nspname=current_schema() "
            "AND table_row.relname=:table AND constraint_row.conname=:constraint"
        ),
        {"table": table_name, "constraint": constraint_name},
    ).scalar_one()
    return bool(value)


def _ensure_restrict_foreign_key(
    connection: Connection,
    *,
    table_name: str,
    columns: tuple[str, ...],
    referred_table: str,
    referred_columns: tuple[str, ...],
    constraint_name: str,
) -> None:
    """Repair one missing FK without accepting orphans or doing an initial scan."""

    inspector = inspect(connection)
    # A narrow fake inspector is used by the SQL-contract unit test.  PostgreSQL
    # execution always exposes full FK introspection and takes the strict path.
    if not hasattr(inspector, "get_foreign_keys"):
        return
    same_columns = [
        foreign_key
        for foreign_key in inspector.get_foreign_keys(table_name)
        if tuple(foreign_key.get("constrained_columns") or ()) == columns
    ]
    exact = [
        foreign_key
        for foreign_key in same_columns
        if foreign_key.get("referred_table") == referred_table
        and tuple(foreign_key.get("referred_columns") or ()) == referred_columns
        and str((foreign_key.get("options") or {}).get("ondelete") or "NO ACTION").upper()
        == "RESTRICT"
    ]
    if len(same_columns) > 1 or (same_columns and not exact):
        raise StrategyPlatformTask1Blocked(
            f"foreign-key identity conflict: {table_name}({','.join(columns)})"
        )

    quote = connection.dialect.identifier_preparer.quote
    schema_name = connection.execute(text("SELECT current_schema()" )).scalar_one()
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(schema_name)
    qualified_table = f"{quoted_schema}.{quote(table_name)}"
    if exact:
        existing_name = exact[0].get("name")
        if not existing_name:
            raise StrategyPlatformTask1Blocked(
                f"foreign key has no auditable name: {table_name}({','.join(columns)})"
            )
        if not _constraint_is_validated(
            connection,
            table_name=table_name,
            constraint_name=str(existing_name),
        ):
            connection.execute(
                text(
                    f"ALTER TABLE {qualified_table} VALIDATE CONSTRAINT "
                    f"{quote(str(existing_name))}"
                )
            )
        return

    qualified_parent = f"{quoted_schema}.{quote(referred_table)}"
    join = " AND ".join(
        f"source.{quote(column)}=parent.{quote(parent_column)}"
        for column, parent_column in zip(columns, referred_columns)
    )
    source_present = " AND ".join(
        f"source.{quote(column)} IS NOT NULL" for column in columns
    )
    parent_absent = " AND ".join(
        f"parent.{quote(column)} IS NULL" for column in referred_columns
    )
    orphan_count = int(
        connection.execute(
            text(
                f"SELECT count(*) FROM {qualified_table} source LEFT JOIN "
                f"{qualified_parent} parent ON {join} WHERE ({source_present}) "
                f"AND ({parent_absent})"
            )
        ).scalar_one()
    )
    if orphan_count:
        raise StrategyPlatformTask1Blocked(
            f"cannot add foreign key {constraint_name}: orphan_count={orphan_count}"
        )
    connection.execute(
        text(
            f"ALTER TABLE {qualified_table} ADD CONSTRAINT {quote(constraint_name)} "
            f"FOREIGN KEY ({','.join(quote(column) for column in columns)}) "
            f"REFERENCES {qualified_parent} "
            f"({','.join(quote(column) for column in referred_columns)}) "
            "ON DELETE RESTRICT NOT VALID"
        )
    )
    connection.execute(
        text(
            f"ALTER TABLE {qualified_table} VALIDATE CONSTRAINT "
            f"{quote(constraint_name)}"
        )
    )


def _ensure_check_constraint(
    connection: Connection,
    *,
    table_name: str,
    constraint_name: str,
    expression: str,
) -> None:
    inspector = inspect(connection)
    if not hasattr(inspector, "get_check_constraints"):
        return
    existing = {
        constraint.get("name")
        for constraint in inspector.get_check_constraints(table_name)
    }
    quote = connection.dialect.identifier_preparer.quote
    schema_name = connection.execute(text("SELECT current_schema()" )).scalar_one()
    qualified_table = (
        f"{connection.dialect.identifier_preparer.quote_schema(schema_name)}."
        f"{quote(table_name)}"
    )
    if constraint_name not in existing:
        violation_count = int(
            connection.execute(
                text(f"SELECT count(*) FROM {qualified_table} WHERE NOT ({expression})")
            ).scalar_one()
        )
        if violation_count:
            raise StrategyPlatformTask1Blocked(
                f"cannot add check {constraint_name}: violation_count={violation_count}"
            )
        connection.execute(
            text(
                f"ALTER TABLE {qualified_table} ADD CONSTRAINT {quote(constraint_name)} "
                f"CHECK ({expression}) NOT VALID"
            )
        )
    if not _constraint_is_validated(
        connection,
        table_name=table_name,
        constraint_name=constraint_name,
    ):
        connection.execute(
            text(
                f"ALTER TABLE {qualified_table} VALIDATE CONSTRAINT "
                f"{quote(constraint_name)}"
            )
        )


def _ensure_unique_constraint(
    connection: Connection,
    *,
    table_name: str,
    columns: tuple[str, ...],
    constraint_name: str,
) -> None:
    """Add one auditable UNIQUE constraint after a duplicate preflight."""

    inspector = inspect(connection)
    if not hasattr(inspector, "get_unique_constraints"):
        return
    matching = [
        constraint
        for constraint in inspector.get_unique_constraints(table_name)
        if tuple(constraint.get("column_names") or ()) == columns
    ]
    if matching:
        if len(matching) != 1 or matching[0].get("name") != constraint_name:
            raise StrategyPlatformTask1Blocked(
                f"unique-constraint identity conflict: {table_name}({','.join(columns)})"
            )
        return

    quote = connection.dialect.identifier_preparer.quote
    schema_name = connection.execute(text("SELECT current_schema()" )).scalar_one()
    qualified_table = (
        f"{connection.dialect.identifier_preparer.quote_schema(schema_name)}."
        f"{quote(table_name)}"
    )
    present = " AND ".join(f"{quote(column)} IS NOT NULL" for column in columns)
    group_columns = ",".join(quote(column) for column in columns)
    duplicate_count = int(
        connection.execute(
            text(
                f"SELECT count(*) FROM (SELECT {group_columns} FROM {qualified_table} "
                f"WHERE {present} GROUP BY {group_columns} HAVING count(*) > 1) duplicates"
            )
        ).scalar_one()
    )
    if duplicate_count:
        raise StrategyPlatformTask1Blocked(
            f"cannot add unique constraint {constraint_name}: "
            f"duplicate_identity_count={duplicate_count}"
        )
    indexes = {
        index.get("name"): index
        for index in inspector.get_indexes(table_name)
        if index.get("name")
    }
    existing_index = indexes.get(constraint_name)
    if existing_index is not None:
        if (
            not existing_index.get("unique")
            or tuple(existing_index.get("column_names") or ()) != columns
        ):
            raise StrategyPlatformTask1Blocked(
                f"unique-index identity conflict: {constraint_name}"
            )
        connection.execute(
            text(
                f"ALTER TABLE {qualified_table} ADD CONSTRAINT "
                f"{quote(constraint_name)} UNIQUE USING INDEX {quote(constraint_name)}"
            )
        )
        return
    connection.execute(
        text(
            f"ALTER TABLE {qualified_table} ADD CONSTRAINT {quote(constraint_name)} "
            f"UNIQUE ({group_columns})"
        )
    )


def _ensure_migration_run_evidence_contract(connection: Connection) -> None:
    inspector = inspect(connection)
    if not hasattr(inspector, "get_columns"):
        return
    table_name = "strategy_platform_migration_runs"
    columns = {column["name"]: column for column in inspector.get_columns(table_name)}
    required = {"evidence_manifest", "evidence_manifest_digest"}
    missing = sorted(required - set(columns))
    row_count = int(
        connection.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one()
    )
    if missing:
        if row_count:
            raise StrategyPlatformTask1Blocked(
                "migration-run evidence columns are missing on a non-empty audit table"
            )
        connection.execute(
            text(
                "ALTER TABLE strategy_platform_migration_runs "
                "ADD COLUMN IF NOT EXISTS evidence_manifest JSON NOT NULL DEFAULT '{}'::json, "
                "ADD COLUMN IF NOT EXISTS evidence_manifest_digest VARCHAR(64) NOT NULL"
            )
        )
        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns(table_name)
        }
    nullable = [
        column
        for column in required
        if bool(columns[column].get("nullable", True))
    ]
    if nullable:
        if row_count:
            invalid = int(
                connection.execute(
                    text(
                        "SELECT count(*) FROM strategy_platform_migration_runs "
                        "WHERE evidence_manifest IS NULL "
                        "OR evidence_manifest_digest IS NULL"
                    )
                ).scalar_one()
            )
            if invalid:
                raise StrategyPlatformTask1Blocked(
                    "migration-run evidence columns contain unauditable NULL values"
                )
        connection.execute(
            text(
                "ALTER TABLE strategy_platform_migration_runs "
                "ALTER COLUMN evidence_manifest SET DEFAULT '{}'::json, "
                "ALTER COLUMN evidence_manifest SET NOT NULL, "
                "ALTER COLUMN evidence_manifest_digest SET NOT NULL"
            )
        )

    _ensure_check_constraint(
        connection,
        table_name=table_name,
        constraint_name="strategy_platform_migration_runs_evidence_digest_check",
        expression="length(evidence_manifest_digest) = 64",
    )
    _ensure_check_constraint(
        connection,
        table_name=table_name,
        constraint_name="strategy_platform_migration_runs_terminal_shape_check",
        expression=(
            "status NOT IN ('SUCCEEDED','FAILED','BLOCKED') OR "
            "(status='SUCCEEDED' AND target_snapshot_digest IS NOT NULL "
            "AND report_digest IS NOT NULL AND completed_at IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL "
            "AND evidence_manifest::jsonb <> '{}'::jsonb) OR "
            "(status IN ('FAILED','BLOCKED') AND completed_at IS NOT NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL)"
        ),
    )


def _ensure_market_quality_v13_contract(connection: Connection) -> None:
    """Add a scoped receipt contract without reclassifying legacy receipts."""

    table_name = "market_data_quality_receipts"
    inspector = inspect(connection)
    if not hasattr(inspector, "get_columns"):
        return
    columns = {
        column["name"] for column in inspector.get_columns(table_name)
    }
    additions = {
        "idempotency_key": "VARCHAR(64)",
        "quality_scope": "VARCHAR(80)",
        "quality_decision": "VARCHAR(80)",
        "file_identity_digest": "VARCHAR(64)",
        "source_identity_digest": "VARCHAR(64)",
        "aggregate_receipt_digest": "VARCHAR(64)",
        "migration_artifact_digest": "VARCHAR(64)",
        "freshness_basis": "VARCHAR(80)",
    }
    missing = [(name, sql_type) for name, sql_type in additions.items() if name not in columns]
    if missing:
        connection.execute(
            text(
                "ALTER TABLE market_data_quality_receipts "
                + ",".join(
                    f"ADD COLUMN IF NOT EXISTS {name} {sql_type}"
                    for name, sql_type in missing
                )
            )
        )
    _ensure_unique_constraint(
        connection,
        table_name=table_name,
        columns=("idempotency_key",),
        constraint_name="market_data_quality_receipts_idempotency_unique",
    )
    _ensure_check_constraint(
        connection,
        table_name=table_name,
        constraint_name="market_data_quality_receipts_v13_scope_check",
        expression=(
            "contract_version <> 'market-data-quality-v13-v1' OR ("
            "idempotency_key IS NOT NULL AND quality_scope="
            "'MIGRATION_SOURCE_CONSISTENCY_AS_OF_SOURCE_RECEIPT' AND "
            "quality_decision='NOT_STRATEGY_QUALIFICATION' AND "
            "file_identity_digest IS NOT NULL AND length(file_identity_digest)=64 AND "
            "source_identity_digest IS NOT NULL AND length(source_identity_digest)=64 AND "
            "aggregate_receipt_digest IS NOT NULL "
            "AND length(aggregate_receipt_digest)=64 AND "
            "migration_artifact_digest IS NOT NULL "
            "AND length(migration_artifact_digest)=64 AND "
            "freshness_basis='ORIGINAL_AGGREGATE_RECEIPT_DOWNLOADED_AT' AND "
            "freshness_seconds IS NULL AND status='PASSED')"
        ),
    )


def install_strategy_platform_v13_task1_schema(connection: Connection) -> None:
    """Install the complete additive P0/P1 schema and transitional lineage FKs."""

    existing = set(inspect(connection).get_table_names())
    required = {
        "configuration_versions",
        "configuration_bundle_snapshots",
        "strategy_deployments",
        "strategy_research_candidates",
        "strategy_targets",
        "trade_intents",
    }
    missing = sorted(required - existing)
    if missing:
        raise StrategyPlatformTask1Blocked(
            "V1.3 Task 1 schema prerequisites are missing: " + ", ".join(missing)
        )

    for table_name in STRATEGY_PLATFORM_V13_EXTENSION_TABLES:
        Base.metadata.tables[table_name].create(bind=connection, checkfirst=True)

    # Partial installs may already have the legacy scalar overlap.  Preserve it
    # without duplicating the source of truth; new profile versions use the
    # per-timeframe candle-count object while each row is constrained to one
    # representation.
    connection.execute(
        text(
            "ALTER TABLE market_data_policy_versions "
            "ADD COLUMN IF NOT EXISTS overlap_by_timeframe JSON, "
            "ALTER COLUMN incremental_overlap_seconds DROP NOT NULL"
        )
    )
    _ensure_check_constraint(
        connection,
        table_name="market_data_policy_versions",
        constraint_name="market_data_policy_versions_overlap_shape_check",
        expression=(
            "(overlap_by_timeframe IS NULL "
            "AND incremental_overlap_seconds IS NOT NULL "
            "AND incremental_overlap_seconds >= 0) OR "
            "(overlap_by_timeframe IS NOT NULL "
            "AND incremental_overlap_seconds IS NULL "
            "AND length(CAST(overlap_by_timeframe AS TEXT)) > 2)"
        ),
    )

    _ensure_migration_run_evidence_contract(connection)
    _ensure_market_quality_v13_contract(connection)

    connection.execute(
        text(
            "ALTER TABLE strategy_deployments "
            "ADD COLUMN IF NOT EXISTS strategy_target_id BIGINT; "
            "ALTER TABLE trade_intents "
            "ADD COLUMN IF NOT EXISTS deployment_id BIGINT, "
            "ADD COLUMN IF NOT EXISTS signal_evaluation_id BIGINT, "
            "ADD COLUMN IF NOT EXISTS runtime_instance_row_id BIGINT"
        )
    )
    foreign_keys = (
        (
            "strategy_deployments",
            ("strategy_target_id",),
            "strategy_targets",
            ("id",),
            "strategy_deployments_strategy_target_id_fkey",
        ),
        (
            "trade_intents",
            ("deployment_id",),
            "strategy_deployments",
            ("id",),
            "trade_intents_deployment_id_fkey",
        ),
        (
            "trade_intents",
            ("signal_evaluation_id",),
            "signal_evaluations",
            ("id",),
            "trade_intents_signal_evaluation_id_fkey",
        ),
        (
            "trade_intents",
            ("runtime_instance_row_id",),
            "strategy_runtime_instances",
            ("id",),
            "trade_intents_runtime_instance_row_id_fkey",
        ),
        (
            "execution_target_definition_versions",
            ("exchange_adapter_key",),
            "adapter_definitions",
            ("adapter_key",),
            "exec_target_versions_exchange_adapter_fkey",
        ),
        (
            "execution_target_definition_versions",
            ("runtime_adapter_key",),
            "adapter_definitions",
            ("adapter_key",),
            "exec_target_versions_runtime_adapter_fkey",
        ),
        (
            "validation_window_config_sets",
            ("default_classifier_adapter_key",),
            "adapter_definitions",
            ("adapter_key",),
            "validation_window_sets_classifier_adapter_fkey",
        ),
        (
            "validation_window_configs",
            ("classifier_adapter_key",),
            "adapter_definitions",
            ("adapter_key",),
            "validation_windows_classifier_adapter_fkey",
        ),
        (
            "quality_gate_rules",
            ("evaluation_adapter_key",),
            "adapter_definitions",
            ("adapter_key",),
            "quality_gate_rules_evaluation_adapter_fkey",
        ),
    )
    for table_name, columns, referred_table, referred_columns, name in foreign_keys:
        _ensure_restrict_foreign_key(
            connection,
            table_name=table_name,
            columns=columns,
            referred_table=referred_table,
            referred_columns=referred_columns,
            constraint_name=name,
        )
    connection.execute(
        text(
            "ALTER TABLE strategy_research_candidates "
            "DROP CONSTRAINT IF EXISTS strategy_research_candidates_pair_check, "
            "DROP CONSTRAINT IF EXISTS strategy_research_candidates_timeframe_check, "
            "ADD CONSTRAINT strategy_research_candidates_pair_check "
            "CHECK (pair IS NULL OR length(pair) > 0), "
            "ADD CONSTRAINT strategy_research_candidates_timeframe_check "
            "CHECK (timeframe IS NULL OR length(timeframe) > 0); "
            "ALTER TABLE strategy_deployments "
            "DROP CONSTRAINT IF EXISTS strategy_deployments_active_slot_check, "
            "ADD CONSTRAINT strategy_deployments_active_slot_check CHECK ("
            "(status='ACTIVE' AND active_slot > 0) OR "
            "(status='DISABLED' AND active_slot IS NULL))"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS strategy_deployments_strategy_target_idx "
            "ON strategy_deployments(strategy_target_id, status, created_at); "
            "CREATE INDEX IF NOT EXISTS trade_intents_deployment_signal_idx "
            "ON trade_intents(deployment_id, signal_evaluation_id, created_at); "
            "CREATE INDEX IF NOT EXISTS trade_intents_runtime_instance_idx "
            "ON trade_intents(runtime_instance_row_id, created_at)"
        )
    )
    _install_extension_immutability_guards(connection)


def _install_extension_immutability_guards(connection: Connection) -> None:
    schema = _quoted_schema(connection)
    connection.execute(
        text(
            f"""
            CREATE OR REPLACE FUNCTION {schema}.guard_strategy_platform_migration_audit()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'strategy platform migration audit is append-only';
                END IF;
                IF TG_TABLE_NAME <> 'strategy_platform_migration_runs' THEN
                    RAISE EXCEPTION 'strategy platform migration evidence is immutable';
                END IF;
                IF OLD.id IS DISTINCT FROM NEW.id
                   OR OLD.migration_key IS DISTINCT FROM NEW.migration_key
                   OR OLD.execution_scope IS DISTINCT FROM NEW.execution_scope
                   OR OLD.source_schema_version IS DISTINCT FROM NEW.source_schema_version
                   OR OLD.target_schema_version IS DISTINCT FROM NEW.target_schema_version
                   OR OLD.source_snapshot_digest IS DISTINCT FROM NEW.source_snapshot_digest
                   OR OLD.operator_identity IS DISTINCT FROM NEW.operator_identity
                   OR OLD.request_id IS DISTINCT FROM NEW.request_id
                   OR OLD.unknown_dimensions::jsonb IS DISTINCT FROM
                      NEW.unknown_dimensions::jsonb
                   OR OLD.evidence_manifest::jsonb IS DISTINCT FROM
                      NEW.evidence_manifest::jsonb
                   OR OLD.evidence_manifest_digest IS DISTINCT FROM NEW.evidence_manifest_digest
                   OR OLD.report_path IS DISTINCT FROM NEW.report_path
                   OR OLD.destructive_write_count IS DISTINCT FROM NEW.destructive_write_count
                   OR OLD.overwritten_row_count IS DISTINCT FROM NEW.overwritten_row_count
                   OR OLD.deleted_row_count IS DISTINCT FROM NEW.deleted_row_count
                   OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                    RAISE EXCEPTION 'strategy platform migration run identity is immutable';
                END IF;
                IF NOT (
                    OLD.status = NEW.status
                    OR OLD.status = 'PLANNED' AND NEW.status IN ('RUNNING','BLOCKED')
                    OR OLD.status = 'RUNNING' AND NEW.status IN ('RECONCILING','FAILED','BLOCKED')
                    OR OLD.status = 'RECONCILING' AND NEW.status IN ('SUCCEEDED','FAILED','BLOCKED')
                ) THEN
                    RAISE EXCEPTION 'illegal strategy platform migration run transition';
                END IF;
                IF OLD.status IN ('SUCCEEDED','FAILED','BLOCKED') THEN
                    RAISE EXCEPTION 'terminal strategy platform migration run is immutable';
                END IF;
                RETURN NEW;
            END
            $$;
            """
        )
    )
    for table_name in (
        "strategy_platform_migration_runs",
        "strategy_platform_migration_table_snapshots",
        "strategy_platform_migration_entity_mappings",
        "strategy_platform_migration_conflicts",
    ):
        connection.execute(
            text(
                f"DROP TRIGGER IF EXISTS {table_name}_append_only ON {schema}.{table_name}; "
                f"CREATE TRIGGER {table_name}_append_only "
                f"BEFORE UPDATE OR DELETE ON {schema}.{table_name} "
                f"FOR EACH ROW EXECUTE FUNCTION "
                f"{schema}.guard_strategy_platform_migration_audit()"
            )
        )

    connection.execute(
        text(
            f"""
            CREATE OR REPLACE FUNCTION
              {schema}.guard_strategy_platform_v13_bundle_required()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.configuration_bundle_snapshot_id IS NULL THEN
                    RAISE EXCEPTION
                      'new V1.3 workflow rows require configuration bundle snapshots';
                END IF;
                RETURN NEW;
            END
            $$;
            """
        )
    )
    for table_name in (
        "research_jobs",
        "strategy_deployments",
        "strategy_validation_plans",
    ):
        connection.execute(
            text(
                f"DROP TRIGGER IF EXISTS {table_name}_v13_bundle_required "
                f"ON {schema}.{table_name}; "
                f"CREATE TRIGGER {table_name}_v13_bundle_required "
                f"BEFORE INSERT ON {schema}.{table_name} FOR EACH ROW EXECUTE FUNCTION "
                f"{schema}.guard_strategy_platform_v13_bundle_required()"
            )
        )

    connection.execute(
        text(
            f"""
            CREATE OR REPLACE FUNCTION
              {schema}.guard_strategy_platform_qualified_mapping()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE
                summary_status text;
                summary_plan_id bigint;
                summary_required bigint;
                summary_passed bigint;
                summary_failed bigint;
                plan_status text;
                relation_matches boolean;
            BEGIN
                IF NEW.quality_status_asserted IS DISTINCT FROM 'QUALIFIED' THEN
                    RETURN NEW;
                END IF;
                IF NEW.dynamic_quality_evidence_id IS NULL THEN
                    RAISE EXCEPTION 'QUALIFIED mapping requires dynamic quality evidence';
                END IF;

                SELECT summary.status,summary.validation_plan_id,
                       summary.required_window_count,summary.passed_window_count,
                       summary.failed_window_count,plan.status
                  INTO summary_status,summary_plan_id,summary_required,
                       summary_passed,summary_failed,plan_status
                  FROM {schema}.strategy_evaluation_summaries summary
                  JOIN {schema}.strategy_validation_plans plan
                    ON plan.id=summary.validation_plan_id
                 WHERE summary.id=NEW.dynamic_quality_evidence_id;
                IF NOT FOUND
                   OR summary_status IS DISTINCT FROM 'QUALIFIED'
                   OR plan_status IS DISTINCT FROM 'QUALIFIED'
                   OR summary_required <= 0
                   OR summary_passed IS DISTINCT FROM summary_required
                   OR summary_failed IS DISTINCT FROM 0 THEN
                    RAISE EXCEPTION
                      'QUALIFIED mapping requires one complete QUALIFIED dynamic summary';
                END IF;

                relation_matches := (
                    NEW.target_table='strategy_evaluation_summaries'
                    AND NEW.target_primary_key=
                        NEW.dynamic_quality_evidence_id::text
                ) OR (
                    NEW.source_table='strategy_validation_plans'
                    AND NEW.source_primary_key=summary_plan_id::text
                ) OR (
                    NEW.evidence_snapshot::jsonb ? 'validation_plan_id'
                    AND NEW.evidence_snapshot::jsonb->>'validation_plan_id'=
                        summary_plan_id::text
                );
                IF relation_matches IS NOT TRUE THEN
                    RAISE EXCEPTION
                      'QUALIFIED mapping evidence is unrelated to the mapped entity';
                END IF;
                RETURN NEW;
            END
            $$;
            DROP TRIGGER IF EXISTS strategy_platform_mapping_qualified_guard
              ON {schema}.strategy_platform_migration_entity_mappings;
            CREATE TRIGGER strategy_platform_mapping_qualified_guard
              BEFORE INSERT OR UPDATE OF quality_status_asserted,
                dynamic_quality_evidence_id,target_table,target_primary_key,
                source_table,source_primary_key,evidence_snapshot
              ON {schema}.strategy_platform_migration_entity_mappings
              FOR EACH ROW EXECUTE FUNCTION
              {schema}.guard_strategy_platform_qualified_mapping();
            """
        )
    )

    connection.execute(
        text(
            f"""
            CREATE OR REPLACE FUNCTION {schema}.guard_strategy_platform_v13_config_child()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE
                old_payload jsonb;
                new_payload jsonb;
                old_version_id bigint;
                new_version_id bigint;
                version_status text;
            BEGIN
                old_payload := CASE WHEN TG_OP='INSERT' THEN '{{}}'::jsonb ELSE to_jsonb(OLD) END;
                new_payload := CASE WHEN TG_OP='DELETE' THEN '{{}}'::jsonb ELSE to_jsonb(NEW) END;
                old_version_id := COALESCE(
                    NULLIF(old_payload->>'configuration_version_id','')::bigint,
                    CASE WHEN TG_TABLE_NAME='research_target_config_sets'
                         THEN NULLIF(old_payload->>'id','')::bigint END,
                    NULLIF(old_payload->>'config_set_id','')::bigint,
                    NULLIF(old_payload->>'generation_profile_version_id','')::bigint,
                    NULLIF(old_payload->>'profile_version_id','')::bigint
                );
                new_version_id := COALESCE(
                    NULLIF(new_payload->>'configuration_version_id','')::bigint,
                    CASE WHEN TG_TABLE_NAME='research_target_config_sets'
                         THEN NULLIF(new_payload->>'id','')::bigint END,
                    NULLIF(new_payload->>'config_set_id','')::bigint,
                    NULLIF(new_payload->>'generation_profile_version_id','')::bigint,
                    NULLIF(new_payload->>'profile_version_id','')::bigint
                );
                IF TG_OP='UPDATE' AND old_version_id IS DISTINCT FROM new_version_id THEN
                    RAISE EXCEPTION 'configuration child version identity is immutable';
                END IF;
                SELECT lifecycle_status INTO version_status
                  FROM {schema}.configuration_versions
                 WHERE id=COALESCE(new_version_id,old_version_id);
                IF version_status IS DISTINCT FROM 'DRAFT' THEN
                    RAISE EXCEPTION 'validated configuration children are immutable';
                END IF;
                RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
            END
            $$;
            """
        )
    )
    for table_name in (
        "strategy_source_definition_versions",
        "trigger_source_definition_versions",
        "timeframe_definition_versions",
        "research_target_config_sets",
        "research_target_configs",
        "strategy_family_definition_versions",
        "provider_model_config_versions",
        "generation_profile_versions",
        "generation_profile_families",
        "scoring_profile_versions",
        "scoring_rules",
        "diversity_profile_versions",
        "diversity_rules",
        "worker_execution_profile_versions",
        "scheduler_profile_versions",
        "market_data_policy_versions",
        "evidence_freshness_profile_versions",
        "evidence_freshness_rules",
        "monitoring_profile_versions",
        "promotion_profile_versions",
        "promotion_rules",
        "risk_profile_versions",
        "risk_rules",
        "capacity_profile_versions",
        "runtime_profile_versions",
        "deployment_profile_versions",
        "market_data_profile_versions",
        "optimization_profile_versions",
        "ui_presentation_profile_versions",
        "research_profile_versions",
    ):
        connection.execute(
            text(
                f"DROP TRIGGER IF EXISTS {table_name}_draft_only ON {schema}.{table_name}; "
                f"CREATE TRIGGER {table_name}_draft_only "
                f"BEFORE INSERT OR UPDATE OR DELETE ON {schema}.{table_name} "
                f"FOR EACH ROW EXECUTE FUNCTION "
                f"{schema}.guard_strategy_platform_v13_config_child()"
            )
        )

    connection.execute(
        text(
            f"""
            CREATE OR REPLACE FUNCTION {schema}.guard_strategy_submission_payload()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE
                payload jsonb;
            BEGIN
                payload := COALESCE(NEW.payload_snapshot::jsonb, '{{}}'::jsonb);
                IF jsonb_path_exists(payload, '$.**.api_key')
                   OR jsonb_path_exists(payload, '$.**.api_secret')
                   OR jsonb_path_exists(payload, '$.**.secret_value')
                   OR jsonb_path_exists(payload, '$.**.password')
                   OR jsonb_path_exists(payload, '$.**.passphrase')
                   OR jsonb_path_exists(payload, '$.**.private_key') THEN
                    RAISE EXCEPTION 'strategy submission payload cannot contain secret values';
                END IF;
                IF jsonb_path_exists(payload, '$.**.python_code')
                   OR jsonb_path_exists(payload, '$.**.callable_source')
                   OR jsonb_path_exists(payload, '$.**.executable_code')
                   OR jsonb_path_exists(payload, '$.**.shell_command') THEN
                    RAISE EXCEPTION 'strategy submission payload cannot contain executable code';
                END IF;
                RETURN NEW;
            END
            $$;
            DROP TRIGGER IF EXISTS strategy_submissions_safe_payload
              ON {schema}.strategy_submissions;
            CREATE TRIGGER strategy_submissions_safe_payload
              BEFORE INSERT OR UPDATE OF payload_snapshot
              ON {schema}.strategy_submissions
              FOR EACH ROW EXECUTE FUNCTION
              {schema}.guard_strategy_submission_payload();
            """
        )
    )


def validate_market_inventory(
    market_inventory: Sequence[MarketFileEvidence],
) -> tuple[MarketFileEvidence, ...]:
    """Fail closed unless the six current public-data targets are proven."""

    records = tuple(market_inventory)
    expected = {
        (pair, timeframe)
        for pair in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT")
        for timeframe in ("5m", "15m")
    }
    identities = {(item.pair, item.timeframe) for item in records}
    if identities != expected or len(records) != len(expected):
        raise StrategyPlatformTask1Blocked(
            "market inventory must contain exactly BTC/ETH/SOL x 5m/15m"
        )
    for item in records:
        expected_interval = {"5m": 300, "15m": 900}.get(item.timeframe)
        if (
            item.exchange.lower() != "okx"
            or item.market_type.lower() != "futures"
            or item.data_kind.lower() != "futures"
            or item.file_format.lower() not in {"feather", "parquet"}
            or item.size_bytes <= 0
            or item.row_count <= 0
            or _SHA256_RE.fullmatch(item.sha256) is None
            or item.expected_interval_seconds <= 0
            or item.expected_interval_seconds != expected_interval
            or item.gap_count != 0
            or item.duplicate_count != 0
            or item.null_count != 0
            or item.first_open_at >= item.last_open_at
            or item.last_open_at >= item.last_close_at
            or item.freshness_status not in {"PASSED", "UNKNOWN"}
            or _SHA256_RE.fullmatch(str(item.source_receipt_digest or "")) is None
            or _aware_utc(item.observed_at) < _aware_utc(item.last_close_at)
            or _aware_utc(item.observed_at)
            > datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(minutes=5)
            or item.relative_path.startswith(("/", "\\"))
            or not item.absolute_path.startswith("/")
        ):
            raise StrategyPlatformTask1Blocked(
                f"market inventory evidence is not acceptance-grade: "
                f"{item.pair} {item.timeframe}"
            )
        windows = item.classification_windows or {}
        required_keys = {"primary_bear", "wf_bull", "wf_range", "oos", "wf_bear"}
        if set(windows) != required_keys:
            raise StrategyPlatformTask1Blocked(
                f"classification evidence is incomplete: {item.pair} {item.timeframe}"
            )
        for key, evidence in windows.items():
            spec = _WINDOW_SPECS[key]
            expected_bounds = spec.get(
                "sol" if item.pair.startswith("SOL/") else "default"
            ) or spec["default"]
            if not _valid_classification_evidence(
                key,
                evidence,
                item.sha256,
                expected_start_at=expected_bounds[0],
                expected_end_at=expected_bounds[1],
                expected_interval_seconds=item.expected_interval_seconds,
                file_first_open_at=item.first_open_at,
                file_last_close_at=item.last_close_at,
            ):
                raise StrategyPlatformTask1Blocked(
                    f"classification evidence is invalid: "
                    f"{item.pair} {item.timeframe} {key}"
                )
    return records


def validate_task1_evidence_manifest(
    evidence_manifest: Mapping[str, Any],
    inventory: Sequence[MarketFileEvidence],
) -> dict[str, Any]:
    """Bind scanner, source, adapter, and file identities to one migration run."""

    if not isinstance(evidence_manifest, Mapping):
        raise StrategyPlatformTask1Blocked("Task 1 evidence manifest must be an object")
    manifest = dict(evidence_manifest)
    legacy = manifest.get("legacy_aggregate_receipt")
    corrected = manifest.get("corrected_matrix")
    snapshot = manifest.get("market_snapshot")
    files = manifest.get("files")
    if (
        manifest.get("schema_version")
        != "strategy-platform-v13-migration-market-evidence-v1"
        or manifest.get("status_scope")
        != "MIGRATION_SOURCE_CONSISTENCY_AS_OF_SOURCE_RECEIPT"
        or manifest.get("freshness_basis")
        != "ORIGINAL_AGGREGATE_RECEIPT_DOWNLOADED_AT"
        or not isinstance(manifest.get("artifact_generation_delay_seconds"), int)
        or int(manifest["artifact_generation_delay_seconds"]) < 0
        or not isinstance(legacy, Mapping)
        or legacy.get("status") != "BLOCKED"
        or not isinstance(corrected, Mapping)
        or corrected.get("status") != "PASSED"
        or not isinstance(snapshot, Mapping)
        or snapshot.get("status") != "PASSED"
        or not isinstance(files, list)
    ):
        raise StrategyPlatformTask1Blocked(
            "Task 1 evidence manifest safety/provenance shape is invalid"
        )
    digest_values = (
        manifest.get("artifact_digest"),
        manifest.get("artifact_file_sha256"),
        legacy.get("sha256"),
        legacy.get("snapshot_digest"),
        legacy.get("report_digest"),
        corrected.get("artifact_sha256"),
        corrected.get("snapshot_digest"),
        corrected.get("report_digest"),
        snapshot.get("snapshot_digest"),
        snapshot.get("report_digest"),
    )
    if any(_SHA256_RE.fullmatch(str(value or "")) is None for value in digest_values):
        raise StrategyPlatformTask1Blocked(
            "Task 1 evidence manifest contains an invalid digest"
        )
    by_target = {
        str(entry.get("target_key")): entry
        for entry in files
        if isinstance(entry, Mapping) and isinstance(entry.get("target_key"), str)
    }
    if len(by_target) != len(inventory) or len(files) != len(inventory):
        raise StrategyPlatformTask1Blocked(
            "Task 1 evidence manifest file cardinality is invalid"
        )
    for item in inventory:
        target_key = (
            f"{item.exchange.lower()}|{item.market_type.lower()}|"
            f"{item.pair}|{item.timeframe}|{item.data_kind.lower()}"
        )
        entry = by_target.get(target_key)
        if (
            entry is None
            or item.receipt_id is not None
            or item.freshness_status != "UNKNOWN"
            or item.quality_scope
            != "MIGRATION_SOURCE_CONSISTENCY_AS_OF_SOURCE_RECEIPT"
            or item.quality_decision != "NOT_STRATEGY_QUALIFICATION"
            or item.freshness_basis
            != "ORIGINAL_AGGREGATE_RECEIPT_DOWNLOADED_AT"
            or item.inspected_at is None
            or _aware_utc(item.inspected_at) != _aware_utc(item.observed_at)
            or item.out_of_order_count != 0
            or item.misaligned_timestamp_count != 0
            or item.invalid_ohlc_count != 0
            or item.negative_volume_count != 0
            or not item.source_type
            or not item.source_receipt_path
            or item.source_receipt_path.startswith(("/", "\\"))
            or _SHA256_RE.fullmatch(str(item.file_identity_digest or "")) is None
            or _SHA256_RE.fullmatch(str(item.source_identity_digest or "")) is None
            or _SHA256_RE.fullmatch(str(item.source_response_chain_digest or "")) is None
            or item.aggregate_receipt_digest != corrected.get("artifact_sha256")
            or item.migration_artifact_digest != manifest.get("artifact_digest")
            or entry.get("file_identity_digest") != item.file_identity_digest
            or entry.get("source_identity_digest") != item.source_identity_digest
            or entry.get("file_sha256") != item.sha256
            or entry.get("source_receipt_digest") != item.source_receipt_digest
        ):
            raise StrategyPlatformTask1Blocked(
                f"Task 1 market provenance is invalid: {item.pair} {item.timeframe}"
            )

    project_root = Path(__file__).resolve().parents[3]
    installed = validate_declared_adapter_coverage(project_root)
    adapter_manifest = {
        "digest": installed_adapter_manifest_digest(installed),
        "adapter_count": len(installed),
        "adapters": [
            {
                "adapter_key": adapter.adapter_key,
                "source_ref": adapter.source_ref,
                "source_sha256": adapter.source_sha256,
            }
            for adapter in installed
        ],
    }
    combined = {**manifest, "installed_adapter_manifest": adapter_manifest}
    # Force canonical serialization now; NaN, executable objects, and unstable
    # values are rejected before any migration audit row is created.
    canonical_json(combined)
    return combined


def verify_market_artifact(item: MarketFileEvidence) -> None:
    """Re-scan one exact candle artifact instead of trusting caller JSON.

    The filesystem read is deliberately local and read-only.  It is required by
    the real-data migration entrypoint, but kept separate from the structural
    inventory validator so contract tests can use synthetic metadata.
    """

    import pandas as pd

    path = Path(item.absolute_path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise StrategyPlatformTask1Blocked(
            f"market artifact is not a regular absolute file: "
            f"{item.pair} {item.timeframe}"
        )
    stat_before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    digest_before = digest.hexdigest()
    if stat_before.st_size != item.size_bytes or digest_before != item.sha256:
        raise StrategyPlatformTask1Blocked(
            f"market artifact size/digest mismatch: {item.pair} {item.timeframe}"
        )
    try:
        if item.file_format.lower() == "feather":
            frame = pd.read_feather(path)
        elif item.file_format.lower() == "parquet":
            frame = pd.read_parquet(path)
        else:  # already rejected by validate_market_inventory; fail closed here too.
            raise StrategyPlatformTask1Blocked(
                "Task 1 migration accepts only Feather/Parquet candle files"
            )
    except StrategyPlatformTask1Blocked:
        raise
    except Exception as exc:
        raise StrategyPlatformTask1Blocked(
            f"market artifact cannot be parsed: {item.pair} {item.timeframe}"
        ) from exc

    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns) or len(frame) != item.row_count:
        raise StrategyPlatformTask1Blocked(
            f"market artifact row/schema mismatch: {item.pair} {item.timeframe}"
        )
    timestamps = pd.to_datetime(frame["date"], utc=True, errors="coerce")
    if timestamps.isna().any():
        raise StrategyPlatformTask1Blocked(
            f"market artifact timestamp invalid: {item.pair} {item.timeframe}"
        )
    first_open = timestamps.iloc[0].to_pydatetime()
    last_open = timestamps.iloc[-1].to_pydatetime()
    if (
        _aware_utc(first_open) != _aware_utc(item.first_open_at)
        or _aware_utc(last_open) != _aware_utc(item.last_open_at)
        or _aware_utc(last_open + timedelta(seconds=item.expected_interval_seconds))
        != _aware_utc(item.last_close_at)
    ):
        raise StrategyPlatformTask1Blocked(
            f"market artifact time bounds mismatch: {item.pair} {item.timeframe}"
        )
    interval = item.expected_interval_seconds
    diffs = timestamps.diff().dt.total_seconds().dropna()
    duplicate_count = int(timestamps.duplicated().sum())
    out_of_order_count = int((diffs < 0).sum())
    aligned = timestamps.array.as_unit("ns").asi8 % (interval * 1_000_000_000)
    misaligned_count = int((aligned != 0).sum())
    sorted_diffs = (
        timestamps.drop_duplicates().sort_values().diff().dt.total_seconds().dropna()
    )
    gap_count = int(
        sum(max(0, math.floor(float(value) / interval) - 1) for value in sorted_diffs)
    )
    numeric = frame[["open", "high", "low", "close", "volume"]].apply(
        pd.to_numeric, errors="coerce"
    )
    null_count = int(numeric.isna().any(axis=1).sum())
    invalid_ohlc = int(
        (
            (numeric["high"] < numeric["low"])
            | (numeric["open"] > numeric["high"])
            | (numeric["open"] < numeric["low"])
            | (numeric["close"] > numeric["high"])
            | (numeric["close"] < numeric["low"])
        ).sum()
    )
    negative_volume = int((numeric["volume"] < 0).sum())
    if (
        duplicate_count != item.duplicate_count
        or out_of_order_count != item.out_of_order_count
        or misaligned_count != item.misaligned_timestamp_count
        or gap_count != item.gap_count
        or null_count != item.null_count
        or invalid_ohlc != item.invalid_ohlc_count
        or negative_volume != item.negative_volume_count
    ):
        raise StrategyPlatformTask1Blocked(
            f"market artifact interval/OHLCV mismatch: {item.pair} {item.timeframe}"
        )
    close = numeric["close"]
    for key, evidence in (item.classification_windows or {}).items():
        try:
            start = datetime.fromisoformat(str(evidence["start_at"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(evidence["end_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError) as exc:
            raise StrategyPlatformTask1Blocked(
                f"classification boundary invalid: {item.pair} {item.timeframe} {key}"
            ) from exc
        selected = (timestamps >= _aware_utc(start)) & (timestamps < _aware_utc(end))
        selected_close = close.loc[selected]
        if len(selected_close) < 2 or len(selected_close) != int(evidence["row_count"]):
            raise StrategyPlatformTask1Blocked(
                f"classification row evidence mismatch: {item.pair} {item.timeframe} {key}"
            )
        first_close = float(selected_close.iloc[0])
        last_close = float(selected_close.iloc[-1])
        close_return = last_close / first_close - 1.0
        if (
            abs(first_close - float(evidence["first_close"])) > 1e-12
            or abs(last_close - float(evidence["last_close"])) > 1e-12
            or abs(close_return - float(evidence["close_return"])) > 1e-12
        ):
            raise StrategyPlatformTask1Blocked(
                f"classification price evidence mismatch: "
                f"{item.pair} {item.timeframe} {key}"
            )
    stat_after = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if (
        stat_after.st_size != stat_before.st_size
        or stat_after.st_mtime_ns != stat_before.st_mtime_ns
        or digest.hexdigest() != digest_before
    ):
        raise StrategyPlatformTask1Blocked(
            f"market artifact changed during migration preflight: "
            f"{item.pair} {item.timeframe}"
        )


def verify_market_artifacts(inventory: Sequence[MarketFileEvidence]) -> None:
    for item in inventory:
        verify_market_artifact(item)


def _valid_classification_evidence(
    window_key: str,
    evidence: Mapping[str, Any],
    file_digest: str,
    *,
    expected_start_at: str | None = None,
    expected_end_at: str | None = None,
    expected_interval_seconds: int | None = None,
    file_first_open_at: datetime | None = None,
    file_last_close_at: datetime | None = None,
) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    digest = evidence.get("market_data_digest") or evidence.get("file_sha256")
    actual = evidence.get("actual_regime") or evidence.get("market_state")
    start_at = evidence.get("start_at") or evidence.get("window_start_at")
    end_at = evidence.get("end_at") or evidence.get("window_end_at")
    try:
        first_close = float(evidence["first_close"])
        last_close = float(evidence["last_close"])
        return_value = float(evidence["close_return"])
        row_count = int(evidence["row_count"])
    except (KeyError, TypeError, ValueError):
        return False
    if (
        digest != file_digest
        or first_close <= 0
        or last_close <= 0
        or row_count < 2
        or abs((last_close / first_close - 1.0) - return_value) > 1e-9
        or actual not in {"bull", "range", "bear"}
        or start_at is None
        or end_at is None
    ):
        return False
    try:
        parsed_start = datetime.fromisoformat(str(start_at).replace("Z", "+00:00"))
        parsed_end = datetime.fromisoformat(str(end_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if _aware_utc(parsed_start) >= _aware_utc(parsed_end):
        return False
    if expected_start_at is not None and expected_end_at is not None:
        expected_start = datetime.fromisoformat(expected_start_at.replace("Z", "+00:00"))
        expected_end = datetime.fromisoformat(expected_end_at.replace("Z", "+00:00"))
        if (
            _aware_utc(parsed_start) != _aware_utc(expected_start)
            or _aware_utc(parsed_end) != _aware_utc(expected_end)
        ):
            return False
        if (
            file_first_open_at is not None
            and _aware_utc(file_first_open_at) > _aware_utc(parsed_start)
        ) or (
            file_last_close_at is not None
            and _aware_utc(file_last_close_at) < _aware_utc(parsed_end)
        ):
            return False
    if expected_interval_seconds is not None:
        duration_seconds = int(
            (_aware_utc(parsed_end) - _aware_utc(parsed_start)).total_seconds()
        )
        expected_rows = duration_seconds // expected_interval_seconds
        if duration_seconds % expected_interval_seconds or row_count != expected_rows:
            return False
    computed = (
        "bull" if return_value >= 0.05 else "bear" if return_value <= -0.05 else "range"
    )
    if actual != computed:
        return False
    expected = None if window_key == "oos" else {
        "primary_bear": "bear",
        "wf_bull": "bull",
        "wf_range": "range",
        "wf_bear": "bear",
    }[window_key]
    return expected is None or actual == expected


_CONFIGURATION_TYPE_LABELS = {
    "strategy-source-definition": "策略来源定义",
    "trigger-source-definition": "触发来源定义",
    "timeframe-definition": "周期定义",
    "execution-target-definition": "执行目标定义",
    "metric-definition": "指标定义",
    "research-target-config-set": "研究目标配置",
    "validation-window-config-set": "动态验证窗口配置",
    "quality-gate-profile": "质量门配置",
    "provider-model-config": "模型供应商配置",
    "generation-profile": "生成配置",
    "strategy-family-definition": "策略家族定义",
    "scoring-profile": "评分配置",
    "diversity-profile": "多样性配置",
    "worker-execution-profile": "Worker 执行配置",
    "scheduler-profile": "调度配置",
    "market-data-policy": "行情策略配置",
    "evidence-freshness-profile": "证据新鲜度配置",
    "monitoring-profile": "监控配置",
    "promotion-profile": "晋级配置",
    "risk-profile": "风险配置",
    "capacity-profile": "容量配置",
    "runtime-profile": "运行配置",
    "deployment-profile": "部署总装配",
    "market-data-profile": "行情总装配",
    "optimization-profile": "优化总装配",
    "ui-presentation-profile": "UI 展示配置",
    "research-profile": "研究总装配",
    "legacy-validation-profile": "历史验证导入装配",
    "legacy-research-profile": "历史研究导入装配",
    "legacy-deployment-profile": "历史部署导入装配",
}

_ADAPTERS = (
    ("window-close-return-v1", "MARKET_CLASSIFIER", {"closed_candles": True}),
    ("threshold-comparison-v1", "QUALITY_EVALUATOR", {"operators": [">", ">=", "<=", "=="]}),
    ("weighted-component-score-v1", "SCORER", {"bounded_score": True}),
    ("linear-normalization-v1", "NORMALIZER", {"bounded_score": True}),
    ("diversity-threshold-v1", "DIVERSITY_EVALUATOR", {"metrics": ["signal_similarity", "pnl_correlation"]}),
    ("okx-public-candles-v1", "MARKET_DATA_DOWNLOADER", {"public_only": True, "closed_candles": True}),
    ("freqtrade-backtest-v1", "BACKTEST_RUNNER", {"exchange_connection": False}),
    ("deepseek-generation-v1", "GENERATION_PROVIDER", {"external_model": True}),
    ("freqtrade-hyperopt-v1", "OPTIMIZER", {"category": "parameter"}),
    ("ai-structure-optimization-v1", "OPTIMIZER", {"category": "structure"}),
    ("docker-runtime-v1", "RUNTIME_PROVIDER", {"demo_only": True}),
    ("simulated-runtime-v1", "RUNTIME_PROVIDER", {"demo_only": True, "exchange_connection": False}),
    (
        "okx-demo-exchange-v1",
        "EXCHANGE_PROVIDER",
        {
            "demo_only": True,
            "allow_real_funds": False,
            "single_writer_required": True,
        },
    ),
    ("strategy-import-v1", "SUBMISSION_SOURCE", {"metadata_only": True}),
)

_SOURCES = (
    ("ai_generated", "模型生成", False),
    ("formal_research", "正式研究", False),
    ("manual_import", "人工导入", True),
    ("external_model_api", "外部模型 API", True),
)

_TRIGGERS = (
    ("scheduled", "定时调度"),
    ("manual", "人工触发"),
    ("optimization", "优化结果"),
    ("import", "历史导入"),
)

_TIMEFRAMES = (
    ("5m", 300, "5 分钟", 10),
    ("15m", 900, "15 分钟", 20),
)

_FAMILIES = (
    ("TREND_BREAKOUT_FOLLOWING", "趋势突破跟随"),
    ("MOMENTUM_VOLUME_CONFIRMATION", "动量成交量确认"),
    ("MEAN_REVERSION", "均值回归"),
    ("VOLATILITY_BREAKOUT", "波动率突破"),
    ("PULLBACK_TREND_CONTINUATION", "回撤趋势延续"),
    ("RANGE_LIQUIDITY_FILTER", "区间流动性过滤"),
)

_METRICS = (
    ("total_score", "策略总分", "score", "strategy_scores.total_score"),
    ("total_trades", "交易笔数", "count", "backtest_results.total_trades"),
    ("net_profit_after_cost", "成本后净收益", "ratio", "backtest_results.metrics_snapshot"),
    ("max_drawdown", "最大回撤", "ratio", "backtest_results.max_drawdown_pct"),
    ("fee_per_side", "单边手续费", "ratio", "backtest profile"),
    ("slippage_per_side", "单边滑点", "ratio", "backtest profile"),
    ("lookahead_passed", "Lookahead 检查", "boolean", "validation evidence"),
    ("profit_score", "收益分", "score", "strategy_scores.profit_score"),
    ("risk_score", "风险分", "score", "strategy_scores.risk_score"),
    ("stability_score", "稳定性分", "score", "strategy_scores.stability_score"),
    ("quality_score", "质量分", "score", "strategy_scores.quality_score"),
)

_WINDOW_SPECS = {
    "primary_bear": {
        "purpose": "primary_scoring",
        "ordinal": 10,
        "name_zh": "主评分熊市窗口",
        "required": False,
        "expected": "bear",
        "default": ("2023-07-01T00:00:00Z", "2023-10-01T00:00:00Z"),
        "sol": ("2023-08-01T00:00:00Z", "2023-10-01T00:00:00Z"),
    },
    "wf_bull": {
        "purpose": "qualification",
        "ordinal": 20,
        "name_zh": "牛市验证窗口",
        "required": True,
        "expected": "bull",
        "default": ("2023-10-01T00:00:00Z", "2024-03-01T00:00:00Z"),
    },
    "wf_range": {
        "purpose": "qualification",
        "ordinal": 30,
        "name_zh": "震荡市验证窗口",
        "required": True,
        "expected": "range",
        "default": ("2024-03-01T00:00:00Z", "2024-06-29T00:00:00Z"),
        "sol": ("2024-03-01T00:00:00Z", "2024-05-01T00:00:00Z"),
    },
    "oos": {
        "purpose": "out_of_sample",
        "ordinal": 40,
        "name_zh": "独立样本外窗口",
        "required": True,
        "expected": None,
        "default": ("2025-01-01T00:00:00Z", "2025-10-01T00:00:00Z"),
    },
    "wf_bear": {
        "purpose": "qualification",
        "ordinal": 50,
        "name_zh": "熊市验证窗口",
        "required": True,
        "expected": "bear",
        "default": ("2025-10-01T00:00:00Z", "2026-02-01T00:00:00Z"),
    },
}


def configuration_digest(
    *, config_type: str, schema_version: str, payload: Mapping[str, Any]
) -> str:
    return canonical_digest(
        {
            "contract": "configuration-version-digest-v1",
            "config_type": config_type,
            "schema_version": schema_version,
            "payload_json": payload,
        }
    )


def configuration_bundle_digest(
    *,
    workflow_kind: str,
    scope_type: str,
    scope_key: str,
    aggregate_profile_version_id: int,
    resolved_versions_json: Mapping[str, int],
    resolved_digests_json: Mapping[str, str],
    capability_snapshot: Mapping[str, Any],
) -> str:
    required = {
        "demo_only": True,
        "allow_real_funds": False,
        "single_writer_required": True,
    }
    if any(capability_snapshot.get(key) != value for key, value in required.items()):
        raise StrategyPlatformTask1Blocked("bundle safety capability is incomplete")
    return canonical_digest(
        {
            "digest_contract": "configuration-bundle-digest-v1",
            "workflow_kind": workflow_kind,
            "scope_type": scope_type,
            "scope_key": scope_key,
            "aggregate_profile_version_id": aggregate_profile_version_id,
            "resolved_versions_json": dict(sorted(resolved_versions_json.items())),
            "resolved_digests_json": dict(sorted(resolved_digests_json.items())),
            "capability_snapshot": capability_snapshot,
        }
    )


def _ensure_configuration_types(connection: Connection) -> None:
    for type_key, name_zh in _CONFIGURATION_TYPE_LABELS.items():
        row = connection.execute(
            text(
                "SELECT name_zh,schema_version,handler_key,editor_capability,enabled "
                "FROM configuration_types WHERE type_key=:key"
            ),
            {"key": type_key},
        ).mappings().first()
        expected = {
            "name_zh": name_zh,
            "schema_version": "1",
            "handler_key": "strategy-platform-closed-json-schema-v1",
            "enabled": True,
        }
        if row is not None:
            identity_keys = ("name_zh", "schema_version", "enabled")
            if any(row[key] != expected[key] for key in identity_keys):
                raise StrategyPlatformTask1Blocked(
                    f"configuration type identity conflict: {type_key}"
                )
            if row["handler_key"] == "generic-json-v1" and (
                row["editor_capability"] or {}
            ).get("json_schema") == {"type": "object"}:
                connection.execute(
                    text(
                        "UPDATE configuration_types SET handler_key=:handler "
                        "WHERE type_key=:key AND handler_key='generic-json-v1'"
                    ),
                    {"key": type_key, "handler": expected["handler_key"]},
                )
            elif row["handler_key"] != expected["handler_key"]:
                raise StrategyPlatformTask1Blocked(
                    f"configuration handler is not installed: {type_key}"
                )
            continue
        connection.execute(
            text(
                "INSERT INTO configuration_types "
                "(type_key,name_zh,description_zh,schema_version,handler_key,"
                "editor_capability,enabled) "
                "VALUES (:key,:name,:description,'1',"
                "'strategy-platform-closed-json-schema-v1',"
                "CAST(:capability AS json),TRUE)"
            ),
            {
                "key": type_key,
                "name": name_zh,
                "description": f"Strategy Platform V1.3 {name_zh}",
                "capability": _json_parameter(
                    {
                        "managed": True,
                        "json_schema": {"type": "object"},
                        "safety": {
                            "demo_only": True,
                            "allow_real_funds": False,
                            "single_writer_required": True,
                        },
                    }
                ),
            },
        )


def _ensure_configuration_payload_schema(
    connection: Connection, *, type_key: str, payload: Mapping[str, Any]
) -> None:
    from app.services.strategy_platform_configuration_validation import (
        ConfigurationPayloadValidationError,
        HANDLER_KEY,
        infer_closed_json_schema,
        validate_closed_json_schema,
    )

    row = connection.execute(
        text(
            "SELECT handler_key,editor_capability FROM configuration_types "
            "WHERE type_key=:key AND enabled=TRUE"
        ),
        {"key": type_key},
    ).mappings().first()
    if row is None or row["handler_key"] != HANDLER_KEY:
        raise StrategyPlatformTask1Blocked(
            f"configuration type has no installed validation handler: {type_key}"
        )
    capability = dict(row["editor_capability"] or {})
    schema = capability.get("json_schema")
    try:
        if schema == {"type": "object"}:
            schema = infer_closed_json_schema(dict(payload))
            capability["json_schema"] = schema
            capability["validation_handler"] = HANDLER_KEY
            capability["schema_is_closed"] = True
            connection.execute(
                text(
                    "UPDATE configuration_types SET editor_capability=CAST(:capability AS json) "
                    "WHERE type_key=:key AND editor_capability::jsonb=CAST(:old AS jsonb)"
                ),
                {
                    "key": type_key,
                    "capability": _json_parameter(capability),
                    "old": _json_parameter(row["editor_capability"]),
                },
            )
        if not isinstance(schema, Mapping) or schema == {"type": "object"}:
            raise ConfigurationPayloadValidationError("generic schema is forbidden")
        try:
            validate_closed_json_schema(dict(payload), schema)
        except ConfigurationPayloadValidationError:
            # A type may have multiple explicitly versioned payload shapes (for
            # example formal and legacy-UNKNOWN quality profiles).  Task 1 adds
            # only the exact closed variant it is currently migrating; this is
            # not a runtime fallback or an open additional-properties schema.
            candidate = infer_closed_json_schema(dict(payload))
            variants = list(schema.get("anyOf", [])) if "anyOf" in schema else [schema]
            if candidate not in variants:
                variants.append(candidate)
                capability["json_schema"] = {"anyOf": variants}
                capability["schema_variant_count"] = len(variants)
                connection.execute(
                    text(
                        "UPDATE configuration_types SET "
                        "editor_capability=CAST(:capability AS json) WHERE type_key=:key"
                    ),
                    {"key": type_key, "capability": _json_parameter(capability)},
                )
            validate_closed_json_schema(dict(payload), {"anyOf": variants})
    except ConfigurationPayloadValidationError as exc:
        raise StrategyPlatformTask1Blocked(
            f"configuration schema validation failed for {type_key}: {exc}"
        ) from exc


def _ensure_configuration_version(
    connection: Connection,
    *,
    type_key: str,
    version_number: int,
    payload: Mapping[str, Any],
    created_by: str,
    change_summary: str,
) -> tuple[int, bool]:
    _ensure_configuration_payload_schema(
        connection, type_key=type_key, payload=payload
    )
    digest = configuration_digest(
        config_type=type_key,
        schema_version="1",
        payload=payload,
    )
    row = connection.execute(
        text(
            "SELECT id,lifecycle_status,config_digest,payload_json "
            "FROM configuration_versions "
            "WHERE type_key=:type_key AND version_number=:version_number"
        ),
        {"type_key": type_key, "version_number": version_number},
    ).mappings().first()
    if row is not None:
        if row["config_digest"] != digest or row["payload_json"] != dict(payload):
            raise StrategyPlatformTask1Blocked(
                f"configuration version identity conflict: {type_key} v{version_number}"
            )
        return int(row["id"]), False
    version_id = connection.execute(
        text(
            "INSERT INTO configuration_versions "
            "(type_key,version_number,lifecycle_status,payload_json,schema_version,"
            "config_digest,change_summary,created_by) "
            "VALUES (:type_key,:version_number,'DRAFT',CAST(:payload AS json),'1',"
            ":digest,:summary,:created_by) RETURNING id"
        ),
        {
            "type_key": type_key,
            "version_number": version_number,
            "payload": _json_parameter(payload),
            "digest": digest,
            "summary": change_summary,
            "created_by": created_by,
        },
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO configuration_audit_events "
            "(configuration_version_id,event_type,actor,request_id,reason,event_snapshot) "
            "VALUES (:id,'DRAFT_CREATED',:actor,:request_id,:reason,CAST(:snapshot AS json))"
        ),
        {
            "id": version_id,
            "actor": created_by,
            "request_id": f"{TASK1_MIGRATION_KEY}:{type_key}:{version_number}:draft",
            "reason": change_summary,
            "snapshot": _json_parameter({"config_digest": digest, "migration": TASK1_MIGRATION_KEY}),
        },
    )
    return int(version_id), True


def _ensure_dependency(
    connection: Connection,
    *,
    parent_id: int,
    child_id: int,
    relation_key: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO configuration_dependencies "
            "(configuration_version_id,depends_on_version_id,relation_key) "
            "VALUES (:parent,:child,:relation) ON CONFLICT DO NOTHING"
        ),
        {"parent": parent_id, "child": child_id, "relation": relation_key},
    )


_CONFIG_SPECIALIZED_TABLES: Mapping[str, tuple[str, str]] = {
    "strategy-source-definition": (
        "strategy_source_definition_versions",
        "configuration_version_id",
    ),
    "trigger-source-definition": (
        "trigger_source_definition_versions",
        "configuration_version_id",
    ),
    "timeframe-definition": (
        "timeframe_definition_versions",
        "configuration_version_id",
    ),
    "execution-target-definition": (
        "execution_target_definition_versions",
        "configuration_version_id",
    ),
    "metric-definition": ("metric_definition_versions", "configuration_version_id"),
    "research-target-config-set": ("research_target_config_sets", "id"),
    "validation-window-config-set": ("validation_window_config_sets", "id"),
    "quality-gate-profile": (
        "quality_gate_profile_versions",
        "configuration_version_id",
    ),
    "provider-model-config": ("provider_model_config_versions", "configuration_version_id"),
    "generation-profile": ("generation_profile_versions", "configuration_version_id"),
    "strategy-family-definition": (
        "strategy_family_definition_versions",
        "configuration_version_id",
    ),
    "scoring-profile": ("scoring_profile_versions", "configuration_version_id"),
    "diversity-profile": ("diversity_profile_versions", "configuration_version_id"),
    "worker-execution-profile": (
        "worker_execution_profile_versions",
        "configuration_version_id",
    ),
    "scheduler-profile": ("scheduler_profile_versions", "configuration_version_id"),
    "market-data-policy": ("market_data_policy_versions", "configuration_version_id"),
    "evidence-freshness-profile": (
        "evidence_freshness_profile_versions",
        "configuration_version_id",
    ),
    "monitoring-profile": ("monitoring_profile_versions", "configuration_version_id"),
    "promotion-profile": ("promotion_profile_versions", "configuration_version_id"),
    "risk-profile": ("risk_profile_versions", "configuration_version_id"),
    "capacity-profile": ("capacity_profile_versions", "configuration_version_id"),
    "runtime-profile": ("runtime_profile_versions", "configuration_version_id"),
    "deployment-profile": ("deployment_profile_versions", "configuration_version_id"),
    "market-data-profile": ("market_data_profile_versions", "configuration_version_id"),
    "optimization-profile": (
        "optimization_profile_versions",
        "configuration_version_id",
    ),
    "ui-presentation-profile": (
        "ui_presentation_profile_versions",
        "configuration_version_id",
    ),
    "research-profile": ("research_profile_versions", "configuration_version_id"),
}


def _validate_configuration_materialization(
    connection: Connection, *, version_id: int
) -> None:
    from app.services.strategy_platform_configuration_validation import (
        ConfigurationPayloadValidationError,
        adapter_keys_in_payload,
    )

    row = connection.execute(
        text(
            "SELECT type_key,schema_version,payload_json,config_digest "
            "FROM configuration_versions WHERE id=:id"
        ),
        {"id": version_id},
    ).mappings().one()
    payload = dict(row["payload_json"] or {})
    _ensure_configuration_payload_schema(
        connection, type_key=str(row["type_key"]), payload=payload
    )
    recomputed = configuration_digest(
        config_type=str(row["type_key"]),
        schema_version=str(row["schema_version"]),
        payload=payload,
    )
    if recomputed != row["config_digest"]:
        raise StrategyPlatformTask1Blocked(
            f"configuration digest cannot be recomputed: {version_id}"
        )
    specialized = _CONFIG_SPECIALIZED_TABLES.get(str(row["type_key"]))
    if specialized is not None:
        table_name, id_column = specialized
        count = int(
            connection.execute(
                text(f"SELECT count(*) FROM {table_name} WHERE {id_column}=:id"),
                {"id": version_id},
            ).scalar_one()
        )
        if count != 1:
            raise StrategyPlatformTask1Blocked(
                f"configuration specialized materialization mismatch: "
                f"version={version_id} table={table_name} count={count}"
            )
    try:
        adapter_keys = adapter_keys_in_payload(payload)
    except ConfigurationPayloadValidationError as exc:
        raise StrategyPlatformTask1Blocked(
            f"configuration adapter reference is invalid: {version_id}: {exc}"
        ) from exc
    for adapter_key in adapter_keys:
        installed = connection.execute(
            text(
                "SELECT count(*) FROM adapter_definitions WHERE adapter_key=:key "
                "AND enabled=TRUE AND registry_metadata_only=TRUE "
                "AND contains_secret_material=FALSE "
                "AND contains_executable_payload=FALSE"
            ),
            {"key": adapter_key},
        ).scalar_one()
        if int(installed) != 1:
            raise StrategyPlatformTask1Blocked(
                f"configuration references unavailable adapter: {adapter_key}"
            )


def _validate_configuration_version(
    connection: Connection,
    *,
    version_id: int,
    created: bool,
    actor: str,
) -> None:
    _validate_configuration_materialization(connection, version_id=version_id)
    status = connection.execute(
        text("SELECT lifecycle_status FROM configuration_versions WHERE id=:id"),
        {"id": version_id},
    ).scalar_one()
    if status == "VALIDATED":
        return
    if status != "DRAFT" or not created:
        raise StrategyPlatformTask1Blocked(
            f"configuration lifecycle conflict for version {version_id}"
        )
    now = datetime.now(timezone.utc)
    connection.execute(
        text(
            "UPDATE configuration_versions SET lifecycle_status='VALIDATED',"
            "validated_at=:now WHERE id=:id AND lifecycle_status='DRAFT'"
        ),
        {"id": version_id, "now": now},
    )
    connection.execute(
        text(
            "INSERT INTO configuration_audit_events "
            "(configuration_version_id,event_type,actor,request_id,reason,event_snapshot) "
            "VALUES (:id,'VALIDATED',:actor,:request_id,"
            "'V1.3 Task 1 deterministic migration',CAST(:snapshot AS json))"
        ),
        {
            "id": version_id,
            "actor": actor,
            "request_id": f"{TASK1_MIGRATION_KEY}:{version_id}:validated",
            "snapshot": _json_parameter({"migration": TASK1_MIGRATION_KEY}),
        },
    )


def _ensure_activation(
    connection: Connection,
    *,
    config_type: str,
    scope_type: str,
    scope_key: str,
    version_id: int,
    actor: str,
) -> None:
    version = connection.execute(
        text(
            "SELECT type_key,lifecycle_status FROM configuration_versions WHERE id=:id"
        ),
        {"id": version_id},
    ).mappings().first()
    if (
        version is None
        or version["type_key"] != config_type
        or version["lifecycle_status"] != "VALIDATED"
    ):
        raise StrategyPlatformTask1Blocked(
            f"configuration activation requires a validated matching version: {config_type}"
        )
    existing = connection.execute(
        text(
            "SELECT version_id FROM configuration_activations "
            "WHERE config_type=:type AND scope_type=:scope_type AND scope_key=:scope_key"
        ),
        {"type": config_type, "scope_type": scope_type, "scope_key": scope_key},
    ).scalar_one_or_none()
    if existing is not None:
        if int(existing) != version_id:
            raise StrategyPlatformTask1Blocked(
                f"activation conflict: {config_type} {scope_type}/{scope_key}"
            )
        return
    connection.execute(
        text(
            "INSERT INTO configuration_activations "
            "(config_type,scope_type,scope_key,version_id,activated_by) "
            "VALUES (:type,:scope_type,:scope_key,:version_id,:actor)"
        ),
        {
            "type": config_type,
            "scope_type": scope_type,
            "scope_key": scope_key,
            "version_id": version_id,
            "actor": actor,
        },
    )
    connection.execute(
        text(
            "INSERT INTO configuration_audit_events "
            "(configuration_version_id,event_type,actor,request_id,scope_type,scope_key,"
            "reason,event_snapshot) VALUES (:id,'ACTIVATED',:actor,:request_id,"
            ":scope_type,:scope_key,'V1.3 Task 1 initial activation',CAST(:snapshot AS json))"
        ),
        {
            "id": version_id,
            "actor": actor,
            "request_id": f"{TASK1_MIGRATION_KEY}:{config_type}:{scope_key}:active",
            "scope_type": scope_type,
            "scope_key": scope_key,
            "snapshot": _json_parameter({"migration": TASK1_MIGRATION_KEY}),
        },
    )


def _ensure_adapter_registry(connection: Connection) -> None:
    project_root = Path(__file__).resolve().parents[3]
    adapters = validate_declared_adapter_coverage(project_root)
    manifest_digest = installed_adapter_manifest_digest(adapters)
    for adapter in adapters:
        display_metadata = {
            "input_schema": adapter.input_schema,
            "output_schema": adapter.output_schema,
            "source_ref": adapter.source_ref,
            "source_sha256": adapter.source_sha256,
            "installed_manifest_digest": manifest_digest,
        }
        existing = connection.execute(
            text(
                "SELECT adapter_kind,implementation_version,input_schema_version,"
                "output_schema_version,capabilities,display_metadata,enabled,"
                "registry_metadata_only,contains_secret_material,"
                "contains_executable_payload FROM adapter_definitions "
                "WHERE adapter_key=:key"
            ),
            {"key": adapter.adapter_key},
        ).mappings().first()
        expected = {
            "adapter_kind": adapter.adapter_kind,
            "implementation_version": adapter.implementation_version,
            "input_schema_version": adapter.input_schema_version,
            "output_schema_version": adapter.output_schema_version,
            "capabilities": dict(adapter.capabilities),
            "display_metadata": display_metadata,
            "enabled": True,
            "registry_metadata_only": True,
            "contains_secret_material": False,
            "contains_executable_payload": False,
        }
        if existing is not None:
            if any(existing[key] != value for key, value in expected.items()):
                raise StrategyPlatformTask1Blocked(
                    f"adapter registry identity conflict: {adapter.adapter_key}"
                )
            continue
        connection.execute(
            text(
                "INSERT INTO adapter_definitions "
                "(adapter_key,adapter_kind,implementation_version,input_schema_version,"
                "output_schema_version,capabilities,display_metadata,enabled,"
                "registry_metadata_only,contains_secret_material,contains_executable_payload) "
                "VALUES (:key,:kind,:implementation,:input_schema,:output_schema,"
                "CAST(:capabilities AS json),CAST(:metadata AS json),"
                "TRUE,TRUE,FALSE,FALSE)"
            ),
            {
                "key": adapter.adapter_key,
                "kind": adapter.adapter_kind,
                "implementation": adapter.implementation_version,
                "input_schema": adapter.input_schema_version,
                "output_schema": adapter.output_schema_version,
                "capabilities": _json_parameter(adapter.capabilities),
                "metadata": _json_parameter(display_metadata),
            },
        )


def _ensure_definition_version(
    connection: Connection,
    *,
    definition_table: str,
    key_column: str,
    key_value: str,
    version_table: str,
    definition_fk_column: str,
    type_key: str,
    version_number: int,
    payload: Mapping[str, Any],
    child_columns: Mapping[str, Any],
    actor: str,
) -> int:
    definition_id = connection.execute(
        text(
            f"INSERT INTO {definition_table} ({key_column}) VALUES (:key) "
            f"ON CONFLICT ({key_column}) DO UPDATE SET {key_column}=EXCLUDED.{key_column} "
            "RETURNING id"
        ),
        {"key": key_value},
    ).scalar_one()
    version_id, created = _ensure_configuration_version(
        connection,
        type_key=type_key,
        version_number=version_number,
        payload=payload,
        created_by=actor,
        change_summary="V1.3 Task 1 registry import",
    )
    if created:
        columns = ["configuration_version_id", definition_fk_column, *child_columns]
        values = {"configuration_version_id": version_id, definition_fk_column: definition_id}
        values.update(child_columns)
        placeholders = []
        parameters: dict[str, Any] = {}
        for index, column in enumerate(columns):
            name = f"value_{index}"
            value = values[column]
            if isinstance(value, (dict, list)):
                placeholders.append(f"CAST(:{name} AS json)")
                parameters[name] = _json_parameter(value)
            else:
                placeholders.append(f":{name}")
                parameters[name] = value
        connection.execute(
            text(
                f"INSERT INTO {version_table} ({','.join(columns)}) "
                f"VALUES ({','.join(placeholders)})"
            ),
            parameters,
        )
    else:
        _assert_specialized_profile(
            connection,
            table_name=version_table,
            version_id=version_id,
            values={definition_fk_column: int(definition_id), **child_columns},
        )
    _validate_configuration_version(
        connection, version_id=version_id, created=created, actor=actor
    )
    return version_id


def _seed_registries(connection: Connection, *, actor: str) -> dict[str, Any]:
    versions: dict[str, Any] = {}
    _ensure_adapter_registry(connection)

    versions["sources"] = {}
    for index, (key, name_zh, allows_external) in enumerate(_SOURCES, start=1):
        versions["sources"][key] = _ensure_definition_version(
            connection,
            definition_table="strategy_source_definitions",
            key_column="source_key",
            key_value=key,
            version_table="strategy_source_definition_versions",
            definition_fk_column="strategy_source_definition_id",
            type_key="strategy-source-definition",
            version_number=index,
            payload={
                "source_key": key,
                "name_zh": name_zh,
                "allows_external_submission": allows_external,
                "metadata_only": True,
                "executable_payload_allowed": False,
            },
            child_columns={
                "name_zh": name_zh,
                "description_zh": f"V1.3 策略来源 {name_zh}",
                "allows_external_submission": allows_external,
                "required_audit_fields": ["request_id", "source_digest"],
                "display_metadata": {},
                "enabled": True,
                "metadata_only": True,
                "executable_payload_allowed": False,
            },
            actor=actor,
        )

    versions["triggers"] = {}
    for index, (key, name_zh) in enumerate(_TRIGGERS, start=1):
        versions["triggers"][key] = _ensure_definition_version(
            connection,
            definition_table="trigger_source_definitions",
            key_column="trigger_key",
            key_value=key,
            version_table="trigger_source_definition_versions",
            definition_fk_column="trigger_source_definition_id",
            type_key="trigger-source-definition",
            version_number=index,
            payload={"trigger_key": key, "name_zh": name_zh, "metadata_only": True},
            child_columns={
                "name_zh": name_zh,
                "description_zh": f"V1.3 触发来源 {name_zh}",
                "required_audit_fields": ["triggered_by", "triggered_at"],
                "display_metadata": {},
                "enabled": True,
                "metadata_only": True,
                "executable_payload_allowed": False,
            },
            actor=actor,
        )

    versions["timeframes"] = {}
    for index, (key, seconds, name_zh, sort_order) in enumerate(_TIMEFRAMES, start=1):
        versions["timeframes"][key] = _ensure_definition_version(
            connection,
            definition_table="timeframe_definitions",
            key_column="timeframe_key",
            key_value=key,
            version_table="timeframe_definition_versions",
            definition_fk_column="timeframe_definition_id",
            type_key="timeframe-definition",
            version_number=index,
            payload={"timeframe_key": key, "duration_seconds": seconds},
            child_columns={
                "duration_seconds": seconds,
                "name_zh": name_zh,
                "description_zh": f"闭合 K 线周期 {name_zh}",
                "sort_order": sort_order,
                "display_metadata": {"external_formats": {"freqtrade": key, "okx": key}},
                "enabled": True,
            },
            actor=actor,
        )

    versions["families"] = {}
    for index, (key, name_zh) in enumerate(_FAMILIES, start=1):
        versions["families"][key] = _ensure_definition_version(
            connection,
            definition_table="strategy_family_definitions",
            key_column="family_key",
            key_value=key,
            version_table="strategy_family_definition_versions",
            definition_fk_column="strategy_family_definition_id",
            type_key="strategy-family-definition",
            version_number=index,
            payload={"family_key": key, "name_zh": name_zh, "enabled": True},
            child_columns={
                "name_zh": name_zh,
                "description_zh": f"正式研究策略家族 {name_zh}",
                "display_metadata": {},
                "enabled": True,
            },
            actor=actor,
        )

    versions["metrics"] = {}
    for index, (key, name_zh, unit, source) in enumerate(_METRICS, start=1):
        versions["metrics"][key] = _ensure_definition_version(
            connection,
            definition_table="metric_definitions",
            key_column="metric_key",
            key_value=key,
            version_table="metric_definition_versions",
            definition_fk_column="metric_definition_id",
            type_key="metric-definition",
            version_number=index,
            payload={"metric_key": key, "name_zh": name_zh, "unit": unit, "data_source": source},
            child_columns={
                "name_zh": name_zh,
                "unit": unit,
                "data_source": source,
                "available_aggregations": ["identity"],
                "display_metadata": {},
            },
            actor=actor,
        )
    return versions


def _ensure_execution_targets(
    connection: Connection, *, actor: str
) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    definitions = (
        (
            "RESEARCH_ONLY",
            "非交易研究目标",
            "NON_TRADING_RESEARCH",
            None,
            None,
            {"exchange_writes": False, "canonical_writer": "NOT_APPLICABLE"},
        ),
        (
            "OKX_DEMO",
            "OKX 模拟盘目标",
            "DEMO_TRADING",
            "okx-demo-exchange-v1",
            "docker-runtime-v1",
            {"exchange_writes": "SOLE_CANONICAL_WRITER", "canonical_writer": "REQUIRED"},
        ),
    )
    for index, (key, name_zh, scope_kind, exchange_adapter, runtime_adapter, writer_policy) in enumerate(
        definitions, start=1
    ):
        definition_id = connection.execute(
            text(
                "INSERT INTO execution_target_definitions (target_key) VALUES (:key) "
                "ON CONFLICT (target_key) DO UPDATE SET target_key=EXCLUDED.target_key "
                "RETURNING id"
            ),
            {"key": key},
        ).scalar_one()
        payload = {
            "target_key": key,
            "scope_kind": scope_kind,
            "exchange_adapter_key": exchange_adapter,
            "runtime_adapter_key": runtime_adapter,
            "writer_policy": writer_policy,
            "demo_only": True,
            "allow_real_funds": False,
            "single_writer_required": True,
        }
        version_id, created = _ensure_configuration_version(
            connection,
            type_key="execution-target-definition",
            version_number=index,
            payload=payload,
            created_by=actor,
            change_summary="V1.3 Task 1 execution target registry",
        )
        if created:
            connection.execute(
                text(
                    "INSERT INTO execution_target_definition_versions "
                    "(configuration_version_id,execution_target_definition_id,name_zh,"
                    "description_zh,scope_kind,exchange_adapter_key,runtime_adapter_key,"
                    "writer_policy,enabled,demo_only,allow_real_funds,single_writer_required) "
                    "VALUES (:version,:definition,:name,:description,:scope,:exchange_adapter,"
                    ":runtime_adapter,CAST(:writer_policy AS json),TRUE,TRUE,FALSE,TRUE)"
                ),
                {
                    "version": version_id,
                    "definition": definition_id,
                    "name": name_zh,
                    "description": f"V1.3 {name_zh}",
                    "scope": scope_kind,
                    "exchange_adapter": exchange_adapter,
                    "runtime_adapter": runtime_adapter,
                    "writer_policy": _json_parameter(writer_policy),
                },
            )
        _validate_configuration_version(
            connection, version_id=version_id, created=created, actor=actor
        )
        result[key] = (int(definition_id), version_id)
    return result


def _seed_research_target_config(
    connection: Connection,
    *,
    market_inventory: Sequence[MarketFileEvidence],
    timeframe_versions: Mapping[str, int],
    actor: str,
) -> int:
    payload = {
        "exchange": "okx",
        "market_type": "futures",
        "targets": [
            {
                "pair": item.pair,
                "instrument_id": item.instrument_id,
                "timeframe": item.timeframe,
                "data_kind": item.data_kind,
                "enabled": True,
                "priority": 100,
                "max_data_age_seconds": 7200,
            }
            for item in sorted(market_inventory, key=lambda row: (row.pair, row.timeframe))
        ],
    }
    version_id, created = _ensure_configuration_version(
        connection,
        type_key="research-target-config-set",
        version_number=1,
        payload=payload,
        created_by=actor,
        change_summary="Import the current BTC/ETH/SOL x 5m/15m research target matrix",
    )
    if created:
        connection.execute(
            text(
                "INSERT INTO research_target_config_sets (id,name,description) "
                "VALUES (:id,'formal-research-targets-v1',"
                "'Current evidence-backed public market research targets')"
            ),
            {"id": version_id},
        )
        for item in sorted(market_inventory, key=lambda row: (row.pair, row.timeframe)):
            connection.execute(
                text(
                    "INSERT INTO research_target_configs "
                    "(config_set_id,exchange,pair,instrument_id,timeframe,data_kind,"
                    "enabled,priority,max_data_age_seconds,display_metadata) "
                    "VALUES (:set_id,'okx',:pair,:instrument,:timeframe,:data_kind,"
                    "TRUE,100,7200,CAST(:metadata AS json))"
                ),
                {
                    "set_id": version_id,
                    "pair": item.pair,
                    "instrument": item.instrument_id,
                    "timeframe": item.timeframe,
                    "data_kind": item.data_kind,
                    "metadata": _json_parameter(
                        {"source": "PUBLIC_MARKET_DATA_ONLY", "file_sha256": item.sha256}
                    ),
                },
            )
        for timeframe in sorted({item.timeframe for item in market_inventory}):
            timeframe_version_id = timeframe_versions.get(timeframe)
            if timeframe_version_id is None:
                raise StrategyPlatformTask1Blocked(
                    f"research target has no timeframe registry version: {timeframe}"
                )
            _ensure_dependency(
                connection,
                parent_id=version_id,
                child_id=timeframe_version_id,
                relation_key=f"timeframe:{timeframe}",
            )
    _validate_configuration_version(
        connection, version_id=version_id, created=created, actor=actor
    )
    return version_id


def _seed_validation_window_config(
    connection: Connection,
    *,
    market_inventory: Sequence[MarketFileEvidence],
    actor: str,
) -> int:
    payload = {
        "contract": "formal-dynamic-validation-windows-v1",
        "classifier_adapter_key": "window-close-return-v1",
        "classifier_parameters": {"bull_threshold": 0.05, "bear_threshold": -0.05},
        "boundary": "[start_at,end_at)",
        "targets": [
            {
                "pair": item.pair,
                "timeframe": item.timeframe,
                "file_sha256": item.sha256,
                "windows": {
                    key: dict((item.classification_windows or {})[key])
                    for key in _WINDOW_SPECS
                },
            }
            for item in sorted(market_inventory, key=lambda row: (row.pair, row.timeframe))
        ],
    }
    version_id, created = _ensure_configuration_version(
        connection,
        type_key="validation-window-config-set",
        version_number=1,
        payload=payload,
        created_by=actor,
        change_summary="Import the current five-window research matrix with real file evidence",
    )
    if created:
        connection.execute(
            text(
                "INSERT INTO validation_window_config_sets "
                "(id,name,default_classifier_adapter_key,default_classifier_parameters) "
                "VALUES (:id,'formal-dynamic-validation-windows-v1',"
                "'window-close-return-v1',CAST(:parameters AS json))"
            ),
            {
                "id": version_id,
                "parameters": _json_parameter(
                    {"bull_threshold": 0.05, "bear_threshold": -0.05}
                ),
            },
        )
        for key, name, qualifies, order in (
            ("primary_scoring", "主评分", False, 10),
            ("qualification", "准入验证", True, 20),
            ("out_of_sample", "独立样本外", True, 30),
        ):
            connection.execute(
                text(
                    "INSERT INTO validation_window_purposes "
                    "(config_set_id,key,name_zh,description_zh,counts_for_qualification,"
                    "enabled,sort_order) VALUES (:set_id,:key,:name,:description,"
                    ":qualifies,TRUE,:sort_order)"
                ),
                {
                    "set_id": version_id,
                    "key": key,
                    "name": name,
                    "description": f"V1.3 {name}窗口用途",
                    "qualifies": qualifies,
                    "sort_order": order,
                },
            )
        for order, (key, name) in enumerate(
            (("bull", "牛市"), ("range", "震荡市"), ("bear", "熊市")), start=1
        ):
            connection.execute(
                text(
                    "INSERT INTO market_regime_definitions "
                    "(config_set_id,key,name_zh,description_zh,dimension_key,enabled,sort_order) "
                    "VALUES (:set_id,:key,:name,:description,'trend',TRUE,:sort_order)"
                ),
                {
                    "set_id": version_id,
                    "key": key,
                    "name": name,
                    "description": "window-close-return-v1 实算趋势状态",
                    "sort_order": order * 10,
                },
            )
        for item in sorted(market_inventory, key=lambda row: (row.pair, row.timeframe)):
            for window_key, spec in _WINDOW_SPECS.items():
                boundaries = spec.get("sol") if item.pair.startswith("SOL/") else None
                start_at, end_at = boundaries or spec["default"]
                evidence = {
                    **dict((item.classification_windows or {})[window_key]),
                    "algorithm": "window-close-return-v1",
                    "parameters": {"bull_threshold": 0.05, "bear_threshold": -0.05},
                    "window_key": window_key,
                    "start_at": start_at,
                    "end_at": end_at,
                    "file_sha256": item.sha256,
                }
                window_id = connection.execute(
                    text(
                        "INSERT INTO validation_window_configs "
                        "(config_set_id,pair,timeframe,data_kind,window_key,purpose_key,"
                        "ordinal,name_zh,description_zh,start_at,end_at,classifier_adapter_key,"
                        "classifier_parameters,required,source_receipt_id,classification_evidence) "
                        "VALUES (:set_id,:pair,:timeframe,:data_kind,:window_key,:purpose,"
                        ":ordinal,:name,:description,:start_at,:end_at,'window-close-return-v1',"
                        "CAST(:parameters AS json),:required,:receipt,CAST(:evidence AS json)) "
                        "RETURNING id"
                    ),
                    {
                        "set_id": version_id,
                        "pair": item.pair,
                        "timeframe": item.timeframe,
                        "data_kind": item.data_kind,
                        "window_key": window_key,
                        "purpose": spec["purpose"],
                        "ordinal": spec["ordinal"],
                        "name": spec["name_zh"],
                        "description": f"{item.pair} {item.timeframe} {spec['name_zh']}",
                        "start_at": start_at,
                        "end_at": end_at,
                        "parameters": _json_parameter(
                            {"bull_threshold": 0.05, "bear_threshold": -0.05}
                        ),
                        "required": spec["required"],
                        "receipt": item.receipt_id,
                        "evidence": _json_parameter(evidence),
                    },
                ).scalar_one()
                if spec["expected"] is not None:
                    connection.execute(
                        text(
                            "INSERT INTO validation_window_expectations "
                            "(window_config_id,dimension_key,operator,expected_value,required) "
                            "VALUES (:window_id,'trend','EQ',:expected,TRUE)"
                        ),
                        {"window_id": window_id, "expected": spec["expected"]},
                    )
    _validate_configuration_version(
        connection, version_id=version_id, created=created, actor=actor
    )
    return version_id


def _seed_quality_profiles(
    connection: Connection,
    *,
    metric_versions: Mapping[str, int],
    actor: str,
) -> tuple[int, int]:
    formal_payload = {
        "contract_version": "formal-strategy-research-aggressive-v1",
        "min_strategy_score": 50.0,
        "min_trades_per_validation_window": 30,
        "validation_requires_positive_net_profit": True,
        "max_drawdown_per_validation_window": 0.15,
        "lookahead_analysis_required": True,
        "fee_per_side": 0.0005,
        "slippage_per_side": 0.0002,
        "required_window_selector": {"required": True},
        "qualification_status_source": "dynamic_rule_evaluations_only",
    }
    profile_id = connection.execute(
        text(
            "INSERT INTO quality_gate_profiles (profile_key,name) "
            "VALUES ('formal-strategy-research-aggressive-v1','正式进攻型研究质量门') "
            "ON CONFLICT (profile_key) DO UPDATE SET profile_key=EXCLUDED.profile_key "
            "RETURNING id"
        )
    ).scalar_one()
    formal_version, created = _ensure_configuration_version(
        connection,
        type_key="quality-gate-profile",
        version_number=1,
        payload=formal_payload,
        created_by=actor,
        change_summary="Exact migration of formal-strategy-research-aggressive-v1",
    )
    if created:
        connection.execute(
            text(
                "INSERT INTO quality_gate_profile_versions "
                "(configuration_version_id,quality_gate_profile_id) VALUES (:version,:profile)"
            ),
            {"version": formal_version, "profile": profile_id},
        )
        rules = (
            ("total_score", {"purpose_key": "primary_scoring"}, ">=", 50.0, "score", 10),
            ("total_trades", {"required": True}, ">=", 30.0, "count", 20),
            ("net_profit_after_cost", {"required": True}, ">", 0.0, "ratio", 30),
            ("max_drawdown", {"required": True}, "<=", 0.15, "ratio", 40),
            ("fee_per_side", {"required": True}, ">=", 0.0005, "ratio", 50),
            ("slippage_per_side", {"required": True}, ">=", 0.0002, "ratio", 60),
            ("lookahead_passed", {"all_windows": True}, "==", 1.0, "boolean", 70),
        )
        for metric, selector, operator, threshold, unit, priority in rules:
            connection.execute(
                text(
                    "INSERT INTO quality_gate_rules "
                    "(profile_version_id,pair,timeframe,window_selector,metric_definition_id,"
                    "evaluation_adapter_key,evaluation_parameters,threshold_value,threshold_max,"
                    "unit,severity,score_weight,priority) VALUES (:profile,NULL,NULL,"
                    "CAST(:selector AS json),:metric,'threshold-comparison-v1',"
                    "CAST(:parameters AS json),:threshold,NULL,:unit,'BLOCKING',NULL,:priority)"
                ),
                {
                    "profile": formal_version,
                    "selector": _json_parameter(selector),
                    "metric": metric_versions[metric],
                    "parameters": _json_parameter({"operator": operator}),
                    "threshold": threshold,
                    "unit": unit,
                    "priority": priority,
                },
            )
    _validate_configuration_version(
        connection, version_id=formal_version, created=created, actor=actor
    )

    legacy_profile_id = connection.execute(
        text(
            "INSERT INTO quality_gate_profiles (profile_key,name) "
            "VALUES ('legacy-imported-quality-unknown-v1','历史质量语义未知') "
            "ON CONFLICT (profile_key) DO UPDATE SET profile_key=EXCLUDED.profile_key "
            "RETURNING id"
        )
    ).scalar_one()
    legacy_version, legacy_created = _ensure_configuration_version(
        connection,
        type_key="quality-gate-profile",
        version_number=2,
        payload={
            "contract_version": "legacy-imported-quality-unknown-v1",
            "quality_status": "UNKNOWN",
            "qualification_allowed": False,
            "reason": "Legacy static/window PASSED is not dynamic quality qualification",
        },
        created_by=actor,
        change_summary="Preserve legacy validation without asserting QUALIFIED",
    )
    if legacy_created:
        connection.execute(
            text(
                "INSERT INTO quality_gate_profile_versions "
                "(configuration_version_id,quality_gate_profile_id) VALUES (:version,:profile)"
            ),
            {"version": legacy_version, "profile": legacy_profile_id},
        )
    _validate_configuration_version(
        connection, version_id=legacy_version, created=legacy_created, actor=actor
    )
    return formal_version, legacy_version


def _insert_specialized_profile(
    connection: Connection,
    *,
    table_name: str,
    version_id: int,
    values: Mapping[str, Any],
) -> None:
    columns = ["configuration_version_id", *values]
    parameters: dict[str, Any] = {"configuration_version_id": version_id}
    placeholders = [":configuration_version_id"]
    for index, (column, value) in enumerate(values.items()):
        key = f"profile_value_{index}"
        if isinstance(value, (dict, list)):
            placeholders.append(f"CAST(:{key} AS json)")
            parameters[key] = _json_parameter(value)
        else:
            placeholders.append(f":{key}")
            parameters[key] = value
    connection.execute(
        text(
            f"INSERT INTO {table_name} ({','.join(columns)}) "
            f"VALUES ({','.join(placeholders)})"
        ),
        parameters,
    )


def _assert_specialized_profile(
    connection: Connection,
    *,
    table_name: str,
    version_id: int,
    values: Mapping[str, Any],
) -> None:
    from decimal import Decimal

    if table_name not in {value[0] for value in _CONFIG_SPECIALIZED_TABLES.values()}:
        raise StrategyPlatformTask1Blocked(
            f"unsupported specialized profile table: {table_name}"
        )
    selected = ",".join(values)
    row = connection.execute(
        text(
            f"SELECT {selected} FROM {table_name} "
            "WHERE configuration_version_id=:version"
        ),
        {"version": version_id},
    ).mappings().first()
    if row is None:
        raise StrategyPlatformTask1Blocked(
            f"configuration specialized row is missing: {table_name}/{version_id}"
        )
    conflicts: list[str] = []
    for key, expected in values.items():
        actual = row[key]
        if isinstance(actual, Decimal) and isinstance(expected, (int, float)):
            matches = actual == Decimal(str(expected))
        elif isinstance(actual, datetime) and isinstance(expected, datetime):
            matches = _aware_utc(actual) == _aware_utc(expected)
        else:
            matches = actual == expected
        if not matches:
            conflicts.append(key)
    if conflicts:
        raise StrategyPlatformTask1Blocked(
            f"configuration specialized row conflicts: "
            f"{table_name}/{version_id} fields={conflicts}"
        )


def _ensure_profile_version(
    connection: Connection,
    *,
    type_key: str,
    version_number: int,
    payload: Mapping[str, Any],
    table_name: str,
    specialized_values: Mapping[str, Any],
    dependencies: Sequence[tuple[int, str]],
    actor: str,
    change_summary: str,
    defer_validation: bool = False,
) -> tuple[int, bool]:
    version_id, created = _ensure_configuration_version(
        connection,
        type_key=type_key,
        version_number=version_number,
        payload=payload,
        created_by=actor,
        change_summary=change_summary,
    )
    if created:
        _insert_specialized_profile(
            connection,
            table_name=table_name,
            version_id=version_id,
            values=specialized_values,
        )
        for child_id, relation_key in dependencies:
            _ensure_dependency(
                connection,
                parent_id=version_id,
                child_id=child_id,
                relation_key=relation_key,
            )
    else:
        _assert_specialized_profile(
            connection,
            table_name=table_name,
            version_id=version_id,
            values=specialized_values,
        )
        existing_dependencies = {
            (int(row["depends_on_version_id"]), str(row["relation_key"]))
            for row in connection.execute(
                text(
                    "SELECT depends_on_version_id,relation_key "
                    "FROM configuration_dependencies "
                    "WHERE configuration_version_id=:version"
                ),
                {"version": version_id},
            ).mappings()
        }
        expected_dependencies = {(int(child), relation) for child, relation in dependencies}
        if existing_dependencies != expected_dependencies:
            raise StrategyPlatformTask1Blocked(
                f"configuration dependency graph conflicts: {type_key} v{version_number}"
            )
    if not defer_validation:
        _validate_configuration_version(
            connection, version_id=version_id, created=created, actor=actor
        )
    return version_id, created


def _seed_workflow_profiles(
    connection: Connection,
    *,
    registry_versions: Mapping[str, Any],
    execution_targets: Mapping[str, tuple[int, int]],
    target_config_id: int,
    window_config_id: int,
    quality_gate_id: int,
    actor: str,
) -> dict[str, int]:
    ids: dict[str, int] = {}

    provider_payload = {
        "provider_adapter_key": "deepseek-generation-v1",
        "provider_key": "deepseek",
        "model_key": "deepseek-v4-pro",
        "timeout_seconds": 180,
        "max_output_tokens": 16000,
        "credential_reference_kind": "REFERENCE_NAME",
        "credential_reference_name": "DEEPSEEK_API_KEY",
        "credential_attestation": "OUT_OF_SCOPE_UNKNOWN",
        "secret_material_present": False,
        "executable_payload_present": False,
    }
    ids["provider"], _ = _ensure_profile_version(
        connection,
        type_key="provider-model-config",
        version_number=1,
        payload=provider_payload,
        table_name="provider_model_config_versions",
        specialized_values={
            "provider_adapter_key": "deepseek-generation-v1",
            "provider_key": "deepseek",
            "model_key": "deepseek-v4-pro",
            "capabilities": {"strategy_blueprint": True, "attestation": "UNKNOWN"},
            "timeout_seconds": 180,
            "max_output_tokens": 16000,
            "generation_limits": {"temperature": 0.2},
            "credential_reference_kind": "REFERENCE_NAME",
            "credential_reference_name": "DEEPSEEK_API_KEY",
            "secret_material_present": False,
            "executable_payload_present": False,
            "enabled": True,
        },
        dependencies=[],
        actor=actor,
        change_summary="Migrate the current DeepSeek model selection without credential values",
    )

    generation_payload = {
        "candidates_per_target": 10,
        "structure_slot_count": 10,
        "total_target_count": 6,
        "total_candidate_count": 60,
        "provider_model_config_version_id": ids["provider"],
        "strategy_families": list(registry_versions["families"]),
        "blueprint_contract": "canonical-blueprint-v2",
    }
    ids["generation"], generation_created = _ensure_profile_version(
        connection,
        type_key="generation-profile",
        version_number=1,
        payload=generation_payload,
        table_name="generation_profile_versions",
        specialized_values={
            "provider_model_config_version_id": ids["provider"],
            "candidates_per_target": 10,
            "structure_slot_count": 10,
            "model_selection_policy": {"mode": "explicit_version"},
            "generation_limits": {"total_targets": 6, "total_candidates": 60},
            "blueprint_requirements": {
                "contract": "canonical-blueprint-v2",
                "exact_render_match": True,
            },
        },
        dependencies=[
            (ids["provider"], "provider_model"),
            *(
                (version_id, f"strategy_family:{key}")
                for key, version_id in sorted(
                    registry_versions["families"].items()
                )
            ),
        ],
        actor=actor,
        change_summary="Migrate the current six-target sixty-candidate generation contract",
        defer_validation=True,
    )
    if generation_created:
        for ordinal, version_id in enumerate(
            registry_versions["families"].values(), start=1
        ):
            connection.execute(
                text(
                    "INSERT INTO generation_profile_families "
                    "(generation_profile_version_id,strategy_family_definition_version_id,"
                    "allocation_count,ordinal,enabled) VALUES (:profile,:family,NULL,:ordinal,TRUE)"
                ),
                {"profile": ids["generation"], "family": version_id, "ordinal": ordinal},
            )
    _validate_configuration_version(
        connection,
        version_id=ids["generation"],
        created=generation_created,
        actor=actor,
    )

    scoring_payload = {
        "algorithm_version": "phase2-quality-v1",
        "component_weights": {
            "profit_score": 0.35,
            "risk_score": 0.25,
            "stability_score": 0.15,
            "quality_score": 0.25,
        },
        "primary_window_selector": {"window_key": "primary_bear"},
    }
    ids["scoring"], scoring_created = _ensure_profile_version(
        connection,
        type_key="scoring-profile",
        version_number=1,
        payload=scoring_payload,
        table_name="scoring_profile_versions",
        specialized_values={
            "scoring_adapter_key": "weighted-component-score-v1",
            "algorithm_version": "phase2-quality-v1",
            "aggregation_method": "weighted_sum",
            "primary_window_selector": {"window_key": "primary_bear"},
            "score_bounds": {"minimum": 0, "maximum": 100},
        },
        dependencies=[
            (registry_versions["metrics"][key], f"metric:{key}")
            for key in ("profit_score", "risk_score", "stability_score", "quality_score")
        ],
        actor=actor,
        change_summary="Migrate the existing phase2-quality-v1 scoring weights",
        defer_validation=True,
    )
    if scoring_created:
        for priority, (metric, weight) in enumerate(
            (("profit_score", 0.35), ("risk_score", 0.25), ("stability_score", 0.15), ("quality_score", 0.25)),
            start=1,
        ):
            connection.execute(
                text(
                    "INSERT INTO scoring_rules "
                    "(profile_version_id,metric_definition_version_id,"
                    "normalization_adapter_key,normalization_parameters,weight,data_source,"
                    "aggregation_method,window_selector,priority) VALUES (:profile,:metric,"
                    "'linear-normalization-v1','{}'::json,:weight,'strategy_scores',"
                    "'weighted_sum',CAST(:selector AS json),:priority)"
                ),
                {
                    "profile": ids["scoring"],
                    "metric": registry_versions["metrics"][metric],
                    "weight": weight,
                    "selector": _json_parameter({"window_key": "primary_bear"}),
                    "priority": priority * 10,
                },
            )
    _validate_configuration_version(
        connection,
        version_id=ids["scoring"],
        created=scoring_created,
        actor=actor,
    )

    diversity_payload = {
        "max_signal_similarity": 0.90,
        "max_abs_pnl_correlation": 0.85,
        "required_family_coverage": list(registry_versions["families"]),
    }
    ids["diversity"], diversity_created = _ensure_profile_version(
        connection,
        type_key="diversity-profile",
        version_number=1,
        payload=diversity_payload,
        table_name="diversity_profile_versions",
        specialized_values={
            "evaluation_adapter_key": "diversity-threshold-v1",
            "evaluation_scope": "per_pair_timeframe_batch",
            "parameters": {"required_family_count": 6},
        },
        dependencies=[
            (version_id, f"strategy_family:{key}")
            for key, version_id in registry_versions["families"].items()
        ],
        actor=actor,
        change_summary="Migrate the current structural diversity thresholds",
        defer_validation=True,
    )
    if diversity_created:
        for priority, (metric, threshold) in enumerate(
            (("max_signal_similarity", 0.90), ("max_abs_pnl_correlation", 0.85)),
            start=1,
        ):
            connection.execute(
                text(
                    "INSERT INTO diversity_rules "
                    "(profile_version_id,metric_key,comparison_adapter_key,parameters,"
                    "threshold_value,unit,severity,priority) VALUES (:profile,:metric,"
                    "'diversity-threshold-v1',CAST(:parameters AS json),:threshold,"
                    "'ratio','BLOCKING',:priority)"
                ),
                {
                    "profile": ids["diversity"],
                    "metric": metric,
                    "parameters": _json_parameter({"operator": "<="}),
                    "threshold": threshold,
                    "priority": priority * 10,
                },
            )
    _validate_configuration_version(
        connection,
        version_id=ids["diversity"],
        created=diversity_created,
        actor=actor,
    )

    worker_payload = {
        "concurrency_limit": 1,
        "batch_size": 1,
        "lease_seconds": 300,
        "heartbeat_seconds": 30,
        "timeout_seconds": 3600,
        "max_retries": 3,
        "backoff_seconds": 5,
        "global_serial": True,
    }
    ids["worker"], _ = _ensure_profile_version(
        connection,
        type_key="worker-execution-profile",
        version_number=1,
        payload=worker_payload,
        table_name="worker_execution_profile_versions",
        specialized_values={
            "concurrency_limit": 1,
            "batch_size": 1,
            "lease_seconds": 300,
            "heartbeat_seconds": 30,
            "timeout_seconds": 3600,
            "max_retries": 3,
            "backoff_seconds": 5,
            "resource_limits": {"global_serial": True},
        },
        dependencies=[],
        actor=actor,
        change_summary="Migrate the current single-worker lease contract",
    )

    scheduler_payload = {
        "schedule_kind": "INTERVAL",
        "interval_seconds": 7200,
        "timezone": "UTC",
        "jitter_seconds": 0,
        "catch_up_enabled": False,
        "revalidation_interval_seconds": 7200,
        "claim_ordering": [
            "validation_priority DESC",
            "last_completed_validation_at NULLS FIRST",
            "created_at ASC",
            "id ASC",
        ],
    }
    ids["scheduler"], _ = _ensure_profile_version(
        connection,
        type_key="scheduler-profile",
        version_number=1,
        payload=scheduler_payload,
        table_name="scheduler_profile_versions",
        specialized_values={
            "schedule_kind": "INTERVAL",
            "cron_expression": None,
            "interval_seconds": 7200,
            "timezone": "UTC",
            "jitter_seconds": 0,
            "catch_up_enabled": False,
            "revalidation_interval_seconds": 7200,
            "claim_ordering": scheduler_payload["claim_ordering"],
            "enabled": True,
        },
        dependencies=[],
        actor=actor,
        change_summary="Migrate the current bi-hourly generation schedule",
    )

    market_policy_payload = {
        "downloader_adapter_key": "okx-public-candles-v1",
        "public_market_data_only": True,
        "overlap_by_timeframe": {"5m": 72, "15m": 24},
        "repair_gaps": True,
        "max_data_age_seconds": 7200,
        "closed_candles_only": True,
    }
    ids["market_policy"], _ = _ensure_profile_version(
        connection,
        type_key="market-data-policy",
        version_number=1,
        payload=market_policy_payload,
        table_name="market_data_policy_versions",
        specialized_values={
            "downloader_adapter_key": "okx-public-candles-v1",
            "timeframe_capability": {"5m": 300, "15m": 900},
            "overlap_by_timeframe": {"5m": 72, "15m": 24},
            "incremental_overlap_seconds": None,
            "repair_gaps": True,
            "max_data_age_seconds": 7200,
            "closed_candles_only": True,
            "resource_limits": {
                "overlap_by_timeframe": {"5m": 72, "15m": 24},
                "atomic_replace": True,
                "public_market_data_only": True,
            },
        },
        dependencies=[],
        actor=actor,
        change_summary="Migrate the public closed-candle six-hour overlap policy",
    )

    ids["freshness"], freshness_created = _ensure_profile_version(
        connection,
        type_key="evidence-freshness-profile",
        version_number=1,
        payload={
            "fail_closed": True,
            "rules": {
                "public_source_receipt": {"max_age_seconds": 7200, "future_skew_seconds": 2, "renewal_lead_seconds": 300},
                "market_file_scan": {"max_age_seconds": 7200, "future_skew_seconds": 2, "renewal_lead_seconds": 300},
            },
        },
        table_name="evidence_freshness_profile_versions",
        specialized_values={"fail_closed": True},
        dependencies=[],
        actor=actor,
        change_summary="Migrate current two-hour public evidence freshness",
        defer_validation=True,
    )
    if freshness_created:
        for evidence_kind in ("public_source_receipt", "market_file_scan"):
            connection.execute(
                text(
                    "INSERT INTO evidence_freshness_rules "
                    "(profile_version_id,evidence_kind,max_age_seconds,future_skew_seconds,"
                    "renewal_lead_seconds,expired_reason_code) VALUES (:profile,:kind,"
                    "7200,2,300,'EVIDENCE_STALE')"
                ),
                {"profile": ids["freshness"], "kind": evidence_kind},
            )
    _validate_configuration_version(
        connection,
        version_id=ids["freshness"],
        created=freshness_created,
        actor=actor,
    )

    ids["monitoring"], _ = _ensure_profile_version(
        connection,
        type_key="monitoring-profile",
        version_number=1,
        payload={
            "heartbeat_ttl_seconds": 30,
            "read_cache_ttl_seconds": 0,
            "soak_seconds": 604800,
            "probe_interval_seconds": 300,
            "max_probe_gap_seconds": 900,
            "retention_seconds": 2592000,
        },
        table_name="monitoring_profile_versions",
        specialized_values={
            "heartbeat_ttl_seconds": 30,
            "read_cache_ttl_seconds": 0,
            "soak_seconds": 604800,
            "probe_interval_seconds": 300,
            "max_probe_gap_seconds": 900,
            "retention_seconds": 2592000,
            "alert_thresholds": {"critical_failure_count": 3, "window_seconds": 600},
        },
        dependencies=[],
        actor=actor,
        change_summary="Migrate current heartbeat and seven-day soak evidence intervals",
    )

    risk_payload = {
        "stake_amount": 1000,
        "stake_currency": "USDT",
        "leverage": 2,
        "max_open_positions": 3,
        "max_order_notional": 1000,
        "max_total_exposure": 3000,
        "max_price_deviation_pct": 0.01,
        "max_orders_per_5_minutes": 6,
        "max_orders_per_hour": 24,
        "fail_closed": True,
        "allow_real_funds": False,
    }
    ids["risk"], risk_created = _ensure_profile_version(
        connection,
        type_key="risk-profile",
        version_number=1,
        payload=risk_payload,
        table_name="risk_profile_versions",
        specialized_values={
            "stake_amount": 1000,
            "stake_currency": "USDT",
            "leverage": 2,
            "max_open_positions": 3,
            "fail_closed": True,
            "allow_real_funds": False,
        },
        dependencies=[],
        actor=actor,
        change_summary="Migrate the current OKX Demo risk policy",
        defer_validation=True,
    )
    if risk_created:
        for priority, (key, threshold, unit) in enumerate(
            (
                ("max_order_notional", 1000, "USDT"),
                ("max_total_exposure", 3000, "USDT"),
                ("max_price_deviation_pct", 0.01, "ratio"),
                ("max_orders_per_5_minutes", 6, "count"),
                ("max_orders_per_hour", 24, "count"),
            ),
            start=1,
        ):
            connection.execute(
                text(
                    "INSERT INTO risk_rules "
                    "(profile_version_id,rule_key,scope_selector,evaluation_adapter_key,"
                    "parameters,threshold_value,threshold_max,unit,severity,priority) "
                    "VALUES (:profile,:key,'{}'::json,'threshold-comparison-v1',"
                    "CAST(:parameters AS json),NULL,:threshold,:unit,'BLOCKING',:priority)"
                ),
                {
                    "profile": ids["risk"],
                    "key": key,
                    "parameters": _json_parameter({"operator": "<="}),
                    "threshold": threshold,
                    "unit": unit,
                    "priority": priority * 10,
                },
            )
    _validate_configuration_version(
        connection,
        version_id=ids["risk"],
        created=risk_created,
        actor=actor,
    )

    ids["capacity"], _ = _ensure_profile_version(
        connection,
        type_key="capacity-profile",
        version_number=1,
        payload={
            "max_active_instances": 9,
            "max_instances_per_instrument": 3,
            "slot_min": 1,
            "slot_max": 9,
            "source": "demo_automation.demo_risk_policy",
        },
        table_name="capacity_profile_versions",
        specialized_values={
            "max_active_instances": 9,
            "max_instances_per_instrument": 3,
            "slot_min": 1,
            "slot_max": 9,
            "allocation_policy": {"order": ["slot ASC"], "config_driven": True},
        },
        dependencies=[],
        actor=actor,
        change_summary="Move the current active strategy capacity out of schema checks",
    )

    ids["runtime"], _ = _ensure_profile_version(
        connection,
        type_key="runtime-profile",
        version_number=1,
        payload={
            "runtime_adapter_key": "docker-runtime-v1",
            "startup_timeout_seconds": 30,
            "stop_timeout_seconds": 15,
            "heartbeat_seconds": 5,
            "demo_only": True,
            "allow_real_funds": False,
            "single_writer_required": True,
            "credential_attestation": "OUT_OF_SCOPE_UNKNOWN",
        },
        table_name="runtime_profile_versions",
        specialized_values={
            "runtime_adapter_key": "docker-runtime-v1",
            "startup_timeout_seconds": 30,
            "stop_timeout_seconds": 15,
            "heartbeat_seconds": 5,
            "image_policy": {"immutable_digest_required": True},
            "runtime_limits": {"one_strategy_per_instance": True},
            "demo_only": True,
            "allow_real_funds": False,
            "single_writer_required": True,
            "secret_material_present": False,
            "executable_payload_present": False,
        },
        dependencies=[],
        actor=actor,
        change_summary="Migrate runtime metadata without starting or restarting a runtime",
    )

    ids["promotion"], promotion_created = _ensure_profile_version(
        connection,
        type_key="promotion-profile",
        version_number=1,
        payload={
            "quality_gate_profile_version_id": quality_gate_id,
            "minimum_market_regime_count": 3,
            "required_approval_count": 1,
            "qualified_only": True,
            "human_approval_required": True,
            "auto_deploy": False,
        },
        table_name="promotion_profile_versions",
        specialized_values={
            "quality_gate_profile_version_id": quality_gate_id,
            "minimum_market_regime_count": 3,
            "required_approval_count": 1,
            "evidence_chain_requirements": {"dynamic_quality_summary": "QUALIFIED"},
            "approval_policy": {"human_approval_required": True, "auto_deploy": False},
        },
        dependencies=[(quality_gate_id, "quality_gate")],
        actor=actor,
        change_summary="Preserve QUALIFIED-only approval separation",
        defer_validation=True,
    )
    if promotion_created:
        connection.execute(
            text(
                "INSERT INTO promotion_rules "
                "(profile_version_id,rule_key,metric_definition_version_id,"
                "evaluation_adapter_key,parameters,threshold_value,severity,priority) "
                "VALUES (:profile,'qualified_dynamic_summary',NULL,'threshold-comparison-v1',"
                "CAST(:parameters AS json),NULL,'BLOCKING',10)"
            ),
            {
                "profile": ids["promotion"],
                "parameters": _json_parameter({"required_status": "QUALIFIED"}),
            },
        )
    _validate_configuration_version(
        connection,
        version_id=ids["promotion"],
        created=promotion_created,
        actor=actor,
    )

    ids["deployment"], _ = _ensure_profile_version(
        connection,
        type_key="deployment-profile",
        version_number=1,
        payload={
            "execution_target": "OKX_DEMO",
            "promotion_profile_version_id": ids["promotion"],
            "risk_profile_version_id": ids["risk"],
            "capacity_profile_version_id": ids["capacity"],
            "runtime_profile_version_id": ids["runtime"],
            "monitoring_profile_version_id": ids["monitoring"],
            "evidence_freshness_profile_version_id": ids["freshness"],
            "demo_only": True,
            "allow_real_funds": False,
            "single_writer_required": True,
        },
        table_name="deployment_profile_versions",
        specialized_values={
            "execution_target_definition_id": execution_targets["OKX_DEMO"][0],
            "promotion_profile_version_id": ids["promotion"],
            "risk_profile_version_id": ids["risk"],
            "capacity_profile_version_id": ids["capacity"],
            "runtime_profile_version_id": ids["runtime"],
            "monitoring_profile_version_id": ids["monitoring"],
            "evidence_freshness_profile_version_id": ids["freshness"],
            "target_selector": {"target_key": "OKX_DEMO"},
            "strategy_selector": {"quality_status": "QUALIFIED", "approval_required": True},
            "demo_only": True,
            "allow_real_funds": False,
            "single_writer_required": True,
        },
        dependencies=[
            (execution_targets["OKX_DEMO"][1], "execution_target"),
            (ids["promotion"], "promotion"),
            (ids["risk"], "risk"),
            (ids["capacity"], "capacity"),
            (ids["runtime"], "runtime"),
            (ids["monitoring"], "monitoring"),
            (ids["freshness"], "freshness"),
        ],
        actor=actor,
        change_summary="Assemble Demo-only deployment policy without deploying anything",
    )

    ids["market_data"], _ = _ensure_profile_version(
        connection,
        type_key="market-data-profile",
        version_number=1,
        payload={
            "research_target_config_set_id": target_config_id,
            "market_data_policy_version_id": ids["market_policy"],
            "evidence_freshness_profile_version_id": ids["freshness"],
            "worker_execution_profile_version_id": ids["worker"],
        },
        table_name="market_data_profile_versions",
        specialized_values={
            "research_target_config_set_id": target_config_id,
            "market_data_policy_version_id": ids["market_policy"],
            "evidence_freshness_profile_version_id": ids["freshness"],
            "worker_execution_profile_version_id": ids["worker"],
        },
        dependencies=[
            (target_config_id, "targets"),
            (ids["market_policy"], "market_data_policy"),
            (ids["freshness"], "freshness"),
            (ids["worker"], "worker"),
        ],
        actor=actor,
        change_summary="Assemble public market-data workflow",
    )

    ids["optimization"], _ = _ensure_profile_version(
        connection,
        type_key="optimization-profile",
        version_number=1,
        payload={
            "optimizer_adapter_key": "freqtrade-hyperopt-v1",
            "spaces": ["buy", "sell", "roi", "stoploss", "trailing"],
            "loss_key": "SharpeHyperOptLoss",
            "max_epochs": 100,
            "selected_trial_creates_new_version": True,
            "auto_deploy": False,
        },
        table_name="optimization_profile_versions",
        specialized_values={
            "optimizer_adapter_key": "freqtrade-hyperopt-v1",
            "research_target_config_set_id": target_config_id,
            "validation_window_config_set_id": window_config_id,
            "scoring_profile_version_id": ids["scoring"],
            "quality_gate_profile_version_id": quality_gate_id,
            "market_data_policy_version_id": ids["market_policy"],
            "worker_execution_profile_version_id": ids["worker"],
            "spaces": ["buy", "sell", "roi", "stoploss", "trailing"],
            "loss_key": "SharpeHyperOptLoss",
            "max_epochs": 100,
            "default_parameters": {},
            "resource_limits": {"global_serial": True},
            "executable_payload_present": False,
        },
        dependencies=[
            (target_config_id, "targets"),
            (window_config_id, "windows"),
            (ids["scoring"], "scoring"),
            (quality_gate_id, "quality_gate"),
            (ids["market_policy"], "market_data_policy"),
            (ids["worker"], "worker"),
        ],
        actor=actor,
        change_summary="Migrate the current safe Hyperopt profile",
    )

    ids["ui"], _ = _ensure_profile_version(
        connection,
        type_key="ui-presentation-profile",
        version_number=1,
        payload={
            "default_sort": ["created_at DESC", "id DESC"],
            "visible_columns": ["name", "source", "pair", "timeframe", "research_status", "overall_score"],
            "filter_order": ["search", "source", "provider", "model", "family", "pair", "timeframe"],
            "page_capabilities": {"strategy_catalog": True, "configuration": True},
        },
        table_name="ui_presentation_profile_versions",
        specialized_values={
            "default_sort": ["created_at DESC", "id DESC"],
            "visible_columns": ["name", "source", "pair", "timeframe", "research_status", "overall_score"],
            "filter_order": ["search", "source", "provider", "model", "family", "pair", "timeframe"],
            "status_display_metadata": {
                "UNKNOWN": {"name_zh": "未知"},
                "BLOCKED": {"name_zh": "已阻断"},
                "QUALIFIED": {"name_zh": "质量合格"},
            },
            "page_capabilities": {"strategy_catalog": True, "configuration": True},
            "executable_payload_present": False,
        },
        dependencies=[],
        actor=actor,
        change_summary="Move catalog presentation defaults into versioned metadata",
    )

    research_dependencies = [
        (target_config_id, "targets"),
        (window_config_id, "windows"),
        (quality_gate_id, "quality_gate"),
        (ids["scoring"], "scoring"),
        (ids["diversity"], "diversity"),
        (ids["generation"], "generation"),
        (ids["provider"], "provider_model"),
        (ids["market_policy"], "market_data_policy"),
        (ids["freshness"], "freshness"),
        (ids["scheduler"], "scheduler"),
        (ids["worker"], "worker"),
    ]
    ids["research"], _ = _ensure_profile_version(
        connection,
        type_key="research-profile",
        version_number=1,
        payload={
            "profile_key": "formal-strategy-research-v1",
            "resolved_dependencies": {
                relation: version for version, relation in research_dependencies
            },
            "generation_and_validation_separated": True,
            "qualification_requires_dynamic_evidence": True,
        },
        table_name="research_profile_versions",
        specialized_values={
            "research_target_config_set_id": target_config_id,
            "validation_window_config_set_id": window_config_id,
            "quality_gate_profile_version_id": quality_gate_id,
            "scoring_profile_version_id": ids["scoring"],
            "diversity_profile_version_id": ids["diversity"],
            "generation_profile_version_id": ids["generation"],
            "provider_model_config_version_id": ids["provider"],
            "market_data_policy_version_id": ids["market_policy"],
            "evidence_freshness_profile_version_id": ids["freshness"],
            "scheduler_profile_version_id": ids["scheduler"],
            "worker_execution_profile_version_id": ids["worker"],
        },
        dependencies=research_dependencies,
        actor=actor,
        change_summary="Create the sole formal research aggregate profile",
    )
    return ids


def _resolve_configuration_graph(
    connection: Connection, aggregate_version_id: int
) -> tuple[dict[str, int], dict[str, str]]:
    pending = [aggregate_version_id]
    seen: set[int] = set()
    resolved: dict[str, tuple[int, str]] = {}
    while pending:
        version_id = pending.pop()
        if version_id in seen:
            continue
        row = connection.execute(
            text(
                "SELECT id,type_key,lifecycle_status,config_digest "
                "FROM configuration_versions WHERE id=:id"
            ),
            {"id": version_id},
        ).mappings().first()
        if row is None or row["lifecycle_status"] != "VALIDATED":
            raise StrategyPlatformTask1Blocked(
                f"configuration graph references non-validated version {version_id}"
            )
        # Registry types intentionally have many independently versioned members
        # (metrics, families, sources, triggers).  The bundle map therefore uses
        # a deterministic type/id key instead of collapsing a type to one row.
        map_key = f"{row['type_key']}:{int(row['id'])}"
        resolved[map_key] = (int(row["id"]), row["config_digest"])
        seen.add(version_id)
        children = connection.execute(
            text(
                "SELECT depends_on_version_id FROM configuration_dependencies "
                "WHERE configuration_version_id=:id ORDER BY relation_key,depends_on_version_id"
            ),
            {"id": version_id},
        ).scalars()
        pending.extend(int(child) for child in children)
    versions = {key: value[0] for key, value in sorted(resolved.items())}
    digests = {key: value[1] for key, value in sorted(resolved.items())}
    return versions, digests


def _ensure_bundle(
    connection: Connection,
    *,
    workflow_kind: str,
    scope_type: str,
    scope_key: str,
    aggregate_version_id: int,
) -> int:
    versions, digests = _resolve_configuration_graph(connection, aggregate_version_id)
    adapter_rows = [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT adapter_key,adapter_kind,implementation_version,"
                "input_schema_version,output_schema_version,capabilities,"
                "display_metadata,enabled,"
                "registry_metadata_only,contains_secret_material,"
                "contains_executable_payload FROM adapter_definitions "
                "ORDER BY adapter_key"
            )
        ).mappings()
    ]
    if not adapter_rows:
        raise StrategyPlatformTask1Blocked("adapter registry snapshot is empty")
    capability = {
        "demo_only": True,
        "allow_real_funds": False,
        "single_writer_required": True,
        "resolution_contract": "strategy-platform-owner-resolver-v1",
        "resolved_type_count": len(versions),
        "adapter_registry_contract": "strategy-platform-adapter-registry-v1",
        "adapter_registry_digest": canonical_digest(adapter_rows),
        "installed_adapter_manifest_digest": installed_adapter_manifest_digest(),
        "adapter_registry_keys": [row["adapter_key"] for row in adapter_rows],
        "resolved_adapter_count": len(adapter_rows),
    }
    digest = configuration_bundle_digest(
        workflow_kind=workflow_kind,
        scope_type=scope_type,
        scope_key=scope_key,
        aggregate_profile_version_id=aggregate_version_id,
        resolved_versions_json=versions,
        resolved_digests_json=digests,
        capability_snapshot=capability,
    )
    existing = connection.execute(
        text(
            "SELECT id,aggregate_profile_version_id,resolved_versions_json,"
            "resolved_digests_json,capability_snapshot "
            "FROM configuration_bundle_snapshots WHERE workflow_kind=:workflow "
            "AND scope_type=:scope_type AND scope_key=:scope_key AND bundle_digest=:digest"
        ),
        {
            "workflow": workflow_kind,
            "scope_type": scope_type,
            "scope_key": scope_key,
            "digest": digest,
        },
    ).mappings().first()
    if existing is not None:
        if (
            int(existing["aggregate_profile_version_id"]) != aggregate_version_id
            or existing["resolved_versions_json"] != versions
            or existing["resolved_digests_json"] != digests
            or existing["capability_snapshot"] != capability
        ):
            raise StrategyPlatformTask1Blocked(
                f"bundle identity conflict: {workflow_kind} {scope_key}"
            )
        return int(existing["id"])
    return int(
        connection.execute(
            text(
                "INSERT INTO configuration_bundle_snapshots "
                "(workflow_kind,scope_type,scope_key,aggregate_profile_version_id,"
                "resolved_versions_json,resolved_digests_json,bundle_digest,capability_snapshot) "
                "VALUES (:workflow,:scope_type,:scope_key,:aggregate,CAST(:versions AS json),"
                "CAST(:digests AS json),:digest,CAST(:capability AS json)) RETURNING id"
            ),
            {
                "workflow": workflow_kind,
                "scope_type": scope_type,
                "scope_key": scope_key,
                "aggregate": aggregate_version_id,
                "versions": _json_parameter(versions),
                "digests": _json_parameter(digests),
                "digest": digest,
                "capability": _json_parameter(capability),
            },
        ).scalar_one()
    )


def _activate_and_bundle_workflows(
    connection: Connection,
    *,
    ids: Mapping[str, int],
    target_config_id: int,
    window_config_id: int,
    quality_gate_id: int,
    actor: str,
) -> dict[str, int]:
    research_versions = {
        "research-target-config-set": target_config_id,
        "validation-window-config-set": window_config_id,
        "quality-gate-profile": quality_gate_id,
        "provider-model-config": ids["provider"],
        "generation-profile": ids["generation"],
        "scoring-profile": ids["scoring"],
        "diversity-profile": ids["diversity"],
        "worker-execution-profile": ids["worker"],
        "scheduler-profile": ids["scheduler"],
        "market-data-policy": ids["market_policy"],
        "evidence-freshness-profile": ids["freshness"],
        "research-profile": ids["research"],
    }
    market_versions = {
        "research-target-config-set": target_config_id,
        "market-data-policy": ids["market_policy"],
        "evidence-freshness-profile": ids["freshness"],
        "worker-execution-profile": ids["worker"],
        "market-data-profile": ids["market_data"],
    }
    optimization_versions = {
        "research-target-config-set": target_config_id,
        "validation-window-config-set": window_config_id,
        "quality-gate-profile": quality_gate_id,
        "scoring-profile": ids["scoring"],
        "market-data-policy": ids["market_policy"],
        "worker-execution-profile": ids["worker"],
        "optimization-profile": ids["optimization"],
    }
    deployment_versions = {
        "promotion-profile": ids["promotion"],
        "risk-profile": ids["risk"],
        "capacity-profile": ids["capacity"],
        "runtime-profile": ids["runtime"],
        "monitoring-profile": ids["monitoring"],
        "evidence-freshness-profile": ids["freshness"],
        "deployment-profile": ids["deployment"],
    }
    ui_versions = {"ui-presentation-profile": ids["ui"]}
    workflow_specs = (
        ("RESEARCH", "production-research", ids["research"], research_versions),
        ("RESEARCH", "design-lab", ids["research"], research_versions),
        ("MARKET_DATA", "production-market-data", ids["market_data"], market_versions),
        ("MARKET_DATA", "design-lab-market-data", ids["market_data"], market_versions),
        ("OPTIMIZATION", "production-optimization", ids["optimization"], optimization_versions),
        ("OPTIMIZATION", "design-lab-optimization", ids["optimization"], optimization_versions),
        ("DEPLOYMENT", "production-demo", ids["deployment"], deployment_versions),
        ("UI", "production-ui", ids["ui"], ui_versions),
        ("UI", "design-lab-ui", ids["ui"], ui_versions),
    )
    bundles: dict[str, int] = {}
    for workflow, scope_key, aggregate_id, versions in workflow_specs:
        for config_type, version_id in versions.items():
            _ensure_activation(
                connection,
                config_type=config_type,
                scope_type="WORKFLOW",
                scope_key=scope_key,
                version_id=version_id,
                actor=actor,
            )
        bundles[scope_key] = _ensure_bundle(
            connection,
            workflow_kind=workflow,
            scope_type="WORKFLOW",
            scope_key=scope_key,
            aggregate_version_id=aggregate_id,
        )
    return bundles


def seed_v13_configuration_graph(
    connection: Connection,
    *,
    market_inventory: Sequence[MarketFileEvidence],
    actor: str,
    aggregate_source_matrix_status: str,
) -> dict[str, Any]:
    """Create the first fully versioned profiles from current code/data facts."""

    inventory = validate_market_inventory(market_inventory)
    if aggregate_source_matrix_status != "PASSED":
        raise StrategyPlatformTask1Blocked(
            "aggregate public source matrix is not PASSED; active configuration is blocked"
        )
    _ensure_configuration_types(connection)
    registry_versions = _seed_registries(connection, actor=actor)
    registered_durations = {
        str(row["timeframe_key"]): int(row["duration_seconds"])
        for row in connection.execute(
            text(
                "SELECT definition.timeframe_key,version.duration_seconds "
                "FROM timeframe_definition_versions version "
                "JOIN timeframe_definitions definition "
                "ON definition.id=version.timeframe_definition_id "
                "WHERE version.configuration_version_id=ANY(:version_ids)"
            ),
            {"version_ids": list(registry_versions["timeframes"].values())},
        ).mappings()
    }
    evidence_durations = {
        item.timeframe: item.expected_interval_seconds for item in inventory
    }
    if registered_durations != evidence_durations:
        raise StrategyPlatformTask1Blocked(
            "market interval evidence does not match timeframe registry versions"
        )
    execution_targets = _ensure_execution_targets(connection, actor=actor)
    target_config = _seed_research_target_config(
        connection,
        market_inventory=inventory,
        timeframe_versions=registry_versions["timeframes"],
        actor=actor,
    )
    window_config = _seed_validation_window_config(
        connection, market_inventory=inventory, actor=actor
    )
    formal_quality, legacy_quality = _seed_quality_profiles(
        connection,
        metric_versions=registry_versions["metrics"],
        actor=actor,
    )
    profile_ids = _seed_workflow_profiles(
        connection,
        registry_versions=registry_versions,
        execution_targets=execution_targets,
        target_config_id=target_config,
        window_config_id=window_config,
        quality_gate_id=formal_quality,
        actor=actor,
    )
    bundles = _activate_and_bundle_workflows(
        connection,
        ids=profile_ids,
        target_config_id=target_config,
        window_config_id=window_config,
        quality_gate_id=formal_quality,
        actor=actor,
    )
    return {
        "registry_versions": registry_versions,
        "execution_targets": execution_targets,
        "target_config_id": target_config,
        "window_config_id": window_config,
        "formal_quality_id": formal_quality,
        "legacy_quality_id": legacy_quality,
        "profile_ids": profile_ids,
        "bundles": bundles,
    }


# Only Task 1 additive columns are removed from the legacy-content digest.  This
# makes the source snapshot stable across the first migration and its mandatory
# idempotency replay while still hashing every pre-existing fact.
_LEGACY_SNAPSHOT_EXCLUDED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "research_jobs": ("configuration_bundle_snapshot_id",),
    "strategy_deployments": (
        "strategy_target_id",
        "configuration_bundle_snapshot_id",
    ),
    "strategy_validation_plans": (
        "strategy_target_id",
        "quality_gate_profile_version_id",
        "validation_window_config_set_id",
        "configuration_bundle_snapshot_id",
        "cycle_number",
        "trigger_source_key",
        "trigger_metadata",
        "started_at",
        "policy_snapshot_digest",
        "market_data_snapshot_digest",
    ),
    "strategy_validation_windows": (
        "window_config_id",
        "window_key_snapshot",
        "name_zh_snapshot",
        "description_zh_snapshot",
        "attempt_number",
        "net_profit_after_cost",
        "max_drawdown",
        "volatility",
        "total_trades",
        "failure_code",
        "failure_message",
    ),
    "trade_intents": (
        "deployment_id",
        "signal_evaluation_id",
        "runtime_instance_row_id",
    ),
    "market_data_quality_receipts": (
        "idempotency_key",
        "quality_scope",
        "quality_decision",
        "file_identity_digest",
        "source_identity_digest",
        "aggregate_receipt_digest",
        "migration_artifact_digest",
        "freshness_basis",
    ),
}

_FOUNDATION_DATA_TABLES = (
    "configuration_types",
    "configuration_versions",
    "configuration_dependencies",
    "configuration_activations",
    "configuration_audit_events",
    "configuration_bundle_snapshots",
    "execution_target_definitions",
    "execution_target_definition_versions",
    "strategy_targets",
    "validation_window_config_sets",
    "validation_window_purposes",
    "market_regime_definitions",
    "validation_window_configs",
    "validation_window_expectations",
    "metric_definitions",
    "metric_definition_versions",
    "quality_gate_profiles",
    "quality_gate_profile_versions",
    "quality_gate_rules",
    "validation_window_scores",
    "validation_window_score_components",
    "quality_rule_evaluations",
    "strategy_evaluation_summaries",
)

_TARGET_SNAPSHOT_TABLES = tuple(
    dict.fromkeys(
        (
            *_FOUNDATION_DATA_TABLES,
            *(
                table_name
                for table_name in STRATEGY_PLATFORM_V13_EXTENSION_TABLES
                if not table_name.startswith("strategy_platform_migration_")
            ),
            "research_jobs",
            "strategy_deployments",
            "strategy_validation_plans",
            "strategy_validation_windows",
            "trade_intents",
            "market_data_quality_receipts",
            *LEGACY_ENTITY_TABLES,
        )
    )
)


def _table_fact(
    connection: Connection,
    table_name: str,
    *,
    legacy_content: bool,
) -> dict[str, Any]:
    allowed = set(LEGACY_ENTITY_TABLES) | set(_TARGET_SNAPSHOT_TABLES)
    if table_name not in allowed:
        raise StrategyPlatformTask1Blocked(f"unsupported snapshot table: {table_name}")
    columns = {
        column["name"] for column in inspect(connection).get_columns(table_name)
    }
    if "id" not in columns:
        primary_key = inspect(connection).get_pk_constraint(table_name).get(
            "constrained_columns"
        ) or []
        if len(primary_key) != 1:
            raise StrategyPlatformTask1Blocked(
                f"snapshot table has no scalar identity: {table_name}"
            )
        identity_column = primary_key[0]
    else:
        identity_column = "id"
    excluded = (
        _LEGACY_SNAPSHOT_EXCLUDED_COLUMNS.get(table_name, ())
        if legacy_content
        else ()
    )
    unknown_exclusions = sorted(set(excluded) - columns)
    if unknown_exclusions:
        raise StrategyPlatformTask1Blocked(
            f"snapshot exclusions are absent on {table_name}: "
            + ", ".join(unknown_exclusions)
        )
    payload_expression = "to_jsonb(snapshot_row)"
    parameters: dict[str, Any] = {}
    if excluded:
        payload_expression += " - CAST(:excluded_columns AS text[])"
        parameters["excluded_columns"] = list(excluded)
    quoted_table = connection.dialect.identifier_preparer.quote(table_name)
    quoted_identity = connection.dialect.identifier_preparer.quote(identity_column)
    where_clause = ""
    if legacy_content and table_name == "market_data_quality_receipts":
        where_clause = (
            " WHERE contract_version <> 'market-data-quality-v13-v1'"
        )
    row = connection.execute(
        text(
            "SELECT count(*) AS row_count,"
            f"min({quoted_identity})::text AS minimum_id,"
            f"max({quoted_identity})::text AS maximum_id,"
            "encode(public.digest(convert_to(COALESCE(string_agg("
            "encode(public.digest(convert_to(("
            + payload_expression
            + ")::text,'UTF8'),'sha256'),'hex'),'' ORDER BY "
            + quoted_identity
            + "),''),'UTF8'),'sha256'),'hex') AS content_digest "
            f"FROM {quoted_table} snapshot_row{where_clause}"
        ),
        parameters,
    ).mappings().one()
    return {
        "table_name": table_name,
        "row_count": int(row["row_count"]),
        "minimum_id": row["minimum_id"],
        "maximum_id": row["maximum_id"],
        "content_digest": row["content_digest"],
        "legacy_content_projection": legacy_content,
        "excluded_columns": sorted(excluded),
    }


def collect_legacy_snapshot(connection: Connection) -> tuple[dict[str, Any], ...]:
    """Hash every legacy row without including columns added by Task 1."""

    existing = set(inspect(connection).get_table_names())
    missing = sorted(set(LEGACY_ENTITY_TABLES) - existing)
    if missing:
        raise StrategyPlatformTask1Blocked(
            "legacy snapshot tables are missing: " + ", ".join(missing)
        )
    return tuple(
        _table_fact(connection, table_name, legacy_content=True)
        for table_name in LEGACY_ENTITY_TABLES
    )


def _snapshot_digest(facts: Sequence[Mapping[str, Any]]) -> str:
    return canonical_digest(
        {
            "contract": "strategy-platform-v13-table-snapshot-v1",
            "tables": [dict(fact) for fact in facts],
        }
    )


def _collect_target_snapshot(connection: Connection) -> tuple[dict[str, Any], ...]:
    """Hash the complete post-migration owner state.

    The source snapshot uses a legacy projection so additive Task 1 columns do
    not make the immutable historical source appear changed.  The target must
    do the opposite: it binds every migrated column and every newly appended
    V1.3 receipt so replay drift cannot hide behind that projection.
    """

    return tuple(
        _table_fact(
            connection,
            table_name,
            legacy_content=False,
        )
        for table_name in _TARGET_SNAPSHOT_TABLES
    )


def _record_table_snapshots(
    connection: Connection,
    *,
    migration_run_id: int,
    phase: str,
    facts: Sequence[Mapping[str, Any]],
) -> None:
    observed_at = datetime.now(timezone.utc)
    for fact in facts:
        table_name = str(fact["table_name"])
        foreign_keys = inspect(connection).get_foreign_keys(table_name)
        orphan_checks: list[dict[str, Any]] = []
        orphan_count = 0
        for foreign_key in foreign_keys:
            constrained = foreign_key.get("constrained_columns") or []
            referred = foreign_key.get("referred_columns") or []
            referred_table = foreign_key.get("referred_table")
            if not constrained or len(constrained) != len(referred) or not referred_table:
                raise StrategyPlatformTask1Blocked(
                    f"cannot verify foreign key on {table_name}: {foreign_key.get('name')}"
                )
            quote = connection.dialect.identifier_preparer.quote
            join = " AND ".join(
                f"source.{quote(source_column)}=parent.{quote(parent_column)}"
                for source_column, parent_column in zip(constrained, referred)
            )
            all_source = " AND ".join(
                f"source.{quote(column)} IS NOT NULL" for column in constrained
            )
            all_parent_null = " AND ".join(
                f"parent.{quote(column)} IS NULL" for column in referred
            )
            count = int(
                connection.execute(
                    text(
                        f"SELECT count(*) FROM {quote(table_name)} source LEFT JOIN "
                        f"{quote(referred_table)} parent ON {join} WHERE ({all_source}) "
                        f"AND ({all_parent_null})"
                    )
                ).scalar_one()
            )
            orphan_count += count
            orphan_checks.append(
                {
                    "constraint": foreign_key.get("name"),
                    "referred_table": referred_table,
                    "orphan_count": count,
                }
            )
        unvalidated = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM pg_constraint constraint_row "
                    "JOIN pg_class table_row ON table_row.oid=constraint_row.conrelid "
                    "JOIN pg_namespace namespace_row ON namespace_row.oid=table_row.relnamespace "
                    "WHERE namespace_row.nspname=current_schema() "
                    "AND table_row.relname=:table AND NOT constraint_row.convalidated"
                ),
                {"table": table_name},
            ).scalar_one()
        )
        if orphan_count or unvalidated:
            raise StrategyPlatformTask1Blocked(
                f"table reconciliation failed for {table_name}: "
                f"orphans={orphan_count} unvalidated_constraints={unvalidated}"
            )
        connection.execute(
            text(
                "INSERT INTO strategy_platform_migration_table_snapshots "
                "(migration_run_id,snapshot_phase,table_name,row_count,minimum_id,"
                "maximum_id,orphan_count,content_digest,constraint_evidence,observed_at) "
                "VALUES (:run,:phase,:table,:count,:minimum,:maximum,:orphans,:digest,"
                "CAST(:constraints AS json),:observed_at) ON CONFLICT DO NOTHING"
            ),
            {
                "run": migration_run_id,
                "phase": phase,
                "table": fact["table_name"],
                "count": fact["row_count"],
                "minimum": fact["minimum_id"],
                "maximum": fact["maximum_id"],
                "orphans": orphan_count,
                "digest": fact["content_digest"],
                "constraints": _json_parameter(
                    {
                        "unvalidated_constraint_count": unvalidated,
                        "foreign_key_orphan_checks": orphan_checks,
                        "legacy_content_projection": bool(
                            fact.get("legacy_content_projection", False)
                        ),
                        "excluded_columns": list(fact.get("excluded_columns", [])),
                    }
                ),
                "observed_at": observed_at,
            },
        )


def _ensure_migration_run(
    connection: Connection,
    *,
    execution_scope: str,
    source_schema_version: str,
    source_snapshot_digest: str,
    actor: str,
    request_id: str,
    report_path: str | None,
    evidence_manifest: Mapping[str, Any],
) -> tuple[int, str]:
    evidence_manifest_digest = canonical_digest(evidence_manifest)
    existing = connection.execute(
        text(
            "SELECT id,status,operator_identity,request_id,source_schema_version,"
            "source_snapshot_digest,"
            "evidence_manifest_digest,report_path FROM "
            "strategy_platform_migration_runs WHERE migration_key=:key "
            "AND execution_scope=:scope AND request_id=:request_id "
            "ORDER BY id DESC LIMIT 1"
        ),
        {
            "key": TASK1_MIGRATION_KEY,
            "scope": execution_scope,
            "request_id": request_id,
        },
    ).mappings().first()
    if existing is not None:
        if (
            existing["operator_identity"] != actor
            or existing["source_schema_version"] != source_schema_version
            or existing["source_snapshot_digest"] != source_snapshot_digest
            or existing["evidence_manifest_digest"] != evidence_manifest_digest
            or existing["report_path"] != report_path
        ):
            raise StrategyPlatformTask1Blocked(
                "migration request identity conflict: request_id is already bound "
                "to different source/operator/evidence/report facts"
            )
        return int(existing["id"]), str(existing["status"])
    successful = connection.execute(
        text(
            "SELECT id,request_id FROM strategy_platform_migration_runs "
            "WHERE migration_key=:key AND execution_scope=:scope "
            "AND source_snapshot_digest=:digest "
            "AND evidence_manifest_digest=:evidence_digest "
            "AND status='SUCCEEDED' ORDER BY id DESC LIMIT 1"
        ),
        {
            "key": TASK1_MIGRATION_KEY,
            "scope": execution_scope,
            "digest": source_snapshot_digest,
            "evidence_digest": evidence_manifest_digest,
        },
    ).mappings().first()
    if successful is not None:
        raise StrategyPlatformTask1Blocked(
            "migration already succeeded for this source/evidence; an idempotency "
            "replay must use the exact original request/operator/report identity "
            f"(run_id={successful['id']}, request_id={successful['request_id']})"
        )
    run_id = connection.execute(
        text(
            "INSERT INTO strategy_platform_migration_runs "
            "(migration_key,execution_scope,source_schema_version,target_schema_version,"
            "source_snapshot_digest,status,operator_identity,request_id,unknown_dimensions,"
            "evidence_manifest,evidence_manifest_digest,report_path,destructive_write_count,"
            "overwritten_row_count,deleted_row_count) "
            "VALUES (:key,:scope,:source,:target,:digest,'PLANNED',:actor,:request_id,"
            "CAST(:unknown AS json),CAST(:evidence_manifest AS json),:evidence_digest,"
            ":report,0,0,0) RETURNING id"
        ),
        {
            "key": TASK1_MIGRATION_KEY,
            "scope": execution_scope,
            "source": source_schema_version,
            "target": TASK1_SCHEMA_VERSION,
            "digest": source_snapshot_digest,
            "actor": actor,
            "request_id": request_id,
            "evidence_manifest": _json_parameter(evidence_manifest),
            "evidence_digest": evidence_manifest_digest,
            "unknown": _json_parameter(
                [
                    "credential_attestation:OUT_OF_SCOPE",
                    "runtime_execution_evidence:UNKNOWN",
                    "legacy_missing_evidence:UNKNOWN",
                ]
            ),
            "report": report_path,
        },
    ).scalar_one()
    connection.execute(
        text(
            "UPDATE strategy_platform_migration_runs SET status='RUNNING',"
            "started_at=clock_timestamp() WHERE id=:id AND status='PLANNED'"
        ),
        {"id": run_id},
    )
    return int(run_id), "RUNNING"


def _market_quality_receipt_payload(item: MarketFileEvidence) -> dict[str, Any]:
    if item.inspected_at is None:
        raise StrategyPlatformTask1Blocked(
            f"market inspection timestamp is missing: {item.pair} {item.timeframe}"
        )
    return {
        "contract": "market-data-quality-v13-receipt-digest-v1",
        "contract_version": "market-data-quality-v13-v1",
        "exchange": item.exchange,
        "pair": item.pair,
        "timeframe": item.timeframe,
        "relative_path": item.relative_path,
        "file_format": item.file_format,
        "file_size": item.size_bytes,
        "file_sha256": item.sha256,
        "source_type": item.source_type,
        "source_receipt_path": item.source_receipt_path,
        "source_receipt_digest": item.source_receipt_digest,
        "source_response_chain_digest": item.source_response_chain_digest,
        "inspected_at": item.inspected_at,
        "row_count": item.row_count,
        "first_open_at": item.first_open_at,
        "last_open_at": item.last_open_at,
        "expected_interval_seconds": item.expected_interval_seconds,
        "missing_interval_count": item.gap_count,
        "duplicate_timestamp_count": item.duplicate_count,
        "out_of_order_count": item.out_of_order_count,
        "misaligned_timestamp_count": item.misaligned_timestamp_count,
        "null_ohlcv_count": item.null_count,
        "invalid_ohlc_count": item.invalid_ohlc_count,
        "negative_volume_count": item.negative_volume_count,
        "freshness_seconds": None,
        "quality_scope": item.quality_scope,
        "quality_decision": item.quality_decision,
        "file_identity_digest": item.file_identity_digest,
        "source_identity_digest": item.source_identity_digest,
        "aggregate_receipt_digest": item.aggregate_receipt_digest,
        "migration_artifact_digest": item.migration_artifact_digest,
        "freshness_basis": item.freshness_basis,
        "status": "PASSED",
        "reason_codes": [],
    }


def _market_quality_receipt_idempotency_key(item: MarketFileEvidence) -> str:
    return canonical_digest(
        {
            "contract": "market-data-quality-v13-idempotency-v1",
            "file_identity_digest": item.file_identity_digest,
            "source_identity_digest": item.source_identity_digest,
            "aggregate_receipt_digest": item.aggregate_receipt_digest,
            "migration_artifact_digest": item.migration_artifact_digest,
        }
    )


def _ensure_market_quality_receipts(
    connection: Connection,
    inventory: Sequence[MarketFileEvidence],
) -> dict[tuple[str, str], int]:
    """Persist one append-only, non-qualification receipt for each scanned file."""

    receipt_ids: dict[tuple[str, str], int] = {}
    for item in inventory:
        payload = _market_quality_receipt_payload(item)
        idempotency_key = _market_quality_receipt_idempotency_key(item)
        evidence_digest = canonical_digest(payload)
        connection.execute(
            text(
                "INSERT INTO market_data_quality_receipts "
                "(idempotency_key,contract_version,exchange,pair,timeframe,relative_path,"
                "file_format,file_size,file_sha256,source_type,source_receipt_path,"
                "source_receipt_digest,source_response_chain_digest,inspected_at,row_count,"
                "first_open_at,last_open_at,expected_interval_seconds,missing_interval_count,"
                "duplicate_timestamp_count,out_of_order_count,misaligned_timestamp_count,"
                "null_ohlcv_count,invalid_ohlc_count,negative_volume_count,freshness_seconds,"
                "quality_scope,quality_decision,file_identity_digest,source_identity_digest,"
                "aggregate_receipt_digest,migration_artifact_digest,freshness_basis,status,"
                "reason_codes,evidence_digest) VALUES (:idempotency,'market-data-quality-v13-v1',"
                ":exchange,:pair,:timeframe,:relative,:format,:size,:file_digest,:source_type,"
                ":source_path,:source_digest,:response_chain,:inspected,:rows,:first_open,"
                ":last_open,:interval,:missing,:duplicates,:out_of_order,:misaligned,:nulls,"
                ":invalid_ohlc,:negative_volume,NULL,:quality_scope,:quality_decision,"
                ":file_identity,:source_identity,:aggregate_digest,:artifact_digest,"
                ":freshness_basis,'PASSED','[]'::json,:evidence_digest) "
                "ON CONFLICT (idempotency_key) DO NOTHING"
            ),
            {
                "idempotency": idempotency_key,
                "exchange": item.exchange,
                "pair": item.pair,
                "timeframe": item.timeframe,
                "relative": item.relative_path,
                "format": item.file_format,
                "size": item.size_bytes,
                "file_digest": item.sha256,
                "source_type": item.source_type,
                "source_path": item.source_receipt_path,
                "source_digest": item.source_receipt_digest,
                "response_chain": item.source_response_chain_digest,
                "inspected": item.inspected_at,
                "rows": item.row_count,
                "first_open": item.first_open_at,
                "last_open": item.last_open_at,
                "interval": item.expected_interval_seconds,
                "missing": item.gap_count,
                "duplicates": item.duplicate_count,
                "out_of_order": item.out_of_order_count,
                "misaligned": item.misaligned_timestamp_count,
                "nulls": item.null_count,
                "invalid_ohlc": item.invalid_ohlc_count,
                "negative_volume": item.negative_volume_count,
                "quality_scope": item.quality_scope,
                "quality_decision": item.quality_decision,
                "file_identity": item.file_identity_digest,
                "source_identity": item.source_identity_digest,
                "aggregate_digest": item.aggregate_receipt_digest,
                "artifact_digest": item.migration_artifact_digest,
                "freshness_basis": item.freshness_basis,
                "evidence_digest": evidence_digest,
            },
        )
        row = connection.execute(
            text(
                "SELECT id,contract_version,exchange,pair,timeframe,relative_path,file_format,"
                "file_size,file_sha256,source_type,source_receipt_path,source_receipt_digest,"
                "source_response_chain_digest,inspected_at,row_count,first_open_at,last_open_at,"
                "expected_interval_seconds,missing_interval_count,duplicate_timestamp_count,"
                "out_of_order_count,misaligned_timestamp_count,null_ohlcv_count,"
                "invalid_ohlc_count,negative_volume_count,freshness_seconds,quality_scope,"
                "quality_decision,file_identity_digest,source_identity_digest,"
                "aggregate_receipt_digest,migration_artifact_digest,freshness_basis,status,"
                "reason_codes,evidence_digest FROM market_data_quality_receipts "
                "WHERE idempotency_key=:idempotency"
            ),
            {"idempotency": idempotency_key},
        ).mappings().one()
        actual = {
            "contract": "market-data-quality-v13-receipt-digest-v1",
            **{key: row[key] for key in payload if key != "contract"},
        }
        for instant_key in ("inspected_at", "first_open_at", "last_open_at"):
            actual[instant_key] = _aware_utc(actual[instant_key])
        if row["evidence_digest"] != evidence_digest or canonical_digest(actual) != evidence_digest:
            raise StrategyPlatformTask1Blocked(
                f"market quality receipt identity conflict: {item.pair} {item.timeframe}"
            )
        receipt_ids[(item.pair, item.timeframe)] = int(row["id"])
    return receipt_ids


def _insert_market_file_records(
    connection: Connection,
    inventory: Sequence[MarketFileEvidence],
    receipt_ids: Mapping[tuple[str, str], int],
) -> dict[tuple[str, str], int]:
    records: dict[tuple[str, str], int] = {}
    for item in inventory:
        receipt_id = receipt_ids.get((item.pair, item.timeframe))
        if receipt_id is None:
            raise StrategyPlatformTask1Blocked(
                f"market receipt is missing: {item.pair} {item.timeframe}"
            )
        scan_id = (
            f"{TASK1_MIGRATION_KEY}:{item.observed_at:%Y%m%dT%H%M%S}:"
            f"{item.sha256[:16]}"
        )
        scan_evidence_digest = _market_file_scan_digest(item)
        connection.execute(
            text(
                "INSERT INTO market_data_file_records "
                "(exchange,market_type,pair,instrument_id,timeframe,data_kind,"
                "absolute_path,relative_path,file_format,file_size,file_sha256,row_count,"
                "first_open_at,last_open_at,last_close_at,gap_count,duplicate_count,"
                "null_count,gap_evidence,freshness_status,scan_id,scan_evidence_digest,"
                "source_receipt_digest,source_receipt_id,"
                "observed_at) VALUES (:exchange,:market_type,:pair,:instrument,:timeframe,"
                ":data_kind,:absolute,:relative,:format,:size,:digest,:rows,:first_open,"
                ":last_open,:last_close,:gaps,:duplicates,:nulls,'[]'::json,:freshness,"
                ":scan,:scan_digest,:source_receipt_digest,:receipt,:observed) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "exchange": item.exchange,
                "market_type": item.market_type,
                "pair": item.pair,
                "instrument": item.instrument_id,
                "timeframe": item.timeframe,
                "data_kind": item.data_kind,
                "absolute": item.absolute_path,
                "relative": item.relative_path,
                "format": item.file_format,
                "size": item.size_bytes,
                "digest": item.sha256,
                "rows": item.row_count,
                "first_open": item.first_open_at,
                "last_open": item.last_open_at,
                "last_close": item.last_close_at,
                "gaps": item.gap_count,
                "duplicates": item.duplicate_count,
                "nulls": item.null_count,
                "freshness": (
                    "FRESH" if item.freshness_status == "PASSED" else "UNKNOWN"
                ),
                "scan": scan_id,
                "scan_digest": scan_evidence_digest,
                "source_receipt_digest": item.source_receipt_digest,
                "receipt": receipt_id,
                "observed": item.observed_at,
            },
        )
        row = connection.execute(
            text(
                "SELECT id,absolute_path,file_size,row_count,first_open_at,last_open_at,"
                "last_close_at,freshness_status FROM market_data_file_records "
                "WHERE exchange=:exchange AND market_type=:market_type AND pair=:pair "
                "AND timeframe=:timeframe AND data_kind=:data_kind "
                "AND relative_path=:relative AND file_sha256=:digest "
                "AND observed_at=:observed"
            ),
            {
                "exchange": item.exchange,
                "market_type": item.market_type,
                "pair": item.pair,
                "timeframe": item.timeframe,
                "data_kind": item.data_kind,
                "relative": item.relative_path,
                "digest": item.sha256,
                "observed": item.observed_at,
            },
        ).mappings().one()
        if (
            int(row["file_size"]) != item.size_bytes
            or int(row["row_count"]) != item.row_count
            or _aware_utc(row["first_open_at"]) != _aware_utc(item.first_open_at)
            or _aware_utc(row["last_open_at"]) != _aware_utc(item.last_open_at)
            or _aware_utc(row["last_close_at"]) != _aware_utc(item.last_close_at)
            or row["freshness_status"]
            != ("FRESH" if item.freshness_status == "PASSED" else "UNKNOWN")
        ):
            raise StrategyPlatformTask1Blocked(
                f"market file identity conflict: {item.pair} {item.timeframe}"
            )
        records[(item.pair, item.timeframe)] = int(row["id"])
    return records


def _ensure_strategy_targets_from_evidence(
    connection: Connection,
    *,
    execution_targets: Mapping[str, tuple[int, int]],
    inventory: Sequence[MarketFileEvidence],
) -> tuple[dict[tuple[int, str, str], int], dict[int, int]]:
    instrument_by_target = {
        (item.pair, item.timeframe): item.instrument_id for item in inventory
    }
    evidence_rows = connection.execute(
        text(
            "SELECT run.strategy_version_id,task.pair,task.timeframe,"
            "min(version.created_at) AS version_created_at,"
            "max(plan_evidence.last_validation_at) AS last_validation_at "
            "FROM backtest_runs run JOIN backtest_tasks task "
            "ON task.backtest_run_id=run.id JOIN strategy_versions version "
            "ON version.id=run.strategy_version_id LEFT JOIN ("
            "SELECT plan.strategy_version_id,promotion_task.pair,"
            "promotion_task.timeframe,max(plan.completed_at) AS last_validation_at "
            "FROM strategy_validation_plans plan JOIN backtest_results promotion "
            "ON promotion.id=plan.promotion_backtest_result_id "
            "JOIN backtest_tasks promotion_task "
            "ON promotion_task.id=promotion.backtest_task_id "
            "GROUP BY plan.strategy_version_id,promotion_task.pair,"
            "promotion_task.timeframe) plan_evidence "
            "ON plan_evidence.strategy_version_id=run.strategy_version_id "
            "AND plan_evidence.pair=task.pair "
            "AND plan_evidence.timeframe=task.timeframe "
            "GROUP BY run.strategy_version_id,task.pair,task.timeframe "
            "ORDER BY run.strategy_version_id,task.pair,task.timeframe"
        )
    ).mappings().all()
    all_version_ids = {
        int(value)
        for value in connection.execute(
            text("SELECT id FROM strategy_versions ORDER BY id")
        ).scalars()
    }
    evidence_by_version: dict[int, list[Mapping[str, Any]]] = {}
    for row in evidence_rows:
        evidence_by_version.setdefault(int(row["strategy_version_id"]), []).append(row)
    missing_versions = sorted(all_version_ids - set(evidence_by_version))
    if missing_versions:
        raise StrategyPlatformTask1Blocked(
            "strategy target evidence is incomplete: "
            f"missing_versions={missing_versions[:10]}"
        )

    research_definition_id = int(execution_targets["RESEARCH_ONLY"][0])
    version_targets: dict[tuple[int, str, str], int] = {}
    for version_id in sorted(all_version_ids):
        for row in evidence_by_version[version_id]:
            identity = (str(row["pair"]), str(row["timeframe"]))
            instrument_id = instrument_by_target.get(identity)
            if instrument_id is None:
                raise StrategyPlatformTask1Blocked(
                    f"no file-backed instrument mapping for version {version_id}: {identity}"
                )
            connection.execute(
                text(
                    "INSERT INTO strategy_targets "
                    "(strategy_version_id,execution_target_id,instrument_id,pair,timeframe,"
                    "status,validation_priority,last_completed_validation_at,created_at,updated_at) "
                    "VALUES (:version,:definition,:instrument,:pair,:timeframe,'ENABLED',100,"
                    ":completed,:created,:created) ON CONFLICT DO NOTHING"
                ),
                {
                    "version": version_id,
                    "definition": research_definition_id,
                    "instrument": instrument_id,
                    "pair": identity[0],
                    "timeframe": identity[1],
                    "completed": row["last_validation_at"],
                    "created": row["version_created_at"],
                },
            )
            target = connection.execute(
                text(
                    "SELECT id,pair,status,validation_priority FROM strategy_targets "
                    "WHERE strategy_version_id=:version AND execution_target_id=:definition "
                    "AND instrument_id=:instrument AND timeframe=:timeframe"
                ),
                {
                    "version": version_id,
                    "definition": research_definition_id,
                    "instrument": instrument_id,
                    "timeframe": identity[1],
                },
            ).mappings().one()
            if (
                target["pair"] != identity[0]
                or target["status"] != "ENABLED"
                or int(target["validation_priority"]) != 100
            ):
                raise StrategyPlatformTask1Blocked(
                    f"strategy target identity conflict for version {version_id}"
                )
            version_targets[(version_id, identity[0], identity[1])] = int(target["id"])

    demo_definition_id = int(execution_targets["OKX_DEMO"][0])
    deployment_targets: dict[int, int] = {}
    deployments = connection.execute(
        text(
            "SELECT id,strategy_version_id,instrument_id,timeframe,strategy_target_id,"
            "real_orders,created_at FROM strategy_deployments ORDER BY id"
        )
    ).mappings().all()
    for deployment in deployments:
        identity = next(
            (
                key
                for key, instrument in instrument_by_target.items()
                if instrument == deployment["instrument_id"]
                and key[1] == deployment["timeframe"]
            ),
            None,
        )
        if identity is None or deployment["real_orders"] is not False:
            raise StrategyPlatformTask1Blocked(
                f"deployment {deployment['id']} has no Demo-only target evidence"
            )
        connection.execute(
            text(
                "INSERT INTO strategy_targets "
                "(strategy_version_id,execution_target_id,instrument_id,pair,timeframe,"
                "status,validation_priority,created_at,updated_at) "
                "VALUES (:version,:definition,:instrument,:pair,:timeframe,'ENABLED',100,"
                ":created,:created) ON CONFLICT DO NOTHING"
            ),
            {
                "version": deployment["strategy_version_id"],
                "definition": demo_definition_id,
                "instrument": deployment["instrument_id"],
                "pair": identity[0],
                "timeframe": identity[1],
                "created": deployment["created_at"],
            },
        )
        target_id = int(
            connection.execute(
                text(
                    "SELECT id FROM strategy_targets WHERE strategy_version_id=:version "
                    "AND execution_target_id=:definition AND instrument_id=:instrument "
                    "AND timeframe=:timeframe"
                ),
                {
                    "version": deployment["strategy_version_id"],
                    "definition": demo_definition_id,
                    "instrument": deployment["instrument_id"],
                    "timeframe": identity[1],
                },
            ).scalar_one()
        )
        existing_target = deployment["strategy_target_id"]
        if existing_target is not None and int(existing_target) != target_id:
            raise StrategyPlatformTask1Blocked(
                f"deployment {deployment['id']} target conflict"
            )
        if existing_target is None:
            connection.execute(
                text(
                    "UPDATE strategy_deployments SET strategy_target_id=:target "
                    "WHERE id=:id AND strategy_target_id IS NULL"
                ),
                {"id": deployment["id"], "target": target_id},
            )
        deployment_targets[int(deployment["id"])] = target_id
    return version_targets, deployment_targets


def _parse_timerange(timerange: str) -> tuple[datetime, datetime]:
    match = _TIMERANGE_RE.fullmatch(timerange or "")
    if match is None:
        raise StrategyPlatformTask1Blocked(f"invalid legacy timerange: {timerange!r}")
    start = datetime.strptime(match.group("start"), "%Y%m%d").replace(
        tzinfo=timezone.utc
    )
    end = datetime.strptime(match.group("end"), "%Y%m%d").replace(
        tzinfo=timezone.utc
    )
    if start >= end:
        raise StrategyPlatformTask1Blocked(f"empty legacy timerange: {timerange!r}")
    return start, end


def _legacy_window_key(row: Mapping[str, Any]) -> str:
    kind = re.sub(r"[^a-z0-9]+", "-", str(row["window_kind"] or "window").lower())
    state = re.sub(
        r"[^a-z0-9]+",
        "-",
        str(row["required_market_state"] or "unconstrained").lower(),
    )
    identity = canonical_digest(
        {
            "timerange": row["timerange"],
            "expected_market_data_digest": row["expected_market_data_digest"],
        }
    )[:12]
    return f"legacy-{int(row['ordinal']):03d}-{kind}-{state}-{identity}"[:120]


def _matching_receipt_id(
    connection: Connection,
    *,
    pair: str,
    timeframe: str,
    file_digest: str,
) -> int | None:
    if _SHA256_RE.fullmatch(file_digest or "") is None:
        return None
    value = connection.execute(
        text(
            "SELECT id FROM market_data_quality_receipts WHERE pair=:pair "
            "AND timeframe=:timeframe AND file_sha256=:digest AND status='PASSED' "
            "AND contract_version='market-data-quality-v13-v1' "
            "AND quality_scope='MIGRATION_SOURCE_CONSISTENCY_AS_OF_SOURCE_RECEIPT' "
            "AND quality_decision='NOT_STRATEGY_QUALIFICATION' "
            "ORDER BY inspected_at DESC,id DESC LIMIT 1"
        ),
        {"pair": pair, "timeframe": timeframe, "digest": file_digest},
    ).scalar_one_or_none()
    return None if value is None else int(value)


def _ensure_legacy_window_configuration(
    connection: Connection,
    *,
    actor: str,
) -> tuple[int, dict[int, int]]:
    source_rows = connection.execute(
        text(
            "SELECT w.id,w.ordinal,w.window_kind,w.required_market_state,w.timerange,"
            "w.expected_market_data_digest,w.market_state,w.market_state_source,"
            "w.market_state_algorithm,w.market_state_parameters,w.market_state_evidence,"
            "COALESCE(w.profile_snapshot->>'pair',task.pair) AS pair,"
            "COALESCE(w.profile_snapshot->>'timeframe',task.timeframe) AS timeframe "
            "FROM strategy_validation_windows w "
            "JOIN strategy_validation_plans p ON p.id=w.validation_plan_id "
            "LEFT JOIN backtest_tasks task ON task.id=w.backtest_task_id "
            "WHERE p.strategy_target_id IS NULL "
            "AND p.quality_gate_profile_version_id IS NULL "
            "AND p.validation_window_config_set_id IS NULL "
            "AND p.configuration_bundle_snapshot_id IS NULL "
            "AND p.trigger_source_key IS NULL ORDER BY w.id"
        )
    ).mappings().all()
    if not source_rows:
        raise StrategyPlatformTask1Blocked("legacy validation windows are absent")
    identities: dict[tuple[Any, ...], dict[str, Any]] = {}
    identity_by_window: dict[int, tuple[Any, ...]] = {}
    for row in source_rows:
        pair = row["pair"]
        timeframe = row["timeframe"]
        if not pair or not timeframe:
            raise StrategyPlatformTask1Blocked(
                f"validation window {row['id']} has no evidence-backed target"
            )
        start_at, end_at = _parse_timerange(str(row["timerange"]))
        window_key = _legacy_window_key(row)
        key = (
            str(pair),
            str(timeframe),
            window_key,
            start_at,
            end_at,
            row["expected_market_data_digest"],
        )
        candidate_evidence = row["market_state_evidence"] or {}
        existing = identities.get(key)
        if existing is None:
            identities[key] = {
                "pair": str(pair),
                "timeframe": str(timeframe),
                "data_kind": "futures",
                "window_key": window_key,
                "purpose_key": (
                    "out_of_sample"
                    if str(row["window_kind"] or "").upper() == "OOS"
                    else "legacy_validation"
                ),
                "ordinal": int(row["ordinal"]),
                "start_at": start_at,
                "end_at": end_at,
                "required_market_state": row["required_market_state"],
                "expected_market_data_digest": row["expected_market_data_digest"],
                "market_state": row["market_state"],
                "market_state_source": row["market_state_source"],
                "market_state_algorithm": row["market_state_algorithm"],
                "market_state_parameters": row["market_state_parameters"] or {},
                "classification_evidence": candidate_evidence,
            }
        else:
            evidence_rank = int(bool(candidate_evidence)) + int(bool(row["market_state"]))
            existing_rank = int(bool(existing["classification_evidence"])) + int(
                bool(existing["market_state"])
            )
            if evidence_rank > existing_rank:
                existing.update(
                    {
                        "market_state": row["market_state"],
                        "market_state_source": row["market_state_source"],
                        "market_state_algorithm": row["market_state_algorithm"],
                        "market_state_parameters": row["market_state_parameters"] or {},
                        "classification_evidence": candidate_evidence,
                    }
                )
        identity_by_window[int(row["id"])] = key
    ordered = sorted(
        identities.items(), key=lambda item: tuple(str(value) for value in item[0])
    )
    payload = {
        "contract": "legacy-validation-window-import-v1",
        "quality_status": "UNKNOWN",
        "qualification_allowed": False,
        "window_count": len(ordered),
        "windows": [
            {
                "pair": value["pair"],
                "timeframe": value["timeframe"],
                "window_key": value["window_key"],
                "start_at": _aware_utc(value["start_at"]).isoformat().replace(
                    "+00:00", "Z"
                ),
                "end_at": _aware_utc(value["end_at"]).isoformat().replace(
                    "+00:00", "Z"
                ),
                "required_market_state": value["required_market_state"],
                "expected_market_data_digest": value["expected_market_data_digest"],
            }
            for _, value in ordered
        ],
    }
    version_id, created = _ensure_configuration_version(
        connection,
        type_key="validation-window-config-set",
        version_number=2,
        payload=payload,
        created_by=actor,
        change_summary="Preserve historical validation windows without reclassifying evidence",
    )
    if created:
        connection.execute(
            text(
                "INSERT INTO validation_window_config_sets "
                "(id,name,default_classifier_adapter_key,default_classifier_parameters) "
                "VALUES (:id,'legacy-validation-window-import-v1',"
                "'window-close-return-v1',CAST(:parameters AS json))"
            ),
            {
                "id": version_id,
                "parameters": _json_parameter(
                    {
                        "bull_threshold": 0.05,
                        "bear_threshold": -0.05,
                        "classification_required": False,
                    }
                ),
            },
        )
        purposes = (
            (
                "legacy_validation",
                "历史验证窗口",
                "保留历史执行证据，不用于 V1.3 动态质量认定",
                False,
                10,
            ),
            (
                "out_of_sample",
                "历史样本外窗口",
                "保留历史 OOS 身份，不声明所需行情状态",
                False,
                20,
            ),
        )
        for key, name, description, counts, order in purposes:
            connection.execute(
                text(
                    "INSERT INTO validation_window_purposes "
                    "(config_set_id,key,name_zh,description_zh,counts_for_qualification,"
                    "enabled,sort_order) VALUES (:set_id,:key,:name,:description,"
                    ":counts,TRUE,:order)"
                ),
                {
                    "set_id": version_id,
                    "key": key,
                    "name": name,
                    "description": description,
                    "counts": counts,
                    "order": order,
                },
            )
        for index, (_, value) in enumerate(ordered, start=1):
            evidence = {
                "import_contract": "legacy-validation-evidence-v1",
                "quality_status": "UNKNOWN",
                "qualification_allowed": False,
                "expected_market_data_digest": value[
                    "expected_market_data_digest"
                ],
                "observed_market_state": value["market_state"],
                "market_state_source": value["market_state_source"],
                "market_state_algorithm": value["market_state_algorithm"],
                "market_state_parameters": value["market_state_parameters"],
                "original_evidence": value["classification_evidence"],
            }
            receipt_id = _matching_receipt_id(
                connection,
                pair=value["pair"],
                timeframe=value["timeframe"],
                file_digest=value["expected_market_data_digest"],
            )
            connection.execute(
                text(
                    "INSERT INTO validation_window_configs "
                    "(config_set_id,pair,timeframe,data_kind,window_key,purpose_key,"
                    "ordinal,name_zh,description_zh,start_at,end_at,"
                    "classifier_adapter_key,classifier_parameters,required,source_receipt_id,"
                    "classification_evidence) VALUES (:set_id,:pair,:timeframe,'futures',"
                    ":window_key,:purpose,:ordinal,:name,:description,:start_at,:end_at,"
                    "'window-close-return-v1',CAST(:parameters AS json),FALSE,:receipt,"
                    "CAST(:evidence AS json))"
                ),
                {
                    "set_id": version_id,
                    "pair": value["pair"],
                    "timeframe": value["timeframe"],
                    "window_key": value["window_key"],
                    "purpose": value["purpose_key"],
                    "ordinal": index,
                    "name": f"历史 {value['window_key']}",
                    "description": (
                        "历史执行窗口，仅保存来源与审计事实；不作为 V1.3 质量通过。"
                    ),
                    "start_at": value["start_at"],
                    "end_at": value["end_at"],
                    "parameters": _json_parameter(
                        {
                            "bull_threshold": 0.05,
                            "bear_threshold": -0.05,
                            "legacy_evidence_may_be_unknown": True,
                        }
                    ),
                    "receipt": receipt_id,
                    "evidence": _json_parameter(evidence),
                },
            )
    _validate_configuration_version(
        connection, version_id=version_id, created=created, actor=actor
    )
    ids_by_identity = {
        (
            row["pair"],
            row["timeframe"],
            row["window_key"],
            _aware_utc(row["start_at"]),
            _aware_utc(row["end_at"]),
            row["classification_evidence"].get("expected_market_data_digest"),
        ): int(row["id"])
        for row in connection.execute(
            text(
                "SELECT id,pair,timeframe,window_key,start_at,end_at,"
                "classification_evidence FROM validation_window_configs "
                "WHERE config_set_id=:id"
            ),
            {"id": version_id},
        ).mappings()
    }
    mapped = {
        window_id: ids_by_identity[identity]
        for window_id, identity in identity_by_window.items()
    }
    return version_id, mapped


def _ensure_legacy_bundle(
    connection: Connection,
    *,
    legacy_window_config_id: int,
    legacy_quality_id: int,
    actor: str,
) -> tuple[int, int]:
    payload = {
        "contract": "legacy-validation-profile-v1",
        "quality_status": "UNKNOWN",
        "qualification_allowed": False,
        "configuration_attestation": "UNKNOWN",
        "runtime_execution_evidence": "OUT_OF_SCOPE",
    }
    aggregate_id, created = _ensure_configuration_version(
        connection,
        type_key="legacy-validation-profile",
        version_number=1,
        payload=payload,
        created_by=actor,
        change_summary="Snapshot historical validation with unknown dynamic quality",
    )
    if created:
        _ensure_dependency(
            connection,
            parent_id=aggregate_id,
            child_id=legacy_window_config_id,
            relation_key="windows",
        )
        _ensure_dependency(
            connection,
            parent_id=aggregate_id,
            child_id=legacy_quality_id,
            relation_key="quality_gate",
        )
    _validate_configuration_version(
        connection, version_id=aggregate_id, created=created, actor=actor
    )
    bundle_id = _ensure_bundle(
        connection,
        workflow_kind="LEGACY_VALIDATION_IMPORT",
        scope_type="MIGRATION",
        scope_key="legacy-validation-evidence",
        aggregate_version_id=aggregate_id,
    )
    return aggregate_id, bundle_id


def _ensure_legacy_workflow_bundle(
    connection: Connection,
    *,
    type_key: str,
    workflow_kind: str,
    scope_key: str,
    actor: str,
) -> int:
    payload = {
        "contract": f"{type_key}-v1",
        "quality_status": "UNKNOWN",
        "configuration_attestation": "UNKNOWN",
        "runtime_execution_evidence": "OUT_OF_SCOPE",
        "demo_only": True,
        "allow_real_funds": False,
        "single_writer_required": True,
    }
    aggregate_id, created = _ensure_configuration_version(
        connection,
        type_key=type_key,
        version_number=1,
        payload=payload,
        created_by=actor,
        change_summary=f"Preserve {workflow_kind} historical configuration as UNKNOWN",
    )
    _validate_configuration_version(
        connection, version_id=aggregate_id, created=created, actor=actor
    )
    return _ensure_bundle(
        connection,
        workflow_kind=workflow_kind,
        scope_type="MIGRATION",
        scope_key=scope_key,
        aggregate_version_id=aggregate_id,
    )


def _seed_legacy_scoring_profile(
    connection: Connection,
    *,
    metric_versions: Mapping[str, int],
    actor: str,
) -> int:
    payload = {
        "algorithm_version": "phase2-quality-v1",
        "component_weights": {
            "profit_score": 0.35,
            "risk_score": 0.25,
            "stability_score": 0.15,
            "quality_score": 0.25,
        },
        "source": "legacy_strategy_scores",
        "values_preserved": True,
        "recalculation_forbidden": True,
    }
    version_id, created = _ensure_profile_version(
        connection,
        type_key="scoring-profile",
        version_number=2,
        payload=payload,
        table_name="scoring_profile_versions",
        specialized_values={
            "scoring_adapter_key": "weighted-component-score-v1",
            "algorithm_version": "phase2-quality-v1",
            "aggregation_method": "legacy_value_preserved",
            "primary_window_selector": {"source": "promotion_backtest_result"},
            "score_bounds": {"minimum": 0, "maximum": 100},
        },
        dependencies=[
            (metric_versions[key], f"metric:{key}")
            for key in (
                "profit_score",
                "risk_score",
                "stability_score",
                "quality_score",
            )
        ],
        actor=actor,
        change_summary="Bind preserved phase2-quality-v1 scores to their historical result",
        defer_validation=True,
    )
    if created:
        for priority, (metric, weight) in enumerate(
            (
                ("profit_score", 0.35),
                ("risk_score", 0.25),
                ("stability_score", 0.15),
                ("quality_score", 0.25),
            ),
            start=1,
        ):
            connection.execute(
                text(
                    "INSERT INTO scoring_rules "
                    "(profile_version_id,metric_definition_version_id,"
                    "normalization_adapter_key,normalization_parameters,weight,data_source,"
                    "aggregation_method,window_selector,priority) VALUES (:profile,:metric,"
                    "'linear-normalization-v1',CAST(:parameters AS json),:weight,"
                    "'strategy_scores','legacy_value_preserved',CAST(:selector AS json),"
                    ":priority)"
                ),
                {
                    "profile": version_id,
                    "metric": metric_versions[metric],
                    "parameters": _json_parameter(
                        {"normalization": "already_persisted", "recalculate": False}
                    ),
                    "weight": weight,
                    "selector": _json_parameter(
                        {"source": "promotion_backtest_result"}
                    ),
                    "priority": priority * 10,
                },
            )
    _validate_configuration_version(
        connection, version_id=version_id, created=created, actor=actor
    )
    return version_id


def _metric_float(metrics: Mapping[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if value is None and isinstance(metrics.get("normalized_metrics"), Mapping):
        value = metrics["normalized_metrics"].get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _failure_code_from_window(row: Mapping[str, Any]) -> str | None:
    if row["status"] == "PASSED":
        return None
    reason = str(row["blocked_reason"] or "").lower()
    if "checksum" in reason or "digest" in reason:
        return "LEGACY_MARKET_DATA_DIGEST_MISMATCH"
    if "fewer than" in reason or "trade" in reason:
        return "LEGACY_INSUFFICIENT_TRADES"
    if "not profitable" in reason or "profit" in reason:
        return "LEGACY_NON_POSITIVE_PROFIT"
    if "drawdown" in reason:
        return "LEGACY_DRAWDOWN_LIMIT"
    return "LEGACY_VALIDATION_BLOCKED"


def _backfill_validation_evidence(
    connection: Connection,
    *,
    migration_run_id: int,
    version_targets: Mapping[tuple[int, str, str], int],
    legacy_window_config_id: int,
    window_config_by_legacy_id: Mapping[int, int],
    legacy_quality_id: int,
    legacy_bundle_id: int,
    legacy_scoring_profile_id: int,
) -> tuple[int, int, int]:
    plan_rows = connection.execute(
        text(
            "SELECT p.id,p.strategy_version_id,p.strategy_target_id,"
            "p.quality_gate_profile_version_id,p.validation_window_config_set_id,"
            "p.configuration_bundle_snapshot_id,p.cycle_number,p.trigger_source_key,"
            "p.trigger_metadata,p.started_at,p.policy_snapshot_digest,"
            "p.market_data_snapshot_digest,p.plan_digest,p.created_at,p.status,"
            "promotion.metrics_snapshot AS promotion_metrics,score.id AS score_id,"
            "score.total_score,score.scoring_version,promotion_task.pair,"
            "promotion_task.timeframe "
            "FROM strategy_validation_plans p JOIN backtest_results promotion "
            "ON promotion.id=p.promotion_backtest_result_id JOIN backtest_tasks promotion_task "
            "ON promotion_task.id=promotion.backtest_task_id LEFT JOIN strategy_scores score "
            "ON score.backtest_result_id=p.promotion_backtest_result_id "
            "WHERE p.strategy_target_id IS NULL "
            "AND p.quality_gate_profile_version_id IS NULL "
            "AND p.validation_window_config_set_id IS NULL "
            "AND p.configuration_bundle_snapshot_id IS NULL "
            "AND p.trigger_source_key IS NULL ORDER BY p.created_at,p.id"
        )
    ).mappings().all()
    target_by_legacy_plan: dict[int, int] = {}
    plan_order_by_target: dict[int, list[tuple[datetime, int, int | None]]] = {}
    for existing_plan in connection.execute(
        text(
            "SELECT id,strategy_target_id,cycle_number,created_at "
            "FROM strategy_validation_plans WHERE strategy_target_id IS NOT NULL "
            "ORDER BY created_at,id"
        )
    ).mappings():
        target = int(existing_plan["strategy_target_id"])
        plan_order_by_target.setdefault(target, []).append(
            (
                _aware_utc(existing_plan["created_at"]),
                int(existing_plan["id"]),
                None
                if existing_plan["cycle_number"] is None
                else int(existing_plan["cycle_number"]),
            )
        )
    for plan in plan_rows:
        target = version_targets.get(
            (
                int(plan["strategy_version_id"]),
                str(plan["pair"]),
                str(plan["timeframe"]),
            )
        )
        if target is None:
            raise StrategyPlatformTask1Blocked(
                f"validation plan {plan['id']} has no strategy target"
            )
        target_by_legacy_plan[int(plan["id"])] = target
        plan_order_by_target.setdefault(target, []).append(
            (_aware_utc(plan["created_at"]), int(plan["id"]), None)
        )
    cycle_by_legacy_plan: dict[int, int] = {}
    for target, ordered_plans in plan_order_by_target.items():
        for expected_cycle, (_, plan_id, existing_cycle) in enumerate(
            sorted(ordered_plans), start=1
        ):
            if existing_cycle is not None and existing_cycle != expected_cycle:
                raise StrategyPlatformTask1Blocked(
                    "validation cycle chronology conflicts with an existing plan: "
                    f"target={target} plan={plan_id} existing={existing_cycle} "
                    f"expected={expected_cycle}"
                )
            if plan_id in target_by_legacy_plan:
                cycle_by_legacy_plan[plan_id] = expected_cycle
    mapped_plans = 0
    for plan in plan_rows:
        target_id = target_by_legacy_plan[int(plan["id"])]
        cycle = cycle_by_legacy_plan[int(plan["id"])]
        promotion_metrics = plan["promotion_metrics"] or {}
        manifest = (
            promotion_metrics.get("parser_metadata", {})
            .get("artifact_manifest", {})
        )
        checksums = manifest.get("checksums", {}) if isinstance(manifest, Mapping) else {}
        policy_digest = checksums.get("config")
        market_digest = checksums.get("market_data")
        if _SHA256_RE.fullmatch(str(policy_digest or "")) is None:
            policy_digest = None
        if _SHA256_RE.fullmatch(str(market_digest or "")) is None:
            market_digest = None
        expected = {
            "strategy_target_id": target_id,
            "quality_gate_profile_version_id": legacy_quality_id,
            "validation_window_config_set_id": legacy_window_config_id,
            "configuration_bundle_snapshot_id": legacy_bundle_id,
            "cycle_number": cycle,
            "trigger_source_key": "import",
            "trigger_metadata": {
                "migration": TASK1_MIGRATION_KEY,
                "legacy_status": plan["status"],
                "quality_status": "UNKNOWN",
                "qualification_allowed": False,
            },
            "started_at": plan["started_at"] or plan["created_at"],
            "policy_snapshot_digest": policy_digest,
            "market_data_snapshot_digest": market_digest,
        }
        conflicts = [
            key
            for key, value in expected.items()
            if plan[key] is not None
            and not (key == "trigger_metadata" and plan[key] == {})
            and plan[key] != value
        ]
        if conflicts:
            raise StrategyPlatformTask1Blocked(
                f"validation plan {plan['id']} migration conflict: {conflicts}"
            )
        connection.execute(
            text(
                "UPDATE strategy_validation_plans SET "
                "strategy_target_id=COALESCE(strategy_target_id,:target),"
                "quality_gate_profile_version_id=COALESCE(quality_gate_profile_version_id,"
                ":quality),validation_window_config_set_id=COALESCE("
                "validation_window_config_set_id,:windows),"
                "configuration_bundle_snapshot_id=COALESCE("
                "configuration_bundle_snapshot_id,:bundle),"
                "cycle_number=COALESCE(cycle_number,:cycle),"
                "trigger_source_key=COALESCE(trigger_source_key,'import'),"
                "trigger_metadata=CASE WHEN trigger_metadata::jsonb='{}'::jsonb THEN "
                "CAST(:trigger AS json) ELSE trigger_metadata END,"
                "started_at=COALESCE(started_at,:started),"
                "policy_snapshot_digest=COALESCE(policy_snapshot_digest,:policy),"
                "market_data_snapshot_digest=COALESCE(market_data_snapshot_digest,:market) "
                "WHERE id=:id"
            ),
            {
                "id": plan["id"],
                "target": target_id,
                "quality": legacy_quality_id,
                "windows": legacy_window_config_id,
                "bundle": legacy_bundle_id,
                "cycle": cycle,
                "trigger": _json_parameter(expected["trigger_metadata"]),
                "started": expected["started_at"],
                "policy": policy_digest,
                "market": market_digest,
            },
        )
        mapped_plans += 1

    window_rows = connection.execute(
        text(
            "SELECT w.id,w.validation_plan_id,w.window_config_id,w.window_key_snapshot,"
            "w.name_zh_snapshot,w.description_zh_snapshot,w.attempt_number,"
            "w.net_profit_after_cost,w.max_drawdown,w.volatility,w.total_trades,"
            "w.failure_code,w.failure_message,w.status,w.blocked_reason,w.backtest_result_id,"
            "r.metrics_snapshot,r.profit_total,r.profit_pct,r.max_drawdown_pct,"
            "r.total_trades AS result_total_trades "
            "FROM strategy_validation_windows w JOIN strategy_validation_plans p "
            "ON p.id=w.validation_plan_id LEFT JOIN backtest_results r "
            "ON r.id=w.backtest_result_id "
            "WHERE p.trigger_metadata::jsonb @> CAST(:migration_metadata AS jsonb) "
            "ORDER BY w.id"
        ),
        {
            "migration_metadata": _json_parameter(
                {"migration": TASK1_MIGRATION_KEY}
            )
        },
    ).mappings().all()
    mapped_windows = 0
    for row in window_rows:
        config_id = window_config_by_legacy_id.get(int(row["id"]))
        if config_id is None:
            raise StrategyPlatformTask1Blocked(
                f"validation window {row['id']} has no dynamic config mapping"
            )
        config = connection.execute(
            text(
                "SELECT window_key,name_zh,description_zh FROM validation_window_configs "
                "WHERE id=:id"
            ),
            {"id": config_id},
        ).mappings().one()
        metrics = row["metrics_snapshot"] or {}
        expected = {
            "window_config_id": config_id,
            "window_key_snapshot": config["window_key"],
            "name_zh_snapshot": config["name_zh"],
            "description_zh_snapshot": config["description_zh"],
            "attempt_number": int(row["attempt_number"] or 1),
            "net_profit_after_cost": (
                float(row["profit_pct"])
                if row["profit_pct"] is not None
                else _metric_float(metrics, "profit_pct")
            ),
            "max_drawdown": (
                float(row["max_drawdown_pct"])
                if row["max_drawdown_pct"] is not None
                else _metric_float(metrics, "max_drawdown_account")
            ),
            "volatility": _metric_float(metrics, "volatility"),
            "total_trades": (
                int(row["result_total_trades"])
                if row["result_total_trades"] is not None
                else None
            ),
            "failure_code": _failure_code_from_window(row),
            "failure_message": None if row["status"] == "PASSED" else row["blocked_reason"],
        }
        conflicts = [
            key
            for key, value in expected.items()
            if row[key] is not None and row[key] != value
        ]
        if conflicts:
            raise StrategyPlatformTask1Blocked(
                f"validation window {row['id']} migration conflict: {conflicts}"
            )
        connection.execute(
            text(
                "UPDATE strategy_validation_windows SET "
                "window_config_id=COALESCE(window_config_id,:config),"
                "window_key_snapshot=COALESCE(window_key_snapshot,:key),"
                "name_zh_snapshot=COALESCE(name_zh_snapshot,:name),"
                "description_zh_snapshot=COALESCE(description_zh_snapshot,:description),"
                "attempt_number=COALESCE(attempt_number,1),"
                "net_profit_after_cost=COALESCE(net_profit_after_cost,:profit),"
                "max_drawdown=COALESCE(max_drawdown,:drawdown),"
                "volatility=COALESCE(volatility,:volatility),"
                "total_trades=COALESCE(total_trades,:trades),"
                "failure_code=COALESCE(failure_code,:failure_code),"
                "failure_message=COALESCE(failure_message,:failure_message) WHERE id=:id"
            ),
            {
                "id": row["id"],
                "config": config_id,
                "key": expected["window_key_snapshot"],
                "name": expected["name_zh_snapshot"],
                "description": expected["description_zh_snapshot"],
                "profit": expected["net_profit_after_cost"],
                "drawdown": expected["max_drawdown"],
                "volatility": expected["volatility"],
                "trades": expected["total_trades"],
                "failure_code": expected["failure_code"],
                "failure_message": expected["failure_message"],
            },
        )
        mapped_windows += 1

    scored = 0
    for plan in plan_rows:
        if plan["score_id"] is None:
            continue
        score = connection.execute(
            text(
                "SELECT s.*,r.metrics_snapshot AS result_metrics FROM strategy_scores s "
                "JOIN backtest_results r ON r.id=s.backtest_result_id WHERE s.id=:id"
            ),
            {"id": plan["score_id"]},
        ).mappings().one()
        score_result_id = int(score["backtest_result_id"])
        exact_window_id = connection.execute(
            text(
                "SELECT id FROM strategy_validation_windows "
                "WHERE validation_plan_id=:plan AND backtest_result_id=:result"
            ),
            {"plan": plan["id"], "result": score_result_id},
        ).scalar_one_or_none()
        if exact_window_id is None:
            connection.execute(
                text(
                    "INSERT INTO strategy_platform_migration_entity_mappings "
                    "(migration_run_id,source_table,source_primary_key,mapping_kind,"
                    "mapping_status,mapping_reason,quality_status_asserted,"
                    "evidence_snapshot) VALUES (:run,'strategy_scores',:source,"
                    "'WINDOW_SCORE_ASSOCIATION','NOT_APPLICABLE',"
                    "'Canonical promotion score has no exact validation-window result; "
                    "the original strategy_scores row remains canonical',"
                    "'UNKNOWN',CAST(:evidence AS json)) ON CONFLICT DO NOTHING"
                ),
                {
                    "run": migration_run_id,
                    "source": str(score["id"]),
                    "evidence": _json_parameter(
                        {
                            "source_backtest_result_id": score_result_id,
                            "no_score_fabrication": True,
                            "qualification_allowed": False,
                        }
                    ),
                },
            )
            continue
        score_payload = {
            "source_strategy_score_id": int(score["id"]),
            "source_backtest_result_id": int(score["backtest_result_id"]),
            "scoring_version": score["scoring_version"],
            "total_score": float(score["total_score"]),
            "components": {
                "profit_score": score["profit_score"],
                "risk_score": score["risk_score"],
                "stability_score": score["stability_score"],
                "quality_score": score["quality_score"],
            },
            "metrics_snapshot": score["metrics_snapshot"],
            "values_preserved": True,
            "quality_status": "UNKNOWN",
        }
        score_digest = canonical_digest(score_payload)
        window_score_id = connection.execute(
            text(
                "INSERT INTO validation_window_scores "
                "(validation_window_id,scoring_version,profile_version_id,total_score,"
                "component_scores_snapshot,metrics_snapshot,score_digest,created_at) "
                "SELECT :window,:scoring,:profile,:total,CAST(:components AS json),"
                "CAST(:metrics AS json),:digest,s.created_at FROM strategy_scores s "
                "WHERE s.id=:score "
                "ON CONFLICT (validation_window_id) DO UPDATE SET "
                "validation_window_id=EXCLUDED.validation_window_id RETURNING id"
            ),
            {
                "scoring": score["scoring_version"],
                "profile": legacy_scoring_profile_id,
                "total": score["total_score"],
                "components": _json_parameter(score_payload),
                "metrics": _json_parameter(score["result_metrics"] or {}),
                "digest": score_digest,
                "window": exact_window_id,
                "score": score["id"],
            },
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO strategy_platform_migration_entity_mappings "
                "(migration_run_id,source_table,source_primary_key,mapping_kind,"
                "target_table,target_primary_key,mapping_status,mapping_reason,"
                "quality_status_asserted,evidence_snapshot) VALUES "
                "(:run,'strategy_scores',:source,'WINDOW_SCORE_ASSOCIATION',"
                "'validation_window_scores',:target,'MAPPED',"
                "'Canonical score has one exact validation-window result; values are "
                "preserved without qualification','UNKNOWN',CAST(:evidence AS json)) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "run": migration_run_id,
                "source": str(score["id"]),
                "target": str(window_score_id),
                "evidence": _json_parameter(
                    {
                        "source_backtest_result_id": score_result_id,
                        "validation_window_id": int(exact_window_id),
                        "values_preserved": True,
                        "qualification_allowed": False,
                    }
                ),
            },
        )
        scored += 1
    unassociated_scores = connection.execute(
        text(
            "SELECT score.id,score.backtest_result_id,EXISTS (SELECT 1 FROM "
            "strategy_validation_plans plan WHERE "
            "plan.promotion_backtest_result_id=score.backtest_result_id) AS has_plan "
            "FROM strategy_scores score WHERE NOT EXISTS (SELECT 1 FROM "
            "strategy_platform_migration_entity_mappings mapping WHERE "
            "mapping.migration_run_id=:run AND mapping.source_table='strategy_scores' "
            "AND mapping.source_primary_key=score.id::text "
            "AND mapping.mapping_kind='WINDOW_SCORE_ASSOCIATION') ORDER BY score.id"
        ),
        {"run": migration_run_id},
    ).mappings()
    for score in unassociated_scores:
        reason_code = (
            "NO_EXACT_VALIDATION_WINDOW" if score["has_plan"] else "NO_VALIDATION_PLAN"
        )
        connection.execute(
            text(
                "INSERT INTO strategy_platform_migration_entity_mappings "
                "(migration_run_id,source_table,source_primary_key,mapping_kind,"
                "mapping_status,mapping_reason,quality_status_asserted,"
                "evidence_snapshot) VALUES (:run,'strategy_scores',:source,"
                "'WINDOW_SCORE_ASSOCIATION','NOT_APPLICABLE',:reason,'UNKNOWN',"
                "CAST(:evidence AS json)) ON CONFLICT DO NOTHING"
            ),
            {
                "run": migration_run_id,
                "source": str(score["id"]),
                "reason": (
                    "Canonical strategy score has no validation plan; the original "
                    "strategy_scores row remains canonical"
                    if not score["has_plan"]
                    else "Canonical strategy score has no exact validation-window "
                    "result; the original strategy_scores row remains canonical"
                ),
                "evidence": _json_parameter(
                    {
                        "reason_code": reason_code,
                        "source_backtest_result_id": int(score["backtest_result_id"]),
                        "no_score_fabrication": True,
                        "qualification_allowed": False,
                    }
                ),
            },
        )
    return mapped_plans, mapped_windows, scored


def _insert_blocked_legacy_summaries(connection: Connection) -> int:
    plans = connection.execute(
        text(
            "SELECT p.id,score.total_score,"
            "count(w.id) FILTER (WHERE config.required) AS required_count,"
            "count(w.id) FILTER (WHERE config.required AND w.status='PASSED') "
            "AS passed_count,"
            "count(w.id) FILTER (WHERE config.required AND w.status<>'PASSED') "
            "AS failed_count,"
            "min(w.window_config_id) FILTER (WHERE config.required "
            "AND w.status<>'PASSED') "
            "AS failure_window_id,array_remove(array_agg(DISTINCT COALESCE("
            "w.failure_code,'LEGACY_VALIDATION_BLOCKED')) FILTER "
            "(WHERE config.required AND w.status<>'PASSED'),NULL) AS reason_codes "
            "FROM strategy_validation_plans p JOIN strategy_validation_windows w "
            "ON w.validation_plan_id=p.id JOIN validation_window_configs config "
            "ON config.id=w.window_config_id LEFT JOIN strategy_scores score "
            "ON score.backtest_result_id=p.promotion_backtest_result_id "
            "WHERE p.trigger_metadata::jsonb @> CAST(:migration_metadata AS jsonb) "
            "GROUP BY p.id,score.total_score ORDER BY p.id"
        ),
        {
            "migration_metadata": _json_parameter(
                {"migration": TASK1_MIGRATION_KEY}
            )
        },
    ).mappings().all()
    for row in plans:
        summary = {
            "validation_plan_id": int(row["id"]),
            "required_window_count": int(row["required_count"]),
            "passed_window_count": int(row["passed_count"]),
            "failed_window_count": int(row["failed_count"]),
            "overall_score": (
                None if row["total_score"] is None else float(row["total_score"])
            ),
            "status": "BLOCKED",
            "primary_failure_window_config_id": row["failure_window_id"],
            "reason_codes": sorted(
                set(row["reason_codes"] or [])
                | {"LEGACY_DYNAMIC_QUALITY_NOT_EVALUATED"}
            ),
            "quality_status_source": "legacy-evidence-import-v1",
            "qualification_allowed": False,
        }
        digest = canonical_digest(summary)
        connection.execute(
            text(
                "INSERT INTO strategy_evaluation_summaries "
                "(validation_plan_id,required_window_count,passed_window_count,"
                "failed_window_count,overall_score,status,"
                "primary_failure_window_config_id,reason_codes,summary_digest) "
                "VALUES (:plan,:required,:passed,:failed,:score,'BLOCKED',:window,"
                "CAST(:reasons AS json),:digest) ON CONFLICT (validation_plan_id) DO NOTHING"
            ),
            {
                "plan": row["id"],
                "required": row["required_count"],
                "passed": row["passed_count"],
                "failed": row["failed_count"],
                "score": row["total_score"],
                "window": row["failure_window_id"],
                "reasons": _json_parameter(summary["reason_codes"]),
                "digest": digest,
            },
        )
        persisted = connection.execute(
            text(
                "SELECT required_window_count,passed_window_count,failed_window_count,"
                "overall_score,status,primary_failure_window_config_id,reason_codes,"
                "summary_digest FROM strategy_evaluation_summaries "
                "WHERE validation_plan_id=:plan"
            ),
            {"plan": row["id"]},
        ).mappings().one()
        if (
            int(persisted["required_window_count"]) != summary["required_window_count"]
            or int(persisted["passed_window_count"]) != summary["passed_window_count"]
            or int(persisted["failed_window_count"]) != summary["failed_window_count"]
            or persisted["status"] != "BLOCKED"
            or persisted["summary_digest"] != digest
        ):
            raise StrategyPlatformTask1Blocked(
                f"legacy evaluation summary conflict for plan {row['id']}"
            )
    return len(plans)


def _record_legacy_workflow_configuration_unknown(
    connection: Connection,
    *,
    migration_run_id: int,
) -> int:
    """Represent missing historical workflow configuration without fabricating it.

    Legacy research and deployment rows did not persist an immutable bundle.  A
    migration-created generic bundle is not evidence of the configuration that
    produced those rows, and some canary source rows are deliberately immutable.
    Keep the additive FK NULL and record that UNKNOWN explicitly.  The v13
    INSERT trigger still requires every newly-created workflow row to bind an
    immutable bundle.
    """

    recorded = 0
    for table_name in ("research_jobs", "strategy_deployments"):
        quoted = connection.dialect.identifier_preparer.quote(table_name)
        rows = connection.execute(
            text(
                f"SELECT id,configuration_bundle_snapshot_id FROM {quoted} "
                "ORDER BY id"
            )
        ).mappings()
        for row in rows:
            bundle_id = row["configuration_bundle_snapshot_id"]
            mapping_kind = (
                "LEGACY_CONFIGURATION_UNKNOWN"
                if bundle_id is None
                else "LEGACY_CONFIGURATION_PRESERVED"
            )
            connection.execute(
                text(
                    "INSERT INTO strategy_platform_migration_entity_mappings "
                    "(migration_run_id,source_table,source_primary_key,mapping_kind,"
                    "target_table,target_primary_key,mapping_status,mapping_reason,"
                    "quality_status_asserted,evidence_snapshot) VALUES "
                    "(:run,:table,:source,:kind,:target_table,:target_id,:status,"
                    ":reason,'UNKNOWN',CAST(:evidence AS json)) ON CONFLICT DO NOTHING"
                ),
                {
                    "run": migration_run_id,
                    "table": table_name,
                    "source": str(row["id"]),
                    "kind": mapping_kind,
                    "target_table": (
                        None if bundle_id is None else "configuration_bundle_snapshots"
                    ),
                    "target_id": None if bundle_id is None else str(bundle_id),
                    "status": "NOT_APPLICABLE" if bundle_id is None else "MAPPED",
                    "reason": (
                        "Historical workflow did not persist configuration evidence; "
                        "the nullable V1.3 bundle FK remains UNKNOWN"
                        if bundle_id is None
                        else "Pre-existing bundle reference preserved without rewrite"
                    ),
                    "evidence": _json_parameter(
                        {
                            "historical_configuration_evidence": (
                                "UNKNOWN" if bundle_id is None else "PRESERVED"
                            ),
                            "source_row_preserved": True,
                            "synthetic_bundle_forbidden": True,
                            "new_v13_rows_require_bundle": True,
                        }
                    ),
                },
            )
            recorded += 1
    return recorded


def _record_legacy_trade_intent_lineage(
    connection: Connection, *, migration_run_id: int
) -> int:
    """Audit proven historical links without mutating the new nullable columns."""

    rows = connection.execute(
        text(
            "SELECT intent.id,intent.deployment_id,intent.signal_evaluation_id,"
            "intent.runtime_instance_row_id,chain.signal_evaluation_id AS proven_signal_id,"
            "signal.deployment_id AS proven_deployment_id "
            "FROM trade_intents intent LEFT JOIN full_chain_runs chain "
            "ON chain.trade_intent_id=intent.id LEFT JOIN signal_evaluations signal "
            "ON signal.id=chain.signal_evaluation_id ORDER BY intent.id"
        )
    ).mappings().all()
    updated = 0
    for row in rows:
        proven_signal = row["proven_signal_id"]
        proven_deployment = row["proven_deployment_id"]
        if row["signal_evaluation_id"] is not None and row[
            "signal_evaluation_id"
        ] != proven_signal:
            raise StrategyPlatformTask1Blocked(
                f"trade intent {row['id']} signal lineage conflict"
            )
        if row["deployment_id"] is not None and row["deployment_id"] != proven_deployment:
            raise StrategyPlatformTask1Blocked(
                f"trade intent {row['id']} deployment lineage conflict"
            )
        proven = proven_signal is not None and proven_deployment is not None
        order_count = int(
            connection.execute(
                text("SELECT count(*) FROM exchange_orders WHERE trade_intent_id=:id"),
                {"id": row["id"]},
            ).scalar_one()
        )
        connection.execute(
            text(
                "INSERT INTO strategy_platform_migration_entity_mappings "
                "(migration_run_id,source_table,source_primary_key,mapping_kind,"
                "target_table,target_primary_key,mapping_status,mapping_reason,"
                "quality_status_asserted,evidence_snapshot) VALUES (:run,"
                "'trade_intents',:source,'LEGACY_SIGNAL_DEPLOYMENT_LINEAGE',"
                ":target_table,:target,:status,:reason,'UNKNOWN',"
                "CAST(:evidence AS json)) ON CONFLICT DO NOTHING"
            ),
            {
                "run": migration_run_id,
                "source": str(row["id"]),
                "target_table": "signal_evaluations" if proven else None,
                "target": str(proven_signal) if proven else None,
                "status": "PRESERVED" if proven else "NOT_APPLICABLE",
                "reason": (
                    "Historical columns remain NULL per V1.3; proven full-chain "
                    "lineage is recorded as audit evidence"
                    if proven
                    else "Historical signal/deployment/runtime lineage is absent; "
                    "NULL is preserved and explicitly classified UNKNOWN"
                ),
                "evidence": _json_parameter(
                    {
                        "signal_evaluation_id": (
                            int(proven_signal) if proven_signal is not None else None
                        ),
                        "deployment_id": (
                            int(proven_deployment)
                            if proven_deployment is not None
                            else None
                        ),
                        "runtime_instance_row_id": None,
                        "linked_exchange_order_count": order_count,
                        "legacy_columns_preserved_null": True,
                        "lineage_evidence_status": "PROVEN" if proven else "UNKNOWN",
                    }
                ),
            },
        )
        updated += 1
        # No historical runtime-instance table existed.  NULL is the accurate
        # UNKNOWN value; a migration must not synthesize a process identity.
    return updated


def _legacy_mapping_payload_expression(table_name: str) -> tuple[str, dict[str, Any]]:
    expression = "to_jsonb(source_row)"
    excluded = _LEGACY_SNAPSHOT_EXCLUDED_COLUMNS.get(table_name, ())
    parameters: dict[str, Any] = {}
    if excluded:
        expression += " - CAST(:excluded_columns AS text[])"
        parameters["excluded_columns"] = list(excluded)
    return expression, parameters


def _record_legacy_source_rows(
    connection: Connection,
    *,
    migration_run_id: int,
) -> int:
    """Persist per-row source digests before any legacy row is augmented."""

    for table_name in LEGACY_ENTITY_TABLES:
        quoted = connection.dialect.identifier_preparer.quote(table_name)
        payload_expression, parameters = _legacy_mapping_payload_expression(table_name)
        connection.execute(
            text(
                "INSERT INTO strategy_platform_migration_entity_mappings "
                "(migration_run_id,source_table,source_primary_key,mapping_kind,"
                "mapping_status,mapping_reason,source_digest,quality_status_asserted,"
                "evidence_snapshot) SELECT :run,:table,source_row.id::text,"
                "'LEGACY_ROW_SOURCE_SNAPSHOT','PRESERVED',"
                "'Pre-migration legacy projection captured before additive backfill',"
                "encode(public.digest(convert_to(("
                + payload_expression
                + ")::text,'UTF8'),'sha256'),'hex'),NULL,"
                "json_build_object('migration',CAST(:migration AS text),'phase','BEFORE',"
                "'batch_is_metadata_only',true) "
                f"FROM {quoted} source_row ON CONFLICT DO NOTHING"
            ),
            {
                **parameters,
                "run": migration_run_id,
                "table": table_name,
                "migration": TASK1_MIGRATION_KEY,
            },
        )
    return int(
        connection.execute(
            text(
                "SELECT count(*) FROM strategy_platform_migration_entity_mappings "
                "WHERE migration_run_id=:run "
                "AND mapping_kind='LEGACY_ROW_SOURCE_SNAPSHOT'"
            ),
            {"run": migration_run_id},
        ).scalar_one()
    )


def _record_legacy_entity_mappings(
    connection: Connection,
    *,
    migration_run_id: int,
    version_targets: Mapping[tuple[int, str, str], int],
    deployment_targets: Mapping[int, int],
    window_config_by_legacy_id: Mapping[int, int],
) -> int:
    for table_name in LEGACY_ENTITY_TABLES:
        quoted = connection.dialect.identifier_preparer.quote(table_name)
        payload_expression, parameters = _legacy_mapping_payload_expression(table_name)
        quality_expression = (
            "CASE WHEN source_row.status='BLOCKED' THEN 'BLOCKED' "
            "WHEN source_row.status='REJECTED' THEN 'REJECTED' "
            "WHEN source_row.status='FAILED' THEN 'FAILED' ELSE 'UNKNOWN' END"
            if table_name in {
                "strategy_validation_plans",
                "strategy_validation_windows",
            }
            else "NULL"
        )
        mismatches = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM "
                    + quoted
                    + " source_row JOIN strategy_platform_migration_entity_mappings "
                    "source_map ON source_map.migration_run_id=:run "
                    "AND source_map.source_table=:table "
                    "AND source_map.source_primary_key=source_row.id::text "
                    "AND source_map.mapping_kind='LEGACY_ROW_SOURCE_SNAPSHOT' "
                    "WHERE source_map.source_digest<>encode(public.digest(convert_to(("
                    + payload_expression
                    + ")::text,'UTF8'),'sha256'),'hex')"
                ),
                {**parameters, "run": migration_run_id, "table": table_name},
            ).scalar_one()
        )
        if mismatches:
            raise StrategyPlatformTask1Blocked(
                f"legacy row content changed before mapping: {table_name}={mismatches}"
            )
        connection.execute(
            text(
                "INSERT INTO strategy_platform_migration_entity_mappings "
                "(migration_run_id,source_table,source_primary_key,mapping_kind,"
                "target_table,target_primary_key,mapping_status,mapping_reason,"
                "source_digest,target_digest,quality_status_asserted,evidence_snapshot) "
                f"SELECT :run,:table,source_row.id::text,'LEGACY_ROW_PRESERVED',:table,"
                "source_row.id::text,'PRESERVED','Original row and primary key preserved',"
                "source_map.source_digest,encode(public.digest(convert_to(("
                + payload_expression
                + ")::text,'UTF8'),'sha256'),'hex'),"
                f"{quality_expression},"
                "json_build_object('migration',CAST(:migration AS text),"
                "'batch_is_metadata_only',true) "
                f"FROM {quoted} source_row JOIN strategy_platform_migration_entity_mappings "
                "source_map ON source_map.migration_run_id=:run "
                "AND source_map.source_table=:table "
                "AND source_map.source_primary_key=source_row.id::text "
                "AND source_map.mapping_kind='LEGACY_ROW_SOURCE_SNAPSHOT' "
                "ON CONFLICT DO NOTHING"
            ),
            {
                **parameters,
                "run": migration_run_id,
                "table": table_name,
                "migration": TASK1_MIGRATION_KEY,
            },
        )
    for (version_id, pair, timeframe), target_id in version_targets.items():
        connection.execute(
            text(
                "INSERT INTO strategy_platform_migration_entity_mappings "
                "(migration_run_id,source_table,source_primary_key,mapping_kind,"
                "target_table,target_primary_key,mapping_status,mapping_reason,"
                "quality_status_asserted,evidence_snapshot) VALUES (:run,"
                "'strategy_versions',:source,'EVIDENCE_BACKED_RESEARCH_TARGET',"
                "'strategy_targets',:target,'MAPPED',"
                "'Unique backtest pair/timeframe evidence maps this version to one target',"
                "'UNKNOWN',CAST(:evidence AS json)) ON CONFLICT DO NOTHING"
            ),
            {
                "run": migration_run_id,
                "source": str(version_id),
                "target": str(target_id),
                "evidence": _json_parameter(
                    {
                        "qualification_allowed": False,
                        "mapping_source": "backtest_runs+backtest_tasks",
                        "pair": pair,
                        "timeframe": timeframe,
                    }
                ),
            },
        )
    for deployment_id, target_id in deployment_targets.items():
        evidence = connection.execute(
            text(
                "SELECT deployment.strategy_version_id,deployment.instrument_id,"
                "deployment.timeframe,deployment.real_orders,target.strategy_version_id "
                "AS target_strategy_version_id,target.instrument_id AS "
                "target_instrument_id,target.timeframe AS target_timeframe,"
                "definition.target_key FROM strategy_deployments deployment "
                "JOIN strategy_targets target ON target.id=:target "
                "JOIN execution_target_definitions definition "
                "ON definition.id=target.execution_target_id WHERE deployment.id=:deployment"
            ),
            {"target": target_id, "deployment": deployment_id},
        ).mappings().one()
        if (
            evidence["target_key"] != "OKX_DEMO"
            or evidence["real_orders"] is not False
            or evidence["strategy_version_id"]
            != evidence["target_strategy_version_id"]
            or evidence["instrument_id"] != evidence["target_instrument_id"]
            or evidence["timeframe"] != evidence["target_timeframe"]
        ):
            raise StrategyPlatformTask1Blocked(
                f"deployment {deployment_id} target evidence mismatch"
            )
        connection.execute(
            text(
                "INSERT INTO strategy_platform_migration_entity_mappings "
                "(migration_run_id,source_table,source_primary_key,mapping_kind,"
                "target_table,target_primary_key,mapping_status,mapping_reason,"
                "quality_status_asserted,evidence_snapshot) VALUES (:run,"
                "'strategy_deployments',:source,'LEGACY_DEMO_DEPLOYMENT_TARGET',"
                "'strategy_targets',:target,'MAPPED',"
                "'Demo-only deployment identity maps to one OKX_DEMO strategy target',"
                "'UNKNOWN',CAST(:evidence AS json)) ON CONFLICT DO NOTHING"
            ),
            {
                "run": migration_run_id,
                "source": str(deployment_id),
                "target": str(target_id),
                "evidence": _json_parameter(
                    {
                        "target_key": "OKX_DEMO",
                        "strategy_version_id": int(evidence["strategy_version_id"]),
                        "instrument_id": evidence["instrument_id"],
                        "timeframe": evidence["timeframe"],
                        "real_orders": False,
                        "qualification_allowed": False,
                    }
                ),
            },
        )
    for window_id, config_id in window_config_by_legacy_id.items():
        connection.execute(
            text(
                "INSERT INTO strategy_platform_migration_entity_mappings "
                "(migration_run_id,source_table,source_primary_key,mapping_kind,"
                "target_table,target_primary_key,mapping_status,mapping_reason,"
                "quality_status_asserted,evidence_snapshot) VALUES (:run,"
                "'strategy_validation_windows',:source,'DYNAMIC_WINDOW_CONFIG',"
                "'validation_window_configs',:target,'MAPPED',"
                "'Historical timerange and target mapped without changing execution status',"
                "'UNKNOWN',CAST(:evidence AS json)) ON CONFLICT DO NOTHING"
            ),
            {
                "run": migration_run_id,
                "source": str(window_id),
                "target": str(config_id),
                "evidence": _json_parameter(
                    {
                        "legacy_passed_is_execution_evidence_only": True,
                        "qualification_allowed": False,
                    }
                ),
            },
        )
    return int(
        connection.execute(
            text(
                "SELECT count(*) FROM strategy_platform_migration_entity_mappings "
                "WHERE migration_run_id=:run"
            ),
            {"run": migration_run_id},
        ).scalar_one()
    )


def _acl_digest(connection: Connection) -> str:
    rows = connection.execute(
        text(
            "SELECT 'SCHEMA' AS object_kind,n.nspname AS object_name,"
            "owner.rolname AS owner,COALESCE(n.nspacl::text,'') AS acl "
            "FROM pg_namespace n JOIN pg_roles owner ON owner.oid=n.nspowner "
            "WHERE n.nspname=current_schema() UNION ALL "
            "SELECT 'RELATION',c.relname,owner.rolname,COALESCE(c.relacl::text,'') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_roles owner ON owner.oid=c.relowner WHERE n.nspname=current_schema() "
            "AND c.relkind IN ('r','p','v','m','S') UNION ALL "
            "SELECT 'COLUMN',c.relname||'.'||a.attname,owner.rolname,"
            "COALESCE(a.attacl::text,'') FROM pg_attribute a "
            "JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_roles owner ON owner.oid=c.relowner "
            "WHERE n.nspname=current_schema() AND a.attnum>0 AND NOT a.attisdropped "
            "UNION ALL SELECT 'FUNCTION',p.oid::regprocedure::text,owner.rolname,"
            "COALESCE(p.proacl::text,'') FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace "
            "JOIN pg_roles owner ON owner.oid=p.proowner "
            "WHERE n.nspname=current_schema() UNION ALL "
            "SELECT 'DEFAULT_ACL',role_row.rolname||':'||"
            "defacl.defaclobjtype::text||':'||"
            "COALESCE(namespace_row.nspname,''),role_row.rolname,"
            "COALESCE(defacl.defaclacl::text,'') FROM pg_default_acl defacl "
            "JOIN pg_roles role_row ON role_row.oid=defacl.defaclrole "
            "LEFT JOIN pg_namespace namespace_row ON namespace_row.oid=defacl.defaclnamespace "
            "WHERE defacl.defaclnamespace=0 OR namespace_row.nspname=current_schema() "
            "UNION ALL SELECT 'ROLE_MEMBERSHIP',member.rolname||'->'||granted.rolname,"
            "grantor.rolname,CASE WHEN membership.admin_option THEN 'ADMIN' ELSE 'MEMBER' END "
            "FROM pg_auth_members membership JOIN pg_roles member "
            "ON member.oid=membership.member JOIN pg_roles granted "
            "ON granted.oid=membership.roleid JOIN pg_roles grantor "
            "ON grantor.oid=membership.grantor "
            "ORDER BY object_kind,object_name,owner,acl"
        )
    ).mappings().all()
    return canonical_digest([dict(row) for row in rows])


def assert_task1_migration_fence(
    connection: Connection,
    *,
    execution_scope: str,
    runtime_schema_compatible: bool = False,
    writers_quiesced: bool = False,
) -> None:
    if execution_scope not in {"DESIGN_LAB", "SHARED_DATABASE"}:
        raise StrategyPlatformTask1Blocked("invalid migration execution scope")
    if execution_scope == "SHARED_DATABASE":
        raise StrategyPlatformTask1Blocked(
            "BLOCKED_LEGACY_SOURCE_READ_ONLY: freqtrade_ai is permanently a "
            "read-only historical source; V1.3 DDL/DML is allowed only on the "
            "physically isolated owner database"
        )
    acquired = connection.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": TASK1_ADVISORY_LOCK_KEY},
    ).scalar_one()
    if acquired is not True:
        raise StrategyPlatformTask1Blocked("Task 1 migration advisory lock is held")
    conflicting_sessions = int(
        connection.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity activity "
                "WHERE activity.datname=current_database() AND activity.pid<>pg_backend_pid() "
                "AND activity.backend_type='client backend' "
                "AND (activity.state<>'idle' OR EXISTS (SELECT 1 FROM pg_locks held "
                "WHERE held.pid=activity.pid AND held.granted AND held.mode IN ("
                "'RowExclusiveLock','ShareRowExclusiveLock','ExclusiveLock',"
                "'AccessExclusiveLock')))"
            )
        ).scalar_one()
    )
    prepared = int(
        connection.execute(
            text("SELECT count(*) FROM pg_prepared_xacts WHERE database=current_database()")
        ).scalar_one()
    )
    if conflicting_sessions or prepared:
        raise StrategyPlatformTask1Blocked(
            "migration ownership fence has active or write-locking sessions: "
            f"sessions={conflicting_sessions} "
            f"prepared_transactions={prepared}"
        )


def _configuration_bundle_digest_mismatch_count(connection: Connection) -> int:
    mismatches = 0
    bundles = connection.execute(
        text(
            "SELECT workflow_kind,scope_type,scope_key,aggregate_profile_version_id,"
            "resolved_versions_json,resolved_digests_json,capability_snapshot,"
            "bundle_digest FROM configuration_bundle_snapshots ORDER BY id"
        )
    ).mappings()
    for bundle in bundles:
        versions = dict(bundle["resolved_versions_json"] or {})
        digests = dict(bundle["resolved_digests_json"] or {})
        if set(versions) != set(digests):
            mismatches += 1
            continue
        referenced = connection.execute(
            text(
                "SELECT id,type_key,lifecycle_status,config_digest FROM "
                "configuration_versions WHERE id=ANY(:ids)"
            ),
            {"ids": [int(value) for value in versions.values()]},
        ).mappings()
        referenced_by_id = {int(row["id"]): row for row in referenced}
        invalid_reference = False
        for map_key, version_id_value in versions.items():
            version_id = int(version_id_value)
            row = referenced_by_id.get(version_id)
            if (
                row is None
                or row["lifecycle_status"] != "VALIDATED"
                or map_key not in {row["type_key"], f"{row['type_key']}:{version_id}"}
                or digests.get(map_key) != row["config_digest"]
            ):
                invalid_reference = True
                break
        if invalid_reference:
            mismatches += 1
            continue
        recomputed = configuration_bundle_digest(
            workflow_kind=bundle["workflow_kind"],
            scope_type=bundle["scope_type"],
            scope_key=bundle["scope_key"],
            aggregate_profile_version_id=int(
                bundle["aggregate_profile_version_id"]
            ),
            resolved_versions_json=versions,
            resolved_digests_json=digests,
            capability_snapshot=dict(bundle["capability_snapshot"] or {}),
        )
        if recomputed != bundle["bundle_digest"]:
            mismatches += 1
    return mismatches


def reconcile_strategy_platform_v13_task1(
    connection: Connection,
    *,
    migration_run_id: int,
    source_snapshot: Sequence[Mapping[str, Any]],
    source_snapshot_digest: str,
    market_inventory: Sequence[MarketFileEvidence],
) -> dict[str, Any]:
    current_legacy = collect_legacy_snapshot(connection)
    current_legacy_digest = _snapshot_digest(current_legacy)
    if current_legacy_digest != source_snapshot_digest:
        raise StrategyPlatformTask1Blocked(
            "legacy source rows changed during Task 1 migration"
        )
    expected_by_table = {row["table_name"]: row for row in source_snapshot}
    current_by_table = {row["table_name"]: row for row in current_legacy}
    mismatches = [
        table_name
        for table_name in LEGACY_ENTITY_TABLES
        if expected_by_table.get(table_name) != current_by_table.get(table_name)
    ]
    if mismatches:
        raise StrategyPlatformTask1Blocked(
            "legacy source table reconciliation failed: " + ", ".join(mismatches)
        )
    counts = {
        "strategy_versions": int(
            connection.execute(text("SELECT count(*) FROM strategy_versions")).scalar_one()
        ),
        "strategy_targets": int(
            connection.execute(text("SELECT count(*) FROM strategy_targets")).scalar_one()
        ),
        "plans": int(
            connection.execute(
                text("SELECT count(*) FROM strategy_validation_plans")
            ).scalar_one()
        ),
        "windows": int(
            connection.execute(
                text("SELECT count(*) FROM strategy_validation_windows")
            ).scalar_one()
        ),
        "scores": int(
            connection.execute(text("SELECT count(*) FROM strategy_scores")).scalar_one()
        ),
        "window_scores": int(
            connection.execute(
                text("SELECT count(*) FROM validation_window_scores")
            ).scalar_one()
        ),
        "summaries": int(
            connection.execute(
                text("SELECT count(*) FROM strategy_evaluation_summaries")
            ).scalar_one()
        ),
        "market_files": int(
            connection.execute(
                text("SELECT count(*) FROM market_data_file_records")
            ).scalar_one()
        ),
    }
    checks = {
        "version_without_target": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_versions version WHERE NOT EXISTS ("
                    "SELECT 1 FROM strategy_targets target "
                    "WHERE target.strategy_version_id=version.id)"
                )
            ).scalar_one()
        ),
        "result_lineage_mismatch": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM backtest_results result "
                    "JOIN backtest_tasks task ON task.id=result.backtest_task_id "
                    "JOIN backtest_runs run ON run.id=result.backtest_run_id "
                    "WHERE task.backtest_run_id<>run.id"
                )
            ).scalar_one()
        ),
        "result_without_target": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM backtest_results result "
                    "JOIN backtest_tasks task ON task.id=result.backtest_task_id "
                    "JOIN backtest_runs run ON run.id=result.backtest_run_id "
                    "WHERE NOT EXISTS (SELECT 1 FROM strategy_targets target "
                    "JOIN execution_target_definitions definition "
                    "ON definition.id=target.execution_target_id "
                    "WHERE target.strategy_version_id=run.strategy_version_id "
                    "AND target.pair=task.pair AND target.timeframe=task.timeframe "
                    "AND definition.target_key='RESEARCH_ONLY')"
                )
            ).scalar_one()
        ),
        "deployment_target_mapping_gap_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_deployments deployment "
                    "LEFT JOIN strategy_targets target "
                    "ON target.id=deployment.strategy_target_id "
                    "LEFT JOIN execution_target_definitions definition "
                    "ON definition.id=target.execution_target_id "
                    "LEFT JOIN strategy_platform_migration_entity_mappings mapping "
                    "ON mapping.migration_run_id=:run "
                    "AND mapping.source_table='strategy_deployments' "
                    "AND mapping.source_primary_key=deployment.id::text "
                    "AND mapping.mapping_kind='LEGACY_DEMO_DEPLOYMENT_TARGET' "
                    "WHERE deployment.strategy_target_id IS NULL OR target.id IS NULL OR "
                    "definition.target_key IS DISTINCT FROM 'OKX_DEMO' OR "
                    "deployment.real_orders IS DISTINCT FROM FALSE OR "
                    "target.strategy_version_id IS DISTINCT FROM "
                    "deployment.strategy_version_id OR target.instrument_id IS DISTINCT FROM "
                    "deployment.instrument_id OR target.timeframe IS DISTINCT FROM "
                    "deployment.timeframe OR mapping.id IS NULL OR "
                    "mapping.mapping_status IS DISTINCT FROM 'MAPPED' OR "
                    "mapping.quality_status_asserted IS DISTINCT FROM 'UNKNOWN' OR "
                    "mapping.target_table IS DISTINCT FROM 'strategy_targets' OR "
                    "mapping.target_primary_key IS DISTINCT FROM target.id::text"
                ),
                {"run": migration_run_id},
            ).scalar_one()
        ),
        "plan_without_snapshot": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_validation_plans WHERE "
                    "strategy_target_id IS NULL OR quality_gate_profile_version_id IS NULL "
                    "OR validation_window_config_set_id IS NULL "
                    "OR configuration_bundle_snapshot_id IS NULL OR cycle_number IS NULL"
                )
            ).scalar_one()
        ),
        "window_without_config": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_validation_windows "
                    "WHERE window_config_id IS NULL OR window_key_snapshot IS NULL"
                )
            ).scalar_one()
        ),
        "trade_intent_lineage_audit_gap_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM trade_intents intent LEFT JOIN "
                    "strategy_platform_migration_entity_mappings mapping ON "
                    "mapping.migration_run_id=:run "
                    "AND mapping.source_table='trade_intents' "
                    "AND mapping.source_primary_key=intent.id::text "
                    "AND mapping.mapping_kind='LEGACY_SIGNAL_DEPLOYMENT_LINEAGE' "
                    "WHERE mapping.id IS NULL OR "
                    "mapping.quality_status_asserted IS DISTINCT FROM 'UNKNOWN' OR "
                    "mapping.mapping_status NOT IN ('PRESERVED','NOT_APPLICABLE') OR "
                    "(mapping.mapping_status='PRESERVED' AND ("
                    "mapping.target_table IS DISTINCT FROM 'signal_evaluations' OR "
                    "mapping.target_primary_key IS NULL)) OR "
                    "(mapping.mapping_status='NOT_APPLICABLE' AND ("
                    "mapping.target_table IS NOT NULL OR "
                    "mapping.target_primary_key IS NOT NULL))"
                ),
                {"run": migration_run_id},
            ).scalar_one()
        ),
        "legacy_qualified_summary_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_evaluation_summaries summary "
                    "JOIN strategy_validation_plans plan "
                    "ON plan.id=summary.validation_plan_id "
                    "WHERE summary.status='QUALIFIED' AND "
                    "plan.trigger_metadata::jsonb @> CAST(:metadata AS jsonb)"
                ),
                {
                    "metadata": _json_parameter(
                        {"migration": TASK1_MIGRATION_KEY}
                    )
                },
            ).scalar_one()
        ),
        "migration_mapping_digest_mismatch_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_platform_migration_entity_mappings "
                    "WHERE migration_run_id=:run "
                    "AND mapping_kind='LEGACY_ROW_PRESERVED' "
                    "AND source_digest IS DISTINCT FROM target_digest"
                ),
                {"run": migration_run_id},
            ).scalar_one()
        ),
        "strategy_score_association_gap_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_scores score LEFT JOIN "
                    "strategy_platform_migration_entity_mappings mapping ON "
                    "mapping.migration_run_id=:run "
                    "AND mapping.source_table='strategy_scores' "
                    "AND mapping.source_primary_key=score.id::text "
                    "AND mapping.mapping_kind='WINDOW_SCORE_ASSOCIATION' "
                    "WHERE mapping.id IS NULL OR "
                    "mapping.quality_status_asserted IS DISTINCT FROM 'UNKNOWN' OR "
                    "mapping.mapping_status IS NULL OR "
                    "mapping.mapping_status NOT IN ('MAPPED','NOT_APPLICABLE') OR "
                    "(mapping.mapping_status='MAPPED' AND ("
                    "mapping.target_table IS DISTINCT FROM 'validation_window_scores' OR "
                    "mapping.target_primary_key IS NULL)) OR "
                    "(mapping.mapping_status='NOT_APPLICABLE' AND "
                    "mapping.target_primary_key IS NOT NULL)"
                ),
                {"run": migration_run_id},
            ).scalar_one()
        ),
        "migration_unmapped_or_ambiguous_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_platform_migration_entity_mappings "
                    "WHERE migration_run_id=:run "
                    "AND mapping_status IN ('UNMAPPED','AMBIGUOUS')"
                ),
                {"run": migration_run_id},
            ).scalar_one()
        ),
        "migration_conflict_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_platform_migration_conflicts "
                    "WHERE migration_run_id=:run AND status IN ('OPEN','BLOCKED')"
                ),
                {"run": migration_run_id},
            ).scalar_one()
        ),
        "legacy_workflow_configuration_audit_gap_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM ("
                    "SELECT 'research_jobs'::text AS source_table,id,"
                    "configuration_bundle_snapshot_id FROM research_jobs UNION ALL "
                    "SELECT 'strategy_deployments'::text,id,"
                    "configuration_bundle_snapshot_id FROM strategy_deployments"
                    ") source WHERE NOT EXISTS (SELECT 1 FROM "
                    "strategy_platform_migration_entity_mappings mapping "
                    "WHERE mapping.migration_run_id=:run "
                    "AND mapping.source_table=source.source_table "
                    "AND mapping.source_primary_key=source.id::text "
                    "AND ((source.configuration_bundle_snapshot_id IS NULL "
                    "AND mapping.mapping_kind='LEGACY_CONFIGURATION_UNKNOWN' "
                    "AND mapping.mapping_status='NOT_APPLICABLE' "
                    "AND mapping.target_primary_key IS NULL) OR "
                    "(source.configuration_bundle_snapshot_id IS NOT NULL "
                    "AND mapping.mapping_kind='LEGACY_CONFIGURATION_PRESERVED' "
                    "AND mapping.mapping_status='MAPPED' "
                    "AND mapping.target_primary_key="
                    "source.configuration_bundle_snapshot_id::text)))"
                ),
                {"run": migration_run_id},
            ).scalar_one()
        ),
        "configuration_dependency_cycle_count": int(
            connection.execute(
                text(
                    "WITH RECURSIVE walk(root,current,path,cycle) AS ("
                    "SELECT configuration_version_id,depends_on_version_id,"
                    "ARRAY[configuration_version_id,depends_on_version_id],FALSE "
                    "FROM configuration_dependencies UNION ALL SELECT walk.root,"
                    "edge.depends_on_version_id,walk.path||edge.depends_on_version_id,"
                    "edge.depends_on_version_id=ANY(walk.path) FROM walk JOIN "
                    "configuration_dependencies edge ON edge.configuration_version_id="
                    "walk.current WHERE NOT walk.cycle) SELECT count(*) FROM walk WHERE cycle"
                )
            ).scalar_one()
        ),
        "configuration_bundle_digest_mismatch_count": (
            _configuration_bundle_digest_mismatch_count(connection)
        ),
        "unvalidated_constraint_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM pg_constraint constraint_row "
                    "JOIN pg_class table_row ON table_row.oid=constraint_row.conrelid "
                    "JOIN pg_namespace namespace_row ON namespace_row.oid=table_row.relnamespace "
                    "WHERE namespace_row.nspname=current_schema() "
                    "AND NOT constraint_row.convalidated"
                )
            ).scalar_one()
        ),
        "formal_window_config_count_mismatch": int(
            connection.execute(
                text(
                    "SELECT abs(count(*)-30) FROM validation_window_configs config "
                    "JOIN validation_window_config_sets config_set "
                    "ON config_set.id=config.config_set_id "
                    "WHERE config_set.name='formal-dynamic-validation-windows-v1'"
                )
            ).scalar_one()
        ),
        "formal_window_receipt_provenance_mismatch_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM validation_window_configs config "
                    "JOIN validation_window_config_sets config_set "
                    "ON config_set.id=config.config_set_id "
                    "LEFT JOIN market_data_quality_receipts receipt "
                    "ON receipt.id=config.source_receipt_id "
                    "WHERE config_set.name='formal-dynamic-validation-windows-v1' AND ("
                    "config.source_receipt_id IS NULL OR receipt.id IS NULL OR "
                    "receipt.contract_version<>'market-data-quality-v13-v1' OR "
                    "receipt.status<>'PASSED' OR "
                    "receipt.quality_scope<>"
                    "'MIGRATION_SOURCE_CONSISTENCY_AS_OF_SOURCE_RECEIPT' OR "
                    "receipt.quality_decision<>'NOT_STRATEGY_QUALIFICATION' OR "
                    "receipt.freshness_seconds IS NOT NULL OR "
                    "receipt.pair<>config.pair OR receipt.timeframe<>config.timeframe OR "
                    "receipt.file_sha256 IS DISTINCT FROM "
                    "config.classification_evidence->>'file_sha256')"
                )
            ).scalar_one()
        ),
    }
    if any(checks.values()):
        raise StrategyPlatformTask1Blocked(
            "Task 1 reconciliation failed: "
            + ", ".join(f"{key}={value}" for key, value in checks.items() if value)
        )
    for source in market_inventory:
        matches = connection.execute(
            text(
                "SELECT pair,timeframe,file_size,file_sha256,row_count,first_open_at,"
                "last_open_at,last_close_at,scan_evidence_digest,source_receipt_digest,"
                "source_receipt_id "
                "FROM market_data_file_records WHERE pair=:pair AND timeframe=:timeframe "
                "AND file_sha256=:digest AND observed_at=:observed"
            ),
            {
                "pair": source.pair,
                "timeframe": source.timeframe,
                "digest": source.sha256,
                "observed": source.observed_at,
            },
        ).mappings().all()
        if len(matches) != 1:
            raise StrategyPlatformTask1Blocked(
                f"market inventory reconciliation cardinality failed for "
                f"{source.pair} {source.timeframe}: {len(matches)}"
            )
        row = matches[0]
        if (
            int(row["file_size"]) != source.size_bytes
            or row["file_sha256"] != source.sha256
            or int(row["row_count"]) != source.row_count
            or _aware_utc(row["first_open_at"]) != _aware_utc(source.first_open_at)
            or _aware_utc(row["last_open_at"]) != _aware_utc(source.last_open_at)
            or _aware_utc(row["last_close_at"]) != _aware_utc(source.last_close_at)
            or row["scan_evidence_digest"] != _market_file_scan_digest(source)
            or row["source_receipt_digest"] != source.source_receipt_digest
        ):
            raise StrategyPlatformTask1Blocked(
                f"market inventory reconciliation failed for "
                f"{row['pair']} {row['timeframe']}"
            )
        receipt = connection.execute(
            text(
                "SELECT idempotency_key,contract_version,status,evidence_digest,"
                "quality_scope,quality_decision,file_identity_digest,"
                "source_identity_digest,aggregate_receipt_digest,"
                "migration_artifact_digest,freshness_basis,freshness_seconds "
                "FROM market_data_quality_receipts WHERE id=:id"
            ),
            {"id": row["source_receipt_id"]},
        ).mappings().first()
        if (
            receipt is None
            or receipt["idempotency_key"]
            != _market_quality_receipt_idempotency_key(source)
            or receipt["contract_version"] != "market-data-quality-v13-v1"
            or receipt["status"] != "PASSED"
            or receipt["evidence_digest"]
            != canonical_digest(_market_quality_receipt_payload(source))
            or receipt["quality_scope"] != source.quality_scope
            or receipt["quality_decision"] != source.quality_decision
            or receipt["file_identity_digest"] != source.file_identity_digest
            or receipt["source_identity_digest"] != source.source_identity_digest
            or receipt["aggregate_receipt_digest"] != source.aggregate_receipt_digest
            or receipt["migration_artifact_digest"] != source.migration_artifact_digest
            or receipt["freshness_basis"] != source.freshness_basis
            or receipt["freshness_seconds"] is not None
        ):
            raise StrategyPlatformTask1Blocked(
                f"market quality receipt reconciliation failed for "
                f"{source.pair} {source.timeframe}"
            )
    target_snapshot = _collect_target_snapshot(connection)
    observations = {
        "qualified_summary_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_evaluation_summaries "
                    "WHERE status='QUALIFIED'"
                )
            ).scalar_one()
        ),
        "candidate_qualified_count": int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_research_candidates "
                    "WHERE status='QUALIFIED'"
                )
            ).scalar_one()
        ),
    }
    return {
        "contract": "strategy-platform-v13-task1-reconciliation-v1",
        "source_snapshot_digest": source_snapshot_digest,
        "target_snapshot_digest": _snapshot_digest(target_snapshot),
        "counts": counts,
        "checks": checks,
        "observations": observations,
        "legacy_status_upgrade_count": checks["legacy_qualified_summary_count"],
        "qualified_created_count": checks["legacy_qualified_summary_count"],
        "credential_attestation": "OUT_OF_SCOPE",
        "runtime_execution_evidence": "UNKNOWN",
        "target_snapshot": target_snapshot,
    }


def migrate_strategy_platform_v13_task1(
    connection: Connection,
    *,
    market_inventory: Sequence[MarketFileEvidence],
    actor: str,
    request_id: str,
    execution_scope: str,
    aggregate_source_matrix_status: str,
    source_schema_version: str,
    evidence_manifest: Mapping[str, Any],
    report_path: str | None = None,
    runtime_schema_compatible: bool = False,
    writers_quiesced: bool = False,
) -> Task1MigrationResult:
    """Run the complete forward-only real-data migration in one transaction."""

    if connection.dialect.name != "postgresql":
        raise StrategyPlatformTask1Blocked("Task 1 real-data migration requires PostgreSQL")
    connection.execute(text("SET LOCAL lock_timeout='3s'"))
    connection.execute(text("SET LOCAL statement_timeout='15min'"))
    assert_task1_migration_fence(
        connection,
        execution_scope=execution_scope,
        runtime_schema_compatible=runtime_schema_compatible,
        writers_quiesced=writers_quiesced,
    )
    inventory = validate_market_inventory(market_inventory)
    bound_evidence_manifest = validate_task1_evidence_manifest(
        evidence_manifest, inventory
    )
    if aggregate_source_matrix_status != "PASSED":
        raise StrategyPlatformTask1Blocked(
            "corrected aggregate source matrix is not PASSED"
        )
    verify_market_artifacts(inventory)
    before_acl = _acl_digest(connection)
    source_snapshot = collect_legacy_snapshot(connection)
    source_digest = _snapshot_digest(source_snapshot)
    run_id, prior_status = _ensure_migration_run(
        connection,
        execution_scope=execution_scope,
        source_schema_version=source_schema_version,
        source_snapshot_digest=source_digest,
        actor=actor,
        request_id=request_id,
        report_path=report_path,
        evidence_manifest=bound_evidence_manifest,
    )
    if prior_status == "SUCCEEDED":
        stored = connection.execute(
            text(
                "SELECT target_snapshot_digest,report_digest FROM "
                "strategy_platform_migration_runs "
                "WHERE id=:run AND status='SUCCEEDED'"
            ),
            {"run": run_id},
        ).mappings().one()
        stored_target_digest = stored["target_snapshot_digest"]
        report = reconcile_strategy_platform_v13_task1(
            connection,
            migration_run_id=run_id,
            source_snapshot=source_snapshot,
            source_snapshot_digest=source_digest,
            market_inventory=inventory,
        )
        if stored_target_digest != report["target_snapshot_digest"]:
            raise StrategyPlatformTask1Blocked(
                "repeat reconciliation target digest drifted from the successful run: "
                f"stored={stored_target_digest} current={report['target_snapshot_digest']}"
            )
        mappings = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM strategy_platform_migration_entity_mappings "
                    "WHERE migration_run_id=:run"
                ),
                {"run": run_id},
            ).scalar_one()
        )
        return Task1MigrationResult(
            migration_run_id=run_id,
            strategy_target_count=report["counts"]["strategy_targets"],
            mapped_version_count=report["counts"]["strategy_versions"],
            mapped_plan_count=report["counts"]["plans"],
            mapped_window_count=report["counts"]["windows"],
            blocked_summary_count=report["counts"]["summaries"],
            market_file_count=report["counts"]["market_files"],
            unmapped_count=0,
            conflict_count=0,
            repeat_noop=True,
            source_snapshot_digest=source_digest,
            target_snapshot_digest=report["target_snapshot_digest"],
            report={
                **{key: value for key, value in report.items() if key != "target_snapshot"},
                "mapping_count": mappings,
                "repeat_noop": True,
                "report_digest": stored["report_digest"],
                "evidence_manifest_digest": canonical_digest(
                    bound_evidence_manifest
                ),
            },
        )
    if prior_status != "RUNNING":
        raise StrategyPlatformTask1Blocked(
            f"migration run {run_id} is not restartable from status {prior_status}"
        )
    _record_table_snapshots(
        connection,
        migration_run_id=run_id,
        phase="BEFORE",
        facts=source_snapshot,
    )
    _record_legacy_source_rows(connection, migration_run_id=run_id)
    receipt_ids = _ensure_market_quality_receipts(connection, inventory)
    persisted_inventory = tuple(
        replace(
            item,
            receipt_id=receipt_ids[(item.pair, item.timeframe)],
        )
        for item in inventory
    )
    seeded = seed_v13_configuration_graph(
        connection,
        market_inventory=persisted_inventory,
        actor=actor,
        aggregate_source_matrix_status=aggregate_source_matrix_status,
    )
    _insert_market_file_records(connection, persisted_inventory, receipt_ids)
    version_targets, deployment_targets = _ensure_strategy_targets_from_evidence(
        connection,
        execution_targets=seeded["execution_targets"],
        inventory=persisted_inventory,
    )
    legacy_window_id, window_mapping = _ensure_legacy_window_configuration(
        connection, actor=actor
    )
    _, legacy_validation_bundle = _ensure_legacy_bundle(
        connection,
        legacy_window_config_id=legacy_window_id,
        legacy_quality_id=seeded["legacy_quality_id"],
        actor=actor,
    )
    legacy_scoring = _seed_legacy_scoring_profile(
        connection,
        metric_versions=seeded["registry_versions"]["metrics"],
        actor=actor,
    )
    mapped_plans, mapped_windows, _ = _backfill_validation_evidence(
        connection,
        migration_run_id=run_id,
        version_targets=version_targets,
        legacy_window_config_id=legacy_window_id,
        window_config_by_legacy_id=window_mapping,
        legacy_quality_id=seeded["legacy_quality_id"],
        legacy_bundle_id=legacy_validation_bundle,
        legacy_scoring_profile_id=legacy_scoring,
    )
    blocked_summaries = _insert_blocked_legacy_summaries(connection)
    _record_legacy_workflow_configuration_unknown(
        connection,
        migration_run_id=run_id,
    )
    _record_legacy_trade_intent_lineage(
        connection, migration_run_id=run_id
    )
    _record_legacy_entity_mappings(
        connection,
        migration_run_id=run_id,
        version_targets=version_targets,
        deployment_targets=deployment_targets,
        window_config_by_legacy_id=window_mapping,
    )
    connection.execute(
        text(
            "UPDATE strategy_platform_migration_runs SET status='RECONCILING' "
            "WHERE id=:id AND status='RUNNING'"
        ),
        {"id": run_id},
    )
    report = reconcile_strategy_platform_v13_task1(
        connection,
        migration_run_id=run_id,
        source_snapshot=source_snapshot,
        source_snapshot_digest=source_digest,
        market_inventory=persisted_inventory,
    )
    after_acl = _acl_digest(connection)
    if before_acl != after_acl:
        raise StrategyPlatformTask1Blocked("Task 1 migration changed schema/table ACL")
    target_snapshot = report["target_snapshot"]
    _record_table_snapshots(
        connection,
        migration_run_id=run_id,
        phase="AFTER",
        facts=target_snapshot,
    )
    report_without_rows = {
        key: value for key, value in report.items() if key != "target_snapshot"
    }
    report_without_rows["evidence_manifest_digest"] = canonical_digest(
        bound_evidence_manifest
    )
    report_digest = canonical_digest(report_without_rows)
    connection.execute(
        text(
            "UPDATE strategy_platform_migration_runs SET status='SUCCEEDED',"
            "target_snapshot_digest=:target,report_digest=:report,"
            "completed_at=clock_timestamp() WHERE id=:id AND status='RECONCILING'"
        ),
        {
            "id": run_id,
            "target": report["target_snapshot_digest"],
            "report": report_digest,
        },
    )
    return Task1MigrationResult(
        migration_run_id=run_id,
        strategy_target_count=report["counts"]["strategy_targets"],
        mapped_version_count=len({key[0] for key in version_targets}),
        mapped_plan_count=mapped_plans,
        mapped_window_count=mapped_windows,
        blocked_summary_count=blocked_summaries,
        market_file_count=report["counts"]["market_files"],
        unmapped_count=0,
        conflict_count=0,
        repeat_noop=False,
        source_snapshot_digest=source_digest,
        target_snapshot_digest=report["target_snapshot_digest"],
        report={**report_without_rows, "report_digest": report_digest, "repeat_noop": False},
    )


def _record_blocked_migration_attempt(
    connection: Connection,
    *,
    actor: str,
    request_id: str,
    execution_scope: str,
    source_schema_version: str,
    report_path: str | None,
    evidence_manifest: Mapping[str, Any],
    error: Exception,
) -> int:
    """Persist failure evidence after the mutating transaction has rolled back."""

    source_snapshot = collect_legacy_snapshot(connection)
    source_digest = _snapshot_digest(source_snapshot)
    evidence_manifest_digest = canonical_digest(evidence_manifest)
    is_blocked = isinstance(error, StrategyPlatformTask1Blocked)
    status = "BLOCKED" if is_blocked else "FAILED"
    error_text = str(error)
    candidate_code = error_text.split(":", 1)[0]
    error_code = (
        candidate_code
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,159}", candidate_code or "")
        else f"TASK1_MIGRATION_{status}"
    )
    existing = connection.execute(
        text(
            "SELECT id,status,operator_identity,source_schema_version,"
            "source_snapshot_digest,"
            "evidence_manifest_digest,report_path,error_code,error_message FROM "
            "strategy_platform_migration_runs WHERE migration_key=:key "
            "AND execution_scope=:scope AND request_id=:request_id"
        ),
        {
            "key": TASK1_MIGRATION_KEY,
            "scope": execution_scope,
            "request_id": request_id,
        },
    ).mappings().first()
    if existing is not None:
        if (
            existing["status"] == status
            and existing["operator_identity"] == actor
            and existing["source_schema_version"] == source_schema_version
            and existing["source_snapshot_digest"] == source_digest
            and existing["evidence_manifest_digest"] == evidence_manifest_digest
            and existing["report_path"] == report_path
            and existing["error_code"] == error_code
            and existing["error_message"] == error_text
        ):
            return int(existing["id"])
        failure_suffix = canonical_digest(
            {
                "request_id": request_id,
                "status": status,
                "actor": actor,
                "source_schema_version": source_schema_version,
                "source_snapshot_digest": source_digest,
                "evidence_manifest_digest": evidence_manifest_digest,
                "report_path": report_path,
                "error_code": error_code,
                "error_message": error_text,
            }
        )[:16]
        request_id = f"{request_id[:134]}:failure:{failure_suffix}"
        repeated = connection.execute(
            text(
                "SELECT id FROM strategy_platform_migration_runs WHERE migration_key=:key "
                "AND execution_scope=:scope AND request_id=:request_id"
            ),
            {
                "key": TASK1_MIGRATION_KEY,
                "scope": execution_scope,
                "request_id": request_id,
            },
        ).scalar_one_or_none()
        if repeated is not None:
            return int(repeated)
    run_id = int(
        connection.execute(
            text(
                "INSERT INTO strategy_platform_migration_runs "
                "(migration_key,execution_scope,source_schema_version,"
                "target_schema_version,source_snapshot_digest,status,operator_identity,"
                "request_id,unknown_dimensions,evidence_manifest,"
                "evidence_manifest_digest,report_path,error_code,error_message,"
                "completed_at,destructive_write_count,overwritten_row_count,"
                "deleted_row_count) VALUES (:key,:scope,:source,:target,:digest,:status,"
                ":actor,:request_id,CAST(:unknown AS json),CAST(:evidence AS json),"
                ":evidence_digest,:report,:error_code,"
                ":error_message,clock_timestamp(),0,0,0) RETURNING id"
            ),
            {
                "key": TASK1_MIGRATION_KEY,
                "scope": execution_scope,
                "source": source_schema_version,
                "target": TASK1_SCHEMA_VERSION,
                "digest": source_digest,
                "status": status,
                "actor": actor,
                "request_id": request_id,
                "unknown": _json_parameter(
                    [
                        "credential_attestation:OUT_OF_SCOPE",
                        "runtime_execution_evidence:UNKNOWN",
                        "migration_changes:ROLLED_BACK",
                    ]
                ),
                "evidence": _json_parameter(evidence_manifest),
                "evidence_digest": evidence_manifest_digest,
                "report": report_path,
                "error_code": error_code,
                "error_message": error_text,
            },
        ).scalar_one()
    )
    connection.execute(
        text(
            "INSERT INTO strategy_platform_migration_conflicts "
            "(migration_run_id,conflict_key,entity_kind,status,reason_code,"
            "source_identifiers,candidate_targets,resolution_evidence) "
            "VALUES (:run,:key,'MIGRATION_ATTEMPT','BLOCKED',:reason,"
            "CAST(:source AS json),'[]'::json,CAST(:evidence AS json))"
        ),
        {
            "run": run_id,
            "key": f"{request_id}:rolled-back",
            "reason": error_code,
            "source": _json_parameter([source_digest]),
            "evidence": _json_parameter(
                {
                    "all_migration_changes_rolled_back": True,
                    "destructive_write_count": 0,
                    "overwritten_row_count": 0,
                    "deleted_row_count": 0,
                    "credential_attestation": "OUT_OF_SCOPE",
                    "runtime_execution_evidence": "UNKNOWN",
                }
            ),
        },
    )
    return run_id


def execute_strategy_platform_v13_task1(
    engine: Engine,
    *,
    market_inventory: Sequence[MarketFileEvidence],
    actor: str,
    request_id: str,
    execution_scope: str,
    aggregate_source_matrix_status: str,
    source_schema_version: str,
    evidence_manifest: Mapping[str, Any],
    report_path: str | None = None,
) -> Task1MigrationResult:
    """Own the transaction and durably audit either success or full rollback."""

    if execution_scope == "SHARED_DATABASE":
        raise StrategyPlatformTask1Blocked(
            "BLOCKED_LEGACY_SOURCE_READ_ONLY: the legacy shared database remains "
            "a strict NO_OP; no database audit row was written"
        )
    try:
        with engine.begin() as connection:
            return migrate_strategy_platform_v13_task1(
                connection,
                market_inventory=market_inventory,
                actor=actor,
                request_id=request_id,
                execution_scope=execution_scope,
                aggregate_source_matrix_status=aggregate_source_matrix_status,
                source_schema_version=source_schema_version,
                evidence_manifest=evidence_manifest,
                report_path=report_path,
            )
    except Exception as error:
        try:
            with engine.begin() as audit_connection:
                _record_blocked_migration_attempt(
                    audit_connection,
                    actor=actor,
                    request_id=request_id,
                    execution_scope=execution_scope,
                    source_schema_version=source_schema_version,
                    report_path=report_path,
                    evidence_manifest=evidence_manifest,
                    error=error,
                )
        except Exception as audit_error:  # pragma: no cover - last-resort evidence note
            if hasattr(error, "add_note"):
                error.add_note(
                    f"Task 1 failure audit could not be persisted: {audit_error}"
                )
        raise
