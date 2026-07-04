from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


DryRunSnapshotStatus = Literal[
    "SUCCESS",
    "FAILED",
    "BLOCKED",
    "SKIPPED",
    "RUNNING",
    "STOPPED",
]
DryRunEventSeverity = Literal["INFO", "WARNING", "ERROR", "CRITICAL"]

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
SECRET_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?secret|secret|password|passphrase|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


class DryRunBalanceSummary(BaseModel):
    currency: Optional[str] = Field(default=None, max_length=24)
    total: Optional[float] = None
    free: Optional[float] = None
    used: Optional[float] = None
    realized_profit: Optional[float] = None
    unrealized_profit: Optional[float] = None

    model_config = {"extra": "forbid"}


class DryRunOpenTradesSummary(BaseModel):
    total_open_trades: int = Field(default=0, ge=0)
    pair_count: int = Field(default=0, ge=0)
    pairs: list[str] = Field(default_factory=list)
    total_stake_amount: Optional[float] = None
    total_profit_abs: Optional[float] = None
    total_profit_pct: Optional[float] = None

    model_config = {"extra": "forbid"}


class DryRunEvent(BaseModel):
    timestamp: datetime
    event_type: str = Field(min_length=1, max_length=120)
    severity: DryRunEventSeverity = "INFO"
    message: str = Field(min_length=1, max_length=1000)
    source: str = Field(min_length=1, max_length=120)
    details: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def redact_secret_values(cls, value: Any) -> Any:
        return redact_dry_run_status_payload(value)

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.upper()
        return value


class DryRunStatusSnapshot(BaseModel):
    status: DryRunSnapshotStatus
    profile_name: Optional[str] = Field(default=None, max_length=120)
    strategy_version_id: Optional[int] = Field(default=None, gt=0)
    strategy_name: Optional[str] = Field(default=None, max_length=120)
    exchange: Optional[str] = Field(default=None, max_length=80)
    pair: Optional[str] = Field(default=None, max_length=80)
    timeframe: Optional[str] = Field(default=None, max_length=32)
    dry_run: Optional[bool] = None
    balance_summary: DryRunBalanceSummary = Field(default_factory=DryRunBalanceSummary)
    open_trades_summary: DryRunOpenTradesSummary = Field(default_factory=DryRunOpenTradesSummary)
    recent_events: list[DryRunEvent] = Field(default_factory=list, max_length=50)
    blocked_reason: Optional[str] = Field(default=None, max_length=1000)
    failed_reason: Optional[str] = Field(default=None, max_length=1000)
    skipped_reason: Optional[str] = Field(default=None, max_length=1000)
    last_updated: datetime
    artifact_manifest_path: Optional[str] = Field(default=None, max_length=1000)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def redact_secret_values(cls, value: Any) -> Any:
        return redact_dry_run_status_payload(value)


def redact_dry_run_status_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if is_secret_key(normalized_key):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_dry_run_status_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_dry_run_status_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_dry_run_status_payload(item) for item in value]
    if isinstance(value, str):
        return redact_secret_text(value)
    return value


def redact_secret_text(value: str) -> str:
    redacted = SECRET_VALUE_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        value,
    )
    return BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED]", redacted)


def is_secret_key(normalized_key: str) -> bool:
    return (
        normalized_key in SECRET_KEY_NAMES
        or normalized_key.endswith("_secret")
        or "api_key" in normalized_key
        or "api_secret" in normalized_key
    )
