from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Optional, Protocol, Sequence

from app.adapters.freqtrade.backtest_runner import (
    FreqtradeBacktestArtifactManifest,
    FreqtradeBacktestArtifactStatus,
)
from app.adapters.freqtrade.config_builder import FreqtradeConfigBuilder
from app.adapters.freqtrade.market_data_catalog import (
    MarketDataCatalog,
    MarketDataCatalogEntry,
    MarketDataCatalogReport,
)
from app.core.paths import resolve_repo_path
from app.schemas.backtest_profile import BacktestProfileV2


BacktestMatrixStatus = Literal["SUCCESS", "FAILED", "BLOCKED"]
MAX_MATRIX_TASKS = 8


class BacktestMatrixRunner(Protocol):
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
        ...


@dataclass(frozen=True)
class BacktestMatrixTaskResult:
    profile_name: str
    pair: str
    timeframe: str
    status: FreqtradeBacktestArtifactStatus
    config_path: Path
    result_path: Path
    manifest_path: Path
    data_path: Optional[Path] = None
    blocked_reason: Optional[str] = None
    failed_reason: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_name": self.profile_name,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "status": self.status,
            "config_path": str(self.config_path),
            "result_path": str(self.result_path),
            "manifest_path": str(self.manifest_path),
            "data_path": str(self.data_path) if self.data_path is not None else None,
            "blocked_reason": self.blocked_reason,
            "failed_reason": self.failed_reason,
        }


@dataclass(frozen=True)
class BacktestMatrixSummary:
    status: BacktestMatrixStatus
    total_tasks: int
    succeeded: int
    failed: int
    blocked: int
    summary_path: Path
    tasks: list[BacktestMatrixTaskResult]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "total_tasks": self.total_tasks,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "blocked": self.blocked,
            "summary_path": str(self.summary_path),
            "tasks": [task.to_dict() for task in self.tasks],
        }

    def write(self) -> Path:
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self.summary_path


