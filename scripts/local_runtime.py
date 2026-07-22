#!/usr/bin/env python3
"""Safe, repeatable local demo/dev runtime manager for Freqtrade AI.

This command manages the FastAPI, DB-backed worker, and Vite development
processes.  The worker may execute explicitly authorized queued research jobs,
but this runtime manager never connects to an exchange, starts dry-run/live
trading, or reads provider credentials.  Runtime state stays in
``.freqtrade-ai/`` so the demo SQLite database is persistent and never defaults
to ``/tmp``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_DIR = REPO_ROOT / ".freqtrade-ai" / "runtime"
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


def runtime_dir(raw_path: Optional[str]) -> Path:
    candidate = Path(raw_path).expanduser() if raw_path else DEFAULT_RUNTIME_DIR
    resolved = candidate.resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise RuntimeBlocked("runtime directory must stay inside this repository") from exc
    return resolved


def demo_database_url(state_dir: Path) -> str:
    return "sqlite+pysqlite:///{}".format(state_dir / "demo.sqlite3")


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


def dev_database_url() -> str:
    """Read only the explicit dev variable, never an inherited DATABASE_URL."""

    value = os.environ.get("FREQTRADE_AI_DEV_DATABASE_URL", "").strip()
    if not value:
        raise RuntimeBlocked(
            "FREQTRADE_AI_DEV_DATABASE_URL is required for dev mode; inherited DATABASE_URL is ignored"
        )
    parsed = urlsplit(value)
    if not parsed.scheme.startswith("postgresql") or not parsed.hostname:
        raise RuntimeBlocked("FREQTRADE_AI_DEV_DATABASE_URL must be a PostgreSQL SQLAlchemy URL")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeBlocked("dev mode only accepts a localhost PostgreSQL target")
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


def clean_environment(database_url: str, mode: str) -> Dict[str, str]:
    environment = os.environ.copy()
    # The selected mode owns its DB target; a shell's DATABASE_URL cannot leak in.
    environment.pop("DATABASE_URL", None)
    environment.update(
        {
            "DATABASE_URL": database_url,
            "APP_ENV": mode,
            "VITE_ENABLE_DEV_FIXTURES": "false",
            "VITE_FREQUI_URL": "",
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


def initialise_demo_database(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    database_url = demo_database_url(state_dir)
    code = "from app.models import Base; from app.db.session import create_database_engine; Base.metadata.create_all(create_database_engine())"
    environment = clean_environment(database_url, "demo")
    environment["PYTHONPATH"] = str(REPO_ROOT / "backend")
    run_checked([str(backend_python()), "-c", code], cwd=REPO_ROOT, environment=environment)


def verify_demo_database(state_dir: Path) -> Dict[str, Any]:
    database_path = state_dir / "demo.sqlite3"
    if not database_path.is_file():
        raise RuntimeBlocked("demo database is missing; run `make demo-up`")
    try:
        with sqlite3.connect(str(database_path)) as connection:
            table_count = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
    except sqlite3.Error as exc:
        raise RuntimeBlocked("demo database is unreadable: {}".format(exc.__class__.__name__)) from exc
    if not table_count:
        raise RuntimeBlocked("demo database has no application tables; run `make demo-up`")
    return {"path": str(database_path), "tables": table_count}


def doctor(mode: str, state_dir: Path) -> Dict[str, Any]:
    checks = {
        "python3": command_exists("python3"),
        "node": command_exists("node"),
        "npm": command_exists("npm"),
        "backend_virtualenv": (REPO_ROOT / "backend" / ".venv" / "bin" / "python").is_file(),
        "frontend_dependencies": (REPO_ROOT / "frontend" / "node_modules" / ".bin" / "vite").is_file(),
        "backend_port_available": port_available(BACKEND_PORT),
        "frontend_port_available": port_available(FRONTEND_PORT),
        "freqtrade_binary": command_exists("freqtrade"),
        "market_data_directory": (REPO_ROOT / "user_data" / "data").is_dir(),
        "live_trading": False,
        "dry_run_trading": False,
    }
    result: Dict[str, Any] = {"mode": mode, "runtime_dir": str(state_dir), "checks": checks}
    if mode == "demo":
        result["database"] = {"kind": "sqlite", "identity": redact_database_url(demo_database_url(state_dir))}
        try:
            result["schema"] = {"status": "READY", **verify_demo_database(state_dir)}
        except RuntimeBlocked as exc:
            result["schema"] = {"status": "BLOCKED", "reason": str(exc)}
    else:
        try:
            database_url = dev_database_url()
            result["database"] = {"kind": "postgresql", "identity": redact_database_url(database_url)}
            try:
                ensure_dev_schema(database_url)
                result["schema"] = {"status": "READY"}
            except RuntimeBlocked as exc:
                result["schema"] = {"status": "BLOCKED", "reason": str(exc)}
        except RuntimeBlocked as exc:
            result["database"] = {"kind": "postgresql", "status": "BLOCKED", "reason": str(exc)}
    return result


def bootstrap() -> Dict[str, Any]:
    run_checked([sys.executable, "-m", "venv", ".venv"], cwd=REPO_ROOT / "backend")
    run_checked(
        [str(backend_python()), "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=REPO_ROOT / "backend",
    )
    run_checked(["npm", "ci"], cwd=REPO_ROOT / "frontend")
    return {"status": "READY", "backend_virtualenv": True, "frontend_dependencies": True}


def ensure_dev_schema(database_url: str) -> None:
    environment = clean_environment(database_url, "dev")
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
            "PostgreSQL schema verification failed; run `make db-init` on the explicit local dev database"
        )


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


def start(mode: str, state_dir: Path) -> Dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    backend_python()
    frontend_vite()
    if not port_available(BACKEND_PORT) or not port_available(FRONTEND_PORT):
        raise RuntimeBlocked("port 8000 or 5173 is already in use; run `make status` before starting")
    if mode == "demo":
        initialise_demo_database(state_dir)
        database_url = demo_database_url(state_dir)
        database = verify_demo_database(state_dir)
    else:
        database_url = dev_database_url()
        ensure_dev_schema(database_url)
        database = {"identity": redact_database_url(database_url), "schema": "verified"}
    environment = clean_environment(database_url, mode)
    try:
        start_service(
            "backend",
            [str(backend_python()), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
            cwd=REPO_ROOT / "backend",
            environment=environment,
            state_dir=state_dir,
        )
        wait_for_url("http://127.0.0.1:{}/health".format(BACKEND_PORT), "backend")
        start_service(
            "worker",
            [
                str(backend_python()),
                "-m",
                "app.workers.deepseek_backtest_worker",
            ],
            cwd=REPO_ROOT / "backend",
            environment=environment,
            state_dir=state_dir,
        )
        wait_for_process(state_dir, "worker")
        start_service(
            "frontend",
            [str(frontend_vite()), "--host", "127.0.0.1", "--port", str(FRONTEND_PORT)],
            cwd=REPO_ROOT / "frontend",
            environment=environment,
            state_dir=state_dir,
        )
        wait_for_url("http://127.0.0.1:{}/".format(FRONTEND_PORT), "frontend")
    except RuntimeBlocked:
        stop_all(state_dir)
        raise
    return {
        "status": "RUNNING",
        "mode": mode,
        "database": database,
        "backend_url": "http://127.0.0.1:{}".format(BACKEND_PORT),
        "frontend_url": "http://127.0.0.1:{}".format(FRONTEND_PORT),
        "trading": {"live": False, "dry_run": False, "real_orders": False},
    }


def current_status(mode: str, state_dir: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "mode": mode,
        "runtime_dir": str(state_dir),
        "services": [
            process_status(state_dir, service)
            for service in ("backend", "worker", "frontend")
        ],
        "trading": {"live": False, "dry_run": False, "real_orders": False},
    }
    try:
        if mode == "demo":
            result["database"] = {"kind": "sqlite", **verify_demo_database(state_dir)}
        else:
            result["database"] = {"kind": "postgresql", "identity": redact_database_url(dev_database_url())}
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
    parser.add_argument("command", choices=("doctor", "bootstrap", "demo-up", "dev-up", "status", "down", "logs", "verify"))
    parser.add_argument("--mode", choices=("demo", "dev"), default="demo")
    parser.add_argument("--runtime-dir")
    parser.add_argument("--lines", type=int, default=80)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        state_dir = runtime_dir(args.runtime_dir)
        if args.command == "doctor":
            payload = doctor(args.mode, state_dir)
        elif args.command == "bootstrap":
            payload = bootstrap()
        elif args.command == "demo-up":
            payload = start("demo", state_dir)
        elif args.command == "dev-up":
            payload = start("dev", state_dir)
        elif args.command == "status":
            payload = current_status(args.mode, state_dir)
        elif args.command == "down":
            payload = stop_all(state_dir)
        elif args.command == "logs":
            payload = recent_logs(state_dir, max(1, args.lines))
        else:
            status = current_status(args.mode, state_dir)
            running = all(service["running"] for service in status["services"])
            if args.mode == "demo":
                verify_demo_database(state_dir)
            else:
                ensure_dev_schema(dev_database_url())
            if not running:
                raise RuntimeBlocked(
                    "backend, worker, and frontend must all be running before verification"
                )
            wait_for_url("http://127.0.0.1:{}/health".format(BACKEND_PORT), "backend")
            wait_for_url("http://127.0.0.1:{}/".format(FRONTEND_PORT), "frontend")
            payload = {"status": "VERIFIED", **status}
        emit(payload, args.json)
        return 0
    except RuntimeBlocked as exc:
        emit({"status": "BLOCKED", "reason": str(exc)}, args.json)
        return 2


if __name__ == "__main__":
    sys.exit(main())
