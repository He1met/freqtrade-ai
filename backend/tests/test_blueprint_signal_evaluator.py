from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib

import pytest
from pydantic import ValidationError

from app.schemas.strategy_blueprint import StrategyBlueprint
from app.schemas.strategy_signal import (
    BlueprintSignalEvaluationRequest,
    ClosedCandle,
)
from app.services.blueprint_signal_evaluator import (
    BlueprintSignalEvaluationBlocked,
    BlueprintSignalEvaluator,
    timeframe_duration,
)
from app.services.strategy_renderer import StrategyCodeRenderer


START = datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def blueprint(
    *,
    entry_rules=None,
    can_short=False,
    short_entry_rules=None,
    timeframe="5m",
    indicators=None,
    regime_rules=None,
) -> StrategyBlueprint:
    return StrategyBlueprint.model_validate(
        {
            "schema_version": "2",
            "name": "Deterministic Signal Strategy",
            "slug": "deterministic-signal-strategy",
            "class_name": "DeterministicSignalStrategy",
            "timeframe": timeframe,
            "indicators": indicators or [
                {"name": "rsi_fast", "kind": "rsi", "period": 3},
                {"name": "ema_fast", "kind": "ema", "period": 3},
                {"name": "sma_fast", "kind": "sma", "period": 3},
            ],
            "entry_rules": entry_rules
            or [
                {"indicator": "rsi_fast", "operator": ">", "value": 99},
                {"indicator": "ema_fast", "operator": ">", "value": 48},
                {"indicator": "sma_fast", "operator": ">", "value": 48},
            ],
            "exit_rules": [],
            "can_short": can_short,
            "short_entry_rules": short_entry_rules or [],
            "short_exit_rules": [],
            "regime_rules": regime_rules or [],
        }
    )


def candles(
    prices,
    *,
    interval=timedelta(minutes=5),
    start=START,
):
    return [
        ClosedCandle(
            open_time=start + index * interval,
            open=price,
            high=Decimal(str(price)) + Decimal("0.1"),
            low=Decimal(str(price)) - Decimal("0.1"),
            close=price,
            volume=100 + index,
            confirmed=True,
        )
        for index, price in enumerate(prices)
    ]


def request(strategy, series, **overrides):
    interval = timeframe_duration(strategy.timeframe)
    code = StrategyCodeRenderer().render(strategy)
    payload = {
        "execution_target": "OKX_DEMO",
        "instrument_id": "BTC-USDT-SWAP",
        "strategy_version_id": 7,
        "candidate_digest": DIGEST_A,
        "market_snapshot_id": "market:trusted-snapshot-1",
        "market_digest": DIGEST_B,
        "blueprint": strategy,
        "generated_code": code,
        "code_hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "candles": series,
        "evaluated_at": series[-1].open_time + interval,
    }
    payload.update(overrides)
    return BlueprintSignalEvaluationRequest.model_validate(payload)


def test_evaluates_rsi_ema_sma_and_and_rules_on_latest_closed_candle() -> None:
    strategy = blueprint()
    series = candles(range(1, 51))

    result = BlueprintSignalEvaluator().evaluate(request(strategy, series))

    assert result.enter_long is True
    assert result.enter_short is False
    assert result.decision == "ACTIONABLE"
    assert result.candle_open_at == series[-1].open_time
    assert result.candle_close_at == series[-1].open_time + timedelta(minutes=5)
    assert result.indicator_values == {
        "ema_fast": "49",
        "rsi_fast": "100",
        "sma_fast": "49",
    }
    assert result.latest_closed_candle_at == series[-1].open_time
    assert result.candle_count == 50
    assert len(result.signal_digest) == 64
    assert [row["matched"] for row in result.rule_evidence] == [True, True, True]


