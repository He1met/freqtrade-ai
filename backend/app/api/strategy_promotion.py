"""Read-only visibility for the Local-to-Demo strategy promotion gate."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import BacktestResult, StrategyCandidateApproval, StrategyScore, StrategyVersion
from app.services.strategy_promotion import (
    StrategyPromotionBlocked,
    promotion_candidate_digest,
)


router = APIRouter(prefix="/api", tags=["strategy-promotion"])


@router.get("/strategy-promotions/evaluate")
def evaluate_strategy_promotion(
    strategy_version_id: int = Query(gt=0),
    backtest_result_id: int = Query(gt=0),
    strategy_score_id: int = Query(gt=0),
    db: Session = Depends(get_db),
) -> dict:
    """Expose the exact Demo gate; ranking scores are never execution approval."""

    version = db.get(StrategyVersion, strategy_version_id)
    result = db.get(BacktestResult, backtest_result_id)
    score = db.get(StrategyScore, strategy_score_id)
    if version is None or result is None or score is None:
        raise HTTPException(status_code=404, detail="strategy promotion lineage not found")
    approval = db.scalars(
        select(StrategyCandidateApproval)
        .where(
            StrategyCandidateApproval.strategy_version_id == strategy_version_id,
            StrategyCandidateApproval.backtest_result_id == backtest_result_id,
            StrategyCandidateApproval.strategy_score_id == strategy_score_id,
        )
        .order_by(StrategyCandidateApproval.id.desc())
        .limit(1)
    ).first()
    base = {
        "execution_target_id": "OKX_DEMO",
        "database_ids": {
            "strategy_version_id": strategy_version_id,
            "backtest_result_id": backtest_result_id,
            "strategy_score_id": strategy_score_id,
        },
        "artifact_refs": {"backtest_result_path": result.result_path},
        "approval": _approval_payload(approval),
    }
    try:
        evidence, digest = promotion_candidate_digest(result, score, version)
    except StrategyPromotionBlocked as exc:
        return {
            **base,
            "status": "BLOCKED",
            "reason": str(exc),
            "policy": None,
            "evidence": None,
            "candidate_digest": None,
        }
    stale_reason = None
    if approval is not None and approval.status == "APPROVED":
        if approval.candidate_digest != digest or approval.promotion_evidence != evidence:
            stale_reason = "strategy or market evidence changed after approval"
    return {
        **base,
        "status": "STALE" if stale_reason else "ELIGIBLE",
        "reason": stale_reason,
        "policy": evidence["policy"],
        "evidence": evidence,
        "candidate_digest": digest,
    }


def _approval_payload(approval: StrategyCandidateApproval | None) -> dict | None:
    if approval is None:
        return None
    return {
        "database_id": approval.id,
        "status": approval.status,
        "requested_by": approval.requested_by,
        "decided_by": approval.decided_by,
        "reason": approval.decision_reason,
        "policy_version": approval.promotion_policy_version,
        "expires_at": approval.expires_at.isoformat(),
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
    }
