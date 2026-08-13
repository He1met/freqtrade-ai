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
import errno
import fcntl
import hashlib
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

try:
    from scripts.local_supervisor_control import (
        SupervisorControlBlocked,
        active_supervisor_automation_fence,
        legacy_runtime_stop_fence,
        reload_supervisor_owner,
        resume_supervisor_control,
        supervisor_control_status,
        suspend_supervisor_control,
    )
except ModuleNotFoundError:  # Direct ``python scripts/local_runtime.py``.
    from local_supervisor_control import (  # type: ignore[no-redef]
        SupervisorControlBlocked,
        active_supervisor_automation_fence,
        legacy_runtime_stop_fence,
        reload_supervisor_owner,
        resume_supervisor_control,
        supervisor_control_status,
        suspend_supervisor_control,
    )


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
SERVICE_EXACT_ARGUMENTS = {
    "backend": ("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"),
    "worker": ("-m", "app.workers.deepseek_backtest_worker"),
    "frontend": ("--host", "127.0.0.1", "--port", "5173"),
    "okx_runtime": (
        "-m",
        "app.adapters.okx_demo.runtime_service",
        "--runtime-dir",
        str(DEFAULT_RUNTIME_DIR),
    ),
}
SERVICE_WORKING_DIRECTORIES = {
    "backend": REPO_ROOT / "backend",
    "worker": REPO_ROOT / "backend",
    "frontend": REPO_ROOT / "frontend",
    "okx_runtime": REPO_ROOT / "backend",
}
SERVICE_START_ORDER = ("backend", "worker", "frontend", "okx_runtime")
SERVICE_STOP_ORDER = tuple(reversed(SERVICE_START_ORDER))
LEGACY_SERVICE_PORTS = (BACKEND_PORT, FRONTEND_PORT)
OKX_RUNTIME_READY_FILE = "okx-runtime.ready.json"
OKX_RUNTIME_FAILURE_FILE = "okx-runtime.failure.json"
OKX_WRITER_LOCK_FILE = "okx-demo-order-writer.lock"
CONTROL_LOCK_FILE = "runtime-control.lock"
OPENINGS_FREEZE_FILE = "okx-runtime.freeze-openings"
LEGACY_GROUP_EVIDENCE_PREFIX = "legacy-stop-group."
PROCESS_STATE_RUNNING = "RUNNING"
PROCESS_STATE_ZOMBIE = "ZOMBIE"
PROCESS_STATE_EXITED = "EXITED"
PROCESS_STATE_INACCESSIBLE = "INACCESSIBLE"
MANAGED_PROCESS_MATCH = "MATCH"
MANAGED_PROCESS_NO_MATCH = "NO_MATCH"
MANAGED_PROCESS_INACCESSIBLE = "INACCESSIBLE"
TERMINAL_PROCESS_STATES = frozenset(
    {PROCESS_STATE_ZOMBIE, PROCESS_STATE_EXITED}
)
PROCESS_STATE_PROBE_TIMEOUT_SECONDS = 5
FINAL_PROCESS_TERMINATION_TIMEOUT_SECONDS = 5
SAFE_PROCESS_PROBE_ENV = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "LC_ALL": "C",
}
SUPERVISOR_MAINTENANCE_COMMANDS = frozenset(
    {
        "supervisor-maintenance-suspend",
        "supervisor-maintenance-status",
        "supervisor-maintenance-resume",
        "supervisor-maintenance-stop-legacy",
        "supervisor-maintenance-reload-owner",
    }
)
FENCE_FIRST_RUNTIME_COMMANDS = frozenset(
    {"up", "supervisor-thaw-openings"}
)
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
        okx_runtime_failure_stage: Optional[str] = None,
        okx_runtime_failure_category: Optional[str] = None,
        okx_runtime_failure_type: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.safe_stage = safe_stage
        self.elapsed_ms = elapsed_ms
        self.okx_runtime_failure_stage = okx_runtime_failure_stage
        self.okx_runtime_failure_category = okx_runtime_failure_category
        self.okx_runtime_failure_type = okx_runtime_failure_type


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


def maintenance_runtime_dir(raw_path: Optional[str]) -> Path:
    """Bind every public maintenance command to the canonical owner state."""

    candidate = Path(raw_path).expanduser() if raw_path else DEFAULT_RUNTIME_DIR
    lexical = Path(os.path.abspath(os.fspath(candidate)))
    canonical = Path(os.path.abspath(os.fspath(DEFAULT_RUNTIME_DIR)))
    if lexical != canonical:
        raise RuntimeBlocked(
            "maintenance runtime directory must be the canonical owner state"
        )
    return lexical


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


def process_state(pid: int) -> str:
    """Classify a PID without treating existence as process availability."""

    if pid <= 0:
        return PROCESS_STATE_EXITED
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return PROCESS_STATE_EXITED
    except PermissionError:
        return PROCESS_STATE_INACCESSIBLE
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return PROCESS_STATE_EXITED
        return PROCESS_STATE_INACCESSIBLE

    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "state="],
            check=False,
            text=True,
            capture_output=True,
            timeout=PROCESS_STATE_PROBE_TIMEOUT_SECONDS,
            env=SAFE_PROCESS_PROBE_ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return PROCESS_STATE_INACCESSIBLE

    states = [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip()
    ]
    if completed.returncode == 0 and len(states) == 1:
        fields = states[0].split()
        if len(fields) == 1:
            marker = fields[0][:1].upper()
            if marker == "Z":
                return PROCESS_STATE_ZOMBIE
            if marker == "X":
                return PROCESS_STATE_EXITED
            if marker in {"D", "I", "R", "S", "T", "U", "W"}:
                return PROCESS_STATE_RUNNING
            return PROCESS_STATE_INACCESSIBLE

    # The process may have exited between the initial existence probe and ps.
    # Any other ambiguous result must remain fail-closed.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return PROCESS_STATE_EXITED
    except PermissionError:
        return PROCESS_STATE_INACCESSIBLE
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return PROCESS_STATE_EXITED
        return PROCESS_STATE_INACCESSIBLE
    return PROCESS_STATE_INACCESSIBLE


