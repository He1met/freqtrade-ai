from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

from app.canonical_v13.accounting import post_production_demo_ledger_entry
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.execution_common import canonical_execution_digest
from app.canonical_v13.fill_service import record_production_demo_fill
from app.canonical_v13.models import (
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    FILLS_TABLE,
    ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
    ORDER_DISPATCH_RECEIPTS_TABLE,
    ORDERS_TABLE,
    RISK_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    SIGNALS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.deployment_approval import (
    CanonicalDeploymentApprovalBlocked,
    approve_demo_deployment,
    approve_demo_canary_recovery,
)
from app.canonical_v13.deployment_control import (
    create_demo_deployment,
    disable_demo_deployment,
)
from app.canonical_v13.phase9_canary_policy import terminate_canary_risk_policy
from app.canonical_v13.phase9_execution_authority import (
    authorize_demo_risk_budget,
    decide_central_demo_risk,
)
from app.canonical_v13.phase9_order_writer import (
    CanonicalOrderRecoveryRequired,
    _acquire_writer_lease,
    _exchange_body,
    _persist_exchange_receipt,
    dispatch_demo_order,
    prepare_demo_order,
    recover_demo_order_get_only,
    terminal_rejected_canary_order_evidence,
)
from app.canonical_v13.phase9_readiness import (
    _dispatch_claim_is_exact,
    inspect_phase9_readiness,
)
from app.canonical_v13.phase9_okx_demo import (
    RedactedOkxDemoDispatchGuard,
    RedactedOkxDemoOrderAbsence,
)
from app.canonical_v13.phase9_production_composition import (
    CanonicalFillWriterOperator,
    CanonicalLedgerWriterOperator,
    CanonicalPhase9CompositionBlocked,
    CanonicalReconciliationWriterOperator,
)
from app.canonical_v13.reconciliation import reconcile_production_demo_chain
from tests.test_canonical_v13_phase9_execution_authority import (
    _production_chain,
    _risk_policy_source,
    canonical_connection,  # noqa: F401 - registers the shared fixture
)
from tests.test_canonical_v13_research_evaluation import NOW


ORDER_BODY = {
    "instId": "BTC-USDT-SWAP",
    "tdMode": "isolated",
    "clOrdId": "v13canary00000000000000000001",
    "side": "buy",
    "posSide": "long",
    "ordType": "limit",
    "sz": "1",
    "px": "10000",
}


def test_missing_client_order_id_is_derived_from_exact_risk_identity():
    risk_decision_id = UUID("7fd4c87f-ac15-4ca4-977c-2be8d140ecca")
    persisted = {key: value for key, value in ORDER_BODY.items() if key != "clOrdId"}

    first = _exchange_body(persisted, risk_decision_id=risk_decision_id)
    replay = _exchange_body(persisted, risk_decision_id=risk_decision_id)

    assert first == replay
    assert first["clOrdId"] == "v137fd4c87fac154ca4977c2be8d140e"
    assert len(first["clOrdId"]) == 32


class FakeTransport:
    def __init__(
        self,
        *,
        place_error: Exception | None = None,
        guard_mutation: dict[str, object] | None = None,
    ) -> None:
        self.place_error = place_error
        self.guard_mutation = guard_mutation or {}
        self.guard_calls = 0
        self.last_guard = None
        self.place_calls = 0
        self.query_calls = 0
        self.absence_calls = 0

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

    def dispatch_guard(
        self, *, instrument, limit_price, effective_leverage, minimum_size
    ):
        self.guard_calls += 1
        observed = NOW + timedelta(seconds=2)
        expires = NOW + timedelta(seconds=20)
        facts = {
            "positions": {
                "instrument": instrument,
                "margin_mode": "isolated",
                "long_contracts": "0",
                "short_contracts": "0",
                "active_position_count": 0,
            },
            "pending_orders": {
                "instrument": instrument,
                "pending_order_count": 0,
            },
            "maximum_order_quantity": {
                "instrument": instrument,
                "margin_mode": "isolated",
                "limit_price": limit_price,
                "effective_leverage": effective_leverage,
                "maximum_buy_contracts": "2",
            },
            "leverage": {
                "instrument": instrument,
                "account_fingerprint_digest": "c" * 64,
                "long": effective_leverage,
                "short": "14",
            },
        }

        def digest(resource):
            return canonical_execution_digest(
                {
                    "execution_target": "OKX_DEMO",
                    "resource": resource,
                    "source": "okx_demo_rest",
                    "authenticated": True,
                    "observed_at": observed.isoformat(),
                    "expires_at": expires.isoformat(),
                    "facts": facts[resource],
                }
            )

        values = {
            "execution_target": "OKX_DEMO",
            "instrument": instrument,
            "account_fingerprint_digest": "c" * 64,
            "credential_generation_digest": "d" * 64,
            "limit_price": limit_price,
            "effective_leverage": effective_leverage,
            "current_short_leverage": "14",
            "minimum_size": minimum_size,
            "maximum_buy_contracts": "2",
            "long_contracts": "0",
            "short_contracts": "0",
            "active_position_count": 0,
            "pending_order_count": 0,
            "observed_at": observed,
            "expires_at": expires,
            "positions_digest": digest("positions"),
            "positions_observed_at": observed,
            "positions_expires_at": expires,
            "pending_orders_digest": digest("pending_orders"),
            "pending_orders_observed_at": observed,
            "pending_orders_expires_at": expires,
            "maximum_order_quantity_digest": digest("maximum_order_quantity"),
            "maximum_order_quantity_observed_at": observed,
            "maximum_order_quantity_expires_at": expires,
            "leverage_digest": digest("leverage"),
            "leverage_observed_at": observed,
            "leverage_expires_at": expires,
        }
        values.update(self.guard_mutation)
        self.last_guard = RedactedOkxDemoDispatchGuard(**values)
        return self.last_guard

    def query(self, *, instrument: str, client_order_id: str):
        self.query_calls += 1
        assert instrument == ORDER_BODY["instId"]
        assert client_order_id == ORDER_BODY["clOrdId"]
        return self._payload()

    def prove_absent(self, *, instrument: str, client_order_id: str):
        self.absence_calls += 1
        raise AssertionError("absence proof was not expected")


class AbsentTransport(FakeTransport):
    def query(self, *, instrument: str, client_order_id: str):
        self.query_calls += 1
        raise RuntimeError("exact order is absent")

    def prove_absent(self, *, instrument: str, client_order_id: str):
        self.absence_calls += 1
        observed = datetime.now(timezone.utc)
        expires = observed + timedelta(seconds=15)

        def digest(resource):
            return canonical_execution_digest(
                {
                    "execution_target": "OKX_DEMO",
                    "resource": resource,
                    "source": "okx_demo_rest",
                    "authenticated": True,
                    "observed_at": observed.isoformat(),
                    "expires_at": expires.isoformat(),
                    "facts": {
                        "instrument": instrument,
                        "client_order_id": client_order_id,
                        "matching_order_count": 0,
                    },
                }
            )

        return RedactedOkxDemoOrderAbsence(
            execution_target="OKX_DEMO",
            instrument=instrument,
            client_order_id=client_order_id,
            account_fingerprint_digest="c" * 64,
            credential_generation_digest="d" * 64,
            exact_order_result_code="51603",
            pending_order_match_count=0,
            history_order_match_count=0,
            pending_orders_digest=digest("pending_orders"),
            orders_history_digest=digest("orders_history"),
            observed_at=observed,
            expires_at=expires,
        )


class FakeFillSession:
    def __init__(self, fills=None) -> None:
        self.fill_calls = 0
        self._fills = fills

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def fills(self, *, instrument: str, order_id: str):
        self.fill_calls += 1
        assert instrument == "BTC-USDT-SWAP"
        assert order_id == "demo-exchange-order-1"
        return self._fills or (
            {
                "fill_id": "demo-fill-1",
                "bill_id": "demo-bill-1",
                "order_id": order_id,
                "inst_id": instrument,
                "price": "10000",
                "size": "1",
                "fee": "-0.01",
                "timestamp": "1787292000000",
            },
        )


def _prepare_authority(connection):
    approval, _deployment, _runtime, intent_id, _launcher = _production_chain(
        connection
    )
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
    attestation = SimpleNamespace(
        attestation_id=connection.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE.c.execution_attestation_id)
        ).scalar_one()
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
    claim = canonical_connection.execute(
        select(ORDER_DISPATCH_RECEIPTS_TABLE).where(
            ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == prepared.order_id
        )
    ).mappings().one()
    assert claim["attempt_ordinal"] == 1
    assert claim["request_digest"] == prepared.request_digest
    assert claim["holder_token_digest"] == "e" * 64
    assert claim["lease_generation"] == prepared.lease_generation
    assert claim["guard_digest"] == transport.last_guard.guard_digest
    assert canonical_execution_digest(claim["guard_json"]) == claim["guard_digest"]
    assert claim["credential_generation_digest"] == "d" * 64
    persisted_order = canonical_connection.execute(
        select(ORDERS_TABLE).where(ORDERS_TABLE.c.id == prepared.order_id)
    ).mappings().one()
    persisted_risk = canonical_connection.execute(
        select(RISK_DECISIONS_TABLE).where(
            RISK_DECISIONS_TABLE.c.id == persisted_order["risk_decision_id"]
        )
    ).mappings().one()
    persisted_policy = canonical_connection.execute(
        select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
            EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id
            == claim["canary_risk_policy_id"]
        )
    ).mappings().one()
    persisted_probe = canonical_connection.execute(
        select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
            EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id == claim["probe_receipt_id"]
        )
    ).mappings().one()
    persisted_attestation = canonical_connection.execute(
        select(EXECUTION_ATTESTATIONS_TABLE).where(
            EXECUTION_ATTESTATIONS_TABLE.c.id == claim["execution_attestation_id"]
        )
    ).mappings().one()
    persisted_lease = canonical_connection.execute(
        select(ORDER_WRITER_LEASES_TABLE)
    ).mappings().one()
    lease_acquired_at = claim["lease_acquired_at"].replace(tzinfo=NOW.tzinfo)
    lease_expires_at = claim["lease_expires_at"].replace(tzinfo=NOW.tzinfo)
    claimed_at = claim["claimed_at"].replace(tzinfo=NOW.tzinfo)
    assert lease_acquired_at <= claimed_at < lease_expires_at
    assert claim["lease_digest"] == persisted_lease["lease_digest"]
    assert claim["claim_digest"] == canonical_execution_digest(
        {
            "contract": "canonical-v13-okx-demo-order-dispatch-claim-v1",
            "order_id": str(prepared.order_id),
            "attempt_ordinal": 1,
            "request_digest": claim["request_digest"],
            "holder_identity": claim["holder_identity"],
            "holder_token_digest": claim["holder_token_digest"],
            "lease_generation": claim["lease_generation"],
            "lease_digest": claim["lease_digest"],
            "lease_acquired_at": lease_acquired_at.isoformat(),
            "lease_expires_at": lease_expires_at.isoformat(),
            "risk_decision_id": str(persisted_risk["id"]),
            "canary_risk_policy_id": str(persisted_policy["id"]),
            "probe_receipt_id": str(persisted_probe["id"]),
            "execution_attestation_id": str(persisted_attestation["id"]),
            "guard_digest": claim["guard_digest"],
            "claimed_at": claimed_at.isoformat(),
        }
    )
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
    assert transport.guard_calls == 1
    outcome = canonical_connection.execute(
        select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE).where(
            ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.order_id == prepared.order_id
        )
    ).mappings().one()
    assert outcome["outcome_mode"] == "POST"
    assert outcome["claim_digest"] == claim["claim_digest"]
    assert outcome["safe_response_json"] == {
        "ordId": "demo-exchange-order-1",
        "clOrdId": ORDER_BODY["clOrdId"],
        "sCode": "0",
    }
    assert outcome["safe_response_digest"] == canonical_execution_digest(
        outcome["safe_response_json"]
    )
    outcome_recorded_at = outcome["recorded_at"].replace(tzinfo=NOW.tzinfo)
    assert outcome["receipt_digest"] == canonical_execution_digest(
        {
            "contract": "canonical-v13-order-dispatch-outcome-v1",
            "outcome_id": str(outcome["id"]),
            "order_id": str(prepared.order_id),
            "dispatch_claim_id": str(claim["id"]),
            "claim_digest": claim["claim_digest"],
            "client_order_id": ORDER_BODY["clOrdId"],
            "exchange_order_id": "demo-exchange-order-1",
            "safe_response_digest": outcome["safe_response_digest"],
            "outcome_mode": "POST",
            "recorded_at": outcome_recorded_at.isoformat(),
        }
    )
    assert replay.receipt_digest == canonical_execution_digest(
        {
            "contract": "canonical-v13-okx-demo-order-receipt-v2",
            "order_id": str(prepared.order_id),
            "request_digest": prepared.request_digest,
            "dispatch_claim_digest": claim["claim_digest"],
            "dispatch_outcome_receipt_digest": outcome["receipt_digest"],
        }
    )
    with pytest.raises(
        CanonicalExecutionChainBlocked,
        match="BLOCKED_ORDER_DISPATCH_OUTCOME_DRIFT",
    ):
        _persist_exchange_receipt(
            canonical_connection,
            order_id=prepared.order_id,
            exchange_order_id="demo-exchange-order-1",
            safe_response=dict(outcome["safe_response_json"]),
            outcome_mode="GET_RECOVERY",
        )
    assert len(
        canonical_connection.execute(
            select(ORDER_DISPATCH_RECEIPTS_TABLE.c.id).where(
                ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == prepared.order_id
            )
        ).all()
    ) == 1


