from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.canonical_v13.accounting import post_production_demo_ledger_entry
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.fill_service import record_production_demo_fill
from app.canonical_v13.models import ORDERS_TABLE, ORDER_WRITER_LEASES_TABLE
from app.canonical_v13.phase9_execution_authority import (
    authorize_demo_risk_budget,
    decide_central_demo_risk,
    record_redacted_demo_attestation,
)
from app.canonical_v13.phase9_order_writer import (
    CanonicalOrderRecoveryRequired,
    _acquire_writer_lease,
    dispatch_demo_order,
    prepare_demo_order,
    recover_demo_order_get_only,
)
from app.canonical_v13.reconciliation import reconcile_production_demo_chain
from tests.test_canonical_v13_phase9_execution_authority import (
    _production_chain,
    _risk_policy_source,
    canonical_connection,  # noqa: F401,F811 - registers the shared fixture
)
from tests.test_canonical_v13_research_evaluation import NOW


ORDER_BODY = {
    "instId": "BTC-USDT-SWAP",
    "tdMode": "isolated",
    "clOrdId": "v13canary00000000000000000001",
    "side": "buy",
    "posSide": "long",
    "ordType": "post_only",
    "sz": "1",
    "px": "10000",
}


class FakeTransport:
    def __init__(self, *, place_error: Exception | None = None) -> None:
        self.place_error = place_error
        self.place_calls = 0
        self.query_calls = 0

    @staticmethod
    def _payload():
        return {
            "code": "0",
            "msg": "",
            "data": [
                {
                    "ordId": "demo-exchange-order-1",
                    "clOrdId": ORDER_BODY["clOrdId"],
                    "sCode": "0",
                }
            ],
        }

    def place(self, body):
        self.place_calls += 1
        assert dict(body) == ORDER_BODY
        if self.place_error is not None:
            raise self.place_error
        return self._payload()

    def query(self, *, instrument: str, client_order_id: str):
        self.query_calls += 1
        assert instrument == ORDER_BODY["instId"]
        assert client_order_id == ORDER_BODY["clOrdId"]
        return self._payload()


def _prepare_authority(connection):
    approval, deployment, _runtime, intent_id, _launcher = _production_chain(connection)
    source_receipt = _risk_policy_source(connection, approval)
    budget = authorize_demo_risk_budget(
        connection,
        deployment_approval_id=approval.deployment_approval_id,
        actor_identity="isolated-human-owner",
        reason="formal fixture budget",
        policy_source_receipt_digest=source_receipt,
        evaluated_at=NOW,
    )
    risk = decide_central_demo_risk(
        connection,
        trade_intent_id=intent_id,
        risk_budget_authorization_id=budget.authorization_id,
        evaluated_at=NOW + timedelta(seconds=1),
    )
    attestation = record_redacted_demo_attestation(
        connection,
        deployment_id=deployment.deployment_id,
        instrument="BTC-USDT-SWAP",
        account_fingerprint_digest="c" * 64,
        credential_generation_digest="d" * 64,
        permissions={"read": True, "trade": True, "withdraw": False},
        observed_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
        evaluated_at=NOW,
    )
    return risk, attestation


def _factory(engine):
    @contextmanager
    def factory():
        with engine.begin() as connection:
            yield connection

    return factory


def test_prepare_then_single_post_and_exact_replay(canonical_connection):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="e" * 64,
            idempotency_key="phase9-order-fixture-1",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )
    transport = FakeTransport()
    factory = _factory(canonical_connection.engine)
    dispatched = dispatch_demo_order(
        factory,
        order_id=prepared.order_id,
        transport=transport,
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="e" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=3),
    )
    assert dispatched.repeat_noop is False
    assert transport.place_calls == 1
    replay = dispatch_demo_order(
        factory,
        order_id=prepared.order_id,
        transport=transport,
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="e" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=4),
    )
    assert replay.repeat_noop is True
    assert replay.receipt_digest == dispatched.receipt_digest
    assert transport.place_calls == 1


