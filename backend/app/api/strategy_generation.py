from __future__ import annotations

from typing import Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.adapters.freqtrade.backtest_runner import FreqtradeBacktestRunner
from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner
from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.db.session import get_db
from app.repositories import StrategyGenerationRunRepository, StrategyRepository
from app.schemas import (
    DeepSeekBacktestLoopRequest,
    DeepSeekBacktestLoopResponse,
    GenerationRunStatus,
    StrategyGenerationApiResponse,
    StrategyGenerationRequest,
    StrategyGenerationRunRead,
    StrategyRead,
    StrategyVersionRead,
    operation_error_evidence,
)
from app.services.strategy_generation import (
    StrategyGenerationExecutionError,
    StrategyGenerationService,
    build_deepseek_single_provider_from_env,
    build_strategy_blueprint_provider_from_env,
)
from app.services.deepseek_backtest_loop import DeepSeekBacktestLoopService
from app.services.operator_authorization import (
    OperatorRequestHeaders,
    operator_request_coordinator,
    operator_request_headers,
    provider_is_real,
)
from app.schemas.strategy_generation_run import DeepSeekSingleGenerationRequest


router = APIRouter(prefix="/api", tags=["strategy-generation"])


def get_strategy_generation_service(db: Session = Depends(get_db)) -> StrategyGenerationService:
    return StrategyGenerationService(
        db,
        provider=build_strategy_blueprint_provider_from_env(),
        file_manager=StrategyFileManager(),
    )


def get_deepseek_single_generation_service(db: Session = Depends(get_db)) -> StrategyGenerationService:
    return StrategyGenerationService(
        db,
        provider=build_deepseek_single_provider_from_env(),
        file_manager=StrategyFileManager(),
    )


def get_deepseek_backtest_loop_service(
    db: Session = Depends(get_db),
) -> DeepSeekBacktestLoopService:
    return DeepSeekBacktestLoopService(
        db,
        generation_service=StrategyGenerationService(
            db,
            provider=build_deepseek_single_provider_from_env(),
            file_manager=StrategyFileManager(),
        ),
        backtest_runner=FreqtradeBacktestRunner(FreqtradeCliRunner()),
    )


@router.get("/strategy-generation-runs", response_model=list[StrategyGenerationRunRead])
def list_strategy_generation_runs(
    status: Optional[GenerationRunStatus] = None,
    db: Session = Depends(get_db),
) -> list[StrategyGenerationRunRead]:
    runs = StrategyGenerationRunRepository(db).list(status=status)
    return [StrategyGenerationRunRead.model_validate(run) for run in runs]


@router.post("/strategy-generation-runs", response_model=StrategyGenerationApiResponse)
def create_strategy_generation_run(
    payload: StrategyGenerationRequest,
    service: StrategyGenerationService = Depends(get_strategy_generation_service),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> StrategyGenerationApiResponse:
    real_provider = provider_is_real(service.provider)

    def execute() -> StrategyGenerationApiResponse:
        if real_provider and not service.has_provider_credential():
            reason = "Missing configured Provider API key environment variable."
            run_id = service.record_blocked_once(
                payload.prompt_summary,
                reason,
                real_call_authorized=True,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Strategy generation was blocked before Provider execution",
                    "strategy_generation_run_id": run_id,
                    "operation_status": "BLOCKED",
                    "evidence": operation_error_evidence(
                        status="BLOCKED",
                        reason=reason,
                        next_action="Set the configured Provider key in ENV and retry with a new idempotency key.",
                        ids={"strategy_generation_run_id": run_id},
                    ).model_dump(mode="json"),
                },
            )
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
                    "operation_status": "FAILED",
                    "evidence": operation_error_evidence(
                        status="FAILED",
                        reason=str(exc),
                        next_action="Inspect the persisted generation run, correct provider or validation errors, and retry.",
                        ids={"strategy_generation_run_id": exc.run_id},
                    ).model_dump(mode="json"),
                },
            ) from exc

        return _build_generation_response(service, result.run_id, result.version_ids)

    return operator_request_coordinator.execute(
        operator_headers,
        operation="strategy_generation.create",
        provider_call=real_provider,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
    )


