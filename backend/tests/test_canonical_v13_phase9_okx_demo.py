from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from app.adapters.okx_demo.errors import OkxReadAdapterError
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.phase9_okx_demo import CanonicalOkxDemoSession


HEX_A = "a" * 64
HEX_B = "b" * 64
NOW = datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc)


class FakeRead:
    def __init__(self):
        self.calls = []
        self.snapshots = {
            "instruments": self._snapshot(
                "instruments",
                [
                    {
                        "inst_id": "BTC-USDT-SWAP",
                        "inst_type": "SWAP",
                        "base_ccy": "BTC",
                        "quote_ccy": "USDT",
                        "settle_ccy": "USDT",
                        "contract_type": "linear",
                        "contract_value": "0.01",
                        "contract_value_ccy": "BTC",
                        "lot_size": "1",
                        "min_size": "1",
                        "tick_size": "0.1",
                        "state": "live",
                    }
                ],
            ),
            "mark_price": self._snapshot(
                "mark_price",
                [
                    {
                        "inst_id": "BTC-USDT-SWAP",
                        "price_kind": "mark",
                        "price": "10000.1",
                        "timestamp": NOW - timedelta(seconds=1),
                    }
                ],
                exchange_timestamp=NOW - timedelta(seconds=1),
            ),
            "account_config": self._snapshot(
                "account_config",
                [
                    {
                        "account_level": "2",
                        "position_mode": "long_short_mode",
                        "auto_loan": False,
                        "greeks_type": "PA",
                    }
                ],
                authenticated=True,
            ),
            "leverage": self._snapshot(
                "leverage",
                [
                    {
                        "inst_id": "BTC-USDT-SWAP",
                        "margin_mode": "isolated",
                        "position_side": "long",
                        "leverage": "14",
                    },
                    {
                        "inst_id": "BTC-USDT-SWAP",
                        "margin_mode": "isolated",
                        "position_side": "short",
                        "leverage": "14",
                    },
                ],
                authenticated=True,
            ),
            "exchange_max_leverage": self._snapshot(
                "exchange_max_leverage",
                [
                    {
                        "inst_id": "BTC-USDT-SWAP",
                        "inst_type": "SWAP",
                        "margin_mode": "isolated",
                        "position_side": "long",
                        "requested_leverage": "14",
                        "max_leverage": "20",
                        "min_leverage": "0.01",
                        "has_pending_orders": False,
                    }
                ],
                authenticated=True,
            ),
            "positions": self._snapshot("positions", [], authenticated=True),
            "pending_orders": self._snapshot(
                "pending_orders", [], authenticated=True
            ),
            "orders_history": self._snapshot(
                "orders_history", [], authenticated=True
            ),
            "maximum_order_quantity": self._snapshot(
                "maximum_order_quantity",
                [
                    {
                        "inst_id": "BTC-USDT-SWAP",
                        "margin_mode": "isolated",
                        "price": "10000.1",
                        "leverage": "14",
                        "max_buy": "2",
                    }
                ],
                authenticated=True,
            ),
        }

    @staticmethod
    def _snapshot(resource, items, *, authenticated=False, exchange_timestamp=None):
        return SimpleNamespace(
            status="READY",
            metadata=SimpleNamespace(
                execution_target="OKX_DEMO",
                source="okx_demo_rest",
                resource=resource,
                fetched_at=NOW - timedelta(seconds=2),
                exchange_timestamp=exchange_timestamp,
                expires_at=NOW + timedelta(seconds=30),
                stale=False,
                authenticated=authenticated,
            ),
            items=items,
        )

    def instruments(self, instrument):
        self.calls.append(("instruments", instrument))
        return self.snapshots["instruments"]

    def mark_price(self, instrument):
        self.calls.append(("mark_price", instrument))
        return self.snapshots["mark_price"]

    def account_config(self):
        self.calls.append(("account_config", None))
        return self.snapshots["account_config"]

    def leverage(self, instrument):
        self.calls.append(("leverage", instrument))
        return self.snapshots["leverage"]

    def exchange_max_leverage(self, instrument):
        self.calls.append(("exchange_max_leverage", instrument))
        return self.snapshots["exchange_max_leverage"]

    def positions(self, instrument):
        self.calls.append(("positions", instrument))
        return self.snapshots["positions"]

    def pending_orders(self, instrument, *, limit):
        assert limit == 100
        self.calls.append(("pending_orders", instrument))
        return self.snapshots["pending_orders"]

    def maximum_order_quantity(
        self, instrument, *, td_mode, price, leverage
    ):
        self.calls.append(
            (
                "maximum_order_quantity",
                instrument,
                td_mode,
                format(price, "f"),
                format(leverage, "f"),
            )
        )
        return self.snapshots["maximum_order_quantity"]

    def orders_history(self, instrument, *, limit):
        assert limit == 100
        self.calls.append(("orders_history", instrument))
        return self.snapshots["orders_history"]

    def order(self, instrument, *, client_order_id):
        return SimpleNamespace(
            items=[
                {
                    "inst_id": instrument,
                    "client_order_id": client_order_id,
                    "order_id": "987",
                }
            ]
        )

    def fills_history(self, instrument, *, limit):
        assert limit == 100
        return SimpleNamespace(
            items=[
                {"inst_id": instrument, "order_id": "987", "fill_id": "1"},
                {"inst_id": instrument, "order_id": "other", "fill_id": "2"},
            ]
        )


