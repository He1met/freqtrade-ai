"""One-shot exact-lineage risk policy for the canonical OKX_DEMO canary."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    lock_execution_boundary,
    require_canonical_execution,
    require_digest,
    require_identity,
)
from app.canonical_v13.models import (
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    ORDERS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
    RECONCILIATION_ITEMS_TABLE,
    RECONCILIATION_RUNS_TABLE,
    RISK_DECISIONS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_VERSIONS_TABLE,
)

POLICY_TTL = timedelta(minutes=30)
MARK_MAXIMUM_AGE = timedelta(seconds=60)
STRATEGY_LEVERAGE_CAP = Decimal(14)
EXECUTION_TARGET = "OKX_DEMO"
INSTRUMENT = "BTC-USDT-SWAP"
PAIR = "BTC/USDT:USDT"


@dataclass(frozen=True)
class CanaryRiskPolicyResult:
    policy_id: UUID
    request_digest: str
    policy_digest: str
    receipt_digest: str
    max_notional: Decimal
    effective_leverage: Decimal
    accepted_at: datetime
    expires_at: datetime
    repeat_noop: bool


@dataclass(frozen=True)
class CanaryRiskPolicyTerminationResult:
    policy_id: UUID
    termination_digest: str
    terminated_at: datetime
    repeat_noop: bool


def _utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_TIMEZONE", f"{field} must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _persisted_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _positive_decimal(value: object, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        parsed = Decimal("NaN")
    if not parsed.is_finite() or parsed <= 0:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_NUMBER", f"{field} must be finite and positive"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _require_exact_strategy(source: str) -> None:
    try:
        tree = ast.parse(source, filename="<canonical-canary-policy>", mode="exec")
    except (SyntaxError, ValueError) as exc:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_STRATEGY_AST", "strategy artifact is not valid Python"
        ) from exc
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    if len(classes) != 1:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_STRATEGY_AST", "exactly one strategy class is required"
        )
    strategy = classes[0]
    can_short = [
        node
        for node in strategy.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and (
            any(
                isinstance(target, ast.Name) and target.id == "can_short"
                for target in node.targets
            )
            if isinstance(node, ast.Assign)
            else isinstance(node.target, ast.Name) and node.target.id == "can_short"
        )
    ]
    if (
        len(can_short) != 1
        or not isinstance(can_short[0].value, ast.Constant)
        or can_short[0].value.value is not False
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_STRATEGY_NOT_LONG_ONLY", "can_short must be exactly False"
        )
    leverage = [
        node
        for node in strategy.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "leverage"
    ]
    if len(leverage) != 1 or "max_leverage" not in {
        argument.arg for argument in leverage[0].args.args
    }:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_STRATEGY_LEVERAGE_AST",
            "one leverage(max_leverage) method is required",
        )
    statements = leverage[0].body
    if (
        len(statements) != 1
        or not isinstance(statements[0], ast.Return)
        or not isinstance(statements[0].value, ast.Call)
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_STRATEGY_LEVERAGE_AST",
            "leverage body must be one exact return",
        )
    call = statements[0].value
    if (
        not isinstance(call.func, ast.Name)
        or call.func.id != "min"
        or call.keywords
        or len(call.args) != 2
        or not isinstance(call.args[0], ast.Constant)
        or not isinstance(call.args[0].value, (int, float))
        or isinstance(call.args[0].value, bool)
        or Decimal(str(call.args[0].value)) != STRATEGY_LEVERAGE_CAP
        or not isinstance(call.args[1], ast.Name)
        or call.args[1].id != "max_leverage"
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_STRATEGY_LEVERAGE_AST",
            "strategy must return min(14.0, max_leverage) exactly",
        )


def _market_facts(
    evidence: Mapping[str, object], *, now: datetime
) -> dict[str, object]:
    metadata = evidence.get("instrument_metadata")
    if not isinstance(metadata, Mapping):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_METADATA", "instrument_metadata must be an object"
        )
    metadata = dict(metadata)
    if set(metadata) != {
        "instrument",
        "instrument_type",
        "contract_type",
        "base_currency",
        "quote_currency",
        "settle_currency",
        "contract_value",
        "contract_value_currency",
        "lot_size",
        "minimum_size",
        "exchange_max_leverage",
        "state",
    }:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_METADATA_FIELDS",
            "instrument metadata field set is not allowlisted",
        )
    metadata_receipt_digest = require_digest(
        evidence.get("metadata_receipt_digest"), field="metadata_receipt_digest"
    )
    expected_metadata_digest = canonical_execution_digest(
        {
            "contract": "canonical-v13-okx-demo-instrument-metadata-receipt-v1",
            "execution_target": EXECUTION_TARGET,
            "instrument": INSTRUMENT,
            "instrument_metadata": metadata,
        }
    )
    if metadata_receipt_digest != expected_metadata_digest:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_METADATA_DIGEST", "instrument metadata receipt drifted"
        )
    if (
        metadata.get("instrument") != INSTRUMENT
        or metadata.get("instrument_type") != "SWAP"
        or metadata.get("contract_type") != "linear"
        or metadata.get("base_currency") != "BTC"
        or metadata.get("quote_currency") != "USDT"
        or metadata.get("settle_currency") != "USDT"
        or metadata.get("state") != "live"
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_METADATA_LINEAGE",
            "only the exact live linear BTC-USDT swap is supported",
        )
    contract_value_ccy = require_identity(
        metadata.get("contract_value_currency"),
        field="contract_value_currency",
        maximum=24,
    )
    if contract_value_ccy != "BTC":
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_INVERSE_OR_UNPROVEN_CONTRACT",
            "linear base-denominated contract value is required",
        )
    minimum_size = _positive_decimal(metadata.get("minimum_size"), field="minimum_size")
    lot_size = _positive_decimal(metadata.get("lot_size"), field="lot_size")
    contract_value = _positive_decimal(
        metadata.get("contract_value"), field="contract_value"
    )
    exchange_max = _positive_decimal(
        metadata.get("exchange_max_leverage"), field="exchange_max_leverage"
    )
    units = minimum_size / lot_size
    if units != units.to_integral_value():
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_MINIMUM_SIZE_ALIGNMENT",
            "minimum_size must align to lot_size",
        )
    mark_price = _positive_decimal(evidence.get("mark_price"), field="mark_price")
    try:
        mark_observed_at = _utc(
            datetime.fromisoformat(str(evidence.get("mark_observed_at"))),
            field="mark_observed_at",
        )
    except ValueError as exc:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_MARK_TIME", "mark observation timestamp is invalid"
        ) from exc
    if not -timedelta(seconds=5) <= now - mark_observed_at <= MARK_MAXIMUM_AGE:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_MARK_FRESHNESS", "mark price must be fresh"
        )
    mark_receipt_digest = require_digest(
        evidence.get("mark_price_receipt_digest"), field="mark_price_receipt_digest"
    )
    expected_mark_digest = canonical_execution_digest(
        {
            "contract": "canonical-v13-okx-demo-mark-price-receipt-v1",
            "execution_target": EXECUTION_TARGET,
            "instrument": INSTRUMENT,
            "metadata_receipt_digest": metadata_receipt_digest,
            "mark_price": _decimal_text(mark_price),
            "observed_at": mark_observed_at.isoformat(),
        }
    )
    if mark_receipt_digest != expected_mark_digest:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_MARK_DIGEST", "mark-price receipt drifted"
        )
    minimum_notional = minimum_size * contract_value * mark_price
    return {
        "minimum_size": minimum_size,
        "contract_value": contract_value,
        "contract_value_ccy": contract_value_ccy,
        "mark_price": mark_price,
        "mark_observed_at": mark_observed_at,
        "exchange_max_leverage": exchange_max,
        "effective_leverage": min(STRATEGY_LEVERAGE_CAP, exchange_max),
        "minimum_notional": minimum_notional,
        "metadata_receipt_digest": metadata_receipt_digest,
        "mark_price_receipt_digest": mark_receipt_digest,
    }


def authorize_canary_risk_policy(
    connection: Connection,
    *,
    qualification_decision_id: UUID,
    deployment_approval_id: UUID,
    execution_attestation_id: UUID,
    actor_identity: str,
    idempotency_key: str,
    reason: str,
    redacted_evidence: Mapping[str, object],
    evaluated_at: datetime | None = None,
) -> CanaryRiskPolicyResult:
    """Persist one immutable policy; exact POST replay is a no-op forever."""

    effective = require_canonical_execution(connection)
    now = _utc(evaluated_at or datetime.now(timezone.utc), field="evaluated_at")
    actor_identity = require_identity(
        actor_identity, field="actor_identity", maximum=160
    )
    idempotency_key = require_identity(
        idempotency_key, field="idempotency_key", maximum=200
    )
    reason = require_identity(reason, field="reason", maximum=2000)
    if not isinstance(redacted_evidence, Mapping):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_EVIDENCE", "redacted evidence must be an object"
        )
    evidence = dict(redacted_evidence)
    if set(evidence) != {
        "contract",
        "execution_target",
        "instrument",
        "position_policy",
        "max_order_count",
        "allow_real_funds",
        "instrument_metadata",
        "metadata_receipt_digest",
        "mark_price",
        "mark_observed_at",
        "mark_price_receipt_digest",
        "attestation_digest",
    }:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_EVIDENCE_FIELDS",
            "redacted evidence field set is not allowlisted",
        )
    request_payload = {
        "contract": "canonical-v13-canary-risk-policy-request-v1",
        "qualification_decision_id": str(qualification_decision_id),
        "deployment_approval_id": str(deployment_approval_id),
        "execution_attestation_id": str(execution_attestation_id),
        "actor_identity": actor_identity,
        "idempotency_key": idempotency_key,
        "reason": reason,
        "redacted_evidence": evidence,
    }
    request_digest = canonical_execution_digest(request_payload)
    lock_execution_boundary(
        effective, key=f"canary-risk-policy:{qualification_decision_id}"
    )
    existing = (
        effective.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                (
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.qualification_decision_id
                    == qualification_decision_id
                )
                | (
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.deployment_approval_id
                    == deployment_approval_id
                )
                | (
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.idempotency_key
                    == idempotency_key
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["request_digest"] != request_digest:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_POLICY_REPLAY_DRIFT",
                "one-shot policy cannot be reset or replaced",
            )
        return CanaryRiskPolicyResult(
            existing["id"],
            request_digest,
            existing["policy_digest"],
            existing["receipt_digest"],
            Decimal(str(existing["max_notional"])),
            Decimal(str(existing["effective_leverage"])),
            _persisted_utc(existing["accepted_at"]),
            _persisted_utc(existing["expires_at"]),
            True,
        )
    decision = (
        effective.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id == qualification_decision_id
            )
        )
        .mappings()
        .one_or_none()
    )
    approval = (
        effective.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id == deployment_approval_id
            )
        )
        .mappings()
        .one_or_none()
    )
    version = (
        effective.execute(
            select(STRATEGY_VERSIONS_TABLE).where(
                STRATEGY_VERSIONS_TABLE.c.id == decision["strategy_version_id"]
            )
        )
        .mappings()
        .one_or_none()
        if decision is not None
        else None
    )
    artifact = (
        effective.execute(
            select(STRATEGY_ARTIFACTS_TABLE).where(
                STRATEGY_ARTIFACTS_TABLE.c.id == version["artifact_id"]
            )
        )
        .mappings()
        .one_or_none()
        if version is not None
        else None
    )
    target = (
        effective.execute(
            select(RESEARCH_TARGETS_TABLE).where(
                RESEARCH_TARGETS_TABLE.c.id == decision["research_target_id"]
            )
        )
        .mappings()
        .one_or_none()
        if decision is not None
        else None
    )
    attestation = (
        effective.execute(
            select(EXECUTION_ATTESTATIONS_TABLE).where(
                EXECUTION_ATTESTATIONS_TABLE.c.id == execution_attestation_id
            )
        )
        .mappings()
        .one_or_none()
    )
    deployment = (
        effective.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == attestation["deployment_id"]
            )
        )
        .mappings()
        .one_or_none()
        if attestation is not None
        else None
    )
    if (
        decision is None
        or decision["status"] != "QUALIFIED"
        or approval is None
        or approval["status"] != "APPROVED"
        or approval["qualification_decision_id"] != qualification_decision_id
        or approval["strategy_version_id"] != decision["strategy_version_id"]
        or version is None
        or artifact is None
        or target is None
        or deployment is None
        or deployment["status"] != "ACTIVE"
        or deployment["deployment_approval_id"] != deployment_approval_id
        or deployment["strategy_version_id"] != decision["strategy_version_id"]
        or deployment["configuration_bundle_id"] != decision["configuration_bundle_id"]
        or deployment["configuration_bundle_digest"]
        != decision["configuration_bundle_digest"]
        or deployment["market_snapshot_id"] != decision["market_snapshot_id"]
        or deployment["market_snapshot_digest"] != decision["market_snapshot_digest"]
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_LINEAGE",
            "exact qualified approval and active Demo deployment are required",
        )
    if (
        target["instrument"] != INSTRUMENT
        or target["pair"] != PAIR
        or target["data_kind"] != "futures"
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_TARGET", "qualified target must be BTC-USDT-SWAP"
        )
    source = artifact["normalized_content"]
    if sha256(source.encode("utf-8")).hexdigest() != artifact["content_digest"]:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_ARTIFACT_DIGEST", "strategy artifact content digest drifted"
        )
    _require_exact_strategy(source)
    if (
        evidence.get("contract") != "canonical-v13-okx-demo-canary-policy-evidence-v1"
        or evidence.get("execution_target") != EXECUTION_TARGET
        or evidence.get("instrument") != INSTRUMENT
        or evidence.get("position_policy") != "LONG_ONLY"
        or evidence.get("max_order_count") != 1
        or evidence.get("allow_real_funds") is not False
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_EVIDENCE", "fixed canary policy facts drifted"
        )
    facts = _market_facts(evidence, now=now)
    attestation_digest = require_digest(
        evidence.get("attestation_digest"), field="attestation_digest"
    )
    if (
        attestation is None
        or attestation["status"] != "READY"
        or attestation["attestation_digest"] != attestation_digest
        or attestation["instrument"] != INSTRUMENT
        or attestation["execution_target"] != EXECUTION_TARGET
        or attestation["permissions_json"]
        != {"read": True, "trade": True, "withdraw": False}
        or _persisted_utc(attestation["observed_at"]) != facts["mark_observed_at"]
        or not _persisted_utc(attestation["observed_at"])
        <= now
        < _persisted_utc(attestation["expires_at"])
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_ATTESTATION",
            "fresh exact redacted Demo attestation is required",
        )

    policy_payload = {
        "contract": "canonical-v13-canary-risk-policy-v1",
        "request_digest": request_digest,
        "qualification_decision_id": str(qualification_decision_id),
        "qualification_decision_digest": decision["decision_digest"],
        "strategy_version_id": str(version["id"]),
        "strategy_artifact_id": str(artifact["id"]),
        "strategy_artifact_digest": artifact["content_digest"],
        "research_target_id": str(target["id"]),
        "research_target_digest": target["target_digest"],
        "configuration_bundle_id": str(decision["configuration_bundle_id"]),
        "configuration_bundle_digest": decision["configuration_bundle_digest"],
        "market_snapshot_id": str(decision["market_snapshot_id"]),
        "market_snapshot_digest": decision["market_snapshot_digest"],
        "execution_target": EXECUTION_TARGET,
        "instrument": INSTRUMENT,
        "position_policy": "LONG_ONLY",
        "max_order_count": 1,
        "minimum_contract_size": _decimal_text(facts["minimum_size"]),
        "contract_value": _decimal_text(facts["contract_value"]),
        "contract_value_ccy": facts["contract_value_ccy"],
        "mark_price": _decimal_text(facts["mark_price"]),
        "max_notional": _decimal_text(facts["minimum_notional"]),
        "strategy_max_leverage": _decimal_text(STRATEGY_LEVERAGE_CAP),
        "exchange_max_leverage": _decimal_text(facts["exchange_max_leverage"]),
        "effective_leverage": _decimal_text(facts["effective_leverage"]),
        "metadata_receipt_digest": facts["metadata_receipt_digest"],
        "mark_price_receipt_digest": facts["mark_price_receipt_digest"],
        "attestation_digest": attestation_digest,
        "allow_real_funds": False,
    }
    policy_digest = canonical_execution_digest(policy_payload)
    policy_id = uuid4()
    expires_at = now + POLICY_TTL
    receipt_payload = {
        "contract": "canonical-v13-canary-risk-policy-receipt-v1",
        "policy_id": str(policy_id),
        "request_digest": request_digest,
        "policy_digest": policy_digest,
        "accepted_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "status": "ACTIVE",
    }
    receipt_digest = canonical_execution_digest(receipt_payload)
    effective.execute(
        EXECUTION_CANARY_RISK_POLICIES_TABLE.insert().values(
            id=policy_id,
            qualification_decision_id=qualification_decision_id,
            deployment_approval_id=deployment_approval_id,
            execution_attestation_id=execution_attestation_id,
            strategy_version_id=version["id"],
            strategy_artifact_id=artifact["id"],
            strategy_artifact_digest=artifact["content_digest"],
            research_target_id=target["id"],
            research_target_digest=target["target_digest"],
            configuration_bundle_id=decision["configuration_bundle_id"],
            configuration_bundle_digest=decision["configuration_bundle_digest"],
            market_snapshot_id=decision["market_snapshot_id"],
            market_snapshot_digest=decision["market_snapshot_digest"],
            execution_target=EXECUTION_TARGET,
            instrument=INSTRUMENT,
            position_policy="LONG_ONLY",
            max_order_count=1,
            minimum_contract_size=facts["minimum_size"],
            contract_value=facts["contract_value"],
            contract_value_ccy=facts["contract_value_ccy"],
            mark_price=facts["mark_price"],
            max_notional=facts["minimum_notional"],
            strategy_max_leverage=STRATEGY_LEVERAGE_CAP,
            exchange_max_leverage=facts["exchange_max_leverage"],
            effective_leverage=facts["effective_leverage"],
            metadata_receipt_digest=facts["metadata_receipt_digest"],
            mark_price_receipt_digest=facts["mark_price_receipt_digest"],
            attestation_digest=attestation_digest,
            actor_identity=actor_identity,
            idempotency_key=idempotency_key,
            reason=reason,
            allow_real_funds=False,
            status="ACTIVE",
            observed_at=facts["mark_observed_at"],
            accepted_at=now,
            expires_at=expires_at,
            terminated_at=None,
            request_digest=request_digest,
            policy_digest=policy_digest,
            receipt_digest=receipt_digest,
            termination_digest=None,
        )
    )
    return CanaryRiskPolicyResult(
        policy_id,
        request_digest,
        policy_digest,
        receipt_digest,
        facts["minimum_notional"],
        facts["effective_leverage"],
        now,
        expires_at,
        False,
    )


def terminate_canary_risk_policy(
    connection: Connection,
    *,
    policy_id: UUID,
    reconciliation_run_id: UUID,
    actor_identity: str,
    evaluated_at: datetime | None = None,
) -> CanaryRiskPolicyTerminationResult:
    """Terminate the one-shot policy only after its exact reconciled order chain."""

    effective = require_canonical_execution(connection)
    now = _utc(evaluated_at or datetime.now(timezone.utc), field="evaluated_at")
    actor_identity = require_identity(
        actor_identity, field="actor_identity", maximum=160
    )
    lock_execution_boundary(effective, key=f"canary-risk-policy:{policy_id}")
    policy = (
        effective.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id == policy_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if policy is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_UNSET", str(policy_id)
        )
    if policy["status"] == "TERMINATED":
        return CanaryRiskPolicyTerminationResult(
            policy_id,
            policy["termination_digest"],
            _persisted_utc(policy["terminated_at"]),
            True,
        )
    if policy["status"] != "ACTIVE":
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_NOT_ACTIVE", policy["status"]
        )
    budget = (
        effective.execute(
            select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE).where(
                EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.execution_canary_risk_policy_id
                == policy_id
            )
        )
        .mappings()
        .one_or_none()
    )
    reservations = (
        effective.execute(
            select(EXECUTION_RISK_RESERVATIONS_TABLE).where(
                EXECUTION_RISK_RESERVATIONS_TABLE.c.risk_budget_authorization_id
                == budget["id"],
                EXECUTION_RISK_RESERVATIONS_TABLE.c.status == "RISK_ACCEPTED",
            )
        )
        .mappings()
        .all()
        if budget is not None
        else []
    )
    risk = (
        effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.trade_intent_id
                == reservations[0]["trade_intent_id"]
            )
        )
        .mappings()
        .one_or_none()
        if len(reservations) == 1
        else None
    )
    order = (
        effective.execute(
            select(ORDERS_TABLE).where(ORDERS_TABLE.c.risk_decision_id == risk["id"])
        )
        .mappings()
        .one_or_none()
        if risk is not None
        else None
    )
    run = (
        effective.execute(
            select(RECONCILIATION_RUNS_TABLE).where(
                RECONCILIATION_RUNS_TABLE.c.id == reconciliation_run_id
            )
        )
        .mappings()
        .one_or_none()
    )
    items = (
        effective.execute(
            select(RECONCILIATION_ITEMS_TABLE).where(
                RECONCILIATION_ITEMS_TABLE.c.reconciliation_run_id
                == reconciliation_run_id,
                RECONCILIATION_ITEMS_TABLE.c.order_id == order["id"],
                RECONCILIATION_ITEMS_TABLE.c.status == "MATCHED",
            )
        )
        .mappings()
        .all()
        if order is not None
        else []
    )
    if (
        budget is None
        or len(reservations) != 1
        or risk is None
        or risk["status"] != "RISK_ACCEPTED"
        or order is None
        or order["exchange_order_id"] is None
        or order["receipt_digest"] is None
        or run is None
        or run["status"] != "SUCCEEDED"
        or run["receipt_digest"] is None
        or len(items) != 1
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_TERMINATION_LINEAGE",
            "one accepted order must finish fill/ledger/reconciliation before termination",
        )
    payload = {
        "contract": "canonical-v13-canary-risk-policy-termination-v1",
        "policy_id": str(policy_id),
        "policy_receipt_digest": policy["receipt_digest"],
        "authorization_digest": budget["authorization_digest"],
        "reservation_digest": reservations[0]["reservation_digest"],
        "order_id": str(order["id"]),
        "order_receipt_digest": order["receipt_digest"],
        "reconciliation_run_id": str(reconciliation_run_id),
        "reconciliation_receipt_digest": run["receipt_digest"],
        "actor_identity": actor_identity,
        "terminated_at": now.isoformat(),
    }
    termination_digest = canonical_execution_digest(payload)
    effective.execute(
        EXECUTION_CANARY_RISK_POLICIES_TABLE.update()
        .where(
            EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id == policy_id,
            EXECUTION_CANARY_RISK_POLICIES_TABLE.c.status == "ACTIVE",
        )
        .values(
            status="TERMINATED",
            terminated_at=now,
            termination_digest=termination_digest,
        )
    )
    return CanaryRiskPolicyTerminationResult(policy_id, termination_digest, now, False)


__all__ = [
    "INSTRUMENT",
    "MARK_MAXIMUM_AGE",
    "POLICY_TTL",
    "STRATEGY_LEVERAGE_CAP",
    "CanaryRiskPolicyResult",
    "CanaryRiskPolicyTerminationResult",
    "authorize_canary_risk_policy",
    "terminate_canary_risk_policy",
]
