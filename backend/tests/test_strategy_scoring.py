from typing import Optional

import pytest
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import BacktestRepository, StrategyRepository, StrategyScoreRepository
from app.schemas import (
    BacktestResultCreate,
    BacktestRunCreate,
    BacktestTaskCreate,
    StrategyCreate,
    StrategyVersionCreate,
)
from app.services.strategy_scoring import SCORING_VERSION, StrategyScoringService


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
            metrics_snapshot={"strategy": slug},
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
    assert score.total_score == pytest.approx(85.75)
    assert score.metrics_snapshot["missing_metrics"] == []


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
    assert score.metrics_snapshot["missing_metrics"] == [
        "profit_pct",
        "max_drawdown_pct",
        "win_rate",
        "total_trades",
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
