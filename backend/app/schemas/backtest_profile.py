from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


BACKTEST_PROFILE_SCHEMA_VERSION = "2"
SECRET_KEY_NAMES = frozenset(
    {
        "api_key",
        "api_secret",
        "key",
        "password",
        "passphrase",
        "secret",
        "token",
    }
)
FORBIDDEN_RUNTIME_KEYS = frozenset(
    {
        "api_server",
        "dry_run",
        "external_message_consumer",
        "force_entry_enable",
        "hyperopt",
        "runmode",
        "telegram",
        "webhook",
    }
)


class BacktestProfileStake(BaseModel):
    currency: str = Field(default="USDT", min_length=1, max_length=16)
    amount: float = Field(default=100, gt=0)
    tradable_balance_ratio: float = Field(default=0.99, gt=0, le=1)
    max_open_trades: int = Field(default=1, ge=1, le=100)

    model_config = {"extra": "forbid"}


class BacktestProfileStrategy(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    path: Optional[str] = Field(default=None, min_length=1, max_length=500)

    model_config = {"extra": "forbid"}


class BacktestProfileDataSource(BaseModel):
    kind: Literal["local"] = "local"
    exchange: str = Field(default="okx", min_length=1, max_length=80)
    datadir: str = Field(default="user_data/data", min_length=1, max_length=500)
    data_format: Optional[str] = Field(default=None, min_length=1, max_length=32)
    trading_mode: Optional[Literal["spot", "futures", "margin"]] = None
    margin_mode: Optional[Literal["isolated", "cross"]] = None

    model_config = {"extra": "forbid"}


class BacktestProfileSafety(BaseModel):
    allow_download: Literal[False] = False
    allow_exchange_connection: Literal[False] = False
    allow_dry_run: Literal[False] = False
    allow_live_trading: Literal[False] = False
    allow_hyperopt: Literal[False] = False

    model_config = {"extra": "forbid"}


class BacktestProfileV2(BaseModel):
    schema_version: Literal["2"] = BACKTEST_PROFILE_SCHEMA_VERSION
    profile_name: str = Field(min_length=1, max_length=120)
    pair: str = Field(min_length=1, max_length=80)
    timeframe: str = Field(min_length=1, max_length=32, pattern=r"^[1-9][0-9]*[mhdw]$")
    timerange: str = Field(min_length=17, max_length=80, pattern=r"^[0-9]{8}-[0-9]{8}$")
    strategy: BacktestProfileStrategy
    stake: BacktestProfileStake = Field(default_factory=BacktestProfileStake)
    data_source: BacktestProfileDataSource = Field(default_factory=BacktestProfileDataSource)
    safety: BacktestProfileSafety = Field(default_factory=BacktestProfileSafety)
    tags: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_and_runtime_keys(cls, value: Any) -> Any:
        cls._reject_forbidden_keys(value)
        return value

    @field_validator("pair")
    @classmethod
    def validate_pair(cls, value: str) -> str:
        pair = value.strip()
        if "/" not in pair:
            raise ValueError("pair must use Freqtrade pair notation such as BTC/USDT")
        return pair

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for tag in value:
            clean = tag.strip()
            if not clean:
                raise ValueError("tags must not contain blank values")
            if clean not in seen:
                normalized.append(clean)
                seen.add(clean)
        return normalized

    @model_validator(mode="after")
    def validate_timerange_order(self) -> "BacktestProfileV2":
        start, end = self.timerange.split("-", maxsplit=1)
        if start >= end:
            raise ValueError("timerange start must be before end")
        return self

    def to_snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def to_config_snapshot(self) -> dict[str, Any]:
        exchange_snapshot: dict[str, Any] = {"name": self.data_source.exchange}
        if self.data_source.trading_mode:
            exchange_snapshot["trading_mode"] = self.data_source.trading_mode
        if self.data_source.margin_mode:
            exchange_snapshot["margin_mode"] = self.data_source.margin_mode

        snapshot: dict[str, Any] = {
            "profile_name": self.profile_name,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "timerange": self.timerange,
            "strategy": self.strategy.name,
            "exchange": exchange_snapshot,
            "datadir": self.data_source.datadir,
            "stake_currency": self.stake.currency,
            "stake_amount": self.stake.amount,
            "tradable_balance_ratio": self.stake.tradable_balance_ratio,
            "max_open_trades": self.stake.max_open_trades,
        }
        if self.strategy.path:
            snapshot["strategy_path"] = self.strategy.path
        return snapshot

    @classmethod
    def _reject_forbidden_keys(cls, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if cls._is_secret_key(normalized):
                    raise ValueError(f"backtest profile contains forbidden secret key: {key}")
                if normalized in FORBIDDEN_RUNTIME_KEYS:
                    raise ValueError(f"backtest profile contains forbidden runtime key: {key}")
                cls._reject_forbidden_keys(item)
            return

        if isinstance(value, list):
            for item in value:
                cls._reject_forbidden_keys(item)

    @staticmethod
    def _is_secret_key(normalized_key: str) -> bool:
        return (
            normalized_key in SECRET_KEY_NAMES
            or normalized_key.endswith("_secret")
            or "api_key" in normalized_key
            or "api_secret" in normalized_key
        )
