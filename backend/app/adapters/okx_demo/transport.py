from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.adapters.okx_demo.errors import OkxReadAdapterError


@dataclass(frozen=True)
class OkxReadHttpResponse:
    status_code: int
    payload: object
    received_at: datetime


class OkxReadTransport(Protocol):
    def get(
        self,
        *,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> OkxReadHttpResponse:
        """Perform one GET and return already decoded JSON."""


class UrllibOkxReadTransport:
    """Minimal GET-only transport. It never logs headers, payloads, or signatures."""

    def __init__(self, base_url: str = "https://openapi.okx.com") -> None:
        self._base_url = base_url.rstrip("/")

    def get(
        self,
        *,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> OkxReadHttpResponse:
        encoded = urlencode(sorted(query.items()))
        url = f"{self._base_url}{path}" + (f"?{encoded}" if encoded else "")
        request = Request(url, method="GET", headers=dict(headers))
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
                status = response.status
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise OkxReadAdapterError(
                    kind="UNAUTHORIZED",
                    status="BLOCKED",
                    message="OKX rejected Demo read authorization",
                    http_status=exc.code,
                ) from exc
            raise OkxReadAdapterError(
                kind="RATE_LIMITED" if exc.code == 429 else "HTTP_ERROR",
                status="FAILED",
                message=f"OKX read request returned HTTP {exc.code}",
                retryable=exc.code == 429 or 500 <= exc.code <= 599,
                http_status=exc.code,
            ) from exc
        except TimeoutError as exc:
            raise OkxReadAdapterError(
                kind="TIMEOUT",
                status="FAILED",
                message="OKX read request timed out",
                retryable=True,
            ) from exc
        except (URLError, OSError) as exc:
            raise OkxReadAdapterError(
                kind="NETWORK",
                status="FAILED",
                message="OKX read request failed at the network boundary",
                retryable=True,
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OkxReadAdapterError(
                kind="INVALID_RESPONSE",
                status="FAILED",
                message="OKX read response was not valid JSON",
            ) from exc
        return OkxReadHttpResponse(
            status_code=status,
            payload=payload,
            received_at=datetime.now(timezone.utc),
        )
