"""Fail-closed Freqtrade adapter for production no-trade research.

The outer process may read canonical evidence and persist receipts.  The sandboxed
strategy process receives only immutable files, has no database URL or credential
environment, and is launched in a one-shot OCI container with networking disabled.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
from uuid import UUID

from sqlalchemy import Connection, select

from app.canonical_v13.market_acquisition import (
    MarketAcquisitionReceipt,
    verify_market_acquisition_receipt,
)
from app.canonical_v13.models import (
    MARKET_ARTIFACTS_TABLE,
    MARKET_INSPECTIONS_TABLE,
    MARKET_PROFILE_VERSIONS_TABLE,
    MARKET_RECEIPTS_TABLE,
    MARKET_SNAPSHOT_MEMBERS_TABLE,
    MARKET_SNAPSHOTS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_VERSIONS_TABLE,
)
from app.canonical_v13.research_validation import (
    EphemeralAttemptReceipt,
    LOOKAHEAD_BLOCK_REASON_CODES,
    LookaheadAnalysisReceipt,
    LOOKAHEAD_FAILURE_DETAILS,
    LOOKAHEAD_FAILURE_STAGE_BY_CODE,
    LOOKAHEAD_FAILURE_STAGES,
    ResearchLineage,
    RunningValidationAttempt,
    STATIC_VALIDATOR_IDENTITY,
    StaticValidationReceipt,
    build_ephemeral_attempt_receipt,
    build_lookahead_receipt,
    canonical_research_digest,
    static_validator_digest,
    validate_ephemeral_launch_spec,
    validate_static_source,
)
from app.canonical_v13.runtime_reader import read_frozen_research_bundle
from app.canonical_v13.offline_exchange_metadata import verify_offline_exchange_metadata


PRODUCTION_RESEARCH_ACTIVATION = "PRODUCTION_RESEARCH_NO_TRADE_V1"
PRODUCTION_LOOKAHEAD_ACTIVATION = "PRODUCTION_LOOKAHEAD_NO_TRADE_V1"
PRODUCTION_LOOKAHEAD_ANALYZER_IDENTITY = "production-freqtrade-lookahead-v1"
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:([0-9a-f]{64})$")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
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
class ProductionLookaheadInputSet:
    workspace: Path
    request_path: Path
    mounts: tuple[ReadOnlyMount, ...]
    input_manifest_digest: str


@dataclass(frozen=True)
class ProductionStaticLookaheadGateReceipt:
    lineage: ResearchLineage
    static_receipt: StaticValidationReceipt
    lookahead_receipt: LookaheadAnalysisReceipt | None
    status: str
    validation_eligible: bool


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


class ProductionLookaheadInputFactory(Protocol):
    def __call__(
        self, lineage: ResearchLineage
    ) -> AbstractContextManager[ProductionLookaheadInputSet]: ...


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


class RemotePodmanVolumeSandboxRunner(BoundedSubprocessSandboxRunner):
    """Stage materialized inputs into a short-lived remote Podman volume."""

    @staticmethod
    def _run_control(argv: Sequence[str]) -> None:
        result = subprocess.run(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            env={"LANG": "C", "LC_ALL": "C"},
            close_fds=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_SANDBOX_STAGING_FAILED",
                "remote sandbox input staging failed",
            )

    @staticmethod
    def _cleanup(runtime: str, *argv: str) -> None:
        try:
            subprocess.run(
                (runtime, *argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                env={"LANG": "C", "LC_ALL": "C"},
                close_fds=True,
                check=False,
                timeout=10,
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
        command = list(argv)
        try:
            container_name = command[command.index("--name") + 1]
            image_index = next(
                index
                for index, value in enumerate(command)
                if _IMAGE.fullmatch(value) is not None
            )
            user_identity = command[command.index("--user") + 1]
        except (StopIteration, ValueError, IndexError):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_SANDBOX_STAGING_COMMAND",
                "remote sandbox command cannot be staged",
            ) from None
        if not _SAFE_KEY.fullmatch(container_name):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_SANDBOX_IDENTITY", "sandbox name is invalid"
            )
        volume_name = f"{container_name}-input"
        staging_name = f"{container_name}-stage"
        bind_mounts: list[tuple[Path, str]] = []
        rewritten: list[str] = []
        index = 0
        while index < len(command):
            if index + 1 < len(command) and command[index] == "--mount":
                fields = dict(
                    field.split("=", 1)
                    for field in command[index + 1].split(",")
                    if "=" in field
                )
                if fields.get("type") == "bind":
                    source = Path(fields.get("src", ""))
                    destination = fields.get("dst", "")
                    if (
                        not source.is_absolute()
                        or not source.exists()
                        or not destination.startswith("/input")
                    ):
                        raise CanonicalProductionResearchBlocked(
                            "BLOCKED_SANDBOX_STAGING_INPUT",
                            "remote sandbox input is invalid",
                        )
                    bind_mounts.append((source, destination))
                    index += 2
                    continue
            rewritten.append(command[index])
            index += 1
        if not bind_mounts or bind_mounts[0][1] != "/input":
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_SANDBOX_STAGING_INPUT",
                "remote sandbox root input is missing",
            )
        runtime = command[0]
        image = command[image_index]
        rewritten_image_index = image_index - (2 * len(bind_mounts))
        try:
            self._run_control((runtime, "volume", "create", volume_name))
            self._run_control(
                (
                    runtime,
                    "create",
                    "--name",
                    staging_name,
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--user",
                    user_identity,
                    "--mount",
                    f"type=volume,src={volume_name},dst=/input",
                    image,
                )
            )
            for source, destination in bind_mounts:
                source_spec = f"{source}/." if source.is_dir() else str(source)
                self._run_control(
                    (runtime, "cp", source_spec, f"{staging_name}:{destination}")
                )
            rewritten[rewritten_image_index:rewritten_image_index] = [
                "--mount",
                f"type=volume,src={volume_name},dst=/input,readonly",
            ]
            return super().run(
                rewritten,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            )
        finally:
            self._cleanup(runtime, "rm", "--force", staging_name)
            self._cleanup(runtime, "volume", "rm", "--force", volume_name)


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


def _validated_roots(
    market_artifact_root: Path, workspace_root: Path
) -> tuple[Path, Path]:
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
    return (
        market_artifact_root.resolve(strict=True),
        workspace_root.resolve(strict=True),
    )


def _frozen_bundle_payload(frozen: object) -> dict[str, object]:
    payload = {
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
    for target in payload["targets"]:
        target["research_target_id"] = str(target["research_target_id"])
    return payload


def _strategy_artifact_for_lineage(
    reader_connection: Connection, lineage: ResearchLineage
) -> Mapping[str, object]:
    artifact = (
        reader_connection.execute(
            select(STRATEGY_ARTIFACTS_TABLE)
            .select_from(
                STRATEGY_VERSIONS_TABLE.join(
                    STRATEGY_ARTIFACTS_TABLE,
                    STRATEGY_ARTIFACTS_TABLE.c.id
                    == STRATEGY_VERSIONS_TABLE.c.artifact_id,
                )
            )
            .where(STRATEGY_VERSIONS_TABLE.c.id == lineage.strategy_version_id)
        )
        .mappings()
        .one_or_none()
    )
    if artifact is None or str(artifact["encoding"]).upper() != "UTF-8":
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_STRATEGY_ARTIFACT_UNSET", "UTF-8 strategy artifact is required"
        )
    strategy_bytes = str(artifact["normalized_content"]).encode("utf-8")
    if (
        sha256(strategy_bytes).hexdigest() != artifact["content_digest"]
        or len(strategy_bytes) != artifact["size_bytes"]
    ):
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_STRATEGY_ARTIFACT_DIGEST_DRIFT",
            "strategy content no longer matches its immutable identity",
        )
    return artifact


def validate_production_static_gate(
    reader_connection: Connection, *, lineage: ResearchLineage
) -> StaticValidationReceipt:
    """Run the non-executing AST gate without creating a plan or DB row."""

    artifact = _strategy_artifact_for_lineage(reader_connection, lineage)
    return validate_static_source(
        str(artifact["normalized_content"]),
        strategy_version_id=lineage.strategy_version_id,
        expected_artifact_digest=str(artifact["content_digest"]),
        validator_identity=STATIC_VALIDATOR_IDENTITY,
        validator_digest=static_validator_digest(),
    )


@contextmanager
def materialize_production_lookahead_inputs(
    reader_connection: Connection,
    *,
    lineage: ResearchLineage,
    market_artifact_root: Path,
    workspace_root: Path,
    observed_at: datetime | None = None,
) -> Iterator[ProductionLookaheadInputSet]:
    """Materialize one fresh, digest-bound lookahead input set without DB writes."""

    market_root, scratch_root = _validated_roots(
        market_artifact_root, workspace_root
    )
    artifact = _strategy_artifact_for_lineage(reader_connection, lineage)
    strategy_bytes = str(artifact["normalized_content"]).encode("utf-8")
    frozen = read_frozen_research_bundle(
        reader_connection,
        configuration_bundle_id=lineage.configuration_bundle_id,
        expected_bundle_digest=lineage.configuration_bundle_digest,
    )
    if (
        frozen.market_snapshot_id != lineage.market_snapshot_id
        or frozen.market_snapshot_digest != lineage.market_snapshot_digest
        or {target.research_target_id for target in frozen.targets}
        != {lineage.research_target_id}
    ):
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_RESEARCH_INPUT_LINEAGE",
            "lookahead lineage differs from the frozen target/bundle/market set",
        )
    window_payload = next(
        (
            dict(item.payload)
            for item in frozen.configurations
            if item.configuration_kind == "WINDOW"
        ),
        None,
    )
    raw_windows = None if window_payload is None else window_payload.get("windows")
    if not isinstance(raw_windows, list):
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_RESEARCH_WINDOWS_UNSET", "WINDOW payload is unavailable"
        )
    required_window_rows = [
        row
        for row in raw_windows
        if isinstance(row, dict) and row.get("required") is True
    ]
    if not required_window_rows or any(
        not isinstance(row.get("coverage"), dict)
        or isinstance(row["coverage"].get("freshness_max_age_seconds"), bool)
        or not isinstance(row["coverage"].get("freshness_max_age_seconds"), int)
        or row["coverage"]["freshness_max_age_seconds"] <= 0
        for row in required_window_rows
    ):
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_MARKET_FRESHNESS_CONTRACT",
            "every required window needs one positive freshness limit",
        )
    freshness_limits = {
        int(row["coverage"]["freshness_max_age_seconds"])
        for row in required_window_rows
    }
    if len(freshness_limits) != 1:
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_MARKET_FRESHNESS_CONTRACT",
            "required windows must share one explicit freshness limit",
        )
    freshness_limit = next(iter(freshness_limits))
    market_rows = (
        reader_connection.execute(
            select(
                MARKET_ARTIFACTS_TABLE.c.locator,
                MARKET_ARTIFACTS_TABLE.c.content_digest,
                MARKET_ARTIFACTS_TABLE.c.size_bytes,
                MARKET_SNAPSHOT_MEMBERS_TABLE.c.coverage_start,
                MARKET_SNAPSHOT_MEMBERS_TABLE.c.coverage_end,
                MARKET_RECEIPTS_TABLE.c.status,
                MARKET_RECEIPTS_TABLE.c.artifact_digest,
                MARKET_RECEIPTS_TABLE.c.inspection_digest.label(
                    "receipt_inspection_digest"
                ),
                MARKET_RECEIPTS_TABLE.c.receipt_digest,
                MARKET_INSPECTIONS_TABLE.c.inspection_json,
                MARKET_INSPECTIONS_TABLE.c.inspection_digest,
            )
            .select_from(
                MARKET_SNAPSHOT_MEMBERS_TABLE.join(
                    MARKET_ARTIFACTS_TABLE,
                    MARKET_ARTIFACTS_TABLE.c.id
                    == MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_artifact_id,
                )
                .join(
                    MARKET_RECEIPTS_TABLE,
                    MARKET_RECEIPTS_TABLE.c.id
                    == MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_receipt_id,
                )
                .join(
                    MARKET_INSPECTIONS_TABLE,
                    MARKET_INSPECTIONS_TABLE.c.id
                    == MARKET_RECEIPTS_TABLE.c.market_inspection_id,
                )
            )
            .where(
                MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_snapshot_id
                == lineage.market_snapshot_id,
                MARKET_SNAPSHOT_MEMBERS_TABLE.c.research_target_id
                == lineage.research_target_id,
            )
        )
        .mappings()
        .all()
    )
    if not market_rows:
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_MARKET_ARTIFACT_UNSET", "target has no sealed market artifact"
        )
    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    frozen_target = frozen.targets[0]
    profile = reader_connection.execute(
        select(MARKET_PROFILE_VERSIONS_TABLE)
        .select_from(
            MARKET_SNAPSHOTS_TABLE.join(
                MARKET_PROFILE_VERSIONS_TABLE,
                MARKET_PROFILE_VERSIONS_TABLE.c.id
                == MARKET_SNAPSHOTS_TABLE.c.market_profile_version_id,
            )
        )
        .where(MARKET_SNAPSHOTS_TABLE.c.id == lineage.market_snapshot_id)
    ).mappings().one_or_none()
    metadata_binding = (
        profile["payload_json"].get("offline_exchange_metadata")
        if profile is not None and isinstance(profile["payload_json"], dict)
        else None
    )
    if (
        profile is None
        or profile["lifecycle_status"] != "VALIDATED"
        or canonical_research_digest(profile["payload_json"]) != profile["payload_digest"]
        or not isinstance(metadata_binding, dict)
    ):
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_OFFLINE_EXCHANGE_METADATA_UNSET",
            "validated market profile has no frozen exchange metadata",
        )
    try:
        metadata_artifact_id = UUID(str(metadata_binding["artifact_id"]))
        metadata_receipt_id = UUID(str(metadata_binding["receipt_id"]))
    except (KeyError, ValueError) as exc:
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_OFFLINE_EXCHANGE_METADATA_LINEAGE", "metadata identity is invalid"
        ) from exc
    metadata_row = reader_connection.execute(
        select(
            MARKET_ARTIFACTS_TABLE.c.locator,
            MARKET_ARTIFACTS_TABLE.c.content_digest,
            MARKET_ARTIFACTS_TABLE.c.size_bytes,
            MARKET_ARTIFACTS_TABLE.c.media_type,
            MARKET_RECEIPTS_TABLE.c.status,
            MARKET_RECEIPTS_TABLE.c.receipt_digest,
            MARKET_INSPECTIONS_TABLE.c.inspection_json,
            MARKET_INSPECTIONS_TABLE.c.inspection_digest,
        )
        .select_from(
            MARKET_ARTIFACTS_TABLE.join(
                MARKET_RECEIPTS_TABLE,
                MARKET_RECEIPTS_TABLE.c.market_artifact_id == MARKET_ARTIFACTS_TABLE.c.id,
            ).join(
                MARKET_INSPECTIONS_TABLE,
                MARKET_INSPECTIONS_TABLE.c.id == MARKET_RECEIPTS_TABLE.c.market_inspection_id,
            )
        )
        .where(
            MARKET_ARTIFACTS_TABLE.c.id == metadata_artifact_id,
            MARKET_RECEIPTS_TABLE.c.id == metadata_receipt_id,
        )
    ).mappings().one_or_none()
    if (
        metadata_row is None
        or metadata_row["status"] != "ACCEPTED"
        or metadata_row["content_digest"] != metadata_binding.get("artifact_digest")
        or metadata_row["receipt_digest"] != metadata_binding.get("receipt_digest")
        or metadata_row["locator"] != metadata_binding.get("artifact_locator")
        or not isinstance(metadata_row["inspection_json"], dict)
        or metadata_row["inspection_json"].get("contract")
        != "canonical-v13-offline-exchange-metadata-inspection-v1"
        or metadata_row["inspection_json"].get("acquisition_receipt_digest")
        != metadata_binding.get("acquisition_receipt_digest")
        or canonical_research_digest(metadata_row["inspection_json"])
        != metadata_row["inspection_digest"]
        or canonical_research_digest(
            {
                "artifact_digest": metadata_row["content_digest"],
                "inspection_digest": metadata_row["inspection_digest"],
                "status": "ACCEPTED",
            }
        )
        != metadata_row["receipt_digest"]
    ):
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_OFFLINE_EXCHANGE_METADATA_LINEAGE", "metadata receipt drifted"
        )
    metadata_source = _safe_market_path(market_root, str(metadata_row["locator"]))
    metadata_content = metadata_source.read_bytes()
    try:
        metadata_payload = verify_offline_exchange_metadata(
            metadata_content,
            expected_digest=str(metadata_row["content_digest"]),
            observed_at=now,
            expected_receipt_digest=str(
                metadata_binding.get("acquisition_receipt_digest")
            ),
        )
    except Exception as exc:
        raise CanonicalProductionResearchBlocked(
            getattr(exc, "code", "BLOCKED_OFFLINE_EXCHANGE_METADATA"),
            "frozen offline exchange metadata is invalid",
        ) from exc
    if (
        metadata_payload.get("target_snapshot_id")
        != str(next(item.snapshot_id for item in frozen.configurations if item.configuration_kind == "TARGET"))
        or metadata_payload.get("window_snapshot_id")
        != str(next(item.snapshot_id for item in frozen.configurations if item.configuration_kind == "WINDOW"))
        or metadata_payload.get("target_key") != frozen_target.target_key
        or metadata_payload.get("pair") != frozen_target.pair
    ):
        raise CanonicalProductionResearchBlocked(
            "BLOCKED_OFFLINE_EXCHANGE_METADATA_LINEAGE", "metadata target/bundle binding drifted"
        )
    market_sources: list[tuple[Path, str, int, str]] = []
    for index, row in enumerate(sorted(market_rows, key=lambda item: item["locator"])):
        inspection = row["inspection_json"]
        acquisition_json = (
            inspection.get("acquisition_receipt_json")
            if isinstance(inspection, dict)
            else None
        )
        try:
            acquisition_receipt = MarketAcquisitionReceipt(**acquisition_json)
        except (TypeError, ValueError):
            acquisition_receipt = None
        if (
            row["status"] != "ACCEPTED"
            or not isinstance(inspection, dict)
            or canonical_research_digest(inspection) != row["inspection_digest"]
            or row["artifact_digest"] != row["content_digest"]
            or row["receipt_inspection_digest"] != row["inspection_digest"]
            or canonical_research_digest(
                {
                    "artifact_digest": row["content_digest"],
                    "inspection_digest": row["inspection_digest"],
                    "status": "ACCEPTED",
                }
            )
            != row["receipt_digest"]
            or inspection.get("provenance_class") != "PRODUCTION_PUBLIC_MARKET_DATA"
            or acquisition_receipt is None
            or not verify_market_acquisition_receipt(acquisition_receipt)
            or acquisition_receipt.receipt_digest
            != inspection.get("acquisition_receipt_digest")
            or acquisition_receipt.content_digest != row["content_digest"]
            or acquisition_receipt.acquired_at != inspection.get("acquired_at")
            or acquisition_receipt.observed_closed_candles
            != inspection.get("row_count")
            or acquisition_receipt.observed_first_open
            != inspection.get("first_open_at")
            or acquisition_receipt.observed_last_close
            != inspection.get("last_close_at")
            or acquisition_receipt.target_key != frozen_target.target_key
            or acquisition_receipt.instrument != frozen_target.instrument
            or acquisition_receipt.pair != frozen_target.pair
            or acquisition_receipt.timeframe != frozen_target.timeframe
            or acquisition_receipt.data_kind != frozen_target.data_kind
            or acquisition_receipt.credential_access != "NONE"
            or acquisition_receipt.network_access != "PUBLIC_MARKET_DATA_ONLY"
        ):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_MARKET_RECEIPT_NOT_ACCEPTED",
                "market receipt is not accepted public-only evidence",
            )
        try:
            acquired_at = datetime.fromisoformat(str(inspection["acquired_at"]))
        except (KeyError, ValueError) as exc:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_MARKET_FRESHNESS_CONTRACT",
                "market acquisition time is invalid",
            ) from exc
        if (
            str(inspection.get("first_open_at"))
            != row["coverage_start"].astimezone(timezone.utc).isoformat()
            or str(inspection.get("last_close_at"))
            != row["coverage_end"].astimezone(timezone.utc).isoformat()
        ):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_MARKET_COVERAGE_DRIFT",
                "snapshot coverage differs from inspected public evidence",
            )
        if (
            acquired_at.tzinfo is None
            or not 0 <= (now - acquired_at).total_seconds() <= freshness_limit
        ):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_MARKET_FRESHNESS_EXPIRED",
                "market acquisition is outside the frozen freshness limit",
            )
        market_sources.append(
            (
                _safe_market_path(market_root, str(row["locator"])),
                str(row["content_digest"]),
                int(row["size_bytes"]),
                f"/input/market-{index:04d}.data",
            )
        )
    workspace = Path(
        tempfile.mkdtemp(prefix=f"v13-lookahead-{lineage.strategy_version_id}-", dir=scratch_root)
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
            mounts.append(ReadOnlyMount(copied_path, destination, digest, size))
        metadata_path = workspace / "exchange-metadata.json"
        metadata_digest, metadata_size = _copy_verified_read_only(
            metadata_source, metadata_path
        )
        if (
            metadata_digest != metadata_row["content_digest"]
            or metadata_size != metadata_row["size_bytes"]
        ):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_OFFLINE_EXCHANGE_METADATA_DIGEST", "metadata file drifted"
            )
        metadata_mount = ReadOnlyMount(
            metadata_path,
            "/input/exchange-metadata.json",
            metadata_digest,
            metadata_size,
        )
        mounts.append(metadata_mount)
        required_windows = [item for item in frozen.windows if item.required]
        if not required_windows:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_RESEARCH_WINDOWS_UNSET", "required window set is empty"
            )
        request_without_digest = {
            "contract": "canonical-v13-freqtrade-lookahead-request-v1",
            "strategy_version_id": str(lineage.strategy_version_id),
            "research_target_id": str(lineage.research_target_id),
            "artifact_digest": str(artifact["content_digest"]),
            "bundle_digest": frozen.configuration_bundle_digest,
            "market_snapshot_digest": frozen.market_snapshot_digest,
            "windows": [
                {
                    "window_key": item.window_key,
                    "window_member_digest": item.member_digest,
                    "window_start": item.start_at,
                    "window_end": item.end_at,
                    "minimum_closed_candles": item.minimum_closed_candles,
                }
                for item in required_windows
            ],
            "market_files": [
                {
                    "path": item.destination,
                    "content_digest": item.content_digest,
                    "size_bytes": item.size_bytes,
                }
                for item in mounts
                if item.destination.startswith("/input/market-")
            ],
            "exchange_metadata": {
                "path": metadata_mount.destination,
                "content_digest": metadata_mount.content_digest,
                "size_bytes": metadata_mount.size_bytes,
                "receipt_digest": metadata_row["receipt_digest"],
            },
        }
        request_payload = {
            **request_without_digest,
            "request_digest": canonical_research_digest(request_without_digest),
        }
        strategy_path = workspace / "strategy.py"
        bundle_path = workspace / "bundle.json"
        request_path = workspace / "lookahead-request.json"
        for path, content in (
            (strategy_path, strategy_bytes),
            (bundle_path, _canonical_bytes(_frozen_bundle_payload(frozen))),
            (request_path, _canonical_bytes(request_payload)),
        ):
            _write_read_only(path, content)
        manifest = {
            "contract": "canonical-v13-production-lookahead-input-set-v1",
            "files": {
                path.name: _file_digest(path)[0]
                for path in (strategy_path, bundle_path, request_path)
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
        yield ProductionLookaheadInputSet(
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


class FreqtradeProductionLookaheadAdapter:
    """One-shot OCI lookahead gate that cannot create a plan or attempt."""

    environment_class = "PRODUCTION_LOOKAHEAD_GATE"
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
        input_factory: ProductionLookaheadInputFactory,
        runner: SandboxRunnerPort,
    ) -> None:
        if activation != PRODUCTION_LOOKAHEAD_ACTIVATION:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_PRODUCTION_LOOKAHEAD_NOT_ACTIVATED",
                "exact no-trade lookahead activation is required",
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
        if os.getuid() == 0:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_SANDBOX_ROOT_IDENTITY",
                "production lookahead must run from an unprivileged service account",
            )
        limits.validate()
        self._runtime_path = runtime_path
        self._image_reference = image_reference
        self._image_digest = match.group(1)
        self._sandbox_identity = f"{os.getuid()}:{os.getgid()}"
        self._limits = limits
        self._input_factory = input_factory
        self._runner = runner

    def _command(
        self, lineage: ResearchLineage, inputs: ProductionLookaheadInputSet
    ) -> tuple[str, ...]:
        name = f"canonical-v13-lookahead-{lineage.strategy_version_id}"
        if not _SAFE_KEY.fullmatch(name):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_SANDBOX_IDENTITY", "lineage cannot form a safe container name"
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
                "lookahead",
                "--request",
                "/input/lookahead-request.json",
                "--bundle",
                "/input/bundle.json",
                "--strategy",
                "/input/strategy.py",
                "--output",
                "-",
            )
        )
        return tuple(argv)

    def _parse_output(
        self,
        *,
        lineage: ResearchLineage,
        artifact_digest: str,
        request: Mapping[str, object],
        raw: bytes,
    ) -> LookaheadAnalysisReceipt:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_LOOKAHEAD_OUTPUT_INVALID",
                "worker output is not one JSON object",
            ) from exc
        required = {
            "contract",
            "request_digest",
            "strategy_version_id",
            "research_target_id",
            "status",
            "has_bias",
            "observed_signal_count",
            "blocked_observed_trade_count",
            "blocked_required_trade_count",
            "window_results",
            "failure_stage",
            "failure_code",
            "tool_return_code",
            "stdout_digest",
            "stderr_digest",
            "redacted_detail",
            "evidence_digest",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_LOOKAHEAD_OUTPUT_INVALID", "worker output fields drifted"
            )
        evidence = {key: value for key, value in payload.items() if key != "evidence_digest"}
        if (
            payload["contract"] != "canonical-v13-freqtrade-lookahead-output-v3"
            or payload["request_digest"] != request.get("request_digest")
            or payload["strategy_version_id"] != str(lineage.strategy_version_id)
            or payload["research_target_id"] != str(lineage.research_target_id)
            or payload["status"] not in {"PASSED", "FAILED", "BLOCKED"}
            or payload["has_bias"] not in {True, False, None}
            or isinstance(payload["observed_signal_count"], bool)
            or not isinstance(payload["observed_signal_count"], int)
            or payload["observed_signal_count"] < 0
            or not isinstance(payload["window_results"], list)
            or not isinstance(payload["stdout_digest"], str)
            or not isinstance(payload["stderr_digest"], str)
            or _HEX_DIGEST.fullmatch(payload["stdout_digest"]) is None
            or _HEX_DIGEST.fullmatch(payload["stderr_digest"]) is None
            or canonical_research_digest(evidence) != payload["evidence_digest"]
        ):
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_LOOKAHEAD_OUTPUT_LINEAGE", "worker output lineage drifted"
            )
        expected_windows = request.get("windows")
        if not isinstance(expected_windows, list) or not expected_windows:
            raise CanonicalProductionResearchBlocked(
                "BLOCKED_LOOKAHEAD_OUTPUT_LINEAGE", "required window set is unavailable"
            )
        if payload["status"] == "BLOCKED":
            blocked_reason = payload["failure_code"]
            blocked_observed = payload["blocked_observed_trade_count"]
            blocked_required = payload["blocked_required_trade_count"]
            if (
                payload["has_bias"] is not None
                or payload["observed_signal_count"] != 0
                or payload["window_results"]
                or payload["failure_stage"] not in LOOKAHEAD_FAILURE_STAGES
                or payload["failure_code"] not in LOOKAHEAD_FAILURE_DETAILS
                or payload["failure_stage"]
                != LOOKAHEAD_FAILURE_STAGE_BY_CODE.get(payload["failure_code"])
                or payload["redacted_detail"]
                != LOOKAHEAD_FAILURE_DETAILS.get(payload["failure_code"])
                or (
                    payload["tool_return_code"] is not None
                    and (
                        isinstance(payload["tool_return_code"], bool)
                        or not isinstance(payload["tool_return_code"], int)
                        or not -255 <= payload["tool_return_code"] <= 255
                    )
                )
                or not isinstance(blocked_reason, str)
                or blocked_reason not in LOOKAHEAD_BLOCK_REASON_CODES
            ):
                raise CanonicalProductionResearchBlocked(
                    "BLOCKED_LOOKAHEAD_OUTPUT_LINEAGE",
                    "blocked worker output is internally inconsistent",
                )
            if blocked_reason == "LOOKAHEAD_INSUFFICIENT_TRADES":
                if (
                    any(
                        isinstance(value, bool) or not isinstance(value, int)
                        for value in (blocked_observed, blocked_required)
                    )
                    or blocked_observed < 0
                    or blocked_required <= blocked_observed
                ):
                    raise CanonicalProductionResearchBlocked(
                        "BLOCKED_LOOKAHEAD_OUTPUT_LINEAGE",
                        "insufficient-trade evidence is invalid",
                    )
            elif blocked_observed is not None or blocked_required is not None:
                raise CanonicalProductionResearchBlocked(
                    "BLOCKED_LOOKAHEAD_OUTPUT_LINEAGE",
                    "blocked trade counts do not match the reason",
                )
        else:
            if (
                payload["failure_stage"] is not None
                or payload["failure_code"] is not None
                or payload["redacted_detail"] is not None
                or payload["tool_return_code"] != 0
                or payload["blocked_observed_trade_count"] is not None
                or payload["blocked_required_trade_count"] is not None
            ):
                raise CanonicalProductionResearchBlocked(
                    "BLOCKED_LOOKAHEAD_OUTPUT_LINEAGE",
                    "successful lookahead output contains failure diagnostics",
                )
            expected_bindings = [
                (item.get("window_key"), item.get("window_member_digest"))
                for item in expected_windows
                if isinstance(item, dict)
            ]
            window_results = payload["window_results"]
            actual_bindings: list[tuple[object, object]] = []
            total_signals = 0
            any_bias = False
            for item in window_results:
                if not isinstance(item, dict) or set(item) != {
                    "window_key",
                    "window_member_digest",
                    "has_bias",
                    "observed_signal_count",
                    "biased_entry_signal_count",
                    "biased_exit_signal_count",
                }:
                    raise CanonicalProductionResearchBlocked(
                        "BLOCKED_LOOKAHEAD_OUTPUT_LINEAGE",
                        "window result fields drifted",
                    )
                counts = (
                    item["observed_signal_count"],
                    item["biased_entry_signal_count"],
                    item["biased_exit_signal_count"],
                )
                if (
                    item["has_bias"] not in {True, False}
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                        for value in counts
                    )
                ):
                    raise CanonicalProductionResearchBlocked(
                        "BLOCKED_LOOKAHEAD_OUTPUT_LINEAGE",
                        "window result values are invalid",
                    )
                actual_bindings.append(
                    (item["window_key"], item["window_member_digest"])
                )
                total_signals += item["observed_signal_count"]
                any_bias = any_bias or item["has_bias"]
            if (
                actual_bindings != expected_bindings
                or payload["observed_signal_count"] != total_signals
                or payload["has_bias"] != any_bias
                or payload["status"] != ("FAILED" if any_bias else "PASSED")
            ):
                raise CanonicalProductionResearchBlocked(
                    "BLOCKED_LOOKAHEAD_OUTPUT_LINEAGE",
                    "required window output is incomplete or inconsistent",
                )
        return build_lookahead_receipt(
            lineage=lineage,
            artifact_digest=artifact_digest,
            analyzer_identity=PRODUCTION_LOOKAHEAD_ANALYZER_IDENTITY,
            analyzer_digest=self._image_digest,
            evidence_digest=str(payload["evidence_digest"]),
            status=str(payload["status"]),
            has_bias=payload["has_bias"],
            observed_signal_count=int(payload["observed_signal_count"]),
            failure_stage=payload["failure_stage"],
            failure_code=payload["failure_code"],
            tool_return_code=payload["tool_return_code"],
            stdout_digest=str(payload["stdout_digest"]),
            stderr_digest=str(payload["stderr_digest"]),
            redacted_detail=payload["redacted_detail"],
            blocked_observed_trade_count=payload["blocked_observed_trade_count"],
            blocked_required_trade_count=payload["blocked_required_trade_count"],
        )

    def execute(
        self, *, lineage: ResearchLineage, artifact_digest: str
    ) -> LookaheadAnalysisReceipt:
        with self._input_factory(lineage) as inputs:
            request = json.loads(inputs.request_path.read_text(encoding="utf-8"))
            result = self._runner.run(
                self._command(lineage, inputs),
                timeout_seconds=self._limits.timeout_seconds,
                max_output_bytes=self._limits.max_output_bytes,
            )
            if result.return_code != 0 or result.stderr:
                raise CanonicalProductionResearchBlocked(
                    "BLOCKED_LOOKAHEAD_PROCESS_FAILED",
                    "sandbox returned a non-zero code or unexpected stderr",
                )
            return self._parse_output(
                lineage=lineage,
                artifact_digest=artifact_digest,
                request=request,
                raw=result.stdout,
            )


def execute_production_static_lookahead_gate(
    reader_connection: Connection,
    *,
    lineage: ResearchLineage,
    adapter: FreqtradeProductionLookaheadAdapter,
) -> ProductionStaticLookaheadGateReceipt:
    """Execute only static then lookahead; never persist or create a plan."""

    static_receipt = validate_production_static_gate(
        reader_connection, lineage=lineage
    )
    if static_receipt.status != "PASSED" or static_receipt.findings:
        return ProductionStaticLookaheadGateReceipt(
            lineage=lineage,
            static_receipt=static_receipt,
            lookahead_receipt=None,
            status="STATIC_FAILED",
            validation_eligible=False,
        )
    lookahead_receipt = adapter.execute(
        lineage=lineage, artifact_digest=static_receipt.artifact_digest
    )
    eligible = (
        lookahead_receipt.status == "PASSED"
        and lookahead_receipt.has_bias is False
        and lookahead_receipt.observed_signal_count > 0
    )
    if eligible:
        status = "PASSED"
    elif lookahead_receipt.status == "FAILED" and lookahead_receipt.has_bias is True:
        status = "LOOKAHEAD_FAILED"
    else:
        status = "LOOKAHEAD_BLOCKED"
    return ProductionStaticLookaheadGateReceipt(
        lineage=lineage,
        static_receipt=static_receipt,
        lookahead_receipt=lookahead_receipt,
        status=status,
        validation_eligible=eligible,
    )


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
    "PRODUCTION_LOOKAHEAD_ACTIVATION",
    "PRODUCTION_LOOKAHEAD_ANALYZER_IDENTITY",
    "PRODUCTION_RESEARCH_ACTIVATION",
    "BoundedSubprocessSandboxRunner",
    "CanonicalProductionResearchBlocked",
    "FreqtradeProductionLookaheadAdapter",
    "FreqtradeProductionResearchAdapter",
    "ProductionLookaheadInputFactory",
    "ProductionLookaheadInputSet",
    "ProductionResearchInputFactory",
    "ProductionResearchInputSet",
    "ProductionResearchLimits",
    "ProductionStaticLookaheadGateReceipt",
    "ReadOnlyMount",
    "SandboxCommandResult",
    "SandboxRunnerPort",
    "execute_production_static_lookahead_gate",
    "materialize_production_lookahead_inputs",
    "materialize_production_research_inputs",
    "validate_production_static_gate",
]
