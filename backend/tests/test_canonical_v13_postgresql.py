from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError

from app.canonical_v13.bootstrap import verify_postgresql_bootstrap
from app.canonical_v13.genesis import (
    assert_postgresql_acl_sql,
    install_canonical_genesis,
    render_postgresql_acl_sql,
    render_postgresql_owner_sql,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


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
    finally:
        engine.dispose()
