from __future__ import annotations

import os

import pytest
from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.db.migrations import SCHEMA_VERSION, upgrade_database
from app.db.session import create_database_engine
from app.services.configuration_resolver import ConfigurationResolverService
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

POSTGRES_WORKER_URL = os.environ.get("POSTGRES_WORKER_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_WORKER_URL,
    reason=(
        "POSTGRES_WORKER_URL is required for the PostgreSQL "
        "strategy-platform read gate"
    ),
)


@pytest.fixture()
def postgres_engine():
    assert POSTGRES_WORKER_URL is not None
    engine = create_database_engine(POSTGRES_WORKER_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    assert upgrade_database(engine) == SCHEMA_VERSION
    yield engine
    engine.dispose()


def _seed_resolvable_graph(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO configuration_types("
                "type_key,name_zh,description_zh,schema_version,handler_key,"
                "editor_capability,enabled) VALUES "
                "('research-profile','研究装配','test','v1','generic-json-v1','{}',true),"
                "('research-targets','研究目标','test','v1','generic-json-v1','{}',true)"
            )
        )
        child_id = connection.execute(
            text(
                "INSERT INTO configuration_versions("
                "type_key,version_number,lifecycle_status,payload_json,schema_version,"
                "config_digest,created_by) VALUES "
                "('research-targets',1,'DRAFT','{}','v1',:digest,'test-owner') "
                "RETURNING id"
            ),
            {"digest": "b" * 64},
        ).scalar_one()
        connection.execute(
            text(
                "UPDATE configuration_versions SET lifecycle_status='VALIDATED',"
                "validated_at=clock_timestamp() WHERE id=:version_id"
            ),
            {"version_id": child_id},
        )
        root_id = connection.execute(
            text(
                "INSERT INTO configuration_versions("
                "type_key,version_number,lifecycle_status,payload_json,schema_version,"
                "config_digest,created_by) VALUES "
                "('research-profile',1,'DRAFT','{}','v1',:digest,'test-owner') "
                "RETURNING id"
            ),
            {"digest": "a" * 64},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO configuration_dependencies("
                "configuration_version_id,depends_on_version_id,relation_key) "
                "VALUES (:root_id,:child_id,'targets')"
            ),
            {"root_id": root_id, "child_id": child_id},
        )
        connection.execute(
            text(
                "UPDATE configuration_versions SET lifecycle_status='VALIDATED',"
                "validated_at=clock_timestamp() WHERE id=:version_id"
            ),
            {"version_id": root_id},
        )
        connection.execute(
            text(
                "INSERT INTO configuration_activations("
                "config_type,scope_type,scope_key,version_id,activated_by) "
                "VALUES ('research-profile','research','production-research',"
                ":root_id,'test-owner')"
            ),
            {"root_id": root_id},
        )


def test_owner_resolver_materializes_postgresql_guarded_snapshot(
    postgres_engine,
) -> None:
    _seed_resolvable_graph(postgres_engine)
    with Session(postgres_engine) as db:
        service = ConfigurationResolverService(db)
        resolution = service.resolve_active(
            workflow_kind="research",
            aggregate_config_type="research-profile",
            scope_type="research",
            scope_key="production-research",
        )
        snapshot = service.materialize_bundle(resolution)
        db.commit()
        stored = service.read_bundle(snapshot.snapshot_id)

    assert stored.bundle_digest == resolution.bundle_digest
    assert stored.capability_snapshot["demo_only"] is True
    with pytest.raises(DBAPIError, match="immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE configuration_bundle_snapshots SET scope_key='changed' "
                    "WHERE id=:bundle_id"
                ),
                {"bundle_id": snapshot.snapshot_id},
            )


def test_runtime_role_cannot_enter_owner_configuration_service(postgres_engine) -> None:
    restricted_url = postgres_engine.url.set(username="freqtrade", password=None)
    restricted_engine = create_engine(restricted_url, pool_pre_ping=True)
    try:
        with Session(restricted_engine) as db:
            with pytest.raises(StrategyPlatformReadError) as exc_info:
                ConfigurationResolverService(db).catalog()
        assert exc_info.value.code == "OWNER_DATABASE_REQUIRED"
        assert exc_info.value.status_code == 403
    finally:
        restricted_engine.dispose()
