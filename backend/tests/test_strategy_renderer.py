import pytest

from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_renderer import StrategyCodeRenderer
from app.services.strategy_static_review import StrategyStaticReviewService


def test_renderer_outputs_freqtrade_strategy_class() -> None:
    blueprint = StrategyBlueprint(
        name="RSI Demo",
        slug="rsi-demo",
        class_name="RsiDemoStrategy",
        indicators=[{"name": "rsi", "kind": "rsi", "period": 14}],
        entry_rules=[{"indicator": "rsi", "operator": "<", "value": 35}],
        exit_rules=[{"indicator": "rsi", "operator": ">", "value": 70}],
    )

    code = StrategyCodeRenderer().render(blueprint)

    assert "class RsiDemoStrategy(IStrategy):" in code
    assert "dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)" in code
    assert "dataframe['rsi'] < 35.0" in code
    assert "dataframe['rsi'] > 70.0" in code
    compile(code, "generated_strategy.py", "exec")


def test_blueprint_rejects_invalid_class_name() -> None:
    try:
        StrategyBlueprint(
            name="Invalid",
            slug="invalid",
            class_name="not a class",
            indicators=[{"name": "rsi", "kind": "rsi", "period": 14}],
            entry_rules=[{"indicator": "rsi", "operator": "<", "value": 35}],
        )
    except ValueError as exc:
        assert "class_name" in str(exc)
    else:
        raise AssertionError("invalid class name was accepted")


def test_blueprint_rejects_rules_for_missing_indicators() -> None:
    try:
        StrategyBlueprint(
            name="Invalid Rule",
            slug="invalid-rule",
            class_name="InvalidRuleStrategy",
            indicators=[{"name": "rsi", "kind": "rsi", "period": 14}],
            entry_rules=[{"indicator": "ema_fast", "operator": "<", "value": 35}],
        )
    except ValueError as exc:
        assert "rule indicator is not defined" in str(exc)
    else:
        raise AssertionError("rule for missing indicator was accepted")


def test_renderer_outputs_dual_direction_signals() -> None:
    blueprint = StrategyBlueprint(
        name="Dual RSI Demo",
        slug="dual-rsi-demo",
        class_name="DualRsiDemoStrategy",
        can_short=True,
        indicators=[{"name": "rsi", "kind": "rsi", "period": 14}],
        entry_rules=[{"indicator": "rsi", "operator": "<", "value": 35}],
        exit_rules=[{"indicator": "rsi", "operator": ">", "value": 55}],
        short_entry_rules=[
            {"indicator": "rsi", "operator": ">", "value": 65}
        ],
        short_exit_rules=[
            {"indicator": "rsi", "operator": "<", "value": 45}
        ],
    )

    code = StrategyCodeRenderer().render(blueprint)

    assert "can_short = True" in code
    assert "'enter_short'] = 1" in code
    assert "'exit_short'] = 1" in code
    compile(code, "generated_strategy.py", "exec")
    assert StrategyStaticReviewService().review_code(code).passed is True


def test_blueprint_rejects_impossible_and_conditions() -> None:
    with pytest.raises(ValueError, match="impossible AND conditions"):
        StrategyBlueprint(
            name="Impossible RSI",
            slug="impossible-rsi",
            class_name="ImpossibleRsiStrategy",
            indicators=[{"name": "rsi", "kind": "rsi", "period": 14}],
            entry_rules=[
                {"indicator": "rsi", "operator": "<", "value": 30},
                {"indicator": "rsi", "operator": ">", "value": 70},
            ],
        )
