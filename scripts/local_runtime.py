#!/usr/bin/env python3
"""Safe, repeatable single-environment runtime manager for Freqtrade AI.

This command manages the FastAPI, DB-backed worker, and Vite development
processes.  The worker may execute explicitly authorized queued research jobs,
but this runtime manager never connects to an exchange, starts dry-run/live
trading, or reads provider credentials.  Runtime state stays in
``.freqtrade-ai/`` and the only application database is local PostgreSQL
``freqtrade_ai``.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.adapters.freqtrade.binary import resolve_freqtrade_binary
from app.adapters.okx_demo.credential_preflight import (
    ALLOW_REAL_FUNDS_ENV,
    EXECUTION_TARGET_ENV,
    OKX_DEMO_CREDENTIAL_ENV_NAMES,
    OKX_DEMO_REQUIRED_ENV_NAMES,
    OKX_DEMO_REST_URL,
    REST_URL_ENV,
)
from app.core.config import load_app_yaml
from app.core.execution_target import (
    ExecutionTargetConfigurationError,
    parse_execution_target_manifest,
)

DEFAULT_RUNTIME_DIR = REPO_ROOT / ".freqtrade-ai" / "runtime"
DEFAULT_RUNTIME_ENV_FILE = REPO_ROOT / ".freqtrade-ai" / "runtime.env"
RUNTIME_ENV_KEYS = frozenset({"DATABASE_URL", "FREQTRADE_BINARY"})
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_KEYCHAIN_SERVICE = "freqtrade-ai/deepseek-api-key"
MANAGED_STRATEGY_PROVIDER = "deepseek"
MANAGED_STRATEGY_MODEL = "deepseek-v4-pro"
DISABLE_ENV_FILE_ENV = "FREQTRADE_AI_DISABLE_ENV_FILE"
OPERATOR_TOKEN_ENV = "FREQTRADE_AI_OPERATOR_TOKEN"
OKX_DEMO_KEYCHAIN_SERVICES = dict(
    zip(
        OKX_DEMO_REQUIRED_ENV_NAMES,
        (
            "freqtrade-ai/okx-demo-api-key",
            "freqtrade-ai/okx-demo-api-secret",
            "freqtrade-ai/okx-demo-api-passphrase",
            "freqtrade-ai/okx-demo-account-fingerprint",
        ),
    )
)
KEYCHAIN_TIMEOUT_SECONDS = 5
SAFE_INHERITED_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "PYTHONUNBUFFERED",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "FREQTRADE_BINARY",
        "FREQTRADE_AI_CANONICAL_REPO_ROOT",
    }
)
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai"
)
BACKEND_PORT = 8000
FRONTEND_PORT = 5173
PID_FILES = {
    "backend": "backend.pid",
    "worker": "worker.pid",
    "frontend": "frontend.pid",
}
LOG_FILES = {
    "backend": "backend.log",
    "worker": "worker.log",
    "frontend": "frontend.log",
}
SERVICE_PROCESS_MARKERS = {
    "backend": "uvicorn",
    "worker": "app.workers.deepseek_backtest_worker",
    "frontend": "vite",
}
SERVICE_WORKING_DIRECTORIES = {
    "backend": REPO_ROOT / "backend",
    "worker": REPO_ROOT / "backend",
    "frontend": REPO_ROOT / "frontend",
}
SECRET_LINE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passphrase)\s*([=:])\s*([^\s,;]+)"
)


class RuntimeBlocked(Exception):
    """A local prerequisite is absent or unsafe; nothing was started."""


def load_runtime_environment(path: Optional[Path] = None) -> None:
    """Load the two non-secret runtime selectors from one repo-local file."""

    config_path = path or DEFAULT_RUNTIME_ENV_FILE
    if not config_path.exists():
        return
    seen = set()
    for line_number, raw_line in enumerate(
        config_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeBlocked(
                "invalid runtime.env line {}: expected KEY=VALUE".format(line_number)
            )
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in RUNTIME_ENV_KEYS:
            raise RuntimeBlocked(
                "runtime.env key is not allowed: {}".format(key or "<empty>")
            )
        if key in seen:
            raise RuntimeBlocked("runtime.env key is duplicated: {}".format(key))
        if not value:
            raise RuntimeBlocked("runtime.env value is empty: {}".format(key))
        seen.add(key)
        os.environ.setdefault(key, value)


def runtime_dir(raw_path: Optional[str]) -> Path:
    candidate = Path(raw_path).expanduser() if raw_path else DEFAULT_RUNTIME_DIR
    resolved = candidate.resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise RuntimeBlocked("runtime directory must stay inside this repository") from exc
    return resolved


def redact_database_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid database url>"
    if not parsed.scheme:
        return "<invalid database url>"
    if parsed.scheme.startswith("sqlite"):
        return "{}://{}".format(parsed.scheme, parsed.path)
    if "@" in parsed.netloc:
        credentials, host = parsed.netloc.rsplit("@", 1)
        username = credentials.split(":", 1)[0]
        netloc = "{}:***@{}".format(username, host)
    else:
        netloc = parsed.netloc
    return "{}://{}{}".format(parsed.scheme, netloc, parsed.path)


def runtime_database_url() -> str:
    """Return the one supported localhost PostgreSQL application database."""

    value = os.environ.get("DATABASE_URL", "").strip() or DEFAULT_DATABASE_URL
    parsed = urlsplit(value)
    if not parsed.scheme.startswith("postgresql") or not parsed.hostname:
        raise RuntimeBlocked("DATABASE_URL must be a PostgreSQL SQLAlchemy URL")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeBlocked("runtime only accepts a localhost PostgreSQL target")
    if parsed.path != "/freqtrade_ai":
        raise RuntimeBlocked("runtime only accepts the canonical freqtrade_ai database")
    return value


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def process_status(state_dir: Path, service: str) -> Dict[str, Any]:
    pid_path = state_dir / PID_FILES[service]
    pid = read_pid(pid_path)
    running = pid is not None and process_running(pid)
    return {"service": service, "pid": pid, "running": running, "pid_file": str(pid_path)}


def is_managed_process(pid: int, service: str) -> bool:
    """Refuse to signal a reused/stale PID that is not our local process."""

    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return False
    command = completed.stdout.strip()
    expected = SERVICE_PROCESS_MARKERS[service]
    if expected not in command:
        return False
    try:
        cwd_result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return False
    expected_cwd = SERVICE_WORKING_DIRECTORIES[service]
    return "n{}".format(expected_cwd) in cwd_result.stdout


def base_service_environment() -> Dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in SAFE_INHERITED_ENV_KEYS
    }
    environment.update(
        {
            "APP_ENV": "local",
            DISABLE_ENV_FILE_ENV: "1",
            "VITE_ENABLE_DEV_FIXTURES": "false",
            "VITE_FREQUI_URL": "",
        }
    )
    return environment


def okx_adapter_base_environment() -> Dict[str, str]:
    """Build the smallest child environment without proxy or CA overrides."""

    inherited_keys = {
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
    }
    environment = {
        key: value for key, value in os.environ.items() if key in inherited_keys
    }
    environment.update(
        {
            "APP_ENV": "local",
            DISABLE_ENV_FILE_ENV: "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def clean_environment(database_url: str) -> Dict[str, str]:
    """Build a non-provider environment for database-only preflight commands."""

    environment = base_service_environment()
    environment["DATABASE_URL"] = database_url
    return environment


def read_deepseek_api_key() -> Tuple[Optional[str], Dict[str, Any]]:
    """Read the durable macOS credential without changing the parent environment."""

    if sys.platform != "darwin":
        value = os.environ.get(DEEPSEEK_API_KEY_ENV, "").strip()
        return (
            value or None,
            {
                "status": "READY" if value else "UNAVAILABLE",
                "configured": bool(value),
                "source": "environment" if value else None,
            },
        )

    security = Path("/usr/bin/security")
    if not security.is_file():
        return None, {
            "status": "UNAVAILABLE",
            "configured": False,
            "source": "keychain",
            "reason": "macOS security command is unavailable",
        }
    account = pwd.getpwuid(os.getuid()).pw_name
    try:
        completed = subprocess.run(
            [
                str(security),
                "find-generic-password",
                "-a",
                account,
                "-s",
                DEEPSEEK_KEYCHAIN_SERVICE,
                "-w",
            ],
            cwd=str(REPO_ROOT),
            check=False,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, {
            "status": "UNAVAILABLE",
            "configured": False,
            "source": "keychain",
            "reason": "Keychain item is missing or inaccessible",
        }

    value = completed.stdout.rstrip("\r\n")
    if (
        completed.returncode != 0
        or not value
        or len(value) > 16384
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        return None, {
            "status": "UNAVAILABLE",
            "configured": False,
            "source": "keychain",
            "reason": "Keychain item is missing or inaccessible",
        }
    return value, {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }


def validate_okx_demo_execution_target() -> None:
    raw_config = load_app_yaml(REPO_ROOT / "config" / "app.yaml")
    try:
        manifest = parse_execution_target_manifest(raw_config.get("execution"))
    except ExecutionTargetConfigurationError as exc:
        raise RuntimeBlocked("OKX Demo execution target is BLOCKED") from exc
    target = manifest.active_target
    if (
        target.target_id != "OKX_DEMO"
        or target.credential_source != "macos_keychain"
        or target.simulated_trading is not True
        or target.allow_real_funds is not False
        or target.order_submission_enabled is not False
    ):
        raise RuntimeBlocked("OKX Demo execution target is BLOCKED")


def _read_macos_keychain_item(service: str) -> Optional[str]:
    if sys.platform != "darwin":
        return None
    security = Path("/usr/bin/security")
    if not security.is_file():
        return None
    account = pwd.getpwuid(os.getuid()).pw_name
    try:
        completed = subprocess.run(
            [
                str(security),
                "find-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
            ],
            cwd=str(REPO_ROOT),
            check=False,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.rstrip("\r\n")
    if (
        completed.returncode != 0
        or not value
        or len(value) > 16384
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        return None
    return value


def read_okx_demo_credentials() -> Tuple[Optional[Dict[str, str]], Dict[str, Any]]:
    """Read the complete Demo bundle without accepting shell or dotenv fallback."""

    validate_okx_demo_execution_target()
    if sys.platform != "darwin":
        return None, {
            "status": "BLOCKED",
            "configured": False,
            "source": "keychain",
            "reason": "macOS Keychain is required for OKX Demo credentials",
        }

    credentials: Dict[str, str] = {}
    for environment_name in OKX_DEMO_REQUIRED_ENV_NAMES:
        service = OKX_DEMO_KEYCHAIN_SERVICES[environment_name]
        value = _read_macos_keychain_item(service)
        if value is None:
            credentials.clear()
            return None, {
                "status": "BLOCKED",
                "configured": False,
                "source": "keychain",
                "reason": "OKX Demo Keychain credential bundle is incomplete or inaccessible",
            }
        credentials[environment_name] = value
    return credentials, {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }


def read_okx_demo_onboarding_credentials() -> Tuple[Optional[Dict[str, str]], Dict[str, Any]]:
    """Read only the three signing credentials used to establish the first pin."""

    validate_okx_demo_execution_target()
    if sys.platform != "darwin":
        return None, {
            "status": "BLOCKED",
            "configured": False,
            "source": "keychain",
            "reason": "macOS Keychain is required for OKX Demo credentials",
        }
    credentials: Dict[str, str] = {}
    for environment_name in OKX_DEMO_CREDENTIAL_ENV_NAMES:
        service = OKX_DEMO_KEYCHAIN_SERVICES[environment_name]
        value = _read_macos_keychain_item(service)
        if value is None:
            credentials.clear()
            return None, {
                "status": "BLOCKED",
                "configured": False,
                "source": "keychain",
                "reason": "OKX Demo signing credential bundle is incomplete or inaccessible",
            }
        credentials[environment_name] = value
    return credentials, {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }


def service_environment(
    service: str,
    database_url: str,
    deepseek_api_key: Optional[str],
    okx_demo_credentials: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Give each managed service only the credentials it needs."""

    if service not in {
        "backend",
        "worker",
        "frontend",
        "okx_adapter",
        "okx_onboarding",
    }:
        raise RuntimeBlocked("unknown managed service environment")
    environment = (
        okx_adapter_base_environment()
        if service in {"okx_adapter", "okx_onboarding"}
        else base_service_environment()
    )
    if service in {"backend", "worker"}:
        environment.update(
            {
                "DATABASE_URL": database_url,
                "STRATEGY_BLUEPRINT_PROVIDER": MANAGED_STRATEGY_PROVIDER,
                "STRATEGY_BLUEPRINT_MODEL": MANAGED_STRATEGY_MODEL,
            }
        )
    if service == "backend":
        operator_token = os.environ.get(OPERATOR_TOKEN_ENV, "")
        if operator_token:
            environment[OPERATOR_TOKEN_ENV] = operator_token
    if service in {"backend", "worker"} and deepseek_api_key:
        environment[DEEPSEEK_API_KEY_ENV] = deepseek_api_key
    if service in {"okx_adapter", "okx_onboarding"}:
        validate_okx_demo_execution_target()
        required_names = (
            OKX_DEMO_REQUIRED_ENV_NAMES
            if service == "okx_adapter"
            else OKX_DEMO_CREDENTIAL_ENV_NAMES
        )
        if not okx_demo_credentials or set(okx_demo_credentials) != set(required_names):
            raise RuntimeBlocked("OKX Demo credential bundle is incomplete")
        environment.update(okx_demo_credentials)
        environment.update(
            {
                EXECUTION_TARGET_ENV: "OKX_DEMO",
                ALLOW_REAL_FUNDS_ENV: "false",
                REST_URL_ENV: OKX_DEMO_REST_URL,
            }
        )
    return environment


