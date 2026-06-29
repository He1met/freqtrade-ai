import subprocess
from pathlib import Path

import pytest

from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner, FreqtradeCommand
from app.adapters.freqtrade.exceptions import FreqtradeCommandError, FreqtradeCommandValidationError


def test_builds_whitelisted_backtesting_command_without_shell() -> None:
    runner = FreqtradeCliRunner(binary="freqtrade")

    args = runner.build_args(
        FreqtradeCommand(
            command="backtesting",
            options={
                "--config": Path("tmp/freqtrade_configs/demo.json"),
                "--pairs": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
                "--strategy": "DemoStrategy",
                "--timeframe": "15m",
            },
            timeout_seconds=30,
        )
    )

    assert args == [
        "freqtrade",
        "backtesting",
        "--config",
        "tmp/freqtrade_configs/demo.json",
        "--pairs",
        "BTC/USDT:USDT",
        "--pairs",
        "ETH/USDT:USDT",
        "--strategy",
        "DemoStrategy",
        "--timeframe",
        "15m",
    ]


def test_rejects_unsupported_command_and_option() -> None:
    runner = FreqtradeCliRunner()

    with pytest.raises(FreqtradeCommandValidationError):
        runner.build_args(FreqtradeCommand(command="trade"))

    with pytest.raises(FreqtradeCommandValidationError):
        runner.build_args(
            FreqtradeCommand(command="backtesting", options={"--enable-proxy": "true"})
        )


def test_rejects_multiline_values() -> None:
    runner = FreqtradeCliRunner()

    with pytest.raises(FreqtradeCommandValidationError):
        runner.build_args(
            FreqtradeCommand(command="backtesting", options={"--strategy": "Demo\nStrategy"})
        )


def test_run_uses_injected_executor_and_maps_failure() -> None:
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append((args, cwd, timeout_seconds))
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="ok", stderr="")

    runner = FreqtradeCliRunner(binary="freqtrade", executor=fake_executor)
    result = runner.run(
        FreqtradeCommand(
            command="list-data",
            options={"--exchange": "okx", "--show-timerange": True},
            cwd=Path("/tmp/project"),
            timeout_seconds=5,
        )
    )

    assert result.return_code == 0
    assert result.stdout == "ok"
    assert calls == [
        (
            ["freqtrade", "list-data", "--exchange", "okx", "--show-timerange"],
            Path("/tmp/project"),
            5,
        )
    ]

    def failing_executor(args, cwd, timeout_seconds):
        return subprocess.CompletedProcess(args=list(args), returncode=2, stdout="", stderr="bad")

    with pytest.raises(FreqtradeCommandError):
        FreqtradeCliRunner(executor=failing_executor).run(FreqtradeCommand(command="list-data"))
