import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.live_candidate import LiveCandidateApprovalRecord
from app.services.live_candidate_approval import (
    LiveCandidateApprovalStateError,
    apply_live_candidate_approval_transition,
    create_live_candidate_approval_record,
)
from app.services.live_candidate_preflight import run_live_candidate_preflight


def valid_profile_payload() -> dict:
    return {
        "name": "phase6-live-candidate-btc-15m",
        "description": "Candidate profile for governance review only.",
        "strategy": {
            "version_id": 321,
            "name": "MvpRsiStrategyOptimized",
            "file_path": "user_data/strategies/generated/MvpRsiStrategyOptimized.py",
        },
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "exchange": {
            "name": "okx",
            "market_type": "futures",
            "settlement_currency": "USDT",
        },
        "capital_limits": {
            "stake_currency": "USDT",
            "max_stake_amount": 100,
            "max_total_exposure": 300,
            "max_open_trades": 3,
        },
        "risk_limits": {
            "max_drawdown_pct": 12.5,
            "max_daily_loss_pct": 3.0,
            "max_position_pct": 20.0,
            "stop_loss_required": True,
            "emergency_stop_required": True,
        },
        "evidence": {
            "backtest": {
                "artifact_ref": "reports/backtests/phase6_candidate_backtest.json",
                "source": "artifact",
                "passed": True,
                "summary_path": "reports/backtests/phase6_candidate_backtest_summary.md",
            },
            "hyperopt": {
                "artifact_ref": "reports/hyperopt/phase6_candidate_hyperopt.json",
                "source": "artifact",
                "passed": True,
            },
            "dry_run": {
                "artifact_ref": "reports/dry_run/phase6_candidate_manifest.json",
                "source": "manifest",
                "passed": True,
            },
        },
        "entry_conditions": {
            "require_backtest_evidence": True,
            "require_hyperopt_evidence": True,
            "require_dry_run_evidence": True,
            "require_risk_limits": True,
            "require_human_approval": True,
        },
        "approval": {
            "requires_human_approval": True,
            "minimum_approvers": 2,
            "approval_scope": "live-candidate-review",
        },
        "locked_variables": {
            "profile_name": "phase6-live-candidate-btc-15m",
            "strategy_version_id": 321,
            "strategy": "MvpRsiStrategyOptimized",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "exchange": "okx",
            "market_type": "futures",
            "stake_currency": "USDT",
            "max_stake_amount": 100,
            "max_total_exposure": 300,
            "max_open_trades": 3,
            "max_drawdown_pct": 12.5,
            "max_daily_loss_pct": 3.0,
            "max_position_pct": 20.0,
            "backtest_evidence": "reports/backtests/phase6_candidate_backtest.json",
            "hyperopt_evidence": "reports/hyperopt/phase6_candidate_hyperopt.json",
            "dry_run_evidence": "reports/dry_run/phase6_candidate_manifest.json",
            "requires_human_approval": True,
            "minimum_approvers": 2,
        },
        "tags": ["phase-6", "governance-only"],
    }


def available_evidence_refs(payload: dict) -> set[str]:
    evidence = payload["evidence"]
    return {
        evidence["backtest"]["artifact_ref"],
        evidence["backtest"]["summary_path"],
        evidence["hyperopt"]["artifact_ref"],
        evidence["dry_run"]["artifact_ref"],
    }


def approval_record() -> LiveCandidateApprovalRecord:
    payload = valid_profile_payload()
    preflight = run_live_candidate_preflight(payload, available_evidence_refs(payload))
    return create_live_candidate_approval_record(
        payload,
        preflight,
        submitted_by={"actor_id": "risk-reviewer", "role": "risk-owner"},
        risk_summary_ref="reports/governance/phase6_candidate_risk_summary.json",
        submitted_at=datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc),
    )


def test_approval_record_starts_pending_and_does_not_unlock_deployment_record() -> None:
    record = approval_record()
    summary = record.to_audit_summary()

    assert record.status == "PENDING_HUMAN_APPROVAL"
    assert record.preflight_status == "APPROVED_FOR_REVIEW"
    assert record.required_approvals == 2
    assert record.can_create_deployment_record is False
    assert summary["can_create_deployment_record"] is False
    assert "deployment execution authorization" in summary["safety_boundary"]


