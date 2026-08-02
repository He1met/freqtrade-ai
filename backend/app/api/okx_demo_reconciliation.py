from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import (
    OkxDemoAccountSnapshot,
    OkxDemoExchangeEvent,
    OkxDemoFillSnapshot,
    OkxDemoOrderSnapshot,
    OkxDemoPositionSnapshot,
    OkxDemoReconciliationState,
    OkxDemoRecoveryBatch,
    OkxDemoRecoveryGrant,
    ReconciliationRun,
)
from app.models.execution_lineage import OKX_DEMO_TARGET_ID
from app.services.okx_demo_submission_grant import (
    OkxDemoSubmissionGrantBlocked,
    OkxDemoSubmissionGrantService,
)
from app.services.okx_demo_canary_preparation import (
    OkxDemoCanaryPreparationBlocked,
    OkxDemoCanaryPreparationWaiting,
    OkxDemoCanaryPreparationService,
)
from app.services.operator_authorization import (
    OperatorRequestHeaders,
    operator_request_coordinator,
    operator_request_headers,
)


router = APIRouter(prefix="/api/okx-demo", tags=["okx-demo-reconciliation"])


class OneShotGrantRequest(BaseModel):
    model_config = {"extra": "forbid"}
    approval_id: int = Field(gt=0)
    canonical_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_order_id: str = Field(min_length=1, max_length=32)


class CanaryPreparationRequest(BaseModel):
    """No caller-controlled order fields are accepted for the canary."""

    model_config = {"extra": "forbid"}


def _canary_preparation_response(result: Any) -> dict[str, Any]:
    return {
        "operation_status": result.operation_status,
        "execution_target_id": "OKX_DEMO",
        "provenance": result.provenance,
        "non_production": True,
        "approval_id": result.approval_id,
        "trade_intent_id": result.trade_intent_id,
        "risk_decision_id": result.risk_decision_id,
        "full_chain_run_id": result.full_chain_run_id,
        "research_job_id": result.research_job_id,
        "research_job_attempt_id": result.research_job_attempt_id,
        "reconciliation_run_id": result.reconciliation_run_id,
        "canonical_hash": result.canonical_hash,
        "policy_digest": result.policy_digest,
        "approved_payload_hash": result.approved_payload_hash,
        "client_order_id": result.client_order_id,
        "instrument_id": result.instrument_id,
        "quantity": format(result.quantity, "f"),
        "notional": format(result.notional, "f"),
        "expires_at": result.expires_at.isoformat(),
        "idempotency_key_digest": result.idempotency_key_digest,
        "credential_values_recorded": False,
    }


def _cache_canary_result(result: Any) -> bool:
    """Keep non-terminal runtime handoffs retryable under one idempotency key."""

    return not (
        isinstance(result, dict)
        and result.get("operation_status") == "WAITING_FOR_RUNTIME_ATTESTATION"
    )


