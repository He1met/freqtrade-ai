import hashlib
import os
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier, Lock, Thread
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.adapters.okx_demo import read_adapter as read_boundary
from app.adapters.okx_demo.errors import OkxReadAdapterError
from app.adapters.okx_demo.transport import OkxReadHttpResponse
from app.adapters.okx_demo.write_transport import (
    _create_attested_writer_credential_bridge,
)
from app.db.migrations import (
    ATTESTATION_ACL_BASE_VERSION,
    ATTESTED_SESSION_BASE_VERSION,
    RISK_CHAIN_BASE_VERSION,
    RISK_CHAIN_HARDENING_BASE_VERSION,
    SCHEMA_VERSION,
    NATURAL_SIGNAL_EVALUATOR_RECEIPT_BASE_VERSION,
    NATURAL_SIGNAL_RISK_BUDGET_BASE_VERSION,
    STALE_NATURAL_APPROVAL_RELEASE_BASE_VERSION,
    SchemaMigrationBlocked,
    TRUSTED_SNAPSHOT_BASE_VERSION,
    VERSION_TABLE,
    _add_natural_signal_risk_chain_boundary,
    _add_trusted_snapshot_boundary,
    harden_attestation_access_boundary,
    schema_problems,
    upgrade_database,
    verify_schema,
)
from app.db.session import create_database_engine, create_session_factory
from app.models import (
    ApprovedExecution,
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    ExchangeOrder,
    FullChainRun,
    FullChainSignalSnapshot,
    FullChainStageRun,
    OkxDemoAttestedSession,
    OkxOrderWriterLease,
    OkxDemoTrustedSnapshot,
    ReconciliationRun,
    RiskBudget,
    RiskDecision,
    ResearchJob,
    ResearchJobAttempt,
    Strategy,
    StrategyCandidateApproval,
    StrategyScore,
    StrategyVersion,
    TradeIntent,
)
from app.models.okx_demo_reconciliation import (
    OkxDemoReconciliationState,
    OkxDemoRecoveryBatch,
)
from app.models.strategy_deployment import SignalEvaluation, StrategyDeployment
from app.adapters.okx_demo.writer_repository import SqlAlchemyOrderWriterStore
from app.models.execution_lineage import OKX_DEMO_TARGET_ID
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.repositories.full_chain import _stable_digest as full_chain_digest
from app.services.risk_chain import (
    RiskChainService,
    _issue_attested_session_capability,
    _normalize_attested_snapshot,
    _persist_attested_session,
    _revoke_attested_session,
    _write_attested_snapshot,
    canonical_digest,
)


POSTGRES_WORKER_URL = os.environ.get("POSTGRES_WORKER_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_WORKER_URL,
    reason="POSTGRES_WORKER_URL is required for the PostgreSQL risk-chain gate",
)


@pytest.fixture()
def postgres_engine():
    assert POSTGRES_WORKER_URL is not None
    schema = "risk_chain_{}".format(uuid4().hex)
    admin = create_database_engine(POSTGRES_WORKER_URL)
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "{}"'.format(schema)))
    engine = create_engine(
        POSTGRES_WORKER_URL,
        pool_pre_ping=True,
        connect_args={"options": "-csearch_path={}".format(schema)},
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "{}" CASCADE'.format(schema)))
        admin.dispose()


def _seed(factory, *, suffix: str = "") -> dict[str, int]:
    with factory.begin() as db:
        ensure_execution_scope_catalog(db)
        strategy = Strategy(
            name="PG risk chain" + (" " + suffix if suffix else ""),
            slug="pg-risk-chain" + ("-" + suffix if suffix else ""),
        )
        db.add(strategy)
        db.flush()
        version = StrategyVersion(
            strategy_id=strategy.id,
            version_number=1,
            blueprint={},
            generated_code="class PGRiskChain: pass",
            file_path="/tmp/pg-risk-chain{}.py".format(
                "-" + suffix if suffix else ""
            ),
            validation_status="passed",
        )
        db.add(version)
        db.flush()
        strategy.current_version_id = version.id
        run = BacktestRun(
            execution_scope_id="LOCAL_DRY_RUN",
            strategy_version_id=version.id,
            config_snapshot={},
            status="succeeded",
        )
        db.add(run)
        db.flush()
        task = BacktestTask(
            backtest_run_id=run.id,
            pair="BTC/USDT:USDT",
            timeframe="15m",
            status="succeeded",
        )
        db.add(task)
        db.flush()
        result = BacktestResult(
            backtest_run_id=run.id,
            backtest_task_id=task.id,
            result_path="/tmp/pg-result.json",
            metrics_snapshot={},
        )
        db.add(result)
        db.flush()
        score = StrategyScore(
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            backtest_result_id=result.id,
            scoring_version="risk-chain-pg-v1",
            total_score=80,
        )
        db.add(score)
        db.flush()
        identity = uuid4().hex
        job = ResearchJob(
            execution_scope_id="LOCAL_DRY_RUN",
            job_type="deepseek_backtest",
            operation="strategy_generation.deepseek_backtest_loop",
            idempotency_key_digest=identity * 2,
            request_hash=uuid4().hex * 2,
            request_payload={"allow_real_call": False},
            status="RUNNING",
            stage="RISK",
            attempt_count=1,
            max_attempts=1,
        )
        db.add(job)
        db.flush()
        attempt = ResearchJobAttempt(
            research_job_id=job.id,
            attempt_number=1,
            execution_scope_id="LOCAL_DRY_RUN",
            status="RUNNING",
        )
        db.add(attempt)
        db.flush()
        chain = FullChainRun(
            research_job_id=job.id,
            research_job_attempt_id=attempt.id,
            research_scope_id="LOCAL_DRY_RUN",
            execution_target_id=OKX_DEMO_TARGET_ID,
            status="EXECUTING",
            current_stage="RISK",
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            backtest_run_id=run.id,
            backtest_task_id=task.id,
            backtest_result_id=result.id,
            strategy_score_id=score.id,
        )
        db.add(chain)
        db.flush()
        approval = StrategyCandidateApproval(
            full_chain_run_id=chain.id,
            execution_target_id=OKX_DEMO_TARGET_ID,
            strategy_version_id=version.id,
            backtest_result_id=result.id,
            strategy_score_id=score.id,
            candidate_digest=uuid4().hex * 2,
            promotion_policy_version="strategy-promotion-v1",
            promotion_evidence={"eligible": True},
            status="APPROVED",
            requested_by="system:test",
            decided_by="system:test",
            decision_reason="PostgreSQL risk fixture.",
            requested_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            decided_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
        )
        db.add(approval)
        db.flush()
        signal = FullChainSignalSnapshot(
            full_chain_run_id=chain.id,
            candidate_approval_id=approval.id,
            execution_target_id=OKX_DEMO_TARGET_ID,
            instrument_id="BTC-USDT-SWAP",
            signal_digest=uuid4().hex * 2,
            source_type="api_aggregate",
            core_data=True,
            source_database_ids={"market_snapshot_id": 1},
            signal_snapshot={"side": "buy", "closed_candle": True},
            observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
        )
        db.add(signal)
        db.flush()
        chain.candidate_approval_id = approval.id
        chain.signal_snapshot_id = signal.id
        db.add(
            FullChainStageRun(
                full_chain_run_id=chain.id,
                stage="SIGNAL",
                status="SUCCESS",
                idempotency_key_digest=uuid4().hex * 2,
                input_digest=uuid4().hex * 2,
                input_snapshot={"instrument_id": "BTC-USDT-SWAP"},
                output_snapshot={"status": "succeeded"},
                database_ids={"signal_snapshot_id": signal.id},
                prepared_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                completed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
        db.flush()
        return {
            "strategy_id": strategy.id,
            "strategy_version_id": version.id,
            "backtest_run_id": run.id,
            "backtest_task_id": task.id,
            "backtest_result_id": result.id,
            "strategy_score_id": score.id,
            "_full_chain_run_id": chain.id,
            "_candidate_approval_id": approval.id,
            "_signal_snapshot_id": signal.id,
            "_signal_digest": signal.signal_digest,
        }


def _request(
    lineage: dict[str, int],
    now: datetime,
    factory=None,
    capability_sink=None,
) -> dict:
    expiry = (now + timedelta(minutes=5)).isoformat()
    instrument = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "source": "okx_demo_rest",
        "resource": "instrument",
        "stale": False,
        "authenticated": False,
        "instId": "BTC-USDT-SWAP",
        "instrument_type": "SWAP",
        "ctVal": "1",
        "ctValCcy": "BTC",
        "lotSz": "0.001",
        "minSz": "0.001",
        "tickSz": "0.1",
        "contract_shape": "linear",
        "expires_at": expiry,
    }
    market = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "source": "okx_demo_rest",
        "resource": "market",
        "stale": False,
        "authenticated": False,
        "instrument_id": "BTC-USDT-SWAP",
        "reference_price": "50000",
        "as_of": now.isoformat(),
        "expires_at": expiry,
    }
    account = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "source": "okx_demo_rest",
        "resource": "account",
        "stale": False,
        "authenticated": True,
        "account_mode": "long_short_mode",
        "margin_mode": "isolated",
        "current_exposure": "0",
        "open_positions": 0,
        "exposure_by_position_side": {"long": "0", "short": "0"},
        "open_positions_by_position_side": {"long": 0, "short": 0},
        "leverage_by_position_side": {"long": "2", "short": "2"},
        "as_of": now.isoformat(),
        "expires_at": expiry,
    }
    def envelope(name: str, content: dict) -> dict:
        digest = canonical_digest(content)
        return {
            "ref": "{}:{}".format(name, digest[:24]),
            "digest": digest,
            "expires_at": expiry,
            "content": content,
        }

    request = {
        "execution_target": OKX_DEMO_TARGET_ID,
        "full_chain_run_id": lineage["_full_chain_run_id"],
        "candidate_approval_id": lineage["_candidate_approval_id"],
        "signal_snapshot_id": lineage["_signal_snapshot_id"],
        "signal_digest": lineage["_signal_digest"],
        "lineage": {
            name: lineage[name]
            for name in (
                "strategy_id",
                "strategy_version_id",
                "backtest_run_id",
                "backtest_task_id",
                "backtest_result_id",
                "strategy_score_id",
            )
        },
        "snapshots": {
            name: envelope(name, content)
            for name, content in (
                ("instrument", instrument),
                ("market", market),
                ("account", account),
            )
        },
        "instrument_id": "BTC-USDT-SWAP",
        "side": "buy",
        "position_side": "long",
        "order_type": "limit",
        "quantity": "0.012",
        "limit_price": "50000",
        "reference_price": "50000",
        "leverage": "2",
        "margin_mode": "isolated",
        "stop_loss": "48000",
        "take_profit": "54000",
        "reduce_only": False,
    }
    if factory is not None:
        snapshots = request.pop("snapshots")
        snapshot_ids = {}
        capability = _issue_attested_session_capability(
            attestation_hmac_key=b"t" * 32,
            pinned_fingerprint_sha256="e" * 64,
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        if capability_sink is not None:
            capability_sink.append(capability)
        with factory.begin() as db:
            for name, envelope in snapshots.items():
                snapshot_expiry = datetime.fromisoformat(
                    envelope["content"]["expires_at"].replace("Z", "+00:00")
                )
                normalized = _normalize_attested_snapshot(
                    capability,
                    kind=name,
                    content=envelope["content"],
                    observed_at=now,
                    expires_at=snapshot_expiry,
                )
                row = _write_attested_snapshot(
                    db,
                    capability,
                    normalized,
                    now=now,
                )
                snapshot_ids[name] = row.snapshot_id
        request["snapshot_ids"] = snapshot_ids
    return request


def _policy() -> dict:
    return {
        "allowed_instruments": ["BTC-USDT-SWAP"],
        "allowed_sides": ["buy", "sell"],
        "allowed_order_types": ["limit", "market"],
        "max_leverage": "3",
        "max_order_notional": "1000",
        "max_total_exposure": "600",
        "max_positions": 1,
        "max_price_deviation_pct": "0.02",
        "min_strategy_score": "70",
        "scoring_version": "risk-chain-pg-v1",
    }


def _seed_unclaimed_natural_approval(
    factory,
    *,
    expired: bool = True,
    with_exchange_order: bool = False,
    with_execution_checkpoint: bool = False,
) -> dict[str, object]:
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    with factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="stale-natural-{}".format(uuid4().hex),
            request=_request(lineage, now, factory),
            policy=_policy(),
            now=now,
        )
    assert result.status == "APPROVED"
    assert result.approved_execution_id is not None

    with factory.begin() as db:
        approval = db.get(ApprovedExecution, result.approved_execution_id)
        assert approval is not None
        intent = db.get(TradeIntent, approval.trade_intent_id)
        decision = db.get(RiskDecision, approval.risk_decision_id)
        chain = db.get(FullChainRun, lineage["_full_chain_run_id"])
        assert intent is not None and decision is not None and chain is not None
        if expired:
            stale_at = now - timedelta(seconds=10)
            # Isolated owner-only fixture setup: move the immutable intent clock
            # only while no ACTIVE approval exists, then restore the exact state
            # that natural wall-clock expiry produces.
            db.execute(
                text("UPDATE approved_executions SET status='EXPIRED' WHERE id=:id"),
                {"id": approval.id},
            )
            db.execute(
                text("UPDATE trade_intents SET expires_at=:stale_at WHERE id=:id"),
                {"stale_at": stale_at, "id": intent.id},
            )
            db.execute(
                text(
                    "UPDATE approved_executions SET expires_at=:stale_at,status='ACTIVE' "
                    "WHERE id=:id"
                ),
                {"stale_at": stale_at, "id": approval.id},
            )
            db.expire_all()
            approval = db.get(ApprovedExecution, result.approved_execution_id)
            intent = db.get(TradeIntent, approval.trade_intent_id)
            decision = db.get(RiskDecision, approval.risk_decision_id)
            chain = db.get(FullChainRun, lineage["_full_chain_run_id"])
            assert intent is not None and decision is not None and chain is not None
        approval.evidence_snapshot = {
            **approval.evidence_snapshot,
            "natural_signal": True,
        }
        decision.evidence_snapshot = {
            **decision.evidence_snapshot,
            "natural_signal": True,
        }
        chain.trade_intent_id = intent.id
        chain.risk_decision_id = decision.id
        chain.approved_execution_id = approval.id
        chain.status = "EXECUTING"
        chain.current_stage = "EXECUTION"
        if with_exchange_order:
            order = ExchangeOrder(
                execution_target_id=OKX_DEMO_TARGET_ID,
                trade_intent_id=intent.id,
                client_order_id=intent.client_order_id,
                exchange_order_id=None,
                status="PREPARED",
                request_snapshot={},
                response_snapshot={},
            )
            db.add(order)
            db.flush()
            chain.exchange_order_id = order.id
        if with_execution_checkpoint:
            db.add(
                FullChainStageRun(
                    full_chain_run_id=chain.id,
                    stage="EXECUTION",
                    status="PREPARED",
                    idempotency_key_digest=uuid4().hex * 2,
                    input_digest=uuid4().hex * 2,
                    input_snapshot={},
                    output_snapshot={},
                    database_ids={"approved_execution_id": approval.id},
                    prepared_at=now,
                )
            )
        reserved_notional = approval.reserved_notional
    return {
        "approval_id": result.approved_execution_id,
        "chain_id": lineage["_full_chain_run_id"],
        "reserved_notional": reserved_notional,
    }


