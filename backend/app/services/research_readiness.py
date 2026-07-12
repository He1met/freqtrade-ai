from __future__ import annotations

import os
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from app.core.config import Settings, get_settings
from app.core.paths import resolve_repo_path
from app.db.migrations import verify_schema
from app.db.session import create_database_engine
from app.schemas.runtime_contract import RuntimeReadOnlyStatus, RuntimeStatusSummary


class ResearchReadinessService:
    """Read-only checks for the local strategy research chain.

    The service reports presence and integrity evidence only.  It never invokes a
    provider, Freqtrade, an exchange, or any trading control.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        environ: Optional[Mapping[str, str]] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._environ = environ if environ is not None else os.environ
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._which = which or shutil.which

    def build(self) -> RuntimeStatusSummary:
        checks = [
            self._database_check(),
            self._provider_check(),
            self._directory_check("strategy_output", self._settings.strategy_output_dir, ".py"),
            self._binary_check(),
            self._market_data_check(),
            self._artifact_check(),
        ]
        status = self._overall_status(checks)
        reason_field = "blocked_reason" if status == "BLOCKED" else "unavailable_reason"
        reason = next((getattr(check, reason_field) for check in checks if getattr(check, reason_field)), None)
        stale = next((check.stale_reason for check in checks if check.stale_reason), None)
        return RuntimeStatusSummary(
            name="research_readiness",
            status=status,
            source="derived",
            last_updated=self._now_provider(),
            summary={
                "READY": "Research prerequisites have local DB, provider, strategy, binary, data, and artifact evidence.",
                "WARNING": "Research prerequisites have non-blocking local warnings.",
                "STALE": "Research market-data evidence is stale.",
                "UNAVAILABLE": "Research prerequisites are missing local evidence.",
                "BLOCKED": "Research prerequisites are blocked by local safety or integrity evidence.",
            }[status],
            blocked_reason=reason if status == "BLOCKED" else None,
            unavailable_reason=reason if status == "UNAVAILABLE" else None,
            stale_reason=stale if status == "STALE" else None,
        )

    def _database_check(self) -> RuntimeStatusSummary:
        try:
            readiness = verify_schema(create_database_engine(self._settings.database_url))
        except Exception as exc:  # Database drivers/configuration must not make the read-only API fail.
            return self._blocked("research_database", f"database readiness check failed: {exc.__class__.__name__}")
        if readiness.ready:
            return self._ready("research_database", "PostgreSQL schema matches the ORM migration contract.")
        return self._blocked("research_database", "; ".join(readiness.problems) or "database schema is not ready")

    def _provider_check(self) -> RuntimeStatusSummary:
        if str(self._environ.get("DEEPSEEK_API_KEY", "")).strip():
            return self._ready("research_provider", "Provider credential is present in the local environment.")
        return self._unavailable("research_provider", "DEEPSEEK_API_KEY is not present; no provider call was attempted.")

    def _directory_check(self, name: str, configured_path: Path, suffix: str) -> RuntimeStatusSummary:
        path = resolve_repo_path(configured_path)
        if not path.is_dir():
            return self._unavailable(name, f"required directory does not exist: {path}")
        if not any(candidate.is_file() and candidate.suffix == suffix for candidate in path.rglob(f"*{suffix}")):
            return self._unavailable(name, f"no {suffix} evidence exists under: {path}")
        return self._ready(name, f"local evidence exists under: {path}")

    def _binary_check(self) -> RuntimeStatusSummary:
        if self._which("freqtrade"):
            return self._ready("freqtrade_binary", "Freqtrade binary is available on PATH.")
        return self._unavailable("freqtrade_binary", "Freqtrade binary is not available on PATH.")

    def _market_data_check(self) -> RuntimeStatusSummary:
        path = resolve_repo_path(self._settings.market_data_dir)
        if not path.is_dir():
            return self._unavailable("market_data", f"market-data directory does not exist: {path}")
        files = [candidate for candidate in path.rglob("*") if candidate.is_file()]
        if not files:
            return self._unavailable("market_data", f"market-data directory has no files: {path}")
        newest = max(candidate.stat().st_mtime for candidate in files)
        age = self._now_provider() - datetime.fromtimestamp(newest, tz=timezone.utc)
        if age > timedelta(days=7):
            return RuntimeStatusSummary(
                name="market_data",
                status="STALE",
                source="artifact",
                source_ref=str(path),
                summary="Local market-data evidence is older than seven days.",
                stale_reason=f"newest market-data file is {int(age.total_seconds() // 86400)} days old",
            )
        return self._ready("market_data", "Local market-data evidence is fresh.", source="artifact", source_ref=str(path))

    def _artifact_check(self) -> RuntimeStatusSummary:
        path = resolve_repo_path(self._settings.backtest_result_dir)
        if not path.is_dir():
            return self._unavailable("artifact_integrity", f"backtest artifact directory does not exist: {path}")
        artifacts = [candidate for candidate in path.rglob("*.json") if candidate.is_file()]
        if not artifacts:
            return self._unavailable("artifact_integrity", f"no JSON backtest artifacts exist under: {path}")
        for artifact in artifacts:
            try:
                payload = artifact.read_text(encoding="utf-8").strip()
                if not payload:
                    return self._blocked("artifact_integrity", f"backtest artifact is empty: {artifact}")
                json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._blocked("artifact_integrity", f"backtest artifact is not UTF-8 JSON: {artifact}")
        return self._ready("artifact_integrity", "Local backtest artifact files are present and readable.", source="artifact", source_ref=str(path))

    @staticmethod
    def _overall_status(checks: list[RuntimeStatusSummary]) -> RuntimeReadOnlyStatus:
        statuses = {check.status for check in checks}
        if "BLOCKED" in statuses:
            return "BLOCKED"
        if "STALE" in statuses:
            return "STALE"
        if "UNAVAILABLE" in statuses:
            return "UNAVAILABLE"
        if "WARNING" in statuses:
            return "WARNING"
        return "READY"

    @staticmethod
    def _ready(name: str, summary: str, source: str = "derived", source_ref: Optional[str] = None) -> RuntimeStatusSummary:
        return RuntimeStatusSummary(name=name, status="READY", source=source, source_ref=source_ref, summary=summary)

    @staticmethod
    def _blocked(name: str, reason: str) -> RuntimeStatusSummary:
        return RuntimeStatusSummary(name=name, status="BLOCKED", source="derived", summary=reason, blocked_reason=reason)

    @staticmethod
    def _unavailable(name: str, reason: str) -> RuntimeStatusSummary:
        return RuntimeStatusSummary(name=name, status="UNAVAILABLE", source="derived", summary=reason, unavailable_reason=reason)
