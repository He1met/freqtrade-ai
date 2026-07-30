import pytest

from app.models import BacktestResult, StrategyScore, StrategyValidationPlan
from app.services.strategy_promotion import (
    StrategyPromotionBlocked,
    assess_strategy_promotion,
)


def _result(**overrides):
    values = {
        "id": 11,
        "profit_pct": 0.06,
        "max_drawdown_pct": 0.12,
        "total_trades": 36,
        "metrics_snapshot": {
            "promotion_evidence": {
                "net_of_costs": True,
                "out_of_sample": {
                    "passed": True,
                    "profit_pct": 0.03,
                    "total_trades": 32,
                },
                "walk_forward": {
                    "passed": True,
                    "market_states": ["bull", "bear", "range"],
                },
                "validation_matrix": {
                    "plan_id": 31,
                    "plan_digest": "a" * 64,
                    "evidence_digest": "b" * 64,
                    "window_result_ids": [101, 102, 103, 104],
                    "provider": "freqtrade",
                },
            }
        },
    }
    values.update(overrides)
    return BacktestResult(**values)


def _score(**overrides):
    values = {
        "id": 21,
        "backtest_result_id": 11,
        "metrics_snapshot": {"source": "backtest_result", "backtest_result_id": 11},
    }
    values.update(overrides)
    return StrategyScore(**values)


def _plan() -> StrategyValidationPlan:
    return StrategyValidationPlan(
        id=31,
        strategy_version_id=1,
        promotion_backtest_result_id=11,
        provider_name="freqtrade",
        strategy_code_digest="c" * 64,
        plan_digest="a" * 64,
        plan_snapshot={},
        status="PASSED",
        evidence_digest="b" * 64,
        promotion_evidence={"window_result_ids": [101, 102, 103, 104]},
    )


def test_validated_strategy_has_explicit_promotion_evidence() -> None:
    assessment = assess_strategy_promotion(
        _result(), _score(), validation_plan=_plan()
    )

    assert assessment["policy"]["policy_version"] == "strategy-promotion-v1"
    assert assessment["net_of_costs"] is True
    assert assessment["walk_forward"]["market_states"] == ["bear", "bull", "range"]


@pytest.mark.parametrize(
    ("result_overrides", "message"),
    [
        ({"profit_pct": 0}, "positive net profit"),
        ({"max_drawdown_pct": 0.21}, "maximum drawdown"),
        ({"total_trades": 29}, "insufficient total trades"),
        ({"metrics_snapshot": {}}, "net-of-costs"),
    ],
)
def test_promotion_rejects_incomplete_or_unsafe_research(result_overrides, message) -> None:
    with pytest.raises(StrategyPromotionBlocked, match=message):
        assess_strategy_promotion(
            _result(**result_overrides), _score(), validation_plan=_plan()
        )


def test_promotion_requires_profitable_oos_and_multiple_market_states() -> None:
    result = _result(
        metrics_snapshot={
            "promotion_evidence": {
                "net_of_costs": True,
                "out_of_sample": {"passed": True, "profit_pct": 0, "total_trades": 40},
                "walk_forward": {"passed": True, "market_states": ["bull", "bear"]},
                "validation_matrix": {
                    "plan_id": 31,
                    "plan_digest": "a" * 64,
                    "evidence_digest": "b" * 64,
                    "window_result_ids": [101, 102, 103, 104],
                    "provider": "freqtrade",
                },
            }
        }
    )
    with pytest.raises(StrategyPromotionBlocked, match="out-of-sample result is not profitable"):
        assess_strategy_promotion(result, _score(), validation_plan=_plan())
