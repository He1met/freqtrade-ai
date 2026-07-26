from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import pwd
import subprocess
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
OKX_DEMO_ACCOUNT_FINGERPRINT_KEYCHAIN_SERVICE = (
    "freqtrade-ai/okx-demo-account-fingerprint"
)
OKX_DEMO_REQUIRED_ENV_NAMES = (
    *OKX_DEMO_CREDENTIAL_ENV_NAMES,
    OKX_DEMO_ACCOUNT_FINGERPRINT_ENV,
)
REQUEST_TIMEOUT_SECONDS = 10
KEYCHAIN_TIMEOUT_SECONDS = 5


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


def _signature(
    secret: str,
    timestamp: str,
    *,
    method: str,
    request_path: str,
    body: str,
) -> str:
    message = "{}{}{}{}".format(timestamp, method, request_path, body)
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


def _build_demo_authorization_headers(
    environment: Mapping[str, str],
    *,
    method: str,
    request_path: str,
    body: str,
    timestamp: Optional[str] = None,
) -> Dict[str, str]:
    """Sign one GET-only OKX Demo request inside the credential-bearing child."""

    if environment.get(EXECUTION_TARGET_ENV) != "OKX_DEMO":
        raise OkxDemoPreflightBlocked("execution target is not the sole OKX_DEMO target")
    if environment.get(ALLOW_REAL_FUNDS_ENV) != "false":
        raise OkxDemoPreflightBlocked("real-fund access is not explicitly disabled")
    if environment.get(REST_URL_ENV) != OKX_DEMO_REST_URL:
        raise OkxDemoPreflightBlocked("OKX Demo REST URL is missing or unknown")
    if method != "GET" or body != "":
        raise OkxDemoPreflightBlocked("OKX Demo credential boundary permits GET only")
    if (
        not isinstance(request_path, str)
        or not request_path.startswith("/api/v5/")
        or "://" in request_path
        or "#" in request_path
        or any(character in request_path for character in ("\x00", "\r", "\n"))
    ):
        raise OkxDemoPreflightBlocked("OKX Demo request path is invalid")

    credential_parts = tuple(
        _required_environment(environment, name)
        for name in OKX_DEMO_CREDENTIAL_ENV_NAMES
    )
    request_timestamp = timestamp or _timestamp()
    if (
        not isinstance(request_timestamp, str)
        or not request_timestamp
        or any(
            character in request_timestamp
            for character in ("\x00", "\r", "\n")
        )
    ):
        raise OkxDemoPreflightBlocked("OKX Demo request timestamp is invalid")
    return dict(
        (
            ("OK-ACCESS-KEY", credential_parts[0]),
            (
                "OK-ACCESS-SIGN",
                _signature(
                    credential_parts[1],
                    request_timestamp,
                    method=method,
                    request_path=request_path,
                    body=body,
                ),
            ),
            ("OK-ACCESS-TIMESTAMP", request_timestamp),
            ("OK-ACCESS-PASSPHRASE", credential_parts[2]),
        )
    )


