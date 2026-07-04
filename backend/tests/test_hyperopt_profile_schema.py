import pytest
from pydantic import ValidationError

from app.schemas.hyperopt_profile import HyperoptProfile


def valid_profile_payload() -> dict:
    return {
        "name": "phase4-local-hyperopt-15m",
        "description": "Local-only Hyperopt profile for one candidate strategy.",
        "strategy": {
            "version_id": 123,
            "name": "MvpRsiStrategy",
            "file_path": "user_data/strategies/generated/MvpRsiStrategy.py",
        },
        "backtest_profile_id": 45,
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "timerange": "20240101-20240201",
        "local_data_source": {
            "kind": "local",
            "root": "user_data/data",
            "exchange": "okx",
            "relative_path": "okx/futures/BTC_USDT_USDT-15m-futures.feather",
            "data_format": "feather",
        },
        "spaces": ["buy", "sell", "roi", "buy"],
        "epochs": 100,
        "hyperopt_loss": "SharpeHyperOptLoss",
        "random_state": 42,
        "max_open_trades": 1,
        "stake_currency": "USDT",
        "locked_variables": {
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "timerange": "20240101-20240201",
            "local_data_source": "okx/futures/BTC_USDT_USDT-15m-futures.feather",
            "strategy_version_id": 123,
            "spaces": ["buy", "sell", "roi"],
            "epochs": 100,
            "hyperopt_loss": "SharpeHyperOptLoss",
        },
        "tags": ["phase-4", "phase-4", "local-only"],
    }


def test_hyperopt_profile_validates_and_exports_snapshots() -> None:
    profile = HyperoptProfile.model_validate(valid_profile_payload())

    full_snapshot = profile.to_snapshot()
    runner_snapshot = profile.to_runner_snapshot()

    assert full_snapshot["schema_version"] == "1"
    assert full_snapshot["spaces"] == ["buy", "sell", "roi"]
    assert full_snapshot["tags"] == ["phase-4", "local-only"]
    assert full_snapshot["safety"] == {
        "allow_download": False,
        "allow_exchange_connection": False,
        "allow_dry_run": False,
        "allow_live_trading": False,
        "dry_run": False,
        "live_trading": False,
    }
    assert runner_snapshot == {
        "profile_name": "phase4-local-hyperopt-15m",
        "strategy": "MvpRsiStrategy",
        "strategy_version_id": 123,
        "strategy_file_path": "user_data/strategies/generated/MvpRsiStrategy.py",
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "timerange": "20240101-20240201",
        "datadir": "user_data/data/okx",
        "data_path": "okx/futures/BTC_USDT_USDT-15m-futures.feather",
        "spaces": ["buy", "sell", "roi"],
        "epochs": 100,
        "hyperopt_loss": "SharpeHyperOptLoss",
        "random_state": 42,
        "max_open_trades": 1,
        "stake_currency": "USDT",
    }


@pytest.mark.parametrize(
    "missing_key",
    ["name", "strategy", "pair", "timeframe", "timerange", "local_data_source", "spaces"],
)
def test_hyperopt_profile_requires_core_fields(missing_key: str) -> None:
    payload = valid_profile_payload()
    payload.pop(missing_key)

    with pytest.raises(ValidationError):
        HyperoptProfile.model_validate(payload)


@pytest.mark.parametrize("space", ["protection", "download", "live"])
def test_hyperopt_profile_rejects_unsupported_spaces(space: str) -> None:
    payload = valid_profile_payload()
    payload["spaces"] = ["buy", space]
    payload["locked_variables"]["spaces"] = ["buy", space]

    with pytest.raises(ValidationError, match="unsupported hyperopt space"):
        HyperoptProfile.model_validate(payload)


def test_hyperopt_profile_rejects_unapproved_loss() -> None:
    payload = valid_profile_payload()
    payload["hyperopt_loss"] = "CustomNetworkLoss"
    payload["locked_variables"]["hyperopt_loss"] = "CustomNetworkLoss"

    with pytest.raises(ValidationError, match="unsupported hyperopt loss"):
        HyperoptProfile.model_validate(payload)


@pytest.mark.parametrize(
    "payload_update",
    [
        {"api_key": "real-key-must-not-enter-profile"},
        {"strategy": {"version_id": 123, "name": "MvpRsiStrategy", "file_path": "x.py", "password": "secret"}},
        {"local_data_source": {"kind": "local", "exchange": "okx", "relative_path": "BTC_USDT-15m.feather", "api_secret_env": "OKX_SECRET"}},
    ],
)
def test_hyperopt_profile_rejects_secret_shaped_fields(payload_update: dict) -> None:
    payload = valid_profile_payload()
    payload.update(payload_update)

    with pytest.raises(ValidationError, match="forbidden secret key"):
        HyperoptProfile.model_validate(payload)


@pytest.mark.parametrize(
    "payload_update",
    [
        {"dry_run": True},
        {"live_trading": True},
        {"runmode": "live"},
        {"safety": {"allow_dry_run": True}},
        {"safety": {"allow_live_trading": True}},
        {"safety": {"allow_exchange_connection": True}},
    ],
)
def test_hyperopt_profile_rejects_trading_runtime_configuration(payload_update: dict) -> None:
    payload = valid_profile_payload()
    payload.update(payload_update)

    with pytest.raises(ValidationError):
        HyperoptProfile.model_validate(payload)


def test_hyperopt_profile_requires_local_data_source_to_match_pair_and_timeframe() -> None:
    payload = valid_profile_payload()
    payload["local_data_source"]["relative_path"] = "okx/futures/ETH_USDT_USDT-1h-futures.feather"
    payload["locked_variables"]["local_data_source"] = "okx/futures/ETH_USDT_USDT-1h-futures.feather"

    with pytest.raises(ValidationError, match="local_data_source must match pair"):
        HyperoptProfile.model_validate(payload)


def test_hyperopt_profile_rejects_missing_locked_variable() -> None:
    payload = valid_profile_payload()
    payload["locked_variables"].pop("timerange")

    with pytest.raises(ValidationError, match="locked_variables missing required keys"):
        HyperoptProfile.model_validate(payload)


def test_hyperopt_profile_rejects_locked_variable_drift() -> None:
    payload = valid_profile_payload()
    payload["locked_variables"]["epochs"] = 50

    with pytest.raises(ValidationError, match="locked_variables.epochs must match profile input"):
        HyperoptProfile.model_validate(payload)


def test_hyperopt_profile_rejects_non_local_data_source() -> None:
    payload = valid_profile_payload()
    payload["local_data_source"]["kind"] = "download"

    with pytest.raises(ValidationError):
        HyperoptProfile.model_validate(payload)


def test_hyperopt_profile_rejects_reversed_timerange() -> None:
    payload = valid_profile_payload()
    payload["timerange"] = "20240201-20240101"
    payload["locked_variables"]["timerange"] = "20240201-20240101"

    with pytest.raises(ValidationError, match="timerange start must be before end"):
        HyperoptProfile.model_validate(payload)
