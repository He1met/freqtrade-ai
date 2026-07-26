from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
import json
from typing import Mapping, Optional

import pytest

from app.adapters.okx_demo import (
    InstrumentSpec,
    OkxDemoCredentialsUnavailable,
    OkxDemoReadAdapter,
    OkxReadAdapterError,
    OkxReadHttpResponse,
    UrllibOkxReadTransport,
    create_attested_okx_demo_read_adapter,
)
from app.adapters.okx_demo import credential_preflight as preflight
from app.adapters.okx_demo import credentials as credential_boundary
from app.adapters.okx_demo import read_adapter as read_boundary
from app.adapters.okx_demo.write_transport import (
    UrllibOkxDemoWriteTransport,
    _create_attested_writer_credential_bridge,
    _create_production_write_transport,
)


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
FRESH_TS = str(int((NOW - timedelta(seconds=1)).timestamp() * 1000))
OLD_TS = str(int((NOW - timedelta(hours=1)).timestamp() * 1000))


class RecordedTransport:
    def __init__(
        self,
        payloads=None,
        *,
        status_code: int = 200,
        received_at: datetime = NOW,
    ) -> None:
        self.payloads = list(payloads or [])
        self.status_code = status_code
        self.received_at = received_at
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
            received_at=self.received_at,
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
    responses = [
        payload
        if isinstance(payload, BaseException)
        else OkxReadHttpResponse(
            status_code=status_code,
            payload=payload,
            received_at=NOW,
        )
        for payload in payloads
    ]
    instance = OkxDemoReadAdapter(
        execution_target="OKX_DEMO",
        recorded_responses=responses,
        credential_provider=credentials,
        now_provider=lambda: NOW,
        ttl_seconds=ttl_seconds,
    )
    return instance, instance._transport


def envelope(data, code="0"):
    return {"code": code, "msg": "", "data": data}


def attested_account(uid: str = "demo-account-a") -> dict[str, str]:
    return {
        "uid": uid,
        "mainUid": "demo-main-account",
        "acctLv": "2",
        "posMode": "net_mode",
        "perm": "read_only,trade",
    }


class CredentialAttestationResponse:
    status = 200

    def __init__(self, account: Mapping[str, str]) -> None:
        self._payload = json.dumps(envelope([dict(account)])).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._payload


def attestation_opener(account: Mapping[str, str]):
    return lambda request, timeout: CredentialAttestationResponse(account)


def install_attestation(monkeypatch, account: Mapping[str, str]) -> None:
    monkeypatch.setattr(
        read_boundary,
        "run_preflight",
        lambda environment: preflight.run_preflight(
            environment,
            opener=attestation_opener(account),
        ),
    )