def test_replayed_submitted_order_renews_expired_writer_lease(canonical_connection):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        first = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="9" * 64,
            idempotency_key="phase9-expired-submitted-lease",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )
    with canonical_connection.begin():
        replay = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="9" * 64,
            idempotency_key="phase9-expired-submitted-lease",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=13),
        )
    assert replay.order_id == first.order_id
    assert replay.request_digest == first.request_digest
    assert replay.lease_generation == first.lease_generation
    assert replay.repeat_noop is True
    renewed = canonical_connection.execute(
        select(ORDER_WRITER_LEASES_TABLE)
    ).mappings().one()
    assert renewed["expires_at"].replace(tzinfo=NOW.tzinfo) == NOW + timedelta(
        seconds=23
    )

    transport = FakeTransport()
    dispatched = dispatch_demo_order(
        _factory(canonical_connection.engine),
        order_id=replay.order_id,
        transport=transport,
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="9" * 64,
        lease_generation=replay.lease_generation,
        evaluated_at=NOW + timedelta(seconds=14),
    )
    assert dispatched.repeat_noop is False
    assert transport.guard_calls == 1
    assert transport.place_calls == 1


def test_dispatch_uses_expired_lineage_with_fresh_private_guard(canonical_connection):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        canonical_connection.execute(
            EXECUTION_ATTESTATIONS_TABLE.update().values(
                expires_at=NOW + timedelta(seconds=2)
            )
        )
        canonical_connection.execute(
            EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.update().values(
                expires_at=NOW + timedelta(seconds=2)
            )
        )
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="1" * 64,
            idempotency_key="phase9-expired-lineage-fresh-guard",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=3),
        )
    transport = FakeTransport()
    dispatched = dispatch_demo_order(
        _factory(canonical_connection.engine),
        order_id=prepared.order_id,
        transport=transport,
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="1" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=4),
    )
    assert dispatched.repeat_noop is False
    assert transport.guard_calls == 1
    assert transport.place_calls == 1


