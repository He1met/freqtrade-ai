from __future__ import annotations

from sqlalchemy import func, select

import pytest

from app.canonical_v13.models import (
    DEPLOYMENTS_TABLE,
    OPTIMIZATION_RUNS_TABLE,
    OPTIMIZATION_TRIALS_TABLE,
    ORDERS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
)
from app.canonical_v13.optimization import (
    CanonicalOptimizationBlocked,
    create_optimization_run,
    link_controlled_submission_version,
    record_isolated_optimization_trial,
)
from app.canonical_v13.intake import controlled_submit_latest
from tests.test_canonical_v13_intake import _snapshot
from app.canonical_v13.research_evaluation import qualify_target, score_target
from tests.test_canonical_v13_research_evaluation import (
    _passing_metrics,
    _validated_attempt,
    canonical_connection,
)


def _count(connection, table) -> int:
    return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def test_optimization_is_zero_write_before_qualified_baseline(canonical_connection):
    with canonical_connection.begin():
        with pytest.raises(CanonicalOptimizationBlocked) as blocked:
            create_optimization_run(
                canonical_connection,
                baseline_qualification_decision_id=None,
                actor_identity="isolated-optimizer",
                objective_json={"metric": "overall_score", "direction": "maximize"},
            )
    assert blocked.value.code == "BLOCKED_QUALIFIED_BASELINE_REQUIRED"
    assert _count(canonical_connection, OPTIMIZATION_RUNS_TABLE) == 0
    assert _count(canonical_connection, OPTIMIZATION_TRIALS_TABLE) == 0


def test_qualified_baseline_allows_only_isolated_trial_records(canonical_connection):
    with canonical_connection.begin():
        plan_id, attempt_id = _validated_attempt(
            canonical_connection, metrics_by_window=_passing_metrics()
        )
        score_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            scorer_identity="isolated-scorer-v1",
        )
        decision = qualify_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            qualifier_identity="isolated-qualifier-v1",
        )
        run = create_optimization_run(
            canonical_connection,
            baseline_qualification_decision_id=decision.qualification_decision_id,
            actor_identity="isolated-optimizer-v1",
            objective_json={"metric": "overall_score", "direction": "maximize"},
        )
        repeated = create_optimization_run(
            canonical_connection,
            baseline_qualification_decision_id=decision.qualification_decision_id,
            actor_identity="isolated-optimizer-v1",
            objective_json={"metric": "overall_score", "direction": "maximize"},
        )
        trial = record_isolated_optimization_trial(
            canonical_connection,
            optimization_run_id=run.optimization_run_id,
            trial_number=1,
            actor_identity="isolated-optimizer-v1",
            parameters_json={"ema_period": 12},
            metrics_json={"overall_score": 82.5},
        )
    assert decision.status == "QUALIFIED"
    assert run.status == "NOT_STARTED"
    assert repeated.optimization_run_id == run.optimization_run_id
    assert repeated.repeat_noop is True
    assert trial.trial_number == 1
    assert _count(canonical_connection, OPTIMIZATION_RUNS_TABLE) == 1
    assert _count(canonical_connection, OPTIMIZATION_TRIALS_TABLE) == 1
    assert _count(canonical_connection, DEPLOYMENTS_TABLE) == 0
    assert _count(canonical_connection, ORDERS_TABLE) == 0


def test_trial_is_immutable_and_cannot_create_or_promote_strategy(canonical_connection):
    with canonical_connection.begin():
        plan_id, attempt_id = _validated_attempt(
            canonical_connection, metrics_by_window=_passing_metrics()
        )
        score_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            scorer_identity="isolated-scorer-v1",
        )
        decision = qualify_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            qualifier_identity="isolated-qualifier-v1",
        )
        run = create_optimization_run(
            canonical_connection,
            baseline_qualification_decision_id=decision.qualification_decision_id,
            actor_identity="isolated-optimizer-v1",
            objective_json={"metric": "overall_score"},
        )
        record_isolated_optimization_trial(
            canonical_connection,
            optimization_run_id=run.optimization_run_id,
            trial_number=1,
            actor_identity="isolated-optimizer-v1",
            parameters_json={"ema_period": 12},
            metrics_json={"overall_score": 82.5},
        )
        with pytest.raises(CanonicalOptimizationBlocked) as rewrite:
            record_isolated_optimization_trial(
                canonical_connection,
                optimization_run_id=run.optimization_run_id,
                trial_number=1,
                actor_identity="isolated-optimizer-v1",
                parameters_json={"ema_period": 99},
                metrics_json={"overall_score": 99},
            )
    assert rewrite.value.code == "BLOCKED_OPTIMIZATION_TRIAL_REWRITE"


def test_trial_metrics_and_controlled_resubmission_lineage_are_durable(
    canonical_connection,
):
    with canonical_connection.begin():
        plan_id, attempt_id = _validated_attempt(
            canonical_connection, metrics_by_window=_passing_metrics()
        )
        score_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            scorer_identity="isolated-scorer-v1",
        )
        decision = qualify_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            qualifier_identity="isolated-qualifier-v1",
        )
        run = create_optimization_run(
            canonical_connection,
            baseline_qualification_decision_id=decision.qualification_decision_id,
            actor_identity="isolated-optimizer-v1",
            objective_json={"metric": "overall_score", "direction": "maximize"},
        )
        trial = record_isolated_optimization_trial(
            canonical_connection,
            optimization_run_id=run.optimization_run_id,
            trial_number=1,
            actor_identity="isolated-optimizer-v1",
            parameters_json={"ema_period": 12},
            metrics_json={"overall_score": 82.5},
        )
        baseline_version_id = canonical_connection.execute(
            select(QUALIFICATION_DECISIONS_TABLE.c.strategy_version_id).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == decision.qualification_decision_id
            )
        ).scalar_one()
        with pytest.raises(CanonicalOptimizationBlocked) as baseline_link:
            link_controlled_submission_version(
                canonical_connection,
                optimization_trial_id=trial.optimization_trial_id,
                submitted_strategy_version_id=baseline_version_id,
            )
        submission = controlled_submit_latest(
            canonical_connection,
            caller_identity="isolated-optimizer-resubmission",
            idempotency_key="optimization-trial-1",
            display_name="Optimization trial 1",
            snapshot=_snapshot(
                entry="optimization/trial-1.py",
                strategy="optimization-trial-1",
                content=(
                    b"from freqtrade.strategy import IStrategy\n"
                    b"class Optimized(IStrategy):\n    pass\n"
                ),
            ),
        )
        link_controlled_submission_version(
            canonical_connection,
            optimization_trial_id=trial.optimization_trial_id,
            submitted_strategy_version_id=submission.strategy_version_id,
        )
        row = canonical_connection.execute(
            select(OPTIMIZATION_TRIALS_TABLE).where(
                OPTIMIZATION_TRIALS_TABLE.c.id == trial.optimization_trial_id
            )
        ).mappings().one()
    assert baseline_link.value.code == "BLOCKED_CONTROLLED_SUBMISSION_REQUIRED"
    assert row["actor_identity"] == "isolated-optimizer-v1"
    assert row["environment_class"] == "ISOLATED_TEST"
    assert row["metrics_json"] == {"overall_score": 82.5}
    assert row["request_digest"] == trial.request_digest
    assert row["submitted_strategy_version_id"] == submission.strategy_version_id
    assert len(row["submission_link_digest"]) == 64
