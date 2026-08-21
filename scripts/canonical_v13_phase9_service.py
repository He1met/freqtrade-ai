"""Manage isolated canonical V1.3 Phase 9 macOS service supervisors.

The command is intentionally two phase: ``prepare`` writes a secret-free plist and
frozen plan, while ``confirm`` performs the first ``launchctl bootstrap``.  The
order writer is disabled unless a canary plan explicitly enables it.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import signal
import subprocess
import sys
import time
from typing import Sequence
from uuid import UUID


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.canonical_v13.phase9_runtime_supervisor import (  # noqa: E402
    CanonicalPhase9SupervisorBlocked,
    Phase9LaunchPlan,
    Phase9Lease,
    RuntimeWorkerSupervisorPort,
    build_launch_plan,
    build_lifecycle_receipt,
    claim_lease,
    heartbeat_lease,
    release_lease,
    validate_supervised_worker_receipt,
    verify_launch_plan,
)
from app.canonical_v13.phase9_topology import PHASE9_SERVICE_SPECS  # noqa: E402


BACKEND_PYTHON = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
SCRIPT_PATH = Path(__file__).resolve()
SUPPORT_ROOT = (
    Path.home() / "Library" / "Application Support" / "FreqtradeAiV13" / "phase9"
)
LAUNCH_AGENT_ROOT = Path.home() / "Library" / "LaunchAgents"
LOG_ROOT = Path.home() / "Library" / "Logs" / "FreqtradeAiV13"
HEARTBEAT_SECONDS = 10
LEASE_TTL_SECONDS = 35
_STOP = False


class FileLeasePort:
    """Atomic one-file lease port; holder material is never persisted."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, service_key: str) -> Path:
        return self.root / f"{service_key}.lease.json"

    def read(self, service_key: str) -> Phase9Lease | None:
        path = self._path(service_key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as exc:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_LEASE_CORRUPT", service_key
            ) from exc
        try:
            return Phase9Lease(
                service_key=str(payload["service_key"]),
                generation=int(payload["generation"]),
                plan_digest=str(payload["plan_digest"]),
                release_digest=str(payload["release_digest"]),
                deployment_id=(
                    UUID(str(payload["deployment_id"]))
                    if payload.get("deployment_id")
                    else None
                ),
                deployment_capability_digest=(
                    str(payload["deployment_capability_digest"])
                    if payload.get("deployment_capability_digest")
                    else None
                ),
                image_digest=(
                    str(payload["image_digest"])
                    if payload.get("image_digest")
                    else None
                ),
                holder_token_digest=str(payload["holder_token_digest"]),
                pid=int(payload["pid"]),
                acquired_at=datetime.fromisoformat(payload["acquired_at"]),
                heartbeat_at=datetime.fromisoformat(payload["heartbeat_at"]),
                expires_at=datetime.fromisoformat(payload["expires_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_LEASE_CORRUPT", service_key
            ) from exc

    def claim(self, lease: Phase9Lease) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(lease.service_key)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_LEASE_RACE", lease.service_key
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                _lease_payload(lease), handle, sort_keys=True, separators=(",", ":")
            )
            handle.write("\n")

    def replace(self, expected: Phase9Lease, lease: Phase9Lease) -> None:
        if self.read(expected.service_key) != expected:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_LEASE_FENCED", expected.service_key
            )
        _atomic_json(
            self._path(expected.service_key), _lease_payload(lease), mode=0o600
        )

    def release(self, expected: Phase9Lease) -> None:
        if self.read(expected.service_key) != expected:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_LEASE_FENCED", expected.service_key
            )
        self._path(expected.service_key).unlink()


class UnixProcessProbe:
    def is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _require_release_checkout() -> str:
    if ".codex/worktrees" in str(REPO_ROOT):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_CANONICAL_RELEASE_CHECKOUT_REQUIRED", str(REPO_ROOT)
        )
    results = {
        "status": _run(["git", "status", "--porcelain"]),
        "head": _run(["git", "rev-parse", "HEAD"]),
        "main": _run(["git", "rev-parse", "origin/main"]),
    }
    if (
        any(result.returncode != 0 for result in results.values())
        or results["status"].stdout.strip()
        or results["head"].stdout.strip() != results["main"].stdout.strip()
        or not BACKEND_PYTHON.is_file()
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_CANONICAL_RELEASE_CHECKOUT_REQUIRED",
            "release is not clean/exact-main",
        )
    return sha256(
        f"canonical-v13-release:{results['head'].stdout.strip()}".encode("ascii")
    ).hexdigest()


