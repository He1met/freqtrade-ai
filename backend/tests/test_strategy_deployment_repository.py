from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db.session import create_database_engine, create_session_factory
from app.models import (
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    FullChainRun,
    FullChainSignalSnapshot,
    ResearchJobAttempt,
    Strategy,
    StrategyCandidateApproval,
    StrategyScore,
    StrategyVersion,
)
from app.repositories import (
    ResearchJobRepository,
    StrategyDeploymentBlocked,
    StrategyDeploymentConflict,
    StrategyDeploymentRepository,
    ensure_execution_scope_catalog,
)


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
POLICY_DIGEST = "d" * 64


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'strategy-deployment.sqlite'}"
    )
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    try:
        with factory() as db:
            ensure_execution_scope_catalog(db)
            db.commit()
        yield factory
    finally:
        engine.dispose()


def seed_approved_candidate(db, *, expires_at: datetime | None = None):
    strategy = Strategy(
        name="DeploymentStrategy",
        slug="deployment-strategy",
        source="ai_generated",
    )
    db.add(strategy)
    db.flush()
    version = StrategyVersion(
        strategy_id=strategy.id,
        version_number=1,
        blueprint={"schema_version": "2", "strategy": {"name": strategy.name}},
        generated_code="class DeploymentStrategy: pass",
        code_hash="c" * 64,
        file_path="user_data/strategies/DeploymentStrategy.py",
        validation_status="passed",
    )
    db.add(version)
    db.flush()
    backtest_run = BacktestRun(
        execution_scope_id="LOCAL_DRY_RUN",
        strategy_version_id=version.id,
        profile_name="deployment-test",
        status="succeeded",
        requested_task_count=1,
    )
    db.add(backtest_run)
    db.flush()
    backtest_task = BacktestTask(
        backtest_run_id=backtest_run.id,
        pair="BTC/USDT:USDT",
        timeframe="5m",
        status="succeeded",
        result_path="reports/backtests/deployment.json",
    )
    db.add(backtest_task)
    db.flush()
    backtest_result = BacktestResult(
        backtest_run_id=backtest_run.id,
        backtest_task_id=backtest_task.id,
        result_path="reports/backtests/deployment.json",
        metrics_snapshot={"acceptance_ready": True},
        profit_pct=0.05,
        max_drawdown_pct=0.1,
        total_trades=30,
    )
    db.add(backtest_result)
    db.flush()
    score = StrategyScore(
        strategy_id=strategy.id,
        strategy_version_id=version.id,
        backtest_result_id=backtest_result.id,
        scoring_version="phase2-quality-v1",
        total_score=82,
        metrics_snapshot={"acceptance_ready": True},
    )
    db.add(score)
    db.commit()

    jobs = ResearchJobRepository(db)
    job = jobs.create(
        job_type="deepseek_backtest",
        operation="strategy_generation.deepseek_backtest_loop",
        idempotency_key_digest="a" * 64,
        request_hash="b" * 64,
        request_payload={"allow_real_call": True},
    )
    claimed = jobs.claim_next(
        owner="deployment-seed",
        lease_seconds=300,
        now=NOW,
    )
    assert claimed is not None and claimed.lease_token
    attempt = db.query(ResearchJobAttempt).filter_by(research_job_id=job.id).one()
    chain = FullChainRun(
        research_job_id=job.id,
        research_job_attempt_id=attempt.id,
        research_scope_id="LOCAL_DRY_RUN",
        execution_target_id="OKX_DEMO",
        status="APPROVED",
        current_stage="SIGNAL",
        strategy_id=strategy.id,
        strategy_version_id=version.id,
        backtest_run_id=backtest_run.id,
        backtest_task_id=backtest_task.id,
        backtest_result_id=backtest_result.id,
        strategy_score_id=score.id,
    )
    db.add(chain)
    db.flush()
    approval = StrategyCandidateApproval(
        full_chain_run_id=chain.id,
        execution_target_id="OKX_DEMO",
        strategy_version_id=version.id,
        backtest_result_id=backtest_result.id,
        strategy_score_id=score.id,
        candidate_digest="e" * 64,
        promotion_policy_version="phase2-quality-v1",
        promotion_evidence={"acceptance_ready": True},
        status="APPROVED",
        requested_by="system:test",
        decided_by="system:test",
        decision_reason="Automatic policy gates passed.",
        requested_at=NOW,
        decided_at=NOW,
        expires_at=expires_at or NOW + timedelta(minutes=5),
    )
    db.add(approval)
    db.flush()
    chain.candidate_approval_id = approval.id
    db.commit()
    return approval


