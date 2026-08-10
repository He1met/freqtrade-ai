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


def test_renderer_preserves_legacy_close_bytes_and_supports_closed_ohlcv_vocabulary() -> None:
    blueprint = StrategyBlueprint(
        name="Volume ATR Demo",
        slug="volume-atr-demo",
        class_name="VolumeAtrDemoStrategy",
        indicators=[
            {"name": "close_raw", "kind": "raw", "period": 1},
            {"name": "volume_raw", "kind": "raw", "period": 1, "source": "volume"},
            {"name": "volume_sma", "kind": "sma", "period": 24, "source": "volume"},
            {"name": "atr", "kind": "atr", "period": 14},
            {"name": "ema", "kind": "ema", "period": 20},
        ],
        entry_rules=[
            {"indicator": "volume_raw", "operator": ">", "compare_indicator": "volume_sma"},
            {"indicator": "atr", "operator": "rising", "lookback": 3},
            {"indicator": "close_raw", "operator": ">", "compare_indicator": "ema"},
        ],
    )

    code = StrategyCodeRenderer().render(blueprint)

    assert "dataframe['close_raw'] = dataframe['close']" in code
    assert "dataframe['volume_raw'] = dataframe['volume']" in code
    assert "ta.SMA(dataframe['volume'], timeperiod=24)" in code
    assert "ta.ATR(dataframe, timeperiod=14)" in code
    assert "ta.EMA(dataframe, timeperiod=20)" in code
    assert "source" not in blueprint.model_dump(mode="json")["indicators"][0]
    assert blueprint.model_dump(mode="json")["indicators"][1]["source"] == "volume"
    compile(code, "generated_strategy.py", "exec")


@pytest.mark.parametrize(
    ("indicator", "message"),
    [
        ({"name": "raw", "kind": "raw", "period": 2}, "raw indicators require period=1"),
        ({"name": "atr", "kind": "atr", "period": 14, "source": "volume"}, "atr indicators require source=close"),
    ],
)
def test_blueprint_rejects_invalid_ohlcv_indicator_shapes(indicator, message) -> None:
    with pytest.raises(ValueError, match=message):
        StrategyBlueprint(
            name="Invalid OHLCV",
            slug="invalid-ohlcv",
            class_name="InvalidOhlcvStrategy",
            indicators=[indicator],
            entry_rules=[{"indicator": indicator["name"], "operator": ">", "value": 1.0}],
        )


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


def test_renderer_outputs_indicator_cross_and_trend_rules_without_future_shift() -> None:
    blueprint = StrategyBlueprint(
        name="Trend Cross Demo",
        slug="trend-cross-demo",
        class_name="TrendCrossDemoStrategy",
        indicators=[
            {"name": "ema_fast", "kind": "ema", "period": 12},
            {"name": "ema_slow", "kind": "ema", "period": 26},
        ],
        entry_rules=[
            {
                "indicator": "ema_fast",
                "operator": "crosses_above",
                "compare_indicator": "ema_slow",
                "lookback": 2,
            },
            {"indicator": "ema_fast", "operator": "rising", "lookback": 3},
        ],
    )

    code = StrategyCodeRenderer().render(blueprint)

    assert "dataframe['ema_fast'] > dataframe['ema_slow']" in code
    assert "dataframe['ema_fast'].shift(2) <= dataframe['ema_slow'].shift(2)" in code
    assert "dataframe['ema_fast'] > dataframe['ema_fast'].shift(3)" in code
    assert "shift(-" not in code
    assert "startup_candle_count = 50" in code
    compile(code, "generated_strategy.py", "exec")
    assert StrategyStaticReviewService().review_code(code).passed is True


def test_renderer_outputs_regime_masks_and_regime_gated_rules() -> None:
    blueprint = StrategyBlueprint(
        name="Regime Demo",
        slug="regime-demo",
        class_name="RegimeDemoStrategy",
        indicators=[
            {"name": "ema_fast", "kind": "ema", "period": 12},
            {"name": "ema_slow", "kind": "ema", "period": 26},
            {"name": "rsi", "kind": "rsi", "period": 14},
        ],
        entry_rules=[
            {"indicator": "rsi", "operator": ">", "value": 50, "regime": "bull"}
        ],
        regime_rules=[
            {
                "regime": "bull",
                "rules": [
                    {
                        "indicator": "ema_fast",
                        "operator": ">",
                        "compare_indicator": "ema_slow",
                    }
                ],
            },
            {
                "regime": "bear",
                "rules": [
                    {
                        "indicator": "ema_fast",
                        "operator": "<",
                        "compare_indicator": "ema_slow",
                    }
                ],
            },
        ],
    )

    code = StrategyCodeRenderer().render(blueprint)

    assert "regime_masks = {" in code
    assert "regime_match_count" in code
    assert "(regime_match_count == 1)" in code
    assert "'bull': reduce" in code
    assert "regime_masks['bull']" in code
    assert code.count("regime_masks = {") == 2
    compile(code, "generated_strategy.py", "exec")
    assert StrategyStaticReviewService().review_code(code).passed is True
