import math
from typing import Literal, Optional

from pydantic import (
    BaseModel,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)


BLUEPRINT_SCHEMA_VERSION = "2"
IndicatorKind = Literal["rsi", "ema", "sma"]
SignalOperator = Literal[
    "<",
    "<=",
    ">",
    ">=",
    "==",
    "crosses_above",
    "crosses_below",
    "rising",
    "falling",
]
MarketRegime = Literal["bull", "bear", "range"]

_VALUE_OPERATORS = {"<", "<=", ">", ">=", "=="}
_CROSSING_OPERATORS = {"crosses_above", "crosses_below"}
_TREND_OPERATORS = {"rising", "falling"}


class IndicatorBlueprint(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    kind: IndicatorKind
    period: int = Field(gt=1, le=500)

    model_config = {"extra": "forbid"}


class SignalRule(BaseModel):
    indicator: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    operator: SignalOperator
    # A rule either compares an indicator with a finite numeric threshold or
    # with another declared indicator.  Cross/trend rules use ``lookback``
    # to inspect only prior closed candles; arbitrary Python expressions are
    # intentionally not part of this contract.
    value: Optional[StrictFloat] = None
    compare_indicator: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    lookback: StrictInt = Field(default=1, ge=1, le=500)
    regime: Optional[MarketRegime] = None

    model_config = {"extra": "forbid"}

    @field_validator("value")
    @classmethod
    def validate_value_is_finite(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and not math.isfinite(value):
            raise ValueError("rule value must be finite")
        return value

    @model_validator(mode="after")
    def validate_expression_shape(self) -> "SignalRule":
        if self.operator in _VALUE_OPERATORS:
            if self.value is None and self.compare_indicator is None:
                raise ValueError(
                    "comparison rules require value or compare_indicator"
                )
            if self.value is not None and self.compare_indicator is not None:
                raise ValueError(
                    "comparison rules cannot set both value and compare_indicator"
                )
            if self.lookback != 1:
                raise ValueError(
                    "lookback is only supported for crossing or trend rules"
                )
        elif self.operator in _CROSSING_OPERATORS:
            if self.compare_indicator is None or self.value is not None:
                raise ValueError(
                    "crossing rules require compare_indicator and no value"
                )
        elif self.operator in _TREND_OPERATORS:
            if self.compare_indicator is not None or self.value is not None:
                raise ValueError("trend rules require no value or compare_indicator")
        return self


class RegimeRule(BaseModel):
    """Closed-candle indicator conditions defining one market regime."""

    regime: MarketRegime
    rules: list[SignalRule] = Field(min_length=1, max_length=8)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def reject_nested_regime_gates(self) -> "RegimeRule":
        if any(rule.regime is not None for rule in self.rules):
            raise ValueError("regime rules cannot contain nested regime gates")
        return self


class StrategyBlueprint(BaseModel):
    schema_version: Literal["2"] = BLUEPRINT_SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=160)
    slug: str = Field(min_length=1, max_length=180, pattern=r"^[a-z0-9][a-z0-9-]*$")
    class_name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = None
    timeframe: str = Field(default="5m", pattern=r"^[1-9][0-9]*[mhdw]$")
    stoploss: float = Field(default=-0.1, gt=-1.0, lt=0)
    minimal_roi: dict[str, float] = Field(default_factory=lambda: {"0": 0.03})
    indicators: list[IndicatorBlueprint] = Field(min_length=1)
    entry_rules: list[SignalRule] = Field(min_length=1)
    exit_rules: list[SignalRule] = Field(default_factory=list)
    can_short: bool = False
    short_entry_rules: list[SignalRule] = Field(default_factory=list)
    short_exit_rules: list[SignalRule] = Field(default_factory=list)
    regime_rules: list[RegimeRule] = Field(default_factory=list, max_length=3)
    tags: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @field_validator("class_name")
    @classmethod
    def validate_class_name(cls, value: str) -> str:
        if not value.isidentifier() or not value[0].isupper():
            raise ValueError("class_name must be a valid Python class name")
        return value

    @field_validator("minimal_roi")
    @classmethod
    def validate_minimal_roi(cls, value: dict[str, float]) -> dict[str, float]:
        if not value:
            raise ValueError("minimal_roi must contain at least one step")
        for key, roi in value.items():
            if not key.isdigit():
                raise ValueError("minimal_roi keys must be minute offsets")
            if roi < 0:
                raise ValueError("minimal_roi values must be non-negative")
            if roi > 10:
                raise ValueError("minimal_roi values must not exceed 10.0")
        return value

    @model_validator(mode="after")
    def validate_blueprint_consistency(self) -> "StrategyBlueprint":
        indicator_names: set[str] = set()
        indicator_by_name: dict[str, IndicatorBlueprint] = {}
        for indicator in self.indicators:
            if indicator.name in indicator_names:
                raise ValueError(f"indicator names must be unique: {indicator.name}")
            indicator_names.add(indicator.name)
            indicator_by_name[indicator.name] = indicator

        rule_groups = {
            "entry_rules": self.entry_rules,
            "exit_rules": self.exit_rules,
            "short_entry_rules": self.short_entry_rules,
            "short_exit_rules": self.short_exit_rules,
        }
        regime_by_name: dict[str, RegimeRule] = {}
        for regime_rule in self.regime_rules:
            if regime_rule.regime in regime_by_name:
                raise ValueError(
                    f"regime rules must be unique: {regime_rule.regime}"
                )
            regime_by_name[regime_rule.regime] = regime_rule
        declared_regimes = set(regime_by_name)
        for rule in [
            rule
            for rules in rule_groups.values()
            for rule in rules
        ] + [rule for item in self.regime_rules for rule in item.rules]:
            indicator = indicator_by_name.get(rule.indicator)
            if indicator is None:
                raise ValueError(f"rule indicator is not defined: {rule.indicator}")
            if rule.compare_indicator is not None and rule.compare_indicator not in indicator_by_name:
                raise ValueError(
                    f"rule compare_indicator is not defined: {rule.compare_indicator}"
                )
            if rule.regime is not None and rule.regime not in declared_regimes:
                raise ValueError(
                    f"rule regime is not defined: {rule.regime}"
                )
            if rule.value is None:
                continue
            if indicator.kind == "rsi" and not 0 <= rule.value <= 100:
                raise ValueError(f"rsi rule value must be between 0 and 100: {rule.indicator}")
            if indicator.kind in {"ema", "sma"} and rule.value <= 0:
                raise ValueError(f"moving average rule value must be positive: {rule.indicator}")
        signal_rules = [
            rule
            for rules in rule_groups.values()
            for rule in rules
        ]
        if self.regime_rules and (
            not signal_rules or any(rule.regime is None for rule in signal_rules)
        ):
            raise ValueError(
                "regime_rules require every signal rule to declare a regime"
            )
        if self.can_short and not self.short_entry_rules:
            raise ValueError(
                "can_short strategies require short_entry_rules"
            )
        if not self.can_short and (
            self.short_entry_rules or self.short_exit_rules
        ):
            raise ValueError(
                "short rules require can_short=true"
            )
        for group_name, rules in rule_groups.items():
            self._validate_rule_group_is_possible(group_name, rules)
        return self

    @staticmethod
    def _validate_rule_group_is_possible(
        group_name: str,
        rules: list[SignalRule],
    ) -> None:
        by_indicator: dict[str, list[SignalRule]] = {}
        for rule in rules:
            # Dynamic indicator relations and temporal rules cannot be
            # reduced to a static threshold interval.  They are evaluated by
            # the deterministic closed-candle evaluator instead.
            if rule.value is None or rule.operator not in _VALUE_OPERATORS:
                continue
            by_indicator.setdefault(rule.indicator, []).append(rule)
        for indicator, indicator_rules in by_indicator.items():
            lower: Optional[tuple[float, bool]] = None
            upper: Optional[tuple[float, bool]] = None
            equal_values: set[float] = set()
            for rule in indicator_rules:
                if rule.operator == "==":
                    equal_values.add(rule.value)
                elif rule.operator in {">", ">="}:
                    candidate = (rule.value, rule.operator == ">=")
                    if lower is None or candidate[0] > lower[0]:
                        lower = candidate
                    elif candidate[0] == lower[0]:
                        lower = (lower[0], lower[1] and candidate[1])
                elif rule.operator in {"<", "<="}:
                    candidate = (rule.value, rule.operator == "<=")
                    if upper is None or candidate[0] < upper[0]:
                        upper = candidate
                    elif candidate[0] == upper[0]:
                        upper = (upper[0], upper[1] and candidate[1])
            impossible = len(equal_values) > 1
            if equal_values:
                value = next(iter(equal_values))
                impossible = impossible or (
                    lower is not None
                    and (
                        value < lower[0]
                        or (value == lower[0] and not lower[1])
                    )
                )
                impossible = impossible or (
                    upper is not None
                    and (
                        value > upper[0]
                        or (value == upper[0] and not upper[1])
                    )
                )
            if lower is not None and upper is not None:
                impossible = impossible or lower[0] > upper[0]
                impossible = impossible or (
                    lower[0] == upper[0]
                    and not (lower[1] and upper[1])
                )
            if impossible:
                raise ValueError(
                    "{} contains impossible AND conditions for {}".format(
                        group_name,
                        indicator,
                    )
                )