def ephemeral_environment(
    account: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    account = account or attested_account()
    return {
        preflight.EXECUTION_TARGET_ENV: "OKX_DEMO",
        preflight.ALLOW_REAL_FUNDS_ENV: "false",
        preflight.REST_URL_ENV: preflight.OKX_DEMO_REST_URL,
        "OKX_DEMO_API_KEY": "temporary-api-key",
        "OKX_DEMO_API_SECRET": "temporary-api-secret",
        "OKX_DEMO_API_PASSPHRASE": "temporary-passphrase",
        preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV: preflight.account_fingerprint(
            account
        ),
        "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY": "74" * 32,
    }


def test_attested_provider_reuses_443_get_only_signature_boundary(
    monkeypatch,
) -> None:
    account = attested_account()
    environment = ephemeral_environment(account)
    install_attestation(monkeypatch, account)
    monkeypatch.setattr(
        preflight,
        "_timestamp",
        lambda: "2026-07-27T01:02:03.004Z",
    )
    monkeypatch.setattr(read_boundary, "_utc_now", lambda: NOW)
    target_transport = RecordedTransport(
        [envelope([{"uTime": FRESH_TS, "totalEq": "1", "details": []}])]
    )
    monkeypatch.setattr(
        read_boundary,
        "UrllibOkxReadTransport",
        lambda: target_transport,
    )
    instance = create_attested_okx_demo_read_adapter(environment)
    environment["OKX_DEMO_API_SECRET"] = "swapped-after-attestation"
    instance.balance("USDT")
    headers = target_transport.calls[0]["headers"]

    auth_headers = {
        name: headers[name]
        for name in (
            "OK-ACCESS-KEY",
            "OK-ACCESS-SIGN",
            "OK-ACCESS-TIMESTAMP",
            "OK-ACCESS-PASSPHRASE",
        )
    }
    assert set(auth_headers) == {
        "OK-ACCESS-KEY",
        "OK-ACCESS-SIGN",
        "OK-ACCESS-TIMESTAMP",
        "OK-ACCESS-PASSPHRASE",
    }
    assert auth_headers["OK-ACCESS-KEY"] == "temporary-api-key"
    assert auth_headers["OK-ACCESS-PASSPHRASE"] == "temporary-passphrase"
    assert auth_headers["OK-ACCESS-TIMESTAMP"] == "2026-07-27T01:02:03.004Z"
    assert (
        auth_headers["OK-ACCESS-SIGN"]
        == "0PFbuXrPz3ectz3yPA0AU6UgyJn7YQwrxkz6ZW7DhCs="
    )
    assert headers["x-simulated-trading"] == "1"


def test_real_attested_factory_result_builds_writer_bridge(monkeypatch) -> None:
    account = attested_account()
    environment = ephemeral_environment(account)
    install_attestation(monkeypatch, account)
    monkeypatch.setattr(read_boundary, "_utc_now", lambda: NOW)

    read_client = create_attested_okx_demo_read_adapter(environment)
    bridge = _create_attested_writer_credential_bridge(read_client)
    transport = _create_production_write_transport(bridge)

    assert type(transport) is UrllibOkxDemoWriteTransport


@pytest.mark.parametrize(
    "mutation",
    [
        lambda environment: environment.pop(
            preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV
        ),
        lambda environment: environment.update(
            {preflight.EXECUTION_TARGET_ENV: "OKX_LIVE"}
        ),
        lambda environment: environment.update(
            {preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV: "not-a-sha256"}
        ),
        lambda environment: environment.pop(
            "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY"
        ),
        lambda environment: environment.update(
            {"FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY": "not-a-key"}
        ),
    ],
)
def test_attested_provider_factory_fails_closed_without_leaking_bundle(
    monkeypatch,
    mutation,
) -> None:
    account = attested_account()
    environment = ephemeral_environment()
    mutation(environment)
    install_attestation(monkeypatch, account)

    with pytest.raises(OkxDemoCredentialsUnavailable) as blocked:
        create_attested_okx_demo_read_adapter(environment)

    rendered = str(blocked.value)
    assert "temporary-api-key" not in rendered
    assert "temporary-api-secret" not in rendered
    assert "temporary-passphrase" not in rendered


def test_wrong_account_credentials_block_before_target_private_read(
    monkeypatch,
) -> None:
    expected_account = attested_account("demo-account-a")
    wrong_account = attested_account("demo-account-b")
    target_transport = RecordedTransport(
        [envelope([{"uTime": FRESH_TS, "totalEq": "1", "details": []}])]
    )
    install_attestation(monkeypatch, wrong_account)

    with pytest.raises(OkxDemoCredentialsUnavailable):
        create_attested_okx_demo_read_adapter(
            ephemeral_environment(expected_account)
        )

    assert target_transport.calls == []


def test_attested_session_expires_before_private_transport(monkeypatch) -> None:
    account = attested_account()
    current = [NOW]
    target_transport = RecordedTransport(
        [envelope([{"uTime": FRESH_TS, "totalEq": "1", "details": []}])]
    )
    install_attestation(monkeypatch, account)
    monkeypatch.setattr(read_boundary, "_utc_now", lambda: current[0])
    monkeypatch.setattr(
        read_boundary,
        "UrllibOkxReadTransport",
        lambda: target_transport,
    )
    instance = create_attested_okx_demo_read_adapter(
        ephemeral_environment(account)
    )
    current[0] = NOW + timedelta(seconds=61)

    with pytest.raises(OkxReadAdapterError) as blocked:
        instance.balance("USDT")

    assert blocked.value.kind == "UNAUTHORIZED"
    assert blocked.value.status == "BLOCKED"
    assert target_transport.calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda account: account.update(uid="another-account"),
        lambda account: account.update(perm="read_only"),
    ],
)
def test_account_config_revalidates_identity_and_permissions(
    monkeypatch,
    mutation,
) -> None:
    account = attested_account()
    drifted = dict(account)
    mutation(drifted)
    target_transport = RecordedTransport([envelope([drifted])])
    install_attestation(monkeypatch, account)
    monkeypatch.setattr(read_boundary, "_utc_now", lambda: NOW)
    monkeypatch.setattr(
        read_boundary,
        "UrllibOkxReadTransport",
        lambda: target_transport,
    )
    instance = create_attested_okx_demo_read_adapter(
        ephemeral_environment(account)
    )

    with pytest.raises(OkxReadAdapterError) as blocked:
        instance.account_config()

    assert blocked.value.kind == "IDENTITY_DRIFT"
    assert blocked.value.status == "BLOCKED"
    assert len(target_transport.calls) == 1
    with pytest.raises(OkxReadAdapterError) as revoked:
        instance.balance("USDT")
    assert revoked.value.kind == "UNAUTHORIZED"
    assert revoked.value.status == "BLOCKED"
    assert len(target_transport.calls) == 1


