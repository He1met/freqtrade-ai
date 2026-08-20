"""Read-only Phase 9 handoff and zero-side-effect acceptance receipts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Final, Mapping
from uuid import UUID

from sqlalchemy import Connection, func, select

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import (
    DEPLOYMENTS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    FILLS_TABLE,
    LEDGER_ENTRIES_TABLE,
    ORDERS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RECONCILIATION_ITEMS_TABLE,
    RECONCILIATION_RUNS_TABLE,
    RISK_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SIGNALS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.phase9_topology import phase9_topology_digest
from app.canonical_v13.research_evaluation import gate_optimization


class CanonicalPhase9ReadinessBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Phase9QualificationHandoff:
    qualification_decision_id: UUID
    qualification_decision_digest: str
    strategy_version_id: UUID
    research_target_id: UUID
    configuration_bundle_id: UUID
    configuration_bundle_digest: str
    market_snapshot_id: UUID
    market_snapshot_digest: str
    validation_plan_id: UUID
    validation_plan_digest: str


@dataclass(frozen=True)
class Phase9ReadinessReceipt:
    contract: str
    stage: str
    status: str
    reason_codes: tuple[str, ...]
    qualification_status_counts: Mapping[str, int]
    execution_domain_counts: Mapping[str, int]
    handoff: Phase9QualificationHandoff | None
    topology_digest: str
    receipt_digest: str


_EXECUTION_TABLES = {
    "deployment_approvals": DEPLOYMENT_APPROVALS_TABLE,
    "deployments": DEPLOYMENTS_TABLE,
    "runtime_instances": RUNTIME_INSTANCES_TABLE,
    "runtime_receipts": RUNTIME_RECEIPTS_TABLE,
    "signals": SIGNALS_TABLE,
    "trade_intents": TRADE_INTENTS_TABLE,
    "risk_decisions": RISK_DECISIONS_TABLE,
    "orders": ORDERS_TABLE,
    "fills": FILLS_TABLE,
    "ledger_entries": LEDGER_ENTRIES_TABLE,
    "reconciliation_runs": RECONCILIATION_RUNS_TABLE,
    "reconciliation_items": RECONCILIATION_ITEMS_TABLE,
}
EXECUTION_DOMAIN_TABLE_NAMES: Final[tuple[str, ...]] = tuple(_EXECUTION_TABLES)

_ZERO_REQUIRED_BY_STAGE = {
    "QUALIFICATION_HANDOFF": frozenset(EXECUTION_DOMAIN_TABLE_NAMES),
    "NO_ORDER_SOAK": frozenset(
        {
            "signals",
            "trade_intents",
            "risk_decisions",
            "orders",
            "fills",
            "ledger_entries",
            "reconciliation_runs",
            "reconciliation_items",
        }
    ),
    "SIGNAL_RISK_SHADOW": frozenset(
        {
            "orders",
            "fills",
            "ledger_entries",
            "reconciliation_runs",
            "reconciliation_items",
        }
    ),
}
_MINIMUM_REQUIRED_BY_STAGE = {
    "QUALIFICATION_HANDOFF": {},
    "NO_ORDER_SOAK": {
        "deployment_approvals": 1,
        "deployments": 1,
        "runtime_instances": 1,
        "runtime_receipts": 1,
    },
    "SIGNAL_RISK_SHADOW": {
        "deployment_approvals": 1,
        "deployments": 1,
        "runtime_instances": 1,
        "runtime_receipts": 1,
        "signals": 1,
        "trade_intents": 1,
        "risk_decisions": 1,
    },
}
PHASE9_ACCEPTANCE_STAGES: Final[tuple[str, ...]] = tuple(_ZERO_REQUIRED_BY_STAGE)


def _effective(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def inspect_phase9_readiness(
    connection: Connection,
    *,
    stage: str = "QUALIFICATION_HANDOFF",
) -> Phase9ReadinessReceipt:
    """Build a deterministic read-only receipt for one Phase 9 gate.

    This function deliberately has no command path.  In particular it never creates
    an approval, deployment, runtime, signal, intent, order, fill, ledger entry, or
    reconciliation row.
    """

    if stage not in _ZERO_REQUIRED_BY_STAGE:
        raise CanonicalPhase9ReadinessBlocked(
            "BLOCKED_PHASE9_STAGE", f"unsupported stage {stage!r}"
        )
    effective = _effective(connection)
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalPhase9ReadinessBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )

    status_rows = effective.execute(
        select(
            QUALIFICATION_DECISIONS_TABLE.c.status,
            func.count().label("row_count"),
        ).group_by(QUALIFICATION_DECISIONS_TABLE.c.status)
    ).all()
    qualification_counts = dict(
        sorted((str(status), int(count)) for status, count in status_rows)
    )
    execution_counts = {
        name: int(
            effective.execute(select(func.count()).select_from(table)).scalar_one()
        )
        for name, table in _EXECUTION_TABLES.items()
    }

    reasons: list[str] = []
    qualified_rows = effective.execute(
        select(QUALIFICATION_DECISIONS_TABLE)
        .where(QUALIFICATION_DECISIONS_TABLE.c.status == "QUALIFIED")
        .order_by(
            QUALIFICATION_DECISIONS_TABLE.c.created_at,
            QUALIFICATION_DECISIONS_TABLE.c.id,
        )
    ).mappings().all()
    handoff: Phase9QualificationHandoff | None = None
    if not qualified_rows:
        reasons.append("CURRENT_CANONICAL_QUALIFIED_UNSET")
    elif len(qualified_rows) != 1:
        reasons.append("CURRENT_CANONICAL_QUALIFIED_AMBIGUOUS")
    else:
        decision = qualified_rows[0]
        gate = gate_optimization(
            effective,
            baseline_qualification_decision_id=decision["id"],
        )
        if gate.status != "READY":
            reasons.append(gate.reason_code)
        else:
            handoff = Phase9QualificationHandoff(
                qualification_decision_id=decision["id"],
                qualification_decision_digest=decision["decision_digest"],
                strategy_version_id=decision["strategy_version_id"],
                research_target_id=decision["research_target_id"],
                configuration_bundle_id=decision["configuration_bundle_id"],
                configuration_bundle_digest=decision[
                    "configuration_bundle_digest"
                ],
                market_snapshot_id=decision["market_snapshot_id"],
                market_snapshot_digest=decision["market_snapshot_digest"],
                validation_plan_id=decision["validation_plan_id"],
                validation_plan_digest=decision["validation_plan_digest"],
            )

    for table_name in sorted(_ZERO_REQUIRED_BY_STAGE[stage]):
        count = execution_counts[table_name]
        if count:
            reasons.append(f"NONZERO_{table_name.upper()}={count}")
    for table_name, minimum in _MINIMUM_REQUIRED_BY_STAGE[stage].items():
        count = execution_counts[table_name]
        if count < minimum:
            reasons.append(f"{table_name.upper()}_EVIDENCE_UNSET")

    unique_reasons = tuple(dict.fromkeys(reasons))
    payload = {
        "contract": "canonical-v13-phase9-readiness-receipt-v1",
        "stage": stage,
        "status": "BLOCKED" if unique_reasons else "READY",
        "reason_codes": unique_reasons,
        "qualification_status_counts": dict(qualification_counts),
        "execution_domain_counts": dict(execution_counts),
        "handoff": (
            {
                key: str(value) if isinstance(value, UUID) else value
                for key, value in asdict(handoff).items()
            }
            if handoff is not None
            else None
        ),
        "topology_digest": phase9_topology_digest(),
    }
    return Phase9ReadinessReceipt(
        contract=payload["contract"],
        stage=stage,
        status=payload["status"],
        reason_codes=unique_reasons,
        qualification_status_counts=qualification_counts,
        execution_domain_counts=execution_counts,
        handoff=handoff,
        topology_digest=payload["topology_digest"],
        receipt_digest=_digest(payload),
    )


__all__ = [
    "EXECUTION_DOMAIN_TABLE_NAMES",
    "PHASE9_ACCEPTANCE_STAGES",
    "CanonicalPhase9ReadinessBlocked",
    "Phase9QualificationHandoff",
    "Phase9ReadinessReceipt",
    "inspect_phase9_readiness",
]
