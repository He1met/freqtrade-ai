"""Explicit production composition root for the standalone canonical V1.3 API.

This module is never imported by the legacy application.  Creating the app requires
six distinct PostgreSQL LOGIN principals aimed at the same dedicated canonical
database.  It does not install genesis, apply ACLs, activate a bundle, or connect
during import.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import os

from fastapi import FastAPI, HTTPException
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.exc import SQLAlchemyError

from app.canonical_v13.api import create_canonical_v13_app
from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.bootstrap import LOCAL_SERVICE_PRINCIPALS, local_role_mapping
from app.canonical_v13.research_persistence import (
    RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY,
    CanonicalResearchPersistenceBlocked,
    resolve_research_persistence_urls,
)


READER_DATABASE_URL_ENV = "FREQTRADE_AI_CANONICAL_V13_READER_DATABASE_URL"
CONTROL_DATABASE_URL_ENV = "FREQTRADE_AI_CANONICAL_V13_CONTROL_DATABASE_URL"


class CanonicalProductionConfigurationBlocked(RuntimeError):
    """Fail-closed startup configuration error that never contains a DSN."""


def _required_postgresql_url(environment: Mapping[str, str], name: str) -> URL:
    raw = environment.get(name)
    if not raw:
        raise CanonicalProductionConfigurationBlocked(
            f"BLOCKED_CANONICAL_DATABASE_URL_UNSET: {name} is required"
        )
    try:
        parsed = make_url(raw)
    except ArgumentError as exc:
        raise CanonicalProductionConfigurationBlocked(
            f"BLOCKED_CANONICAL_DATABASE_URL_INVALID: {name} is invalid"
        ) from exc
    if parsed.drivername != "postgresql+psycopg" or not parsed.database:
        raise CanonicalProductionConfigurationBlocked(
            f"BLOCKED_CANONICAL_DATABASE_URL_INVALID: {name} must use "
            "postgresql+psycopg and name a database"
        )
    return parsed


def _database_locator(url: URL) -> tuple[object, ...]:
    return (
        url.drivername,
        url.host,
        url.port,
        url.database,
        tuple(sorted((key, tuple(value)) for key, value in url.normalized_query.items())),
    )


def _connection_factory(engine: Engine):
    @contextmanager
    def factory():
        with engine.connect() as connection:
            yield connection

    return factory


def create_app(environment: Mapping[str, str] | None = None) -> FastAPI:
    """Build the standalone app; database connections remain request-scoped."""

    resolved_environment = os.environ if environment is None else environment
    reader_url = _required_postgresql_url(
        resolved_environment, READER_DATABASE_URL_ENV
    )
    control_url = _required_postgresql_url(
        resolved_environment, CONTROL_DATABASE_URL_ENV
    )
    service_principal_by_capability = {
        capability: principal
        for principal, capability in LOCAL_SERVICE_PRINCIPALS.items()
    }
    if (
        reader_url.username
        != service_principal_by_capability["canonical_api_reader"]
        or control_url.username
        != service_principal_by_capability["canonical_control_writer"]
    ):
        raise CanonicalProductionConfigurationBlocked(
            "BLOCKED_CANONICAL_ROLE_IDENTITY: exact reader/control LOGIN principals "
            "are required"
        )
    if _database_locator(reader_url) != _database_locator(control_url):
        raise CanonicalProductionConfigurationBlocked(
            "BLOCKED_CANONICAL_DATABASE_SPLIT: reader and control roles must target "
            "the same canonical database"
        )

    try:
        research_urls = resolve_research_persistence_urls(
            resolved_environment, role_mapping=local_role_mapping()
        )
    except CanonicalResearchPersistenceBlocked as exc:
        raise CanonicalProductionConfigurationBlocked(str(exc)) from exc
    if research_urls.database_locator != _database_locator(reader_url):
        raise CanonicalProductionConfigurationBlocked(
            "BLOCKED_CANONICAL_DATABASE_SPLIT: API and research roles must target "
            "the same canonical database"
        )
    usernames = {
        reader_url.username,
        control_url.username,
        *(url.username for url in research_urls.urls.values()),
    }
    if len(usernames) != 6:
        raise CanonicalProductionConfigurationBlocked(
            "BLOCKED_CANONICAL_ROLE_SEPARATION: API and research roles must differ"
        )

    reader_engine = create_engine(reader_url, pool_pre_ping=True)
    control_engine = create_engine(control_url, pool_pre_ping=True)
    research_engines = {
        capability: create_engine(url, pool_pre_ping=True)
        for capability, url in research_urls.urls.items()
    }
    app = create_canonical_v13_app(
        reader_connection_factory=_connection_factory(reader_engine),
        control_connection_factory=_connection_factory(control_engine),
        validation_connection_factory=_connection_factory(
            research_engines["canonical_validation_writer"]
        ),
        scoring_connection_factory=_connection_factory(
            research_engines["canonical_scoring_writer"]
        ),
        qualification_connection_factory=_connection_factory(
            research_engines["canonical_qualification_writer"]
        ),
    )

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {
            "status": "HEALTHY",
            "service": "canonical-v13-api",
            "trading_capability": "TRADING_DISABLED",
        }

    @app.get("/readyz")
    def readyz() -> dict[str, object]:
        identities: dict[str, str] = {}
        try:
            for capability, engine in (
                ("reader", reader_engine),
                ("control", control_engine),
                *tuple(research_engines.items()),
            ):
                with engine.connect() as connection:
                    verification = verify_canonical_genesis(connection)
                    if not verification.accepted:
                        raise CanonicalProductionConfigurationBlocked(
                            "BLOCKED_WRONG_CANONICAL_DATABASE"
                        )
                    identities[capability] = str(
                        connection.execute(text("SELECT current_user")).scalar_one()
                    )
            if len(set(identities.values())) != len(identities):
                raise CanonicalProductionConfigurationBlocked(
                    "BLOCKED_CANONICAL_ROLE_SEPARATION"
                )
        except CanonicalProductionConfigurationBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except (SQLAlchemyError, OSError):
            raise HTTPException(
                status_code=503,
                detail="BLOCKED_CANONICAL_DATABASE_UNAVAILABLE",
            ) from None
        return {
            "status": "READY",
            "service": "canonical-v13-api",
            "reader_identity": identities["reader"],
            "control_identity": identities["control"],
            "research_identities": {
                capability: identities[capability]
                for capability in RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY
            },
            "trading_capability": "TRADING_DISABLED",
        }

    def dispose_engines() -> None:
        for engine in research_engines.values():
            engine.dispose()
        control_engine.dispose()
        reader_engine.dispose()

    app.add_event_handler("shutdown", dispose_engines)
    app.state.canonical_reader_engine = reader_engine
    app.state.canonical_control_engine = control_engine
    app.state.canonical_research_engines = research_engines
    return app


__all__ = [
    "CONTROL_DATABASE_URL_ENV",
    "READER_DATABASE_URL_ENV",
    "CanonicalProductionConfigurationBlocked",
    "create_app",
]
