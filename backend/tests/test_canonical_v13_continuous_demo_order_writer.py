from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.canonical_v13.continuous_demo_order_writer import (
    dispatch_continuous_demo_order,
    prepare_continuous_demo_order,
)
from app.canonical_v13.execution_common import canonical_execution_digest
from app.canonical_v13.models import (
    DEPLOYMENTS_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    FILLS_TABLE,
    LEDGER_ENTRIES_TABLE,
    ORDER_DISPATCH_RECEIPTS_TABLE,
    RISK_DECISIONS_TABLE,
    SIGNALS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.phase9_order_writer import _exchange_body
from app.canonical_v13.phase9_production_composition import (
    CanonicalFillWriterOperator,
    CanonicalLedgerWriterOperator,
)
from app.canonical_v13.risk_service import INTENT_MODE_CONTINUOUS_OPEN
from app.canonical_v13.risk_service import INTENT_MODE_POSITION_EXIT
from tests.test_canonical_v13_continuous_demo_execution import _exit_guard
from tests.test_canonical_v13_phase9_canary_policy import _fixture
from tests.test_canonical_v13_phase9_execution_authority import canonical_connection  # noqa: F401, F811
from tests.test_canonical_v13_phase9_order_writer import (
    FakeFillSession,
    FakeTransport,
    ORDER_BODY,
    _factory,
)
from tests.test_canonical_v13_research_evaluation import NOW


CONTINUOUS_OPEN_BODY = {
    key: value for key, value in ORDER_BODY.items() if key != "px"
}
CONTINUOUS_OPEN_BODY["ordType"] = "market"


class FakeContinuousOpenTransport(FakeTransport):
    def place(self, body):
        self.place_calls += 1
        assert dict(body) == CONTINUOUS_OPEN_BODY
        return self._payload()

    def preflight_place(self, body):
        assert dict(body) == CONTINUOUS_OPEN_BODY


CONTINUOUS_EXIT_BODY = {
    "instId": "BTC-USDT-SWAP",
    "tdMode": "isolated",
    "clOrdId": ORDER_BODY["clOrdId"],
    "side": "sell",
    "posSide": "long",
    "ordType": "market",
    "sz": "1",
}


class FakeContinuousExitTransport(FakeTransport):
    def place(self, body):
        self.place_calls += 1
        assert dict(body) == CONTINUOUS_EXIT_BODY
        return self._payload()

    def preflight_place(self, body):
        assert dict(body) == CONTINUOUS_EXIT_BODY

    def exit_guard(self, *, instrument, expected_contracts):
        assert instrument == "BTC-USDT-SWAP"
        assert expected_contracts == "1"
        return _exit_guard()


def _continuous_open_grant(connection):
    _decision, _approval, _probe, receipt = _fixture(connection)
    signal = connection.execute(select(SIGNALS_TABLE)).mappings().one()
    deployment = connection.execute(select(DEPLOYMENTS_TABLE)).mappings().one()
    probe = connection.execute(
        select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
            EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id == receipt.probe_receipt_id
        )
    ).mappings().one()
    attestation = connection.execute(
        select(EXECUTION_ATTESTATIONS_TABLE).where(
            EXECUTION_ATTESTATIONS_TABLE.c.id == probe["execution_attestation_id"]
        )
    ).mappings().one()
    intent_id = uuid4()
    intent_json = {
        "contract": "canonical-v13-continuous-demo-intent-v1",
        "action": "OPEN_LONG",
        "execution_target": "OKX_DEMO",
        "instrument": "BTC-USDT-SWAP",
        "exchange_body": CONTINUOUS_OPEN_BODY,
        "allow_real_funds": False,
        "intent_mode": INTENT_MODE_CONTINUOUS_OPEN,
    }
    intent_digest = canonical_execution_digest(intent_json)
    connection.execute(
        TRADE_INTENTS_TABLE.insert().values(
            id=intent_id,
            signal_id=signal["id"],
            intent_mode=INTENT_MODE_CONTINUOUS_OPEN,
            status="INTENT_ACCEPTED",
            intent_json=intent_json,
            intent_digest=intent_digest,
            created_at=NOW,
        )
    )
    decision_id = uuid4()
    grant = {
        "contract": "canonical-v13-continuous-execution-grant-v1",
        "action": "OPEN_LONG",
        "deployment_id": str(deployment["id"]),
        "deployment_capability_digest": deployment["capability_digest"],
        "execution_attestation_id": str(attestation["id"]),
        "probe_receipt_id": str(probe["id"]),
        "probe_receipt_digest": probe["receipt_digest"],
        "minimum_contract_size": "1",
        "limit_price": "10000",
        "strategy_max_leverage": "12",
        "effective_leverage": "12",
        "max_order_count": 1,
        "expires_at": (NOW + timedelta(seconds=45)).isoformat(),
        "trade_intent_id": str(intent_id),
        "intent_digest": intent_digest,
        "decision_mode": INTENT_MODE_CONTINUOUS_OPEN,
        "order_submission_enabled": True,
        "execution_authorized": True,
        "status": "RISK_ACCEPTED",
        "allow_real_funds": False,
    }
    connection.execute(
        RISK_DECISIONS_TABLE.insert().values(
            id=decision_id,
            trade_intent_id=intent_id,
            decision_mode=INTENT_MODE_CONTINUOUS_OPEN,
            status="RISK_ACCEPTED",
            decision_json=grant,
            decision_digest=canonical_execution_digest(grant),
            created_at=NOW,
        )
    )
    return decision_id, attestation["id"]