def test_20260727_02_upgrades_to_risk_chain_atomically(postgres_engine) -> None:
    pre_bridge_tables = [
        table
        for table in Base.metadata.tables.values()
        if table.name != "strategy_research_candidate_bridge_events"
    ]
    Base.metadata.create_all(postgres_engine, tables=pre_bridge_tables)
    with postgres_engine.begin() as connection:
        connection.execute(text("DROP TABLE okx_demo_automation_guard_events"))
        connection.execute(text("DROP TABLE okx_demo_automation_guard_states"))
        connection.execute(
            text(
                "ALTER TABLE okx_demo_canary_consent_handoffs "
                "DROP CONSTRAINT "
                "okx_demo_canary_consent_handoffs_full_chain_run_id_fkey, "
                "DROP CONSTRAINT "
                "okx_demo_canary_consent_handoffs_approval_id_fkey, "
                "DROP CONSTRAINT "
                "okx_demo_canary_consent_handoffs_grant_id_fkey; "
                "ALTER TABLE okx_demo_submission_grants "
                "DROP CONSTRAINT "
                "okx_demo_submission_grants_handoff_id_fkey"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE full_chain_runs "
                "DROP CONSTRAINT IF EXISTS "
                "full_chain_runs_signal_evaluation_id_fkey"
            )
        )
        connection.execute(text("DROP TABLE signal_evaluations"))
        connection.execute(text("DROP TABLE strategy_deployments"))
        for table_name in (
            "full_chain_signal_snapshots",
            "full_chain_stage_runs",
            "strategy_candidate_approvals",
            "full_chain_runs",
        ):
            connection.execute(text('DROP TABLE "{}"'.format(table_name)))
        connection.execute(
            text("ALTER TABLE okx_demo_recovery_grants DROP COLUMN lifecycle_id")
        )
        connection.execute(
            text(
                "DROP TABLE okx_demo_accepted_not_found_terminalizations CASCADE"
            )
        )
        connection.execute(text("DROP TABLE okx_demo_canary_lifecycles"))
        connection.execute(text("DROP TABLE okx_demo_submission_grants"))
        connection.execute(text("DROP TABLE approved_executions"))
        connection.execute(text("DROP TABLE risk_budgets"))
        connection.execute(
            text(
                "ALTER TABLE trade_intents "
                "DROP CONSTRAINT trade_intents_intent_id_key, "
                "DROP CONSTRAINT trade_intents_target_idempotency_unique, "
                "DROP CONSTRAINT trade_intents_approval_identity_unique, "
                "DROP CONSTRAINT trade_intents_status_check, "
                "DROP CONSTRAINT trade_intents_intent_id_format_check, "
                "DROP CONSTRAINT trade_intents_canonical_hash_format_check, "
                "DROP CONSTRAINT trade_intents_policy_digest_format_check, "
                "DROP CONSTRAINT trade_intents_idempotency_digest_format_check, "
                "DROP CONSTRAINT trade_intents_side_check, "
                "DROP CONSTRAINT trade_intents_position_side_check, "
                "DROP CONSTRAINT trade_intents_margin_mode_check, "
                "DROP CONSTRAINT trade_intents_order_type_check, "
                "DROP CONSTRAINT trade_intents_order_combo_check, "
                "DROP COLUMN intent_id, DROP COLUMN canonical_hash, "
                "DROP COLUMN policy_digest, DROP COLUMN idempotency_key_digest, "
                "DROP COLUMN strategy_id, "
                "DROP COLUMN backtest_run_id, DROP COLUMN backtest_result_id, "
                "DROP COLUMN strategy_score_id, DROP COLUMN expires_at, "
                "DROP COLUMN reference_price, DROP COLUMN leverage, "
                "DROP COLUMN margin_mode, DROP COLUMN stop_loss, "
                "DROP COLUMN take_profit, DROP COLUMN reduce_only"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE risk_decisions "
                "DROP CONSTRAINT risk_decisions_id_intent_unique, "
                "DROP CONSTRAINT risk_decisions_decision_check"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE {} (version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())".format(VERSION_TABLE)
            )
        )
        connection.execute(
            text("INSERT INTO {} (version) VALUES (:version)".format(VERSION_TABLE)),
            {"version": RISK_CHAIN_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True


def test_20260727_03_hardens_existing_risk_chain(postgres_engine) -> None:
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE approved_executions "
                "DROP CONSTRAINT approved_executions_intent_identity_fkey, "
                "DROP CONSTRAINT approved_executions_decision_intent_fkey, "
                "DROP CONSTRAINT approved_executions_payload_identity_fkey, "
                "DROP CONSTRAINT approved_executions_claim_required_check, "
                "DROP CONSTRAINT approved_executions_status_check, "
                "DROP CONSTRAINT approved_executions_approved_state_check, "
                "DROP CONSTRAINT approved_executions_reservation_check, "
                "DROP CONSTRAINT approved_executions_client_order_id_format_check, "
                "DROP CONSTRAINT approved_executions_intent_id_format_check, "
                "DROP CONSTRAINT approved_executions_authorization_schema_check, "
                "DROP CONSTRAINT approved_executions_canonical_hash_format_check, "
                "DROP CONSTRAINT approved_executions_policy_digest_format_check, "
                    "DROP CONSTRAINT approved_executions_payload_hash_format_check, "
                    "DROP CONSTRAINT approved_executions_instrument_snapshot_fkey, "
                    "DROP CONSTRAINT approved_executions_market_snapshot_fkey, "
                    "DROP CONSTRAINT approved_executions_account_snapshot_fkey, "
                "DROP COLUMN status, DROP COLUMN decision, "
                "DROP COLUMN intent_status, DROP COLUMN reserved_notional, "
                "DROP COLUMN authorization_schema_version, "
                    "DROP COLUMN canonical_hash, DROP COLUMN policy_digest, "
                    "DROP COLUMN approved_payload_hash, "
                    "DROP COLUMN instrument_snapshot_id, "
                    "DROP COLUMN market_snapshot_id, "
                    "DROP COLUMN account_snapshot_id"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE trade_intents "
                "DROP CONSTRAINT trade_intents_approval_identity_unique, "
                "DROP CONSTRAINT trade_intents_approved_payload_unique, "
                "DROP CONSTRAINT trade_intents_status_check, "
                "DROP CONSTRAINT trade_intents_authorization_schema_check, "
                "DROP CONSTRAINT trade_intents_scope_contract_check, "
                "DROP CONSTRAINT trade_intents_intent_id_format_check, "
                "DROP CONSTRAINT trade_intents_canonical_hash_format_check, "
                "DROP CONSTRAINT trade_intents_policy_digest_format_check, "
                "DROP CONSTRAINT trade_intents_idempotency_digest_format_check, "
                "DROP CONSTRAINT trade_intents_side_check, "
                "DROP CONSTRAINT trade_intents_position_side_check, "
                "DROP CONSTRAINT trade_intents_margin_mode_check, "
                "DROP CONSTRAINT trade_intents_order_type_check, "
                "DROP CONSTRAINT trade_intents_order_combo_check, "
                "DROP COLUMN authorization_schema_version, "
                "DROP COLUMN approved_payload_hash, "
                "DROP COLUMN policy_digest, DROP COLUMN reference_price, "
                "DROP COLUMN leverage, DROP COLUMN margin_mode, "
                "DROP COLUMN stop_loss, DROP COLUMN take_profit, "
                "DROP COLUMN reduce_only, "
                "ALTER COLUMN instrument_id SET NOT NULL, "
                "ALTER COLUMN side SET NOT NULL, "
                "ALTER COLUMN position_side SET NOT NULL, "
                "ALTER COLUMN order_type SET NOT NULL, "
                "ALTER COLUMN quantity SET NOT NULL"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE risk_decisions "
                "DROP CONSTRAINT risk_decisions_id_intent_unique, "
                "DROP CONSTRAINT risk_decisions_decision_check, "
                "DROP CONSTRAINT risk_decisions_authorization_schema_check, "
                "DROP CONSTRAINT risk_decisions_policy_digest_format_check, "
                "DROP COLUMN authorization_schema_version, "
                "DROP COLUMN policy_digest"
            )
        )
        connection.execute(text("DROP TABLE okx_demo_trusted_snapshots"))
        connection.execute(
            text(
                "ALTER TABLE risk_budgets "
                "DROP CONSTRAINT risk_budgets_nonnegative_check"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE {} (version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())".format(VERSION_TABLE)
            )
        )
        connection.execute(
            text("INSERT INTO {} (version) VALUES (:version)".format(VERSION_TABLE)),
            {"version": RISK_CHAIN_HARDENING_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True


def test_20260727_04_adds_trusted_registry_and_immutability_triggers(
    postgres_engine,
) -> None:
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE approved_executions "
                "DROP CONSTRAINT approved_executions_instrument_snapshot_fkey, "
                "DROP CONSTRAINT approved_executions_market_snapshot_fkey, "
                "DROP CONSTRAINT approved_executions_account_snapshot_fkey, "
                "DROP COLUMN instrument_snapshot_id, "
                "DROP COLUMN market_snapshot_id, "
                "DROP COLUMN account_snapshot_id"
            )
        )
        connection.execute(text("DROP TABLE okx_demo_trusted_snapshots"))
        connection.execute(
            text(
                "CREATE TABLE {} (version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())".format(VERSION_TABLE)
            )
        )
        connection.execute(
            text("INSERT INTO {} (version) VALUES (:version)".format(VERSION_TABLE)),
            {"version": TRUSTED_SNAPSHOT_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True
    with postgres_engine.connect() as connection:
        triggers = set(
            connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal"
                )
            ).scalars()
        )
    assert "trade_intents_active_approval_immutable" in triggers
    assert "okx_demo_trusted_snapshots_immutable" in triggers


