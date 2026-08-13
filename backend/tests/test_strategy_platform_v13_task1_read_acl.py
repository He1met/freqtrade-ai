from __future__ import annotations

import re
from pathlib import Path


SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "strategy_platform_v13_task1_read_acl.sql"
)

READ_TABLES = {
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

OWNER_ONLY_TABLES = {
    "strategy_targets",
    "validation_window_scores",
    "validation_window_score_components",
    "quality_rule_evaluations",
    "strategy_evaluation_summaries",
    "strategy_submissions",
    "strategy_runtime_instances",
    "strategy_position_ledger_entries",
    "strategy_position_reconciliation_items",
    "market_data_file_records",
    "market_data_update_jobs",
    "market_data_update_items",
    "optimization_runs",
    "optimization_trials",
    "strategy_platform_migration_table_snapshots",
    "strategy_platform_migration_entity_mappings",
    "strategy_platform_migration_conflicts",
}

LEGACY_CAPABILITY_TABLES = {
    "approved_executions",
    "backtest_results",
    "backtest_runs",
    "backtest_tasks",
    "exchange_fills",
    "exchange_orders",
    "exchange_positions",
    "execution_manifests",
    "execution_scopes",
    "full_chain_runs",
    "full_chain_signal_snapshots",
    "full_chain_stage_runs",
    "local_test_batches",
    "local_test_db_events",
    "market_data_quality_receipts",
    "okx_demo_accepted_not_found_terminalizations",
    "okx_demo_account_snapshots",
    "okx_demo_attestation_secrets",
    "okx_demo_attested_sessions",
    "okx_demo_automation_guard_events",
    "okx_demo_automation_guard_states",
    "okx_demo_canary_consent_handoffs",
    "okx_demo_canary_lifecycles",
    "okx_demo_exchange_events",
    "okx_demo_fill_snapshots",
    "okx_demo_operator_consent_secrets",
    "okx_demo_order_snapshots",
    "okx_demo_position_snapshots",
    "okx_demo_reconciliation_states",
    "okx_demo_recovery_batches",
    "okx_demo_recovery_grants",
    "okx_demo_soak_events",
    "okx_demo_soak_probes",
    "okx_demo_soak_runs",
    "okx_demo_submission_grants",
    "okx_demo_trusted_snapshots",
    "okx_order_write_attempts",
    "okx_order_writer_leases",
    "reconciliation_runs",
    "research_job_attempts",
    "research_jobs",
    "research_worker_control",
    "risk_budgets",
    "risk_decisions",
    "signal_evaluations",
    "strategies",
    "strategy_candidate_approvals",
    "strategy_deployments",
    "strategy_failure_reasons",
    "strategy_generation_runs",
    "strategy_research_attempt_events",
    "strategy_research_batches",
    "strategy_research_candidate_bridge_events",
    "strategy_research_candidates",
    "strategy_scores",
    "strategy_validation_plans",
    "strategy_validation_windows",
    "strategy_versions",
    "trade_intents",
}

LEGACY_CAPABILITY_SEQUENCES = {
    "approved_executions_id_seq",
    "backtest_results_id_seq",
    "backtest_runs_id_seq",
    "backtest_tasks_id_seq",
    "exchange_fills_id_seq",
    "exchange_orders_id_seq",
    "exchange_positions_id_seq",
    "execution_manifests_id_seq",
    "full_chain_runs_id_seq",
    "full_chain_signal_snapshots_id_seq",
    "full_chain_stage_runs_id_seq",
    "local_test_batches_id_seq",
    "local_test_db_events_id_seq",
    "market_data_quality_receipts_id_seq",
    "okx_demo_accepted_not_found_terminalizations_id_seq",
    "okx_demo_account_snapshots_database_id_seq",
    "okx_demo_automation_guard_events_id_seq",
    "okx_demo_exchange_events_database_id_seq",
    "okx_demo_fill_snapshots_database_id_seq",
    "okx_demo_order_snapshots_database_id_seq",
    "okx_demo_position_snapshots_database_id_seq",
    "okx_demo_reconciliation_states_database_id_seq",
    "okx_demo_recovery_batches_database_id_seq",
    "okx_demo_recovery_grants_database_id_seq",
    "okx_demo_soak_events_id_seq",
    "okx_demo_soak_probes_id_seq",
    "okx_demo_soak_runs_id_seq",
    "okx_demo_trusted_snapshots_database_id_seq",
    "okx_order_write_attempts_id_seq",
    "reconciliation_runs_id_seq",
    "research_job_attempts_id_seq",
    "research_jobs_id_seq",
    "risk_decisions_id_seq",
    "signal_evaluations_id_seq",
    "strategies_id_seq",
    "strategy_candidate_approvals_id_seq",
    "strategy_deployments_id_seq",
    "strategy_failure_reasons_id_seq",
    "strategy_generation_runs_id_seq",
    "strategy_research_attempt_events_id_seq",
    "strategy_research_batches_id_seq",
    "strategy_research_candidate_bridge_events_id_seq",
    "strategy_research_candidates_id_seq",
    "strategy_scores_id_seq",
    "strategy_validation_plans_id_seq",
    "strategy_validation_windows_id_seq",
    "strategy_versions_id_seq",
    "trade_intents_id_seq",
}

LEGACY_CAPABILITY_FUNCTIONS = {
    ("apply_okx_demo_reconciliation_gate", "bigint"),
    ("bridge_okx_demo_managed_fill", "bigint"),
    ("can_resume_okx_demo_canary_recovery", "bigint"),
    ("claim_atomic_okx_demo_canary_dispatch", "bigint,text,text,bigint,text"),
    ("claim_okx_demo_canary_consent", "text,text,bigint,jsonb"),
    ("claim_okx_demo_continuous_dispatch", "bigint,text"),
    (
        "commit_atomic_okx_demo_canary_prepare",
        "text,text,bigint,bigint,bigint,bigint,jsonb,jsonb,jsonb",
    ),
    (
        "create_okx_demo_canary_cleanup_intent",
        "character varying,bigint,bigint",
    ),
    ("create_okx_demo_canary_lifecycle", "character varying"),
    ("create_okx_demo_canary_lineage", "jsonb"),
    ("eligible_atomic_okx_demo_canary_predecessor", ""),
    ("fail_okx_demo_canary_grant_before_prepare", "text"),
    ("fail_requested_okx_demo_canary_consent", "text,text,text"),
    (
        "finalize_okx_demo_canary_consent",
        "text,text,bigint,bigint,bigint,bigint,jsonb",
    ),
    ("finalize_okx_demo_reconciliation_run", "bigint,jsonb,jsonb,text,text"),
    ("finalized_okx_demo_canary_consent", "text"),
    ("freeze_okx_demo_reconciliation_gate", "text,text,timestamp with time zone"),
    (
        "issue_okx_demo_canary_recovery_grant",
        "character varying,bigint,text,bigint",
    ),
    ("issue_okx_demo_submission_grant", "jsonb"),
    ("lock_okx_demo_reconciliation_state", ""),
    ("okx_demo_canary_consent_eligibility", ""),
    ("okx_demo_continuous_opening_allowed", "text"),
    ("pending_okx_demo_canary_consent", ""),
    ("persist_okx_demo_natural_risk_chain", "jsonb"),
    ("prepare_okx_demo_canary_residual_child", "bigint,bigint"),
    ("record_okx_demo_automation_failure", "text,text,bigint,text"),
    ("record_okx_demo_automation_health", "bigint,text"),
    ("release_expired_okx_demo_approval", "bigint"),
    ("request_atomic_okx_demo_canary_consent", "text,text,text,text"),
    ("request_okx_demo_canary_consent", "text,text,text,text"),
    ("revoke_okx_demo_attested_session", "text,text,text,bigint"),
    ("revoke_restarted_okx_demo_canary_grant", "text,text"),
    ("settle_okx_demo_canary_handoff", "text"),
    (
        "transition_okx_demo_canary_lifecycle",
        "character varying,text,bigint,bigint,character varying,bigint",
    ),
    (
        "validate_atomic_okx_demo_dispatch_authority",
        "bigint,text,text,bigint,text,text",
    ),
    ("write_okx_demo_attested_session", "text,text,text,bigint,bigint,text,text"),
    (
        "write_okx_demo_trusted_snapshot",
        "text,text,text,text,jsonb,text,timestamp with time zone,timestamp with time zone",
    ),
}

LEGACY_ACL_GRANTEES = {"freqtrade", "freqtrade_ai_attestor"}

GUARD_FUNCTIONS = {
    "guard_configuration_version",
    "guard_configuration_activation",
    "guard_configuration_bundle_snapshot",
    "guard_configuration_dependency",
    "prevent_strategy_platform_mutation",
    "guard_configuration_child",
    "guard_strategy_validation_plan",
    "guard_strategy_validation_window",
    "guard_strategy_platform_migration_audit",
    "guard_strategy_platform_v13_bundle_required",
    "guard_strategy_platform_qualified_mapping",
    "guard_strategy_platform_v13_config_child",
    "guard_strategy_submission_payload",
}


def _inserted_values(sql: str, table_name: str) -> set[str]:
    match = re.search(
        rf"INSERT INTO {re.escape(table_name)} \([^;]+?\) VALUES(?P<rows>.*?);",
        sql,
        re.DOTALL,
    )
    assert match is not None
    return set(re.findall(r"\('([a-z0-9_]+)'\)", match.group("rows")))


def _inserted_function_signatures(
    sql: str, table_name: str
) -> list[tuple[str, str]]:
    match = re.search(
        rf"INSERT INTO {re.escape(table_name)} \([^;]+?\) VALUES(?P<rows>.*?);",
        sql,
        re.DOTALL,
    )
    assert match is not None
    return re.findall(
        r"\(\s*'([a-z0-9_]+)'\s*,\s*'([^']*)'\s*\)",
        match.group("rows"),
        re.DOTALL,
    )


def test_read_acl_uses_exact_v13_object_sets() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")

    assert _inserted_values(
        sql, "_strategy_platform_v13_read_acl_allowlist"
    ) == READ_TABLES
    assert _inserted_values(
        sql, "_strategy_platform_v13_owner_only_tables"
    ) == OWNER_ONLY_TABLES
    assert _inserted_values(
        sql, "_strategy_platform_v13_guard_functions"
    ) == GUARD_FUNCTIONS
    assert len(READ_TABLES) == 54
    assert len(OWNER_ONLY_TABLES) == 17
    assert READ_TABLES.isdisjoint(OWNER_ONLY_TABLES)


def test_read_acl_uses_exact_legacy_capability_object_sets() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    function_signatures = _inserted_function_signatures(
        sql, "_strategy_platform_v13_legacy_capability_functions"
    )

    assert _inserted_values(
        sql, "_strategy_platform_v13_legacy_capability_tables"
    ) == LEGACY_CAPABILITY_TABLES
    assert _inserted_values(
        sql, "_strategy_platform_v13_legacy_capability_sequences"
    ) == LEGACY_CAPABILITY_SEQUENCES
    assert _inserted_values(
        sql, "_strategy_platform_v13_legacy_acl_grantees"
    ) == LEGACY_ACL_GRANTEES
    assert set(function_signatures) == LEGACY_CAPABILITY_FUNCTIONS
    assert len(LEGACY_CAPABILITY_TABLES) == 59
    assert len(LEGACY_CAPABILITY_SEQUENCES) == 48
    assert len(function_signatures) == len(LEGACY_CAPABILITY_FUNCTIONS) == 37
    assert LEGACY_CAPABILITY_TABLES.isdisjoint(READ_TABLES)
    assert LEGACY_CAPABILITY_TABLES.isdisjoint(OWNER_ONLY_TABLES)


def test_read_acl_is_design_lab_only_and_transactional() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    assert sql.startswith("\\set ON_ERROR_STOP on\n")
    assert re.search(r"\nBEGIN;\n", sql)
    assert sql.rstrip().endswith("COMMIT;")
    assert "current_database() IS DISTINCT FROM 'freqtrade_ai_design_lab'" in sql
    assert "current_schemas(false)::text[]" in sql
    assert "ARRAY['public']::text[]" in sql
    assert "current_user IS DISTINCT FROM session_user" in sql
    assert "20260813_47" in sql
    assert "expected 71 tables" in sql
    assert "Every safety and object-identity fact is checked before" in sql
    assert normalized.index("DO $preflight$") < normalized.index("DO $grant_read_acl$")
    assert normalized.index("DO $grant_read_acl$") < normalized.index(
        "DO $revoke_legacy_capabilities$"
    )
    assert normalized.index("DO $revoke_legacy_capabilities$") < normalized.index(
        "DO $postcondition$"
    )


def test_read_acl_fails_closed_on_runtime_role_and_membership() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")

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
    assert "member = runtime_oid OR roleid = runtime_oid" in sql
    assert "has_schema_privilege('freqtrade', 'public', 'CREATE')" in sql
    assert "has_database_privilege('freqtrade', current_database(), 'CREATE')" in sql
    assert "has_schema_privilege('freqtrade', 'public', 'USAGE')" in sql
    assert "BLOCKED_RUNTIME_OWNS_V13_RELATION" in sql
    assert "BLOCKED_RUNTIME_ROLE_ATTRIBUTES_AFTER_GRANT" in sql


def test_read_acl_grants_only_explicit_table_select() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    upper = sql.upper()

    assert "GRANT SELECT ON TABLE %I.%I TO %I" in sql
    assert "GRANT INSERT" not in upper
    assert "GRANT UPDATE" not in upper
    assert "GRANT DELETE" not in upper
    assert "GRANT TRUNCATE" not in upper
    assert "GRANT REFERENCES" not in upper
    assert "GRANT TRIGGER" not in upper
    assert "GRANT USAGE" not in upper
    assert "GRANT EXECUTE" not in upper
    assert "ON ALL TABLES" not in upper
    assert "ALL TABLES IN SCHEMA" not in upper
    assert "ON ALL SEQUENCES" not in upper
    assert "ALL SEQUENCES IN SCHEMA" not in upper
    assert "ON ALL FUNCTIONS" not in upper
    assert "ALL FUNCTIONS IN SCHEMA" not in upper
    assert "ALTER DEFAULT PRIVILEGES" not in upper
    assert "CREATE ROLE" not in upper
    assert "ALTER ROLE" not in upper


def test_read_acl_attests_exact_direct_acl_and_no_write_or_sequence_access() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")

    assert "pg_catalog.aclexplode(relation.relacl)" in sql
    assert "acl.grantee = runtime_oid" in sql
    assert "acl.privilege_type = 'SELECT'" in sql
    assert "acl.is_grantable IS FALSE" in sql
    assert "acl.grantee IN (0, runtime_oid)" in sql
    assert (
        "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'" in sql
    )
    assert "dependency.deptype IN ('a', 'i')" in sql
    assert "acl.privilege_type IN ('USAGE', 'SELECT', 'UPDATE')" in sql
    assert "BLOCKED_RUNTIME_V13_SEQUENCE_PRIVILEGE" in sql
    assert "BLOCKED_RUNTIME_CREATE_PRIVILEGE_AFTER_GRANT" in sql
    assert "pg_catalog.aclexplode(attribute.attacl)" in sql
    assert "has_column_privilege(" in sql
    assert "BLOCKED_UNEXPECTED_DIRECT_V13_COLUMN_ACL" in sql


def test_read_acl_revokes_every_guard_function_from_public_and_runtime() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")

    assert (
        "REVOKE EXECUTE ON FUNCTION %I.%I() FROM PUBLIC, %I" in sql
    )
    assert "function_row.pronargs = 0" in sql
    assert "function_row.prosecdef IS TRUE" in sql
    assert "has_function_privilege(" in sql
    assert "BLOCKED_GUARD_FUNCTION_EXECUTE_PRIVILEGE" in sql


def test_read_acl_revokes_exact_legacy_capabilities_and_preserves_owners() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")

    assert "expected 59, observed %" in sql
    assert "expected 48, observed %" in sql
    assert "expected 37, observed %" in sql
    assert (
        "REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM PUBLIC, %I" in sql
    )
    assert (
        "REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM %I" in sql
    )
    assert (
        "REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM PUBLIC, %I" in sql
    )
    assert (
        "REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM %I" in sql
    )
    assert (
        "REVOKE EXECUTE ON FUNCTION %I.%I(%s) FROM PUBLIC, %I" in sql
    )
    assert (
        "REVOKE EXECUTE ON FUNCTION %I.%I(%s) FROM %I" in sql
    )
    assert "pg_catalog.to_regprocedure(" in sql
    assert "function_row.prosecdef IS NOT TRUE" in sql
    assert "freqtrade_ai_attestor" in sql
    assert "BLOCKED_RUNTIME_LEGACY_TABLE_PRIVILEGE" in sql
    assert "BLOCKED_NON_OWNER_LEGACY_TABLE_ACL" in sql
    assert "BLOCKED_RUNTIME_LEGACY_COLUMN_PRIVILEGE" in sql
    assert "BLOCKED_NON_OWNER_LEGACY_COLUMN_ACL" in sql
    assert "BLOCKED_RUNTIME_LEGACY_SEQUENCE_PRIVILEGE" in sql
    assert "BLOCKED_NON_OWNER_LEGACY_SEQUENCE_ACL" in sql
    assert "BLOCKED_RUNTIME_LEGACY_FUNCTION_EXECUTE" in sql
    assert "BLOCKED_NON_OWNER_LEGACY_FUNCTION_ACL" in sql
    assert "BLOCKED_LEGACY_TABLE_OWNER_PRIVILEGE" in sql
    assert "BLOCKED_LEGACY_SEQUENCE_OWNER_IDENTITY" in sql
    assert "BLOCKED_LEGACY_FUNCTION_OWNER_EXECUTE" in sql
    assert "BLOCKED_UNKNOWN_LEGACY_TABLE_ACL_GRANTEE" in sql
    assert "BLOCKED_UNKNOWN_LEGACY_SEQUENCE_ACL_GRANTEE" in sql
    assert "BLOCKED_UNKNOWN_LEGACY_FUNCTION_ACL_GRANTEE" in sql
    assert sql.count("'freqtrade_ai_attestor'") >= 7
    assert sql.count("IF item.relowner <> attestor_oid THEN") == 2
    assert "IF item.proowner <> attestor_oid THEN" in sql


def test_sequence_acl_checks_are_catalog_typed_and_planner_safe() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")

    # PostgreSQL may evaluate a has_sequence_privilege() predicate before a
    # pg_class relkind join filter.  Never invoke that relation-type-sensitive
    # helper from these catalog scans; inspect the exact sequence ACL instead.
    assert "has_sequence_privilege(" not in sql
    assert sql.count("pg_catalog.acldefault(") == 3
    assert sql.count("acl.privilege_type IN ('USAGE', 'SELECT', 'UPDATE')") == 3
    assert "sequence_relation.relowner = runtime_role.oid" in sql
    assert "sequence_relation.relowner = runtime_oid" in sql
    assert "relation.relowner = runtime_oid" in sql


def test_read_acl_checks_secret_tables_by_count_only_before_and_after() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    for table_name in (
        "okx_demo_attestation_secrets",
        "okx_demo_operator_consent_secrets",
    ):
        assert sql.count(f"FROM public.{table_name};") == 2
        assert normalized.count(
            f"SELECT count(*) INTO observed_count FROM public.{table_name};"
        ) == 2
        assert f"SELECT * FROM public.{table_name}" not in normalized


def test_read_acl_removes_table_and_column_level_runtime_capability() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")

    assert sql.count(
        "REVOKE SELECT (%s), INSERT (%s), UPDATE (%s), REFERENCES (%s)"
    ) == 3
    assert "attribute.attnum > 0" in sql
    assert "attribute.attisdropped IS FALSE" in sql
    assert "pg_catalog.aclexplode(attribute.attacl)" in sql
    assert "'SELECT,INSERT,UPDATE,REFERENCES'" in sql


def test_read_acl_forbids_default_acl_expansion() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")

    assert "pg_catalog.pg_default_acl" in sql
    assert "default_acl.defaclobjtype IN ('r', 'S', 'f')" in sql
    assert "acl.grantee IN (0, runtime_role.oid)" in sql
    assert "BLOCKED_V13_DEFAULT_ACL" in sql