def test_evaluates_raw_volume_sma_and_atr_from_closed_ohlcv() -> None:
    strategy = blueprint(
        indicators=[
            {"name": "close_raw", "kind": "raw", "period": 1},
            {"name": "volume_raw", "kind": "raw", "period": 1, "source": "volume"},
            {"name": "volume_sma", "kind": "sma", "period": 3, "source": "volume"},
            {"name": "atr", "kind": "atr", "period": 3},
        ],
        entry_rules=[
            {"indicator": "volume_raw", "operator": ">", "compare_indicator": "volume_sma"},
            {"indicator": "atr", "operator": ">", "value": 0.1},
            {"indicator": "close_raw", "operator": ">", "value": 49.0},
        ],
    )

    result = BlueprintSignalEvaluator().evaluate(
        request(strategy, candles(range(1, 51)))
    )

    assert result.decision == "ACTIONABLE"
    assert result.indicator_values["close_raw"] == "50"
    assert result.indicator_values["volume_raw"] == "149"
    assert result.indicator_values["volume_sma"] == "148"
    assert result.indicator_values["atr"] == "1.1"


def test_and_rule_requires_every_condition() -> None:
    strategy = blueprint(
        entry_rules=[
            {"indicator": "rsi_fast", "operator": ">", "value": 99},
            {"indicator": "sma_fast", "operator": ">", "value": 50},
        ]
    )

    result = BlueprintSignalEvaluator().evaluate(
        request(strategy, candles(range(1, 51)))
    )

    assert result.enter_long is False
    assert result.enter_short is False
    assert result.decision == "NO_ACTION"
    assert [row["matched"] for row in result.rule_evidence] == [True, False]


def test_evaluates_short_entry_from_falling_closed_candles() -> None:
    strategy = blueprint(
        entry_rules=[
            {"indicator": "rsi_fast", "operator": ">", "value": 90},
        ],
        can_short=True,
        short_entry_rules=[
            {"indicator": "rsi_fast", "operator": "<", "value": 1},
        ],
    )

    result = BlueprintSignalEvaluator().evaluate(
        request(strategy, candles(range(100, 50, -1)))
    )

    assert result.enter_long is False
    assert result.enter_short is True
    assert result.indicator_values["rsi_fast"] == "0"


def test_conflicting_long_and_short_entries_are_blocked() -> None:
    matching_rule = [
        {"indicator": "rsi_fast", "operator": "<", "value": 1},
    ]
    strategy = blueprint(
        entry_rules=matching_rule,
        can_short=True,
        short_entry_rules=matching_rule,
    )

    with pytest.raises(
        BlueprintSignalEvaluationBlocked,
        match="conflicting long and short",
    ):
        BlueprintSignalEvaluator().evaluate(
            request(strategy, candles([10] * 50))
        )


def test_digest_is_deterministic_and_binds_complete_candle_evidence() -> None:
    strategy = blueprint()
    original = candles(range(1, 51))
    evaluator = BlueprintSignalEvaluator()

    first = evaluator.evaluate(request(strategy, original))
    replay = evaluator.evaluate(request(strategy, candles(range(1, 51))))
    later_audit = evaluator.evaluate(
        request(
            strategy,
            candles(range(1, 51)),
            evaluated_at=original[-1].open_time + timedelta(minutes=6),
        )
    )
    changed_identity = evaluator.evaluate(
        request(
            strategy,
            candles(range(1, 51)),
            candidate_digest="c" * 64,
        )
    )
    changed = candles(range(1, 51))
    changed[-2] = changed[-2].model_copy(
        update={
            "open": Decimal("48.5"),
            "high": Decimal("49"),
            "low": Decimal("48"),
            "close": Decimal("48.5"),
        }
    )
    changed_result = evaluator.evaluate(request(strategy, changed))

    assert first.signal_digest == replay.signal_digest
    assert first.signal_digest == later_audit.signal_digest
    assert changed_identity.signal_digest != first.signal_digest
    assert changed_result.signal_digest != first.signal_digest


