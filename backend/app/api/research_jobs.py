from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas import (
    DeepSeekBacktestLoopRequest,
    ResearchJobCancelRequest,
    ResearchJobRead,
    ResearchWorkerControlRead,
    ResearchWorkerPauseRequest,
)
from app.services.operator_authorization import (
    OperatorRequestHeaders,
    operator_request_coordinator,
    operator_request_headers,
)
from app.services.research_job_queue import (
    DEEPSEEK_BACKTEST_OPERATION,
    ResearchJobConflict,
    ResearchJobQueueService,
    UnsafeResearchJobPayload,
)


router = APIRouter(prefix="/api", tags=["research-jobs"])


def get_research_job_queue(db: Session = Depends(get_db)) -> ResearchJobQueueService:
    return ResearchJobQueueService(db)


@router.post(
    "/strategy-generation-runs/deepseek-single/backtest-loop",
    response_model=ResearchJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post(
    "/deepseek-backtest-jobs",
    response_model=ResearchJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_deepseek_backtest_job(
    payload: DeepSeekBacktestLoopRequest,
    queue: ResearchJobQueueService = Depends(get_research_job_queue),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> ResearchJobRead:
    def execute() -> ResearchJobRead:
        try:
            job = queue.enqueue_deepseek_backtest(
                payload,
                idempotency_key=operator_headers.idempotency_key or "",
            )
        except ResearchJobConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={"operation_status": "BLOCKED", "message": str(exc)},
            ) from exc
        except UnsafeResearchJobPayload as exc:
            raise HTTPException(
                status_code=422,
                detail={"operation_status": "BLOCKED", "message": str(exc)},
            ) from exc
        return ResearchJobRead.model_validate(job)

    return operator_request_coordinator.execute(
        operator_headers,
        operation=DEEPSEEK_BACKTEST_OPERATION,
        provider_call=payload.allow_real_call,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
    )


@router.get("/deepseek-backtest-jobs", response_model=list[ResearchJobRead])
def list_deepseek_backtest_jobs(
    limit: int = 100,
    queue: ResearchJobQueueService = Depends(get_research_job_queue),
) -> list[ResearchJobRead]:
    return queue.list(limit=max(1, min(limit, 500)))


@router.get("/deepseek-backtest-jobs/{job_id}", response_model=ResearchJobRead)
def get_deepseek_backtest_job(
    job_id: int,
    queue: ResearchJobQueueService = Depends(get_research_job_queue),
) -> ResearchJobRead:
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="research job not found")
    return job


@router.post("/deepseek-backtest-jobs/{job_id}/cancel", response_model=ResearchJobRead)
def cancel_deepseek_backtest_job(
    job_id: int,
    payload: ResearchJobCancelRequest,
    queue: ResearchJobQueueService = Depends(get_research_job_queue),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> ResearchJobRead:
    def execute() -> ResearchJobRead:
        job = queue.cancel(job_id, payload.reason)
        if job is None:
            raise HTTPException(status_code=404, detail="research job not found")
        return job

    return operator_request_coordinator.execute(
        operator_headers,
        operation="research_job.cancel",
        provider_call=False,
        request_payload={"job_id": job_id, **payload.model_dump(mode="json")},
        handler=execute,
    )


@router.get("/deepseek-backtest-worker/status", response_model=ResearchWorkerControlRead)
def get_deepseek_backtest_worker_status(
    queue: ResearchJobQueueService = Depends(get_research_job_queue),
) -> ResearchWorkerControlRead:
    return queue.worker_status()


@router.post("/deepseek-backtest-worker/pause", response_model=ResearchWorkerControlRead)
def pause_deepseek_backtest_worker(
    payload: ResearchWorkerPauseRequest,
    queue: ResearchJobQueueService = Depends(get_research_job_queue),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> ResearchWorkerControlRead:
    return operator_request_coordinator.execute(
        operator_headers,
        operation="research_worker.pause",
        provider_call=False,
        request_payload=payload.model_dump(mode="json"),
        handler=lambda: queue.pause(payload.reason),
    )


@router.post("/deepseek-backtest-worker/resume", response_model=ResearchWorkerControlRead)
def resume_deepseek_backtest_worker(
    queue: ResearchJobQueueService = Depends(get_research_job_queue),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> ResearchWorkerControlRead:
    return operator_request_coordinator.execute(
        operator_headers,
        operation="research_worker.resume",
        provider_call=False,
        request_payload={"paused": False},
        handler=queue.resume,
    )
