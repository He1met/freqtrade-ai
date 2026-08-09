from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.full_chain import StrategyCandidateApproval
from app.models.strategy import Strategy, StrategyVersion
from app.models.strategy_deployment import SignalEvaluation, StrategyDeployment
from app.repositories.strategy_research import StrategyResearchRepository
from app.schemas.okx_demo_runtime_activity import (
    OkxDemoActiveDeploymentRead,
    OkxDemoProjectionWindow,
    OkxDemoRuntimeActivityRead,
    OkxDemoSignalEvaluationRead,
)
from app.schemas.strategy_research import (
    MarketDataQualityReceiptRead,
    StrategyResearchAttemptEventRead,
    StrategyResearchAttemptRead,
    StrategyResearchBatchRead,
    StrategyResearchWorkspaceRead,
    StrategyResearchWorkspaceSectionRead,
    StrategyResearchWorkspaceSectionsRead,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrategyResearchWorkspaceService:
    """Build the formal research page projection without mutating state."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def build(self, *, attempt_limit: int = 10) -> StrategyResearchWorkspaceRead:
        repository = StrategyResearchRepository(self.db)
        section_statuses: dict[str, StrategyResearchWorkspaceSectionRead] = {}
        try:
            chains = repository.list_recent_attempt_event_chains(attempt_limit=attempt_limit)
            section_statuses["attempts"] = StrategyResearchWorkspaceSectionRead(status="AVAILABLE")
        except SQLAlchemyError:
            self.db.rollback()
            chains = []
            section_statuses["attempts"] = StrategyResearchWorkspaceSectionRead(
                status="UNKNOWN",
                reason_code="ATTEMPT_RECEIPTS_UNAVAILABLE",
            )
        attempts = [
            StrategyResearchAttemptRead(
                attempt_id=events[0].attempt_id,
                latest_outcome=events[-1].outcome,
                events=[StrategyResearchAttemptEventRead.model_validate(event) for event in events],
            )
            for events in chains
            if events
        ]
        try:
            batches = repository.list_batches(limit=1)
            section_statuses["batch"] = StrategyResearchWorkspaceSectionRead(status="AVAILABLE")
        except SQLAlchemyError:
            self.db.rollback()
            batches = []
            section_statuses["batch"] = StrategyResearchWorkspaceSectionRead(
                status="UNKNOWN",
                reason_code="RESEARCH_BATCHES_UNAVAILABLE",
            )
        latest_batch = batches[0] if batches else None
        try:
            latest_quality = repository.latest_quality_receipt()
            section_statuses["quality"] = StrategyResearchWorkspaceSectionRead(status="AVAILABLE")
        except SQLAlchemyError:
            self.db.rollback()
            latest_quality = None
            section_statuses["quality"] = StrategyResearchWorkspaceSectionRead(
                status="UNKNOWN",
                reason_code="MARKET_DATA_QUALITY_RECEIPTS_UNAVAILABLE",
            )
        if section_statuses["batch"].status == "UNKNOWN":
            handoff = "UNKNOWN"
        elif latest_batch is None:
            handoff = "NOT_EVALUATED"
        elif latest_batch.qualified_count == 0:
            handoff = "NOT_QUEUED_NO_QUALIFIED"
        else:
            # No auditable candidate -> canonical lifecycle bridge exists yet.
            handoff = "CANONICAL_LINK_UNAVAILABLE"
        return StrategyResearchWorkspaceRead(
            schema_version="formal-strategy-research-workspace-v1",
            as_of=_utc_now(),
            source_type="database",
            core_data=True,
            evidence_status=(
                "COMPLETE"
                if all(section.status == "AVAILABLE" for section in section_statuses.values())
                else "PARTIAL"
            ),
            sections=StrategyResearchWorkspaceSectionsRead(**section_statuses),
            attempts=attempts,
            latest_quality_receipt=(
                MarketDataQualityReceiptRead.model_validate(latest_quality)
                if latest_quality is not None
                else None
            ),
            latest_batch=(
                StrategyResearchBatchRead.model_validate(latest_batch)
                if latest_batch is not None
                else None
            ),
            handoff_status=handoff,
        )


class OkxDemoRuntimeActivityService:
    """Expose an allowlisted, DB-backed OKX Demo deployment/signal projection."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def build(self, *, signal_limit: int = 20) -> OkxDemoRuntimeActivityRead:
        deployment_rows = self.db.execute(
            select(
                StrategyDeployment,
                Strategy,
                StrategyVersion,
                StrategyCandidateApproval,
            )
            .join(Strategy, Strategy.id == StrategyDeployment.strategy_id)
            .join(StrategyVersion, StrategyVersion.id == StrategyDeployment.strategy_version_id)
            .join(
                StrategyCandidateApproval,
                StrategyCandidateApproval.id == StrategyDeployment.candidate_approval_id,
            )
            .where(
                StrategyDeployment.execution_target_id == "OKX_DEMO",
                StrategyDeployment.status == "ACTIVE",
            )
            .order_by(StrategyDeployment.active_slot.asc(), StrategyDeployment.id.asc())
        ).all()
        deployments = [
            OkxDemoActiveDeploymentRead(
                deployment_id=deployment.id,
                status="ACTIVE",
                active_slot=deployment.active_slot,
                instrument_id=deployment.instrument_id,
                timeframe=deployment.timeframe,
                strategy_id=strategy.id,
                strategy_name=strategy.name,
                strategy_version_id=version.id,
                strategy_version_number=version.version_number,
                candidate_approval_id=approval.id,
                candidate_approval_status=approval.status,
                created_at=deployment.created_at,
            )
            for deployment, strategy, version, approval in deployment_rows
        ]
        evaluation_rows = list(
            self.db.scalars(
                select(SignalEvaluation)
                .where(SignalEvaluation.execution_target_id == "OKX_DEMO")
                .order_by(
                    SignalEvaluation.closed_candle_at.desc(),
                    SignalEvaluation.id.desc(),
                )
                .limit(signal_limit + 1)
            ).all()
        )
        has_more = len(evaluation_rows) > signal_limit
        evaluations = [
            OkxDemoSignalEvaluationRead(
                evaluation_id=evaluation.id,
                deployment_id=evaluation.deployment_id,
                instrument_id=evaluation.instrument_id,
                timeframe=evaluation.timeframe,
                closed_candle_at=evaluation.closed_candle_at,
                status=evaluation.status,
                completed_at=evaluation.completed_at,
                error_code=evaluation.error_code,
                created_at=evaluation.created_at,
            )
            for evaluation in evaluation_rows[:signal_limit]
        ]
        return OkxDemoRuntimeActivityRead(
            schema_version="okx-demo-runtime-activity-v1",
            as_of=_utc_now(),
            source_type="database",
            core_data=True,
            execution_target="OKX_DEMO",
            allow_real_funds=False,
            real_orders=False,
            active_deployments=deployments,
            recent_signal_evaluations=evaluations,
            signal_window=OkxDemoProjectionWindow(
                returned_count=len(evaluations),
                limit=signal_limit,
                has_more=has_more,
            ),
        )
