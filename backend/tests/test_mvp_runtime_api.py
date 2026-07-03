import json

from fastapi.testclient import TestClient

from app.api import mvp_runtime
from app.main import app


def test_runtime_api_returns_sections(tmp_path, monkeypatch) -> None:
    payload = {
        "strategies": [{"id": "strategy-1", "name": "Runtime Strategy"}],
        "generationRuns": [{"id": "generation-1", "status": "succeeded"}],
        "backtestRuns": [{"id": "run-1", "status": "succeeded"}],
        "backtestTasks": [{"id": "task-1", "status": "succeeded"}],
        "ranking": [{"rank": 1, "strategyId": "strategy-1"}],
        "failureReasons": [],
        "versionLineage": [],
    }
    runtime_data = tmp_path / "mvp-data.json"
    runtime_data.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("FREQTRADE_AI_MVP_DATA_PATH", str(runtime_data))

    client = TestClient(app)

    assert client.get("/api/strategies").json() == payload["strategies"]
    assert client.get("/api/generation-runs").json() == payload["generationRuns"]
    assert client.get("/api/backtest-runs").json() == payload["backtestRuns"]
    assert client.get("/api/backtest-tasks").json() == payload["backtestTasks"]
    assert client.get("/api/ranking").json() == payload["ranking"]
    assert client.get("/api/strategy-failure-reasons").json() == payload["failureReasons"]
    assert client.get("/api/strategy-version-lineage").json() == payload["versionLineage"]
    assert client.get("/api/mvp/runtime-data").json() == payload


def test_runtime_api_reports_missing_seed_file(monkeypatch) -> None:
    monkeypatch.delenv("FREQTRADE_AI_MVP_DATA_PATH", raising=False)
    monkeypatch.setattr(
        mvp_runtime,
        "DEFAULT_RUNTIME_DATA_PATH",
        mvp_runtime.REPO_ROOT / "tmp" / "runtime" / "missing-test-file.json",
    )
    client = TestClient(app)

    response = client.get("/api/strategies")

    assert response.status_code == 503
    assert "seed_runtime_mvp.py" in response.json()["detail"]
