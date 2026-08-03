from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time

import pytest

from app.adapters.okx_demo.models import OkxReadSnapshot, SnapshotMetadata
from app.adapters.okx_demo.order_writer import (
    ManagedOrder,
    OkxDemoOrderWriter,
    WriteAttemptRecord,
)
from app.adapters.okx_demo.write_semantics import (
    OkxDemoTransportError,
    OkxDemoWriteBlocked,
)
from app.adapters.okx_demo.writer_models import (
    ApprovedExecutionView,
    OrderSubmissionAuthorization,
)
from app.adapters.okx_demo.writer_state import (
    WriteEvent,
    WriteState,
    transition_write_state,
)


NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def snapshot(resource, items):
    return OkxReadSnapshot(
        metadata=SnapshotMetadata(
            resource=resource,
            fetched_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
            stale=False,
            authenticated=resource == "order",
        ),
        items=items,
    )


def instrument_item():
    return {
        "inst_id": "BTC-USDT-SWAP",
        "inst_type": "SWAP",
        "base_ccy": "BTC",
        "quote_ccy": "USDT",
        "settle_ccy": "USDT",
        "contract_type": "linear",
        "contract_value": "0.01",
        "contract_value_ccy": "BTC",
        "lot_size": "0.01",
        "min_size": "0.01",
        "tick_size": "0.1",
        "state": "live",
        "listed_at": None,
    }


def order_item(**overrides):
    value = {
        "grant_id": "1" * 32,
        "inst_id": "BTC-USDT-SWAP",
        "order_id": "exchange-order-1",
        "client_order_id": "WriterOrder001",
        "state": "live",
        "side": "buy",
        "position_side": "long",
        "margin_mode": "isolated",
        "order_type": "limit",
        "reduce_only": False,
        "price": Decimal("57000.1"),
        "size": Decimal("0.02"),
    }
    value.update(overrides)
    return value


def approved(**overrides):
    value = {
        "approval_id": 3,
        "trade_intent_id": 7,
        "risk_decision_id": 9,
        "execution_target_id": "OKX_DEMO",
        "authorization_schema_version": "RISK_V1",
        "canonical_hash": "b" * 64,
        "policy_digest": "c" * 64,
        "approved_payload_hash": "d" * 64,
        "client_order_id": "WriterOrder001",
        "instrument_id": "BTC-USDT-SWAP",
        "side": "buy",
            "position_side": "long",
        "order_type": "limit",
        "contracts": Decimal("0.02"),
        "limit_price": Decimal("57000.1"),
        "reduce_only": False,
        "margin_mode": "isolated",
        "leverage": Decimal("3"),
        "approved_at": NOW - timedelta(seconds=10),
        "expires_at": NOW + timedelta(seconds=50),
        "policy_version": "risk-v1",
        "idempotency_digest": "a" * 64,
        "take_profit_trigger_price": None,
        "take_profit_order_price": None,
        "stop_loss_trigger_price": None,
        "stop_loss_order_price": None,
    }
    value.update(overrides)
    return ApprovedExecutionView(**value)


def submission_grant(**overrides):
    value = {
        "grant_id": "1" * 32,
        "execution_target_id": "OKX_DEMO",
        "authorization_schema_version": "RISK_V1",
        "canonical_hash": "b" * 64,
        "policy_digest": "c" * 64,
        "approved_payload_hash": "d" * 64,
        "allow_real_funds": False,
        "simulated_trading": True,
        "order_submission_enabled": True,
        "writer_instance_id": "WriterInstance01",
        "approval_id": 3,
        "client_order_id": "WriterOrder001",
        "issued_at": NOW - timedelta(seconds=1),
        "expires_at": NOW + timedelta(seconds=10),
    }
    value.update(overrides)
    return OrderSubmissionAuthorization(**value)


