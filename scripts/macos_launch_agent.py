#!/usr/bin/env python3
"""Install and manage the macOS LaunchAgent for the single local runtime."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence


LABEL = "com.he1met.freqtrade-ai.runtime"
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PYTHON = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
SUPERVISOR_SCRIPT = REPO_ROOT / "scripts" / "local_supervisor.py"
RUNTIME_SCRIPT = REPO_ROOT / "scripts" / "local_runtime.py"
LOG_DIR = REPO_ROOT / ".freqtrade-ai" / "launchd"
STDOUT_LOG = LOG_DIR / "supervisor.log"
STDERR_LOG = LOG_DIR / "supervisor-error.log"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "{}.plist".format(LABEL)


class LaunchAgentBlocked(Exception):
    """The LaunchAgent cannot be installed or safely managed."""


def launchd_target() -> str:
    return "gui/{}/{}".format(os.getuid(), LABEL)


def launchd_domain() -> str:
    return "gui/{}".format(os.getuid())


def run(
    command: Sequence[str],
    *,
    check: bool = False,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(REPO_ROOT),
        check=check,
        capture_output=capture_output,
        text=True,
    )


def resolve_freqtrade_binary() -> Path:
    if not BACKEND_PYTHON.is_file():
        raise LaunchAgentBlocked("backend virtualenv is missing; run `make bootstrap`")
    completed = subprocess.run(
        [str(BACKEND_PYTHON), str(RUNTIME_SCRIPT), "doctor", "--json"],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise LaunchAgentBlocked(
            completed.stderr.strip() or completed.stdout.strip() or "Freqtrade binary is unavailable"
        )
    try:
        payload = json.loads(completed.stdout)
        freqtrade = payload["freqtrade"]
        resolved_path = freqtrade["resolved_path"]
    except (KeyError, TypeError, json.JSONDecodeError):
        raise LaunchAgentBlocked("runtime doctor returned invalid binary evidence")
    if freqtrade.get("status") != "READY" or not resolved_path:
        raise LaunchAgentBlocked(
            str(freqtrade.get("reason") or "Freqtrade binary is unavailable")
        )
    binary = Path(resolved_path).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise LaunchAgentBlocked("resolved Freqtrade binary is not executable")
    return binary


def launchd_path() -> str:
    entries = [
        str(BACKEND_PYTHON.parent),
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    return ":".join(dict.fromkeys(entries))


def plist_payload(freqtrade_binary: Path) -> Dict[str, Any]:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(BACKEND_PYTHON), str(SUPERVISOR_SCRIPT)],
        "WorkingDirectory": str(REPO_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "LowPriorityIO": True,
        "StandardOutPath": str(STDOUT_LOG),
        "StandardErrorPath": str(STDERR_LOG),
        "EnvironmentVariables": {
            "PATH": launchd_path(),
            "PYTHONUNBUFFERED": "1",
            "FREQTRADE_BINARY": str(freqtrade_binary),
            "FREQTRADE_AI_SUPERVISOR_INTERVAL": "30",
        },
    }


def write_plist(payload: Dict[str, Any]) -> None:
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PLIST_PATH.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
    temporary.replace(PLIST_PATH)


def bootout() -> None:
    run(["launchctl", "bootout", launchd_target()])


def wait_until_running(timeout_seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        completed = run(["launchctl", "print", launchd_target()])
        if completed.returncode == 0 and "\n\tstate = running\n" in completed.stdout:
            return True
        time.sleep(0.25)
    return False


def install() -> Dict[str, Any]:
    if sys.platform != "darwin" or shutil.which("launchctl") is None:
        raise LaunchAgentBlocked("macOS launchctl is required")
    if not SUPERVISOR_SCRIPT.is_file() or not RUNTIME_SCRIPT.is_file():
        raise LaunchAgentBlocked("runtime supervisor files are missing")
    binary = resolve_freqtrade_binary()
    write_plist(plist_payload(binary))
    bootout()
    run(["launchctl", "enable", launchd_target()])
    completed = run(
        ["launchctl", "bootstrap", launchd_domain(), str(PLIST_PATH)]
    )
    if completed.returncode:
        raise LaunchAgentBlocked(
            completed.stderr.strip() or "launchctl bootstrap failed"
        )
    if not wait_until_running():
        bootout()
        error_tail = ""
        if STDERR_LOG.is_file():
            error_tail = STDERR_LOG.read_text(
                encoding="utf-8", errors="replace"
            )[-1200:]
        raise LaunchAgentBlocked(
            "LaunchAgent did not stay running. macOS may be blocking background access "
            "to this repository under ~/Documents. Grant the interpreter Full Disk Access "
            "or move the repository outside a protected folder."
            + ("\n" + error_tail if error_tail else "")
        )
    return {
        "status": "INSTALLED",
        "label": LABEL,
        "plist": str(PLIST_PATH),
        "logs": [str(STDOUT_LOG), str(STDERR_LOG)],
        "freqtrade_binary": str(binary),
        "trading": {"live": False, "dry_run": False, "real_orders": False},
    }


def status() -> Dict[str, Any]:
    completed = run(["launchctl", "print", launchd_target()])
    loaded = completed.returncode == 0
    state_match = re.search(r"(?m)^\s*state = ([^\n]+)$", completed.stdout)
    pid_match = re.search(r"(?m)^\s*pid = ([0-9]+)$", completed.stdout)
    exit_match = re.search(r"(?m)^\s*last exit code = ([^\n]+)$", completed.stdout)
    return {
        "status": "LOADED" if loaded else "NOT_LOADED",
        "label": LABEL,
        "plist_exists": PLIST_PATH.is_file(),
        "target": launchd_target(),
        "state": state_match.group(1).strip() if state_match else None,
        "pid": int(pid_match.group(1)) if pid_match else None,
        "last_exit_code": exit_match.group(1).strip() if exit_match else None,
        "reason": None if loaded else completed.stderr.strip(),
    }


def restart() -> Dict[str, Any]:
    completed = run(["launchctl", "kickstart", "-k", launchd_target()])
    if completed.returncode:
        raise LaunchAgentBlocked(completed.stderr.strip() or "launchctl kickstart failed")
    return {"status": "RESTARTED", "label": LABEL}


def logs(lines: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {"status": "READY", "logs": {}}
    for path in (STDOUT_LOG, STDERR_LOG):
        if path.is_file():
            result["logs"][str(path)] = path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[-lines:]
        else:
            result["logs"][str(path)] = []
    return result


def uninstall() -> Dict[str, Any]:
    bootout()
    runtime_down = run(
        [str(BACKEND_PYTHON), str(RUNTIME_SCRIPT), "down", "--json"]
    )
    PLIST_PATH.unlink(missing_ok=True)
    return {
        "status": "UNINSTALLED",
        "label": LABEL,
        "runtime_down_return_code": runtime_down.returncode,
    }


def emit(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("status={}".format(payload.get("status")))
    for key, value in payload.items():
        if key != "status":
            print("{}={}".format(key, value))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("install", "status", "logs", "restart", "uninstall"),
    )
    parser.add_argument("--lines", type=int, default=80)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "install":
            payload = install()
        elif args.command == "status":
            payload = status()
        elif args.command == "logs":
            payload = logs(max(1, args.lines))
        elif args.command == "restart":
            payload = restart()
        else:
            payload = uninstall()
        emit(payload, args.json)
        return 0 if payload.get("status") != "NOT_LOADED" else 2
    except LaunchAgentBlocked as exc:
        emit({"status": "BLOCKED", "reason": str(exc)}, args.json)
        return 2


if __name__ == "__main__":
    sys.exit(main())
