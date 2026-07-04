from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from app.adapters.freqtrade.cli_runner import (
    FreqtradeCliRunner,
    FreqtradeCommand,
    FreqtradeCommandResult,
)
from app.adapters.freqtrade.market_data_index import SUPPORTED_DATA_SUFFIXES
from app.schemas.hyperopt_profile import HyperoptProfile


FreqtradeHyperoptArtifactStatus = Literal["SUCCESS", "FAILED", "BLOCKED"]
MAX_MANIFEST_LOG_CHARS = 4000
SECRET_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?secret|secret|password|passphrase|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


@dataclass(frozen=True)
class FreqtradeHyperoptExecution:
    result_path: Path
    command_args: list[str]
    command_result: FreqtradeCommandResult


@dataclass(frozen=True)
class FreqtradeHyperoptArtifactManifest:
    manifest_version: int
    status: FreqtradeHyperoptArtifactStatus
    profile_name: str
    strategy_version_id: int
    strategy_name: str
    strategy_file_path: Path
    config_path: Path
    userdir: Optional[Path]
    datadir: Optional[Path]
    pair: str
    timeframe: str
    timerange: str
    spaces: list[str]
    epochs: int
    hyperopt_loss: str
    command_args: list[str]
    return_code: Optional[int]
    stdout: str
    stderr: str
    result_path: Path
    manifest_path: Path
    best_params_path: Optional[Path] = None
    blocked_reason: Optional[str] = None
    failed_reason: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_version": self.manifest_version,
            "status": self.status,
            "profile_name": self.profile_name,
            "strategy_version_id": self.strategy_version_id,
            "strategy_name": self.strategy_name,
            "strategy_file_path": str(self.strategy_file_path),
            "config_path": str(self.config_path),
            "userdir": str(self.userdir) if self.userdir is not None else None,
            "datadir": str(self.datadir) if self.datadir is not None else None,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "timerange": self.timerange,
            "spaces": list(self.spaces),
            "epochs": self.epochs,
            "hyperopt_loss": self.hyperopt_loss,
            "command_args": self.command_args,
            "return_code": self.return_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "result_path": str(self.result_path),
            "best_params_path": (
                str(self.best_params_path) if self.best_params_path is not None else None
            ),
            "manifest_path": str(self.manifest_path),
            "blocked_reason": self.blocked_reason,
            "failed_reason": self.failed_reason,
        }

    def write(self) -> Path:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self.manifest_path