class FakeWrite:
    def __init__(self):
        self.calls = []

    def post(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "code": "0",
            "data": [{"ordId": "987", "clOrdId": "canonical-1", "sCode": "0"}],
        }


def _session(read=None):
    closed = []
    write = FakeWrite()
    read = read or FakeRead()
    session = CanonicalOkxDemoSession(
        read_client=read,
        write_port=write,
        account_fingerprint_digest=HEX_A,
        credential_generation_digest=HEX_B,
        close_callback=lambda: closed.append(True),
        now_provider=lambda: NOW,
    )
    return session, read, write, closed


def test_redacted_probe_and_transport_have_no_credential_surface() -> None:
    session, read, write, closed = _session()
    probe = session.probe(instrument="BTC-USDT-SWAP")
    assert probe.permissions == {"read": True, "trade": True, "withdraw": False}
    assert probe.simulated_trading is True
    assert probe.allow_real_funds is False
    assert probe.account_fingerprint_digest == HEX_A
    assert probe.contract_value == "0.01"
    assert probe.contract_value_currency == "BTC"
    assert probe.lot_size == "1"
    assert probe.min_size == "1"
    assert probe.tick_size == "0.1"
    assert probe.mark_price == "10000.1"
    assert probe.current_long_leverage == "14"
    assert probe.current_short_leverage == "14"
    assert probe.exchange_max_leverage == "20"
    assert probe.limit_price == "10000.1"
    assert probe.maximum_buy_contracts == "2"
    assert probe.active_position_count == probe.pending_order_count == 0
    assert probe.observed_at == NOW - timedelta(seconds=1)
    assert probe.expires_at == NOW + timedelta(seconds=30)
    assert all(
        len(digest) == 64
        for digest in (
            probe.instrument_digest,
            probe.mark_price_digest,
            probe.account_config_digest,
            probe.leverage_digest,
            probe.exchange_max_leverage_digest,
            probe.positions_digest,
            probe.pending_orders_digest,
            probe.maximum_order_quantity_digest,
        )
    )
    assert read.calls == [
        ("instruments", "BTC-USDT-SWAP"),
        ("mark_price", "BTC-USDT-SWAP"),
        ("account_config", None),
        ("leverage", "BTC-USDT-SWAP"),
        ("exchange_max_leverage", "BTC-USDT-SWAP"),
        ("positions", "BTC-USDT-SWAP"),
        ("pending_orders", "BTC-USDT-SWAP"),
        (
            "maximum_order_quantity",
            "BTC-USDT-SWAP",
            "isolated",
            "10000.1",
            "14",
        ),
    ]
    expected_max_digest = sha256(
        json.dumps(
            {
                "execution_target": "OKX_DEMO",
                "resource": "exchange_max_leverage",
                "source": "okx_demo_rest",
                "authenticated": True,
                "observed_at": (NOW - timedelta(seconds=2)).isoformat(),
                "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
                "facts": {
                    "instrument": "BTC-USDT-SWAP",
                    "exchange_max_leverage": "20",
                    "has_pending_orders": False,
                },
            },
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert probe.exchange_max_leverage_digest == expected_max_digest
    expected_current_digest = sha256(
        json.dumps(
            {
                "execution_target": "OKX_DEMO",
                "resource": "leverage",
                "source": "okx_demo_rest",
                "authenticated": True,
                "observed_at": (NOW - timedelta(seconds=2)).isoformat(),
                "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
                "facts": {
                    "instrument": "BTC-USDT-SWAP",
                    "account_fingerprint_digest": HEX_A,
                    "long": probe.current_long_leverage,
                    "short": probe.current_short_leverage,
                },
            },
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert probe.leverage_digest == expected_current_digest
    assert not hasattr(session, "credentials")
    placed = session.place(
        {
            "instId": "BTC-USDT-SWAP",
            "clOrdId": "canonical-1",
            "side": "buy",
            "sz": "1",
        }
    )
    assert placed["code"] == "0"
    assert write.calls == [
        {
            "path": "/api/v5/trade/order",
            "body": {
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "canonical-1",
                "side": "buy",
                "sz": "1",
            },
        }
    ]
    assert (
        session.query(instrument="BTC-USDT-SWAP", client_order_id="canonical-1")[
            "data"
        ][0]["ordId"]
        == "987"
    )
    assert [
        item["fill_id"]
        for item in session.fills(instrument="BTC-USDT-SWAP", order_id="987")
    ] == ["1"]
    session.close()
    session.close()
    assert closed == [True]


def test_exact_51603_plus_empty_pending_and_history_proves_absence() -> None:
    class AbsentRead(FakeRead):
        def order(self, instrument, *, client_order_id):
            raise OkxReadAdapterError(
                kind="BUSINESS_ERROR",
                status="FAILED",
                message="redacted",
                retryable=False,
                okx_code="51603",
            )

    session, read, _write, _closed = _session(AbsentRead())
    evidence = session.prove_absent(
        instrument="BTC-USDT-SWAP", client_order_id="canonical-1"
    )
    assert evidence.exact_order_result_code == "51603"
    assert evidence.pending_order_match_count == 0
    assert evidence.history_order_match_count == 0
    assert len(evidence.evidence_digest) == 64
    assert read.calls[-2:] == [
        ("pending_orders", "BTC-USDT-SWAP"),
        ("orders_history", "BTC-USDT-SWAP"),
    ]


def test_absence_is_blocked_when_pending_or_history_matches() -> None:
    class ContradictedRead(FakeRead):
        def __init__(self):
            super().__init__()
            self.snapshots["orders_history"].items = [
                {"client_order_id": "canonical-1"}
            ]

        def order(self, instrument, *, client_order_id):
            raise OkxReadAdapterError(
                kind="BUSINESS_ERROR",
                status="FAILED",
                message="redacted",
                retryable=False,
                okx_code="51603",
            )

    session, _read, _write, _closed = _session(ContradictedRead())
    with pytest.raises(
        CanonicalExecutionChainBlocked,
        match="BLOCKED_OKX_DEMO_ORDER_ABSENCE_CONTRADICTED",
    ):
        session.prove_absent(
            instrument="BTC-USDT-SWAP", client_order_id="canonical-1"
        )


def test_probe_verifies_untimestamped_maximum_snapshot_after_request() -> None:
    read = FakeRead()
    maximum = read.snapshots["maximum_order_quantity"]
    maximum.metadata.fetched_at = NOW + timedelta(seconds=1)
    maximum.metadata.expires_at = NOW + timedelta(seconds=16)
    verification_times = iter((NOW, NOW + timedelta(seconds=1)))
    session = CanonicalOkxDemoSession(
        read_client=read,
        write_port=FakeWrite(),
        account_fingerprint_digest=HEX_A,
        credential_generation_digest=HEX_B,
        close_callback=lambda: None,
        now_provider=lambda: next(verification_times),
    )

    probe = session.probe(instrument="BTC-USDT-SWAP")

    assert probe.maximum_order_quantity_observed_at == NOW + timedelta(seconds=1)
    assert probe.maximum_order_quantity_expires_at == NOW + timedelta(seconds=16)


@pytest.mark.parametrize(
    ("fetched_at", "expires_at"),
    (
        (NOW + timedelta(seconds=2), NOW + timedelta(seconds=17)),
        (NOW + timedelta(seconds=1), NOW + timedelta(seconds=1)),
    ),
)
def test_probe_still_rejects_future_or_expired_maximum_snapshot(
    fetched_at, expires_at
) -> None:
    read = FakeRead()
    maximum = read.snapshots["maximum_order_quantity"]
    maximum.metadata.fetched_at = fetched_at
    maximum.metadata.expires_at = expires_at
    verification_times = iter((NOW, NOW + timedelta(seconds=1)))
    session = CanonicalOkxDemoSession(
        read_client=read,
        write_port=FakeWrite(),
        account_fingerprint_digest=HEX_A,
        credential_generation_digest=HEX_B,
        close_callback=lambda: None,
        now_provider=lambda: next(verification_times),
    )

    with pytest.raises(CanonicalExecutionChainBlocked) as blocked:
        session.probe(instrument="BTC-USDT-SWAP")

    assert blocked.value.code == "BLOCKED_OKX_DEMO_SNAPSHOT_FRESHNESS"


def test_closed_session_fails_before_transport() -> None:
    session, _read, _write, _closed = _session()
    session.close()
    with pytest.raises(CanonicalExecutionChainBlocked) as blocked:
        session.probe(instrument="BTC-USDT-SWAP")
    assert blocked.value.code == "BLOCKED_OKX_DEMO_SESSION_CLOSED"


def test_dispatch_guard_is_typed_flat_current_capacity_evidence() -> None:
    session, read, write, _closed = _session()
    guard = session.dispatch_guard(
        instrument="BTC-USDT-SWAP",
        limit_price="10000.1",
        effective_leverage="14",
        minimum_size="1",
    )
    assert guard.maximum_buy_contracts == "2"
    assert guard.long_contracts == guard.short_contracts == "0"
    assert guard.active_position_count == guard.pending_order_count == 0
    assert guard.credential_generation_digest == HEX_B
    assert len(guard.guard_digest) == 64
    assert write.calls == []
    assert read.calls == [
        ("positions", "BTC-USDT-SWAP"),
        ("pending_orders", "BTC-USDT-SWAP"),
        ("leverage", "BTC-USDT-SWAP"),
        (
            "maximum_order_quantity",
            "BTC-USDT-SWAP",
            "isolated",
            "10000.1",
            "14",
        ),
    ]


def test_dispatch_guard_canonicalizes_equivalent_leverage_scale() -> None:
    read = FakeRead()
    for item in read.snapshots["leverage"].items:
        item["leverage"] = "2.000"
    read.snapshots["maximum_order_quantity"].items[0]["leverage"] = "2"
    session, _read, write, _closed = _session(read)

    guard = session.dispatch_guard(
        instrument="BTC-USDT-SWAP",
        limit_price="10000.1",
        effective_leverage="2.000000000000000000",
        minimum_size="1",
    )

    assert guard.effective_leverage == "2"
    assert guard.current_short_leverage == "2"
    expected = sha256(
        json.dumps(
            {
                "execution_target": "OKX_DEMO",
                "resource": "leverage",
                "source": "okx_demo_rest",
                "authenticated": True,
                "observed_at": guard.leverage_observed_at.isoformat(),
                "expires_at": guard.leverage_expires_at.isoformat(),
                "facts": {
                    "instrument": "BTC-USDT-SWAP",
                    "account_fingerprint_digest": HEX_A,
                    "long": guard.effective_leverage,
                    "short": guard.current_short_leverage,
                },
            },
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert guard.leverage_digest == expected
    assert write.calls == []


def test_dispatch_guard_zero_capacity_blocks_without_post() -> None:
    read = FakeRead()
    read.snapshots["maximum_order_quantity"].items[0]["max_buy"] = "0"
    session, _read, write, _closed = _session(read)
    with pytest.raises(
        CanonicalExecutionChainBlocked,
        match="BLOCKED_OKX_DEMO_DISPATCH_CAPACITY_SHORTFALL",
    ):
        session.dispatch_guard(
            instrument="BTC-USDT-SWAP",
            limit_price="10000.1",
            effective_leverage="14",
            minimum_size="1",
        )
    assert write.calls == []


def test_probe_rejects_caller_supplied_observation_time() -> None:
    session, _read, _write, _closed = _session()
    with pytest.raises(TypeError):
        session.probe(  # type: ignore[call-arg]
            instrument="BTC-USDT-SWAP", observed_at=NOW
        )


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        (
            lambda read: setattr(read.snapshots["mark_price"], "status", "BLOCKED"),
            "BLOCKED_OKX_DEMO_SNAPSHOT_FRESHNESS",
        ),
        (
            lambda read: setattr(read.snapshots["instruments"].metadata, "stale", True),
            "BLOCKED_OKX_DEMO_SNAPSHOT_FRESHNESS",
        ),
        (
            lambda read: setattr(
                read.snapshots["account_config"].metadata, "expires_at", NOW
            ),
            "BLOCKED_OKX_DEMO_SNAPSHOT_FRESHNESS",
        ),
        (
            lambda read: setattr(
                read.snapshots["account_config"].metadata, "authenticated", False
            ),
            "BLOCKED_OKX_DEMO_SNAPSHOT_FRESHNESS",
        ),
        (
            lambda read: read.snapshots["instruments"].items.clear(),
            "BLOCKED_OKX_DEMO_INSTRUMENT_IDENTITY",
        ),
        (
            lambda read: (
                read.snapshots["mark_price"].items[0].update(inst_id="ETH-USDT-SWAP")
            ),
            "BLOCKED_OKX_DEMO_MARK_IDENTITY",
        ),
        (
            lambda read: read.snapshots["mark_price"].items[0].update(price="0"),
            "BLOCKED_OKX_DEMO_MARK_PRICE",
        ),
        (
            lambda read: (
                read.snapshots["account_config"]
                .items[0]
                .update(position_mode="net_mode")
            ),
            "BLOCKED_OKX_DEMO_ACCOUNT_CONFIG",
        ),
        (
            lambda read: read.snapshots["leverage"].items.pop(),
            "BLOCKED_OKX_DEMO_LEVERAGE_IDENTITY",
        ),
        (
            lambda read: (
                read.snapshots["leverage"].items[1].update(position_side="long")
            ),
            "BLOCKED_OKX_DEMO_LEVERAGE_IDENTITY",
        ),
        (
            lambda read: read.snapshots["leverage"].items[0].update(leverage="0"),
            "BLOCKED_OKX_DEMO_LEVERAGE_VALUE",
        ),
        (
            lambda read: read.snapshots["exchange_max_leverage"].items.clear(),
            "BLOCKED_OKX_DEMO_EXCHANGE_MAX_LEVERAGE_IDENTITY",
        ),
        (
            lambda read: read.snapshots["exchange_max_leverage"].items[0].update(
                inst_id="ETH-USDT-SWAP"
            ),
            "BLOCKED_OKX_DEMO_EXCHANGE_MAX_LEVERAGE_IDENTITY",
        ),
        (
            lambda read: read.snapshots["exchange_max_leverage"].items[0].update(
                max_leverage="0"
            ),
            "BLOCKED_OKX_DEMO_EXCHANGE_MAX_LEVERAGE_VALUE",
        ),
        (
            lambda read: setattr(
                read.snapshots["exchange_max_leverage"].metadata,
                "authenticated",
                False,
            ),
            "BLOCKED_OKX_DEMO_SNAPSHOT_FRESHNESS",
        ),
        (
            lambda read: read.snapshots["positions"].items.append(
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "margin_mode": "isolated",
                    "position_side": "long",
                    "contracts": "1",
                }
            ),
            "BLOCKED_OKX_DEMO_POSITION_NOT_FLAT",
        ),
        (
            lambda read: read.snapshots["pending_orders"].items.append(
                {"inst_id": "BTC-USDT-SWAP"}
            ),
            "BLOCKED_OKX_DEMO_PENDING_ORDERS",
        ),
        (
            lambda read: read.snapshots["exchange_max_leverage"].items[0].update(
                has_pending_orders=True
            ),
            "BLOCKED_OKX_DEMO_PENDING_ORDERS",
        ),
        (
            lambda read: read.snapshots["maximum_order_quantity"].items[0].update(
                max_buy="0"
            ),
            "BLOCKED_OKX_DEMO_CAPACITY_SHORTFALL",
        ),
        (
            lambda read: read.snapshots["maximum_order_quantity"].items[0].update(
                max_buy="NaN"
            ),
            "BLOCKED_OKX_DEMO_MAXIMUM_ORDER_QUANTITY_VALUE",
        ),
        (
            lambda read: read.snapshots["maximum_order_quantity"].items[0].update(
                inst_id="ETH-USDT-SWAP"
            ),
            "BLOCKED_OKX_DEMO_MAXIMUM_ORDER_QUANTITY_IDENTITY",
        ),
        (
            lambda read: setattr(
                read.snapshots["maximum_order_quantity"].metadata,
                "expires_at",
                NOW,
            ),
            "BLOCKED_OKX_DEMO_SNAPSHOT_FRESHNESS",
        ),
    ),
)
def test_probe_fails_closed_on_missing_stale_identity_account_and_leverage(
    mutation, code
) -> None:
    read = FakeRead()
    mutation(read)
    session, _read, _write, _closed = _session(read)
    with pytest.raises(CanonicalExecutionChainBlocked) as blocked:
        session.probe(instrument="BTC-USDT-SWAP")
    assert blocked.value.code == code


@pytest.mark.parametrize(
    "field", ("contract_value", "lot_size", "min_size", "tick_size")
)
def test_probe_rejects_non_positive_contract_facts(field) -> None:
    read = FakeRead()
    read.snapshots["instruments"].items[0][field] = "0"
    session, _read, _write, _closed = _session(read)
    with pytest.raises(
        CanonicalExecutionChainBlocked, match="BLOCKED_OKX_DEMO_INSTRUMENT_CONTRACT"
    ):
        session.probe(instrument="BTC-USDT-SWAP")


def test_probe_resource_digest_changes_with_safe_metadata() -> None:
    first, _read, _write, _closed = _session()
    first_probe = first.probe(instrument="BTC-USDT-SWAP")
    changed = FakeRead()
    changed.snapshots["instruments"].items[0]["contract_value"] = "0.02"
    second, _read, _write, _closed = _session(changed)
    second_probe = second.probe(instrument="BTC-USDT-SWAP")
    assert first_probe.instrument_digest != second_probe.instrument_digest
    assert first_probe.mark_price_digest == second_probe.mark_price_digest


def test_constructor_rejects_non_digest_identity() -> None:
    with pytest.raises(CanonicalExecutionChainBlocked) as blocked:
        CanonicalOkxDemoSession(
            read_client=FakeRead(),
            write_port=FakeWrite(),
            account_fingerprint_digest="secret",
            credential_generation_digest=HEX_B,
            close_callback=lambda: None,
        )
    assert blocked.value.code == "BLOCKED_OKX_DEMO_ATTESTATION_DIGEST"
