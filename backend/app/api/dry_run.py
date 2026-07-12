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
from app.services.operator_authorization import (
    OperatorRequestHeaders,
    operator_request_coordinator,
    operator_request_headers,
)


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
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> DryRunControlReport:
    def execute() -> DryRunControlReport:
        report = DryRunControlService(db).start(payload)
        if report is None:
            raise HTTPException(status_code=404, detail="strategy version not found")
        return report

    return operator_request_coordinator.execute(
        operator_headers,
        operation="dry_run.start",
        provider_call=False,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
    )


@router.post("/control/stop", response_model=DryRunControlReport)
def stop_controlled_dry_run(
    payload: DryRunControlStopRequest,
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> DryRunControlReport:
    return operator_request_coordinator.execute(
        operator_headers,
        operation="dry_run.stop",
        provider_call=False,
        request_payload=payload.model_dump(mode="json"),
        handler=lambda: DryRunControlService().stop(payload),
    )


@router.get("/status", response_model=DryRunStatusSnapshot)
def dry_run_status() -> DryRunStatusSnapshot:
    return DryRunControlService().snapshot()


@router.get("/management")
def dry_run_management() -> dict:
    return DryRunControlService().management()
