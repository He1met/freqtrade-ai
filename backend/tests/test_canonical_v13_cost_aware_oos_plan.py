from __future__ import annotations

import ast
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "research/canonical_v13_optimization_batches/cost_aware_oos_15m_20260823.json"
WORKER = ROOT / "containers/canonical-v13-research/canonical_v13_cost_aware_optimizer.py"
ROLLOUT = ROOT / "scripts/canonical_v13_oos_window_rollout.py"


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_plan_freezes_disjoint_windows_costs_and_bounded_search() -> None:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    windows = plan["data_isolation"]
    assert _time(windows["train"]["end_at"]) == _time(
        windows["validation"]["start_at"]
    )
    assert _time(windows["validation"]["end_at"]) == _time(
        windows["holdout"]["start_at"]
    )
    assert windows["train"]["days"] >= 120
    assert windows["validation"]["days"] >= 30
    assert windows["holdout"]["days"] == 30
    assert windows["holdout"]["visible_during_search"] is False
    assert plan["execution"] == {
        "trial_budget": 96,
        "family_trial_budget": 32,
        "seed": 20260823,
        "single_writer": True,
        "serial_trials": True,
        "network": "none",
        "finalist_limit": 3,
        "holdout_feedback_to_search": False,
    }
    assert [family["trial_count"] for family in plan["families"]] == [32, 32, 32]
    assert plan["costs"]["qualification_fee_rate"] == 0.0005
    assert plan["costs"]["qualification_slippage_rate"] == 0.0002
    assert plan["hard_gates"]["trade_count"]["threshold"] == 30
    assert plan["hard_gates"]["net_return_after_cost"]["operator"] == ">"
    assert plan["hard_gates"]["maximum_drawdown"]["threshold"] == 0.15
    assert plan["hard_gates"]["overall_score"]["threshold"] == 50
    assert plan["target"]["leverage"] == 2.0
    assert plan["safety"]["allow_real_funds"] is False
    assert plan["safety"]["private_exchange_access"] is False
    assert plan["safety"]["order_submission"] is False


def test_worker_is_syntactically_valid_and_has_no_clock_entry_escape() -> None:
    source = WORKER.read_text(encoding="utf-8")
    ast.parse(source)
    banned = (
        ".dt.hour",
        ".dt.minute",
        "current_time.hour",
        "current_time.minute",
        "hour %",
        "minute %",
    )
    assert all(token not in source for token in banned)
    assert '"--network"' not in source
    assert 'sorted(path.name for path in Path("/sys/class/net").iterdir()) != ["lo"]' in source
    assert "min(2.0, max_leverage)" in source
    assert "can_short = False" in source
    assert "position_adjustment_enable = False" in source
    assert '"--enable-protections"' in source
    assert "2.0 * slippage * trade_count" not in source
    assert 'float(row.get("profit_ratio", 0.0))' not in source
    assert "sum(profits) / wallet" not in source
    assert 'float(row["profit_account"])' in source
    assert "market must be strictly continuous at 900 seconds" in source
    assert "market OHLC relationship is invalid" in source


def test_oos_window_rollout_plan_is_three_way_and_holdout_only_required() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROLLOUT), "plan", "--plan-file", str(PLAN)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    windows = result["window_payload"]["windows"]
    assert [item["window_key"] for item in windows] == [
        "optimization-train",
        "optimization-validation",
        "optimization-holdout",
    ]
    assert [item["required"] for item in windows] == [False, False, True]
    assert result["trading_capability"] == "TRADING_DISABLED"
    assert result["execution_side_effects"] == 0
