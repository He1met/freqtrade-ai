from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner
from app.adapters.freqtrade.config_builder import DryRunEnvPreflight, FreqtradeConfigBuilder
from app.adapters.freqtrade.dry_run_runner import (
    FreqtradeDryRunArtifactManifest,
    FreqtradeDryRunRunner,
)
from app.core.config import Settings, get_settings
from app.core.paths import resolve_repo_path
from app.repositories import StrategyRepository
from app.schemas.dry_run_control import (
    DryRunControlReport,
    DryRunControlStartRequest,
    DryRunControlStatus,
    DryRunControlStopRequest,
)
from app.schemas.dry_run_profile import DryRunProfile
from app.schemas.dry_run_status import DryRunStatusSnapshot
from app.services.dry_run_readiness import DryRunReadinessService
from app.services.dry_run_status import DryRunStatusSnapshotService
from app.services.freq_ui_link import FreqUILinkMetadataService


class DryRunControlService:
    """Controlled local dry-run boundary.

    The default backend setting keeps the real process path disabled. This
    service still writes redacted local evidence for BLOCKED/FAILED states so
    operators can inspect exactly why a start did not occur.
    """

    def __init__(
        self,
        db: Optional[Session] = None,
        *,
        environ: Optional[Mapping[str, str]] = None,
        settings: Optional[Settings] = None,
        config_builder: Optional[FreqtradeConfigBuilder] = None,
        runner: Optional[FreqtradeDryRunRunner] = None,
        report_dir: Optional[Path] = None,
    ) -> None:
        self.db = db
        self.environ = environ
        self.settings = settings or get_settings()
        self.config_builder = config_builder or FreqtradeConfigBuilder()
        self.runner = runner or FreqtradeDryRunRunner(FreqtradeCliRunner())
        self.report_dir = resolve_repo_path(report_dir or Path("reports/runtime"))
        self.manifest_path = self.report_dir / "dry-run-manifest.json"
        self.status_snapshot_path = self.report_dir / "dry-run-status.json"

    def start(self, payload: DryRunControlStartRequest) -> Optional[DryRunControlReport]:
        if self.db is None:
            raise ValueError("database session is required for controlled dry-run start")

        repository = StrategyRepository(self.db)
        version = repository.get_version(payload.strategy_version_id)
        if version is None:
            return None

        readiness_service = DryRunReadinessService(
            self.db,
            environ=self.environ,
            settings=self.settings,
            config_builder=self.config_builder,
        )
        readiness = readiness_service.evaluate(payload)
        if readiness is None:
            return None

        generated_at = self._now()
        profile = readiness_service.build_profile(payload, version)
        if profile is None:
            snapshot = self._blocked_snapshot(
                "dry-run profile is invalid or unsafe",
                generated_at=generated_at,
            )
            self._write_status_snapshot(snapshot)
            return self._report(
                status="BLOCKED",
                generated_at=generated_at,
                readiness=readiness,
                status_snapshot=snapshot,
                blocked_reasons=["dry-run profile is invalid or unsafe"],
            )

        config_result = self.config_builder.build_dry_run_config(
            profile,
            environ=self.environ,
            required_env_vars=payload.required_env_vars,
            optional_env_vars=payload.optional_env_vars,
        )
        blockers = self._start_blockers(payload, readiness.blocked_reasons, config_result.env_preflight)

        if blockers:
            blocked_preflight = self._blocked_preflight(config_result.env_preflight, blockers)
            manifest = self.runner.run_dry_run_with_artifact_manifest(
                profile=profile,
                config_path=config_result.config_path,
                manifest_path=self.manifest_path,
                timeout_seconds=payload.timeout_seconds,
                env_preflight=blocked_preflight,
                status_snapshots=[
                    self._snapshot_payload(
                        profile,
                        status="BLOCKED",
                        generated_at=generated_at,
                        blocked_reason="; ".join(blockers),
                    )
                ],
            )
        else:
            manifest = self.runner.run_dry_run_with_artifact_manifest(
                profile=profile,
                config_path=config_result.config_path,
                manifest_path=self.manifest_path,
                timeout_seconds=payload.timeout_seconds,
                env_preflight=config_result.env_preflight,
                status_snapshots=[
                    self._snapshot_payload(
                        profile,
                        status="RUNNING",
                        generated_at=generated_at,
                    )
                ],
            )

        snapshot = self._snapshot_from_manifest(manifest, profile, generated_at=generated_at)
        self._write_status_snapshot(snapshot)
        return self._report(
            status=self._control_status(manifest.status),
            generated_at=generated_at,
            readiness=readiness,
            status_snapshot=snapshot,
            manifest=manifest,
            config_path=config_result.config_path,
            blocked_reasons=blockers or ([manifest.blocked_reason] if manifest.blocked_reason else []),
            failed_reason=manifest.failed_reason,
            skipped_reason=manifest.skipped_reason,
        )

    def stop(self, payload: DryRunControlStopRequest) -> DryRunControlReport:
        generated_at = self._now()
        manifest = self._read_manifest()
        snapshot_payload: dict[str, Any] = {
            "status": "STOPPED",
            "dry_run": True,
            "last_updated": generated_at,
            "recent_events": [
                {
                    "timestamp": generated_at,
                    "event_type": "controlled_dry_run_stop_recorded",
                    "severity": "INFO",
                    "message": payload.reason,
                    "source": "dry-run-control-service",
                    "details": {"kills_external_process": False},
                }
            ],
            "artifact_manifest_path": str(self.manifest_path) if manifest is not None else None,
        }
        if manifest is not None:
            snapshot_payload.update(
                {
                    "profile_name": manifest.get("profile_name"),
                    "strategy_version_id": manifest.get("strategy_version_id"),
                    "strategy_name": manifest.get("strategy_name"),
                    "pair": manifest.get("pair"),
                    "timeframe": manifest.get("timeframe"),
                }
            )

        snapshot = DryRunStatusSnapshot.model_validate(snapshot_payload)
        self._write_status_snapshot(snapshot)
        return self._report(
            status="STOPPED",
            generated_at=generated_at,
            readiness=None,
            status_snapshot=snapshot,
            manifest_path=str(self.manifest_path) if manifest is not None else None,
            config_path=str(manifest.get("config_path")) if manifest is not None and manifest.get("config_path") else None,
        )

    def snapshot(self) -> DryRunStatusSnapshot:
        return DryRunStatusSnapshotService().snapshot_from_controlled_json(self.status_snapshot_path)

    def management(self) -> dict[str, Any]:
        manifest = self._read_manifest()
        return {
            "manifest": manifest,
            "snapshot": self.snapshot().model_dump(mode="json"),
            "freq_ui_link": FreqUILinkMetadataService()
            .metadata_from_settings(self.settings)
            .model_dump(mode="json"),
        }

    def _start_blockers(
        self,
        payload: DryRunControlStartRequest,
        readiness_blockers: list[str],
        env_preflight: DryRunEnvPreflight,
    ) -> list[str]:
        blockers = list(readiness_blockers)
        if env_preflight.status == "BLOCKED" and env_preflight.blocked_reason:
            blockers.append(env_preflight.blocked_reason)
        if not payload.manual_approval:
            blockers.append("manual approval is required before controlled dry-run start")
        if not self.settings.allow_controlled_dry_run_process:
            blockers.append("controlled dry-run process start is disabled by backend safety setting")
        if self.settings.allow_live_trading:
            blockers.append("backend live trading setting must remain disabled")
        if self.settings.allow_dry_run_trading:
            blockers.append("legacy allow_dry_run_trading must remain disabled for this controlled boundary")
        return self._dedupe(blockers)

    def _blocked_preflight(
        self,
        env_preflight: DryRunEnvPreflight,
        blockers: list[str],
    ) -> DryRunEnvPreflight:
        return DryRunEnvPreflight(
            status="BLOCKED",
            required_env_present=env_preflight.required_env_present,
            required_env_missing=env_preflight.required_env_missing,
            optional_env_present=env_preflight.optional_env_present,
            optional_env_missing=env_preflight.optional_env_missing,
            blocked_reason="; ".join(blockers),
        )

    def _snapshot_from_manifest(
        self,
        manifest: FreqtradeDryRunArtifactManifest,
        profile: DryRunProfile,
        *,
        generated_at: datetime,
    ) -> DryRunStatusSnapshot:
        status: str = "STOPPED" if manifest.status == "SUCCESS" else manifest.status
        event_type = {
            "SUCCESS": "controlled_dry_run_completed",
            "FAILED": "controlled_dry_run_failed",
            "BLOCKED": "controlled_dry_run_blocked",
            "SKIPPED": "controlled_dry_run_skipped",
        }.get(manifest.status, "controlled_dry_run_status")
        message = (
            manifest.blocked_reason
            or manifest.failed_reason
            or manifest.skipped_reason
            or "controlled dry-run command completed and no managed process remains"
        )
        return DryRunStatusSnapshot.model_validate(
            {
                **self._snapshot_payload(
                    profile,
                    status=status,
                    generated_at=generated_at,
                    blocked_reason=manifest.blocked_reason,
                    failed_reason=manifest.failed_reason,
                    skipped_reason=manifest.skipped_reason,
                ),
                "artifact_manifest_path": str(manifest.manifest_path),
                "recent_events": [
                    {
                        "timestamp": generated_at,
                        "event_type": event_type,
                        "severity": "ERROR" if manifest.status == "FAILED" else "WARNING" if manifest.status == "BLOCKED" else "INFO",
                        "message": message,
                        "source": "dry-run-control-service",
                        "details": {
                            "return_code": manifest.return_code,
                            "starts_live_trading": False,
                            "places_real_orders": False,
                        },
                    }
                ],
            }
        )

    def _snapshot_payload(
        self,
        profile: DryRunProfile,
        *,
        status: str,
        generated_at: datetime,
        blocked_reason: Optional[str] = None,
        failed_reason: Optional[str] = None,
        skipped_reason: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "profile_name": profile.name,
            "strategy_version_id": profile.strategy.version_id,
            "strategy_name": profile.strategy.name,
            "exchange": profile.exchange.name,
            "pair": profile.pair,
            "timeframe": profile.timeframe,
            "dry_run": True,
            "balance_summary": {},
            "open_trades_summary": {},
            "recent_events": [],
            "blocked_reason": blocked_reason,
            "failed_reason": failed_reason,
            "skipped_reason": skipped_reason,
            "last_updated": generated_at,
        }

    def _blocked_snapshot(self, reason: str, *, generated_at: datetime) -> DryRunStatusSnapshot:
        return DryRunStatusSnapshot.model_validate(
            {
                "status": "BLOCKED",
                "dry_run": None,
                "blocked_reason": reason,
                "last_updated": generated_at,
                "recent_events": [
                    {
                        "timestamp": generated_at,
                        "event_type": "controlled_dry_run_blocked",
                        "severity": "WARNING",
                        "message": reason,
                        "source": "dry-run-control-service",
                    }
                ],
            }
        )

    def _write_status_snapshot(self, snapshot: DryRunStatusSnapshot) -> None:
        self.status_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_snapshot_path.write_text(
            snapshot.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

    def _read_manifest(self) -> Optional[dict[str, Any]]:
        if not self.manifest_path.exists() or not self.manifest_path.is_file():
            return None
        return FreqtradeDryRunArtifactManifest.read(self.manifest_path)

    def _report(
        self,
        *,
        status: DryRunControlStatus,
        generated_at: datetime,
        readiness: Optional[Any],
        status_snapshot: DryRunStatusSnapshot,
        manifest: Optional[FreqtradeDryRunArtifactManifest] = None,
        manifest_path: Optional[str] = None,
        config_path: Optional[Path | str] = None,
        blocked_reasons: Optional[list[str]] = None,
        failed_reason: Optional[str] = None,
        skipped_reason: Optional[str] = None,
    ) -> DryRunControlReport:
        return DryRunControlReport(
            status=status,
            generated_at=generated_at,
            manifest_path=str(manifest.manifest_path) if manifest else manifest_path,
            config_path=str(config_path) if config_path is not None else None,
            status_snapshot_path=str(self.status_snapshot_path),
            readiness=readiness,
            status_snapshot=status_snapshot,
            blocked_reasons=blocked_reasons or [],
            failed_reason=failed_reason,
            skipped_reason=skipped_reason,
            safety={
                "local_only": True,
                "dry_run": status_snapshot.dry_run is True or status in ("BLOCKED", "STOPPED"),
                "starts_live_trading": False,
                "places_real_orders": False,
                "stores_sensitive_values": False,
                "uses_background_queue": False,
                "kills_external_process": False,
            },
        )

    def _control_status(self, status: str) -> DryRunControlStatus:
        if status == "SUCCESS":
            return "SUCCESS"
        if status == "FAILED":
            return "FAILED"
        if status == "BLOCKED":
            return "BLOCKED"
        if status == "SKIPPED":
            return "SKIPPED"
        return "FAILED"

    def _dedupe(self, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            clean = value.strip()
            if clean and clean not in seen:
                result.append(clean)
                seen.add(clean)
        return result

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)
