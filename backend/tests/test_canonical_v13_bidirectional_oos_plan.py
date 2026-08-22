from __future__ import annotations

import ast
from datetime import datetime
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLAN = (
    ROOT
    / "research/canonical_v13_optimization_batches/"
    "bidirectional_cost_aware_oos_15m_20260823.json"
)
WORKER = (
    ROOT
    / "containers/canonical-v13-research/"
    "canonical_v13_cost_aware_optimizer.py"
)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_bidirectional_plan_is_frozen_bounded_and_holdout_blind() -> None:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    windows = plan["data_isolation"]
    assert _time(windows["train"]["end_at"]) == _time(
        windows["validation"]["start_at"]
    )
    assert _time(windows["validation"]["end_at"]) == _time(
        windows["holdout"]["start_at"]
    )
    assert windows["train"]["days"] == 120
    assert windows["validation"]["days"] == 30
    assert windows["holdout"]["days"] == 30
    assert windows["holdout"]["visible_during_search"] is False
    assert windows["holdout"]["single_evaluation_per_finalist"] is True
    assert plan["execution"] == {
        "trial_budget": 96,
        "family_trial_budget": 32,
        "seed": 2026082301,
        "single_writer": True,
        "serial_trials": True,
        "network": "none",
        "finalist_limit": 3,
        "holdout_feedback_to_search": False,
    }
    assert [family["trial_count"] for family in plan["families"]] == [32, 32, 32]


def test_bidirectional_plan_keeps_cost_risk_and_direction_boundaries() -> None:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    assert plan["target"]["direction"] == "BIDIRECTIONAL_SINGLE_NET_POSITION"
    assert plan["target"]["leverage"] == 1.0
    assert plan["target"]["maximum_open_trades"] == 1
    assert plan["target"]["position_adjustment"] is False
    assert plan["directionality"] == {
        "can_short": True,
        "hedging": False,
        "simultaneous_long_short": False,
        "neutral_regime_action": "NO_ACTION",
        "exit_semantics": "POSITION_CLOSING_REDUCE_ONLY",
        "minimum_directional_trades_30d": 6,
        "minimum_directional_net_after_cost": 0.0,
        "maximum_directional_top_trade_share": 0.5,
    }
    assert plan["position_sizing"] == {
        "dry_run_wallet_quote": 10000,
        "stake_amount_quote": 100,
        "maximum_nominal_wallet_fraction": 0.01,
        "maximum_open_trades": 1,
        "position_adjustment": False,
    }
    assert plan["fixed_strategy_contract"]["atr_kill_multiplier"] == 2.5
    assert plan["fixed_strategy_contract"]["minimal_roi"] == {
        "0": 0.02,
        "1440": 0.01,
        "2880": 0.005,
    }
    assert plan["objective"]["selection_thresholds"] == {
        "minimum_total_trades_30d": 30,
        "maximum_total_top_trade_profit_share": 0.35,
        "maximum_train_validation_drawdown": 0.15,
    }
    assert plan["costs"]["qualification_fee_rate"] == 0.0005
    assert plan["costs"]["qualification_slippage_rate"] == 0.0002
    assert plan["hard_gates"]["trade_count"]["threshold"] == 30
    assert plan["hard_gates"]["maximum_drawdown"]["threshold"] == 0.15
    assert plan["hard_gates"]["overall_score"]["threshold"] == 50
    assert plan["safety"]["allow_real_funds"] is False
    assert plan["safety"]["private_exchange_access"] is False
    assert plan["safety"]["order_submission"] is False
    assert plan["safety"]["short"] is True
    assert plan["safety"]["hedge"] is False


def test_worker_has_symmetric_closed_candle_short_and_attribution_contracts() -> None:
    source = WORKER.read_text(encoding="utf-8")
    ast.parse(source)
    for family in (
        "bidirectional-regime-trend",
        "bidirectional-volatility-breakout",
        "bidirectional-momentum-continuation",
    ):
        assert family in source
    for token in (
        '"enter_long"',
        '"enter_short"',
        '"exit_long"',
        '"exit_short"',
        '"is_short"',
        '"direction_attribution"',
        '"maximum_drawdown_contribution"',
        '"min(1.0, max_leverage)"',
        "max_entry_position_adjustment = 0",
        'exit_position_semantics = "POSITION_CLOSING_REDUCE_ONLY"',
        '"turnover_penalty"',
        '"low_trade_count_penalty"',
    ):
        assert token in source
    banned = (
        ".dt.hour",
        ".dt.minute",
        "current_time.hour",
        "current_time.minute",
        "hour %",
        "minute %",
    )
    assert all(token not in source for token in banned)
    assert "position_adjustment_enable = False" in source
    assert 'Path("/sys/class/net")' in source
