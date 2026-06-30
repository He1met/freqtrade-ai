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

        for rule in [*self.entry_rules, *self.exit_rules]:
            indicator = indicator_by_name.get(rule.indicator)
            if indicator is None:
                raise ValueError(f"rule indicator is not defined: {rule.indicator}")
            if indicator.kind == "rsi" and not 0 <= rule.value <= 100:
                raise ValueError(f"rsi rule value must be between 0 and 100: {rule.indicator}")
            if indicator.kind in {"ema", "sma"} and rule.value <= 0:
                raise ValueError(f"moving average rule value must be positive: {rule.indicator}")
        return self
