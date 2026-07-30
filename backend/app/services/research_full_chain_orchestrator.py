from __future__ import annotations

import hashlib
import json
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.backtest import BacktestResult, BacktestRun, BacktestTask
from app.models.full_chain import FullChainRun, FullChainStageRun
from app.models.research_job import ResearchJob
from app.models.strategy import Strategy, StrategyVersion
from app.models.strategy_generation_run import StrategyGenerationRun
from app.models.strategy_score import StrategyScore
from app.repositories.full_chain import FullChainBlocked, FullChainRepository
from app.schemas.deepseek_backtest_loop import DeepSeekBacktestLoopResponse
from app.schemas.dry_run_status import redact_secret_text


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


class ResearchFullChainOrchestrator:
    """Bind one real ResearchJob result to the existing durable full chain."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.chains = FullChainRepository(db)

    def begin(self, job_id: int, lease_token: str) -> FullChainRun:
        job = self.db.get(ResearchJob, job_id)
        if job is None:
            raise ResearchFullChainBlocked("research job is missing")
        chain = self.chains.open_for_claimed_job(job_id, lease_token)
        existing = self.db.scalar(
            select(FullChainStageRun).where(
                FullChainStageRun.full_chain_run_id == chain.id,
                FullChainStageRun.stage == "GENERATION",
            )
        )
        if existing is not None:
            raise ResearchFullChainBlocked(
                "GENERATION already has a durable checkpoint; refusing to repeat "
                "the Provider call without explicit recovery"
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

    def advance(
        self,
        job_id: int,
        lease_token: str,
        response: DeepSeekBacktestLoopResponse,
    ) -> int:
        links = self._links(response)
        chain = self.db.scalar(
            select(FullChainRun).where(
                FullChainRun.research_job_id == job_id,
                FullChainRun.run_kind == "RESEARCH",
            )
        )
        if chain is None:
            raise ResearchFullChainBlocked(
                "research full-chain run is missing",
                links=links,
            )
        try:
            generation = self._require(StrategyGenerationRun, links["strategy_generation_run_id"])
            strategy = self._require(Strategy, links["strategy_id"])
            version = self._require(StrategyVersion, links["strategy_version_id"])
            run = self._require(BacktestRun, links["backtest_run_id"])
            task = self._require(BacktestTask, links["backtest_task_id"])
            result = self._require(BacktestResult, links["backtest_result_id"])
            score = self._require(StrategyScore, links["strategy_score_id"])

            self.chains.complete_stage(
                chain.id,
                "GENERATION",
                lease_token,
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
            self.chains.prepare_stage(
                chain.id,
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
            )
            self.chains.complete_stage(
                chain.id,
                "BACKTEST",
                lease_token,
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
            self.chains.prepare_stage(
                chain.id,
                "SCORING",
                lease_token,
                idempotency_key=(
                    f"research-job:{job_id}:{score.id}:scoring"
                ),
                input_snapshot={
                    "backtest_result_id": result.id,
                    "scoring_version": score.scoring_version,
                },
            )
            self.chains.complete_stage(
                chain.id,
                "SCORING",
                lease_token,
                database_ids={"strategy_score_id": score.id},
                output_snapshot={
                    "status": "SUCCESS",
                    "total_score": score.total_score,
                    "score_digest": _digest(score.metrics_snapshot),
                },
            )
            self.chains.prepare_stage(
                chain.id,
                "CANDIDATE_APPROVAL",
                lease_token,
                idempotency_key=(
                    f"research-job:{job_id}:{version.id}:{result.id}:{score.id}:candidate"
                ),
                input_snapshot={
                    "strategy_version_id": version.id,
                    "backtest_result_id": result.id,
                    "strategy_score_id": score.id,
                    "execution_target_id": "OKX_DEMO",
                },
            )
            approval = self.chains.auto_approve_candidate(
                chain.id,
                lease_token,
            )
        except (FullChainBlocked, KeyError, TypeError, ValueError) as exc:
            reason = str(exc)
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
                try:
                    self.chains.fail_stage(
                        chain.id,
                        checkpoint.stage,
                        lease_token,
                        status="BLOCKED",
                        error_code="RESEARCH_PROMOTION_BLOCKED",
                        error_message=reason,
                    )
                except FullChainBlocked:
                    self.db.rollback()
            raise ResearchFullChainBlocked(reason, links=links) from exc
        return approval.id

    def fail_generation(
        self,
        job_id: int,
        lease_token: str,
        *,
        status: str,
        reason: str,
    ) -> None:
        chain = self.db.scalar(
            select(FullChainRun).where(
                FullChainRun.research_job_id == job_id,
                FullChainRun.run_kind == "RESEARCH",
            )
        )
        if chain is None:
            return
        checkpoint = self.db.scalar(
            select(FullChainStageRun).where(
                FullChainStageRun.full_chain_run_id == chain.id,
                FullChainStageRun.stage == "GENERATION",
                FullChainStageRun.status == "PREPARED",
            )
        )
        if checkpoint is None:
            return
        self.chains.fail_stage(
            chain.id,
            "GENERATION",
            lease_token,
            status=status if status in {"FAILED", "BLOCKED"} else "FAILED",
            error_code="RESEARCH_EXECUTION_" + status,
            error_message=reason,
        )

    def _links(self, response: DeepSeekBacktestLoopResponse) -> dict[str, int]:
        values = response.evidence.ids
        required = (
            "strategy_generation_run_id",
            "strategy_id",
            "strategy_version_id",
            "backtest_run_id",
            "backtest_task_id",
            "backtest_result_id",
            "strategy_score_id",
        )
        links: dict[str, int] = {}
        for key in required:
            value = values.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ResearchFullChainBlocked(
                    f"research response is missing persisted {key}",
                    links=links,
                )
            links[key] = value
        return links

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
