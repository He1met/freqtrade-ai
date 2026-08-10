from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.strategy_blueprint import MarketRegime, StrategyBlueprint


class ClosedCandle(BaseModel):
    """One exchange candle whose timestamp is its UTC opening time."""

    open_time: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    confirmed: Literal[True]

    model_config = {"extra": "forbid"}

    @field_validator("open_time")
    @classmethod
    def require_aware_open_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("candle open_time must be timezone-aware")
        return value

    @field_validator("open", "high", "low", "close", "volume")
    @classmethod
    def require_finite_number(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("candle numeric values must be finite")
        return value

    @model_validator(mode="after")
    def validate_price_range(self) -> "ClosedCandle":
        if self.high < max(self.open, self.close) or self.low > min(
            self.open, self.close
        ):
            raise ValueError("candle OHLC range is inconsistent")
        if self.low > self.high:
            raise ValueError("candle low must not exceed high")
        return self


class BlueprintSignalEvaluationRequest(BaseModel):
    execution_target: Literal["OKX_DEMO"]
    instrument_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Z0-9]+-[A-Z0-9]+-SWAP$",
    )
    strategy_version_id: int = Field(gt=0)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_snapshot_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]*$",
    )
    market_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    blueprint: StrategyBlueprint
    generated_code: str = Field(min_length=1)
    code_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candles: list[ClosedCandle] = Field(min_length=1, max_length=1000)
    evaluated_at: datetime

    model_config = {"extra": "forbid"}

    @field_validator("evaluated_at")
    @classmethod
    def require_aware_evaluated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")
        return value


class BlueprintSignalEvaluation(BaseModel):
    schema_version: Literal["1"] = "1"
    evaluator_version: Literal[
        "blueprint-signal-v2",
        "blueprint-signal-v2.1",
    ] = "blueprint-signal-v2.1"
    indicator_engine_version: Literal[
        "decimal-talib-golden-v1",
        "decimal-talib-golden-v2",
    ] = "decimal-talib-golden-v2"
    execution_target: Literal["OKX_DEMO"] = "OKX_DEMO"
    instrument_id: str
    strategy_version_id: int = Field(gt=0)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_snapshot_id: str
    market_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_slug: str
    class_name: str
    timeframe: str
    decision: Literal["ACTIONABLE", "NO_ACTION"]
    candle_open_at: datetime
    candle_close_at: datetime
    latest_closed_candle_at: datetime
    evaluated_at: datetime
    enter_long: bool
    enter_short: bool
    market_regime: Optional[MarketRegime] = None
    indicator_values: dict[str, str]
    rule_evidence: list[dict[str, object]]
    candle_count: int = Field(gt=0)
    signal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    model_config = {"extra": "forbid"}
