from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app.db.migrations import (
    SCHEMA_VERSION,
    STRATEGY_PLATFORM_V1_BASE_VERSION,
    STRATEGY_PLATFORM_V1_TABLES,
    VERSION_TABLE,
    upgrade_database,
    verify_schema,
)
from app.db.session import create_database_engine

POSTGRES_WORKER_URL = os.environ.get("POSTGRES_WORKER_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_WORKER_URL,
    reason="POSTGRES_WORKER_URL is required for the PostgreSQL strategy-platform gate",
)


@pytest.fixture()
def postgres_engine():
    assert POSTGRES_WORKER_URL is not None
    engine = create_database_engine(POSTGRES_WORKER_URL)
    _reset_schema(engine)
    yield engine
    _reset_schema(engine)
    upgrade_database(engine)
    engine.dispose()


def _reset_schema(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


def _simulate_v45_schema(connection) -> None:
    """Remove only V1.3 additions in a disposable test DB to exercise v45 upgrade."""

    connection.execute(
        text(
            "ALTER TABLE research_jobs "
            "DROP COLUMN configuration_bundle_snapshot_id; "
            "ALTER TABLE strategy_deployments "
            "DROP COLUMN configuration_bundle_snapshot_id"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE strategy_validation_windows "
            "DROP CONSTRAINT strategy_validation_windows_plan_config_attempt_unique, "
            "DROP CONSTRAINT strategy_validation_windows_attempt_number_check, "
            "DROP CONSTRAINT strategy_validation_windows_total_trades_check, "
            "DROP CONSTRAINT strategy_validation_windows_status_check, "
            "ADD CONSTRAINT strategy_validation_windows_status_check CHECK ("
            "status IN ('DECLARED','READY','PASSED','BLOCKED')), "
            "ADD CONSTRAINT strategy_validation_windows_kind_check CHECK ("
            "window_kind IN ('OOS','WALK_FORWARD')), "
            "ADD CONSTRAINT strategy_validation_windows_plan_ordinal_unique "
            "UNIQUE (validation_plan_id, ordinal), "
            "ALTER COLUMN window_kind SET NOT NULL, "
            "DROP COLUMN window_config_id, "
            "DROP COLUMN window_key_snapshot, "
            "DROP COLUMN name_zh_snapshot, "
            "DROP COLUMN description_zh_snapshot, "
            "DROP COLUMN attempt_number, "
            "DROP COLUMN net_profit_after_cost, "
            "DROP COLUMN max_drawdown, "
            "DROP COLUMN volatility, "
            "DROP COLUMN total_trades, "
            "DROP COLUMN failure_code, "
            "DROP COLUMN failure_message"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE strategy_validation_plans "
            "DROP CONSTRAINT strategy_validation_plans_target_cycle_unique, "
            "DROP CONSTRAINT strategy_validation_plans_cycle_number_check, "
            "DROP CONSTRAINT strategy_validation_plans_snapshot_digest_check, "
            "DROP CONSTRAINT strategy_validation_plans_status_check, "
            "ADD CONSTRAINT strategy_validation_plans_status_check CHECK ("
            "status IN ('DECLARED','RUNNING','PASSED','BLOCKED')), "
            "DROP COLUMN strategy_target_id, "
            "DROP COLUMN quality_gate_profile_version_id, "
            "DROP COLUMN validation_window_config_set_id, "
            "DROP COLUMN configuration_bundle_snapshot_id, "
            "DROP COLUMN cycle_number, "
            "DROP COLUMN trigger_source_key, "
            "DROP COLUMN trigger_metadata, "
            "DROP COLUMN started_at, "
            "DROP COLUMN policy_snapshot_digest, "
            "DROP COLUMN market_data_snapshot_digest"
        )
    )
    for table_name in reversed(STRATEGY_PLATFORM_V1_TABLES):
        connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
    connection.execute(text(f"DELETE FROM {VERSION_TABLE}"))
    connection.execute(
        text(f"INSERT INTO {VERSION_TABLE}(version) VALUES (:version)"),
        {"version": STRATEGY_PLATFORM_V1_BASE_VERSION},
    )


def test_v45_upgrade_preserves_existing_rows_and_is_idempotent(postgres_engine) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO strategies(name,slug,description,status,source,tags) "
                "VALUES ('pre-v13','pre-v13','preserve me','active','manual','[]'::json)"
            )
        )
        strategy_id = connection.execute(
            text("SELECT id FROM strategies WHERE slug='pre-v13'")
        ).scalar_one()
        _simulate_v45_schema(connection)

    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    assert verify_schema(postgres_engine).ready is True
    with postgres_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id,name,description,status,source "
                "FROM strategies WHERE slug='pre-v13'"
            )
        ).one()
        assert tuple(row) == (
            strategy_id,
            "pre-v13",
            "preserve me",
            "active",
            "manual",
        )


