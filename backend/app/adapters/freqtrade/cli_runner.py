from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import subprocess
from pathlib import Path
from typing import Callable, Optional, Union

from app.adapters.freqtrade.exceptions import (
    FreqtradeCommandError,
    FreqtradeCommandValidationError,
)


CommandScalarValue = Union[str, int, float, Path]
CommandOptionValue = Union[CommandScalarValue, bool, Sequence[CommandScalarValue]]
CommandExecutor = Callable[
    [Sequence[str], Optional[Path], Optional[int]],
    subprocess.CompletedProcess[str],
]


ALLOWED_COMMAND_OPTIONS: dict[str, frozenset[str]] = {
    "backtesting": frozenset(
        {
            "--config",
            "--datadir",
            "--export",
            "--export-filename",
            "--pairs",
            "--strategy",
            "--strategy-path",
            "--timeframe",
            "--timerange",
            "--userdir",
        }
    ),
    "list-data": frozenset(
        {
            "--datadir",
            "--exchange",
            "--pairs",
            "--show-timerange",
            "--timeframes",
            "--userdir",
        }
    ),
}


@dataclass(frozen=True)
class FreqtradeCommand:
    command: str
    options: Mapping[str, CommandOptionValue] | None = None
    positional_args: Sequence[str] = ()
    cwd: Path | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class FreqtradeCommandResult:
    return_code: int
    stdout: str
    stderr: str


class FreqtradeCliRunner:
    """Safe boundary for supported Freqtrade CLI commands."""

    def __init__(
        self,
        binary: str = "freqtrade",
        executor: CommandExecutor | None = None,
    ) -> None:
        self._binary = binary
        self._executor = executor or self._subprocess_executor

    def build_args(self, command: FreqtradeCommand) -> list[str]:
        self._validate_command(command)
        args = [self._binary, command.command]

        for option, value in sorted((command.options or {}).items()):
            self._append_option(args, option, value)

        for value in command.positional_args:
            self._validate_value(value)
            args.append(str(value))

        return args

    def run(self, command: FreqtradeCommand) -> FreqtradeCommandResult:
        result = self.run_unchecked(command)
        if result.return_code != 0:
            raise FreqtradeCommandError(
                f"Freqtrade command '{command.command}' exited with code {result.return_code}"
            )
        return result

    def run_unchecked(self, command: FreqtradeCommand) -> FreqtradeCommandResult:
        args = self.build_args(command)
        completed = self._executor(args, command.cwd, command.timeout_seconds)
        return FreqtradeCommandResult(
            return_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def _validate_command(self, command: FreqtradeCommand) -> None:
        if command.command not in ALLOWED_COMMAND_OPTIONS:
            raise FreqtradeCommandValidationError(
                f"Unsupported Freqtrade command: {command.command}"
            )

        allowed_options = ALLOWED_COMMAND_OPTIONS[command.command]
        for option in command.options or {}:
            if option not in allowed_options:
                raise FreqtradeCommandValidationError(
                    f"Unsupported option for '{command.command}': {option}"
                )
            self._validate_option_name(option)

    def _append_option(self, args: list[str], option: str, value: CommandOptionValue) -> None:
        if isinstance(value, bool):
            if value:
                args.append(option)
            return

        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Path)):
            for item in value:
                self._validate_value(item)
                args.extend([option, str(item)])
            return

        self._validate_value(value)
        args.extend([option, str(value)])

    def _validate_option_name(self, option: str) -> None:
        if not option.startswith("--") or any(character.isspace() for character in option):
            raise FreqtradeCommandValidationError(f"Invalid option name: {option}")

    def _validate_value(self, value: object) -> None:
        text = str(value)
        if "\x00" in text or "\n" in text or "\r" in text:
            raise FreqtradeCommandValidationError("Command values must be single-line strings")

    def _subprocess_executor(
        self,
        args: Sequence[str],
        cwd: Path | None,
        timeout_seconds: int | None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            cwd=cwd,
            timeout=timeout_seconds,
            check=False,
            capture_output=True,
            text=True,
        )