class FakeReadClient:
    def __init__(self, orders=(), positions=(), leverages=None):
        self.orders = list(orders)
        self.position_responses = list(positions)
        self.leverage_responses = (
            [
                [
                    {
                        "inst_id": "BTC-USDT-SWAP",
                        "margin_mode": "isolated",
            "position_side": "long",
                        "leverage": Decimal("3"),
                    }
                ]
            ]
            if leverages is None
            else list(leverages)
        )
        self.calls = []

    def instruments(self, inst_id=None):
        self.calls.append(("instruments", inst_id))
        return snapshot("instruments", [instrument_item()])

    def order(self, inst_id, *, order_id=None, client_order_id=None):
        self.calls.append(("order", inst_id, order_id, client_order_id))
        response = self.orders.pop(0)
        if isinstance(response, BaseException):
            raise response
        return snapshot("order", response)

    def positions(self, inst_id=None):
        self.calls.append(("positions", inst_id))
        response = self.position_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return snapshot("positions", response)

    def leverage(self, inst_id):
        self.calls.append(("leverage", inst_id))
        response = self.leverage_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return snapshot("leverage", response)


class FakeWriteTransport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def post(self, *, path, body, timeout_seconds=10.0):
        self.calls.append((path, dict(body)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeStore:
    def __init__(self, unresolved=None, unresolved_order=None):
        self.current = unresolved
        self.unresolved_order = unresolved_order
        self.events = []
        self.next_attempt_id = 1
        self.next_order_id = 101
        self.lease_calls = []
        self.atomic_remaining_ms = 900
        self.atomic_order_delay_seconds = 0.0

    @property
    def atomic_process_token_digest(self):
        return "a" * 64

    @property
    def atomic_process_token(self):
        return "b" * 64

    def validate_atomic_dispatch_authority(
        self, *, attempt_id, runtime_instance_id, lease_generation,
        request_digest, bundle_digest,
    ):
        self.lease_calls.append(
            ("atomic", attempt_id, runtime_instance_id, lease_generation,
             request_digest, bundle_digest)
        )
        return self.atomic_remaining_ms

    def load_atomic_attempt(self, attempt_id):
        assert self.current is not None and self.current.attempt_id == attempt_id
        return self.current

    def acquire_lease(
        self,
        *,
        grant_id,
        authorization_mode,
        writer_instance_id,
        approval_id,
        canonical_hash,
        policy_digest,
        approved_payload_hash,
        now,
        expires_at,
    ):
        self.lease_calls.append((writer_instance_id, now, expires_at))

    def acquire_recovery_lease(self, grant_database_id, *, now):
        self.lease_calls.append(("recovery", grant_database_id, now))

    def claim_unresolved_for_reconciliation(
        self,
        attempt_id,
        *,
        now,
        expires_at,
    ):
        assert self.current is not None
        assert self.current.attempt_id == attempt_id
        self.lease_calls.append(("reconcile", attempt_id, now, expires_at))
        return self.current

    def load_recovery_order(self, grant_database_id):
        assert grant_database_id > 0
        return self.unresolved_order or managed_order()

    def unresolved(self):
        if self.current is None:
            return None
        if self.current.state in {
            WriteState.PREPARED,
            WriteState.DISPATCHED,
            WriteState.ACKNOWLEDGED,
            WriteState.RECOVERY_REQUIRED,
            WriteState.RESIDUAL_CLOSE_REQUIRED,
        }:
            return self.current
        return None

    def prepare_place(
        self,
        command,
        *,
        operation,
        operation_id,
        request_digest,
        safe_request_snapshot,
    ):
        order = ManagedOrder(
            exchange_order_row_id=self.next_order_id,
            trade_intent_id=command.trade_intent_id,
            approval_id=command.approval_id,
            canonical_hash=command.canonical_hash,
            policy_digest=command.policy_digest,
            approved_payload_hash=command.approved_payload_hash,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            exchange_order_id=None,
            side=command.side,
            position_side=command.position_side,
            order_type=command.order_type,
            contracts=command.contracts,
            limit_price=command.limit_price,
            reduce_only=command.reduce_only,
        )
        return order, self._prepare(
            order,
            operation,
            operation_id,
            request_digest,
            safe_request_snapshot,
        )

    def prepare_existing(
        self,
        order,
        *,
        operation,
        operation_id,
        request_digest,
        safe_request_snapshot,
        recovery_grant_database_id=None,
    ):
        attempt = self._prepare(
            order,
            operation,
            operation_id,
            request_digest,
            safe_request_snapshot,
        )
        if recovery_grant_database_id is not None:
            attempt = WriteAttemptRecord(
                **{
                    **attempt.__dict__,
                    "recovery_grant_database_id":
                        recovery_grant_database_id,
                }
            )
            self.current = attempt
        return attempt

    def prepare_close_cleanup(
        self,
        parent,
        command,
        *,
        operation_id,
        request_digest,
        safe_request_snapshot,
    ):
        parent_reconciled = WriteAttemptRecord(
            **{
                **parent.__dict__,
                "state": WriteState.RECONCILED,
            }
        )
        self.current = parent_reconciled
        order = ManagedOrder(
            exchange_order_row_id=self.next_order_id + 1,
            trade_intent_id=command.trade_intent_id,
            approval_id=command.approval_id,
            canonical_hash=command.canonical_hash,
            policy_digest=command.policy_digest,
            approved_payload_hash=command.approved_payload_hash,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            exchange_order_id=None,
            side=command.side,
            position_side=command.position_side,
            order_type=command.order_type,
            contracts=command.contracts,
            limit_price=command.limit_price,
            reduce_only=command.reduce_only,
        )
        attempt = self._prepare(
            order,
            "CLOSE",
            operation_id,
            request_digest,
            safe_request_snapshot,
        )
        attempt = WriteAttemptRecord(
            **{
                **attempt.__dict__,
                "parent_attempt_id": parent.attempt_id,
                "close_sequence": parent.close_sequence + 1,
            }
        )
        self.current = attempt
        return order, attempt

    def _prepare(self, order, operation, operation_id, digest, snapshot_value):
        attempt = WriteAttemptRecord(
            attempt_id=self.next_attempt_id,
            exchange_order_row_id=order.exchange_order_row_id,
            operation=operation,
            operation_id=operation_id,
            client_order_id=order.client_order_id,
            instrument_id=order.instrument_id,
            state=WriteState.PREPARED,
            request_digest=digest,
            safe_request_snapshot=dict(snapshot_value),
            attempt_count=1,
            lease_generation=1,
        )
        self.current = attempt
        self.unresolved_order = order
        self.events.append(("PREPARE", operation))
        return attempt

    def transition(
        self,
        attempt,
        *,
        event,
        exchange_order_id=None,
        order_state=None,
        reason_code=None,
        safe_response_snapshot=None,
    ):
        updated = WriteAttemptRecord(
            **{
                **attempt.__dict__,
                "state": transition_write_state(attempt.state, event),
            }
        )
        self.current = updated
        self.events.append((event.value, updated.state.value, reason_code))
        return updated

    def order_for_attempt(self, attempt):
        time.sleep(self.atomic_order_delay_seconds)
        return self.unresolved_order


def response(**overrides):
    item = {
        "ordId": "exchange-order-1",
        "clOrdId": "WriterOrder001",
        "sCode": "0",
    }
    item.update(overrides)
    return {"code": "0", "data": [item]}


def writer(read, write, store):
    return OkxDemoOrderWriter(
        read_client=read,
        write_transport=write,
        store=store,
        now_provider=lambda: NOW,
    )


def managed_order():
    return ManagedOrder(
        exchange_order_row_id=101,
        trade_intent_id=7,
        approval_id=3,
        canonical_hash="b" * 64,
        policy_digest="c" * 64,
        approved_payload_hash="d" * 64,
        instrument_id="BTC-USDT-SWAP",
        client_order_id="WriterOrder001",
        exchange_order_id="exchange-order-1",
        side="buy",
        position_side="long",
        order_type="limit",
        contracts=Decimal("0.02"),
        limit_price=Decimal("57000.1"),
        reduce_only=False,
    )


def test_limit_place_is_prepared_before_post_and_reconciled() -> None:
    read = FakeReadClient(orders=[[order_item()]])
    write = FakeWriteTransport([response()])
    store = FakeStore()

    result = writer(read, write, store).place(
        approved(),
        submission_grant=submission_grant(),
    )

    assert result.status == "RECONCILED"
    assert write.calls == [
        (
            "/api/v5/trade/order",
            {
                "instId": "BTC-USDT-SWAP",
                "tdMode": "isolated",
                "side": "buy",
                            "posSide": "long",
                "ordType": "limit",
                "sz": "0.02",
                "clOrdId": "WriterOrder001",
                "px": "57000.1",
            },
        )
    ]
    assert [event[0] for event in store.events] == [
        "PREPARE",
        "ACKNOWLEDGE",
        "RECONCILE",
    ]


def test_atomic_dispatch_posts_once_and_same_receipt_cannot_replay() -> None:
    body = {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "isolated",
        "side": "buy",
        "posSide": "long",
        "ordType": "limit",
        "sz": "0.02",
        "clOrdId": "WriterOrder001",
        "px": "57000.1",
    }
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    order = managed_order()
    attempt = WriteAttemptRecord(
        attempt_id=7,
        exchange_order_row_id=order.exchange_order_row_id,
        operation="PLACE",
        operation_id=order.client_order_id,
        client_order_id=order.client_order_id,
        instrument_id=order.instrument_id,
        state=WriteState.DISPATCHED,
        request_digest=digest,
        safe_request_snapshot=body,
        attempt_count=1,
        lease_generation=3,
    )
    store = FakeStore(unresolved=attempt, unresolved_order=order)
    write = FakeWriteTransport([response()])
    subject = writer(FakeReadClient(orders=[[order_item()]]), write, store)
    receipt = {
        "attempt_id": attempt.attempt_id,
        "exchange_order_row_id": order.exchange_order_row_id,
        "client_order_id": order.client_order_id,
        "instrument_id": order.instrument_id,
        "request_body": body,
        "request_digest": digest,
        "lease_generation": 3,
        "runtime_instance_id": "RuntimeAtomic1",
        "holder_token_digest": "a" * 64,
        "bundle_digest": "c" * 64,
        "dispatch_remaining_ms": 900,
        "dispatch_claimed_at": NOW.isoformat(),
    }

    capability = subject.seal_atomic_dispatch(
        receipt, runtime_instance_id="RuntimeAtomic1"
    )
    result = subject.dispatch_atomic_once(capability)

    assert result.status == "RECONCILED"
    assert len(write.calls) == 1
    with pytest.raises(OkxDemoWriteBlocked, match="already consumed"):
        subject.dispatch_atomic_once(capability)
    assert len(write.calls) == 1


def test_atomic_dispatch_capability_is_single_use_under_concurrency() -> None:
    body = {
        "instId": "BTC-USDT-SWAP", "tdMode": "isolated", "side": "buy",
        "posSide": "long", "ordType": "limit", "sz": "0.02",
        "clOrdId": "WriterOrder001", "px": "57000.1",
    }
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    order = managed_order()
    attempt = WriteAttemptRecord(
        attempt_id=77, exchange_order_row_id=order.exchange_order_row_id,
        operation="PLACE", operation_id=order.client_order_id,
        client_order_id=order.client_order_id, instrument_id=order.instrument_id,
        state=WriteState.DISPATCHED, request_digest=digest,
        safe_request_snapshot=body, attempt_count=1, lease_generation=3,
    )
    store = FakeStore(unresolved=attempt, unresolved_order=order)
    write = FakeWriteTransport([response()])
    subject = writer(FakeReadClient(orders=[[order_item()]]), write, store)
    capability = subject.seal_atomic_dispatch({
        "attempt_id": 77, "exchange_order_row_id": order.exchange_order_row_id,
        "client_order_id": order.client_order_id, "instrument_id": order.instrument_id,
        "request_body": body, "request_digest": digest, "lease_generation": 3,
        "runtime_instance_id": "RuntimeAtomic1", "bundle_digest": "c" * 64,
        "holder_token_digest": "a" * 64,
        "dispatch_remaining_ms": 900, "dispatch_claimed_at": NOW.isoformat(),
    }, runtime_instance_id="RuntimeAtomic1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: _dispatch_outcome(subject, capability), range(2)))

    assert sorted(outcomes) == ["BLOCKED", "RECONCILED"]
    assert len(write.calls) == 1


def _dispatch_outcome(subject, capability):
    try:
        return subject.dispatch_atomic_once(capability).status
    except OkxDemoWriteBlocked:
        return "BLOCKED"


def test_atomic_dispatch_expired_after_db_revalidation_is_zero_post() -> None:
    body = {"instId": "BTC-USDT-SWAP"}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    order = managed_order()
    attempt = WriteAttemptRecord(
        attempt_id=78, exchange_order_row_id=order.exchange_order_row_id,
        operation="PLACE", operation_id=order.client_order_id,
        client_order_id=order.client_order_id, instrument_id=order.instrument_id,
        state=WriteState.DISPATCHED, request_digest=digest,
        safe_request_snapshot=body, attempt_count=1, lease_generation=3,
    )
    store = FakeStore(unresolved=attempt, unresolved_order=order)
    store.atomic_remaining_ms = 150
    store.atomic_order_delay_seconds = 0.1
    write = FakeWriteTransport([response()])
    subject = writer(FakeReadClient(), write, store)
    capability = subject.seal_atomic_dispatch({
        "attempt_id": 78, "exchange_order_row_id": order.exchange_order_row_id,
        "client_order_id": order.client_order_id, "instrument_id": order.instrument_id,
        "request_body": body, "request_digest": digest, "lease_generation": 3,
        "runtime_instance_id": "RuntimeAtomic1", "bundle_digest": "c" * 64,
        "holder_token_digest": "a" * 64,
        "dispatch_remaining_ms": 900, "dispatch_claimed_at": NOW.isoformat(),
    }, runtime_instance_id="RuntimeAtomic1")

    with pytest.raises(OkxDemoWriteBlocked, match="expired before POST"):
        subject.dispatch_atomic_once(capability)
    assert write.calls == []


def test_reconcile_unresolved_place_is_get_only_and_never_posts() -> None:
    attempt = WriteAttemptRecord(
        attempt_id=71,
        exchange_order_row_id=101,
        operation="PLACE",
        operation_id="WriterOrder001",
        client_order_id="WriterOrder001",
        instrument_id="BTC-USDT-SWAP",
        state=WriteState.PREPARED,
        request_digest="a" * 64,
        safe_request_snapshot={},
        attempt_count=1,
    )
    store = FakeStore(unresolved=attempt, unresolved_order=managed_order())
    read = FakeReadClient(orders=[[order_item()]])
    write = FakeWriteTransport()

    result = writer(read, write, store).reconcile_unresolved(attempt.attempt_id)

    assert result.status == "RECONCILED"
    assert write.calls == []
    assert store.lease_calls[0][0:2] == ("reconcile", attempt.attempt_id)


@pytest.mark.parametrize("state", ["live", "partially_filled", "filled"])
def test_place_reconciles_supported_exchange_states(state) -> None:
    read = FakeReadClient(orders=[[order_item(state=state)]])
    write = FakeWriteTransport([response()])
    store = FakeStore()

    result = writer(read, write, store).place(
        approved(),
        submission_grant=submission_grant(),
    )

    assert result.status == "RECONCILED"
    assert result.order_state == state


def test_explicit_rejection_is_terminal_without_read_retry() -> None:
    read = FakeReadClient()
    write = FakeWriteTransport(
        [{"code": "0", "data": [{"sCode": "51008", "clOrdId": "WriterOrder001"}]}]
    )
    store = FakeStore()

    result = writer(read, write, store).place(
        approved(),
        submission_grant=submission_grant(),
    )

    assert result.status == "REJECTED"
    assert not [call for call in read.calls if call[0] == "order"]
    assert len(write.calls) == 1


def test_timeout_queries_once_and_never_reposts() -> None:
    read = FakeReadClient(orders=[[order_item()]])
    write = FakeWriteTransport(
        [OkxDemoTransportError(unknown_write_outcome=True)]
    )
    store = FakeStore()

    result = writer(read, write, store).place(
        approved(),
        submission_grant=submission_grant(),
    )

    assert result.status == "RECONCILED"
    assert len(write.calls) == 1
    assert [event[0] for event in store.events] == [
        "PREPARE",
        "OUTCOME_UNKNOWN",
        "RECONCILE",
    ]


def test_mismatched_leverage_is_attested_then_order_is_placed() -> None:
    read = FakeReadClient(
        orders=[[order_item()]],
        leverages=[
            [
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "margin_mode": "isolated",
                        "position_side": "long",
                    "leverage": Decimal("2"),
                }
            ],
            [
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "margin_mode": "isolated",
                    "position_side": "long",
                    "leverage": Decimal("3"),
                }
            ],
        ],
    )
    write = FakeWriteTransport(
        [
            {
                "code": "0",
                "data": [
                    {
                        "sCode": "0",
                        "instId": "BTC-USDT-SWAP",
                        "lever": "3",
                        "mgnMode": "isolated",
                        "posSide": "long",
                    }
                ],
            },
            response(),
        ]
    )
    store = FakeStore()

    result = writer(read, write, store).place(
        approved(),
        submission_grant=submission_grant(),
    )

    assert result.status == "RECONCILED"
    assert [call[0] for call in write.calls] == [
        "/api/v5/account/set-leverage",
        "/api/v5/trade/order",
    ]
    assert [event[:2] for event in store.events] == [
        ("PREPARE", "SET_LEVERAGE"),
        ("ACKNOWLEDGE", "ACKNOWLEDGED"),
        ("RECONCILE", "RECONCILED"),
        ("PREPARE", "PLACE"),
        ("ACKNOWLEDGE", "ACKNOWLEDGED"),
        ("RECONCILE", "RECONCILED"),
    ]


def test_set_leverage_timeout_queries_and_never_reposts() -> None:
    read = FakeReadClient(
        orders=[[order_item()]],
        leverages=[
            [],
            [
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "margin_mode": "isolated",
                        "position_side": "long",
                    "leverage": Decimal("3"),
                }
            ],
        ],
    )
    write = FakeWriteTransport(
        [
            OkxDemoTransportError(unknown_write_outcome=True),
            response(),
        ]
    )
    store = FakeStore()

    result = writer(read, write, store).place(
        approved(),
        submission_grant=submission_grant(),
    )

    assert result.status == "RECONCILED"
    assert [call[0] for call in write.calls].count(
        "/api/v5/account/set-leverage"
    ) == 1


