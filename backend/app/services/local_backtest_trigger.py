from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

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
    LocalBacktestTriggerRequest,
    LocalBacktestTriggerResponse,
)
from app.schemas.backtest_profile import BacktestProfileV2


class LocalBacktestTriggerService:
    """Creates durable local backtest records after fail-closed preflight checks."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.strategies = StrategyRepository(db)
        self.backtests = BacktestRepository(db)

    def trigger(self, payload: LocalBacktestTriggerRequest) -> Optional[LocalBacktestTriggerResponse]:
        version = self.strategies.get_version(payload.strategy_version_id)
        if version is None:
            return None

        profile, blocked_reasons = self._validate_profile(payload.profile)
        if profile is not None:
            blocked_reasons.extend(self._validate_strategy_version(version, profile))
            blocked_reasons.extend(self._validate_local_data(profile))
            blocked_reasons.extend(self._validate_adapter_safety(profile))

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
                config_path=None,
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

        exact_timerange = [
            entry for entry in matching_pair_timeframe if entry.timerange == profile.timerange
        ]
        if not exact_timerange:
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

    def _config_snapshot(
        self,
        *,
        payload: LocalBacktestTriggerRequest,
        version: StrategyVersion,
        profile: Optional[BacktestProfileV2],
        blocked_reasons: list[str],
    ) -> dict[str, Any]:
        profile_snapshot = profile.to_snapshot() if profile is not None else payload.profile
        return {
            "trigger": "local_backtest_preflight",
            "phase": "phase8",
            "execution_mode": "preflight_only",
            "strategy_version_id": version.id,
            "strategy_file_path": version.file_path,
            "profile": profile_snapshot,
            "preflight_status": "blocked" if blocked_reasons else "ready",
            "blocked_reasons": list(blocked_reasons),
            "safety": {
                "market_data_download": False,
                "exchange_connection": False,
                "dry_run": False,
                "live_trading": False,
                "real_orders": False,
                "freqtrade_execution": False,
            },
        }


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
