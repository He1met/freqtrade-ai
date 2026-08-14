"""Qualifier-only terminal decision entrypoint.

Qualification reads an immutable score receipt but never accepts caller-computed
readiness.  Hard gates remain authoritative and are evaluated before minimum score.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection

from app.canonical_v13.research_evaluation import qualify_target


QUALIFICATION_RECEIPT_CONTRACT = "canonical-v13-qualification-receipt-v1"


@dataclass(frozen=True)
class QualificationReceipt:
    contract: str
    qualification_decision_id: UUID
    target_score_id: UUID
    validation_plan_id: UUID
    validation_attempt_id: UUID
    quality_snapshot_id: UUID
    status: str
    reason_code: str
    decision_digest: str
    evidence_count: int
    repeat_noop: bool


def persist_qualification_receipt(
    connection: Connection,
    *,
    validation_plan_id: UUID,
    validation_attempt_id: UUID,
    qualifier_identity: str,
) -> QualificationReceipt:
    result = qualify_target(
        connection,
        validation_plan_id=validation_plan_id,
        validation_attempt_id=validation_attempt_id,
        qualifier_identity=qualifier_identity,
    )
    return QualificationReceipt(
        contract=QUALIFICATION_RECEIPT_CONTRACT, **result.__dict__
    )


__all__ = [
    "QUALIFICATION_RECEIPT_CONTRACT",
    "QualificationReceipt",
    "persist_qualification_receipt",
]
