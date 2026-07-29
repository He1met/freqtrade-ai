from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class DemoAutomationConfigurationError(RuntimeError):
    """Raised when the durable OKX Demo automation boundary is ambiguous."""


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

    model_config = {"extra": "forbid"}


def parse_demo_automation_policy(raw: Any) -> OkxDemoAutomationPolicy:
    if not isinstance(raw, dict) or not raw:
        raise DemoAutomationConfigurationError(
            "OKX Demo automation policy is missing; implicit authorization is forbidden"
        )
    try:
        return OkxDemoAutomationPolicy.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise DemoAutomationConfigurationError(
            f"OKX Demo automation policy is blocked: {exc}"
        ) from exc
