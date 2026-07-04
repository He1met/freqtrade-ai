from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.freq_ui import FreqUILinkConfig
from app.services.freq_ui_link import FreqUILinkMetadataService


def test_disabled_metadata_without_config_returns_blocked_reason() -> None:
    metadata = FreqUILinkMetadataService().metadata_from_config(None)

    assert metadata.enabled is False
    assert metadata.base_url is None
    assert metadata.environment_label == "local-dry-run"
    assert metadata.blocked_reason == "FreqUI link is not configured"
    assert metadata.access_mode == "read-only-link"


def test_enabled_metadata_is_generated_from_safe_config() -> None:
    metadata = FreqUILinkMetadataService().metadata_from_config(
        {
            "enabled": True,
            "base_url": "http://127.0.0.1:8080",
            "environment_label": "local-dev",
        }
    )

    assert metadata.enabled is True
    assert str(metadata.base_url) == "http://127.0.0.1:8080/"
    assert metadata.environment_label == "local-dev"
    assert metadata.blocked_reason is None
    assert metadata.access_mode == "read-only-link"


def test_metadata_can_be_generated_from_settings() -> None:
    settings = Settings(
        frequi_enabled=True,
        frequi_url="http://localhost:8080",
        frequi_environment_label="local-settings",
    )

    metadata = FreqUILinkMetadataService().metadata_from_settings(settings)

    assert metadata.enabled is True
    assert str(metadata.base_url) == "http://localhost:8080/"
    assert metadata.environment_label == "local-settings"


def test_enabled_config_requires_valid_base_url() -> None:
    with pytest.raises(ValidationError, match="valid URL"):
        FreqUILinkConfig.model_validate(
            {
                "enabled": True,
                "base_url": "not-a-url",
                "environment_label": "local-dev",
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": True, "base_url": "http://localhost:8080", "api_secret": "real-secret"},
        {"enabled": True, "base_url": "http://localhost:8080", "credentials": {"token": "real-secret"}},
        {"enabled": True, "base_url": "http://localhost:8080", "auth_token": "real-secret"},
    ],
)
def test_secret_shaped_config_is_rejected_without_rendering_secret_values(payload: dict) -> None:
    with pytest.raises(ValidationError) as exc_info:
        FreqUILinkConfig.model_validate(payload)

    rendered = str(exc_info.value)
    assert "forbidden credential key" in rendered
    assert "real-secret" not in rendered
