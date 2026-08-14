"""Fail-closed production composition for research persistence identities.

This module only validates connection locators.  It does not connect, create an
engine, execute research, or expose credentials.  The production orchestrator uses
the result to keep validation, scoring, qualification, and optimization transactions
on four distinct PostgreSQL roles aimed at one canonical database.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from app.canonical_v13.role_mapping import CanonicalRoleMapping


VALIDATION_DATABASE_URL_ENV: Final = (
    "FREQTRADE_AI_CANONICAL_V13_VALIDATION_DATABASE_URL"
)
SCORING_DATABASE_URL_ENV: Final = "FREQTRADE_AI_CANONICAL_V13_SCORING_DATABASE_URL"
QUALIFICATION_DATABASE_URL_ENV: Final = (
    "FREQTRADE_AI_CANONICAL_V13_QUALIFICATION_DATABASE_URL"
)
OPTIMIZATION_DATABASE_URL_ENV: Final = (
    "FREQTRADE_AI_CANONICAL_V13_OPTIMIZATION_DATABASE_URL"
)

RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "canonical_validation_writer": VALIDATION_DATABASE_URL_ENV,
        "canonical_scoring_writer": SCORING_DATABASE_URL_ENV,
        "canonical_qualification_writer": QUALIFICATION_DATABASE_URL_ENV,
        "canonical_optimization_writer": OPTIMIZATION_DATABASE_URL_ENV,
    }
)


class CanonicalResearchPersistenceBlocked(RuntimeError):
    """A production persistence locator is missing, ambiguous, or over-broad."""


@dataclass(frozen=True)
class ResearchPersistenceURLs:
    urls: Mapping[str, URL]
    database_locator: tuple[object, ...]

    def for_capability(self, logical_role: str) -> URL:
        try:
            return self.urls[logical_role]
        except KeyError as exc:
            raise CanonicalResearchPersistenceBlocked(
                f"BLOCKED_RESEARCH_PERSISTENCE_CAPABILITY: {logical_role!r}"
            ) from exc


def _database_locator(url: URL) -> tuple[object, ...]:
    return (
        url.drivername,
        url.host,
        url.port,
        url.database,
        tuple(
            sorted(
                (key, tuple(value))
                for key, value in url.normalized_query.items()
            )
        ),
    )


def resolve_research_persistence_urls(
    environment: Mapping[str, str],
    *,
    role_mapping: CanonicalRoleMapping,
) -> ResearchPersistenceURLs:
    """Resolve four exact roles without returning or logging any raw DSN."""

    urls: dict[str, URL] = {}
    for logical_role, environment_name in (
        RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY.items()
    ):
        raw = environment.get(environment_name)
        if not raw:
            raise CanonicalResearchPersistenceBlocked(
                f"BLOCKED_RESEARCH_DATABASE_URL_UNSET: {environment_name}"
            )
        try:
            parsed = make_url(raw)
        except ArgumentError as exc:
            raise CanonicalResearchPersistenceBlocked(
                f"BLOCKED_RESEARCH_DATABASE_URL_INVALID: {environment_name}"
            ) from exc
        if parsed.drivername != "postgresql+psycopg" or not parsed.database:
            raise CanonicalResearchPersistenceBlocked(
                f"BLOCKED_RESEARCH_DATABASE_URL_INVALID: {environment_name}"
            )
        expected_role = role_mapping.physical(logical_role)
        if parsed.username != expected_role:
            raise CanonicalResearchPersistenceBlocked(
                "BLOCKED_RESEARCH_ROLE_IDENTITY: "
                f"{logical_role} requires its exact role"
            )
        urls[logical_role] = parsed

    locators = {_database_locator(url) for url in urls.values()}
    if len(locators) != 1:
        raise CanonicalResearchPersistenceBlocked(
            "BLOCKED_RESEARCH_DATABASE_SPLIT: all research writers must target "
            "one canonical database"
        )
    usernames = {url.username for url in urls.values()}
    if len(usernames) != len(urls):
        raise CanonicalResearchPersistenceBlocked(
            "BLOCKED_RESEARCH_ROLE_SEPARATION: writer roles must be distinct"
        )
    return ResearchPersistenceURLs(
        urls=MappingProxyType(urls),
        database_locator=next(iter(locators)),
    )


__all__ = [
    "OPTIMIZATION_DATABASE_URL_ENV",
    "QUALIFICATION_DATABASE_URL_ENV",
    "RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY",
    "SCORING_DATABASE_URL_ENV",
    "VALIDATION_DATABASE_URL_ENV",
    "CanonicalResearchPersistenceBlocked",
    "ResearchPersistenceURLs",
    "resolve_research_persistence_urls",
]
