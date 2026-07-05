from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Union

from app.schemas.live_candidate import (
    LiveCandidateApprovalActor,
    LiveCandidateApprovalDecision,
    LiveCandidateApprovalDecisionType,
    LiveCandidateApprovalRecord,
    LiveCandidatePreflightResult,
    LiveCandidateProfile,
)


class LiveCandidateApprovalStateError(ValueError):
    """Raised when a manual approval transition violates Phase 6 governance."""


def create_live_candidate_approval_record(
    profile_payload: Union[LiveCandidateProfile, dict[str, Any]],
    preflight_payload: Union[LiveCandidatePreflightResult, dict[str, Any]],
    submitted_by: Union[LiveCandidateApprovalActor, dict[str, Any]],
    risk_summary_ref: str,
    submitted_at: datetime | None = None,
) -> LiveCandidateApprovalRecord:
    profile = (
        profile_payload
        if isinstance(profile_payload, LiveCandidateProfile)
        else LiveCandidateProfile.model_validate(profile_payload)
    )
    preflight = (
        preflight_payload
        if isinstance(preflight_payload, LiveCandidatePreflightResult)
        else LiveCandidatePreflightResult.model_validate(preflight_payload)
    )
    actor = (
        submitted_by
        if isinstance(submitted_by, LiveCandidateApprovalActor)
        else LiveCandidateApprovalActor.model_validate(submitted_by)
    )

    blockers = list(preflight.blockers)
    failures = list(preflight.failures)
    status = "PENDING_HUMAN_APPROVAL"
    profile_hash = profile.profile_hash()
    if preflight.status != "APPROVED_FOR_REVIEW":
        status = "BLOCKED_BY_PREFLIGHT"
    if preflight.profile_hash != profile_hash:
        status = "BLOCKED_BY_PREFLIGHT"
        blockers.append("preflight result does not match candidate profile")

    return LiveCandidateApprovalRecord(
        status=status,
        profile_name=profile.name,
        profile_hash=profile_hash,
        risk_summary_ref=risk_summary_ref,
        preflight_status=preflight.status,
        submitted_by=actor,
        submitted_at=submitted_at or datetime.now(timezone.utc),
        required_approvals=profile.approval.minimum_approvers,
        blockers=blockers,
        failures=failures,
    )


def apply_live_candidate_approval_transition(
    record_payload: Union[LiveCandidateApprovalRecord, dict[str, Any]],
    decision: LiveCandidateApprovalDecisionType,
    actor: Union[LiveCandidateApprovalActor, dict[str, Any]],
    basis: str,
    decided_at: datetime | None = None,
    revocation_reason: str | None = None,
) -> LiveCandidateApprovalRecord:
    record = (
        record_payload
        if isinstance(record_payload, LiveCandidateApprovalRecord)
        else LiveCandidateApprovalRecord.model_validate(record_payload)
    )
    actor_record = (
        actor if isinstance(actor, LiveCandidateApprovalActor) else LiveCandidateApprovalActor.model_validate(actor)
    )

    _validate_transition(record, decision, actor_record)
    next_decision = LiveCandidateApprovalDecision(
        decision=decision,
        actor=actor_record,
        decided_at=decided_at or datetime.now(timezone.utc),
        basis=basis,
        revocation_reason=revocation_reason,
    )

    decisions = [*record.decisions, next_decision]
    next_status = _next_status(record, decision, decisions)
    record_data = record.model_dump(mode="python")
    record_data.update(
        {
            "status": next_status,
            "decisions": decisions,
            "revocation_reason": revocation_reason if decision == "REVOKE" else None,
        }
    )
    return LiveCandidateApprovalRecord.model_validate(record_data)


def _validate_transition(
    record: LiveCandidateApprovalRecord,
    decision: LiveCandidateApprovalDecisionType,
    actor: LiveCandidateApprovalActor,
) -> None:
    if record.status == "BLOCKED_BY_PREFLIGHT":
        raise LiveCandidateApprovalStateError("blocked preflight records cannot receive approval decisions")

    if decision in {"APPROVE", "REJECT"} and record.status != "PENDING_HUMAN_APPROVAL":
        raise LiveCandidateApprovalStateError("approve or reject is only allowed while human approval is pending")

    if decision == "REVOKE" and record.status != "APPROVED_FOR_DEPLOYMENT_RECORD":
        raise LiveCandidateApprovalStateError("revoke is only allowed after human approval is complete")

    if decision == "EXPIRE" and record.status != "PENDING_HUMAN_APPROVAL":
        raise LiveCandidateApprovalStateError("expire is only allowed while human approval is pending")

    if decision == "APPROVE":
        approver_ids = {
            existing.actor.actor_id
            for existing in record.decisions
            if existing.decision == "APPROVE"
        }
        if actor.actor_id in approver_ids:
            raise LiveCandidateApprovalStateError("the same actor cannot approve a candidate twice")


def _next_status(
    record: LiveCandidateApprovalRecord,
    decision: LiveCandidateApprovalDecisionType,
    decisions: list[LiveCandidateApprovalDecision],
) -> str:
    if decision == "REJECT":
        return "REJECTED"
    if decision == "REVOKE":
        return "REVOKED"
    if decision == "EXPIRE":
        return "EXPIRED"

    approval_count = len(
        {
            item.actor.actor_id
            for item in decisions
            if item.decision == "APPROVE"
        }
    )
    if approval_count >= record.required_approvals:
        return "APPROVED_FOR_DEPLOYMENT_RECORD"
    return "PENDING_HUMAN_APPROVAL"
