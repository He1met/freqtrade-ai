from __future__ import annotations

from hashlib import sha256
import inspect as python_inspect
from pathlib import Path
import re

from sqlalchemy import CheckConstraint, DateTime, JSON, UniqueConstraint, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.canonical_v13 import models as canonical_models
from app.canonical_v13 import (
    accounting,
    deployment_approval,
    deployment_control,
    fill_service,
    order_service,
    reconciliation,
    research_authorization,
    research_execution,
    risk_service,
    signal_service,
)
from app.canonical_v13.genesis import (
    postgresql_acl_problems,
    render_postgresql_acl_sql,
    render_postgresql_genesis_ddl,
)
from app.canonical_v13.manifest import (
    CANONICAL_AUTHORITY_REVISION,
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_DATABASE_PURPOSE,
    CANONICAL_GENESIS_VERSION,
    CANONICAL_LEGACY_IMPORT_MODE,
    CANONICAL_MANIFEST_DIGEST,
    CANONICAL_MANIFEST_KEY,
    CANONICAL_TABLE_MANIFEST,
    CANONICAL_TABLE_NAMES,
    CANONICAL_TABLES_BY_DOMAIN,
    CANONICAL_TRADING_CAPABILITY,
    INITIAL_PRODUCTION_STATES,
    P0_CONFIGURATION_KINDS,
    READER_TABLE_ALLOWLIST,
    WRITER_READ_ALLOWLIST,
    WRITER_TABLE_ALLOWLIST,
    canonical_manifest_json,
    manifest_problems,
)
from app.canonical_v13.models import CanonicalBase


EXPECTED_TABLES_BY_DOMAIN = {
    "schema_audit": (
        "schema_metadata",
        "audit_events",
        "idempotency_receipts",
    ),
    "intake_catalog": (
        "strategy_artifacts",
        "strategy_submissions",
        "strategy_intake_receipts",
        "strategies",
        "strategy_versions",
    ),
    "control_plane": (
        "configuration_profiles",
        "configuration_versions",
        "configuration_dependencies",
        "configuration_snapshots",
        "configuration_snapshot_members",
        "configuration_bundles",
        "configuration_bundle_members",
        "configuration_activations",
        "research_targets",
        "research_target_allocations",
    ),
    "market": (
        "market_profiles",
        "market_profile_versions",
        "market_artifacts",
        "market_inspections",
        "market_receipts",
        "market_snapshots",
        "market_snapshot_members",
    ),
    "validation": (
        "research_gate_attempts",
        "research_gate_receipts",
        "validation_plans",
        "validation_plan_windows",
        "validation_attempts",
        "validation_window_results",
    ),
    "scoring_qualification": (
        "target_scores",
        "qualification_decisions",
        "qualification_window_evidence",
    ),
    "optimization": ("optimization_runs", "optimization_trials"),
    "approval_deployment": (
        "deployment_approvals",
        "deployments",
        "runtime_image_acceptances",
        "runtime_instances",
        "runtime_receipts",
    ),
    "execution": (
        "execution_canary_probe_receipts",
        "execution_canary_risk_policies",
        "execution_risk_budget_authorizations",
        "execution_risk_reservations",
        "execution_attestations",
        "order_writer_leases",
        "signals",
        "trade_intents",
        "risk_decisions",
        "orders",
        "order_dispatch_receipts",
        "order_dispatch_outcome_receipts",
        "fills",
        "ledger_entries",
        "reconciliation_runs",
        "reconciliation_items",
    ),
}


def test_exact_identity_and_table_manifest_matches_frozen_design() -> None:
    assert CANONICAL_AUTHORITY_REVISION == "20260822_phase9_deployment_rollover12"
    assert CANONICAL_DATABASE_PURPOSE == "FREQTRADE_AI_V13_CANONICAL"
    assert CANONICAL_BUSINESS_SCHEMA == "strategy_platform_v13"
    assert CANONICAL_GENESIS_VERSION == "20260814_01"
    assert CANONICAL_MANIFEST_KEY == "canonical-v13-table-manifest-v1"
    assert CANONICAL_LEGACY_IMPORT_MODE == "EXTERNAL_LATEST_ONLY"
    assert CANONICAL_TRADING_CAPABILITY == "TRADING_DISABLED"
    assert dict(CANONICAL_TABLES_BY_DOMAIN) == EXPECTED_TABLES_BY_DOMAIN
    assert CANONICAL_TABLE_NAMES == tuple(
        table for tables in EXPECTED_TABLES_BY_DOMAIN.values() for table in tables
    )
    assert len(CANONICAL_TABLE_NAMES) == 57
    assert manifest_problems() == ()


