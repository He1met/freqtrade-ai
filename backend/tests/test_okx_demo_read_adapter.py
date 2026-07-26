from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Mapping

import pytest

from app.adapters.okx_demo import (
    InstrumentSpec,
    OkxDemoReadAdapter,
    OkxReadAdapterError,
    OkxReadHttpResponse,
)


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
FRESH_TS = str(int((NOW - timedelta(seconds=1)).timestamp() * 1000))
OLD_TS = str(int((NOW - timedelta(hours=1)).timestamp() * 1000))


class RecordedTransport:
    def __init__(self, payloads=None, *, status_code: int = 200) -> None:
        self.payloads = list(payloads or [])
        self.status_code = status_code
        self.calls: list[dict[str, object]] = []

    def get(
        self,
        *,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> OkxReadHttpResponse:
        self.calls.append(
            {
                "path": path,
                "query": dict(query),
                "headers": dict(headers),
                "timeout_seconds": timeout_seconds,
            }
        )
        payload = self.payloads.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return OkxReadHttpResponse(
            status_code=self.status_code,
            payload=payload,
            received_at=NOW,
        )


class RecordedCredentialProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def authorization_headers(
        self,
        *,
        method: str,
        request_path: str,
        body: str,
    ) -> Mapping[str, str]:
        self.calls.append(
            {"method": method, "request_path": request_path, "body": body}
        )
        return {
            "OK-ACCESS-KEY": "test-redacted",
            "OK-ACCESS-SIGN": "test-redacted",
            "OK-ACCESS-TIMESTAMP": "test-redacted",
            "OK-ACCESS-PASSPHRASE": "test-redacted",
        }


class HeaderCredentialProvider:
    def __init__(self, headers) -> None:
        self.headers = headers

    def authorization_headers(
        self,
        *,
        method: str,
        request_path: str,
        body: str,
    ) -> Mapping[str, str]:
        del method, request_path, body
        return self.headers


def adapter(payloads, *, credentials=None, status_code=200, ttl_seconds=None):
    transport = RecordedTransport(payloads, status_code=status_code)
    instance = OkxDemoReadAdapter(
        execution_target="OKX_DEMO",
        transport=transport,
        credential_provider=credentials,
        now_provider=lambda: NOW,
        ttl_seconds=ttl_seconds,
    )
    return instance, transport


def envelope(data, code="0"):
    return {"code": code, "msg": "", "data": data}


def test_adapter_rejects_every_target_except_okx_demo() -> None:
    with pytest.raises(OkxReadAdapterError) as exc_info:
        OkxDemoReadAdapter(execution_target="OKX_LIVE")

    assert exc_info.value.kind == "UNSAFE_TARGET"
    assert exc_info.value.status == "BLOCKED"


def test_instrument_schema_and_contract_conversion_use_exchange_metadata() -> None:
    instance, _ = adapter(
        [
            envelope(
                [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "instType": "SWAP",
                        "baseCcy": "BTC",
                        "quoteCcy": "USDT",
                        "settleCcy": "USDT",
                        "ctType": "linear",
                        "ctVal": "0.01",
                        "ctValCcy": "BTC",
                        "lotSz": "1",
                        "minSz": "2",
                        "tickSz": "0.1",
                        "state": "live",
                        "listTime": "1700000000000",
                    }
                ]
            )
        ]
    )

    snapshot = instance.instruments("BTC-USDT-SWAP")
    spec = InstrumentSpec.model_validate(snapshot.items[0])
    conversion = spec.units_to_contracts(Decimal("0.057"))

    assert snapshot.status == "READY"
    assert snapshot.metadata.execution_target == "OKX_DEMO"
    assert snapshot.metadata.source == "okx_demo_rest"
    assert snapshot.metadata.authenticated is False
    assert snapshot.metadata.stale is False
    assert spec.contract_value == Decimal("0.01")
    assert conversion.contracts == Decimal("5")
    assert conversion.normalized_units == Decimal("0.05")
    assert conversion.unit == "BTC"
    assert spec.contracts_to_units(Decimal("2")) == Decimal("0.02")
    with pytest.raises(ValueError, match="lot_size"):
        spec.contracts_to_units(Decimal("2.5"))