def _state_path(service_key: str) -> Path:
    return SUPPORT_ROOT / f"{service_key}.state.json"


def _receipt_path(service_key: str) -> Path:
    return SUPPORT_ROOT / f"{service_key}.receipts.jsonl"


def _plist_path(service_key: str) -> Path:
    return (
        LAUNCH_AGENT_ROOT
        / f"{PHASE9_SERVICE_SPECS[service_key].launch_agent_label}.plist"
    )


def _launchctl_target(service_key: str) -> str:
    return f"gui/{os.getuid()}/{PHASE9_SERVICE_SPECS[service_key].launch_agent_label}"


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _atomic_json(path: Path, payload: dict[str, object], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _lease_payload(lease: Phase9Lease) -> dict[str, object]:
    return {
        **asdict(lease),
        "deployment_id": str(lease.deployment_id) if lease.deployment_id else None,
        "acquired_at": lease.acquired_at.isoformat(),
        "heartbeat_at": lease.heartbeat_at.isoformat(),
        "expires_at": lease.expires_at.isoformat(),
    }


def _plan_payload(plan: Phase9LaunchPlan) -> dict[str, object]:
    return {
        **asdict(plan),
        "plan_id": str(plan.plan_id),
        "deployment_id": str(plan.deployment_id) if plan.deployment_id else None,
        "prepared_at": plan.prepared_at.isoformat(),
    }


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _load_state(service_key: str) -> dict[str, object] | None:
    try:
        payload = json.loads(_state_path(service_key).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_STATE_CORRUPT", service_key
        ) from exc
    if not isinstance(payload, dict):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_STATE_CORRUPT", service_key
        )
    return payload


