\set ON_ERROR_STOP on
\pset pager off
\pset null '[UNKNOWN]'

-- Strategy Platform V1.3 Task 1 read-only PostgreSQL evidence template.
--
-- Run with psql as a role that can SELECT every in-scope table.  This script
-- intentionally does not inspect file contents or secret-bearing columns.  It
-- does not enumerate ACLs, grants, credential values, or protected-row counts;
-- uses \gexec only where a preceding catalog query generates SELECT statements;
-- no generated statement performs DDL, DML, COPY, or invokes a mutating routine.
-- Missing V1.3 tables remain auditable as MISSING/UNKNOWN instead of being
-- hidden behind an exception or treated as empty.
--
-- Companion file evidence is emitted before and after migration by:
--   python backend/scripts/strategy_platform_v13_market_data_inventory.py \
--     snapshot --canonical-data-root <resolved-user-data-data> \
--     --repository-root <canonical-repository> \
--     --expected-targets <explicit-target-manifest.json> \
--     --aggregate-receipt <aggregate-source-receipt.json>
--   python backend/scripts/strategy_platform_v13_market_data_inventory.py \
--     compare --before <before.json> --after <after.json>
--   python backend/scripts/strategy_platform_v13_market_data_inventory.py \
--     migration-evidence --canonical-data-root <resolved-user-data-data> \
--     --repository-root <artifact-repository> \
--     --source-repository-root <canonical-source-repository> \
--     --expected-targets <explicit-target-manifest.json> \
--     --original-aggregate-receipt <legacy-receipt.json> \
--     --corrected-aggregate-receipt <path-only-corrected-receipt.json>
-- The manifest schema is strategy-platform-v13-market-target-manifest-v1.
-- Its target paths may be safe input locators; the emitted persistent identity
-- is canonical-market-data-root-relative-v1 only.  Main Task 1 should consume
-- target_contract/file_evidence/source_evidence/aggregate_binding, retain NULL
-- for UNKNOWN fields, and never persist observed_absolute_path as identity.
-- PASSED exits 0; BLOCKED and UNKNOWN both exit non-zero while remaining
-- distinct in JSON.  This SQL is the complementary database-side read-only
-- report and does not authorize migration from a successful evidence run.
-- Migration evidence reports legacy receipt BLOCKED separately, permits only
-- the two root-contract fields plus canonicalization of six source paths, and
-- evaluates close-time freshness as of the original receipt downloaded_at.
-- generated_at and artifact_generation_delay_seconds remain explicit so this
-- historical acquisition-time classification cannot imply current freshness.

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

SELECT
    'execution_context'::text AS evidence_section,
    current_database()::text AS database_name,
    current_schema()::text AS schema_name,
    current_user::text AS current_role,
    current_setting('transaction_isolation')::text AS transaction_isolation,
    current_setting('transaction_read_only')::text AS transaction_read_only,
    transaction_timestamp() AS evidence_as_of;

-- ---------------------------------------------------------------------------
-- Schema marker and expected Task 1 relations
-- ---------------------------------------------------------------------------

SELECT
    'schema_marker_presence'::text AS evidence_section,
    'freqtrade_ai_schema_migrations'::text AS table_name,
    '20260813_47'::text AS expected_schema_version,
    CASE
        WHEN to_regclass(
            format('%I.%I', current_schema(), 'freqtrade_ai_schema_migrations')
        ) IS NULL THEN 'MISSING'
        ELSE 'PRESENT'
    END AS availability,
    CASE
        WHEN to_regclass(
            format('%I.%I', current_schema(), 'freqtrade_ai_schema_migrations')
        ) IS NULL THEN 'UNKNOWN'
        ELSE 'CHECKED_BELOW'
    END AS evidence_status;

SELECT format(
    $sql$
    WITH latest AS (
        SELECT version, applied_at
        FROM %I.%I
        ORDER BY applied_at DESC, version DESC
        LIMIT 1
    )
    SELECT
        'schema_marker'::text AS evidence_section,
        (SELECT count(*) FROM %I.%I)::bigint AS marker_row_count,
        latest.version::text AS observed_schema_version,
        '20260813_47'::text AS expected_schema_version,
        latest.applied_at AS applied_at,
        CASE
            WHEN latest.version IS NULL THEN 'UNKNOWN'
            WHEN latest.version = '20260813_47' THEN 'PASSED'
            ELSE 'BLOCKED'
        END AS evidence_status
    FROM (SELECT 1) AS singleton
    LEFT JOIN latest ON TRUE
    $sql$,
    current_schema(), 'freqtrade_ai_schema_migrations',
    current_schema(), 'freqtrade_ai_schema_migrations'
)
WHERE to_regclass(
    format('%I.%I', current_schema(), 'freqtrade_ai_schema_migrations')
) IS NOT NULL
\gexec

WITH expected_tables(table_name, evidence_group) AS (
    VALUES
        ('configuration_types', 'configuration_core'),
        ('configuration_versions', 'configuration_core'),
        ('configuration_dependencies', 'configuration_core'),
        ('configuration_activations', 'configuration_core'),
        ('configuration_audit_events', 'configuration_core'),
        ('configuration_bundle_snapshots', 'configuration_core'),
        ('strategy_targets', 'strategy_core'),
        ('adapter_definitions', 'task1_extension'),
        ('strategy_source_definitions', 'task1_extension'),
        ('strategy_source_definition_versions', 'task1_extension'),
        ('trigger_source_definitions', 'task1_extension'),
        ('trigger_source_definition_versions', 'task1_extension'),
        ('timeframe_definitions', 'task1_extension'),
        ('timeframe_definition_versions', 'task1_extension'),
        ('research_target_config_sets', 'task1_extension'),
        ('research_target_configs', 'task1_extension'),
        ('strategy_family_definitions', 'task1_extension'),
        ('strategy_family_definition_versions', 'task1_extension'),
        ('provider_model_config_versions', 'task1_extension'),
        ('generation_profile_versions', 'task1_extension'),
        ('generation_profile_families', 'task1_extension'),
        ('scoring_profile_versions', 'task1_extension'),
        ('scoring_rules', 'task1_extension'),
        ('diversity_profile_versions', 'task1_extension'),
        ('diversity_rules', 'task1_extension'),
        ('worker_execution_profile_versions', 'task1_extension'),
        ('scheduler_profile_versions', 'task1_extension'),
        ('market_data_policy_versions', 'task1_extension'),
        ('evidence_freshness_profile_versions', 'task1_extension'),
        ('evidence_freshness_rules', 'task1_extension'),
        ('monitoring_profile_versions', 'task1_extension'),
        ('promotion_profile_versions', 'task1_extension'),
        ('promotion_rules', 'task1_extension'),
        ('risk_profile_versions', 'task1_extension'),
        ('risk_rules', 'task1_extension'),
        ('capacity_profile_versions', 'task1_extension'),
        ('runtime_profile_versions', 'task1_extension'),
        ('deployment_profile_versions', 'task1_extension'),
        ('market_data_profile_versions', 'task1_extension'),
        ('optimization_profile_versions', 'task1_extension'),
        ('ui_presentation_profile_versions', 'task1_extension'),
        ('research_profile_versions', 'task1_extension'),
        ('strategy_submissions', 'task1_extension'),
        ('strategy_runtime_instances', 'task1_extension'),
        ('strategy_position_ledger_entries', 'task1_extension'),
        ('strategy_position_reconciliation_items', 'task1_extension'),
        ('market_data_file_records', 'market_data_evidence'),
        ('market_data_update_jobs', 'task1_extension'),
        ('market_data_update_items', 'task1_extension'),
        ('optimization_runs', 'task1_extension'),
        ('optimization_trials', 'task1_extension'),
        ('strategy_platform_migration_runs', 'migration_evidence'),
        ('strategy_platform_migration_table_snapshots', 'migration_evidence'),
        ('strategy_platform_migration_entity_mappings', 'migration_evidence'),
        ('strategy_platform_migration_conflicts', 'migration_evidence')
)
SELECT
    'task1_table_presence'::text AS evidence_section,
    evidence_group,
    table_name,
    CASE
        WHEN to_regclass(format('%I.%I', current_schema(), table_name)) IS NULL
            THEN 'MISSING'
        ELSE 'PRESENT'
    END AS availability
