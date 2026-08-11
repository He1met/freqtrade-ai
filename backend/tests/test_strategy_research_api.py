import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import create_session_factory, get_db
from app.core.strategy_research_contract import official_research_policy
from app.main import app
from app.api.strategy_research import _get_bihourly_trigger
from app.models import Base, Strategy, StrategyVersion
from app.schemas.strategy_research import FormalResearchRunRead
from app.services.formal_strategy_research import get_formal_strategy_research_coordinator
from app.services.strategy_research import StrategyResearchPersistenceService
from app.services.bihourly_strategy_research_trigger import (
    BihourlyStrategyResearchTriggerResult,
)


REPORT = (
    Path(__file__).resolve().parents[2]
    / "reports"
    / "research"
    / "strategy-candidates-20260806.json"
)


def write_official_report(tmp_path, *, qualified=False):
    payload = json.loads(REPORT.read_text())
    payload["selection_policy"] = official_research_policy()
    if qualified:
        name = next(iter(payload["candidates"]))
        candidate = payload["candidates"][name]
        for window_name in ("wf_bull", "wf_range", "oos", "wf_bear"):
            candidate["windows"][window_name].update(
                {
                    "status": "SUCCESS",
                    "total_trades": 30,
                    "profit_pct": 0.001,
                    "max_drawdown_pct": 0.15,
                    "net_of_fee_and_slippage": True,
                    "fee_per_side": 0.0005,
                    "slippage_per_side": 0.0002,
                }
            )
        candidate["validation_passed"] = True
        candidate["deployable_candidate"] = True
        payload["qualified_candidates"] = [name]
    pairs = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    payload["environment"]["pairs"] = pairs
    payload["environment"]["timeframes"] = ["5m", "15m"]
    payload["environment"]["market_data"] = [
        {
            "pair": pair,
            "timeframe": timeframe,
            "path": f"futures/{pair.split('/')[0]}-{timeframe}.feather",
            "sha256": "a" * 64,
        }
        for pair in pairs
        for timeframe in ("5m", "15m")
    ]
    for index, evidence in enumerate(payload["candidates"].values()):
        timeframe = "5m" if index < 5 else "15m"
        evidence["declared_timeframe"] = timeframe
        evidence["deployment_target"] = {"pair": pairs[0], "timeframe": timeframe}
        evidence["targets"] = {
            f"{pair}|{timeframe}": {
                **copy.deepcopy(evidence),
                "pair": pair,
                "timeframe": timeframe,
            }
            for pair in pairs
        }
    path = tmp_path / "official-api-report.json"
    path.write_text(json.dumps(payload))
    return path


def _all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


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


