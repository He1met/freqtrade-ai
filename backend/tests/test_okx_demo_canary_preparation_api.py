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
from app.adapters.okx_demo.errors import OkxReadAdapterError
from app.main import app
from app.models import (
    Base,
    FullChainRun,
    OkxDemoAttestedSession,
    OkxDemoSubmissionGrant,
    OkxDemoTrustedSnapshot,
    ResearchJob,
    ResearchJobAttempt,
    TradeIntent,
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


def test_canary_retry_endpoint_is_idempotent_and_returns_successor(client, monkeypatch):
    api, _calls = client
    calls = []

    def retry(_self, *, idempotency_key):
        calls.append(idempotency_key)
        return SimpleNamespace(
            operation_status="WAITING_FOR_RUNTIME_ATTESTATION",
            research_job_id=701,
            retry_of_job_id=15,
            idempotency_key_digest="f" * 64,
        )

    monkeypatch.setattr(OkxDemoCanaryPreparationService, "retry_attestation", retry)
    headers = {
        "X-Operator-Token": "operator-test-token",
        "Idempotency-Key": "canary-retry-endpoint-1",
        "X-Provider-Authorization": "once",
    }
    first = api.post("/api/okx-demo/canary/retry", headers=headers, json={})
    replay = api.post("/api/okx-demo/canary/retry", headers=headers, json={})
    assert first.status_code == 202
    assert replay.status_code == 202
    assert first.json()["operation_status"] == "WAITING_FOR_RUNTIME_ATTESTATION"
    assert first.json()["attestation_request_job_id"] == 701
    assert first.json()["retry_of_job_id"] == 15
    assert first.json() == replay.json()
    assert calls == ["canary-retry-endpoint-1"]


def test_fresh_execution_only_endpoint_reports_superseded_history(client, monkeypatch):
    api, _calls = client

    def prepare_fresh(_self, *, idempotency_key):
        assert idempotency_key == "fresh-entry-endpoint-1"
        raise OkxDemoCanaryPreparationWaiting(
            702,
            entry_kind="FRESH_EXECUTION_ONLY",
            supersedes_job_ids=(15, 16),
        )

    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "prepare_fresh_execution_only",
        prepare_fresh,
    )
    headers = {
        "X-Operator-Token": "operator-test-token",
        "Idempotency-Key": "fresh-entry-endpoint-1",
        "X-Provider-Authorization": "once",
    }
    response = api.post(
        "/api/okx-demo/canary/prepare-execution-only",
        headers=headers,
        json={},
    )
    assert response.status_code == 202
    assert response.json()["entry_kind"] == "FRESH_EXECUTION_ONLY"
    assert response.json()["attestation_request_job_id"] == 702
    assert response.json()["supersedes_job_ids"] == [15, 16]


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
    assert job.request_payload["bundle_kind"] == "EXECUTION_ONLY"
    assert "candle_limit" not in job.request_payload
    assert "timeframe" not in job.request_payload


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
            "bundle_kind": "EXECUTION_ONLY",
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
        def capture_execution_attestation(self, db, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
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


def _blocked_attestation_job(
    db_session,
    *,
    key: str,
    error_message: str = "OkxReadAdapterError",
    evidence=None,
    request_payload=None,
):
    job = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="okx_demo_controlled_canary",
        operation=CANARY_OPERATION,
        idempotency_key_digest=hashlib.sha256(key.encode()).hexdigest(),
        request_hash="9" * 64,
        request_payload=request_payload
        or {
            "provenance": CANARY_PROVENANCE,
            "execution_target": "OKX_DEMO",
            "instrument_id": "BTC-USDT-SWAP",
            "bundle_kind": "EXECUTION_ONLY",
        },
        status="BLOCKED",
        stage="CANARY_SNAPSHOT_BLOCKED",
        attempt_count=1,
        max_attempts=1,
        evidence_snapshot=evidence or {"provenance": CANARY_PROVENANCE},
        error_message=error_message,
        started_at=NOW,
        completed_at=NOW,
    )
    db_session.add(job)
    db_session.commit()
    return job


