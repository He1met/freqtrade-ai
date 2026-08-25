"""Server-side composition helpers for canonical Phase 9 services.

This module never discovers credentials and never performs work on import.  The
macOS operator supplies already resolved, in-memory connection factories and
ports.  Consequently LaunchAgent plists remain secret-free while tests can use
network-none fakes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Protocol
from uuid import UUID, uuid5, NAMESPACE_URL

from sqlalchemy import Connection, select

from app.canonical_v13.deployment_control import (
    confirm_production_demo_runtime_observation,
    confirm_production_demo_runtime_stop_observation,
)
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.phase9_canary_policy import (
    CanaryProbeReceiptResult,
    persist_canary_probe_receipt,
)
from app.canonical_v13.phase9_execution_authority import (
    RedactedExecutionAttestationResult,
    record_redacted_demo_attestation,
)
from app.canonical_v13.phase9_okx_demo import (
    RedactedOkxDemoDispatchGuard,
    RedactedOkxDemoOrderAbsence,
    RedactedOkxDemoProbe,
)
from app.canonical_v13.models import (
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    EXECUTION_ATTESTATIONS_TABLE,
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_CANARY_PROBE_RECEIPTS_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    EXECUTION_RISK_RESERVATIONS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    FILLS_TABLE,
    LEDGER_ENTRIES_TABLE,
    ORDERS_TABLE,
    RISK_DECISIONS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.accounting import post_production_demo_ledger_entry
from app.canonical_v13.fill_service import record_production_demo_fill
from app.canonical_v13.reconciliation import reconcile_production_demo_chain
from app.canonical_v13.order_service import CANONICAL_ORDER_WRITER_IDENTITY
from app.canonical_v13.phase9_order_writer import (
    CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
    CanaryRecoveryOrder,
    DispatchedDemoOrder,
    PreparedDemoOrder,
    dispatch_demo_order,
    prepare_demo_order,
    recover_demo_order_get_only,
    validate_canary_recovery_order,
)
from app.canonical_v13.phase9_runtime_supervisor import (
    OrderWriterCanaryAuthority,
    Phase9LaunchPlan,
    Phase9Lease,
    Phase9LifecycleReceipt,
    RuntimeWorkerSupervisorPort,
    build_production_runtime_observation,
    build_production_runtime_stop_observation,
    require_current_order_writer_canary_authority,
)
from app.canonical_v13.runtime_contract import (
    FrozenRuntimeLaunchSpec,
    build_runtime_observation_receipt,
    frozen_runtime_launch_spec_digest,
)


ConnectionFactory = Callable[[], AbstractContextManager[Connection]]


class DemoSessionPort(Protocol):
    def probe(self, *, instrument: str) -> RedactedOkxDemoProbe: ...

    def place(self, body: Mapping[str, str]) -> Mapping[str, object]: ...

    def preflight_place(self, body: Mapping[str, str]) -> None: ...

    def dispatch_guard(
        self,
        *,
        instrument: str,
        limit_price: str,
        effective_leverage: str,
        minimum_size: str,
    ) -> RedactedOkxDemoDispatchGuard: ...

    def query(
        self, *, instrument: str, client_order_id: str
    ) -> Mapping[str, object]: ...

    def prove_absent(
        self, *, instrument: str, client_order_id: str
    ) -> RedactedOkxDemoOrderAbsence: ...

    def fills(
        self, *, instrument: str, order_id: str
    ) -> tuple[Mapping[str, object], ...]: ...

    def __enter__(self) -> "DemoSessionPort": ...

    def __exit__(self, exc_type, exc, traceback) -> None: ...


DemoSessionFactory = Callable[[], DemoSessionPort]


@dataclass(frozen=True)
class RecordedCanaryProbe:
    attestation: RedactedExecutionAttestationResult
    probe_receipt: CanaryProbeReceiptResult


def record_current_canary_attestation(
    deployment_connection: Connection,
    *,
    deployment_id: UUID,
    session: DemoSessionPort,
    evaluated_at: datetime,
) -> tuple[RedactedOkxDemoProbe, RedactedExecutionAttestationResult]:
    """First committed saga step: persist only deployment-owned attestation."""

    probe = session.probe(instrument="BTC-USDT-SWAP")
    attestation = record_redacted_demo_attestation(
        deployment_connection,
        deployment_id=deployment_id,
        instrument=probe.instrument,
        account_fingerprint_digest=probe.account_fingerprint_digest,
        credential_generation_digest=probe.credential_generation_digest,
        permissions=probe.permissions,
        observed_at=probe.observed_at,
        expires_at=probe.expires_at,
        evaluated_at=evaluated_at,
    )
    return probe, attestation


def record_current_canary_probe_receipt(
    approval_connection: Connection,
    *,
    deployment_id: UUID,
    probe: RedactedOkxDemoProbe,
    attestation: RedactedExecutionAttestationResult,
    evaluated_at: datetime,
) -> CanaryProbeReceiptResult:
    """Second committed saga step after the attestation FK is visible."""

    return persist_canary_probe_receipt(
        approval_connection,
        probe=probe,
        deployment_id=deployment_id,
        execution_attestation_id=attestation.attestation_id,
        evaluated_at=evaluated_at,
    )


class CanonicalPhase9CompositionBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _positive_decimal(value: object, *, field: str) -> Decimal:
    try:
        resolved = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CanonicalPhase9CompositionBlocked(
            "BLOCKED_PHASE9_FILL_VALUE", f"{field} is not decimal"
        ) from exc
    if not resolved.is_finite() or resolved <= 0:
        raise CanonicalPhase9CompositionBlocked(
            "BLOCKED_PHASE9_FILL_VALUE", f"{field} must be finite and positive"
        )
    return resolved


def _effective(connection: Connection) -> Connection:
    return (
        connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        if connection.dialect.name == "sqlite"
        else connection
    )


class CanonicalFillWriterOperator:
    """GET exact exchange fills and persist only server-derived safe facts."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory,
        session_factory: DemoSessionFactory,
    ) -> None:
        self._connection_factory = connection_factory
        self._session_factory = session_factory

    def collect(self, *, order_id: UUID) -> tuple[UUID, ...]:
        with self._connection_factory() as connection:
            connection = _effective(connection)
            order = connection.execute(
                select(ORDERS_TABLE).where(ORDERS_TABLE.c.id == order_id)
            ).mappings().one_or_none()
            decision = (
                connection.execute(
                    select(RISK_DECISIONS_TABLE).where(
                        RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
                    )
                ).mappings().one_or_none()
                if order is not None
                else None
            )
            intent = (
                connection.execute(
                    select(TRADE_INTENTS_TABLE).where(
                        TRADE_INTENTS_TABLE.c.id == decision["trade_intent_id"]
                    )
                ).mappings().one_or_none()
                if decision is not None
                else None
            )
        decision_json = decision["decision_json"] if decision is not None else None
        intent_json = intent["intent_json"] if intent is not None else None
        exchange_body = (
            intent_json.get("exchange_body")
            if isinstance(intent_json, Mapping)
            else None
        )
        if (
            order is None
            or order["status"] not in {"ACCEPTED", "PARTIAL", "FILLED"}
            or order["demo_only"] is not True
            or order["allow_real_funds"] is not False
            or not order["exchange_order_id"]
            or not order["receipt_digest"]
            or decision is None
            or decision["status"] != "RISK_ACCEPTED"
            or not isinstance(decision_json, Mapping)
            or decision_json.get("decision_mode") != "EXECUTION"
            or decision_json.get("execution_authorized") is not True
            or intent is None
            or intent["status"] != "INTENT_ACCEPTED"
            or not isinstance(intent_json, Mapping)
            or not isinstance(exchange_body, Mapping)
            or exchange_body.get("instId") != "BTC-USDT-SWAP"
            or exchange_body.get("side") != "buy"
            or exchange_body.get("posSide") != "long"
        ):
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_FILL_ORDER", "accepted Demo order is required"
            )
        instrument = str(exchange_body["instId"])
        requested_size = _positive_decimal(exchange_body.get("sz"), field="order_size")
        with self._session_factory() as session:
            exchange_fills = session.fills(
                instrument=instrument, order_id=str(order["exchange_order_id"])
            )
        if not exchange_fills:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_FILL_UNSET", "exchange returned no exact order fill"
            )
        validated: list[tuple[str, dict[str, object]]] = []
        seen: set[str] = set()
        cumulative_size = Decimal(0)
        for raw in exchange_fills:
            exchange_fill_id = str(raw.get("fill_id") or "").strip()
            if (
                not exchange_fill_id
                or exchange_fill_id in seen
                or raw.get("inst_id") != instrument
                or str(raw.get("order_id") or "") != str(order["exchange_order_id"])
            ):
                raise CanonicalPhase9CompositionBlocked(
                    "BLOCKED_PHASE9_FILL_IDENTITY", "exchange fill identity drifted"
                )
            seen.add(exchange_fill_id)
            price = _positive_decimal(raw.get("price"), field="price")
            size = _positive_decimal(raw.get("size"), field="size")
            cumulative_size += size
            if cumulative_size > requested_size:
                raise CanonicalPhase9CompositionBlocked(
                    "BLOCKED_PHASE9_FILL_SIZE",
                    "cumulative fills exceed persisted order size",
                )
            payload = {
                "contract": "canonical-v13-okx-demo-fill-evidence-v1",
                "evidence_class": "PRODUCTION_OKX_DEMO",
                "allow_real_funds": False,
                "instrument": instrument,
                "exchange_order_id": str(order["exchange_order_id"]),
                "exchange_fill_id": exchange_fill_id,
                "bill_id": str(raw.get("bill_id") or ""),
                "price": str(price),
                "size": str(size),
                "fee": str(raw.get("fee") or "0"),
                "timestamp": str(raw.get("timestamp") or ""),
                "side": str(exchange_body["side"]),
                "position_side": str(exchange_body["posSide"]),
                "requested_size": str(requested_size),
            }
            validated.append((exchange_fill_id, payload))
        persisted: list[UUID] = []
        for exchange_fill_id, payload in validated:
            with self._connection_factory() as connection:
                connection = _effective(connection)
                persisted.append(
                    record_production_demo_fill(
                        connection,
                        order_id=order_id,
                        exchange_fill_id=exchange_fill_id,
                        fill_json=payload,
                    )
                )
        return tuple(persisted)


