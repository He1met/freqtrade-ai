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
import uuid
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
PHASE9_PRINCIPAL_SPECS = tuple(
    (
        f"freqtrade_ai_v13_{name}_login",
        f"freqtrade_ai_v13_{name}_writer",
        f"freqtrade-ai/v13/phase9-{name}-password",
    )
    for name in (
        "approval",
        "deployment",
        "signal",
        "risk",
        "order",
        "fill",
        "ledger",
        "reconciliation",
    )
)
RUNTIME_READER_PRINCIPAL_SPEC = (
    "freqtrade_ai_v13_runtime_login",
    "freqtrade_ai_v13_runtime_reader",
    "freqtrade-ai/v13/runtime-reader-password",
)
RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE = (
    "freqtrade-ai/v13/runtime-signal-receipt-hmac-v1"
)
PHASE9_CLEANUP_LAUNCH_AGENT_LABELS = (
    LABEL,
    "ai.freqtrade.canonical-v13.runtime",
    "ai.freqtrade.canonical-v13.order-writer",
)
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PYTHON = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
SCRIPT_PATH = Path(__file__).resolve()
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "FreqtradeAiV13"
STDOUT_LOG = LOG_DIR / "canonical-api.log"
STDERR_LOG = LOG_DIR / "canonical-api-error.log"
READER_ROTATION_AGGREGATE_TYPE = "api_reader_credential_rotation"
READER_ROTATION_EVENT = "API_READER_CREDENTIAL_ROTATED"


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
        f"SCRAM-SHA-256${iterations}:{encoded_salt}${encoded_stored}:{encoded_server}"
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


def _replace_keychain(service: str, material: str) -> None:
    """Replace one fixed Keychain item without putting material in argv/logs."""

    if not _keychain_item_exists(service):
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_ITEM_MISSING")
    completed = subprocess.run(
        [
            str(_security_command()),
            "add-generic-password",
            "-U",
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
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_REPLACE_FAILED")


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


def _delete_keychain_strict(service: str) -> bool:
    """Delete one fixed item and prove absence without reading its value."""

    if not _keychain_item_exists(service):
        return False
    completed = subprocess.run(
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
    if completed.returncode != 0 or _keychain_item_exists(service):
        raise CanonicalServiceBlocked("BLOCKED_PHASE9_CLEANUP_KEYCHAIN_DELETE")
    return True


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
    from app.canonical_v13.authority_upgrade import (  # noqa: PLC0415
        verify_authority_upgrade_state,
    )
    from app.canonical_v13.bootstrap import (  # noqa: PLC0415
        local_legacy_research_writer_role,
        local_role_mapping,
    )
    from sqlalchemy import create_engine  # noqa: PLC0415

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
        raise CanonicalServiceBlocked("BLOCKED_RESEARCH_AUTHORITY_PREFLIGHT") from exc
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


def _require_phase9_schema_preprovisioned() -> None:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from app.canonical_v13.bootstrap import local_role_mapping  # noqa: PLC0415
    from app.canonical_v13.phase9_schema_upgrade import (  # noqa: PLC0415
        verify_phase9_schema_upgrade,
    )
    from app.canonical_v13.acceptance_signal_trigger_upgrade import (  # noqa: PLC0415
        verify_acceptance_signal_trigger_upgrade,
    )
    from sqlalchemy import create_engine  # noqa: PLC0415

    engine = create_engine(
        URL.create("postgresql+psycopg", database=DATABASE_NAME),
        pool_pre_ping=True,
    )
    try:
        with engine.connect() as connection:
            verification = verify_phase9_schema_upgrade(connection)
            acceptance_trigger = verify_acceptance_signal_trigger_upgrade(
                connection, role_mapping=local_role_mapping()
            )
    except Exception as exc:
        raise CanonicalServiceBlocked("BLOCKED_PHASE9_SCHEMA_PREFLIGHT") from exc
    finally:
        engine.dispose()
    if verification.status != "ACCEPTED" or acceptance_trigger.status != "ACCEPTED":
        raise CanonicalServiceBlocked("BLOCKED_PHASE9_SCHEMA_PREFLIGHT")


def _grant_phase9_database_connect(connection: psycopg.Connection[Any]) -> None:
    for _principal, capability, _service in PHASE9_PRINCIPAL_SPECS:
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(DATABASE_NAME), sql.Identifier(capability)
            )
        )


def provision_phase9_principals() -> dict[str, object]:
    """Add eight exact execution LOGINs only after the Phase 9 schema is current."""

    services = tuple(spec[2] for spec in PHASE9_PRINCIPAL_SPECS)
    if any(_read_keychain(service) is not None for service in services):
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_ITEM_ALREADY_EXISTS")
    _require_phase9_schema_preprovisioned()
    principals = tuple(spec[0] for spec in PHASE9_PRINCIPAL_SPECS)
    capabilities = tuple(spec[1] for spec in PHASE9_PRINCIPAL_SPECS)
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
        for principal, _capability, service in PHASE9_PRINCIPAL_SPECS:
            _add_keychain(service, materials[principal])
            added.append(service)
        with _admin_connection() as connection:
            with connection.transaction():
                for principal, capability, _service in PHASE9_PRINCIPAL_SPECS:
                    connection.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS "
                            "CONNECTION LIMIT 2 PASSWORD {}"
                        ).format(
                            sql.Identifier(principal),
                            sql.Literal(_scram_verifier(materials[principal])),
                        )
                    )
                    connection.execute(
                        sql.SQL("GRANT {} TO {}").format(
                            sql.Identifier(capability), sql.Identifier(principal)
                        )
                    )
                _grant_phase9_database_connect(connection)
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


