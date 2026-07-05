from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.adapters.freqtrade.exceptions import FreqtradeResultParseError
from app.adapters.freqtrade.result_parser import FreqtradeResultParser
from app.core.config import get_settings
from app.core.paths import resolve_repo_path
from app.models.backtest import BacktestResult, BacktestTask
from app.repositories import BacktestRepository
from app.schemas import (
    BacktestArtifactIngestRequest,
    BacktestArtifactIngestResponse,
    BacktestResultCreate,
    BacktestResultRead,
    BacktestRunRead,
    BacktestRunStatusUpdate,
    BacktestTaskRead,
    BacktestTaskStatusUpdate,
)
from app.schemas.dry_run_status import redact_dry_run_status_payload, redact_secret_text


class BacktestArtifactIngestService:
    """Persists normalized results from existing local backtest artifacts only."""

    def __init__(
        self,
        db: Session,
        parser: Optional[FreqtradeResultParser] = None,
        approved_roots: Optional[list[Path]] = None,
    ) -> None:
        self.repository = BacktestRepository(db)
        self.parser = parser or FreqtradeResultParser()
        self.approved_roots = [
            root.resolve(strict=False)
            for root in (approved_roots or [resolve_repo_path(get_settings().backtest_result_dir)])
        ]
        self.safe_tmp_root = Path("/tmp").resolve(strict=False)

    def ingest_task_artifact(
        self,
        task_id: int,
        payload: BacktestArtifactIngestRequest,
    ) -> Optional[BacktestArtifactIngestResponse]:
        task = self.repository.get_task(task_id)
        if task is None:
            return None

        manifest_path = self._path_from_text(payload.manifest_path)
        if payload.manifest_path is not None and self._contains_secret_value(payload.manifest_path):
            return self._record_blocked(
                task,
                "artifact manifest path contains secret-shaped value",
                manifest_path=None,
                result_path=None,
            )
        if manifest_path is not None and not self._is_approved_artifact_path(manifest_path):
            return self._record_blocked(
                task,
                "artifact manifest path is outside approved local artifact directories",
                manifest_path=None,
                result_path=None,
            )

        result_path = self._path_from_text(payload.result_path)
        if payload.result_path is not None and self._contains_secret_value(payload.result_path):
            return self._record_blocked(
                task,
                "backtest result path contains secret-shaped value",
                manifest_path=manifest_path,
                result_path=None,
            )
        if result_path is not None and not self._is_approved_artifact_path(result_path):
            return self._record_blocked(
                task,
                "backtest result path is outside approved local artifact directories",
                manifest_path=manifest_path,
                result_path=None,
            )

        manifest = None
        if manifest_path is not None:
            manifest, blocked_response = self._read_manifest(task, manifest_path, result_path)
            if blocked_response is not None:
                return blocked_response
            if manifest is not None:
                result_path = self._result_path_from_manifest(manifest, result_path)

        if result_path is None:
            return self._record_blocked(
                task,
                "artifact ingest requires manifest_path or result_path",
                manifest_path=manifest_path,
                result_path=None,
            )
        if self._contains_secret_value(str(result_path)):
            return self._record_blocked(
                task,
                "backtest result path contains secret-shaped value",
                manifest_path=manifest_path,
                result_path=None,
            )
        if not self._is_approved_artifact_path(result_path):
            return self._record_blocked(
                task,
                "backtest result path is outside approved local artifact directories",
                manifest_path=manifest_path,
                result_path=None,
            )

        if manifest is not None:
            manifest_status = str(manifest.get("status") or "").upper()
            if manifest_status == "BLOCKED":
                return self._record_blocked(
                    task,
                    self._manifest_reason(manifest, "blocked_reason", "artifact manifest is BLOCKED"),
                    manifest_path=manifest_path,
                    result_path=result_path,
                )
            if manifest_status == "FAILED":
                return self._record_failed(
                    task,
                    self._manifest_reason(manifest, "failed_reason", "artifact manifest is FAILED"),
                    manifest_path=manifest_path,
                    result_path=result_path,
                )
            if manifest_status != "SUCCESS":
                return self._record_failed(
                    task,
                    "artifact manifest status must be SUCCESS, FAILED, or BLOCKED",
                    manifest_path=manifest_path,
                    result_path=result_path,
                )

        if not result_path.exists() or not result_path.is_file():
            return self._record_blocked(
                task,
                f"backtest result artifact does not exist: {result_path}",
                manifest_path=manifest_path,
                result_path=result_path,
            )

        strategy_name = payload.strategy_name or self._strategy_name_from_manifest(manifest)
        try:
            parsed_result = self.parser.parse_backtest_result(
                result_path,
                strategy_name=strategy_name,
            )
        except FreqtradeResultParseError as exc:
            return self._record_failed(
                task,
                f"backtest result parse failed: {exc}",
                manifest_path=manifest_path,
                result_path=result_path,
            )

        result = self.repository.save_result(
            task.id,
            self._result_with_ingest_metadata(
                parsed_result,
                task,
                manifest=manifest,
                manifest_path=manifest_path,
                result_path=result_path,
                strategy_name=strategy_name,
            ),
        )
        updated_task = self.repository.update_task_status(
            task.id,
            BacktestTaskStatusUpdate(
                status="succeeded",
                result_path=str(result_path),
                error_message=self._artifact_note("SUCCEEDED", None, manifest_path, result_path),
            ),
        )
        self._refresh_run_status(task.backtest_run_id)
        if result is None or updated_task is None:
            raise RuntimeError(f"Backtest task disappeared during artifact ingest: {task.id}")
        return self._response(
            updated_task,
            status="succeeded",
            result=result,
            reason=None,
            manifest_path=manifest_path,
            result_path=result_path,
        )

    def _read_manifest(
        self,
        task: BacktestTask,
        manifest_path: Path,
        result_path: Optional[Path],
    ) -> tuple[Optional[dict[str, Any]], Optional[BacktestArtifactIngestResponse]]:
        if not manifest_path.exists() or not manifest_path.is_file():
            return None, self._record_blocked(
                task,
                f"artifact manifest does not exist: {manifest_path}",
                manifest_path=manifest_path,
                result_path=result_path,
            )
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None, self._record_failed(
                task,
                f"artifact manifest is not valid JSON: {manifest_path}",
                manifest_path=manifest_path,
                result_path=result_path,
            )
        if not isinstance(payload, dict):
            return None, self._record_failed(
                task,
                "artifact manifest root must be an object",
                manifest_path=manifest_path,
                result_path=result_path,
            )
        return payload, None

    def _result_path_from_manifest(
        self,
        manifest: dict[str, Any],
        override: Optional[Path],
    ) -> Optional[Path]:
        if override is not None:
            return override
        value = manifest.get("result_path")
        if isinstance(value, str) and value.strip():
            return self._path_from_text(value)
        return None

    def _result_with_ingest_metadata(
        self,
        parsed_result: BacktestResultCreate,
        task: BacktestTask,
        *,
        manifest: Optional[dict[str, Any]],
        manifest_path: Optional[Path],
        result_path: Path,
        strategy_name: Optional[str],
    ) -> BacktestResultCreate:
        metrics_snapshot = redact_dry_run_status_payload(parsed_result.metrics_snapshot)
        parser_metadata = dict(metrics_snapshot.get("parser_metadata") or {})
        parser_metadata.update(
            {
                "source": "freqtrade_result_parser",
                "ingest_source": "local_backtest_artifact_ingest",
                "backtest_run_id": task.backtest_run_id,
                "backtest_task_id": task.id,
                "strategy_version_id": task.run.strategy_version_id,
                "strategy_name": strategy_name,
            }
        )
        artifact_manifest = {
            "manifest_path": str(manifest_path) if manifest_path is not None else None,
            "result_path": str(result_path),
            "status": manifest.get("status") if manifest is not None else None,
            "manifest_version": manifest.get("manifest_version") if manifest is not None else None,
            "config_path": manifest.get("config_path") if manifest is not None else task.config_path,
            "return_code": manifest.get("return_code") if manifest is not None else None,
            "blocked_reason": manifest.get("blocked_reason") if manifest is not None else None,
            "failed_reason": manifest.get("failed_reason") if manifest is not None else None,
            "command_args": self._safe_command_args(manifest.get("command_args"))
            if manifest is not None
            else None,
            "stdout": manifest.get("stdout") if manifest is not None else None,
            "stderr": manifest.get("stderr") if manifest is not None else None,
        }
        parser_metadata["artifact_manifest"] = redact_dry_run_status_payload(artifact_manifest)
        metrics_snapshot["parser_metadata"] = parser_metadata
        return parsed_result.model_copy(
            update={
                "result_path": str(result_path),
                "metrics_snapshot": metrics_snapshot,
            }
        )

    def _record_blocked(
        self,
        task: BacktestTask,
        reason: str,
        *,
        manifest_path: Optional[Path],
        result_path: Optional[Path],
    ) -> BacktestArtifactIngestResponse:
        return self._record_terminal(
            task,
            status="blocked",
            prefix="BLOCKED",
            reason=reason,
            manifest_path=manifest_path,
            result_path=result_path,
        )

    def _record_failed(
        self,
        task: BacktestTask,
        reason: str,
        *,
        manifest_path: Optional[Path],
        result_path: Optional[Path],
    ) -> BacktestArtifactIngestResponse:
        return self._record_terminal(
            task,
            status="failed",
            prefix="FAILED",
            reason=reason,
            manifest_path=manifest_path,
            result_path=result_path,
        )

    def _record_terminal(
        self,
        task: BacktestTask,
        *,
        status: str,
        prefix: str,
        reason: str,
        manifest_path: Optional[Path],
        result_path: Optional[Path],
    ) -> BacktestArtifactIngestResponse:
        safe_reason = self._safe_text(reason)
        updated_task = self.repository.update_task_status(
            task.id,
            BacktestTaskStatusUpdate(
                status=status,  # type: ignore[arg-type]
                result_path=str(result_path) if result_path is not None else None,
                error_message=self._artifact_note(prefix, safe_reason, manifest_path, result_path),
            ),
        )
        self._refresh_run_status(task.backtest_run_id)
        if updated_task is None:
            raise RuntimeError(f"Backtest task disappeared during artifact ingest: {task.id}")
        return self._response(
            updated_task,
            status=status,
            result=None,
            reason=f"{prefix}: {safe_reason}",
            manifest_path=manifest_path,
            result_path=result_path,
        )

    def _refresh_run_status(self, run_id: int) -> None:
        tasks = self.repository.list_tasks(run_id)
        if not tasks:
            return
        statuses = {task.status for task in tasks}
        if "running" in statuses:
            self.repository.update_run_status(run_id, BacktestRunStatusUpdate(status="running"))
            return
        if "pending" in statuses:
            return
        if "failed" in statuses:
            self.repository.update_run_status(run_id, BacktestRunStatusUpdate(status="failed"))
            return
        if "blocked" in statuses:
            self.repository.update_run_status(run_id, BacktestRunStatusUpdate(status="blocked"))
            return
        if statuses == {"cancelled"}:
            self.repository.update_run_status(run_id, BacktestRunStatusUpdate(status="cancelled"))
            return
        self.repository.update_run_status(run_id, BacktestRunStatusUpdate(status="succeeded"))

    def _response(
        self,
        task: BacktestTask,
        *,
        status: str,
        result: Optional[BacktestResult],
        reason: Optional[str],
        manifest_path: Optional[Path],
        result_path: Optional[Path],
    ) -> BacktestArtifactIngestResponse:
        run = self.repository.get_run(task.backtest_run_id)
        if run is None:
            raise RuntimeError(f"Backtest run disappeared during artifact ingest: {task.backtest_run_id}")
        return BacktestArtifactIngestResponse(
            run=BacktestRunRead.model_validate(run),
            task=BacktestTaskRead.model_validate(task),
            result=BacktestResultRead.model_validate(result) if result is not None else None,
            ingest_status=status,  # type: ignore[arg-type]
            reason=reason,
            manifest_path=str(manifest_path) if manifest_path is not None else None,
            result_path=str(result_path) if result_path is not None else None,
        )

    def _artifact_note(
        self,
        prefix: str,
        reason: Optional[str],
        manifest_path: Optional[Path],
        result_path: Optional[Path],
    ) -> str:
        parts = [prefix]
        if reason:
            parts.append(reason)
        if manifest_path is not None:
            parts.append(f"manifest_path={manifest_path}")
        if result_path is not None:
            parts.append(f"result_path={result_path}")
        return self._safe_text("; ".join(parts))

    def _manifest_reason(self, manifest: dict[str, Any], key: str, fallback: str) -> str:
        value = manifest.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return fallback

    def _strategy_name_from_manifest(self, manifest: Optional[dict[str, Any]]) -> Optional[str]:
        if manifest is None:
            return None
        value = manifest.get("strategy_name")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _path_from_text(self, value: Optional[str]) -> Optional[Path]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        candidate = Path(stripped)
        if candidate.is_absolute():
            return candidate
        return resolve_repo_path(candidate)

    def _contains_secret_value(self, value: str) -> bool:
        return redact_secret_text(value) != value

    def _safe_text(self, value: str, max_length: int = 1000) -> str:
        return redact_secret_text(value).strip()[:max_length]

    def _is_approved_artifact_path(self, path: Path) -> bool:
        resolved = path.resolve(strict=False)
        if any(_is_relative_to(resolved, root) for root in self.approved_roots):
            return True
        if _is_relative_to(resolved, self.safe_tmp_root):
            relative = resolved.relative_to(self.safe_tmp_root)
            return bool(relative.parts) and relative.parts[0].startswith("freqtrade-ai-")
        return False

    def _safe_command_args(self, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        sanitized = []
        redact_next = False
        for item in value:
            text = str(item)
            normalized = text.lower().replace("-", "_")
            if redact_next:
                sanitized.append("[REDACTED]")
                redact_next = False
                continue
            sanitized.append(redact_secret_text(text))
            if normalized in {
                "__api_key",
                "__api_secret",
                "__password",
                "__passphrase",
                "__secret",
                "__token",
            }:
                redact_next = True
        return sanitized


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