def test_prepared_crash_recovery_queries_and_does_not_post() -> None:
    order = managed_order()
    attempt = WriteAttemptRecord(
        attempt_id=8,
        exchange_order_row_id=101,
        operation="PLACE",
        operation_id="WriterOrder001",
        client_order_id="WriterOrder001",
        instrument_id="BTC-USDT-SWAP",
        state=WriteState.PREPARED,
        request_digest="b" * 64,
        safe_request_snapshot={"instId": "BTC-USDT-SWAP", "sz": "0.02"},
        attempt_count=1,
    )
    store = FakeStore(attempt, order)
    read = FakeReadClient(orders=[[order_item()]])
    write = FakeWriteTransport()

    result = writer(read, write, store).place(
        approved(),
        submission_grant=submission_grant(),
    )

    assert result.status == "RECONCILED"
    assert write.calls == []
    assert [event[0] for event in store.events] == [
        "ACKNOWLEDGE",
        "RECONCILE",
    ]


def test_unresolved_attempt_cannot_be_recovered_by_another_approval() -> None:
    order = ManagedOrder(
        **{
            **managed_order().__dict__,
            "approval_id": 999,
        }
    )
    attempt = WriteAttemptRecord(
        attempt_id=8,
        exchange_order_row_id=101,
        operation="PLACE",
        operation_id="WriterOrder001",
        client_order_id="WriterOrder001",
        instrument_id="BTC-USDT-SWAP",
        state=WriteState.PREPARED,
        request_digest="b" * 64,
        safe_request_snapshot={"instId": "BTC-USDT-SWAP", "sz": "0.02"},
        attempt_count=1,
    )
    store = FakeStore(attempt, order)

    with pytest.raises(OkxDemoWriteBlocked, match="different approval"):
        writer(FakeReadClient(), FakeWriteTransport(), store).place(
            approved(),
            submission_grant=submission_grant(),
        )


