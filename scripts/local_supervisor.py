#!/usr/bin/env python3
"""Continuously verify and recover the one local Freqtrade AI runtime.

This supervisor never creates jobs or starts trading.  It delegates all state
changes to ``local_runtime.py``, so the canonical PostgreSQL, queue-idleness,
schema, localhost, and no-trading guards remain authoritative.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SCRIPT = REPO_ROOT / "scripts" / "local_runtime.py"
DEFAULT_INTERVAL_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 330
STOP_EVENT = threading.Event()
LAST_CREDENTIAL_GENERATION: Optional[str] = None


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(event: str, **details: Any) -> None:
    print(
        json.dumps(
            {"timestamp": timestamp(), "event": event, **details},
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


def interval_seconds() -> int:
    raw = os.environ.get("FREQTRADE_AI_SUPERVISOR_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return max(5, min(value, 3600))


def run_runtime(command: str, timeout: int = COMMAND_TIMEOUT_SECONDS) -> Dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(RUNTIME_SCRIPT), command, "--json"],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    payload: Dict[str, Any]
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {
            "status": "FAILED",
            "reason": "runtime command returned invalid JSON",
        }
    payload["return_code"] = completed.returncode
    if completed.stderr.strip():
        payload["stderr"] = "runtime command wrote redacted diagnostic output"
    return payload


def verify_or_recover() -> bool:
    verification = run_runtime("verify")
    if verification["return_code"] == 0:
        emit("runtime_verified", status=verification.get("status"))
        return True

    emit(
        "runtime_recovery_started",
        verify_status=verification.get("status"),
        verify_reason=verification.get("reason"),
    )
    stopped = run_runtime("down")
    if any(service.get("status") == "BLOCKED" for service in stopped.get("services", [])):
        emit("runtime_recovery_blocked", stage="down", details=stopped)
        return False

    started = run_runtime("up")
    if started["return_code"] != 0:
        emit(
            "runtime_recovery_blocked",
            stage="up",
            status=started.get("status"),
            reason=started.get("reason"),
        )
        return False

    final_verification = run_runtime("verify")
    recovered = final_verification["return_code"] == 0
    emit(
        "runtime_recovered" if recovered else "runtime_recovery_failed",
        status=final_verification.get("status"),
        reason=final_verification.get("reason"),
    )
    return recovered


def credential_generation() -> Optional[str]:
    """Read only operator-managed, non-secret Keychain generation metadata."""

    capability = run_runtime("supervisor-capability")
    generation = capability.get("_generation")
    if (
        capability.get("return_code") != 0
        or capability.get("status") != "READY"
        or not isinstance(generation, str)
        or not 1 <= len(generation) <= 64
    ):
        return None
    return generation


def controlled_credential_restart(generation: str) -> bool:
    """Commit generation only after the replacement child verifies."""

    global LAST_CREDENTIAL_GENERATION
    stopped = run_runtime("down")
    if any(
        service.get("status") == "BLOCKED"
        for service in stopped.get("services", [])
    ):
        emit("runtime_recovery_blocked", stage="credential-rotation-down")
        return False
    started = run_runtime("up")
    if started.get("return_code") != 0:
        emit("runtime_recovery_blocked", stage="credential-rotation-up")
        return False
    verification = run_runtime("verify")
    if verification.get("return_code") != 0:
        emit("runtime_recovery_blocked", stage="credential-rotation-verify")
        return False
    LAST_CREDENTIAL_GENERATION = generation
    return True


def supervise_once() -> bool:
    generation = credential_generation()
    if generation is None:
        emit("credential_capability_unavailable")
        frozen = run_runtime("supervisor-freeze-openings")
        if frozen.get("return_code") != 0:
            emit("runtime_recovery_blocked", stage="freeze-openings")
            return False
        return verify_or_recover()
    if (
        LAST_CREDENTIAL_GENERATION is None
        or generation != LAST_CREDENTIAL_GENERATION
    ):
        emit("credential_rotation_detected")
        return controlled_credential_restart(generation)
    run_runtime("supervisor-thaw-openings")
    return verify_or_recover()


def _stop(_signum: int, _frame: Optional[object]) -> None:
    STOP_EVENT.set()


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv:
        raise SystemExit("local_supervisor.py does not accept command arguments")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    interval = interval_seconds()
    emit(
        "supervisor_started",
        repo=str(REPO_ROOT),
        interval_seconds=interval,
        trading={"live": False, "dry_run": False, "real_orders": False},
    )
    while not STOP_EVENT.is_set():
        try:
            supervise_once()
        except subprocess.TimeoutExpired:
            emit("runtime_recovery_failed", reason="runtime command timed out")
        except Exception as exc:
            emit("supervisor_error", error_type=exc.__class__.__name__)
        STOP_EVENT.wait(interval)
    stopped = run_runtime("down")
    emit(
        "supervisor_stopped",
        runtime_down=(
            "CLEAN"
            if not any(
                service.get("status") == "BLOCKED"
                for service in stopped.get("services", [])
            )
            else "BLOCKED"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