def _load_plan(service_key: str) -> tuple[Phase9LaunchPlan, dict[str, object]]:
    state = _load_state(service_key)
    if state is None or not isinstance(state.get("plan"), dict):
        raise CanonicalPhase9SupervisorBlocked("BLOCKED_PHASE9_PLAN_UNSET", service_key)
    payload = state["plan"]
    try:
        plan = Phase9LaunchPlan(
            plan_id=UUID(str(payload["plan_id"])),
            service_key=str(payload["service_key"]),
            stage=str(payload["stage"]),
            launch_agent_label=str(payload["launch_agent_label"]),
            process_identity=str(payload["process_identity"]),
            postgres_capability=str(payload["postgres_capability"]),
            deployment_id=(
                UUID(str(payload["deployment_id"]))
                if payload.get("deployment_id")
                else None
            ),
            deployment_capability_digest=(
                str(payload["deployment_capability_digest"])
                if payload.get("deployment_capability_digest")
                else None
            ),
            image_digest=(
                str(payload["image_digest"]) if payload.get("image_digest") else None
            ),
            release_digest=str(payload["release_digest"]),
            generation=int(payload["generation"]),
            prepared_at=datetime.fromisoformat(str(payload["prepared_at"])),
            demo_only=payload["demo_only"] is True,
            allow_real_funds=payload["allow_real_funds"] is True,
            order_writer_enabled=payload["order_writer_enabled"] is True,
            plan_digest=str(payload["plan_digest"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_STATE_CORRUPT", service_key
        ) from exc
    verify_launch_plan(plan)
    return plan, state


def _append_receipt(receipt: object) -> None:
    payload = _json_safe(asdict(receipt))  # type: ignore[arg-type]
    if not isinstance(payload, dict):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RECEIPT_SERIALIZATION", "receipt is not a mapping"
        )
    path = _receipt_path(str(payload["service_key"]))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        json.dump(
            payload, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def plist_payload(plan: Phase9LaunchPlan) -> dict[str, object]:
    """Return a LaunchAgent definition containing no credential or DSN material."""

    verify_launch_plan(plan)
    return {
        "Label": plan.launch_agent_label,
        "ProgramArguments": [
            str(BACKEND_PYTHON),
            str(SCRIPT_PATH),
            "supervise",
            "--service",
            plan.service_key,
            "--plan-digest",
            plan.plan_digest,
        ],
        "WorkingDirectory": str(REPO_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(LOG_ROOT / f"phase9-{plan.service_key}.log"),
        "StandardErrorPath": str(LOG_ROOT / f"phase9-{plan.service_key}-error.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
            "FREQTRADE_AI_DISABLE_ENV_FILE": "1",
            "FREQTRADE_AI_CANONICAL_PHASE9_STAGE": plan.stage,
        },
    }


def prepare(
    service_key: str,
    stage: str,
    *,
    release_digest: str,
    deployment_id: UUID | None,
    deployment_capability_digest: str | None,
    image_digest: str | None,
    enable_order_writer: bool,
) -> dict[str, object]:
    observed_release_digest = _require_release_checkout()
    if observed_release_digest != release_digest:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RELEASE_DRIFT",
            "prepared release digest does not match clean exact-main HEAD",
        )
    prior = _load_state(service_key)
    generation = 1
    if prior and isinstance(prior.get("plan"), dict):
        existing_plan, _existing_state = _load_plan(service_key)
        if prior.get("status") == "PREPARED":
            if (
                existing_plan.stage != stage
                or existing_plan.release_digest != release_digest
                or existing_plan.deployment_id != deployment_id
                or existing_plan.deployment_capability_digest
                != deployment_capability_digest
                or existing_plan.image_digest != image_digest
                or existing_plan.order_writer_enabled != enable_order_writer
            ):
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_PREPARED_PLAN_EXISTS",
                    "confirm or explicitly stop the frozen plan before replacing it",
                )
            return {
                "status": "PREPARED",
                "service": service_key,
                "stage": stage,
                "generation": existing_plan.generation,
                "plan_digest": existing_plan.plan_digest,
                "receipt_digest": prior.get("prepare_receipt_digest"),
                "repeat_noop": True,
            }
        if prior.get("status") in {"CONFIRMED", "RUNNING"}:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_ACTIVE_PLAN_EXISTS",
                "stop the confirmed generation before preparing another one",
            )
        generation = existing_plan.generation + 1
    plan = build_launch_plan(
        service_key=service_key,
        stage=stage,
        generation=generation,
        prepared_at=_now(),
        release_digest=release_digest,
        deployment_id=deployment_id,
        deployment_capability_digest=deployment_capability_digest,
        image_digest=image_digest,
        order_writer_enabled=enable_order_writer,
    )
    if FileLeasePort(SUPPORT_ROOT).read(service_key) is not None:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_LEASE_HELD", "stop the current generation before prepare"
        )
    LAUNCH_AGENT_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = _plist_path(service_key).with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(plist_payload(plan), handle, sort_keys=True)
    temporary.replace(_plist_path(service_key))
    receipt = build_lifecycle_receipt(
        service_key=service_key,
        action="PREPARE",
        status="PREPARED",
        generation=generation,
        observed_at=_now(),
        plan_digest=plan.plan_digest,
        details={
            "label": plan.launch_agent_label,
            "stage": stage,
            "plist_secret_count": 0,
        },
    )
    state = {
        "status": "PREPARED",
        "plan": _plan_payload(plan),
        "confirmed_at": None,
        "prepare_receipt_digest": receipt.receipt_digest,
    }
    _atomic_json(_state_path(service_key), state)
    _append_receipt(receipt)
    return {
        "status": "PREPARED",
        "service": service_key,
        "stage": stage,
        "generation": generation,
        "plan_digest": plan.plan_digest,
        "receipt_digest": receipt.receipt_digest,
        "repeat_noop": False,
    }