def test_requires_enough_contiguous_confirmed_closed_candles() -> None:
    strategy = blueprint()
    evaluator = BlueprintSignalEvaluator()
    insufficient = candles(range(1, 50))
    with pytest.raises(BlueprintSignalEvaluationBlocked, match="exactly 50"):
        evaluator.evaluate(request(strategy, insufficient))

    gap = candles(range(1, 51))
    gap[-1] = gap[-1].model_copy(
        update={"open_time": gap[-1].open_time + timedelta(minutes=5)}
    )
    with pytest.raises(BlueprintSignalEvaluationBlocked, match="contiguous"):
        evaluator.evaluate(request(strategy, gap))

    open_candle_request = BlueprintSignalEvaluationRequest(
        execution_target="OKX_DEMO",
        instrument_id="BTC-USDT-SWAP",
        strategy_version_id=7,
        candidate_digest=DIGEST_A,
        market_snapshot_id="market:trusted-snapshot-1",
        market_digest=DIGEST_B,
        blueprint=strategy,
        generated_code=StrategyCodeRenderer().render(strategy),
        code_hash=hashlib.sha256(
            StrategyCodeRenderer().render(strategy).encode("utf-8")
        ).hexdigest(),
        candles=candles(range(1, 51)),
        evaluated_at=START + timedelta(minutes=249),
    )
    with pytest.raises(BlueprintSignalEvaluationBlocked, match="not closed"):
        evaluator.evaluate(open_candle_request)

    stale = request(
        strategy,
        candles(range(1, 51)),
        evaluated_at=START + timedelta(minutes=255),
    )
    with pytest.raises(BlueprintSignalEvaluationBlocked, match="stale"):
        evaluator.evaluate(stale)

    with pytest.raises(ValidationError):
        ClosedCandle(
            open_time=START,
            open=1,
            high=2,
            low=1,
            close=2,
            volume=1,
            confirmed=False,
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("15m", timedelta(minutes=15)),
        ("2h", timedelta(hours=2)),
        ("1d", timedelta(days=1)),
        ("1w", timedelta(days=7)),
    ],
)
def test_timeframe_duration_is_explicit(value, expected) -> None:
    assert timeframe_duration(value) == expected


def test_weekly_candles_use_monday_utc_alignment() -> None:
    strategy = blueprint(timeframe="1w")
    monday = datetime(2025, 1, 6, 0, 0, tzinfo=timezone.utc)
    weekly = candles(
        range(1, 51),
        interval=timedelta(weeks=1),
        start=monday,
    )

    result = BlueprintSignalEvaluator().evaluate(request(strategy, weekly))

    assert result.candle_open_at.weekday() == 0
    misaligned = [
        candle.model_copy(
            update={"open_time": candle.open_time + timedelta(days=3)}
        )
        for candle in weekly
    ]
    with pytest.raises(BlueprintSignalEvaluationBlocked, match="timeframe epoch"):
        BlueprintSignalEvaluator().evaluate(request(strategy, misaligned))


def test_strategy_version_code_must_match_deterministic_renderer_and_hash() -> None:
    strategy = blueprint()
    series = candles(range(1, 51))
    valid = request(strategy, series)
    altered_code = valid.generated_code + "\n# altered"
    with pytest.raises(
        BlueprintSignalEvaluationBlocked,
        match="deterministic blueprint rendering",
    ):
        BlueprintSignalEvaluator().evaluate(
            valid.model_copy(
                update={
                    "generated_code": altered_code,
                    "code_hash": hashlib.sha256(
                        altered_code.encode("utf-8")
                    ).hexdigest(),
                }
            )
        )
    with pytest.raises(
        BlueprintSignalEvaluationBlocked,
        match="code_hash",
    ):
        BlueprintSignalEvaluator().evaluate(
            valid.model_copy(update={"code_hash": "f" * 64})
        )