class CanonicalLedgerWriterOperator:
    """Post a fill-derived contract entry; callers cannot supply amount or asset."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def post(self, *, fill_id: UUID) -> UUID:
        with self._connection_factory() as connection:
            connection = _effective(connection)
            fill = connection.execute(
                select(FILLS_TABLE).where(FILLS_TABLE.c.id == fill_id)
            ).mappings().one_or_none()
            payload = fill["fill_json"] if fill is not None else None
            if (
                fill is None
                or not isinstance(payload, Mapping)
                or payload.get("evidence_class") != "PRODUCTION_OKX_DEMO"
                or payload.get("allow_real_funds") is not False
                or payload.get("side") != "buy"
                or payload.get("position_side") != "long"
            ):
                raise CanonicalPhase9CompositionBlocked(
                    "BLOCKED_PHASE9_LEDGER_FILL", "exact long Demo fill is required"
                )
            amount = _positive_decimal(payload.get("size"), field="size")
            exchange_fill_id = str(payload.get("exchange_fill_id") or "").strip()
            instrument = str(payload.get("instrument") or "").strip()
            if not exchange_fill_id or instrument != "BTC-USDT-SWAP":
                raise CanonicalPhase9CompositionBlocked(
                    "BLOCKED_PHASE9_LEDGER_FILL", "fill identity drifted"
                )
            return post_production_demo_ledger_entry(
                connection,
                fill_id=fill_id,
                entry_key=f"okx-demo-fill:{exchange_fill_id}:long-contracts",
                asset=instrument,
                amount=amount,
                entry_type="OKX_DEMO_LONG_FILL_CONTRACTS",
            )


class CanonicalReconciliationWriterOperator:
    """Reconcile persisted order/fill/ledger lineage from one order selector."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def reconcile(self, *, order_id: UUID) -> tuple[UUID, ...]:
        with self._connection_factory() as connection:
            connection = _effective(connection)
            fills = connection.execute(
                select(FILLS_TABLE).where(FILLS_TABLE.c.order_id == order_id)
            ).mappings().all()
            if not fills:
                raise CanonicalPhase9CompositionBlocked(
                    "BLOCKED_PHASE9_RECONCILIATION_FILL", "persisted fill is required"
                )
            runs: list[UUID] = []
            for fill in fills:
                ledger = connection.execute(
                    select(LEDGER_ENTRIES_TABLE).where(
                        LEDGER_ENTRIES_TABLE.c.fill_id == fill["id"]
                    )
                ).mappings().one_or_none()
                if ledger is None:
                    raise CanonicalPhase9CompositionBlocked(
                        "BLOCKED_PHASE9_RECONCILIATION_LEDGER",
                        "exact persisted ledger entry is required",
                    )
                runs.append(
                    reconcile_production_demo_chain(
                        connection,
                        order_id=order_id,
                        fill_id=fill["id"],
                        ledger_entry_id=ledger["id"],
                    )
                )
            return tuple(runs)


