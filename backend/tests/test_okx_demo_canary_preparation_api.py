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
    ExchangePosition,
    ResearchJob,
    ResearchJobAttempt,
    TradeIntent,
)
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.services.okx_demo_canary_preparation import (
    CANARY_PROVENANCE,
    CANARY_OPERATION,
    FRESH_EXECUTION_ONLY_ENTRY,
    FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY,
    FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY,
    FRESH_EXECUTION_ONLY_RECOVERY,
    FRESH_EXECUTION_ONLY_REFRESH,
    FRESH_EXECUTION_ONLY_REFRESH_RETRY,
    OkxDemoCanaryPreparationBlocked,
    OkxDemoCanaryPreparationRuntimeBusy,
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


def test_refresh_execution_only_endpoint_reports_source_lineage(client, monkeypatch):
    api, _calls = client

    def refresh(_self, *, idempotency_key):
        assert idempotency_key == "fresh-refresh-endpoint-1"
        raise OkxDemoCanaryPreparationWaiting(
            703,
            entry_kind=FRESH_EXECUTION_ONLY_REFRESH,
            supersedes_job_ids=(15, 16, 17),
            refresh_of_job_id=17,
        )

    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "prepare_fresh_execution_only_refresh",
        refresh,
    )
    response = api.post(
        "/api/okx-demo/canary/refresh-execution-only",
        headers={
            "X-Operator-Token": "operator-test-token",
            "Idempotency-Key": "fresh-refresh-endpoint-1",
            "X-Provider-Authorization": "once",
        },
        json={},
    )
    assert response.status_code == 202
    assert response.json()["entry_kind"] == FRESH_EXECUTION_ONLY_REFRESH
    assert response.json()["refresh_of_job_id"] == 17
    assert response.json()["supersedes_job_ids"] == [15, 16, 17]


def test_refresh_runtime_lock_contention_is_retryable_for_same_key(client, monkeypatch):
    api, _calls = client
    calls = []
    prepared_result = _result()
    prepared_result.entry_kind = FRESH_EXECUTION_ONLY_REFRESH
    outcomes = [
        OkxDemoCanaryPreparationRuntimeBusy(),
        prepared_result,
    ]

    def refresh(_self, *, idempotency_key):
        calls.append(idempotency_key)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "prepare_fresh_execution_only_refresh",
        refresh,
    )
    headers = {
        "X-Operator-Token": "operator-test-token",
        "Idempotency-Key": "fresh-refresh-lock-retry-1",
        "X-Provider-Authorization": "once",
    }

    waiting = api.post(
        "/api/okx-demo/canary/refresh-execution-only",
        headers=headers,
        json={},
    )
    prepared = api.post(
        "/api/okx-demo/canary/refresh-execution-only",
        headers=headers,
        json={},
    )
    replay = api.post(
        "/api/okx-demo/canary/refresh-execution-only",
        headers=headers,
        json={},
    )

    assert waiting.status_code == 202
    assert waiting.json()["operation_status"] == "WAITING_FOR_RUNTIME_ATTESTATION"
    assert "attestation_request_job_id" not in waiting.json()
    assert prepared.status_code == 202
    assert prepared.json()["operation_status"] == "PREPARED"
    assert replay.status_code == 202
    assert replay.json() == prepared.json()
    assert calls == ["fresh-refresh-lock-retry-1", "fresh-refresh-lock-retry-1"]


def test_recovery_endpoint_reports_bounded_lineage(client, monkeypatch):
    api, _calls = client

    def recover(_self, *, idempotency_key):
        assert idempotency_key == "fresh-recovery-endpoint-1"
        raise OkxDemoCanaryPreparationWaiting(
            704,
            entry_kind=FRESH_EXECUTION_ONLY_RECOVERY,
            supersedes_job_ids=(15, 16, 17, 18, 19),
            recovery_of_job_id=19,
        )

    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "prepare_fresh_execution_only_recovery",
        recover,
    )
    response = api.post(
        "/api/okx-demo/canary/recover-execution-only",
        headers={
            "X-Operator-Token": "operator-test-token",
            "Idempotency-Key": "fresh-recovery-endpoint-1",
            "X-Provider-Authorization": "once",
        },
        json={},
    )
    assert response.status_code == 202
    assert response.json()["operation_status"] == "WAITING_FOR_RUNTIME_ATTESTATION"
    assert response.json()["entry_kind"] == FRESH_EXECUTION_ONLY_RECOVERY
    assert response.json()["recovery_of_job_id"] == 19
    assert response.json()["supersedes_job_ids"] == [15, 16, 17, 18, 19]


def test_post_persistence_endpoint_reports_single_successor(client, monkeypatch):
    api, _calls = client

    def recover(_self, *, idempotency_key):
        assert idempotency_key == "post-persistence-endpoint-1"
        raise OkxDemoCanaryPreparationWaiting(
            905,
            entry_kind=FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY,
            supersedes_job_ids=(15, 16, 17, 18, 19, 20),
            recovery_of_job_id=20,
        )

    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "prepare_post_persistence_recovery",
        recover,
    )
    response = api.post(
        "/api/okx-demo/canary/recover-post-persistence",
        headers={
            "X-Operator-Token": "operator-test-token",
            "Idempotency-Key": "post-persistence-endpoint-1",
            "X-Provider-Authorization": "once",
        },
        json={},
    )
    assert response.status_code == 202
    assert response.json()["entry_kind"] == (
        FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY
    )
    assert response.json()["recovery_of_job_id"] == 20
    assert response.json()["supersedes_job_ids"] == [15, 16, 17, 18, 19, 20]