@router.post("/canary/prepare", status_code=202)
def prepare_controlled_canary(
    payload: CanaryPreparationRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> dict[str, Any]:
    """Persist a non-production canary lineage before arming #595's grant.

    This endpoint deliberately does not create a submission grant and never
    enables global order submission.  The caller must use the existing
    one-shot grant endpoint with the returned, exact hashes; the canonical
    runtime then remains the only writer.
    """

    def execute() -> dict[str, Any]:
        try:
            result = OkxDemoCanaryPreparationService(db).prepare(
                idempotency_key=operator_headers.idempotency_key or "",
            )
        except OkxDemoCanaryPreparationWaiting as exc:
            return {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "non_production": True,
                "attestation_request_job_id": exc.job_id,
                "credential_values_recorded": False,
            }
        except OkxDemoCanaryPreparationBlocked as exc:
            raise HTTPException(
                status_code=409,
                detail={"operation_status": "BLOCKED", "message": str(exc)},
            ) from exc
        return _canary_preparation_response(result)

    return operator_request_coordinator.execute(
        operator_headers,
        operation="okx-demo-controlled-canary-prepare",
        provider_call=True,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
        cache_result=_cache_canary_result,
    )


@router.post("/canary/finalize", status_code=202)
def finalize_controlled_canary(
    payload: CanaryPreparationRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> dict[str, Any]:
    """Finalize the same idempotent canary after runtime snapshot handoff."""

    def execute() -> dict[str, Any]:
        try:
            result = OkxDemoCanaryPreparationService(db).prepare(
                idempotency_key=operator_headers.idempotency_key or "",
            )
        except OkxDemoCanaryPreparationWaiting as exc:
            return {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "non_production": True,
                "attestation_request_job_id": exc.job_id,
                "credential_values_recorded": False,
            }
        except OkxDemoCanaryPreparationBlocked as exc:
            raise HTTPException(
                status_code=409,
                detail={"operation_status": "BLOCKED", "message": str(exc)},
            ) from exc
        return _canary_preparation_response(result)

    return operator_request_coordinator.execute(
        operator_headers,
        operation="okx-demo-controlled-canary-finalize",
        provider_call=True,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
        cache_result=_cache_canary_result,
    )


@router.post("/submission-grants/one-shot", status_code=202)
def arm_one_shot_submission_grant(
    payload: OneShotGrantRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> dict[str, Any]:
    def execute() -> dict[str, Any]:
        try:
            grant = OkxDemoSubmissionGrantService(db).arm(
                **payload.model_dump()
            )
        except OkxDemoSubmissionGrantBlocked as exc:
            raise HTTPException(
                status_code=409,
                detail={"operation_status": "BLOCKED", "message": str(exc)},
            ) from exc
        return {
            "operation_status": "ARMED",
            "execution_target_id": "OKX_DEMO",
            "grant_id": grant.grant_id,
            "approval_id": grant.approval_id,
            "expires_at": grant.expires_at.isoformat(),
            "credential_values_recorded": False,
        }

    return operator_request_coordinator.execute(
        operator_headers,
        operation="okx-demo-one-shot-submission-grant",
        # Reuse the existing explicit single-attempt consent header.  This is
        # not a provider call, but it is equally consequential.
        provider_call=True,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
    )


@router.get("/reconciliation/latest")
def latest_reconciliation(db: Session = Depends(get_db)) -> dict[str, Any]:
    state = db.scalars(
        select(OkxDemoReconciliationState).where(
            OkxDemoReconciliationState.execution_target_id
            == OKX_DEMO_TARGET_ID
        )
    ).first()
    run = (
        db.get(ReconciliationRun, state.last_reconciliation_run_id)
        if state is not None and state.last_reconciliation_run_id is not None
        else None
    )
    if state is None:
        return _unknown_payload()
    return {
        "execution_target_id": OKX_DEMO_TARGET_ID,
        "status": state.status,
        "opening_frozen": state.opening_frozen,
        "reason": state.block_reason,
        "authoritative_observed_at": _iso(state.last_event_observed_at),
        "reconciliation_run_database_id": (
            run.id if run is not None else None
        ),
        "database_ids": (
            run.database_ids
            if run is not None
            else {"reconciliation_state": [state.database_id]}
        ),
        "artifact": (
            {
                "artifact_id": _artifact_id(run.artifact_path),
                "sha256": run.artifact_sha256,
                "status": run.artifact_status,
            }
            if run is not None
            else None
        ),
        "data_source": {
            "source_type": "api_aggregate",
            "core_data": True,
        },
    }


@router.get("/reconciliation/runs/{run_id}")
def reconciliation_run(
    run_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.get(ReconciliationRun, run_id)
    if row is None or row.execution_target_id != OKX_DEMO_TARGET_ID:
        raise HTTPException(status_code=404, detail="reconciliation run not found")
    return {
        "execution_target_id": OKX_DEMO_TARGET_ID,
        "reconciliation_run_database_id": row.id,
        "status": row.status,
        "database_ids": row.database_ids,
        "summary": row.summary_snapshot,
        "artifact": {
            "artifact_id": _artifact_id(row.artifact_path),
            "sha256": row.artifact_sha256,
            "status": row.artifact_status,
        },
        "authoritative_observed_at": _iso(row.authoritative_observed_at),
        "completed_at": _iso(row.completed_at),
        "data_source": {
            "source_type": row.source_type,
            "core_data": row.core_data,
        },
    }


@router.get("/exchange-state")
def exchange_state(
    event_limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    counts = {}
    for label, model in (
        ("events", OkxDemoExchangeEvent),
        ("orders", OkxDemoOrderSnapshot),
        ("fills", OkxDemoFillSnapshot),
        ("positions", OkxDemoPositionSnapshot),
        ("accounts", OkxDemoAccountSnapshot),
        ("recovery_batches", OkxDemoRecoveryBatch),
        ("recovery_grants", OkxDemoRecoveryGrant),
    ):
        counts[label] = db.scalar(
            select(func.count()).select_from(model).where(
                model.execution_target_id == OKX_DEMO_TARGET_ID
            )
        )
    event_ids = list(
        db.scalars(
            select(OkxDemoExchangeEvent.database_id)
            .where(
                OkxDemoExchangeEvent.execution_target_id
                == OKX_DEMO_TARGET_ID
            )
            .order_by(
                OkxDemoExchangeEvent.observed_at.desc(),
                OkxDemoExchangeEvent.database_id.desc(),
            )
            .limit(event_limit)
        ).all()
    )
    return {
        "execution_target_id": OKX_DEMO_TARGET_ID,
        "counts": counts,
        "database_ids": {"exchange_events": event_ids},
        "raw_exchange_payloads_exposed": False,
        "data_source": {
            "source_type": "database",
            "core_data": True,
        },
    }


def _unknown_payload() -> dict[str, Any]:
    return {
        "execution_target_id": OKX_DEMO_TARGET_ID,
        "status": "UNKNOWN",
        "opening_frozen": True,
        "reason": "RECONCILIATION_REQUIRED",
        "authoritative_observed_at": None,
        "reconciliation_run_database_id": None,
        "database_ids": {},
        "artifact": None,
        "data_source": {"source_type": "api_aggregate", "core_data": True},
    }


def _iso(value: Any) -> Any:
    return value.isoformat() if value is not None else None


def _artifact_id(value: Any) -> Any:
    return Path(value).name if value else None
