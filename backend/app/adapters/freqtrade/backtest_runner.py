from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from app.adapters.freqtrade.cli_runner import (
    FreqtradeCliRunner,
    FreqtradeCommand,
    FreqtradeCommandResult,
)
from app.adapters.freqtrade.exceptions import FreqtradeCommandError
from app.adapters.freqtrade.market_data_index import SUPPORTED_DATA_SUFFIXES
from app.schemas.dry_run_status import redact_secret_text


FreqtradeBacktestArtifactStatus = Literal["SUCCESS", "FAILED", "BLOCKED"]


@dataclass(frozen=True)
class FreqtradeBacktestExecution:
    result_path: Path
    command_args: list[str]
    command_result: FreqtradeCommandResult


@dataclass(frozen=True)
class FreqtradeBacktestArtifactManifest:
    manifest_version: int
    status: FreqtradeBacktestArtifactStatus
    config_path: Path
    strategy_name: str
    result_path: Path
    manifest_path: Path
    command_args: list[str]
    return_code: Optional[int]
    stdout: str
    stderr: str
    datadir: Optional[Path] = None
    strategy_path: Optional[Path] = None
    userdir: Optional[Path] = None
    blocked_reason: Optional[str] = None
    failed_reason: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_version": self.manifest_version,
            "status": self.status,
            "config_path": str(self.config_path),
            "strategy_name": self.strategy_name,
            "result_path": str(self.result_path),
            "manifest_path": str(self.manifest_path),
            "command_args": self.command_args,
            "return_code": self.return_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "datadir": str(self.datadir) if self.datadir is not None else None,
            "strategy_path": str(self.strategy_path) if self.strategy_path is not None else None,
            "userdir": str(self.userdir) if self.userdir is not None else None,
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


