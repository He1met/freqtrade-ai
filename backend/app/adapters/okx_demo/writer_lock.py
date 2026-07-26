from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat
from typing import IO, Optional

from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked


class OkxDemoWriterProcessLock:
    """Non-blocking local guard; the database lease remains authoritative."""

    def __init__(self, path: Path) -> None:
        resolved = path.expanduser()
        if not resolved.is_absolute() or resolved.name in {"", ".", ".."}:
            raise OkxDemoWriteBlocked("writer process lock path must be absolute")
        self._path = resolved
        self._handle: Optional[IO[str]] = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise OkxDemoWriteBlocked("writer process lock is already held")
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self._path,
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError:
            raise OkxDemoWriteBlocked(
                "writer process lock path is not a safe local file"
            ) from None
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            os.close(descriptor)
            raise OkxDemoWriteBlocked(
                "writer process lock file ownership or permissions are unsafe"
            )
        handle = os.fdopen(descriptor, "r+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise OkxDemoWriteBlocked(
                "another OKX_DEMO writer process holds the local lock"
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "OkxDemoWriterProcessLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()