def test_uncertain_post_never_reposts_and_get_only_recovers(canonical_connection):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="f" * 64,
            idempotency_key="phase9-order-fixture-2",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )
    transport = FakeTransport(place_error=TimeoutError())
    factory = _factory(canonical_connection.engine)
    with pytest.raises(
        CanonicalOrderRecoveryRequired, match="BLOCKED_ORDER_RECOVERY_REQUIRED"
    ):
        dispatch_demo_order(
            factory,
            order_id=prepared.order_id,
            transport=transport,
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="f" * 64,
            lease_generation=prepared.lease_generation,
            evaluated_at=NOW + timedelta(seconds=3),
        )
    assert transport.place_calls == 1
    with pytest.raises(
        CanonicalOrderRecoveryRequired,
        match="BLOCKED_ORDER_GET_ONLY_RECOVERY_REQUIRED",
    ):
        dispatch_demo_order(
            factory,
            order_id=prepared.order_id,
            transport=transport,
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="f" * 64,
            lease_generation=prepared.lease_generation,
            evaluated_at=NOW + timedelta(seconds=4),
        )
    assert transport.place_calls == 1
    assert (
        recover_demo_order_get_only(
            factory, order_id=prepared.order_id, transport=transport
        ).exchange_order_id
        == "demo-exchange-order-1"
    )
    assert transport.place_calls == 1
    assert transport.query_calls == 1


def test_stale_attestation_and_competing_lease_fail_closed(canonical_connection):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_ORDER_ATTESTATION_STALE"
        ):
            prepare_demo_order(
                canonical_connection,
                risk_decision_id=risk.risk_decision_id,
                attestation_id=attestation.attestation_id,
                writer_identity="canonical_order_writer",
                holder_identity="canonical-v13-order-writer-v1",
                holder_token_digest="1" * 64,
                idempotency_key="phase9-order-fixture-3",
                order_request=ORDER_BODY,
                evaluated_at=NOW + timedelta(seconds=61),
            )
        assert (
            _acquire_writer_lease(
                canonical_connection,
                holder_identity="writer-a",
                holder_token_digest="2" * 64,
                now=NOW,
                lease_ttl=timedelta(seconds=10),
            )
            == 1
        )
        with pytest.raises(
            CanonicalExecutionChainBlocked, match="BLOCKED_ORDER_WRITER_LEASE_HELD"
        ):
            _acquire_writer_lease(
                canonical_connection,
                holder_identity="writer-b",
                holder_token_digest="3" * 64,
                now=NOW + timedelta(seconds=1),
                lease_ttl=timedelta(seconds=10),
            )
        lease = (
            canonical_connection.execute(select(ORDER_WRITER_LEASES_TABLE))
            .mappings()
            .one()
        )
        assert lease["holder_identity"] == "writer-a"
        assert (
            canonical_connection.execute(select(ORDERS_TABLE.c.id)).scalar_one_or_none()
            is None
        )


def test_exchange_fill_ledger_reconciliation_exact_replay(canonical_connection):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="4" * 64,
            idempotency_key="phase9-order-fixture-4",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )
    factory = _factory(canonical_connection.engine)
    dispatched = dispatch_demo_order(
        factory,
        order_id=prepared.order_id,
        transport=FakeTransport(),
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="4" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=3),
    )
    with canonical_connection.begin():
        fill_id = record_production_demo_fill(
            canonical_connection,
            order_id=prepared.order_id,
            exchange_fill_id="demo-exchange-fill-1",
            fill_json={
                "evidence_class": "PRODUCTION_OKX_DEMO",
                "allow_real_funds": False,
                "exchange_order_id": dispatched.exchange_order_id,
                "exchange_fill_id": "demo-exchange-fill-1",
                "instrument": "BTC-USDT-SWAP",
                "size": "1",
                "price": "10000",
            },
        )
        assert (
            record_production_demo_fill(
                canonical_connection,
                order_id=prepared.order_id,
                exchange_fill_id="demo-exchange-fill-1",
                fill_json={
                    "evidence_class": "PRODUCTION_OKX_DEMO",
                    "allow_real_funds": False,
                    "exchange_order_id": dispatched.exchange_order_id,
                    "exchange_fill_id": "demo-exchange-fill-1",
                    "instrument": "BTC-USDT-SWAP",
                    "size": "1",
                    "price": "10000",
                },
            )
            == fill_id
        )
        ledger_id = post_production_demo_ledger_entry(
            canonical_connection,
            fill_id=fill_id,
            entry_key="demo-exchange-fill-1:BTC",
            asset="BTC",
            amount=Decimal("0.001"),
            entry_type="DEMO_POSITION_FILL",
        )
        run_id = reconcile_production_demo_chain(
            canonical_connection,
            order_id=prepared.order_id,
            fill_id=fill_id,
            ledger_entry_id=ledger_id,
        )
        assert (
            reconcile_production_demo_chain(
                canonical_connection,
                order_id=prepared.order_id,
                fill_id=fill_id,
                ledger_entry_id=ledger_id,
            )
            == run_id
        )
