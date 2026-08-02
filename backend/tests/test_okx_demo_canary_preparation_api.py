from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app
from app.models import (
    Base,
    FullChainRun,
    OkxDemoAttestedSession,
    OkxDemoTrustedSnapshot,
    ResearchJob,
    ResearchJobAttempt,
)
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.services.okx_demo_canary_preparation import (
    CANARY_PROVENANCE,
    CANARY_OPERATION,
    OkxDemoCanaryPreparationBlocked,
    OkxDemoCanaryPreparationService,
    OkxDemoCanaryPreparationWaiting,
    process_pending_canary_attestation,
)
from app.services.operator_authorization import operator_request_coordinator


NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)


def _result():
    return SimpleNamespace(
        operation_status="PREPARED",
        provenance=CANARY_PROVENANCE,
        approval_id=101,
        trade_intent_id=102,
        risk_decision_id=103,
        full_chain_run_id=104,
        research_job_id=105,
        research_job_attempt_id=106,
        reconciliation_run_id=107,
        canonical_hash="a" * 64,
        policy_digest="b" * 64,
        approved_payload_hash="c" * 64,
        client_order_id="FAICANARY" + "d" * 23,
        instrument_id="BTC-USDT-SWAP",
        quantity=Decimal("1"),
        notional=Decimal("10"),
        expires_at=NOW + timedelta(seconds=10),
        idempotency_key_digest="e" * 64,
    )


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("FREQTRADE_AI_OPERATOR_TOKEN", "operator-test-token")
    calls = []

    def prepare(_self, *, idempotency_key):
        calls.append(idempotency_key)
        return _result()

    monkeypatch.setattr(OkxDemoCanaryPreparationService, "prepare", prepare)

    def override_db():
        yield object()

    app.dependency_overrides[get_db] = override_db
    operator_request_coordinator.reset_for_tests()
    try:
        yield TestClient(app), calls
    finally:
        operator_request_coordinator.reset_for_tests()
        app.dependency_overrides.clear()


def test_canary_prepare_requires_operator_and_once_consent(client):
    api, calls = client
    no_token = api.post(
        "/api/okx-demo/canary/prepare",
        headers={"Idempotency-Key": "canary-no-token", "X-Provider-Authorization": "once"},
        json={},
    )
    assert no_token.status_code == 401
    no_consent = api.post(
        "/api/okx-demo/canary/prepare",
        headers={"X-Operator-Token": "operator-test-token", "Idempotency-Key": "canary-no-consent"},
        json={},
    )
    assert no_consent.status_code == 409
    assert calls == []


def test_canary_prepare_returns_non_production_lineage(client):
    api, calls = client
    response = api.post(
        "/api/okx-demo/canary/prepare",
        headers={
            "X-Operator-Token": "operator-test-token",
            "Idempotency-Key": "canary-prepare-1",
            "X-Provider-Authorization": "once",
        },
        json={},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["operation_status"] == "PREPARED"
    assert body["execution_target_id"] == "OKX_DEMO"
    assert body["provenance"] == CANARY_PROVENANCE
    assert body["non_production"] is True
    assert body["instrument_id"] == "BTC-USDT-SWAP"
    assert body["notional"] == "10"
    assert body["credential_values_recorded"] is False
    assert calls == ["canary-prepare-1"]


def test_canary_prepare_rejects_caller_order_overrides(client):
    api, calls = client
    response = api.post(
        "/api/okx-demo/canary/prepare",
        headers={
            "X-Operator-Token": "operator-test-token",
            "Idempotency-Key": "canary-override",
            "X-Provider-Authorization": "once",
        },
        json={"quantity": "999", "instrument_id": "ETH-USDT-SWAP"},
    )
    assert response.status_code == 422
    assert calls == []


def test_canary_finalize_retries_waiting_same_key_after_runtime_handoff(client, monkeypatch):
    api, _calls = client
    calls = []
    outcomes = [
        OkxDemoCanaryPreparationWaiting(700),
        _result(),
    ]

    def prepare(_self, *, idempotency_key):
        calls.append(idempotency_key)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(OkxDemoCanaryPreparationService, "prepare", prepare)
    headers = {
        "X-Operator-Token": "operator-test-token",
        "Idempotency-Key": "canary-finalize-retry",
        "X-Provider-Authorization": "once",
    }

    waiting = api.post("/api/okx-demo/canary/finalize", headers=headers, json={})
    prepared = api.post("/api/okx-demo/canary/finalize", headers=headers, json={})
    replay = api.post("/api/okx-demo/canary/finalize", headers=headers, json={})

    assert waiting.status_code == 202
    assert waiting.json()["operation_status"] == "WAITING_FOR_RUNTIME_ATTESTATION"
    assert prepared.status_code == 202
    assert prepared.json()["operation_status"] == "PREPARED"
    assert replay.status_code == 202
    assert replay.json() == prepared.json()
    assert calls == ["canary-finalize-retry", "canary-finalize-retry"]


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        ensure_execution_scope_catalog(db)
        yield db
    engine.dispose()


def test_prepare_without_attested_snapshots_queues_runtime_handoff(db_session, monkeypatch):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as blocked:
        OkxDemoCanaryPreparationService(
            db_session,
            now_provider=lambda: NOW,
        ).prepare(idempotency_key="runtime-handoff-1")
    job = db_session.get(ResearchJob, blocked.value.job_id)
    assert job is not None
    assert job.operation == CANARY_OPERATION
    assert job.status == "AWAITING_APPROVAL"
    assert job.stage == "CANARY_SNAPSHOT_REQUESTED"
    assert job.request_payload["provenance"] == CANARY_PROVENANCE
    assert job.request_payload["candle_limit"] == 2


def test_different_idempotency_key_cannot_create_second_pending_request(db_session, monkeypatch):
    first = OkxDemoCanaryPreparationService(
        db_session,
        now_provider=lambda: NOW,
    )
    with pytest.raises(OkxDemoCanaryPreparationWaiting):
        first.prepare(idempotency_key="pending-canary-1")
    monkeypatch.setattr(first, "_has_fresh_snapshot_rows", lambda _now: True)
    with pytest.raises(OkxDemoCanaryPreparationBlocked) as blocked:
        first.prepare(idempotency_key="pending-canary-2")
    assert "another controlled canary request" in str(blocked.value)
    assert db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).count() == 1


