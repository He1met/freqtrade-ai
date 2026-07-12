from __future__ import annotations

import json
import os
import subprocess
import zipfile
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.freqtrade.backtest_runner import FreqtradeBacktestRunner
from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner
from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.api.strategy_generation import get_deepseek_backtest_loop_service
from app.db.session import create_database_engine, create_session_factory
from app.main import app
from app.models import (
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    Strategy,
    StrategyGenerationRun,
    StrategyScore,
    StrategyVersion,
)
from app.services.deepseek_backtest_loop import DeepSeekBacktestLoopService
from app.services.strategy_generation import (
    LLMProviderConfig,
    OpenAICompatibleStrategyBlueprintProvider,
    StrategyGenerationService,
)


def blueprint_payload(slug: str = "loop-deepseek-rsi") -> dict:
    return {
        "schema_version": "2",
        "name": "Loop DeepSeek RSI Strategy",
        "slug": slug,
        "class_name": "LoopDeepseekRsiStrategy",
        "description": "Mocked real-provider response for the minimal deepseek backtest loop.",
        "indicators": [{"name": "rsi", "kind": "rsi", "period": 14}],
        "entry_rules": [{"indicator": "rsi", "operator": "<", "value": 32}],
        "exit_rules": [{"indicator": "rsi", "operator": ">", "value": 68}],
        "tags": ["phase-9", "real-provider-mock"],
    }


class MockLLMResponse:
    def __init__(self, body: dict) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.body


class MockLLMClient:
    def __init__(self, response: MockLLMResponse) -> None:
        self.response = response
        self.requests: list[dict] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict, timeout: float) -> MockLLMResponse:
        self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self.response


def write_market_data(tmp_path: Path) -> Path:
    datadir = tmp_path / "user_data" / "data"
    exchange_dir = datadir / "okx" / "futures"
    exchange_dir.mkdir(parents=True)
    exchange_dir.joinpath("BTC_USDT_USDT-15m-futures.feather").write_bytes(b"local candles")
    return datadir


def install_fake_freqtrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "freqtrade"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("FREQTRADE_BINARY", raising=False)
    return binary


