import json

import pytest

from app.adapters.freqtrade.config_builder import FreqtradeConfigBuilder
from app.adapters.freqtrade.exceptions import FreqtradeConfigError
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
            },
        }
    )

    config = builder.build_backtest_config_dict(profile)
    config_path = builder.build_backtest_config(profile.to_snapshot())

    assert config["strategy"] == "MvpRsiStrategy"
    assert config["strategy_path"] == "user_data/strategies/generated"
    assert config["exchange"]["name"] == "okx"
    assert config["exchange"]["pair_whitelist"] == ["BTC/USDT:USDT"]
    assert config["datadir"] == "user_data/data/okx"
    assert config["stake_currency"] == "USDT"
    assert config["stake_amount"] == 125
    assert config["tradable_balance_ratio"] == 0.95
    assert config["max_open_trades"] == 2
    assert config_path.name == "phase3-baseline-mvprsistrategy-btc-usdt-usdt-15m.json"
