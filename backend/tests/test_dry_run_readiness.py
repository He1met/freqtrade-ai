from collections.abc import Generator
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory, get_db
from app.main import app
from app.models import Base
from app.repositories import StrategyRepository
from app.schemas import StrategyCreate, StrategyVersionCreate


def client_with_db(tmp_path: Path) -> tuple[TestClient, object]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'readiness.sqlite'}")
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
    class_name: str = "PhaseEightDryRunReady",
    file_exists: bool = True,
    validation_status: str = "passed",
) -> int:
    code = f"class {class_name}:\n    pass\n"
    strategy_file = tmp_path / "user_data" / "strategies" / "generated" / f"{class_name}.py"
    if file_exists:
        strategy_file.parent.mkdir(parents=True, exist_ok=True)
        strategy_file.write_text(code, encoding="utf-8")
    repository = StrategyRepository(db)
    strategy = repository.create(StrategyCreate(name=class_name, slug=class_name.lower()))
    version = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": class_name},
            generated_code=code,
            code_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
            file_path=str(strategy_file),
            validation_status=validation_status,
        )
    )
    assert version is not None
    return version.id


def write_market_data(tmp_path: Path) -> Path:
    data_dir = tmp_path / "user_data" / "data"
    exchange_dir = data_dir / "okx"
    exchange_dir.mkdir(parents=True)
    exchange_dir.joinpath("BTC_USDT_USDT-15m-20240101-20240201.feather").write_bytes(b"local candles")
    return data_dir


def readiness_payload(strategy_version_id: int, market_data_dir: Path) -> dict:
    return {
        "strategy_version_id": strategy_version_id,
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "exchange": "okx",
        "market_data_dir": str(market_data_dir),
    }


def test_dry_run_readiness_returns_ready_only_when_all_local_checks_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FREQTRADE_DRY_RUN_API_KEY", "present")
    monkeypatch.setenv("FREQTRADE_DRY_RUN_API_SECRET", "present")
    client, session_factory = client_with_db(tmp_path)
    market_data_dir = write_market_data(tmp_path)
    with session_factory() as db:
        version_id = seed_strategy_version(db, tmp_path)

    try:
        response = client.post("/api/dry-run/readiness", json=readiness_payload(version_id, market_data_dir))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "READY"
    assert payload["blocked_reasons"] == []
    assert {check["name"]: check["status"] for check in payload["checks"]} == {
        "safety_boundary": "READY",
        "dry_run_profile": "READY",
        "strategy_file": "READY",
        "local_market_data": "READY",
        "env_only_credentials": "READY",
        "dry_run_config_preview": "READY",
    }
    assert payload["config_preview"]["dry_run"] is True
    assert payload["safety"]["starts_freqtrade"] is False
    assert payload["safety"]["live_trading"] is False
    assert payload["safety"]["real_orders"] is False
    assert payload["safety"]["stores_sensitive_values"] is False


def test_dry_run_readiness_blocks_missing_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("FREQTRADE_DRY_RUN_API_KEY", raising=False)
    monkeypatch.delenv("FREQTRADE_DRY_RUN_API_SECRET", raising=False)
    client, session_factory = client_with_db(tmp_path)
    market_data_dir = write_market_data(tmp_path)
    with session_factory() as db:
        version_id = seed_strategy_version(db, tmp_path)

    try:
        response = client.post("/api/dry-run/readiness", json=readiness_payload(version_id, market_data_dir))
    finally:
        app.dependency_overrides.clear()

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "BLOCKED"
    assert any("required ENV variables are missing" in reason for reason in payload["blocked_reasons"])
    assert payload["env_preflight"]["required_env_missing"] == [
        "FREQTRADE_DRY_RUN_API_KEY",
        "FREQTRADE_DRY_RUN_API_SECRET",
    ]


def test_dry_run_readiness_blocks_missing_strategy_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FREQTRADE_DRY_RUN_API_KEY", "present")
    monkeypatch.setenv("FREQTRADE_DRY_RUN_API_SECRET", "present")
    client, session_factory = client_with_db(tmp_path)
    market_data_dir = write_market_data(tmp_path)
    with session_factory() as db:
        version_id = seed_strategy_version(db, tmp_path, file_exists=False)

    try:
        response = client.post("/api/dry-run/readiness", json=readiness_payload(version_id, market_data_dir))
    finally:
        app.dependency_overrides.clear()

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "BLOCKED"
    assert any("strategy file does not exist" in reason for reason in payload["blocked_reasons"])


def test_dry_run_readiness_blocks_missing_local_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FREQTRADE_DRY_RUN_API_KEY", "present")
    monkeypatch.setenv("FREQTRADE_DRY_RUN_API_SECRET", "present")
    client, session_factory = client_with_db(tmp_path)
    with session_factory() as db:
        version_id = seed_strategy_version(db, tmp_path)

    try:
        response = client.post(
            "/api/dry-run/readiness",
            json=readiness_payload(version_id, tmp_path / "missing-data"),
        )
    finally:
        app.dependency_overrides.clear()

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "BLOCKED"
    assert any("market data directory does not exist" in reason for reason in payload["blocked_reasons"])


def test_dry_run_readiness_blocks_unsafe_dry_run_false(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FREQTRADE_DRY_RUN_API_KEY", "present")
    monkeypatch.setenv("FREQTRADE_DRY_RUN_API_SECRET", "present")
    client, session_factory = client_with_db(tmp_path)
    market_data_dir = write_market_data(tmp_path)
    with session_factory() as db:
        version_id = seed_strategy_version(db, tmp_path)

    payload = readiness_payload(version_id, market_data_dir)
    payload["dry_run"] = False
    try:
        response = client.post("/api/dry-run/readiness", json=payload)
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "BLOCKED"
    assert "dry_run must be true" in body["blocked_reasons"]
