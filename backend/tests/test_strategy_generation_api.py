from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.db.session import create_database_engine, create_session_factory, get_db
from app.main import app
from app.models import Base
from app.repositories import StrategyGenerationRunRepository, StrategyRepository
from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_generation import (
    FakeStrategyBlueprintProvider,
    LLMProviderConfig,
    OpenAICompatibleStrategyBlueprintProvider,
    StrategyBlueprintProvider,
    StrategyGenerationService,
)
from app.api.strategy_generation import (
    get_deepseek_single_generation_service,
    get_strategy_generation_service,
)


class FailingProvider(FakeStrategyBlueprintProvider):
    provider_name = "api-failing-fixture"

    def generate(self, prompt_summary: str, requested_count: int) -> list[StrategyBlueprint]:
        raise RuntimeError("provider unavailable")


def blueprint_payload(slug: str = "api-deepseek-rsi") -> dict:
    return {
        "schema_version": "2",
        "name": "API DeepSeek RSI Strategy",
        "slug": slug,
        "class_name": "ApiDeepseekRsiStrategy",
        "description": "Mocked real-provider response for API database-chain acceptance.",
        "indicators": [{"name": "rsi", "kind": "rsi", "period": 14}],
        "entry_rules": [{"indicator": "rsi", "operator": "<", "value": 32}],
        "exit_rules": [{"indicator": "rsi", "operator": ">", "value": 68}],
        "tags": ["phase-9", "real-provider-mock"],
    }


class MockLLMResponse:
    def __init__(self, body: dict) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.body


class FailingMockLLMResponse(MockLLMResponse):
    def raise_for_status(self) -> None:
        raise RuntimeError("test provider failure")


class MockLLMClient:
    def __init__(self, response: MockLLMResponse) -> None:
        self.response = response
        self.requests: list[dict] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict, timeout: float) -> MockLLMResponse:
        self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self.response


def client_with_generation_service(tmp_path: Path, provider: StrategyBlueprintProvider) -> tuple[TestClient, object]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'generation-api.sqlite'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    output_dir = tmp_path / "strategies"
    output_dir.mkdir()

    def override_service() -> Generator[StrategyGenerationService, None, None]:
        db = session_factory()
        try:
            yield StrategyGenerationService(
                db,
                provider=provider,
                file_manager=StrategyFileManager(output_dir=output_dir, approved_roots=[output_dir]),
            )
        finally:
            db.close()

    def override_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_strategy_generation_service] = override_service
    app.dependency_overrides[get_db] = override_db
    return TestClient(
        app,
        headers={
            "X-Operator-Token": "synthetic-test-operator-token",
            "Idempotency-Key": "strategy-generation-test",
            "X-Provider-Authorization": "once",
        },
    ), session_factory


def client_with_deepseek_single_service(
    tmp_path: Path,
    provider: StrategyBlueprintProvider,
) -> tuple[TestClient, object]:
    client, session_factory = client_with_generation_service(tmp_path, provider)

    def override_service() -> Generator[StrategyGenerationService, None, None]:
        db = session_factory()
        output_dir = tmp_path / "deepseek-strategies"
        output_dir.mkdir(exist_ok=True)
        try:
            yield StrategyGenerationService(
                db,
                provider=provider,
                file_manager=StrategyFileManager(output_dir=output_dir, approved_roots=[output_dir]),
            )
        finally:
            db.close()

    app.dependency_overrides[get_deepseek_single_generation_service] = override_service
    return client, session_factory


