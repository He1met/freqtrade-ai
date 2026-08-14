from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, select
import pytest

from app.canonical_v13 import genesis as canonical_genesis
from app.canonical_v13.genesis import (
    CANONICAL_GENESIS_IDENTITY,
    GENESIS_METADATA_KEY,
    CanonicalGenesisBlocked,
    canonical_business_row_count,
    install_canonical_genesis,
    verify_canonical_genesis,
)
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
    CANONICAL_TABLE_NAMES,
)
from app.canonical_v13.models import SCHEMA_METADATA_TABLE


def _sqlite_connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    return engine, engine.connect()


def test_empty_database_install_and_exact_repeat_are_accepted() -> None:
    engine, connection = _sqlite_connection()
    try:
        with connection.begin():
            first = install_canonical_genesis(
                connection, installer_identity="phase1-isolated-test"
            )
        assert first.created is True
        assert first.repeat_noop is False
        assert first.manifest_digest == CANONICAL_MANIFEST_DIGEST
        assert first.business_row_count == 0
        assert set(first.table_names) == set(CANONICAL_TABLE_NAMES)
        assert set(inspect(connection).get_table_names()) == set(CANONICAL_TABLE_NAMES)

        translated = connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        identity_before = dict(
            translated.execute(
                select(SCHEMA_METADATA_TABLE).where(
                    SCHEMA_METADATA_TABLE.c.metadata_key == GENESIS_METADATA_KEY
                )
            ).one()._mapping
        )
        assert identity_before["installer_identity"] == "phase1-isolated-test"
        assert identity_before["manifest_digest"] == CANONICAL_MANIFEST_DIGEST
        assert identity_before["production_default_target"] == "UNSET"
        assert identity_before["production_default_count"] == "UNSET"
        assert identity_before["production_default_cap"] == "UNSET"
        assert identity_before["trading_capability"] == "TRADING_DISABLED"
        connection.rollback()

        with connection.begin():
            repeat = install_canonical_genesis(
                connection, installer_identity="different-repeat-caller"
            )
        assert repeat.created is False
        assert repeat.repeat_noop is True
        assert repeat.business_row_count == 0
        assert repeat.installer_identity == "phase1-isolated-test"

        identity_after = dict(
            translated.execute(
                select(SCHEMA_METADATA_TABLE).where(
                    SCHEMA_METADATA_TABLE.c.metadata_key == GENESIS_METADATA_KEY
                )
            ).one()._mapping
        )
        assert identity_after == identity_before
    finally:
        connection.close()
        engine.dispose()


def test_genesis_business_rows_are_zero_and_identity_is_exact() -> None:
    engine, connection = _sqlite_connection()
    try:
        with connection.begin():
            install_canonical_genesis(
                connection, installer_identity="phase1-zero-row-test"
            )
        translated = connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        assert canonical_business_row_count(translated) == 0
        verification = verify_canonical_genesis(
            translated, require_zero_business_rows=True
        )
        assert verification.accepted is True
        assert verification.problems == ()
        assert verification.business_row_count == 0
        assert verification.manifest_digest == CANONICAL_MANIFEST_DIGEST

        runtime_verification = verify_canonical_genesis(translated)
        assert runtime_verification.accepted is True
        assert runtime_verification.business_row_count is None

        row = dict(translated.execute(select(SCHEMA_METADATA_TABLE)).one()._mapping)
        for key, expected in CANONICAL_GENESIS_IDENTITY.as_database_values().items():
            assert row[key] == expected
    finally:
        connection.close()
        engine.dispose()


def test_runtime_identity_guard_does_not_scan_business_tables(monkeypatch) -> None:
    engine, connection = _sqlite_connection()
    try:
        with connection.begin():
            install_canonical_genesis(
                connection, installer_identity="runtime-identity-test"
            )

        def forbidden_count(_connection):
            raise AssertionError("runtime identity guard must not count business rows")

        monkeypatch.setattr(
            canonical_genesis, "canonical_business_row_count", forbidden_count
        )
        assert verify_canonical_genesis(connection).accepted is True
    finally:
        connection.close()
        engine.dispose()