def provision_runtime_reader() -> dict[str, object]:
    """Provision one non-API runtime reader LOGIN plus its receipt HMAC key."""

    principal, capability, password_service = RUNTIME_READER_PRINCIPAL_SPEC
    services = (password_service, RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE)
    if any(_read_keychain(service) is not None for service in services):
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_ITEM_ALREADY_EXISTS")
    _require_phase9_schema_preprovisioned()
    with _admin_connection() as connection:
        existing = connection.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            ([principal],),
        ).fetchall()
        capability_exists = connection.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            ([capability],),
        ).fetchall()
    if existing:
        raise CanonicalServiceBlocked("BLOCKED_LOGIN_PRINCIPAL_ALREADY_EXISTS")
    if {str(row[0]) for row in capability_exists} != {capability}:
        raise CanonicalServiceBlocked("BLOCKED_CAPABILITY_ROLE_MISSING")
    database_login_material = secrets.token_urlsafe(48)
    signer_key = secrets.token_urlsafe(64)
    added: list[str] = []
    provisioned = False
    try:
        for service, value in (
            (password_service, database_login_material),
            (RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE, signer_key),
        ):
            _add_keychain(service, value)
            added.append(service)
        with _admin_connection() as connection:
            with connection.transaction():
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS "
                        "CONNECTION LIMIT 2 PASSWORD {}"
                    ).format(
                        sql.Identifier(principal),
                        sql.Literal(_scram_verifier(database_login_material)),
                    )
                )
                connection.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(capability), sql.Identifier(principal)
                    )
                )
                connection.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(DATABASE_NAME), sql.Identifier(capability)
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
        "principals": [principal],
        "capabilities": [capability],
        "keychain_items": 2,
        "api_reads_runtime_identity": False,
    }


def _phase9_cleanup_specs() -> tuple[tuple[str, str, str], ...]:
    return (*PHASE9_PRINCIPAL_SPECS, RUNTIME_READER_PRINCIPAL_SPEC)


def _phase9_cleanup_keychain_services() -> tuple[str, ...]:
    return (
        *(spec[2] for spec in _phase9_cleanup_specs()),
        RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE,
    )


def _require_phase9_cleanup_schema_ready() -> None:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from app.canonical_v13.phase9_schema_upgrade import (  # noqa: PLC0415
        verify_phase9_schema_upgrade,
    )
    from sqlalchemy import create_engine  # noqa: PLC0415

    engine = create_engine(
        URL.create("postgresql+psycopg", database=DATABASE_NAME),
        pool_pre_ping=True,
    )
    try:
        with engine.connect() as connection:
            verification = verify_phase9_schema_upgrade(connection)
    except Exception as exc:
        raise CanonicalServiceBlocked(
            "BLOCKED_PHASE9_CLEANUP_SCHEMA_PREFLIGHT"
        ) from exc
    finally:
        engine.dispose()
    if verification.status != "PREVIOUS_READY" or any(
        verification.affected_row_counts.values()
    ):
        raise CanonicalServiceBlocked("BLOCKED_PHASE9_CLEANUP_SCHEMA_PREFLIGHT")