def test_unresolved_unknown_stays_sticky_and_blocks_new_post() -> None:
    order = managed_order()
    attempt = WriteAttemptRecord(
        attempt_id=8,
        exchange_order_row_id=101,
        operation="PLACE",
        operation_id="WriterOrder001",
        client_order_id="WriterOrder001",
        instrument_id="BTC-USDT-SWAP",
        state=WriteState.RECOVERY_REQUIRED,
        request_digest="b" * 64,
        safe_request_snapshot={"instId": "BTC-USDT-SWAP", "sz": "0.02"},
        attempt_count=1,
    )
    store = FakeStore(attempt, order)
    read = FakeReadClient(orders=[[]])
    write = FakeWriteTransport()

    result = writer(read, write, store).place(
        approved(),
        submission_grant=submission_grant(),
    )

    assert result.status == "RECOVERY_REQUIRED"
    assert write.calls == []
    assert store.current.state == WriteState.RECOVERY_REQUIRED


def test_cancel_ack_requires_terminal_order_state() -> None:
    order = managed_order()
    store = FakeStore()
    read = FakeReadClient(orders=[[order_item(state="live")]])
    write = FakeWriteTransport([response()])

    result = writer(read, write, store).cancel(
        order,
        submission_grant=submission_grant(),
    )

    assert result.status == "RECOVERY_REQUIRED"
    assert write.calls[0][0] == "/api/v5/trade/cancel-order"