def test_reserved_canary_dispatches_after_policy_window_with_fresh_guard(
    canonical_connection,
):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        canonical_connection.execute(
            EXECUTION_CANARY_RISK_POLICIES_TABLE.update().values(
                expires_at=NOW + timedelta(seconds=2)
            )
        )
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="2" * 64,
            idempotency_key="phase9-expired-policy-fresh-guard",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=3),
        )
    transport = FakeTransport()
    dispatched = dispatch_demo_order(
        _factory(canonical_connection.engine),
        order_id=prepared.order_id,
        transport=transport,
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="2" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=4),
    )
    assert dispatched.repeat_noop is False
    assert transport.guard_calls == 1
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
    assert transport.guard_calls == 1
    assert len(
        canonical_connection.execute(
            select(ORDER_DISPATCH_RECEIPTS_TABLE.c.id).where(
                ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == prepared.order_id
            )
        ).all()
    ) == 1
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
    recovered_outcome = canonical_connection.execute(
        select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE).where(
            ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.order_id == prepared.order_id
        )
    ).mappings().one()
    assert recovered_outcome["outcome_mode"] == "GET_RECOVERY"


def test_proven_absent_first_attempt_allows_one_same_order_retry(
    canonical_connection,
):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        canonical_connection.execute(
            EXECUTION_CANARY_RISK_POLICIES_TABLE.update().values(
                expires_at=NOW + timedelta(seconds=4)
            )
        )
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="3" * 64,
            idempotency_key="phase9-bounded-negative-retry",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )
    factory = _factory(canonical_connection.engine)
    first_transport = AbsentTransport(place_error=TimeoutError())
    with pytest.raises(CanonicalOrderRecoveryRequired):
        dispatch_demo_order(
            factory,
            order_id=prepared.order_id,
            transport=first_transport,
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="3" * 64,
            lease_generation=prepared.lease_generation,
            evaluated_at=NOW + timedelta(seconds=3),
        )
    recovered = recover_demo_order_get_only(
        factory, order_id=prepared.order_id, transport=first_transport
    )
    assert recovered.status == "RETRY_READY"
    assert recovered.retry_authorized is True
    assert recovered.exchange_order_id is None
    absence_replay = recover_demo_order_get_only(
        factory, order_id=prepared.order_id, transport=first_transport
    )
    assert absence_replay.repeat_noop is True
    assert absence_replay.receipt_digest == recovered.receipt_digest
    assert first_transport.query_calls == 1
    assert first_transport.absence_calls == 1
    with canonical_connection.begin():
        retried = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="3" * 64,
            idempotency_key="phase9-bounded-negative-retry",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=4),
        )
    second_transport = FakeTransport()
    accepted = dispatch_demo_order(
        factory,
        order_id=retried.order_id,
        transport=second_transport,
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="3" * 64,
        lease_generation=retried.lease_generation,
        evaluated_at=NOW + timedelta(seconds=5),
    )
    assert accepted.status == "ACCEPTED"
    assert accepted.exchange_order_id == "demo-exchange-order-1"
    claims = canonical_connection.execute(
        select(ORDER_DISPATCH_RECEIPTS_TABLE)
        .where(ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == prepared.order_id)
        .order_by(ORDER_DISPATCH_RECEIPTS_TABLE.c.attempt_ordinal)
    ).mappings().all()
    outcomes = canonical_connection.execute(
        select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE).where(
            ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.order_id == prepared.order_id
        )
    ).mappings().all()
    assert [row["attempt_ordinal"] for row in claims] == [1, 2]
    assert {row["outcome_mode"] for row in outcomes} == {"GET_NOT_FOUND", "POST"}
    persisted_order = canonical_connection.execute(
        select(ORDERS_TABLE).where(ORDERS_TABLE.c.id == prepared.order_id)
    ).mappings().one()
    persisted_risk = canonical_connection.execute(
        select(RISK_DECISIONS_TABLE).where(
            RISK_DECISIONS_TABLE.c.id == persisted_order["risk_decision_id"]
        )
    ).mappings().one()
    policy = canonical_connection.execute(
        select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
            EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id
            == claims[1]["canary_risk_policy_id"]
        )
    ).mappings().one()
    probe = canonical_connection.execute(
        select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
            EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id
            == claims[1]["probe_receipt_id"]
        )
    ).mappings().one()
    persisted_attestation = canonical_connection.execute(
        select(EXECUTION_ATTESTATIONS_TABLE).where(
            EXECUTION_ATTESTATIONS_TABLE.c.id
            == claims[1]["execution_attestation_id"]
        )
    ).mappings().one()
    lease = canonical_connection.execute(
        select(ORDER_WRITER_LEASES_TABLE)
    ).mappings().one()
    assert _dispatch_claim_is_exact(
        canonical_connection,
        claim=claims[0],
        order=persisted_order,
        risk=persisted_risk,
        policy=policy,
        probe=probe,
        attestation=persisted_attestation,
        lease=lease,
    )
    assert _dispatch_claim_is_exact(
        canonical_connection,
        claim=claims[1],
        order=persisted_order,
        risk=persisted_risk,
        policy=policy,
        probe=probe,
        attestation=persisted_attestation,
        lease=lease,
    )
    replay = dispatch_demo_order(
        factory,
        order_id=prepared.order_id,
        transport=second_transport,
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="3" * 64,
        lease_generation=retried.lease_generation,
        evaluated_at=NOW + timedelta(seconds=6),
    )
    assert replay.repeat_noop is True
    assert second_transport.place_calls == 1


