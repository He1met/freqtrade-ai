"""Manage isolated canonical V1.3 Phase 9 macOS service supervisors.

The command is intentionally two phase: ``prepare`` writes a secret-free plist and
frozen plan, while ``confirm`` performs the first ``launchctl bootstrap``.  The
order writer is disabled unless a canary plan explicitly enables it.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import importlib.util
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
    OrderWriterCanaryAuthority,
    OrderWriterCanaryAuthorityPort,
    Phase9LaunchPlan,
    Phase9Lease,
    RuntimeWorkerSupervisorPort,
    build_launch_plan,
    build_lifecycle_receipt,
    build_order_writer_canary_authority,
    claim_lease,
    heartbeat_lease,
    release_lease,
    require_current_order_writer_canary_authority,
    runtime_image_plan_authority,
    validate_supervised_worker_receipt,
    verify_launch_plan,
)
from app.canonical_v13.market_acquisition import (  # noqa: E402
    CanonicalMarketAcquisitionBlocked,
)
from app.canonical_v13.runtime_image_authority import (  # noqa: E402
    CanonicalRuntimeImageBlocked,
    load_accepted_runtime_image,
)
from app.canonical_v13.phase9_production_composition import (  # noqa: E402
    CanonicalFillWriterOperator,
    CanonicalLedgerWriterOperator,
    CanonicalOrderWriterOperator,
    CanonicalPhase9CompositionBlocked,
    CanonicalReconciliationWriterOperator,
    DatabaseOrderWriterAuthorityVerifier,
    RecordedCanaryProbe,
    confirm_running_runtime_from_supervisor,
    confirm_stopped_runtime_from_supervisor,
    record_current_canary_attestation,
    record_current_canary_probe_receipt,
)
from app.canonical_v13.phase9_recovery_composition import (  # noqa: E402
    accept_phase9_recovery_soak,
)
from app.canonical_v13.phase9_recovery_acceptance import (  # noqa: E402
    CanonicalPhase9RecoveryAcceptanceBlocked,
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
RUNTIME_CONTAINER_STOP_GRACE_SECONDS = 10
SUPERVISOR_TEARDOWN_TIMEOUT_SECONDS = RUNTIME_CONTAINER_STOP_GRACE_SECONDS + 5
RUNTIME_LAUNCHD_EXIT_TIMEOUT_SECONDS = SUPERVISOR_TEARDOWN_TIMEOUT_SECONDS + 5
_STOP = False
ORDER_HOLDER_KEYCHAIN_SERVICE = "freqtrade-ai/v13/phase9-order-holder-token"
RUNTIME_CREDENTIAL_REFERENCE = "none:public-okx-market-only"
RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE = (
    "freqtrade-ai/v13/runtime-signal-receipt-hmac-v1"
)
PODMAN_PATH = "/opt/homebrew/bin/podman"


class RuntimeContainerPort:
    """Rootless Podman boundary for the accepted long-lived runtime artifact."""

    def __init__(self, runner=None) -> None:
        self._runner = runner

    def _execute(self, command: list[str]):
        runner = self._runner or _run
        return runner(command)

    @staticmethod
    def name(plan: Phase9LaunchPlan) -> str:
        return f"canonical-v13-runtime-{plan.generation}-{plan.plan_digest[:12]}"

    def start(self, plan: Phase9LaunchPlan) -> str:
        if (
            plan.service_key != "long_lived_runtime"
            or plan.image_digest is None
            or plan.runtime_image_config_digest is None
            or plan.runtime_image_acceptance_id is None
        ):
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_RUNTIME_CONTAINER_AUTHORITY", "accepted runtime image is required"
            )
        command = [
            PODMAN_PATH,
            "run",
            "--detach",
            "--rm",
            "--name",
            self.name(plan),
            "--network=none",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--memory=256m",
            "--cpus=0.5",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m",
            "--tmpfs=/run/canonical-v13-runtime:rw,noexec,nosuid,nodev,size=4m",
            "--label",
            f"io.freqtrade-ai.plan-digest={plan.plan_digest}",
            f"sha256:{plan.runtime_image_config_digest}",
            "serve",
        ]
        result = self._execute(command)
        container_id = result.stdout.strip() if result.returncode == 0 else ""
        if (
            result.returncode != 0
            or len(container_id) < 12
            or any(character not in "0123456789abcdef" for character in container_id)
        ):
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_RUNTIME_CONTAINER_START", "rootless accepted image did not start"
            )
        return container_id

    def verify(self, plan: Phase9LaunchPlan, container_id: str) -> str:
        result = self._execute(
            [
                PODMAN_PATH,
                "container",
                "inspect",
                "--format",
                "{{.State.Running}} {{.Image}} {{index .Config.Labels \"io.freqtrade-ai.plan-digest\"}}",
                container_id,
            ]
        )
        fields = result.stdout.strip().split()
        observed_image = fields[1].removeprefix("sha256:") if len(fields) == 3 else ""
        if (
            result.returncode != 0
            or fields[:1] != ["true"]
            or observed_image != plan.runtime_image_config_digest
            or fields[2:] != [plan.plan_digest]
        ):
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_RUNTIME_CONTAINER_OBSERVATION",
                "running container does not match accepted plan",
            )
        return sha256(
            json.dumps(
                {
                    "container_id": container_id,
                    "image_config_digest": observed_image,
                    "image_manifest_digest": plan.image_digest,
                    "plan_digest": plan.plan_digest,
                    "security_profile": "rootless-readonly-capdrop-network-none-v1",
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def stop(self, plan: Phase9LaunchPlan, container_id: str) -> None:
        result = self._execute(
            [
                PODMAN_PATH,
                "stop",
                f"--time={RUNTIME_CONTAINER_STOP_GRACE_SECONDS}",
                container_id,
            ]
        )
        if result.returncode != 0:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_RUNTIME_CONTAINER_STOP", self.name(plan)
            )


def _authority_from_payload(payload: object) -> OrderWriterCanaryAuthority | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("order writer canary authority must be an object")
    return OrderWriterCanaryAuthority(
        deployment_id=UUID(str(payload["deployment_id"])),
        deployment_capability_digest=str(payload["deployment_capability_digest"]),
        execution_canary_risk_policy_id=UUID(
            str(payload["execution_canary_risk_policy_id"])
        ),
        execution_canary_risk_policy_digest=str(
            payload["execution_canary_risk_policy_digest"]
        ),
        attestation_id=UUID(str(payload["attestation_id"])),
        attestation_digest=str(payload["attestation_digest"]),
        attestation_expires_at=datetime.fromisoformat(
            str(payload["attestation_expires_at"])
        ),
        instrument_metadata_digest=str(payload["instrument_metadata_digest"]),
        mark_price_snapshot_digest=str(payload["mark_price_snapshot_digest"]),
        effective_leverage=str(payload["effective_leverage"]),
        position_policy=str(payload["position_policy"]),
    )


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
                runtime_image_acceptance_id=(
                    UUID(str(payload["runtime_image_acceptance_id"]))
                    if payload.get("runtime_image_acceptance_id")
                    else None
                ),
                runtime_image_acceptance_receipt_digest=(
                    str(payload["runtime_image_acceptance_receipt_digest"])
                    if payload.get("runtime_image_acceptance_receipt_digest")
                    else None
                ),
                runtime_image_config_digest=(
                    str(payload["runtime_image_config_digest"])
                    if payload.get("runtime_image_config_digest")
                    else None
                ),
                order_writer_canary_authority=_authority_from_payload(
                    payload.get("order_writer_canary_authority")
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


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_PRODUCTION_COMPOSITION", name
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _phase9_database_url(capability: str) -> str:
    api_service = _load_script_module(
        "canonical_v13_api_service_phase9_boundary",
        REPO_ROOT / "scripts" / "canonical_v13_api_service.py",
    )
    for principal, physical_capability, keychain_service in api_service.PHASE9_PRINCIPAL_SPECS:
        if physical_capability == f"freqtrade_ai_v13_{capability.removeprefix('canonical_')}":
            return api_service._database_url(principal, keychain_service)
    if capability == "canonical_runtime_reader":
        principal, _physical, keychain_service = (
            api_service.RUNTIME_READER_PRINCIPAL_SPEC
        )
        return api_service._database_url(principal, keychain_service)
    raise CanonicalPhase9SupervisorBlocked(
        "BLOCKED_PHASE9_DATABASE_CAPABILITY", capability
    )


def _control_database_url() -> str:
    api_service = _load_script_module(
        "canonical_v13_api_service_phase9_control_boundary",
        REPO_ROOT / "scripts" / "canonical_v13_api_service.py",
    )
    return api_service.canonical_control_database_url()


def _connection_factory(database_url: str):
    from sqlalchemy import create_engine  # noqa: PLC0415

    engine = create_engine(database_url, pool_pre_ping=True)

    @contextmanager
    def factory():
        with engine.begin() as connection:
            yield connection

    return factory


def _read_order_holder_token() -> str:
    api_service = _load_script_module(
        "canonical_v13_api_service_phase9_holder_boundary",
        REPO_ROOT / "scripts" / "canonical_v13_api_service.py",
    )
    holder_fence_material = api_service._read_keychain(
        ORDER_HOLDER_KEYCHAIN_SERVICE
    )
    if holder_fence_material is None or len(holder_fence_material) < 48:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_ORDER_HOLDER", "stable Keychain holder token is required"
        )
    return holder_fence_material


def _production_authority_port() -> DatabaseOrderWriterAuthorityVerifier:
    return DatabaseOrderWriterAuthorityVerifier(
        _connection_factory(_phase9_database_url("canonical_order_writer"))
    )


def _production_runtime_worker_factory():
    """Compose B from public market data and two isolated DB capabilities."""

    from app.canonical_v13.okx_public_market import (  # noqa: PLC0415
        OkxPublicHistoryCandleDownloader,
    )
    from app.canonical_v13.phase9_production_runtime import (  # noqa: PLC0415
        ProductionRuntimeWorkerFactory,
    )
    from app.canonical_v13.phase9_keychain import (  # noqa: PLC0415
        CanonicalPhase9KeychainBlocked,
        read_canonical_service_secret,
    )

    try:
        signing_key = read_canonical_service_secret(
            RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE
        )
    except CanonicalPhase9KeychainBlocked as exc:
        raise CanonicalPhase9SupervisorBlocked(exc.code, exc.detail) from None

    return ProductionRuntimeWorkerFactory(
        runtime_connection_factory=_connection_factory(
            _phase9_database_url("canonical_runtime_reader")
        ),
        signal_connection_factory=_connection_factory(
            _phase9_database_url("canonical_signal_writer")
        ),
        downloader=OkxPublicHistoryCandleDownloader(),
        signing_key=signing_key,
    )


def _load_runtime_image_authority(acceptance_id: UUID):
    try:
        factory = _connection_factory(
            _phase9_database_url("canonical_deployment_writer")
        )
        with factory() as connection:
            return runtime_image_plan_authority(
                load_accepted_runtime_image(connection, acceptance_id)
            )
    except CanonicalRuntimeImageBlocked as exc:
        raise CanonicalPhase9SupervisorBlocked(exc.code, exc.detail) from None


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
    payload = _json_safe(asdict(lease))
    assert isinstance(payload, dict)
    return payload


def _plan_payload(plan: Phase9LaunchPlan) -> dict[str, object]:
    payload = _json_safe(asdict(plan))
    assert isinstance(payload, dict)
    return payload


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
            runtime_image_acceptance_id=(
                UUID(str(payload["runtime_image_acceptance_id"]))
                if payload.get("runtime_image_acceptance_id")
                else None
            ),
            runtime_image_acceptance_receipt_digest=(
                str(payload["runtime_image_acceptance_receipt_digest"])
                if payload.get("runtime_image_acceptance_receipt_digest")
                else None
            ),
            runtime_image_config_digest=(
                str(payload["runtime_image_config_digest"])
                if payload.get("runtime_image_config_digest")
                else None
            ),
            release_digest=str(payload["release_digest"]),
            generation=int(payload["generation"]),
            prepared_at=datetime.fromisoformat(str(payload["prepared_at"])),
            demo_only=payload["demo_only"] is True,
            allow_real_funds=payload["allow_real_funds"] is True,
            order_writer_enabled=payload["order_writer_enabled"] is True,
            plan_digest=str(payload["plan_digest"]),
            order_writer_canary_authority=_authority_from_payload(
                payload.get("order_writer_canary_authority")
            ),
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


def _latest_running_heartbeat(service_key: str, plan: Phase9LaunchPlan):
    try:
        lines = _receipt_path(service_key).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RUNTIME_HEARTBEAT_UNSET", service_key
        ) from exc
    for line in reversed(lines):
        try:
            payload = json.loads(line)
            if (
                payload.get("action") == "HEARTBEAT"
                and payload.get("status") == "RUNNING"
                and payload.get("plan_digest") == plan.plan_digest
                and int(payload.get("generation")) == plan.generation
            ):
                receipt = build_lifecycle_receipt(
                    service_key=str(payload["service_key"]),
                    action=str(payload["action"]),
                    status=str(payload["status"]),
                    generation=int(payload["generation"]),
                    observed_at=datetime.fromisoformat(str(payload["observed_at"])),
                    plan_digest=str(payload["plan_digest"]),
                    holder_token_digest=str(payload["holder_token_digest"]),
                    details=dict(payload["details"]),
                    receipt_id=UUID(str(payload["receipt_id"])),
                )
                if receipt.receipt_digest != payload.get("receipt_digest"):
                    continue
                return receipt
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    raise CanonicalPhase9SupervisorBlocked(
        "BLOCKED_PHASE9_RUNTIME_HEARTBEAT_UNSET", service_key
    )


def _latest_stop_receipt(service_key: str, plan: Phase9LaunchPlan):
    for receipt in reversed(_verified_lifecycle_receipts(service_key)):
        if (
            receipt.action == "STOP"
            and receipt.status == "STOPPED"
            and receipt.plan_digest == plan.plan_digest
            and receipt.generation == plan.generation
        ):
            return receipt
    raise CanonicalPhase9SupervisorBlocked(
        "BLOCKED_PHASE9_RUNTIME_STOP_RECEIPT_UNSET", service_key
    )


def _verified_lifecycle_receipts(service_key: str):
    try:
        lines = _receipt_path(service_key).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RECOVERY_RECEIPTS_UNSET", service_key
        ) from exc
    receipts = []
    for line in lines:
        try:
            payload = json.loads(line)
            receipt = build_lifecycle_receipt(
                service_key=str(payload["service_key"]),
                action=str(payload["action"]),
                status=str(payload["status"]),
                generation=int(payload["generation"]),
                observed_at=datetime.fromisoformat(str(payload["observed_at"])),
                plan_digest=(
                    str(payload["plan_digest"])
                    if payload.get("plan_digest") is not None
                    else None
                ),
                holder_token_digest=(
                    str(payload["holder_token_digest"])
                    if payload.get("holder_token_digest") is not None
                    else None
                ),
                details=dict(payload["details"]),
                receipt_id=UUID(str(payload["receipt_id"])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_RECOVERY_RECEIPT_CORRUPT", service_key
            ) from exc
        if receipt.receipt_digest != payload.get("receipt_digest"):
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_RECOVERY_RECEIPT_CORRUPT", service_key
            )
        receipts.append(receipt)
    return tuple(receipts)


class FilesystemRecoverySupervisorEvidence:
    """Read current supervisor state without mutating launchd or lease files."""

    def latest_lifecycle(self, *, service_key: str, action: str):
        for receipt in reversed(_verified_lifecycle_receipts(service_key)):
            if receipt.action == action:
                return receipt
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RECOVERY_RECEIPT_UNSET", f"{service_key}:{action}"
        )

    def launch_agent_loaded(self, service_key: str) -> bool:
        return (
            _run(["launchctl", "print", _launchctl_target(service_key)]).returncode
            == 0
        )

    def file_lease(self, service_key: str):
        return FileLeasePort(SUPPORT_ROOT).read(service_key)

    def process_alive(self, pid: int) -> bool:
        return UnixProcessProbe().is_alive(pid)


def plist_payload(plan: Phase9LaunchPlan) -> dict[str, object]:
    """Return a LaunchAgent definition containing no credential or DSN material."""

    verify_launch_plan(plan)
    payload: dict[str, object] = {
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
    if plan.service_key == "long_lived_runtime":
        payload["ExitTimeOut"] = RUNTIME_LAUNCHD_EXIT_TIMEOUT_SECONDS
    return payload


def prepare(
    service_key: str,
    stage: str,
    *,
    release_digest: str,
    deployment_id: UUID | None,
    deployment_capability_digest: str | None,
    runtime_image_acceptance_id: UUID | None,
    enable_order_writer: bool,
    order_writer_canary_authority: OrderWriterCanaryAuthority | None = None,
) -> dict[str, object]:
    observed_release_digest = _require_release_checkout()
    if observed_release_digest != release_digest:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RELEASE_DRIFT",
            "prepared release digest does not match clean exact-main HEAD",
        )
    runtime_authority = None
    if service_key == "long_lived_runtime":
        if runtime_image_acceptance_id is None:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_RUNTIME_PLAN_LINEAGE_UNSET",
                "--runtime-image-acceptance-id is required",
            )
        runtime_authority = _load_runtime_image_authority(
            runtime_image_acceptance_id
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
                or existing_plan.runtime_image_acceptance_id
                != runtime_image_acceptance_id
                or existing_plan.order_writer_enabled != enable_order_writer
                or existing_plan.order_writer_canary_authority
                != order_writer_canary_authority
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
        runtime_image_authority=runtime_authority,
        order_writer_enabled=enable_order_writer,
        order_writer_canary_authority=order_writer_canary_authority,
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


def confirm(
    service_key: str,
    plan_digest: str,
    *,
    authority_port: OrderWriterCanaryAuthorityPort | None = None,
) -> dict[str, object]:
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
    require_current_order_writer_canary_authority(
        plan=plan, observed_at=_now(), port=authority_port
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


def _stop_exact_runtime_container(plan: Phase9LaunchPlan) -> None:
    if plan.service_key != "long_lived_runtime":
        return
    container_name = RuntimeContainerPort.name(plan)
    observed = _run([PODMAN_PATH, "container", "exists", container_name])
    if observed.returncode not in {0, 1}:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_STOP_CONTAINER_OBSERVATION", plan.service_key
        )
    if observed.returncode == 0:
        _run(
            [
                PODMAN_PATH,
                "stop",
                f"--time={RUNTIME_CONTAINER_STOP_GRACE_SECONDS}",
                container_name,
            ]
        )
        observed = _run([PODMAN_PATH, "container", "exists", container_name])
    if observed.returncode != 1:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_STOP_CONTAINER_HELD", plan.service_key
        )


def stop(service_key: str) -> dict[str, object]:
    _require_release_checkout()
    plan, state = _load_plan(service_key)
    if state.get("status") == "STOPPED":
        _stop_exact_runtime_container(plan)
        return {
            "status": "STOPPED",
            "service": service_key,
            "receipt_digest": state.get("stop_receipt_digest"),
            "repeat_noop": True,
        }
    completed = _run(["launchctl", "bootout", _launchctl_target(service_key)])
    lease_port = FileLeasePort(SUPPORT_ROOT)
    # The runtime releases its file lease only after the rootless container has
    # completed its graceful stop.  Keep this supervisory confirmation window
    # strictly larger than the container grace period so normal teardown cannot
    # be misclassified as an orphaned live lease.
    deadline = time.monotonic() + SUPERVISOR_TEARDOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        observed_lease = lease_port.read(service_key)
        if observed_lease is None or (
            observed_lease.expires_at <= _now()
            and not UnixProcessProbe().is_alive(observed_lease.pid)
        ):
            break
        time.sleep(0.1)
    remaining_lease = lease_port.read(service_key)
    if (
        remaining_lease is not None
        and remaining_lease.expires_at <= _now()
        and not UnixProcessProbe().is_alive(remaining_lease.pid)
    ):
        lease_port.release(remaining_lease)
        _append_receipt(
            build_lifecycle_receipt(
                service_key=service_key,
                action="STOP_ORPHAN_LEASE_RELEASE",
                status="RECOVERED",
                generation=plan.generation,
                observed_at=_now(),
                plan_digest=plan.plan_digest,
                holder_token_digest=remaining_lease.holder_token_digest,
                details={
                    "pid": remaining_lease.pid,
                    "expires_at": remaining_lease.expires_at.isoformat(),
                    "reason_code": "EXPIRED_DEAD_LEASE",
                },
            )
        )
        remaining_lease = None
    if remaining_lease is not None:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_STOP_LEASE_HELD", service_key
        )
    _stop_exact_runtime_container(plan)
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


def restart(
    service_key: str,
    plan_digest: str,
    *,
    authority_port: OrderWriterCanaryAuthorityPort | None = None,
) -> dict[str, object]:
    _require_release_checkout()
    plan, state = _load_plan(service_key)
    if (
        state.get("status") not in {"CONFIRMED", "RUNNING"}
        or plan.plan_digest != plan_digest
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RESTART_UNCONFIRMED", service_key
        )
    require_current_order_writer_canary_authority(
        plan=plan, observed_at=_now(), port=authority_port
    )
    restart_mode = "KICKSTART"
    if service_key == "long_lived_runtime":
        restart_mode = "GRACEFUL_BOOTOUT_BOOTSTRAP"
        retired = _run(["launchctl", "bootout", _launchctl_target(service_key)])
        if retired.returncode != 0:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_RESTART_FAILED", "runtime bootout failed"
            )
        lease_port = FileLeasePort(SUPPORT_ROOT)
        deadline = time.monotonic() + 10
        while lease_port.read(service_key) is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        if lease_port.read(service_key) is not None:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_RESTART_LEASE_HELD", service_key
            )
        container = _run(
            [PODMAN_PATH, "container", "exists", RuntimeContainerPort.name(plan)]
        )
        if container.returncode not in {0, 1}:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_RESTART_CONTAINER_OBSERVATION", service_key
            )
        if container.returncode == 0:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_RESTART_CONTAINER_HELD", service_key
            )
        label_deadline = time.monotonic() + 10
        label = _run(["launchctl", "print", _launchctl_target(service_key)])
        while label.returncode == 0 and time.monotonic() < label_deadline:
            time.sleep(0.1)
            label = _run(["launchctl", "print", _launchctl_target(service_key)])
        if label.returncode == 0:
            raise CanonicalPhase9SupervisorBlocked(
                "BLOCKED_PHASE9_RESTART_LABEL_HELD", service_key
            )
        kicked = _run(
            [
                "launchctl",
                "bootstrap",
                _launchctl_domain(),
                str(_plist_path(service_key)),
            ]
        )
    else:
        kicked = _run(
            ["launchctl", "kickstart", "-k", _launchctl_target(service_key)]
        )
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
        details={"label": plan.launch_agent_label, "restart_mode": restart_mode},
    )
    _append_receipt(receipt)
    return {
        "status": "RESTARTED",
        "service": service_key,
        "receipt_digest": receipt.receipt_digest,
    }


def recover(
    service_key: str,
    *,
    authority_port: OrderWriterCanaryAuthorityPort | None = None,
) -> dict[str, object]:
    _require_release_checkout()
    plan, state = _load_plan(service_key)
    require_current_order_writer_canary_authority(
        plan=plan, observed_at=_now(), port=authority_port
    )
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


_RETRYABLE_PUBLIC_MARKET_BLOCKERS = frozenset(
    {
        "BLOCKED_OKX_PUBLIC_HTTP",
        "BLOCKED_OKX_PUBLIC_UNAVAILABLE",
        "BLOCKED_OKX_CANDLE_GAP",
        "BLOCKED_OKX_CANDLE_UNCONFIRMED_OR_INVALID",
    }
)


def _retryable_public_market_blocker(exc: BaseException) -> str | None:
    """Return only reviewed credential-free market transients from a cause chain."""

    observed: BaseException | None = exc
    seen: set[int] = set()
    while observed is not None and id(observed) not in seen:
        seen.add(id(observed))
        if isinstance(observed, CanonicalMarketAcquisitionBlocked):
            return (
                observed.code
                if observed.code in _RETRYABLE_PUBLIC_MARKET_BLOCKERS
                else None
            )
        observed = observed.__cause__ or observed.__context__
    return None


def _record_resilient_worker_heartbeat(
    *,
    plan: Phase9LaunchPlan,
    worker_port: RuntimeWorkerSupervisorPort,
    observed_at: datetime,
) -> None:
    """Keep the fenced runtime alive only for reviewed no-signal market transients."""

    try:
        _record_worker_heartbeat(
            plan=plan,
            worker_port=worker_port,
            observed_at=observed_at,
        )
    except CanonicalPhase9SupervisorBlocked as exc:
        reason_code = _retryable_public_market_blocker(exc)
        if reason_code is None:
            raise
        _append_receipt(
            build_lifecycle_receipt(
                service_key=plan.service_key,
                action="WORKER_HEARTBEAT",
                status="BLOCKED",
                generation=plan.generation,
                observed_at=observed_at,
                plan_digest=plan.plan_digest,
                details={
                    "reason_code": reason_code,
                    "retryable_public_market_transient": True,
                    "signal_candidate_digest": None,
                    "persistence_target": "canonical_signal_writer",
                    "order_submission_enabled": False,
                },
            )
        )


def confirm_runtime_observation(plan_digest: str) -> dict[str, object]:
    """Promote PENDING to ACTIVE only from the current live supervisor evidence."""

    _require_release_checkout()
    plan, state = _load_plan("long_lived_runtime")
    lease = FileLeasePort(SUPPORT_ROOT).read("long_lived_runtime")
    observed_at = _now()
    if (
        state.get("status") != "RUNNING"
        or plan.plan_digest != plan_digest
        or lease is None
        or lease.expires_at <= observed_at
        or not UnixProcessProbe().is_alive(lease.pid)
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RUNTIME_OBSERVATION",
            "exact RUNNING state, live holder, and fresh lease are required",
        )
    heartbeat = _latest_running_heartbeat("long_lived_runtime", plan)
    factory = _connection_factory(
        _phase9_database_url("canonical_deployment_writer")
    )
    with factory() as connection:
        runtime_id = confirm_running_runtime_from_supervisor(
            connection,
            plan=plan,
            lease=lease,
            heartbeat_receipt=heartbeat,
            observed_at=observed_at,
            credential_reference=RUNTIME_CREDENTIAL_REFERENCE,
        )
    return {
        "status": "ACTIVE",
        "service": plan.service_key,
        "deployment_id": str(plan.deployment_id),
        "runtime_instance_id": str(runtime_id),
        "plan_digest": plan.plan_digest,
        "runtime_receipt_digest": heartbeat.receipt_digest,
    }


def confirm_runtime_stop_observation(plan_digest: str) -> dict[str, object]:
    """Persist STOPPED only after launchd, lease, process, and container are absent."""

    _require_release_checkout()
    plan, state = _load_plan("long_lived_runtime")
    if state.get("status") != "STOPPED" or plan.plan_digest != plan_digest:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RUNTIME_STOP_OBSERVATION",
            "exact STOPPED state and plan digest are required",
        )
    launch_agent = _run(
        ["launchctl", "print", _launchctl_target("long_lived_runtime")]
    )
    if launch_agent.returncode not in {0, 3, 113}:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_STOP_LAUNCHD_OBSERVATION", plan.service_key
        )
    launch_agent_loaded = launch_agent.returncode == 0
    lease = FileLeasePort(SUPPORT_ROOT).read("long_lived_runtime")
    heartbeat = _latest_running_heartbeat("long_lived_runtime", plan)
    heartbeat_pid = heartbeat.details.get("pid")
    if isinstance(heartbeat_pid, bool) or not isinstance(heartbeat_pid, int):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_RUNTIME_STOP_RECEIPT_UNSET", plan.service_key
        )
    holder_pid_alive = UnixProcessProbe().is_alive(heartbeat_pid)
    container = _run(
        [PODMAN_PATH, "container", "exists", RuntimeContainerPort.name(plan)]
    )
    if container.returncode not in {0, 1}:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_STOP_CONTAINER_OBSERVATION", plan.service_key
        )
    observed_at = _now()
    factory = _connection_factory(
        _phase9_database_url("canonical_deployment_writer")
    )
    with factory() as connection:
        result = confirm_stopped_runtime_from_supervisor(
            connection,
            plan=plan,
            stop_receipt=_latest_stop_receipt("long_lived_runtime", plan),
            observed_at=observed_at,
            launch_agent_loaded=launch_agent_loaded,
            holder_pid_alive=holder_pid_alive,
            lease=lease,
            container_present=container.returncode == 0,
            credential_reference=RUNTIME_CREDENTIAL_REFERENCE,
        )
    return {
        "status": result.status,
        "service": plan.service_key,
        "deployment_id": str(plan.deployment_id),
        "runtime_instance_id": str(result.runtime_instance_id),
        "plan_digest": plan.plan_digest,
        "runtime_stop_receipt_digest": result.receipt_digest,
        "repeat_noop": result.repeat_noop,
    }


def _production_okx_session_factory():
    from app.canonical_v13.phase9_keychain import (  # noqa: PLC0415
        CanonicalPhase9KeychainBlocked,
        read_canonical_okx_demo_capability,
    )
    from app.canonical_v13.phase9_okx_demo import (  # noqa: PLC0415
        create_canonical_okx_demo_session,
    )

    def session_factory():
        try:
            capability = read_canonical_okx_demo_capability()
        except CanonicalPhase9KeychainBlocked as exc:
            raise CanonicalPhase9SupervisorBlocked(exc.code, exc.detail) from None
        return create_canonical_okx_demo_session(
            capability.environment,
            credential_generation_digest=capability.credential_generation_digest,
            lock_path=SUPPORT_ROOT / "canonical-order-writer.transport.lock",
        )

    return session_factory


def _production_order_operator(
    *, plan: Phase9LaunchPlan, lease: Phase9Lease, holder_token: str
) -> CanonicalOrderWriterOperator:
    connection_factory = _connection_factory(
        _phase9_database_url("canonical_order_writer")
    )
    return CanonicalOrderWriterOperator(
        plan=plan,
        supervisor_lease=lease,
        authority_port=_production_authority_port(),
        holder_token=holder_token,
        connection_factory=connection_factory,
        session_factory=_production_okx_session_factory(),
    )


def _probe_saga_path(deployment_id: UUID) -> Path:
    return SUPPORT_ROOT / f"canary-probe-{deployment_id}.saga.json"


def _sealed_probe_for_saga(
    deployment_id: UUID,
    session_factory,
    *,
    evaluated_at: datetime,
    linked_probe_receipt_exists,
):
    """Recover only a server-sealed safe probe; callers never provide evidence."""

    from app.canonical_v13.phase9_okx_demo import RedactedOkxDemoProbe  # noqa: PLC0415

    path = _probe_saga_path(deployment_id)

    def seal_current_probe():
        with session_factory() as session:
            current = session.probe(instrument="BTC-USDT-SWAP")
        _atomic_json(
            path,
            {
                "contract": "canonical-v13-okx-demo-probe-saga-v1",
                "deployment_id": str(deployment_id),
                "probe": _json_safe(asdict(current)),
            },
        )
        return current

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return seal_current_probe()
    except (OSError, ValueError, TypeError) as exc:
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_PROBE_SAGA", "sealed probe saga is corrupt"
        ) from exc
    raw = payload.get("probe") if isinstance(payload, dict) else None
    expected = {field.name for field in fields(RedactedOkxDemoProbe)}
    if (
        payload.get("contract") != "canonical-v13-okx-demo-probe-saga-v1"
        or payload.get("deployment_id") != str(deployment_id)
        or not isinstance(raw, dict)
        or set(raw) != expected
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_PROBE_SAGA", "sealed probe saga identity drifted"
        )
    for name in tuple(expected):
        if name.endswith("_at") or name.endswith("_expires_at") or name == "expires_at":
            raw[name] = datetime.fromisoformat(str(raw[name]))
    raw["permissions"] = dict(raw["permissions"])
    sealed = RedactedOkxDemoProbe(**raw)
    if sealed.expires_at > evaluated_at:
        return sealed
    if linked_probe_receipt_exists():
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_PROBE_SAGA_LINKED_EXPIRED",
            "expired sealed probe is already linked and cannot be replaced",
        )
    # An attestation committed before the approval-writer step is immutable but
    # does not authorize execution by itself.  If its sealed evidence expires,
    # atomically replace only the local safe saga with a new authenticated probe;
    # the orphan attestation remains auditable and cannot be rebound.
    return seal_current_probe()


def _linked_probe_receipt_exists(approval_factory, deployment_id: UUID) -> bool:
    from sqlalchemy import select  # noqa: PLC0415

    from app.canonical_v13.models import (  # noqa: PLC0415
        EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    )

    with approval_factory() as approval_connection:
        effective = getattr(approval_connection, "connection", approval_connection)
        return (
            effective.execute(
                select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id).where(
                    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.deployment_id
                    == deployment_id
                )
            ).first()
            is not None
        )


def probe_canary(deployment_id: UUID) -> dict[str, object]:
    """Persist one sealed probe through separately owned capability transactions."""

    _require_release_checkout()
    now = _now()
    deployment_factory = _connection_factory(
        _phase9_database_url("canonical_deployment_writer")
    )
    approval_factory = _connection_factory(
        _phase9_database_url("canonical_approval_writer")
    )
    session_factory = _production_okx_session_factory()
    probe = _sealed_probe_for_saga(
        deployment_id,
        session_factory,
        evaluated_at=now,
        linked_probe_receipt_exists=lambda: _linked_probe_receipt_exists(
            approval_factory, deployment_id
        ),
    )

    class SealedProbeSession:
        def probe(self, *, instrument: str):
            if instrument != probe.instrument:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_PROBE_INSTRUMENT", instrument
                )
            return probe

    # Commit the deployment-owned FK parent before opening the approval writer
    # transaction.  The sealed safe probe file makes a crash between these two
    # steps an exact idempotent replay without another authenticated probe.
    with deployment_factory() as deployment_connection:
        sealed_probe, attestation = record_current_canary_attestation(
            deployment_connection,
            deployment_id=deployment_id,
            session=SealedProbeSession(),
            evaluated_at=now,
        )
    with approval_factory() as approval_connection:
        probe_receipt = record_current_canary_probe_receipt(
            approval_connection,
            deployment_id=deployment_id,
            probe=sealed_probe,
            attestation=attestation,
            evaluated_at=now,
        )
    recorded = RecordedCanaryProbe(attestation, probe_receipt)
    return {
        "status": "READY",
        "deployment_id": str(deployment_id),
        "attestation_id": str(recorded.attestation.attestation_id),
        "attestation_digest": recorded.attestation.attestation_digest,
        "probe_receipt_id": str(recorded.probe_receipt.probe_receipt_id),
        "probe_receipt_digest": recorded.probe_receipt.receipt_digest,
        "observed_at": recorded.probe_receipt.observed_at.isoformat(),
        "expires_at": recorded.probe_receipt.expires_at.isoformat(),
        "repeat_noop": (
            recorded.attestation.repeat_noop
            and recorded.probe_receipt.repeat_noop
        ),
        "execution_target": "OKX_DEMO",
        "allow_real_funds": False,
    }


def dispatch_canary(plan_digest: str, risk_decision_id: UUID) -> dict[str, object]:
    """Dispatch one exact persisted canary request through the canonical saga."""

    _require_release_checkout()
    plan, state = _load_plan("order_writer")
    lease = FileLeasePort(SUPPORT_ROOT).read("order_writer")
    now = _now()
    if (
        state.get("status") != "RUNNING"
        or plan.plan_digest != plan_digest
        or lease is None
        or lease.expires_at <= now
        or not UnixProcessProbe().is_alive(lease.pid)
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_ORDER_SUPERVISOR_FENCE", "writer supervisor is not live"
        )
    result = _production_order_operator(
        plan=plan, lease=lease, holder_token=_read_order_holder_token()
    ).dispatch_canary(risk_decision_id=risk_decision_id, evaluated_at=now)
    return {
        "status": "ACCEPTED",
        "order_id": str(result.order_id),
        "exchange_order_id": result.exchange_order_id,
        "receipt_digest": result.receipt_digest,
        "repeat_noop": result.repeat_noop,
    }


def recover_canary(plan_digest: str, order_id: UUID) -> dict[str, object]:
    """Use only GET recovery for a previously uncertain canonical Demo order."""

    _require_release_checkout()
    plan, state = _load_plan("order_writer")
    lease = FileLeasePort(SUPPORT_ROOT).read("order_writer")
    now = _now()
    if (
        state.get("status") != "RUNNING"
        or plan.plan_digest != plan_digest
        or lease is None
        or lease.expires_at <= now
        or not UnixProcessProbe().is_alive(lease.pid)
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_ORDER_SUPERVISOR_FENCE", "writer plan/lease is unavailable"
        )
    result = _production_order_operator(
        plan=plan, lease=lease, holder_token=_read_order_holder_token()
    ).recover_canary(order_id=order_id, evaluated_at=now)
    replay_receipt = build_lifecycle_receipt(
        service_key="order_writer",
        action="ORDER_REPLAY",
        status="CONFIRMED" if result.repeat_noop else "RECOVERED",
        generation=plan.generation,
        observed_at=now,
        plan_digest=plan.plan_digest,
        details={
            "order_id": str(result.order_id),
            "order_receipt_digest": result.receipt_digest,
            "repeat_noop": result.repeat_noop,
            "transport_mode": "GET_ONLY",
        },
    )
    _append_receipt(replay_receipt)
    return {
        "status": "RECOVERED",
        "order_id": str(result.order_id),
        "exchange_order_id": result.exchange_order_id,
        "receipt_digest": result.receipt_digest,
        "repeat_noop": result.repeat_noop,
        "replay_evidence_receipt_digest": replay_receipt.receipt_digest,
    }


def collect_canary_fills(order_id: UUID) -> dict[str, object]:
    """GET-only fill collection under the independently resolved fill identity."""

    _require_release_checkout()
    operator = CanonicalFillWriterOperator(
        connection_factory=_connection_factory(
            _phase9_database_url("canonical_fill_writer")
        ),
        session_factory=_production_okx_session_factory(),
    )
    fill_ids = operator.collect(order_id=order_id)
    return {
        "status": "RECORDED",
        "order_id": str(order_id),
        "fill_ids": [str(value) for value in fill_ids],
        "fill_count": len(fill_ids),
        "execution_target": "OKX_DEMO",
        "allow_real_funds": False,
    }


def post_canary_ledger(fill_id: UUID) -> dict[str, object]:
    """Post a server-derived ledger entry under canonical_ledger_writer only."""

    _require_release_checkout()
    entry_id = CanonicalLedgerWriterOperator(
        _connection_factory(_phase9_database_url("canonical_ledger_writer"))
    ).post(fill_id=fill_id)
    return {
        "status": "POSTED",
        "fill_id": str(fill_id),
        "ledger_entry_id": str(entry_id),
        "execution_target": "OKX_DEMO",
        "allow_real_funds": False,
    }


def reconcile_canary(order_id: UUID) -> dict[str, object]:
    """Reconcile persisted lineage under canonical_reconciliation_writer only."""

    _require_release_checkout()
    run_ids = CanonicalReconciliationWriterOperator(
        _connection_factory(_phase9_database_url("canonical_reconciliation_writer"))
    ).reconcile(order_id=order_id)
    return {
        "status": "SUCCEEDED",
        "order_id": str(order_id),
        "reconciliation_run_ids": [str(value) for value in run_ids],
        "run_count": len(run_ids),
        "execution_target": "OKX_DEMO",
        "allow_real_funds": False,
    }


def accept_recovery_soak(qualification_decision_id: UUID) -> dict[str, object]:
    """Accept D only from current DB and read-only supervisor evidence ports."""

    _require_release_checkout()
    with _connection_factory(_control_database_url())() as connection:
        result = accept_phase9_recovery_soak(
            connection,
            qualification_decision_id=qualification_decision_id,
            supervisor=FilesystemRecoverySupervisorEvidence(),
            observed_at=_now(),
        )
    return {
        **result,
        "qualification_decision_id": str(qualification_decision_id),
        "execution_target": "OKX_DEMO",
        "allow_real_funds": False,
    }


def supervise(
    service_key: str,
    plan_digest: str,
    *,
    worker_port: RuntimeWorkerSupervisorPort | None = None,
    authority_port: OrderWriterCanaryAuthorityPort | None = None,
    lease_holder_token: str | None = None,
    production_compose: bool = False,
    runtime_container_port: RuntimeContainerPort | None = None,
) -> None:
    plan, state = _load_plan(service_key)
    if (
        state.get("status") not in {"CONFIRMED", "RUNNING"}
        or plan.plan_digest != plan_digest
    ):
        raise CanonicalPhase9SupervisorBlocked(
            "BLOCKED_PHASE9_SUPERVISE_UNCONFIRMED", service_key
        )
    if production_compose and service_key == "order_writer":
        authority_port = authority_port or _production_authority_port()
        lease_holder_token = lease_holder_token or _read_order_holder_token()
    if production_compose and service_key == "long_lived_runtime":
        if plan.stage != "NO_ORDER_SOAK":
            worker_port = worker_port or _production_runtime_worker_factory().build(plan)
        runtime_container_port = runtime_container_port or RuntimeContainerPort()
    require_current_order_writer_canary_authority(
        plan=plan, observed_at=_now(), port=authority_port
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
    lease_holder_nonce = lease_holder_token or secrets.token_urlsafe(48)
    lease_port = FileLeasePort(SUPPORT_ROOT)
    lease, receipt = claim_lease(
        lease_port,
        plan=plan,
        holder_token=lease_holder_nonce,
        pid=os.getpid(),
        now=_now(),
        ttl=timedelta(seconds=LEASE_TTL_SECONDS),
        process_probe=UnixProcessProbe(),
        authority_port=authority_port,
    )
    _append_receipt(receipt)
    container_id: str | None = None
    try:
        if runtime_container_port is not None:
            container_id = runtime_container_port.start(plan)
            container_observation_digest = runtime_container_port.verify(
                plan, container_id
            )
            _append_receipt(
                build_lifecycle_receipt(
                    service_key=service_key,
                    action="RUNTIME_CONTAINER_START",
                    status="RUNNING",
                    generation=plan.generation,
                    observed_at=_now(),
                    plan_digest=plan.plan_digest,
                    holder_token_digest=lease.holder_token_digest,
                    details={
                        "runtime_image_acceptance_id": str(
                            plan.runtime_image_acceptance_id
                        ),
                        "runtime_image_acceptance_receipt_digest": (
                            plan.runtime_image_acceptance_receipt_digest
                        ),
                        "container_observation_digest": container_observation_digest,
                        "network": "NONE",
                        "read_only": True,
                        "cap_drop": "ALL",
                        "no_new_privileges": True,
                    },
                )
            )
        if worker_port is not None:
            _record_resilient_worker_heartbeat(
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
                authority_port=authority_port,
            )
            _append_receipt(heartbeat)
            if worker_port is not None:
                _record_resilient_worker_heartbeat(
                    plan=plan,
                    worker_port=worker_port,
                    observed_at=_now(),
                )
    finally:
        container_error: Exception | None = None
        if runtime_container_port is not None and container_id is not None:
            try:
                runtime_container_port.stop(plan, container_id)
            except Exception as exc:
                container_error = exc
        if lease_port.read(service_key) == lease:
            _append_receipt(
                release_lease(
                    lease_port,
                    lease=lease,
                    holder_token=lease_holder_nonce,
                    now=_now(),
                )
            )
        if container_error is not None:
            raise container_error


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
            "confirm-runtime-observation",
            "confirm-runtime-stop-observation",
            "probe-canary",
            "dispatch-canary",
            "recover-canary",
            "collect-canary-fills",
            "post-canary-ledger",
            "reconcile-canary",
            "accept-recovery-soak",
        ),
    )
    parser.add_argument(
        "--service",
        required=True,
        choices=(
            "long_lived_runtime",
            "order_writer",
            "fill_writer",
            "ledger_writer",
            "reconciliation_writer",
            "recovery_control",
        ),
    )
    parser.add_argument(
        "--stage", choices=("NO_ORDER_SOAK", "SIGNAL_RISK_SHADOW", "OKX_DEMO_CANARY")
    )
    parser.add_argument("--plan-digest")
    parser.add_argument("--release-digest")
    parser.add_argument("--deployment-id", type=UUID)
    parser.add_argument("--deployment-capability-digest")
    parser.add_argument("--runtime-image-acceptance-id", type=UUID)
    parser.add_argument("--enable-order-writer", action="store_true")
    parser.add_argument("--execution-canary-risk-policy-id", type=UUID)
    parser.add_argument("--execution-canary-risk-policy-digest")
    parser.add_argument("--attestation-id", type=UUID)
    parser.add_argument("--attestation-digest")
    parser.add_argument("--attestation-expires-at")
    parser.add_argument("--instrument-metadata-digest")
    parser.add_argument("--mark-price-snapshot-digest")
    parser.add_argument("--effective-leverage")
    parser.add_argument("--position-policy", default="LONG_ONLY")
    parser.add_argument("--risk-decision-id", type=UUID)
    parser.add_argument("--order-id", type=UUID)
    parser.add_argument("--fill-id", type=UUID)
    parser.add_argument("--qualification-decision-id", type=UUID)
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
            writer_authority = None
            if args.service == "order_writer":
                try:
                    attestation_expires_at = datetime.fromisoformat(
                        str(args.attestation_expires_at).replace("Z", "+00:00")
                    )
                except (TypeError, ValueError) as exc:
                    raise CanonicalPhase9SupervisorBlocked(
                        "BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
                        "--attestation-expires-at must be timezone-aware ISO8601",
                    ) from exc
                writer_authority = build_order_writer_canary_authority(
                    deployment_id=args.deployment_id,
                    deployment_capability_digest=args.deployment_capability_digest,
                    execution_canary_risk_policy_id=(
                        args.execution_canary_risk_policy_id
                    ),
                    execution_canary_risk_policy_digest=(
                        args.execution_canary_risk_policy_digest
                    ),
                    attestation_id=args.attestation_id,
                    attestation_digest=args.attestation_digest,
                    attestation_expires_at=attestation_expires_at,
                    instrument_metadata_digest=args.instrument_metadata_digest,
                    mark_price_snapshot_digest=args.mark_price_snapshot_digest,
                    effective_leverage=args.effective_leverage,
                    position_policy=args.position_policy,
                )
            payload = prepare(
                args.service,
                resolved_stage,
                release_digest=args.release_digest,
                deployment_id=args.deployment_id,
                deployment_capability_digest=args.deployment_capability_digest,
                runtime_image_acceptance_id=args.runtime_image_acceptance_id,
                enable_order_writer=args.enable_order_writer,
                order_writer_canary_authority=writer_authority,
            )
        elif args.command == "confirm":
            if not args.plan_digest:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_PLAN_DIGEST", "--plan-digest is required"
                )
            payload = confirm(
                args.service,
                args.plan_digest,
                authority_port=(
                    _production_authority_port()
                    if args.service == "order_writer"
                    else None
                ),
            )
        elif args.command == "status":
            payload = status(args.service)
        elif args.command == "restart":
            if not args.plan_digest:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_PLAN_DIGEST", "--plan-digest is required"
                )
            payload = restart(
                args.service,
                args.plan_digest,
                authority_port=(
                    _production_authority_port()
                    if args.service == "order_writer"
                    else None
                ),
            )
        elif args.command == "stop":
            payload = stop(args.service)
        elif args.command == "recover":
            payload = recover(
                args.service,
                authority_port=(
                    _production_authority_port()
                    if args.service == "order_writer"
                    else None
                ),
            )
        elif args.command == "confirm-runtime-observation":
            if args.service != "long_lived_runtime" or not args.plan_digest:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_RUNTIME_OBSERVATION",
                    "long_lived_runtime and --plan-digest are required",
                )
            payload = confirm_runtime_observation(args.plan_digest)
        elif args.command == "confirm-runtime-stop-observation":
            if args.service != "long_lived_runtime" or not args.plan_digest:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_RUNTIME_STOP_OBSERVATION",
                    "long_lived_runtime and --plan-digest are required",
                )
            payload = confirm_runtime_stop_observation(args.plan_digest)
        elif args.command == "probe-canary":
            if args.service != "order_writer" or args.deployment_id is None:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_PROBE_COMMAND",
                    "order_writer and --deployment-id are required",
                )
            payload = probe_canary(args.deployment_id)
        elif args.command == "dispatch-canary":
            if (
                args.service != "order_writer"
                or not args.plan_digest
                or args.risk_decision_id is None
            ):
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_ORDER_COMMAND",
                    "order_writer, --plan-digest, and --risk-decision-id are required",
                )
            payload = dispatch_canary(args.plan_digest, args.risk_decision_id)
        elif args.command == "recover-canary":
            if (
                args.service != "order_writer"
                or not args.plan_digest
                or args.order_id is None
            ):
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_ORDER_COMMAND",
                    "order_writer, --plan-digest, and --order-id are required",
                )
            payload = recover_canary(args.plan_digest, args.order_id)
        elif args.command == "collect-canary-fills":
            if args.service != "fill_writer" or args.order_id is None:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_FILL_COMMAND",
                    "fill_writer and --order-id are required",
                )
            payload = collect_canary_fills(args.order_id)
        elif args.command == "post-canary-ledger":
            if args.service != "ledger_writer" or args.fill_id is None:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_LEDGER_COMMAND",
                    "ledger_writer and --fill-id are required",
                )
            payload = post_canary_ledger(args.fill_id)
        elif args.command == "reconcile-canary":
            if args.service != "reconciliation_writer" or args.order_id is None:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_RECONCILIATION_COMMAND",
                    "reconciliation_writer and --order-id are required",
                )
            payload = reconcile_canary(args.order_id)
        elif args.command == "accept-recovery-soak":
            if (
                args.service != "recovery_control"
                or args.qualification_decision_id is None
            ):
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_RECOVERY_ACCEPTANCE_COMMAND",
                    "recovery_control and --qualification-decision-id are required",
                )
            payload = accept_recovery_soak(args.qualification_decision_id)
        else:
            if not args.plan_digest:
                raise CanonicalPhase9SupervisorBlocked(
                    "BLOCKED_PHASE9_PLAN_DIGEST", "--plan-digest is required"
                )
            supervise(args.service, args.plan_digest, production_compose=True)
            return 0
    except (
        CanonicalPhase9SupervisorBlocked,
        CanonicalPhase9CompositionBlocked,
        CanonicalPhase9RecoveryAcceptanceBlocked,
    ) as exc:
        payload = {"status": "BLOCKED", "reason": exc.code, "detail": exc.detail}
    except Exception as exc:
        payload = {
            "status": "BLOCKED",
            "reason": "BLOCKED_PHASE9_PRODUCTION_COMMAND",
            "detail": type(exc).__name__,
        }
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
            "ACTIVE",
            "ACCEPTED",
            "READY",
            "RECORDED",
            "POSTED",
            "SUCCEEDED",
        }
        else 2
    )


if __name__ == "__main__":
    sys.exit(main())