class RuntimeWorkerFactoryPort(Protocol):
    def build(self, plan: Phase9LaunchPlan) -> RuntimeWorkerSupervisorPort: ...


@dataclass(frozen=True)
class SupervisePorts:
    worker_port: RuntimeWorkerSupervisorPort | None
    authority_port: "DatabaseOrderWriterAuthorityVerifier | None"


class DatabaseOrderWriterAuthorityVerifier:
    """Recompute one frozen writer authority from current canonical rows."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def verify(
        self, authority: OrderWriterCanaryAuthority, *, observed_at: datetime
    ) -> bool:
        if observed_at.tzinfo is None:
            return False
        now = observed_at.astimezone(timezone.utc)
        with self._connection_factory() as connection:
            deployment = connection.execute(
                select(DEPLOYMENTS_TABLE).where(
                    DEPLOYMENTS_TABLE.c.id == authority.deployment_id
                )
            ).mappings().one_or_none()
            policy = connection.execute(
                select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                    EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id
                    == authority.execution_canary_risk_policy_id
                )
            ).mappings().one_or_none()
            attestation = connection.execute(
                select(EXECUTION_ATTESTATIONS_TABLE).where(
                    EXECUTION_ATTESTATIONS_TABLE.c.id == authority.attestation_id
                )
            ).mappings().one_or_none()
            probe = (
                connection.execute(
                    select(EXECUTION_CANARY_PROBE_RECEIPTS_TABLE).where(
                        EXECUTION_CANARY_PROBE_RECEIPTS_TABLE.c.id
                        == policy["probe_receipt_id"]
                    )
                ).mappings().one_or_none()
                if policy is not None
                else None
            )
        expires_at = attestation["expires_at"] if attestation is not None else None
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        attestation_observed_at = (
            attestation["observed_at"] if attestation is not None else None
        )
        if (
            isinstance(attestation_observed_at, datetime)
            and attestation_observed_at.tzinfo is None
        ):
            attestation_observed_at = attestation_observed_at.replace(
                tzinfo=timezone.utc
            )
        policy_expires_at = policy["expires_at"] if policy is not None else None
        if isinstance(policy_expires_at, datetime) and policy_expires_at.tzinfo is None:
            policy_expires_at = policy_expires_at.replace(tzinfo=timezone.utc)
        policy_accepted_at = policy["accepted_at"] if policy is not None else None
        if isinstance(policy_accepted_at, datetime) and policy_accepted_at.tzinfo is None:
            policy_accepted_at = policy_accepted_at.replace(tzinfo=timezone.utc)
        probe_expires_at = probe["expires_at"] if probe is not None else None
        if isinstance(probe_expires_at, datetime) and probe_expires_at.tzinfo is None:
            probe_expires_at = probe_expires_at.replace(tzinfo=timezone.utc)
        probe_observed_at = probe["observed_at"] if probe is not None else None
        if isinstance(probe_observed_at, datetime) and probe_observed_at.tzinfo is None:
            probe_observed_at = probe_observed_at.replace(tzinfo=timezone.utc)
        resource_windows: list[tuple[datetime, datetime]] = []
        if probe is not None:
            for prefix in (
                "instrument",
                "mark_price",
                "account_config",
                "leverage",
                "exchange_max_leverage",
            ):
                resource_observed = probe[f"{prefix}_observed_at"]
                resource_expires = probe[f"{prefix}_expires_at"]
                if resource_observed.tzinfo is None:
                    resource_observed = resource_observed.replace(tzinfo=timezone.utc)
                if resource_expires.tzinfo is None:
                    resource_expires = resource_expires.replace(tzinfo=timezone.utc)
                resource_windows.append((resource_observed, resource_expires))
        return bool(
            deployment is not None
            and deployment["status"] == "ACTIVE"
            and deployment["demo_only"] is True
            and deployment["allow_real_funds"] is False
            and deployment["capability_digest"]
            == authority.deployment_capability_digest
            and policy is not None
            and policy["deployment_approval_id"]
            == deployment["deployment_approval_id"]
            and policy["execution_attestation_id"] == authority.attestation_id
            and policy["policy_digest"]
            == authority.execution_canary_risk_policy_digest
            and policy["status"] == "ACTIVE"
            and policy["execution_target"] == "OKX_DEMO"
            and policy["allow_real_funds"] is False
            and policy["position_policy"] == "LONG_ONLY"
            and Decimal(str(policy["strategy_max_leverage"]))
            == Decimal(authority.strategy_max_leverage)
            and Decimal(str(policy["effective_leverage"]))
            == Decimal(authority.effective_leverage)
            and policy_expires_at is not None
            and policy_accepted_at is not None
            and policy_accepted_at <= now
            and policy_expires_at - policy_accepted_at == timedelta(minutes=30)
            and attestation is not None
            and attestation["deployment_id"] == authority.deployment_id
            and attestation["status"] == "READY"
            and attestation["execution_target"] == "OKX_DEMO"
            and attestation["permissions_json"]
            == {"read": True, "trade": True, "withdraw": False}
            and attestation["attestation_digest"] == authority.attestation_digest
            and expires_at == authority.attestation_expires_at
            and expires_at is not None
            and attestation_observed_at is not None
            and attestation_observed_at <= now
            and attestation_observed_at < expires_at
            and probe is not None
            and policy["probe_receipt_id"] == probe["id"]
            and probe["execution_attestation_id"] == authority.attestation_id
            and probe["deployment_id"] == authority.deployment_id
            and probe["execution_target"] == "OKX_DEMO"
            and probe["instrument"] == policy["instrument"]
            and probe["allow_real_funds"] is False
            and probe["simulated_trading"] is True
            and probe["instrument_digest"]
            == authority.instrument_metadata_digest
            and probe["mark_price_digest"]
            == authority.mark_price_snapshot_digest
            and policy["metadata_receipt_digest"]
            == authority.instrument_metadata_digest
            and policy["mark_price_receipt_digest"]
            == authority.mark_price_snapshot_digest
            and probe_expires_at is not None
            and probe_observed_at is not None
            and probe_observed_at <= now
            and probe_observed_at < probe_expires_at
            and all(observed <= now and observed < expires for observed, expires in resource_windows)
        )

    def verify_recovery(
        self,
        authority: OrderWriterCanaryAuthority,
        *,
        order_id: UUID,
        observed_at: datetime,
    ) -> bool:
        if observed_at.tzinfo is None:
            return False
        try:
            with self._connection_factory() as connection:
                validate_canary_recovery_order(
                    connection,
                    order_id=order_id,
                    authority=authority,
                    allow_terminal_replay=True,
                )
        except Exception:
            return False
        return True


class CanonicalOrderWriterOperator:
    """Execute one DB-authorized Demo order without caller-supplied order facts."""

    def __init__(
        self,
        *,
        plan: Phase9LaunchPlan,
        supervisor_lease: Phase9Lease,
        authority_port: DatabaseOrderWriterAuthorityVerifier,
        holder_token: str,
        connection_factory: ConnectionFactory,
        session_factory: DemoSessionFactory,
    ) -> None:
        if len(holder_token) < 32:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_ORDER_HOLDER", "stable holder token is invalid"
            )
        holder_digest = sha256(
            json.dumps(
                {"holder_token": holder_token},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if (
            plan.service_key != "order_writer"
            or supervisor_lease.service_key != plan.service_key
            or supervisor_lease.plan_digest != plan.plan_digest
            or supervisor_lease.generation != plan.generation
            or supervisor_lease.order_writer_canary_authority
            != plan.order_writer_canary_authority
            or supervisor_lease.recovery_order_id != plan.recovery_order_id
            or supervisor_lease.holder_token_digest != holder_digest
        ):
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_ORDER_SUPERVISOR_FENCE",
                "operator must share the exact verified supervisor lease holder",
            )
        self._plan = plan
        self._supervisor_lease = supervisor_lease
        self._authority_port = authority_port
        self._holder_digest = holder_digest
        self._connection_factory = connection_factory
        self._session_factory = session_factory

    def prepare_canary(
        self, *, risk_decision_id: UUID, evaluated_at: datetime
    ) -> PreparedDemoOrder:
        """Durably prepare the exact request and DB lease without exchange I/O."""

        if evaluated_at.tzinfo is None:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_ORDER_TIMEZONE", "evaluated_at must be aware"
            )
        if self._plan.recovery_order_id is not None:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_RECOVERY_PLAN_DISPATCH",
                "recovery plan cannot prepare a new canary order",
            )
        require_current_order_writer_canary_authority(
            plan=self._plan,
            observed_at=evaluated_at,
            port=self._authority_port,
        )
        if self._supervisor_lease.expires_at <= evaluated_at.astimezone(timezone.utc):
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_ORDER_SUPERVISOR_FENCE",
                "supervisor lease is no longer fresh",
            )
        with self._connection_factory() as connection:
            decision = connection.execute(
                select(RISK_DECISIONS_TABLE).where(
                    RISK_DECISIONS_TABLE.c.id == risk_decision_id
                )
            ).mappings().one_or_none()
            intent = (
                connection.execute(
                    select(TRADE_INTENTS_TABLE).where(
                        TRADE_INTENTS_TABLE.c.id == decision["trade_intent_id"]
                    )
                ).mappings().one_or_none()
                if decision is not None
                else None
            )
            request = intent["intent_json"].get("exchange_body") if intent else None
            if (
                decision is None
                or decision["status"] != "RISK_ACCEPTED"
                or intent is None
                or not isinstance(request, Mapping)
            ):
                raise CanonicalPhase9CompositionBlocked(
                    "BLOCKED_PHASE9_ORDER_AUTHORITY",
                    "exact accepted risk decision and persisted exchange body required",
                )
            reservation = connection.execute(
                select(EXECUTION_RISK_RESERVATIONS_TABLE).where(
                    EXECUTION_RISK_RESERVATIONS_TABLE.c.trade_intent_id == intent["id"]
                )
            ).mappings().one_or_none()
            budget = (
                connection.execute(
                    select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE).where(
                        EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.id
                        == reservation["risk_budget_authorization_id"]
                    )
                ).mappings().one_or_none()
                if reservation is not None
                else None
            )
            policy = (
                connection.execute(
                    select(EXECUTION_CANARY_RISK_POLICIES_TABLE).where(
                        EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id
                        == budget["execution_canary_risk_policy_id"]
                    )
                ).mappings().one_or_none()
                if budget is not None
                else None
            )
            try:
                attestation_uuid = UUID(str(policy["execution_attestation_id"]))
            except (TypeError, ValueError) as exc:
                raise CanonicalPhase9CompositionBlocked(
                    "BLOCKED_PHASE9_ORDER_ATTESTATION",
                    "risk policy does not bind an execution attestation",
                ) from exc
            prepared = prepare_demo_order(
                connection,
                risk_decision_id=risk_decision_id,
                attestation_id=attestation_uuid,
                writer_identity=CANONICAL_ORDER_WRITER_IDENTITY,
                holder_identity=CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
                holder_token_digest=self._holder_digest,
                idempotency_key=f"phase9-canary:{risk_decision_id}",
                order_request={str(key): str(value) for key, value in request.items()},
                evaluated_at=evaluated_at,
            )
        return prepared

    def dispatch_canary(
        self, *, risk_decision_id: UUID, evaluated_at: datetime
    ) -> DispatchedDemoOrder:
        prepared = self.prepare_canary(
            risk_decision_id=risk_decision_id,
            evaluated_at=evaluated_at,
        )
        with self._session_factory() as session:
            return dispatch_demo_order(
                self._connection_factory,
                order_id=prepared.order_id,
                transport=session,
                holder_identity=CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
                holder_token_digest=self._holder_digest,
                lease_generation=prepared.lease_generation,
                evaluated_at=evaluated_at,
            )

    def recover_canary(
        self, *, order_id: UUID, evaluated_at: datetime
    ) -> DispatchedDemoOrder:
        """Resolve an uncertain POST by the saga's GET-only recovery path."""

        if self._plan.recovery_order_id != order_id:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_RECOVERY_ORDER_DRIFT",
                "GET recovery must match the exact frozen order",
            )
        require_current_order_writer_canary_authority(
            plan=self._plan,
            observed_at=evaluated_at,
            port=self._authority_port,
        )
        if self._supervisor_lease.expires_at <= evaluated_at.astimezone(timezone.utc):
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_ORDER_SUPERVISOR_FENCE",
                "supervisor lease is no longer fresh",
            )
        with self._session_factory() as session:
            return recover_demo_order_get_only(
                self._connection_factory,
                order_id=order_id,
                transport=session,
            )

    def retry_canary(
        self, *, order_id: UUID, evaluated_at: datetime
    ) -> DispatchedDemoOrder:
        """Use the saga-authorized second POST after exact GET_NOT_FOUND only."""

        if self._plan.recovery_order_id != order_id:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_RECOVERY_ORDER_DRIFT",
                "retry must match the exact frozen recovery order",
            )
        require_current_order_writer_canary_authority(
            plan=self._plan,
            observed_at=evaluated_at,
            port=self._authority_port,
        )
        if self._supervisor_lease.expires_at <= evaluated_at.astimezone(timezone.utc):
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_ORDER_SUPERVISOR_FENCE",
                "supervisor lease is no longer fresh",
            )
        with self._connection_factory() as connection:
            recovery = validate_canary_recovery_order(
                connection,
                order_id=order_id,
                authority=self._plan.order_writer_canary_authority,
                require_negative_outcome=True,
            )
            authority = self._plan.order_writer_canary_authority
            assert authority is not None
            prepared = prepare_demo_order(
                connection,
                risk_decision_id=recovery.risk_decision_id,
                attestation_id=authority.attestation_id,
                writer_identity=CANONICAL_ORDER_WRITER_IDENTITY,
                holder_identity=CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
                holder_token_digest=self._holder_digest,
                idempotency_key=f"phase9-canary:{recovery.risk_decision_id}",
                order_request=recovery.request_body,
                evaluated_at=evaluated_at,
            )
        if prepared.order_id != order_id:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_RECOVERY_ORDER_DRIFT",
                "prepared retry did not resolve to the frozen order",
            )
        with self._session_factory() as session:
            return dispatch_demo_order(
                self._connection_factory,
                order_id=order_id,
                transport=session,
                holder_identity=CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
                holder_token_digest=self._holder_digest,
                lease_generation=prepared.lease_generation,
                evaluated_at=evaluated_at,
            )


