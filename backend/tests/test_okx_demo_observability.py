from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory, get_db
from app.main import app
from app.models import Base
from app.models.execution_lineage import (
    ExchangeFill,
    ExchangeOrder,
    ExchangePosition,
    RiskDecision,
    TradeIntent,
)
from app.models.okx_demo_reconciliation import OkxDemoOrderSnapshot
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.services.okx_demo_observability import (
    OkxDemoObservabilityService,
    order_completion,
)
from app.services.okx_demo_reconciliation import (
    OkxDemoReconciliationService,
    SCHEMA_VERSION,
)
from app.services.okx_demo_runtime_readiness import blocked_runtime_readiness
from app.services.okx_demo_runtime_readiness import OkxDemoRuntimeReadiness


def _client(tmp_path: Path) -> tuple[TestClient, object]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'okx-observability.sqlite'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), session_factory


def test_empty_database_is_explicitly_not_acceptable(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    try:
        response = client.get("/api/okx-demo/observability")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["target"]["target_id"] == "OKX_DEMO"
    assert payload["target"]["account_mode"] == "demo"
    assert payload["target"]["allow_real_funds"] is False
    assert payload["source_type"] == "api_aggregate"
    assert payload["core_data"] is True
    assert payload["orders"] == []
    assert payload["scope"]["truncated"] is False
    assert payload["scope"]["intent_total_count"] == 0
    assert payload["scope"]["order_total_count"] == 0
    assert payload["acceptance_state"] == "NOT_ACCEPTABLE"
    assert "空结果" in payload["acceptance_reason"]
    assert payload["account"]["status"] == "NOT_AVAILABLE"


def test_runtime_activity_empty_projection_is_demo_only_and_explicit(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    try:
        response = client.get("/api/okx-demo/runtime-activity?signal_limit=3")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "okx-demo-runtime-activity-v1"
    assert payload["execution_target"] == "OKX_DEMO"
    assert payload["allow_real_funds"] is False
    assert payload["real_orders"] is False
    assert payload["active_deployments"] == []
    assert payload["recent_signal_evaluations"] == []
    assert payload["signal_window"] == {
        "returned_count": 0,
        "limit": 3,
        "has_more": False,
    }


def test_allowlisted_projection_omits_raw_snapshots_and_requires_reconciliation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.okx_demo_observability.read_okx_demo_runtime_readiness",
        blocked_runtime_readiness,
    )
    client, session_factory = _client(tmp_path)
    now = datetime.now(timezone.utc)
    digests = {
        "intent_id": "a" * 64,
        "canonical_hash": "b" * 64,
        "policy_digest": "c" * 64,
        "approved_payload_hash": "d" * 64,
        "idempotency_key_digest": "e" * 64,
    }
    with session_factory() as session:
        ensure_execution_scope_catalog(session)
        intent = TradeIntent(
            execution_target_id="OKX_DEMO",
            authorization_schema_version="RISK_V1",
            client_order_id="okxDemo451",
            instrument_id="BTC-USDT-SWAP",
            side="buy",
            position_side="long",
            order_type="market",
            quantity=Decimal("1"),
            reference_price=Decimal("60000"),
            leverage=Decimal("2"),
            margin_mode="isolated",
            reduce_only=False,
            status="APPROVED",
            request_snapshot={
                "api_key": "must-not-leak",
                "response": {"passphrase": "must-not-leak"},
                "artifact_path": "/private/path",
            },
            expires_at=now + timedelta(minutes=2),
            **digests,
        )
        session.add(intent)
        session.flush()
        decision = RiskDecision(
            execution_target_id="OKX_DEMO",
            trade_intent_id=intent.id,
            authorization_schema_version="RISK_V1",
            policy_digest=digests["policy_digest"],
            decision="APPROVED",
            policy_version="risk-v1",
            evidence_snapshot={"secret": "must-not-leak"},
        )
        session.add(decision)
        order = ExchangeOrder(
            execution_target_id="OKX_DEMO",
            trade_intent_id=intent.id,
            client_order_id=intent.client_order_id,
            exchange_order_id="451234567890",
            status="live",
            request_snapshot={"api_secret": "must-not-leak"},
            response_snapshot={"raw": "must-not-leak"},
        )
        session.add(order)
        session.flush()
        session.add(
            ExchangeFill(
                execution_target_id="OKX_DEMO",
                exchange_order_row_id=order.id,
                exchange_fill_id="fill451",
                price=Decimal("60001"),
                quantity=Decimal("1"),
                fee=Decimal("-0.1"),
                snapshot={"token": "must-not-leak"},
            )
        )
        session.add(
            ExchangePosition(
                execution_target_id="OKX_DEMO",
                instrument_id="BTC-USDT-SWAP",
                position_side="long",
                quantity=Decimal("1"),
                average_price=Decimal("60001"),
                snapshot={"password": "must-not-leak"},
                observed_at=now,
            )
        )
        session.flush()
        service = OkxDemoReconciliationService(
            session,
            evidence_root=tmp_path / "managed" / "evidence",
            allowed_evidence_root=tmp_path / "managed",
        )
        events = [
            _event(
                "ORDER",
                "451234567890",
                {
                    "ordId": "451234567890",
                    "clOrdId": "okxDemo451",
                    "instId": "BTC-USDT-SWAP",
                    "state": "live",
                    "sz": "1",
                    "accFillSz": "1",
                    "avgPx": "60001",
                    "reduceOnly": False,
                },
                now,
            ),
            _event(
                "FILL",
                "fill451",
                {
                    "fillId": "fill451",
                    "ordId": "451234567890",
                    "instId": "BTC-USDT-SWAP",
                    "fillPx": "60001",
                    "fillSz": "1",
                    "fee": "-0.1",
                },
                now,
            ),
            _event(
                "POSITION",
                "BTC-USDT-SWAP:long",
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "pos": "1",
                    "avgPx": "60001",
                },
                now,
            ),
            _event(
                "ACCOUNT",
                "account",
                {
                    "accountFingerprint": "f" * 64,
                    "equity": "10000",
                    "availableBalance": "9000",
                    "marginBalance": "1000",
                },
                now,
            ),
        ]
        service.ingest_recovery_batch(
            events,
            recovery_batch_id="observability-451",
            high_watermarks={
                "ORDER": "order-end",
                "FILL": "fill-end",
                "POSITION": "position-end",
                "ACCOUNT": "account-end",
            },
            observed_at=now,
            completed_at=now + timedelta(seconds=1),
        )
        result = service.reconcile(now=now + timedelta(seconds=2))
        assert result.status == "RECONCILED"
        session.commit()

    try:
        response = client.get("/api/okx-demo/observability")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_type"] == "api_aggregate"
    assert payload["acceptance_state"] == "NOT_ACCEPTABLE"
    assert next(
        item for item in payload["readiness"] if item["key"] == "writer"
    )["status"] == "BLOCKED"
    assert payload["orders"][0]["completion_state"] == "INCOMPLETE"
    assert payload["orders"][0]["full_chain_database_id"] is None
    assert "完整持久链" in payload["orders"][0]["completion_reason"]
    assert payload["orders"][0]["exchange_order_id"] == "451234567890"
    assert payload["orders"][0]["authoritative_snapshot_database_id"] > 0
    assert payload["orders"][0]["authoritative_event_database_id"] > 0
    assert payload["orders"][0]["risk_decision"]["database_id"] > 0
    assert payload["latest_reconciliation"]["database_id"] > 0
    assert payload["latest_reconciliation"]["state_database_id"] > 0
    assert payload["latest_reconciliation"]["artifact_status"] == "READY"
    assert payload["account"]["status"] == "READY"
    assert payload["account"]["database_id"] > 0
    assert payload["account"]["equity"] == "10000.000000000000000000"
    serialized = response.text.lower()
    for forbidden in (
        "must-not-leak",
        "request_snapshot",
        "response_snapshot",
        "summary_snapshot",
        "raw_response",
        "artifact_path",
        "/private/path",
        "api_key",
        "api_secret",
        "passphrase",
        "password",
        "token",
    ):
        assert forbidden not in serialized

    with session_factory.begin() as session:
        OkxDemoReconciliationService(session).ingest_event(
            _event(
                "FILL",
                "fill452",
                {
                    "fillId": "fill452",
                    "ordId": "451234567890",
                    "instId": "BTC-USDT-SWAP",
                    "fillPx": "60002",
                    "fillSz": "0.1",
                    "fee": "-0.01",
                },
                now + timedelta(seconds=3),
                source_sequence=2,
            )
        )
    with session_factory() as session:
        after_unreconciled_event = OkxDemoObservabilityService(session).build()
    assert after_unreconciled_event.orders[0].completion_state == "INCOMPLETE"
    assert after_unreconciled_event.acceptance_state == "NOT_ACCEPTABLE"
    assert next(
        check
        for check in after_unreconciled_event.readiness
        if check.key == "reconciliation"
    ).status == "UNKNOWN"