def _require_phase9_cleanup_services_stopped() -> None:
    domain = f"gui/{os.getuid()}"
    for label in PHASE9_CLEANUP_LAUNCH_AGENT_LABELS:
        completed = _run(["launchctl", "print", f"{domain}/{label}"])
        if completed.returncode == 0:
            raise CanonicalServiceBlocked("BLOCKED_PHASE9_CLEANUP_SERVICE_RUNNING")
        if completed.returncode not in {3, 113}:
            raise CanonicalServiceBlocked("BLOCKED_PHASE9_CLEANUP_SERVICE_STATE")


def _phase9_cleanup_role_state(
    connection: psycopg.Connection[Any],
) -> dict[str, tuple[object, ...]]:
    principals = [spec[0] for spec in _phase9_cleanup_specs()]
    rows = connection.execute(
        """
        SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
               rolinherit, rolreplication, rolbypassrls, rolconnlimit
        FROM pg_catalog.pg_roles
        WHERE rolname = ANY(%s)
        """,
        (principals,),
    ).fetchall()
    return {str(row[0]): tuple(row[1:]) for row in rows}


def _require_exact_phase9_cleanup_roles(
    connection: psycopg.Connection[Any],
    observed: dict[str, tuple[object, ...]],
) -> None:
    specs = _phase9_cleanup_specs()
    principals = {spec[0] for spec in specs}
    if set(observed) != principals:
        raise CanonicalServiceBlocked("BLOCKED_PHASE9_CLEANUP_ROLE_PARTIAL")
    expected_attributes = (True, False, False, False, True, False, False, 2)
    if any(attributes != expected_attributes for attributes in observed.values()):
        raise CanonicalServiceBlocked("BLOCKED_PHASE9_CLEANUP_ROLE_ATTRIBUTES")
    memberships = connection.execute(
        """
        SELECT member.rolname, granted.rolname
        FROM pg_catalog.pg_auth_members membership
        JOIN pg_catalog.pg_roles member ON member.oid = membership.member
        JOIN pg_catalog.pg_roles granted ON granted.oid = membership.roleid
        WHERE member.rolname = ANY(%s)
        """,
        (list(principals),),
    ).fetchall()
    if {(str(row[0]), str(row[1])) for row in memberships} != {
        (principal, capability) for principal, capability, _service in specs
    }:
        raise CanonicalServiceBlocked("BLOCKED_PHASE9_CLEANUP_ROLE_MEMBERSHIP")
    active = int(
        connection.execute(
            """
            SELECT count(*)
            FROM pg_catalog.pg_stat_activity
            WHERE usename = ANY(%s) AND pid <> pg_backend_pid()
            """,
            (list(principals),),
        ).fetchone()[0]
    )
    if active:
        raise CanonicalServiceBlocked("BLOCKED_PHASE9_CLEANUP_ACTIVE_SESSION")


