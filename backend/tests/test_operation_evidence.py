import pytest
from pydantic import ValidationError

from app.schemas.data_source import api_aggregate_source
from app.schemas.operation_evidence import OperationEvidence, operation_error_evidence


def test_acceptance_ready_success_requires_persisted_core_ids() -> None:
    source = api_aggregate_source("operation", {"strategy_id": 7})
    evidence = OperationEvidence(
        status="SUCCESS",
        ids={"strategy_id": 7},
        data_source=source,
        next_action="Refresh the strategy API.",
        acceptance_ready=True,
    )

    assert evidence.data_source.core_data is True
    assert evidence.failed_reason is None
    assert evidence.blocked_reason is None


def test_failed_and_blocked_evidence_require_explicit_reason() -> None:
    with pytest.raises(ValidationError, match="FAILED evidence requires failed_reason"):
        OperationEvidence(status="FAILED", next_action="Retry.")

    blocked = operation_error_evidence(
        status="BLOCKED",
        reason="strategy version not found",
        next_action="Create a persisted strategy version.",
        ids={"strategy_version_id": 99},
    )
    assert blocked.blocked_reason == "strategy version not found"
    assert blocked.acceptance_ready is False
