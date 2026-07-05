from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.dry_run_control import (
    DryRunControlReport,
    DryRunControlStartRequest,
    DryRunControlStopRequest,
)
from app.schemas.dry_run_readiness import DryRunReadinessReport, DryRunReadinessRequest
from app.schemas.dry_run_status import DryRunStatusSnapshot
from app.services.dry_run_control import DryRunControlService
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


@router.post("/control/start", response_model=DryRunControlReport)
def start_controlled_dry_run(
    payload: DryRunControlStartRequest,
    db: Session = Depends(get_db),
) -> DryRunControlReport:
    report = DryRunControlService(db).start(payload)
    if report is None:
        raise HTTPException(status_code=404, detail="strategy version not found")
    return report


@router.post("/control/stop", response_model=DryRunControlReport)
def stop_controlled_dry_run(payload: DryRunControlStopRequest) -> DryRunControlReport:
    return DryRunControlService().stop(payload)


@router.get("/status", response_model=DryRunStatusSnapshot)
def dry_run_status() -> DryRunStatusSnapshot:
    return DryRunControlService().snapshot()


@router.get("/management")
def dry_run_management() -> dict:
    return DryRunControlService().management()
