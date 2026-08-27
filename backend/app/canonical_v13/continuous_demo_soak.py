"""Bounded continuous OKX_DEMO state machine.

One tick advances at most one durable step.  It never creates a signal and it
never holds a writer lease between ticks.  Openings consume only fresh natural
15m signals; exits drain the exact reconciled long position even when new
openings are disabled.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import Connection, func, select

from app.canonical_v13.continuous_demo_execution import (
    MAXIMUM_SIGNAL_AGE,
    extract_strategy_exit_after_seconds,
    grant_continuous_open,
    grant_position_exit,
)
from app.canonical_v13.continuous_demo_order_writer import (
    ContinuousDemoTransport,
    dispatch_continuous_demo_order,
    prepare_continuous_demo_order,
)
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.models import (
    DEPLOYMENTS_TABLE,
    FILLS_TABLE,
    LEDGER_ENTRIES_TABLE,
    ORDERS_TABLE,
    RECONCILIATION_ITEMS_TABLE,
    RISK_DECISIONS_TABLE,
    SIGNALS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_VERSIONS_TABLE,
    TRADE_INTENTS_TABLE,
)
from app.canonical_v13.phase9_canary_policy import persist_canary_probe_receipt
from app.canonical_v13.phase9_execution_authority import record_redacted_demo_attestation
from app.canonical_v13.phase9_order_writer import (
    CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
    cancel_prepared_demo_order,
    recover_demo_order_get_only,
    release_demo_order_writer_lease,
)
from app.canonical_v13.phase9_production_composition import (
    CanonicalFillWriterOperator,
    CanonicalLedgerWriterOperator,
    CanonicalPhase9CompositionBlocked,
    CanonicalReconciliationWriterOperator,
)
from app.canonical_v13.risk_service import (
    INTENT_MODE_CONTINUOUS_OPEN,
    INTENT_MODE_POSITION_EXIT,
)


INSTRUMENT = "BTC-USDT-SWAP"
MARKET_FILL_TIMEOUT = timedelta(seconds=60)
MAXIMUM_FILL_SLIPPAGE_BPS = Decimal("100")


ConnectionFactory = Callable[[], AbstractContextManager[Connection]]
SessionFactory = Callable[[], AbstractContextManager[ContinuousDemoTransport]]


@dataclass(frozen=True)
class ContinuousDemoSoakResult:
    status: str
    action: str
    reason_code: str
    order_id: UUID | None
    signal_id: UUID | None
    risk_decision_id: UUID | None
    repeat_noop: bool
    allow_real_funds: bool = False


def _utc(value: datetime | None = None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_CONTINUOUS_SOAK_TIME", "tick time must be timezone-aware"
        )
    return resolved.astimezone(timezone.utc)


def _persisted_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _effective(connection: Connection) -> Connection:
    return (
        connection.execution_options(schema_translate_map={"strategy_platform_v13": None})
        if connection.dialect.name == "sqlite"
        else connection
    )


def _result(
    *,
    status: str,
    action: str,
    reason: str,
    order_id: UUID | None = None,
    signal_id: UUID | None = None,
    risk_decision_id: UUID | None = None,
    repeat_noop: bool = False,
) -> ContinuousDemoSoakResult:
    return ContinuousDemoSoakResult(
        status,
        action,
        reason,
        order_id,
        signal_id,
        risk_decision_id,
        repeat_noop,
    )


class ContinuousDemoSoakOperator:
    def __init__(
        self,
        *,
        reader_factory: ConnectionFactory,
        deployment_factory: ConnectionFactory,
        approval_factory: ConnectionFactory,
        risk_factory: ConnectionFactory,
        order_factory: ConnectionFactory,
        fill_factory: ConnectionFactory,
        ledger_factory: ConnectionFactory,
        reconciliation_factory: ConnectionFactory,
        session_factory: SessionFactory,
        holder_token_digest: str,
    ) -> None:
        self._reader_factory = reader_factory
        self._deployment_factory = deployment_factory
        self._approval_factory = approval_factory
        self._risk_factory = risk_factory
        self._order_factory = order_factory
        self._fill_factory = fill_factory
        self._ledger_factory = ledger_factory
        self._reconciliation_factory = reconciliation_factory
        self._session_factory = session_factory
        self._holder_token_digest = holder_token_digest

    def _active_deployment_id(self) -> UUID:
        with self._reader_factory() as connection:
            connection = _effective(connection)
            rows = connection.execute(
                select(DEPLOYMENTS_TABLE.c.id).where(
                    DEPLOYMENTS_TABLE.c.status == "ACTIVE",
                    DEPLOYMENTS_TABLE.c.demo_only.is_(True),
                    DEPLOYMENTS_TABLE.c.allow_real_funds.is_(False),
                )
            ).scalars().all()
        if len(rows) != 1:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_ACTIVE_DEPLOYMENT",
                "exactly one Demo-only ACTIVE deployment is required",
            )
        return rows[0]

    def _fill_progress(
        self,
        order: Mapping[str, object],
        *,
        connection: Connection | None = None,
    ) -> tuple[Decimal, Decimal]:
        if connection is None:
            with self._reader_factory() as opened:
                return self._fill_progress(
                    order, connection=_effective(opened)
                )
        effective = _effective(connection)
        decision = effective.execute(
            select(RISK_DECISIONS_TABLE).where(
                RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
            )
        ).mappings().one_or_none()
        intent = (
            effective.execute(
                select(TRADE_INTENTS_TABLE).where(
                    TRADE_INTENTS_TABLE.c.id == decision["trade_intent_id"]
                )
            ).mappings().one_or_none()
            if decision is not None
            else None
        )
        exchange_body = (
            intent["intent_json"].get("exchange_body")
            if intent is not None
            and isinstance(intent["intent_json"], Mapping)
            else None
        )
        fills = effective.execute(
            select(FILLS_TABLE.c.fill_json).where(
                FILLS_TABLE.c.order_id == order["id"]
            )
        ).scalars().all()
        try:
            requested = Decimal(str(exchange_body["sz"]))
            filled = sum(
                (Decimal(str(payload["size"])) for payload in fills), Decimal(0)
            )
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_FILL_PROGRESS",
                "persisted order size or fill size is invalid",
            ) from exc
        if (
            not requested.is_finite()
            or requested <= 0
            or not filled.is_finite()
            or filled < 0
            or filled > requested
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_FILL_PROGRESS",
                "cumulative fill size is outside the persisted order size",
            )
        return filled, requested

    def _execution_state(self) -> tuple[Mapping[str, object] | None, str]:
        with self._reader_factory() as connection:
            connection = _effective(connection)
            rows = connection.execute(
                select(ORDERS_TABLE, RISK_DECISIONS_TABLE.c.decision_mode)
                .join(
                    RISK_DECISIONS_TABLE,
                    RISK_DECISIONS_TABLE.c.id == ORDERS_TABLE.c.risk_decision_id,
                )
                .where(
                    RISK_DECISIONS_TABLE.c.decision_mode.in_(
                        ("EXECUTION", INTENT_MODE_CONTINUOUS_OPEN, INTENT_MODE_POSITION_EXIT)
                    )
                )
                .order_by(ORDERS_TABLE.c.created_at.desc())
            ).mappings().all()
            for row in rows:
                order_id = row["id"]
                fill_count = int(
                    connection.execute(
                        select(func.count()).select_from(FILLS_TABLE).where(
                            FILLS_TABLE.c.order_id == order_id
                        )
                    ).scalar_one()
                )
                ledger_count = int(
                    connection.execute(
                        select(func.count())
                        .select_from(LEDGER_ENTRIES_TABLE)
                        .join(FILLS_TABLE, FILLS_TABLE.c.id == LEDGER_ENTRIES_TABLE.c.fill_id)
                        .where(FILLS_TABLE.c.order_id == order_id)
                    ).scalar_one()
                )
                recon_count = int(
                    connection.execute(
                        select(func.count()).select_from(RECONCILIATION_ITEMS_TABLE).where(
                            RECONCILIATION_ITEMS_TABLE.c.order_id == order_id
                        )
                    ).scalar_one()
                )
                if row["status"] in {"SUBMITTED", "DISPATCHING"}:
                    return row, "DISPATCH"
                if row["status"] in {"ACCEPTED", "PARTIAL", "FILLED"}:
                    if fill_count == 0:
                        return row, "FILL"
                    filled, requested = self._fill_progress(
                        row, connection=connection
                    )
                    if filled < requested:
                        return row, "FILL"
                    if ledger_count < fill_count:
                        return row, "LEDGER"
                    if recon_count < fill_count:
                        return row, "RECONCILE"
            return None, "COMPLETE"

    def _release_lease(self, *, now: datetime) -> None:
        try:
            with self._order_factory() as connection:
                release_demo_order_writer_lease(
                    connection,
                    holder_identity=CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
                    holder_token_digest=self._holder_token_digest,
                    evaluated_at=now,
                )
        except CanonicalExecutionChainBlocked as exc:
            if exc.code != "BLOCKED_ORDER_WRITER_LEASE_UNSET":
                raise

    def _dispatch_existing(
        self, order: Mapping[str, object], *, now: datetime
    ) -> ContinuousDemoSoakResult:
        decision_expires = None
        with self._reader_factory() as connection:
            connection = _effective(connection)
            decision = connection.execute(
                select(RISK_DECISIONS_TABLE).where(
                    RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
                )
            ).mappings().one()
            raw_expiry = decision["decision_json"].get("expires_at")
            if isinstance(raw_expiry, str):
                decision_expires = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        if order["status"] == "SUBMITTED" and (
            decision_expires is None or decision_expires <= now
        ):
            with self._order_factory() as connection:
                cancelled = cancel_prepared_demo_order(
                    connection, order_id=order["id"], evaluated_at=now
                )
            self._release_lease(now=now)
            return _result(
                status="NO_ACTION",
                action="CANCEL_EXPIRED_PREPARED_ORDER",
                reason="CONTINUOUS_GRANT_EXPIRED_BEFORE_POST",
                order_id=cancelled.order_id,
                risk_decision_id=order["risk_decision_id"],
                repeat_noop=cancelled.repeat_noop,
            )
        with self._session_factory() as session:
            try:
                if order["status"] == "DISPATCHING":
                    dispatched = recover_demo_order_get_only(
                        self._order_factory,
                        order_id=order["id"],
                        transport=session,
                    )
                else:
                    lease_generation = self._renew_prepared_lease(order["id"], now=now)
                    dispatched = dispatch_continuous_demo_order(
                        self._order_factory,
                        order_id=order["id"],
                        transport=session,
                        holder_identity=CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
                        holder_token_digest=self._holder_token_digest,
                        lease_generation=lease_generation,
                        evaluated_at=now,
                    )
            finally:
                self._release_lease(now=now)
        return _result(
            status="ADVANCED",
            action="DISPATCH_ORDER",
            reason="CONTINUOUS_ORDER_DISPATCHED",
            order_id=dispatched.order_id,
            risk_decision_id=order["risk_decision_id"],
            repeat_noop=dispatched.repeat_noop,
        )

    def _renew_prepared_lease(self, order_id: UUID, *, now: datetime) -> int:
        with self._reader_factory() as connection:
            connection = _effective(connection)
            order = connection.execute(
                select(ORDERS_TABLE).where(ORDERS_TABLE.c.id == order_id)
            ).mappings().one()
            decision = connection.execute(
                select(RISK_DECISIONS_TABLE).where(
                    RISK_DECISIONS_TABLE.c.id == order["risk_decision_id"]
                )
            ).mappings().one()
            attestation_id = UUID(str(decision["decision_json"]["execution_attestation_id"]))
        with self._order_factory() as connection:
            prepared = prepare_continuous_demo_order(
                connection,
                risk_decision_id=order["risk_decision_id"],
                attestation_id=attestation_id,
                writer_identity="canonical_order_writer",
                holder_identity=CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
                holder_token_digest=self._holder_token_digest,
                idempotency_key=order["idempotency_key"],
                evaluated_at=now,
            )
        return prepared.lease_generation

    def _fill(self, order: Mapping[str, object], *, now: datetime) -> ContinuousDemoSoakResult:
        try:
            ids = CanonicalFillWriterOperator(
                connection_factory=self._fill_factory,
                session_factory=self._session_factory,
            ).collect(order_id=order["id"])
        except CanonicalPhase9CompositionBlocked as exc:
            age = now - _persisted_utc(order["created_at"])
            if exc.code == "BLOCKED_PHASE9_FILL_UNSET" and age <= MARKET_FILL_TIMEOUT:
                return _result(
                    status="WAITING",
                    action="WAIT_FOR_MARKET_FILL",
                    reason="MARKET_FILL_PENDING",
                    order_id=order["id"],
                    risk_decision_id=order["risk_decision_id"],
                )
            raise
        filled, requested = self._fill_progress(order)
        if filled < requested:
            age = now - _persisted_utc(order["created_at"])
            if age <= MARKET_FILL_TIMEOUT:
                return _result(
                    status="WAITING",
                    action="WAIT_FOR_MARKET_FILL",
                    reason="MARKET_FILL_PARTIAL",
                    order_id=order["id"],
                    risk_decision_id=order["risk_decision_id"],
                )
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_PARTIAL_FILL_TIMEOUT",
                "market order did not reach its exact persisted size",
            )
        return _result(
            status="ADVANCED",
            action="COLLECT_FILL",
            reason="EXCHANGE_FILL_RECORDED",
            order_id=order["id"],
            risk_decision_id=order["risk_decision_id"],
            repeat_noop=not ids,
        )

    def _ledger(self, order: Mapping[str, object]) -> ContinuousDemoSoakResult:
        with self._reader_factory() as connection:
            connection = _effective(connection)
            fill_ids = connection.execute(
                select(FILLS_TABLE.c.id)
                .outerjoin(
                    LEDGER_ENTRIES_TABLE,
                    LEDGER_ENTRIES_TABLE.c.fill_id == FILLS_TABLE.c.id,
                )
                .where(
                    FILLS_TABLE.c.order_id == order["id"],
                    LEDGER_ENTRIES_TABLE.c.id.is_(None),
                )
            ).scalars().all()
        for fill_id in fill_ids:
            CanonicalLedgerWriterOperator(self._ledger_factory).post(fill_id=fill_id)
        return _result(
            status="ADVANCED",
            action="POST_LEDGER",
            reason="FILL_DERIVED_LEDGER_RECORDED",
            order_id=order["id"],
            risk_decision_id=order["risk_decision_id"],
        )

    def _fresh_flat_probe(self, *, deployment_id: UUID, now: datetime):
        with self._session_factory() as session:
            probe = session.probe(instrument=INSTRUMENT)
        with self._deployment_factory() as connection:
            attestation = record_redacted_demo_attestation(
                connection,
                deployment_id=deployment_id,
                instrument=probe.instrument,
                account_fingerprint_digest=probe.account_fingerprint_digest,
                credential_generation_digest=probe.credential_generation_digest,
                permissions=probe.permissions,
                observed_at=probe.observed_at,
                expires_at=probe.expires_at,
                evaluated_at=now,
            )
        with self._approval_factory() as connection:
            receipt = persist_canary_probe_receipt(
                connection,
                probe=probe,
                deployment_id=deployment_id,
                execution_attestation_id=attestation.attestation_id,
                evaluated_at=now,
            )
        return probe, attestation, receipt

    def _reconcile(self, order: Mapping[str, object], *, now: datetime) -> ContinuousDemoSoakResult:
        flat_probe_id = None
        if order["decision_mode"] == INTENT_MODE_POSITION_EXIT:
            _probe, _attestation, receipt = self._fresh_flat_probe(
                deployment_id=self._active_deployment_id(), now=now
            )
            flat_probe_id = receipt.probe_receipt_id
        runs = CanonicalReconciliationWriterOperator(
            self._reconciliation_factory
        ).reconcile(
            order_id=order["id"],
            flat_probe_receipt_id=flat_probe_id,
            evaluated_at=now,
        )
        with self._reader_factory() as connection:
            connection = _effective(connection)
            slippage = [
                Decimal(str(row["fill_json"].get("slippage_bps", "0")))
                for row in connection.execute(
                    select(FILLS_TABLE).where(FILLS_TABLE.c.order_id == order["id"])
                ).mappings()
            ]
        excessive_slippage = bool(
            slippage and max(slippage) > MAXIMUM_FILL_SLIPPAGE_BPS
        )
        return _result(
            status="BLOCKED" if excessive_slippage else "ADVANCED",
            action="RECONCILE",
            reason=(
                "MARKET_FILL_SLIPPAGE_LIMIT_EXCEEDED"
                if excessive_slippage
                else (
                    "POSITION_CLOSED_AND_FLAT_RECONCILED"
                    if flat_probe_id is not None
                    else "ORDER_FILL_LEDGER_RECONCILED"
                )
            ),
            order_id=order["id"],
            risk_decision_id=order["risk_decision_id"],
            repeat_noop=not runs,
        )

    def _ledger_net(self) -> Decimal:
        with self._reader_factory() as connection:
            connection = _effective(connection)
            return Decimal(
                str(
                    connection.execute(
                        select(func.coalesce(func.sum(LEDGER_ENTRIES_TABLE.c.amount), 0)).where(
                            LEDGER_ENTRIES_TABLE.c.asset == INSTRUMENT
                        )
                    ).scalar_one()
                )
            )

    def _entry_order_id(self) -> UUID:
        with self._reader_factory() as connection:
            connection = _effective(connection)
            rows = connection.execute(
                select(ORDERS_TABLE.c.id, TRADE_INTENTS_TABLE.c.intent_json)
                .join(RISK_DECISIONS_TABLE, RISK_DECISIONS_TABLE.c.id == ORDERS_TABLE.c.risk_decision_id)
                .join(TRADE_INTENTS_TABLE, TRADE_INTENTS_TABLE.c.id == RISK_DECISIONS_TABLE.c.trade_intent_id)
                .where(ORDERS_TABLE.c.status.in_(("ACCEPTED", "FILLED")))
                .order_by(ORDERS_TABLE.c.created_at.desc())
            ).mappings().all()
        buys = [
            row["id"]
            for row in rows
            if isinstance(row["intent_json"], Mapping)
            and isinstance(row["intent_json"].get("exchange_body"), Mapping)
            and row["intent_json"]["exchange_body"].get("side") == "buy"
        ]
        if not buys:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_ENTRY_ORDER", "no reconciled long entry order exists"
            )
        return buys[0]

    def _grant_exit(self, *, now: datetime) -> ContinuousDemoSoakResult:
        deployment_id = self._active_deployment_id()
        entry_order_id = self._entry_order_id()
        size = self._ledger_net()
        if size <= 0:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_POSITION_LEDGER", "positive long ledger is required"
            )
        with self._session_factory() as session:
            guard = session.exit_guard(
                instrument=INSTRUMENT, expected_contracts=format(size.normalize(), "f")
            )
        # The authenticated exchange snapshots are observed after the tick's
        # initial timestamp.  Bind every downstream decision to that newer
        # observation instead of treating normal network latency as future
        # evidence.
        operation_now = max(now, guard.observed_at)
        with self._deployment_factory() as connection:
            attestation = record_redacted_demo_attestation(
                connection,
                deployment_id=deployment_id,
                instrument=guard.instrument,
                account_fingerprint_digest=guard.account_fingerprint_digest,
                credential_generation_digest=guard.credential_generation_digest,
                permissions=guard.permissions,
                observed_at=guard.observed_at,
                expires_at=guard.expires_at,
                evaluated_at=operation_now,
            )
        with self._risk_factory() as connection:
            grant = grant_position_exit(
                connection,
                entry_order_id=entry_order_id,
                attestation_id=attestation.attestation_id,
                guard=guard,
                evaluated_at=operation_now,
            )
        return self._prepare_and_dispatch(
            grant.risk_decision_id,
            attestation.attestation_id,
            signal_id=None,
            now=operation_now,
        )

    def _exit_due(self, entry_order_id: UUID, *, now: datetime) -> bool:
        with self._reader_factory() as connection:
            connection = _effective(connection)
            row = connection.execute(
                select(FILLS_TABLE.c.fill_json, STRATEGY_ARTIFACTS_TABLE.c.normalized_content)
                .join(ORDERS_TABLE, ORDERS_TABLE.c.id == FILLS_TABLE.c.order_id)
                .join(RISK_DECISIONS_TABLE, RISK_DECISIONS_TABLE.c.id == ORDERS_TABLE.c.risk_decision_id)
                .join(TRADE_INTENTS_TABLE, TRADE_INTENTS_TABLE.c.id == RISK_DECISIONS_TABLE.c.trade_intent_id)
                .join(SIGNALS_TABLE, SIGNALS_TABLE.c.id == TRADE_INTENTS_TABLE.c.signal_id)
                .join(STRATEGY_VERSIONS_TABLE, STRATEGY_VERSIONS_TABLE.c.id == SIGNALS_TABLE.c.strategy_version_id)
                .join(STRATEGY_ARTIFACTS_TABLE, STRATEGY_ARTIFACTS_TABLE.c.id == STRATEGY_VERSIONS_TABLE.c.artifact_id)
                .where(FILLS_TABLE.c.order_id == entry_order_id)
                .order_by(FILLS_TABLE.c.created_at.desc())
                .limit(1)
            ).mappings().one_or_none()
        if row is None:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_EXIT_FILL", "entry fill is missing"
            )
        raw_timestamp = row["fill_json"].get("timestamp")
        if isinstance(raw_timestamp, str) and raw_timestamp.isdigit():
            fill_at = datetime.fromtimestamp(int(raw_timestamp) / 1000, tz=timezone.utc)
        else:
            fill_at = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
        hold_seconds = extract_strategy_exit_after_seconds(
            str(row["normalized_content"])
        )
        return now >= fill_at + timedelta(seconds=hold_seconds)

    def _next_signal_id(self, *, now: datetime) -> UUID | None:
        with self._risk_factory() as connection:
            connection = _effective(connection)
            rows = connection.execute(
                select(SIGNALS_TABLE)
                .where(
                    SIGNALS_TABLE.c.source_kind == "NATURAL_STRATEGY_SIGNAL",
                    SIGNALS_TABLE.c.acceptance_trigger_id.is_(None),
                    SIGNALS_TABLE.c.created_at >= now - MAXIMUM_SIGNAL_AGE,
                )
                .order_by(SIGNALS_TABLE.c.created_at.desc())
            ).mappings().all()
            for row in rows:
                used = connection.execute(
                    select(func.count()).select_from(TRADE_INTENTS_TABLE).where(
                        TRADE_INTENTS_TABLE.c.signal_id == row["id"],
                        TRADE_INTENTS_TABLE.c.intent_mode == INTENT_MODE_CONTINUOUS_OPEN,
                    )
                ).scalar_one()
                if int(used) == 0:
                    return row["id"]
        return None

    def _prepare_and_dispatch(
        self,
        risk_decision_id: UUID,
        attestation_id: UUID,
        *,
        signal_id: UUID | None,
        now: datetime,
    ) -> ContinuousDemoSoakResult:
        with self._order_factory() as connection:
            prepared = prepare_continuous_demo_order(
                connection,
                risk_decision_id=risk_decision_id,
                attestation_id=attestation_id,
                writer_identity="canonical_order_writer",
                holder_identity=CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
                holder_token_digest=self._holder_token_digest,
                idempotency_key=f"continuous-demo-{risk_decision_id}",
                evaluated_at=now,
            )
        try:
            with self._session_factory() as session:
                dispatched = dispatch_continuous_demo_order(
                    self._order_factory,
                    order_id=prepared.order_id,
                    transport=session,
                    holder_identity=CANONICAL_ORDER_WRITER_PROCESS_IDENTITY,
                    holder_token_digest=self._holder_token_digest,
                    lease_generation=prepared.lease_generation,
                    evaluated_at=now,
                )
        finally:
            self._release_lease(now=now)
        return _result(
            status="ADVANCED",
            action="OPEN_LONG" if signal_id is not None else "CLOSE_LONG",
            reason="NATURAL_SIGNAL_DISPATCHED" if signal_id is not None else "DUE_EXIT_DISPATCHED",
            order_id=dispatched.order_id,
            signal_id=signal_id,
            risk_decision_id=risk_decision_id,
            repeat_noop=dispatched.repeat_noop,
        )

    def _grant_open(self, signal_id: UUID, *, now: datetime) -> ContinuousDemoSoakResult:
        deployment_id = self._active_deployment_id()
        probe, attestation, receipt = self._fresh_flat_probe(
            deployment_id=deployment_id, now=now
        )
        # The authenticated exchange snapshots are observed after the tick's
        # initial timestamp. Bind the grant and order authority checks to that
        # newer observation, matching the position-exit path below.
        operation_now = max(now, probe.observed_at)
        with self._risk_factory() as connection:
            grant = grant_continuous_open(
                connection,
                signal_id=signal_id,
                probe_receipt_id=receipt.probe_receipt_id,
                evaluated_at=operation_now,
            )
        return self._prepare_and_dispatch(
            grant.risk_decision_id,
            attestation.attestation_id,
            signal_id=signal_id,
            now=operation_now,
        )

    def tick(
        self, *, openings_enabled: bool, evaluated_at: datetime | None = None
    ) -> ContinuousDemoSoakResult:
        now = _utc(evaluated_at)
        order, state = self._execution_state()
        if order is not None:
            if state == "DISPATCH":
                return self._dispatch_existing(order, now=now)
            if state == "FILL":
                return self._fill(order, now=now)
            if state == "LEDGER":
                return self._ledger(order)
            if state == "RECONCILE":
                return self._reconcile(order, now=now)
        net = self._ledger_net()
        if net < 0:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_CONTINUOUS_NEGATIVE_POSITION", "canonical ledger is short"
            )
        if net > 0:
            entry_order_id = self._entry_order_id()
            if not self._exit_due(entry_order_id, now=now):
                return _result(
                    status="NO_ACTION",
                    action="HOLD_POSITION",
                    reason="QUALIFIED_EXIT_NOT_DUE",
                    order_id=entry_order_id,
                )
            return self._grant_exit(now=now)
        if not openings_enabled:
            return _result(
                status="DRAINED",
                action="NO_ACTION",
                reason="OPENINGS_DISABLED_AND_FLAT",
            )
        signal_id = self._next_signal_id(now=now)
        if signal_id is None:
            return _result(
                status="NO_ACTION",
                action="NO_ACTION",
                reason="NO_FRESH_UNCONSUMED_NATURAL_SIGNAL",
            )
        return self._grant_open(signal_id, now=now)


__all__ = [
    "ContinuousDemoSoakOperator",
    "ContinuousDemoSoakResult",
    "MARKET_FILL_TIMEOUT",
]