def cleanup_phase9_provisioning() -> dict[str, object]:
    """Remove only exact, stopped, empty Phase 9 service provisioning."""

    _require_phase9_cleanup_schema_ready()
    _require_phase9_cleanup_services_stopped()
    specs = _phase9_cleanup_specs()
    services = _phase9_cleanup_keychain_services()
    keychain_present = {
        service for service in services if _keychain_item_exists(service)
    }
    try:
        with _admin_connection() as connection:
            observed = _phase9_cleanup_role_state(connection)
            if observed:
                _require_exact_phase9_cleanup_roles(connection, observed)
                if keychain_present != set(services):
                    raise CanonicalServiceBlocked(
                        "BLOCKED_PHASE9_CLEANUP_KEYCHAIN_PARTIAL"
                    )
                with connection.transaction():
                    for principal, capability, _service in specs:
                        connection.execute(
                            sql.SQL("REVOKE {} FROM {}").format(
                                sql.Identifier(capability), sql.Identifier(principal)
                            )
                        )
                    for principal, _capability, _service in specs:
                        connection.execute(
                            sql.SQL("DROP ROLE {}").format(sql.Identifier(principal))
                        )
                roles_dropped = len(specs)
            else:
                roles_dropped = 0
        with _admin_connection() as connection:
            if _phase9_cleanup_role_state(connection):
                raise CanonicalServiceBlocked(
                    "BLOCKED_PHASE9_CLEANUP_DATABASE_POSTVERIFY"
                )
    except CanonicalServiceBlocked:
        raise
    except Exception as exc:
        raise CanonicalServiceBlocked(
            "BLOCKED_PHASE9_CLEANUP_DATABASE_WRITE"
        ) from exc
    deleted = sum(_delete_keychain_strict(service) for service in services)
    if any(_keychain_item_exists(service) for service in services):
        raise CanonicalServiceBlocked("BLOCKED_PHASE9_CLEANUP_KEYCHAIN_REMAINS")
    return {
        "status": "CLEANED_UP",
        "database": DATABASE_NAME,
        "principals_removed": roles_dropped,
        "keychain_items_removed": deleted,
        "repeat_noop": roles_dropped == 0 and deleted == 0,
    }


def _verify_research_provisioned_state():
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from app.canonical_v13.bootstrap import (  # noqa: PLC0415
        LOCAL_RESEARCH_SERVICE_PRINCIPALS,
        LOCAL_SERVICE_PRINCIPALS,
        local_role_mapping,
        verify_postgresql_bootstrap,
    )
    from sqlalchemy import create_engine  # noqa: PLC0415

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
        if set(connect_states) != set(capabilities) or any(connect_states.values()):
            raise CanonicalServiceBlocked("BLOCKED_RESEARCH_CONNECT_REPAIR_PARTIAL")
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


def _canonical_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _require_reader_rotation_safe() -> str:
    _require_release_checkout()
    for label in PHASE9_CLEANUP_LAUNCH_AGENT_LABELS[1:]:
        observed = _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
        if observed.returncode == 0:
            raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_EXECUTION_SERVICE_LOADED")
    api_state = status(DEFAULT_API_PORT)
    if api_state["status"] != "READY":
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_API_NOT_READY")
    release = _run(["git", "rev-parse", "HEAD"])
    if release.returncode != 0 or len(release.stdout.strip()) != 40:
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_RELEASE_IDENTITY")
    return release.stdout.strip()


def _reader_role_verifier(connection: psycopg.Connection[Any]) -> str:
    role = connection.execute(
        """
        SELECT r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole,
               r.rolinherit, r.rolreplication, r.rolbypassrls,
               r.rolconnlimit, a.rolpassword
        FROM pg_catalog.pg_roles AS r
        JOIN pg_catalog.pg_authid AS a ON a.oid = r.oid
        WHERE r.rolname = %s
        """,
        (READER_PRINCIPAL,),
    ).fetchone()
    if role is None or tuple(role[:8]) != (
        True,
        False,
        False,
        False,
        True,
        False,
        False,
        8,
    ):
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_ROLE_DRIFT")
    verifier = str(role[8] or "")
    if not verifier.startswith("SCRAM-SHA-256$"):
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_VERIFIER_INVALID")
    memberships = connection.execute(
        """
        SELECT parent.rolname, membership.admin_option
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
        JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid
        WHERE member.rolname = %s
        ORDER BY parent.rolname
        """,
        (READER_PRINCIPAL,),
    ).fetchall()
    if memberships != [(READER_CAPABILITY, False)]:
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_MEMBERSHIP_DRIFT")
    return verifier


