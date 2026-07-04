from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


DRY_RUN_PROFILE_SCHEMA_VERSION = "1"
REQUIRED_LOCKED_VARIABLES = frozenset(
    {
        "profile_name",
        "strategy_version_id",
        "strategy",
        "pair",
        "timeframe",
        "exchange",
        "stake_currency",
        "stake_amount",
        "max_open_trades",
        "dry_run",
        "freq_ui_enabled",
    }
)
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
        "download_data",
        "external_message_consumer",
        "force_entry_enable",
        "hyperopt",
        "live",
        "real_order",
        "real_orders",
        "runmode",
        "telegram",
        "trade",
        "webhook",
    }
)


class DryRunProfileStrategy(BaseModel):
    version_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    file_path: Optional[str] = Field(default=None, min_length=1, max_length=500)

    model_config = {"extra": "forbid"}


class DryRunProfileStake(BaseModel):
    currency: str = Field(default="USDT", min_length=1, max_length=16)
    amount: float = Field(default=100, gt=0)
    tradable_balance_ratio: float = Field(default=0.99, gt=0, le=1)
    max_open_trades: int = Field(default=1, ge=1, le=100)

    model_config = {"extra": "forbid"}


class DryRunProfileExchange(BaseModel):
    name: str = Field(default="okx", min_length=1, max_length=80)
    trading_mode: Literal["futures", "spot"] = "futures"

    model_config = {"extra": "forbid"}


class DryRunProfileFreqUILink(BaseModel):
    enabled: bool = False
    base_url: Optional[HttpUrl] = None
    environment_label: str = Field(default="local-dry-run", min_length=1, max_length=80)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_enabled_link(self) -> "DryRunProfileFreqUILink":
        if self.enabled and self.base_url is None:
            raise ValueError("freq_ui.base_url is required when freq_ui.enabled is true")
        return self


class DryRunProfileCommandOptions(BaseModel):
    user_data_dir: str = Field(default="user_data", min_length=1, max_length=500)
    strategy_path: Optional[str] = Field(default=None, min_length=1, max_length=500)
    log_level: Literal["INFO", "WARNING", "ERROR"] = "INFO"

    model_config = {"extra": "forbid"}

    @field_validator("user_data_dir", "strategy_path")
    @classmethod
    def validate_repository_relative_path(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        path_value = value.strip()
        if path_value.startswith(("http://", "https://")):
            raise ValueError("dry-run command paths must not be URLs")
        if path_value.startswith("/"):
            raise ValueError("dry-run command paths must be repository-relative")
        if ".." in path_value.split("/"):
            raise ValueError("dry-run command paths must not contain parent traversal")
        return path_value


class DryRunProfileSafety(BaseModel):
    allow_download: Literal[False] = False
    allow_exchange_connection: Literal[False] = False
    allow_live_trading: Literal[False] = False
    allow_real_orders: Literal[False] = False
    allow_dry_run: Literal[True] = True
    dry_run: Literal[True] = True
    live_trading: Literal[False] = False

    model_config = {"extra": "forbid"}


class DryRunProfile(BaseModel):
    schema_version: Literal["1"] = DRY_RUN_PROFILE_SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    strategy: DryRunProfileStrategy
    pair: str = Field(min_length=1, max_length=80)
    timeframe: str = Field(min_length=1, max_length=32, pattern=r"^[1-9][0-9]*[mhdw]$")
    stake: DryRunProfileStake = Field(default_factory=DryRunProfileStake)
    exchange: DryRunProfileExchange = Field(default_factory=DryRunProfileExchange)
    freq_ui: DryRunProfileFreqUILink = Field(default_factory=DryRunProfileFreqUILink)
    command_options: DryRunProfileCommandOptions = Field(default_factory=DryRunProfileCommandOptions)
    safety: DryRunProfileSafety = Field(default_factory=DryRunProfileSafety)
    locked_variables: dict[str, Any] = Field(default_factory=dict)
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
    def validate_locked_variables(self) -> "DryRunProfile":
        if not self.locked_variables:
            return self

        missing_locks = sorted(REQUIRED_LOCKED_VARIABLES - set(self.locked_variables))
        if missing_locks:
            raise ValueError(f"locked_variables missing required keys: {', '.join(missing_locks)}")

        expected_locks = self._locked_variable_snapshot()
        for key, expected_value in expected_locks.items():
            if self.locked_variables.get(key) != expected_value:
                raise ValueError(f"locked_variables.{key} must match profile input")

        return self

    def to_snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def to_input_snapshot(self) -> dict[str, Any]:
        snapshot = self.to_snapshot()
        snapshot["locked_variables"] = self._locked_variable_snapshot()
        snapshot["profile_hash"] = self.profile_hash()
        return snapshot

    def to_runner_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "profile_name": self.name,
            "strategy": self.strategy.name,
            "strategy_version_id": self.strategy.version_id,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "exchange": self.exchange.name,
            "trading_mode": self.exchange.trading_mode,
            "stake_currency": self.stake.currency,
            "stake_amount": self.stake.amount,
            "tradable_balance_ratio": self.stake.tradable_balance_ratio,
            "max_open_trades": self.stake.max_open_trades,
            "dry_run": self.safety.dry_run,
            "user_data_dir": self.command_options.user_data_dir,
            "log_level": self.command_options.log_level,
            "freq_ui": {
                "enabled": self.freq_ui.enabled,
                "base_url": str(self.freq_ui.base_url) if self.freq_ui.base_url else None,
                "environment_label": self.freq_ui.environment_label,
            },
        }
        if self.strategy.file_path:
            snapshot["strategy_file_path"] = self.strategy.file_path
        if self.command_options.strategy_path:
            snapshot["strategy_path"] = self.command_options.strategy_path
        return snapshot

    def profile_hash(self) -> str:
        payload = self.to_snapshot()
        payload.pop("locked_variables", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _locked_variable_snapshot(self) -> dict[str, Any]:
        return {
            "profile_name": self.name,
            "strategy_version_id": self.strategy.version_id,
            "strategy": self.strategy.name,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "exchange": self.exchange.name,
            "stake_currency": self.stake.currency,
            "stake_amount": self.stake.amount,
            "max_open_trades": self.stake.max_open_trades,
            "dry_run": self.safety.dry_run,
            "freq_ui_enabled": self.freq_ui.enabled,
        }

    @classmethod
    def _reject_forbidden_keys(cls, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if cls._is_secret_key(normalized):
                    raise ValueError(f"dry-run profile contains forbidden secret key: {key}")
                if normalized in FORBIDDEN_RUNTIME_KEYS:
                    raise ValueError(f"dry-run profile contains forbidden runtime key: {key}")
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