def confirm(service_key: str, plan_digest: str) -> dict[str, object]:
    _require_release_checkout()
    if shutil.which("launchctl") is None:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_LAUNCHCTL_REQUIRED", "launchctl"
        )
    plan, state = _load_plan(service_key)
    if plan.plan_digest != plan_digest:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_CONFIRMATION_DRIFT", "prepared plan digest/status mismatch"
        )
    if state.get("status") in {"CONFIRMED", "RUNNING"}:
        return {
            "status": "CONFIRMED",
            "service": service_key,
            "generation": plan.generation,
            "plan_digest": plan.plan_digest,
            "receipt_digest": state.get("confirm_receipt_digest"),
            "repeat_noop": True,
        }
    if state.get("status") != "PREPARED":
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_CONFIRMATION_DRIFT", "prepared plan digest/status mismatch"
        )
    confirmed_at = _now()
    receipt = build_lifecycle_receipt(
        service_key=service_key,
        action="CONFIRM",
        status="CONFIRMED",
        generation=plan.generation,
        observed_at=confirmed_at,
        plan_digest=plan.plan_digest,
        details={"label": plan.launch_agent_label, "stage": plan.stage},
    )
    confirmed_state = {
        **state,
        "status": "CONFIRMED",
        "confirmed_at": confirmed_at.isoformat(),
        "confirm_receipt_digest": receipt.receipt_digest,
    }
    # Publish the exact confirmation before bootstrap so RunAtLoad can never
    # observe a PREPARED-only plan.  A failed bootstrap restores PREPARED.
    _atomic_json(_state_path(service_key), confirmed_state)
    _run(["launchctl", "bootout", _launchctl_target(service_key)])
    completed = _run(
        ["launchctl", "bootstrap", _launchctl_domain(), str(_plist_path(service_key))]
    )
    if completed.returncode != 0:
        _atomic_json(_state_path(service_key), state)
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_BOOTSTRAP_FAILED", f"service={service_key}"
        )
    _append_receipt(receipt)
    return {
        "status": "CONFIRMED",
        "service": service_key,
        "generation": plan.generation,
        "plan_digest": plan.plan_digest,
        "receipt_digest": receipt.receipt_digest,
        "repeat_noop": False,
    }


def status(service_key: str) -> dict[str, object]:
    state = _load_state(service_key)
    lease = FileLeasePort(SUPPORT_ROOT).read(service_key)
    loaded = (
        _run(["launchctl", "print", _launchctl_target(service_key)]).returncode == 0
    )
    lease_fresh = bool(lease and lease.expires_at > _now())
    holder_alive = bool(lease and UnixProcessProbe().is_alive(lease.pid))
    return {
        "status": "RUNNING" if loaded and lease_fresh and holder_alive else "BLOCKED",
        "service": service_key,
        "loaded": loaded,
        "lease_present": lease is not None,
        "lease_fresh": lease_fresh,
        "holder_alive": holder_alive,
        "generation": lease.generation if lease else None,
        "heartbeat_at": lease.heartbeat_at.isoformat() if lease else None,
        "plan_status": state.get("status") if state else "UNSET",
        "order_writer_enabled": bool(
            state
            and isinstance(state.get("plan"), dict)
            and state["plan"].get("order_writer_enabled") is True
        ),
    }


def stop(service_key: str) -> dict[str, object]:
    _require_release_checkout()
    plan, state = _load_plan(service_key)
    if state.get("status") == "STOPPED":
        return {
            "status": "STOPPED",
            "service": service_key,
            "receipt_digest": state.get("stop_receipt_digest"),
            "repeat_noop": True,
        }
    completed = _run(["launchctl", "bootout", _launchctl_target(service_key)])
    lease_port = FileLeasePort(SUPPORT_ROOT)
    deadline = time.monotonic() + 10
    while lease_port.read(service_key) is not None and time.monotonic() < deadline:
        time.sleep(0.1)
    if lease_port.read(service_key) is not None:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_STOP_LEASE_HELD", service_key
        )
    if completed.returncode not in {0, 3, 113}:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_BOOTOUT_FAILED", service_key
        )
    observed_at = _now()
    receipt = build_lifecycle_receipt(
        service_key=service_key,
        action="STOP",
        status="STOPPED",
        generation=plan.generation,
        observed_at=observed_at,
        plan_digest=plan.plan_digest,
        details={"label": plan.launch_agent_label},
    )
    _atomic_json(
        _state_path(service_key),
        {
            **state,
            "status": "STOPPED",
            "stopped_at": observed_at.isoformat(),
            "stop_receipt_digest": receipt.receipt_digest,
        },
    )
    _append_receipt(receipt)
    return {
        "status": "STOPPED",
        "service": service_key,
        "receipt_digest": receipt.receipt_digest,
        "repeat_noop": False,
    }


