from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories import BacktestRepository
from app.schemas import (
    BacktestArtifactIngestRequest,
    BacktestArtifactIngestResponse,
    BacktestResultRead,
    BacktestRunRead,
    BacktestTaskRead,
    LocalBacktestTriggerRequest,
    LocalBacktestTriggerResponse,
    operation_error_evidence,
)
from app.services.backtest_artifact_ingest import BacktestArtifactIngestService
from app.services.local_backtest_trigger import LocalBacktestTriggerService
from app.services.operator_authorization import (
    OperatorRequestHeaders,
    operator_request_coordinator,
    operator_request_headers,
)


router = APIRouter(prefix="/api", tags=["backtests"])


@router.post(
    "/backtest-runs/local",
    response_model=LocalBacktestTriggerResponse,
    status_code=status.HTTP_201_CREATED,
)
def trigger_local_backtest(
    payload: LocalBacktestTriggerRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> LocalBacktestTriggerResponse:
    def execute() -> LocalBacktestTriggerResponse:
        result = LocalBacktestTriggerService(db).trigger(payload)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "strategy version not found",
                    "evidence": operation_error_evidence(
                        status="BLOCKED",
                        reason="strategy version not found",
                        next_action="Create or select a persisted strategy version before triggering a backtest.",
                        ids={"strategy_version_id": payload.strategy_version_id},
                    ).model_dump(mode="json"),
                },
            )
        return result

    return operator_request_coordinator.execute(
        operator_headers,
        operation="backtest.trigger_local",
        provider_call=False,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
    )


@router.post(
    "/backtest-tasks/{task_id}/artifact-ingest",
    response_model=BacktestArtifactIngestResponse,
)
def ingest_backtest_task_artifact(
    task_id: int,
    payload: BacktestArtifactIngestRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> BacktestArtifactIngestResponse:
    def execute() -> BacktestArtifactIngestResponse:
        result = BacktestArtifactIngestService(db).ingest_task_artifact(task_id, payload)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "backtest task not found",
                    "evidence": operation_error_evidence(
                        status="BLOCKED",
                        reason="backtest task not found",
                        next_action="Create or select a persisted backtest task before ingesting artifacts.",
                        ids={"backtest_task_id": task_id},
                    ).model_dump(mode="json"),
                },
            )
        return result

    return operator_request_coordinator.execute(
        operator_headers,
        operation="backtest.artifact_ingest",
        provider_call=False,
        request_payload={"task_id": task_id, **payload.model_dump(mode="json")},
        handler=execute,
    )


@router.get("/backtest-runs", response_model=list[BacktestRunRead])
def list_backtest_runs(
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[BacktestRunRead]:
    runs = BacktestRepository(db).list_runs(limit=limit)
    return [BacktestRunRead.model_validate(run) for run in runs]


@router.get("/backtest-tasks", response_model=list[BacktestTaskRead])
def list_all_backtest_tasks(
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[BacktestTaskRead]:
    tasks = BacktestRepository(db).list_all_tasks(limit=limit)
    return [BacktestTaskRead.model_validate(task) for task in tasks]


@router.get("/backtest-results", response_model=list[BacktestResultRead])
def list_backtest_results(
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[BacktestResultRead]:
    results = BacktestRepository(db).list_all_results(limit=limit)
    return [BacktestResultRead.model_validate(result) for result in results]


@router.get("/backtest-runs/{run_id}", response_model=BacktestRunRead)
def get_backtest_run(run_id: int, db: Session = Depends(get_db)) -> BacktestRunRead:
    run = BacktestRepository(db).get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backtest run not found")
    return BacktestRunRead.model_validate(run)


@router.get("/backtest-runs/{run_id}/tasks", response_model=list[BacktestTaskRead])
def list_backtest_tasks(run_id: int, db: Session = Depends(get_db)) -> list[BacktestTaskRead]:
    repository = BacktestRepository(db)
    if repository.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="backtest run not found")
    return [BacktestTaskRead.model_validate(task) for task in repository.list_tasks(run_id)]


@router.get("/backtest-tasks/{task_id}", response_model=BacktestTaskRead)
def get_backtest_task(task_id: int, db: Session = Depends(get_db)) -> BacktestTaskRead:
    task = BacktestRepository(db).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="backtest task not found")
    return BacktestTaskRead.model_validate(task)
