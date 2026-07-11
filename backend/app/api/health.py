from fastapi import APIRouter, HTTPException

from app.core.config import get_settings
from app.db.migrations import verify_schema
from app.db.session import engine


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
    }


@router.get("/readyz")
def readiness() -> dict[str, object]:
    """Fail closed when PostgreSQL or its versioned ORM schema is unavailable."""

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