def compose_supervise_ports(
    plan: Phase9LaunchPlan,
    *,
    runtime_worker_factory: RuntimeWorkerFactoryPort | None,
    order_connection_factory: ConnectionFactory | None,
) -> SupervisePorts:
    """Compose exact service ports; never silently return an unusable default."""

    if plan.service_key == "long_lived_runtime":
        if runtime_worker_factory is None:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_RUNTIME_COMPOSITION_UNSET",
                "a sealed runtime DB/signer/market/evaluator factory is required",
            )
        return SupervisePorts(runtime_worker_factory.build(plan), None)
    if plan.service_key == "order_writer":
        if order_connection_factory is None:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_ORDER_COMPOSITION_UNSET",
                "canonical_order_writer database capability is required",
            )
        return SupervisePorts(
            None, DatabaseOrderWriterAuthorityVerifier(order_connection_factory)
        )
    raise CanonicalPhase9CompositionBlocked(
        "BLOCKED_PHASE9_SERVICE", plan.service_key
    )


def confirm_running_runtime_from_supervisor(
    connection: Connection,
    *,
    plan: Phase9LaunchPlan,
    lease: Phase9Lease,
    heartbeat_receipt: Phase9LifecycleReceipt,
    observed_at: datetime,
    credential_reference: str,
) -> UUID:
    """Persist ACTIVE only from the exact live supervisor evidence objects."""

    if (
        plan.service_key != "long_lived_runtime"
        or plan.deployment_id is None
        or plan.deployment_capability_digest is None
        or plan.image_digest is None
        or credential_reference != "none:public-okx-market-only"
    ):
        raise CanonicalPhase9CompositionBlocked(
            "BLOCKED_PHASE9_RUNTIME_CONFIRMATION_PLAN",
            "exact runtime plan and public-only credential boundary are required",
        )
    deployment = connection.execute(
        select(DEPLOYMENTS_TABLE).where(
            DEPLOYMENTS_TABLE.c.id == plan.deployment_id
        )
    ).mappings().one_or_none()
    approval = (
        connection.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id
                == deployment["deployment_approval_id"]
            )
        ).mappings().one_or_none()
        if deployment is not None
        else None
    )
    qualification = (
        connection.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == approval["qualification_decision_id"]
            )
        ).mappings().one_or_none()
        if approval is not None
        else None
    )
    if (
        deployment is None
        or deployment["status"] not in {"PENDING", "ACTIVE"}
        or deployment["capability_digest"] != plan.deployment_capability_digest
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or approval is None
        or approval["status"] != "APPROVED"
        or qualification is None
        or qualification["status"] != "QUALIFIED"
    ):
        raise CanonicalPhase9CompositionBlocked(
            "BLOCKED_PHASE9_RUNTIME_CONFIRMATION_LINEAGE",
            "current deployment, approval, or qualification drifted",
        )
    launch_spec = FrozenRuntimeLaunchSpec(
        deployment_id=plan.deployment_id,
        approval_id=approval["id"],
        qualification_decision_id=qualification["id"],
        strategy_version_id=deployment["strategy_version_id"],
        configuration_bundle_id=deployment["configuration_bundle_id"],
        configuration_bundle_digest=deployment["configuration_bundle_digest"],
        market_snapshot_id=deployment["market_snapshot_id"],
        market_snapshot_digest=deployment["market_snapshot_digest"],
        deployment_capability_digest=deployment["capability_digest"],
        runtime_identity=plan.process_identity,
        image_digest=plan.image_digest,
        service_account="canonical_runtime_reader",
        network_policy="DEMO_EXCHANGE_ONLY",
        credential_reference=credential_reference,
    )
    # A deployment owns one stable runtime process identity across the serial
    # NO_ORDER_SOAK -> SIGNAL_RISK_SHADOW stage transition.  The stage-specific
    # plan/generation remains bound into each observation receipt, but must not
    # manufacture a replacement runtime row for the same deployment.
    runtime_id = uuid5(
        NAMESPACE_URL,
        f"canonical-v13-runtime:{plan.deployment_id}:{plan.process_identity}",
    )
    receipt = build_production_runtime_observation(
        plan=plan,
        launch_spec=launch_spec,
        runtime_instance_id=runtime_id,
        lease=lease,
        running_receipt=heartbeat_receipt,
        observed_at=observed_at,
    )
    return confirm_production_demo_runtime_observation(
        connection,
        deployment_id=plan.deployment_id,
        runtime_identity=plan.process_identity,
        image_digest=plan.image_digest,
        credential_reference=credential_reference,
        receipt=receipt,
        evaluated_at=observed_at,
    )