def process_group_state(pid: int) -> str:
    """Prove that a managed session has no live members before retirement."""

    try:
        completed = subprocess.run(
            ["/bin/ps", "-axo", "pid=,pgid=,state="],
            check=False,
            text=True,
            capture_output=True,
            timeout=PROCESS_STATE_PROBE_TIMEOUT_SECONDS,
            env=SAFE_PROCESS_PROBE_ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return PROCESS_STATE_INACCESSIBLE
    if completed.returncode != 0:
        return PROCESS_STATE_INACCESSIBLE
    seen = False
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        if int(fields[1]) != pid:
            continue
        seen = True
        if len(fields) != 3:
            return PROCESS_STATE_INACCESSIBLE
        marker = fields[2][:1].upper()
        if marker not in {"Z", "X"}:
            return PROCESS_STATE_RUNNING
    return PROCESS_STATE_EXITED if seen else PROCESS_STATE_EXITED


def process_running(pid: int) -> bool:
    return process_state(pid) == PROCESS_STATE_RUNNING


def read_pid(path: Path) -> Optional[int]:
    try:
        raw = read_private_state(path).strip()
    except FileNotFoundError:
        return None
    except OSError:
        raise RuntimeBlocked("PID evidence could not be inspected safely") from None
    if not raw.isdigit() or int(raw) <= 0:
        raise RuntimeBlocked("PID evidence is malformed")
    return int(raw)


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


def legacy_group_evidence_path(state_dir: Path, service: str, pid: int) -> Path:
    return state_dir / "{}{}.{}.pid".format(
        LEGACY_GROUP_EVIDENCE_PREFIX,
        service,
        pid,
    )


def legacy_group_evidence(state_dir: Path) -> Dict[str, list[int]]:
    evidence = {service: [] for service in SERVICE_START_ORDER}
    try:
        entries = list(state_dir.iterdir())
    except FileNotFoundError:
        return evidence
    except OSError:
        raise RuntimeBlocked("legacy process-group evidence is unavailable") from None
    pattern = re.compile(
        r"^{}({})\.([1-9][0-9]*)\.pid$".format(
            re.escape(LEGACY_GROUP_EVIDENCE_PREFIX),
            "|".join(re.escape(service) for service in SERVICE_START_ORDER),
        )
    )
    for path in entries:
        if not path.name.startswith(LEGACY_GROUP_EVIDENCE_PREFIX):
            continue
        match = pattern.fullmatch(path.name)
        if match is None:
            raise RuntimeBlocked("legacy process-group evidence is malformed")
        service = match.group(1)
        pid = int(match.group(2))
        if read_pid(path) != pid:
            raise RuntimeBlocked("legacy process-group evidence is inconsistent")
        evidence[service].append(pid)
    for pids in evidence.values():
        pids.sort()
    return evidence


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


def suspend_supervisor_for_migration(
    state_dir: Path,
    *,
    request_id: str,
    operator_identity: str,
    reason: str,
    target_schema_version: str,
) -> Dict[str, Any]:
    """Atomically fence supervisor automation without reading runtime inputs."""

    state_dir = maintenance_runtime_dir(str(state_dir))
    try:
        return suspend_supervisor_control(
            state_dir,
            request_id=request_id,
            operator_identity=operator_identity,
            reason=reason,
            target_schema_version=target_schema_version,
            trusted_root=REPO_ROOT,
        )
    except SupervisorControlBlocked as exc:
        raise RuntimeBlocked(str(exc)) from None


def resume_supervisor_after_migration(
    state_dir: Path,
    *,
    cutover_generation: str,
    request_id: str,
) -> Dict[str, Any]:
    """CAS-resume the exact suspended cutover generation and request."""

    state_dir = maintenance_runtime_dir(str(state_dir))
    try:
        return resume_supervisor_control(
            state_dir,
            cutover_generation=cutover_generation,
            request_id=request_id,
            trusted_root=REPO_ROOT,
        )
    except SupervisorControlBlocked as exc:
        raise RuntimeBlocked(str(exc)) from None


def read_supervisor_maintenance_status(state_dir: Path) -> Dict[str, Any]:
    """Read only non-secret control and durable observation metadata."""

    state_dir = maintenance_runtime_dir(str(state_dir))
    try:
        return supervisor_control_status(state_dir, trusted_root=REPO_ROOT)
    except SupervisorControlBlocked as exc:
        raise RuntimeBlocked(str(exc)) from None


def stop_legacy_runtime_for_migration(
    state_dir: Path,
    *,
    cutover_generation: str,
    request_id: str,
) -> Dict[str, Any]:
    """Stop only exact marker+cwd legacy children under an observed fence."""

    state_dir = maintenance_runtime_dir(str(state_dir))
    try:
        with legacy_runtime_stop_fence(
            state_dir,
            cutover_generation=cutover_generation,
            request_id=request_id,
            trusted_root=REPO_ROOT,
        ) as authorization:
            with runtime_control_lock(state_dir):
                child_snapshot = authorization["_legacy_child_snapshot"]
                stopped = stop_legacy_snapshot_processes(
                    state_dir,
                    child_snapshot["children"],
                )
                prove_legacy_snapshot_retirement(
                    state_dir,
                    child_snapshot["children"],
                )
                retirement = authorization["_commit_retirement"]()
                authorization["retirement_committed"] = True
                authorization["retirement_receipt"] = retirement
        return {
            "status": "LEGACY_RUNTIME_STOPPED",
            "mode": authorization["mode"],
            "cutover_generation": authorization["cutover_generation"],
            "request_id": authorization["request_id"],
            "services": stopped["services"],
            "automatic_recovery": "FENCED",
            "retirement_committed": authorization[
                "retirement_committed"
            ],
            "legacy_retirement_path": authorization[
                "legacy_retirement_path"
            ],
        }
    except SupervisorControlBlocked as exc:
        raise RuntimeBlocked(str(exc)) from None


def reload_supervisor_for_migration(
    state_dir: Path,
    *,
    cutover_generation: str,
    request_id: str,
) -> Dict[str, Any]:
    """Reload only the launchd supervisor owner; do not touch child services."""

    state_dir = maintenance_runtime_dir(str(state_dir))

    def capture_children() -> Sequence[Mapping[str, Any]]:
        with runtime_control_lock(state_dir):
            return capture_legacy_child_process_snapshot(state_dir)

    try:
        return reload_supervisor_owner(
            state_dir,
            cutover_generation=cutover_generation,
            request_id=request_id,
            capture_legacy_children=capture_children,
            trusted_root=REPO_ROOT,
        )
    except SupervisorControlBlocked as exc:
        raise RuntimeBlocked(str(exc)) from None


def process_status(state_dir: Path, service: str) -> Dict[str, Any]:
    pid_path = state_dir / PID_FILES[service]
    pid = read_pid(pid_path)
    state = process_state(pid) if pid is not None else PROCESS_STATE_EXITED
    return {
        "service": service,
        "pid": pid,
        "running": state == PROCESS_STATE_RUNNING,
        "process_state": state,
        "pid_file": str(pid_path),
    }


def managed_process_identity(pid: int, service: str) -> str:
    """Refuse to signal a reused/stale PID that is not our local process."""

    try:
        if os.getpgid(pid) != pid:
            return MANAGED_PROCESS_NO_MATCH
    except ProcessLookupError:
        return MANAGED_PROCESS_NO_MATCH
    except (PermissionError, OSError):
        return MANAGED_PROCESS_INACCESSIBLE
    try:
        completed = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
            check=False,
            text=True,
            capture_output=True,
            timeout=PROCESS_STATE_PROBE_TIMEOUT_SECONDS,
            env=SAFE_PROCESS_PROBE_ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return MANAGED_PROCESS_INACCESSIBLE
    if completed.returncode != 0:
        return (
            MANAGED_PROCESS_NO_MATCH
            if process_state(pid) in TERMINAL_PROCESS_STATES
            else MANAGED_PROCESS_INACCESSIBLE
        )
    command = completed.stdout.strip()
    expected = SERVICE_PROCESS_MARKERS[service]
    if expected not in command:
        return MANAGED_PROCESS_NO_MATCH
    argument_suffix = " " + " ".join(SERVICE_EXACT_ARGUMENTS[service])
    if not command.endswith(argument_suffix):
        return MANAGED_PROCESS_NO_MATCH
    try:
        cwd_result = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            check=False,
            text=True,
            capture_output=True,
            timeout=PROCESS_STATE_PROBE_TIMEOUT_SECONDS,
            env=SAFE_PROCESS_PROBE_ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return MANAGED_PROCESS_INACCESSIBLE
    if cwd_result.returncode != 0:
        return (
            MANAGED_PROCESS_NO_MATCH
            if process_state(pid) in TERMINAL_PROCESS_STATES
            else MANAGED_PROCESS_INACCESSIBLE
        )
    expected_cwd = SERVICE_WORKING_DIRECTORIES[service]
    cwd_paths = [
        line[1:]
        for line in cwd_result.stdout.splitlines()
        if line.startswith("n")
    ]
    return (
        MANAGED_PROCESS_MATCH
        if cwd_paths == [str(expected_cwd)]
        else MANAGED_PROCESS_NO_MATCH
    )


def _managed_process_ps_value(pid: int, field: str) -> Tuple[str, Optional[str]]:
    try:
        completed = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", field + "="],
            check=False,
            text=True,
            capture_output=True,
            timeout=PROCESS_STATE_PROBE_TIMEOUT_SECONDS,
            env=SAFE_PROCESS_PROBE_ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return MANAGED_PROCESS_INACCESSIBLE, None
    value = completed.stdout.strip()
    if completed.returncode == 0 and value:
        return MANAGED_PROCESS_MATCH, value
    return (
        (MANAGED_PROCESS_NO_MATCH, None)
        if process_state(pid) in TERMINAL_PROCESS_STATES
        else (MANAGED_PROCESS_INACCESSIBLE, None)
    )


def _managed_process_cwd(pid: int) -> Tuple[str, Optional[str]]:
    try:
        completed = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            check=False,
            text=True,
            capture_output=True,
            timeout=PROCESS_STATE_PROBE_TIMEOUT_SECONDS,
            env=SAFE_PROCESS_PROBE_ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return MANAGED_PROCESS_INACCESSIBLE, None
    paths = [
        line[1:]
        for line in completed.stdout.splitlines()
        if line.startswith("n")
    ]
    if completed.returncode == 0 and len(paths) == 1 and paths[0].startswith("/"):
        return MANAGED_PROCESS_MATCH, paths[0]
    return (
        (MANAGED_PROCESS_NO_MATCH, None)
        if process_state(pid) in TERMINAL_PROCESS_STATES
        else (MANAGED_PROCESS_INACCESSIBLE, None)
    )


def managed_process_snapshot(
    pid: int,
    service: str,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Double-probe one exact process identity without persisting its argv."""

    if service not in SERVICE_START_ORDER or pid <= 0:
        return MANAGED_PROCESS_NO_MATCH, None
    state = process_state(pid)
    if state in TERMINAL_PROCESS_STATES:
        return MANAGED_PROCESS_NO_MATCH, None
    if state != PROCESS_STATE_RUNNING:
        return MANAGED_PROCESS_INACCESSIBLE, None
    try:
        first_pgid = os.getpgid(pid)
    except ProcessLookupError:
        return MANAGED_PROCESS_NO_MATCH, None
    except (PermissionError, OSError):
        return MANAGED_PROCESS_INACCESSIBLE, None
    if first_pgid != pid:
        return MANAGED_PROCESS_NO_MATCH, None

    first_values = []
    for field in ("lstart", "command"):
        status, value = _managed_process_ps_value(pid, field)
        if status != MANAGED_PROCESS_MATCH or value is None:
            return status, None
        first_values.append(value)
    cwd_status, first_cwd = _managed_process_cwd(pid)
    if cwd_status != MANAGED_PROCESS_MATCH or first_cwd is None:
        return cwd_status, None
    started, command = first_values
    expected_marker = SERVICE_PROCESS_MARKERS[service]
    expected_suffix = " " + " ".join(SERVICE_EXACT_ARGUMENTS[service])
    if (
        expected_marker not in command
        or not command.endswith(expected_suffix)
        or first_cwd != str(SERVICE_WORKING_DIRECTORIES[service])
    ):
        return MANAGED_PROCESS_NO_MATCH, None

    second_state = process_state(pid)
    try:
        second_pgid = os.getpgid(pid)
    except ProcessLookupError:
        return MANAGED_PROCESS_NO_MATCH, None
    except (PermissionError, OSError):
        return MANAGED_PROCESS_INACCESSIBLE, None
    second_values = []
    for field in ("lstart", "command"):
        status, value = _managed_process_ps_value(pid, field)
        if status != MANAGED_PROCESS_MATCH or value is None:
            return status, None
        second_values.append(value)
    cwd_status, second_cwd = _managed_process_cwd(pid)
    if cwd_status != MANAGED_PROCESS_MATCH or second_cwd is None:
        return cwd_status, None
    if (
        second_state != PROCESS_STATE_RUNNING
        or second_pgid != first_pgid
        or second_values != first_values
        or second_cwd != first_cwd
    ):
        return MANAGED_PROCESS_INACCESSIBLE, None

    command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
    start_token = hashlib.sha256(
        "{}\0{}\0{}\0{}\0{}".format(
            pid,
            first_pgid,
            started,
            command_sha256,
            first_cwd,
        ).encode("utf-8")
    ).hexdigest()
    return MANAGED_PROCESS_MATCH, {
        "service": service,
        "pid": pid,
        "pgid": first_pgid,
        "start_token": start_token,
        "command_sha256": command_sha256,
        "cwd": first_cwd,
    }


def is_managed_process(pid: int, service: str) -> bool:
    return managed_process_identity(pid, service) == MANAGED_PROCESS_MATCH


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
    """Permanent tombstone for the retired direct-transport canary path."""

    del allow_demo_order, instrument
    return {
        "status": "BLOCKED",
        "execution_target": "OKX_DEMO",
        "reason": "direct OKX Demo canary is permanently disabled; use canonical runtime one-shot grant",
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
    if current.get("process_state") == PROCESS_STATE_INACCESSIBLE:
        raise RuntimeBlocked(
            "{} process state is inaccessible; refusing a competing start".format(
                service
            )
        )
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
            probe_timeout = min(
                READINESS_PROBE_TIMEOUT_SECONDS,
                max(0.1, deadline - time.monotonic()),
            )
            with urlopen(url, timeout=probe_timeout) as response:
                if 200 <= response.status < 400:
                    return
        except (URLError, OSError):
            time.sleep(0.5)
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
    except FileNotFoundError:
        return None
    except OSError:
        raise RuntimeBlocked(
            "writer lock evidence could not be inspected safely"
        ) from None
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise RuntimeBlocked("writer lock evidence is not an owner-private file")
    handle = os.fdopen(descriptor, "r+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                handle.seek(0)
                return int(handle.read().strip())
            except (OSError, ValueError):
                raise RuntimeBlocked(
                    "held writer lock evidence contains an invalid owner"
                ) from None
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
        "automation_guard",
        "pid",
    }:
        return {
            "status": "BLOCKED",
            "reason": "OKX runtime readiness contains unexpected fields",
        }
    status = payload.get("status")
    reconciliation = payload.get("reconciliation")
    automation_guard = payload.get("automation_guard")
    known_guard_states = {
        "RUNNING",
        "BLOCKED",
        "COOLDOWN",
        "MANUAL_RESET_REQUIRED",
    }
    valid_state = (
        status == "READY"
        and reconciliation in {"RECONCILED", "RECOVERED"}
        and automation_guard == "RUNNING"
    ) or (
        status == "BLOCKED_OPENINGS"
        and reconciliation in {
            "RECONCILED",
            "RECOVERED",
            "DRIFTED",
            "STALE",
            "UNKNOWN",
        }
        and automation_guard in known_guard_states
    ) or (
        status == "RECOVERY_ONLY"
        and reconciliation == "DRIFTED"
        and automation_guard in known_guard_states
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
        "automation_guard": automation_guard,
    }


OKX_RUNTIME_STARTUP_TIMEOUT_SECONDS = 300
SCHEMA_VERIFY_TIMEOUT_SECONDS = 90
WORKER_QUEUE_VERIFY_TIMEOUT_SECONDS = 30
# PostgreSQL schema readiness is intentionally strict and can take several
# seconds on the canonical local database. Keep one probe in flight long
# enough for that check to finish; a shorter client timeout causes the
# synchronous /readyz handler to continue after the client gives up, and the
# retry loop then piles up idle-in-transaction connections.
READINESS_PROBE_TIMEOUT_SECONDS = 30
# Real canonical macOS cold starts have reached ~181s before Uvicorn
# readiness. Keep enough headroom for normal machine variance while the
# supervisor still enforces the overall startup command budget.
BACKEND_STARTUP_TIMEOUT_SECONDS = 240
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
SAFE_OKX_RUNTIME_FAILURE_STAGES = frozenset(
    {
        "reconciliation-adapter-load",
        "reconciliation-adapter-create",
        "writer-lock",
        "read-attestation",
        "writer-credential-bridge",
        "database-engine",
        "database-connect",
        "database-session",
        "startup-reconciliation",
        "writer-capability",
        "runtime",
    }
)
SAFE_OKX_RUNTIME_FAILURE_CATEGORIES = frozenset(
    {
        "PREFLIGHT",
        "ATTESTATION",
        "DATABASE",
        "RECONCILIATION",
        "WRITER",
        "RUNTIME",
        "UNEXPECTED",
    }
)
SAFE_OKX_RUNTIME_FAILURE_TYPES = frozenset(
    {
        "DatabaseError",
        "IntegrityError",
        "InterfaceError",
        "OkxDemoCredentialsUnavailable",
        "OkxDemoPreflightBlocked",
        "OkxDemoReconciliationBlocked",
        "OkxDemoRuntimeBlocked",
        "OkxDemoWriteBlocked",
        "OperationalError",
        "ProgrammingError",
        "UnexpectedError",
    }
)


def okx_runtime_failure(state_dir: Path) -> Dict[str, str]:
    path = state_dir / OKX_RUNTIME_FAILURE_FILE
    try:
        metadata = path.lstat()
    except OSError:
        return {}
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or set(payload) != {
        "status",
        "stage",
        "category",
        "cause_type",
    }:
        return {}
    stage = payload.get("stage")
    category = payload.get("category")
    cause_type = payload.get("cause_type")
    if (
        payload.get("status") != "BLOCKED"
        or stage not in SAFE_OKX_RUNTIME_FAILURE_STAGES
        or category not in SAFE_OKX_RUNTIME_FAILURE_CATEGORIES
        or cause_type not in SAFE_OKX_RUNTIME_FAILURE_TYPES
    ):
        return {}
    return {
        "stage": stage,
        "category": category,
        "cause_type": cause_type,
    }


def wait_for_okx_runtime(
    state_dir: Path,
    timeout_seconds: int = OKX_RUNTIME_STARTUP_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if okx_runtime_readiness(state_dir).get("status") in {
            "READY",
            "BLOCKED_OPENINGS",
            "RECOVERY_ONLY",
        }:
            return
        if not process_status(state_dir, "okx_runtime")["running"]:
            break
        time.sleep(0.25)
    failure = okx_runtime_failure(state_dir)
    raise RuntimeBlocked(
        "OKX runtime did not establish attested reconciliation and unique writer readiness",
        okx_runtime_failure_stage=failure.get("stage"),
        okx_runtime_failure_category=failure.get("category"),
        okx_runtime_failure_type=failure.get("cause_type"),
    )


def clear_okx_runtime_failure(state_dir: Path) -> None:
    """Remove only owner-controlled stale failure evidence before child spawn."""

    path = state_dir / OKX_RUNTIME_FAILURE_FILE
    try:
        metadata = path.lstat()
    except OSError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise RuntimeBlocked(
            "stale OKX runtime failure evidence is not an owner-controlled file"
        )
    try:
        path.unlink()
    except OSError:
        raise RuntimeBlocked(
            "stale OKX runtime failure evidence could not be cleared"
        ) from None


def cleanup_stale_runtime_state(state_dir: Path) -> None:
    """Remove dead PID/readiness evidence; never bless an unrelated live PID."""

    for service in SERVICE_START_ORDER:
        pid_path = state_dir / PID_FILES[service]
        pid = read_pid(pid_path)
        if pid is not None:
            state = process_state(pid)
            if state in TERMINAL_PROCESS_STATES:
                if process_group_state(pid) not in TERMINAL_PROCESS_STATES:
                    raise RuntimeBlocked(
                        "{} process leader is terminal but its group is not; "
                        "refusing a competing start".format(service)
                    )
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
    if okx_status.get("process_state") in TERMINAL_PROCESS_STATES:
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
    candidates: Dict[str, list[int]] = {
        service: [] for service in services
    }
    for service in services:
        try:
            exact_suffix = re.escape(
                " " + " ".join(SERVICE_EXACT_ARGUMENTS[service])
            ) + "$"
            completed = subprocess.run(
                ["/usr/bin/pgrep", "-f", exact_suffix],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=SAFE_PROCESS_PROBE_ENV,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeBlocked("managed process discovery is unavailable") from None
        if completed.returncode not in {0, 1}:
            raise RuntimeBlocked("managed process discovery is unavailable")
        for raw_pid in completed.stdout.splitlines():
            if not raw_pid.strip().isdigit():
                raise RuntimeBlocked("managed process discovery is ambiguous")
            pid = int(raw_pid.strip())
            if pid in {os.getpid(), tracked[service]}:
                continue
            identity = managed_process_identity(pid, service)
            if identity == MANAGED_PROCESS_INACCESSIBLE:
                raise RuntimeBlocked(
                    "managed process ownership could not be established safely"
                )
            if identity == MANAGED_PROCESS_MATCH:
                candidates[service].append(pid)
    return candidates


def orphaned_managed_processes(
    state_dir: Path,
    service: str,
) -> list[int]:
    return orphaned_managed_process_map(state_dir, (service,))[service]


def _legacy_service_candidate_pids(
    state_dir: Path,
    services: Sequence[str],
) -> Tuple[Dict[str, Optional[int]], Dict[str, list[int]]]:
    tracked = {
        service: read_pid(state_dir / PID_FILES[service])
        for service in services
    }
    candidates: Dict[str, list[int]] = {}
    for service in services:
        try:
            exact_suffix = re.escape(
                " " + " ".join(SERVICE_EXACT_ARGUMENTS[service])
            ) + "$"
            completed = subprocess.run(
                ["/usr/bin/pgrep", "-f", exact_suffix],
                check=False,
                capture_output=True,
                text=True,
                timeout=PROCESS_STATE_PROBE_TIMEOUT_SECONDS,
                env=SAFE_PROCESS_PROBE_ENV,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeBlocked(
                "legacy child candidate discovery is unavailable"
            ) from None
        if completed.returncode not in {0, 1}:
            raise RuntimeBlocked(
                "legacy child candidate discovery is unavailable"
            )
        service_pids = set()
        if tracked[service] is not None:
            service_pids.add(int(tracked[service]))
        for raw_pid in completed.stdout.splitlines():
            if not raw_pid.strip().isdigit() or int(raw_pid.strip()) <= 0:
                raise RuntimeBlocked(
                    "legacy child candidate discovery is ambiguous"
                )
            pid = int(raw_pid.strip())
            if pid != os.getpid():
                service_pids.add(pid)
        candidates[service] = sorted(service_pids)
    return tracked, candidates


def _legacy_child_process_snapshot_once(
    state_dir: Path,
    *,
    allowed_terminal_groups: frozenset[Tuple[str, int]] = frozenset(),
) -> list[Dict[str, Any]]:
    tracked, candidates = _legacy_service_candidate_pids(
        state_dir,
        SERVICE_START_ORDER,
    )
    snapshot = []
    seen_pids = set()
    for service in SERVICE_START_ORDER:
        for pid in candidates[service]:
            status, identity = managed_process_snapshot(pid, service)
            if status == MANAGED_PROCESS_MATCH and identity is not None:
                if pid in seen_pids:
                    raise RuntimeBlocked(
                        "legacy child candidate has ambiguous service ownership"
                    )
                seen_pids.add(pid)
                snapshot.append(identity)
                continue
            state = process_state(pid)
            if pid == tracked[service]:
                group_state = process_group_state(pid)
                if (
                    state in TERMINAL_PROCESS_STATES
                    and group_state in TERMINAL_PROCESS_STATES
                ):
                    continue
                if (
                    state in TERMINAL_PROCESS_STATES
                    and group_state == PROCESS_STATE_RUNNING
                ):
                    if (service, pid) in allowed_terminal_groups:
                        continue
                    raise RuntimeBlocked(
                        "legacy child snapshot cannot establish a terminal leader's live group identity"
                    )
                raise RuntimeBlocked(
                    "tracked legacy child identity could not be snapshotted safely"
                )
            if status == MANAGED_PROCESS_INACCESSIBLE:
                raise RuntimeBlocked(
                    "legacy child candidate identity is inaccessible"
                )
            if state == PROCESS_STATE_RUNNING:
                raise RuntimeBlocked(
                    "legacy child candidate ownership is ambiguous"
                )
    snapshot.sort(key=lambda child: (str(child["service"]), int(child["pid"])))
    return snapshot


def capture_legacy_child_process_snapshot(
    state_dir: Path,
) -> Sequence[Mapping[str, Any]]:
    """Capture a stable child generation while the legacy owner is paused."""

    first = _legacy_child_process_snapshot_once(state_dir)
    second = _legacy_child_process_snapshot_once(state_dir)
    if first != second:
        raise RuntimeBlocked(
            "legacy child candidate set changed during generation snapshot"
        )
    return second


def _legacy_snapshot_children_by_key(
    children: Sequence[Mapping[str, Any]],
) -> Dict[Tuple[str, int], Dict[str, Any]]:
    indexed: Dict[Tuple[str, int], Dict[str, Any]] = {}
    seen_pids = set()
    for raw_child in children:
        child = dict(raw_child)
        service = child.get("service")
        pid = child.get("pid")
        if (
            service not in SERVICE_START_ORDER
            or type(pid) is not int
            or pid <= 0
            or child.get("pgid") != pid
            or (service, pid) in indexed
            or pid in seen_pids
        ):
            raise RuntimeBlocked("legacy child snapshot is invalid")
        indexed[(str(service), pid)] = child
        seen_pids.add(pid)
    return indexed


def _matching_live_snapshot_identity(child: Mapping[str, Any]) -> bool:
    status, current = managed_process_snapshot(
        int(child["pid"]),
        str(child["service"]),
    )
    return status == MANAGED_PROCESS_MATCH and current == dict(child)


def _preflight_legacy_snapshot_stop(
    state_dir: Path,
    children: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    expected = _legacy_snapshot_children_by_key(children)
    allowed_terminal_groups = frozenset(expected)
    first = _legacy_child_process_snapshot_once(
        state_dir,
        allowed_terminal_groups=allowed_terminal_groups,
    )
    second = _legacy_child_process_snapshot_once(
        state_dir,
        allowed_terminal_groups=allowed_terminal_groups,
    )
    if first != second:
        raise RuntimeBlocked(
            "legacy child candidate set changed before stop; zero signals sent"
        )
    for current in second:
        key = (str(current["service"]), int(current["pid"]))
        if key not in expected or expected[key] != current:
            raise RuntimeBlocked(
                "unexpected legacy child candidate blocks stop; zero signals sent"
            )

    for service in SERVICE_START_ORDER:
        tracked_pid = read_pid(state_dir / PID_FILES[service])
        if tracked_pid is not None and (service, tracked_pid) not in expected:
            if (
                process_state(tracked_pid) not in TERMINAL_PROCESS_STATES
                or process_group_state(tracked_pid)
                not in TERMINAL_PROCESS_STATES
            ):
                raise RuntimeBlocked(
                    "unexpected tracked legacy child blocks stop; zero signals sent"
                )
    recorded = legacy_group_evidence(state_dir)
    for service, pids in recorded.items():
        if any((service, pid) not in expected for pid in pids):
            raise RuntimeBlocked(
                "unexpected legacy process-group evidence blocks stop; zero signals sent"
            )

    plan = []
    for key, child in expected.items():
        pid = int(child["pid"])
        state = process_state(pid)
        group_state = process_group_state(pid)
        if group_state == PROCESS_STATE_INACCESSIBLE:
            raise RuntimeBlocked(
                "legacy child process group is inaccessible; zero signals sent"
            )
        if group_state in TERMINAL_PROCESS_STATES:
            continue
        if state == PROCESS_STATE_RUNNING:
            if not _matching_live_snapshot_identity(child):
                raise RuntimeBlocked(
                    "legacy child identity changed before stop; zero signals sent"
                )
        elif state not in TERMINAL_PROCESS_STATES:
            raise RuntimeBlocked(
                "legacy child state is inaccessible; zero signals sent"
            )
        plan.append(dict(child))
    order = {service: index for index, service in enumerate(SERVICE_STOP_ORDER)}
    plan.sort(key=lambda child: (order[str(child["service"])], int(child["pid"])))
    return plan


def _snapshot_group_signalable(child: Mapping[str, Any]) -> bool:
    pid = int(child["pid"])
    state = process_state(pid)
    group_state = process_group_state(pid)
    if group_state == PROCESS_STATE_INACCESSIBLE:
        raise RuntimeBlocked("legacy child process group became inaccessible")
    if group_state in TERMINAL_PROCESS_STATES:
        return False
    if state == PROCESS_STATE_RUNNING:
        if not _matching_live_snapshot_identity(child):
            raise RuntimeBlocked(
                "legacy child identity changed immediately before signaling"
            )
        return True
    if state in TERMINAL_PROCESS_STATES:
        # A durable live-leader snapshot binds this still-live orphaned PGID.
        return True
    raise RuntimeBlocked("legacy child state became inaccessible before signaling")


def _wait_for_snapshot_groups(
    children: Sequence[Mapping[str, Any]],
    timeout_seconds: float,
) -> list[Dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    remaining = [dict(child) for child in children]
    while remaining and time.monotonic() < deadline:
        next_remaining = []
        for child in remaining:
            state = process_group_state(int(child["pid"]))
            if state == PROCESS_STATE_INACCESSIBLE:
                raise RuntimeBlocked(
                    "legacy child process-group termination is inaccessible"
                )
            if state not in TERMINAL_PROCESS_STATES:
                next_remaining.append(child)
        remaining = next_remaining
        if remaining:
            time.sleep(0.1)
    return remaining


def stop_legacy_snapshot_processes(
    state_dir: Path,
    children: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Signal only identities durably captured for this cutover generation."""

    expected = _legacy_snapshot_children_by_key(children)
    plan = _preflight_legacy_snapshot_stop(state_dir, children)
    # Revalidate the complete plan once more before the first signal. This
    # closes PID reuse between discovery and the transaction's signal phase.
    signalable = [child for child in plan if _snapshot_group_signalable(child)]
    for child in signalable:
        try:
            os.killpg(int(child["pgid"]), signal.SIGTERM)
        except ProcessLookupError:
            if process_group_state(int(child["pid"])) not in TERMINAL_PROCESS_STATES:
                raise RuntimeBlocked(
                    "legacy child process group disappeared ambiguously"
                ) from None
        except PermissionError:
            raise RuntimeBlocked(
                "legacy child process group could not be signaled safely"
            ) from None

    remaining = _wait_for_snapshot_groups(signalable, 10)
    killable = [
        child for child in remaining if _snapshot_group_signalable(child)
    ]
    for child in killable:
        try:
            os.killpg(int(child["pgid"]), signal.SIGKILL)
        except ProcessLookupError:
            if process_group_state(int(child["pid"])) not in TERMINAL_PROCESS_STATES:
                raise RuntimeBlocked(
                    "legacy child process group could not be resolved safely"
                ) from None
        except PermissionError:
            raise RuntimeBlocked(
                "legacy child process group could not be terminated safely"
            ) from None
    remaining = _wait_for_snapshot_groups(
        killable,
        FINAL_PROCESS_TERMINATION_TIMEOUT_SECONDS,
    )
    if remaining:
        raise RuntimeBlocked(
            "legacy child process group did not reach a terminal state"
        )

    for (service, pid), _child in expected.items():
        if process_group_state(pid) not in TERMINAL_PROCESS_STATES:
            raise RuntimeBlocked(
                "legacy child process group still has live members"
            )
        pid_path = state_dir / PID_FILES[service]
        if read_pid(pid_path) == pid:
            pid_path.unlink(missing_ok=True)
        evidence_path = legacy_group_evidence_path(state_dir, service, pid)
        if evidence_path.exists() and read_pid(evidence_path) == pid:
            evidence_path.unlink(missing_ok=True)
    for service in SERVICE_START_ORDER:
        pid_path = state_dir / PID_FILES[service]
        pid = read_pid(pid_path)
        if (
            pid is not None
            and process_state(pid) in TERMINAL_PROCESS_STATES
            and process_group_state(pid) in TERMINAL_PROCESS_STATES
        ):
            pid_path.unlink(missing_ok=True)
    (state_dir / OKX_RUNTIME_READY_FILE).unlink(missing_ok=True)

    services = []
    for service in SERVICE_STOP_ORDER:
        service_children = [
            child
            for child in expected.values()
            if child["service"] == service
        ]
        if not service_children:
            services.append({"service": service, "status": "not-snapshotted"})
        else:
            services.extend(
                {
                    "service": service,
                    "status": "stopped",
                    "pid": int(child["pid"]),
                }
                for child in sorted(
                    service_children,
                    key=lambda item: int(item["pid"]),
                )
            )
    return {"services": services}


def prove_legacy_snapshot_retirement(
    state_dir: Path,
    children: Sequence[Mapping[str, Any]],
) -> None:
    expected = _legacy_snapshot_children_by_key(children)
    for _proof_round in range(2):
        for _key, child in expected.items():
            state = process_group_state(int(child["pid"]))
            if state not in TERMINAL_PROCESS_STATES:
                raise RuntimeBlocked(
                    "legacy child snapshot terminal proof is incomplete"
                )
        if _legacy_child_process_snapshot_once(state_dir):
            raise RuntimeBlocked(
                "unexpected legacy child candidate blocks retirement receipt"
            )
        for service in SERVICE_START_ORDER:
            if read_pid(state_dir / PID_FILES[service]) is not None:
                raise RuntimeBlocked(
                    "legacy tracked PID evidence remains before retirement"
                )
        if any(legacy_group_evidence(state_dir).values()):
            raise RuntimeBlocked(
                "legacy process-group evidence remains before retirement"
            )
        if any(legacy_service_port_owners().values()):
            raise RuntimeBlocked(
                "legacy service port ownership is not stably empty before retirement"
            )
        if _writer_lock_holder(state_dir) is not None:
            raise RuntimeBlocked(
                "legacy writer lock remains held before retirement"
            )


def legacy_service_port_owners() -> Dict[int, list[int]]:
    """Return bounded local listener metadata; never connect to a service."""

    owners: Dict[int, list[int]] = {}
    for port in LEGACY_SERVICE_PORTS:
        try:
            completed = subprocess.run(
                [
                    "/usr/sbin/lsof",
                    "-nP",
                    "-iTCP:{}".format(port),
                    "-sTCP:LISTEN",
                    "-Fp",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=PROCESS_STATE_PROBE_TIMEOUT_SECONDS,
                env=SAFE_PROCESS_PROBE_ENV,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeBlocked(
                "legacy service port ownership is unavailable"
            ) from None
        if completed.returncode not in {0, 1}:
            raise RuntimeBlocked(
                "legacy service port ownership is unavailable"
            )
        port_owners = []
        for line in completed.stdout.splitlines():
            if not line:
                continue
            if not line.startswith("p") or not line[1:].isdigit():
                raise RuntimeBlocked(
                    "legacy service port ownership is ambiguous"
                )
            pid = int(line[1:])
            if pid <= 0:
                raise RuntimeBlocked(
                    "legacy service port ownership is ambiguous"
                )
            port_owners.append(pid)
        owners[port] = sorted(set(port_owners))
    return owners


def cleanup_orphaned_managed_processes(state_dir: Path) -> None:
    """Terminate only exact marker+cwd children missing from canonical PID files."""

    orphaned = orphaned_managed_process_map(
        state_dir,
        SERVICE_STOP_ORDER,
    )
    recorded = legacy_group_evidence(state_dir)
    for service in SERVICE_STOP_ORDER:
        orphaned[service] = sorted(
            set(orphaned[service]) | set(recorded[service])
        )
    for service in SERVICE_STOP_ORDER:
        for pid in orphaned[service]:
            evidence_path = legacy_group_evidence_path(state_dir, service, pid)
            identity = managed_process_identity(pid, service)
            if identity != MANAGED_PROCESS_MATCH:
                state = process_state(pid)
                if state in TERMINAL_PROCESS_STATES:
                    if process_group_state(pid) not in TERMINAL_PROCESS_STATES:
                        raise RuntimeBlocked(
                            "managed orphan process group still has live members"
                        )
                    evidence_path.unlink(missing_ok=True)
                    continue
                if identity == MANAGED_PROCESS_INACCESSIBLE:
                    raise RuntimeBlocked(
                        "managed orphan process ownership could not be "
                        "re-established safely"
                    )
                raise RuntimeBlocked(
                    "managed orphan process identity changed before signaling"
                )
            if pid not in recorded[service]:
                write_private_state(evidence_path, "{}\n".format(pid))
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                if process_state(pid) in TERMINAL_PROCESS_STATES:
                    if process_group_state(pid) not in TERMINAL_PROCESS_STATES:
                        raise RuntimeBlocked(
                            "managed orphan process group still has live members"
                        )
                    evidence_path.unlink(missing_ok=True)
                    continue
                raise RuntimeBlocked(
                    "managed orphan process group could not be resolved safely"
                ) from None
            except PermissionError:
                raise RuntimeBlocked(
                    "managed orphan process group could not be signaled safely"
                ) from None
            deadline = time.monotonic() + 10
            state = process_state(pid)
            while (
                time.monotonic() < deadline
                and state == PROCESS_STATE_RUNNING
            ):
                time.sleep(0.1)
                state = process_state(pid)
            if state in TERMINAL_PROCESS_STATES:
                if process_group_state(pid) not in TERMINAL_PROCESS_STATES:
                    raise RuntimeBlocked(
                        "managed orphan process group still has live members"
                    )
                evidence_path.unlink(missing_ok=True)
                continue
            if state == PROCESS_STATE_INACCESSIBLE:
                raise RuntimeBlocked(
                    "managed orphan process state is inaccessible"
                )
            if not is_managed_process(pid, service):
                state = process_state(pid)
                if state in TERMINAL_PROCESS_STATES:
                    if process_group_state(pid) not in TERMINAL_PROCESS_STATES:
                        raise RuntimeBlocked(
                            "managed orphan process group still has live members"
                        )
                    evidence_path.unlink(missing_ok=True)
                    continue
                raise RuntimeBlocked(
                    "managed orphan process identity changed before termination"
                )
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                state = process_state(pid)
            except PermissionError:
                raise RuntimeBlocked(
                    "managed orphan process group could not be terminated safely"
                ) from None
            else:
                state = process_state(pid)
            if state == PROCESS_STATE_RUNNING:
                deadline = (
                    time.monotonic()
                    + FINAL_PROCESS_TERMINATION_TIMEOUT_SECONDS
                )
                while (
                    time.monotonic() < deadline
                    and state == PROCESS_STATE_RUNNING
                ):
                    time.sleep(0.1)
                    state = process_state(pid)
            if state not in TERMINAL_PROCESS_STATES:
                raise RuntimeBlocked(
                    "managed orphan process group did not reach a terminal state"
                )
            if process_group_state(pid) not in TERMINAL_PROCESS_STATES:
                raise RuntimeBlocked(
                    "managed orphan process group still has live members"
                )
            evidence_path.unlink(missing_ok=True)


def stop_service(state_dir: Path, service: str) -> Dict[str, Any]:
    pid_path = state_dir / PID_FILES[service]
    pid = read_pid(pid_path)
    if pid is None:
        return {"service": service, "status": "not-managed"}
    state = process_state(pid)
    if state in TERMINAL_PROCESS_STATES:
        group_state = process_group_state(pid)
        if group_state not in TERMINAL_PROCESS_STATES:
            return {
                "service": service,
                "status": "BLOCKED",
                "pid": pid,
                "reason": "managed process leader is terminal but its group still has live members",
            }
        pid_path.unlink(missing_ok=True)
        return {"service": service, "status": "stale-pid-removed", "pid": pid}
    if state == PROCESS_STATE_INACCESSIBLE:
        return {
            "service": service,
            "status": "BLOCKED",
            "pid": pid,
            "reason": "managed process state could not be established safely",
        }
    if not is_managed_process(pid, service):
        state = process_state(pid)
        if state in TERMINAL_PROCESS_STATES:
            if process_group_state(pid) not in TERMINAL_PROCESS_STATES:
                return {
                    "service": service,
                    "status": "BLOCKED",
                    "pid": pid,
                    "reason": "managed process leader is terminal but its group still has live members",
                }
            pid_path.unlink(missing_ok=True)
            return {
                "service": service,
                "status": "stale-pid-removed",
                "pid": pid,
            }
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
    state = process_state(pid)
    while time.monotonic() < deadline and state == PROCESS_STATE_RUNNING:
        time.sleep(0.1)
        state = process_state(pid)
    if state == PROCESS_STATE_INACCESSIBLE:
        return {
            "service": service,
            "status": "BLOCKED",
            "pid": pid,
            "reason": "managed process termination could not be verified safely",
        }
    if state == PROCESS_STATE_RUNNING:
        if not is_managed_process(pid, service):
            state = process_state(pid)
            if state in TERMINAL_PROCESS_STATES:
                if process_group_state(pid) not in TERMINAL_PROCESS_STATES:
                    return {
                        "service": service,
                        "status": "BLOCKED",
                        "pid": pid,
                        "reason": "managed process leader terminated but its group still has live members",
                    }
                pid_path.unlink(missing_ok=True)
                return {
                    "service": service,
                    "status": "stopped",
                    "pid": pid,
                }
            return {
                "service": service,
                "status": "BLOCKED",
                "pid": pid,
                "reason": "managed process identity changed during termination",
            }
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            state = process_state(pid)
        except PermissionError:
            return {
                "service": service,
                "status": "BLOCKED",
                "pid": pid,
                "reason": "managed process group could not be terminated safely",
            }
        else:
            deadline = (
                time.monotonic()
                + FINAL_PROCESS_TERMINATION_TIMEOUT_SECONDS
            )
            state = process_state(pid)
            while (
                time.monotonic() < deadline
                and state == PROCESS_STATE_RUNNING
            ):
                time.sleep(0.1)
                state = process_state(pid)
    if state not in TERMINAL_PROCESS_STATES:
        return {
            "service": service,
            "status": "BLOCKED",
            "pid": pid,
            "reason": "managed process group did not reach a terminal state",
        }
    if process_group_state(pid) not in TERMINAL_PROCESS_STATES:
        return {
            "service": service,
            "status": "BLOCKED",
            "pid": pid,
            "reason": "managed process leader terminated but its group still has live members",
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
    stopped_pids = {
        str(item.get("service")): int(item["pid"])
        for item in stopped.get("services", [])
        if isinstance(item, Mapping)
        and isinstance(item.get("service"), str)
        and type(item.get("pid")) is int
        and int(item["pid"]) > 0
    }
    statuses = {
        service: process_status(state_dir, service)
        for service in SERVICE_START_ORDER
    }
    nonterminal_groups = [
        service
        for service, pid in stopped_pids.items()
        if process_group_state(pid)
        not in TERMINAL_PROCESS_STATES
    ]
    remaining = [
        service for service, status in statuses.items() if status["running"]
    ]
    inaccessible = [
        service
        for service, status in statuses.items()
        if status.get("process_state") == PROCESS_STATE_INACCESSIBLE
    ]
    orphaned = orphaned_managed_process_map(
        state_dir,
        SERVICE_START_ORDER,
    )
    orphaned = {
        service: pids for service, pids in orphaned.items() if pids
    }
    recorded_groups = {
        service: pids
        for service, pids in legacy_group_evidence(state_dir).items()
        if pids
    }
    lock_holder = _writer_lock_holder(state_dir)
    if (
        blocked
        or remaining
        or inaccessible
        or nonterminal_groups
        or orphaned
        or recorded_groups
        or lock_holder is not None
    ):
        raise RuntimeBlocked(
            "runtime startup failed and cleanup is incomplete; "
            "blocked={}, remaining={}, inaccessible={}, nonterminal_groups={}, orphaned={}, recorded_groups={}, "
            "writer_lock_held={}".format(
                blocked,
                remaining,
                inaccessible,
                nonterminal_groups,
                sorted(orphaned),
                sorted(recorded_groups),
                lock_holder is not None,
            )
        )


def start(state_dir: Path) -> Dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_runtime_state(state_dir)
    statuses = {
        service: process_status(state_dir, service)
        for service in SERVICE_START_ORDER
    }
    running = [
        service for service, status in statuses.items() if status["running"]
    ]
    inaccessible = [
        service
        for service, status in statuses.items()
        if status.get("process_state") == PROCESS_STATE_INACCESSIBLE
    ]
    if running or inaccessible:
        raise RuntimeBlocked(
            "runtime is already managed; repeated up was refused: {}".format(
                ", ".join(running + inaccessible)
            )
        )
    cleanup_orphaned_managed_processes(state_dir)
    clear_okx_runtime_failure(state_dir)
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
    except Exception as exc:
        failed_stage = stage
        failed_elapsed_ms = int(
            (time.monotonic() - stage_started) * 1000
        )
        runtime_failure_stage = getattr(
            exc,
            "okx_runtime_failure_stage",
            None,
        )
        runtime_failure_type = getattr(
            exc,
            "okx_runtime_failure_type",
            None,
        )
        runtime_failure_category = getattr(
            exc,
            "okx_runtime_failure_category",
            None,
        )
        stopped = stop_all(state_dir)
        require_complete_startup_cleanup(state_dir, stopped)
        raise RuntimeBlocked(
            "runtime startup failed at a managed stage and was cleaned up",
            safe_stage=failed_stage,
            elapsed_ms=failed_elapsed_ms,
            okx_runtime_failure_stage=runtime_failure_stage,
            okx_runtime_failure_category=runtime_failure_category,
            okx_runtime_failure_type=runtime_failure_type,
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
        in {"READY", "BLOCKED_OPENINGS", "RECOVERY_ONLY"}
    )
    if ready and result["okx_runtime"].get("status") in {
        "BLOCKED_OPENINGS",
        "RECOVERY_ONLY",
    }:
        result["status"] = result["okx_runtime"]["status"]
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
            "supervisor-maintenance-suspend",
            "supervisor-maintenance-status",
            "supervisor-maintenance-resume",
            "supervisor-maintenance-stop-legacy",
            "supervisor-maintenance-reload-owner",
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
    parser.add_argument("--request-id")
    parser.add_argument("--operator-identity")
    parser.add_argument("--reason")
    parser.add_argument("--target-schema-version")
    parser.add_argument("--cutover-generation")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if (
            args.command not in SUPERVISOR_MAINTENANCE_COMMANDS
            and args.command not in FENCE_FIRST_RUNTIME_COMMANDS
        ):
            load_runtime_environment()
        state_dir = (
            maintenance_runtime_dir(args.runtime_dir)
            if args.command in SUPERVISOR_MAINTENANCE_COMMANDS
            else runtime_dir(args.runtime_dir)
        )
        if args.command == "doctor":
            payload = doctor(state_dir)
        elif args.command == "bootstrap":
            payload = bootstrap()
        elif args.command == "up":
            try:
                with active_supervisor_automation_fence(
                    DEFAULT_RUNTIME_DIR,
                    trusted_root=REPO_ROOT,
                ):
                    load_runtime_environment()
                    with runtime_control_lock(state_dir):
                        payload = start(state_dir)
            except SupervisorControlBlocked as exc:
                raise RuntimeBlocked(str(exc)) from None
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
            try:
                with active_supervisor_automation_fence(
                    DEFAULT_RUNTIME_DIR,
                    trusted_root=REPO_ROOT,
                ):
                    with runtime_control_lock(state_dir):
                        payload = thaw_okx_openings(state_dir)
            except SupervisorControlBlocked as exc:
                raise RuntimeBlocked(str(exc)) from None
        elif args.command == "supervisor-maintenance-suspend":
            if not all(
                isinstance(value, str) and value
                for value in (
                    args.request_id,
                    args.operator_identity,
                    args.reason,
                    args.target_schema_version,
                )
            ):
                raise RuntimeBlocked(
                    "supervisor maintenance suspend requires request, operator, "
                    "reason, and target schema metadata"
                )
            payload = suspend_supervisor_for_migration(
                state_dir,
                request_id=args.request_id,
                operator_identity=args.operator_identity,
                reason=args.reason,
                target_schema_version=args.target_schema_version,
            )
        elif args.command == "supervisor-maintenance-status":
            payload = read_supervisor_maintenance_status(state_dir)
        elif args.command == "supervisor-maintenance-resume":
            if not all(
                isinstance(value, str) and value
                for value in (args.cutover_generation, args.request_id)
            ):
                raise RuntimeBlocked(
                    "supervisor maintenance resume requires the exact cutover "
                    "generation and request"
                )
            payload = resume_supervisor_after_migration(
                state_dir,
                cutover_generation=args.cutover_generation,
                request_id=args.request_id,
            )
        elif args.command == "supervisor-maintenance-stop-legacy":
            if not all(
                isinstance(value, str) and value
                for value in (args.cutover_generation, args.request_id)
            ):
                raise RuntimeBlocked(
                    "legacy runtime stop requires the exact cutover generation "
                    "and request"
                )
            payload = stop_legacy_runtime_for_migration(
                state_dir,
                cutover_generation=args.cutover_generation,
                request_id=args.request_id,
            )
        elif args.command == "supervisor-maintenance-reload-owner":
            if not all(
                isinstance(value, str) and value
                for value in (args.cutover_generation, args.request_id)
            ):
                raise RuntimeBlocked(
                    "supervisor owner reload requires the exact cutover "
                    "generation and request"
                )
            payload = reload_supervisor_for_migration(
                state_dir,
                cutover_generation=args.cutover_generation,
                request_id=args.request_id,
            )
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
                        not in {"BLOCKED_OPENINGS", "RECOVERY_ONLY"}
                    )
                    or status["okx_runtime"].get("status")
                    not in {"READY", "BLOCKED_OPENINGS", "RECOVERY_ONLY"}
                ):
                    raise RuntimeBlocked(
                        "OKX target, Keychain capability, reconciliation, schema/ACL, "
                        "and writer uniqueness must all be READY"
                    )
                wait_for_url(
                    "http://127.0.0.1:{}/readyz".format(BACKEND_PORT),
                    "backend readiness",
                    timeout_seconds=BACKEND_STARTUP_TIMEOUT_SECONDS,
                )
                wait_for_url(
                    "http://127.0.0.1:{}/".format(FRONTEND_PORT),
                    "frontend",
                    timeout_seconds=FRONTEND_STARTUP_TIMEOUT_SECONDS,
                )
                payload = {
                    **status,
                    "status": (
                        status["okx_runtime"]["status"]
                        if status["okx_runtime"].get("status")
                        in {"BLOCKED_OPENINGS", "RECOVERY_ONLY"}
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
            if (
                exc.okx_runtime_failure_stage
                in SAFE_OKX_RUNTIME_FAILURE_STAGES
                and exc.okx_runtime_failure_category
                in SAFE_OKX_RUNTIME_FAILURE_CATEGORIES
                and isinstance(exc.okx_runtime_failure_type, str)
                and exc.okx_runtime_failure_type
                in SAFE_OKX_RUNTIME_FAILURE_TYPES
            ):
                blocked["okx_runtime_failure_stage"] = (
                    exc.okx_runtime_failure_stage
                )
                blocked["okx_runtime_failure_type"] = (
                    exc.okx_runtime_failure_type
                )
                blocked["okx_runtime_failure_category"] = (
                    exc.okx_runtime_failure_category
                )
        emit(blocked, args.json)
        return 2


if __name__ == "__main__":
    sys.exit(main())
