from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

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
    assert probe.observed_at == NOW - timedelta(seconds=1)
    assert probe.expires_at == NOW + timedelta(seconds=30)
    assert all(
        len(digest) == 64
        for digest in (
            probe.instrument_digest,
            probe.mark_price_digest,
            probe.account_config_digest,
            probe.leverage_digest,
        )
    )
    assert read.calls == [
        ("instruments", "BTC-USDT-SWAP"),
        ("mark_price", "BTC-USDT-SWAP"),
        ("account_config", None),
        ("leverage", "BTC-USDT-SWAP"),
    ]
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


def test_closed_session_fails_before_transport() -> None:
    session, _read, _write, _closed = _session()
    session.close()
    with pytest.raises(CanonicalExecutionChainBlocked) as blocked:
        session.probe(instrument="BTC-USDT-SWAP")
    assert blocked.value.code == "BLOCKED_OKX_DEMO_SESSION_CLOSED"


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
