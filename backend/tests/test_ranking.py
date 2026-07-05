from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory, get_db
from app.main import app
from app.models import BacktestResult, Base
from app.repositories import BacktestRepository, StrategyRepository, StrategyScoreRepository
from app.schemas import (
    BacktestResultCreate,
    BacktestRunCreate,
    BacktestTaskCreate,
    StrategyCreate,
    StrategyScoreCreate,
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
) -> BacktestResult:
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
    return result


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
    assert ranking[0].backtest_result_id is not None
    assert ranking[0].data_source.core_data is True
    assert ranking[0].data_source.database_ids["backtest_result_id"] == ranking[0].backtest_result_id
    assert ranking[0].total_score > ranking[1].total_score


def test_empty_ranking_returns_empty_list(db_session: Session) -> None:
    assert StrategyScoreRepository(db_session).list_ranking() == []


def test_fixture_only_score_source_is_filtered_from_core_ranking(db_session: Session) -> None:
    result = score_strategy(
        db_session,
        slug="fixture-source",
        profit_pct=0.10,
        max_drawdown_pct=0.04,
        win_rate=0.65,
        total_trades=30,
    )
    repository = StrategyScoreRepository(db_session)
    existing_score = repository.get_for_version(
        result.run.strategy_version_id,
        "phase2-quality-v1",
    )
    assert existing_score is not None
    db_session.delete(existing_score)
    db_session.commit()

    score = repository.save(
        StrategyScoreCreate(
            strategy_id=result.run.strategy_version.strategy_id,
            strategy_version_id=result.run.strategy_version_id,
            backtest_result_id=result.id,
            scoring_version="fixture-score-v1",
            total_score=99.0,
            metrics_snapshot={
                "source": "fixture",
                "backtest_result_id": result.id,
                "missing_metrics": [],
            },
        )
    )

    assert score is not None
    assert StrategyScoreRepository(db_session).list_ranking() == []


def test_ranking_api_returns_database_rows_with_traceability(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'ranking.sqlite'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as setup_session:
        result = score_strategy(
            setup_session,
            slug="api-ranked",
            profit_pct=0.14,
            max_drawdown_pct=0.03,
            win_rate=0.68,
            total_trades=44,
        )
        result_id = result.id

    def override_get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).get("/api/ranking")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["strategy_slug"] == "api-ranked"
    assert payload[0]["backtest_result_id"] == result_id
    assert payload[0]["data_source"]["source_type"] == "api_aggregate"
    assert payload[0]["data_source"]["core_data"] is True
    assert payload[0]["data_source"]["database_ids"]["backtest_result_id"] == result_id