def test_20260727_05_adds_private_attested_session_boundary(
    postgres_engine,
) -> None:
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        _add_trusted_snapshot_boundary(connection)
        connection.execute(
            text(
                "ALTER TABLE okx_demo_trusted_snapshots "
                "DROP CONSTRAINT okx_demo_trusted_snapshots_session_fkey, "
                "DROP CONSTRAINT okx_demo_trusted_snapshots_time_check, "
                "DROP COLUMN attested_session_expires_at"
            )
        )
        connection.execute(text("DROP TABLE okx_demo_attested_sessions"))
        connection.execute(
            text(
                "CREATE TABLE {} (version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())".format(VERSION_TABLE)
            )
        )
        connection.execute(
            text("INSERT INTO {} (version) VALUES (:version)".format(VERSION_TABLE)),
            {"version": ATTESTED_SESSION_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is True
    with postgres_engine.connect() as connection:
        boundaries = connection.execute(
            text(
                "SELECT c.relname, r.rolname "
                "FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() "
                "AND c.relname IN ('okx_demo_attested_sessions', "
                "'okx_demo_trusted_snapshots')"
            )
        ).all()
    assert set(boundaries) == {
        ("okx_demo_attested_sessions", "freqtrade_ai_attestor"),
        ("okx_demo_trusted_snapshots", "freqtrade_ai_attestor"),
    }


def test_20260727_07_adds_approval_snapshot_foreign_keys(
    postgres_engine,
) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE approved_executions "
                "DROP CONSTRAINT approved_executions_instrument_snapshot_fkey, "
                "DROP CONSTRAINT approved_executions_market_snapshot_fkey, "
                "DROP CONSTRAINT approved_executions_account_snapshot_fkey, "
                "DROP COLUMN instrument_snapshot_id, "
                "DROP COLUMN market_snapshot_id, "
                "DROP COLUMN account_snapshot_id"
            )
        )
        connection.execute(text("DELETE FROM {}".format(VERSION_TABLE)))
        connection.execute(
            text(
                "INSERT INTO {} (version) VALUES (:version)".format(
                    VERSION_TABLE
                )
            ),
            {"version": ATTESTATION_ACL_BASE_VERSION},
        )
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True


def test_readiness_fails_closed_before_peer_admin_hardening(
    postgres_engine,
) -> None:
    Base.metadata.create_all(postgres_engine)
    problems = schema_problems(postgres_engine)
    assert any(
        problem.startswith("attestation owner mismatch:")
        for problem in problems
    )
    assert any(
        problem.startswith("attestation function boundary mismatch:")
        for problem in problems
    )
    assert "controlled canary consent secret table missing" in problems


@pytest.mark.parametrize(
    ("tamper_sql", "expected_problem"),
    [
        (
            "ALTER ROLE freqtrade_ai_attestor LOGIN INHERIT",
            "attestor role boundary mismatch",
        ),
        (
            "GRANT SELECT ON okx_demo_attestation_secrets TO freqtrade",
            "runtime can read attestation secret table",
        ),
        (
            """
            CREATE OR REPLACE FUNCTION write_okx_demo_attested_session(
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
            AS $$ BEGIN RETURN; END; $$;
            ALTER FUNCTION write_okx_demo_attested_session(
                text,text,text,bigint,bigint,text,text
            ) OWNER TO freqtrade_ai_attestor;
            REVOKE ALL ON FUNCTION write_okx_demo_attested_session(
                text,text,text,bigint,bigint,text,text
            ) FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION write_okx_demo_attested_session(
                text,text,text,bigint,bigint,text,text
            ) TO freqtrade
            """,
            "attestation function definition mismatch: "
            "write_okx_demo_attested_session",
        ),
    ],
)
def test_attestation_verifier_detects_role_acl_and_body_tampering(
    postgres_engine,
    tamper_sql,
    expected_problem,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(text(tamper_sql))
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        expected_problem in problem
        for problem in readiness.problems
    )


def test_attestation_hardening_removes_runtime_membership(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "GRANT freqtrade_ai_attestor TO freqtrade "
                "WITH ADMIN TRUE, INHERIT FALSE, SET TRUE"
            )
        )
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        "runtime role membership reaches attestor owner role" in problem
        for problem in readiness.problems
    )
    harden_attestation_access_boundary(postgres_engine)
    assert verify_schema(postgres_engine).ready is True


def test_attestation_hardening_converges_current_schema_proof_key(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE okx_demo_attestation_secrets "
                "SET hmac_key = :mismatched_key "
                "WHERE secret_id = 'ACTIVE'"
            ),
            {"mismatched_key": b"x" * 32},
        )
    harden_attestation_access_boundary(postgres_engine)
    with postgres_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT hmac_key = :expected_key "
                "FROM okx_demo_attestation_secrets "
                "WHERE secret_id = 'ACTIVE'"
            ),
            {"expected_key": b"t" * 32},
        ).scalar_one() is True
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text("SELECT hmac_key FROM okx_demo_attestation_secrets")
            )


def test_attestation_hardening_refuses_key_rotation_with_active_session(
    postgres_engine,
    monkeypatch,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    raw_request = _request(lineage, now)
    envelope = raw_request["snapshots"]["instrument"]
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="d" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    normalized = _normalize_attested_snapshot(
        capability,
        kind="instrument",
        content=envelope["content"],
        observed_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        _write_attested_snapshot(db, capability, normalized, now=now)
    monkeypatch.setenv(
        "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY",
        "78" * 32,
    )
    with pytest.raises(
        SchemaMigrationBlocked,
        match="blocked by an active session",
    ):
        harden_attestation_access_boundary(postgres_engine)


@pytest.mark.parametrize(
    "privilege",
    [
        "SUPERUSER",
        "CREATEROLE",
        "CREATEDB",
        "REPLICATION",
        "BYPASSRLS",
    ],
)
def test_attestation_hardening_removes_attestor_role_privileges(
    postgres_engine,
    privilege,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text("ALTER ROLE freqtrade_ai_attestor {}".format(privilege))
        )
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        "attestor role boundary mismatch" in problem
        for problem in readiness.problems
    )
    harden_attestation_access_boundary(postgres_engine)
    assert verify_schema(postgres_engine).ready is True


@pytest.mark.parametrize(
    "table_name",
    [
        "okx_demo_attested_sessions",
        "okx_demo_attestation_secrets",
        "okx_demo_trusted_snapshots",
    ],
)
def test_reharden_removes_set_only_role_table_delete_and_truncate(
    postgres_engine,
    table_name,
) -> None:
    upgrade_database(postgres_engine)
    intermediary = "freqtrade_ai_acl_{}".format(uuid4().hex[:12])
    try:
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    'CREATE ROLE "{}" NOLOGIN NOINHERIT; '
                    'GRANT "{}" TO freqtrade '
                    'WITH INHERIT FALSE, SET TRUE; '
                    'GRANT DELETE, TRUNCATE ON {} TO "{}"'.format(
                        intermediary,
                        intermediary,
                        table_name,
                        intermediary,
                    )
                )
            )
        readiness = verify_schema(postgres_engine)
        assert readiness.ready is False
        assert any(
            "runtime reachable table privileges are not revoked" in problem
            for problem in readiness.problems
        )
        harden_attestation_access_boundary(postgres_engine)
        assert verify_schema(postgres_engine).ready is True
        with pytest.raises(DBAPIError):
            with postgres_engine.begin() as connection:
                connection.execute(
                    text('SET LOCAL ROLE "{}"'.format(intermediary))
                )
                connection.execute(text("TRUNCATE TABLE {}".format(table_name)))
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    'REVOKE "{}" FROM freqtrade; DROP ROLE "{}"'.format(
                        intermediary, intermediary
                    )
                )
            )


def test_attestation_verifier_detects_indirect_membership_and_privileged_parent(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    intermediary = "freqtrade_ai_membership_{}".format(uuid4().hex[:12])
    privileged = "freqtrade_ai_privileged_{}".format(uuid4().hex[:12])
    try:
        with postgres_engine.begin() as connection:
            connection.execute(
                text('CREATE ROLE "{}" NOLOGIN'.format(intermediary))
            )
            connection.execute(
                text('CREATE ROLE "{}" NOLOGIN CREATEDB'.format(privileged))
            )
            connection.execute(
                text(
                    'GRANT freqtrade_ai_attestor TO "{}"; '
                    'GRANT "{}" TO freqtrade; '
                    'GRANT "{}" TO freqtrade_ai_attestor'.format(
                        intermediary,
                        intermediary,
                        privileged,
                    )
                )
            )
        readiness = verify_schema(postgres_engine)
        assert readiness.ready is False
        assert any(
            "runtime role membership reaches attestor owner role" in problem
            for problem in readiness.problems
        )
        assert any(
            "attestor role membership reaches a privileged role" in problem
            for problem in readiness.problems
        )
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    'REVOKE freqtrade_ai_attestor FROM "{}"; '
                    'REVOKE "{}" FROM freqtrade; '
                    'REVOKE "{}" FROM freqtrade_ai_attestor; '
                    'DROP ROLE "{}"; DROP ROLE "{}"'.format(
                        intermediary,
                        intermediary,
                        privileged,
                        intermediary,
                        privileged,
                    )
                )
            )


