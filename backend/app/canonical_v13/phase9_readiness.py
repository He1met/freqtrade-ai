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
    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE,
    AUDIT_EVENTS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    FILLS_TABLE,
    LEDGER_ENTRIES_TABLE,
    ORDERS_TABLE,
    ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE,
    ORDER_DISPATCH_RECEIPTS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
    RECONCILIATION_ITEMS_TABLE,
    RECONCILIATION_RUNS_TABLE,
    RISK_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    SIGNALS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.phase9_canary_policy import (
    extract_strategy_leverage_cap,
    validate_persisted_canary_probe_receipt,
)
from app.canonical_v13.phase9_execution_authority import (
    shadow_signal_source_accepted,
)
from app.canonical_v13.order_service import CANONICAL_ORDER_WRITER_IDENTITY
from app.canonical_v13.phase9_topology import phase9_topology_digest
from app.canonical_v13.research_evaluation import gate_optimization
from app.canonical_v13.risk_service import (
    INTENT_MODE_EXECUTION,
    INTENT_MODE_SIGNAL_RISK_SHADOW,
)


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
    "acceptance_signal_triggers": ACCEPTANCE_SIGNAL_TRIGGERS_TABLE,
    "deployment_approvals": DEPLOYMENT_APPROVALS_TABLE,
    "deployments": DEPLOYMENTS_TABLE,
    "runtime_instances": RUNTIME_INSTANCES_TABLE,
    "runtime_receipts": RUNTIME_RECEIPTS_TABLE,
    "execution_risk_budget_authorizations": EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    "execution_canary_risk_policies": EXECUTION_CANARY_RISK_POLICIES_TABLE,
    "execution_canary_probe_receipts": EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    "execution_risk_reservations": EXECUTION_RISK_RESERVATIONS_TABLE,
    "execution_attestations": EXECUTION_ATTESTATIONS_TABLE,
    "order_writer_leases": ORDER_WRITER_LEASES_TABLE,
    "signals": SIGNALS_TABLE,
    "trade_intents": TRADE_INTENTS_TABLE,
    "risk_decisions": RISK_DECISIONS_TABLE,
    "orders": ORDERS_TABLE,
    "order_dispatch_outcome_receipts": ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE,
    "order_dispatch_receipts": ORDER_DISPATCH_RECEIPTS_TABLE,
    "fills": FILLS_TABLE,
    "ledger_entries": LEDGER_ENTRIES_TABLE,
    "reconciliation_runs": RECONCILIATION_RUNS_TABLE,
    "reconciliation_items": RECONCILIATION_ITEMS_TABLE,
}
EXECUTION_DOMAIN_TABLE_NAMES: Final[tuple[str, ...]] = tuple(_EXECUTION_TABLES)

