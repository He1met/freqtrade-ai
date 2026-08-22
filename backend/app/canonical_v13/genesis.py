"""Fail-closed genesis installer and offline PostgreSQL evidence generators."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from sqlalchemy import Connection, func, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateSchema, CreateTable

from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_BUSINESS_TABLE_NAMES,
    CANONICAL_DATABASE_PURPOSE,
    CANONICAL_GENESIS_VERSION,
    CANONICAL_LEGACY_IMPORT_MODE,
    CANONICAL_MANIFEST_DIGEST,
    CANONICAL_MANIFEST_KEY,
    CANONICAL_PRODUCTION_DEFAULT,
    CANONICAL_TABLE_NAMES,
    CANONICAL_TRADING_CAPABILITY,
    READER_IDENTITIES,
    READER_TABLE_ALLOWLIST,
    TABLE_MANIFEST_BY_NAME,
    WRITER_IDENTITIES,
    WRITER_READ_ALLOWLIST,
    WRITER_TABLE_ALLOWLIST,
    assert_canonical_manifest,
)
from app.canonical_v13.models import (
    CANONICAL_TABLES,
    SCHEMA_METADATA_TABLE,
    CanonicalBase,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping
from app.canonical_v13.gate_receipt_upgrade import GATE_GUARD_FUNCTION_NAMES


GENESIS_METADATA_KEY: Final = "canonical-v13-genesis"
CANONICAL_GUARD_FUNCTION_NAMES: Final = (
    *GATE_GUARD_FUNCTION_NAMES,
    "guard_runtime_image_acceptances_append_only",
    "guard_deployments_disable_evidence",
)


@dataclass(frozen=True)
class CanonicalGenesisIdentity:
    database_purpose: str
    business_schema: str
    genesis_version: str
    manifest_key: str
    manifest_digest: str
    legacy_import_mode: str
    production_default_target: str
    production_default_count: str
    production_default_cap: str
    trading_capability: str

    def as_database_values(self) -> dict[str, str]:
        return {
            "database_purpose": self.database_purpose,
            "business_schema": self.business_schema,
            "genesis_version": self.genesis_version,
            "manifest_key": self.manifest_key,
            "manifest_digest": self.manifest_digest,
            "legacy_import_mode": self.legacy_import_mode,
            "production_default_target": self.production_default_target,
            "production_default_count": self.production_default_count,
            "production_default_cap": self.production_default_cap,
            "trading_capability": self.trading_capability,
        }


CANONICAL_GENESIS_IDENTITY: Final = CanonicalGenesisIdentity(
    database_purpose=CANONICAL_DATABASE_PURPOSE,
    business_schema=CANONICAL_BUSINESS_SCHEMA,
    genesis_version=CANONICAL_GENESIS_VERSION,
    manifest_key=CANONICAL_MANIFEST_KEY,
    manifest_digest=CANONICAL_MANIFEST_DIGEST,
    legacy_import_mode=CANONICAL_LEGACY_IMPORT_MODE,
    production_default_target=CANONICAL_PRODUCTION_DEFAULT,
    production_default_count=CANONICAL_PRODUCTION_DEFAULT,
    production_default_cap=CANONICAL_PRODUCTION_DEFAULT,
    trading_capability=CANONICAL_TRADING_CAPABILITY,
)


@dataclass(frozen=True)
class GenesisInstallResult:
    created: bool
    repeat_noop: bool
    manifest_digest: str
    table_names: tuple[str, ...]
    business_row_count: int
    installer_identity: str


@dataclass(frozen=True)
class GenesisVerification:
    accepted: bool
    problems: tuple[str, ...]
    table_names: tuple[str, ...]
    business_row_count: int | None
    manifest_digest: str | None


class CanonicalGenesisBlocked(RuntimeError):
    """A stable fail-closed genesis error with a machine-readable reason code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _effective_connection(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _inspection_schema(connection: Connection) -> str | None:
    return None if connection.dialect.name == "sqlite" else CANONICAL_BUSINESS_SCHEMA


def _existing_tables(connection: Connection) -> tuple[str, ...]:
    return tuple(
        sorted(
            inspect(connection).get_table_names(schema=_inspection_schema(connection))
        )
    )


def _postgresql_user_objects(connection: Connection) -> tuple[str, ...]:
    """List user-created objects that would make the database non-canonical.

    PostgreSQL's ``public`` schema exists in a fresh database, so the namespace
    itself is allowed.  Objects in it are not.  The canonical schema may contain
    only the exact table/index objects emitted by this metadata and the three
    fail-closed gate guard functions; views, sequences, other functions, and
    standalone user types remain forbidden.
    """

    if connection.dialect.name != "postgresql":
        return ()
    rows = connection.execute(
        text(
            """
            WITH user_namespaces AS (
              SELECT oid, nspname
              FROM pg_catalog.pg_namespace
              WHERE nspname NOT IN ('pg_catalog', 'information_schema')
                AND nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
            )
            SELECT 'SCHEMA' AS object_kind, n.nspname AS schema_name,
                   n.nspname AS object_name, NULL::text AS relation_kind
            FROM user_namespaces n
            WHERE n.nspname NOT IN ('public', :canonical_schema)
            UNION ALL
            SELECT 'RELATION', n.nspname, c.relname, c.relkind::text
            FROM pg_catalog.pg_class c
            JOIN user_namespaces n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'i', 'S', 'v', 'm', 'f')
            UNION ALL
            SELECT 'FUNCTION', n.nspname, p.proname, NULL::text
            FROM pg_catalog.pg_proc p
            JOIN user_namespaces n ON n.oid = p.pronamespace
            UNION ALL
            SELECT 'TYPE', n.nspname, t.typname, NULL::text
            FROM pg_catalog.pg_type t
            JOIN user_namespaces n ON n.oid = t.typnamespace
            WHERE t.typrelid = 0 AND t.typtype IN ('d', 'e', 'r')
            ORDER BY 1, 2, 3
            """
        ),
        {"canonical_schema": CANONICAL_BUSINESS_SCHEMA},
    ).mappings()

    expected_indexes = {
        constraint.name
        for table in CanonicalBase.metadata.tables.values()
        for constraint in table.constraints
        if constraint.name is not None
        and constraint.__class__.__name__
        in {"PrimaryKeyConstraint", "UniqueConstraint"}
    }
    expected_indexes.update(
        index.name
        for table in CanonicalBase.metadata.tables.values()
        for index in table.indexes
        if index.name is not None
    )
    canonical_tables_complete = set(_existing_tables(connection)) == set(
        CANONICAL_TABLE_NAMES
    )
    expected_guard_functions = set(CANONICAL_GUARD_FUNCTION_NAMES)
    problems: list[str] = []
    for row in rows:
        kind = str(row["object_kind"])
        schema = str(row["schema_name"])
        name = str(row["object_name"])
        relation_kind = row["relation_kind"]
        allowed = False
        if kind == "RELATION" and schema == CANONICAL_BUSINESS_SCHEMA:
            if relation_kind in {"r", "p"}:
                allowed = name in CANONICAL_TABLE_NAMES
            elif relation_kind == "i":
                allowed = name in expected_indexes
        elif kind == "FUNCTION" and schema == CANONICAL_BUSINESS_SCHEMA:
            allowed = canonical_tables_complete and name in expected_guard_functions
        if not allowed:
            problems.append(f"{kind.lower()}:{schema}.{name}")
    return tuple(problems)


def _database_isolation_problems(connection: Connection) -> tuple[str, ...]:
    return tuple(
        f"unexpected user object in canonical database: {value}"
        for value in _postgresql_user_objects(connection)
    )


def _validate_existing_table_shape(existing_tables: tuple[str, ...]) -> None:
    existing = set(existing_tables)
    expected = set(CANONICAL_TABLE_NAMES)
    if not existing:
        return
    extras = sorted(existing - expected)
    if extras:
        raise CanonicalGenesisBlocked(
            "BLOCKED_NON_CANONICAL_TABLES",
            f"unexpected tables in canonical schema: {extras}",
        )
    missing = sorted(expected - existing)
    if missing:
        raise CanonicalGenesisBlocked(
            "BLOCKED_PARTIAL_CANONICAL_SCHEMA",
            f"non-empty schema is missing canonical tables: {missing}",
        )


def _identity_rows(connection: Connection) -> tuple[dict[str, object], ...]:
    result = connection.execute(
        select(SCHEMA_METADATA_TABLE).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == GENESIS_METADATA_KEY
        )
    )
    return tuple(dict(row._mapping) for row in result)


