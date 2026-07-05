import json
import os
from pathlib import Path

import pytest

from app.spikes.real_freqtrade_backtest import (
    SpikeConfig,
    find_freqtrade_binary,
    infer_trading_mode,
    parse_required_metrics,
    run_spike,
    select_market_data_file,
)


def test_find_freqtrade_binary_reports_missing_explicit_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    assert find_freqtrade_binary(str(tmp_path / "missing-freqtrade")) is None


def test_spike_blocks_when_market_data_is_missing(tmp_path) -> None:
    freqtrade_bin = tmp_path / "freqtrade"
    freqtrade_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    freqtrade_bin.chmod(0o755)

    report = run_spike(
        SpikeConfig(
            tmp_dir=tmp_path / "work",
            report_path=tmp_path / "report.md",
            market_data_dir=tmp_path / "empty-data",
            freqtrade_binary=str(freqtrade_bin),
        )
    )

    assert report.status == "BLOCKED"
    assert any("no local market data" in blocker for blocker in report.blockers)
    assert report.manifest_path is not None
    manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "BLOCKED"
    assert "no local market data" in manifest["blocked_reason"]
    assert manifest["strategy_name"] == "MvpRsiStrategy"
    assert report.report_path is not None
    report_text = report.report_path.read_text(encoding="utf-8")
    assert "Status: BLOCKED" in report_text
    assert "Artifact manifest:" in report_text


def test_select_market_data_file_uses_existing_local_files(tmp_path) -> None:
    data_file = tmp_path / "okx" / "BTC_USDT_USDT-15m-futures.feather"
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"fixture")

    selected = select_market_data_file(tmp_path)

    assert selected is not None
    assert selected.exchange == "okx"
    assert selected.pair == "BTC/USDT:USDT"
    assert selected.timeframe == "15m"
    assert infer_trading_mode(selected) == "futures"


def test_parse_required_metrics_extracts_real_result_summary(tmp_path) -> None:
    result_path = tmp_path / "backtest-result.json"
    result_path.write_text(
        json.dumps(
            {
                "strategy": {
                    "MvpRsiStrategy": {
                        "profit_total_abs": 12.5,
                        "max_drawdown_pct": 3.0,
                        "wins": 6,
                        "losses": 3,
                        "draws": 1,
                        "total_trades": 10,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    metrics = parse_required_metrics(result_path, "MvpRsiStrategy")

    assert metrics == {
        "profit_total": 12.5,
        "max_drawdown_pct": 0.03,
        "total_trades": 10,
        "win_rate": pytest.approx(0.6),
    }


def test_parse_required_metrics_reports_missing_metric(tmp_path) -> None:
    result_path = tmp_path / "backtest-result.json"
    result_path.write_text(
        json.dumps({"strategy": {"MvpRsiStrategy": {"total_trades": 0}}}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="missing required metrics"):
        parse_required_metrics(result_path, "MvpRsiStrategy")
