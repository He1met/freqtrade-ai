from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.db.migrations import SCHEMA_VERSION, upgrade_database
from app.db.session import create_database_engine
from app.models import (
    ConfigurationActivation,
    ConfigurationAuditEvent,
    ConfigurationType,
    ConfigurationVersion,
)
from app.schemas.strategy_platform import (
    ConfigurationDraftCreateRequest,
    ConfigurationVersionActionRequest,
)
from app.services.configuration_management import ConfigurationManagementService

POSTGRES_WORKER_URL = os.environ.get("POSTGRES_WORKER_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_WORKER_URL,
    reason="POSTGRES_WORKER_URL is required for configuration write contracts",
)
SCOPE = {"scope_type": "research", "scope_key": "production-research"}


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            }
        },
        "required": ["targets"],
        "additionalProperties": False,
    }


@pytest.fixture()
def postgres_engine():
    assert POSTGRES_WORKER_URL is not None
    engine = create_database_engine(POSTGRES_WORKER_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    assert upgrade_database(engine) == SCHEMA_VERSION
    with Session(engine) as db:
        db.add(
            ConfigurationType(
                type_key="research-targets",
                name_zh="研究目标",
                description_zh="PostgreSQL write contract",
                schema_version="v1",
                handler_key="generic-json-v1",
                editor_capability={
                    "write_enabled": True,
                    "json_schema": _schema(),
                },
                enabled=True,
            )
        )
        active = ConfigurationVersion(
            type_key="research-targets",
            version_number=1,
            lifecycle_status="DRAFT",
            payload_json={"targets": ["BTC/USDT:USDT"]},
            schema_version="v1",
            config_digest="a" * 64,
            change_summary="seed",
            created_by="seed-owner",
        )
        db.add(active)
        db.flush()
        active.lifecycle_status = "VALIDATED"
        active.validated_at = datetime.now(timezone.utc)
        db.flush()
        db.add(
            ConfigurationActivation(
                config_type="research-targets",
                scope_type=SCOPE["scope_type"],
                scope_key=SCOPE["scope_key"],
                version_id=active.id,
                activated_by="seed-owner",
            )
        )
        db.commit()
    yield engine
    engine.dispose()


def _create_same_draft(engine) -> tuple[int, bool]:
    with Session(engine) as db:
        result = ConfigurationManagementService(db).create_draft(
            config_type="research-targets",
            request=ConfigurationDraftCreateRequest(
                **SCOPE,
                change_summary="concurrent durable request",
                payload_json={"targets": ["BTC/USDT:USDT", "ETH/USDT:USDT"]},
                dependencies=[],
            ),
            request_id="postgres-config-create-0001",
        )
        db.commit()
        return result.version.id, result.idempotent_replay


def test_postgresql_configuration_writes_are_durable_atomic_and_owner_only(
    postgres_engine,
) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: _create_same_draft(postgres_engine), range(2)))
    assert results[0][0] == results[1][0]
    assert sorted(replayed for _version_id, replayed in results) == [False, True]

    draft_id = results[0][0]
    action = ConfigurationVersionActionRequest(**SCOPE, reason="postgres contract")
    with Session(postgres_engine) as db:
        service = ConfigurationManagementService(db)
        validated = service.validate_version(
            config_type="research-targets",
            version_id=draft_id,
            request=action,
            request_id="postgres-config-validate-0001",
        )
        assert validated.validation_bundle is not None
        activated = service.activate_version(
            config_type="research-targets",
            version_id=draft_id,
            request=action,
            request_id="postgres-config-activate-0001",
        )
        db.commit()
        assert activated.active_version_id == draft_id

    with Session(postgres_engine) as db:
        with pytest.raises(StrategyPlatformReadError) as exc_info:
            ConfigurationManagementService(db).retire_version(
                config_type="research-targets",
                version_id=draft_id,
                request=action,
                request_id="postgres-config-retire-active-0001",
            )
        assert exc_info.value.code == "ACTIVE_CONFIGURATION_CANNOT_BE_RETIRED"
        db.rollback()
        assert db.scalar(
            select(func.count(ConfigurationAuditEvent.id)).where(
                ConfigurationAuditEvent.request_id == "postgres-config-create-0001"
            )
        ) == 1

    restricted_url = postgres_engine.url.set(username="freqtrade", password=None)
    restricted_engine = create_engine(restricted_url, pool_pre_ping=True)
    try:
        with Session(restricted_engine) as db:
            with pytest.raises(StrategyPlatformReadError) as exc_info:
                ConfigurationManagementService(db).audit_history(
                    config_type="research-targets",
                    **SCOPE,
                    limit=10,
                )
        assert exc_info.value.code == "OWNER_DATABASE_REQUIRED"
    finally:
        restricted_engine.dispose()
