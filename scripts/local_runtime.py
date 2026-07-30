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
from contextlib import contextmanager
import fcntl
import json
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_RUNTIME_DIR = REPO_ROOT / ".freqtrade-ai" / "runtime"
DEFAULT_RUNTIME_ENV_FILE = REPO_ROOT / ".freqtrade-ai" / "runtime.env"
RUNTIME_ENV_KEYS = frozenset({"DATABASE_URL", "FREQTRADE_BINARY"})
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_KEYCHAIN_SERVICE = "freqtrade-ai/deepseek-api-key"
MANAGED_STRATEGY_PROVIDER = "deepseek"
MANAGED_STRATEGY_MODEL = "deepseek-v4-pro"
DISABLE_ENV_FILE_ENV = "FREQTRADE_AI_DISABLE_ENV_FILE"
OPERATOR_TOKEN_ENV = "FREQTRADE_AI_OPERATOR_TOKEN"
OPERATOR_TOKEN_KEYCHAIN_SERVICE = "freqtrade-ai/operator-token"
OKX_DEMO_CREDENTIAL_ENV_NAMES = (
    "OKX_DEMO_API_KEY",
    "OKX_DEMO_API_SECRET",
    "OKX_DEMO_API_PASSPHRASE",
)
OKX_DEMO_ACCOUNT_FINGERPRINT_ENV = "OKX_DEMO_ACCOUNT_FINGERPRINT"
OKX_DEMO_REQUIRED_ENV_NAMES = (
    *OKX_DEMO_CREDENTIAL_ENV_NAMES,
    OKX_DEMO_ACCOUNT_FINGERPRINT_ENV,
)
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
OKX_DEMO_CREDENTIAL_GENERATION_SERVICE = (
    "freqtrade-ai/okx-demo-credential-generation"
)
EXECUTION_TARGET_ENV = "FREQTRADE_AI_EXECUTION_TARGET"
ALLOW_REAL_FUNDS_ENV = "FREQTRADE_AI_ALLOW_REAL_FUNDS"
REST_URL_ENV = "FREQTRADE_AI_OKX_DEMO_REST_URL"
OKX_DEMO_REST_URL = "https://openapi.okx.com"
IP_WHITELIST_REJECTED_REASON = (
    "OKX Demo API IP whitelist rejected the current egress IP"
)
SAFE_OPERATOR_PREFLIGHT_REASONS = frozenset(
    {
        IP_WHITELIST_REJECTED_REASON,
        "OKX Demo account attestation transport failed",
        "OKX Demo account identity is unknown",
        "OKX Demo account fingerprint does not match",
        "OKX Demo API permissions must be exactly read_only and trade",
        "OKX Demo position mode must be long_short_mode",
        "OKX Demo account level must be Futures mode",
    }
)
ALLOW_DEMO_ORDER_ENV = "FREQTRADE_AI_ALLOW_DEMO_ORDER"
OKX_DEMO_CANARY_DEFAULT_INSTRUMENT = "BTC-USDT-SWAP"
OKX_DEMO_CANARY_ALLOWED_INSTRUMENTS = frozenset(
    {OKX_DEMO_CANARY_DEFAULT_INSTRUMENT}
)
ATTESTATION_PROOF_KEY_ENV = "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY"
ATTESTATION_PROOF_KEYCHAIN_SERVICE = (
    "freqtrade-ai/okx-demo-attestation-proof-key"
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
    "okx_runtime": "okx-runtime.pid",
}
LOG_FILES = {
    "backend": "backend.log",
    "worker": "worker.log",
    "frontend": "frontend.log",
    "okx_runtime": "okx-runtime.log",
}
SERVICE_PROCESS_MARKERS = {
    "backend": "uvicorn",
    "worker": "app.workers.deepseek_backtest_worker",
    "frontend": "vite",
    "okx_runtime": "app.adapters.okx_demo.runtime_service",
}
SERVICE_WORKING_DIRECTORIES = {
    "backend": REPO_ROOT / "backend",
    "worker": REPO_ROOT / "backend",
    "frontend": REPO_ROOT / "frontend",
    "okx_runtime": REPO_ROOT / "backend",
}
SERVICE_START_ORDER = ("backend", "worker", "frontend", "okx_runtime")
SERVICE_STOP_ORDER = tuple(reversed(SERVICE_START_ORDER))
OKX_RUNTIME_READY_FILE = "okx-runtime.ready.json"
OKX_WRITER_LOCK_FILE = "okx-demo-order-writer.lock"
CONTROL_LOCK_FILE = "runtime-control.lock"
OPENINGS_FREEZE_FILE = "okx-runtime.freeze-openings"
SECRET_LINE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passphrase|authorization|"
    r"ok-access-(?:key|sign|passphrase))([\"']?\s*[:=]\s*[\"']?)([^\s,;\"']+)"
)
DATABASE_CREDENTIALS = re.compile(
    r"(?i)(postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^:\s/@]+:)([^@\s]+)(@)"
)