@router.post("/strategy-generation-runs/deepseek-single", response_model=StrategyGenerationApiResponse)
def create_deepseek_single_generation_run(
    payload: DeepSeekSingleGenerationRequest,
    service: StrategyGenerationService = Depends(get_deepseek_single_generation_service),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> StrategyGenerationApiResponse:
    if service.provider.provider_name != "deepseek":
        raise HTTPException(status_code=500, detail="DeepSeek single-run provider boundary is misconfigured")

    def execute() -> StrategyGenerationApiResponse:
        return _execute_deepseek_single_generation(payload, service)

    return operator_request_coordinator.execute(
        operator_headers,
        operation="strategy_generation.deepseek_single",
        provider_call=payload.allow_real_call,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
    )


def _execute_deepseek_single_generation(
    payload: DeepSeekSingleGenerationRequest,
    service: StrategyGenerationService,
) -> StrategyGenerationApiResponse:
    if not payload.allow_real_call:
        reason = "Real DeepSeek call requires explicit single-run authorization."
        run_id = service.record_blocked_once(
            payload.prompt_summary,
            reason,
            real_call_authorized=False,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "DeepSeek single run was blocked before provider execution",
                "strategy_generation_run_id": run_id,
                "evidence": operation_error_evidence(
                    status="BLOCKED",
                    reason=reason,
                    next_action="Set the key in ENV and retry once with allow_real_call=true.",
                    ids={"strategy_generation_run_id": run_id},
                ).model_dump(mode="json"),
            },
        )

    if not service.has_provider_credential():
        reason = "Missing configured DeepSeek API key environment variable."
        run_id = service.record_blocked_once(
            payload.prompt_summary,
            reason,
            real_call_authorized=True,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "DeepSeek single run was blocked before provider execution",
                "strategy_generation_run_id": run_id,
                "evidence": operation_error_evidence(
                    status="BLOCKED",
                    reason=reason,
                    next_action="Set the configured DeepSeek key in ENV and retry once.",
                    ids={"strategy_generation_run_id": run_id},
                ).model_dump(mode="json"),
            },
        )

    try:
        result = service.run_once_with_result(
            payload.prompt_summary,
            requested_count=1,
            execution_metadata={
                "real_call_authorized": True,
                "real_call_attempted": True,
                "credential_env_present": True,
                "credential_values_recorded": False,
            },
        )
    except StrategyGenerationExecutionError as exc:
        is_missing_key = "missing LLM API key environment variable" in str(exc)
        status = "BLOCKED" if is_missing_key else "FAILED"
        raise HTTPException(
            status_code=409 if is_missing_key else 502,
            detail={
                "message": "DeepSeek single run did not produce an accepted strategy",
                "strategy_generation_run_id": exc.run_id,
                "evidence": operation_error_evidence(
                    status=status,
                    reason=str(exc),
                    next_action=(
                        "Set the configured DeepSeek key in ENV and retry once."
                        if is_missing_key
                        else "Inspect the persisted provider diagnostics and retry only after correcting the failure."
                    ),
                    ids={"strategy_generation_run_id": exc.run_id},
                ).model_dump(mode="json"),
            },
        ) from exc

    return _build_generation_response(service, result.run_id, result.version_ids)


@router.post(
    "/strategy-generation-runs/deepseek-single/backtest-loop",
    response_model=DeepSeekBacktestLoopResponse,
)
def run_deepseek_backtest_loop(
    payload: DeepSeekBacktestLoopRequest,
    service: DeepSeekBacktestLoopService = Depends(get_deepseek_backtest_loop_service),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> DeepSeekBacktestLoopResponse:
    return operator_request_coordinator.execute(
        operator_headers,
        operation="strategy_generation.deepseek_backtest_loop",
        provider_call=payload.allow_real_call,
        request_payload=payload.model_dump(mode="json"),
        handler=lambda: service.run(payload),
    )


def _build_generation_response(
    service: StrategyGenerationService,
    run_id: int,
    version_ids: Iterable[int],
) -> StrategyGenerationApiResponse:
    run_repository = service.run_repository
    strategy_repository = service.strategy_repository
    run = run_repository.get(run_id)
    if run is None or run.status != "succeeded":
        raise HTTPException(
            status_code=500,
            detail="strategy generation API could not verify the persisted generation run",
        )

    versions = _load_versions(strategy_repository, version_ids, run_id)
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