def _continuous_exit_grant(connection):
    _decision, _approval, _probe, receipt = _fixture(connection)
    deployment = connection.execute(select(DEPLOYMENTS_TABLE)).mappings().one()
    signal = connection.execute(select(SIGNALS_TABLE)).mappings().one()
    attestation = connection.execute(
        select(EXECUTION_ATTESTATIONS_TABLE).where(
            EXECUTION_ATTESTATIONS_TABLE.c.id
            == connection.execute(
                select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.execution_attestation_id).where(
                    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id == receipt.probe_receipt_id
                )
            ).scalar_one()
        )
    ).mappings().one()
    intent_id = uuid4()
    intent_json = {
        "contract": "canonical-v13-continuous-demo-intent-v1",
        "action": "CLOSE_LONG",
        "execution_target": "OKX_DEMO",
        "instrument": "BTC-USDT-SWAP",
        "exchange_body": CONTINUOUS_EXIT_BODY,
        "allow_real_funds": False,
        "intent_mode": INTENT_MODE_POSITION_EXIT,
    }
    intent_digest = canonical_execution_digest(intent_json)
    connection.execute(
        TRADE_INTENTS_TABLE.insert().values(
            id=intent_id,
            signal_id=signal["id"],
            intent_mode=INTENT_MODE_POSITION_EXIT,
            status="INTENT_ACCEPTED",
            intent_json=intent_json,
            intent_digest=intent_digest,
            created_at=NOW,
        )
    )
    decision_id = uuid4()
    grant = {
        "contract": "canonical-v13-continuous-execution-grant-v1",
        "action": "CLOSE_LONG",
        "deployment_id": str(deployment["id"]),
        "deployment_capability_digest": deployment["capability_digest"],
        "execution_attestation_id": str(attestation["id"]),
        "minimum_contract_size": "1",
        "maximum_close_contracts": "1",
        "reference_price": "10000.1",
        "strategy_max_leverage": "12",
        "effective_leverage": "12",
        "max_order_count": 1,
        "expires_at": (NOW + timedelta(seconds=15)).isoformat(),
        "trade_intent_id": str(intent_id),
        "intent_digest": intent_digest,
        "decision_mode": INTENT_MODE_POSITION_EXIT,
        "order_submission_enabled": True,
        "execution_authorized": True,
        "status": "RISK_ACCEPTED",
        "allow_real_funds": False,
    }
    connection.execute(
        RISK_DECISIONS_TABLE.insert().values(
            id=decision_id,
            trade_intent_id=intent_id,
            decision_mode=INTENT_MODE_POSITION_EXIT,
            status="RISK_ACCEPTED",
            decision_json=grant,
            decision_digest=canonical_execution_digest(grant),
            created_at=NOW,
        )
    )
    return decision_id, attestation["id"]


def test_close_body_is_allowlisted_without_limit_price():
    decision_id = uuid4()
    body = {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "isolated",
        "side": "sell",
        "posSide": "long",
        "ordType": "market",
        "sz": "0.01",
    }
    resolved = _exchange_body(body, risk_decision_id=decision_id)
    assert resolved == {
        **body,
        "clOrdId": f"v13{decision_id.hex[:29]}",
    }
    assert "px" not in resolved


