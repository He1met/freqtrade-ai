from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.session import create_database_engine, create_session_factory
from app.models import (
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    FullChainRun,
    FullChainStageRun,
    ResearchJobAttempt,
    Strategy,
    StrategyCandidateApproval,
    StrategyDeployment,
    StrategyGenerationRun,
    StrategyScore,
    StrategyVersion,
)
from app.repositories import ResearchJobRepository, ensure_execution_scope_catalog
from app.services.research_job_queue import ResearchJobQueueService
from app.services.strategy_deployment_continuation import (
    StrategyDeploymentContinuation,
    default_strategy_deployment_continuation_factory,
)
from app.schemas.deepseek_backtest_loop import DeepSeekBacktestLoopRequest
from app.workers.deepseek_backtest_worker import DeepSeekBacktestWorker


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'deployment-continuation.sqlite'}"
    )
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as db:
        ensure_execution_scope_catalog(db)
        db.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def _blueprint(*, timeframe: str = "5m") -> dict:
    return {
        "schema_version": "2",
        "name": "Continuation Strategy",
        "slug": "continuation-strategy",
        "class_name": "ContinuationStrategy",
        "timeframe": timeframe,
        "indicators": [{"name": "rsi", "kind": "rsi", "period": 14}],
        "entry_rules": [{"indicator": "rsi", "operator": "<", "value": 30}],
        "exit_rules": [{"indicator": "rsi", "operator": ">", "value": 70}],
    }


