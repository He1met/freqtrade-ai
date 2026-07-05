import json
from pathlib import Path

from app.spikes.real_freqtrade_backtest import SpikeConfig, run_spike


FAKE_FREQTRADE = """#!/bin/sh
if [ "$1" = "list-data" ]; then
  echo "BTC/USDT:USDT 15m 20240101-20240201"
  exit 0
fi
if [ "$1" = "backtesting" ]; then
  result_dir=""
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--backtest-directory" ]; then
      shift
      result_dir="$1"
    fi
    shift
  done
  mkdir -p "$result_dir"
  cat > "$result_dir/backtest-result-2026-07-05_18-44-00.json" <<'JSON'
{"strategy":{"MvpRsiStrategy":{"profit_total_abs":4.2,"max_drawdown_pct":2.0,"wins":3,"losses":1,"draws":0,"total_trades":4}}}
JSON
  cat > "$result_dir/.last_result.json" <<'JSON'
{"latest_backtest":"backtest-result-2026-07-05_18-44-00.json"}
JSON
  echo "backtesting complete"
  exit 0
fi
echo "unsupported command" >&2
exit 9
"""


def test_phase3_baseline_blocks_without_local_market_data(tmp_path) -> None:
    freqtrade_bin = tmp_path / "freqtrade"
    freqtrade_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    freqtrade_bin.chmod(0o755)

    report = run_spike(
        SpikeConfig(
            tmp_dir=tmp_path / "work",
            report_path=tmp_path / "phase3-report.md",
            market_data_dir=tmp_path / "empty-data",
            freqtrade_binary=str(freqtrade_bin),
            report_title="Phase 3 Local Data Availability and Backtest Baseline Report",
            profile_name="phase3_local_data_baseline",
            bot_name="freqtrade_ai_phase3_baseline",
            strategy_prompt="Generate one Phase 3 local backtesting baseline strategy.",
        )
    )

    assert report.status == "BLOCKED"
    assert report.return_code is None
    assert report.list_data_return_code is None
    assert report.manifest_path is not None
    blocked_manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    assert blocked_manifest["status"] == "BLOCKED"
    assert "no local market data files found" in blocked_manifest["blocked_reason"]
    report_text = report.report_path.read_text(encoding="utf-8")
    assert "Phase 3 Local Data Availability" in report_text
    assert "no local market data files found" in report_text
    assert "Result JSON: not available" in report_text
    assert "Artifact manifest:" in report_text


def test_phase3_baseline_records_timerange_backtest_artifacts_and_metrics(tmp_path) -> None:
    freqtrade_bin = tmp_path / "freqtrade"
    freqtrade_bin.write_text(FAKE_FREQTRADE, encoding="utf-8")
    freqtrade_bin.chmod(0o755)
    data_file = tmp_path / "data" / "okx" / "BTC_USDT_USDT-15m-futures.feather"
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"local candles")

    report = run_spike(
        SpikeConfig(
            tmp_dir=tmp_path / "work",
            report_path=tmp_path / "phase3-report.md",
            market_data_dir=tmp_path / "data",
            freqtrade_binary=str(freqtrade_bin),
            report_title="Phase 3 Local Data Availability and Backtest Baseline Report",
            profile_name="phase3_local_data_baseline",
            bot_name="freqtrade_ai_phase3_baseline",
            strategy_prompt="Generate one Phase 3 local backtesting baseline strategy.",
        )
    )

    assert report.status == "SUCCESS"
    assert report.list_data_return_code == 0
    assert report.return_code == 0
    assert report.config_path is not None
    assert report.strategy_file is not None
    assert report.result_path is not None
    assert report.manifest_path is not None
    assert report.metrics["profit_total"] == 4.2
    assert "20240101-20240201" in report.list_data_stdout
    assert "--trading-mode" in report.list_data_args
    assert "--timeframes" not in report.list_data_args

    config = json.loads(Path(report.config_path).read_text(encoding="utf-8"))
    assert config["bot_name"] == "freqtrade_ai_phase3_baseline"
    assert config["trading_mode"] == "futures"
    assert config["margin_mode"] == "isolated"
    assert Path(config["user_data_dir"]).is_dir()
    assert "api_key" not in json.dumps(config).lower()

    report_text = report.report_path.read_text(encoding="utf-8")
    assert "Timerange Check" in report_text
    assert "BTC/USDT:USDT" in report_text
    assert "backtesting complete" in report_text
    assert "Artifact manifest:" in report_text

    manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "SUCCESS"
    assert manifest["result_path"] == str(report.result_path)
    assert manifest["manifest_path"] == str(report.manifest_path)
    assert manifest["blocked_reason"] is None
    assert manifest["failed_reason"] is None