def test_second_proven_absence_is_terminal_and_never_posts_third_time(
    canonical_connection,
):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="4" * 64,
            idempotency_key="phase9-bounded-negative-terminal",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )
    factory = _factory(canonical_connection.engine)
    first_transport = AbsentTransport(place_error=TimeoutError())
    with pytest.raises(CanonicalOrderRecoveryRequired):
        dispatch_demo_order(
            factory,
            order_id=prepared.order_id,
            transport=first_transport,
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="4" * 64,
            lease_generation=prepared.lease_generation,
            evaluated_at=NOW + timedelta(seconds=3),
        )
    recover_demo_order_get_only(
        factory, order_id=prepared.order_id, transport=first_transport
    )
    with canonical_connection.begin():
        retried = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="4" * 64,
            idempotency_key="phase9-bounded-negative-terminal",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=4),
        )
    second_transport = AbsentTransport(place_error=TimeoutError())
    with pytest.raises(CanonicalOrderRecoveryRequired):
        dispatch_demo_order(
            factory,
            order_id=prepared.order_id,
            transport=second_transport,
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="4" * 64,
            lease_generation=retried.lease_generation,
            evaluated_at=NOW + timedelta(seconds=5),
        )
    terminal = recover_demo_order_get_only(
        factory, order_id=prepared.order_id, transport=second_transport
    )
    assert terminal.status == "REJECTED"
    assert terminal.retry_authorized is False
    with pytest.raises(CanonicalOrderRecoveryRequired):
        dispatch_demo_order(
            factory,
            order_id=prepared.order_id,
            transport=second_transport,
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="4" * 64,
            lease_generation=retried.lease_generation,
            evaluated_at=NOW + timedelta(seconds=6),
        )
    assert first_transport.place_calls + second_transport.place_calls == 2

    with canonical_connection.begin():
        order = canonical_connection.execute(
            select(ORDERS_TABLE).where(ORDERS_TABLE.c.id == prepared.order_id)
        ).mappings().one()
        risk_row = canonical_connection.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
            )
        ).mappings().one()
        intent = canonical_connection.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.id == risk_row["trade_intent_id"]
            )
        ).mappings().one()
        signal = canonical_connection.execute(
            select(SIGNALS_TABLE).where(SIGNALS_TABLE.c.id == intent["signal_id"])
        ).mappings().one()
        deployment = canonical_connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == signal["deployment_id"]
            )
        ).mappings().one()
        approval = canonical_connection.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id
                == deployment["deployment_approval_id"]
            )
        ).mappings().one()
        evidence = terminal_rejected_canary_order_evidence(
            canonical_connection,
            order_id=prepared.order_id,
            deployment_id=deployment["id"],
        )
        assert evidence["status"] == "REJECTED"
        assert len(evidence["claim_digests"]) == 2
        assert len(evidence["outcome_receipt_digests"]) == 2
        canonical_connection.execute(
            RUNTIME_INSTANCES_TABLE.update()
            .where(RUNTIME_INSTANCES_TABLE.c.deployment_id == deployment["id"])
            .values(status="STOPPED")
        )
        canonical_connection.execute(
            ORDER_WRITER_LEASES_TABLE.update().values(status="RELEASED")
        )
        recovery = approve_demo_canary_recovery(
            canonical_connection,
            qualification_decision_id=approval["qualification_decision_id"],
            deployment_id=deployment["id"],
            order_id=prepared.order_id,
            actor_identity="operator:isolated-recovery",
            reason="one bounded zero-side-effect canary recovery",
            idempotency_key="isolated-terminal-canary-recovery-v1",
        )
        assert recovery.approval_generation == 2
        assert recovery.repeat_noop is False
        replay = approve_demo_canary_recovery(
            canonical_connection,
            qualification_decision_id=approval["qualification_decision_id"],
            deployment_id=deployment["id"],
            order_id=prepared.order_id,
            actor_identity="operator:isolated-recovery",
            reason="one bounded zero-side-effect canary recovery",
            idempotency_key="isolated-terminal-canary-recovery-v1",
        )
        assert replay.deployment_approval_id == recovery.deployment_approval_id
        assert replay.recovery_receipt_digest == recovery.recovery_receipt_digest
        assert replay.repeat_noop is True
        with pytest.raises(
            CanonicalDeploymentApprovalBlocked,
            match="BLOCKED_CANARY_RECOVERY_ALREADY_AUTHORIZED",
        ):
            approve_demo_canary_recovery(
                canonical_connection,
                qualification_decision_id=approval["qualification_decision_id"],
                deployment_id=deployment["id"],
                order_id=prepared.order_id,
                actor_identity="operator:isolated-recovery",
                reason="one bounded zero-side-effect canary recovery",
                idempotency_key="isolated-terminal-canary-recovery-v2",
            )
        disabled = disable_demo_deployment(
            canonical_connection,
            deployment_id=deployment["id"],
            superseded_by_qualification_decision_id=approval[
                "qualification_decision_id"
            ],
            actor_identity="operator:isolated-recovery",
            reason="replace only the terminal zero-side-effect canary lineage",
        )
        assert disabled.status == "DISABLED"
        successor = create_demo_deployment(
            canonical_connection,
            deployment_approval_id=recovery.deployment_approval_id,
        )
        assert successor.status == "PENDING"
        assert successor.deployment_id != deployment["id"]
        from tests.test_canonical_v13_phase9_readiness import (
            _handoff,
            _qualified,
            _seed_runtime_for_deployment,
            _seed_stage_b,
        )

        handoff = _handoff(
            canonical_connection,
            SimpleNamespace(
                qualification_decision_id=approval["qualification_decision_id"]
            ),
        )
        successor_runtime_id = _seed_runtime_for_deployment(
            canonical_connection,
            handoff,
            successor.deployment_id,
        )
        evaluated_at = datetime(2026, 8, 21, tzinfo=timezone.utc) + timedelta(
            seconds=20
        )
        no_order_soak = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="NO_ORDER_SOAK",
            evaluated_at=evaluated_at,
        )
        _seed_stage_b(
            canonical_connection,
            handoff,
            successor.deployment_id,
            successor_runtime_id,
        )
        shadow = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=handoff,
            stage="SIGNAL_RISK_SHADOW",
            evaluated_at=evaluated_at,
        )
        assert no_order_soak.status == "READY", no_order_soak.reason_codes
        assert no_order_soak.lineage_evidence_counts["orders"] == 0
        assert no_order_soak.execution_domain_counts["orders"] == 1
        assert shadow.status == "READY", shadow.reason_codes
        assert shadow.lineage_evidence_counts["orders"] == 0
        assert shadow.execution_domain_counts["orders"] == 1

        from hashlib import sha256

        from app.canonical_v13.models import (
            QUALIFICATION_DECISIONS_TABLE,
            STRATEGY_ARTIFACTS_TABLE,
            STRATEGY_VERSIONS_TABLE,
        )
        from app.canonical_v13.phase9_canary_policy import (
            authorize_canary_risk_policy,
            persist_canary_probe_receipt,
        )
        from app.canonical_v13.phase9_execution_authority import (
            record_redacted_demo_attestation,
        )
        from tests.test_canonical_v13_phase9_canary_policy import (
            STRATEGY_SOURCE,
            _sealed_probe,
        )

        decision = (
            canonical_connection.execute(
                select(QUALIFICATION_DECISIONS_TABLE).where(
                    QUALIFICATION_DECISIONS_TABLE.c.id
                    == approval["qualification_decision_id"]
                )
            )
            .mappings()
            .one()
        )
        version = (
            canonical_connection.execute(
                select(STRATEGY_VERSIONS_TABLE).where(
                    STRATEGY_VERSIONS_TABLE.c.id == decision["strategy_version_id"]
                )
            )
            .mappings()
            .one()
        )
        canonical_connection.execute(
            STRATEGY_ARTIFACTS_TABLE.update()
            .where(STRATEGY_ARTIFACTS_TABLE.c.id == version["artifact_id"])
            .values(
                normalized_content=STRATEGY_SOURCE,
                content_digest=sha256(STRATEGY_SOURCE.encode()).hexdigest(),
                size_bytes=len(STRATEGY_SOURCE.encode()),
            )
        )
        renewed_at = NOW + timedelta(minutes=31)
        fresh_probe = _sealed_probe(now=renewed_at)
        fresh_attestation = record_redacted_demo_attestation(
            canonical_connection,
            deployment_id=successor.deployment_id,
            instrument=fresh_probe.instrument,
            account_fingerprint_digest=fresh_probe.account_fingerprint_digest,
            credential_generation_digest=fresh_probe.credential_generation_digest,
            permissions=fresh_probe.permissions,
            observed_at=fresh_probe.observed_at,
            expires_at=fresh_probe.expires_at,
            evaluated_at=renewed_at,
        )
        fresh_receipt = persist_canary_probe_receipt(
            canonical_connection,
            probe=fresh_probe,
            deployment_id=successor.deployment_id,
            execution_attestation_id=fresh_attestation.attestation_id,
            evaluated_at=renewed_at,
        )
        renewed_policy = authorize_canary_risk_policy(
            canonical_connection,
            qualification_decision_id=decision["id"],
            deployment_approval_id=recovery.deployment_approval_id,
            probe_receipt_id=fresh_receipt.probe_receipt_id,
            actor_identity="operator:isolated-recovery",
            idempotency_key="isolated-terminal-canary-recovery-policy-v1",
            reason="renew only after exact generation-two recovery",
            evaluated_at=renewed_at,
        )
        policy_rows = (
            canonical_connection.execute(
                select(EXECUTION_CANARY_RISK_POLICIES_TABLE).order_by(
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.accepted_at
                )
            )
            .mappings()
            .all()
        )
        assert renewed_policy.repeat_noop is False
        assert [row["status"] for row in policy_rows] == ["EXPIRED", "ACTIVE"]
        assert (
            policy_rows[-1]["deployment_approval_id"]
            == recovery.deployment_approval_id
        )

        canonical_connection.execute(
            RUNTIME_INSTANCES_TABLE.update()
            .where(RUNTIME_INSTANCES_TABLE.c.id == successor_runtime_id)
            .values(status="STOPPED")
        )
        fresh_qualification = _qualified(canonical_connection)
        fresh_handoff = _handoff(canonical_connection, fresh_qualification)
        disable_demo_deployment(
            canonical_connection,
            deployment_id=successor.deployment_id,
            superseded_by_qualification_decision_id=(
                fresh_qualification.qualification_decision_id
            ),
            actor_identity="operator:isolated-requalification",
            reason="archive the exhausted qualification before a fresh generation one",
        )
        fresh_approval = approve_demo_deployment(
            canonical_connection,
            qualification_decision_id=(
                fresh_qualification.qualification_decision_id
            ),
            actor_identity="operator:isolated-requalification",
            reason="fresh immutable qualification starts at generation one",
        )
        fresh_deployment = create_demo_deployment(
            canonical_connection,
            deployment_approval_id=fresh_approval.deployment_approval_id,
        )
        _seed_runtime_for_deployment(
            canonical_connection,
            fresh_handoff,
            fresh_deployment.deployment_id,
        )
        fresh_no_order_soak = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=fresh_handoff,
            stage="NO_ORDER_SOAK",
            evaluated_at=evaluated_at,
        )
        assert fresh_no_order_soak.status == "READY", (
            fresh_no_order_soak.reason_codes
        )
        assert fresh_no_order_soak.lineage_evidence_counts["orders"] == 0
        assert fresh_no_order_soak.execution_domain_counts["orders"] == 1

        canonical_connection.execute(
            ORDERS_TABLE.update()
            .where(ORDERS_TABLE.c.id == prepared.order_id)
            .values(status="DISPATCHING")
        )
        invalid_archive = inspect_phase9_readiness(
            canonical_connection,
            qualification_handoff=fresh_handoff,
            stage="NO_ORDER_SOAK",
            evaluated_at=evaluated_at,
        )
        assert invalid_archive.status == "BLOCKED"
        assert "CANARY_ARCHIVED_HISTORY_SCOPE_INVALID" in (
            invalid_archive.reason_codes
        )
        assert "NONZERO_ORDERS=1" in invalid_archive.reason_codes


