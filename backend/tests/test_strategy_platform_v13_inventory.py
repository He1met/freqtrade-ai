from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "strategy_platform_v13_inventory.py"
)
EVIDENCE_SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "migrations"
    / "strategy_platform_v13_task1_evidence.sql"
)
SPEC = importlib.util.spec_from_file_location(
    "strategy_platform_v13_inventory", SCRIPT_PATH
)
assert SPEC is not None
assert SPEC.loader is not None
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def test_canonical_json_and_digest_are_stable() -> None:
    value = {
        "z_decimal": Decimal("12.340"),
        "naive_time": datetime(2026, 8, 13, 4, 5, 6, 7000),
        "aware_time": datetime(
            2026,
            8,
            13,
            12,
            5,
            6,
            7000,
            tzinfo=timezone.utc,
        ),
        "day": date(2026, 8, 13),
        "bytes": b"\x00\xff",
        "unicode": "行情",
    }

    compact = inventory.canonical_json(value)

    assert compact == (
        '{"aware_time":"2026-08-13T12:05:06.007000Z",'
        '"bytes":"00ff","day":"2026-08-13",'
        '"naive_time":"2026-08-13T04:05:06.007000Z",'
        '"unicode":"行情","z_decimal":"12.340"}'
    )
    assert json.loads(inventory.canonical_json(value, pretty=True)) == json.loads(
        compact
    )
    expected = hashlib.sha256(compact.encode("utf-8")).hexdigest()
    assert inventory.canonical_sha256(value) == expected
    assert inventory.canonical_sha256(dict(reversed(list(value.items())))) == expected


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        inventory.canonical_json({"not_evidence": float("nan")})


@pytest.mark.parametrize("identifier", ["public", "lab_13", "SchemaV1"])
def test_simple_identifier_accepts_safe_schema_names(identifier: str) -> None:
    assert inventory.require_simple_identifier(identifier) == identifier


@pytest.mark.parametrize(
    "identifier",
    ["", "public.v1", "public;DROP TABLE x", 'public"x', "two words"],
)
def test_simple_identifier_rejects_sql_or_qualified_input(identifier: str) -> None:
    with pytest.raises(inventory.InventoryBlocked):
        inventory.require_simple_identifier(identifier, label="schema")


@pytest.mark.parametrize(
    "identifier",
    (
        "operator_secrets",
        "api_credentials",
        "password_hash",
        "encrypted_api_key_ciphertext",
        "refresh_token",
        "private_key_material",
        "auth_header",
    ),
)
def test_sensitive_identifier_detection_is_fail_closed(identifier: str) -> None:
    assert inventory._is_sensitive_identifier(identifier) is True


def test_sensitive_relations_include_sensitive_columns() -> None:
    columns = {
        "safe_table": {"id": "uuid", "name": "text"},
        "provider_settings": {"id": "uuid", "encrypted_api_key": "text"},
    }

    assert inventory._sensitive_relations(columns) == {"provider_settings"}


def test_sql_evidence_excludes_acl_and_protected_relation_reads() -> None:
    sql = EVIDENCE_SQL_PATH.read_text(encoding="utf-8")

    assert "aclexplode" not in sql
    assert "pg_default_acl" not in sql
    assert "has_schema_privilege" not in sql
    assert "EXCLUDED_SENSITIVE_RELATION" in sql
    assert "sensitive_attribute.attname" in sql
    assert "secrets?|credentials?|passwords?|passphrases?|tokens?" in sql


class _QuotingConnection:
    dialect = postgresql.dialect()


def test_fk_orphan_sql_quotes_catalog_identifiers_and_preserves_column_order() -> None:
    sql = inventory.build_fk_orphan_sql(
        _QuotingConnection(),
        child_schema="tenant",
        child_table="child records",
        child_columns=["parent id", "kind"],
        parent_schema="reference",
        parent_table="parent",
        parent_columns=["id", "kind"],
    )

    assert sql.startswith(
        'SELECT count(*)::bigint AS orphan_count FROM "tenant"."child records" '
        "AS child WHERE "
    )
    assert 'child."parent id" IS NOT NULL AND child."kind" IS NOT NULL' in sql
    assert 'FROM "reference"."parent" AS parent' in sql
    assert 'parent."id" = child."parent id"' in sql
    assert 'parent."kind" = child."kind"' in sql
    assert "SELECT *" not in sql