def publish(db, approval_id: int):
    return StrategyDeploymentRepository(db).publish(
        candidate_approval_id=approval_id,
        instrument_id="BTC-USDT-SWAP",
        timeframe="5m",
        deployment_policy_digest=POLICY_DIGEST,
        now=NOW,
    )


def test_publish_is_idempotent_and_persists_exact_candidate_binding(
    session_factory,
) -> None:
    with session_factory() as db:
        approval = seed_approved_candidate(db)
        first = publish(db, approval.id)
        replay = publish(db, approval.id)
        deployment_id = first.id

        assert replay.id == deployment_id
        assert first.execution_target_id == "OKX_DEMO"
        assert first.candidate_approval_id == approval.id
        assert first.strategy_version_id == approval.strategy_version_id
        assert first.candidate_digest == approval.candidate_digest
        assert first.promotion_policy_version == approval.promotion_policy_version
        assert first.deployment_policy_digest == POLICY_DIGEST
        assert first.evidence_snapshot["manual_confirmation_required"] is False
        assert first.evidence_snapshot["allow_real_funds"] is False

        with pytest.raises(StrategyDeploymentConflict, match="different deployment"):
            StrategyDeploymentRepository(db).publish(
                candidate_approval_id=approval.id,
                instrument_id="ETH-USDT-SWAP",
                timeframe="5m",
                deployment_policy_digest=POLICY_DIGEST,
                now=NOW,
            )

    with session_factory() as restarted_db:
        persisted = StrategyDeploymentRepository(restarted_db).get_deployment(
            deployment_id
        )
        assert persisted is not None
        assert persisted.status == "ACTIVE"


def test_single_consumer_and_no_action_do_not_create_full_chain_signal(
    session_factory,
) -> None:
    with session_factory() as db:
        approval = seed_approved_candidate(db)
        deployment = publish(db, approval.id)
        repository = StrategyDeploymentRepository(db)
        first = repository.enqueue_evaluation(
            deployment.id,
            closed_candle_at=NOW,
        )
        replay = repository.enqueue_evaluation(
            deployment.id,
            closed_candle_at=NOW,
        )
        second = repository.enqueue_evaluation(
            deployment.id,
            closed_candle_at=NOW + timedelta(minutes=5),
        )
        assert replay.id == first.id

        claimed = repository.claim_next(
            owner="okx-runtime-one",
            lease_seconds=60,
            now=NOW + timedelta(seconds=1),
        )
        assert claimed is not None
        assert claimed.id == first.id
        assert claimed.lease_token
        assert claimed.fencing_sequence == 1
        assert (
            repository.claim_next(
                owner="okx-runtime-two",
                lease_seconds=60,
                now=NOW + timedelta(seconds=2),
            )
            is None
        )

        completed = repository.complete(
            claimed.id,
            lease_token=claimed.lease_token,
            fencing_sequence=claimed.fencing_sequence,
            status="NO_ACTION",
            input_digest="f" * 64,
            result_snapshot={"status": "NO_ACTION", "evaluator_version": "1"},
            now=NOW + timedelta(seconds=3),
        )
        assert completed is not None and completed.status == "NO_ACTION"
        assert db.query(FullChainSignalSnapshot).count() == 0

        next_claim = repository.claim_next(
            owner="okx-runtime-one",
            lease_seconds=60,
            now=NOW + timedelta(seconds=4),
        )
        assert next_claim is not None and next_claim.id == second.id

        with pytest.raises(StrategyDeploymentBlocked, match="NO_ACTION"):
            repository.complete(
                next_claim.id,
                lease_token=next_claim.lease_token,
                fencing_sequence=next_claim.fencing_sequence,
                status="NO_ACTION",
                input_digest="f" * 64,
                result_snapshot={"signal_snapshot_id": 123},
                now=NOW + timedelta(seconds=5),
            )


