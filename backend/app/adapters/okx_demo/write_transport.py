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
    OkxDemoPreDispatchBlocked,
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
MAX_ERROR_RESPONSE_BYTES = 16 * 1024
SAFE_WRITE_RESPONSE_KEYS = frozenset({"code", "msg", "data", "inTime", "outTime"})


def _explicit_http_rejection(
    exc: HTTPError,
    *,
    expected_client_order_id: object,
    raw_payload: bytes | None = None,
) -> dict[str, object] | None:
    """Return only a redacted, explicit OKX rejection from an HTTP error.

    The response body is never forwarded.  A malformed, oversized, successful,
    or ambiguous envelope remains an unknown write outcome and must use the
    existing GET-only recovery path.
    """

    if not 400 <= exc.code <= 599:
        return None
    if raw_payload is None:
        try:
            raw_payload = exc.read(MAX_ERROR_RESPONSE_BYTES + 1)
        except (OSError, TypeError):
            return None
    if (
        not isinstance(raw_payload, bytes)
        or len(raw_payload) > MAX_ERROR_RESPONSE_BYTES
    ):
        return None
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or set(payload).difference(SAFE_WRITE_RESPONSE_KEYS)
        or not isinstance(payload.get("code"), str)
        or not payload["code"].isdigit()
        or payload["code"] == "0"
        or not isinstance(payload.get("data", []), list)
    ):
        return None
    data = payload.get("data", [])
    if not data:
        return {"code": payload["code"], "msg": "", "data": []}
    if (
        len(data) != 1
        or not isinstance(data[0], Mapping)
        or not isinstance(expected_client_order_id, str)
        or not expected_client_order_id
    ):
        return None
    item = data[0]
    order_id = item.get("ordId")
    result_code = item.get("sCode")
    if (
        item.get("clOrdId") != expected_client_order_id
        or order_id not in (None, "")
        or not isinstance(result_code, str)
        or not result_code.isdigit()
        or result_code == "0"
    ):
        return None
    return {
        "code": payload["code"],
        "msg": "",
        "data": [
            {
                "ordId": "",
                "clOrdId": expected_client_order_id,
                "sCode": result_code,
            }
        ],
    }


def _ambiguous_http_diagnostic(
    exc: HTTPError,
    *,
    expected_client_order_id: object,
    raw_payload: bytes | None = None,
) -> dict[str, object]:
    """Extract only numeric response codes and identity match state.

    The raw body, message, headers, URL, and observed client-order ID never
    cross this boundary.  This diagnostic does not turn an ambiguous response
    into a rejection; the caller must still use GET-only recovery.
    """

    diagnostic: dict[str, object] = {
        "failure_kind": "HTTP_ERROR_AMBIGUOUS",
        "http_status_code": int(exc.code),
        "client_order_id_state": "UNKNOWN",
    }
    if raw_payload is None:
        try:
            raw_payload = exc.read(MAX_ERROR_RESPONSE_BYTES + 1)
        except (OSError, TypeError):
            return diagnostic
    if not isinstance(raw_payload, bytes) or len(raw_payload) > MAX_ERROR_RESPONSE_BYTES:
        return diagnostic
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return diagnostic
    if not isinstance(payload, dict):
        return diagnostic
    code = payload.get("code")
    if isinstance(code, str) and code.isdigit():
        diagnostic["okx_code"] = code
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping):
        return diagnostic
    item = data[0]
    s_code = item.get("sCode")
    if isinstance(s_code, str) and s_code.isdigit():
        diagnostic["okx_s_code"] = s_code
    observed_client_order_id = item.get("clOrdId")
    if observed_client_order_id in (None, ""):
        diagnostic["client_order_id_state"] = "MISSING"
    elif observed_client_order_id == expected_client_order_id:
        diagnostic["client_order_id_state"] = "MATCH"
    else:
        diagnostic["client_order_id_state"] = "MISMATCH"
    return diagnostic


class OkxDemoWriteTransport(Protocol):
    def preflight(
        self,
        *,
        path: str,
        body: Mapping[str, Any],
        timeout_seconds: float = 10.0,
    ) -> None:
        """Validate and sign one request without starting network I/O."""

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
        request = self._authorized_request(
            path=path, body=body, timeout_seconds=timeout_seconds
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                status_code = response.status
                raw_payload = response.read()
        except HTTPError as exc:
            try:
                error_payload = exc.read(MAX_ERROR_RESPONSE_BYTES + 1)
            except (OSError, TypeError):
                error_payload = None
            explicit_rejection = _explicit_http_rejection(
                exc,
                expected_client_order_id=body.get("clOrdId"),
                raw_payload=error_payload,
            )
            if explicit_rejection is not None:
                return explicit_rejection
            raise OkxDemoTransportError(
                unknown_write_outcome=True,
                **_ambiguous_http_diagnostic(
                    exc,
                    expected_client_order_id=body.get("clOrdId"),
                    raw_payload=error_payload,
                ),
            ) from None
        except (TimeoutError, URLError, OSError):
            raise OkxDemoTransportError(
                unknown_write_outcome=True,
                failure_kind="NETWORK_ERROR",
            ) from None
        if status_code != 200:
            raise OkxDemoTransportError(
                unknown_write_outcome=True,
                failure_kind="HTTP_STATUS_NON_200",
                http_status_code=int(status_code),
            )
        try:
            return json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OkxDemoTransportError(
                unknown_write_outcome=True,
                failure_kind="RESPONSE_DECODE_ERROR",
                http_status_code=200,
            ) from None

    def preflight(
        self,
        *,
        path: str,
        body: Mapping[str, Any],
        timeout_seconds: float = 10.0,
    ) -> None:
        """Prove request canonicalization and signing without network access."""

        self._authorized_request(
            path=path, body=body, timeout_seconds=timeout_seconds
        )

    def _authorized_request(
        self,
        *,
        path: str,
        body: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Request:
        if path not in WRITE_PATHS:
            raise OkxDemoPreDispatchBlocked("PATH_NOT_ALLOWLISTED")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
            or timeout_seconds > 30
        ):
            raise OkxDemoPreDispatchBlocked("TIMEOUT_INVALID")
        if not isinstance(body, Mapping) or not body:
            raise OkxDemoPreDispatchBlocked("BODY_INVALID")
        try:
            body_text = json.dumps(
                dict(body),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise OkxDemoPreDispatchBlocked("BODY_INVALID") from None
        try:
            auth_headers = self._credential_provider.authorization_headers(
                method="POST",
                request_path=path,
                body=body_text,
            )
        except OkxDemoCredentialsUnavailable:
            raise OkxDemoPreDispatchBlocked("AUTH_SESSION_UNAVAILABLE") from None
        if (
            set(auth_headers) != REQUIRED_AUTH_HEADERS
            or any(
                not isinstance(value, str)
                or not value
                or CONTROL_CHARACTER_PATTERN.search(value)
                for value in auth_headers.values()
            )
        ):
            raise OkxDemoPreDispatchBlocked("AUTH_HEADERS_INVALID")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "freqtrade-ai-okx-demo-writer/1",
            "x-simulated-trading": "1",
            **dict(auth_headers),
        }
        return Request(
            self._base_url + path,
            method="POST",
            data=body_text.encode("utf-8"),
            headers=headers,
        )


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
        raise OkxDemoPreDispatchBlocked("AUTH_SESSION_UNAVAILABLE")
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
