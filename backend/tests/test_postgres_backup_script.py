import importlib.util
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "postgres_backup.py"
SPEC = importlib.util.spec_from_file_location("postgres_backup", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
postgres_backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(postgres_backup)


DATABASE_URL = "postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai"


def test_backup_is_atomic_data_only_and_excludes_attestation_secrets(
    monkeypatch,
    tmp_path,
):
    observed = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        kwargs["stdout"].write(b"-- PostgreSQL database dump\n" + b"x" * 256)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(postgres_backup.subprocess, "run", fake_run)
    backup, manifest = postgres_backup.create_backup(
        database_url=DATABASE_URL,
        output_dir=tmp_path,
        pg_dump_binary="/usr/bin/pg_dump",
    )

    assert "--data-only" in observed["argv"]
    assert (
        "--exclude-table=public.okx_demo_attestation_secrets"
        in observed["argv"]
    )
    assert (
        "--exclude-table=public.okx_demo_operator_consent_secrets"
        in observed["argv"]
    )
    dump_url = observed["argv"][-1]
    assert "change_me" not in dump_url
    assert "freqtrade@" not in dump_url
    assert "host=%2Ftmp" in dump_url
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["kind"] == "postgresql-data-only"
    assert payload["excluded_tables"] == [
        "public.okx_demo_attestation_secrets",
        "public.okx_demo_operator_consent_secrets",
        "public.freqtrade_ai_schema_migrations",
    ]
    assert payload["reattestation_required_after_restore"] is True
    assert payload["reauthorization_required_after_restore"] is True
    assert payload["credential_values_recorded"] is False
    assert "change_me" not in manifest.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".*.tmp"))


def test_backup_failure_removes_partial_files(monkeypatch, tmp_path):
    def fake_run(_argv, **kwargs):
        kwargs["stdout"].write(b"partial")
        return SimpleNamespace(returncode=1, stderr=b"permission denied")

    monkeypatch.setattr(postgres_backup.subprocess, "run", fake_run)
    with pytest.raises(postgres_backup.BackupBlocked):
        postgres_backup.create_backup(
            database_url=DATABASE_URL,
            output_dir=tmp_path,
            pg_dump_binary="/usr/bin/pg_dump",
        )

    assert list(tmp_path.iterdir()) == []


def test_backup_rejects_noncanonical_database() -> None:
    with pytest.raises(postgres_backup.BackupBlocked):
        postgres_backup.peer_admin_database_url(
            "postgresql+psycopg://freqtrade:change_me@example.com/other"
        )


def test_restore_verifies_checksum_and_uses_one_transaction(monkeypatch, tmp_path):
    backup = tmp_path / "backup.sql"
    backup.write_text("-- PostgreSQL database dump\n" + "x" * 256, encoding="utf-8")
    manifest = tmp_path / "backup.manifest.json"
    manifest.write_text(json.dumps({
        "backup_file": backup.name,
        "sha256": postgres_backup.hashlib.sha256(backup.read_bytes()).hexdigest(),
        "kind": "postgresql-data-only",
        "excluded_tables": list(postgres_backup.EXCLUDED_TABLES),
        "reattestation_required_after_restore": True,
        "reauthorization_required_after_restore": True,
        "credential_values_recorded": False,
        "restore_transaction": "single-transaction",
        "restore_requires_empty_v28_schema": True,
    }), encoding="utf-8")
    observed = {}
    monkeypatch.setattr(
        postgres_backup.subprocess,
        "run",
        lambda argv, **_kwargs: (
            observed.update(argv=argv) or SimpleNamespace(returncode=0)
        ),
    )
    monkeypatch.setattr(
        postgres_backup,
        "require_empty_v28_restore_target",
        lambda _database_url: None,
    )

    postgres_backup.restore_backup(
        database_url=DATABASE_URL,
        backup_path=backup,
        manifest_path=manifest,
        psql_binary="/usr/bin/psql",
    )

    assert "--single-transaction" in observed["argv"]
    assert "--set=ON_ERROR_STOP=1" in observed["argv"]
    command_index = next(
        index
        for index, argument in enumerate(observed["argv"])
        if argument.startswith("--command=")
    )
    file_index = observed["argv"].index("--file={}".format(backup.resolve()))
    assert command_index < file_index
    preflight_sql = observed["argv"][command_index]
    assert preflight_sql.startswith("--command=LOCK TABLE ")
    assert " IN ACCESS EXCLUSIVE MODE;" in preflight_sql
    assert 'public."freqtrade_ai_schema_migrations"' in preflight_sql
    assert 'public."okx_demo_attestation_secrets"' in preflight_sql
    assert 'public."okx_demo_operator_consent_secrets"' in preflight_sql
    assert "version_count <> 1" in preflight_sql
    assert postgres_backup.SCHEMA_VERSION in preflight_sql
    assert preflight_sql.index("LOCK TABLE") < preflight_sql.index("SELECT count(*)")
    assert preflight_sql.index("SELECT count(*)") < preflight_sql.index(
        'SELECT 1 FROM public."okx_demo_attestation_secrets"'
    )