def test_strategy_generation_api_persists_run_strategy_and_version(tmp_path: Path) -> None:
    client, session_factory = client_with_generation_service(tmp_path, FakeStrategyBlueprintProvider())
    try:
        response = client.post(
            "/api/strategy-generation-runs",
            json={
                "prompt_summary": "Generate one local Phase 8 strategy.",
                "requested_count": 1,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["status"] == "succeeded"
    assert payload["run"]["data_source"]["source_type"] == "database"
    assert payload["run"]["data_source"]["core_data"] is True
    assert payload["strategy_versions"][0]["generation_run_id"] == payload["run"]["id"]
    assert payload["strategy_versions"][0]["data_source"]["database_ids"]["strategy_version_id"]
    assert payload["data_source"]["source_type"] == "api_aggregate"
    assert payload["evidence"]["status"] == "SUCCESS"
    assert payload["evidence"]["acceptance_ready"] is True
    assert payload["evidence"]["ids"]["strategy_generation_run_id"] == payload["run"]["id"]
    assert payload["evidence"]["ids"]["strategy_version_id"] == payload["strategy_versions"][0]["id"]
    assert payload["data_source"]["core_data"] is True
    assert payload["data_source"]["database_ids"]["strategy_id"] == payload["strategies"][0]["id"]
    assert payload["data_source"]["database_ids"]["strategy_version_id"] == payload["strategy_versions"][0]["id"]

    with session_factory() as db:
        run = StrategyGenerationRunRepository(db).get(payload["run"]["id"])
        assert run is not None
        assert run.status == "succeeded"
        strategy = StrategyRepository(db).get(payload["strategies"][0]["id"])
        version = StrategyRepository(db).get_version(payload["strategy_versions"][0]["id"])
        assert strategy is not None
        assert version is not None
        assert version.generation_run_id == run.id
        assert Path(version.file_path).exists()


def test_strategy_generation_api_rejects_invalid_operator_token_before_provider_or_database(
    tmp_path: Path,
) -> None:
    provider = FakeStrategyBlueprintProvider()
    client, session_factory = client_with_generation_service(tmp_path, provider)
    try:
        response = client.post(
            "/api/strategy-generation-runs",
            headers={"X-Operator-Token": "wrong-synthetic-token"},
            json={
                "prompt_summary": "This request must not reach the provider.",
                "requested_count": 1,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.json()["detail"]["operation_status"] == "UNAUTHORIZED"
    assert "wrong-synthetic-token" not in response.text
    with session_factory() as db:
        assert StrategyGenerationRunRepository(db).list() == []


def test_strategy_generation_api_rejects_more_than_one_strategy_per_request(
    tmp_path: Path,
) -> None:
    client, session_factory = client_with_generation_service(tmp_path, FakeStrategyBlueprintProvider())
    try:
        response = client.post(
            "/api/strategy-generation-runs",
            json={"prompt_summary": "Do not generate a batch.", "requested_count": 2},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    with session_factory() as db:
        assert StrategyGenerationRunRepository(db).list() == []


def test_strategy_generation_api_persists_real_provider_database_chain_with_mock_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-secret-value")
    http_client = MockLLMClient(MockLLMResponse({"blueprints": [blueprint_payload()]}))
    provider = OpenAICompatibleStrategyBlueprintProvider(
        LLMProviderConfig(
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key_env="TEST_LLM_API_KEY",
        ),
        http_client=http_client,
    )
    client, session_factory = client_with_generation_service(tmp_path, provider)
    try:
        response = client.post(
            "/api/strategy-generation-runs",
            json={
                "prompt_summary": "Generate one DeepSeek strategy through the real-provider boundary.",
                "requested_count": 1,
            },
        )
        list_response = client.get("/api/strategy-generation-runs")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert list_response.status_code == 200
    payload = response.json()
    listed_runs = list_response.json()
    run_id = payload["run"]["id"]
    strategy_id = payload["strategies"][0]["id"]
    strategy_version_id = payload["strategy_versions"][0]["id"]

    assert payload["run"]["provider"] == "deepseek"
    assert payload["run"]["model"] == "deepseek-v4-pro"
    assert payload["run"]["params_snapshot"]["mode"] == "real_provider"
    assert payload["run"]["params_snapshot"]["provider"] == "deepseek"
    assert payload["run"]["params_snapshot"]["api_key_env"] == "TEST_LLM_API_KEY"
    assert payload["run"]["status"] == "succeeded"
    assert payload["run"]["accepted_count"] == 1
    assert payload["strategy_versions"][0]["generation_run_id"] == run_id
    assert payload["strategy_versions"][0]["data_source"]["database_ids"]["generation_run_id"] == run_id
    assert payload["data_source"]["database_ids"] == {
        "strategy_generation_run_id": run_id,
        "strategy_id": strategy_id,
        "first_strategy_id": strategy_id,
        "strategy_version_id": strategy_version_id,
        "first_strategy_version_id": strategy_version_id,
    }
    assert any(item["id"] == run_id and item["provider"] == "deepseek" for item in listed_runs)
    assert len(http_client.requests) == 1
    assert http_client.requests[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert http_client.requests[0]["json"]["model"] == "deepseek-v4-pro"
    assert "test-secret-value" not in str(http_client.requests[0]["json"])
    assert "test-secret-value" not in str(payload)
    assert "test-secret-value" not in str(listed_runs)

    with session_factory() as db:
        run = StrategyGenerationRunRepository(db).get(run_id)
        strategy = StrategyRepository(db).get(strategy_id)
        version = StrategyRepository(db).get_version(strategy_version_id)
        assert run is not None
        assert run.status == "succeeded"
        assert run.provider == "deepseek"
        assert run.params_snapshot["real_provider"] is True
        assert "test-secret-value" not in str(run.params_snapshot)
        assert strategy is not None
        assert strategy.current_version_id == strategy_version_id
        assert version is not None
        assert version.strategy_id == strategy_id
        assert version.generation_run_id == run_id
        assert version.validation_status == "passed"
        assert Path(version.file_path).exists()


def test_strategy_generation_api_blocks_real_provider_without_credential_before_http_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_LLM_API_KEY", raising=False)
    http_client = MockLLMClient(MockLLMResponse({"blueprints": [blueprint_payload()]}))
    provider = OpenAICompatibleStrategyBlueprintProvider(
        LLMProviderConfig(
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key_env="TEST_LLM_API_KEY",
        ),
        http_client=http_client,
    )
    client, session_factory = client_with_generation_service(tmp_path, provider)
    try:
        response = client.post(
            "/api/strategy-generation-runs",
            json={"prompt_summary": "Do not call the real Provider.", "requested_count": 1},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"]["operation_status"] == "BLOCKED"
    assert http_client.requests == []
    with session_factory() as db:
        runs = StrategyGenerationRunRepository(db).list()
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].params_snapshot["credential_values_recorded"] is False


def test_strategy_generation_api_failure_persists_failed_run(tmp_path: Path) -> None:
    client, session_factory = client_with_generation_service(tmp_path, FailingProvider())
    try:
        response = client.post(
            "/api/strategy-generation-runs",
            json={
                "prompt_summary": "Generate one local Phase 8 strategy.",
                "requested_count": 1,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["strategy_generation_run_id"]
    assert "provider unavailable" in detail["failed_reason"]
    assert detail["evidence"]["status"] == "FAILED"
    assert detail["evidence"]["acceptance_ready"] is False
    assert detail["evidence"]["ids"]["strategy_generation_run_id"] > 0

    with session_factory() as db:
        run = StrategyGenerationRunRepository(db).get(detail["strategy_generation_run_id"])
        assert run is not None
        assert run.status == "failed"
        assert run.failed_count == 1
        assert run.error_message == "provider unavailable"
        assert StrategyRepository(db).get_by_slug("mvp-rsi-strategy") is None


def test_strategy_generation_api_rejects_empty_prompt(tmp_path: Path) -> None:
    client, _ = client_with_generation_service(tmp_path, FakeStrategyBlueprintProvider())
    try:
        response = client.post(
            "/api/strategy-generation-runs",
            json={"prompt_summary": "", "requested_count": 1},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_deepseek_single_api_defaults_to_persisted_blocked_without_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-secret-value")
    http_client = MockLLMClient(MockLLMResponse({"blueprints": [blueprint_payload()]}))
    provider = OpenAICompatibleStrategyBlueprintProvider(
        LLMProviderConfig(
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key_env="TEST_LLM_API_KEY",
        ),
        http_client=http_client,
    )
    client, session_factory = client_with_deepseek_single_service(tmp_path, provider)
    try:
        response = client.post(
            "/api/strategy-generation-runs/deepseek-single",
            json={"prompt_summary": "Generate exactly one strategy."},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["evidence"]["status"] == "BLOCKED"
    assert detail["evidence"]["acceptance_ready"] is False
    assert http_client.requests == []
    with session_factory() as db:
        run = StrategyGenerationRunRepository(db).get(detail["strategy_generation_run_id"])
        assert run is not None
        assert run.status == "failed"
        assert run.params_snapshot["provider_kind"] == "real"
        assert run.params_snapshot["operation_status"] == "BLOCKED"
        assert run.params_snapshot["real_call_authorized"] is False
        assert run.params_snapshot["real_call_attempted"] is False
        assert "test-secret-value" not in str(run.params_snapshot)


def test_deepseek_single_api_blocks_missing_env_key_before_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_LLM_API_KEY", raising=False)
    http_client = MockLLMClient(MockLLMResponse({"blueprints": [blueprint_payload()]}))
    provider = OpenAICompatibleStrategyBlueprintProvider(
        LLMProviderConfig(
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key_env="TEST_LLM_API_KEY",
        ),
        http_client=http_client,
    )
    client, session_factory = client_with_deepseek_single_service(tmp_path, provider)
    try:
        response = client.post(
            "/api/strategy-generation-runs/deepseek-single",
            json={"prompt_summary": "Generate exactly one strategy.", "allow_real_call": True},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["evidence"]["status"] == "BLOCKED"
    assert http_client.requests == []
    with session_factory() as db:
        run = StrategyGenerationRunRepository(db).get(detail["strategy_generation_run_id"])
        assert run is not None
        assert run.params_snapshot["real_call_authorized"] is True
        assert run.params_snapshot["real_call_attempted"] is False
        assert run.params_snapshot["credential_env_present"] is False


def test_deepseek_single_api_authorized_success_uses_one_mock_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_secret = "test-secret-value"
    monkeypatch.setenv("TEST_LLM_API_KEY", test_secret)
    http_client = MockLLMClient(MockLLMResponse({"blueprints": [blueprint_payload("single-deepseek")]}))
    provider = OpenAICompatibleStrategyBlueprintProvider(
        LLMProviderConfig(
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key_env="TEST_LLM_API_KEY",
        ),
        http_client=http_client,
    )
    client, session_factory = client_with_deepseek_single_service(tmp_path, provider)
    try:
        response = client.post(
            "/api/strategy-generation-runs/deepseek-single",
            json={"prompt_summary": "Generate exactly one strategy.", "allow_real_call": True},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["evidence"]["status"] == "SUCCESS"
    assert payload["evidence"]["acceptance_ready"] is True
    assert len(http_client.requests) == 1
    assert http_client.requests[0]["json"]["model"] == "deepseek-v4-pro"
    assert test_secret not in str(payload)
    with session_factory() as db:
        run = StrategyGenerationRunRepository(db).get(payload["run"]["id"])
        assert run is not None
        assert run.requested_count == 1
        assert run.params_snapshot["provider_kind"] == "real"
        assert run.params_snapshot["real_call_authorized"] is True
        assert run.params_snapshot["real_call_attempted"] is True
        assert run.params_snapshot["operation_status"] == "SUCCESS"
        assert test_secret not in str(run.params_snapshot)


def test_deepseek_single_api_provider_failure_persists_failed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_secret = "test-secret-value"
    monkeypatch.setenv("TEST_LLM_API_KEY", test_secret)
    http_client = MockLLMClient(FailingMockLLMResponse({}))
    provider = OpenAICompatibleStrategyBlueprintProvider(
        LLMProviderConfig(
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key_env="TEST_LLM_API_KEY",
        ),
        http_client=http_client,
    )
    client, session_factory = client_with_deepseek_single_service(tmp_path, provider)
    try:
        response = client.post(
            "/api/strategy-generation-runs/deepseek-single",
            json={"prompt_summary": "Generate exactly one strategy.", "allow_real_call": True},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["evidence"]["status"] == "FAILED"
    assert detail["evidence"]["acceptance_ready"] is False
    assert len(http_client.requests) == 1
    assert test_secret not in str(detail)
    with session_factory() as db:
        run = StrategyGenerationRunRepository(db).get(detail["strategy_generation_run_id"])
        assert run is not None
        assert run.status == "failed"
        assert run.params_snapshot["operation_status"] == "FAILED"
        assert run.params_snapshot["provider_failure"]["category"] == "provider_request_error"
        assert test_secret not in str(run.params_snapshot)