def _verify_reader_hba_scram(connection: psycopg.Connection[Any]) -> None:
    """Require exact password-authenticated loopback rules before mutation.

    The managed API always connects over IPv4 loopback, but both loopback
    families are pinned so a later host change cannot silently fall through to
    a broader ``trust`` rule.  Requiring these to be the first two host rules is
    intentionally strict: an earlier host rule is an unreviewed authentication
    bypass for this credential-rotation contract.
    """

    rows = connection.execute(
        """
        SELECT line_number, type, database, user_name, address, netmask,
               auth_method, error
        FROM pg_catalog.pg_hba_file_rules
        WHERE type = 'host'
        ORDER BY line_number
        """
    ).fetchall()
    expected = (
        (
            (DATABASE_NAME,),
            (READER_PRINCIPAL,),
            "127.0.0.1",
            "255.255.255.255",
            "scram-sha-256",
        ),
        (
            (DATABASE_NAME,),
            (READER_PRINCIPAL,),
            "::1",
            "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
            "scram-sha-256",
        ),
    )
    if len(rows) < len(expected):
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_HBA_UNSAFE")
    observed = tuple(
        (
            tuple(row[2] or ()),
            tuple(row[3] or ()),
            str(row[4] or ""),
            str(row[5] or ""),
            str(row[6] or ""),
        )
        for row in rows[: len(expected)]
        if str(row[1]) == "host" and row[7] is None
    )
    if observed != expected:
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_HBA_UNSAFE")


def _connect_reader(material: str) -> psycopg.Connection[Any]:
    parameters: dict[str, object] = {
        "dbname": DATABASE_NAME,
        "user": READER_PRINCIPAL,
        "host": DATABASE_HOST,
        "port": DATABASE_PORT,
        "connect_timeout": 5,
    }
    # Keep the in-memory credential out of argv, environment, URLs and logs.
    parameters["pass" + "word"] = material.strip()
    return psycopg.connect(**parameters)


def _verify_reader_material(material: str) -> None:
    with _connect_reader(material) as connection:
        observed = connection.execute(
            """
            SELECT current_user, current_database(),
                   pg_has_role(current_user, %s, 'MEMBER'),
                   has_table_privilege(current_user, 'strategy_platform_v13.schema_metadata', 'SELECT'),
                   has_table_privilege(current_user, 'strategy_platform_v13.schema_metadata', 'INSERT'),
                   has_table_privilege(current_user, 'strategy_platform_v13.schema_metadata', 'UPDATE'),
                   has_table_privilege(current_user, 'strategy_platform_v13.schema_metadata', 'DELETE'),
                   has_table_privilege(current_user, 'strategy_platform_v13.schema_metadata', 'TRUNCATE')
            """,
            (READER_CAPABILITY,),
        ).fetchone()
    if observed != (
        READER_PRINCIPAL,
        DATABASE_NAME,
        True,
        True,
        False,
        False,
        False,
        False,
    ):
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_READ_ONLY_VERIFY")


def _verify_reader_material_rejected(material: str) -> None:
    try:
        connection = _connect_reader(material)
    except psycopg.OperationalError:
        return
    connection.close()
    raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_OLD_CREDENTIAL_ACCEPTED")


