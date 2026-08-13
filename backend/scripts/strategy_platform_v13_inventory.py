#!/usr/bin/env python3
"""Collect a canonical, read-only PostgreSQL inventory for V1.3 Task 1.

This command records database/catalog facts only.  It never reads credential
values, never writes database state, and never treats a clean point-in-time
session snapshot as authorization to run a migration.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine


REPORT_SCHEMA_VERSION = "strategy-platform-v13-postgresql-inventory-v1"
VERSION_TABLE = "freqtrade_ai_schema_migrations"

_SIMPLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_TABLE_NAMES = frozenset(
    {
        "okx_demo_attestation_secrets",
        "okx_demo_operator_consent_secrets",
    }
)
_SENSITIVE_IDENTIFIER = re.compile(
    r"(?:^|_)(?:secrets?|credentials?|passwords?|passphrases?|tokens?|"
    r"api_keys?|access_keys?|private_keys?|auth(?:entication|orization)?)(?:_|$)"
)
_ID_RANGE_TYPES = frozenset(
    {
        "bigint",
        "character",
        "character varying",
        "integer",
        "smallint",
        "text",
        "uuid",
    }
)


class InventoryBlocked(RuntimeError):
    """Raised when the command cannot prove its read-only inventory contract."""


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any, *, pretty: bool = False) -> str:
    """Return one deterministic UTF-8 JSON representation of ``value``."""

    return json.dumps(
        value,
        allow_nan=False,
        default=_json_default,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=True,
    )


def canonical_sha256(value: Any) -> str:
    """Hash the compact canonical representation, never a pretty rendering."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_simple_identifier(value: str, *, label: str = "identifier") -> str:
    """Validate the only identifier supplied by the CLI (the schema name)."""

    if not _SIMPLE_IDENTIFIER.fullmatch(value):
        raise InventoryBlocked(f"{label} is not a simple PostgreSQL identifier")
    return value


def _quote(connection: Connection, identifier: str) -> str:
    # Catalog identifiers may legitimately need quoting.  Always quoting them
    # keeps generated count-only SQL independent from search_path and keywords.
    return connection.dialect.identifier_preparer.quote_identifier(identifier)


def _qualified(connection: Connection, schema: str, table: str) -> str:
    return f"{_quote(connection, schema)}.{_quote(connection, table)}"


def _is_sensitive_identifier(identifier: str) -> bool:
    lowered = identifier.lower()
    return lowered in _SENSITIVE_TABLE_NAMES or _SENSITIVE_IDENTIFIER.search(lowered) is not None


def _sensitive_relations(columns: Mapping[str, Mapping[str, str]]) -> set[str]:
    """Fail closed from catalog names without reading protected rows."""

    return {
        table_name
        for table_name, table_columns in columns.items()
        if _is_sensitive_identifier(table_name)
        or any(_is_sensitive_identifier(column) for column in table_columns)
    }


def _dict_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _require_schema(connection: Connection, schema: str) -> None:
    exists = connection.execute(
        text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = :schema"
            ")"
        ),
        {"schema": schema},
    ).scalar_one()
    if not bool(exists):
        raise InventoryBlocked("requested PostgreSQL schema does not exist")


def _schema_marker(connection: Connection, schema: str) -> dict[str, Any]:
    qualified_name = f"{schema}.{VERSION_TABLE}"
    exists = bool(
        connection.execute(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :table_name "
                "  AND c.relkind IN ('r', 'p')"
                ")"
            ),
            {"schema": schema, "table_name": VERSION_TABLE},
        ).scalar_one()
    )
    if not exists:
        return {
            "current": None,
            "exists": False,
            "history": [],
            "row_count": 0,
            "table": qualified_name,
        }

    rows = connection.execute(
        text(
            "SELECT version, applied_at "
            f"FROM {_qualified(connection, schema, VERSION_TABLE)} "
            "ORDER BY applied_at, version"
        )
    ).mappings().all()
    history = [
        {"applied_at": row["applied_at"], "version": row["version"]}
        for row in rows
    ]
    return {
        "current": history[-1] if history else None,
        "exists": True,
        "history": history,
        "row_count": len(history),
        "table": qualified_name,
    }