FROM expected_tables
ORDER BY evidence_group, table_name;

-- Exact counts are generated only for non-sensitive relations.  Relation and
-- column names are classified from catalogs before any table SELECT; protected
-- relations emit EXCLUDED/NULL evidence and are never counted or dereferenced.
WITH relations AS (
    SELECT
        namespace.nspname AS schema_name,
        relation.relname AS table_name,
        CASE
            WHEN EXISTS (
                SELECT 1
                FROM pg_attribute attribute
                WHERE attribute.attrelid = relation.oid
                  AND attribute.attname = 'id'
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
            ) THEN 'id'
            WHEN EXISTS (
                SELECT 1
                FROM pg_attribute attribute
                WHERE attribute.attrelid = relation.oid
                  AND attribute.attname = 'version'
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
            ) THEN 'version'
            ELSE NULL
        END AS bound_column,
        lower(relation.relname) ~
            '(^|_)(secrets?|credentials?|passwords?|passphrases?|tokens?|api_keys?|access_keys?|private_keys?|auth(entication|orization)?)(_|$)'
        OR EXISTS (
            SELECT 1
            FROM pg_attribute sensitive_attribute
            WHERE sensitive_attribute.attrelid = relation.oid
              AND sensitive_attribute.attnum > 0
              AND NOT sensitive_attribute.attisdropped
              AND lower(sensitive_attribute.attname) ~
                  '(^|_)(secrets?|credentials?|passwords?|passphrases?|tokens?|api_keys?|access_keys?|private_keys?|auth(entication|orization)?)(_|$)'
        ) AS is_sensitive
    FROM pg_class relation
    JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = current_schema()
      AND relation.relkind IN ('r', 'p')
      AND relation.relpersistence <> 't'
)
SELECT CASE
    WHEN is_sensitive THEN format(
        $sql$
        SELECT
            'existing_table_inventory'::text AS evidence_section,
            %L::text AS table_name,
            NULL::bigint AS row_count,
            NULL::text AS minimum_key_text,
            NULL::text AS maximum_key_text,
            NULL::text AS bound_column,
            'EXCLUDED_SENSITIVE_RELATION'::text AS inventory_status
        $sql$,
        table_name
    )
    ELSE format(
        $sql$
        SELECT
            'existing_table_inventory'::text AS evidence_section,
            %L::text AS table_name,
            count(*)::bigint AS row_count,
            %s AS minimum_key_text,
            %s AS maximum_key_text,
            %L::text AS bound_column,
            'INVENTORIED'::text AS inventory_status
        FROM %I.%I
        $sql$,
        table_name,
        CASE
            WHEN bound_column IS NULL THEN 'NULL::text'
            ELSE format('min(%I::text)::text', bound_column)
        END,
        CASE
            WHEN bound_column IS NULL THEN 'NULL::text'
            ELSE format('max(%I::text)::text', bound_column)
        END,
        bound_column,
        schema_name,
        table_name
    )
END
FROM relations
ORDER BY table_name
\gexec

-- ---------------------------------------------------------------------------
-- Market-data file/index/receipt evidence
-- ---------------------------------------------------------------------------

