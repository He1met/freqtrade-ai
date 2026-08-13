#!/usr/bin/env python3
"""Private, local-only supervisor maintenance control state.

The module deliberately has no database, credential, or network imports.  It
only persists an owner-private fence which suppresses automatic local runtime
actions while a separately authorized migration owner performs a cutover.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple
from uuid import UUID, uuid4


CONTROL_SCHEMA_VERSION = "supervisor-control-v1"
GENERATION_SCHEMA_VERSION = "supervisor-control-generation-v1"
OBSERVATION_SCHEMA_VERSION = "supervisor-control-observation-v2"
CONTROL_STATE_FILE = "supervisor-control.json"
CONTROL_GENERATION_FILE = "supervisor-control.generation.json"
CONTROL_OBSERVATION_FILE = "supervisor-control.observed.json"
LEGACY_CHILD_SNAPSHOT_FILE = "supervisor-control.legacy-children.json"
LEGACY_RETIREMENT_FILE = "supervisor-control.legacy-retired.json"
CONTROL_LOCK_FILE = "supervisor-control.lock"
CONTROL_MODE_ACTIVE = "ACTIVE"
CONTROL_MODE_MIGRATION_SUSPENDED = "MIGRATION_SUSPENDED"
CONTROL_MODES = frozenset(
    {CONTROL_MODE_ACTIVE, CONTROL_MODE_MIGRATION_SUSPENDED}
)
OBSERVED_MODE_FAIL_CLOSED = "INVALID_FAIL_CLOSED"
OBSERVED_MODES = frozenset({*CONTROL_MODES, OBSERVED_MODE_FAIL_CLOSED})
GENERATION_PENDING = "PENDING_CONTROL"
GENERATION_COMMITTED = "COMMITTED"
GENERATION_STATES = frozenset({GENERATION_PENDING, GENERATION_COMMITTED})
CONTROL_TARGET_SCHEMA_VERSION = "20260813_47"
LAUNCHD_LABEL = "com.he1met.freqtrade-ai.runtime"
MAX_CONTROL_FILE_BYTES = 16 * 1024
MAX_GENERATION = 99_999_999_999_999_999_999
GENERATION_WIDTH = 20
MAX_OBSERVATION_AGE_SECONDS = 120
PROBE_TIMEOUT_SECONDS = 5
SUPERVISOR_DRAIN_TIMEOUT_SECONDS = 120

CONTROL_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "cutover_generation",
        "request_id",
        "operator_identity",
        "reason",
        "target_schema_version",
        "requested_at",
        "updated_at",
    }
)
GENERATION_FIELDS = frozenset(
    {
        "schema_version",
        "last_issued_generation",
        "request_id",
        "operator_identity",
        "reason",
        "target_schema_version",
        "requested_at",
        "commit_state",
    }
)
OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "supervisor_pid",
        "supervisor_start_token",
        "supervisor_instance_id",
        "supervisor_command_sha256",
        "supervisor_cwd",
        "supervisor_launchd_label",
        "supervisor_release_sha256",
        "supervisor_runtime_schema_version",
        "observed_generation",
        "observed_request_id",
        "observed_mode",
        "observed_at",
    }
)
LEGACY_RETIREMENT_SCHEMA_VERSION = "supervisor-control-legacy-retired-v2"
LEGACY_CHILD_SNAPSHOT_SCHEMA_VERSION = "supervisor-control-legacy-children-v1"
LEGACY_CHILD_SERVICES = frozenset(
    {"backend", "worker", "frontend", "okx_runtime"}
)
LEGACY_CHILD_FIELDS = frozenset(
    {
        "service",
        "pid",
        "pgid",
        "start_token",
        "command_sha256",
        "cwd",
    }
)
LEGACY_CHILD_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "cutover_generation",
        "request_id",
        "supervisor_pid",
        "supervisor_start_token",
        "supervisor_command_sha256",
        "supervisor_cwd",
        "supervisor_release_sha256",
        "supervisor_runtime_schema_version",
        "children",
        "captured_at",
    }
)
LEGACY_RETIREMENT_FIELDS = frozenset(
    {
        "schema_version",
        "cutover_generation",
        "request_id",
        "supervisor_pid",
        "supervisor_start_token",
        "supervisor_release_sha256",
        "supervisor_observation_sha256",
        "legacy_child_snapshot_sha256",
        "services_terminal",
        "managed_orphans_absent",
        "service_ports_unbound",
        "writer_lock_unheld",
        "retired_at",
    }
)
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}")
REASON = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:@/-]{0,159}")
SCHEMA_VERSION = re.compile(r"[0-9]{8}_[0-9]{2}")
UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
SHA256 = re.compile(r"[0-9a-f]{64}")
GENERATION = re.compile(r"[0-9]{20}")
MIGRATION_SCHEMA_ASSIGNMENT = re.compile(
    r'^SCHEMA_VERSION\s*=\s*"([0-9]{8}_[0-9]{2})"\s*$',
    re.MULTILINE,
)


class SupervisorControlBlocked(Exception):
    """The maintenance state cannot be trusted; automation must stop."""


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or UTC_TIMESTAMP.fullmatch(value) is None:
        raise SupervisorControlBlocked(
            "supervisor maintenance state contains an invalid {}".format(field)
        )
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise SupervisorControlBlocked(
            "supervisor maintenance state contains an invalid {}".format(field)
        ) from None


def _validate_timestamp(value: Any, field: str) -> str:
    _parse_timestamp(value, field)
    return value


def _validate_token(value: Any, field: str) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise SupervisorControlBlocked(
            "supervisor maintenance state contains an invalid {}".format(field)
        )
    return value


def _validate_reason(value: Any) -> str:
    if not isinstance(value, str) or REASON.fullmatch(value) is None:
        raise SupervisorControlBlocked(
            "supervisor maintenance state contains an invalid reason"
        )
    return value


def _validate_generation(value: Any, *, optional: bool = False) -> Optional[str]:
    if optional and value is None:
        return None
    if not isinstance(value, str) or GENERATION.fullmatch(value) is None:
        raise SupervisorControlBlocked(
            "supervisor maintenance state contains an invalid cutover generation"
        )
    number = int(value)
    if number <= 0 or number > MAX_GENERATION:
        raise SupervisorControlBlocked(
            "supervisor maintenance state contains an invalid cutover generation"
        )
    return value


def _next_generation(previous: Optional[str]) -> str:
    number = 0 if previous is None else int(_validate_generation(previous) or "0")
    if number >= MAX_GENERATION:
        raise SupervisorControlBlocked("cutover generation space is exhausted")
    return str(number + 1).zfill(GENERATION_WIDTH)


def _validate_target_schema(value: Any) -> str:
    if value != CONTROL_TARGET_SCHEMA_VERSION:
        raise SupervisorControlBlocked(
            "supervisor maintenance target schema must be {}".format(
                CONTROL_TARGET_SCHEMA_VERSION
            )
        )
    return value


def _validate_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise SupervisorControlBlocked(
            "supervisor maintenance observation contains an invalid {}".format(
                field
            )
        )
    return value


def _validate_uuid4(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SupervisorControlBlocked(
            "supervisor maintenance observation contains an invalid {}".format(
                field
            )
        )
    try:
        parsed = UUID(value)
    except ValueError:
        raise SupervisorControlBlocked(
            "supervisor maintenance observation contains an invalid {}".format(
                field
            )
        ) from None
    if parsed.version != 4 or str(parsed) != value:
        raise SupervisorControlBlocked(
            "supervisor maintenance observation contains an invalid {}".format(
                field
            )
        )
    return value


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _validate_directory_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise SupervisorControlBlocked(
            "supervisor maintenance directory is not owner-controlled"
        )


def _open_state_directory(
    state_dir: Path,
    *,
    trusted_root: Path,
    create: bool,
) -> Optional[int]:
    root = _lexical_absolute(trusted_root)
    state = _lexical_absolute(state_dir)
    try:
        relative = state.relative_to(root)
    except ValueError:
        raise SupervisorControlBlocked(
            "supervisor maintenance directory must stay inside the trusted repository"
        ) from None
    if not relative.parts:
        raise SupervisorControlBlocked(
            "supervisor maintenance directory must be below the trusted repository"
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError:
        raise SupervisorControlBlocked(
            "trusted supervisor repository could not be opened safely"
        ) from None
    try:
        _validate_directory_metadata(os.fstat(descriptor))
        for component in relative.parts:
            created_here = False
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    created_here = True
                except FileExistsError:
                    pass
                except OSError:
                    raise SupervisorControlBlocked(
                        "supervisor maintenance directory could not be created safely"
                    ) from None
                try:
                    if created_here:
                        os.chmod(
                            component,
                            0o700,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                    child = os.open(component, flags, dir_fd=descriptor)
                except OSError:
                    raise SupervisorControlBlocked(
                        "supervisor maintenance directory could not be opened safely"
                    ) from None
                if created_here:
                    os.fchmod(child, 0o700)
                    os.fsync(child)
            except OSError:
                raise SupervisorControlBlocked(
                    "supervisor maintenance directory contains a symlink or unsafe component"
                ) from None
            try:
                _validate_directory_metadata(os.fstat(child))
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def ensure_control_directory(
    state_dir: Path,
    *,
    create: bool,
    trusted_root: Optional[Path] = None,
) -> bool:
    root = trusted_root or Path(__file__).resolve().parents[1]
    descriptor = _open_state_directory(
        state_dir,
        trusted_root=root,
        create=create,
    )
    if descriptor is None:
        return False
    os.close(descriptor)
    return True


def _validate_existing_regular_file(
    directory_descriptor: int,
    name: str,
    *,
    allow_missing: bool,
) -> bool:
    try:
        metadata = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if allow_missing:
            return False
        raise SupervisorControlBlocked(
            "supervisor maintenance state is missing"
        ) from None
    except OSError:
        raise SupervisorControlBlocked(
            "supervisor maintenance state target is unsafe"
        ) from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > MAX_CONTROL_FILE_BYTES
    ):
        raise SupervisorControlBlocked(
            "supervisor maintenance state is not a private 0600 regular file"
        )
    return True


def _read_json_file(
    directory_descriptor: int,
    name: str,
) -> Optional[Dict[str, Any]]:
    if not _validate_existing_regular_file(
        directory_descriptor,
        name,
        allow_missing=True,
    ):
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError:
        raise SupervisorControlBlocked(
            "supervisor maintenance state could not be opened safely"
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_CONTROL_FILE_BYTES
        ):
            raise SupervisorControlBlocked(
                "supervisor maintenance state is not a private 0600 regular file"
            )
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise SupervisorControlBlocked(
                "supervisor maintenance state is malformed"
            ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise SupervisorControlBlocked(
            "supervisor maintenance state is malformed"
        )
    return payload


def _write_json_file(
    directory_descriptor: int,
    name: str,
    payload: Dict[str, Any],
) -> None:
    _validate_existing_regular_file(
        directory_descriptor,
        name,
        allow_missing=True,
    )
    temporary = ".{}.{}.{}.tmp".format(name, os.getpid(), uuid4().hex)
    descriptor: Optional[int] = None
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise SupervisorControlBlocked(
                "supervisor maintenance temporary state is unsafe"
            )
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_CONTROL_FILE_BYTES:
            raise SupervisorControlBlocked(
                "supervisor maintenance state exceeds its size limit"
            )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    except SupervisorControlBlocked:
        raise
    except OSError:
        raise SupervisorControlBlocked(
            "supervisor maintenance state could not be committed atomically"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_descriptor)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                pass


def _open_lock(directory_descriptor: int) -> int:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(
            CONTROL_LOCK_FILE,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.fsync(directory_descriptor)
    except FileExistsError:
        _validate_existing_regular_file(
            directory_descriptor,
            CONTROL_LOCK_FILE,
            allow_missing=False,
        )
        try:
            descriptor = os.open(
                CONTROL_LOCK_FILE,
                flags,
                dir_fd=directory_descriptor,
            )
        except OSError:
            raise SupervisorControlBlocked(
                "supervisor maintenance lock could not be opened safely"
            ) from None
    except OSError:
        raise SupervisorControlBlocked(
            "supervisor maintenance lock could not be created safely"
        ) from None
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise SupervisorControlBlocked(
            "supervisor maintenance lock is unsafe"
        )
    return descriptor


@contextmanager
def _locked_directory(
    state_dir: Path,
    *,
    trusted_root: Path,
    create: bool,
) -> Iterator[Optional[int]]:
    directory_descriptor = _open_state_directory(
        state_dir,
        trusted_root=trusted_root,
        create=create,
    )
    if directory_descriptor is None:
        yield None
        return
    lock_descriptor: Optional[int] = None
    try:
        lock_descriptor = _open_lock(directory_descriptor)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        yield directory_descriptor
    finally:
        if lock_descriptor is not None:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
        os.close(directory_descriptor)


def _validate_control(payload: Dict[str, Any]) -> Dict[str, Any]:
    if set(payload) != CONTROL_FIELDS:
        raise SupervisorControlBlocked(
            "supervisor maintenance state fields are unsupported"
        )
    if payload.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise SupervisorControlBlocked(
            "supervisor maintenance state schema is unsupported"
        )
    mode = payload.get("mode")
    if mode not in CONTROL_MODES:
        raise SupervisorControlBlocked(
            "supervisor maintenance state mode is unsupported"
        )
    generation = _validate_generation(payload.get("cutover_generation"))
    request_id = _validate_token(payload.get("request_id"), "request_id")
    operator_identity = _validate_token(
        payload.get("operator_identity"),
        "operator_identity",
    )
    reason = _validate_reason(payload.get("reason"))
    target_schema_version = _validate_target_schema(
        payload.get("target_schema_version")
    )
    requested_at = _validate_timestamp(
        payload.get("requested_at"),
        "requested_at",
    )
    updated_at = _validate_timestamp(payload.get("updated_at"), "updated_at")
    if updated_at < requested_at:
        raise SupervisorControlBlocked(
            "supervisor maintenance state timestamps are inconsistent"
        )
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "mode": mode,
        "cutover_generation": generation,
        "request_id": request_id,
        "operator_identity": operator_identity,
        "reason": reason,
        "target_schema_version": target_schema_version,
        "requested_at": requested_at,
        "updated_at": updated_at,
    }


def _validate_generation_ledger(payload: Dict[str, Any]) -> Dict[str, Any]:
    if set(payload) != GENERATION_FIELDS:
        raise SupervisorControlBlocked(
            "supervisor maintenance generation ledger fields are unsupported"
        )
    if payload.get("schema_version") != GENERATION_SCHEMA_VERSION:
        raise SupervisorControlBlocked(
            "supervisor maintenance generation ledger schema is unsupported"
        )
    commit_state = payload.get("commit_state")
    if commit_state not in GENERATION_STATES:
        raise SupervisorControlBlocked(
            "supervisor maintenance generation ledger state is unsupported"
        )
    return {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "last_issued_generation": _validate_generation(
            payload.get("last_issued_generation")
        ),
        "request_id": _validate_token(payload.get("request_id"), "request_id"),
        "operator_identity": _validate_token(
            payload.get("operator_identity"),
            "operator_identity",
        ),
        "reason": _validate_reason(payload.get("reason")),
        "target_schema_version": _validate_target_schema(
            payload.get("target_schema_version")
        ),
        "requested_at": _validate_timestamp(
            payload.get("requested_at"),
            "requested_at",
        ),
        "commit_state": commit_state,
    }


def _validate_observation(payload: Dict[str, Any]) -> Dict[str, Any]:
    if set(payload) != OBSERVATION_FIELDS:
        raise SupervisorControlBlocked(
            "supervisor maintenance observation fields are unsupported"
        )
    if payload.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
        raise SupervisorControlBlocked(
            "supervisor maintenance observation schema is unsupported"
        )
    supervisor_pid = payload.get("supervisor_pid")
    if type(supervisor_pid) is not int or supervisor_pid <= 0:
        raise SupervisorControlBlocked(
            "supervisor maintenance observation PID is invalid"
        )
    cwd = payload.get("supervisor_cwd")
    if not isinstance(cwd, str) or not cwd.startswith("/") or len(cwd) > 1024:
        raise SupervisorControlBlocked(
            "supervisor maintenance observation cwd is invalid"
        )
    launchd_label = payload.get("supervisor_launchd_label")
    if launchd_label != LAUNCHD_LABEL:
        raise SupervisorControlBlocked(
            "supervisor maintenance observation launch label is invalid"
        )
    runtime_schema = payload.get("supervisor_runtime_schema_version")
    if (
        not isinstance(runtime_schema, str)
        or SCHEMA_VERSION.fullmatch(runtime_schema) is None
    ):
        raise SupervisorControlBlocked(
            "supervisor maintenance observation schema capability is invalid"
        )
    observed_mode = payload.get("observed_mode")
    if observed_mode not in OBSERVED_MODES:
        raise SupervisorControlBlocked(
            "supervisor maintenance observation mode is unsupported"
        )
    observed_generation = _validate_generation(
        payload.get("observed_generation"),
        optional=observed_mode == OBSERVED_MODE_FAIL_CLOSED,
    )
    observed_request_id = payload.get("observed_request_id")
    if observed_request_id is not None:
        observed_request_id = _validate_token(
            observed_request_id,
            "observed_request_id",
        )
    if observed_mode != OBSERVED_MODE_FAIL_CLOSED and (
        observed_generation is None or observed_request_id is None
    ):
        raise SupervisorControlBlocked(
            "supervisor maintenance observation is incomplete"
        )
    observed_at = _validate_timestamp(payload.get("observed_at"), "observed_at")
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "supervisor_pid": supervisor_pid,
        "supervisor_start_token": _validate_sha256(
            payload.get("supervisor_start_token"),
            "start token",
        ),
        "supervisor_instance_id": _validate_uuid4(
            payload.get("supervisor_instance_id"),
            "instance ID",
        ),
        "supervisor_command_sha256": _validate_sha256(
            payload.get("supervisor_command_sha256"),
            "command digest",
        ),
        "supervisor_cwd": cwd,
        "supervisor_launchd_label": launchd_label,
        "supervisor_release_sha256": _validate_sha256(
            payload.get("supervisor_release_sha256"),
            "release digest",
        ),
        "supervisor_runtime_schema_version": runtime_schema,
        "observed_generation": observed_generation,
        "observed_request_id": observed_request_id,
        "observed_mode": observed_mode,
        "observed_at": observed_at,
    }


def _validate_legacy_retirement(payload: Dict[str, Any]) -> Dict[str, Any]:
    if set(payload) != LEGACY_RETIREMENT_FIELDS:
        raise SupervisorControlBlocked(
            "legacy retirement receipt fields are unsupported"
        )
    if payload.get("schema_version") != LEGACY_RETIREMENT_SCHEMA_VERSION:
        raise SupervisorControlBlocked(
            "legacy retirement receipt schema is unsupported"
        )
    supervisor_pid = payload.get("supervisor_pid")
    if type(supervisor_pid) is not int or supervisor_pid <= 0:
        raise SupervisorControlBlocked("legacy retirement receipt PID is invalid")
    for field in (
        "services_terminal",
        "managed_orphans_absent",
        "service_ports_unbound",
        "writer_lock_unheld",
    ):
        if payload.get(field) is not True:
            raise SupervisorControlBlocked(
                "legacy retirement receipt terminal proof is incomplete"
            )
    return {
        "schema_version": LEGACY_RETIREMENT_SCHEMA_VERSION,
        "cutover_generation": _validate_generation(
            payload.get("cutover_generation")
        ),
        "request_id": _validate_token(payload.get("request_id"), "request_id"),
        "supervisor_pid": supervisor_pid,
        "supervisor_start_token": _validate_sha256(
            payload.get("supervisor_start_token"),
            "retired supervisor start token",
        ),
        "supervisor_release_sha256": _validate_sha256(
            payload.get("supervisor_release_sha256"),
            "retired supervisor release digest",
        ),
        "supervisor_observation_sha256": _validate_sha256(
            payload.get("supervisor_observation_sha256"),
            "retired supervisor observation digest",
        ),
        "legacy_child_snapshot_sha256": _validate_sha256(
            payload.get("legacy_child_snapshot_sha256"),
            "retired legacy child snapshot digest",
        ),
        "services_terminal": True,
        "managed_orphans_absent": True,
        "service_ports_unbound": True,
        "writer_lock_unheld": True,
        "retired_at": _validate_timestamp(
            payload.get("retired_at"),
            "retired_at",
        ),
    }


def _validate_snapshot_cwd(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or "\0" in value
        or len(value) > 4096
    ):
        raise SupervisorControlBlocked(
            "legacy child snapshot contains an invalid {}".format(field)
        )
    return value


def _validate_legacy_child_snapshot(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    if set(payload) != LEGACY_CHILD_SNAPSHOT_FIELDS:
        raise SupervisorControlBlocked(
            "legacy child snapshot fields are unsupported"
        )
    if payload.get("schema_version") != LEGACY_CHILD_SNAPSHOT_SCHEMA_VERSION:
        raise SupervisorControlBlocked(
            "legacy child snapshot schema is unsupported"
        )
    supervisor_pid = payload.get("supervisor_pid")
    if type(supervisor_pid) is not int or supervisor_pid <= 0:
        raise SupervisorControlBlocked("legacy child snapshot supervisor PID is invalid")
    runtime_schema = payload.get("supervisor_runtime_schema_version")
    if (
        not isinstance(runtime_schema, str)
        or SCHEMA_VERSION.fullmatch(runtime_schema) is None
    ):
        raise SupervisorControlBlocked(
            "legacy child snapshot supervisor schema capability is invalid"
        )
    raw_children = payload.get("children")
    if not isinstance(raw_children, list) or len(raw_children) > 64:
        raise SupervisorControlBlocked("legacy child snapshot children are invalid")
    children = []
    seen_pids = set()
    seen_service_pids = set()
    for raw_child in raw_children:
        if not isinstance(raw_child, dict) or set(raw_child) != LEGACY_CHILD_FIELDS:
            raise SupervisorControlBlocked(
                "legacy child snapshot entry fields are unsupported"
            )
        service = raw_child.get("service")
        pid = raw_child.get("pid")
        pgid = raw_child.get("pgid")
        if service not in LEGACY_CHILD_SERVICES:
            raise SupervisorControlBlocked(
                "legacy child snapshot service is unsupported"
            )
        if (
            type(pid) is not int
            or type(pgid) is not int
            or pid <= 0
            or pgid != pid
        ):
            raise SupervisorControlBlocked(
                "legacy child snapshot process group is invalid"
            )
        if pid in seen_pids or (service, pid) in seen_service_pids:
            raise SupervisorControlBlocked(
                "legacy child snapshot process identity is duplicated"
            )
        seen_pids.add(pid)
        seen_service_pids.add((service, pid))
        children.append(
            {
                "service": service,
                "pid": pid,
                "pgid": pgid,
                "start_token": _validate_sha256(
                    raw_child.get("start_token"),
                    "child start token",
                ),
                "command_sha256": _validate_sha256(
                    raw_child.get("command_sha256"),
                    "child command digest",
                ),
                "cwd": _validate_snapshot_cwd(raw_child.get("cwd"), "child cwd"),
            }
        )
    children.sort(key=lambda child: (str(child["service"]), int(child["pid"])))
    if children != raw_children:
        raise SupervisorControlBlocked(
            "legacy child snapshot entries are not in canonical order"
        )
    return {
        "schema_version": LEGACY_CHILD_SNAPSHOT_SCHEMA_VERSION,
        "cutover_generation": _validate_generation(
            payload.get("cutover_generation")
        ),
        "request_id": _validate_token(payload.get("request_id"), "request_id"),
        "supervisor_pid": supervisor_pid,
        "supervisor_start_token": _validate_sha256(
            payload.get("supervisor_start_token"),
            "snapshot supervisor start token",
        ),
        "supervisor_command_sha256": _validate_sha256(
            payload.get("supervisor_command_sha256"),
            "snapshot supervisor command digest",
        ),
        "supervisor_cwd": _validate_snapshot_cwd(
            payload.get("supervisor_cwd"),
            "supervisor cwd",
        ),
        "supervisor_release_sha256": _validate_sha256(
            payload.get("supervisor_release_sha256"),
            "snapshot supervisor release digest",
        ),
        "supervisor_runtime_schema_version": runtime_schema,
        "children": children,
        "captured_at": _validate_timestamp(
            payload.get("captured_at"),
            "captured_at",
        ),
    }


def _read_legacy_child_snapshot(
    directory_descriptor: int,
) -> Optional[Dict[str, Any]]:
    payload = _read_json_file(directory_descriptor, LEGACY_CHILD_SNAPSHOT_FILE)
    return None if payload is None else _validate_legacy_child_snapshot(payload)


def _legacy_child_snapshot_sha256(snapshot: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(snapshot),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _supervisor_observation_sha256(observation: Mapping[str, Any]) -> str:
    validated = _validate_observation(dict(observation))
    encoded = json.dumps(
        validated,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_legacy_retirement(
    directory_descriptor: int,
) -> Optional[Dict[str, Any]]:
    payload = _read_json_file(directory_descriptor, LEGACY_RETIREMENT_FILE)
    return None if payload is None else _validate_legacy_retirement(payload)


def _retirement_matches_control(
    retirement: Dict[str, Any],
    control: Dict[str, Any],
) -> bool:
    return (
        retirement["cutover_generation"] == control["cutover_generation"]
        and retirement["request_id"] == control["request_id"]
        and control["mode"] == CONTROL_MODE_MIGRATION_SUSPENDED
    )


def _ledger_matches_control(
    ledger: Dict[str, Any],
    control: Dict[str, Any],
) -> bool:
    return all(
        (
            ledger[ledger_key] == control[control_key]
            for ledger_key, control_key in (
                ("last_issued_generation", "cutover_generation"),
                ("request_id", "request_id"),
                ("operator_identity", "operator_identity"),
                ("reason", "reason"),
                ("target_schema_version", "target_schema_version"),
                ("requested_at", "requested_at"),
            )
        )
    )


def _pending_matches_request(
    ledger: Dict[str, Any],
    *,
    request_id: str,
    operator_identity: str,
    reason: str,
    target_schema_version: str,
) -> bool:
    return (
        ledger["commit_state"] == GENERATION_PENDING
        and ledger["request_id"] == request_id
        and ledger["operator_identity"] == operator_identity
        and ledger["reason"] == reason
        and ledger["target_schema_version"] == target_schema_version
    )


def _control_from_ledger(ledger: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "mode": CONTROL_MODE_MIGRATION_SUSPENDED,
        "cutover_generation": ledger["last_issued_generation"],
        "request_id": ledger["request_id"],
        "operator_identity": ledger["operator_identity"],
        "reason": ledger["reason"],
        "target_schema_version": ledger["target_schema_version"],
        "requested_at": ledger["requested_at"],
        "updated_at": ledger["requested_at"],
    }


def _read_committed_snapshot(
    directory_descriptor: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    control_payload = _read_json_file(directory_descriptor, CONTROL_STATE_FILE)
    ledger_payload = _read_json_file(directory_descriptor, CONTROL_GENERATION_FILE)
    observation_payload = _read_json_file(
        directory_descriptor,
        CONTROL_OBSERVATION_FILE,
    )
    child_snapshot_payload = _read_json_file(
        directory_descriptor,
        LEGACY_CHILD_SNAPSHOT_FILE,
    )
    retirement_payload = _read_json_file(
        directory_descriptor,
        LEGACY_RETIREMENT_FILE,
    )
    if (
        control_payload is None
        and ledger_payload is None
        and observation_payload is None
        and child_snapshot_payload is None
        and retirement_payload is None
    ):
        return None, None, None
    if control_payload is None or ledger_payload is None:
        raise SupervisorControlBlocked(
            "supervisor maintenance history is incomplete; automation remains fenced"
        )
    control = _validate_control(control_payload)
    ledger = _validate_generation_ledger(ledger_payload)
    observation = (
        None
        if observation_payload is None
        else _validate_observation(observation_payload)
    )
    child_snapshot = (
        None
        if child_snapshot_payload is None
        else _validate_legacy_child_snapshot(child_snapshot_payload)
    )
    if ledger["commit_state"] != GENERATION_COMMITTED:
        raise SupervisorControlBlocked(
            "supervisor maintenance generation commit is incomplete"
        )
    if not _ledger_matches_control(ledger, control):
        raise SupervisorControlBlocked(
            "supervisor maintenance control and generation ledger disagree"
        )
    if child_snapshot is not None and (
        child_snapshot["cutover_generation"] != control["cutover_generation"]
        or child_snapshot["request_id"] != control["request_id"]
        or control["mode"] != CONTROL_MODE_MIGRATION_SUSPENDED
        or _parse_timestamp(child_snapshot["captured_at"], "captured_at")
        < _parse_timestamp(control["requested_at"], "requested_at")
    ):
        raise SupervisorControlBlocked(
            "legacy child snapshot does not match suspended control"
        )
    if retirement_payload is not None:
        retirement = _validate_legacy_retirement(retirement_payload)
        if (
            child_snapshot is None
            or
            not _retirement_matches_control(retirement, control)
            or not _observation_matches_control(control, observation)
            or observation is None
            or any(
                retirement[retirement_key] != observation[observation_key]
                for retirement_key, observation_key in (
                    ("supervisor_pid", "supervisor_pid"),
                    ("supervisor_start_token", "supervisor_start_token"),
                    ("supervisor_release_sha256", "supervisor_release_sha256"),
                )
            )
            or retirement["legacy_child_snapshot_sha256"]
            != _legacy_child_snapshot_sha256(child_snapshot)
            or retirement["supervisor_observation_sha256"]
            != _supervisor_observation_sha256(observation)
            or child_snapshot["supervisor_release_sha256"]
            != observation["supervisor_release_sha256"]
            or child_snapshot["supervisor_runtime_schema_version"]
            != observation["supervisor_runtime_schema_version"]
            or child_snapshot["supervisor_start_token"]
            == observation["supervisor_start_token"]
            or _parse_timestamp(child_snapshot["captured_at"], "captured_at")
            > _parse_timestamp(observation["observed_at"], "observed_at")
            or _parse_timestamp(retirement["retired_at"], "retired_at")
            < _parse_timestamp(observation["observed_at"], "observed_at")
        ):
            raise SupervisorControlBlocked(
                "legacy retirement receipt provenance is incomplete"
            )
    return control, ledger, observation


def _default_control() -> Dict[str, Any]:
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "mode": CONTROL_MODE_ACTIVE,
        "cutover_generation": None,
        "request_id": None,
        "operator_identity": None,
        "reason": None,
        "target_schema_version": None,
        "requested_at": None,
        "updated_at": None,
        "source": "DEFAULT_MISSING",
    }


def _build_identity(repo_root: Path) -> Tuple[str, str]:
    files = (
        repo_root / "scripts" / "local_supervisor.py",
        repo_root / "scripts" / "local_supervisor_control.py",
        repo_root / "scripts" / "local_runtime.py",
        repo_root / "backend" / "app" / "db" / "migrations.py",
    )
    digest = hashlib.sha256()
    migration_source: Optional[str] = None
    for path in files:
        try:
            content = path.read_bytes()
        except OSError:
            raise RuntimeError("supervisor build identity could not be loaded") from None
        digest.update(path.relative_to(repo_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
        if path.name == "migrations.py":
            try:
                migration_source = content.decode("utf-8")
            except UnicodeError:
                raise RuntimeError(
                    "supervisor schema capability could not be loaded"
                ) from None
    match = (
        None
        if migration_source is None
        else MIGRATION_SCHEMA_ASSIGNMENT.search(migration_source)
    )
    if match is None:
        raise RuntimeError("supervisor schema capability could not be loaded")
    return digest.hexdigest(), match.group(1)


_MODULE_REPO_ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR_RELEASE_SHA256, SUPERVISOR_RUNTIME_SCHEMA_VERSION = _build_identity(
    _MODULE_REPO_ROOT
)
SUPERVISOR_INSTANCE_ID = str(uuid4())


def _run_probe(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SupervisorControlBlocked(
            "canonical supervisor identity could not be verified"
        ) from None


def _launchd_snapshot() -> Tuple[str, int]:
    # The tabular list contains PID/status/label only.  Unlike ``launchctl
    # print``, it never exposes the job environment to this process.
    completed = _run_probe(["/bin/launchctl", "list"])
    if completed.returncode != 0:
        raise SupervisorControlBlocked(
            "canonical supervisor launch owner is unavailable"
        )
    matches = []
    for line in completed.stdout.splitlines():
        columns = line.split()
        if len(columns) == 3 and columns[2] == LAUNCHD_LABEL:
            matches.append(columns)
    if len(matches) != 1 or not matches[0][0].isdigit():
        raise SupervisorControlBlocked(
            "canonical supervisor launch owner is not running"
        )
    pid = int(matches[0][0])
    if pid <= 0:
        raise SupervisorControlBlocked(
            "canonical supervisor launch owner is not running"
        )
    return LAUNCHD_LABEL, pid


def _ps_value(pid: int, field: str) -> str:
    completed = _run_probe(["/bin/ps", "-ww", "-p", str(pid), "-o", field + "="])
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise SupervisorControlBlocked(
            "canonical supervisor process identity could not be verified"
        )
    return value


def _process_cwd(pid: int) -> str:
    completed = _run_probe(
        ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]
    )
    if completed.returncode != 0:
        raise SupervisorControlBlocked(
            "canonical supervisor working directory could not be verified"
        )
    paths = [line[1:] for line in completed.stdout.splitlines() if line.startswith("n")]
    if len(paths) != 1 or not paths[0].startswith("/"):
        raise SupervisorControlBlocked(
            "canonical supervisor working directory could not be verified"
        )
    return paths[0]


def _probe_canonical_supervisor(
    trusted_root: Path,
    *,
    expected_pid: Optional[int] = None,
    require_self: bool = False,
) -> Dict[str, Any]:
    root = _lexical_absolute(trusted_root)
    first_launch = _launchd_snapshot()
    launchd_label, pid = first_launch
    if expected_pid is not None and pid != expected_pid:
        raise SupervisorControlBlocked(
            "canonical supervisor PID changed"
        )
    if require_self and pid != os.getpid():
        raise SupervisorControlBlocked(
            "maintenance receipt writer is not the canonical supervisor"
        )
    state = _ps_value(pid, "state")
    if state[:1].upper() == "Z":
        raise SupervisorControlBlocked("canonical supervisor is a zombie process")
    started = _ps_value(pid, "lstart")
    command = _ps_value(pid, "command")
    cwd = _process_cwd(pid)
    expected_script = str(root / "scripts" / "local_supervisor.py")
    command_suffix = " " + expected_script
    interpreter = (
        command[: -len(command_suffix)]
        if command.endswith(command_suffix)
        else ""
    )
    venv_python = root / "backend" / ".venv" / "bin" / "python"
    try:
        resolved_python = venv_python.resolve(strict=True)
    except OSError:
        raise SupervisorControlBlocked(
            "canonical supervisor interpreter could not be verified"
        ) from None
    allowed_interpreters = {
        str(venv_python),
        str(resolved_python),
        str(
            resolved_python.parent.parent
            / "Resources"
            / "Python.app"
            / "Contents"
            / "MacOS"
            / "Python"
        ),
    }
    if (
        interpreter not in allowed_interpreters
        or cwd != str(root)
    ):
        raise SupervisorControlBlocked(
            "canonical supervisor command or working directory is unexpected"
        )
    second_launch = _launchd_snapshot()
    second_state = _ps_value(pid, "state")
    second_started = _ps_value(pid, "lstart")
    second_command = _ps_value(pid, "command")
    if (
        second_launch != first_launch
        or second_state[:1].upper() == "Z"
        or second_started != started
        or second_command != command
    ):
        raise SupervisorControlBlocked(
            "canonical supervisor identity changed during observation"
        )
    command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
    start_token = hashlib.sha256(
        "{}\0{}\0{}\0{}".format(
            pid,
            started,
            command_sha256,
            cwd,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "supervisor_pid": pid,
        "supervisor_start_token": start_token,
        "supervisor_command_sha256": command_sha256,
        "supervisor_cwd": cwd,
        "supervisor_launchd_label": launchd_label,
    }


def _kickstart_supervisor_owner() -> None:
    target = "gui/{}/{}".format(os.getuid(), LAUNCHD_LABEL)
    completed = _run_probe(["/bin/launchctl", "kickstart", "-k", target])
    if completed.returncode != 0:
        raise SupervisorControlBlocked(
            "canonical supervisor owner could not be reloaded"
        )


def _supervisor_runtime_children(pid: int, trusted_root: Path) -> list[int]:
    completed = _run_probe(
        ["/bin/ps", "-ww", "-axo", "pid=,ppid=,state="]
    )
    if completed.returncode != 0:
        raise SupervisorControlBlocked(
            "supervisor child-process snapshot is unavailable"
        )
    root = _lexical_absolute(trusted_root)
    runtime_script = str(root / "scripts" / "local_runtime.py")
    children = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split()
        if len(fields) < 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        if int(fields[1]) != pid:
            continue
        marker = fields[2][:1].upper()
        if marker in {"Z", "X"}:
            continue
        child_pid = int(fields[0])
        command = _ps_value(child_pid, "command")
        child_cwd = _process_cwd(child_pid)
        confirmation = _run_probe(
            ["/bin/ps", "-ww", "-p", str(child_pid), "-o", "ppid=,state="]
        )
        confirmation_fields = confirmation.stdout.strip().split()
        if (
            confirmation.returncode != 0
            or len(confirmation_fields) != 2
            or not confirmation_fields[0].isdigit()
            or int(confirmation_fields[0]) != pid
            or confirmation_fields[1][:1].upper() in {"Z", "X"}
        ):
            raise SupervisorControlBlocked(
                "supervisor child-process identity changed during drain"
            )
        if (
            command.endswith(" " + runtime_script)
            or " " + runtime_script + " " in command
        ) and child_cwd == str(root):
            children.append(child_pid)
        else:
            raise SupervisorControlBlocked(
                "canonical supervisor has an unexpected live child"
            )
    return children


def _pause_and_drain_supervisor(
    identity: Dict[str, Any],
    trusted_root: Path,
) -> None:
    pid = int(identity["supervisor_pid"])
    target = "gui/{}/{}".format(os.getuid(), LAUNCHD_LABEL)
    paused_result = _run_probe(
        ["/bin/launchctl", "kill", "SIGSTOP", target]
    )
    if paused_result.returncode != 0:
        raise SupervisorControlBlocked(
            "canonical supervisor could not be paused for a safe reload"
        )
    # Once a pre-fence owner is paused, every failure remains fail-closed.  It
    # must never be resumed into an automatic recovery loop that cannot observe
    # the already-durable MIGRATION_SUSPENDED control record.
    paused = _probe_canonical_supervisor(
        trusted_root,
        expected_pid=pid,
    )
    if paused["supervisor_start_token"] != identity["supervisor_start_token"]:
        raise SupervisorControlBlocked(
            "canonical supervisor identity changed before drain; owner remains paused"
        )
    deadline = time.monotonic() + SUPERVISOR_DRAIN_TIMEOUT_SECONDS
    while _supervisor_runtime_children(pid, trusted_root):
        if time.monotonic() >= deadline:
            raise SupervisorControlBlocked(
                "canonical supervisor runtime command did not drain; owner remains paused"
            )
        time.sleep(0.1)


def _commit_legacy_child_snapshot(
    directory: int,
    *,
    control: Dict[str, Any],
    supervisor: Dict[str, Any],
    observation: Optional[Dict[str, Any]],
    raw_children: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    try:
        children = [dict(child) for child in raw_children]
    except (TypeError, ValueError):
        raise SupervisorControlBlocked(
            "legacy child snapshot callback returned invalid evidence"
        ) from None
    candidate = _validate_legacy_child_snapshot(
        {
            "schema_version": LEGACY_CHILD_SNAPSHOT_SCHEMA_VERSION,
            "cutover_generation": control["cutover_generation"],
            "request_id": control["request_id"],
            "supervisor_pid": supervisor["supervisor_pid"],
            "supervisor_start_token": supervisor["supervisor_start_token"],
            "supervisor_command_sha256": supervisor[
                "supervisor_command_sha256"
            ],
            "supervisor_cwd": supervisor["supervisor_cwd"],
            # These fields bind the deployed code performing capture and the
            # replacement capability which must later write the observation.
            # They are not an attestation of code already loaded by the paused
            # legacy process; its exact OS identity is bound separately above.
            "supervisor_release_sha256": (
                SUPERVISOR_RELEASE_SHA256
                if observation is None
                else observation["supervisor_release_sha256"]
            ),
            "supervisor_runtime_schema_version": (
                SUPERVISOR_RUNTIME_SCHEMA_VERSION
                if observation is None
                else observation["supervisor_runtime_schema_version"]
            ),
            "children": children,
            "captured_at": _utc_now(),
        }
    )
    existing = _read_legacy_child_snapshot(directory)
    if existing is not None:
        candidate_without_time = {
            key: value for key, value in candidate.items() if key != "captured_at"
        }
        existing_without_time = {
            key: value for key, value in existing.items() if key != "captured_at"
        }
        current_children = candidate_without_time.pop("children")
        captured_children = existing_without_time.pop("children")
        if (
            existing_without_time != candidate_without_time
            or any(child not in captured_children for child in current_children)
        ):
            raise SupervisorControlBlocked(
                "legacy child snapshot is immutable and no longer matches"
            )
        return existing
    _write_json_file(directory, LEGACY_CHILD_SNAPSHOT_FILE, candidate)
    return candidate


def reload_supervisor_owner(
    state_dir: Path,
    *,
    cutover_generation: str,
    request_id: str,
    capture_legacy_children: Callable[[], Sequence[Mapping[str, Any]]],
    trusted_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reload only the canonical supervisor process under a suspended fence."""

    root = trusted_root or _MODULE_REPO_ROOT
    cutover_generation = _validate_generation(cutover_generation) or ""
    request_id = _validate_token(request_id, "request_id")
    with _locked_directory(state_dir, trusted_root=root, create=False) as directory:
        if directory is None:
            raise SupervisorControlBlocked(
                "supervisor reload requires existing control history"
            )
        control, _ledger, _observation = _read_committed_snapshot(directory)
        retirement = _read_legacy_retirement(directory)
        if (
            control is None
            or control["mode"] != CONTROL_MODE_MIGRATION_SUSPENDED
            or control["cutover_generation"] != cutover_generation
            or control["request_id"] != request_id
        ):
            raise SupervisorControlBlocked(
                "supervisor reload requires the exact suspended transition"
            )
        if retirement is not None:
            raise SupervisorControlBlocked(
                "legacy runtime is permanently retired; reload is refused"
            )
        if (
            _observation is None
            and control["cutover_generation"]
            != str(1).zfill(GENERATION_WIDTH)
        ):
            raise SupervisorControlBlocked(
                "missing owner receipt permits bootstrap only for the first generation"
            )
        if SUPERVISOR_RUNTIME_SCHEMA_VERSION == control["target_schema_version"]:
            raise SupervisorControlBlocked(
                "legacy child snapshot requires the pre-target supervisor capability"
            )
        bootstrap_without_observation = _observation is None
        if not bootstrap_without_observation:
            if not _observation_matches_control(control, _observation):
                raise SupervisorControlBlocked(
                    "supervisor reload requires a matching durable owner receipt"
                )
            assert _observation is not None
            age = datetime.now(timezone.utc) - _parse_timestamp(
                _observation["observed_at"],
                "observed_at",
            )
            if (
                age.total_seconds() < -5
                or age.total_seconds() > MAX_OBSERVATION_AGE_SECONDS
            ):
                raise SupervisorControlBlocked(
                    "supervisor reload observation is stale"
                )
            if (
                _observation["supervisor_release_sha256"]
                != SUPERVISOR_RELEASE_SHA256
                or _observation["supervisor_runtime_schema_version"]
                != SUPERVISOR_RUNTIME_SCHEMA_VERSION
                or _observation["supervisor_runtime_schema_version"]
                == control["target_schema_version"]
            ):
                raise SupervisorControlBlocked(
                    "supervisor reload requires the observed pre-target owner capability"
                )
        previous = _probe_canonical_supervisor(root)
        if _observation is not None:
            for field in (
                "supervisor_pid",
                "supervisor_start_token",
                "supervisor_command_sha256",
                "supervisor_cwd",
                "supervisor_launchd_label",
            ):
                if previous[field] != _observation[field]:
                    raise SupervisorControlBlocked(
                        "supervisor reload observation identity is stale"
                    )
        existing_snapshot = _read_legacy_child_snapshot(directory)
        if existing_snapshot is not None:
            if (
                existing_snapshot["supervisor_release_sha256"]
                != SUPERVISOR_RELEASE_SHA256
                or existing_snapshot["supervisor_runtime_schema_version"]
                != SUPERVISOR_RUNTIME_SCHEMA_VERSION
            ):
                raise SupervisorControlBlocked(
                    "deployed supervisor capability changed since the legacy child snapshot"
                )
            for snapshot_field, supervisor_field in (
                ("supervisor_pid", "supervisor_pid"),
                ("supervisor_start_token", "supervisor_start_token"),
                ("supervisor_command_sha256", "supervisor_command_sha256"),
                ("supervisor_cwd", "supervisor_cwd"),
            ):
                if existing_snapshot[snapshot_field] != previous[supervisor_field]:
                    raise SupervisorControlBlocked(
                        "legacy child snapshot belongs to a different supervisor owner"
                    )
        _pause_and_drain_supervisor(previous, root)
        child_snapshot = existing_snapshot
        if child_snapshot is None:
            child_snapshot = _commit_legacy_child_snapshot(
                directory,
                control=control,
                supervisor=previous,
                observation=_observation,
                raw_children=capture_legacy_children(),
            )
        confirmed_previous = _probe_canonical_supervisor(
            root,
            expected_pid=previous["supervisor_pid"],
        )
        for field in (
            "supervisor_pid",
            "supervisor_start_token",
            "supervisor_command_sha256",
            "supervisor_cwd",
            "supervisor_launchd_label",
        ):
            if confirmed_previous[field] != previous[field]:
                raise SupervisorControlBlocked(
                    "legacy supervisor identity changed after child snapshot; owner remains paused"
                )
        _kickstart_supervisor_owner()
        deadline = time.monotonic() + 15
        replacement: Optional[Dict[str, Any]] = None
        while time.monotonic() < deadline:
            try:
                candidate = _probe_canonical_supervisor(root)
            except SupervisorControlBlocked:
                candidate = None
            if (
                candidate is not None
                and candidate["supervisor_start_token"]
                != previous["supervisor_start_token"]
            ):
                replacement = candidate
                break
            time.sleep(0.1)
        if replacement is None:
            raise SupervisorControlBlocked(
                "canonical supervisor replacement was not observed"
            )
        return {
            "status": "SUPERVISOR_OWNER_RELOADED",
            "mode": control["mode"],
            "cutover_generation": control["cutover_generation"],
            "request_id": control["request_id"],
            "launchd_label": LAUNCHD_LABEL,
            "previous_supervisor_pid": previous["supervisor_pid"],
            "supervisor_pid": replacement["supervisor_pid"],
            "legacy_child_count": len(child_snapshot["children"]),
            "bootstrap_without_observation": bootstrap_without_observation,
            "observation_required": True,
            **control_paths(state_dir),
        }


