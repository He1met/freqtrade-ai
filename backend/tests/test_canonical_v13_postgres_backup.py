from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
    CANONICAL_TABLE_NAMES,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts/canonical_v13_postgres_backup.py"
SPEC = importlib.util.spec_from_file_location("canonical_v13_postgres_backup", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def _counts(value: int = 0) -> dict[str, int]:
    counts = {name: value for name in CANONICAL_TABLE_NAMES}
    counts["schema_metadata"] = 1
    return counts


def _manifest(*, archive: Path, counts: dict[str, int]) -> dict[str, object]:
    return backup.build_manifest(
        archive_name=archive.name,
        archive_sha256=backup._sha256_file(archive),
        release_sha="a" * 40,
        row_counts=counts,
        created_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )


def test_plan_is_offline_exact_and_never_executes() -> None:
    result = backup.plan()
    assert result == {
        "status": "READY",
        "contract": "canonical-v13-postgres-data-backup-v1",
        "source_database": "freqtrade_ai_v13",
        "restore_database_pattern": (
            "freqtrade_ai_v13_restore_[a-z0-9][a-z0-9_]*"
        ),
        "business_schema": CANONICAL_BUSINESS_SCHEMA,
        "canonical_manifest_digest": CANONICAL_MANIFEST_DIGEST,
        "authority_mapping_digest": backup.local_role_mapping().mapping_digest,
        "table_count": 56,
        "credential_values_recorded": False,
        "execution_performed": False,
    }
    assert len(CANONICAL_TABLE_NAMES) == 56


def test_dispatch_credential_generation_is_exact_digest_only_allowlist() -> None:
    backup._require_exact_manifest()
    assert (
        "order_dispatch_receipts.credential_generation_digest"
        in backup.SAFE_SENSITIVE_METADATA_COLUMNS
    )
    assert all(
        entry != "order_dispatch_receipts.credential_generation"
        for entry in backup.SAFE_SENSITIVE_METADATA_COLUMNS
    )


def test_database_targets_are_explicit_local_passwordless_and_isolated() -> None:
    source = backup._database_url(
        "postgresql+psycopg:///freqtrade_ai_v13",
        expected_database="freqtrade_ai_v13",
        restore_target=False,
    )
    assert source.database == "freqtrade_ai_v13"
    restored = backup._database_url(
        "postgresql+psycopg:///freqtrade_ai_v13_restore_phase9_001",
        expected_database="freqtrade_ai_v13_restore_phase9_001",
        restore_target=True,
    )
    assert restored.database == "freqtrade_ai_v13_restore_phase9_001"
    for raw, expected, is_restore in (
        (
            "postgresql+psycopg://operator:secret@localhost/freqtrade_ai_v13",
            "freqtrade_ai_v13",
            False,
        ),
        (
            "postgresql+psycopg://remote.example/freqtrade_ai_v13",
            "freqtrade_ai_v13",
            False,
        ),
        (
            "postgresql+psycopg:///freqtrade_ai_v13?password=hidden",
            "freqtrade_ai_v13",
            False,
        ),
        (
            "postgresql+psycopg:///freqtrade_ai_v13",
            "freqtrade_ai_v13",
            True,
        ),
    ):
        with pytest.raises(backup.CanonicalBackupBlocked):
            backup._database_url(
                raw, expected_database=expected, restore_target=is_restore
            )


def test_dump_command_is_exact_data_only_and_excludes_identity_data() -> None:
    parsed = backup._database_url(
        "postgresql+psycopg:///freqtrade_ai_v13",
        expected_database="freqtrade_ai_v13",
        restore_target=False,
    )
    command = backup.dump_command(
        binary="/usr/bin/pg_dump",
        database_url=parsed,
        output_path=Path("/tmp/exact.dump"),
    )
    assert "--format=custom" in command
    assert "--data-only" in command
    assert f"--schema={CANONICAL_BUSINESS_SCHEMA}" in command
    assert (
        f"--exclude-table-data={CANONICAL_BUSINESS_SCHEMA}.schema_metadata"
        in command
    )
    table_arguments = [value for value in command if value.startswith("--table=")]
    assert table_arguments == [
        f"--table={CANONICAL_BUSINESS_SCHEMA}.{name}"
        for name in CANONICAL_TABLE_NAMES
    ]
    assert len(table_arguments) == 56
    rendered = " ".join(command)
    assert "secret" not in rendered
    assert "password" not in rendered
    assert "public." not in rendered


def test_manifest_rejects_table_authority_policy_and_archive_drift(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"fake-custom-archive")
    payload = _manifest(archive=archive, counts=_counts())
    assert backup.validate_manifest(payload, archive_path=archive) == payload

    drifted = dict(payload)
    drifted["tables"] = list(CANONICAL_TABLE_NAMES[:-1])
    with pytest.raises(
        backup.CanonicalBackupBlocked,
        match="BLOCKED_CANONICAL_BACKUP_MANIFEST_DRIFT",
    ):
        backup.validate_manifest(drifted, archive_path=archive)

    drifted = dict(payload)
    drifted["secret_exclusion_policy"] = {
        **payload["secret_exclusion_policy"],
        "credential_values_recorded": True,
    }
    with pytest.raises(
        backup.CanonicalBackupBlocked,
        match="BLOCKED_CANONICAL_BACKUP_SAFETY_POLICY",
    ):
        backup.validate_manifest(drifted, archive_path=archive)

    archive.write_bytes(b"changed")
    with pytest.raises(
        backup.CanonicalBackupBlocked,
        match="BLOCKED_CANONICAL_BACKUP_ARCHIVE_DIGEST",
    ):
        backup.validate_manifest(payload, archive_path=archive)


def test_create_backup_uses_fake_runner_and_writes_atomic_manifest(
    tmp_path: Path,
) -> None:
    observed: list[tuple[str, ...]] = []

    def runner(command):
        command = tuple(command)
        observed.append(command)
        output = next(
            value.removeprefix("--file=")
            for value in command
            if value.startswith("--file=")
        )
        Path(output).write_bytes(b"fake-pg-custom-archive")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    result = backup.create_backup(
        source_database_url="postgresql+psycopg:///freqtrade_ai_v13",
        output_directory=tmp_path / "outside-repository",
        pg_dump_binary=sys.executable,
        runner=runner,
        release_guard=lambda: "b" * 40,
        database_inspector=lambda _url, require_zero: _counts(),
    )
    assert result["status"] == "BACKED_UP"
    assert result["table_count"] == 56
    assert result["credential_values_recorded"] is False
    assert len(observed) == 1
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    archive = Path(result["archive_path"])
    backup.validate_manifest(manifest, archive_path=archive)
    assert manifest["release_sha"] == "b" * 40
    assert not list((tmp_path / "outside-repository").glob(".*.tmp"))


def test_restore_requires_empty_preflight_and_exact_post_counts(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"fake-pg-custom-archive")
    counts = _counts()
    counts["audit_events"] = 3
    manifest_path = tmp_path / "backup.manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(archive=archive, counts=counts)), encoding="utf-8"
    )
    inspections: list[bool] = []

    def inspector(_url, *, require_zero):
        inspections.append(require_zero)
        return _counts() if require_zero else counts

    commands: list[tuple[str, ...]] = []

    def runner(command):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    result = backup.restore_backup(
        restore_database_url=(
            "postgresql+psycopg:///freqtrade_ai_v13_restore_phase9_002"
        ),
        restore_database_name="freqtrade_ai_v13_restore_phase9_002",
        archive_path=archive,
        manifest_path=manifest_path,
        pg_restore_binary=sys.executable,
        runner=runner,
        release_guard=lambda: "a" * 40,
        database_inspector=inspector,
    )
    assert result["status"] == "RESTORED_AND_VERIFIED"
    assert inspections == [True, False]
    assert len(commands) == 1
    assert "--single-transaction" in commands[0]
    assert "--exit-on-error" in commands[0]
    assert "--data-only" in commands[0]


def test_restore_fails_closed_on_post_restore_count_drift(tmp_path: Path) -> None:
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"fake-pg-custom-archive")
    manifest_path = tmp_path / "backup.manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(archive=archive, counts=_counts())), encoding="utf-8"
    )
    observations = iter((_counts(), {**_counts(), "audit_events": 1}))
    with pytest.raises(
        backup.CanonicalBackupBlocked,
        match="BLOCKED_CANONICAL_RESTORE_ROW_COUNTS",
    ):
        backup.restore_backup(
            restore_database_url=(
                "postgresql+psycopg:///freqtrade_ai_v13_restore_phase9_003"
            ),
            restore_database_name="freqtrade_ai_v13_restore_phase9_003",
            archive_path=archive,
            manifest_path=manifest_path,
            pg_restore_binary=sys.executable,
            runner=lambda command: subprocess.CompletedProcess(command, 0, b"", b""),
            release_guard=lambda: "a" * 40,
            database_inspector=lambda _url, require_zero: next(observations),
        )
