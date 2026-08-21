from __future__ import annotations

import re
from pathlib import Path


SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "strategy_platform_v13_owner_activation_acl_repair.sql"
)

EXPECTED_READ_TABLES = {
    "configuration_types",
    "configuration_versions",
    "configuration_dependencies",
    "configuration_activations",
    "configuration_audit_events",
    "configuration_bundle_snapshots",
    "adapter_definitions",
    "execution_target_definitions",
    "execution_target_definition_versions",
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
    "strategy_platform_migration_runs",
}


def _sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def _inserted_tables(sql: str) -> set[str]:
    match = re.search(
        r"INSERT INTO _strategy_platform_v13_runtime_read_allowlist "
        r"\(table_name\) VALUES(?P<rows>.*?);",
        sql,
        re.DOTALL,
    )
    assert match is not None
    return set(re.findall(r"\('([a-z0-9_]+)'\)", match.group("rows")))


def test_acl_repair_is_exact_design_lab_v47_transaction() -> None:
    sql = _sql()
    normalized = " ".join(sql.split())

    assert sql.startswith("\\set ON_ERROR_STOP on\n")
    assert re.search(r"\nBEGIN;\n", sql)
    assert sql.rstrip().endswith("COMMIT;")
    assert "SET LOCAL lock_timeout = '5s'" in sql
    assert "SET LOCAL statement_timeout = '30s'" in sql
    assert "current_database() IS DISTINCT FROM 'freqtrade_ai_design_lab'" in sql
    assert "current_schemas(false)::text[]" in sql
    assert "current_user IS DISTINCT FROM session_user" in sql
    assert "pg_catalog.pg_get_userbyid(marker_relation.relowner)" in sql
    assert "'20260813_47'" in sql
    assert normalized.index("DO $preflight$") < normalized.index(
        "REVOKE SELECT ON TABLE public.freqtrade_ai_schema_migrations FROM freqtrade"
    )
    assert normalized.index(
        "REVOKE SELECT ON TABLE public.freqtrade_ai_schema_migrations FROM freqtrade"
    ) < normalized.index("DO $postcondition$")


def test_acl_repair_locks_exact_accepted_54_table_contract() -> None:
    sql = _sql()

    assert _inserted_tables(sql) == EXPECTED_READ_TABLES
    assert len(EXPECTED_READ_TABLES) == 54
    assert "expected 54, observed %" in sql
    assert "expected 56, observed %" in sql
    assert "BLOCKED_SCHEMA_MARKER_SELECT_DRIFT" in sql
    assert "BLOCKED_POST_REPAIR_TOTAL_SELECT_CARDINALITY" in sql


def test_acl_repair_has_one_narrow_mutation_and_never_grants() -> None:
    sql = _sql()
    statements = re.findall(
        r"^\s*(GRANT|REVOKE)\b[^;]*;",
        sql,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    assert statements == ["REVOKE"]
    assert sql.count(
        "REVOKE SELECT ON TABLE public.freqtrade_ai_schema_migrations FROM freqtrade;"
    ) == 1
    assert not re.search(r"^\s*GRANT\b", sql, re.IGNORECASE | re.MULTILINE)
    assert "ON ALL TABLES" not in sql.upper()
    assert "ALL TABLES IN SCHEMA" not in sql.upper()
    assert "ALTER DEFAULT PRIVILEGES" not in sql.upper()
    assert "CREATE ROLE" not in sql.upper()
    assert "ALTER ROLE" not in sql.upper()


def test_acl_repair_requires_terminal_forward_only_run_and_empty_secrets() -> None:
    sql = _sql()
    normalized = " ".join(sql.split())

    assert "migration_key = 'strategy-platform-v13-task1-real-data-v1'" in sql
    assert "run.execution_scope = 'DESIGN_LAB'" in sql
    assert "latest_run.status IS DISTINCT FROM 'SUCCEEDED'" in sql
    assert "latest_run.target_schema_version IS DISTINCT FROM '20260813_47'" in sql
    for field in (
        "destructive_write_count",
        "overwritten_row_count",
        "deleted_row_count",
    ):
        assert f"latest_run.{field} IS DISTINCT FROM 0" in sql
    assert "run.status IN ('PLANNED', 'RUNNING', 'RECONCILING')" in sql
    for table_name in (
        "okx_demo_attestation_secrets",
        "okx_demo_operator_consent_secrets",
    ):
        assert normalized.count(
            f"SELECT count(*) INTO observed_count FROM public.{table_name};"
        ) == 1
        assert f"SELECT * FROM public.{table_name}" not in normalized


def test_acl_repair_fails_closed_on_role_and_acl_unknowns() -> None:
    sql = _sql()

    for attribute in (
        "rolcanlogin IS NOT TRUE",
        "rolsuper IS NOT FALSE",
        "rolcreaterole IS NOT FALSE",
        "rolcreatedb IS NOT FALSE",
        "rolreplication IS NOT FALSE",
        "rolbypassrls IS NOT FALSE",
    ):
        assert attribute in sql
    assert "member = runtime_role.oid OR roleid = runtime_role.oid" in sql
    assert "has_schema_privilege('freqtrade', 'public', 'CREATE')" in sql
    assert "has_database_privilege('freqtrade', current_database(), 'CREATE')" in sql
    assert "BLOCKED_UNKNOWN_RUNTIME_OR_PUBLIC_TABLE_ACL" in sql
    assert "BLOCKED_UNKNOWN_RUNTIME_OR_PUBLIC_COLUMN_ACL" in sql
    assert "BLOCKED_RUNTIME_TABLE_OWNERSHIP_OR_WRITE_PRIVILEGE" in sql
    assert "BLOCKED_RUNTIME_OR_PUBLIC_SEQUENCE_PRIVILEGE" in sql
    assert "BLOCKED_RUNTIME_OR_PUBLIC_DEFAULT_ACL" in sql
    assert sql.count("relation.relkind IN ('r', 'p', 'v', 'm', 'f')") == 8
    assert "pg_catalog.pg_default_acl" in sql
    assert "default_acl.defaclobjtype IN ('r', 'S', 'f')" in sql
    assert "acl.grantee IN (0, runtime_role.oid)" in sql
    assert "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'" in sql
    assert "acl.privilege_type IN ('USAGE', 'SELECT', 'UPDATE')" in sql


def test_acl_repair_postcondition_rechecks_no_runtime_expansion() -> None:
    sql = _sql()

    assert "BLOCKED_SCHEMA_MARKER_SELECT_REMAINS" in sql
    assert "BLOCKED_POST_REPAIR_READ_ACL_CARDINALITY" in sql
    assert "BLOCKED_POST_REPAIR_UNKNOWN_TABLE_ACL" in sql
    assert "BLOCKED_POST_REPAIR_UNKNOWN_COLUMN_ACL" in sql
    assert "BLOCKED_POST_REPAIR_TABLE_OWNERSHIP_OR_WRITE_PRIVILEGE" in sql
    assert "BLOCKED_POST_REPAIR_SEQUENCE_PRIVILEGE" in sql
    assert "BLOCKED_POST_REPAIR_DEFAULT_ACL" in sql
    assert "BLOCKED_POST_REPAIR_RUNTIME_CREATE_PRIVILEGE" in sql