SELECT format(
    $sql$
    WITH summary AS (
        SELECT
            count(*)::bigint AS record_count,
            count(*) FILTER (
                WHERE btrim(exchange) = ''
                   OR btrim(market_type) = ''
                   OR btrim(pair) = ''
                   OR btrim(timeframe) = ''
                   OR btrim(data_kind) = ''
            )::bigint AS missing_identity_count,
            count(*) FILTER (
                WHERE btrim(absolute_path) = '' OR absolute_path !~ '^/'
            )::bigint AS noncanonical_absolute_path_count,
            count(*) FILTER (
                WHERE btrim(relative_path) = ''
                   OR relative_path ~ '^/'
                   OR relative_path ~ '(^|/)\.\.(/|$)'
                   OR relative_path ~ '^\./'
                   OR strpos(relative_path, '//') > 0
                   OR strpos(relative_path, chr(92)) > 0
            )::bigint AS noncanonical_relative_path_count,
            count(*) FILTER (
                WHERE btrim(file_format) = ''
                   OR file_size < 0
                   OR file_sha256 !~ '^[0-9a-f]{64}$'
            )::bigint AS invalid_file_metadata_count,
            count(*) FILTER (
                WHERE row_count <= 0
                   OR first_open_at IS NULL
                   OR last_open_at IS NULL
                   OR last_close_at IS NULL
                   OR first_open_at > last_open_at
                   OR last_open_at > last_close_at
            )::bigint AS invalid_coverage_count,
            count(*) FILTER (
                WHERE gap_count <> 0
                   OR duplicate_count <> 0
                   OR null_count <> 0
            )::bigint AS invalid_content_quality_count,
            count(*) FILTER (
                WHERE last_close_at > transaction_timestamp()
                   OR observed_at < last_close_at
            )::bigint AS invalid_close_time_count,
            count(*) FILTER (
                WHERE freshness_status <> 'FRESH'
            )::bigint AS nonfresh_count,
            count(*) FILTER (
                WHERE btrim(scan_id) = '' OR observed_at IS NULL
            )::bigint AS missing_scan_evidence_count,
            count(*) FILTER (
                WHERE source_receipt_id IS NULL
            )::bigint AS missing_receipt_reference_count,
            min(first_open_at) AS earliest_open_at,
            max(last_open_at) AS latest_open_at,
            max(last_close_at) AS latest_close_at,
            max(observed_at) AS latest_observed_at,
            extract(epoch FROM (
                transaction_timestamp() - max(last_close_at)
            ))::bigint AS latest_close_age_seconds,
            sum(row_count)::numeric AS total_rows,
            sum(file_size)::numeric AS total_file_bytes
        FROM %I.%I
    )
    SELECT
        'market_data_file_record_summary'::text AS evidence_section,
        summary.*,
        CASE
            WHEN record_count = 0 THEN 'UNKNOWN'
            WHEN missing_identity_count = 0
             AND noncanonical_absolute_path_count = 0
             AND noncanonical_relative_path_count = 0
             AND invalid_file_metadata_count = 0
             AND invalid_coverage_count = 0
             AND invalid_content_quality_count = 0
             AND invalid_close_time_count = 0
             AND nonfresh_count = 0
             AND missing_scan_evidence_count = 0
             AND missing_receipt_reference_count = 0
                THEN 'PASSED'
            ELSE 'BLOCKED'
        END AS stored_record_status,
        'UNKNOWN_REQUIRES_CANONICAL_DATA_ROOT_INVENTORY'::text
            AS absolute_relative_root_reconciliation_status,
        'UNKNOWN_REQUIRES_FILE_RESCAN'::text
            AS file_bytes_digest_recomputation_status
    FROM summary
    $sql$,
    current_schema(), 'market_data_file_records'
)
WHERE to_regclass(
    format('%I.%I', current_schema(), 'market_data_file_records')
) IS NOT NULL
\gexec

SELECT format(
    $sql$
    SELECT
        'market_data_file_record_detail'::text AS evidence_section,
        id,
        exchange,
        market_type,
        pair,
        instrument_id,
        timeframe,
        data_kind,
        concat_ws('|', exchange, market_type, pair, timeframe, data_kind, relative_path)
            AS canonical_relative_source_identity,
        absolute_path,
        relative_path,
        file_format,
        file_size,
        file_sha256,
        row_count,
        first_open_at,
        last_open_at,
        last_close_at,
        gap_count,
        duplicate_count,
        null_count,
        freshness_status,
        scan_id,
        source_receipt_id,
        observed_at,
        CASE
            WHEN btrim(absolute_path) = '' OR absolute_path !~ '^/'
              OR btrim(relative_path) = '' OR relative_path ~ '^/'
              OR relative_path ~ '(^|/)\.\.(/|$)'
              OR relative_path ~ '^\./'
              OR strpos(relative_path, '//') > 0
              OR strpos(relative_path, chr(92)) > 0
                THEN 'BLOCKED'
            ELSE 'PASSED'
        END AS stored_path_contract_status,
        CASE
            WHEN file_sha256 ~ '^[0-9a-f]{64}$' THEN 'FORMAT_PASSED_NOT_RECOMPUTED'
            ELSE 'BLOCKED'
        END AS stored_digest_status,
        CASE
            WHEN row_count > 0
             AND first_open_at IS NOT NULL
             AND last_open_at IS NOT NULL
             AND last_close_at IS NOT NULL
             AND first_open_at <= last_open_at
             AND last_open_at <= last_close_at
                THEN 'PASSED'
            ELSE 'BLOCKED'
        END AS coverage_status
    FROM %I.%I
    ORDER BY exchange, pair, timeframe, data_kind, observed_at, id
    $sql$,
    current_schema(), 'market_data_file_records'
)
WHERE to_regclass(
    format('%I.%I', current_schema(), 'market_data_file_records')
) IS NOT NULL
\gexec

SELECT format(
    $sql$
    WITH exact_duplicates AS (
        SELECT 1
        FROM %I.%I
        GROUP BY exchange, market_type, pair, timeframe, data_kind,
                 relative_path, file_sha256, observed_at
        HAVING count(*) > 1
    ), relative_identity_collisions AS (
        SELECT 1
        FROM %I.%I
        GROUP BY exchange, market_type, pair, timeframe, data_kind,
                 file_sha256, observed_at
        HAVING count(DISTINCT relative_path) > 1
    ), absolute_identity_collisions AS (
        SELECT 1
        FROM %I.%I
        GROUP BY exchange, market_type, pair, timeframe, data_kind,
                 relative_path, file_sha256
        HAVING count(DISTINCT absolute_path) > 1
    )
    SELECT
        'market_data_file_identity_duplicates'::text AS evidence_section,
        (SELECT count(*) FROM exact_duplicates)::bigint
            AS exact_duplicate_group_count,
        (SELECT count(*) FROM relative_identity_collisions)::bigint
            AS digest_to_multiple_relative_identity_group_count,
        (SELECT count(*) FROM absolute_identity_collisions)::bigint
            AS relative_identity_to_multiple_absolute_path_group_count,
        CASE
            WHEN (SELECT count(*) FROM exact_duplicates) = 0
             AND (SELECT count(*) FROM relative_identity_collisions) = 0
             AND (SELECT count(*) FROM absolute_identity_collisions) = 0
                THEN 'PASSED'
            ELSE 'BLOCKED'
        END AS evidence_status
    $sql$,
    current_schema(), 'market_data_file_records',
    current_schema(), 'market_data_file_records',
    current_schema(), 'market_data_file_records'
)
WHERE to_regclass(
    format('%I.%I', current_schema(), 'market_data_file_records')
) IS NOT NULL
\gexec