def test_completion_predicate_fails_closed_for_each_missing_evidence() -> None:
    complete = {
        "database_id": 1,
        "exchange_order_id": "123",
        "intent_id": "a" * 64,
        "risk_decision": "APPROVED",
        "authoritative_snapshot_database_id": 3,
        "authoritative_event_database_id": 4,
        "authoritative_identity_matches": True,
        "reconciliation_database_id": 2,
        "reconciliation_state_database_id": 5,
        "reconciliation_status": "RECONCILED",
        "reconciliation_opening_frozen": False,
        "reconciliation_artifact_ready": True,
        "reconciliation_source_is_core": True,
        "reconciliation_covers_snapshot": True,
        "reconciliation_covers_event": True,
        "authoritative_fills_covered": True,
        "full_chain_database_id": 10,
        "full_chain_complete": True,
    }
    assert order_completion(**complete)[0] == "COMPLETE"

    for field in complete:
        candidate = dict(complete)
        candidate[field] = (
            True
            if field == "reconciliation_opening_frozen"
            else False
            if isinstance(candidate[field], bool)
            else None
        )
        state, reason = order_completion(**candidate)
        assert state == "INCOMPLETE"
        assert "缺少" in reason


def test_incremental_recovery_keeps_historical_snapshots_without_marking_current_projection_unknown(
    tmp_path: Path,
) -> None:
    """A no-change batch proves current state without copying old order snapshots."""

    _, session_factory = _client(tmp_path)
    now = datetime.now(timezone.utc)
    first_account = {
        "accountFingerprint": "a" * 64,
        "equity": "10000",
        "availableBalance": "9000",
        "marginBalance": "1000",
    }
    with session_factory.begin() as session:
        ensure_execution_scope_catalog(session)
        service = OkxDemoReconciliationService(
            session,
            evidence_root=tmp_path / "managed" / "evidence",
            allowed_evidence_root=tmp_path / "managed",
        )
        service.ingest_recovery_batch(
            [
                _event(
                    "ORDER",
                    "historic-order",
                    {
                        "ordId": "historic-order",
                        "instId": "BTC-USDT-SWAP",
                        "state": "filled",
                        "sz": "1",
                        "accFillSz": "1",
                        "avgPx": "60000",
                        "reduceOnly": False,
                    },
                    now,
                ),
                _event(
                    "FILL",
                    "historic-fill",
                    {
                        "fillId": "historic-fill",
                        "ordId": "historic-order",
                        "instId": "BTC-USDT-SWAP",
                        "fillPx": "60000",
                        "fillSz": "1",
                        "fee": "-0.1",
                    },
                    now,
                ),
                _event(
                    "POSITION",
                    "BTC-USDT-SWAP:long",
                    {
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "pos": "0",
                    },
                    now,
                ),
                _event("ACCOUNT", "account", first_account, now),
            ],
            recovery_batch_id="historical-baseline",
            high_watermarks={
                "ORDER": "first-order",
                "FILL": "first-fill",
                "POSITION": "first-position",
                "ACCOUNT": "first-account",
            },
            observed_at=now,
            completed_at=now + timedelta(seconds=1),
        )
        assert service.reconcile(now=now + timedelta(seconds=2)).status == "RECONCILED"

        # The next complete REST recovery has no order/fill/position changes.
        # It intentionally persists only its current account proof.
        second_observed_at = now + timedelta(seconds=10)
        service.ingest_recovery_batch(
            [
                _event(
                    "ACCOUNT",
                    "account",
                    first_account,
                    second_observed_at,
                    source_sequence=2,
                )
            ],
            recovery_batch_id="incremental-no-change",
            high_watermarks={
                "ORDER": "second-order",
                "FILL": "second-fill",
                "POSITION": "second-position",
                "ACCOUNT": "second-account",
            },
            observed_at=second_observed_at,
            completed_at=second_observed_at + timedelta(seconds=1),
        )
        result = service.reconcile(
            now=second_observed_at + timedelta(seconds=2),
            recovered=True,
        )
        assert result.status == "RECOVERED"
        assert result.database_ids["order_snapshots"] == []
        assert result.database_ids["fill_snapshots"] == []
        assert session.query(OkxDemoOrderSnapshot).count() == 1

    ready = OkxDemoRuntimeReadiness(
        status="READY",
        target_ready=True,
        credentials_ready=True,
        writer_ready=True,
        observed_at=now + timedelta(seconds=12),
    )
    with session_factory() as session:
        response = OkxDemoObservabilityService(
            session,
            runtime_readiness_provider=lambda: ready,
        ).build()

    assert response.latest_reconciliation is not None
    assert response.latest_reconciliation.status == "RECOVERED"
    assert response.orders == []
    assert response.positions == []
    assert response.account.status == "READY"
    assert {
        check.key: check.status for check in response.readiness
    }["market"] == "READY"
    assert {
        check.key: check.status for check in response.readiness
    }["reconciliation"] == "READY"


def _event(
    kind: str,
    entity_key: str,
    payload: dict,
    observed_at: datetime,
    *,
    source_sequence: int = 1,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_target": "OKX_DEMO",
        "source": "REST",
        "entity_kind": kind,
        "entity_key": entity_key,
        "source_sequence": source_sequence,
        "stream_generation": 1,
        "observed_at": observed_at.isoformat(),
        "received_at": (observed_at + timedelta(seconds=1)).isoformat(),
        "payload": payload,
    }