def test_cancel_reconciles_canceled_state() -> None:
    order = managed_order()
    store = FakeStore()
    read = FakeReadClient(orders=[[order_item(state="canceled")]])
    write = FakeWriteTransport([response()])

    result = writer(read, write, store).cancel(
        order,
        submission_grant=submission_grant(),
    )

    assert result.status == "RECONCILED"
    assert result.order_state == "canceled"


def test_recovery_cancel_binds_grant_before_the_only_network_post() -> None:
    store = FakeStore(unresolved_order=managed_order())
    read = FakeReadClient(orders=[[order_item(state="canceled")]])
    write = FakeWriteTransport([response()])

    result = writer(read, write, store).recovery_cancel(
        recovery_grant_database_id=448,
    )

    assert result.status == "RECONCILED"
    assert store.lease_calls == [("recovery", 448, NOW)]
    assert store.current.recovery_grant_database_id == 448
    assert write.calls == [
        (
            "/api/v5/trade/cancel-order",
            {
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "WriterOrder001",
            },
        )
    ]


def test_amend_reconciles_new_precision_and_request_id() -> None:
    order = managed_order()
    store = FakeStore()
    read = FakeReadClient(
        orders=[
            [
                order_item(
                    size=Decimal("0.03"),
                    price=Decimal("57001.2"),
                )
            ]
        ],
    )
    write = FakeWriteTransport([response(reqId="AmendRequest001")])

    result = writer(read, write, store).amend(
        order,
        submission_grant=submission_grant(),
        request_id="AmendRequest001",
        new_contracts=Decimal("0.03"),
        new_price=Decimal("57001.2"),
    )

    assert result.status == "RECONCILED"
    assert write.calls[0] == (
        "/api/v5/trade/amend-order",
        {
            "instId": "BTC-USDT-SWAP",
            "clOrdId": "WriterOrder001",
            "reqId": "AmendRequest001",
            "cxlOnFail": True,
            "newSz": "0.03",
            "newPx": "57001.2",
        },
    )


