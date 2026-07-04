from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from app.adapters.freqtrade.cli_runner import (
    FreqtradeCliRunner,
    FreqtradeCommand,
    FreqtradeCommandResult,
)
from app.adapters.freqtrade.exceptions import FreqtradeCommandValidationError
from app.adapters.freqtrade.config_builder import DryRunEnvPreflight
from app.schemas.dry_run_profile import DryRunProfile


FreqtradeDryRunArtifactStatus = Literal["SUCCESS", "FAILED", "BLOCKED", "SKIPPED"]
MAX_MANIFEST_LOG_CHARS = 4000
SECRET_KEY_NAMES = frozenset(
    {
        "api_key",
        "api_secret",
        "key",
        "password",
        "passphrase",
        "secret",
        "token",
    }
)
SECRET_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?secret|secret|password|passphrase|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


@dataclass(frozen=True)
class FreqtradeDryRunCommandPlan:
    profile_name: str
    strategy_version_id: int
    strategy_name: str
    pair: str
    timeframe: str
    config_path: Path
    userdir: Path
    strategy_path: Optional[Path]
    command_args: list[str]
    timeout_seconds: Optional[int]


@dataclass(frozen=True)
class FreqtradeDryRunExecution:
    plan: FreqtradeDryRunCommandPlan
    command_result: FreqtradeCommandResult


@dataclass(frozen=True)
class FreqtradeDryRunArtifactManifest:
    manifest_version: int
    status: FreqtradeDryRunArtifactStatus
    profile_name: str
    strategy_version_id: int
    strategy_name: str
    pair: str
    timeframe: str
    config_path: Path
    manifest_path: Path
    command_args: list[str]
    return_code: Optional[int]
    stdout: str
    stderr: str
    userdir: Path
    strategy_path: Optional[Path]
    profile_snapshot: dict[str, Any]
    env_preflight: Optional[dict[str, Any]] = None
    status_snapshots: list[dict[str, Any]] | None = None
    blocked_reason: Optional[str] = None
    failed_reason: Optional[str] = None
    skipped_reason: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "manifest_version": self.manifest_version,
            "status": self.status,
            "profile_name": self.profile_name,
            "strategy_version_id": self.strategy_version_id,
            "strategy_name": self.strategy_name,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "config_path": str(self.config_path),
            "manifest_path": str(self.manifest_path),
            "command_args": list(self.command_args),
            "return_code": self.return_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "userdir": str(self.userdir),
            "strategy_path": str(self.strategy_path) if self.strategy_path is not None else None,
            "profile_snapshot": self.profile_snapshot,
            "env_preflight": self.env_preflight,
            "status_snapshots": list(self.status_snapshots or []),
            "blocked_reason": self.blocked_reason,
            "failed_reason": self.failed_reason,
            "skipped_reason": self.skipped_reason,
        }
        return _sanitize_manifest_payload(payload)

    def write(self) -> Path:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self.manifest_path

    @staticmethod
    def read(manifest_path: Path) -> dict[str, Any]:
        return json.loads(manifest_path.read_text(encoding="utf-8"))


