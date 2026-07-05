from collections.abc import Generator
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory, get_db
from app.main import app
from app.models import Base
from app.repositories import (
    BacktestRepository,
    StrategyGenerationRunRepository,
    StrategyRepository,
)
from app.schemas import (
    BacktestResultCreate,
    BacktestRunCreate,
    BacktestTaskCreate,
    StrategyCreate,
    StrategyGenerationRunCreate,
    StrategyVersionCreate,
)


def test_phase8_real_data_list_apis_return_database_sources(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'phase8-real-data.sqlite'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    strategy_code = "class Phase8RealData:\n    pass\n"
    strategy_file = strategy_dir / "phase8_real_data.py"
    strategy_file.write_text(strategy_code, encoding="utf-8")
    strategy_checksum = hashlib.sha256(strategy_code.encode("utf-8")).hexdigest()

    with session_factory() as session:
        generation_run = StrategyGenerationRunRepository(session).create(
            StrategyGenerationRunCreate(
                provider="local-test",
                model="deterministic",
                prompt_hash="phase8-list-api",
                prompt_summary="phase8 real data list API",
                requested_count=1,
            )
        )
        strategy_repository = StrategyRepository(session)
        strategy = strategy_repository.create(
            StrategyCreate(name="Phase8 Real Data", slug="phase8-real-data")
        )
        version = strategy_repository.create_version(
            StrategyVersionCreate(
                strategy_id=strategy.id,
                generation_run_id=generation_run.id,
                blueprint={"class_name": "Phase8RealData"},
                generated_code=strategy_code,
                code_hash=strategy_checksum,
                file_path=str(strategy_file),
                validation_status="passed",
                diff_snapshot={
                    "strategy_file_validation": {
                        "approved_root": str(strategy_dir),
                        "checksum": strategy_checksum,
                        "validation_status": "passed",
                        "write_status": "written",
                    }
                },
            )
        )
        assert version is not None

        backtest_repository = BacktestRepository(session)
        run = backtest_repository.create_run(
            BacktestRunCreate(strategy_version_id=version.id, profile_name="phase8-list")
        )
        assert run is not None
        task = backtest_repository.create_task(
            run.id,
            BacktestTaskCreate(pair="BTC/USDT:USDT", timeframe="15m"),
        )
        assert task is not None
        result = backtest_repository.save_result(
            task.id,
            BacktestResultCreate(
                result_path="reports/backtests/phase8-real-data.json",
                profit_pct=0.08,
                max_drawdown_pct=0.02,
                win_rate=0.62,
                total_trades=24,
            ),
        )
        assert result is not None
        generation_run_id = generation_run.id
        strategy_id = strategy.id
        version_id = version.id
        backtest_run_id = run.id
        backtest_task_id = task.id

    def override_get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        strategies = client.get("/api/strategies")
        versions = client.get("/api/strategy-versions")
        generation_runs = client.get("/api/strategy-generation-runs")
        backtest_runs = client.get("/api/backtest-runs")
        backtest_tasks = client.get("/api/backtest-tasks")
        backtest_results = client.get("/api/backtest-results")
    finally:
        app.dependency_overrides.clear()

    assert strategies.status_code == 200
    assert strategies.json()[0]["data_source"]["core_data"] is True
    assert strategies.json()[0]["id"] == strategy_id
    assert versions.status_code == 200
    assert versions.json()[0]["file_path"] == str(strategy_file)
    assert versions.json()[0]["file_state"]["status"] == "READY"
    assert versions.json()[0]["data_source"]["core_data"] is True
    assert versions.json()[0]["data_source"]["database_ids"]["strategy_version_id"] == version_id
    assert generation_runs.status_code == 200
    assert generation_runs.json()[0]["id"] == generation_run_id
    assert backtest_runs.status_code == 200
    assert backtest_runs.json()[0]["strategy_version_id"] == version_id
    assert backtest_tasks.status_code == 200
    assert backtest_tasks.json()[0]["backtest_run_id"] == backtest_run_id
    assert backtest_results.status_code == 200
    assert backtest_results.json()[0]["backtest_task_id"] == backtest_task_id
    assert backtest_results.json()[0]["data_source"]["artifact_refs"]["result_path"] == (
        "reports/backtests/phase8-real-data.json"
    )
