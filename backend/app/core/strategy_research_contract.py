from __future__ import annotations

from math import isclose
from typing import Any


RESEARCH_CONTRACT_VERSION = "formal-strategy-research-aggressive-v1"
RESEARCH_RISK_PROFILE = "AGGRESSIVE"
RESEARCH_PROFILE_LABEL = "进攻型：最大回撤 15%"
MIN_STRATEGY_SCORE = 50.0
MIN_VALIDATION_TRADES = 30
MAX_VALIDATION_DRAWDOWN = 0.15
MIN_FEE_PER_SIDE = 0.0005
MIN_SLIPPAGE_PER_SIDE = 0.0002


def official_research_policy() -> dict[str, Any]:
    """Return the project-owned qualification contract used by every lifecycle stage."""
    return {
        "contract_version": RESEARCH_CONTRACT_VERSION,
        "risk_profile": RESEARCH_RISK_PROFILE,
        "profile_label": RESEARCH_PROFILE_LABEL,
        "min_strategy_score": MIN_STRATEGY_SCORE,
        "min_trades_per_validation_window": MIN_VALIDATION_TRADES,
        "validation_requires_positive_net_profit": True,
        "max_drawdown_per_validation_window": MAX_VALIDATION_DRAWDOWN,
        "lookahead_analysis_required": True,
        "fee_per_side": MIN_FEE_PER_SIDE,
        "slippage_per_side": MIN_SLIPPAGE_PER_SIDE,
        "required_validation_windows": ["wf_bull", "wf_range", "oos", "wf_bear"],
        "score_source": "primary_bear net of fee and slippage",
    }


def matches_official_research_policy(policy: object) -> bool:
    """Fail closed unless a report uses the exact project-owned contract."""
    if not isinstance(policy, dict):
        return False
    expected = official_research_policy()
    for key, value in expected.items():
        actual = policy.get(key)
        if isinstance(value, float):
            if isinstance(actual, bool) or not isinstance(actual, (int, float)):
                return False
            if not isclose(float(actual), value, rel_tol=0.0, abs_tol=1e-12):
                return False
        elif actual != value:
            return False
    return True