def test_configuration_activation_bundle_and_immutability_guards(
    postgres_engine,
) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    digest = "a" * 64
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO configuration_types("
                "type_key,name_zh,description_zh,schema_version,handler_key,"
                "editor_capability,enabled) VALUES ("
                "'research-profile','研究装配','test','v1','research-v1','{}'::json,true)"
            )
        )
        version_id = connection.execute(
            text(
                "INSERT INTO configuration_versions("
                "type_key,version_number,lifecycle_status,payload_json,schema_version,"
                "config_digest,created_by) VALUES ("
                "'research-profile',1,'DRAFT','{}'::json,'v1',:digest,'test') "
                "RETURNING id"
            ),
            {"digest": digest},
        ).scalar_one()

    with pytest.raises(DBAPIError, match="VALIDATED"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO configuration_activations("
                    "config_type,scope_type,scope_key,version_id,activated_by) "
                    "VALUES ('research-profile','research','production-research',"
                    ":version_id,'test')"
                ),
                {"version_id": version_id},
            )

    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE configuration_versions SET lifecycle_status='VALIDATED',"
                "validated_at=clock_timestamp() WHERE id=:version_id"
            ),
            {"version_id": version_id},
        )
        connection.execute(
            text(
                "INSERT INTO configuration_activations("
                "config_type,scope_type,scope_key,version_id,activated_by) "
                "VALUES ('research-profile','research','production-research',"
                ":version_id,'test')"
            ),
            {"version_id": version_id},
        )
        bundle_id = connection.execute(
            text(
                "INSERT INTO configuration_bundle_snapshots("
                "workflow_kind,scope_type,scope_key,aggregate_profile_version_id,"
                "resolved_versions_json,resolved_digests_json,bundle_digest,"
                "capability_snapshot) VALUES ("
                "'research','research','production-research',:version_id,"
                "json_build_object('research-profile',:version_id),"
                "json_build_object('research-profile',CAST(:config_digest AS text)),"
                ":bundle_digest,"
                "json_build_object('demo_only',true,'allow_real_funds',false,"
                "'single_writer_required',true)) RETURNING id"
            ),
            {
                "version_id": version_id,
                "config_digest": digest,
                "bundle_digest": "b" * 64,
            },
        ).scalar_one()

    with postgres_engine.begin() as connection:
        dependency_version_id = connection.execute(
            text(
                "INSERT INTO configuration_versions("
                "type_key,version_number,lifecycle_status,payload_json,schema_version,"
                "config_digest,created_by) VALUES ("
                "'research-profile',2,'DRAFT','{}'::json,'v1',:digest,'test') "
                "RETURNING id"
            ),
            {"digest": "d" * 64},
        ).scalar_one()
        connection.execute(
            text(
                "UPDATE configuration_versions SET lifecycle_status='VALIDATED',"
                "validated_at=clock_timestamp() WHERE id=:version_id"
            ),
            {"version_id": dependency_version_id},
        )

    with pytest.raises(DBAPIError, match="dependencies are immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO configuration_dependencies("
                    "configuration_version_id,depends_on_version_id,relation_key) "
                    "VALUES (:version_id,:dependency_version_id,'late-binding')"
                ),
                {
                    "version_id": version_id,
                    "dependency_version_id": dependency_version_id,
                },
            )

    with pytest.raises(DBAPIError, match="version/digest mismatch"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO configuration_bundle_snapshots("
                    "workflow_kind,scope_type,scope_key,aggregate_profile_version_id,"
                    "resolved_versions_json,resolved_digests_json,bundle_digest,"
                    "capability_snapshot) VALUES ("
                    "'research','research','production-research',:version_id,"
                    "json_build_object('research-profile',:version_id),"
                    "json_build_object('research-profile',CAST(:config_digest AS text),"
                    "'unexpected','not-resolved'),:bundle_digest,"
                    "json_build_object('demo_only',true,'allow_real_funds',false,"
                    "'single_writer_required',true))"
                ),
                {
                    "version_id": version_id,
                    "config_digest": digest,
                    "bundle_digest": "c" * 64,
                },
            )

    with pytest.raises(DBAPIError, match="immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE configuration_versions SET payload_json="
                    "json_build_object('candidate_count',61) WHERE id=:version_id"
                ),
                {"version_id": version_id},
            )
    with pytest.raises(DBAPIError, match="immutable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE configuration_bundle_snapshots SET scope_key='changed' "
                    "WHERE id=:bundle_id"
                ),
                {"bundle_id": bundle_id},
            )


@pytest.mark.parametrize(
    "payload,error",
    (
        ('{"allow_real_funds":true}', "Demo-only writer safety"),
        ('{"demo_only":false}', "Demo-only writer safety"),
        ('{"single_writer_required":false}', "Demo-only writer safety"),
        ('{"api_secret":"not-allowed"}', "secret values"),
        ('{"python_code":"print(1)"}', "executable code"),
    ),
)
def test_configuration_cannot_weaken_safety_or_embed_secrets(
    postgres_engine, payload: str, error: str
) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO configuration_types("
                "type_key,name_zh,description_zh,schema_version,handler_key,"
                "editor_capability,enabled) VALUES ("
                "'safety-test','安全','test','v1','test-v1','{}'::json,true)"
            )
        )
    with pytest.raises(DBAPIError, match=error):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO configuration_versions("
                    "type_key,version_number,lifecycle_status,payload_json,schema_version,"
                    "config_digest,created_by) VALUES ("
                    "'safety-test',1,'DRAFT',CAST(:payload AS json),'v1',:digest,'test')"
                ),
                {"payload": payload, "digest": "c" * 64},
            )


def test_v13_tables_do_not_expand_runtime_acl(postgres_engine) -> None:
    assert upgrade_database(postgres_engine) == SCHEMA_VERSION
    inspector = inspect(postgres_engine)
    assert set(STRATEGY_PLATFORM_V1_TABLES).issubset(inspector.get_table_names())
    with postgres_engine.connect() as connection:
        granted = (
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.role_table_grants "
                    "WHERE grantee='freqtrade' AND table_schema='public' "
                    "AND table_name = ANY(:tables)"
                ),
                {"tables": list(STRATEGY_PLATFORM_V1_TABLES)},
            )
            .scalars()
            .all()
        )
    assert granted == []