def test_public_market_resources_have_stable_normalized_schemas() -> None:
    payloads = [
        envelope(
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "last": "100",
                    "bidPx": "99",
                    "askPx": "101",
                    "open24h": "90",
                    "high24h": "110",
                    "low24h": "80",
                    "vol24h": "20",
                    "volCcy24h": "2",
                    "ts": FRESH_TS,
                }
            ]
        ),
        envelope([[FRESH_TS, "90", "110", "80", "100", "20", "2", "2000", "1"]]),
        envelope(
            [
                {
                    "bids": [["99", "2", "0", "3"]],
                    "asks": [["101", "1", "0", "2"]],
                    "ts": FRESH_TS,
                }
            ]
        ),
        envelope([{"instId": "BTC-USDT-SWAP", "markPx": "100", "ts": FRESH_TS}]),
        envelope([{"instId": "BTC-USDT", "idxPx": "100.5", "ts": FRESH_TS}]),
        envelope(
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "fundingRate": "0.0001",
                    "nextFundingRate": "0.0002",
                    "fundingTime": FRESH_TS,
                    "nextFundingTime": str(int(FRESH_TS) + 28800000),
                }
            ]
        ),
        envelope(
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "oi": "1234",
                    "oiCcy": "12.34",
                    "ts": FRESH_TS,
                }
            ]
        ),
    ]
    instance, transport = adapter(payloads)

    ticker = instance.ticker("BTC-USDT-SWAP")
    candles = instance.candles("BTC-USDT-SWAP", bar="1m", limit=1)
    book = instance.orderbook("BTC-USDT-SWAP", depth=5)
    mark = instance.mark_price("BTC-USDT-SWAP")
    index = instance.index_price("BTC-USDT")
    funding = instance.funding_rate("BTC-USDT-SWAP")
    interest = instance.open_interest("BTC-USDT-SWAP")

    assert ticker.items[0]["last"] == "100"
    assert candles.items[0]["confirmed"] is True
    assert book.items[0]["bids"][0] == {"price": "99", "size": "2", "orders": 3}
    assert mark.items[0]["price_kind"] == "mark"
    assert index.items[0]["price_kind"] == "index"
    assert funding.items[0]["funding_rate"] == "0.0001"
    assert interest.items[0]["open_interest_contracts"] == "1234"
    assert all(
        call["headers"].get("x-simulated-trading") is None
        for call in transport.calls
    )