class FreqtradeBacktestRunner:
    """Runs Freqtrade backtests through the safe CLI runner boundary."""

    def __init__(self, cli_runner: FreqtradeCliRunner) -> None:
        self._cli_runner = cli_runner

    def run_backtest(
        self,
        config_path: Path,
        strategy_name: str,
        result_path: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Path:
        execution = self.run_backtest_with_output(
            config_path,
            strategy_name,
            result_path=result_path,
            timeout_seconds=timeout_seconds,
        )
        if execution.command_result.return_code != 0:
            raise FreqtradeCommandError(
                f"Freqtrade backtesting exited with code {execution.command_result.return_code}"
            )
        return execution.result_path

    def run_backtest_with_output(
        self,
        config_path: Path,
        strategy_name: str,
        result_path: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
        datadir: Optional[Path] = None,
        strategy_path: Optional[Path] = None,
        userdir: Optional[Path] = None,
    ) -> FreqtradeBacktestExecution:
        if result_path is None:
            raise ValueError("result_path is required for Phase 1 backtest execution")
        result_path.parent.mkdir(parents=True, exist_ok=True)

        command = self._build_backtesting_command(
            config_path,
            strategy_name,
            result_path,
            timeout_seconds=timeout_seconds,
            datadir=datadir,
            strategy_path=strategy_path,
            userdir=userdir,
        )
        command_result = self._cli_runner.run_unchecked(command)
        if command_result.return_code == 0:
            self._materialize_backtest_result_json(result_path)

        return FreqtradeBacktestExecution(
            result_path=result_path,
            command_args=self._cli_runner.build_args(command),
            command_result=command_result,
        )

    def run_backtest_with_artifact_manifest(
        self,
        config_path: Path,
        strategy_name: str,
        result_path: Path,
        manifest_path: Path,
        timeout_seconds: Optional[int] = None,
        datadir: Optional[Path] = None,
        strategy_path: Optional[Path] = None,
        userdir: Optional[Path] = None,
    ) -> FreqtradeBacktestArtifactManifest:
        command = self._build_backtesting_command(
            config_path,
            strategy_name,
            result_path,
            timeout_seconds=timeout_seconds,
            datadir=datadir,
            strategy_path=strategy_path,
            userdir=userdir,
        )
        command_args = self._cli_runner.build_args(command)

        blocked_reason = self._local_data_blocker(datadir)
        if blocked_reason is not None:
            return self._write_manifest(
                status="BLOCKED",
                config_path=config_path,
                strategy_name=strategy_name,
                result_path=result_path,
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=None,
                stdout="",
                stderr="",
                datadir=datadir,
                strategy_path=strategy_path,
                userdir=userdir,
                blocked_reason=blocked_reason,
            )

        result_path.parent.mkdir(parents=True, exist_ok=True)
        command_result = self._cli_runner.run_unchecked(command)
        if command_result.return_code != 0:
            return self._write_manifest(
                status="FAILED",
                config_path=config_path,
                strategy_name=strategy_name,
                result_path=result_path,
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=command_result.return_code,
                stdout=command_result.stdout,
                stderr=command_result.stderr,
                datadir=datadir,
                strategy_path=strategy_path,
                userdir=userdir,
                failed_reason=(
                    f"Freqtrade backtesting exited with code {command_result.return_code}"
                ),
            )

        self._materialize_backtest_result_json(result_path)
        if not result_path.exists():
            return self._write_manifest(
                status="FAILED",
                config_path=config_path,
                strategy_name=strategy_name,
                result_path=result_path,
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=command_result.return_code,
                stdout=command_result.stdout,
                stderr=command_result.stderr,
                datadir=datadir,
                strategy_path=strategy_path,
                userdir=userdir,
                failed_reason=f"Freqtrade result JSON was not generated: {result_path}",
            )

        return self._write_manifest(
            status="SUCCESS",
            config_path=config_path,
            strategy_name=strategy_name,
            result_path=result_path,
            manifest_path=manifest_path,
            command_args=command_args,
            return_code=command_result.return_code,
            stdout=command_result.stdout,
            stderr=command_result.stderr,
            datadir=datadir,
            strategy_path=strategy_path,
            userdir=userdir,
        )

    def _build_backtesting_command(
        self,
        config_path: Path,
        strategy_name: str,
        result_path: Path,
        timeout_seconds: Optional[int] = None,
        datadir: Optional[Path] = None,
        strategy_path: Optional[Path] = None,
        userdir: Optional[Path] = None,
    ) -> FreqtradeCommand:
        options = {
            "--backtest-directory": result_path.parent,
            "--config": config_path,
            "--strategy": strategy_name,
            "--export": "trades",
        }
        if datadir is not None:
            options["--datadir"] = datadir
        if strategy_path is not None:
            options["--strategy-path"] = strategy_path
        if userdir is not None:
            options["--userdir"] = userdir

        return FreqtradeCommand(
            command="backtesting",
            options=options,
            timeout_seconds=timeout_seconds,
        )

    def _local_data_blocker(self, datadir: Optional[Path]) -> Optional[str]:
        if datadir is None:
            return None
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

    def _materialize_backtest_result_json(self, result_path: Path) -> Optional[Path]:
        if result_path.exists():
            return result_path

        result_dir = result_path.parent
        json_candidate = self._latest_unpacked_backtest_json(result_dir)
        if json_candidate is not None:
            result_path.write_bytes(json_candidate.read_bytes())
            return result_path

        zip_candidate = self._latest_backtest_zip(result_dir)
        if zip_candidate is None:
            return None

        json_name = self._result_json_name_from_zip(zip_candidate)
        if json_name is None:
            return None

        with zipfile.ZipFile(zip_candidate) as archive:
            result_path.write_bytes(archive.read(json_name))
        return result_path

    def _latest_backtest_zip(self, result_dir: Path) -> Optional[Path]:
        last_result_path = result_dir / ".last_result.json"
        if last_result_path.exists():
            try:
                latest = json.loads(last_result_path.read_text(encoding="utf-8")).get(
                    "latest_backtest"
                )
            except json.JSONDecodeError:
                latest = None
            if isinstance(latest, str):
                candidate = result_dir / latest
                if candidate.exists() and candidate.suffix == ".zip":
                    return candidate

        candidates = sorted(
            result_dir.glob("backtest-result-*.zip"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _latest_unpacked_backtest_json(self, result_dir: Path) -> Optional[Path]:
        candidates = [
            path
            for path in result_dir.glob("backtest-result-*.json")
            if not path.name.endswith(".meta.json") and not path.name.endswith("_config.json")
        ]
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return candidates[0] if candidates else None

    def _result_json_name_from_zip(self, zip_path: Path) -> Optional[str]:
        with zipfile.ZipFile(zip_path) as archive:
            candidates = [
                name
                for name in archive.namelist()
                if name.endswith(".json")
                and not name.endswith("_config.json")
                and not name.endswith(".meta.json")
            ]
        return sorted(candidates)[0] if candidates else None

    def _write_manifest(
        self,
        status: FreqtradeBacktestArtifactStatus,
        config_path: Path,
        strategy_name: str,
        result_path: Path,
        manifest_path: Path,
        command_args: list[str],
        return_code: Optional[int],
        stdout: str,
        stderr: str,
        datadir: Optional[Path] = None,
        strategy_path: Optional[Path] = None,
        userdir: Optional[Path] = None,
        blocked_reason: Optional[str] = None,
        failed_reason: Optional[str] = None,
    ) -> FreqtradeBacktestArtifactManifest:
        manifest = FreqtradeBacktestArtifactManifest(
            manifest_version=1,
            status=status,
            config_path=config_path,
            strategy_name=strategy_name,
            result_path=result_path,
            manifest_path=manifest_path,
            command_args=[redact_secret_text(arg) for arg in command_args],
            return_code=return_code,
            stdout=_sanitize_and_tail(stdout),
            stderr=_sanitize_and_tail(stderr),
            datadir=datadir,
            strategy_path=strategy_path,
            userdir=userdir,
            blocked_reason=blocked_reason,
            failed_reason=failed_reason,
        )
        manifest.write()
        return manifest


def _sanitize_and_tail(text: str, max_length: int = 4000) -> str:
    redacted = redact_secret_text(text)
    if len(redacted) <= max_length:
        return redacted
    return redacted[-max_length:]
