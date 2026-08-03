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
    FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY,
    FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY,
    FRESH_EXECUTION_ONLY_RECOVERY,
    OkxDemoCanaryPreparationBlocked,
    OkxDemoCanaryPreparationRuntimeBusy,
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
    response = {
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
    entry_kind = getattr(result, "entry_kind", None)
    if entry_kind is not None:
        response["entry_kind"] = entry_kind
        response["supersedes_job_ids"] = list(
            getattr(result, "supersedes_job_ids", ())
        )
    refresh_of_job_id = getattr(result, "refresh_of_job_id", None)
    if refresh_of_job_id is not None:
        response["refresh_of_job_id"] = refresh_of_job_id
    recovery_of_job_id = getattr(result, "recovery_of_job_id", None)
    if recovery_of_job_id is not None:
        response["recovery_of_job_id"] = recovery_of_job_id
    return response


def _cache_canary_result(result: Any) -> bool:
    """Keep non-terminal runtime handoffs retryable under one idempotency key."""

    return not (
        isinstance(result, dict)
        and result.get("operation_status") == "WAITING_FOR_RUNTIME_ATTESTATION"
    )


def _cache_canary_retry_result(_result: Any) -> bool:
    """A retry handoff is terminal for its key even while runtime is pending."""

    return True


def _cache_terminal_canary_consent_result(result: Any) -> bool:
    """Cache consent responses only after the durable handoff is terminal."""

    return isinstance(result, dict) and result.get("operation_status") in {
        "CONSUMED",
        "REVOKED",
        "FAILED",
        "EXPIRED",
    }


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
            response = {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "non_production": True,
                "attestation_request_job_id": exc.job_id,
                "credential_values_recorded": False,
            }
            if exc.entry_kind is not None:
                response["entry_kind"] = exc.entry_kind
                response["supersedes_job_ids"] = list(exc.supersedes_job_ids)
            if exc.refresh_of_job_id is not None:
                response["refresh_of_job_id"] = exc.refresh_of_job_id
            return response
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


@router.post("/canary/prepare-execution-only", status_code=202)
def prepare_fresh_execution_only_canary(
    payload: CanaryPreparationRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> dict[str, Any]:
    """Start one fresh execution-only handoff after immutable old failures.

    This operator entry is deliberately separate from ``/canary/prepare`` so
    old signal-bundle and retry ResearchJobs remain a hard stop on the
    original path.  The service records their ids and never updates them.
    """

    def execute() -> dict[str, Any]:
        try:
            result = OkxDemoCanaryPreparationService(db).prepare_fresh_execution_only(
                idempotency_key=operator_headers.idempotency_key or "",
            )
        except OkxDemoCanaryPreparationWaiting as exc:
            response = {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "entry_kind": "FRESH_EXECUTION_ONLY",
                "non_production": True,
                "attestation_request_job_id": exc.job_id,
                "credential_values_recorded": False,
            }
            response["supersedes_job_ids"] = list(exc.supersedes_job_ids)
            return response
        except OkxDemoCanaryPreparationBlocked as exc:
            raise HTTPException(
                status_code=409,
                detail={"operation_status": "BLOCKED", "message": str(exc)},
            ) from exc
        response = _canary_preparation_response(result)
        response["entry_kind"] = "FRESH_EXECUTION_ONLY"
        return response

    return operator_request_coordinator.execute(
        operator_headers,
        operation="okx-demo-controlled-canary-fresh-execution-only",
        provider_call=True,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
        cache_result=_cache_canary_result,
    )


@router.post("/canary/refresh-execution-only", status_code=202)
def refresh_fresh_execution_only_canary(
    payload: CanaryPreparationRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> dict[str, Any]:
    """Re-attest an expired successful fresh canary handoff.

    A new idempotency key creates one immutable ResearchJob successor.  The
    same key is used after the runtime handoff to finalize it; this endpoint
    never changes the source job or enables order submission.
    """

    def execute() -> dict[str, Any]:
        try:
            result = OkxDemoCanaryPreparationService(
                db
            ).prepare_fresh_execution_only_refresh(
                idempotency_key=operator_headers.idempotency_key or "",
            )
        except OkxDemoCanaryPreparationRuntimeBusy as exc:
            response = {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "entry_kind": exc.entry_kind or "FRESH_EXECUTION_ONLY_REFRESH",
                "non_production": True,
                "supersedes_job_ids": list(exc.supersedes_job_ids),
                "credential_values_recorded": False,
            }
            if exc.job_id is not None:
                response["attestation_request_job_id"] = exc.job_id
            if exc.refresh_of_job_id is not None:
                response["refresh_of_job_id"] = exc.refresh_of_job_id
            return response
        except OkxDemoCanaryPreparationWaiting as exc:
            response = {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "entry_kind": exc.entry_kind or "FRESH_EXECUTION_ONLY_REFRESH",
                "non_production": True,
                "attestation_request_job_id": exc.job_id,
                "supersedes_job_ids": list(exc.supersedes_job_ids),
                "credential_values_recorded": False,
            }
            if exc.refresh_of_job_id is not None:
                response["refresh_of_job_id"] = exc.refresh_of_job_id
            return response
        except OkxDemoCanaryPreparationBlocked as exc:
            raise HTTPException(
                status_code=409,
                detail={"operation_status": "BLOCKED", "message": str(exc)},
            ) from exc
        response = _canary_preparation_response(result)
        response["entry_kind"] = result.entry_kind or "FRESH_EXECUTION_ONLY_REFRESH"
        return response

    return operator_request_coordinator.execute(
        operator_headers,
        operation="okx-demo-controlled-canary-refresh-execution-only",
        provider_call=True,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
        cache_result=_cache_canary_result,
    )


@router.post("/canary/retry", status_code=202)
def retry_controlled_canary_attestation(
    payload: CanaryPreparationRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> dict[str, Any]:
    """Queue one successor after a transient runtime attestation failure.

    The retry key must be new.  This endpoint only creates a durable runtime
    handoff; it never creates a grant, enables submission, or changes the
    original blocked ResearchJob.
    """

    def execute() -> dict[str, Any]:
        try:
            result = OkxDemoCanaryPreparationService(db).retry_attestation(
                idempotency_key=operator_headers.idempotency_key or "",
            )
        except OkxDemoCanaryPreparationBlocked as exc:
            raise HTTPException(
                status_code=409,
                detail={"operation_status": "BLOCKED", "message": str(exc)},
            ) from exc
        return {
            "operation_status": result.operation_status,
            "execution_target_id": "OKX_DEMO",
            "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
            "non_production": True,
            "attestation_request_job_id": result.research_job_id,
            "retry_of_job_id": result.retry_of_job_id,
            "idempotency_key_digest": result.idempotency_key_digest,
            "credential_values_recorded": False,
        }

    return operator_request_coordinator.execute(
        operator_headers,
        operation="okx-demo-controlled-canary-retry",
        provider_call=True,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
        cache_result=_cache_canary_retry_result,
    )


@router.post("/canary/recover-execution-only", status_code=202)
def recover_fresh_execution_only_canary(
    payload: CanaryPreparationRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> dict[str, Any]:
    """Create the one bounded recovery successor after the #616 ACL failure.

    This is not a third normal refresh.  The service only accepts the exact
    immutable depth-two execution-only lineage and retains the existing
    grant/writer gates for any later order attempt.
    """

    def execute() -> dict[str, Any]:
        try:
            result = OkxDemoCanaryPreparationService(
                db
            ).prepare_fresh_execution_only_recovery(
                idempotency_key=operator_headers.idempotency_key or "",
            )
        except OkxDemoCanaryPreparationRuntimeBusy as exc:
            response = {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "entry_kind": exc.entry_kind or FRESH_EXECUTION_ONLY_RECOVERY,
                "non_production": True,
                "supersedes_job_ids": list(exc.supersedes_job_ids),
                "credential_values_recorded": False,
            }
            if exc.job_id is not None:
                response["attestation_request_job_id"] = exc.job_id
            if exc.recovery_of_job_id is not None:
                response["recovery_of_job_id"] = exc.recovery_of_job_id
            return response
        except OkxDemoCanaryPreparationWaiting as exc:
            response = {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "entry_kind": exc.entry_kind or FRESH_EXECUTION_ONLY_RECOVERY,
                "non_production": True,
                "attestation_request_job_id": exc.job_id,
                "supersedes_job_ids": list(exc.supersedes_job_ids),
                "credential_values_recorded": False,
            }
            if exc.recovery_of_job_id is not None:
                response["recovery_of_job_id"] = exc.recovery_of_job_id
            return response
        except OkxDemoCanaryPreparationBlocked as exc:
            raise HTTPException(
                status_code=409,
                detail={"operation_status": "BLOCKED", "message": str(exc)},
            ) from exc
        response = _canary_preparation_response(result)
        response["entry_kind"] = result.entry_kind or FRESH_EXECUTION_ONLY_RECOVERY
        return response

    return operator_request_coordinator.execute(
        operator_headers,
        operation="okx-demo-controlled-canary-recover-execution-only",
        provider_call=True,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
        cache_result=_cache_canary_result,
    )


@router.post("/canary/recover-post-persistence", status_code=202)
def recover_post_persistence_canary(
    payload: CanaryPreparationRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> dict[str, Any]:
    """Create the only fresh successor after job20's atomic write rollback."""

    def execute() -> dict[str, Any]:
        try:
            result = OkxDemoCanaryPreparationService(
                db
            ).prepare_post_persistence_recovery(
                idempotency_key=operator_headers.idempotency_key or "",
            )
        except OkxDemoCanaryPreparationRuntimeBusy as exc:
            response = {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "entry_kind": exc.entry_kind
                or FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY,
                "non_production": True,
                "supersedes_job_ids": list(exc.supersedes_job_ids),
                "credential_values_recorded": False,
            }
            if exc.job_id is not None:
                response["attestation_request_job_id"] = exc.job_id
            if exc.recovery_of_job_id is not None:
                response["recovery_of_job_id"] = exc.recovery_of_job_id
            return response
        except OkxDemoCanaryPreparationWaiting as exc:
            return {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "entry_kind": exc.entry_kind
                or FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY,
                "non_production": True,
                "attestation_request_job_id": exc.job_id,
                "recovery_of_job_id": exc.recovery_of_job_id,
                "supersedes_job_ids": list(exc.supersedes_job_ids),
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
        operation="okx-demo-controlled-canary-recover-post-persistence",
        provider_call=True,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
        cache_result=_cache_canary_result,
    )


@router.post("/canary/recover-final-expiry", status_code=202)
def recover_final_expiry_canary(
    payload: CanaryPreparationRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> dict[str, Any]:
    """Create the sole final successor after post-persistence snapshot expiry."""

    def execute() -> dict[str, Any]:
        try:
            result = OkxDemoCanaryPreparationService(
                db
            ).prepare_final_expiry_recovery(
                idempotency_key=operator_headers.idempotency_key or "",
            )
        except OkxDemoCanaryPreparationRuntimeBusy as exc:
            response = {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "entry_kind": exc.entry_kind
                or FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY,
                "non_production": True,
                "supersedes_job_ids": list(exc.supersedes_job_ids),
                "credential_values_recorded": False,
            }
            if exc.job_id is not None:
                response["attestation_request_job_id"] = exc.job_id
            if exc.recovery_of_job_id is not None:
                response["recovery_of_job_id"] = exc.recovery_of_job_id
            return response
        except OkxDemoCanaryPreparationWaiting as exc:
            return {
                "operation_status": "WAITING_FOR_RUNTIME_ATTESTATION",
                "execution_target_id": "OKX_DEMO",
                "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
                "entry_kind": exc.entry_kind
                or FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY,
                "non_production": True,
                "attestation_request_job_id": exc.job_id,
                "recovery_of_job_id": exc.recovery_of_job_id,
                "supersedes_job_ids": list(exc.supersedes_job_ids),
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
        operation="okx-demo-controlled-canary-recover-final-expiry",
        provider_call=True,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
        cache_result=_cache_canary_result,
    )


@router.post("/canary/consent-finalize", status_code=202)
def consent_final_attestation_canary(
    payload: CanaryPreparationRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> dict[str, Any]:
    """Persist the sole consent-bound handoff; the canonical runtime finalizes it."""

    def execute() -> dict[str, Any]:
        try:
            result = OkxDemoCanaryPreparationService(
                db
            ).request_final_attestation_consent(
                idempotency_key=operator_headers.idempotency_key or "",
                operator_token=operator_headers.operator_token or "",
            )
        except OkxDemoCanaryPreparationBlocked as exc:
            raise HTTPException(
                status_code=409,
                detail={"operation_status": "BLOCKED", "message": str(exc)},
            ) from exc
        return {
            "operation_status": result.operation_status,
            "execution_target_id": "OKX_DEMO",
            "provenance": "CONTROLLED_CANARY_NON_PRODUCTION",
            "non_production": True,
            "handoff_id": result.handoff_id,
            "source_job_id": result.source_job_id,
            "consent_deadline_at": result.consent_deadline_at.isoformat(),
            "credential_values_recorded": False,
        }

    return operator_request_coordinator.execute(
        operator_headers,
        operation="okx-demo-canary-consent-finalize",
        provider_call=True,
        request_payload=payload.model_dump(mode="json"),
        handler=execute,
        cache_result=_cache_terminal_canary_consent_result,
    )


@router.post("/submission-grants/one-shot", status_code=202)
def arm_one_shot_submission_grant(
    payload: OneShotGrantRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> dict[str, Any]:
    raise HTTPException(
        status_code=409,
        detail={
            "operation_status": "BLOCKED",
            "message": "Legacy one-shot arming is disabled; use consent-finalize.",
        },
    )

    # Retained below as unreachable compatibility documentation until the
    # endpoint is removed in a separately versioned API change.
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
