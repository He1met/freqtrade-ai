from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.backtest import BacktestResult, BacktestRun, BacktestTask
from app.models.execution_lineage import (
    LOCAL_DRY_RUN_SCOPE_ID,
    ResearchJobAttempt,
)
from app.models.full_chain import (
    FullChainRun,
    FullChainStageRun,
    StrategyCandidateApproval,
)
from app.models.research_job import ResearchJob
from app.models.strategy import Strategy, StrategyVersion
from app.models.strategy_generation_run import StrategyGenerationRun
from app.models.strategy_score import StrategyScore
from app.repositories.full_chain import FullChainBlocked, FullChainRepository
from app.repositories.research_jobs import ResearchJobRepository
from app.schemas.deepseek_backtest_loop import DeepSeekBacktestLoopResponse
from app.schemas.dry_run_status import redact_secret_text

LINK_KEYS = (
    "strategy_generation_run_id",
    "strategy_id",
    "strategy_version_id",
    "backtest_run_id",
    "backtest_task_id",
    "backtest_result_id",
    "strategy_score_id",
)


class ResearchFullChainBlocked(ValueError):
    """A real research result could not safely advance to Demo deployment."""

    def __init__(self, reason: str, *, links: Optional[dict[str, int]] = None):
        super().__init__(redact_secret_text(reason)[:2000])
        self.links = links or {}


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ResearchFullChainOrchestrator:
    """Bind one real ResearchJob result to the existing durable full chain."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.chains = FullChainRepository(db)

    def recover_one_stale(self) -> Optional[int]:
        """Requeue one stale attempt only when the next action is non-ambiguous."""

        jobs = ResearchJobRepository(self.db)
        candidates = list(
            self.db.scalars(
                select(ResearchJob)
                .where(
                    ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                    ResearchJob.status == "STALE",
                    ResearchJob.stage == "LEASE_EXPIRED",
                )
                .order_by(ResearchJob.id)
            )
        )
        for job in candidates:
            chain = self._chain(job.id)
            generation_stage = self._stage(chain, "GENERATION") if chain else None
            links = self._job_links(job)
            if (
                job.provider_attempted_at is None
                and job.provider_completed_at is None
                and not links
                and (
                    chain is None
                    or (
                        chain.status in {"RUNNING", "STALE"}
                        and (
                            generation_stage is None
                            or generation_stage.status == "PREPARED"
                        )
                    )
                )
            ):
                recovered = jobs.prepare_stale_recovery(
                    job.id,
                    recovery_stage="GENERATION_RETRY",
                    commit=False,
                )
                if recovered is not None:
                    self._resume_recoverable_chain(chain)
                    self.db.commit()
                    return recovered.id
                self._block_unrecoverable_stale(
                    job,
                    chain,
                    "safe generation recovery guard rejected the stale job",
                )
                continue
            if (
                job.provider_attempted_at is not None
                and job.provider_completed_at is None
            ):
                # The external outcome is ambiguous. It must remain isolated as
                # STALE and may never be replayed automatically.
                continue
            if (
                job.provider_attempted_at is not None
                and job.provider_completed_at is not None
                and len(links) == len(LINK_KEYS)
                and chain is not None
                and chain.status in {"RUNNING", "STALE"}
            ):
                try:
                    self._load_and_validate(job, links)
                except ResearchFullChainBlocked as exc:
                    self._block_unrecoverable_stale(job, chain, str(exc))
                    continue
                recovered = jobs.prepare_stale_recovery(
                    job.id,
                    recovery_stage="PERSISTED_RESULT_RECOVERY",
                    commit=False,
                )
                if recovered is not None:
                    self._resume_recoverable_chain(chain)
                    self.db.commit()
                    return recovered.id
                self._block_unrecoverable_stale(
                    job,
                    chain,
                    "persisted-result recovery guard rejected the stale job",
                )
                continue
            self._block_unrecoverable_stale(
                job,
                chain,
                "stale research job has inconsistent provider or lineage state",
            )
        return None

    def terminalize_owned(
        self,
        job_id: int,
        lease_token: str,
        *,
        status: str,
        stage: str,
        reason: str,
        links: Optional[dict[str, int]] = None,
        evidence_snapshot: Optional[dict] = None,
        provider_completed: bool,
    ) -> bool:
        """Atomically close Job, Attempt, FullChain and its active checkpoint."""

        if status not in {"FAILED", "BLOCKED", "CANCELLED", "STALE"}:
            raise ValueError("invalid research terminal status")
        safe_reason = redact_secret_text(reason)[:2000]
        current_time = datetime.now(timezone.utc)
        job = ResearchJobRepository(self.db).get(job_id)
        if (
            job is None
            or job.status != "RUNNING"
            or job.lease_token != lease_token
            or job.lease_expires_at is None
            or _as_utc(job.lease_expires_at) <= current_time
        ):
            self.db.rollback()
            return False
        chain = self._chain(job_id)
        try:
            if chain is not None:
                checkpoint = self.db.scalar(
                    select(FullChainStageRun)
                    .where(
                        FullChainStageRun.full_chain_run_id == chain.id,
                        FullChainStageRun.status == "PREPARED",
                    )
                    .order_by(FullChainStageRun.id.desc())
                    .limit(1)
                )
                if checkpoint is not None:
                    self.chains.fail_stage(
                        chain.id,
                        checkpoint.stage,
                        lease_token,
                        status=status,
                        error_code=f"RESEARCH_{status}",
                        error_message=safe_reason,
                        now=current_time,
                        commit=False,
                        allow_cancel_requested=status == "CANCELLED",
                    )
                elif chain.status not in {
                    "SUCCESS",
                    "FAILED",
                    "BLOCKED",
                    "CANCELLED",
                    "STALE",
                }:
                    chain.status = status
                    chain.terminal_reason = safe_reason
                    chain.completed_at = current_time
            safe_evidence = {
                **(evidence_snapshot or {}),
                "status": status,
                "acceptance_ready": False,
                "failed_reason": safe_reason,
            }
            if chain is not None:
                safe_evidence["full_chain_run_id"] = chain.id
                safe_evidence["full_chain_status"] = status
            completed = ResearchJobRepository(self.db).complete(
                job_id,
                lease_token,
                status=status,
                stage=stage,
                links=links or {},
                evidence_snapshot=safe_evidence,
                error_message=safe_reason,
                provider_completed=provider_completed,
                now=current_time,
                commit=False,
            )
            if completed is None:
                self.db.rollback()
                return False
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            raise

    def begin(self, job_id: int, lease_token: str) -> FullChainRun:
        job = self.db.get(ResearchJob, job_id)
        if job is None:
            raise ResearchFullChainBlocked("research job is missing")
        chain = self.chains.open_for_claimed_job(job_id, lease_token)
        existing = self._stage(chain, "GENERATION")
        if existing is not None:
            if (
                existing.status == "PREPARED"
                and job.stage == "GENERATION_RETRY"
                and job.provider_attempted_at is None
            ):
                return chain
            raise ResearchFullChainBlocked(
                "GENERATION already has a durable checkpoint; Provider replay "
                "is not safe for this recovery state"
            )
        self.chains.prepare_stage(
            chain.id,
            "GENERATION",
            lease_token,
            idempotency_key=(
                f"research-job:{job.id}:{job.request_hash}:generation"
            ),
            input_snapshot={
                "research_job_id": job.id,
                "request_digest": job.request_hash,
                "provider": "deepseek",
                "execution_scope_id": "LOCAL_DRY_RUN",
                "execution_target_id": "OKX_DEMO",
            },
        )
        return chain

    def checkpoint_response(
        self,
        job_id: int,
        lease_token: str,
        response: DeepSeekBacktestLoopResponse,
    ) -> dict[str, int]:
        """Durably bind all external results before advancing checkpoints."""

        links = self._links(response)
        job = self.db.get(ResearchJob, job_id)
        if job is None:
            raise ResearchFullChainBlocked("research job is missing", links=links)
        self._load_and_validate(job, links)
        checkpointed = ResearchJobRepository(self.db).checkpoint_research_result(
            job.id,
            lease_token,
            links=links,
            evidence_snapshot={
                "status": "RUNNING",
                "acceptance_ready": False,
                "recovery_stage": "PERSISTED_RESULT",
                "database_ids": links,
            },
        )
        if checkpointed is None:
            raise ResearchFullChainBlocked(
                "persisted research result could not be checkpointed",
                links=links,
            )
        return links

    def advance(
        self,
        job_id: int,
        lease_token: str,
        response: Optional[DeepSeekBacktestLoopResponse] = None,
    ) -> int:
        job = self.db.get(ResearchJob, job_id)
        if job is None:
            raise ResearchFullChainBlocked("research job is missing")
        links = self._links(response) if response is not None else self._job_links(job)
        if len(links) != len(LINK_KEYS):
            raise ResearchFullChainBlocked(
                "persisted research recovery is missing complete database IDs",
                links=links,
            )
        chain = self._chain(job_id)
        if chain is None:
            raise ResearchFullChainBlocked(
                "research full-chain run is missing",
                links=links,
            )
        try:
            (
                generation,
                strategy,
                version,
                run,
                task,
                result,
                score,
            ) = self._load_and_validate(job, links)
            self._ensure_stage(
                chain,
                "GENERATION",
                lease_token,
                idempotency_key=(
                    f"research-job:{job.id}:{job.request_hash}:generation"
                ),
                input_snapshot={
                    "research_job_id": job.id,
                    "request_digest": job.request_hash,
                    "provider": "deepseek",
                    "execution_scope_id": "LOCAL_DRY_RUN",
                    "execution_target_id": "OKX_DEMO",
                },
                database_ids={
                    "strategy_generation_run_id": generation.id,
                    "strategy_id": strategy.id,
                    "strategy_version_id": version.id,
                },
                output_snapshot={
                    "status": "SUCCESS",
                    "strategy_code_digest": (
                        version.code_hash
                        or hashlib.sha256(
                            version.generated_code.encode("utf-8")
                        ).hexdigest()
                    ),
                },
            )
            self._ensure_stage(
                chain,
                "BACKTEST",
                lease_token,
                idempotency_key=(
                    f"research-job:{job_id}:{run.id}:{task.id}:backtest"
                ),
                input_snapshot={
                    "strategy_version_id": version.id,
                    "backtest_run_id": run.id,
                    "backtest_task_id": task.id,
                },
                database_ids={
                    "backtest_run_id": run.id,
                    "backtest_task_id": task.id,
                    "backtest_result_id": result.id,
                },
                output_snapshot={
                    "status": "SUCCESS",
                    "result_digest": _digest(result.metrics_snapshot),
                    "promotion_evidence_present": isinstance(
                        result.metrics_snapshot.get("promotion_evidence")
                        if isinstance(result.metrics_snapshot, dict)
                        else None,
                        dict,
                    ),
                },
            )
            self._ensure_stage(
                chain,
                "SCORING",
                lease_token,
                idempotency_key=(
                    f"research-job:{job_id}:{score.id}:scoring"
                ),
                input_snapshot={
                    "backtest_result_id": result.id,
                    "scoring_version": score.scoring_version,
                },
                database_ids={"strategy_score_id": score.id},
                output_snapshot={
                    "status": "SUCCESS",
                    "total_score": score.total_score,
                    "score_digest": _digest(score.metrics_snapshot),
                },
            )
            approval = self._approved_candidate(chain)
            if approval is not None:
                return approval.id
            candidate = self._stage(chain, "CANDIDATE_APPROVAL")
            if candidate is None:
                self.chains.prepare_stage(
                    chain.id,
                    "CANDIDATE_APPROVAL",
                    lease_token,
                    idempotency_key=(
                        f"research-job:{job_id}:{version.id}:{result.id}:"
                        f"{score.id}:candidate"
                    ),
                    input_snapshot={
                        "strategy_version_id": version.id,
                        "backtest_result_id": result.id,
                        "strategy_score_id": score.id,
                        "execution_target_id": "OKX_DEMO",
                    },
                )
            elif candidate.status != "PREPARED":
                raise FullChainBlocked(
                    "candidate approval checkpoint is terminal but not approved"
                )
            approval = self.chains.auto_approve_candidate(
                chain.id,
                lease_token,
            )
        except (FullChainBlocked, KeyError, TypeError, ValueError) as exc:
            raise ResearchFullChainBlocked(str(exc), links=links) from exc
        return approval.id

    def _ensure_stage(
        self,
        chain: FullChainRun,
        stage: str,
        lease_token: str,
        *,
        idempotency_key: str,
        input_snapshot: dict,
        database_ids: dict[str, int],
        output_snapshot: dict,
    ) -> FullChainStageRun:
        checkpoint = self._stage(chain, stage)
        if checkpoint is None:
            checkpoint = self.chains.prepare_stage(
                chain.id,
                stage,
                lease_token,
                idempotency_key=idempotency_key,
                input_snapshot=input_snapshot,
            )
        if checkpoint.status not in {"PREPARED", "SUCCESS"}:
            raise FullChainBlocked(
                f"{stage} checkpoint is terminal and cannot recover"
            )
        return self.chains.complete_stage(
            chain.id,
            stage,
            lease_token,
            database_ids=database_ids,
            output_snapshot=output_snapshot,
        )

    def _load_and_validate(
        self,
        job: ResearchJob,
        links: dict[str, int],
    ) -> tuple[
        StrategyGenerationRun,
        Strategy,
        StrategyVersion,
        BacktestRun,
        BacktestTask,
        BacktestResult,
        StrategyScore,
    ]:
        generation = self._require(
            StrategyGenerationRun,
            links["strategy_generation_run_id"],
        )
        strategy = self._require(Strategy, links["strategy_id"])
        version = self._require(StrategyVersion, links["strategy_version_id"])
        run = self._require(BacktestRun, links["backtest_run_id"])
        task = self._require(BacktestTask, links["backtest_task_id"])
        result = self._require(BacktestResult, links["backtest_result_id"])
        score = self._require(StrategyScore, links["strategy_score_id"])
        self._require_real_provider(job, generation)
        if (
            generation.status != "succeeded"
            or generation.execution_scope_id != "LOCAL_DRY_RUN"
            or version.generation_run_id != generation.id
            or version.strategy_id != strategy.id
            or run.execution_scope_id != "LOCAL_DRY_RUN"
            or run.strategy_version_id != version.id
            or run.status != "succeeded"
            or task.backtest_run_id != run.id
            or task.status != "succeeded"
            or result.backtest_run_id != run.id
            or result.backtest_task_id != task.id
            or score.strategy_id != strategy.id
            or score.strategy_version_id != version.id
            or score.backtest_result_id != result.id
        ):
            raise ResearchFullChainBlocked(
                "persisted research lineage is incomplete or inconsistent",
                links=links,
            )
        return generation, strategy, version, run, task, result, score

    @staticmethod
    def _require_real_provider(
        job: ResearchJob,
        generation: StrategyGenerationRun,
    ) -> None:
        params = generation.params_snapshot
        payload = job.request_payload
        if (
            os.environ.get("FREQTRADE_AI_CI_OFFLINE") == "1"
            or not isinstance(payload, dict)
            or payload.get("allow_real_call") is not True
            or job.provider_attempted_at is None
            or not isinstance(params, dict)
            or generation.provider != "deepseek"
            or params.get("mode") != "real_provider"
            or params.get("provider") != "deepseek"
            or params.get("real_provider") is not True
            or params.get("provider_kind") != "real"
            or params.get("real_call_authorized") is not True
            or params.get("real_call_attempted") is not True
            or params.get("credential_env_present") is not True
            or params.get("credential_values_recorded") is not False
            or params.get("operation_status") != "SUCCESS"
        ):
            raise ResearchFullChainBlocked(
                "candidate approval requires attested real DeepSeek provider provenance"
            )

    def _approved_candidate(
        self,
        chain: FullChainRun,
    ) -> Optional[StrategyCandidateApproval]:
        if chain.candidate_approval_id is None:
            return None
        approval = self.db.get(
            StrategyCandidateApproval,
            chain.candidate_approval_id,
        )
        checkpoint = self._stage(chain, "CANDIDATE_APPROVAL")
        if (
            approval is not None
            and approval.status == "APPROVED"
            and checkpoint is not None
            and checkpoint.status == "SUCCESS"
            and checkpoint.database_ids.get("candidate_approval_id")
            == approval.id
        ):
            return approval
        return None

    def _block_unrecoverable_stale(
        self,
        job: ResearchJob,
        chain: Optional[FullChainRun],
        reason: str,
    ) -> None:
        """Quarantine malformed stale state without requiring an expired lease."""

        safe_reason = redact_secret_text(reason)[:2000]
        current_time = datetime.now(timezone.utc)
        evidence_snapshot = {
            **job.evidence_snapshot,
            "status": "BLOCKED",
            "acceptance_ready": False,
            "recovery_allowed": False,
            "failed_reason": safe_reason,
        }
        if chain is not None:
            evidence_snapshot = {
                **evidence_snapshot,
                "full_chain_run_id": chain.id,
                "full_chain_status": "BLOCKED",
            }
        blocked = self.db.execute(
            update(ResearchJob)
            .where(
                ResearchJob.id == job.id,
                ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                ResearchJob.status == "STALE",
                ResearchJob.stage == "LEASE_EXPIRED",
                ResearchJob.lease_token.is_(None),
            )
            .values(
                status="BLOCKED",
                stage="RECOVERY_BLOCKED",
                error_message=safe_reason,
                completed_at=current_time,
                evidence_snapshot=evidence_snapshot,
            )
        )
        if blocked.rowcount != 1:
            self.db.rollback()
            return
        attempt = self.db.scalar(
            select(ResearchJobAttempt)
            .where(
                ResearchJobAttempt.research_job_id == job.id,
                ResearchJobAttempt.execution_scope_id
                == LOCAL_DRY_RUN_SCOPE_ID,
                ResearchJobAttempt.attempt_number == job.attempt_count,
            )
            .limit(1)
        )
        if attempt is not None:
            attempt.status = "BLOCKED"
            attempt.completed_at = current_time
            attempt.error_message = safe_reason
            attempt.evidence_snapshot = evidence_snapshot
        if chain is not None and chain.status not in {
            "SUCCESS",
            "FAILED",
            "BLOCKED",
            "CANCELLED",
        }:
            checkpoint = self.db.scalar(
                select(FullChainStageRun)
                .where(
                    FullChainStageRun.full_chain_run_id == chain.id,
                    FullChainStageRun.status == "PREPARED",
                )
                .order_by(FullChainStageRun.id.desc())
                .limit(1)
            )
            if checkpoint is not None:
                checkpoint.status = "BLOCKED"
                checkpoint.error_code = "RESEARCH_RECOVERY_BLOCKED"
                checkpoint.error_message = safe_reason
                checkpoint.completed_at = current_time
            chain.status = "BLOCKED"
            chain.terminal_reason = safe_reason
            chain.completed_at = current_time
        self.db.commit()

    @staticmethod
    def _resume_recoverable_chain(chain: Optional[FullChainRun]) -> None:
        if chain is None:
            return
        if chain.status not in {"RUNNING", "STALE"}:
            raise ResearchFullChainBlocked(
                "stale recovery cannot resume a terminal full-chain run"
            )
        chain.status = "RUNNING"
        chain.terminal_reason = None
        chain.completed_at = None

    def _chain(self, job_id: int) -> Optional[FullChainRun]:
        return self.db.scalar(
            select(FullChainRun).where(
                FullChainRun.research_job_id == job_id,
                FullChainRun.run_kind == "RESEARCH",
            )
        )

    def _stage(
        self,
        chain: FullChainRun,
        stage: str,
    ) -> Optional[FullChainStageRun]:
        return self.db.scalar(
            select(FullChainStageRun).where(
                FullChainStageRun.full_chain_run_id == chain.id,
                FullChainStageRun.stage == stage,
            )
        )

    def _links(
        self,
        response: Optional[DeepSeekBacktestLoopResponse],
    ) -> dict[str, int]:
        if response is None:
            return {}
        values = response.evidence.ids
        links: dict[str, int] = {}
        for key in LINK_KEYS:
            value = values.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ResearchFullChainBlocked(
                    f"research response is missing persisted {key}",
                    links=links,
                )
            links[key] = value
        return links

    @staticmethod
    def _job_links(job: ResearchJob) -> dict[str, int]:
        return {
            key: value
            for key in LINK_KEYS
            if isinstance((value := getattr(job, key)), int)
            and not isinstance(value, bool)
            and value > 0
        }

    def _require(self, model, entity_id: int):
        entity = self.db.get(model, entity_id)
        if entity is None:
            raise ResearchFullChainBlocked(
                f"{model.__name__} evidence row is missing"
            )
        return entity


def default_research_full_chain_orchestrator_factory(
    db: Session,
) -> ResearchFullChainOrchestrator:
    return ResearchFullChainOrchestrator(db)