def local_profile(datadir: Path) -> dict:
    return {
        "schema_version": "2",
        "profile_name": "phase9-loop-local",
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "timerange": "20240101-20240201",
        "strategy": {"name": "LoopDeepseekRsiStrategy"},
        "data_source": {
            "kind": "local",
            "exchange": "okx",
            "datadir": str(datadir),
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
    }


def build_service(
    tmp_path: Path,
    session_factory,
    http_client: MockLLMClient,
    *,
    fake_executor=None,
) -> DeepSeekBacktestLoopService:
    db = session_factory()
    output_dir = tmp_path / "strategies"
    output_dir.mkdir(exist_ok=True)
    provider = OpenAICompatibleStrategyBlueprintProvider(
        LLMProviderConfig(
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key_env="TEST_LLM_API_KEY",
        ),
        http_client=http_client,
    )
    generation_service = StrategyGenerationService(
        db,
        provider=provider,
        file_manager=StrategyFileManager(output_dir=output_dir, approved_roots=[output_dir]),
    )
    return DeepSeekBacktestLoopService(
        db,
        generation_service=generation_service,
        backtest_runner=FreqtradeBacktestRunner(FreqtradeCliRunner(executor=fake_executor))
        if fake_executor is not None
        else FreqtradeBacktestRunner(FreqtradeCliRunner()),
    )


def client_with_loop_service(
    tmp_path: Path,
    http_client: MockLLMClient,
    *,
    fake_executor=None,
) -> tuple[TestClient, object]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'deepseek-loop.sqlite'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    def override_service() -> Generator[DeepSeekBacktestLoopService, None, None]:
        service = build_service(
            tmp_path,
            session_factory,
            http_client,
            fake_executor=fake_executor,
        )
        try:
            yield service
        finally:
            service.db.close()

    app.dependency_overrides[get_deepseek_backtest_loop_service] = override_service
    return TestClient(
        app,
        headers={
            "X-Operator-Token": "synthetic-test-operator-token",
            "Idempotency-Key": "deepseek-loop-test",
            "X-Provider-Authorization": "once",
        },
    ), session_factory


def test_deepseek_backtest_loop_blocks_without_explicit_real_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-secret-value")
    http_client = MockLLMClient(MockLLMResponse({"blueprints": [blueprint_payload()]}))
    client, session_factory = client_with_loop_service(tmp_path, http_client)
    datadir = write_market_data(tmp_path)
    try:
        response = client.post(
            "/api/strategy-generation-runs/deepseek-single/backtest-loop",
            json={
                "prompt_summary": "Generate one DeepSeek strategy and run the local backtest loop.",
                "allow_real_call": False,
                "backtest_profile": local_profile(datadir),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["overall_status"] == "blocked"
    assert payload["generation"] is None
    assert payload["generation_run"]["provider"] == "deepseek"
    assert payload["generation_run"]["status"] == "failed"
    assert payload["evidence"]["status"] == "BLOCKED"
    assert payload["evidence"]["ids"]["strategy_generation_run_id"] == payload["generation_run"]["id"]
    assert payload["backtest"] is None
    assert payload["execution"] is None
    assert payload["artifact_ingest"] is None
    assert http_client.requests == []

    with session_factory() as db:
        assert db.query(StrategyGenerationRun).count() == 1
        assert db.query(Strategy).count() == 0
        assert db.query(StrategyVersion).count() == 0
        assert db.query(BacktestRun).count() == 0
        assert db.query(BacktestTask).count() == 0
        assert db.query(BacktestResult).count() == 0
        assert db.query(StrategyScore).count() == 0


def test_deepseek_backtest_loop_succeeds_with_mock_provider_and_fake_freqtrade_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-secret-value")
    datadir = write_market_data(tmp_path)
    install_fake_freqtrade(tmp_path, monkeypatch)
    http_client = MockLLMClient(MockLLMResponse({"blueprints": [blueprint_payload()]}))
    observed_args = []

    def fake_executor(args, cwd, timeout_seconds):
        observed_args.extend(args)
        result_dir = Path(args[args.index("--backtest-directory") + 1])
        result_dir.mkdir(parents=True, exist_ok=True)
        zip_path = result_dir / "backtest-result-2026-07-11_12-00-00.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr(
                "backtest-result-2026-07-11_12-00-00.json",
                json.dumps(
                    {
                        "strategy": {
                            "LoopDeepseekRsiStrategy": {
                                "profit_total_abs": 123.4,
                                "profit_total_pct": 12.5,
                                "max_drawdown_pct": 4.2,
                                "winrate": 61.0,
                                "total_trades": 42,
                                "timerange": "20240101-20240201",
                            }
                        }
                    }
                ),
            )
            archive.writestr("backtest-result-2026-07-11_12-00-00_config.json", "{}")
        (result_dir / ".last_result.json").write_text(
            json.dumps({"latest_backtest": zip_path.name}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="backtesting complete",
            stderr="",
        )

    client, session_factory = client_with_loop_service(
        tmp_path,
        http_client,
        fake_executor=fake_executor,
    )
    try:
        request_payload = {
            "prompt_summary": "Generate one DeepSeek strategy and run the local backtest loop.",
            "allow_real_call": True,
            "backtest_profile": local_profile(datadir),
            "timeout_seconds": 60,
        }
        response = client.post(
            "/api/strategy-generation-runs/deepseek-single/backtest-loop",
            json=request_payload,
        )
        replay_response = client.post(
            "/api/strategy-generation-runs/deepseek-single/backtest-loop",
            json=request_payload,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert replay_response.status_code == 200
    assert replay_response.json() == payload
    assert payload["overall_status"] == "succeeded"
    assert payload["generation_run"]["provider"] == "deepseek"
    assert payload["generation"]["run"]["status"] == "succeeded"
    assert payload["generation"]["strategy_versions"][0]["data_source"]["core_data"] is True
    assert payload["backtest"]["preflight_status"] == "ready"
    assert payload["execution"]["status"] == "succeeded"
    assert payload["artifact_ingest"]["ingest_status"] == "succeeded"
    assert payload["artifact_ingest"]["score"]["data_source"]["core_data"] is True
    assert payload["evidence"]["status"] == "SUCCESS"
    assert payload["evidence"]["acceptance_ready"] is True
    assert payload["evidence"]["ids"]["strategy_generation_run_id"] == payload["generation_run"]["id"]
    assert payload["evidence"]["ids"]["strategy_version_id"] == payload["generation"]["strategy_versions"][0]["id"]
    assert payload["evidence"]["ids"]["backtest_run_id"] == payload["backtest"]["run"]["id"]
    assert payload["evidence"]["ids"]["backtest_task_id"] == payload["backtest"]["tasks"][0]["id"]
    assert payload["evidence"]["ids"]["backtest_result_id"] == payload["artifact_ingest"]["result"]["id"]
    assert payload["evidence"]["ids"]["strategy_score_id"] == payload["artifact_ingest"]["score"]["id"]
    assert payload["evidence"]["artifact_refs"]["artifact_manifest_path"].endswith("artifact-manifest.json")
    assert payload["evidence"]["artifact_refs"]["backtest_result_path"].endswith("backtest-result.json")
    assert len(http_client.requests) == 1
    assert http_client.requests[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert "test-secret-value" not in json.dumps(payload)
    assert observed_args[observed_args.index("--datadir") + 1] == str(datadir / "okx")

    with session_factory() as db:
        assert db.query(StrategyGenerationRun).count() == 1
        assert db.query(Strategy).count() == 1
        assert db.query(StrategyVersion).count() == 1
        assert db.query(BacktestRun).count() == 1
        assert db.query(BacktestTask).count() == 1
        assert db.query(BacktestResult).count() == 1
        assert db.query(StrategyScore).count() == 1
        run = db.query(BacktestRun).one()
        task = db.query(BacktestTask).one()
        assert run.status == "succeeded"
        assert task.status == "succeeded"


def test_deepseek_backtest_loop_blocks_when_preflight_dependencies_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-secret-value")
    missing_datadir = tmp_path / "missing-data"
    install_fake_freqtrade(tmp_path, monkeypatch)
    http_client = MockLLMClient(MockLLMResponse({"blueprints": [blueprint_payload("loop-preflight-blocked")]}))

    def fake_executor(args, cwd, timeout_seconds):
        raise AssertionError("freqtrade executor must not run when preflight is blocked")

    client, session_factory = client_with_loop_service(
        tmp_path,
        http_client,
        fake_executor=fake_executor,
    )
    try:
        response = client.post(
            "/api/strategy-generation-runs/deepseek-single/backtest-loop",
            json={
                "prompt_summary": "Generate one DeepSeek strategy and stop when local data is missing.",
                "allow_real_call": True,
                "backtest_profile": local_profile(missing_datadir),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["overall_status"] == "blocked"
    assert payload["generation"]["run"]["status"] == "succeeded"
    assert payload["backtest"]["preflight_status"] == "blocked"
    assert payload["execution"] is None
    assert payload["artifact_ingest"] is None
    assert payload["evidence"]["status"] == "BLOCKED"
    assert "market data directory does not exist" in payload["evidence"]["blocked_reason"]
    assert len(http_client.requests) == 1

    with session_factory() as db:
        assert db.query(StrategyGenerationRun).count() == 1
        assert db.query(Strategy).count() == 1
        assert db.query(StrategyVersion).count() == 1
        assert db.query(BacktestRun).count() == 1
        assert db.query(BacktestTask).count() == 1
        assert db.query(BacktestResult).count() == 0
        assert db.query(StrategyScore).count() == 0
        assert db.query(BacktestRun).one().status == "blocked"
        assert db.query(BacktestTask).one().status == "blocked"