SELECT format(
    $sql$
    WITH link_metrics AS (
        SELECT
            count(file_record.id)::bigint AS file_record_count,
            count(*) FILTER (
                WHERE file_record.source_receipt_id IS NULL
            )::bigint AS missing_receipt_reference_count,
            count(*) FILTER (
                WHERE file_record.source_receipt_id IS NOT NULL
                  AND receipt.id IS NULL
            )::bigint AS dangling_receipt_reference_count,
            count(*) FILTER (
                WHERE receipt.id IS NOT NULL AND receipt.status <> 'PASSED'
            )::bigint AS nonpassed_receipt_count,
            count(*) FILTER (
                WHERE receipt.id IS NOT NULL
                  AND (
                      receipt.exchange IS DISTINCT FROM file_record.exchange
                   OR receipt.pair IS DISTINCT FROM file_record.pair
                   OR receipt.timeframe IS DISTINCT FROM file_record.timeframe
                   OR receipt.relative_path IS DISTINCT FROM file_record.relative_path
                   OR receipt.file_format IS DISTINCT FROM file_record.file_format
                   OR receipt.file_size IS DISTINCT FROM file_record.file_size
                   OR receipt.file_sha256 IS DISTINCT FROM file_record.file_sha256
                   OR receipt.row_count IS DISTINCT FROM file_record.row_count
                   OR receipt.first_open_at IS DISTINCT FROM file_record.first_open_at
                   OR receipt.last_open_at IS DISTINCT FROM file_record.last_open_at
                  )
            )::bigint AS receipt_identity_or_coverage_mismatch_count,
            count(*) FILTER (
                WHERE receipt.id IS NOT NULL
                  AND (
                      receipt.evidence_digest !~ '^[0-9a-f]{64}$'
                   OR receipt.source_receipt_digest IS NULL
                   OR receipt.source_receipt_digest !~ '^[0-9a-f]{64}$'
                   OR receipt.source_response_chain_digest IS NULL
                   OR receipt.source_response_chain_digest !~ '^[0-9a-f]{64}$'
                  )
            )::bigint AS incomplete_receipt_digest_chain_count
        FROM %I.%I AS file_record
        LEFT JOIN %I.%I AS receipt
          ON receipt.id = file_record.source_receipt_id
    ), receipt_metrics AS (
        SELECT
            count(*)::bigint AS receipt_count,
            count(*) FILTER (
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM %I.%I AS file_record
                    WHERE file_record.source_receipt_id = receipt.id
                )
            )::bigint AS unreferenced_receipt_count
        FROM %I.%I AS receipt
    )
    SELECT
        'market_data_receipt_reconciliation'::text AS evidence_section,
        link_metrics.*,
        receipt_metrics.receipt_count,
        receipt_metrics.unreferenced_receipt_count,
        CASE
            WHEN link_metrics.file_record_count = 0
              OR receipt_metrics.receipt_count = 0 THEN 'UNKNOWN'
            WHEN link_metrics.missing_receipt_reference_count = 0
             AND link_metrics.dangling_receipt_reference_count = 0
             AND link_metrics.nonpassed_receipt_count = 0
             AND link_metrics.receipt_identity_or_coverage_mismatch_count = 0
             AND link_metrics.incomplete_receipt_digest_chain_count = 0
             AND receipt_metrics.unreferenced_receipt_count = 0
                THEN 'PASSED'
            ELSE 'BLOCKED'
        END AS evidence_status
    FROM link_metrics
    CROSS JOIN receipt_metrics
    $sql$,
    current_schema(), 'market_data_file_records',
    current_schema(), 'market_data_quality_receipts',
    current_schema(), 'market_data_file_records',
    current_schema(), 'market_data_quality_receipts'
)
WHERE to_regclass(
          format('%I.%I', current_schema(), 'market_data_file_records')
      ) IS NOT NULL
  AND to_regclass(
          format('%I.%I', current_schema(), 'market_data_quality_receipts')
      ) IS NOT NULL
\gexec

SELECT
    'market_data_receipt_reconciliation_availability'::text AS evidence_section,
    CASE
        WHEN to_regclass(
            format('%I.%I', current_schema(), 'market_data_file_records')
        ) IS NULL THEN 'UNKNOWN_MARKET_DATA_FILE_RECORDS_MISSING'
        WHEN to_regclass(
            format('%I.%I', current_schema(), 'market_data_quality_receipts')
        ) IS NULL THEN 'UNKNOWN_MARKET_DATA_QUALITY_RECEIPTS_MISSING'
        ELSE 'CHECKED_ABOVE'
    END AS evidence_status;

-- ---------------------------------------------------------------------------
-- PostgreSQL FK validation and generic orphan checks
-- ---------------------------------------------------------------------------

WITH foreign_keys AS (
    SELECT
        constraint_row.convalidated,
        lower(child_relation.relname) ~
            '(^|_)(secrets?|credentials?|passwords?|passphrases?|tokens?|api_keys?|access_keys?|private_keys?|auth(entication|orization)?)(_|$)'
        OR lower(parent_relation.relname) ~
            '(^|_)(secrets?|credentials?|passwords?|passphrases?|tokens?|api_keys?|access_keys?|private_keys?|auth(entication|orization)?)(_|$)'
        OR EXISTS (
            SELECT 1 FROM pg_attribute sensitive_attribute
            WHERE sensitive_attribute.attrelid IN (
                constraint_row.conrelid, constraint_row.confrelid
            )
              AND sensitive_attribute.attnum > 0
              AND NOT sensitive_attribute.attisdropped
              AND lower(sensitive_attribute.attname) ~
                  '(^|_)(secrets?|credentials?|passwords?|passphrases?|tokens?|api_keys?|access_keys?|private_keys?|auth(entication|orization)?)(_|$)'
        ) AS is_sensitive
    FROM pg_constraint constraint_row
    JOIN pg_class child_relation
      ON child_relation.oid = constraint_row.conrelid
    JOIN pg_namespace child_namespace
      ON child_namespace.oid = child_relation.relnamespace
    JOIN pg_class parent_relation
      ON parent_relation.oid = constraint_row.confrelid
    WHERE constraint_row.contype = 'f'
      AND child_namespace.nspname = current_schema()
)
SELECT
    'foreign_key_constraint_summary'::text AS evidence_section,
    count(*) FILTER (WHERE NOT is_sensitive)::bigint AS foreign_key_count,
    count(*) FILTER (WHERE is_sensitive)::bigint AS excluded_sensitive_count,
    count(*) FILTER (WHERE NOT is_sensitive AND NOT convalidated)::bigint
        AS not_validated_count,
    CASE
        WHEN count(*) FILTER (WHERE NOT is_sensitive) = 0 THEN 'UNKNOWN'
        WHEN count(*) FILTER (
            WHERE NOT is_sensitive AND NOT convalidated
        ) = 0
            THEN 'PASSED'
        ELSE 'BLOCKED'
    END AS evidence_status
FROM foreign_keys;

