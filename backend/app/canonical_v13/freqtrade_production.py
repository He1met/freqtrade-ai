"""Fail-closed Freqtrade adapter for production no-trade research.

The outer process may read canonical evidence and persist receipts.  The sandboxed
strategy process receives only immutable files, has no database URL or credential
environment, and is launched in a one-shot OCI container with networking disabled.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
from types import MappingProxyType
from typing import Protocol

from sqlalchemy import Connection, select

from app.canonical_v13.models import (
    MARKET_ARTIFACTS_TABLE,
    MARKET_RECEIPTS_TABLE,
    MARKET_SNAPSHOT_MEMBERS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
)
from app.canonical_v13.research_validation import (
    EphemeralAttemptReceipt,
    RunningValidationAttempt,
    build_ephemeral_attempt_receipt,
    canonical_research_digest,
    validate_ephemeral_launch_spec,
)
from app.canonical_v13.runtime_reader import read_frozen_research_bundle


PRODUCTION_RESEARCH_ACTIVATION = "PRODUCTION_RESEARCH_NO_TRADE_V1"
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:([0-9a-f]{64})$")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:/-]{1,240}$")


class CanonicalProductionResearchBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ProductionResearchLimits:
    cpu_count: str = "1.0"
    memory_mb: int = 1024
    timeout_seconds: int = 900
    max_output_bytes: int = 2 * 1024 * 1024
    pids_limit: int = 64
    tmpfs_mb: int = 128

    def validate(self) -> None:
        if self.cpu_count not in {"0.5", "1.0", "2.0"}:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_RESEARCH_CPU_LIMIT", "CPU limit is outside the allowlist"
            )
        if not 256 <= self.memory_mb <= 4096:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_RESEARCH_MEMORY_LIMIT", "memory limit must be 256..4096 MiB"
            )
        if not 30 <= self.timeout_seconds <= 3600:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_RESEARCH_TIMEOUT_LIMIT", "timeout must be 30..3600 seconds"
            )
        if not 4096 <= self.max_output_bytes <= 8 * 1024 * 1024:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_RESEARCH_OUTPUT_LIMIT", "output limit is unsafe"
            )
        if not 16 <= self.pids_limit <= 256 or not 32 <= self.tmpfs_mb <= 512:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_RESEARCH_PROCESS_LIMIT", "PID or tmpfs limit is unsafe"
            )


@dataclass(frozen=True)
class ReadOnlyMount:
    source: Path
    destination: str
    content_digest: str
    size_bytes: int


@dataclass(frozen=True)
class ProductionResearchInputSet:
    workspace: Path
    request_path: Path
    mounts: tuple[ReadOnlyMount, ...]
    input_manifest_digest: str


@dataclass(frozen=True)
class SandboxCommandResult:
    return_code: int
    stdout: bytes
    stderr: bytes


class SandboxRunnerPort(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> SandboxCommandResult: ...


class ProductionResearchInputFactory(Protocol):
    def __call__(
        self, running_attempt: RunningValidationAttempt
    ) -> AbstractContextManager[ProductionResearchInputSet]: ...


class BoundedSubprocessSandboxRunner:
    """Run one argv without a shell while bounding time and combined output."""

    @staticmethod
    def _terminate_container(
        process: subprocess.Popen[bytes], argv: Sequence[str]
    ) -> None:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        try:
            name = argv[argv.index("--name") + 1]
        except (ValueError, IndexError):
            return
        if not _SAFE_KEY.fullmatch(name):
            return
        try:
            subprocess.run(
                (argv[0], "rm", "--force", name),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                env={"LANG": "C", "LC_ALL": "C"},
                close_fds=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> SandboxCommandResult:
        if not argv or not Path(argv[0]).is_absolute():
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_SANDBOX_RUNTIME_PATH", "sandbox runtime must be absolute"
            )
        process = subprocess.Popen(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env={"LANG": "C", "LC_ALL": "C"},
            close_fds=True,
        )
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        started = time.monotonic()
        try:
            while selector.get_map():
                if time.monotonic() - started > timeout_seconds:
                    self._terminate_container(process, argv)
                    raise CanonicalProductionResearchBlocked(
                        "BLOCKED_FREQTRADE_TIMEOUT",
                        "sandbox exceeded its fixed timeout",
                    )
                for key, _mask in selector.select(timeout=0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[key.data].extend(chunk)
                    if sum(len(value) for value in buffers.values()) > max_output_bytes:
                        self._terminate_container(process, argv)
                        raise CanonicalProductionResearchBlocked(
                            "BLOCKED_FREQTRADE_OUTPUT_LIMIT",
                            "sandbox exceeded its combined output limit",
                        )
            return_code = process.wait(timeout=5)
        finally:
            selector.close()
            if process.poll() is None:
                self._terminate_container(process, argv)
        return SandboxCommandResult(
            return_code=return_code,
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
        )


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_RESEARCH_INPUT_JSON", "input evidence is not canonical JSON"
        ) from exc


def _file_digest(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _safe_market_path(root: Path, locator: str) -> Path:
    parts = locator.split("/")
    if (
        not locator
        or locator.startswith("/")
        or "\\" in locator
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_MARKET_ARTIFACT_LOCATOR",
            "market locator must be root-relative POSIX",
        )
    candidate = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_MARKET_ARTIFACT_PATH",
                "market artifact path must not contain symlinks",
            )
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_MARKET_ARTIFACT_PATH", "market artifact escapes its root"
        ) from exc
    if not resolved.is_file():
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_MARKET_ARTIFACT_PATH", "market artifact is not a regular file"
        )
    return resolved


def _validate_mount_source(path: Path, *, code: str) -> None:
    if any(character in str(path) for character in (",", "\n", "\r")):
        raise CanonicalProductionResearchBlocked(
            code, "bind mount source contains an unsafe delimiter"
        )


def _write_read_only(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _copy_verified_read_only(source: Path, destination: Path) -> tuple[str, int]:
    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    destination_descriptor: int | None = None
    digest = sha256()
    size = 0
    try:
        if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_MARKET_ARTIFACT_PATH",
                "market artifact is not a regular file",
            )
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o400,
        )
        while chunk := os.read(source_descriptor, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination_descriptor, remaining)
                remaining = remaining[written:]
        os.fsync(destination_descriptor)
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)
    return digest.hexdigest(), size


@contextmanager
def materialize_production_research_inputs(
    reader_connection: Connection,
    *,
    running_attempt: RunningValidationAttempt,
    market_artifact_root: Path,
    workspace_root: Path,
) -> Iterator[ProductionResearchInputSet]:
    """Materialize a digest-checked, read-only input set for one exact attempt."""

    validate_ephemeral_launch_spec(running_attempt.launch_spec)
    for root, code in (
        (market_artifact_root, "BLOCKED_MARKET_ARTIFACT_ROOT"),
        (workspace_root, "BLOCKED_RESEARCH_WORKSPACE_ROOT"),
    ):
        if (
            not root.is_absolute()
            or not root.is_dir()
            or root.is_symlink()
            or root.resolve(strict=True) != root
        ):
            raise CanonicalProductionResearchBlocked(
                code, "root must be absolute and safe"
            )
        _validate_mount_source(root, code=code)
    market_root = market_artifact_root.resolve(strict=True)
    scratch_root = workspace_root.resolve(strict=True)
    spec = running_attempt.launch_spec
    artifact = (
        reader_connection.execute(
            select(STRATEGY_ARTIFACTS_TABLE).where(
                STRATEGY_ARTIFACTS_TABLE.c.id == spec.artifact_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if artifact is None or str(artifact["encoding"]).upper() != "UTF-8":
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_STRATEGY_ARTIFACT_UNSET", "UTF-8 strategy artifact is required"
        )
    strategy_bytes = artifact["normalized_content"].encode("utf-8")
    if (
        sha256(strategy_bytes).hexdigest() != spec.artifact_digest
        or len(strategy_bytes) != artifact["size_bytes"]
    ):
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_STRATEGY_ARTIFACT_DIGEST_DRIFT",
            "strategy content no longer matches its immutable identity",
        )
    frozen = read_frozen_research_bundle(
        reader_connection,
        configuration_bundle_id=spec.lineage.configuration_bundle_id,
        expected_bundle_digest=spec.lineage.configuration_bundle_digest,
    )
    if (
        frozen.market_snapshot_id != spec.lineage.market_snapshot_id
        or frozen.market_snapshot_digest != spec.lineage.market_snapshot_digest
    ):
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_RESEARCH_INPUT_LINEAGE", "bundle and attempt market lineage differ"
        )
    market_rows = (
        reader_connection.execute(
            select(
                MARKET_ARTIFACTS_TABLE.c.locator,
                MARKET_ARTIFACTS_TABLE.c.content_digest,
                MARKET_ARTIFACTS_TABLE.c.size_bytes,
                MARKET_RECEIPTS_TABLE.c.status,
            )
            .select_from(
                MARKET_SNAPSHOT_MEMBERS_TABLE.join(
                    MARKET_ARTIFACTS_TABLE,
                    MARKET_ARTIFACTS_TABLE.c.id
                    == MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_artifact_id,
                ).join(
                    MARKET_RECEIPTS_TABLE,
                    MARKET_RECEIPTS_TABLE.c.id
                    == MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_receipt_id,
                )
            )
            .where(
                MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_snapshot_id
                == spec.lineage.market_snapshot_id,
                MARKET_SNAPSHOT_MEMBERS_TABLE.c.research_target_id
                == spec.lineage.research_target_id,
            )
        )
        .mappings()
        .all()
    )
    if not market_rows:
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_MARKET_ARTIFACT_UNSET", "target has no sealed market artifact"
        )
    market_sources: list[tuple[Path, str, int, str]] = []
    for index, row in enumerate(sorted(market_rows, key=lambda item: item["locator"])):
        if row["status"] != "ACCEPTED":
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_MARKET_RECEIPT_NOT_ACCEPTED", "market receipt is not accepted"
            )
        path = _safe_market_path(market_root, row["locator"])
        market_sources.append(
            (
                path,
                row["content_digest"],
                row["size_bytes"],
                f"/input/market-{index:04d}.data",
            )
        )
    workspace = Path(
        tempfile.mkdtemp(
            prefix=f"v13-{running_attempt.validation_attempt_id}-", dir=scratch_root
        )
    )
    try:
        mounts: list[ReadOnlyMount] = []
        for source, expected_digest, expected_size, destination in market_sources:
            copied_path = workspace / Path(destination).name
            digest, size = _copy_verified_read_only(source, copied_path)
            if digest != expected_digest or size != expected_size:
                raise CanonicalProductionResearchBlocked(
                    "BLOCKED_MARKET_ARTIFACT_DIGEST_DRIFT",
                    "market artifact differs from canonical evidence",
                )
            mounts.append(
                ReadOnlyMount(
                    source=copied_path,
                    destination=destination,
                    content_digest=digest,
                    size_bytes=size,
                )
            )
        strategy_path = workspace / "strategy.py"
        bundle_path = workspace / "bundle.json"
        plan_path = workspace / "validation-plan.json"
        request_path = workspace / "request.json"
        bundle_payload = {
            "configuration_bundle_id": str(frozen.configuration_bundle_id),
            "configuration_bundle_digest": frozen.configuration_bundle_digest,
            "market_snapshot_id": str(frozen.market_snapshot_id),
            "market_snapshot_digest": frozen.market_snapshot_digest,
            "capability": dict(frozen.capability),
            "configurations": [
                {
                    "configuration_kind": item.configuration_kind,
                    "snapshot_id": str(item.snapshot_id),
                    "snapshot_digest": item.snapshot_digest,
                    "payload": dict(item.payload),
                }
                for item in frozen.configurations
            ],
            "targets": [asdict(item) for item in frozen.targets],
        }
        for target in bundle_payload["targets"]:
            target["research_target_id"] = str(target["research_target_id"])
        plan_payload = {
            "validation_plan_id": str(spec.validation_plan_id),
            "validation_plan_digest": spec.validation_plan_digest,
            "windows": [
                {
                    **asdict(item),
                    "validation_plan_window_id": str(item.validation_plan_window_id),
                    "window_snapshot_member_id": str(item.window_snapshot_member_id),
                    "window_start": item.window_start.isoformat(),
                    "window_end": item.window_end.isoformat(),
                }
                for item in spec.windows
            ],
        }
        request_payload = {
            "contract": "canonical-v13-freqtrade-backtest-request-v1",
            "validation_attempt_id": str(running_attempt.validation_attempt_id),
            "attempt_request_digest": running_attempt.request_digest,
            "artifact_digest": spec.artifact_digest,
            "bundle_digest": frozen.configuration_bundle_digest,
            "market_snapshot_digest": frozen.market_snapshot_digest,
            "validation_plan_digest": spec.validation_plan_digest,
            "market_files": [
                {
                    "path": item.destination,
                    "content_digest": item.content_digest,
                    "size_bytes": item.size_bytes,
                }
                for item in mounts
            ],
        }
        for path, content in (
            (strategy_path, strategy_bytes),
            (bundle_path, _canonical_bytes(bundle_payload)),
            (plan_path, _canonical_bytes(plan_payload)),
            (request_path, _canonical_bytes(request_payload)),
        ):
            _write_read_only(path, content)
        manifest = {
            "contract": "canonical-v13-production-research-input-set-v1",
            "files": {
                path.name: _file_digest(path)[0]
                for path in (strategy_path, bundle_path, plan_path, request_path)
            },
            "market": [
                {
                    "destination": item.destination,
                    "content_digest": item.content_digest,
                    "size_bytes": item.size_bytes,
                }
                for item in mounts
            ],
        }
        workspace.chmod(0o500)
        yield ProductionResearchInputSet(
            workspace=workspace,
            request_path=request_path,
            mounts=tuple(mounts),
            input_manifest_digest=canonical_research_digest(manifest),
        )
    finally:
        workspace.chmod(0o700)
        for child in workspace.iterdir():
            child.chmod(0o600)
        shutil.rmtree(workspace)


class FreqtradeProductionResearchAdapter:
    """One-shot OCI adapter; it never receives a SQLAlchemy connection or DSN."""

    environment_class = "PRODUCTION_RESEARCH"
    network_mode = "none"
    credential_mounts: tuple[str, ...] = ()
    exchange_capabilities: tuple[str, ...] = ()
    order_capabilities: tuple[str, ...] = ()
    writer_capabilities: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        activation: str,
        runtime_path: Path,
        image_reference: str,
        limits: ProductionResearchLimits,
        input_factory: ProductionResearchInputFactory,
        runner: SandboxRunnerPort,
    ) -> None:
        if activation != PRODUCTION_RESEARCH_ACTIVATION:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_PRODUCTION_RESEARCH_NOT_ACTIVATED",
                "exact no-trade production activation is required",
            )
        if (
            not runtime_path.is_absolute()
            or not runtime_path.is_file()
            or runtime_path.is_symlink()
            or runtime_path.resolve(strict=True) != runtime_path
            or not os.access(runtime_path, os.X_OK)
        ):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_SANDBOX_RUNTIME_PATH", "OCI runtime path is unavailable"
            )
        match = _IMAGE.fullmatch(image_reference)
        if match is None:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_EXECUTOR_IMAGE_UNPINNED", "image must be pinned by SHA-256"
            )
        sandbox_uid = os.getuid()
        sandbox_gid = os.getgid()
        if sandbox_uid == 0:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_SANDBOX_ROOT_IDENTITY",
                "production research must run from an unprivileged service account",
            )
        limits.validate()
        self._runtime_path = runtime_path
        self._image_reference = image_reference
        self._image_digest = match.group(1)
        self._sandbox_identity = f"{sandbox_uid}:{sandbox_gid}"
        self._limits = limits
        self._input_factory = input_factory
        self._runner = runner

    def _command(
        self,
        running_attempt: RunningValidationAttempt,
        inputs: ProductionResearchInputSet,
    ) -> tuple[str, ...]:
        name = f"canonical-v13-{running_attempt.validation_attempt_id}"
        if not _SAFE_KEY.fullmatch(name):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_SANDBOX_IDENTITY", "attempt cannot form a safe container name"
            )
        argv = [
            str(self._runtime_path),
            "run",
            "--rm",
            "--init",
            "--stop-timeout",
            "5",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self._limits.pids_limit),
            "--cpus",
            self._limits.cpu_count,
            "--memory",
            f"{self._limits.memory_mb}m",
            "--memory-swap",
            f"{self._limits.memory_mb}m",
            "--user",
            self._sandbox_identity,
            "--tmpfs",
            f"/work:rw,noexec,nosuid,nodev,size={self._limits.tmpfs_mb}m",
            "--mount",
            f"type=bind,src={inputs.workspace},dst=/input,readonly",
        ]
        for mount in inputs.mounts:
            argv.extend(
                (
                    "--mount",
                    f"type=bind,src={mount.source},dst={mount.destination},readonly",
                )
            )
        argv.extend(
            (
                self._image_reference,
                "/opt/freqtrade-ai/bin/canonical-v13-research-worker",
                "backtest",
                "--request",
                "/input/request.json",
                "--bundle",
                "/input/bundle.json",
                "--plan",
                "/input/validation-plan.json",
                "--strategy",
                "/input/strategy.py",
                "--output",
                "-",
            )
        )
        return tuple(argv)

    def _parse_output(
        self, running_attempt: RunningValidationAttempt, raw: bytes
    ) -> EphemeralAttemptReceipt:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_FREQTRADE_OUTPUT_INVALID",
                "worker output is not one JSON object",
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {
            "contract",
            "validation_attempt_id",
            "attempt_request_digest",
            "status",
            "windows",
        }:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_FREQTRADE_OUTPUT_INVALID", "worker output fields drifted"
            )
        if (
            payload["contract"] != "canonical-v13-freqtrade-backtest-output-v1"
            or payload["validation_attempt_id"]
            != str(running_attempt.validation_attempt_id)
            or payload["attempt_request_digest"] != running_attempt.request_digest
            or payload["status"] not in {"SUCCEEDED", "FAILED", "BLOCKED"}
            or not isinstance(payload["windows"], list)
        ):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_FREQTRADE_OUTPUT_LINEAGE", "worker output lineage drifted"
            )
        metrics: dict[str, Mapping[str, object]] = {}
        for item in payload["windows"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"window_key", "window_member_digest", "metrics"}
                or not isinstance(item["metrics"], dict)
                or item["window_key"] in metrics
            ):
                raise CanonicalProductionResearchBlocked(
                    "BLOCKED_FREQTRADE_OUTPUT_INVALID", "window output fields drifted"
                )
            expected = next(
                (
                    window
                    for window in running_attempt.launch_spec.windows
                    if window.required and window.window_key == item["window_key"]
                ),
                None,
            )
            if (
                expected is None
                or expected.window_member_digest != item["window_member_digest"]
            ):
                raise CanonicalProductionResearchBlocked(
                    "BLOCKED_FREQTRADE_OUTPUT_LINEAGE", "window output lineage drifted"
                )
            metrics[item["window_key"]] = MappingProxyType(dict(item["metrics"]))
        return build_ephemeral_attempt_receipt(
            running_attempt,
            metrics_by_window_key=metrics,
            status=payload["status"],
        )

    def execute(
        self, running_attempt: RunningValidationAttempt
    ) -> EphemeralAttemptReceipt:
        if running_attempt.launch_spec.executor_image_digest != self._image_digest:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_EXECUTOR_IMAGE_DIGEST_DRIFT",
                "attempt and pinned OCI image digests differ",
            )
        try:
            with self._input_factory(running_attempt) as inputs:
                result = self._runner.run(
                    self._command(running_attempt, inputs),
                    timeout_seconds=self._limits.timeout_seconds,
                    max_output_bytes=self._limits.max_output_bytes,
                )
                if result.return_code != 0 or result.stderr:
                    raise CanonicalProductionResearchBlocked(
                        "BLOCKED_FREQTRADE_PROCESS_FAILED",
                        "sandbox returned a non-zero code or unexpected stderr",
                    )
                return self._parse_output(running_attempt, result.stdout)
        except CanonicalProductionResearchBlocked:
            return build_ephemeral_attempt_receipt(
                running_attempt,
                metrics_by_window_key={},
                status="BLOCKED",
            )


__all__ = [
    "PRODUCTION_RESEARCH_ACTIVATION",
    "BoundedSubprocessSandboxRunner",
    "CanonicalProductionResearchBlocked",
    "FreqtradeProductionResearchAdapter",
    "ProductionResearchInputFactory",
    "ProductionResearchInputSet",
    "ProductionResearchLimits",
    "ReadOnlyMount",
    "SandboxCommandResult",
    "SandboxRunnerPort",
    "materialize_production_research_inputs",
]
