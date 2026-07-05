import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.services.live_candidate_approval import (
    apply_live_candidate_approval_transition,
    create_live_candidate_approval_record,
)
from app.services.live_candidate_deployment import (
    LiveCandidateDeploymentStateError,
    create_live_candidate_deployment_record,
    record_live_candidate_deployment_result,
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


def rollback_plan_payload() -> dict:
    return {
        "plan_id": "phase6-manual-rollback-plan",
        "summary": "Manual governance rollback checklist for the candidate record.",
        "owner": {"actor_id": "release-owner", "role": "operator"},
        "trigger_conditions": [
            "Manual result review marks the candidate unavailable.",
            "Risk owner revokes the prior approval record.",
        ],
        "steps": [
            {
                "order": 1,
                "action": "Record the rollback decision in the governance log.",
                "expected_outcome": "Candidate status is auditable as rolled back.",
                "verification_ref": "reports/governance/phase6_rollback_review.md",
            },
            {
                "order": 2,
                "action": "Attach the follow-up review reference to the candidate record.",
                "expected_outcome": "Maintainers can trace the manual rollback decision.",
            },
        ],
        "verification_steps": [
            "Confirm the rollback decision is present in the audit summary.",
            "Confirm no runtime control field is present in the record.",
        ],
        "evidence_refs": ["reports/governance/phase6_rollback_review.md"],
    }


def pending_approval_record() -> tuple[dict, object]:
    payload = valid_profile_payload()
    preflight = run_live_candidate_preflight(payload, available_evidence_refs(payload))
    approval = create_live_candidate_approval_record(
        payload,
        preflight,
        submitted_by={"actor_id": "risk-reviewer", "role": "risk-owner"},
        risk_summary_ref="reports/governance/phase6_candidate_risk_summary.json",
        submitted_at=datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc),
    )
    return payload, approval


def approved_record() -> tuple[dict, object]:
    payload, approval = pending_approval_record()
    first = apply_live_candidate_approval_transition(
        approval,
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
    return payload, approved


def deployment_record():
    payload, approval = approved_record()
    return create_live_candidate_deployment_record(
        payload,
        approval,
        rollback_plan_payload(),
        planned_environment="production-candidate",
        planned_by={"actor_id": "release-owner", "role": "operator"},
        approval_record_ref="reports/governance/phase6_candidate_approval.json",
        planned_at=datetime(2026, 7, 5, 10, 15, tzinfo=timezone.utc),
    )


def test_deployment_record_stores_plan_without_execution() -> None:
    record = deployment_record()
    summary = record.to_audit_summary()

    assert record.status == "PLANNED"
    assert record.can_record_manual_result is True
    assert record.rollback_plan.steps[0].order == 1
    assert summary["planned_environment"] == "production-candidate"
    assert "does not execute deployment" in summary["safety_boundary"]
    assert "deploy_command" not in json.dumps(summary)
    assert "start_live" not in json.dumps(summary)


def test_missing_rollback_plan_fails_closed() -> None:
    payload, approval = approved_record()

    with pytest.raises(LiveCandidateDeploymentStateError, match="rollback plan is required"):
        create_live_candidate_deployment_record(
            payload,
            approval,
            None,
            planned_environment="production-candidate",
            planned_by={"actor_id": "release-owner", "role": "operator"},
            approval_record_ref="reports/governance/phase6_candidate_approval.json",
        )


def test_incomplete_approval_creates_blocked_record() -> None:
    payload, approval = pending_approval_record()

    record = create_live_candidate_deployment_record(
        payload,
        approval,
        rollback_plan_payload(),
        planned_environment="production-candidate",
        planned_by={"actor_id": "release-owner", "role": "operator"},
        approval_record_ref="reports/governance/phase6_candidate_approval.json",
    )

    assert record.status == "BLOCKED"
    assert record.can_record_manual_result is False
    assert any("manual approval is not complete" in blocker for blocker in record.blockers)


def test_manual_result_is_recorded_as_audit_update_only() -> None:
    record = deployment_record()

    result = record_live_candidate_deployment_result(
        record,
        result_status="MANUAL_SUCCESS",
        recorded_by={"actor_id": "release-owner", "role": "operator"},
        summary="Manual deployment result was recorded outside this system.",
        recorded_at=datetime(2026, 7, 5, 10, 45, tzinfo=timezone.utc),
        evidence_ref="reports/governance/phase6_manual_result.json",
    )
    summary = result.to_audit_summary()

    assert result.status == "MANUAL_RESULT_RECORDED"
    assert result.can_record_manual_result is False
    assert result.result is not None
    assert result.result.source == "manual-record"
    assert summary["result"]["status"] == "MANUAL_SUCCESS"
    assert "deployment_executor" not in json.dumps(summary)


def test_deployment_records_reject_secret_and_runtime_inputs() -> None:
    payload, approval = approved_record()
    bad_plan = rollback_plan_payload()
    bad_plan["steps"][0]["deploy_command"] = "freqtrade trade"

    with pytest.raises(ValidationError, match="forbidden runtime key"):
        create_live_candidate_deployment_record(
            payload,
            approval,
            bad_plan,
            planned_environment="production-candidate",
            planned_by={"actor_id": "release-owner", "role": "operator"},
            approval_record_ref="reports/governance/phase6_candidate_approval.json",
        )

    record = deployment_record()
    with pytest.raises(ValidationError):
        record_live_candidate_deployment_result(
            record,
            result_status="MANUAL_FAILED",
            recorded_by={"actor_id": "operator-ghp_token", "role": "operator"},
            summary="Manual result was rejected by review.",
        )
