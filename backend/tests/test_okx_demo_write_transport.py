import json

import pytest

import app.adapters.okx_demo as okx_demo_boundary
from app.adapters.okx_demo.credentials import OkxDemoCredentialsUnavailable
from app.adapters.okx_demo.write_semantics import (
    OkxDemoTransportError,
    OkxDemoWriteBlocked,
)
from app.adapters.okx_demo.write_transport import (
    OfflineOkxDemoWriteTransportHarness,
    UrllibOkxDemoWriteTransport,
    _create_production_write_transport,
)


class Provider:
    def __init__(self):
        self.calls = []

    def authorization_headers(self, *, method, request_path, body):
        self.calls.append((method, request_path, body))
        return {
            "OK-ACCESS-KEY": "ephemeral-key",
            "OK-ACCESS-SIGN": "ephemeral-signature",
            "OK-ACCESS-TIMESTAMP": "2026-07-27T00:00:00.000Z",
            "OK-ACCESS-PASSPHRASE": "ephemeral-passphrase",
        }


class Response:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self._payload


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def test_write_transport_is_fixed_to_demo_and_signs_canonical_body() -> None:
    provider = Provider()
    opener = Opener(Response(b'{"code":"0","data":[]}'))
    transport = OfflineOkxDemoWriteTransportHarness(provider, opener=opener)

    payload = transport.post(
        path="/api/v5/trade/order",
        body={"sz": "1", "instId": "BTC-USDT-SWAP"},
    )

    request, timeout = opener.calls[0]
    canonical_body = '{"instId":"BTC-USDT-SWAP","sz":"1"}'
    assert provider.calls == [("POST", "/api/v5/trade/order", canonical_body)]
    assert request.full_url == "https://offline.invalid/api/v5/trade/order"
    assert request.data == canonical_body.encode()
    assert request.get_header("X-simulated-trading") == "1"
    assert timeout == 10.0
    assert payload == {"code": "0", "data": []}


def test_set_leverage_uses_the_same_attested_demo_boundary() -> None:
    provider = Provider()
    opener = Opener(Response(b'{"code":"0","data":[]}'))
    transport = OfflineOkxDemoWriteTransportHarness(provider, opener=opener)

    transport.post(
        path="/api/v5/account/set-leverage",
        body={
            "instId": "BTC-USDT-SWAP",
            "lever": "3",
            "mgnMode": "isolated",
            "posSide": "net",
        },
    )

    request, _timeout = opener.calls[0]
    assert request.full_url == (
        "https://offline.invalid/api/v5/account/set-leverage"
    )
    assert request.get_header("X-simulated-trading") == "1"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v5/account/set-position-mode",
        "/api/v5/trade/order-algo",
        "https://example.invalid/api/v5/trade/order",
    ],
)
def test_write_transport_rejects_nonallowlisted_paths_before_credentials(path) -> None:
    provider = Provider()
    transport = OfflineOkxDemoWriteTransportHarness(
        provider,
        opener=Opener(Response(b"{}")),
    )

    with pytest.raises(OkxDemoWriteBlocked, match="allowlisted"):
        transport.post(path=path, body={"a": "b"})

    assert provider.calls == []


def test_offline_transport_rejects_demo_live_or_alternate_origins() -> None:
    with pytest.raises(OkxDemoWriteBlocked, match="non-routable"):
        OfflineOkxDemoWriteTransportHarness(
            Provider(),
            opener=Opener(Response(b"{}")),
            base_url="https://www.okx.com",
        )
    with pytest.raises(OkxDemoWriteBlocked, match="non-routable"):
        OfflineOkxDemoWriteTransportHarness(
            Provider(),
            opener=Opener(Response(b"{}")),
            base_url="https://openapi.okx.com",
        )


@pytest.mark.parametrize("raw_payload", [b'{"code":"0"', b"\xff"])
def test_invalid_write_response_is_always_unknown(raw_payload) -> None:
    transport = OfflineOkxDemoWriteTransportHarness(
        Provider(),
        opener=Opener(Response(raw_payload)),
    )

    with pytest.raises(OkxDemoTransportError) as captured:
        transport.post(path="/api/v5/trade/order", body={"a": "b"})

    assert captured.value.unknown_write_outcome is True


def test_unavailable_or_malformed_attestation_is_zero_network() -> None:
    class Unavailable:
        def authorization_headers(self, **_kwargs):
            raise OkxDemoCredentialsUnavailable("unavailable")

    opener = Opener(Response(b"{}"))
    transport = OfflineOkxDemoWriteTransportHarness(Unavailable(), opener=opener)

    with pytest.raises(OkxDemoWriteBlocked, match="attested"):
        transport.post(path="/api/v5/trade/order", body={"a": "b"})

    assert opener.calls == []

    class Malformed:
        def authorization_headers(self, **_kwargs):
            return {"OK-ACCESS-KEY": "only-one-header"}

    transport = OfflineOkxDemoWriteTransportHarness(Malformed(), opener=opener)
    with pytest.raises(OkxDemoWriteBlocked, match="invalid authorization"):
        transport.post(path="/api/v5/trade/order", body={"a": "b"})
    assert opener.calls == []


def test_canonical_json_rejects_nonfinite_numbers_before_credentials() -> None:
    provider = Provider()
    transport = OfflineOkxDemoWriteTransportHarness(
        provider,
        opener=Opener(Response(b"{}")),
    )

    with pytest.raises(OkxDemoWriteBlocked, match="canonical JSON"):
        transport.post(path="/api/v5/trade/order", body={"px": float("nan")})

    assert provider.calls == []


def test_production_transport_rejects_arbitrary_provider_construction() -> None:
    with pytest.raises(OkxDemoWriteBlocked, match="server factory"):
        UrllibOkxDemoWriteTransport(Provider(), _capability=object())
    with pytest.raises(OkxDemoWriteBlocked, match="attested session"):
        _create_production_write_transport(Provider())


def test_offline_transport_harness_is_not_exported_by_adapter_package() -> None:
    assert not hasattr(
        okx_demo_boundary,
        "OfflineOkxDemoWriteTransportHarness",
    )
