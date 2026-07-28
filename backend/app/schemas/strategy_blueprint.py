import math
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


BLUEPRINT_SCHEMA_VERSION = "2"
IndicatorKind = Literal["rsi", "ema", "sma"]
SignalOperator = Literal["<", "<=", ">", ">=", "=="]


class IndicatorBlueprint(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    kind: IndicatorKind
    period: int = Field(gt=1, le=500)

    model_config = {"extra": "forbid"}


class SignalRule(BaseModel):
    indicator: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    operator: SignalOperator
    value: float

    model_config = {"extra": "forbid"}

    @field_validator("value")
    @classmethod
    def validate_value_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("rule value must be finite")
        return value


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
        for rule in [
            rule
            for rules in rule_groups.values()
            for rule in rules
        ]:
            indicator = indicator_by_name.get(rule.indicator)
            if indicator is None:
                raise ValueError(f"rule indicator is not defined: {rule.indicator}")
            if indicator.kind == "rsi" and not 0 <= rule.value <= 100:
                raise ValueError(f"rsi rule value must be between 0 and 100: {rule.indicator}")
            if indicator.kind in {"ema", "sma"} and rule.value <= 0:
                raise ValueError(f"moving average rule value must be positive: {rule.indicator}")
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
