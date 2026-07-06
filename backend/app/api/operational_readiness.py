from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories import StrategyFailureReasonRepository, StrategyRepository
from app.schemas import StrategyFailureReasonFilter, StrategyFailureReasonRead
from app.services.audit_log import GovernanceAuditLogService


router = APIRouter(prefix="/api", tags=["operational-readiness"])


@router.get("/hyperopt-runs")
def list_hyperopt_runs() -> list[dict[str, Any]]:
    return []


@router.get("/live-candidates/governance")
@router.get("/live-candidates")
def read_live_candidate_governance() -> dict[str, Any]:
    return {
        "source_ref": "backend-api:live-candidates/governance",
        "read_only": True,
        "safety_boundary": (
            "Live candidate governance is read-only API evidence only; it cannot start live "
            "trading, place real orders, connect to exchanges, or deploy services."
        ),
        "profiles": [],
        "approvals": [],
        "deployments": [],
        "monitoring_snapshots": [],
    }


@router.get("/governance-events")
@router.get("/audit-log/governance-events")
def list_governance_events(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    events = GovernanceAuditLogService().query_events(limit=limit)
    return [
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "status": event.status,
            "actor": event.actor.display_name or event.actor.actor_id,
            "source_name": event.source.name,
            "summary": event.summary,
            "reason": event.reason,
            "artifact_links": [
                {
                    "name": link.name,
                    "path": link.path,
                    "source": event.source.source_type,
                    "status": event.status,
                    "exists": False,
                }
                for link in event.artifact_links
            ],
            "created_at": event.created_at.isoformat(),
        }
        for event in events
    ]


@router.get("/strategy-failure-reasons", response_model=list[StrategyFailureReasonRead])
def list_strategy_failure_reasons(
    strategy_id: Optional[int] = Query(default=None, gt=0),
    strategy_version_id: Optional[int] = Query(default=None, gt=0),
    db: Session = Depends(get_db),
) -> list[StrategyFailureReasonRead]:
    filters = StrategyFailureReasonFilter(
        strategy_id=strategy_id,
        strategy_version_id=strategy_version_id,
    )
    reasons = StrategyFailureReasonRepository(db).list_reasons(filters)
    return [StrategyFailureReasonRead.model_validate(reason) for reason in reasons]


@router.get("/strategy-version-lineage")
@router.get("/strategy-versions/lineage")
def list_strategy_version_lineage(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    versions = StrategyRepository(db).list_versions(limit=limit)
    return [
        {
            "id": version.id,
            "strategy_id": version.strategy_id,
            "parent_version_id": version.parent_version_id,
            "version_number": version.version_number,
            "change_summary": version.change_summary,
            "diff_snapshot": version.diff_snapshot,
            "has_parent": version.parent_version_id is not None,
            "created_at": version.created_at.isoformat() if version.created_at else None,
        }
        for version in versions
    ]
