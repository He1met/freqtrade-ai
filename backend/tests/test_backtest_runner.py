import subprocess
from pathlib import Path

import pytest

from app.adapters.freqtrade.backtest_runner import FreqtradeBacktestRunner
from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner


def test_backtest_runner_invokes_safe_cli_with_export_file() -> None:
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append((args, cwd, timeout_seconds))
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    runner = FreqtradeBacktestRunner(FreqtradeCliRunner(executor=fake_executor))
    result_path = runner.run_backtest(
        Path("tmp/freqtrade_configs/backtest.json"),
        "MvpRsiStrategy",
        result_path=Path("reports/backtests/result.json"),
        timeout_seconds=60,
    )

    assert result_path == Path("reports/backtests/result.json")
    assert calls == [
        (
            [
                "freqtrade",
                "backtesting",
                "--config",
                "tmp/freqtrade_configs/backtest.json",
                "--export",
                "trades",
                "--export-filename",
                "reports/backtests/result.json",
                "--strategy",
                "MvpRsiStrategy",
            ],
            None,
            60,
        )
    ]


def test_backtest_runner_requires_result_path() -> None:
    def fake_executor(args, cwd, timeout_seconds):
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    runner = FreqtradeBacktestRunner(FreqtradeCliRunner(executor=fake_executor))

    with pytest.raises(ValueError, match="result_path is required"):
        runner.run_backtest(Path("tmp/config.json"), "MvpRsiStrategy")