def _columns_by_table(connection: Connection, schema: str) -> dict[str, dict[str, str]]:
    rows = connection.execute(
        text(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = :schema "
            "ORDER BY table_name, ordinal_position"
        ),
        {"schema": schema},
    ).mappings()
    columns: dict[str, dict[str, str]] = {}
    for row in rows:
        columns.setdefault(row["table_name"], {})[row["column_name"]] = row[
            "data_type"
        ]
    return columns


def _table_inventory(
    connection: Connection,
    schema: str,
    *,
    columns: Mapping[str, Mapping[str, str]] | None = None,
    sensitive_relations: set[str] | None = None,
) -> list[dict[str, Any]]:
    columns = dict(columns) if columns is not None else _columns_by_table(connection, schema)
    sensitive_relations = (
        set(sensitive_relations)
        if sensitive_relations is not None
        else _sensitive_relations(columns)
    )
    relations = connection.execute(
        text(
            "SELECT c.relname AS table_name, owner.rolname AS owner, c.relkind, "
            "       pg_total_relation_size(c.oid)::bigint AS total_bytes "
            "FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            "JOIN pg_catalog.pg_roles AS owner ON owner.oid = c.relowner "
            "WHERE n.nspname = :schema AND c.relkind IN ('r', 'p') "
            "ORDER BY c.relname"
        ),
        {"schema": schema},
    ).mappings()

    inventory: list[dict[str, Any]] = []
    for relation in relations:
        table_name = relation["table_name"]
        if table_name in sensitive_relations or _is_sensitive_identifier(table_name):
            # Do not even count protected rows.  The table identity and explicit
            # exclusion are the complete evidence for this inventory surface.
            inventory.append(
                {
                    "id_range": None,
                    "inventory_status": "EXCLUDED_SENSITIVE_TABLE",
                    "row_count": None,
                    "table": table_name,
                }
            )
            continue

        qualified = _qualified(connection, schema, table_name)
        id_type = columns.get(table_name, {}).get("id")
        if id_type in _ID_RANGE_TYPES:
            id_column = _quote(connection, "id")
            row = connection.execute(
                text(
                    "SELECT count(*)::bigint AS row_count, "
                    f"(SELECT {id_column}::text FROM {qualified} "
                    f" WHERE {id_column} IS NOT NULL "
                    f" ORDER BY {id_column} ASC LIMIT 1) AS min_id, "
                    f"(SELECT {id_column}::text FROM {qualified} "
                    f" WHERE {id_column} IS NOT NULL "
                    f" ORDER BY {id_column} DESC LIMIT 1) AS max_id "
                    f"FROM {qualified}"
                )
            ).mappings().one()
            id_range = {"max": row["max_id"], "min": row["min_id"]}
            id_range_status = "INVENTORIED"
        else:
            row = connection.execute(
                text(f"SELECT count(*)::bigint AS row_count FROM {qualified}")
            ).mappings().one()
            id_range = None
            id_range_status = "ABSENT" if id_type is None else "UNSUPPORTED_TYPE"

        inventory.append(
            {
                "id_column_data_type": id_type,
                "id_range": id_range,
                "id_range_status": id_range_status,
                "inventory_status": "INVENTORIED",
                "owner": relation["owner"],
                "relation_kind": (
                    "PARTITIONED_TABLE" if relation["relkind"] == "p" else "TABLE"
                ),
                "row_count": int(row["row_count"]),
                "table": table_name,
                "total_bytes": int(relation["total_bytes"]),
            }
        )
    return inventory