def test_manual_approvals_unlock_only_the_next_governance_record() -> None:
    record = approval_record()

    first = apply_live_candidate_approval_transition(
        record,
        decision="APPROVE",
        actor={"actor_id": "risk-reviewer-a", "role": "risk-owner"},
        basis="Reviewed passing preflight and offline evidence summary.",
        decided_at=datetime(2026, 7, 5, 10, 5, tzinfo=timezone.utc),
    )
    approved = apply_live_candidate_approval_transition(
        first,
        decision="APPROVE",
        actor={"actor_id": "maintainer-b", "role": "maintainer"},
        basis="Confirmed candidate boundaries and manual approval record.",
        decided_at=datetime(2026, 7, 5, 10, 10, tzinfo=timezone.utc),
    )

    assert first.status == "PENDING_HUMAN_APPROVAL"
    assert first.can_create_deployment_record is False
    assert approved.status == "APPROVED_FOR_DEPLOYMENT_RECORD"
    assert approved.can_create_deployment_record is True
    assert "start_live" not in json.dumps(approved.to_audit_summary())


def test_cannot_enter_deployment_record_state_without_human_approvals() -> None:
    record = approval_record()
    data = record.model_dump(mode="python")
    data["status"] = "APPROVED_FOR_DEPLOYMENT_RECORD"

    with pytest.raises(ValidationError, match="human approvals"):
        LiveCandidateApprovalRecord.model_validate(data)


def test_blocked_preflight_record_cannot_receive_approval_decision() -> None:
    payload = valid_profile_payload()
    preflight = run_live_candidate_preflight(payload)
    record = create_live_candidate_approval_record(
        payload,
        preflight,
        submitted_by={"actor_id": "risk-reviewer", "role": "risk-owner"},
        risk_summary_ref="reports/governance/phase6_candidate_risk_summary.json",
    )

    assert record.status == "BLOCKED_BY_PREFLIGHT"
    assert record.can_create_deployment_record is False
    with pytest.raises(LiveCandidateApprovalStateError, match="blocked preflight"):
        apply_live_candidate_approval_transition(
            record,
            decision="APPROVE",
            actor={"actor_id": "risk-reviewer-a", "role": "risk-owner"},
            basis="This should remain blocked.",
        )


def test_illegal_approval_transitions_are_rejected() -> None:
    record = approval_record()

    with pytest.raises(LiveCandidateApprovalStateError, match="revoke"):
        apply_live_candidate_approval_transition(
            record,
            decision="REVOKE",
            actor={"actor_id": "maintainer-b", "role": "maintainer"},
            basis="Cannot revoke before approval is complete.",
            revocation_reason="Approval was never complete.",
        )

    first = apply_live_candidate_approval_transition(
        record,
        decision="APPROVE",
        actor={"actor_id": "risk-reviewer-a", "role": "risk-owner"},
        basis="Reviewed passing preflight and offline evidence summary.",
    )
    with pytest.raises(LiveCandidateApprovalStateError, match="same actor"):
        apply_live_candidate_approval_transition(
            first,
            decision="APPROVE",
            actor={"actor_id": "risk-reviewer-a", "role": "risk-owner"},
            basis="Duplicate approval must be rejected.",
        )


def test_revoked_approval_records_require_and_store_revocation_reason() -> None:
    record = approval_record()
    first = apply_live_candidate_approval_transition(
        record,
        decision="APPROVE",
        actor={"actor_id": "risk-reviewer-a", "role": "risk-owner"},
        basis="Reviewed passing preflight and offline evidence summary.",
    )
    approved = apply_live_candidate_approval_transition(
        first,
        decision="APPROVE",
        actor={"actor_id": "maintainer-b", "role": "maintainer"},
        basis="Confirmed candidate boundaries and manual approval record.",
    )

    with pytest.raises(ValidationError, match="revocation_reason"):
        apply_live_candidate_approval_transition(
            approved,
            decision="REVOKE",
            actor={"actor_id": "maintainer-b", "role": "maintainer"},
            basis="Revocation requires a reason.",
        )

    revoked = apply_live_candidate_approval_transition(
        approved,
        decision="REVOKE",
        actor={"actor_id": "maintainer-b", "role": "maintainer"},
        basis="Risk summary was superseded by a newer review.",
        revocation_reason="Risk summary reference is no longer current.",
    )

    assert revoked.status == "REVOKED"
    assert revoked.can_create_deployment_record is False
    assert revoked.revocation_reason == "Risk summary reference is no longer current."


def test_approval_records_reject_secret_shaped_inputs() -> None:
    record = approval_record()

    with pytest.raises(ValidationError):
        apply_live_candidate_approval_transition(
            record,
            decision="APPROVE",
            actor={"actor_id": "reviewer-with-ghp_token", "role": "risk-owner"},
            basis="Reviewed passing preflight and offline evidence summary.",
        )
