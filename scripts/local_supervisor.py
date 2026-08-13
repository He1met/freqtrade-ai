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

try:
    from scripts.local_supervisor_control import (
        CONTROL_MODE_MIGRATION_SUSPENDED,
        SupervisorControlBlocked,
        observe_supervisor_control,
    )
except ModuleNotFoundError:  # Direct ``python scripts/local_supervisor.py``.
    from local_supervisor_control import (  # type: ignore[no-redef]
        CONTROL_MODE_MIGRATION_SUSPENDED,
        SupervisorControlBlocked,
        observe_supervisor_control,
    )


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SCRIPT = REPO_ROOT / "scripts" / "local_runtime.py"
SUPERVISOR_RUNTIME_DIR = REPO_ROOT / ".freqtrade-ai" / "runtime"
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
STOP_EVENT = threading.Event()
LAST_CREDENTIAL_GENERATION: Optional[str] = None
LAST_FAILED_CREDENTIAL_GENERATION: Optional[str] = None
LAST_FAILED_CREDENTIAL_RETRY_AT = 0.0
CAPABILITY_UNAVAILABLE_TERMINAL = object()
LAST_TERMINAL_CREDENTIAL_GENERATION: Optional[object] = None


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


def safe_runtime_failure_details(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return only bounded, allowlisted startup diagnostics."""

    details: Dict[str, Any] = {}
    runtime_stage = payload.get("startup_stage")
    runtime_elapsed_ms = payload.get("startup_stage_elapsed_ms")
    command_elapsed_ms = payload.get("command_elapsed_ms")
    runtime_failure_stage = payload.get("okx_runtime_failure_stage")
    runtime_failure_type = payload.get("okx_runtime_failure_type")
    runtime_failure_category = payload.get("okx_runtime_failure_category")
    if (
        runtime_stage in SAFE_RUNTIME_STARTUP_STAGES
        and isinstance(runtime_elapsed_ms, int)
        and 0 <= runtime_elapsed_ms <= COMMAND_TIMEOUT_SECONDS * 1000
    ):
        details["runtime_stage"] = runtime_stage
        details["runtime_stage_elapsed_ms"] = runtime_elapsed_ms
    if (
        isinstance(command_elapsed_ms, int)
        and 0 <= command_elapsed_ms <= COMMAND_TIMEOUT_SECONDS * 1000
    ):
        details["runtime_command_elapsed_ms"] = command_elapsed_ms
    if (
        runtime_failure_stage in SAFE_OKX_RUNTIME_FAILURE_STAGES
        and runtime_failure_category in SAFE_OKX_RUNTIME_FAILURE_CATEGORIES
        and runtime_failure_type in SAFE_OKX_RUNTIME_FAILURE_TYPES
    ):
        details["okx_runtime_failure_stage"] = runtime_failure_stage
        details["okx_runtime_failure_category"] = runtime_failure_category
        details["okx_runtime_failure_type"] = runtime_failure_type
    return details


def is_terminal_credential_attestation_failure(
    payload: Dict[str, Any],
) -> bool:
    """Recognize only the existing fail-closed credential diagnostic."""

    return (
        payload.get("okx_runtime_failure_stage") == "read-attestation"
        and payload.get("okx_runtime_failure_category") == "ATTESTATION"
        and payload.get("okx_runtime_failure_type")
        == "OkxDemoCredentialsUnavailable"
    )


def record_terminal_credential_failure(
    generation: Optional[str],
    stage: str,
    payload: Dict[str, Any],
) -> bool:
    """Latch one generation or unavailable-capability episode safely."""

    global LAST_FAILED_CREDENTIAL_GENERATION
    global LAST_FAILED_CREDENTIAL_RETRY_AT
    global LAST_TERMINAL_CREDENTIAL_GENERATION

    LAST_FAILED_CREDENTIAL_GENERATION = generation
    LAST_FAILED_CREDENTIAL_RETRY_AT = 0.0
    LAST_TERMINAL_CREDENTIAL_GENERATION = (
        generation
        if generation is not None
        else CAPABILITY_UNAVAILABLE_TERMINAL
    )
    generation_status = (
        {"credential_generation_status": "UNAVAILABLE"}
        if generation is None
        else {}
    )
    emit(
        "runtime_recovery_blocked",
        stage=stage,
        terminal_for_credential_generation=True,
        **generation_status,
        **safe_runtime_failure_details(payload),
    )
    return False


def verify_or_recover(generation: Optional[str] = None) -> bool:
    verification = run_runtime("verify")
    if verification["return_code"] == 0:
        emit("runtime_verified", status=verification.get("status"))
        return True
    emit(
        "runtime_recovery_started",
        verify_status=verification.get("status"),
        **safe_runtime_failure_details(verification),
    )
    stopped = run_runtime("down")
    if any(service.get("status") == "BLOCKED" for service in stopped.get("services", [])):
        emit("runtime_recovery_blocked", stage="down", details=stopped)
        return False

    started = run_runtime("up")
    if started["return_code"] != 0:
        if is_terminal_credential_attestation_failure(started):
            return record_terminal_credential_failure(
                generation,
                "up",
                started,
            )
        emit(
            "runtime_recovery_blocked",
            stage="up",
            status=started.get("status"),
            **safe_runtime_failure_details(started),
        )
        return False

    final_verification = run_runtime("verify")
    recovered = final_verification["return_code"] == 0
    emit(
        "runtime_recovered" if recovered else "runtime_recovery_failed",
        status=final_verification.get("status"),
        **safe_runtime_failure_details(final_verification),
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
    global LAST_TERMINAL_CREDENTIAL_GENERATION

    def record_failure(
        stage: str,
        runtime_payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        global LAST_FAILED_CREDENTIAL_GENERATION
        global LAST_FAILED_CREDENTIAL_RETRY_AT
        if (
            stage == "credential-rotation-up"
            and runtime_payload is not None
            and is_terminal_credential_attestation_failure(runtime_payload)
        ):
            return record_terminal_credential_failure(
                generation,
                stage,
                runtime_payload,
            )
        LAST_FAILED_CREDENTIAL_GENERATION = generation
        LAST_FAILED_CREDENTIAL_RETRY_AT = (
            time.monotonic() + CREDENTIAL_RETRY_COOLDOWN_SECONDS
        )
        details: Dict[str, Any] = {
            "stage": stage,
            "retry_after_seconds": CREDENTIAL_RETRY_COOLDOWN_SECONDS,
        }
        if runtime_payload is not None:
            details.update(safe_runtime_failure_details(runtime_payload))
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
    LAST_TERMINAL_CREDENTIAL_GENERATION = None
    emit(
        "credential_rotation_completed",
        command_elapsed_ms={
            "down": stopped.get("command_elapsed_ms"),
            "up": started.get("command_elapsed_ms"),
            "verify": verification.get("command_elapsed_ms"),
        },
    )
    return True


def supervisor_automation_allowed(*, stage: str) -> bool:
    """Observe the local cutover fence before any automatic runtime action."""

    try:
        observation = observe_supervisor_control(
            SUPERVISOR_RUNTIME_DIR,
            trusted_root=REPO_ROOT,
        )
    except SupervisorControlBlocked:
        emit(
            "supervisor_maintenance_fail_closed",
            stage=stage,
            status="BLOCKED",
        )
        return False
    if observation.get("mode") == CONTROL_MODE_MIGRATION_SUSPENDED:
        emit(
            "supervisor_migration_suspended",
            stage=stage,
            status="MIGRATION_SUSPENDED",
            cutover_generation=observation.get("cutover_generation"),
            observed_receipt="DURABLE",
        )
        return False
    return True


def supervise_once() -> bool:
    global LAST_FAILED_CREDENTIAL_GENERATION
    global LAST_FAILED_CREDENTIAL_RETRY_AT
    global LAST_TERMINAL_CREDENTIAL_GENERATION
    if not supervisor_automation_allowed(stage="supervise"):
        return False
    generation = credential_generation()
    if generation is None:
        emit("credential_capability_unavailable")
        if LAST_TERMINAL_CREDENTIAL_GENERATION is not None:
            emit(
                "runtime_recovery_suppressed",
                status="BLOCKED",
                terminal_for_credential_generation=True,
                credential_generation_status="UNAVAILABLE",
                okx_runtime_failure_stage="read-attestation",
                okx_runtime_failure_category="ATTESTATION",
                okx_runtime_failure_type="OkxDemoCredentialsUnavailable",
            )
            return False
        frozen = run_runtime("supervisor-freeze-openings")
        if frozen.get("return_code") != 0:
            emit("runtime_recovery_blocked", stage="freeze-openings")
            return False
        return verify_or_recover()
    if generation == LAST_TERMINAL_CREDENTIAL_GENERATION:
        emit(
            "runtime_recovery_suppressed",
            status="BLOCKED",
            terminal_for_credential_generation=True,
            okx_runtime_failure_stage="read-attestation",
            okx_runtime_failure_category="ATTESTATION",
            okx_runtime_failure_type="OkxDemoCredentialsUnavailable",
        )
        return False
    if LAST_TERMINAL_CREDENTIAL_GENERATION is not None:
        LAST_TERMINAL_CREDENTIAL_GENERATION = None
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
    return verify_or_recover(generation)


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
    if not supervisor_automation_allowed(stage="shutdown"):
        emit(
            "supervisor_stopped",
            runtime_down="SUPPRESSED_BY_MAINTENANCE_FENCE",
        )
        return 0
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
