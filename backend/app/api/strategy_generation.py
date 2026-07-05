from __future__ import annotations

from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.db.session import get_db
from app.repositories import StrategyGenerationRunRepository, StrategyRepository
from app.schemas import (
    StrategyGenerationApiResponse,
    StrategyGenerationRequest,
    StrategyGenerationRunRead,
    StrategyRead,
    StrategyVersionRead,
)
from app.services.strategy_generation import (
    StrategyGenerationExecutionError,
    StrategyGenerationService,
    build_strategy_blueprint_provider_from_env,
)


router = APIRouter(prefix="/api", tags=["strategy-generation"])


def get_strategy_generation_service(db: Session = Depends(get_db)) -> StrategyGenerationService:
    return StrategyGenerationService(
        db,
        provider=build_strategy_blueprint_provider_from_env(),
        file_manager=StrategyFileManager(),
    )


@router.post("/strategy-generation-runs", response_model=StrategyGenerationApiResponse)
def create_strategy_generation_run(
    payload: StrategyGenerationRequest,
    service: StrategyGenerationService = Depends(get_strategy_generation_service),
) -> StrategyGenerationApiResponse:
    try:
        result = service.run_once_with_result(
            payload.prompt_summary,
            requested_count=payload.requested_count,
        )
    except StrategyGenerationExecutionError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "strategy generation failed after creating a database run record",
                "strategy_generation_run_id": exc.run_id,
                "failed_reason": str(exc),
            },
        ) from exc

    run_repository = service.run_repository
    strategy_repository = service.strategy_repository
    run = run_repository.get(result.run_id)
    if run is None or run.status != "succeeded":
        raise HTTPException(
            status_code=500,
            detail="strategy generation API could not verify the persisted generation run",
        )

    versions = _load_versions(strategy_repository, result.version_ids, result.run_id)
    strategies = _load_strategies(strategy_repository, [version.strategy_id for version in versions])
    if not versions or run.accepted_count != len(versions):
        raise HTTPException(
            status_code=500,
            detail="strategy generation API could not reconcile accepted versions with database rows",
        )

    return StrategyGenerationApiResponse(
        run=StrategyGenerationRunRead.model_validate(run),
        strategies=[StrategyRead.model_validate(strategy) for strategy in strategies],
        strategy_versions=[StrategyVersionRead.model_validate(version) for version in versions],
    )


def _load_versions(
    repository: StrategyRepository,
    version_ids: Iterable[int],
    generation_run_id: int,
):
    versions = []
    for version_id in version_ids:
        version = repository.get_version(version_id)
        if version is None or version.generation_run_id != generation_run_id:
            raise HTTPException(
                status_code=500,
                detail="strategy generation API could not verify a persisted strategy version",
            )
        versions.append(version)
    return versions


def _load_strategies(repository: StrategyRepository, strategy_ids: Iterable[int]):
    strategies = []
    seen: set[int] = set()
    for strategy_id in strategy_ids:
        if strategy_id in seen:
            continue
        strategy = repository.get(strategy_id)
        if strategy is None:
            raise HTTPException(
                status_code=500,
                detail="strategy generation API could not verify a persisted strategy",
            )
        seen.add(strategy_id)
        strategies.append(strategy)
    return strategies
