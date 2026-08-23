from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from app.canonical_v13.acceptance_signal_trigger_upgrade import (
    apply_acceptance_signal_trigger_upgrade,
)
from app.canonical_v13.gate_receipt_upgrade import verify_gate_receipt_upgrade
from app.canonical_v13.genesis import (
    install_canonical_genesis,
    postgresql_acl_statements,
    render_postgresql_owner_sql,
)
from app.canonical_v13.models import RESEARCH_GATE_ATTEMPTS_TABLE
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts/canonical_v13_postgres_backup.py"
SPEC = importlib.util.spec_from_file_location("canonical_v13_postgres_backup", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)

DATABASE_URL = os.environ.get("CANONICAL_V13_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANONICAL_V13_POSTGRES_URL is required for the isolated restore contract",
)


def _sql_statements(sql: str) -> tuple[str, ...]:
    return tuple(statement for statement in sql.split(";\n") if statement.strip())


def _terminal_attempt(attempt_id):
    observed_at = datetime(2026, 8, 21, tzinfo=timezone.utc)
    lineage_id = uuid4()
    digest = "a" * 64
    return {
        "id": attempt_id,
        "strategy_version_id": lineage_id,
        "research_target_id": uuid4(),
        "artifact_digest": digest,
        "gate_contract_version": "canonical-v13-planless-gate-attempt-v3",
        "release_commit": "b" * 40,
        "executor_image_digest": digest,
        "worker_source_digest": digest,
        "target_snapshot_id": uuid4(),
        "target_snapshot_digest": digest,
        "window_snapshot_id": uuid4(),
        "window_snapshot_digest": digest,
        "market_profile_version_id": uuid4(),
        "market_profile_digest": digest,
        "configuration_bundle_id": uuid4(),
        "configuration_bundle_digest": digest,
        "market_snapshot_id": uuid4(),
        "market_snapshot_digest": digest,
        "idempotency_key": f"historical-terminal:{attempt_id}",
        "request_digest": attempt_id.hex.ljust(64, "0"),
        "status": "PASSED",
        "terminal_reason_code": None,
        "writer_identity": "canonical_validation_writer",
        "lease_token_digest": None,
        "lease_expires_at": None,
        "created_at": observed_at,
        "started_at": observed_at,
        "completed_at": observed_at,
    }


@pytest.mark.parametrize(
    ("overrides", "unsafe"),
    (
        ({}, False),
        ({"credential_reference": "keychain:opaque-runtime-ref"}, False),
        ({"credential_reference": "none:other"}, True),
        ({"service_account": "canonical_order_writer"}, True),
        ({"network_policy": "UNRESTRICTED"}, True),
        ({"runtime_class": "EPHEMERAL_RESEARCH_WORKER"}, True),
        ({"filesystem_mode": "READ_WRITE"}, True),
        ({"research_executor_capability": True}, True),
        ({"order_writer_capability": True}, True),
    ),
)
def test_public_market_runtime_reference_is_only_safe_for_exact_capability_boundary(
    overrides: dict[str, object], unsafe: bool
) -> None:
    assert DATABASE_URL is not None
    values = {
        "credential_reference": "none:public-okx-market-only",
        "service_account": "canonical_runtime_reader",
        "network_policy": "DEMO_EXCHANGE_ONLY",
        "runtime_class": "LONG_LIVED_TRADING_RUNTIME",
        "filesystem_mode": "READ_ONLY",
        "research_executor_capability": False,
        "order_writer_capability": False,
        **overrides,
    }
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            observed = connection.execute(
                text(
                    "SELECT count(*) FROM (VALUES ("
                    ":credential_reference, :service_account, :network_policy, "
                    ":runtime_class, :filesystem_mode, "
                    ":research_executor_capability, :order_writer_capability"
                    ")) AS candidate(credential_reference, service_account, "
                    "network_policy, runtime_class, filesystem_mode, "
                    "research_executor_capability, order_writer_capability) "
                    "WHERE "
                    + backup._unsafe_runtime_credential_reference_predicate(
                        "candidate"
                    )
                ),
                values,
            ).scalar_one()
    finally:
        engine.dispose()
    assert bool(observed) is unsafe