def test_runtime_handoff_persists_only_attested_bundle_references(db_session):
    job = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="okx_demo_controlled_canary",
        operation=CANARY_OPERATION,
        idempotency_key_digest="a" * 64,
        request_hash="b" * 64,
        request_payload={
            "provenance": CANARY_PROVENANCE,
            "execution_target": "OKX_DEMO",
            "instrument_id": "BTC-USDT-SWAP",
            "timeframe": "1m",
            "candle_limit": 2,
        },
        status="AWAITING_APPROVAL",
        stage="CANARY_SNAPSHOT_REQUESTED",
        attempt_count=0,
        max_attempts=1,
        evidence_snapshot={"provenance": CANARY_PROVENANCE},
        started_at=NOW,
    )
    db_session.add(job)
    db_session.commit()

    class Reference:
        def __init__(self, database_id, snapshot_id, digest):
            self.database_id = database_id
            self.snapshot_id = snapshot_id
            self.digest = digest

    class Bundle:
        observed_at = NOW
        expires_at = NOW + timedelta(seconds=30)
        instrument = Reference(1, "instrument-snapshot", "c" * 64)
        market = Reference(2, "market-snapshot", "d" * 64)
        account = Reference(3, "account-snapshot", "e" * 64)

    class RuntimeRead:
        def capture_trusted_signal_bundle(self, db, *, inst_id, timeframe, candle_limit):
            assert (inst_id, timeframe, candle_limit) == ("BTC-USDT-SWAP", "1m", 2)
            return Bundle()

    assert process_pending_canary_attestation(
        read_client=RuntimeRead(), db=db_session, now=NOW
    ) is True
    db_session.commit()
    refreshed = db_session.get(ResearchJob, job.id)
    assert refreshed.status == "SUCCESS"
    assert refreshed.stage == "CANARY_SNAPSHOTS_READY"
    assert refreshed.evidence_snapshot["provenance"] == CANARY_PROVENANCE
    assert set(refreshed.evidence_snapshot["snapshot_evidence"]) == {
        "instrument",
        "market",
        "account",
    }


def _seed_attested_snapshots(db_session):
    session = OkxDemoAttestedSession(
        session_id="okx-demo-test-session",
        execution_target_id="OKX_DEMO",
        pinned_fingerprint_sha256="f" * 64,
        capability_proof_digest="1" * 64,
        attestation_nonce="2" * 64,
        created_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
    )
    db_session.add(session)
    snapshots = {}
    contents = {
        "instrument": {
            "instId": "BTC-USDT-SWAP",
            "contract_shape": "linear",
            "state": "live",
            "execution_target": "OKX_DEMO",
            "source": "okx_demo_rest",
            "stale": False,
        },
        "market": {
            "instrument_id": "BTC-USDT-SWAP",
            "execution_target": "OKX_DEMO",
            "source": "okx_demo_rest",
            "stale": False,
        },
        "account": {
            "execution_target": "OKX_DEMO",
            "authenticated": True,
            "source": "okx_demo_rest",
            "stale": False,
        },
    }
    for index, kind in enumerate(("instrument", "market", "account"), 1):
        row = OkxDemoTrustedSnapshot(
            snapshot_id="{}-snapshot".format(kind),
            kind=kind,
            execution_target_id="OKX_DEMO",
            content_json=contents[kind],
            digest="{}".format(index) * 64,
            source_type="api_aggregate",
            core_data=True,
            attested_session_id=session.session_id,
            attestation_fingerprint_sha256=session.pinned_fingerprint_sha256,
            attested_session_expires_at=session.expires_at,
            observed_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )
        db_session.add(row)
        snapshots[kind] = row
    db_session.commit()
    return snapshots