class FreqtradeHyperoptRunner:
    """Runs Freqtrade hyperopt through the safe CLI runner boundary."""

    def __init__(self, cli_runner: FreqtradeCliRunner) -> None:
        self._cli_runner = cli_runner

    def run_hyperopt_with_artifact_manifest(
        self,
        profile: HyperoptProfile,
        config_path: Path,
        result_path: Path,
        manifest_path: Path,
        timeout_seconds: Optional[int] = None,
        datadir: Optional[Path] = None,
        strategy_path: Optional[Path] = None,
        userdir: Optional[Path] = None,
        best_params_path: Optional[Path] = None,
    ) -> FreqtradeHyperoptArtifactManifest:
        resolved_strategy_path = strategy_path or Path(profile.strategy.file_path).parent
        resolved_datadir = datadir or Path(profile.local_data_source.root) / profile.local_data_source.exchange

        command = self._build_hyperopt_command(
            profile=profile,
            config_path=config_path,
            result_path=result_path,
            timeout_seconds=timeout_seconds,
            datadir=resolved_datadir,
            strategy_path=resolved_strategy_path,
            userdir=userdir,
        )
        command_args = self._sanitize_command_args(self._cli_runner.build_args(command))

        blocked_reason = self._local_preflight_blocker(
            config_path=config_path,
            strategy_file_path=Path(profile.strategy.file_path),
            strategy_path=resolved_strategy_path,
            datadir=resolved_datadir,
        )
        if blocked_reason is not None:
            return self._write_manifest(
                profile=profile,
                status="BLOCKED",
                config_path=config_path,
                result_path=result_path,
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=None,
                stdout="",
                stderr="",
                datadir=resolved_datadir,
                strategy_path=resolved_strategy_path,
                userdir=userdir,
                best_params_path=best_params_path,
                blocked_reason=blocked_reason,
            )

        try:
            command_result = self._cli_runner.run_unchecked(command)
        except FileNotFoundError as exc:
            return self._write_manifest(
                profile=profile,
                status="BLOCKED",
                config_path=config_path,
                result_path=result_path,
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=None,
                stdout="",
                stderr=_sanitize_and_tail(str(exc)),
                datadir=resolved_datadir,
                strategy_path=resolved_strategy_path,
                userdir=userdir,
                best_params_path=best_params_path,
                blocked_reason="freqtrade binary is not available",
            )

        if command_result.return_code != 0:
            return self._write_manifest(
                profile=profile,
                status="FAILED",
                config_path=config_path,
                result_path=result_path,
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=command_result.return_code,
                stdout=command_result.stdout,
                stderr=command_result.stderr,
                datadir=resolved_datadir,
                strategy_path=resolved_strategy_path,
                userdir=userdir,
                best_params_path=best_params_path,
                failed_reason=f"Freqtrade hyperopt exited with code {command_result.return_code}",
            )

        if not result_path.exists():
            return self._write_manifest(
                profile=profile,
                status="FAILED",
                config_path=config_path,
                result_path=result_path,
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=command_result.return_code,
                stdout=command_result.stdout,
                stderr=command_result.stderr,
                datadir=resolved_datadir,
                strategy_path=resolved_strategy_path,
                userdir=userdir,
                best_params_path=best_params_path,
                failed_reason=f"Freqtrade hyperopt result JSON was not generated: {result_path}",
            )

        if best_params_path is not None and not best_params_path.exists():
            return self._write_manifest(
                profile=profile,
                status="FAILED",
                config_path=config_path,
                result_path=result_path,
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=command_result.return_code,
                stdout=command_result.stdout,
                stderr=command_result.stderr,
                datadir=resolved_datadir,
                strategy_path=resolved_strategy_path,
                userdir=userdir,
                best_params_path=best_params_path,
                failed_reason=f"Freqtrade hyperopt best params JSON was not generated: {best_params_path}",
            )

        return self._write_manifest(
            profile=profile,
            status="SUCCESS",
            config_path=config_path,
            result_path=result_path,
            manifest_path=manifest_path,
            command_args=command_args,
            return_code=command_result.return_code,
            stdout=command_result.stdout,
            stderr=command_result.stderr,
            datadir=resolved_datadir,
            strategy_path=resolved_strategy_path,
            userdir=userdir,
            best_params_path=best_params_path,
        )

    def _build_hyperopt_command(
        self,
        profile: HyperoptProfile,
        config_path: Path,
        result_path: Path,
        timeout_seconds: Optional[int],
        datadir: Path,
        strategy_path: Path,
        userdir: Optional[Path],
    ) -> FreqtradeCommand:
        options = {
            "--config": config_path,
            "--datadir": datadir,
            "--epochs": profile.epochs,
            "--export": "trades",
            "--export-filename": result_path,
            "--hyperopt-loss": profile.hyperopt_loss,
            "--print-json": True,
            "--spaces": profile.spaces,
            "--strategy": profile.strategy.name,
            "--strategy-path": strategy_path,
            "--timeframe": profile.timeframe,
            "--timerange": profile.timerange,
        }
        if profile.random_state is not None:
            options["--random-state"] = profile.random_state
        if userdir is not None:
            options["--userdir"] = userdir

        return FreqtradeCommand(
            command="hyperopt",
            options=options,
            timeout_seconds=timeout_seconds,
        )

    def _local_preflight_blocker(
        self,
        config_path: Path,
        strategy_file_path: Path,
        strategy_path: Path,
        datadir: Path,
    ) -> Optional[str]:
        if not config_path.exists():
            return f"hyperopt config file does not exist: {config_path}"
        if not strategy_path.exists():
            return f"strategy path does not exist: {strategy_path}"
        if not strategy_file_path.exists():
            return f"strategy file does not exist: {strategy_file_path}"
        if not datadir.exists():
            return f"local market data directory does not exist: {datadir}"
        if not datadir.is_dir():
            return f"local market data path is not a directory: {datadir}"
        if not any(self._is_supported_market_data_file(path) for path in datadir.rglob("*")):
            return f"no supported local market data files found under {datadir}"
        return None

    def _is_supported_market_data_file(self, path: Path) -> bool:
        if not path.is_file():
            return False
        filename = path.name.lower()
        return any(filename.endswith(suffix) for suffix in SUPPORTED_DATA_SUFFIXES)

    def _write_manifest(
        self,
        profile: HyperoptProfile,
        status: FreqtradeHyperoptArtifactStatus,
        config_path: Path,
        result_path: Path,
        manifest_path: Path,
        command_args: list[str],
        return_code: Optional[int],
        stdout: str,
        stderr: str,
        datadir: Optional[Path] = None,
        strategy_path: Optional[Path] = None,
        userdir: Optional[Path] = None,
        best_params_path: Optional[Path] = None,
        blocked_reason: Optional[str] = None,
        failed_reason: Optional[str] = None,
    ) -> FreqtradeHyperoptArtifactManifest:
        manifest = FreqtradeHyperoptArtifactManifest(
            manifest_version=1,
            status=status,
            profile_name=profile.name,
            strategy_version_id=profile.strategy.version_id,
            strategy_name=profile.strategy.name,
            strategy_file_path=Path(profile.strategy.file_path),
            config_path=config_path,
            userdir=userdir,
            datadir=datadir,
            pair=profile.pair,
            timeframe=profile.timeframe,
            timerange=profile.timerange,
            spaces=list(profile.spaces),
            epochs=profile.epochs,
            hyperopt_loss=profile.hyperopt_loss,
            command_args=command_args,
            return_code=return_code,
            stdout=_sanitize_and_tail(stdout),
            stderr=_sanitize_and_tail(stderr),
            result_path=result_path,
            best_params_path=best_params_path,
            manifest_path=manifest_path,
            blocked_reason=blocked_reason,
            failed_reason=failed_reason,
        )
        manifest.write()
        return manifest

    def _sanitize_command_args(self, command_args: list[str]) -> list[str]:
        return [_sanitize_and_tail(arg, max_chars=1000) for arg in command_args]


def _sanitize_and_tail(text: str, max_chars: int = MAX_MANIFEST_LOG_CHARS) -> str:
    redacted = SECRET_VALUE_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    redacted = BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED]", redacted)
    if len(redacted) <= max_chars:
        return redacted
    return redacted[-max_chars:]
