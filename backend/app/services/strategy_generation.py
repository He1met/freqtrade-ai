import json
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import ValidationError

from sqlalchemy.orm import Session

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.repositories import StrategyGenerationRunRepository, StrategyRepository
from app.schemas import (
    StrategyCreate,
    StrategyGenerationRunCreate,
    StrategyGenerationRunStatusUpdate,
    StrategyVersionCreate,
)
from app.schemas.dry_run_status import redact_secret_text
from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_file_validation import StrategyFileValidationService
from app.services.strategy_renderer import StrategyCodeRenderer


class StrategyBlueprintProvider(Protocol):
    """Stable boundary for fake, mock, and real LLM-backed blueprint providers."""

    provider_name: str
    model_name: str

    def generate(self, prompt_summary: str, requested_count: int) -> list[StrategyBlueprint]:
        ...

    def metadata_snapshot(self) -> dict[str, Any]:
        ...


class LLMProviderConfigurationError(RuntimeError):
    pass


class LLMProviderResponseError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMProviderConfig:
    """ENV-only provider configuration.

    The provider reads secret values from the environment at call time. Config
    objects store environment variable names and public endpoint metadata only.
    """

    provider_name: str
    model_name: str
    base_url: str
    api_key_env: str
    endpoint_path: str = "/chat/completions"
    temperature: float = 0.2
    timeout_seconds: float = 30.0
    max_output_tokens: Optional[int] = None

    @classmethod
    def from_env(cls) -> "LLMProviderConfig":
        provider_name = os.environ.get("STRATEGY_BLUEPRINT_PROVIDER", "fake").strip().lower()
        defaults = _provider_defaults(provider_name)
        return cls(
            provider_name=provider_name or "fake",
            model_name=os.environ.get("STRATEGY_BLUEPRINT_MODEL", defaults["model_name"]).strip()
            or defaults["model_name"],
            base_url=os.environ.get("STRATEGY_BLUEPRINT_BASE_URL", defaults["base_url"]).strip()
            or defaults["base_url"],
            api_key_env=os.environ.get("STRATEGY_BLUEPRINT_API_KEY_ENV", defaults["api_key_env"]).strip()
            or defaults["api_key_env"],
            timeout_seconds=float(os.environ.get("STRATEGY_BLUEPRINT_TIMEOUT_SECONDS", "30")),
            max_output_tokens=_optional_int_from_env("STRATEGY_BLUEPRINT_MAX_OUTPUT_TOKENS"),
        )

    def metadata_snapshot(self) -> dict[str, Any]:
        return {
            "mode": "real_provider",
            "provider": self.provider_name,
            "model": self.model_name,
            "base_url": _redact_url_userinfo(self.base_url),
            "endpoint_path": self.endpoint_path,
            "api_key_env": self.api_key_env,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
            "real_provider": True,
        }


class LLMHTTPResponse(Protocol):
    def raise_for_status(self) -> None:
        ...

    def json(self) -> dict[str, Any]:
        ...


