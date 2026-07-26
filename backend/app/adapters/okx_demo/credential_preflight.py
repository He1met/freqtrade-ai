from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


EXECUTION_TARGET_ENV = "FREQTRADE_AI_EXECUTION_TARGET"
ALLOW_REAL_FUNDS_ENV = "FREQTRADE_AI_ALLOW_REAL_FUNDS"
REST_URL_ENV = "FREQTRADE_AI_OKX_DEMO_REST_URL"
OKX_DEMO_REST_URL = "https://openapi.okx.com"
ACCOUNT_CONFIG_PATH = "/api/v5/account/config"
SIMULATED_TRADING_HEADER = ("x-simulated-trading", "1")
OKX_DEMO_CREDENTIAL_ENV_NAMES = (
    "OKX_DEMO_API_KEY",
    "OKX_DEMO_API_SECRET",
    "OKX_DEMO_API_PASSPHRASE",
)
OKX_DEMO_ACCOUNT_FINGERPRINT_ENV = "OKX_DEMO_ACCOUNT_FINGERPRINT"
OKX_DEMO_REQUIRED_ENV_NAMES = (
    *OKX_DEMO_CREDENTIAL_ENV_NAMES,
    OKX_DEMO_ACCOUNT_FINGERPRINT_ENV,
)
REQUEST_TIMEOUT_SECONDS = 10


class OkxDemoPreflightBlocked(RuntimeError):
    """The credential cannot be attested as the sole safe Demo target."""


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if (
        not value
        or len(value) > 16384
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise OkxDemoPreflightBlocked("OKX Demo credential bundle is incomplete")
    return value


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _signature(secret: str, timestamp: str) -> str:
    message = "{}GET{}".format(timestamp, ACCOUNT_CONFIG_PATH)
    digest = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def account_fingerprint(account: Mapping[str, Any]) -> str:
    identity = {
        "uid": account.get("uid"),
        "mainUid": account.get("mainUid"),
        "acctLv": account.get("acctLv"),
        "posMode": account.get("posMode"),
    }
    if not all(isinstance(value, str) and value.strip() for value in identity.values()):
        raise OkxDemoPreflightBlocked("OKX Demo account identity is unknown")
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_account_config_request(
    environment: Mapping[str, str],
    *,
    timestamp: Optional[str] = None,
) -> Request:
    if environment.get(EXECUTION_TARGET_ENV) != "OKX_DEMO":
        raise OkxDemoPreflightBlocked("execution target is not the sole OKX_DEMO target")
    if environment.get(ALLOW_REAL_FUNDS_ENV) != "false":
        raise OkxDemoPreflightBlocked("real-fund access is not explicitly disabled")
    if environment.get(REST_URL_ENV) != OKX_DEMO_REST_URL:
        raise OkxDemoPreflightBlocked("OKX Demo REST URL is missing or unknown")

    credential_parts = tuple(
        _required_environment(environment, name)
        for name in OKX_DEMO_CREDENTIAL_ENV_NAMES
    )
    request_timestamp = timestamp or _timestamp()
    return Request(
        OKX_DEMO_REST_URL + ACCOUNT_CONFIG_PATH,
        method="GET",
        headers=dict(
            (
                ("Accept", "application/json"),
                ("OK-ACCESS-KEY", credential_parts[0]),
                (
                    "OK-ACCESS-SIGN",
                    _signature(credential_parts[1], request_timestamp),
                ),
                ("OK-ACCESS-TIMESTAMP", request_timestamp),
                ("OK-ACCESS-PASSPHRASE", credential_parts[2]),
                SIMULATED_TRADING_HEADER,
            )
        ),
    )


def validate_account_config(
    payload: Any,
    *,
    expected_fingerprint: str,
) -> Dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise OkxDemoPreflightBlocked("OKX Demo account attestation failed")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise OkxDemoPreflightBlocked("OKX Demo account identity is unknown")

    account = data[0]
    if (
        len(expected_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in expected_fingerprint)
    ):
        raise OkxDemoPreflightBlocked("OKX Demo account fingerprint is missing or invalid")
    observed_fingerprint = account_fingerprint(account)
    if not hmac.compare_digest(observed_fingerprint, expected_fingerprint):
        raise OkxDemoPreflightBlocked("OKX Demo account fingerprint does not match")
    permissions = account.get("perm")
    if not isinstance(permissions, str):
        raise OkxDemoPreflightBlocked("OKX Demo API permissions are unknown")
    permission_set = {
        permission.strip().lower()
        for permission in permissions.split(",")
        if permission.strip()
    }
    if permission_set != {"read_only", "trade"}:
        raise OkxDemoPreflightBlocked(
            "OKX Demo API permissions must be exactly read_only and trade"
        )
    if account.get("posMode") != "net_mode":
        raise OkxDemoPreflightBlocked("OKX Demo position mode must be net_mode")
    if account.get("acctLv") != "2":
        raise OkxDemoPreflightBlocked("OKX Demo account level must be Futures mode")

    return {
        "status": "READY",
        "execution_target": "OKX_DEMO",
        "remote_account_evidence": {
            "authenticated_demo_response": True,
            "identity_present": True,
            "fingerprint_match": True,
            "permissions": {
                "read": True,
                "trade": True,
                "withdraw": False,
            },
            "account_level": "2",
            "position_mode": "net_mode",
        },
        "local_target_contract": {
            "product_type": "SWAP",
            "margin_mode": "isolated",
            "allow_real_funds": False,
        },
        "request_contract": {
            "method": "GET",
            "path": ACCOUNT_CONFIG_PATH,
            "simulated_trading_header": True,
        },
    }


def run_preflight(
    environment: Optional[Mapping[str, str]] = None,
    *,
    opener: Callable[..., Any] = urlopen,
) -> Dict[str, Any]:
    active_environment = os.environ if environment is None else environment
    request = build_account_config_request(active_environment)
    expected_fingerprint = _required_environment(
        active_environment,
        OKX_DEMO_ACCOUNT_FINGERPRINT_ENV,
    )
    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status < 200 or response.status >= 300:
                raise OkxDemoPreflightBlocked(
                    "OKX Demo account attestation transport failed"
                )
            raw_payload = response.read()
    except (HTTPError, URLError, TimeoutError, OSError):
        raise OkxDemoPreflightBlocked(
            "OKX Demo account attestation transport failed"
        ) from None
    try:
        payload = json.loads(raw_payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise OkxDemoPreflightBlocked(
            "OKX Demo account attestation returned invalid JSON"
        ) from None
    return validate_account_config(
        payload,
        expected_fingerprint=expected_fingerprint,
    )


def main() -> int:
    try:
        payload = run_preflight()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except OkxDemoPreflightBlocked as exc:
        print(
            json.dumps(
                {"status": "BLOCKED", "reason": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
