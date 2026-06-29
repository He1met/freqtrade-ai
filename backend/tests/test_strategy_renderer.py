from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_renderer import StrategyCodeRenderer


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
