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


def test_builds_whitelisted_hyperopt_command_without_shell() -> None:
    runner = FreqtradeCliRunner(binary="freqtrade")

    args = runner.build_args(
        FreqtradeCommand(
            command="hyperopt",
            options={
                "--config": Path("tmp/freqtrade_configs/hyperopt.json"),
                "--datadir": Path("user_data/data/okx"),
                "--epochs": 25,
                "--export": "trades",
                "--export-filename": Path("tmp/hyperopt/result.json"),
                "--hyperopt-loss": "SharpeHyperOptLoss",
                "--print-json": True,
                "--random-state": 42,
                "--spaces": ["buy", "sell"],
                "--strategy": "DemoStrategy",
                "--strategy-path": Path("tmp/strategies"),
                "--timeframe": "15m",
                "--timerange": "20240101-20240131",
                "--userdir": Path("tmp/user_data"),
            },
            timeout_seconds=30,
        )
    )

    assert args == [
        "freqtrade",
        "hyperopt",
        "--config",
        "tmp/freqtrade_configs/hyperopt.json",
        "--datadir",
        "user_data/data/okx",
        "--epochs",
        "25",
        "--export",
        "trades",
        "--export-filename",
        "tmp/hyperopt/result.json",
        "--hyperopt-loss",
        "SharpeHyperOptLoss",
        "--print-json",
        "--random-state",
        "42",
        "--spaces",
        "buy",
        "sell",
        "--strategy",
        "DemoStrategy",
        "--strategy-path",
        "tmp/strategies",
        "--timeframe",
        "15m",
        "--timerange",
        "20240101-20240131",
        "--userdir",
        "tmp/user_data",
    ]


def test_builds_whitelisted_dry_run_trade_command_without_shell() -> None:
    runner = FreqtradeCliRunner(binary="freqtrade")

    args = runner.build_args(
        FreqtradeCommand(
            command="trade",
            options={
                "--config": Path("tmp/freqtrade_configs/dry-run.json"),
                "--dry-run": True,
                "--loglevel": "INFO",
                "--strategy": "DemoStrategy",
                "--strategy-path": Path("user_data/strategies/generated"),
                "--userdir": Path("user_data"),
            },
            timeout_seconds=30,
        )
    )

    assert args == [
        "freqtrade",
        "trade",
        "--config",
        "tmp/freqtrade_configs/dry-run.json",
        "--dry-run",
        "--loglevel",
        "INFO",
        "--strategy",
        "DemoStrategy",
        "--strategy-path",
        "user_data/strategies/generated",
        "--userdir",
        "user_data",
    ]


def test_rejects_unsupported_command_and_option() -> None:
    runner = FreqtradeCliRunner()

    with pytest.raises(FreqtradeCommandValidationError):
        runner.build_args(FreqtradeCommand(command="trade"))

    with pytest.raises(FreqtradeCommandValidationError):
        runner.build_args(
            FreqtradeCommand(command="backtesting", options={"--enable-proxy": "true"})
        )


@pytest.mark.parametrize(
    "option",
    [
        "--api-key",
        "--download-data",
        "--dry-run",
        "--enable-proxy",
        "--live",
        "--webserver",
    ],
)
def test_rejects_unsafe_hyperopt_options(option: str) -> None:
    runner = FreqtradeCliRunner()

    with pytest.raises(FreqtradeCommandValidationError):
        runner.build_args(FreqtradeCommand(command="hyperopt", options={option: "true"}))


@pytest.mark.parametrize(
    "option",
    [
        "--api-key",
        "--download-data",
        "--live",
        "--real-orders",
        "--webserver",
    ],
)
def test_rejects_unsafe_dry_run_trade_options(option: str) -> None:
    runner = FreqtradeCliRunner()

    with pytest.raises(FreqtradeCommandValidationError):
        runner.build_args(
            FreqtradeCommand(
                command="trade",
                options={"--dry-run": True, option: "true"},
            )
        )


def test_rejects_dry_run_trade_without_enabled_dry_run_flag() -> None:
    runner = FreqtradeCliRunner()

    with pytest.raises(FreqtradeCommandValidationError, match="requires --dry-run"):
        runner.build_args(
            FreqtradeCommand(
                command="trade",
                options={"--config": Path("tmp/freqtrade_configs/dry-run.json")},
            )
        )

    with pytest.raises(FreqtradeCommandValidationError, match="requires --dry-run"):
        runner.build_args(
            FreqtradeCommand(
                command="trade",
                options={
                    "--config": Path("tmp/freqtrade_configs/dry-run.json"),
                    "--dry-run": False,
                },
            )
        )


def test_rejects_secret_shaped_command_values() -> None:
    runner = FreqtradeCliRunner()

    with pytest.raises(FreqtradeCommandValidationError, match="secret-shaped"):
        runner.build_args(
            FreqtradeCommand(
                command="trade",
                options={
                    "--config": "tmp/freqtrade_configs/dry-run.json?api_key=real",
                    "--dry-run": True,
                },
            )
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
