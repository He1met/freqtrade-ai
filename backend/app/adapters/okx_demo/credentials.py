from __future__ import annotations

from typing import Mapping, Protocol

from app.adapters.okx_demo.credential_preflight import (
    ALLOW_REAL_FUNDS_ENV,
    EXECUTION_TARGET_ENV,
    OKX_DEMO_ACCOUNT_FINGERPRINT_ENV,
    OKX_DEMO_CREDENTIAL_ENV_NAMES,
    REST_URL_ENV,
    OkxDemoPreflightBlocked,
    _build_demo_authorization_headers,
    run_preflight,
)


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


_ATTESTED_PROVIDER_CAPABILITY = object()
_ATTESTED_ENVIRONMENT_NAMES = (
    EXECUTION_TARGET_ENV,
    ALLOW_REAL_FUNDS_ENV,
    REST_URL_ENV,
    *OKX_DEMO_CREDENTIAL_ENV_NAMES,
    OKX_DEMO_ACCOUNT_FINGERPRINT_ENV,
)


class _AttestedOkxDemoCredentialProvider:
    """A frozen credential snapshot already attested against its account pin."""

    def __init__(
        self,
        environment: Mapping[str, str],
        *,
        capability: object,
    ) -> None:
        if capability is not _ATTESTED_PROVIDER_CAPABILITY:
            raise OkxDemoCredentialsUnavailable(
                "OKX_DEMO credential provider requires account attestation"
            )
        self._environment = dict(environment)

    def authorization_headers(
        self,
        *,
        method: str,
        request_path: str,
        body: str,
    ) -> Mapping[str, str]:
        try:
            return _build_demo_authorization_headers(
                self._environment,
                method=method,
                request_path=request_path,
                body=body,
            )
        except (OkxDemoPreflightBlocked, OSError, TypeError, ValueError):
            raise OkxDemoCredentialsUnavailable(
                "OKX_DEMO credential provider is unavailable"
            ) from None

    def close(self) -> None:
        self._environment.clear()


def _is_attested_okx_demo_credential_provider(provider: object) -> bool:
    """Keep the real HTTP transport on the factory-only provider path."""

    return isinstance(provider, _AttestedOkxDemoCredentialProvider)


def attest_okx_demo_credential_provider(
    environment: Mapping[str, str],
) -> OkxDemoCredentialProvider:
    """Attest one frozen #443 bundle before enabling private GET signatures."""

    snapshot = {
        name: environment.get(name, "")
        for name in _ATTESTED_ENVIRONMENT_NAMES
    }
    try:
        run_preflight(snapshot)
    except OkxDemoPreflightBlocked:
        snapshot.clear()
        raise OkxDemoCredentialsUnavailable(
            "OKX_DEMO credential provider account attestation failed"
        ) from None
    provider = _AttestedOkxDemoCredentialProvider(
        snapshot,
        capability=_ATTESTED_PROVIDER_CAPABILITY,
    )
    snapshot.clear()
    return provider
