import json
from pathlib import Path

import pytest

from app.adapters.freqtrade.config_builder import FreqtradeConfigBuilder
from app.adapters.freqtrade.exceptions import FreqtradeConfigError
from app.schemas.dry_run_profile import DryRunProfile
from app.schemas.backtest_profile import BacktestProfileV2


def test_builds_sanitized_backtest_config_file(tmp_path) -> None:
    builder = FreqtradeConfigBuilder(default_output_dir=tmp_path)

    config_path = builder.build_backtest_config(
        {
            "profile_name": "quick_filter",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "timerange": "20240101-20240201",
            "strategy": "MvpRsiStrategy",
            "strategy_path": "user_data/strategies/generated",
            "exchange": {
                "name": "okx",
                "trading_mode": "futures",
                "margin_mode": "isolated",
                "credentials": {
                    "api_key_env": "OKX_DEMO_API_KEY",
                    "api_secret_env": "OKX_DEMO_API_SECRET",
                    "api_passphrase_env": "OKX_DEMO_API_PASSPHRASE",
                },
            },
        }
    )

    config = json.loads(config_path.read_text())

    assert config_path.parent == tmp_path
    assert config_path.name == "quick-filter-mvprsistrategy-btc-usdt-usdt-15m.json"
    assert config["exchange"] == {
        "name": "okx",
        "pair_whitelist": ["BTC/USDT:USDT"],
        "pair_blacklist": [],
    }
    assert config["timeframe"] == "15m"
    assert config["timerange"] == "20240101-20240201"
    assert config["trading_mode"] == "futures"
    assert config["margin_mode"] == "isolated"
    assert config["unfilledtimeout"] == {
        "entry": 10,
        "exit": 10,
        "exit_timeout_count": 0,
        "unit": "minutes",
    }
    assert config["entry_pricing"]["price_side"] == "same"
    assert config["entry_pricing"]["use_order_book"] is True
    assert config["entry_pricing"]["check_depth_of_market"]["enabled"] is False
    assert config["exit_pricing"] == {
        "price_side": "same",
        "use_order_book": True,
        "order_book_top": 1,
    }
    serialized = json.dumps(config).lower()
    assert "okx_demo_api" not in serialized
    assert "api_key" not in serialized
    assert "api_secret" not in serialized
    assert "passphrase" not in serialized


def test_rejects_missing_required_fields(tmp_path) -> None:
    builder = FreqtradeConfigBuilder(default_output_dir=tmp_path)

    with pytest.raises(FreqtradeConfigError):
        builder.build_backtest_config({"pair": "BTC/USDT", "timeframe": "15m"})


def test_rejects_forbidden_secret_keys_in_generated_config(tmp_path) -> None:
    builder = FreqtradeConfigBuilder(default_output_dir=tmp_path)

    with pytest.raises(FreqtradeConfigError):
        builder._reject_secret_keys({"exchange": {"secret": "do-not-write"}})


def test_builds_config_from_backtest_profile_v2(tmp_path) -> None:
    builder = FreqtradeConfigBuilder(default_output_dir=tmp_path)
    profile = BacktestProfileV2.model_validate(
        {
            "profile_name": "phase3_baseline",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "timerange": "20240101-20240201",
            "strategy": {
                "name": "MvpRsiStrategy",
                "path": "user_data/strategies/generated",
            },
            "stake": {
                "currency": "USDT",
                "amount": 125,
                "tradable_balance_ratio": 0.95,
                "max_open_trades": 2,
            },
            "data_source": {
                "kind": "local",
                "exchange": "okx",
                "datadir": "user_data/data/okx",
                "trading_mode": "futures",
                "margin_mode": "isolated",
            },
        }
    )

    config = builder.build_backtest_config_dict(profile)
    config_path = builder.build_backtest_config(profile.to_snapshot())

    assert config["strategy"] == "MvpRsiStrategy"
    assert config["strategy_path"] == "user_data/strategies/generated"
    assert config["exchange"]["name"] == "okx"
    assert config["exchange"]["pair_whitelist"] == ["BTC/USDT:USDT"]
    assert config["trading_mode"] == "futures"
    assert config["margin_mode"] == "isolated"
    assert config["datadir"] == "user_data/data/okx"
    assert config["stake_currency"] == "USDT"
    assert config["stake_amount"] == 125
    assert config["tradable_balance_ratio"] == 0.95
    assert config["max_open_trades"] == 2
    assert config_path.name == "phase3-baseline-mvprsistrategy-btc-usdt-usdt-15m.json"


