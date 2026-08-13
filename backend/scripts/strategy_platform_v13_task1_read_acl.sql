\set ON_ERROR_STOP on

-- Strategy Platform V1.3 Task 1 read-only runtime ACL.
--
-- This script is intentionally valid only for the physically isolated V1.3
-- owner database.  It grants the existing runtime role read access to the
-- immutable registry/configuration catalog and to the terminal migration-run
-- header.  It does not grant configuration writes, operational writes,
-- sequence access, schema creation, role membership, or access to credentials.
--
-- Run only with psql -X after the v47 migration and reconciliation have passed:
--   psql -X -d freqtrade_ai_design_lab \
--     -f backend/scripts/strategy_platform_v13_task1_read_acl.sql

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TEMPORARY TABLE _strategy_platform_v13_read_acl_allowlist (
    table_name name PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO _strategy_platform_v13_read_acl_allowlist (table_name) VALUES
    ('configuration_types'),
    ('configuration_versions'),
    ('configuration_dependencies'),
    ('configuration_activations'),
    ('configuration_audit_events'),
    ('configuration_bundle_snapshots'),
    ('adapter_definitions'),
    ('execution_target_definitions'),
    ('execution_target_definition_versions'),
    ('strategy_source_definitions'),
    ('strategy_source_definition_versions'),
    ('trigger_source_definitions'),
    ('trigger_source_definition_versions'),
    ('timeframe_definitions'),
    ('timeframe_definition_versions'),
    ('research_target_config_sets'),
    ('research_target_configs'),
    ('strategy_family_definitions'),
    ('strategy_family_definition_versions'),
    ('provider_model_config_versions'),
    ('generation_profile_versions'),
    ('generation_profile_families'),
    ('scoring_profile_versions'),
    ('scoring_rules'),
    ('diversity_profile_versions'),
    ('diversity_rules'),
    ('worker_execution_profile_versions'),
    ('scheduler_profile_versions'),
    ('market_data_policy_versions'),
    ('evidence_freshness_profile_versions'),
    ('evidence_freshness_rules'),
    ('monitoring_profile_versions'),
    ('promotion_profile_versions'),
    ('promotion_rules'),
    ('risk_profile_versions'),
    ('risk_rules'),
    ('capacity_profile_versions'),
    ('runtime_profile_versions'),
    ('deployment_profile_versions'),
    ('market_data_profile_versions'),
    ('optimization_profile_versions'),
    ('ui_presentation_profile_versions'),
    ('research_profile_versions'),
    ('validation_window_config_sets'),
    ('validation_window_purposes'),
    ('market_regime_definitions'),
    ('validation_window_configs'),
    ('validation_window_expectations'),
    ('metric_definitions'),
    ('metric_definition_versions'),
    ('quality_gate_profiles'),
    ('quality_gate_profile_versions'),
    ('quality_gate_rules'),
    ('strategy_platform_migration_runs');

-- V1.3 tables outside the read allowlist remain owner-only.  The list is
-- explicit so future schema additions cannot inherit an accidental grant.
CREATE TEMPORARY TABLE _strategy_platform_v13_owner_only_tables (
    table_name name PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO _strategy_platform_v13_owner_only_tables (table_name) VALUES
    ('strategy_targets'),
    ('validation_window_scores'),
    ('validation_window_score_components'),
    ('quality_rule_evaluations'),
    ('strategy_evaluation_summaries'),
    ('strategy_submissions'),
    ('strategy_runtime_instances'),
    ('strategy_position_ledger_entries'),
    ('strategy_position_reconciliation_items'),
    ('market_data_file_records'),
    ('market_data_update_jobs'),
    ('market_data_update_items'),
    ('optimization_runs'),
    ('optimization_trials'),
    ('strategy_platform_migration_table_snapshots'),
    ('strategy_platform_migration_entity_mappings'),
    ('strategy_platform_migration_conflicts');

-- The isolated V1.3 database preserves legacy history, but the abandoned
-- runtime must retain no direct capability over that history.  This exact set
-- is the union of every legacy table that carried a direct `freqtrade` ACL in
-- the source and every legacy OKX capability/secret table.  Owner access is
-- preserved; no replacement runtime grant is installed.
CREATE TEMPORARY TABLE _strategy_platform_v13_legacy_capability_tables (
    table_name name PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO _strategy_platform_v13_legacy_capability_tables (table_name) VALUES
    ('approved_executions'),
    ('backtest_results'),
    ('backtest_runs'),
    ('backtest_tasks'),
    ('exchange_fills'),
    ('exchange_orders'),
    ('exchange_positions'),
    ('execution_manifests'),
    ('execution_scopes'),
    ('full_chain_runs'),
    ('full_chain_signal_snapshots'),
    ('full_chain_stage_runs'),
    ('local_test_batches'),
    ('local_test_db_events'),
    ('market_data_quality_receipts'),
    ('okx_demo_accepted_not_found_terminalizations'),
    ('okx_demo_account_snapshots'),
    ('okx_demo_attestation_secrets'),
    ('okx_demo_attested_sessions'),
    ('okx_demo_automation_guard_events'),
    ('okx_demo_automation_guard_states'),
    ('okx_demo_canary_consent_handoffs'),
    ('okx_demo_canary_lifecycles'),
    ('okx_demo_exchange_events'),
    ('okx_demo_fill_snapshots'),
    ('okx_demo_operator_consent_secrets'),
    ('okx_demo_order_snapshots'),
    ('okx_demo_position_snapshots'),
    ('okx_demo_reconciliation_states'),
    ('okx_demo_recovery_batches'),
    ('okx_demo_recovery_grants'),
    ('okx_demo_soak_events'),
    ('okx_demo_soak_probes'),
    ('okx_demo_soak_runs'),
    ('okx_demo_submission_grants'),
    ('okx_demo_trusted_snapshots'),
    ('okx_order_write_attempts'),
    ('okx_order_writer_leases'),
    ('reconciliation_runs'),
    ('research_job_attempts'),
    ('research_jobs'),
    ('research_worker_control'),
    ('risk_budgets'),
    ('risk_decisions'),
    ('signal_evaluations'),
    ('strategies'),
    ('strategy_candidate_approvals'),
    ('strategy_deployments'),
    ('strategy_failure_reasons'),
    ('strategy_generation_runs'),
    ('strategy_research_attempt_events'),
    ('strategy_research_batches'),
    ('strategy_research_candidate_bridge_events'),
    ('strategy_research_candidates'),
    ('strategy_scores'),
    ('strategy_validation_plans'),
    ('strategy_validation_windows'),
    ('strategy_versions'),
    ('trade_intents');

CREATE TEMPORARY TABLE _strategy_platform_v13_legacy_capability_sequences (
    sequence_name name PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO _strategy_platform_v13_legacy_capability_sequences (sequence_name) VALUES
    ('approved_executions_id_seq'),
    ('backtest_results_id_seq'),
    ('backtest_runs_id_seq'),
    ('backtest_tasks_id_seq'),
    ('exchange_fills_id_seq'),
    ('exchange_orders_id_seq'),
    ('exchange_positions_id_seq'),
    ('execution_manifests_id_seq'),
    ('full_chain_runs_id_seq'),
    ('full_chain_signal_snapshots_id_seq'),
    ('full_chain_stage_runs_id_seq'),
    ('local_test_batches_id_seq'),
    ('local_test_db_events_id_seq'),
    ('market_data_quality_receipts_id_seq'),
    ('okx_demo_accepted_not_found_terminalizations_id_seq'),
    ('okx_demo_account_snapshots_database_id_seq'),
    ('okx_demo_automation_guard_events_id_seq'),
    ('okx_demo_exchange_events_database_id_seq'),
    ('okx_demo_fill_snapshots_database_id_seq'),
    ('okx_demo_order_snapshots_database_id_seq'),
    ('okx_demo_position_snapshots_database_id_seq'),
    ('okx_demo_reconciliation_states_database_id_seq'),
    ('okx_demo_recovery_batches_database_id_seq'),
    ('okx_demo_recovery_grants_database_id_seq'),
    ('okx_demo_soak_events_id_seq'),
    ('okx_demo_soak_probes_id_seq'),
    ('okx_demo_soak_runs_id_seq'),
    ('okx_demo_trusted_snapshots_database_id_seq'),
    ('okx_order_write_attempts_id_seq'),
    ('reconciliation_runs_id_seq'),
    ('research_job_attempts_id_seq'),
    ('research_jobs_id_seq'),
    ('risk_decisions_id_seq'),
    ('signal_evaluations_id_seq'),
    ('strategies_id_seq'),
    ('strategy_candidate_approvals_id_seq'),
    ('strategy_deployments_id_seq'),
    ('strategy_failure_reasons_id_seq'),
    ('strategy_generation_runs_id_seq'),
    ('strategy_research_attempt_events_id_seq'),
    ('strategy_research_batches_id_seq'),
    ('strategy_research_candidate_bridge_events_id_seq'),
    ('strategy_research_candidates_id_seq'),
    ('strategy_scores_id_seq'),
    ('strategy_validation_plans_id_seq'),
    ('strategy_validation_windows_id_seq'),
    ('strategy_versions_id_seq'),
    ('trade_intents_id_seq');

-- Exact signatures of the restored legacy SECURITY DEFINER execution surface.
-- Function bodies and credential values are never read by this script.
CREATE TEMPORARY TABLE _strategy_platform_v13_legacy_capability_functions (
    function_name name NOT NULL,
    identity_arguments text NOT NULL,
    PRIMARY KEY (function_name, identity_arguments)
) ON COMMIT DROP;

INSERT INTO _strategy_platform_v13_legacy_capability_functions (
    function_name, identity_arguments
) VALUES
    ('apply_okx_demo_reconciliation_gate', 'bigint'),
    ('bridge_okx_demo_managed_fill', 'bigint'),
    ('can_resume_okx_demo_canary_recovery', 'bigint'),
    ('claim_atomic_okx_demo_canary_dispatch', 'bigint,text,text,bigint,text'),
    ('claim_okx_demo_canary_consent', 'text,text,bigint,jsonb'),
    ('claim_okx_demo_continuous_dispatch', 'bigint,text'),
    (
        'commit_atomic_okx_demo_canary_prepare',
        'text,text,bigint,bigint,bigint,bigint,jsonb,jsonb,jsonb'
    ),
    ('create_okx_demo_canary_cleanup_intent', 'character varying,bigint,bigint'),
    ('create_okx_demo_canary_lifecycle', 'character varying'),
    ('create_okx_demo_canary_lineage', 'jsonb'),
    ('eligible_atomic_okx_demo_canary_predecessor', ''),
    ('fail_okx_demo_canary_grant_before_prepare', 'text'),
    ('fail_requested_okx_demo_canary_consent', 'text,text,text'),
    (
        'finalize_okx_demo_canary_consent',
        'text,text,bigint,bigint,bigint,bigint,jsonb'
    ),
    ('finalize_okx_demo_reconciliation_run', 'bigint,jsonb,jsonb,text,text'),
    ('finalized_okx_demo_canary_consent', 'text'),
    ('freeze_okx_demo_reconciliation_gate', 'text,text,timestamp with time zone'),
    (
        'issue_okx_demo_canary_recovery_grant',
        'character varying,bigint,text,bigint'
    ),
    ('issue_okx_demo_submission_grant', 'jsonb'),
    ('lock_okx_demo_reconciliation_state', ''),
    ('okx_demo_canary_consent_eligibility', ''),
    ('okx_demo_continuous_opening_allowed', 'text'),
    ('pending_okx_demo_canary_consent', ''),
    ('persist_okx_demo_natural_risk_chain', 'jsonb'),
    ('prepare_okx_demo_canary_residual_child', 'bigint,bigint'),
    ('record_okx_demo_automation_failure', 'text,text,bigint,text'),
    ('record_okx_demo_automation_health', 'bigint,text'),
    ('release_expired_okx_demo_approval', 'bigint'),
    ('request_atomic_okx_demo_canary_consent', 'text,text,text,text'),
    ('request_okx_demo_canary_consent', 'text,text,text,text'),
    ('revoke_okx_demo_attested_session', 'text,text,text,bigint'),
    ('revoke_restarted_okx_demo_canary_grant', 'text,text'),
    ('settle_okx_demo_canary_handoff', 'text'),
    (
        'transition_okx_demo_canary_lifecycle',
        'character varying,text,bigint,bigint,character varying,bigint'
    ),
    (
        'validate_atomic_okx_demo_dispatch_authority',
        'bigint,text,text,bigint,text,text'
    ),
    ('write_okx_demo_attested_session', 'text,text,text,bigint,bigint,text,text'),
    (
        'write_okx_demo_trusted_snapshot',
        'text,text,text,text,jsonb,text,timestamp with time zone,timestamp with time zone'
    );

-- Exact restored non-owner role surface observed in the source catalog.  The
-- runtime role is validated separately above; this owner role must lose only
-- ACLs on objects it does not own.  Any other restored grantee blocks before
-- the first ACL mutation instead of being discovered and revoked dynamically.
CREATE TEMPORARY TABLE _strategy_platform_v13_legacy_acl_grantees (
    role_name name PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO _strategy_platform_v13_legacy_acl_grantees (role_name) VALUES
    ('freqtrade'),
    ('freqtrade_ai_attestor');

CREATE TEMPORARY TABLE _strategy_platform_v13_guard_functions (
    function_name name PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO _strategy_platform_v13_guard_functions (function_name) VALUES
    ('guard_configuration_version'),
    ('guard_configuration_activation'),
    ('guard_configuration_bundle_snapshot'),
    ('guard_configuration_dependency'),
    ('prevent_strategy_platform_mutation'),
    ('guard_configuration_child'),
    ('guard_strategy_validation_plan'),
    ('guard_strategy_validation_window'),
    ('guard_strategy_platform_migration_audit'),
    ('guard_strategy_platform_v13_bundle_required'),
    ('guard_strategy_platform_qualified_mapping'),
    ('guard_strategy_platform_v13_config_child'),
    ('guard_strategy_submission_payload');

-- Every safety and object-identity fact is checked before the first ACL write.
DO $preflight$
DECLARE
    runtime_role pg_catalog.pg_roles%ROWTYPE;
    attestor_role pg_catalog.pg_roles%ROWTYPE;
    applying_role pg_catalog.pg_roles%ROWTYPE;
    observed_version text;
    observed_count bigint;
BEGIN
    IF current_database() IS DISTINCT FROM 'freqtrade_ai_design_lab' THEN
        RAISE EXCEPTION
            'BLOCKED_WRONG_DATABASE: expected freqtrade_ai_design_lab, observed %',
            current_database();
    END IF;
    IF current_schema() IS DISTINCT FROM 'public'
       OR current_schemas(false)::text[] IS DISTINCT FROM ARRAY['public']::text[] THEN
        RAISE EXCEPTION
            'BLOCKED_UNEXPECTED_SCHEMA_PATH: expected only public, observed %',
            current_schemas(false);
    END IF;
    IF current_user IS DISTINCT FROM session_user THEN
        RAISE EXCEPTION
            'BLOCKED_SET_ROLE_SESSION: current_user % differs from session_user %',
            current_user, session_user;
    END IF;
    SELECT * INTO STRICT applying_role
      FROM pg_catalog.pg_roles
     WHERE rolname = current_user;

    SELECT * INTO runtime_role
      FROM pg_catalog.pg_roles
     WHERE rolname = 'freqtrade';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_ROLE_MISSING: freqtrade';
    END IF;
    IF runtime_role.rolcanlogin IS NOT TRUE
       OR runtime_role.rolsuper IS NOT FALSE
       OR runtime_role.rolcreaterole IS NOT FALSE
       OR runtime_role.rolcreatedb IS NOT FALSE
       OR runtime_role.rolreplication IS NOT FALSE
       OR runtime_role.rolbypassrls IS NOT FALSE THEN
        RAISE EXCEPTION
            'BLOCKED_RUNTIME_ROLE_ATTRIBUTES: freqtrade is not the expected least-privilege LOGIN role';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_auth_members
         WHERE member = runtime_role.oid OR roleid = runtime_role.oid
    ) THEN
        RAISE EXCEPTION
            'BLOCKED_RUNTIME_ROLE_MEMBERSHIP: freqtrade must have no inherited or delegated memberships';
    END IF;

    SELECT * INTO attestor_role
      FROM pg_catalog.pg_roles
     WHERE rolname = 'freqtrade_ai_attestor';
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'BLOCKED_ATTESTOR_ROLE_MISSING: freqtrade_ai_attestor';
    END IF;
    IF attestor_role.rolcanlogin IS NOT FALSE
       OR attestor_role.rolsuper IS NOT FALSE
       OR attestor_role.rolcreaterole IS NOT FALSE
       OR attestor_role.rolcreatedb IS NOT FALSE
       OR attestor_role.rolreplication IS NOT FALSE
       OR attestor_role.rolbypassrls IS NOT FALSE THEN
        RAISE EXCEPTION
            'BLOCKED_ATTESTOR_ROLE_ATTRIBUTES: expected least-privilege NOLOGIN owner';
    END IF;
    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_legacy_acl_grantees expected
      JOIN pg_catalog.pg_roles role_row
        ON role_row.rolname = expected.role_name;
    IF observed_count IS DISTINCT FROM 2::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_LEGACY_ACL_GRANTEE_SET: expected 2 roles, observed %',
            observed_count;
    END IF;
    IF has_schema_privilege('freqtrade', 'public', 'CREATE')
       OR has_database_privilege('freqtrade', current_database(), 'CREATE') THEN
        RAISE EXCEPTION
            'BLOCKED_RUNTIME_CREATE_PRIVILEGE: freqtrade must not create schema objects';
    END IF;
    IF NOT has_schema_privilege('freqtrade', 'public', 'USAGE') THEN
        RAISE EXCEPTION
            'BLOCKED_RUNTIME_SCHEMA_USAGE: freqtrade cannot resolve allowlisted tables';
    END IF;

    IF pg_catalog.to_regclass('public.freqtrade_ai_schema_migrations') IS NULL THEN
        RAISE EXCEPTION 'BLOCKED_SCHEMA_MARKER_MISSING';
    END IF;
    SELECT version INTO observed_version
      FROM public.freqtrade_ai_schema_migrations
     ORDER BY applied_at DESC, version DESC
     LIMIT 1;
    IF observed_version IS DISTINCT FROM '20260813_47' THEN
        RAISE EXCEPTION
            'BLOCKED_SCHEMA_VERSION: expected 20260813_47, observed %',
            observed_version;
    END IF;

    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_read_acl_allowlist;
    IF observed_count IS DISTINCT FROM 54::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_READ_ALLOWLIST_CARDINALITY: expected 54, observed %',
            observed_count;
    END IF;
    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_owner_only_tables;
    IF observed_count IS DISTINCT FROM 17::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_OWNER_ONLY_CARDINALITY: expected 17, observed %',
            observed_count;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_read_acl_allowlist allowed
          JOIN pg_temp._strategy_platform_v13_owner_only_tables denied
            USING (table_name)
    ) THEN
        RAISE EXCEPTION 'BLOCKED_ACL_ALLOWLIST_OVERLAP';
    END IF;

    SELECT count(*) INTO observed_count
      FROM (
          SELECT table_name
            FROM pg_temp._strategy_platform_v13_read_acl_allowlist
          UNION ALL
          SELECT table_name
            FROM pg_temp._strategy_platform_v13_owner_only_tables
      ) expected
      JOIN pg_catalog.pg_class relation
        ON relation.relname = expected.table_name
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = relation.relnamespace
       AND namespace.nspname = 'public'
     WHERE relation.relkind IN ('r', 'p');
    IF observed_count IS DISTINCT FROM 71::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_V13_RELATION_SET: expected 71 tables, observed %',
            observed_count;
    END IF;

    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_legacy_capability_tables expected
      JOIN pg_catalog.pg_class relation
        ON relation.relname = expected.table_name
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = relation.relnamespace
       AND namespace.nspname = 'public'
     WHERE relation.relkind IN ('r', 'p');
    IF observed_count IS DISTINCT FROM 59::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_LEGACY_CAPABILITY_TABLE_SET: expected 59, observed %',
            observed_count;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables legacy
          JOIN pg_temp._strategy_platform_v13_read_acl_allowlist allowed
            USING (table_name)
    ) OR EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables legacy
          JOIN pg_temp._strategy_platform_v13_owner_only_tables denied
            USING (table_name)
    ) THEN
        RAISE EXCEPTION 'BLOCKED_LEGACY_V13_ACL_OVERLAP';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE relation.relowner = runtime_role.oid
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_OWNS_LEGACY_CAPABILITY_TABLE';
    END IF;
    IF applying_role.rolsuper IS NOT TRUE AND EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE pg_get_userbyid(relation.relowner) IS DISTINCT FROM current_user
    ) THEN
        RAISE EXCEPTION
            'BLOCKED_APPLYING_ROLE_CANNOT_REVOKE_LEGACY_CAPABILITIES';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) acl
         WHERE acl.grantee <> relation.relowner
           AND acl.grantee NOT IN (0, runtime_role.oid, attestor_role.oid)
    ) OR EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          JOIN pg_catalog.pg_attribute attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum > 0
           AND attribute.attisdropped IS FALSE
          CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) acl
         WHERE acl.grantee <> relation.relowner
           AND acl.grantee NOT IN (0, runtime_role.oid, attestor_role.oid)
    ) THEN
        RAISE EXCEPTION 'BLOCKED_UNKNOWN_LEGACY_TABLE_ACL_GRANTEE';
    END IF;

    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_legacy_capability_sequences expected
      JOIN pg_catalog.pg_class relation
        ON relation.relname = expected.sequence_name
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = relation.relnamespace
       AND namespace.nspname = 'public'
     WHERE relation.relkind = 'S';
    IF observed_count IS DISTINCT FROM 48::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_LEGACY_CAPABILITY_SEQUENCE_SET: expected 48, observed %',
            observed_count;
    END IF;
    SELECT count(DISTINCT sequence_relation.oid) INTO observed_count
      FROM pg_temp._strategy_platform_v13_legacy_capability_sequences expected_sequence
      JOIN pg_catalog.pg_class sequence_relation
        ON sequence_relation.relname = expected_sequence.sequence_name
       AND sequence_relation.relkind = 'S'
      JOIN pg_catalog.pg_namespace sequence_namespace
        ON sequence_namespace.oid = sequence_relation.relnamespace
       AND sequence_namespace.nspname = 'public'
      JOIN pg_catalog.pg_depend dependency
        ON dependency.objid = sequence_relation.oid
       AND dependency.classid = 'pg_catalog.pg_class'::regclass
       AND dependency.refclassid = 'pg_catalog.pg_class'::regclass
       AND dependency.deptype IN ('a', 'i')
      JOIN pg_catalog.pg_class table_relation
        ON table_relation.oid = dependency.refobjid
      JOIN pg_catalog.pg_namespace table_namespace
        ON table_namespace.oid = table_relation.relnamespace
       AND table_namespace.nspname = 'public'
      JOIN pg_temp._strategy_platform_v13_legacy_capability_tables expected_table
        ON expected_table.table_name = table_relation.relname;
    IF observed_count IS DISTINCT FROM 48::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_LEGACY_CAPABILITY_SEQUENCE_OWNERSHIP: expected 48, observed %',
            observed_count;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_sequences expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.sequence_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE relation.relowner = runtime_role.oid
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_OWNS_LEGACY_CAPABILITY_SEQUENCE';
    END IF;
    IF applying_role.rolsuper IS NOT TRUE AND EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_sequences expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.sequence_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE pg_get_userbyid(relation.relowner) IS DISTINCT FROM current_user
    ) THEN
        RAISE EXCEPTION
            'BLOCKED_APPLYING_ROLE_CANNOT_REVOKE_LEGACY_SEQUENCES';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_sequences expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.sequence_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) acl
         WHERE acl.grantee <> relation.relowner
           AND acl.grantee NOT IN (0, runtime_role.oid, attestor_role.oid)
    ) THEN
        RAISE EXCEPTION 'BLOCKED_UNKNOWN_LEGACY_SEQUENCE_ACL_GRANTEE';
    END IF;

    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_legacy_capability_functions expected
      JOIN pg_catalog.pg_proc function_row
        ON function_row.oid = pg_catalog.to_regprocedure(
            format(
                '%I.%I(%s)',
                'public', expected.function_name, expected.identity_arguments
            )
        );
    IF observed_count IS DISTINCT FROM 37::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_LEGACY_CAPABILITY_FUNCTION_SET: expected 37, observed %',
            observed_count;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.oid = pg_catalog.to_regprocedure(
                format(
                    '%I.%I(%s)',
                    'public', expected.function_name, expected.identity_arguments
                )
            )
         WHERE function_row.prosecdef IS NOT TRUE
            OR pg_get_userbyid(function_row.proowner)
               IS DISTINCT FROM 'freqtrade_ai_attestor'
            OR function_row.proowner = runtime_role.oid
    ) THEN
        RAISE EXCEPTION 'BLOCKED_LEGACY_CAPABILITY_FUNCTION_IDENTITY';
    END IF;
    IF applying_role.rolsuper IS NOT TRUE AND EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.oid = pg_catalog.to_regprocedure(
                format(
                    '%I.%I(%s)',
                    'public', expected.function_name, expected.identity_arguments
                )
            )
         WHERE pg_get_userbyid(function_row.proowner) IS DISTINCT FROM current_user
    ) THEN
        RAISE EXCEPTION
            'BLOCKED_APPLYING_ROLE_CANNOT_REVOKE_LEGACY_FUNCTIONS';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.oid = pg_catalog.to_regprocedure(
                format(
                    '%I.%I(%s)',
                    'public', expected.function_name, expected.identity_arguments
                )
            )
          CROSS JOIN LATERAL pg_catalog.aclexplode(function_row.proacl) acl
         WHERE acl.grantee <> function_row.proowner
           AND acl.grantee NOT IN (0, runtime_role.oid, attestor_role.oid)
    ) THEN
        RAISE EXCEPTION 'BLOCKED_UNKNOWN_LEGACY_FUNCTION_ACL_GRANTEE';
    END IF;

    -- Only cardinality is observed.  The excluded dump must leave both secret
    -- relations empty; this script never selects a credential column or value.
    SELECT count(*) INTO observed_count
      FROM public.okx_demo_attestation_secrets;
    IF observed_count IS DISTINCT FROM 0::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_ATTESTATION_SECRET_ROWS_PRESENT: observed %',
            observed_count;
    END IF;
    SELECT count(*) INTO observed_count
      FROM public.okx_demo_operator_consent_secrets;
    IF observed_count IS DISTINCT FROM 0::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_OPERATOR_CONSENT_SECRET_ROWS_PRESENT: observed %',
            observed_count;
    END IF;

    -- The applying session must own every relation it changes.  Superuser
    -- status alone is not accepted as an object-identity substitute.
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_read_acl_allowlist expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE pg_get_userbyid(relation.relowner) IS DISTINCT FROM current_user
    ) THEN
        RAISE EXCEPTION
            'BLOCKED_APPLYING_ROLE_NOT_RELATION_OWNER';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM (
              SELECT table_name
                FROM pg_temp._strategy_platform_v13_read_acl_allowlist
              UNION ALL
              SELECT table_name
                FROM pg_temp._strategy_platform_v13_owner_only_tables
          ) expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE relation.relowner = runtime_role.oid
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_OWNS_V13_RELATION';
    END IF;

    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_guard_functions expected
      JOIN pg_catalog.pg_proc function_row
        ON function_row.proname = expected.function_name
       AND function_row.pronargs = 0
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = function_row.pronamespace
       AND namespace.nspname = 'public';
    IF observed_count IS DISTINCT FROM 13::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_V13_GUARD_FUNCTION_SET: expected 13, observed %',
            observed_count;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_guard_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.proname = expected.function_name
           AND function_row.pronargs = 0
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = function_row.pronamespace
           AND namespace.nspname = 'public'
         WHERE pg_get_userbyid(function_row.proowner) IS DISTINCT FROM current_user
            OR function_row.prosecdef IS TRUE
            OR function_row.proowner = runtime_role.oid
    ) THEN
        RAISE EXCEPTION
            'BLOCKED_V13_GUARD_FUNCTION_OWNERSHIP';
    END IF;

    -- Owner-only operational/evidence tables must already have no effective
    -- runtime privilege.  This read-ACL script will not silently repair an
    -- unexpected broader ACL on those objects.
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_owner_only_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE has_table_privilege(
                   'freqtrade', relation.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               )
    ) THEN
        RAISE EXCEPTION
            'BLOCKED_OWNER_ONLY_RUNTIME_PRIVILEGE';
    END IF;

    -- No V1.3 sequence may be reachable by the runtime role.  Reads use
    -- persisted IDs and never require nextval/currval.
    IF EXISTS (
        SELECT 1
          FROM (
              SELECT table_name
                FROM pg_temp._strategy_platform_v13_read_acl_allowlist
              UNION ALL
              SELECT table_name
                FROM pg_temp._strategy_platform_v13_owner_only_tables
          ) expected
          JOIN pg_catalog.pg_class table_relation
            ON table_relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace table_namespace
            ON table_namespace.oid = table_relation.relnamespace
           AND table_namespace.nspname = 'public'
          JOIN pg_catalog.pg_depend dependency
            ON dependency.refobjid = table_relation.oid
           AND dependency.classid = 'pg_catalog.pg_class'::regclass
           AND dependency.refclassid = 'pg_catalog.pg_class'::regclass
           AND dependency.deptype IN ('a', 'i')
          JOIN pg_catalog.pg_class sequence_relation
            ON sequence_relation.oid = dependency.objid
           AND sequence_relation.relkind = 'S'
         WHERE sequence_relation.relowner = runtime_role.oid
            OR EXISTS (
                SELECT 1
                  FROM pg_catalog.aclexplode(
                      COALESCE(
                          sequence_relation.relacl,
                          pg_catalog.acldefault(
                              'S', sequence_relation.relowner
                          )
                      )
                  ) acl
                 WHERE acl.grantee IN (0, runtime_role.oid)
                   AND acl.privilege_type IN ('USAGE', 'SELECT', 'UPDATE')
            )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_V13_RUNTIME_SEQUENCE_PRIVILEGE';
    END IF;

    -- A default ACL would silently extend this exact object allowlist to
    -- future tables/functions/sequences, so it is forbidden for this role.
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_default_acl default_acl
          LEFT JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = default_acl.defaclnamespace
          CROSS JOIN LATERAL pg_catalog.aclexplode(default_acl.defaclacl) acl
         WHERE (default_acl.defaclnamespace = 0
                OR namespace.nspname = 'public')
           AND default_acl.defaclobjtype IN ('r', 'S', 'f')
           AND acl.grantee IN (0, runtime_role.oid)
    ) THEN
        RAISE EXCEPTION 'BLOCKED_V13_DEFAULT_ACL';
    END IF;