def test_database_wide_user_object_drift_blocks_install(monkeypatch) -> None:
    engine, connection = _sqlite_connection()
    monkeypatch.setattr(
        canonical_genesis,
        "_postgresql_user_objects",
        lambda _connection: ("relation:public.legacy_strategies",),
    )
    try:
        with pytest.raises(CanonicalGenesisBlocked) as raised:
            with connection.begin():
                install_canonical_genesis(
                    connection, installer_identity="wrong-database-test"
                )
        assert raised.value.code == "BLOCKED_NON_EMPTY_CANONICAL_DATABASE"
        assert inspect(connection).get_table_names() == []
    finally:
        connection.close()
        engine.dispose()


def test_partial_canonical_schema_is_blocked_without_repair() -> None:
    engine, connection = _sqlite_connection()
    try:
        translated = connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        with translated.begin():
            SCHEMA_METADATA_TABLE.create(translated)
        with pytest.raises(CanonicalGenesisBlocked) as raised:
            with translated.begin():
                install_canonical_genesis(
                    translated, installer_identity="partial-schema-test"
                )
        assert raised.value.code == "BLOCKED_PARTIAL_CANONICAL_SCHEMA"
        assert inspect(translated).get_table_names() == ["schema_metadata"]
    finally:
        connection.close()
        engine.dispose()


def test_extra_legacy_table_is_blocked_without_creating_canonical_tables() -> None:
    engine, connection = _sqlite_connection()
    try:
        legacy_metadata = MetaData()
        Table(
            "research_jobs",
            legacy_metadata,
            Column("id", Integer, primary_key=True),
        )
        with connection.begin():
            legacy_metadata.create_all(connection)
        with pytest.raises(CanonicalGenesisBlocked) as raised:
            with connection.begin():
                install_canonical_genesis(
                    connection, installer_identity="legacy-table-test"
                )
        assert raised.value.code == "BLOCKED_NON_CANONICAL_TABLES"
        assert inspect(connection).get_table_names() == ["research_jobs"]
    finally:
        connection.close()
        engine.dispose()


def test_identity_drift_is_blocked_and_never_rewritten() -> None:
    engine, connection = _sqlite_connection()
    try:
        with connection.begin():
            install_canonical_genesis(
                connection, installer_identity="identity-drift-test"
            )
        translated = connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        with translated.begin():
            translated.execute(
                SCHEMA_METADATA_TABLE.update()
                .where(SCHEMA_METADATA_TABLE.c.metadata_key == GENESIS_METADATA_KEY)
                .values(manifest_digest="0" * 64)
            )

        with pytest.raises(CanonicalGenesisBlocked) as raised:
            with translated.begin():
                install_canonical_genesis(
                    translated, installer_identity="identity-drift-retry"
                )
        assert raised.value.code == "BLOCKED_WRONG_CANONICAL_DATABASE"
        observed = translated.execute(
            select(SCHEMA_METADATA_TABLE.c.manifest_digest)
        ).scalar_one()
        assert observed == "0" * 64
    finally:
        connection.close()
        engine.dispose()


@pytest.mark.parametrize("installer_identity", ["", " leading", "trailing "])
def test_installer_identity_must_be_explicit_and_trimmed(
    installer_identity: str,
) -> None:
    engine, connection = _sqlite_connection()
    try:
        with pytest.raises(CanonicalGenesisBlocked) as raised:
            with connection.begin():
                install_canonical_genesis(
                    connection, installer_identity=installer_identity
                )
        assert raised.value.code == "BLOCKED_INVALID_INSTALLER_IDENTITY"
        assert inspect(connection).get_table_names() == []
    finally:
        connection.close()
        engine.dispose()
