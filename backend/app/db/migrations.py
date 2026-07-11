"""Versioned PostgreSQL schema management for the local Freqtrade AI backend.

The application deliberately keeps migrations small and dependency-free.  PostgreSQL
DDL is transactional, so an upgrade either records the target version after every
contract check succeeds or leaves the database untouched.  Existing *non-empty*
unversioned databases are blocked instead of guessed at or rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Union

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine, URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CheckConstraint, UniqueConstraint

from app import models  # noqa: F401 - imports every model into Base.metadata
from app.core.exceptions import ConfigurationError
from app.models.base import Base


SCHEMA_VERSION = "20260712_01"
VERSION_TABLE = "freqtrade_ai_schema_migrations"


class SchemaMigrationBlocked(ConfigurationError):
    """Raised when a legacy database needs an explicit, data-preserving migration."""


@dataclass(frozen=True)
class SchemaReadiness:
    database_identity: str
    schema_version: Optional[str]
    ready: bool
    problems: tuple[str, ...]


def psql_database_url(database_url: str) -> str:
    """Return a libpq URL without the SQLAlchemy driver or embedded password."""

    try:
        url = make_url(database_url)
    except Exception as exc:  # SQLAlchemy normalizes several URL parsing errors.
        raise ConfigurationError("DATABASE_URL is not a valid SQLAlchemy URL.") from exc
    if not url.drivername.startswith("postgresql"):
        raise ConfigurationError("PostgreSQL DATABASE_URL is required for migrations.")
    return URL.create(
        drivername="postgresql",
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    ).render_as_string(hide_password=False)


def database_identity(engine: Engine) -> str:
    """Expose only dialect, host, port and database name for diagnostics."""

    url = engine.url
    host = url.host or "local"
    port = f":{url.port}" if url.port else ""
    database = url.database or "<default>"
    return f"{engine.dialect.name}://{host}{port}/{database}"


def _require_postgres(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        raise ConfigurationError(
            "Versioned migrations require PostgreSQL; refusing to treat a non-PostgreSQL "
            "database as a successful migration target."
        )


def _expected_tables() -> dict[str, object]:
    # ``strategies.current_version_id`` and ``strategy_versions.strategy_id`` form a
    # legitimate FK cycle, so ``sorted_tables`` emits a warning and is not needed for
    # read-only contract comparison.
    return {name: Base.metadata.tables[name] for name in sorted(Base.metadata.tables)}


def _expected_unique_columns(table: object) -> set[frozenset[str]]:
    unique_sets: set[frozenset[str]] = set()
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            unique_sets.add(frozenset(column.name for column in constraint.columns))
    for column in table.columns:
        if column.unique:
            unique_sets.add(frozenset((column.name,)))
    return unique_sets


def schema_problems(bind: Union[Connection, Engine]) -> list[str]:
    """Compare the live PostgreSQL schema to the SQLAlchemy metadata contract."""

    if isinstance(bind, Engine):
        with bind.connect() as connection:
            return schema_problems(connection)

    problems: list[str] = []
    if bind.dialect.name != "postgresql":
        return ["database dialect is not PostgreSQL"]

    schema_name = bind.execute(text("SELECT current_schema()")).scalar_one()
    inspector = inspect(bind)
    actual_table_names = set(inspector.get_table_names(schema=schema_name))
    for name, table in _expected_tables().items():
        if name not in actual_table_names:
            problems.append(f"missing table: {name}")
            continue

        expected_columns = {column.name for column in table.columns}
        actual_columns = {column["name"] for column in inspector.get_columns(name, schema=schema_name)}
        for column in sorted(expected_columns - actual_columns):
            problems.append(f"missing column: {name}.{column}")
        for column in sorted(actual_columns - expected_columns):
            problems.append(f"unexpected column: {name}.{column}")

        expected_fks = {
            (foreign_key.parent.name, foreign_key.column.table.name, foreign_key.column.name)
            for foreign_key in table.foreign_keys
        }
        actual_fks = {
            (foreign_key["constrained_columns"][0], foreign_key["referred_table"], foreign_key["referred_columns"][0])
            for foreign_key in inspector.get_foreign_keys(name, schema=schema_name)
            if len(foreign_key["constrained_columns"]) == 1
            and len(foreign_key["referred_columns"]) == 1
        }
        for foreign_key in sorted(expected_fks - actual_fks):
            problems.append(
                "missing foreign key: "
                f"{name}.{foreign_key[0]} -> {foreign_key[1]}.{foreign_key[2]}"
            )

        actual_unique = {
            frozenset(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(name, schema=schema_name)
            if constraint.get("column_names")
        }
        for columns in sorted(_expected_unique_columns(table) - actual_unique, key=sorted):
            problems.append(f"missing unique constraint: {name}({','.join(sorted(columns))})")

        expected_checks = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
        }
        actual_checks = {
            constraint.get("name")
            for constraint in inspector.get_check_constraints(name, schema=schema_name)
            if constraint.get("name")
        }
        for check_name in sorted(expected_checks - actual_checks):
            problems.append(f"missing check constraint: {name}.{check_name}")
    return problems


def _create_version_table(connection: Connection) -> None:
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (
                version VARCHAR(64) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )


def _current_version(connection: Connection) -> Optional[str]:
    return connection.execute(
        text(f"SELECT version FROM {VERSION_TABLE} ORDER BY applied_at DESC LIMIT 1")
    ).scalar_one_or_none()


def _nonempty_tables(connection: Connection, table_names: Iterable[str]) -> list[str]:
    nonempty: list[str] = []
    for table_name in table_names:
        exists = connection.execute(
            text(f'SELECT EXISTS (SELECT 1 FROM "{table_name}" LIMIT 1)')
        ).scalar_one()
        if exists:
            nonempty.append(table_name)
    return nonempty


def _drop_empty_legacy_tables(connection: Connection, table_names: Iterable[str]) -> None:
    # These are application-owned tables. CASCADE is safe only after the empty-table
    # guard above has verified that no user/runtime data can be discarded.
    for table_name in table_names:
        connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))


def upgrade_database(engine: Engine) -> str:
    """Upgrade a local PostgreSQL database atomically to ``SCHEMA_VERSION``.

    A schema created by the old unversioned SQL file is accepted only when every
    managed table is empty.  A non-empty legacy database needs a separately planned
    data migration, and this function raises before altering it.
    """

    _require_postgres(engine)
    expected_table_names = set(_expected_tables())
    try:
        with engine.begin() as connection:
            _create_version_table(connection)
            current_version = _current_version(connection)
            if current_version == SCHEMA_VERSION:
                problems = schema_problems(connection)
                if problems:
                    raise SchemaMigrationBlocked(
                        "Recorded schema version does not match ORM metadata: " + "; ".join(problems)
                    )
                return current_version
            if current_version is not None:
                raise SchemaMigrationBlocked(
                    f"Unsupported schema version {current_version!r}; expected {SCHEMA_VERSION!r}."
                )

            schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
            existing_tables = (
                set(inspect(connection).get_table_names(schema=schema_name)) & expected_table_names
            )
            if existing_tables:
                nonempty = _nonempty_tables(connection, existing_tables)
                if nonempty:
                    raise SchemaMigrationBlocked(
                        "Unversioned legacy schema contains data in "
                        f"{', '.join(sorted(nonempty))}. Create a backup and use an explicit "
                        "data-preserving migration; no changes were applied."
                    )
                _drop_empty_legacy_tables(connection, existing_tables)

            Base.metadata.create_all(bind=connection)
            problems = schema_problems(connection)
            if problems:
                raise SchemaMigrationBlocked(
                    "Generated schema does not match ORM metadata: " + "; ".join(problems)
                )
            connection.execute(
                text(f"INSERT INTO {VERSION_TABLE} (version) VALUES (:version)"),
                {"version": SCHEMA_VERSION},
            )
    except SQLAlchemyError as exc:
        raise ConfigurationError(
            f"Database migration failed for {database_identity(engine)}: {exc.__class__.__name__}"
        ) from exc
    return SCHEMA_VERSION


def verify_schema(engine: Engine) -> SchemaReadiness:
    """Return a non-secret readiness result; callers decide the HTTP/CLI failure mode."""

    identity = database_identity(engine)
    if engine.dialect.name != "postgresql":
        return SchemaReadiness(identity, None, False, ("database dialect is not PostgreSQL",))
    try:
        with engine.connect() as connection:
            schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
            if VERSION_TABLE not in inspect(connection).get_table_names(schema=schema_name):
                return SchemaReadiness(identity, None, False, ("migration version table is missing",))
            version = _current_version(connection)
            problems = schema_problems(connection)
    except SQLAlchemyError as exc:
        return SchemaReadiness(identity, None, False, (f"database query failed: {exc.__class__.__name__}",))
    if version != SCHEMA_VERSION:
        problems.append(f"schema version is {version or '<missing>'}, expected {SCHEMA_VERSION}")
    return SchemaReadiness(identity, version, not problems, tuple(problems))
