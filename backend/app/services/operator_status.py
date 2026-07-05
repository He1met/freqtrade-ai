from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from app.core.config import REPO_ROOT, Settings, get_settings
from app.schemas.dry_run_status import redact_dry_run_status_payload
from app.schemas.operator_status import (
    OperatorArtifactStatus,
    OperatorDiagnosticCheck,
    OperatorEnvPresence,
    OperatorReadinessStatus,
    OperatorRuntimeContractSummary,
    OperatorStatusReport,
)
from app.schemas.runtime_contract import RuntimeReadOnlyStatus
from app.services.runtime_contract import RuntimeReadOnlyContractService


DEFAULT_OPERATOR_ENV_NAMES = (
    "APP_ENV",
    "DATABASE_URL",
    "FREQUI_URL",
    "OKX_DEMO_API_KEY",
    "OKX_DEMO_API_SECRET",
    "OKX_DEMO_API_PASSPHRASE",
    "MIMO_API_KEY",
    "OPENAI_API_KEY",
    "STRATEGY_BLUEPRINT_API_KEY_ENV",
)


class OperatorStatusService:
    """Builds local read-only operator diagnostics without runtime control."""

    def __init__(
        self,
        runtime_contract_service: Optional[RuntimeReadOnlyContractService] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._runtime_contract_service = (
            runtime_contract_service
            or RuntimeReadOnlyContractService(now_provider=now_provider)
        )
        self._now_provider = now_provider
        self._environ = environ if environ is not None else os.environ

    def build_status(
        self,
        repo_root: Optional[Path] = None,
        settings: Optional[Settings] = None,
        dry_run_status_path: Optional[Path] = None,
        dry_run_manifest_path: Optional[Path] = None,
        live_candidate_monitoring_path: Optional[Path] = None,
        live_candidate_monitoring_manifest_path: Optional[Path] = None,
        phase7_smoke_summary_path: Optional[Path] = None,
        env_names: tuple[str, ...] = DEFAULT_OPERATOR_ENV_NAMES,
        required_env_names: tuple[str, ...] = (),
    ) -> OperatorStatusReport:
        repo_root = (repo_root or REPO_ROOT).resolve()
        settings = settings or get_settings()
        runtime_report_dir = repo_root / "reports" / "runtime"
        dry_run_status_path = dry_run_status_path or runtime_report_dir / "dry-run-status.json"
        dry_run_manifest_path = dry_run_manifest_path or runtime_report_dir / "dry-run-manifest.json"
        live_candidate_monitoring_path = (
            live_candidate_monitoring_path
            or runtime_report_dir / "live-candidate-monitoring.json"
        )
        live_candidate_monitoring_manifest_path = (
            live_candidate_monitoring_manifest_path
            or runtime_report_dir / "live-candidate-monitoring-manifest.json"
        )
        phase7_smoke_summary_path = (
            phase7_smoke_summary_path or runtime_report_dir / "phase7-smoke-summary.json"
        )

        generated_at = self._now()
        checks: list[OperatorDiagnosticCheck] = []
        checks.extend(self._repo_checks(repo_root))
        checks.extend(self._config_checks(repo_root))
        checks.extend(self._directory_checks(repo_root, settings, runtime_report_dir))
        checks.extend(self._safety_checks(settings))
        checks.append(self._smoke_check(phase7_smoke_summary_path))

        artifacts = self._artifact_statuses(
            dry_run_status_path=dry_run_status_path,
            dry_run_manifest_path=dry_run_manifest_path,
            live_candidate_monitoring_path=live_candidate_monitoring_path,
            live_candidate_monitoring_manifest_path=live_candidate_monitoring_manifest_path,
            phase7_smoke_summary_path=phase7_smoke_summary_path,
        )

        runtime_contract = self._runtime_contract_service.build_contract(
            dry_run_status_path=dry_run_status_path,
            dry_run_manifest_path=dry_run_manifest_path,
            live_candidate_monitoring_path=live_candidate_monitoring_path,
            live_candidate_monitoring_manifest_path=live_candidate_monitoring_manifest_path,
            phase7_smoke_summary_path=phase7_smoke_summary_path,
        )
        runtime_summary = OperatorRuntimeContractSummary(
            status=runtime_contract.status,
            runtime_readiness_status=runtime_contract.runtime_readiness.status,
            fallback_active=runtime_contract.fallback_status.active,
            smoke_status=runtime_contract.smoke_status.status,
            artifact_count=len(runtime_contract.artifact_links),
            blocked_reasons=runtime_contract.blocked_reasons,
            unavailable_reasons=runtime_contract.unavailable_reasons,
        )
        checks.append(self._runtime_contract_check(runtime_summary))

        env_presence = self._env_presence(env_names, required_env_names)
        checks.extend(self._env_checks(env_presence))

        status = self._overall_status(checks)
        return OperatorStatusReport(
            status=status,
            generated_at=generated_at,
            repo_root=str(repo_root),
            checks=checks,
            artifacts=artifacts,
            env_presence=env_presence,
            runtime_contract=runtime_summary,
            blocked_reasons=self._blocked_reasons(checks, runtime_summary),
            unavailable_reasons=self._unavailable_reasons(checks, runtime_summary),
            warnings=self._warnings(checks),
        )

    def _repo_checks(self, repo_root: Path) -> list[OperatorDiagnosticCheck]:
        checks: list[OperatorDiagnosticCheck] = [
            self._path_check(
                name="repo_root",
                area="repo",
                path=repo_root,
                required=True,
                expected_kind="dir",
                blocked_summary="Repository root is not available.",
                ready_summary="Repository root is available.",
            ),
            self._path_check(
                name="git_metadata",
                area="repo",
                path=repo_root / ".git",
                required=True,
                expected_kind="any",
                blocked_summary="Git metadata is not available.",
                ready_summary="Git metadata is available.",
            ),
        ]
        return checks

    def _config_checks(self, repo_root: Path) -> list[OperatorDiagnosticCheck]:
        config_paths = (
            repo_root / "config" / "app.yaml",
            repo_root / "config" / "exchange.yaml",
            repo_root / "config" / "llm.yaml",
            repo_root / ".env.example",
        )
        return [
            self._path_check(
                name=f"config_{path.name.replace('.', '_')}",
                area="config",
                path=path,
                required=True,
                expected_kind="file",
                blocked_summary=f"Required config file is missing: {path}",
                ready_summary=f"Required config file exists: {path.name}",
            )
            for path in config_paths
        ]

    def _directory_checks(
        self,
        repo_root: Path,
        settings: Settings,
        runtime_report_dir: Path,
    ) -> list[OperatorDiagnosticCheck]:
        required_dirs = {
            "freqtrade_user_data": settings.freqtrade_user_data,
            "strategy_output_dir": settings.strategy_output_dir,
            "market_data_dir": settings.market_data_dir,
            "backtest_result_dir": settings.backtest_result_dir,
            "log_dir": settings.log_dir,
            "tmp_freqtrade_config_dir": settings.tmp_freqtrade_config_dir,
        }
        checks = [
            self._path_check(
                name=name,
                area="artifact",
                path=self._resolve_repo_path(repo_root, path),
                required=True,
                expected_kind="dir",
                blocked_summary=f"Required local directory is missing: {name}",
                ready_summary=f"Required local directory exists: {name}",
            )
            for name, path in required_dirs.items()
        ]
        checks.append(
            self._path_check(
                name="runtime_report_dir",
                area="artifact",
                path=runtime_report_dir,
                required=False,
                expected_kind="dir",
                blocked_summary="Runtime report directory path is invalid.",
                ready_summary="Runtime report directory exists.",
                unavailable_summary="Runtime report directory has not been generated yet.",
            )
        )
        return checks

    def _safety_checks(self, settings: Settings) -> list[OperatorDiagnosticCheck]:
        unsafe_flags: list[str] = []
        if settings.allow_live_trading:
            unsafe_flags.append("allow_live_trading=true")
        if settings.allow_dry_run_trading:
            unsafe_flags.append("allow_dry_run_trading=true")
        if not unsafe_flags:
            return [
                OperatorDiagnosticCheck(
                    name="runtime_control_disabled",
                    area="security",
                    status="READY",
                    source="settings",
                    summary="Runtime control flags are disabled.",
                )
            ]
        reason = "Operator status requires runtime control flags to remain disabled."
        return [
            OperatorDiagnosticCheck(
                name="runtime_control_disabled",
                area="security",
                status="BLOCKED",
                source="settings",
                summary=reason,
                blocked_reason=f"{reason} Unsafe flags: {', '.join(unsafe_flags)}",
            )
        ]

    def _artifact_statuses(
        self,
        dry_run_status_path: Path,
        dry_run_manifest_path: Path,
        live_candidate_monitoring_path: Path,
        live_candidate_monitoring_manifest_path: Path,
        phase7_smoke_summary_path: Path,
    ) -> list[OperatorArtifactStatus]:
        artifacts = (
            ("dry_run_status_json", dry_run_status_path, "filesystem"),
            ("dry_run_manifest", dry_run_manifest_path, "artifact"),
            ("live_candidate_monitoring_json", live_candidate_monitoring_path, "filesystem"),
            (
                "live_candidate_monitoring_manifest",
                live_candidate_monitoring_manifest_path,
                "artifact",
            ),
            ("phase7_smoke_summary", phase7_smoke_summary_path, "artifact"),
        )
        return [
            OperatorArtifactStatus(
                name=name,
                path=str(path),
                source=source,
                exists=path.exists(),
                status="READY" if path.is_file() else "UNAVAILABLE",
            )
            for name, path, source in artifacts
        ]

    def _smoke_check(self, path: Path) -> OperatorDiagnosticCheck:
        if not path.exists():
            reason = f"Phase 7 smoke summary does not exist: {path}"
            return OperatorDiagnosticCheck(
                name="phase7_smoke_summary",
                area="smoke",
                status="UNAVAILABLE",
                source="artifact",
                path=str(path),
                exists=False,
                required=False,
                summary="Phase 7 smoke summary has not been generated.",
                unavailable_reason=reason,
            )
        if not path.is_file():
            reason = f"Phase 7 smoke summary path is not a file: {path}"
            return OperatorDiagnosticCheck(
                name="phase7_smoke_summary",
                area="smoke",
                status="BLOCKED",
                source="artifact",
                path=str(path),
                exists=True,
                required=False,
                summary="Phase 7 smoke summary path is invalid.",
                blocked_reason=reason,
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except JSONDecodeError:
            return OperatorDiagnosticCheck(
                name="phase7_smoke_summary",
                area="smoke",
                status="BLOCKED",
                source="artifact",
                path=str(path),
                exists=True,
                required=False,
                summary="Phase 7 smoke summary is not valid JSON.",
                blocked_reason="Phase 7 smoke summary is not valid JSON",
            )
        if not isinstance(payload, dict):
            return OperatorDiagnosticCheck(
                name="phase7_smoke_summary",
                area="smoke",
                status="BLOCKED",
                source="artifact",
                path=str(path),
                exists=True,
                required=False,
                summary="Phase 7 smoke summary root is not an object.",
                blocked_reason="Phase 7 smoke summary root must be an object",
            )

        sanitized = redact_dry_run_status_payload(payload)
        raw_status = self._optional_text(
            sanitized.get("status"),
            sanitized.get("result"),
            sanitized.get("outcome"),
        )
        status = self._operator_status_from_runtime(self._smoke_runtime_status(raw_status))
        return OperatorDiagnosticCheck(
            name="phase7_smoke_summary",
            area="smoke",
            status=status,
            source="artifact",
            path=str(path),
            exists=True,
            required=False,
            summary=f"Phase 7 smoke summary status is {raw_status or 'UNKNOWN'}.",
            blocked_reason=self._optional_text(
                sanitized.get("blocked_reason"),
                sanitized.get("failed_reason"),
                sanitized.get("error"),
            )
            if status == "BLOCKED"
            else None,
            unavailable_reason=self._optional_text(sanitized.get("unavailable_reason"))
            if status == "UNAVAILABLE"
            else None,
            warnings=self._text_list(sanitized.get("warnings")),
        )

    def _runtime_contract_check(
        self,
        runtime_summary: OperatorRuntimeContractSummary,
    ) -> OperatorDiagnosticCheck:
        status = self._operator_status_from_runtime(runtime_summary.status)
        return OperatorDiagnosticCheck(
            name="runtime_read_only_contract",
            area="runtime_contract",
            status=status,
            source="runtime-contract",
            summary=f"Runtime read-only contract status is {runtime_summary.status}.",
            blocked_reason=(
                runtime_summary.blocked_reasons[0]
                if status == "BLOCKED" and runtime_summary.blocked_reasons
                else None
            ),
            unavailable_reason=(
                runtime_summary.unavailable_reasons[0]
                if status == "UNAVAILABLE" and runtime_summary.unavailable_reasons
                else None
            ),
            warnings=(
                ["Runtime contract is reporting warning-level evidence."]
                if runtime_summary.status == "WARNING"
                else []
            ),
        )

    def _env_presence(
        self,
        env_names: tuple[str, ...],
        required_env_names: tuple[str, ...],
    ) -> list[OperatorEnvPresence]:
        required = set(required_env_names)
        result: list[OperatorEnvPresence] = []
        seen: set[str] = set()
        for name in env_names:
            normalized = name.strip().upper()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(
                OperatorEnvPresence(
                    name=normalized,
                    present=normalized in self._environ,
                    required=normalized in required,
                )
            )
        return result

    def _env_checks(self, env_presence: list[OperatorEnvPresence]) -> list[OperatorDiagnosticCheck]:
        checks: list[OperatorDiagnosticCheck] = []
        for item in env_presence:
            if item.required and not item.present:
                checks.append(
                    OperatorDiagnosticCheck(
                        name=f"env_{item.name}",
                        area="env",
                        status="BLOCKED",
                        source="env",
                        summary=f"Required ENV variable is missing: {item.name}",
                        blocked_reason=f"Required ENV variable is missing: {item.name}",
                    )
                )
        if not checks:
            checks.append(
                OperatorDiagnosticCheck(
                    name="env_presence",
                    area="env",
                    status="READY",
                    source="env",
                    summary="ENV presence was checked without rendering values.",
                )
            )
        return checks

    def _path_check(
        self,
        name: str,
        area: str,
        path: Path,
        required: bool,
        expected_kind: str,
        blocked_summary: str,
        ready_summary: str,
        unavailable_summary: Optional[str] = None,
    ) -> OperatorDiagnosticCheck:
        exists = path.exists()
        kind_matches = exists and (
            expected_kind == "any"
            or (expected_kind == "file" and path.is_file())
            or (expected_kind == "dir" and path.is_dir())
        )
        if kind_matches:
            return OperatorDiagnosticCheck(
                name=name,
                area=area,
                status="READY",
                source="filesystem",
                path=str(path),
                exists=exists,
                required=required,
                summary=ready_summary,
            )
        if required:
            reason = f"{blocked_summary} Path: {path}"
            return OperatorDiagnosticCheck(
                name=name,
                area=area,
                status="BLOCKED",
                source="filesystem",
                path=str(path),
                exists=exists,
                required=required,
                summary=blocked_summary,
                blocked_reason=reason,
            )
        reason = f"{unavailable_summary or blocked_summary} Path: {path}"
        return OperatorDiagnosticCheck(
            name=name,
            area=area,
            status="UNAVAILABLE",
            source="filesystem",
            path=str(path),
            exists=exists,
            required=required,
            summary=unavailable_summary or blocked_summary,
            unavailable_reason=reason,
        )

    def _overall_status(
        self,
        checks: list[OperatorDiagnosticCheck],
    ) -> OperatorReadinessStatus:
        statuses = {check.status for check in checks}
        if "BLOCKED" in statuses:
            return "BLOCKED"
        if "UNAVAILABLE" in statuses:
            return "UNAVAILABLE"
        return "READY"

    def _operator_status_from_runtime(
        self,
        status: RuntimeReadOnlyStatus,
    ) -> OperatorReadinessStatus:
        if status == "BLOCKED":
            return "BLOCKED"
        if status in {"UNAVAILABLE", "STALE"}:
            return "UNAVAILABLE"
        return "READY"

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

    def _blocked_reasons(
        self,
        checks: list[OperatorDiagnosticCheck],
        runtime_summary: OperatorRuntimeContractSummary,
    ) -> list[str]:
        values = [check.blocked_reason for check in checks if check.blocked_reason]
        values.extend(runtime_summary.blocked_reasons)
        return self._dedupe_text(values)

    def _unavailable_reasons(
        self,
        checks: list[OperatorDiagnosticCheck],
        runtime_summary: OperatorRuntimeContractSummary,
    ) -> list[str]:
        values = [check.unavailable_reason for check in checks if check.unavailable_reason]
        values.extend(runtime_summary.unavailable_reasons)
        return self._dedupe_text(values)

    def _warnings(self, checks: list[OperatorDiagnosticCheck]) -> list[str]:
        values = [warning for check in checks for warning in check.warnings]
        return self._dedupe_text(values)

    def _resolve_repo_path(self, repo_root: Path, path: Path) -> Path:
        if path.is_absolute():
            return path
        return (repo_root / path).resolve()

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