WITH foreign_keys AS (
    SELECT
        constraint_row.*,
        child_namespace.nspname AS child_schema,
        child_relation.relname AS child_table_name,
        parent_namespace.nspname AS parent_schema,
        parent_relation.relname AS parent_table_name,
        lower(child_relation.relname) ~
            '(^|_)(secrets?|credentials?|passwords?|passphrases?|tokens?|api_keys?|access_keys?|private_keys?|auth(entication|orization)?)(_|$)'
        OR lower(parent_relation.relname) ~
            '(^|_)(secrets?|credentials?|passwords?|passphrases?|tokens?|api_keys?|access_keys?|private_keys?|auth(entication|orization)?)(_|$)'
        OR EXISTS (
            SELECT 1 FROM pg_attribute sensitive_attribute
            WHERE sensitive_attribute.attrelid IN (
                constraint_row.conrelid, constraint_row.confrelid
            )
              AND sensitive_attribute.attnum > 0
              AND NOT sensitive_attribute.attisdropped
              AND lower(sensitive_attribute.attname) ~
                  '(^|_)(secrets?|credentials?|passwords?|passphrases?|tokens?|api_keys?|access_keys?|private_keys?|auth(entication|orization)?)(_|$)'
        ) AS is_sensitive
    FROM pg_constraint constraint_row
    JOIN pg_class child_relation
      ON child_relation.oid = constraint_row.conrelid
    JOIN pg_namespace child_namespace
      ON child_namespace.oid = child_relation.relnamespace
    JOIN pg_class parent_relation
      ON parent_relation.oid = constraint_row.confrelid
    JOIN pg_namespace parent_namespace
      ON parent_namespace.oid = parent_relation.relnamespace
    WHERE constraint_row.contype = 'f'
      AND child_namespace.nspname = current_schema()
)
SELECT
    'foreign_key_constraint_detail'::text AS evidence_section,
    conname AS constraint_name,
    format('%I.%I', child_schema, child_table_name) AS child_table,
    format('%I.%I', parent_schema, parent_table_name) AS parent_table,
    convalidated AS is_validated,
    condeferrable AS is_deferrable,
    condeferred AS is_initially_deferred,
    CASE WHEN is_sensitive THEN NULL
         ELSE pg_get_constraintdef(oid, TRUE) END AS constraint_definition,
    CASE WHEN is_sensitive THEN 'EXCLUDED_SENSITIVE_RELATION'
         WHEN convalidated THEN 'PASSED' ELSE 'BLOCKED' END
        AS validation_status
FROM foreign_keys
ORDER BY child_table, constraint_name;

-- MATCH SIMPLE exempts a row if any child key is NULL.  MATCH FULL exempts
-- only an all-NULL key.  Every generated statement below is a SELECT and emits
-- one auditable orphan count for one FK, including composite foreign keys.
WITH fk_columns AS (
    SELECT
        constraint_row.oid AS constraint_oid,
        constraint_row.conname AS constraint_name,
        constraint_row.confmatchtype AS match_type,
        child_namespace.nspname AS child_schema,
        child_relation.relname AS child_table,
        parent_namespace.nspname AS parent_schema,
        parent_relation.relname AS parent_table,
        lower(child_relation.relname) ~
            '(^|_)(secrets?|credentials?|passwords?|passphrases?|tokens?|api_keys?|access_keys?|private_keys?|auth(entication|orization)?)(_|$)'
        OR lower(parent_relation.relname) ~
            '(^|_)(secrets?|credentials?|passwords?|passphrases?|tokens?|api_keys?|access_keys?|private_keys?|auth(entication|orization)?)(_|$)'
        OR EXISTS (
            SELECT 1 FROM pg_attribute sensitive_attribute
            WHERE sensitive_attribute.attrelid IN (
                constraint_row.conrelid, constraint_row.confrelid
            )
              AND sensitive_attribute.attnum > 0
              AND NOT sensitive_attribute.attisdropped
              AND lower(sensitive_attribute.attname) ~
                  '(^|_)(secrets?|credentials?|passwords?|passphrases?|tokens?|api_keys?|access_keys?|private_keys?|auth(entication|orization)?)(_|$)'
        ) AS is_sensitive,
        child_key.ordinality AS key_ordinality,
        child_attribute.attname AS child_column,
        parent_attribute.attname AS parent_column
    FROM pg_constraint constraint_row
    JOIN pg_class child_relation
      ON child_relation.oid = constraint_row.conrelid
    JOIN pg_namespace child_namespace
      ON child_namespace.oid = child_relation.relnamespace
    JOIN pg_class parent_relation
      ON parent_relation.oid = constraint_row.confrelid
    JOIN pg_namespace parent_namespace
      ON parent_namespace.oid = parent_relation.relnamespace
    CROSS JOIN LATERAL unnest(constraint_row.conkey)
        WITH ORDINALITY AS child_key(attnum, ordinality)
    JOIN LATERAL unnest(constraint_row.confkey)
        WITH ORDINALITY AS parent_key(attnum, ordinality)
      ON parent_key.ordinality = child_key.ordinality
    JOIN pg_attribute child_attribute
      ON child_attribute.attrelid = constraint_row.conrelid
     AND child_attribute.attnum = child_key.attnum
    JOIN pg_attribute parent_attribute
      ON parent_attribute.attrelid = constraint_row.confrelid
     AND parent_attribute.attnum = parent_key.attnum
    WHERE constraint_row.contype = 'f'
      AND child_namespace.nspname = current_schema()
), fk_checks AS (
    SELECT
        constraint_oid,
        constraint_name,
        match_type,
        child_schema,
        child_table,
        parent_schema,
        parent_table,
        is_sensitive,
        string_agg(
            format('parent_row.%I = child_row.%I', parent_column, child_column),
            ' AND ' ORDER BY key_ordinality
        ) AS join_predicate,
        CASE
            WHEN match_type = 'f' THEN
                'NOT (' || string_agg(
                    format('child_row.%I IS NULL', child_column),
                    ' AND ' ORDER BY key_ordinality
                ) || ')'
            ELSE string_agg(
                format('child_row.%I IS NOT NULL', child_column),
                ' AND ' ORDER BY key_ordinality
            )
        END AS child_key_predicate
    FROM fk_columns
    GROUP BY constraint_oid, constraint_name, match_type,
             child_schema, child_table, parent_schema, parent_table, is_sensitive
)
SELECT CASE
    WHEN is_sensitive THEN format(
        $sql$
        SELECT
            'foreign_key_orphan_check'::text AS evidence_section,
            %L::text AS constraint_name,
            %L::text AS child_table,
            %L::text AS parent_table,
            NULL::bigint AS orphan_count,
            'EXCLUDED_SENSITIVE_RELATION'::text AS evidence_status
        $sql$,
        constraint_name,
        format('%I.%I', child_schema, child_table),
        format('%I.%I', parent_schema, parent_table)
    )
    ELSE format(
        $sql$
        SELECT
            'foreign_key_orphan_check'::text AS evidence_section,
            %L::text AS constraint_name,
            %L::text AS child_table,
            %L::text AS parent_table,
            count(*)::bigint AS orphan_count,
            CASE WHEN count(*) = 0 THEN 'PASSED' ELSE 'BLOCKED' END
                AS evidence_status
        FROM %I.%I AS child_row
        WHERE %s
          AND NOT EXISTS (
              SELECT 1
              FROM %I.%I AS parent_row
              WHERE %s
          )
        $sql$,
        constraint_name,
        format('%I.%I', child_schema, child_table),
        format('%I.%I', parent_schema, parent_table),
        child_schema,
        child_table,
        child_key_predicate,
        parent_schema,
        parent_table,
        join_predicate
    )
