"""Fail-closed promotion checks between research scoring and execution.

Ranking is intentionally broader than execution eligibility.  This module is
the narrow, durable boundary: a candidate may be useful for research while
still being ineligible for a Demo or Live execution chain.
"""

from __future__ import annotations

import math
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import object_session

from app.models.backtest import BacktestResult
from app.models.strategy_validation import StrategyValidationPlan
from app.models.strategy_score import StrategyScore
from app.models.strategy import StrategyVersion


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
    strategy_version: StrategyVersion | None = None,
    policy: StrategyPromotionPolicy = DEFAULT_PROMOTION_POLICY,
    validation_plan: StrategyValidationPlan | None = None,
) -> dict[str, Any]:
    """Return immutable promotion evidence or raise before approval is created.

    ``promotion_evidence`` is written by a validation workflow, never inferred
    from an aggregate score.  It proves that returns are net of costs and that
    out-of-sample and walk-forward checks were performed across market states.
    """

    if score.backtest_result_id != result.id:
        raise StrategyPromotionBlocked("promotion score is not bound to backtest result")
    if strategy_version is not None:
        if score.strategy_version_id != strategy_version.id:
            raise StrategyPromotionBlocked("promotion score is not bound to strategy version")
        if result.run is None or result.run.strategy_version_id != strategy_version.id:
            raise StrategyPromotionBlocked("backtest result is not bound to strategy version")
        if strategy_version.validation_status != "passed":
            raise StrategyPromotionBlocked("strategy version is not validated")
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
    matrix = _mapping(evidence.get("validation_matrix"), "validation_matrix")
    persisted_plan = validation_plan or _persisted_validation_plan(result)
    if persisted_plan is None or persisted_plan.status != "PASSED":
        raise StrategyPromotionBlocked(
            "promotion requires a passing persisted validation matrix"
        )
    session = object_session(result)
    if session is None:
        raise StrategyPromotionBlocked(
            "promotion validation requires session-backed database lineage"
        )
    try:
        from app.services.strategy_validation_matrix import (
            StrategyValidationBlocked,
            StrategyValidationMatrixService,
        )

        StrategyValidationMatrixService(session).assert_current_for_promotion(
            persisted_plan
        )
    except StrategyValidationBlocked as exc:
        raise StrategyPromotionBlocked(
            "promotion validation matrix is stale or invalid: {}".format(exc)
        ) from exc
    if (
        matrix.get("plan_id") != persisted_plan.id
        or matrix.get("plan_digest") != persisted_plan.plan_digest
        or matrix.get("evidence_digest") != persisted_plan.evidence_digest
        or matrix.get("provider") != "freqtrade"
    ):
        raise StrategyPromotionBlocked("promotion validation matrix lineage does not match")
    result_ids = matrix.get("window_result_ids")
    persisted_ids = persisted_plan.promotion_evidence.get("window_result_ids")
    if (
        not isinstance(result_ids, list)
        or len(result_ids) < 4
        or len(set(result_ids)) != len(result_ids)
        or result_ids != persisted_ids
    ):
        raise StrategyPromotionBlocked("promotion validation window lineage is incomplete")
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

    assessment = {
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
    if strategy_version is not None:
        # The version ID alone is insufficient: a manual rewrite or a changed
        # market/result payload under a reused database row must invalidate an
        # earlier approval before any Demo signal can advance.
        assessment["lineage"] = {
            "strategy_version_id": strategy_version.id,
            "strategy_code_digest": _strategy_code_digest(strategy_version),
            "validation_status": strategy_version.validation_status,
            "backtest_timerange": result.timerange,
            "backtest_result_path": result.result_path,
            "market_data_digest": _stable_digest(
                {
                    "timerange": result.timerange,
                    "run_config": result.run.config_snapshot,
                    "promotion_evidence": raw_evidence,
                }
            ),
        }
    return assessment


def promotion_candidate_digest(
    result: BacktestResult,
    score: StrategyScore,
    strategy_version: StrategyVersion,
    *,
    policy: StrategyPromotionPolicy = DEFAULT_PROMOTION_POLICY,
) -> tuple[dict[str, Any], str]:
    """Assess one exact candidate and return its immutable approval digest."""

    assessment = assess_strategy_promotion(
        result,
        score,
        strategy_version=strategy_version,
        policy=policy,
    )
    return assessment, _stable_digest(
        {
            "strategy_version_id": strategy_version.id,
            "backtest_result_id": result.id,
            "strategy_score_id": score.id,
            "assessment": assessment,
        }
    )


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


def _strategy_code_digest(strategy_version: StrategyVersion) -> str:
    code_hash = strategy_version.code_hash
    if isinstance(code_hash, str) and code_hash.strip():
        return code_hash.strip()
    return hashlib.sha256(strategy_version.generated_code.encode("utf-8")).hexdigest()


def _stable_digest(value: Mapping[str, Any]) -> str:
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _persisted_validation_plan(
    result: BacktestResult,
) -> StrategyValidationPlan | None:
    session = object_session(result)
    if session is None or result.id is None:
        return None
    return session.scalar(
        select(StrategyValidationPlan).where(
            StrategyValidationPlan.promotion_backtest_result_id == result.id
        )
    )
