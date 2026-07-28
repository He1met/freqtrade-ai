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
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["kind"] == "postgresql-data-only"
    assert payload["excluded_tables"] == [
        "public.okx_demo_attestation_secrets"
    ]
    assert payload["reattestation_required_after_restore"] is True
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
