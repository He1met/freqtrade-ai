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
SINGLE_ACTIVE_DEPLOYMENT_BASE_VERSION = "20260730_22"
ONE_SHOT_SUBMISSION_GRANT_BASE_VERSION = "20260730_23"
STRATEGY_VALIDATION_BASE_VERSION = "20260801_24"
CANARY_LINEAGE_WRITE_BASE_VERSION = "20260801_25"
CANARY_FINAL_EXPIRY_BASE_VERSION = "20260802_26"
CANARY_LIFECYCLE_BASE_VERSION = "20260802_27"
CANARY_CONSENT_HANDOFF_BASE_VERSION = "20260802_28"
CANARY_CONSENT_FAILURE_AUDIT_BASE_VERSION = "20260803_29"
CANARY_ATOMIC_PREPARE_BASE_VERSION = "20260803_30"
ACCEPTED_NOT_FOUND_TERMINALIZATION_BASE_VERSION = "20260803_31"
BOUNDED_SECOND_ACCEPTANCE_BASE_VERSION = "20260803_32"
FINAL_ACCEPTANCE_BASE_VERSION = "20260804_33"
CONTINUOUS_DEMO_BASE_VERSION = "20260804_34"
CONTINUOUS_DEMO_SELECTION_V2_BASE_VERSION = "20260804_35"
RESEARCH_PERSISTENCE_BASE_VERSION = "20260804_36"
SCHEMA_VERSION = "20260809_37"
VERSION_TABLE = "freqtrade_ai_schema_migrations"
ATTESTATION_PROOF_KEY_ENV = "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY"
OPERATOR_TOKEN_ENV = "FREQTRADE_AI_OPERATOR_TOKEN"

RUNTIME_APPLICATION_TABLES = (
    "strategies",
    "strategy_versions",
    "strategy_generation_runs",
    "strategy_failure_reasons",
    "backtest_runs",
    "backtest_tasks",
    "backtest_results",
    "strategy_scores",
    "strategy_research_batches",
    "strategy_research_candidates",
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
    "signal_evaluations",
    "risk_budgets",
)

RUNTIME_APPEND_ONLY_TABLES = frozenset(
    {"strategy_research_attempt_events", "market_data_quality_receipts"}
)

CANARY_LINEAGE_BOUNDARY_TABLES = frozenset(
    {
        "trade_intents",
        "risk_decisions",
        "approved_executions",
        "full_chain_runs",
        "reconciliation_runs",
        "okx_demo_reconciliation_states",
        "okx_demo_attested_sessions",
        "okx_demo_trusted_snapshots",
        "okx_demo_submission_grants",
        "okx_order_write_attempts",
        "exchange_orders",
        "exchange_positions",
    }
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


CANARY_LINEAGE_FUNCTION_BODY = """
DECLARE
    allowed_keys CONSTANT text[] := ARRAY[
        'execution_target', 'provenance', 'non_production',
        'full_chain_run_id', 'reconciliation_run_id',
        'intent_id', 'canonical_hash', 'policy_digest',
        'approved_payload_hash', 'idempotency_key_digest',
        'client_order_id', 'instrument_id', 'side', 'position_side',
        'order_type', 'quantity', 'limit_price', 'reference_price',
        'leverage', 'margin_mode', 'stop_loss', 'take_profit',
        'reduce_only', 'notional', 'request_snapshot', 'expires_at',
        'canonical_input_serialized', 'policy_serialized',
        'approved_payload_serialized', 'intent_identity_serialized',
        'instrument_snapshot_id', 'market_snapshot_id',
        'account_snapshot_id'
    ];
    chain_row record;
    existing_intent SCHEMA_TOKEN.trade_intents%ROWTYPE;
    decision_row SCHEMA_TOKEN.risk_decisions%ROWTYPE;
    approval_row SCHEMA_TOKEN.approved_executions%ROWTYPE;
    instrument_row SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
    market_row SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
    account_row SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
    full_chain_run_id bigint;
    reconciliation_run_id bigint;
    expires_at timestamptz;
    quantity numeric;
    limit_price numeric;
    reference_price numeric;
    leverage numeric;
    stop_loss numeric;
    take_profit numeric;
    notional numeric;
    minimum_size numeric;
    lot_size numeric;
    contract_value numeric;
    tick_size numeric;
    best_bid numeric;
    best_ask numeric;
    mark_price numeric;
    snapshot_leverage numeric;
    derived_quantity numeric;
    derived_limit_price numeric;
    computed_notional numeric;
    expected_signal_digest text;
    expected_lineage jsonb;
    expected_canonical_input jsonb;
    expected_request_snapshot jsonb;
    inserted_intent_id bigint;
    inserted_decision_id bigint;
    inserted_approval_id bigint;
    is_replay boolean := FALSE;
    atomic_successor_allowed boolean := FALSE;
BEGIN
    IF NOT pg_try_advisory_xact_lock(5067747289570038600) THEN
        RAISE EXCEPTION 'controlled canary coordination lock is busy';
    END IF;

    IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR NOT p_payload ?& allowed_keys
       OR EXISTS (
           SELECT 1 FROM jsonb_object_keys(p_payload) AS supplied(key)
           WHERE supplied.key <> ALL (allowed_keys)
             AND supplied.key <> 'consent_handoff_id'
       )
       OR p_payload->>'execution_target' IS DISTINCT FROM 'OKX_DEMO'
       OR p_payload->>'provenance'
            IS DISTINCT FROM 'CONTROLLED_CANARY_NON_PRODUCTION'
       OR p_payload->'non_production' IS DISTINCT FROM 'true'::jsonb
       OR p_payload->>'instrument_id' IS DISTINCT FROM 'BTC-USDT-SWAP'
       OR p_payload->>'side' IS DISTINCT FROM 'buy'
       OR p_payload->>'position_side' IS DISTINCT FROM 'long'
       OR p_payload->>'order_type' IS DISTINCT FROM 'limit'
       OR p_payload->>'margin_mode' IS DISTINCT FROM 'isolated'
       OR p_payload->'reduce_only' IS DISTINCT FROM 'false'::jsonb
       OR (p_payload->>'full_chain_run_id' ~ '^[1-9][0-9]*$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'reconciliation_run_id' ~ '^[1-9][0-9]*$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'intent_id' ~ '^[0-9a-f]{64}$') IS DISTINCT FROM TRUE
       OR (p_payload->>'canonical_hash' ~ '^[0-9a-f]{64}$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'policy_digest' ~ '^[0-9a-f]{64}$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'approved_payload_hash' ~ '^[0-9a-f]{64}$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'idempotency_key_digest' ~ '^[0-9a-f]{64}$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'client_order_id' ~ '^FAICANARY[0-9a-f]{23}$')
            IS DISTINCT FROM TRUE
       OR p_payload->>'client_order_id'
            IS DISTINCT FROM 'FAICANARY' || left(p_payload->>'intent_id', 23)
       OR (p_payload->>'quantity' ~ '^[0-9]+(\\.[0-9]+)?$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'limit_price' ~ '^[0-9]+(\\.[0-9]+)?$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'reference_price' ~ '^[0-9]+(\\.[0-9]+)?$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'leverage' ~ '^[0-9]+(\\.[0-9]+)?$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'stop_loss' ~ '^[0-9]+(\\.[0-9]+)?$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'take_profit' ~ '^[0-9]+(\\.[0-9]+)?$')
            IS DISTINCT FROM TRUE
       OR (p_payload->>'notional' ~ '^[0-9]+(\\.[0-9]+)?$')
            IS DISTINCT FROM TRUE
       OR jsonb_typeof(p_payload->'request_snapshot') IS DISTINCT FROM 'object'
       OR (p_payload->>'canonical_input_serialized')::jsonb
            IS DISTINCT FROM p_payload->'request_snapshot'->'canonical_input'
       OR encode(public.digest(
            convert_to(p_payload->>'canonical_input_serialized', 'UTF8'), 'sha256'
          ), 'hex') IS DISTINCT FROM p_payload->>'canonical_hash'
       OR (p_payload->>'policy_serialized')::jsonb IS DISTINCT FROM jsonb_build_object(
            'provenance', 'CONTROLLED_CANARY_NON_PRODUCTION',
            'allowed_instruments', jsonb_build_array('BTC-USDT-SWAP'),
            'allowed_sides', jsonb_build_array('buy'),
            'allowed_order_types', jsonb_build_array('limit'),
            'max_leverage', p_payload->>'leverage',
            'max_order_notional', '20', 'max_total_exposure', '20',
            'max_positions', 1, 'max_price_deviation_pct', '0.01',
            'min_strategy_score', '0', 'scoring_version', 'controlled-canary-v1'
          )
       OR encode(public.digest(
            convert_to(p_payload->>'policy_serialized', 'UTF8'), 'sha256'
          ), 'hex') IS DISTINCT FROM p_payload->>'policy_digest'
       OR (p_payload->>'approved_payload_serialized')::jsonb
            IS DISTINCT FROM jsonb_build_object(
                'canonical_input', p_payload->'request_snapshot'->'canonical_input',
                'notional', p_payload->>'notional',
                'provenance', 'CONTROLLED_CANARY_NON_PRODUCTION'
            )
       OR encode(public.digest(
            convert_to(p_payload->>'approved_payload_serialized', 'UTF8'), 'sha256'
          ), 'hex') IS DISTINCT FROM p_payload->>'approved_payload_hash'
       OR (p_payload->>'intent_identity_serialized')::jsonb
            IS DISTINCT FROM jsonb_build_object(
                'provenance', 'CONTROLLED_CANARY_NON_PRODUCTION',
                'idempotency_key_digest', p_payload->>'idempotency_key_digest',
                'canonical_hash', p_payload->>'canonical_hash'
            )
       OR encode(public.digest(
            convert_to(p_payload->>'intent_identity_serialized', 'UTF8'), 'sha256'
          ), 'hex') IS DISTINCT FROM p_payload->>'intent_id'
    THEN
        RAISE EXCEPTION 'invalid controlled canary lineage payload';
    END IF;

    full_chain_run_id := (p_payload->>'full_chain_run_id')::bigint;
    reconciliation_run_id := (p_payload->>'reconciliation_run_id')::bigint;
    expires_at := (p_payload->>'expires_at')::timestamptz;
    quantity := (p_payload->>'quantity')::numeric;
    limit_price := (p_payload->>'limit_price')::numeric;
    reference_price := (p_payload->>'reference_price')::numeric;
    leverage := (p_payload->>'leverage')::numeric;
    stop_loss := (p_payload->>'stop_loss')::numeric;
    take_profit := (p_payload->>'take_profit')::numeric;
    notional := (p_payload->>'notional')::numeric;
    expected_signal_digest := encode(public.digest(convert_to(format(
        '{"full_chain_run_id":%s,"instrument_id":"BTC-USDT-SWAP",'
        '"provenance":"CONTROLLED_CANARY_NON_PRODUCTION",'
        '"quantity":"%s","side":"buy"}',
        full_chain_run_id,
        p_payload->>'quantity'
    ), 'UTF8'), 'sha256'), 'hex');
    expected_lineage := jsonb_build_object(
        'provenance', 'CONTROLLED_CANARY_NON_PRODUCTION',
        'strategy_id', NULL,
        'strategy_version_id', NULL,
        'backtest_run_id', NULL,
        'backtest_task_id', NULL,
        'backtest_result_id', NULL,
        'strategy_score_id', NULL
    );
    expected_canonical_input := jsonb_build_object(
        'execution_target', 'OKX_DEMO',
        'full_chain_run_id', full_chain_run_id,
        'candidate_approval_id', NULL,
        'signal_snapshot_id', NULL,
        'signal_digest', expected_signal_digest,
        'lineage', expected_lineage,
        'snapshot_ids', jsonb_build_object(
            'instrument', p_payload->>'instrument_snapshot_id',
            'market', p_payload->>'market_snapshot_id',
            'account', p_payload->>'account_snapshot_id'
        ),
        'instrument_id', 'BTC-USDT-SWAP',
        'side', 'buy',
        'position_side', 'long',
        'order_type', 'limit',
        'quantity', p_payload->>'quantity',
        'limit_price', p_payload->>'limit_price',
        'reference_price', p_payload->>'reference_price',
        'leverage', p_payload->>'leverage',
        'margin_mode', 'isolated',
        'stop_loss', p_payload->>'stop_loss',
        'take_profit', p_payload->>'take_profit',
        'reduce_only', FALSE,
        'provenance', 'CONTROLLED_CANARY_NON_PRODUCTION'
    );
    expected_request_snapshot := jsonb_build_object(
        'canonical_input', expected_canonical_input,
        'snapshot_evidence', p_payload->'request_snapshot'->'snapshot_evidence',
        'provenance', 'CONTROLLED_CANARY_NON_PRODUCTION',
        'non_production', TRUE
    );

    IF quantity <= 0 OR limit_price <= 0 OR reference_price <= 0
       OR leverage <= 0 OR stop_loss <= 0 OR take_profit <= 0
       OR stop_loss >= reference_price OR take_profit <= reference_price
       OR notional <= 0 OR notional > 20
       OR expires_at <= statement_timestamp()
       OR expires_at > statement_timestamp() + INTERVAL '10 seconds'
       OR p_payload->'request_snapshot'->>'provenance'
            IS DISTINCT FROM 'CONTROLLED_CANARY_NON_PRODUCTION'
       OR p_payload->'request_snapshot'->'non_production'
            IS DISTINCT FROM 'true'::jsonb
       OR p_payload->'request_snapshot'->'canonical_input'->>'provenance'
            IS DISTINCT FROM 'CONTROLLED_CANARY_NON_PRODUCTION'
       OR p_payload->'request_snapshot'->'canonical_input'->>'execution_target'
            IS DISTINCT FROM 'OKX_DEMO'
       OR p_payload->'request_snapshot'->'canonical_input'->>'full_chain_run_id'
            IS DISTINCT FROM full_chain_run_id::text
       OR p_payload->'request_snapshot'->'canonical_input'->'candidate_approval_id'
            IS DISTINCT FROM 'null'::jsonb
       OR p_payload->'request_snapshot'->'canonical_input'->'signal_snapshot_id'
            IS DISTINCT FROM 'null'::jsonb
       OR (p_payload->'request_snapshot'->'canonical_input'->>'signal_digest'
            ~ '^[0-9a-f]{64}$') IS DISTINCT FROM TRUE
       OR p_payload->'request_snapshot'->'canonical_input'->>'signal_digest'
            IS DISTINCT FROM expected_signal_digest
       OR p_payload->'request_snapshot'->'canonical_input'->>'instrument_id'
            IS DISTINCT FROM 'BTC-USDT-SWAP'
       OR p_payload->'request_snapshot'->'canonical_input'->>'side'
            IS DISTINCT FROM 'buy'
       OR p_payload->'request_snapshot'->'canonical_input'->>'position_side'
            IS DISTINCT FROM 'long'
       OR p_payload->'request_snapshot'->'canonical_input'->>'order_type'
            IS DISTINCT FROM 'limit'
       OR p_payload->'request_snapshot'->'canonical_input'->>'margin_mode'
            IS DISTINCT FROM 'isolated'
       OR p_payload->'request_snapshot'->'canonical_input'->'reduce_only'
            IS DISTINCT FROM 'false'::jsonb
       OR p_payload->'request_snapshot'->'canonical_input'->>'quantity'
            IS DISTINCT FROM p_payload->>'quantity'
       OR p_payload->'request_snapshot'->'canonical_input'->>'limit_price'
            IS DISTINCT FROM p_payload->>'limit_price'
       OR p_payload->'request_snapshot'->'canonical_input'->>'reference_price'
            IS DISTINCT FROM p_payload->>'reference_price'
       OR p_payload->'request_snapshot'->'canonical_input'->>'leverage'
            IS DISTINCT FROM p_payload->>'leverage'
       OR p_payload->'request_snapshot'->'canonical_input'->>'stop_loss'
            IS DISTINCT FROM p_payload->>'stop_loss'
       OR p_payload->'request_snapshot'->'canonical_input'->>'take_profit'
            IS DISTINCT FROM p_payload->>'take_profit'
       OR p_payload->'request_snapshot'->'canonical_input'->'lineage'
            IS DISTINCT FROM expected_lineage
       OR p_payload->'request_snapshot'->'canonical_input'
            IS DISTINCT FROM expected_canonical_input
       OR p_payload->'request_snapshot' IS DISTINCT FROM expected_request_snapshot
    THEN
        RAISE EXCEPTION 'controlled canary lineage safety contract failed';
    END IF;

    SELECT * INTO existing_intent
    FROM SCHEMA_TOKEN.trade_intents
    WHERE execution_target_id = 'OKX_DEMO'
      AND idempotency_key_digest = p_payload->>'idempotency_key_digest';
    IF FOUND THEN
        SELECT * INTO decision_row
        FROM SCHEMA_TOKEN.risk_decisions
        WHERE trade_intent_id = existing_intent.id;
        SELECT * INTO approval_row
        FROM SCHEMA_TOKEN.approved_executions
        WHERE trade_intent_id = existing_intent.id;
        SELECT id, run_kind, research_scope_id, execution_target_id,
               status, current_stage, strategy_generation_run_id,
               strategy_id, strategy_version_id, backtest_run_id,
               backtest_task_id, backtest_result_id, strategy_score_id,
               candidate_approval_id, signal_snapshot_id, signal_evaluation_id,
               trade_intent_id, risk_decision_id, approved_execution_id,
               exchange_order_id
        INTO chain_row
        FROM SCHEMA_TOKEN.full_chain_runs
        WHERE id = full_chain_run_id;
        IF existing_intent.intent_id IS DISTINCT FROM p_payload->>'intent_id'
           OR existing_intent.canonical_hash IS DISTINCT FROM p_payload->>'canonical_hash'
           OR existing_intent.policy_digest IS DISTINCT FROM p_payload->>'policy_digest'
           OR existing_intent.approved_payload_hash
                IS DISTINCT FROM p_payload->>'approved_payload_hash'
           OR existing_intent.client_order_id
                IS DISTINCT FROM p_payload->>'client_order_id'
           OR existing_intent.request_snapshot::jsonb
                IS DISTINCT FROM p_payload->'request_snapshot'
           OR existing_intent.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
           OR existing_intent.authorization_schema_version IS DISTINCT FROM 'RISK_V1'
           OR existing_intent.status IS DISTINCT FROM 'APPROVED'
           OR decision_row.id IS NULL
           OR decision_row.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
           OR decision_row.trade_intent_id IS DISTINCT FROM existing_intent.id
           OR decision_row.authorization_schema_version IS DISTINCT FROM 'RISK_V1'
           OR decision_row.policy_digest IS DISTINCT FROM p_payload->>'policy_digest'
           OR decision_row.decision IS DISTINCT FROM 'APPROVED'
           OR decision_row.policy_version IS DISTINCT FROM 'controlled-canary-v1'
           OR decision_row.evidence_snapshot::jsonb IS DISTINCT FROM jsonb_build_object(
                'reasons', '[]'::jsonb,
                'input_digest', p_payload->>'canonical_hash',
                'policy_digest', p_payload->>'policy_digest',
                'lineage', expected_lineage,
                'notional', p_payload->>'notional',
                'provenance', 'CONTROLLED_CANARY_NON_PRODUCTION',
                'non_production', TRUE,
                'llm_authority', FALSE
           )
           OR decision_row.evidence_snapshot::jsonb->>'provenance'
                IS DISTINCT FROM 'CONTROLLED_CANARY_NON_PRODUCTION'
           OR decision_row.evidence_snapshot::jsonb->'non_production'
                IS DISTINCT FROM 'true'::jsonb
           OR decision_row.evidence_snapshot::jsonb->>'input_digest'
                IS DISTINCT FROM p_payload->>'canonical_hash'
           OR decision_row.evidence_snapshot::jsonb->>'policy_digest'
                IS DISTINCT FROM p_payload->>'policy_digest'
           OR decision_row.evidence_snapshot::jsonb->>'notional'
                IS DISTINCT FROM p_payload->>'notional'
           OR decision_row.evidence_snapshot::jsonb->'lineage'
                IS DISTINCT FROM p_payload->'request_snapshot'
                    ->'canonical_input'->'lineage'
           OR approval_row.id IS NULL
           OR approval_row.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
           OR approval_row.trade_intent_id IS DISTINCT FROM existing_intent.id
           OR approval_row.risk_decision_id IS DISTINCT FROM decision_row.id
           OR approval_row.intent_id IS DISTINCT FROM p_payload->>'intent_id'
           OR approval_row.client_order_id IS DISTINCT FROM p_payload->>'client_order_id'
           OR approval_row.authorization_schema_version IS DISTINCT FROM 'RISK_V1'
           OR approval_row.canonical_hash IS DISTINCT FROM p_payload->>'canonical_hash'
           OR approval_row.policy_digest IS DISTINCT FROM p_payload->>'policy_digest'
           OR approval_row.approved_payload_hash
                IS DISTINCT FROM p_payload->>'approved_payload_hash'
           OR approval_row.instrument_snapshot_id
                IS DISTINCT FROM p_payload->>'instrument_snapshot_id'
           OR approval_row.market_snapshot_id
                IS DISTINCT FROM p_payload->>'market_snapshot_id'
           OR approval_row.account_snapshot_id
                IS DISTINCT FROM p_payload->>'account_snapshot_id'
           OR approval_row.decision IS DISTINCT FROM 'APPROVED'
           OR approval_row.intent_status IS DISTINCT FROM 'APPROVED'
           OR approval_row.reserved_notional IS DISTINCT FROM notional
           OR approval_row.order_submission_authorized IS NOT FALSE
           OR approval_row.claim_required IS NOT TRUE
           OR approval_row.status IS DISTINCT FROM 'ACTIVE'
           OR approval_row.expires_at IS DISTINCT FROM expires_at
           OR approval_row.expires_at <= statement_timestamp()
           OR approval_row.evidence_snapshot::jsonb IS DISTINCT FROM jsonb_build_object(
                'provenance', 'CONTROLLED_CANARY_NON_PRODUCTION',
                'non_production', TRUE,
                'lineage', expected_lineage,
                'snapshot_evidence', p_payload->'request_snapshot'
                    ->'snapshot_evidence'
           )
           OR approval_row.evidence_snapshot::jsonb->>'provenance'
                IS DISTINCT FROM 'CONTROLLED_CANARY_NON_PRODUCTION'
           OR approval_row.evidence_snapshot::jsonb->'non_production'
                IS DISTINCT FROM 'true'::jsonb
           OR approval_row.evidence_snapshot::jsonb->'lineage'
                IS DISTINCT FROM p_payload->'request_snapshot'
                    ->'canonical_input'->'lineage'
           OR approval_row.evidence_snapshot::jsonb->'snapshot_evidence'
                IS DISTINCT FROM p_payload->'request_snapshot'->'snapshot_evidence'
           OR chain_row.id IS NULL
           OR chain_row.run_kind IS DISTINCT FROM 'RESEARCH'
           OR chain_row.research_scope_id IS DISTINCT FROM 'LOCAL_DRY_RUN'
           OR chain_row.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
           OR chain_row.status IS DISTINCT FROM 'EXECUTING'
           OR chain_row.current_stage IS DISTINCT FROM 'EXECUTION'
           OR chain_row.trade_intent_id IS DISTINCT FROM existing_intent.id
           OR chain_row.risk_decision_id IS DISTINCT FROM decision_row.id
           OR chain_row.approved_execution_id IS DISTINCT FROM approval_row.id
        THEN
            RAISE EXCEPTION 'controlled canary lineage idempotency conflict';
        END IF;
        is_replay := TRUE;
    END IF;

    SELECT id, run_kind, research_scope_id, execution_target_id,
           status, current_stage, strategy_generation_run_id,
           strategy_id, strategy_version_id, backtest_run_id,
           backtest_task_id, backtest_result_id, strategy_score_id,
           candidate_approval_id, signal_snapshot_id, signal_evaluation_id,
           trade_intent_id, risk_decision_id, approved_execution_id,
           exchange_order_id
    INTO chain_row
    FROM SCHEMA_TOKEN.full_chain_runs
    WHERE id = full_chain_run_id;
    IF NOT FOUND
       OR chain_row.run_kind IS DISTINCT FROM 'RESEARCH'
       OR chain_row.research_scope_id IS DISTINCT FROM 'LOCAL_DRY_RUN'
       OR chain_row.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
       OR chain_row.status IS DISTINCT FROM 'EXECUTING'
       OR chain_row.current_stage IS DISTINCT FROM 'EXECUTION'
       OR chain_row.strategy_generation_run_id IS NOT NULL
       OR chain_row.strategy_id IS NOT NULL
       OR chain_row.strategy_version_id IS NOT NULL
       OR chain_row.backtest_run_id IS NOT NULL
       OR chain_row.backtest_task_id IS NOT NULL
       OR chain_row.backtest_result_id IS NOT NULL
       OR chain_row.strategy_score_id IS NOT NULL
       OR chain_row.candidate_approval_id IS NOT NULL
       OR chain_row.signal_snapshot_id IS NOT NULL
       OR chain_row.signal_evaluation_id IS NOT NULL
       OR (NOT is_replay AND (
            chain_row.trade_intent_id IS NOT NULL
            OR chain_row.risk_decision_id IS NOT NULL
            OR chain_row.approved_execution_id IS NOT NULL
       ))
       OR chain_row.exchange_order_id IS NOT NULL
    THEN
        RAISE EXCEPTION 'controlled canary full-chain binding mismatch';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM SCHEMA_TOKEN.okx_demo_reconciliation_states AS state
        JOIN SCHEMA_TOKEN.reconciliation_runs AS run
          ON run.id = state.last_reconciliation_run_id
        WHERE state.execution_target_id = 'OKX_DEMO'
          AND state.opening_frozen IS FALSE
          AND state.status IN ('RECONCILED', 'RECOVERED')
          AND run.id = reconciliation_run_id
          AND run.execution_target_id = 'OKX_DEMO'
          AND run.status IN ('RECONCILED', 'RECOVERED')
          AND run.artifact_status = 'READY'
          AND run.source_type = 'api_aggregate'
          AND run.core_data IS TRUE
          AND run.completed_at IS NOT NULL
          AND run.authoritative_observed_at IS NOT NULL
          AND run.completed_at >= statement_timestamp() - INTERVAL '30 seconds'
          AND run.completed_at <= statement_timestamp() + INTERVAL '5 seconds'
          AND run.authoritative_observed_at
                >= statement_timestamp() - INTERVAL '30 seconds'
          AND run.authoritative_observed_at
                <= statement_timestamp() + INTERVAL '5 seconds'
          AND run.database_ids::jsonb->'reconciliation_run'
                = jsonb_build_array(run.id)
          AND run.database_ids::jsonb->'order_snapshots' = '[]'::jsonb
          AND run.database_ids::jsonb->'position_snapshots' = '[]'::jsonb
    )
    THEN
        RAISE EXCEPTION 'controlled canary reconciliation is not fresh';
    END IF;

    IF p_payload ? 'consent_handoff_id' THEN
        SELECT EXISTS(
          SELECT 1
          FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs successor
          JOIN SCHEMA_TOKEN.okx_demo_canary_consent_handoffs predecessor
            ON predecessor.handoff_id=successor.supersedes_handoff_id
          WHERE successor.handoff_id=p_payload->>'consent_handoff_id'
            AND successor.status='REQUESTED'
            AND ((successor.terminal_receipt_id IS NULL
                  AND predecessor.status='EXPIRED'
                  AND predecessor.failure_code='FINALIZED_EVIDENCE_EXPIRED'
                  AND predecessor.grant_id IS NULL
                  AND (SELECT count(*) FROM SCHEMA_TOKEN.trade_intents
                       WHERE execution_target_id='OKX_DEMO')=1
                  AND (SELECT count(*) FROM SCHEMA_TOKEN.approved_executions
                       WHERE execution_target_id='OKX_DEMO' AND status='EXPIRED')=1
                  AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants)
                  AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
                       WHERE execution_target_id='OKX_DEMO')
                  AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_orders
                       WHERE execution_target_id='OKX_DEMO')
                  AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles))
              OR (successor.terminal_receipt_id IS NOT NULL
                  AND SCHEMA_TOKEN.exact_bounded_accepted_not_found_predecessor(
                        predecessor.handoff_id)
                  AND EXISTS(SELECT 1
                       FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations receipt
                       WHERE receipt.id=successor.terminal_receipt_id
                         AND receipt.predecessor_handoff_id=predecessor.handoff_id)
                  AND (SELECT count(*) FROM SCHEMA_TOKEN.trade_intents
                       WHERE execution_target_id='OKX_DEMO')=
                      (SELECT receipt_depth+1 FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
                       WHERE id=successor.terminal_receipt_id)
                  AND (SELECT count(*) FROM SCHEMA_TOKEN.approved_executions
                       WHERE execution_target_id='OKX_DEMO' AND status='EXPIRED')=
                      (SELECT receipt_depth+1 FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
                       WHERE id=successor.terminal_receipt_id)
                  AND (SELECT count(*) FROM SCHEMA_TOKEN.okx_demo_submission_grants
                       WHERE execution_target_id='OKX_DEMO' AND status='CONSUMED')=
                      (SELECT receipt_depth FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
                       WHERE id=successor.terminal_receipt_id)
                  AND (SELECT count(*) FROM SCHEMA_TOKEN.okx_order_write_attempts
                       WHERE execution_target_id='OKX_DEMO'
                         AND state='USER_ACCEPTED_NOT_FOUND_NO_FILL')=
                      (SELECT receipt_depth FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
                       WHERE id=successor.terminal_receipt_id)
                  AND (SELECT count(*) FROM SCHEMA_TOKEN.exchange_orders
                       WHERE execution_target_id='OKX_DEMO'
                         AND status='USER_ACCEPTED_NOT_FOUND_NO_FILL')=
                      (SELECT receipt_depth FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
                       WHERE id=successor.terminal_receipt_id)
                  AND (SELECT count(*) FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
                       WHERE cleanup_phase='TERMINAL' AND outcome='FAILED')=
                      (SELECT receipt_depth FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
                       WHERE id=successor.terminal_receipt_id)
                  AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_fills
                       WHERE execution_target_id='OKX_DEMO')
                  AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.risk_budgets
                       WHERE execution_target_id='OKX_DEMO'
                         AND (reserved_notional<>0 OR approved_positions<>0))))
        ) INTO atomic_successor_allowed;
    END IF;

    IF NOT is_replay AND NOT atomic_successor_allowed AND (EXISTS (
        SELECT 1 FROM SCHEMA_TOKEN.trade_intents AS prior_intent
        WHERE prior_intent.execution_target_id = 'OKX_DEMO'
    ) OR EXISTS (
        SELECT 1 FROM SCHEMA_TOKEN.approved_executions AS prior_approval
        WHERE prior_approval.execution_target_id = 'OKX_DEMO'
    ) OR EXISTS (
        SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants AS prior_grant
        WHERE prior_grant.execution_target_id = 'OKX_DEMO'
    ) OR EXISTS (
        SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts AS prior_attempt
        WHERE prior_attempt.execution_target_id = 'OKX_DEMO'
    ) OR EXISTS (
        SELECT 1 FROM SCHEMA_TOKEN.exchange_orders AS prior_order
        WHERE prior_order.execution_target_id = 'OKX_DEMO'
    ) OR EXISTS (
        SELECT 1 FROM SCHEMA_TOKEN.exchange_positions AS prior_position
        WHERE prior_position.execution_target_id = 'OKX_DEMO'
          AND prior_position.quantity <> 0
    ))
    THEN
        RAISE EXCEPTION 'controlled canary durable boundary is occupied';
    END IF;

    SELECT * INTO instrument_row
    FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
    WHERE snapshot_id = p_payload->>'instrument_snapshot_id'
      AND kind = 'instrument';
    SELECT * INTO market_row
    FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
    WHERE snapshot_id = p_payload->>'market_snapshot_id'
      AND kind = 'market';
    SELECT * INTO account_row
    FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
    WHERE snapshot_id = p_payload->>'account_snapshot_id'
      AND kind = 'account';
    IF instrument_row.database_id IS NULL
       OR market_row.database_id IS NULL
       OR account_row.database_id IS NULL
       OR instrument_row.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
       OR market_row.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
       OR account_row.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
       OR instrument_row.source_type IS DISTINCT FROM 'api_aggregate'
       OR market_row.source_type IS DISTINCT FROM 'api_aggregate'
       OR account_row.source_type IS DISTINCT FROM 'api_aggregate'
       OR instrument_row.core_data IS NOT TRUE
       OR market_row.core_data IS NOT TRUE
       OR account_row.core_data IS NOT TRUE
       OR instrument_row.content_json::jsonb->>'execution_target'
            IS DISTINCT FROM 'OKX_DEMO'
       OR market_row.content_json::jsonb->>'execution_target'
            IS DISTINCT FROM 'OKX_DEMO'
       OR account_row.content_json::jsonb->>'execution_target'
            IS DISTINCT FROM 'OKX_DEMO'
       OR instrument_row.content_json::jsonb->>'source'
            IS DISTINCT FROM 'okx_demo_rest'
       OR market_row.content_json::jsonb->>'source'
            IS DISTINCT FROM 'okx_demo_rest'
       OR account_row.content_json::jsonb->>'source'
            IS DISTINCT FROM 'okx_demo_rest'
       OR instrument_row.content_json::jsonb->>'resource'
            IS DISTINCT FROM 'instrument'
       OR market_row.content_json::jsonb->>'resource'
            IS DISTINCT FROM 'market'
       OR account_row.content_json::jsonb->>'resource'
            IS DISTINCT FROM 'account'
       OR instrument_row.content_json::jsonb->'stale'
            IS DISTINCT FROM 'false'::jsonb
       OR market_row.content_json::jsonb->'stale'
            IS DISTINCT FROM 'false'::jsonb
       OR account_row.content_json::jsonb->'stale'
            IS DISTINCT FROM 'false'::jsonb
       OR instrument_row.content_json::jsonb->>'instId'
            IS DISTINCT FROM 'BTC-USDT-SWAP'
       OR instrument_row.content_json::jsonb->>'state' IS DISTINCT FROM 'live'
       OR (instrument_row.content_json::jsonb->>'contract_shape'
            IN ('linear', 'inverse')) IS DISTINCT FROM TRUE
       OR market_row.content_json::jsonb->>'instrument_id'
            IS DISTINCT FROM 'BTC-USDT-SWAP'
       OR account_row.content_json::jsonb->'authenticated'
            IS DISTINCT FROM 'true'::jsonb
       OR (instrument_row.content_json::jsonb->>'ctVal'
            ~ '^[0-9]+(\\.[0-9]+)?$') IS DISTINCT FROM TRUE
       OR (instrument_row.content_json::jsonb->>'minSz'
            ~ '^[0-9]+(\\.[0-9]+)?$') IS DISTINCT FROM TRUE
       OR (instrument_row.content_json::jsonb->>'lotSz'
            ~ '^[0-9]+(\\.[0-9]+)?$') IS DISTINCT FROM TRUE
       OR (instrument_row.content_json::jsonb->>'tickSz'
            ~ '^[0-9]+(\\.[0-9]+)?$') IS DISTINCT FROM TRUE
       OR (market_row.content_json::jsonb->>'reference_price'
            ~ '^[0-9]+(\\.[0-9]+)?$') IS DISTINCT FROM TRUE
       OR (market_row.content_json::jsonb->'bbo'->>'bid_price'
            ~ '^[0-9]+(\\.[0-9]+)?$') IS DISTINCT FROM TRUE
       OR (market_row.content_json::jsonb->'bbo'->>'ask_price'
            ~ '^[0-9]+(\\.[0-9]+)?$') IS DISTINCT FROM TRUE
       OR (market_row.content_json::jsonb->'mark'->>'price'
            ~ '^[0-9]+(\\.[0-9]+)?$') IS DISTINCT FROM TRUE
       OR (account_row.content_json::jsonb->'leverage_by_position_side'->>'long'
            ~ '^[0-9]+(\\.[0-9]+)?$') IS DISTINCT FROM TRUE
       OR (market_row.content_json::jsonb->>'as_of') IS NULL
       OR (market_row.content_json::jsonb->>'as_of')::timestamptz
            < statement_timestamp() - INTERVAL '30 seconds'
       OR (market_row.content_json::jsonb->>'as_of')::timestamptz
            > statement_timestamp() + INTERVAL '5 seconds'
       OR instrument_row.attested_session_id
            IS DISTINCT FROM market_row.attested_session_id
       OR instrument_row.attested_session_id
            IS DISTINCT FROM account_row.attested_session_id
       OR instrument_row.expires_at <= statement_timestamp()
       OR market_row.expires_at <= statement_timestamp()
       OR account_row.expires_at <= statement_timestamp()
       OR expires_at > instrument_row.expires_at
       OR expires_at > market_row.expires_at
       OR expires_at > account_row.expires_at
       OR NOT EXISTS (
           SELECT 1 FROM SCHEMA_TOKEN.okx_demo_attested_sessions AS session
           WHERE session.session_id = instrument_row.attested_session_id
             AND session.execution_target_id = 'OKX_DEMO'
             AND session.revoked_at IS NULL
             AND session.expires_at > statement_timestamp()
       )
       OR p_payload->'request_snapshot'#>>'{snapshot_evidence,instrument,snapshot_id}'
            IS DISTINCT FROM instrument_row.snapshot_id
       OR p_payload->'request_snapshot'#>>'{snapshot_evidence,market,snapshot_id}'
            IS DISTINCT FROM market_row.snapshot_id
       OR p_payload->'request_snapshot'#>>'{snapshot_evidence,account,snapshot_id}'
            IS DISTINCT FROM account_row.snapshot_id
       OR p_payload->'request_snapshot'#>>'{snapshot_evidence,instrument,digest}'
            IS DISTINCT FROM instrument_row.digest
       OR p_payload->'request_snapshot'#>>'{snapshot_evidence,market,digest}'
            IS DISTINCT FROM market_row.digest
       OR p_payload->'request_snapshot'#>>'{snapshot_evidence,account,digest}'
            IS DISTINCT FROM account_row.digest
       OR (p_payload->'request_snapshot'#>>'{snapshot_evidence,instrument,database_id}'
            ~ '^[1-9][0-9]*$') IS DISTINCT FROM TRUE
       OR (p_payload->'request_snapshot'#>>'{snapshot_evidence,market,database_id}'
            ~ '^[1-9][0-9]*$') IS DISTINCT FROM TRUE
       OR (p_payload->'request_snapshot'#>>'{snapshot_evidence,account,database_id}'
            ~ '^[1-9][0-9]*$') IS DISTINCT FROM TRUE
       OR (p_payload->'request_snapshot'#>>'{snapshot_evidence,instrument,database_id}')::bigint
            IS DISTINCT FROM instrument_row.database_id
       OR (p_payload->'request_snapshot'#>>'{snapshot_evidence,market,database_id}')::bigint
            IS DISTINCT FROM market_row.database_id
       OR (p_payload->'request_snapshot'#>>'{snapshot_evidence,account,database_id}')::bigint
            IS DISTINCT FROM account_row.database_id
       OR (p_payload->'request_snapshot'#>>'{snapshot_evidence,instrument,expires_at}')::timestamptz
            IS DISTINCT FROM instrument_row.expires_at
       OR (p_payload->'request_snapshot'#>>'{snapshot_evidence,market,expires_at}')::timestamptz
            IS DISTINCT FROM market_row.expires_at
       OR (p_payload->'request_snapshot'#>>'{snapshot_evidence,account,expires_at}')::timestamptz
            IS DISTINCT FROM account_row.expires_at
       OR p_payload->'request_snapshot'->'canonical_input'->'snapshot_ids'
            IS DISTINCT FROM jsonb_build_object(
                'instrument', instrument_row.snapshot_id,
                'market', market_row.snapshot_id,
                'account', account_row.snapshot_id
            )
       OR p_payload->'request_snapshot'->'snapshot_evidence'
            IS DISTINCT FROM jsonb_build_object(
                'instrument', jsonb_build_object(
                    'snapshot_id', instrument_row.snapshot_id,
                    'database_id', instrument_row.database_id,
                    'digest', instrument_row.digest,
                    'expires_at', p_payload->'request_snapshot'
                        #>>'{snapshot_evidence,instrument,expires_at}'
                ),
                'market', jsonb_build_object(
                    'snapshot_id', market_row.snapshot_id,
                    'database_id', market_row.database_id,
                    'digest', market_row.digest,
                    'expires_at', p_payload->'request_snapshot'
                        #>>'{snapshot_evidence,market,expires_at}'
                ),
                'account', jsonb_build_object(
                    'snapshot_id', account_row.snapshot_id,
                    'database_id', account_row.database_id,
                    'digest', account_row.digest,
                    'expires_at', p_payload->'request_snapshot'
                        #>>'{snapshot_evidence,account,expires_at}'
                )
            )
    THEN
        RAISE EXCEPTION 'controlled canary attested snapshot binding failed';
    END IF;

    minimum_size := (instrument_row.content_json->>'minSz')::numeric;
    lot_size := (instrument_row.content_json->>'lotSz')::numeric;
    contract_value := (instrument_row.content_json->>'ctVal')::numeric;
    tick_size := (instrument_row.content_json->>'tickSz')::numeric;
    best_bid := (market_row.content_json->'bbo'->>'bid_price')::numeric;
    best_ask := (market_row.content_json->'bbo'->>'ask_price')::numeric;
    mark_price := (market_row.content_json->'mark'->>'price')::numeric;
    snapshot_leverage := (
        account_row.content_json->'leverage_by_position_side'->>'long'
    )::numeric;
    derived_quantity := ceil(minimum_size / lot_size) * lot_size;
    derived_limit_price := floor(best_bid / tick_size) * tick_size;
    computed_notional := derived_quantity
        * contract_value
        * greatest(reference_price, best_ask, derived_limit_price);
    IF contract_value <= 0
       OR minimum_size <= 0
       OR lot_size <= 0
       OR tick_size <= 0
       OR best_bid <= 0
       OR best_ask <= 0
       OR mark_price <= 0
       OR snapshot_leverage <= 0
       OR (market_row.content_json->>'reference_price')::numeric
            IS DISTINCT FROM mark_price
       OR reference_price IS DISTINCT FROM (
            market_row.content_json->>'reference_price'
       )::numeric
       OR reference_price IS DISTINCT FROM mark_price
       OR leverage IS DISTINCT FROM snapshot_leverage
       OR quantity IS DISTINCT FROM derived_quantity
       OR limit_price IS DISTINCT FROM derived_limit_price
       OR stop_loss IS DISTINCT FROM reference_price * 0.95
       OR take_profit IS DISTINCT FROM reference_price * 1.05
       OR abs(limit_price - reference_price) / reference_price > 0.01
       OR computed_notional IS DISTINCT FROM notional
       OR computed_notional > 20
    THEN
        RAISE EXCEPTION 'controlled canary order derivation mismatch';
    END IF;

    IF is_replay THEN
        RETURN jsonb_build_object(
            'trade_intent_id', existing_intent.id,
            'risk_decision_id', decision_row.id,
            'approved_execution_id', approval_row.id
        );
    END IF;

    INSERT INTO SCHEMA_TOKEN.trade_intents (
        execution_target_id, authorization_schema_version, intent_id,
        canonical_hash, policy_digest, approved_payload_hash,
        idempotency_key_digest, client_order_id, instrument_id, side,
        position_side, order_type, quantity, limit_price, reference_price,
        leverage, margin_mode, stop_loss, take_profit, reduce_only, status,
        request_snapshot, expires_at
    ) VALUES (
        'OKX_DEMO', 'RISK_V1', p_payload->>'intent_id',
        p_payload->>'canonical_hash', p_payload->>'policy_digest',
        p_payload->>'approved_payload_hash',
        p_payload->>'idempotency_key_digest', p_payload->>'client_order_id',
        'BTC-USDT-SWAP', 'buy', 'long', 'limit', quantity, limit_price,
        reference_price, leverage, 'isolated', stop_loss, take_profit,
        FALSE, 'APPROVED', (p_payload->'request_snapshot')::json, expires_at
    ) RETURNING id INTO inserted_intent_id;

    INSERT INTO SCHEMA_TOKEN.risk_decisions (
        execution_target_id, trade_intent_id, authorization_schema_version,
        policy_digest, decision, policy_version, evidence_snapshot
    ) VALUES (
        'OKX_DEMO', inserted_intent_id, 'RISK_V1',
        p_payload->>'policy_digest', 'APPROVED', 'controlled-canary-v1',
        jsonb_build_object(
            'reasons', '[]'::jsonb,
            'input_digest', p_payload->>'canonical_hash',
            'policy_digest', p_payload->>'policy_digest',
            'lineage', p_payload->'request_snapshot'->'canonical_input'->'lineage',
            'notional', p_payload->>'notional',
            'provenance', 'CONTROLLED_CANARY_NON_PRODUCTION',
            'non_production', TRUE,
            'llm_authority', FALSE
        )::json
    ) RETURNING id INTO inserted_decision_id;

    INSERT INTO SCHEMA_TOKEN.approved_executions (
        execution_target_id, trade_intent_id, risk_decision_id, intent_id,
        client_order_id, authorization_schema_version, canonical_hash,
        policy_digest, approved_payload_hash, instrument_snapshot_id,
        market_snapshot_id, account_snapshot_id, decision, intent_status,
        reserved_notional, order_submission_authorized, claim_required,
        status, expires_at, evidence_snapshot
    ) VALUES (
        'OKX_DEMO', inserted_intent_id, inserted_decision_id,
        p_payload->>'intent_id', p_payload->>'client_order_id', 'RISK_V1',
        p_payload->>'canonical_hash', p_payload->>'policy_digest',
        p_payload->>'approved_payload_hash',
        p_payload->>'instrument_snapshot_id',
        p_payload->>'market_snapshot_id',
        p_payload->>'account_snapshot_id',
        'APPROVED', 'APPROVED', notional, FALSE, TRUE, 'ACTIVE', expires_at,
        jsonb_build_object(
            'provenance', 'CONTROLLED_CANARY_NON_PRODUCTION',
            'non_production', TRUE,
            'lineage', p_payload->'request_snapshot'->'canonical_input'->'lineage',
            'snapshot_evidence', p_payload->'request_snapshot'->'snapshot_evidence'
        )::json
    ) RETURNING id INTO inserted_approval_id;

    RETURN jsonb_build_object(
        'trade_intent_id', inserted_intent_id,
        'risk_decision_id', inserted_decision_id,
        'approved_execution_id', inserted_approval_id
    );
EXCEPTION
    WHEN unique_violation THEN
        RAISE EXCEPTION 'controlled canary lineage identity conflict';
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
    rendered = rendered.replace("!=", "<>")
    while True:
        unwrapped = re.sub(
            r"\(([a-z_][a-z0-9_]*(?:(?:<>|=)[^()]*|isnotnull|isnull))\)",
            r"\1",
            rendered,
        )
        unwrapped = re.sub(r"\(([a-z_][a-z0-9_]*)\)", r"\1", unwrapped)
        if unwrapped == rendered:
            break
        rendered = unwrapped
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
    "okx_demo_accepted_not_found_kind_check",
    "okx_demo_accepted_not_found_fact_check",
    "okx_demo_accepted_not_found_identity_check",
    "okx_demo_submission_grants_target_check",
    "okx_demo_submission_grants_status_check",
    "okx_demo_submission_grants_digest_check",
    "okx_demo_submission_grants_provenance_check",
    "okx_demo_submission_grants_time_check",
    "okx_demo_submission_grants_risk_check",
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
        "okx_demo_submission_grants_guard": (
            "beforeupdateon",
            "invalidone-shotsubmissiongranttransition",
            "one-shotsubmissiongrantidentityisimmutable",
            "old.status<>'active'",
        ),
        "exchange_orders_guard": (
            "beforeinsertorupdateon",
            "invalidexchangeordercreation",
            "invalidexchangeordertransition",
            "old.request_snapshot::jsonb",
            "old.exchange_order_idisnotnull",
        ),
        "strategy_validation_plans_immutable": (
            "beforedeleteorupdateon",
            "strategyvalidationplansareimmutable",
            "old.plan_digestisdistinctfromnew.plan_digest",
        ),
        "strategy_validation_windows_immutable": (
            "beforedeleteorupdateon",
            "strategyvalidationwindowsareimmutable",
            "old.execution_idisnotnull",
            "old.expected_market_data_digestisdistinctfromnew.expected_market_data_digest",
        ),
        "strategy_research_attempt_events_immutable": (
            "beforedeleteorupdateon",
            "strategy_research_attempt_events",
            "researchreceiptsareappend-only",
        ),
        "market_data_quality_receipts_immutable": (
            "beforedeleteorupdateon",
            "market_data_quality_receipts",
            "researchreceiptsareappend-only",
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
    for table_name in RUNTIME_APPEND_ONLY_TABLES:
        owner = bind.execute(
            text(
                "SELECT tableowner FROM pg_tables "
                "WHERE schemaname=:schema_name AND tablename=:table_name"
            ),
            {"schema_name": schema_name, "table_name": table_name},
        ).scalar_one_or_none()
        if owner != "freqtrade_ai_attestor":
            problems.append(f"append-only table owner mismatch: {table_name}")
        privileges = {
            privilege: bind.execute(
                text("SELECT has_table_privilege('freqtrade', :table_name, :privilege)"),
                {
                    "table_name": "{}.{}".format(schema_name, table_name),
                    "privilege": privilege,
                },
            ).scalar_one()
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
        if privileges != {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
        }:
            problems.append(f"append-only table ACL mismatch: {table_name}")
        sequence_identity = bind.execute(
            text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
            {"table_name": "{}.{}".format(schema_name, table_name)},
        ).scalar_one_or_none()
        if sequence_identity:
            sequence_owner = bind.execute(
                text(
                    "SELECT pg_get_userbyid(sequence.relowner) "
                    "FROM pg_class AS sequence "
                    "WHERE sequence.oid=CAST(:sequence AS regclass)"
                ),
                {"sequence": sequence_identity},
            ).scalar_one()
            if sequence_owner != "freqtrade_ai_attestor":
                problems.append(f"append-only sequence owner mismatch: {table_name}")
    function_owner = bind.execute(
        text(
            "SELECT pg_get_userbyid(proowner) FROM pg_proc "
            "WHERE oid=to_regprocedure('prevent_research_receipt_mutation()')"
        )
    ).scalar_one_or_none()
    if function_owner != "freqtrade_ai_attestor":
        problems.append("append-only trigger function owner mismatch")
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
            expected_acl = (
                "freqtrade_ai_attestor",
                True,
                True,
                False,
                False,
                False,
            )
            if reconciliation_sequence_acl.get(expected_name) != expected_acl:
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
                      'freeze_okx_demo_reconciliation_gate',
                      'create_okx_demo_canary_lineage'
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
        (
            canary_owner,
            canary_security_definer,
            canary_config,
            canary_runtime_execute,
            canary_public_execute,
            canary_source,
        ) = function_security.get(
            "create_okx_demo_canary_lineage",
            (None, False, [], False, True, ""),
        )
        expected_canary_hash = _canonical_function_body(
            CANARY_LINEAGE_FUNCTION_BODY.replace(
                "SCHEMA_TOKEN",
                schema_name,
            ),
            schema_name,
        )
        if (
            canary_owner != "freqtrade_ai_attestor"
            or canary_security_definer is not True
            or "search_path=pg_catalog" not in canary_config
            or canary_runtime_execute is not True
            or canary_public_execute is True
            or _canonical_function_body(canary_source, schema_name)
            != expected_canary_hash
        ):
            problems.append("controlled canary lineage function boundary mismatch")
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
        problems.extend(_one_shot_submission_grant_acl_problems(bind, schema_name))
        problems.extend(_canary_lifecycle_acl_problems(bind, schema_name))
        problems.extend(_canary_consent_acl_problems(bind, schema_name))
        problems.extend(_accepted_not_found_boundary_problems(bind, schema_name))
        problems.extend(_continuous_demo_automation_boundary_problems(bind, schema_name))
        problems.extend(_expired_approval_attestor_acl_problems(bind, schema_name))
        problems.extend(_strategy_validation_acl_problems(bind, schema_name))
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


def _required_operator_consent_proof_key() -> bytes:
    operator_token = os.environ.get(OPERATOR_TOKEN_ENV, "")
    if len(operator_token) < 16:
        raise SchemaMigrationBlocked(
            "{} must be configured before hardening operator consent".format(
                OPERATOR_TOKEN_ENV
            )
        )
    return hashlib.sha256(operator_token.encode("utf-8")).digest()


def _converge_operator_consent_proof_key(connection: Connection) -> None:
    """Admin-only key provisioning; runtime roles have no secret-table access."""

    schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(schema_name)
    table = "{}.okx_demo_operator_consent_secrets".format(quoted_schema)
    connection.execute(text(
        "ALTER TABLE {} OWNER TO freqtrade_ai_attestor".format(table)
    ))
    connection.execute(text(
        "REVOKE ALL ON TABLE {} FROM PUBLIC,freqtrade".format(table)
    ))
    proof_key = _required_operator_consent_proof_key()
    current_key = connection.execute(text(
        "SELECT hmac_key FROM {} WHERE secret_id"
        "='ACTIVE'".format(table)
    )).scalar_one_or_none()
    if current_key != proof_key:
        nonterminal = connection.execute(text(
            "SELECT count(*) FROM {}.okx_demo_canary_consent_handoffs "
            "WHERE status IN ('REQUESTED','FINALIZING','FINALIZED','GRANT_ISSUED')"
            .format(quoted_schema)
        )).scalar_one()
        if nonterminal:
            raise SchemaMigrationBlocked(
                "operator consent proof rotation is blocked by an active handoff"
            )
    connection.execute(text(
        "INSERT INTO {}(secret_id,hmac_key) VALUES('ACTIVE',:proof_key) "
        "ON CONFLICT(secret_id) DO UPDATE SET hmac_key=EXCLUDED.hmac_key"
        .format(table)
    ), {"proof_key": proof_key})
    if connection.execute(text(
        "SELECT hmac_key IS NOT DISTINCT FROM :proof_key FROM {} "
        "WHERE secret_id"
        "='ACTIVE'".format(table)
    ), {"proof_key": proof_key}).scalar_one_or_none() is not True:
        raise SchemaMigrationBlocked("operator consent proof hardening failed")


def harden_operator_consent_access_boundary(engine: Engine) -> None:
    """Provision or rotate the consent verifier using a peer-admin connection."""

    with engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            raise SchemaMigrationBlocked(
                "Operator consent hardening requires PostgreSQL."
            )
        _require_attestation_admin(connection)
        schema_name = connection.execute(text("SELECT current_schema()" )).scalar_one()
        if not connection.execute(text(
            "SELECT to_regclass(:table) IS NOT NULL"
        ), {"table": "{}.okx_demo_operator_consent_secrets".format(schema_name)}).scalar_one():
            raise SchemaMigrationBlocked(
                "operator consent schema must be migrated before hardening"
            )
        _converge_operator_consent_proof_key(connection)


def revoke_operator_consents_for_key_hardening(engine: Engine) -> int:
    """Explicit peer-admin revocation required before restore/key rotation."""

    with engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            raise SchemaMigrationBlocked(
                "Operator consent revocation requires PostgreSQL."
            )
        _require_attestation_admin(connection)
        return int(connection.execute(text(
            "SELECT revoke_all_okx_demo_canary_consents_for_hardening()"
        )).scalar_one())


def revoke_attested_sessions_for_key_hardening(engine: Engine) -> int:
    """Explicit peer-admin revocation before restoring an attestation key."""

    with engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            raise SchemaMigrationBlocked(
                "Attested session revocation requires PostgreSQL."
            )
        _require_attestation_admin(connection)
        result = connection.execute(text(
            "UPDATE okx_demo_attested_sessions SET "
            "revoked_at=clock_timestamp(),revoke_reason='WRITE_FAILURE' "
            "WHERE revoked_at IS NULL"
        ))
        return int(result.rowcount or 0)


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
    # Fresh metadata creation can materialize the v32 receipt before this legacy
    # pre-release table replacement.  It is necessarily empty here and is
    # recreated with its final owner/FKs by the v32 boundary.
    receipt_table = "{}.{}".format(
        quote(schema_name), quote("okx_demo_accepted_not_found_terminalizations")
    )
    if connection.execute(
        text("SELECT to_regclass(:table_name) IS NOT NULL"),
        {"table_name": receipt_table},
    ).scalar_one():
        if connection.execute(
            text("SELECT count(*) FROM {}".format(receipt_table))
        ).scalar_one():
            raise SchemaMigrationBlocked(
                "Refusing to replace writer tables with accepted terminalization history"
            )
        connection.execute(text("DROP TABLE {} CASCADE".format(receipt_table)))
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


def _grant_expired_approval_attestor_acl(connection: Connection) -> None:
    schema_name = connection.execute(text("SELECT current_schema() ")).scalar_one()
    required_columns = {
        "approved_executions": {
            "id",
            "trade_intent_id",
            "risk_decision_id",
            "execution_target_id",
            "status",
            "expires_at",
            "reserved_notional",
            "evidence_snapshot",
        },
        "trade_intents": {"id", "expires_at"},
        "risk_decisions": {
            "id",
            "execution_target_id",
            "decision",
            "evidence_snapshot",
        },
        "full_chain_runs": {
            "id",
            "research_job_id",
            "research_job_attempt_id",
            "run_kind",
            "signal_evaluation_id",
            "research_scope_id",
            "approved_execution_id",
            "execution_target_id",
            "status",
            "current_stage",
            "strategy_generation_run_id",
            "strategy_id",
            "strategy_version_id",
            "backtest_run_id",
            "backtest_task_id",
            "backtest_result_id",
            "strategy_score_id",
            "candidate_approval_id",
            "signal_snapshot_id",
            "trade_intent_id",
            "risk_decision_id",
            "exchange_order_id",
            "terminal_reason",
            "completed_at",
        },
        "risk_budgets": {
            "execution_target_id",
            "reserved_notional",
            "approved_positions",
        },
    }
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names(schema=schema_name))
    for table_name, columns in required_columns.items():
        if table_name not in existing_tables:
            return
        actual = {
            column["name"]
            for column in inspector.get_columns(table_name, schema=schema_name)
        }
        if not columns.issubset(actual):
            # Earlier versioned upgrades add these columns later in the same
            # transaction.  Deferring this ACL step keeps those upgrades
            # atomic without issuing GRANTs against a pre-lineage table.
            return
    quoted_schema = connection.dialect.identifier_preparer.quote(schema_name)
    connection.execute(
        text(
            """
            REVOKE SELECT, UPDATE ON __SCHEMA__.risk_budgets
                FROM freqtrade_ai_attestor;
            GRANT SELECT (id, trade_intent_id, risk_decision_id,
                          execution_target_id, status, expires_at,
                          reserved_notional, evidence_snapshot),
                  UPDATE (status, evidence_snapshot)
                ON __SCHEMA__.approved_executions TO freqtrade_ai_attestor;
            GRANT SELECT (id, expires_at)
                ON __SCHEMA__.trade_intents TO freqtrade_ai_attestor;
            GRANT SELECT (id, execution_target_id, decision, evidence_snapshot),
                  UPDATE (evidence_snapshot)
                ON __SCHEMA__.risk_decisions TO freqtrade_ai_attestor;
            GRANT SELECT (id, research_job_id, research_job_attempt_id,
                          run_kind, signal_evaluation_id, research_scope_id,
                          execution_target_id, status, current_stage,
                          strategy_generation_run_id, strategy_id,
                          strategy_version_id, backtest_run_id,
                          backtest_task_id, backtest_result_id,
                          strategy_score_id, candidate_approval_id,
                          signal_snapshot_id, trade_intent_id,
                          risk_decision_id, approved_execution_id,
                          exchange_order_id),
                  UPDATE (status, terminal_reason, completed_at)
                ON __SCHEMA__.full_chain_runs TO freqtrade_ai_attestor;
            GRANT SELECT (execution_target_id, reserved_notional,
                          approved_positions),
                  UPDATE (reserved_notional, approved_positions)
                ON __SCHEMA__.risk_budgets TO freqtrade_ai_attestor;
            """.replace("__SCHEMA__", quoted_schema)
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
                       'PREPARED', 'DISPATCHED', 'ACKNOWLEDGED', 'REJECTED',
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
            REVOKE SELECT, UPDATE ON __SCHEMA__.risk_budgets
                FROM freqtrade_ai_attestor;
            GRANT SELECT (id, trade_intent_id, risk_decision_id,
                          execution_target_id, status, expires_at,
                          reserved_notional, evidence_snapshot),
                  UPDATE (status, evidence_snapshot)
                ON __SCHEMA__.approved_executions
                TO freqtrade_ai_attestor;
            GRANT SELECT (id, expires_at)
                ON __SCHEMA__.trade_intents
                TO freqtrade_ai_attestor;
            GRANT SELECT (id, execution_target_id, decision,
                          evidence_snapshot),
                  UPDATE (evidence_snapshot)
                ON __SCHEMA__.risk_decisions
                TO freqtrade_ai_attestor;
            GRANT SELECT (approved_execution_id, execution_target_id),
                  UPDATE (status, terminal_reason, completed_at)
                ON __SCHEMA__.full_chain_runs
                TO freqtrade_ai_attestor;
            GRANT SELECT (execution_target_id, reserved_notional,
                          approved_positions),
                  UPDATE (reserved_notional, approved_positions)
                ON __SCHEMA__.risk_budgets
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
                SELECT approved.id,
                       approved.trade_intent_id,
                       approved.risk_decision_id,
                       approved.execution_target_id,
                       approved.status,
                       approved.expires_at,
                       approved.reserved_notional,
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
            quote(column["name"])
            for column in inspect(connection).get_columns(
                table_name, schema=schema_name
            )
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
                    IF NEW.client_order_id ~ '^rcv[0-9]{20}(C[1-3])?$' THEN
                        v_recovery_grant_id :=
                            substring(NEW.client_order_id from 4 for 20)::BIGINT;
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
                                 AND (
                                   NEW.client_order_id !~ 'C[1-3]$'
                                   OR EXISTS (
                                     SELECT 1
                                     FROM __SCHEMA__.okx_demo_canary_lifecycles l
                                     JOIN __SCHEMA__.okx_order_write_attempts parent
                                       ON parent.approval_id=l.cleanup_approval_id
                                      AND parent.operation='CLOSE'
                                      AND parent.state='RESIDUAL_CLOSE_REQUIRED'
                                      AND parent.close_sequence+1=substring(NEW.client_order_id from 25)::integer
                                     JOIN __SCHEMA__.exchange_orders parent_order
                                       ON parent_order.id=parent.exchange_order_row_id
                                      AND parent_order.trade_intent_id=l.cleanup_trade_intent_id
                                     JOIN __SCHEMA__.okx_demo_recovery_grants old_grant
                                       ON old_grant.database_id=parent.recovery_grant_database_id
                                      AND old_grant.lifecycle_id=l.lifecycle_id
                                      AND old_grant.status='CONSUMED'
                                    WHERE l.lifecycle_id=recovery_grant.lifecycle_id
                                      AND l.cleanup_phase='CLEANUP_PENDING'
                                      AND l.outcome='FAILED'
                                      AND NEW.trade_intent_id=l.cleanup_trade_intent_id
                                      AND intent.reduce_only IS TRUE
                                      AND intent.order_type='market'
                                      AND recovery_grant.max_quantity=(intent.quantity-(
                                        SELECT COALESCE(sum(COALESCE(NULLIF(a.safe_response_snapshot::jsonb->>'accumulated_fill_size','')::numeric,0)),0)
                                        FROM __SCHEMA__.okx_order_write_attempts a
                                        JOIN __SCHEMA__.exchange_orders eo ON eo.id=a.exchange_order_row_id
                                        WHERE eo.trade_intent_id=l.cleanup_trade_intent_id AND a.operation='CLOSE'))
                                      AND NEW.request_snapshot::jsonb=jsonb_build_object(
                                        'instId',intent.instrument_id,'tdMode','isolated',
                                        'side',CASE WHEN intent.position_side='long' THEN 'sell' ELSE 'buy' END,
                                        'posSide',intent.position_side,'ordType','market',
                                        'sz',recovery_grant.max_quantity::text,
                                        'clOrdId',NEW.client_order_id,'reduceOnly',TRUE)
                                      AND NOT EXISTS(
                                        SELECT 1 FROM __SCHEMA__.okx_order_write_attempts other
                                        WHERE other.state IN('PREPARED','DISPATCHED','ACKNOWLEDGED','RECOVERY_REQUIRED','RESIDUAL_CLOSE_REQUIRED')
                                          AND other.id<>parent.id)
                                   )
                                 )
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
                IF NEW.status = 'USER_ACCEPTED_NOT_FOUND_NO_FILL' THEN
                    IF OLD.status <> 'RECOVERY_REQUIRED'
                       OR OLD.id IS DISTINCT FROM NEW.id
                       OR OLD.execution_target_id IS DISTINCT FROM NEW.execution_target_id
                       OR OLD.trade_intent_id IS DISTINCT FROM NEW.trade_intent_id
                       OR OLD.client_order_id IS DISTINCT FROM NEW.client_order_id
                       OR OLD.request_snapshot::jsonb IS DISTINCT FROM NEW.request_snapshot::jsonb
                       OR OLD.created_at IS DISTINCT FROM NEW.created_at
                       OR OLD.exchange_order_id IS NOT NULL
                       OR NEW.exchange_order_id IS NOT NULL
                       OR OLD.response_snapshot::jsonb IS DISTINCT FROM
                          NEW.response_snapshot::jsonb
                       OR NOT EXISTS (
                           SELECT 1
                           FROM __SCHEMA__.okx_demo_accepted_not_found_terminalizations receipt
                           WHERE receipt.exchange_order_row_id=OLD.id
                             AND receipt.request_digest IN (
                               SELECT attempt.request_digest
                               FROM __SCHEMA__.okx_order_write_attempts attempt
                               WHERE attempt.exchange_order_row_id=OLD.id
                                 AND attempt.operation='PLACE'
                                 AND attempt.attempt_count=1
                             )
                             AND receipt.attempt_id IN (
                               SELECT attempt.id
                               FROM __SCHEMA__.okx_order_write_attempts attempt
                               WHERE attempt.exchange_order_row_id=OLD.id
                                 AND attempt.operation='PLACE'
                                 AND attempt.attempt_count=1
                             )
                       ) THEN
                        RAISE EXCEPTION 'invalid accepted NOT_FOUND exchange order transition';
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
                       'PREPARED', 'DISPATCHED', 'ACKNOWLEDGED', 'REJECTED',
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
    _ensure_one_shot_submission_grant(connection)
    _grant_expired_approval_attestor_acl(connection)
    _add_canary_lineage_write_boundary(connection)


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


def _add_research_receipt_boundary(connection: Connection) -> None:
    """Make research attempt and market-data receipts append-only in PostgreSQL."""

    for table_name in RUNTIME_APPEND_ONLY_TABLES:
        Base.metadata.tables[table_name].create(bind=connection, checkfirst=True)
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION prevent_research_receipt_mutation()
            RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
            BEGIN
                RAISE EXCEPTION 'research receipts are append-only';
            END;
            $$;
            DROP TRIGGER IF EXISTS strategy_research_attempt_events_immutable
                ON strategy_research_attempt_events;
            CREATE TRIGGER strategy_research_attempt_events_immutable
                BEFORE UPDATE OR DELETE ON strategy_research_attempt_events
                FOR EACH ROW EXECUTE FUNCTION prevent_research_receipt_mutation();

            DROP TRIGGER IF EXISTS market_data_quality_receipts_immutable
                ON market_data_quality_receipts;
            CREATE TRIGGER market_data_quality_receipts_immutable
                BEFORE UPDATE OR DELETE ON market_data_quality_receipts
                FOR EACH ROW EXECUTE FUNCTION prevent_research_receipt_mutation();
            """
        )
    )
    schema_name = connection.execute(text("SELECT current_schema()" )).scalar_one()
    quote = connection.dialect.identifier_preparer.quote
    qualified_schema = quote(schema_name)
    connection.execute(
        text(
            "ALTER FUNCTION prevent_research_receipt_mutation() "
            "OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON FUNCTION prevent_research_receipt_mutation() "
            "FROM PUBLIC, freqtrade"
        )
    )
    for table_name in RUNTIME_APPEND_ONLY_TABLES:
        qualified_table = "{}.{}".format(qualified_schema, quote(table_name))
        connection.execute(
            text(
                "ALTER TABLE {} OWNER TO freqtrade_ai_attestor; "
                "REVOKE ALL ON TABLE {} FROM PUBLIC, freqtrade; "
                "GRANT SELECT, INSERT ON TABLE {} TO freqtrade".format(
                    qualified_table, qualified_table, qualified_table
                )
            )
        )
        sequence_identity = connection.execute(
            text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
            {"table_name": "{}.{}".format(schema_name, table_name)},
        ).scalar_one()
        if sequence_identity:
            connection.execute(
                text(
                    "ALTER SEQUENCE {} OWNER TO freqtrade_ai_attestor; "
                    "REVOKE ALL ON SEQUENCE {} FROM PUBLIC, freqtrade; "
                    "GRANT USAGE, SELECT ON SEQUENCE {} TO freqtrade".format(
                        sequence_identity, sequence_identity, sequence_identity
                    )
                )
            )


def _add_one_shot_submission_grant_boundary(connection: Connection) -> None:
    """Install the narrow ACL and irreversible DB transition guard."""

    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION guard_okx_demo_submission_grant()
            RETURNS trigger LANGUAGE plpgsql
            SECURITY DEFINER SET search_path = pg_catalog AS $$
            BEGIN
                IF OLD.status <> 'ACTIVE'
                   OR NEW.status NOT IN ('CONSUMED', 'EXPIRED', 'FAILED') THEN
                    RAISE EXCEPTION 'invalid one-shot submission grant transition';
                END IF;
                IF OLD.grant_id IS DISTINCT FROM NEW.grant_id
                   OR OLD.execution_target_id IS DISTINCT FROM NEW.execution_target_id
                   OR OLD.approval_id IS DISTINCT FROM NEW.approval_id
                   OR OLD.reconciliation_run_id IS DISTINCT FROM NEW.reconciliation_run_id
                   OR OLD.canonical_hash IS DISTINCT FROM NEW.canonical_hash
                   OR OLD.policy_digest IS DISTINCT FROM NEW.policy_digest
                   OR OLD.approved_payload_hash IS DISTINCT FROM NEW.approved_payload_hash
                   OR OLD.client_order_id IS DISTINCT FROM NEW.client_order_id
                   OR OLD.instrument_id IS DISTINCT FROM NEW.instrument_id
                   OR OLD.canary_quantity IS DISTINCT FROM NEW.canary_quantity
                   OR OLD.canary_notional IS DISTINCT FROM NEW.canary_notional
                   OR OLD.request_digest IS DISTINCT FROM NEW.request_digest
                   OR OLD.provenance IS DISTINCT FROM NEW.provenance
                   OR OLD.issued_at IS DISTINCT FROM NEW.issued_at
                   OR OLD.expires_at IS DISTINCT FROM NEW.expires_at THEN
                    RAISE EXCEPTION 'one-shot submission grant identity is immutable';
                END IF;
                IF NEW.consumed_at IS NULL THEN
                    RAISE EXCEPTION 'terminal one-shot grant requires consumed_at';
                END IF;
                RETURN NEW;
            END;
            $$;
            ALTER FUNCTION guard_okx_demo_submission_grant()
                OWNER TO freqtrade_ai_attestor;
            REVOKE ALL ON FUNCTION guard_okx_demo_submission_grant()
                FROM PUBLIC, freqtrade;
            ALTER TABLE okx_demo_submission_grants
                OWNER TO freqtrade_ai_attestor;
            DROP TRIGGER IF EXISTS okx_demo_submission_grants_guard
                ON okx_demo_submission_grants;
            CREATE TRIGGER okx_demo_submission_grants_guard
                BEFORE UPDATE ON okx_demo_submission_grants
                FOR EACH ROW EXECUTE FUNCTION guard_okx_demo_submission_grant();
            REVOKE ALL ON TABLE okx_demo_submission_grants FROM PUBLIC, freqtrade;
            GRANT SELECT, INSERT ON TABLE okx_demo_submission_grants TO freqtrade;
            GRANT UPDATE (status, writer_instance_id, consumed_at)
                ON TABLE okx_demo_submission_grants TO freqtrade;
            """
        )
    )


def _ensure_one_shot_submission_grant(connection: Connection) -> bool:
    """Create the FK-backed grant only after every parent table exists."""

    schema_name = connection.execute(text("SELECT current_schema() ")).scalar_one()
    required = {"execution_scopes", "approved_executions", "reconciliation_runs"}
    if not required.issubset(
        inspect(connection).get_table_names(schema=schema_name)
    ):
        return False
    Base.metadata.tables["okx_demo_submission_grants"].create(
        bind=connection,
        checkfirst=True,
    )
    _add_one_shot_submission_grant_boundary(connection)
    return True


def _add_canary_lineage_write_boundary(connection: Connection) -> None:
    """Install one fixed owner-mediated write path for controlled canary lineage."""

    schema_name, effective_schemas = connection.execute(
        text("SELECT current_schema(), current_schemas(false)")
    ).one()
    if not schema_name or list(effective_schemas or ()) != [schema_name]:
        raise SchemaMigrationBlocked(
            "Canary lineage boundary requires exactly one effective schema"
        )
    missing = CANARY_LINEAGE_BOUNDARY_TABLES - set(
        inspect(connection).get_table_names(schema=schema_name)
    )
    if missing:
        raise SchemaMigrationBlocked(
            "Canary lineage boundary tables are missing: "
            + ", ".join(sorted(missing))
        )
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(schema_name)
    function_sql = """
        CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.create_okx_demo_canary_lineage(
            p_payload jsonb
        ) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$CANARY_BODY$$;
    """
    connection.execute(
        text(
            function_sql.replace("CANARY_BODY", CANARY_LINEAGE_FUNCTION_BODY).replace(
                "SCHEMA_TOKEN", quoted_schema
            )
        )
    )
    connection.execute(
        text(
            "ALTER FUNCTION {}.create_okx_demo_canary_lineage(jsonb) "
            "OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON FUNCTION {}.create_okx_demo_canary_lineage(jsonb) "
            "FROM PUBLIC, freqtrade; "
            "GRANT EXECUTE ON FUNCTION {}.create_okx_demo_canary_lineage(jsonb) "
            "TO freqtrade".format(
                quoted_schema,
                quoted_schema,
                quoted_schema,
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


def _one_shot_submission_grant_acl_problems(
    connection: Connection,
    schema_name: str,
) -> list[str]:
    table_name = "{}.okx_demo_submission_grants".format(schema_name)
    # Older installations can enter schema-problem inspection before the
    # optional one-shot boundary has been created.  Report no grant-specific
    # problem for that pre-boundary state; the migration helper will create it
    # when all of its parent lineage tables are available.  More importantly,
    # do not turn a fail-closed diagnostic into a NoResultError.
    if not connection.execute(
        text("SELECT to_regclass(:table_name) IS NOT NULL"),
        {"table_name": table_name},
    ).scalar_one():
        return []
    can_select, can_insert, can_update_table, can_unsafe = connection.execute(
        text(
            "SELECT has_table_privilege('freqtrade', :table, 'SELECT'), "
            "has_table_privilege('freqtrade', :table, 'INSERT'), "
            "has_table_privilege('freqtrade', :table, 'UPDATE'), "
            "has_table_privilege('freqtrade', :table, "
            "'DELETE,TRUNCATE,REFERENCES,TRIGGER')"
        ),
        {"table": table_name},
    ).one()
    problems = []
    if not can_select or can_insert or can_update_table or can_unsafe:
        problems.append("one-shot submission grant table ACL mismatch")
    boundary = connection.execute(
        text(
            "SELECT table_owner.rolname, "
            "EXISTS (SELECT 1 FROM aclexplode(COALESCE("
            "relation.relacl, acldefault('r', relation.relowner))) AS acl "
            "WHERE acl.grantee = 0), function_owner.rolname, "
            "function.prosecdef, function.proconfig, "
            "EXISTS (SELECT 1 FROM aclexplode(COALESCE("
            "function.proacl, acldefault('f', function.proowner))) AS acl "
            "WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE') "
            "FROM pg_class AS relation "
            "JOIN pg_roles AS table_owner ON table_owner.oid = relation.relowner "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "JOIN pg_proc AS function ON function.pronamespace = namespace.oid "
            "AND function.proname = 'guard_okx_demo_submission_grant' "
            "JOIN pg_roles AS function_owner ON function_owner.oid = function.proowner "
            "WHERE relation.oid = to_regclass(:table)"
        ),
        {"table": table_name},
    ).first()
    if boundary is None:
        return ["one-shot submission grant owner boundary mismatch"]
    owner, public_acl, function_owner, security_definer, function_config, public_execute = boundary
    if (
        owner != "freqtrade_ai_attestor"
        or public_acl
        or function_owner != "freqtrade_ai_attestor"
        or security_definer is not True
        or "search_path=pg_catalog" not in (function_config or [])
        or public_execute
    ):
        problems.append("one-shot submission grant owner boundary mismatch")
    allowed_updates = {"status", "writer_instance_id", "consumed_at"}
    for column in inspect(connection).get_columns(
        "okx_demo_submission_grants", schema=schema_name
    ):
        can_update = connection.execute(
            text(
                "SELECT has_column_privilege('freqtrade', :table, :column, 'UPDATE')"
            ),
            {"table": table_name, "column": column["name"]},
        ).scalar_one()
        if bool(can_update) != (column["name"] in allowed_updates):
            problems.append(
                "one-shot submission grant column ACL mismatch: " + column["name"]
            )
    return problems


def _continuous_demo_automation_boundary_problems(
    connection: Connection,
    schema_name: str,
) -> list[str]:
    """Return owner/ACL/function drift in the standing Demo guard."""

    problems: list[str] = []
    deployment_table = f"{schema_name}.strategy_deployments"
    deployment_acl = connection.execute(
        text(
            "SELECT owner.rolname,"
            "has_table_privilege('freqtrade',:table,'SELECT'),"
            "has_table_privilege('freqtrade',:table,'INSERT,UPDATE,DELETE') "
            "FROM pg_class relation JOIN pg_roles owner "
            "ON owner.oid=relation.relowner WHERE relation.oid=to_regclass(:table)"
        ),
        {"table": deployment_table},
    ).first()
    if (
        deployment_acl is None
        or deployment_acl[0] != "freqtrade_ai_attestor"
        or deployment_acl[1] is not True
        or deployment_acl[2] is True
    ):
        problems.append("continuous Demo deployment table ACL mismatch")
    for table_name in (
        "okx_demo_automation_guard_states",
        "okx_demo_automation_guard_events",
    ):
        table = f"{schema_name}.{table_name}"
        if not connection.execute(
            text("SELECT to_regclass(:table) IS NOT NULL"), {"table": table}
        ).scalar_one():
            problems.append("continuous Demo guard table missing: " + table_name)
            continue
        owner, runtime_select, runtime_write, public_acl = connection.execute(
            text(
                "SELECT owner.rolname,"
                "has_table_privilege('freqtrade',:table,'SELECT'),"
                "has_table_privilege('freqtrade',:table,'INSERT,UPDATE,DELETE'),"
                "EXISTS(SELECT 1 FROM pg_class relation CROSS JOIN LATERAL "
                "aclexplode(COALESCE(relation.relacl,"
                "acldefault('r',relation.relowner))) acl "
                "WHERE relation.oid=to_regclass(:table) AND acl.grantee=0) "
                "FROM pg_class relation JOIN pg_roles owner "
                "ON owner.oid=relation.relowner "
                "WHERE relation.oid=to_regclass(:table)"
            ),
            {"table": table},
        ).one()
        if (
            owner != "freqtrade_ai_attestor"
            or runtime_select is not True
            or runtime_write is True
            or public_acl is True
        ):
            problems.append("continuous Demo guard table ACL mismatch: " + table_name)

    expected_functions = {
        "okx_demo_continuous_opening_allowed(text)": True,
        "claim_okx_demo_continuous_dispatch(bigint,text)": True,
        "record_okx_demo_automation_failure(text,text,bigint,text)": True,
        "record_okx_demo_automation_health(bigint,text)": True,
        "enable_okx_demo_continuous_automation(text,bigint)": False,
        "reset_okx_demo_continuous_automation(text,bigint)": False,
    }
    for signature, runtime_execute in expected_functions.items():
        row = connection.execute(
            text(
                "SELECT owner.rolname,function.prosecdef,function.proconfig,"
                "has_function_privilege('freqtrade',function.oid,'EXECUTE'),"
                "EXISTS(SELECT 1 FROM aclexplode(COALESCE(function.proacl,"
                "acldefault('f',function.proowner))) acl WHERE acl.grantee=0 "
                "AND acl.privilege_type='EXECUTE') FROM pg_proc function "
                "JOIN pg_roles owner ON owner.oid=function.proowner "
                "WHERE function.oid=to_regprocedure(:signature)"
            ),
            {"signature": f"{schema_name}.{signature}"},
        ).first()
        if (
            row is None
            or row[0] != "freqtrade_ai_attestor"
            or row[1] is not True
            or "search_path=pg_catalog" not in (row[2] or [])
            or bool(row[3]) is not runtime_execute
            or row[4] is True
        ):
            problems.append("continuous Demo guard function ACL mismatch: " + signature)
    trigger = connection.execute(
        text(
            "SELECT trigger.tgenabled,function_owner.rolname "
            "FROM pg_trigger trigger JOIN pg_proc function "
            "ON function.oid=trigger.tgfoid JOIN pg_roles function_owner "
            "ON function_owner.oid=function.proowner "
            "WHERE trigger.tgrelid=to_regclass(:table) "
            "AND trigger.tgname='okx_demo_automation_events_immutable' "
            "AND NOT trigger.tgisinternal"
        ),
        {"table": f"{schema_name}.okx_demo_automation_guard_events"},
    ).first()
    if trigger is None or trigger[0] != "O" or trigger[1] != "freqtrade_ai_attestor":
        problems.append("continuous Demo guard append-only trigger mismatch")
    for table_name, trigger_name in (
        ("strategies", "active_demo_strategy_material_immutable"),
        ("strategy_versions", "active_demo_strategy_version_immutable"),
        ("strategy_candidate_approvals", "active_demo_selection_receipt_immutable"),
        ("strategy_scores", "active_demo_strategy_score_immutable"),
        ("backtest_results", "active_demo_backtest_result_immutable"),
        ("backtest_runs", "active_demo_backtest_run_immutable"),
        ("backtest_tasks", "active_demo_backtest_task_immutable"),
        ("full_chain_runs", "active_demo_selection_chain_immutable"),
    ):
        material_trigger = connection.execute(
            text(
                "SELECT trigger.tgenabled,function_owner.rolname "
                "FROM pg_trigger trigger JOIN pg_proc function "
                "ON function.oid=trigger.tgfoid JOIN pg_roles function_owner "
                "ON function_owner.oid=function.proowner "
                "WHERE trigger.tgrelid=to_regclass(:table) "
                "AND trigger.tgname=:trigger AND NOT trigger.tgisinternal"
            ),
            {
                "table": f"{schema_name}.{table_name}",
                "trigger": trigger_name,
            },
        ).first()
        if (
            material_trigger is None
            or material_trigger[0] != "O"
            or material_trigger[1] != "freqtrade_ai_attestor"
        ):
            problems.append(
                "continuous Demo active material trigger mismatch: " + table_name
            )
    index_definition = connection.execute(
        text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname=:schema "
            "AND tablename='strategy_deployments' "
            "AND indexname='strategy_deployments_active_slot_idx'"
        ),
        {"schema": schema_name},
    ).scalar_one_or_none()
    normalized_index = " ".join((index_definition or "").lower().split())
    if (
        "unique index" not in normalized_index
        or "execution_target_id, active_slot" not in normalized_index
        or "status" not in normalized_index
        or "active" not in normalized_index
    ):
        problems.append("continuous Demo active-slot index mismatch")
    return problems


def _accepted_not_found_boundary_problems(
    connection: Connection,
    schema_name: str,
) -> list[str]:
    """Return drift in the owner-only accepted-NOT_FOUND boundary."""

    table = f"{schema_name}.okx_demo_accepted_not_found_terminalizations"
    if not connection.execute(
        text("SELECT to_regclass(:table) IS NOT NULL"), {"table": table}
    ).scalar_one():
        return ["accepted NOT_FOUND terminalization table missing"]
    problems: list[str] = []
    owner, runtime_dml, public_dml = connection.execute(text(
        "SELECT owner.rolname,"
        "has_table_privilege('freqtrade',:table,'SELECT,INSERT,UPDATE,DELETE'),"
        "EXISTS(SELECT 1 FROM pg_class c CROSS JOIN LATERAL "
        "aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) acl "
        "WHERE c.oid=to_regclass(:table) AND acl.grantee=0 AND "
        "acl.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE')) "
        "FROM pg_class relation JOIN pg_roles owner ON owner.oid=relation.relowner "
        "WHERE relation.oid=to_regclass(:table)"
    ), {"table": table}).one()
    if owner != "freqtrade_ai_attestor" or runtime_dml or public_dml:
        problems.append("accepted NOT_FOUND terminalization table ACL mismatch")
    expected_functions = {
        "terminalize_accepted_not_found_no_fill(jsonb)": (
            False,
            ("absolute_submission_claim", "attempt.last_attempt_at", "request_digest"),
        ),
        "exact_accepted_not_found_predecessor(text)": (
            False,
            ("USER_ACCEPTED_NOT_FOUND_NO_FILL", "accepted_terminalization_id"),
        ),
        "exact_bounded_accepted_not_found_predecessor(text)": (
            False,
            ("receipt.receipt_depth=2", "parent.receipt_depth=1"),
        ),
        "terminalize_second_accepted_not_found_no_fill(jsonb)": (
            False,
            ("receipt_depth',2", "absolute_submission_claim',false", "<>2"),
        ),
        "terminalize_final_accepted_not_found_no_fill(jsonb)": (
            False,
            (
                "USER_ACCEPTED_NOT_FOUND_NO_FILL_FINAL_V1",
                "receipt_depth',3",
                "successor_allowed',false",
                "<>3",
            ),
        ),
        "okx_demo_canary_consent_eligibility()": (
            True,
            ("ACCEPTED_SUCCESSOR", "ORDER BY receipt_depth DESC", "BLOCKED"),
        ),
    }
    for signature, (runtime_execute, fragments) in expected_functions.items():
        row = connection.execute(text(
            "SELECT owner.rolname,p.prosecdef,p.proconfig,"
            "has_function_privilege('freqtrade',p.oid,'EXECUTE'),"
            "EXISTS(SELECT 1 FROM aclexplode(p.proacl) acl WHERE acl.grantee=0 "
            "AND acl.privilege_type='EXECUTE'),p.prosrc FROM pg_proc p "
            "JOIN pg_roles owner ON owner.oid=p.proowner "
            "WHERE p.oid=to_regprocedure(:signature)"
        ), {"signature": f"{schema_name}.{signature}"}).first()
        if (
            row is None
            or row[0] != "freqtrade_ai_attestor"
            or row[1] is not True
            or "search_path=pg_catalog" not in (row[2] or [])
            or row[3] is not runtime_execute
            or row[4] is True
            or any(fragment not in row[5] for fragment in fragments)
        ):
            problems.append("accepted NOT_FOUND function boundary mismatch: " + signature)
    for table_name, trigger_name, function_name, fragments in (
        (
            "okx_demo_accepted_not_found_terminalizations",
            "okx_demo_accepted_not_found_immutable",
            "guard_accepted_not_found_terminalization",
            ("receipts are append-only", "RAISE EXCEPTION"),
        ),
        (
            "okx_order_write_attempts",
            "okx_order_write_attempts_accepted_not_found_guard",
            "guard_accepted_not_found_attempt_transition",
            ("accepted NOT_FOUND attempt is immutable", "receipt.request_digest"),
        ),
        (
            "okx_demo_canary_consent_handoffs",
            "okx_demo_canary_bounded_accepted_successor_guard",
            "guard_bounded_accepted_successor_handoff",
            ("receipt-bound successor is required", "invalid bounded accepted successor"),
        ),
    ):
        row = connection.execute(text(
            "SELECT p.proname,t.tgenabled,pg_get_triggerdef(t.oid),owner.rolname,"
            "p.prosecdef,p.proconfig,p.prosrc FROM pg_trigger t "
            "JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_roles owner ON owner.oid=p.proowner "
            "WHERE t.tgrelid=to_regclass(:table) "
            "AND t.tgname=:trigger AND NOT t.tgisinternal"
        ), {
            "table": f"{schema_name}.{table_name}", "trigger": trigger_name,
        }).first()
        if (
            row is None or row[0] != function_name or row[1] != "O"
            or "FOR EACH ROW EXECUTE FUNCTION" not in row[2]
            or row[3] != "freqtrade_ai_attestor" or row[4] is not True
            or "search_path=pg_catalog" not in (row[5] or [])
            or any(fragment not in row[6] for fragment in fragments)
        ):
            problems.append("accepted NOT_FOUND trigger boundary mismatch: " + trigger_name)
    index_row = connection.execute(text(
        "SELECT pg_get_expr(i.indpred,i.indrelid),"
        "(SELECT array_agg(a.attname ORDER BY key.ordinality) FROM "
        "unnest(i.indkey) WITH ORDINALITY key(attnum,ordinality) JOIN pg_attribute a "
        "ON a.attrelid=i.indrelid AND a.attnum=key.attnum) "
        "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=:schema AND c.relname="
        "'okx_demo_canary_one_accepted_successor_idx' AND i.indisunique "
        "AND i.indrelid=to_regclass(:table)"
    ), {
        "schema": schema_name,
        "table": f"{schema_name}.okx_demo_canary_consent_handoffs",
    }).first()
    if (
        index_row is None
        or "terminal_receipt_id IS NOT NULL" not in index_row[0]
        or list(index_row[1] or []) != ["execution_target_id", "terminal_receipt_id"]
    ):
        problems.append("accepted NOT_FOUND successor index boundary mismatch")
    return problems


def _canary_consent_acl_problems(connection: Connection, schema_name: str) -> list[str]:
    table = "{}.okx_demo_canary_consent_handoffs".format(schema_name)
    if not connection.execute(
        text("SELECT to_regclass(:table) IS NOT NULL"), {"table": table}
    ).scalar_one():
        return ["controlled canary consent handoff table missing"]
    owner, runtime_select, runtime_insert, runtime_update, runtime_delete, public_dml = connection.execute(text(
        "SELECT owner.rolname,"
        "has_table_privilege('freqtrade',:table,'SELECT'),"
        "has_table_privilege('freqtrade',:table,'INSERT'),"
        "has_table_privilege('freqtrade',:table,'UPDATE'),"
        "has_table_privilege('freqtrade',:table,'DELETE'),"
        "EXISTS(SELECT 1 FROM aclexplode(COALESCE(relation.relacl,"
        "acldefault('r',relation.relowner))) acl WHERE acl.grantee=0 "
        "AND acl.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')) "
        "FROM pg_class relation JOIN pg_roles owner ON owner.oid=relation.relowner "
        "WHERE relation.oid=to_regclass(:table)"
    ), {"table": table}).one()
    problems = []
    if (
        owner != "freqtrade_ai_attestor" or runtime_select or runtime_insert
        or runtime_update or runtime_delete or public_dml
    ):
        problems.append("controlled canary consent handoff ACL mismatch")
    secret_table = "{}.okx_demo_operator_consent_secrets".format(schema_name)
    consent_key_table_exists = connection.execute(
        text("SELECT to_regclass(:table) IS NOT NULL"),
        {"table": secret_table},
    ).scalar_one()
    if not consent_key_table_exists:
        problems.append("controlled canary consent secret table missing")
    else:
        (
            secret_owner,
            secret_runtime_select,
            secret_runtime_insert,
            secret_runtime_update,
            runtime_delete_on_consent_key,
        ) = connection.execute(text(
            "SELECT owner.rolname,"
            "has_table_privilege('freqtrade',:table,'SELECT'),"
            "has_table_privilege('freqtrade',:table,'INSERT'),"
            "has_table_privilege('freqtrade',:table,'UPDATE'),"
            "has_table_privilege('freqtrade',:table,'DELETE') "
            "FROM pg_class relation JOIN pg_roles owner ON owner.oid=relation.relowner "
            "WHERE relation.oid=to_regclass(:table)"
        ), {"table": secret_table}).one()
        if (
            secret_owner != "freqtrade_ai_attestor" or secret_runtime_select
            or secret_runtime_insert or secret_runtime_update
            or runtime_delete_on_consent_key
        ):
            problems.append("controlled canary consent secret ACL mismatch")
    for signature in {
        "request_okx_demo_canary_consent(text,text,text,text)",
        "pending_okx_demo_canary_consent()",
        "fail_requested_okx_demo_canary_consent(text,text,text)",
        "claim_okx_demo_canary_consent(text,text,bigint,jsonb)",
        "finalize_okx_demo_canary_consent(text,text,bigint,bigint,bigint,bigint,jsonb)",
        "finalized_okx_demo_canary_consent(text)",
        "issue_okx_demo_submission_grant(jsonb)",
        "revoke_restarted_okx_demo_canary_grant(text,text)",
        "fail_okx_demo_canary_grant_before_prepare(text)",
        "settle_okx_demo_canary_handoff(text)",
        "eligible_atomic_okx_demo_canary_predecessor()",
        "request_atomic_okx_demo_canary_consent(text,text,text,text)",
        "finalize_atomic_okx_demo_canary_consent(text,text,bigint,bigint,bigint,bigint,jsonb)",
        "issue_atomic_okx_demo_submission_grant(jsonb)",
        "prepare_atomic_okx_demo_canary_dispatch(jsonb)",
        "commit_atomic_okx_demo_canary_prepare(text,text,bigint,bigint,bigint,bigint,jsonb,jsonb,jsonb)",
        "claim_atomic_okx_demo_canary_dispatch(bigint,text,text,bigint,text)",
    }:
        row = connection.execute(text(
            "SELECT owner.rolname,p.prosecdef,p.proconfig,"
            "has_function_privilege('freqtrade',p.oid,'EXECUTE'),"
            "has_function_privilege('public',p.oid,'EXECUTE') "
            "FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner "
            "WHERE p.oid=to_regprocedure(:signature)"
        ), {"signature": "{}.{}".format(schema_name, signature)}).first()
        runtime_execute_expected = not signature.startswith((
            "finalize_atomic_", "issue_atomic_", "prepare_atomic_"
        ))
        if (
            row is None or row[0] != "freqtrade_ai_attestor" or row[1] is not True
            or "search_path=pg_catalog" not in (row[2] or [])
            or row[3] is not runtime_execute_expected or row[4] is True
        ):
            problems.append("controlled canary consent function mismatch: " + signature)
    return problems


def _canary_lifecycle_acl_problems(connection: Connection, schema_name: str) -> list[str]:
    if "okx_demo_canary_lifecycles" not in inspect(connection).get_table_names(schema=schema_name):
        return ["controlled canary lifecycle table missing"]
    problems: list[str] = []
    for signature, fragments in {
        "lock_okx_demo_reconciliation_state()": (
            "FOR UPDATE",
            "controlled recovery state lock is missing",
        ),
        "create_okx_demo_canary_lifecycle(character varying)": (
            "CONTROLLED_CANARY_NON_PRODUCTION",
            "baseline_position_quantity",
            "lifecycle_id IS NULL AND status='ACTIVE'",
        ),
        "create_okx_demo_canary_cleanup_intent(character varying,bigint,bigint)": ("canary cleanup grant binding rejected", "cleanup_trade_intent_id"),
        "bridge_okx_demo_managed_fill(bigint)": (
            "managed fill evidence rejected",
            "authenticated IS NOT TRUE",
            "managed fill lineage conflict",
        ),
        "prepare_okx_demo_canary_residual_child(bigint,bigint)": (
            "residual canary child context rejected",
            "lock_okx_demo_reconciliation_state",
            "RESIDUAL_CLOSE_REQUIRED",
            "SUPERSEDED_BY_CLOSE_CLEANUP",
        ),
        "can_resume_okx_demo_canary_recovery(bigint)": (
            "require_current_okx_demo_canary_recovery_run",
            "lifecycle_id IS DISTINCT FROM lifecycle",
        ),
        "transition_okx_demo_canary_lifecycle(character varying,text,bigint,bigint,character varying,bigint)": ("BIND_OPENING", "RECORD_FILLS", "EXHAUST_RECOVERY", "TERMINALIZE"),
        "issue_okx_demo_canary_recovery_grant(character varying,bigint,text,bigint)": ("canary recovery grant context rejected", "lifecycle_id"),
    }.items():
        row = connection.execute(text(
            "SELECT owner.rolname,p.prosecdef,p.proconfig,has_function_privilege('freqtrade',p.oid,'EXECUTE'), "
            "EXISTS(SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee=0 AND a.privilege_type='EXECUTE'),p.prosrc "
            "FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner "
            "WHERE p.oid=to_regprocedure(:signature)"
        ), {"signature": f"{schema_name}.{signature}"}).first()
        if row is None or row[0] != "freqtrade_ai_attestor" or row[1] is not True or "search_path=pg_catalog" not in (row[2] or []) or row[3] is not True or row[4] is True or any(fragment not in row[5] for fragment in fragments):
            problems.append("controlled canary lifecycle function boundary mismatch: " + signature)
    for trigger_name, function_name, security_definer, trigger_event, fragments in (
        (
            "okx_demo_canary_recovery_insert_guard",
            "guard_okx_demo_canary_recovery_insert",
            False,
            "BEFORE INSERT ON",
            (
                "NEW.lifecycle_id IS NOT NULL",
                "current_user IS DISTINCT FROM 'freqtrade_ai_attestor'",
                "lock_okx_demo_reconciliation_state",
                "generic recovery grant conflicts with controlled canary lifecycle",
            ),
        ),
        (
            "okx_demo_recovery_lifecycle_identity_guard",
            "guard_okx_demo_recovery_lifecycle_identity",
            True,
            "BEFORE UPDATE ON",
            ("OLD.lifecycle_id IS DISTINCT FROM NEW.lifecycle_id",),
        ),
    ):
        row = connection.execute(text(
            "SELECT p.proname,owner.rolname,p.prosecdef,p.proconfig,t.tgenabled,p.prosrc,pg_get_triggerdef(t.oid) "
            "FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid "
            "JOIN pg_roles owner ON owner.oid=p.proowner "
            "WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger AND NOT t.tgisinternal"
        ), {
            "table": f"{schema_name}.okx_demo_recovery_grants",
            "trigger": trigger_name,
        }).first()
        if (
            row is None
            or row[0] != function_name
            or row[1] != "freqtrade_ai_attestor"
            or row[2] is not security_definer
            or "search_path=pg_catalog" not in (row[3] or [])
            or row[4] != "O"
            or any(fragment not in row[5] for fragment in fragments)
            or trigger_event not in row[6]
            or "FOR EACH ROW EXECUTE FUNCTION" not in row[6]
        ):
            problems.append("controlled canary lifecycle trigger boundary mismatch: " + trigger_name)
    lifecycle_dml, recovery_insert = connection.execute(text(
        "SELECT has_table_privilege('freqtrade',:lifecycle,'INSERT,UPDATE,DELETE'),has_table_privilege('freqtrade',:recovery,'INSERT')"
    ), {"lifecycle": f"{schema_name}.okx_demo_canary_lifecycles", "recovery": f"{schema_name}.okx_demo_recovery_grants"}).one()
    if lifecycle_dml or not recovery_insert:
        problems.append("controlled canary lifecycle runtime ACL mismatch")
    allowed_selects = {
        "lifecycle_id", "execution_target_id", "opening_trade_intent_id",
        "cleanup_trade_intent_id",
        "cleanup_phase", "outcome", "deadline_at", "fencing_version",
        "opening_exchange_order_row_id", "cleanup_exchange_order_row_id",
        "baseline_evidence_digest", "attributed_fill_quantity", "max_quantity",
        "fill_attribution_digest", "failure_code", "final_evidence_digest",
        "terminal_at", "revoked_at",
    }
    for column in inspect(connection).get_columns(
        "okx_demo_canary_lifecycles", schema=schema_name
    ):
        can_select = connection.execute(
            text(
                "SELECT has_column_privilege('freqtrade',:table,:column,'SELECT')"
            ),
            {
                "table": f"{schema_name}.okx_demo_canary_lifecycles",
                "column": column["name"],
            },
        ).scalar_one()
        if bool(can_select) != (column["name"] in allowed_selects):
            problems.append(
                "controlled canary lifecycle column SELECT ACL mismatch: "
                + column["name"]
            )
    return problems


def _expired_approval_attestor_acl_problems(
    connection: Connection,
    schema_name: str,
) -> list[str]:
    """Verify the NOLOGIN owner and its narrow SECURITY DEFINER entrypoint.

    PostgreSQL owners always have implicit full privileges, so lineage tables
    rely on the NOLOGIN attestor boundary.  Ordinary application tables retain
    their owner and grant the attestor only the columns used by this function.
    """

    owner_tables = {
        "approved_executions",
        "trade_intents",
        "risk_decisions",
    }
    delegated_columns = {
        "full_chain_runs": {
            "SELECT": {
                "id",
                "research_job_id",
                "research_job_attempt_id",
                "run_kind",
                "signal_evaluation_id",
                "research_scope_id",
                "execution_target_id",
                "status",
                "current_stage",
                "strategy_generation_run_id",
                "strategy_id",
                "strategy_version_id",
                "backtest_run_id",
                "backtest_task_id",
                "backtest_result_id",
                "strategy_score_id",
                "candidate_approval_id",
                "signal_snapshot_id",
                "trade_intent_id",
                "risk_decision_id",
                "approved_execution_id",
                "exchange_order_id",
            },
            "UPDATE": {"status", "terminal_reason", "completed_at"},
        },
        "risk_budgets": {
            "SELECT": {"execution_target_id", "reserved_notional", "approved_positions"},
            "UPDATE": {"reserved_notional", "approved_positions"},
        },
    }
    required_tables = owner_tables | set(delegated_columns)
    if not required_tables.issubset(
        set(inspect(connection).get_table_names(schema=schema_name))
    ):
        return []
    problems = []
    for table_name in sorted(owner_tables | set(delegated_columns)):
        qualified = "{}.{}".format(schema_name, table_name)
        owner, public_unsafe, runtime_unsafe, attestor_table_acl = connection.execute(
            text(
                "SELECT owner.rolname, "
                "EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                "relation.relacl, acldefault('r', relation.relowner))) AS acl "
                "WHERE acl.grantee = 0 AND acl.privilege_type IN "
                "('DELETE','TRUNCATE','REFERENCES','TRIGGER')), "
                "EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                "relation.relacl, acldefault('r', relation.relowner))) AS acl "
                "WHERE acl.grantee = (SELECT oid FROM pg_roles "
                "WHERE rolname = 'freqtrade') AND acl.privilege_type IN "
                "('DELETE','TRUNCATE','REFERENCES','TRIGGER')), "
                "EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                "relation.relacl, acldefault('r', relation.relowner))) AS acl "
                "WHERE acl.grantee = (SELECT oid FROM pg_roles "
                "WHERE rolname = 'freqtrade_ai_attestor') "
                "AND acl.privilege_type IN "
                "('SELECT','UPDATE','INSERT','DELETE','TRUNCATE',"
                "'REFERENCES','TRIGGER')) "
                "FROM pg_class AS relation JOIN pg_roles AS owner "
                "ON owner.oid = relation.relowner "
                "WHERE relation.oid = to_regclass(:table)"
            ),
            {"table": qualified},
        ).one()
        if table_name in owner_tables and owner != "freqtrade_ai_attestor":
            problems.append("expired approval table owner mismatch: " + table_name)
        if public_unsafe or runtime_unsafe:
            problems.append("expired approval unsafe table ACL: " + table_name)
        if table_name in delegated_columns:
            if owner == "freqtrade_ai_attestor" or attestor_table_acl:
                problems.append(
                    "expired approval delegated table ACL is too broad: " + table_name
                )
            for column in inspect(connection).get_columns(
                table_name, schema=schema_name
            ):
                for privilege in ("SELECT", "UPDATE"):
                    actual = connection.execute(
                        text(
                            "SELECT has_column_privilege("
                            "'freqtrade_ai_attestor', :table, :column, :privilege)"
                        ),
                        {
                            "table": qualified,
                            "column": column["name"],
                            "privilege": privilege,
                        },
                    ).scalar_one()
                    expected = column["name"] in delegated_columns[table_name][privilege]
                    if bool(actual) != expected:
                        problems.append(
                            "expired approval delegated column ACL mismatch: "
                            "{}.{} {}".format(table_name, column["name"], privilege)
                        )
    boundary = connection.execute(
        text(
            "SELECT owner.rolname, function.prosecdef, function.proconfig, "
            "has_function_privilege('freqtrade', function.oid, 'EXECUTE'), "
            "EXISTS (SELECT 1 FROM aclexplode(COALESCE(function.proacl, "
            "acldefault('f', function.proowner))) AS acl "
            "WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE') "
            "FROM pg_proc AS function "
            "JOIN pg_namespace AS namespace ON namespace.oid = function.pronamespace "
            "JOIN pg_roles AS owner ON owner.oid = function.proowner "
            "WHERE namespace.nspname = :schema_name "
            "AND function.proname = 'release_expired_okx_demo_approval'"
        ),
        {"schema_name": schema_name},
    ).first()
    if (
        boundary is None
        or boundary[0] != "freqtrade_ai_attestor"
        or boundary[1] is not True
        or "search_path={}, pg_catalog".format(schema_name)
        not in [value.replace('"', "") for value in (boundary[2] or [])]
        or boundary[3] is not True
        or boundary[4] is True
    ):
        problems.append("expired approval function boundary mismatch")
    return problems


def _strategy_validation_acl_problems(
    connection: Connection,
    schema_name: str,
) -> list[str]:
    """Verify immutable validation identity and narrow mutable-column updates."""

    problems: list[str] = []
    mutable_columns = {
        "strategy_validation_plans": {
            "status",
            "promotion_evidence",
            "evidence_digest",
            "blocked_reason",
            "completed_at",
        },
        "strategy_validation_windows": {
            "expected_config_digest",
            "backtest_run_id",
            "backtest_task_id",
            "backtest_result_id",
            "execution_id",
            "artifact_manifest_checksum",
            "result_checksum",
            "market_state",
            "market_state_source",
            "market_state_algorithm",
            "market_state_parameters",
            "market_state_evidence",
            "market_state_evidence_digest",
            "status",
            "blocked_reason",
        },
    }
    inspector = inspect(connection)
    tables = set(inspector.get_table_names(schema=schema_name))
    for table_name, allowed_updates in mutable_columns.items():
        if table_name not in tables:
            problems.append("strategy validation ACL table missing: " + table_name)
            continue
        qualified = f"{schema_name}.{table_name}"
        can_select, can_insert, can_table_update, can_delete = connection.execute(
            text(
                "SELECT "
                "has_table_privilege('freqtrade', :table, 'SELECT'), "
                "has_table_privilege('freqtrade', :table, 'INSERT'), "
                "has_table_privilege('freqtrade', :table, 'UPDATE'), "
                "has_table_privilege('freqtrade', :table, 'DELETE')"
            ),
            {"table": qualified},
        ).one()
        if not (can_select and can_insert) or can_table_update or can_delete:
            problems.append("strategy validation table ACL mismatch: " + table_name)
        for column in inspector.get_columns(table_name, schema=schema_name):
            can_update = connection.execute(
                text(
                    "SELECT has_column_privilege("
                    "'freqtrade', :table, :column, 'UPDATE')"
                ),
                {"table": qualified, "column": column["name"]},
            ).scalar_one()
            if bool(can_update) != (column["name"] in allowed_updates):
                problems.append(
                    "strategy validation column ACL mismatch: "
                    f"{table_name}.{column['name']}"
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
    _add_single_active_strategy_deployment_index(connection)


def _add_single_active_strategy_deployment_index(connection: Connection) -> None:
    """Fail closed before enforcing one ACTIVE deployment for OKX_DEMO."""

    table_names = set(
        inspect(connection).get_table_names(
            schema=connection.execute(text("SELECT current_schema()")).scalar_one()
        )
    )
    if "strategy_deployments" not in table_names:
        return
    active_count = int(
        connection.execute(
            text(
                "SELECT count(*) FROM strategy_deployments "
                "WHERE execution_target_id = 'OKX_DEMO' AND status = 'ACTIVE'"
            )
        ).scalar_one()
    )
    if active_count > 1:
        raise SchemaMigrationBlocked(
            "Multiple ACTIVE OKX_DEMO strategy deployments require an explicit "
            "data-preserving resolution before migration"
        )
    # Keep the historical v22 boundary independent from current ORM metadata.
    # Fresh databases still traverse v22 before the v35 three-slot migration.
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "strategy_deployments_single_active_idx "
            "ON strategy_deployments(execution_target_id) "
            "WHERE status='ACTIVE'"
        )
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


def _add_deferred_canary_terminalization_foreign_keys(
    connection: Connection,
) -> None:
    """Restore lifecycle FKs deferred by the terminalization receipt cycle."""

    schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
    table_name = "okx_demo_canary_lifecycles"
    actual_fks = {
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key.get("referred_schema") or schema_name,
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
            ((foreign_key.get("options") or {}).get("ondelete") or "NO ACTION").upper(),
            ((foreign_key.get("options") or {}).get("onupdate") or "NO ACTION").upper(),
            bool((foreign_key.get("options") or {}).get("deferrable", False)),
            ((foreign_key.get("options") or {}).get("initially") or "").upper()
            or None,
        )
        for foreign_key in inspect(connection).get_foreign_keys(
            table_name,
            schema=schema_name,
        )
    }
    for constraint in Base.metadata.tables[table_name].constraints:
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


def _add_strategy_validation_matrix(connection: Connection) -> None:
    """Install immutable OOS/walk-forward plans without touching execution tables."""

    for table_name in ("strategy_validation_plans", "strategy_validation_windows"):
        Base.metadata.tables[table_name].create(bind=connection, checkfirst=True)
    schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(schema_name)
    quote = connection.dialect.identifier_preparer.quote
    mutable_columns = {
        "strategy_validation_plans": (
            "status",
            "promotion_evidence",
            "evidence_digest",
            "blocked_reason",
            "completed_at",
        ),
        "strategy_validation_windows": (
            "expected_config_digest",
            "backtest_run_id",
            "backtest_task_id",
            "backtest_result_id",
            "execution_id",
            "artifact_manifest_checksum",
            "result_checksum",
            "market_state",
            "market_state_source",
            "market_state_algorithm",
            "market_state_parameters",
            "market_state_evidence",
            "market_state_evidence_digest",
            "status",
            "blocked_reason",
        ),
    }
    for table_name, update_columns in mutable_columns.items():
        quoted_table = connection.dialect.identifier_preparer.quote(table_name)
        quoted_updates = ", ".join(quote(name) for name in update_columns)
        connection.execute(
            text(
                f"REVOKE ALL ON TABLE {quoted_schema}.{quoted_table} "
                f"FROM PUBLIC, freqtrade; "
                f"GRANT SELECT, INSERT ON TABLE "
                f"{quoted_schema}.{quoted_table} TO freqtrade; "
                f"GRANT UPDATE ({quoted_updates}) ON TABLE "
                f"{quoted_schema}.{quoted_table} TO freqtrade"
            )
        )
        sequence_name = f"{table_name}_id_seq"
        quoted_sequence = connection.dialect.identifier_preparer.quote(sequence_name)
        connection.execute(
            text(
                f"REVOKE ALL ON SEQUENCE {quoted_schema}.{quoted_sequence} "
                f"FROM PUBLIC, freqtrade; "
                f"GRANT USAGE, SELECT ON SEQUENCE "
                f"{quoted_schema}.{quoted_sequence} TO freqtrade"
            )
        )
    connection.execute(
        text(
            f"""
            CREATE OR REPLACE FUNCTION {quoted_schema}.guard_strategy_validation_plan()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'strategy validation plans are immutable';
                END IF;
                IF OLD.strategy_version_id IS DISTINCT FROM NEW.strategy_version_id
                   OR OLD.promotion_backtest_result_id IS DISTINCT FROM NEW.promotion_backtest_result_id
                   OR OLD.provider_name IS DISTINCT FROM NEW.provider_name
                   OR OLD.strategy_code_digest IS DISTINCT FROM NEW.strategy_code_digest
                   OR OLD.plan_digest IS DISTINCT FROM NEW.plan_digest
                   OR OLD.plan_snapshot::jsonb IS DISTINCT FROM NEW.plan_snapshot::jsonb
                   OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                    RAISE EXCEPTION 'strategy validation plan identity is immutable';
                END IF;
                RETURN NEW;
            END
            $$;
            DROP TRIGGER IF EXISTS strategy_validation_plans_immutable
                ON {quoted_schema}.strategy_validation_plans;
            CREATE TRIGGER strategy_validation_plans_immutable
                BEFORE UPDATE OR DELETE ON {quoted_schema}.strategy_validation_plans
                FOR EACH ROW EXECUTE FUNCTION
                {quoted_schema}.guard_strategy_validation_plan();

            CREATE OR REPLACE FUNCTION {quoted_schema}.guard_strategy_validation_window()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'strategy validation windows are immutable';
                END IF;
                IF OLD.validation_plan_id IS DISTINCT FROM NEW.validation_plan_id
                   OR OLD.ordinal IS DISTINCT FROM NEW.ordinal
                   OR OLD.window_kind IS DISTINCT FROM NEW.window_kind
                   OR OLD.required_market_state IS DISTINCT FROM NEW.required_market_state
                   OR OLD.timerange IS DISTINCT FROM NEW.timerange
                   OR OLD.profile_snapshot::jsonb IS DISTINCT FROM NEW.profile_snapshot::jsonb
                   OR OLD.expected_market_data_digest IS DISTINCT FROM NEW.expected_market_data_digest
                   OR OLD.created_at IS DISTINCT FROM NEW.created_at
                   OR (OLD.backtest_run_id IS NOT NULL
                       AND OLD.backtest_run_id IS DISTINCT FROM NEW.backtest_run_id)
                   OR (OLD.backtest_task_id IS NOT NULL
                       AND OLD.backtest_task_id IS DISTINCT FROM NEW.backtest_task_id)
                   OR (OLD.backtest_result_id IS NOT NULL
                       AND OLD.backtest_result_id IS DISTINCT FROM NEW.backtest_result_id)
                   OR (OLD.execution_id IS NOT NULL
                       AND OLD.execution_id IS DISTINCT FROM NEW.execution_id) THEN
                    RAISE EXCEPTION 'strategy validation window identity is immutable';
                END IF;
                RETURN NEW;
            END
            $$;
            DROP TRIGGER IF EXISTS strategy_validation_windows_immutable
                ON {quoted_schema}.strategy_validation_windows;
            CREATE TRIGGER strategy_validation_windows_immutable
                BEFORE UPDATE OR DELETE ON {quoted_schema}.strategy_validation_windows
                FOR EACH ROW EXECUTE FUNCTION
                {quoted_schema}.guard_strategy_validation_window();
            """
        )
    )


def _add_controlled_canary_lifecycle_boundary(connection: Connection) -> None:
    """Install v27 durable canary identity with function-only runtime writes."""

    Base.metadata.tables["okx_demo_canary_lifecycles"].create(
        bind=connection, checkfirst=True
    )
    columns = {
        column["name"]
        for column in inspect(connection).get_columns("okx_demo_recovery_grants")
    }
    if "lifecycle_id" not in columns:
        connection.execute(text("ALTER TABLE okx_demo_recovery_grants ADD COLUMN lifecycle_id VARCHAR(32) REFERENCES okx_demo_canary_lifecycles(lifecycle_id) ON DELETE RESTRICT"))
    connection.execute(text("""
        ALTER TABLE okx_demo_canary_lifecycles
          DROP CONSTRAINT IF EXISTS okx_demo_canary_lifecycle_phase_check;
        ALTER TABLE okx_demo_canary_lifecycles
          ADD CONSTRAINT okx_demo_canary_lifecycle_phase_check
          CHECK (cleanup_phase IN ('ARMED','OPENING_SUBMITTED','CANCEL_PENDING',
            'CLEANUP_PENDING','RECOVERY_EXHAUSTED','TERMINAL','REVOKED'));
    """))
    connection.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS
          okx_demo_recovery_grants_one_active_lifecycle_action_idx
        ON okx_demo_recovery_grants (lifecycle_id, action)
        WHERE lifecycle_id IS NOT NULL AND status = 'ACTIVE'
    """))
    schema_name = connection.execute(text("SELECT current_schema() ")).scalar_one()
    quoted_schema = '"{}"'.format(schema_name.replace('"', '""'))
    connection.execute(text("""
        CREATE OR REPLACE FUNCTION lock_okx_demo_reconciliation_state()
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE state_id bigint;
        BEGIN
          SELECT database_id INTO state_id
            FROM SCHEMA_TOKEN.okx_demo_reconciliation_states
            WHERE execution_target_id='OKX_DEMO' FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'controlled recovery state lock is missing';
          END IF;
          RETURN state_id;
        END $$;
        ALTER FUNCTION lock_okx_demo_reconciliation_state() OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION lock_okx_demo_reconciliation_state() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION lock_okx_demo_reconciliation_state() TO freqtrade;
        CREATE OR REPLACE FUNCTION create_okx_demo_canary_lifecycle(
            p_grant_id varchar)
        RETURNS varchar LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog AS $$
        DECLARE g SCHEMA_TOKEN.okx_demo_submission_grants%ROWTYPE;
                a SCHEMA_TOKEN.approved_executions%ROWTYPE;
                i SCHEMA_TOKEN.trade_intents%ROWTYPE;
                r SCHEMA_TOKEN.reconciliation_runs%ROWTYPE;
                s SCHEMA_TOKEN.okx_demo_reconciliation_states%ROWTYPE;
                expected_digest text; computed_deadline timestamptz;
        BEGIN
          SELECT * INTO g FROM SCHEMA_TOKEN.okx_demo_submission_grants WHERE grant_id=p_grant_id FOR UPDATE;
          IF NOT FOUND THEN RAISE EXCEPTION 'controlled canary grant missing'; END IF;
          SELECT * INTO a FROM SCHEMA_TOKEN.approved_executions WHERE id=g.approval_id;
          IF NOT FOUND THEN RAISE EXCEPTION 'controlled canary approval missing'; END IF;
          SELECT * INTO i FROM SCHEMA_TOKEN.trade_intents WHERE id=a.trade_intent_id;
          IF NOT FOUND THEN RAISE EXCEPTION 'controlled canary intent missing'; END IF;
          SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs WHERE id=g.reconciliation_run_id;
          IF NOT FOUND THEN RAISE EXCEPTION 'controlled canary baseline missing'; END IF;
          SELECT * INTO s FROM SCHEMA_TOKEN.okx_demo_reconciliation_states
            WHERE execution_target_id='OKX_DEMO' FOR UPDATE;
          IF NOT FOUND THEN RAISE EXCEPTION 'controlled canary reconciliation state missing'; END IF;
          expected_digest := encode(public.digest(convert_to(concat_ws('|',r.id::text,r.artifact_sha256,r.authoritative_observed_at::text,r.completed_at::text),'UTF8'),'sha256'),'hex');
          computed_deadline := LEAST(statement_timestamp()+interval '30 seconds',g.expires_at,a.expires_at,i.expires_at);
          IF g.status IS DISTINCT FROM 'ACTIVE' OR g.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
             OR g.provenance IS DISTINCT FROM 'CONTROLLED_CANARY_NON_PRODUCTION'
             OR g.approval_id IS DISTINCT FROM a.id OR g.client_order_id IS DISTINCT FROM a.client_order_id
             OR g.client_order_id IS DISTINCT FROM i.client_order_id OR g.instrument_id IS DISTINCT FROM i.instrument_id
             OR g.canary_quantity IS DISTINCT FROM i.quantity OR a.trade_intent_id IS DISTINCT FROM i.id
             OR a.execution_target_id IS DISTINCT FROM 'OKX_DEMO' OR a.status IS DISTINCT FROM 'ACTIVE'
             OR a.order_submission_authorized IS DISTINCT FROM FALSE OR a.claim_required IS NOT TRUE
             OR i.execution_target_id IS DISTINCT FROM 'OKX_DEMO' OR i.status IS DISTINCT FROM 'APPROVED'
             OR i.reduce_only IS NOT FALSE OR i.instrument_id IS DISTINCT FROM 'BTC-USDT-SWAP'
             OR i.side IS DISTINCT FROM 'buy' OR i.position_side IS DISTINCT FROM 'long'
             OR i.client_order_id IS NULL OR i.quantity IS NULL OR i.quantity<=0
             OR r.execution_target_id IS DISTINCT FROM 'OKX_DEMO' OR r.status IS DISTINCT FROM 'RECONCILED'
             OR r.artifact_status IS DISTINCT FROM 'READY' OR r.source_type IS DISTINCT FROM 'api_aggregate' OR r.core_data IS NOT TRUE
             OR s.last_reconciliation_run_id IS DISTINCT FROM r.id
             OR s.status IS DISTINCT FROM 'RECONCILED' OR s.opening_frozen IS NOT FALSE
             OR r.completed_at IS NULL OR r.authoritative_observed_at IS NULL
             OR r.completed_at < statement_timestamp()-interval '30 seconds'
             OR r.authoritative_observed_at < statement_timestamp()-interval '30 seconds'
             OR r.completed_at > statement_timestamp()+interval '5 seconds'
             OR r.authoritative_observed_at > statement_timestamp()+interval '5 seconds'
             OR jsonb_typeof(r.database_ids::jsonb) IS DISTINCT FROM 'object'
             OR (r.database_ids::jsonb)->'reconciliation_run' IS DISTINCT FROM jsonb_build_array(r.id)
             OR (r.database_ids::jsonb)->'order_snapshots' IS DISTINCT FROM '[]'::jsonb
             OR (r.database_ids::jsonb)->'position_snapshots' IS DISTINCT FROM '[]'::jsonb
             OR jsonb_typeof((r.database_ids::jsonb)->'recovery_batches') IS DISTINCT FROM 'array'
             OR jsonb_array_length((r.database_ids::jsonb)->'recovery_batches') IS DISTINCT FROM 1
             OR NOT EXISTS (SELECT 1 FROM SCHEMA_TOKEN.okx_demo_recovery_batches b
                 WHERE b.database_id=((r.database_ids::jsonb)->'recovery_batches'->>0)::bigint
                   AND b.execution_target_id='OKX_DEMO' AND b.authenticated AND b.pagination_complete
                   AND b.complete_streams::jsonb IS NOT DISTINCT FROM '["ACCOUNT","FILL","ORDER","POSITION"]'::jsonb
                   AND (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(b.high_watermarks::jsonb) k)
                       IS NOT DISTINCT FROM ARRAY['ACCOUNT','FILL','ORDER','POSITION']::text[]
                   AND b.completed_at>=statement_timestamp()-interval '30 seconds'
                   AND b.observed_at>=statement_timestamp()-interval '30 seconds')
             OR EXISTS (SELECT 1 FROM SCHEMA_TOKEN.okx_demo_recovery_batches b
                 WHERE b.database_id=((r.database_ids::jsonb)->'recovery_batches'->>0)::bigint
                   AND (b.completed_at>statement_timestamp()+interval '5 seconds'
                     OR b.observed_at>statement_timestamp()+interval '5 seconds'))
             OR (r.artifact_sha256~'^[0-9a-f]{64}$') IS NOT TRUE
             OR computed_deadline IS NULL OR computed_deadline<=statement_timestamp()
             OR EXISTS (SELECT 1 FROM SCHEMA_TOKEN.okx_demo_recovery_grants
                 WHERE lifecycle_id IS NULL AND status='ACTIVE')
             OR EXISTS (SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles WHERE cleanup_phase NOT IN ('TERMINAL','REVOKED'))
          THEN RAISE EXCEPTION 'unsafe controlled canary lifecycle baseline'; END IF;
          INSERT INTO SCHEMA_TOKEN.okx_demo_canary_lifecycles(
            lifecycle_id,execution_target_id,submission_grant_id,opening_approval_id,
            opening_trade_intent_id,baseline_reconciliation_run_id,
            baseline_position_quantity,baseline_evidence_digest,
            attributed_fill_quantity,max_quantity,outcome,cleanup_phase,
            deadline_at,fencing_version,created_at,updated_at)
          VALUES(g.grant_id,'OKX_DEMO',g.grant_id,a.id,i.id,r.id,0,
            expected_digest,
            0,g.canary_quantity,'PENDING','ARMED',
            computed_deadline,1,statement_timestamp(),statement_timestamp());
          RETURN g.grant_id;
        END $$;
        ALTER FUNCTION create_okx_demo_canary_lifecycle(varchar) OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION create_okx_demo_canary_lifecycle(varchar) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION create_okx_demo_canary_lifecycle(varchar) TO freqtrade;
        CREATE OR REPLACE FUNCTION require_current_okx_demo_canary_run(p_run_id bigint)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE r SCHEMA_TOKEN.reconciliation_runs%ROWTYPE;
                s SCHEMA_TOKEN.okx_demo_reconciliation_states%ROWTYPE;
        BEGIN
          SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs WHERE id=p_run_id;
          IF NOT FOUND THEN RETURN FALSE; END IF;
          SELECT * INTO s FROM SCHEMA_TOKEN.okx_demo_reconciliation_states WHERE execution_target_id='OKX_DEMO';
          IF NOT FOUND THEN RETURN FALSE; END IF;
          RETURN r.execution_target_id='OKX_DEMO' AND r.status IN('RECONCILED','RECOVERED')
            AND r.artifact_status='READY' AND r.source_type='api_aggregate' AND r.core_data
            AND r.artifact_sha256~'^[0-9a-f]{64}$'
            AND r.completed_at IS NOT NULL AND r.authoritative_observed_at IS NOT NULL
            AND r.completed_at>=statement_timestamp()-interval '30 seconds'
            AND r.authoritative_observed_at>=statement_timestamp()-interval '30 seconds'
            AND r.completed_at<=statement_timestamp()+interval '5 seconds'
            AND r.authoritative_observed_at<=statement_timestamp()+interval '5 seconds'
            AND s.last_reconciliation_run_id=r.id AND s.status IN('RECONCILED','RECOVERED') AND NOT s.opening_frozen
            AND jsonb_typeof(r.database_ids::jsonb)='object'
            AND (r.database_ids::jsonb)->'reconciliation_run' IS NOT DISTINCT FROM jsonb_build_array(r.id)
            AND jsonb_array_length((r.database_ids::jsonb)->'recovery_batches')=1
            AND EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_recovery_batches b
              WHERE b.database_id=((r.database_ids::jsonb)->'recovery_batches'->>0)::bigint
                AND b.execution_target_id='OKX_DEMO' AND b.authenticated AND b.pagination_complete
                AND b.complete_streams::jsonb IS NOT DISTINCT FROM '["ACCOUNT","FILL","ORDER","POSITION"]'::jsonb
                AND (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(b.high_watermarks::jsonb) k)
                    IS NOT DISTINCT FROM ARRAY['ACCOUNT','FILL','ORDER','POSITION']::text[]
                AND b.completed_at>=statement_timestamp()-interval '30 seconds'
                AND b.observed_at>=statement_timestamp()-interval '30 seconds'
                AND b.completed_at<=statement_timestamp()+interval '5 seconds'
                AND b.observed_at<=statement_timestamp()+interval '5 seconds');
        END $$;
        ALTER FUNCTION require_current_okx_demo_canary_run(bigint) OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION require_current_okx_demo_canary_run(bigint) FROM PUBLIC,freqtrade;
        CREATE OR REPLACE FUNCTION require_current_okx_demo_canary_recovery_run(p_run_id bigint,p_lifecycle varchar)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE r SCHEMA_TOKEN.reconciliation_runs%ROWTYPE; s SCHEMA_TOKEN.okx_demo_reconciliation_states%ROWTYPE;
                l SCHEMA_TOKEN.okx_demo_canary_lifecycles%ROWTYPE; oi SCHEMA_TOKEN.trade_intents%ROWTYPE;
                opening_ord text; opening_client text;
        BEGIN
          SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs WHERE id=p_run_id; IF NOT FOUND THEN RETURN FALSE; END IF;
          SELECT * INTO s FROM SCHEMA_TOKEN.okx_demo_reconciliation_states WHERE execution_target_id='OKX_DEMO'; IF NOT FOUND THEN RETURN FALSE; END IF;
          SELECT * INTO l FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles WHERE lifecycle_id=p_lifecycle; IF NOT FOUND THEN RETURN FALSE; END IF;
          SELECT * INTO oi FROM SCHEMA_TOKEN.trade_intents WHERE id=l.opening_trade_intent_id; IF NOT FOUND THEN RETURN FALSE; END IF;
          SELECT exchange_order_id,client_order_id INTO opening_ord,opening_client FROM SCHEMA_TOKEN.exchange_orders WHERE id=l.opening_exchange_order_row_id;
          RETURN r.status='DRIFTED' AND s.status='DRIFTED' AND s.opening_frozen AND s.last_reconciliation_run_id=r.id
            AND r.execution_target_id='OKX_DEMO' AND r.artifact_status='READY' AND r.artifact_sha256~'^[0-9a-f]{64}$' AND r.source_type='api_aggregate' AND r.core_data
            AND r.completed_at>=statement_timestamp()-interval '30 seconds' AND r.authoritative_observed_at>=statement_timestamp()-interval '30 seconds'
            AND r.completed_at<=statement_timestamp()+interval '5 seconds' AND r.authoritative_observed_at<=statement_timestamp()+interval '5 seconds'
            AND jsonb_typeof(r.summary_snapshot::jsonb)='object'
            AND (r.summary_snapshot::jsonb)->>'execution_target'='OKX_DEMO'
            AND (r.summary_snapshot::jsonb)->>'source_type'='api_aggregate'
            AND (r.summary_snapshot::jsonb)->>'status'='DRIFTED'
            AND (r.summary_snapshot::jsonb)->'core_data'='true'::jsonb
            AND (r.summary_snapshot::jsonb)->'opening_frozen'='true'::jsonb
            AND (r.summary_snapshot::jsonb)->'database_ids' IS NOT DISTINCT FROM r.database_ids::jsonb
            AND jsonb_typeof((r.summary_snapshot::jsonb)->'findings')='array'
            AND jsonb_array_length((r.summary_snapshot::jsonb)->'findings')>0
            AND (r.database_ids::jsonb)->'reconciliation_run' IS NOT DISTINCT FROM jsonb_build_array(r.id)
            AND EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_order_snapshots x
                WHERE x.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'order_snapshots')::bigint)
                  AND x.exchange_order_id=opening_ord AND x.client_order_id=opening_client)
            AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_order_snapshots x WHERE x.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'order_snapshots')::bigint) AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_orders eo WHERE (eo.id=l.opening_exchange_order_row_id OR eo.trade_intent_id=l.cleanup_trade_intent_id) AND eo.exchange_order_id=x.exchange_order_id AND eo.client_order_id=x.client_order_id))
            AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_fill_snapshots x WHERE x.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'fill_snapshots')::bigint) AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_orders eo WHERE (eo.id=l.opening_exchange_order_row_id OR eo.trade_intent_id=l.cleanup_trade_intent_id) AND eo.exchange_order_id=x.exchange_order_id))
            AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_position_snapshots x WHERE x.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'position_snapshots')::bigint) AND (x.instrument_id IS DISTINCT FROM oi.instrument_id OR x.position_side IS DISTINCT FROM oi.position_side))
            AND jsonb_array_length((r.database_ids::jsonb)->'recovery_batches')=1
            AND EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_recovery_batches b WHERE b.database_id=((r.database_ids::jsonb)->'recovery_batches'->>0)::bigint AND b.execution_target_id='OKX_DEMO' AND b.authenticated AND b.pagination_complete AND b.complete_streams::jsonb IS NOT DISTINCT FROM '["ACCOUNT","FILL","ORDER","POSITION"]'::jsonb AND (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(b.high_watermarks::jsonb) k) IS NOT DISTINCT FROM ARRAY['ACCOUNT','FILL','ORDER','POSITION']::text[] AND b.completed_at>=statement_timestamp()-interval '30 seconds' AND b.observed_at>=statement_timestamp()-interval '30 seconds' AND b.completed_at<=statement_timestamp()+interval '5 seconds' AND b.observed_at<=statement_timestamp()+interval '5 seconds')
            AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(COALESCE((r.summary_snapshot::jsonb)->'findings','[]'::jsonb)) f WHERE (((f->>'code'='POSITION_DRIFT' AND f->>'identity'=oi.instrument_id||':'||oi.position_side) OR (f->>'code' IN('CONTROLLED_CANARY_DEADLINE_CANCEL_REQUIRED','CONTROLLED_CANARY_FILL_ATTRIBUTED','CONTROLLED_CANARY_CLEANUP_REQUIRED') AND f->>'identity'='canary:'||substr(encode(public.digest(convert_to(l.lifecycle_id,'UTF8'),'sha256'),'hex'),1,16)))) IS NOT TRUE);
        END $$;
        ALTER FUNCTION require_current_okx_demo_canary_recovery_run(bigint,varchar) OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION require_current_okx_demo_canary_recovery_run(bigint,varchar) FROM PUBLIC,freqtrade;
        CREATE OR REPLACE FUNCTION can_resume_okx_demo_canary_recovery(p_run_id bigint)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE lifecycle varchar; lifecycle_count bigint;
        BEGIN
          SELECT min(lifecycle_id),count(*) INTO lifecycle,lifecycle_count
            FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
            WHERE cleanup_phase NOT IN('TERMINAL','REVOKED');
          RETURN lifecycle_count=1
            AND SCHEMA_TOKEN.require_current_okx_demo_canary_recovery_run(
                p_run_id,lifecycle) IS TRUE
            AND NOT EXISTS(
                SELECT 1 FROM SCHEMA_TOKEN.okx_demo_recovery_grants
                WHERE status='ACTIVE' AND lifecycle_id IS DISTINCT FROM lifecycle);
        END $$;
        ALTER FUNCTION can_resume_okx_demo_canary_recovery(bigint) OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION can_resume_okx_demo_canary_recovery(bigint) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION can_resume_okx_demo_canary_recovery(bigint) TO freqtrade;
        CREATE OR REPLACE FUNCTION create_okx_demo_canary_cleanup_intent(
          p_lifecycle varchar,p_grant_id bigint,p_expected_version bigint)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE l SCHEMA_TOKEN.okx_demo_canary_lifecycles%ROWTYPE;
                g SCHEMA_TOKEN.okx_demo_recovery_grants%ROWTYPE;
                oi SCHEMA_TOKEN.trade_intents%ROWTYPE;
                oa SCHEMA_TOKEN.approved_executions%ROWTYPE;
                existing SCHEMA_TOKEN.trade_intents%ROWTYPE;
                cleanup_id bigint; cleanup_decision_id bigint; new_cleanup_approval_id bigint;
                cleanup_side text; cleanup_client text;
                identity_digest text;
        BEGIN
          SELECT * INTO l FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
            WHERE lifecycle_id=p_lifecycle FOR UPDATE;
          IF NOT FOUND OR p_expected_version IS NULL
             OR l.fencing_version IS DISTINCT FROM p_expected_version
             OR l.cleanup_phase IS DISTINCT FROM 'CLEANUP_PENDING'
             OR l.outcome IS DISTINCT FROM 'FAILED' OR l.attributed_fill_quantity<=0
          THEN RAISE EXCEPTION 'canary cleanup intent context rejected'; END IF;
          SELECT * INTO g FROM SCHEMA_TOKEN.okx_demo_recovery_grants
            WHERE database_id=p_grant_id;
          SELECT * INTO oi FROM SCHEMA_TOKEN.trade_intents
            WHERE id=l.opening_trade_intent_id;
          SELECT * INTO oa FROM SCHEMA_TOKEN.approved_executions
            WHERE id=l.opening_approval_id;
          IF g.database_id IS NULL OR oi.id IS NULL OR oa.id IS NULL
             OR g.lifecycle_id IS DISTINCT FROM l.lifecycle_id
             OR g.action IS DISTINCT FROM 'REDUCE_ONLY' OR g.status IS DISTINCT FROM 'ACTIVE'
             OR g.reconciliation_run_id IS NULL
             OR g.instrument_id IS DISTINCT FROM oi.instrument_id
             OR g.position_side IS DISTINCT FROM oi.position_side
             OR g.max_quantity IS DISTINCT FROM l.attributed_fill_quantity
             OR g.expires_at<=statement_timestamp()
             OR SCHEMA_TOKEN.require_current_okx_demo_canary_recovery_run(g.reconciliation_run_id,l.lifecycle_id) IS NOT TRUE
          THEN RAISE EXCEPTION 'canary cleanup grant binding rejected'; END IF;
          cleanup_side:=CASE WHEN oi.position_side='long' THEN 'sell'
                             WHEN oi.position_side='short' THEN 'buy' ELSE NULL END;
          cleanup_client:='rcv'||lpad(g.database_id::text,20,'0');
          identity_digest:=encode(public.digest(convert_to(concat_ws('|',l.lifecycle_id,g.database_id::text,
            oi.instrument_id,oi.position_side,cleanup_side,l.attributed_fill_quantity::text),'UTF8'),'sha256'),'hex');
          IF cleanup_side IS NULL THEN RAISE EXCEPTION 'canary cleanup direction rejected'; END IF;
          IF l.cleanup_trade_intent_id IS NOT NULL THEN
            SELECT * INTO existing FROM SCHEMA_TOKEN.trade_intents WHERE id=l.cleanup_trade_intent_id;
            IF NOT FOUND OR existing.client_order_id IS DISTINCT FROM cleanup_client
               OR existing.instrument_id IS DISTINCT FROM oi.instrument_id
               OR existing.position_side IS DISTINCT FROM oi.position_side
               OR existing.side IS DISTINCT FROM cleanup_side
               OR existing.quantity IS DISTINCT FROM l.attributed_fill_quantity
               OR existing.reduce_only IS NOT TRUE OR existing.order_type IS DISTINCT FROM 'market'
            THEN RAISE EXCEPTION 'persisted canary cleanup intent mismatch'; END IF;
            IF l.cleanup_approval_id IS NULL OR NOT EXISTS(
              SELECT 1 FROM SCHEMA_TOKEN.approved_executions a
               WHERE a.id=l.cleanup_approval_id AND a.trade_intent_id=existing.id
                 AND a.status='ACTIVE' AND a.expires_at=g.expires_at)
            THEN RAISE EXCEPTION 'persisted canary cleanup approval mismatch'; END IF;
            RETURN l.cleanup_approval_id;
          END IF;
          INSERT INTO SCHEMA_TOKEN.trade_intents(
            execution_target_id,authorization_schema_version,intent_id,canonical_hash,
            policy_digest,approved_payload_hash,idempotency_key_digest,client_order_id,
            instrument_id,side,position_side,order_type,quantity,reference_price,
            leverage,margin_mode,reduce_only,status,request_snapshot,expires_at)
          VALUES('OKX_DEMO','RISK_V1',identity_digest,identity_digest,identity_digest,
            identity_digest,identity_digest,cleanup_client,oi.instrument_id,cleanup_side,
            oi.position_side,'market',l.attributed_fill_quantity,oi.reference_price,
            oi.leverage,'isolated',TRUE,'APPROVED',jsonb_build_object(
              'provenance','CONTROLLED_CANARY_NON_PRODUCTION','lifecycle_id_hash',
              substr(encode(public.digest(convert_to(l.lifecycle_id,'UTF8'),'sha256'),'hex'),1,16),
              'recovery_grant_database_id',g.database_id,'reduce_only',TRUE)::json,g.expires_at)
          RETURNING id INTO cleanup_id;
          INSERT INTO SCHEMA_TOKEN.risk_decisions(
            execution_target_id,trade_intent_id,authorization_schema_version,
            policy_digest,decision,policy_version,evidence_snapshot)
          VALUES('OKX_DEMO',cleanup_id,'RISK_V1',identity_digest,'APPROVED',
            'controlled-canary-cleanup-v1',jsonb_build_object(
              'provenance','CONTROLLED_CANARY_NON_PRODUCTION',
              'recovery_grant_database_id',g.database_id)::json)
          RETURNING id INTO cleanup_decision_id;
          INSERT INTO SCHEMA_TOKEN.approved_executions(
            execution_target_id,trade_intent_id,risk_decision_id,intent_id,
            client_order_id,authorization_schema_version,canonical_hash,
            policy_digest,approved_payload_hash,instrument_snapshot_id,
            market_snapshot_id,account_snapshot_id,decision,intent_status,
            reserved_notional,order_submission_authorized,claim_required,status,
            expires_at,evidence_snapshot)
          VALUES('OKX_DEMO',cleanup_id,cleanup_decision_id,identity_digest,
            cleanup_client,'RISK_V1',identity_digest,identity_digest,identity_digest,
            oa.instrument_snapshot_id,oa.market_snapshot_id,oa.account_snapshot_id,
            'APPROVED','APPROVED',oa.reserved_notional,FALSE,TRUE,'ACTIVE',g.expires_at,
            jsonb_build_object('provenance','CONTROLLED_CANARY_NON_PRODUCTION',
              'recovery_grant_database_id',g.database_id)::json)
          RETURNING id INTO new_cleanup_approval_id;
          UPDATE SCHEMA_TOKEN.okx_demo_canary_lifecycles
             SET cleanup_trade_intent_id=cleanup_id,cleanup_approval_id=new_cleanup_approval_id,
                 fencing_version=fencing_version+1,
                 updated_at=statement_timestamp()
           WHERE lifecycle_id=l.lifecycle_id;
          RETURN new_cleanup_approval_id;
        END $$;
        ALTER FUNCTION create_okx_demo_canary_cleanup_intent(varchar,bigint,bigint) OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION create_okx_demo_canary_cleanup_intent(varchar,bigint,bigint) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION create_okx_demo_canary_cleanup_intent(varchar,bigint,bigint) TO freqtrade;
        CREATE OR REPLACE FUNCTION bridge_okx_demo_managed_fill(p_fill_snapshot_id bigint)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE fs SCHEMA_TOKEN.okx_demo_fill_snapshots%ROWTYPE;
                ev SCHEMA_TOKEN.okx_demo_exchange_events%ROWTYPE;
                batch SCHEMA_TOKEN.okx_demo_recovery_batches%ROWTYPE;
                managed_count bigint; managed_order_id bigint;
                existing SCHEMA_TOKEN.exchange_fills%ROWTYPE; inserted_id bigint;
                evidence jsonb;
        BEGIN
          SELECT * INTO fs FROM SCHEMA_TOKEN.okx_demo_fill_snapshots
            WHERE database_id=p_fill_snapshot_id;
          IF NOT FOUND THEN RAISE EXCEPTION 'managed fill snapshot missing'; END IF;
          SELECT * INTO ev FROM SCHEMA_TOKEN.okx_demo_exchange_events
            WHERE database_id=fs.event_database_id;
          SELECT * INTO batch FROM SCHEMA_TOKEN.okx_demo_recovery_batches
            WHERE database_id=ev.recovery_batch_database_id;
          IF ev.database_id IS NULL OR batch.database_id IS NULL
             OR fs.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
             OR ev.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
             OR batch.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
             OR ev.entity_kind IS DISTINCT FROM 'FILL'
             OR ev.payload_digest!~'^[0-9a-f]{64}$'
             OR ev.payload::jsonb IS DISTINCT FROM fs.authoritative_snapshot::jsonb
             OR ev.payload->>'fillId' IS DISTINCT FROM fs.exchange_fill_id
             OR ev.payload->>'ordId' IS DISTINCT FROM fs.exchange_order_id
             OR ev.payload->>'instId' IS DISTINCT FROM fs.instrument_id
             OR (ev.payload->>'fillPx')::numeric IS DISTINCT FROM fs.price
             OR (ev.payload->>'fillSz')::numeric IS DISTINCT FROM fs.quantity
             OR NULLIF(ev.payload->>'fee','')::numeric IS DISTINCT FROM fs.fee
             OR batch.authenticated IS NOT TRUE OR batch.pagination_complete IS NOT TRUE
             OR batch.complete_streams::jsonb IS DISTINCT FROM '["ACCOUNT","FILL","ORDER","POSITION"]'::jsonb
             OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(batch.high_watermarks::jsonb) k)
                IS DISTINCT FROM ARRAY['ACCOUNT','FILL','ORDER','POSITION']::text[]
             OR batch.observed_at>batch.completed_at OR batch.event_count<=0
          THEN RAISE EXCEPTION 'managed fill evidence rejected'; END IF;
          SELECT count(*),min(id) INTO managed_count,managed_order_id
            FROM SCHEMA_TOKEN.exchange_orders
           WHERE execution_target_id='OKX_DEMO'
             AND exchange_order_id=fs.exchange_order_id;
          IF managed_count=0 THEN RETURN NULL; END IF;
          IF managed_count<>1 THEN RAISE EXCEPTION 'managed fill order identity ambiguous'; END IF;
          evidence:=jsonb_build_object(
            'source','okx_demo_reconciliation',
            'fill_snapshot_database_id',fs.database_id,
            'event_database_id',ev.database_id,
            'payload_digest',ev.payload_digest,
            'authoritative_snapshot',fs.authoritative_snapshot::jsonb,
            'observed_at',fs.observed_at);
          SELECT * INTO existing FROM SCHEMA_TOKEN.exchange_fills
           WHERE execution_target_id='OKX_DEMO'
             AND exchange_fill_id=fs.exchange_fill_id FOR UPDATE;
          IF FOUND THEN
            IF existing.exchange_order_row_id IS DISTINCT FROM managed_order_id
               OR existing.price IS DISTINCT FROM fs.price
               OR existing.quantity IS DISTINCT FROM fs.quantity
               OR existing.fee IS DISTINCT FROM fs.fee
               OR existing.snapshot::jsonb IS DISTINCT FROM evidence
            THEN RAISE EXCEPTION 'managed fill lineage conflict'; END IF;
            RETURN existing.id;
          END IF;
          INSERT INTO SCHEMA_TOKEN.exchange_fills(
            execution_target_id,exchange_order_row_id,exchange_fill_id,
            price,quantity,fee,snapshot)
          VALUES('OKX_DEMO',managed_order_id,fs.exchange_fill_id,
            fs.price,fs.quantity,fs.fee,evidence::json)
          RETURNING id INTO inserted_id;
          RETURN inserted_id;
        END $$;
        ALTER FUNCTION bridge_okx_demo_managed_fill(bigint) OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION bridge_okx_demo_managed_fill(bigint) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION bridge_okx_demo_managed_fill(bigint) TO freqtrade;
        CREATE OR REPLACE FUNCTION prepare_okx_demo_canary_residual_child(
          p_parent_attempt_id bigint,p_grant_id bigint)
        RETURNS TABLE(order_id bigint,attempt_id bigint)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE parent SCHEMA_TOKEN.okx_order_write_attempts%ROWTYPE;
                old_grant SCHEMA_TOKEN.okx_demo_recovery_grants%ROWTYPE;
                fresh_grant SCHEMA_TOKEN.okx_demo_recovery_grants%ROWTYPE;
                lifecycle SCHEMA_TOKEN.okx_demo_canary_lifecycles%ROWTYPE;
                intent SCHEMA_TOKEN.trade_intents%ROWTYPE;
                lease SCHEMA_TOKEN.okx_order_writer_leases%ROWTYPE;
                filled numeric; remaining numeric; next_sequence integer;
                child_client text; child_body jsonb; child_digest text;
                new_order_id bigint; new_attempt_id bigint;
        BEGIN
          PERFORM SCHEMA_TOKEN.lock_okx_demo_reconciliation_state();
          SELECT * INTO parent FROM SCHEMA_TOKEN.okx_order_write_attempts
           WHERE id=p_parent_attempt_id FOR UPDATE;
          SELECT * INTO fresh_grant FROM SCHEMA_TOKEN.okx_demo_recovery_grants
           WHERE database_id=p_grant_id FOR UPDATE;
          SELECT * INTO old_grant FROM SCHEMA_TOKEN.okx_demo_recovery_grants
           WHERE database_id=parent.recovery_grant_database_id;
          SELECT * INTO lifecycle FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
           WHERE lifecycle_id=fresh_grant.lifecycle_id FOR UPDATE;
          SELECT * INTO intent FROM SCHEMA_TOKEN.trade_intents
           WHERE id=lifecycle.cleanup_trade_intent_id;
          SELECT * INTO lease FROM SCHEMA_TOKEN.okx_order_writer_leases
           WHERE execution_target_id='OKX_DEMO' FOR UPDATE;
          next_sequence:=parent.close_sequence+1;
          SELECT COALESCE(sum(COALESCE(NULLIF(a.safe_response_snapshot::jsonb->>'accumulated_fill_size','')::numeric,0)),0)
            INTO filled FROM SCHEMA_TOKEN.okx_order_write_attempts a
            JOIN SCHEMA_TOKEN.exchange_orders eo ON eo.id=a.exchange_order_row_id
           WHERE eo.trade_intent_id=intent.id AND a.operation='CLOSE';
          remaining:=intent.quantity-filled;
          IF parent.id IS NULL OR fresh_grant.database_id IS NULL OR old_grant.database_id IS NULL
             OR lifecycle.lifecycle_id IS NULL OR intent.id IS NULL OR lease.execution_target_id IS NULL
             OR parent.operation IS DISTINCT FROM 'CLOSE'
             OR parent.state IS DISTINCT FROM 'RESIDUAL_CLOSE_REQUIRED'
             OR next_sequence NOT BETWEEN 1 AND 3
             OR parent.lease_generation IS DISTINCT FROM lease.generation
             OR lease.expires_at<=statement_timestamp()
             OR old_grant.status IS DISTINCT FROM 'CONSUMED'
             OR old_grant.lifecycle_id IS DISTINCT FROM lifecycle.lifecycle_id
             OR fresh_grant.database_id=old_grant.database_id
             OR fresh_grant.lifecycle_id IS DISTINCT FROM lifecycle.lifecycle_id
             OR fresh_grant.action IS DISTINCT FROM 'REDUCE_ONLY'
             OR fresh_grant.status IS DISTINCT FROM 'ACTIVE'
             OR fresh_grant.expires_at<=statement_timestamp()
             OR lifecycle.cleanup_phase IS DISTINCT FROM 'CLEANUP_PENDING'
             OR lifecycle.outcome IS DISTINCT FROM 'FAILED'
             OR parent.approval_id IS DISTINCT FROM lifecycle.cleanup_approval_id
             OR intent.execution_target_id IS DISTINCT FROM 'OKX_DEMO'
             OR intent.reduce_only IS NOT TRUE OR intent.order_type IS DISTINCT FROM 'market'
             OR fresh_grant.instrument_id IS DISTINCT FROM intent.instrument_id
             OR fresh_grant.position_side IS DISTINCT FROM intent.position_side
             OR remaining<=0 OR fresh_grant.max_quantity IS DISTINCT FROM remaining
             OR SCHEMA_TOKEN.require_current_okx_demo_canary_recovery_run(
                  fresh_grant.reconciliation_run_id,lifecycle.lifecycle_id) IS NOT TRUE
          THEN RAISE EXCEPTION 'residual canary child context rejected'; END IF;
          child_client:='rcv'||lpad(fresh_grant.database_id::text,20,'0')||'C'||next_sequence::text;
          child_body:=jsonb_build_object(
            'instId',intent.instrument_id,'tdMode','isolated',
            'side',CASE WHEN intent.position_side='long' THEN 'sell' ELSE 'buy' END,
            'posSide',intent.position_side,'ordType','market','sz',remaining::text,
            'clOrdId',child_client,'reduceOnly',TRUE);
          child_digest:=encode(public.digest(convert_to(child_body::text,'UTF8'),'sha256'),'hex');
          INSERT INTO SCHEMA_TOKEN.exchange_orders(
            execution_target_id,trade_intent_id,client_order_id,status,
            request_snapshot,response_snapshot)
          VALUES('OKX_DEMO',intent.id,child_client,'PREPARED',child_body::json,'{}'::json)
          RETURNING id INTO new_order_id;
          UPDATE SCHEMA_TOKEN.okx_order_write_attempts
             SET state='RECONCILED',order_state='residual_cleanup_started',
                 reason_code='SUPERSEDED_BY_CLOSE_CLEANUP',updated_at=statement_timestamp()
           WHERE id=parent.id;
          UPDATE SCHEMA_TOKEN.okx_demo_recovery_grants
             SET status='CONSUMED',consumed_at=statement_timestamp()
           WHERE database_id=fresh_grant.database_id AND status='ACTIVE';
          IF NOT FOUND THEN RAISE EXCEPTION 'fresh residual grant consume failed'; END IF;
          INSERT INTO SCHEMA_TOKEN.okx_order_write_attempts(
            execution_target_id,exchange_order_row_id,approval_id,recovery_grant_database_id,
            operation,operation_id,client_order_id,instrument_id,state,request_digest,
            safe_request_snapshot,safe_response_snapshot,attempt_count,lease_generation,
            parent_attempt_id,close_sequence,last_attempt_at,created_at,updated_at)
          VALUES('OKX_DEMO',new_order_id,lifecycle.cleanup_approval_id,fresh_grant.database_id,
            'CLOSE',child_client,child_client,intent.instrument_id,'PREPARED',child_digest,
            child_body::json,'{}'::json,1,lease.generation,parent.id,next_sequence,
            statement_timestamp(),statement_timestamp(),statement_timestamp())
          RETURNING id INTO new_attempt_id;
          RETURN QUERY SELECT new_order_id,new_attempt_id;
        END $$;
        ALTER FUNCTION prepare_okx_demo_canary_residual_child(bigint,bigint) OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION prepare_okx_demo_canary_residual_child(bigint,bigint) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION prepare_okx_demo_canary_residual_child(bigint,bigint) TO freqtrade;
        CREATE OR REPLACE FUNCTION transition_okx_demo_canary_lifecycle(
          p_lifecycle varchar,p_action text,p_order_id bigint,p_run_id bigint,
          p_evidence_digest varchar,p_expected_version bigint)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE l SCHEMA_TOKEN.okx_demo_canary_lifecycles%ROWTYPE;
                o SCHEMA_TOKEN.exchange_orders%ROWTYPE;
                i SCHEMA_TOKEN.trade_intents%ROWTYPE;
                r SCHEMA_TOKEN.reconciliation_runs%ROWTYPE;
                exhaustion_attempt SCHEMA_TOKEN.okx_order_write_attempts%ROWTYPE;
                exact_fill numeric; remaining numeric; authoritative_remaining numeric;
                next_outcome text; next_version bigint;
                computed_digest text; opening_state text; opening_filled numeric;
        BEGIN
          SELECT * INTO l FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles WHERE lifecycle_id=p_lifecycle FOR UPDATE;
          IF NOT FOUND OR p_expected_version IS NULL OR l.fencing_version IS DISTINCT FROM p_expected_version OR p_evidence_digest IS NULL OR p_evidence_digest!~'^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'stale or invalid canary lifecycle transition'; END IF;
          IF p_action='BIND_OPENING' THEN
            SELECT * INTO o FROM SCHEMA_TOKEN.exchange_orders WHERE id=p_order_id;
            IF NOT FOUND OR l.cleanup_phase IS DISTINCT FROM 'ARMED'
               OR o.trade_intent_id IS DISTINCT FROM l.opening_trade_intent_id
               OR o.client_order_id IS DISTINCT FROM (SELECT client_order_id FROM SCHEMA_TOKEN.trade_intents WHERE id=l.opening_trade_intent_id)
               OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants g WHERE g.grant_id=l.submission_grant_id AND g.status='CONSUMED' AND g.approval_id=l.opening_approval_id AND g.consumed_at IS NOT NULL)
               OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts a WHERE a.exchange_order_row_id=o.id AND a.approval_id=l.opening_approval_id AND a.operation='PLACE' AND a.attempt_count=1 AND a.recovery_grant_database_id IS NULL AND a.created_at<=l.deadline_at)
               OR p_evidence_digest IS DISTINCT FROM (SELECT encode(public.digest(convert_to(concat_ws('|',o.id::text,o.client_order_id,ti.instrument_id,a.request_digest),'UTF8'),'sha256'),'hex') FROM SCHEMA_TOKEN.trade_intents ti JOIN SCHEMA_TOKEN.okx_order_write_attempts a ON a.exchange_order_row_id=o.id WHERE ti.id=o.trade_intent_id AND a.operation='PLACE')
            THEN RAISE EXCEPTION 'opening order lifecycle binding rejected'; END IF;
            UPDATE SCHEMA_TOKEN.okx_demo_canary_lifecycles SET opening_exchange_order_row_id=o.id,opening_order_identity_digest=p_evidence_digest,cleanup_phase='OPENING_SUBMITTED',fencing_version=fencing_version+1,updated_at=statement_timestamp() WHERE lifecycle_id=p_lifecycle RETURNING fencing_version INTO next_version;
          ELSIF p_action='RECORD_FILLS' THEN
            SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs WHERE id=p_run_id;
            IF NOT FOUND OR SCHEMA_TOKEN.require_current_okx_demo_canary_recovery_run(p_run_id,p_lifecycle) IS NOT TRUE
               OR l.opening_exchange_order_row_id IS NULL
               OR (SELECT exchange_order_id FROM SCHEMA_TOKEN.exchange_orders WHERE id=l.opening_exchange_order_row_id) IS NULL
            THEN RAISE EXCEPTION 'fill attribution run rejected'; END IF;
            SELECT os.status,os.filled_quantity INTO opening_state,opening_filled FROM SCHEMA_TOKEN.okx_demo_order_snapshots os
              JOIN SCHEMA_TOKEN.exchange_orders eo ON eo.id=l.opening_exchange_order_row_id
              WHERE os.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'order_snapshots')::bigint)
                AND os.exchange_order_id=eo.exchange_order_id AND os.client_order_id=eo.client_order_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'exact opening order snapshot missing'; END IF;
            SELECT COALESCE(sum(f.quantity),0) INTO exact_fill FROM SCHEMA_TOKEN.okx_demo_fill_snapshots f
              WHERE f.database_id IN (SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'fill_snapshots')::bigint)
                AND f.exchange_order_id=(SELECT exchange_order_id FROM SCHEMA_TOKEN.exchange_orders WHERE id=l.opening_exchange_order_row_id);
            computed_digest:=encode(public.digest(convert_to(concat_ws('|',l.lifecycle_id,r.id::text,r.artifact_sha256,exact_fill::text,opening_state,COALESCE((SELECT string_agg(value::text,',' ORDER BY value::text) FROM jsonb_array_elements_text((r.database_ids::jsonb)->'fill_snapshots') value),'')),'UTF8'),'sha256'),'hex');
            IF l.cleanup_phase NOT IN('OPENING_SUBMITTED','CANCEL_PENDING','CLEANUP_PENDING')
               OR opening_filled<0 OR opening_filled>l.max_quantity
               OR exact_fill IS DISTINCT FROM opening_filled
               OR exact_fill<l.attributed_fill_quantity OR exact_fill>l.max_quantity
               OR p_evidence_digest IS DISTINCT FROM computed_digest
            THEN RAISE EXCEPTION 'fill attribution transition rejected'; END IF;
            next_outcome:=CASE WHEN exact_fill>0 THEN 'FAILED' ELSE l.outcome END;
            UPDATE SCHEMA_TOKEN.okx_demo_canary_lifecycles SET attributed_fill_quantity=exact_fill,fill_attribution_digest=computed_digest,outcome=next_outcome,failure_code=CASE WHEN exact_fill>0 THEN 'CANARY_FILLED' ELSE failure_code END,cleanup_phase=CASE WHEN statement_timestamp()>=deadline_at AND opening_state IN('live','partially_filled') THEN 'CANCEL_PENDING' WHEN statement_timestamp()>=deadline_at AND exact_fill=0 AND opening_state IN('filled','canceled','mmp_canceled') THEN 'CANCEL_PENDING' WHEN exact_fill>0 AND opening_state IN('filled','canceled','mmp_canceled') THEN 'CLEANUP_PENDING' ELSE cleanup_phase END,fencing_version=fencing_version+1,updated_at=statement_timestamp() WHERE lifecycle_id=p_lifecycle RETURNING fencing_version INTO next_version;
          ELSIF p_action='BIND_CLEANUP' THEN
            SELECT * INTO o FROM SCHEMA_TOKEN.exchange_orders WHERE id=p_order_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'cleanup order missing'; END IF;
            SELECT * INTO i FROM SCHEMA_TOKEN.trade_intents WHERE id=o.trade_intent_id;
            IF NOT FOUND OR SCHEMA_TOKEN.require_current_okx_demo_canary_recovery_run(p_run_id,p_lifecycle) IS NOT TRUE
               OR l.cleanup_phase IS DISTINCT FROM 'CLEANUP_PENDING' OR l.outcome IS DISTINCT FROM 'FAILED'
               OR i.id IS DISTINCT FROM l.cleanup_trade_intent_id OR l.cleanup_approval_id IS NULL
               OR i.execution_target_id IS DISTINCT FROM 'OKX_DEMO' OR i.reduce_only IS NOT TRUE
               OR i.instrument_id IS DISTINCT FROM (SELECT instrument_id FROM SCHEMA_TOKEN.trade_intents WHERE id=l.opening_trade_intent_id)
               OR i.position_side IS DISTINCT FROM (SELECT position_side FROM SCHEMA_TOKEN.trade_intents WHERE id=l.opening_trade_intent_id)
               OR i.side IS DISTINCT FROM (CASE
                    WHEN (SELECT position_side FROM SCHEMA_TOKEN.trade_intents WHERE id=l.opening_trade_intent_id)='long' THEN 'sell'
                    WHEN (SELECT position_side FROM SCHEMA_TOKEN.trade_intents WHERE id=l.opening_trade_intent_id)='short' THEN 'buy'
                    ELSE NULL END)
               OR i.quantity IS DISTINCT FROM l.attributed_fill_quantity
               OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_order_snapshots os JOIN SCHEMA_TOKEN.exchange_orders eo ON eo.id=l.opening_exchange_order_row_id JOIN SCHEMA_TOKEN.reconciliation_runs rr ON rr.id=p_run_id WHERE os.database_id IN(SELECT jsonb_array_elements_text((rr.database_ids::jsonb)->'order_snapshots')::bigint) AND os.exchange_order_id=eo.exchange_order_id AND os.client_order_id=eo.client_order_id AND os.status IN('filled','canceled','mmp_canceled'))
               OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts a JOIN SCHEMA_TOKEN.okx_demo_recovery_grants g ON g.database_id=a.recovery_grant_database_id WHERE a.exchange_order_row_id=o.id AND a.approval_id=l.cleanup_approval_id AND a.operation='CLOSE' AND a.state='PREPARED' AND a.attempt_count=1 AND g.lifecycle_id=l.lifecycle_id AND g.reconciliation_run_id=p_run_id AND g.action='REDUCE_ONLY' AND g.status='CONSUMED' AND g.exchange_order_row_id IS NULL AND g.instrument_id=i.instrument_id AND g.position_side=i.position_side AND g.max_quantity=l.attributed_fill_quantity)
               OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_position_snapshots p JOIN SCHEMA_TOKEN.reconciliation_runs rr ON rr.id=p_run_id WHERE p.database_id IN(SELECT jsonb_array_elements_text((rr.database_ids::jsonb)->'position_snapshots')::bigint) AND p.instrument_id=i.instrument_id AND p.position_side=i.position_side AND abs(p.quantity)=l.attributed_fill_quantity)
               OR p_evidence_digest IS DISTINCT FROM encode(public.digest(convert_to(concat_ws('|',l.lifecycle_id,o.id::text,i.id::text,i.quantity::text,p_run_id::text),'UTF8'),'sha256'),'hex')
            THEN RAISE EXCEPTION 'cleanup lifecycle binding rejected'; END IF;
            UPDATE SCHEMA_TOKEN.okx_demo_canary_lifecycles SET cleanup_trade_intent_id=i.id,cleanup_exchange_order_row_id=o.id,fencing_version=fencing_version+1,updated_at=statement_timestamp() WHERE lifecycle_id=p_lifecycle RETURNING fencing_version INTO next_version;
          ELSIF p_action='EXHAUST_RECOVERY' THEN
            SELECT * INTO exhaustion_attempt FROM SCHEMA_TOKEN.okx_order_write_attempts
             WHERE id=p_order_id FOR UPDATE;
            SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs WHERE id=p_run_id;
            SELECT * INTO i FROM SCHEMA_TOKEN.trade_intents WHERE id=l.cleanup_trade_intent_id;
            SELECT i.quantity-COALESCE(sum(COALESCE(NULLIF(x.safe_response_snapshot::jsonb->>'accumulated_fill_size','')::numeric,0)),0)
              INTO remaining FROM SCHEMA_TOKEN.okx_order_write_attempts x
              JOIN SCHEMA_TOKEN.exchange_orders eo ON eo.id=x.exchange_order_row_id
             WHERE eo.trade_intent_id=l.cleanup_trade_intent_id AND x.operation='CLOSE';
            SELECT abs(p.quantity) INTO authoritative_remaining
              FROM SCHEMA_TOKEN.okx_demo_position_snapshots p
             WHERE p.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'position_snapshots')::bigint)
               AND p.instrument_id=i.instrument_id AND p.position_side=i.position_side;
            computed_digest:=encode(public.digest(convert_to(concat_ws('|',
              l.lifecycle_id,exhaustion_attempt.id::text,r.id::text,r.artifact_sha256,remaining::text,
              'CLEANUP_LIMIT_REACHED'),'UTF8'),'sha256'),'hex');
            IF NOT FOUND OR l.cleanup_phase IS DISTINCT FROM 'CLEANUP_PENDING'
               OR l.outcome IS DISTINCT FROM 'FAILED'
               OR r.id IS NULL OR i.id IS NULL
               OR SCHEMA_TOKEN.require_current_okx_demo_canary_recovery_run(
                    p_run_id,p_lifecycle) IS NOT TRUE
               OR remaining<=0 OR authoritative_remaining IS DISTINCT FROM remaining
               OR exhaustion_attempt.operation IS DISTINCT FROM 'CLOSE'
               OR exhaustion_attempt.state IS DISTINCT FROM 'RESIDUAL_CLOSE_REQUIRED'
               OR exhaustion_attempt.close_sequence IS DISTINCT FROM 3
               OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_orders eo
                    WHERE eo.id=exhaustion_attempt.exchange_order_row_id
                      AND eo.trade_intent_id=l.cleanup_trade_intent_id)
               OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_recovery_grants g
                    WHERE g.database_id=exhaustion_attempt.recovery_grant_database_id
                      AND g.lifecycle_id=l.lifecycle_id AND g.status='CONSUMED')
               OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_recovery_grants g
                    WHERE g.lifecycle_id=l.lifecycle_id AND g.status='ACTIVE')
               OR p_evidence_digest IS DISTINCT FROM computed_digest
            THEN RAISE EXCEPTION 'canary cleanup exhaustion rejected'; END IF;
            UPDATE SCHEMA_TOKEN.okx_demo_canary_lifecycles
               SET cleanup_phase='RECOVERY_EXHAUSTED',failure_code='CLEANUP_LIMIT_REACHED',
                   fencing_version=fencing_version+1,updated_at=statement_timestamp()
             WHERE lifecycle_id=p_lifecycle RETURNING fencing_version INTO next_version;
          ELSIF p_action IN('TERMINALIZE','REVOKE_UNOPENED') THEN
            SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs WHERE id=p_run_id;
            IF NOT FOUND OR (SCHEMA_TOKEN.require_current_okx_demo_canary_run(p_run_id) IS NOT TRUE
               AND SCHEMA_TOKEN.require_current_okx_demo_canary_recovery_run(p_run_id,p_lifecycle) IS NOT TRUE)
            THEN RAISE EXCEPTION 'terminal lifecycle run rejected'; END IF;
            computed_digest:=encode(public.digest(convert_to(concat_ws('|',l.lifecycle_id,r.id::text,r.artifact_sha256,l.outcome,l.attributed_fill_quantity::text),'UTF8'),'sha256'),'hex');
            IF p_evidence_digest IS DISTINCT FROM computed_digest THEN RAISE EXCEPTION 'terminal lifecycle digest rejected'; END IF;
            IF p_action='REVOKE_UNOPENED' THEN
              IF l.cleanup_phase IS DISTINCT FROM 'ARMED' OR l.opening_exchange_order_row_id IS NOT NULL
                 OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts a WHERE a.approval_id=l.opening_approval_id AND a.operation='PLACE')
                 OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants g WHERE g.grant_id=l.submission_grant_id AND g.status='ACTIVE')
              THEN RAISE EXCEPTION 'unopened lifecycle revocation rejected'; END IF;
              UPDATE SCHEMA_TOKEN.okx_demo_submission_grants SET status='FAILED',consumed_at=statement_timestamp() WHERE grant_id=l.submission_grant_id AND status='ACTIVE';
            ELSE
              IF statement_timestamp()<l.deadline_at OR l.cleanup_phase NOT IN('OPENING_SUBMITTED','CANCEL_PENDING','CLEANUP_PENDING') OR l.opening_exchange_order_row_id IS NULL
                 OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_order_snapshots os JOIN SCHEMA_TOKEN.exchange_orders eo ON eo.id=l.opening_exchange_order_row_id WHERE os.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'order_snapshots')::bigint) AND os.exchange_order_id=eo.exchange_order_id AND os.client_order_id=eo.client_order_id AND os.status IN('filled','canceled','mmp_canceled') AND os.filled_quantity=l.attributed_fill_quantity)
                 OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_position_snapshots p JOIN SCHEMA_TOKEN.trade_intents oi ON oi.id=l.opening_trade_intent_id WHERE p.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'position_snapshots')::bigint) AND p.instrument_id=oi.instrument_id AND p.position_side=oi.position_side AND p.quantity<>0)
	                 OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_order_snapshots os
	                      WHERE os.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'order_snapshots')::bigint)
	                        AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_orders eo
	                          WHERE (eo.id=l.opening_exchange_order_row_id OR eo.trade_intent_id=l.cleanup_trade_intent_id)
	                            AND eo.exchange_order_id=os.exchange_order_id AND eo.client_order_id=os.client_order_id))
                 OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_fill_snapshots fs
	                      WHERE fs.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'fill_snapshots')::bigint)
	                        AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_orders eo
	                          WHERE (eo.id=l.opening_exchange_order_row_id OR eo.trade_intent_id=l.cleanup_trade_intent_id)
	                            AND eo.exchange_order_id=fs.exchange_order_id))
                 OR (l.attributed_fill_quantity>0 AND (
                      (SELECT COALESCE(sum(fs.quantity),0) FROM SCHEMA_TOKEN.okx_demo_fill_snapshots fs
                        JOIN SCHEMA_TOKEN.exchange_orders eo ON eo.id=l.opening_exchange_order_row_id
                        WHERE fs.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'fill_snapshots')::bigint)
                          AND fs.exchange_order_id=eo.exchange_order_id) IS DISTINCT FROM l.attributed_fill_quantity
                      OR
	                      (SELECT COALESCE(sum(fs.quantity),0) FROM SCHEMA_TOKEN.okx_demo_fill_snapshots fs
	                        JOIN SCHEMA_TOKEN.exchange_orders eo ON eo.trade_intent_id=l.cleanup_trade_intent_id
	                        WHERE fs.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'fill_snapshots')::bigint)
	                          AND fs.exchange_order_id=eo.exchange_order_id) IS DISTINCT FROM l.attributed_fill_quantity))
                 OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_position_snapshots ps
                      JOIN SCHEMA_TOKEN.trade_intents oi ON oi.id=l.opening_trade_intent_id
                      WHERE ps.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'position_snapshots')::bigint)
                        AND (ps.instrument_id IS DISTINCT FROM oi.instrument_id OR ps.position_side IS DISTINCT FROM oi.position_side))
	                 OR (l.attributed_fill_quantity>0 AND (l.cleanup_exchange_order_row_id IS NULL
	                      OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_orders ce
	                         WHERE ce.trade_intent_id=l.cleanup_trade_intent_id
	                           AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_order_snapshots cs
	                             WHERE cs.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'order_snapshots')::bigint)
	                               AND cs.exchange_order_id=ce.exchange_order_id AND cs.client_order_id=ce.client_order_id
	                               AND cs.reduce_only AND cs.status IN('filled','canceled','mmp_canceled')))
	                      OR (SELECT COALESCE(sum(cs.filled_quantity),0) FROM SCHEMA_TOKEN.okx_demo_order_snapshots cs
	                            JOIN SCHEMA_TOKEN.exchange_orders ce ON ce.trade_intent_id=l.cleanup_trade_intent_id
	                           WHERE cs.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'order_snapshots')::bigint)
	                             AND cs.exchange_order_id=ce.exchange_order_id AND cs.client_order_id=ce.client_order_id)
	                         IS DISTINCT FROM l.attributed_fill_quantity))
	                 OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts a
	                      JOIN SCHEMA_TOKEN.exchange_orders eo ON eo.id=a.exchange_order_row_id
	                     WHERE (eo.id=l.opening_exchange_order_row_id OR eo.trade_intent_id=l.cleanup_trade_intent_id)
	                       AND a.state IN('PREPARED','DISPATCHED','ACKNOWLEDGED','RECOVERY_REQUIRED','RESIDUAL_CLOSE_REQUIRED'))
              THEN RAISE EXCEPTION 'terminal lifecycle evidence rejected'; END IF;
            END IF;
            UPDATE SCHEMA_TOKEN.okx_demo_recovery_grants SET status='EXPIRED' WHERE lifecycle_id=p_lifecycle AND status='ACTIVE';
            UPDATE SCHEMA_TOKEN.okx_demo_canary_lifecycles SET outcome=CASE WHEN p_action='REVOKE_UNOPENED' THEN 'FAILED' WHEN outcome='FAILED' THEN 'FAILED' WHEN attributed_fill_quantity=0 THEN 'PASSED' ELSE 'FAILED' END,failure_code=CASE WHEN p_action='REVOKE_UNOPENED' THEN 'AUTHORIZATION_REVOKED' ELSE failure_code END,cleanup_phase=CASE WHEN p_action='REVOKE_UNOPENED' THEN 'REVOKED' ELSE 'TERMINAL' END,final_reconciliation_run_id=r.id,final_evidence_digest=computed_digest,terminal_at=statement_timestamp(),revoked_at=statement_timestamp(),fencing_version=fencing_version+1,updated_at=statement_timestamp() WHERE lifecycle_id=p_lifecycle RETURNING fencing_version INTO next_version;
          ELSE RAISE EXCEPTION 'unsupported canary lifecycle transition'; END IF;
          RETURN next_version;
        END $$;
        ALTER FUNCTION transition_okx_demo_canary_lifecycle(varchar,text,bigint,bigint,varchar,bigint) OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION transition_okx_demo_canary_lifecycle(varchar,text,bigint,bigint,varchar,bigint) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION transition_okx_demo_canary_lifecycle(varchar,text,bigint,bigint,varchar,bigint) TO freqtrade;
        CREATE OR REPLACE FUNCTION guard_okx_demo_canary_recovery_insert()
        RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN
          PERFORM SCHEMA_TOKEN.lock_okx_demo_reconciliation_state();
          IF NEW.lifecycle_id IS NOT NULL AND current_user IS DISTINCT FROM 'freqtrade_ai_attestor'
          THEN RAISE EXCEPTION 'lifecycle recovery grants require controlled issuer'; END IF;
          IF NEW.lifecycle_id IS NULL AND EXISTS(
              SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
              WHERE cleanup_phase NOT IN('TERMINAL','REVOKED'))
          THEN RAISE EXCEPTION 'generic recovery grant conflicts with controlled canary lifecycle'; END IF;
          RETURN NEW;
        END $$;
        ALTER FUNCTION guard_okx_demo_canary_recovery_insert() OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION guard_okx_demo_canary_recovery_insert() FROM PUBLIC,freqtrade;
        DROP TRIGGER IF EXISTS okx_demo_canary_recovery_insert_guard ON okx_demo_recovery_grants;
        CREATE TRIGGER okx_demo_canary_recovery_insert_guard BEFORE INSERT ON okx_demo_recovery_grants FOR EACH ROW EXECUTE FUNCTION guard_okx_demo_canary_recovery_insert();
        CREATE OR REPLACE FUNCTION issue_okx_demo_canary_recovery_grant(
          p_lifecycle varchar,p_run_id bigint,p_action text,p_expected_version bigint)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE l SCHEMA_TOKEN.okx_demo_canary_lifecycles%ROWTYPE; oi SCHEMA_TOKEN.trade_intents%ROWTYPE;
                r SCHEMA_TOKEN.reconciliation_runs%ROWTYPE;
                quantity numeric; order_row bigint; inserted_id bigint; digest text; effective_expiry timestamptz;
        BEGIN
          SELECT * INTO l FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles WHERE lifecycle_id=p_lifecycle FOR UPDATE;
          SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs WHERE id=p_run_id;
          IF NOT FOUND THEN RAISE EXCEPTION 'canary recovery run missing'; END IF;
          effective_expiry:=LEAST(statement_timestamp()+interval '10 seconds',r.completed_at+interval '30 seconds',r.authoritative_observed_at+interval '30 seconds');
          IF NOT FOUND OR p_expected_version IS NULL OR l.fencing_version IS DISTINCT FROM p_expected_version
             OR SCHEMA_TOKEN.require_current_okx_demo_canary_recovery_run(p_run_id,p_lifecycle) IS NOT TRUE
             OR effective_expiry<=statement_timestamp()
             OR (p_action='CANCEL' AND NOT EXISTS(
                  SELECT 1 FROM jsonb_array_elements((r.summary_snapshot::jsonb)->'findings') f
                   WHERE f->>'code'='CONTROLLED_CANARY_DEADLINE_CANCEL_REQUIRED'
                     AND f->>'identity'='canary:'||substr(encode(public.digest(convert_to(l.lifecycle_id,'UTF8'),'sha256'),'hex'),1,16)))
             OR (p_action='CANCEL' AND NOT EXISTS(
                  SELECT 1 FROM SCHEMA_TOKEN.okx_demo_order_snapshots os
                   JOIN SCHEMA_TOKEN.exchange_orders eo ON eo.id=l.opening_exchange_order_row_id
                   WHERE os.database_id IN(SELECT jsonb_array_elements_text((r.database_ids::jsonb)->'order_snapshots')::bigint)
                     AND os.exchange_order_id=eo.exchange_order_id
                     AND os.client_order_id=eo.client_order_id
                     AND os.status IN('live','partially_filled')))
             OR (p_action='REDUCE_ONLY' AND NOT EXISTS(
                  SELECT 1 FROM jsonb_array_elements((r.summary_snapshot::jsonb)->'findings') f
                   WHERE f->>'code'='CONTROLLED_CANARY_CLEANUP_REQUIRED'
                     AND f->>'identity'='canary:'||substr(encode(public.digest(convert_to(l.lifecycle_id,'UTF8'),'sha256'),'hex'),1,16)))
             OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_recovery_grants g WHERE g.lifecycle_id=l.lifecycle_id AND g.action=p_action AND g.status='ACTIVE')
             OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_recovery_grants g
                  JOIN SCHEMA_TOKEN.okx_order_write_attempts a
                    ON a.recovery_grant_database_id=g.database_id
                 WHERE g.lifecycle_id=l.lifecycle_id
                   AND g.action=p_action
                   AND a.state IN('PREPARED','DISPATCHED','ACKNOWLEDGED','RECOVERY_REQUIRED','RESIDUAL_CLOSE_REQUIRED')
                   AND NOT (p_action='REDUCE_ONLY'
                     AND a.state='RESIDUAL_CLOSE_REQUIRED'
                     AND a.close_sequence<3
                     AND g.status='CONSUMED'
                     AND EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_orders eo
                       WHERE eo.id=a.exchange_order_row_id
                         AND eo.trade_intent_id=l.cleanup_trade_intent_id)))
          THEN RAISE EXCEPTION 'canary recovery grant context rejected'; END IF;
          SELECT * INTO oi FROM SCHEMA_TOKEN.trade_intents WHERE id=l.opening_trade_intent_id;
          IF p_action='CANCEL' AND l.cleanup_phase='CANCEL_PENDING' THEN quantity:=0; order_row:=l.opening_exchange_order_row_id;
          ELSIF p_action='REDUCE_ONLY' AND l.cleanup_phase='CLEANUP_PENDING' AND l.outcome='FAILED' THEN
            SELECT abs(p.quantity) INTO quantity FROM SCHEMA_TOKEN.okx_demo_position_snapshots p JOIN SCHEMA_TOKEN.reconciliation_runs rr ON rr.id=p_run_id WHERE p.database_id IN(SELECT jsonb_array_elements_text((rr.database_ids::jsonb)->'position_snapshots')::bigint) AND p.instrument_id=oi.instrument_id AND p.position_side=oi.position_side;
            IF NOT FOUND OR quantity<=0 OR quantity>l.attributed_fill_quantity THEN RAISE EXCEPTION 'canary cleanup quantity rejected'; END IF; order_row:=NULL;
          ELSE RAISE EXCEPTION 'canary recovery action rejected'; END IF;
          digest:=encode(public.digest(convert_to(concat_ws('|',l.lifecycle_id,p_run_id::text,p_action,COALESCE(order_row::text,''),quantity::text,effective_expiry::text),'UTF8'),'sha256'),'hex');
          INSERT INTO SCHEMA_TOKEN.okx_demo_recovery_grants(execution_target_id,reconciliation_run_id,lifecycle_id,exchange_order_row_id,grant_digest,action,instrument_id,position_side,max_quantity,status,expires_at)
          VALUES('OKX_DEMO',p_run_id,l.lifecycle_id,order_row,digest,p_action,oi.instrument_id,oi.position_side,quantity,'ACTIVE',effective_expiry) RETURNING database_id INTO inserted_id;
          UPDATE SCHEMA_TOKEN.okx_demo_canary_lifecycles SET fencing_version=fencing_version+1,updated_at=statement_timestamp() WHERE lifecycle_id=l.lifecycle_id;
          RETURN inserted_id;
        END $$;
        ALTER FUNCTION issue_okx_demo_canary_recovery_grant(varchar,bigint,text,bigint) OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION issue_okx_demo_canary_recovery_grant(varchar,bigint,text,bigint) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION issue_okx_demo_canary_recovery_grant(varchar,bigint,text,bigint) TO freqtrade;
        ALTER TABLE okx_demo_canary_lifecycles OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON TABLE okx_demo_canary_lifecycles FROM PUBLIC, freqtrade;
        REVOKE INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON TABLE okx_demo_canary_lifecycles FROM freqtrade;
        CREATE OR REPLACE FUNCTION guard_okx_demo_recovery_lifecycle_identity()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
          IF TG_OP='UPDATE' AND OLD.lifecycle_id IS DISTINCT FROM NEW.lifecycle_id
          THEN RAISE EXCEPTION 'recovery lifecycle identity is immutable'; END IF;
          RETURN NEW;
        END $$;
        ALTER FUNCTION guard_okx_demo_recovery_lifecycle_identity() OWNER TO freqtrade_ai_attestor;
        REVOKE ALL ON FUNCTION guard_okx_demo_recovery_lifecycle_identity() FROM PUBLIC,freqtrade;
        DROP TRIGGER IF EXISTS okx_demo_recovery_lifecycle_identity_guard ON okx_demo_recovery_grants;
        CREATE TRIGGER okx_demo_recovery_lifecycle_identity_guard BEFORE UPDATE ON okx_demo_recovery_grants FOR EACH ROW EXECUTE FUNCTION guard_okx_demo_recovery_lifecycle_identity();
        GRANT SELECT (
          lifecycle_id,execution_target_id,opening_trade_intent_id,cleanup_trade_intent_id,
          cleanup_phase,outcome,deadline_at,
          fencing_version,opening_exchange_order_row_id,cleanup_exchange_order_row_id,
          baseline_evidence_digest,attributed_fill_quantity,max_quantity,fill_attribution_digest,
          failure_code,final_evidence_digest,terminal_at,revoked_at)
          ON okx_demo_canary_lifecycles TO freqtrade;
    """.replace("SCHEMA_TOKEN", quoted_schema)))


def _add_canary_consent_handoff_boundary(connection: Connection) -> None:
    """Install the owner-only, consent-bound final attestation handoff."""

    Base.metadata.tables["okx_demo_canary_consent_handoffs"].create(
        bind=connection, checkfirst=True
    )
    schema_name, effective_schemas = connection.execute(
        text("SELECT current_schema(), current_schemas(false)")
    ).one()
    if not schema_name or list(effective_schemas or ()) != [schema_name]:
        raise SchemaMigrationBlocked(
            "Canary consent boundary requires exactly one effective schema"
        )
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(schema_name)
    connection.execute(text(
        "LOCK TABLE {}.okx_demo_canary_consent_handoffs "
        "IN SHARE ROW EXCLUSIVE MODE NOWAIT".format(quoted_schema)
    ))
    duplicate_active_source = connection.execute(text(
        "SELECT source_job_id FROM {}.okx_demo_canary_consent_handoffs "
        "WHERE status IN ('REQUESTED','FINALIZED','GRANT_ISSUED') "
        "GROUP BY source_job_id HAVING count(*)>1 ORDER BY source_job_id LIMIT 1"
        .format(quoted_schema)
    )).scalar_one_or_none()
    if duplicate_active_source is not None:
        raise SchemaMigrationBlocked(
            "v29 refuses multiple active consent handoffs for source job {}".format(
                duplicate_active_source
            )
        )
    connection.execute(text(
        "ALTER TABLE {}.okx_demo_canary_consent_handoffs DROP CONSTRAINT IF EXISTS "
        "okx_demo_canary_consent_source_unique".format(quoted_schema)
    ))
    connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "okx_demo_canary_consent_active_source_unique ON "
        "{}.okx_demo_canary_consent_handoffs(source_job_id) "
        "WHERE status IN ('REQUESTED','FINALIZED','GRANT_ISSUED')"
        .format(quoted_schema)
    ))
    connection.execute(text(
        "LOCK TABLE {}.research_jobs IN SHARE ROW EXCLUSIVE MODE NOWAIT".format(
            quoted_schema
        )
    ))
    successor_id = connection.execute(text(
        "SELECT id FROM {}.research_jobs WHERE id>22 "
        "AND operation='okx_demo.execution_chain_canary' ORDER BY id LIMIT 1"
        .format(quoted_schema)
    )).scalar_one_or_none()
    if successor_id is not None:
        raise SchemaMigrationBlocked(
            "v28 refuses existing successor canary source job {}".format(
                successor_id
            )
        )
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS {}.okx_demo_operator_consent_secrets ("
        "secret_id varchar(16) PRIMARY KEY CHECK (secret_id='ACTIVE'),"
        "hmac_key bytea NOT NULL CHECK (octet_length(hmac_key)=32),"
        "created_at timestamptz NOT NULL DEFAULT statement_timestamp())"
        .format(quoted_schema)
    ))
    _converge_operator_consent_proof_key(connection)
    ddl = r"""
    ALTER TABLE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
      OWNER TO freqtrade_ai_attestor;
    ALTER TABLE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
      DROP CONSTRAINT IF EXISTS okx_demo_canary_consent_handoffs_source_job_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_canary_consent_handoffs_reconciliation_run_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_canary_consent_handoffs_audit_job_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_canary_consent_handoffs_full_chain_run_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_canary_consent_handoffs_approval_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_canary_consent_handoffs_grant_id_fkey,
      ADD CONSTRAINT okx_demo_canary_consent_handoffs_source_job_id_fkey
        FOREIGN KEY(source_job_id) REFERENCES SCHEMA_TOKEN.research_jobs(id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
      ADD CONSTRAINT okx_demo_canary_consent_handoffs_reconciliation_run_id_fkey
        FOREIGN KEY(reconciliation_run_id) REFERENCES SCHEMA_TOKEN.reconciliation_runs(id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
      ADD CONSTRAINT okx_demo_canary_consent_handoffs_audit_job_id_fkey
        FOREIGN KEY(audit_job_id) REFERENCES SCHEMA_TOKEN.research_jobs(id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
      ADD CONSTRAINT okx_demo_canary_consent_handoffs_full_chain_run_id_fkey
        FOREIGN KEY(full_chain_run_id) REFERENCES SCHEMA_TOKEN.full_chain_runs(id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
      ADD CONSTRAINT okx_demo_canary_consent_handoffs_approval_id_fkey
        FOREIGN KEY(approval_id) REFERENCES SCHEMA_TOKEN.approved_executions(id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
      ADD CONSTRAINT okx_demo_canary_consent_handoffs_grant_id_fkey
        FOREIGN KEY(grant_id) REFERENCES SCHEMA_TOKEN.okx_demo_submission_grants(grant_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
    REVOKE ALL ON TABLE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
      FROM PUBLIC, freqtrade;
    GRANT SELECT ON TABLE SCHEMA_TOKEN.research_jobs TO freqtrade_ai_attestor;
    ALTER TABLE SCHEMA_TOKEN.okx_demo_submission_grants
      ADD COLUMN IF NOT EXISTS handoff_id varchar(32);
    DO $$
    DECLARE legacy record;
    BEGIN
      FOR legacy IN SELECT g.grant_id,g.approval_id,a.status approval_status,
          a.reserved_notional
        FROM SCHEMA_TOKEN.okx_demo_submission_grants g
        JOIN SCHEMA_TOKEN.approved_executions a ON a.id=g.approval_id
        WHERE g.handoff_id IS NULL
          AND g.provenance='CONTROLLED_CANARY_NON_PRODUCTION'
          AND g.status='ACTIVE' FOR UPDATE OF g,a LOOP
        IF legacy.approval_status='ACTIVE' THEN
          PERFORM pg_advisory_xact_lock(hashtext('OKX_DEMO-risk-budget'));
          UPDATE SCHEMA_TOKEN.approved_executions SET status='EXPIRED',
            evidence_snapshot=jsonb_set(evidence_snapshot::jsonb,
              '{invalidation_reason}',to_jsonb(
                'v28 migration revoked unbound controlled grant'::text),true)::json
            WHERE id=legacy.approval_id AND status='ACTIVE';
          UPDATE SCHEMA_TOKEN.full_chain_runs SET status='BLOCKED',
            terminal_reason='v28 migration revoked unbound controlled grant',
            completed_at=statement_timestamp()
            WHERE approved_execution_id=legacy.approval_id
              AND execution_target_id='OKX_DEMO';
          UPDATE SCHEMA_TOKEN.risk_budgets SET
            reserved_notional=greatest(0,reserved_notional-legacy.reserved_notional),
            approved_positions=greatest(0,approved_positions-1)
            WHERE execution_target_id='OKX_DEMO';
        END IF;
        UPDATE SCHEMA_TOKEN.okx_demo_submission_grants
          SET status='FAILED',consumed_at=statement_timestamp()
          WHERE grant_id=legacy.grant_id AND status='ACTIVE';
      END LOOP;
    END $$;
    ALTER TABLE SCHEMA_TOKEN.okx_demo_submission_grants
      DROP CONSTRAINT IF EXISTS okx_demo_submission_grants_handoff_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_submission_grants_handoff_id_key,
      ADD CONSTRAINT okx_demo_submission_grants_handoff_id_fkey
        FOREIGN KEY(handoff_id) REFERENCES SCHEMA_TOKEN.okx_demo_canary_consent_handoffs(handoff_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
      ADD CONSTRAINT okx_demo_submission_grants_handoff_id_key UNIQUE(handoff_id);

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.canonical_jsonb_text(p_value jsonb)
    RETURNS text LANGUAGE sql IMMUTABLE STRICT SET search_path=pg_catalog AS $$
      SELECT CASE jsonb_typeof(p_value)
        WHEN 'object' THEN '{'||COALESCE((SELECT string_agg(
          to_jsonb(key)::text||':'||SCHEMA_TOKEN.canonical_jsonb_text(value),',' ORDER BY key)
          FROM jsonb_each(p_value)),'')||'}'
        WHEN 'array' THEN '['||COALESCE((SELECT string_agg(
          SCHEMA_TOKEN.canonical_jsonb_text(value),',' ORDER BY ordinal)
          FROM jsonb_array_elements(p_value) WITH ORDINALITY item(value,ordinal)),'')||']'
        ELSE p_value::text END
    $$;
    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.canonical_decimal_text(p_value numeric)
    RETURNS text LANGUAGE sql IMMUTABLE STRICT SET search_path=pg_catalog AS $$
      SELECT CASE WHEN position('.' IN p_value::text)=0 THEN p_value::text
        ELSE rtrim(rtrim(p_value::text,'0'),'.') END
    $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.freeze_okx_demo_canary_source()
    RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    BEGIN
      IF TG_OP IN ('UPDATE','DELETE') AND OLD.id=22 THEN
        RAISE EXCEPTION 'immutable canary source job 22 cannot change';
      END IF;
      IF TG_OP IN ('INSERT','UPDATE') AND NEW.id>22
         AND NEW.operation='okx_demo.execution_chain_canary' THEN
        RAISE EXCEPTION 'successor canary source is forbidden';
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$;
    DROP TRIGGER IF EXISTS freeze_okx_demo_canary_source ON SCHEMA_TOKEN.research_jobs;
    CREATE TRIGGER freeze_okx_demo_canary_source
      BEFORE INSERT OR UPDATE OR DELETE ON SCHEMA_TOKEN.research_jobs
      FOR EACH ROW EXECUTE FUNCTION SCHEMA_TOKEN.freeze_okx_demo_canary_source();

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.require_okx_demo_grant_handoff()
    RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    BEGIN
      IF TG_OP='UPDATE' AND OLD.handoff_id IS DISTINCT FROM NEW.handoff_id THEN
        RAISE EXCEPTION 'submission grant handoff is immutable';
      END IF;
      IF TG_OP='INSERT' AND (NEW.provenance<>'CONTROLLED_CANARY_NON_PRODUCTION'
         OR NEW.handoff_id IS NULL OR NOT EXISTS(
           SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs h
           WHERE h.handoff_id=NEW.handoff_id
             AND h.approval_id=NEW.approval_id
             AND h.reconciliation_run_id=NEW.reconciliation_run_id
             AND (h.status='FINALIZED' OR (
               h.grant_id=NEW.grant_id AND (
                 (NEW.status='ACTIVE' AND h.status='GRANT_ISSUED') OR
                 (NEW.status='ACTIVE' AND h.status='CONSUMED' AND EXISTS(
                    SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants current_grant
                    WHERE current_grant.grant_id=NEW.grant_id
                      AND current_grant.status='CONSUMED')) OR
                 (NEW.status='CONSUMED' AND h.status='CONSUMED') OR
                 (NEW.status='EXPIRED' AND h.status='EXPIRED') OR
                 (NEW.status='FAILED' AND h.status IN ('FAILED','REVOKED'))
               )
             )))) THEN
        RAISE EXCEPTION 'controlled canary grant lacks exact finalized handoff';
      END IF;
      RETURN NEW;
    END $$;
    DROP TRIGGER IF EXISTS require_okx_demo_grant_handoff
      ON SCHEMA_TOKEN.okx_demo_submission_grants;
    CREATE CONSTRAINT TRIGGER require_okx_demo_grant_handoff
      AFTER INSERT OR UPDATE ON SCHEMA_TOKEN.okx_demo_submission_grants
      DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION SCHEMA_TOKEN.require_okx_demo_grant_handoff();

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.request_okx_demo_canary_consent(
      p_idempotency_digest text, p_nonce text, p_payload text, p_proof text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE source SCHEMA_TOKEN.research_jobs%ROWTYPE;
            existing SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            new_id text; v_consent_digest text; payload_digest text; proof_key bytea;
    BEGIN
      IF NOT pg_try_advisory_xact_lock(5067747289570038601) THEN
        RAISE EXCEPTION 'controlled canary consent request lock is busy';
      END IF;
      IF p_idempotency_digest !~ '^[0-9a-f]{64}$'
         OR p_nonce !~ '^[0-9a-f]{64}$' OR p_proof !~ '^[0-9a-f]{64}$'
         OR p_payload::jsonb IS DISTINCT FROM jsonb_build_object(
           'authorization','once',
           'consent_policy','immutable-job-22-final-attestation-v1',
           'execution_target','OKX_DEMO',
           'idempotency_key_digest',p_idempotency_digest,
           'instrument_id','BTC-USDT-SWAP','max_notional','20',
           'operation','okx-demo-canary-consent-finalize',
           'source_ancestry','[15,16,17,18,19,20,21,22]'::jsonb,
           'source_job_id',22) THEN
        RAISE EXCEPTION 'invalid controlled canary consent identity';
      END IF;
      SELECT hmac_key INTO proof_key
        FROM SCHEMA_TOKEN.okx_demo_operator_consent_secrets
        WHERE secret_id='ACTIVE';
      IF proof_key IS NULL OR NOT public.hmac(
           convert_to(p_payload||'|'||p_nonce,'UTF8'),proof_key,'sha256')
           = decode(p_proof,'hex') THEN
        RAISE EXCEPTION 'invalid controlled canary operator proof';
      END IF;
      payload_digest:=encode(public.digest(convert_to(p_payload,'UTF8'),'sha256'),'hex');
      v_consent_digest:=encode(public.digest(convert_to(
        payload_digest||'|'||p_nonce||'|'||p_proof,'UTF8'),'sha256'),'hex');
      SELECT * INTO existing
        FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE idempotency_key_digest=p_idempotency_digest
           OR consent_digest=v_consent_digest OR consent_nonce=p_nonce FOR UPDATE;
      IF FOUND THEN
        IF existing.idempotency_key_digest IS DISTINCT FROM p_idempotency_digest THEN
          RAISE EXCEPTION 'controlled canary consent identity conflict';
        END IF;
        RETURN jsonb_build_object('handoff_id',existing.handoff_id,
          'status',existing.status,'source_job_id',existing.source_job_id,
          'consent_deadline_at',existing.consent_deadline_at);
      END IF;
      IF EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
          WHERE source_job_id=22
            AND status IN ('REQUESTED','FINALIZED','GRANT_ISSUED')) THEN
        RAISE EXCEPTION 'active controlled canary consent already exists';
      END IF;
      SELECT * INTO source FROM SCHEMA_TOKEN.research_jobs
       WHERE id=22
         AND execution_scope_id='LOCAL_DRY_RUN'
         AND operation='okx_demo.execution_chain_canary'
         AND status='SUCCESS' AND stage='CANARY_SNAPSHOTS_READY'
         AND request_payload::jsonb->>'provenance'='CONTROLLED_CANARY_NON_PRODUCTION'
         AND request_payload::jsonb->>'execution_target'='OKX_DEMO'
         AND request_payload::jsonb->>'instrument_id'='BTC-USDT-SWAP'
         AND request_payload::jsonb->>'entry_kind'='FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY'
         AND request_payload::jsonb->>'recovery_of_job_id'='21'
         AND request_payload::jsonb->'supersedes_job_ids'='[15,16,17,18,19,20,21]'::jsonb;
      IF NOT FOUND THEN RAISE EXCEPTION 'immutable final-expiry source job 22 is unavailable'; END IF;
      IF EXISTS(SELECT 1 FROM SCHEMA_TOKEN.research_jobs
          WHERE id>22 AND operation='okx_demo.execution_chain_canary') THEN
        RAISE EXCEPTION 'successor canary source already exists';
      END IF;
      IF EXISTS (SELECT 1 FROM SCHEMA_TOKEN.trade_intents
          WHERE execution_target_id='OKX_DEMO'
            AND request_snapshot::jsonb->>'provenance'='CONTROLLED_CANARY_NON_PRODUCTION')
         OR EXISTS (SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants)
         OR EXISTS (SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
             WHERE execution_target_id='OKX_DEMO')
         OR EXISTS (SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles) THEN
        RAISE EXCEPTION 'controlled canary execution boundary is occupied';
      END IF;
      IF EXISTS (
        SELECT 1 FROM jsonb_each(source.evidence_snapshot::jsonb->'snapshot_evidence') e
        WHERE (e.value->>'expires_at')::timestamptz > statement_timestamp()
      ) OR (SELECT count(*) FROM jsonb_object_keys(COALESCE(
        source.evidence_snapshot::jsonb->'snapshot_evidence','{}'::jsonb)))<>3 THEN
        RAISE EXCEPTION 'final-expiry source is not completely expired';
      END IF;
      new_id := left(encode(public.digest(convert_to(
        p_idempotency_digest||'|'||v_consent_digest||'|22','UTF8'),'sha256'),'hex'),32);
      INSERT INTO SCHEMA_TOKEN.okx_demo_canary_consent_handoffs(
        handoff_id,execution_target_id,source_job_id,source_ancestry,source_fingerprint,
        idempotency_key_digest,consent_nonce,consent_payload_digest,consent_digest,
        provenance,instrument_id,
        max_notional,status,snapshot_binding,consented_at,consent_deadline_at,
        created_at,updated_at)
      VALUES(new_id,'OKX_DEMO',22,'[15,16,17,18,19,20,21,22]'::json,
        encode(public.digest(convert_to(jsonb_build_object('id',source.id,
          'request_hash',source.request_hash,'request_payload',source.request_payload::jsonb,
          'status',source.status,'stage',source.stage,
          'evidence_snapshot',source.evidence_snapshot::jsonb,'completed_at',source.completed_at)::text,
          'UTF8'),'sha256'),'hex'),
        p_idempotency_digest,p_nonce,payload_digest,v_consent_digest,
        'CONTROLLED_CANARY_NON_PRODUCTION',
        'BTC-USDT-SWAP',20,'REQUESTED','{}'::json,statement_timestamp(),
        statement_timestamp()+interval '60 seconds',statement_timestamp(),statement_timestamp());
      RETURN jsonb_build_object('handoff_id',new_id,'status','REQUESTED',
        'source_job_id',22,'consent_deadline_at',statement_timestamp()+interval '60 seconds');
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret()
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE proof_key bytea;
    BEGIN
      SELECT hmac_key INTO proof_key
        FROM SCHEMA_TOKEN.okx_demo_operator_consent_secrets
        WHERE secret_id='ACTIVE';
      IF NOT FOUND OR octet_length(proof_key)<>32 THEN
        RAISE EXCEPTION 'active operator consent proof key unavailable';
      END IF;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.pending_okx_demo_canary_consent()
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            predecessor SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
    BEGIN
      PERFORM SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret();
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
       WHERE status='REQUESTED' ORDER BY consented_at,handoff_id LIMIT 1 FOR UPDATE;
      IF NOT FOUND THEN RETURN NULL; END IF;
      IF h.consent_deadline_at<=statement_timestamp()+interval '15 seconds' THEN
        UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET status='EXPIRED',
          failure_code='INSUFFICIENT_CAPTURE_BUDGET',updated_at=statement_timestamp()
          WHERE handoff_id=h.handoff_id;
        RETURN jsonb_build_object('handoff_id',h.handoff_id,'status','EXPIRED');
      END IF;
      RETURN jsonb_build_object('handoff_id',h.handoff_id,
        'status','REQUESTED',
        'source_job_id',h.source_job_id,'source_ancestry',h.source_ancestry::jsonb,
        'idempotency_key_digest',h.idempotency_key_digest,
        'instrument_id',h.instrument_id,'max_notional',h.max_notional::text,
        'consent_deadline_at',h.consent_deadline_at);
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.fail_requested_okx_demo_canary_consent(
      p_handoff_id text,p_failure_stage text,p_failure_category text)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
    BEGIN
      IF p_failure_stage NOT IN (
           'SAFETY_PRECHECK','FRESH_RECONCILIATION','DB_CLOCK',
           'HANDOFF_RECHECK','RECONCILIATION_VALIDATE','ATTESTATION_CAPTURE',
           'ATTESTATION_BINDING','HANDOFF_CLAIM','LINEAGE_PERSIST',
           'SAFETY_FINAL','HANDOFF_FINALIZE')
         OR p_failure_category NOT IN (
           'DATABASE','EXCHANGE_READ','SAFETY','UNEXPECTED') THEN
        RAISE EXCEPTION 'invalid consent capture failure identity';
      END IF;
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=p_handoff_id FOR UPDATE;
      IF NOT FOUND OR h.status<>'REQUESTED' THEN RETURN FALSE; END IF;
      IF EXISTS (SELECT 1 FROM SCHEMA_TOKEN.trade_intents
          WHERE execution_target_id='OKX_DEMO'
            AND request_snapshot::jsonb->>'provenance'='CONTROLLED_CANARY_NON_PRODUCTION')
         OR EXISTS (SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants)
         OR EXISTS (SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
             WHERE execution_target_id='OKX_DEMO')
         OR EXISTS (SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles) THEN
        RAISE EXCEPTION 'consent capture failure boundary is occupied';
      END IF;
      UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        SET status='EXPIRED',
            failure_code='CAPTURE_'||p_failure_stage||'_'||p_failure_category,
            updated_at=statement_timestamp()
        WHERE handoff_id=h.handoff_id AND status='REQUESTED';
      RETURN FOUND;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.claim_okx_demo_canary_consent(
      p_handoff_id text,p_runtime_id text,p_reconciliation_run_id bigint,p_binding jsonb)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            r SCHEMA_TOKEN.reconciliation_runs%ROWTYPE;
            s SCHEMA_TOKEN.okx_demo_reconciliation_states%ROWTYPE;
            instrument SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            market SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            account SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
    BEGIN
      PERFORM SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret();
      IF NOT pg_try_advisory_xact_lock(5067747289570038600) THEN
        RAISE EXCEPTION 'controlled canary coordination lock is busy';
      END IF;
      IF p_runtime_id !~ '^[A-Za-z0-9]{8,64}$' OR jsonb_typeof(p_binding)<>'object' THEN
        RAISE EXCEPTION 'invalid controlled canary runtime claim';
      END IF;
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=p_handoff_id FOR UPDATE;
      IF NOT FOUND OR h.status<>'REQUESTED'
         OR h.consent_deadline_at<=statement_timestamp()+(
              CASE WHEN h.supersedes_handoff_id IS NULL
                THEN interval '5 seconds' ELSE interval '1 second' END)
         OR h.source_job_id<>22 OR h.source_ancestry::jsonb<>'[15,16,17,18,19,20,21,22]'::jsonb THEN
        RAISE EXCEPTION 'controlled canary consent is not claimable';
      END IF;
      SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs WHERE id=p_reconciliation_run_id;
      SELECT * INTO s FROM SCHEMA_TOKEN.okx_demo_reconciliation_states
        WHERE execution_target_id='OKX_DEMO' FOR UPDATE;
      IF r.id IS NULL OR s.database_id IS NULL OR s.last_reconciliation_run_id<>r.id
         OR s.status NOT IN ('RECONCILED','RECOVERED') OR s.opening_frozen
         OR r.execution_target_id<>'OKX_DEMO' OR r.status NOT IN ('RECONCILED','RECOVERED')
         OR r.artifact_status<>'READY' OR r.source_type<>'api_aggregate' OR NOT r.core_data
         OR r.completed_at<statement_timestamp()-interval '30 seconds'
         OR r.authoritative_observed_at<statement_timestamp()-interval '30 seconds'
         OR r.database_ids::jsonb->'order_snapshots'<>'[]'::jsonb
         OR r.database_ids::jsonb->'position_snapshots'<>'[]'::jsonb THEN
        RAISE EXCEPTION 'fresh empty reconciliation is required';
      END IF;
      SELECT * INTO instrument FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
       WHERE database_id=(p_binding#>>'{instrument,database_id}')::bigint;
      SELECT * INTO market FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
       WHERE database_id=(p_binding#>>'{market,database_id}')::bigint;
      SELECT * INTO account FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
       WHERE database_id=(p_binding#>>'{account,database_id}')::bigint;
      IF instrument.database_id IS NULL OR market.database_id IS NULL OR account.database_id IS NULL
         OR instrument.kind<>'instrument' OR market.kind<>'market' OR account.kind<>'account'
         OR instrument.execution_target_id<>'OKX_DEMO' OR market.execution_target_id<>'OKX_DEMO'
         OR account.execution_target_id<>'OKX_DEMO'
         OR instrument.snapshot_id IS DISTINCT FROM p_binding#>>'{instrument,snapshot_id}'
         OR market.snapshot_id IS DISTINCT FROM p_binding#>>'{market,snapshot_id}'
         OR account.snapshot_id IS DISTINCT FROM p_binding#>>'{account,snapshot_id}'
         OR instrument.digest IS DISTINCT FROM p_binding#>>'{instrument,digest}'
         OR market.digest IS DISTINCT FROM p_binding#>>'{market,digest}'
         OR account.digest IS DISTINCT FROM p_binding#>>'{account,digest}'
         OR instrument.digest IS DISTINCT FROM encode(public.digest(convert_to(
              SCHEMA_TOKEN.canonical_jsonb_text(instrument.content_json::jsonb),'UTF8'),'sha256'),'hex')
         OR market.digest IS DISTINCT FROM encode(public.digest(convert_to(
              SCHEMA_TOKEN.canonical_jsonb_text(market.content_json::jsonb),'UTF8'),'sha256'),'hex')
         OR account.digest IS DISTINCT FROM encode(public.digest(convert_to(
              SCHEMA_TOKEN.canonical_jsonb_text(account.content_json::jsonb),'UTF8'),'sha256'),'hex')
         OR instrument.snapshot_id IS DISTINCT FROM instrument.kind||':'||left(encode(public.digest(convert_to(
              SCHEMA_TOKEN.canonical_jsonb_text(jsonb_build_object(
                'digest',instrument.digest,'fingerprint',instrument.attestation_fingerprint_sha256,
                'kind',instrument.kind,'observed_at',to_char(instrument.observed_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS')||
                  CASE WHEN extract(microseconds FROM instrument.observed_at)::integer%1000000=0 THEN '' ELSE '.'||to_char(instrument.observed_at AT TIME ZONE 'UTC','US') END||'+00:00',
                'session_id',instrument.attested_session_id)),'UTF8'),'sha256'),'hex'),48)
         OR market.snapshot_id IS DISTINCT FROM market.kind||':'||left(encode(public.digest(convert_to(
              SCHEMA_TOKEN.canonical_jsonb_text(jsonb_build_object(
                'digest',market.digest,'fingerprint',market.attestation_fingerprint_sha256,
                'kind',market.kind,'observed_at',to_char(market.observed_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS')||
                  CASE WHEN extract(microseconds FROM market.observed_at)::integer%1000000=0 THEN '' ELSE '.'||to_char(market.observed_at AT TIME ZONE 'UTC','US') END||'+00:00',
                'session_id',market.attested_session_id)),'UTF8'),'sha256'),'hex'),48)
         OR account.snapshot_id IS DISTINCT FROM account.kind||':'||left(encode(public.digest(convert_to(
              SCHEMA_TOKEN.canonical_jsonb_text(jsonb_build_object(
                'digest',account.digest,'fingerprint',account.attestation_fingerprint_sha256,
                'kind',account.kind,'observed_at',to_char(account.observed_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS')||
                  CASE WHEN extract(microseconds FROM account.observed_at)::integer%1000000=0 THEN '' ELSE '.'||to_char(account.observed_at AT TIME ZONE 'UTC','US') END||'+00:00',
                'session_id',account.attested_session_id)),'UTF8'),'sha256'),'hex'),48)
         OR instrument.attested_session_id IS DISTINCT FROM market.attested_session_id
         OR instrument.attested_session_id IS DISTINCT FROM account.attested_session_id
         OR (h.supersedes_handoff_id IS NOT NULL AND (
              instrument.observed_at<h.consented_at OR market.observed_at<h.consented_at
              OR account.observed_at<h.consented_at))
         OR instrument.expires_at<=statement_timestamp() OR market.expires_at<=statement_timestamp()
         OR account.expires_at<=statement_timestamp()
         OR NOT EXISTS (SELECT 1 FROM SCHEMA_TOKEN.okx_demo_attested_sessions a
              WHERE a.session_id=instrument.attested_session_id AND a.execution_target_id='OKX_DEMO'
                AND a.revoked_at IS NULL AND a.expires_at>statement_timestamp()) THEN
        RAISE EXCEPTION 'exact fresh attested snapshot binding failed';
      END IF;
      RETURN jsonb_build_object('handoff_id',h.handoff_id,
        'idempotency_key_digest',h.idempotency_key_digest,'source_job_id',h.source_job_id);
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.finalize_okx_demo_canary_consent(
      p_handoff_id text,p_runtime_id text,p_audit_job_id bigint,
      p_full_chain_run_id bigint,p_approval_id bigint,
      p_reconciliation_run_id bigint,p_binding jsonb)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            a SCHEMA_TOKEN.approved_executions%ROWTYPE;
            j SCHEMA_TOKEN.research_jobs%ROWTYPE;
            instrument SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            market SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            account SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            v_bundle_digest text;
    BEGIN
      PERFORM SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret();
      PERFORM SCHEMA_TOKEN.claim_okx_demo_canary_consent(
        p_handoff_id,p_runtime_id,p_reconciliation_run_id,p_binding);
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=p_handoff_id FOR UPDATE;
      SELECT * INTO a FROM SCHEMA_TOKEN.approved_executions WHERE id=p_approval_id;
      SELECT * INTO j FROM SCHEMA_TOKEN.research_jobs WHERE id=p_audit_job_id;
      SELECT * INTO instrument FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(p_binding#>>'{instrument,database_id}')::bigint;
      SELECT * INTO market FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(p_binding#>>'{market,database_id}')::bigint;
      SELECT * INTO account FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(p_binding#>>'{account,database_id}')::bigint;
      v_bundle_digest:=encode(public.digest(convert_to(
        SCHEMA_TOKEN.canonical_jsonb_text(p_binding),'UTF8'),'sha256'),'hex');
      IF h.status<>'REQUESTED' OR h.supersedes_handoff_id IS NOT NULL
         OR h.consent_deadline_at<=statement_timestamp()+interval '5 seconds'
         OR a.id IS NULL OR j.id IS NULL
         OR j.id=22 OR j.operation<>'okx_demo_canary_consent_execution_audit'
         OR a.execution_target_id<>'OKX_DEMO' OR a.status<>'ACTIVE'
         OR a.instrument_snapshot_id IS DISTINCT FROM p_binding#>>'{instrument,snapshot_id}'
         OR a.market_snapshot_id IS DISTINCT FROM p_binding#>>'{market,snapshot_id}'
         OR a.account_snapshot_id IS DISTINCT FROM p_binding#>>'{account,snapshot_id}'
         OR NOT EXISTS (SELECT 1 FROM SCHEMA_TOKEN.full_chain_runs f
              WHERE f.id=p_full_chain_run_id AND f.research_job_id=j.id
                AND f.approved_execution_id=a.id AND f.execution_target_id='OKX_DEMO')
         OR NOT EXISTS (SELECT 1 FROM SCHEMA_TOKEN.research_jobs source
              WHERE source.id=22 AND source.status='SUCCESS' AND source.stage='CANARY_SNAPSHOTS_READY'
                AND source.request_payload::jsonb->>'entry_kind'='FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY'
                AND h.source_fingerprint=encode(public.digest(convert_to(jsonb_build_object(
                  'id',source.id,'request_hash',source.request_hash,
                  'request_payload',source.request_payload::jsonb,'status',source.status,
                  'stage',source.stage,'evidence_snapshot',source.evidence_snapshot::jsonb,
                  'completed_at',source.completed_at)::text,'UTF8'),'sha256'),'hex')) THEN
        RAISE EXCEPTION 'controlled canary consent finalization mismatch';
      END IF;
      UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET status='FINALIZED',
        runtime_instance_id=p_runtime_id,reconciliation_run_id=p_reconciliation_run_id,
        attested_session_id=instrument.attested_session_id,snapshot_binding=p_binding::json,
        bundle_digest=v_bundle_digest,
        bundle_observed_at=GREATEST(instrument.observed_at,market.observed_at,account.observed_at),
        bundle_expires_at=LEAST(instrument.expires_at,market.expires_at,account.expires_at),
        audit_job_id=j.id,full_chain_run_id=p_full_chain_run_id,approval_id=a.id,
        finalized_at=statement_timestamp(),updated_at=statement_timestamp()
        WHERE handoff_id=h.handoff_id;
      RETURN TRUE;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.expire_okx_demo_canary_approval(
      p_approval_id bigint,p_reason text)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE a SCHEMA_TOKEN.approved_executions%ROWTYPE;
    BEGIN
      IF p_reason IS NULL OR length(p_reason)>80 THEN
        RAISE EXCEPTION 'invalid canary approval terminal reason';
      END IF;
      PERFORM pg_advisory_xact_lock(hashtext('OKX_DEMO-risk-budget'));
      SELECT * INTO a FROM SCHEMA_TOKEN.approved_executions
        WHERE id=p_approval_id FOR UPDATE;
      IF NOT FOUND OR a.status<>'ACTIVE' THEN RETURN FALSE; END IF;
      UPDATE SCHEMA_TOKEN.approved_executions SET status='EXPIRED',
        evidence_snapshot=jsonb_set(evidence_snapshot::jsonb,'{invalidation_reason}',
          to_jsonb(p_reason),true)::json WHERE id=a.id;
      UPDATE SCHEMA_TOKEN.full_chain_runs SET status='BLOCKED',terminal_reason=p_reason,
        completed_at=statement_timestamp()
        WHERE approved_execution_id=a.id AND execution_target_id='OKX_DEMO';
      UPDATE SCHEMA_TOKEN.risk_budgets SET
        reserved_notional=greatest(0,reserved_notional-a.reserved_notional),
        approved_positions=greatest(0,approved_positions-1)
        WHERE execution_target_id='OKX_DEMO';
      RETURN TRUE;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.finalized_okx_demo_canary_consent(
      p_runtime_id text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            a SCHEMA_TOKEN.approved_executions%ROWTYPE;
            g SCHEMA_TOKEN.okx_demo_submission_grants%ROWTYPE;
            r SCHEMA_TOKEN.reconciliation_runs%ROWTYPE;
            s SCHEMA_TOKEN.okx_demo_reconciliation_states%ROWTYPE;
            invalid_finalized boolean;
    BEGIN
      PERFORM SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret();
      IF NOT pg_try_advisory_xact_lock(5067747289570038600) THEN
        RAISE EXCEPTION 'controlled canary coordination lock is busy';
      END IF;
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE status='GRANT_ISSUED' ORDER BY finalized_at LIMIT 1 FOR UPDATE;
      IF FOUND THEN
        SELECT * INTO g FROM SCHEMA_TOKEN.okx_demo_submission_grants
          WHERE grant_id=h.grant_id FOR UPDATE;
        IF g.status='CONSUMED' AND EXISTS(
          SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles l
          JOIN SCHEMA_TOKEN.okx_order_write_attempts w
            ON w.exchange_order_row_id=l.opening_exchange_order_row_id
           AND w.approval_id=l.opening_approval_id AND w.operation='PLACE'
           AND w.attempt_count=1
          WHERE l.submission_grant_id=g.grant_id AND l.cleanup_phase='OPENING_SUBMITTED') THEN
          UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET status='CONSUMED',
            updated_at=statement_timestamp() WHERE handoff_id=h.handoff_id;
        ELSIF g.status IN ('FAILED','EXPIRED') OR g.expires_at<=statement_timestamp()
           OR h.consent_deadline_at<=statement_timestamp()
           OR h.runtime_instance_id IS DISTINCT FROM p_runtime_id THEN
          UPDATE SCHEMA_TOKEN.okx_demo_submission_grants SET status=CASE
              WHEN g.status='ACTIVE' AND g.expires_at<=statement_timestamp() THEN 'EXPIRED'
              WHEN g.status='ACTIVE' THEN 'FAILED' ELSE g.status END,
            consumed_at=COALESCE(consumed_at,statement_timestamp())
            WHERE grant_id=g.grant_id AND status='ACTIVE';
          PERFORM SCHEMA_TOKEN.expire_okx_demo_canary_approval(
            h.approval_id,'consent grant terminalized before exact prepare');
          UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET
            status=CASE WHEN g.expires_at<=statement_timestamp() THEN 'EXPIRED' ELSE 'REVOKED' END,
            failure_code='GRANT_RECONCILIATION_TERMINAL',revoked_at=statement_timestamp(),
            updated_at=statement_timestamp() WHERE handoff_id=h.handoff_id;
        END IF;
      END IF;
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE status='FINALIZED' ORDER BY finalized_at,handoff_id LIMIT 1 FOR UPDATE;
      IF NOT FOUND THEN RETURN NULL; END IF;
      SELECT * INTO a FROM SCHEMA_TOKEN.approved_executions WHERE id=h.approval_id FOR UPDATE;
      SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs WHERE id=h.reconciliation_run_id;
      SELECT * INTO s FROM SCHEMA_TOKEN.okx_demo_reconciliation_states
        WHERE execution_target_id='OKX_DEMO';
      invalid_finalized := a.id IS NULL OR a.status<>'ACTIVE'
        OR a.expires_at<=statement_timestamp()+interval '5 seconds'
        OR h.consent_deadline_at<=statement_timestamp()+interval '5 seconds'
        OR h.bundle_expires_at<=statement_timestamp()+interval '5 seconds'
        OR r.id IS NULL OR s.last_reconciliation_run_id<>r.id OR s.opening_frozen
        OR r.completed_at<statement_timestamp()-interval '30 seconds'
        OR r.authoritative_observed_at<statement_timestamp()-interval '30 seconds'
        OR r.database_ids::jsonb->'order_snapshots'<>'[]'::jsonb
        OR r.database_ids::jsonb->'position_snapshots'<>'[]'::jsonb;
      IF invalid_finalized THEN
        PERFORM SCHEMA_TOKEN.expire_okx_demo_canary_approval(
          h.approval_id,'finalized consent expired before grant');
        UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET status='EXPIRED',
          failure_code='FINALIZED_EVIDENCE_EXPIRED',revoked_at=statement_timestamp(),
          updated_at=statement_timestamp() WHERE handoff_id=h.handoff_id;
        RETURN jsonb_build_object('status','EXPIRED','handoff_id',h.handoff_id);
      END IF;
      UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        SET runtime_instance_id=p_runtime_id,updated_at=statement_timestamp()
        WHERE handoff_id=h.handoff_id;
      RETURN jsonb_build_object('status','FINALIZED','handoff_id',h.handoff_id,
        'approval_id',a.id,'canonical_hash',a.canonical_hash,
        'policy_digest',a.policy_digest,'approved_payload_hash',a.approved_payload_hash,
        'client_order_id',a.client_order_id);
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.issue_okx_demo_submission_grant(p_payload jsonb)
    RETURNS varchar LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE a SCHEMA_TOKEN.approved_executions%ROWTYPE;
            i SCHEMA_TOKEN.trade_intents%ROWTYPE;
            d SCHEMA_TOKEN.risk_decisions%ROWTYPE;
            h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            r SCHEMA_TOKEN.reconciliation_runs%ROWTYPE;
            s SCHEMA_TOKEN.okx_demo_reconciliation_states%ROWTYPE;
            instrument SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            market SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            account SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            new_id text; expires timestamptz; computed_notional numeric;
            computed_request_digest text;
    BEGIN
      PERFORM SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret();
      IF NOT pg_try_advisory_xact_lock(5067747289570038600) THEN
        RAISE EXCEPTION 'controlled canary coordination lock is busy';
      END IF;
      SELECT * INTO a FROM SCHEMA_TOKEN.approved_executions
        WHERE id=(p_payload->>'approval_id')::bigint;
      SELECT * INTO i FROM SCHEMA_TOKEN.trade_intents WHERE id=a.trade_intent_id;
      SELECT * INTO d FROM SCHEMA_TOKEN.risk_decisions WHERE id=a.risk_decision_id;
      IF NOT p_payload ? 'handoff_id' OR NOT p_payload ? 'runtime_instance_id' THEN
        RAISE EXCEPTION 'all controlled canary grants require finalized consent';
      END IF;
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=p_payload->>'handoff_id' FOR UPDATE;
      SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs WHERE id=h.reconciliation_run_id;
      SELECT * INTO s FROM SCHEMA_TOKEN.okx_demo_reconciliation_states
        WHERE execution_target_id='OKX_DEMO';
      SELECT * INTO instrument FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(h.snapshot_binding::jsonb#>>'{instrument,database_id}')::bigint;
      SELECT * INTO market FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(h.snapshot_binding::jsonb#>>'{market,database_id}')::bigint;
      SELECT * INTO account FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(h.snapshot_binding::jsonb#>>'{account,database_id}')::bigint;
      new_id:=p_payload->>'grant_id'; expires:=(p_payload->>'expires_at')::timestamptz;
      computed_notional:=i.quantity*(instrument.content_json::jsonb->>'ctVal')::numeric*
        GREATEST((market.content_json::jsonb->>'reference_price')::numeric,
          (market.content_json::jsonb#>>'{bbo,ask_price}')::numeric,
          COALESCE(i.limit_price,0));
      computed_request_digest:=encode(public.digest(convert_to(
        SCHEMA_TOKEN.canonical_jsonb_text(jsonb_build_object(
          'approval_id',a.id,'approved_payload_hash',a.approved_payload_hash,
          'canary_notional',SCHEMA_TOKEN.canonical_decimal_text(computed_notional),
          'canary_quantity',SCHEMA_TOKEN.canonical_decimal_text(i.quantity),
          'canonical_hash',a.canonical_hash,'client_order_id',a.client_order_id,
          'instrument_id',i.instrument_id,'policy_digest',a.policy_digest,
          'provenance','CONTROLLED_CANARY_NON_PRODUCTION',
          'reconciliation_run_id',r.id)),'UTF8'),'sha256'),'hex');
      IF new_id!~'^[0-9a-f]{32}$' OR a.id IS NULL OR i.id IS NULL
         OR a.status<>'ACTIVE' OR i.status<>'APPROVED' OR a.execution_target_id<>'OKX_DEMO'
         OR i.execution_target_id<>'OKX_DEMO' OR i.instrument_id<>'BTC-USDT-SWAP'
         OR i.reduce_only OR a.order_submission_authorized OR NOT a.claim_required
         OR h.status<>'FINALIZED' OR h.supersedes_handoff_id IS NOT NULL
         OR h.approval_id<>a.id
         OR h.runtime_instance_id IS DISTINCT FROM p_payload->>'runtime_instance_id'
         OR h.finalized_at>=transaction_timestamp()
         OR h.consent_deadline_at<=statement_timestamp()+interval '5 seconds'
         OR h.bundle_expires_at<=statement_timestamp()+interval '5 seconds'
         OR r.id IS NULL OR r.id<>h.reconciliation_run_id
         OR r.status NOT IN ('RECONCILED','RECOVERED') OR r.artifact_status<>'READY'
         OR r.source_type<>'api_aggregate' OR NOT r.core_data
         OR r.completed_at<statement_timestamp()-interval '30 seconds'
         OR r.authoritative_observed_at<statement_timestamp()-interval '30 seconds'
         OR r.database_ids::jsonb->'order_snapshots'<>'[]'::jsonb
         OR r.database_ids::jsonb->'position_snapshots'<>'[]'::jsonb
         OR s.last_reconciliation_run_id<>r.id OR s.opening_frozen
         OR instrument.expires_at<=statement_timestamp()+interval '5 seconds'
         OR market.expires_at<=statement_timestamp()+interval '5 seconds'
         OR account.expires_at<=statement_timestamp()+interval '5 seconds'
         OR a.instrument_snapshot_id<>instrument.snapshot_id
         OR a.market_snapshot_id<>market.snapshot_id
         OR a.account_snapshot_id<>account.snapshot_id
         OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.research_jobs j
              WHERE j.id=h.audit_job_id
                AND j.operation='okx_demo_canary_consent_execution_audit'
                AND j.status='SUCCESS')
         OR expires<=statement_timestamp() OR expires>a.expires_at OR expires>i.expires_at
         OR (p_payload->>'canary_notional')::numeric>20
         OR (p_payload->>'canary_notional')::numeric IS DISTINCT FROM computed_notional
         OR computed_notional IS DISTINCT FROM a.reserved_notional
         OR SCHEMA_TOKEN.canonical_decimal_text(
              (d.evidence_snapshot::jsonb->>'notional')::numeric)
              IS DISTINCT FROM SCHEMA_TOKEN.canonical_decimal_text(computed_notional)
         OR p_payload->>'canonical_hash' IS DISTINCT FROM a.canonical_hash
         OR p_payload->>'policy_digest' IS DISTINCT FROM a.policy_digest
         OR p_payload->>'approved_payload_hash' IS DISTINCT FROM a.approved_payload_hash
         OR p_payload->>'client_order_id' IS DISTINCT FROM a.client_order_id
         OR p_payload->>'instrument_id' IS DISTINCT FROM i.instrument_id
         OR (p_payload->>'canary_quantity')::numeric IS DISTINCT FROM i.quantity
         OR p_payload->>'request_digest' IS DISTINCT FROM computed_request_digest THEN
        RAISE EXCEPTION 'unsafe one-shot submission grant payload request=% expected=% notional=% payload=% reserved=% decision=%',
          p_payload->>'request_digest',computed_request_digest,computed_notional,
          p_payload->>'canary_notional',a.reserved_notional,
          d.evidence_snapshot::jsonb->>'notional';
      END IF;
      INSERT INTO SCHEMA_TOKEN.okx_demo_submission_grants(
        grant_id,handoff_id,execution_target_id,approval_id,reconciliation_run_id,canonical_hash,
        policy_digest,approved_payload_hash,client_order_id,instrument_id,canary_quantity,
        canary_notional,request_digest,provenance,status,writer_instance_id,issued_at,expires_at)
      VALUES(new_id,h.handoff_id,'OKX_DEMO',a.id,r.id,
        a.canonical_hash,a.policy_digest,a.approved_payload_hash,a.client_order_id,i.instrument_id,
        (p_payload->>'canary_quantity')::numeric,(p_payload->>'canary_notional')::numeric,
        p_payload->>'request_digest','CONTROLLED_CANARY_NON_PRODUCTION','ACTIVE',
        NULL,statement_timestamp(),expires);
      UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET status='GRANT_ISSUED',
        grant_id=new_id,updated_at=statement_timestamp() WHERE handoff_id=h.handoff_id;
      RETURN new_id;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.revoke_restarted_okx_demo_canary_grant(
      p_grant_id text,p_runtime_id text)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
    BEGIN
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE grant_id=p_grant_id FOR UPDATE;
      IF NOT FOUND THEN RETURN FALSE; END IF;
      IF h.status='GRANT_ISSUED' AND h.runtime_instance_id IS DISTINCT FROM p_runtime_id THEN
        UPDATE SCHEMA_TOKEN.okx_demo_submission_grants SET status='FAILED',
          consumed_at=statement_timestamp() WHERE grant_id=p_grant_id AND status='ACTIVE';
        PERFORM SCHEMA_TOKEN.expire_okx_demo_canary_approval(
          h.approval_id,'runtime restart before exact prepare');
        UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET status='REVOKED',
          failure_code='RUNTIME_RESTART_BEFORE_PREPARED',revoked_at=statement_timestamp(),
          updated_at=statement_timestamp() WHERE handoff_id=h.handoff_id;
        RETURN TRUE;
      END IF;
      RETURN FALSE;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.fail_okx_demo_canary_grant_before_prepare(
      p_grant_id text)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            g SCHEMA_TOKEN.okx_demo_submission_grants%ROWTYPE;
    BEGIN
      PERFORM SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret();
      IF NOT pg_try_advisory_xact_lock(5067747289570038600) THEN
        RAISE EXCEPTION 'controlled canary coordination lock is busy';
      END IF;
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE grant_id=p_grant_id FOR UPDATE;
      SELECT * INTO g FROM SCHEMA_TOKEN.okx_demo_submission_grants
        WHERE grant_id=p_grant_id FOR UPDATE;
      IF NOT FOUND OR h.handoff_id IS NULL OR h.status<>'GRANT_ISSUED'
         OR g.status<>'ACTIVE' OR g.approval_id IS DISTINCT FROM h.approval_id THEN
        RETURN FALSE;
      END IF;
      IF EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
          WHERE approval_id=g.approval_id AND operation IN ('PLACE','CLOSE')) THEN
        RETURN FALSE;
      END IF;
      UPDATE SCHEMA_TOKEN.okx_demo_submission_grants SET status='FAILED',
        consumed_at=statement_timestamp() WHERE grant_id=p_grant_id;
      PERFORM SCHEMA_TOKEN.expire_okx_demo_canary_approval(
        h.approval_id,'writer failed before durable prepare');
      UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET status='FAILED',
        failure_code='WRITER_FAILED_BEFORE_PREPARED',revoked_at=statement_timestamp(),
        updated_at=statement_timestamp() WHERE handoff_id=h.handoff_id;
      RETURN TRUE;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.settle_okx_demo_canary_handoff(
      p_grant_id text)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            g SCHEMA_TOKEN.okx_demo_submission_grants%ROWTYPE;
    BEGIN
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE grant_id=p_grant_id FOR UPDATE;
      IF NOT FOUND THEN RETURN NULL; END IF;
      IF h.status IN ('CONSUMED','REVOKED','FAILED','EXPIRED') THEN
        RETURN h.status;
      END IF;
      SELECT * INTO g FROM SCHEMA_TOKEN.okx_demo_submission_grants
        WHERE grant_id=p_grant_id;
      IF g.status='CONSUMED' THEN
        IF NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles l
             JOIN SCHEMA_TOKEN.okx_order_write_attempts a
               ON a.approval_id=l.opening_approval_id AND a.operation='PLACE'
              AND a.attempt_count=1 AND a.recovery_grant_database_id IS NULL
             WHERE l.submission_grant_id=g.grant_id
               AND l.opening_exchange_order_row_id=a.exchange_order_row_id
               AND l.cleanup_phase='OPENING_SUBMITTED') THEN
          RAISE EXCEPTION 'consumed grant lacks exact prepared lifecycle';
        END IF;
        UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
          SET status='CONSUMED',updated_at=statement_timestamp()
          WHERE handoff_id=h.handoff_id;
        RETURN 'CONSUMED';
      ELSIF g.status IN ('FAILED','EXPIRED') THEN
        PERFORM SCHEMA_TOKEN.expire_okx_demo_canary_approval(
          h.approval_id,'canary grant terminal before exact prepare');
        UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
          SET status=g.status,failure_code='GRANT_'||g.status,
              revoked_at=statement_timestamp(),updated_at=statement_timestamp()
          WHERE handoff_id=h.handoff_id;
        RETURN g.status;
      END IF;
      RETURN h.status;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.revoke_all_okx_demo_canary_consents_for_hardening()
    RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            revoked_count bigint:=0;
    BEGIN
      IF NOT pg_try_advisory_xact_lock(5067747289570038600) THEN
        RAISE EXCEPTION 'controlled canary coordination lock is busy';
      END IF;
      FOR h IN SELECT * FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE status IN ('REQUESTED','FINALIZED','GRANT_ISSUED') FOR UPDATE LOOP
        IF h.grant_id IS NOT NULL THEN
          UPDATE SCHEMA_TOKEN.okx_demo_submission_grants SET status='FAILED',
            consumed_at=COALESCE(consumed_at,statement_timestamp())
            WHERE grant_id=h.grant_id AND status='ACTIVE';
        END IF;
        IF h.approval_id IS NOT NULL THEN
          PERFORM SCHEMA_TOKEN.expire_okx_demo_canary_approval(
            h.approval_id,'operator consent revoked before key hardening');
        END IF;
        UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET
          status=CASE WHEN h.status='REQUESTED' THEN 'EXPIRED' ELSE 'REVOKED' END,
          failure_code='KEY_HARDENING_REAUTHORIZATION_REQUIRED',
          revoked_at=CASE WHEN h.status='REQUESTED' THEN NULL ELSE statement_timestamp() END,
          updated_at=statement_timestamp() WHERE handoff_id=h.handoff_id;
        revoked_count:=revoked_count+1;
      END LOOP;
      RETURN revoked_count;
    END $$;
    """.replace("SCHEMA_TOKEN", quoted_schema)
    connection.execute(text(ddl))
    signatures = (
        "request_okx_demo_canary_consent(text,text,text,text)",
        "pending_okx_demo_canary_consent()",
        "fail_requested_okx_demo_canary_consent(text,text,text)",
        "claim_okx_demo_canary_consent(text,text,bigint,jsonb)",
        "finalize_okx_demo_canary_consent(text,text,bigint,bigint,bigint,bigint,jsonb)",
        "finalized_okx_demo_canary_consent(text)",
        "issue_okx_demo_submission_grant(jsonb)",
        "revoke_restarted_okx_demo_canary_grant(text,text)",
        "fail_okx_demo_canary_grant_before_prepare(text)",
        "settle_okx_demo_canary_handoff(text)",
    )
    for signature in signatures:
        connection.execute(text(
            "ALTER FUNCTION {0}.{1} OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON FUNCTION {0}.{1} FROM PUBLIC; "
            "GRANT EXECUTE ON FUNCTION {0}.{1} TO freqtrade".format(
                quoted_schema, signature
            )
        ))
    for signature in (
        "canonical_jsonb_text(jsonb)",
        "canonical_decimal_text(numeric)",
        "require_active_okx_demo_operator_consent_secret()",
        "freeze_okx_demo_canary_source()",
        "require_okx_demo_grant_handoff()",
        "expire_okx_demo_canary_approval(bigint,text)",
        "revoke_all_okx_demo_canary_consents_for_hardening()",
    ):
        connection.execute(text(
            "ALTER FUNCTION {0}.{1} OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON FUNCTION {0}.{1} FROM PUBLIC,freqtrade"
            .format(quoted_schema, signature)
        ))
    connection.execute(text(
        "REVOKE INSERT ON TABLE {}.okx_demo_submission_grants FROM freqtrade"
        .format(quoted_schema)
    ))


def _add_accepted_not_found_terminalization_boundary(connection: Connection) -> None:
    """Install v32's owner-only, append-only accepted-NOT_FOUND receipt."""

    schema_name = connection.execute(text("SELECT current_schema()" )).scalar_one()
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(schema_name)
    receipt_table = Base.metadata.tables[
        "okx_demo_accepted_not_found_terminalizations"
    ]
    receipt_table.create(bind=connection, checkfirst=True)
    ddl = r"""
    ALTER TABLE SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
      ADD COLUMN IF NOT EXISTS request_digest varchar(64),
      ALTER COLUMN request_digest SET NOT NULL,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_terminalizations_source_job_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_terminalizations_predecessor_handoff_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_terminalizations_predecessor_grant_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_terminalizations_lifecycle_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_terminalizations_attempt_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_terminalizations_exchange_order_row_id_fkey,
      ADD CONSTRAINT okx_demo_accepted_not_found_terminalizations_source_job_id_fkey
        FOREIGN KEY(source_job_id) REFERENCES SCHEMA_TOKEN.research_jobs(id) ON DELETE RESTRICT,
      ADD CONSTRAINT okx_demo_accepted_not_found_terminalizations_predecessor_handoff_id_fkey
        FOREIGN KEY(predecessor_handoff_id) REFERENCES SCHEMA_TOKEN.okx_demo_canary_consent_handoffs(handoff_id) ON DELETE RESTRICT,
      ADD CONSTRAINT okx_demo_accepted_not_found_terminalizations_predecessor_grant_id_fkey
        FOREIGN KEY(predecessor_grant_id) REFERENCES SCHEMA_TOKEN.okx_demo_submission_grants(grant_id) ON DELETE RESTRICT,
      ADD CONSTRAINT okx_demo_accepted_not_found_terminalizations_lifecycle_id_fkey
        FOREIGN KEY(lifecycle_id) REFERENCES SCHEMA_TOKEN.okx_demo_canary_lifecycles(lifecycle_id) ON DELETE RESTRICT,
      ADD CONSTRAINT okx_demo_accepted_not_found_terminalizations_attempt_id_fkey
        FOREIGN KEY(attempt_id) REFERENCES SCHEMA_TOKEN.okx_order_write_attempts(id) ON DELETE RESTRICT,
      ADD CONSTRAINT okx_demo_accepted_not_found_terminalizations_exchange_order_row_id_fkey
        FOREIGN KEY(exchange_order_row_id) REFERENCES SCHEMA_TOKEN.exchange_orders(id) ON DELETE RESTRICT;

    ALTER TABLE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
      ADD COLUMN IF NOT EXISTS terminal_receipt_id bigint;
    ALTER TABLE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
      DROP CONSTRAINT IF EXISTS okx_demo_canary_consent_handoffs_terminal_receipt_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_canary_consent_handoffs_terminal_receipt_id_key,
      ADD CONSTRAINT okx_demo_canary_consent_handoffs_terminal_receipt_id_fkey
        FOREIGN KEY(terminal_receipt_id)
        REFERENCES SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations(id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
      ADD CONSTRAINT okx_demo_canary_consent_handoffs_terminal_receipt_id_key
        UNIQUE(terminal_receipt_id);

    ALTER TABLE SCHEMA_TOKEN.okx_demo_canary_lifecycles
      ADD COLUMN IF NOT EXISTS accepted_terminalization_id bigint;
    ALTER TABLE SCHEMA_TOKEN.okx_demo_canary_lifecycles
      DROP CONSTRAINT IF EXISTS okx_demo_canary_lifecycles_accepted_terminalization_id_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_canary_lifecycles_accepted_terminalization_id_key,
      ADD CONSTRAINT okx_demo_canary_lifecycles_accepted_terminalization_id_fkey
        FOREIGN KEY(accepted_terminalization_id)
        REFERENCES SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations(id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
      ADD CONSTRAINT okx_demo_canary_lifecycles_accepted_terminalization_id_key
        UNIQUE(accepted_terminalization_id),
      DROP CONSTRAINT IF EXISTS okx_demo_canary_lifecycle_terminal_shape_check,
      ADD CONSTRAINT okx_demo_canary_lifecycle_terminal_shape_check CHECK(
        cleanup_phase<>'TERMINAL' OR (
          outcome IN ('PASSED','FAILED') AND terminal_at IS NOT NULL
          AND revoked_at IS NOT NULL AND final_evidence_digest IS NOT NULL
          AND ((accepted_terminalization_id IS NULL
                AND final_reconciliation_run_id IS NOT NULL)
               OR (accepted_terminalization_id IS NOT NULL
                   AND final_reconciliation_run_id IS NULL))));

    ALTER TABLE SCHEMA_TOKEN.okx_order_write_attempts
      DROP CONSTRAINT IF EXISTS okx_order_write_attempts_state_check,
      ADD CONSTRAINT okx_order_write_attempts_state_check CHECK(
        state IN ('PREPARED','DISPATCHED','ACKNOWLEDGED','REJECTED',
                  'RECOVERY_REQUIRED','RESIDUAL_CLOSE_REQUIRED','RECONCILED',
                  'USER_ACCEPTED_NOT_FOUND_NO_FILL'));
    DROP INDEX IF EXISTS SCHEMA_TOKEN.okx_order_write_attempts_one_unresolved_target_idx;
    CREATE UNIQUE INDEX okx_order_write_attempts_one_unresolved_target_idx
      ON SCHEMA_TOKEN.okx_order_write_attempts(execution_target_id)
      WHERE state IN ('PREPARED','DISPATCHED','ACKNOWLEDGED',
                      'RECOVERY_REQUIRED','RESIDUAL_CLOSE_REQUIRED');

    DROP INDEX IF EXISTS SCHEMA_TOKEN.okx_demo_canary_one_successor_ever_idx;
    CREATE UNIQUE INDEX IF NOT EXISTS okx_demo_canary_one_accepted_successor_idx
      ON SCHEMA_TOKEN.okx_demo_canary_consent_handoffs(execution_target_id)
      WHERE terminal_receipt_id IS NOT NULL;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.guard_accepted_not_found_terminalization()
    RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    BEGIN
      RAISE EXCEPTION 'accepted NOT_FOUND terminalization receipts are append-only';
    END $$;
    DROP TRIGGER IF EXISTS okx_demo_accepted_not_found_immutable
      ON SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations;
    CREATE TRIGGER okx_demo_accepted_not_found_immutable
      BEFORE UPDATE OR DELETE ON SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
      FOR EACH ROW EXECUTE FUNCTION SCHEMA_TOKEN.guard_accepted_not_found_terminalization();

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.guard_accepted_not_found_attempt_transition()
    RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    BEGIN
      IF OLD.state='USER_ACCEPTED_NOT_FOUND_NO_FILL' THEN
        IF NEW IS DISTINCT FROM OLD THEN
          RAISE EXCEPTION 'accepted NOT_FOUND attempt is immutable';
        END IF;
        RETURN NEW;
      END IF;
      IF NEW.state='USER_ACCEPTED_NOT_FOUND_NO_FILL' THEN
        IF OLD.state<>'RECOVERY_REQUIRED'
           OR NEW.order_state<>'USER_ACCEPTED_NOT_FOUND_NO_FILL'
           OR OLD.id IS DISTINCT FROM NEW.id
           OR OLD.execution_target_id IS DISTINCT FROM NEW.execution_target_id
           OR OLD.exchange_order_row_id IS DISTINCT FROM NEW.exchange_order_row_id
           OR OLD.approval_id IS DISTINCT FROM NEW.approval_id
           OR OLD.recovery_grant_database_id IS DISTINCT FROM NEW.recovery_grant_database_id
           OR OLD.operation IS DISTINCT FROM NEW.operation
           OR OLD.operation_id IS DISTINCT FROM NEW.operation_id
           OR OLD.client_order_id IS DISTINCT FROM NEW.client_order_id
           OR OLD.instrument_id IS DISTINCT FROM NEW.instrument_id
           OR OLD.request_digest IS DISTINCT FROM NEW.request_digest
           OR OLD.safe_request_snapshot::jsonb IS DISTINCT FROM NEW.safe_request_snapshot::jsonb
           OR OLD.safe_response_snapshot::jsonb IS DISTINCT FROM NEW.safe_response_snapshot::jsonb
           OR OLD.attempt_count IS DISTINCT FROM NEW.attempt_count
           OR OLD.lease_generation IS DISTINCT FROM NEW.lease_generation
           OR OLD.parent_attempt_id IS DISTINCT FROM NEW.parent_attempt_id
           OR OLD.close_sequence IS DISTINCT FROM NEW.close_sequence
           OR OLD.reason_code IS DISTINCT FROM NEW.reason_code
           OR OLD.last_attempt_at IS DISTINCT FROM NEW.last_attempt_at
           OR OLD.dispatch_not_after IS DISTINCT FROM NEW.dispatch_not_after
           OR OLD.created_at IS DISTINCT FROM NEW.created_at
           OR NOT EXISTS(
             SELECT 1
               FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations receipt
              WHERE receipt.attempt_id=OLD.id
                AND receipt.exchange_order_row_id=OLD.exchange_order_row_id
                AND receipt.request_digest=OLD.request_digest)
        THEN
          RAISE EXCEPTION 'invalid accepted NOT_FOUND attempt transition';
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    DROP TRIGGER IF EXISTS okx_order_write_attempts_accepted_not_found_guard
      ON SCHEMA_TOKEN.okx_order_write_attempts;
    CREATE TRIGGER okx_order_write_attempts_accepted_not_found_guard
      BEFORE UPDATE ON SCHEMA_TOKEN.okx_order_write_attempts
      FOR EACH ROW EXECUTE FUNCTION SCHEMA_TOKEN.guard_accepted_not_found_attempt_transition();

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.exact_accepted_not_found_predecessor(
      p_handoff_id text)
    RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
      SELECT count(*)=1
        FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations receipt
        JOIN SCHEMA_TOKEN.okx_demo_canary_consent_handoffs root
          ON root.handoff_id=receipt.predecessor_handoff_id
        JOIN SCHEMA_TOKEN.okx_demo_canary_consent_handoffs original
          ON original.handoff_id=root.supersedes_handoff_id
        JOIN SCHEMA_TOKEN.okx_demo_submission_grants grant_row
          ON grant_row.grant_id=receipt.predecessor_grant_id
        JOIN SCHEMA_TOKEN.okx_demo_canary_lifecycles lifecycle
          ON lifecycle.lifecycle_id=receipt.lifecycle_id
        JOIN SCHEMA_TOKEN.okx_order_write_attempts attempt
          ON attempt.id=receipt.attempt_id
        JOIN SCHEMA_TOKEN.exchange_orders order_row
          ON order_row.id=receipt.exchange_order_row_id
       WHERE root.handoff_id=p_handoff_id
         AND root.supersedes_handoff_id IS NOT NULL AND root.status='CONSUMED'
         AND original.supersedes_handoff_id IS NULL
         AND original.status='EXPIRED'
         AND original.failure_code='FINALIZED_EVIDENCE_EXPIRED'
         AND original.grant_id IS NULL
         AND root.grant_id=grant_row.grant_id AND grant_row.status='CONSUMED'
         AND attempt.exchange_order_row_id=order_row.id
         AND attempt.approval_id=grant_row.approval_id
         AND receipt.request_digest=attempt.request_digest
         AND attempt.operation='PLACE' AND attempt.attempt_count=1
         AND attempt.state='USER_ACCEPTED_NOT_FOUND_NO_FILL'
         AND attempt.order_state='USER_ACCEPTED_NOT_FOUND_NO_FILL'
         AND order_row.status='USER_ACCEPTED_NOT_FOUND_NO_FILL'
         AND order_row.exchange_order_id IS NULL
         AND lifecycle.submission_grant_id=grant_row.grant_id
         AND lifecycle.opening_exchange_order_row_id=order_row.id
         AND lifecycle.cleanup_phase='TERMINAL' AND lifecycle.outcome='FAILED'
         AND lifecycle.failure_code='USER_ACCEPTED_NOT_FOUND_NO_FILL'
         AND lifecycle.final_reconciliation_run_id IS NULL
         AND lifecycle.accepted_terminalization_id=receipt.id
         AND lifecycle.final_evidence_digest=receipt.acceptance_digest
         AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_fills fill_row
               WHERE fill_row.exchange_order_row_id=order_row.id)
         AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts child
               WHERE child.parent_attempt_id=attempt.id);
    $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.okx_demo_canary_consent_eligibility()
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE receipt SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations%ROWTYPE;
            predecessor jsonb;
    BEGIN
      SELECT * INTO receipt FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
        ORDER BY id LIMIT 1;
      IF FOUND THEN
        IF SCHEMA_TOKEN.exact_accepted_not_found_predecessor(
             receipt.predecessor_handoff_id)
           AND NOT EXISTS(SELECT 1
                FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs child
                WHERE child.supersedes_handoff_id=receipt.predecessor_handoff_id) THEN
          RETURN jsonb_build_object('eligibility_state','ACCEPTED_SUCCESSOR',
            'handoff_id',receipt.predecessor_handoff_id,
            'terminal_receipt_id',receipt.id,
            'terminal_evidence_digest',receipt.acceptance_digest,
            'source_job_id',receipt.source_job_id);
        END IF;
        RETURN jsonb_build_object('eligibility_state','BLOCKED');
      END IF;
      predecessor:=SCHEMA_TOKEN.eligible_atomic_okx_demo_canary_predecessor();
      RETURN jsonb_build_object('eligibility_state','PRISTINE',
        'predecessor',predecessor);
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.terminalize_accepted_not_found_no_fill(
      p_payload jsonb)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE attempt SCHEMA_TOKEN.okx_order_write_attempts%ROWTYPE;
            order_row SCHEMA_TOKEN.exchange_orders%ROWTYPE;
            lifecycle SCHEMA_TOKEN.okx_demo_canary_lifecycles%ROWTYPE;
            grant_row SCHEMA_TOKEN.okx_demo_submission_grants%ROWTYPE;
            handoff SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            original SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            lease SCHEMA_TOKEN.okx_order_writer_leases%ROWTYPE;
            existing SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations%ROWTYPE;
            evidence jsonb; identity jsonb; v_evidence_digest text;
            v_acceptance_digest text; new_receipt_id bigint;
            v_approval_id bigint; v_approval_status text;
            v_chain_id bigint; v_chain_status text;
            v_chain_approval_id bigint; v_chain_target text;
            observed_at timestamptz;
    BEGIN
      PERFORM pg_advisory_xact_lock(5067747289570038600);
      IF jsonb_typeof(p_payload)<>'object'
         OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_payload) key)
              IS DISTINCT FROM ARRAY['acceptance_digest','attempt_id',
                'evidence_digest','evidence_observed_at','evidence_snapshot',
                'expected_fencing_version','lifecycle_id']::text[]
         OR p_payload->>'acceptance_digest'!~'^[0-9a-f]{64}$'
         OR p_payload->>'evidence_digest'!~'^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid accepted NOT_FOUND terminalization payload';
      END IF;
      observed_at:=(p_payload->>'evidence_observed_at')::timestamptz;
      evidence:=p_payload->'evidence_snapshot';
      SELECT * INTO attempt FROM SCHEMA_TOKEN.okx_order_write_attempts
        WHERE id=(p_payload->>'attempt_id')::bigint FOR UPDATE;
      SELECT * INTO order_row FROM SCHEMA_TOKEN.exchange_orders
        WHERE id=attempt.exchange_order_row_id FOR UPDATE;
      SELECT * INTO lifecycle FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
        WHERE lifecycle_id=p_payload->>'lifecycle_id' FOR UPDATE;
      SELECT * INTO grant_row FROM SCHEMA_TOKEN.okx_demo_submission_grants
        WHERE grant_id=lifecycle.submission_grant_id FOR UPDATE;
      SELECT * INTO handoff FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=grant_row.handoff_id FOR UPDATE;
      SELECT * INTO original FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=handoff.supersedes_handoff_id FOR UPDATE;
      SELECT id,status INTO v_approval_id,v_approval_status
        FROM SCHEMA_TOKEN.approved_executions
        WHERE id=grant_row.approval_id FOR UPDATE;
      SELECT id,status,approved_execution_id,execution_target_id
        INTO v_chain_id,v_chain_status,v_chain_approval_id,v_chain_target
        FROM SCHEMA_TOKEN.full_chain_runs
        WHERE id=handoff.full_chain_run_id FOR UPDATE;
      SELECT * INTO lease FROM SCHEMA_TOKEN.okx_order_writer_leases
        WHERE execution_target_id='OKX_DEMO' FOR UPDATE;

      SELECT * INTO existing
        FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
       WHERE acceptance_digest=p_payload->>'acceptance_digest'
          OR attempt_id=attempt.id OR exchange_order_row_id=order_row.id
          OR lifecycle_id=lifecycle.lifecycle_id
          OR predecessor_grant_id=grant_row.grant_id
          OR predecessor_handoff_id=handoff.handoff_id FOR UPDATE;
      IF FOUND THEN
        IF existing.acceptance_digest=p_payload->>'acceptance_digest'
           AND existing.attempt_id=attempt.id
           AND existing.exchange_order_row_id=order_row.id
           AND existing.lifecycle_id=lifecycle.lifecycle_id
           AND existing.evidence_digest=p_payload->>'evidence_digest' THEN
          RETURN jsonb_build_object('terminalization_id',existing.id,
            'acceptance_digest',existing.acceptance_digest,'idempotent',true);
        END IF;
        RAISE EXCEPTION 'accepted NOT_FOUND terminalization identity drift';
      END IF;

      IF attempt.id IS NULL OR order_row.id IS NULL OR lifecycle.lifecycle_id IS NULL
         OR grant_row.grant_id IS NULL OR handoff.handoff_id IS NULL OR lease.execution_target_id IS NULL
         OR handoff.source_job_id<>22 OR handoff.supersedes_handoff_id IS NULL
         OR original.handoff_id IS NULL OR original.supersedes_handoff_id IS NOT NULL
         OR original.status<>'EXPIRED'
         OR original.failure_code<>'FINALIZED_EVIDENCE_EXPIRED'
         OR original.grant_id IS NOT NULL
         OR handoff.status<>'CONSUMED' OR grant_row.status<>'CONSUMED'
         OR handoff.grant_id IS DISTINCT FROM grant_row.grant_id
         OR v_approval_id IS NULL OR v_approval_status<>'ACTIVE'
         OR v_approval_id<>grant_row.approval_id
         OR v_chain_id IS NULL OR v_chain_status<>'EXECUTING'
         OR v_chain_approval_id<>v_approval_id
         OR v_chain_target<>'OKX_DEMO'
         OR attempt.operation<>'PLACE' OR attempt.state<>'RECOVERY_REQUIRED'
         OR attempt.reason_code<>'EXACT_ORDER_NOT_FOUND' OR attempt.attempt_count<>1
         OR attempt.safe_response_snapshot::jsonb IS DISTINCT FROM
              jsonb_build_object('exact_order_get','NOT_FOUND','okx_code','51603')
         OR attempt.recovery_grant_database_id IS NOT NULL OR attempt.parent_attempt_id IS NOT NULL
         OR order_row.exchange_order_id IS NOT NULL OR order_row.id<>lifecycle.opening_exchange_order_row_id
         OR lifecycle.cleanup_phase<>'OPENING_SUBMITTED'
         OR lifecycle.outcome<>'PENDING' OR lifecycle.attributed_fill_quantity<>0
         OR lifecycle.fencing_version<>(p_payload->>'expected_fencing_version')::bigint
         OR handoff.consent_deadline_at>=clock_timestamp()
         OR handoff.bundle_expires_at>=clock_timestamp()
         OR grant_row.expires_at>=clock_timestamp()
         OR lifecycle.deadline_at>=clock_timestamp()
         OR lease.expires_at>=clock_timestamp()
         OR attempt.dispatch_not_after IS NULL
         OR attempt.dispatch_not_after>=clock_timestamp()
         OR observed_at IS DISTINCT FROM attempt.last_attempt_at
         OR attempt.last_attempt_at<clock_timestamp()-interval '5 minutes'
         OR attempt.last_attempt_at>clock_timestamp()+interval '5 seconds'
         OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_order_write_attempts
               WHERE execution_target_id='OKX_DEMO')<>1
         OR (SELECT count(*) FROM SCHEMA_TOKEN.exchange_orders
               WHERE execution_target_id='OKX_DEMO')<>1
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_fills
               WHERE execution_target_id='OKX_DEMO')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
               WHERE parent_attempt_id=attempt.id)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
               WHERE supersedes_handoff_id=handoff.handoff_id) THEN
        RAISE EXCEPTION 'accepted NOT_FOUND terminalization precondition mismatch';
      END IF;
      IF EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
             WHERE status IN ('REQUESTED','FINALIZED','GRANT_ISSUED'))
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants
             WHERE status='ACTIVE')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.approved_executions
             WHERE execution_target_id='OKX_DEMO' AND status='ACTIVE'
               AND id<>v_approval_id)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.full_chain_runs
             WHERE execution_target_id='OKX_DEMO' AND status='EXECUTING'
               AND id<>v_chain_id)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
             WHERE lifecycle_id<>lifecycle.lifecycle_id
               AND cleanup_phase NOT IN ('TERMINAL','REVOKED'))
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_positions
             WHERE execution_target_id='OKX_DEMO' AND quantity<>0)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.risk_budgets
             WHERE execution_target_id='OKX_DEMO'
               AND (reserved_notional<>0 OR approved_positions<>0)) THEN
        RAISE EXCEPTION 'accepted NOT_FOUND terminalization precondition mismatch';
      END IF;
      IF evidence IS DISTINCT FROM jsonb_build_object(
           'absolute_submission_claim',false,'attempt_count',1,
           'client_order_id',attempt.client_order_id,'exchange_result_code','51603',
           'exchange_result_state','NOT_FOUND','fill_count',0,
           'instrument_id',attempt.instrument_id,'query_kind','exact_get',
           'request_digest',attempt.request_digest,
           'restart_resubmission_count',0) THEN
        RAISE EXCEPTION 'accepted NOT_FOUND evidence mismatch';
      END IF;
      v_evidence_digest:=encode(public.digest(convert_to(
        SCHEMA_TOKEN.canonical_jsonb_text(evidence),'UTF8'),'sha256'),'hex');
      identity:=jsonb_build_object('acceptance_kind','USER_ACCEPTED_NOT_FOUND_NO_FILL_V1',
        'absolute_submission_claim',false,'attempt_id',attempt.id,
        'evidence_digest',v_evidence_digest,'evidence_observed_at',observed_at,
        'exchange_order_row_id',order_row.id,'lifecycle_id',lifecycle.lifecycle_id,
        'predecessor_grant_id',grant_row.grant_id,
        'predecessor_handoff_id',handoff.handoff_id,
        'request_digest',attempt.request_digest,'source_job_id',22);
      v_acceptance_digest:=encode(public.digest(convert_to(
        SCHEMA_TOKEN.canonical_jsonb_text(identity),'UTF8'),'sha256'),'hex');
      IF v_evidence_digest IS DISTINCT FROM p_payload->>'evidence_digest'
         OR v_acceptance_digest IS DISTINCT FROM p_payload->>'acceptance_digest' THEN
        RAISE EXCEPTION 'accepted NOT_FOUND digest mismatch';
      END IF;
      INSERT INTO SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations(
        source_job_id,predecessor_handoff_id,predecessor_grant_id,lifecycle_id,
        attempt_id,exchange_order_row_id,acceptance_kind,absolute_submission_claim,
        exchange_result_code,exchange_result_state,fill_count,attempt_count,
        restart_resubmission_count,request_digest,evidence_observed_at,evidence_snapshot,
        evidence_digest,acceptance_digest)
      VALUES(22,handoff.handoff_id,grant_row.grant_id,lifecycle.lifecycle_id,
        attempt.id,order_row.id,'USER_ACCEPTED_NOT_FOUND_NO_FILL_V1',false,
        '51603','NOT_FOUND',0,1,0,attempt.request_digest,observed_at,evidence::json,
        v_evidence_digest,v_acceptance_digest) RETURNING id INTO new_receipt_id;
      UPDATE SCHEMA_TOKEN.okx_order_write_attempts
         SET state='USER_ACCEPTED_NOT_FOUND_NO_FILL',
             order_state='USER_ACCEPTED_NOT_FOUND_NO_FILL',updated_at=clock_timestamp()
       WHERE id=attempt.id AND state='RECOVERY_REQUIRED' AND attempt_count=1;
      IF NOT FOUND THEN RAISE EXCEPTION 'accepted NOT_FOUND attempt CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.exchange_orders
         SET status='USER_ACCEPTED_NOT_FOUND_NO_FILL',updated_at=clock_timestamp()
       WHERE id=order_row.id AND exchange_order_id IS NULL;
      IF NOT FOUND THEN RAISE EXCEPTION 'accepted NOT_FOUND order CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.okx_demo_canary_lifecycles
         SET outcome='FAILED',cleanup_phase='TERMINAL',
             failure_code='USER_ACCEPTED_NOT_FOUND_NO_FILL',
             final_reconciliation_run_id=NULL,
             accepted_terminalization_id=new_receipt_id,
             final_evidence_digest=v_acceptance_digest,
             terminal_at=clock_timestamp(),revoked_at=clock_timestamp(),
             fencing_version=fencing_version+1,updated_at=clock_timestamp()
       WHERE lifecycle_id=lifecycle.lifecycle_id
         AND cleanup_phase='OPENING_SUBMITTED'
         AND fencing_version=(p_payload->>'expected_fencing_version')::bigint;
      IF NOT FOUND THEN RAISE EXCEPTION 'accepted NOT_FOUND lifecycle CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.approved_executions
         SET status='EXPIRED'
       WHERE id=v_approval_id AND status='ACTIVE';
      IF NOT FOUND THEN RAISE EXCEPTION 'accepted NOT_FOUND approval CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.full_chain_runs
         SET status='BLOCKED',terminal_reason='user accepted NOT_FOUND no-fill terminal',
             completed_at=clock_timestamp()
       WHERE id=v_chain_id AND status='EXECUTING';
      IF NOT FOUND THEN RAISE EXCEPTION 'accepted NOT_FOUND full-chain CAS lost'; END IF;
      RETURN jsonb_build_object('terminalization_id',new_receipt_id,
        'acceptance_digest',v_acceptance_digest,'idempotent',false,
        'absolute_submission_claim',false);
    END $$;
    """.replace("SCHEMA_TOKEN", quoted_schema)
    connection.execute(text(ddl))
    for signature in (
        "guard_accepted_not_found_terminalization()",
        "guard_accepted_not_found_attempt_transition()",
        "exact_accepted_not_found_predecessor(text)",
        "terminalize_accepted_not_found_no_fill(jsonb)",
    ):
        connection.execute(text(
            "ALTER FUNCTION {0}.{1} OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON FUNCTION {0}.{1} FROM PUBLIC,freqtrade"
            .format(quoted_schema, signature)
        ))
    connection.execute(text(
        "ALTER FUNCTION {0}.okx_demo_canary_consent_eligibility() "
        "OWNER TO freqtrade_ai_attestor; REVOKE ALL ON FUNCTION "
        "{0}.okx_demo_canary_consent_eligibility() FROM PUBLIC; GRANT EXECUTE ON FUNCTION "
        "{0}.okx_demo_canary_consent_eligibility() TO freqtrade".format(quoted_schema)
    ))
    connection.execute(text(
        "ALTER TABLE {0}.okx_demo_accepted_not_found_terminalizations "
        "OWNER TO freqtrade_ai_attestor; REVOKE ALL ON TABLE "
        "{0}.okx_demo_accepted_not_found_terminalizations "
        "FROM PUBLIC,freqtrade".format(quoted_schema)
    ))
    connection.execute(text(
        "GRANT SELECT (id,trade_intent_id,risk_decision_id,execution_target_id,"
        "status,expires_at,reserved_notional,evidence_snapshot), UPDATE (status) "
        "ON {0}.approved_executions TO freqtrade_ai_attestor; "
        "GRANT SELECT (id,research_job_id,research_job_attempt_id,run_kind,"
        "signal_evaluation_id,research_scope_id,execution_target_id,status,"
        "current_stage,strategy_generation_run_id,strategy_id,strategy_version_id,"
        "backtest_run_id,backtest_task_id,backtest_result_id,strategy_score_id,"
        "candidate_approval_id,signal_snapshot_id,trade_intent_id,risk_decision_id,"
        "approved_execution_id,exchange_order_id), "
        "UPDATE (status,terminal_reason,completed_at) ON {0}.full_chain_runs "
        "TO freqtrade_ai_attestor; "
        "GRANT SELECT (execution_target_id,reserved_notional,approved_positions), "
        "UPDATE (reserved_notional,approved_positions) ON {0}.risk_budgets "
        "TO freqtrade_ai_attestor".format(quoted_schema)
    ))
    sequence_identity = connection.execute(text(
        "SELECT pg_get_serial_sequence(:table_name,'id')"
    ), {"table_name": f"{schema_name}.okx_demo_accepted_not_found_terminalizations"}).scalar_one()
    if sequence_identity:
        connection.execute(text(
            "ALTER SEQUENCE {} OWNER TO freqtrade_ai_attestor; REVOKE ALL ON SEQUENCE {} "
            "FROM PUBLIC,freqtrade".format(sequence_identity, sequence_identity)
        ))


def _add_bounded_second_accepted_not_found_boundary(connection: Connection) -> None:
    """Install v33's fixed second receipt and final-successor boundary.

    This is deliberately not a recursive receipt design.  The v33 functions only
    create depth-1/depth-2 receipts and allow one receipt-bound final handoff.  The
    table constraint also preserves a later v34 depth-3 final receipt so replaying
    this installer during a future upgrade cannot invalidate durable evidence.
    """

    schema_name = connection.execute(text("SELECT current_schema()" )).scalar_one()
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(schema_name)
    ddl = r"""
    ALTER TABLE SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
      ADD COLUMN IF NOT EXISTS receipt_depth integer DEFAULT 1,
      ADD COLUMN IF NOT EXISTS parent_terminal_receipt_id bigint;
    UPDATE SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
       SET receipt_depth=1 WHERE receipt_depth IS NULL;
    ALTER TABLE SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
      ALTER COLUMN receipt_depth SET DEFAULT 1,
      ALTER COLUMN receipt_depth SET NOT NULL,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_terminalizations_source_job_id_key,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_kind_check,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_identity_check,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_receipt_depth_key,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_parent_terminal_receipt_id_key,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_terminalizations_parent_terminal_receipt_id_fkey,
      ADD CONSTRAINT okx_demo_accepted_not_found_kind_check CHECK(
        (receipt_depth=1 AND parent_terminal_receipt_id IS NULL
          AND acceptance_kind='USER_ACCEPTED_NOT_FOUND_NO_FILL_V1')
        OR (receipt_depth=2 AND parent_terminal_receipt_id IS NOT NULL
          AND acceptance_kind='USER_ACCEPTED_NOT_FOUND_NO_FILL_V2')
        OR (receipt_depth=3 AND parent_terminal_receipt_id IS NOT NULL
          AND acceptance_kind='USER_ACCEPTED_NOT_FOUND_NO_FILL_FINAL_V1')),
      ADD CONSTRAINT okx_demo_accepted_not_found_identity_check CHECK(
        source_job_id=22 AND receipt_depth IN (1,2,3)
        AND length(request_digest)=64 AND length(evidence_digest)=64
        AND length(acceptance_digest)=64),
      ADD CONSTRAINT okx_demo_accepted_not_found_receipt_depth_key UNIQUE(receipt_depth),
      ADD CONSTRAINT okx_demo_accepted_not_found_parent_terminal_receipt_id_key
        UNIQUE(parent_terminal_receipt_id),
      ADD CONSTRAINT okx_demo_accepted_not_found_terminalizations_parent_terminal_receipt_id_fkey
        FOREIGN KEY(parent_terminal_receipt_id)
        REFERENCES SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations(id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

    DROP INDEX IF EXISTS SCHEMA_TOKEN.okx_demo_canary_one_accepted_successor_idx;
    CREATE UNIQUE INDEX okx_demo_canary_one_accepted_successor_idx
      ON SCHEMA_TOKEN.okx_demo_canary_consent_handoffs(
        execution_target_id,terminal_receipt_id)
      WHERE terminal_receipt_id IS NOT NULL;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.exact_bounded_accepted_not_found_predecessor(
      p_handoff_id text)
    RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
      SELECT count(*)=1
        FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations receipt
        JOIN SCHEMA_TOKEN.okx_demo_canary_consent_handoffs handoff
          ON handoff.handoff_id=receipt.predecessor_handoff_id
        JOIN SCHEMA_TOKEN.okx_demo_submission_grants grant_row
          ON grant_row.grant_id=receipt.predecessor_grant_id
        JOIN SCHEMA_TOKEN.okx_demo_canary_lifecycles lifecycle
          ON lifecycle.lifecycle_id=receipt.lifecycle_id
        JOIN SCHEMA_TOKEN.okx_order_write_attempts attempt
          ON attempt.id=receipt.attempt_id
        JOIN SCHEMA_TOKEN.exchange_orders order_row
          ON order_row.id=receipt.exchange_order_row_id
        LEFT JOIN SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations parent
          ON parent.id=receipt.parent_terminal_receipt_id
       WHERE handoff.handoff_id=p_handoff_id
         AND receipt.absolute_submission_claim IS FALSE
         AND receipt.exchange_result_code='51603'
         AND receipt.exchange_result_state='NOT_FOUND'
         AND receipt.fill_count=0 AND receipt.attempt_count=1
         AND receipt.restart_resubmission_count=0
         AND handoff.status='CONSUMED' AND handoff.grant_id=grant_row.grant_id
         AND grant_row.status='CONSUMED'
         AND attempt.exchange_order_row_id=order_row.id
         AND attempt.approval_id=grant_row.approval_id
         AND receipt.request_digest=attempt.request_digest
         AND attempt.operation='PLACE' AND attempt.attempt_count=1
         AND attempt.state='USER_ACCEPTED_NOT_FOUND_NO_FILL'
         AND attempt.order_state='USER_ACCEPTED_NOT_FOUND_NO_FILL'
         AND order_row.status='USER_ACCEPTED_NOT_FOUND_NO_FILL'
         AND order_row.exchange_order_id IS NULL
         AND lifecycle.submission_grant_id=grant_row.grant_id
         AND lifecycle.opening_exchange_order_row_id=order_row.id
         AND lifecycle.cleanup_phase='TERMINAL' AND lifecycle.outcome='FAILED'
         AND lifecycle.failure_code='USER_ACCEPTED_NOT_FOUND_NO_FILL'
         AND lifecycle.final_reconciliation_run_id IS NULL
         AND lifecycle.accepted_terminalization_id=receipt.id
         AND lifecycle.final_evidence_digest=receipt.acceptance_digest
         AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_fills fill_row
               WHERE fill_row.exchange_order_row_id=order_row.id)
         AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts child
               WHERE child.parent_attempt_id=attempt.id)
         AND ((receipt.receipt_depth=1
                AND SCHEMA_TOKEN.exact_accepted_not_found_predecessor(handoff.handoff_id))
              OR (receipt.receipt_depth=2
                AND receipt.acceptance_kind='USER_ACCEPTED_NOT_FOUND_NO_FILL_V2'
                AND parent.receipt_depth=1
                AND handoff.terminal_receipt_id=parent.id
                AND handoff.supersedes_handoff_id=parent.predecessor_handoff_id
                AND SCHEMA_TOKEN.exact_accepted_not_found_predecessor(
                      parent.predecessor_handoff_id)));
    $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.guard_bounded_accepted_successor_handoff()
    RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE receipt SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations%ROWTYPE;
    BEGIN
      IF TG_OP='UPDATE' AND
         (NEW.terminal_receipt_id IS DISTINCT FROM OLD.terminal_receipt_id
          OR NEW.supersedes_handoff_id IS DISTINCT FROM OLD.supersedes_handoff_id) THEN
        RAISE EXCEPTION 'bounded accepted successor identity is immutable';
      END IF;
      IF TG_OP='INSERT' THEN
        IF NEW.terminal_receipt_id IS NULL THEN
          IF EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations) THEN
            RAISE EXCEPTION 'receipt-bound successor is required';
          END IF;
        ELSE
          SELECT * INTO receipt
            FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
           WHERE id=NEW.terminal_receipt_id;
          IF receipt.id IS NULL OR receipt.receipt_depth NOT IN (1,2)
             OR NEW.supersedes_handoff_id IS DISTINCT FROM receipt.predecessor_handoff_id
             OR NOT SCHEMA_TOKEN.exact_bounded_accepted_not_found_predecessor(
                    receipt.predecessor_handoff_id)
             OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs child
                   WHERE child.terminal_receipt_id=receipt.id) THEN
            RAISE EXCEPTION 'invalid bounded accepted successor';
          END IF;
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    DROP TRIGGER IF EXISTS okx_demo_canary_bounded_accepted_successor_guard
      ON SCHEMA_TOKEN.okx_demo_canary_consent_handoffs;
    CREATE TRIGGER okx_demo_canary_bounded_accepted_successor_guard
      BEFORE INSERT OR UPDATE ON SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
      FOR EACH ROW EXECUTE FUNCTION SCHEMA_TOKEN.guard_bounded_accepted_successor_handoff();

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.okx_demo_canary_consent_eligibility()
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE receipt SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations%ROWTYPE;
            predecessor jsonb;
    BEGIN
      SELECT * INTO receipt FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
        ORDER BY receipt_depth DESC LIMIT 1;
      IF FOUND THEN
        IF SCHEMA_TOKEN.exact_bounded_accepted_not_found_predecessor(
             receipt.predecessor_handoff_id)
           AND NOT EXISTS(SELECT 1
                FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs child
                WHERE child.terminal_receipt_id=receipt.id) THEN
          RETURN jsonb_build_object('eligibility_state','ACCEPTED_SUCCESSOR',
            'handoff_id',receipt.predecessor_handoff_id,
            'terminal_receipt_id',receipt.id,
            'terminal_evidence_digest',receipt.acceptance_digest,
            'source_job_id',receipt.source_job_id);
        END IF;
        RETURN jsonb_build_object('eligibility_state','BLOCKED');
      END IF;
      predecessor:=SCHEMA_TOKEN.eligible_atomic_okx_demo_canary_predecessor();
      RETURN jsonb_build_object('eligibility_state','PRISTINE','predecessor',predecessor);
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.terminalize_second_accepted_not_found_no_fill(
      p_payload jsonb)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE attempt SCHEMA_TOKEN.okx_order_write_attempts%ROWTYPE;
            order_row SCHEMA_TOKEN.exchange_orders%ROWTYPE;
            lifecycle SCHEMA_TOKEN.okx_demo_canary_lifecycles%ROWTYPE;
            grant_row SCHEMA_TOKEN.okx_demo_submission_grants%ROWTYPE;
            handoff SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            parent SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations%ROWTYPE;
            lease SCHEMA_TOKEN.okx_order_writer_leases%ROWTYPE;
            existing SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations%ROWTYPE;
            evidence jsonb; identity jsonb; v_evidence_digest text;
            v_acceptance_digest text; new_receipt_id bigint;
            v_approval_id bigint; v_approval_status text;
            v_chain_id bigint; v_chain_status text;
            v_chain_approval_id bigint; v_chain_target text;
            observed_at timestamptz;
    BEGIN
      PERFORM pg_advisory_xact_lock(5067747289570038600);
      IF jsonb_typeof(p_payload)<>'object'
         OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_payload) key)
              IS DISTINCT FROM ARRAY['acceptance_digest','attempt_id',
                'evidence_digest','evidence_observed_at','evidence_snapshot',
                'expected_fencing_version','lifecycle_id','parent_terminal_receipt_id']::text[]
         OR p_payload->>'acceptance_digest'!~'^[0-9a-f]{64}$'
         OR p_payload->>'evidence_digest'!~'^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid second accepted NOT_FOUND terminalization payload';
      END IF;
      observed_at:=(p_payload->>'evidence_observed_at')::timestamptz;
      evidence:=p_payload->'evidence_snapshot';
      SELECT * INTO parent FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
        WHERE id=(p_payload->>'parent_terminal_receipt_id')::bigint FOR UPDATE;
      SELECT * INTO attempt FROM SCHEMA_TOKEN.okx_order_write_attempts
        WHERE id=(p_payload->>'attempt_id')::bigint FOR UPDATE;
      SELECT * INTO order_row FROM SCHEMA_TOKEN.exchange_orders
        WHERE id=attempt.exchange_order_row_id FOR UPDATE;
      SELECT * INTO lifecycle FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
        WHERE lifecycle_id=p_payload->>'lifecycle_id' FOR UPDATE;
      SELECT * INTO grant_row FROM SCHEMA_TOKEN.okx_demo_submission_grants
        WHERE grant_id=lifecycle.submission_grant_id FOR UPDATE;
      SELECT * INTO handoff FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=grant_row.handoff_id FOR UPDATE;
      SELECT id,status INTO v_approval_id,v_approval_status
        FROM SCHEMA_TOKEN.approved_executions WHERE id=grant_row.approval_id FOR UPDATE;
      SELECT id,status,approved_execution_id,execution_target_id
        INTO v_chain_id,v_chain_status,v_chain_approval_id,v_chain_target
        FROM SCHEMA_TOKEN.full_chain_runs WHERE id=handoff.full_chain_run_id FOR UPDATE;
      SELECT * INTO lease FROM SCHEMA_TOKEN.okx_order_writer_leases
        WHERE execution_target_id='OKX_DEMO' FOR UPDATE;

      SELECT * INTO existing
        FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
       WHERE receipt_depth=2 OR acceptance_digest=p_payload->>'acceptance_digest'
          OR attempt_id=attempt.id OR exchange_order_row_id=order_row.id
          OR lifecycle_id=lifecycle.lifecycle_id OR predecessor_grant_id=grant_row.grant_id
          OR predecessor_handoff_id=handoff.handoff_id FOR UPDATE;
      IF FOUND THEN
        IF existing.receipt_depth=2
           AND existing.parent_terminal_receipt_id=parent.id
           AND existing.acceptance_digest=p_payload->>'acceptance_digest'
           AND existing.attempt_id=attempt.id
           AND existing.exchange_order_row_id=order_row.id
           AND existing.lifecycle_id=lifecycle.lifecycle_id
           AND existing.evidence_digest=p_payload->>'evidence_digest' THEN
          RETURN jsonb_build_object('terminalization_id',existing.id,
            'receipt_depth',2,'acceptance_digest',existing.acceptance_digest,
            'idempotent',true,'absolute_submission_claim',false);
        END IF;
        RAISE EXCEPTION 'second accepted NOT_FOUND terminalization identity drift';
      END IF;

      IF parent.id IS NULL OR parent.receipt_depth<>1
         OR NOT SCHEMA_TOKEN.exact_accepted_not_found_predecessor(
                parent.predecessor_handoff_id)
         OR attempt.id IS NULL OR order_row.id IS NULL OR lifecycle.lifecycle_id IS NULL
         OR grant_row.grant_id IS NULL OR handoff.handoff_id IS NULL
         OR lease.execution_target_id IS NULL OR handoff.source_job_id<>22
         OR handoff.terminal_receipt_id IS DISTINCT FROM parent.id
         OR handoff.supersedes_handoff_id IS DISTINCT FROM parent.predecessor_handoff_id
         OR handoff.status<>'CONSUMED' OR grant_row.status<>'CONSUMED'
         OR handoff.grant_id IS DISTINCT FROM grant_row.grant_id
         OR v_approval_id IS NULL OR v_approval_status<>'ACTIVE'
         OR v_approval_id<>grant_row.approval_id
         OR v_chain_id IS NULL OR v_chain_status<>'EXECUTING'
         OR v_chain_approval_id<>v_approval_id OR v_chain_target<>'OKX_DEMO'
         OR attempt.operation<>'PLACE' OR attempt.state<>'RECOVERY_REQUIRED'
         OR attempt.reason_code<>'EXACT_ORDER_NOT_FOUND' OR attempt.attempt_count<>1
         OR attempt.safe_response_snapshot::jsonb IS DISTINCT FROM
              jsonb_build_object('exact_order_get','NOT_FOUND','okx_code','51603')
         OR attempt.recovery_grant_database_id IS NOT NULL
         OR attempt.parent_attempt_id IS NOT NULL
         OR order_row.exchange_order_id IS NOT NULL
         OR order_row.id<>lifecycle.opening_exchange_order_row_id
         OR lifecycle.cleanup_phase<>'OPENING_SUBMITTED'
         OR lifecycle.outcome<>'PENDING' OR lifecycle.attributed_fill_quantity<>0
         OR lifecycle.fencing_version<>(p_payload->>'expected_fencing_version')::bigint
         OR handoff.consent_deadline_at>=clock_timestamp()
         OR handoff.bundle_expires_at>=clock_timestamp()
         OR grant_row.expires_at>=clock_timestamp()
         OR lifecycle.deadline_at>=clock_timestamp() OR lease.expires_at>=clock_timestamp()
         OR attempt.dispatch_not_after IS NULL OR attempt.dispatch_not_after>=clock_timestamp()
         -- This owner action accepts the immutable, user-approved historical
         -- exact-GET fact; freshness still applies to a later consent, not here.
         OR observed_at IS DISTINCT FROM attempt.last_attempt_at
         OR attempt.last_attempt_at>clock_timestamp()+interval '5 seconds'
         OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_order_write_attempts
               WHERE execution_target_id='OKX_DEMO')<>2
         OR (SELECT count(*) FROM SCHEMA_TOKEN.exchange_orders
               WHERE execution_target_id='OKX_DEMO')<>2
         OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles)<>2
         OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations)<>1
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_fills
               WHERE execution_target_id='OKX_DEMO')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
               WHERE parent_attempt_id=attempt.id)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
               WHERE supersedes_handoff_id=handoff.handoff_id) THEN
        RAISE EXCEPTION 'second accepted NOT_FOUND terminalization precondition mismatch';
      END IF;
      IF EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
             WHERE status IN ('REQUESTED','FINALIZED','GRANT_ISSUED'))
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants WHERE status='ACTIVE')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.approved_executions
             WHERE execution_target_id='OKX_DEMO' AND status='ACTIVE' AND id<>v_approval_id)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.full_chain_runs
             WHERE execution_target_id='OKX_DEMO' AND status='EXECUTING' AND id<>v_chain_id)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
             WHERE lifecycle_id<>lifecycle.lifecycle_id
               AND cleanup_phase NOT IN ('TERMINAL','REVOKED'))
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_positions
             WHERE execution_target_id='OKX_DEMO' AND quantity<>0)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.risk_budgets
             WHERE execution_target_id='OKX_DEMO'
               AND (reserved_notional<>0 OR approved_positions<>0)) THEN
        RAISE EXCEPTION 'second accepted NOT_FOUND terminalization precondition mismatch';
      END IF;
      IF evidence IS DISTINCT FROM jsonb_build_object(
           'absolute_submission_claim',false,'attempt_count',1,
           'client_order_id',attempt.client_order_id,'exchange_result_code','51603',
           'exchange_result_state','NOT_FOUND','fill_count',0,
           'instrument_id',attempt.instrument_id,'query_kind','exact_get',
           'request_digest',attempt.request_digest,'restart_resubmission_count',0) THEN
        RAISE EXCEPTION 'second accepted NOT_FOUND evidence mismatch';
      END IF;
      v_evidence_digest:=encode(public.digest(convert_to(
        SCHEMA_TOKEN.canonical_jsonb_text(evidence),'UTF8'),'sha256'),'hex');
      identity:=jsonb_build_object(
        'acceptance_kind','USER_ACCEPTED_NOT_FOUND_NO_FILL_V2',
        'absolute_submission_claim',false,'attempt_id',attempt.id,
        'evidence_digest',v_evidence_digest,'evidence_observed_at',observed_at,
        'exchange_order_row_id',order_row.id,'lifecycle_id',lifecycle.lifecycle_id,
        'parent_terminal_receipt_id',parent.id,
        'predecessor_grant_id',grant_row.grant_id,
        'predecessor_handoff_id',handoff.handoff_id,'receipt_depth',2,
        'request_digest',attempt.request_digest,'source_job_id',22);
      v_acceptance_digest:=encode(public.digest(convert_to(
        SCHEMA_TOKEN.canonical_jsonb_text(identity),'UTF8'),'sha256'),'hex');
      IF v_evidence_digest IS DISTINCT FROM p_payload->>'evidence_digest'
         OR v_acceptance_digest IS DISTINCT FROM p_payload->>'acceptance_digest' THEN
        RAISE EXCEPTION 'second accepted NOT_FOUND digest mismatch';
      END IF;
      INSERT INTO SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations(
        source_job_id,receipt_depth,parent_terminal_receipt_id,predecessor_handoff_id,
        predecessor_grant_id,lifecycle_id,attempt_id,exchange_order_row_id,
        acceptance_kind,absolute_submission_claim,exchange_result_code,
        exchange_result_state,fill_count,attempt_count,restart_resubmission_count,
        request_digest,evidence_observed_at,evidence_snapshot,evidence_digest,
        acceptance_digest)
      VALUES(22,2,parent.id,handoff.handoff_id,grant_row.grant_id,lifecycle.lifecycle_id,
        attempt.id,order_row.id,'USER_ACCEPTED_NOT_FOUND_NO_FILL_V2',false,
        '51603','NOT_FOUND',0,1,0,attempt.request_digest,observed_at,evidence::json,
        v_evidence_digest,v_acceptance_digest) RETURNING id INTO new_receipt_id;
      UPDATE SCHEMA_TOKEN.okx_order_write_attempts
         SET state='USER_ACCEPTED_NOT_FOUND_NO_FILL',
             order_state='USER_ACCEPTED_NOT_FOUND_NO_FILL',updated_at=clock_timestamp()
       WHERE id=attempt.id AND state='RECOVERY_REQUIRED' AND attempt_count=1;
      IF NOT FOUND THEN RAISE EXCEPTION 'second accepted NOT_FOUND attempt CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.exchange_orders
         SET status='USER_ACCEPTED_NOT_FOUND_NO_FILL',updated_at=clock_timestamp()
       WHERE id=order_row.id AND exchange_order_id IS NULL;
      IF NOT FOUND THEN RAISE EXCEPTION 'second accepted NOT_FOUND order CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.okx_demo_canary_lifecycles
         SET outcome='FAILED',cleanup_phase='TERMINAL',
             failure_code='USER_ACCEPTED_NOT_FOUND_NO_FILL',
             final_reconciliation_run_id=NULL,accepted_terminalization_id=new_receipt_id,
             final_evidence_digest=v_acceptance_digest,
             terminal_at=clock_timestamp(),revoked_at=clock_timestamp(),
             fencing_version=fencing_version+1,updated_at=clock_timestamp()
       WHERE lifecycle_id=lifecycle.lifecycle_id AND cleanup_phase='OPENING_SUBMITTED'
         AND fencing_version=(p_payload->>'expected_fencing_version')::bigint;
      IF NOT FOUND THEN RAISE EXCEPTION 'second accepted NOT_FOUND lifecycle CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.approved_executions SET status='EXPIRED'
       WHERE id=v_approval_id AND status='ACTIVE';
      IF NOT FOUND THEN RAISE EXCEPTION 'second accepted NOT_FOUND approval CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.full_chain_runs
         SET status='BLOCKED',terminal_reason='user accepted second NOT_FOUND no-fill terminal',
             completed_at=clock_timestamp()
       WHERE id=v_chain_id AND status='EXECUTING';
      IF NOT FOUND THEN RAISE EXCEPTION 'second accepted NOT_FOUND full-chain CAS lost'; END IF;
      RETURN jsonb_build_object('terminalization_id',new_receipt_id,'receipt_depth',2,
        'acceptance_digest',v_acceptance_digest,'idempotent',false,
        'absolute_submission_claim',false);
    END $$;
    """.replace("SCHEMA_TOKEN", quoted_schema)
    connection.execute(text(ddl))
    for signature in (
        "exact_bounded_accepted_not_found_predecessor(text)",
        "guard_bounded_accepted_successor_handoff()",
        "terminalize_second_accepted_not_found_no_fill(jsonb)",
    ):
        connection.execute(text(
            "ALTER FUNCTION {0}.{1} OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON FUNCTION {0}.{1} FROM PUBLIC,freqtrade"
            .format(quoted_schema, signature)
        ))
    connection.execute(text(
        "ALTER FUNCTION {0}.okx_demo_canary_consent_eligibility() "
        "OWNER TO freqtrade_ai_attestor; REVOKE ALL ON FUNCTION "
        "{0}.okx_demo_canary_consent_eligibility() FROM PUBLIC; "
        "GRANT EXECUTE ON FUNCTION {0}.okx_demo_canary_consent_eligibility() "
        "TO freqtrade".format(quoted_schema)
    ))
    connection.execute(text(
        "ALTER TABLE {0}.okx_demo_accepted_not_found_terminalizations "
        "OWNER TO freqtrade_ai_attestor; REVOKE ALL ON TABLE "
        "{0}.okx_demo_accepted_not_found_terminalizations FROM PUBLIC,freqtrade"
        .format(quoted_schema)
    ))


def _add_final_accepted_not_found_boundary(connection: Connection) -> None:
    """Install v34's owner-only, non-successorable final zero-fill receipt."""

    schema_name = connection.execute(text("SELECT current_schema()" )).scalar_one()
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(schema_name)
    ddl = r"""
    ALTER TABLE SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_kind_check,
      DROP CONSTRAINT IF EXISTS okx_demo_accepted_not_found_identity_check,
      ADD CONSTRAINT okx_demo_accepted_not_found_kind_check CHECK(
        (receipt_depth=1 AND parent_terminal_receipt_id IS NULL
          AND acceptance_kind='USER_ACCEPTED_NOT_FOUND_NO_FILL_V1')
        OR (receipt_depth=2 AND parent_terminal_receipt_id IS NOT NULL
          AND acceptance_kind='USER_ACCEPTED_NOT_FOUND_NO_FILL_V2')
        OR (receipt_depth=3 AND parent_terminal_receipt_id IS NOT NULL
          AND acceptance_kind='USER_ACCEPTED_NOT_FOUND_NO_FILL_FINAL_V1')),
      ADD CONSTRAINT okx_demo_accepted_not_found_identity_check CHECK(
        source_job_id=22 AND receipt_depth IN (1,2,3)
        AND length(request_digest)=64 AND length(evidence_digest)=64
        AND length(acceptance_digest)=64);

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.terminalize_final_accepted_not_found_no_fill(
      p_payload jsonb)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE attempt SCHEMA_TOKEN.okx_order_write_attempts%ROWTYPE;
            order_row SCHEMA_TOKEN.exchange_orders%ROWTYPE;
            lifecycle SCHEMA_TOKEN.okx_demo_canary_lifecycles%ROWTYPE;
            grant_row SCHEMA_TOKEN.okx_demo_submission_grants%ROWTYPE;
            handoff SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            parent SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations%ROWTYPE;
            lease SCHEMA_TOKEN.okx_order_writer_leases%ROWTYPE;
            existing SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations%ROWTYPE;
            evidence jsonb; identity jsonb; v_evidence_digest text;
            v_acceptance_digest text; new_receipt_id bigint;
            v_approval_id bigint; v_approval_status text;
            v_chain_id bigint; v_chain_status text;
            v_chain_approval_id bigint; v_chain_target text;
            observed_at timestamptz;
    BEGIN
      PERFORM pg_advisory_xact_lock(5067747289570038600);
      IF jsonb_typeof(p_payload)<>'object'
         OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_payload) key)
              IS DISTINCT FROM ARRAY['acceptance_digest','attempt_id',
                'evidence_digest','evidence_observed_at','evidence_snapshot',
                'expected_fencing_version','lifecycle_id','parent_terminal_receipt_id']::text[]
         OR p_payload->>'acceptance_digest'!~'^[0-9a-f]{64}$'
         OR p_payload->>'evidence_digest'!~'^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid final accepted NOT_FOUND terminalization payload';
      END IF;
      observed_at:=(p_payload->>'evidence_observed_at')::timestamptz;
      evidence:=p_payload->'evidence_snapshot';
      SELECT * INTO parent FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
        WHERE id=(p_payload->>'parent_terminal_receipt_id')::bigint FOR UPDATE;
      SELECT * INTO attempt FROM SCHEMA_TOKEN.okx_order_write_attempts
        WHERE id=(p_payload->>'attempt_id')::bigint FOR UPDATE;
      SELECT * INTO order_row FROM SCHEMA_TOKEN.exchange_orders
        WHERE id=attempt.exchange_order_row_id FOR UPDATE;
      SELECT * INTO lifecycle FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
        WHERE lifecycle_id=p_payload->>'lifecycle_id' FOR UPDATE;
      SELECT * INTO grant_row FROM SCHEMA_TOKEN.okx_demo_submission_grants
        WHERE grant_id=lifecycle.submission_grant_id FOR UPDATE;
      SELECT * INTO handoff FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=grant_row.handoff_id FOR UPDATE;
      SELECT id,status INTO v_approval_id,v_approval_status
        FROM SCHEMA_TOKEN.approved_executions WHERE id=grant_row.approval_id FOR UPDATE;
      SELECT id,status,approved_execution_id,execution_target_id
        INTO v_chain_id,v_chain_status,v_chain_approval_id,v_chain_target
        FROM SCHEMA_TOKEN.full_chain_runs WHERE id=handoff.full_chain_run_id FOR UPDATE;
      SELECT * INTO lease FROM SCHEMA_TOKEN.okx_order_writer_leases
        WHERE execution_target_id='OKX_DEMO' FOR UPDATE;

      SELECT * INTO existing
        FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
       WHERE receipt_depth=3 OR acceptance_digest=p_payload->>'acceptance_digest'
          OR attempt_id=attempt.id OR exchange_order_row_id=order_row.id
          OR lifecycle_id=lifecycle.lifecycle_id OR predecessor_grant_id=grant_row.grant_id
          OR predecessor_handoff_id=handoff.handoff_id FOR UPDATE;
      IF FOUND THEN
        IF existing.receipt_depth=3
           AND existing.parent_terminal_receipt_id=parent.id
           AND existing.acceptance_digest=p_payload->>'acceptance_digest'
           AND existing.attempt_id=attempt.id
           AND existing.exchange_order_row_id=order_row.id
           AND existing.lifecycle_id=lifecycle.lifecycle_id
           AND existing.evidence_digest=p_payload->>'evidence_digest' THEN
          RETURN jsonb_build_object('terminalization_id',existing.id,
            'receipt_depth',3,'acceptance_digest',existing.acceptance_digest,
            'idempotent',true,'absolute_submission_claim',false,
            'successor_allowed',false);
        END IF;
        RAISE EXCEPTION 'final accepted NOT_FOUND terminalization identity drift';
      END IF;

      IF parent.id IS NULL OR parent.receipt_depth<>2
         OR parent.acceptance_kind<>'USER_ACCEPTED_NOT_FOUND_NO_FILL_V2'
         OR NOT SCHEMA_TOKEN.exact_bounded_accepted_not_found_predecessor(
                parent.predecessor_handoff_id)
         OR attempt.id IS NULL OR order_row.id IS NULL OR lifecycle.lifecycle_id IS NULL
         OR grant_row.grant_id IS NULL OR handoff.handoff_id IS NULL
         OR lease.execution_target_id IS NULL OR handoff.source_job_id<>22
         OR handoff.terminal_receipt_id IS DISTINCT FROM parent.id
         OR handoff.supersedes_handoff_id IS DISTINCT FROM parent.predecessor_handoff_id
         OR handoff.status<>'CONSUMED' OR grant_row.status<>'CONSUMED'
         OR handoff.grant_id IS DISTINCT FROM grant_row.grant_id
         OR v_approval_id IS NULL OR v_approval_status<>'ACTIVE'
         OR v_approval_id<>grant_row.approval_id
         OR v_chain_id IS NULL OR v_chain_status<>'EXECUTING'
         OR v_chain_approval_id<>v_approval_id OR v_chain_target<>'OKX_DEMO'
         OR attempt.operation<>'PLACE' OR attempt.state<>'RECOVERY_REQUIRED'
         OR attempt.reason_code<>'EXACT_ORDER_NOT_FOUND' OR attempt.attempt_count<>1
         OR attempt.safe_response_snapshot::jsonb IS DISTINCT FROM
              jsonb_build_object('exact_order_get','NOT_FOUND','okx_code','51603')
         OR attempt.recovery_grant_database_id IS NOT NULL
         OR attempt.parent_attempt_id IS NOT NULL
         OR order_row.exchange_order_id IS NOT NULL
         OR order_row.id<>lifecycle.opening_exchange_order_row_id
         OR lifecycle.cleanup_phase<>'OPENING_SUBMITTED'
         OR lifecycle.outcome<>'PENDING' OR lifecycle.attributed_fill_quantity<>0
         OR lifecycle.fencing_version<>(p_payload->>'expected_fencing_version')::bigint
         OR handoff.consent_deadline_at>=clock_timestamp()
         OR handoff.bundle_expires_at>=clock_timestamp()
         OR grant_row.expires_at>=clock_timestamp()
         OR lifecycle.deadline_at>=clock_timestamp() OR lease.expires_at>=clock_timestamp()
         OR attempt.dispatch_not_after IS NULL OR attempt.dispatch_not_after>=clock_timestamp()
         OR observed_at IS DISTINCT FROM attempt.last_attempt_at
         OR attempt.last_attempt_at>clock_timestamp()+interval '5 seconds'
         OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_order_write_attempts
               WHERE execution_target_id='OKX_DEMO')<>3
         OR (SELECT count(*) FROM SCHEMA_TOKEN.exchange_orders
               WHERE execution_target_id='OKX_DEMO')<>3
         OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles)<>3
         OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations)<>2
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_fills
               WHERE execution_target_id='OKX_DEMO')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
               WHERE parent_attempt_id=attempt.id)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
               WHERE supersedes_handoff_id=handoff.handoff_id) THEN
        RAISE EXCEPTION 'final accepted NOT_FOUND terminalization precondition mismatch';
      END IF;
      IF EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
             WHERE status IN ('REQUESTED','FINALIZED','GRANT_ISSUED'))
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants WHERE status='ACTIVE')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.approved_executions
             WHERE execution_target_id='OKX_DEMO' AND status='ACTIVE' AND id<>v_approval_id)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.full_chain_runs
             WHERE execution_target_id='OKX_DEMO' AND status='EXECUTING' AND id<>v_chain_id)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
             WHERE lifecycle_id<>lifecycle.lifecycle_id
               AND cleanup_phase NOT IN ('TERMINAL','REVOKED'))
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_positions
             WHERE execution_target_id='OKX_DEMO' AND quantity<>0)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.risk_budgets
             WHERE execution_target_id='OKX_DEMO'
               AND (reserved_notional<>0 OR approved_positions<>0)) THEN
        RAISE EXCEPTION 'final accepted NOT_FOUND terminalization precondition mismatch';
      END IF;
      IF evidence IS DISTINCT FROM jsonb_build_object(
           'absolute_submission_claim',false,'attempt_count',1,
           'client_order_id',attempt.client_order_id,'exchange_result_code','51603',
           'exchange_result_state','NOT_FOUND','fill_count',0,
           'instrument_id',attempt.instrument_id,'query_kind','exact_get',
           'request_digest',attempt.request_digest,'restart_resubmission_count',0) THEN
        RAISE EXCEPTION 'final accepted NOT_FOUND evidence mismatch';
      END IF;
      v_evidence_digest:=encode(public.digest(convert_to(
        SCHEMA_TOKEN.canonical_jsonb_text(evidence),'UTF8'),'sha256'),'hex');
      identity:=jsonb_build_object(
        'acceptance_kind','USER_ACCEPTED_NOT_FOUND_NO_FILL_FINAL_V1',
        'absolute_submission_claim',false,'attempt_id',attempt.id,
        'evidence_digest',v_evidence_digest,'evidence_observed_at',observed_at,
        'exchange_order_row_id',order_row.id,'lifecycle_id',lifecycle.lifecycle_id,
        'parent_terminal_receipt_id',parent.id,
        'predecessor_grant_id',grant_row.grant_id,
        'predecessor_handoff_id',handoff.handoff_id,'receipt_depth',3,
        'request_digest',attempt.request_digest,'source_job_id',22,
        'successor_allowed',false);
      v_acceptance_digest:=encode(public.digest(convert_to(
        SCHEMA_TOKEN.canonical_jsonb_text(identity),'UTF8'),'sha256'),'hex');
      IF v_evidence_digest IS DISTINCT FROM p_payload->>'evidence_digest'
         OR v_acceptance_digest IS DISTINCT FROM p_payload->>'acceptance_digest' THEN
        RAISE EXCEPTION 'final accepted NOT_FOUND digest mismatch';
      END IF;
      INSERT INTO SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations(
        source_job_id,receipt_depth,parent_terminal_receipt_id,predecessor_handoff_id,
        predecessor_grant_id,lifecycle_id,attempt_id,exchange_order_row_id,
        acceptance_kind,absolute_submission_claim,exchange_result_code,
        exchange_result_state,fill_count,attempt_count,restart_resubmission_count,
        request_digest,evidence_observed_at,evidence_snapshot,evidence_digest,
        acceptance_digest)
      VALUES(22,3,parent.id,handoff.handoff_id,grant_row.grant_id,lifecycle.lifecycle_id,
        attempt.id,order_row.id,'USER_ACCEPTED_NOT_FOUND_NO_FILL_FINAL_V1',false,
        '51603','NOT_FOUND',0,1,0,attempt.request_digest,observed_at,evidence::json,
        v_evidence_digest,v_acceptance_digest) RETURNING id INTO new_receipt_id;
      UPDATE SCHEMA_TOKEN.okx_order_write_attempts
         SET state='USER_ACCEPTED_NOT_FOUND_NO_FILL',
             order_state='USER_ACCEPTED_NOT_FOUND_NO_FILL',updated_at=clock_timestamp()
       WHERE id=attempt.id AND state='RECOVERY_REQUIRED' AND attempt_count=1;
      IF NOT FOUND THEN RAISE EXCEPTION 'final accepted NOT_FOUND attempt CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.exchange_orders
         SET status='USER_ACCEPTED_NOT_FOUND_NO_FILL',updated_at=clock_timestamp()
       WHERE id=order_row.id AND exchange_order_id IS NULL;
      IF NOT FOUND THEN RAISE EXCEPTION 'final accepted NOT_FOUND order CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.okx_demo_canary_lifecycles
         SET outcome='FAILED',cleanup_phase='TERMINAL',
             failure_code='USER_ACCEPTED_NOT_FOUND_NO_FILL',
             final_reconciliation_run_id=NULL,accepted_terminalization_id=new_receipt_id,
             final_evidence_digest=v_acceptance_digest,
             terminal_at=clock_timestamp(),revoked_at=clock_timestamp(),
             fencing_version=fencing_version+1,updated_at=clock_timestamp()
       WHERE lifecycle_id=lifecycle.lifecycle_id AND cleanup_phase='OPENING_SUBMITTED'
         AND fencing_version=(p_payload->>'expected_fencing_version')::bigint;
      IF NOT FOUND THEN RAISE EXCEPTION 'final accepted NOT_FOUND lifecycle CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.approved_executions SET status='EXPIRED'
       WHERE id=v_approval_id AND status='ACTIVE';
      IF NOT FOUND THEN RAISE EXCEPTION 'final accepted NOT_FOUND approval CAS lost'; END IF;
      UPDATE SCHEMA_TOKEN.full_chain_runs
         SET status='BLOCKED',terminal_reason='user accepted final NOT_FOUND no-fill terminal',
             completed_at=clock_timestamp()
       WHERE id=v_chain_id AND status='EXECUTING';
      IF NOT FOUND THEN RAISE EXCEPTION 'final accepted NOT_FOUND full-chain CAS lost'; END IF;
      RETURN jsonb_build_object('terminalization_id',new_receipt_id,'receipt_depth',3,
        'acceptance_digest',v_acceptance_digest,'idempotent',false,
        'absolute_submission_claim',false,'successor_allowed',false);
    END $$;
    """.replace("SCHEMA_TOKEN", quoted_schema)
    connection.execute(text(ddl))
    connection.execute(text(
        "ALTER FUNCTION {0}.terminalize_final_accepted_not_found_no_fill(jsonb) "
        "OWNER TO freqtrade_ai_attestor; REVOKE ALL ON FUNCTION "
        "{0}.terminalize_final_accepted_not_found_no_fill(jsonb) "
        "FROM PUBLIC,freqtrade".format(quoted_schema)
    ))


def _add_atomic_canary_prepare_boundary(connection: Connection) -> None:
    """Install v31's consent successor and durable dispatch fence."""

    schema_name = connection.execute(text("SELECT current_schema()" )).scalar_one()
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(schema_name)
    ddl = r"""
    ALTER TABLE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
      ADD COLUMN IF NOT EXISTS supersedes_handoff_id varchar(32);
    ALTER TABLE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
      DROP CONSTRAINT IF EXISTS okx_demo_canary_consent_supersedes_fkey,
      DROP CONSTRAINT IF EXISTS okx_demo_canary_consent_supersedes_unique,
      ADD CONSTRAINT okx_demo_canary_consent_supersedes_fkey
        FOREIGN KEY(supersedes_handoff_id)
        REFERENCES SCHEMA_TOKEN.okx_demo_canary_consent_handoffs(handoff_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
      ADD CONSTRAINT okx_demo_canary_consent_supersedes_unique
        UNIQUE(supersedes_handoff_id);

    ALTER TABLE SCHEMA_TOKEN.okx_order_write_attempts
      ADD COLUMN IF NOT EXISTS dispatch_not_after timestamptz;
    ALTER TABLE SCHEMA_TOKEN.okx_order_write_attempts
      DROP CONSTRAINT IF EXISTS okx_order_write_attempts_state_check,
      ADD CONSTRAINT okx_order_write_attempts_state_check CHECK(
        state IN ('PREPARED','DISPATCHED','ACKNOWLEDGED','REJECTED',
                  'RECOVERY_REQUIRED','RESIDUAL_CLOSE_REQUIRED','RECONCILED',
                  'USER_ACCEPTED_NOT_FOUND_NO_FILL'));
    DROP INDEX IF EXISTS SCHEMA_TOKEN.okx_order_write_attempts_one_unresolved_target_idx;
    CREATE UNIQUE INDEX okx_order_write_attempts_one_unresolved_target_idx
      ON SCHEMA_TOKEN.okx_order_write_attempts(execution_target_id)
      WHERE state IN ('PREPARED','DISPATCHED','ACKNOWLEDGED',
                      'RECOVERY_REQUIRED','RESIDUAL_CLOSE_REQUIRED');

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.pending_okx_demo_canary_consent()
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
    BEGIN
      PERFORM SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret();
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
       WHERE status='REQUESTED' ORDER BY consented_at,handoff_id LIMIT 1 FOR UPDATE;
      IF NOT FOUND THEN RETURN NULL; END IF;
      IF h.consent_deadline_at<=statement_timestamp()+interval '15 seconds' THEN
        UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET status='EXPIRED',
          failure_code='INSUFFICIENT_CAPTURE_BUDGET',updated_at=statement_timestamp()
          WHERE handoff_id=h.handoff_id;
        RETURN jsonb_build_object('handoff_id',h.handoff_id,'status','EXPIRED');
      END IF;
      RETURN jsonb_build_object('handoff_id',h.handoff_id,
        'status','REQUESTED','source_job_id',h.source_job_id,
        'source_ancestry',h.source_ancestry::jsonb,
        'supersedes_handoff_id',h.supersedes_handoff_id,
        'idempotency_key_digest',h.idempotency_key_digest,
        'instrument_id',h.instrument_id,'max_notional',h.max_notional::text,
        'consent_deadline_at',h.consent_deadline_at);
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.eligible_atomic_okx_demo_canary_predecessor()
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            a SCHEMA_TOKEN.approved_executions%ROWTYPE;
            receipt SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations%ROWTYPE;
    BEGIN
      SELECT * INTO receipt FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
        ORDER BY receipt_depth DESC LIMIT 1;
      IF FOUND THEN
        IF SCHEMA_TOKEN.exact_bounded_accepted_not_found_predecessor(
             receipt.predecessor_handoff_id)
           AND NOT EXISTS(SELECT 1
                FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs child
                WHERE child.terminal_receipt_id=receipt.id) THEN
          RETURN jsonb_build_object('handoff_id',receipt.predecessor_handoff_id,
            'source_job_id',receipt.source_job_id,
            'terminal_receipt_id',receipt.id,
            'terminal_evidence_digest',receipt.acceptance_digest);
        END IF;
        RETURN NULL;
      END IF;
      IF (SELECT count(*) FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
          WHERE status='EXPIRED' AND failure_code='FINALIZED_EVIDENCE_EXPIRED'
            AND grant_id IS NULL)<>1 THEN RETURN NULL; END IF;
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE status='EXPIRED' AND failure_code='FINALIZED_EVIDENCE_EXPIRED'
          AND grant_id IS NULL ORDER BY created_at DESC LIMIT 1;
      SELECT * INTO a FROM SCHEMA_TOKEN.approved_executions WHERE id=h.approval_id;
      IF h.handoff_id IS NULL OR h.runtime_instance_id IS NULL
         OR h.finalized_at IS NULL OR h.approval_id IS NULL
         OR a.id IS NULL OR a.status<>'EXPIRED'
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
              WHERE supersedes_handoff_id=h.handoff_id)
         OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.full_chain_runs f
              WHERE f.id=h.full_chain_run_id AND f.approved_execution_id=a.id
                AND f.execution_target_id='OKX_DEMO' AND f.status='BLOCKED')
         OR (SELECT count(*) FROM SCHEMA_TOKEN.trade_intents i
              WHERE i.execution_target_id='OKX_DEMO'
                AND i.request_snapshot::jsonb->>'provenance'=
                    'CONTROLLED_CANARY_NON_PRODUCTION')<>1
         OR (SELECT count(*) FROM SCHEMA_TOKEN.risk_decisions d
              JOIN SCHEMA_TOKEN.trade_intents i ON i.id=d.trade_intent_id
              WHERE i.execution_target_id='OKX_DEMO'
                AND i.request_snapshot::jsonb->>'provenance'=
                    'CONTROLLED_CANARY_NON_PRODUCTION')<>1
         OR (SELECT count(*) FROM SCHEMA_TOKEN.approved_executions x
              JOIN SCHEMA_TOKEN.trade_intents i ON i.id=x.trade_intent_id
              WHERE i.execution_target_id='OKX_DEMO'
                AND i.request_snapshot::jsonb->>'provenance'=
                    'CONTROLLED_CANARY_NON_PRODUCTION')<>1
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
              WHERE execution_target_id='OKX_DEMO')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_orders
              WHERE execution_target_id='OKX_DEMO')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_fills
              WHERE execution_target_id='OKX_DEMO')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.risk_budgets
              WHERE execution_target_id='OKX_DEMO'
                AND (reserved_notional<>0 OR approved_positions<>0)) THEN
        RETURN NULL;
      END IF;
      RETURN jsonb_build_object('handoff_id',h.handoff_id,
        'source_job_id',h.source_job_id);
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.request_atomic_okx_demo_canary_consent(
      p_idempotency_digest text,p_nonce text,p_payload text,p_proof text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE source SCHEMA_TOKEN.research_jobs%ROWTYPE;
            predecessor SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            existing SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            eligible jsonb; new_id text; v_consent_digest text;
            payload_digest text; proof_key bytea; expected_payload jsonb;
            v_terminal_receipt_id bigint;
    BEGIN
      IF NOT pg_try_advisory_xact_lock(5067747289570038601) THEN
        RAISE EXCEPTION 'controlled canary consent request lock is busy';
      END IF;
      eligible:=SCHEMA_TOKEN.eligible_atomic_okx_demo_canary_predecessor();
      IF eligible IS NULL THEN
        RAISE EXCEPTION 'atomic canary predecessor is unavailable';
      END IF;
      SELECT * INTO predecessor FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=eligible->>'handoff_id' FOR UPDATE;
      v_terminal_receipt_id:=(eligible->>'terminal_receipt_id')::bigint;
      IF v_terminal_receipt_id IS NOT NULL THEN
        expected_payload:=jsonb_build_object(
          'authorization','once','consent_policy','accepted-not-found-successor-v1',
          'execution_target','OKX_DEMO','idempotency_key_digest',p_idempotency_digest,
          'instrument_id','BTC-USDT-SWAP','max_notional','20',
          'operation','okx-demo-canary-consent-finalize',
          'source_ancestry','[15,16,17,18,19,20,21,22]'::jsonb,
          'source_job_id',22,'supersedes_handoff_id',predecessor.handoff_id,
          'terminal_receipt_id',v_terminal_receipt_id,
          'terminal_evidence_digest',eligible->>'terminal_evidence_digest');
      ELSE
        expected_payload:=jsonb_build_object(
          'authorization','once','consent_policy','atomic-prepared-v1',
          'execution_target','OKX_DEMO','idempotency_key_digest',p_idempotency_digest,
          'instrument_id','BTC-USDT-SWAP','max_notional','20',
          'operation','okx-demo-canary-consent-finalize',
          'source_ancestry','[15,16,17,18,19,20,21,22]'::jsonb,
          'source_job_id',22,'supersedes_handoff_id',predecessor.handoff_id);
      END IF;
      IF p_idempotency_digest!~'^[0-9a-f]{64}$'
         OR p_nonce!~'^[0-9a-f]{64}$' OR p_proof!~'^[0-9a-f]{64}$'
         OR p_payload::jsonb IS DISTINCT FROM expected_payload THEN
        RAISE EXCEPTION 'invalid atomic canary consent identity';
      END IF;
      SELECT hmac_key INTO proof_key
        FROM SCHEMA_TOKEN.okx_demo_operator_consent_secrets WHERE secret_id='ACTIVE';
      IF proof_key IS NULL OR NOT public.hmac(
           convert_to(p_payload||'|'||p_nonce,'UTF8'),proof_key,'sha256')
           =decode(p_proof,'hex') THEN
        RAISE EXCEPTION 'invalid controlled canary operator proof';
      END IF;
      payload_digest:=encode(public.digest(convert_to(p_payload,'UTF8'),'sha256'),'hex');
      v_consent_digest:=encode(public.digest(convert_to(
        payload_digest||'|'||p_nonce||'|'||p_proof,'UTF8'),'sha256'),'hex');
      SELECT * INTO existing FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE idempotency_key_digest=p_idempotency_digest
           OR consent_digest=v_consent_digest OR consent_nonce=p_nonce FOR UPDATE;
      IF FOUND THEN
        IF existing.idempotency_key_digest IS DISTINCT FROM p_idempotency_digest THEN
          RAISE EXCEPTION 'controlled canary consent identity conflict';
        END IF;
        RETURN jsonb_build_object('handoff_id',existing.handoff_id,
          'status',existing.status,'source_job_id',existing.source_job_id,
          'consent_deadline_at',existing.consent_deadline_at);
      END IF;
      IF EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
          WHERE status IN ('REQUESTED','FINALIZED','GRANT_ISSUED')) THEN
        RAISE EXCEPTION 'active controlled canary consent already exists';
      END IF;
      SELECT * INTO source FROM SCHEMA_TOKEN.research_jobs WHERE id=22
        AND execution_scope_id='LOCAL_DRY_RUN'
        AND status='SUCCESS' AND stage='CANARY_SNAPSHOTS_READY'
        AND operation='okx_demo.execution_chain_canary'
        AND request_payload::jsonb->>'provenance'='CONTROLLED_CANARY_NON_PRODUCTION'
        AND request_payload::jsonb->>'execution_target'='OKX_DEMO'
        AND request_payload::jsonb->>'instrument_id'='BTC-USDT-SWAP'
        AND request_payload::jsonb->>'entry_kind'='FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY'
        AND request_payload::jsonb->>'recovery_of_job_id'='21'
        AND request_payload::jsonb->'supersedes_job_ids'='[15,16,17,18,19,20,21]'::jsonb;
      IF NOT FOUND OR predecessor.source_job_id<>source.id THEN
        RAISE EXCEPTION 'immutable canary source is unavailable';
      END IF;
      IF EXISTS(SELECT 1 FROM SCHEMA_TOKEN.research_jobs
          WHERE id>22 AND operation='okx_demo.execution_chain_canary') THEN
        RAISE EXCEPTION 'successor canary source already exists';
      END IF;
      new_id:=left(encode(public.digest(convert_to(
        p_idempotency_digest||'|'||v_consent_digest||'|'||predecessor.handoff_id,
        'UTF8'),'sha256'),'hex'),32);
      INSERT INTO SCHEMA_TOKEN.okx_demo_canary_consent_handoffs(
        handoff_id,execution_target_id,source_job_id,supersedes_handoff_id,terminal_receipt_id,
        source_ancestry,source_fingerprint,idempotency_key_digest,consent_nonce,
        consent_payload_digest,consent_digest,provenance,instrument_id,max_notional,
        status,snapshot_binding,consented_at,consent_deadline_at,created_at,updated_at)
      VALUES(new_id,'OKX_DEMO',source.id,predecessor.handoff_id,v_terminal_receipt_id,
        predecessor.source_ancestry,predecessor.source_fingerprint,
        p_idempotency_digest,p_nonce,payload_digest,v_consent_digest,
        'CONTROLLED_CANARY_NON_PRODUCTION','BTC-USDT-SWAP',20,'REQUESTED','{}'::json,
        statement_timestamp(),statement_timestamp()+interval '60 seconds',
        statement_timestamp(),statement_timestamp());
      RETURN jsonb_build_object('handoff_id',new_id,'status','REQUESTED',
        'source_job_id',source.id,
        'consent_deadline_at',statement_timestamp()+interval '60 seconds');
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.finalize_atomic_okx_demo_canary_consent(
      p_handoff_id text,p_runtime_id text,p_audit_job_id bigint,
      p_full_chain_run_id bigint,p_approval_id bigint,
      p_reconciliation_run_id bigint,p_binding jsonb)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            predecessor SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            a SCHEMA_TOKEN.approved_executions%ROWTYPE;
            j SCHEMA_TOKEN.research_jobs%ROWTYPE;
            instrument SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            market SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            account SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            v_bundle_digest text;
    BEGIN
      PERFORM SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret();
      PERFORM SCHEMA_TOKEN.claim_okx_demo_canary_consent(
        p_handoff_id,p_runtime_id,p_reconciliation_run_id,p_binding);
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=p_handoff_id FOR UPDATE;
      SELECT * INTO predecessor FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=h.supersedes_handoff_id;
      SELECT * INTO a FROM SCHEMA_TOKEN.approved_executions WHERE id=p_approval_id;
      SELECT * INTO j FROM SCHEMA_TOKEN.research_jobs WHERE id=p_audit_job_id;
      SELECT * INTO instrument FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(p_binding#>>'{instrument,database_id}')::bigint;
      SELECT * INTO market FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(p_binding#>>'{market,database_id}')::bigint;
      SELECT * INTO account FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(p_binding#>>'{account,database_id}')::bigint;
      IF h.status<>'REQUESTED' OR h.supersedes_handoff_id IS NULL
         OR NOT ((predecessor.status='EXPIRED'
                  AND predecessor.failure_code='FINALIZED_EVIDENCE_EXPIRED'
                  AND predecessor.grant_id IS NULL
                  AND h.terminal_receipt_id IS NULL)
                 OR (h.terminal_receipt_id IS NOT NULL
                     AND SCHEMA_TOKEN.exact_bounded_accepted_not_found_predecessor(
                           predecessor.handoff_id)))
         OR h.consent_deadline_at<=clock_timestamp()+interval '1 second'
         OR instrument.expires_at<=clock_timestamp()+interval '1 second'
         OR market.expires_at<=clock_timestamp()+interval '1 second'
         OR account.expires_at<=clock_timestamp()+interval '1 second'
         OR instrument.observed_at<h.consented_at
         OR market.observed_at<h.consented_at OR account.observed_at<h.consented_at
         OR a.id IS NULL OR a.status<>'ACTIVE' OR a.execution_target_id<>'OKX_DEMO'
         OR j.id IS NULL OR j.operation<>'okx_demo_canary_consent_execution_audit'
         OR a.instrument_snapshot_id IS DISTINCT FROM p_binding#>>'{instrument,snapshot_id}'
         OR a.market_snapshot_id IS DISTINCT FROM p_binding#>>'{market,snapshot_id}'
         OR a.account_snapshot_id IS DISTINCT FROM p_binding#>>'{account,snapshot_id}'
         OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.full_chain_runs f
              WHERE f.id=p_full_chain_run_id AND f.research_job_id=j.id
                AND f.approved_execution_id=a.id AND f.execution_target_id='OKX_DEMO') THEN
        RAISE EXCEPTION 'atomic canary consent finalization mismatch';
      END IF;
      v_bundle_digest:=encode(public.digest(convert_to(
        SCHEMA_TOKEN.canonical_jsonb_text(p_binding),'UTF8'),'sha256'),'hex');
      UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET status='FINALIZED',
        runtime_instance_id=p_runtime_id,reconciliation_run_id=p_reconciliation_run_id,
        attested_session_id=instrument.attested_session_id,snapshot_binding=p_binding::json,
        bundle_digest=v_bundle_digest,
        bundle_observed_at=GREATEST(instrument.observed_at,market.observed_at,account.observed_at),
        bundle_expires_at=LEAST(instrument.expires_at,market.expires_at,account.expires_at),
        audit_job_id=j.id,full_chain_run_id=p_full_chain_run_id,approval_id=a.id,
        finalized_at=clock_timestamp(),updated_at=clock_timestamp()
        WHERE handoff_id=h.handoff_id;
      RETURN TRUE;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.issue_atomic_okx_demo_submission_grant(
      p_payload jsonb)
    RETURNS varchar LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            predecessor SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            a SCHEMA_TOKEN.approved_executions%ROWTYPE;
            i SCHEMA_TOKEN.trade_intents%ROWTYPE;
            d SCHEMA_TOKEN.risk_decisions%ROWTYPE;
            r SCHEMA_TOKEN.reconciliation_runs%ROWTYPE;
            s SCHEMA_TOKEN.okx_demo_reconciliation_states%ROWTYPE;
            instrument SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            market SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            account SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            new_id text; expires timestamptz; computed_notional numeric;
            computed_request_digest text;
    BEGIN
      IF NOT pg_try_advisory_xact_lock(5067747289570038600) THEN
        RAISE EXCEPTION 'controlled canary coordination lock is busy';
      END IF;
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=p_payload->>'handoff_id' FOR UPDATE;
      SELECT * INTO predecessor FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=h.supersedes_handoff_id;
      SELECT * INTO a FROM SCHEMA_TOKEN.approved_executions WHERE id=h.approval_id;
      SELECT * INTO i FROM SCHEMA_TOKEN.trade_intents WHERE id=a.trade_intent_id;
      SELECT * INTO d FROM SCHEMA_TOKEN.risk_decisions WHERE id=a.risk_decision_id;
      SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs WHERE id=h.reconciliation_run_id;
      SELECT * INTO s FROM SCHEMA_TOKEN.okx_demo_reconciliation_states
        WHERE execution_target_id='OKX_DEMO';
      SELECT * INTO instrument FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(h.snapshot_binding::jsonb#>>'{instrument,database_id}')::bigint;
      SELECT * INTO market FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(h.snapshot_binding::jsonb#>>'{market,database_id}')::bigint;
      SELECT * INTO account FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(h.snapshot_binding::jsonb#>>'{account,database_id}')::bigint;
      new_id:=p_payload->>'grant_id'; expires:=(p_payload->>'expires_at')::timestamptz;
      computed_notional:=i.quantity*(instrument.content_json::jsonb->>'ctVal')::numeric*
        GREATEST((market.content_json::jsonb->>'reference_price')::numeric,
          (market.content_json::jsonb#>>'{bbo,ask_price}')::numeric,COALESCE(i.limit_price,0));
      computed_request_digest:=encode(public.digest(convert_to(
        SCHEMA_TOKEN.canonical_jsonb_text(jsonb_build_object(
          'approval_id',a.id,'approved_payload_hash',a.approved_payload_hash,
          'canary_notional',SCHEMA_TOKEN.canonical_decimal_text(computed_notional),
          'canary_quantity',SCHEMA_TOKEN.canonical_decimal_text(i.quantity),
          'canonical_hash',a.canonical_hash,'client_order_id',a.client_order_id,
          'instrument_id',i.instrument_id,'policy_digest',a.policy_digest,
          'provenance','CONTROLLED_CANARY_NON_PRODUCTION',
          'reconciliation_run_id',r.id)),'UTF8'),'sha256'),'hex');
      IF new_id!~'^[0-9a-f]{32}$' OR h.status<>'FINALIZED'
         OR h.supersedes_handoff_id IS NULL OR a.id IS NULL OR i.id IS NULL
         OR NOT ((h.terminal_receipt_id IS NULL
                  AND predecessor.status='EXPIRED'
                  AND predecessor.failure_code='FINALIZED_EVIDENCE_EXPIRED'
                  AND predecessor.grant_id IS NULL)
                 OR (h.terminal_receipt_id IS NOT NULL
                     AND SCHEMA_TOKEN.exact_bounded_accepted_not_found_predecessor(
                           predecessor.handoff_id)))
         OR a.status<>'ACTIVE' OR i.status<>'APPROVED' OR d.decision<>'APPROVED'
         OR a.execution_target_id<>'OKX_DEMO' OR i.execution_target_id<>'OKX_DEMO'
         OR i.instrument_id<>'BTC-USDT-SWAP' OR i.reduce_only
         OR a.order_submission_authorized OR NOT a.claim_required
         OR h.runtime_instance_id IS DISTINCT FROM p_payload->>'runtime_instance_id'
         OR h.consent_deadline_at<=clock_timestamp()+interval '1 second'
         OR h.bundle_expires_at<=clock_timestamp()+interval '1 second'
         OR instrument.expires_at<=clock_timestamp()+interval '1 second'
         OR market.expires_at<=clock_timestamp()+interval '1 second'
         OR account.expires_at<=clock_timestamp()+interval '1 second'
         OR r.id IS NULL OR r.status NOT IN ('RECONCILED','RECOVERED')
         OR r.artifact_status<>'READY' OR r.source_type<>'api_aggregate' OR NOT r.core_data
         OR r.completed_at<clock_timestamp()-interval '30 seconds'
         OR r.authoritative_observed_at<clock_timestamp()-interval '30 seconds'
         OR r.database_ids::jsonb->'order_snapshots'<>'[]'::jsonb
         OR r.database_ids::jsonb->'position_snapshots'<>'[]'::jsonb
         OR s.last_reconciliation_run_id<>r.id OR s.opening_frozen
         OR expires<=clock_timestamp()+interval '1 second' OR expires>a.expires_at
         OR expires>i.expires_at OR computed_notional>20
         OR (p_payload->>'canary_notional')::numeric IS DISTINCT FROM computed_notional
         OR computed_notional IS DISTINCT FROM a.reserved_notional
         OR p_payload->>'canonical_hash' IS DISTINCT FROM a.canonical_hash
         OR p_payload->>'policy_digest' IS DISTINCT FROM a.policy_digest
         OR p_payload->>'approved_payload_hash' IS DISTINCT FROM a.approved_payload_hash
         OR p_payload->>'client_order_id' IS DISTINCT FROM a.client_order_id
         OR p_payload->>'instrument_id' IS DISTINCT FROM i.instrument_id
         OR (p_payload->>'canary_quantity')::numeric IS DISTINCT FROM i.quantity
         OR p_payload->>'request_digest' IS DISTINCT FROM computed_request_digest THEN
        RAISE EXCEPTION 'unsafe atomic canary grant';
      END IF;
      INSERT INTO SCHEMA_TOKEN.okx_demo_submission_grants(
        grant_id,handoff_id,execution_target_id,approval_id,reconciliation_run_id,
        canonical_hash,policy_digest,approved_payload_hash,client_order_id,instrument_id,
        canary_quantity,canary_notional,request_digest,provenance,status,
        writer_instance_id,issued_at,expires_at)
      VALUES(new_id,h.handoff_id,'OKX_DEMO',a.id,r.id,a.canonical_hash,a.policy_digest,
        a.approved_payload_hash,a.client_order_id,i.instrument_id,i.quantity,computed_notional,
        computed_request_digest,'CONTROLLED_CANARY_NON_PRODUCTION','ACTIVE',NULL,
        clock_timestamp(),expires);
      UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET status='GRANT_ISSUED',
        grant_id=new_id,updated_at=clock_timestamp() WHERE handoff_id=h.handoff_id;
      RETURN new_id;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.prepare_atomic_okx_demo_canary_dispatch(
      p_payload jsonb)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            g SCHEMA_TOKEN.okx_demo_submission_grants%ROWTYPE;
            a SCHEMA_TOKEN.approved_executions%ROWTYPE;
            i SCHEMA_TOKEN.trade_intents%ROWTYPE;
            r SCHEMA_TOKEN.reconciliation_runs%ROWTYPE;
            s SCHEMA_TOKEN.okx_demo_reconciliation_states%ROWTYPE;
            account SCHEMA_TOKEN.okx_demo_trusted_snapshots%ROWTYPE;
            lease SCHEMA_TOKEN.okx_order_writer_leases%ROWTYPE;
            expected_body jsonb; attached jsonb; lifecycle_id text;
            order_id bigint; attempt_id bigint; identity_digest text;
            expected_request_digest text; submission_not_after timestamptz;
            dispatch_not_after timestamptz;
            baseline_digest text; lifecycle_deadline timestamptz;
    BEGIN
      IF NOT pg_try_advisory_xact_lock(5067747289570038600) THEN
        RAISE EXCEPTION 'controlled canary coordination lock is busy';
      END IF;
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE handoff_id=p_payload->>'handoff_id' FOR UPDATE;
      SELECT * INTO g FROM SCHEMA_TOKEN.okx_demo_submission_grants
        WHERE grant_id=p_payload->>'grant_id' FOR UPDATE;
      SELECT * INTO a FROM SCHEMA_TOKEN.approved_executions WHERE id=g.approval_id FOR UPDATE;
      SELECT * INTO i FROM SCHEMA_TOKEN.trade_intents WHERE id=a.trade_intent_id FOR UPDATE;
      SELECT * INTO r FROM SCHEMA_TOKEN.reconciliation_runs
        WHERE id=g.reconciliation_run_id;
      SELECT * INTO s FROM SCHEMA_TOKEN.okx_demo_reconciliation_states
        WHERE execution_target_id='OKX_DEMO' FOR UPDATE;
      SELECT * INTO account FROM SCHEMA_TOKEN.okx_demo_trusted_snapshots
        WHERE database_id=(h.snapshot_binding::jsonb#>>'{account,database_id}')::bigint;
      submission_not_after:=LEAST(h.bundle_expires_at,h.consent_deadline_at,
        a.expires_at,i.expires_at,g.expires_at);
      dispatch_not_after:=LEAST(submission_not_after,
        clock_timestamp()+interval '1 second');
      expected_body:=jsonb_build_object('instId',i.instrument_id,'tdMode','isolated',
        'side',i.side,'posSide',i.position_side,'ordType',i.order_type,
        'sz',SCHEMA_TOKEN.canonical_decimal_text(i.quantity),'clOrdId',i.client_order_id,
        'px',SCHEMA_TOKEN.canonical_decimal_text(i.limit_price));
      IF i.take_profit IS NOT NULL OR i.stop_loss IS NOT NULL THEN
        attached:='{}'::jsonb;
        IF i.take_profit IS NOT NULL THEN
          attached:=attached||jsonb_build_object(
            'attachAlgoClOrdId',left(i.client_order_id,30)||'TP',
            'tpTriggerPx',SCHEMA_TOKEN.canonical_decimal_text(i.take_profit),
            'tpOrdPx','-1','tpTriggerPxType','mark');
        END IF;
        IF i.stop_loss IS NOT NULL THEN
          attached:=attached||jsonb_build_object(
            'attachAlgoClOrdId',COALESCE(attached->>'attachAlgoClOrdId',
              left(i.client_order_id,30)||'EX'),
            'slTriggerPx',SCHEMA_TOKEN.canonical_decimal_text(i.stop_loss),
            'slOrdPx','-1','slTriggerPxType','mark');
        END IF;
        expected_body:=expected_body||jsonb_build_object('attachAlgoOrds',jsonb_build_array(attached));
      END IF;
      expected_request_digest:=encode(public.digest(convert_to(
        SCHEMA_TOKEN.canonical_jsonb_text(expected_body),'UTF8'),'sha256'),'hex');
      IF h.status<>'GRANT_ISSUED' OR g.status<>'ACTIVE'
         OR h.grant_id IS DISTINCT FROM g.grant_id OR h.approval_id<>a.id
         OR h.runtime_instance_id IS DISTINCT FROM p_payload->>'runtime_instance_id'
         OR p_payload->>'holder_token_digest'!~'^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'unsafe atomic canary prepare identity';
      END IF;
      IF p_payload->>'request_digest' IS DISTINCT FROM expected_request_digest
         OR p_payload->'request_body' IS DISTINCT FROM expected_body THEN
        RAISE EXCEPTION 'unsafe atomic canary prepare request';
      END IF;
      IF account.content_json::jsonb#>>'{leverage_by_position_side,long}'
              IS DISTINCT FROM SCHEMA_TOKEN.canonical_decimal_text(i.leverage) THEN
        RAISE EXCEPTION 'unsafe atomic canary prepare leverage';
      END IF;
      IF r.id IS NULL OR r.status NOT IN ('RECONCILED','RECOVERED')
         OR r.artifact_status<>'READY' OR r.source_type<>'api_aggregate' OR NOT r.core_data
         OR s.last_reconciliation_run_id<>r.id OR s.opening_frozen
         OR r.database_ids::jsonb->'order_snapshots'<>'[]'::jsonb
         OR r.database_ids::jsonb->'position_snapshots'<>'[]'::jsonb
         OR jsonb_typeof(r.database_ids::jsonb->'recovery_batches') IS DISTINCT FROM 'array'
         OR jsonb_array_length(r.database_ids::jsonb->'recovery_batches')<>1
         OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_recovery_batches b
              WHERE b.database_id=(r.database_ids::jsonb->'recovery_batches'->>0)::bigint
                AND b.execution_target_id='OKX_DEMO' AND b.authenticated
                AND b.pagination_complete
                AND b.complete_streams::jsonb='["ACCOUNT","FILL","ORDER","POSITION"]'::jsonb) THEN
        RAISE EXCEPTION 'unsafe atomic canary prepare reconciliation';
      END IF;
      IF submission_not_after<=clock_timestamp()+interval '1 second' THEN
        RAISE EXCEPTION 'unsafe atomic canary prepare dispatch budget';
      END IF;
      IF EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
              WHERE execution_target_id='OKX_DEMO')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_orders
              WHERE execution_target_id='OKX_DEMO')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles) THEN
        IF h.terminal_receipt_id IS NULL
           OR NOT SCHEMA_TOKEN.exact_bounded_accepted_not_found_predecessor(
                    h.supersedes_handoff_id)
           OR NOT EXISTS(SELECT 1
                FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations receipt
                WHERE receipt.id=h.terminal_receipt_id
                  AND receipt.predecessor_handoff_id=h.supersedes_handoff_id)
           OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_order_write_attempts
                 WHERE execution_target_id='OKX_DEMO')<>
                (SELECT receipt_depth FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
                  WHERE id=h.terminal_receipt_id)
           OR (SELECT count(*) FROM SCHEMA_TOKEN.exchange_orders
                 WHERE execution_target_id='OKX_DEMO')<>
                (SELECT receipt_depth FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
                  WHERE id=h.terminal_receipt_id)
           OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles)<>
                (SELECT receipt_depth FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
                  WHERE id=h.terminal_receipt_id)
           OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_fills
                 WHERE execution_target_id='OKX_DEMO') THEN
          RAISE EXCEPTION 'unsafe atomic canary prepare occupied';
        END IF;
      END IF;
      SELECT * INTO lease FROM SCHEMA_TOKEN.okx_order_writer_leases
        WHERE execution_target_id='OKX_DEMO' FOR UPDATE;
      IF NOT FOUND THEN
        INSERT INTO SCHEMA_TOKEN.okx_order_writer_leases(
          execution_target_id,holder_token_digest,generation,acquired_at,heartbeat_at,expires_at)
        VALUES('OKX_DEMO',p_payload->>'holder_token_digest',1,clock_timestamp(),
          clock_timestamp(),submission_not_after) RETURNING * INTO lease;
      ELSIF lease.holder_token_digest=p_payload->>'holder_token_digest' THEN
        UPDATE SCHEMA_TOKEN.okx_order_writer_leases SET heartbeat_at=clock_timestamp(),
          expires_at=submission_not_after WHERE execution_target_id='OKX_DEMO'
          RETURNING * INTO lease;
      ELSIF lease.expires_at<=clock_timestamp() THEN
        UPDATE SCHEMA_TOKEN.okx_order_writer_leases SET
          holder_token_digest=p_payload->>'holder_token_digest',generation=generation+1,
          acquired_at=clock_timestamp(),heartbeat_at=clock_timestamp(),
          expires_at=submission_not_after WHERE execution_target_id='OKX_DEMO'
          RETURNING * INTO lease;
      ELSE
        RAISE EXCEPTION 'another OKX_DEMO writer holds the database lease';
      END IF;
      lifecycle_id:=g.grant_id;
      baseline_digest:=encode(public.digest(convert_to(concat_ws('|',r.id::text,
        r.artifact_sha256,r.authoritative_observed_at::text,r.completed_at::text),
        'UTF8'),'sha256'),'hex');
      lifecycle_deadline:=LEAST(clock_timestamp()+interval '30 seconds',
        g.expires_at,a.expires_at,i.expires_at);
      INSERT INTO SCHEMA_TOKEN.okx_demo_canary_lifecycles(
        lifecycle_id,execution_target_id,submission_grant_id,opening_approval_id,
        opening_trade_intent_id,baseline_reconciliation_run_id,
        baseline_position_quantity,baseline_evidence_digest,
        attributed_fill_quantity,max_quantity,outcome,cleanup_phase,
        deadline_at,fencing_version,created_at,updated_at)
      VALUES(lifecycle_id,'OKX_DEMO',g.grant_id,a.id,i.id,r.id,0,
        baseline_digest,0,g.canary_quantity,'PENDING','ARMED',lifecycle_deadline,
        1,clock_timestamp(),clock_timestamp());
      INSERT INTO SCHEMA_TOKEN.exchange_orders(
        execution_target_id,trade_intent_id,client_order_id,status,
        request_snapshot,response_snapshot,created_at,updated_at)
      VALUES('OKX_DEMO',i.id,i.client_order_id,'PREPARED',expected_body::json,'{}'::json,
        clock_timestamp(),clock_timestamp()) RETURNING id INTO order_id;
      INSERT INTO SCHEMA_TOKEN.okx_order_write_attempts(
        execution_target_id,exchange_order_row_id,approval_id,operation,operation_id,
        client_order_id,instrument_id,state,request_digest,safe_request_snapshot,
         safe_response_snapshot,attempt_count,lease_generation,close_sequence,
        last_attempt_at,dispatch_not_after,created_at,updated_at)
      VALUES('OKX_DEMO',order_id,a.id,'PLACE',i.client_order_id,i.client_order_id,
        i.instrument_id,'PREPARED',expected_request_digest,expected_body::json,'{}'::json,
        1,lease.generation,0,clock_timestamp(),dispatch_not_after,
        clock_timestamp(),clock_timestamp())
      RETURNING id INTO attempt_id;
      identity_digest:=encode(public.digest(convert_to(
        order_id::text||'|'||i.client_order_id||'|'||i.instrument_id||'|'||expected_request_digest,
        'UTF8'),'sha256'),'hex');
      UPDATE SCHEMA_TOKEN.okx_demo_submission_grants SET status='CONSUMED',
        writer_instance_id=p_payload->>'runtime_instance_id',consumed_at=clock_timestamp()
        WHERE grant_id=g.grant_id;
      UPDATE SCHEMA_TOKEN.okx_demo_canary_consent_handoffs SET status='CONSUMED',
        updated_at=clock_timestamp() WHERE handoff_id=h.handoff_id;
      PERFORM SCHEMA_TOKEN.transition_okx_demo_canary_lifecycle(
        lifecycle_id,'BIND_OPENING',order_id,NULL,identity_digest,1);
      RETURN jsonb_build_object('attempt_id',attempt_id,'exchange_order_row_id',order_id,
        'lease_generation',lease.generation,'request_digest',expected_request_digest,
        'client_order_id',i.client_order_id,'instrument_id',i.instrument_id,
        'request_body',expected_body,'dispatch_not_after',dispatch_not_after,
        'dispatch_guard_policy','db-clock-monotonic-v2','dispatch_guard_ms',1000,
        'dispatch_claim_min_remaining_ms',500,'post_start_reserve_ms',100,
        'submission_not_after',submission_not_after,
        'bundle_digest',h.bundle_digest);
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.commit_atomic_okx_demo_canary_prepare(
      p_handoff_id text,p_runtime_id text,p_audit_job_id bigint,
      p_full_chain_run_id bigint,p_approval_id bigint,
      p_reconciliation_run_id bigint,p_binding jsonb,
      p_grant_payload jsonb,p_prepare_payload jsonb)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE grant_id text; receipt jsonb;
    BEGIN
      PERFORM SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret();
      IF p_grant_payload->>'handoff_id' IS DISTINCT FROM p_handoff_id
         OR p_grant_payload->>'runtime_instance_id' IS DISTINCT FROM p_runtime_id
         OR p_prepare_payload->>'handoff_id' IS DISTINCT FROM p_handoff_id
         OR p_prepare_payload->>'runtime_instance_id' IS DISTINCT FROM p_runtime_id
         OR p_prepare_payload->>'grant_id' IS DISTINCT FROM p_grant_payload->>'grant_id' THEN
        RAISE EXCEPTION 'atomic canary coordinator identity mismatch';
      END IF;
      PERFORM SCHEMA_TOKEN.finalize_atomic_okx_demo_canary_consent(
        p_handoff_id,p_runtime_id,p_audit_job_id,p_full_chain_run_id,
        p_approval_id,p_reconciliation_run_id,p_binding);
      grant_id:=SCHEMA_TOKEN.issue_atomic_okx_demo_submission_grant(p_grant_payload);
      IF grant_id IS DISTINCT FROM p_grant_payload->>'grant_id' THEN
        RAISE EXCEPTION 'atomic canary grant identity changed';
      END IF;
      receipt:=SCHEMA_TOKEN.prepare_atomic_okx_demo_canary_dispatch(p_prepare_payload);
      RETURN receipt||jsonb_build_object('grant_id',grant_id,
        'handoff_id',p_handoff_id,'runtime_instance_id',p_runtime_id);
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.claim_atomic_okx_demo_canary_dispatch(
      p_attempt_id bigint,p_runtime_id text,p_holder_digest text,
      p_lease_generation bigint,p_request_digest text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE attempt SCHEMA_TOKEN.okx_order_write_attempts%ROWTYPE;
            order_row SCHEMA_TOKEN.exchange_orders%ROWTYPE;
            lease SCHEMA_TOKEN.okx_order_writer_leases%ROWTYPE;
            g SCHEMA_TOKEN.okx_demo_submission_grants%ROWTYPE;
            h SCHEMA_TOKEN.okx_demo_canary_consent_handoffs%ROWTYPE;
            lifecycle SCHEMA_TOKEN.okx_demo_canary_lifecycles%ROWTYPE;
            submission_not_after timestamptz; claimed_at timestamptz;
            dispatch_remaining_ms bigint;
    BEGIN
      PERFORM SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret();
      IF NOT pg_try_advisory_xact_lock(5067747289570038600) THEN
        RAISE EXCEPTION 'controlled canary coordination lock is busy';
      END IF;
      SELECT * INTO attempt FROM SCHEMA_TOKEN.okx_order_write_attempts
        WHERE id=p_attempt_id FOR UPDATE;
      SELECT * INTO order_row FROM SCHEMA_TOKEN.exchange_orders
        WHERE id=attempt.exchange_order_row_id FOR UPDATE;
      SELECT * INTO g FROM SCHEMA_TOKEN.okx_demo_submission_grants
        WHERE approval_id=attempt.approval_id FOR UPDATE;
      SELECT * INTO h FROM SCHEMA_TOKEN.okx_demo_canary_consent_handoffs
        WHERE grant_id=g.grant_id FOR UPDATE;
      SELECT * INTO lifecycle FROM SCHEMA_TOKEN.okx_demo_canary_lifecycles
        WHERE submission_grant_id=g.grant_id FOR UPDATE;
      SELECT * INTO lease FROM SCHEMA_TOKEN.okx_order_writer_leases
        WHERE execution_target_id='OKX_DEMO' FOR UPDATE;
      submission_not_after:=LEAST(h.bundle_expires_at,h.consent_deadline_at,
        g.expires_at,lease.expires_at);
      claimed_at:=clock_timestamp();
      IF attempt.id IS NULL OR attempt.state<>'PREPARED' OR attempt.operation<>'PLACE'
         OR attempt.attempt_count<>1 OR attempt.request_digest IS DISTINCT FROM p_request_digest
         OR order_row.status<>'PREPARED' OR order_row.client_order_id<>attempt.client_order_id
         OR g.status<>'CONSUMED' OR h.status<>'CONSUMED'
         OR g.writer_instance_id IS DISTINCT FROM p_runtime_id
         OR h.runtime_instance_id IS DISTINCT FROM p_runtime_id
         OR p_holder_digest!~'^[0-9a-f]{64}$'
         OR lease.holder_token_digest IS DISTINCT FROM encode(public.digest(
              convert_to(p_holder_digest,'UTF8'),'sha256'),'hex')
         OR lease.generation<>p_lease_generation
         OR attempt.lease_generation<>lease.generation
         OR lifecycle.cleanup_phase<>'OPENING_SUBMITTED'
         OR lifecycle.opening_exchange_order_row_id<>order_row.id
         OR attempt.dispatch_not_after IS NULL
         OR attempt.dispatch_not_after<=claimed_at+interval '500 milliseconds' THEN
        RETURN NULL;
      END IF;
      dispatch_remaining_ms:=floor(extract(epoch FROM
        (attempt.dispatch_not_after-claimed_at))*1000)::bigint;
      UPDATE SCHEMA_TOKEN.okx_order_write_attempts SET state='DISPATCHED',
        last_attempt_at=claimed_at,updated_at=claimed_at
        WHERE id=attempt.id;
      UPDATE SCHEMA_TOKEN.exchange_orders SET status='DISPATCHED',
        updated_at=claimed_at WHERE id=order_row.id;
      RETURN jsonb_build_object('attempt_id',attempt.id,
        'exchange_order_row_id',order_row.id,'client_order_id',order_row.client_order_id,
        'instrument_id',attempt.instrument_id,
        'request_body',attempt.safe_request_snapshot::jsonb,
        'request_digest',attempt.request_digest,
        'runtime_instance_id',p_runtime_id,
        'holder_token_digest',lease.holder_token_digest,
        'bundle_digest',h.bundle_digest,
        'dispatch_claimed_at',claimed_at,
        'dispatch_remaining_ms',dispatch_remaining_ms,
        'dispatch_not_after',attempt.dispatch_not_after,
        'submission_not_after',submission_not_after);
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.validate_atomic_okx_demo_dispatch_authority(
      p_attempt_id bigint,p_runtime_id text,p_holder_token text,
      p_lease_generation bigint,p_request_digest text,p_bundle_digest text)
    RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE remaining_ms bigint;
    BEGIN
      PERFORM SCHEMA_TOKEN.require_active_okx_demo_operator_consent_secret();
      IF p_holder_token!~'^[0-9a-f]{64}$' THEN RETURN NULL; END IF;
      SELECT floor(extract(epoch FROM
               (a.dispatch_not_after-clock_timestamp()))*1000)::bigint
        INTO remaining_ms
        FROM SCHEMA_TOKEN.okx_order_write_attempts a
        JOIN SCHEMA_TOKEN.okx_order_writer_leases l
          ON l.execution_target_id=a.execution_target_id
        JOIN SCHEMA_TOKEN.okx_demo_submission_grants g ON g.approval_id=a.approval_id
        JOIN SCHEMA_TOKEN.okx_demo_canary_consent_handoffs h ON h.grant_id=g.grant_id
       WHERE a.id=p_attempt_id AND a.state='DISPATCHED'
         AND a.request_digest=p_request_digest
         AND a.lease_generation=p_lease_generation
         AND l.generation=p_lease_generation
         AND l.holder_token_digest=encode(public.digest(
               convert_to(p_holder_token,'UTF8'),'sha256'),'hex')
         AND l.expires_at>clock_timestamp()
         AND g.status='CONSUMED' AND g.writer_instance_id=p_runtime_id
         AND h.status='CONSUMED' AND h.runtime_instance_id=p_runtime_id
         AND h.bundle_digest=p_bundle_digest
         AND a.dispatch_not_after>clock_timestamp()+interval '100 milliseconds';
      RETURN remaining_ms;
    END $$;
    """.replace("SCHEMA_TOKEN", quoted_schema)
    connection.execute(text(ddl))
    runtime_signatures = {
        "eligible_atomic_okx_demo_canary_predecessor()",
        "request_atomic_okx_demo_canary_consent(text,text,text,text)",
        "commit_atomic_okx_demo_canary_prepare(text,text,bigint,bigint,bigint,bigint,jsonb,jsonb,jsonb)",
        "claim_atomic_okx_demo_canary_dispatch(bigint,text,text,bigint,text)",
        "validate_atomic_okx_demo_dispatch_authority(bigint,text,text,bigint,text,text)",
    }
    for signature in (
        "eligible_atomic_okx_demo_canary_predecessor()",
        "request_atomic_okx_demo_canary_consent(text,text,text,text)",
        "finalize_atomic_okx_demo_canary_consent(text,text,bigint,bigint,bigint,bigint,jsonb)",
        "issue_atomic_okx_demo_submission_grant(jsonb)",
        "prepare_atomic_okx_demo_canary_dispatch(jsonb)",
        "commit_atomic_okx_demo_canary_prepare(text,text,bigint,bigint,bigint,bigint,jsonb,jsonb,jsonb)",
        "claim_atomic_okx_demo_canary_dispatch(bigint,text,text,bigint,text)",
        "validate_atomic_okx_demo_dispatch_authority(bigint,text,text,bigint,text,text)",
    ):
        connection.execute(text(
            "ALTER FUNCTION {0}.{1} OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON FUNCTION {0}.{1} FROM PUBLIC; "
            "{2}".format(
                quoted_schema, signature,
                (
                    "GRANT EXECUTE ON FUNCTION {0}.{1} TO freqtrade"
                    if signature in runtime_signatures
                    else "REVOKE EXECUTE ON FUNCTION {0}.{1} FROM freqtrade"
                ).format(quoted_schema, signature),
            )
        ))


def _add_continuous_demo_automation_boundary(connection: Connection) -> None:
    """Install the fixed three-strategy Demo guard without generic permissions."""

    schema_name = connection.execute(text("SELECT current_schema()" )).scalar_one()
    quoted_schema = connection.dialect.identifier_preparer.quote_schema(schema_name)
    for table_name in (
        "okx_demo_automation_guard_states",
        "okx_demo_automation_guard_events",
    ):
        Base.metadata.tables[table_name].create(bind=connection, checkfirst=True)
    ddl = r"""
    ALTER TABLE SCHEMA_TOKEN.strategy_deployments
      ADD COLUMN IF NOT EXISTS active_slot integer,
      ADD COLUMN IF NOT EXISTS risk_policy_digest varchar(64);
    DO $$
    DECLARE active_count integer;
    BEGIN
      SELECT count(*) INTO active_count FROM SCHEMA_TOKEN.strategy_deployments
       WHERE status='ACTIVE';
      IF active_count>3 THEN
        RAISE EXCEPTION 'more than three ACTIVE OKX_DEMO deployments exist';
      END IF;
      WITH ranked AS (
        SELECT id,row_number() OVER(ORDER BY id)::integer slot
          FROM SCHEMA_TOKEN.strategy_deployments WHERE status='ACTIVE')
      UPDATE SCHEMA_TOKEN.strategy_deployments d SET active_slot=ranked.slot
        FROM ranked WHERE d.id=ranked.id AND d.active_slot IS NULL;
      UPDATE SCHEMA_TOKEN.strategy_deployments SET active_slot=NULL
       WHERE status='DISABLED' AND active_slot IS NOT NULL;
    END $$;
    DROP INDEX IF EXISTS SCHEMA_TOKEN.strategy_deployments_single_active_idx;
    DROP INDEX IF EXISTS SCHEMA_TOKEN.strategy_deployments_active_slot_idx;
    ALTER TABLE SCHEMA_TOKEN.strategy_deployments
      DROP CONSTRAINT IF EXISTS strategy_deployments_active_slot_check,
      ADD CONSTRAINT strategy_deployments_active_slot_check CHECK(
        (status='ACTIVE' AND active_slot BETWEEN 1 AND 3)
        OR (status='DISABLED' AND active_slot IS NULL));
    CREATE UNIQUE INDEX strategy_deployments_active_slot_idx
      ON SCHEMA_TOKEN.strategy_deployments(execution_target_id,active_slot)
      WHERE status='ACTIVE';
    ALTER TABLE SCHEMA_TOKEN.strategy_deployments OWNER TO freqtrade_ai_attestor;
    REVOKE ALL ON TABLE SCHEMA_TOKEN.strategy_deployments FROM PUBLIC,freqtrade;
    GRANT SELECT ON TABLE SCHEMA_TOKEN.strategy_deployments TO freqtrade;
    ALTER SEQUENCE SCHEMA_TOKEN.strategy_deployments_id_seq
      OWNER TO freqtrade_ai_attestor;
    REVOKE ALL ON SEQUENCE SCHEMA_TOKEN.strategy_deployments_id_seq
      FROM PUBLIC,freqtrade;
    GRANT SELECT ON TABLE SCHEMA_TOKEN.signal_evaluations,
      SCHEMA_TOKEN.strategy_candidate_approvals,
      SCHEMA_TOKEN.strategies TO freqtrade_ai_attestor;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.guard_active_demo_strategy_material()
    RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    DECLARE material_id bigint:=OLD.id; referenced boolean:=false;
    BEGIN
      IF TG_TABLE_NAME='strategies' THEN
        SELECT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.strategy_deployments
          WHERE status='ACTIVE' AND strategy_id=material_id) INTO referenced;
      ELSIF TG_TABLE_NAME='strategy_versions' THEN
        SELECT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.strategy_deployments
          WHERE status='ACTIVE' AND strategy_version_id=material_id) INTO referenced;
      ELSIF TG_TABLE_NAME='strategy_candidate_approvals' THEN
        SELECT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.strategy_deployments
          WHERE status='ACTIVE' AND candidate_approval_id=material_id) INTO referenced;
      ELSIF TG_TABLE_NAME='strategy_scores' THEN
        SELECT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.strategy_deployments deployment
          JOIN SCHEMA_TOKEN.strategy_candidate_approvals selection
            ON selection.id=deployment.candidate_approval_id
          WHERE deployment.status='ACTIVE' AND selection.strategy_score_id=material_id)
          INTO referenced;
      ELSIF TG_TABLE_NAME='backtest_results' THEN
        SELECT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.strategy_deployments deployment
          JOIN SCHEMA_TOKEN.strategy_candidate_approvals selection
            ON selection.id=deployment.candidate_approval_id
          WHERE deployment.status='ACTIVE' AND selection.backtest_result_id=material_id)
          INTO referenced;
      ELSIF TG_TABLE_NAME='full_chain_runs' THEN
        SELECT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.strategy_deployments deployment
          JOIN SCHEMA_TOKEN.strategy_candidate_approvals selection
            ON selection.id=deployment.candidate_approval_id
          WHERE deployment.status='ACTIVE' AND selection.full_chain_run_id=material_id)
          INTO referenced;
      ELSIF TG_TABLE_NAME='backtest_runs' THEN
        SELECT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.strategy_deployments deployment
          JOIN SCHEMA_TOKEN.strategy_candidate_approvals selection
            ON selection.id=deployment.candidate_approval_id
          JOIN SCHEMA_TOKEN.full_chain_runs chain
            ON chain.id=selection.full_chain_run_id
          WHERE deployment.status='ACTIVE' AND chain.backtest_run_id=material_id)
          INTO referenced;
      ELSIF TG_TABLE_NAME='backtest_tasks' THEN
        SELECT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.strategy_deployments deployment
          JOIN SCHEMA_TOKEN.strategy_candidate_approvals selection
            ON selection.id=deployment.candidate_approval_id
          JOIN SCHEMA_TOKEN.full_chain_runs chain
            ON chain.id=selection.full_chain_run_id
          WHERE deployment.status='ACTIVE' AND chain.backtest_task_id=material_id)
          INTO referenced;
      END IF;
      IF referenced THEN
        RAISE EXCEPTION 'ACTIVE OKX_DEMO deployment material is immutable';
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$;
    DROP TRIGGER IF EXISTS active_demo_strategy_material_immutable
      ON SCHEMA_TOKEN.strategies;
    CREATE TRIGGER active_demo_strategy_material_immutable
      BEFORE UPDATE OR DELETE ON SCHEMA_TOKEN.strategies FOR EACH ROW
      EXECUTE FUNCTION SCHEMA_TOKEN.guard_active_demo_strategy_material();
    DROP TRIGGER IF EXISTS active_demo_strategy_version_immutable
      ON SCHEMA_TOKEN.strategy_versions;
    CREATE TRIGGER active_demo_strategy_version_immutable
      BEFORE UPDATE OR DELETE ON SCHEMA_TOKEN.strategy_versions FOR EACH ROW
      EXECUTE FUNCTION SCHEMA_TOKEN.guard_active_demo_strategy_material();
    DROP TRIGGER IF EXISTS active_demo_selection_receipt_immutable
      ON SCHEMA_TOKEN.strategy_candidate_approvals;
    CREATE TRIGGER active_demo_selection_receipt_immutable
      BEFORE UPDATE OR DELETE ON SCHEMA_TOKEN.strategy_candidate_approvals FOR EACH ROW
      EXECUTE FUNCTION SCHEMA_TOKEN.guard_active_demo_strategy_material();
    DROP TRIGGER IF EXISTS active_demo_strategy_score_immutable
      ON SCHEMA_TOKEN.strategy_scores;
    CREATE TRIGGER active_demo_strategy_score_immutable
      BEFORE UPDATE OR DELETE ON SCHEMA_TOKEN.strategy_scores FOR EACH ROW
      EXECUTE FUNCTION SCHEMA_TOKEN.guard_active_demo_strategy_material();
    DROP TRIGGER IF EXISTS active_demo_backtest_result_immutable
      ON SCHEMA_TOKEN.backtest_results;
    CREATE TRIGGER active_demo_backtest_result_immutable
      BEFORE UPDATE OR DELETE ON SCHEMA_TOKEN.backtest_results FOR EACH ROW
      EXECUTE FUNCTION SCHEMA_TOKEN.guard_active_demo_strategy_material();
    DROP TRIGGER IF EXISTS active_demo_backtest_run_immutable
      ON SCHEMA_TOKEN.backtest_runs;
    CREATE TRIGGER active_demo_backtest_run_immutable
      BEFORE UPDATE OR DELETE ON SCHEMA_TOKEN.backtest_runs FOR EACH ROW
      EXECUTE FUNCTION SCHEMA_TOKEN.guard_active_demo_strategy_material();
    DROP TRIGGER IF EXISTS active_demo_backtest_task_immutable
      ON SCHEMA_TOKEN.backtest_tasks;
    CREATE TRIGGER active_demo_backtest_task_immutable
      BEFORE UPDATE OR DELETE ON SCHEMA_TOKEN.backtest_tasks FOR EACH ROW
      EXECUTE FUNCTION SCHEMA_TOKEN.guard_active_demo_strategy_material();
    DROP TRIGGER IF EXISTS active_demo_selection_chain_immutable
      ON SCHEMA_TOKEN.full_chain_runs;
    CREATE TRIGGER active_demo_selection_chain_immutable
      BEFORE UPDATE OR DELETE ON SCHEMA_TOKEN.full_chain_runs FOR EACH ROW
      EXECUTE FUNCTION SCHEMA_TOKEN.guard_active_demo_strategy_material();

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.guard_okx_demo_automation_event()
    RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    BEGIN RAISE EXCEPTION 'OKX_DEMO automation events are append-only'; END $$;
    DROP TRIGGER IF EXISTS okx_demo_automation_events_immutable
      ON SCHEMA_TOKEN.okx_demo_automation_guard_events;
    CREATE TRIGGER okx_demo_automation_events_immutable
      BEFORE UPDATE OR DELETE ON SCHEMA_TOKEN.okx_demo_automation_guard_events
      FOR EACH ROW EXECUTE FUNCTION SCHEMA_TOKEN.guard_okx_demo_automation_event();

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.okx_demo_continuous_opening_allowed(
      p_policy_digest text)
    RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
      SELECT EXISTS(
        SELECT 1 FROM SCHEMA_TOKEN.okx_demo_automation_guard_states state
         JOIN SCHEMA_TOKEN.okx_demo_reconciliation_states reconciliation
           ON reconciliation.execution_target_id=state.execution_target_id
         JOIN SCHEMA_TOKEN.reconciliation_runs run
           ON run.id=reconciliation.last_reconciliation_run_id
        WHERE state.execution_target_id='OKX_DEMO'
          AND state.authorization_mode IN ('CONTINUOUS_DEMO_V1')
          AND state.operational_state='RUNNING'
          AND state.policy_digest=p_policy_digest
          AND state.deployment_set_digest=(
            SELECT encode(public.digest(convert_to(COALESCE(string_agg(
              deployment.id::text||':'||deployment.active_slot::text||':'||
              deployment.candidate_approval_id::text||':'||
              deployment.candidate_digest||':'||deployment.deployment_policy_digest||':'||
              COALESCE(deployment.risk_policy_digest,''),
              '|' ORDER BY deployment.active_slot),''),'UTF8'),'sha256'),'hex')
            FROM SCHEMA_TOKEN.strategy_deployments deployment
            WHERE deployment.execution_target_id='OKX_DEMO'
              AND deployment.status='ACTIVE')
          AND reconciliation.status IN ('RECONCILED','RECOVERED')
          AND reconciliation.opening_frozen=false
          AND run.execution_target_id='OKX_DEMO'
          AND run.status IN ('RECONCILED','RECOVERED')
          AND run.completed_at BETWEEN clock_timestamp()-interval '90 seconds'
                                   AND clock_timestamp()+interval '5 seconds'
          AND NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
             WHERE execution_target_id='OKX_DEMO' AND state IN
               ('PREPARED','DISPATCHED','ACKNOWLEDGED','RECOVERY_REQUIRED',
                'RESIDUAL_CLOSE_REQUIRED')));
    $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.claim_okx_demo_continuous_dispatch(
      p_approval_id bigint,p_policy_digest text)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE guard SCHEMA_TOKEN.okx_demo_automation_guard_states%ROWTYPE;
            approval SCHEMA_TOKEN.approved_executions%ROWTYPE;
            event_digest text; now_value timestamptz:=clock_timestamp();
    BEGIN
      PERFORM pg_advisory_xact_lock(543000003);
      SELECT * INTO guard FROM SCHEMA_TOKEN.okx_demo_automation_guard_states
       WHERE execution_target_id='OKX_DEMO' FOR UPDATE;
      SELECT * INTO approval FROM SCHEMA_TOKEN.approved_executions
       WHERE id=p_approval_id FOR UPDATE;
      IF guard.execution_target_id IS NULL
         OR guard.authorization_mode<>'CONTINUOUS_DEMO_V1'
         OR guard.operational_state<>'RUNNING'
         OR guard.policy_digest IS DISTINCT FROM p_policy_digest
         OR approval.id IS NULL OR approval.status<>'ACTIVE'
         OR approval.policy_digest IS DISTINCT FROM p_policy_digest
         OR approval.expires_at<=now_value
         OR NOT SCHEMA_TOKEN.okx_demo_continuous_opening_allowed(p_policy_digest)
         OR (EXISTS(SELECT 1 FROM SCHEMA_TOKEN.strategy_deployments
              WHERE execution_target_id='OKX_DEMO' AND status='ACTIVE')
             AND NOT EXISTS(
               SELECT 1 FROM SCHEMA_TOKEN.full_chain_runs execution_chain
               JOIN SCHEMA_TOKEN.signal_evaluations evaluation
                 ON evaluation.id=execution_chain.signal_evaluation_id
               JOIN SCHEMA_TOKEN.strategy_deployments deployment
                 ON deployment.id=evaluation.deployment_id
               JOIN SCHEMA_TOKEN.strategy_candidate_approvals selection
                 ON selection.id=deployment.candidate_approval_id
               WHERE execution_chain.run_kind='EXECUTION'
                 AND execution_chain.approved_execution_id=p_approval_id
                 AND deployment.status='ACTIVE'
                 AND selection.status='APPROVED'
                 AND selection.promotion_policy_version='okx-demo-selection-v2'
                 AND deployment.deployment_policy_digest IS NOT NULL
                 AND deployment.risk_policy_digest=p_policy_digest))
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
              WHERE approval_id=p_approval_id AND operation IN ('PLACE','CLOSE'))
         OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_demo_automation_guard_events
              WHERE event_kind='ACTION_DISPATCH'
                AND observed_at>now_value-interval '5 minutes')>=6
         OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_demo_automation_guard_events
              WHERE event_kind='ACTION_DISPATCH'
                AND observed_at>now_value-interval '1 hour')>=24 THEN
        RETURN false;
      END IF;
      event_digest:=encode(public.digest(convert_to(
        'dispatch|'||p_approval_id::text||'|'||p_policy_digest,'UTF8'),'sha256'),'hex');
      INSERT INTO SCHEMA_TOKEN.okx_demo_automation_guard_events(
        execution_target_id,event_key,event_kind,failure_class,policy_digest,
        approved_execution_id,reconciliation_run_id,evidence_snapshot,observed_at)
      VALUES('OKX_DEMO',event_digest,'ACTION_DISPATCH',NULL,p_policy_digest,
        p_approval_id,guard.last_healthy_reconciliation_run_id,
        jsonb_build_object('max_orders_per_5_minutes',6,
          'max_orders_per_hour',24,'allow_real_funds',false)::json,now_value)
      ON CONFLICT(event_key) DO NOTHING;
      RETURN FOUND;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.record_okx_demo_automation_failure(
      p_failure_class text,p_event_key text,p_reconciliation_run_id bigint,
      p_policy_digest text)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE guard SCHEMA_TOKEN.okx_demo_automation_guard_states%ROWTYPE;
            now_value timestamptz:=clock_timestamp(); next_count integer;
    BEGIN
      IF p_failure_class NOT IN ('SUBMISSION','AUTHENTICATION',
          'RECONCILIATION_TRANSIENT','RECONCILIATION','DUPLICATE')
         OR p_event_key!~'^[0-9a-f]{64}$' THEN RETURN 'BLOCKED'; END IF;
      PERFORM pg_advisory_xact_lock(543000003);
      SELECT * INTO guard FROM SCHEMA_TOKEN.okx_demo_automation_guard_states
       WHERE execution_target_id='OKX_DEMO' FOR UPDATE;
      IF guard.execution_target_id IS NULL
         OR guard.authorization_mode<>'CONTINUOUS_DEMO_V1'
         OR guard.policy_digest IS DISTINCT FROM p_policy_digest THEN RETURN 'BLOCKED'; END IF;
      INSERT INTO SCHEMA_TOKEN.okx_demo_automation_guard_events(
        execution_target_id,event_key,event_kind,failure_class,policy_digest,
        reconciliation_run_id,evidence_snapshot,observed_at)
      VALUES('OKX_DEMO',p_event_key,'CRITICAL_FAILURE',p_failure_class,p_policy_digest,
        p_reconciliation_run_id,jsonb_build_object('allow_real_funds',false)::json,now_value)
      ON CONFLICT(event_key) DO NOTHING;
      IF NOT FOUND THEN RETURN guard.operational_state; END IF;
      IF p_failure_class IN ('RECONCILIATION','DUPLICATE') THEN
        UPDATE SCHEMA_TOKEN.okx_demo_automation_guard_states
           SET operational_state='MANUAL_RESET_REQUIRED',health_check_required=true,
               manual_reset_reason=p_failure_class,critical_failure_count=3,
               cooldown_until=NULL,fencing_version=fencing_version+1,
               updated_at=now_value WHERE execution_target_id='OKX_DEMO';
        INSERT INTO SCHEMA_TOKEN.okx_demo_automation_guard_events(
          execution_target_id,event_key,event_kind,failure_class,policy_digest,
          reconciliation_run_id,evidence_snapshot,observed_at)
        VALUES('OKX_DEMO',encode(public.digest(convert_to(
          'manual|'||p_event_key,'UTF8'),'sha256'),'hex'),'MANUAL_LATCHED',
          p_failure_class,p_policy_digest,p_reconciliation_run_id,
          jsonb_build_object('automatic_recovery',false,
            'allow_real_funds',false)::json,now_value);
        RETURN 'MANUAL_RESET_REQUIRED';
      END IF;
      IF guard.failure_window_started_at IS NULL
         OR guard.failure_window_started_at<=now_value-interval '10 minutes' THEN
        next_count:=1;
        UPDATE SCHEMA_TOKEN.okx_demo_automation_guard_states
           SET failure_window_started_at=now_value,critical_failure_count=1,
               updated_at=now_value WHERE execution_target_id='OKX_DEMO';
      ELSE
        next_count:=guard.critical_failure_count+1;
        UPDATE SCHEMA_TOKEN.okx_demo_automation_guard_states
           SET critical_failure_count=LEAST(next_count,3),updated_at=now_value
         WHERE execution_target_id='OKX_DEMO';
      END IF;
      IF next_count>=3 THEN
        UPDATE SCHEMA_TOKEN.okx_demo_automation_guard_states
           SET operational_state='COOLDOWN',cooldown_until=now_value+interval '15 minutes',
               health_check_required=true,fencing_version=fencing_version+1,
               updated_at=now_value WHERE execution_target_id='OKX_DEMO';
        INSERT INTO SCHEMA_TOKEN.okx_demo_automation_guard_events(
          execution_target_id,event_key,event_kind,failure_class,policy_digest,
          reconciliation_run_id,evidence_snapshot,observed_at)
        VALUES('OKX_DEMO',encode(public.digest(convert_to(
          'cooldown|'||p_event_key,'UTF8'),'sha256'),'hex'),'COOLDOWN_ENTERED',
          p_failure_class,p_policy_digest,p_reconciliation_run_id,
          jsonb_build_object('failure_threshold',3,'window_minutes',10,
            'cooldown_minutes',15,'allow_real_funds',false)::json,now_value);
        RETURN 'COOLDOWN';
      END IF;
      RETURN 'RUNNING';
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.record_okx_demo_automation_health(
      p_reconciliation_run_id bigint,p_policy_digest text)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE guard SCHEMA_TOKEN.okx_demo_automation_guard_states%ROWTYPE;
            run_status text; state_status text; frozen boolean;
            current_run_id bigint; completed_at_value timestamptz;
            now_value timestamptz:=clock_timestamp();
    BEGIN
      PERFORM pg_advisory_xact_lock(543000003);
      SELECT * INTO guard FROM SCHEMA_TOKEN.okx_demo_automation_guard_states
       WHERE execution_target_id='OKX_DEMO' FOR UPDATE;
      SELECT status,completed_at INTO run_status,completed_at_value
        FROM SCHEMA_TOKEN.reconciliation_runs
       WHERE id=p_reconciliation_run_id AND execution_target_id='OKX_DEMO';
      SELECT status,opening_frozen,last_reconciliation_run_id
        INTO state_status,frozen,current_run_id
       FROM SCHEMA_TOKEN.okx_demo_reconciliation_states
       WHERE execution_target_id='OKX_DEMO';
      IF guard.execution_target_id IS NULL
         OR guard.authorization_mode<>'CONTINUOUS_DEMO_V1'
         OR guard.policy_digest IS DISTINCT FROM p_policy_digest
         OR run_status NOT IN ('RECONCILED','RECOVERED')
         OR completed_at_value IS NULL
         OR completed_at_value<now_value-interval '90 seconds'
         OR completed_at_value>now_value+interval '5 seconds'
         OR current_run_id IS DISTINCT FROM p_reconciliation_run_id
         OR state_status NOT IN ('RECONCILED','RECOVERED') OR frozen
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
              WHERE execution_target_id='OKX_DEMO' AND state IN
               ('PREPARED','DISPATCHED','ACKNOWLEDGED','RECOVERY_REQUIRED',
                'RESIDUAL_CLOSE_REQUIRED')) THEN RETURN 'BLOCKED'; END IF;
      IF guard.operational_state='COOLDOWN' AND guard.cooldown_until<=now_value THEN
        UPDATE SCHEMA_TOKEN.okx_demo_automation_guard_states
           SET operational_state='RUNNING',critical_failure_count=0,
               failure_window_started_at=NULL,cooldown_until=NULL,
               health_check_required=false,last_healthy_reconciliation_run_id=p_reconciliation_run_id,
               fencing_version=fencing_version+1,updated_at=now_value
         WHERE execution_target_id='OKX_DEMO';
        INSERT INTO SCHEMA_TOKEN.okx_demo_automation_guard_events(
          execution_target_id,event_key,event_kind,failure_class,policy_digest,
          reconciliation_run_id,evidence_snapshot,observed_at)
        VALUES('OKX_DEMO',encode(public.digest(convert_to(
          'health|'||p_reconciliation_run_id::text||'|'||
          guard.fencing_version::text,'UTF8'),'sha256'),'hex'),'HEALTH_RECOVERED',
          NULL,p_policy_digest,p_reconciliation_run_id,
          jsonb_build_object('health_check_required',true,
            'allow_real_funds',false)::json,now_value);
        RETURN 'RUNNING';
      END IF;
      IF guard.operational_state='RUNNING' THEN
        UPDATE SCHEMA_TOKEN.okx_demo_automation_guard_states
           SET last_healthy_reconciliation_run_id=p_reconciliation_run_id,updated_at=now_value
         WHERE execution_target_id='OKX_DEMO';
      END IF;
      RETURN guard.operational_state;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.enable_okx_demo_continuous_automation(
      p_policy_digest text,p_reconciliation_run_id bigint)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE new_fencing bigint; event_digest text; deployment_digest text;
    BEGIN
      PERFORM pg_advisory_xact_lock(543000003);
      SELECT encode(public.digest(convert_to(COALESCE(string_agg(
        deployment.id::text||':'||deployment.active_slot::text||':'||
        deployment.candidate_approval_id::text||':'||deployment.candidate_digest||':'||
        deployment.deployment_policy_digest||':'||
        COALESCE(deployment.risk_policy_digest,''),
        '|' ORDER BY deployment.active_slot),''),
        'UTF8'),'sha256'),'hex') INTO deployment_digest
      FROM SCHEMA_TOKEN.strategy_deployments deployment
      WHERE deployment.execution_target_id='OKX_DEMO' AND deployment.status='ACTIVE';
      IF p_policy_digest!~'^[0-9a-f]{64}$'
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_automation_guard_states
              WHERE execution_target_id='OKX_DEMO')
         OR (SELECT status FROM SCHEMA_TOKEN.reconciliation_runs
              WHERE id=p_reconciliation_run_id)<>'RECOVERED'
         OR (SELECT completed_at FROM SCHEMA_TOKEN.reconciliation_runs
              WHERE id=p_reconciliation_run_id AND execution_target_id='OKX_DEMO')
              NOT BETWEEN clock_timestamp()-interval '90 seconds'
                      AND clock_timestamp()+interval '5 seconds'
         OR (SELECT last_reconciliation_run_id
              FROM SCHEMA_TOKEN.okx_demo_reconciliation_states
              WHERE execution_target_id='OKX_DEMO') IS DISTINCT FROM p_reconciliation_run_id
         OR (SELECT opening_frozen
              FROM SCHEMA_TOKEN.okx_demo_reconciliation_states
              WHERE execution_target_id='OKX_DEMO') IS NOT FALSE
         OR (SELECT count(*) FROM SCHEMA_TOKEN.strategy_deployments
              WHERE status='ACTIVE') NOT BETWEEN 1 AND 3
         OR EXISTS(
              SELECT 1 FROM SCHEMA_TOKEN.strategy_deployments deployment
              JOIN SCHEMA_TOKEN.strategy_candidate_approvals selection
                ON selection.id=deployment.candidate_approval_id
              JOIN SCHEMA_TOKEN.strategies strategy
                ON strategy.id=deployment.strategy_id
              WHERE deployment.status='ACTIVE' AND (
                selection.status<>'APPROVED'
                OR selection.promotion_policy_version<>'okx-demo-selection-v2'
                OR deployment.risk_policy_digest IS DISTINCT FROM p_policy_digest
                OR strategy.name NOT IN ('DeepSeekRegimeCrossoverCandidateB',
                                         'Codex Okx Demo Dual RSI Strategy')
                OR COALESCE((selection.promotion_evidence::jsonb#>>
                    '{selection,production_promotion_claim}')::boolean,true)
                OR COALESCE((selection.promotion_evidence::jsonb#>>
                    '{selection,allow_real_funds}')::boolean,true)
                OR COALESCE((selection.promotion_evidence::jsonb#>>
                    '{selection,minimum_score}')::numeric,-1)<>50))
         OR (SELECT count(*) FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations)<>3
         OR NOT EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_accepted_not_found_terminalizations
              WHERE receipt_depth=3 AND absolute_submission_claim=false)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_demo_submission_grants WHERE status='ACTIVE')
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
              WHERE state IN ('PREPARED','DISPATCHED','ACKNOWLEDGED','RECOVERY_REQUIRED',
                              'RESIDUAL_CLOSE_REQUIRED'))
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.exchange_positions
              WHERE execution_target_id='OKX_DEMO' AND quantity<>0)
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.risk_budgets
              WHERE execution_target_id='OKX_DEMO'
                AND (reserved_notional<>0 OR approved_positions<>0)) THEN RETURN false; END IF;
      INSERT INTO SCHEMA_TOKEN.okx_demo_automation_guard_states(
        execution_target_id,authorization_mode,operational_state,policy_digest,
        deployment_set_digest,
        critical_failure_count,health_check_required,last_healthy_reconciliation_run_id,
        fencing_version)
      VALUES('OKX_DEMO','CONTINUOUS_DEMO_V1','RUNNING',p_policy_digest,
        deployment_digest,0,false,
        p_reconciliation_run_id,1)
      RETURNING fencing_version INTO new_fencing;
      event_digest:=encode(public.digest(convert_to(
        'authorization|'||p_policy_digest||'|'||new_fencing::text,
        'UTF8'),'sha256'),'hex');
      INSERT INTO SCHEMA_TOKEN.okx_demo_automation_guard_events(
        execution_target_id,event_key,event_kind,failure_class,policy_digest,
        reconciliation_run_id,evidence_snapshot,observed_at)
      VALUES('OKX_DEMO',event_digest,'AUTHORIZATION_ENABLED',NULL,p_policy_digest,
        p_reconciliation_run_id,jsonb_build_object('max_active_strategies',3,
          'max_positions',3,'max_order_notional',1000,'max_total_exposure',3000,
          'max_leverage',2,'deployment_set_digest',deployment_digest,
          'allow_real_funds',false)::json,clock_timestamp());
      RETURN true;
    END $$;

    CREATE OR REPLACE FUNCTION SCHEMA_TOKEN.reset_okx_demo_continuous_automation(
      p_policy_digest text,p_reconciliation_run_id bigint)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    DECLARE guard SCHEMA_TOKEN.okx_demo_automation_guard_states%ROWTYPE;
            event_digest text; now_value timestamptz:=clock_timestamp();
    BEGIN
      PERFORM pg_advisory_xact_lock(543000003);
      SELECT * INTO guard FROM SCHEMA_TOKEN.okx_demo_automation_guard_states
       WHERE execution_target_id='OKX_DEMO' FOR UPDATE;
      IF guard.execution_target_id IS NULL
         OR guard.authorization_mode<>'CONTINUOUS_DEMO_V1'
         OR guard.operational_state<>'MANUAL_RESET_REQUIRED'
         OR guard.policy_digest IS DISTINCT FROM p_policy_digest
         OR (SELECT status FROM SCHEMA_TOKEN.reconciliation_runs
              WHERE id=p_reconciliation_run_id AND execution_target_id='OKX_DEMO')
              NOT IN ('RECONCILED','RECOVERED')
         OR (SELECT last_reconciliation_run_id
              FROM SCHEMA_TOKEN.okx_demo_reconciliation_states
              WHERE execution_target_id='OKX_DEMO') IS DISTINCT FROM p_reconciliation_run_id
         OR EXISTS(SELECT 1 FROM SCHEMA_TOKEN.okx_order_write_attempts
              WHERE execution_target_id='OKX_DEMO' AND state IN
                ('PREPARED','DISPATCHED','ACKNOWLEDGED','RECOVERY_REQUIRED',
                 'RESIDUAL_CLOSE_REQUIRED')) THEN RETURN false; END IF;
      UPDATE SCHEMA_TOKEN.okx_demo_automation_guard_states
         SET operational_state='RUNNING',critical_failure_count=0,
             failure_window_started_at=NULL,cooldown_until=NULL,
             health_check_required=false,manual_reset_reason=NULL,
             last_healthy_reconciliation_run_id=p_reconciliation_run_id,
             fencing_version=fencing_version+1,updated_at=now_value
       WHERE execution_target_id='OKX_DEMO';
      event_digest:=encode(public.digest(convert_to(
        'manual-reset|'||p_reconciliation_run_id::text||'|'||
        guard.fencing_version::text,'UTF8'),'sha256'),'hex');
      INSERT INTO SCHEMA_TOKEN.okx_demo_automation_guard_events(
        execution_target_id,event_key,event_kind,failure_class,policy_digest,
        reconciliation_run_id,evidence_snapshot,observed_at)
      VALUES('OKX_DEMO',event_digest,'MANUAL_RESET',NULL,p_policy_digest,
        p_reconciliation_run_id,jsonb_build_object('owner_mediated',true,
          'allow_real_funds',false)::json,now_value);
      RETURN true;
    END $$;
    """.replace("SCHEMA_TOKEN", quoted_schema)
    connection.execute(text(ddl))
    owner_only = {
        "enable_okx_demo_continuous_automation(text,bigint)",
        "reset_okx_demo_continuous_automation(text,bigint)",
        "guard_okx_demo_automation_event()",
        "guard_active_demo_strategy_material()",
    }
    runtime_functions = {
        "okx_demo_continuous_opening_allowed(text)",
        "claim_okx_demo_continuous_dispatch(bigint,text)",
        "record_okx_demo_automation_failure(text,text,bigint,text)",
        "record_okx_demo_automation_health(bigint,text)",
    }
    for signature in owner_only | runtime_functions:
        connection.execute(text(
            "ALTER FUNCTION {0}.{1} OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON FUNCTION {0}.{1} FROM PUBLIC,freqtrade; {2}"
            .format(
                quoted_schema,
                signature,
                (
                    "GRANT EXECUTE ON FUNCTION {0}.{1} TO freqtrade"
                    .format(quoted_schema, signature)
                    if signature in runtime_functions else ""
                ),
            )
        ))
    for table_name in (
        "okx_demo_automation_guard_states",
        "okx_demo_automation_guard_events",
    ):
        connection.execute(text(
            "ALTER TABLE {0}.{1} OWNER TO freqtrade_ai_attestor; "
            "REVOKE ALL ON TABLE {0}.{1} FROM PUBLIC,freqtrade; "
            "GRANT SELECT ON TABLE {0}.{1} TO freqtrade"
            .format(quoted_schema, table_name)
        ))
    connection.execute(text(
        "ALTER SEQUENCE {0}.okx_demo_automation_guard_events_id_seq "
        "OWNER TO freqtrade_ai_attestor; REVOKE ALL ON SEQUENCE "
        "{0}.okx_demo_automation_guard_events_id_seq FROM PUBLIC,freqtrade"
        .format(quoted_schema)
    ))


def _finalize_current_canary_boundaries(connection: Connection) -> list[str]:
    """Converge every supported upgrade path on the complete current boundary."""

    required_tables = {
        "approved_executions",
        "exchange_orders",
        "full_chain_runs",
        "okx_demo_attestation_secrets",
        "okx_demo_fill_snapshots",
        "okx_demo_order_snapshots",
        "okx_demo_position_snapshots",
        "okx_demo_reconciliation_states",
        "okx_demo_recovery_batches",
        "okx_demo_recovery_grants",
        "okx_demo_submission_grants",
        "okx_demo_trusted_snapshots",
        "okx_order_write_attempts",
        "reconciliation_runs",
        "research_jobs",
        "risk_budgets",
        "risk_decisions",
        "trade_intents",
    }
    schema_name = connection.execute(text("SELECT current_schema()" )).scalar_one()
    actual_tables = set(inspect(connection).get_table_names(schema=schema_name))
    missing_tables = sorted(required_tables - actual_tables)
    if missing_tables:
        raise SchemaMigrationBlocked(
            "v28 canary boundary dependencies are incomplete: "
            + ", ".join(missing_tables)
        )
    grant_foreign_keys = {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspect(connection).get_foreign_keys(
            "okx_demo_submission_grants",
            schema=schema_name,
        )
    }
    for column, target, ondelete in (
        ("execution_target_id", "execution_scopes(scope_id)", ""),
        ("approval_id", "approved_executions(id)", " ON DELETE RESTRICT"),
        (
            "reconciliation_run_id",
            "reconciliation_runs(id)",
            " ON DELETE RESTRICT",
        ),
    ):
        if (column,) not in grant_foreign_keys:
            connection.execute(
                text(
                    "ALTER TABLE okx_demo_submission_grants "
                    "ADD CONSTRAINT okx_demo_submission_grants_{}_fkey "
                    "FOREIGN KEY ({}) REFERENCES {}{}".format(
                        column,
                        column,
                        target,
                        ondelete,
                    )
                )
            )
    _add_controlled_canary_lifecycle_boundary(connection)
    if "okx_demo_canary_consent_handoffs" in actual_tables:
        connection.execute(text(
            "ALTER TABLE okx_demo_canary_consent_handoffs "
            "ADD COLUMN IF NOT EXISTS supersedes_handoff_id varchar(32)"
        ))
    _add_canary_consent_handoff_boundary(connection)
    # Reinstall the exchange-order guard on every supported upgrade path so
    # v31 databases gain the receipt-bound terminal status transition too.
    _add_okx_demo_runtime_recovery_binding(connection)
    _add_accepted_not_found_terminalization_boundary(connection)
    _add_deferred_canary_terminalization_foreign_keys(connection)
    _add_bounded_second_accepted_not_found_boundary(connection)
    _add_final_accepted_not_found_boundary(connection)
    _add_atomic_canary_prepare_boundary(connection)
    _add_continuous_demo_automation_boundary(connection)
    _add_research_receipt_boundary(connection)
    _grant_runtime_application_acl(connection)
    return schema_problems(connection)


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
            if current_version == RESEARCH_PERSISTENCE_BASE_VERSION:
                _add_research_receipt_boundary(connection)
                _grant_runtime_application_acl(connection)
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Research receipt upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
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
                SINGLE_ACTIVE_DEPLOYMENT_BASE_VERSION,
                ONE_SHOT_SUBMISSION_GRANT_BASE_VERSION,
                STRATEGY_VALIDATION_BASE_VERSION,
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
                _ensure_one_shot_submission_grant(connection)
                if {
                    "approved_executions",
                    "trade_intents",
                    "risk_decisions",
                    "full_chain_runs",
                    "risk_budgets",
                }.issubset(
                    inspect(connection).get_table_names(
                        schema=connection.execute(
                            text("SELECT current_schema()")
                        ).scalar_one()
                    )
                ):
                    _grant_expired_approval_attestor_acl(connection)
                _add_strategy_validation_matrix(connection)
                schema_name = connection.execute(
                    text("SELECT current_schema()")
                ).scalar_one()
                if CANARY_LINEAGE_BOUNDARY_TABLES.issubset(
                    inspect(connection).get_table_names(schema=schema_name)
                ):
                    _add_canary_lineage_write_boundary(connection)
            if current_version in {
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
                }:
                problems = _finalize_current_canary_boundaries(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Controlled canary lifecycle upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == CANARY_LINEAGE_WRITE_BASE_VERSION:
                problems = _finalize_current_canary_boundaries(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Canary lineage write upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == STRATEGY_VALIDATION_BASE_VERSION:
                problems = _finalize_current_canary_boundaries(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Strategy validation matrix upgrade does not match ORM metadata: "
                        + "; ".join(problems)
                    )
                connection.execute(
                    text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                    {"version": SCHEMA_VERSION},
                )
                return SCHEMA_VERSION
            if current_version == RECONCILIATION_INDEX_BASE_VERSION:
                _add_strategy_deployment_queue(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
            if current_version == SINGLE_ACTIVE_DEPLOYMENT_BASE_VERSION:
                _add_single_active_strategy_deployment_index(connection)
                problems = _finalize_current_canary_boundaries(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Single active deployment upgrade does not match ORM metadata: "
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
                problems = _finalize_current_canary_boundaries(connection)
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
                problems = _finalize_current_canary_boundaries(connection)
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
                problems = _finalize_current_canary_boundaries(connection)
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
                problems = _finalize_current_canary_boundaries(connection)
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
                problems = _finalize_current_canary_boundaries(connection)
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
                problems = _finalize_current_canary_boundaries(connection)
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
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_controlled_canary_lifecycle_boundary(connection)
                _add_full_chain(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_controlled_canary_lifecycle_boundary(connection)
                _add_full_chain(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_controlled_canary_lifecycle_boundary(connection)
                _add_full_chain(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_controlled_canary_lifecycle_boundary(connection)
                _add_full_chain(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_controlled_canary_lifecycle_boundary(connection)
                _add_full_chain(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_controlled_canary_lifecycle_boundary(connection)
                _add_full_chain(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_controlled_canary_lifecycle_boundary(connection)
                _add_full_chain(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_controlled_canary_lifecycle_boundary(connection)
                _add_full_chain(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_controlled_canary_lifecycle_boundary(connection)
                _add_full_chain(connection)
                _add_canary_consent_handoff_boundary(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_controlled_canary_lifecycle_boundary(connection)
                _add_full_chain(connection)
                _add_canary_consent_handoff_boundary(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_controlled_canary_lifecycle_boundary(connection)
                _add_full_chain(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                _add_canary_consent_handoff_boundary(connection)
                problems = _finalize_current_canary_boundaries(connection)
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
                problems = _finalize_current_canary_boundaries(connection)
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
                problems = _finalize_current_canary_boundaries(connection)
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
            if current_version == ONE_SHOT_SUBMISSION_GRANT_BASE_VERSION:
                _ensure_one_shot_submission_grant(connection)
                _grant_expired_approval_attestor_acl(connection)
                _grant_runtime_application_acl(connection)
                problems = _finalize_current_canary_boundaries(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "One-shot submission grant upgrade does not match ORM metadata: "
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
            # The one-shot grant trigger/table are owned by the least-privilege
            # attestor role.  Establish that role boundary before installing
            # the grant ACL so a fresh PostgreSQL database cannot fail closed
            # merely because the role has not existed yet.
            _add_attested_session_boundary(connection)
            _ensure_one_shot_submission_grant(connection)
            _add_order_writer(connection)
            _add_okx_demo_reconciliation(connection)
            _add_okx_demo_runtime_recovery_binding(connection)
            _add_full_chain(connection)
            _add_strategy_validation_matrix(connection)
            _add_controlled_canary_lifecycle_boundary(connection)
            _add_canary_consent_handoff_boundary(connection)
            problems = _finalize_current_canary_boundaries(connection)
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
