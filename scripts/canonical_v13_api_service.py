#!/usr/bin/env python3
"""Provision and manage the loopback-only canonical V1.3 API on macOS."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import plistlib
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence
from urllib.error import URLError
from urllib.request import urlopen

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL


LABEL = "com.he1met.freqtrade-ai.v13-canonical-api"
DATABASE_NAME = "freqtrade_ai_v13"
DATABASE_HOST = "127.0.0.1"
DATABASE_PORT = 5432
DEFAULT_API_PORT = 8011
READER_PRINCIPAL = "freqtrade_ai_v13_api_login"
CONTROL_PRINCIPAL = "freqtrade_ai_v13_control_login"
READER_CAPABILITY = "freqtrade_ai_v13_api_reader"
CONTROL_CAPABILITY = "freqtrade_ai_v13_control_writer"
READER_KEYCHAIN_SERVICE = "freqtrade-ai/v13/api-reader-password"
CONTROL_KEYCHAIN_SERVICE = "freqtrade-ai/v13/control-password"
RESEARCH_PRINCIPAL_SPECS = (
    (
        "freqtrade_ai_v13_validation_login",
        "freqtrade_ai_v13_validation_writer",
        "freqtrade-ai/v13/research-validation-password",
    ),
    (
        "freqtrade_ai_v13_scoring_login",
        "freqtrade_ai_v13_scoring_writer",
        "freqtrade-ai/v13/research-scoring-password",
    ),
    (
        "freqtrade_ai_v13_qualification_login",
        "freqtrade_ai_v13_qualification_writer",
        "freqtrade-ai/v13/research-qualification-password",
    ),
    (
        "freqtrade_ai_v13_optimization_login",
        "freqtrade_ai_v13_optimization_writer",
        "freqtrade-ai/v13/research-optimization-password",
    ),
)
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PYTHON = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
SCRIPT_PATH = Path(__file__).resolve()
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "FreqtradeAiV13"
STDOUT_LOG = LOG_DIR / "canonical-api.log"
STDERR_LOG = LOG_DIR / "canonical-api-error.log"


class CanonicalServiceBlocked(RuntimeError):
    pass


def _scram_verifier(material: str, *, salt: bytes | None = None) -> str:
    """Create a PostgreSQL SCRAM verifier without placing plaintext in SQL."""

    resolved_salt = secrets.token_bytes(16) if salt is None else salt
    if len(resolved_salt) < 16:
        raise CanonicalServiceBlocked("BLOCKED_SCRAM_SALT_INVALID")
    iterations = 4096
    salted = hashlib.pbkdf2_hmac(
        "sha256", material.encode("utf-8"), resolved_salt, iterations
    )
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    encoded_salt = base64.b64encode(resolved_salt).decode("ascii")
    encoded_stored = base64.b64encode(stored_key).decode("ascii")
    encoded_server = base64.b64encode(server_key).decode("ascii")
    return (
        f"SCRAM-SHA-256${iterations}:{encoded_salt}$"
        f"{encoded_stored}:{encoded_server}"
    )


def _security_command() -> Path:
    path = Path("/usr/bin/security")
    if sys.platform != "darwin" or not path.is_file():
        raise CanonicalServiceBlocked("BLOCKED_MACOS_KEYCHAIN_REQUIRED")
    return path


def _keychain_account() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def _read_keychain(service: str) -> str | None:
    completed = subprocess.run(
        [
            str(_security_command()),
            "find-generic-password",
            "-a",
            _keychain_account(),
            "-s",
            service,
            "-w",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode == 44:
        return None
    if completed.returncode != 0:
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_READ_FAILED")
    material = completed.stdout.rstrip("\r\n")
    if len(material) < 48 or any(character in material for character in "\x00\r\n"):
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_VALUE_INVALID")
    return material


def _keychain_item_exists(service: str) -> bool:
    completed = subprocess.run(
        [
            str(_security_command()),
            "find-generic-password",
            "-a",
            _keychain_account(),
            "-s",
            service,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode == 44:
        return False
    if completed.returncode != 0:
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_READ_FAILED")
    return True


def _add_keychain(service: str, material: str) -> None:
    if _read_keychain(service) is not None:
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_ITEM_ALREADY_EXISTS")
    completed = subprocess.run(
        [
            str(_security_command()),
            "add-generic-password",
            "-a",
            _keychain_account(),
            "-s",
            service,
            "-w",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        input=material + "\n" + material + "\n",
    )
    if completed.returncode != 0 or _read_keychain(service) != material:
        _delete_new_keychain(service)
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_WRITE_FAILED")


def _delete_new_keychain(service: str) -> None:
    subprocess.run(
        [
            str(_security_command()),
            "delete-generic-password",
            "-a",
            _keychain_account(),
            "-s",
            service,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        stdin=subprocess.DEVNULL,
    )


def _admin_connection() -> psycopg.Connection[Any]:
    connection = psycopg.connect(f"dbname={DATABASE_NAME}")
    row = connection.execute(
        "SELECT current_database(), current_setting('is_superuser')"
    ).fetchone()
    if row != (DATABASE_NAME, "on"):
        connection.close()
        raise CanonicalServiceBlocked("BLOCKED_LOCAL_PROVISIONER_AUTHORITY")
    return connection


def provision_principals() -> dict[str, object]:
    services = (READER_KEYCHAIN_SERVICE, CONTROL_KEYCHAIN_SERVICE)
    if any(_read_keychain(service) is not None for service in services):
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_ITEM_ALREADY_EXISTS")
    with _admin_connection() as connection:
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                ([READER_PRINCIPAL, CONTROL_PRINCIPAL],),
            )
        }
        capabilities = {
            str(row[0])
            for row in connection.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                ([READER_CAPABILITY, CONTROL_CAPABILITY],),
            )
        }
    if existing:
        raise CanonicalServiceBlocked("BLOCKED_LOGIN_PRINCIPAL_ALREADY_EXISTS")
    if capabilities != {READER_CAPABILITY, CONTROL_CAPABILITY}:
        raise CanonicalServiceBlocked("BLOCKED_CAPABILITY_ROLE_MISSING")

    reader_material = secrets.token_urlsafe(48)
    control_material = secrets.token_urlsafe(48)
    added: list[str] = []
    provisioned = False
    try:
        _add_keychain(READER_KEYCHAIN_SERVICE, reader_material)
        added.append(READER_KEYCHAIN_SERVICE)
        _add_keychain(CONTROL_KEYCHAIN_SERVICE, control_material)
        added.append(CONTROL_KEYCHAIN_SERVICE)
        with _admin_connection() as connection:
            with connection.transaction():
                for principal, capability, material in (
                    (READER_PRINCIPAL, READER_CAPABILITY, reader_material),
                    (CONTROL_PRINCIPAL, CONTROL_CAPABILITY, control_material),
                ):
                    auth_verifier = _scram_verifier(material)
                    connection.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS "
                            "CONNECTION LIMIT 8 PASSWORD {}"
                        ).format(
                            sql.Identifier(principal),
                            sql.Literal(auth_verifier),
                        )
                    )
                    connection.execute(
                        sql.SQL("GRANT {} TO {}").format(
                            sql.Identifier(capability), sql.Identifier(principal)
                        )
                    )
        provisioned = True
    finally:
        if not provisioned:
            for service in reversed(added):
                _delete_new_keychain(service)
    return {
        "status": "PROVISIONED",
        "database": DATABASE_NAME,
        "principals": [READER_PRINCIPAL, CONTROL_PRINCIPAL],
        "capabilities": [READER_CAPABILITY, CONTROL_CAPABILITY],
        "keychain_items": len(added),
    }


def _require_research_authority_preprovisioned() -> None:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from sqlalchemy import create_engine  # noqa: PLC0415

    from app.canonical_v13.authority_upgrade import (  # noqa: PLC0415
        verify_authority_upgrade_state,
    )
    from app.canonical_v13.bootstrap import (  # noqa: PLC0415
        local_legacy_research_writer_role,
        local_role_mapping,
    )

    engine = create_engine(
        URL.create("postgresql+psycopg", database=DATABASE_NAME),
        pool_pre_ping=True,
    )
    try:
        with engine.connect() as connection:
            verification = verify_authority_upgrade_state(
                connection,
                role_mapping=local_role_mapping(),
                legacy_research_writer_role=local_legacy_research_writer_role(),
                require_no_research_rows=True,
            )
    except Exception as exc:
        raise CanonicalServiceBlocked(
            "BLOCKED_RESEARCH_AUTHORITY_PREFLIGHT"
        ) from exc
    finally:
        engine.dispose()
    if not verification.accepted or verification.state != "CURRENT":
        raise CanonicalServiceBlocked("BLOCKED_RESEARCH_AUTHORITY_PREFLIGHT")


def _grant_research_database_connect(
    connection: psycopg.Connection[Any],
) -> None:
    for _principal, capability, _service in RESEARCH_PRINCIPAL_SPECS:
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(DATABASE_NAME),
                sql.Identifier(capability),
            )
        )


def provision_research_principals() -> dict[str, object]:
    """Add four exact research LOGINs without modifying existing API principals."""

    services = tuple(spec[2] for spec in RESEARCH_PRINCIPAL_SPECS)
    if any(_read_keychain(service) is not None for service in services):
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_ITEM_ALREADY_EXISTS")
    _require_research_authority_preprovisioned()
    principals = tuple(spec[0] for spec in RESEARCH_PRINCIPAL_SPECS)
    capabilities = tuple(spec[1] for spec in RESEARCH_PRINCIPAL_SPECS)
    with _admin_connection() as connection:
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (list(principals),),
            )
        }
        observed_capabilities = {
            str(row[0])
            for row in connection.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (list(capabilities),),
            )
        }
    if existing:
        raise CanonicalServiceBlocked("BLOCKED_LOGIN_PRINCIPAL_ALREADY_EXISTS")
    if observed_capabilities != set(capabilities):
        raise CanonicalServiceBlocked("BLOCKED_CAPABILITY_ROLE_MISSING")

    materials = {principal: secrets.token_urlsafe(48) for principal in principals}
    added: list[str] = []
    provisioned = False
    try:
        for principal, _capability, service in RESEARCH_PRINCIPAL_SPECS:
            _add_keychain(service, materials[principal])
            added.append(service)
        with _admin_connection() as connection:
            with connection.transaction():
                for principal, capability, _service in RESEARCH_PRINCIPAL_SPECS:
                    auth_verifier = _scram_verifier(materials[principal])
                    connection.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS "
                            "CONNECTION LIMIT 4 PASSWORD {}"
                        ).format(
                            sql.Identifier(principal),
                            sql.Literal(auth_verifier),
                        )
                    )
                    connection.execute(
                        sql.SQL("GRANT {} TO {}").format(
                            sql.Identifier(capability), sql.Identifier(principal)
                        )
                    )
                _grant_research_database_connect(connection)
        provisioned = True
    finally:
        if not provisioned:
            for service in reversed(added):
                _delete_new_keychain(service)
    return {
        "status": "PROVISIONED",
        "database": DATABASE_NAME,
        "principals": list(principals),
        "capabilities": list(capabilities),
        "keychain_items": len(added),
    }


def _verify_research_provisioned_state():
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from sqlalchemy import create_engine  # noqa: PLC0415

    from app.canonical_v13.bootstrap import (  # noqa: PLC0415
        LOCAL_RESEARCH_SERVICE_PRINCIPALS,
        LOCAL_SERVICE_PRINCIPALS,
        local_role_mapping,
        verify_postgresql_bootstrap,
    )

    service_principals = dict(LOCAL_SERVICE_PRINCIPALS)
    service_principals.update(LOCAL_RESEARCH_SERVICE_PRINCIPALS)
    engine = create_engine(
        URL.create("postgresql+psycopg", database=DATABASE_NAME),
        pool_pre_ping=True,
    )
    try:
        with engine.connect() as connection:
            return verify_postgresql_bootstrap(
                connection,
                role_mapping=local_role_mapping(),
                require_zero_business_rows=False,
                service_principals=service_principals,
            )
    finally:
        engine.dispose()


def repair_research_database_connect() -> dict[str, object]:
    """Repair only the exact all-missing CONNECT state without touching secrets."""

    services = tuple(spec[2] for spec in RESEARCH_PRINCIPAL_SPECS)
    if any(not _keychain_item_exists(service) for service in services):
        raise CanonicalServiceBlocked("BLOCKED_RESEARCH_KEYCHAIN_INCOMPLETE")
    before = _verify_research_provisioned_state()
    if before.problems != ("missing service database CONNECT count=4",):
        raise CanonicalServiceBlocked("BLOCKED_RESEARCH_CONNECT_REPAIR_PREFLIGHT")

    capabilities = tuple(spec[1] for spec in RESEARCH_PRINCIPAL_SPECS)
    with _admin_connection() as connection:
        connect_states = {
            str(row[0]): bool(row[1])
            for row in connection.execute(
                """
                SELECT rolname,
                       has_database_privilege(
                           rolname, current_database(), 'CONNECT'
                       )
                FROM pg_catalog.pg_roles
                WHERE rolname = ANY(%s)
                """,
                (list(capabilities),),
            )
        }
        if set(connect_states) != set(capabilities) or any(
            connect_states.values()
        ):
            raise CanonicalServiceBlocked(
                "BLOCKED_RESEARCH_CONNECT_REPAIR_PARTIAL"
            )
        with connection.transaction():
            _grant_research_database_connect(connection)

    after = _verify_research_provisioned_state()
    if not after.accepted:
        raise CanonicalServiceBlocked("BLOCKED_RESEARCH_CONNECT_REPAIR_POSTVERIFY")
    return {
        "status": "REPAIRED",
        "database": DATABASE_NAME,
        "capabilities": list(capabilities),
        "database_connect_grants": len(capabilities),
        "keychain_items_modified": 0,
    }


def _database_url(principal: str, service: str) -> str:
    value = _read_keychain(service)
    if value is None:
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_ITEM_MISSING")
    return URL.create(
        "postgresql+psycopg",
        username=principal,
        password=value.strip(),
        host=DATABASE_HOST,
        port=DATABASE_PORT,
        database=DATABASE_NAME,
    ).render_as_string(hide_password=False)


def canonical_control_database_url() -> str:
    """Return the in-memory control DSN without printing or persisting it."""

    return _database_url(CONTROL_PRINCIPAL, CONTROL_KEYCHAIN_SERVICE)


def _production_database_environment() -> dict[str, str]:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from app.canonical_v13.production import (  # noqa: PLC0415
        CONTROL_DATABASE_URL_ENV,
        READER_DATABASE_URL_ENV,
    )
    from app.canonical_v13.research_persistence import (  # noqa: PLC0415
        OPTIMIZATION_DATABASE_URL_ENV,
        QUALIFICATION_DATABASE_URL_ENV,
        SCORING_DATABASE_URL_ENV,
        VALIDATION_DATABASE_URL_ENV,
    )

    research_environment_names = (
        VALIDATION_DATABASE_URL_ENV,
        SCORING_DATABASE_URL_ENV,
        QUALIFICATION_DATABASE_URL_ENV,
        OPTIMIZATION_DATABASE_URL_ENV,
    )
    return {
        READER_DATABASE_URL_ENV: _database_url(
            READER_PRINCIPAL, READER_KEYCHAIN_SERVICE
        ),
        CONTROL_DATABASE_URL_ENV: _database_url(
            CONTROL_PRINCIPAL, CONTROL_KEYCHAIN_SERVICE
        ),
        **{
            environment_name: _database_url(principal, service)
            for environment_name, (principal, _capability, service) in zip(
                research_environment_names,
                RESEARCH_PRINCIPAL_SPECS,
                strict=True,
            )
        },
    }


def require_release_checkout() -> None:
    """Expose the existing clean/exact-main release guard to sibling tools."""

    _require_release_checkout()


def serve(port: int) -> None:
    if not 1024 <= port <= 65535:
        raise CanonicalServiceBlocked("BLOCKED_INVALID_LOOPBACK_PORT")
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from app.canonical_v13.production import (  # noqa: PLC0415
        create_app,
    )
    import uvicorn  # noqa: PLC0415

    app = create_app(
        _production_database_environment(),
        market_artifact_root=REPO_ROOT / "user_data" / "data",
    )
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


def _launchctl_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )


def _require_release_checkout() -> None:
    if ".codex/worktrees" in str(REPO_ROOT):
        raise CanonicalServiceBlocked("BLOCKED_CANONICAL_RELEASE_CHECKOUT_REQUIRED")
    status_result = _run(["git", "status", "--porcelain"])
    head_result = _run(["git", "rev-parse", "HEAD"])
    main_result = _run(["git", "rev-parse", "origin/main"])
    if (
        status_result.returncode != 0
        or status_result.stdout.strip()
        or head_result.returncode != 0
        or main_result.returncode != 0
        or head_result.stdout.strip() != main_result.stdout.strip()
    ):
        raise CanonicalServiceBlocked("BLOCKED_CANONICAL_RELEASE_CHECKOUT_REQUIRED")
    if not BACKEND_PYTHON.is_file():
        raise CanonicalServiceBlocked("BLOCKED_BACKEND_VIRTUALENV_MISSING")


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        try:
            candidate.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _plist_payload(port: int) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(BACKEND_PYTHON),
            str(SCRIPT_PATH),
            "serve",
            "--port",
            str(port),
        ],
        "WorkingDirectory": str(REPO_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(STDOUT_LOG),
        "StandardErrorPath": str(STDERR_LOG),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
            "FREQTRADE_AI_DISABLE_ENV_FILE": "1",
        },
    }


def install(port: int) -> dict[str, object]:
    _require_release_checkout()
    if shutil.which("launchctl") is None:
        raise CanonicalServiceBlocked("BLOCKED_LAUNCHCTL_REQUIRED")
    if not _port_available(port):
        raise CanonicalServiceBlocked("BLOCKED_LOOPBACK_PORT_IN_USE")
    required_services = (
        READER_KEYCHAIN_SERVICE,
        CONTROL_KEYCHAIN_SERVICE,
        *(spec[2] for spec in RESEARCH_PRINCIPAL_SPECS),
    )
    if any(_read_keychain(service) is None for service in required_services):
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_ITEM_MISSING")
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PLIST_PATH.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(_plist_payload(port), handle, sort_keys=True)
    temporary.replace(PLIST_PATH)
    _run(["launchctl", "bootout", _launchctl_target()])
    completed = _run(
        ["launchctl", "bootstrap", _launchctl_domain(), str(PLIST_PATH)]
    )
    if completed.returncode != 0:
        raise CanonicalServiceBlocked("BLOCKED_LAUNCHCTL_BOOTSTRAP_FAILED")
    return {"status": "INSTALLED", "label": LABEL, "port": port}


def status(port: int) -> dict[str, object]:
    launch = _run(["launchctl", "print", _launchctl_target()])
    loaded = launch.returncode == 0
    health = "UNAVAILABLE"
    ready = "UNAVAILABLE"
    if loaded:
        for path, target in (("healthz", "health"), ("readyz", "ready")):
            try:
                with urlopen(
                    f"http://127.0.0.1:{port}/{path}", timeout=2
                ) as response:
                    payload = json.loads(response.read())
                value = str(payload.get("status", "UNKNOWN"))
            except (OSError, URLError, ValueError, json.JSONDecodeError):
                value = "UNAVAILABLE"
            if target == "health":
                health = value
            else:
                ready = value
    return {
        "status": "READY" if loaded and ready == "READY" else "BLOCKED",
        "label": LABEL,
        "loaded": loaded,
        "health": health,
        "ready": ready,
        "port": port,
    }


def restart(port: int) -> dict[str, object]:
    _require_release_checkout()
    if not PLIST_PATH.is_file():
        raise CanonicalServiceBlocked("BLOCKED_LAUNCH_AGENT_MISSING")
    kicked = _run(["launchctl", "kickstart", "-k", _launchctl_target()])
    if kicked.returncode != 0:
        raise CanonicalServiceBlocked("BLOCKED_SERVICE_RESTART_FAILED")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observed = status(port)
        if observed["status"] == "READY":
            return {**observed, "status": "RESTARTED"}
        time.sleep(0.25)
    raise CanonicalServiceBlocked("BLOCKED_SERVICE_RESTART_TIMEOUT")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "provision",
            "provision-research",
            "repair-research-connect",
            "serve",
            "install",
            "status",
            "restart",
        ),
    )
    parser.add_argument("--port", type=int, default=DEFAULT_API_PORT)
    args = parser.parse_args(argv)
    try:
        if args.command == "provision":
            payload = provision_principals()
        elif args.command == "provision-research":
            payload = provision_research_principals()
        elif args.command == "repair-research-connect":
            payload = repair_research_database_connect()
        elif args.command == "serve":
            serve(args.port)
            return 0
        elif args.command == "install":
            payload = install(args.port)
        elif args.command == "restart":
            payload = restart(args.port)
        else:
            payload = status(args.port)
    except CanonicalServiceBlocked as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] in {"PROVISIONED", "REPAIRED", "INSTALLED", "READY", "RESTARTED"} else 2


if __name__ == "__main__":
    sys.exit(main())