def _observation_matches_control(
    control: Dict[str, Any],
    observation: Optional[Dict[str, Any]],
) -> bool:
    return (
        observation is not None
        and observation["observed_mode"] == control["mode"]
        and observation["observed_generation"]
        == control["cutover_generation"]
        and observation["observed_request_id"] == control["request_id"]
    )


def _resume_eligibility(
    control: Dict[str, Any],
    observation: Optional[Dict[str, Any]],
    *,
    trusted_root: Path,
    probe_process: bool,
) -> Tuple[bool, str]:
    if not _observation_matches_control(control, observation):
        return False, "OBSERVATION_TUPLE_MISMATCH"
    assert observation is not None
    if observation["observed_mode"] != CONTROL_MODE_MIGRATION_SUSPENDED:
        return False, "CONTROL_NOT_SUSPENDED"
    age = datetime.now(timezone.utc) - _parse_timestamp(
        observation["observed_at"],
        "observed_at",
    )
    if age.total_seconds() < -5 or age.total_seconds() > MAX_OBSERVATION_AGE_SECONDS:
        return False, "OBSERVATION_STALE"
    if (
        observation["supervisor_runtime_schema_version"]
        != control["target_schema_version"]
        or SUPERVISOR_RUNTIME_SCHEMA_VERSION != control["target_schema_version"]
    ):
        return False, "SUPERVISOR_SCHEMA_CAPABILITY_MISMATCH"
    if observation["supervisor_release_sha256"] != SUPERVISOR_RELEASE_SHA256:
        return False, "SUPERVISOR_RELEASE_MISMATCH"
    if probe_process:
        try:
            current = _probe_canonical_supervisor(
                trusted_root,
                expected_pid=observation["supervisor_pid"],
            )
        except SupervisorControlBlocked:
            return False, "SUPERVISOR_IDENTITY_UNAVAILABLE"
        for field in (
            "supervisor_pid",
            "supervisor_start_token",
            "supervisor_command_sha256",
            "supervisor_cwd",
            "supervisor_launchd_label",
        ):
            if current[field] != observation[field]:
                return False, "SUPERVISOR_IDENTITY_MISMATCH"
    return True, "ELIGIBLE"


