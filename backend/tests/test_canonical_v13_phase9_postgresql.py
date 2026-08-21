from __future__ import annotations

import os

import pytest
from app.canonical_v13.phase9_schema_upgrade import (
    PHASE9_DATABASE_CONNECT_DELTA,
    PHASE9_EXTENSION_TABLE_NAMES,
    PHASE9_UNIQUE_CONSTRAINTS,
    CanonicalPhase9SchemaUpgradeBlocked,
    apply_phase9_schema_upgrade,
    rollback_phase9_schema_upgrade,
    verify_phase9_schema_upgrade,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("CANONICAL_V13_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANONICAL_V13_POSTGRES_URL is required for the isolated contract",
)


def test_phase9_postgresql_upgrade_rollback_and_exact_replay() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            initial = verify_phase9_schema_upgrade(connection)
            assert initial.status == "ACCEPTED"
            assert initial.present_constraints == tuple(
                sorted(PHASE9_UNIQUE_CONSTRAINTS)
            )
            assert initial.present_extension_tables == tuple(
                sorted(PHASE9_EXTENSION_TABLE_NAMES)
            )
            assert set(initial.affected_row_counts.values()) == {0}

            database_name = str(
                connection.execute(text("SELECT current_database()")).scalar_one()
            )
            for role in PHASE9_DATABASE_CONNECT_DELTA:
                connection.exec_driver_sql(
                    f'GRANT CONNECT ON DATABASE "{database_name}" '
                    f"TO {mapping.physical(role)}"
                )

            rolled_back = rollback_phase9_schema_upgrade(
                connection, role_mapping=mapping
            )
            assert rolled_back.status == "ROLLED_BACK"
            assert rolled_back.present_constraints == ()
            assert rolled_back.present_extension_tables == ()
            assert rolled_back.destructive_row_operations == 0

            rollback_replay = rollback_phase9_schema_upgrade(
                connection, role_mapping=mapping
            )
            assert rollback_replay.status == "PREVIOUS_READY"
            assert rollback_replay.repeat_noop is True

            upgraded = apply_phase9_schema_upgrade(
                connection,
                role_mapping=mapping,
            )
            assert upgraded.status == "UPGRADED"
            assert upgraded.present_constraints == tuple(
                sorted(PHASE9_UNIQUE_CONSTRAINTS)
            )
            assert upgraded.present_extension_tables == tuple(
                sorted(PHASE9_EXTENSION_TABLE_NAMES)
            )
            assert upgraded.destructive_row_operations == 0

            repeated = apply_phase9_schema_upgrade(
                connection,
                role_mapping=mapping,
            )
            assert repeated.status == "ACCEPTED"
            assert repeated.repeat_noop is True
            assert (
                repeated.receipt_digest
                == verify_phase9_schema_upgrade(connection).receipt_digest
            )
    finally:
        engine.dispose()


def test_phase9_previous_acl_and_connect_drift_fail_closed_atomically() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    extra_role = mapping.physical("canonical_approval_writer")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "GRANT DELETE ON TABLE strategy_platform_v13.signals "
                f"TO {extra_role}"
            )

        with pytest.raises(
            CanonicalPhase9SchemaUpgradeBlocked,
            match="BLOCKED_PREVIOUS_ACL_DRIFT",
        ):
            with engine.begin() as connection:
                rollback_phase9_schema_upgrade(connection, role_mapping=mapping)

        with engine.connect() as connection:
            current = verify_phase9_schema_upgrade(connection, role_mapping=mapping)
            assert current.status == "ACCEPTED"

        with engine.begin() as connection:
            connection.exec_driver_sql(
                "REVOKE DELETE ON TABLE strategy_platform_v13.signals "
                f"FROM {extra_role}"
            )
            previous = rollback_phase9_schema_upgrade(
                connection, role_mapping=mapping
            )
            assert previous.status == "ROLLED_BACK"
            database_name = str(
                connection.execute(text("SELECT current_database()")).scalar_one()
            )
            connection.exec_driver_sql(
                f'GRANT CONNECT ON DATABASE "{database_name}" TO {extra_role}'
            )
            with pytest.raises(
                CanonicalPhase9SchemaUpgradeBlocked,
                match="BLOCKED_PREVIOUS_DATABASE_CONNECT_DRIFT",
            ):
                verify_phase9_schema_upgrade(connection, role_mapping=mapping)
            connection.exec_driver_sql(
                f'REVOKE CONNECT ON DATABASE "{database_name}" FROM {extra_role}'
            )
            assert (
                verify_phase9_schema_upgrade(connection, role_mapping=mapping).status
                == "PREVIOUS_READY"
            )
            assert (
                apply_phase9_schema_upgrade(connection, role_mapping=mapping).status
                == "UPGRADED"
            )
    finally:
        engine.dispose()