_ZERO_REQUIRED_BY_STAGE = {
    # Approval/deployment/runtime lifecycle evidence is append-preserved across
    # qualified lineage rollovers.  A handoff instead proves there is no
    # nonterminal deployment below, while all order-domain evidence stays zero.
    "QUALIFICATION_HANDOFF": frozenset(
        set(EXECUTION_DOMAIN_TABLE_NAMES)
        - {
            "deployment_approvals",
            "deployments",
            "runtime_instances",
            "runtime_receipts",
        }
    ),
    "NO_ORDER_SOAK": frozenset(
        {
            "acceptance_signal_triggers",
            "execution_risk_reservations",
            "execution_risk_budget_authorizations",
            "execution_canary_risk_policies",
            "execution_canary_probe_receipts",
            "execution_attestations",
            "order_writer_leases",
            "signals",
            "trade_intents",
            "risk_decisions",
            "orders",
            "order_dispatch_outcome_receipts",
            "order_dispatch_receipts",
            "fills",
            "ledger_entries",
            "reconciliation_runs",
            "reconciliation_items",
        }
    ),
    "SIGNAL_RISK_SHADOW": frozenset(
        {
            "execution_risk_reservations",
            "execution_risk_budget_authorizations",
            "execution_canary_risk_policies",
            "execution_canary_probe_receipts",
            "execution_attestations",
            "order_writer_leases",
            "orders",
            "order_dispatch_outcome_receipts",
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

_STRICT_CANARY_GLOBAL_TABLES: Final[tuple[str, ...]] = (
    "acceptance_signal_triggers",
    "execution_risk_budget_authorizations",
    "execution_canary_risk_policies",
    "execution_canary_probe_receipts",
    "execution_risk_reservations",
    "execution_attestations",
    "order_writer_leases",
    "signals",
    "trade_intents",
    "risk_decisions",
    "orders",
    "order_dispatch_outcome_receipts",
    "order_dispatch_receipts",
    "fills",
    "ledger_entries",
    "reconciliation_runs",
    "reconciliation_items",
)


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


def _decimal_text(value: object) -> str:
    decimal_value = Decimal(str(value))
    rendered = format(decimal_value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _absence_resource_digest(
    *, resource: str, safe: Mapping[str, object], observed_at: datetime, expires_at: datetime
) -> str:
    return _digest(
        {
            "execution_target": "OKX_DEMO",
            "resource": resource,
            "source": "okx_demo_rest",
            "authenticated": True,
            "observed_at": observed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "facts": {
                "instrument": safe.get("instrument"),
                "client_order_id": safe.get("client_order_id"),
                "matching_order_count": 0,
            },
        }
    )


def _persisted_decimal_equal(
    connection: Connection, left: object, right: object
) -> bool:
    left_decimal = Decimal(str(left))
    right_decimal = Decimal(str(right))
    if connection.dialect.name != "sqlite":
        return left_decimal == right_decimal
    # SQLite has no fixed-point NUMERIC storage and the test adapter round-trips
    # through float. Production PostgreSQL remains exact; this tolerance only
    # neutralizes that fixture adapter artifact before digest recomputation.
    return abs(left_decimal - right_decimal) <= Decimal("1e-12")


def _dispatch_claim_is_exact(
    connection: Connection,
    *,
    claim: Mapping[str, object],
    order: Mapping[str, object],
    risk: Mapping[str, object],
    policy: Mapping[str, object],
    probe: Mapping[str, object],
    attestation: Mapping[str, object],
    lease: Mapping[str, object],
) -> bool:
    guard = claim["guard_json"]
    if not isinstance(guard, Mapping):
        return False
    try:
        claimed_at = _persisted_utc(claim["claimed_at"])
        resource_times = {
            name: (
                _persisted_utc(claim[f"{name}_observed_at"]),
                _persisted_utc(claim[f"{name}_expires_at"]),
            )
            for name in ("positions", "pending_orders", "maximum_order_quantity")
        }
        resource_times["leverage"] = (
            _persisted_utc(claim["guard_leverage_observed_at"]),
            _persisted_utc(claim["guard_leverage_expires_at"]),
        )
        observed_at = _persisted_utc(claim["guard_observed_at"])
        expires_at = _persisted_utc(claim["guard_expires_at"])
        decimal_names = (
            "current_short_leverage",
            "limit_price",
            "effective_leverage",
            "minimum_size",
            "maximum_buy_contracts",
            "long_contracts",
            "short_contracts",
        )
        if not all(isinstance(guard[name], str) for name in decimal_names):
            return False
        current_short = guard["current_short_leverage"]
        limit_price = guard["limit_price"]
        effective_leverage = guard["effective_leverage"]
        minimum_size = guard["minimum_size"]
        maximum_buy = guard["maximum_buy_contracts"]
        long_contracts = guard["long_contracts"]
        short_contracts = guard["short_contracts"]
        if not all(Decimal(guard[name]).is_finite() for name in decimal_names):
            return False
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False
    if (
        observed_at != max(value[0] for value in resource_times.values())
        or expires_at > min(value[1] for value in resource_times.values())
        or any(
            not (start <= claimed_at < end) for start, end in resource_times.values()
        )
        or not (observed_at <= claimed_at < expires_at)
    ):
        return False
    # C/D are historical acceptance checks. Attempt 1 freezes the short-lived
    # authority while it is fresh. A bounded attempt 2 derives authority from
    # that exact first claim plus its immutable negative outcome and therefore
    # needs a fresh guard/lease, not a renewed policy or attestation.
    try:
        probe_resource_windows = [
            (
                _persisted_utc(probe[f"{prefix}_observed_at"]),
                _persisted_utc(probe[f"{prefix}_expires_at"]),
            )
            for prefix in (
                "instrument",
                "mark_price",
                "account_config",
                "leverage",
                "exchange_max_leverage",
                "positions",
                "pending_orders",
                "maximum_order_quantity",
            )
        ]
        probe_observed_at = _persisted_utc(probe["observed_at"])
        probe_expires_at = _persisted_utc(probe["expires_at"])
        attestation_observed_at = _persisted_utc(attestation["observed_at"])
        attestation_expires_at = _persisted_utc(attestation["expires_at"])
        policy_accepted_at = _persisted_utc(policy["accepted_at"])
        policy_expires_at = _persisted_utc(policy["expires_at"])
        lease_acquired_at = _persisted_utc(claim["lease_acquired_at"])
        lease_expires_at = _persisted_utc(claim["lease_expires_at"])
    except (CanonicalExecutionChainBlocked, KeyError, TypeError, ValueError):
        return False
    attempt_ordinal = claim.get("attempt_ordinal")
    if attempt_ordinal not in {1, 2}:
        return False
    original_authority_fresh = (
        all(
            observed <= claimed_at < expires
            for observed, expires in probe_resource_windows
        )
        and probe_observed_at <= claimed_at < probe_expires_at
        and attestation_observed_at <= claimed_at < attestation_expires_at
        and policy_accepted_at <= claimed_at < policy_expires_at
    )
    if (
        (attempt_ordinal == 1 and not original_authority_fresh)
        or not (lease_acquired_at <= claimed_at < lease_expires_at)
    ):
        return False
    resource_facts = {
        "positions": {
            "instrument": policy["instrument"],
            "margin_mode": "isolated",
            "long_contracts": "0",
            "short_contracts": "0",
            "active_position_count": 0,
        },
        "pending_orders": {
            "instrument": policy["instrument"],
            "pending_order_count": 0,
        },
        "maximum_order_quantity": {
            "instrument": policy["instrument"],
            "margin_mode": "isolated",
            "limit_price": limit_price,
            "effective_leverage": effective_leverage,
            "maximum_buy_contracts": maximum_buy,
        },
        "leverage": {
            "instrument": policy["instrument"],
            "account_fingerprint_digest": claim["account_fingerprint_digest"],
            "long": effective_leverage,
            "short": current_short,
        },
    }
    digest_fields = {
        "positions": "positions_digest",
        "pending_orders": "pending_orders_digest",
        "maximum_order_quantity": "maximum_order_quantity_digest",
        "leverage": "guard_leverage_digest",
    }
    for resource, facts in resource_facts.items():
        start, end = resource_times[resource]
        expected = _digest(
            {
                "execution_target": "OKX_DEMO",
                "resource": resource,
                "source": "okx_demo_rest",
                "authenticated": True,
                "observed_at": start.isoformat(),
                "expires_at": end.isoformat(),
                "facts": facts,
            }
        )
        if claim[digest_fields[resource]] != expected:
            return False
    expected_guard = {
        "contract": "canonical-v13-okx-demo-dispatch-guard-v1",
        "execution_target": "OKX_DEMO",
        "instrument": policy["instrument"],
        "account_fingerprint_digest": claim["account_fingerprint_digest"],
        "credential_generation_digest": claim["credential_generation_digest"],
        "limit_price": limit_price,
        "effective_leverage": effective_leverage,
        "current_short_leverage": current_short,
        "minimum_size": minimum_size,
        "maximum_buy_contracts": maximum_buy,
        "long_contracts": long_contracts,
        "short_contracts": short_contracts,
        "active_position_count": claim["active_position_count"],
        "pending_order_count": claim["pending_order_count"],
        "observed_at": observed_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "positions_digest": claim["positions_digest"],
        "positions_observed_at": resource_times["positions"][0].isoformat(),
        "positions_expires_at": resource_times["positions"][1].isoformat(),
        "pending_orders_digest": claim["pending_orders_digest"],
        "pending_orders_observed_at": resource_times["pending_orders"][0].isoformat(),
        "pending_orders_expires_at": resource_times["pending_orders"][1].isoformat(),
        "maximum_order_quantity_digest": claim["maximum_order_quantity_digest"],
        "maximum_order_quantity_observed_at": resource_times["maximum_order_quantity"][
            0
        ].isoformat(),
        "maximum_order_quantity_expires_at": resource_times["maximum_order_quantity"][
            1
        ].isoformat(),
        "leverage_digest": claim["guard_leverage_digest"],
        "leverage_observed_at": resource_times["leverage"][0].isoformat(),
        "leverage_expires_at": resource_times["leverage"][1].isoformat(),
    }
    claim_payload = {
        "contract": "canonical-v13-okx-demo-order-dispatch-claim-v1",
        "order_id": str(order["id"]),
        "attempt_ordinal": claim["attempt_ordinal"],
        "request_digest": claim["request_digest"],
        "holder_identity": claim["holder_identity"],
        "holder_token_digest": claim["holder_token_digest"],
        "lease_generation": claim["lease_generation"],
        "lease_digest": claim["lease_digest"],
        "lease_acquired_at": lease_acquired_at.isoformat(),
        "lease_expires_at": lease_expires_at.isoformat(),
        "risk_decision_id": str(risk["id"]),
        "canary_risk_policy_id": str(policy["id"]),
        "probe_receipt_id": str(probe["id"]),
        "execution_attestation_id": str(attestation["id"]),
        "guard_digest": claim["guard_digest"],
        "claimed_at": claimed_at.isoformat(),
    }
    return (
        dict(guard) == expected_guard
        and claim["guard_digest"] == _digest(expected_guard)
        and claim["claim_digest"] == _digest(claim_payload)
        and claim["order_id"] == order["id"]
        and claim["risk_decision_id"] == risk["id"]
        and claim["canary_risk_policy_id"] == policy["id"]
        and claim["probe_receipt_id"] == probe["id"]
        and claim["execution_attestation_id"] == attestation["id"]
        and claim["request_digest"] == order["request_digest"]
        and claim["holder_identity"] == "canonical-v13-order-writer-v1"
        and claim["lease_digest"]
        == _digest(
            {
                "execution_target": "OKX_DEMO",
                "holder_identity": claim["holder_identity"],
                "holder_token_digest": claim["holder_token_digest"],
                "generation": claim["lease_generation"],
                "expires_at": lease_expires_at.isoformat(),
            }
        )
        and claim["account_fingerprint_digest"]
        == attestation["account_fingerprint_digest"]
        == probe["account_fingerprint_digest"]
        and claim["credential_generation_digest"]
        == attestation["credential_generation_digest"]
        == probe["credential_generation_digest"]
        and _persisted_decimal_equal(
            connection, claim["limit_price"], policy["limit_price"]
        )
        and _persisted_decimal_equal(
            connection, claim["effective_leverage"], policy["effective_leverage"]
        )
        and _persisted_decimal_equal(
            connection, claim["minimum_size"], policy["minimum_contract_size"]
        )
        and Decimal(str(claim["maximum_buy_contracts"]))
        >= Decimal(str(claim["minimum_size"]))
        and Decimal(str(claim["long_contracts"])) == 0
        and Decimal(str(claim["short_contracts"])) == 0
        and claim["active_position_count"] == 0
        and claim["pending_order_count"] == 0
    )


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
    if not approvals:
        reasons.append("EXACT_APPROVED_DEPLOYMENT_APPROVAL_EVIDENCE_UNSET")
        return counts
    deployments = (
        connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.deployment_approval_id.in_(
                    [row["id"] for row in approvals]
                )
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
    current_approvals = [
        row for row in approvals if row["id"] == deployment["deployment_approval_id"]
    ]
    counts["deployment_approvals"] = len(current_approvals)
    if len(current_approvals) != 1:
        reasons.append("EXACT_APPROVED_DEPLOYMENT_APPROVAL_EVIDENCE_UNSET")
        return counts
    approval = current_approvals[0]

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

    triggers = (
        connection.execute(
            select(ACCEPTANCE_SIGNAL_TRIGGERS_TABLE).where(
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.deployment_id == deployment["id"],
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.qualification_decision_id
                == handoff.qualification_decision_id,
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.deployment_approval_id
                == approval["id"],
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.runtime_instance_id == runtime["id"],
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.strategy_version_id
                == handoff.strategy_version_id,
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.research_target_id
                == handoff.research_target_id,
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.configuration_bundle_id
                == handoff.configuration_bundle_id,
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.configuration_bundle_digest
                == handoff.configuration_bundle_digest,
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.market_snapshot_id
                == handoff.market_snapshot_id,
                ACCEPTANCE_SIGNAL_TRIGGERS_TABLE.c.market_snapshot_digest
                == handoff.market_snapshot_digest,
            )
        )
        .mappings()
        .all()
    )
    counts["acceptance_signal_triggers"] = len(triggers)

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
    signals = [
        row
        for row in signals
        if (
            row["source_kind"] == "NATURAL_STRATEGY_SIGNAL"
            and row["acceptance_trigger_id"] is None
            and row["signal_json"].get("natural_signal") is True
        )
        or (
            row["source_kind"] == "ACCEPTANCE_SCHEDULED_TEST"
            and row["acceptance_trigger_id"] is not None
            and row["signal_json"].get("source_kind")
            == "ACCEPTANCE_SCHEDULED_TEST"
            and row["signal_json"].get("natural_signal") is False
            and row["signal_json"].get("acceptance_only") is True
            and isinstance(row["worker_receipt_digest"], str)
            and isinstance(row["worker_signer_key_id"], str)
            and row["worker_signature_algorithm"] == "HMAC_SHA256_V1"
            and isinstance(row["worker_signature"], str)
        )
    ]
    counts["signals"] = len(signals)
    natural_signals = [
        row for row in signals if row["source_kind"] == "NATURAL_STRATEGY_SIGNAL"
    ]
    acceptance_signals = [
        row for row in signals if row["source_kind"] == "ACCEPTANCE_SCHEDULED_TEST"
    ]
    selected_shadow_signals = acceptance_signals if triggers else natural_signals
    signal_ids = [row["id"] for row in selected_shadow_signals]
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
    shadow_intents = [
        row
        for row in intents
        if row["intent_mode"] == INTENT_MODE_SIGNAL_RISK_SHADOW
    ]
    execution_intents = [
        row for row in intents if row["intent_mode"] == INTENT_MODE_EXECUTION
    ]
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
    if stage == "SIGNAL_RISK_SHADOW":
        target = (
            connection.execute(
                select(RESEARCH_TARGETS_TABLE).where(
                    RESEARCH_TARGETS_TABLE.c.id == handoff.research_target_id
                )
            )
            .mappings()
            .one_or_none()
        )
        intent_by_id = {row["id"]: row for row in shadow_intents}
        signal_by_id = {row["id"]: row for row in selected_shadow_signals}
        exact_shadow = []
        shadow_risks = [
            row
            for row in risks
            if row["decision_mode"] == INTENT_MODE_SIGNAL_RISK_SHADOW
        ]
        for row in shadow_risks:
            intent = intent_by_id.get(row["trade_intent_id"])
            signal = signal_by_id.get(intent["signal_id"]) if intent else None
            payload = row["decision_json"]
            body = intent["intent_json"].get("exchange_body") if intent else None
            if not isinstance(payload, Mapping) or not isinstance(body, Mapping):
                continue
            baseline_body = dict(body)
            counterfactual_body = {
                **baseline_body,
                "side": "sell",
                "posSide": "short",
            }
            expected_checks = [
                {
                    "check_id": "EXACT_LONG_ONLY_BASELINE",
                    "input_digest": _digest(baseline_body),
                    "outcome": "ACCEPTED",
                    "reason_code": "SHADOW_EXACT_TARGET_LONG_ONLY_ACCEPTED",
                    "order_submission_enabled": False,
                    "execution_authorized": False,
                },
                {
                    "check_id": "LONG_ONLY_REJECTED_COUNTERFACTUAL",
                    "input_digest": _digest(counterfactual_body),
                    "outcome": "REJECTED",
                    "reason_code": "SHADOW_SHORT_SELL_COUNTERFACTUAL_REJECTED",
                    "order_submission_enabled": False,
                    "execution_authorized": False,
                },
            ]
            if (
                target is not None
                and signal is not None
                and _digest(dict(signal["signal_json"])) == signal["signal_digest"]
                and shadow_signal_source_accepted(signal)
                and signal["signal_json"].get("allow_real_funds") is False
                and _digest(dict(intent["intent_json"])) == intent["intent_digest"]
                and intent["intent_json"].get("execution_target") == "OKX_DEMO"
                and intent["intent_json"].get("allow_real_funds") is False
                and intent["intent_json"].get("instrument") == target["instrument"]
                and body.get("instId") == target["instrument"]
                and body.get("tdMode") == "isolated"
                and body.get("side") == "buy"
                and body.get("posSide") == "long"
                and row["status"] == "RISK_ACCEPTED"
                and payload.get("contract")
                == "canonical-v13-signal-risk-shadow-decision-v1"
                and payload.get("decision_mode") == "SIGNAL_RISK_SHADOW"
                and payload.get("trade_intent_id") == str(intent["id"])
                and payload.get("intent_digest") == intent["intent_digest"]
                and payload.get("research_target_id") == str(target["id"])
                and payload.get("research_target_digest") == target["target_digest"]
                and payload.get("checks") == expected_checks
                and payload.get("status") == "RISK_ACCEPTED"
                and payload.get("reason_code")
                == "SHADOW_BASELINE_AND_COUNTERFACTUAL_VERIFIED"
                and payload.get("order_submission_enabled") is False
                and payload.get("execution_authorized") is False
                and payload.get("risk_budget_authorization_id") is None
                and payload.get("reservation_id") is None
                and payload.get("allow_real_funds") is False
                and _digest(dict(payload)) == row["decision_digest"]
            ):
                exact_shadow.append(row)
        if len(selected_shadow_signals) != 1:
            reasons.append("EXACT_SINGLE_SHADOW_SIGNAL_REQUIRED")
        if triggers:
            if (
                len(triggers) != 1
                or len(acceptance_signals) != 1
                or acceptance_signals[0]["acceptance_trigger_id"]
                != triggers[0]["id"]
                or acceptance_signals[0]["signal_json"].get(
                    "acceptance_trigger_receipt_digest"
                )
                != triggers[0]["receipt_digest"]
            ):
                reasons.append("EXACT_ACCEPTANCE_TRIGGER_SIGNAL_LINEAGE_REQUIRED")
        if len(shadow_intents) != 1:
            reasons.append("EXACT_SINGLE_SHADOW_INTENT_REQUIRED")
        if len(shadow_risks) != 1 or len(exact_shadow) != 1:
            reasons.append("EXACT_SINGLE_SHADOW_DECISION_RECEIPT_REQUIRED")
        return counts

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
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.status != "EXPIRED",
            )
        )
        .mappings()
        .all()
    )
    counts["execution_canary_risk_policies"] = len(risk_policy_sources)
    execution_intent_ids = [row["id"] for row in execution_intents]
    reservations = (
        connection.execute(
            select(EXECUTION_RISK_RESERVATIONS_TABLE).where(
                EXECUTION_RISK_RESERVATIONS_TABLE.c.trade_intent_id.in_(
                    execution_intent_ids
                )
            )
        )
        .mappings()
        .all()
        if execution_intent_ids
        else []
    )
    counts["execution_risk_reservations"] = len(reservations)
    if not signals:
        reasons.append("EXACT_SIGNAL_EVIDENCE_UNSET")
    if len(execution_intents) != 1:
        reasons.append("EXACT_SINGLE_EXECUTION_INTENT_REQUIRED")
    execution_risks = [
        row
        for row in risks
        if row["decision_mode"] == INTENT_MODE_EXECUTION
        and row["trade_intent_id"] in execution_intent_ids
        and isinstance(row["decision_json"], Mapping)
        and row["decision_json"].get("decision_mode") == "EXECUTION"
        and row["decision_json"].get("execution_authorized")
        == (row["status"] == "RISK_ACCEPTED")
        and row["decision_json"].get("order_submission_enabled")
        == (row["status"] == "RISK_ACCEPTED")
    ]
    if any(
        row["intent_json"].get("intent_mode") != INTENT_MODE_EXECUTION
        for row in execution_intents
    ):
        reasons.append("EXACT_EXECUTION_INTENT_MODE_LINEAGE_DRIFT")
    if not any(row["status"] == "RISK_ACCEPTED" for row in execution_risks):
        reasons.append("EXACT_RISK_ACCEPTED_EVIDENCE_UNSET")
    if len([row for row in execution_risks if row["status"] == "RISK_ACCEPTED"]) != 1:
        reasons.append("EXACT_SINGLE_EXECUTION_RISK_ACCEPTED_REQUIRED")
    if len(budgets) != 1:
        reasons.append("EXACT_RISK_BUDGET_AUTHORIZATION_EVIDENCE_UNSET")
    exact_policy_source = []
    for source in risk_policy_sources:
        artifact = (
            connection.execute(
                select(STRATEGY_ARTIFACTS_TABLE).where(
                    STRATEGY_ARTIFACTS_TABLE.c.id
                    == source["strategy_artifact_id"]
                )
            )
            .mappings()
            .one_or_none()
        )
        artifact_leverage_cap: Decimal | None = None
        if (
            artifact is not None
            and artifact["content_digest"] == source["strategy_artifact_digest"]
            and sha256(artifact["normalized_content"].encode("utf-8")).hexdigest()
            == artifact["content_digest"]
        ):
            try:
                artifact_leverage_cap = extract_strategy_leverage_cap(
                    artifact["normalized_content"]
                )
            except CanonicalExecutionChainBlocked:
                pass
        probe_rows = (
            connection.execute(
                select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
                    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id
                    == source["probe_receipt_id"]
                )
            )
            .mappings()
            .all()
        )
        counts["execution_canary_probe_receipts"] += len(probe_rows)
        probe_is_exact = False
        probe_validated = False
        if len(probe_rows) == 1:
            probe = probe_rows[0]
            try:
                validate_persisted_canary_probe_receipt(
                    connection,
                    probe_receipt_id=probe["id"],
                    evaluated_at=_persisted_utc(source["accepted_at"]),
                )
            except CanonicalExecutionChainBlocked:
                pass
            else:
                probe_validated = True
                probe_is_exact = (
                    probe["deployment_id"] == deployment["id"]
                    and probe["execution_attestation_id"]
                    == source["execution_attestation_id"]
                    and probe["instrument"] == source["instrument"]
                    and probe["instrument_digest"] == source["metadata_receipt_digest"]
                    and probe["mark_price_digest"]
                    == source["mark_price_receipt_digest"]
                    and _persisted_decimal_equal(
                        connection,
                        probe["minimum_size"],
                        source["minimum_contract_size"],
                    )
                    and _persisted_decimal_equal(
                        connection, probe["contract_value"], source["contract_value"]
                    )
                    and probe["contract_value_ccy"] == source["contract_value_ccy"]
                    and _persisted_decimal_equal(
                        connection, probe["mark_price"], source["mark_price"]
                    )
                    and _persisted_decimal_equal(
                        connection, probe["limit_price"], source["limit_price"]
                    )
                    and _persisted_decimal_equal(
                        connection,
                        probe["maximum_buy_contracts"],
                        source["maximum_buy_contracts"],
                    )
                    and Decimal(str(probe["long_contracts"])) == 0
                    and Decimal(str(probe["short_contracts"])) == 0
                    and probe["active_position_count"] == 0
                    and probe["pending_order_count"] == 0
                    and _persisted_decimal_equal(
                        connection,
                        probe["exchange_max_leverage"],
                        source["exchange_max_leverage"],
                    )
                )
        if len(probe_rows) != 1:
            reasons.append("CANONICAL_RISK_POLICY_PROBE_RECEIPT_DRIFT")
            reasons.append("CANONICAL_RISK_POLICY_PROBE_VALIDATION_BLOCKED")
            continue
        request_payload = {
            "contract": "canonical-v13-canary-risk-policy-request-v1",
            "qualification_decision_id": str(source["qualification_decision_id"]),
            "deployment_approval_id": str(source["deployment_approval_id"]),
            "probe_receipt_id": str(source["probe_receipt_id"]),
            "actor_identity": source["actor_identity"],
            "idempotency_key": source["idempotency_key"],
            "reason": source["reason"],
        }
        request_is_exact = _digest(request_payload) == source["request_digest"]
        policy_payload = {
            "contract": "canonical-v13-canary-risk-policy-v1",
            "request_digest": source["request_digest"],
            "qualification_decision_id": str(source["qualification_decision_id"]),
            "qualification_decision_digest": handoff.qualification_decision_digest,
            "probe_receipt_id": str(source["probe_receipt_id"]),
            "probe_receipt_digest": (
                probe["receipt_digest"] if len(probe_rows) == 1 else ""
            ),
            "strategy_version_id": str(source["strategy_version_id"]),
            "strategy_artifact_id": str(source["strategy_artifact_id"]),
            "strategy_artifact_digest": source["strategy_artifact_digest"],
            "research_target_id": str(source["research_target_id"]),
            "research_target_digest": source["research_target_digest"],
            "configuration_bundle_id": str(source["configuration_bundle_id"]),
            "configuration_bundle_digest": source["configuration_bundle_digest"],
            "market_snapshot_id": str(source["market_snapshot_id"]),
            "market_snapshot_digest": source["market_snapshot_digest"],
            "execution_target": source["execution_target"],
            "instrument": source["instrument"],
            "position_policy": source["position_policy"],
            "max_order_count": source["max_order_count"],
            "minimum_contract_size": _decimal_text(probe["minimum_size"]),
            "contract_value": _decimal_text(probe["contract_value"]),
            "contract_value_ccy": source["contract_value_ccy"],
            "mark_price": _decimal_text(probe["mark_price"]),
            "limit_price": _decimal_text(probe["limit_price"]),
            "maximum_buy_contracts": _decimal_text(probe["maximum_buy_contracts"]),
            "max_notional": _decimal_text(
                Decimal(probe["minimum_size"])
                * Decimal(probe["contract_value"])
                * Decimal(probe["mark_price"])
            ),
            "strategy_max_leverage": (
                _decimal_text(artifact_leverage_cap)
                if artifact_leverage_cap is not None
                else ""
            ),
            "exchange_max_leverage": _decimal_text(probe["exchange_max_leverage"]),
            "effective_leverage": _decimal_text(probe["current_long_leverage"]),
            "metadata_receipt_digest": source["metadata_receipt_digest"],
            "mark_price_receipt_digest": source["mark_price_receipt_digest"],
            "attestation_digest": source["attestation_digest"],
            "allow_real_funds": False,
        }
        policy_is_exact = _digest(policy_payload) == source["policy_digest"]
        receipt_payload = {
            "contract": "canonical-v13-canary-risk-policy-receipt-v1",
            "policy_id": str(source["id"]),
            "request_digest": source["request_digest"],
            "policy_digest": source["policy_digest"],
            "accepted_at": _persisted_utc(source["accepted_at"]).isoformat(),
            "expires_at": _persisted_utc(source["expires_at"]).isoformat(),
            # This is the immutable issuance receipt.  Termination changes the
            # row state but cannot rewrite the originally accepted receipt.
            "status": "ACTIVE",
        }
        receipt_is_exact = _digest(receipt_payload) == source["receipt_digest"]
        if not probe_is_exact:
            reasons.append("CANONICAL_RISK_POLICY_PROBE_RECEIPT_DRIFT")
        if not probe_validated:
            reasons.append("CANONICAL_RISK_POLICY_PROBE_VALIDATION_BLOCKED")
        if not request_is_exact:
            reasons.append("CANONICAL_RISK_POLICY_REQUEST_DIGEST_DRIFT")
        if not policy_is_exact:
            reasons.append("CANONICAL_RISK_POLICY_DIGEST_DRIFT")
        if not receipt_is_exact:
            reasons.append("CANONICAL_RISK_POLICY_RECEIPT_DIGEST_DRIFT")
        exact_linked_budgets = []
        for budget in budgets:
            authorization_payload = {
                "contract": "canonical-v13-demo-risk-budget-authorization-v1",
                "execution_canary_risk_policy_id": str(source["id"]),
                "deployment_approval_id": str(approval["id"]),
                "approval_digest": approval["approval_digest"],
                "qualification_decision_id": str(handoff.qualification_decision_id),
                "qualification_decision_digest": handoff.qualification_decision_digest,
                "execution_target": "OKX_DEMO",
                "instrument": source["instrument"],
                "max_notional": str(Decimal(str(source["max_notional"]))),
                "max_order_count": source["max_order_count"],
                "position_policy": source["position_policy"],
                "strategy_max_leverage": str(source["strategy_max_leverage"]),
                "effective_leverage": str(source["effective_leverage"]),
                "actor_identity": budget["actor_identity"],
                "reason": budget["reason"],
                "policy_digest": source["policy_digest"],
                "source_receipt_digest": source["receipt_digest"],
                "expires_at": _persisted_utc(source["expires_at"]).isoformat(),
                "allow_real_funds": False,
            }
            if (
                budget["execution_canary_risk_policy_id"] == source["id"]
                and budget["deployment_approval_id"] == approval["id"]
                and budget["execution_target"] == "OKX_DEMO"
                and budget["instrument"] == source["instrument"]
                and budget["max_order_count"] == 1
                and budget["policy_digest"] == source["policy_digest"]
                and budget["source_receipt_digest"] == source["receipt_digest"]
                and _persisted_utc(budget["expires_at"])
                == _persisted_utc(source["expires_at"])
                and _digest(authorization_payload) == budget["authorization_digest"]
            ):
                exact_linked_budgets.append(budget)
        if (
            probe_is_exact
            and request_is_exact
            and policy_is_exact
            and receipt_is_exact
            and source["qualification_decision_id"] == handoff.qualification_decision_id
            and source["deployment_approval_id"] == approval["id"]
            and source["strategy_version_id"] == handoff.strategy_version_id
            and source["research_target_id"] == handoff.research_target_id
            and source["configuration_bundle_id"] == handoff.configuration_bundle_id
            and source["configuration_bundle_digest"]
            == handoff.configuration_bundle_digest
            and source["market_snapshot_id"] == handoff.market_snapshot_id
            and source["market_snapshot_digest"] == handoff.market_snapshot_digest
            and source["execution_target"] == "OKX_DEMO"
            and source["allow_real_funds"] is False
            and (
                (
                    stage == "OKX_DEMO_CANARY"
                    and source["status"] in {"ACTIVE", "TERMINATED"}
                )
                or (stage == "RECOVERY_SOAK" and source["status"] == "TERMINATED")
            )
            and (
                source["terminated_at"] is None and source["termination_digest"] is None
                if source["status"] == "ACTIVE"
                else source["terminated_at"] is not None
                and source["termination_digest"] is not None
            )
            and source["position_policy"] == "LONG_ONLY"
            and source["max_order_count"] == 1
            and artifact_leverage_cap is not None
            and Decimal(str(source["strategy_max_leverage"]))
            == artifact_leverage_cap
            and _persisted_decimal_equal(
                connection,
                source["effective_leverage"],
                Decimal(str(probe["current_long_leverage"])),
            )
            and Decimal(str(source["effective_leverage"]))
            <= min(
                Decimal(str(source["strategy_max_leverage"])),
                Decimal(str(source["exchange_max_leverage"])),
            )
            and _persisted_decimal_equal(
                connection, source["limit_price"], probe["limit_price"]
            )
            and _persisted_decimal_equal(
                connection,
                source["maximum_buy_contracts"],
                probe["maximum_buy_contracts"],
            )
            and Decimal(str(source["maximum_buy_contracts"]))
            >= Decimal(str(source["minimum_contract_size"]))
            and _persisted_decimal_equal(
                connection,
                source["max_notional"],
                Decimal(str(source["minimum_contract_size"]))
                * Decimal(str(source["contract_value"]))
                * Decimal(str(source["mark_price"])),
            )
            and _persisted_utc(source["expires_at"])
            - _persisted_utc(source["accepted_at"])
            == timedelta(minutes=30)
            and len(exact_linked_budgets) == 1
        ):
            exact_policy_source.append(source)
    if not risk_policy_sources:
        reasons.append("CANONICAL_RISK_POLICY_LINEAGE_UNSET")
    elif len(exact_policy_source) != 1:
        reasons.append("CANONICAL_RISK_POLICY_LINEAGE_UNSET")
    accepted_reservation_digests = {
        row["reservation_digest"]
        for row in reservations
        if row["status"] == "RISK_ACCEPTED"
    }
    if (
        len(reservations) != 1
        or len(accepted_reservation_digests) != 1
        or len(execution_risks) != 1
    ):
        reasons.append("EXACT_SINGLE_EXECUTION_RISK_RESERVATION_REQUIRED")
    if any(
        row in execution_risks
        and row["status"] == "RISK_ACCEPTED"
        and row["decision_json"].get("reservation_digest")
        not in accepted_reservation_digests
        for row in risks
    ):
        reasons.append("EXACT_RISK_BUDGET_RESERVATION_EVIDENCE_UNSET")
    linked_attestation_ids = [
        source["execution_attestation_id"] for source in risk_policy_sources
    ]
    attestations = (
        connection.execute(
            select(EXECUTION_ATTESTATIONS_TABLE).where(
                EXECUTION_ATTESTATIONS_TABLE.c.deployment_id == deployment["id"],
                EXECUTION_ATTESTATIONS_TABLE.c.id.in_(linked_attestation_ids),
            )
        )
        .mappings()
        .all()
    )
    exact_attestations = []
    for row in attestations:
        if (
            row["status"] == "READY"
            and row["execution_target"] == "OKX_DEMO"
            and row["permissions_json"]
            == {"read": True, "trade": True, "withdraw": False}
            and len(exact_policy_source) == 1
            and row["id"] == exact_policy_source[0]["execution_attestation_id"]
            and row["attestation_digest"]
            == exact_policy_source[0]["attestation_digest"]
        ):
            exact_attestations.append(row)
    counts["execution_attestations"] = len(attestations)
    if len(attestations) != 1 or len(exact_attestations) != 1:
        reasons.append("EXACT_OKX_DEMO_ATTESTATION_EVIDENCE_UNSET")
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
    counts["order_writer_leases"] = len(leases)
    if len(leases) != 1 or len(exact_leases) != 1:
        reasons.append("EXACT_SINGLE_ORDER_WRITER_LEASE_EVIDENCE_UNSET")

    accepted_ids = [
        row["id"] for row in execution_risks if row["status"] == "RISK_ACCEPTED"
    ]
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
    accepted_risk = next(
        row for row in execution_risks if row["id"] == order["risk_decision_id"]
    )
    claim_policy = exact_policy_source[0] if len(exact_policy_source) == 1 else None
    claim_probe = (
        connection.execute(
            select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
                EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id
                == claim_policy["probe_receipt_id"]
            )
        )
        .mappings()
        .one_or_none()
        if claim_policy is not None
        else None
    )
    claim_attestation = (
        connection.execute(
            select(EXECUTION_ATTESTATIONS_TABLE).where(
                EXECUTION_ATTESTATIONS_TABLE.c.id
                == claim_policy["execution_attestation_id"]
            )
        )
        .mappings()
        .one_or_none()
        if claim_policy is not None
        else None
    )
    claims = (
        connection.execute(
            select(ORDER_DISPATCH_RECEIPTS_TABLE).where(
                ORDER_DISPATCH_RECEIPTS_TABLE.c.order_id == order["id"]
            )
        )
        .mappings()
        .all()
    )
    counts["order_dispatch_receipts"] = len(claims)
    exact_claims = []
    for claim in claims:
        if (
            claim["attempt_ordinal"] in {1, 2}
            and len(exact_leases) == 1
            and claim_policy is not None
            and claim_probe is not None
            and claim_attestation is not None
            and _dispatch_claim_is_exact(
                connection,
                claim=claim,
                order=order,
                risk=accepted_risk,
                policy=claim_policy,
                probe=claim_probe,
                attestation=claim_attestation,
                lease=exact_leases[0],
            )
        ):
            exact_claims.append(claim)
    if (
        len(claims) not in {1, 2}
        or len(exact_claims) != len(claims)
        or sorted(row["attempt_ordinal"] for row in exact_claims)
        != list(range(1, len(exact_claims) + 1))
    ):
        reasons.append("EXACT_SINGLE_ORDER_POST_RECEIPT_UNPROVEN")
    order_intent = next(
        (row for row in intents if row["id"] == accepted_risk["trade_intent_id"]),
        None,
    )
    exchange_body = (
        order_intent["intent_json"].get("exchange_body")
        if order_intent is not None and isinstance(order_intent["intent_json"], Mapping)
        else None
    )
    if not isinstance(exchange_body, Mapping):
        exchange_body = {}
    outcomes = (
        connection.execute(
            select(ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE).where(
                ORDER_DISPATCH_OUTCOME_RECEIPTS_TABLE.c.order_id == order["id"]
            )
        )
        .mappings()
        .all()
    )
    counts["order_dispatch_outcome_receipts"] = len(outcomes)
    claims_by_id = {row["id"]: row for row in exact_claims}
    accepted_outcomes = []
    negative_outcomes = []
    for outcome in outcomes:
        claim = claims_by_id.get(outcome["dispatch_claim_id"])
        safe_response = outcome["safe_response_json"]
        if claim is None or not isinstance(safe_response, Mapping):
            continue
        safe_response_digest = _digest(dict(safe_response))
        outcome_payload = {
            "contract": "canonical-v13-order-dispatch-outcome-v1",
            "outcome_id": str(outcome["id"]),
            "order_id": str(order["id"]),
            "dispatch_claim_id": str(claim["id"]),
            "claim_digest": claim["claim_digest"],
            "client_order_id": outcome["client_order_id"],
            "exchange_order_id": outcome["exchange_order_id"],
            "safe_response_digest": safe_response_digest,
            "outcome_mode": outcome["outcome_mode"],
            "recorded_at": _persisted_utc(outcome["recorded_at"]).isoformat(),
        }
        outcome_digest = _digest(outcome_payload)
        common = (
            outcome["order_id"] == order["id"]
            and outcome["claim_digest"] == claim["claim_digest"]
            and outcome["client_order_id"] == exchange_body.get("clOrdId")
            and outcome["safe_response_digest"] == safe_response_digest
            and outcome["receipt_digest"] == outcome_digest
            and _persisted_utc(outcome["recorded_at"])
            >= _persisted_utc(claim["claimed_at"])
        )
        if not common:
            continue
        if outcome["outcome_mode"] in {"POST", "GET_RECOVERY"}:
            order_digest = _digest(
                {
                    "contract": "canonical-v13-okx-demo-order-receipt-v2",
                    "order_id": str(order["id"]),
                    "request_digest": order["request_digest"],
                    "dispatch_claim_digest": claim["claim_digest"],
                    "dispatch_outcome_receipt_digest": outcome_digest,
                }
            )
            if (
                claim["attempt_ordinal"] == len(exact_claims)
                and outcome["exchange_order_id"] == order["exchange_order_id"]
                and safe_response.get("clOrdId") == outcome["client_order_id"]
                and safe_response.get("ordId") == outcome["exchange_order_id"]
                and safe_response.get("sCode") == "0"
                and order["receipt_digest"] == order_digest
            ):
                accepted_outcomes.append(outcome)
        elif outcome["outcome_mode"] == "GET_NOT_FOUND":
            try:
                observed_at = _persisted_utc(
                    datetime.fromisoformat(str(safe_response["observed_at"]))
                )
                expires_at = _persisted_utc(
                    datetime.fromisoformat(str(safe_response["expires_at"]))
                )
            except (KeyError, TypeError, ValueError):
                continue
            if (
                claim["attempt_ordinal"] == 1
                and outcome["exchange_order_id"] is None
                and safe_response.get("contract")
                == "canonical-v13-okx-demo-order-absence-v1"
                and safe_response.get("execution_target") == "OKX_DEMO"
                and safe_response.get("instrument") == exchange_body.get("instId")
                and safe_response.get("client_order_id")
                == exchange_body.get("clOrdId")
                and safe_response.get("account_fingerprint_digest")
                == claim["account_fingerprint_digest"]
                and safe_response.get("credential_generation_digest")
                == claim["credential_generation_digest"]
                and safe_response.get("exact_order_result_code") == "51603"
                and safe_response.get("pending_order_match_count") == 0
                and safe_response.get("history_order_match_count") == 0
                and observed_at < expires_at
                and safe_response.get("pending_orders_digest")
                == _absence_resource_digest(
                    resource="pending_orders",
                    safe=safe_response,
                    observed_at=observed_at,
                    expires_at=expires_at,
                )
                and safe_response.get("orders_history_digest")
                == _absence_resource_digest(
                    resource="orders_history",
                    safe=safe_response,
                    observed_at=observed_at,
                    expires_at=expires_at,
                )
            ):
                negative_outcomes.append(outcome)
    expected_outcome_count = 1 if len(exact_claims) == 1 else 2
    retry_sequence_exact = True
    if len(exact_claims) == 2 and len(negative_outcomes) == 1:
        second_claim = next(
            row for row in exact_claims if row["attempt_ordinal"] == 2
        )
        retry_sequence_exact = _persisted_utc(
            negative_outcomes[0]["recorded_at"]
        ) <= _persisted_utc(second_claim["claimed_at"])
    if (
        len(outcomes) != expected_outcome_count
        or len(accepted_outcomes) != 1
        or len(negative_outcomes) != (1 if len(exact_claims) == 2 else 0)
        or not retry_sequence_exact
    ):
        reasons.append("EXACT_SINGLE_ORDER_POST_OUTCOME_UNPROVEN")

    try:
        requested_size = Decimal(str(exchange_body["sz"]))
    except (KeyError, TypeError, ArithmeticError):
        requested_size = Decimal(0)
    fills = (
        connection.execute(
            select(FILLS_TABLE).where(FILLS_TABLE.c.order_id == order["id"])
        )
        .mappings()
        .all()
    )
    exact_fills = [
        row
        for row in fills
        if isinstance(row["fill_json"], Mapping)
        and row["fill_json"].get("evidence_class") == "PRODUCTION_OKX_DEMO"
        and row["fill_json"].get("allow_real_funds") is False
        and row["fill_json"].get("exchange_order_id") == order["exchange_order_id"]
        and row["fill_json"].get("exchange_fill_id") == row["exchange_fill_id"]
        and row["fill_json"].get("instrument") == exchange_body.get("instId")
        and row["fill_json"].get("side") == exchange_body.get("side") == "buy"
        and row["fill_json"].get("position_side")
        == exchange_body.get("posSide")
        == "long"
        and row["receipt_digest"]
        == _digest(
            {
                "contract": "canonical-v13-okx-demo-fill-v1",
                "order_id": str(order["id"]),
                "order_receipt_digest": order["receipt_digest"],
                "fill": dict(row["fill_json"]),
            }
        )
    ]
    counts["fills"] = len(exact_fills)
    fill_ids = [row["id"] for row in exact_fills]
    try:
        filled_size = sum(
            (Decimal(str(row["fill_json"]["size"])) for row in exact_fills),
            Decimal(0),
        )
    except (KeyError, ArithmeticError):
        filled_size = Decimal(0)
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
    exact_ledgers = []
    for fill in exact_fills:
        matching = [row for row in ledgers if row["fill_id"] == fill["id"]]
        if len(matching) != 1:
            continue
        ledger = matching[0]
        expected_payload = {
            "contract": "canonical-v13-okx-demo-ledger-entry-v1",
            "fill_id": str(fill["id"]),
            "fill_receipt_digest": fill["receipt_digest"],
            "entry_key": ledger["entry_key"],
            "asset": ledger["asset"],
            "amount": _decimal_text(fill["fill_json"]["size"]),
            "entry_type": ledger["entry_type"],
            "evidence_class": "PRODUCTION_OKX_DEMO",
            "allow_real_funds": False,
        }
        if (
            ledger["entry_key"]
            == f"okx-demo-fill:{fill['exchange_fill_id']}:long-contracts"
            and ledger["asset"] == fill["fill_json"]["instrument"]
            and Decimal(str(ledger["amount"]))
            == Decimal(str(fill["fill_json"]["size"]))
            and ledger["entry_type"] == "OKX_DEMO_LONG_FILL_CONTRACTS"
            and ledger["entry_digest"] == _digest(expected_payload)
        ):
            exact_ledgers.append(ledger)
    counts["ledger_entries"] = len(exact_ledgers)
    ledger_ids = [row["id"] for row in exact_ledgers]
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
    exact_items = [
        row
        for row in items
        if row["item_type"] == "OKX_DEMO_ORDER_FILL_LEDGER_CHAIN"
        and row["evidence_digest"] == _digest(dict(row["evidence_json"]))
    ]
    counts["reconciliation_items"] = len(exact_items)
    run_ids = [row["reconciliation_run_id"] for row in exact_items]
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
    exact_runs = [
        row
        for row in runs
        if row["receipt_digest"]
        == _digest({"run_id": str(row["id"]), "scope_digest": row["scope_digest"]})
    ]
    counts["reconciliation_runs"] = len(exact_runs)
    if (
        not exact_fills
        or len(exact_fills) != len(fills)
        or len({row["exchange_fill_id"] for row in exact_fills}) != len(exact_fills)
        or filled_size != requested_size
    ):
        reasons.append("EXACT_COMPLETE_FILL_EVIDENCE_UNSET")
    if len(exact_ledgers) != len(exact_fills) or len(ledgers) != len(exact_fills):
        reasons.append("EXACT_LEDGER_EVIDENCE_UNSET")
    if (
        len(exact_items) != len(exact_fills)
        or len(items) != len(exact_fills)
        or len(exact_runs) != len(exact_fills)
        or len(runs) != len(exact_fills)
        or len(set(run_ids)) != len(exact_fills)
    ):
        reasons.append("EXACT_RECONCILIATION_EVIDENCE_UNSET")
    if stage == "RECOVERY_SOAK":
        policy = exact_policy_source[0] if len(exact_policy_source) == 1 else None
        linked_budgets = (
            [
                row
                for row in budgets
                if policy is not None
                and row["execution_canary_risk_policy_id"] == policy["id"]
            ]
            if policy is not None
            else []
        )
        accepted_reservations = [
            row for row in reservations if row["status"] == "RISK_ACCEPTED"
        ]
        exact_fill_evidence = []
        for fill in exact_fills:
            matching_ledger = [
                row for row in exact_ledgers if row["fill_id"] == fill["id"]
            ]
            matching_item = [row for row in exact_items if row["fill_id"] == fill["id"]]
            if len(matching_ledger) != 1 or len(matching_item) != 1:
                continue
            ledger = matching_ledger[0]
            item = matching_item[0]
            run = next(
                (
                    row
                    for row in exact_runs
                    if row["id"] == item["reconciliation_run_id"]
                ),
                None,
            )
            if run is None:
                continue
            exact_fill_evidence.append(
                {
                    "fill_id": str(fill["id"]),
                    "fill_receipt_digest": fill["receipt_digest"],
                    "ledger_entry_id": str(ledger["id"]),
                    "ledger_entry_digest": ledger["entry_digest"],
                    "reconciliation_run_id": str(run["id"]),
                    "reconciliation_receipt_digest": run["receipt_digest"],
                    "size": _decimal_text(fill["fill_json"]["size"]),
                }
            )
        exact_fill_evidence.sort(key=lambda value: value["fill_id"])
        terminal_is_exact = False
        if (
            policy is not None
            and policy["status"] == "TERMINATED"
            and policy["terminated_at"] is not None
            and len(linked_budgets) == 1
            and len(accepted_reservations) == 1
            and len(exact_fill_evidence) == len(exact_fills)
            and len(exact_fills) == len(exact_runs)
        ):
            terminal_payload = {
                "contract": "canonical-v13-canary-risk-policy-termination-v2",
                "policy_id": str(policy["id"]),
                "policy_receipt_digest": policy["receipt_digest"],
                "authorization_digest": linked_budgets[0]["authorization_digest"],
                "reservation_digest": accepted_reservations[0]["reservation_digest"],
                "order_id": str(order["id"]),
                "order_receipt_digest": order["receipt_digest"],
                "complete_fill_ledger_reconciliation": exact_fill_evidence,
                "actor_identity": policy["actor_identity"],
                "terminated_at": _persisted_utc(policy["terminated_at"]).isoformat(),
            }
            terminal_is_exact = policy["termination_digest"] == _digest(
                terminal_payload
            ) and all(row["completed_at"] is not None for row in exact_runs)
        if not terminal_is_exact:
            reasons.append("EXACT_CANARY_POLICY_TERMINATION_V2_UNPROVEN")
    return counts


