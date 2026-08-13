\set ON_ERROR_STOP on

-- Repair the one accepted-design-lab ACL drift observed before V1.3 owner
-- activation: the runtime role must not read the owner schema-version marker.
--
-- This is deliberately not a general ACL reconciler.  It proceeds only when
-- every catalog fact is the already-accepted v47 state plus exactly one direct,
-- non-grantable SELECT on public.freqtrade_ai_schema_migrations.  Unknown ACL
-- state blocks before the sole mutation.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TEMPORARY TABLE _strategy_platform_v13_runtime_read_allowlist (
    table_name name PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO _strategy_platform_v13_runtime_read_allowlist (table_name) VALUES
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

DO $preflight$
DECLARE
    runtime_role pg_catalog.pg_roles%ROWTYPE;
    marker_relation record;
    latest_run record;
    observed_count bigint;
    observed_version text;
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
            'BLOCKED_RUNTIME_ROLE_MEMBERSHIP: freqtrade must have no role memberships';
    END IF;
    IF has_schema_privilege('freqtrade', 'public', 'CREATE')
       OR has_database_privilege('freqtrade', current_database(), 'CREATE') THEN
        RAISE EXCEPTION
            'BLOCKED_RUNTIME_CREATE_PRIVILEGE: freqtrade must not create objects';
    END IF;
    IF NOT has_schema_privilege('freqtrade', 'public', 'USAGE') THEN
        RAISE EXCEPTION
            'BLOCKED_RUNTIME_SCHEMA_USAGE: freqtrade cannot resolve allowlisted tables';
    END IF;

    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_runtime_read_allowlist;
    IF observed_count IS DISTINCT FROM 54::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_READ_ALLOWLIST_CARDINALITY: expected 54, observed %',
            observed_count;
    END IF;
    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_runtime_read_allowlist expected
      JOIN pg_catalog.pg_class relation
        ON relation.relname = expected.table_name
       AND relation.relkind IN ('r', 'p')
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = relation.relnamespace
       AND namespace.nspname = 'public';
    IF observed_count IS DISTINCT FROM 54::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_READ_ALLOWLIST_RELATION_SET: expected 54, observed %',
            observed_count;
    END IF;

    SELECT relation.oid, relation.relowner
      INTO marker_relation
      FROM pg_catalog.pg_class relation
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = relation.relnamespace
       AND namespace.nspname = 'public'
     WHERE relation.relname = 'freqtrade_ai_schema_migrations'
       AND relation.relkind IN ('r', 'p');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'BLOCKED_SCHEMA_MARKER_MISSING';
    END IF;
    IF pg_catalog.pg_get_userbyid(marker_relation.relowner)
       IS DISTINCT FROM current_user THEN
        RAISE EXCEPTION
            'BLOCKED_SCHEMA_MARKER_OWNER: current owner is %, applying owner is %',
            pg_catalog.pg_get_userbyid(marker_relation.relowner), current_user;
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

    SELECT run.status, run.target_schema_version,
           run.destructive_write_count, run.overwritten_row_count,
           run.deleted_row_count, run.completed_at
      INTO latest_run
      FROM public.strategy_platform_migration_runs run
     WHERE run.migration_key = 'strategy-platform-v13-task1-real-data-v1'
       AND run.execution_scope = 'DESIGN_LAB'
     ORDER BY run.completed_at DESC NULLS FIRST, run.id DESC
     LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'BLOCKED_MIGRATION_RUN: design-lab migration run is missing';
    END IF;
    IF latest_run.status IS DISTINCT FROM 'SUCCEEDED'
       OR latest_run.target_schema_version IS DISTINCT FROM '20260813_47'
       OR latest_run.destructive_write_count IS DISTINCT FROM 0
       OR latest_run.overwritten_row_count IS DISTINCT FROM 0
       OR latest_run.deleted_row_count IS DISTINCT FROM 0
       OR latest_run.completed_at IS NULL THEN
        RAISE EXCEPTION
            'BLOCKED_MIGRATION_RUN: latest design-lab v47 migration is not safely SUCCEEDED';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.strategy_platform_migration_runs run
         WHERE run.execution_scope = 'DESIGN_LAB'
           AND run.status IN ('PLANNED', 'RUNNING', 'RECONCILING')
    ) THEN
        RAISE EXCEPTION 'BLOCKED_NONTERMINAL_MIGRATION_RUN';
    END IF;

    SELECT count(*) INTO observed_count
      FROM public.okx_demo_attestation_secrets;
    IF observed_count IS DISTINCT FROM 0::bigint THEN
        RAISE EXCEPTION 'BLOCKED_ATTESTATION_SECRET_ROWS: expected 0, observed %', observed_count;
    END IF;
    SELECT count(*) INTO observed_count
      FROM public.okx_demo_operator_consent_secrets;
    IF observed_count IS DISTINCT FROM 0::bigint THEN
        RAISE EXCEPTION 'BLOCKED_OPERATOR_SECRET_ROWS: expected 0, observed %', observed_count;
    END IF;

    -- The accepted state is exactly 54 allowlisted table SELECT entries plus
    -- the one marker-table SELECT being removed.  Any other runtime or PUBLIC
    -- table/column ACL is an unknown state and must not be repaired implicitly.
    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_runtime_read_allowlist expected
      JOIN pg_catalog.pg_class relation
        ON relation.relname = expected.table_name
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = relation.relnamespace
       AND namespace.nspname = 'public'
      CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) acl
     WHERE acl.grantee = runtime_role.oid
       AND acl.privilege_type = 'SELECT'
       AND acl.is_grantable IS FALSE;
    IF observed_count IS DISTINCT FROM 54::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_ACCEPTED_READ_ACL_CARDINALITY: expected 54, observed %',
            observed_count;
    END IF;
    SELECT count(*) INTO observed_count
      FROM pg_catalog.pg_class relation
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = relation.relnamespace
       AND namespace.nspname = 'public'
      CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) acl
     WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
       AND acl.grantee = runtime_role.oid
       AND acl.privilege_type = 'SELECT'
       AND acl.is_grantable IS FALSE;
    IF observed_count IS DISTINCT FROM 55::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_PRE_REPAIR_SELECT_CARDINALITY: expected 55, observed %',
            observed_count;
    END IF;
    SELECT count(*) INTO observed_count
      FROM pg_catalog.aclexplode(
          (SELECT relation.relacl
             FROM pg_catalog.pg_class relation
            WHERE relation.oid = marker_relation.oid)
      ) acl
     WHERE acl.grantee = runtime_role.oid
       AND acl.privilege_type = 'SELECT'
       AND acl.is_grantable IS FALSE;
    IF observed_count IS DISTINCT FROM 1::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_SCHEMA_MARKER_SELECT_DRIFT: expected exactly one, observed %',
            observed_count;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class relation
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) acl
         WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND acl.grantee IN (0, runtime_role.oid)
           AND NOT (
               acl.grantee = runtime_role.oid
               AND acl.privilege_type = 'SELECT'
               AND acl.is_grantable IS FALSE
               AND (
                   relation.oid = marker_relation.oid
                   OR EXISTS (
                       SELECT 1
                         FROM pg_temp._strategy_platform_v13_runtime_read_allowlist expected
                        WHERE expected.table_name = relation.relname
                   )
               )
           )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_UNKNOWN_RUNTIME_OR_PUBLIC_TABLE_ACL';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class relation
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          JOIN pg_catalog.pg_attribute attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum > 0
           AND attribute.attisdropped IS FALSE
          CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) acl
         WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND acl.grantee IN (0, runtime_role.oid)
    ) THEN
        RAISE EXCEPTION 'BLOCKED_UNKNOWN_RUNTIME_OR_PUBLIC_COLUMN_ACL';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class relation
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND (
               relation.relowner = runtime_role.oid
               OR has_table_privilege(
                   'freqtrade', relation.oid,
                   'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               )
           )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_TABLE_OWNERSHIP_OR_WRITE_PRIVILEGE';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class relation
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE relation.relkind = 'S'
           AND (
               relation.relowner = runtime_role.oid
               OR EXISTS (
                   SELECT 1
                     FROM pg_catalog.aclexplode(
                         COALESCE(
                             relation.relacl,
                             pg_catalog.acldefault('S', relation.relowner)
                         )
                     ) acl
                    WHERE acl.grantee IN (0, runtime_role.oid)
                      AND acl.privilege_type IN ('USAGE', 'SELECT', 'UPDATE')
               )
           )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_OR_PUBLIC_SEQUENCE_PRIVILEGE';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_default_acl default_acl
          LEFT JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = default_acl.defaclnamespace
          CROSS JOIN LATERAL pg_catalog.aclexplode(default_acl.defaclacl) acl
         WHERE (default_acl.defaclnamespace = 0 OR namespace.nspname = 'public')
           AND default_acl.defaclobjtype IN ('r', 'S', 'f')
           AND acl.grantee IN (0, runtime_role.oid)
    ) THEN
        RAISE EXCEPTION 'BLOCKED_RUNTIME_OR_PUBLIC_DEFAULT_ACL';
    END IF;