def test_explicit_post_rejection_is_terminal_and_audited(canonical_connection):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="5" * 64,
            idempotency_key="phase9-explicit-rejection",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )

    class RejectedTransport(FakeTransport):
        def place(self, body):
            self.place_calls += 1
            return {
                "code": "1",
                "msg": "redacted",
                "data": [
                    {
                        "ordId": "",
                        "clOrdId": ORDER_BODY["clOrdId"],
                        "sCode": "51000",
                        "sMsg": "redacted",
                    }
                ],
            }

    transport = RejectedTransport()
    result = dispatch_demo_order(
        _factory(canonical_connection.engine),
        order_id=prepared.order_id,
        transport=transport,
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="5" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=3),
    )
    assert result.status == "REJECTED"
    assert result.retry_authorized is False
    outcome = canonical_connection.execute(
        select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE)
    ).mappings().one()
    assert outcome["outcome_mode"] == "POST_REJECTED"
    assert outcome["safe_response_json"] == {
        "code": "1",
        "ordId": "",
        "clOrdId": ORDER_BODY["clOrdId"],
        "sCode": "51000",
    }


def test_explicit_top_level_post_rejection_is_terminal_and_redacted(
    canonical_connection,
):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="5" * 64,
            idempotency_key="phase9-explicit-top-level-rejection",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )

    class RejectedTransport(FakeTransport):
        def place(self, body):
            self.place_calls += 1
            return {
                "code": "50101",
                "msg": "raw exchange message must not be persisted",
                "data": [],
            }

    result = dispatch_demo_order(
        _factory(canonical_connection.engine),
        order_id=prepared.order_id,
        transport=RejectedTransport(),
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="5" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=3),
    )

    assert result.status == "REJECTED"
    assert result.retry_authorized is False
    outcome = canonical_connection.execute(
        select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE)
    ).mappings().one()
    assert outcome["outcome_mode"] == "POST_REJECTED"
    assert outcome["safe_response_json"] == {
        "code": "50101",
        "ordId": "",
        "clOrdId": ORDER_BODY["clOrdId"],
        "sCode": "",
    }


