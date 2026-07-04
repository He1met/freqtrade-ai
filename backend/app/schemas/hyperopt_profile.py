from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


HYPEROPT_PROFILE_SCHEMA_VERSION = "1"
MAX_HYPEROPT_EPOCHS = 500
ALLOWED_HYPEROPT_SPACES = frozenset({"buy", "sell", "roi", "stoploss", "trailing"})
ALLOWED_HYPEROPT_LOSSES = frozenset(
    {
        "CalmarHyperOptLoss",
        "MaxDrawDownHyperOptLoss",
        "MaxDrawDownRelativeHyperOptLoss",
        "OnlyProfitHyperOptLoss",
        "ProfitDrawDownHyperOptLoss",
        "SharpeHyperOptLoss",
        "SharpeHyperOptLossDaily",
        "ShortTradeDurHyperOptLoss",
        "SortinoHyperOptLoss",
        "SortinoHyperOptLossDaily",
    }
)
REQUIRED_LOCKED_VARIABLES = frozenset(
    {
        "pair",
        "timeframe",
        "timerange",
        "local_data_source",
        "strategy_version_id",
        "spaces",
        "epochs",
        "hyperopt_loss",
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
        "api_server",
        "download_data",
        "external_message_consumer",
        "force_entry_enable",
        "runmode",
        "telegram",
        "webhook",
    }
)


class HyperoptProfileStrategy(BaseModel):
    version_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    file_path: str = Field(min_length=1, max_length=500)

    model_config = {"extra": "forbid"}


class HyperoptProfileLocalDataSource(BaseModel):
    kind: Literal["local"] = "local"
    root: str = Field(default="user_data/data", min_length=1, max_length=500)
    exchange: str = Field(default="okx", min_length=1, max_length=80)
    relative_path: str = Field(min_length=1, max_length=500)
    data_format: Optional[str] = Field(default=None, min_length=1, max_length=32)

    model_config = {"extra": "forbid"}

    @field_validator("root", "relative_path")
    @classmethod
    def validate_local_path(cls, value: str) -> str:
        path_value = value.strip()
        if path_value.startswith(("http://", "https://")):
            raise ValueError("local data paths must not be URLs")
        if path_value.startswith("/"):
            raise ValueError("local data paths must be repository-relative")
        if ".." in path_value.split("/"):
            raise ValueError("local data paths must not contain parent traversal")
        return path_value


class HyperoptProfileSafety(BaseModel):
    allow_download: Literal[False] = False
    allow_exchange_connection: Literal[False] = False
    allow_dry_run: Literal[False] = False
    allow_live_trading: Literal[False] = False
    dry_run: Literal[False] = False
    live_trading: Literal[False] = False

    model_config = {"extra": "forbid"}


class HyperoptProfile(BaseModel):
    schema_version: Literal["1"] = HYPEROPT_PROFILE_SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    strategy: HyperoptProfileStrategy
    backtest_profile_id: Optional[int] = Field(default=None, gt=0)
    pair: str = Field(min_length=1, max_length=80)
    timeframe: str = Field(min_length=1, max_length=32, pattern=r"^[1-9][0-9]*[mhdw]$")
    timerange: str = Field(min_length=17, max_length=80, pattern=r"^[0-9]{8}-[0-9]{8}$")
    local_data_source: HyperoptProfileLocalDataSource
    spaces: list[str] = Field(min_length=1)
    epochs: int = Field(gt=0, le=MAX_HYPEROPT_EPOCHS)
    hyperopt_loss: str = Field(min_length=1, max_length=120)
    random_state: Optional[int] = Field(default=None, ge=0)
    max_open_trades: int = Field(default=1, ge=1, le=100)
    stake_currency: str = Field(default="USDT", min_length=1, max_length=16)
    safety: HyperoptProfileSafety = Field(default_factory=HyperoptProfileSafety)
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

    @field_validator("spaces")
    @classmethod
    def validate_spaces(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for space in value:
            clean = space.strip().lower()
            if clean not in ALLOWED_HYPEROPT_SPACES:
                raise ValueError(f"unsupported hyperopt space: {space}")
            if clean not in seen:
                normalized.append(clean)
                seen.add(clean)
        return normalized

    @field_validator("hyperopt_loss")
    @classmethod
    def validate_hyperopt_loss(cls, value: str) -> str:
        loss = value.strip()
        if loss not in ALLOWED_HYPEROPT_LOSSES:
            raise ValueError(f"unsupported hyperopt loss: {value}")
        return loss

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
    def validate_timerange_data_source_and_locks(self) -> "HyperoptProfile":
        start, end = self.timerange.split("-", maxsplit=1)
        if start >= end:
            raise ValueError("timerange start must be before end")

        path_token = self.local_data_source.relative_path.replace("/", "_").replace("-", "_")
        pair_token = self.pair.replace("/", "_").replace(":", "_")
        if pair_token not in path_token:
            raise ValueError("local_data_source must match pair")
        if self.timeframe not in self.local_data_source.relative_path:
            raise ValueError("local_data_source must match timeframe")

        missing_locks = sorted(REQUIRED_LOCKED_VARIABLES - set(self.locked_variables))
        if missing_locks:
            raise ValueError(f"locked_variables missing required keys: {', '.join(missing_locks)}")

        expected_locks = {
            "pair": self.pair,
            "timeframe": self.timeframe,
            "timerange": self.timerange,
            "local_data_source": self.local_data_source.relative_path,
            "strategy_version_id": self.strategy.version_id,
            "spaces": self.spaces,
            "epochs": self.epochs,
            "hyperopt_loss": self.hyperopt_loss,
        }
        for key, expected_value in expected_locks.items():
            if self.locked_variables.get(key) != expected_value:
                raise ValueError(f"locked_variables.{key} must match profile input")

        return self

    def to_snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def to_runner_snapshot(self) -> dict[str, Any]:
        return {
            "profile_name": self.name,
            "strategy": self.strategy.name,
            "strategy_version_id": self.strategy.version_id,
            "strategy_file_path": self.strategy.file_path,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "timerange": self.timerange,
            "datadir": f"{self.local_data_source.root}/{self.local_data_source.exchange}",
            "data_path": self.local_data_source.relative_path,
            "spaces": list(self.spaces),
            "epochs": self.epochs,
            "hyperopt_loss": self.hyperopt_loss,
            "random_state": self.random_state,
            "max_open_trades": self.max_open_trades,
            "stake_currency": self.stake_currency,
        }

    @classmethod
    def _reject_forbidden_keys(cls, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if cls._is_secret_key(normalized):
                    raise ValueError(f"hyperopt profile contains forbidden secret key: {key}")
                if normalized in FORBIDDEN_RUNTIME_KEYS:
                    raise ValueError(f"hyperopt profile contains forbidden runtime key: {key}")
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
