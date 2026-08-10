from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Optional

from app.schemas.strategy_blueprint import (
    MarketRegime,
    RegimeRule,
    SignalRule,
    StrategyBlueprint,
)
from app.schemas.strategy_signal import (
    BlueprintSignalEvaluation,
    BlueprintSignalEvaluationRequest,
    ClosedCandle,
)
from app.services.strategy_renderer import StrategyCodeRenderer


EVALUATOR_VERSION = "blueprint-signal-v2.1"
INDICATOR_ENGINE_VERSION = "decimal-talib-golden-v2"
STARTUP_CANDLE_COUNT = 50
RULE_BOUNDARY_RELATIVE_TOLERANCE = Decimal("1e-12")
TIMEFRAME_PATTERN = re.compile(r"^([1-9][0-9]*)([mhdw])$")
IndicatorSeries = list[Optional[Decimal]]


class BlueprintSignalEvaluationBlocked(ValueError):
    """The supplied research or candle evidence cannot produce a safe signal."""


class BlueprintSignalEvaluator:
    """Deterministically evaluate the renderer-supported Blueprint vocabulary.

    This service deliberately does not load or execute a generated Python file.
    It has no network, filesystem, environment, credential, or dynamic-import
    boundary. Its indicator definitions are versioned below so a signal can be
    reproduced without relying on ambient Freqtrade or pandas versions.
    """

    def evaluate(
        self,
        request: BlueprintSignalEvaluationRequest,
    ) -> BlueprintSignalEvaluation:
        blueprint = request.blueprint
        rendered_code = StrategyCodeRenderer().render(blueprint)
        generated_hash = hashlib.sha256(
            request.generated_code.encode("utf-8")
        ).hexdigest()
        if rendered_code != request.generated_code:
            raise BlueprintSignalEvaluationBlocked(
                "generated code does not match deterministic blueprint rendering"
            )
        if generated_hash != request.code_hash:
            raise BlueprintSignalEvaluationBlocked(
                "generated code does not match strategy version code_hash"
            )
        interval = timeframe_duration(blueprint.timeframe)
        evaluated_at = request.evaluated_at.astimezone(timezone.utc)
        candles = self._validated_candles(
            request.candles,
            timeframe=blueprint.timeframe,
            interval=interval,
            evaluated_at=evaluated_at,
            required_count=self._required_candle_count(blueprint),
        )
        source_values = {
            "open": [candle.open for candle in candles],
            "high": [candle.high for candle in candles],
            "low": [candle.low for candle in candles],
            "close": [candle.close for candle in candles],
            "volume": [candle.volume for candle in candles],
        }
        series_by_name: dict[str, IndicatorSeries] = {}
        for indicator in blueprint.indicators:
            values = source_values[indicator.source]
            if indicator.kind == "raw":
                series = list(values)
            elif indicator.kind == "atr":
                series = _atr(
                    source_values["high"],
                    source_values["low"],
                    source_values["close"],
                    indicator.period,
                )
            else:
                calculator = {
                    "rsi": _rsi,
                    "ema": _ema,
                    "sma": _sma,
                }[indicator.kind]
                series = calculator(values, indicator.period)
            series_by_name[indicator.name] = series

        latest_values: dict[str, Decimal] = {}
        for name, series in series_by_name.items():
            value = series[-1]
            if value is None or not value.is_finite():
                raise BlueprintSignalEvaluationBlocked(
                    f"indicator {name} is unavailable on the latest closed candle"
                )
            latest_values[name] = value

        active_regime = _evaluate_regime(
            blueprint.regime_rules,
            series_by_name,
            index=len(candles) - 1,
        )

        enter_long, long_evidence = _evaluate_rules(
            "long",
            blueprint.entry_rules,
            series_by_name,
            index=len(candles) - 1,
            active_regime=active_regime,
        )
        enter_short, short_evidence = _evaluate_rules(
            "short",
            blueprint.short_entry_rules if blueprint.can_short else [],
            series_by_name,
            index=len(candles) - 1,
            active_regime=active_regime,
        )
        if enter_long and enter_short:
            raise BlueprintSignalEvaluationBlocked(
                "latest closed candle produces conflicting long and short entries"
            )

        latest = candles[-1]
        candle_close_at = latest.open_time + interval
        rule_evidence = [*long_evidence, *short_evidence]
        indicator_snapshot = {
            name: _decimal_text(value)
            for name, value in sorted(latest_values.items())
        }
        digest_payload = {
            "evaluator_version": EVALUATOR_VERSION,
            "indicator_engine_version": INDICATOR_ENGINE_VERSION,
            "execution_target": request.execution_target,
            "instrument_id": request.instrument_id,
            "strategy_version_id": request.strategy_version_id,
            "candidate_digest": request.candidate_digest,
            "market_snapshot_id": request.market_snapshot_id,
            "market_digest": request.market_digest,
            "code_hash": request.code_hash,
            "blueprint": blueprint.model_dump(mode="json"),
            "candles": [_canonical_candle(candle) for candle in candles],
            "decision": {
                "enter_long": enter_long,
                "enter_short": enter_short,
                "market_regime": active_regime,
                "indicator_values": indicator_snapshot,
                "rule_evidence": rule_evidence,
                "candle_open_at": _datetime_text(latest.open_time),
                "candle_close_at": _datetime_text(candle_close_at),
            },
        }
        signal_digest = hashlib.sha256(
            _canonical_json(digest_payload).encode("utf-8")
        ).hexdigest()
        return BlueprintSignalEvaluation(
            execution_target=request.execution_target,
            instrument_id=request.instrument_id,
            strategy_version_id=request.strategy_version_id,
            candidate_digest=request.candidate_digest,
            market_snapshot_id=request.market_snapshot_id,
            market_digest=request.market_digest,
            code_hash=request.code_hash,
            strategy_slug=blueprint.slug,
            class_name=blueprint.class_name,
            timeframe=blueprint.timeframe,
            decision=(
                "ACTIONABLE"
                if enter_long or enter_short
                else "NO_ACTION"
            ),
            candle_open_at=latest.open_time,
            candle_close_at=candle_close_at,
            latest_closed_candle_at=latest.open_time,
            evaluated_at=evaluated_at,
            enter_long=enter_long,
            enter_short=enter_short,
            market_regime=active_regime,
            indicator_values=indicator_snapshot,
            rule_evidence=rule_evidence,
            candle_count=len(candles),
            signal_digest=signal_digest,
        )

    @staticmethod
    def _required_candle_count(blueprint: StrategyBlueprint) -> int:
        # RSI consumes one more close than its period because it evaluates price
        # deltas. Requiring the same margin for every indicator keeps the input
        # contract simple and exceeds the generated strategy's startup floor.
        all_rules = [
            *blueprint.entry_rules,
            *blueprint.exit_rules,
            *blueprint.short_entry_rules,
            *blueprint.short_exit_rules,
            *[rule for item in blueprint.regime_rules for rule in item.rules],
        ]
        max_lookback = max((rule.lookback for rule in all_rules), default=1)
        return max(
            STARTUP_CANDLE_COUNT,
            max(indicator.period for indicator in blueprint.indicators)
            + max_lookback
            + 1,
        )

    @staticmethod
    def _validated_candles(
        candles: list[ClosedCandle],
        *,
        timeframe: str,
        interval: timedelta,
        evaluated_at: datetime,
        required_count: int,
    ) -> list[ClosedCandle]:
        if len(candles) != required_count:
            raise BlueprintSignalEvaluationBlocked(
                f"exactly {required_count} confirmed candles are required"
            )
        normalized = [
            candle.model_copy(
                update={"open_time": candle.open_time.astimezone(timezone.utc)}
            )
            for candle in candles
        ]
        for previous, current in zip(normalized, normalized[1:]):
            if current.open_time <= previous.open_time:
                raise BlueprintSignalEvaluationBlocked(
                    "candles must be strictly ordered without duplicate timestamps"
                )
            if current.open_time - previous.open_time != interval:
                raise BlueprintSignalEvaluationBlocked(
                    "candles must form one contiguous timeframe series"
                )
        latest = normalized[-1]
        interval_seconds = int(interval.total_seconds())
        alignment_anchor = (
            datetime(1970, 1, 5, tzinfo=timezone.utc)
            if timeframe.endswith("w")
            else datetime(1970, 1, 1, tzinfo=timezone.utc)
        )
        if any(
            int((candle.open_time - alignment_anchor).total_seconds())
            % interval_seconds
            != 0
            for candle in normalized
        ):
            raise BlueprintSignalEvaluationBlocked(
                "candle timestamps must align to the timeframe epoch"
            )
        if latest.open_time + interval > evaluated_at:
            raise BlueprintSignalEvaluationBlocked(
                "latest candle is not closed at evaluated_at"
            )
        if evaluated_at - (latest.open_time + interval) >= interval:
            raise BlueprintSignalEvaluationBlocked(
                "latest closed candle is stale at evaluated_at"
            )
        return normalized