def test_fk_orphan_sql_handles_match_full_partial_null_rows() -> None:
    sql = inventory.build_fk_orphan_sql(
        _QuotingConnection(),
        child_schema="public",
        child_table="child",
        child_columns=["parent_id", "parent_kind"],
        parent_schema="public",
        parent_table="parent",
        parent_columns=["id", "kind"],
        match_type="f",
    )

    assert " OR " in sql
    assert "AND NOT (child.\"parent_id\" IS NOT NULL" in sql
    assert "NOT EXISTS" in sql


@pytest.mark.parametrize(
    ("child_columns", "parent_columns", "match_type"),
    [([], [], "s"), (["a"], ["a", "b"], "s"), (["a"], ["a"], "p")],
)
def test_fk_orphan_sql_fails_closed_on_incomplete_or_unknown_metadata(
    child_columns: list[str],
    parent_columns: list[str],
    match_type: str,
) -> None:
    with pytest.raises(inventory.InventoryBlocked):
        inventory.build_fk_orphan_sql(
            _QuotingConnection(),
            child_schema="public",
            child_table="child",
            child_columns=child_columns,
            parent_schema="public",
            parent_table="parent",
            parent_columns=parent_columns,
            match_type=match_type,
        )


class _FakeTransaction:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.rolled_back = False

    def rollback(self) -> None:
        self.rolled_back = True
        self.events.append("rollback")


class _FakeConnection:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.transaction = _FakeTransaction(events)

    def execution_options(self, **options):
        self.events.append(("execution_options", options))
        return self

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, *_args):
        self.events.append("exit")

    def begin(self):
        self.events.append("begin")
        return self.transaction

    def exec_driver_sql(self, statement: str):
        self.events.append(("exec_driver_sql", statement))
        return SimpleNamespace()


class _FakeEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, events: list[object]) -> None:
        self.connection = _FakeConnection(events)

    def connect(self):
        return self.connection


def test_collect_inventory_sets_read_only_first_and_rolls_back(monkeypatch) -> None:
    events: list[object] = []
    engine = _FakeEngine(events)

    def observed(name, value):
        def helper(*_args, **_kwargs):
            events.append(name)
            return value

        return helper

    marker = {
        "current": {"version": "20260813_46"},
        "exists": True,
        "history": [{"version": "20260813_46"}],
        "row_count": 1,
        "table": "public.freqtrade_ai_schema_migrations",
    }
    tables = [
        {
            "inventory_status": "INVENTORIED",
            "row_count": 1,
            "table": "strategies",
        }
    ]
    constraints = [
        {"name": "strategies_pkey", "table": "strategies", "validated": True}
    ]
    foreign_keys = [
        {
            "inventory_status": "INVENTORIED",
            "orphan_count": 0,
        }
    ]
    session = {
        "migration_authorized_by_report": False,
        "snapshot_gate": "CLEAR_AT_SNAPSHOT",
    }
    transaction_contract = {
        "database_reported_isolation": "repeatable read",
        "database_reported_read_only": "on",
        "database_writes_performed": False,
        "isolation": "REPEATABLE READ",
        "read_only": True,
    }
    monkeypatch.setattr(
        inventory,
        "_transaction_contract",
        observed("transaction_contract", transaction_contract),
    )
    monkeypatch.setattr(inventory, "_require_schema", observed("schema", None))
    monkeypatch.setattr(inventory, "_schema_marker", observed("marker", marker))
    monkeypatch.setattr(
        inventory,
        "_columns_by_table",
        observed("columns", {"strategies": {"id": "uuid"}}),
    )
    monkeypatch.setattr(
        inventory,
        "_sensitive_relations",
        observed("sensitive_relations", set()),
    )
    monkeypatch.setattr(inventory, "_table_inventory", observed("tables", tables))
    monkeypatch.setattr(
        inventory,
        "_constraints",
        observed("constraints", constraints),
    )
    monkeypatch.setattr(
        inventory,
        "_foreign_key_orphans",
        observed("foreign_keys", foreign_keys),
    )
    monkeypatch.setattr(
        inventory,
        "_session_safety",
        observed("session", session),
    )

    report = inventory.collect_inventory(engine)

    assert events[:4] == [
        "enter",
        ("execution_options", {"isolation_level": "REPEATABLE READ"}),
        "begin",
        ("exec_driver_sql", "SET TRANSACTION READ ONLY"),
    ]
    assert events[4:13] == [
        "transaction_contract",
        "schema",
        "marker",
        "columns",
        "sensitive_relations",
        "tables",
        "constraints",
        "foreign_keys",
        "session",
    ]
    assert events[-2:] == ["rollback", "exit"]
    assert engine.connection.transaction.rolled_back is True
    assert report["status"] == "PASSED"
    assert report["migration_authorization"]["authorized"] is False
    assert report["scope"]["acl"] == "OUT_OF_SCOPE_NOT_ACCESSED"
    assert "acl" not in report
    assert report["session_and_lock_snapshot"]["migration_authorized_by_report"] is False
    expected_report = dict(report)
    digest = expected_report.pop("evidence_sha256")
    assert digest == inventory.canonical_sha256(expected_report)