def test_continuous_open_uses_independent_per_signal_grant(  # noqa: F811
    canonical_connection,  # noqa: F811
):
    with canonical_connection.begin():
        decision_id, attestation_id = _continuous_open_grant(canonical_connection)
        prepared = prepare_continuous_demo_order(
            canonical_connection,
            risk_decision_id=decision_id,
            attestation_id=attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="e" * 64,
            idempotency_key="continuous-natural-signal-fixture",
            evaluated_at=NOW + timedelta(seconds=2),
        )
    transport = FakeContinuousOpenTransport(
        guard_mutation={
            "account_fingerprint_digest": "d" * 64,
            "credential_generation_digest": "e" * 64,
        }
    )
    dispatched = dispatch_continuous_demo_order(
        _factory(canonical_connection.engine),
        order_id=prepared.order_id,
        transport=transport,
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="e" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=2),
    )
    replay = dispatch_continuous_demo_order(
        _factory(canonical_connection.engine),
        order_id=prepared.order_id,
        transport=transport,
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="e" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=3),
    )
    with canonical_connection.begin():
        claim = canonical_connection.execute(
            select(ORDER_DISPATCH_RECEIPTS_TABLE).where(
                ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == prepared.order_id
            )
        ).mappings().one()
    assert dispatched.status == "ACCEPTED"
    assert replay.order_id == dispatched.order_id
    assert replay.repeat_noop is True
    assert transport.place_calls == 1
    assert claim["dispatch_mode"] == INTENT_MODE_CONTINUOUS_OPEN
    assert claim["canary_risk_policy_id"] is None
    assert claim["probe_receipt_id"] is not None


def test_position_exit_dispatch_is_sell_long_market_and_replayable(  # noqa: F811
    canonical_connection,  # noqa: F811
):
    with canonical_connection.begin():
        decision_id, attestation_id = _continuous_exit_grant(canonical_connection)
        prepared = prepare_continuous_demo_order(
            canonical_connection,
            risk_decision_id=decision_id,
            attestation_id=attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="e" * 64,
            idempotency_key="continuous-position-exit-fixture",
            evaluated_at=NOW + timedelta(seconds=2),
        )
    transport = FakeContinuousExitTransport()
    dispatched = dispatch_continuous_demo_order(
        _factory(canonical_connection.engine),
        order_id=prepared.order_id,
        transport=transport,
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="e" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=2),
    )
    with canonical_connection.begin():
        claim = canonical_connection.execute(
            select(ORDER_DISPATCH_RECEIPTS_TABLE).where(
                ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == prepared.order_id
            )
        ).mappings().one()
    assert dispatched.status == "ACCEPTED"
    assert transport.place_calls == 1
    assert claim["dispatch_mode"] == INTENT_MODE_POSITION_EXIT
    assert claim["probe_receipt_id"] is None
    assert claim["maximum_close_contracts"] == 1
    assert claim["maximum_buy_contracts"] is None
    assert claim["limit_price"] is None


def test_position_exit_fill_posts_negative_contract_ledger(  # noqa: F811
    canonical_connection,  # noqa: F811
):
    with canonical_connection.begin():
        decision_id, attestation_id = _continuous_exit_grant(canonical_connection)
        prepared = prepare_continuous_demo_order(
            canonical_connection,
            risk_decision_id=decision_id,
            attestation_id=attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="e" * 64,
            idempotency_key="continuous-position-exit-ledger-fixture",
            evaluated_at=NOW + timedelta(seconds=2),
        )
    dispatch_continuous_demo_order(
        _factory(canonical_connection.engine),
        order_id=prepared.order_id,
        transport=FakeContinuousExitTransport(),
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="e" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=2),
    )
    fill_id = CanonicalFillWriterOperator(
        connection_factory=_factory(canonical_connection.engine),
        session_factory=lambda: FakeFillSession(),
    ).collect(order_id=prepared.order_id)[0]
    ledger_id = CanonicalLedgerWriterOperator(
        _factory(canonical_connection.engine)
    ).post(fill_id=fill_id)
    with canonical_connection.begin():
        fill = canonical_connection.execute(
            select(FILLS_TABLE).where(FILLS_TABLE.c.id == fill_id)
        ).mappings().one()
        ledger = canonical_connection.execute(
            select(LEDGER_ENTRIES_TABLE).where(
                LEDGER_ENTRIES_TABLE.c.id == ledger_id
            )
        ).mappings().one()
    assert fill["fill_json"]["side"] == "sell"
    assert fill["fill_json"]["position_side"] == "long"
    assert ledger["amount"] == -1
    assert ledger["entry_type"] == "OKX_DEMO_LONG_CLOSE_FILL_CONTRACTS"
