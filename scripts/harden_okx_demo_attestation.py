#!/usr/bin/env python3
"""One-shot local peer-admin hardening for the OKX Demo attestation boundary."""

from __future__ import annotations

import os
import ipaddress
import pwd
import secrets
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.adapters.okx_demo.attestation_proof import (  # noqa: E402
    ATTESTATION_PROOF_KEY_ENV,
    ATTESTATION_PROOF_KEYCHAIN_SERVICE,
)
from app.db.migrations import (  # noqa: E402
    harden_attestation_access_boundary,
    upgrade_database,
    verify_schema,
)
from app.db.session import create_database_engine  # noqa: E402


class HardeningBlocked(RuntimeError):
    pass


def _keychain_key() -> str:
    if sys.platform != "darwin" or not Path("/usr/bin/security").is_file():
        raise HardeningBlocked("macOS Keychain is required")
    account = pwd.getpwuid(os.getuid()).pw_name
    common = [
        "-a",
        account,
        "-s",
        ATTESTATION_PROOF_KEYCHAIN_SERVICE,
    ]
    found = subprocess.run(
        ["/usr/bin/security", "find-generic-password", *common, "-w"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        stdin=subprocess.DEVNULL,
    )
    if found.returncode == 0:
        value = found.stdout.rstrip("\r\n")
    elif found.returncode == 44:
        value = secrets.token_hex(32)
        added = subprocess.run(
            ["/usr/bin/security", "add-generic-password", *common, "-w"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            input=value + "\n" + value + "\n",
        )
        if added.returncode != 0:
            raise HardeningBlocked("attestation proof key Keychain write failed")
    else:
        raise HardeningBlocked("attestation proof key Keychain read failed")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise HardeningBlocked("attestation proof key Keychain value is invalid")
    return value


def _database_identity(engine) -> tuple[str, str, int, str, int, str]:
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT current_database(), "
                    "COALESCE(inet_server_addr()::text, 'local'), "
                    "COALESCE(inet_server_port(), "
                    "current_setting('port')::integer), current_user, "
                    "(SELECT oid FROM pg_database "
                    " WHERE datname = current_database()), "
                    "(SELECT system_identifier::text FROM pg_control_system())"
                )
            ).one()
    except BaseException:
        raise HardeningBlocked(
            "database cluster identity cannot be proven"
        ) from None
    return (
        str(row[0]),
        str(row[1]),
        int(row[2]),
        str(row[3]),
        int(row[4]),
        str(row[5]),
    )


def _is_local_server_address(value: str) -> bool:
    if value == "local":
        return True
    try:
        return ipaddress.ip_interface(value).ip.is_loopback
    except ValueError:
        return False


def main() -> int:
    runtime_raw = os.environ.get("DATABASE_URL", "")
    if not runtime_raw:
        raise HardeningBlocked("DATABASE_URL is required")
    runtime_url = make_url(runtime_raw)
    if (
        runtime_url.get_backend_name() != "postgresql"
        or runtime_url.host not in {"localhost", "127.0.0.1", "::1"}
        or runtime_url.database != "freqtrade_ai"
    ):
        raise HardeningBlocked("hardening is restricted to the canonical local database")
    runtime_engine = create_database_engine(runtime_raw)
    runtime_identity = _database_identity(runtime_engine)
    if runtime_identity[0] != runtime_url.database or runtime_identity[3] != "freqtrade":
        raise HardeningBlocked("runtime database identity is not canonical")

    peer_url = URL.create(
        drivername=runtime_url.drivername,
        database=runtime_url.database,
        query={
            "host": "/tmp",
            "port": str(runtime_url.port or 5432),
        },
    )
    admin_engine = create_database_engine(peer_url.render_as_string(hide_password=False))
    admin_identity = _database_identity(admin_engine)
    if (
        admin_identity[0] != runtime_identity[0]
        or admin_identity[2] != runtime_identity[2]
        or admin_identity[4] != runtime_identity[4]
        or admin_identity[5] != runtime_identity[5]
        or not _is_local_server_address(runtime_identity[1])
        or not _is_local_server_address(admin_identity[1])
        or admin_identity[3] == "freqtrade"
    ):
        raise HardeningBlocked("peer admin is not connected to the same local database")

    key = _keychain_key()
    os.environ[ATTESTATION_PROOF_KEY_ENV] = key
    try:
        version = upgrade_database(admin_engine)
        harden_attestation_access_boundary(admin_engine)
        readiness = verify_schema(runtime_engine)
    finally:
        os.environ.pop(ATTESTATION_PROOF_KEY_ENV, None)
        key = ""
    if not readiness.ready:
        raise HardeningBlocked("attestation hardening verification failed")
    print("database=freqtrade_ai")
    print("schema_version={}".format(version))
    print("attestation_boundary=READY")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HardeningBlocked as exc:
        print("BLOCKED: {}".format(exc), file=sys.stderr)
        raise SystemExit(2)