def _constraints(
    connection: Connection, schema: str, *, sensitive_relations: set[str] | None = None
) -> list[dict[str, Any]]:
    sensitive_relations = sensitive_relations or set()
    rows = connection.execute(
        text(
            "SELECT rel.relname AS table_name, con.conname AS constraint_name, "
            "       con.contype AS constraint_type, con.convalidated AS validated, "
            "       con.condeferrable AS deferrable, "
            "       con.condeferred AS initially_deferred "
            "FROM pg_catalog.pg_constraint AS con "
            "JOIN pg_catalog.pg_class AS rel ON rel.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = rel.relnamespace "
            "WHERE n.nspname = :schema "
            "ORDER BY rel.relname, con.conname"
        ),
        {"schema": schema},
    ).mappings()
    type_names = {
        "c": "CHECK",
        "f": "FOREIGN_KEY",
        "p": "PRIMARY_KEY",
        "u": "UNIQUE",
        "x": "EXCLUSION",
    }
    return [
        {
            "deferrable": bool(row["deferrable"]),
            "definition": None,
            "initially_deferred": bool(row["initially_deferred"]),
            "inventory_status": (
                "EXCLUDED_SENSITIVE_TABLE"
                if row["table_name"] in sensitive_relations
                else "INVENTORIED_CATALOG_METADATA_ONLY"
            ),
            "name": row["constraint_name"],
            "table": row["table_name"],
            "type": type_names.get(row["constraint_type"], row["constraint_type"]),
            "validated": bool(row["validated"]),
        }
        for row in rows
    ]


def _foreign_key_metadata(
    connection: Connection, schema: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT child.relname AS child_table, con.conname AS constraint_name, "
            "       parent_ns.nspname AS parent_schema, "
            "       parent.relname AS parent_table, "
            "       ARRAY(SELECT child_attr.attname "
            "             FROM unnest(con.conkey) WITH ORDINALITY AS key(attnum, ord) "
            "             JOIN pg_catalog.pg_attribute AS child_attr "
            "               ON child_attr.attrelid = con.conrelid "
            "              AND child_attr.attnum = key.attnum "
            "             ORDER BY key.ord) AS child_columns, "
            "       ARRAY(SELECT parent_attr.attname "
            "             FROM unnest(con.confkey) WITH ORDINALITY AS key(attnum, ord) "
            "             JOIN pg_catalog.pg_attribute AS parent_attr "
            "               ON parent_attr.attrelid = con.confrelid "
            "              AND parent_attr.attnum = key.attnum "
            "             ORDER BY key.ord) AS parent_columns, "
            "       con.confmatchtype AS match_type, "
            "       con.convalidated AS validated "
            "FROM pg_catalog.pg_constraint AS con "
            "JOIN pg_catalog.pg_class AS child ON child.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace AS child_ns "
            "  ON child_ns.oid = child.relnamespace "
            "JOIN pg_catalog.pg_class AS parent ON parent.oid = con.confrelid "
            "JOIN pg_catalog.pg_namespace AS parent_ns "
            "  ON parent_ns.oid = parent.relnamespace "
            "WHERE con.contype = 'f' AND child_ns.nspname = :schema "
            "ORDER BY child.relname, con.conname"
        ),
        {"schema": schema},
    ).mappings()
    return _dict_rows(rows)


def build_fk_orphan_sql(
    connection: Connection,
    *,
    child_schema: str,
    child_table: str,
    child_columns: Sequence[str],
    parent_schema: str,
    parent_table: str,
    parent_columns: Sequence[str],
    match_type: str = "s",
) -> str:
    """Build a quoted, count-only orphan query from PostgreSQL catalog data."""

    if not child_columns or len(child_columns) != len(parent_columns):
        raise InventoryBlocked("foreign key catalog metadata is incomplete")
    if match_type not in {"s", "f"}:
        # PostgreSQL does not implement MATCH PARTIAL.  Refuse to under-count if
        # a future server/catalog ever exposes semantics this tool cannot prove.
        raise InventoryBlocked("foreign key match semantics are unsupported")

    child = _qualified(connection, child_schema, child_table)
    parent = _qualified(connection, parent_schema, parent_table)
    child_refs = [f"child.{_quote(connection, column)}" for column in child_columns]
    all_nonnull = " AND ".join(f"{column} IS NOT NULL" for column in child_refs)
    any_nonnull = " OR ".join(f"{column} IS NOT NULL" for column in child_refs)
    joins = " AND ".join(
        f"parent.{_quote(connection, parent_column)} = "
        f"child.{_quote(connection, child_column)}"
        for child_column, parent_column in zip(child_columns, parent_columns)
    )
    missing_parent = (
        f"NOT EXISTS (SELECT 1 FROM {parent} AS parent WHERE {joins})"
    )
    if match_type == "f":
        predicate = (
            f"((({any_nonnull}) AND NOT ({all_nonnull})) "
            f"OR (({all_nonnull}) AND {missing_parent}))"
        )
    else:
        predicate = f"({all_nonnull}) AND {missing_parent}"
    return (
        "SELECT count(*)::bigint AS orphan_count "
        f"FROM {child} AS child WHERE {predicate}"
    )