def test_attestation_hardening_removes_column_privilege_bypasses(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "GRANT SELECT (hmac_key) ON okx_demo_attestation_secrets "
                "TO freqtrade; "
                "GRANT UPDATE (revoked_at) ON okx_demo_attested_sessions "
                "TO freqtrade"
            )
        )
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        "runtime can read attestation secret column" in problem
        for problem in readiness.problems
    )
    assert any(
        "runtime column DML is not revoked" in problem
        for problem in readiness.problems
    )
    harden_attestation_access_boundary(postgres_engine)
    assert verify_schema(postgres_engine).ready is True
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text("SELECT hmac_key FROM okx_demo_attestation_secrets")
            )


@pytest.mark.parametrize(
    "constraint_name",
    [
        "approved_executions_no_submission_check",
        "approved_executions_claim_required_check",
        "approved_executions_approved_state_check",
        "risk_decisions_decision_check",
        "trade_intents_status_check",
    ],
)
def test_schema_verifier_detects_critical_check_tampering(
    postgres_engine, constraint_name
) -> None:
    upgrade_database(postgres_engine)
    table_name = (
        "approved_executions"
        if constraint_name.startswith("approved_executions")
        else "risk_decisions"
        if constraint_name.startswith("risk_decisions")
        else "trade_intents"
    )
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                'ALTER TABLE "{}" DROP CONSTRAINT "{}", '
                'ADD CONSTRAINT "{}" CHECK (TRUE)'.format(
                    table_name,
                    constraint_name,
                    constraint_name,
                )
            )
        )
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        "check definition mismatch: {}.{}".format(table_name, constraint_name)
        in problem
        for problem in readiness.problems
    )


def test_schema_verifier_detects_composite_lineage_fk_removal(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE approved_executions DROP CONSTRAINT "
                "approved_executions_decision_intent_fkey"
            )
        )
    readiness = verify_schema(postgres_engine)
    assert readiness.ready is False
    assert any(
        "missing foreign key: approved_executions" in problem
        for problem in readiness.problems
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE approved_executions SET order_submission_authorized = TRUE",
        "UPDATE approved_executions SET claim_required = FALSE",
        "UPDATE approved_executions SET reserved_notional = 0",
        "UPDATE approved_executions SET intent_id = 'bad'",
        "UPDATE trade_intents SET side = 'hold'",
        "UPDATE trade_intents SET quantity = quantity + 1",
        "UPDATE trade_intents SET leverage = leverage + 1",
        "UPDATE trade_intents SET stop_loss = stop_loss - 1",
        "UPDATE trade_intents SET take_profit = take_profit + 1",
        "UPDATE trade_intents SET instrument_id = 'ETH-USDT-SWAP'",
        "UPDATE trade_intents SET canonical_hash = repeat('0', 64)",
        "UPDATE trade_intents SET policy_digest = repeat('0', 64)",
        "UPDATE trade_intents SET policy_digest = NULL, side = 'hold'",
        "UPDATE trade_intents SET status = 'FORGED'",
        "UPDATE risk_decisions SET decision = 'FORGED'",
    ],
)
def test_database_rejects_direct_authorization_tampering(
    postgres_engine, statement
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    with factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="tamper-target",
            request=_request(lineage, now, factory),
            policy=_policy(),
            now=now,
        )
    assert result.status == "APPROVED"
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text(statement))
    with factory() as db:
        assert db.get(ApprovedExecution, result.approved_execution_id).status == "ACTIVE"
        assert db.get(TradeIntent, result.trade_intent_id).status == "APPROVED"


def test_postgresql_accepts_local_market_observation_with_ahead_exchange_events(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    request = _request(lineage, now)
    market = request["snapshots"]["market"]["content"]
    exchange_time = now + timedelta(seconds=1)
    market["bbo"] = {
        "bid_price": "49999.9",
        "bid_size": "1",
        "ask_price": "50000.1",
        "ask_size": "1",
        "timestamp": exchange_time.isoformat(),
    }
    market["mark"] = {
        "price": "50000",
        "timestamp": exchange_time.isoformat(),
    }

    snapshots = request.pop("snapshots")
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="e" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    snapshot_ids = {}
    with factory.begin() as db:
        for kind, envelope in snapshots.items():
            content = envelope["content"]
            expires_at = datetime.fromisoformat(content["expires_at"])
            normalized = _normalize_attested_snapshot(
                capability,
                kind=kind,
                content=content,
                observed_at=now,
                expires_at=expires_at,
            )
            row = _write_attested_snapshot(
                db,
                capability,
                normalized,
                now=now,
            )
            snapshot_ids[kind] = row.snapshot_id
    request["snapshot_ids"] = snapshot_ids

    with factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="local-market-observation-ahead-exchange-events",
            request=request,
            policy=_policy(),
            now=now,
        )

    assert result.status == "APPROVED"
    assert result.approved_execution_id is not None


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE okx_demo_trusted_snapshots SET digest = repeat('0', 64)",
        "DELETE FROM okx_demo_trusted_snapshots",
    ],
)
def test_trusted_snapshot_registry_is_database_immutable(
    postgres_engine, statement
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    request = _request(
        _seed(factory),
        datetime.now(timezone.utc),
        factory,
    )
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text(statement))
    with factory() as db:
        assert len(db.scalars(select(OkxDemoTrustedSnapshot)).all()) == 3
        assert set(request["snapshot_ids"].values()) == {
            row.snapshot_id
            for row in db.scalars(select(OkxDemoTrustedSnapshot)).all()
        }


@pytest.mark.parametrize(
    "statement",
    [
        (
            "INSERT INTO okx_demo_attested_sessions (session_id) "
            "VALUES ('forged-session')"
        ),
        (
            "INSERT INTO okx_demo_trusted_snapshots (snapshot_id) "
            "VALUES ('forged-snapshot')"
        ),
    ],
)
def test_runtime_role_cannot_directly_insert_attestation_rows(
    postgres_engine, statement
) -> None:
    upgrade_database(postgres_engine)
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(text(statement))