class FreqtradeDryRunRunner:
    """Builds and runs local dry-run commands through the safe CLI runner."""

    def __init__(self, cli_runner: FreqtradeCliRunner) -> None:
        self._cli_runner = cli_runner

    def build_dry_run_plan(
        self,
        profile: DryRunProfile,
        config_path: Path,
        timeout_seconds: Optional[int] = None,
    ) -> FreqtradeDryRunCommandPlan:
        command = self._build_trade_command(
            profile=profile,
            config_path=config_path,
            timeout_seconds=timeout_seconds,
        )
        return FreqtradeDryRunCommandPlan(
            profile_name=profile.name,
            strategy_version_id=profile.strategy.version_id,
            strategy_name=profile.strategy.name,
            pair=profile.pair,
            timeframe=profile.timeframe,
            config_path=config_path,
            userdir=Path(profile.command_options.user_data_dir),
            strategy_path=(
                Path(profile.command_options.strategy_path)
                if profile.command_options.strategy_path is not None
                else None
            ),
            command_args=self._cli_runner.build_args(command),
            timeout_seconds=timeout_seconds,
        )

    def run_dry_run_with_output(
        self,
        profile: DryRunProfile,
        config_path: Path,
        timeout_seconds: Optional[int] = None,
    ) -> FreqtradeDryRunExecution:
        command = self._build_trade_command(
            profile=profile,
            config_path=config_path,
            timeout_seconds=timeout_seconds,
        )
        plan = self.build_dry_run_plan(
            profile=profile,
            config_path=config_path,
            timeout_seconds=timeout_seconds,
        )
        return FreqtradeDryRunExecution(
            plan=plan,
            command_result=self._cli_runner.run_unchecked(command),
        )

    def run_dry_run_with_artifact_manifest(
        self,
        profile: DryRunProfile,
        config_path: Path,
        manifest_path: Path,
        timeout_seconds: Optional[int] = None,
        env_preflight: Optional[DryRunEnvPreflight] = None,
        status_snapshots: Optional[list[dict[str, Any]]] = None,
        skipped_reason: Optional[str] = None,
    ) -> FreqtradeDryRunArtifactManifest:
        plan = self.build_dry_run_plan(
            profile=profile,
            config_path=config_path,
            timeout_seconds=timeout_seconds,
        )
        command_args = self._sanitize_command_args(plan.command_args)
        env_preflight_report = env_preflight.to_report() if env_preflight is not None else None

        if skipped_reason is not None:
            return self._write_manifest(
                profile=profile,
                plan=plan,
                status="SKIPPED",
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=None,
                stdout="",
                stderr="",
                env_preflight=env_preflight_report,
                status_snapshots=status_snapshots,
                skipped_reason=skipped_reason,
            )

        blocked_reason = self._local_preflight_blocker(
            config_path=config_path,
            userdir=plan.userdir,
            strategy_path=plan.strategy_path,
            env_preflight=env_preflight,
        )
        if blocked_reason is not None:
            return self._write_manifest(
                profile=profile,
                plan=plan,
                status="BLOCKED",
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=None,
                stdout="",
                stderr="",
                env_preflight=env_preflight_report,
                status_snapshots=status_snapshots,
                blocked_reason=blocked_reason,
            )

        command = self._build_trade_command(
            profile=profile,
            config_path=config_path,
            timeout_seconds=timeout_seconds,
        )
        try:
            command_result = self._cli_runner.run_unchecked(command)
        except FileNotFoundError as exc:
            return self._write_manifest(
                profile=profile,
                plan=plan,
                status="BLOCKED",
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=None,
                stdout="",
                stderr=str(exc),
                env_preflight=env_preflight_report,
                status_snapshots=status_snapshots,
                blocked_reason="freqtrade binary is not available",
            )

        if command_result.return_code != 0:
            return self._write_manifest(
                profile=profile,
                plan=plan,
                status="FAILED",
                manifest_path=manifest_path,
                command_args=command_args,
                return_code=command_result.return_code,
                stdout=command_result.stdout,
                stderr=command_result.stderr,
                env_preflight=env_preflight_report,
                status_snapshots=status_snapshots,
                failed_reason=f"Freqtrade dry-run exited with code {command_result.return_code}",
            )

        return self._write_manifest(
            profile=profile,
            plan=plan,
            status="SUCCESS",
            manifest_path=manifest_path,
            command_args=command_args,
            return_code=command_result.return_code,
            stdout=command_result.stdout,
            stderr=command_result.stderr,
            env_preflight=env_preflight_report,
            status_snapshots=status_snapshots,
        )

    def _build_trade_command(
        self,
        profile: DryRunProfile,
        config_path: Path,
        timeout_seconds: Optional[int],
    ) -> FreqtradeCommand:
        if not profile.safety.dry_run or not profile.safety.allow_dry_run:
            raise FreqtradeCommandValidationError("Dry-run profile must keep dry_run enabled")
        if profile.safety.live_trading or profile.safety.allow_live_trading:
            raise FreqtradeCommandValidationError("Dry-run profile must not allow live trading")
        if profile.safety.allow_real_orders:
            raise FreqtradeCommandValidationError("Dry-run profile must not allow real orders")
        if profile.safety.allow_download:
            raise FreqtradeCommandValidationError("Dry-run profile must not allow downloads")

        options = {
            "--config": config_path,
            "--dry-run": True,
            "--loglevel": profile.command_options.log_level,
            "--strategy": profile.strategy.name,
            "--userdir": Path(profile.command_options.user_data_dir),
        }
        if profile.command_options.strategy_path is not None:
            options["--strategy-path"] = Path(profile.command_options.strategy_path)

        return FreqtradeCommand(
            command="trade",
            options=options,
            timeout_seconds=timeout_seconds,
        )

    def _local_preflight_blocker(
        self,
        config_path: Path,
        userdir: Path,
        strategy_path: Optional[Path],
        env_preflight: Optional[DryRunEnvPreflight],
    ) -> Optional[str]:
        if env_preflight is not None and env_preflight.status == "BLOCKED":
            return env_preflight.blocked_reason or "dry-run ENV preflight is blocked"
        if not config_path.exists():
            return f"dry-run config file does not exist: {config_path}"
        if not userdir.exists():
            return f"dry-run user data directory does not exist: {userdir}"
        if not userdir.is_dir():
            return f"dry-run user data path is not a directory: {userdir}"
        if strategy_path is not None and not strategy_path.exists():
            return f"dry-run strategy path does not exist: {strategy_path}"
        if strategy_path is not None and not strategy_path.is_dir():
            return f"dry-run strategy path is not a directory: {strategy_path}"
        return None

    def _write_manifest(
        self,
        profile: DryRunProfile,
        plan: FreqtradeDryRunCommandPlan,
        status: FreqtradeDryRunArtifactStatus,
        manifest_path: Path,
        command_args: list[str],
        return_code: Optional[int],
        stdout: str,
        stderr: str,
        env_preflight: Optional[dict[str, Any]] = None,
        status_snapshots: Optional[list[dict[str, Any]]] = None,
        blocked_reason: Optional[str] = None,
        failed_reason: Optional[str] = None,
        skipped_reason: Optional[str] = None,
    ) -> FreqtradeDryRunArtifactManifest:
        manifest = FreqtradeDryRunArtifactManifest(
            manifest_version=1,
            status=status,
            profile_name=profile.name,
            strategy_version_id=profile.strategy.version_id,
            strategy_name=profile.strategy.name,
            pair=profile.pair,
            timeframe=profile.timeframe,
            config_path=plan.config_path,
            manifest_path=manifest_path,
            command_args=command_args,
            return_code=return_code,
            stdout=_sanitize_and_tail(stdout),
            stderr=_sanitize_and_tail(stderr),
            userdir=plan.userdir,
            strategy_path=plan.strategy_path,
            profile_snapshot=profile.to_input_snapshot(),
            env_preflight=env_preflight,
            status_snapshots=status_snapshots or [],
            blocked_reason=blocked_reason,
            failed_reason=failed_reason,
            skipped_reason=skipped_reason,
        )
        manifest.write()
        return manifest

    def _sanitize_command_args(self, command_args: list[str]) -> list[str]:
        return [_sanitize_and_tail(arg, max_chars=1000) for arg in command_args]


def _sanitize_manifest_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if _is_secret_key(normalized):
                sanitized[str(key)] = "[REDACTED]"
            else:
                sanitized[str(key)] = _sanitize_manifest_payload(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_manifest_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_manifest_payload(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _sanitize_and_tail(value)
    return value


def _is_secret_key(normalized_key: str) -> bool:
    return (
        normalized_key in SECRET_KEY_NAMES
        or normalized_key.endswith("_secret")
        or "api_key" in normalized_key
        or "api_secret" in normalized_key
    )


def _sanitize_and_tail(text: str, max_chars: int = MAX_MANIFEST_LOG_CHARS) -> str:
    redacted = SECRET_VALUE_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    redacted = BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED]", redacted)
    if len(redacted) <= max_chars:
        return redacted
    return redacted[-max_chars:]