def control_paths(state_dir: Path) -> Dict[str, str]:
    return {
        "state_path": str((_lexical_absolute(state_dir) / CONTROL_STATE_FILE)),
        "generation_ledger_path": str(
            (_lexical_absolute(state_dir) / CONTROL_GENERATION_FILE)
        ),
        "observed_receipt_path": str(
            (_lexical_absolute(state_dir) / CONTROL_OBSERVATION_FILE)
        ),
        "legacy_child_snapshot_path": str(
            (_lexical_absolute(state_dir) / LEGACY_CHILD_SNAPSHOT_FILE)
        ),
        "legacy_retirement_path": str(
            (_lexical_absolute(state_dir) / LEGACY_RETIREMENT_FILE)
        ),
        "lock_path": str((_lexical_absolute(state_dir) / CONTROL_LOCK_FILE)),
    }


def read_supervisor_control(
    state_dir: Path,
    *,
    trusted_root: Optional[Path] = None,
) -> Dict[str, Any]:
    root = trusted_root or _MODULE_REPO_ROOT
    with _locked_directory(state_dir, trusted_root=root, create=False) as directory:
        if directory is None:
            return _default_control()
        control, _ledger, _observation = _read_committed_snapshot(directory)
        return _default_control() if control is None else {**control, "source": "FILE"}


