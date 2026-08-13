\set ON_ERROR_STOP on

-- Read-only V1.3 Task 1 acceptance evidence.  Run with:
--   psql -X -d freqtrade_ai_design_lab -f backend/scripts/strategy_platform_v13_task1_reconciliation.sql
-- It does not read credential/secret row contents.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

SELECT clock_timestamp() AS observed_at,
       current_database() AS database_name,
       current_schema() AS schema_name,
       current_user AS role_name,
       pg_size_pretty(pg_database_size(current_database())) AS database_size;

SELECT version, applied_at
FROM freqtrade_ai_schema_migrations
ORDER BY applied_at DESC, version DESC
LIMIT 3;

SELECT table_name, row_count
FROM (
    SELECT 'strategies' AS table_name, count(*)::bigint AS row_count FROM strategies
    UNION ALL SELECT 'strategy_versions', count(*) FROM strategy_versions
    UNION ALL SELECT 'strategy_targets', count(*) FROM strategy_targets
    UNION ALL SELECT 'backtest_runs', count(*) FROM backtest_runs
    UNION ALL SELECT 'backtest_tasks', count(*) FROM backtest_tasks
    UNION ALL SELECT 'backtest_results', count(*) FROM backtest_results
    UNION ALL SELECT 'strategy_validation_plans', count(*) FROM strategy_validation_plans
    UNION ALL SELECT 'strategy_validation_windows', count(*) FROM strategy_validation_windows
    UNION ALL SELECT 'strategy_scores', count(*) FROM strategy_scores
    UNION ALL SELECT 'validation_window_scores', count(*) FROM validation_window_scores
    UNION ALL SELECT 'strategy_evaluation_summaries', count(*) FROM strategy_evaluation_summaries
    UNION ALL SELECT 'strategy_research_candidates', count(*) FROM strategy_research_candidates
    UNION ALL SELECT 'strategy_candidate_approvals', count(*) FROM strategy_candidate_approvals
    UNION ALL SELECT 'strategy_deployments', count(*) FROM strategy_deployments
    UNION ALL SELECT 'signal_evaluations', count(*) FROM signal_evaluations
    UNION ALL SELECT 'trade_intents', count(*) FROM trade_intents
    UNION ALL SELECT 'exchange_orders', count(*) FROM exchange_orders
    UNION ALL SELECT 'exchange_fills', count(*) FROM exchange_fills
    UNION ALL SELECT 'exchange_positions', count(*) FROM exchange_positions
    UNION ALL SELECT 'market_data_file_records', count(*) FROM market_data_file_records
) counts
ORDER BY table_name;

SELECT run.id, run.execution_scope, run.status, run.source_schema_version,
       run.target_schema_version, run.source_snapshot_digest,
       run.target_snapshot_digest, run.destructive_write_count,
       run.overwritten_row_count, run.deleted_row_count,
       run.unknown_dimensions, run.started_at, run.completed_at
FROM strategy_platform_migration_runs run
ORDER BY run.id;

SELECT mapping_status, mapping_kind, quality_status_asserted, count(*) AS row_count
FROM strategy_platform_migration_entity_mappings
GROUP BY mapping_status, mapping_kind, quality_status_asserted
ORDER BY mapping_status, mapping_kind, quality_status_asserted;

SELECT count(*) FILTER (WHERE mapping_status IN ('UNMAPPED', 'AMBIGUOUS'))
           AS unresolved_mapping_count,
       count(*) FILTER (
           WHERE mapping_kind = 'LEGACY_ROW_PRESERVED'
             AND source_digest IS DISTINCT FROM target_digest
       ) AS preserved_digest_mismatch_count,
       count(*) FILTER (WHERE quality_status_asserted = 'QUALIFIED')
           AS qualified_mapping_count
FROM strategy_platform_migration_entity_mappings;

SELECT count(*) AS open_or_blocked_conflict_count
FROM strategy_platform_migration_conflicts
WHERE status IN ('OPEN', 'BLOCKED');

SELECT count(*) AS unvalidated_constraint_count
FROM pg_constraint constraint_row
JOIN pg_class table_row ON table_row.oid = constraint_row.conrelid
JOIN pg_namespace namespace_row ON namespace_row.oid = table_row.relnamespace
WHERE namespace_row.nspname = current_schema()
  AND NOT constraint_row.convalidated;

