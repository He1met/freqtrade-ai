from collections.abc import Generator
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory, get_db
from app.main import app
from app.models.base import Base


def client_with_empty_database(tmp_path: Path) -> TestClient:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'phase9-live-api.sqlite'}")
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)

    def override_get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_phase9_api_endpoints_do_not_require_frontend_debug_seed(tmp_path: Path) -> None:
    client = client_with_empty_database(tmp_path)
    try:
        assert client.get("/api/hyperopt-runs").json() == []

        live_governance = client.get("/api/live-candidates/governance")
        assert live_governance.status_code == 200
        assert live_governance.json()["source_ref"] == "backend-api:live-candidates/governance"
        assert live_governance.json()["profiles"] == []

        assert client.get("/api/governance-events").status_code == 200
        assert client.get("/api/strategy-failure-reasons").json() == []
        assert client.get("/api/strategy-version-lineage").json() == []
    finally:
        app.dependency_overrides.clear()


def test_runtime_endpoints_are_available_under_api_prefix() -> None:
    client = TestClient(app)

    runtime_contract = client.get("/api/runtime/read-only")
    operator_status = client.get("/api/runtime/operator-status")

    assert runtime_contract.status_code == 200
    assert runtime_contract.json()["safety"]["read_only"] is True
    assert operator_status.status_code == 200
    assert operator_status.json()["safety"]["reports_env_values"] is False
