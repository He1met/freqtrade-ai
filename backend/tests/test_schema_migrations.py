import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql

from app.core.exceptions import ConfigurationError
from app.db.migrations import (
    RUNTIME_APPEND_ONLY_TABLES,
    BRIDGE_APPEND_ONLY_TABLES,
    ATTESTATION_ACL_BASE_VERSION,
    ATTESTED_SESSION_BASE_VERSION,
    CANARY_LINEAGE_WRITE_BASE_VERSION,
    CANARY_FINAL_EXPIRY_BASE_VERSION,
    CANARY_LIFECYCLE_BASE_VERSION,
    CANARY_CONSENT_HANDOFF_BASE_VERSION,
    CANARY_CONSENT_FAILURE_AUDIT_BASE_VERSION,
    CANARY_ATOMIC_PREPARE_BASE_VERSION,
    ACCEPTED_NOT_FOUND_TERMINALIZATION_BASE_VERSION,
    BOUNDED_SECOND_ACCEPTANCE_BASE_VERSION,
    FINAL_ACCEPTANCE_BASE_VERSION,
    CONTINUOUS_DEMO_BASE_VERSION,
    CONTINUOUS_DEMO_SELECTION_V2_BASE_VERSION,
    RESEARCH_PERSISTENCE_BASE_VERSION,
    CANDIDATE_BRIDGE_BASE_VERSION,
    NATURAL_SIGNAL_RISK_CHAIN_BASE_VERSION,
    MULTI_ASSET_CAPACITY_BASE_VERSION,
    AUTOMATION_GUARD_REBIND_BASE_VERSION,
    DEPLOYMENT_POLICY_REBIND_BASE_VERSION,
    NATURAL_SIGNAL_EVALUATOR_RECEIPT_BASE_VERSION,
    NATURAL_SIGNAL_RISK_BUDGET_BASE_VERSION,
    STALE_NATURAL_APPROVAL_RELEASE_BASE_VERSION,
    STRATEGY_PLATFORM_V1_BASE_VERSION,
    EARLY_TARGET_LINEAGE_VERSION,
    DUAL_SIDE_BASE_VERSION,
    FULL_CHAIN_BASE_VERSION,
    HMAC_ATTESTATION_BASE_VERSION,
    LEGACY_SCHEMA_VERSION,
    ORDER_WRITER_BASE_VERSION,
    PREVIOUS_SCHEMA_VERSION,
    RECONCILIATION_INDEX_BASE_VERSION,
    RECONCILIATION_BATCH_FRESHNESS_BASE_VERSION,
    RECONCILIATION_BASE_VERSION,
    RECOVERY_WALL_CLOCK_BASE_VERSION,
    RISK_CHAIN_BASE_VERSION,
    RISK_CHAIN_HARDENING_BASE_VERSION,
    RUNTIME_APP_ACL_BASE_VERSION,
    FILL_SNAPSHOT_REPEAT_BASE_VERSION,
    RUNTIME_RECOVERY_BASE_VERSION,
    STRATEGY_PROMOTION_BASE_VERSION,
    STRATEGY_DEPLOYMENT_BASE_VERSION,
    SINGLE_ACTIVE_DEPLOYMENT_BASE_VERSION,
    STRATEGY_VALIDATION_BASE_VERSION,
    STRATEGY_PLATFORM_V13_TASK1_CRITICAL_CHECK_DEFINITIONS,
    STRATEGY_PLATFORM_V13_GUARD_FUNCTIONS,
    STRATEGY_PLATFORM_V13_LEGACY_CAPABILITY_FUNCTIONS,
    STRATEGY_PLATFORM_V13_LEGACY_CAPABILITY_SEQUENCES,
    STRATEGY_PLATFORM_V13_LEGACY_CAPABILITY_TABLES,
    STRATEGY_PLATFORM_V13_OWNER_ONLY_TABLES,
    STRATEGY_PLATFORM_V13_OWNER_READ_TABLES,
    SCHEMA_VERSION,
    _critical_check_definition_problems,
    _demo_automation_policy_digest,
    _normalized_index_definition,
    _normalized_sql_definition,
    _rebind_demo_automation_guard_policy,
    SOAK_BASE_VERSION,
    TARGET_LINEAGE_BASE_VERSION,
    TRUSTED_SNAPSHOT_BASE_VERSION,
    psql_database_url,
    schema_problems,
    strategy_platform_v13_owner_schema_problems,
    verify_schema,
    _add_controlled_canary_lifecycle_boundary,
    _add_canary_consent_handoff_boundary,
    _add_atomic_canary_prepare_boundary,
    _add_accepted_not_found_terminalization_boundary,
    _add_bounded_second_accepted_not_found_boundary,
    _add_final_accepted_not_found_boundary,
    _add_continuous_demo_automation_boundary,
    _add_research_receipt_boundary,
    _add_natural_signal_risk_chain_boundary,
)
from app.db.session import create_database_engine
from app.models.base import Base


