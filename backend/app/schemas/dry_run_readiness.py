from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


DryRunReadinessStatus = Literal["READY", "BLOCKED"]


class DryRunReadinessRequest(BaseModel):
    strategy_version_id: int = Field(gt=0)
    strategy_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    pair: str = Field(default="BTC/USDT:USDT", min_length=1, max_length=80)
    timeframe: str = Field(default="15m", min_length=1, max_length=32, pattern=r"^[1-9][0-9]*[mhdw]$")
    exchange: str = Field(default="okx", min_length=1, max_length=80)
    trading_mode: Literal["futures", "spot"] = "futures"
    stake_currency: str = Field(default="USDT", min_length=1, max_length=16)
    stake_amount: float = Field(default=100, gt=0)
    max_open_trades: int = Field(default=1, ge=1, le=100)
    market_data_dir: Optional[str] = Field(default=None, min_length=1, max_length=500)
    required_env_vars: tuple[str, ...] = Field(
        default=("FREQTRADE_DRY_RUN_API_KEY", "FREQTRADE_DRY_RUN_API_SECRET"),
        min_length=1,
        max_length=8,
    )
    optional_env_vars: tuple[str, ...] = Field(
        default=("FREQTRADE_DRY_RUN_API_PASSPHRASE",),
        max_length=8,
    )
    dry_run: bool = True
    allow_exchange_connection: bool = False
    allow_live_trading: bool = False
    allow_real_orders: bool = False

    model_config = {"extra": "forbid"}

    @field_validator("pair")
    @classmethod
    def validate_pair(cls, value: str) -> str:
        pair = value.strip()
        if "/" not in pair:
            raise ValueError("pair must use Freqtrade pair notation such as BTC/USDT")
        return pair

    @field_validator("market_data_dir")
    @classmethod
    def validate_market_data_dir(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value.startswith(("http://", "https://")):
            raise ValueError("market_data_dir must be local")
        return value


class DryRunReadinessCheck(BaseModel):
    name: str
    status: DryRunReadinessStatus
    summary: str
    blocked_reason: Optional[str] = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class DryRunReadinessReport(BaseModel):
    status: DryRunReadinessStatus
    generated_at: datetime
    strategy_version_id: int
    profile_name: str
    blocked_reasons: list[str]
    checks: list[DryRunReadinessCheck]
    env_preflight: dict[str, Any]
    config_preview: dict[str, Any]
    safety: dict[str, bool]
