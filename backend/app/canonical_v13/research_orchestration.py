"""Production no-trade research sequencing, batching, and read projections."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import Connection, select

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.models import (
    QUALIFICATION_DECISIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
    TARGET_SCORES_TABLE,
    VALIDATION_ATTEMPTS_TABLE,
    VALIDATION_PLANS_TABLE,
)
from app.canonical_v13.research_authorization import (
    ResearchAuthorizationConsumption,
    verify_persisted_research_authorization_consumption,
    verify_research_authorization_consumption,
)
from app.canonical_v13.research_execution import IsolatedResearchExecutorPort
from app.canonical_v13.research_qualification import (
    QualificationReceipt,
    persist_qualification_receipt,
)
from app.canonical_v13.research_scoring import (
    ScoringReceipt,
    persist_scoring_receipt,
)
from app.canonical_v13.research_validation import (
    EphemeralAttemptReceipt,
    RunningValidationAttempt,
    build_ephemeral_attempt_receipt,
    load_running_validation_attempt,
    record_terminal_attempt,
)
from app.canonical_v13.runtime_reader import FrozenResearchBundle


class CanonicalResearchOrchestrationBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class ConnectionFactory(Protocol):
    def __call__(self) -> AbstractContextManager[Connection]: ...


@dataclass(frozen=True)
class SerialResearchBatch:
    target_id: UUID
    target_key: str
    batch_number: int
    per_target_cap: int
    strategy_version_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class ResearchChainProjection:
    validation_plan_id: UUID
    validation_plan_digest: str
    strategy_version_id: UUID
    research_target_id: UUID
    target_key: str
    plan_status: str
    validation_attempt_id: UUID | None
    attempt_status: str | None
    attempt_receipt_digest: str | None
    target_score_id: UUID | None
    overall_score: str | None
    score_digest: str | None
    qualification_decision_id: UUID | None
    qualification_status: str | None
    qualification_reason_code: str | None
    qualification_decision_digest: str | None


@dataclass(frozen=True)
class ProductionResearchChainReceipt:
    validation_attempt_id: UUID
    authorization_id: UUID
    consumption_receipt_digest: str
    attempt_status: str
    attempt_receipt_digest: str
    scoring_receipt: ScoringReceipt | None
    qualification_receipt: QualificationReceipt | None
    trading_capability: str = "TRADING_DISABLED"


def plan_serial_research_batches(
    frozen_bundle: FrozenResearchBundle,
    *,
    candidates_by_target: Mapping[UUID, Sequence[UUID]],
) -> tuple[SerialResearchBatch, ...]:
    """Chunk each dynamic target independently by its frozen explicit cap."""

    targets = {item.research_target_id: item for item in frozen_bundle.targets}
    allocations = {item.research_target_id: item for item in frozen_bundle.allocations}
    if set(candidates_by_target) != set(targets) or set(allocations) != set(targets):
        raise CanonicalResearchOrchestrationBlocked(
            "BLOCKED_RESEARCH_TARGET_SET_MISMATCH",
            "candidate mapping must exactly equal the frozen dynamic target set",
        )
    batches: list[SerialResearchBatch] = []
    globally_seen: set[UUID] = set()
    for target_id, target in sorted(
        targets.items(), key=lambda item: item[1].target_key
    ):
        allocation = allocations[target_id]
        cap = allocation.candidate_cap
        candidates = tuple(candidates_by_target[target_id])
        if cap <= 0 or not candidates or len(candidates) != len(set(candidates)):
            raise CanonicalResearchOrchestrationBlocked(
                "BLOCKED_RESEARCH_BATCH_INPUT",
                "each target requires unique candidates and a positive frozen cap",
            )
        if globally_seen.intersection(candidates):
            raise CanonicalResearchOrchestrationBlocked(
                "BLOCKED_RESEARCH_CANDIDATE_TARGET_AMBIGUOUS",
                "one strategy version cannot be scheduled under multiple targets",
            )
        globally_seen.update(candidates)
        for offset in range(0, len(candidates), cap):
            batches.append(
                SerialResearchBatch(
                    target_id=target_id,
                    target_key=target.target_key,
                    batch_number=(offset // cap) + 1,
                    per_target_cap=cap,
                    strategy_version_ids=candidates[offset : offset + cap],
                )
            )
    return tuple(batches)


def read_research_chain_projection(
    connection: Connection, *, validation_plan_id: UUID
) -> ResearchChainProjection:
    verification = verify_canonical_genesis(connection)
    if not verification.accepted:
        raise CanonicalResearchOrchestrationBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    plan = (
        connection.execute(
            select(VALIDATION_PLANS_TABLE).where(
                VALIDATION_PLANS_TABLE.c.id == validation_plan_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if plan is None:
        raise CanonicalResearchOrchestrationBlocked(
            "BLOCKED_VALIDATION_PLAN_NOT_FOUND", "canonical validation plan is absent"
        )
    target_key = connection.execute(
        select(RESEARCH_TARGETS_TABLE.c.target_key).where(
            RESEARCH_TARGETS_TABLE.c.id == plan["research_target_id"]
        )
    ).scalar_one()
    attempts = (
        connection.execute(
            select(VALIDATION_ATTEMPTS_TABLE)
            .where(VALIDATION_ATTEMPTS_TABLE.c.validation_plan_id == validation_plan_id)
            .order_by(VALIDATION_ATTEMPTS_TABLE.c.attempt_number.desc())
        )
        .mappings()
        .all()
    )
    attempt = attempts[0] if attempts else None
    score = (
        connection.execute(
            select(TARGET_SCORES_TABLE).where(
                TARGET_SCORES_TABLE.c.validation_plan_id == validation_plan_id,
                TARGET_SCORES_TABLE.c.validation_plan_digest
                == plan["validation_plan_digest"],
            )
        )
        .mappings()
        .one_or_none()
    )
    decision = (
        connection.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.validation_plan_id
                == validation_plan_id,
                QUALIFICATION_DECISIONS_TABLE.c.validation_plan_digest
                == plan["validation_plan_digest"],
            )
        )
        .mappings()
        .one_or_none()
    )
    return ResearchChainProjection(
        validation_plan_id=plan["id"],
        validation_plan_digest=plan["validation_plan_digest"],
        strategy_version_id=plan["strategy_version_id"],
        research_target_id=plan["research_target_id"],
        target_key=target_key,
        plan_status=plan["status"],
        validation_attempt_id=attempt["id"] if attempt else None,
        attempt_status=attempt["status"] if attempt else None,
        attempt_receipt_digest=attempt["receipt_digest"] if attempt else None,
        target_score_id=score["id"] if score else None,
        overall_score=str(score["overall_score"]) if score else None,
        score_digest=score["score_digest"] if score else None,
        qualification_decision_id=decision["id"] if decision else None,
        qualification_status=decision["status"] if decision else None,
        qualification_reason_code=decision["reason_code"] if decision else None,
        qualification_decision_digest=decision["decision_digest"] if decision else None,
    )


def _run_transaction(
    factory: ConnectionFactory, handler: Callable[[Connection], object]
) -> object:
    with factory() as connection:
        if not isinstance(connection, Connection) or connection.closed:
            raise CanonicalResearchOrchestrationBlocked(
                "BLOCKED_RESEARCH_CONNECTION_FACTORY",
                "factory did not yield a connection",
            )
        if connection.in_transaction():
            raise CanonicalResearchOrchestrationBlocked(
                "BLOCKED_RESEARCH_TRANSACTION_OWNERSHIP", "connection must be idle"
            )
        with connection.begin():
            return handler(connection)


def _verify_execution_boundary(
    *,
    running: RunningValidationAttempt,
    consumption: ResearchAuthorizationConsumption,
    executor: IsolatedResearchExecutorPort,
) -> None:
    verify_research_authorization_consumption(consumption)
    if (
        consumption.environment_class != "PRODUCTION_RESEARCH"
        or executor.environment_class != "PRODUCTION_RESEARCH"
        or executor.network_mode != "none"
        or executor.credential_mounts
        or executor.exchange_capabilities
        or executor.order_capabilities
        or executor.writer_capabilities
    ):
        raise CanonicalResearchOrchestrationBlocked(
            "BLOCKED_PRODUCTION_EXECUTOR_CAPABILITY",
            "production executor capability envelope drifted",
        )
    if (
        consumption.attempt_id != running.validation_attempt_id
        or consumption.lineage != running.launch_spec.lineage
        or consumption.validation_plan_id != running.launch_spec.validation_plan_id
        or consumption.validation_plan_digest
        != running.launch_spec.validation_plan_digest
    ):
        raise CanonicalResearchOrchestrationBlocked(
            "BLOCKED_EXECUTION_AUTHORIZATION_LINEAGE",
            "consumption receipt does not bind the running attempt",
        )


def execute_production_research_chain(
    *,
    audit_connection_factory: ConnectionFactory,
    validation_connection_factory: ConnectionFactory,
    scoring_connection_factory: ConnectionFactory,
    qualification_connection_factory: ConnectionFactory,
    validation_attempt_id: UUID,
    expected_plan_digest: str,
    authorization_consumption: ResearchAuthorizationConsumption,
    executor: IsolatedResearchExecutorPort,
    scorer_identity: str,
    qualifier_identity: str,
) -> ProductionResearchChainReceipt:
    """Run sandbox, then validation -> score -> qualifier in separate transactions."""

    if (
        not scorer_identity
        or not qualifier_identity
        or scorer_identity == qualifier_identity
    ):
        raise CanonicalResearchOrchestrationBlocked(
            "BLOCKED_EVALUATION_CAPABILITY_OVERLAP",
            "scorer and qualifier identities must be distinct",
        )
    _run_transaction(
        audit_connection_factory,
        lambda connection: verify_persisted_research_authorization_consumption(
            connection, consumption=authorization_consumption
        ),
    )
    running = _run_transaction(
        validation_connection_factory,
        lambda connection: load_running_validation_attempt(
            connection,
            validation_attempt_id=validation_attempt_id,
            expected_plan_digest=expected_plan_digest,
        ),
    )
    assert isinstance(running, RunningValidationAttempt)
    _verify_execution_boundary(
        running=running,
        consumption=authorization_consumption,
        executor=executor,
    )
    try:
        receipt = executor.execute(running)
        if not isinstance(receipt, EphemeralAttemptReceipt):
            raise TypeError("executor returned another receipt type")
    except Exception:
        receipt = build_ephemeral_attempt_receipt(
            running, metrics_by_window_key={}, status="BLOCKED"
        )
    _run_transaction(
        validation_connection_factory,
        lambda connection: record_terminal_attempt(connection, receipt=receipt),
    )
    if receipt.status != "SUCCEEDED":
        return ProductionResearchChainReceipt(
            validation_attempt_id=validation_attempt_id,
            authorization_id=authorization_consumption.authorization_id,
            consumption_receipt_digest=authorization_consumption.receipt_digest,
            attempt_status=receipt.status,
            attempt_receipt_digest=receipt.receipt_digest,
            scoring_receipt=None,
            qualification_receipt=None,
        )
    scoring = _run_transaction(
        scoring_connection_factory,
        lambda connection: persist_scoring_receipt(
            connection,
            validation_plan_id=running.launch_spec.validation_plan_id,
            validation_attempt_id=validation_attempt_id,
            scorer_identity=scorer_identity,
        ),
    )
    assert isinstance(scoring, ScoringReceipt)
    qualification = _run_transaction(
        qualification_connection_factory,
        lambda connection: persist_qualification_receipt(
            connection,
            validation_plan_id=running.launch_spec.validation_plan_id,
            validation_attempt_id=validation_attempt_id,
            qualifier_identity=qualifier_identity,
        ),
    )
    assert isinstance(qualification, QualificationReceipt)
    return ProductionResearchChainReceipt(
        validation_attempt_id=validation_attempt_id,
        authorization_id=authorization_consumption.authorization_id,
        consumption_receipt_digest=authorization_consumption.receipt_digest,
        attempt_status=receipt.status,
        attempt_receipt_digest=receipt.receipt_digest,
        scoring_receipt=scoring,
        qualification_receipt=qualification,
    )


__all__ = [
    "CanonicalResearchOrchestrationBlocked",
    "ConnectionFactory",
    "ProductionResearchChainReceipt",
    "ResearchChainProjection",
    "SerialResearchBatch",
    "execute_production_research_chain",
    "plan_serial_research_batches",
    "read_research_chain_projection",
]