def backend_python() -> Path:
    executable = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
    if not executable.is_file():
        raise RuntimeBlocked("backend virtualenv is missing; run `make bootstrap`")
    return executable


def frontend_vite() -> Path:
    executable = REPO_ROOT / "frontend" / "node_modules" / ".bin" / "vite"
    if not executable.is_file():
        raise RuntimeBlocked("frontend dependencies are missing; run `make bootstrap`")
    return executable


def run_checked(command: Sequence[str], *, cwd: Path, environment: Optional[Dict[str, str]] = None) -> None:
    completed = subprocess.run(command, cwd=str(cwd), env=environment, check=False)
    if completed.returncode:
        raise RuntimeBlocked("command failed (exit {}): {}".format(completed.returncode, command[0]))


def doctor(state_dir: Path) -> Dict[str, Any]:
    freqtrade_resolution = resolve_freqtrade_binary()
    database_url = runtime_database_url()
    checks = {
        "python3": command_exists("python3"),
        "node": command_exists("node"),
        "npm": command_exists("npm"),
        "backend_virtualenv": (REPO_ROOT / "backend" / ".venv" / "bin" / "python").is_file(),
        "frontend_dependencies": (REPO_ROOT / "frontend" / "node_modules" / ".bin" / "vite").is_file(),
        "backend_port_available": port_available(BACKEND_PORT),
        "frontend_port_available": port_available(FRONTEND_PORT),
        "freqtrade_binary": freqtrade_resolution.ready,
        "market_data_directory": (REPO_ROOT / "user_data" / "data").is_dir(),
        "live_trading": False,
        "dry_run_trading": False,
    }
    result: Dict[str, Any] = {
        "environment": "local",
        "runtime_dir": str(state_dir),
        "checks": checks,
    }
    result["freqtrade"] = {
        "source": freqtrade_resolution.source,
        "resolved_path": (
            str(freqtrade_resolution.resolved_path)
            if freqtrade_resolution.resolved_path is not None
            else None
        ),
        "status": "READY" if freqtrade_resolution.ready else "BLOCKED",
        "reason": freqtrade_resolution.blocked_reason,
    }
    result["database"] = {"kind": "postgresql", "identity": redact_database_url(database_url)}
    try:
        ensure_schema(database_url)
        result["schema"] = {"status": "READY"}
    except RuntimeBlocked as exc:
        result["schema"] = {"status": "BLOCKED", "reason": str(exc)}
    return result


