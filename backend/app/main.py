from fastapi import FastAPI

from app.api.debug_mvp import router as debug_mvp_router
from app.api.health import router as health_router
from app.api.runtime import router as runtime_router
from app.api.strategy_generation import router as strategy_generation_router
from app.core.config import get_settings
from app.core.logging import configure_logging


configure_logging()
settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Freqtrade AI phase 0 backend skeleton.",
)

app.include_router(debug_mvp_router)
app.include_router(health_router)
app.include_router(runtime_router)
app.include_router(strategy_generation_router)
