from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.adapters.freqtrade.backtest_runner import (
    FreqtradeBacktestArtifactManifest,
    FreqtradeBacktestRunner,
)
from app.core.config import get_settings
from app.core.paths import resolve_repo_path
from app.repositories import BacktestRepository, StrategyGenerationRunRepository, StrategyRepository
from app.schemas import (
    BacktestArtifactIngestRequest,
    BacktestRunStatusUpdate,
    BacktestTaskStatusUpdate,
    LocalBacktestTriggerRequest,
    OperationEvidence,
    StrategyGenerationApiResponse,
    StrategyGenerationRunRead,
    StrategyRead,
    StrategyVersionRead,
    api_aggregate_source,
    operation_error_evidence,
)
from app.schemas.backtest import BacktestArtifactIngestResponse, LocalBacktestTriggerResponse
from app.schemas.backtest_profile import BacktestProfileV2
from app.schemas.deepseek_backtest_loop import (
    DeepSeekBacktestExecutionRead,
    DeepSeekBacktestLoopRequest,
    DeepSeekBacktestLoopResponse,
)
from app.services.backtest_artifact_ingest import BacktestArtifactIngestService
from app.services.local_backtest_trigger import LocalBacktestTriggerService
from app.services.strategy_generation import StrategyGenerationExecutionError, StrategyGenerationService


@dataclass(frozen=True)
class DeepSeekBacktestLoopExecutionArtifacts:
    manifest_path: Path
    result_path: Path


