import pytest
from pydantic import ValidationError

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
        "tags": ["phase-5", "phase-5", "local-only"],
    }


def test_dry_run_profile_validates_and_exports_snapshots() -> None:
    profile = DryRunProfile.model_validate(valid_profile_payload())

    full_snapshot = profile.to_snapshot()
    input_snapshot = profile.to_input_snapshot()
    runner_snapshot = profile.to_runner_snapshot()

    assert full_snapshot["schema_version"] == "1"
    assert full_snapshot["tags"] == ["phase-5", "local-only"]
    assert full_snapshot["safety"] == {
        "allow_download": False,
        "allow_exchange_connection": False,
        "allow_live_trading": False,
        "allow_real_orders": False,
        "allow_dry_run": True,
        "dry_run": True,
        "live_trading": False,
    }
    assert len(input_snapshot["profile_hash"]) == 64
    assert input_snapshot["locked_variables"]["dry_run"] is True
    assert runner_snapshot == {
        "profile_name": "phase5-local-dry-run",
        "strategy": "MvpRsiStrategy",
        "strategy_version_id": 123,
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "exchange": "okx",
        "trading_mode": "futures",
        "stake_currency": "USDT",
        "stake_amount": 100,
        "tradable_balance_ratio": 0.99,
        "max_open_trades": 1,
        "dry_run": True,
        "user_data_dir": "user_data",
        "log_level": "INFO",
        "freq_ui": {
            "enabled": True,
            "base_url": "http://127.0.0.1:8080/",
            "environment_label": "local-dry-run",
        },
        "strategy_file_path": "user_data/strategies/generated/MvpRsiStrategy.py",
        "strategy_path": "user_data/strategies/generated",
    }


@pytest.mark.parametrize("missing_key", ["name", "strategy", "pair", "timeframe"])
def test_dry_run_profile_requires_core_fields(missing_key: str) -> None:
    payload = valid_profile_payload()
    payload.pop(missing_key)

    with pytest.raises(ValidationError):
        DryRunProfile.model_validate(payload)


@pytest.mark.parametrize(
    "payload_update",
    [
        {"api_key": "real-key-must-not-enter-profile"},
        {"strategy": {"version_id": 123, "name": "MvpRsiStrategy", "file_path": "x.py", "password": "secret"}},
        {"exchange": {"name": "okx", "api_secret_env": "OKX_SECRET"}},
        {"freq_ui": {"enabled": True, "base_url": "http://127.0.0.1:8080", "token": "real-token"}},
    ],
)
def test_dry_run_profile_rejects_secret_shaped_fields(payload_update: dict) -> None:
    payload = valid_profile_payload()
    payload.update(payload_update)

    with pytest.raises(ValidationError, match="forbidden secret key"):
        DryRunProfile.model_validate(payload)


@pytest.mark.parametrize(
    "payload_update",
    [
        {"runmode": "live"},
        {"live": True},
        {"trade": True},
        {"force_entry_enable": True},
        {"safety": {"dry_run": False}},
        {"safety": {"allow_dry_run": False}},
        {"safety": {"allow_live_trading": True}},
        {"safety": {"allow_real_orders": True}},
        {"safety": {"live_trading": True}},
    ],
)
def test_dry_run_profile_rejects_live_or_real_order_configuration(payload_update: dict) -> None:
    payload = valid_profile_payload()
    payload.update(payload_update)

    with pytest.raises(ValidationError):
        DryRunProfile.model_validate(payload)


def test_dry_run_profile_requires_freq_ui_base_url_when_enabled() -> None:
    payload = valid_profile_payload()
    payload["freq_ui"] = {"enabled": True}
    payload["locked_variables"]["freq_ui_enabled"] = True

    with pytest.raises(ValidationError, match="freq_ui.base_url is required"):
        DryRunProfile.model_validate(payload)


def test_dry_run_profile_rejects_command_path_escape() -> None:
    payload = valid_profile_payload()
    payload["command_options"]["strategy_path"] = "../outside"

    with pytest.raises(ValidationError, match="parent traversal"):
        DryRunProfile.model_validate(payload)


def test_dry_run_profile_rejects_locked_variable_drift() -> None:
    payload = valid_profile_payload()
    payload["locked_variables"]["pair"] = "ETH/USDT:USDT"

    with pytest.raises(ValidationError, match="locked_variables.pair must match profile input"):
        DryRunProfile.model_validate(payload)


def test_dry_run_profile_hash_is_stable_and_changes_with_input() -> None:
    profile = DryRunProfile.model_validate(valid_profile_payload())
    same_profile = DryRunProfile.model_validate(valid_profile_payload())
    changed_payload = valid_profile_payload()
    changed_payload["stake"]["amount"] = 250
    changed_payload["locked_variables"]["stake_amount"] = 250
    changed_profile = DryRunProfile.model_validate(changed_payload)

    assert profile.profile_hash() == same_profile.profile_hash()
    assert profile.profile_hash() != changed_profile.profile_hash()