def seed_resumed_candidate(
    factory,
    *,
    pair: str = "BTC/USDT:USDT",
    request_timeframe: str = "5m",
    task_timeframe: str = "5m",
    blueprint_timeframe: str = "5m",
    approval_expires_at: datetime = NOW + timedelta(hours=1),
    policy_version: str = "phase2-quality-v1",
) -> tuple[int, int]:
    with factory() as db:
        generation = StrategyGenerationRun(
            execution_scope_id="LOCAL_DRY_RUN",
            provider="deepseek",
            model="deepseek-test",
            status="succeeded",
            requested_count=1,
            generated_count=1,
            accepted_count=1,
        )
        strategy = Strategy(
            name="Continuation Strategy",
            slug="continuation-strategy",
            source="ai_generated",
        )
        db.add_all([generation, strategy])
        db.flush()
        version = StrategyVersion(
            strategy_id=strategy.id,
            generation_run_id=generation.id,
            version_number=1,
            blueprint=_blueprint(timeframe=blueprint_timeframe),
            generated_code="class ContinuationStrategy: pass",
            code_hash="c" * 64,
            file_path="user_data/strategies/ContinuationStrategy.py",
            validation_status="passed",
        )
        db.add(version)
        db.flush()
        backtest_run = BacktestRun(
            execution_scope_id="LOCAL_DRY_RUN",
            strategy_version_id=version.id,
            profile_name="continuation",
            status="succeeded",
            requested_task_count=1,
        )
        db.add(backtest_run)
        db.flush()
        task = BacktestTask(
            backtest_run_id=backtest_run.id,
            pair=pair,
            timeframe=task_timeframe,
            status="succeeded",
            result_path="reports/backtests/continuation.json",
        )
        db.add(task)
        db.flush()
        result = BacktestResult(
            backtest_run_id=backtest_run.id,
            backtest_task_id=task.id,
            result_path="reports/backtests/continuation.json",
            metrics_snapshot={"acceptance_ready": True},
            profit_pct=0.05,
            max_drawdown_pct=0.1,
            total_trades=30,
        )
        db.add(result)
        db.flush()
        score = StrategyScore(
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            backtest_result_id=result.id,
            scoring_version="phase2-quality-v1",
            total_score=82,
            metrics_snapshot={"acceptance_ready": True},
        )
        db.add(score)
        db.commit()

        request = DeepSeekBacktestLoopRequest(
            prompt_summary="Generate and validate one candidate.",
            allow_real_call=True,
            backtest_profile={
                "pair": pair,
                "timeframe": request_timeframe,
            },
        )
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request,
            idempotency_key=f"deployment-continuation-{pair}",
        ).id
        jobs = ResearchJobRepository(db)
        job = jobs.claim_next(owner="continuation-seed", lease_seconds=3600, now=NOW)
        assert job is not None and job.lease_token
        job.stage = "SIGNAL"
        db.flush()

        attempt = db.scalar(
            select(ResearchJobAttempt).where(
                ResearchJobAttempt.research_job_id == job.id,
                ResearchJobAttempt.attempt_number == job.attempt_count,
            )
        )
        assert attempt is not None
        chain = FullChainRun(
            research_job_id=job.id,
            research_job_attempt_id=attempt.id,
            run_kind="RESEARCH",
            research_scope_id="LOCAL_DRY_RUN",
            execution_target_id="OKX_DEMO",
            status="APPROVED",
            current_stage="SIGNAL",
            strategy_generation_run_id=generation.id,
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            backtest_run_id=backtest_run.id,
            backtest_task_id=task.id,
            backtest_result_id=result.id,
            strategy_score_id=score.id,
            started_at=NOW,
        )
        db.add(chain)
        db.flush()
        approval = StrategyCandidateApproval(
            full_chain_run_id=chain.id,
            execution_target_id="OKX_DEMO",
            strategy_version_id=version.id,
            backtest_result_id=result.id,
            strategy_score_id=score.id,
            candidate_digest="a" * 64,
            promotion_policy_version=policy_version,
            promotion_evidence={
                "policy": {
                    "policy_version": policy_version,
                    "minimum_score": 70,
                }
            },
            status="APPROVED",
            requested_by="system:okx-demo-auto-policy-v1",
            decided_by="system:okx-demo-auto-policy-v1",
            decision_reason="Deterministic gates passed.",
            requested_at=NOW,
            decided_at=NOW,
            expires_at=approval_expires_at,
        )
        db.add(approval)
        db.flush()
        chain.candidate_approval_id = approval.id
        checkpoint = FullChainStageRun(
            full_chain_run_id=chain.id,
            stage="CANDIDATE_APPROVAL",
            status="SUCCESS",
            idempotency_key_digest="d" * 64,
            input_digest="e" * 64,
            input_snapshot={"candidate_digest": approval.candidate_digest},
            output_snapshot={"status": "APPROVED"},
            database_ids={"candidate_approval_id": approval.id},
            prepared_at=NOW,
            completed_at=NOW,
        )
        db.add(checkpoint)
        job.evidence_snapshot = {
            "status": "PENDING",
            "full_chain_run_id": chain.id,
            "candidate_approval_id": approval.id,
        }
        db.commit()
        waiting = jobs.wait_for_candidate_approval(
            job.id,
            job.lease_token,
            evidence_snapshot={
                "status": "AWAITING_APPROVAL",
                "full_chain_run_id": chain.id,
                "candidate_approval_id": approval.id,
            },
            now=NOW,
        )
        assert waiting is not None
        resumed = jobs.resume_after_candidate_approval(
            job.id,
            evidence_snapshot={
                "status": "PENDING",
                "full_chain_run_id": chain.id,
                "candidate_approval_id": approval.id,
            },
        )
        assert resumed is not None
        return job_id, approval.id


def continuation_factory(db):
    return StrategyDeploymentContinuation(db, clock=lambda: NOW)


def test_worker_defaults_to_durable_strategy_deployment_continuation() -> None:
    worker = DeepSeekBacktestWorker()
    assert (
        worker.continuation_factory
        is default_strategy_deployment_continuation_factory
    )


def test_approved_candidate_is_published_and_job_completes_without_research(
    session_factory,
) -> None:
    job_id, approval_id = seed_resumed_candidate(session_factory)
    research_calls: list[bool] = []
    worker = DeepSeekBacktestWorker(
        session_factory=session_factory,
        service_factory=lambda db: research_calls.append(True),
        continuation_factory=continuation_factory,
        owner="continuation-worker",
        lease_seconds=3600,
    )

    assert worker.run_once() == job_id
    assert research_calls == []
    with session_factory() as db:
        job = ResearchJobRepository(db).get(job_id)
        deployment = db.scalar(
            select(StrategyDeployment).where(
                StrategyDeployment.candidate_approval_id == approval_id
            )
        )
        assert job is not None
        assert job.status == "SUCCESS"
        assert job.stage == "DEPLOYED"
        assert job.lease_token is None
        assert job.evidence_snapshot["strategy_deployment_id"] == deployment.id
        assert job.evidence_snapshot["research_repeated"] is False
        assert deployment.instrument_id == "BTC-USDT-SWAP"
        assert deployment.timeframe == "5m"


