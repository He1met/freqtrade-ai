from __future__ import annotations

import json
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import ValidationError

from app.schemas.dry_run_status import (
    DryRunBalanceSummary,
    DryRunEvent,
    DryRunOpenTradesSummary,
    DryRunSnapshotStatus,
    DryRunStatusSnapshot,
    redact_dry_run_status_payload,
)


class DryRunStatusParser:
    """Parses read-only dry-run status payloads into stable DTOs."""

    def parse_fixture_payload(self, payload: dict[str, Any]) -> DryRunStatusSnapshot:
        return self.parse_status_payload(payload, source="fixture")

    def parse_controlled_json_payload(self, payload: dict[str, Any]) -> DryRunStatusSnapshot:
        return self.parse_status_payload(payload, source="controlled-local-json")

    def parse_status_payload(
        self,
        payload: dict[str, Any],
        source: str = "status-json",
        artifact_manifest_path: Optional[Path] = None,
    ) -> DryRunStatusSnapshot:
        try:
            sanitized = self._sanitize_mapping(payload)
            normalized = self._normalize_status_payload(
                sanitized,
                source=source,
                artifact_manifest_path=artifact_manifest_path,
            )
            return self._validate_snapshot(normalized, source=source)
        except Exception as exc:
            return self.failed_snapshot(
                failed_reason=self._safe_failure_reason(
                    exc,
                    fallback="invalid dry-run status payload",
                ),
                artifact_manifest_path=artifact_manifest_path,
            )

    def parse_artifact_manifest_payload(
        self,
        payload: dict[str, Any],
        artifact_manifest_path: Path,
    ) -> DryRunStatusSnapshot:
        try:
            sanitized = self._sanitize_mapping(payload)
            status_snapshots = sanitized.get("status_snapshots")
            if not status_snapshots:
                return self._empty_manifest_snapshot(sanitized, artifact_manifest_path)
            if not isinstance(status_snapshots, list):
                return self.failed_snapshot(
                    failed_reason="dry-run artifact manifest status_snapshots must be a list",
                    artifact_manifest_path=artifact_manifest_path,
                )

            latest = status_snapshots[-1]
            if not isinstance(latest, dict):
                return self.failed_snapshot(
                    failed_reason="dry-run artifact manifest latest status snapshot must be an object",
                    artifact_manifest_path=artifact_manifest_path,
                )

            merged_payload = self._manifest_base_payload(sanitized)
            merged_payload.update(latest)
            merged_payload.setdefault("status", sanitized.get("status"))
            for reason_key in ("blocked_reason", "failed_reason", "skipped_reason"):
                if reason_key not in merged_payload:
                    merged_payload[reason_key] = sanitized.get(reason_key)
            merged_payload["artifact_manifest_path"] = str(artifact_manifest_path)

            normalized = self._normalize_status_payload(
                merged_payload,
                source="artifact-manifest",
                artifact_manifest_path=artifact_manifest_path,
            )
            return self._validate_snapshot(normalized, source="artifact-manifest")
        except Exception as exc:
            return self.failed_snapshot(
                failed_reason=self._safe_failure_reason(
                    exc,
                    fallback="invalid dry-run artifact manifest payload",
                ),
                artifact_manifest_path=artifact_manifest_path,
            )

    def blocked_snapshot(
        self,
        blocked_reason: str,
        artifact_manifest_path: Optional[Path] = None,
    ) -> DryRunStatusSnapshot:
        return DryRunStatusSnapshot(
            status="BLOCKED",
            dry_run=None,
            balance_summary=DryRunBalanceSummary(),
            open_trades_summary=DryRunOpenTradesSummary(),
            recent_events=[
                self._event(
                    event_type="status_snapshot_blocked",
                    severity="WARNING",
                    message=blocked_reason,
                    source="dry-run-status-service",
                )
            ],
            blocked_reason=blocked_reason,
            last_updated=self._now(),
            artifact_manifest_path=str(artifact_manifest_path) if artifact_manifest_path else None,
        )

    def failed_snapshot(
        self,
        failed_reason: str,
        artifact_manifest_path: Optional[Path] = None,
    ) -> DryRunStatusSnapshot:
        return DryRunStatusSnapshot(
            status="FAILED",
            dry_run=None,
            balance_summary=DryRunBalanceSummary(),
            open_trades_summary=DryRunOpenTradesSummary(),
            recent_events=[
                self._event(
                    event_type="status_snapshot_failed",
                    severity="ERROR",
                    message=failed_reason,
                    source="dry-run-status-service",
                )
            ],
            failed_reason=failed_reason,
            last_updated=self._now(),
            artifact_manifest_path=str(artifact_manifest_path) if artifact_manifest_path else None,
        )

    def _normalize_status_payload(
        self,
        payload: dict[str, Any],
        source: str,
        artifact_manifest_path: Optional[Path],
    ) -> dict[str, Any]:
        snapshot_payload = self._nested_snapshot_payload(payload)
        profile_snapshot = self._mapping(payload.get("profile_snapshot"))
        profile_snapshot = profile_snapshot or self._mapping(snapshot_payload.get("profile_snapshot"))
        strategy_payload = self._mapping(snapshot_payload.get("strategy")) or self._mapping(
            profile_snapshot.get("strategy")
        )
        exchange_payload = self._mapping(snapshot_payload.get("exchange")) or self._mapping(
            profile_snapshot.get("exchange")
        )
        safety_payload = self._mapping(snapshot_payload.get("safety")) or self._mapping(
            profile_snapshot.get("safety")
        )
        config_payload = self._mapping(snapshot_payload.get("config"))

        status = self._normalize_status(
            snapshot_payload.get("status")
            or snapshot_payload.get("state")
            or snapshot_payload.get("bot_state")
            or payload.get("status")
            or "BLOCKED"
        )
        dry_run = self._optional_bool(
            snapshot_payload.get("dry_run"),
            safety_payload.get("dry_run"),
            config_payload.get("dry_run"),
            profile_snapshot.get("dry_run"),
        )

        balance_summary = self._balance_summary(
            snapshot_payload.get("balance_summary")
            or snapshot_payload.get("balance")
            or snapshot_payload.get("balances")
        )
        open_trades_summary = self._open_trades_summary(
            snapshot_payload.get("open_trades_summary")
            or snapshot_payload.get("open_trades")
            or snapshot_payload.get("trades")
        )
        recent_events = self._events(
            snapshot_payload.get("recent_events") or snapshot_payload.get("events"),
            source=source,
        )
        last_updated = self._last_updated(snapshot_payload, recent_events)

        return {
            "status": status,
            "profile_name": self._optional_text(
                snapshot_payload.get("profile_name"),
                profile_snapshot.get("name"),
                profile_snapshot.get("profile_name"),
            ),
            "strategy_version_id": self._optional_int(
                snapshot_payload.get("strategy_version_id"),
                strategy_payload.get("version_id"),
            ),
            "strategy_name": self._optional_text(
                snapshot_payload.get("strategy_name"),
                snapshot_payload.get("strategy") if isinstance(snapshot_payload.get("strategy"), str) else None,
                strategy_payload.get("name"),
            ),
            "exchange": self._optional_text(
                snapshot_payload.get("exchange") if isinstance(snapshot_payload.get("exchange"), str) else None,
                exchange_payload.get("name"),
            ),
            "pair": self._optional_text(snapshot_payload.get("pair"), self._first_pair(config_payload)),
            "timeframe": self._optional_text(
                snapshot_payload.get("timeframe"),
                config_payload.get("timeframe"),
            ),
            "dry_run": dry_run,
            "balance_summary": balance_summary,
            "open_trades_summary": open_trades_summary,
            "recent_events": recent_events,
            "blocked_reason": self._optional_text(snapshot_payload.get("blocked_reason")),
            "failed_reason": self._optional_text(snapshot_payload.get("failed_reason")),
            "skipped_reason": self._optional_text(snapshot_payload.get("skipped_reason")),
            "last_updated": last_updated,
            "artifact_manifest_path": self._optional_text(
                snapshot_payload.get("artifact_manifest_path"),
                str(artifact_manifest_path) if artifact_manifest_path else None,
            ),
        }

    def _validate_snapshot(
        self,
        normalized: dict[str, Any],
        source: str,
    ) -> DryRunStatusSnapshot:
        snapshot = DryRunStatusSnapshot.model_validate(normalized)
        if snapshot.dry_run is False:
            return self.failed_snapshot(
                failed_reason="dry-run status payload reported dry_run=false",
                artifact_manifest_path=Path(snapshot.artifact_manifest_path)
                if snapshot.artifact_manifest_path
                else None,
            )
        if snapshot.status in ("SUCCESS", "RUNNING") and snapshot.dry_run is not True:
            return self.failed_snapshot(
                failed_reason=(
                    f"{source} reported {snapshot.status} without an explicit dry_run=true flag"
                ),
                artifact_manifest_path=Path(snapshot.artifact_manifest_path)
                if snapshot.artifact_manifest_path
                else None,
            )
        return snapshot

    def _empty_manifest_snapshot(
        self,
        manifest: dict[str, Any],
        artifact_manifest_path: Path,
    ) -> DryRunStatusSnapshot:
        base_payload = self._manifest_base_payload(manifest)
        manifest_status = self._normalize_status(manifest.get("status") or "SKIPPED")
        if manifest_status not in ("BLOCKED", "FAILED", "SKIPPED"):
            manifest_status = "SKIPPED"
        reason_key = {
            "BLOCKED": "blocked_reason",
            "FAILED": "failed_reason",
            "SKIPPED": "skipped_reason",
        }[manifest_status]
        base_payload["status"] = manifest_status
        base_payload[reason_key] = (
            manifest.get(reason_key)
            or "dry-run artifact manifest does not contain status_snapshots"
        )
        base_payload["artifact_manifest_path"] = str(artifact_manifest_path)
        base_payload["recent_events"] = [
            {
                "timestamp": self._now(),
                "event_type": "status_snapshots_empty",
                "severity": "WARNING",
                "message": "dry-run artifact manifest does not contain status_snapshots",
                "source": "artifact-manifest",
                "details": {"manifest_status": manifest.get("status")},
            }
        ]
        normalized = self._normalize_status_payload(
            base_payload,
            source="artifact-manifest",
            artifact_manifest_path=artifact_manifest_path,
        )
        return self._validate_snapshot(normalized, source="artifact-manifest")

    def _manifest_base_payload(self, manifest: dict[str, Any]) -> dict[str, Any]:
        profile_snapshot = self._mapping(manifest.get("profile_snapshot"))
        return {
            "status": manifest.get("status"),
            "profile_name": manifest.get("profile_name") or profile_snapshot.get("name"),
            "strategy_version_id": manifest.get("strategy_version_id"),
            "strategy_name": manifest.get("strategy_name"),
            "exchange": manifest.get("exchange"),
            "pair": manifest.get("pair"),
            "timeframe": manifest.get("timeframe"),
            "dry_run": self._optional_bool(
                manifest.get("dry_run"),
                self._mapping(profile_snapshot.get("safety")).get("dry_run"),
            ),
            "profile_snapshot": profile_snapshot,
            "blocked_reason": manifest.get("blocked_reason"),
            "failed_reason": manifest.get("failed_reason"),
            "skipped_reason": manifest.get("skipped_reason"),
            "artifact_manifest_path": manifest.get("manifest_path"),
            "last_updated": manifest.get("last_updated"),
        }

    def _sanitize_mapping(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("dry-run status payload must be a JSON object")
        sanitized = redact_dry_run_status_payload(payload)
        if not isinstance(sanitized, dict):
            raise ValueError("dry-run status payload must be a JSON object")
        return sanitized

    def _nested_snapshot_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        for key in ("snapshot", "status_snapshot", "dry_run_status"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                merged = {key: value for key, value in payload.items() if key != "status_snapshots"}
                merged.update(nested)
                return merged
        return payload

    def _balance_summary(self, payload: Any) -> DryRunBalanceSummary:
        if not payload:
            return DryRunBalanceSummary()
        if isinstance(payload, list):
            return self._balance_summary_from_list(payload)
        if not isinstance(payload, dict):
            return DryRunBalanceSummary()
        if "total" not in payload and len(payload) == 1:
            nested = next(iter(payload.values()))
            if isinstance(nested, dict):
                payload = {"currency": next(iter(payload.keys())), **nested}
        return DryRunBalanceSummary.model_validate(
            {
                "currency": self._optional_text(payload.get("currency"), payload.get("stake_currency")),
                "total": self._optional_float(payload.get("total"), payload.get("total_balance")),
                "free": self._optional_float(payload.get("free"), payload.get("free_balance")),
                "used": self._optional_float(payload.get("used"), payload.get("used_balance")),
                "realized_profit": self._optional_float(
                    payload.get("realized_profit"),
                    payload.get("profit_total"),
                ),
                "unrealized_profit": self._optional_float(
                    payload.get("unrealized_profit"),
                    payload.get("profit_abs"),
                ),
            }
        )

    def _balance_summary_from_list(self, payload: list[Any]) -> DryRunBalanceSummary:
        first_balance = next((item for item in payload if isinstance(item, dict)), None)
        if first_balance is None:
            return DryRunBalanceSummary()
        return self._balance_summary(first_balance)

    def _open_trades_summary(self, payload: Any) -> DryRunOpenTradesSummary:
        if not payload:
            return DryRunOpenTradesSummary()
        if isinstance(payload, dict) and "total_open_trades" in payload:
            return DryRunOpenTradesSummary.model_validate(
                {
                    "total_open_trades": payload.get("total_open_trades") or 0,
                    "pair_count": payload.get("pair_count") or len(payload.get("pairs") or []),
                    "pairs": list(payload.get("pairs") or []),
                    "total_stake_amount": self._optional_float(payload.get("total_stake_amount")),
                    "total_profit_abs": self._optional_float(payload.get("total_profit_abs")),
                    "total_profit_pct": self._optional_float(payload.get("total_profit_pct")),
                }
            )
        trades = payload.get("trades") if isinstance(payload, dict) else payload
        if not isinstance(trades, list):
            return DryRunOpenTradesSummary()

        open_trades = [
            item
            for item in trades
            if isinstance(item, dict) and item.get("is_open", item.get("open", True)) is not False
        ]
        pairs = sorted(
            {
                str(item.get("pair"))
                for item in open_trades
                if isinstance(item.get("pair"), str) and item.get("pair")
            }
        )
        return DryRunOpenTradesSummary(
            total_open_trades=len(open_trades),
            pair_count=len(pairs),
            pairs=pairs,
            total_stake_amount=self._sum_optional_float(open_trades, "stake_amount", "stake"),
            total_profit_abs=self._sum_optional_float(open_trades, "profit_abs", "profit_total_abs"),
            total_profit_pct=self._sum_optional_float(open_trades, "profit_pct", "profit_ratio"),
        )

    def _events(self, payload: Any, source: str) -> list[DryRunEvent]:
        if not payload:
            return []
        raw_events = payload if isinstance(payload, list) else [payload]
        events: list[DryRunEvent] = []
        for item in raw_events[:50]:
            if isinstance(item, str):
                events.append(
                    self._event(
                        event_type="message",
                        severity="INFO",
                        message=item,
                        source=source,
                    )
                )
                continue
            if not isinstance(item, dict):
                continue
            events.append(
                self._event(
                    event_type=self._optional_text(
                        item.get("event_type"),
                        item.get("type"),
                        "status_event",
                    )
                    or "status_event",
                    severity=self._normalize_severity(item.get("severity") or item.get("level")),
                    message=self._optional_text(item.get("message"), item.get("reason")) or "status event",
                    source=self._optional_text(item.get("source"), source) or source,
                    timestamp=self._optional_datetime(item.get("timestamp"), item.get("time")),
                    details=self._mapping(item.get("details")),
                )
            )
        return events

    def _event(
        self,
        event_type: str,
        severity: str,
        message: str,
        source: str,
        timestamp: Optional[datetime] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> DryRunEvent:
        return DryRunEvent(
            timestamp=timestamp or self._now(),
            event_type=event_type,
            severity=severity,
            message=message,
            source=source,
            details=details or {},
        )

    def _last_updated(
        self,
        payload: dict[str, Any],
        events: list[DryRunEvent],
    ) -> datetime:
        explicit = self._optional_datetime(
            payload.get("last_updated"),
            payload.get("updated_at"),
            payload.get("timestamp"),
        )
        if explicit is not None:
            return explicit
        if events:
            return max(event.timestamp for event in events)
        return self._now()

    def _normalize_status(self, value: Any) -> DryRunSnapshotStatus:
        if not isinstance(value, str) or not value.strip():
            return "BLOCKED"
        normalized = value.strip().upper().replace("-", "_")
        aliases: dict[str, DryRunSnapshotStatus] = {
            "OK": "SUCCESS",
            "SUCCEEDED": "SUCCESS",
            "SUCCESSFUL": "SUCCESS",
            "STARTED": "RUNNING",
            "ACTIVE": "RUNNING",
            "OPEN": "RUNNING",
            "ERROR": "FAILED",
            "FAILURE": "FAILED",
            "EMPTY": "SKIPPED",
            "NO_DATA": "SKIPPED",
        }
        normalized = aliases.get(normalized, normalized)
        allowed = {"SUCCESS", "FAILED", "BLOCKED", "SKIPPED", "RUNNING", "STOPPED"}
        if normalized not in allowed:
            raise ValueError("dry-run status payload contains unsupported status")
        return normalized  # type: ignore[return-value]

    def _normalize_severity(self, value: Any) -> str:
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

    def _first_pair(self, payload: dict[str, Any]) -> Optional[str]:
        exchange = self._mapping(payload.get("exchange"))
        whitelist = exchange.get("pair_whitelist")
        if isinstance(whitelist, list) and whitelist and isinstance(whitelist[0], str):
            return whitelist[0]
        return None

    def _mapping(self, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _optional_text(self, *values: Any) -> Optional[str]:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _optional_int(self, *values: Any) -> Optional[int]:
        for value in values:
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
        return None

    def _optional_float(self, *values: Any) -> Optional[float]:
        for value in values:
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str) and value.strip():
                try:
                    return float(value)
                except ValueError:
                    continue
        return None

    def _optional_bool(self, *values: Any) -> Optional[bool]:
        for value in values:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
                return value.strip().lower() == "true"
        return None

    def _optional_datetime(self, *values: Any) -> Optional[datetime]:
        for value in values:
            if isinstance(value, datetime):
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

    def _sum_optional_float(self, rows: list[dict[str, Any]], *keys: str) -> Optional[float]:
        total = 0.0
        found = False
        for row in rows:
            value = self._optional_float(*(row.get(key) for key in keys))
            if value is None:
                continue
            total += value
            found = True
        return total if found else None

    def _safe_failure_reason(self, exc: Exception, fallback: str) -> str:
        if isinstance(exc, ValidationError):
            return fallback
        message = str(exc).strip()
        if not message:
            return fallback
        return str(redact_dry_run_status_payload(message))[:1000]

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)


class DryRunStatusSnapshotService:
    """Loads status artifacts from disk and returns fail-closed snapshots."""

    def __init__(self, parser: Optional[DryRunStatusParser] = None) -> None:
        self._parser = parser or DryRunStatusParser()

    def snapshot_from_fixture_json(self, path: Path) -> DryRunStatusSnapshot:
        return self._load_json_file(
            path,
            lambda payload: self._parser.parse_fixture_payload(payload),
        )

    def snapshot_from_controlled_json(self, path: Path) -> DryRunStatusSnapshot:
        return self._load_json_file(
            path,
            lambda payload: self._parser.parse_controlled_json_payload(payload),
        )

    def snapshot_from_artifact_manifest(self, path: Path) -> DryRunStatusSnapshot:
        return self._load_json_file(
            path,
            lambda payload: self._parser.parse_artifact_manifest_payload(payload, path),
            artifact_manifest_path=path,
        )

    def _load_json_file(
        self,
        path: Path,
        parser: Callable[[dict[str, Any]], DryRunStatusSnapshot],
        artifact_manifest_path: Optional[Path] = None,
    ) -> DryRunStatusSnapshot:
        if not path.exists():
            return self._parser.blocked_snapshot(
                blocked_reason=f"dry-run status JSON file does not exist: {path}",
                artifact_manifest_path=artifact_manifest_path,
            )
        if not path.is_file():
            return self._parser.blocked_snapshot(
                blocked_reason=f"dry-run status JSON path is not a file: {path}",
                artifact_manifest_path=artifact_manifest_path,
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except JSONDecodeError:
            return self._parser.failed_snapshot(
                failed_reason="dry-run status JSON is not valid JSON",
                artifact_manifest_path=artifact_manifest_path,
            )
        if not isinstance(payload, dict):
            return self._parser.failed_snapshot(
                failed_reason="dry-run status JSON root must be an object",
                artifact_manifest_path=artifact_manifest_path,
            )
        return parser(payload)