def test_reduce_only_market_execution_is_classified_as_close() -> None:
    store = FakeStore()
    read = FakeReadClient(
        orders=[
            [
                order_item(
                    state="filled",
                    side="sell",
                    order_type="market",
                    reduce_only=True,
                    price=None,
                )
            ]
        ],
        positions=[
            [
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "margin_mode": "isolated",
                        "position_side": "long",
                    "contracts": Decimal("0.02"),
                }
            ],
            [],
        ],
    )
    write = FakeWriteTransport([response()])

    result = writer(read, write, store).place(
        approved(
            side="sell",
            order_type="market",
            limit_price=None,
            reduce_only=True,
        ),
        submission_grant=submission_grant(),
    )

    assert result.operation == "CLOSE"
    assert write.calls[0][1]["reduceOnly"] is True


def test_canceled_partial_close_creates_deterministic_residual_cleanup() -> None:
    store = FakeStore()
    read = FakeReadClient(
        orders=[
            [
                order_item(
                    state="canceled",
                    side="sell",
                    order_type="market",
                    reduce_only=True,
                    price=None,
                )
            ],
            [
                order_item(
                    order_id="exchange-order-2",
                    client_order_id="WriterOrder001C1",
                    state="filled",
                    side="sell",
                    order_type="market",
                    reduce_only=True,
                    price=None,
                    size=Decimal("0.01"),
                )
            ],
        ],
        positions=[
            [
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "margin_mode": "isolated",
                        "position_side": "long",
                    "contracts": Decimal("0.02"),
                }
            ],
            [
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "margin_mode": "isolated",
                        "position_side": "long",
                    "contracts": Decimal("0.01"),
                }
            ],
            [
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "margin_mode": "isolated",
                    "position_side": "long",
                    "contracts": Decimal("0.01"),
                }
            ],
            [],
        ],
        leverages=[
            [
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "margin_mode": "isolated",
                    "position_side": "long",
                    "leverage": Decimal("3"),
                }
            ],
            [
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "margin_mode": "isolated",
                    "position_side": "long",
                    "leverage": Decimal("3"),
                }
            ],
        ],
    )
    write = FakeWriteTransport(
        [
            response(),
            response(
                ordId="exchange-order-2",
                clOrdId="WriterOrder001C1",
            ),
        ]
    )
    close = approved(
        side="sell",
        order_type="market",
        limit_price=None,
        reduce_only=True,
    )
    first = writer(read, write, store).place(
        close,
        submission_grant=submission_grant(),
    )
    second = writer(read, write, store).place(
        close,
        submission_grant=submission_grant(),
    )

    assert first.status == "RESIDUAL_CLOSE_REQUIRED"
    assert second.status == "RECONCILED"
    assert write.calls[1][1]["clOrdId"] == "WriterOrder001C1"
    assert write.calls[1][1]["sz"] == "0.01"
    assert len(write.calls) == 2


def test_expired_authorization_performs_no_store_or_network_work() -> None:
    read = FakeReadClient()
    write = FakeWriteTransport()
    store = FakeStore()

    with pytest.raises(OkxDemoWriteBlocked, match="expired"):
        writer(read, write, store).place(
            approved(),
            submission_grant=submission_grant(expires_at=NOW),
        )

    assert store.events == []
    assert read.calls == []
    assert write.calls == []


def test_mismatched_approved_payload_authorization_is_zero_db_and_network() -> None:
    read = FakeReadClient()
    write = FakeWriteTransport()
    store = FakeStore()

    with pytest.raises(OkxDemoWriteBlocked, match="different approved payload"):
        writer(read, write, store).place(
            approved(),
            submission_grant=submission_grant(approved_payload_hash="e" * 64),
        )

    assert store.lease_calls == []
    assert store.events == []
    assert read.calls == []
    assert write.calls == []
