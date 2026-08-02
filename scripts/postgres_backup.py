#!/usr/bin/env python3
"""Create a secret-excluding logical backup of the single local PostgreSQL."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.db.migrations import (
    SCHEMA_VERSION,
    VERSION_TABLE,
    psql_database_url,
    verify_schema,
)
from app.models import Base


EXCLUDED_TABLES = (
    "public.okx_demo_attestation_secrets",
    "public.okx_demo_operator_consent_secrets",
    "public.{}".format(VERSION_TABLE),
)


class BackupBlocked(RuntimeError):
    pass


def restore_data_tables() -> tuple[str, ...]:
    """Return every data table controlled by the v28 restore contract."""

    secret_tables = (table.split(".", 1)[1] for table in EXCLUDED_TABLES[:2])
    return tuple(sorted(set(Base.metadata.tables) | set(secret_tables)))


def require_empty_v28_restore_target(database_url: str) -> None:
    """Fail before COPY unless the target is an empty, exact v28 schema."""

    parsed_peer = make_url(database_url)
    admin_engine = create_engine(
        parsed_peer.set(drivername="postgresql+psycopg")
    )
    try:
        readiness = verify_schema(admin_engine)
        if not readiness.ready or readiness.schema_version != SCHEMA_VERSION:
            raise BackupBlocked("restore target is not an exact v28 schema")
        with admin_engine.connect() as connection:
            schema_name = connection.execute(
                text("SELECT current_schema()")
            ).scalar_one()
            existing = set(inspect(connection).get_table_names(schema=schema_name))
            managed = set(restore_data_tables())
            if managed - existing:
                raise BackupBlocked("restore target is missing managed tables")
            for table_name in sorted(managed):
                if connection.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM {}.{} LIMIT 1)".format(
                        connection.dialect.identifier_preparer.quote_schema(schema_name),
                        connection.dialect.identifier_preparer.quote(table_name),
                    )
                )).scalar_one():
                    raise BackupBlocked(
                        "restore target contains managed data: {}".format(table_name)
                    )
    finally:
        admin_engine.dispose()


def restore_transaction_preflight_sql() -> str:
    """Build the authoritative restore checks run under the import locks."""

    managed_tables = list(restore_data_tables())
    locked_tables = [VERSION_TABLE, *managed_tables]

    def qualified(table_name: str) -> str:
        return 'public."{}"'.format(table_name.replace('"', '""'))

    checks = []
    for table_name in managed_tables:
        checks.append(
            "IF EXISTS (SELECT 1 FROM {} LIMIT 1) THEN\n"
            "    RAISE EXCEPTION 'restore target contains managed data: {}';\n"
            "END IF;".format(qualified(table_name), table_name)
        )
    return (
        "LOCK TABLE {} IN ACCESS EXCLUSIVE MODE;\n"
        "DO $restore_preflight$\n"
        "DECLARE\n"
        "    version_count bigint;\n"
        "    version_value text;\n"
        "BEGIN\n"
        "    SELECT count(*), min(version) INTO version_count, version_value\n"
        "    FROM {};\n"
        "    IF version_count <> 1 OR version_value IS DISTINCT FROM '{}' THEN\n"
        "        RAISE EXCEPTION 'restore target is not an exact v28 schema';\n"
        "    END IF;\n"
        "    {}\n"
        "END\n"
        "$restore_preflight$;"
    ).format(
        ", ".join(qualified(table_name) for table_name in locked_tables),
        qualified(VERSION_TABLE),
        SCHEMA_VERSION.replace("'", "''"),
        "\n    ".join(checks),
    )


def peer_admin_database_url(database_url: str) -> str:
    """Return the local peer-admin URL without copying runtime credentials."""
    parsed = make_url(database_url)
    if (
        parsed.get_backend_name() != "postgresql"
        or parsed.host not in {"localhost", "127.0.0.1", "::1"}
        or parsed.database != "freqtrade_ai"
    ):
        raise BackupBlocked(
            "backup is restricted to the canonical local PostgreSQL database"
        )
    peer = URL.create(
        drivername="postgresql",
        database=parsed.database,
        query={
            "host": "/tmp",
            "port": str(parsed.port or 5432),
        },
    )
    return peer.render_as_string(hide_password=False)


def create_backup(
    *,
    database_url: str,
    output_dir: Path,
    pg_dump_binary: Optional[str] = None,
) -> tuple[Path, Path]:
    if not database_url.strip():
        raise BackupBlocked("DATABASE_URL is required")
    binary = pg_dump_binary or shutil.which("pg_dump")
    if not binary:
        raise BackupBlocked("pg_dump is unavailable")

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    identity = "{}-{}".format(stamp, uuid4().hex)
    backup_path = output_dir / "freqtrade-ai-{}.sql".format(identity)
    manifest_path = output_dir / "freqtrade-ai-{}.manifest.json".format(identity)
    backup_tmp = output_dir / ".{}.tmp".format(backup_path.name)
    manifest_tmp = output_dir / ".{}.tmp".format(manifest_path.name)

    argv = [
        binary,
        "--data-only",
        "--format=plain",
        "--no-owner",
        "--no-privileges",
    ]
    argv.extend("--exclude-table={}".format(table) for table in EXCLUDED_TABLES)
    argv.append(psql_database_url(peer_admin_database_url(database_url)))
    try:
        descriptor = os.open(
            backup_tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            completed = subprocess.run(
                argv,
                check=False,
                stdout=output,
                stderr=subprocess.PIPE,
            )
            output.flush()
            os.fsync(output.fileno())
        if completed.returncode != 0 or backup_tmp.stat().st_size < 100:
            raise BackupBlocked(
                "pg_dump failed; protected attestation secrets remain excluded"
            )
        digest_builder = hashlib.sha256()
        with backup_tmp.open("rb") as backup_input:
            for chunk in iter(lambda: backup_input.read(1024 * 1024), b""):
                digest_builder.update(chunk)
        digest = digest_builder.hexdigest()
        manifest = {
            "schema_version": "1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "backup_file": backup_path.name,
            "sha256": digest,
            "kind": "postgresql-data-only",
            "excluded_tables": list(EXCLUDED_TABLES),
            "reattestation_required_after_restore": True,
            (
                "reauthorization_required_"
                "after_restore"
            ): True,
            "credential_values_recorded": False,
            "restore_transaction": "single-transaction",
            "restore_requires_empty_v28_schema": True,
        }
        manifest_descriptor = os.open(
            manifest_tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(manifest_descriptor, "w", encoding="utf-8") as output:
            json.dump(manifest, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(backup_tmp, backup_path)
        os.replace(manifest_tmp, manifest_path)
        return backup_path, manifest_path
    except Exception:
        backup_tmp.unlink(missing_ok=True)
        manifest_tmp.unlink(missing_ok=True)
        raise


def restore_backup(
    *,
    database_url: str,
    backup_path: Path,
    manifest_path: Path,
    psql_binary: Optional[str] = None,
) -> None:
    """Restore a verified data-only dump atomically into an empty v28 schema."""

    binary = psql_binary or shutil.which("psql")
    if not binary:
        raise BackupBlocked("psql is unavailable")
    backup_path = backup_path.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupBlocked("backup manifest is unreadable") from exc
    if (
        manifest.get("backup_file") != backup_path.name
        or manifest.get("kind") != "postgresql-data-only"
        or manifest.get("excluded_tables") != list(EXCLUDED_TABLES)
        or manifest.get("restore_transaction") != "single-transaction"
        or manifest.get("restore_requires_empty_v28_schema") is not True
        or manifest.get("reattestation_required_after_restore") is not True
        or manifest.get("reauthorization_required_after_restore") is not True
        or manifest.get("credential_values_recorded") is not False
    ):
        raise BackupBlocked("backup manifest violates the controlled restore contract")
    digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    if digest != str(manifest.get("sha256", "")):
        raise BackupBlocked("backup checksum mismatch")
    peer_url = peer_admin_database_url(database_url)
    require_empty_v28_restore_target(peer_url)
    argv = [
        binary,
        "--single-transaction",
        "--set=ON_ERROR_STOP=1",
        "--command={}".format(restore_transaction_preflight_sql()),
        "--file={}".format(backup_path),
        psql_database_url(peer_url),
    ]
    completed = subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise BackupBlocked("atomic PostgreSQL restore failed")


def main() -> int:
    try:
        backup, manifest = create_backup(
            database_url=os.environ.get("DATABASE_URL", ""),
            output_dir=REPO_ROOT / ".freqtrade-ai" / "backups",
        )
    except BackupBlocked as exc:
        print("status=BLOCKED")
        print("reason={}".format(exc))
        return 2
    print("status=READY")
    print("backup={}".format(backup))
    print("manifest={}".format(manifest))
    print("reattestation_required_after_restore=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