V13_TASK1_CRITICAL_CHECK_TABLES = {
    "deployment_profile_versions_safety_check": "deployment_profile_versions",
    "execution_target_definition_versions_safety_check": (
        "execution_target_definition_versions"
    ),
    "market_data_policy_versions_overlap_shape_check": (
        "market_data_policy_versions"
    ),
    "market_data_quality_receipts_v13_scope_check": (
        "market_data_quality_receipts"
    ),
    "risk_profile_versions_safety_check": "risk_profile_versions",
    "runtime_profile_versions_safety_check": "runtime_profile_versions",
    "strategy_evaluation_summaries_counts_check": (
        "strategy_evaluation_summaries"
    ),
    "strategy_evaluation_summaries_status_check": (
        "strategy_evaluation_summaries"
    ),
    "strategy_platform_migration_mapping_qualified_evidence_check": (
        "strategy_platform_migration_entity_mappings"
    ),
    "strategy_platform_migration_mapping_quality_status_check": (
        "strategy_platform_migration_entity_mappings"
    ),
    "strategy_platform_migration_runs_evidence_digest_check": (
        "strategy_platform_migration_runs"
    ),
    "strategy_platform_migration_runs_forward_only_check": (
        "strategy_platform_migration_runs"
    ),
    "strategy_platform_migration_runs_terminal_shape_check": (
        "strategy_platform_migration_runs"
    ),
    "strategy_runtime_instances_safety_check": "strategy_runtime_instances",
}


POSTGRESQL_V13_CHECK_DEPARSE_SAMPLES = {
    "market_data_policy_versions_overlap_shape_check": (
        "overlap_by_timeframe IS NULL AND incremental_overlap_seconds IS NOT NULL "
        "AND incremental_overlap_seconds >= 0 OR overlap_by_timeframe IS NOT NULL "
        "AND incremental_overlap_seconds IS NULL AND "
        "length(overlap_by_timeframe::text) > 2"
    ),
    "market_data_quality_receipts_v13_scope_check": (
        "contract_version <> 'market-data-quality-v13-v1'::text OR "
        "idempotency_key IS NOT NULL AND quality_scope = "
        "'MIGRATION_SOURCE_CONSISTENCY_AS_OF_SOURCE_RECEIPT'::text AND "
        "quality_decision = 'NOT_STRATEGY_QUALIFICATION'::text AND "
        "file_identity_digest IS NOT NULL AND length(file_identity_digest) = 64 "
        "AND source_identity_digest IS NOT NULL AND "
        "length(source_identity_digest) = 64 AND aggregate_receipt_digest IS NOT NULL "
        "AND length(aggregate_receipt_digest) = 64 AND "
        "migration_artifact_digest IS NOT NULL AND "
        "length(migration_artifact_digest) = 64 AND freshness_basis = "
        "'ORIGINAL_AGGREGATE_RECEIPT_DOWNLOADED_AT'::text AND "
        "freshness_seconds IS NULL AND status = 'PASSED'::text"
    ),
    "strategy_evaluation_summaries_counts_check": (
        "required_window_count >= 0 AND passed_window_count >= 0 AND "
        "failed_window_count >= 0 AND "
        "(passed_window_count + failed_window_count) <= required_window_count"
    ),
    "strategy_platform_migration_runs_terminal_shape_check": (
        "(status <> ALL (ARRAY['SUCCEEDED'::text, 'FAILED'::text, "
        "'BLOCKED'::text])) OR status = 'SUCCEEDED'::text AND "
        "target_snapshot_digest IS NOT NULL AND report_digest IS NOT NULL AND "
        "completed_at IS NOT NULL AND error_code IS NULL AND error_message IS NULL "
        "AND evidence_manifest::text <> '{}'::text OR "
        "(status = ANY (ARRAY['FAILED'::text, 'BLOCKED'::text])) AND "
        "completed_at IS NOT NULL AND error_code IS NOT NULL AND "
        "error_message IS NOT NULL"
    ),
}


