"""Authorized isolated research execution orchestration.

This module does not implement a backtest engine. It composes an injected isolated
adapter with exact one-shot authority and the existing immutable receipt recorder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol
from uuid import UUID

from sqlalchemy import Connection

from app.canonical_v13.research_authorization import (
    CanonicalResearchAuthorizationBlocked,
    ResearchAuthorizationConsumption,
    verify_research_authorization_consumption,
)
from app.canonical_v13.research_validation import (
    EphemeralAttemptReceipt,
    RunningValidationAttempt,
    TerminalAttemptResult,
    record_terminal_attempt,
    simulate_ephemeral_attempt,
    validate_ephemeral_launch_spec,
)


class CanonicalResearchExecutionBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class IsolatedResearchExecutorPort(Protocol):
    environment_class: str
    network_mode: str
    credential_mounts: tuple[str, ...]
    exchange_capabilities: tuple[str, ...]
    order_capabilities: tuple[str, ...]
    writer_capabilities: tuple[str, ...]

    def execute(
        self, running_attempt: RunningValidationAttempt
    ) -> EphemeralAttemptReceipt:
        ...


@dataclass(frozen=True)
class AuthorizedResearchExecutionResult:
    authorization_id: UUID
    consumption_receipt_digest: str
    attempt_receipt_digest: str
    attempt_status: str
    plan_status: str
    environment_class: str


@dataclass(frozen=True)
class SimulatedResearchExecutor:
    """Test-only adapter around caller-supplied metrics, never production evidence."""

    metrics_by_window_key: Mapping[str, Mapping[str, object]]
    status: str = "SUCCEEDED"
    environment_class: str = "ISOLATED_TEST"
    network_mode: str = "none"
    credential_mounts: tuple[str, ...] = ()
    exchange_capabilities: tuple[str, ...] = ()
    order_capabilities: tuple[str, ...] = ()
    writer_capabilities: tuple[str, ...] = ()

    def execute(
        self, running_attempt: RunningValidationAttempt
    ) -> EphemeralAttemptReceipt:
        return simulate_ephemeral_attempt(
            running_attempt,
            metrics_by_window_key=self.metrics_by_window_key,
            status=self.status,
        )


def _validate_executor(executor: IsolatedResearchExecutorPort) -> None:
    if (
        executor.environment_class not in {"ISOLATED_TEST", "PRODUCTION_RESEARCH"}
        or executor.network_mode != "none"
        or executor.credential_mounts
        or executor.exchange_capabilities
        or executor.order_capabilities
        or executor.writer_capabilities
    ):
        raise CanonicalResearchExecutionBlocked(
            "BLOCKED_EXECUTOR_CAPABILITY_DRIFT",
            "research executor must be ephemeral, networkless, and writerless",
        )


def execute_consumed_research_attempt(
    connection: Connection,
    *,
    running_attempt: RunningValidationAttempt,
    authorization_consumption: ResearchAuthorizationConsumption | None,
    executor: IsolatedResearchExecutorPort,
) -> AuthorizedResearchExecutionResult:
    """Accept a control receipt, then persist through the validation writer.

    Authorization issuance/consumption is deliberately a separate control-writer
    transaction.  This function never writes ``audit_events`` and therefore remains
    executable by the isolated ``canonical_validation_writer`` identity.
    """

    _validate_executor(executor)
    validate_ephemeral_launch_spec(running_attempt.launch_spec)
    if authorization_consumption is None:
        raise CanonicalResearchExecutionBlocked(
            "BLOCKED_EXPLICIT_AUTHORITY_REQUIRED",
            "each attempt requires one immutable authorization receipt",
        )
    if executor.environment_class != "ISOLATED_TEST":
        raise CanonicalResearchExecutionBlocked(
            "BLOCKED_EXPLICIT_AUTHORITY_REQUIRED",
            "real research execution is outside this isolated acceptance",
        )
    try:
        verify_research_authorization_consumption(authorization_consumption)
    except CanonicalResearchAuthorizationBlocked as exc:
        raise CanonicalResearchExecutionBlocked(
            "BLOCKED_EXECUTION_AUTHORIZATION_DIGEST_DRIFT",
            "control-plane authorization receipt did not verify",
        ) from exc
    consumption = authorization_consumption
    if (
        consumption.attempt_id != running_attempt.validation_attempt_id
        or consumption.lineage != running_attempt.launch_spec.lineage
        or consumption.validation_plan_id
        != running_attempt.launch_spec.validation_plan_id
        or consumption.validation_plan_digest
        != running_attempt.launch_spec.validation_plan_digest
        or consumption.environment_class != executor.environment_class
    ):
        raise CanonicalResearchExecutionBlocked(
            "BLOCKED_EXECUTION_AUTHORIZATION_LINEAGE",
            "authorization receipt does not match this attempt and executor",
        )
    receipt = executor.execute(running_attempt)
    terminal: TerminalAttemptResult = record_terminal_attempt(
        connection, receipt=receipt
    )
    return AuthorizedResearchExecutionResult(
        authorization_id=consumption.authorization_id,
        consumption_receipt_digest=consumption.receipt_digest,
        attempt_receipt_digest=terminal.receipt_digest,
        attempt_status=terminal.attempt_status,
        plan_status=terminal.plan_status,
        environment_class=executor.environment_class,
    )


__all__ = [
    "AuthorizedResearchExecutionResult",
    "CanonicalResearchExecutionBlocked",
    "IsolatedResearchExecutorPort",
    "SimulatedResearchExecutor",
    "execute_consumed_research_attempt",
]
