"""Fail-closed locator contract for eight isolated Phase 9 writer LOGINs."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from app.canonical_v13.role_mapping import CanonicalRoleMapping


PHASE9_PERSISTENCE_ENV_BY_CAPABILITY: Final[Mapping[str, str]] = MappingProxyType(
    {
        capability: f"FREQTRADE_AI_CANONICAL_V13_{capability.removeprefix('canonical_').upper()}_DATABASE_URL"
        for capability in (
            "canonical_approval_writer",
            "canonical_deployment_writer",
            "canonical_signal_writer",
            "canonical_risk_writer",
            "canonical_order_writer",
            "canonical_fill_writer",
            "canonical_ledger_writer",
            "canonical_reconciliation_writer",
        )
    }
)

API_PHASE9_CAPABILITIES: Final[tuple[str, ...]] = (
    "canonical_approval_writer",
    "canonical_deployment_writer",
    "canonical_risk_writer",
)


class CanonicalPhase9PersistenceBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Phase9PersistenceURLs:
    urls: Mapping[str, URL]
    database_locator: tuple[object, ...]


def _locator(url: URL) -> tuple[object, ...]:
    return (
        url.drivername,
        url.host,
        url.port,
        url.database,
        tuple(
            sorted((key, tuple(value)) for key, value in url.normalized_query.items())
        ),
    )


def phase9_service_principal(
    role_mapping: CanonicalRoleMapping, logical_role: str
) -> str:
    if logical_role not in PHASE9_PERSISTENCE_ENV_BY_CAPABILITY:
        raise CanonicalPhase9PersistenceBlocked(
            f"BLOCKED_PHASE9_PERSISTENCE_CAPABILITY: {logical_role!r}"
        )
    role = role_mapping.physical(logical_role)
    if not role.endswith("_writer"):
        raise CanonicalPhase9PersistenceBlocked("BLOCKED_PHASE9_ROLE_MAPPING")
    return role.removesuffix("_writer") + "_login"


def resolve_phase9_persistence_urls(
    environment: Mapping[str, str],
    *,
    role_mapping: CanonicalRoleMapping,
    capabilities: tuple[str, ...] | None = None,
) -> Phase9PersistenceURLs:
    resolved_capabilities = (
        tuple(PHASE9_PERSISTENCE_ENV_BY_CAPABILITY)
        if capabilities is None
        else capabilities
    )
    if (
        not resolved_capabilities
        or len(set(resolved_capabilities)) != len(resolved_capabilities)
        or any(
            capability not in PHASE9_PERSISTENCE_ENV_BY_CAPABILITY
            for capability in resolved_capabilities
        )
    ):
        raise CanonicalPhase9PersistenceBlocked(
            "BLOCKED_PHASE9_PERSISTENCE_CAPABILITY_SET"
        )
    urls: dict[str, URL] = {}
    for capability in resolved_capabilities:
        environment_name = PHASE9_PERSISTENCE_ENV_BY_CAPABILITY[capability]
        raw = environment.get(environment_name)
        if not raw:
            raise CanonicalPhase9PersistenceBlocked(
                f"BLOCKED_PHASE9_DATABASE_URL_UNSET: {environment_name}"
            )
        try:
            parsed = make_url(raw)
        except ArgumentError as exc:
            raise CanonicalPhase9PersistenceBlocked(
                f"BLOCKED_PHASE9_DATABASE_URL_INVALID: {environment_name}"
            ) from exc
        if (
            parsed.drivername != "postgresql+psycopg"
            or not parsed.database
            or parsed.username != phase9_service_principal(role_mapping, capability)
        ):
            raise CanonicalPhase9PersistenceBlocked(
                f"BLOCKED_PHASE9_ROLE_IDENTITY: {capability}"
            )
        urls[capability] = parsed
    locators = {_locator(url) for url in urls.values()}
    if len(locators) != 1 or len({url.username for url in urls.values()}) != len(urls):
        raise CanonicalPhase9PersistenceBlocked(
            "BLOCKED_PHASE9_DATABASE_OR_IDENTITY_SEPARATION"
        )
    return Phase9PersistenceURLs(
        urls=MappingProxyType(urls), database_locator=next(iter(locators))
    )


__all__ = [
    "API_PHASE9_CAPABILITIES",
    "PHASE9_PERSISTENCE_ENV_BY_CAPABILITY",
    "CanonicalPhase9PersistenceBlocked",
    "Phase9PersistenceURLs",
    "phase9_service_principal",
    "resolve_phase9_persistence_urls",
]
