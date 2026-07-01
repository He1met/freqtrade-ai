from typing import Any, Optional

import pytest
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory
from app.models import BacktestResult, Base
from app.repositories import BacktestRepository, StrategyRepository, StrategyScoreRepository
from app.schemas import (
    BacktestResultCreate,
    BacktestRunCreate,
    BacktestTaskCreate,
    StrategyFailureReasonCreate,
    StrategyCreate,
    StrategyVersionCreate,
)
from app.services.strategy_scoring import SCORING_VERSION, StrategyScoringService
from app.services.strategy_failure_reasons import StrategyFailureReasonService


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


def create_backtest_result(
    db_session: Session,
    slug: str = "scored-strategy",
    profit_pct: Optional[float] = 0.12,
    max_drawdown_pct: Optional[float] = 0.05,
    win_rate: Optional[float] = 0.60,
    total_trades: Optional[int] = 45,
    metrics_snapshot: Optional[dict[str, Any]] = None,
) -> int:
    strategy_repository = StrategyRepository(db_session)
    strategy = strategy_repository.create(
        StrategyCreate(name=slug.replace("-", " ").title(), slug=slug)
    )
    version = strategy_repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": slug.title().replace("-", "")},
            generated_code=f"class {slug.title().replace('-', '')}: pass",
            file_path=f"user_data/strategies/generated/{slug}.py",
        )
    )
    assert version is not None

    backtest_repository = BacktestRepository(db_session)
    run = backtest_repository.create_run(BacktestRunCreate(strategy_version_id=version.id))
    assert run is not None
    task = backtest_repository.create_task(
        run.id,
        BacktestTaskCreate(pair="BTC/USDT", timeframe="15m"),
    )
    assert task is not None
    result = backtest_repository.save_result(
        task.id,
        BacktestResultCreate(
            result_path=f"reports/backtests/{slug}.json",
            metrics_snapshot=metrics_snapshot or {"strategy": slug},
            profit_pct=profit_pct,
            max_drawdown_pct=max_drawdown_pct,
            win_rate=win_rate,
            total_trades=total_trades,
        ),
    )
    assert result is not None
    return result.id


def test_scores_backtest_result_and_saves_strategy_score(db_session: Session) -> None:
    result_id = create_backtest_result(db_session)

    score = StrategyScoringService(db_session).score_backtest_result(result_id)

    assert score is not None
    assert score.scoring_version == SCORING_VERSION
    assert score.backtest_result_id == result_id
    assert score.profit_score == 100.0
    assert score.risk_score == 75.0
    assert score.stability_score == 60.0
    assert score.quality_score == 100.0
    assert score.total_score == pytest.approx(87.75)
    assert score.metrics_snapshot["missing_metrics"] == []
    assert score.metrics_snapshot["score_breakdown"] == [
        {"name": "profit_score", "score": 100.0, "weight": 0.35, "contribution": 35.0},
        {"name": "risk_score", "score": 75.0, "weight": 0.25, "contribution": 18.75},
        {"name": "stability_score", "score": 60.0, "weight": 0.15, "contribution": 9.0},
        {"name": "quality_score", "score": 100.0, "weight": 0.25, "contribution": 25.0},
    ]
    assert score.metrics_snapshot["elimination"] == {"eliminated": False, "reasons": []}


def test_warning_signals_reduce_quality_without_eliminating(db_session: Session) -> None:
    result_id = create_backtest_result(
        db_session,
        slug="warning-strategy",
        profit_pct=0.03,
        max_drawdown_pct=0.25,
        win_rate=0.32,
        total_trades=5,
        metrics_snapshot={
            "validation": {"passed": True, "warnings": ["unused optional parameter"]},
            "static_review": {
                "passed": True,
                "findings": [
                    {
                        "rule_id": "lookahead-001",
                        "category": "lookahead_bias",
                        "severity": "warning",
                        "message": "Potential shifted indicator usage.",
                    }
                ],
            },
        },
    )

    score = StrategyScoringService(db_session).score_backtest_result(result_id)

    assert score is not None
    assert score.total_score > 0.0
    assert score.metrics_snapshot["elimination"]["eliminated"] is False
    assert score.metrics_snapshot["quality_breakdown"]["validation"]["warning_count"] == 1
    assert score.metrics_snapshot["quality_breakdown"]["static_review"]["warning_count"] == 1
    assert [warning["code"] for warning in score.metrics_snapshot["warnings"]] == [
        "elevated_drawdown",
        "low_trade_count",
        "low_win_rate",
        "validation_warnings",
        "static_review_warnings",
    ]


def test_static_review_error_eliminates_strategy_with_reason(db_session: Session) -> None:
    result_id = create_backtest_result(
        db_session,
        slug="blocked-static-review",
        metrics_snapshot={
            "static_review": {
                "passed": False,
                "findings": [
                    {
                        "rule_id": "network-access",
                        "category": "network_access",
                        "severity": "error",
                        "message": "Strategy attempted network access.",
                    }
                ],
            },
        },
    )

    score = StrategyScoringService(db_session).score_backtest_result(result_id)

    assert score is not None
    assert score.total_score == 0.0
    assert score.metrics_snapshot["raw_total_score"] > 0.0
    assert score.metrics_snapshot["elimination"]["eliminated"] is True
    assert [reason["code"] for reason in score.metrics_snapshot["elimination"]["reasons"]] == [
        "static_review_errors"
    ]


def test_recorded_failure_reason_eliminates_strategy(db_session: Session) -> None:
    result_id = create_backtest_result(db_session, slug="recorded-failure")
    result = db_session.get(BacktestResult, result_id)
    assert result is not None
    StrategyFailureReasonService(db_session).record_failure(
        StrategyFailureReasonCreate(
            strategy_id=result.run.strategy_version.strategy_id,
            strategy_version_id=result.run.strategy_version_id,
            stage="validation",
            reason_type="validation_error",
            severity="error",
            message="Blueprint validation failed before scoring.",
        )
    )

    score = StrategyScoringService(db_session).score_backtest_result(result_id)

    assert score is not None
    assert score.total_score == 0.0
    assert score.metrics_snapshot["quality_breakdown"]["failure_history"]["error_count"] == 1
    assert [reason["code"] for reason in score.metrics_snapshot["elimination"]["reasons"]] == [
        "recorded_failure_reasons"
    ]


def test_missing_metrics_create_clear_zero_score_snapshot(db_session: Session) -> None:
    result_id = create_backtest_result(
        db_session,
        slug="missing-metrics",
        profit_pct=None,
        max_drawdown_pct=None,
        win_rate=None,
        total_trades=None,
    )

    score = StrategyScoringService(db_session).score_backtest_result(result_id)

    assert score is not None
    assert score.total_score == 0.0
    assert score.metrics_snapshot["elimination"]["eliminated"] is True
    assert score.metrics_snapshot["missing_metrics"] == [
        "profit_pct",
        "max_drawdown_pct",
        "win_rate",
        "total_trades",
    ]
    assert [reason["code"] for reason in score.metrics_snapshot["elimination"]["reasons"]] == [
        "missing_backtest_metrics"
    ]


def test_scoring_updates_existing_version_score(db_session: Session) -> None:
    result_id = create_backtest_result(db_session, slug="updatable-score", profit_pct=0.01)
    service = StrategyScoringService(db_session)

    first = service.score_backtest_result(result_id)
    second = service.score_backtest_result(result_id)

    assert first is not None
    assert second is not None
    assert first.id == second.id
    ranking = StrategyScoreRepository(db_session).list_ranking()
    assert len(ranking) == 1
