from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.dry_run_readiness import DryRunReadinessReport, DryRunReadinessRequest
from app.services.dry_run_readiness import DryRunReadinessService


router = APIRouter(prefix="/api/dry-run", tags=["dry-run"])


@router.post("/readiness", response_model=DryRunReadinessReport)
def check_dry_run_readiness(
    payload: DryRunReadinessRequest,
    db: Session = Depends(get_db),
) -> DryRunReadinessReport:
    report = DryRunReadinessService(db).evaluate(payload)
    if report is None:
        raise HTTPException(status_code=404, detail="strategy version not found")
    return report
