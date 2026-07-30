import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from typing import Optional

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.migrations import upgrade_database
from app.db.session import create_database_engine, create_session_factory
from app.models import (
    ApprovedExecution,
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    FullChainRun,
    FullChainStageRun,
    ReconciliationRun,
    ResearchJobAttempt,
    ResearchWorkerControl,
    RiskDecision,
    Strategy,
    StrategyCandidateApproval,
    StrategyGenerationRun,
    StrategyScore,
    StrategyVersion,
    TradeIntent,
)
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.api.strategy_promotion import evaluate_strategy_promotion
from app.repositories.full_chain import (
    FullChainBlocked,
    FullChainConflict,
    FullChainRepository,
    OKX_DEMO_AUTO_APPROVAL_ACTOR,
    require_authoritative_reconciliation,
)
from app.repositories.research_jobs import ResearchJobRepository


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
POSTGRES_WORKER_URL = os.environ.get("POSTGRES_WORKER_URL")


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        ensure_execution_scope_catalog(session)
        yield session
    engine.dispose()


@pytest.fixture
def postgres_factory():
    if not POSTGRES_WORKER_URL:
        pytest.skip(
            "POSTGRES_WORKER_URL is required for the PostgreSQL approval gate"
        )
    engine = create_database_engine(POSTGRES_WORKER_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    upgrade_database(engine)
    factory = create_session_factory(engine)
    yield factory
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def claimed_job(db):
    jobs = ResearchJobRepository(db)
    job = jobs.create(
        job_type="deepseek_backtest",
        operation="strategy_generation.deepseek_backtest_loop",
        idempotency_key_digest="a" * 64,
        request_hash="b" * 64,
        request_payload={"allow_real_call": False, "strategy": {"name": "Chain"}},
    )
    claimed = jobs.claim_next(owner="full-chain-test", lease_seconds=600, now=NOW)
    assert claimed is not None and claimed.id == job.id and claimed.lease_token
    return claimed


def seed_research_lineage(db):
    generation = StrategyGenerationRun(
        execution_scope_id="LOCAL_DRY_RUN",
        provider="deepseek",
        model="deepseek-test",
        status="succeeded",
        requested_count=1,
        generated_count=1,
        accepted_count=1,
    )
    strategy = Strategy(name="Chain", slug="chain", source="ai_generated")
    db.add_all([generation, strategy])
    db.flush()
    version = StrategyVersion(
        strategy_id=strategy.id,
        generation_run_id=generation.id,
        version_number=1,
        blueprint={"strategy": "Chain"},
        generated_code="class Chain: pass",
        code_hash="c" * 64,
        file_path="user_data/strategies/Chain.py",
        validation_status="passed",
    )
    db.add(version)
    db.flush()
    run = BacktestRun(
        execution_scope_id="LOCAL_DRY_RUN",
        strategy_version_id=version.id,
        profile_name="full-chain",
        status="succeeded",
        requested_task_count=1,
    )
    db.add(run)
    db.flush()
    task = BacktestTask(
        backtest_run_id=run.id,
        pair="BTC/USDT:USDT",
        timeframe="5m",
        status="succeeded",
        result_path="reports/backtests/chain.json",
    )
    db.add(task)
    db.flush()
    result = BacktestResult(
        backtest_run_id=run.id,
        backtest_task_id=task.id,
        result_path="reports/backtests/chain.json",
        metrics_snapshot={
            "promotion_evidence": {
                "net_of_costs": True,
                "out_of_sample": {
                    "passed": True,
                    "profit_pct": 0.04,
                    "total_trades": 36,
                },
                "walk_forward": {
                    "passed": True,
                    "market_states": ["bull", "bear", "range"],
                },
            },
        },
        profit_pct=0.06,
        max_drawdown_pct=0.12,
        total_trades=36,
    )
    db.add(result)
    db.flush()
    score = StrategyScore(
        strategy_id=strategy.id,
        strategy_version_id=version.id,
        backtest_result_id=result.id,
        scoring_version="full-chain-v1",
        total_score=80,
        metrics_snapshot={
            "source": "backtest_result",
            "backtest_result_id": result.id,
            "acceptance_ready": True,
        },
    )
    db.add(score)
    db.commit()
    return generation, strategy, version, run, task, result, score


def prepare_and_complete_research(db, repository, chain, lease_token):
    generation, strategy, version, run, task, result, score = seed_research_lineage(db)
    repository.prepare_stage(
        chain.id,
        "GENERATION",
        lease_token,
        idempotency_key="generation-1",
        input_snapshot={"provider": "deepseek", "model": "deepseek-test"},
        now=NOW,
    )
    repository.complete_stage(
        chain.id,
        "GENERATION",
        lease_token,
        database_ids={
            "strategy_generation_run_id": generation.id,
            "strategy_id": strategy.id,
            "strategy_version_id": version.id,
        },
        output_snapshot={"status": "succeeded", "artifact_sha256": "c" * 64},
        now=NOW,
    )
    repository.prepare_stage(
        chain.id,
        "BACKTEST",
        lease_token,
        idempotency_key="backtest-1",
        input_snapshot={"strategy_version_id": version.id, "profile": "full-chain"},
        now=NOW,
    )
    repository.complete_stage(
        chain.id,
        "BACKTEST",
        lease_token,
        database_ids={
            "backtest_run_id": run.id,
            "backtest_task_id": task.id,
            "backtest_result_id": result.id,
        },
        output_snapshot={"status": "succeeded", "artifact_sha256": "d" * 64},
        now=NOW,
    )
    repository.prepare_stage(
        chain.id,
        "SCORING",
        lease_token,
        idempotency_key="scoring-1",
        input_snapshot={"backtest_result_id": result.id, "scoring_version": "full-chain-v1"},
        now=NOW,
    )
    repository.complete_stage(
        chain.id,
        "SCORING",
        lease_token,
        database_ids={"strategy_score_id": score.id},
        output_snapshot={"status": "succeeded", "score": 80},
        now=NOW,
    )
    return generation, strategy, version, run, task, result, score


def test_chain_reuses_research_job_attempt_and_persists_approval_and_signal(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    replay = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    assert replay.id == chain.id
    assert chain.research_scope_id == "LOCAL_DRY_RUN"
    assert chain.execution_target_id == "OKX_DEMO"

    lineage = prepare_and_complete_research(
        db, repository, chain, job.lease_token
    )
    version, result, score = lineage[2], lineage[5], lineage[6]

    with pytest.raises(FullChainBlocked, match="predecessors"):
        repository.prepare_stage(
            chain.id,
            "RISK",
            job.lease_token,
            idempotency_key="skip-to-risk",
            input_snapshot={"execution_target": "OKX_DEMO"},
            now=NOW,
        )

    approval_stage = repository.prepare_stage(
        chain.id,
        "CANDIDATE_APPROVAL",
        job.lease_token,
        idempotency_key="candidate-approval-1",
        input_snapshot={
            "strategy_version_id": version.id,
            "backtest_result_id": result.id,
            "strategy_score_id": score.id,
        },
        now=NOW,
    )
    approval = repository.create_candidate_approval(
        chain.id,
        job.lease_token,
        requested_by="operator",
        expires_at=NOW + timedelta(minutes=10),
        now=NOW,
    )
    assert approval.status == "PENDING"
    repository.decide_candidate(
        approval.id,
        decision="APPROVED",
        decided_by="operator",
        reason="Reviewed the exact scored candidate.",
        now=NOW + timedelta(seconds=1),
    )
    waiting_job = ResearchJobRepository(db).get(job.id)
    assert waiting_job is not None
    assert waiting_job.status == "PENDING"
    assert waiting_job.stage == "CANDIDATE_APPROVED"
    assert waiting_job.lease_token is None
    resumed = ResearchJobRepository(db).claim_next(
        owner="full-chain-resume",
        lease_seconds=600,
        now=NOW + timedelta(seconds=2),
    )
    assert resumed is not None
    assert resumed.id == job.id
    assert resumed.attempt_count == 1
    assert resumed.lease_token
    repository.open_for_claimed_job(
        resumed.id,
        resumed.lease_token,
        now=NOW + timedelta(seconds=2),
    )
    assert approval_stage.id is not None
    repository.prepare_stage(
        chain.id,
        "SIGNAL",
        resumed.lease_token,
        idempotency_key="signal-1",
        input_snapshot={"instrument_id": "BTC-USDT-SWAP"},
        now=NOW + timedelta(seconds=3),
    )
    signal = repository.record_signal(
        chain.id,
        resumed.lease_token,
        instrument_id="BTC-USDT-SWAP",
        source_type="api_aggregate",
        source_database_ids={"market_snapshot_id": 12, "candle_id": 34},
        signal_snapshot={"side": "buy", "timeframe": "5m", "closed_candle": True},
        observed_at=NOW + timedelta(seconds=3),
        expires_at=NOW + timedelta(minutes=1),
    )
    repository.complete_stage(
        chain.id,
        "SIGNAL",
        resumed.lease_token,
        database_ids={"signal_snapshot_id": signal.id},
        output_snapshot={"status": "succeeded", "signal_digest": signal.signal_digest},
        now=NOW + timedelta(seconds=3),
    )

    persisted = db.get(FullChainRun, chain.id)
    assert persisted.status == "EXECUTING"
    assert persisted.strategy_version_id == version.id
    assert persisted.candidate_approval_id == approval.id
    assert persisted.signal_snapshot_id == signal.id
    assert [
        row.stage
        for row in db.query(FullChainStageRun)
        .filter(FullChainStageRun.full_chain_run_id == chain.id)
        .order_by(FullChainStageRun.id)
    ] == [
        "GENERATION",
        "BACKTEST",
        "SCORING",
        "CANDIDATE_APPROVAL",
        "SIGNAL",
    ]

    intent = TradeIntent(
        execution_target_id="OKX_DEMO",
        authorization_schema_version="RISK_V1",
        intent_id="1" * 64,
        canonical_hash="2" * 64,
        policy_digest="3" * 64,
        approved_payload_hash="4" * 64,
        idempotency_key_digest="5" * 64,
        client_order_id="FAI" + "1" * 29,
        strategy_id=chain.strategy_id,
        strategy_version_id=chain.strategy_version_id,
        backtest_run_id=chain.backtest_run_id,
        backtest_result_id=chain.backtest_result_id,
        strategy_score_id=chain.strategy_score_id,
        instrument_id=signal.instrument_id,
        side="buy",
        position_side="long",
        order_type="limit",
        quantity=Decimal("0.01"),
        limit_price=Decimal("50000"),
        reference_price=Decimal("50000"),
        leverage=Decimal("2"),
        margin_mode="isolated",
        stop_loss=Decimal("48000"),
        take_profit=Decimal("54000"),
        reduce_only=False,
        status="APPROVED",
        request_snapshot={
            "canonical_input": {
                "full_chain_run_id": chain.id,
                "candidate_approval_id": approval.id + 1,
                "signal_snapshot_id": signal.id,
                "signal_digest": signal.signal_digest,
            }
        },
        expires_at=NOW + timedelta(minutes=2),
    )
    db.add(intent)
    db.flush()
    decision = RiskDecision(
        execution_target_id="OKX_DEMO",
        trade_intent_id=intent.id,
        authorization_schema_version="RISK_V1",
        policy_digest=intent.policy_digest,
        decision="APPROVED",
        policy_version="risk-chain-v2",
        evidence_snapshot={},
    )
    db.add(decision)
    db.flush()
    execution = ApprovedExecution(
        execution_target_id="OKX_DEMO",
        trade_intent_id=intent.id,
        risk_decision_id=decision.id,
        intent_id=intent.intent_id,
        client_order_id=intent.client_order_id,
        authorization_schema_version="RISK_V1",
        canonical_hash=intent.canonical_hash,
        policy_digest=intent.policy_digest,
        approved_payload_hash=intent.approved_payload_hash,
        instrument_snapshot_id="instrument:test",
        market_snapshot_id="market:test",
        account_snapshot_id="account:test",
        decision="APPROVED",
        intent_status="APPROVED",
        reserved_notional=Decimal("500"),
        order_submission_authorized=False,
        claim_required=True,
        status="ACTIVE",
        expires_at=NOW + timedelta(minutes=2),
        evidence_snapshot={},
    )
    db.add(execution)
    db.commit()
    repository.prepare_stage(
        chain.id,
        "RISK",
        resumed.lease_token,
        idempotency_key="risk-binding-check",
        input_snapshot={"signal_snapshot_id": signal.id},
        now=NOW + timedelta(seconds=4),
    )

    with pytest.raises(
        FullChainBlocked,
        match="risk approval lineage is inconsistent",
    ):
        repository.complete_stage(
            chain.id,
            "RISK",
            resumed.lease_token,
            database_ids={
                "trade_intent_id": intent.id,
                "risk_decision_id": decision.id,
                "approved_execution_id": execution.id,
            },
            output_snapshot={"status": "APPROVED"},
            now=NOW + timedelta(seconds=4),
        )


def test_candidate_approval_blocks_unvalidated_promotion_evidence(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    lineage = prepare_and_complete_research(db, repository, chain, job.lease_token)
    result = lineage[5]
    result.metrics_snapshot = {}
    db.commit()
    repository.prepare_stage(
        chain.id,
        "CANDIDATE_APPROVAL",
        job.lease_token,
        idempotency_key="candidate-promotion-blocked",
        input_snapshot={"backtest_result_id": result.id},
        now=NOW,
    )

    with pytest.raises(FullChainBlocked, match="promotion requires net-of-costs evidence"):
        repository.create_candidate_approval(
            chain.id,
            job.lease_token,
            requested_by="operator",
            expires_at=NOW + timedelta(minutes=10),
            now=NOW,
        )


def test_okx_demo_candidate_is_automatically_approved_with_auditable_gates(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    lineage = prepare_and_complete_research(db, repository, chain, job.lease_token)
    version, result, score = lineage[2], lineage[5], lineage[6]
    checkpoint = repository.prepare_stage(
        chain.id,
        "CANDIDATE_APPROVAL",
        job.lease_token,
        idempotency_key="automatic-candidate-approval-1",
        input_snapshot={
            "approval_mode": "AUTOMATIC",
            "execution_target_id": "OKX_DEMO",
            "strategy_version_id": version.id,
            "backtest_result_id": result.id,
            "strategy_score_id": score.id,
        },
        now=NOW,
    )

    approval = repository.auto_approve_candidate(
        chain.id,
        job.lease_token,
        now=NOW,
    )

    assert approval.status == "APPROVED"
    assert approval.requested_by == OKX_DEMO_AUTO_APPROVAL_ACTOR
    assert approval.decided_by == OKX_DEMO_AUTO_APPROVAL_ACTOR
    assert approval.decision_reason == (
        "Automatically approved for OKX_DEMO after deterministic "
        "promotion gates passed."
    )
    assert approval.expires_at.replace(tzinfo=timezone.utc) == NOW + timedelta(minutes=5)
    assert checkpoint.status == "SUCCESS"
    assert checkpoint.database_ids == {"candidate_approval_id": approval.id}
    assert checkpoint.output_snapshot["approval_mode"] == "AUTOMATIC"
    assert checkpoint.output_snapshot["manual_confirmation_required"] is False
    assert checkpoint.output_snapshot["automation_policy_schema_version"] == "1"
    assert checkpoint.output_snapshot["execution_target_id"] == "OKX_DEMO"
    assert checkpoint.output_snapshot["allow_real_funds"] is False
    assert checkpoint.output_snapshot["candidate_digest"] == approval.candidate_digest
    assert checkpoint.output_snapshot["hard_gates"] == {
        "validated_strategy_version": True,
        "positive_net_profit": True,
        "drawdown_limit": True,
        "minimum_trade_count": True,
        "out_of_sample": True,
        "walk_forward_market_states": True,
        "net_of_costs": True,
    }
    persisted_chain = db.get(FullChainRun, chain.id)
    assert persisted_chain.status == "APPROVED"
    assert persisted_chain.current_stage == "SIGNAL"
    resumed_job = ResearchJobRepository(db).get(job.id)
    assert resumed_job is not None
    assert resumed_job.status == "PENDING"
    assert resumed_job.stage == "CANDIDATE_APPROVED"
    assert resumed_job.lease_token is None


def test_okx_demo_auto_approval_rolls_back_every_state_on_commit_failure(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    lineage = prepare_and_complete_research(
        db,
        repository,
        chain,
        job.lease_token,
    )
    repository.prepare_stage(
        chain.id,
        "CANDIDATE_APPROVAL",
        job.lease_token,
        idempotency_key="automatic-candidate-rollback",
        input_snapshot={
            "approval_mode": "AUTOMATIC",
            "execution_target_id": "OKX_DEMO",
            "strategy_version_id": lineage[2].id,
            "backtest_result_id": lineage[5].id,
            "strategy_score_id": lineage[6].id,
        },
        now=NOW,
    )

    def fail_commit(_session) -> None:
        raise RuntimeError("simulated atomic approval commit failure")

    event.listen(db, "before_commit", fail_commit)
    try:
        with pytest.raises(
            RuntimeError,
            match="simulated atomic approval commit failure",
        ):
            repository.auto_approve_candidate(
                chain.id,
                job.lease_token,
                now=NOW,
            )
    finally:
        event.remove(db, "before_commit", fail_commit)

    db.expire_all()
    persisted_chain = db.get(FullChainRun, chain.id)
    persisted_job = ResearchJobRepository(db).get(job.id)
    checkpoint = (
        db.query(FullChainStageRun)
        .filter(
            FullChainStageRun.full_chain_run_id == chain.id,
            FullChainStageRun.stage == "CANDIDATE_APPROVAL",
        )
        .one()
    )
    control = db.get(ResearchWorkerControl, 1)
    assert db.query(StrategyCandidateApproval).count() == 0
    assert persisted_chain is not None
    assert persisted_chain.candidate_approval_id is None
    assert persisted_chain.status == "RUNNING"
    assert persisted_chain.current_stage == "CANDIDATE_APPROVAL"
    assert checkpoint.status == "PREPARED"
    assert checkpoint.database_ids == {}
    assert persisted_job is not None
    assert persisted_job.status == "RUNNING"
    assert persisted_job.lease_token == job.lease_token
    assert control is not None
    assert control.active_job_id == job.id
    assert control.active_lease_token == job.lease_token


def test_okx_demo_auto_approval_rejects_stale_lease_without_partial_state(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    lineage = prepare_and_complete_research(
        db,
        repository,
        chain,
        job.lease_token,
    )
    repository.prepare_stage(
        chain.id,
        "CANDIDATE_APPROVAL",
        job.lease_token,
        idempotency_key="automatic-candidate-stale-lease",
        input_snapshot={
            "strategy_version_id": lineage[2].id,
            "backtest_result_id": lineage[5].id,
            "strategy_score_id": lineage[6].id,
        },
        now=NOW,
    )

    with pytest.raises(FullChainBlocked, match="lease.*fenced"):
        repository.auto_approve_candidate(
            chain.id,
            "stale-lease-token",
            now=NOW,
        )

    db.expire_all()
    checkpoint = (
        db.query(FullChainStageRun)
        .filter(
            FullChainStageRun.full_chain_run_id == chain.id,
            FullChainStageRun.stage == "CANDIDATE_APPROVAL",
        )
        .one()
    )
    persisted_job = ResearchJobRepository(db).get(job.id)
    assert db.query(StrategyCandidateApproval).count() == 0
    assert checkpoint.status == "PREPARED"
    assert persisted_job is not None
    assert persisted_job.status == "RUNNING"
    assert persisted_job.lease_token == job.lease_token


def test_okx_demo_auto_approval_rejects_attempt_mismatch(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    lineage = prepare_and_complete_research(
        db,
        repository,
        chain,
        job.lease_token,
    )
    repository.prepare_stage(
        chain.id,
        "CANDIDATE_APPROVAL",
        job.lease_token,
        idempotency_key="automatic-candidate-attempt-mismatch",
        input_snapshot={
            "strategy_version_id": lineage[2].id,
            "backtest_result_id": lineage[5].id,
            "strategy_score_id": lineage[6].id,
        },
        now=NOW,
    )
    persisted_job = ResearchJobRepository(db).get(job.id)
    assert persisted_job is not None
    persisted_job.attempt_count = 2
    db.add(
        ResearchJobAttempt(
            research_job_id=job.id,
            attempt_number=2,
            execution_scope_id="LOCAL_DRY_RUN",
            status="RUNNING",
            started_at=NOW,
        )
    )
    db.commit()

    with pytest.raises(FullChainBlocked, match="Attempt.*fenced"):
        repository.auto_approve_candidate(
            chain.id,
            job.lease_token,
            now=NOW,
        )

    db.expire_all()
    assert db.query(StrategyCandidateApproval).count() == 0
    assert (
        db.query(FullChainStageRun)
        .filter(
            FullChainStageRun.full_chain_run_id == chain.id,
            FullChainStageRun.stage == "CANDIDATE_APPROVAL",
        )
        .one()
        .status
        == "PREPARED"
    )


def test_postgresql_concurrent_okx_demo_auto_approval_has_one_fenced_winner(
    postgres_factory,
) -> None:
    with postgres_factory() as setup:
        job = claimed_job(setup)
        repository = FullChainRepository(setup)
        chain = repository.open_for_claimed_job(
            job.id,
            job.lease_token,
            now=NOW,
        )
        lineage = prepare_and_complete_research(
            setup,
            repository,
            chain,
            job.lease_token,
        )
        repository.prepare_stage(
            chain.id,
            "CANDIDATE_APPROVAL",
            job.lease_token,
            idempotency_key="postgres-concurrent-auto-approval",
            input_snapshot={
                "strategy_version_id": lineage[2].id,
                "backtest_result_id": lineage[5].id,
                "strategy_score_id": lineage[6].id,
            },
            now=NOW,
        )
        job_id = job.id
        chain_id = chain.id
        lease_token = job.lease_token

    barrier = Barrier(2)

    def approve() -> tuple[str, Optional[int]]:
        with postgres_factory() as session:
            barrier.wait()
            try:
                approval = FullChainRepository(
                    session
                ).auto_approve_candidate(
                    chain_id,
                    lease_token,
                    now=NOW,
                )
                return "APPROVED", approval.id
            except (FullChainBlocked, FullChainConflict):
                return "FENCED", None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: approve(), range(2)))

    assert sorted(status for status, _approval_id in outcomes) == [
        "APPROVED",
        "FENCED",
    ]
    with postgres_factory() as verify:
        approvals = verify.query(StrategyCandidateApproval).all()
        assert len(approvals) == 1
        assert approvals[0].status == "APPROVED"
        persisted_job = ResearchJobRepository(verify).get(job_id)
        assert persisted_job is not None
        assert persisted_job.status == "PENDING"
        assert persisted_job.stage == "CANDIDATE_APPROVED"
        assert persisted_job.lease_token is None
        checkpoint = (
            verify.query(FullChainStageRun)
            .filter(
                FullChainStageRun.full_chain_run_id == chain_id,
                FullChainStageRun.stage == "CANDIDATE_APPROVAL",
            )
            .one()
        )
        assert checkpoint.status == "SUCCESS"
        assert checkpoint.database_ids == {
            "candidate_approval_id": approvals[0].id
        }


def test_okx_demo_auto_approval_keeps_promotion_fail_closed(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    lineage = prepare_and_complete_research(db, repository, chain, job.lease_token)
    result = lineage[5]
    result.metrics_snapshot = {}
    db.commit()
    repository.prepare_stage(
        chain.id,
        "CANDIDATE_APPROVAL",
        job.lease_token,
        idempotency_key="automatic-candidate-promotion-blocked",
        input_snapshot={
            "approval_mode": "AUTOMATIC",
            "execution_target_id": "OKX_DEMO",
            "backtest_result_id": result.id,
        },
        now=NOW,
    )

    with pytest.raises(
        FullChainBlocked,
        match="promotion requires net-of-costs evidence",
    ):
        repository.auto_approve_candidate(
            chain.id,
            job.lease_token,
            now=NOW,
        )


def test_approved_candidate_can_be_revoked_before_execution(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    lineage = prepare_and_complete_research(db, repository, chain, job.lease_token)
    repository.prepare_stage(
        chain.id,
        "CANDIDATE_APPROVAL",
        job.lease_token,
        idempotency_key="candidate-revocation",
        input_snapshot={"strategy_score_id": lineage[6].id},
        now=NOW,
    )
    approval = repository.create_candidate_approval(
        chain.id,
        job.lease_token,
        requested_by="operator",
        expires_at=NOW + timedelta(minutes=10),
        now=NOW,
    )
    repository.decide_candidate(
        approval.id,
        decision="APPROVED",
        decided_by="operator",
        reason="Initial review completed.",
        now=NOW + timedelta(seconds=1),
    )

    revoked = repository.revoke_candidate_approval(
        approval.id,
        revoked_by="operator",
        reason="Market conditions changed.",
        now=NOW + timedelta(seconds=2),
    )

    assert revoked.status == "REVOKED"
    assert db.get(FullChainRun, chain.id).status == "BLOCKED"


def test_approved_candidate_is_automatically_revoked_when_market_evidence_changes(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    lineage = prepare_and_complete_research(db, repository, chain, job.lease_token)
    repository.prepare_stage(
        chain.id,
        "CANDIDATE_APPROVAL",
        job.lease_token,
        idempotency_key="candidate-auto-revalidation",
        input_snapshot={"strategy_score_id": lineage[6].id},
        now=NOW,
    )
    approval = repository.create_candidate_approval(
        chain.id,
        job.lease_token,
        requested_by="operator",
        expires_at=NOW + timedelta(minutes=10),
        now=NOW,
    )
    repository.decide_candidate(
        approval.id,
        decision="APPROVED",
        decided_by="operator",
        reason="Evidence reviewed.",
        now=NOW + timedelta(seconds=1),
    )
    resumed = ResearchJobRepository(db).claim_next(
        owner="promotion-revalidation", lease_seconds=600, now=NOW + timedelta(seconds=2)
    )
    assert resumed is not None and resumed.lease_token
    repository.open_for_claimed_job(resumed.id, resumed.lease_token, now=NOW + timedelta(seconds=2))
    repository.prepare_stage(
        chain.id,
        "SIGNAL",
        resumed.lease_token,
        idempotency_key="signal-auto-revalidation",
        input_snapshot={"instrument_id": "BTC-USDT-SWAP"},
        now=NOW + timedelta(seconds=3),
    )
    result = lineage[5]
    result.metrics_snapshot = {
        **result.metrics_snapshot,
        "promotion_evidence": {
            **result.metrics_snapshot["promotion_evidence"],
            "walk_forward": {"passed": True, "market_states": ["bull", "bear", "range", "changed"]},
        },
    }
    db.commit()

    with pytest.raises(FullChainBlocked, match="Automatic promotion invalidation"):
        repository.record_signal(
            chain.id,
            resumed.lease_token,
            instrument_id="BTC-USDT-SWAP",
            source_type="api_aggregate",
            source_database_ids={"market_snapshot_id": 12, "candle_id": 34},
            signal_snapshot={"side": "buy", "timeframe": "5m", "closed_candle": True},
            observed_at=NOW + timedelta(seconds=3),
            expires_at=NOW + timedelta(minutes=1),
        )

    persisted = db.get(FullChainRun, chain.id)
    assert persisted is not None and persisted.status == "BLOCKED"
    assert db.get(type(approval), approval.id).status == "REVOKED"
    assert "market evidence changed" in (db.get(type(approval), approval.id).decision_reason or "")


def test_promotion_api_exposes_policy_evidence_and_never_treats_score_as_approval(db) -> None:
    _, _, version, _, _, result, score = seed_research_lineage(db)

    response = evaluate_strategy_promotion(
        strategy_version_id=version.id,
        backtest_result_id=result.id,
        strategy_score_id=score.id,
        db=db,
    )

    assert response["status"] == "ELIGIBLE"
    assert response["approval"] is None
    assert response["policy"]["policy_version"] == "strategy-promotion-v1"
    assert response["evidence"]["net_of_costs"] is True
    assert response["artifact_refs"]["backtest_result_path"] == result.result_path


def test_stage_prepare_is_idempotent_and_rejects_changed_input_or_secrets(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    first = repository.prepare_stage(
        chain.id,
        "GENERATION",
        job.lease_token,
        idempotency_key="generation-1",
        input_snapshot={"provider": "deepseek"},
        now=NOW,
    )
    with pytest.raises(FullChainBlocked, match="refusing to repeat"):
        repository.prepare_stage(
            chain.id,
            "GENERATION",
            job.lease_token,
            idempotency_key="generation-1",
            input_snapshot={"provider": "deepseek"},
            now=NOW,
        )
    assert first.status == "PREPARED"
    with pytest.raises(FullChainConflict, match="different"):
        repository.prepare_stage(
            chain.id,
            "GENERATION",
            job.lease_token,
            idempotency_key="generation-1",
            input_snapshot={"provider": "fallback"},
            now=NOW,
        )

    second_job = ResearchJobRepository(db).create(
        job_type="deepseek_backtest",
        operation="strategy_generation.deepseek_backtest_loop",
        idempotency_key_digest="e" * 64,
        request_hash="f" * 64,
        request_payload={"allow_real_call": False},
    )
    assert second_job.status == "PENDING"
    with pytest.raises(FullChainBlocked, match="ResearchJob lease"):
        repository.open_for_claimed_job(second_job.id, "not-owner", now=NOW)

    with pytest.raises(FullChainBlocked, match="secret-shaped"):
        repository.prepare_stage(
            chain.id,
            "GENERATION",
            job.lease_token,
            idempotency_key="other",
            input_snapshot={"api_key": "must-not-persist"},
            now=NOW,
        )


def test_empty_or_failed_stage_never_marks_chain_success(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    repository.prepare_stage(
        chain.id,
        "GENERATION",
        job.lease_token,
        idempotency_key="generation-failure",
        input_snapshot={"provider": "deepseek"},
        now=NOW,
    )
    with pytest.raises(FullChainBlocked, match="database IDs mismatch"):
        repository.complete_stage(
            chain.id,
            "GENERATION",
            job.lease_token,
            database_ids={},
            output_snapshot={"status": "succeeded"},
            now=NOW,
        )
    repository.fail_stage(
        chain.id,
        "GENERATION",
        job.lease_token,
        status="BLOCKED",
        error_code="PROVIDER_NOT_CONFIGURED",
        error_message="DeepSeek was not called.",
        now=NOW,
    )
    persisted = db.get(FullChainRun, chain.id)
    assert persisted.status == "BLOCKED"
    assert persisted.completed_at == NOW
    assert db.query(FullChainRun).filter(FullChainRun.status == "SUCCESS").count() == 0


def test_candidate_expiry_is_persisted_and_blocks_downstream_stages(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    prepare_and_complete_research(db, repository, chain, job.lease_token)
    repository.prepare_stage(
        chain.id,
        "CANDIDATE_APPROVAL",
        job.lease_token,
        idempotency_key="candidate-expiry",
        input_snapshot={"review": "required"},
        now=NOW,
    )
    approval = repository.create_candidate_approval(
        chain.id,
        job.lease_token,
        requested_by="operator",
        expires_at=NOW + timedelta(seconds=1),
        now=NOW,
    )
    with pytest.raises(FullChainBlocked, match="expired"):
        repository.decide_candidate(
            approval.id,
            decision="APPROVED",
            decided_by="operator",
            reason="too late",
            now=NOW + timedelta(seconds=2),
        )
    db.refresh(approval)
    db.refresh(chain)
    assert approval.status == "EXPIRED"
    assert chain.status == "BLOCKED"
    with pytest.raises(FullChainBlocked, match="terminal"):
        repository.prepare_stage(
            chain.id,
            "SIGNAL",
            job.lease_token,
            idempotency_key="blocked-signal",
            input_snapshot={"instrument_id": "BTC-USDT-SWAP"},
            now=NOW + timedelta(seconds=2),
        )


def test_reconciliation_row_alone_cannot_mark_an_incomplete_chain_success(db) -> None:
    job = claimed_job(db)
    repository = FullChainRepository(db)
    chain = repository.open_for_claimed_job(job.id, job.lease_token, now=NOW)
    reconciliation = ReconciliationRun(
        execution_target_id="OKX_DEMO",
        status="RECONCILED",
        summary_snapshot={"fixture": True},
        started_at=NOW,
        completed_at=NOW,
    )
    db.add(reconciliation)
    db.flush()
    db.add(
        FullChainStageRun(
            full_chain_run_id=chain.id,
            stage="RECONCILIATION",
            status="PREPARED",
            idempotency_key_digest="1" * 64,
            input_digest="2" * 64,
            input_snapshot={"execution_target": "OKX_DEMO"},
            prepared_at=NOW,
        )
    )
    db.commit()

    with pytest.raises(FullChainBlocked, match="missing database IDs"):
        repository.finalize_reconciliation(
            chain.id,
            job.lease_token,
            reconciliation_run_id=reconciliation.id,
            now=NOW,
        )
    db.refresh(chain)
    assert chain.status == "RUNNING"
    assert chain.reconciliation_run_id is None


def authoritative_reconciliation() -> ReconciliationRun:
    database_ids = {
        "reconciliation_run": [99],
        "exchange_events": [101, 102, 103, 104],
        "order_snapshots": [201],
        "fill_snapshots": [301],
        "position_snapshots": [],
        "account_snapshots": [401],
        "repaired_exchange_orders": [],
        "recovery_batches": [501],
        "reconciliation_state": [601],
        "recovery_grants": [],
    }
    observed_at = NOW - timedelta(seconds=1)
    return ReconciliationRun(
        id=99,
        execution_target_id="OKX_DEMO",
        status="RECONCILED",
        summary_snapshot={
            "execution_target": "OKX_DEMO",
            "status": "RECONCILED",
            "source_type": "api_aggregate",
            "core_data": True,
            "database_ids": database_ids,
            "authoritative_observed_at": observed_at.isoformat(),
        },
        database_ids=database_ids,
        artifact_path="reports/okx_demo_reconciliation/okx-demo-reconciliation-99.json",
        artifact_sha256="a" * 64,
        artifact_status="READY",
        authoritative_observed_at=observed_at,
        source_type="api_aggregate",
        core_data=True,
        started_at=observed_at,
        completed_at=NOW,
    )


def test_only_finalized_authoritative_reconciliation_is_acceptance_ready() -> None:
    reconciliation = authoritative_reconciliation()
    assert require_authoritative_reconciliation(reconciliation) == reconciliation.database_ids

    reconciliation.artifact_status = "PENDING"
    with pytest.raises(FullChainBlocked, match="not finalized"):
        require_authoritative_reconciliation(reconciliation)

    reconciliation.artifact_status = "READY"
    reconciliation.database_ids = {
        **reconciliation.database_ids,
        "fill_snapshots": [],
    }
    reconciliation.summary_snapshot = {
        **reconciliation.summary_snapshot,
        "database_ids": reconciliation.database_ids,
    }
    with pytest.raises(FullChainBlocked, match="empty.*fill_snapshots"):
        require_authoritative_reconciliation(reconciliation)