def _recovery_acceptance_is_exact(
    connection: Connection,
    event: Mapping[str, object],
    *,
    handoff: Phase9QualificationHandoff,
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
    policy = (
        connection.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.qualification_decision_id
                == handoff.qualification_decision_id
            )
        )
        .mappings()
        .one_or_none()
    )
    exact_orders = (
        connection.execute(
            select(ORDERS_TABLE.c.receipt_digest)
            .select_from(
                ORDERS_TABLE.join(
                    RISK_DECISIONS_TABLE,
                    RISK_DECISIONS_TABLE.c.id == ORDERS_TABLE.c.risk_decision_id,
                )
                .join(
                    TRADE_INTENTS_TABLE,
                    TRADE_INTENTS_TABLE.c.id == RISK_DECISIONS_TABLE.c.trade_intent_id,
                )
                .join(
                    SIGNALS_TABLE,
                    SIGNALS_TABLE.c.id == TRADE_INTENTS_TABLE.c.signal_id,
                )
            )
            .where(SIGNALS_TABLE.c.research_target_id == handoff.research_target_id)
        )
        .mappings()
        .all()
    )
    exact_order = exact_orders[0] if len(exact_orders) == 1 else None
    latest_runtime_receipt = connection.execute(
        select(RUNTIME_RECEIPTS_TABLE.c.receipt_digest)
        .select_from(
            RUNTIME_RECEIPTS_TABLE.join(
                RUNTIME_INSTANCES_TABLE,
                RUNTIME_INSTANCES_TABLE.c.id
                == RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id,
            ).join(
                DEPLOYMENTS_TABLE,
                DEPLOYMENTS_TABLE.c.id == RUNTIME_INSTANCES_TABLE.c.deployment_id,
            )
        )
        .where(DEPLOYMENTS_TABLE.c.strategy_version_id == handoff.strategy_version_id)
        .order_by(RUNTIME_RECEIPTS_TABLE.c.observed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
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
        and policy is not None
        and policy["status"] == "TERMINATED"
        and evidence.get("policy_termination_receipt_digest")
        == policy["termination_digest"]
        and exact_order is not None
        and evidence.get("order_replay_receipt_digest") == exact_order["receipt_digest"]
        and evidence.get("observability_receipt_digest") == latest_runtime_receipt
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
    if stage == "QUALIFICATION_HANDOFF":
        nonterminal_deployments = int(
            effective.execute(
                select(func.count())
                .select_from(DEPLOYMENTS_TABLE)
                .where(DEPLOYMENTS_TABLE.c.status.in_(("PENDING", "ACTIVE")))
            ).scalar_one()
        )
        if nonterminal_deployments:
            reasons.append(f"NONTERMINAL_DEPLOYMENT_PRESENT={nonterminal_deployments}")
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
        relevant_global_counts = dict(execution_counts)
        live_policy_rows = (
            effective.execute(
                select(
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id,
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.probe_receipt_id,
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.execution_attestation_id,
                ).where(EXECUTION_CANARY_RISK_POLICIES_TABLE.c.status != "EXPIRED")
            )
            .mappings()
            .all()
        )
        relevant_global_counts["execution_canary_risk_policies"] = len(
            live_policy_rows
        )
        relevant_global_counts["execution_canary_probe_receipts"] = len(
            {row["probe_receipt_id"] for row in live_policy_rows}
        )
        relevant_global_counts["execution_attestations"] = len(
            {row["execution_attestation_id"] for row in live_policy_rows}
        )
        for table_name in _STRICT_CANARY_GLOBAL_TABLES:
            if relevant_global_counts[table_name] != lineage_counts[table_name]:
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
            if _recovery_acceptance_is_exact(
                effective, event, handoff=qualification_handoff
            )
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
