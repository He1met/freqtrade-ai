from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.phase9_okx_demo import CanonicalOkxDemoSession


HEX_A = "a" * 64
HEX_B = "b" * 64
NOW = datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc)


class FakeRead:
    def instruments(self, instrument):
        return SimpleNamespace(
            items=[
                {
                    "inst_id": instrument,
                    "inst_type": "SWAP",
                    "base_ccy": "BTC",
                    "quote_ccy": "USDT",
                    "settle_ccy": "USDT",
                    "contract_value": "0.01",
                    "contract_value_ccy": "BTC",
                    "lot_size": "1",
                    "min_size": "1",
                    "tick_size": "0.1",
                    "state": "live",
                }
            ]
        )

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


def _session():
    closed = []
    write = FakeWrite()
    session = CanonicalOkxDemoSession(
        read_client=FakeRead(),
        write_port=write,
        account_fingerprint_digest=HEX_A,
        credential_generation_digest=HEX_B,
        close_callback=lambda: closed.append(True),
    )
    return session, write, closed


def test_redacted_probe_and_transport_have_no_credential_surface() -> None:
    session, write, closed = _session()
    probe = session.probe(instrument="BTC-USDT-SWAP", observed_at=NOW)
    assert probe.permissions == {"read": True, "trade": True, "withdraw": False}
    assert probe.simulated_trading is True
    assert probe.allow_real_funds is False
    assert probe.account_fingerprint_digest == HEX_A
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
    session, _write, _closed = _session()
    session.close()
    with pytest.raises(CanonicalExecutionChainBlocked) as blocked:
        session.probe(instrument="BTC-USDT-SWAP", observed_at=NOW)
    assert blocked.value.code == "BLOCKED_OKX_DEMO_SESSION_CLOSED"


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
