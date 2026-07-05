from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Union

from app.schemas.live_candidate import (
    LiveCandidateApprovalActor,
    LiveCandidateApprovalRecord,
    LiveCandidateDeploymentEnvironment,
    LiveCandidateDeploymentRecord,
    LiveCandidateDeploymentResult,
    LiveCandidateDeploymentResultStatus,
    LiveCandidateDeploymentStatus,
    LiveCandidateProfile,
    LiveCandidateRollbackPlan,
)


class LiveCandidateDeploymentStateError(ValueError):
    """Raised when a deployment governance record would violate Phase 6 boundaries."""


def create_live_candidate_deployment_record(
    profile_payload: Union[LiveCandidateProfile, dict[str, Any]],
    approval_payload: Union[LiveCandidateApprovalRecord, dict[str, Any]],
    rollback_plan_payload: Union[LiveCandidateRollbackPlan, dict[str, Any], None],
    planned_environment: LiveCandidateDeploymentEnvironment,
    planned_by: Union[LiveCandidateApprovalActor, dict[str, Any]],
    approval_record_ref: str,
    planned_at: datetime | None = None,
    record_id: str | None = None,
) -> LiveCandidateDeploymentRecord:
    if not rollback_plan_payload:
        raise LiveCandidateDeploymentStateError("rollback plan is required before creating a deployment record")

    profile = (
        profile_payload
        if isinstance(profile_payload, LiveCandidateProfile)
        else LiveCandidateProfile.model_validate(profile_payload)
    )
    approval = (
        approval_payload
        if isinstance(approval_payload, LiveCandidateApprovalRecord)
        else LiveCandidateApprovalRecord.model_validate(approval_payload)
    )
    rollback_plan = (
        rollback_plan_payload
        if isinstance(rollback_plan_payload, LiveCandidateRollbackPlan)
        else LiveCandidateRollbackPlan.model_validate(rollback_plan_payload)
    )
    actor = (
        planned_by
        if isinstance(planned_by, LiveCandidateApprovalActor)
        else LiveCandidateApprovalActor.model_validate(planned_by)
    )

    profile_hash = profile.profile_hash()
    blockers: list[str] = []
    if approval.profile_name != profile.name or approval.profile_hash != profile_hash:
        blockers.append("approval record does not match candidate profile")
    if approval.preflight_status != "APPROVED_FOR_REVIEW":
        blockers.append("approval record does not reference passing preflight")
    if not approval.can_create_deployment_record:
        blockers.append("manual approval is not complete for deployment record creation")

    status: LiveCandidateDeploymentStatus = "BLOCKED" if blockers else "PLANNED"
    return LiveCandidateDeploymentRecord(
        record_id=record_id or f"deployment-record-{profile_hash[:12]}",
        status=status,
        profile_name=profile.name,
        profile_hash=profile_hash,
        approval_record_ref=approval_record_ref,
        approval_status=approval.status,
        preflight_status=approval.preflight_status,
        planned_environment=planned_environment,
        planned_by=actor,
        planned_at=planned_at or datetime.now(timezone.utc),
        rollback_plan=rollback_plan,
        blockers=blockers,
    )


def record_live_candidate_deployment_result(
    record_payload: Union[LiveCandidateDeploymentRecord, dict[str, Any]],
    result_status: LiveCandidateDeploymentResultStatus,
    recorded_by: Union[LiveCandidateApprovalActor, dict[str, Any]],
    summary: str,
    recorded_at: datetime | None = None,
    evidence_ref: str | None = None,
) -> LiveCandidateDeploymentRecord:
    record = (
        record_payload
        if isinstance(record_payload, LiveCandidateDeploymentRecord)
        else LiveCandidateDeploymentRecord.model_validate(record_payload)
    )
    if not record.can_record_manual_result:
        raise LiveCandidateDeploymentStateError("manual deployment result can only be recorded for planned records")

    actor = (
        recorded_by
        if isinstance(recorded_by, LiveCandidateApprovalActor)
        else LiveCandidateApprovalActor.model_validate(recorded_by)
    )
    result = LiveCandidateDeploymentResult(
        status=result_status,
        recorded_by=actor,
        recorded_at=recorded_at or datetime.now(timezone.utc),
        summary=summary,
        evidence_ref=evidence_ref,
    )

    next_status: LiveCandidateDeploymentStatus = "MANUAL_RESULT_RECORDED"
    if result_status == "ROLLBACK_RECORDED":
        next_status = "ROLLBACK_RECORDED"
    if result_status == "CANCELLED":
        next_status = "CANCELLED"

    record_data = record.model_dump(mode="python")
    record_data.update({"status": next_status, "result": result})
    return LiveCandidateDeploymentRecord.model_validate(record_data)
