"""One-shot exact-lineage risk policy for the canonical OKX_DEMO canary."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import Connection, or_, select

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
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    FILLS_TABLE,
    LEDGER_ENTRIES_TABLE,
    ORDERS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RECONCILIATION_ITEMS_TABLE,
    RECONCILIATION_RUNS_TABLE,
    RESEARCH_TARGETS_TABLE,
    RISK_DECISIONS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_VERSIONS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.phase9_okx_demo import RedactedOkxDemoProbe
from app.canonical_v13.phase9_order_writer import (
    terminal_rejected_canary_order_evidence,
)

POLICY_TTL = timedelta(minutes=30)
MARK_MAXIMUM_AGE = timedelta(seconds=60)
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


@dataclass(frozen=True)
class CanaryProbeReceiptResult:
    probe_receipt_id: UUID
    receipt_digest: str
    observed_at: datetime
    expires_at: datetime
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


def _persisted_decimal_equal(
    connection: Connection, left: object, right: object
) -> bool:
    left_decimal = Decimal(str(left))
    right_decimal = Decimal(str(right))
    if connection.dialect.name != "sqlite":
        return left_decimal == right_decimal
    return abs(left_decimal - right_decimal) <= Decimal("1e-12")


def _approved_recovery_predecessor(
    connection: Connection,
    *,
    active_policy: Mapping[str, object],
    qualification_decision_id: UUID,
    deployment_approval_id: UUID,
) -> Mapping[str, object] | None:
    """Prove that one spent predecessor belongs to the exact recovery approval.

    A normal policy with downstream budget authority remains non-renewable.  The
    sole exception is the already-approved generation-two recovery path: its
    generation-one deployment must be disabled, the successor must be the unique
    active deployment, and the bound order must still be the exact terminal
    two-attempt rejection with zero fill/accounting/reconciliation side effects.
    """

    recovery_approval = (
        connection.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id == deployment_approval_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        recovery_approval is None
        or recovery_approval["status"] != "APPROVED"
        or recovery_approval["approval_generation"] != 2
        or recovery_approval["qualification_decision_id"] != qualification_decision_id
        or recovery_approval["recovery_of_deployment_id"] is None
        or recovery_approval["recovery_order_id"] is None
        or recovery_approval["recovery_request_digest"] is None
        or recovery_approval["recovery_receipt_digest"] is None
    ):
        return None
    source_deployment = (
        connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == recovery_approval["recovery_of_deployment_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    source_probe = (
        connection.execute(
            select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
                EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id
                == active_policy["probe_receipt_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    source_approval = (
        connection.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id
                == source_deployment["deployment_approval_id"]
            )
        )
        .mappings()
        .one_or_none()
        if source_deployment is not None
        else None
    )
    active_successors = (
        connection.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.deployment_approval_id == deployment_approval_id,
                DEPLOYMENTS_TABLE.c.status == "ACTIVE",
            )
        )
        .mappings()
        .all()
    )
    if (
        source_deployment is None
        or source_deployment["status"] != "DISABLED"
        or source_deployment["superseded_by_qualification_decision_id"]
        != qualification_decision_id
        or source_deployment["disable_request_digest"] is None
        or source_deployment["disable_receipt_digest"] is None
        or source_approval is None
        or source_approval["status"] != "APPROVED"
        or source_approval["approval_generation"] != 1
        or source_approval["qualification_decision_id"]
        != qualification_decision_id
        or active_policy["deployment_approval_id"]
        != source_approval["id"]
        or active_policy["strategy_version_id"]
        != recovery_approval["strategy_version_id"]
        or source_deployment["strategy_version_id"]
        != recovery_approval["strategy_version_id"]
        or source_probe is None
        or source_probe["deployment_id"] != source_deployment["id"]
        or len(active_successors) != 1
        or active_successors[0]["strategy_version_id"]
        != source_deployment["strategy_version_id"]
        or active_successors[0]["configuration_bundle_id"]
        != source_deployment["configuration_bundle_id"]
        or active_successors[0]["configuration_bundle_digest"]
        != source_deployment["configuration_bundle_digest"]
        or active_successors[0]["market_snapshot_id"]
        != source_deployment["market_snapshot_id"]
        or active_successors[0]["market_snapshot_digest"]
        != source_deployment["market_snapshot_digest"]
        or active_successors[0]["demo_only"] is not True
        or active_successors[0]["allow_real_funds"] is not False
    ):
        return None
    try:
        terminal = terminal_rejected_canary_order_evidence(
            connection,
            order_id=recovery_approval["recovery_order_id"],
            deployment_id=source_deployment["id"],
        )
    except CanonicalExecutionChainBlocked:
        return None
    return {
        "recovery_approval_id": str(recovery_approval["id"]),
        "recovery_approval_digest": recovery_approval["approval_digest"],
        "recovery_request_digest": recovery_approval["recovery_request_digest"],
        "recovery_receipt_digest": recovery_approval["recovery_receipt_digest"],
        "source_deployment_id": str(source_deployment["id"]),
        "source_policy_id": str(active_policy["id"]),
        "terminal_evidence_digest": terminal["evidence_digest"],
    }


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


def _nonnegative_decimal(value: object, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        parsed = Decimal("NaN")
    if not parsed.is_finite() or parsed < 0:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_NUMBER", f"{field} must be finite and nonnegative"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def extract_strategy_leverage_cap(source: str) -> Decimal:
    """Extract the exact long-only leverage literal from the accepted artifact."""
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
        or not isinstance(call.args[1], ast.Name)
        or call.args[1].id != "max_leverage"
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_STRATEGY_LEVERAGE_AST",
            "strategy must return min(<positive literal>, max_leverage) exactly",
        )
    leverage_cap = Decimal(str(call.args[0].value))
    if not leverage_cap.is_finite() or leverage_cap <= 0:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_STRATEGY_LEVERAGE_AST",
            "strategy leverage literal must be finite and positive",
        )
    return leverage_cap


def _resource_digest(
    *,
    resource: str,
    observed_at: datetime,
    expires_at: datetime,
    authenticated: bool,
    facts: dict[str, object],
) -> str:
    return canonical_execution_digest(
        {
            "execution_target": EXECUTION_TARGET,
            "resource": resource,
            "source": "okx_demo_rest",
            "authenticated": authenticated,
            "observed_at": observed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "facts": facts,
        }
    )


def _probe_safe_facts(probe: object) -> dict[str, object]:
    numeric_fields = (
        "contract_value",
        "lot_size",
        "min_size",
        "tick_size",
        "mark_price",
        "current_long_leverage",
        "current_short_leverage",
        "exchange_max_leverage",
        "limit_price",
        "maximum_buy_contracts",
    )
    values = {name: getattr(probe, name, None) for name in numeric_fields}
    if (
        getattr(probe, "execution_target", None) != EXECUTION_TARGET
        or getattr(probe, "instrument", None) != INSTRUMENT
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_PROBE_IDENTITY", "exact OKX_DEMO instrument is required"
        )
    decimals = {
        name: _positive_decimal(value, field=name) for name, value in values.items()
    }
    if getattr(probe, "contract_value_currency", None) != "BTC":
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_INVERSE_OR_UNPROVEN_CONTRACT",
            "linear base-denominated contract value is required",
        )
    if (
        decimals["min_size"] / decimals["lot_size"]
        != (decimals["min_size"] / decimals["lot_size"]).to_integral_value()
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_MINIMUM_SIZE_ALIGNMENT",
            "minimum_size must align to lot_size",
        )
    account_fingerprint = require_digest(
        getattr(probe, "account_fingerprint_digest", None),
        field="account_fingerprint_digest",
    )
    credential_generation = require_digest(
        getattr(probe, "credential_generation_digest", None),
        field="credential_generation_digest",
    )
    permissions = dict(getattr(probe, "permissions", {}))
    long_contracts = _nonnegative_decimal(
        getattr(probe, "long_contracts", None), field="long_contracts"
    )
    short_contracts = _nonnegative_decimal(
        getattr(probe, "short_contracts", None), field="short_contracts"
    )
    active_position_count = getattr(probe, "active_position_count", None)
    pending_order_count = getattr(probe, "pending_order_count", None)
    if (
        permissions != {"read": True, "trade": True, "withdraw": False}
        or getattr(probe, "simulated_trading", None) is not True
        or getattr(probe, "allow_real_funds", None) is not False
        or long_contracts != 0
        or short_contracts != 0
        or active_position_count != 0
        or pending_order_count != 0
        or decimals["maximum_buy_contracts"] < decimals["min_size"]
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_PROBE_PERMISSIONS", "exact Demo permissions are required"
        )
    return {
        "instrument": {
            "inst_id": INSTRUMENT,
            "inst_type": "SWAP",
            "contract_type": "linear",
            "base_ccy": "BTC",
            "quote_ccy": "USDT",
            "settle_ccy": "USDT",
            "contract_value": _decimal_text(decimals["contract_value"]),
            "contract_value_ccy": "BTC",
            "lot_size": _decimal_text(decimals["lot_size"]),
            "min_size": _decimal_text(decimals["min_size"]),
            "tick_size": _decimal_text(decimals["tick_size"]),
            "state": "live",
        },
        "mark_price": {
            "inst_id": INSTRUMENT,
            "price_kind": "mark",
            "price": _decimal_text(decimals["mark_price"]),
            "timestamp": _utc(
                getattr(probe, "mark_price_observed_at", None),
                field="mark_price_observed_at",
            ).isoformat(),
        },
        "account_config": {
            "account_level": "2",
            "position_mode": "long_short_mode",
            "account_fingerprint_digest": account_fingerprint,
            "credential_generation_digest": credential_generation,
            "permissions": permissions,
            "simulated_trading": True,
        },
        "leverage": {
            "instrument": INSTRUMENT,
            "account_fingerprint_digest": account_fingerprint,
            "long": _decimal_text(decimals["current_long_leverage"]),
            "short": _decimal_text(decimals["current_short_leverage"]),
        },
        "exchange_max_leverage": {
            "instrument": INSTRUMENT,
            "exchange_max_leverage": _decimal_text(decimals["exchange_max_leverage"]),
            "has_pending_orders": False,
        },
        "positions": {
            "instrument": INSTRUMENT,
            "margin_mode": "isolated",
            "long_contracts": "0",
            "short_contracts": "0",
            "active_position_count": 0,
        },
        "pending_orders": {
            "instrument": INSTRUMENT,
            "pending_order_count": 0,
            "exchange_reports_pending_orders": False,
        },
        "maximum_order_quantity": {
            "instrument": INSTRUMENT,
            "margin_mode": "isolated",
            "limit_price": _decimal_text(decimals["limit_price"]),
            "effective_leverage": _decimal_text(decimals["current_long_leverage"]),
            "maximum_buy_contracts": _decimal_text(
                decimals["maximum_buy_contracts"]
            ),
        },
    }


_PROBE_RESOURCE_CONTRACT = (
    ("instrument", "instrument_digest", "instruments", False),
    ("mark_price", "mark_price_digest", "mark_price", False),
    ("account_config", "account_config_digest", "account_config", True),
    ("leverage", "leverage_digest", "leverage", True),
    (
        "exchange_max_leverage",
        "exchange_max_leverage_digest",
        "exchange_max_leverage",
        True,
    ),
    ("positions", "positions_digest", "positions", True),
    ("pending_orders", "pending_orders_digest", "pending_orders", True),
    (
        "maximum_order_quantity",
        "maximum_order_quantity_digest",
        "maximum_order_quantity",
        True,
    ),
)


def _probe_times(probe: object, prefix: str) -> tuple[datetime, datetime]:
    return (
        _utc(
            getattr(probe, f"{prefix}_observed_at", None), field=f"{prefix}_observed_at"
        ),
        _utc(
            getattr(probe, f"{prefix}_expires_at", None), field=f"{prefix}_expires_at"
        ),
    )


def _probe_receipt_payload(
    *,
    receipt_id: UUID,
    deployment_id: UUID,
    execution_attestation_id: UUID,
    attestation_digest: str,
    facts_digest: str,
    resource_digests: dict[str, str],
    observed_at: datetime,
    expires_at: datetime,
) -> dict[str, object]:
    return {
        "contract": "canonical-v13-okx-demo-canary-probe-receipt-v1",
        "probe_receipt_id": str(receipt_id),
        "deployment_id": str(deployment_id),
        "execution_attestation_id": str(execution_attestation_id),
        "attestation_digest": attestation_digest,
        "execution_target": EXECUTION_TARGET,
        "instrument": INSTRUMENT,
        "safe_facts_digest": facts_digest,
        "resource_digests": resource_digests,
        "observed_at": observed_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


def persist_canary_probe_receipt(
    connection: Connection,
    *,
    probe: RedactedOkxDemoProbe,
    deployment_id: UUID,
    execution_attestation_id: UUID,
    evaluated_at: datetime | None = None,
) -> CanaryProbeReceiptResult:
    """Persist only evidence emitted by the sealed server-side OKX_DEMO probe."""

    effective = require_canonical_execution(connection)
    now = _utc(evaluated_at or datetime.now(timezone.utc), field="evaluated_at")
    if not isinstance(probe, RedactedOkxDemoProbe):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_PROBE_TYPE",
            "a sealed RedactedOkxDemoProbe instance is required",
        )
    safe_facts = _probe_safe_facts(probe)
    facts_digest = canonical_execution_digest(safe_facts)
    resource_digests: dict[str, str] = {}
    resource_times: dict[str, tuple[datetime, datetime]] = {}
    for prefix, digest_field, resource, authenticated in _PROBE_RESOURCE_CONTRACT:
        observed, expires = _probe_times(probe, prefix)
        if (
            observed > now + timedelta(seconds=5)
            or expires <= now
            or expires <= observed
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_PROBE_FRESHNESS",
                f"{resource} evidence is not currently fresh",
            )
        if resource == "mark_price" and now - observed > MARK_MAXIMUM_AGE:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_MARK_FRESHNESS", "mark price must be fresh"
            )
        expected = _resource_digest(
            resource=resource,
            observed_at=observed,
            expires_at=expires,
            authenticated=authenticated,
            facts=safe_facts[prefix],
        )
        supplied = require_digest(
            getattr(probe, digest_field, None), field=digest_field
        )
        if supplied != expected:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_PROBE_RESOURCE_DIGEST",
                f"{resource} resource digest drifted",
            )
        resource_digests[prefix] = supplied
        resource_times[prefix] = (observed, expires)
    observed_at = max(value[0] for value in resource_times.values())
    resource_expires_at = min(value[1] for value in resource_times.values())
    expires_at = _utc(probe.expires_at, field="expires_at")
    if (
        _utc(probe.observed_at, field="observed_at") != observed_at
        or expires_at > resource_expires_at
        or expires_at <= now
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_PROBE_COMBINED_WINDOW",
            "combined probe freshness window drifted",
        )
    deployment = (
        effective.execute(
            select(DEPLOYMENTS_TABLE).where(DEPLOYMENTS_TABLE.c.id == deployment_id)
        )
        .mappings()
        .one_or_none()
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
    if (
        deployment is None
        or deployment["status"] != "ACTIVE"
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or attestation is None
        or attestation["status"] != "READY"
        or attestation["deployment_id"] != deployment_id
        or attestation["execution_target"] != EXECUTION_TARGET
        or attestation["instrument"] != INSTRUMENT
        or attestation["account_fingerprint_digest"] != probe.account_fingerprint_digest
        or attestation["credential_generation_digest"]
        != probe.credential_generation_digest
        or attestation["permissions_json"] != dict(probe.permissions)
        or _persisted_utc(attestation["observed_at"]) != observed_at
        or _persisted_utc(attestation["expires_at"]) != expires_at
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_PROBE_LINEAGE",
            "probe must exactly match its active Demo deployment and attestation",
        )
    lock_execution_boundary(
        effective, key=f"canary-probe-receipt:{execution_attestation_id}"
    )
    existing = (
        effective.execute(
            select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
                EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.execution_attestation_id
                == execution_attestation_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["safe_facts_digest"] != facts_digest or any(
            existing[f"{prefix}_digest"] != digest
            for prefix, digest in resource_digests.items()
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_PROBE_REPLAY_DRIFT",
                "an attestation cannot be rebound to different probe evidence",
            )
        return CanaryProbeReceiptResult(
            existing["id"],
            existing["receipt_digest"],
            _persisted_utc(existing["observed_at"]),
            _persisted_utc(existing["expires_at"]),
            True,
        )
    receipt_id = uuid4()
    attestation_digest = require_digest(
        attestation["attestation_digest"], field="attestation_digest"
    )
    receipt_digest = canonical_execution_digest(
        _probe_receipt_payload(
            receipt_id=receipt_id,
            deployment_id=deployment_id,
            execution_attestation_id=execution_attestation_id,
            attestation_digest=attestation_digest,
            facts_digest=facts_digest,
            resource_digests=resource_digests,
            observed_at=observed_at,
            expires_at=expires_at,
        )
    )
    decimals = {
        name: _positive_decimal(getattr(probe, name), field=name)
        for name in (
            "contract_value",
            "lot_size",
            "min_size",
            "tick_size",
            "mark_price",
            "current_long_leverage",
            "current_short_leverage",
            "exchange_max_leverage",
            "limit_price",
            "maximum_buy_contracts",
        )
    }
    values: dict[str, object] = {
        "id": receipt_id,
        "deployment_id": deployment_id,
        "execution_attestation_id": execution_attestation_id,
        "execution_target": EXECUTION_TARGET,
        "instrument": INSTRUMENT,
        "account_fingerprint_digest": probe.account_fingerprint_digest,
        "credential_generation_digest": probe.credential_generation_digest,
        "permissions_json": dict(probe.permissions),
        "simulated_trading": True,
        "allow_real_funds": False,
        "contract_value": _decimal_text(decimals["contract_value"]),
        "contract_value_ccy": "BTC",
        "lot_size": _decimal_text(decimals["lot_size"]),
        "minimum_size": _decimal_text(decimals["min_size"]),
        "tick_size": _decimal_text(decimals["tick_size"]),
        "mark_price": _decimal_text(decimals["mark_price"]),
        "current_long_leverage": _decimal_text(decimals["current_long_leverage"]),
        "current_short_leverage": _decimal_text(decimals["current_short_leverage"]),
        "exchange_max_leverage": _decimal_text(decimals["exchange_max_leverage"]),
        "limit_price": _decimal_text(decimals["limit_price"]),
        "maximum_buy_contracts": _decimal_text(decimals["maximum_buy_contracts"]),
        "long_contracts": "0",
        "short_contracts": "0",
        "active_position_count": 0,
        "pending_order_count": 0,
        "observed_at": observed_at,
        "expires_at": expires_at,
        "safe_facts_json": safe_facts,
        "safe_facts_digest": facts_digest,
        "receipt_digest": receipt_digest,
        "created_at": now,
    }
    for prefix, digest in resource_digests.items():
        values[f"{prefix}_digest"] = digest
        values[f"{prefix}_observed_at"] = resource_times[prefix][0]
        values[f"{prefix}_expires_at"] = resource_times[prefix][1]
    effective.execute(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.insert().values(**values))
    return CanaryProbeReceiptResult(
        receipt_id, receipt_digest, observed_at, expires_at, False
    )


def _validated_probe_receipt(
    row: Mapping[str, object],
    *,
    attestation: Mapping[str, object] | None,
    deployment: Mapping[str, object] | None,
    now: datetime,
    strategy_max_leverage: Decimal | None = None,
) -> dict[str, object]:
    decimals = {
        name: _positive_decimal(row[name], field=name)
        for name in (
            "contract_value",
            "lot_size",
            "minimum_size",
            "tick_size",
            "mark_price",
            "current_long_leverage",
            "current_short_leverage",
            "exchange_max_leverage",
            "limit_price",
            "maximum_buy_contracts",
        )
    }
    if (
        decimals["minimum_size"] / decimals["lot_size"]
        != (decimals["minimum_size"] / decimals["lot_size"]).to_integral_value()
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_MINIMUM_SIZE_ALIGNMENT",
            "minimum_size must align to lot_size",
        )
    permissions = {"read": True, "trade": True, "withdraw": False}
    safe_facts: dict[str, object] = {
        "instrument": {
            "inst_id": INSTRUMENT,
            "inst_type": "SWAP",
            "contract_type": "linear",
            "base_ccy": "BTC",
            "quote_ccy": "USDT",
            "settle_ccy": "USDT",
            "contract_value": _decimal_text(decimals["contract_value"]),
            "contract_value_ccy": "BTC",
            "lot_size": _decimal_text(decimals["lot_size"]),
            "min_size": _decimal_text(decimals["minimum_size"]),
            "tick_size": _decimal_text(decimals["tick_size"]),
            "state": "live",
        },
        "mark_price": {
            "inst_id": INSTRUMENT,
            "price_kind": "mark",
            "price": _decimal_text(decimals["mark_price"]),
            "timestamp": _persisted_utc(row["mark_price_observed_at"]).isoformat(),
        },
        "account_config": {
            "account_level": "2",
            "position_mode": "long_short_mode",
            "account_fingerprint_digest": row["account_fingerprint_digest"],
            "credential_generation_digest": row["credential_generation_digest"],
            "permissions": permissions,
            "simulated_trading": True,
        },
        "leverage": {
            "instrument": INSTRUMENT,
            "account_fingerprint_digest": row["account_fingerprint_digest"],
            "long": _decimal_text(decimals["current_long_leverage"]),
            "short": _decimal_text(decimals["current_short_leverage"]),
        },
        "exchange_max_leverage": {
            "instrument": INSTRUMENT,
            "exchange_max_leverage": _decimal_text(decimals["exchange_max_leverage"]),
            "has_pending_orders": False,
        },
        "positions": {
            "instrument": INSTRUMENT,
            "margin_mode": "isolated",
            "long_contracts": "0",
            "short_contracts": "0",
            "active_position_count": 0,
        },
        "pending_orders": {
            "instrument": INSTRUMENT,
            "pending_order_count": 0,
            "exchange_reports_pending_orders": False,
        },
        "maximum_order_quantity": {
            "instrument": INSTRUMENT,
            "margin_mode": "isolated",
            "limit_price": _decimal_text(decimals["limit_price"]),
            "effective_leverage": _decimal_text(decimals["current_long_leverage"]),
            "maximum_buy_contracts": _decimal_text(
                decimals["maximum_buy_contracts"]
            ),
        },
    }
    facts_digest = canonical_execution_digest(safe_facts)
    if (
        row["execution_target"] != EXECUTION_TARGET
        or row["instrument"] != INSTRUMENT
        or row["contract_value_ccy"] != "BTC"
        or row["permissions_json"] != permissions
        or row["simulated_trading"] is not True
        or row["allow_real_funds"] is not False
        or _nonnegative_decimal(row["long_contracts"], field="long_contracts") != 0
        or _nonnegative_decimal(row["short_contracts"], field="short_contracts") != 0
        or row["active_position_count"] != 0
        or row["pending_order_count"] != 0
        or decimals["maximum_buy_contracts"] < decimals["minimum_size"]
        or row["safe_facts_json"] != safe_facts
        or row["safe_facts_digest"] != facts_digest
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_PROBE_FACTS_DRIFT", "persisted safe probe facts drifted"
        )
    resource_digests: dict[str, str] = {}
    resource_times: dict[str, tuple[datetime, datetime]] = {}
    for prefix, digest_field, resource, authenticated in _PROBE_RESOURCE_CONTRACT:
        observed = _persisted_utc(row[f"{prefix}_observed_at"])
        expires = _persisted_utc(row[f"{prefix}_expires_at"])
        if (
            observed > now + timedelta(seconds=5)
            or expires <= now
            or expires <= observed
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_PROBE_FRESHNESS",
                f"persisted {resource} evidence is not currently fresh",
            )
        if resource == "mark_price" and now - observed > MARK_MAXIMUM_AGE:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_MARK_FRESHNESS", "mark price must be fresh"
            )
        expected_digest = _resource_digest(
            resource=resource,
            observed_at=observed,
            expires_at=expires,
            authenticated=authenticated,
            facts=safe_facts[prefix],
        )
        if row[digest_field] != expected_digest:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_PROBE_RESOURCE_DIGEST",
                f"persisted {resource} digest drifted",
            )
        resource_digests[prefix] = expected_digest
        resource_times[prefix] = (observed, expires)
    observed_at = max(value[0] for value in resource_times.values())
    resource_expires_at = min(value[1] for value in resource_times.values())
    expires_at = _persisted_utc(row["expires_at"])
    if expires_at <= now:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_PROBE_FRESHNESS",
            "persisted combined probe receipt is expired",
        )
    attestation_digest = (
        require_digest(attestation["attestation_digest"], field="attestation_digest")
        if attestation is not None
        else ""
    )
    expected_receipt_digest = canonical_execution_digest(
        _probe_receipt_payload(
            receipt_id=row["id"],
            deployment_id=row["deployment_id"],
            execution_attestation_id=row["execution_attestation_id"],
            attestation_digest=attestation_digest,
            facts_digest=facts_digest,
            resource_digests=resource_digests,
            observed_at=observed_at,
            expires_at=expires_at,
        )
    )
    if (
        _persisted_utc(row["observed_at"]) != observed_at
        or expires_at > resource_expires_at
        or row["receipt_digest"] != expected_receipt_digest
        or deployment is None
        or deployment["id"] != row["deployment_id"]
        or deployment["status"] != "ACTIVE"
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or attestation is None
        or attestation["id"] != row["execution_attestation_id"]
        or attestation["deployment_id"] != row["deployment_id"]
        or attestation["status"] != "READY"
        or attestation["execution_target"] != EXECUTION_TARGET
        or attestation["instrument"] != INSTRUMENT
        or attestation["account_fingerprint_digest"]
        != row["account_fingerprint_digest"]
        or attestation["credential_generation_digest"]
        != row["credential_generation_digest"]
        or attestation["permissions_json"] != permissions
        or _persisted_utc(attestation["observed_at"]) != observed_at
        or _persisted_utc(attestation["expires_at"]) != expires_at
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_PROBE_RECEIPT_DRIFT",
            "persisted probe receipt lineage or digest drifted",
        )
    minimum_notional = (
        decimals["minimum_size"] * decimals["contract_value"] * decimals["mark_price"]
    )
    exchange_max = decimals["exchange_max_leverage"]
    leverage_cap = exchange_max
    if strategy_max_leverage is not None:
        if not strategy_max_leverage.is_finite() or strategy_max_leverage <= 0:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_STRATEGY_LEVERAGE_AST",
                "strategy leverage cap must be finite and positive",
            )
        leverage_cap = min(strategy_max_leverage, exchange_max)
    effective_leverage = decimals["current_long_leverage"]
    if effective_leverage > leverage_cap:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_CURRENT_LEVERAGE_EXCEEDS_POLICY",
            "current long leverage exceeds the strategy/exchange canary cap",
        )
    return {
        "minimum_size": decimals["minimum_size"],
        "contract_value": decimals["contract_value"],
        "contract_value_ccy": "BTC",
        "mark_price": decimals["mark_price"],
        "limit_price": decimals["limit_price"],
        "maximum_buy_contracts": decimals["maximum_buy_contracts"],
        "mark_observed_at": _persisted_utc(row["mark_price_observed_at"]),
        "exchange_max_leverage": exchange_max,
        "effective_leverage": effective_leverage,
        "minimum_notional": minimum_notional,
        "metadata_receipt_digest": row["instrument_digest"],
        "mark_price_receipt_digest": row["mark_price_digest"],
        "attestation_digest": attestation_digest,
    }


def validate_persisted_canary_probe_receipt(
    connection: Connection,
    *,
    probe_receipt_id: UUID,
    evaluated_at: datetime,
) -> dict[str, object]:
    """Read and recompute one immutable probe receipt without creating evidence."""

    effective = require_canonical_execution(connection)
    now = _utc(evaluated_at, field="evaluated_at")
    row = (
        effective.execute(
            select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
                EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id == probe_receipt_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_PROBE_RECEIPT_UNSET", str(probe_receipt_id)
        )
    attestation = (
        effective.execute(
            select(EXECUTION_ATTESTATIONS_TABLE).where(
                EXECUTION_ATTESTATIONS_TABLE.c.id
                == row["execution_attestation_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    deployment = (
        effective.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == row["deployment_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    return _validated_probe_receipt(
        row, attestation=attestation, deployment=deployment, now=now
    )


def authorize_canary_risk_policy(
    connection: Connection,
    *,
    qualification_decision_id: UUID,
    deployment_approval_id: UUID,
    probe_receipt_id: UUID,
    actor_identity: str,
    idempotency_key: str,
    reason: str,
    evaluated_at: datetime | None = None,
) -> CanaryRiskPolicyResult:
    """Persist one active policy; exact replay and expired renewal are deterministic."""

    effective = require_canonical_execution(connection)
    now = _utc(evaluated_at or datetime.now(timezone.utc), field="evaluated_at")
    actor_identity = require_identity(
        actor_identity, field="actor_identity", maximum=160
    )
    idempotency_key = require_identity(
        idempotency_key, field="idempotency_key", maximum=200
    )
    reason = require_identity(reason, field="reason", maximum=2000)
    request_payload = {
        "contract": "canonical-v13-canary-risk-policy-request-v1",
        "qualification_decision_id": str(qualification_decision_id),
        "deployment_approval_id": str(deployment_approval_id),
        "probe_receipt_id": str(probe_receipt_id),
        "actor_identity": actor_identity,
        "idempotency_key": idempotency_key,
        "reason": reason,
    }
    request_digest = canonical_execution_digest(request_payload)
    lock_execution_boundary(
        effective, key=f"canary-risk-policy:{qualification_decision_id}"
    )
    existing = (
        effective.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.idempotency_key
                == idempotency_key
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
    active = (
        effective.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.status == "ACTIVE",
                or_(
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.qualification_decision_id
                    == qualification_decision_id,
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.deployment_approval_id
                    == deployment_approval_id,
                ),
            )
        )
        .mappings()
        .one_or_none()
    )
    if active is not None:
        if _persisted_utc(active["expires_at"]) > now:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_POLICY_ACTIVE",
                "the exact lineage already has a fresh active policy",
            )
        linked_budget = effective.execute(
            select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.id).where(
                EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.execution_canary_risk_policy_id
                == active["id"]
            )
        ).scalar_one_or_none()
        recovery_predecessor = (
            _approved_recovery_predecessor(
                effective,
                active_policy=active,
                qualification_decision_id=qualification_decision_id,
                deployment_approval_id=deployment_approval_id,
            )
            if linked_budget is not None
            else None
        )
        if linked_budget is not None and recovery_predecessor is None:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_POLICY_EXPIRED_WITH_BUDGET",
                "an expired policy with downstream authority cannot be renewed",
            )
        expiration_payload: dict[str, object] = {
            "contract": "canonical-v13-canary-risk-policy-expiration-v1",
            "policy_id": str(active["id"]),
            "policy_receipt_digest": active["receipt_digest"],
            "reason_code": (
                "POLICY_TTL_EXPIRED_AFTER_APPROVED_RECOVERY"
                if recovery_predecessor is not None
                else "POLICY_TTL_EXPIRED_BEFORE_BUDGET"
            ),
            "expired_at": now.isoformat(),
        }
        if recovery_predecessor is not None:
            expiration_payload["approved_recovery"] = recovery_predecessor
        effective.execute(
            EXECUTION_CANARY_RISK_POLICIES_TABLE.update()
            .where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id == active["id"],
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.status == "ACTIVE",
            )
            .values(
                status="EXPIRED",
                terminated_at=now,
                termination_digest=canonical_execution_digest(expiration_payload),
            )
        )
    probe_receipt = (
        effective.execute(
            select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
                EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id == probe_receipt_id
            )
        )
        .mappings()
        .one_or_none()
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
                EXECUTION_ATTESTATIONS_TABLE.c.id
                == probe_receipt["execution_attestation_id"]
            )
        )
        .mappings()
        .one_or_none()
        if probe_receipt is not None
        else None
    )
    deployment = (
        effective.execute(
            select(DEPLOYMENTS_TABLE).where(
                DEPLOYMENTS_TABLE.c.id == probe_receipt["deployment_id"]
            )
        )
        .mappings()
        .one_or_none()
        if probe_receipt is not None
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
        or probe_receipt is None
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
    strategy_leverage_cap = extract_strategy_leverage_cap(source)
    facts = _validated_probe_receipt(
        probe_receipt,
        attestation=attestation,
        deployment=deployment,
        now=now,
        strategy_max_leverage=strategy_leverage_cap,
    )
    if (
        probe_receipt["deployment_id"] != deployment["id"]
        or deployment["deployment_approval_id"] != deployment_approval_id
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_PROBE_LINEAGE",
            "probe receipt must belong to the exact approved deployment",
        )

    policy_payload = {
        "contract": "canonical-v13-canary-risk-policy-v1",
        "request_digest": request_digest,
        "qualification_decision_id": str(qualification_decision_id),
        "qualification_decision_digest": decision["decision_digest"],
        "probe_receipt_id": str(probe_receipt_id),
        "probe_receipt_digest": probe_receipt["receipt_digest"],
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
        "limit_price": _decimal_text(facts["limit_price"]),
        "maximum_buy_contracts": _decimal_text(facts["maximum_buy_contracts"]),
        "max_notional": _decimal_text(facts["minimum_notional"]),
        "strategy_max_leverage": _decimal_text(strategy_leverage_cap),
        "exchange_max_leverage": _decimal_text(facts["exchange_max_leverage"]),
        "effective_leverage": _decimal_text(facts["effective_leverage"]),
        "metadata_receipt_digest": facts["metadata_receipt_digest"],
        "mark_price_receipt_digest": facts["mark_price_receipt_digest"],
        "attestation_digest": facts["attestation_digest"],
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
            execution_attestation_id=probe_receipt["execution_attestation_id"],
            probe_receipt_id=probe_receipt_id,
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
            limit_price=facts["limit_price"],
            maximum_buy_contracts=facts["maximum_buy_contracts"],
            max_notional=facts["minimum_notional"],
            strategy_max_leverage=strategy_leverage_cap,
            exchange_max_leverage=facts["exchange_max_leverage"],
            effective_leverage=facts["effective_leverage"],
            metadata_receipt_digest=facts["metadata_receipt_digest"],
            mark_price_receipt_digest=facts["mark_price_receipt_digest"],
            attestation_digest=facts["attestation_digest"],
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
    repeat_noop = policy["status"] == "TERMINATED"
    if policy["status"] not in {"ACTIVE", "TERMINATED"}:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_NOT_ACTIVE", policy["status"]
        )
    if actor_identity != policy["actor_identity"]:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_TERMINATION_ACTOR",
            "the exact policy owner must terminate or replay termination",
        )
    budget = (
        effective.execute(
            select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE).where(
                getattr(
                    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c,
                    "execution_canary_risk_policy_id",
                )
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
    intent = (
        effective.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.id == risk["trade_intent_id"]
            )
        )
        .mappings()
        .one_or_none()
        if risk is not None
        else None
    )
    fills = (
        effective.execute(
            select(FILLS_TABLE).where(FILLS_TABLE.c.order_id == order["id"])
        )
        .mappings()
        .all()
        if order is not None
        else []
    )
    ledgers = (
        effective.execute(
            select(LEDGER_ENTRIES_TABLE).where(
                LEDGER_ENTRIES_TABLE.c.fill_id.in_([fill["id"] for fill in fills])
            )
        )
        .mappings()
        .all()
        if fills
        else []
    )
    items = (
        effective.execute(
            select(RECONCILIATION_ITEMS_TABLE).where(
                RECONCILIATION_ITEMS_TABLE.c.order_id == order["id"]
            )
        )
        .mappings()
        .all()
        if order is not None
        else []
    )
    run_ids = [item["reconciliation_run_id"] for item in items]
    runs = (
        effective.execute(
            select(RECONCILIATION_RUNS_TABLE).where(
                RECONCILIATION_RUNS_TABLE.c.id.in_(run_ids)
            )
        )
        .mappings()
        .all()
        if run_ids
        else []
    )
    exact_fill_evidence: list[dict[str, str]] = []
    requested_size = Decimal(0)
    if intent is not None and isinstance(intent["intent_json"], Mapping):
        exchange_body = intent["intent_json"].get("exchange_body")
        if isinstance(exchange_body, Mapping):
            try:
                requested_size = Decimal(str(exchange_body.get("sz")))
            except (InvalidOperation, TypeError, ValueError):
                requested_size = Decimal(0)
    for fill in fills:
        payload = fill["fill_json"]
        matching_ledgers = [row for row in ledgers if row["fill_id"] == fill["id"]]
        matching_items = [row for row in items if row["fill_id"] == fill["id"]]
        if (
            not isinstance(payload, Mapping)
            or payload.get("evidence_class") != "PRODUCTION_OKX_DEMO"
            or payload.get("allow_real_funds") is not False
            or payload.get("exchange_order_id") != order["exchange_order_id"]
            or payload.get("exchange_fill_id") != fill["exchange_fill_id"]
            or payload.get("instrument") != policy["instrument"]
            or payload.get("side") != "buy"
            or payload.get("position_side") != "long"
            or fill["receipt_digest"]
            != canonical_execution_digest(
                {
                    "contract": "canonical-v13-okx-demo-fill-v1",
                    "order_id": str(order["id"]),
                    "order_receipt_digest": order["receipt_digest"],
                    "fill": dict(payload),
                }
            )
            or len(matching_ledgers) != 1
            or len(matching_items) != 1
        ):
            continue
        ledger = matching_ledgers[0]
        item = matching_items[0]
        run = next(
            (
                row
                for row in runs
                if row["id"] == item["reconciliation_run_id"]
            ),
            None,
        )
        try:
            fill_size = Decimal(str(payload["size"]))
        except (KeyError, InvalidOperation, TypeError, ValueError):
            continue
        ledger_payload = {
            "contract": "canonical-v13-okx-demo-ledger-entry-v1",
            "fill_id": str(fill["id"]),
            "fill_receipt_digest": fill["receipt_digest"],
            "entry_key": ledger["entry_key"],
            "asset": ledger["asset"],
            "amount": _decimal_text(fill_size),
            "entry_type": ledger["entry_type"],
            "evidence_class": "PRODUCTION_OKX_DEMO",
            "allow_real_funds": False,
        }
        if (
            fill_size <= 0
            or ledger["entry_key"]
            != f"okx-demo-fill:{fill['exchange_fill_id']}:long-contracts"
            or ledger["asset"] != policy["instrument"]
            or not _persisted_decimal_equal(effective, ledger["amount"], fill_size)
            or ledger["entry_type"] != "OKX_DEMO_LONG_FILL_CONTRACTS"
            or ledger["entry_digest"] != canonical_execution_digest(ledger_payload)
            or item["ledger_entry_id"] != ledger["id"]
            or item["status"] != "MATCHED"
            or item["item_type"] != "OKX_DEMO_ORDER_FILL_LEDGER_CHAIN"
            or item["evidence_digest"]
            != canonical_execution_digest(dict(item["evidence_json"]))
            or run is None
            or run["status"] != "SUCCEEDED"
            or run["receipt_digest"]
            != canonical_execution_digest(
                {"run_id": str(run["id"]), "scope_digest": run["scope_digest"]}
            )
        ):
            continue
        exact_fill_evidence.append(
            {
                "fill_id": str(fill["id"]),
                "fill_receipt_digest": fill["receipt_digest"],
                "ledger_entry_id": str(ledger["id"]),
                "ledger_entry_digest": ledger["entry_digest"],
                "reconciliation_run_id": str(run["id"]),
                "reconciliation_receipt_digest": run["receipt_digest"],
                "size": _decimal_text(fill_size),
            }
        )
    exact_fill_evidence.sort(key=lambda value: value["fill_id"])
    if (
        budget is None
        or len(reservations) != 1
        or risk is None
        or risk["status"] != "RISK_ACCEPTED"
        or order is None
        or order["exchange_order_id"] is None
        or order["receipt_digest"] is None
        or intent is None
        or not fills
        or len(exact_fill_evidence) != len(fills)
        or len(ledgers) != len(fills)
        or len(items) != len(fills)
        or len(runs) != len(fills)
        or len(set(run_ids)) != len(fills)
        or reconciliation_run_id not in set(run_ids)
        or sum(
            (Decimal(value["size"]) for value in exact_fill_evidence), Decimal(0)
        )
        != requested_size
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_TERMINATION_LINEAGE",
            "one accepted order must finish fill/ledger/reconciliation "
            "before termination",
        )
    terminated_at = (
        _persisted_utc(policy["terminated_at"]) if repeat_noop else now
    )
    payload = {
        "contract": "canonical-v13-canary-risk-policy-termination-v2",
        "policy_id": str(policy_id),
        "policy_receipt_digest": policy["receipt_digest"],
        "authorization_digest": budget["authorization_digest"],
        "reservation_digest": reservations[0]["reservation_digest"],
        "order_id": str(order["id"]),
        "order_receipt_digest": order["receipt_digest"],
        "complete_fill_ledger_reconciliation": exact_fill_evidence,
        "actor_identity": actor_identity,
        "terminated_at": terminated_at.isoformat(),
    }
    termination_digest = canonical_execution_digest(payload)
    if repeat_noop:
        if policy["termination_digest"] != termination_digest:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CANARY_POLICY_TERMINATION_REPLAY_DRIFT",
                "persisted terminal evidence no longer matches the exact chain",
            )
        return CanaryRiskPolicyTerminationResult(
            policy_id, termination_digest, terminated_at, True
        )
    effective.execute(
        EXECUTION_CANARY_RISK_POLICIES_TABLE.update()
        .where(
            EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id == policy_id,
            EXECUTION_CANARY_RISK_POLICIES_TABLE.c.status == "ACTIVE",
        )
        .values(
            status="TERMINATED",
            terminated_at=terminated_at,
            termination_digest=termination_digest,
        )
    )
    return CanaryRiskPolicyTerminationResult(
        policy_id, termination_digest, terminated_at, False
    )


def validate_terminated_canary_risk_policy(
    connection: Connection, *, policy_id: UUID
) -> CanaryRiskPolicyTerminationResult:
    """Recompute a terminal policy from persisted lineage without accepting facts."""

    effective = require_canonical_execution(connection)
    policy = (
        effective.execute(
            select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id == policy_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if policy is None or policy["status"] != "TERMINATED":
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_TERMINATION_UNSET", str(policy_id)
        )
    budget = (
        effective.execute(
            select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE).where(
                getattr(
                    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c,
                    "execution_canary_risk_policy_id",
                )
                == policy_id
            )
        )
        .mappings()
        .one_or_none()
    )
    reservation = (
        effective.execute(
            select(EXECUTION_RISK_RESERVATIONS_TABLE).where(
                EXECUTION_RISK_RESERVATIONS_TABLE.c.risk_budget_authorization_id
                == budget["id"],
                EXECUTION_RISK_RESERVATIONS_TABLE.c.status == "RISK_ACCEPTED",
            )
        )
        .mappings()
        .one_or_none()
        if budget is not None
        else None
    )
    risk = (
        effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.trade_intent_id
                == reservation["trade_intent_id"]
            )
        )
        .mappings()
        .one_or_none()
        if reservation is not None
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
    anchor = (
        effective.execute(
            select(RECONCILIATION_ITEMS_TABLE.c.reconciliation_run_id)
            .where(RECONCILIATION_ITEMS_TABLE.c.order_id == order["id"])
            .order_by(RECONCILIATION_ITEMS_TABLE.c.reconciliation_run_id)
            .limit(1)
        ).scalar_one_or_none()
        if order is not None
        else None
    )
    if anchor is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CANARY_POLICY_TERMINATION_LINEAGE",
            "terminal reconciliation anchor is unavailable",
        )
    return terminate_canary_risk_policy(
        effective,
        policy_id=policy_id,
        reconciliation_run_id=anchor,
        actor_identity=policy["actor_identity"],
        evaluated_at=_persisted_utc(policy["terminated_at"]),
    )


__all__ = [
    "INSTRUMENT",
    "MARK_MAXIMUM_AGE",
    "POLICY_TTL",
    "extract_strategy_leverage_cap",
    "CanaryProbeReceiptResult",
    "CanaryRiskPolicyResult",
    "CanaryRiskPolicyTerminationResult",
    "authorize_canary_risk_policy",
    "persist_canary_probe_receipt",
    "terminate_canary_risk_policy",
    "validate_terminated_canary_risk_policy",
    "validate_persisted_canary_probe_receipt",
]
