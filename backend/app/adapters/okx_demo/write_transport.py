from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Mapping, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request

from app.adapters.okx_demo.credential_preflight import OKX_DEMO_REST_URL
from app.adapters.okx_demo.credentials import (
    OkxDemoCredentialProvider,
    OkxDemoCredentialsUnavailable,
)
from app.adapters.okx_demo.read_adapter import (
    OkxDemoReadClient,
    _AttestedWriterCredentialHandle,
)
from app.adapters.okx_demo.secure_http import build_direct_no_redirect_opener
from app.adapters.okx_demo.write_semantics import (
    OkxDemoTransportError,
    OkxDemoWriteBlocked,
)


WRITE_PATHS = frozenset(
    {
        "/api/v5/trade/order",
        "/api/v5/trade/cancel-order",
        "/api/v5/trade/amend-order",
        "/api/v5/account/set-leverage",
    }
)
REQUIRED_AUTH_HEADERS = frozenset(
    {
        "OK-ACCESS-KEY",
        "OK-ACCESS-SIGN",
        "OK-ACCESS-TIMESTAMP",
        "OK-ACCESS-PASSPHRASE",
    }
)
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
OFFLINE_TEST_ORIGIN = "https://offline.invalid"


class OkxDemoWriteTransport(Protocol):
    def post(
        self,
        *,
        path: str,
        body: Mapping[str, Any],
        timeout_seconds: float = 10.0,
    ) -> Any:
        """Perform one Demo POST; failures never expose raw response content."""


class UrllibOkxDemoWriteTransport:
    """Production transport. Construction is restricted to the server factory."""

    def __init__(
        self,
        credential_provider: OkxDemoCredentialProvider,
        *,
        _capability: object,
    ) -> None:
        raise OkxDemoWriteBlocked(
            "OKX production write transport requires the attested server factory"
        )

    def post(
        self,
        *,
        path: str,
        body: Mapping[str, Any],
        timeout_seconds: float = 10.0,
    ) -> Any:
        if path not in WRITE_PATHS:
            raise OkxDemoWriteBlocked("OKX write path is not allowlisted")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
            or timeout_seconds > 30
        ):
            raise OkxDemoWriteBlocked("OKX write timeout must be within 0-30 seconds")
        if not isinstance(body, Mapping) or not body:
            raise OkxDemoWriteBlocked("OKX write body must be a non-empty object")
        try:
            body_text = json.dumps(
                dict(body),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise OkxDemoWriteBlocked("OKX write body is not canonical JSON") from None
        try:
            auth_headers = self._credential_provider.authorization_headers(
                method="POST",
                request_path=path,
                body=body_text,
            )
        except OkxDemoCredentialsUnavailable:
            raise OkxDemoWriteBlocked(
                "OKX_DEMO write requires an active #445 attested session"
            ) from None
        if (
            set(auth_headers) != REQUIRED_AUTH_HEADERS
            or any(
                not isinstance(value, str)
                or not value
                or CONTROL_CHARACTER_PATTERN.search(value)
                for value in auth_headers.values()
            )
        ):
            raise OkxDemoWriteBlocked(
                "OKX_DEMO attested session returned invalid authorization headers"
            )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "freqtrade-ai-okx-demo-writer/1",
            "x-simulated-trading": "1",
            **dict(auth_headers),
        }
        request = Request(
            self._base_url + path,
            method="POST",
            data=body_text.encode("utf-8"),
            headers=headers,
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                status_code = response.status
                raw_payload = response.read()
        except HTTPError:
            raise OkxDemoTransportError(unknown_write_outcome=True) from None
        except (TimeoutError, URLError, OSError):
            raise OkxDemoTransportError(unknown_write_outcome=True) from None
        if status_code != 200:
            raise OkxDemoTransportError(unknown_write_outcome=True)
        try:
            return json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OkxDemoTransportError(unknown_write_outcome=True) from None


class OfflineOkxDemoWriteTransportHarness(UrllibOkxDemoWriteTransport):
    """Explicit recorded/offline seam; server code must never instantiate it."""

    def __init__(
        self,
        credential_provider: OkxDemoCredentialProvider,
        *,
        opener,
        base_url: str = OFFLINE_TEST_ORIGIN,
    ) -> None:
        if base_url.rstrip("/") != OFFLINE_TEST_ORIGIN:
            raise OkxDemoWriteBlocked(
                "offline write harness permits only a non-routable test origin"
            )
        self._credential_provider = credential_provider
        self._opener = opener
        self._base_url = OFFLINE_TEST_ORIGIN


def _create_production_write_transport(
    credential_provider: _AttestedWriterCredentialHandle,
) -> UrllibOkxDemoWriteTransport:
    if (
        type(credential_provider) is not _AttestedWriterCredentialHandle
        or not credential_provider.active()
    ):
        raise OkxDemoWriteBlocked(
            "OKX production write transport requires an attested session"
        )
    transport = object.__new__(UrllibOkxDemoWriteTransport)
    transport._credential_provider = credential_provider
    transport._opener = build_direct_no_redirect_opener()
    transport._base_url = OKX_DEMO_REST_URL
    return transport


def _create_attested_writer_credential_bridge(
    read_client: OkxDemoReadClient,
) -> _AttestedWriterCredentialHandle:
    try:
        handle = read_client._writer_credential_handle
    except AttributeError:
        raise OkxDemoCredentialsUnavailable(
            "OKX_DEMO attested credential session is unavailable"
        ) from None
    if type(handle) is not _AttestedWriterCredentialHandle:
        raise OkxDemoCredentialsUnavailable(
            "OKX_DEMO attested credential handle identity is invalid"
        )
    return handle
