from __future__ import annotations

import json
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import ValidationError

from app.schemas.live_candidate import (
    LiveCandidateAlertSeverity,
    LiveCandidateAlertSummary,
    LiveCandidateMonitoringDataSource,
    LiveCandidateMonitoringSnapshot,
    LiveCandidateMonitoringSourceType,
    LiveCandidateMonitoringStatus,
    LiveCandidateProfile,
)


MONITORING_FORBIDDEN_CONTROL_KEYS = frozenset(
    {
        "control_action",
        "control_actions",
        "control_url",
        "deploy_live",
        "freqtrade_live_rest",
        "live_rest",
        "start",
        "start_bot",
        "start_live",
        "stop",
        "stop_bot",
        "stop_live",
    }
)


class LiveCandidateMonitoringParser:
    """Parses read-only live-candidate monitoring artifacts into governance DTOs."""

    def __init__(
        self,
        now_provider: Optional[Callable[[], datetime]] = None,
        default_stale_after_seconds: int = 3600,
    ) -> None:
        self._now_provider = now_provider
        self._default_stale_after_seconds = default_stale_after_seconds

    def parse_fixture_payload(self, payload: dict[str, Any]) -> LiveCandidateMonitoringSnapshot:
        return self.parse_status_payload(payload, source="fixture")

    def parse_controlled_json_payload(self, payload: dict[str, Any]) -> LiveCandidateMonitoringSnapshot:
        return self.parse_status_payload(payload, source="controlled-local-json")

    def parse_status_payload(
        self,
        payload: dict[str, Any],
        source: LiveCandidateMonitoringSourceType,
        source_ref: Optional[str] = None,
    ) -> LiveCandidateMonitoringSnapshot:
        try:
            self._reject_unsafe_payload(payload)
            normalized = self._normalize_status_payload(payload, source=source, source_ref=source_ref)
            return LiveCandidateMonitoringSnapshot.model_validate(normalized)
        except Exception as exc:
            if self._is_secret_or_control_rejection(exc):
                return self.blocked_snapshot(
                    blocked_reason=self._safe_blocked_reason(exc),
                    source=source,
                    source_ref=source_ref,
                )
            return self.unavailable_snapshot(
                unavailable_reason=self._safe_unavailable_reason(exc),
                source=source,
                source_ref=source_ref,
            )

    def parse_artifact_manifest_payload(
        self,
        payload: dict[str, Any],
        artifact_manifest_path: Path,
    ) -> LiveCandidateMonitoringSnapshot:
        try:
            self._reject_unsafe_payload(payload)
            snapshots = payload.get("monitoring_snapshots")
            if snapshots is None:
                snapshots = payload.get("status_snapshots")
            if not snapshots:
                return self.unavailable_snapshot(
                    unavailable_reason="live-candidate artifact manifest does not contain monitoring_snapshots",
                    source="artifact",
                    source_ref=str(artifact_manifest_path),
                )
            if not isinstance(snapshots, list):
                return self.unavailable_snapshot(
                    unavailable_reason="live-candidate artifact manifest monitoring_snapshots must be a list",
                    source="artifact",
                    source_ref=str(artifact_manifest_path),
                )

            latest = snapshots[-1]
            if not isinstance(latest, dict):
                return self.unavailable_snapshot(
                    unavailable_reason="live-candidate artifact manifest latest monitoring snapshot must be an object",
                    source="artifact",
                    source_ref=str(artifact_manifest_path),
                )

            merged = self._manifest_base_payload(payload)
            merged.update(latest)
            merged["artifact_manifest_path"] = str(artifact_manifest_path)
            return self.parse_status_payload(
                merged,
                source="artifact",
                source_ref=str(artifact_manifest_path),
            )
        except Exception as exc:
            if self._is_secret_or_control_rejection(exc):
                return self.blocked_snapshot(
                    blocked_reason=self._safe_blocked_reason(exc),
                    source="artifact",
                    source_ref=str(artifact_manifest_path),
                )
            return self.unavailable_snapshot(
                unavailable_reason=self._safe_unavailable_reason(exc),
                source="artifact",
                source_ref=str(artifact_manifest_path),
            )

    def blocked_snapshot(
        self,
        blocked_reason: str,
        source: LiveCandidateMonitoringSourceType = "controlled-local-json",
        source_ref: Optional[str] = None,
    ) -> LiveCandidateMonitoringSnapshot:
        now = self._now()
        data_source = self._data_source(source=source, source_ref=source_ref, generated_at=now)
        return LiveCandidateMonitoringSnapshot(
            status="BLOCKED",
            source=data_source,
            last_updated=now,
            blockers=[blocked_reason],
            alerts=[
                LiveCandidateAlertSummary(
                    alert_id="monitoring-input-blocked",
                    status="BLOCKED",
                    severity="ERROR",
                    message=blocked_reason,
                    source=data_source,
                    last_updated=now,
                )
            ],
        )

    def unavailable_snapshot(
        self,
        unavailable_reason: str,
        source: LiveCandidateMonitoringSourceType = "controlled-local-json",
        source_ref: Optional[str] = None,
    ) -> LiveCandidateMonitoringSnapshot:
        now = self._now()
        data_source = self._data_source(source=source, source_ref=source_ref, generated_at=now)
        return LiveCandidateMonitoringSnapshot(
            status="UNAVAILABLE",
            source=data_source,
            last_updated=now,
            unavailable_reason=unavailable_reason,
            alerts=[
                LiveCandidateAlertSummary(
                    alert_id="monitoring-input-unavailable",
                    status="UNAVAILABLE",
                    severity="WARNING",
                    message=unavailable_reason,
                    source=data_source,
                    last_updated=now,
                )
            ],
        )

    def _normalize_status_payload(
        self,
        payload: dict[str, Any],
        source: LiveCandidateMonitoringSourceType,
        source_ref: Optional[str],
    ) -> dict[str, Any]:
        snapshot_payload = self._nested_snapshot_payload(payload)
        last_updated = self._last_updated(snapshot_payload)
        data_source = self._data_source(
            source=source,
            source_ref=self._optional_text(
                source_ref,
                snapshot_payload.get("source_ref"),
                snapshot_payload.get("artifact_manifest_path"),
            ),
            generated_at=self._optional_datetime(
                snapshot_payload.get("generated_at"),
                snapshot_payload.get("source_generated_at"),
            ),
        )
        alerts = self._alerts(
            snapshot_payload.get("alerts") or snapshot_payload.get("alert_summaries"),
            source=data_source,
            fallback_last_updated=last_updated,
        )
        blockers = self._text_list(snapshot_payload.get("blockers"))
        warnings = self._text_list(snapshot_payload.get("warnings"))
        status = self._derive_status(
            snapshot_payload.get("status") or snapshot_payload.get("state"),
            alerts=alerts,
            blockers=blockers,
            unavailable_reason=self._optional_text(snapshot_payload.get("unavailable_reason")),
            stale_reason=self._optional_text(snapshot_payload.get("stale_reason")),
            warnings=warnings,
        )
        stale_reason = self._optional_text(snapshot_payload.get("stale_reason"))
        stale_after_seconds = self._optional_int(snapshot_payload.get("stale_after_seconds"))
        if (
            status in {"OK", "WARNING"}
            and stale_after_seconds is not None
            and self._is_stale(last_updated, stale_after_seconds)
        ):
            status = "STALE"
            stale_reason = f"monitoring data is older than {stale_after_seconds} seconds"

        return {
            "status": status,
            "source": data_source,
            "last_updated": last_updated,
            "profile_name": self._optional_text(
                snapshot_payload.get("profile_name"),
                self._mapping(snapshot_payload.get("profile")).get("name"),
            ),
            "profile_hash": self._optional_text(snapshot_payload.get("profile_hash")),
            "deployment_record_id": self._optional_text(
                snapshot_payload.get("deployment_record_id"),
                snapshot_payload.get("record_id"),
            ),
            "deployment_status": self._optional_text(snapshot_payload.get("deployment_status")),
            "approval_status": self._optional_text(snapshot_payload.get("approval_status")),
            "preflight_status": self._optional_text(snapshot_payload.get("preflight_status")),
            "pair": self._optional_text(snapshot_payload.get("pair")),
            "timeframe": self._optional_text(snapshot_payload.get("timeframe")),
            "alerts": alerts,
            "blockers": blockers,
            "unavailable_reason": self._optional_text(snapshot_payload.get("unavailable_reason")),
            "stale_reason": stale_reason,
            "warnings": warnings,
        }

    def _manifest_base_payload(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": manifest.get("status"),
            "profile_name": manifest.get("profile_name"),
            "profile_hash": manifest.get("profile_hash"),
            "deployment_record_id": manifest.get("deployment_record_id"),
            "deployment_status": manifest.get("deployment_status"),
            "approval_status": manifest.get("approval_status"),
            "preflight_status": manifest.get("preflight_status"),
            "pair": manifest.get("pair"),
            "timeframe": manifest.get("timeframe"),
            "blockers": manifest.get("blockers"),
            "warnings": manifest.get("warnings"),
            "unavailable_reason": manifest.get("unavailable_reason"),
            "stale_reason": manifest.get("stale_reason"),
            "last_updated": manifest.get("last_updated"),
            "generated_at": manifest.get("generated_at"),
            "source_ref": manifest.get("source_ref"),
        }

    def _reject_unsafe_payload(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ValueError("live-candidate monitoring payload must be a JSON object")
        self._reject_monitoring_control_keys(payload)
        LiveCandidateProfile._reject_forbidden_input(payload)

    def _reject_monitoring_control_keys(self, payload: Any) -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in MONITORING_FORBIDDEN_CONTROL_KEYS:
                    raise ValueError(f"live candidate monitoring contains forbidden control key: {key}")
                self._reject_monitoring_control_keys(value)
            return
        if isinstance(payload, list):
            for value in payload:
                self._reject_monitoring_control_keys(value)

    def _nested_snapshot_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        for key in ("snapshot", "monitoring_snapshot", "live_candidate_monitoring"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                merged = {item_key: value for item_key, value in payload.items() if item_key != "monitoring_snapshots"}
                merged.update(nested)
                return merged
        return payload

    def _data_source(
        self,
        source: LiveCandidateMonitoringSourceType,
        source_ref: Optional[str],
        generated_at: Optional[datetime],
    ) -> LiveCandidateMonitoringDataSource:
        return LiveCandidateMonitoringDataSource(
            source=source,
            ref=source_ref or f"{source}:inline",
            generated_at=generated_at,
        )

    def _alerts(
        self,
        payload: Any,
        source: LiveCandidateMonitoringDataSource,
        fallback_last_updated: datetime,
    ) -> list[LiveCandidateAlertSummary]:
        if not payload:
            return []
        raw_alerts = payload if isinstance(payload, list) else [payload]
        alerts: list[LiveCandidateAlertSummary] = []
        for index, item in enumerate(raw_alerts[:100]):
            if isinstance(item, str):
                alerts.append(
                    LiveCandidateAlertSummary(
                        alert_id=f"alert-{index + 1}",
                        status="WARNING",
                        severity="WARNING",
                        message=item,
                        source=source,
                        last_updated=fallback_last_updated,
                    )
                )
                continue
            if not isinstance(item, dict):
                continue
            severity = self._normalize_severity(item.get("severity") or item.get("level"))
            status = self._normalize_status(
                item.get("status") or item.get("state") or self._status_from_severity(severity)
            )
            alerts.append(
                LiveCandidateAlertSummary(
                    alert_id=self._optional_text(item.get("alert_id"), item.get("id"))
                    or f"alert-{index + 1}",
                    status=status,
                    severity=severity,
                    message=self._optional_text(item.get("message"), item.get("summary")) or "monitoring alert",
                    source=source,
                    last_updated=self._optional_datetime(
                        item.get("last_updated"),
                        item.get("updated_at"),
                        item.get("timestamp"),
                    )
                    or fallback_last_updated,
                    evidence_ref=self._optional_text(item.get("evidence_ref")),
                    details=self._mapping(item.get("details")),
                )
            )
        return alerts

    def _derive_status(
        self,
        raw_status: Any,
        alerts: list[LiveCandidateAlertSummary],
        blockers: list[str],
        unavailable_reason: Optional[str],
        stale_reason: Optional[str],
        warnings: list[str],
    ) -> LiveCandidateMonitoringStatus:
        if raw_status is not None:
            return self._normalize_status(raw_status)
        if blockers or any(alert.status == "BLOCKED" for alert in alerts):
            return "BLOCKED"
        if unavailable_reason or any(alert.status == "UNAVAILABLE" for alert in alerts):
            return "UNAVAILABLE"
        if stale_reason or any(alert.status == "STALE" for alert in alerts):
            return "STALE"
        if warnings or any(alert.status == "WARNING" for alert in alerts):
            return "WARNING"
        return "OK"

    def _normalize_status(self, value: Any) -> LiveCandidateMonitoringStatus:
        if not isinstance(value, str) or not value.strip():
            return "UNAVAILABLE"
        normalized = value.strip().upper().replace("-", "_")
        aliases: dict[str, LiveCandidateMonitoringStatus] = {
            "ACTIVE": "OK",
            "AVAILABLE": "OK",
            "HEALTHY": "OK",
            "PASS": "OK",
            "PASSED": "OK",
            "READY": "OK",
            "SUCCESS": "OK",
            "WARN": "WARNING",
            "DEGRADED": "WARNING",
            "OLD": "STALE",
            "EXPIRED": "STALE",
            "MISSING": "UNAVAILABLE",
            "OFFLINE": "UNAVAILABLE",
            "FAILED": "UNAVAILABLE",
            "FAILURE": "UNAVAILABLE",
            "ERROR": "UNAVAILABLE",
        }
        normalized = aliases.get(normalized, normalized)
        allowed = {"OK", "WARNING", "STALE", "UNAVAILABLE", "BLOCKED"}
        if normalized not in allowed:
            raise ValueError("live-candidate monitoring payload contains unsupported status")
        return normalized  # type: ignore[return-value]

    def _normalize_severity(self, value: Any) -> LiveCandidateAlertSeverity:
        if not isinstance(value, str) or not value.strip():
            return "INFO"
        normalized = value.strip().upper()
        if normalized in {"WARN", "WARNING"}:
            return "WARNING"
        if normalized in {"ERROR", "FAILED", "FAILURE"}:
            return "ERROR"
        if normalized in {"CRITICAL", "FATAL"}:
            return "CRITICAL"
        return "INFO"

    def _status_from_severity(self, severity: LiveCandidateAlertSeverity) -> LiveCandidateMonitoringStatus:
        if severity in {"ERROR", "CRITICAL"}:
            return "BLOCKED"
        if severity == "WARNING":
            return "WARNING"
        return "OK"

    def _last_updated(self, payload: dict[str, Any]) -> datetime:
        explicit = self._optional_datetime(
            payload.get("last_updated"),
            payload.get("updated_at"),
            payload.get("timestamp"),
        )
        return explicit or self._now()

    def _is_stale(self, last_updated: datetime, stale_after_seconds: int) -> bool:
        if stale_after_seconds < 0:
            return False
        return (self._now() - last_updated).total_seconds() > stale_after_seconds

    def _text_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        raw_items = value if isinstance(value, list) else [value]
        return [str(item).strip() for item in raw_items if str(item).strip()]

    def _mapping(self, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _optional_text(self, *values: Any) -> Optional[str]:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _optional_int(self, value: Any) -> Optional[int]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        return None

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

    def _is_secret_or_control_rejection(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            "forbidden secret key" in message
            or "secret-shaped value" in message
            or "forbidden runtime key" in message
            or "forbidden control key" in message
        )

    def _safe_blocked_reason(self, exc: Exception) -> str:
        message = str(exc).lower()
        if "forbidden control key" in message or "forbidden runtime key" in message:
            return "live-candidate monitoring control-shaped input was rejected"
        return "live-candidate monitoring sensitive input was rejected"

    def _safe_unavailable_reason(self, exc: Exception) -> str:
        if isinstance(exc, ValidationError):
            return "live-candidate monitoring payload failed DTO validation"
        message = str(exc).strip()
        return message[:1000] if message else "live-candidate monitoring payload is unavailable"

    def _now(self) -> datetime:
        if self._now_provider is not None:
            now = self._now_provider()
            if now.tzinfo is None or now.utcoffset() is None:
                return now.replace(tzinfo=timezone.utc)
            return now
        return datetime.now(timezone.utc)


class LiveCandidateMonitoringSnapshotService:
    """Loads read-only monitoring artifacts from disk without runtime control calls."""

    def __init__(self, parser: Optional[LiveCandidateMonitoringParser] = None) -> None:
        self._parser = parser or LiveCandidateMonitoringParser()

    def snapshot_from_fixture_json(self, path: Path) -> LiveCandidateMonitoringSnapshot:
        return self._load_json_file(
            path,
            lambda payload: self._parser.parse_fixture_payload(payload),
            source="fixture",
        )

    def snapshot_from_controlled_json(self, path: Path) -> LiveCandidateMonitoringSnapshot:
        return self._load_json_file(
            path,
            lambda payload: self._parser.parse_controlled_json_payload(payload),
            source="controlled-local-json",
        )

    def snapshot_from_artifact_manifest(self, path: Path) -> LiveCandidateMonitoringSnapshot:
        return self._load_json_file(
            path,
            lambda payload: self._parser.parse_artifact_manifest_payload(payload, path),
            source="artifact",
            source_ref=str(path),
        )

    def _load_json_file(
        self,
        path: Path,
        parser: Callable[[dict[str, Any]], LiveCandidateMonitoringSnapshot],
        source: LiveCandidateMonitoringSourceType,
        source_ref: Optional[str] = None,
    ) -> LiveCandidateMonitoringSnapshot:
        if not path.exists():
            return self._parser.unavailable_snapshot(
                unavailable_reason=f"live-candidate monitoring JSON file does not exist: {path}",
                source=source,
                source_ref=source_ref or str(path),
            )
        if not path.is_file():
            return self._parser.unavailable_snapshot(
                unavailable_reason=f"live-candidate monitoring JSON path is not a file: {path}",
                source=source,
                source_ref=source_ref or str(path),
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except JSONDecodeError:
            return self._parser.unavailable_snapshot(
                unavailable_reason="live-candidate monitoring JSON is not valid JSON",
                source=source,
                source_ref=source_ref or str(path),
            )
        if not isinstance(payload, dict):
            return self._parser.unavailable_snapshot(
                unavailable_reason="live-candidate monitoring JSON root must be an object",
                source=source,
                source_ref=source_ref or str(path),
            )
        return parser(payload)
