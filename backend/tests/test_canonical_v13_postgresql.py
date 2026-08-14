from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError

from app.canonical_v13.bootstrap import verify_postgresql_bootstrap
from app.canonical_v13.authority_upgrade import (
    PREVIOUS_CANONICAL_MANIFEST_DIGEST,
    apply_authority_upgrade,
    render_authority_rollback_acl_sql,
    rollback_authority_upgrade,
    verify_authority_upgrade_state,
)
from app.canonical_v13.configuration_governance import (
    create_audited_configuration_draft,
)
from app.canonical_v13.genesis import (
    assert_postgresql_acl_sql,
    install_canonical_genesis,
    render_postgresql_acl_sql,
    render_postgresql_owner_sql,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping
from app.canonical_v13.manifest import CANONICAL_MANIFEST_DIGEST
from app.canonical_v13.models import AUDIT_EVENTS_TABLE, SCHEMA_METADATA_TABLE


DATABASE_URL = os.environ.get("CANONICAL_V13_POSTGRES_URL")
ROLE_PREFIX = os.environ.get(
    "CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_"
)

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANONICAL_V13_POSTGRES_URL is required for the isolated contract",
)


def _statements(sql: str) -> tuple[str, ...]:
    return tuple(statement for statement in sql.split(";\n") if statement.strip())


def test_empty_postgresql_genesis_mapping_acl_and_repeat_noop() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(ROLE_PREFIX)
    acl = render_postgresql_acl_sql(mapping)
    assert_postgresql_acl_sql(acl, mapping)
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            first = install_canonical_genesis(
                connection, installer_identity="canonical-v13-ci"
            )
            if first.created:
                for statement in _statements(acl):
                    connection.exec_driver_sql(statement)
                for statement in _statements(render_postgresql_owner_sql(mapping)):
                    connection.exec_driver_sql(statement)
            accepted = verify_postgresql_bootstrap(
                connection,
                role_mapping=mapping,
                require_zero_business_rows=True,
            )
        assert first.created is True or first.repeat_noop is True
        assert accepted.accepted is True
        assert accepted.problems == ()
        assert accepted.table_count == 46
        assert accepted.business_row_count == 0

        with engine.begin() as connection:
            repeat = install_canonical_genesis(
                connection, installer_identity="canonical-v13-ci-repeat"
            )
        assert repeat.created is False
        assert repeat.repeat_noop is True

        with engine.connect() as connection:
            transaction = connection.begin()
            connection.exec_driver_sql(
                f"SET LOCAL ROLE {mapping.physical('canonical_api_reader')}"
            )
            savepoint = connection.begin_nested()
            with pytest.raises(DBAPIError) as denied:
                connection.exec_driver_sql(
                    "INSERT INTO strategy_platform_v13.audit_events "
                    "DEFAULT VALUES"
                )
            assert denied.value.orig.sqlstate == "42501"
            savepoint.rollback()
            transaction.rollback()

        with engine.connect() as connection:
            transaction = connection.begin()
            connection.exec_driver_sql(
                f"SET LOCAL ROLE {mapping.physical('canonical_control_writer')}"
            )
            result = create_audited_configuration_draft(
                connection,
                actor_identity="canonical-v13-ci-control",
                idempotency_key="configuration-draft-acl-v1",
                profile_key="configuration-draft-acl-v1",
                configuration_kind="DIVERSITY",
                scope_key="ci",
                workflow_key="research",
                schema_json={"type": "object", "additionalProperties": False},
                payload_json={
                    "rules": [
                        {
                            "rule_key": "exact",
                            "algorithm": "exact-v1",
                            "metric": "duplicate_count",
                            "operator": "==",
                            "threshold": 0,
                        }
                    ]
                },
                adapter_identity="canonical-v13-ci-adapter",
                adapter_digest="a" * 64,
                dependencies=(),
            )
            assert result["idempotent_replay"] is False
            transaction.rollback()

        legacy_role = ROLE_PREFIX + "research_writer"
        with engine.connect() as connection:
            transaction = connection.begin()
            current = verify_authority_upgrade_state(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
            )
            assert current.accepted is True
            assert current.state == "CURRENT"

            for statement in _statements(
                render_authority_rollback_acl_sql(
                    role_mapping=mapping,
                    legacy_research_writer_role=legacy_role,
                )
            ):
                connection.exec_driver_sql(statement)
            connection.execute(
                SCHEMA_METADATA_TABLE.update()
                .where(
                    SCHEMA_METADATA_TABLE.c.metadata_key
                    == "canonical-v13-genesis",
                    SCHEMA_METADATA_TABLE.c.manifest_digest
                    == CANONICAL_MANIFEST_DIGEST,
                )
                .values(manifest_digest=PREVIOUS_CANONICAL_MANIFEST_DIGEST)
            )
            previous = verify_authority_upgrade_state(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
            )
            assert previous.accepted is True
            assert previous.state == "PREVIOUS_READY"

            upgraded = apply_authority_upgrade(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
                actor_identity="canonical-v13-ci-authority-upgrade",
            )
            assert upgraded.status == "UPGRADED"
            assert upgraded.generation == 1
            assert upgraded.research_row_count == 0

            event = connection.execute(
                AUDIT_EVENTS_TABLE.select().where(
                    AUDIT_EVENTS_TABLE.c.event_type
                    == "CANONICAL_RESEARCH_AUTHORITY_UPGRADED"
                )
            ).mappings().one()
            connection.execute(
                AUDIT_EVENTS_TABLE.update()
                .where(AUDIT_EVENTS_TABLE.c.id == event["id"])
                .values(receipt_digest="0" * 64)
            )
            drifted = verify_authority_upgrade_state(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
            )
            assert drifted.accepted is False
            assert any("receipt drift" in problem for problem in drifted.problems)
            connection.execute(
                AUDIT_EVENTS_TABLE.update()
                .where(AUDIT_EVENTS_TABLE.c.id == event["id"])
                .values(receipt_digest=event["receipt_digest"])
            )

            rolled_back = rollback_authority_upgrade(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
                actor_identity="canonical-v13-ci-authority-rollback",
            )
            assert rolled_back.status == "ROLLED_BACK"
            assert rolled_back.generation == 1

            reapplied = apply_authority_upgrade(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=legacy_role,
                actor_identity="canonical-v13-ci-authority-reapply",
            )
            assert reapplied.status == "UPGRADED"
            assert reapplied.generation == 2
            transaction.rollback()
    finally:
        engine.dispose()