END
FROM fk_checks
ORDER BY child_schema, child_table, constraint_name
\gexec

-- ---------------------------------------------------------------------------
-- Migration runs, BEFORE/AFTER snapshots, mappings, and conflicts
-- ---------------------------------------------------------------------------

SELECT format(
    $sql$
    SELECT
        'strategy_platform_migration_run'::text AS evidence_section,
        id AS migration_run_id,
        migration_key,
        execution_scope,
        source_schema_version,
        target_schema_version,
        source_snapshot_digest,
        target_snapshot_digest,
        status,
        destructive_write_count,
        overwritten_row_count,
        deleted_row_count,
        unknown_dimensions,
        report_digest,
        error_code,
        started_at,
        completed_at,
        CASE
            WHEN destructive_write_count <> 0
              OR overwritten_row_count <> 0
              OR deleted_row_count <> 0 THEN 'BLOCKED'
            WHEN status IN ('FAILED', 'BLOCKED') THEN 'BLOCKED'
            WHEN status = 'SUCCEEDED'
             AND source_snapshot_digest ~ '^[0-9a-f]{64}$'
             AND target_snapshot_digest ~ '^[0-9a-f]{64}$'
             AND report_digest ~ '^[0-9a-f]{64}$'
             AND unknown_dimensions::jsonb = '[]'::jsonb THEN 'PASSED'
            ELSE 'UNKNOWN'
        END AS audit_record_status
    FROM %I.%I
    ORDER BY id
    $sql$,
    current_schema(), 'strategy_platform_migration_runs'
)
WHERE to_regclass(
    format('%I.%I', current_schema(), 'strategy_platform_migration_runs')
) IS NOT NULL
\gexec

SELECT format(
    $sql$
    SELECT
        'strategy_platform_migration_snapshot'::text AS evidence_section,
        migration_run_id,
        snapshot_phase,
        table_name,
        row_count,
        minimum_id,
        maximum_id,
        orphan_count,
        content_digest,
        observed_at,
        CASE
            WHEN row_count < 0 OR orphan_count <> 0 THEN 'BLOCKED'
            WHEN content_digest IS NULL THEN 'UNKNOWN'
            WHEN content_digest ~ '^[0-9a-f]{64}$' THEN 'PASSED'
            ELSE 'BLOCKED'
        END AS snapshot_status
    FROM %I.%I
    ORDER BY migration_run_id, table_name, snapshot_phase
    $sql$,
    current_schema(), 'strategy_platform_migration_table_snapshots'
)
WHERE to_regclass(
    format(
        '%I.%I', current_schema(),
        'strategy_platform_migration_table_snapshots'
    )
) IS NOT NULL
\gexec

SELECT format(
    $sql$
    WITH before_snapshot AS (
        SELECT *
        FROM %I.%I
        WHERE snapshot_phase = 'BEFORE'
    ), after_snapshot AS (
        SELECT *
        FROM %I.%I
        WHERE snapshot_phase = 'AFTER'
    )
    SELECT
        'strategy_platform_before_after_reconciliation'::text
            AS evidence_section,
        COALESCE(before_snapshot.migration_run_id, after_snapshot.migration_run_id)
            AS migration_run_id,
        COALESCE(before_snapshot.table_name, after_snapshot.table_name)
            AS table_name,
        before_snapshot.row_count AS before_row_count,
        after_snapshot.row_count AS after_row_count,
        after_snapshot.row_count - before_snapshot.row_count AS row_count_delta,
        before_snapshot.minimum_id AS before_minimum_id,
        after_snapshot.minimum_id AS after_minimum_id,
        before_snapshot.maximum_id AS before_maximum_id,
        after_snapshot.maximum_id AS after_maximum_id,
        before_snapshot.content_digest AS before_content_digest,
        after_snapshot.content_digest AS after_content_digest,
        before_snapshot.orphan_count AS before_orphan_count,
        after_snapshot.orphan_count AS after_orphan_count,
        CASE
            WHEN before_snapshot.id IS NULL OR after_snapshot.id IS NULL
                THEN 'UNKNOWN_MISSING_PHASE'
            WHEN after_snapshot.row_count < before_snapshot.row_count
              OR after_snapshot.orphan_count <> 0 THEN 'BLOCKED'
            ELSE 'PASSED'
        END AS reconciliation_status
    FROM before_snapshot
    FULL OUTER JOIN after_snapshot
      ON after_snapshot.migration_run_id = before_snapshot.migration_run_id
     AND after_snapshot.table_name = before_snapshot.table_name
    ORDER BY migration_run_id, table_name
    $sql$,
    current_schema(), 'strategy_platform_migration_table_snapshots',
    current_schema(), 'strategy_platform_migration_table_snapshots'
)
WHERE to_regclass(
    format(
        '%I.%I', current_schema(),
        'strategy_platform_migration_table_snapshots'
    )
) IS NOT NULL
\gexec

SELECT format(
    $sql$
    WITH mapping_summary AS (
        SELECT
            migration_run_id,
            count(*)::bigint AS mapping_count,
            count(*) FILTER (
                WHERE mapping_status IN ('UNMAPPED', 'AMBIGUOUS')
            )::bigint AS unresolved_mapping_count,
            count(*) FILTER (
                WHERE mapping_status IN ('MAPPED', 'PRESERVED')
                  AND (target_table IS NULL OR target_primary_key IS NULL)
            )::bigint AS incomplete_target_identity_count,
            count(*) FILTER (
                WHERE target_table IS NOT NULL
                  AND to_regclass(
                      format('%%I.%%I', current_schema(), target_table)
                  ) IS NULL
            )::bigint AS missing_target_table_count,
            count(*) FILTER (
                WHERE source_digest IS NOT NULL
                  AND target_digest IS NOT NULL
                  AND source_digest IS DISTINCT FROM target_digest
            )::bigint AS transformed_digest_count,
            count(*) FILTER (
                WHERE quality_status_asserted = 'QUALIFIED'
                  AND dynamic_quality_evidence_id IS NULL
            )::bigint AS qualified_without_dynamic_evidence_count
        FROM %I.%I
        GROUP BY migration_run_id
    )
    SELECT
        'strategy_platform_migration_mapping_summary'::text
            AS evidence_section,
        mapping_summary.*,
        CASE
            WHEN mapping_count = 0 THEN 'UNKNOWN'
            WHEN unresolved_mapping_count = 0
             AND incomplete_target_identity_count = 0
             AND missing_target_table_count = 0
             AND qualified_without_dynamic_evidence_count = 0 THEN 'PASSED'
            ELSE 'BLOCKED'
        END AS evidence_status
    FROM mapping_summary
    ORDER BY migration_run_id
    $sql$,
    current_schema(), 'strategy_platform_migration_entity_mappings'
)
WHERE to_regclass(
    format(
        '%I.%I', current_schema(),
        'strategy_platform_migration_entity_mappings'
    )
) IS NOT NULL
\gexec

