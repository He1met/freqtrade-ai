from copy import deepcopy

import pytest

from app.core import config
from app.core.config import REPO_ROOT, get_settings, load_app_yaml
from app.core.demo_automation import (
    DemoAutomationConfigurationError,
    parse_demo_automation_policy,
)


def test_app_yaml_pins_okx_demo_automation_without_live_or_real_funds() -> None:
    raw = load_app_yaml(REPO_ROOT / "config" / "app.yaml")
    policy = parse_demo_automation_policy(raw.get("demo_automation"))

    assert policy.enabled is True
    assert policy.execution_target_id == "OKX_DEMO"
    assert policy.automatic_strategy_selection is True
    assert policy.automatic_candidate_approval is True
    assert policy.automatic_demo_order_submission is True
    assert policy.automatic_service_recovery is True
    assert policy.fail_closed is True
    assert policy.require_validated_backtest is True
    assert policy.require_fresh_market_and_account_data is True
    assert policy.require_risk_approval is True
    assert policy.require_position_and_notional_limits is True
    assert policy.require_unique_writer is True
    assert policy.require_idempotency is True
    assert policy.require_reconciliation is True
    assert policy.allow_live_trading is False
    assert policy.allow_real_funds is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", False),
        ("execution_target_id", "OKX_LIVE"),
        ("automatic_candidate_approval", False),
        ("require_risk_approval", False),
        ("require_unique_writer", False),
        ("require_idempotency", False),
        ("require_reconciliation", False),
        ("allow_live_trading", True),
        ("allow_real_funds", True),
    ],
)
def test_demo_automation_contract_cannot_be_weakened(field: str, value) -> None:
    raw = load_app_yaml(REPO_ROOT / "config" / "app.yaml")["demo_automation"]
    unsafe = deepcopy(raw)
    unsafe[field] = value

    with pytest.raises(DemoAutomationConfigurationError):
        parse_demo_automation_policy(unsafe)


def test_settings_loads_the_explicit_demo_automation_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_config = load_app_yaml(REPO_ROOT / "config" / "app.yaml")
    monkeypatch.setattr(config, "load_app_yaml", lambda _path: real_config)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.demo_automation_policy.execution_target_id == "OKX_DEMO"
        assert settings.demo_automation_policy.automatic_demo_order_submission is True
        assert settings.demo_automation_policy.allow_real_funds is False
    finally:
        get_settings.cache_clear()


def test_missing_demo_automation_policy_fails_closed() -> None:
    with pytest.raises(
        DemoAutomationConfigurationError,
        match="implicit authorization is forbidden",
    ):
        parse_demo_automation_policy(None)
