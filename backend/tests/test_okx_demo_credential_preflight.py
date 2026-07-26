import json

import pytest

from app.adapters.okx_demo import credential_preflight as preflight


def valid_environment() -> dict[str, str]:
    return {
        preflight.EXECUTION_TARGET_ENV: "OKX_DEMO",
        preflight.ALLOW_REAL_FUNDS_ENV: "false",
        preflight.REST_URL_ENV: preflight.OKX_DEMO_REST_URL,
        "OKX_DEMO_API_KEY": "test-api-key",
        "OKX_DEMO_API_SECRET": "test-api-secret",
        "OKX_DEMO_API_PASSPHRASE": "test-passphrase",
        preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV: preflight.account_fingerprint(
            valid_payload()["data"][0]
        ),
    }


def valid_payload() -> dict:
    return {
        "code": "0",
        "msg": "",
        "data": [
            {
                "uid": "must-not-be-rendered",
                "mainUid": "also-private",
                "acctLv": "2",
                "posMode": "net_mode",
                "perm": "read_only,trade",
            }
        ],
    }


def test_request_is_signed_for_fixed_demo_account_config_contract() -> None:
    request = preflight.build_account_config_request(
        valid_environment(),
        timestamp="2026-07-27T01:02:03.004Z",
    )

    assert request.full_url == "https://openapi.okx.com/api/v5/account/config"
    assert request.method == "GET"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["x-simulated-trading"] == "1"
    assert headers["ok-access-key"] == "test-api-key"
    assert headers["ok-access-passphrase"] == "test-passphrase"
    assert headers["ok-access-timestamp"] == "2026-07-27T01:02:03.004Z"
    assert headers["ok-access-sign"]


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (preflight.EXECUTION_TARGET_ENV, "OKX_LIVE", "sole OKX_DEMO"),
        (preflight.ALLOW_REAL_FUNDS_ENV, "true", "real-fund"),
        (preflight.REST_URL_ENV, "https://example.invalid", "URL"),
        ("OKX_DEMO_API_PASSPHRASE", "", "bundle is incomplete"),
    ],
)
def test_request_contract_fails_closed(name: str, value: str, message: str) -> None:
    environment = valid_environment()
    environment[name] = value

    with pytest.raises(preflight.OkxDemoPreflightBlocked, match=message):
        preflight.build_account_config_request(environment)


def test_valid_demo_identity_and_minimum_permissions_are_redacted() -> None:
    expected = preflight.account_fingerprint(valid_payload()["data"][0])
    result = preflight.validate_account_config(
        valid_payload(),
        expected_fingerprint=expected,
    )

    assert result["status"] == "READY"
    assert result["execution_target"] == "OKX_DEMO"
    assert result["remote_account_evidence"]["fingerprint_match"] is True
    assert result["remote_account_evidence"]["permissions"] == {
        "read": True,
        "trade": True,
        "withdraw": False,
    }
    assert result["local_target_contract"] == {
        "product_type": "SWAP",
        "margin_mode": "isolated",
        "allow_real_funds": False,
    }
    rendered = json.dumps(result)
    assert "must-not-be-rendered" not in rendered
    assert "mainUid" not in rendered


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update(code="1"), "attestation failed"),
        (lambda payload: payload.update(data=[]), "identity is unknown"),
        (lambda payload: payload["data"][0].update(uid=""), "identity is unknown"),
        (
            lambda payload: payload["data"][0].update(perm="read_only"),
            "exactly read_only and trade",
        ),
        (
            lambda payload: payload["data"][0].update(
                perm="read_only,trade,withdraw"
            ),
            "exactly read_only and trade",
        ),
        (
            lambda payload: payload["data"][0].update(posMode="long_short_mode"),
            "net_mode",
        ),
        (
            lambda payload: payload["data"][0].update(acctLv="3"),
            "Futures mode",
        ),
    ],
)
def test_account_attestation_rejects_unknown_or_unsafe_contract(
    mutation,
    message: str,
) -> None:
    payload = valid_payload()
    mutation(payload)
    try:
        expected = preflight.account_fingerprint(payload["data"][0])
    except (IndexError, preflight.OkxDemoPreflightBlocked):
        expected = preflight.account_fingerprint(valid_payload()["data"][0])

    with pytest.raises(preflight.OkxDemoPreflightBlocked, match=message):
        preflight.validate_account_config(
            payload,
            expected_fingerprint=expected,
        )


@pytest.mark.parametrize("expected", ["", "not-a-sha256", "f" * 64])
def test_account_attestation_requires_matching_pinned_fingerprint(expected: str) -> None:
    with pytest.raises(
        preflight.OkxDemoPreflightBlocked,
        match="fingerprint",
    ):
        preflight.validate_account_config(
            valid_payload(),
            expected_fingerprint=expected,
        )


class FakeResponse:
    status = 200

    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


def test_run_preflight_never_returns_raw_account_identifiers() -> None:
    payload = json.dumps(valid_payload()).encode()

    result = preflight.run_preflight(
        valid_environment(),
        opener=lambda request, timeout: FakeResponse(payload),
    )

    assert result["remote_account_evidence"]["fingerprint_match"] is True
    assert "must-not-be-rendered" not in json.dumps(result)


def test_run_preflight_does_not_call_network_without_pinned_fingerprint() -> None:
    environment = valid_environment()
    environment.pop(preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV)

    with pytest.raises(
        preflight.OkxDemoPreflightBlocked,
        match="bundle is incomplete",
    ):
        preflight.run_preflight(
            environment,
            opener=lambda *_args, **_kwargs: pytest.fail(
                "network must not run without identity pinning"
            ),
        )


def test_transport_and_invalid_json_fail_without_echoing_remote_content() -> None:
    with pytest.raises(
        preflight.OkxDemoPreflightBlocked,
        match="invalid JSON",
    ) as invalid:
        preflight.run_preflight(
            valid_environment(),
            opener=lambda request, timeout: FakeResponse(b"remote-secret-content"),
        )

    assert "remote-secret-content" not in str(invalid.value)