def test_manifest_digest_is_canonical_sha256() -> None:
    manifest_json = canonical_manifest_json()
    assert (
        CANONICAL_MANIFEST_DIGEST == sha256(manifest_json.encode("utf-8")).hexdigest()
    )
    assert len(CANONICAL_MANIFEST_DIGEST) == 64
    assert "20260813_47" not in manifest_json
    assert "freqtrade_ai_design_lab" not in manifest_json
    assert "strategy_platform_migration_" not in manifest_json


def test_seven_p0_kinds_and_initial_values_have_no_business_defaults() -> None:
    assert P0_CONFIGURATION_KINDS == (
        "TARGET",
        "WINDOW",
        "GENERATION",
        "DIVERSITY",
        "QUALITY_QUALIFICATION",
        "SCORING",
        "RESEARCH_AGGREGATE",
    )
    assert dict(INITIAL_PRODUCTION_STATES) == {
        "target_set": "UNSET",
        "per_target_allocation": "UNSET",
        "per_target_cap": "UNSET",
        "market_profile": "UNSET",
        "market_snapshot": "UNSET",
        "window_coverage": "UNSET",
        "research_bundle": "BLOCKED",
        "trading": "TRADING_DISABLED",
    }
    allocation = CanonicalBase.metadata.tables[
        f"{CANONICAL_BUSINESS_SCHEMA}.research_target_allocations"
    ]
    assert allocation.c.allocation_count.default is None
    assert allocation.c.allocation_count.server_default is None
    assert allocation.c.candidate_cap.default is None
    assert allocation.c.candidate_cap.server_default is None


def test_each_table_has_one_writer_and_reader_maps_are_explicit() -> None:
    table_writer_counts = {table: 0 for table in CANONICAL_TABLE_NAMES}
    for tables in WRITER_TABLE_ALLOWLIST.values():
        for table in tables:
            table_writer_counts[table] += 1
    assert set(table_writer_counts.values()) == {1}
    assert WRITER_TABLE_ALLOWLIST["canonical_projection_writer"] == ()
    assert WRITER_TABLE_ALLOWLIST["canonical_validation_writer"] == (
        "research_gate_attempts",
        "research_gate_receipts",
        "validation_plans",
        "validation_plan_windows",
        "validation_attempts",
        "validation_window_results",
    )
    assert WRITER_TABLE_ALLOWLIST["canonical_scoring_writer"] == ("target_scores",)
    assert WRITER_TABLE_ALLOWLIST["canonical_qualification_writer"] == (
        "qualification_decisions",
        "qualification_window_evidence",
    )
    assert WRITER_TABLE_ALLOWLIST["canonical_optimization_writer"] == (
        "optimization_runs",
        "optimization_trials",
    )
    assert "canonical_research_writer" not in WRITER_TABLE_ALLOWLIST

    for entry in CANONICAL_TABLE_MANIFEST:
        assert entry.table in WRITER_TABLE_ALLOWLIST[entry.writer]
        assert entry.writer_privileges in {
            ("SELECT", "INSERT"),
            ("SELECT", "INSERT", "UPDATE"),
        }
        assert "DELETE" not in entry.writer_privileges
        for reader in entry.readers:
            if reader in READER_TABLE_ALLOWLIST:
                assert entry.table in READER_TABLE_ALLOWLIST[reader]
            else:
                assert entry.table in WRITER_READ_ALLOWLIST[reader]

    assert set(READER_TABLE_ALLOWLIST["canonical_research_reader"]).isdisjoint(
        {"orders", "fills", "ledger_entries"}
    )
    assert {
        "market_artifacts",
        "market_inspections",
        "market_receipts",
        "market_snapshots",
        "market_snapshot_members",
    }.issubset(READER_TABLE_ALLOWLIST["canonical_research_reader"])
    assert set(READER_TABLE_ALLOWLIST["canonical_runtime_reader"]).isdisjoint(
        {"validation_attempts", "validation_window_results", "target_scores"}
    )
    assert (
        "qualification_decisions" in READER_TABLE_ALLOWLIST["canonical_runtime_reader"]
    )
    assert set(READER_TABLE_ALLOWLIST["canonical_runtime_reader"]).isdisjoint(
        {"signals", "trade_intents", "risk_decisions", "orders", "fills"}
    )


