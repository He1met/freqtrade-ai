from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.mvp_runtime import router as mvp_runtime_router
from app.core.config import get_settings
from app.core.logging import configure_logging


configure_logging()
settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Freqtrade AI phase 0 backend skeleton.",
)

app.include_router(health_router)
app.include_router(mvp_runtime_router)