def timeframe_duration(value: str) -> timedelta:
    match = TIMEFRAME_PATTERN.fullmatch(value)
    if match is None:
        raise BlueprintSignalEvaluationBlocked("strategy timeframe is unsupported")
    amount = int(match.group(1))
    unit = match.group(2)
    seconds = {
        "m": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60,
        "w": 7 * 24 * 60 * 60,
    }[unit]
    return timedelta(seconds=amount * seconds)


def _sma(values: list[Decimal], period: int) -> IndicatorSeries:
    output: IndicatorSeries = [None] * len(values)
    if len(values) < period:
        return output
    running = sum(values[:period], Decimal("0"))
    output[period - 1] = running / Decimal(period)
    for index in range(period, len(values)):
        running += values[index] - values[index - period]
        output[index] = running / Decimal(period)
    return output


def _ema(values: list[Decimal], period: int) -> IndicatorSeries:
    output: IndicatorSeries = [None] * len(values)
    if len(values) < period:
        return output
    with localcontext() as context:
        context.prec = 34
        previous = sum(values[:period], Decimal("0")) / Decimal(period)
        output[period - 1] = previous
        alpha = Decimal(2) / Decimal(period + 1)
        for index in range(period, len(values)):
            previous = (values[index] - previous) * alpha + previous
            output[index] = previous
    return output