def bootstrap() -> Dict[str, Any]:
    run_checked([sys.executable, "-m", "venv", ".venv"], cwd=REPO_ROOT / "backend")
    run_checked(
        [str(backend_python()), "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=REPO_ROOT / "backend",
    )
    run_checked(["npm", "ci"], cwd=REPO_ROOT / "frontend")
    return {"status": "READY", "backend_virtualenv": True, "frontend_dependencies": True}


def ensure_schema(database_url: str) -> None:
    environment = clean_environment(database_url)
    completed = subprocess.run(
        [str(backend_python()), "-m", "app.db.migrate", "verify", "--database-url", database_url],
        cwd=str(REPO_ROOT / "backend"),
        env=environment,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode:
        raise RuntimeBlocked(
            "PostgreSQL schema verification failed; run `make db-init` on the canonical database"
        )


def ensure_worker_queue_idle(database_url: str) -> None:
    code = (
        "from sqlalchemy import create_engine, text; "
        "engine=create_engine(__import__('os').environ['DATABASE_URL']); "
        "connection=engine.connect(); "
        "count=connection.execute(text("
        "\"SELECT count(*) FROM research_jobs WHERE status IN ('pending','running')\""
        ")).scalar_one(); "
        "connection.close(); "
        "raise SystemExit(0 if count == 0 else 3)"
    )
    completed = subprocess.run(
        [str(backend_python()), "-c", code],
        cwd=str(REPO_ROOT / "backend"),
        env=clean_environment(database_url),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode:
        raise RuntimeBlocked(
            "research worker queue is not idle; resolve pending/running jobs before `make up`"
        )


def run_okx_demo_preflight() -> Dict[str, Any]:
    """Run authenticated read-only attestation in the sole credential-bearing child."""

    credentials, credential_status = read_okx_demo_credentials()
    if credentials is None:
        return {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "credentials": credential_status,
            "reason": credential_status["reason"],
        }

    try:
        completed = subprocess.run(
            [
                str(backend_python()),
                "-m",
                "app.adapters.okx_demo.credential_preflight",
            ],
            cwd=str(REPO_ROOT / "backend"),
            env=service_environment(
                "okx_adapter",
                runtime_database_url(),
                None,
                credentials,
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "credentials": credential_status,
            "reason": "OKX Demo account attestation process failed",
        }
    finally:
        credentials.clear()

    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if (
        completed.returncode != 0
        or payload.get("status") != "READY"
        or payload.get("execution_target") != "OKX_DEMO"
        or payload.get("remote_account_evidence")
        != {
            "authenticated_demo_response": True,
            "identity_present": True,
            "fingerprint_match": True,
            "permissions": {"read": True, "trade": True, "withdraw": False},
            "account_level": "2",
            "position_mode": "net_mode",
        }
        or payload.get("local_target_contract")
        != {
            "product_type": "SWAP",
            "margin_mode": "isolated",
            "allow_real_funds": False,
        }
        or payload.get("request_contract")
        != {
            "method": "GET",
            "path": "/api/v5/account/config",
            "simulated_trading_header": True,
        }
    ):
        return {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "credentials": credential_status,
            "reason": "OKX Demo account identity or permissions could not be attested",
        }
    return {
        "status": "READY",
        "execution_target": "OKX_DEMO",
        "remote_account_evidence": {
            "authenticated_demo_response": True,
            "identity_present": True,
            "fingerprint_match": True,
            "permissions": {"read": True, "trade": True, "withdraw": False},
            "account_level": "2",
            "position_mode": "net_mode",
        },
        "local_target_contract": {
            "product_type": "SWAP",
            "margin_mode": "isolated",
            "allow_real_funds": False,
        },
        "request_contract": {
            "method": "GET",
            "path": "/api/v5/account/config",
            "simulated_trading_header": True,
        },
        "credentials": credential_status,
    }


def run_okx_demo_account_pin() -> Dict[str, Any]:
    """Establish the first account pin in a one-shot credential-bearing child."""

    credentials, credential_status = read_okx_demo_onboarding_credentials()
    if credentials is None:
        return {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "credentials": credential_status,
            "reason": credential_status["reason"],
        }
    try:
        completed = subprocess.run(
            [
                str(backend_python()),
                "-m",
                "app.adapters.okx_demo.credential_preflight",
                "--pin-account",
            ],
            cwd=str(REPO_ROOT / "backend"),
            env=service_environment(
                "okx_onboarding",
                runtime_database_url(),
                None,
                credentials,
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "credentials": credential_status,
            "reason": "OKX Demo account fingerprint onboarding process failed",
        }
    finally:
        credentials.clear()

    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if (
        completed.returncode != 0
        or payload
        != {
            "status": "READY",
            "execution_target": "OKX_DEMO",
            "account_fingerprint_pinned": True,
        }
    ):
        return {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "credentials": credential_status,
            "reason": "OKX Demo account fingerprint onboarding was refused or failed",
        }
    return {
        "status": "READY",
        "execution_target": "OKX_DEMO",
        "account_fingerprint_pinned": True,
        "credentials": credential_status,
    }


def start_service(
    service: str,
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Dict[str, str],
    state_dir: Path,
) -> None:
    current = process_status(state_dir, service)
    if current["running"]:
        raise RuntimeBlocked("{} is already managed by this runtime (pid {})".format(service, current["pid"]))
    log_path = state_dir / LOG_FILES[service]
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    (state_dir / PID_FILES[service]).write_text("{}\n".format(process.pid), encoding="utf-8")


def wait_for_url(url: str, description: str, timeout_seconds: int = 20) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if 200 <= response.status < 400:
                    return
        except (URLError, OSError):
            time.sleep(0.25)
    raise RuntimeBlocked("{} did not become reachable within {} seconds".format(description, timeout_seconds))


def wait_for_process(state_dir: Path, service: str, timeout_seconds: float = 2.0) -> None:
    """Fail startup when a managed process exits immediately after launch."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = process_status(state_dir, service)
        if not status["running"]:
            raise RuntimeBlocked("{} exited during startup; inspect {}".format(service, LOG_FILES[service]))
        time.sleep(0.1)


def stop_service(state_dir: Path, service: str) -> Dict[str, Any]:
    pid_path = state_dir / PID_FILES[service]
    pid = read_pid(pid_path)
    if pid is None:
        return {"service": service, "status": "not-managed"}
    if not process_running(pid):
        pid_path.unlink(missing_ok=True)
        return {"service": service, "status": "stale-pid-removed", "pid": pid}
    if not is_managed_process(pid, service):
        return {
            "service": service,
            "status": "BLOCKED",
            "pid": pid,
            "reason": "pid file does not point to the managed local process",
        }
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and process_running(pid):
        time.sleep(0.1)
    if process_running(pid):
        os.killpg(pid, signal.SIGKILL)
    pid_path.unlink(missing_ok=True)
    return {"service": service, "status": "stopped", "pid": pid}


def stop_all(state_dir: Path) -> Dict[str, Any]:
    return {
        "services": [
            stop_service(state_dir, service)
            for service in ("worker", "frontend", "backend")
        ]
    }


def start(state_dir: Path) -> Dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    backend_python()
    frontend_vite()
    if not port_available(BACKEND_PORT) or not port_available(FRONTEND_PORT):
        raise RuntimeBlocked("port 8000 or 5173 is already in use; run `make status` before starting")
    database_url = runtime_database_url()
    ensure_schema(database_url)
    ensure_worker_queue_idle(database_url)
    database = {"identity": redact_database_url(database_url), "schema": "verified"}
    deepseek_api_key, deepseek_credential = read_deepseek_api_key()
    try:
        start_service(
            "backend",
            [str(backend_python()), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
            cwd=REPO_ROOT / "backend",
            environment=service_environment("backend", database_url, deepseek_api_key),
            state_dir=state_dir,
        )
        wait_for_url("http://127.0.0.1:{}/readyz".format(BACKEND_PORT), "backend readiness")
        start_service(
            "worker",
            [
                str(backend_python()),
                "-m",
                "app.workers.deepseek_backtest_worker",
            ],
            cwd=REPO_ROOT / "backend",
            environment=service_environment("worker", database_url, deepseek_api_key),
            state_dir=state_dir,
        )
        wait_for_process(state_dir, "worker")
        start_service(
            "frontend",
            [str(frontend_vite()), "--host", "127.0.0.1", "--port", str(FRONTEND_PORT)],
            cwd=REPO_ROOT / "frontend",
            environment=service_environment("frontend", database_url, deepseek_api_key),
            state_dir=state_dir,
        )
        wait_for_url("http://127.0.0.1:{}/".format(FRONTEND_PORT), "frontend")
    except RuntimeBlocked:
        stop_all(state_dir)
        raise
    return {
        "status": "RUNNING",
        "environment": "local",
        "database": database,
        "credentials": {"deepseek_api_key": deepseek_credential},
        "backend_url": "http://127.0.0.1:{}".format(BACKEND_PORT),
        "frontend_url": "http://127.0.0.1:{}".format(FRONTEND_PORT),
        "trading": {"live": False, "dry_run": False, "real_orders": False},
    }


def current_status(state_dir: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "environment": "local",
        "runtime_dir": str(state_dir),
        "services": [
            process_status(state_dir, service)
            for service in ("backend", "worker", "frontend")
        ],
        "trading": {"live": False, "dry_run": False, "real_orders": False},
    }
    try:
        database_url = runtime_database_url()
        ensure_schema(database_url)
        result["database"] = {
            "kind": "postgresql",
            "identity": redact_database_url(database_url),
            "schema": "verified",
        }
    except RuntimeBlocked as exc:
        result["database"] = {"status": "BLOCKED", "reason": str(exc)}
    return result


def redact_line(line: str) -> str:
    return SECRET_LINE.sub(lambda match: "{}{}***".format(match.group(1), match.group(2)), line)


def recent_logs(state_dir: Path, lines: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for service, filename in LOG_FILES.items():
        path = state_dir / filename
        if not path.exists():
            result[service] = {"status": "missing", "path": str(path)}
            continue
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        result[service] = {"status": "available", "path": str(path), "lines": [redact_line(line) for line in tail]}
    return result


def emit(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("status={}".format(payload.get("status", "READY")))
    for key, value in payload.items():
        if key != "status":
            print("{}={}".format(key, value))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "doctor",
            "bootstrap",
            "up",
            "status",
            "down",
            "logs",
            "verify",
            "okx-preflight",
            "okx-pin-account",
        ),
    )
    parser.add_argument("--runtime-dir")
    parser.add_argument("--lines", type=int, default=80)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        load_runtime_environment()
        state_dir = runtime_dir(args.runtime_dir)
        if args.command == "doctor":
            payload = doctor(state_dir)
        elif args.command == "bootstrap":
            payload = bootstrap()
        elif args.command == "up":
            payload = start(state_dir)
        elif args.command == "status":
            payload = current_status(state_dir)
        elif args.command == "down":
            payload = stop_all(state_dir)
        elif args.command == "logs":
            payload = recent_logs(state_dir, max(1, args.lines))
        elif args.command == "okx-preflight":
            payload = run_okx_demo_preflight()
            if payload["status"] != "READY":
                raise RuntimeBlocked(str(payload["reason"]))
        elif args.command == "okx-pin-account":
            payload = run_okx_demo_account_pin()
            if payload["status"] != "READY":
                raise RuntimeBlocked(str(payload["reason"]))
        else:
            status = current_status(state_dir)
            running = all(service["running"] for service in status["services"])
            ensure_schema(runtime_database_url())
            if not running:
                raise RuntimeBlocked(
                    "backend, worker, and frontend must all be running before verification"
                )
            wait_for_url("http://127.0.0.1:{}/readyz".format(BACKEND_PORT), "backend readiness")
            wait_for_url("http://127.0.0.1:{}/".format(FRONTEND_PORT), "frontend")
            payload = {"status": "VERIFIED", **status}
        emit(payload, args.json)
        return 0
    except RuntimeBlocked as exc:
        emit({"status": "BLOCKED", "reason": str(exc)}, args.json)
        return 2


if __name__ == "__main__":
    sys.exit(main())
