from __future__ import annotations

from typing import Any, Mapping, Optional, Union

from app.core.config import Settings
from app.schemas.freq_ui import FreqUILinkConfig, FreqUILinkMetadata


DEFAULT_DISABLED_REASON = "FreqUI link is not configured"
EXPLICIT_DISABLED_REASON = "FreqUI link is disabled"


class FreqUILinkMetadataService:
    def metadata_from_settings(self, settings: Settings) -> FreqUILinkMetadata:
        return self.metadata_from_config(
            {
                "enabled": settings.frequi_enabled,
                "base_url": settings.frequi_url,
                "environment_label": settings.frequi_environment_label,
            }
        )

    def metadata_from_config(
        self,
        config: Optional[Union[FreqUILinkConfig, Mapping[str, Any]]],
    ) -> FreqUILinkMetadata:
        if config is None:
            return self._disabled(DEFAULT_DISABLED_REASON)

        if isinstance(config, FreqUILinkConfig):
            validated = config
        else:
            if not config:
                return self._disabled(DEFAULT_DISABLED_REASON)
            validated = FreqUILinkConfig.model_validate(dict(config))

        if not validated.enabled:
            reason = EXPLICIT_DISABLED_REASON
            if validated.base_url is None:
                reason = DEFAULT_DISABLED_REASON
            return self._disabled(reason, environment_label=validated.environment_label)

        return FreqUILinkMetadata(
            enabled=True,
            base_url=validated.base_url,
            environment_label=validated.environment_label,
            blocked_reason=None,
        )

    def _disabled(
        self,
        blocked_reason: str,
        environment_label: str = "local-dry-run",
    ) -> FreqUILinkMetadata:
        return FreqUILinkMetadata(
            enabled=False,
            base_url=None,
            environment_label=environment_label,
            blocked_reason=blocked_reason,
        )