def _rsi(values: list[Decimal], period: int) -> IndicatorSeries:
    output: IndicatorSeries = [None] * len(values)
    if len(values) <= period:
        return output
    with localcontext() as context:
        context.prec = 34
        deltas = [
            values[index] - values[index - 1]
            for index in range(1, len(values))
        ]
        average_gain = sum(
            (max(delta, Decimal("0")) for delta in deltas[:period]),
            Decimal("0"),
        ) / Decimal(period)
        average_loss = sum(
            (max(-delta, Decimal("0")) for delta in deltas[:period]),
            Decimal("0"),
        ) / Decimal(period)
        output[period] = _rsi_value(average_gain, average_loss)
        for delta_index in range(period, len(deltas)):
            gain = max(deltas[delta_index], Decimal("0"))
            loss = max(-deltas[delta_index], Decimal("0"))
            average_gain = (
                average_gain * Decimal(period - 1) + gain
            ) / Decimal(period)
            average_loss = (
                average_loss * Decimal(period - 1) + loss
            ) / Decimal(period)
            output[delta_index + 1] = _rsi_value(
                average_gain,
                average_loss,
            )
    return output


def _rsi_value(average_gain: Decimal, average_loss: Decimal) -> Decimal:
    if average_loss == 0:
        return Decimal("100") if average_gain > 0 else Decimal("0")
    relative_strength = average_gain / average_loss
    return Decimal("100") - Decimal("100") / (
        Decimal("1") + relative_strength
    )


def _atr(
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
    period: int,
) -> IndicatorSeries:
    output: IndicatorSeries = [None] * len(closes)
    if len(closes) <= period:
        return output
    true_ranges = [
        max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
        for index in range(1, len(closes))
    ]
    with localcontext() as context:
        context.prec = 34
        previous = sum(true_ranges[:period], Decimal("0")) / Decimal(period)
        output[period] = previous
        for range_index in range(period, len(true_ranges)):
            previous = (
                previous * Decimal(period - 1) + true_ranges[range_index]
            ) / Decimal(period)
            output[range_index + 1] = previous
    return output


def _evaluate_regime(
    regime_rules: list[RegimeRule],
    series_by_name: dict[str, IndicatorSeries],
    *,
    index: int,
) -> Optional[MarketRegime]:
    if not regime_rules:
        return None
    matches: list[MarketRegime] = []
    for regime_rule in regime_rules:
        matched, _ = _evaluate_rules(
            f"regime:{regime_rule.regime}",
            regime_rule.rules,
            series_by_name,
            index=index,
            active_regime=None,
        )
        if matched:
            matches.append(regime_rule.regime)
    if len(matches) != 1:
        if not matches:
            raise BlueprintSignalEvaluationBlocked(
                "latest closed candle does not match exactly one declared market regime"
            )
        raise BlueprintSignalEvaluationBlocked(
            "latest closed candle matches multiple declared market regimes"
        )
    return matches[0]


def _evaluate_rules(
    side: str,
    rules: list[SignalRule],
    series_by_name: dict[str, IndicatorSeries],
    *,
    index: int,
    active_regime: Optional[MarketRegime],
) -> tuple[bool, list[dict[str, object]]]:
    if not rules:
        return False, []
    decisions = []
    evidence = []
    for rule in rules:
        if rule.regime is not None and rule.regime != active_regime:
            decisions.append(False)
            evidence.append(
                {
                    "side": side,
                    "indicator": rule.indicator,
                    "operator": rule.operator,
                    "regime": rule.regime,
                    "active_regime": active_regime,
                    "matched": False,
                }
            )
            continue
        matched, rule_evidence = _evaluate_rule(
            rule,
            series_by_name,
            index=index,
        )
        if rule.regime is not None:
            rule_evidence.update(
                {
                    "regime": rule.regime,
                    "active_regime": active_regime,
                }
            )
        decisions.append(matched)
        evidence.append(
            {
                "side": side,
                **rule_evidence,
                "matched": matched,
            }
        )
    return all(decisions), evidence


