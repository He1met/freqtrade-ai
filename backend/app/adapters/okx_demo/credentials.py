from __future__ import annotations

from typing import Mapping, Protocol

class OkxDemoCredentialsUnavailable(RuntimeError):
    """Raised when #443 has not supplied an authorized Demo credential source."""


class OkxDemoCredentialProvider(Protocol):
    """Narrow #443 boundary; callers never receive raw credential fields."""

    def authorization_headers(
        self,
        *,
        method: str,
        request_path: str,
        body: str,
    ) -> Mapping[str, str]:
        """Return ephemeral OKX auth headers for one read-only request."""


class UnavailableOkxDemoCredentialProvider:
    def authorization_headers(
        self,
        *,
        method: str,
        request_path: str,
        body: str,
    ) -> Mapping[str, str]:
        del method, request_path, body
        raise OkxDemoCredentialsUnavailable(
            "OKX_DEMO credential provider is not configured"
        )
