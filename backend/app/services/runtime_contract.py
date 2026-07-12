from __future__ import annotations

import json
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.config import REPO_ROOT, get_settings
from app.schemas.dry_run_status import DryRunStatusSnapshot, redact_dry_run_status_payload
from app.schemas.live_candidate import LiveCandidateMonitoringSnapshot
from app.schemas.runtime_contract import (
    RuntimeArtifactLink,
    RuntimeFallbackStatus,
    RuntimeReadOnlyContract,
    RuntimeReadOnlySource,
    RuntimeReadOnlyStatus,
    RuntimeStatusSummary,
)
from app.services.dry_run_status import DryRunStatusSnapshotService
from app.services.live_candidate_monitoring import LiveCandidateMonitoringSnapshotService
from app.services.research_readiness import ResearchReadinessService


DEFAULT_RUNTIME_REPORT_DIR = REPO_ROOT / "reports" / "runtime"
DEFAULT_DRY_RUN_STATUS_PATH = DEFAULT_RUNTIME_REPORT_DIR / "dry-run-status.json"
DEFAULT_DRY_RUN_MANIFEST_PATH = DEFAULT_RUNTIME_REPORT_DIR / "dry-run-manifest.json"
DEFAULT_LIVE_CANDIDATE_MONITORING_PATH = DEFAULT_RUNTIME_REPORT_DIR / "live-candidate-monitoring.json"
DEFAULT_LIVE_CANDIDATE_MONITORING_MANIFEST_PATH = (
    DEFAULT_RUNTIME_REPORT_DIR / "live-candidate-monitoring-manifest.json"
)
DEFAULT_PHASE7_SMOKE_SUMMARY_PATH = DEFAULT_RUNTIME_REPORT_DIR / "phase7-smoke-summary.json"


