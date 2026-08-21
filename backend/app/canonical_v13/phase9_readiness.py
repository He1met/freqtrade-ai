"""Read-only Phase 9 handoff and staged acceptance receipts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Final
from uuid import UUID

from sqlalchemy import Connection, func, select

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    FILLS_TABLE,
    LEDGER_ENTRIES_TABLE,
    ORDERS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RECONCILIATION_ITEMS_TABLE,
    RECONCILIATION_RUNS_TABLE,
    RISK_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SIGNALS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.order_service import CANONICAL_ORDER_WRITER_IDENTITY
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
    lineage_evidence_counts: Mapping[str, int]
    handoff: Phase9QualificationHandoff | None
    topology_digest: str
    receipt_digest: str


_EXECUTION_TABLES = {
    "deployment_approvals": DEPLOYMENT_APPROVALS_TABLE,
    "deployments": DEPLOYMENTS_TABLE,
    "runtime_instances": RUNTIME_INSTANCES_TABLE,
    "runtime_receipts": RUNTIME_RECEIPTS_TABLE,
    "execution_risk_budget_authorizations": EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    "execution_canary_risk_policies": EXECUTION_CANARY_RISK_POLICIES_TABLE,
    "execution_risk_reservations": EXECUTION_RISK_RESERVATIONS_TABLE,
    "execution_attestations": EXECUTION_ATTESTATIONS_TABLE,
    "order_writer_leases": ORDER_WRITER_LEASES_TABLE,
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
            "execution_risk_reservations",
            "execution_risk_budget_authorizations",
            "execution_canary_risk_policies",
            "execution_attestations",
            "order_writer_leases",
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
            "order_writer_leases",
            "orders",
            "fills",
            "ledger_entries",
            "reconciliation_runs",
            "reconciliation_items",
        }
    ),
    "OKX_DEMO_CANARY": frozenset(),
    "RECOVERY_SOAK": frozenset(),
}
PHASE9_ACCEPTANCE_STAGES: Final[tuple[str, ...]] = tuple(_ZERO_REQUIRED_BY_STAGE)


def _effective(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _persisted_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


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


def _handoff_from_decision(
    decision: Mapping[str, object],
) -> Phase9QualificationHandoff:
    return Phase9QualificationHandoff(
        qualification_decision_id=decision["id"],
        qualification_decision_digest=decision["decision_digest"],
        strategy_version_id=decision["strategy_version_id"],
        research_target_id=decision["research_target_id"],
        configuration_bundle_id=decision["configuration_bundle_id"],
        configuration_bundle_digest=decision["configuration_bundle_digest"],
        market_snapshot_id=decision["market_snapshot_id"],
        market_snapshot_digest=decision["market_snapshot_digest"],
        validation_plan_id=decision["validation_plan_id"],
        validation_plan_digest=decision["validation_plan_digest"],
    )


def _runtime_receipt_is_exact(
    receipt: Mapping[str, object],
    *,
    runtime: Mapping[str, object],
    deployment: Mapping[str, object],
    evaluated_at: datetime,
) -> bool:
    observation = receipt["observation_json"]
    if not isinstance(observation, Mapping):
        return False
    observed_at = receipt["observed_at"]
    if not isinstance(observed_at, datetime):
        return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age = evaluated_at - observed_at.astimezone(timezone.utc)
    return (
        -timedelta(seconds=5) <= age <= timedelta(minutes=5)
        and receipt["status"] == "HEALTHY"
        and receipt["evidence_class"] == "PRODUCTION_DEMO_RUNTIME"
        and receipt["runtime_instance_id"] == runtime["id"]
        and receipt["launch_spec_digest"] == runtime["launch_spec_digest"]
        and receipt["capability_digest"] == deployment["capability_digest"]
        and receipt["network_policy"]
        == runtime["network_policy"]
        == "DEMO_EXCHANGE_ONLY"
        and receipt["service_account"]
        == runtime["service_account"]
        == "canonical_runtime_reader"
        and receipt["order_writer_capability"] is False
        and observation.get("runtime_instance_id") == str(runtime["id"])
        and observation.get("launch_spec_digest") == runtime["launch_spec_digest"]
        and observation.get("capability_digest") == deployment["capability_digest"]
        and observation.get("status") == "HEALTHY"
        and observation.get("network_policy") == "DEMO_EXCHANGE_ONLY"
        and observation.get("service_account") == "canonical_runtime_reader"
        and observation.get("order_writer_capability") is False
        and observation.get("evidence_class") == "PRODUCTION_DEMO_RUNTIME"
        and _digest(dict(observation)) == receipt["observation_digest"]
        and _digest(
            {"contract": "canonical-v13-runtime-observation-v1", **dict(observation)}
        )
        == receipt["receipt_digest"]
    )


def _inspect_lineage(
    connection: Connection,
    *,
    handoff: Phase9QualificationHandoff,
    stage: str,
    evaluated_at: datetime,
    reasons: list[str],
) -> dict[str, int]:
    """Inspect only evidence reachable from the explicitly supplied handoff."""
    counts = {name: 0 for name in EXECUTION_DOMAIN_TABLE_NAMES}
    approvals = (
        connection.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.qualification_decision_id
                == handoff.qualification_decision_id
            )
        )
        .mappings()
        .all()
    )
    approvals = [
        row
        for row in approvals
        if row["strategy_version_id"] == handoff.strategy_version_id
        and row["status"] == "APPROVED"
    ]
    counts["deployment_approvals"] = len(approvals)
    if len(approvals) != 1:
        reasons.append("EXACT_APPROVED_DEPLOYMENT_APPROVAL_EVIDENCE_UNSET")
        return counts
    approval = approvals[0]

    deployments = (
        connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.deployment_approval_id == approval["id"]
            )
        )
        .mappings()
        .all()
    )
    deployments = [
        row
        for row in deployments
        if row["strategy_version_id"] == handoff.strategy_version_id
        and row["configuration_bundle_id"] == handoff.configuration_bundle_id
        and row["configuration_bundle_digest"] == handoff.configuration_bundle_digest
        and row["market_snapshot_id"] == handoff.market_snapshot_id
        and row["market_snapshot_digest"] == handoff.market_snapshot_digest
        and row["status"] == "ACTIVE"
        and row["demo_only"] is True
        and row["allow_real_funds"] is False
    ]
    counts["deployments"] = len(deployments)
    if len(deployments) != 1:
        reasons.append("EXACT_ACTIVE_DEMO_DEPLOYMENT_EVIDENCE_UNSET")
        return counts
    deployment = deployments[0]

    runtimes = (
        connection.execute(
            select(RUNTIME_INSTANCES_TABLE).where(
                RUNTIME_INSTANCES_TABLE.c.deployment_id == deployment["id"]
            )
        )
        .mappings()
        .all()
    )
    runtimes = [
        row
        for row in runtimes
        if row["status"] == "HEALTHY"
        and row["network_policy"] == "DEMO_EXCHANGE_ONLY"
        and row["runtime_class"] == "LONG_LIVED_TRADING_RUNTIME"
        and row["filesystem_mode"] == "READ_ONLY"
        and row["service_account"] == "canonical_runtime_reader"
        and row["research_executor_capability"] is False
        and row["order_writer_capability"] is False
    ]
    counts["runtime_instances"] = len(runtimes)
    if len(runtimes) != 1:
        reasons.append("EXACT_HEALTHY_LONG_LIVED_RUNTIME_EVIDENCE_UNSET")
        return counts
    runtime = runtimes[0]

    receipts = (
        connection.execute(
            select(RUNTIME_RECEIPTS_TABLE).where(
                RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime["id"]
            )
        )
        .mappings()
        .all()
    )
    receipts = [
        row
        for row in receipts
        if _runtime_receipt_is_exact(
            row,
            runtime=runtime,
            deployment=deployment,
            evaluated_at=evaluated_at,
        )
    ]
    counts["runtime_receipts"] = len(receipts)
    if not receipts:
        reasons.append("EXACT_PRODUCTION_RUNTIME_RECEIPT_EVIDENCE_UNSET")
        return counts
    if stage == "NO_ORDER_SOAK":
        return counts

    signals = (
        connection.execute(
            select(SIGNALS_TABLE).where(
                SIGNALS_TABLE.c.deployment_id == deployment["id"],
                SIGNALS_TABLE.c.runtime_instance_id == runtime["id"],
                SIGNALS_TABLE.c.strategy_version_id == handoff.strategy_version_id,
                SIGNALS_TABLE.c.research_target_id == handoff.research_target_id,
                SIGNALS_TABLE.c.configuration_bundle_id
                == handoff.configuration_bundle_id,
                SIGNALS_TABLE.c.configuration_bundle_digest
                == handoff.configuration_bundle_digest,
                SIGNALS_TABLE.c.market_snapshot_id == handoff.market_snapshot_id,
                SIGNALS_TABLE.c.market_snapshot_digest
                == handoff.market_snapshot_digest,
            )
        )
        .mappings()
        .all()
    )
    counts["signals"] = len(signals)
    signal_ids = [row["id"] for row in signals]
    intents = (
        connection.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.signal_id.in_(signal_ids)
            )
        )
        .mappings()
        .all()
        if signal_ids
        else []
    )
    counts["trade_intents"] = len(intents)
    intent_ids = [row["id"] for row in intents]
    risks = (
        connection.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.trade_intent_id.in_(intent_ids)
            )
        )
        .mappings()
        .all()
        if intent_ids
        else []
    )
    counts["risk_decisions"] = len(risks)
    budgets = (
        connection.execute(
            select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE).where(
                EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.deployment_approval_id
                == approval["id"]
            )
        )
        .mappings()
        .all()
    )
    counts["execution_risk_budget_authorizations"] = len(budgets)
    risk_policy_sources = (
        connection.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.qualification_decision_id
                == handoff.qualification_decision_id,
            )
        )
        .mappings()
        .all()
    )
    reservations = (
        connection.execute(
            select(EXECUTION_RISK_RESERVATIONS_TABLE).where(
                EXECUTION_RISK_RESERVATIONS_TABLE.c.trade_intent_id.in_(intent_ids)
            )
        )
        .mappings()
        .all()
        if intent_ids
        else []
    )
    counts["execution_risk_reservations"] = len(reservations)
    if not signals:
        reasons.append("EXACT_SIGNAL_EVIDENCE_UNSET")
    if not intents:
        reasons.append("EXACT_TRADE_INTENT_EVIDENCE_UNSET")
    if not any(row["status"] == "RISK_ACCEPTED" for row in risks):
        reasons.append("EXACT_RISK_ACCEPTED_EVIDENCE_UNSET")
    if not any(row["status"] == "REJECTED" for row in risks):
        reasons.append("EXACT_RISK_REJECTED_EVIDENCE_UNSET")
    if len(budgets) != 1:
        reasons.append("EXACT_RISK_BUDGET_AUTHORIZATION_EVIDENCE_UNSET")
    exact_policy_source = []
    for source in risk_policy_sources:
        if (
            source["strategy_version_id"] == handoff.strategy_version_id
            and source["research_target_id"] == handoff.research_target_id
            and source["configuration_bundle_id"] == handoff.configuration_bundle_id
            and source["configuration_bundle_digest"]
            == handoff.configuration_bundle_digest
            and source["market_snapshot_id"] == handoff.market_snapshot_id
            and source["market_snapshot_digest"] == handoff.market_snapshot_digest
            and source["execution_target"] == "OKX_DEMO"
            and source["allow_real_funds"] is False
            and source["position_policy"] == "LONG_ONLY"
            and source["max_order_count"] == 1
            and Decimal(str(source["strategy_max_leverage"])) == Decimal("14")
            and Decimal(str(source["effective_leverage"]))
            <= min(
                Decimal(str(source["strategy_max_leverage"])),
                Decimal(str(source["exchange_max_leverage"])),
            )
            and Decimal(str(source["max_notional"]))
            == Decimal(str(source["minimum_contract_size"]))
            * Decimal(str(source["contract_value"]))
            * Decimal(str(source["mark_price"]))
            and _persisted_utc(source["expires_at"])
            - _persisted_utc(source["accepted_at"])
            == timedelta(minutes=30)
            and any(
                budget["source_receipt_digest"] == source["receipt_digest"]
                and budget["execution_canary_risk_policy_id"] == source["id"]
                for budget in budgets
            )
        ):
            exact_policy_source.append(source)
    if not risk_policy_sources:
        reasons.extend(
            (
                "CANONICAL_PHASE9_RISK_BUDGET_SOURCE_UNSET",
                "CANONICAL_RISK_POLICY_LINEAGE_UNSET",
            )
        )
    elif len(exact_policy_source) != 1:
        reasons.append("CANONICAL_RISK_POLICY_LINEAGE_UNSET")
    accepted_reservation_digests = {
        row["reservation_digest"]
        for row in reservations
        if row["status"] == "RISK_ACCEPTED"
    }
    if any(
        row["status"] == "RISK_ACCEPTED"
        and row["decision_json"].get("reservation_digest")
        not in accepted_reservation_digests
        for row in risks
    ):
        reasons.append("EXACT_RISK_BUDGET_RESERVATION_EVIDENCE_UNSET")
    if stage == "SIGNAL_RISK_SHADOW":
        return counts

    attestations = (
        connection.execute(
            select(EXECUTION_ATTESTATIONS_TABLE).where(
                EXECUTION_ATTESTATIONS_TABLE.c.deployment_id == deployment["id"]
            )
        )
        .mappings()
        .all()
    )
    fresh_attestations = []
    for row in attestations:
        observed_at = row["observed_at"]
        expires_at = row["expires_at"]
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if (
            row["status"] == "READY"
            and row["execution_target"] == "OKX_DEMO"
            and row["permissions_json"]
            == {"read": True, "trade": True, "withdraw": False}
            and observed_at <= evaluated_at < expires_at
        ):
            fresh_attestations.append(row)
    counts["execution_attestations"] = len(fresh_attestations)
    if len(fresh_attestations) != 1:
        reasons.append("EXACT_FRESH_OKX_DEMO_ATTESTATION_EVIDENCE_UNSET")
    leases = (
        connection.execute(
            select(ORDER_WRITER_LEASES_TABLE).where(
                ORDER_WRITER_LEASES_TABLE.c.execution_target == "OKX_DEMO",
            )
        )
        .mappings()
        .all()
    )
    exact_leases = []
    for row in leases:
        expires_at = row["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expected_lease_digest = _digest(
            {
                "execution_target": "OKX_DEMO",
                "holder_identity": row["holder_identity"],
                "holder_token_digest": row["holder_token_digest"],
                "generation": row["generation"],
                "expires_at": expires_at.isoformat(),
            }
        )
        if (
            row["holder_identity"] == "canonical-v13-order-writer-v1"
            and row["generation"] > 0
            and row["status"] in {"ACTIVE", "RELEASED", "EXPIRED"}
            and row["lease_digest"] == expected_lease_digest
        ):
            exact_leases.append(row)
    counts["order_writer_leases"] = len(exact_leases)
    if len(exact_leases) != 1:
        reasons.append("EXACT_SINGLE_ORDER_WRITER_LEASE_EVIDENCE_UNSET")

    accepted_ids = [row["id"] for row in risks if row["status"] == "RISK_ACCEPTED"]
    orders = (
        connection.execute(
            select(ORDERS_TABLE).where(
                ORDERS_TABLE.c.risk_decision_id.in_(accepted_ids)
            )
        )
        .mappings()
        .all()
        if accepted_ids
        else []
    )
    counts["orders"] = len(orders)
    valid_orders = [
        row
        for row in orders
        if row["writer_identity"] == CANONICAL_ORDER_WRITER_IDENTITY
        and row["demo_only"] is True
        and row["allow_real_funds"] is False
        and row["exchange_order_id"]
        and row["status"] in {"ACCEPTED", "PARTIAL", "FILLED"}
        and row["receipt_digest"]
    ]
    if len(valid_orders) != 1:
        reasons.append("EXACT_SINGLE_OKX_DEMO_ORDER_EVIDENCE_UNSET")
        return counts
    order = valid_orders[0]
    fills = (
        connection.execute(
            select(FILLS_TABLE).where(FILLS_TABLE.c.order_id == order["id"])
        )
        .mappings()
        .all()
    )
    fills = [
        row
        for row in fills
        if row["fill_json"].get("evidence_class") == "PRODUCTION_OKX_DEMO"
        and row["fill_json"].get("allow_real_funds") is False
        and row["fill_json"].get("exchange_order_id") == order["exchange_order_id"]
    ]
    counts["fills"] = len(fills)
    fill_ids = [row["id"] for row in fills]
    ledgers = (
        connection.execute(
            select(LEDGER_ENTRIES_TABLE).where(
                LEDGER_ENTRIES_TABLE.c.fill_id.in_(fill_ids)
            )
        )
        .mappings()
        .all()
        if fill_ids
        else []
    )
    counts["ledger_entries"] = len(ledgers)
    ledger_ids = [row["id"] for row in ledgers]
    items = (
        connection.execute(
            select(RECONCILIATION_ITEMS_TABLE).where(
                RECONCILIATION_ITEMS_TABLE.c.order_id == order["id"],
                RECONCILIATION_ITEMS_TABLE.c.fill_id.in_(fill_ids),
                RECONCILIATION_ITEMS_TABLE.c.ledger_entry_id.in_(ledger_ids),
                RECONCILIATION_ITEMS_TABLE.c.status == "MATCHED",
            )
        )
        .mappings()
        .all()
        if fill_ids and ledger_ids
        else []
    )
    counts["reconciliation_items"] = len(items)
    run_ids = [row["reconciliation_run_id"] for row in items]
    runs = (
        connection.execute(
            select(RECONCILIATION_RUNS_TABLE).where(
                RECONCILIATION_RUNS_TABLE.c.id.in_(run_ids),
                RECONCILIATION_RUNS_TABLE.c.status == "SUCCEEDED",
                RECONCILIATION_RUNS_TABLE.c.receipt_digest.is_not(None),
            )
        )
        .mappings()
        .all()
        if run_ids
        else []
    )
    counts["reconciliation_runs"] = len(runs)
    if len(fills) != 1:
        reasons.append("EXACT_SINGLE_FILL_EVIDENCE_UNSET")
    if not ledgers:
        reasons.append("EXACT_LEDGER_EVIDENCE_UNSET")
    if not items or not runs:
        reasons.append("EXACT_RECONCILIATION_EVIDENCE_UNSET")
    return counts


def _recovery_acceptance_is_exact(
    event: Mapping[str, object], *, handoff: Phase9QualificationHandoff
) -> bool:
    evidence = event["evidence_json"]
    if not isinstance(evidence, Mapping):
        return False
    restart = evidence.get("runtime_restart")
    recovery = evidence.get("runtime_recovery")
    writer_stop = evidence.get("writer_stop")
    if not all(
        isinstance(value, Mapping) for value in (restart, recovery, writer_stop)
    ):
        return False
    return (
        event["event_type"] == "PHASE9_RECOVERY_SOAK_ACCEPTED"
        and event["aggregate_type"] == "canonical_phase9_recovery"
        and event["aggregate_id"] == str(handoff.qualification_decision_id)
        and evidence.get("contract") == "canonical-v13-phase9-recovery-acceptance-v1"
        and evidence.get("qualification_decision_id")
        == str(handoff.qualification_decision_id)
        and evidence.get("active_supervisor_lease_count") == 0
        and evidence.get("active_db_writer_lease_count") == 0
        and evidence.get("zombie_process_count") == 0
        and evidence.get("exact_order_count") == 1
        and restart.get("service_key") == "long_lived_runtime"
        and restart.get("action") == "RESTART"
        and restart.get("status") == "CONFIRMED"
        and recovery.get("service_key") == "long_lived_runtime"
        and recovery.get("action") == "RECOVER"
        and recovery.get("status") in {"RECOVERED", "NO_OP"}
        and recovery.get("generation") == restart.get("generation")
        and recovery.get("plan_digest") == restart.get("plan_digest")
        and writer_stop.get("service_key") == "order_writer"
        and writer_stop.get("action") == "STOP"
        and writer_stop.get("status") == "STOPPED"
        and _digest(dict(evidence)) == event["request_digest"]
        and _digest(
            {
                "event_type": "PHASE9_RECOVERY_SOAK_ACCEPTED",
                "request_digest": event["request_digest"],
            }
        )
        == event["receipt_digest"]
    )


def inspect_phase9_readiness(
    connection: Connection,
    *,
    qualification_handoff: Phase9QualificationHandoff,
    stage: str = "QUALIFICATION_HANDOFF",
    evaluated_at: datetime | None = None,
) -> Phase9ReadinessReceipt:
    """Build a deterministic, read-only receipt for one exact Phase 9 handoff."""
    if stage not in _ZERO_REQUIRED_BY_STAGE:
        raise CanonicalPhase9ReadinessBlocked(
            "BLOCKED_PHASE9_STAGE", f"unsupported stage {stage!r}"
        )
    resolved_now = evaluated_at or datetime.now(timezone.utc)
    if resolved_now.tzinfo is None:
        raise CanonicalPhase9ReadinessBlocked(
            "BLOCKED_PHASE9_TIMEZONE", "evaluated_at must be timezone-aware"
        )
    resolved_now = resolved_now.astimezone(timezone.utc)
    effective = _effective(connection)
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalPhase9ReadinessBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )

    status_rows = effective.execute(
        select(
            QUALIFICATION_DECISIONS_TABLE.c.status, func.count().label("row_count")
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
    decision = (
        effective.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == qualification_handoff.qualification_decision_id
            )
        )
        .mappings()
        .one_or_none()
    )
    verified_handoff: Phase9QualificationHandoff | None = None
    if decision is None:
        reasons.append("EXACT_QUALIFICATION_DECISION_NOT_FOUND")
    elif decision["status"] != "QUALIFIED":
        reasons.append("EXACT_QUALIFICATION_DECISION_NOT_QUALIFIED")
    else:
        persisted = _handoff_from_decision(decision)
        mismatches = [
            field
            for field, expected in asdict(qualification_handoff).items()
            if getattr(persisted, field) != expected
        ]
        if mismatches:
            reasons.extend(
                f"EXACT_QUALIFICATION_HANDOFF_{field.upper()}_MISMATCH"
                for field in mismatches
            )
        else:
            gate = gate_optimization(
                effective, baseline_qualification_decision_id=decision["id"]
            )
            if gate.status != "READY":
                reasons.append(gate.reason_code)
            else:
                verified_handoff = persisted

    lineage_counts = {name: 0 for name in EXECUTION_DOMAIN_TABLE_NAMES}
    if verified_handoff is not None and stage != "QUALIFICATION_HANDOFF":
        lineage_counts = _inspect_lineage(
            effective,
            handoff=verified_handoff,
            stage=stage,
            evaluated_at=resolved_now,
            reasons=reasons,
        )
        active_deployments = int(
            effective.execute(
                select(func.count())
                .select_from(DEPLOYMENTS_TABLE)
                .where(DEPLOYMENTS_TABLE.c.status == "ACTIVE")
            ).scalar_one()
        )
        healthy_runtimes = int(
            effective.execute(
                select(func.count())
                .select_from(RUNTIME_INSTANCES_TABLE)
                .where(RUNTIME_INSTANCES_TABLE.c.status == "HEALTHY")
            ).scalar_one()
        )
        if active_deployments != lineage_counts["deployments"]:
            reasons.append("EXACT_ACTIVE_DEPLOYMENT_NOT_GLOBALLY_UNIQUE")
        if healthy_runtimes != lineage_counts["runtime_instances"]:
            reasons.append("EXACT_HEALTHY_RUNTIME_NOT_GLOBALLY_UNIQUE")
    for table_name in sorted(_ZERO_REQUIRED_BY_STAGE[stage]):
        count = execution_counts[table_name]
        if count:
            reasons.append(f"NONZERO_{table_name.upper()}={count}")

    if stage in {"OKX_DEMO_CANARY", "RECOVERY_SOAK"}:
        if execution_counts["orders"] != 1 or lineage_counts["orders"] != 1:
            reasons.append("EXACT_SINGLE_CANARY_ORDER_COUNT_REQUIRED")
        for table_name in ("fills", "ledger_entries", "reconciliation_items"):
            if execution_counts[table_name] != lineage_counts[table_name]:
                reasons.append(f"UNRELATED_{table_name.upper()}_EVIDENCE_PRESENT")
    if stage == "RECOVERY_SOAK":
        recovery_events = (
            effective.execute(
                select(AUDIT_EVENTS_TABLE).where(
                    AUDIT_EVENTS_TABLE.c.aggregate_type == "canonical_phase9_recovery",
                    AUDIT_EVENTS_TABLE.c.aggregate_id
                    == str(qualification_handoff.qualification_decision_id),
                    AUDIT_EVENTS_TABLE.c.event_type == "PHASE9_RECOVERY_SOAK_ACCEPTED",
                )
            )
            .mappings()
            .all()
        )
        exact_recovery = [
            event
            for event in recovery_events
            if _recovery_acceptance_is_exact(event, handoff=qualification_handoff)
        ]
        lineage_counts["recovery_acceptance_receipts"] = len(exact_recovery)
        if len(recovery_events) != 1 or len(exact_recovery) != 1:
            reasons.append("EXACT_RECOVERY_SOAK_ACCEPTANCE_UNSET")

    unique_reasons = tuple(dict.fromkeys(reasons))
    payload = {
        "contract": "canonical-v13-phase9-readiness-receipt-v2",
        "stage": stage,
        "status": "BLOCKED" if unique_reasons else "READY",
        "reason_codes": unique_reasons,
        "qualification_status_counts": dict(qualification_counts),
        "execution_domain_counts": dict(execution_counts),
        "lineage_evidence_counts": dict(lineage_counts),
        "handoff": (
            {
                key: str(value) if isinstance(value, UUID) else value
                for key, value in asdict(verified_handoff).items()
            }
            if verified_handoff is not None
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
        lineage_evidence_counts=lineage_counts,
        handoff=verified_handoff,
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
