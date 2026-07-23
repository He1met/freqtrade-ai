from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Callable, Mapping, Optional


Which = Callable[[str], Optional[str]]


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
) -> FreqtradeBinaryResolution:
    environment = environ if environ is not None else os.environ
    path_lookup = which or shutil.which
    configured = str(environment.get("FREQTRADE_BINARY", "")).strip()
    source = "FREQTRADE_BINARY" if configured else "PATH"
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
