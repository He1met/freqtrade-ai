from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Callable, Mapping, Optional


Which = Callable[[str], Optional[str]]
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_RUNTIME_ENV_PATH = REPO_ROOT / ".freqtrade-ai" / "runtime.env"


@dataclass(frozen=True)
class FreqtradeBinaryResolution:
    configured: str
    source: str
    resolved_path: Optional[Path]
    blocked_reason: Optional[str]

    @property
    def ready(self) -> bool:
        return self.resolved_path is not None and self.blocked_reason is None


def resolve_freqtrade_binary(
    *,
    environ: Optional[Mapping[str, str]] = None,
    which: Optional[Which] = None,
    runtime_env_path: Path = DEFAULT_RUNTIME_ENV_PATH,
) -> FreqtradeBinaryResolution:
    """Resolve the sole Freqtrade binary contract for every local entrypoint.

    An explicitly injected environment always wins.  If it is absent, use the
    canonical, non-secret ``runtime.env`` selector before consulting ``PATH``.
    This keeps doctor, API workers, launchd and standalone diagnostics on the
    same executable without loading any other runtime value from that file.
    """

    environment = environ if environ is not None else os.environ
    path_lookup = which or shutil.which
    configured = str(environment.get("FREQTRADE_BINARY", "")).strip()
    source = "FREQTRADE_BINARY"
    if not configured:
        configured = runtime_env_freqtrade_binary(runtime_env_path)
        source = "runtime.env" if configured else "PATH"
    candidate = configured or "freqtrade"

    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_absolute():
            resolved = configured_path.resolve()
        elif "/" in configured or "\\" in configured:
            return FreqtradeBinaryResolution(
                configured=candidate,
                source=source,
                resolved_path=None,
                blocked_reason="FREQTRADE_BINARY must be an absolute executable path",
            )
        else:
            discovered = path_lookup(configured)
            resolved = Path(discovered).resolve() if discovered else None
    else:
        discovered = path_lookup("freqtrade")
        resolved = Path(discovered).resolve() if discovered else None

    if resolved is None:
        return FreqtradeBinaryResolution(
            configured=candidate,
            source=source,
            resolved_path=None,
            blocked_reason=f"freqtrade binary is not available: {candidate}",
        )
    if not resolved.exists():
        reason = f"freqtrade binary does not exist: {resolved}"
    elif not resolved.is_file():
        reason = f"freqtrade binary path is not a file: {resolved}"
    elif not os.access(resolved, os.X_OK):
        reason = f"freqtrade binary is not executable: {resolved}"
    else:
        reason = None
    return FreqtradeBinaryResolution(
        configured=candidate,
        source=source,
        resolved_path=resolved,
        blocked_reason=reason,
    )


def runtime_env_freqtrade_binary(path: Path = DEFAULT_RUNTIME_ENV_PATH) -> str:
    """Read only the non-secret binary selector from the canonical runtime file."""

    try:
        if not path.is_file() or path.is_symlink():
            return ""
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key == "FREQTRADE_BINARY":
                return value
    except OSError:
        return ""
    return ""
