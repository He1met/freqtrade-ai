from __future__ import annotations

import ast
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.adapters.freqtrade.config_builder import FreqtradeConfigBuilder
from app.adapters.freqtrade.exceptions import FreqtradeConfigError
from app.adapters.freqtrade.market_data_catalog import MarketDataCatalog
from app.core.config import Settings, get_settings
from app.core.paths import resolve_repo_path
from app.models.strategy import StrategyVersion
from app.repositories import StrategyRepository
from app.schemas.dry_run_profile import DryRunProfile
from app.schemas.dry_run_readiness import (
    DryRunReadinessCheck,
    DryRunReadinessReport,
    DryRunReadinessRequest,
    DryRunReadinessStatus,
)


class DryRunReadinessService:
    """Readiness-only dry-run preflight. It never starts a process."""

    def __init__(
        self,
        db: Session,
        *,
        environ: Optional[Mapping[str, str]] = None,
        settings: Optional[Settings] = None,
        config_builder: Optional[FreqtradeConfigBuilder] = None,
    ) -> None:
        self.db = db
        self.strategies = StrategyRepository(db)
        self.environ = environ
        self.settings = settings or get_settings()
        self.config_builder = config_builder or FreqtradeConfigBuilder()

    def evaluate(self, payload: DryRunReadinessRequest) -> Optional[DryRunReadinessReport]:
        version = self.strategies.get_version(payload.strategy_version_id)
        if version is None:
            return None

        generated_at = datetime.now(timezone.utc)
        checks: list[DryRunReadinessCheck] = []
        safety_check = self._safety_check(payload)
        checks.append(safety_check)

        profile = self._build_profile(payload, version)
        profile_check = self._profile_check(profile)
        checks.append(profile_check)

        if profile is not None:
            checks.append(self._strategy_file_check(version, profile))
            checks.append(self._local_data_check(payload, profile))
            env_preflight = self.config_builder.check_dry_run_env(
                environ=self.environ,
                required_env_vars=payload.required_env_vars,
                optional_env_vars=payload.optional_env_vars,
            )
            checks.append(self._env_check(env_preflight))
            config_preview, config_check = self._config_check(profile)
            checks.append(config_check)
        else:
            env_preflight = self.config_builder.check_dry_run_env(
                environ=self.environ,
                required_env_vars=payload.required_env_vars,
                optional_env_vars=payload.optional_env_vars,
            )
            checks.append(self._env_check(env_preflight))
            config_preview = {}

        blocked_reasons = [
            check.blocked_reason
            for check in checks
            if check.status == "BLOCKED" and check.blocked_reason is not None
        ]
        status: DryRunReadinessStatus = "BLOCKED" if blocked_reasons else "READY"
        return DryRunReadinessReport(
            status=status,
            generated_at=generated_at,
            strategy_version_id=version.id,
            profile_name=self._profile_name(payload, version),
            blocked_reasons=blocked_reasons,
            checks=checks,
            env_preflight=env_preflight.to_report(),
            config_preview=config_preview,
            safety={
                "readiness_only": True,
                "starts_freqtrade": False,
                "writes_config": False,
                "exchange_connection": False,
                "live_trading": False,
                "real_orders": False,
                "stores_sensitive_values": False,
            },
        )

    def _build_profile(
        self,
        payload: DryRunReadinessRequest,
        version: StrategyVersion,
    ) -> Optional[DryRunProfile]:
        if not payload.dry_run or payload.allow_exchange_connection or payload.allow_live_trading or payload.allow_real_orders:
            return None

        strategy_name = self._strategy_name(payload, version)
        command_options = {
            "user_data_dir": "user_data",
            "log_level": "INFO",
        }
        version_file_path = Path(version.file_path)
        if not version_file_path.is_absolute():
            command_options["strategy_path"] = str(version_file_path.parent)

        profile_payload = {
            "name": self._profile_name(payload, version),
            "description": "Phase 8 dry-run readiness preflight only.",
            "strategy": {
                "version_id": version.id,
                "name": strategy_name,
                "file_path": version.file_path,
            },
            "pair": payload.pair,
            "timeframe": payload.timeframe,
            "stake": {
                "currency": payload.stake_currency,
                "amount": payload.stake_amount,
                "max_open_trades": payload.max_open_trades,
            },
            "exchange": {"name": payload.exchange, "trading_mode": payload.trading_mode},
            "command_options": command_options,
            "locked_variables": {
                "profile_name": self._profile_name(payload, version),
                "strategy_version_id": version.id,
                "strategy": strategy_name,
                "pair": payload.pair,
                "timeframe": payload.timeframe,
                "exchange": payload.exchange,
                "stake_currency": payload.stake_currency,
                "stake_amount": payload.stake_amount,
                "max_open_trades": payload.max_open_trades,
                "dry_run": True,
                "freq_ui_enabled": False,
            },
            "tags": ["phase-8", "readiness-only"],
        }
        try:
            return DryRunProfile.model_validate(profile_payload)
        except ValidationError:
            return None

    def _safety_check(self, payload: DryRunReadinessRequest) -> DryRunReadinessCheck:
        blockers: list[str] = []
        if not payload.dry_run:
            blockers.append("dry_run must be true")
        if payload.allow_exchange_connection:
            blockers.append("readiness cannot allow exchange connection")
        if payload.allow_live_trading:
            blockers.append("readiness cannot allow live trading")
        if payload.allow_real_orders:
            blockers.append("readiness cannot allow real orders")
        if self.settings.allow_live_trading:
            blockers.append("backend live trading setting must remain disabled")
        if self.settings.allow_dry_run_trading:
            blockers.append("backend dry-run trading setting must remain disabled in readiness-only mode")
        return self._check(
            "safety_boundary",
            blockers,
            ready_summary="dry_run=true and no runtime execution capability is enabled",
            evidence={
                "dry_run": payload.dry_run,
                "allow_exchange_connection": payload.allow_exchange_connection,
                "allow_live_trading": payload.allow_live_trading,
                "allow_real_orders": payload.allow_real_orders,
            },
        )

    def _profile_check(self, profile: Optional[DryRunProfile]) -> DryRunReadinessCheck:
        if profile is None:
            return DryRunReadinessCheck(
                name="dry_run_profile",
                status="BLOCKED",
                summary="Dry-run profile could not be constructed safely.",
                blocked_reason="dry-run profile is invalid or unsafe",
                evidence={},
            )
        return DryRunReadinessCheck(
            name="dry_run_profile",
            status="READY",
            summary="Dry-run profile is valid and locked.",
            evidence={
                "profile_hash": profile.profile_hash(),
                "dry_run": profile.safety.dry_run,
                "strategy_version_id": profile.strategy.version_id,
            },
        )

    def _strategy_file_check(
        self,
        version: StrategyVersion,
        profile: DryRunProfile,
    ) -> DryRunReadinessCheck:
        blockers: list[str] = []
        strategy_path = resolve_repo_path(version.file_path)
        if version.validation_status != "passed":
            blockers.append(f"strategy version validation_status is {version.validation_status}, expected passed")
        if strategy_path.suffix != ".py":
            blockers.append("strategy file path must end with .py")
        if not strategy_path.exists():
            blockers.append(f"strategy file does not exist: {strategy_path}")
            return self._check("strategy_file", blockers, ready_summary="strategy file is present", evidence={"path": str(strategy_path)})
        if not strategy_path.is_file():
            blockers.append(f"strategy file path is not a file: {strategy_path}")
            return self._check("strategy_file", blockers, ready_summary="strategy file is present", evidence={"path": str(strategy_path)})

        try:
            code = strategy_path.read_text(encoding="utf-8")
            tree = ast.parse(code, filename=str(strategy_path))
        except (OSError, SyntaxError) as exc:
            blockers.append(f"strategy file cannot be read or parsed: {exc.__class__.__name__}")
            return self._check("strategy_file", blockers, ready_summary="strategy file is present", evidence={"path": str(strategy_path)})

        if not any(isinstance(node, ast.ClassDef) and node.name == profile.strategy.name for node in tree.body):
            blockers.append(f"strategy file does not define class {profile.strategy.name}")
        if version.code_hash:
            file_hash = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
            if file_hash != version.code_hash:
                blockers.append("strategy file checksum does not match strategy version code_hash")
        return self._check(
            "strategy_file",
            blockers,
            ready_summary="strategy file exists, parses, and matches the selected version",
            evidence={"path": str(strategy_path), "class_name": profile.strategy.name},
        )

    def _local_data_check(
        self,
        payload: DryRunReadinessRequest,
        profile: DryRunProfile,
    ) -> DryRunReadinessCheck:
        market_data_dir = Path(payload.market_data_dir) if payload.market_data_dir else self.settings.market_data_dir
        report = MarketDataCatalog(market_data_dir=market_data_dir).inspect(exchange=payload.exchange)
        blockers = list(report.blockers)
        matching = [
            entry
            for entry in report.available_entries
            if entry.exchange == payload.exchange
            and entry.pair == profile.pair
            and entry.timeframe == profile.timeframe
        ]
        if report.status != "available" and not blockers:
            blockers.append(f"local market data is {report.status}")
        if not matching:
            blockers.append(f"no local market data file matches {payload.exchange} {profile.pair} {profile.timeframe}")
        return self._check(
            "local_market_data",
            blockers,
            ready_summary="matching local market data is available",
            evidence={
                "market_data_dir": str(market_data_dir),
                "exchange": payload.exchange,
                "pair": profile.pair,
                "timeframe": profile.timeframe,
                "matches": len(matching),
            },
        )

    def _env_check(self, env_preflight: object) -> DryRunReadinessCheck:
        blocked_reason = getattr(env_preflight, "blocked_reason", None)
        return DryRunReadinessCheck(
            name="env_only_credentials",
            status=getattr(env_preflight, "status"),
            summary="Required dry-run ENV names are present." if getattr(env_preflight, "status") == "READY" else "Required dry-run ENV names are missing.",
            blocked_reason=blocked_reason,
            evidence=getattr(env_preflight, "to_report")(),
        )

    def _config_check(self, profile: DryRunProfile) -> tuple[dict[str, object], DryRunReadinessCheck]:
        try:
            config = self.config_builder.build_dry_run_config_dict(profile)
        except FreqtradeConfigError as exc:
            return {}, DryRunReadinessCheck(
                name="dry_run_config_preview",
                status="BLOCKED",
                summary="Dry-run config preview is unsafe.",
                blocked_reason=str(exc),
                evidence={},
            )
        blockers: list[str] = []
        if config.get("dry_run") is not True:
            blockers.append("dry-run config preview must keep dry_run=true")
        if config.get("initial_state") != "stopped":
            blockers.append("dry-run config preview must keep initial_state=stopped")
        preview = {
            "dry_run": config.get("dry_run"),
            "initial_state": config.get("initial_state"),
            "exchange": config.get("exchange", {}).get("name") if isinstance(config.get("exchange"), dict) else None,
            "strategy": config.get("strategy"),
            "pair_whitelist": config.get("exchange", {}).get("pair_whitelist") if isinstance(config.get("exchange"), dict) else [],
        }
        return preview, self._check(
            "dry_run_config_preview",
            blockers,
            ready_summary="config preview is dry-run-only and contains no secret values",
            evidence=preview,
        )

    def _check(
        self,
        name: str,
        blockers: list[str],
        *,
        ready_summary: str,
        evidence: dict[str, object],
    ) -> DryRunReadinessCheck:
        if blockers:
            return DryRunReadinessCheck(
                name=name,
                status="BLOCKED",
                summary=f"{name} is blocked.",
                blocked_reason="; ".join(blockers),
                evidence=evidence,
            )
        return DryRunReadinessCheck(
            name=name,
            status="READY",
            summary=ready_summary,
            evidence=evidence,
        )

    def _strategy_name(self, payload: DryRunReadinessRequest, version: StrategyVersion) -> str:
        if payload.strategy_name:
            return payload.strategy_name
        blueprint = version.blueprint or {}
        class_name = blueprint.get("class_name") if isinstance(blueprint, dict) else None
        if isinstance(class_name, str) and class_name:
            return class_name
        return Path(version.file_path).stem

    def _profile_name(self, payload: DryRunReadinessRequest, version: StrategyVersion) -> str:
        return f"phase8-readiness-v{version.id}-{payload.exchange}-{payload.timeframe}"