def require_pinned_account_fingerprint(environment: Mapping[str, str]) -> str:
    """Require the canonical pin before an authenticated adapter read is signed."""

    fingerprint = _required_environment(
        environment,
        OKX_DEMO_ACCOUNT_FINGERPRINT_ENV,
    )
    if (
        len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise OkxDemoPreflightBlocked(
            "OKX Demo account fingerprint is missing or invalid"
        )
    return fingerprint


def build_account_config_request(
    environment: Mapping[str, str],
    *,
    timestamp: Optional[str] = None,
) -> Request:
    headers = _build_demo_authorization_headers(
        environment,
        method="GET",
        request_path=ACCOUNT_CONFIG_PATH,
        body="",
        timestamp=timestamp,
    )
    headers.update(
        {
            "Accept": "application/json",
            SIMULATED_TRADING_HEADER[0]: SIMULATED_TRADING_HEADER[1],
        }
    )
    return Request(
        OKX_DEMO_REST_URL + ACCOUNT_CONFIG_PATH,
        method="GET",
        headers=headers,
    )


def validate_account_config(
    payload: Any,
    *,
    expected_fingerprint: str,
) -> Dict[str, Any]:
    account = validate_account_safety(payload)
    if (
        len(expected_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in expected_fingerprint)
    ):
        raise OkxDemoPreflightBlocked("OKX Demo account fingerprint is missing or invalid")
    observed_fingerprint = account_fingerprint(account)
    if not hmac.compare_digest(observed_fingerprint, expected_fingerprint):
        raise OkxDemoPreflightBlocked("OKX Demo account fingerprint does not match")
    return _ready_attestation()


def validate_account_safety(payload: Any) -> Mapping[str, Any]:
    """Validate remote identity, permissions, and account mode before pinning."""

    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise OkxDemoPreflightBlocked("OKX Demo account attestation failed")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise OkxDemoPreflightBlocked("OKX Demo account identity is unknown")

    account = data[0]
    account_fingerprint(account)
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
    return account


def _ready_attestation() -> Dict[str, Any]:
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


def _fetch_account_config(
    request: Request,
    *,
    opener: Callable[..., Any],
) -> Any:
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
        return json.loads(raw_payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise OkxDemoPreflightBlocked(
            "OKX Demo account attestation returned invalid JSON"
        ) from None


def run_preflight(
    environment: Optional[Mapping[str, str]] = None,
    *,
    opener: Callable[..., Any] = urlopen,
) -> Dict[str, Any]:
    active_environment = os.environ if environment is None else environment
    request = build_account_config_request(active_environment)
    expected_fingerprint = require_pinned_account_fingerprint(active_environment)
    payload = _fetch_account_config(request, opener=opener)
    return validate_account_config(
        payload,
        expected_fingerprint=expected_fingerprint,
    )


def account_fingerprint_pin_exists() -> bool:
    if sys.platform != "darwin":
        raise OkxDemoPreflightBlocked("macOS Keychain is required for account pinning")
    account = pwd.getpwuid(os.getuid()).pw_name
    try:
        completed = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                OKX_DEMO_ACCOUNT_FINGERPRINT_KEYCHAIN_SERVICE,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise OkxDemoPreflightBlocked(
            "OKX Demo account fingerprint Keychain check failed"
        ) from None
    if completed.returncode == 0:
        return True
    if completed.returncode == 44:
        return False
    raise OkxDemoPreflightBlocked(
        "OKX Demo account fingerprint Keychain check failed"
    )


def _delete_account_fingerprint_pin(account: str) -> bool:
    command = [
        "/usr/bin/security",
        "delete-generic-password",
        "-a",
        account,
        "-s",
        OKX_DEMO_ACCOUNT_FINGERPRINT_KEYCHAIN_SERVICE,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def write_account_fingerprint_pin(
    fingerprint: str,
) -> None:
    """Write and verify the digest without placing it in process argv."""

    if sys.platform != "darwin":
        raise OkxDemoPreflightBlocked("macOS Keychain is required for account pinning")
    if (
        len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise OkxDemoPreflightBlocked(
            "OKX Demo account fingerprint Keychain write failed"
        )
    account = pwd.getpwuid(os.getuid()).pw_name
    add_command = [
        "/usr/bin/security",
        "add-generic-password",
        "-a",
        account,
        "-s",
        OKX_DEMO_ACCOUNT_FINGERPRINT_KEYCHAIN_SERVICE,
        "-w",
    ]
    try:
        completed = subprocess.run(
            add_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            input=fingerprint + "\n" + fingerprint + "\n",
        )
    except (OSError, subprocess.TimeoutExpired):
        raise OkxDemoPreflightBlocked(
            "OKX Demo account fingerprint Keychain write failed"
        ) from None
    if completed.returncode != 0:
        raise OkxDemoPreflightBlocked(
            "OKX Demo account fingerprint Keychain write failed"
        )

    read_command = [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        account,
        "-s",
        OKX_DEMO_ACCOUNT_FINGERPRINT_KEYCHAIN_SERVICE,
        "-w",
    ]
    readback: Optional[str] = None
    try:
        verified = subprocess.run(
            read_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
        candidate = verified.stdout.rstrip("\r\n")
        if (
            verified.returncode == 0
            and len(candidate) == 64
            and not any(character in candidate for character in ("\x00", "\r", "\n"))
        ):
            readback = candidate
    except (OSError, subprocess.TimeoutExpired):
        readback = None
    if readback is not None and hmac.compare_digest(readback, fingerprint):
        return

    _delete_account_fingerprint_pin(account)
    raise OkxDemoPreflightBlocked(
        "OKX Demo account fingerprint Keychain verification failed"
    )


def run_account_pin(
    environment: Optional[Mapping[str, str]] = None,
    *,
    opener: Callable[..., Any] = urlopen,
    pin_exists: Callable[[], bool] = account_fingerprint_pin_exists,
    pin_writer: Callable[[str], None] = write_account_fingerprint_pin,
) -> Dict[str, Any]:
    """Pin one validated Demo identity without returning its identity or digest."""

    if pin_exists():
        raise OkxDemoPreflightBlocked(
            "OKX Demo account fingerprint already exists; refusing to overwrite"
        )
    active_environment = os.environ if environment is None else environment
    request = build_account_config_request(active_environment)
    payload = _fetch_account_config(request, opener=opener)
    account = validate_account_safety(payload)
    pin_writer(account_fingerprint(account))
    return {
        "status": "READY",
        "execution_target": "OKX_DEMO",
        "account_fingerprint_pinned": True,
    }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin-account", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        payload = run_account_pin() if args.pin_account else run_preflight()
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