def test_runtime_role_can_only_write_with_private_capability_functions(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    raw_request = _request(lineage, now)
    envelope = raw_request["snapshots"]["instrument"]
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="d" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    normalized = _normalize_attested_snapshot(
        capability,
        kind="instrument",
        content=envelope["content"],
        observed_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        row = _write_attested_snapshot(
            db, capability, normalized, now=now
        )
        database_id = row.database_id
        snapshot_id = row.snapshot_id
        session_id = row.attested_session_id
    with factory() as db:
        assert db.get(OkxDemoTrustedSnapshot, database_id).snapshot_id == snapshot_id
        assert db.get(OkxDemoAttestedSession, session_id).revoked_at is None


def test_runtime_role_cannot_self_mint_session_with_forged_hmac(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    now = datetime.now(timezone.utc)
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"x" * 32,
        pinned_fingerprint_sha256="d" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    content = {
        "execution_target": "OKX_DEMO",
        "source": "okx_demo_rest",
        "resource": "instrument",
        "stale": False,
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
    }
    normalized = _normalize_attested_snapshot(
        capability,
        kind="instrument",
        content=content,
        observed_at=now,
        expires_at=now + timedelta(seconds=30),
    )
    factory = create_session_factory(postgres_engine)
    with pytest.raises(DBAPIError):
        with factory.begin() as db:
            db.execute(text("SET LOCAL ROLE freqtrade"))
            _write_attested_snapshot(db, capability, normalized, now=now)


def test_runtime_role_durable_revoke_blocks_old_session(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    raw_request = _request(lineage, now)
    envelope = raw_request["snapshots"]["instrument"]
    envelope["content"]["expires_at"] = (
        now + timedelta(seconds=30)
    ).isoformat()
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="d" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    normalized = _normalize_attested_snapshot(
        capability,
        kind="instrument",
        content=envelope["content"],
        observed_at=now,
        expires_at=now + timedelta(seconds=30),
    )
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        row = _write_attested_snapshot(db, capability, normalized, now=now)
        session_id = row.attested_session_id
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        _revoke_attested_session(
            db,
            capability,
            reason="IDENTITY_DRIFT",
            revoked_at=now + timedelta(seconds=1),
        )
    with factory() as db:
        session = db.get(OkxDemoAttestedSession, session_id)
        assert session is not None
        assert session.revoke_reason == "IDENTITY_DRIFT"
        assert session.revoked_at is not None


def test_runtime_role_persists_unused_session_before_durable_revoke(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    now = datetime.now(timezone.utc)
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="d" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        persisted = _persist_attested_session(
            db,
            capability,
            now=now,
        )
        session_id = persisted.session_id
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        persisted = db.get(OkxDemoAttestedSession, session_id)
        assert persisted is not None
        assert persisted.revoked_at is None
        _revoke_attested_session(
            db,
            capability,
            reason="FACTORY_CLOSE",
            revoked_at=now + timedelta(seconds=1),
        )
    with factory() as db:
        persisted = db.get(OkxDemoAttestedSession, session_id)
        assert persisted is not None
        assert persisted.revoke_reason == "FACTORY_CLOSE"
        assert persisted.revoked_at is not None


def test_bound_attested_session_renews_across_multiple_ttl_windows(
    postgres_engine,
    monkeypatch,
) -> None:
    upgrade_database(postgres_engine)
    now = datetime.now(timezone.utc)
    current = [now]
    expected_fingerprint = "d" * 64
    monkeypatch.setattr(read_boundary, "_utc_now", lambda: current[0])
    monkeypatch.setattr(read_boundary, "run_preflight", lambda environment: None)
    monkeypatch.setattr(
        read_boundary,
        "require_pinned_account_fingerprint",
        lambda environment: expected_fingerprint,
    )
    monkeypatch.setattr(
        read_boundary,
        "_build_demo_authorization_headers",
        lambda *args, **kwargs: {
            "OK-ACCESS-KEY": "temporary-key",
            "OK-ACCESS-SIGN": "signature",
            "OK-ACCESS-TIMESTAMP": "2026-07-27T00:00:00.000Z",
            "OK-ACCESS-PASSPHRASE": "temporary-passphrase",
        },
    )
    client = read_boundary.create_attested_okx_demo_read_adapter(
        {
            "FREQTRADE_AI_EXECUTION_TARGET": "OKX_DEMO",
            "FREQTRADE_AI_ALLOW_REAL_FUNDS": "false",
            "FREQTRADE_AI_OKX_DEMO_REST_URL": "https://openapi.okx.com",
            "OKX_DEMO_API_KEY": "temporary-key",
            "OKX_DEMO_API_SECRET": "temporary-secret",
            "OKX_DEMO_API_PASSPHRASE": "temporary-passphrase",
            "OKX_DEMO_ACCOUNT_FINGERPRINT": expected_fingerprint,
            "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY": "74" * 32,
        }
    )
    handle = _create_attested_writer_credential_bridge(client)
    with postgres_engine.connect() as connection:
        db = Session(bind=connection)
        handle.bind_database(db)
        db.close()
    for elapsed_seconds in (55, 110):
        current[0] = now + timedelta(seconds=elapsed_seconds)
        headers = handle.authorization_headers(
            method="GET",
            request_path="/api/v5/account/config",
            body="",
        )
        assert headers["OK-ACCESS-KEY"] == "temporary-key"
    with Session(postgres_engine) as db:
        sessions = db.scalars(
            select(OkxDemoAttestedSession).order_by(
                OkxDemoAttestedSession.created_at
            )
        ).all()
        assert len(sessions) == 3
        assert [
            session.revoke_reason for session in sessions
        ] == ["EXPIRED", "EXPIRED", None]
        assert sum(session.revoked_at is None for session in sessions) == 1
    current[0] = now + timedelta(seconds=111)
    client.close()
    with Session(postgres_engine) as db:
        sessions = db.scalars(
            select(OkxDemoAttestedSession)
        ).all()
        assert all(session.revoked_at is not None for session in sessions)
        assert sum(
            session.revoke_reason == "FACTORY_CLOSE"
            for session in sessions
        ) == 1


def test_initial_attested_session_bind_renews_near_expiry(
    postgres_engine,
    monkeypatch,
) -> None:
    upgrade_database(postgres_engine)
    now = datetime.now(timezone.utc)
    current = [now]
    expected_fingerprint = "e" * 64
    monkeypatch.setattr(read_boundary, "_utc_now", lambda: current[0])
    monkeypatch.setattr(read_boundary, "run_preflight", lambda environment: None)
    monkeypatch.setattr(
        read_boundary,
        "require_pinned_account_fingerprint",
        lambda environment: expected_fingerprint,
    )
    monkeypatch.setattr(
        read_boundary,
        "_build_demo_authorization_headers",
        lambda *args, **kwargs: {
            "OK-ACCESS-KEY": "temporary-key",
            "OK-ACCESS-SIGN": "signature",
            "OK-ACCESS-TIMESTAMP": "2026-07-27T00:00:00.000Z",
            "OK-ACCESS-PASSPHRASE": "temporary-passphrase",
        },
    )
    client = read_boundary.create_attested_okx_demo_read_adapter(
        {
            "FREQTRADE_AI_EXECUTION_TARGET": "OKX_DEMO",
            "FREQTRADE_AI_ALLOW_REAL_FUNDS": "false",
            "FREQTRADE_AI_OKX_DEMO_REST_URL": "https://openapi.okx.com",
            "OKX_DEMO_API_KEY": "temporary-key",
            "OKX_DEMO_API_SECRET": "temporary-secret",
            "OKX_DEMO_API_PASSPHRASE": "temporary-passphrase",
            "OKX_DEMO_ACCOUNT_FINGERPRINT": expected_fingerprint,
            "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY": "74" * 32,
        }
    )
    handle = _create_attested_writer_credential_bridge(client)

    # Reconciliation may finish inside the renewal lead window. The initial
    # writer bind must rotate first instead of persisting a stale capability.
    current[0] = now + timedelta(seconds=55)
    with postgres_engine.connect() as connection:
        db = Session(bind=connection)
        handle.bind_database(db)
        db.close()

    with Session(postgres_engine) as db:
        sessions = db.scalars(select(OkxDemoAttestedSession)).all()
        assert len(sessions) == 1
        assert sessions[0].created_at == current[0]
        assert sessions[0].expires_at == current[0] + timedelta(seconds=60)
        assert sessions[0].revoked_at is None

    current[0] = now + timedelta(seconds=56)
    client.close()


def test_approved_execution_snapshot_foreign_keys_restrict_registry_delete(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    request = _request(lineage, now, factory)
    with factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="snapshot-fk-restrict",
            request=request,
            policy=_policy(),
            now=now,
        )
    with factory() as db:
        approved = db.get(ApprovedExecution, result.approved_execution_id)
        assert approved is not None
        snapshot_id = approved.instrument_snapshot_id
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "DROP TRIGGER okx_demo_trusted_snapshots_immutable "
                    "ON okx_demo_trusted_snapshots"
                )
            )
            connection.execute(
                text(
                    "DELETE FROM okx_demo_trusted_snapshots "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {"snapshot_id": snapshot_id},
            )


@pytest.mark.parametrize(
    "revoke_mode",
    ["IDENTITY_DRIFT", "FACTORY_CLOSE"],
)
def test_real_factory_revoke_blocks_idempotent_retry_and_releases_budget(
    postgres_engine,
    monkeypatch,
    revoke_mode,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    current = [now]
    expected_fingerprint = "d" * 64
    monkeypatch.setattr(read_boundary, "_utc_now", lambda: current[0])
    monkeypatch.setattr(read_boundary, "run_preflight", lambda environment: None)
    monkeypatch.setattr(
        read_boundary,
        "require_pinned_account_fingerprint",
        lambda environment: expected_fingerprint,
    )
    monkeypatch.setattr(
        read_boundary,
        "_build_demo_authorization_headers",
        lambda *args, **kwargs: {
            "OK-ACCESS-KEY": "temporary-key",
            "OK-ACCESS-SIGN": "signature",
            "OK-ACCESS-TIMESTAMP": "2026-07-27T00:00:00.000Z",
            "OK-ACCESS-PASSPHRASE": "temporary-passphrase",
        },
    )

    class DriftTransport:
        def get(self, **kwargs):
            return OkxReadHttpResponse(
                status_code=200,
                payload={
                    "code": "0",
                    "msg": "",
                    "data": [
                        {
                            "uid": "drifted",
                            "mainUid": "drifted",
                            "acctLv": "2",
                            "posMode": "net_mode",
                            "perm": "read_only,trade",
                        }
                    ],
                },
                received_at=current[0],
            )

    monkeypatch.setattr(
        read_boundary,
        "UrllibOkxReadTransport",
        lambda: DriftTransport(),
    )
    client = read_boundary.create_attested_okx_demo_read_adapter(
        {
            "FREQTRADE_AI_EXECUTION_TARGET": "OKX_DEMO",
            "FREQTRADE_AI_ALLOW_REAL_FUNDS": "false",
            "FREQTRADE_AI_OKX_DEMO_REST_URL": "https://openapi.okx.com",
            "OKX_DEMO_API_KEY": "temporary-key",
            "OKX_DEMO_API_SECRET": "temporary-secret",
            "OKX_DEMO_API_PASSPHRASE": "temporary-passphrase",
            "OKX_DEMO_ACCOUNT_FINGERPRINT": expected_fingerprint,
            "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY": "74" * 32,
        }
    )
    request = _request(lineage, now)
    envelopes = request.pop("snapshots")
    snapshot_ids = {}
    with factory.begin() as db:
        db.execute(text("SET LOCAL ROLE freqtrade"))
        for kind, envelope in envelopes.items():
            envelope["content"]["expires_at"] = (
                now + timedelta(seconds=30)
            ).isoformat()
            row = client._persist_risk_snapshot(
                db,
                kind=kind,
                content=envelope["content"],
                observed_at=now,
                snapshot_expires_at=now + timedelta(seconds=30),
            )
            snapshot_ids[kind] = row.snapshot_id
            session_id = row.attested_session_id
    request["snapshot_ids"] = snapshot_ids
    with factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="factory-revoke-{}".format(revoke_mode.lower()),
            request=request,
            policy=_policy(),
            now=now,
        )
    assert result.status == "APPROVED"
    with factory() as db:
        approved = db.get(ApprovedExecution, result.approved_execution_id)
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert approved is not None and budget is not None
        reserved_before = budget.reserved_notional
        permission_reservation = approved.reserved_notional
    current[0] = now + timedelta(seconds=1)
    if revoke_mode == "IDENTITY_DRIFT":
        with pytest.raises(OkxReadAdapterError) as blocked:
            client.account_config()
        assert blocked.value.kind == "IDENTITY_DRIFT"
    else:
        client.close()
    with factory() as db:
        session = db.get(OkxDemoAttestedSession, session_id)
        assert session is not None
        assert session.revoke_reason == revoke_mode
        assert session.revoked_at is not None
    with factory() as db:
        retry = RiskChainService(db).evaluate(
            idempotency_key="factory-revoke-{}".format(revoke_mode.lower()),
            request=request,
            policy=_policy(),
            now=now + timedelta(seconds=2),
        )
    assert retry.status == "BLOCKED"
    assert retry.approved_execution_id is None
    with factory() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget is not None
        assert budget.reserved_notional == reserved_before - permission_reservation


@pytest.mark.parametrize(
    ("session_state", "expected_status"),
    [("REVOKED", "BLOCKED"), ("EXPIRED", "EXPIRED")],
)
def test_claim_active_approval_directly_invalidates_stale_session_atomically(
    postgres_engine,
    session_state,
    expected_status,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    capabilities = []
    request = _request(
        lineage,
        now,
        factory,
        capability_sink=capabilities,
    )
    with factory() as db:
        result = RiskChainService(db).evaluate(
            idempotency_key="pg-direct-claim-{}".format(session_state.lower()),
            request=request,
            policy=_policy(),
            now=now,
        )
    with factory() as db:
        approved = db.get(ApprovedExecution, result.approved_execution_id)
        assert approved is not None
        snapshot = db.scalars(
            select(OkxDemoTrustedSnapshot).where(
                OkxDemoTrustedSnapshot.snapshot_id
                == approved.instrument_snapshot_id
            )
        ).one()
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget is not None
        session_id = snapshot.attested_session_id
        intent_id = approved.trade_intent_id
        decision_id = approved.risk_decision_id
        reserved_before = budget.reserved_notional
        positions_before = budget.approved_positions
        permission_reservation = approved.reserved_notional
    if session_state == "REVOKED":
        with factory.begin() as db:
            db.execute(text("SET LOCAL ROLE freqtrade"))
            _revoke_attested_session(
                db,
                capabilities[0],
                reason="IDENTITY_DRIFT",
                revoked_at=now + timedelta(seconds=1),
            )
        claim_now = now + timedelta(seconds=2)
    else:
        claim_now = now + timedelta(minutes=11)
    with factory() as db:
        assert (
            RiskChainService(db).claim_active_approval(
                result.approved_execution_id,
                now=claim_now,
            )
            is None
        )
    with factory() as db:
        intent = db.get(TradeIntent, intent_id)
        decision = db.get(RiskDecision, decision_id)
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert db.get(ApprovedExecution, result.approved_execution_id) is None
        assert intent is not None and intent.status == expected_status
        assert decision is not None and decision.decision == expected_status
        assert budget is not None
        assert budget.reserved_notional == reserved_before - permission_reservation
        assert budget.approved_positions == positions_before - 1


def test_security_definer_rejects_wrong_pinned_account(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    raw_request = _request(lineage, now)
    instrument = raw_request["snapshots"]["instrument"]
    capability = _issue_attested_session_capability(
        attestation_hmac_key=b"t" * 32,
        pinned_fingerprint_sha256="c" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    normalized = _normalize_attested_snapshot(
        capability,
        kind="instrument",
        content=instrument["content"],
        observed_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    with factory.begin() as db:
        _write_attested_snapshot(db, capability, normalized, now=now)
    forged_account = {
        "execution_target": "OKX_DEMO",
        "source": "okx_demo_rest",
        "resource": "account",
        "stale": False,
        "authenticated": True,
        "pinned_account_fingerprint": "b" * 64,
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "SELECT write_okx_demo_trusted_snapshot("
                    "CAST(:session_id AS text), CAST(:proof AS text), "
                    "CAST(:snapshot_id AS text), 'account', "
                    "CAST(:content AS jsonb), CAST(:digest AS text), "
                    ":observed_at, :expires_at)"
                ),
                {
                    "session_id": capability._identity.session_id,
                    "proof": capability._proof,
                    "snapshot_id": "account:" + "0" * 48,
                    "content": json.dumps(forged_account),
                    "digest": "0" * 64,
                    "observed_at": now,
                    "expires_at": now + timedelta(minutes=5),
                },
            )


def test_revoked_or_expired_attested_session_blocks_authorization(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    now = datetime.now(timezone.utc)
    lineage = _seed(factory)
    revoked_request = _request(lineage, now, factory)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE okx_demo_attested_sessions "
                "SET revoked_at = :revoked_at, revoke_reason = 'IDENTITY_DRIFT'"
            ),
            {"revoked_at": now},
        )
    with factory() as db:
        revoked = RiskChainService(db).evaluate(
            idempotency_key="revoked-attestation",
            request=revoked_request,
            policy=_policy(),
            now=now,
        )
    assert revoked.status == "BLOCKED"

    expired_request = _request(lineage, now, factory)
    with factory() as db:
        expired = RiskChainService(db).evaluate(
            idempotency_key="expired-attestation-session",
            request=expired_request,
            policy=_policy(),
            now=now + timedelta(minutes=11),
        )
    assert expired.status == "BLOCKED"


def test_legacy_authorization_row_cannot_become_active_approval(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    with factory.begin() as db:
        ensure_execution_scope_catalog(db)
        intent = TradeIntent(
            execution_target_id=OKX_DEMO_TARGET_ID,
            authorization_schema_version="LEGACY",
            client_order_id="LEGACY1",
            status="UNKNOWN_LEGACY",
            request_snapshot={},
        )
        db.add(intent)
        db.flush()
        decision = RiskDecision(
            execution_target_id=OKX_DEMO_TARGET_ID,
            trade_intent_id=intent.id,
            authorization_schema_version="LEGACY",
            policy_digest=None,
            decision="BLOCKED",
            policy_version="legacy",
            evidence_snapshot={},
        )
        db.add(decision)
        db.flush()
        legacy_intent_id = intent.id
        legacy_decision_id = decision.id

    with pytest.raises(DBAPIError):
        with factory.begin() as db:
            db.add(
                ApprovedExecution(
                    execution_target_id=OKX_DEMO_TARGET_ID,
                    trade_intent_id=legacy_intent_id,
                    risk_decision_id=legacy_decision_id,
                    intent_id="0" * 64,
                    client_order_id="LEGACY1",
                    authorization_schema_version="RISK_V1",
                    canonical_hash="0" * 64,
                    policy_digest="0" * 64,
                    approved_payload_hash="0" * 64,
                    decision="APPROVED",
                    intent_status="APPROVED",
                    reserved_notional=1,
                    order_submission_authorized=False,
                    claim_required=True,
                    status="ACTIVE",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    evidence_snapshot={},
                )
            )


def test_owner_sweep_releases_only_unclaimed_expired_natural_approval(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    seeded = _seed_unclaimed_natural_approval(factory)

    with postgres_engine.begin() as connection:
        first = connection.execute(
            text("SELECT release_unclaimed_expired_natural_approvals()")
        ).scalar_one()
        second = connection.execute(
            text("SELECT release_unclaimed_expired_natural_approvals()")
        ).scalar_one()

    assert first == 1
    assert second == 0
    with factory() as db:
        approval = db.get(ApprovedExecution, seeded["approval_id"])
        chain = db.get(FullChainRun, seeded["chain_id"])
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert approval is not None and approval.status == "EXPIRED"
        assert chain is not None and chain.status == "BLOCKED"
        assert chain.current_stage == "EXECUTION"
        assert chain.terminal_reason == (
            "unclaimed natural approval expired without execution"
        )
        assert budget is not None
        assert budget.approved_positions == 0
        assert budget.reserved_notional == Decimal("0")

    # The recovered slot is usable by the unchanged strict risk policy.
    second_lineage = _seed(factory, suffix=uuid4().hex)
    now = datetime.now(timezone.utc)
    with factory() as db:
        recovered = RiskChainService(db).evaluate(
            idempotency_key="recovered-stale-natural-budget",
            request=_request(second_lineage, now, factory),
            policy=_policy(),
            now=now,
        )
    assert recovered.status == "APPROVED"


@pytest.mark.parametrize(
    "seed_options",
    [
        {"expired": False},
        {"with_exchange_order": True},
        {"with_execution_checkpoint": True},
    ],
    ids=["not-expired", "exchange-order", "execution-checkpoint"],
)
def test_owner_sweep_preserves_unproven_or_started_execution(
    postgres_engine,
    seed_options,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    seeded = _seed_unclaimed_natural_approval(factory, **seed_options)

    with postgres_engine.begin() as connection:
        released = connection.execute(
            text("SELECT release_unclaimed_expired_natural_approvals()")
        ).scalar_one()

    assert released == 0
    with factory() as db:
        approval = db.get(ApprovedExecution, seeded["approval_id"])
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert approval is not None and approval.status == "ACTIVE"
        assert budget is not None and budget.approved_positions == 1
        assert budget.reserved_notional == seeded["reserved_notional"]


def test_owner_sweep_is_concurrent_and_idempotent(postgres_engine) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    _seed_unclaimed_natural_approval(factory)
    barrier = Barrier(2)
    results: list[int] = []
    failures: list[Exception] = []
    mutex = Lock()

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            with postgres_engine.begin() as connection:
                released = connection.execute(
                    text("SELECT release_unclaimed_expired_natural_approvals()")
                ).scalar_one()
            with mutex:
                results.append(released)
        except Exception as exc:  # pragma: no cover - assertion reports details
            with mutex:
                failures.append(exc)

    threads = [Thread(target=worker), Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert sorted(results) == [0, 1]
    with factory() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget is not None
        assert budget.approved_positions == 0
        assert budget.reserved_notional == Decimal("0")


def test_owner_sweep_fails_closed_on_budget_mismatch(postgres_engine) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    seeded = _seed_unclaimed_natural_approval(factory)
    with factory.begin() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget is not None
        budget.reserved_notional = Decimal("0")
        budget.approved_positions = 0

    with pytest.raises(DBAPIError, match="stale natural approval budget is inconsistent"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text("SELECT release_unclaimed_expired_natural_approvals()")
            )

    with factory() as db:
        approval = db.get(ApprovedExecution, seeded["approval_id"])
        chain = db.get(FullChainRun, seeded["chain_id"])
        assert approval is not None and approval.status == "ACTIVE"
        assert chain is not None and chain.status == "EXECUTING"


def test_runtime_cannot_call_or_bypass_private_stale_approval_sweep(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    seeded = _seed_unclaimed_natural_approval(factory)

    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text("SELECT release_unclaimed_expired_natural_approvals()")
            )
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE freqtrade"))
            connection.execute(
                text(
                    "UPDATE approved_executions SET status='EXPIRED' "
                    "WHERE id=:approval_id"
                ),
                {"approval_id": seeded["approval_id"]},
            )

    with factory() as db:
        approval = db.get(ApprovedExecution, seeded["approval_id"])
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert approval is not None and approval.status == "ACTIVE"
        assert budget is not None and budget.approved_positions == 1


def test_natural_risk_boundary_invokes_private_stale_approval_sweep(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_functiondef("
                "'persist_okx_demo_natural_risk_chain(jsonb)'::regprocedure)"
            )
        ).scalar_one()
    assert "PERFORM" in definition
    assert "release_unclaimed_expired_natural_approvals()" in definition


def test_postgresql_budget_lock_allows_only_one_concurrent_permission(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    request = _request(lineage, now, factory)
    barrier = Barrier(2)
    results = []
    failures = []
    mutex = Lock()

    def worker(key: str) -> None:
        try:
            barrier.wait(timeout=5)
            with factory() as db:
                result = RiskChainService(db).evaluate(
                    idempotency_key=key,
                    request=request,
                    policy=_policy(),
                    now=now,
                )
            with mutex:
                results.append(result.status)
        except Exception as exc:  # pragma: no cover - assertion reports the exact type
            with mutex:
                failures.append(type(exc).__name__)

    threads = [
        Thread(target=worker, args=("concurrent-{}".format(index),))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert sorted(results) == ["APPROVED", "REJECTED"]
    with factory() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget.reserved_notional == 600
        assert budget.approved_positions == 1
        assert len(db.scalars(select(ApprovedExecution)).all()) == 1
        assert len(db.scalars(select(TradeIntent)).all()) == 2


def test_postgresql_concurrent_idempotent_retry_reads_one_chain(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = datetime.now(timezone.utc)
    request = _request(lineage, now, factory)
    barrier = Barrier(2)
    results = []
    failures = []
    mutex = Lock()

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            with factory() as db:
                result = RiskChainService(db).evaluate(
                    idempotency_key="same-concurrent-request",
                    request=request,
                    policy=_policy(),
                    now=now,
                )
            with mutex:
                results.append(result)
        except Exception as exc:  # pragma: no cover - assertion reports the exact type
            with mutex:
                failures.append(type(exc).__name__)

    threads = [Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert len(results) == 2
    assert results[0] == results[1]
    with factory() as db:
        budget = db.get(RiskBudget, OKX_DEMO_TARGET_ID)
        assert budget.reserved_notional == 600
        assert budget.approved_positions == 1
        assert len(db.scalars(select(ApprovedExecution)).all()) == 1
        assert len(db.scalars(select(TradeIntent)).all()) == 1


def test_postgresql_v44_owner_initializes_missing_natural_budget_once(
    postgres_engine,
) -> None:
    """Exercise the repository signal digest through the real owner function."""

    upgrade_database(postgres_engine)
    factory = create_session_factory(postgres_engine)
    lineage = _seed(factory)
    now = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(
        microsecond=482565
    )
    policy = {
        "allowed_instruments": [
            "BTC-USDT-SWAP",
            "ETH-USDT-SWAP",
            "SOL-USDT-SWAP",
        ],
        "allowed_sides": ["buy", "sell"],
        "allowed_order_types": ["limit"],
        "max_leverage": "2",
        "max_order_notional": "1000",
        "max_total_exposure": "3000",
        "max_positions": 3,
        "max_price_deviation_pct": "0.01",
        "min_strategy_score": "50",
        "scoring_version": "phase2-quality-v1",
    }
    policy_digest = canonical_digest(policy)
    selection_evidence = {
        "policy": {
            "execution_target_id": "OKX_DEMO",
            "allow_real_funds": False,
            "production_promotion_claim": False,
            "validated_backtest_required": True,
            "minimum_score": 50,
        }
    }
    lease_token = "a" * 64

    with factory.begin() as db:
        source_chain = db.get(FullChainRun, lineage["_full_chain_run_id"])
        source_approval = db.get(
            StrategyCandidateApproval, lineage["_candidate_approval_id"]
        )
        old_signal = db.get(
            FullChainSignalSnapshot, lineage["_signal_snapshot_id"]
        )
        source_chain.signal_snapshot_id = None
        for stage in db.scalars(
            select(FullChainStageRun).where(
                FullChainStageRun.full_chain_run_id == source_chain.id
            )
        ).all():
            db.delete(stage)
        db.delete(old_signal)
        source_chain.run_kind = "RESEARCH"
        source_chain.status = "APPROVED"
        source_chain.current_stage = "CANDIDATE_APPROVAL"
        source_approval.promotion_policy_version = "okx-demo-selection-v2"
        source_approval.promotion_evidence = selection_evidence
        source_approval.expires_at = now + timedelta(minutes=10)
        score = db.get(StrategyScore, lineage["strategy_score_id"])
        score.scoring_version = "phase2-quality-v1"
        score.total_score = 80
        result = db.get(BacktestResult, lineage["backtest_result_id"])
        result.max_drawdown_pct = Decimal("0.10")
        version = db.get(StrategyVersion, lineage["strategy_version_id"])
        version.blueprint = {
            "stoploss": -0.04,
            "minimal_roi": {"0": 0.08},
        }
        db.flush()

        deployment = StrategyDeployment(
            execution_target_id="OKX_DEMO",
            candidate_approval_id=source_approval.id,
            strategy_id=lineage["strategy_id"],
            strategy_version_id=lineage["strategy_version_id"],
            candidate_digest=source_approval.candidate_digest,
            promotion_policy_version="okx-demo-selection-v2",
            deployment_policy_digest="b" * 64,
            risk_policy_digest=policy_digest,
            instrument_id="BTC-USDT-SWAP",
            timeframe="15m",
            status="ACTIVE",
            active_slot=1,
            evidence_snapshot={
                "execution_target_id": "OKX_DEMO",
                "allow_real_funds": False,
            },
        )
        db.add(deployment)
        db.flush()
        evaluation = SignalEvaluation(
            deployment_id=deployment.id,
            execution_target_id="OKX_DEMO",
            instrument_id="BTC-USDT-SWAP",
            timeframe="15m",
            closed_candle_at=now - timedelta(minutes=15),
            status="LEASED",
            lease_owner="runtime-test",
            lease_token=lease_token,
            lease_expires_at=now + timedelta(minutes=5),
            heartbeat_at=now,
            fencing_sequence=1,
        )
        db.add(evaluation)
        db.flush()
        execution_chain = FullChainRun(
            research_job_id=source_chain.research_job_id,
            research_job_attempt_id=source_chain.research_job_attempt_id,
            run_kind="EXECUTION",
            signal_evaluation_id=evaluation.id,
            research_scope_id="LOCAL_DRY_RUN",
            execution_target_id="OKX_DEMO",
            status="EXECUTING",
            current_stage="RISK",
            strategy_id=lineage["strategy_id"],
            strategy_version_id=lineage["strategy_version_id"],
            backtest_run_id=lineage["backtest_run_id"],
            backtest_task_id=lineage["backtest_task_id"],
            backtest_result_id=lineage["backtest_result_id"],
            strategy_score_id=lineage["strategy_score_id"],
        )
        db.add(execution_chain)
        db.flush()
        execution_approval = StrategyCandidateApproval(
            full_chain_run_id=execution_chain.id,
            execution_target_id="OKX_DEMO",
            strategy_version_id=lineage["strategy_version_id"],
            backtest_result_id=lineage["backtest_result_id"],
            strategy_score_id=lineage["strategy_score_id"],
            candidate_digest=source_approval.candidate_digest,
            promotion_policy_version="okx-demo-selection-v2",
            promotion_evidence=selection_evidence,
            status="APPROVED",
            requested_by="system:test",
            decided_by="system:test",
            decision_reason="Natural signal v39 integration fixture.",
            requested_at=now,
            decided_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        db.add(execution_approval)
        db.flush()
        execution_chain.candidate_approval_id = execution_approval.id
        deployment_id = deployment.id
        evaluation_id = evaluation.id
        execution_chain_id = execution_chain.id
        execution_approval_id = execution_approval.id
        lineage.update(
            _full_chain_run_id=execution_chain_id,
            _candidate_approval_id=execution_approval_id,
            _signal_snapshot_id=1,
            _signal_digest="d" * 64,
        )

    request = _request(lineage, now, factory)
    request["quantity"] = "0.001"
    with factory.begin() as db:
        snapshot_rows = {
            kind: db.scalars(
                select(OkxDemoTrustedSnapshot).where(
                    OkxDemoTrustedSnapshot.snapshot_id == request["snapshot_ids"][kind]
                )
            ).one()
            for kind in ("instrument", "market", "account")
        }
        source_database_ids = {
            "instrument_snapshot": snapshot_rows["instrument"].database_id,
            "market_snapshot": snapshot_rows["market"].database_id,
            "account_snapshot": snapshot_rows["account"].database_id,
        }
        signal_snapshot = {
            "decision": "ACTIONABLE",
            "instrument_id": "BTC-USDT-SWAP",
            "strategy_version_id": lineage["strategy_version_id"],
            "candidate_digest": db.get(
                StrategyCandidateApproval, execution_approval_id
            ).candidate_digest,
            "market_snapshot_id": snapshot_rows["market"].snapshot_id,
            "market_digest": snapshot_rows["market"].digest,
            "enter_long": True,
            "enter_short": False,
        }
        signal_digest = full_chain_digest(
            {
                "candidate_digest": signal_snapshot["candidate_digest"],
                "instrument_id": "BTC-USDT-SWAP",
                "source_type": "api_aggregate",
                "source_database_ids": source_database_ids,
                "signal_snapshot": signal_snapshot,
                "observed_at": now,
                "expires_at": now + timedelta(minutes=5),
            }
        )
        signal = FullChainSignalSnapshot(
            full_chain_run_id=execution_chain_id,
            candidate_approval_id=execution_approval_id,
            execution_target_id="OKX_DEMO",
            instrument_id="BTC-USDT-SWAP",
            signal_digest=signal_digest,
            source_type="api_aggregate",
            core_data=True,
            source_database_ids=source_database_ids,
            signal_snapshot=signal_snapshot,
            observed_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        db.add(signal)
        db.flush()
        signal_id = signal.id
        chain = db.get(FullChainRun, execution_chain_id)
        chain.signal_snapshot_id = signal_id
        evaluation = db.get(SignalEvaluation, evaluation_id)
        evaluation.result_snapshot = {
            "checkpoint_schema": "SIGNAL_EVALUATION_V1",
            "evaluation_id": evaluation_id,
            "deployment_id": deployment_id,
            "closed_candle_at": evaluation.closed_candle_at.isoformat(),
            "bundle": {
                kind: {
                    "database_id": snapshot_rows[kind].database_id,
                    "snapshot_id": snapshot_rows[kind].snapshot_id,
                    "digest": snapshot_rows[kind].digest,
                }
                for kind in ("instrument", "market", "account")
            },
            "signal": signal_snapshot,
        }
    request.update(
        full_chain_run_id=execution_chain_id,
        candidate_approval_id=execution_approval_id,
        signal_snapshot_id=signal_id,
        signal_digest=signal_digest,
    )
    authorization_input = RiskChainService._authorization_input(request)
    authorization_digest = canonical_digest(authorization_input)
    key = "signal-evaluation-{}".format(evaluation_id)
    key_digest = hashlib.sha256(key.encode()).hexdigest()
    intent_id = canonical_digest(
        {
            "execution_target": "OKX_DEMO",
            "input_digest": authorization_digest,
            "policy_digest": policy_digest,
            "idempotency_digest": key_digest,
        }
    )
    with factory.begin() as db:
        db.add_all(
            [
                FullChainStageRun(
                    full_chain_run_id=execution_chain_id,
                    stage="SIGNAL",
                    status="SUCCESS",
                    idempotency_key_digest="1" * 64,
                    input_digest="2" * 64,
                    input_snapshot={"evaluation_id": evaluation_id},
                    output_snapshot={"status": "ACTIONABLE"},
                    database_ids={"signal_snapshot_id": signal_id},
                    prepared_at=now,
                    completed_at=now,
                ),
                FullChainStageRun(
                    full_chain_run_id=execution_chain_id,
                    stage="RISK",
                    status="PREPARED",
                    idempotency_key_digest=hashlib.sha256(
                        "risk-evaluation:{}:{}".format(
                            evaluation_id, signal_digest
                        ).encode()
                    ).hexdigest(),
                    input_digest="3" * 64,
                    input_snapshot={
                        "evaluation_id": evaluation_id,
                        "signal_snapshot_id": signal_id,
                        "signal_digest": signal_digest,
                        "risk_input_digest": canonical_digest(request),
                        "risk_canonical_hash": authorization_digest,
                        "risk_idempotency_digest": key_digest,
                        "risk_intent_id": intent_id,
                        "risk_client_order_id": "FAI" + intent_id[:29],
                        "policy_digest": policy_digest,
                    },
                    output_snapshot={},
                    database_ids={},
                    prepared_at=now,
                ),
            ]
        )
        batch = OkxDemoRecoveryBatch(
            execution_target_id="OKX_DEMO",
            recovery_batch_id="e" * 64,
            authenticated=True,
            pagination_complete=True,
            complete_streams=["ACCOUNT", "FILL", "ORDER", "POSITION"],
            high_watermarks={name: 0 for name in ("ACCOUNT", "FILL", "ORDER", "POSITION")},
            overlap_started_at=now - timedelta(seconds=1),
            observed_at=now,
            completed_at=now,
            event_count=0,
            evidence_digest="f" * 64,
        )
        db.add(batch)
        db.flush()
        reconciliation = ReconciliationRun(
            execution_target_id="OKX_DEMO",
            status="RECOVERED",
            summary_snapshot={},
            database_ids={},
            artifact_status="READY",
            authoritative_observed_at=now,
            source_type="api_aggregate",
            core_data=True,
            started_at=now,
            completed_at=now,
        )
        db.add(reconciliation)
        db.flush()
        reconciliation.database_ids = {
            "reconciliation_run": [reconciliation.id],
            "recovery_batches": [batch.database_id],
        }
        state = db.scalars(
            select(OkxDemoReconciliationState).where(
                OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
            )
        ).one_or_none()
        if state is None:
            state = OkxDemoReconciliationState(
                execution_target_id="OKX_DEMO",
            )
            db.add(state)
        state.status = "RECOVERED"
        state.opening_frozen = False
        state.block_reason = None
        state.last_event_observed_at = now
        state.last_reconciliation_run_id = reconciliation.id
        db.add(
            OkxOrderWriterLease(
                execution_target_id="OKX_DEMO",
                holder_token_digest="9" * 64,
                generation=1,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        )
        deployment_digest = db.execute(
            text(
                "SELECT encode(public.digest(convert_to(string_agg("
                "id::text||':'||active_slot::text||':'||candidate_approval_id::text||':'||"
                "candidate_digest||':'||deployment_policy_digest||':'||"
                "COALESCE(risk_policy_digest,''),'|' ORDER BY id),'UTF8'),'sha256'),'hex') "
                "FROM strategy_deployments WHERE status='ACTIVE'"
            )
        ).scalar_one()
        db.execute(
            text(
                "INSERT INTO okx_demo_automation_guard_states("
                "execution_target_id,authorization_mode,operational_state,policy_digest,"
                "deployment_set_digest,critical_failure_count,health_check_required,"
                "last_healthy_reconciliation_run_id,fencing_version) VALUES("
                "'OKX_DEMO','CONTINUOUS_DEMO_V1','RUNNING',:policy,:deployments,0,false,:run,1)"
            ),
            {
                "policy": policy_digest,
                "deployments": deployment_digest,
                "run": reconciliation.id,
            },
        )

    with postgres_engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM risk_budgets")).scalar_one() == 0
        before = connection.execute(text(
            "SELECT (SELECT count(*) FROM trade_intents),"
            "(SELECT count(*) FROM risk_decisions),"
            "(SELECT count(*) FROM approved_executions)"
        )).one()
        connection.execute(text("RESET ROLE"))
        _add_natural_signal_risk_chain_boundary(
            connection,
            refresh_supporting_boundaries=False,
            rebind_automation_guard=False,
            initialize_missing_budget=False,
        )
    with factory() as db:
        db.execute(text("SET ROLE freqtrade"))
        db.commit()
        with pytest.raises(DBAPIError, match="natural signal risk budget is missing"):
            RiskChainService(db).evaluate(
                idempotency_key=key,
                request=request,
                policy=policy,
                natural_signal_context={
                    "deployment_id": deployment_id,
                    "lease_token": lease_token,
                    "fencing_sequence": 1,
                },
                now=now,
            )
        db.rollback()
    with postgres_engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM risk_budgets")).scalar_one() == 0
        after = connection.execute(text(
            "SELECT (SELECT count(*) FROM trade_intents),"
            "(SELECT count(*) FROM risk_decisions),"
            "(SELECT count(*) FROM approved_executions)"
        )).one()
        assert after == before
        connection.execute(text("RESET ROLE"))
        _add_natural_signal_risk_chain_boundary(
            connection,
            refresh_supporting_boundaries=False,
            rebind_automation_guard=False,
        )

    with factory() as db:
        db.execute(text("SET ROLE freqtrade"))
        db.commit()
        result = RiskChainService(db).evaluate(
            idempotency_key=key,
            request=request,
            policy=policy,
            natural_signal_context={
                "deployment_id": deployment_id,
                "lease_token": lease_token,
                "fencing_sequence": 1,
            },
            now=now,
        )
        db.execute(text("RESET ROLE"))
        db.commit()
    assert result.status == "APPROVED"
    assert result.approved_execution_id is not None
    with factory() as db:
        budget = db.get(RiskBudget, "OKX_DEMO")
        assert budget is not None
        assert budget.reserved_notional > 0
        assert budget.approved_positions == 1
        assert db.execute(text("SELECT count(*) FROM risk_budgets")).scalar_one() == 1

    with factory() as db:
        db.execute(text("SET ROLE freqtrade"))
        db.commit()
        replay = RiskChainService(db).evaluate(
            idempotency_key=key,
            request=request,
            policy=policy,
            natural_signal_context={
                "deployment_id": deployment_id,
                "lease_token": lease_token,
                "fencing_sequence": 1,
            },
            now=now,
        )
        db.execute(text("RESET ROLE"))
        db.commit()
    assert replay == result
    with factory() as db:
        assert db.execute(text("SELECT count(*) FROM risk_budgets")).scalar_one() == 1

    with factory.begin() as db:
        chain = db.get(FullChainRun, execution_chain_id)
        checkpoint = db.scalars(
            select(FullChainStageRun).where(
                FullChainStageRun.full_chain_run_id == chain.id,
                FullChainStageRun.stage == "RISK",
            )
        ).one()
        chain.trade_intent_id = result.trade_intent_id
        chain.risk_decision_id = result.risk_decision_id
        chain.approved_execution_id = result.approved_execution_id
        chain.current_stage = "EXECUTION"
        checkpoint.status = "SUCCESS"
        checkpoint.database_ids = {
            "trade_intent_id": result.trade_intent_id,
            "risk_decision_id": result.risk_decision_id,
            "approved_execution_id": result.approved_execution_id,
        }
        checkpoint.completed_at = now

    with factory() as db:
        db.execute(text("SET ROLE freqtrade"))
        db.commit()
        claimed = SqlAlchemyOrderWriterStore(
            db, now_provider=lambda: now
        ).load_approved_execution(result.approved_execution_id)
        db.execute(text("RESET ROLE"))
        db.commit()
    assert claimed.approval_id == result.approved_execution_id

    with factory.begin() as db:
        db.execute(text("RESET ROLE"))
        chain = db.get(FullChainRun, execution_chain_id)
        chain.trade_intent_id = None
        chain.risk_decision_id = None
        chain.approved_execution_id = None
        chain.current_stage = "RISK"
        db.flush()
        db.execute(
            text("DELETE FROM trade_intents WHERE id=:intent_id"),
            {"intent_id": result.trade_intent_id},
        )
        checkpoint = db.scalars(
            select(FullChainStageRun).where(
                FullChainStageRun.full_chain_run_id == chain.id,
                FullChainStageRun.stage == "RISK",
            )
        ).one()
        checkpoint.status = "PREPARED"
        checkpoint.database_ids = {}
        checkpoint.completed_at = None
        budget = db.get(RiskBudget, "OKX_DEMO")
        budget.reserved_notional = Decimal("3000")
        budget.approved_positions = 3
    with factory() as db:
        db.execute(text("SET ROLE freqtrade"))
        db.commit()
        exhausted = RiskChainService(db).evaluate(
            idempotency_key=key,
            request=request,
            policy=policy,
            natural_signal_context={
                "deployment_id": deployment_id,
                "lease_token": lease_token,
                "fencing_sequence": 1,
            },
            now=now,
        )
        db.execute(text("RESET ROLE"))
        db.commit()
    assert exhausted.status == "REJECTED"
    assert exhausted.approved_execution_id is None
    with factory() as db:
        budget = db.get(RiskBudget, "OKX_DEMO")
        assert budget.reserved_notional == Decimal("3000")
        assert budget.approved_positions == 3
        assert db.execute(text("SELECT count(*) FROM risk_budgets")).scalar_one() == 1

    with factory() as db:
        db.execute(text("SET ROLE freqtrade"))
        db.commit()
        with pytest.raises(DBAPIError):
            RiskChainService(db).evaluate(
                idempotency_key=key + "-alternate",
                request=request,
                policy=policy,
                natural_signal_context={
                    "deployment_id": deployment_id,
                    "lease_token": lease_token,
                    "fencing_sequence": 1,
                },
                now=now,
            )
        db.rollback()


def test_postgresql_v44_reinstalls_repository_datetime_digest_contract(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_functiondef("
                "'persist_okx_demo_natural_risk_chain(jsonb)'::regprocedure)"
            )
        ).scalar_one()
        legacy_definition = definition.replace(
            "'YYYY-MM-DD HH24:MI:SS'",
            "'YYYY-MM-DD\"T\"HH24:MI:SS'",
            2,
        )
        assert legacy_definition != definition
        connection.execute(text(legacy_definition))
        connection.execute(
            text("DELETE FROM freqtrade_ai_schema_migrations WHERE version=:version"),
            {"version": SCHEMA_VERSION},
        )
        connection.execute(
            text(
                "INSERT INTO freqtrade_ai_schema_migrations(version) "
                "VALUES(:version)"
            ),
            {"version": NATURAL_SIGNAL_EVALUATOR_RECEIPT_BASE_VERSION},
        )
        connection.execute(
            text(
                "INSERT INTO okx_demo_automation_guard_states("
                "execution_target_id,authorization_mode,operational_state,"
                "policy_digest,deployment_set_digest,critical_failure_count,"
                "health_check_required,fencing_version) VALUES("
                "'OKX_DEMO','CONTINUOUS_DEMO_V1','RUNNING',:digest,:digest,"
                "0,false,1)"
            ),
            {"digest": "0" * 64},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    with postgres_engine.connect() as connection:
        repaired = connection.execute(
            text(
                "SELECT pg_get_functiondef("
                "'persist_okx_demo_natural_risk_chain(jsonb)'::regprocedure)"
            )
        ).scalar_one()
        guard = connection.execute(
            text(
                "SELECT operational_state,policy_digest,deployment_set_digest,"
                "fencing_version FROM okx_demo_automation_guard_states "
                "WHERE execution_target_id='OKX_DEMO'"
            )
        ).one()
    assert "FullChainRepository._stable_digest uses Python's str(datetime)" in repaired
    assert repaired.count("'YYYY-MM-DD HH24:MI:SS'") >= 2
    assert tuple(guard) == ("RUNNING", "0" * 64, "0" * 64, 1)


def test_postgresql_v43_upgrades_budget_initializer_and_acl_idempotently(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(text("RESET ROLE"))
        _add_natural_signal_risk_chain_boundary(
            connection,
            refresh_supporting_boundaries=False,
            rebind_automation_guard=False,
            initialize_missing_budget=False,
        )
        connection.execute(text(
            "REVOKE INSERT ON risk_budgets FROM freqtrade_ai_attestor"
        ))
        connection.execute(
            text("DELETE FROM freqtrade_ai_schema_migrations WHERE version=:version"),
            {"version": SCHEMA_VERSION},
        )
        connection.execute(
            text("INSERT INTO freqtrade_ai_schema_migrations(version) VALUES(:version)"),
            {"version": NATURAL_SIGNAL_RISK_BUDGET_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True
    with postgres_engine.connect() as connection:
        source = connection.execute(text(
            "SELECT prosrc FROM pg_proc WHERE oid="
            "'persist_okx_demo_natural_risk_chain(jsonb)'::regprocedure"
        )).scalar_one()
        acl = connection.execute(text(
            "SELECT has_table_privilege('freqtrade','risk_budgets','INSERT,UPDATE,DELETE'),"
            "has_column_privilege('freqtrade_ai_attestor','risk_budgets',"
            "'execution_target_id','INSERT'),"
            "has_column_privilege('freqtrade_ai_attestor','risk_budgets',"
            "'updated_at','INSERT')"
        )).one()
    assert "ON CONFLICT (execution_target_id) DO NOTHING" in source
    assert tuple(acl) == (False, True, True)


def test_postgresql_v44_upgrades_private_stale_release_idempotently(
    postgres_engine,
) -> None:
    upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(text("RESET ROLE"))
        connection.execute(
            text(
                "ALTER FUNCTION release_unclaimed_expired_natural_approvals() "
                "OWNER TO postgres; "
                "REVOKE SELECT (exchange_fill_id) ON full_chain_runs "
                "FROM freqtrade_ai_attestor"
            )
        )
        connection.execute(
            text("DELETE FROM freqtrade_ai_schema_migrations WHERE version=:version"),
            {"version": SCHEMA_VERSION},
        )
        connection.execute(
            text("INSERT INTO freqtrade_ai_schema_migrations(version) VALUES(:version)"),
            {"version": STALE_NATURAL_APPROVAL_RELEASE_BASE_VERSION},
        )

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True
    with postgres_engine.connect() as connection:
        owner, runtime_execute, fill_select = connection.execute(
            text(
                "SELECT owner.rolname,"
                "has_function_privilege('freqtrade',function.oid,'EXECUTE'),"
                "has_column_privilege('freqtrade_ai_attestor','full_chain_runs',"
                "'exchange_fill_id','SELECT') "
                "FROM pg_proc function JOIN pg_roles owner "
                "ON owner.oid=function.proowner "
                "WHERE function.oid="
                "'release_unclaimed_expired_natural_approvals()'::regprocedure"
            )
        ).one()
    assert owner == "freqtrade_ai_attestor"
    assert runtime_execute is False
    assert fill_select is True
