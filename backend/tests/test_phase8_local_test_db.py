from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.exceptions import ConfigurationError
from app.db.session import create_database_engine, create_session_factory
from app.models import (
    BacktestResult,
    BacktestRun,
    BacktestTask,
    LocalTestBatch,
    LocalTestDbEvent,
    Strategy,
    StrategyFailureReason,
    StrategyScore,
    StrategyVersion,
)
from app.schemas import (
    BacktestResultRead,
    BacktestRunRead,
    StrategyRead,
    StrategyScoreRead,
    StrategyVersionRead,
)
from app.services.local_test_db import LocalTestDatabaseGuard, Phase8LocalTestDbService


@pytest.fixture()
def safe_sqlite_url() -> Generator[str, None, None]:
    path = Path("/tmp") / f"freqtrade-ai-pytest-phase8-{uuid4().hex}.sqlite"
    try:
        yield f"sqlite+pysqlite:///{path}"
    finally:
        path.unlink(missing_ok=True)


def test_guard_allows_only_safe_local_test_targets() -> None:
    guard = LocalTestDatabaseGuard()

    sqlite_target = guard.validate(
        "sqlite+pysqlite:////tmp/freqtrade-ai-phase8-guard.sqlite",
        "local",
    )
    postgres_target = guard.validate(
        "postgresql+psycopg://freqtrade:placeholder@localhost:5432/freqtrade_ai_phase8_test",
        "phase8",
    )
    phase9_target = guard.validate(
        "postgresql+psycopg://freqtrade:placeholder@localhost:5432/freqtrade_ai_phase9_test",
        "phase9",
    )

    assert sqlite_target.dialect == "sqlite"
    assert postgres_target.dialect == "postgresql"
    assert phase9_target.environment_label == "phase9"
    assert "placeholder" not in postgres_target.redacted_url

    unsafe_urls = [
        "sqlite+pysqlite:///:memory:",
        "sqlite+pysqlite:////tmp/not-freqtrade.sqlite",
        "sqlite+pysqlite:////var/tmp/freqtrade-ai-phase8.sqlite",
        "postgresql+psycopg://freqtrade:placeholder@example.com:5432/freqtrade_ai_phase8_test",
        "postgresql+psycopg://freqtrade:placeholder@localhost:5432/freqtrade_ai",
        "postgresql+psycopg://freqtrade:placeholder@localhost:5432/freqtrade_ai_prod",
    ]
    for database_url in unsafe_urls:
        with pytest.raises(ConfigurationError):
            guard.validate(database_url, "local")
    with pytest.raises(ConfigurationError):
        guard.validate("sqlite+pysqlite:////tmp/freqtrade-ai-phase9-local-test.sqlite", "production")


def test_reset_seed_dirty_and_trace_metadata(safe_sqlite_url: str) -> None:
    service = Phase8LocalTestDbService(safe_sqlite_url, environment_label="test")

    reset_summary = service.reset_database()
    baseline_summary = service.seed_baseline()
    dirty_summary = service.seed_dirty_scenarios()

    assert reset_summary["scenario_set"] == "reset"
    assert baseline_summary["source_counts"] == {"seed_generated": 6}
    assert set(baseline_summary["scenario_counts"]) == {
        "success",
        "failed",
        "blocked",
        "unknown-source",
        "missing-artifact",
        "partial-completion",
    }
    assert dirty_summary["source_counts"] == {"dirty_seed_generated": 3}

    engine = create_database_engine(safe_sqlite_url)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        batches = session.scalars(select(LocalTestBatch)).all()
        events = session.scalars(select(LocalTestDbEvent)).all()
        assert len(batches) == 3
        assert len(events) == 10

        success_strategy = session.scalars(
            select(Strategy).where(Strategy.name == "Phase8 Local Test success")
        ).one()
        success_version = session.scalars(
            select(StrategyVersion).where(StrategyVersion.strategy_id == success_strategy.id)
        ).one()
        success_run = session.scalars(
            select(BacktestRun).where(BacktestRun.profile_name == "phase8-local-test-success")
        ).one()
        success_result = session.scalars(
            select(BacktestResult).where(BacktestResult.backtest_run_id == success_run.id)
        ).one()
        dirty_score = session.scalars(
            select(StrategyScore).where(
                StrategyScore.scoring_version == "phase8-local-test-dirty-score-without-result"
            )
        ).one()

        strategy_source = StrategyRead.model_validate(success_strategy).data_source
        version_source = StrategyVersionRead.model_validate(success_version).data_source
        run_source = BacktestRunRead.model_validate(success_run).data_source
        result_source = BacktestResultRead.model_validate(success_result).data_source
        dirty_score_source = StrategyScoreRead.model_validate(dirty_score).data_source

        assert strategy_source.source_type == "fixture"
        assert version_source.source_type == "fixture"
        assert run_source.source_type == "fixture"
        assert result_source.source_type == "fixture"
        assert dirty_score_source.source_type == "fixture"
        assert dirty_score_source.core_data is False
        assert dirty_score_source.database_ids["strategy_score_id"] == dirty_score.id
        assert result_source.core_data is False
        assert result_source.database_ids["backtest_result_id"] == success_result.id

        unknown_run = session.scalars(
            select(BacktestRun).where(BacktestRun.profile_name == "phase8-local-test-unknown-source")
        ).one()
        assert BacktestRunRead.model_validate(unknown_run).data_source.source_type == "unknown"