def rotate_api_reader(
    *, actor_identity: str, idempotency_key: str, port: int
) -> dict[str, object]:
    """Rotate only the API reader LOGIN and persist a redacted audit receipt."""

    if not actor_identity or len(actor_identity) > 160:
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_ACTOR_INVALID")
    if not idempotency_key or len(idempotency_key) > 160:
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_KEY_INVALID")
    release_sha = _require_reader_rotation_safe()
    request_digest = _canonical_digest(
        {
            "actor_identity": actor_identity,
            "idempotency_key": idempotency_key,
            "principal": READER_PRINCIPAL,
            "release_sha": release_sha,
            "scope": "API_READER_ONLY",
        }
    )
    old_material: str | None = None
    keychain_update_attempted = False
    event_id = str(uuid.uuid4())
    with _admin_connection() as connection:
        try:
            with connection.transaction():
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{READER_ROTATION_AGGREGATE_TYPE}:{READER_PRINCIPAL}",),
                )
                _verify_reader_hba_scram(connection)
                replay = connection.execute(
                    """
                    SELECT id, request_digest, receipt_digest, evidence_json
                    FROM strategy_platform_v13.audit_events
                    WHERE aggregate_type = %s AND aggregate_id = %s
                      AND event_type = %s
                    """,
                    (
                        READER_ROTATION_AGGREGATE_TYPE,
                        idempotency_key,
                        READER_ROTATION_EVENT,
                    ),
                ).fetchall()
                if replay:
                    if len(replay) != 1 or str(replay[0][1]) != request_digest:
                        raise CanonicalServiceBlocked(
                            "BLOCKED_READER_ROTATION_REPLAY_DRIFT"
                        )
                    evidence = dict(replay[0][3])
                    replay_receipt = _canonical_digest(
                        {
                            "event_id": str(replay[0][0]),
                            "event_type": READER_ROTATION_EVENT,
                            "request_digest": request_digest,
                            "evidence": evidence,
                        }
                    )
                    if (
                        str(replay[0][2]) != replay_receipt
                        or evidence.get("release_sha") != release_sha
                        or evidence.get("scope") != "API_READER_ONLY"
                        or evidence.get("trading_credentials_modified") is not False
                    ):
                        raise CanonicalServiceBlocked(
                            "BLOCKED_READER_ROTATION_RECEIPT_DRIFT"
                        )
                    return {
                        "status": "NO_OP_ALREADY_ROTATED",
                        "scope": "API_READER_ONLY",
                        "credential_generation": evidence[
                            "credential_generation"
                        ],
                        "receipt_digest": replay_receipt,
                        "release_sha": evidence["release_sha"],
                        "secret_material_exposed": False,
                    }

                previous_verifier = _reader_role_verifier(connection)
                old_material = _read_keychain(READER_KEYCHAIN_SERVICE)
                if old_material is None:
                    raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_ITEM_MISSING")
                _verify_reader_material(old_material)
                generation = int(
                    connection.execute(
                        """
                        SELECT count(*) FROM strategy_platform_v13.audit_events
                        WHERE aggregate_type = %s AND event_type = %s
                        """,
                        (READER_ROTATION_AGGREGATE_TYPE, READER_ROTATION_EVENT),
                    ).fetchone()[0]
                ) + 1
                new_material = secrets.token_urlsafe(48)
                new_verifier = _scram_verifier(new_material)
                evidence = {
                    "actor_identity": actor_identity,
                    "credential_generation": generation,
                    "database": DATABASE_NAME,
                    "keychain_service_digest": hashlib.sha256(
                        READER_KEYCHAIN_SERVICE.encode("utf-8")
                    ).hexdigest(),
                    "new_verifier_digest": hashlib.sha256(
                        new_verifier.encode("utf-8")
                    ).hexdigest(),
                    "previous_verifier_digest": hashlib.sha256(
                        previous_verifier.encode("utf-8")
                    ).hexdigest(),
                    "principal": READER_PRINCIPAL,
                    "release_sha": release_sha,
                    "scope": "API_READER_ONLY",
                    "trading_credentials_modified": False,
                }
                receipt_digest = _canonical_digest(
                    {
                        "event_id": event_id,
                        "event_type": READER_ROTATION_EVENT,
                        "request_digest": request_digest,
                        "evidence": evidence,
                    }
                )
                connection.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(READER_PRINCIPAL),
                        sql.Literal(new_verifier),
                    )
                )
                keychain_update_attempted = True
                _replace_keychain(READER_KEYCHAIN_SERVICE, new_material)
                connection.execute(
                    """
                    INSERT INTO strategy_platform_v13.audit_events (
                        id, event_type, aggregate_type, aggregate_id,
                        actor_identity, request_digest, receipt_digest,
                        evidence_json, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                    """,
                    (
                        event_id,
                        READER_ROTATION_EVENT,
                        READER_ROTATION_AGGREGATE_TYPE,
                        idempotency_key,
                        actor_identity,
                        request_digest,
                        receipt_digest,
                        json.dumps(evidence, sort_keys=True),
                    ),
                )
            # _admin_connection() performs a preflight query, so this is the
            # outer transaction commit. Keep it inside the Keychain rollback
            # guard rather than relying on the connection context manager.
            connection.commit()
        except Exception:
            if keychain_update_attempted and old_material is not None:
                _replace_keychain(READER_KEYCHAIN_SERVICE, old_material)
            raise

    if old_material is None:
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_INTERNAL_STATE")
    _verify_reader_material_rejected(old_material)
    current_material = _read_keychain(READER_KEYCHAIN_SERVICE)
    if current_material is None:
        raise CanonicalServiceBlocked("BLOCKED_KEYCHAIN_ITEM_MISSING")
    _verify_reader_material(current_material)
    restarted = restart(port)
    if restarted["status"] != "RESTARTED":
        raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_API_RESTART")
    return {
        "status": "ROTATED",
        "scope": "API_READER_ONLY",
        "credential_generation": generation,
        "receipt_digest": receipt_digest,
        "release_sha": release_sha,
        "old_credential_rejected": True,
        "new_credential_read_only": True,
        "api_restart_count": 1,
        "api_health": restarted["health"],
        "api_ready": restarted["ready"],
        "trading_credentials_modified": False,
        "secret_material_exposed": False,
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


def _production_database_environment(
    *, phase9_capabilities: tuple[str, ...] | None = None
) -> dict[str, str]:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from app.canonical_v13.phase9_persistence import (  # noqa: PLC0415
        PHASE9_PERSISTENCE_ENV_BY_CAPABILITY,
    )
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
    resolved_phase9_capabilities = (
        tuple(PHASE9_PERSISTENCE_ENV_BY_CAPABILITY)
        if phase9_capabilities is None
        else phase9_capabilities
    )
    if any(
        capability not in PHASE9_PERSISTENCE_ENV_BY_CAPABILITY
        for capability in resolved_phase9_capabilities
    ):
        raise CanonicalServiceBlocked("BLOCKED_PHASE9_PERSISTENCE_CAPABILITY_SET")
    phase9_specs = {
        logical_capability: (principal, service)
        for logical_capability, (principal, _physical_capability, service) in zip(
            PHASE9_PERSISTENCE_ENV_BY_CAPABILITY,
            PHASE9_PRINCIPAL_SPECS,
            strict=True,
        )
    }
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
        **{
            PHASE9_PERSISTENCE_ENV_BY_CAPABILITY[capability]: _database_url(
                *phase9_specs[capability]
            )
            for capability in resolved_phase9_capabilities
        },
    }