def test_psql_url_strips_sqlalchemy_driver_and_password() -> None:
    url = psql_database_url(
        "postgresql+psycopg://freqtrade:secret-value@localhost:5432/freqtrade_ai"
    )

    assert url == "postgresql://freqtrade@localhost:5432/freqtrade_ai"
    assert "secret-value" not in url


def test_psql_url_rejects_non_postgresql_database() -> None:
    with pytest.raises(ConfigurationError, match="PostgreSQL"):
        psql_database_url("sqlite+pysqlite:///:memory:")


def test_schema_verification_fails_closed_for_sqlite() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")

    result = verify_schema(engine)

    assert result.ready is False
    assert result.schema_version is None
    assert result.problems == ("database dialect is not PostgreSQL",)
    assert schema_problems(engine) == ["database dialect is not PostgreSQL"]
    assert strategy_platform_v13_owner_schema_problems(engine) == [
        "database dialect is not PostgreSQL"
    ]


def test_schema_version_is_explicit_and_stable() -> None:
    assert LEGACY_SCHEMA_VERSION == "20260712_01"
    assert PREVIOUS_SCHEMA_VERSION == "20260722_01"
    assert TARGET_LINEAGE_BASE_VERSION == "20260723_01"
    assert EARLY_TARGET_LINEAGE_VERSION == "20260727_01"
    assert RISK_CHAIN_BASE_VERSION == "20260727_02"
    assert RISK_CHAIN_HARDENING_BASE_VERSION == "20260727_03"
    assert TRUSTED_SNAPSHOT_BASE_VERSION == "20260727_04"
    assert ATTESTED_SESSION_BASE_VERSION == "20260727_05"
    assert HMAC_ATTESTATION_BASE_VERSION == "20260727_06"
    assert ATTESTATION_ACL_BASE_VERSION == "20260727_07"
    assert ORDER_WRITER_BASE_VERSION == "20260727_08"
    assert RECONCILIATION_BASE_VERSION == "20260727_09"
    assert RUNTIME_RECOVERY_BASE_VERSION == "20260727_10"
    assert FULL_CHAIN_BASE_VERSION == "20260727_11"
    assert SOAK_BASE_VERSION == "20260727_12"
    assert RUNTIME_APP_ACL_BASE_VERSION == "20260727_13"
    assert FILL_SNAPSHOT_REPEAT_BASE_VERSION == "20260727_14"
    assert RECONCILIATION_BATCH_FRESHNESS_BASE_VERSION == "20260728_15"
    assert RECOVERY_WALL_CLOCK_BASE_VERSION == "20260728_16"
    assert DUAL_SIDE_BASE_VERSION == "20260728_17"
    assert STRATEGY_PROMOTION_BASE_VERSION == "20260728_18"
    assert STRATEGY_DEPLOYMENT_BASE_VERSION == "20260729_19"
    assert RECONCILIATION_INDEX_BASE_VERSION == "20260729_21"
    assert SINGLE_ACTIVE_DEPLOYMENT_BASE_VERSION == "20260730_22"
    assert STRATEGY_VALIDATION_BASE_VERSION == "20260801_24"
    assert CANARY_LINEAGE_WRITE_BASE_VERSION == "20260801_25"
    assert CANARY_FINAL_EXPIRY_BASE_VERSION == "20260802_26"
    assert CANARY_LIFECYCLE_BASE_VERSION == "20260802_27"
    assert CANARY_CONSENT_HANDOFF_BASE_VERSION == "20260802_28"
    assert CANARY_CONSENT_FAILURE_AUDIT_BASE_VERSION == "20260803_29"
    assert CANARY_ATOMIC_PREPARE_BASE_VERSION == "20260803_30"
    assert ACCEPTED_NOT_FOUND_TERMINALIZATION_BASE_VERSION == "20260803_31"
    assert BOUNDED_SECOND_ACCEPTANCE_BASE_VERSION == "20260803_32"
    assert FINAL_ACCEPTANCE_BASE_VERSION == "20260804_33"
    assert CONTINUOUS_DEMO_BASE_VERSION == "20260804_34"
    assert CONTINUOUS_DEMO_SELECTION_V2_BASE_VERSION == "20260804_35"
    assert RESEARCH_PERSISTENCE_BASE_VERSION == "20260804_36"
    assert CANDIDATE_BRIDGE_BASE_VERSION == "20260809_37"
    assert NATURAL_SIGNAL_RISK_CHAIN_BASE_VERSION == "20260809_38"
    assert MULTI_ASSET_CAPACITY_BASE_VERSION == "20260810_39"
    assert AUTOMATION_GUARD_REBIND_BASE_VERSION == "20260810_40"
    assert DEPLOYMENT_POLICY_REBIND_BASE_VERSION == "20260810_41"
    assert NATURAL_SIGNAL_EVALUATOR_RECEIPT_BASE_VERSION == "20260810_42"
    assert NATURAL_SIGNAL_RISK_BUDGET_BASE_VERSION == "20260811_43"
    assert STALE_NATURAL_APPROVAL_RELEASE_BASE_VERSION == "20260811_44"
    assert STRATEGY_PLATFORM_V1_BASE_VERSION == "20260811_45"
    assert SCHEMA_VERSION == "20260813_47"