def test_inventory_gate_reports_all_known_blockers() -> None:
    reasons = inventory._blocked_reasons(
        marker={"exists": False, "history": []},
        tables=[],
        constraints=[{"validated": False}],
        foreign_keys=[{"orphan_count": 3}],
        session_safety={"snapshot_gate": "UNKNOWN_AT_SNAPSHOT"},
    )

    assert reasons == [
        "SCHEMA_MARKER_MISSING",
        "NO_TABLES_IN_SCHEMA",
        "UNVALIDATED_CONSTRAINTS_PRESENT",
        "FOREIGN_KEY_ORPHANS_PRESENT",
        "SESSION_VISIBILITY_UNKNOWN",
    ]


class _DisposableEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_cli_error_does_not_render_database_url_or_exception(monkeypatch, capsys) -> None:
    database_url = (
        "postgresql+psycopg://inventory_user:leak-sentinel@"
        "db.internal.example/freqtrade_ai"
    )
    engine = _DisposableEngine()
    observed: dict[str, object] = {}

    def fake_create_engine(url: str, **_kwargs):
        observed["url"] = url
        return engine

    def fail_collection(_engine, **_kwargs):
        raise RuntimeError(f"could not connect to {database_url}")

    monkeypatch.setattr(inventory, "create_engine", fake_create_engine)
    monkeypatch.setattr(inventory, "collect_inventory", fail_collection)

    assert inventory.main(["--database-url", database_url]) == 2

    emitted = capsys.readouterr()
    rendered = emitted.out + emitted.err
    assert observed["url"] == database_url
    assert engine.disposed is True
    assert database_url not in rendered
    assert "leak-sentinel" not in rendered
    assert "RuntimeError" not in rendered
    assert emitted.err == "inventory blocked: inventory collection failed\n"


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [("PASSED", 0), ("BLOCKED", 2), ("UNKNOWN", 2)],
)
def test_cli_exit_code_fails_closed_for_non_passed_reports(
    monkeypatch,
    capsys,
    status: str,
    expected_exit: int,
) -> None:
    engine = _DisposableEngine()
    report = {"evidence_sha256": "a" * 64, "status": status}
    monkeypatch.setattr(inventory, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(inventory, "collect_inventory", lambda *_args, **_kwargs: report)

    exit_code = inventory.main(
        ["--database-url", "postgresql://local/inventory", "--compact"]
    )

    emitted = capsys.readouterr()
    assert exit_code == expected_exit
    assert json.loads(emitted.out) == report
    assert engine.disposed is True
    if status == "PASSED":
        assert emitted.err == ""
    else:
        assert emitted.err == "inventory blocked: evidence gate not passed\n"


def test_cli_missing_database_url_is_blocked_without_engine(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        inventory,
        "create_engine",
        lambda *_args, **_kwargs: pytest.fail("engine must not be created"),
    )

    assert inventory.main([]) == 2
    assert capsys.readouterr().err == "inventory blocked: DATABASE_URL is required\n"