def test_common_import_and_constructor_paths_cannot_create_real_session() -> None:
    assert not hasattr(credential_boundary, "_ATTESTED_PROVIDER_CAPABILITY")
    assert not hasattr(credential_boundary, "_AttestedOkxDemoCredentialProvider")
    assert tuple(
        inspect.signature(create_attested_okx_demo_read_adapter).parameters
    ) == ("environment",)
    class WrappedRealTransport:
        def __init__(self) -> None:
            self.inner = UrllibOkxReadTransport()

    with pytest.raises(TypeError):
        OkxDemoReadAdapter(
            execution_target="OKX_DEMO",
            transport=WrappedRealTransport(),
            credential_provider=RecordedCredentialProvider(),
        )


def test_adapter_rejects_every_target_except_okx_demo() -> None:
    with pytest.raises(OkxReadAdapterError) as exc_info:
        OkxDemoReadAdapter(execution_target="OKX_LIVE")

    assert exc_info.value.kind == "UNSAFE_TARGET"
    assert exc_info.value.status == "BLOCKED"


def test_normalizer_constructor_has_no_transport_injection_parameter() -> None:
    with pytest.raises(TypeError):
        OkxDemoReadAdapter(
            execution_target="OKX_DEMO",
            transport=UrllibOkxReadTransport(),
            credential_provider=RecordedCredentialProvider(),
        )


def test_real_transport_rejects_an_alternate_credential_origin() -> None:
    with pytest.raises(OkxReadAdapterError) as exc_info:
        UrllibOkxReadTransport(base_url="https://example.invalid")

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
                        "uly": "BTC-USDT",
                        "instFamily": "BTC-USDT",
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


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update(ctValCcy="USDT"),
        lambda item: item.update(
            instId="BTC-USD-SWAP",
            uly="BTC-USD",
            instFamily="BTC-USD",
            quoteCcy="USD",
            settleCcy="USD",
            ctType="inverse",
            ctValCcy="USD",
        ),
    ],
)
def test_inconsistent_linear_or_inverse_contract_metadata_blocks(
    mutation,
) -> None:
    item = {
        "instId": "BTC-USDT-SWAP",
        "instType": "SWAP",
        "uly": "BTC-USDT",
        "instFamily": "BTC-USDT",
        "baseCcy": "BTC",
        "quoteCcy": "USDT",
        "settleCcy": "USDT",
        "ctType": "linear",
        "ctVal": "0.01",
        "ctValCcy": "BTC",
        "lotSz": "1",
        "minSz": "1",
        "tickSz": "0.1",
        "state": "live",
    }
    mutation(item)
    instance, _ = adapter([envelope([item])])

    with pytest.raises(OkxReadAdapterError) as blocked:
        instance.instruments(item["instId"])

    assert blocked.value.kind == "INVALID_RESPONSE"
    assert blocked.value.status == "BLOCKED"