def test_v47_task1_critical_check_attestation_set_is_explicit() -> None:
    assert STRATEGY_PLATFORM_V13_TASK1_CRITICAL_CHECK_DEFINITIONS == frozenset(
        V13_TASK1_CRITICAL_CHECK_TABLES
    )


def test_v47_owner_acl_catalogs_are_exact_and_disjoint() -> None:
    assert len(STRATEGY_PLATFORM_V13_OWNER_READ_TABLES) == 54
    assert len(STRATEGY_PLATFORM_V13_OWNER_ONLY_TABLES) == 17
    assert len(STRATEGY_PLATFORM_V13_LEGACY_CAPABILITY_TABLES) == 59
    assert len(STRATEGY_PLATFORM_V13_LEGACY_CAPABILITY_SEQUENCES) == 48
    assert len(STRATEGY_PLATFORM_V13_LEGACY_CAPABILITY_FUNCTIONS) == 37
    assert len(STRATEGY_PLATFORM_V13_GUARD_FUNCTIONS) == 13
    assert (
        STRATEGY_PLATFORM_V13_OWNER_READ_TABLES
        & STRATEGY_PLATFORM_V13_OWNER_ONLY_TABLES
    ) == frozenset()
    assert (
        (
            STRATEGY_PLATFORM_V13_OWNER_READ_TABLES
            | STRATEGY_PLATFORM_V13_OWNER_ONLY_TABLES
        )
        & STRATEGY_PLATFORM_V13_LEGACY_CAPABILITY_TABLES
    ) == frozenset()


@pytest.mark.parametrize(
    "postgresql_definition",
    (
        "((stopped_at IS NULL) AND ((status)::text = ANY "
        "(ARRAY[('UNKNOWN'::character varying)::text, "
        "('STARTING'::character varying)::text, "
        "('HEALTHY'::character varying)::text, "
        "('DEGRADED'::character varying)::text])))",
        "((stopped_at IS NULL) AND ((status)::text = ANY "
        "((ARRAY['UNKNOWN'::character varying, "
        "'STARTING'::character varying, 'HEALTHY'::character varying, "
        "'DEGRADED'::character varying])::text[])))",
    ),
)
def test_v47_runtime_index_predicate_accepts_restore_deparse_variants(
    postgresql_definition: str,
) -> None:
    assert _normalized_index_definition(postgresql_definition) == (
        "stopped_atisnullandstatusin("
        "'unknown','starting','healthy','degraded')"
    )