def test_builds_sanitized_dry_run_config_file_with_env_only_preflight(tmp_path) -> None:
    builder = FreqtradeConfigBuilder(default_output_dir=tmp_path)
    profile = DryRunProfile.model_validate(valid_dry_run_profile_payload())

    result = builder.build_dry_run_config(
        profile,
        environ={
            "FREQTRADE_DRY_RUN_API_KEY": "real-key-that-must-not-render",
            "FREQTRADE_DRY_RUN_API_SECRET": "real-secret-that-must-not-render",
            "FREQTRADE_DRY_RUN_API_PASSPHRASE": "real-passphrase-that-must-not-render",
        },
    )

    config = json.loads(result.config_path.read_text())
    serialized_config = json.dumps(config, sort_keys=True)
    serialized_report = json.dumps(result.env_preflight.to_report(), sort_keys=True)

    assert result.config_path.parent == tmp_path
    assert result.config_path.name == (
        "phase5-local-dry-run-mvprsistrategy-btc-usdt-usdt-15m-dry-run.json"
    )
    assert result.env_preflight.status == "READY"
    assert result.env_preflight.required_env_present == (
        "FREQTRADE_DRY_RUN_API_KEY",
        "FREQTRADE_DRY_RUN_API_SECRET",
    )
    assert config["dry_run"] is True
    assert config["initial_state"] == "stopped"
    assert config["exchange"] == {
        "name": "okx",
        "pair_whitelist": ["BTC/USDT:USDT"],
        "pair_blacklist": [],
    }
    assert config["strategy"] == "MvpRsiStrategy"
    assert config["strategy_path"] == "user_data/strategies/generated"
    assert "real-key-that-must-not-render" not in serialized_config
    assert "real-secret-that-must-not-render" not in serialized_config
    assert "real-passphrase-that-must-not-render" not in serialized_config
    assert "FREQTRADE_DRY_RUN_API_KEY" not in serialized_config
    assert "api_key" not in serialized_config.lower()
    assert "api_secret" not in serialized_config.lower()
    assert "real-key-that-must-not-render" not in serialized_report
    assert "real-secret-that-must-not-render" not in serialized_report


def test_dry_run_config_reports_blocked_missing_env_without_secret_values(tmp_path) -> None:
    builder = FreqtradeConfigBuilder(default_output_dir=tmp_path)

    result = builder.build_dry_run_config(
        valid_dry_run_profile_payload(),
        environ={"FREQTRADE_DRY_RUN_API_KEY": "real-key-that-must-not-render"},
    )

    report = result.env_preflight.to_report()
    serialized_report = json.dumps(report, sort_keys=True)

    assert result.env_preflight.status == "BLOCKED"
    assert report["required_env_present"] == ["FREQTRADE_DRY_RUN_API_KEY"]
    assert report["required_env_missing"] == ["FREQTRADE_DRY_RUN_API_SECRET"]
    assert report["blocked_reason"] == (
        "required ENV variables are missing or empty: FREQTRADE_DRY_RUN_API_SECRET"
    )
    assert "real-key-that-must-not-render" not in serialized_report
    assert result.config_path.exists()


def test_dry_run_config_rejects_non_controlled_output_dir(tmp_path) -> None:
    builder = FreqtradeConfigBuilder(default_output_dir=tmp_path)

    with pytest.raises(FreqtradeConfigError, match="controlled tmp config directory"):
        builder.build_dry_run_config(
            valid_dry_run_profile_payload(),
            output_dir=Path.home() / ".freqtrade",
            environ={
                "FREQTRADE_DRY_RUN_API_KEY": "key",
                "FREQTRADE_DRY_RUN_API_SECRET": "secret",
            },
        )


def test_dry_run_env_preflight_rejects_invalid_env_names(tmp_path) -> None:
    builder = FreqtradeConfigBuilder(default_output_dir=tmp_path)

    with pytest.raises(FreqtradeConfigError, match="Invalid ENV variable name"):
        builder.check_dry_run_env(
            environ={},
            required_env_vars=("FREQTRADE_DRY_RUN_API_KEY;printenv",),
        )


def valid_dry_run_profile_payload() -> dict:
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
