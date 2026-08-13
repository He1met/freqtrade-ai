import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.backtests import router as backtests_router
from app.api.dry_run import router as dry_run_router
from app.api.health import router as health_router
from app.api.okx_demo_observability import router as okx_demo_observability_router
from app.api.okx_demo_reconciliation import router as okx_demo_reconciliation_router
from app.api.operational_readiness import router as operational_readiness_router
from app.api.ranking import router as ranking_router
from app.api.research_jobs import router as research_jobs_router
from app.api.runtime import router as runtime_router
from app.api.runtime_v13 import router as runtime_v13_router
from app.api.strategies import router as strategies_router
from app.api.strategy_generation import router as strategy_generation_router
from app.api.strategy_platform import router as strategy_platform_router
from app.api.strategy_promotion import router as strategy_promotion_router
from app.api.strategy_research import router as strategy_research_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.strategy_platform_errors import StrategyPlatformReadError

configure_logging()
settings = get_settings()
logging.getLogger(__name__).info(
    "execution target configured target_id=%s account_mode=%s simulated_trading=%s "
    "allow_real_funds=%s order_submission_enabled=%s",
    settings.execution_target_manifest.active_target_id,
    settings.execution_target_manifest.active_target.account_mode,
    settings.execution_target_manifest.active_target.simulated_trading,
    settings.execution_target_manifest.active_target.allow_real_funds,
    settings.execution_target_manifest.active_target.order_submission_enabled,
)

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Freqtrade AI phase 0 backend skeleton.",
)


@app.exception_handler(StrategyPlatformReadError)
async def strategy_platform_read_error_handler(
    _request: Request, exc: StrategyPlatformReadError
) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail()})


app.include_router(backtests_router)
app.include_router(dry_run_router)
app.include_router(health_router)
app.include_router(ranking_router)
app.include_router(research_jobs_router)
app.include_router(runtime_router)
app.include_router(runtime_router, prefix="/api")
app.include_router(runtime_v13_router)
app.include_router(operational_readiness_router)
app.include_router(okx_demo_reconciliation_router)
app.include_router(okx_demo_observability_router)
app.include_router(strategies_router)
app.include_router(strategy_generation_router)
app.include_router(strategy_promotion_router)
app.include_router(strategy_research_router)
app.include_router(strategy_platform_router)
