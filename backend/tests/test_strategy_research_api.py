from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app
from app.models import Base
from app.schemas.strategy_research import FormalResearchRunRead
from app.services.formal_strategy_research import get_formal_strategy_research_coordinator
from app.services.strategy_research import StrategyResearchPersistenceService


REPORT = (
    Path(__file__).resolve().parents[2]
    / "reports"
    / "research"
    / "strategy-candidates-20260806.json"
)


def test_research_endpoints_are_empty_but_explicit_before_first_batch():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        assert client.get("/api/strategy-research-batches").json() == []
        assert client.get("/api/strategy-research-candidates?status=QUALIFIED").json() == []
    finally:
        app.dependency_overrides.clear()


def test_research_api_exposes_complete_candidate_evidence():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        StrategyResearchPersistenceService(db).persist_report(
            REPORT, run_id="api-evidence", repository_commit="c" * 40
        )

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        payload = TestClient(app).get("/api/strategy-research-batches").json()
    finally:
        app.dependency_overrides.clear()

    assert len(payload) == 1
    assert payload[0]["generated_count"] == 10
    assert payload[0]["persisted_count"] == 10
    assert len(payload[0]["candidates"]) == 10
    assert all(item["evidence_snapshot"]["windows"] for item in payload[0]["candidates"])
    assert all(item["rejection_reasons"] for item in payload[0]["candidates"])


def test_formal_research_api_uses_credential_free_shared_coordinator():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    class FakeCoordinator:
        def status(self, db):
            return FormalResearchRunRead(
                status="READY", reason_code="READY", reason="ready", active=False
            )

        def start(self, db, *, trigger):
            assert trigger == "manual"
            return FormalResearchRunRead(
                status="RUNNING",
                reason_code="STARTED",
                reason="started",
                active=True,
                run_id="202608090515",
                trigger="manual",
            )

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_formal_strategy_research_coordinator] = FakeCoordinator
    try:
        client = TestClient(app)
        ready = client.get("/api/strategy-research/formal-run")
        started = client.post("/api/strategy-research/formal-run", json={})
    finally:
        app.dependency_overrides.clear()

    assert ready.status_code == 200
    assert ready.json()["status"] == "READY"
    assert started.status_code == 200
    assert started.json()["run_id"] == "202608090515"
    assert started.json()["safety"] == {
        "execution_target": "OKX_DEMO",
        "allow_real_funds": False,
        "real_orders": False,
        "credentials_collected": False,
        "dry_run_trading_authorized": False,
        "grant_authorized": False,
        "manual_order_authorized": False,
    }