@pytest.mark.parametrize(
    "guard_mutation",
    (
        {"active_position_count": 1},
        {"pending_order_count": 1},
        {"maximum_buy_contracts": "0"},
        {"credential_generation_digest": "9" * 64},
        {"expires_at": NOW + timedelta(seconds=2)},
        {"leverage_digest": "9" * 64},
    ),
)
def test_dispatch_guard_tamper_or_toctou_blocks_before_post(
    canonical_connection, guard_mutation
):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="7" * 64,
            idempotency_key=f"phase9-guard-{next(iter(guard_mutation))}",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )
    transport = FakeTransport(guard_mutation=guard_mutation)
    with pytest.raises(CanonicalExecutionChainBlocked):
        dispatch_demo_order(
            _factory(canonical_connection.engine),
            order_id=prepared.order_id,
            transport=transport,
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="7" * 64,
            lease_generation=prepared.lease_generation,
            evaluated_at=NOW + timedelta(seconds=3),
        )
    assert transport.guard_calls == 1
    assert transport.place_calls == 0


@pytest.mark.parametrize(
    "lease_mutation",
    (
        {"lease_digest": "9" * 64},
        {"created_at": NOW + timedelta(seconds=4)},
        {"expires_at": NOW + timedelta(seconds=3)},
    ),
)
def test_dispatch_claim_rejects_drifted_or_nonfresh_lease(
    canonical_connection, lease_mutation
) -> None:
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="6" * 64,
            idempotency_key=f"phase9-lease-{next(iter(lease_mutation))}",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )
    with canonical_connection.begin():
        canonical_connection.execute(
            ORDER_WRITER_LEASES_TABLE.update().values(**lease_mutation)
        )
    transport = FakeTransport()
    with pytest.raises(
        CanonicalExecutionChainBlocked,
        match="BLOCKED_ORDER_WRITER_LEASE_FENCED",
    ):
        dispatch_demo_order(
            _factory(canonical_connection.engine),
            order_id=prepared.order_id,
            transport=transport,
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="6" * 64,
            lease_generation=prepared.lease_generation,
            evaluated_at=NOW + timedelta(seconds=3),
        )
    assert transport.place_calls == 0
    assert canonical_connection.execute(
        select(ORDER_DISPATCH_RECEIPTS_TABLE.c.id).where(
            ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == prepared.order_id
        )
    ).all() == []