class BacktestMatrixExecutionService:
    """Executes a small local-only backtest matrix and records fail-closed output."""

    def __init__(
        self,
        runner: BacktestMatrixRunner,
        config_builder: Optional[FreqtradeConfigBuilder] = None,
        market_data_catalog: Optional[MarketDataCatalog] = None,
    ) -> None:
        self._runner = runner
        self._config_builder = config_builder or FreqtradeConfigBuilder()
        self._market_data_catalog = market_data_catalog or MarketDataCatalog()

    def execute_matrix(
        self,
        profiles: Sequence[BacktestProfileV2 | dict[str, object]],
        output_dir: Path,
        config_dir: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
        max_tasks: int = MAX_MATRIX_TASKS,
    ) -> BacktestMatrixSummary:
        validated_profiles = self._validate_profiles(profiles, max_tasks=max_tasks)
        resolved_output_dir = resolve_repo_path(output_dir)
        resolved_config_dir = (
            resolve_repo_path(config_dir)
            if config_dir is not None
            else resolved_output_dir / "configs"
        )
        catalog_report = self._market_data_catalog.inspect()

        tasks: list[BacktestMatrixTaskResult] = []
        for index, profile in enumerate(validated_profiles, start=1):
            config_path = self._config_builder.build_backtest_config(
                profile,
                output_dir=resolved_config_dir,
            )
            task_slug = self._task_slug(profile)
            result_path = resolved_output_dir / f"{index:02d}-{task_slug}-result.json"
            manifest_path = resolved_output_dir / f"{index:02d}-{task_slug}-manifest.json"
            matching_data = self._matching_data_entry(catalog_report, profile)
            if matching_data is None:
                tasks.append(
                    self._write_blocked_task(
                        profile=profile,
                        config_path=config_path,
                        result_path=result_path,
                        manifest_path=manifest_path,
                        catalog_report=catalog_report,
                    )
                )
                continue

            manifest = self._runner.run_backtest_with_artifact_manifest(
                config_path,
                profile.strategy.name,
                result_path=result_path,
                manifest_path=manifest_path,
                timeout_seconds=timeout_seconds,
                datadir=matching_data.path.parent,
                strategy_path=Path(profile.strategy.path) if profile.strategy.path else None,
            )
            tasks.append(
                BacktestMatrixTaskResult(
                    profile_name=profile.profile_name,
                    pair=profile.pair,
                    timeframe=profile.timeframe,
                    status=manifest.status,
                    config_path=config_path,
                    result_path=result_path,
                    manifest_path=manifest_path,
                    data_path=matching_data.path,
                    blocked_reason=manifest.blocked_reason,
                    failed_reason=manifest.failed_reason,
                )
            )

        summary = self._build_summary(
            tasks,
            summary_path=resolved_output_dir / "backtest-matrix-summary.json",
        )
        summary.write()
        return summary

    def _validate_profiles(
        self,
        profiles: Sequence[BacktestProfileV2 | dict[str, object]],
        max_tasks: int,
    ) -> list[BacktestProfileV2]:
        if max_tasks < 1:
            raise ValueError("max_tasks must be at least 1")
        if not profiles:
            raise ValueError("backtest matrix requires at least one profile")
        if len(profiles) > max_tasks:
            raise ValueError(f"backtest matrix is limited to {max_tasks} tasks")
        return [
            profile
            if isinstance(profile, BacktestProfileV2)
            else BacktestProfileV2.model_validate(profile)
            for profile in profiles
        ]

    def _matching_data_entry(
        self,
        catalog_report: MarketDataCatalogReport,
        profile: BacktestProfileV2,
    ) -> Optional[MarketDataCatalogEntry]:
        for entry in catalog_report.available_entries:
            if (
                entry.exchange == profile.data_source.exchange
                and entry.pair == profile.pair
                and entry.timeframe == profile.timeframe
            ):
                return entry
        return None

    def _write_blocked_task(
        self,
        profile: BacktestProfileV2,
        config_path: Path,
        result_path: Path,
        manifest_path: Path,
        catalog_report: MarketDataCatalogReport,
    ) -> BacktestMatrixTaskResult:
        reason = self._blocked_reason(profile, catalog_report.blockers)
        manifest = FreqtradeBacktestArtifactManifest(
            manifest_version=1,
            status="BLOCKED",
            config_path=config_path,
            strategy_name=profile.strategy.name,
            result_path=result_path,
            manifest_path=manifest_path,
            command_args=[],
            return_code=None,
            stdout="",
            stderr="",
            datadir=catalog_report.market_data_dir / profile.data_source.exchange,
            strategy_path=Path(profile.strategy.path) if profile.strategy.path else None,
            userdir=None,
            blocked_reason=reason,
        )
        manifest.write()
        return BacktestMatrixTaskResult(
            profile_name=profile.profile_name,
            pair=profile.pair,
            timeframe=profile.timeframe,
            status="BLOCKED",
            config_path=config_path,
            result_path=result_path,
            manifest_path=manifest_path,
            blocked_reason=reason,
        )

    def _blocked_reason(self, profile: BacktestProfileV2, blockers: Iterable[str]) -> str:
        blocker_text = "; ".join(blockers)
        base = (
            "no available local market data for "
            f"{profile.data_source.exchange} {profile.pair} {profile.timeframe}"
        )
        if blocker_text:
            return f"{base}: {blocker_text}"
        return base

    def _build_summary(
        self,
        tasks: list[BacktestMatrixTaskResult],
        summary_path: Path,
    ) -> BacktestMatrixSummary:
        succeeded = sum(1 for task in tasks if task.status == "SUCCESS")
        failed = sum(1 for task in tasks if task.status == "FAILED")
        blocked = sum(1 for task in tasks if task.status == "BLOCKED")
        if failed:
            status: BacktestMatrixStatus = "FAILED"
        elif blocked:
            status = "BLOCKED"
        else:
            status = "SUCCESS"
        return BacktestMatrixSummary(
            status=status,
            total_tasks=len(tasks),
            succeeded=succeeded,
            failed=failed,
            blocked=blocked,
            summary_path=summary_path,
            tasks=tasks,
        )

    def _task_slug(self, profile: BacktestProfileV2) -> str:
        raw = f"{profile.profile_name}-{profile.pair}-{profile.timeframe}"
        characters = [character.lower() if character.isalnum() else "-" for character in raw]
        return "-".join("".join(characters).split("-")).strip("-") or "backtest-task"
