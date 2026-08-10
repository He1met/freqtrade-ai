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
    assert policy.demo_risk_policy.allowed_instruments == (
        "BTC-USDT-SWAP",
        "ETH-USDT-SWAP",
        "SOL-USDT-SWAP",
    )
    assert policy.demo_risk_policy.allowed_sides == ("buy", "sell")
    assert policy.demo_risk_policy.allowed_order_types == ("limit",)
    assert policy.demo_risk_policy.max_leverage == 2
    assert policy.demo_risk_policy.max_order_notional == 1000
    assert policy.demo_risk_policy.max_total_exposure == 3000
    assert policy.demo_risk_policy.max_positions == 3
    assert policy.demo_risk_policy.max_price_deviation_pct == 0.01
    assert policy.demo_risk_policy.min_strategy_score == 50
    assert policy.demo_risk_policy.max_active_strategies == 9
    assert policy.demo_risk_policy.max_orders_per_5_minutes == 6
    assert policy.demo_risk_policy.max_orders_per_hour == 24
    assert policy.demo_risk_policy.critical_failure_threshold == 3
    assert policy.demo_risk_policy.critical_failure_window_minutes == 10
    assert policy.demo_risk_policy.cooldown_minutes == 15
    assert policy.demo_risk_policy.recovery_health_check_required is True
    assert policy.demo_risk_policy.scoring_version == "phase2-quality-v1"


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_instruments", ["ETH-USDT-SWAP"]),
        ("allowed_sides", ["buy"]),
        ("allowed_order_types", ["market"]),
        ("max_leverage", 3),
        ("max_order_notional", 1001),
        ("max_total_exposure", 3001),
        ("max_positions", 4),
        ("max_price_deviation_pct", 0.02),
        ("min_strategy_score", 49),
        ("max_active_strategies", 4),
        ("max_orders_per_5_minutes", 7),
        ("max_orders_per_hour", 25),
        ("critical_failure_threshold", 4),
        ("critical_failure_window_minutes", 11),
        ("cooldown_minutes", 16),
        ("recovery_health_check_required", False),
        ("scoring_version", "fixture-score-v1"),
    ],
)
def test_demo_risk_policy_cannot_be_weakened(field: str, value) -> None:
    raw = load_app_yaml(REPO_ROOT / "config" / "app.yaml")["demo_automation"]
    unsafe = deepcopy(raw)
    unsafe["demo_risk_policy"][field] = value

    with pytest.raises(DemoAutomationConfigurationError):
        parse_demo_automation_policy(unsafe)


def test_demo_risk_policy_missing_or_extra_fields_fail_closed() -> None:
    raw = load_app_yaml(REPO_ROOT / "config" / "app.yaml")["demo_automation"]

    missing_policy = deepcopy(raw)
    del missing_policy["demo_risk_policy"]
    with pytest.raises(DemoAutomationConfigurationError, match="fields"):
        parse_demo_automation_policy(missing_policy)

    missing_limit = deepcopy(raw)
    del missing_limit["demo_risk_policy"]["max_order_notional"]
    with pytest.raises(DemoAutomationConfigurationError, match="risk policy fields"):
        parse_demo_automation_policy(missing_limit)

    extra_limit = deepcopy(raw)
    extra_limit["demo_risk_policy"]["unreviewed_limit"] = 1
    with pytest.raises(DemoAutomationConfigurationError, match="risk policy fields"):
        parse_demo_automation_policy(extra_limit)