def test_independent_get_fill_ledger_reconciliation_workers_replay_noop(
    canonical_connection,
):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="a" * 64,
            idempotency_key="phase9-order-production-workers",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )
    factory = _factory(canonical_connection.engine)
    dispatch_demo_order(
        factory,
        order_id=prepared.order_id,
        transport=FakeTransport(),
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="a" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=3),
    )
    session = FakeFillSession()
    fill_worker = CanonicalFillWriterOperator(
        connection_factory=factory, session_factory=lambda: session
    )
    first_fills = fill_worker.collect(order_id=prepared.order_id)
    replay_fills = fill_worker.collect(order_id=prepared.order_id)
    assert replay_fills == first_fills
    assert session.fill_calls == 2

    ledger_worker = CanonicalLedgerWriterOperator(factory)
    first_entry = ledger_worker.post(fill_id=first_fills[0])
    assert ledger_worker.post(fill_id=first_fills[0]) == first_entry

    reconciliation_worker = CanonicalReconciliationWriterOperator(factory)
    first_runs = reconciliation_worker.reconcile(order_id=prepared.order_id)
    assert reconciliation_worker.reconcile(order_id=prepared.order_id) == first_runs


def test_fill_worker_rejects_cumulative_overfill_before_any_insert(
    canonical_connection,
):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="b" * 64,
            idempotency_key="phase9-order-overfill",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )
    factory = _factory(canonical_connection.engine)
    dispatch_demo_order(
        factory,
        order_id=prepared.order_id,
        transport=FakeTransport(),
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="b" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=3),
    )
    rows = tuple(
        {
            "fill_id": f"demo-overfill-{index}",
            "bill_id": f"demo-bill-{index}",
            "order_id": "demo-exchange-order-1",
            "inst_id": "BTC-USDT-SWAP",
            "price": "10000",
            "size": "0.6",
            "fee": "-0.01",
            "timestamp": f"178729200000{index}",
        }
        for index in (1, 2)
    )
    worker = CanonicalFillWriterOperator(
        connection_factory=factory,
        session_factory=lambda: FakeFillSession(rows),
    )
    with pytest.raises(
        CanonicalPhase9CompositionBlocked, match="BLOCKED_PHASE9_FILL_SIZE"
    ):
        worker.collect(order_id=prepared.order_id)
    assert canonical_connection.execute(select(FILLS_TABLE.c.id)).all() == []