def _foreign_key_orphans(
    connection: Connection, schema: str, *, sensitive_relations: set[str] | None = None
) -> list[dict[str, Any]]:
    sensitive_relations = sensitive_relations or set()
    result: list[dict[str, Any]] = []
    match_names = {"f": "FULL", "s": "SIMPLE"}
    for fk in _foreign_key_metadata(connection, schema):
        child_columns = list(fk["child_columns"])
        parent_columns = list(fk["parent_columns"])
        item: dict[str, Any] = {
            "child_columns": child_columns,
            "child_table": fk["child_table"],
            "constraint": fk["constraint_name"],
            "constraint_validated": bool(fk["validated"]),
            "match_type": match_names.get(fk["match_type"], fk["match_type"]),
            "parent_columns": parent_columns,
            "parent_schema": fk["parent_schema"],
            "parent_table": fk["parent_table"],
        }
        if (
            fk["child_table"] in sensitive_relations
            or fk["parent_table"] in sensitive_relations
            or _is_sensitive_identifier(fk["child_table"])
            or _is_sensitive_identifier(fk["parent_table"])
        ):
            item.update(
                {
                    "inventory_status": "EXCLUDED_SENSITIVE_TABLE",
                    "orphan_count": None,
                }
            )
        else:
            orphan_sql = build_fk_orphan_sql(
                connection,
                child_columns=child_columns,
                child_schema=schema,
                child_table=fk["child_table"],
                match_type=fk["match_type"],
                parent_columns=parent_columns,
                parent_schema=fk["parent_schema"],
                parent_table=fk["parent_table"],
            )
            orphan_count = connection.exec_driver_sql(orphan_sql).scalar_one()
            item.update(
                {
                    "inventory_status": "INVENTORIED",
                    "orphan_count": int(orphan_count),
                }
            )
        result.append(item)
    return result


