from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


SECRET_KEY_NAMES = frozenset(
    {
        "api_key",
        "api_secret",
        "authorization",
        "credential",
        "credentials",
        "key",
        "password",
        "passphrase",
        "private_key",
        "secret",
        "token",
    }
)


class FreqUILinkConfig(BaseModel):
    enabled: bool = False
    base_url: Optional[HttpUrl] = None
    environment_label: str = Field(default="local-dry-run", min_length=1, max_length=80)

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def reject_secret_shaped_config(cls, value: Any) -> Any:
        reject_secret_shaped_keys(value)
        return value

    @model_validator(mode="after")
    def validate_enabled_link(self) -> "FreqUILinkConfig":
        if self.enabled and self.base_url is None:
            raise ValueError("freq_ui.base_url is required when freq_ui.enabled is true")
        return self


class FreqUILinkMetadata(BaseModel):
    enabled: bool
    base_url: Optional[HttpUrl] = None
    environment_label: str = Field(min_length=1, max_length=80)
    blocked_reason: Optional[str] = Field(default=None, max_length=1000)
    access_mode: Literal["read-only-link"] = "read-only-link"

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    @model_validator(mode="after")
    def validate_blocked_reason(self) -> "FreqUILinkMetadata":
        if self.enabled and self.blocked_reason is not None:
            raise ValueError("enabled FreqUI metadata must not include blocked_reason")
        if not self.enabled and not self.blocked_reason:
            raise ValueError("disabled FreqUI metadata requires blocked_reason")
        return self


def reject_secret_shaped_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = normalize_config_key(key)
            if is_secret_shaped_key(normalized):
                raise ValueError(f"FreqUI config contains forbidden credential key: {key}")
            reject_secret_shaped_keys(item)
        return

    if isinstance(value, list):
        for item in value:
            reject_secret_shaped_keys(item)


def normalize_config_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")


def is_secret_shaped_key(normalized_key: str) -> bool:
    return (
        normalized_key in SECRET_KEY_NAMES
        or normalized_key.endswith("_secret")
        or "api_key" in normalized_key
        or "api_secret" in normalized_key
        or "credential" in normalized_key
        or "password" in normalized_key
        or "passphrase" in normalized_key
        or "private_key" in normalized_key
        or "token" in normalized_key
    )
