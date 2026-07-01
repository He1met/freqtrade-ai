import pytest
from typing import Optional

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
        file_manager=StrategyFileManager(output_dir=tmp_path),
    )

    version_ids = service.run_once("Generate one conservative RSI strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "succeeded"
    assert run.generated_count == 1
    assert run.accepted_count == 1
    assert run.failed_count == 0

    assert len(version_ids) == 1
    strategy = StrategyRepository(db_session).get_by_slug("mvp-rsi-strategy")
    assert strategy is not None
    version = StrategyRepository(db_session).get_latest_version(strategy.id)
    assert version is not None
    assert version.generation_run_id == run.id
    assert version.validation_status == "passed"
    assert version.blueprint["class_name"] == "MvpRsiStrategy"
    assert "class MvpRsiStrategy(IStrategy):" in version.generated_code
    assert tmp_path.joinpath("mvp_rsi_strategy_run_1_1.py").exists()


class FailingProvider(FakeStrategyBlueprintProvider):
    provider_name = "fake-failure"

    def generate(self, prompt_summary: str, requested_count: int) -> list[StrategyBlueprint]:
        raise RuntimeError("provider unavailable")


def test_generation_failure_marks_run_failed_without_strategy_version(
    db_session: Session,
    tmp_path,
) -> None:
    service = StrategyGenerationService(
        db_session,
        provider=FailingProvider(),
        file_manager=StrategyFileManager(output_dir=tmp_path),
    )

    with pytest.raises(RuntimeError):
        service.run_once("Generate one strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "failed"
    assert run.failed_count == 1
    assert run.error_message == "provider unavailable"
    assert StrategyRepository(db_session).get_by_slug("mvp-rsi-strategy") is None
    assert list(tmp_path.iterdir()) == []


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
        file_manager=StrategyFileManager(output_dir=tmp_path),
    )

    version_ids = service.run_once("Generate one strategy.", requested_count=1)

    run = StrategyGenerationRunRepository(db_session).list()[0]
    assert run.status == "succeeded"
    assert run.provider == "mimo"
    assert run.model == "mimo-test"
    assert len(version_ids) == 1
    assert StrategyRepository(db_session).get_by_slug("service-mock-rsi") is not None