def test_future_attestation_and_competing_lease_fail_closed(canonical_connection):
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
                evaluated_at=NOW - timedelta(seconds=1),
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
                "side": "buy",
                "position_side": "long",
                "requested_size": "1",
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
                    "side": "buy",
                    "position_side": "long",
                    "requested_size": "1",
                    "size": "1",
                    "price": "10000",
                },
            )
            == fill_id
        )
        ledger_id = post_production_demo_ledger_entry(
            canonical_connection,
            fill_id=fill_id,
            entry_key="okx-demo-fill:demo-exchange-fill-1:long-contracts",
            asset="BTC-USDT-SWAP",
            amount=Decimal("1"),
            entry_type="OKX_DEMO_LONG_FILL_CONTRACTS",
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
        policy_id = canonical_connection.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id)
        ).scalar_one()
        terminated = terminate_canary_risk_policy(
            canonical_connection,
            policy_id=policy_id,
            reconciliation_run_id=run_id,
            actor_identity="isolated-policy-owner",
            evaluated_at=NOW + timedelta(seconds=4),
        )
        repeated = terminate_canary_risk_policy(
            canonical_connection,
            policy_id=policy_id,
            reconciliation_run_id=run_id,
            actor_identity="isolated-policy-owner",
            evaluated_at=NOW + timedelta(seconds=5),
        )
        assert terminated.repeat_noop is False
        assert repeated.repeat_noop is True
        assert repeated.termination_digest == terminated.termination_digest


def test_policy_termination_requires_every_partial_fill_chain(canonical_connection):
    with canonical_connection.begin():
        risk, attestation = _prepare_authority(canonical_connection)
        prepared = prepare_demo_order(
            canonical_connection,
            risk_decision_id=risk.risk_decision_id,
            attestation_id=attestation.attestation_id,
            writer_identity="canonical_order_writer",
            holder_identity="canonical-v13-order-writer-v1",
            holder_token_digest="5" * 64,
            idempotency_key="phase9-order-partial-termination",
            order_request=ORDER_BODY,
            evaluated_at=NOW + timedelta(seconds=2),
        )
    dispatched = dispatch_demo_order(
        _factory(canonical_connection.engine),
        order_id=prepared.order_id,
        transport=FakeTransport(),
        holder_identity="canonical-v13-order-writer-v1",
        holder_token_digest="5" * 64,
        lease_generation=prepared.lease_generation,
        evaluated_at=NOW + timedelta(seconds=3),
    )

    def persist_chain(fill_identity: str, size: str):
        fill_id = record_production_demo_fill(
            canonical_connection,
            order_id=prepared.order_id,
            exchange_fill_id=fill_identity,
            fill_json={
                "evidence_class": "PRODUCTION_OKX_DEMO",
                "allow_real_funds": False,
                "exchange_order_id": dispatched.exchange_order_id,
                "exchange_fill_id": fill_identity,
                "instrument": "BTC-USDT-SWAP",
                "side": "buy",
                "position_side": "long",
                "requested_size": "1",
                "size": size,
                "price": "10000",
            },
        )
        ledger_id = post_production_demo_ledger_entry(
            canonical_connection,
            fill_id=fill_id,
            entry_key=f"okx-demo-fill:{fill_identity}:long-contracts",
            asset="BTC-USDT-SWAP",
            amount=Decimal(size),
            entry_type="OKX_DEMO_LONG_FILL_CONTRACTS",
        )
        return reconcile_production_demo_chain(
            canonical_connection,
            order_id=prepared.order_id,
            fill_id=fill_id,
            ledger_entry_id=ledger_id,
        )

    with canonical_connection.begin():
        first_run = persist_chain("demo-exchange-fill-partial-1", "0.4")
        policy_id = canonical_connection.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id)
        ).scalar_one()
        with pytest.raises(
            CanonicalExecutionChainBlocked,
            match="BLOCKED_CANARY_POLICY_TERMINATION_LINEAGE",
        ):
            terminate_canary_risk_policy(
                canonical_connection,
                policy_id=policy_id,
                reconciliation_run_id=first_run,
                actor_identity="isolated-policy-owner",
                evaluated_at=NOW + timedelta(seconds=4),
            )
        persist_chain("demo-exchange-fill-partial-2", "0.6")
        terminated = terminate_canary_risk_policy(
            canonical_connection,
            policy_id=policy_id,
            reconciliation_run_id=first_run,
            actor_identity="isolated-policy-owner",
            evaluated_at=NOW + timedelta(seconds=5),
        )

    assert terminated.repeat_noop is False
