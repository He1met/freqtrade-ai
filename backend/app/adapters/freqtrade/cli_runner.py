from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FreqtradeCommand:
    args: list[str]
    cwd: Path | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class FreqtradeCommandResult:
    return_code: int
    stdout: str
    stderr: str


class FreqtradeCliRunner:
    """Single boundary for future Freqtrade CLI execution."""

    def run(self, command: FreqtradeCommand) -> FreqtradeCommandResult:
        raise NotImplementedError("Phase 0 only defines the Freqtrade CLI boundary.")