def test_final_expiry_endpoint_reports_single_successor(client, monkeypatch):
    api, _calls = client

    def recover(_self, *, idempotency_key):
        assert idempotency_key == "final-expiry-endpoint-1"
        raise OkxDemoCanaryPreparationWaiting(
            906,
            entry_kind=FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY,
            supersedes_job_ids=(15, 16, 17, 18, 19, 20, 21),
            recovery_of_job_id=21,
        )

    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "prepare_final_expiry_recovery",
        recover,
    )
    response = api.post(
        "/api/okx-demo/canary/recover-final-expiry",
        headers={
            "X-Operator-Token": "operator-test-token",
            "Idempotency-Key": "final-expiry-endpoint-1",
            "X-Provider-Authorization": "once",
        },
        json={},
    )
    assert response.status_code == 202
    assert response.json()["entry_kind"] == (
        FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY
    )
    assert response.json()["recovery_of_job_id"] == 21
    assert response.json()["supersedes_job_ids"] == [15, 16, 17, 18, 19, 20, 21]


def test_consent_finalize_endpoint_persists_one_bounded_request(client, monkeypatch):
    api, _calls = client
    calls = []

    def request(_self, *, idempotency_key, operator_token):
        calls.append(idempotency_key)
        assert operator_token == "operator-test-token"
        return SimpleNamespace(
            operation_status="REQUESTED" if len(calls) == 1 else "EXPIRED",
            handoff_id="a" * 32,
            source_job_id=22,
            consent_deadline_at=NOW + timedelta(seconds=60),
        )

    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "request_final_attestation_consent",
        request,
    )
    headers = {
        "X-Operator-Token": "operator-test-token",
        "Idempotency-Key": "final-consent-627",
        "X-Provider-Authorization": "once",
    }
    first = api.post(
        "/api/okx-demo/canary/consent-finalize", headers=headers, json={}
    )
    replay = api.post(
        "/api/okx-demo/canary/consent-finalize", headers=headers, json={}
    )
    terminal_replay = api.post(
        "/api/okx-demo/canary/consent-finalize", headers=headers, json={}
    )
    assert first.status_code == replay.status_code == terminal_replay.status_code == 202
    assert first.json()["operation_status"] == "REQUESTED"
    assert replay.json()["operation_status"] == "EXPIRED"
    assert terminal_replay.json() == replay.json()
    assert first.json()["handoff_id"] == replay.json()["handoff_id"]
    assert first.json()["source_job_id"] == 22
    assert first.json()["credential_values_recorded"] is False
    assert calls == ["final-consent-627", "final-consent-627"]


def test_refresh_terminal_block_remains_cached_for_same_key(client, monkeypatch):
    api, _calls = client
    calls = []

    def refresh(_self, *, idempotency_key):
        calls.append(idempotency_key)
        raise OkxDemoCanaryPreparationBlocked("fresh canary source is unsafe")

    monkeypatch.setattr(
        OkxDemoCanaryPreparationService,
        "prepare_fresh_execution_only_refresh",
        refresh,
    )
    headers = {
        "X-Operator-Token": "operator-test-token",
        "Idempotency-Key": "fresh-refresh-terminal-1",
        "X-Provider-Authorization": "once",
    }

    first = api.post(
        "/api/okx-demo/canary/refresh-execution-only",
        headers=headers,
        json={},
    )
    replay = api.post(
        "/api/okx-demo/canary/refresh-execution-only",
        headers=headers,
        json={},
    )

    assert first.status_code == 409
    assert replay.status_code == 409
    assert replay.json() == first.json()
    assert calls == ["fresh-refresh-terminal-1"]


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


def _successful_fresh_source(db_session, *, legacy_ids=(15, 16)):
    payload = {
        "provenance": CANARY_PROVENANCE,
        "execution_target": "OKX_DEMO",
        "instrument_id": "BTC-USDT-SWAP",
        "bundle_kind": "EXECUTION_ONLY",
        "non_production": True,
        "entry_kind": FRESH_EXECUTION_ONLY_ENTRY,
        "supersedes_job_ids": list(legacy_ids),
    }
    evidence = {
        "provenance": CANARY_PROVENANCE,
        "non_production": True,
        "entry_kind": FRESH_EXECUTION_ONLY_ENTRY,
        "supersedes_job_ids": list(legacy_ids),
        "snapshot_evidence": {
            "instrument": {"snapshot_id": "old-instrument", "digest": "1" * 64},
            "market": {"snapshot_id": "old-market", "digest": "2" * 64},
            "account": {"snapshot_id": "old-account", "digest": "3" * 64},
        },
    }
    source = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="okx_demo_controlled_canary",
        operation=CANARY_OPERATION,
        idempotency_key_digest=hashlib.sha256(b"fresh-source").hexdigest(),
        request_hash="a" * 64,
        request_payload=payload,
        status="SUCCESS",
        stage="CANARY_SNAPSHOTS_READY",
        attempt_count=1,
        max_attempts=1,
        evidence_snapshot=evidence,
        started_at=NOW,
        completed_at=NOW,
    )
    db_session.add(source)
    db_session.commit()
    return source


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