def _identity_problems(
    row: dict[str, object],
    *,
    accepted_manifest_digests: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    expected = CANONICAL_GENESIS_IDENTITY.as_database_values()
    accepted_digests = accepted_manifest_digests or (CANONICAL_MANIFEST_DIGEST,)
    return tuple(
        f"{key} expected={expected_value!r} observed={row.get(key)!r}"
        for key, expected_value in expected.items()
        if (
            row.get(key) not in accepted_digests
            if key == "manifest_digest"
            else row.get(key) != expected_value
        )
    )


def canonical_business_row_count(connection: Connection) -> int:
    """Count every non-identity row without reading any external database."""

    return sum(
        int(
            connection.execute(
                select(func.count()).select_from(CANONICAL_TABLES[table_name])
            ).scalar_one()
        )
        for table_name in CANONICAL_BUSINESS_TABLE_NAMES
    )


def verify_canonical_genesis(
    connection: Connection,
    *,
    require_zero_business_rows: bool = False,
    include_business_row_count: bool = False,
    accepted_manifest_digests: tuple[str, ...] | None = None,
) -> GenesisVerification:
    """Verify exact tables and genesis identity; never repairs observed drift."""

    assert_canonical_manifest()
    effective = _effective_connection(connection)
    table_names = _existing_tables(effective)
    expected = set(CANONICAL_TABLE_NAMES)
    observed = set(table_names)
    problems: list[str] = []
    problems.extend(_database_isolation_problems(effective))
    extras = sorted(observed - expected)
    missing = sorted(expected - observed)
    if extras:
        problems.append(f"unexpected canonical-schema tables: {extras}")
    if missing:
        problems.append(f"missing canonical tables: {missing}")

    business_row_count: int | None = None
    manifest_digest: str | None = None
    if not extras and not missing:
        rows = _identity_rows(effective)
        if len(rows) != 1:
            problems.append(
                f"expected one {GENESIS_METADATA_KEY!r} identity row, found {len(rows)}"
            )
        else:
            identity_drift = _identity_problems(
                rows[0],
                accepted_manifest_digests=accepted_manifest_digests,
            )
            problems.extend(identity_drift)
            manifest_digest = str(rows[0]["manifest_digest"])
        if require_zero_business_rows or include_business_row_count:
            business_row_count = canonical_business_row_count(effective)
        if require_zero_business_rows and business_row_count != 0:
            problems.append(
                f"genesis acceptance requires business rows 0, found {business_row_count}"
            )
    return GenesisVerification(
        accepted=not problems,
        problems=tuple(problems),
        table_names=table_names,
        business_row_count=business_row_count,
        manifest_digest=manifest_digest,
    )


def install_canonical_genesis(
    connection: Connection,
    *,
    installer_identity: str,
) -> GenesisInstallResult:
    """Install from an empty schema or prove an exact repeated-install no-op.

    The caller owns the transaction. The function never imports application session
    configuration and therefore cannot discover or connect to another database.
    """

    if not installer_identity or installer_identity.strip() != installer_identity:
        raise CanonicalGenesisBlocked(
            "BLOCKED_INVALID_INSTALLER_IDENTITY",
            "installer_identity must be explicit, non-empty, and trimmed",
        )
    assert_canonical_manifest()
    effective = _effective_connection(connection)
    isolation_problems = _database_isolation_problems(effective)
    if isolation_problems:
        raise CanonicalGenesisBlocked(
            "BLOCKED_NON_EMPTY_CANONICAL_DATABASE",
            "; ".join(isolation_problems),
        )
    existing_tables = _existing_tables(effective)
    _validate_existing_table_shape(existing_tables)

    if existing_tables:
        verification = verify_canonical_genesis(
            effective,
            require_zero_business_rows=False,
            include_business_row_count=True,
        )
        if not verification.accepted:
            raise CanonicalGenesisBlocked(
                "BLOCKED_WRONG_CANONICAL_DATABASE",
                "; ".join(verification.problems),
            )
        rows = _identity_rows(effective)
        return GenesisInstallResult(
            created=False,
            repeat_noop=True,
            manifest_digest=CANONICAL_MANIFEST_DIGEST,
            table_names=tuple(sorted(CANONICAL_TABLE_NAMES)),
            business_row_count=verification.business_row_count or 0,
            installer_identity=str(rows[0]["installer_identity"]),
        )

    if effective.dialect.name == "postgresql":
        effective.execute(CreateSchema(CANONICAL_BUSINESS_SCHEMA, if_not_exists=True))
    CanonicalBase.metadata.create_all(bind=effective, checkfirst=False)
    if effective.dialect.name == "postgresql":
        from app.canonical_v13.gate_receipt_upgrade import (  # noqa: PLC0415
            install_gate_receipt_triggers,
        )
        from app.canonical_v13.runtime_image_upgrade import (  # noqa: PLC0415
            install_runtime_image_trigger,
        )
        from app.canonical_v13.deployment_rollover_upgrade import (  # noqa: PLC0415
            install_deployment_rollover_trigger,
        )

        install_gate_receipt_triggers(effective)
        install_runtime_image_trigger(effective)
        install_deployment_rollover_trigger(effective)
    effective.execute(
        SCHEMA_METADATA_TABLE.insert().values(
            metadata_key=GENESIS_METADATA_KEY,
            **CANONICAL_GENESIS_IDENTITY.as_database_values(),
            installed_at=datetime.now(timezone.utc),
            installer_identity=installer_identity,
        )
    )
    verification = verify_canonical_genesis(effective, require_zero_business_rows=True)
    if not verification.accepted:
        raise CanonicalGenesisBlocked(
            "BLOCKED_GENESIS_VERIFICATION",
            "; ".join(verification.problems),
        )
    return GenesisInstallResult(
        created=True,
        repeat_noop=False,
        manifest_digest=CANONICAL_MANIFEST_DIGEST,
        table_names=tuple(sorted(CANONICAL_TABLE_NAMES)),
        business_row_count=verification.business_row_count or 0,
        installer_identity=installer_identity,
    )


def render_postgresql_genesis_ddl(
    role_mapping: CanonicalRoleMapping | None = None,
) -> str:
    """Render PostgreSQL genesis DDL offline; no engine or connection is created."""

    assert_canonical_manifest()
    dialect = postgresql.dialect()
    statements = [
        str(
            CreateSchema(CANONICAL_BUSINESS_SCHEMA, if_not_exists=True).compile(
                dialect=dialect
            )
        ),
    ]
    for table in CanonicalBase.metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)).strip())
        for index in sorted(table.indexes, key=lambda value: value.name or ""):
            statements.append(str(CreateIndex(index).compile(dialect=dialect)).strip())
    from app.canonical_v13.gate_receipt_upgrade import (  # noqa: PLC0415
        gate_receipt_trigger_statements,
    )

    statements.extend(gate_receipt_trigger_statements())
    from app.canonical_v13.runtime_image_upgrade import (  # noqa: PLC0415
        runtime_image_trigger_statements,
    )

    statements.extend(runtime_image_trigger_statements())
    from app.canonical_v13.deployment_rollover_upgrade import (  # noqa: PLC0415
        deployment_rollover_trigger_statements,
    )

    statements.extend(deployment_rollover_trigger_statements())
    statements.extend(
        render_postgresql_owner_sql(role_mapping).rstrip(";\n").split(";\n")
    )
    return ";\n\n".join(statement.rstrip(";") for statement in statements) + ";\n"