def test_v47_owner_schema_verifier_replaces_only_retired_runtime_acl_checks() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(strategy_platform_v13_owner_schema_problems)
    for fragment in (
        "_include_legacy_runtime_acl=False",
        "expected_database",
        "TASK1_MIGRATION_KEY",
        'migration_run["status"] != "SUCCEEDED"',
        "canonical_digest(manifest)",
        "STRATEGY_PLATFORM_V13_OWNER_READ_TABLES",
        "STRATEGY_PLATFORM_V13_OWNER_ONLY_TABLES",
        "STRATEGY_PLATFORM_V13_LEGACY_CAPABILITY_TABLES",
        "STRATEGY_PLATFORM_V13_LEGACY_CAPABILITY_SEQUENCES",
        "STRATEGY_PLATFORM_V13_LEGACY_CAPABILITY_FUNCTIONS",
        "STRATEGY_PLATFORM_V13_GUARD_FUNCTIONS",
        "direct_runtime_select_tables",
        "V1.3 direct read ACL set mismatch",
        "SELECT count(*)",
    ):
        assert fragment in source


@pytest.mark.parametrize(
    ("constraint_name", "postgresql_definition"),
    sorted(POSTGRESQL_V13_CHECK_DEPARSE_SAMPLES.items()),
)
def test_v47_task1_schema_attestation_accepts_equivalent_postgresql_deparse(
    constraint_name: str,
    postgresql_definition: str,
) -> None:
    table_name = V13_TASK1_CRITICAL_CHECK_TABLES[constraint_name]
    table = Base.metadata.tables[table_name]

    assert _critical_check_definition_problems(
        table_name=table_name,
        table=table,
        actual_check_definitions={
            constraint_name: _normalized_sql_definition(postgresql_definition)
        },
        dialect=postgresql.dialect(),
    ) == []


@pytest.mark.parametrize(
    ("constraint_name", "table_name"),
    sorted(V13_TASK1_CRITICAL_CHECK_TABLES.items()),
)
def test_v47_task1_schema_attestation_rejects_same_name_weakened_checks(
    constraint_name: str,
    table_name: str,
) -> None:
    table = Base.metadata.tables[table_name]
    assert any(
        isinstance(constraint, CheckConstraint)
        and constraint.name == constraint_name
        for constraint in table.constraints
    )

    problems = _critical_check_definition_problems(
        table_name=table_name,
        table=table,
        actual_check_definitions={constraint_name: "true"},
        dialect=postgresql.dialect(),
    )

    assert problems == [
        f"check definition mismatch: {table_name}.{constraint_name}"
    ]


def test_v41_guard_policy_digests_match_the_exact_v39_and_v40_contracts() -> None:
    assert _demo_automation_policy_digest(
        allowed_instruments=("BTC-USDT-SWAP",),
        max_active_strategies=3,
    ) == "63fa1f4c0270b0630daf30862231de4ee46e43e85bf9911809682c6095e4ec5b"
    assert _demo_automation_policy_digest(
        allowed_instruments=(
            "BTC-USDT-SWAP",
            "ETH-USDT-SWAP",
            "SOL-USDT-SWAP",
        ),
        max_active_strategies=9,
    ) == "7318d7559b79afb72faf379c216bedb7989964352f95fa126add98e5d17405e2"


def test_v42_rebind_is_fenced_audited_and_updates_only_exact_active_source_rows() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_rebind_demo_automation_guard_policy)
    for fragment in (
        "no pending write, no unexpired approval",
        "risk_policy_digest=:source",
        "SET risk_policy_digest=:target",
        "status='ACTIVE'",
        "operational_state='COOLDOWN'",
        "fresh_health_check_required",
        "rebound_active_deployments",
        "allow_real_funds",
    ):
        assert fragment in source