def test_metadata_is_independent_exact_and_all_foreign_keys_stay_canonical() -> None:
    source = python_inspect.getsource(canonical_models)
    assert "from app.models" not in source
    assert "import app.models" not in source
    assert "Base.metadata" not in source.replace("CanonicalBase.metadata", "")

    tables = tuple(CanonicalBase.metadata.tables.values())
    assert {table.name for table in tables} == set(CANONICAL_TABLE_NAMES)
    assert {table.schema for table in tables} == {CANONICAL_BUSINESS_SCHEMA}
    for table in tables:
        for foreign_key in table.foreign_keys:
            assert foreign_key.column.table.schema == CANONICAL_BUSINESS_SCHEMA
            assert foreign_key.column.table.name in CANONICAL_TABLE_NAMES
            local = foreign_key.parent
            if not local.unique:
                assert any(
                    tuple(index.columns) == (local,) for index in table.indexes
                ), f"non-unique FK lacks index: {table.name}.{local.name}"
        for column in table.columns:
            if column.name.endswith("digest"):
                assert any(
                    "length(" in str(constraint.sqltext).lower()
                    for constraint in column.constraints
                ), f"digest lacks length check: {table.name}.{column.name}"

    plan_windows = next(
        table for table in tables if table.name == "validation_plan_windows"
    )
    member_column = plan_windows.c.window_snapshot_member_id
    assert member_column.nullable is False
    foreign_keys = tuple(member_column.foreign_keys)
    assert len(foreign_keys) == 1
    assert foreign_keys[0].target_fullname == (
        f"{CANONICAL_BUSINESS_SCHEMA}.configuration_snapshot_members.id"
    )


def test_postgresql_ddl_and_acl_are_offline_exact_allowlists() -> None:
    ddl = render_postgresql_genesis_ddl()
    assert f"CREATE SCHEMA IF NOT EXISTS {CANONICAL_BUSINESS_SCHEMA}" in ddl
    assert "OWNER TO canonical_schema_owner" in ddl
    for table_name in CANONICAL_TABLE_NAMES:
        assert f"{CANONICAL_BUSINESS_SCHEMA}.{table_name}" in ddl
        assert (
            f"ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
            "OWNER TO canonical_schema_owner"
        ) in ddl
    assert "20260813_47" not in ddl
    assert "strategy_platform_migration_" not in ddl
    assert "okx_demo_" not in ddl.lower()
    assert "CREATE INDEX ix_validation_attempts_validation_plan_id" in ddl
    assert "length(request_digest) = 64" in ddl
    last_create_index = ddl.rfind("CREATE INDEX")
    first_table_owner = ddl.find("ALTER TABLE")
    schema_owner = ddl.find(
        f"ALTER SCHEMA {CANONICAL_BUSINESS_SCHEMA} OWNER TO canonical_schema_owner"
    )
    assert 0 < last_create_index < first_table_owner < schema_owner
    assert ddl.rstrip().endswith(
        f"ALTER SCHEMA {CANONICAL_BUSINESS_SCHEMA} OWNER TO canonical_schema_owner;"
    )

    acl = render_postgresql_acl_sql()
    assert postgresql_acl_problems(acl) == ()
    assert "GRANT SELECT ON ALL TABLES" not in acl
    assert "SECURITY DEFINER" not in acl
    assert "GRANT ALL" not in acl
    assert " TO PUBLIC" not in acl
    for writer, table_names in WRITER_TABLE_ALLOWLIST.items():
        for table_name in table_names:
            assert (
                f"ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} TO {writer}" in acl
            )
    for reader, table_names in READER_TABLE_ALLOWLIST.items():
        for table_name in table_names:
            assert (
                f"GRANT SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
                f"TO {reader}"
            ) in acl
    for writer, table_names in WRITER_READ_ALLOWLIST.items():
        assert set(table_names).isdisjoint(WRITER_TABLE_ALLOWLIST[writer])
        for table_name in table_names:
            assert (
                f"GRANT SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
                f"TO {writer}"
            ) in acl

    assert WRITER_READ_ALLOWLIST["canonical_order_writer"] == (
        "schema_metadata",
        "deployments",
        "trade_intents",
        "risk_decisions",
        "execution_risk_budget_authorizations",
        "execution_canary_risk_policies",
        "execution_risk_reservations",
        "execution_attestations",
        "execution_canary_probe_receipts",
    )
    assert WRITER_TABLE_ALLOWLIST["canonical_order_writer"] == (
        "orders",
        "order_dispatch_receipts",
        "order_dispatch_outcome_receipts",
        "order_writer_leases",
    )
    assert WRITER_READ_ALLOWLIST["canonical_control_writer"] == (
        "schema_metadata",
        "deployment_approvals",
        "deployments",
        "runtime_instances",
        "runtime_receipts",
        "order_writer_leases",
        "execution_canary_risk_policies",
        "execution_risk_budget_authorizations",
        "execution_risk_reservations",
        "signals",
        "trade_intents",
        "risk_decisions",
        "orders",
        "fills",
        "ledger_entries",
        "reconciliation_runs",
        "reconciliation_items",
    )

    reconciliation_items = CanonicalBase.metadata.tables[
        f"{CANONICAL_BUSINESS_SCHEMA}.reconciliation_items"
    ]
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in reconciliation_items.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("evidence_digest",) in unique_columns
    assert ("order_id", "fill_id", "ledger_entry_id") not in unique_columns