def test_private_account_resources_require_demo_auth_but_never_render_it() -> None:
    credentials = RecordedCredentialProvider()
    payloads = [
        envelope(
            [
                {
                    "acctLv": "2",
                    "posMode": "net_mode",
                    "autoLoan": "false",
                    "greeksType": "PA",
                }
            ]
        ),
        envelope(
            [
                {
                    "uTime": FRESH_TS,
                    "totalEq": "1000",
                    "details": [
                        {
                            "ccy": "USDT",
                            "availBal": "900",
                            "cashBal": "950",
                            "frozenBal": "50",
                            "eq": "1000",
                            "upl": "10",
                        }
                    ],
                }
            ]
        ),
        envelope(
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "mgnMode": "isolated",
                    "posSide": "net",
                    "pos": "2",
                    "availPos": "2",
                    "avgPx": "95",
                    "markPx": "100",
                    "liqPx": "50",
                    "lever": "3",
                    "mgnRatio": "10",
                    "upl": "10",
                    "uTime": FRESH_TS,
                }
            ]
        ),
        envelope(
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "mgnMode": "isolated",
                    "posSide": "net",
                    "lever": "3",
                }
            ]
        ),
        envelope([{"instId": "BTC-USDT-SWAP", "maker": "-0.0002", "taker": "0.0005"}]),
        envelope(
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "123",
                    "clOrdId": "client123",
                    "state": "live",
                    "side": "buy",
                    "posSide": "net",
                    "ordType": "limit",
                    "px": "90",
                    "sz": "2",
                    "accFillSz": "0",
                    "avgPx": "",
                    "fee": "",
                    "feeCcy": "",
                    "cTime": FRESH_TS,
                    "uTime": FRESH_TS,
                }
            ]
        ),
    ]
    instance, transport = adapter(payloads, credentials=credentials)

    config = instance.account_config()
    balance = instance.balance("USDT")
    positions = instance.positions("BTC-USDT-SWAP")
    leverage = instance.leverage("BTC-USDT-SWAP")
    fees = instance.fees("BTC-USDT-SWAP")
    order = instance.order("BTC-USDT-SWAP", client_order_id="client123")

    assert config.items[0]["position_mode"] == "net_mode"
    assert balance.items[0]["available_balance"] == "900"
    assert positions.items[0]["contracts"] == "2"
    assert leverage.items[0]["leverage"] == "3"
    assert fees.items[0]["maker_rate"] == "-0.0002"
    assert order.items[0]["order_id"] == "123"
    assert all(snapshot.metadata.authenticated for snapshot in [
        config,
        balance,
        positions,
        leverage,
        fees,
        order,
    ])
    assert all(
        call["headers"]["x-simulated-trading"] == "1"
        for call in transport.calls
    )
    rendered = json.dumps(
        [snapshot.model_dump(mode="json") for snapshot in [
            config,
            balance,
            positions,
            leverage,
            fees,
            order,
        ]]
    )
    assert "OK-ACCESS-KEY" not in rendered
    assert "OK-ACCESS-SIGN" not in rendered
    assert "test-redacted" not in rendered
    assert credentials.calls[-1]["request_path"].endswith(
        "clOrdId=client123&instId=BTC-USDT-SWAP"
    )


def test_private_read_is_blocked_until_credential_provider_is_available() -> None:
    instance, transport = adapter([])

    with pytest.raises(OkxReadAdapterError) as exc_info:
        instance.balance()

    assert exc_info.value.kind == "UNAUTHORIZED"
    assert exc_info.value.status == "BLOCKED"
    assert transport.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        {
            "OK-ACCESS-KEY": "value",
            "OK-ACCESS-SIGN": "value",
            "OK-ACCESS-TIMESTAMP": "value",
            "OK-ACCESS-PASSPHRASE": "value",
            "x-simulated-trading": "0",
        },
        {
            "OK-ACCESS-KEY": "value",
            "ok-access-key": "value",
            "OK-ACCESS-SIGN": "value",
            "OK-ACCESS-TIMESTAMP": "value",
            "OK-ACCESS-PASSPHRASE": "value",
        },
        {
            "OK-ACCESS-KEY": "",
            "OK-ACCESS-SIGN": "value",
            "OK-ACCESS-TIMESTAMP": "value",
            "OK-ACCESS-PASSPHRASE": "value",
        },
        {
            "OK-ACCESS-KEY": "   ",
            "OK-ACCESS-SIGN": "value",
            "OK-ACCESS-TIMESTAMP": "value",
            "OK-ACCESS-PASSPHRASE": "value",
        },
        {
            "OK-ACCESS-KEY": "value\nforged",
            "OK-ACCESS-SIGN": "value",
            "OK-ACCESS-TIMESTAMP": "value",
            "OK-ACCESS-PASSPHRASE": "value",
        },
    ],
)
def test_private_auth_headers_are_exact_nonempty_and_adapter_owned(headers) -> None:
    instance, transport = adapter(
        [],
        credentials=HeaderCredentialProvider(headers),
    )

    with pytest.raises(OkxReadAdapterError) as exc_info:
        instance.account_config()

    assert exc_info.value.kind == "UNAUTHORIZED"
    assert exc_info.value.status == "BLOCKED"
    assert transport.calls == []


