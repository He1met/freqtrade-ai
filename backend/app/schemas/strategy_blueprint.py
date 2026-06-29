from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


IndicatorKind = Literal["rsi", "ema", "sma"]
SignalOperator = Literal["<", "<=", ">", ">=", "=="]


class IndicatorBlueprint(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: IndicatorKind
    period: int = Field(gt=1, le=500)


class SignalRule(BaseModel):
    indicator: str = Field(min_length=1, max_length=80)
    operator: SignalOperator
    value: float


class StrategyBlueprint(BaseModel):
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
        return value

    @model_validator(mode="after")
    def validate_rule_indicators(self) -> "StrategyBlueprint":
        indicator_names = {indicator.name for indicator in self.indicators}
        for rule in [*self.entry_rules, *self.exit_rules]:
            if rule.indicator not in indicator_names:
                raise ValueError(f"rule indicator is not defined: {rule.indicator}")
        return self
