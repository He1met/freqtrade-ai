"""Independent per-signal execution grants for bounded continuous OKX_DEMO.

The immutable ``risk_decisions`` row is the canonical execution grant.  It is
separate from the one-shot Phase 9 canary policy/budget and can be created only
for one exact natural signal (open) or its exact reconciled position (exit).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, func, select

from app.canonical_v13.execution_common import (
    CanonicalExecutionChainBlocked,
    canonical_execution_digest,
    lock_execution_boundary,
    require_canonical_execution,
)
from app.canonical_v13.models import (
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    FILLS_TABLE,
    LEDGER_ENTRIES_TABLE,
    ORDERS_TABLE,
    ORDER_WRITER_LEASES_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RECONCILIATION_ITEMS_TABLE,
    RESEARCH_TARGETS_TABLE,
    RISK_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    SIGNALS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_VERSIONS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.phase9_canary_policy import (
    extract_strategy_leverage_cap,
    validate_persisted_canary_probe_receipt,
)
from app.canonical_v13.phase9_okx_demo import (
    RedactedOkxDemoExitGuard,
    redacted_exit_guard_payload,
)
from app.canonical_v13.risk_service import (
    INTENT_MODE_CONTINUOUS_OPEN,
    INTENT_MODE_POSITION_EXIT,
)


INSTRUMENT = "BTC-USDT-SWAP"
OPEN_GRANT_TTL = timedelta(seconds=45)
EXIT_GRANT_TTL = timedelta(seconds=15)
MAXIMUM_SIGNAL_AGE = timedelta(minutes=20)


@dataclass(frozen=True)
class ContinuousExecutionGrantResult:
    trade_intent_id: UUID
    risk_decision_id: UUID
    decision_mode: str
    action: str
    grant_digest: str
    expires_at: datetime
    repeat_noop: bool


def _utc(value: object, *, field: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_TIME", f"{field} must be aware UTC"
        )
    return value.astimezone(timezone.utc)


def _persisted_utc(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return _utc(value, field=field)


def _decimal(value: object, *, field: str, positive: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        result = Decimal("NaN")
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_NUMBER", f"{field} is invalid"
        )
    return result


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _safe_constant_number(node: ast.AST) -> Decimal | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return Decimal(str(node.value))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left = _safe_constant_number(node.left)
        right = _safe_constant_number(node.right)
        return left * right if left is not None and right is not None else None
    return None


def extract_strategy_exit_after_seconds(source: str) -> int:
    """Extract one exact ``exposure_seconds >= <constant>`` exit threshold."""

    try:
        tree = ast.parse(source, filename="<continuous-demo-exit>", mode="exec")
    except (SyntaxError, ValueError) as exc:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_EXIT_AST", "strategy artifact is not valid Python"
        ) from exc
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    if len(classes) != 1:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_EXIT_AST", "one strategy class is required"
        )
    custom_exit = [
        node
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "custom_exit"
    ]
    thresholds: list[Decimal] = []
    for node in ast.walk(custom_exit[0]) if len(custom_exit) == 1 else ():
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.GtE)
            and len(node.comparators) == 1
            and isinstance(node.left, ast.Name)
            and node.left.id == "exposure_seconds"
        ):
            value = _safe_constant_number(node.comparators[0])
            if value is not None:
                thresholds.append(value)
    if (
        len(thresholds) != 1
        or thresholds[0] <= 0
        or thresholds[0] != thresholds[0].to_integral_value()
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_EXIT_AST",
            "custom_exit must contain one exact exposure_seconds threshold",
        )
    return int(thresholds[0])


def _artifact_lineage(
    connection: Connection, *, signal: Mapping[str, object]
) -> tuple[
    Mapping[str, object],
    Mapping[str, object],
    Mapping[str, object],
    Mapping[str, object],
    Mapping[str, object],
    Mapping[str, object],
]:
    deployment = connection.execute(
        select(DEPLOYMENTS_TABLE).where(DEPLOYMENTS_TABLE.c.id == signal["deployment_id"])
    ).mappings().one_or_none()
    approval = (
        connection.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id == deployment["deployment_approval_id"]
            )
        ).mappings().one_or_none()
        if deployment is not None
        else None
    )
    qualification = (
        connection.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id == approval["qualification_decision_id"]
            )
        ).mappings().one_or_none()
        if approval is not None
        else None
    )
    version = connection.execute(
        select(STRATEGY_VERSIONS_TABLE).where(
            STRATEGY_VERSIONS_TABLE.c.id == signal["strategy_version_id"]
        )
    ).mappings().one_or_none()
    artifact = (
        connection.execute(
            select(STRATEGY_ARTIFACTS_TABLE).where(
                STRATEGY_ARTIFACTS_TABLE.c.id == version["artifact_id"]
            )
        ).mappings().one_or_none()
        if version is not None
        else None
    )
    target = connection.execute(
        select(RESEARCH_TARGETS_TABLE).where(
            RESEARCH_TARGETS_TABLE.c.id == signal["research_target_id"]
        )
    ).mappings().one_or_none()
    if any(value is None for value in (deployment, approval, qualification, version, artifact, target)):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_LINEAGE", "deployment qualification lineage is incomplete"
        )
    assert deployment is not None and approval is not None and qualification is not None
    assert version is not None and artifact is not None and target is not None
    evidence = signal["signal_json"]
    source = str(artifact["normalized_content"])
    if (
        deployment["status"] != "ACTIVE"
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or approval["status"] != "APPROVED"
        or qualification["status"] != "QUALIFIED"
        or deployment["strategy_version_id"] != version["id"]
        or approval["strategy_version_id"] != version["id"]
        or qualification["strategy_version_id"] != version["id"]
        or qualification["research_target_id"] != target["id"]
        or signal["configuration_bundle_id"] != deployment["configuration_bundle_id"]
        or signal["configuration_bundle_digest"] != deployment["configuration_bundle_digest"]
        or signal["market_snapshot_id"] != deployment["market_snapshot_id"]
        or signal["market_snapshot_digest"] != deployment["market_snapshot_digest"]
        or qualification["configuration_bundle_id"] != deployment["configuration_bundle_id"]
        or qualification["configuration_bundle_digest"] != deployment["configuration_bundle_digest"]
        or qualification["market_snapshot_id"] != deployment["market_snapshot_id"]
        or qualification["market_snapshot_digest"] != deployment["market_snapshot_digest"]
        or not isinstance(evidence, Mapping)
        or evidence.get("qualification_decision_id") != str(qualification["id"])
        or evidence.get("qualification_decision_digest") != qualification["decision_digest"]
        or evidence.get("deployment_id") != str(deployment["id"])
        or evidence.get("deployment_capability_digest") != deployment["capability_digest"]
        or evidence.get("strategy_version_id") != str(version["id"])
        or evidence.get("research_target_id") != str(target["id"])
        or target["instrument"] != INSTRUMENT
        or target["timeframe"] != "15m"
        or target["data_kind"] != "futures"
        or sha256(source.encode("utf-8")).hexdigest() != artifact["content_digest"]
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_LINEAGE_DRIFT", "exact qualified Demo lineage drifted"
        )
    return deployment, approval, qualification, version, artifact, target


def _replay(
    connection: Connection, *, signal_id: UUID, mode: str
) -> ContinuousExecutionGrantResult | None:
    intent = connection.execute(
        select(TRADE_INTENTS_TABLE).where(
            TRADE_INTENTS_TABLE.c.signal_id == signal_id,
            TRADE_INTENTS_TABLE.c.intent_mode == mode,
        )
    ).mappings().one_or_none()
    if intent is None:
        return None
    decision = connection.execute(
        select(RISK_DECISIONS_TABLE).where(
            RISK_DECISIONS_TABLE.c.trade_intent_id == intent["id"],
            RISK_DECISIONS_TABLE.c.decision_mode == mode,
        )
    ).mappings().one_or_none()
    payload = decision["decision_json"] if decision is not None else None
    if (
        intent["status"] != "INTENT_ACCEPTED"
        or intent["intent_json"].get("intent_mode") != mode
        or intent["intent_digest"] != canonical_execution_digest(intent["intent_json"])
        or decision is None
        or decision["status"] != "RISK_ACCEPTED"
        or not isinstance(payload, Mapping)
        or payload.get("decision_mode") != mode
        or payload.get("execution_authorized") is not True
        or payload.get("allow_real_funds") is not False
        or decision["decision_digest"] != canonical_execution_digest(payload)
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_REPLAY_INCOMPLETE", "intent/grant pair drifted"
        )
    return ContinuousExecutionGrantResult(
        intent["id"],
        decision["id"],
        mode,
        str(payload["action"]),
        decision["decision_digest"],
        _utc(payload["expires_at"], field="grant.expires_at"),
        True,
    )


def _insert_pair(
    connection: Connection,
    *,
    signal_id: UUID,
    mode: str,
    intent_payload: Mapping[str, object],
    grant_payload: Mapping[str, object],
    now: datetime,
) -> ContinuousExecutionGrantResult:
    intent = dict(intent_payload)
    intent["intent_mode"] = mode
    intent_digest = canonical_execution_digest(intent)
    intent_id = uuid4()
    connection.execute(
        TRADE_INTENTS_TABLE.insert().values(
            id=intent_id,
            signal_id=signal_id,
            intent_mode=mode,
            status="INTENT_ACCEPTED",
            intent_json=intent,
            intent_digest=intent_digest,
            created_at=now,
        )
    )
    grant = {
        **dict(grant_payload),
        "trade_intent_id": str(intent_id),
        "intent_digest": intent_digest,
        "decision_mode": mode,
        "order_submission_enabled": True,
        "execution_authorized": True,
        "status": "RISK_ACCEPTED",
        "allow_real_funds": False,
    }
    grant_digest = canonical_execution_digest(grant)
    decision_id = uuid4()
    connection.execute(
        RISK_DECISIONS_TABLE.insert().values(
            id=decision_id,
            trade_intent_id=intent_id,
            decision_mode=mode,
            status="RISK_ACCEPTED",
            decision_json=grant,
            decision_digest=grant_digest,
            created_at=now,
        )
    )
    return ContinuousExecutionGrantResult(
        intent_id,
        decision_id,
        mode,
        str(grant["action"]),
        grant_digest,
        _utc(grant["expires_at"], field="grant.expires_at"),
        False,
    )


def _require_no_incomplete_execution(connection: Connection) -> None:
    in_flight = int(
        connection.execute(
            select(func.count()).select_from(ORDERS_TABLE).where(
                ORDERS_TABLE.c.status.in_(("SUBMITTED", "DISPATCHING", "PARTIAL"))
            )
        ).scalar_one()
    )
    fills = int(connection.execute(select(func.count()).select_from(FILLS_TABLE)).scalar_one())
    ledgers = int(
        connection.execute(select(func.count()).select_from(LEDGER_ENTRIES_TABLE)).scalar_one()
    )
    reconciled = int(
        connection.execute(
            select(func.count()).select_from(RECONCILIATION_ITEMS_TABLE).where(
                RECONCILIATION_ITEMS_TABLE.c.status == "MATCHED"
            )
        ).scalar_one()
    )
    accepted_order_ids = set(
        connection.execute(
            select(ORDERS_TABLE.c.id).where(
                ORDERS_TABLE.c.status.in_(("ACCEPTED", "FILLED"))
            )
        ).scalars()
    )
    filled_order_ids = set(
        connection.execute(select(FILLS_TABLE.c.order_id).distinct()).scalars()
    )
    mismatched = int(
        connection.execute(
            select(func.count()).select_from(RECONCILIATION_ITEMS_TABLE).where(
                RECONCILIATION_ITEMS_TABLE.c.status != "MATCHED"
            )
        ).scalar_one()
    )
    lease = connection.execute(
        select(ORDER_WRITER_LEASES_TABLE).where(
            ORDER_WRITER_LEASES_TABLE.c.execution_target == "OKX_DEMO"
        )
    ).mappings().one_or_none()
    now = datetime.now(timezone.utc)
    active_lease = bool(
        lease is not None
        and lease["status"] == "ACTIVE"
        and _persisted_utc(lease["expires_at"], field="lease.expires_at") > now
    )
    if (
        in_flight
        or accepted_order_ids.difference(filled_order_ids)
        or fills != ledgers
        or fills != reconciled
        or mismatched
        or active_lease
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_EXECUTION_DOMAIN_DRIFT",
            "in-flight, unreconciled, mismatched, or leased execution evidence exists",
        )


def grant_continuous_open(
    connection: Connection,
    *,
    signal_id: UUID,
    probe_receipt_id: UUID,
    evaluated_at: datetime | None = None,
) -> ContinuousExecutionGrantResult:
    """Grant one minimum-size opening for one fresh closed-candle natural signal."""

    effective = require_canonical_execution(connection)
    now = _utc(evaluated_at or datetime.now(timezone.utc), field="evaluated_at")
    lock_execution_boundary(effective, key=f"continuous-open:{signal_id}")
    replay = _replay(effective, signal_id=signal_id, mode=INTENT_MODE_CONTINUOUS_OPEN)
    if replay is not None:
        return replay
    signal = effective.execute(
        select(SIGNALS_TABLE).where(SIGNALS_TABLE.c.id == signal_id)
    ).mappings().one_or_none()
    if signal is None:
        raise CanonicalExecutionChainBlocked("BLOCKED_CONTINUOUS_SIGNAL_UNSET", str(signal_id))
    deployment, approval, qualification, version, artifact, target = _artifact_lineage(
        effective, signal=signal
    )
    evidence = signal["signal_json"]
    evaluation = evidence.get("evaluation") if isinstance(evidence, Mapping) else None
    signal_at = _utc(evidence.get("evaluated_at"), field="signal.evaluated_at")
    runtime = effective.execute(
        select(RUNTIME_INSTANCES_TABLE).where(
            RUNTIME_INSTANCES_TABLE.c.id == signal["runtime_instance_id"]
        )
    ).mappings().one_or_none()
    if (
        signal["source_kind"] != "NATURAL_STRATEGY_SIGNAL"
        or signal["acceptance_trigger_id"] is not None
        or evidence.get("evidence_class") != "PRODUCTION_OKX_DEMO"
        or evidence.get("natural_signal") is not True
        or evidence.get("allow_real_funds") is not False
        or evidence.get("instrument") != INSTRUMENT
        or not isinstance(evaluation, Mapping)
        or evaluation.get("direction") != "LONG"
        or evaluation.get("closed_candle") is not True
        or evaluation.get("artifact_digest") != artifact["content_digest"]
        or signal_at > now
        or now - signal_at > MAXIMUM_SIGNAL_AGE
        or runtime is None
        or runtime["deployment_id"] != deployment["id"]
        or runtime["status"] != "HEALTHY"
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_NATURAL_SIGNAL",
            "one fresh closed 15m natural LONG signal is required",
        )
    _require_no_incomplete_execution(effective)
    net_contracts = Decimal(
        str(
            effective.execute(
                select(func.coalesce(func.sum(LEDGER_ENTRIES_TABLE.c.amount), 0)).where(
                    LEDGER_ENTRIES_TABLE.c.asset == INSTRUMENT
                )
            ).scalar_one()
        )
    )
    if net_contracts != 0:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_NOT_FLAT", "canonical contract ledger is not flat"
        )
    leverage_cap = extract_strategy_leverage_cap(str(artifact["normalized_content"]))
    facts = validate_persisted_canary_probe_receipt(
        effective,
        probe_receipt_id=probe_receipt_id,
        evaluated_at=now,
        strategy_max_leverage=leverage_cap,
    )
    probe = effective.execute(
        select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
            EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id == probe_receipt_id
        )
    ).mappings().one()
    if probe["deployment_id"] != deployment["id"]:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_PROBE_LINEAGE", "fresh flat probe belongs to another deployment"
        )
    size = _decimal(facts["minimum_size"], field="minimum_size")
    price = _decimal(facts["limit_price"], field="limit_price")
    contract_value = _decimal(facts["contract_value"], field="contract_value")
    notional = size * contract_value * _decimal(facts["mark_price"], field="mark_price")
    expires_at = min(
        _persisted_utc(probe["expires_at"], field="probe.expires_at"),
        now + OPEN_GRANT_TTL,
    )
    intent_payload = {
        "contract": "canonical-v13-continuous-demo-intent-v1",
        "action": "OPEN_LONG",
        "execution_target": "OKX_DEMO",
        "source_kind": "NATURAL_STRATEGY_SIGNAL",
        "signal_digest": signal["signal_digest"],
        "instrument": INSTRUMENT,
        "notional": _text(notional),
        "probe_receipt_id": str(probe_receipt_id),
        "probe_receipt_digest": probe["receipt_digest"],
        "exchange_body": {
            "instId": INSTRUMENT,
            "tdMode": "isolated",
            "side": "buy",
            "posSide": "long",
            "ordType": "market",
            "sz": _text(size),
        },
        "allow_real_funds": False,
    }
    grant_payload = {
        "contract": "canonical-v13-continuous-execution-grant-v1",
        "action": "OPEN_LONG",
        "signal_id": str(signal_id),
        "signal_digest": signal["signal_digest"],
        "qualification_decision_id": str(qualification["id"]),
        "qualification_decision_digest": qualification["decision_digest"],
        "deployment_approval_id": str(approval["id"]),
        "deployment_approval_digest": approval["approval_digest"],
        "deployment_id": str(deployment["id"]),
        "deployment_capability_digest": deployment["capability_digest"],
        "strategy_version_id": str(version["id"]),
        "strategy_artifact_digest": artifact["content_digest"],
        "research_target_id": str(target["id"]),
        "configuration_bundle_id": str(deployment["configuration_bundle_id"]),
        "configuration_bundle_digest": deployment["configuration_bundle_digest"],
        "market_snapshot_id": str(deployment["market_snapshot_id"]),
        "market_snapshot_digest": deployment["market_snapshot_digest"],
        "probe_receipt_id": str(probe_receipt_id),
        "probe_receipt_digest": probe["receipt_digest"],
        "execution_attestation_id": str(probe["execution_attestation_id"]),
        "minimum_contract_size": _text(size),
        "contract_value": _text(contract_value),
        "reference_price": _text(_decimal(facts["mark_price"], field="mark_price")),
        "limit_price": _text(price),
        "maximum_buy_contracts": _text(
            _decimal(facts["maximum_buy_contracts"], field="maximum_buy_contracts")
        ),
        "max_notional": _text(notional),
        "strategy_max_leverage": _text(leverage_cap),
        "effective_leverage": _text(
            _decimal(facts["effective_leverage"], field="effective_leverage")
        ),
        "max_order_count": 1,
        "granted_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    return _insert_pair(
        effective,
        signal_id=signal_id,
        mode=INTENT_MODE_CONTINUOUS_OPEN,
        intent_payload=intent_payload,
        grant_payload=grant_payload,
        now=now,
    )


def _fill_time(payload: Mapping[str, object]) -> datetime:
    value = payload.get("timestamp")
    if isinstance(value, str) and value.isdigit():
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    return _utc(value, field="fill.timestamp")


def grant_position_exit(
    connection: Connection,
    *,
    entry_order_id: UUID,
    attestation_id: UUID,
    guard: RedactedOkxDemoExitGuard,
    evaluated_at: datetime | None = None,
) -> ContinuousExecutionGrantResult:
    """Grant an exact market sell/long only after the artifact's exit threshold."""

    effective = require_canonical_execution(connection)
    now = _utc(evaluated_at or datetime.now(timezone.utc), field="evaluated_at")
    if not isinstance(guard, RedactedOkxDemoExitGuard):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_EXIT_GUARD", "sealed typed exit guard is required"
        )
    entry_order = effective.execute(
        select(ORDERS_TABLE).where(ORDERS_TABLE.c.id == entry_order_id)
    ).mappings().one_or_none()
    entry_risk = (
        effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.id == entry_order["risk_decision_id"]
            )
        ).mappings().one_or_none()
        if entry_order is not None
        else None
    )
    entry_intent = (
        effective.execute(
            select(TRADE_INTENTS_TABLE).where(
                TRADE_INTENTS_TABLE.c.id == entry_risk["trade_intent_id"]
            )
        ).mappings().one_or_none()
        if entry_risk is not None
        else None
    )
    signal = (
        effective.execute(
            select(SIGNALS_TABLE).where(SIGNALS_TABLE.c.id == entry_intent["signal_id"])
        ).mappings().one_or_none()
        if entry_intent is not None
        else None
    )
    if signal is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_EXIT_LINEAGE", "entry order lineage is incomplete"
        )
    lock_execution_boundary(effective, key=f"continuous-exit:{signal['id']}")
    replay = _replay(effective, signal_id=signal["id"], mode=INTENT_MODE_POSITION_EXIT)
    if replay is not None:
        return replay
    deployment, approval, qualification, version, artifact, target = _artifact_lineage(
        effective, signal=signal
    )
    _require_no_incomplete_execution(effective)
    entry_body = entry_intent["intent_json"].get("exchange_body")
    fills = effective.execute(
        select(FILLS_TABLE).where(FILLS_TABLE.c.order_id == entry_order_id).order_by(FILLS_TABLE.c.created_at)
    ).mappings().all()
    fill_ids = [row["id"] for row in fills]
    ledgers = (
        effective.execute(
            select(LEDGER_ENTRIES_TABLE).where(LEDGER_ENTRIES_TABLE.c.fill_id.in_(fill_ids))
        ).mappings().all()
        if fill_ids
        else []
    )
    reconciliations = effective.execute(
        select(RECONCILIATION_ITEMS_TABLE).where(
            RECONCILIATION_ITEMS_TABLE.c.order_id == entry_order_id
        )
    ).mappings().all()
    if (
        entry_order is None
        or entry_order["status"] not in {"ACCEPTED", "FILLED"}
        or entry_order["demo_only"] is not True
        or entry_order["allow_real_funds"] is not False
        or entry_risk is None
        or entry_risk["status"] != "RISK_ACCEPTED"
        or entry_intent is None
        or entry_intent["status"] != "INTENT_ACCEPTED"
        or not isinstance(entry_body, Mapping)
        or entry_body.get("instId") != INSTRUMENT
        or entry_body.get("side") != "buy"
        or entry_body.get("posSide") != "long"
        or not fills
        or len(fills) != len(ledgers)
        or len(fills) != len(reconciliations)
        or any(row["status"] != "MATCHED" for row in reconciliations)
        or any(
            row["fill_json"].get("side") != "buy"
            or row["fill_json"].get("position_side") != "long"
            or row["fill_json"].get("allow_real_funds") is not False
            for row in fills
        )
        or any(row["amount"] <= 0 for row in ledgers)
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_EXIT_ENTRY",
            "one reconciled canonical Demo long entry is required",
        )
    hold_seconds = extract_strategy_exit_after_seconds(str(artifact["normalized_content"]))
    latest_fill_at = max(_fill_time(row["fill_json"]) for row in fills)
    if now < latest_fill_at + timedelta(seconds=hold_seconds):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_EXIT_NOT_DUE", "qualified strategy exit threshold is not met"
        )
    attestation = effective.execute(
        select(EXECUTION_ATTESTATIONS_TABLE).where(
            EXECUTION_ATTESTATIONS_TABLE.c.id == attestation_id
        )
    ).mappings().one_or_none()
    guard_payload = redacted_exit_guard_payload(guard)
    if (
        attestation is None
        or attestation["deployment_id"] != deployment["id"]
        or attestation["status"] != "READY"
        or attestation["execution_target"] != "OKX_DEMO"
        or attestation["instrument"] != INSTRUMENT
        or attestation["account_fingerprint_digest"] != guard.account_fingerprint_digest
        or attestation["credential_generation_digest"] != guard.credential_generation_digest
        or attestation["permissions_json"] != {"read": True, "trade": True, "withdraw": False}
        or _persisted_utc(
            attestation["observed_at"], field="attestation.observed_at"
        )
        > guard.observed_at
        or _persisted_utc(attestation["expires_at"], field="attestation.expires_at")
        < guard.expires_at
        or guard.execution_target != "OKX_DEMO"
        or guard.instrument != INSTRUMENT
        or guard.simulated_trading is not True
        or guard.allow_real_funds is not False
        or guard.pending_order_count != 0
        or guard.short_contracts != "0"
        or guard.active_position_count != 1
        or guard.guard_digest != canonical_execution_digest(guard_payload)
        or guard.observed_at > now
        or guard.expires_at <= now
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_EXIT_GUARD_DRIFT", "fresh exit guard or attestation drifted"
        )
    size = _decimal(guard.close_contracts, field="close_contracts")
    ledger_net = Decimal(
        str(
            effective.execute(
                select(func.coalesce(func.sum(LEDGER_ENTRIES_TABLE.c.amount), 0)).where(
                    LEDGER_ENTRIES_TABLE.c.asset == INSTRUMENT
                )
            ).scalar_one()
        )
    )
    if ledger_net != size or Decimal(guard.long_contracts) != size:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_EXIT_LEDGER_DRIFT",
            "private position and canonical contract ledger differ",
        )
    reference = _decimal(guard.reference_price, field="reference_price")
    contract_value = _decimal(guard.contract_value, field="contract_value")
    leverage_cap = extract_strategy_leverage_cap(str(artifact["normalized_content"]))
    effective_leverage = _decimal(
        guard.effective_leverage, field="effective_leverage"
    )
    if effective_leverage > leverage_cap:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_EXIT_LEVERAGE",
            "current position leverage exceeds the qualified artifact cap",
        )
    notional = size * contract_value * reference
    expires_at = min(guard.expires_at, now + EXIT_GRANT_TTL)
    intent_payload = {
        "contract": "canonical-v13-continuous-demo-intent-v1",
        "action": "CLOSE_LONG",
        "execution_target": "OKX_DEMO",
        "source_entry_order_id": str(entry_order_id),
        "source_entry_order_receipt_digest": entry_order["receipt_digest"],
        "source_fill_receipt_digests": [row["receipt_digest"] for row in fills],
        "source_ledger_entry_digests": [row["entry_digest"] for row in ledgers],
        "signal_digest": signal["signal_digest"],
        "instrument": INSTRUMENT,
        "notional": _text(notional),
        "exit_after_seconds": hold_seconds,
        "exchange_body": {
            "instId": INSTRUMENT,
            "tdMode": "isolated",
            "side": "sell",
            "posSide": "long",
            "ordType": "market",
            "sz": _text(size),
        },
        "allow_real_funds": False,
    }
    grant_payload = {
        "contract": "canonical-v13-continuous-execution-grant-v1",
        "action": "CLOSE_LONG",
        "signal_id": str(signal["id"]),
        "signal_digest": signal["signal_digest"],
        "qualification_decision_id": str(qualification["id"]),
        "qualification_decision_digest": qualification["decision_digest"],
        "deployment_approval_id": str(approval["id"]),
        "deployment_approval_digest": approval["approval_digest"],
        "deployment_id": str(deployment["id"]),
        "deployment_capability_digest": deployment["capability_digest"],
        "strategy_version_id": str(version["id"]),
        "strategy_artifact_digest": artifact["content_digest"],
        "research_target_id": str(target["id"]),
        "configuration_bundle_id": str(deployment["configuration_bundle_id"]),
        "configuration_bundle_digest": deployment["configuration_bundle_digest"],
        "market_snapshot_id": str(deployment["market_snapshot_id"]),
        "market_snapshot_digest": deployment["market_snapshot_digest"],
        "execution_attestation_id": str(attestation_id),
        "exit_guard": guard_payload,
        "exit_guard_digest": guard.guard_digest,
        "source_entry_order_id": str(entry_order_id),
        "minimum_contract_size": _text(size),
        "maximum_close_contracts": _text(size),
        "reference_price": _text(reference),
        "contract_value": _text(contract_value),
        "strategy_max_leverage": _text(leverage_cap),
        "effective_leverage": _text(effective_leverage),
        "max_order_count": 1,
        "exit_after_seconds": hold_seconds,
        "granted_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    return _insert_pair(
        effective,
        signal_id=signal["id"],
        mode=INTENT_MODE_POSITION_EXIT,
        intent_payload=intent_payload,
        grant_payload=grant_payload,
        now=now,
    )


__all__ = [
    "ContinuousExecutionGrantResult",
    "extract_strategy_exit_after_seconds",
    "grant_continuous_open",
    "grant_position_exit",
]