class RuntimeBlocked(Exception):
    """A local prerequisite is absent or unsafe; nothing was started."""

    def __init__(
        self,
        message: str,
        *,
        safe_stage: Optional[str] = None,
        elapsed_ms: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.safe_stage = safe_stage
        self.elapsed_ms = elapsed_ms


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
        return int(read_private_state(path).strip())
    except (OSError, ValueError):
        return None


def read_private_state(path: Path) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise OSError("unsafe private runtime state")
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        return handle.read()


def write_private_state(path: Path, value: str) -> None:
    temporary = path.with_name(
        ".{}.{}.tmp".format(path.name, os.getpid())
    )
    descriptor = os.open(
        temporary,
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def runtime_control_lock(state_dir: Path):
    """Serialize up/down/verify without creating another supervisor."""

    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state_dir / CONTROL_LOCK_FILE
    descriptor = os.open(
        path,
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise RuntimeBlocked("runtime control lock is not a safe local file")
    handle = os.fdopen(descriptor, "r+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


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


def read_operator_token() -> Tuple[Optional[str], Dict[str, Any]]:
    """Read the local action credential without accepting a macOS ENV fallback."""

    value = (
        _read_macos_keychain_item(OPERATOR_TOKEN_KEYCHAIN_SERVICE)
        if sys.platform == "darwin"
        else os.environ.get(OPERATOR_TOKEN_ENV, "").strip() or None
    )
    source = "keychain" if sys.platform == "darwin" else "environment"
    if (
        value is None
        or not 32 <= len(value) <= 512
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        return None, {
            "status": "UNAVAILABLE",
            "configured": False,
            "source": source,
            "reason": "Local operator credential is missing or inaccessible",
        }
    return value, {
        "status": "READY",
        "configured": True,
        "source": source,
    }


def configure_operator_token() -> Dict[str, Any]:
    """Prompt once for a Keychain-backed local action credential."""

    if sys.platform != "darwin":
        raise RuntimeBlocked("macOS Keychain is required")
    current, _metadata = read_operator_token()
    if current is not None:
        return {
            "status": "READY",
            "configured": True,
            "source": "keychain",
            "changed": False,
            "next_action": "operator token is already configured",
        }
    security = Path("/usr/bin/security")
    if not security.is_file():
        raise RuntimeBlocked("macOS security command is unavailable")
    if not sys.stdin.isatty():
        raise RuntimeBlocked(
            "operator token initialization requires an interactive terminal"
        )
    account = pwd.getpwuid(os.getuid()).pw_name
    try:
        completed = subprocess.run(
            [
                str(security),
                "add-generic-password",
                "-a",
                account,
                "-s",
                OPERATOR_TOKEN_KEYCHAIN_SERVICE,
                "-w",
            ],
            cwd=str(REPO_ROOT),
            check=False,
        )
    except OSError:
        raise RuntimeBlocked("operator token could not be stored in Keychain") from None
    configured, _metadata = read_operator_token()
    if completed.returncode != 0 or configured is None:
        raise RuntimeBlocked(
            "operator token was not stored; enter a value of at least 32 characters"
        )
    return {
        "status": "READY",
        "configured": True,
        "source": "keychain",
        "changed": True,
        "next_action": "run `make autostart-restart`",
    }


def validate_okx_demo_execution_target() -> None:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeBlocked("OKX Demo execution target is BLOCKED") from exc

    try:
        raw_config = yaml.safe_load(
            (REPO_ROOT / "config" / "app.yaml").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise RuntimeBlocked("OKX Demo execution target is BLOCKED") from exc

    execution = raw_config.get("execution") if isinstance(raw_config, dict) else None
    if not isinstance(execution, dict):
        raise RuntimeBlocked("OKX Demo execution target is BLOCKED")
    allowed_manifest_keys = {
        "schema_version",
        "implicit_fallback",
        "targets",
        "non_exchange_scopes",
    }
    if (
        set(execution) - allowed_manifest_keys
        or execution.get("schema_version", "1") != "1"
        or execution.get("implicit_fallback") is not False
    ):
        raise RuntimeBlocked("OKX Demo execution target is BLOCKED")

    targets = execution.get("targets")
    scopes = execution.get("non_exchange_scopes")
    if (
        not isinstance(targets, list)
        or len(targets) != 1
        or not isinstance(scopes, list)
        or len(scopes) != 1
    ):
        raise RuntimeBlocked("OKX Demo execution target is BLOCKED")

    expected_target = {
        "target_id": "OKX_DEMO",
        "status": "ACTIVE",
        "exchange": "okx",
        "product_type": "SWAP",
        "margin_mode": "isolated",
        "position_mode": "long_short_mode",
        "account_mode": "demo",
        "simulated_trading": True,
        "credential_source": "macos_keychain",
        "write_policy": "SOLE_EXCHANGE_ORDER_TARGET",
        "order_submission_enabled": False,
        "allow_real_funds": False,
    }
    expected_scope = {
        "scope_id": "LOCAL_DRY_RUN",
        "scope_type": "local_simulation",
        "exchange_order_execution": False,
        "write_policy": "NO_EXCHANGE_WRITES",
    }

    def matches_exact_contract(actual: Any, expected: Mapping[str, Any]) -> bool:
        return (
            isinstance(actual, dict)
            and set(actual) == set(expected)
            and all(
                type(actual[key]) is type(value) and actual[key] == value
                for key, value in expected.items()
            )
        )

    if not matches_exact_contract(targets[0], expected_target):
        raise RuntimeBlocked("OKX Demo execution target is BLOCKED")
    if not matches_exact_contract(scopes[0], expected_scope):
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


def read_okx_runtime_capability() -> Tuple[
    Optional[Dict[str, str]],
    Dict[str, Any],
]:
    """Read the sole runtime bundle plus its non-secret rotation generation."""

    credentials, metadata = read_okx_demo_credentials()
    if credentials is None:
        return None, metadata
    proof_key = _read_macos_keychain_item(ATTESTATION_PROOF_KEYCHAIN_SERVICE)
    if (
        proof_key is None
        or len(proof_key) != 64
        or any(character not in "0123456789abcdef" for character in proof_key)
    ):
        credentials.clear()
        return None, {
            "status": "BLOCKED",
            "configured": False,
            "source": "keychain",
            "reason": "OKX Demo attestation proof key is missing or inaccessible",
        }
    credentials[ATTESTATION_PROOF_KEY_ENV] = proof_key
    generation = _read_macos_keychain_item(
        OKX_DEMO_CREDENTIAL_GENERATION_SERVICE
    )
    if (
        generation is None
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", generation)
        is None
    ):
        credentials.clear()
        return None, {
            "status": "BLOCKED",
            "configured": False,
            "source": "keychain",
            "reason": "OKX Demo credential generation metadata is missing or invalid",
        }
    return credentials, {
        "status": "READY",
        "configured": True,
        "source": "keychain",
        "_generation": generation,
    }


def configure_okx_credential_generation() -> Dict[str, Any]:
    """Rotate non-secret metadata after the operator replaces all OKX keys."""

    if sys.platform != "darwin":
        raise RuntimeBlocked("macOS Keychain is required")
    security = Path("/usr/bin/security")
    if not security.is_file():
        raise RuntimeBlocked("macOS security command is unavailable")
    credentials, metadata = read_okx_demo_credentials()
    if credentials is None:
        raise RuntimeBlocked(str(metadata["reason"]))
    credentials.clear()
    account = pwd.getpwuid(os.getuid()).pw_name
    generation = uuid4().hex
    try:
        completed = subprocess.run(
            [
                str(security),
                "add-generic-password",
                "-U",
                "-a",
                account,
                "-s",
                OKX_DEMO_CREDENTIAL_GENERATION_SERVICE,
                "-w",
                generation,
            ],
            cwd=str(REPO_ROOT),
            check=False,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeBlocked(
            "OKX Demo credential generation could not be updated"
        ) from None
    if completed.returncode != 0:
        raise RuntimeBlocked(
            "OKX Demo credential generation could not be updated"
        )
    return {
        "status": "READY",
        "credential_generation": "UPDATED",
        "next_action": "run `make autostart-restart`",
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
    *,
    operator_token: Optional[str] = None,
    allow_demo_order: bool = False,
) -> Dict[str, str]:
    """Give each managed service only the credentials it needs."""

    if service not in {
        "backend",
        "worker",
        "frontend",
        "okx_adapter",
        "okx_onboarding",
        "okx_canary",
        "okx_runtime",
    }:
        raise RuntimeBlocked("unknown managed service environment")
    environment = (
        okx_adapter_base_environment()
        if service in {
            "okx_adapter",
            "okx_onboarding",
            "okx_canary",
            "okx_runtime",
        }
        else base_service_environment()
    )
    if service in {"backend", "worker", "okx_runtime"}:
        environment.update(
            {
                "DATABASE_URL": database_url,
            }
        )
    if service in {"backend", "worker"}:
        environment.update(
            {
                "STRATEGY_BLUEPRINT_PROVIDER": MANAGED_STRATEGY_PROVIDER,
                "STRATEGY_BLUEPRINT_MODEL": MANAGED_STRATEGY_MODEL,
            }
        )
    if service == "backend" and operator_token:
        environment[OPERATOR_TOKEN_ENV] = operator_token
    if service in {"backend", "worker"} and deepseek_api_key:
        environment[DEEPSEEK_API_KEY_ENV] = deepseek_api_key
    if service in {
        "okx_adapter",
        "okx_onboarding",
        "okx_canary",
        "okx_runtime",
    }:
        validate_okx_demo_execution_target()
        if service in {"okx_adapter", "okx_runtime"}:
            required_names = (
                *OKX_DEMO_REQUIRED_ENV_NAMES,
                ATTESTATION_PROOF_KEY_ENV,
            )
        elif service == "okx_canary":
            required_names = OKX_DEMO_REQUIRED_ENV_NAMES
        else:
            required_names = OKX_DEMO_CREDENTIAL_ENV_NAMES
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
        if service == "okx_canary":
            if not allow_demo_order:
                raise RuntimeBlocked(
                    "explicit --allow-demo-order authorization is required"
                )
            environment[ALLOW_DEMO_ORDER_ENV] = "true"
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
    backend_path = str(REPO_ROOT / "backend")
    sys.path.insert(0, backend_path)
    try:
        from app.adapters.freqtrade.binary import resolve_freqtrade_binary
    finally:
        sys.path.remove(backend_path)

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
    try:
        completed = subprocess.run(
            [str(backend_python()), "-m", "app.db.migrate", "verify", "--database-url", database_url],
            cwd=str(REPO_ROOT / "backend"),
            env=environment,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SCHEMA_VERIFY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeBlocked(
            "PostgreSQL schema verification timed out; inspect the canonical database"
        ) from None
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
        "\"SELECT count(*) FROM research_jobs WHERE status IN ('PENDING','RUNNING')\""
        ")).scalar_one(); "
        "connection.close(); "
        "raise SystemExit(0 if count == 0 else 3)"
    )
    try:
        completed = subprocess.run(
            [str(backend_python()), "-c", code],
            cwd=str(REPO_ROOT / "backend"),
            env=clean_environment(database_url),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=WORKER_QUEUE_VERIFY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeBlocked(
            "research worker queue read timed out; inspect the canonical database"
        ) from None
    if completed.returncode == 3:
        raise RuntimeBlocked(
            "research worker queue is not idle; resolve PENDING/RUNNING jobs before `make up`"
        )
    if completed.returncode:
        raise RuntimeBlocked(
            "research worker queue read failed; verify runtime database ACL and schema"
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
    proof_key = _read_macos_keychain_item(ATTESTATION_PROOF_KEYCHAIN_SERVICE)
    if (
        proof_key is None
        or len(proof_key) != 64
        or any(character not in "0123456789abcdef" for character in proof_key)
    ):
        credentials.clear()
        return {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "credentials": {
                "status": "BLOCKED",
                "configured": False,
                "source": "keychain",
                "reason": "OKX Demo attestation proof key is missing or inaccessible",
            },
            "reason": "OKX Demo attestation proof key is missing or inaccessible",
        }
    credentials[ATTESTATION_PROOF_KEY_ENV] = proof_key

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
            "position_mode": "long_short_mode",
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
        safe_reason = payload.get("reason")
        if safe_reason not in SAFE_OPERATOR_PREFLIGHT_REASONS:
            safe_reason = (
                "OKX Demo account identity or permissions could not be attested"
            )
        return {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "credentials": credential_status,
            "reason": safe_reason,
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
            "position_mode": "long_short_mode",
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
    except subprocess.TimeoutExpired:
        return {
            "status": "RECOVERY_REQUIRED",
            "execution_target": "OKX_DEMO",
            "credentials": credential_status,
            "reason": "OKX Demo canary child timed out; reconcile its artifact before retry",
        }
    except OSError:
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


OKX_DEMO_CANARY_SEQUENCE_VALUES = frozenset(
    {
        "account_attested",
        "initial_scope_empty",
        "limit_order_accepted",
        "place_outcome_unknown",
        "place_reconciled_by_cl_ord_id",
        "order_queried",
        "place_intent_persisted",
        "cancel_requested",
        "cancel_state_queried",
        "cancel_reconciliation_uncertain",
        "unexpected_fill_cleanup_attempted",
        "post_cancel_fill_cleanup_attempted",
        "final_scope_cleanup_attempted",
        "reservation_recovered_before_write",
        "recovery_started",
        "recovery_verified",
        "final_scope_empty",
    }
)


def _validate_okx_demo_canary_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    status = payload.get("status")
    if status not in {"PASSED", "FAILED", "BLOCKED", "RECOVERY_REQUIRED"}:
        raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    if payload.get("execution_target") != "OKX_DEMO":
        raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    instrument = payload.get("instrument")
    if instrument not in OKX_DEMO_CANARY_ALLOWED_INSTRUMENTS:
        raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    artifact_id = payload.get("artifact_id")
    if (
        not isinstance(artifact_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", artifact_id) is None
    ):
        raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {
        "cl_ord_id_sha256",
        "order_id_sha256",
        "cleanup_cl_ord_id_sha256",
        "simulated_trading_header",
        "sequence",
    }:
        raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    for name in (
        "cl_ord_id_sha256",
        "order_id_sha256",
        "cleanup_cl_ord_id_sha256",
    ):
        value = evidence.get(name)
        if value is not None and (
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
        ):
            raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    sequence = evidence.get("sequence")
    if (
        not isinstance(sequence, list)
        or len(sequence) > 20
        or any(item not in OKX_DEMO_CANARY_SEQUENCE_VALUES for item in sequence)
    ):
        raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    if evidence.get("simulated_trading_header") is not True:
        raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    reason_code = payload.get("reason_code")
    if reason_code is not None and (
        not isinstance(reason_code, str)
        or re.fullmatch(r"[A-Z0-9_]{1,80}", reason_code) is None
    ):
        raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    if status == "PASSED" and reason_code is not None:
        raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    if status != "PASSED" and reason_code is None:
        raise RuntimeBlocked("OKX Demo canary returned invalid evidence")
    return {
        "status": status,
        "execution_target": "OKX_DEMO",
        "artifact_id": artifact_id,
        "instrument": instrument,
        "evidence": {
            "cl_ord_id_sha256": evidence["cl_ord_id_sha256"],
            "order_id_sha256": evidence["order_id_sha256"],
            "cleanup_cl_ord_id_sha256": evidence[
                "cleanup_cl_ord_id_sha256"
            ],
            "simulated_trading_header": True,
            "sequence": list(sequence),
        },
        **({"reason_code": reason_code} if reason_code is not None else {}),
    }


def run_okx_demo_canary(
    *,
    allow_demo_order: bool,
    instrument: str,
) -> Dict[str, Any]:
    """Run the explicit one-shot Demo order canary in its credential child."""

    if not allow_demo_order:
        return {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "reason": "explicit --allow-demo-order authorization is required",
        }
    if instrument not in OKX_DEMO_CANARY_ALLOWED_INSTRUMENTS:
        return {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "reason": "OKX Demo canary instrument is not allowlisted",
        }
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
                "app.adapters.okx_demo.demo_canary",
                "--allow-demo-order",
                "--instrument",
                instrument,
            ],
            cwd=str(REPO_ROOT / "backend"),
            env=service_environment(
                "okx_canary",
                DEFAULT_DATABASE_URL,
                None,
                credentials,
                allow_demo_order=True,
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "credentials": credential_status,
            "reason": "OKX Demo canary child failed or timed out",
        }
    finally:
        credentials.clear()
    try:
        validated = _validate_okx_demo_canary_payload(json.loads(completed.stdout))
    except (TypeError, json.JSONDecodeError, RuntimeBlocked):
        return {
            "status": "RECOVERY_REQUIRED",
            "execution_target": "OKX_DEMO",
            "credentials": credential_status,
            "reason": "OKX Demo canary returned invalid or unsafe evidence",
        }
    expected_exit = {
        "PASSED": 0,
        "FAILED": 1,
        "BLOCKED": 2,
        "RECOVERY_REQUIRED": 2,
    }[validated["status"]]
    if completed.returncode != expected_exit:
        return {
            "status": "RECOVERY_REQUIRED",
            "execution_target": "OKX_DEMO",
            "credentials": credential_status,
            "reason": "OKX Demo canary exit status did not match its evidence",
        }
    validated["credentials"] = credential_status
    return validated


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
    log_descriptor = os.open(
        log_path,
        os.O_CREAT
        | os.O_APPEND
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    log_metadata = os.fstat(log_descriptor)
    if (
        not stat.S_ISREG(log_metadata.st_mode)
        or log_metadata.st_uid != os.getuid()
        or stat.S_IMODE(log_metadata.st_mode) & 0o077
    ):
        os.close(log_descriptor)
        raise RuntimeBlocked("{} log path is unsafe".format(service))
    log_handle = os.fdopen(log_descriptor, "a", encoding="utf-8")
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
    try:
        write_private_state(
            state_dir / PID_FILES[service],
            "{}\n".format(process.pid),
        )
    except OSError:
        os.killpg(process.pid, signal.SIGTERM)
        raise RuntimeBlocked("{} PID state could not be written safely".format(service))


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


def _writer_lock_holder(state_dir: Path) -> Optional[int]:
    path = state_dir / OKX_WRITER_LOCK_FILE
    try:
        descriptor = os.open(
            path,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        return None
    handle = os.fdopen(descriptor, "r+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                handle.seek(0)
                return int(handle.read().strip())
            except ValueError:
                return None
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return None
    finally:
        handle.close()


def okx_runtime_readiness(state_dir: Path) -> Dict[str, Any]:
    """Validate only the safe, fixed child readiness contract."""

    service = process_status(state_dir, "okx_runtime")
    path = state_dir / OKX_RUNTIME_READY_FILE
    if (
        not service["running"]
        or not isinstance(service.get("pid"), int)
        or not is_managed_process(service["pid"], "okx_runtime")
    ):
        return {"status": "BLOCKED", "reason": "OKX runtime is not running"}
    try:
        metadata = path.lstat()
        if (
            not path.is_file()
            or path.is_symlink()
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError
        payload = json.loads(read_private_state(path))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {
            "status": "BLOCKED",
            "reason": "OKX runtime readiness evidence is missing or unsafe",
        }
    if set(payload) != {
        "status",
        "execution_target",
        "adapter",
        "reconciliation",
        "writer",
        "pid",
    }:
        return {
            "status": "BLOCKED",
            "reason": "OKX runtime readiness contains unexpected fields",
        }
    status = payload.get("status")
    reconciliation = payload.get("reconciliation")
    valid_state = (
        status == "READY"
        and reconciliation in {"RECONCILED", "RECOVERED"}
    ) or (
        status == "BLOCKED_OPENINGS"
        and reconciliation in {"DRIFTED", "STALE", "UNKNOWN"}
    )
    if (
        not valid_state
        or payload.get("execution_target") != "OKX_DEMO"
        or payload.get("adapter") != "ATTESTED"
        or payload.get("writer") != "UNIQUE"
        or payload.get("pid") != service["pid"]
        or _writer_lock_holder(state_dir) != service["pid"]
    ):
        return {
            "status": "BLOCKED",
            "reason": "OKX runtime readiness or writer uniqueness is not verified",
        }
    return {
        "status": status,
        "execution_target": "OKX_DEMO",
        "adapter": "ATTESTED",
        "reconciliation": reconciliation,
        "writer": "UNIQUE",
    }


OKX_RUNTIME_STARTUP_TIMEOUT_SECONDS = 300
SCHEMA_VERIFY_TIMEOUT_SECONDS = 90
WORKER_QUEUE_VERIFY_TIMEOUT_SECONDS = 30
BACKEND_STARTUP_TIMEOUT_SECONDS = 120
WORKER_STARTUP_TIMEOUT_SECONDS = 5
FRONTEND_STARTUP_TIMEOUT_SECONDS = 45
STARTUP_PREFLIGHT_BUDGET_SECONDS = 180
STARTUP_CLEANUP_BUDGET_SECONDS = 60
STARTUP_COMMAND_BUDGET_SECONDS = (
    STARTUP_PREFLIGHT_BUDGET_SECONDS
    + BACKEND_STARTUP_TIMEOUT_SECONDS
    + WORKER_STARTUP_TIMEOUT_SECONDS
    + FRONTEND_STARTUP_TIMEOUT_SECONDS
    + OKX_RUNTIME_STARTUP_TIMEOUT_SECONDS
    + STARTUP_CLEANUP_BUDGET_SECONDS
)
SAFE_STARTUP_STAGES = frozenset(
    {
        "backend-readiness",
        "worker-stability",
        "frontend-readiness",
        "okx-runtime-readiness",
    }
)


def wait_for_okx_runtime(
    state_dir: Path,
    timeout_seconds: int = OKX_RUNTIME_STARTUP_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if okx_runtime_readiness(state_dir).get("status") == "READY":
            return
        if not process_status(state_dir, "okx_runtime")["running"]:
            break
        time.sleep(0.25)
    raise RuntimeBlocked(
        "OKX runtime did not establish attested reconciliation and unique writer readiness"
    )


def cleanup_stale_runtime_state(state_dir: Path) -> None:
    """Remove dead PID/readiness evidence; never bless an unrelated live PID."""

    for service in SERVICE_START_ORDER:
        pid_path = state_dir / PID_FILES[service]
        pid = read_pid(pid_path)
        if pid is not None and not process_running(pid):
            pid_path.unlink(missing_ok=True)
        elif pid is None:
            try:
                metadata = pid_path.lstat()
            except OSError:
                continue
            if (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == os.getuid()
            ):
                # Remove legacy/invalid owner-controlled PID evidence. Any
                # live child is rediscovered by marker+cwd before startup.
                pid_path.unlink(missing_ok=True)
    okx_status = process_status(state_dir, "okx_runtime")
    if not okx_status["running"]:
        (state_dir / OKX_RUNTIME_READY_FILE).unlink(missing_ok=True)


def freeze_okx_openings(state_dir: Path, timeout_seconds: int = 5) -> Dict[str, Any]:
    if not process_status(state_dir, "okx_runtime")["running"]:
        raise RuntimeBlocked("OKX runtime is not running; openings cannot be frozen")
    path = state_dir / OPENINGS_FREEZE_FILE
    descriptor = os.open(
        path,
        os.O_CREAT
        | os.O_TRUNC
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("BLOCKED_OPENINGS\n")
        handle.flush()
        os.fsync(handle.fileno())
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        readiness = okx_runtime_readiness(state_dir)
        if readiness.get("status") == "BLOCKED_OPENINGS":
            return {
                "status": "BLOCKED_OPENINGS",
                "reason": "credential capability is unavailable",
            }
        time.sleep(0.1)
    raise RuntimeBlocked("OKX runtime did not freeze openings")


def thaw_okx_openings(state_dir: Path, timeout_seconds: int = 5) -> Dict[str, Any]:
    if not process_status(state_dir, "okx_runtime")["running"]:
        raise RuntimeBlocked("OKX runtime is not running; openings cannot be thawed")
    (state_dir / OPENINGS_FREEZE_FILE).unlink(missing_ok=True)
    deadline = time.monotonic() + timeout_seconds
    last = {"status": "BLOCKED"}
    while time.monotonic() < deadline:
        last = okx_runtime_readiness(state_dir)
        if last.get("status") == "READY" or (
            last.get("status") == "BLOCKED_OPENINGS"
            and last.get("reconciliation") != "UNKNOWN"
        ):
            return last
        time.sleep(0.1)
    return last


def orphaned_managed_process_map(
    state_dir: Path,
    services: Sequence[str],
) -> Dict[str, list[int]]:
    """Classify one process-table snapshot by exact managed markers."""

    tracked = {
        service: read_pid(state_dir / PID_FILES[service])
        for service in services
    }
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeBlocked("managed process discovery is unavailable") from None
    candidates: Dict[str, list[int]] = {
        service: [] for service in services
    }
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        for service in services:
            if SERVICE_PROCESS_MARKERS[service] not in fields[1]:
                continue
            if pid in {os.getpid(), tracked[service]}:
                continue
            if is_managed_process(pid, service):
                candidates[service].append(pid)
    return candidates


def orphaned_managed_processes(
    state_dir: Path,
    service: str,
) -> list[int]:
    return orphaned_managed_process_map(state_dir, (service,))[service]


def cleanup_orphaned_managed_processes(state_dir: Path) -> None:
    """Terminate only exact marker+cwd children missing from canonical PID files."""

    orphaned = orphaned_managed_process_map(
        state_dir,
        SERVICE_STOP_ORDER,
    )
    for service in SERVICE_STOP_ORDER:
        for pid in orphaned[service]:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            deadline = time.monotonic() + 10
            alive = True
            while time.monotonic() < deadline:
                alive = process_running(pid)
                if not alive:
                    break
                time.sleep(0.1)
            if alive:
                os.killpg(pid, signal.SIGKILL)


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
    except PermissionError:
        return {
            "service": service,
            "status": "BLOCKED",
            "pid": pid,
            "reason": "managed process group could not be signaled safely",
        }
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and process_running(pid):
        time.sleep(0.1)
    if process_running(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except PermissionError:
            return {
                "service": service,
                "status": "BLOCKED",
                "pid": pid,
                "reason": "managed process group could not be terminated safely",
            }
    pid_path.unlink(missing_ok=True)
    return {"service": service, "status": "stopped", "pid": pid}


def stop_all(state_dir: Path) -> Dict[str, Any]:
    services = [
        stop_service(state_dir, service)
        for service in SERVICE_STOP_ORDER
    ]
    (state_dir / OKX_RUNTIME_READY_FILE).unlink(missing_ok=True)
    return {"services": services}


def require_complete_startup_cleanup(
    state_dir: Path,
    stopped: Mapping[str, Any],
) -> None:
    blocked = [
        item.get("service", "unknown")
        for item in stopped.get("services", [])
        if item.get("status") == "BLOCKED"
    ]
    remaining = [
        service
        for service in SERVICE_START_ORDER
        if process_status(state_dir, service)["running"]
    ]
    orphaned = orphaned_managed_process_map(
        state_dir,
        SERVICE_START_ORDER,
    )
    orphaned = {
        service: pids for service, pids in orphaned.items() if pids
    }
    lock_holder = _writer_lock_holder(state_dir)
    if blocked or remaining or orphaned or lock_holder is not None:
        raise RuntimeBlocked(
            "runtime startup failed and cleanup is incomplete; "
            "blocked={}, remaining={}, orphaned={}, writer_lock_held={}".format(
                blocked,
                remaining,
                sorted(orphaned),
                lock_holder is not None,
            )
        )


def start(state_dir: Path) -> Dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_runtime_state(state_dir)
    running = [
        service
        for service in SERVICE_START_ORDER
        if process_status(state_dir, service)["running"]
    ]
    if running:
        raise RuntimeBlocked(
            "runtime is already managed; repeated up was refused: {}".format(
                ", ".join(running)
            )
        )
    cleanup_orphaned_managed_processes(state_dir)
    backend_python()
    frontend_vite()
    if not port_available(BACKEND_PORT) or not port_available(FRONTEND_PORT):
        raise RuntimeBlocked("port 8000 or 5173 is already in use; run `make status` before starting")
    database_url = runtime_database_url()
    validate_okx_demo_execution_target()
    ensure_schema(database_url)
    ensure_worker_queue_idle(database_url)
    database = {"identity": redact_database_url(database_url), "schema": "verified"}
    deepseek_api_key, deepseek_credential = read_deepseek_api_key()
    operator_token, operator_credential = read_operator_token()
    if operator_token is None:
        raise RuntimeBlocked(str(operator_credential["reason"]))
    okx_credentials, okx_capability = read_okx_runtime_capability()
    if okx_credentials is None:
        raise RuntimeBlocked(str(okx_capability["reason"]))
    (state_dir / OPENINGS_FREEZE_FILE).unlink(missing_ok=True)
    stage = "backend-readiness"
    stage_started = time.monotonic()
    stage_durations: Dict[str, int] = {}
    try:
        start_service(
            "backend",
            [str(backend_python()), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
            cwd=REPO_ROOT / "backend",
            environment=service_environment(
                "backend",
                database_url,
                deepseek_api_key,
                operator_token=operator_token,
            ),
            state_dir=state_dir,
        )
        wait_for_url(
            "http://127.0.0.1:{}/readyz".format(BACKEND_PORT),
            "backend readiness",
            timeout_seconds=BACKEND_STARTUP_TIMEOUT_SECONDS,
        )
        stage_durations[stage] = int(
            (time.monotonic() - stage_started) * 1000
        )
        stage = "worker-stability"
        stage_started = time.monotonic()
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
        wait_for_process(
            state_dir,
            "worker",
            timeout_seconds=WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        stage_durations[stage] = int(
            (time.monotonic() - stage_started) * 1000
        )
        stage = "frontend-readiness"
        stage_started = time.monotonic()
        start_service(
            "frontend",
            [str(frontend_vite()), "--host", "127.0.0.1", "--port", str(FRONTEND_PORT)],
            cwd=REPO_ROOT / "frontend",
            environment=service_environment("frontend", database_url, deepseek_api_key),
            state_dir=state_dir,
        )
        wait_for_url(
            "http://127.0.0.1:{}/".format(FRONTEND_PORT),
            "frontend",
            timeout_seconds=FRONTEND_STARTUP_TIMEOUT_SECONDS,
        )
        stage_durations[stage] = int(
            (time.monotonic() - stage_started) * 1000
        )
        stage = "okx-runtime-readiness"
        stage_started = time.monotonic()
        start_service(
            "okx_runtime",
            [
                str(backend_python()),
                "-m",
                "app.adapters.okx_demo.runtime_service",
                "--runtime-dir",
                str(state_dir),
            ],
            cwd=REPO_ROOT / "backend",
            environment=service_environment(
                "okx_runtime",
                database_url,
                None,
                okx_credentials,
            ),
            state_dir=state_dir,
        )
        wait_for_okx_runtime(state_dir)
        stage_durations[stage] = int(
            (time.monotonic() - stage_started) * 1000
        )
    except Exception:
        failed_stage = stage
        failed_elapsed_ms = int(
            (time.monotonic() - stage_started) * 1000
        )
        stopped = stop_all(state_dir)
        require_complete_startup_cleanup(state_dir, stopped)
        raise RuntimeBlocked(
            "runtime startup failed at a managed stage and was cleaned up",
            safe_stage=failed_stage,
            elapsed_ms=failed_elapsed_ms,
        ) from None
    finally:
        okx_credentials.clear()
    return {
        "status": "RUNNING",
        "environment": "local",
        "database": database,
        "credentials": {
            "deepseek_provider": deepseek_credential,
            "local_action": operator_credential,
            "okx_demo": {
                key: value
                for key, value in okx_capability.items()
                if key != "_generation"
            },
        },
        "backend_url": "http://127.0.0.1:{}".format(BACKEND_PORT),
        "frontend_url": "http://127.0.0.1:{}".format(FRONTEND_PORT),
        "startup": {
            "status": "READY",
            "stage_elapsed_ms": stage_durations,
        },
        "trading": {"live": False, "dry_run": False, "real_orders": False},
    }


def current_status(state_dir: Path) -> Dict[str, Any]:
    services = [
        process_status(state_dir, service)
        for service in SERVICE_START_ORDER
    ]
    result: Dict[str, Any] = {
        "environment": "local",
        "runtime_dir": str(state_dir),
        "services": services,
        "trading": {"live": False, "dry_run": False, "real_orders": False},
    }
    try:
        validate_okx_demo_execution_target()
        result["execution_target"] = {
            "status": "READY",
            "active": "OKX_DEMO",
        }
    except RuntimeBlocked as exc:
        result["execution_target"] = {
            "status": "BLOCKED",
            "reason": str(exc),
        }
    credentials, capability = read_okx_runtime_capability()
    if credentials is not None:
        credentials.clear()
    operator_token, operator_credential = read_operator_token()
    result["credentials"] = {
        "local_action": operator_credential,
        "okx_demo": {
            key: value for key, value in capability.items() if key != "_generation"
        }
    }
    result["okx_runtime"] = okx_runtime_readiness(state_dir)
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
    ready = (
        all(service["running"] for service in services)
        and result["execution_target"].get("status") == "READY"
        and result["credentials"]["okx_demo"].get("status") == "READY"
        and result["credentials"]["local_action"].get("status") == "READY"
        and result["database"].get("schema") == "verified"
        and result["okx_runtime"].get("status")
        in {"READY", "BLOCKED_OPENINGS"}
    )
    if ready and result["okx_runtime"].get("status") == "BLOCKED_OPENINGS":
        result["status"] = "BLOCKED_OPENINGS"
    else:
        result["status"] = "RUNNING" if ready else "DEGRADED"
    return result


def redact_line(line: str) -> str:
    redacted = SECRET_LINE.sub(
        lambda match: "{}{}***".format(match.group(1), match.group(2)),
        line,
    )
    return DATABASE_CREDENTIALS.sub(r"\1***\3", redacted)


def recent_logs(state_dir: Path, lines: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for service, filename in LOG_FILES.items():
        path = state_dir / filename
        if not path.exists():
            result[service] = {"status": "missing", "path": str(path)}
            continue
        try:
            tail = read_private_state(path).splitlines()[-lines:]
        except OSError:
            result[service] = {
                "status": "BLOCKED",
                "path": str(path),
                "reason": "runtime log path is unsafe",
            }
            continue
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
            "okx-demo-canary",
            "okx-rotate-generation",
            "operator-token-init",
            "operator-token-status",
            "supervisor-capability",
            "supervisor-freeze-openings",
            "supervisor-thaw-openings",
        ),
    )
    parser.add_argument("--runtime-dir")
    parser.add_argument("--lines", type=int, default=80)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-demo-order", action="store_true")
    parser.add_argument(
        "--instrument",
        default=OKX_DEMO_CANARY_DEFAULT_INSTRUMENT,
    )
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
            with runtime_control_lock(state_dir):
                payload = start(state_dir)
        elif args.command == "status":
            payload = current_status(state_dir)
        elif args.command == "down":
            with runtime_control_lock(state_dir):
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
        elif args.command == "okx-demo-canary":
            payload = run_okx_demo_canary(
                allow_demo_order=args.allow_demo_order,
                instrument=args.instrument,
            )
            emit(payload, args.json)
            if payload["status"] == "PASSED":
                return 0
            return 1 if payload["status"] == "FAILED" else 2
        elif args.command == "okx-rotate-generation":
            payload = configure_okx_credential_generation()
        elif args.command == "operator-token-init":
            payload = configure_operator_token()
        elif args.command == "operator-token-status":
            _operator_token, payload = read_operator_token()
        elif args.command == "supervisor-capability":
            credentials, payload = read_okx_runtime_capability()
            if credentials is not None:
                credentials.clear()
            if payload["status"] != "READY":
                raise RuntimeBlocked(str(payload["reason"]))
        elif args.command == "supervisor-freeze-openings":
            with runtime_control_lock(state_dir):
                payload = freeze_okx_openings(state_dir)
        elif args.command == "supervisor-thaw-openings":
            with runtime_control_lock(state_dir):
                payload = thaw_okx_openings(state_dir)
        else:
            with runtime_control_lock(state_dir):
                status = current_status(state_dir)
                running = all(
                    service["running"] for service in status["services"]
                )
                ensure_schema(runtime_database_url())
                if not running:
                    raise RuntimeBlocked(
                        "backend, worker, frontend, and OKX runtime must all be running "
                        "before verification"
                    )
                if (
                    status["execution_target"].get("status") != "READY"
                    or (
                        status["credentials"]["okx_demo"].get("status")
                        != "READY"
                        and status["okx_runtime"].get("status")
                        != "BLOCKED_OPENINGS"
                    )
                    or status["okx_runtime"].get("status")
                    not in {"READY", "BLOCKED_OPENINGS"}
                ):
                    raise RuntimeBlocked(
                        "OKX target, Keychain capability, reconciliation, schema/ACL, "
                        "and writer uniqueness must all be READY"
                    )
                wait_for_url(
                    "http://127.0.0.1:{}/readyz".format(BACKEND_PORT),
                    "backend readiness",
                )
                wait_for_url(
                    "http://127.0.0.1:{}/".format(FRONTEND_PORT),
                    "frontend",
                )
                payload = {
                    **status,
                    "status": (
                        "BLOCKED_OPENINGS"
                        if status["okx_runtime"].get("status")
                        == "BLOCKED_OPENINGS"
                        else "VERIFIED"
                    ),
                }
        emit(payload, args.json)
        return 0
    except RuntimeBlocked as exc:
        blocked = {"status": "BLOCKED", "reason": str(exc)}
        if (
            exc.safe_stage in SAFE_STARTUP_STAGES
            and isinstance(exc.elapsed_ms, int)
            and 0 <= exc.elapsed_ms <= STARTUP_COMMAND_BUDGET_SECONDS * 1000
        ):
            blocked["startup_stage"] = exc.safe_stage
            blocked["startup_stage_elapsed_ms"] = exc.elapsed_ms
        emit(blocked, args.json)
        return 2


if __name__ == "__main__":
    sys.exit(main())