def test_refresh_re_attests_expired_fresh_source_and_finalizes_same_key(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    legacy = _blocked_attestation_job(
        db_session,
        key="refresh-legacy-15",
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
        key="refresh-terminal-16",
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
    source = _successful_fresh_source(
        db_session,
        legacy_ids=(legacy.id, terminal.id),
    )
    source_status = source.status
    source_stage = source.stage
    source_hash = source.request_hash
    source_payload = dict(source.request_payload)
    source_evidence = dict(source.evidence_snapshot)
    # Simulate the source snapshot TTL having elapsed by advancing the
    # operator clock; the database check constraint intentionally prevents
    # mutating a trusted row into an invalid expires_at value.
    snapshots = _seed_attested_snapshots(db_session)
    refresh_now = NOW + timedelta(minutes=2)
    current_now = [refresh_now]

    service = OkxDemoCanaryPreparationService(
        db_session, now_provider=lambda: current_now[0]
    )
    assert service._has_fresh_snapshot_rows(refresh_now) is False
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as waiting:
        service.prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-service-1"
        )
    refresh = db_session.get(ResearchJob, waiting.value.job_id)
    assert refresh is not None
    assert refresh.request_payload["entry_kind"] == FRESH_EXECUTION_ONLY_REFRESH
    assert refresh.request_payload["refresh_of_job_id"] == source.id
    assert refresh.request_payload["supersedes_job_ids"] == [
        legacy.id,
        terminal.id,
        source.id,
    ]
    assert "timeframe" not in refresh.request_payload
    assert "candle_limit" not in refresh.request_payload
    assert db_session.get(ResearchJob, source.id).status == source_status
    assert db_session.get(ResearchJob, source.id).stage == source_stage
    assert db_session.get(ResearchJob, source.id).request_hash == source_hash
    assert db_session.get(ResearchJob, source.id).request_payload == source_payload
    assert db_session.get(ResearchJob, source.id).evidence_snapshot == source_evidence

    class Reference:
        def __init__(self, database_id, snapshot_id, digest):
            self.database_id = database_id
            self.snapshot_id = snapshot_id
            self.digest = digest

    class Bundle:
        observed_at = NOW
        expires_at = NOW + timedelta(seconds=30)
        instrument = Reference(10, "refresh-instrument", "4" * 64)
        market = Reference(11, "refresh-market", "5" * 64)
        account = Reference(12, "refresh-account", "6" * 64)

    class RuntimeRead:
        def capture_execution_attestation(self, db, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return Bundle()

    assert process_pending_canary_attestation(
        read_client=RuntimeRead(), db=db_session, now=refresh_now
    ) is True
    db_session.commit()
    db_session.refresh(refresh)
    assert refresh.status == "SUCCESS"
    assert refresh.stage == "CANARY_SNAPSHOTS_READY"
    assert refresh.evidence_snapshot["entry_kind"] == FRESH_EXECUTION_ONLY_REFRESH
    assert refresh.evidence_snapshot["refresh_of_job_id"] == source.id
    assert refresh.evidence_snapshot["supersedes_job_ids"] == [
        legacy.id,
        terminal.id,
        source.id,
    ]
    assert "candle" not in str(refresh.evidence_snapshot).lower()

    _patch_lineage_dependencies(monkeypatch, snapshots)
    current_now[0] = NOW
    result = service.prepare_fresh_execution_only_refresh(
        idempotency_key="fresh-refresh-service-1"
    )
    assert result.operation_status == "PREPARED"
    assert result.entry_kind == FRESH_EXECUTION_ONLY_REFRESH
    assert result.refresh_of_job_id == source.id
    assert result.supersedes_job_ids == (legacy.id, terminal.id, source.id)
    db_session.refresh(source)
    assert source.status == source_status
    assert source.stage == source_stage
    assert source.request_hash == source_hash
    assert source.request_payload == source_payload
    assert source.evidence_snapshot == source_evidence

    monkeypatch.setattr(
        service,
        "_reconciliation_run_id_for_approval",
        lambda _approval_id: 1,
    )
    replay = service.prepare_fresh_execution_only_refresh(
        idempotency_key="fresh-refresh-service-1"
    )
    assert replay.research_job_id == result.research_job_id
    assert replay.trade_intent_id == result.trade_intent_id


def test_refresh_rejects_second_key_pending_and_unsafe_activity(db_session, monkeypatch):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    source = _successful_fresh_source(db_session)
    service = OkxDemoCanaryPreparationService(db_session, now_provider=lambda: NOW)
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as waiting:
        service.prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-pending-1"
        )
    pending = db_session.get(ResearchJob, waiting.value.job_id)
    assert pending is not None
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="successor already"):
        service.prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-pending-2"
        )
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as replay:
        service.prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-pending-1"
        )
    assert replay.value.job_id == pending.id

    # A fresh source plus any pre-existing execution lineage is unsafe even
    # before a successor is enqueued.
    db_session.delete(pending)
    db_session.commit()
    position = ExchangePosition(
        execution_target_id="OKX_DEMO",
        instrument_id="BTC-USDT-SWAP",
        position_side="long",
        quantity=Decimal("1"),
        average_price=Decimal("100"),
        snapshot={"source": "test"},
        observed_at=NOW,
    )
    db_session.add(position)
    db_session.commit()
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="position"):
        service.prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-unsafe-position"
        )
    db_session.delete(position)
    db_session.commit()
    db_session.add(
        TradeIntent(
            execution_target_id="OKX_DEMO",
            authorization_schema_version="RISK_V1",
            intent_id="f" * 64,
            canonical_hash="1" * 64,
            policy_digest="2" * 64,
            approved_payload_hash="3" * 64,
            idempotency_key_digest="4" * 64,
            client_order_id="REFRESHUNSAFE001",
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
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="TradeIntent"):
        service.prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-unsafe-1"
        )
    assert db_session.get(ResearchJob, source.id).status == "SUCCESS"


def test_refresh_runtime_lock_contention_does_not_create_or_terminally_fail(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    source = _successful_fresh_source(db_session)
    before_count = db_session.query(ResearchJob).filter_by(
        operation=CANARY_OPERATION
    ).count()
    monkeypatch.setattr(
        "app.services.okx_demo_canary_preparation.try_one_shot_transaction_lock",
        lambda _db: False,
    )

    with pytest.raises(OkxDemoCanaryPreparationRuntimeBusy) as creation_busy:
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-lock-creation"
        )
    assert creation_busy.value.job_id is None
    assert creation_busy.value.entry_kind == FRESH_EXECUTION_ONLY_REFRESH
    assert (
        db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).count()
        == before_count
    )
    assert db_session.get(ResearchJob, source.id).status == "SUCCESS"

    refresh = _successful_refresh_successor(
        db_session,
        source,
        key="fresh-refresh-lock-finalize",
        expires_at=NOW + timedelta(seconds=30),
    )
    before_count = db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).count()
    with pytest.raises(OkxDemoCanaryPreparationRuntimeBusy) as finalize_busy:
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-lock-finalize"
        )
    assert finalize_busy.value.job_id == refresh.id
    assert finalize_busy.value.entry_kind == FRESH_EXECUTION_ONLY_REFRESH
    assert finalize_busy.value.refresh_of_job_id == source.id
    assert finalize_busy.value.supersedes_job_ids == tuple(
        refresh.request_payload["supersedes_job_ids"]
    )
    assert (
        db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).count()
        == before_count
    )
    assert db_session.get(ResearchJob, refresh.id).status == "SUCCESS"