def test_restore_terminal_historical_gate_row_with_triggers_transactional(
    tmp_path: Path,
) -> None:
    assert DATABASE_URL is not None
    pg_dump = shutil.which("pg_dump")
    pg_restore = shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        pytest.skip("PostgreSQL client binaries are required")

    base = make_url(DATABASE_URL)
    admin_url = base.set(database="postgres")
    suffix = uuid4().hex[:12]
    source_name = f"freqtrade_ai_v13_restore_source_{suffix}"
    target_name = f"freqtrade_ai_v13_restore_target_{suffix}"
    source_url = base.set(database=source_name)
    target_url = base.set(database=target_name)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    source = None
    target = None
    created_roles: list[str] = []
    try:
        with admin.connect() as connection:
            is_superuser = connection.execute(
                text(
                    "SELECT rolsuper FROM pg_catalog.pg_roles "
                    "WHERE rolname=current_user"
                )
            ).scalar_one()
            if not is_superuser:
                pytest.skip(
                    "isolated restore regression requires a PostgreSQL superuser"
                )
            role_mapping = backup.local_role_mapping()
            existing_roles = set(
                connection.execute(
                    text(
                        "SELECT rolname FROM pg_catalog.pg_roles "
                        "WHERE rolname = ANY(:roles)"
                    ),
                    {"roles": list(role_mapping.roles.values())},
                ).scalars()
            )
            for role_name in role_mapping.roles.values():
                if role_name not in existing_roles:
                    connection.exec_driver_sql(
                        f'CREATE ROLE "{role_name}" NOLOGIN NOSUPERUSER '
                        "NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION "
                        "NOBYPASSRLS"
                    )
                    created_roles.append(role_name)
            connection.exec_driver_sql(
                f'CREATE DATABASE "{source_name}" TEMPLATE template0'
            )
            connection.exec_driver_sql(
                f'CREATE DATABASE "{target_name}" TEMPLATE template0'
            )

        source = create_engine(source_url)
        target = create_engine(target_url)
        for engine in (source, target):
            with engine.begin() as connection:
                install_canonical_genesis(
                    connection, installer_identity="canonical-backup-restore-test"
                )
                for statement in _sql_statements(
                    render_postgresql_owner_sql(role_mapping)
                ):
                    connection.exec_driver_sql(statement)
                for statement in postgresql_acl_statements(role_mapping):
                    connection.exec_driver_sql(statement)
                upgraded = apply_acceptance_signal_trigger_upgrade(
                    connection, role_mapping=role_mapping
                )
                assert upgraded.status == "UPGRADED"

        attempt_id = uuid4()
        with source.begin() as connection:
            # The source represents already-accepted historical evidence. Replica
            # mode is confined to this disposable fixture so foreign-key lineage
            # need not recreate the entire research graph.
            connection.exec_driver_sql("SET LOCAL session_replication_role=replica")
            connection.execute(
                RESEARCH_GATE_ATTEMPTS_TABLE.insert().values(
                    **_terminal_attempt(attempt_id)
                )
            )

        archive = tmp_path / "terminal-gate.dump"
        dumped = subprocess.run(
            [
                pg_dump,
                "--format=custom",
                "--data-only",
                "--no-owner",
                "--no-privileges",
                "--table=strategy_platform_v13.research_gate_attempts",
                f"--file={archive}",
                backup._libpq_url(source_url),
            ],
            check=False,
            capture_output=True,
        )
        assert dumped.returncode == 0, dumped.stderr.decode(errors="replace")
        restored = subprocess.run(
            backup.restore_command(
                binary=pg_restore,
                database_url=target_url,
                archive_path=archive,
            ),
            check=False,
            capture_output=True,
        )
        assert restored.returncode == 0, restored.stderr.decode(errors="replace")

        failed_replay = subprocess.run(
            backup.restore_command(
                binary=pg_restore,
                database_url=target_url,
                archive_path=archive,
            ),
            check=False,
            capture_output=True,
        )
        assert failed_replay.returncode != 0

        with target.connect() as connection:
            transaction = connection.begin()
            restored_rows = connection.execute(
                select(
                    RESEARCH_GATE_ATTEMPTS_TABLE.c.id,
                    RESEARCH_GATE_ATTEMPTS_TABLE.c.status,
                ).where(RESEARCH_GATE_ATTEMPTS_TABLE.c.id == attempt_id)
            ).all()
            assert restored_rows == [(attempt_id, "PASSED")]
            backup._verify_restore_trigger_boundary(
                connection, require_superuser=True
            )
            assert verify_gate_receipt_upgrade(connection).status == "ACCEPTED"
            savepoint = connection.begin_nested()
            with pytest.raises(DBAPIError):
                connection.execute(
                    RESEARCH_GATE_ATTEMPTS_TABLE.update()
                    .where(RESEARCH_GATE_ATTEMPTS_TABLE.c.id == attempt_id)
                    .values(status="BLOCKED", terminal_reason_code="tamper")
                )
            savepoint.rollback()
            transaction.rollback()
    finally:
        if source is not None:
            source.dispose()
        if target is not None:
            target.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{source_name}"')
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{target_name}"')
            for role_name in reversed(created_roles):
                connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role_name}"')
        admin.dispose()
