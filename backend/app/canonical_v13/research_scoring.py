"""Scoring-only persistence entrypoint.

This module intentionally does not import or expose qualification functions.  Its
production caller must use the canonical scoring writer, whose PostgreSQL ACL can
write only ``target_scores``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection

from app.canonical_v13.research_evaluation import score_target


SCORING_RECEIPT_CONTRACT = "canonical-v13-scoring-receipt-v1"


@dataclass(frozen=True)
class ScoringReceipt:
    contract: str
    target_score_id: UUID
    validation_plan_id: UUID
    validation_attempt_id: UUID
    scoring_snapshot_id: UUID
    overall_score: Decimal
    required_window_result_set_digest: str
    score_digest: str
    required_window_count: int
    repeat_noop: bool


def persist_scoring_receipt(
    connection: Connection,
    *,
    validation_plan_id: UUID,
    validation_attempt_id: UUID,
    scorer_identity: str,
) -> ScoringReceipt:
    result = score_target(
        connection,
        validation_plan_id=validation_plan_id,
        validation_attempt_id=validation_attempt_id,
        scorer_identity=scorer_identity,
    )
    return ScoringReceipt(contract=SCORING_RECEIPT_CONTRACT, **result.__dict__)


__all__ = [
    "SCORING_RECEIPT_CONTRACT",
    "ScoringReceipt",
    "persist_scoring_receipt",
]