END
$preflight$;

REVOKE SELECT ON TABLE public.freqtrade_ai_schema_migrations FROM freqtrade;

DO $postcondition$
DECLARE
    runtime_oid oid;
    marker_oid oid;
    observed_count bigint;
BEGIN
    SELECT oid INTO STRICT runtime_oid
      FROM pg_catalog.pg_roles
     WHERE rolname = 'freqtrade';
    SELECT relation.oid INTO STRICT marker_oid
      FROM pg_catalog.pg_class relation
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = relation.relnamespace
       AND namespace.nspname = 'public'
     WHERE relation.relname = 'freqtrade_ai_schema_migrations'
       AND relation.relkind IN ('r', 'p');

    SELECT count(*) INTO observed_count
      FROM pg_temp._strategy_platform_v13_runtime_read_allowlist expected
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
            'BLOCKED_POST_REPAIR_READ_ACL_CARDINALITY: expected 54, observed %',
            observed_count;
    END IF;
    SELECT count(*) INTO observed_count
      FROM pg_catalog.pg_class relation
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = relation.relnamespace
       AND namespace.nspname = 'public'
      CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) acl
     WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
       AND acl.grantee = runtime_oid
       AND acl.privilege_type = 'SELECT'
       AND acl.is_grantable IS FALSE;
    IF observed_count IS DISTINCT FROM 54::bigint THEN
        RAISE EXCEPTION
            'BLOCKED_POST_REPAIR_TOTAL_SELECT_CARDINALITY: expected 54, observed %',
            observed_count;
    END IF;
    IF has_table_privilege('freqtrade', marker_oid, 'SELECT') THEN
        RAISE EXCEPTION 'BLOCKED_SCHEMA_MARKER_SELECT_REMAINS';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class relation
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) acl
         WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND acl.grantee IN (0, runtime_oid)
           AND NOT (
               acl.grantee = runtime_oid
               AND acl.privilege_type = 'SELECT'
               AND acl.is_grantable IS FALSE
               AND EXISTS (
                   SELECT 1
                     FROM pg_temp._strategy_platform_v13_runtime_read_allowlist expected
                    WHERE expected.table_name = relation.relname
               )
           )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_POST_REPAIR_UNKNOWN_TABLE_ACL';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class relation
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
          JOIN pg_catalog.pg_attribute attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum > 0
           AND attribute.attisdropped IS FALSE
          CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) acl
         WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND acl.grantee IN (0, runtime_oid)
    ) THEN
        RAISE EXCEPTION 'BLOCKED_POST_REPAIR_UNKNOWN_COLUMN_ACL';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class relation
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND (
               relation.relowner = runtime_oid
               OR has_table_privilege(
                   'freqtrade', relation.oid,
                   'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               )
           )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_POST_REPAIR_TABLE_OWNERSHIP_OR_WRITE_PRIVILEGE';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class relation
          JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = relation.relnamespace
           AND namespace.nspname = 'public'
         WHERE relation.relkind = 'S'
           AND (
               relation.relowner = runtime_oid
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
           )
    ) THEN
        RAISE EXCEPTION 'BLOCKED_POST_REPAIR_SEQUENCE_PRIVILEGE';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_default_acl default_acl
          LEFT JOIN pg_catalog.pg_namespace namespace
            ON namespace.oid = default_acl.defaclnamespace
          CROSS JOIN LATERAL pg_catalog.aclexplode(default_acl.defaclacl) acl
         WHERE (default_acl.defaclnamespace = 0 OR namespace.nspname = 'public')
           AND default_acl.defaclobjtype IN ('r', 'S', 'f')
           AND acl.grantee IN (0, runtime_oid)
    ) THEN
        RAISE EXCEPTION 'BLOCKED_POST_REPAIR_DEFAULT_ACL';
    END IF;
    IF has_schema_privilege('freqtrade', 'public', 'CREATE')
       OR has_database_privilege('freqtrade', current_database(), 'CREATE') THEN
        RAISE EXCEPTION 'BLOCKED_POST_REPAIR_RUNTIME_CREATE_PRIVILEGE';
    END IF;
END
$postcondition$;

COMMIT;
