from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.full_chain import StrategyCandidateApproval
from app.models.strategy_research import StrategyResearchCandidateBridgeEvent
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
    StrategyResearchCandidateLifecycleRead,
    StrategyResearchLifecycleSummaryRead,
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
        candidates = list(latest_batch.candidates) if latest_batch is not None else []
        candidate_ids = [candidate.id for candidate in candidates]
        try:
            bridge_rows = repository.list_latest_candidate_bridge_events(
                candidate_ids=candidate_ids
            )
            section_statuses["bridge"] = StrategyResearchWorkspaceSectionRead(status="AVAILABLE")
        except SQLAlchemyError:
            self.db.rollback()
            bridge_rows = []
            section_statuses["bridge"] = StrategyResearchWorkspaceSectionRead(
                status="UNKNOWN",
                reason_code="CANDIDATE_BRIDGE_EVENTS_UNAVAILABLE",
            )
        bridge_by_candidate = {row.research_candidate_id: row for row in bridge_rows}
        chain_ids = [
            row.canonical_full_chain_run_id
            for row in bridge_rows
            if row.canonical_full_chain_run_id is not None
        ]
        try:
            approval_rows = (
                list(
                    self.db.scalars(
                        select(StrategyCandidateApproval).where(
                            StrategyCandidateApproval.full_chain_run_id.in_(chain_ids)
                        )
                    ).all()
                )
                if chain_ids
                else []
            )
            section_statuses["approval"] = StrategyResearchWorkspaceSectionRead(status="AVAILABLE")
        except SQLAlchemyError:
            self.db.rollback()
            approval_rows = []
            section_statuses["approval"] = StrategyResearchWorkspaceSectionRead(
                status="UNKNOWN",
                reason_code="CANDIDATE_APPROVALS_UNAVAILABLE",
            )
        approval_by_chain = {row.full_chain_run_id: row for row in approval_rows}
        approval_ids = [row.id for row in approval_rows]
        try:
            deployment_rows = (
                list(
                    self.db.scalars(
                        select(StrategyDeployment).where(
                            StrategyDeployment.candidate_approval_id.in_(approval_ids),
                            StrategyDeployment.execution_target_id == "OKX_DEMO",
                        )
                    ).all()
                )
                if approval_ids
                else []
            )
            section_statuses["deployment"] = StrategyResearchWorkspaceSectionRead(status="AVAILABLE")
        except SQLAlchemyError:
            self.db.rollback()
            deployment_rows = []
            section_statuses["deployment"] = StrategyResearchWorkspaceSectionRead(
                status="UNKNOWN",
                reason_code="CANDIDATE_DEPLOYMENTS_UNAVAILABLE",
            )
        deployment_by_approval = {row.candidate_approval_id: row for row in deployment_rows}
        candidate_lifecycles = [
            self._candidate_lifecycle(
                candidate,
                bridge_by_candidate.get(candidate.id),
                approval_by_chain=approval_by_chain,
                deployment_by_approval=deployment_by_approval,
                sections=section_statuses,
            )
            for candidate in candidates
        ]
        lifecycle_summary = self._lifecycle_summary(
            latest_batch=latest_batch,
            lifecycles=candidate_lifecycles,
            bridge_section=section_statuses["bridge"],
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
            schema_version="formal-strategy-research-workspace-v2",
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
            lifecycle_summary=lifecycle_summary,
            candidate_lifecycles=candidate_lifecycles,
            handoff_status=handoff,
        )

    @staticmethod
    def _candidate_lifecycle(
        candidate,
        bridge: StrategyResearchCandidateBridgeEvent | None,
        *,
        approval_by_chain: dict[int, StrategyCandidateApproval],
        deployment_by_approval: dict[int, StrategyDeployment],
        sections: dict[str, StrategyResearchWorkspaceSectionRead],
    ) -> StrategyResearchCandidateLifecycleRead:
        if candidate.status == "REJECTED":
            status, reason = "NOT_APPLICABLE_REJECTED", "RESEARCH_CANDIDATE_REJECTED"
        elif candidate.status == "VALIDATION_FAILED":
            status, reason = "NOT_APPLICABLE_VALIDATION_FAILED", "RESEARCH_VALIDATION_FAILED"
        elif sections["bridge"].status == "UNKNOWN":
            status, reason = "UNKNOWN", "CANDIDATE_BRIDGE_EVENTS_UNAVAILABLE"
        elif bridge is None or bridge.outcome != "BRIDGED":
            status = "UNBRIDGED_REVALIDATION_REQUIRED"
            reason = bridge.reason_code if bridge is not None else "CANONICAL_BLUEPRINT_V2_REQUIRED"
        elif sections["approval"].status == "UNKNOWN":
            status, reason = "UNKNOWN", "CANDIDATE_APPROVALS_UNAVAILABLE"
        else:
            approval = approval_by_chain.get(bridge.canonical_full_chain_run_id)
            if approval is None:
                status, reason = (
                    "BRIDGED_PENDING_CANONICAL_VALIDATION",
                    "CANONICAL_VALIDATION_REQUIRED",
                )
            elif approval.status == "PENDING":
                status, reason = "BRIDGED_PENDING_APPROVAL", "HUMAN_APPROVAL_PENDING"
            elif approval.status != "APPROVED":
                status, reason = "BRIDGED_APPROVAL_REJECTED", f"APPROVAL_{approval.status}"
            elif sections["deployment"].status == "UNKNOWN":
                status, reason = "UNKNOWN", "CANDIDATE_DEPLOYMENTS_UNAVAILABLE"
            else:
                deployment = deployment_by_approval.get(approval.id)
                if deployment is None:
                    status, reason = "APPROVED_NOT_DEPLOYED", "NO_DEMO_DEPLOYMENT_RECEIPT"
                elif deployment.status == "ACTIVE":
                    status, reason = "DEPLOYED_ACTIVE_DEMO", "OKX_DEMO_DEPLOYMENT_ACTIVE"
                else:
                    status, reason = "DEPLOYED_DISABLED", "OKX_DEMO_DEPLOYMENT_DISABLED"
        approval = (
            approval_by_chain.get(bridge.canonical_full_chain_run_id)
            if bridge is not None and bridge.canonical_full_chain_run_id is not None
            else None
        )
        deployment = deployment_by_approval.get(approval.id) if approval is not None else None
        return StrategyResearchCandidateLifecycleRead(
            candidate_id=candidate.id,
            batch_id=candidate.batch_id,
            candidate_name=candidate.candidate_name,
            research_status=candidate.status,
            lifecycle_status=status,
            reason_code=reason,
            source_code_digest=candidate.code_digest,
            bridge_event_id=bridge.id if bridge is not None else None,
            bridge_outcome=bridge.outcome if bridge is not None else None,
            bridge_contract_version=bridge.bridge_contract_version if bridge is not None else None,
            blueprint_digest=bridge.blueprint_digest if bridge is not None else None,
            canonical_strategy_id=bridge.strategy_id if bridge is not None else None,
            canonical_strategy_version_id=bridge.strategy_version_id if bridge is not None else None,
            canonical_full_chain_run_id=(
                bridge.canonical_full_chain_run_id if bridge is not None else None
            ),
            candidate_approval_id=approval.id if approval is not None else None,
            candidate_approval_status=approval.status if approval is not None else None,
            deployment_id=deployment.id if deployment is not None else None,
            deployment_status=deployment.status if deployment is not None else None,
            active_slot=deployment.active_slot if deployment is not None else None,
            created_at=bridge.created_at if bridge is not None else None,
        )

    @staticmethod
    def _lifecycle_summary(
        *,
        latest_batch,
        lifecycles: list[StrategyResearchCandidateLifecycleRead],
        bridge_section: StrategyResearchWorkspaceSectionRead,
    ) -> StrategyResearchLifecycleSummaryRead:
        qualified = [item for item in lifecycles if item.research_status == "QUALIFIED"]
        counts = {
            "unbridged_count": sum(item.lifecycle_status == "UNBRIDGED_REVALIDATION_REQUIRED" for item in qualified),
            "pending_canonical_validation_count": sum(item.lifecycle_status == "BRIDGED_PENDING_CANONICAL_VALIDATION" for item in qualified),
            "pending_approval_count": sum(item.lifecycle_status == "BRIDGED_PENDING_APPROVAL" for item in qualified),
            "approved_not_deployed_count": sum(item.lifecycle_status == "APPROVED_NOT_DEPLOYED" for item in qualified),
            "active_demo_count": sum(item.lifecycle_status == "DEPLOYED_ACTIVE_DEMO" for item in qualified),
            "unknown_count": sum(item.lifecycle_status == "UNKNOWN" for item in qualified),
        }
        if bridge_section.status == "UNKNOWN":
            status, reason = "UNKNOWN", "CANDIDATE_BRIDGE_EVENTS_UNAVAILABLE"
        elif latest_batch is None:
            status, reason = "NOT_EVALUATED", "NO_RESEARCH_BATCH"
        elif not qualified:
            status, reason = "NOT_QUEUED_NO_QUALIFIED", "NO_QUALIFIED_CANDIDATES"
        else:
            active_statuses = {item.lifecycle_status for item in qualified}
            if len(active_statuses) == 1:
                only = next(iter(active_statuses))
                status = (
                    only
                    if only in {
                        "UNBRIDGED_REVALIDATION_REQUIRED",
                        "BRIDGED_PENDING_CANONICAL_VALIDATION",
                        "BRIDGED_PENDING_APPROVAL",
                        "APPROVED_NOT_DEPLOYED",
                        "DEPLOYED_ACTIVE_DEMO",
                        "UNKNOWN",
                    }
                    else "MIXED"
                )
            else:
                status = "MIXED"
            reason = "CANDIDATE_LIFECYCLE_SUMMARY"
        return StrategyResearchLifecycleSummaryRead(
            status=status,
            qualified_count=len(qualified),
            reason_code=reason,
            **counts,
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
