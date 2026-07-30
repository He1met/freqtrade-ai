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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SCRIPT = REPO_ROOT / "scripts" / "local_runtime.py"
DEFAULT_INTERVAL_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 900
CREDENTIAL_RETRY_COOLDOWN_SECONDS = 300
SAFE_RUNTIME_STARTUP_STAGES = frozenset(
    {
        "backend-readiness",
        "worker-stability",
        "frontend-readiness",
        "okx-runtime-readiness",
    }
)
STOP_EVENT = threading.Event()
LAST_CREDENTIAL_GENERATION: Optional[str] = None
LAST_FAILED_CREDENTIAL_GENERATION: Optional[str] = None
LAST_FAILED_CREDENTIAL_RETRY_AT = 0.0


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
    started_at = time.monotonic()
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
    payload["command_elapsed_ms"] = int(
        (time.monotonic() - started_at) * 1000
    )
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
    global LAST_FAILED_CREDENTIAL_GENERATION
    global LAST_FAILED_CREDENTIAL_RETRY_AT

    def record_failure(
        stage: str,
        runtime_payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        global LAST_FAILED_CREDENTIAL_GENERATION
        global LAST_FAILED_CREDENTIAL_RETRY_AT
        LAST_FAILED_CREDENTIAL_GENERATION = generation
        LAST_FAILED_CREDENTIAL_RETRY_AT = (
            time.monotonic() + CREDENTIAL_RETRY_COOLDOWN_SECONDS
        )
        details: Dict[str, Any] = {
            "stage": stage,
            "retry_after_seconds": CREDENTIAL_RETRY_COOLDOWN_SECONDS,
        }
        if runtime_payload is not None:
            runtime_stage = runtime_payload.get("startup_stage")
            runtime_elapsed_ms = runtime_payload.get(
                "startup_stage_elapsed_ms"
            )
            command_elapsed_ms = runtime_payload.get("command_elapsed_ms")
            if (
                runtime_stage in SAFE_RUNTIME_STARTUP_STAGES
                and isinstance(runtime_elapsed_ms, int)
                and 0 <= runtime_elapsed_ms <= COMMAND_TIMEOUT_SECONDS * 1000
            ):
                details["runtime_stage"] = runtime_stage
                details["runtime_stage_elapsed_ms"] = runtime_elapsed_ms
            if (
                isinstance(command_elapsed_ms, int)
                and 0
                <= command_elapsed_ms
                <= COMMAND_TIMEOUT_SECONDS * 1000
            ):
                details["runtime_command_elapsed_ms"] = (
                    command_elapsed_ms
                )
        emit("runtime_recovery_blocked", **details)
        return False

    stopped = run_runtime("down")
    if any(
        service.get("status") == "BLOCKED"
        for service in stopped.get("services", [])
    ):
        return record_failure("credential-rotation-down", stopped)
    started = run_runtime("up")
    if started.get("return_code") != 0:
        return record_failure("credential-rotation-up", started)
    verification = run_runtime("verify")
    if verification.get("return_code") != 0:
        return record_failure("credential-rotation-verify", verification)
    LAST_CREDENTIAL_GENERATION = generation
    LAST_FAILED_CREDENTIAL_GENERATION = None
    LAST_FAILED_CREDENTIAL_RETRY_AT = 0.0
    emit(
        "credential_rotation_completed",
        command_elapsed_ms={
            "down": stopped.get("command_elapsed_ms"),
            "up": started.get("command_elapsed_ms"),
            "verify": verification.get("command_elapsed_ms"),
        },
    )
    return True


def supervise_once() -> bool:
    global LAST_FAILED_CREDENTIAL_GENERATION
    global LAST_FAILED_CREDENTIAL_RETRY_AT
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
        if (
            generation == LAST_FAILED_CREDENTIAL_GENERATION
            and time.monotonic() < LAST_FAILED_CREDENTIAL_RETRY_AT
        ):
            emit(
                "credential_rotation_backoff",
                retry_after_seconds=max(
                    1,
                    int(LAST_FAILED_CREDENTIAL_RETRY_AT - time.monotonic()),
                ),
            )
            return False
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