class LLMHTTPClient(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> LLMHTTPResponse:
        ...


class OpenAICompatibleStrategyBlueprintProvider:
    """Provider boundary for OpenAI-compatible chat completion APIs.

    This class intentionally owns the vendor-shaped HTTP payload and response
    parsing so generation services do not depend on provider-specific fields.
    """

    def __init__(self, config: LLMProviderConfig, http_client: Optional[LLMHTTPClient] = None) -> None:
        self.config = config
        self.provider_name = config.provider_name
        self.model_name = config.model_name
        self.http_client = http_client or httpx.Client()

    def metadata_snapshot(self) -> dict[str, Any]:
        return self.config.metadata_snapshot()

    def generate(self, prompt_summary: str, requested_count: int) -> list[StrategyBlueprint]:
        if requested_count <= 0:
            return []

        # Keep fail-closed behavior here: real providers may only run when the
        # configured secret exists in ENV, and the secret is never copied into
        # the payload, logs, database, or exception message.
        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise LLMProviderConfigurationError(
                f"missing LLM API key environment variable: {self.config.api_key_env}"
            )

        payload = self._build_payload(prompt_summary, requested_count)
        response = self.http_client.post(
            self._endpoint_url(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.config.timeout_seconds,
        )
        try:
            response.raise_for_status()
            raw_response = response.json()
        except Exception as exc:
            raise LLMProviderResponseError("LLM provider request failed") from exc

        raw_blueprints = self._extract_blueprints(raw_response)
        if len(raw_blueprints) < requested_count:
            raise LLMProviderResponseError("LLM provider returned fewer blueprints than requested")

        blueprints: list[StrategyBlueprint] = []
        for item in raw_blueprints[:requested_count]:
            try:
                blueprints.append(StrategyBlueprint.model_validate(item))
            except ValidationError as exc:
                raise LLMProviderResponseError("LLM provider returned an invalid strategy blueprint") from exc
        return blueprints

    def _endpoint_url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/{self.config.endpoint_path.lstrip('/')}"

    def _build_payload(self, prompt_summary: str, requested_count: int) -> dict[str, Any]:
        # The prompt asks for a JSON object so validation can happen at the
        # StrategyBlueprint schema boundary before any code is rendered.
        payload: dict[str, Any] = {
            "model": self.config.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Generate Freqtrade strategy blueprints. Return only JSON with a "
                        "top-level blueprints array. Every blueprint must satisfy schema_version 2."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Requested count: {requested_count}\n"
                        f"Prompt summary: {prompt_summary}\n"
                        "Use only safe indicator metadata and do not include credentials."
                    ),
                },
            ],
            "temperature": self.config.temperature,
            "response_format": {"type": "json_object"},
        }
        if self.config.max_output_tokens is not None:
            payload["max_tokens"] = self.config.max_output_tokens
        return payload

    def _extract_blueprints(self, raw_response: dict[str, Any]) -> list[dict[str, Any]]:
        """Accept the response shapes used by chat APIs and test doubles."""

        content: Any = raw_response
        choices = raw_response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {})
            content = message.get("content")

        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise LLMProviderResponseError("LLM provider returned non-JSON blueprint content") from exc

        if isinstance(content, dict) and isinstance(content.get("blueprints"), list):
            return content["blueprints"]
        if isinstance(content, list):
            return content
        if isinstance(content, dict) and content.get("schema_version") == "2":
            return [content]
        raise LLMProviderResponseError("LLM provider response did not contain strategy blueprints")


def _optional_int_from_env(name: str) -> Optional[int]:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return int(value)


def _provider_defaults(provider_name: str) -> dict[str, str]:
    normalized = (provider_name or "fake").strip().lower()
    defaults = {
        "model_name": "gpt-4.1-mini",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    }
    if normalized == "deepseek":
        return {
            "model_name": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com",
            "api_key_env": "DEEPSEEK_API_KEY",
        }
    if normalized == "mimo":
        return {
            "model_name": "mimo-2.5-pro",
            "base_url": "https://api.example.com/v1",
            "api_key_env": "MIMO_API_KEY",
        }
    return defaults


def _redact_url_userinfo(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.username and not parsed.password:
        return url
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"[REDACTED]@{host}", parsed.path, parsed.query, parsed.fragment))


@dataclass(frozen=True)
class StrategyGenerationResult:
    run_id: int
    version_ids: list[int]


class StrategyGenerationExecutionError(RuntimeError):
    def __init__(self, message: str, run_id: int) -> None:
        super().__init__(message)
        self.run_id = run_id


class FakeStrategyBlueprintProvider:
    """Offline provider used by tests, smoke checks, and local fallback mode."""

    provider_name = "fake"
    model_name = "offline-fixture"

    def __init__(self, blueprints: Optional[list[StrategyBlueprint]] = None) -> None:
        self.blueprints = blueprints or [
            StrategyBlueprint(
                name="MVP RSI Strategy",
                slug="mvp-rsi-strategy",
                class_name="MvpRsiStrategy",
                description="Offline fixture strategy generated for Phase 1 smoke coverage.",
                indicators=[
                    {"name": "rsi", "kind": "rsi", "period": 14},
                    {"name": "ema_fast", "kind": "ema", "period": 12},
                ],
                entry_rules=[{"indicator": "rsi", "operator": "<", "value": 30}],
                exit_rules=[{"indicator": "rsi", "operator": ">", "value": 70}],
                tags=["phase-1", "fake-provider"],
            )
        ]

    def generate(self, prompt_summary: str, requested_count: int) -> list[StrategyBlueprint]:
        return self.blueprints[:requested_count]

    def metadata_snapshot(self) -> dict[str, Any]:
        return {
            "mode": "offline",
            "provider": self.provider_name,
            "model": self.model_name,
            "source": "fixture",
            "real_provider": False,
        }


def build_strategy_blueprint_provider_from_env(
    http_client: Optional[LLMHTTPClient] = None,
) -> StrategyBlueprintProvider:
    config = LLMProviderConfig.from_env()
    # The fake provider remains the default so local tests and smoke commands
    # never require network access or a real LLM key.
    if config.provider_name == "fake":
        return FakeStrategyBlueprintProvider()
    return OpenAICompatibleStrategyBlueprintProvider(config=config, http_client=http_client)