END
$preflight$;

-- Exact object grants: no ALL TABLES clause and no default privilege mutation.
DO $grant_read_acl$
DECLARE
    item record;
    column_list text;
BEGIN
    FOR item IN
        SELECT table_name
          FROM pg_temp._strategy_platform_v13_read_acl_allowlist
         ORDER BY table_name
    LOOP
        SELECT string_agg(format('%I', attribute.attname), ', ' ORDER BY attribute.attnum)
          INTO column_list
          FROM pg_catalog.pg_attribute attribute
          JOIN pg_catalog.pg_class relation
            ON relation.oid = attribute.attrelid
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE relation.relname = item.table_name
           AND attribute.attnum > 0
           AND attribute.attisdropped IS FALSE;
        EXECUTE format(
            'REVOKE SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER '
            'ON TABLE %I.%I FROM PUBLIC, %I',
            'public', item.table_name, 'freqtrade'
        );
        EXECUTE format(
            'REVOKE SELECT (%s), INSERT (%s), UPDATE (%s), REFERENCES (%s) '
            'ON TABLE %I.%I FROM PUBLIC, %I',
            column_list, column_list, column_list, column_list,
            'public', item.table_name, 'freqtrade'
        );
        EXECUTE format(
            'GRANT SELECT ON TABLE %I.%I TO %I',
            'public', item.table_name, 'freqtrade'
        );
    END LOOP;