def read_supervisor_observation(
    state_dir: Path,
    *,
    trusted_root: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    root = trusted_root or _MODULE_REPO_ROOT
    with _locked_directory(state_dir, trusted_root=root, create=False) as directory:
        if directory is None:
            return None
        _control, _ledger, observation = _read_committed_snapshot(directory)
        return observation


@contextmanager
def active_supervisor_automation_fence(
    state_dir: Path,
    *,
    trusted_root: Optional[Path] = None,
) -> Iterator[None]:
    """Hold an ACTIVE maintenance snapshot across one local runtime mutation."""

    root = trusted_root or _MODULE_REPO_ROOT
    with _locked_directory(state_dir, trusted_root=root, create=True) as directory:
        assert directory is not None
        control, _ledger, _observation = _read_committed_snapshot(directory)
        if control is None:
            yield
            return
        if _read_legacy_retirement(directory) is not None:
            raise SupervisorControlBlocked(
                "legacy runtime is permanently retired; automation is refused"
            )
        if control["mode"] != CONTROL_MODE_ACTIVE:
            raise SupervisorControlBlocked(
                "runtime automation is suppressed by MIGRATION_SUSPENDED control"
            )
        if SUPERVISOR_RUNTIME_SCHEMA_VERSION != control["target_schema_version"]:
            raise SupervisorControlBlocked(
                "active control requires a matching supervisor schema capability"
            )
        yield


def suspend_supervisor_control(
    state_dir: Path,
    *,
    request_id: str,
    operator_identity: str,
    reason: str,
    target_schema_version: str,
    trusted_root: Optional[Path] = None,
) -> Dict[str, Any]:
    root = trusted_root or _MODULE_REPO_ROOT
    request_id = _validate_token(request_id, "request_id")
    operator_identity = _validate_token(operator_identity, "operator_identity")
    reason = _validate_reason(reason)
    target_schema_version = _validate_target_schema(target_schema_version)
    with _locked_directory(state_dir, trusted_root=root, create=True) as directory:
        assert directory is not None
        control_payload = _read_json_file(directory, CONTROL_STATE_FILE)
        ledger_payload = _read_json_file(directory, CONTROL_GENERATION_FILE)
        observation_payload = _read_json_file(directory, CONTROL_OBSERVATION_FILE)
        retirement_payload = _read_json_file(directory, LEGACY_RETIREMENT_FILE)
        if observation_payload is not None:
            _validate_observation(observation_payload)
        if retirement_payload is not None:
            _validate_legacy_retirement(retirement_payload)
            raise SupervisorControlBlocked(
                "legacy runtime is permanently retired; new control generation refused"
            )

        control = (
            None if control_payload is None else _validate_control(control_payload)
        )
        ledger = (
            None
            if ledger_payload is None
            else _validate_generation_ledger(ledger_payload)
        )
        if control is None and ledger is None:
            if observation_payload is not None:
                raise SupervisorControlBlocked(
                    "supervisor maintenance receipt exists without control history"
                )
            now = _utc_now()
            ledger = {
                "schema_version": GENERATION_SCHEMA_VERSION,
                "last_issued_generation": _next_generation(None),
                "request_id": request_id,
                "operator_identity": operator_identity,
                "reason": reason,
                "target_schema_version": target_schema_version,
                "requested_at": now,
                "commit_state": GENERATION_PENDING,
            }
            _write_json_file(directory, CONTROL_GENERATION_FILE, ledger)
        elif ledger is None:
            raise SupervisorControlBlocked(
                "supervisor maintenance control exists without its generation ledger"
            )
        elif ledger["commit_state"] == GENERATION_PENDING:
            if not _pending_matches_request(
                ledger,
                request_id=request_id,
                operator_identity=operator_identity,
                reason=reason,
                target_schema_version=target_schema_version,
            ):
                raise SupervisorControlBlocked(
                    "a different generation commit is incomplete"
                )
            expected = _control_from_ledger(ledger)
            if control is not None and control != expected:
                if not (
                    control["mode"] == CONTROL_MODE_ACTIVE
                    and int(control["cutover_generation"]) + 1
                    == int(ledger["last_issued_generation"])
                ):
                    raise SupervisorControlBlocked(
                        "pending generation cannot be reconciled with control state"
                    )
        else:
            if control is None:
                raise SupervisorControlBlocked(
                    "supervisor maintenance generation ledger exists without control state"
                )
            if not _ledger_matches_control(ledger, control):
                raise SupervisorControlBlocked(
                    "supervisor maintenance control and generation ledger disagree"
                )
            if control["mode"] == CONTROL_MODE_MIGRATION_SUSPENDED:
                raise SupervisorControlBlocked(
                    "supervisor is already suspended for an in-flight transition"
                )
            now = _utc_now()
            ledger = {
                "schema_version": GENERATION_SCHEMA_VERSION,
                "last_issued_generation": _next_generation(
                    ledger["last_issued_generation"]
                ),
                "request_id": request_id,
                "operator_identity": operator_identity,
                "reason": reason,
                "target_schema_version": target_schema_version,
                "requested_at": now,
                "commit_state": GENERATION_PENDING,
            }
            _write_json_file(directory, CONTROL_GENERATION_FILE, ledger)

        state = _control_from_ledger(ledger)
        if control != state:
            _write_json_file(directory, CONTROL_STATE_FILE, state)
        committed = {**ledger, "commit_state": GENERATION_COMMITTED}
        _write_json_file(directory, CONTROL_GENERATION_FILE, committed)
        return {
            "status": CONTROL_MODE_MIGRATION_SUSPENDED,
            "control": state,
            "observed_matches_control": False,
            "resume_eligible": False,
            **control_paths(state_dir),
        }


def resume_supervisor_control(
    state_dir: Path,
    *,
    cutover_generation: str,
    request_id: str,
    trusted_root: Optional[Path] = None,
) -> Dict[str, Any]:
    root = trusted_root or _MODULE_REPO_ROOT
    cutover_generation = _validate_generation(cutover_generation) or ""
    request_id = _validate_token(request_id, "request_id")
    with _locked_directory(state_dir, trusted_root=root, create=False) as directory:
        if directory is None:
            raise SupervisorControlBlocked(
                "supervisor resume requires existing control history"
            )
        control, _ledger, observation = _read_committed_snapshot(directory)
        if _read_legacy_retirement(directory) is not None:
            raise SupervisorControlBlocked(
                "legacy runtime is permanently retired; resume is refused"
            )
        if _read_legacy_child_snapshot(directory) is not None:
            raise SupervisorControlBlocked(
                "legacy child snapshot is immutable; old generation resume is refused"
            )
        if control is None:
            raise SupervisorControlBlocked(
                "supervisor resume requires an existing suspended control record"
            )
        if control["mode"] != CONTROL_MODE_MIGRATION_SUSPENDED:
            raise SupervisorControlBlocked(
                "supervisor resume requires the matching suspended transition"
            )
        if (
            control["cutover_generation"] != cutover_generation
            or control["request_id"] != request_id
        ):
            raise SupervisorControlBlocked(
                "stale supervisor resume compare-and-swap was refused"
            )
        eligible, reason_code = _resume_eligibility(
            control,
            observation,
            trusted_root=root,
            probe_process=True,
        )
        if not eligible:
            raise SupervisorControlBlocked(
                "supervisor resume is not eligible: {}".format(reason_code)
            )
        state = {**control, "mode": CONTROL_MODE_ACTIVE, "updated_at": _utc_now()}
        _write_json_file(directory, CONTROL_STATE_FILE, state)
        return {
            "status": CONTROL_MODE_ACTIVE,
            "control": state,
            "resume_cas": "MATCHED",
            "observed_matches_control": False,
            "resume_eligible": False,
            **control_paths(state_dir),
        }


def _validate_legacy_runtime_stop(
    directory: int,
    *,
    cutover_generation: str,
    request_id: str,
    trusted_root: Path,
) -> Dict[str, Any]:
    control, _ledger, observation = _read_committed_snapshot(directory)
    if _read_legacy_retirement(directory) is not None:
        raise SupervisorControlBlocked(
            "legacy runtime is permanently retired; repeated stop is refused"
        )
    if (
        control is None
        or control["mode"] != CONTROL_MODE_MIGRATION_SUSPENDED
        or control["cutover_generation"] != cutover_generation
        or control["request_id"] != request_id
    ):
        raise SupervisorControlBlocked(
            "legacy runtime stop requires the exact suspended transition"
        )
    if not _observation_matches_control(control, observation):
        raise SupervisorControlBlocked(
            "legacy runtime stop requires the matching durable suspended receipt"
        )
    assert observation is not None
    child_snapshot = _read_legacy_child_snapshot(directory)
    if child_snapshot is None:
        raise SupervisorControlBlocked(
            "legacy runtime stop requires the generation-bound child snapshot"
        )
    if (
        child_snapshot["cutover_generation"] != control["cutover_generation"]
        or child_snapshot["request_id"] != control["request_id"]
        or child_snapshot["supervisor_release_sha256"]
        != observation["supervisor_release_sha256"]
        or child_snapshot["supervisor_runtime_schema_version"]
        != observation["supervisor_runtime_schema_version"]
        or child_snapshot["supervisor_start_token"]
        == observation["supervisor_start_token"]
        or _parse_timestamp(child_snapshot["captured_at"], "captured_at")
        > _parse_timestamp(observation["observed_at"], "observed_at")
    ):
        raise SupervisorControlBlocked(
            "legacy child snapshot provenance does not match the current owner"
        )
    age = datetime.now(timezone.utc) - _parse_timestamp(
        observation["observed_at"],
        "observed_at",
    )
    if age.total_seconds() < -5 or age.total_seconds() > MAX_OBSERVATION_AGE_SECONDS:
        raise SupervisorControlBlocked(
            "legacy runtime stop requires a fresh suspended receipt"
        )
    current = _probe_canonical_supervisor(
        trusted_root,
        expected_pid=observation["supervisor_pid"],
    )
    for field in (
        "supervisor_pid",
        "supervisor_start_token",
        "supervisor_command_sha256",
        "supervisor_cwd",
        "supervisor_launchd_label",
    ):
        if current[field] != observation[field]:
            raise SupervisorControlBlocked(
                "legacy runtime stop supervisor identity changed"
            )
    if observation["supervisor_release_sha256"] != SUPERVISOR_RELEASE_SHA256:
        raise SupervisorControlBlocked(
            "legacy runtime stop receipt release is stale"
        )
    if (
        observation["supervisor_runtime_schema_version"]
        != SUPERVISOR_RUNTIME_SCHEMA_VERSION
        or observation["supervisor_runtime_schema_version"]
        == control["target_schema_version"]
    ):
        raise SupervisorControlBlocked(
            "legacy runtime stop requires the pre-target supervisor capability"
        )
    return {
        **control,
        "_observation": observation,
        "_legacy_child_snapshot": child_snapshot,
    }


def _commit_legacy_retirement(
    directory: int,
    control: Dict[str, Any],
) -> Dict[str, Any]:
    if _read_legacy_retirement(directory) is not None:
        raise SupervisorControlBlocked(
            "legacy retirement receipt already exists"
        )
    observation = control["_observation"]
    retirement = {
        "schema_version": LEGACY_RETIREMENT_SCHEMA_VERSION,
        "cutover_generation": control["cutover_generation"],
        "request_id": control["request_id"],
        "supervisor_pid": observation["supervisor_pid"],
        "supervisor_start_token": observation["supervisor_start_token"],
        "supervisor_release_sha256": observation[
            "supervisor_release_sha256"
        ],
        "supervisor_observation_sha256": _supervisor_observation_sha256(
            observation
        ),
        "legacy_child_snapshot_sha256": _legacy_child_snapshot_sha256(
            control["_legacy_child_snapshot"]
        ),
        "services_terminal": True,
        "managed_orphans_absent": True,
        "service_ports_unbound": True,
        "writer_lock_unheld": True,
        "retired_at": _utc_now(),
    }
    _write_json_file(directory, LEGACY_RETIREMENT_FILE, retirement)
    return retirement


@contextmanager
def legacy_runtime_stop_fence(
    state_dir: Path,
    *,
    cutover_generation: str,
    request_id: str,
    trusted_root: Optional[Path] = None,
) -> Iterator[Dict[str, Any]]:
    """Hold the control CAS lock while exact legacy processes are stopped."""

    root = trusted_root or _MODULE_REPO_ROOT
    cutover_generation = _validate_generation(cutover_generation) or ""
    request_id = _validate_token(request_id, "request_id")
    with _locked_directory(state_dir, trusted_root=root, create=False) as directory:
        if directory is None:
            raise SupervisorControlBlocked(
                "legacy runtime stop requires existing control history"
            )
        control = _validate_legacy_runtime_stop(
            directory,
            cutover_generation=cutover_generation,
            request_id=request_id,
            trusted_root=root,
        )
        transaction = {
            "status": "AUTHORIZED",
            "mode": control["mode"],
            "cutover_generation": control["cutover_generation"],
            "request_id": control["request_id"],
            "observed_matches_control": True,
            "retirement_committed": False,
            "_legacy_child_snapshot": control["_legacy_child_snapshot"],
            "_commit_retirement": lambda: _commit_legacy_retirement(
                directory,
                control,
            ),
            **control_paths(state_dir),
        }
        yield transaction


def authorize_legacy_runtime_stop(
    state_dir: Path,
    *,
    cutover_generation: str,
    request_id: str,
    trusted_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read-only authorization probe for the legacy teardown contract."""

    with legacy_runtime_stop_fence(
        state_dir,
        cutover_generation=cutover_generation,
        request_id=request_id,
        trusted_root=trusted_root,
    ) as authorization:
        return {
            key: value
            for key, value in authorization.items()
            if not key.startswith("_")
        }


def observe_supervisor_control(
    state_dir: Path,
    *,
    trusted_root: Optional[Path] = None,
) -> Dict[str, Any]:
    root = trusted_root or _MODULE_REPO_ROOT
    with _locked_directory(state_dir, trusted_root=root, create=False) as directory:
        if directory is None:
            return {
                "status": "READY",
                "mode": CONTROL_MODE_ACTIVE,
                "cutover_generation": None,
                "request_id": None,
                "observed_receipt": None,
                "observed_matches_control": True,
                "resume_eligible": False,
                **control_paths(state_dir),
            }
        control, _ledger, _observation = _read_committed_snapshot(directory)
        if control is None:
            return {
                "status": "READY",
                "mode": CONTROL_MODE_ACTIVE,
                "cutover_generation": None,
                "request_id": None,
                "observed_receipt": None,
                "observed_matches_control": True,
                "resume_eligible": False,
                **control_paths(state_dir),
            }
        retirement = _read_legacy_retirement(directory)
        if retirement is not None:
            return {
                "status": "LEGACY_RETIRED",
                "mode": control["mode"],
                "cutover_generation": control["cutover_generation"],
                "request_id": control["request_id"],
                "observed_receipt": _observation,
                "observed_matches_control": _observation_matches_control(
                    control,
                    _observation,
                ),
                "legacy_retired": True,
                "legacy_retirement_receipt": retirement,
                "resume_eligible": False,
                **control_paths(state_dir),
            }
        identity = _probe_canonical_supervisor(root, require_self=True)
        if (
            control["mode"] == CONTROL_MODE_ACTIVE
            and SUPERVISOR_RUNTIME_SCHEMA_VERSION
            != control["target_schema_version"]
        ):
            raise SupervisorControlBlocked(
                "active maintenance state requires a newer supervisor schema capability"
            )
        receipt = {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            **identity,
            "supervisor_instance_id": SUPERVISOR_INSTANCE_ID,
            "supervisor_release_sha256": SUPERVISOR_RELEASE_SHA256,
            "supervisor_runtime_schema_version": SUPERVISOR_RUNTIME_SCHEMA_VERSION,
            "observed_generation": control["cutover_generation"],
            "observed_request_id": control["request_id"],
            "observed_mode": control["mode"],
            "observed_at": _utc_now(),
        }
        _write_json_file(directory, CONTROL_OBSERVATION_FILE, receipt)
        eligible, _reason_code = _resume_eligibility(
            control,
            receipt,
            trusted_root=root,
            probe_process=False,
        )
        return {
            "status": "READY",
            "mode": control["mode"],
            "cutover_generation": control["cutover_generation"],
            "request_id": control["request_id"],
            "observed_receipt": receipt,
            "observed_matches_control": True,
            "resume_eligible": eligible,
            **control_paths(state_dir),
        }


def supervisor_control_status(
    state_dir: Path,
    *,
    trusted_root: Optional[Path] = None,
) -> Dict[str, Any]:
    root = trusted_root or _MODULE_REPO_ROOT
    with _locked_directory(state_dir, trusted_root=root, create=False) as directory:
        if directory is None:
            return {
                "status": "READY",
                "mode": CONTROL_MODE_ACTIVE,
                "control": _default_control(),
                "observed_receipt": None,
                "observed_matches_control": True,
                "resume_eligible": False,
                "resume_block_reason": "NO_SUSPENDED_CONTROL",
                **control_paths(state_dir),
            }
        control, _ledger, observation = _read_committed_snapshot(directory)
        if control is None:
            return {
                "status": "READY",
                "mode": CONTROL_MODE_ACTIVE,
                "control": _default_control(),
                "observed_receipt": None,
                "observed_matches_control": True,
                "resume_eligible": False,
                "resume_block_reason": "NO_SUSPENDED_CONTROL",
                **control_paths(state_dir),
            }
        matches = _observation_matches_control(control, observation)
        retirement = _read_legacy_retirement(directory)
        if retirement is not None:
            return {
                "status": "LEGACY_RETIRED",
                "mode": control["mode"],
                "control": {**control, "source": "FILE"},
                "observed_receipt": observation,
                "observed_matches_control": matches,
                "legacy_retired": True,
                "legacy_retirement_receipt": retirement,
                "resume_eligible": False,
                "resume_block_reason": "LEGACY_RETIRED",
                **control_paths(state_dir),
            }
        eligible, reason_code = _resume_eligibility(
            control,
            observation,
            trusted_root=root,
            probe_process=matches,
        )
        return {
            "status": "READY",
            "mode": control["mode"],
            "control": {**control, "source": "FILE"},
            "observed_receipt": observation,
            "observed_matches_control": matches,
            "resume_eligible": eligible,
            "resume_block_reason": reason_code,
            **control_paths(state_dir),
        }
