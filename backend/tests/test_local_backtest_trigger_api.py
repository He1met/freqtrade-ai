from collections.abc import Generator
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory, get_db
from app.main import app
from app.models import BacktestResult, BacktestRun, BacktestTask, Base
from app.repositories import StrategyRepository
from app.schemas import StrategyCreate, StrategyVersionCreate


def client_with_backtest_db(tmp_path: Path) -> tuple[TestClient, object]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'backtest-api.sqlite'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    def override_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), session_factory


def seed_strategy_version(
    db: Session,
    tmp_path: Path,
    *,
    class_name: str = "PhaseEightStrategy",
    validation_status: str = "passed",
    file_exists: bool = True,
) -> int:
    strategy_file = tmp_path / "strategies" / f"{class_name}.py"
    code = f"class {class_name}:\n    pass\n"
    if file_exists:
        strategy_file.parent.mkdir(parents=True)
        strategy_file.write_text(code, encoding="utf-8")

    repository = StrategyRepository(db)
    strategy = repository.create(StrategyCreate(name=class_name, slug=class_name.lower()))
    version = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"phase": "phase8"},
            generated_code=code,
            code_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
            file_path=str(strategy_file),
            validation_status=validation_status,
        )
    )
    assert version is not None
    return version.id


def write_market_data(tmp_path: Path) -> Path:
    datadir = tmp_path / "user_data" / "data"
    exchange_dir = datadir / "okx"
    exchange_dir.mkdir(parents=True)
    exchange_dir.joinpath("BTC_USDT-5m-20240101-20240201.feather").write_bytes(b"local candles")
    return datadir


def local_profile(datadir: Path, *, strategy_name: str = "PhaseEightStrategy") -> dict:
    return {
        "schema_version": "2",
        "profile_name": "phase8-local",
        "pair": "BTC/USDT",
        "timeframe": "5m",
        "timerange": "20240101-20240201",
        "strategy": {"name": strategy_name},
        "data_source": {
            "kind": "local",
            "exchange": "okx",
            "datadir": str(datadir),
        },
        "safety": {
            "allow_download": False,
            "allow_exchange_connection": False,
            "allow_dry_run": False,
            "allow_live_trading": False,
            "allow_hyperopt": False,
        },
    }


def test_local_backtest_trigger_creates_pending_records_and_reconciles_status(tmp_path: Path) -> None:
    client, session_factory = client_with_backtest_db(tmp_path)
    datadir = write_market_data(tmp_path)
    with session_factory() as db:
        version_id = seed_strategy_version(db, tmp_path)

    try:
        response = client.post(
            "/api/backtest-runs/local",
            json={"strategy_version_id": version_id, "profile": local_profile(datadir)},
        )
        assert response.status_code == 201
        payload = response.json()
        run_id = payload["run"]["id"]
        task_id = payload["tasks"][0]["id"]

        run_response = client.get(f"/api/backtest-runs/{run_id}")
        tasks_response = client.get(f"/api/backtest-runs/{run_id}/tasks")
        task_response = client.get(f"/api/backtest-tasks/{task_id}")
    finally:
        app.dependency_overrides.clear()

    assert payload["preflight_status"] == "ready"
    assert payload["blocked_reasons"] == []
    assert payload["execution_mode"] == "preflight_only"
    assert payload["run"]["status"] == "pending"
    assert payload["tasks"][0]["status"] == "pending"
    assert payload["tasks"][0]["pair"] == "BTC/USDT"
    assert payload["tasks"][0]["timeframe"] == "5m"
    assert payload["run"]["config_snapshot"]["safety"] == {
        "market_data_download": False,
        "exchange_connection": False,
        "dry_run": False,
        "live_trading": False,
        "real_orders": False,
        "freqtrade_execution": False,
    }

    assert run_response.status_code == 200
    assert run_response.json()["status"] == "pending"
    assert tasks_response.status_code == 200
    assert [task["id"] for task in tasks_response.json()] == [task_id]
    assert task_response.status_code == 200
    assert task_response.json()["status"] == "pending"

    with session_factory() as db:
        run = db.get(BacktestRun, run_id)
        task = db.get(BacktestTask, task_id)
        assert run is not None
        assert task is not None
        assert run.status == "pending"
        assert task.status == "pending"
        assert db.query(BacktestResult).count() == 0


def test_local_backtest_trigger_blocks_when_local_data_is_missing(tmp_path: Path) -> None:
    client, session_factory = client_with_backtest_db(tmp_path)
    missing_datadir = tmp_path / "missing-data"
    with session_factory() as db:
        version_id = seed_strategy_version(db, tmp_path)

    try:
        response = client.post(
            "/api/backtest-runs/local",
            json={"strategy_version_id": version_id, "profile": local_profile(missing_datadir)},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    payload = response.json()
    assert payload["preflight_status"] == "blocked"
    assert payload["run"]["status"] == "blocked"
    assert payload["tasks"][0]["status"] == "blocked"
    assert payload["tasks"][0]["error_message"].startswith("BLOCKED:")
    assert "market data directory does not exist" in payload["tasks"][0]["error_message"]

    with session_factory() as db:
        run = db.get(BacktestRun, payload["run"]["id"])
        task = db.get(BacktestTask, payload["tasks"][0]["id"])
        assert run is not None
        assert task is not None
        assert run.status == "blocked"
        assert task.status == "blocked"
        assert task.completed_at is not None


def test_local_backtest_trigger_blocks_when_strategy_file_is_missing(tmp_path: Path) -> None:
    client, session_factory = client_with_backtest_db(tmp_path)
    datadir = write_market_data(tmp_path)
    with session_factory() as db:
        version_id = seed_strategy_version(db, tmp_path, file_exists=False)

    try:
        response = client.post(
            "/api/backtest-runs/local",
            json={"strategy_version_id": version_id, "profile": local_profile(datadir)},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    payload = response.json()
    assert payload["preflight_status"] == "blocked"
    assert payload["run"]["status"] == "blocked"
    assert "strategy file does not exist" in payload["tasks"][0]["error_message"]


def test_local_backtest_trigger_persists_blocked_record_for_invalid_profile(tmp_path: Path) -> None:
    client, session_factory = client_with_backtest_db(tmp_path)
    with session_factory() as db:
        version_id = seed_strategy_version(db, tmp_path)

    profile = local_profile(tmp_path / "missing")
    profile["safety"]["allow_download"] = True

    try:
        response = client.post(
            "/api/backtest-runs/local",
            json={"strategy_version_id": version_id, "profile": profile},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    payload = response.json()
    assert payload["preflight_status"] == "blocked"
    assert payload["run"]["status"] == "blocked"
    assert payload["tasks"][0]["status"] == "blocked"
    assert "profile invalid at safety.allow_download" in payload["tasks"][0]["error_message"]


def test_local_backtest_trigger_persists_blocked_record_for_unusable_task_fields(
    tmp_path: Path,
) -> None:
    client, session_factory = client_with_backtest_db(tmp_path)
    with session_factory() as db:
        version_id = seed_strategy_version(db, tmp_path)

    profile = local_profile(tmp_path / "missing")
    profile["timeframe"] = "1" * 200

    try:
        response = client.post(
            "/api/backtest-runs/local",
            json={"strategy_version_id": version_id, "profile": profile},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    payload = response.json()
    assert payload["preflight_status"] == "blocked"
    assert payload["tasks"][0]["status"] == "blocked"
    assert len(payload["tasks"][0]["timeframe"]) == 32
    assert "profile invalid at timeframe" in payload["tasks"][0]["error_message"]
