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
from app.api.strategy_generation import get_strategy_generation_service


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
    return TestClient(app), session_factory


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
