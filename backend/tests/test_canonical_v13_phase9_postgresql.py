from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from app.canonical_v13.phase9_schema_upgrade import (
    PHASE9_EXTENSION_TABLE_NAMES,
    PHASE9_UNIQUE_CONSTRAINTS,
    apply_phase9_schema_upgrade,
    rollback_phase9_schema_upgrade,
    verify_phase9_schema_upgrade,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


DATABASE_URL = os.environ.get("CANONICAL_V13_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANONICAL_V13_POSTGRES_URL is required for the isolated contract",
)


def test_phase9_postgresql_upgrade_rollback_and_exact_replay() -> None:
    assert DATABASE_URL is not None
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

            rolled_back = rollback_phase9_schema_upgrade(connection)
            assert rolled_back.status == "ROLLED_BACK"
            assert rolled_back.present_constraints == ()
            assert rolled_back.present_extension_tables == ()
            assert rolled_back.destructive_row_operations == 0

            upgraded = apply_phase9_schema_upgrade(
                connection,
                role_mapping=CanonicalRoleMapping.from_prefix(
                    os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
                ),
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
                role_mapping=CanonicalRoleMapping.from_prefix(
                    os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
                ),
            )
            assert repeated.status == "ACCEPTED"
            assert repeated.repeat_noop is True
            assert (
                repeated.receipt_digest
                == verify_phase9_schema_upgrade(connection).receipt_digest
            )
    finally:
        engine.dispose()
