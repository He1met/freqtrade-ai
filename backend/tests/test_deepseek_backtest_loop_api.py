from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory, get_db
from app.main import app
from app.models import (
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    ResearchJob,
    Strategy,
    StrategyGenerationRun,
    StrategyScore,
    StrategyVersion,
)
from app.services.deepseek_backtest_loop import DeepSeekBacktestLoopService


def loop_request_payload() -> dict:
    return {
        "prompt_summary": "Queue one DeepSeek strategy and local backtest job.",
        "allow_real_call": True,
        "backtest_profile": {
            "schema_version": "2",
            "profile_name": "phase9-worker-api",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "timerange": "20240101-20240201",
            "strategy": {"name": "QueuedDeepSeekStrategy"},
            "data_source": {
                "kind": "local",
                "exchange": "okx",
                "datadir": "user_data/data",
                "trading_mode": "futures",
                "margin_mode": "isolated",
            },
            "safety": {
                "allow_download": False,
                "allow_exchange_connection": False,
                "allow_dry_run": False,
                "allow_live_trading": False,
                "allow_hyperopt": False,
            },
        },
        "timeout_seconds": 60,
    }


@pytest.fixture()
def queued_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'research-job-api.sqlite'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    provider_execution_calls = []

    def fail_if_sync_loop_executes(self, payload):
        provider_execution_calls.append(payload)
        raise AssertionError("the enqueue API must not execute the synchronous Provider/backtest loop")

    monkeypatch.setattr(DeepSeekBacktestLoopService, "run", fail_if_sync_loop_executes)

    def override_get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(
        app,
        headers={
            "X-Operator-Token": "synthetic-test-operator-token",
            "Idempotency-Key": "queued-deepseek-api-test",
            "X-Provider-Authorization": "once",
        },
    )
    try:
        yield client, session_factory, provider_execution_calls
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_legacy_backtest_loop_post_queues_job_and_returns_202_without_provider_execution(
    queued_api,
) -> None:
    client, session_factory, provider_execution_calls = queued_api

    response = client.post(
        "/api/strategy-generation-runs/deepseek-single/backtest-loop",
        json=loop_request_payload(),
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["job_type"] == "deepseek_backtest"
    assert payload["status"] == "PENDING"
    assert payload["stage"] == "QUEUED"
    assert payload["attempt_count"] == 0
    assert payload["lease_owner"] is None
    assert payload["status_url"] == f"/api/deepseek-backtest-jobs/{payload['id']}"
    assert payload["data_source"]["core_data"] is True
    assert payload["data_source"]["database_ids"]["research_job_id"] == payload["id"]
    assert provider_execution_calls == []

    with session_factory() as db:
        job = db.get(ResearchJob, payload["id"])
        assert job is not None
        assert job.status == "PENDING"
        assert db.query(ResearchJob).count() == 1
        assert db.query(StrategyGenerationRun).count() == 0
        assert db.query(Strategy).count() == 0
        assert db.query(StrategyVersion).count() == 0
        assert db.query(BacktestRun).count() == 0
        assert db.query(BacktestTask).count() == 0
        assert db.query(BacktestResult).count() == 0
        assert db.query(StrategyScore).count() == 0


def test_research_job_get_list_and_worker_status_are_database_backed(queued_api) -> None:
    client, _session_factory, provider_execution_calls = queued_api
    created = client.post(
        "/api/strategy-generation-runs/deepseek-single/backtest-loop",
        json=loop_request_payload(),
    )
    assert created.status_code == 202
    job_id = created.json()["id"]

    detail = client.get(f"/api/deepseek-backtest-jobs/{job_id}")
    listing = client.get("/api/deepseek-backtest-jobs")
    worker_status = client.get("/api/deepseek-backtest-worker/status")

    assert detail.status_code == 200
    assert detail.json()["id"] == job_id
    assert detail.json()["status"] == "PENDING"
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()] == [job_id]
    assert worker_status.status_code == 200
    assert worker_status.json()["paused"] is False
    assert worker_status.json()["active_job_id"] is None
    assert worker_status.json()["pending_jobs"] == 1
    assert worker_status.json()["running_jobs"] == 0
    assert provider_execution_calls == []


def test_cancel_api_persists_pending_terminal_state_and_status_endpoint_reflects_it(
    queued_api,
) -> None:
    client, session_factory, provider_execution_calls = queued_api
    created = client.post(
        "/api/strategy-generation-runs/deepseek-single/backtest-loop",
        json=loop_request_payload(),
    )
    assert created.status_code == 202
    job_id = created.json()["id"]

    cancelled = client.post(
        f"/api/deepseek-backtest-jobs/{job_id}/cancel",
        json={"reason": "Cancelled by API contract test."},
        headers={"Idempotency-Key": "cancel-research-job-test"},
    )
    refreshed = client.get(f"/api/deepseek-backtest-jobs/{job_id}")
    worker_status = client.get("/api/deepseek-backtest-worker/status")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["stage"] == "CANCELLED"
    assert cancelled.json()["cancel_requested"] is True
    assert cancelled.json()["lease_owner"] is None
    assert cancelled.json()["completed_at"] is not None
    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "CANCELLED"
    assert worker_status.status_code == 200
    assert worker_status.json()["pending_jobs"] == 0
    assert worker_status.json()["running_jobs"] == 0
    assert provider_execution_calls == []

    with session_factory() as db:
        job = db.get(ResearchJob, job_id)
        assert job is not None
        assert job.status == "CANCELLED"
        assert job.error_message == "Cancelled by API contract test."
        assert job.evidence_snapshot["status"] == "CANCELLED"


def test_pause_and_cancel_reasons_are_redacted_before_database_persistence(queued_api) -> None:
    client, session_factory, provider_execution_calls = queued_api
    created = client.post(
        "/api/strategy-generation-runs/deepseek-single/backtest-loop",
        json=loop_request_payload(),
    )
    assert created.status_code == 202
    job_id = created.json()["id"]

    paused = client.post(
        "/api/deepseek-backtest-worker/pause",
        json={"reason": "maintenance api_key=synthetic-sensitive-value"},
        headers={"Idempotency-Key": "pause-reason-redaction"},
    )
    cancelled = client.post(
        f"/api/deepseek-backtest-jobs/{job_id}/cancel",
        json={"reason": "cancel token=synthetic-sensitive-value"},
        headers={"Idempotency-Key": "cancel-reason-redaction"},
    )

    assert paused.status_code == 200
    assert paused.json()["reason"] == "maintenance api_key=[REDACTED]"
    assert cancelled.status_code == 200
    assert cancelled.json()["error_message"] == "cancel token=[REDACTED]"
    assert "synthetic-sensitive-value" not in str(paused.json())
    assert "synthetic-sensitive-value" not in str(cancelled.json())
    assert provider_execution_calls == []

    with session_factory() as db:
        job = db.get(ResearchJob, job_id)
        assert job is not None
        assert job.error_message == "cancel token=[REDACTED]"
        assert "synthetic-sensitive-value" not in str(job.evidence_snapshot)
