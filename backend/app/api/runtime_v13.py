from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.db.session import engine
from app.schemas.runtime_v13 import RuntimeResearchConfigurationReadiness
from app.services.runtime_research_bundle_binding import (
    read_runtime_research_bundle_binding,
)


router = APIRouter(prefix="/api/v1/runtime", tags=["runtime-v13"])


@router.get(
    "/configuration-readiness",
    response_model=RuntimeResearchConfigurationReadiness,
)
def runtime_configuration_readiness() -> RuntimeResearchConfigurationReadiness:
    settings = get_settings()
    if settings.v13_no_trade_mode is not True:
        raise StrategyPlatformReadError(
            "V13_NO_TRADE_MODE_DISABLED",
            "V1.3 no-trade runtime mode is not explicitly enabled.",
            status_code=503,
        )
    bundle_id = settings.v13_configuration_bundle_snapshot_id
    if bundle_id is None:
        raise StrategyPlatformReadError(
            "V13_CONFIGURATION_BUNDLE_ID_REQUIRED",
            "V1.3 no-trade runtime requires an explicit immutable bundle id.",
            status_code=503,
        )
    binding = read_runtime_research_bundle_binding(engine, bundle_id)
    return RuntimeResearchConfigurationReadiness.model_validate(
        binding.sanitized_readiness()
    )