@pytest.mark.parametrize(
    ("status_code", "kind", "retryable"),
    [
        (401, "UNAUTHORIZED", False),
        (429, "RATE_LIMITED", True),
        (503, "HTTP_ERROR", True),
        (400, "HTTP_ERROR", False),
    ],
)
def test_http_errors_are_structured(status_code, kind, retryable) -> None:
    instance, _ = adapter([envelope([])], status_code=status_code)

    with pytest.raises(OkxReadAdapterError) as exc_info:
        instance.ticker("BTC-USDT-SWAP")

    assert exc_info.value.kind == kind
    assert exc_info.value.status == ("BLOCKED" if status_code == 401 else "FAILED")
    assert exc_info.value.retryable is retryable
    assert exc_info.value.http_status == status_code


@pytest.mark.parametrize(
    ("payload", "kind", "status", "retryable"),
    [
        (envelope([], code="50011"), "RATE_LIMITED", "FAILED", True),
        (envelope([], code="50113"), "UNAUTHORIZED", "BLOCKED", False),
        (envelope([], code="51000"), "BUSINESS_ERROR", "FAILED", False),
        ({"code": "0", "data": "bad"}, "INVALID_RESPONSE", "FAILED", False),
    ],
)
def test_business_and_response_errors_are_structured(
    payload,
    kind,
    status,
    retryable,
) -> None:
    instance, _ = adapter([payload])

    with pytest.raises(OkxReadAdapterError) as exc_info:
        instance.ticker("BTC-USDT-SWAP")

    assert exc_info.value.kind == kind
    assert exc_info.value.status == status
    assert exc_info.value.retryable is retryable


@pytest.mark.parametrize(
    ("failure", "kind"),
    [(TimeoutError(), "TIMEOUT"), (OSError(), "NETWORK")],
)
def test_transport_failures_are_mapped_without_exception_text(failure, kind) -> None:
    instance, _ = adapter([failure])

    with pytest.raises(OkxReadAdapterError) as exc_info:
        instance.ticker("BTC-USDT-SWAP")

    assert exc_info.value.kind == kind
    assert exc_info.value.retryable is True


def test_expired_market_data_is_blocked_not_returned_as_ready() -> None:
    instance, _ = adapter(
        [
            envelope(
                [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "last": "100",
                        "bidPx": "99",
                        "askPx": "101",
                        "open24h": "90",
                        "high24h": "110",
                        "low24h": "80",
                        "vol24h": "20",
                        "volCcy24h": "2",
                        "ts": OLD_TS,
                    }
                ]
            )
        ]
    )

    with pytest.raises(OkxReadAdapterError) as exc_info:
        instance.ticker("BTC-USDT-SWAP")

    assert exc_info.value.kind == "STALE_DATA"
    assert exc_info.value.status == "BLOCKED"


def test_empty_positions_and_balances_are_valid_authorized_snapshots() -> None:
    credentials = RecordedCredentialProvider()
    instance, _ = adapter(
        [envelope([]), envelope([])],
        credentials=credentials,
    )

    positions = instance.positions()
    balance = instance.balance()

    assert positions.items == []
    assert balance.items == []
    assert positions.status == balance.status == "READY"


def test_invalid_inputs_block_before_transport_and_no_write_surface_exists() -> None:
    instance, transport = adapter([])

    with pytest.raises(OkxReadAdapterError):
        instance.ticker("BTC-USDT")
    with pytest.raises(OkxReadAdapterError):
        instance.order("BTC-USDT-SWAP")
    with pytest.raises(OkxReadAdapterError):
        instance.order(
            "BTC-USDT-SWAP",
            order_id="123",
            client_order_id="client123",
        )

    assert transport.calls == []
    assert not hasattr(instance, "place_order")
    assert not hasattr(instance, "cancel_order")
    assert not hasattr(instance, "set_leverage")