def _evaluate_rule(
    rule: SignalRule,
    series_by_name: dict[str, IndicatorSeries],
    *,
    index: int,
) -> tuple[bool, dict[str, object]]:
    value = _series_value(series_by_name, rule.indicator, index)
    evidence: dict[str, object] = {
        "indicator": rule.indicator,
        "operator": rule.operator,
        "observed_value": _decimal_text(value),
    }
    if rule.operator in {"<", "<=", ">", ">=", "=="}:
        if rule.compare_indicator is not None:
            compare_value = _series_value(
                series_by_name,
                rule.compare_indicator,
                index,
            )
            evidence.update(
                {
                    "compare_indicator": rule.compare_indicator,
                    "observed_compare_value": _decimal_text(compare_value),
                }
            )
            matched = {
                "<": value < compare_value,
                "<=": value <= compare_value,
                ">": value > compare_value,
                ">=": value >= compare_value,
                "==": value == compare_value,
            }[rule.operator]
            return matched, evidence
        threshold = Decimal(str(rule.value))
        tolerance = max(
            Decimal("1e-12"),
            abs(threshold) * RULE_BOUNDARY_RELATIVE_TOLERANCE,
        )
        if abs(value - threshold) <= tolerance:
            raise BlueprintSignalEvaluationBlocked(
                f"indicator {rule.indicator} is inside the TA-Lib comparison boundary"
            )
        matched = {
            "<": value < threshold,
            "<=": value <= threshold,
            ">": value > threshold,
            ">=": value >= threshold,
            "==": value == threshold,
        }[rule.operator]
        evidence["threshold"] = _decimal_text(threshold)
        return matched, evidence

    previous = _series_value(
        series_by_name,
        rule.indicator,
        index - rule.lookback,
    )
    if rule.operator in {"crosses_above", "crosses_below"}:
        compare_value = _series_value(
            series_by_name,
            rule.compare_indicator,
            index,
        )
        previous_compare_value = _series_value(
            series_by_name,
            rule.compare_indicator,
            index - rule.lookback,
        )
        evidence.update(
            {
                "compare_indicator": rule.compare_indicator,
                "observed_compare_value": _decimal_text(compare_value),
                "previous_value": _decimal_text(previous),
                "previous_compare_value": _decimal_text(previous_compare_value),
                "lookback": rule.lookback,
            }
        )
        if rule.operator == "crosses_above":
            return (
                value > compare_value and previous <= previous_compare_value,
                evidence,
            )
        return (
            value < compare_value and previous >= previous_compare_value,
            evidence,
        )
    evidence.update(
        {
            "previous_value": _decimal_text(previous),
            "lookback": rule.lookback,
        }
    )
    if rule.operator == "rising":
        return value > previous, evidence
    if rule.operator == "falling":
        return value < previous, evidence
    raise BlueprintSignalEvaluationBlocked(
        f"unsupported signal operator: {rule.operator}"
    )


def _series_value(
    series_by_name: dict[str, IndicatorSeries],
    name: Optional[str],
    index: int,
) -> Decimal:
    if name is None:
        raise BlueprintSignalEvaluationBlocked(
            "relation rule is missing compare_indicator"
        )
    if index < 0:
        raise BlueprintSignalEvaluationBlocked(
            "lookback exceeds available closed candle history"
        )
    series = series_by_name.get(name)
    if series is None or index >= len(series) or series[index] is None:
        raise BlueprintSignalEvaluationBlocked(
            f"indicator {name} is unavailable at the required closed candle"
        )
    value = series[index]
    assert value is not None
    if not value.is_finite():
        raise BlueprintSignalEvaluationBlocked(
            f"indicator {name} is non-finite at the required closed candle"
        )
    return value


def _canonical_candle(candle: ClosedCandle) -> dict[str, object]:
    return {
        "open_time": _datetime_text(candle.open_time),
        "open": _decimal_text(candle.open),
        "high": _decimal_text(candle.high),
        "low": _decimal_text(candle.low),
        "close": _decimal_text(candle.close),
        "volume": _decimal_text(candle.volume),
        "confirmed": True,
    }


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _datetime_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