def test_postgresql_types_constraints_and_locking_compile_offline() -> None:
    tables = tuple(CanonicalBase.metadata.sorted_tables)
    foreign_keys = tuple(key for table in tables for key in table.foreign_keys)
    checks = tuple(
        constraint
        for table in tables
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )
    uniques = tuple(
        constraint
        for table in tables
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    )
    indexes = tuple(index for table in tables for index in table.indexes)
    datetimes = tuple(
        column
        for table in tables
        for column in table.columns
        if isinstance(column.type, DateTime)
    )
    json_columns = tuple(
        column
        for table in tables
        for column in table.columns
        if isinstance(column.type, JSON)
    )

    assert len(tables) == 57
    assert len(foreign_keys) == 120
    assert len(checks) == 76
    assert len(uniques) == 77
    assert len(indexes) == 104
    assert len(datetimes) == 98
    assert len(json_columns) == 27
    assert all(key.deferrable is not True for key in foreign_keys)
    assert all(column.type.timezone is True for column in datetimes)
    assert all(
        column.server_default is None for table in tables for column in table.columns
    )

    dialect = postgresql.dialect()
    compiled_tables = "\n".join(
        str(CreateTable(table).compile(dialect=dialect)) for table in tables
    )
    assert compiled_tables.count("TIMESTAMP WITH TIME ZONE") == len(datetimes)
    assert compiled_tables.count(" JSON NOT NULL") == len(json_columns)
    assert "DEFERRABLE" not in compiled_tables

    partial = next(
        index
        for index in indexes
        if index.name == "audit_events_research_authorization_terminal_unique"
    )
    compiled_partial = str(CreateIndex(partial).compile(dialect=dialect))
    assert " WHERE aggregate_type = 'research_execution_authorization'" in (
        compiled_partial
    )
    locked = str(select(tables[0]).with_for_update().compile(dialect=dialect))
    assert locked.endswith(" FOR UPDATE")


def test_acl_static_validator_fails_closed_on_any_mutation() -> None:
    acl = render_postgresql_acl_sql()
    mutated = acl.replace(
        "GRANT SELECT ON TABLE strategy_platform_v13.orders TO canonical_api_reader",
        "GRANT SELECT ON ALL TABLES IN SCHEMA strategy_platform_v13 TO canonical_api_reader",
    )
    problems = postgresql_acl_problems(mutated)
    assert "ACL text differs from the exact canonical allowlist" in problems
    assert "wildcard GRANT ON ALL TABLES is forbidden" in problems


def test_execution_modules_carry_only_their_single_writer_capability() -> None:
    expected_writes = {
        deployment_approval: {"DEPLOYMENT_APPROVALS_TABLE"},
        deployment_control: {
            "DEPLOYMENTS_TABLE",
            "RUNTIME_INSTANCES_TABLE",
            "RUNTIME_RECEIPTS_TABLE",
        },
        signal_service: {"SIGNALS_TABLE"},
        risk_service: {"TRADE_INTENTS_TABLE", "RISK_DECISIONS_TABLE"},
        order_service: {"ORDERS_TABLE"},
        fill_service: {"FILLS_TABLE"},
        accounting: {"LEDGER_ENTRIES_TABLE"},
        reconciliation: {
            "RECONCILIATION_RUNS_TABLE",
            "RECONCILIATION_ITEMS_TABLE",
        },
        research_authorization: {"AUDIT_EVENTS_TABLE"},
        research_execution: set(),
    }
    pattern = re.compile(r"([A-Z][A-Z_]+_TABLE)\.(?:insert|update|delete)\(")
    for module, expected in expected_writes.items():
        source = python_inspect.getsource(module)
        observed = set(pattern.findall(source))
        assert observed == expected, module.__name__

    canonical_package = Path(canonical_models.__file__).parent
    assert not (canonical_package / "execution_chain.py").exists()
