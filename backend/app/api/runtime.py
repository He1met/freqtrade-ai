from fastapi import APIRouter

from app.core.config import get_settings
from app.core.execution_target import ExecutionTargetManifest
from app.schemas.operator_status import OperatorStatusReport
from app.schemas.runtime_contract import RuntimeReadOnlyContract
from app.services.operator_status import OperatorStatusService
from app.services.runtime_contract import RuntimeReadOnlyContractService


router = APIRouter(prefix="/runtime", tags=["runtime"])


@router.get("/read-only", response_model=RuntimeReadOnlyContract)
def runtime_read_only_contract() -> RuntimeReadOnlyContract:
    return RuntimeReadOnlyContractService().build_contract()


@router.get("/execution-target", response_model=ExecutionTargetManifest)
def runtime_execution_target() -> ExecutionTargetManifest:
    """Return routing identity only; credentials are deliberately absent."""

    return get_settings().execution_target_manifest


@router.get("/operator-status", response_model=OperatorStatusReport)
def runtime_operator_status() -> OperatorStatusReport:
    return OperatorStatusService().build_status()
