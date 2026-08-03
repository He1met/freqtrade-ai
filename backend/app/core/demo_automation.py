from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class DemoAutomationConfigurationError(RuntimeError):
    """Raised when the durable OKX Demo automation boundary is ambiguous."""


class OkxDemoRiskPolicy(BaseModel):
    """Locked numerical limits for the first automated OKX Demo policy."""

    schema_version: Literal["1"] = "1"
    allowed_instruments: tuple[Literal["BTC-USDT-SWAP"]] = (
        "BTC-USDT-SWAP",
    )
    allowed_sides: tuple[Literal["buy"], Literal["sell"]] = ("buy", "sell")
    allowed_order_types: tuple[Literal["limit"]] = ("limit",)
    max_leverage: Literal[2] = 2
    max_order_notional: Literal[1000] = 1000
    max_total_exposure: Literal[3000] = 3000
    max_positions: Literal[3] = 3
    max_price_deviation_pct: Literal[0.01] = 0.01
    min_strategy_score: Literal[50] = 50
    max_active_strategies: Literal[3] = 3
    max_orders_per_5_minutes: Literal[6] = 6
    max_orders_per_hour: Literal[24] = 24
    critical_failure_threshold: Literal[3] = 3
    critical_failure_window_minutes: Literal[10] = 10
    cooldown_minutes: Literal[15] = 15
    recovery_health_check_required: Literal[True] = True
    scoring_version: Literal["phase2-quality-v1"] = "phase2-quality-v1"

    model_config = {"extra": "forbid"}


class OkxDemoAutomationPolicy(BaseModel):
    schema_version: Literal["1"] = "1"
    enabled: Literal[True] = True
    execution_target_id: Literal["OKX_DEMO"] = "OKX_DEMO"
    automatic_strategy_selection: Literal[True] = True
    automatic_candidate_approval: Literal[True] = True
    automatic_demo_order_submission: Literal[True] = True
    automatic_service_recovery: Literal[True] = True
    fail_closed: Literal[True] = True
    require_validated_backtest: Literal[True] = True
    require_fresh_market_and_account_data: Literal[True] = True
    require_risk_approval: Literal[True] = True
    require_position_and_notional_limits: Literal[True] = True
    require_unique_writer: Literal[True] = True
    require_idempotency: Literal[True] = True
    require_reconciliation: Literal[True] = True
    allow_live_trading: Literal[False] = False
    allow_real_funds: Literal[False] = False
    demo_risk_policy: OkxDemoRiskPolicy = OkxDemoRiskPolicy()

    model_config = {"extra": "forbid"}


def parse_demo_automation_policy(raw: Any) -> OkxDemoAutomationPolicy:
    if not isinstance(raw, dict) or not raw:
        raise DemoAutomationConfigurationError(
            "OKX Demo automation policy is missing; implicit authorization is forbidden"
        )
    expected_automation_fields = set(OkxDemoAutomationPolicy.model_fields)
    if set(raw) != expected_automation_fields:
        raise DemoAutomationConfigurationError(
            "OKX Demo automation policy fields are missing or unexpected"
        )
    raw_risk_policy = raw.get("demo_risk_policy")
    if not isinstance(raw_risk_policy, dict):
        raise DemoAutomationConfigurationError(
            "OKX Demo risk policy is missing; numerical limits are required"
        )
    if set(raw_risk_policy) != set(OkxDemoRiskPolicy.model_fields):
        raise DemoAutomationConfigurationError(
            "OKX Demo risk policy fields are missing or unexpected"
        )
    try:
        return OkxDemoAutomationPolicy.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise DemoAutomationConfigurationError(
            f"OKX Demo automation policy is blocked: {exc}"
        ) from exc