def test_expired_lease_recovers_with_new_fence_and_rejects_stale_owner(
    session_factory,
) -> None:
    with session_factory() as db:
        approval = seed_approved_candidate(db)
        deployment = publish(db, approval.id)
        repository = StrategyDeploymentRepository(db)
        evaluation = repository.enqueue_evaluation(
            deployment.id,
            closed_candle_at=NOW,
        )
        first = repository.claim_next(
            owner="runtime-before-restart",
            lease_seconds=10,
            now=NOW + timedelta(seconds=1),
        )
        assert first is not None and first.id == evaluation.id and first.lease_token
        first_token = first.lease_token
        first_fence = first.fencing_sequence

    with session_factory() as restarted_db:
        repository = StrategyDeploymentRepository(restarted_db)
        assert repository.expire_stale(now=NOW + timedelta(seconds=12)) == 1
        recovered = repository.claim_next(
            owner="runtime-after-restart",
            lease_seconds=60,
            now=NOW + timedelta(seconds=13),
        )
        assert recovered is not None and recovered.lease_token
        assert recovered.lease_token != first_token
        assert recovered.fencing_sequence == first_fence + 1
        assert (
            repository.heartbeat(
                recovered.id,
                lease_token=first_token,
                fencing_sequence=first_fence,
                lease_seconds=60,
                now=NOW + timedelta(seconds=14),
            )
            is False
        )
        assert (
            repository.complete(
                recovered.id,
                lease_token=first_token,
                fencing_sequence=first_fence,
                status="ACTIONABLE",
                input_digest="f" * 64,
                result_snapshot={"status": "ACTIONABLE"},
                now=NOW + timedelta(seconds=14),
            )
            is None
        )
        completed = repository.complete(
            recovered.id,
            lease_token=recovered.lease_token,
            fencing_sequence=recovered.fencing_sequence,
            status="ACTIONABLE",
            input_digest="f" * 64,
            result_snapshot={"status": "ACTIONABLE"},
            now=NOW + timedelta(seconds=15),
        )
        assert completed is not None and completed.status == "ACTIONABLE"


def test_disable_fences_pending_and_leased_evaluations_across_restart(
    session_factory,
) -> None:
    with session_factory() as db:
        approval = seed_approved_candidate(db)
        deployment = publish(db, approval.id)
        repository = StrategyDeploymentRepository(db)
        leased = repository.enqueue_evaluation(
            deployment.id,
            closed_candle_at=NOW,
        )
        pending = repository.enqueue_evaluation(
            deployment.id,
            closed_candle_at=NOW + timedelta(minutes=5),
        )
        claimed = repository.claim_next(
            owner="runtime-before-disable",
            lease_seconds=60,
            now=NOW + timedelta(seconds=1),
        )
        assert claimed is not None and claimed.id == leased.id and claimed.lease_token
        old_token = claimed.lease_token
        old_fence = claimed.fencing_sequence

        disabled = repository.disable(
            deployment.id,
            reason="Policy version retired.",
            now=NOW + timedelta(seconds=2),
        )
        assert disabled is not None and disabled.status == "DISABLED"
        assert repository.get_evaluation(leased.id).status == "BLOCKED"
        assert repository.get_evaluation(pending.id).status == "BLOCKED"
        assert (
            repository.complete(
                leased.id,
                lease_token=old_token,
                fencing_sequence=old_fence,
                status="ACTIONABLE",
                input_digest="f" * 64,
                result_snapshot={"status": "ACTIONABLE"},
                now=NOW + timedelta(seconds=3),
            )
            is None
        )
        with pytest.raises(StrategyDeploymentBlocked, match="disabled"):
            repository.enqueue_evaluation(
                deployment.id,
                closed_candle_at=NOW + timedelta(minutes=10),
            )

    with session_factory() as restarted_db:
        repository = StrategyDeploymentRepository(restarted_db)
        persisted = repository.get_deployment(deployment.id)
        assert persisted is not None and persisted.status == "DISABLED"
        assert (
            repository.claim_next(
                owner="runtime-after-disable",
                lease_seconds=60,
                now=NOW + timedelta(minutes=20),
            )
            is None
        )


def test_publish_requires_current_approved_candidate(session_factory) -> None:
    with session_factory() as db:
        expired = seed_approved_candidate(
            db,
            expires_at=NOW - timedelta(seconds=1),
        )
        with pytest.raises(StrategyDeploymentBlocked, match="expired"):
            publish(db, expired.id)
