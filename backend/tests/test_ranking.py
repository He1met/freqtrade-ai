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
from app.services.strategy_scoring import StrategyScoringService


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


def score_strategy(
    db_session: Session,
    slug: str,
    profit_pct: float,
    max_drawdown_pct: float,
    win_rate: float,
    total_trades: int,
) -> None:
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
            profit_pct=profit_pct,
            max_drawdown_pct=max_drawdown_pct,
            win_rate=win_rate,
            total_trades=total_trades,
        ),
    )
    assert result is not None
    assert StrategyScoringService(db_session).score_backtest_result(result.id) is not None


def test_ranking_orders_by_total_score_desc(db_session: Session) -> None:
    score_strategy(
        db_session,
        slug="weak-strategy",
        profit_pct=-0.04,
        max_drawdown_pct=0.30,
        win_rate=0.30,
        total_trades=4,
    )
    score_strategy(
        db_session,
        slug="strong-strategy",
        profit_pct=0.18,
        max_drawdown_pct=0.03,
        win_rate=0.70,
        total_trades=50,
    )

    ranking = StrategyScoreRepository(db_session).list_ranking()

    assert [entry.strategy_slug for entry in ranking] == ["strong-strategy", "weak-strategy"]
    assert ranking[0].strategy_name == "Strong Strategy"
    assert ranking[0].file_path == "user_data/strategies/generated/strong-strategy.py"
    assert ranking[0].total_score > ranking[1].total_score


def test_empty_ranking_returns_empty_list(db_session: Session) -> None:
    assert StrategyScoreRepository(db_session).list_ranking() == []