SELECT format(
    $sql$
    SELECT
        'strategy_platform_migration_mapping_detail'::text
            AS evidence_section,
        migration_run_id,
        source_table,
        mapping_kind,
        target_table,
        mapping_status,
        quality_status_asserted,
        count(*)::bigint AS mapping_count,
        count(*) FILTER (
            WHERE source_digest IS NULL
        )::bigint AS missing_source_digest_count,
        count(*) FILTER (
            WHERE target_table IS NOT NULL AND target_digest IS NULL
        )::bigint AS missing_target_digest_count
    FROM %I.%I
    GROUP BY migration_run_id, source_table, mapping_kind, target_table,
             mapping_status, quality_status_asserted
    ORDER BY migration_run_id, source_table, mapping_kind, mapping_status
    $sql$,
    current_schema(), 'strategy_platform_migration_entity_mappings'
)
WHERE to_regclass(
    format(
        '%I.%I', current_schema(),
        'strategy_platform_migration_entity_mappings'
    )
) IS NOT NULL
\gexec

SELECT format(
    $sql$
    SELECT
        'strategy_platform_migration_conflict_summary'::text
            AS evidence_section,
        migration_run_id,
        count(*)::bigint AS conflict_count,
        count(*) FILTER (WHERE status = 'OPEN')::bigint AS open_count,
        count(*) FILTER (WHERE status = 'BLOCKED')::bigint AS blocked_count,
        count(*) FILTER (WHERE status = 'PRESERVED')::bigint AS preserved_count,
        count(*) FILTER (WHERE status = 'RESOLVED')::bigint AS resolved_count,
        CASE
            WHEN count(*) FILTER (WHERE status IN ('OPEN', 'BLOCKED')) = 0
                THEN 'PASSED'
            ELSE 'BLOCKED'
        END AS evidence_status
    FROM %I.%I
    GROUP BY migration_run_id
    ORDER BY migration_run_id
    $sql$,
    current_schema(), 'strategy_platform_migration_conflicts'
)
WHERE to_regclass(
    format(
        '%I.%I', current_schema(), 'strategy_platform_migration_conflicts'
    )
) IS NOT NULL
\gexec

SELECT format(
    $sql$
    SELECT
        'strategy_platform_migration_conflict_detail'::text
            AS evidence_section,
        migration_run_id,
        entity_kind,
        status,
        reason_code,
        count(*)::bigint AS conflict_count
    FROM %I.%I
    GROUP BY migration_run_id, entity_kind, status, reason_code
    ORDER BY migration_run_id, status, entity_kind, reason_code
    $sql$,
    current_schema(), 'strategy_platform_migration_conflicts'
)
WHERE to_regclass(
    format(
        '%I.%I', current_schema(), 'strategy_platform_migration_conflicts'
    )
) IS NOT NULL
\gexec

SELECT format(
    $sql$
    SELECT
        'market_data_before_after_snapshot'::text AS evidence_section,
        migration_run_id,
        snapshot_phase,
        table_name,
        row_count,
        minimum_id,
        maximum_id,
        orphan_count,
        content_digest,
        observed_at
    FROM %I.%I
    WHERE table_name IN (
        'market_data_file_records', 'market_data_quality_receipts'
    )
    ORDER BY migration_run_id, table_name, snapshot_phase
    $sql$,
    current_schema(), 'strategy_platform_migration_table_snapshots'
)
WHERE to_regclass(
    format(
        '%I.%I', current_schema(),
        'strategy_platform_migration_table_snapshots'
    )
) IS NOT NULL
\gexec

-- ---------------------------------------------------------------------------
-- Immutable configuration bundle referential and digest evidence
-- ---------------------------------------------------------------------------

SELECT format(
    $sql$
    WITH summary AS (
        SELECT
            count(*)::bigint AS bundle_count,
            count(*) FILTER (
                WHERE bundle_digest !~ '^[0-9a-f]{64}$'
            )::bigint AS invalid_stored_bundle_digest_count,
            count(*) FILTER (
                WHERE jsonb_typeof(resolved_versions_json::jsonb) <> 'object'
                   OR resolved_versions_json::jsonb = '{}'::jsonb
                   OR jsonb_typeof(resolved_digests_json::jsonb) <> 'object'
            )::bigint AS invalid_resolved_map_count,
            count(*) FILTER (
                WHERE NOT (
                    capability_snapshot::jsonb @> '{"demo_only": true}'::jsonb
                    AND capability_snapshot::jsonb @>
                        '{"allow_real_funds": false}'::jsonb
                    AND capability_snapshot::jsonb @>
                        '{"single_writer_required": true}'::jsonb
                )
            )::bigint AS unsafe_capability_snapshot_count
        FROM %I.%I
    )
    SELECT
        'configuration_bundle_shape_summary'::text AS evidence_section,
        summary.*,
        CASE
            WHEN bundle_count = 0 THEN 'UNKNOWN'
            WHEN invalid_stored_bundle_digest_count = 0
             AND invalid_resolved_map_count = 0
             AND unsafe_capability_snapshot_count = 0 THEN 'PASSED'
            ELSE 'BLOCKED'
        END AS stored_shape_status,
        'UNKNOWN_REQUIRES_CANONICAL_BUNDLE_DIGEST_RECOMPUTATION'::text
            AS bundle_digest_recomputation_status
    FROM summary
    $sql$,
    current_schema(), 'configuration_bundle_snapshots'
)
WHERE to_regclass(
    format('%I.%I', current_schema(), 'configuration_bundle_snapshots')
) IS NOT NULL
\gexec