def test_local_test_db_service_lists_batch_summaries(safe_sqlite_url: str) -> None:
    service = Phase8LocalTestDbService(safe_sqlite_url, environment_label="test")
    service.reset_database()
    service.seed_baseline()

    payload = service.summarize_batches(limit=5)

    assert payload["batches"]
    assert payload["batches"][0]["source_label"] == "phase8-local-test-db"
    assert payload["batches"][0]["source_counts"]


def test_operational_readiness_seed_report_covers_phase9_qa_states(safe_sqlite_url: str) -> None:
    service = Phase8LocalTestDbService(safe_sqlite_url, environment_label="phase9")

    report = service.seed_operational_readiness()

    expected_scenarios = {
        "success",
        "failed",
        "blocked",
        "unknown-source",
        "missing-artifact",
        "partial-completion",
        "dirty-score-without-result",
        "dirty-stale-running-backtest",
        "dirty-task-result-path-without-result-row",
    }
    coverage = {item["scenario"]: item for item in report["coverage"]}

    assert report["phase"] == "Phase 9"
    assert report["status"] == "ready_for_qa"
    assert report["environment_label"] == "phase9"
    assert report["data_source_policy"]["can_accept_as_real_run"] is False
    assert set(coverage) == expected_scenarios
    assert all(item["present"] for item in coverage.values())
    assert all(item["can_accept_as_real_run"] is False for item in coverage.values())
    assert coverage["success"]["source_kind"] == "seed_generated"
    assert coverage["blocked"]["blocked_reason"].startswith("BLOCKED:")
    assert coverage["unknown-source"]["blocked_reason"].startswith("UNKNOWN_SOURCE:")
    assert coverage["dirty-score-without-result"]["source_kind"] == "dirty_seed_generated"
    assert coverage["dirty-score-without-result"]["blocked_reason"].startswith("DIRTY_DATA:")
    assert all(item["status"] == "refused" for item in report["guard"]["refusal_examples"])

    engine = create_database_engine(safe_sqlite_url)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        assert session.query(Strategy).count() == 9
        assert session.query(BacktestRun).count() == 9
        assert session.query(BacktestTask).count() == 10
        assert session.query(BacktestResult).count() == 3
        assert session.query(StrategyScore).count() == 4
        assert session.query(StrategyFailureReason).count() == 3

        blocked_run = session.scalars(
            select(BacktestRun).where(BacktestRun.profile_name == "phase8-local-test-blocked")
        ).one()
        blocked_task = session.scalars(
            select(BacktestTask).where(BacktestTask.backtest_run_id == blocked_run.id)
        ).one()
        unknown_run = session.scalars(
            select(BacktestRun).where(BacktestRun.profile_name == "phase8-local-test-unknown-source")
        ).one()
        dirty_score = session.scalars(
            select(StrategyScore).where(
                StrategyScore.scoring_version == "phase8-local-test-dirty-score-without-result"
            )
        ).one()

        blocked_source = BacktestRunRead.model_validate(blocked_run).data_source
        unknown_source = BacktestRunRead.model_validate(unknown_run).data_source
        dirty_score_source = StrategyScoreRead.model_validate(dirty_score).data_source

        assert blocked_run.status == "blocked"
        assert blocked_task.status == "blocked"
        assert blocked_source.source_type == "fixture"
        assert blocked_source.core_data is False
        assert blocked_source.blocked_reason.startswith("BLOCKED:")
        assert unknown_source.source_type == "unknown"
        assert unknown_source.core_data is False
        assert dirty_score_source.source_type == "fixture"
        assert dirty_score_source.core_data is False