def confirm_stopped_runtime_from_supervisor(
    connection: Connection,
    *,
    plan: Phase9LaunchPlan,
    stop_receipt: Phase9LifecycleReceipt,
    observed_at: datetime,
    launch_agent_loaded: bool,
    holder_pid_alive: bool,
    lease: Phase9Lease | None,
    container_present: bool,
    credential_reference: str,
    predecessor_stop_receipt: Phase9LifecycleReceipt | None = None,
    predecessor_container_present: bool = False,
):
    """Persist STOPPED only from exact current supervisor and database lineage."""

    if (
        plan.service_key != "long_lived_runtime"
        or plan.deployment_id is None
        or plan.deployment_capability_digest is None
        or plan.image_digest is None
        or credential_reference != "none:public-okx-market-only"
    ):
        raise CanonicalPhase9CompositionBlocked(
            "BLOCKED_PHASE9_RUNTIME_STOP_PLAN",
            "exact runtime plan and public-only credential boundary are required",
        )
    deployment = connection.execute(
        select(DEPLOYMENTS_TABLE).where(DEPLOYMENTS_TABLE.c.id == plan.deployment_id)
    ).mappings().one_or_none()
    runtime = connection.execute(
        select(RUNTIME_INSTANCES_TABLE).where(
            RUNTIME_INSTANCES_TABLE.c.deployment_id == plan.deployment_id
        )
    ).mappings().one_or_none()
    approval = (
        connection.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id
                == deployment["deployment_approval_id"]
            )
        ).mappings().one_or_none()
        if deployment is not None
        else None
    )
    qualification = (
        connection.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == approval["qualification_decision_id"]
            )
        ).mappings().one_or_none()
        if approval is not None
        else None
    )
    stable_lineage = (
        deployment is None
        or deployment["status"] not in {"ACTIVE", "DISABLED"}
        or deployment["capability_digest"] != plan.deployment_capability_digest
        or deployment["demo_only"] is not True
        or deployment["allow_real_funds"] is not False
        or runtime is None
        or (
            deployment["status"] == "DISABLED"
            and runtime["status"] != "STOPPED"
        )
        or runtime["runtime_identity"] != plan.process_identity
        or runtime["service_account"] != "canonical_runtime_reader"
        or runtime["order_writer_capability"] is not False
        or approval is None
        or approval["status"] != "APPROVED"
        or qualification is None
        or qualification["status"] != "QUALIFIED"
    )
    if stable_lineage:
        raise CanonicalPhase9CompositionBlocked(
            "BLOCKED_PHASE9_RUNTIME_STOP_LINEAGE",
            "exact ACTIVE Demo runtime lineage is required",
        )
    observed_image_digest = str(runtime["image_digest"])
    recovering_predecessor = observed_image_digest != plan.image_digest
    launch_spec = FrozenRuntimeLaunchSpec(
        deployment_id=plan.deployment_id,
        approval_id=approval["id"],
        qualification_decision_id=qualification["id"],
        strategy_version_id=deployment["strategy_version_id"],
        configuration_bundle_id=deployment["configuration_bundle_id"],
        configuration_bundle_digest=deployment["configuration_bundle_digest"],
        market_snapshot_id=deployment["market_snapshot_id"],
        market_snapshot_digest=deployment["market_snapshot_digest"],
        deployment_capability_digest=deployment["capability_digest"],
        runtime_identity=plan.process_identity,
        image_digest=observed_image_digest,
        service_account="canonical_runtime_reader",
        network_policy="DEMO_EXCHANGE_ONLY",
        credential_reference=credential_reference,
    )
    if recovering_predecessor:
        latest_runtime_receipt = connection.execute(
            select(RUNTIME_RECEIPTS_TABLE)
            .where(RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime["id"])
            .order_by(RUNTIME_RECEIPTS_TABLE.c.observed_at.desc())
            .limit(1)
        ).mappings().one_or_none()
        if (
            predecessor_stop_receipt is None
            or stop_receipt.action != "STOP"
            or stop_receipt.status != "STOPPED"
            or stop_receipt.generation != plan.generation
            or stop_receipt.plan_digest != plan.plan_digest
            or stop_receipt.observed_at > observed_at
            or predecessor_stop_receipt.action != "STOP"
            or predecessor_stop_receipt.status != "STOPPED"
            or predecessor_stop_receipt.generation != plan.generation - 1
            or predecessor_stop_receipt.observed_at > plan.prepared_at
            or predecessor_stop_receipt.details.get("label")
            != plan.launch_agent_label
            or latest_runtime_receipt is None
            or latest_runtime_receipt["status"] != "HEALTHY"
            or latest_runtime_receipt["evidence_class"]
            != "PRODUCTION_DEMO_RUNTIME"
            or latest_runtime_receipt["launch_spec_digest"]
            != runtime["launch_spec_digest"]
            or latest_runtime_receipt["capability_digest"]
            != deployment["capability_digest"]
            or latest_runtime_receipt["network_policy"] != "DEMO_EXCHANGE_ONLY"
            or latest_runtime_receipt["service_account"]
            != "canonical_runtime_reader"
            or latest_runtime_receipt["order_writer_capability"] is not False
            or runtime["status"] != "HEALTHY"
            or frozen_runtime_launch_spec_digest(launch_spec)
            != runtime["launch_spec_digest"]
            or launch_agent_loaded
            or holder_pid_alive
            or lease is not None
            or container_present
            or predecessor_container_present
        ):
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_RUNTIME_PREDECESSOR_STOP",
                "verified predecessor stop, persisted runtime lineage, and zero holders are required",
            )
        receipt = build_runtime_observation_receipt(
            runtime_instance_id=runtime["id"],
            launch_spec=launch_spec,
            status="STOPPED",
            observed_at=observed_at,
            evidence_class="PRODUCTION_DEMO_RUNTIME_STOP",
        )
    else:
        receipt = build_production_runtime_stop_observation(
            plan=plan,
            launch_spec=launch_spec,
            runtime_instance_id=runtime["id"],
            stop_receipt=stop_receipt,
            observed_at=observed_at,
            launch_agent_loaded=launch_agent_loaded,
            holder_pid_alive=holder_pid_alive,
            lease=lease,
            container_present=container_present,
        )
    return confirm_production_demo_runtime_stop_observation(
        connection,
        deployment_id=plan.deployment_id,
        receipt=receipt,
        evaluated_at=observed_at,
    )


__all__ = [
    "CanonicalPhase9CompositionBlocked",
    "CanonicalOrderWriterOperator",
    "CanonicalFillWriterOperator",
    "CanonicalLedgerWriterOperator",
    "CanonicalReconciliationWriterOperator",
    "DatabaseOrderWriterAuthorityVerifier",
    "RuntimeWorkerFactoryPort",
    "RecordedCanaryProbe",
    "SupervisePorts",
    "compose_supervise_ports",
    "confirm_running_runtime_from_supervisor",
    "confirm_stopped_runtime_from_supervisor",
    "record_current_canary_attestation",
    "record_current_canary_probe_receipt",
]
