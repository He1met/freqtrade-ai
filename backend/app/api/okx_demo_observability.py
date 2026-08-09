from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.okx_demo_observability import OkxDemoObservabilityResponse
from app.schemas.okx_demo_runtime_activity import OkxDemoRuntimeActivityRead
from app.services.formal_read_models import OkxDemoRuntimeActivityService
from app.services.okx_demo_observability import OkxDemoObservabilityService


router = APIRouter(prefix="/api/okx-demo", tags=["okx-demo"])


@router.get("/observability", response_model=OkxDemoObservabilityResponse)
def read_okx_demo_observability(
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
) -> OkxDemoObservabilityResponse:
    """Expose only the allowlisted database projection used by the desktop page."""

    return OkxDemoObservabilityService(db).build(limit=limit)


@router.get("/runtime-activity", response_model=OkxDemoRuntimeActivityRead)
def read_okx_demo_runtime_activity(
    signal_limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> OkxDemoRuntimeActivityRead:
    """Read active deployments and recent signal evaluations without runtime writes."""

    return OkxDemoRuntimeActivityService(db).build(signal_limit=signal_limit)
