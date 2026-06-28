from fastapi import APIRouter

from app.core.config import get_settings


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