END
$grant_read_acl$;

-- Remove every restored legacy table/sequence/function capability from PUBLIC
-- and both exact non-owner roles observed in the source catalog.  Each object
-- and grantee comes from a fixed allowlist above; no schema wildcard, dynamic
-- grantee discovery, or future-object default privilege is used.
DO $revoke_legacy_capabilities$
DECLARE
    item record;
    column_list text;
    attestor_oid oid;
BEGIN
    SELECT oid INTO STRICT attestor_oid
      FROM pg_catalog.pg_roles
     WHERE rolname = 'freqtrade_ai_attestor';

    FOR item IN
        SELECT expected.table_name, relation.relowner
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         ORDER BY expected.table_name
    LOOP
        SELECT string_agg(format('%I', attribute.attname), ', ' ORDER BY attribute.attnum)
          INTO column_list
          FROM pg_catalog.pg_attribute attribute
          JOIN pg_catalog.pg_class relation
            ON relation.oid = attribute.attrelid
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE relation.relname = item.table_name
           AND attribute.attnum > 0
           AND attribute.attisdropped IS FALSE;
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM PUBLIC, %I',
            'public', item.table_name, 'freqtrade'
        );
        EXECUTE format(
            'REVOKE SELECT (%s), INSERT (%s), UPDATE (%s), REFERENCES (%s) '
            'ON TABLE %I.%I FROM PUBLIC, %I',
            column_list, column_list, column_list, column_list,
            'public', item.table_name, 'freqtrade'
        );
        IF item.relowner <> attestor_oid THEN
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM %I',
                'public', item.table_name, 'freqtrade_ai_attestor'
            );
            EXECUTE format(
                'REVOKE SELECT (%s), INSERT (%s), UPDATE (%s), REFERENCES (%s) '
                'ON TABLE %I.%I FROM %I',
                column_list, column_list, column_list, column_list,
                'public', item.table_name, 'freqtrade_ai_attestor'
            );
        END IF;
    END LOOP;

    FOR item IN
        SELECT expected.sequence_name, relation.relowner
          FROM pg_temp._strategy_platform_v13_legacy_capability_sequences expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.sequence_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         ORDER BY expected.sequence_name
    LOOP
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM PUBLIC, %I',
            'public', item.sequence_name, 'freqtrade'
        );
        IF item.relowner <> attestor_oid THEN
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM %I',
                'public', item.sequence_name, 'freqtrade_ai_attestor'
            );
        END IF;
    END LOOP;

    FOR item IN
        SELECT expected.function_name, expected.identity_arguments,
               function_row.proowner
          FROM pg_temp._strategy_platform_v13_legacy_capability_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.oid = pg_catalog.to_regprocedure(
                format(
                    '%I.%I(%s)',
                    'public', expected.function_name, expected.identity_arguments
                )
            )
         ORDER BY expected.function_name, expected.identity_arguments
    LOOP
        EXECUTE format(
            'REVOKE EXECUTE ON FUNCTION %I.%I(%s) FROM PUBLIC, %I',
            'public', item.function_name, item.identity_arguments, 'freqtrade'
        );
        IF item.proowner <> attestor_oid THEN
            EXECUTE format(
                'REVOKE EXECUTE ON FUNCTION %I.%I(%s) FROM %I',
                'public', item.function_name, item.identity_arguments,
                'freqtrade_ai_attestor'
            );
        END IF;
    END LOOP;
