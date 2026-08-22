#!/usr/bin/env python3
"""Run one canonical V1.3 no-trade research action behind the DB credential boundary."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
api_service = importlib.import_module("scripts.canonical_v13_api_service")
RESEARCH_CLI_PATH = REPO_ROOT / "backend/scripts/canonical_v13_research.py"
_COMMANDS = {"gate", "worker-execute"}
_EXECUTION_FIELDS = {
    "api_base_url",
    "oci_runtime",
    "image_reference",
    "market_artifact_root",
    "workspace_root",
    "cpu_limit",
    "memory_mb",
    "timeout_seconds",
    "output_bytes",
    "pids_limit",
    "tmpfs_mb",
}


class CanonicalResearchServiceBlocked(RuntimeError):
    pass


def _owned_json_file(path: Path) -> dict[str, object]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise CanonicalResearchServiceBlocked("BLOCKED_SUPERVISOR_COMMAND_FILE_PATH")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o600:
        raise CanonicalResearchServiceBlocked("BLOCKED_SUPERVISOR_COMMAND_FILE_MODE")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalResearchServiceBlocked(
            "BLOCKED_SUPERVISOR_COMMAND_FILE_INVALID"
        ) from exc
    if not isinstance(payload, dict):
        raise CanonicalResearchServiceBlocked(
            "BLOCKED_SUPERVISOR_COMMAND_FILE_INVALID"
        )
    return payload


def _load_research_cli() -> ModuleType:
    backend = str(REPO_ROOT / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    spec = importlib.util.spec_from_file_location(
        "canonical_v13_supervised_research_cli", RESEARCH_CLI_PATH
    )
    if spec is None or spec.loader is None:
        raise CanonicalResearchServiceBlocked("BLOCKED_RESEARCH_CLI_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _execution_environment(
    cli: ModuleType, *, command: str, execution: object
) -> dict[str, str]:
    if not isinstance(execution, dict) or set(execution) != _EXECUTION_FIELDS:
        raise CanonicalResearchServiceBlocked("BLOCKED_SUPERVISOR_EXECUTION_FIELDS")
    environment = {
        cli.API_BASE_ENV: str(execution["api_base_url"]),
        cli.OCI_RUNTIME_ENV: str(execution["oci_runtime"]),
        cli.IMAGE_ENV: str(execution["image_reference"]),
        cli.MARKET_ROOT_ENV: str(execution["market_artifact_root"]),
        cli.WORKSPACE_ROOT_ENV: str(execution["workspace_root"]),
        cli.CPU_LIMIT_ENV: str(execution["cpu_limit"]),
        cli.MEMORY_LIMIT_ENV: str(execution["memory_mb"]),
        cli.TIMEOUT_LIMIT_ENV: str(execution["timeout_seconds"]),
        cli.OUTPUT_LIMIT_ENV: str(execution["output_bytes"]),
        cli.PIDS_LIMIT_ENV: str(execution["pids_limit"]),
        cli.TMPFS_LIMIT_ENV: str(execution["tmpfs_mb"]),
    }
    if command == "gate":
        environment[cli.LOOKAHEAD_ACTIVATION_ENV] = (
            "PRODUCTION_LOOKAHEAD_NO_TRADE_V1"
        )
    else:
        environment[cli.ACTIVATION_ENV] = "PRODUCTION_RESEARCH_NO_TRADE_V1"
    return environment


@contextmanager
def _single_executor_lock(workspace_root: Path):
    try:
        info = workspace_root.stat()
    except OSError as exc:
        raise CanonicalResearchServiceBlocked(
            "BLOCKED_SUPERVISOR_LOCK_ROOT"
        ) from exc
    if (
        workspace_root.is_symlink()
        or not workspace_root.is_dir()
        or info.st_uid != os.getuid()
        or info.st_mode & 0o777 != 0o700
    ):
        raise CanonicalResearchServiceBlocked("BLOCKED_SUPERVISOR_LOCK_ROOT")
    path = workspace_root / ".canonical-v13-research-supervisor.lock"
    descriptor = os.open(
        path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
    )
    handle = os.fdopen(descriptor, "r+")
    try:
        if (
            os.fstat(handle.fileno()).st_uid != os.getuid()
            or os.fstat(handle.fileno()).st_mode & 0o777 != 0o600
        ):
            raise CanonicalResearchServiceBlocked("BLOCKED_SUPERVISOR_LOCK_FILE")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CanonicalResearchServiceBlocked(
                "BLOCKED_RESEARCH_EXECUTOR_ALREADY_ACTIVE"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _database_environment(cli: ModuleType, *, command: str) -> dict[str, str]:
    environment = {
        cli.READER_DATABASE_URL_ENV: api_service._database_url(
            api_service.READER_PRINCIPAL, api_service.READER_KEYCHAIN_SERVICE
        )
    }
    if command == "worker-execute":
        research_specs = api_service.RESEARCH_PRINCIPAL_SPECS[:3]
        environment.update(
            {
                cli.VALIDATION_DATABASE_URL_ENV: api_service._database_url(
                    research_specs[0][0], research_specs[0][2]
                ),
                cli.SCORING_DATABASE_URL_ENV: api_service._database_url(
                    research_specs[1][0], research_specs[1][2]
                ),
                cli.QUALIFICATION_DATABASE_URL_ENV: api_service._database_url(
                    research_specs[2][0], research_specs[2][2]
                ),
            }
        )
    return environment


def execute(command_file: Path) -> dict[str, object]:
    command = _owned_json_file(command_file)
    if set(command) != {"command", "research_command_file", "execution"}:
        raise CanonicalResearchServiceBlocked("BLOCKED_SUPERVISOR_COMMAND_FIELDS")
    action = command["command"]
    if not isinstance(action, str) or action not in _COMMANDS:
        raise CanonicalResearchServiceBlocked("BLOCKED_SUPERVISOR_COMMAND")
    research_command_path = Path(str(command["research_command_file"]))
    research_command = _owned_json_file(research_command_path)
    cli = _load_research_cli()
    environment = _execution_environment(
        cli, command=str(action), execution=command["execution"]
    )
    workspace_root = Path(environment[cli.WORKSPACE_ROOT_ENV])
    with _single_executor_lock(workspace_root):
        api_service.require_release_checkout()
        environment.update(_database_environment(cli, command=str(action)))
        if action == "gate":
            with cli._gate_writer_lock(environment):
                return cli._gate_execute(environment, research_command)
        return cli._worker_execute(environment, research_command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("execute",))
    parser.add_argument("--command-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = execute(args.command_file)
    except (CanonicalResearchServiceBlocked, api_service.CanonicalServiceBlocked) as exc:
        result = {"status": "BLOCKED", "reason_code": str(exc)}
    except Exception:
        result = {
            "status": "BLOCKED",
            "reason_code": "BLOCKED_RESEARCH_SUPERVISOR_FAILURE",
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 2 if result.get("status") == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(main())