class StrategyGenerationService:
    """Coordinates one generation run without embedding provider details."""

    def __init__(
        self,
        db: Session,
        provider: StrategyBlueprintProvider,
        renderer: Optional[StrategyCodeRenderer] = None,
        file_manager: Optional[StrategyFileManager] = None,
    ) -> None:
        self.run_repository = StrategyGenerationRunRepository(db)
        self.strategy_repository = StrategyRepository(db)
        self.provider = provider
        self.renderer = renderer or StrategyCodeRenderer()
        self.file_manager = file_manager or StrategyFileManager()
        self.file_validation_service = StrategyFileValidationService(self.file_manager)

    def run_once(self, prompt_summary: str, requested_count: int = 1) -> list[int]:
        return self.run_once_with_result(prompt_summary, requested_count=requested_count).version_ids

    def run_once_with_result(
        self,
        prompt_summary: str,
        requested_count: int = 1,
    ) -> StrategyGenerationResult:
        # Persist the run before provider execution so failures are visible to
        # the UI and later quality-analysis tasks.
        run = self.run_repository.create(
            StrategyGenerationRunCreate(
                provider=self.provider.provider_name,
                model=self.provider.model_name,
                prompt_summary=prompt_summary,
                params_snapshot=self._provider_params_snapshot(),
                requested_count=requested_count,
            )
        )
        self.run_repository.update_status(
            run.id,
            StrategyGenerationRunStatusUpdate(status="running"),
        )

        try:
            blueprints = self.provider.generate(prompt_summary, requested_count)
            version_ids = self._persist_blueprints(run.id, blueprints)
        except Exception as exc:
            safe_error = self._redact_sensitive_error(str(exc))
            self.run_repository.update_status(
                run.id,
                StrategyGenerationRunStatusUpdate(
                    status="failed",
                    failed_count=requested_count,
                    error_message=safe_error,
                ),
            )
            raise StrategyGenerationExecutionError(safe_error, run.id) from exc

        self.run_repository.update_status(
            run.id,
            StrategyGenerationRunStatusUpdate(
                status="succeeded",
                generated_count=len(blueprints),
                accepted_count=len(version_ids),
                failed_count=max(0, len(blueprints) - len(version_ids)),
            ),
        )
        return StrategyGenerationResult(run_id=run.id, version_ids=version_ids)

    def _provider_params_snapshot(self) -> dict[str, Any]:
        metadata_getter = getattr(self.provider, "metadata_snapshot", None)
        if callable(metadata_getter):
            return metadata_getter()
        return {
            "mode": "unknown",
            "provider": self.provider.provider_name,
            "model": self.provider.model_name,
            "real_provider": self.provider.provider_name != "fake",
        }

    def _redact_sensitive_error(self, message: str) -> str:
        redacted = redact_secret_text(message)
        config = getattr(self.provider, "config", None)
        credential_env_name = getattr(config, "api_key_env", None)
        if isinstance(credential_env_name, str):
            secret_value = os.environ.get(credential_env_name)
            if secret_value and len(secret_value) >= 4:
                redacted = redacted.replace(secret_value, "[REDACTED]")
        return redacted

    def _persist_blueprints(
        self,
        run_id: int,
        blueprints: list[StrategyBlueprint],
    ) -> list[int]:
        version_ids: list[int] = []
        for index, blueprint in enumerate(blueprints, start=1):
            # Rendering and runnable-file validation stay behind dedicated boundaries.
            code = self.renderer.render(blueprint)
            file_result = self.file_validation_service.write_validated_strategy_file(
                class_name=blueprint.class_name,
                code=code,
                file_stem=f"{blueprint.slug}_run_{run_id}_{index}",
            )
            strategy = self.strategy_repository.get_by_slug(blueprint.slug)
            if strategy is None:
                strategy = self.strategy_repository.create(
                    StrategyCreate(
                        name=blueprint.name,
                        slug=blueprint.slug,
                        description=blueprint.description,
                        tags=blueprint.tags,
                    )
                )
            version = self.strategy_repository.create_version(
                StrategyVersionCreate(
                    strategy_id=strategy.id,
                    generation_run_id=run_id,
                    blueprint=blueprint.model_dump(),
                    generated_code=code,
                    code_hash=file_result.code_hash,
                    file_path=str(file_result.file_path),
                    validation_status=file_result.validation_status,
                    validation_errors=file_result.validation_errors,
                    diff_snapshot={"strategy_file_validation": file_result.to_snapshot()},
                )
            )
            if version is not None:
                version_ids.append(version.id)
        return version_ids
