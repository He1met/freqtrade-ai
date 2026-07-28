"""Fail-closed promotion checks between research scoring and execution.

Ranking is intentionally broader than execution eligibility.  This module is
the narrow, durable boundary: a candidate may be useful for research while
still being ineligible for a Demo or Live execution chain.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from app.models.backtest import BacktestResult
from app.models.strategy_score import StrategyScore


PROMOTION_POLICY_VERSION = "strategy-promotion-v1"


class StrategyPromotionBlocked(ValueError):
    """Raised when a candidate lacks the evidence required for promotion."""


@dataclass(frozen=True)
class StrategyPromotionPolicy:
    policy_version: str = PROMOTION_POLICY_VERSION
    min_profit_pct: float = 0.0
    max_drawdown_pct: float = 0.20
    min_total_trades: int = 30
    min_market_states: int = 3


DEFAULT_PROMOTION_POLICY = StrategyPromotionPolicy()


def assess_strategy_promotion(
    result: BacktestResult,
    score: StrategyScore,
    *,
    policy: StrategyPromotionPolicy = DEFAULT_PROMOTION_POLICY,
) -> dict[str, Any]:
    """Return immutable promotion evidence or raise before approval is created.

    ``promotion_evidence`` is written by a validation workflow, never inferred
    from an aggregate score.  It proves that returns are net of costs and that
    out-of-sample and walk-forward checks were performed across market states.
    """

    if score.backtest_result_id != result.id:
        raise StrategyPromotionBlocked("promotion score is not bound to backtest result")
    score_snapshot = _mapping(score.metrics_snapshot, "score metrics snapshot")
    if score_snapshot.get("source") not in {None, "backtest_result"}:
        raise StrategyPromotionBlocked("promotion score has an unsupported source")
    snapshot_result_id = score_snapshot.get("backtest_result_id")
    if snapshot_result_id is not None and snapshot_result_id != result.id:
        raise StrategyPromotionBlocked("promotion score snapshot lineage is inconsistent")
    elimination = score_snapshot.get("elimination")
    if isinstance(elimination, Mapping) and elimination.get("eliminated") is True:
        raise StrategyPromotionBlocked("strategy score is eliminated from promotion")

    profit_pct = _finite(result.profit_pct, "profit_pct")
    if profit_pct <= policy.min_profit_pct:
        raise StrategyPromotionBlocked("promotion requires positive net profit")
    max_drawdown_pct = abs(_finite(result.max_drawdown_pct, "max_drawdown_pct"))
    if max_drawdown_pct > policy.max_drawdown_pct:
        raise StrategyPromotionBlocked("promotion maximum drawdown exceeded")
    total_trades = _positive_int(result.total_trades, "total_trades")
    if total_trades < policy.min_total_trades:
        raise StrategyPromotionBlocked("promotion has insufficient total trades")

    metrics = _mapping(result.metrics_snapshot, "backtest metrics snapshot")
    raw_evidence = metrics.get("promotion_evidence")
    if not isinstance(raw_evidence, Mapping):
        raise StrategyPromotionBlocked("promotion requires net-of-costs evidence")
    evidence = raw_evidence
    if evidence.get("net_of_costs") is not True:
        raise StrategyPromotionBlocked("promotion requires net-of-costs evidence")
    out_of_sample = _mapping(evidence.get("out_of_sample"), "out_of_sample")
    if out_of_sample.get("passed") is not True:
        raise StrategyPromotionBlocked("promotion requires passing out-of-sample evidence")
    if _finite(out_of_sample.get("profit_pct"), "out_of_sample profit_pct") <= 0:
        raise StrategyPromotionBlocked("out-of-sample result is not profitable")
    if _positive_int(out_of_sample.get("total_trades"), "out_of_sample total_trades") < policy.min_total_trades:
        raise StrategyPromotionBlocked("out-of-sample result has insufficient trades")

    walk_forward = _mapping(evidence.get("walk_forward"), "walk_forward")
    if walk_forward.get("passed") is not True:
        raise StrategyPromotionBlocked("promotion requires passing walk-forward evidence")
    states = walk_forward.get("market_states")
    if not isinstance(states, list):
        raise StrategyPromotionBlocked("walk-forward market states are missing")
    normalized_states = sorted({state.strip() for state in states if isinstance(state, str) and state.strip()})
    if len(normalized_states) < policy.min_market_states:
        raise StrategyPromotionBlocked("walk-forward market-state coverage is insufficient")

    return {
        "policy": asdict(policy),
        "backtest_result_id": result.id,
        "strategy_score_id": score.id,
        "metrics": {
            "profit_pct": profit_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "total_trades": total_trades,
        },
        "out_of_sample": {
            "profit_pct": _finite(out_of_sample.get("profit_pct"), "out_of_sample profit_pct"),
            "total_trades": _positive_int(out_of_sample.get("total_trades"), "out_of_sample total_trades"),
        },
        "walk_forward": {"market_states": normalized_states},
        "net_of_costs": True,
    }


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StrategyPromotionBlocked("{} is missing or invalid".format(name))
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise StrategyPromotionBlocked("{} is missing or invalid".format(name))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise StrategyPromotionBlocked("{} is missing or invalid".format(name))
    if not math.isfinite(number):
        raise StrategyPromotionBlocked("{} is missing or invalid".format(name))
    return number


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StrategyPromotionBlocked("{} is missing or invalid".format(name))
    return value