def _successful_refresh_successor(
    db_session,
    source: ResearchJob,
    *,
    entry_kind: str = FRESH_EXECUTION_ONLY_REFRESH,
    key: str = "successful-refresh",
    expires_at: datetime = NOW - timedelta(seconds=1),
):
    source_payload = dict(source.request_payload)
    supersedes = list(source_payload["supersedes_job_ids"]) + [source.id]
    payload = {
        "provenance": CANARY_PROVENANCE,
        "execution_target": "OKX_DEMO",
        "instrument_id": "BTC-USDT-SWAP",
        "bundle_kind": "EXECUTION_ONLY",
        "non_production": True,
        "entry_kind": entry_kind,
        "refresh_of_job_id": source.id,
        "supersedes_job_ids": supersedes,
    }
    evidence = {
        "provenance": CANARY_PROVENANCE,
        "non_production": True,
        "entry_kind": entry_kind,
        "refresh_of_job_id": source.id,
        "supersedes_job_ids": supersedes,
        "snapshot_evidence": {
            kind: {
                "snapshot_id": "stale-{}".format(kind),
                "digest": str(index) * 64,
                "expires_at": expires_at.isoformat(),
            }
            for index, kind in enumerate(("instrument", "market", "account"), start=1)
        },
    }
    job = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="okx_demo_controlled_canary",
        operation=CANARY_OPERATION,
        idempotency_key_digest=hashlib.sha256(key.encode()).hexdigest(),
        request_hash="b" * 64,
        request_payload=payload,
        status="SUCCESS",
        stage="CANARY_SNAPSHOTS_READY",
        attempt_count=1,
        max_attempts=1,
        evidence_snapshot=evidence,
        started_at=NOW,
        completed_at=NOW,
    )
    db_session.add(job)
    db_session.commit()
    return job


def _successful_recovery_successor(
    db_session,
    source: ResearchJob,
    *,
    key: str = "successful-recovery",
    expires_at: datetime = NOW - timedelta(seconds=1),
):
    source_payload = dict(source.request_payload)
    supersedes = list(source_payload["supersedes_job_ids"]) + [source.id]
    payload = {
        "provenance": CANARY_PROVENANCE,
        "execution_target": "OKX_DEMO",
        "instrument_id": "BTC-USDT-SWAP",
        "bundle_kind": "EXECUTION_ONLY",
        "non_production": True,
        "entry_kind": FRESH_EXECUTION_ONLY_RECOVERY,
        "recovery_of_job_id": source.id,
        "supersedes_job_ids": supersedes,
        "recovery_boundary": "PRE_616_FINALIZE_ACL_FAILURE",
    }
    evidence = {
        "provenance": CANARY_PROVENANCE,
        "non_production": True,
        "entry_kind": FRESH_EXECUTION_ONLY_RECOVERY,
        "recovery_of_job_id": source.id,
        "supersedes_job_ids": supersedes,
        "recovery_boundary": "PRE_616_FINALIZE_ACL_FAILURE",
        "snapshot_evidence": {
            kind: {
                "snapshot_id": "recovery-{}".format(kind),
                "digest": str(index) * 64,
                "expires_at": expires_at.isoformat(),
            }
            for index, kind in enumerate(("instrument", "market", "account"), start=1)
        },
    }
    job = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="okx_demo_controlled_canary",
        operation=CANARY_OPERATION,
        idempotency_key_digest=hashlib.sha256(key.encode()).hexdigest(),
        request_hash="c" * 64,
        request_payload=payload,
        status="SUCCESS",
        stage="CANARY_SNAPSHOTS_READY",
        attempt_count=1,
        max_attempts=1,
        evidence_snapshot=evidence,
        started_at=NOW,
        completed_at=NOW,
    )
    db_session.add(job)
    db_session.commit()
    return job


def _successful_post_persistence_successor(
    db_session,
    source: ResearchJob,
    *,
    key: str = "successful-post-persistence-recovery",
    expires_at: datetime = NOW - timedelta(seconds=1),
):
    source_payload = dict(source.request_payload)
    supersedes = list(source_payload["supersedes_job_ids"]) + [source.id]
    payload = {
        "provenance": CANARY_PROVENANCE,
        "execution_target": "OKX_DEMO",
        "instrument_id": "BTC-USDT-SWAP",
        "bundle_kind": "EXECUTION_ONLY",
        "non_production": True,
        "entry_kind": FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY,
        "recovery_of_job_id": source.id,
        "supersedes_job_ids": supersedes,
        "recovery_boundary": "POST_PERSISTENCE_LINEAGE_WRITE_FAILURE",
    }
    evidence = {
        "provenance": CANARY_PROVENANCE,
        "non_production": True,
        "entry_kind": FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY,
        "recovery_of_job_id": source.id,
        "supersedes_job_ids": supersedes,
        "recovery_boundary": "POST_PERSISTENCE_LINEAGE_WRITE_FAILURE",
        "snapshot_evidence": {
            kind: {
                "snapshot_id": "post-persistence-{}".format(kind),
                "digest": str(index) * 64,
                "expires_at": expires_at.isoformat(),
            }
            for index, kind in enumerate(("instrument", "market", "account"), start=1)
        },
    }
    job = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="okx_demo_controlled_canary",
        operation=CANARY_OPERATION,
        idempotency_key_digest=hashlib.sha256(key.encode()).hexdigest(),
        request_hash="d" * 64,
        request_payload=payload,
        status="SUCCESS",
        stage="CANARY_SNAPSHOTS_READY",
        attempt_count=1,
        max_attempts=1,
        evidence_snapshot=evidence,
        started_at=NOW,
        completed_at=NOW,
    )
    db_session.add(job)
    db_session.commit()
    return job