SELECT format(
    $sql$
    WITH bundle_rows AS (
        SELECT
            bundle.*,
            CASE
                WHEN jsonb_typeof(bundle.resolved_versions_json::jsonb) = 'object'
                    THEN bundle.resolved_versions_json::jsonb
                ELSE '{}'::jsonb
            END AS version_map,
            CASE
                WHEN jsonb_typeof(bundle.resolved_digests_json::jsonb) = 'object'
                    THEN bundle.resolved_digests_json::jsonb
                ELSE '{}'::jsonb
            END AS digest_map
        FROM %I.%I AS bundle
    )
    SELECT
        'configuration_bundle_resolution'::text AS evidence_section,
        bundle_rows.id AS bundle_id,
        bundle_rows.workflow_kind,
        bundle_rows.scope_type,
        bundle_rows.scope_key,
        bundle_rows.aggregate_profile_version_id,
        bundle_rows.bundle_digest,
        count(version_entry.key)::bigint AS resolved_entry_count,
        count(*) FILTER (
            WHERE version_entry.key IS NOT NULL
              AND version_entry.value !~ '^[0-9]+$'
        )::bigint AS invalid_version_id_count,
        count(*) FILTER (
            WHERE version_entry.key IS NOT NULL
              AND resolved_version.id IS NULL
        )::bigint AS orphan_resolved_version_count,
        count(*) FILTER (
            WHERE resolved_version.id IS NOT NULL
              AND resolved_version.lifecycle_status <> 'VALIDATED'
        )::bigint AS nonvalidated_resolved_version_count,
        count(*) FILTER (
            WHERE resolved_version.id IS NOT NULL
              AND resolved_version.type_key IS DISTINCT FROM version_entry.key
        )::bigint AS resolved_type_key_mismatch_count,
        count(*) FILTER (
            WHERE version_entry.key IS NOT NULL
              AND NOT (bundle_rows.digest_map ? version_entry.key)
        )::bigint AS missing_resolved_digest_count,
        count(*) FILTER (
            WHERE resolved_version.id IS NOT NULL
              AND resolved_version.config_digest IS DISTINCT FROM
                  bundle_rows.digest_map ->> version_entry.key
        )::bigint AS resolved_digest_mismatch_count,
        (
            SELECT count(*)
            FROM jsonb_object_keys(bundle_rows.digest_map) AS digest_key(key)
            WHERE NOT (bundle_rows.version_map ? digest_key.key)
        )::bigint AS extra_resolved_digest_count,
        CASE
            WHEN aggregate_version.id IS NULL THEN 'ORPHAN'
            WHEN aggregate_version.lifecycle_status <> 'VALIDATED'
                THEN 'NOT_VALIDATED'
            ELSE 'VALID'
        END AS aggregate_profile_status,
        CASE
            WHEN count(version_entry.key) = 0 THEN 'UNKNOWN'
            WHEN aggregate_version.id IS NULL
              OR aggregate_version.lifecycle_status <> 'VALIDATED'
              OR count(*) FILTER (
                    WHERE version_entry.key IS NOT NULL
                      AND (
                          version_entry.value !~ '^[0-9]+$'
                          OR resolved_version.id IS NULL
                          OR resolved_version.lifecycle_status <> 'VALIDATED'
                          OR resolved_version.type_key IS DISTINCT FROM version_entry.key
                          OR NOT (bundle_rows.digest_map ? version_entry.key)
                          OR resolved_version.config_digest IS DISTINCT FROM
                              bundle_rows.digest_map ->> version_entry.key
                      )
                 ) <> 0
              OR (
                    SELECT count(*)
                    FROM jsonb_object_keys(bundle_rows.digest_map)
                        AS digest_key(key)
                    WHERE NOT (bundle_rows.version_map ? digest_key.key)
                 ) <> 0 THEN 'BLOCKED'
            ELSE 'PASSED'
        END AS referential_evidence_status,
        'UNKNOWN_NOT_RECOMPUTED_BY_THIS_SQL_TEMPLATE'::text
            AS bundle_digest_recomputation_status
    FROM bundle_rows
    LEFT JOIN LATERAL jsonb_each_text(bundle_rows.version_map)
        AS version_entry(key, value) ON TRUE
    LEFT JOIN %I.%I AS resolved_version
      ON resolved_version.id::text = version_entry.value
    LEFT JOIN %I.%I AS aggregate_version
      ON aggregate_version.id = bundle_rows.aggregate_profile_version_id
    GROUP BY bundle_rows.id, bundle_rows.workflow_kind, bundle_rows.scope_type,
             bundle_rows.scope_key, bundle_rows.aggregate_profile_version_id,
             bundle_rows.bundle_digest, bundle_rows.version_map,
             bundle_rows.digest_map, aggregate_version.id,
             aggregate_version.lifecycle_status
    ORDER BY bundle_rows.id
    $sql$,
    current_schema(), 'configuration_bundle_snapshots',
    current_schema(), 'configuration_versions',
    current_schema(), 'configuration_versions'
)
WHERE to_regclass(
          format('%I.%I', current_schema(), 'configuration_bundle_snapshots')
      ) IS NOT NULL
  AND to_regclass(
          format('%I.%I', current_schema(), 'configuration_versions')
      ) IS NOT NULL
\gexec

SELECT
    'configuration_bundle_resolution_availability'::text AS evidence_section,
    CASE
        WHEN to_regclass(
            format('%I.%I', current_schema(), 'configuration_bundle_snapshots')
        ) IS NULL THEN 'UNKNOWN_CONFIGURATION_BUNDLE_SNAPSHOTS_MISSING'
        WHEN to_regclass(
            format('%I.%I', current_schema(), 'configuration_versions')
        ) IS NULL THEN 'UNKNOWN_CONFIGURATION_VERSIONS_MISSING'
        ELSE 'CHECKED_ABOVE'
    END AS evidence_status;

-- SQL alone cannot prove filesystem bytes, canonical data-root containment, or
-- receipt artifact bytes.  Those dimensions must stay UNKNOWN until the
-- companion inventory CLI provides fresh, independently hashed evidence.
SELECT *
FROM (
    VALUES
        (
            'evidence_boundary',
            'canonical_data_root_and_absolute_relative_resolution',
            'UNKNOWN',
            'Requires the Task 1 market-data inventory CLI output.'
        ),
        (
            'evidence_boundary',
            'file_size_sha256_row_count_and_close_time_rescan',
            'UNKNOWN',
            'Stored PostgreSQL values were reported but file bytes were not read.'
        ),
        (
            'evidence_boundary',
            'source_receipt_artifact_digest_recomputation',
            'UNKNOWN',
            'Stored receipt links were reconciled but receipt files were not read.'
        ),
        (
            'evidence_boundary',
            'configuration_bundle_digest_recomputation',
            'UNKNOWN',
            'Stored maps and digests were checked; canonical application digest must be recomputed separately.'
        )
) AS boundary(evidence_section, evidence_dimension, evidence_status, reason)
ORDER BY evidence_dimension;

ROLLBACK;