def _session_safety(connection: Connection, schema: str) -> dict[str, Any]:
    identity = connection.execute(
        text(
            "SELECT current_database() AS database_name, "
            "       current_user AS current_role, "
            "       transaction_timestamp() AS observed_at, "
            "       current_setting('transaction_read_only') "
            "         AS transaction_read_only, "
            "       current_setting('transaction_isolation') "
            "         AS transaction_isolation, "
            "       current_setting('server_version') AS server_version"
        )
    ).mappings().one()
    sessions = connection.execute(
        text(
            "SELECT count(*) FILTER (WHERE pid <> pg_backend_pid())::bigint "
            "         AS other_sessions, "
            "       count(*) FILTER (WHERE pid <> pg_backend_pid() "
            "         AND state IS NULL)::bigint AS other_sessions_redacted, "
            "       count(*) FILTER (WHERE pid <> pg_backend_pid() "
            "         AND state = 'active')::bigint AS other_active_sessions, "
            "       count(*) FILTER (WHERE pid <> pg_backend_pid() "
            "         AND xact_start IS NOT NULL)::bigint AS other_open_transactions, "
            "       count(*) FILTER (WHERE pid <> pg_backend_pid() "
            "         AND state = 'idle in transaction')::bigint "
            "         AS other_idle_in_transaction, "
            "       count(*) FILTER (WHERE pid <> pg_backend_pid() "
            "         AND wait_event_type = 'Lock')::bigint AS other_lock_waiters "
            "FROM pg_catalog.pg_stat_activity "
            "WHERE datname = current_database() AND backend_type = 'client backend'"
        )
    ).mappings().one()
    prepared_count = int(
        connection.execute(
            text(
                "SELECT count(*)::bigint FROM pg_catalog.pg_prepared_xacts "
                "WHERE database = current_database()"
            )
        ).scalar_one()
    )
    relation_locks = connection.execute(
        text(
            "SELECT c.relname AS table_name, locks.mode, locks.granted, "
            "       count(*)::bigint AS lock_count "
            "FROM pg_catalog.pg_locks AS locks "
            "JOIN pg_catalog.pg_class AS c ON c.oid = locks.relation "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            "WHERE locks.pid <> pg_backend_pid() AND n.nspname = :schema "
            "GROUP BY c.relname, locks.mode, locks.granted "
            "ORDER BY c.relname, locks.mode, locks.granted"
        ),
        {"schema": schema},
    ).mappings()
    advisory_locks = connection.execute(
        text(
            "SELECT mode, granted, count(*)::bigint AS lock_count "
            "FROM pg_catalog.pg_locks "
            "WHERE pid <> pg_backend_pid() AND locktype = 'advisory' "
            "GROUP BY mode, granted ORDER BY mode, granted"
        )
    ).mappings()

    session_counts = {key: int(value) for key, value in sessions.items()}
    relation_lock_rows = _dict_rows(relation_locks)
    advisory_lock_rows = _dict_rows(advisory_locks)
    waiting_lock = bool(session_counts["other_lock_waiters"]) or any(
        not bool(row["granted"])
        for row in [*relation_lock_rows, *advisory_lock_rows]
    )
    if session_counts["other_sessions_redacted"]:
        snapshot_gate = "UNKNOWN_AT_SNAPSHOT"
    elif (
        session_counts["other_active_sessions"]
        or session_counts["other_open_transactions"]
        or prepared_count
        or waiting_lock
    ):
        snapshot_gate = "NOT_CLEAR_AT_SNAPSHOT"
    else:
        snapshot_gate = "CLEAR_AT_SNAPSHOT"

    return {
        "continuous_writer_quiescence_proven": False,
        "database_identity": dict(identity),
        "interpretation": "POINT_IN_TIME_OBSERVATION_ONLY",
        "migration_authorized_by_report": False,
        "other_advisory_locks": advisory_lock_rows,
        "other_relation_locks": relation_lock_rows,
        "prepared_transactions": prepared_count,
        "sessions": session_counts,
        "snapshot_gate": snapshot_gate,
    }


def _transaction_contract(connection: Connection) -> dict[str, Any]:
    row = connection.execute(
        text(
            "SELECT current_setting('transaction_read_only') AS read_only, "
            "       current_setting('transaction_isolation') AS isolation"
        )
    ).mappings().one()
    read_only = row["read_only"]
    isolation = row["isolation"]
    if read_only != "on" or isolation.lower() != "repeatable read":
        raise InventoryBlocked("database did not honor the snapshot contract")
    return {
        "database_reported_isolation": isolation,
        "database_reported_read_only": read_only,
        "database_writes_performed": False,
        "isolation": "REPEATABLE READ",
        "read_only": True,
        "set_transaction_read_only_was_first_statement": True,
    }


