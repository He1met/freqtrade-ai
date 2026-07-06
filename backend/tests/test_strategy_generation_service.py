from typing import Optional

import pytest
from sqlalchemy.orm import Session

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import StrategyGenerationRunRepository, StrategyRepository
from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_generation import (
    FakeStrategyBlueprintProvider,
    LLMProviderConfig,
    LLMProviderConfigurationError,
    LLMProviderResponseError,
    OpenAICompatibleStrategyBlueprintProvider,
    StrategyGenerationService,
    build_strategy_blueprint_provider_from_env,
)


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


def test_fake_provider_generation_creates_version_and_file(
    db_session: Session,
    tmp_path,
) -> None:
    service = StrategyGenerationService(
        db_session,
        provider=FakeStrategyBlueprintProvider(),
        file_manager=StrategyFileManager(output_dir=tmp_path, approved_roots=[tmp_path]),
    )

    version_ids = service.run_once("Generate one conservative RSI strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "succeeded"
    assert run.generated_count == 1
    assert run.accepted_count == 1
    assert run.failed_count == 0
    assert run.params_snapshot["mode"] == "offline"
    assert run.params_snapshot["real_provider"] is False

    assert len(version_ids) == 1
    strategy = StrategyRepository(db_session).get_by_slug("mvp-rsi-strategy")
    assert strategy is not None
    version = StrategyRepository(db_session).get_latest_version(strategy.id)
    assert version is not None
    assert version.generation_run_id == run.id
    assert version.validation_status == "passed"
    assert version.validation_errors == []
    assert version.code_hash is not None
    assert version.diff_snapshot["strategy_file_validation"]["write_status"] == "written"
    assert version.diff_snapshot["strategy_file_validation"]["validation_status"] == "passed"
    assert version.diff_snapshot["strategy_file_validation"]["checksum"] == version.code_hash
    assert version.blueprint["class_name"] == "MvpRsiStrategy"
    assert "class MvpRsiStrategy(IStrategy):" in version.generated_code
    assert tmp_path.joinpath("mvp_rsi_strategy_run_1_1.py").exists()


class FailingProvider(FakeStrategyBlueprintProvider):
    provider_name = "fake-failure"

    def generate(self, prompt_summary: str, requested_count: int) -> list[StrategyBlueprint]:
        raise RuntimeError("provider unavailable")


class LeakyDeepSeekProvider:
    provider_name = "deepseek"
    model_name = "deepseek-v4-pro"

    def __init__(self) -> None:
        self.config = LLMProviderConfig(
            provider_name=self.provider_name,
            model_name=self.model_name,
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        )

    def generate(self, prompt_summary: str, requested_count: int) -> list[StrategyBlueprint]:
        raise RuntimeError("provider failed api_key=test-deepseek-secret-value")

    def metadata_snapshot(self) -> dict[str, object]:
        return self.config.metadata_snapshot()


def test_generation_failure_marks_run_failed_without_strategy_version(
    db_session: Session,
    tmp_path,
) -> None:
    service = StrategyGenerationService(
        db_session,
        provider=FailingProvider(),
        file_manager=StrategyFileManager(output_dir=tmp_path, approved_roots=[tmp_path]),
    )

    with pytest.raises(RuntimeError):
        service.run_once("Generate one strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "failed"
    assert run.failed_count == 1
    assert run.error_message == "provider unavailable"
    assert StrategyRepository(db_session).get_by_slug("mvp-rsi-strategy") is None
    assert list(tmp_path.iterdir()) == []


def test_generation_fails_closed_when_strategy_directory_is_missing(
    db_session: Session,
    tmp_path,
) -> None:
    missing_dir = tmp_path / "missing" / "generated"
    service = StrategyGenerationService(
        db_session,
        provider=FakeStrategyBlueprintProvider(),
        file_manager=StrategyFileManager(output_dir=missing_dir, approved_roots=[missing_dir]),
    )

    with pytest.raises(Exception, match="strategy output directory does not exist"):
        service.run_once("Generate one conservative RSI strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "failed"
    assert run.error_message == "BLOCKED: strategy output directory does not exist"
    assert StrategyRepository(db_session).get_by_slug("mvp-rsi-strategy") is None
    assert not missing_dir.exists()


def provider_config() -> LLMProviderConfig:
    return LLMProviderConfig(
        provider_name="mimo",
        model_name="mimo-test",
        base_url="https://llm.example.test/v1",
        api_key_env="TEST_LLM_API_KEY",
        max_output_tokens=1200,
    )


def blueprint_payload(slug: str = "mock-rsi-strategy") -> dict:
    return {
        "schema_version": "2",
        "name": "Mock RSI Strategy",
        "slug": slug,
        "class_name": "MockRsiStrategy",
        "description": "Mocked LLM response for provider boundary tests.",
        "indicators": [{"name": "rsi", "kind": "rsi", "period": 14}],
        "entry_rules": [{"indicator": "rsi", "operator": "<", "value": 31}],
        "exit_rules": [{"indicator": "rsi", "operator": ">", "value": 69}],
        "tags": ["phase-2", "mock-llm"],
    }


class MockLLMResponse:
    def __init__(self, body: dict, raise_error: Optional[Exception] = None) -> None:
        self.body = body
        self.raise_error = raise_error

    def raise_for_status(self) -> None:
        if self.raise_error is not None:
            raise self.raise_error

    def json(self) -> dict:
        return self.body


class MockLLMClient:
    def __init__(self, response: MockLLMResponse) -> None:
        self.response = response
        self.requests: list[dict] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict, timeout: float) -> MockLLMResponse:
        self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self.response


def test_real_llm_provider_requires_env_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_LLM_API_KEY", raising=False)
    client = MockLLMClient(MockLLMResponse({"blueprints": [blueprint_payload()]}))
    provider = OpenAICompatibleStrategyBlueprintProvider(provider_config(), http_client=client)

    with pytest.raises(LLMProviderConfigurationError, match="TEST_LLM_API_KEY"):
        provider.generate("Generate one strategy.", requested_count=1)

    assert client.requests == []


def test_provider_metadata_redacts_base_url_userinfo() -> None:
    config = LLMProviderConfig(
        provider_name="deepseek",
        model_name="deepseek-v4-pro",
        base_url="https://user:secret@api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
    )

    metadata = config.metadata_snapshot()

    assert metadata["base_url"] == "https://[REDACTED]@api.deepseek.com/v1"
    assert "secret" not in str(metadata)


def test_provider_factory_defaults_to_fake_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRATEGY_BLUEPRINT_PROVIDER", raising=False)

    provider = build_strategy_blueprint_provider_from_env()

    assert isinstance(provider, FakeStrategyBlueprintProvider)


def test_provider_factory_uses_deepseek_env_only_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_BLUEPRINT_PROVIDER", "deepseek")
    monkeypatch.delenv("STRATEGY_BLUEPRINT_MODEL", raising=False)
    monkeypatch.delenv("STRATEGY_BLUEPRINT_BASE_URL", raising=False)
    monkeypatch.delenv("STRATEGY_BLUEPRINT_API_KEY_ENV", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client = MockLLMClient(MockLLMResponse({"blueprints": [blueprint_payload()]}))

    provider = build_strategy_blueprint_provider_from_env(http_client=client)

    assert isinstance(provider, OpenAICompatibleStrategyBlueprintProvider)
    assert provider.provider_name == "deepseek"
    assert provider.model_name == "deepseek-v4-pro"
    assert provider.config.base_url == "https://api.deepseek.com"
    assert provider.config.api_key_env == "DEEPSEEK_API_KEY"
    assert provider.metadata_snapshot()["api_key_env"] == "DEEPSEEK_API_KEY"
    assert "api_key" not in provider.metadata_snapshot()
    with pytest.raises(LLMProviderConfigurationError, match="DEEPSEEK_API_KEY"):
        provider.generate("Generate one strategy.", requested_count=1)
    assert client.requests == []


def test_provider_factory_fail_closed_when_real_mode_has_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_BLUEPRINT_PROVIDER", "mimo")
    monkeypatch.setenv("STRATEGY_BLUEPRINT_MODEL", "mimo-test")
    monkeypatch.setenv("STRATEGY_BLUEPRINT_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("STRATEGY_BLUEPRINT_API_KEY_ENV", "TEST_LLM_API_KEY")
    monkeypatch.delenv("TEST_LLM_API_KEY", raising=False)
    provider = build_strategy_blueprint_provider_from_env(
        http_client=MockLLMClient(MockLLMResponse({"blueprints": [blueprint_payload()]}))
    )

    with pytest.raises(LLMProviderConfigurationError, match="TEST_LLM_API_KEY"):
        provider.generate("Generate one strategy.", requested_count=1)


def test_real_llm_provider_uses_env_key_and_validates_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-secret-value")
    response = MockLLMResponse(
        {
            "choices": [
                {
                    "message": {
                        "content": '{"blueprints": [%s]}' % StrategyBlueprint.model_validate(
                            blueprint_payload()
                        ).model_dump_json()
                    }
                }
            ]
        }
    )
    client = MockLLMClient(response)
    provider = OpenAICompatibleStrategyBlueprintProvider(provider_config(), http_client=client)

    blueprints = provider.generate("Generate one conservative RSI strategy.", requested_count=1)

    assert len(blueprints) == 1
    assert blueprints[0].slug == "mock-rsi-strategy"
    assert client.requests[0]["url"] == "https://llm.example.test/v1/chat/completions"
    assert client.requests[0]["headers"]["Authorization"] == "Bearer test-secret-value"
    assert client.requests[0]["json"]["model"] == "mimo-test"
    assert "test-secret-value" not in str(client.requests[0]["json"])
    assert "test-secret-value" not in str(provider.metadata_snapshot())


def test_real_llm_provider_rejects_non_json_content_without_leaking_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-secret-value")
    response = MockLLMResponse({"choices": [{"message": {"content": "not json"}}]})
    provider = OpenAICompatibleStrategyBlueprintProvider(
        provider_config(),
        http_client=MockLLMClient(response),
    )

    with pytest.raises(LLMProviderResponseError) as exc_info:
        provider.generate("Generate one strategy.", requested_count=1)

    assert "test-secret-value" not in str(exc_info.value)
    assert exc_info.value.failure_category == "response_parse_error"


def test_real_llm_provider_rejects_invalid_blueprint_without_leaking_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-secret-value")
    invalid_payload = blueprint_payload()
    invalid_payload["slug"] = "Invalid Slug"
    response = MockLLMResponse({"blueprints": [invalid_payload]})
    provider = OpenAICompatibleStrategyBlueprintProvider(
        provider_config(),
        http_client=MockLLMClient(response),
    )

    with pytest.raises(LLMProviderResponseError) as exc_info:
        provider.generate("Generate one strategy.", requested_count=1)

    assert "invalid strategy blueprint" in str(exc_info.value)
    assert "blueprint_schema_error" in str(exc_info.value)
    assert "blueprints[0].slug" in str(exc_info.value)
    assert "test-secret-value" not in str(exc_info.value)
    assert exc_info.value.failure_category == "blueprint_schema_error"
    assert any("blueprints[0].slug" in item for item in exc_info.value.details)


def test_service_records_blueprint_schema_failure_diagnostics_without_secret_leak(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-secret-value")
    invalid_payload = blueprint_payload()
    invalid_payload["entry_rules"] = [{"indicator": "missing_rsi", "operator": "<", "value": 31}]
    response = MockLLMResponse({"blueprints": [invalid_payload]})
    provider = OpenAICompatibleStrategyBlueprintProvider(
        provider_config(),
        http_client=MockLLMClient(response),
    )
    service = StrategyGenerationService(
        db_session,
        provider=provider,
        file_manager=StrategyFileManager(output_dir=tmp_path, approved_roots=[tmp_path]),
    )

    with pytest.raises(RuntimeError) as exc_info:
        service.run_once("Generate one strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "failed"
    assert run.failed_count == 1
    assert "blueprint_schema_error" in (run.error_message or "")
    assert "test-secret-value" not in str(exc_info.value)
    assert "test-secret-value" not in str(run.params_snapshot)
    assert run.params_snapshot["provider_failure"]["category"] == "blueprint_schema_error"
    assert run.params_snapshot["provider_failure"]["message"] == (
        "LLM provider returned an invalid strategy blueprint"
    )
    assert any(
        "blueprints[0]" in item and "rule indicator is not defined" in item
        for item in run.params_snapshot["provider_failure"]["details"]
    )
    assert StrategyRepository(db_session).get_by_slug("mock-rsi-strategy") is None


def test_service_records_real_provider_metadata_with_mock_client(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-secret-value")
    response = MockLLMResponse({"blueprints": [blueprint_payload(slug="service-mock-rsi")]})
    provider = OpenAICompatibleStrategyBlueprintProvider(
        provider_config(),
        http_client=MockLLMClient(response),
    )
    service = StrategyGenerationService(
        db_session,
        provider=provider,
        file_manager=StrategyFileManager(output_dir=tmp_path, approved_roots=[tmp_path]),
    )

    version_ids = service.run_once("Generate one strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "succeeded"
    assert run.provider == "mimo"
    assert run.model == "mimo-test"
    assert run.params_snapshot["mode"] == "real_provider"
    assert run.params_snapshot["provider"] == "mimo"
    assert run.params_snapshot["model"] == "mimo-test"
    assert run.params_snapshot["api_key_env"] == "TEST_LLM_API_KEY"
    assert "test-secret-value" not in str(run.params_snapshot)
    assert len(version_ids) == 1
    assert StrategyRepository(db_session).get_by_slug("service-mock-rsi") is not None


def test_service_records_deepseek_success_with_mock_client(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("STRATEGY_BLUEPRINT_PROVIDER", "deepseek")
    monkeypatch.delenv("STRATEGY_BLUEPRINT_MODEL", raising=False)
    monkeypatch.delenv("STRATEGY_BLUEPRINT_BASE_URL", raising=False)
    monkeypatch.delenv("STRATEGY_BLUEPRINT_API_KEY_ENV", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-secret-value")
    response = MockLLMResponse({"blueprints": [blueprint_payload(slug="deepseek-mock-rsi")]})
    client = MockLLMClient(response)
    provider = build_strategy_blueprint_provider_from_env(http_client=client)
    service = StrategyGenerationService(
        db_session,
        provider=provider,
        file_manager=StrategyFileManager(output_dir=tmp_path, approved_roots=[tmp_path]),
    )

    version_ids = service.run_once("Generate one DeepSeek strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "succeeded"
    assert run.provider == "deepseek"
    assert run.model == "deepseek-v4-pro"
    assert run.params_snapshot["mode"] == "real_provider"
    assert run.params_snapshot["base_url"] == "https://api.deepseek.com"
    assert run.params_snapshot["api_key_env"] == "DEEPSEEK_API_KEY"
    assert "test-deepseek-secret-value" not in str(run.params_snapshot)
    assert client.requests[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert client.requests[0]["json"]["model"] == "deepseek-v4-pro"
    assert "test-deepseek-secret-value" not in str(client.requests[0]["json"])
    assert len(version_ids) == 1
    assert StrategyRepository(db_session).get_by_slug("deepseek-mock-rsi") is not None


def test_service_records_deepseek_failed_run_without_secret_leak(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-secret-value")
    service = StrategyGenerationService(
        db_session,
        provider=LeakyDeepSeekProvider(),
        file_manager=StrategyFileManager(output_dir=tmp_path, approved_roots=[tmp_path]),
    )

    with pytest.raises(RuntimeError) as exc_info:
        service.run_once("Generate one DeepSeek strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "failed"
    assert run.provider == "deepseek"
    assert run.model == "deepseek-v4-pro"
    assert run.failed_count == 1
    assert run.params_snapshot["mode"] == "real_provider"
    assert run.params_snapshot["api_key_env"] == "DEEPSEEK_API_KEY"
    assert run.error_message == "provider failed api_key=[REDACTED]"
    assert "test-deepseek-secret-value" not in str(exc_info.value)
    assert "test-deepseek-secret-value" not in str(run.params_snapshot)
    assert StrategyRepository(db_session).get_by_slug("mvp-rsi-strategy") is None