def test_research_api_exposes_complete_candidate_evidence(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        StrategyResearchPersistenceService(db).persist_report(
            write_official_report(tmp_path),
            run_id="api-evidence",
            repository_commit="c" * 40,
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
    assert payload[0]["selection_policy"]["profile_label"] == "进攻型：最大回撤 15%"


def test_qualified_candidate_api_requires_exact_official_contract(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        batch = StrategyResearchPersistenceService(db).persist_report(
            write_official_report(tmp_path, qualified=True),
            run_id="api-qualified",
            repository_commit="d" * 40,
        )
        batch_id = batch.id

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        qualified = client.get(
            "/api/strategy-research-candidates?status=QUALIFIED"
        ).json()
        assert len(qualified) == 1
        assert qualified[0]["quality_contract"]["max_drawdown_per_validation_window"] == 0.15

        with factory() as db:
            batch = db.get(type(batch), batch_id)
            batch.selection_policy = {
                **batch.selection_policy,
                "max_drawdown_per_validation_window": 0.10,
            }
            db.commit()
        assert client.get(
            "/api/strategy-research-candidates?status=QUALIFIED"
        ).json() == []
    finally:
        app.dependency_overrides.clear()


def test_workspace_reports_canonical_link_unavailable_without_guessing_queue(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        batch = StrategyResearchPersistenceService(db).persist_report(
            write_official_report(tmp_path, qualified=True),
            run_id="workspace-qualified",
            repository_commit="e" * 40,
        )
        qualified_candidate = next(
            candidate for candidate in batch.candidates if candidate.status == "QUALIFIED"
        )
        # A legacy canonical row with the same human identity and digest is not
        # a bridge receipt and must never be guessed into the lifecycle.
        strategy = Strategy(
            name=qualified_candidate.candidate_name,
            slug="legacy-same-name-and-digest",
            status="draft",
            source="ai_generated",
            tags=["legacy-unlinked"],
        )
        db.add(strategy)
        db.flush()
        version = StrategyVersion(
            strategy_id=strategy.id,
            version_number=1,
            blueprint={},
            generated_code="SECRET_GENERATED_CODE_MUST_NOT_LEAK",
            code_hash=qualified_candidate.code_digest,
            file_path="user_data/strategies/legacy_same_name_and_digest.py",
            validation_status="pending",
            validation_errors=[],
            diff_snapshot={"secret": "CANONICAL_SECRET_MUST_NOT_LEAK"},
        )
        db.add(version)
        db.flush()
        strategy.current_version_id = version.id
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        response = TestClient(app).get("/api/strategy-research/workspace?attempt_limit=1")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "formal-strategy-research-workspace-v2"
    assert payload["source_type"] == "database"
    assert payload["core_data"] is True
    assert payload["execution_target_id"] == "OKX_DEMO"
    assert payload["allow_real_funds"] is False
    assert payload["real_orders"] is False
    assert payload["evidence_status"] == "COMPLETE"
    assert payload["sections"] == {
        "attempts": {"status": "AVAILABLE", "reason_code": None},
        "quality": {"status": "AVAILABLE", "reason_code": None},
        "batch": {"status": "AVAILABLE", "reason_code": None},
        "bridge": {"status": "AVAILABLE", "reason_code": None},
        "approval": {"status": "AVAILABLE", "reason_code": None},
        "deployment": {"status": "AVAILABLE", "reason_code": None},
    }
    assert payload["latest_batch"]["qualified_count"] == 1
    assert payload["handoff_status"] == "CANONICAL_LINK_UNAVAILABLE"
    assert payload["lifecycle_summary"]["status"] == "UNBRIDGED_REVALIDATION_REQUIRED"
    assert payload["candidate_lifecycles"][0]["lifecycle_status"] == (
        "UNBRIDGED_REVALIDATION_REQUIRED"
    )
    assert payload["candidate_lifecycles"][0]["bridge_event_id"] is None
    assert payload["candidate_lifecycles"][0]["canonical_strategy_id"] is None
    assert payload["candidate_lifecycles"][0]["canonical_strategy_version_id"] is None
    assert {"generated_code", "lease_token", "secret"}.isdisjoint(set(_all_keys(payload)))
    assert "MUST_NOT_LEAK" not in response.text
    assert payload["attempts"] == []


def test_workspace_get_performs_selects_without_commit_flush_or_write():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    # Match the production dependency's autoflush-disabled session contract.
    factory = create_session_factory(engine)
    db = factory()
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture_sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip())

    def _forbid_write(*_args, **_kwargs):
        raise AssertionError("GET projection attempted an ORM write boundary")

    db.commit = _forbid_write
    db.flush = _forbid_write
    db.add = _forbid_write
    db.delete = _forbid_write

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        response = TestClient(app).get("/api/strategy-research/workspace?attempt_limit=1")
    finally:
        app.dependency_overrides.clear()
        db.close()

    assert response.status_code == 200
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


def test_workspace_preserves_batch_when_new_receipt_tables_are_unavailable(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        StrategyResearchPersistenceService(db).persist_report(
            write_official_report(tmp_path),
            run_id="workspace-partial-schema",
            repository_commit="f" * 40,
        )
    Base.metadata.tables["strategy_research_attempt_events"].drop(engine)
    Base.metadata.tables["market_data_quality_receipts"].drop(engine)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        response = TestClient(app).get("/api/strategy-research/workspace?attempt_limit=1")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["evidence_status"] == "PARTIAL"
    assert payload["sections"]["attempts"] == {
        "status": "UNKNOWN",
        "reason_code": "ATTEMPT_RECEIPTS_UNAVAILABLE",
    }
    assert payload["sections"]["quality"] == {
        "status": "UNKNOWN",
        "reason_code": "MARKET_DATA_QUALITY_RECEIPTS_UNAVAILABLE",
    }
    assert payload["sections"]["batch"] == {
        "status": "AVAILABLE",
        "reason_code": None,
    }
    assert payload["latest_batch"]["run_id"] == "workspace-partial-schema"
    assert payload["attempts"] == []
    assert payload["latest_quality_receipt"] is None
    assert payload["handoff_status"] == "NOT_QUEUED_NO_QUALIFIED"


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
    assert ready.json()["quality_contract"]["profile_label"] == "进攻型：最大回撤 15%"
    assert ready.json()["quality_contract"]["max_drawdown_per_validation_window"] == 0.15


def test_manual_bihourly_generation_api_uses_shared_zero_order_trigger(monkeypatch):
    class FakeTrigger:
        def run(self, *, trigger):
            assert trigger == "manual"
            return BihourlyStrategyResearchTriggerResult(
                schema_version="bihourly-strategy-research-trigger-v1",
                status="NO_OP",
                reason_code="MARKET_DATA_NOT_FRESH",
                trigger="manual",
                run_id="2026081110",
                persisted_count=0,
                runtime_status="BLOCKED_OPENINGS",
                opening_guard="BLOCKED",
            )

    operator_token = "local-research-operator-token-123456789"
    monkeypatch.setenv("FREQTRADE_AI_OPERATOR_TOKEN", operator_token)
    app.dependency_overrides[_get_bihourly_trigger] = FakeTrigger
    try:
        response = TestClient(app).post(
            "/api/strategy-research/bihourly-generation",
            json={},
            headers={
                "X-Operator-Token": operator_token,
                "Idempotency-Key": "manual-research-2026081110",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "bihourly-strategy-research-trigger-v1",
        "status": "NO_OP",
        "reason_code": "MARKET_DATA_NOT_FRESH",
        "trigger": "manual",
        "run_id": "2026081110",
        "persisted_count": 0,
        "runtime_status": "BLOCKED_OPENINGS",
        "opening_guard": "BLOCKED",
        "generation_only": True,
        "serial_consumer_separate": True,
        "backtest_started": False,
        "deployment_started": False,
        "signal_or_order_started": False,
        "real_orders": False,
        "allow_real_funds": False,
        "exchange_access": "PUBLIC_MARKET_DATA_ONLY",
    }