SELECT count(*) AS version_without_target_count
FROM strategy_versions version
WHERE NOT EXISTS (
    SELECT 1 FROM strategy_targets target
    WHERE target.strategy_version_id = version.id
);

SELECT count(*) AS legacy_qualified_summary_count
FROM strategy_evaluation_summaries summary
JOIN strategy_validation_plans plan ON plan.id = summary.validation_plan_id
WHERE summary.status = 'QUALIFIED'
  AND plan.trigger_metadata::jsonb @> jsonb_build_object(
      'migration', 'strategy-platform-v13-task1-real-data-v1'
  );

SELECT count(*) AS strategy_score_association_gap_count
FROM strategy_scores score
LEFT JOIN strategy_platform_migration_entity_mappings mapping
  ON mapping.migration_run_id = (
      SELECT id FROM strategy_platform_migration_runs
      WHERE status = 'SUCCEEDED' ORDER BY id DESC LIMIT 1
  )
 AND mapping.source_table = 'strategy_scores'
 AND mapping.source_primary_key = score.id::text
 AND mapping.mapping_kind = 'WINDOW_SCORE_ASSOCIATION'
WHERE mapping.id IS NULL
   OR mapping.mapping_status NOT IN ('MAPPED', 'NOT_APPLICABLE')
   OR mapping.quality_status_asserted IS DISTINCT FROM 'UNKNOWN';

SELECT count(*) AS deployment_target_mapping_gap_count
FROM strategy_deployments deployment
LEFT JOIN strategy_targets target ON target.id = deployment.strategy_target_id
LEFT JOIN execution_target_definitions definition
  ON definition.id = target.execution_target_id
LEFT JOIN strategy_platform_migration_entity_mappings mapping
  ON mapping.migration_run_id = (
      SELECT id FROM strategy_platform_migration_runs
      WHERE status = 'SUCCEEDED' ORDER BY id DESC LIMIT 1
  )
 AND mapping.source_table = 'strategy_deployments'
 AND mapping.source_primary_key = deployment.id::text
 AND mapping.mapping_kind = 'LEGACY_DEMO_DEPLOYMENT_TARGET'
WHERE definition.target_key IS DISTINCT FROM 'OKX_DEMO'
   OR deployment.real_orders IS DISTINCT FROM FALSE
   OR target.strategy_version_id IS DISTINCT FROM deployment.strategy_version_id
   OR target.instrument_id IS DISTINCT FROM deployment.instrument_id
   OR target.timeframe IS DISTINCT FROM deployment.timeframe
   OR mapping.mapping_status IS DISTINCT FROM 'MAPPED'
   OR mapping.target_primary_key IS DISTINCT FROM target.id::text;

SELECT count(*) AS trade_intent_lineage_audit_gap_count
FROM trade_intents intent
LEFT JOIN strategy_platform_migration_entity_mappings mapping
  ON mapping.migration_run_id = (
      SELECT id FROM strategy_platform_migration_runs
      WHERE status = 'SUCCEEDED' ORDER BY id DESC LIMIT 1
  )
 AND mapping.source_table = 'trade_intents'
 AND mapping.source_primary_key = intent.id::text
 AND mapping.mapping_kind = 'LEGACY_SIGNAL_DEPLOYMENT_LINEAGE'
WHERE mapping.id IS NULL
   OR mapping.quality_status_asserted IS DISTINCT FROM 'UNKNOWN'
   OR mapping.mapping_status NOT IN ('PRESERVED', 'NOT_APPLICABLE')
   OR (mapping.mapping_status = 'PRESERVED'
       AND (mapping.target_table IS DISTINCT FROM 'signal_evaluations'
            OR mapping.target_primary_key IS NULL))
   OR (mapping.mapping_status = 'NOT_APPLICABLE'
       AND (mapping.target_table IS NOT NULL
            OR mapping.target_primary_key IS NOT NULL));

SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = current_schema()
  AND grantee = 'freqtrade'
  AND table_name IN (
      'configuration_versions',
      'configuration_bundle_snapshots',
      'strategy_targets',
      'strategy_platform_migration_runs'
  )
ORDER BY table_name, privilege_type;

SELECT 'OUT_OF_SCOPE' AS credential_attestation,
       'UNKNOWN' AS runtime_execution_evidence,
       'NOT_ACCESSED' AS orders_signals_deployment_action;

ROLLBACK;
