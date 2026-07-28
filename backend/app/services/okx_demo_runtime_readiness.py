from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any, Optional

from app.core.config import get_settings


READY_FILENAME = "okx-runtime.ready.json"
WRITER_LOCK_FILENAME = "okx-demo-order-writer.lock"
# The credential-bearing runtime refreshes readiness between complete,
# authenticated REST reconciliation cycles.  One bounded cycle may take
# longer than five seconds when all paginated exchange resources are read.
# PID liveness and the exclusive writer lock are still verified on every
# request, so this age budget does not keep a dead writer looking healthy.
MAX_HEARTBEAT_AGE = timedelta(seconds=90)


@dataclass(frozen=True)
class OkxDemoRuntimeReadiness:
    """Safe projection of the private #449 runtime evidence."""

    status: str
    target_ready: bool
    credentials_ready: bool
    writer_ready: bool
    observed_at: Optional[datetime]


def blocked_runtime_readiness() -> OkxDemoRuntimeReadiness:
    return OkxDemoRuntimeReadiness(
        status="BLOCKED",
        target_ready=False,
        credentials_ready=False,
        writer_ready=False,
        observed_at=None,
    )


def read_okx_demo_runtime_readiness(
    *,
    now: Optional[datetime] = None,
    runtime_dir: Optional[Path] = None,
) -> OkxDemoRuntimeReadiness:
    """Verify the current private heartbeat without returning operational details."""

    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expected_runtime_dir = (
        get_settings().canonical_repo_root.expanduser().resolve()
        / ".freqtrade-ai"
        / "runtime"
    )
    candidate = (runtime_dir or expected_runtime_dir).expanduser().resolve()
    if runtime_dir is None and candidate != expected_runtime_dir:
        return blocked_runtime_readiness()

    ready = _read_private_json(candidate / READY_FILENAME)
    if ready is None:
        return blocked_runtime_readiness()
    payload, modified_at = ready
    if set(payload) != {
        "status",
        "execution_target",
        "adapter",
        "reconciliation",
        "writer",
        "pid",
    }:
        return blocked_runtime_readiness()
    runtime_status = payload.get("status")
    reconciliation = payload.get("reconciliation")
    valid_state = (
        runtime_status == "READY"
        and reconciliation in {"RECONCILED", "RECOVERED"}
    ) or (
        runtime_status == "BLOCKED_OPENINGS"
        and reconciliation in {"DRIFTED", "STALE", "UNKNOWN"}
    )
    process_id = payload.get("pid")
    heartbeat_age = observed_now - modified_at
    if (
        not valid_state
        or payload.get("execution_target") != "OKX_DEMO"
        or payload.get("adapter") != "ATTESTED"
        or payload.get("writer") != "UNIQUE"
        or not isinstance(process_id, int)
        or isinstance(process_id, bool)
        or process_id <= 0
        or heartbeat_age < -timedelta(seconds=2)
        or heartbeat_age > MAX_HEARTBEAT_AGE
        or not _process_is_running(process_id)
        or not _writer_lock_is_held_by(
            candidate / WRITER_LOCK_FILENAME,
            process_id,
        )
    ):
        return blocked_runtime_readiness()

    return OkxDemoRuntimeReadiness(
        status=runtime_status,
        target_ready=True,
        credentials_ready=runtime_status == "READY",
        writer_ready=True,
        observed_at=modified_at,
    )


def _read_private_json(path: Path) -> Optional[tuple[dict[str, Any], datetime]]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            return None
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return None
        return payload, datetime.fromtimestamp(metadata.st_mtime, timezone.utc)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _process_is_running(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
        return True
    except (OSError, ValueError):
        return False


def _writer_lock_is_held_by(path: Path, process_id: int) -> bool:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            return False
        descriptor = os.open(
            path,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        handle = os.fdopen(descriptor, "r+")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                handle.seek(0)
                return int(handle.read().strip()) == process_id
            except ValueError:
                return False
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    finally:
        handle.close()
