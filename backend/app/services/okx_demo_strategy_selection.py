from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import (
    BacktestResult,
    BacktestRun,
    BacktestTask,
    FullChainRun,
    FullChainStageRun,
    ResearchJob,
    ResearchJobAttempt,
    Strategy,
    StrategyCandidateApproval,
    StrategyDeployment,
    StrategyScore,
    StrategyValidationPlan,
    StrategyVersion,
)
from app.repositories.strategy_deployments import StrategyDeploymentRepository
from app.services.okx_demo_automation_guard import OkxDemoAutomationGuard
from app.services.risk_chain import canonical_digest


class OkxDemoStrategySelectionBlocked(RuntimeError):
    """The fixed Demo selection evidence is absent, stale, or ambiguous."""


class OkxDemoStrategySelectionService:
    """Owner-mediated selection of existing, already validated Demo strategies."""

    POLICY_VERSION = "okx-demo-selection-v1"
    ALLOWED_NAMES = frozenset(
        {"DeepSeekRegimeCrossoverCandidateB", "CodexOkxDemoDualRsiStrategy"}
    )

    def __init__(self, db: Session, *, project_root: Path):
        self.db = db
        self.project_root = project_root.resolve()

    def publish(self, strategy_name: str, *, now: datetime | None = None) -> StrategyDeployment:
        current = now or datetime.now(timezone.utc)
        if strategy_name not in self.ALLOWED_NAMES:
            raise OkxDemoStrategySelectionBlocked("strategy is not in the fixed Demo allowlist")
        if self.db.get_bind().dialect.name != "postgresql":
            raise OkxDemoStrategySelectionBlocked("selection requires PostgreSQL")
        if self.db.execute(text("SELECT current_user='freqtrade'" )).scalar_one():
            raise OkxDemoStrategySelectionBlocked("runtime role cannot publish Demo selections")
        self.db.execute(text("SELECT pg_advisory_xact_lock(543000003)"))

        strategy = self.db.scalar(
            select(Strategy).where(Strategy.name == strategy_name).with_for_update()
        )
        if strategy is None:
            raise OkxDemoStrategySelectionBlocked("strategy is missing")
        version = self.db.scalar(
            select(StrategyVersion)
            .where(StrategyVersion.strategy_id == strategy.id)
            .order_by(StrategyVersion.version_number.desc(), StrategyVersion.id.desc())
            .with_for_update()
        )
        if version is None or version.validation_status != "passed":
            raise OkxDemoStrategySelectionBlocked("current strategy version is not validated")
        newer_count = self.db.scalar(
            select(func.count(StrategyVersion.id)).where(
                StrategyVersion.strategy_id == strategy.id,
                StrategyVersion.version_number > version.version_number,
            )
        )
        if int(newer_count or 0) != 0:
            raise OkxDemoStrategySelectionBlocked("strategy version is not current")
        source_path = (self.project_root / version.file_path).resolve()
        if self.project_root not in source_path.parents or not source_path.is_file():
            raise OkxDemoStrategySelectionBlocked("strategy source file is missing")
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != version.code_hash:
            raise OkxDemoStrategySelectionBlocked("strategy source hash does not match")

        score = self.db.scalar(
            select(StrategyScore)
            .where(
                StrategyScore.strategy_id == strategy.id,
                StrategyScore.strategy_version_id == version.id,
                StrategyScore.scoring_version == "phase2-quality-v1",
                StrategyScore.total_score >= 50,
            )
            .order_by(StrategyScore.total_score.desc(), StrategyScore.id.desc())
            .with_for_update()
        )
        if score is None:
            raise OkxDemoStrategySelectionBlocked("strategy score is below the Demo threshold")
        result = self.db.scalar(
            select(BacktestResult)
            .where(BacktestResult.id == score.backtest_result_id)
            .with_for_update()
        )
        run = (
            self.db.scalar(
                select(BacktestRun)
                .where(BacktestRun.id == result.backtest_run_id)
                .with_for_update()
            )
            if result is not None
            else None
        )
        task = (
            self.db.scalar(
                select(BacktestTask)
                .where(BacktestTask.id == result.backtest_task_id)
                .with_for_update()
            )
            if result is not None and result.backtest_task_id is not None
            else None
        )
        plan = self.db.scalar(
            select(StrategyValidationPlan)
            .where(
                StrategyValidationPlan.strategy_version_id == version.id,
                StrategyValidationPlan.promotion_backtest_result_id == (
                    result.id if result is not None else -1
                ),
                StrategyValidationPlan.status == "PASSED",
                StrategyValidationPlan.strategy_code_digest == version.code_hash,
            )
            .order_by(StrategyValidationPlan.id.desc())
            .with_for_update()
        )
        if (
            result is None
            or run is None
            or task is None
            or plan is None
            or run.strategy_version_id != version.id
            or run.status != "succeeded"
            or task.backtest_run_id != run.id
            or task.status != "succeeded"
        ):
            raise OkxDemoStrategySelectionBlocked("validated backtest lineage is incomplete")
        source_chain = self.db.scalar(
            select(FullChainRun)
            .where(
                FullChainRun.run_kind == "RESEARCH",
                FullChainRun.strategy_id == strategy.id,
                FullChainRun.strategy_version_id == version.id,
                FullChainRun.backtest_run_id == run.id,
                FullChainRun.backtest_task_id == task.id,
                FullChainRun.backtest_result_id == result.id,
                FullChainRun.strategy_score_id == score.id,
            )
            .order_by(FullChainRun.id.desc())
            .with_for_update()
        )
        if source_chain is None:
            raise OkxDemoStrategySelectionBlocked("research full-chain lineage is missing")

        selection = {
            "schema_version": "1",
            "policy_version": self.POLICY_VERSION,
            "execution_target_id": "OKX_DEMO",
            "strategy_id": strategy.id,
            "strategy_version_id": version.id,
            "backtest_run_id": run.id,
            "backtest_task_id": task.id,
            "backtest_result_id": result.id,
            "strategy_score_id": score.id,
            "validation_plan_id": plan.id,
            "strategy_code_digest": version.code_hash,
            "minimum_score": 50,
            "actual_score": float(score.total_score),
            "validated_backtest_required": True,
            "production_promotion_claim": False,
            "allow_real_funds": False,
        }
        candidate_digest = canonical_digest(selection)
        operation = "okx_demo.selection.fixed_v1"
        job_key = hashlib.sha256((operation + "|" + candidate_digest).encode()).hexdigest()
        existing_job = self.db.scalar(
            select(ResearchJob).where(
                ResearchJob.execution_scope_id == "LOCAL_DRY_RUN",
                ResearchJob.operation == operation,
                ResearchJob.idempotency_key_digest == job_key,
            )
        )
        if existing_job is not None:
            deployment = self.db.scalar(
                select(StrategyDeployment)
                .join(StrategyCandidateApproval)
                .join(FullChainRun)
                .where(FullChainRun.research_job_id == existing_job.id)
            )
            if deployment is None:
                raise OkxDemoStrategySelectionBlocked("selection replay is incomplete")
            return deployment

        job = ResearchJob(
            execution_scope_id="LOCAL_DRY_RUN",
            job_type="okx_demo_strategy_selection",
            operation=operation,
            idempotency_key_digest=job_key,
            request_hash=candidate_digest,
            request_payload=selection,
            status="SUCCESS",
            stage="DEPLOYED",
            attempt_count=1,
            max_attempts=1,
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            backtest_run_id=run.id,
            backtest_task_id=task.id,
            backtest_result_id=result.id,
            strategy_score_id=score.id,
            evidence_snapshot=selection,
            started_at=current,
            completed_at=current,
        )
        self.db.add(job)
        self.db.flush()
        attempt = ResearchJobAttempt(
            research_job_id=job.id,
            attempt_number=1,
            execution_scope_id="LOCAL_DRY_RUN",
            status="SUCCESS",
            started_at=current,
            completed_at=current,
        )
        self.db.add(attempt)
        self.db.flush()
        chain = FullChainRun(
            research_job_id=job.id,
            research_job_attempt_id=attempt.id,
            run_kind="RESEARCH",
            research_scope_id="LOCAL_DRY_RUN",
            execution_target_id="OKX_DEMO",
            status="APPROVED",
            current_stage="CANDIDATE_APPROVAL",
            strategy_generation_run_id=source_chain.strategy_generation_run_id,
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            backtest_run_id=run.id,
            backtest_task_id=task.id,
            backtest_result_id=result.id,
            strategy_score_id=score.id,
            started_at=current,
        )
        self.db.add(chain)
        self.db.flush()
        approval = StrategyCandidateApproval(
            full_chain_run_id=chain.id,
            execution_target_id="OKX_DEMO",
            strategy_version_id=version.id,
            backtest_result_id=result.id,
            strategy_score_id=score.id,
            candidate_digest=candidate_digest,
            promotion_policy_version=self.POLICY_VERSION,
            promotion_evidence={"policy": selection, "selection": selection},
            status="APPROVED",
            requested_by="owner:okx-demo-selection",
            decided_by="owner:okx-demo-selection",
            decision_reason="Demo-only selection; no production promotion claim.",
            requested_at=current,
            decided_at=current,
            expires_at=current + timedelta(minutes=10),
        )
        self.db.add(approval)
        self.db.flush()
        chain.candidate_approval_id = approval.id
        checkpoint = FullChainStageRun(
            full_chain_run_id=chain.id,
            stage="CANDIDATE_APPROVAL",
            status="SUCCESS",
            idempotency_key_digest=job_key,
            input_digest=candidate_digest,
            input_snapshot=selection,
            database_ids={"candidate_approval_id": approval.id},
            output_snapshot={"status": "APPROVED", "selection": selection},
            prepared_at=current,
            completed_at=current,
        )
        self.db.add(checkpoint)
        deployment_policy = canonical_digest(
            {
                "selection": selection,
                "demo_risk_policy_digest": OkxDemoAutomationGuard.policy_digest(),
            }
        )
        deployment = StrategyDeploymentRepository(self.db).publish(
            candidate_approval_id=approval.id,
            instrument_id="BTC-USDT-SWAP",
            timeframe=task.timeframe,
            deployment_policy_digest=deployment_policy,
            risk_policy_digest=OkxDemoAutomationGuard.policy_digest(),
            commit=False,
            now=current,
        )
        self.db.commit()
        self.db.refresh(deployment)
        return deployment