@pytest.mark.parametrize(
    ("pair", "instrument"),
    [
        ("ETH/USDT:USDT", "ETH-USDT-SWAP"),
        ("SOL/USDT:USDT", "SOL-USDT-SWAP"),
    ],
)
def test_multi_asset_candidate_preserves_pair_to_instrument_binding(
    session_factory, pair, instrument
) -> None:
    job_id, approval_id = seed_resumed_candidate(session_factory, pair=pair)
    worker = DeepSeekBacktestWorker(
        session_factory=session_factory,
        continuation_factory=continuation_factory,
        owner="multi-asset-continuation-worker",
        lease_seconds=3600,
    )

    assert worker.run_once() == job_id
    with session_factory() as db:
        deployment = db.scalar(
            select(StrategyDeployment).where(
                StrategyDeployment.candidate_approval_id == approval_id
            )
        )
        assert deployment is not None
        assert deployment.instrument_id == instrument
        assert deployment.timeframe == "5m"


def test_existing_identical_deployment_is_reused_after_restart(
    session_factory,
) -> None:
    job_id, approval_id = seed_resumed_candidate(session_factory)
    with session_factory() as db:
        # Simulate the recoverable publish/terminal-checkpoint crash window.
        from app.repositories.strategy_deployments import StrategyDeploymentRepository

        approval = db.get(StrategyCandidateApproval, approval_id)
        policy_digest = __import__(
            "app.services.strategy_deployment_continuation",
            fromlist=["_stable_digest"],
        )._stable_digest(
            {
                "schema_version": "1",
                "execution_target_id": "OKX_DEMO",
                "instrument_id": "BTC-USDT-SWAP",
                "timeframe": "5m",
                "candidate_digest": approval.candidate_digest,
                "approval_policy_evidence": {"status": "APPROVED"},
                "promotion_policy": approval.promotion_evidence["policy"],
                "promotion_policy_version": approval.promotion_policy_version,
            }
        )
        deployment = StrategyDeploymentRepository(db).publish(
            candidate_approval_id=approval_id,
            instrument_id="BTC-USDT-SWAP",
            timeframe="5m",
            deployment_policy_digest=policy_digest,
            now=NOW,
        )
        deployment_id = deployment.id

    worker = DeepSeekBacktestWorker(
        session_factory=session_factory,
        service_factory=lambda db: (_ for _ in ()).throw(
            AssertionError("research must not repeat")
        ),
        continuation_factory=continuation_factory,
        owner="restart-worker",
        lease_seconds=3600,
    )
    assert worker.run_once() == job_id
    with session_factory() as db:
        deployments = list(db.scalars(select(StrategyDeployment)).all())
        assert [item.id for item in deployments] == [deployment_id]
        assert ResearchJobRepository(db).get(job_id).stage == "DEPLOYED"


@pytest.mark.parametrize(
    ("seed_kwargs", "reason"),
    [
        ({"request_timeframe": "15m"}, "lineage are inconsistent"),
        ({"blueprint_timeframe": "15m"}, "timeframe are inconsistent"),
        (
            {"approval_expires_at": NOW - timedelta(seconds=1)},
            "approval is missing, expired, or inconsistent",
        ),
    ],
)
def test_missing_expired_or_inconsistent_evidence_blocks_without_deployment(
    session_factory,
    seed_kwargs,
    reason,
) -> None:
    job_id, _ = seed_resumed_candidate(session_factory, **seed_kwargs)
    worker = DeepSeekBacktestWorker(
        session_factory=session_factory,
        service_factory=lambda db: (_ for _ in ()).throw(
            AssertionError("research must not repeat")
        ),
        continuation_factory=continuation_factory,
        owner="blocked-continuation-worker",
        lease_seconds=3600,
    )
    assert worker.run_once() == job_id
    with session_factory() as db:
        job = ResearchJobRepository(db).get(job_id)
        assert job.status == "BLOCKED"
        assert job.stage == "SIGNAL"
        assert reason in (job.error_message or "")
        assert db.scalar(select(StrategyDeployment)) is None