END
$revoke_legacy_capabilities$;

-- Trigger invocation is performed by PostgreSQL and does not require callers
-- to hold EXECUTE on the trigger function.  Remove the default PUBLIC surface.
DO $revoke_guard_execute$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT function_name
          FROM pg_temp._strategy_platform_v13_guard_functions
         ORDER BY function_name
    LOOP
        EXECUTE format(
            'REVOKE EXECUTE ON FUNCTION %I.%I() FROM PUBLIC, %I',
            'public', item.function_name, 'freqtrade'
        );
    END LOOP;
END
$revoke_guard_execute$;

-- Postcondition attestation is part of the same transaction.  Any mismatch
-- aborts and rolls back every preceding ACL change.
DO $postcondition$
DECLARE
    runtime_oid oid;
    observed_count bigint;
BEGIN
    SELECT oid INTO STRICT runtime_oid
      FROM pg_catalog.pg_roles
     WHERE rolname = 'freqtrade';

    -- Exactly one direct, non-grantable SELECT entry per allowlisted table.
    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_read_acl_allowlist expected
      JOIN pg_catalog.pg_class relation
        ON relation.relname = expected.table_name
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = relation.relnamespace
       AND namespace.nspname = 'public'
      CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) acl
     WHERE acl.grantee = runtime_oid
       AND acl.privilege_type = 'SELECT'
       AND acl.is_grantable IS FALSE;
    IF observed_count IS DISTINCT FROM 54::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_READ_ACL_CARDINALITY: expected 54, observed %',
            observed_count;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_read_acl_allowlist expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) acl
         WHERE acl.grantee IN (0, runtime_oid)
           AND NOT (
               acl.grantee = runtime_oid
               AND acl.privilege_type = 'SELECT'
               AND acl.is_grantable IS FALSE
           )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_UNEXPECTED_DIRECT_READ_ACL';
    END IF;

    -- All 71 V1.3 tables are non-writable to the runtime role; the 17
    -- operational/evidence tables are not readable either.
    IF EXISTS (
        SELECT 1
          FROM (
              SELECT table_name
                FROM pg_temp._strategy_platform_v13_read_acl_allowlist
              UNION ALL
              SELECT table_name
                FROM pg_temp._strategy_platform_v13_owner_only_tables
          ) expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE has_table_privilege(
                   'freqtrade', relation.oid,
                   'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_V13_WRITE_PRIVILEGE';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_owner_only_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE has_table_privilege('freqtrade', relation.oid, 'SELECT')
    ) THEN
        RAISE EXCEPTION 'BLOCKED_OWNER_ONLY_RUNTIME_SELECT';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM (
              SELECT table_name
                FROM pg_temp._strategy_platform_v13_read_acl_allowlist
              UNION ALL
              SELECT table_name
                FROM pg_temp._strategy_platform_v13_owner_only_tables
          ) expected
          JOIN pg_catalog.pg_class table_relation
            ON table_relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace table_namespace
            ON table_namespace.oid = table_relation.relnamespace
           AND table_namespace.nspname = 'public'
          JOIN pg_catalog.pg_depend dependency
            ON dependency.refobjid = table_relation.oid
           AND dependency.classid = 'pg_catalog.pg_class'::regclass
           AND dependency.refclassid = 'pg_catalog.pg_class'::regclass
           AND dependency.deptype IN ('a', 'i')
          JOIN pg_catalog.pg_class sequence_relation
            ON sequence_relation.oid = dependency.objid
           AND sequence_relation.relkind = 'S'
         WHERE sequence_relation.relowner = runtime_oid
            OR EXISTS (
                SELECT 1
                  FROM pg_catalog.aclexplode(
                      COALESCE(
                          sequence_relation.relacl,
                          pg_catalog.acldefault(
                              'S', sequence_relation.relowner
                          )
                      )
                  ) acl
                 WHERE acl.grantee IN (0, runtime_oid)
                   AND acl.privilege_type IN ('USAGE', 'SELECT', 'UPDATE')
            )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_V13_SEQUENCE_PRIVILEGE';
    END IF;

    -- Table-level SELECT is the only direct runtime ACL on the read allowlist;
    -- no column ACL may survive on any V1.3 relation.
    IF EXISTS (
        SELECT 1
          FROM (
              SELECT table_name
                FROM pg_temp._strategy_platform_v13_read_acl_allowlist
              UNION ALL
              SELECT table_name
                FROM pg_temp._strategy_platform_v13_owner_only_tables
          ) expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          JOIN pg_catalog.pg_attribute attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum > 0
           AND attribute.attisdropped IS FALSE
          CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) acl
         WHERE acl.grantee IN (0, runtime_oid)
    ) THEN
        RAISE EXCEPTION 'BLOCKED_UNEXPECTED_DIRECT_V13_COLUMN_ACL';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_owner_only_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          JOIN pg_catalog.pg_attribute attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum > 0
           AND attribute.attisdropped IS FALSE
         WHERE has_column_privilege(
                   'freqtrade', relation.oid, attribute.attnum,
                   'SELECT,INSERT,UPDATE,REFERENCES'
               )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_OWNER_ONLY_RUNTIME_COLUMN_PRIVILEGE';
    END IF;

    -- The abandoned execution surface is owner-only: no effective runtime
    -- privilege, no direct non-owner ACL, and no loss of owner authority.
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE has_table_privilege(
                   'freqtrade', relation.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_LEGACY_TABLE_PRIVILEGE';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) acl
         WHERE acl.grantee <> relation.relowner
    ) THEN
        RAISE EXCEPTION 'BLOCKED_NON_OWNER_LEGACY_TABLE_ACL';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE NOT has_table_privilege(
                   pg_get_userbyid(relation.relowner), relation.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_LEGACY_TABLE_OWNER_PRIVILEGE';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          JOIN pg_catalog.pg_attribute attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum > 0
           AND attribute.attisdropped IS FALSE
         WHERE has_column_privilege(
                   'freqtrade', relation.oid, attribute.attnum,
                   'SELECT,INSERT,UPDATE,REFERENCES'
               )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_LEGACY_COLUMN_PRIVILEGE';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_tables expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.table_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          JOIN pg_catalog.pg_attribute attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum > 0
           AND attribute.attisdropped IS FALSE
          CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) acl
         WHERE acl.grantee <> relation.relowner
    ) THEN
        RAISE EXCEPTION 'BLOCKED_NON_OWNER_LEGACY_COLUMN_ACL';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_sequences expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.sequence_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE relation.relowner = runtime_oid
            OR EXISTS (
                SELECT 1
                  FROM pg_catalog.aclexplode(
                      COALESCE(
                          relation.relacl,
                          pg_catalog.acldefault('S', relation.relowner)
                      )
                  ) acl
                 WHERE acl.grantee IN (0, runtime_oid)
                   AND acl.privilege_type IN ('USAGE', 'SELECT', 'UPDATE')
            )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_LEGACY_SEQUENCE_PRIVILEGE';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_sequences expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.sequence_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) acl
         WHERE acl.grantee <> relation.relowner
    ) THEN
        RAISE EXCEPTION 'BLOCKED_NON_OWNER_LEGACY_SEQUENCE_ACL';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_sequences expected
          JOIN pg_catalog.pg_class relation
            ON relation.relname = expected.sequence_name
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE relation.relowner = runtime_oid
            OR NOT EXISTS (
                SELECT 1
                  FROM pg_catalog.pg_roles owner_role
                 WHERE owner_role.oid = relation.relowner
            )
    ) THEN
        -- PostgreSQL object owners retain implicit sequence privileges even
        -- when their explicit ACL entry is absent.  Preserve and attest the
        -- non-runtime owner identity instead of calling a helper that the
        -- planner may evaluate against non-sequence pg_class rows.
        RAISE EXCEPTION 'BLOCKED_LEGACY_SEQUENCE_OWNER_IDENTITY';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.oid = pg_catalog.to_regprocedure(
                format(
                    '%I.%I(%s)',
                    'public', expected.function_name, expected.identity_arguments
                )
            )
         WHERE has_function_privilege(
                   'freqtrade', function_row.oid, 'EXECUTE'
               )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_LEGACY_FUNCTION_EXECUTE';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.oid = pg_catalog.to_regprocedure(
                format(
                    '%I.%I(%s)',
                    'public', expected.function_name, expected.identity_arguments
                )
            )
          CROSS JOIN LATERAL pg_catalog.aclexplode(function_row.proacl) acl
         WHERE acl.grantee <> function_row.proowner
    ) THEN
        RAISE EXCEPTION 'BLOCKED_NON_OWNER_LEGACY_FUNCTION_ACL';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_legacy_capability_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.oid = pg_catalog.to_regprocedure(
                format(
                    '%I.%I(%s)',
                    'public', expected.function_name, expected.identity_arguments
                )
            )
         WHERE NOT has_function_privilege(
                   pg_get_userbyid(function_row.proowner),
                   function_row.oid, 'EXECUTE'
               )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_LEGACY_FUNCTION_OWNER_EXECUTE';
    END IF;

    -- Reconfirm that the restore exclusion remained effective; only row
    -- cardinality is observed, never a secret column or value.
    SELECT count(*) INTO observed_count
      FROM public.okx_demo_attestation_secrets;
    IF observed_count IS DISTINCT FROM 0::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_ATTESTATION_SECRET_ROWS_AFTER_GRANT: observed %',
            observed_count;
    END IF;
    SELECT count(*) INTO observed_count
      FROM public.okx_demo_operator_consent_secrets;
    IF observed_count IS DISTINCT FROM 0::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_OPERATOR_CONSENT_SECRET_ROWS_AFTER_GRANT: observed %',
            observed_count;
    END IF;

    IF has_schema_privilege('freqtrade', 'public', 'CREATE')
       OR has_database_privilege('freqtrade', current_database(), 'CREATE') THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_CREATE_PRIVILEGE_AFTER_GRANT';
    END IF;
    IF NOT has_schema_privilege('freqtrade', 'public', 'USAGE') THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_SCHEMA_USAGE_AFTER_GRANT';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_auth_members
         WHERE member = runtime_oid OR roleid = runtime_oid
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_ROLE_MEMBERSHIP_AFTER_GRANT';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_roles
         WHERE oid = runtime_oid
           AND (
               rolcanlogin IS NOT TRUE
               OR rolsuper IS NOT FALSE
               OR rolcreaterole IS NOT FALSE
               OR rolcreatedb IS NOT FALSE
               OR rolreplication IS NOT FALSE
               OR rolbypassrls IS NOT FALSE
           )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_ROLE_ATTRIBUTES_AFTER_GRANT';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_guard_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.proname = expected.function_name
           AND function_row.pronargs = 0
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = function_row.pronamespace
           AND namespace.nspname = 'public'
          CROSS JOIN LATERAL pg_catalog.aclexplode(function_row.proacl) acl
         WHERE acl.grantee IN (0, runtime_oid)
           AND acl.privilege_type = 'EXECUTE'
    ) OR EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_guard_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.proname = expected.function_name
           AND function_row.pronargs = 0
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = function_row.pronamespace
           AND namespace.nspname = 'public'
         WHERE has_function_privilege(
                   'freqtrade', function_row.oid, 'EXECUTE'
               )
    ) OR EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_guard_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.proname = expected.function_name
           AND function_row.pronargs = 0
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = function_row.pronamespace
           AND namespace.nspname = 'public'
          CROSS JOIN LATERAL pg_catalog.aclexplode(function_row.proacl) acl
         WHERE acl.grantee <> function_row.proowner
    ) OR EXISTS (
        SELECT 1
          FROM pg_temp._strategy_platform_v13_guard_functions expected
          JOIN pg_catalog.pg_proc function_row
            ON function_row.proname = expected.function_name
           AND function_row.pronargs = 0
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = function_row.pronamespace
           AND namespace.nspname = 'public'
         WHERE NOT has_function_privilege(
                   pg_get_userbyid(function_row.proowner),
                   function_row.oid, 'EXECUTE'
               )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_GUARD_FUNCTION_EXECUTE_PRIVILEGE';
    END IF;
END
$postcondition$;

COMMIT;
