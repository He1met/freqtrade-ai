import json
from pathlib import Path
from typing import Optional

import pytest

from app.adapters.freqtrade.backtest_runner import FreqtradeBacktestArtifactManifest
from app.adapters.freqtrade.config_builder import FreqtradeConfigBuilder
from app.adapters.freqtrade.market_data_catalog import MarketDataCatalog
from app.services.backtest_matrix import BacktestMatrixExecutionService


def profile_payload(pair: str, timeframe: str = "15m", name: str = "fixture") -> dict:
    return {
        "profile_name": name,
        "pair": pair,
        "timeframe": timeframe,
        "timerange": "20240101-20240201",
        "strategy": {"name": "MvpRsiStrategy", "path": "user_data/strategies/generated"},
        "data_source": {
            "kind": "local",
            "exchange": "okx",
            "datadir": "user_data/data",
            "data_format": "feather",
        },
    }


class FakeMatrixRunner:
    def __init__(self, statuses: Optional[dict[str, str]] = None) -> None:
        self.statuses = statuses or {}
        self.calls: list[tuple[Path, str, Path, Path, Optional[Path]]] = []

    def run_backtest_with_artifact_manifest(
        self,
        config_path,
        strategy_name,
        result_path,
        manifest_path,
        timeout_seconds=None,
        datadir=None,
        strategy_path=None,
        userdir=None,
    ):
        self.calls.append((config_path, strategy_name, result_path, manifest_path, datadir))
        status = self.statuses.get(str(result_path), "SUCCESS")
        if status == "SUCCESS":
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text('{"strategy": {}}', encoding="utf-8")
            failed_reason = None
        else:
            failed_reason = "fixture backtest failed"

        manifest = FreqtradeBacktestArtifactManifest(
            manifest_version=1,
            status=status,
            config_path=config_path,
            strategy_name=strategy_name,
            result_path=result_path,
            manifest_path=manifest_path,
            command_args=["freqtrade", "backtesting"],
            return_code=0 if status == "SUCCESS" else 2,
            stdout="ok" if status == "SUCCESS" else "",
            stderr="" if status == "SUCCESS" else "failed",
            datadir=datadir,
            strategy_path=strategy_path,
            userdir=userdir,
            failed_reason=failed_reason,
        )
        manifest.write()
        return manifest


def test_executes_fixture_matrix_and_blocks_missing_data(tmp_path) -> None:
    market_data_dir = tmp_path / "user_data" / "data"
    okx_dir = market_data_dir / "okx"
    okx_dir.mkdir(parents=True)
    (okx_dir / "BTC_USDT_USDT-15m-futures.feather").write_bytes(b"candles")

    runner = FakeMatrixRunner()
    service = BacktestMatrixExecutionService(
        runner=runner,
        config_builder=FreqtradeConfigBuilder(default_output_dir=tmp_path / "configs"),
        market_data_catalog=MarketDataCatalog(market_data_dir=market_data_dir),
    )

    summary = service.execute_matrix(
        [
            profile_payload("BTC/USDT:USDT", name="btc_fixture"),
            profile_payload("ETH/USDT:USDT", name="eth_missing"),
        ],
        output_dir=tmp_path / "matrix",
    )

    assert summary.status == "BLOCKED"
    assert summary.total_tasks == 2
    assert summary.succeeded == 1
    assert summary.failed == 0
    assert summary.blocked == 1
    assert len(runner.calls) == 1
    assert runner.calls[0][4] == okx_dir
    blocked_task = summary.tasks[1]
    assert blocked_task.status == "BLOCKED"
    assert "no available local market data for okx ETH/USDT:USDT 15m" in (
        blocked_task.blocked_reason or ""
    )
    stored_summary = json.loads(summary.summary_path.read_text(encoding="utf-8"))
    assert stored_summary["status"] == "BLOCKED"
    blocked_manifest = json.loads(blocked_task.manifest_path.read_text(encoding="utf-8"))
    assert blocked_manifest["status"] == "BLOCKED"


def test_failed_task_makes_matrix_failed(tmp_path) -> None:
    market_data_dir = tmp_path / "user_data" / "data"
    okx_dir = market_data_dir / "okx"
    okx_dir.mkdir(parents=True)
    (okx_dir / "BTC_USDT_USDT-15m-futures.feather").write_bytes(b"candles")

    failing_result = tmp_path / "matrix" / "01-btc-fixture-btc-usdt-usdt-15m-result.json"
    runner = FakeMatrixRunner(statuses={str(failing_result): "FAILED"})
    service = BacktestMatrixExecutionService(
        runner=runner,
        market_data_catalog=MarketDataCatalog(market_data_dir=market_data_dir),
    )

    summary = service.execute_matrix(
        [profile_payload("BTC/USDT:USDT", name="btc_fixture")],
        output_dir=tmp_path / "matrix",
        config_dir=tmp_path / "configs",
    )

    assert summary.status == "FAILED"
    assert summary.succeeded == 0
    assert summary.failed == 1
    assert summary.tasks[0].failed_reason == "fixture backtest failed"


def test_matrix_rejects_unbounded_task_count(tmp_path) -> None:
    service = BacktestMatrixExecutionService(
        runner=FakeMatrixRunner(),
        market_data_catalog=MarketDataCatalog(market_data_dir=tmp_path / "data"),
    )

    with pytest.raises(ValueError, match="limited to 1 tasks"):
        service.execute_matrix(
            [
                profile_payload("BTC/USDT:USDT", name="one"),
                profile_payload("ETH/USDT:USDT", name="two"),
            ],
            output_dir=tmp_path / "matrix",
            max_tasks=1,
        )