def _successful_post_persistence_lineage(
    db_session,
    *,
    post_expires_at: datetime = NOW - timedelta(seconds=1),
):
    legacy = _blocked_attestation_job(
        db_session,
        key="final-expiry-legacy",
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
        key="final-expiry-terminal",
        evidence={"provenance": CANARY_PROVENANCE},
    )
    source = _successful_fresh_source(
        db_session,
        legacy_ids=(legacy.id, terminal.id),
    )
    first = _successful_refresh_successor(db_session, source)
    second = _successful_refresh_successor(
        db_session,
        first,
        entry_kind=FRESH_EXECUTION_ONLY_REFRESH_RETRY,
        key="final-expiry-refresh-retry",
    )
    recovery = _successful_recovery_successor(db_session, second)
    post = _successful_post_persistence_successor(
        db_session,
        recovery,
        expires_at=post_expires_at,
    )
    return legacy, terminal, source, first, second, recovery, post


def test_refresh_allows_one_bounded_retry_from_stale_successor(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    source = _successful_fresh_source(db_session)
    stale_refresh = _successful_refresh_successor(db_session, source)

    with pytest.raises(OkxDemoCanaryPreparationWaiting) as waiting:
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-retry-1"
        )

    retry = db_session.get(ResearchJob, waiting.value.job_id)
    assert retry is not None
    assert retry.request_payload["entry_kind"] == FRESH_EXECUTION_ONLY_REFRESH_RETRY
    assert retry.request_payload["refresh_of_job_id"] == stale_refresh.id
    assert retry.request_payload["supersedes_job_ids"] == [
        *source.request_payload["supersedes_job_ids"],
        source.id,
        stale_refresh.id,
    ]
    assert db_session.get(ResearchJob, source.id).status == "SUCCESS"
    assert db_session.get(ResearchJob, stale_refresh.id).status == "SUCCESS"


def test_refresh_retry_cap_rejects_third_successor_and_missing_expiry(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    source = _successful_fresh_source(db_session)
    first = _successful_refresh_successor(db_session, source)
    second = _successful_refresh_successor(
        db_session,
        first,
        entry_kind=FRESH_EXECUTION_ONLY_REFRESH_RETRY,
        key="successful-refresh-retry",
    )
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="limit reached"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-retry-3"
        )
    assert db_session.get(ResearchJob, first.id).status == "SUCCESS"
    assert db_session.get(ResearchJob, second.id).status == "SUCCESS"

    # A successful refresh without explicit expiry evidence cannot be used as
    # a source for another attempt; missing evidence is not treated as stale.
    db_session.delete(second)
    db_session.commit()
    first.evidence_snapshot = {
        **first.evidence_snapshot,
        "snapshot_evidence": {
            kind: {"snapshot_id": "missing-expiry", "digest": "d" * 64}
            for kind in ("instrument", "market", "account")
        },
    }
    db_session.commit()
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="expiry evidence"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-retry-missing-expiry"
        )


def test_recovery_creates_one_successor_after_exhausted_refresh_lineage(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    legacy = _blocked_attestation_job(
        db_session,
        key="recovery-legacy-15",
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
        key="recovery-terminal-16",
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
    source = _successful_fresh_source(
        db_session,
        legacy_ids=(legacy.id, terminal.id),
    )
    first = _successful_refresh_successor(db_session, source)
    second = _successful_refresh_successor(
        db_session,
        first,
        entry_kind=FRESH_EXECUTION_ONLY_REFRESH_RETRY,
        key="recovery-refresh-retry-19",
    )
    source_snapshot = {
        "status": source.status,
        "stage": source.stage,
        "request_hash": source.request_hash,
        "request_payload": dict(source.request_payload),
        "evidence_snapshot": dict(source.evidence_snapshot),
    }

    service = OkxDemoCanaryPreparationService(db_session, now_provider=lambda: NOW)
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as waiting:
        service.prepare_fresh_execution_only_recovery(
            idempotency_key="recovery-successor-20"
        )
    recovery = db_session.get(ResearchJob, waiting.value.job_id)
    assert recovery is not None
    assert recovery.request_payload["entry_kind"] == FRESH_EXECUTION_ONLY_RECOVERY
    assert recovery.request_payload["recovery_of_job_id"] == second.id
    assert recovery.request_payload["supersedes_job_ids"] == [
        legacy.id,
        terminal.id,
        source.id,
        first.id,
        second.id,
    ]
    assert recovery.request_payload["recovery_boundary"] == (
        "PRE_616_FINALIZE_ACL_FAILURE"
    )
    assert {
        "status": source.status,
        "stage": source.stage,
        "request_hash": source.request_hash,
        "request_payload": dict(source.request_payload),
        "evidence_snapshot": dict(source.evidence_snapshot),
    } == source_snapshot

    # The bounded recovery entry does not reopen the ordinary refresh chain.
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="unknown or pending"):
        service.prepare_fresh_execution_only_refresh(
            idempotency_key="recovery-must-not-be-third-refresh"
        )
    assert db_session.query(ResearchJob).filter_by(
        operation=CANARY_OPERATION
    ).count() == 6

    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="recovery successor"):
        service.prepare_fresh_execution_only_recovery(
            idempotency_key="recovery-successor-21"
        )
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as replay:
        service.prepare_fresh_execution_only_recovery(
            idempotency_key="recovery-successor-20"
        )
    assert replay.value.job_id == recovery.id


