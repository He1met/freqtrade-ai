import subprocess
from pathlib import Path

from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner
from app.adapters.freqtrade.dry_run_runner import FreqtradeDryRunRunner
from app.schemas.dry_run_profile import DryRunProfile


def valid_profile_payload() -> dict:
    return {
        "name": "phase5-local-dry-run",
        "description": "Local dry-run profile for one candidate strategy.",
        "strategy": {
            "version_id": 123,
            "name": "MvpRsiStrategy",
            "file_path": "user_data/strategies/generated/MvpRsiStrategy.py",
        },
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "stake": {
            "currency": "USDT",
            "amount": 100,
            "tradable_balance_ratio": 0.99,
            "max_open_trades": 1,
        },
        "exchange": {"name": "okx", "trading_mode": "futures"},
        "freq_ui": {
            "enabled": True,
            "base_url": "http://127.0.0.1:8080",
            "environment_label": "local-dry-run",
        },
        "command_options": {
            "user_data_dir": "user_data",
            "strategy_path": "user_data/strategies/generated",
            "log_level": "INFO",
        },
        "locked_variables": {
            "profile_name": "phase5-local-dry-run",
            "strategy_version_id": 123,
            "strategy": "MvpRsiStrategy",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "exchange": "okx",
            "stake_currency": "USDT",
            "stake_amount": 100,
            "max_open_trades": 1,
            "dry_run": True,
            "freq_ui_enabled": True,
        },
        "tags": ["phase-5", "local-only"],
    }


def test_dry_run_runner_builds_controlled_trade_plan_from_profile() -> None:
    profile = DryRunProfile.model_validate(valid_profile_payload())
    runner = FreqtradeDryRunRunner(FreqtradeCliRunner(binary="freqtrade"))

    plan = runner.build_dry_run_plan(
        profile=profile,
        config_path=Path("tmp/freqtrade_configs/dry-run.json"),
        timeout_seconds=45,
    )

    assert plan.profile_name == "phase5-local-dry-run"
    assert plan.strategy_version_id == 123
    assert plan.pair == "BTC/USDT:USDT"
    assert plan.timeframe == "15m"
    assert plan.userdir == Path("user_data")
    assert plan.strategy_path == Path("user_data/strategies/generated")
    assert plan.command_args == [
        "freqtrade",
        "trade",
        "--config",
        "tmp/freqtrade_configs/dry-run.json",
        "--dry-run",
        "--loglevel",
        "INFO",
        "--strategy",
        "MvpRsiStrategy",
        "--strategy-path",
        "user_data/strategies/generated",
        "--userdir",
        "user_data",
    ]


def test_dry_run_runner_uses_fake_executor_and_captures_success_output() -> None:
    calls = []

    def fake_executor(args, cwd, timeout_seconds):
        calls.append((list(args), cwd, timeout_seconds))
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="dry-run started",
            stderr="",
        )

    profile = DryRunProfile.model_validate(valid_profile_payload())
    runner = FreqtradeDryRunRunner(FreqtradeCliRunner(executor=fake_executor))

    execution = runner.run_dry_run_with_output(
        profile=profile,
        config_path=Path("tmp/freqtrade_configs/dry-run.json"),
        timeout_seconds=45,
    )

    assert execution.command_result.return_code == 0
    assert execution.command_result.stdout == "dry-run started"
    assert execution.command_result.stderr == ""
    assert execution.plan.command_args == calls[0][0]
    assert calls[0][1] is None
    assert calls[0][2] == 45


def test_dry_run_runner_captures_failed_fake_executor_output_without_raising() -> None:
    def fake_executor(args, cwd, timeout_seconds):
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=2,
            stdout="dry-run stdout",
            stderr="dry-run stderr",
        )

    profile = DryRunProfile.model_validate(valid_profile_payload())
    runner = FreqtradeDryRunRunner(FreqtradeCliRunner(executor=fake_executor))

    execution = runner.run_dry_run_with_output(
        profile=profile,
        config_path=Path("tmp/freqtrade_configs/dry-run.json"),
    )

    assert execution.command_result.return_code == 2
    assert execution.command_result.stdout == "dry-run stdout"
    assert execution.command_result.stderr == "dry-run stderr"