def restart(service_key: str, plan_digest: str) -> dict[str, object]:
    _require_release_checkout()
    plan, state = _load_plan(service_key)
    if (
        state.get("status") not in {"CONFIRMED", "RUNNING"}
        or plan.plan_digest != plan_digest
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RESTART_UNCONFIRMED", service_key
        )
    kicked = _run(["launchctl", "kickstart", "-k", _launchctl_target(service_key)])
    if kicked.returncode != 0:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RESTART_FAILED", service_key
        )
    receipt = build_lifecycle_receipt(
        service_key=service_key,
        action="RESTART",
        status="CONFIRMED",
        generation=plan.generation,
        observed_at=_now(),
        plan_digest=plan.plan_digest,
        details={"label": plan.launch_agent_label},
    )
    _append_receipt(receipt)
    return {
        "status": "RESTARTED",
        "service": service_key,
        "receipt_digest": receipt.receipt_digest,
    }


def recover(service_key: str) -> dict[str, object]:
    _require_release_checkout()
    plan, state = _load_plan(service_key)
    lease_port = FileLeasePort(SUPPORT_ROOT)
    lease = lease_port.read(service_key)
    orphan_cleaned = False
    now = _now()
    if (
        lease is not None
        and lease.expires_at <= now
        and not UnixProcessProbe().is_alive(lease.pid)
    ):
        lease_port.release(lease)
        orphan_cleaned = True
    loaded = (
        _run(["launchctl", "print", _launchctl_target(service_key)]).returncode == 0
    )
    if not loaded:
        if state.get("status") not in {"CONFIRMED", "RUNNING"}:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_RECOVERY_UNCONFIRMED", service_key
            )
        boot = _run(
            [
                "launchctl",
                "bootstrap",
                _launchctl_domain(),
                str(_plist_path(service_key)),
            ]
        )
        if boot.returncode != 0:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_RECOVERY_FAILED", service_key
            )
    receipt = build_lifecycle_receipt(
        service_key=service_key,
        action="RECOVER",
        status="RECOVERED" if orphan_cleaned or not loaded else "NO_OP",
        generation=plan.generation,
        observed_at=now,
        plan_digest=plan.plan_digest,
        details={"orphan_cleaned": orphan_cleaned, "bootstrap_required": not loaded},
    )
    _append_receipt(receipt)
    return {
        "status": receipt.status,
        "service": service_key,
        "orphan_cleaned": orphan_cleaned,
        "receipt_digest": receipt.receipt_digest,
    }


def _request_stop(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def _record_worker_heartbeat(
    *,
    plan: Phase9LaunchPlan,
    worker_port: RuntimeWorkerSupervisorPort,
    observed_at: datetime,
) -> None:
    try:
        worker_receipt = worker_port.heartbeat(
            stage=plan.stage,
            plan_digest=plan.plan_digest,
            observed_at=observed_at,
        )
        validate_supervised_worker_receipt(
            plan=plan,
            receipt=worker_receipt,
            port=worker_port,
        )
    except Exception as exc:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_WORKER_HEARTBEAT",
            "runtime worker heartbeat/receipt did not pass the injected boundary",
        ) from exc
    _append_receipt(
        build_lifecycle_receipt(
            service_key=plan.service_key,
            action="WORKER_HEARTBEAT",
            status="RUNNING",
            generation=plan.generation,
            observed_at=observed_at,
            plan_digest=plan.plan_digest,
            details={
                "worker_receipt_digest": worker_receipt.receipt_digest,
                "runtime_receipt_digest": worker_receipt.runtime_receipt_digest,
                "signal_candidate_digest": worker_receipt.signal_candidate_digest,
                "reason_code": worker_receipt.reason_code,
                "persistence_target": worker_receipt.persistence_target,
                "order_submission_enabled": False,
            },
        )
    )