def render_postgresql_owner_sql(
    role_mapping: CanonicalRoleMapping | None = None,
) -> str:
    """Render owner transfer only, ordered after every create/index operation."""

    resolved = role_mapping or CanonicalRoleMapping.identity()
    owner = resolved.physical("canonical_schema_owner")
    statements = [
        f"ALTER TABLE {CANONICAL_BUSINESS_SCHEMA}.{table.name} " f"OWNER TO {owner}"
        for table in CanonicalBase.metadata.sorted_tables
    ]
    statements.extend(
        f"ALTER FUNCTION {CANONICAL_BUSINESS_SCHEMA}.{function_name}() OWNER TO {owner}"
        for function_name in CANONICAL_GUARD_FUNCTION_NAMES
    )
    statements.append(f"ALTER SCHEMA {CANONICAL_BUSINESS_SCHEMA} OWNER TO {owner}")
    return ";\n".join(statements) + ";\n"


def postgresql_acl_statements(
    role_mapping: CanonicalRoleMapping | None = None,
    *,
    guard_function_names: tuple[str, ...] = CANONICAL_GUARD_FUNCTION_NAMES,
) -> tuple[str, ...]:
    """Return an exact per-table ACL plan; wildcard table grants are forbidden."""

    assert_canonical_manifest()
    resolved = role_mapping or CanonicalRoleMapping.identity()
    roles = tuple(
        resolved.physical(role) for role in (*WRITER_IDENTITIES, *READER_IDENTITIES)
    )
    statements: list[str] = [
        f"REVOKE ALL PRIVILEGES ON SCHEMA {CANONICAL_BUSINESS_SCHEMA} FROM PUBLIC"
    ]
    statements.extend(
        f"GRANT USAGE ON SCHEMA {CANONICAL_BUSINESS_SCHEMA} TO {role}" for role in roles
    )
    for table_name in CANONICAL_TABLE_NAMES:
        qualified = f"{CANONICAL_BUSINESS_SCHEMA}.{table_name}"
        statements.append(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM PUBLIC")
        statements.extend(
            f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {role}" for role in roles
        )
    statements.extend(postgresql_owner_table_grant_statements(resolved))
    owner = resolved.physical("canonical_schema_owner")
    for function_name in guard_function_names:
        qualified = f"{CANONICAL_BUSINESS_SCHEMA}.{function_name}()"
        statements.append(f"REVOKE ALL PRIVILEGES ON FUNCTION {qualified} FROM PUBLIC")
        statements.extend(
            f"REVOKE ALL PRIVILEGES ON FUNCTION {qualified} FROM {role}"
            for role in roles
        )
        statements.append(f"GRANT EXECUTE ON FUNCTION {qualified} TO {owner}")

    for writer, table_names in WRITER_TABLE_ALLOWLIST.items():
        physical_writer = resolved.physical(writer)
        for table_name in table_names:
            privileges = TABLE_MANIFEST_BY_NAME[table_name].writer_privileges
            statements.append(
                "GRANT "
                + ", ".join(privileges)
                + f" ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
                + f"TO {physical_writer}"
            )
    for writer, table_names in WRITER_READ_ALLOWLIST.items():
        physical_writer = resolved.physical(writer)
        statements.extend(
            f"GRANT SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
            f"TO {physical_writer}"
            for table_name in table_names
        )
    for reader, table_names in READER_TABLE_ALLOWLIST.items():
        physical_reader = resolved.physical(reader)
        statements.extend(
            f"GRANT SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
            f"TO {physical_reader}"
            for table_name in table_names
        )
    return tuple(statements)


def postgresql_owner_table_grant_statements(
    role_mapping: CanonicalRoleMapping | None = None,
) -> tuple[str, ...]:
    """Restore standard table-owner rights after the exact ACL reset."""

    resolved = role_mapping or CanonicalRoleMapping.identity()
    owner = resolved.physical("canonical_schema_owner")
    return tuple(
        "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        "ON TABLE "
        f"{CANONICAL_BUSINESS_SCHEMA}.{table_name} TO {owner}"
        for table_name in CANONICAL_TABLE_NAMES
    )


def render_postgresql_acl_sql(
    role_mapping: CanonicalRoleMapping | None = None,
) -> str:
    return ";\n".join(postgresql_acl_statements(role_mapping)) + ";\n"


def postgresql_acl_problems(
    sql: str,
    role_mapping: CanonicalRoleMapping | None = None,
) -> tuple[str, ...]:
    """Static exact-plan verification for reviewed PostgreSQL ACL evidence."""

    problems: list[str] = []
    expected = render_postgresql_acl_sql(role_mapping)
    if sql != expected:
        problems.append("ACL text differs from the exact canonical allowlist")
    upper = sql.upper()
    if "GRANT " in upper and " ON ALL TABLES " in upper:
        problems.append("wildcard GRANT ON ALL TABLES is forbidden")
    if "SECURITY DEFINER" in upper:
        problems.append("SECURITY DEFINER is forbidden")
    if "GRANT ALL" in upper:
        problems.append("GRANT ALL is forbidden")
    if " TO PUBLIC" in upper:
        problems.append("grant to PUBLIC is forbidden")
    for forbidden in (
        "FREQTRADE_AI_DESIGN_LAB",
        "20260813_47",
        "STRATEGY_PLATFORM_MIGRATION_",
        "OKX_DEMO_",
    ):
        if forbidden in upper:
            problems.append(f"legacy token {forbidden} is forbidden")
    return tuple(problems)


def assert_postgresql_acl_sql(
    sql: str,
    role_mapping: CanonicalRoleMapping | None = None,
) -> None:
    problems = postgresql_acl_problems(sql, role_mapping)
    if problems:
        raise CanonicalGenesisBlocked("BLOCKED_ACL_DESIGN_DRIFT", "; ".join(problems))


__all__ = [
    "CANONICAL_GENESIS_IDENTITY",
    "CanonicalGenesisBlocked",
    "CanonicalGenesisIdentity",
    "GENESIS_METADATA_KEY",
    "GATE_GUARD_FUNCTION_NAMES",
    "GenesisInstallResult",
    "GenesisVerification",
    "assert_postgresql_acl_sql",
    "canonical_business_row_count",
    "install_canonical_genesis",
    "postgresql_acl_problems",
    "postgresql_acl_statements",
    "postgresql_owner_table_grant_statements",
    "render_postgresql_acl_sql",
    "render_postgresql_genesis_ddl",
    "render_postgresql_owner_sql",
    "verify_canonical_genesis",
]