def _patch_lineage_dependencies(monkeypatch, snapshots):
    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "_fresh_empty_reconciliation",
        lambda _self, _now: 1,
    )
    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "_fresh_snapshots",
        lambda _self, _now: snapshots,
    )
    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "_derive_order",
        lambda _self, _snapshots, _now: {
            "instrument_id": "BTC-USDT-SWAP",
            "quantity": Decimal("1"),
            "notional": Decimal("10"),
            "limit_price": Decimal("100"),
            "reference_price": Decimal("100"),
            "leverage": Decimal("1"),
            "expires_at": NOW + timedelta(seconds=10),
            "side": "buy",
            "position_side": "long",
            "order_type": "limit",
            "margin_mode": "isolated",
            "reduce_only": False,
            "stop_loss": Decimal("95"),
            "take_profit": Decimal("105"),
        },
    )


def test_finalize_with_fresh_snapshots_creates_lineage_job(db_session, monkeypatch):
    snapshots = _seed_attested_snapshots(db_session)
    # Fresh trusted rows allow finalize to proceed without creating a second
    # pending request.  The lineage path itself creates exactly one durable
    # canary ResearchJob and keeps strategy identifiers null.
    _patch_lineage_dependencies(monkeypatch, snapshots)
    result = OkxDemoCanaryPreparationService(
        db_session,
        now_provider=lambda: NOW,
    ).prepare(idempotency_key="lineage-reuse-1")
    assert result.provenance == CANARY_PROVENANCE
    chain = db_session.get(FullChainRun, result.full_chain_run_id)
    assert chain is not None
    assert chain.strategy_version_id is None
    assert chain.candidate_approval_id is None
    assert chain.signal_evaluation_id is None
    assert db_session.query(FullChainRun).count() == 1
    assert db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).count() == 1
    job = db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).one()
    assert job.status == "SUCCESS"
    assert job.stage == "CANARY_PREPARED"


def test_finalize_reuses_attested_runtime_request_job(db_session, monkeypatch):
    snapshots = _seed_attested_snapshots(db_session)
    _patch_lineage_dependencies(monkeypatch, snapshots)
    key = "runtime-job-reuse-1"
    job = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="okx_demo_controlled_canary",
        operation=CANARY_OPERATION,
        idempotency_key_digest=hashlib.sha256(key.encode()).hexdigest(),
        request_hash="8" * 64,
        request_payload={"provenance": CANARY_PROVENANCE},
        status="SUCCESS",
        stage="CANARY_SNAPSHOTS_READY",
        attempt_count=1,
        max_attempts=1,
        evidence_snapshot={"provenance": CANARY_PROVENANCE},
        started_at=NOW,
        completed_at=NOW,
    )
    db_session.add(job)
    db_session.commit()

    result = OkxDemoCanaryPreparationService(
        db_session,
        now_provider=lambda: NOW,
    ).prepare(idempotency_key=key)
    attempt = (
        db_session.query(ResearchJobAttempt)
        .filter_by(research_job_id=job.id, attempt_number=1)
        .one()
    )
    chain = db_session.get(FullChainRun, result.full_chain_run_id)
    assert db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).count() == 1
    assert result.research_job_id == job.id
    assert result.research_job_attempt_id == attempt.id
    assert chain.research_job_id == job.id
    assert chain.research_job_attempt_id == attempt.id


def test_derive_order_uses_attested_contract_shape_and_exchange_minimum():
    now = NOW
    expiry = NOW + timedelta(seconds=30)
    snapshots = {
        "instrument": SimpleNamespace(
            content_json={
                "instId": "BTC-USDT-SWAP",
                "contract_shape": "linear",
                "state": "live",
                "minSz": "1",
                "lotSz": "1",
                "ctVal": "0.00001",
                "tickSz": "0.1",
            },
            expires_at=expiry,
        ),
        "market": SimpleNamespace(
            content_json={
                "reference_price": "100",
                "as_of": now.isoformat(),
                "bbo": {"ask_price": "100.1", "bid_price": "99.9"},
                "mark": {"price": "100"},
            },
            expires_at=expiry,
        ),
        "account": SimpleNamespace(
            content_json={
                "leverage_by_position_side": {"long": "1"},
            },
            expires_at=expiry,
        ),
    }
    service = object.__new__(OkxDemoCanaryPreparationService)
    order = service._derive_order(snapshots, now)
    assert order["instrument_id"] == "BTC-USDT-SWAP"
    assert order["quantity"] == Decimal("1")
    assert order["limit_price"] == Decimal("99.9")
    assert order["notional"] == Decimal("0.001001")
