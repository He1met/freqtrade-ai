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

from sqlalchemy.engine import URL, make_url


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.db.migrations import psql_database_url


EXCLUDED_TABLES = ("public.okx_demo_attestation_secrets",)


class BackupBlocked(RuntimeError):
    pass


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
        "--exclude-table={}".format(EXCLUDED_TABLES[0]),
        psql_database_url(peer_admin_database_url(database_url)),
    ]
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
            "credential_values_recorded": False,
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
