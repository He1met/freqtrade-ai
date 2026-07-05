from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional, Union

from pydantic import ValidationError

from app.schemas.live_candidate import (
    LiveCandidatePreflightResult,
    LiveCandidatePreflightStatus,
    LiveCandidateProfile,
    LiveCandidateRiskCheck,
)


MAX_REVIEW_DRAWDOWN_PCT = 30.0
MAX_REVIEW_DAILY_LOSS_PCT = 10.0
MAX_REVIEW_POSITION_PCT = 50.0
MAX_REVIEW_OPEN_TRADES = 10


def run_live_candidate_preflight(
    payload: Union[LiveCandidateProfile, dict[str, Any]],
    available_evidence_refs: Optional[Iterable[str]] = None,
) -> LiveCandidatePreflightResult:
    try:
        profile = (
            payload
            if isinstance(payload, LiveCandidateProfile)
            else LiveCandidateProfile.model_validate(payload)
        )
    except ValidationError as exc:
        blockers = _safe_validation_blockers(exc)
        return _result(
            status="BLOCKED",
            profile=None,
            checks=[
                LiveCandidateRiskCheck(
                    name="profile_validation",
                    status="BLOCKED",
                    summary="Profile input failed governance validation before risk preflight.",
                    blockers=blockers,
                )
            ],
            blockers=blockers,
        )

    checks = [
        _strategy_check(profile),
        _evidence_check(profile, available_evidence_refs),
        _capital_limits_check(profile),
        _trading_scope_check(profile),
        _risk_limits_check(profile),
        _abnormal_stop_check(profile),
        _approval_boundary_check(profile),
        _safety_boundary_check(profile),
    ]

    blockers = [item for check in checks for item in check.blockers]
    failures = [item for check in checks for item in check.failures]

    if blockers:
        status = "BLOCKED"
    elif failures:
        status = "FAILED"
    else:
        status = "APPROVED_FOR_REVIEW"

    return _result(
        status=status,
        profile=profile,
        checks=checks,
        blockers=blockers,
        failures=failures,
    )


def _strategy_check(profile: LiveCandidateProfile) -> LiveCandidateRiskCheck:
    return LiveCandidateRiskCheck(
        name="strategy_version",
        status="PASS",
        summary="Strategy version is pinned for governance review.",
        details={
            "strategy": profile.strategy.name,
            "strategy_version_id": profile.strategy.version_id,
        },
    )


def _evidence_check(
    profile: LiveCandidateProfile,
    available_evidence_refs: Optional[Iterable[str]],
) -> LiveCandidateRiskCheck:
    required_refs = _required_evidence_refs(profile)
    if available_evidence_refs is None:
        return LiveCandidateRiskCheck(
            name="offline_evidence",
            status="BLOCKED",
            summary="Offline evidence availability manifest is required before human approval review.",
            blockers=["offline evidence availability manifest is missing"],
            details={"required_refs": sorted(required_refs)},
        )

    available = {ref.strip() for ref in available_evidence_refs if ref and ref.strip()}
    missing = sorted(required_refs - available)
    if missing:
        return LiveCandidateRiskCheck(
            name="offline_evidence",
            status="BLOCKED",
            summary="Required offline evidence references are missing from the availability manifest.",
            blockers=["required offline evidence is missing"],
            details={"missing_refs": missing},
        )

    return LiveCandidateRiskCheck(
        name="offline_evidence",
        status="PASS",
        summary="Backtest, Hyperopt, and dry-run evidence are present in the offline manifest.",
        details={"required_refs": sorted(required_refs)},
    )


def _capital_limits_check(profile: LiveCandidateProfile) -> LiveCandidateRiskCheck:
    failures = []
    limits = profile.capital_limits
    if limits.max_open_trades > MAX_REVIEW_OPEN_TRADES:
        failures.append("max_open_trades exceeds governance review threshold")
    if limits.max_total_exposure > limits.max_stake_amount * limits.max_open_trades:
        failures.append("max_total_exposure exceeds max_stake_amount multiplied by max_open_trades")

    return LiveCandidateRiskCheck(
        name="capital_limits",
        status="FAILED" if failures else "PASS",
        summary="Capital limits are within governance review thresholds."
        if not failures
        else "Capital limits exceed governance review thresholds.",
        failures=failures,
        details={
            "stake_currency": limits.stake_currency,
            "max_stake_amount": limits.max_stake_amount,
            "max_total_exposure": limits.max_total_exposure,
            "max_open_trades": limits.max_open_trades,
        },
    )


def _trading_scope_check(profile: LiveCandidateProfile) -> LiveCandidateRiskCheck:
    return LiveCandidateRiskCheck(
        name="trading_scope",
        status="PASS",
        summary="Pair, timeframe, exchange, and market type are explicitly scoped.",
        details={
            "pair": profile.pair,
            "timeframe": profile.timeframe,
            "exchange": profile.exchange.name,
            "market_type": profile.exchange.market_type,
        },
    )


