from fastapi import APIRouter, HTTPException

from app.core.config import get_settings
from app.db.migrations import verify_schema
from app.db.session import engine
from app.services.runtime_research_bundle_binding import (
    read_runtime_research_bundle_binding,
)


router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.env,
        "database_enabled": settings.database_enabled,
        "allow_live_trading": settings.allow_live_trading,
        "allow_dry_run_trading": settings.allow_dry_run_trading,
        "execution_target_id": settings.execution_target_manifest.active_target_id,
    }


@router.get("/readyz")
def readiness() -> dict[str, object]:
    """Fail closed when PostgreSQL or its versioned ORM schema is unavailable."""

    settings = get_settings()
    if settings.v13_no_trade_mode is True:
        bundle_id = settings.v13_configuration_bundle_snapshot_id
        if bundle_id is None:
            payload = {
                "ready": False,
                "runtime_mode": "V13_NO_TRADE",
                "problem": "V13_CONFIGURATION_BUNDLE_ID_REQUIRED",
            }
            raise HTTPException(status_code=503, detail=payload)
        try:
            binding = read_runtime_research_bundle_binding(engine, bundle_id)
        except Exception as exc:
            problem = getattr(exc, "code", exc.__class__.__name__)
            payload = {
                "ready": False,
                "runtime_mode": "V13_NO_TRADE",
                "problem": problem,
            }
            raise HTTPException(status_code=503, detail=payload) from None
        return binding.sanitized_readiness()

    result = verify_schema(engine)
    payload = {
        "ready": result.ready,
        "database": result.database_identity,
        "schema_version": result.schema_version,
        "problems": list(result.problems),
    }
    if not result.ready:
        raise HTTPException(status_code=503, detail=payload)
    return payload