def test_recovery_rejects_incomplete_or_nonexpired_lineage(db_session, monkeypatch):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    legacy = _blocked_attestation_job(
        db_session,
        key="recovery-incomplete-legacy",
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
        key="recovery-incomplete-terminal",
        evidence={"provenance": CANARY_PROVENANCE},
    )
    source = _successful_fresh_source(
        db_session,
        legacy_ids=(legacy.id, terminal.id),
    )
    first = _successful_refresh_successor(db_session, source)
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="two exhausted"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only_recovery(
            idempotency_key="recovery-incomplete-1"
        )

    # A depth-two lineage with fresh final evidence is not recoverable.  A
    # recovery entry cannot be used to extend a still-live handoff.
    _successful_refresh_successor(
        db_session,
        first,
        entry_kind=FRESH_EXECUTION_ONLY_REFRESH_RETRY,
        key="recovery-fresh-retry",
        expires_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="expiry evidence"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only_recovery(
            idempotency_key="recovery-fresh-2"
        )


def test_recovery_runtime_handoff_finalizes_same_key_without_mutating_history(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    legacy = _blocked_attestation_job(
        db_session,
        key="recovery-finalize-legacy",
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
        key="recovery-finalize-terminal",
        evidence={"provenance": CANARY_PROVENANCE},
    )
    source = _successful_fresh_source(
        db_session,
        legacy_ids=(legacy.id, terminal.id),
    )
    first = _successful_refresh_successor(db_session, source)
    second = _successful_refresh_successor(
        db_session,
        first,
        entry_kind=FRESH_EXECUTION_ONLY_REFRESH_RETRY,
        key="recovery-finalize-retry",
    )
    original = {
        job.id: (job.status, job.stage, job.request_hash, dict(job.request_payload))
        for job in (legacy, terminal, source, first, second)
    }
    service = OkxDemoCanaryPreparationService(db_session, now_provider=lambda: NOW)
    key = "recovery-finalize-20"
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as waiting:
        service.prepare_fresh_execution_only_recovery(idempotency_key=key)
    recovery = db_session.get(ResearchJob, waiting.value.job_id)
    assert recovery is not None

    class Reference:
        def __init__(self, database_id, snapshot_id, digest):
            self.database_id = database_id
            self.snapshot_id = snapshot_id
            self.digest = digest

    class Bundle:
        observed_at = NOW
        expires_at = NOW + timedelta(seconds=30)
        instrument = Reference(21, "recovery-instrument", "4" * 64)
        market = Reference(22, "recovery-market", "5" * 64)
        account = Reference(23, "recovery-account", "6" * 64)

    class RuntimeRead:
        def capture_execution_attestation(self, db, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return Bundle()

    assert process_pending_canary_attestation(
        read_client=RuntimeRead(), db=db_session, now=NOW
    ) is True
    db_session.commit()
    db_session.refresh(recovery)
    assert recovery.status == "SUCCESS"
    assert recovery.stage == "CANARY_SNAPSHOTS_READY"
    assert recovery.evidence_snapshot["entry_kind"] == FRESH_EXECUTION_ONLY_RECOVERY
    assert recovery.evidence_snapshot["recovery_of_job_id"] == second.id
    assert recovery.evidence_snapshot["recovery_boundary"] == (
        "PRE_616_FINALIZE_ACL_FAILURE"
    )

    snapshots = _seed_attested_snapshots(db_session)
    _patch_lineage_dependencies(monkeypatch, snapshots)
    result = service.prepare_fresh_execution_only_recovery(idempotency_key=key)
    assert result.operation_status == "PREPARED"
    assert result.entry_kind == FRESH_EXECUTION_ONLY_RECOVERY
    assert result.recovery_of_job_id == second.id
    assert result.supersedes_job_ids == (
        legacy.id,
        terminal.id,
        source.id,
        first.id,
        second.id,
    )
    for job in (legacy, terminal, source, first, second):
        current = db_session.get(ResearchJob, job.id)
        assert (
            current.status,
            current.stage,
            current.request_hash,
            dict(current.request_payload),
        ) == original[job.id]


def test_post_persistence_recovery_is_single_use_and_shape_driven(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    legacy = _blocked_attestation_job(
        db_session,
        key="post-persistence-legacy",
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
        key="post-persistence-terminal",
        evidence={"provenance": CANARY_PROVENANCE},
    )
    source = _successful_fresh_source(
        db_session,
        legacy_ids=(legacy.id, terminal.id),
    )
    first = _successful_refresh_successor(db_session, source)
    second = _successful_refresh_successor(
        db_session,
        first,
        entry_kind=FRESH_EXECUTION_ONLY_REFRESH_RETRY,
        key="post-persistence-refresh-retry",
    )
    recovery = _successful_recovery_successor(db_session, second)
    assert recovery.id != 20  # selection is by immutable shape, never a fixed id.

    service = OkxDemoCanaryPreparationService(db_session, now_provider=lambda: NOW)
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as waiting:
        service.prepare_post_persistence_recovery(
            idempotency_key="post-persistence-successor"
        )
    successor = db_session.get(ResearchJob, waiting.value.job_id)
    assert successor.request_payload["entry_kind"] == (
        FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY
    )
    assert successor.request_payload["recovery_of_job_id"] == recovery.id
    assert successor.request_payload["supersedes_job_ids"] == [
        legacy.id,
        terminal.id,
        source.id,
        first.id,
        second.id,
        recovery.id,
    ]
    assert successor.request_payload["recovery_boundary"] == (
        "POST_PERSISTENCE_LINEAGE_WRITE_FAILURE"
    )

    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="already exists"):
        service.prepare_post_persistence_recovery(
            idempotency_key="post-persistence-second-successor"
        )
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as replay:
        service.prepare_post_persistence_recovery(
            idempotency_key="post-persistence-successor"
        )
    assert replay.value.job_id == successor.id


def test_final_expiry_recovery_is_single_use_cumulative_and_idempotent(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    lineage = _successful_post_persistence_lineage(db_session)
    post = lineage[-1]
    service = OkxDemoCanaryPreparationService(db_session, now_provider=lambda: NOW)

    with pytest.raises(OkxDemoCanaryPreparationWaiting) as waiting:
        service.prepare_final_expiry_recovery(idempotency_key="final-expiry-successor")
    successor = db_session.get(ResearchJob, waiting.value.job_id)
    assert successor.request_payload == {
        "provenance": CANARY_PROVENANCE,
        "execution_target": "OKX_DEMO",
        "instrument_id": "BTC-USDT-SWAP",
        "bundle_kind": "EXECUTION_ONLY",
        "non_production": True,
        "entry_kind": FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY,
        "recovery_of_job_id": post.id,
        "supersedes_job_ids": [job.id for job in lineage],
        "recovery_boundary": "POST_PERSISTENCE_RECOVERY_SNAPSHOT_EXPIRY",
    }
    assert successor.max_attempts == 1

    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="already exists"):
        service.prepare_final_expiry_recovery(
            idempotency_key="final-expiry-second-successor"
        )
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as replay:
        service.prepare_final_expiry_recovery(idempotency_key="final-expiry-successor")
    assert replay.value.job_id == successor.id


def test_final_expiry_recovery_requires_explicitly_expired_source(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    _successful_post_persistence_lineage(
        db_session,
        post_expires_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="still fresh"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_final_expiry_recovery(idempotency_key="final-expiry-too-fresh")
    assert (
        db_session.query(ResearchJob)
        .filter(
            ResearchJob.operation == CANARY_OPERATION,
            ResearchJob.request_payload["entry_kind"].as_string()
            == FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY,
        )
        .count()
        == 0
    )


def test_final_expiry_recovery_requires_all_three_snapshots_expired(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    post = _successful_post_persistence_lineage(db_session)[-1]
    evidence = dict(post.evidence_snapshot)
    snapshots = {
        kind: dict(reference)
        for kind, reference in evidence["snapshot_evidence"].items()
    }
    snapshots["account"]["expires_at"] = (NOW + timedelta(seconds=1)).isoformat()
    evidence["snapshot_evidence"] = snapshots
    post.evidence_snapshot = evidence
    db_session.commit()

    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="still fresh"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_final_expiry_recovery(idempotency_key="final-expiry-partial")
    assert (
        db_session.query(ResearchJob)
        .filter_by(operation=CANARY_OPERATION)
        .count()
        == 7
    )


def test_final_expiry_recovery_rejects_pending_unknown_history_without_residue(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    lineage = _successful_post_persistence_lineage(db_session)
    pending = ResearchJob(
        execution_scope_id="LOCAL_DRY_RUN",
        job_type="okx_demo_controlled_canary",
        operation=CANARY_OPERATION,
        idempotency_key_digest="9" * 64,
        request_hash="8" * 64,
        request_payload={"provenance": CANARY_PROVENANCE},
        status="AWAITING_APPROVAL",
        stage="CANARY_SNAPSHOT_REQUESTED",
        attempt_count=0,
        max_attempts=1,
        evidence_snapshot={"provenance": CANARY_PROVENANCE},
        started_at=NOW,
    )
    db_session.add(pending)
    db_session.commit()
    before = db_session.query(ResearchJob).count()

    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="unknown or pending"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_final_expiry_recovery(idempotency_key="final-expiry-pending")
    assert db_session.query(ResearchJob).count() == before == len(lineage) + 1


def test_final_expiry_runtime_attestation_then_atomic_lineage_finalization(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    lineage = _successful_post_persistence_lineage(db_session)
    service = OkxDemoCanaryPreparationService(db_session, now_provider=lambda: NOW)
    key = "final-expiry-runtime"
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as waiting:
        service.prepare_final_expiry_recovery(idempotency_key=key)
    successor = db_session.get(ResearchJob, waiting.value.job_id)

    class Reference:
        def __init__(self, database_id, snapshot_id, digest):
            self.database_id = database_id
            self.snapshot_id = snapshot_id
            self.digest = digest

    class Bundle:
        observed_at = NOW
        expires_at = NOW + timedelta(seconds=30)
        instrument = Reference(1, "final-instrument", "a" * 64)
        market = Reference(2, "final-market", "b" * 64)
        account = Reference(3, "final-account", "c" * 64)

    class RuntimeRead:
        def capture_execution_attestation(self, db, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return Bundle()

    assert process_pending_canary_attestation(
        read_client=RuntimeRead(), db=db_session, now=NOW
    ) is True
    db_session.commit()
    db_session.refresh(successor)
    assert successor.status == "SUCCESS"
    assert successor.stage == "CANARY_SNAPSHOTS_READY"
    assert successor.evidence_snapshot["entry_kind"] == (
        FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY
    )
    assert successor.evidence_snapshot["recovery_of_job_id"] == lineage[-1].id
    assert successor.evidence_snapshot["recovery_boundary"] == (
        "POST_PERSISTENCE_RECOVERY_SNAPSHOT_EXPIRY"
    )

    snapshots = _seed_attested_snapshots(db_session)
    _patch_lineage_dependencies(monkeypatch, snapshots)
    result = service.prepare_final_expiry_recovery(idempotency_key=key)
    assert result.operation_status == "PREPARED"
    assert result.entry_kind == FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY
    assert result.recovery_of_job_id == lineage[-1].id
    assert result.supersedes_job_ids == tuple(job.id for job in lineage)


def test_post_persistence_recovery_rejects_existing_execution_activity(
    db_session, monkeypatch
):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    legacy = _blocked_attestation_job(
        db_session,
        key="post-activity-legacy",
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
        key="post-activity-terminal",
        evidence={"provenance": CANARY_PROVENANCE},
    )
    source = _successful_fresh_source(
        db_session,
        legacy_ids=(legacy.id, terminal.id),
    )
    first = _successful_refresh_successor(db_session, source)
    second = _successful_refresh_successor(
        db_session,
        first,
        entry_kind=FRESH_EXECUTION_ONLY_REFRESH_RETRY,
        key="post-activity-refresh-retry",
    )
    _successful_recovery_successor(db_session, second, key="post-activity-recovery")
    db_session.add(
        TradeIntent(
            execution_target_id="OKX_DEMO",
            authorization_schema_version="RISK_V1",
            intent_id="f" * 64,
            canonical_hash="e" * 64,
            policy_digest="d" * 64,
            idempotency_key_digest="c" * 64,
            client_order_id="FAICANARY" + "b" * 23,
            instrument_id="BTC-USDT-SWAP",
            side="buy",
            position_side="long",
            order_type="limit",
            quantity=Decimal("1"),
            limit_price=Decimal("10"),
            reference_price=Decimal("10"),
            leverage=Decimal("1"),
            margin_mode="isolated",
            reduce_only=False,
            status="APPROVED",
            request_snapshot={},
            expires_at=NOW + timedelta(seconds=10),
        )
    )
    db_session.commit()

    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="prior TradeIntent"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_post_persistence_recovery(
            idempotency_key="post-activity-successor"
        )


@pytest.mark.parametrize(
    ("blocked_index", "message"),
    (
        (0, "TradeIntent"),
        (1, "ApprovedExecution"),
        (2, "submission grant"),
        (3, "writer attempt"),
        (4, "exchange order"),
        (5, "exchange position"),
    ),
)
def test_post_persistence_shared_activity_gate_rejects_every_durable_boundary(
    blocked_index, message
):
    class ScalarResult:
        def __init__(self, value):
            self.value = value

        def first(self):
            return self.value

    class BoundarySession:
        def __init__(self):
            self.index = 0

        def scalars(self, _query):
            value = 1 if self.index == blocked_index else None
            self.index += 1
            return ScalarResult(value)

    service = OkxDemoCanaryPreparationService(BoundarySession())
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match=message):
        service._require_no_canary_activity_for_refresh()


def test_refresh_lineage_requires_depth_specific_entry_kind(db_session, monkeypatch):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    source = _successful_fresh_source(db_session)
    malformed = _successful_refresh_successor(
        db_session,
        source,
        entry_kind=FRESH_EXECUTION_ONLY_REFRESH_RETRY,
        key="malformed-refresh-kind",
    )
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="limit reached"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-kind-check"
        )
    assert db_session.get(ResearchJob, malformed.id).status == "SUCCESS"


def test_refresh_rejects_source_without_non_production_marker(db_session, monkeypatch):
    monkeypatch.setenv("FREQTRADE_AI_EXECUTION_TARGET", "OKX_DEMO")
    monkeypatch.setenv("FREQTRADE_AI_SIMULATED_TRADING", "true")
    monkeypatch.setenv("FREQTRADE_AI_ALLOW_REAL_FUNDS", "false")
    source = _successful_fresh_source(db_session)
    payload = dict(source.request_payload)
    payload["non_production"] = False
    source.request_payload = payload
    db_session.commit()
    service = OkxDemoCanaryPreparationService(db_session, now_provider=lambda: NOW)
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="source is not ready"):
        service.prepare_fresh_execution_only_refresh(
            idempotency_key="fresh-refresh-no-non-production"
        )
    assert db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).count() == 1


def test_fresh_entry_runtime_handoff_persists_only_execution_lineage(db_session):
    legacy = _blocked_attestation_job(
        db_session,
        key="legacy-runtime-entry",
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
        key="terminal-runtime-entry",
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
    service = OkxDemoCanaryPreparationService(db_session, now_provider=lambda: NOW)
    with pytest.raises(OkxDemoCanaryPreparationWaiting) as waiting:
        service.prepare_fresh_execution_only(idempotency_key="fresh-runtime-entry")
    job = db_session.get(ResearchJob, waiting.value.job_id)

    class Reference:
        def __init__(self, database_id, snapshot_id, digest):
            self.database_id = database_id
            self.snapshot_id = snapshot_id
            self.digest = digest

    class Bundle:
        observed_at = NOW
        expires_at = NOW + timedelta(seconds=30)
        instrument = Reference(1, "instrument-fresh", "c" * 64)
        market = Reference(2, "market-fresh", "d" * 64)
        account = Reference(3, "account-fresh", "e" * 64)

    class RuntimeRead:
        def capture_execution_attestation(self, db, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return Bundle()

    assert process_pending_canary_attestation(
        read_client=RuntimeRead(), db=db_session, now=NOW
    ) is True
    db_session.commit()
    db_session.refresh(job)
    assert job.status == "SUCCESS"
    assert job.stage == "CANARY_SNAPSHOTS_READY"
    assert job.evidence_snapshot["entry_kind"] == "FRESH_EXECUTION_ONLY"
    assert job.evidence_snapshot["supersedes_job_ids"] == [legacy.id, terminal.id]
    assert "candle" not in str(job.evidence_snapshot).lower()


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


def test_fresh_entry_requires_immutable_terminal_history(db_session):
    with pytest.raises(OkxDemoCanaryPreparationBlocked, match="immutable terminal"):
        OkxDemoCanaryPreparationService(
            db_session, now_provider=lambda: NOW
        ).prepare_fresh_execution_only(idempotency_key="fresh-without-history")
    assert db_session.query(ResearchJob).filter_by(operation=CANARY_OPERATION).count() == 0


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
