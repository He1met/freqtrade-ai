from __future__ import annotations

import json
from typing import Mapping

from app.adapters.okx_demo.credential_preflight import (
    ALLOW_REAL_FUNDS_ENV,
    EXECUTION_TARGET_ENV,
    OKX_DEMO_CREDENTIAL_ENV_NAMES,
    OKX_DEMO_REST_URL,
    REST_URL_ENV,
    OkxDemoPreflightBlocked,
    _required_environment,
    _signature,
    _timestamp,
)


def build_demo_write_authorization_headers(
    environment: Mapping[str, str],
    *,
    method: str,
    request_path: str,
    body: str,
    timestamp: str | None = None,
) -> dict[str, str]:
    """Sign one exact allowlisted OKX Demo write inside the sealed bridge."""

    from app.adapters.okx_demo.write_transport import WRITE_PATHS

    if environment.get(EXECUTION_TARGET_ENV) != "OKX_DEMO":
        raise OkxDemoPreflightBlocked(
            "execution target is not the sole OKX_DEMO target"
        )
    if environment.get(ALLOW_REAL_FUNDS_ENV) != "false":
        raise OkxDemoPreflightBlocked(
            "real-fund access is not explicitly disabled"
        )
    if environment.get(REST_URL_ENV) != OKX_DEMO_REST_URL:
        raise OkxDemoPreflightBlocked(
            "OKX Demo REST URL is missing or unknown"
        )
    if method != "POST" or request_path not in WRITE_PATHS:
        raise OkxDemoPreflightBlocked(
            "OKX Demo writer credential boundary permits allowlisted POST only"
        )
    try:
        decoded = json.loads(body)
        canonical_body = json.dumps(
            decoded,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise OkxDemoPreflightBlocked(
            "OKX Demo writer body is not canonical JSON"
        ) from None
    if not isinstance(decoded, dict) or not decoded or canonical_body != body:
        raise OkxDemoPreflightBlocked(
            "OKX Demo writer body is not canonical JSON"
        )

    parts = tuple(
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
            ("OK-ACCESS-KEY", parts[0]),
            (
                "OK-ACCESS-SIGN",
                _signature(
                    parts[1],
                    request_timestamp,
                    method=method,
                    request_path=request_path,
                    body=body,
                ),
            ),
            ("OK-ACCESS-TIMESTAMP", request_timestamp),
            ("OK-ACCESS-PASSPHRASE", parts[2]),
        )
    )
