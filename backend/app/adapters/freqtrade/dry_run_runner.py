from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.adapters.freqtrade.cli_runner import (
    FreqtradeCliRunner,
    FreqtradeCommand,
    FreqtradeCommandResult,
)
from app.adapters.freqtrade.exceptions import FreqtradeCommandValidationError
from app.schemas.dry_run_profile import DryRunProfile


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
