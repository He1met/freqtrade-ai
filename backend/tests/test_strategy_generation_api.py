from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.db.session import create_database_engine, create_session_factory
from app.main import app
from app.models import Base
from app.repositories import StrategyGenerationRunRepository, StrategyRepository
from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_generation import FakeStrategyBlueprintProvider, StrategyGenerationService
from app.api.strategy_generation import get_strategy_generation_service


class FailingProvider(FakeStrategyBlueprintProvider):
    provider_name = "api-failing-fixture"

    def generate(self, prompt_summary: str, requested_count: int) -> list[StrategyBlueprint]:
        raise RuntimeError("provider unavailable")


def client_with_generation_service(tmp_path: Path, provider: FakeStrategyBlueprintProvider) -> tuple[TestClient, object]:
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

    app.dependency_overrides[get_strategy_generation_service] = override_service
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
    assert payload["data_source"]["core_data"] is True

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