class DeepSeekBacktestLoopService:
    """Runs the minimal DeepSeek -> local backtest -> ingest -> scoring loop."""

    def __init__(
        self,
        db: Session,
        *,
        generation_service: StrategyGenerationService,
        backtest_trigger_service: Optional[LocalBacktestTriggerService] = None,
        backtest_runner: Optional[FreqtradeBacktestRunner] = None,
        artifact_ingest_service: Optional[BacktestArtifactIngestService] = None,
    ) -> None:
        self.db = db
        self.generation_service = generation_service
        self.backtest_trigger_service = backtest_trigger_service or LocalBacktestTriggerService(db)
        self.backtest_runner = backtest_runner
        if self.backtest_runner is None:
            raise ValueError("DeepSeek backtest loop requires a configured FreqtradeBacktestRunner")
        self.artifact_ingest_service = artifact_ingest_service or BacktestArtifactIngestService(db)
        self.generation_runs = StrategyGenerationRunRepository(db)
        self.strategies = StrategyRepository(db)
        self.backtests = BacktestRepository(db)

    def run(self, payload: DeepSeekBacktestLoopRequest) -> DeepSeekBacktestLoopResponse:
        if self.generation_service.provider.provider_name != "deepseek":
            raise RuntimeError("DeepSeek backtest loop provider boundary is misconfigured")

        if not payload.allow_real_call:
            reason = "Real DeepSeek call requires explicit single-run authorization."
            run_id = self.generation_service.record_blocked_once(
                payload.prompt_summary,
                reason,
                real_call_authorized=False,
            )
            run = self._require_generation_run(run_id)
            return DeepSeekBacktestLoopResponse(
                overall_status="blocked",
                generation_run=StrategyGenerationRunRead.model_validate(run),
                evidence=self._blocked_evidence(
                    reason,
                    ids={"strategy_generation_run_id": run.id},
                    next_action="Retry once with allow_real_call=true after verifying the local operator boundary.",
                ),
            )

        if not self.generation_service.has_provider_credential():
            reason = "Missing configured DeepSeek API key environment variable."
            run_id = self.generation_service.record_blocked_once(
                payload.prompt_summary,
                reason,
                real_call_authorized=True,
            )
            run = self._require_generation_run(run_id)
            return DeepSeekBacktestLoopResponse(
                overall_status="blocked",
                generation_run=StrategyGenerationRunRead.model_validate(run),
                evidence=self._blocked_evidence(
                    reason,
                    ids={"strategy_generation_run_id": run.id},
                    next_action="Set the configured DeepSeek key in ENV and retry once.",
                ),
            )

        try:
            generation_result = self.generation_service.run_once_with_result(
                payload.prompt_summary,
                requested_count=1,
                execution_metadata={
                    "real_call_authorized": True,
                    "real_call_attempted": True,
                    "credential_env_present": True,
                    "credential_values_recorded": False,
                    "loop_mode": "deepseek_backtest_minimal_loop",
                },
            )
        except StrategyGenerationExecutionError as exc:
            run = self._require_generation_run(exc.run_id)
            status = "blocked" if "missing LLM API key environment variable" in str(exc) else "failed"
            evidence = (
                self._blocked_evidence(
                    str(exc),
                    ids={"strategy_generation_run_id": run.id},
                    next_action="Set the configured DeepSeek key in ENV and retry once.",
                )
                if status == "blocked"
                else self._failed_evidence(
                    str(exc),
                    ids={"strategy_generation_run_id": run.id},
                    next_action="Inspect the persisted generation run diagnostics and retry only after correcting the provider failure.",
                )
            )
            return DeepSeekBacktestLoopResponse(
                overall_status=status,
                generation_run=StrategyGenerationRunRead.model_validate(run),
                evidence=evidence,
            )

        generation = self._generation_response(generation_result.run_id, generation_result.version_ids)
        version = self.strategies.get_version(generation_result.version_ids[0])
        if version is None:
            raise RuntimeError("DeepSeek backtest loop lost the generated strategy version")

        backtest = self.backtest_trigger_service.trigger(
            LocalBacktestTriggerRequest(
                strategy_version_id=version.id,
                profile=payload.backtest_profile,
            )
        )
        if backtest is None:
            return DeepSeekBacktestLoopResponse(
                overall_status="failed",
                generation_run=generation.run,
                generation=generation,
                evidence=self._failed_evidence(
                    "Local backtest trigger did not return a response.",
                    ids=self._merge_ids(generation.run.id, generation.strategies, generation.strategy_versions),
                    artifact_refs=self._generation_artifact_refs(generation.strategy_versions),
                    next_action="Inspect the generated strategy version and the local backtest trigger boundary before retrying.",
                ),
            )

        if backtest.preflight_status == "blocked":
            ids = self._merge_ids(generation.run.id, generation.strategies, generation.strategy_versions)
            ids.update(backtest.evidence.ids if backtest.evidence is not None else {})
            artifact_refs = self._generation_artifact_refs(generation.strategy_versions)
            if backtest.evidence is not None:
                artifact_refs.update(backtest.evidence.artifact_refs)
            return DeepSeekBacktestLoopResponse(
                overall_status="blocked",
                generation_run=generation.run,
                generation=generation,
                backtest=backtest,
                evidence=self._blocked_evidence(
                    "; ".join(backtest.blocked_reasons) or "Local backtest preflight is BLOCKED.",
                    ids=ids,
                    artifact_refs=artifact_refs,
                    next_action="Resolve the blocked local Freqtrade/data/config prerequisites and submit a new loop run.",
                ),
            )

        profile = BacktestProfileV2.model_validate(payload.backtest_profile)
        claimed_task = self.backtests.claim_next_pending_task(backtest.run.id)
        if claimed_task is None:
            return DeepSeekBacktestLoopResponse(
                overall_status="failed",
                generation_run=generation.run,
                generation=generation,
                backtest=backtest,
                evidence=self._failed_evidence(
                    "Local backtest task could not be claimed for execution.",
                    ids=self._ids_from_generation_and_backtest(generation, backtest),
                    artifact_refs=self._artifact_refs_from_generation_and_backtest(generation, backtest),
                    next_action="Inspect backtest task state in the database and retry with a fresh loop run.",
                ),
            )

        execution_paths = self._execution_artifacts(claimed_task.backtest_run_id, claimed_task.id)
        manifest = self.backtest_runner.run_backtest_with_artifact_manifest(
            Path(claimed_task.config_path or ""),
            profile.strategy.name,
            result_path=execution_paths.result_path,
            manifest_path=execution_paths.manifest_path,
            timeout_seconds=payload.timeout_seconds,
            datadir=(
                resolve_repo_path(profile.data_source.datadir)
                / profile.data_source.exchange
            ),
            strategy_path=resolve_repo_path(version.file_path).parent,
            userdir=resolve_repo_path(get_settings().freqtrade_user_data),
        )
        execution = self._execution_read(manifest)

        if manifest.status != "SUCCESS":
            self._mark_execution_terminal(claimed_task.id, manifest)
            ids = self._ids_from_generation_and_backtest(generation, backtest)
            artifact_refs = self._artifact_refs_from_generation_and_backtest(generation, backtest)
            artifact_refs.update(execution.data_source.artifact_refs)
            reason = manifest.blocked_reason if manifest.status == "BLOCKED" else manifest.failed_reason
            evidence = (
                self._blocked_evidence(
                    reason or "Local backtest execution is BLOCKED.",
                    ids=ids,
                    artifact_refs=artifact_refs,
                    next_action="Resolve the local backtest blocker and submit a new loop run.",
                )
                if manifest.status == "BLOCKED"
                else self._failed_evidence(
                    reason or "Local backtest execution failed.",
                    ids=ids,
                    artifact_refs=artifact_refs,
                    next_action="Inspect the manifest stdout/stderr and retry only after correcting the Freqtrade execution failure.",
                )
            )
            return DeepSeekBacktestLoopResponse(
                overall_status="blocked" if manifest.status == "BLOCKED" else "failed",
                generation_run=generation.run,
                generation=generation,
                backtest=backtest,
                execution=execution,
                evidence=evidence,
            )

        artifact_ingest = self.artifact_ingest_service.ingest_task_artifact(
            claimed_task.id,
            BacktestArtifactIngestRequest(
                manifest_path=str(manifest.manifest_path),
                result_path=str(manifest.result_path),
                strategy_name=profile.strategy.name,
            ),
        )
        if artifact_ingest is None:
            return DeepSeekBacktestLoopResponse(
                overall_status="failed",
                generation_run=generation.run,
                generation=generation,
                backtest=backtest,
                execution=execution,
                evidence=self._failed_evidence(
                    "Backtest artifact ingest did not return a response.",
                    ids=self._ids_from_generation_and_backtest(generation, backtest),
                    artifact_refs=self._artifact_refs_from_generation_and_backtest(generation, backtest)
                    | execution.data_source.artifact_refs,
                    next_action="Inspect the persisted task, manifest, and result artifact before retrying ingest.",
                ),
            )

        overall_status = {
            "succeeded": "succeeded",
            "blocked": "blocked",
            "failed": "failed",
        }[artifact_ingest.ingest_status]
        evidence = self._final_evidence(generation, backtest, execution, artifact_ingest)
        return DeepSeekBacktestLoopResponse(
            overall_status=overall_status,
            generation_run=generation.run,
            generation=generation,
            backtest=backtest,
            execution=execution,
            artifact_ingest=artifact_ingest,
            evidence=evidence,
        )

    def _generation_response(
        self,
        run_id: int,
        version_ids: Iterable[int],
    ) -> StrategyGenerationApiResponse:
        run = self._require_generation_run(run_id)
        versions = []
        strategies = []
        seen_strategy_ids: set[int] = set()
        for version_id in version_ids:
            version = self.strategies.get_version(version_id)
            if version is None:
                raise RuntimeError(f"DeepSeek backtest loop could not load strategy version {version_id}")
            versions.append(version)
            if version.strategy_id in seen_strategy_ids:
                continue
            strategy = self.strategies.get(version.strategy_id)
            if strategy is None:
                raise RuntimeError(f"DeepSeek backtest loop could not load strategy {version.strategy_id}")
            seen_strategy_ids.add(version.strategy_id)
            strategies.append(strategy)
        return StrategyGenerationApiResponse(
            run=StrategyGenerationRunRead.model_validate(run),
            strategies=[StrategyRead.model_validate(strategy) for strategy in strategies],
            strategy_versions=[StrategyVersionRead.model_validate(version) for version in versions],
        )

    def _require_generation_run(self, run_id: int):
        run = self.generation_runs.get(run_id)
        if run is None:
            raise RuntimeError(f"DeepSeek backtest loop could not load generation run {run_id}")
        return run

    def _execution_artifacts(self, run_id: int, task_id: int) -> DeepSeekBacktestLoopExecutionArtifacts:
        root = resolve_repo_path(get_settings().backtest_result_dir) / f"deepseek-loop-run-{run_id}-task-{task_id}"
        return DeepSeekBacktestLoopExecutionArtifacts(
            manifest_path=root / "artifact-manifest.json",
            result_path=root / "backtest-result.json",
        )

    def _execution_read(self, manifest: FreqtradeBacktestArtifactManifest) -> DeepSeekBacktestExecutionRead:
        status_map = {
            "SUCCESS": "succeeded",
            "FAILED": "failed",
            "BLOCKED": "blocked",
        }
        return DeepSeekBacktestExecutionRead(
            status=status_map[manifest.status],
            manifest_path=str(manifest.manifest_path),
            result_path=str(manifest.result_path),
            command_args=list(manifest.command_args),
            return_code=manifest.return_code,
            blocked_reason=manifest.blocked_reason,
            failed_reason=manifest.failed_reason,
            created_at=datetime.now(timezone.utc),
        )

    def _mark_execution_terminal(
        self,
        task_id: int,
        manifest: FreqtradeBacktestArtifactManifest,
    ) -> None:
        prefix = "BLOCKED" if manifest.status == "BLOCKED" else "FAILED"
        reason = manifest.blocked_reason if manifest.status == "BLOCKED" else manifest.failed_reason
        task = self.backtests.update_task_status(
            task_id,
            BacktestTaskStatusUpdate(
                status=manifest.status.lower(),  # type: ignore[arg-type]
                result_path=str(manifest.result_path),
                error_message=(
                    f"{prefix}: {reason}; manifest_path={manifest.manifest_path}; result_path={manifest.result_path}"
                ),
            ),
        )
        if task is None:
            raise RuntimeError(f"DeepSeek backtest loop lost task {task_id} while updating execution status")
        self._refresh_run_status(task.backtest_run_id)

    def _refresh_run_status(self, run_id: int) -> None:
        tasks = self.backtests.list_tasks(run_id)
        if not tasks:
            return
        statuses = {task.status for task in tasks}
        if "running" in statuses:
            self.backtests.update_run_status(run_id, BacktestRunStatusUpdate(status="running"))
            return
        if "pending" in statuses:
            return
        if "failed" in statuses:
            self.backtests.update_run_status(run_id, BacktestRunStatusUpdate(status="failed"))
            return
        if "blocked" in statuses:
            self.backtests.update_run_status(run_id, BacktestRunStatusUpdate(status="blocked"))
            return
        if statuses == {"cancelled"}:
            self.backtests.update_run_status(run_id, BacktestRunStatusUpdate(status="cancelled"))
            return
        self.backtests.update_run_status(run_id, BacktestRunStatusUpdate(status="succeeded"))

    def _final_evidence(
        self,
        generation: StrategyGenerationApiResponse,
        backtest: LocalBacktestTriggerResponse,
        execution: DeepSeekBacktestExecutionRead,
        artifact_ingest: BacktestArtifactIngestResponse,
    ) -> OperationEvidence:
        ids = self._ids_from_generation_and_backtest(generation, backtest)
        if artifact_ingest.evidence is not None:
            ids.update(artifact_ingest.evidence.ids)
        artifact_refs = self._artifact_refs_from_generation_and_backtest(generation, backtest)
        artifact_refs.update(execution.data_source.artifact_refs)
        if artifact_ingest.evidence is not None:
            artifact_refs.update(artifact_ingest.evidence.artifact_refs)
        source = api_aggregate_source(
            "deepseek_backtest_minimal_loop",
            ids,
            artifact_refs=artifact_refs,
            freshness=generation.run.created_at,
        )
        if artifact_ingest.ingest_status == "succeeded":
            return OperationEvidence(
                status="SUCCESS",
                ids=ids,
                artifact_refs=artifact_refs,
                data_source=source,
                next_action="Refresh generation, strategy, backtest, result, and ranking APIs to reconcile the persisted loop evidence.",
                acceptance_ready=True,
            )
        if artifact_ingest.ingest_status == "blocked":
            return OperationEvidence(
                status="BLOCKED",
                ids=ids,
                artifact_refs=artifact_refs,
                data_source=source,
                blocked_reason=artifact_ingest.reason or "Backtest artifact ingest is BLOCKED.",
                next_action="Resolve the reported artifact blocker and submit a new loop run.",
                acceptance_ready=False,
            )
        return OperationEvidence(
            status="FAILED",
            ids=ids,
            artifact_refs=artifact_refs,
            data_source=source,
            failed_reason=artifact_ingest.reason or "Backtest artifact ingest failed.",
            next_action="Inspect the manifest/result parsing failure and retry only after correcting the artifact or runtime issue.",
            acceptance_ready=False,
        )

    def _merge_ids(self, run_id: int, strategies: list[StrategyRead], versions: list[StrategyVersionRead]) -> dict[str, int]:
        ids = {"strategy_generation_run_id": run_id}
        if strategies:
            ids["strategy_id"] = strategies[0].id
        if versions:
            ids["strategy_version_id"] = versions[0].id
        return ids

    def _generation_artifact_refs(self, versions: list[StrategyVersionRead]) -> dict[str, str]:
        refs: dict[str, str] = {}
        if versions:
            refs["strategy_file_path"] = versions[0].file_path
        return refs

    def _ids_from_generation_and_backtest(
        self,
        generation: StrategyGenerationApiResponse,
        backtest: LocalBacktestTriggerResponse,
    ) -> dict[str, int]:
        ids = self._merge_ids(generation.run.id, generation.strategies, generation.strategy_versions)
        if backtest.evidence is not None:
            ids.update(backtest.evidence.ids)
        return ids

    def _artifact_refs_from_generation_and_backtest(
        self,
        generation: StrategyGenerationApiResponse,
        backtest: LocalBacktestTriggerResponse,
    ) -> dict[str, str]:
        refs = self._generation_artifact_refs(generation.strategy_versions)
        if backtest.evidence is not None:
            refs.update(backtest.evidence.artifact_refs)
        return refs

    def _blocked_evidence(
        self,
        reason: str,
        *,
        ids: dict[str, int],
        next_action: str,
        artifact_refs: Optional[dict[str, str]] = None,
    ) -> OperationEvidence:
        source = api_aggregate_source(
            "deepseek_backtest_minimal_loop",
            ids,
            artifact_refs=artifact_refs or {},
        )
        return operation_error_evidence(
            status="BLOCKED",
            reason=reason,
            next_action=next_action,
            ids=ids,
            artifact_refs=artifact_refs or {},
            data_source=source,
        )

    def _failed_evidence(
        self,
        reason: str,
        *,
        ids: dict[str, int],
        next_action: str,
        artifact_refs: Optional[dict[str, str]] = None,
    ) -> OperationEvidence:
        source = api_aggregate_source(
            "deepseek_backtest_minimal_loop",
            ids,
            artifact_refs=artifact_refs or {},
        )
        return operation_error_evidence(
            status="FAILED",
            reason=reason,
            next_action=next_action,
            ids=ids,
            artifact_refs=artifact_refs or {},
            data_source=source,
        )
