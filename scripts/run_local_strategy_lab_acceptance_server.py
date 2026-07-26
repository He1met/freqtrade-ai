#!/usr/bin/env python3
"""Safely seed, supervise, and clean the isolated Issue #433 backend."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from seed_local_strategy_lab_acceptance import create_seed


SAFE_INHERITED_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ")
TEST_DISABLE_ENV_FILE_ENV = "FREQTRADE_AI_TEST_DISABLE_ENV_FILE"
FORBIDDEN_ENV_MARKERS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PROVIDER",
    "EXCHANGE",
    "OPERATOR",
    "FREQTRADE",
    "TRADING",
    "ORDER",
)


def build_backend_env(
    manifest: Mapping[str, object],
    source_env: Mapping[str, str],
) -> dict[str, str]:
    """Build a minimal environment; never copy the caller environment wholesale."""

    environment = {
        name: source_env[name]
        for name in SAFE_INHERITED_ENV
        if source_env.get(name)
    }
    environment.update(
        {
            "APP_ENV": "phase8",
            "DATABASE_URL": str(manifest["database_url"]),
            "FREQTRADE_AI_CANONICAL_REPO_ROOT": str(manifest["canonical_root"]),
            "E2E_ACCEPTANCE_MANIFEST": json.dumps(manifest, sort_keys=True),
            TEST_DISABLE_ENV_FILE_ENV: "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    for name in environment:
        upper = name.upper()
        if name in {
            "FREQTRADE_AI_CANONICAL_REPO_ROOT",
            TEST_DISABLE_ENV_FILE_ENV,
            "E2E_ACCEPTANCE_MANIFEST",
        }:
            continue
        if any(marker in upper for marker in FORBIDDEN_ENV_MARKERS):
            raise RuntimeError(f"unsafe environment name reached acceptance backend: {name}")
    return environment


def cleanup_seed_root(manifest: Mapping[str, object], expected_parent: Path) -> None:
    """Remove exactly the root allocated by this wrapper, without following links."""

    parent = expected_parent.expanduser().resolve(strict=True)
    root = Path(str(manifest["canonical_root"]))
    if (
        root.parent != parent
        or not root.name.startswith("freqtrade-ai-issue-433-")
        or root == parent
    ):
        raise RuntimeError("refusing to clean an unowned acceptance root")
    mode = os.lstat(root).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError("refusing to clean a symlink or non-directory acceptance root")
    manifest_path = Path(str(manifest["manifest_path"]))
    database_path = Path(str(manifest["database"]))
    if manifest_path.parent != root or not database_path.is_relative_to(root):
        raise RuntimeError("acceptance manifest does not prove root ownership")
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        persisted.get("canonical_root") != str(root)
        or persisted.get("database") != str(database_path)
        or persisted.get("safety") != manifest.get("safety")
    ):
        raise RuntimeError("acceptance cleanup ownership manifest mismatch")
    shutil.rmtree(root)


def write_cleanup_registry(path: Path, manifest: Mapping[str, object], parent: Path) -> None:
    resolved_parent = parent.expanduser().resolve(strict=True)
    if (
        path.parent.resolve(strict=True) != resolved_parent
        or not path.name.startswith("freqtrade-ai-issue-433-registry-")
        or path.suffix != ".json"
    ):
        raise ValueError("unsafe acceptance cleanup registry path")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, json.dumps(manifest, sort_keys=True).encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_server(
    *,
    parent: Path,
    host: str,
    port: int,
    profile: str,
    registry: Optional[Path] = None,
    source_env: Optional[Mapping[str, str]] = None,
) -> int:
    manifest: Optional[Mapping[str, object]] = None
    child: Optional[subprocess.Popen[bytes]] = None
    previous_handlers: dict[int, object] = {}
    stop_signal: Optional[int] = None

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stop_signal
        stop_signal = stop_signal or signum
        if child is not None and child.poll() is None:
            child.terminate()

    try:
        # Install handlers before allocating the seed root or starting Uvicorn.
        # A signal received in either window is remembered and handled after the
        # current atomic operation returns, so the finally block owns cleanup.
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, request_stop)
        if stop_signal is not None:
            return 128 + stop_signal

        manifest = create_seed(parent, profile)
        if stop_signal is not None:
            return 128 + stop_signal
        if registry is not None:
            write_cleanup_registry(registry, manifest, parent)
        if stop_signal is not None:
            return 128 + stop_signal
        environment = build_backend_env(manifest, source_env or os.environ)
        argv = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            host,
            "--port",
            str(port),
        ]
        child = subprocess.Popen(argv, env=environment)
        if stop_signal is not None and child.poll() is None:
            child.terminate()
        child_returncode = child.wait()
        if stop_signal is not None:
            return 128 + stop_signal
        return child_returncode
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if manifest is not None:
            cleanup_seed_root(manifest, parent)
        if registry is not None:
            try:
                registry.unlink()
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--registry", required=True)
    parser.add_argument(
        "--profile",
        choices=("empty", "complete-current", "missing-result", "missing-strategy", "long-evidence"),
        default="complete-current",
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port in {5173, 8000}:
        raise ValueError("unsafe acceptance backend port")
    return run_server(
        parent=Path(args.parent),
        host=args.host,
        port=args.port,
        profile=args.profile,
        registry=Path(args.registry),
    )


if __name__ == "__main__":
    raise SystemExit(main())
