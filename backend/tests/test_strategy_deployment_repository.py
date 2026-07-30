from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
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
    StrategyDeployment,
    StrategyScore,
    StrategyValidationPlan,
    StrategyVersion,
)
from app.repositories import (
    ResearchJobRepository,
    StrategyDeploymentBlocked,
    StrategyDeploymentConflict,
    StrategyDeploymentRepository,
    ensure_execution_scope_catalog,
)
from app.repositories.full_chain import (
    FullChainBlocked,
    FullChainConflict,
    FullChainRepository,
)
from app.services.strategy_promotion import promotion_candidate_digest


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


def seed_approved_candidate(
    db,
    *,
    expires_at: datetime | None = None,
    suffix: str = "",
):
    slug_suffix = f"-{suffix}" if suffix else ""
    strategy = Strategy(
        name=f"DeploymentStrategy{suffix}",
        slug=f"deployment-strategy{slug_suffix}",
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
        file_path=f"user_data/strategies/DeploymentStrategy{suffix}.py",
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
        result_path=f"reports/backtests/deployment{slug_suffix}.json",
    )
    db.add(backtest_task)
    db.flush()
    backtest_result = BacktestResult(
        backtest_run_id=backtest_run.id,
        backtest_task_id=backtest_task.id,
        result_path=f"reports/backtests/deployment{slug_suffix}.json",
        metrics_snapshot={
            "acceptance_ready": True,
            "promotion_evidence": {
                "net_of_costs": True,
                "out_of_sample": {
                    "passed": True,
                    "profit_pct": 0.04,
                    "total_trades": 30,
                },
                "walk_forward": {
                    "passed": True,
                    "market_states": ["bull", "bear", "range"],
                },
            },
        },
        profit_pct=0.05,
        max_drawdown_pct=0.1,
        total_trades=30,
    )
    db.add(backtest_result)
    db.flush()
    validation_plan = StrategyValidationPlan(
        strategy_version_id=version.id,
        promotion_backtest_result_id=backtest_result.id,
        provider_name="freqtrade",
        strategy_code_digest=version.code_hash,
        plan_digest=hashlib.sha256(f"plan{suffix}".encode()).hexdigest(),
        plan_snapshot={"test_scope": "deployment mechanics"},
        status="PASSED",
        evidence_digest=hashlib.sha256(f"evidence{suffix}".encode()).hexdigest(),
        promotion_evidence={
            "window_result_ids": [
                backtest_result.id * 10 + index for index in range(1, 5)
            ]
        },
    )
    db.add(validation_plan)
    db.flush()
    backtest_result.metrics_snapshot = {
        **backtest_result.metrics_snapshot,
        "promotion_evidence": {
            **backtest_result.metrics_snapshot["promotion_evidence"],
            "validation_matrix": {
                "plan_id": validation_plan.id,
                "plan_digest": validation_plan.plan_digest,
                "evidence_digest": validation_plan.evidence_digest,
                "window_result_ids": validation_plan.promotion_evidence[
                    "window_result_ids"
                ],
                "provider": "freqtrade",
            },
        },
    }
    score = StrategyScore(
        strategy_id=strategy.id,
        strategy_version_id=version.id,
        backtest_result_id=backtest_result.id,
        scoring_version="phase2-quality-v1",
        total_score=82,
        metrics_snapshot={
            "source": "backtest_result",
            "backtest_result_id": backtest_result.id,
            "acceptance_ready": True,
        },
    )
    db.add(score)
    db.commit()

    jobs = ResearchJobRepository(db)
    job = jobs.create(
        job_type="deepseek_backtest",
        operation="strategy_generation.deepseek_backtest_loop",
        idempotency_key_digest=hashlib.sha256(
            f"deployment-seed{suffix}".encode()
        ).hexdigest(),
        request_hash=hashlib.sha256(
            f"deployment-request{suffix}".encode()
        ).hexdigest(),
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
    promotion_evidence, candidate_digest = promotion_candidate_digest(
        backtest_result,
        score,
        version,
    )
    approval = StrategyCandidateApproval(
        full_chain_run_id=chain.id,
        execution_target_id="OKX_DEMO",
        strategy_version_id=version.id,
        backtest_result_id=backtest_result.id,
        strategy_score_id=score.id,
        candidate_digest=candidate_digest,
        promotion_policy_version=promotion_evidence["policy"]["policy_version"],
        promotion_evidence=promotion_evidence,
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


def test_publish_blocks_a_second_active_okx_demo_deployment(
    session_factory,
) -> None:
    with session_factory() as db:
        first_approval = seed_approved_candidate(db, suffix="One")
        first = publish(db, first_approval.id)
        jobs = ResearchJobRepository(db)
        first_chain = db.get(FullChainRun, first_approval.full_chain_run_id)
        assert first_chain is not None
        active_job = jobs.get(first_chain.research_job_id)
        assert active_job is not None and active_job.lease_token
        jobs.complete(
            active_job.id,
            active_job.lease_token,
            status="SUCCESS",
            stage="DEPLOYED",
            links={},
            evidence_snapshot={"status": "SUCCESS"},
            error_message=None,
            provider_completed=False,
            now=NOW,
        )

        second_approval = seed_approved_candidate(db, suffix="Two")
        with pytest.raises(
            StrategyDeploymentConflict,
            match="already has a different ACTIVE",
        ):
            publish(db, second_approval.id)

        persisted = db.query(StrategyDeployment).all()
        assert [deployment.id for deployment in persisted] == [first.id]


def test_leased_signal_checkpoint_survives_expiry_and_is_immutable(
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
            owner="first-runtime",
            lease_seconds=2,
            now=NOW + timedelta(seconds=1),
        )
        assert first is not None and first.lease_token
        first_lease_token = first.lease_token
        first_fencing_sequence = first.fencing_sequence
        checkpoint = {
            "checkpoint_schema": "SIGNAL_EVALUATION_V1",
            "evaluation_id": evaluation.id,
            "bundle": {"market": "trusted:1"},
            "signal": {"decision": "ACTIONABLE"},
        }
        repository.checkpoint_leased(
            evaluation.id,
            lease_token=first_lease_token,
            fencing_sequence=first_fencing_sequence,
            input_digest="e" * 64,
            result_snapshot=checkpoint,
            now=NOW + timedelta(seconds=2),
        )

        assert repository.expire_stale(now=NOW + timedelta(seconds=4)) == 1
        second = repository.claim_next(
            owner="recovery-runtime",
            lease_seconds=30,
            now=NOW + timedelta(seconds=4),
        )
        assert second is not None and second.lease_token
        assert second.lease_token != first_lease_token
        assert second.fencing_sequence == first_fencing_sequence + 1
        assert second.input_digest == "e" * 64
        assert second.result_snapshot == checkpoint

        repository.checkpoint_leased(
            evaluation.id,
            lease_token=second.lease_token,
            fencing_sequence=second.fencing_sequence,
            input_digest="e" * 64,
            result_snapshot=checkpoint,
            now=NOW + timedelta(seconds=5),
        )
        with pytest.raises(
            StrategyDeploymentConflict,
            match="checkpoint is immutable",
        ):
            repository.checkpoint_leased(
                evaluation.id,
                lease_token=second.lease_token,
                fencing_sequence=second.fencing_sequence,
                input_digest="f" * 64,
                result_snapshot=checkpoint,
                now=NOW + timedelta(seconds=5),
            )


def test_actionable_evaluation_opens_one_fenced_execution_chain(
    session_factory,
) -> None:
    with session_factory() as db:
        source_approval = seed_approved_candidate(db)
        deployment = publish(db, source_approval.id)
        evaluations = StrategyDeploymentRepository(db)
        evaluation = evaluations.enqueue_evaluation(
            deployment.id,
            closed_candle_at=NOW,
        )
        claimed = evaluations.claim_next(
            owner="okx-runtime",
            lease_seconds=60,
            now=NOW + timedelta(seconds=1),
        )
        assert claimed is not None and claimed.lease_token

        chains = FullChainRepository(db)
        first = chains.open_for_signal_evaluation(
            evaluation.id,
            claimed.lease_token,
            claimed.fencing_sequence,
            now=NOW + timedelta(seconds=2),
        )
        replay = chains.open_for_signal_evaluation(
            evaluation.id,
            claimed.lease_token,
            claimed.fencing_sequence,
            now=NOW + timedelta(seconds=2),
        )
        assert replay.id == first.id
        assert first.run_kind == "EXECUTION"
        assert first.signal_evaluation_id == evaluation.id
        source_chain = db.get(FullChainRun, source_approval.full_chain_run_id)
        assert source_chain is not None
        assert first.research_job_id == source_chain.research_job_id
        assert first.candidate_approval_id != source_approval.id
        assert db.query(FullChainRun).filter_by(run_kind="EXECUTION").count() == 1

        prepared = chains.prepare_execution_stage(
            first.id,
            "SIGNAL",
            evaluation_id=evaluation.id,
            lease_token=claimed.lease_token,
            fencing_sequence=claimed.fencing_sequence,
            idempotency_key="evaluation-signal",
            input_snapshot={"evaluation_id": evaluation.id},
            now=NOW + timedelta(seconds=3),
        )
        resumed = chains.prepare_execution_stage(
            first.id,
            "SIGNAL",
            evaluation_id=evaluation.id,
            lease_token=claimed.lease_token,
            fencing_sequence=claimed.fencing_sequence,
            idempotency_key="evaluation-signal",
            input_snapshot={"evaluation_id": evaluation.id},
            now=NOW + timedelta(seconds=3),
        )
        assert resumed.id == prepared.id
        assert resumed.status == "PREPARED"
        signal = chains.record_execution_signal(
            first.id,
            evaluation_id=evaluation.id,
            lease_token=claimed.lease_token,
            fencing_sequence=claimed.fencing_sequence,
            instrument_id="BTC-USDT-SWAP",
            source_type="database",
            source_database_ids={
                "instrument_snapshot_id": 1,
                "market_snapshot_id": 2,
                "account_snapshot_id": 3,
            },
            signal_snapshot={
                "decision": "ACTIONABLE",
                "enter_long": True,
                "enter_short": False,
            },
            observed_at=NOW + timedelta(seconds=3),
            expires_at=NOW + timedelta(seconds=30),
        )
        completed_signal = chains.complete_execution_stage(
            first.id,
            "SIGNAL",
            evaluation_id=evaluation.id,
            lease_token=claimed.lease_token,
            fencing_sequence=claimed.fencing_sequence,
            database_ids={"signal_snapshot_id": signal.id},
            output_snapshot={
                "status": "ACTIONABLE",
                "signal_digest": signal.signal_digest,
            },
            now=NOW + timedelta(seconds=4),
        )
        replayed_signal = chains.complete_execution_stage(
            first.id,
            "SIGNAL",
            evaluation_id=evaluation.id,
            lease_token=claimed.lease_token,
            fencing_sequence=claimed.fencing_sequence,
            database_ids={"signal_snapshot_id": signal.id},
            output_snapshot={
                "status": "ACTIONABLE",
                "signal_digest": signal.signal_digest,
            },
            now=NOW + timedelta(seconds=5),
        )
        assert completed_signal.status == "SUCCESS"
        assert replayed_signal.id == completed_signal.id
        assert first.current_stage == "RISK"
        with pytest.raises(FullChainConflict, match="different idempotency"):
            chains.prepare_execution_stage(
                first.id,
                "SIGNAL",
                evaluation_id=evaluation.id,
                lease_token=claimed.lease_token,
                fencing_sequence=claimed.fencing_sequence,
                idempotency_key="different",
                input_snapshot={"evaluation_id": evaluation.id},
                now=NOW + timedelta(seconds=3),
            )

    with session_factory() as restarted_db:
        with pytest.raises(FullChainBlocked, match="absent, stale, or fenced"):
            FullChainRepository(restarted_db).open_for_signal_evaluation(
                evaluation.id,
                claimed.lease_token,
                claimed.fencing_sequence + 1,
                now=NOW + timedelta(seconds=4),
            )


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