@pytest.mark.parametrize(
    (
        "inst_id",
        "contract_type",
        "settle_ccy",
        "contract_value_ccy",
        "expected_base",
        "expected_quote",
    ),
    [
        ("BTC-USDT-SWAP", "linear", "USDT", "BTC", "BTC", "USDT"),
        ("BTC-USD-SWAP", "inverse", "BTC", "USD", "BTC", "USD"),
    ],
)
def test_real_okx_empty_base_quote_are_derived_from_swap_identity(
    inst_id: str,
    contract_type: str,
    settle_ccy: str,
    contract_value_ccy: str,
    expected_base: str,
    expected_quote: str,
) -> None:
    family = inst_id.removesuffix("-SWAP")
    item = {
        "instId": inst_id,
        "instType": "SWAP",
        "uly": family,
        "instFamily": family,
        "baseCcy": "",
        "quoteCcy": "",
        "settleCcy": settle_ccy,
        "ctType": contract_type,
        "ctVal": "1",
        "ctValCcy": contract_value_ccy,
        "lotSz": "1",
        "minSz": "1",
        "tickSz": "0.1",
        "state": "live",
    }
    instance, _ = adapter([envelope([item])])

    snapshot = instance.instruments(inst_id)

    assert snapshot.items[0]["base_ccy"] == expected_base
    assert snapshot.items[0]["quote_ccy"] == expected_quote


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


def test_future_exchange_timestamp_is_blocked_as_invalid_response() -> None:
    future_ts = str(int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp() * 1000))
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
                        "ts": future_ts,
                    }
                ]
            )
        ]
    )

    with pytest.raises(OkxReadAdapterError) as blocked:
        instance.ticker("BTC-USDT-SWAP")

    assert blocked.value.kind == "INVALID_RESPONSE"
    assert blocked.value.status == "BLOCKED"


def test_funding_schedule_uses_received_at_for_freshness() -> None:
    planned = str(int(NOW.timestamp() * 1000) + 8 * 60 * 60 * 1000)
    instance, _ = adapter(
        [
            envelope(
                [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "fundingRate": "0.0001",
                        "fundingTime": planned,
                    }
                ]
            )
        ]
    )

    snapshot = instance.funding_rate("BTC-USDT-SWAP")

    assert snapshot.metadata.exchange_timestamp is None
    assert snapshot.metadata.expires_at == NOW + timedelta(seconds=300)


@pytest.mark.parametrize(
    ("funding_time", "next_funding_time"),
    [
        (
            str(int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)),
            None,
        ),
        (
            str(int(NOW.timestamp() * 1000) + 8 * 60 * 60 * 1000),
            str(int(NOW.timestamp() * 1000) + 60 * 60 * 1000),
        ),
    ],
)
def test_impossible_funding_schedule_is_blocked(
    funding_time: str,
    next_funding_time: Optional[str],
) -> None:
    item = {
        "instId": "BTC-USDT-SWAP",
        "fundingRate": "0.0001",
        "fundingTime": funding_time,
    }
    if next_funding_time is not None:
        item["nextFundingTime"] = next_funding_time
    instance, _ = adapter([envelope([item])])

    with pytest.raises(OkxReadAdapterError) as blocked:
        instance.funding_rate("BTC-USDT-SWAP")

    assert blocked.value.kind == "INVALID_RESPONSE"
    assert blocked.value.status == "BLOCKED"


@pytest.mark.parametrize("naive_source", ["received_at", "now"])
def test_freshness_requires_timezone_aware_received_at_and_now(
    naive_source: str,
) -> None:
    payload = envelope([])
    transport = RecordedTransport(
        [payload],
        received_at=NOW.replace(tzinfo=None)
        if naive_source == "received_at"
        else NOW,
    )
    instance = OkxDemoReadAdapter(
        execution_target="OKX_DEMO",
        recorded_responses=[
            OkxReadHttpResponse(
                status_code=200,
                payload=payload,
                received_at=transport.received_at,
            )
        ],
        credential_provider=RecordedCredentialProvider(),
        now_provider=(
            (lambda: NOW.replace(tzinfo=None))
            if naive_source == "now"
            else (lambda: NOW)
        ),
    )

    with pytest.raises(OkxReadAdapterError) as blocked:
        instance.positions()

    assert blocked.value.status == "BLOCKED"


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
                "uly": "ETH-USDT",
                "instFamily": "ETH-USDT",
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