def mismatch_cases():
    ticker = {
        "instId": "ETH-USDT-SWAP",
        "last": "100",
        "bidPx": "99",
        "askPx": "101",
        "open24h": "90",
        "high24h": "110",
        "low24h": "80",
        "vol24h": "20",
        "volCcy24h": "2",
        "ts": FRESH_TS,
    }
    return [
        (
            lambda adapter: adapter.instruments("BTC-USDT-SWAP"),
            {
                "instId": "ETH-USDT-SWAP",
                "instType": "SWAP",
                "baseCcy": "ETH",
                "quoteCcy": "USDT",
                "settleCcy": "USDT",
                "ctType": "linear",
                "ctVal": "0.1",
                "ctValCcy": "ETH",
                "lotSz": "1",
                "minSz": "1",
                "tickSz": "0.01",
                "state": "live",
            },
            False,
        ),
        (lambda adapter: adapter.ticker("BTC-USDT-SWAP"), ticker, False),
        (
            lambda adapter: adapter.mark_price("BTC-USDT-SWAP"),
            {"instId": "ETH-USDT-SWAP", "markPx": "100", "ts": FRESH_TS},
            False,
        ),
        (
            lambda adapter: adapter.index_price("BTC-USDT"),
            {"instId": "ETH-USDT", "idxPx": "100", "ts": FRESH_TS},
            False,
        ),
        (
            lambda adapter: adapter.funding_rate("BTC-USDT-SWAP"),
            {
                "instId": "ETH-USDT-SWAP",
                "fundingRate": "0.0001",
                "fundingTime": FRESH_TS,
            },
            False,
        ),
        (
            lambda adapter: adapter.open_interest("BTC-USDT-SWAP"),
            {
                "instId": "ETH-USDT-SWAP",
                "oi": "100",
                "oiCcy": "10",
                "ts": FRESH_TS,
            },
            False,
        ),
        (
            lambda adapter: adapter.positions("BTC-USDT-SWAP"),
            {
                "instId": "ETH-USDT-SWAP",
                "mgnMode": "isolated",
                "posSide": "net",
                "pos": "0",
                "availPos": "0",
                "uTime": FRESH_TS,
            },
            True,
        ),
        (
            lambda adapter: adapter.leverage("BTC-USDT-SWAP"),
            {
                "instId": "ETH-USDT-SWAP",
                "mgnMode": "isolated",
                "posSide": "net",
                "lever": "3",
            },
            True,
        ),
        (
            lambda adapter: adapter.fees("BTC-USDT-SWAP"),
            {
                "instId": "ETH-USDT-SWAP",
                "maker": "-0.0002",
                "taker": "0.0005",
            },
            True,
        ),
        (
            lambda adapter: adapter.order("BTC-USDT-SWAP", order_id="123"),
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "999",
                "clOrdId": "client999",
                "state": "live",
                "side": "buy",
                "posSide": "net",
                "ordType": "limit",
                "px": "90",
                "sz": "2",
                "accFillSz": "0",
                "cTime": FRESH_TS,
                "uTime": FRESH_TS,
            },
            True,
        ),
        (
            lambda adapter: adapter.order(
                "BTC-USDT-SWAP",
                client_order_id="client123",
            ),
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "123",
                "clOrdId": "client999",
                "state": "live",
                "side": "buy",
                "posSide": "net",
                "ordType": "limit",
                "px": "90",
                "sz": "2",
                "accFillSz": "0",
                "cTime": FRESH_TS,
                "uTime": FRESH_TS,
            },
            True,
        ),
    ]


@pytest.mark.parametrize(
    ("caller", "response_item", "authenticated"),
    mismatch_cases(),
)
def test_exact_queries_reject_mismatched_response_identity(
    caller,
    response_item,
    authenticated,
) -> None:
    credentials = RecordedCredentialProvider() if authenticated else None
    instance, transport = adapter(
        [envelope([response_item])],
        credentials=credentials,
    )

    with pytest.raises(OkxReadAdapterError) as exc_info:
        caller(instance)

    assert exc_info.value.kind == "INVALID_RESPONSE"
    assert exc_info.value.status == "FAILED"
    assert len(transport.calls) == 1