def _blocked_reasons(
    *,
    marker: Mapping[str, Any],
    tables: Sequence[Mapping[str, Any]],
    constraints: Sequence[Mapping[str, Any]],
    foreign_keys: Sequence[Mapping[str, Any]],
    session_safety: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if not marker["exists"]:
        reasons.append("SCHEMA_MARKER_MISSING")
    elif not marker["history"]:
        reasons.append("SCHEMA_MARKER_EMPTY")
    if not tables:
        reasons.append("NO_TABLES_IN_SCHEMA")
    if any(not bool(item["validated"]) for item in constraints):
        reasons.append("UNVALIDATED_CONSTRAINTS_PRESENT")
    if any(
        item["orphan_count"] is not None and int(item["orphan_count"]) > 0
        for item in foreign_keys
    ):
        reasons.append("FOREIGN_KEY_ORPHANS_PRESENT")
    snapshot_gate = session_safety["snapshot_gate"]
    if snapshot_gate == "UNKNOWN_AT_SNAPSHOT":
        reasons.append("SESSION_VISIBILITY_UNKNOWN")
    elif snapshot_gate != "CLEAR_AT_SNAPSHOT":
        reasons.append("CONCURRENT_DATABASE_ACTIVITY_AT_SNAPSHOT")
    return reasons


def collect_inventory(engine: Engine, *, schema: str = "public") -> dict[str, Any]:
    """Collect one exact PostgreSQL snapshot without making database writes."""

    schema = require_simple_identifier(schema, label="schema")
    if engine.dialect.name != "postgresql":
        raise InventoryBlocked("PostgreSQL is required")

    with engine.connect() as connection:
        connection = connection.execution_options(isolation_level="REPEATABLE READ")
        transaction = connection.begin()
        try:
            # Must be the first statement after BEGIN.  All helpers below are
            # SELECT-only and run in this exact repeatable-read snapshot.
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            transaction_contract = _transaction_contract(connection)
            _require_schema(connection, schema)
            marker = _schema_marker(connection, schema)
            columns = _columns_by_table(connection, schema)
            sensitive_relations = _sensitive_relations(columns)
            tables = _table_inventory(
                connection,
                schema,
                columns=columns,
                sensitive_relations=sensitive_relations,
            )
            constraints = _constraints(
                connection, schema, sensitive_relations=sensitive_relations
            )
            foreign_keys = _foreign_key_orphans(
                connection, schema, sensitive_relations=sensitive_relations
            )
            session_safety = _session_safety(connection, schema)

            reasons = _blocked_reasons(
                constraints=constraints,
                foreign_keys=foreign_keys,
                marker=marker,
                session_safety=session_safety,
                tables=tables,
            )
            report: dict[str, Any] = {
                "blocked_reasons": reasons,
                "report_schema_version": REPORT_SCHEMA_VERSION,
                "migration_authorization": {
                    "authorized": False,
                    "reason": "INVENTORY_AND_POINT_IN_TIME_EVIDENCE_ONLY",
                },
                "scope": {
                    "credentials": "OUT_OF_SCOPE_NOT_ACCESSED",
                    "acl": "OUT_OF_SCOPE_NOT_ACCESSED",
                    "database_facts": "IN_SCOPE",
                    "database_schema": schema,
                    "okx_live": "OUT_OF_SCOPE_NOT_ACCESSED",
                    "orders_signals_runtime": "OUT_OF_SCOPE_NOT_ACCESSED",
                },
                "status": "BLOCKED" if reasons else "PASSED",
                "transaction_contract": transaction_contract,
                "schema_marker": marker,
                "tables": {
                    "items": tables,
                    "sensitive_table_values_or_counts_inspected": False,
                },
                "constraints": {
                    "items": constraints,
                    "unvalidated": [
                        {"name": item["name"], "table": item["table"]}
                        for item in constraints
                        if not item["validated"]
                    ],
                },
                "foreign_key_orphans": {
                    "items": foreign_keys,
                    "known_orphan_count": sum(
                        int(item["orphan_count"])
                        for item in foreign_keys
                        if item["orphan_count"] is not None
                    ),
                },
                "session_and_lock_snapshot": session_safety,
            }
            report["digests"] = {
                "constraints_sha256": canonical_sha256(constraints),
                "foreign_key_orphans_sha256": canonical_sha256(foreign_keys),
                "schema_marker_sha256": canonical_sha256(marker),
                "session_and_lock_snapshot_sha256": canonical_sha256(session_safety),
                "tables_sha256": canonical_sha256(tables),
            }
            report["evidence_sha256"] = canonical_sha256(report)
            return report
        finally:
            # Explicit rollback avoids even relying on a read-only COMMIT path.
            transaction.rollback()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Emit a canonical, read-only V1.3 PostgreSQL inventory."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="PostgreSQL SQLAlchemy URL (defaults to DATABASE_URL; never printed)",
    )
    parser.add_argument("--schema", default="public")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact canonical JSON instead of indented canonical JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.database_url.strip():
        print("inventory blocked: DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        engine = create_engine(args.database_url, pool_pre_ping=True)
        try:
            report = collect_inventory(engine, schema=args.schema)
        finally:
            engine.dispose()
    except Exception:
        # Never render exception messages: DBAPI/URL exceptions may echo a URL,
        # role, host, password, DSN option, or server-supplied sensitive text.
        print("inventory blocked: inventory collection failed", file=sys.stderr)
        return 2

    print(canonical_json(report, pretty=not args.compact))
    if report["status"] != "PASSED":
        print("inventory blocked: evidence gate not passed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