def test_transient_read_failure_is_redacted_and_allows_one_successor(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    key = "transient-attestation-1"
    job = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="okx_demo_controlled_canary",
        operation=CANARY_OPERATION,
        idempotency_key_digest=hashlib.sha256(key.encode()).hexdigest(),
        request_hash="a" * 64,
        request_payload={
            "provenance": CANARY_PROVENANCE,
            "execution_target": "OKX_DEMO",
            "instrument_id": "BTC-USDT-SWAP",
            "bundle_kind": "EXECUTION_ONLY",
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

    class TransientRead:
        def capture_execution_attestation(self, *_args, **_kwargs):
            raise OkxReadAdapterError(
                kind="RATE_LIMITED",
                status="FAILED",
                message="provider payload must not persist",
                retryable=True,
                http_status=429,
            )

    assert process_pending_canary_attestation(
        read_client=TransientRead(), db=db_session, now=NOW
    ) is True
    db_session.commit()
    db_session.refresh(job)
    assert job.status == "BLOCKED"
    assert job.error_message == "OkxReadAdapterError"
    assert job.evidence_snapshot["attestation_error"] == {
        "error_type": "OkxReadAdapterError",
        "kind": "RATE_LIMITED",
        "status": "FAILED",
        "retryable": True,
    }
    assert "provider payload" not in str(job.evidence_snapshot)

    retry = OkxDemoCanaryPreparationService(
        db_session, now_provider=lambda: NOW
    ).retry_attestation(idempotency_key="transient-attestation-2")
    assert retry.operation_status == "WAITING_FOR_RUNTIME_ATTESTATION"
    assert retry.retry_of_job_id == job.id
    successor = db_session.get(ResearchJob, retry.research_job_id)
    assert successor is not None
    assert successor.status == "AWAITING_APPROVAL"
    assert successor.request_payload["retry_of_job_id"] == job.id
    assert db_session.get(ResearchJob, job.id).status == "BLOCKED"

    replay = OkxDemoCanaryPreparationService(
        db_session, now_provider=lambda: NOW
    ).retry_attestation(idempotency_key="transient-attestation-2")
    assert replay.research_job_id == retry.research_job_id
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="successor already"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).retry_attestation(idempotency_key="transient-attestation-3")


def test_legacy_read_error_allows_one_explicit_retry_without_rewriting_source(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    source = _blocked_attestation_job(db_session, key="legacy-attestation-1")
    retry = OkxDemoCanaryPreparationService(
        db_session, now_provider=lambda: NOW
    ).retry_attestation(idempotency_key="legacy-attestation-2")
    assert retry.retry_of_job_id == source.id
    db_session.refresh(source)
    assert source.status == "BLOCKED"
    assert source.error_message == "OkxReadAdapterError"
    assert source.evidence_snapshot == {"provenance": CANARY_PROVENANCE}


def test_terminal_read_error_cannot_be_retried(db_session, monkeypatch):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    _blocked_attestation_job(
        db_session,
        key="terminal-attestation-1",
        evidence={
            "provenance": CANARY_PROVENANCE,
            "attestation_error": {
                "error_type": "OkxReadAdapterError",
                "kind": "UNAUTHORIZED",
                "status": "BLOCKED",
                "retryable": False,
            },
        },
    )
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="no retryable"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).retry_attestation(idempotency_key="terminal-attestation-2")


def test_fresh_execution_only_entry_preserves_terminal_history_and_is_single_flight(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    legacy = _blocked_attestation_job(
        db_session,
        key="legacy-signal-entry-1",
        request_payload={
            "provenance": CANARY_PROVENANCE,
            "execution_target": "OKX_DEMO",
            "instrument_id": "BTC-USDT-SWAP",
            "timeframe": "1m",
            "candle_limit": 2,
            "non_production": True,
        },
    )
    terminal = _blocked_attestation_job(
        db_session,
        key="terminal-execution-entry-1",
        evidence={
            "provenance": CANARY_PROVENANCE,
            "attestation_error": {
                "error_type": "OkxReadAdapterError",
                "kind": "INVALID_SIGNAL_BUNDLE",
                "status": "BLOCKED",
                "retryable": False,
            },
        },
    )
    legacy_payload = dict(legacy.request_payload)
    terminal_evidence = dict(terminal.evidence_snapshot)
    legacy_request_hash = legacy.request_hash
    terminal_request_hash = terminal.request_hash

    with pytest.raises(OkxDemoCanaryPreparationWaiting) as waiting:
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only(idempotency_key="fresh-entry-1")
    fresh = db_session.get(ResearchJob, waiting.value.job_id)
    assert fresh is not None
    assert fresh.request_payload["entry_kind"] == "FRESH_EXECUTION_ONLY"
    assert fresh.request_payload["bundle_kind"] == "EXECUTION_ONLY"
    assert "timeframe" not in fresh.request_payload
    assert "candle_limit" not in fresh.request_payload
    assert fresh.request_payload["supersedes_job_ids"] == [legacy.id, terminal.id]
    assert db_session.get(ResearchJob, legacy.id).request_payload == legacy_payload
    assert db_session.get(ResearchJob, terminal.id).evidence_snapshot == terminal_evidence
    assert db_session.get(ResearchJob, legacy.id).request_hash == legacy_request_hash
    assert db_session.get(ResearchJob, terminal.id).request_hash == terminal_request_hash

    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="fresh execution-only"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only(idempotency_key="fresh-entry-2")
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as replay:
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only(idempotency_key="fresh-entry-1")
    assert replay.value.job_id == fresh.id


def test_fresh_execution_only_entry_finalization_retains_lineage_metadata(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    legacy = _blocked_attestation_job(
        db_session,
        key="legacy-signal-entry-2",
        request_payload={
            "provenance": CANARY_PROVENANCE,
            "execution_target": "OKX_DEMO",
            "instrument_id": "BTC-USDT-SWAP",
            "timeframe": "5m",
            "candle_limit": 2,
            "non_production": True,
        },
    )
    terminal = _blocked_attestation_job(
        db_session,
        key="terminal-execution-entry-2",
        evidence={
            "provenance": CANARY_PROVENANCE,
            "attestation_error": {
                "error_type": "OkxReadAdapterError",
                "kind": "INVALID_SIGNAL_BUNDLE",
                "status": "BLOCKED",
                "retryable": False,
            },
        },
    )
    snapshots = _seed_attested_snapshots(db_session)
    _patch_lineage_dependencies(monkeypatch, snapshots)
    service = OkxDemoCanaryPreparationService(
        db_session, now_provider=lambda: NOW
    )
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as waiting:
        service.prepare_fresh_execution_only(idempotency_key="fresh-entry-finalize")
    handoff = db_session.get(ResearchJob, waiting.value.job_id)
    handoff.status = "SUCCESS"
    handoff.stage = "CANARY_SNAPSHOTS_READY"
    handoff.attempt_count = 1
    handoff.completed_at = NOW
    handoff.evidence_snapshot = {
        "provenance": CANARY_PROVENANCE,
        "entry_kind": "FRESH_EXECUTION_ONLY",
        "supersedes_job_ids": [legacy.id, terminal.id],
    }
    db_session.commit()

    result = service.prepare_fresh_execution_only(idempotency_key="fresh-entry-finalize")
    assert result.entry_kind == "FRESH_EXECUTION_ONLY"
    assert result.supersedes_job_ids == (legacy.id, terminal.id)
    prepared = db_session.get(ResearchJob, handoff.id)
    assert prepared.request_payload["entry_kind"] == "FRESH_EXECUTION_ONLY"
    assert prepared.request_payload["supersedes_job_ids"] == [legacy.id, terminal.id]
    assert db_session.get(ResearchJob, legacy.id).status == "BLOCKED"
    assert db_session.get(ResearchJob, terminal.id).status == "BLOCKED"
    monkeypatch.setattr(
        service,
        "_reconciliation_run_id_for_approval",
        lambda _approval_id: 1,
    )
    replay = service.prepare_fresh_execution_only(idempotency_key="fresh-entry-finalize")
    assert replay.trade_intent_id == result.trade_intent_id
    assert replay.research_job_id == result.research_job_id


def test_fresh_execution_only_entry_rejects_non_demo_manifest(db_session, monkeypatch):
    class Target:
        simulated_trading = True
        allow_real_funds = False
        order_submission_enabled = False

    class Manifest:
        active_target_id = "OKX_LIVE"
        active_target = Target()

    monkeypatch.setattr(
        "app.services.okx_demo_canary_preparation.get_settings",
        lambda: type("Settings", (), {"execution_target_manifest": Manifest()})(),
    )
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="OKX_DEMO"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only(idempotency_key="fresh-non-demo")


def test_fresh_entry_rejects_unknown_terminal_error_without_new_job(db_session):
    _blocked_attestation_job(
        db_session,
        key="unknown-terminal-entry",
        evidence={
            "provenance": CANARY_PROVENANCE,
            "attestation_error": {
                "error_type": "OkxReadAdapterError",
                "kind": "UNAUTHORIZED",
                "status": "BLOCKED",
                "retryable": False,
            },
        },
    )
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="idempotency boundary"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only(idempotency_key="fresh-unknown-terminal")
    assert db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).count() == 1


def test_fresh_entry_preflight_rejects_active_grant_before_runtime_handoff(db_session):
    db_session.add(
        OkxDemoSubmissionGrant(
            grant_id="a" * 32,
            execution_target_id="OKX_DEMO",
            approval_id=9001,
            reconciliation_run_id=9002,
            canonical_hash="b" * 64,
            policy_digest="c" * 64,
            approved_payload_hash="d" * 64,
            client_order_id="ACTIVEGRANT001",
            instrument_id="BTC-USDT-SWAP",
            canary_quantity=Decimal("1"),
            canary_notional=Decimal("1"),
            request_digest="e" * 64,
            provenance=CANARY_PROVENANCE,
            status="ACTIVE",
            issued_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=10),
        )
    )
    db_session.commit()
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="grant"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only(idempotency_key="fresh-active-grant")
    assert db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).count() == 0


def test_fresh_entry_preflight_rejects_existing_canary_trade_intent(db_session):
    db_session.add(
        TradeIntent(
            execution_target_id="OKX_DEMO",
            authorization_schema_version="RISK_V1",
            intent_id="f" * 64,
            canonical_hash="1" * 64,
            policy_digest="2" * 64,
            approved_payload_hash="3" * 64,
            idempotency_key_digest="4" * 64,
            client_order_id="PRIORCANARY001",
            instrument_id="BTC-USDT-SWAP",
            side="buy",
            position_side="long",
            order_type="limit",
            quantity=Decimal("1"),
            limit_price=Decimal("100"),
            reference_price=Decimal("100"),
            leverage=Decimal("1"),
            margin_mode="isolated",
            stop_loss=Decimal("95"),
            take_profit=Decimal("105"),
            reduce_only=False,
            status="APPROVED",
            request_snapshot={"provenance": CANARY_PROVENANCE},
            expires_at=NOW + timedelta(seconds=10),
        )
    )
    db_session.commit()
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="prior controlled canary"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only(idempotency_key="fresh-prior-intent")
    assert db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).count() == 0


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