def test_v39_natural_signal_risk_boundary_is_narrow_and_demo_only() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_add_natural_signal_risk_chain_boundary)
    for fragment in (
        "persist_okx_demo_natural_risk_chain",
        "SECURITY DEFINER SET search_path=pg_catalog",
        "CONTINUOUS_DEMO_V1",
        "okx-demo-selection-v2",
        "abs(result.max_drawdown_pct)<=0.15",
        "natural signal writer fence is invalid",
        "okx_demo_continuous_opening_allowed",
        "signal_snapshot::jsonb->'enter_long'",
        "p_payload->>'position_side'<>'long'",
        "p_payload->>'position_side'<>'short'",
        "quantity_value IS DISTINCT FROM minimum_size",
        "order_price IS DISTINCT FROM expected_limit",
        "stop_loss')::numeric IS DISTINCT FROM expected_stop",
        "take_profit')::numeric IS DISTINCT FROM expected_take",
        "allow_real_funds",
        "real_orders",
        "REVOKE ALL ON FUNCTION",
        "GRANT EXECUTE ON FUNCTION",
        "REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON TABLE {0}.risk_budgets",
    ):
        assert fragment in source
    assert "create_okx_demo_canary_lineage" not in source


def test_v37_research_receipts_are_append_only_runtime_tables() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_add_research_receipt_boundary)
    assert RUNTIME_APPEND_ONLY_TABLES == frozenset(
        {"strategy_research_attempt_events", "market_data_quality_receipts"}
    )
    assert BRIDGE_APPEND_ONLY_TABLES == frozenset(
        {"strategy_research_candidate_bridge_events"}
    )
    for fragment in (
        "prevent_research_receipt_mutation",
        "research receipts are append-only",
        "BEFORE UPDATE OR DELETE ON strategy_research_attempt_events",
        "BEFORE UPDATE OR DELETE ON market_data_quality_receipts",
        "BEFORE UPDATE OR DELETE ON strategy_research_candidate_bridge_events",
    ):
        assert fragment in source


def test_v36_continuous_demo_boundary_is_fixed_and_owner_controlled() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_add_continuous_demo_automation_boundary)
    for fragment in (
        "active_slot BETWEEN 1 AND MAX_ACTIVE_TOKEN",
        "CONTINUOUS_DEMO_V1",
        "okx-demo-selection-v2",
        "Codex Okx Demo Dual RSI Strategy",
        "active_demo_strategy_score_immutable",
        "active_demo_backtest_result_immutable",
        "active_demo_selection_chain_immutable",
        "max_orders_per_5_minutes',6",
        "max_orders_per_hour',24",
        "interval '10 minutes'",
        "interval '15 minutes'",
        "MANUAL_RESET_REQUIRED",
        "absolute_submission_claim=false",
        "REVOKE ALL ON TABLE",
        "REVOKE ALL ON FUNCTION",
    ):
        assert fragment in source


def test_v33_second_receipt_is_fixed_depth_owner_only_and_non_recursive() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_add_bounded_second_accepted_not_found_boundary)
    for fragment in (
        "receipt_depth IN (1,2,3)",
        "receipt.receipt_depth NOT IN (1,2)",
        "receipt_depth=2",
        "parent_terminal_receipt_id",
        "USER_ACCEPTED_NOT_FOUND_NO_FILL_V2",
        "terminalize_second_accepted_not_found_no_fill",
        "absolute_submission_claim',false",
        "exact_bounded_accepted_not_found_predecessor",
        "receipt-bound successor is required",
        "invalid bounded accepted successor",
        "ORDER BY receipt_depth DESC",
        "REVOKE ALL ON FUNCTION",
        "FROM PUBLIC,freqtrade",
    ):
        assert fragment in source
    assert "last_attempt_at<clock_timestamp()-interval '5 minutes'" not in source


def test_v34_final_receipt_is_owner_only_and_non_successorable() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_add_final_accepted_not_found_boundary)
    for fragment in (
        "receipt_depth IN (1,2,3)",
        "receipt_depth=3",
        "USER_ACCEPTED_NOT_FOUND_NO_FILL_FINAL_V1",
        "terminalize_final_accepted_not_found_no_fill",
        "successor_allowed',false",
        "absolute_submission_claim',false",
        "parent.receipt_depth<>2",
        "<>3",
        "REVOKE ALL ON FUNCTION",
        "FROM PUBLIC,freqtrade",
    ):
        assert fragment in source