def test_indicator_engine_matches_fixed_talib_golden_vector() -> None:
    # Generated once with the sole configured Freqtrade 2026.5 environment:
    # talib 0.6.8, RSI/EMA/SMA/ATR(timeperiod=14). These constants deliberately
    # remain in the backend suite so ambient dependency upgrades cannot silently
    # redefine the evaluator.
    talib_expected = {
        "rsi_14": Decimal("44.306144809270585"),
        "ema_14": Decimal("107.08699627029503"),
        "sma_14": Decimal("107.07500000000003"),
        "atr_14": Decimal("2.561678815131764"),
    }
    strategy = StrategyBlueprint.model_validate(
        {
            "schema_version": "2",
            "name": "TA Lib Golden Strategy",
            "slug": "talib-golden-strategy",
            "class_name": "TalibGoldenStrategy",
            "timeframe": "5m",
            "indicators": [
                {"name": "rsi_14", "kind": "rsi", "period": 14},
                {"name": "ema_14", "kind": "ema", "period": 14},
                {"name": "sma_14", "kind": "sma", "period": 14},
                {"name": "atr_14", "kind": "atr", "period": 14},
            ],
            "entry_rules": [
                {"indicator": "rsi_14", "operator": ">", "value": 40},
            ],
        }
    )
    prices = [
        100 + (index % 7) * 1.3 - (index % 5) * 0.7 + index * 0.11
        for index in range(50)
    ]

    result = BlueprintSignalEvaluator().evaluate(
        request(strategy, candles(prices))
    )

    assert result.indicator_engine_version == "decimal-talib-golden-v2"
    for name, expected in talib_expected.items():
        observed = Decimal(result.indicator_values[name])
        assert abs(observed - expected) <= Decimal("1e-12")


def test_talib_numeric_comparison_boundary_is_fail_closed() -> None:
    strategy = StrategyBlueprint.model_validate(
        {
            "schema_version": "2",
            "name": "TA Lib Boundary Strategy",
            "slug": "talib-boundary-strategy",
            "class_name": "TalibBoundaryStrategy",
            "timeframe": "5m",
            "indicators": [
                {"name": "rsi_14", "kind": "rsi", "period": 14},
            ],
            "entry_rules": [
                {
                    "indicator": "rsi_14",
                    "operator": ">",
                    "value": 44.306144809270585,
                },
            ],
        }
    )
    prices = [
        100 + (index % 7) * 1.3 - (index % 5) * 0.7 + index * 0.11
        for index in range(50)
    ]

    with pytest.raises(
        BlueprintSignalEvaluationBlocked,
        match="TA-Lib comparison boundary",
    ):
        BlueprintSignalEvaluator().evaluate(
            request(strategy, candles(prices))
        )


def test_evaluates_indicator_cross_and_rising_rules_on_closed_candles() -> None:
    strategy = blueprint(
        indicators=[
            {"name": "ema_fast", "kind": "ema", "period": 3},
            {"name": "ema_slow", "kind": "ema", "period": 8},
        ],
        entry_rules=[
            {
                "indicator": "ema_fast",
                "operator": "crosses_above",
                "compare_indicator": "ema_slow",
                "lookback": 1,
            },
            {"indicator": "ema_fast", "operator": "rising", "lookback": 2},
        ],
    )
    prices = [Decimal("10")] * 48 + [Decimal("8"), Decimal("20")]

    result = BlueprintSignalEvaluator().evaluate(
        request(strategy, candles(prices))
    )

    assert result.enter_long is True
    assert result.market_regime is None
    assert [item["operator"] for item in result.rule_evidence] == [
        "crosses_above",
        "rising",
    ]
    assert all("previous_value" in item for item in result.rule_evidence)


def test_regime_rules_gate_signals_and_require_one_match() -> None:
    strategy = blueprint(
        indicators=[
            {"name": "ema_fast", "kind": "ema", "period": 3},
            {"name": "ema_slow", "kind": "ema", "period": 8},
            {"name": "rsi", "kind": "rsi", "period": 3},
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
    rising_prices = [Decimal("10")] * 42 + [Decimal("8")] * 5 + [Decimal("20")] * 3
    result = BlueprintSignalEvaluator().evaluate(
        request(strategy, candles(rising_prices))
    )

    assert result.market_regime == "bull"
    assert result.enter_long is True
    assert any(item.get("regime") == "bull" for item in result.rule_evidence)

    no_match_strategy = blueprint(
        indicators=[
            {"name": "ema_fast", "kind": "ema", "period": 3},
            {"name": "ema_slow", "kind": "ema", "period": 8},
        ],
        entry_rules=[
            {
                "indicator": "ema_fast",
                "operator": ">",
                "compare_indicator": "ema_slow",
                "regime": "bull",
            }
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
    with pytest.raises(
        BlueprintSignalEvaluationBlocked,
        match="does not match exactly one declared market regime",
    ):
        BlueprintSignalEvaluator().evaluate(
            request(no_match_strategy, candles([Decimal("10")] * 50))
        )
