from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import math
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
    StrategyVersion,
)
from app.repositories.strategy_deployments import StrategyDeploymentRepository
from app.services.okx_demo_automation_guard import OkxDemoAutomationGuard
from app.services.okx_demo_selection_policy import OKX_DEMO_SELECTION_POLICY_VERSION
from app.services.risk_chain import canonical_digest


class OkxDemoStrategySelectionBlocked(RuntimeError):
    """The fixed Demo selection evidence is absent, stale, or ambiguous."""


OKX_DEMO_ALLOWED_STRATEGIES = {
    "DeepSeekRegimeCrossoverCandidateB": "DeepSeekRegimeCrossoverCandidateB",
    "Codex Okx Demo Dual RSI Strategy": "CodexOkxDemoDualRsiStrategy",
}
OKX_DEMO_SELECTION_KEYS = {
    "schema_version",
    "policy_version",
    "execution_target_id",
    "strategy_id",
    "strategy_version_id",
    "backtest_run_id",
    "backtest_task_id",
    "backtest_result_id",
    "strategy_score_id",
    "strategy_code_digest",
    "expected_strategy_class",
    "current_version_id",
    "minimum_score",
    "actual_score",
    "validated_backtest_required",
    "validation_basis",
    "production_validation_plan_required",
    "production_promotion_claim",
    "allow_real_funds",
    "source_provenance",
    "source_full_chain_id",
}


def validate_okx_demo_selection_receipt(
    db: Session,
    selection: dict,
    *,
    project_root: Path,
) -> None:
    """Revalidate one fixed Demo selection without granting production eligibility."""

    if not isinstance(selection, dict) or set(selection) != OKX_DEMO_SELECTION_KEYS:
        raise OkxDemoStrategySelectionBlocked("Demo selection fields are incomplete")
    exact_values = {
        "schema_version": "1",
        "policy_version": OKX_DEMO_SELECTION_POLICY_VERSION,
        "execution_target_id": "OKX_DEMO",
        "minimum_score": 50,
        "validated_backtest_required": True,
        "validation_basis": "DEMO_EXISTING_BACKTEST_V1",
        "production_validation_plan_required": False,
        "production_promotion_claim": False,
        "allow_real_funds": False,
        "source_provenance": "EXISTING_VALIDATED_ARTIFACTS",
    }
    if any(
        type(selection.get(key)) is not type(value) or selection.get(key) != value
        for key, value in exact_values.items()
    ):
        raise OkxDemoStrategySelectionBlocked("Demo selection policy changed")
    id_keys = {
        "strategy_id",
        "strategy_version_id",
        "backtest_run_id",
        "backtest_task_id",
        "backtest_result_id",
        "strategy_score_id",
        "current_version_id",
    }
    if any(
        isinstance(selection.get(key), bool)
        or not isinstance(selection.get(key), int)
        or selection[key] <= 0
        for key in id_keys
    ):
        raise OkxDemoStrategySelectionBlocked("Demo selection IDs are invalid")
    source_full_chain_id = selection.get("source_full_chain_id")
    if source_full_chain_id is not None and (
        isinstance(source_full_chain_id, bool)
        or not isinstance(source_full_chain_id, int)
        or source_full_chain_id <= 0
    ):
        raise OkxDemoStrategySelectionBlocked("Demo source chain ID is invalid")

    strategy = db.get(Strategy, selection["strategy_id"])
    version = db.get(StrategyVersion, selection["strategy_version_id"])
    run = db.get(BacktestRun, selection["backtest_run_id"])
    task = db.get(BacktestTask, selection["backtest_task_id"])
    result = db.get(BacktestResult, selection["backtest_result_id"])
    score = db.get(StrategyScore, selection["strategy_score_id"])
    if any(row is None for row in (strategy, version, run, task, result, score)):
        raise OkxDemoStrategySelectionBlocked("Demo selection research records are missing")
    expected_class = OKX_DEMO_ALLOWED_STRATEGIES.get(strategy.name)
    if expected_class is None or selection["expected_strategy_class"] != expected_class:
        raise OkxDemoStrategySelectionBlocked("strategy is not in the fixed Demo allowlist")
    if (
        version.strategy_id != strategy.id
        or strategy.current_version_id != version.id
        or selection["current_version_id"] != version.id
        or version.validation_status != "passed"
    ):
        raise OkxDemoStrategySelectionBlocked("current strategy version is not validated")
    newer_count = db.scalar(
        select(func.count(StrategyVersion.id)).where(
            StrategyVersion.strategy_id == strategy.id,
            StrategyVersion.version_number > version.version_number,
        )
    )
    if int(newer_count or 0) != 0:
        raise OkxDemoStrategySelectionBlocked("strategy version is not current")

    canonical_root = project_root.resolve()
    source_path = (canonical_root / version.file_path).resolve()
    if canonical_root not in source_path.parents or not source_path.is_file():
        raise OkxDemoStrategySelectionBlocked("strategy source file is missing")
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    database_digest = hashlib.sha256(version.generated_code.encode("utf-8")).hexdigest()
    if (
        not version.code_hash
        or source_digest != version.code_hash
        or database_digest != version.code_hash
        or selection["strategy_code_digest"] != version.code_hash
    ):
        raise OkxDemoStrategySelectionBlocked("strategy code hash does not match")
    if version.blueprint.get("class_name") != expected_class:
        raise OkxDemoStrategySelectionBlocked("strategy blueprint class is inconsistent")
    try:
        class_names = {
            node.name for node in ast.parse(version.generated_code).body
            if isinstance(node, ast.ClassDef)
        }
    except SyntaxError as exc:
        raise OkxDemoStrategySelectionBlocked("strategy database code is invalid") from exc
    if expected_class not in class_names:
        raise OkxDemoStrategySelectionBlocked("strategy class is missing from database code")

    if (
        score.strategy_id != strategy.id
        or score.strategy_version_id != version.id
        or score.backtest_result_id != result.id
        or score.scoring_version != "phase2-quality-v1"
        or score.total_score < 50
    ):
        raise OkxDemoStrategySelectionBlocked("strategy score is below the Demo threshold")
    actual_score = selection.get("actual_score")
    if isinstance(actual_score, bool) or not isinstance(actual_score, (int, float)):
        raise OkxDemoStrategySelectionBlocked("Demo strategy score is invalid")
    try:
        score_matches = (
            math.isfinite(float(actual_score))
            and Decimal(str(actual_score)) == Decimal(str(score.total_score))
        )
    except (InvalidOperation, ValueError, OverflowError):
        score_matches = False
    if not score_matches:
        raise OkxDemoStrategySelectionBlocked("Demo strategy score changed")
    if (
        result.backtest_run_id != run.id
        or result.backtest_task_id != task.id
        or run.strategy_version_id != version.id
        or run.status != "succeeded"
        or task.backtest_run_id != run.id
        or task.status != "succeeded"
        or task.pair != "BTC/USDT:USDT"
        or version.blueprint.get("timeframe") != task.timeframe
        or str(run.config_snapshot.get("strategy_version_id")) != str(version.id)
        or Path(str(run.config_snapshot.get("strategy_file_path", ""))).resolve()
        != source_path
    ):
        raise OkxDemoStrategySelectionBlocked("validated backtest lineage is incomplete")

    if source_full_chain_id is not None:
        source_chain = db.get(FullChainRun, source_full_chain_id)
        source_job = db.get(ResearchJob, source_chain.research_job_id) if source_chain else None
        if (
            source_chain is None
            or source_job is None
            or source_chain.run_kind != "RESEARCH"
            or source_job.operation.startswith("okx_demo.selection.")
            or source_chain.strategy_id != strategy.id
            or source_chain.strategy_version_id != version.id
            or source_chain.backtest_run_id != run.id
            or source_chain.backtest_task_id != task.id
            or source_chain.backtest_result_id != result.id
            or source_chain.strategy_score_id != score.id
        ):
            raise OkxDemoStrategySelectionBlocked("Demo source chain lineage is inconsistent")


