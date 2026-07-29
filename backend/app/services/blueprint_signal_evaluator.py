from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Callable, Optional

from app.schemas.strategy_blueprint import SignalRule, StrategyBlueprint
from app.schemas.strategy_signal import (
    BlueprintSignalEvaluation,
    BlueprintSignalEvaluationRequest,
    ClosedCandle,
)
from app.services.strategy_renderer import StrategyCodeRenderer


EVALUATOR_VERSION = "blueprint-signal-v1"
INDICATOR_ENGINE_VERSION = "decimal-talib-golden-v1"
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
        closes = [candle.close for candle in candles]
        series_by_name: dict[str, IndicatorSeries] = {}
        for indicator in blueprint.indicators:
            calculator = {
                "rsi": _rsi,
                "ema": _ema,
                "sma": _sma,
            }[indicator.kind]
            series_by_name[indicator.name] = calculator(closes, indicator.period)

        latest_values: dict[str, Decimal] = {}
        for name, series in series_by_name.items():
            value = series[-1]
            if value is None or not value.is_finite():
                raise BlueprintSignalEvaluationBlocked(
                    f"indicator {name} is unavailable on the latest closed candle"
                )
            latest_values[name] = value

        enter_long, long_evidence = _evaluate_rules(
            "long",
            blueprint.entry_rules,
            latest_values,
        )
        enter_short, short_evidence = _evaluate_rules(
            "short",
            blueprint.short_entry_rules if blueprint.can_short else [],
            latest_values,
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
        return max(
            STARTUP_CANDLE_COUNT,
            max(indicator.period for indicator in blueprint.indicators) + 1,
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


def _evaluate_rules(
    side: str,
    rules: list[SignalRule],
    indicator_values: dict[str, Decimal],
) -> tuple[bool, list[dict[str, object]]]:
    operators: dict[str, Callable[[Decimal, Decimal], bool]] = {
        "<": lambda left, right: left < right,
        "<=": lambda left, right: left <= right,
        ">": lambda left, right: left > right,
        ">=": lambda left, right: left >= right,
        "==": lambda left, right: left == right,
    }
    if not rules:
        return False, []
    decisions = []
    evidence = []
    for rule in rules:
        value = indicator_values[rule.indicator]
        threshold = Decimal(str(rule.value))
        tolerance = max(
            Decimal("1e-12"),
            abs(threshold) * RULE_BOUNDARY_RELATIVE_TOLERANCE,
        )
        if abs(value - threshold) <= tolerance:
            raise BlueprintSignalEvaluationBlocked(
                f"indicator {rule.indicator} is inside the TA-Lib comparison boundary"
            )
        matched = operators[rule.operator](value, threshold)
        decisions.append(matched)
        evidence.append(
            {
                "side": side,
                "indicator": rule.indicator,
                "operator": rule.operator,
                "threshold": _decimal_text(threshold),
                "observed_value": _decimal_text(value),
                "matched": matched,
            }
        )
    return all(decisions), evidence


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