def require_release_checkout() -> None:
    """Expose the existing clean/exact-main release guard to sibling tools."""

    _require_release_checkout()


def serve(port: int) -> None:
    if not 1024 <= port <= 65535:
        raise CanonicalServiceBlocked("BLOCKED_INVALID_LOOPBACK_PORT")
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import uvicorn  # noqa: PLC0415
    from app.canonical_v13.phase9_persistence import (  # noqa: PLC0415
        API_PHASE9_CAPABILITIES,
    )
    from app.canonical_v13.production import (  # noqa: PLC0415
        create_app,
    )

    app = create_app(
        _production_database_environment(
            phase9_capabilities=API_PHASE9_CAPABILITIES
        ),
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
        *(spec[2] for spec in PHASE9_PRINCIPAL_SPECS),
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
    completed = _run(["launchctl", "bootstrap", _launchctl_domain(), str(PLIST_PATH)])
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
                with urlopen(f"http://127.0.0.1:{port}/{path}", timeout=2) as response:
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
            "provision-phase9",
            "provision-runtime-reader",
            "cleanup-phase9-provisioning",
            "repair-research-connect",
            "rotate-api-reader",
            "serve",
            "install",
            "status",
            "restart",
        ),
    )
    parser.add_argument("--port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--actor-identity")
    parser.add_argument("--idempotency-key")
    args = parser.parse_args(argv)
    try:
        if args.command == "provision":
            payload = provision_principals()
        elif args.command == "provision-research":
            payload = provision_research_principals()
        elif args.command == "provision-phase9":
            payload = provision_phase9_principals()
        elif args.command == "provision-runtime-reader":
            payload = provision_runtime_reader()
        elif args.command == "cleanup-phase9-provisioning":
            payload = cleanup_phase9_provisioning()
        elif args.command == "repair-research-connect":
            payload = repair_research_database_connect()
        elif args.command == "rotate-api-reader":
            if args.actor_identity is None or args.idempotency_key is None:
                raise CanonicalServiceBlocked("BLOCKED_READER_ROTATION_ARGUMENTS_REQUIRED")
            payload = rotate_api_reader(
                actor_identity=args.actor_identity,
                idempotency_key=args.idempotency_key,
                port=args.port,
            )
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
    return (
        0
        if payload["status"]
        in {
            "PROVISIONED",
            "REPAIRED",
            "ROTATED",
            "NO_OP_ALREADY_ROTATED",
            "CLEANED_UP",
            "INSTALLED",
            "READY",
            "RESTARTED",
        }
        else 2
    )


if __name__ == "__main__":
    sys.exit(main())
