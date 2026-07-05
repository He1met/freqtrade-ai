#!/usr/bin/env python3
"""Offline Phase 6 live-candidate governance smoke check.

The smoke path uses LiveCandidateProfile fixtures, offline evidence manifests,
manual approval records, deployment governance records, rollback plans, and
read-only monitoring snapshots. It does not start Freqtrade, connect to an
exchange, download market data, place orders, read real credentials, or perform
production deployment.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"
if (
    os.environ.get("FREQTRADE_AI_PHASE6_SMOKE_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_PHASE6_SMOKE_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.schemas.live_candidate import LiveCandidateProfile  # noqa: E402
from app.services.live_candidate_approval import (  # noqa: E402
    apply_live_candidate_approval_transition,
    create_live_candidate_approval_record,
)
from app.services.live_candidate_deployment import (  # noqa: E402
    LiveCandidateDeploymentStateError,
    create_live_candidate_deployment_record,
    record_live_candidate_deployment_result,
)
from app.services.live_candidate_monitoring import (  # noqa: E402
    LiveCandidateMonitoringParser,
    LiveCandidateMonitoringSnapshotService,
)
from app.services.live_candidate_preflight import run_live_candidate_preflight  # noqa: E402


FIXED_SUBMITTED_AT = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)
FIXED_APPROVED_AT_A = datetime(2026, 7, 5, 10, 5, tzinfo=timezone.utc)
FIXED_APPROVED_AT_B = datetime(2026, 7, 5, 10, 10, tzinfo=timezone.utc)
FIXED_PLANNED_AT = datetime(2026, 7, 5, 10, 15, tzinfo=timezone.utc)
FIXED_RESULT_AT = datetime(2026, 7, 5, 10, 45, tzinfo=timezone.utc)
FIXED_MONITORING_NOW = datetime(2026, 7, 5, 11, 0, tzinfo=timezone.utc)


@dataclass
class Phase6SmokeContext:
    tmp_dir: Path
    profile_payload: Optional[dict[str, Any]] = None
    profile: Optional[LiveCandidateProfile] = None
    evidence_refs: set[str] = field(default_factory=set)
    passing_preflight: Any = None
    pending_approval: Any = None
    approved_approval: Any = None
    planned_deployment: Any = None
    recorded_deployment: Any = None
    monitoring_snapshot: Any = None
    blocked_paths: dict[str, str] = field(default_factory=dict)
    failed_paths: dict[str, str] = field(default_factory=dict)


def log(message: str) -> None:
    print(message, flush=True)


def run_step(name: str, action: Callable[[], None]) -> None:
    log(f"[RUN] {name}")
    try:
        action()
    except Exception as exc:
        log(f"[FAIL] {name}: {exc}")
        traceback.print_exc()
        raise
    log(f"[PASS] {name}")


def prepare_tmp_dir(tmp_dir: Path) -> None:
    if tmp_dir in {Path("/"), REPO_ROOT, BACKEND_PATH, REPO_ROOT.parent}:
        raise RuntimeError(f"Refusing unsafe smoke tmp-dir: {tmp_dir}")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)


def create_context(tmp_dir: Path) -> Phase6SmokeContext:
    return Phase6SmokeContext(tmp_dir=tmp_dir)


def profile_payload() -> dict[str, Any]:
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


def rollback_plan_payload() -> dict[str, Any]:
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


def available_evidence_refs(payload: dict[str, Any]) -> set[str]:
    evidence = payload["evidence"]
    refs = {
        evidence["backtest"]["artifact_ref"],
        evidence["hyperopt"]["artifact_ref"],
        evidence["dry_run"]["artifact_ref"],
    }
    refs.add(evidence["backtest"]["summary_path"])
    return refs


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_fixture_inputs(context: Phase6SmokeContext) -> None:
    payload = profile_payload()
    refs = available_evidence_refs(payload)
    for ref in sorted(refs):
        target = context.tmp_dir / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix == ".md":
            target.write_text("# Phase 6 fixture evidence summary\n", encoding="utf-8")
            continue
        write_json(
            target,
            {
                "fixture": True,
                "ref": ref,
                "phase": "phase-6",
                "generated_for": "offline-governance-smoke",
            },
        )

    manifest_path = context.tmp_dir / "reports/governance/phase6_evidence_manifest.json"
    write_json(
        manifest_path,
        {
            "available_evidence_refs": sorted(refs),
            "fixture_only": True,
            "safety": {
                "exchange_connection": False,
                "market_data_download": False,
                "live_trading": False,
                "real_orders": False,
            },
        },
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    context.profile_payload = payload
    context.evidence_refs = set(manifest["available_evidence_refs"])
    log(f"  evidence_manifest={manifest_path}")


def require_profile_payload(context: Phase6SmokeContext) -> dict[str, Any]:
    if context.profile_payload is None:
        raise RuntimeError("profile fixture must exist")
    return context.profile_payload


def require_passing_records(context: Phase6SmokeContext) -> tuple[LiveCandidateProfile, Any, Any]:
    if context.profile is None or context.passing_preflight is None or context.approved_approval is None:
        raise RuntimeError("passing profile, preflight, and approval records must exist")
    return context.profile, context.passing_preflight, context.approved_approval


def run_passing_preflight(context: Phase6SmokeContext) -> None:
    payload = require_profile_payload(context)
    profile = LiveCandidateProfile.model_validate(payload)
    preflight = run_live_candidate_preflight(profile, context.evidence_refs)
    if preflight.status != "APPROVED_FOR_REVIEW" or not preflight.can_enter_human_approval:
        raise RuntimeError(f"Expected APPROVED_FOR_REVIEW preflight, got {preflight.to_audit_summary()}")

    context.profile = profile
    context.passing_preflight = preflight
    write_json(
        context.tmp_dir / "reports/governance/phase6_candidate_risk_summary.json",
        preflight.to_audit_summary(),
    )
    log(f"  preflight_status={preflight.status}")


def run_manual_approval_success(context: Phase6SmokeContext) -> None:
    if context.profile is None or context.passing_preflight is None:
        raise RuntimeError("profile and passing preflight must exist")

    pending = create_live_candidate_approval_record(
        context.profile,
        context.passing_preflight,
        submitted_by={"actor_id": "risk-reviewer", "role": "risk-owner"},
        risk_summary_ref="reports/governance/phase6_candidate_risk_summary.json",
        submitted_at=FIXED_SUBMITTED_AT,
    )
    first = apply_live_candidate_approval_transition(
        pending,
        decision="APPROVE",
        actor={"actor_id": "risk-reviewer-a", "role": "risk-owner"},
        basis="Reviewed passing preflight and offline evidence summary.",
        decided_at=FIXED_APPROVED_AT_A,
    )
    approved = apply_live_candidate_approval_transition(
        first,
        decision="APPROVE",
        actor={"actor_id": "maintainer-b", "role": "maintainer"},
        basis="Confirmed candidate boundaries and manual approval record.",
        decided_at=FIXED_APPROVED_AT_B,
    )
    if pending.status != "PENDING_HUMAN_APPROVAL" or approved.status != "APPROVED_FOR_DEPLOYMENT_RECORD":
        raise RuntimeError("Manual approval state machine did not reach the expected governance states")

    context.pending_approval = pending
    context.approved_approval = approved
    write_json(
        context.tmp_dir / "reports/governance/phase6_candidate_approval.json",
        approved.to_audit_summary(),
    )
    log(f"  approval_status={approved.status}")


def run_deployment_record_success(context: Phase6SmokeContext) -> None:
    profile, _, approved = require_passing_records(context)
    planned = create_live_candidate_deployment_record(
        profile,
        approved,
        rollback_plan_payload(),
        planned_environment="production-candidate",
        planned_by={"actor_id": "release-owner", "role": "operator"},
        approval_record_ref="reports/governance/phase6_candidate_approval.json",
        planned_at=FIXED_PLANNED_AT,
    )
    recorded = record_live_candidate_deployment_result(
        planned,
        result_status="MANUAL_SUCCESS",
        recorded_by={"actor_id": "release-owner", "role": "operator"},
        summary="Manual result was recorded outside this system and audited here.",
        recorded_at=FIXED_RESULT_AT,
        evidence_ref="reports/governance/phase6_manual_result.json",
    )
    if planned.status != "PLANNED" or recorded.status != "MANUAL_RESULT_RECORDED":
        raise RuntimeError("Deployment governance records did not reach the expected audit states")

    context.planned_deployment = planned
    context.recorded_deployment = recorded
    write_json(
        context.tmp_dir / "reports/governance/phase6_deployment_record.json",
        recorded.to_audit_summary(),
    )
    log(f"  deployment_statuses={planned.status},{recorded.status}")


def run_readonly_monitoring_success(context: Phase6SmokeContext) -> None:
    if context.profile is None or context.planned_deployment is None:
        raise RuntimeError("profile and planned deployment record must exist")

    monitoring_path = context.tmp_dir / "reports/governance/phase6_live_candidate_monitoring.json"
    payload = {
        "status": "ok",
        "profile_name": context.profile.name,
        "profile_hash": context.profile.profile_hash(),
        "deployment_record_id": context.planned_deployment.record_id,
        "deployment_status": context.planned_deployment.status,
        "approval_status": context.planned_deployment.approval_status,
        "preflight_status": context.planned_deployment.preflight_status,
        "pair": context.profile.pair,
        "timeframe": context.profile.timeframe,
        "source_ref": "reports/governance/phase6_live_candidate_monitoring.json",
        "last_updated": "2026-07-05T10:55:00Z",
        "alerts": [
            {
                "alert_id": "manual-review-green",
                "severity": "info",
                "message": "Offline governance fixture is readable.",
                "last_updated": "2026-07-05T10:55:00Z",
                "evidence_ref": "reports/governance/phase6_live_candidate_monitoring.json",
            }
        ],
    }
    write_json(monitoring_path, payload)
    parser = LiveCandidateMonitoringParser(now_provider=lambda: FIXED_MONITORING_NOW)
    snapshot = LiveCandidateMonitoringSnapshotService(parser).snapshot_from_controlled_json(monitoring_path)
    if snapshot.status != "OK":
        raise RuntimeError(f"Expected OK monitoring snapshot, got {snapshot.to_readonly_summary()}")

    context.monitoring_snapshot = snapshot
    log(f"  monitoring_status={snapshot.status}")


def run_blocked_paths(context: Phase6SmokeContext) -> None:
    payload = require_profile_payload(context)

    missing_evidence_refs = set(context.evidence_refs)
    missing_evidence_refs.remove(payload["evidence"]["dry_run"]["artifact_ref"])
    missing_evidence = run_live_candidate_preflight(payload, missing_evidence_refs)
    if missing_evidence.status != "BLOCKED":
        raise RuntimeError(f"Expected missing evidence BLOCKED, got {missing_evidence.to_audit_summary()}")
    context.blocked_paths["missing_risk_evidence"] = missing_evidence.status

    if context.profile is None or context.pending_approval is None:
        raise RuntimeError("profile and pending approval must exist")
    blocked_deployment = create_live_candidate_deployment_record(
        context.profile,
        context.pending_approval,
        rollback_plan_payload(),
        planned_environment="production-candidate",
        planned_by={"actor_id": "release-owner", "role": "operator"},
        approval_record_ref="reports/governance/phase6_candidate_approval.json",
    )
    if blocked_deployment.status != "BLOCKED":
        raise RuntimeError(f"Expected missing approval BLOCKED, got {blocked_deployment.to_audit_summary()}")
    context.blocked_paths["missing_manual_approval"] = blocked_deployment.status

    _, _, approved = require_passing_records(context)
    try:
        create_live_candidate_deployment_record(
            context.profile,
            approved,
            None,
            planned_environment="production-candidate",
            planned_by={"actor_id": "release-owner", "role": "operator"},
            approval_record_ref="reports/governance/phase6_candidate_approval.json",
        )
    except LiveCandidateDeploymentStateError as exc:
        context.blocked_paths["missing_rollback_plan"] = "BLOCKED"
        write_json(
            context.tmp_dir / "reports/governance/phase6_missing_rollback_blocked.json",
            {"status": "BLOCKED", "blocked_reason": str(exc)},
        )
    else:
        raise RuntimeError("Missing rollback plan should fail closed")

    parser = LiveCandidateMonitoringParser(now_provider=lambda: FIXED_MONITORING_NOW)
    control_blocked = parser.parse_controlled_json_payload(
        {
            "status": "ok",
            "profile_name": context.profile.name,
            "profile_hash": context.profile.profile_hash(),
            "start": True,
            "last_updated": "2026-07-05T10:55:00Z",
        }
    )
    if control_blocked.status != "BLOCKED":
        summary = control_blocked.to_readonly_summary()
        raise RuntimeError(f"Expected control-shaped monitoring input BLOCKED, got {summary}")
    context.blocked_paths["monitoring_control_input"] = control_blocked.status
    log("  blocked_paths=missing_risk_evidence,missing_manual_approval,missing_rollback_plan,monitoring_control_input")


def run_failed_path(context: Phase6SmokeContext) -> None:
    payload = copy.deepcopy(require_profile_payload(context))
    payload["risk_limits"]["max_drawdown_pct"] = 45.0
    payload["risk_limits"]["max_daily_loss_pct"] = 12.0
    payload["locked_variables"]["max_drawdown_pct"] = 45.0
    payload["locked_variables"]["max_daily_loss_pct"] = 12.0

    failed = run_live_candidate_preflight(payload, context.evidence_refs)
    if failed.status != "FAILED":
        raise RuntimeError(f"Expected risk threshold FAILED, got {failed.to_audit_summary()}")
    context.failed_paths["risk_thresholds"] = failed.status
    write_json(
        context.tmp_dir / "reports/governance/phase6_failed_risk_thresholds.json",
        failed.to_audit_summary(),
    )
    log(f"  failed_paths=risk_thresholds:{failed.status}")


def write_summary(context: Phase6SmokeContext) -> None:
    if (
        context.profile is None
        or context.passing_preflight is None
        or context.approved_approval is None
        or context.planned_deployment is None
        or context.recorded_deployment is None
        or context.monitoring_snapshot is None
    ):
        raise RuntimeError("all success-path records must exist before writing summary")
    required_blocked = {"missing_risk_evidence", "missing_manual_approval", "missing_rollback_plan"}
    if not required_blocked.issubset(context.blocked_paths):
        raise RuntimeError(f"missing required blocked paths: {required_blocked - set(context.blocked_paths)}")
    if context.failed_paths.get("risk_thresholds") != "FAILED":
        raise RuntimeError("risk threshold failed path was not recorded")

    summary_path = context.tmp_dir / "phase6-smoke-summary.json"
    write_json(
        summary_path,
        {
            "status": "PASS",
            "profile_name": context.profile.name,
            "success_path": {
                "preflight": context.passing_preflight.status,
                "approval": context.approved_approval.status,
                "deployment_planned": context.planned_deployment.status,
                "deployment_manual_result": context.recorded_deployment.status,
                "monitoring": context.monitoring_snapshot.status,
            },
            "blocked_paths": context.blocked_paths,
            "failed_paths": context.failed_paths,
            "summary": (
                "Offline fixture governance chain passed. BLOCKED and FAILED paths "
                "remain fail-closed and do not authorize live trading or deployment."
            ),
            "safety": {
                "real_freqtrade": False,
                "exchange_connection": False,
                "market_data_download": False,
                "live_trading": False,
                "real_orders": False,
                "real_credentials_read": False,
                "credentials_persisted": False,
                "production_deployment": False,
                "deployment_execution": False,
                "freqtrade_source_modified": False,
            },
        },
    )
    log(f"  summary_path={summary_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the offline Phase 6 live-candidate governance smoke check.")
    parser.add_argument("--offline", action="store_true", help="Required; confirms offline fixture mode.")
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("/tmp/freqtrade-ai-phase6-smoke"),
        help="Temporary workspace for generated fixture evidence and governance records.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.offline:
        log("[FAIL] --offline is required; this smoke command only supports fixture mode.")
        return 2

    tmp_dir = args.tmp_dir.expanduser().resolve()
    prepare_tmp_dir(tmp_dir)
    os.chdir(tmp_dir)
    log(f"[INFO] tmp_dir={tmp_dir}")
    log(
        "[INFO] mode=offline-fixture; no real Freqtrade, exchange connection, "
        "market-data download, live trading, real orders, credential reads, "
        "or production deployment execution"
    )

    context = create_context(tmp_dir)
    run_step("write fixture evidence manifest and candidate profile", lambda: write_fixture_inputs(context))
    run_step("validate LiveCandidateProfile and passing preflight", lambda: run_passing_preflight(context))
    run_step("record manual approval state machine", lambda: run_manual_approval_success(context))
    run_step("record deployment governance and rollback plan", lambda: run_deployment_record_success(context))
    run_step("parse read-only monitoring snapshot", lambda: run_readonly_monitoring_success(context))
    run_step("verify fail-closed BLOCKED paths", lambda: run_blocked_paths(context))
    run_step("verify FAILED risk threshold path", lambda: run_failed_path(context))
    run_step("write Phase 6 smoke summary", lambda: write_summary(context))

    log("[PASS] Phase 6 smoke completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