class OkxDemoStrategySelectionService:
    """Owner-mediated selection of existing, already validated Demo strategies."""

    POLICY_VERSION = OKX_DEMO_SELECTION_POLICY_VERSION
    ALLOWED_STRATEGIES = OKX_DEMO_ALLOWED_STRATEGIES

    def __init__(self, db: Session, *, project_root: Path):
        self.db = db
        self.project_root = project_root.resolve()

    def publish(self, strategy_name: str, *, now: datetime | None = None) -> StrategyDeployment:
        current = now or datetime.now(timezone.utc)
        if strategy_name not in self.ALLOWED_STRATEGIES:
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
        if strategy.current_version_id != version.id:
            raise OkxDemoStrategySelectionBlocked("strategy current-version pointer is inconsistent")
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
        if hashlib.sha256(version.generated_code.encode("utf-8")).hexdigest() != version.code_hash:
            raise OkxDemoStrategySelectionBlocked("strategy database code hash does not match")
        expected_class = self.ALLOWED_STRATEGIES[strategy_name]
        if version.blueprint.get("class_name") != expected_class:
            raise OkxDemoStrategySelectionBlocked("strategy blueprint class is inconsistent")
        try:
            class_names = {
                node.name
                for node in ast.walk(ast.parse(version.generated_code))
                if isinstance(node, ast.ClassDef)
            }
        except SyntaxError as exc:
            raise OkxDemoStrategySelectionBlocked("strategy database code is invalid") from exc
        if expected_class not in class_names:
            raise OkxDemoStrategySelectionBlocked("strategy class is missing from database code")

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
        if (
            result is None
            or run is None
            or task is None
            or run.strategy_version_id != version.id
            or run.status != "succeeded"
            or task.backtest_run_id != run.id
            or task.status != "succeeded"
            or task.pair != "BTC/USDT:USDT"
            or version.blueprint.get("timeframe") != task.timeframe
            or str(run.config_snapshot.get("strategy_version_id")) != str(version.id)
            or Path(str(run.config_snapshot.get("strategy_file_path", ""))).resolve()
            != source_path
        ):
            raise OkxDemoStrategySelectionBlocked("validated backtest lineage is incomplete")
        source_chain = self.db.scalar(
            select(FullChainRun)
            .join(ResearchJob, ResearchJob.id == FullChainRun.research_job_id)
            .where(
                ResearchJob.operation.not_like("okx_demo.selection.%"),
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
            "strategy_code_digest": version.code_hash,
            "expected_strategy_class": expected_class,
            "current_version_id": strategy.current_version_id,
            "minimum_score": 50,
            "actual_score": float(score.total_score),
            "validated_backtest_required": True,
            "validation_basis": "DEMO_EXISTING_BACKTEST_V1",
            "production_validation_plan_required": False,
            "production_promotion_claim": False,
            "allow_real_funds": False,
            "source_provenance": "EXISTING_VALIDATED_ARTIFACTS",
            "source_full_chain_id": source_chain.id if source_chain is not None else None,
        }
        validate_okx_demo_selection_receipt(
            self.db,
            selection,
            project_root=self.project_root,
        )
        candidate_digest = canonical_digest(selection)
        operation = "okx_demo.selection.fixed_v2"
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
            strategy_generation_run_id=(
                source_chain.strategy_generation_run_id
                if source_chain is not None
                else None
            ),
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
