import pytest
from pydantic import ValidationError

from app.schemas.backtest_profile import BacktestProfileV2


def valid_profile_payload() -> dict:
    return {
        "profile_name": "phase3_baseline",
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "timerange": "20240101-20240201",
        "strategy": {"name": "MvpRsiStrategy", "path": "user_data/strategies/generated"},
        "stake": {
            "currency": "USDT",
            "amount": 100,
            "tradable_balance_ratio": 0.99,
            "max_open_trades": 1,
        },
        "data_source": {
            "kind": "local",
            "exchange": "okx",
            "datadir": "user_data/data",
            "data_format": "feather",
        },
        "tags": ["baseline", "baseline", "local-only"],
    }


def test_backtest_profile_v2_validates_and_exports_snapshots() -> None:
    profile = BacktestProfileV2.model_validate(valid_profile_payload())

    full_snapshot = profile.to_snapshot()
    config_snapshot = profile.to_config_snapshot()

    assert full_snapshot["schema_version"] == "2"
    assert full_snapshot["safety"] == {
        "allow_download": False,
        "allow_exchange_connection": False,
        "allow_dry_run": False,
        "allow_live_trading": False,
        "allow_hyperopt": False,
    }
    assert full_snapshot["tags"] == ["baseline", "local-only"]
    assert config_snapshot == {
        "profile_name": "phase3_baseline",
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "timerange": "20240101-20240201",
        "strategy": "MvpRsiStrategy",
        "strategy_path": "user_data/strategies/generated",
        "exchange": {"name": "okx"},
        "datadir": "user_data/data",
        "stake_currency": "USDT",
        "stake_amount": 100,
        "tradable_balance_ratio": 0.99,
        "max_open_trades": 1,
    }


def test_backtest_profile_v2_exports_local_futures_runtime_options() -> None:
    payload = valid_profile_payload()
    payload["data_source"]["trading_mode"] = "futures"
    payload["data_source"]["margin_mode"] = "isolated"

    config_snapshot = BacktestProfileV2.model_validate(payload).to_config_snapshot()

    assert config_snapshot["exchange"] == {
        "name": "okx",
        "trading_mode": "futures",
        "margin_mode": "isolated",
    }


@pytest.mark.parametrize("missing_key", ["profile_name", "pair", "timeframe", "timerange", "strategy"])
def test_backtest_profile_v2_requires_core_fields(missing_key: str) -> None:
    payload = valid_profile_payload()
    payload.pop(missing_key)

    with pytest.raises(ValidationError):
        BacktestProfileV2.model_validate(payload)


@pytest.mark.parametrize(
    "payload_update",
    [
        {"api_key": "real-key-must-not-enter-profile"},
        {"data_source": {"kind": "local", "exchange": "okx", "api_secret_env": "OKX_SECRET"}},
        {"strategy": {"name": "MvpRsiStrategy", "password": "secret"}},
    ],
)
def test_backtest_profile_v2_rejects_secret_shaped_fields(payload_update: dict) -> None:
    payload = valid_profile_payload()
    payload.update(payload_update)

    with pytest.raises(ValidationError, match="forbidden secret key"):
        BacktestProfileV2.model_validate(payload)


@pytest.mark.parametrize(
    "payload_update",
    [
        {"dry_run": True},
        {"runmode": "live"},
        {"safety": {"allow_dry_run": True}},
        {"safety": {"allow_live_trading": True}},
        {"safety": {"allow_hyperopt": True}},
    ],
)
def test_backtest_profile_v2_rejects_forbidden_runtime_configuration(payload_update: dict) -> None:
    payload = valid_profile_payload()
    payload.update(payload_update)

    with pytest.raises(ValidationError):
        BacktestProfileV2.model_validate(payload)


def test_backtest_profile_v2_rejects_non_local_data_source() -> None:
    payload = valid_profile_payload()
    payload["data_source"]["kind"] = "download"

    with pytest.raises(ValidationError):
        BacktestProfileV2.model_validate(payload)


def test_backtest_profile_v2_rejects_reversed_timerange() -> None:
    payload = valid_profile_payload()
    payload["timerange"] = "20240201-20240101"

    with pytest.raises(ValidationError, match="timerange start must be before end"):
        BacktestProfileV2.model_validate(payload)
