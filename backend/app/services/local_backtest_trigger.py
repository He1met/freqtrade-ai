from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import shutil
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.adapters.freqtrade.config_builder import FreqtradeConfigBuilder
from app.adapters.freqtrade.exceptions import FreqtradeConfigError
from app.adapters.freqtrade.market_data_catalog import MarketDataCatalog
from app.core.config import get_settings
from app.core.paths import resolve_repo_path
from app.models.strategy import StrategyVersion
from app.repositories import BacktestRepository, StrategyRepository
from app.schemas.backtest import (
    BacktestRunCreate,
    BacktestRunRead,
    BacktestRunStatusUpdate,
    BacktestTaskCreate,
    BacktestTaskRead,
    BacktestTaskStatusUpdate,
    LocalBacktestPreflightCheck,
    LocalBacktestTriggerRequest,
    LocalBacktestTriggerResponse,
)
from app.schemas.backtest_profile import BacktestProfileV2


class LocalBacktestTriggerService:
    """Creates durable local backtest records after fail-closed preflight checks."""

    def __init__(
        self,
        db: Session,
        config_builder: Optional[FreqtradeConfigBuilder] = None,
    ) -> None:
        self.db = db
        self.strategies = StrategyRepository(db)
        self.backtests = BacktestRepository(db)
        self.config_builder = config_builder or FreqtradeConfigBuilder()

    def trigger(self, payload: LocalBacktestTriggerRequest) -> Optional[LocalBacktestTriggerResponse]:
        version = self.strategies.get_version(payload.strategy_version_id)
        if version is None:
            return None

        profile, blocked_reasons = self._validate_profile(payload.profile)
        preflight_checks = [
            self._check(
                "profile",
                blocked_reasons,
                ready_summary="Backtest profile is valid and local-only.",
                evidence={"strategy_version_id": version.id},
            )
        ]
        config_path: Optional[Path] = None
        if profile is not None:
            strategy_blockers = self._validate_strategy_version(version, profile)
            preflight_checks.append(
                self._check(
                    "strategy_file",
                    strategy_blockers,
                    ready_summary="Strategy file exists, parses, and matches the selected version.",
                    evidence={
                        "strategy_version_id": version.id,
                        "strategy_file_path": version.file_path,
                        "strategy_name": profile.strategy.name,
                    },
                )
            )
            local_data_blockers = self._validate_local_data(profile)
            preflight_checks.append(
                self._check(
                    "local_market_data",
                    local_data_blockers,
                    ready_summary="Matching local market data is available.",
                    evidence={
                        "exchange": profile.data_source.exchange,
                        "datadir": profile.data_source.datadir,
                        "pair": profile.pair,
                        "timeframe": profile.timeframe,
                        "timerange": profile.timerange,
                    },
                )
            )
            preflight_checks.append(self._freqtrade_binary_check())
            config_path, config_blockers = self._build_backtest_config(profile)
            preflight_checks.append(
                self._check(
                    "backtest_config",
                    config_blockers,
                    ready_summary="Backtest config was generated without secret values.",
                    evidence={"config_path": str(config_path) if config_path is not None else None},
                )
            )
            permission_blockers = self._validate_artifact_permissions()
            preflight_checks.append(
                self._check(
                    "artifact_permissions",
                    permission_blockers,
                    ready_summary="Backtest artifact directories are writable.",
                    evidence={
                        "backtest_result_dir": str(resolve_repo_path(get_settings().backtest_result_dir)),
                    },
                )
            )
            adapter_blockers = self._validate_adapter_safety(profile)
            preflight_checks.append(
                self._check(
                    "adapter_safety",
                    adapter_blockers,
                    ready_summary="Backtest preflight will not download data, connect to an exchange, or trade.",
                    evidence={
                        "market_data_download": False,
                        "exchange_connection": False,
                        "dry_run": False,
                        "live_trading": False,
                        "real_orders": False,
                        "freqtrade_execution": False,
                    },
                )
            )
            blocked_reasons = [
                check.blocked_reason
                for check in preflight_checks
                if check.status == "BLOCKED" and check.blocked_reason is not None
            ]

        pair = (
            profile.pair
            if profile is not None
            else _safe_task_value(payload.profile.get("pair"), "UNSPECIFIED", max_length=80)
        )
        timeframe = (
            profile.timeframe
            if profile is not None
            else _safe_task_value(payload.profile.get("timeframe"), "unspecified", max_length=32)
        )
        config_snapshot = self._config_snapshot(
            payload=payload,
            version=version,
            profile=profile,
            blocked_reasons=blocked_reasons,
            preflight_checks=preflight_checks,
            config_path=config_path,
        )
        run = self.backtests.create_run(
            BacktestRunCreate(
                strategy_version_id=version.id,
                profile_name=_profile_name(payload.profile, profile),
                config_snapshot=config_snapshot,
            )
        )
        if run is None:
            return None

        task = self.backtests.create_task(
            run.id,
            BacktestTaskCreate(
                pair=pair,
                timeframe=timeframe,
                config_path=str(config_path) if config_path is not None else None,
            ),
        )
        if task is None:
            return None

        if blocked_reasons:
            blocked_message = "BLOCKED: " + "; ".join(blocked_reasons)
            run = self.backtests.update_run_status(run.id, BacktestRunStatusUpdate(status="blocked"))
            task = self.backtests.update_task_status(
                task.id,
                BacktestTaskStatusUpdate(status="blocked", error_message=blocked_message),
            )

        if run is None or task is None:
            return None

        return LocalBacktestTriggerResponse(
            run=BacktestRunRead.model_validate(run),
            tasks=[BacktestTaskRead.model_validate(task)],
            preflight_status="blocked" if blocked_reasons else "ready",
            blocked_reasons=blocked_reasons,
            preflight_checks=preflight_checks,
        )

    def _validate_profile(self, raw_profile: dict[str, Any]) -> tuple[Optional[BacktestProfileV2], list[str]]:
        try:
            return BacktestProfileV2.model_validate(raw_profile), []
        except ValidationError as exc:
            reasons = [
                f"profile invalid at {'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            ]
            return None, reasons or ["profile invalid"]

    def _validate_strategy_version(
        self,
        version: StrategyVersion,
        profile: BacktestProfileV2,
    ) -> list[str]:
        blockers: list[str] = []
        if version.validation_status != "passed":
            blockers.append(
                f"strategy version validation_status is {version.validation_status}, expected passed"
            )

        strategy_path = resolve_repo_path(version.file_path)
        if profile.strategy.path is not None:
            requested_path = resolve_repo_path(profile.strategy.path)
            if requested_path != strategy_path:
                blockers.append("profile strategy.path does not match the selected strategy version file_path")

        blockers.extend(self._validate_strategy_file(strategy_path, version, profile.strategy.name))
        return blockers

    def _validate_strategy_file(
        self,
        strategy_path: Path,
        version: StrategyVersion,
        class_name: str,
    ) -> list[str]:
        blockers: list[str] = []
        if strategy_path.suffix != ".py":
            blockers.append("strategy file path must end with .py")
        if not strategy_path.exists():
            blockers.append(f"strategy file does not exist: {strategy_path}")
            return blockers
        if not strategy_path.is_file():
            blockers.append(f"strategy file path is not a file: {strategy_path}")
            return blockers

        try:
            code = strategy_path.read_text(encoding="utf-8")
        except OSError as exc:
            blockers.append(f"strategy file cannot be read: {exc.__class__.__name__}")
            return blockers

        try:
            tree = ast.parse(code, filename=str(strategy_path))
        except SyntaxError as exc:
            blockers.append(f"strategy file cannot be parsed: {exc.msg}")
            return blockers

        if not any(isinstance(node, ast.ClassDef) and node.name == class_name for node in tree.body):
            blockers.append(f"strategy file does not define class {class_name}")

        if version.code_hash:
            file_hash = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
            if file_hash != version.code_hash:
                blockers.append("strategy file checksum does not match strategy version code_hash")
        return blockers

    def _validate_local_data(self, profile: BacktestProfileV2) -> list[str]:
        report = MarketDataCatalog(market_data_dir=Path(profile.data_source.datadir)).inspect(
            exchange=profile.data_source.exchange
        )
        if report.status != "available":
            return list(report.blockers) or [f"local market data is {report.status}"]

        matching_pair_timeframe = [
            entry
            for entry in report.available_entries
            if entry.exchange == profile.data_source.exchange
            and entry.pair == profile.pair
            and entry.timeframe == profile.timeframe
        ]
        if not matching_pair_timeframe:
            return [
                "no local market data file matches "
                f"{profile.data_source.exchange} {profile.pair} {profile.timeframe}"
            ]

        usable_timerange = [
            entry
            for entry in matching_pair_timeframe
            if entry.timerange is None or entry.timerange == profile.timerange
        ]
        if not usable_timerange:
            return [
                "no local market data file matches "
                f"{profile.data_source.exchange} {profile.pair} {profile.timeframe} {profile.timerange}"
            ]
        return []

    def _validate_adapter_safety(self, profile: BacktestProfileV2) -> list[str]:
        blockers: list[str] = []
        safety = profile.safety
        if safety.allow_download:
            blockers.append("profile safety forbids market-data download")
        if safety.allow_exchange_connection:
            blockers.append("profile safety forbids exchange connection")
        if safety.allow_dry_run:
            blockers.append("profile safety forbids dry-run")
        if safety.allow_live_trading:
            blockers.append("profile safety forbids live trading")
        if safety.allow_hyperopt:
            blockers.append("profile safety forbids hyperopt")

        settings = get_settings()
        if settings.allow_live_trading:
            blockers.append("backend live trading setting must remain disabled")
        if settings.allow_dry_run_trading:
            blockers.append("backend dry-run trading setting must remain disabled")
        return blockers

    def _freqtrade_binary_check(self) -> LocalBacktestPreflightCheck:
        binary = os.environ.get("FREQTRADE_BINARY", "freqtrade").strip()
        evidence: dict[str, Any] = {
            "binary": binary or "freqtrade",
            "source": "FREQTRADE_BINARY" if os.environ.get("FREQTRADE_BINARY") else "default",
        }
        blockers: list[str] = []
        if not binary:
            blockers.append("freqtrade binary is not configured")
            return self._check(
                "freqtrade_binary",
                blockers,
                ready_summary="Freqtrade binary is available.",
                evidence=evidence,
            )

        binary_path = self._resolve_binary_path(binary)
        if binary_path is None:
            blockers.append(f"freqtrade binary is not available: {binary}")
        else:
            evidence["resolved_path"] = str(binary_path)
            if not binary_path.exists():
                blockers.append(f"freqtrade binary does not exist: {binary_path}")
            elif not binary_path.is_file():
                blockers.append(f"freqtrade binary path is not a file: {binary_path}")
            elif not os.access(binary_path, os.X_OK):
                blockers.append(f"freqtrade binary is not executable: {binary_path}")

        return self._check(
            "freqtrade_binary",
            blockers,
            ready_summary="Freqtrade binary is available.",
            evidence=evidence,
        )

    def _resolve_binary_path(self, binary: str) -> Optional[Path]:
        if Path(binary).is_absolute() or "/" in binary or "\\" in binary:
            return resolve_repo_path(binary)
        resolved = shutil.which(binary)
        return Path(resolved) if resolved else None

    def _build_backtest_config(self, profile: BacktestProfileV2) -> tuple[Optional[Path], list[str]]:
        try:
            return self.config_builder.build_backtest_config(profile), []
        except (FreqtradeConfigError, OSError) as exc:
            return None, [f"backtest config cannot be generated: {exc}"]

    def _validate_artifact_permissions(self) -> list[str]:
        blockers: list[str] = []
        result_dir = resolve_repo_path(get_settings().backtest_result_dir)
        try:
            result_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return [f"backtest result directory cannot be created: {exc.__class__.__name__}"]

        if not result_dir.is_dir():
            blockers.append(f"backtest result path is not a directory: {result_dir}")
        elif not os.access(result_dir, os.W_OK | os.X_OK):
            blockers.append(f"backtest result directory is not writable: {result_dir}")
        return blockers

    def _config_snapshot(
        self,
        *,
        payload: LocalBacktestTriggerRequest,
        version: StrategyVersion,
        profile: Optional[BacktestProfileV2],
        blocked_reasons: list[str],
        preflight_checks: list[LocalBacktestPreflightCheck],
        config_path: Optional[Path],
    ) -> dict[str, Any]:
        profile_snapshot = profile.to_snapshot() if profile is not None else payload.profile
        return {
            "trigger": "local_backtest_preflight",
            "phase": "phase8",
            "execution_mode": "preflight_only",
            "strategy_version_id": version.id,
            "strategy_file_path": version.file_path,
            "config_path": str(config_path) if config_path is not None else None,
            "profile": profile_snapshot,
            "preflight_status": "blocked" if blocked_reasons else "ready",
            "blocked_reasons": list(blocked_reasons),
            "preflight_checks": [
                check.model_dump(mode="json")
                for check in preflight_checks
            ],
            "safety": {
                "market_data_download": False,
                "exchange_connection": False,
                "dry_run": False,
                "live_trading": False,
                "real_orders": False,
                "freqtrade_execution": False,
            },
        }

    def _check(
        self,
        name: str,
        blockers: list[str],
        *,
        ready_summary: str,
        evidence: dict[str, Any],
    ) -> LocalBacktestPreflightCheck:
        if blockers:
            return LocalBacktestPreflightCheck(
                name=name,
                status="BLOCKED",
                summary=f"{name} is blocked.",
                blocked_reason="; ".join(blockers),
                evidence=evidence,
            )
        return LocalBacktestPreflightCheck(
            name=name,
            status="READY",
            summary=ready_summary,
            evidence=evidence,
        )


def _safe_task_value(value: Any, fallback: str, *, max_length: int) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:max_length]
    return fallback


def _profile_name(raw_profile: dict[str, Any], profile: Optional[BacktestProfileV2]) -> Optional[str]:
    if profile is not None:
        return profile.profile_name
    value = raw_profile.get("profile_name")
    if isinstance(value, str) and value.strip():
        return value.strip()[:120]
    return None