def test_v32_accepted_not_found_sql_is_owner_only_and_single_successor() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_add_accepted_not_found_terminalization_boundary)
    for fragment in (
        "terminalize_accepted_not_found_no_fill",
        "USER_ACCEPTED_NOT_FOUND_NO_FILL_V1",
        "absolute_submission_claim",
        "exchange_result_code','51603'",
        "observed_at IS DISTINCT FROM attempt.last_attempt_at",
        "attempt.safe_response_snapshot::jsonb IS DISTINCT FROM",
        "receipt.request_digest=attempt.request_digest",
        "guard_accepted_not_found_attempt_transition",
        "pg_advisory_xact_lock(5067747289570038600)",
        "okx_demo_canary_one_accepted_successor_idx",
        "terminal_receipt_id IS NOT NULL",
        "okx_demo_canary_consent_eligibility",
        "'ACCEPTED_SUCCESSOR'",
        "'BLOCKED'",
        "REVOKE ALL ON FUNCTION",
        "FROM PUBLIC,freqtrade",
    ):
        assert fragment in source


def test_v31_atomic_prepare_sql_is_single_commit_and_fail_closed() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_add_atomic_canary_prepare_boundary)
    for fragment in (
        "commit_atomic_okx_demo_canary_prepare",
        "dispatch_not_after",
        "clock_timestamp()+interval '1 second'",
        "dispatch_guard_policy','db-clock-monotonic-v2'",
        "dispatch_claim_min_remaining_ms',500",
        "post_start_reserve_ms',100",
        "validate_atomic_okx_demo_dispatch_authority",
        "state='DISPATCHED'",
        "require_active_okx_demo_operator_consent_secret",
        "REVOKE EXECUTE ON FUNCTION",
        "supersedes_handoff_id",
    ):
        assert fragment in source


def test_v28_consent_handoff_sql_is_owner_managed_and_exact() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_add_canary_consent_handoff_boundary)
    for fragment in (
        "source.id=22",
        "'[15,16,17,18,19,20,21,22]'::jsonb",
        "consent_deadline_at<=statement_timestamp()",
        "exact fresh attested snapshot binding failed",
        "instrument.database_id",
        "instrument.attested_session_id IS DISTINCT FROM market.attested_session_id",
        "REVOKE INSERT ON TABLE",
        "RUNTIME_RESTART_BEFORE_PREPARED",
        "SECURITY DEFINER SET search_path=pg_catalog",
        "okx_demo_operator_consent_secrets",
        "public.hmac(",
        "p_payload||'|'||p_nonce",
    ):
        assert fragment in source


def test_v27_canary_lifecycle_sql_is_fail_closed() -> None:
    import inspect as pyinspect

    source = pyinspect.getsource(_add_controlled_canary_lifecycle_boundary)
    for fragment in (
        "p_expected_version IS NULL",
        "IS DISTINCT FROM p_expected_version",
        "require_current_okx_demo_canary_recovery_run",
        "opening_state IN('live','partially_filled') THEN 'CANCEL_PENDING'",
        "a.operation='PLACE' AND a.attempt_count=1",
        "g.lifecycle_id=l.lifecycle_id AND g.reconciliation_run_id=p_run_id",
        "outcome='FAILED' THEN 'FAILED'",
        "UPDATE SCHEMA_TOKEN.okx_demo_submission_grants SET status='FAILED'",
        "UPDATE SCHEMA_TOKEN.okx_demo_recovery_grants SET status='EXPIRED'",
        "current_user IS DISTINCT FROM 'freqtrade_ai_attestor'",
        "okx_demo_recovery_grants_one_active_lifecycle_action_idx",
        "a.state IN('PREPARED','DISPATCHED','ACKNOWLEDGED','RECOVERY_REQUIRED','RESIDUAL_CLOSE_REQUIRED')",
    ):
        assert fragment in source
