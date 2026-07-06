from collections.abc import Generator
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory, get_db
from app.main import app
from app.models.debug_mvp_seed import DebugMvpSeedPayload
from app.repositories.debug_mvp_seed_data import DebugMvpSeedDataRepository
from app.services.debug_mvp_seed_data import (
    FRONTEND_MVP_ENDPOINT_ALIASES,
    build_debug_mvp_seed_payloads,
)


def client_with_seeded_database(tmp_path: Path, seed: bool = True) -> TestClient:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'debug-seed.sqlite'}"
    engine = create_database_engine(database_url)
    DebugMvpSeedPayload.__table__.create(bind=engine, checkfirst=True)
    session_factory = create_session_factory(engine)

    if seed:
        with session_factory() as session:
            DebugMvpSeedDataRepository(session).upsert_payloads(build_debug_mvp_seed_payloads())

    def override_get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_seeded_debug_payloads_are_returned_from_api(tmp_path: Path) -> None:
    client = client_with_seeded_database(tmp_path)
    try:
        strategies = client.get("/api/mvp/strategies")
        runtime_contract = client.get("/api/mvp/runtime/read-only")
        ranking = client.get("/api/mvp/ranking")

        assert strategies.status_code == 200
        assert strategies.json()[0]["name"] == "SeededBackendRsi001"
        assert strategies.json()[0]["source"] == "backend_seeded_api"
        assert runtime_contract.status_code == 200
        assert runtime_contract.json()["fallback_status"]["active"] is False
        assert ranking.status_code == 200
        assert ranking.json()[0]["strategy_name"] == "SeededBackendRsi001"
    finally:
        app.dependency_overrides.clear()


def test_missing_seed_data_returns_clear_404(tmp_path: Path) -> None:
    client = client_with_seeded_database(tmp_path, seed=False)
    try:
        response = client.get("/api/mvp/strategies")

        assert response.status_code == 404
        assert "Seeded frontend debug data is missing" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_unknown_debug_endpoint_returns_clear_404(tmp_path: Path) -> None:
    client = client_with_seeded_database(tmp_path)
    try:
        response = client.get("/api/not-a-frontend-endpoint")

        assert response.status_code == 404
        assert response.json()["detail"] == "Not Found"
    finally:
        app.dependency_overrides.clear()


def test_seed_payload_covers_all_frontend_endpoint_groups() -> None:
    payloads = build_debug_mvp_seed_payloads()

    assert set(FRONTEND_MVP_ENDPOINT_ALIASES.values()) <= set(payloads)