class RuntimeReadOnlyContractService:
    """Builds a read-only runtime contract from local DTOs and artifacts."""

    def __init__(
        self,
        dry_run_service: Optional[DryRunStatusSnapshotService] = None,
        monitoring_service: Optional[LiveCandidateMonitoringSnapshotService] = None,
        research_service: Optional[ResearchReadinessService] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._dry_run_service = dry_run_service or DryRunStatusSnapshotService()
        self._monitoring_service = monitoring_service or LiveCandidateMonitoringSnapshotService()
        self._research_service = research_service or ResearchReadinessService()
        self._now_provider = now_provider

    def build_contract(
        self,
        dry_run_status_path: Optional[Path] = None,
        dry_run_manifest_path: Optional[Path] = None,
        live_candidate_monitoring_path: Optional[Path] = None,
        live_candidate_monitoring_manifest_path: Optional[Path] = None,
        phase7_smoke_summary_path: Optional[Path] = None,
    ) -> RuntimeReadOnlyContract:
        dry_run_status_path = dry_run_status_path or DEFAULT_DRY_RUN_STATUS_PATH
        dry_run_manifest_path = dry_run_manifest_path or DEFAULT_DRY_RUN_MANIFEST_PATH
        live_candidate_monitoring_path = (
            live_candidate_monitoring_path or DEFAULT_LIVE_CANDIDATE_MONITORING_PATH
        )
        live_candidate_monitoring_manifest_path = (
            live_candidate_monitoring_manifest_path
            or DEFAULT_LIVE_CANDIDATE_MONITORING_MANIFEST_PATH
        )
        phase7_smoke_summary_path = phase7_smoke_summary_path or DEFAULT_PHASE7_SMOKE_SUMMARY_PATH

        generated_at = self._now()
        dry_run_snapshot, dry_run_summary = self._dry_run_snapshot(
            status_path=dry_run_status_path,
            manifest_path=dry_run_manifest_path,
        )
        monitoring_snapshot, monitoring_summary = self._monitoring_snapshot(
            status_path=live_candidate_monitoring_path,
            manifest_path=live_candidate_monitoring_manifest_path,
        )
        smoke_summary = self._smoke_summary(phase7_smoke_summary_path)
        system_status = self._system_status(generated_at)
        research_readiness = self._research_service.build()
        dry_run_readiness = self._dry_run_readiness(dry_run_summary)
        live_readiness = self._live_readiness(generated_at)
        runtime_readiness = self._runtime_readiness(
            generated_at=generated_at,
            components=[research_readiness, system_status],
        )
        fallback_status = self._fallback_status(
            [research_readiness, system_status]
        )
        blocked_reasons = self._blocked_reasons(
            [runtime_readiness, research_readiness, dry_run_readiness, live_readiness, system_status],
            dry_run_snapshot=dry_run_snapshot,
            monitoring_snapshot=monitoring_snapshot,
        )
        unavailable_reasons = self._unavailable_reasons(
            [runtime_readiness, research_readiness, dry_run_readiness, live_readiness, system_status],
            monitoring_snapshot=monitoring_snapshot,
        )
        status = self._overall_status(
            [runtime_readiness, research_readiness, system_status]
        )

        return RuntimeReadOnlyContract(
            status=status,
            generated_at=generated_at,
            system_status=system_status,
            runtime_readiness=runtime_readiness,
            research_readiness=research_readiness,
            dry_run_readiness=dry_run_readiness,
            live_readiness=live_readiness,
            fallback_status=fallback_status,
            smoke_status=smoke_summary,
            dry_run_status=dry_run_snapshot,
            live_candidate_monitoring=monitoring_snapshot,
            artifact_links=self._artifact_links(
                dry_run_status_path=dry_run_status_path,
                dry_run_manifest_path=dry_run_manifest_path,
                live_candidate_monitoring_path=live_candidate_monitoring_path,
                live_candidate_monitoring_manifest_path=live_candidate_monitoring_manifest_path,
                phase7_smoke_summary_path=phase7_smoke_summary_path,
                dry_run_snapshot=dry_run_snapshot,
                monitoring_snapshot=monitoring_snapshot,
            ),
            blocked_reasons=blocked_reasons,
            unavailable_reasons=unavailable_reasons,
        )

    def _dry_run_snapshot(
        self,
        status_path: Path,
        manifest_path: Path,
    ) -> tuple[DryRunStatusSnapshot, RuntimeStatusSummary]:
        if manifest_path.exists():
            snapshot = self._dry_run_service.snapshot_from_artifact_manifest(manifest_path)
            source: RuntimeReadOnlySource = "artifact"
            source_ref = str(manifest_path)
        else:
            snapshot = self._dry_run_service.snapshot_from_controlled_json(status_path)
            source = "controlled-local-json" if status_path.exists() else "missing"
            source_ref = str(status_path)

        status = self._dry_run_runtime_status(snapshot)
        return snapshot, RuntimeStatusSummary(
            name="dry_run_status",
            status=status,
            source=source,
            source_ref=source_ref,
            last_updated=snapshot.last_updated,
            summary=f"Dry-run runtime status is {snapshot.status}.",
            blocked_reason=snapshot.blocked_reason or snapshot.failed_reason,
            unavailable_reason=snapshot.skipped_reason if snapshot.status == "SKIPPED" else None,
            warnings=[],
        )

    def _monitoring_snapshot(
        self,
        status_path: Path,
        manifest_path: Path,
    ) -> tuple[LiveCandidateMonitoringSnapshot, RuntimeStatusSummary]:
        if manifest_path.exists():
            snapshot = self._monitoring_service.snapshot_from_artifact_manifest(manifest_path)
            source: RuntimeReadOnlySource = "artifact"
            source_ref = str(manifest_path)
        else:
            snapshot = self._monitoring_service.snapshot_from_controlled_json(status_path)
            source = "controlled-local-json" if status_path.exists() else "missing"
            source_ref = str(status_path)

        status = self._monitoring_runtime_status(snapshot)
        return snapshot, RuntimeStatusSummary(
            name="live_candidate_monitoring",
            status=status,
            source=source,
            source_ref=source_ref,
            last_updated=snapshot.last_updated,
            summary=f"Live-candidate monitoring status is {snapshot.status}.",
            blocked_reason="; ".join(snapshot.blockers) or None,
            unavailable_reason=snapshot.unavailable_reason,
            stale_reason=snapshot.stale_reason,
            warnings=snapshot.warnings,
        )

    def _smoke_summary(self, path: Path) -> RuntimeStatusSummary:
        if not path.exists():
            return RuntimeStatusSummary(
                name="phase7_smoke",
                status="UNAVAILABLE",
                source="missing",
                source_ref=str(path),
                summary="Phase 7 smoke summary has not been generated.",
                unavailable_reason=f"Phase 7 smoke summary does not exist: {path}",
            )
        if not path.is_file():
            return RuntimeStatusSummary(
                name="phase7_smoke",
                status="BLOCKED",
                source="artifact",
                source_ref=str(path),
                summary="Phase 7 smoke summary path is not a file.",
                blocked_reason=f"Phase 7 smoke summary path is not a file: {path}",
            )

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except JSONDecodeError:
            return RuntimeStatusSummary(
                name="phase7_smoke",
                status="BLOCKED",
                source="artifact",
                source_ref=str(path),
                summary="Phase 7 smoke summary is not valid JSON.",
                blocked_reason="Phase 7 smoke summary is not valid JSON",
            )
        if not isinstance(payload, dict):
            return RuntimeStatusSummary(
                name="phase7_smoke",
                status="BLOCKED",
                source="artifact",
                source_ref=str(path),
                summary="Phase 7 smoke summary root is not an object.",
                blocked_reason="Phase 7 smoke summary root must be an object",
            )

        sanitized = redact_dry_run_status_payload(payload)
        raw_status = self._optional_text(
            sanitized.get("status"),
            sanitized.get("result"),
            sanitized.get("outcome"),
        )
        status = self._smoke_runtime_status(raw_status)
        blocked_reason = self._optional_text(
            sanitized.get("blocked_reason"),
            sanitized.get("failed_reason"),
            sanitized.get("error"),
        )
        unavailable_reason = self._optional_text(sanitized.get("unavailable_reason"))
        warnings = self._text_list(sanitized.get("warnings"))

        return RuntimeStatusSummary(
            name="phase7_smoke",
            status=status,
            source="artifact",
            source_ref=str(path),
            last_updated=self._optional_datetime(
                sanitized.get("last_updated"),
                sanitized.get("generated_at"),
                sanitized.get("timestamp"),
            ),
            summary=f"Phase 7 smoke summary status is {raw_status or 'UNKNOWN'}.",
            blocked_reason=blocked_reason if status == "BLOCKED" else None,
            unavailable_reason=unavailable_reason if status == "UNAVAILABLE" else None,
            warnings=warnings,
        )

    def _system_status(self, generated_at: datetime) -> RuntimeStatusSummary:
        settings = get_settings()
        unsafe_flags: list[str] = []
        if settings.allow_live_trading:
            unsafe_flags.append("allow_live_trading=true")
        if settings.allow_dry_run_trading:
            unsafe_flags.append("allow_dry_run_trading=true")

        if unsafe_flags:
            reason = "Runtime read-only contract requires trading control flags to stay disabled."
            return RuntimeStatusSummary(
                name="system_status",
                status="BLOCKED",
                source="settings",
                last_updated=generated_at,
                summary=reason,
                blocked_reason=f"{reason} Unsafe flags: {', '.join(unsafe_flags)}",
            )

        return RuntimeStatusSummary(
            name="system_status",
            status="READY",
            source="settings",
            last_updated=generated_at,
            summary="Application settings keep runtime control disabled.",
        )

    def _runtime_readiness(
        self,
        generated_at: datetime,
        components: list[RuntimeStatusSummary],
    ) -> RuntimeStatusSummary:
        status = self._overall_status(components)
        blocked = self._first_reason(components, "blocked_reason")
        unavailable = self._first_reason(components, "unavailable_reason")
        stale = self._first_reason(components, "stale_reason")
        warnings = [warning for component in components for warning in component.warnings]
        summary = {
            "READY": "Runtime read-only contract has all required local evidence.",
            "WARNING": "Runtime read-only contract has non-blocking warnings.",
            "STALE": "Runtime read-only contract has stale local evidence.",
            "UNAVAILABLE": "Runtime read-only contract is missing local evidence.",
            "BLOCKED": "Runtime read-only contract is blocked by local evidence.",
        }[status]
        return RuntimeStatusSummary(
            name="runtime_readiness",
            status=status,
            source="derived",
            last_updated=generated_at,
            summary=summary,
            blocked_reason=blocked,
            unavailable_reason=unavailable,
            stale_reason=stale,
            warnings=warnings,
        )

    def _dry_run_readiness(self, dry_run_summary: RuntimeStatusSummary) -> RuntimeStatusSummary:
        """Expose dry-run evidence without allowing it to mask research readiness."""
        status = dry_run_summary.status
        return RuntimeStatusSummary(
            name="dry_run_readiness",
            status=status,
            source=dry_run_summary.source,
            source_ref=dry_run_summary.source_ref,
            last_updated=dry_run_summary.last_updated,
            summary=(
                "Dry-run readiness is independent from research and requires explicit local authorization."
                if status in {"BLOCKED", "UNAVAILABLE"}
                else "Dry-run readiness has local read-only evidence."
            ),
            blocked_reason=dry_run_summary.blocked_reason,
            unavailable_reason=dry_run_summary.unavailable_reason,
            stale_reason=dry_run_summary.stale_reason,
            warnings=dry_run_summary.warnings,
        )

    def _live_readiness(self, generated_at: datetime) -> RuntimeStatusSummary:
        return RuntimeStatusSummary(
            name="live_readiness",
            status="BLOCKED",
            source="derived",
            last_updated=generated_at,
            summary="Live readiness is disabled by policy and has no start control.",
            blocked_reason="live trading is disabled pending a future separately authorized issue",
        )

    def _fallback_status(self, components: list[RuntimeStatusSummary]) -> RuntimeFallbackStatus:
        fallback_sources = [
            component.name
            for component in components
            if component.status in {"BLOCKED", "UNAVAILABLE", "STALE"}
        ]
        active = bool(fallback_sources)
        status = self._overall_status(components)
        return RuntimeFallbackStatus(
            active=active,
            status=status,
            reason=(
                "Runtime contract is using blocked, stale, or unavailable fallback state."
                if active
                else None
            ),
            sources=fallback_sources,
        )

    def _artifact_links(
        self,
        dry_run_status_path: Path,
        dry_run_manifest_path: Path,
        live_candidate_monitoring_path: Path,
        live_candidate_monitoring_manifest_path: Path,
        phase7_smoke_summary_path: Path,
        dry_run_snapshot: DryRunStatusSnapshot,
        monitoring_snapshot: LiveCandidateMonitoringSnapshot,
    ) -> list[RuntimeArtifactLink]:
        links = [
            self._artifact_link("dry_run_status_json", dry_run_status_path, "controlled-local-json"),
            self._artifact_link("dry_run_manifest", dry_run_manifest_path, "artifact"),
            self._artifact_link(
                "live_candidate_monitoring_json",
                live_candidate_monitoring_path,
                "controlled-local-json",
            ),
            self._artifact_link(
                "live_candidate_monitoring_manifest",
                live_candidate_monitoring_manifest_path,
                "artifact",
            ),
            self._artifact_link("phase7_smoke_summary", phase7_smoke_summary_path, "artifact"),
        ]
        if dry_run_snapshot.artifact_manifest_path:
            links.append(
                self._artifact_link(
                    "dry_run_snapshot_manifest_ref",
                    Path(dry_run_snapshot.artifact_manifest_path),
                    "artifact",
                )
            )
        if monitoring_snapshot.source.ref:
            links.append(
                self._artifact_link(
                    "live_candidate_monitoring_source_ref",
                    Path(monitoring_snapshot.source.ref),
                    monitoring_snapshot.source.source,
                )
            )
        return links

    def _artifact_link(
        self,
        name: str,
        path: Path,
        source: RuntimeReadOnlySource,
    ) -> RuntimeArtifactLink:
        return RuntimeArtifactLink(
            name=name,
            path=str(path),
            source=source,
            status="READY" if path.exists() else "UNAVAILABLE",
            exists=path.exists(),
        )

    def _blocked_reasons(
        self,
        components: list[RuntimeStatusSummary],
        dry_run_snapshot: DryRunStatusSnapshot,
        monitoring_snapshot: LiveCandidateMonitoringSnapshot,
    ) -> list[str]:
        reasons = [component.blocked_reason for component in components if component.blocked_reason]
        reasons.extend(monitoring_snapshot.blockers)
        if dry_run_snapshot.failed_reason:
            reasons.append(dry_run_snapshot.failed_reason)
        return self._dedupe_text(reasons)

    def _unavailable_reasons(
        self,
        components: list[RuntimeStatusSummary],
        monitoring_snapshot: LiveCandidateMonitoringSnapshot,
    ) -> list[str]:
        reasons = [
            component.unavailable_reason
            for component in components
            if component.unavailable_reason
        ]
        if monitoring_snapshot.unavailable_reason:
            reasons.append(monitoring_snapshot.unavailable_reason)
        return self._dedupe_text(reasons)

    def _overall_status(self, components: list[RuntimeStatusSummary]) -> RuntimeReadOnlyStatus:
        statuses = {component.status for component in components}
        if "BLOCKED" in statuses:
            return "BLOCKED"
        if "STALE" in statuses:
            return "STALE"
        if "UNAVAILABLE" in statuses:
            return "UNAVAILABLE"
        if "WARNING" in statuses:
            return "WARNING"
        return "READY"

    def _dry_run_runtime_status(self, snapshot: DryRunStatusSnapshot) -> RuntimeReadOnlyStatus:
        if snapshot.status in {"SUCCESS", "RUNNING", "STOPPED"}:
            return "READY"
        if snapshot.status == "SKIPPED":
            return "UNAVAILABLE"
        return "BLOCKED"

    def _monitoring_runtime_status(
        self,
        snapshot: LiveCandidateMonitoringSnapshot,
    ) -> RuntimeReadOnlyStatus:
        return {
            "OK": "READY",
            "WARNING": "WARNING",
            "STALE": "STALE",
            "UNAVAILABLE": "UNAVAILABLE",
            "BLOCKED": "BLOCKED",
        }[snapshot.status]

    def _smoke_runtime_status(self, value: Optional[str]) -> RuntimeReadOnlyStatus:
        if value is None:
            return "UNAVAILABLE"
        normalized = value.strip().upper().replace("-", "_")
        if normalized in {"OK", "PASS", "PASSED", "SUCCESS", "SUCCEEDED", "READY"}:
            return "READY"
        if normalized in {"WARN", "WARNING"}:
            return "WARNING"
        if normalized in {"STALE", "EXPIRED"}:
            return "STALE"
        if normalized in {"MISSING", "UNAVAILABLE", "SKIPPED"}:
            return "UNAVAILABLE"
        return "BLOCKED"

    def _first_reason(
        self,
        components: list[RuntimeStatusSummary],
        field_name: str,
    ) -> Optional[str]:
        for component in components:
            value = getattr(component, field_name)
            if value:
                return str(value)
        return None

    def _optional_text(self, *values: Any) -> Optional[str]:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _text_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        raw_items = value if isinstance(value, list) else [value]
        return [str(item).strip() for item in raw_items if str(item).strip()]

    def _optional_datetime(self, *values: Any) -> Optional[datetime]:
        for value in values:
            if isinstance(value, datetime):
                if value.tzinfo is None or value.utcoffset() is None:
                    return value.replace(tzinfo=timezone.utc)
                return value
            if isinstance(value, str) and value.strip():
                normalized = value.strip()
                if normalized.endswith("Z"):
                    normalized = f"{normalized[:-1]}+00:00"
                try:
                    parsed = datetime.fromisoformat(normalized)
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed
        return None

    def _dedupe_text(self, values: list[Optional[str]]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value is None:
                continue
            clean = str(redact_dry_run_status_payload(value)).strip()
            if clean and clean not in seen:
                result.append(clean)
                seen.add(clean)
        return result

    def _now(self) -> datetime:
        if self._now_provider is not None:
            now = self._now_provider()
            if now.tzinfo is None or now.utcoffset() is None:
                return now.replace(tzinfo=timezone.utc)
            return now
        return datetime.now(timezone.utc)
