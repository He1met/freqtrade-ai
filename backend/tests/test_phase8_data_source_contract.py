import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory, get_db
from app.main import app
from app.models import Base
from app.models.debug_mvp_seed import DebugMvpSeedPayload
from app.repositories import BacktestRepository, StrategyRepository, StrategyScoreRepository
from app.repositories.debug_mvp_seed_data import DebugMvpSeedDataRepository
from app.schemas import (
    BacktestResultCreate,
    BacktestResultRead,
    BacktestRunCreate,
    BacktestRunRead,
    BacktestTaskCreate,
    BacktestTaskRead,
    DataSourceTrace,
    StrategyCreate,
    StrategyRead,
    StrategyScoreRead,
    StrategyVersionCreate,
    StrategyVersionRead,
    fallback_source,
    fixture_source,
    unknown_source,
)
from app.services.debug_mvp_seed_data import build_debug_mvp_seed_payloads
from app.services.strategy_scoring import StrategyScoringService


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


def test_core_database_read_models_include_traceable_database_source(db_session: Session) -> None:
    strategy_repository = StrategyRepository(db_session)
    strategy = strategy_repository.create(
        StrategyCreate(name="Phase 8 Source Contract", slug="phase8-source-contract")
    )
    version = strategy_repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": "Phase8SourceContract"},
            generated_code="class Phase8SourceContract: pass",
            file_path="user_data/strategies/generated/phase8_source_contract.py",
            validation_status="passed",
        )
    )
    assert version is not None

    strategy_read = StrategyRead.model_validate(strategy)
    version_read = StrategyVersionRead.model_validate(version)

    assert strategy_read.data_source.source_type == "database"
    assert strategy_read.data_source.core_data is True
    assert strategy_read.data_source.database_ids == {"strategy_id": strategy.id}
    assert version_read.data_source.database_ids["strategy_version_id"] == version.id
    assert version_read.data_source.database_ids["strategy_id"] == strategy.id
    assert (
        version_read.data_source.artifact_refs["strategy_file_path"]
        == "user_data/strategies/generated/phase8_source_contract.py"
    )


def test_backtest_and_score_read_models_include_database_traceability(db_session: Session) -> None:
    strategy_repository = StrategyRepository(db_session)
    strategy = strategy_repository.create(
        StrategyCreate(name="Phase 8 Backtest Trace", slug="phase8-backtest-trace")
    )
    version = strategy_repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": "Phase8BacktestTrace"},
            generated_code="class Phase8BacktestTrace: pass",
            file_path="user_data/strategies/generated/phase8_backtest_trace.py",
        )
    )
    assert version is not None

    backtest_repository = BacktestRepository(db_session)
    run = backtest_repository.create_run(
        BacktestRunCreate(strategy_version_id=version.id, profile_name="phase8-local")
    )
    assert run is not None
    task = backtest_repository.create_task(
        run.id,
        BacktestTaskCreate(
            pair="BTC/USDT",
            timeframe="15m",
            config_path="reports/phase8/config.json",
        ),
    )
    assert task is not None
    result = backtest_repository.save_result(
        task.id,
        BacktestResultCreate(
            result_path="reports/phase8/backtest-result.json",
            profit_pct=0.08,
            max_drawdown_pct=0.02,
            win_rate=0.62,
            total_trades=20,
        ),
    )
    assert result is not None

    score = StrategyScoringService(db_session).score_backtest_result(result.id)
    assert score is not None
    ranking = StrategyScoreRepository(db_session).list_ranking()

    assert BacktestRunRead.model_validate(run).data_source.database_ids == {
        "backtest_run_id": run.id,
        "strategy_version_id": version.id,
    }
    task_source = BacktestTaskRead.model_validate(task).data_source
    assert task_source.database_ids["backtest_task_id"] == task.id
    assert task_source.artifact_refs["config_path"] == "reports/phase8/config.json"
    result_source = BacktestResultRead.model_validate(result).data_source
    assert result_source.database_ids["backtest_result_id"] == result.id
    assert result_source.artifact_refs["result_path"] == "reports/phase8/backtest-result.json"
    score_source = StrategyScoreRead.model_validate(score).data_source
    assert score_source.database_ids["backtest_result_id"] == result.id
    assert ranking[0].data_source.source_type == "api_aggregate"
    assert ranking[0].data_source.core_data is True
    assert ranking[0].data_source.database_ids["strategy_score_id"] == score.id


def test_fixture_fallback_and_unknown_sources_cannot_claim_core_success() -> None:
    assert fixture_source("local fixture").core_data is False
    assert fallback_source("frontend fallback").core_data is False
    assert unknown_source("unknown source").core_data is False

    with pytest.raises(ValidationError, match="fallback data cannot satisfy core success"):
        DataSourceTrace(
            source_type="fallback",
            source_detail="frontend fallback",
            core_data=True,
        )


def test_seeded_debug_api_marks_payloads_as_fixture_not_core_success(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'debug-seed.sqlite'}"
    engine = create_database_engine(database_url)
    DebugMvpSeedPayload.__table__.create(bind=engine, checkfirst=True)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        DebugMvpSeedDataRepository(session).upsert_payloads(build_debug_mvp_seed_payloads())

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).get("/api/strategies")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["name"] == "SeededBackendRsi001"
    assert payload[0]["data_source"]["source_type"] == "fixture"
    assert payload[0]["data_source"]["core_data"] is False
    assert "backend-seeded-sqlite-debug" in payload[0]["data_source"]["source_detail"]