def _risk_limits_check(profile: LiveCandidateProfile) -> LiveCandidateRiskCheck:
    failures = []
    limits = profile.risk_limits
    if limits.max_drawdown_pct > MAX_REVIEW_DRAWDOWN_PCT:
        failures.append("max_drawdown_pct exceeds governance review threshold")
    if limits.max_daily_loss_pct > MAX_REVIEW_DAILY_LOSS_PCT:
        failures.append("max_daily_loss_pct exceeds governance review threshold")
    if limits.max_position_pct > MAX_REVIEW_POSITION_PCT:
        failures.append("max_position_pct exceeds governance review threshold")

    return LiveCandidateRiskCheck(
        name="risk_limits",
        status="FAILED" if failures else "PASS",
        summary="Drawdown, daily loss, and position limits are within review thresholds."
        if not failures
        else "Risk limits exceed governance review thresholds.",
        failures=failures,
        details={
            "max_drawdown_pct": limits.max_drawdown_pct,
            "max_daily_loss_pct": limits.max_daily_loss_pct,
            "max_position_pct": limits.max_position_pct,
        },
    )


def _abnormal_stop_check(profile: LiveCandidateProfile) -> LiveCandidateRiskCheck:
    return LiveCandidateRiskCheck(
        name="abnormal_stop_conditions",
        status="PASS",
        summary="Stop-loss and emergency-stop requirements are locked on.",
        details={
            "stop_loss_required": profile.risk_limits.stop_loss_required,
            "emergency_stop_required": profile.risk_limits.emergency_stop_required,
        },
    )


def _approval_boundary_check(profile: LiveCandidateProfile) -> LiveCandidateRiskCheck:
    return LiveCandidateRiskCheck(
        name="human_approval_boundary",
        status="PASS",
        summary="Human approval is required before any later deployment decision.",
        details={
            "requires_human_approval": profile.approval.requires_human_approval,
            "minimum_approvers": profile.approval.minimum_approvers,
            "approval_scope": profile.approval.approval_scope,
        },
    )


def _safety_boundary_check(profile: LiveCandidateProfile) -> LiveCandidateRiskCheck:
    return LiveCandidateRiskCheck(
        name="phase6_safety_boundary",
        status="PASS",
        summary="Profile remains governance-only and contains no live execution permission.",
        details=profile.safety.model_dump(mode="json"),
    )


def _required_evidence_refs(profile: LiveCandidateProfile) -> set[str]:
    refs = {
        profile.evidence.backtest.artifact_ref,
        profile.evidence.hyperopt.artifact_ref,
        profile.evidence.dry_run.artifact_ref,
    }
    for evidence_ref in (
        profile.evidence.backtest,
        profile.evidence.hyperopt,
        profile.evidence.dry_run,
    ):
        if evidence_ref.summary_path:
            refs.add(evidence_ref.summary_path)
    return refs


def _safe_validation_blockers(exc: ValidationError) -> list[str]:
    text = str(exc).lower()
    if "forbidden secret" in text:
        return ["secret-shaped input was rejected"]
    if "forbidden runtime key" in text or "allow_live" in text or "allow_real" in text:
        return ["Phase 6 safety boundary conflict was rejected"]
    if "evidence" in text:
        return ["required evidence is missing or invalid"]
    if "locked_variables" in text:
        return ["locked variables are missing or inconsistent"]
    return ["profile validation failed; preflight cannot continue"]


def _result(
    status: LiveCandidatePreflightStatus,
    profile: Optional[LiveCandidateProfile],
    checks: list[LiveCandidateRiskCheck],
    blockers: Optional[list[str]] = None,
    failures: Optional[list[str]] = None,
) -> LiveCandidatePreflightResult:
    blockers = blockers or []
    failures = failures or []
    return LiveCandidatePreflightResult(
        status=status,
        profile_name=profile.name if profile else None,
        profile_hash=profile.profile_hash() if profile else None,
        can_enter_human_approval=status == "APPROVED_FOR_REVIEW",
        summary=_summary_for_status(status),
        checks=checks,
        blockers=blockers,
        failures=failures,
    )


def _summary_for_status(status: LiveCandidatePreflightStatus) -> str:
    if status == "APPROVED_FOR_REVIEW":
        return (
            "Preflight passed and the candidate may enter human approval review; "
            "this is not live-trading or deployment authorization."
        )
    if status == "FAILED":
        return "Preflight failed governance risk thresholds and cannot enter human approval review."
    return "Preflight is blocked until required offline evidence and safe governance inputs are complete."