def supervise(
    service_key: str,
    plan_digest: str,
    *,
    worker_port: RuntimeWorkerSupervisorPort | None = None,
) -> None:
    plan, state = _load_plan(service_key)
    if (
        state.get("status") not in {"CONFIRMED", "RUNNING"}
        or plan.plan_digest != plan_digest
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_SUPERVISE_UNCONFIRMED", service_key
        )
    if (
        service_key == "long_lived_runtime"
        and plan.stage != "NO_ORDER_SOAK"
        and worker_port is None
    ):
        blocked = build_lifecycle_receipt(
            service_key=service_key,
            action="WORKER_HEARTBEAT",
            status="BLOCKED",
            generation=plan.generation,
            observed_at=_now(),
            plan_digest=plan.plan_digest,
            details={
                "reason_code": "RUNTIME_WORKER_PORT_UNSET",
                "order_submission_enabled": False,
            },
        )
        _append_receipt(blocked)
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RUNTIME_WORKER_UNSET",
            "long-lived runtime requires an explicitly composed worker port",
        )
    lease_holder_nonce = secrets.token_urlsafe(48)
    lease_port = FileLeasePort(SUPPORT_ROOT)
    lease, receipt = claim_lease(
        lease_port,
        plan=plan,
        holder_token=lease_holder_nonce,
        pid=os.getpid(),
        now=_now(),
        ttl=timedelta(seconds=LEASE_TTL_SECONDS),
        process_probe=UnixProcessProbe(),
    )
    _append_receipt(receipt)
    try:
        if worker_port is not None:
            _record_worker_heartbeat(
                plan=plan,
                worker_port=worker_port,
                observed_at=_now(),
            )
        _atomic_json(
            _state_path(service_key),
            {**state, "status": "RUNNING", "running_since": _now().isoformat()},
        )
        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
        while not _STOP:
            time.sleep(HEARTBEAT_SECONDS)
            if _STOP:
                break
            lease, heartbeat = heartbeat_lease(
                lease_port,
                lease=lease,
                holder_token=lease_holder_nonce,
                now=_now(),
                ttl=timedelta(seconds=LEASE_TTL_SECONDS),
            )
            _append_receipt(heartbeat)
            if worker_port is not None:
                _record_worker_heartbeat(
                    plan=plan,
                    worker_port=worker_port,
                    observed_at=_now(),
                )
    finally:
        if lease_port.read(service_key) == lease:
            _append_receipt(
                release_lease(
                    lease_port,
                    lease=lease,
                    holder_token=lease_holder_nonce,
                    now=_now(),
                )
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "prepare",
            "confirm",
            "status",
            "restart",
            "stop",
            "recover",
            "supervise",
        ),
    )
    parser.add_argument(
        "--service", required=True, choices=("long_lived_runtime", "order_writer")
    )
    parser.add_argument(
        "--stage", choices=("NO_ORDER_SOAK", "SIGNAL_RISK_SHADOW", "OKX_DEMO_CANARY")
    )
    parser.add_argument("--plan-digest")
    parser.add_argument("--release-digest")
    parser.add_argument("--deployment-id", type=UUID)
    parser.add_argument("--deployment-capability-digest")
    parser.add_argument("--image-digest")
    parser.add_argument("--enable-order-writer", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            resolved_stage = args.stage
            if resolved_stage is None and args.service == "long_lived_runtime":
                resolved_stage = "NO_ORDER_SOAK"
            if resolved_stage is None:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_STAGE", "--stage is required"
                )
            if not args.release_digest:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_RELEASE_DIGEST",
                    "--release-digest is required",
                )
            payload = prepare(
                args.service,
                resolved_stage,
                release_digest=args.release_digest,
                deployment_id=args.deployment_id,
                deployment_capability_digest=args.deployment_capability_digest,
                image_digest=args.image_digest,
                enable_order_writer=args.enable_order_writer,
            )
        elif args.command == "confirm":
            if not args.plan_digest:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_PLAN_DIGEST", "--plan-digest is required"
                )
            payload = confirm(args.service, args.plan_digest)
        elif args.command == "status":
            payload = status(args.service)
        elif args.command == "restart":
            if not args.plan_digest:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_PLAN_DIGEST", "--plan-digest is required"
                )
            payload = restart(args.service, args.plan_digest)
        elif args.command == "stop":
            payload = stop(args.service)
        elif args.command == "recover":
            payload = recover(args.service)
        else:
            if not args.plan_digest:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_PLAN_DIGEST", "--plan-digest is required"
                )
            supervise(args.service, args.plan_digest)
            return 0
    except CanonicalPhase9SupervisorBlocked as exc:
        payload = {"status": "BLOCKED", "reason": exc.code, "detail": exc.detail}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return (
        0
        if payload["status"]
        in {
            "PREPARED",
            "CONFIRMED",
            "RUNNING",
            "RESTARTED",
            "STOPPED",
            "RECOVERED",
            "NO_OP",
        }
        else 2
    )


if __name__ == "__main__":
    sys.exit(main())
