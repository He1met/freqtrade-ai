from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import secrets
from typing import Any, Mapping, Optional

from sqlalchemy import select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.okx_demo.order_writer import (
    ManagedOrder,
    WriteAttemptRecord,
    WriterOperation,
)
from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.adapters.okx_demo.writer_models import (
    ClaimedApprovedExecution,
    NormalizedOrderCommand,
)
from app.adapters.okx_demo.writer_state import (
    WriteEvent,
    WriteState,
    transition_write_state,
)
from app.models.execution_lineage import (
    ApprovedExecution,
    ExchangeOrder,
    ExecutionScope,
    OkxDemoAttestedSession,
    OkxDemoTrustedSnapshot,
    RiskDecision,
    TradeIntent,
    ReconciliationRun,
)
from app.models.okx_demo_reconciliation import (
    OkxDemoPositionSnapshot,
    OkxDemoReconciliationState,
    OkxDemoRecoveryGrant,
)
from app.models.order_writer import OkxOrderWriteAttempt, OkxOrderWriterLease
from app.services.risk_chain import RiskChainBlocked, RiskChainService, canonical_digest
from app.services.okx_demo_reconciliation import (
    OkxDemoReconciliationBlocked,
    OkxDemoReconciliationService,
)


OKX_DEMO = "OKX_DEMO"
UNRESOLVED_STATES = (
    WriteState.PREPARED.value,
    WriteState.ACKNOWLEDGED.value,
    WriteState.RECOVERY_REQUIRED.value,
    WriteState.RESIDUAL_CLOSE_REQUIRED.value,
)
PLACEMENT_OPERATIONS = ("PLACE", "CLOSE")


class SqlAlchemyOrderWriterStore:
    """Commit-per-transition store; no transaction spans an exchange request."""

    def __init__(
        self,
        db: Session,
        *,
        now_provider=lambda: datetime.now(timezone.utc),
        pinned_connection: Optional[Connection] = None,
    ) -> None:
        self.db = db
        self._now_provider = now_provider
        self._pinned_connection = pinned_connection
        self._holder_digest: Optional[str] = None
        self._lease_generation: Optional[int] = None
        self._process_token_digest = hashlib.sha256(
            secrets.token_bytes(32)
        ).hexdigest()

    def load_approved_execution(self, approval_id: int) -> ClaimedApprovedExecution:
        try:
            now = self._now()
            self._claim_active_approval(approval_id, now=now)
            approved, intent, decision = self._approval_lineage(
                approval_id,
                for_update=False,
            )
            self._validate_approval(approved, intent, decision, now=now)
            expires_at = min(
                value
                for value in (approved.expires_at, intent.expires_at)
                if value is not None
            )
            claimed = ClaimedApprovedExecution(
                approval_id=approved.id,
                trade_intent_id=intent.id,
                risk_decision_id=decision.id,
                execution_target_id=OKX_DEMO,
                authorization_schema_version="RISK_V1",
                canonical_hash=approved.canonical_hash,
                policy_digest=approved.policy_digest,
                approved_payload_hash=approved.approved_payload_hash,
                client_order_id=intent.client_order_id,
                instrument_id=intent.instrument_id,
                side=intent.side,
                position_side=intent.position_side,
                order_type=intent.order_type,
                contracts=intent.quantity,
                limit_price=intent.limit_price,
                reduce_only=bool(intent.reduce_only),
                margin_mode=intent.margin_mode,
                leverage=intent.leverage,
                approved_at=_aware_utc(approved.created_at),
                expires_at=_aware_utc(expires_at),
                policy_version=decision.policy_version,
                idempotency_digest=intent.idempotency_key_digest,
                take_profit_trigger_price=intent.take_profit,
                take_profit_order_price=(
                    Decimal("-1") if intent.take_profit is not None else None
                ),
                stop_loss_trigger_price=intent.stop_loss,
                stop_loss_order_price=(
                    Decimal("-1") if intent.stop_loss is not None else None
                ),
            )
            self.db.commit()
            return claimed
        except Exception:
            self.db.rollback()
            raise

    def acquire_recovery_lease(
        self,
        grant_database_id: int,
        *,
        now: datetime,
    ) -> None:
        """Fence recovery writes without depending on an expired approval."""

        now = _aware_utc(now)
        digest = self._process_token_digest
        try:
            self._lock_lease_key()
            self._require_target_contract()
            grant = self.db.scalars(
                select(OkxDemoRecoveryGrant)
                .where(
                    OkxDemoRecoveryGrant.database_id == grant_database_id,
                    OkxDemoRecoveryGrant.execution_target_id == OKX_DEMO,
                )
                .with_for_update()
            ).first()
            resumable_attempt = (
                self.db.scalars(
                    select(OkxOrderWriteAttempt).where(
                        OkxOrderWriteAttempt.recovery_grant_database_id
                        == grant_database_id,
                        OkxOrderWriteAttempt.state.in_(UNRESOLVED_STATES),
                    )
                ).first()
                if grant is not None and grant.status == "CONSUMED"
                else None
            )
            if (
                grant is None
                or (
                    grant.status == "ACTIVE"
                    and _aware_utc(grant.expires_at) <= now
                )
                or (
                    grant.status != "ACTIVE"
                    and resumable_attempt is None
                )
            ):
                raise OkxDemoWriteBlocked("recovery grant is not active")
            lease_expires_at = (
                _aware_utc(grant.expires_at)
                if grant.status == "ACTIVE"
                else now + timedelta(seconds=30)
            )
            lease = self.db.scalars(
                select(OkxOrderWriterLease)
                .where(OkxOrderWriterLease.execution_target_id == OKX_DEMO)
                .with_for_update()
            ).first()
            if lease is None:
                lease = OkxOrderWriterLease(
                    execution_target_id=OKX_DEMO,
                    holder_token_digest=digest,
                    generation=1,
                    acquired_at=now,
                    heartbeat_at=now,
                    expires_at=lease_expires_at,
                )
                self.db.add(lease)
            elif lease.holder_token_digest == digest:
                lease.heartbeat_at = now
                lease.expires_at = lease_expires_at
            elif _aware_utc(lease.expires_at) <= now:
                lease.holder_token_digest = digest
                lease.generation += 1
                lease.acquired_at = now
                lease.heartbeat_at = now
                lease.expires_at = lease_expires_at
            else:
                raise OkxDemoWriteBlocked(
                    "another OKX_DEMO writer holds the database lease"
                )
            self.db.commit()
            self._holder_digest = digest
            self._lease_generation = lease.generation
        except Exception:
            self.db.rollback()
            raise

    def load_recovery_order(self, grant_database_id: int) -> ManagedOrder:
        self._require_lease()
        grant = self.db.get(OkxDemoRecoveryGrant, grant_database_id)
        if (
            grant is None
            or grant.action != "CANCEL"
            or grant.exchange_order_row_id is None
        ):
            self.db.rollback()
            raise OkxDemoWriteBlocked("recovery cancel grant is invalid")
        order = self.db.get(ExchangeOrder, grant.exchange_order_row_id)
        intent = (
            self.db.get(TradeIntent, order.trade_intent_id)
            if order is not None
            else None
        )
        approval = (
            self.db.scalars(
                select(ApprovedExecution)
                .where(ApprovedExecution.trade_intent_id == intent.id)
                .order_by(ApprovedExecution.id.desc())
            ).first()
            if intent is not None
            else None
        )
        if order is None or intent is None:
            self.db.rollback()
            raise OkxDemoWriteBlocked("recovery cancel lineage is missing")
        managed = self._managed_order(
            order,
            intent,
            approval.id if approval is not None else intent.id,
        )
        self.db.commit()
        return managed

    def prepare_recovery_close(
        self,
        grant_database_id: int,
    ) -> tuple[ManagedOrder, WriteAttemptRecord, Mapping[str, Any]]:
        """Atomically persist a close attempt and consume its exact grant."""

        self._require_lease()
        try:
            now = self._now()
            grant = self.db.get(OkxDemoRecoveryGrant, grant_database_id)
            run = (
                self.db.get(ReconciliationRun, grant.reconciliation_run_id)
                if grant is not None
                else None
            )
            position_ids = (
                (run.database_ids or {}).get("position_snapshots", [])
                if run is not None
                else []
            )
            position = self.db.scalars(
                select(OkxDemoPositionSnapshot).where(
                    OkxDemoPositionSnapshot.database_id.in_(position_ids),
                    OkxDemoPositionSnapshot.instrument_id
                    == (grant.instrument_id if grant is not None else ""),
                    OkxDemoPositionSnapshot.position_side
                    == (grant.position_side if grant is not None else ""),
                )
            ).first()
            quantity = (
                abs(Decimal(position.quantity))
                if position is not None
                else Decimal("0")
            )
            if (
                grant is None
                or grant.action != "REDUCE_ONLY"
                or position is None
                or grant.position_side not in ("long", "short")
                or position.position_side != grant.position_side
                or quantity <= 0
                or quantity > Decimal(grant.max_quantity)
            ):
                raise OkxDemoWriteBlocked(
                    "recovery close authoritative position is invalid"
                )
            intent = self.db.scalars(
                select(TradeIntent)
                .where(
                    TradeIntent.execution_target_id == OKX_DEMO,
                    TradeIntent.instrument_id == grant.instrument_id,
                    TradeIntent.position_side == grant.position_side,
                )
                .order_by(TradeIntent.id.desc())
            ).first()
            approval = (
                self.db.scalars(
                    select(ApprovedExecution)
                    .where(ApprovedExecution.trade_intent_id == intent.id)
                    .order_by(ApprovedExecution.id.desc())
                ).first()
                if intent is not None
                else None
            )
            if intent is None:
                raise OkxDemoWriteBlocked(
                    "recovery close has no persisted local lineage"
                )
            lineage_id = approval.id if approval is not None else intent.id
            client_order_id = "rcv{:020d}".format(grant.database_id)
            # In long/short mode the close direction is derived from the
            # persisted position identity, never from a signed/net quantity.
            side = "sell" if grant.position_side == "long" else "buy"
            body = {
                "instId": grant.instrument_id,
                "tdMode": "isolated",
                "side": side,
                "posSide": grant.position_side,
                "ordType": "market",
                "sz": format(quantity, "f"),
                "clOrdId": client_order_id,
                "reduceOnly": True,
            }
            order_row = ExchangeOrder(
                execution_target_id=OKX_DEMO,
                trade_intent_id=intent.id,
                client_order_id=client_order_id,
                status=WriteState.PREPARED.value,
                request_snapshot=dict(body),
                response_snapshot={},
            )
            self.db.add(order_row)
            self.db.flush()
            attempt = self._new_attempt(
                order_row,
                lineage_id,
                operation="CLOSE",
                operation_id=client_order_id,
                request_digest=hashlib.sha256(
                    json.dumps(
                        body, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
                safe_request_snapshot=body,
                now=now,
                recovery_grant_database_id=grant_database_id,
            )
            claim = OkxDemoReconciliationService(
                self.db
            ).claim_recovery_grant(
                grant_database_id,
                action="REDUCE_ONLY",
                quantity=quantity,
                now=now,
            )
            if (
                claim.get("instrument_id") != grant.instrument_id
                or claim.get("position_side") != grant.position_side
            ):
                raise OkxDemoWriteBlocked(
                    "recovery close grant identity changed"
                )
            self.db.commit()
            return (
                self._managed_order(order_row, intent, lineage_id),
                self._record(attempt),
                body,
            )
        except (
            OkxDemoWriteBlocked,
            OkxDemoReconciliationBlocked,
            IntegrityError,
        ):
            self.db.rollback()
            raise OkxDemoWriteBlocked(
                "recovery close could not be prepared atomically"
            ) from None

    def acquire_lease(
        self,
        *,
        writer_instance_id: str,
        approval_id: int,
        canonical_hash: str,
        policy_digest: str,
        approved_payload_hash: str,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        now = _aware_utc(now)
        expires_at = _aware_utc(expires_at)
        if expires_at <= now:
            raise OkxDemoWriteBlocked("writer lease cannot outlive expired authorization")
        if not writer_instance_id:
            raise OkxDemoWriteBlocked("writer instance identity is missing")
        digest = self._process_token_digest
        try:
            self._claim_active_approval(approval_id, now=now)
            self._lock_lease_key()
            self._require_target_contract()
            approved, intent, decision = self._approval_lineage(
                approval_id,
                for_update=True,
            )
            self._validate_approval(approved, intent, decision, now=now)
            if (
                canonical_hash != approved.canonical_hash
                or policy_digest != approved.policy_digest
                or approved_payload_hash != approved.approved_payload_hash
            ):
                raise OkxDemoWriteBlocked(
                    "writer lease authorization differs from persisted approval"
                )
            expires_at = min(
                expires_at,
                *(
                    _aware_utc(value)
                    for value in (approved.expires_at, intent.expires_at)
                    if value is not None
                ),
            )
            unresolved = list(
                self.db.scalars(
                    select(OkxOrderWriteAttempt)
                    .where(
                        OkxOrderWriteAttempt.execution_target_id == OKX_DEMO,
                        OkxOrderWriteAttempt.state.in_(UNRESOLVED_STATES),
                    )
                    .order_by(OkxOrderWriteAttempt.id)
                    .with_for_update()
                )
            )
            if len(unresolved) > 1:
                raise OkxDemoWriteBlocked(
                    "multiple unresolved writer attempts exist"
                )
            if unresolved and unresolved[0].approval_id != approved.id:
                raise OkxDemoWriteBlocked(
                    "unresolved attempt belongs to another approval"
                )
            lease = self.db.scalars(
                select(OkxOrderWriterLease)
                .where(OkxOrderWriterLease.execution_target_id == OKX_DEMO)
                .with_for_update()
            ).first()
            if lease is None:
                lease = OkxOrderWriterLease(
                    execution_target_id=OKX_DEMO,
                    holder_token_digest=digest,
                    generation=1,
                    acquired_at=now,
                    heartbeat_at=now,
                    expires_at=expires_at,
                )
                self.db.add(lease)
            elif lease.holder_token_digest == digest:
                lease.heartbeat_at = now
                lease.expires_at = expires_at
            elif _aware_utc(lease.expires_at) <= now:
                lease.holder_token_digest = digest
                lease.generation += 1
                lease.acquired_at = now
                lease.heartbeat_at = now
                lease.expires_at = expires_at
            else:
                raise OkxDemoWriteBlocked(
                    "another OKX_DEMO writer holds the database lease"
                )
            lease_generation = lease.generation
            self.db.commit()
            self._holder_digest = digest
            self._lease_generation = lease_generation
        except OkxDemoWriteBlocked:
            self.db.rollback()
            raise
        except IntegrityError:
            self.db.rollback()
            raise OkxDemoWriteBlocked(
                "another OKX_DEMO writer acquired the database lease"
            ) from None

    def unresolved(self) -> Optional[WriteAttemptRecord]:
        self._require_lease()
        rows = list(
            self.db.scalars(
                select(OkxOrderWriteAttempt)
                .where(
                    OkxOrderWriteAttempt.execution_target_id == OKX_DEMO,
                    OkxOrderWriteAttempt.state.in_(UNRESOLVED_STATES),
                )
                .order_by(OkxOrderWriteAttempt.id)
                .with_for_update()
            ).all()
        )
        if len(rows) > 1:
            self.db.rollback()
            raise OkxDemoWriteBlocked("multiple unresolved writer attempts exist")
        if rows and rows[0].lease_generation != self._lease_generation:
            rows[0].lease_generation = self._lease_generation
            self.db.flush()
        record = self._record(rows[0]) if rows else None
        self.db.commit()
        return record

    def prepare_place(
        self,
        command: NormalizedOrderCommand,
        *,
        operation: WriterOperation,
        operation_id: str,
        request_digest: str,
        safe_request_snapshot: Mapping[str, Any],
    ) -> tuple[ManagedOrder, WriteAttemptRecord]:
        self._require_lease()
        try:
            self._require_reconciliation_allows(operation)
            approved, intent, decision = self._approval_lineage(
                command.approval_id,
                for_update=True,
            )
            now = self._now()
            self._validate_approval(approved, intent, decision, now=now)
            self._validate_command(command, approved, intent, decision)
            prior_placement = self.db.scalars(
                select(OkxOrderWriteAttempt).where(
                    OkxOrderWriteAttempt.approval_id == approved.id,
                    OkxOrderWriteAttempt.operation.in_(PLACEMENT_OPERATIONS),
                )
            ).first()
            if prior_placement is not None:
                raise OkxDemoWriteBlocked("approved execution is already consumed")
            order_row = self.db.scalars(
                select(ExchangeOrder)
                .where(
                    ExchangeOrder.execution_target_id == OKX_DEMO,
                    ExchangeOrder.client_order_id == command.client_order_id,
                )
                .with_for_update()
            ).first()
            if order_row is None:
                order_row = ExchangeOrder(
                    execution_target_id=OKX_DEMO,
                    trade_intent_id=intent.id,
                    client_order_id=command.client_order_id,
                    status="PREPARED",
                    request_snapshot=dict(safe_request_snapshot),
                    response_snapshot={},
                )
                self.db.add(order_row)
                self.db.flush()
            elif order_row.trade_intent_id != intent.id:
                raise OkxDemoWriteBlocked("persisted order lineage is inconsistent")
            attempt = self._new_attempt(
                order_row,
                approved.id,
                operation=operation,
                operation_id=operation_id,
                request_digest=request_digest,
                safe_request_snapshot=safe_request_snapshot,
                now=now,
            )
            self.db.commit()
            return (
                self._managed_order(order_row, intent, approved.id),
                self._record(attempt),
            )
        except (OkxDemoWriteBlocked, IntegrityError):
            self.db.rollback()
            raise OkxDemoWriteBlocked(
                "approved execution cannot be claimed for this operation"
            ) from None

    def prepare_existing(
        self,
        order: ManagedOrder,
        *,
        operation: WriterOperation,
        operation_id: str,
        request_digest: str,
        safe_request_snapshot: Mapping[str, Any],
        recovery_grant_database_id: Optional[int] = None,
    ) -> WriteAttemptRecord:
        self._require_lease()
        try:
            self._require_reconciliation_allows(operation)
            row = self.db.get(ExchangeOrder, order.exchange_order_row_id)
            approved, intent, decision = self._approval_lineage(
                order.approval_id,
                for_update=True,
            )
            now = self._now()
            if recovery_grant_database_id is None:
                self._validate_approval(approved, intent, decision, now=now)
            if (
                row is None
                or row.execution_target_id != OKX_DEMO
                or row.trade_intent_id != intent.id
                or row.client_order_id != order.client_order_id
                or order.canonical_hash != intent.canonical_hash
                or order.policy_digest != intent.policy_digest
                or order.approved_payload_hash != intent.approved_payload_hash
                or order.instrument_id != intent.instrument_id
                or order.side != intent.side
                or order.order_type != intent.order_type
                or order.contracts != intent.quantity
                or order.limit_price != intent.limit_price
                or order.reduce_only is not bool(intent.reduce_only)
            ):
                raise OkxDemoWriteBlocked("existing order lineage is inconsistent")
            attempt = self._new_attempt(
                row,
                approved.id,
                operation=operation,
                operation_id=operation_id,
                request_digest=request_digest,
                safe_request_snapshot=safe_request_snapshot,
                now=now,
                recovery_grant_database_id=recovery_grant_database_id,
            )
            if recovery_grant_database_id is not None:
                if operation != "CANCEL":
                    raise OkxDemoWriteBlocked(
                        "recovery grant action is not supported by this operation"
                    )
                claim = OkxDemoReconciliationService(
                    self.db
                ).claim_recovery_grant(
                    recovery_grant_database_id,
                    action="CANCEL",
                    quantity=Decimal("0"),
                    now=now,
                )
                if (
                    claim.get("exchange_order_row_id")
                    != order.exchange_order_row_id
                ):
                    raise OkxDemoWriteBlocked(
                        "recovery grant belongs to another exchange order"
                    )
            self.db.commit()
            return self._record(attempt)
        except (
            OkxDemoWriteBlocked,
            OkxDemoReconciliationBlocked,
            IntegrityError,
        ):
            self.db.rollback()
            raise OkxDemoWriteBlocked(
                "order operation could not be prepared exactly once"
            ) from None

    def prepare_close_cleanup(
        self,
        parent: WriteAttemptRecord,
        command: NormalizedOrderCommand,
        *,
        operation_id: str,
        request_digest: str,
        safe_request_snapshot: Mapping[str, Any],
    ) -> tuple[ManagedOrder, WriteAttemptRecord]:
        self._require_lease()
        try:
            parent_row = self.db.scalars(
                select(OkxOrderWriteAttempt)
                .where(OkxOrderWriteAttempt.id == parent.attempt_id)
                .with_for_update()
            ).one()
            if (
                parent_row.operation != "CLOSE"
                or parent_row.state != WriteState.RESIDUAL_CLOSE_REQUIRED.value
                or parent_row.lease_generation != self._lease_generation
                or parent_row.close_sequence + 1 > 3
            ):
                raise OkxDemoWriteBlocked("residual close parent is not claimable")
            approved, intent, decision = self._approval_lineage(
                parent_row.approval_id,
                for_update=True,
            )
            now = self._now()
            self._validate_approval(approved, intent, decision, now=now)
            self._validate_cleanup_command(command, approved, intent, decision)
            prior_close_attempts = list(
                self.db.scalars(
                    select(OkxOrderWriteAttempt).where(
                        OkxOrderWriteAttempt.approval_id == approved.id,
                        OkxOrderWriteAttempt.operation == "CLOSE",
                    )
                )
            )
            filled_contracts = sum(
                (
                    Decimal(
                        str(
                            (item.safe_response_snapshot or {}).get(
                                "accumulated_fill_size",
                                "0",
                            )
                        )
                    )
                    for item in prior_close_attempts
                ),
                Decimal("0"),
            )
            remaining_approved_contracts = Decimal(intent.quantity) - filled_contracts
            if (
                remaining_approved_contracts <= 0
                or command.contracts != remaining_approved_contracts
            ):
                raise OkxDemoWriteBlocked(
                    "residual close does not equal the remaining approved quantity"
                )
            parent_row.state = WriteState.RECONCILED.value
            parent_row.order_state = "residual_cleanup_started"
            parent_row.reason_code = "SUPERSEDED_BY_CLOSE_CLEANUP"
            cleanup_order = ExchangeOrder(
                execution_target_id=OKX_DEMO,
                trade_intent_id=intent.id,
                client_order_id=command.client_order_id,
                status=WriteState.PREPARED.value,
                request_snapshot=dict(safe_request_snapshot),
                response_snapshot={},
            )
            self.db.add(cleanup_order)
            self.db.flush()
            attempt = self._new_attempt(
                cleanup_order,
                approved.id,
                operation="CLOSE",
                operation_id=operation_id,
                request_digest=request_digest,
                safe_request_snapshot=safe_request_snapshot,
                now=now,
                parent_attempt_id=parent_row.id,
                close_sequence=parent_row.close_sequence + 1,
            )
            self.db.commit()
            return (
                self._managed_order(cleanup_order, intent, approved.id),
                self._record(attempt),
            )
        except (OkxDemoWriteBlocked, IntegrityError):
            self.db.rollback()
            raise OkxDemoWriteBlocked(
                "residual close cleanup could not be prepared"
            ) from None

    def transition(
        self,
        attempt: WriteAttemptRecord,
        *,
        event: WriteEvent,
        exchange_order_id: Optional[str] = None,
        order_state: Optional[str] = None,
        reason_code: Optional[str] = None,
        safe_response_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> WriteAttemptRecord:
        self._require_lease()
        row = self.db.scalars(
            select(OkxOrderWriteAttempt)
            .where(OkxOrderWriteAttempt.id == attempt.attempt_id)
            .with_for_update()
        ).first()
        if row is None or row.state != attempt.state.value:
            self.db.rollback()
            raise OkxDemoWriteBlocked("writer attempt state changed concurrently")
        if row.lease_generation != self._lease_generation:
            self.db.rollback()
            raise OkxDemoWriteBlocked("writer attempt fencing token is stale")
        row.state = transition_write_state(WriteState(row.state), event).value
        row.reason_code = reason_code
        row.order_state = order_state or row.order_state
        row.last_attempt_at = self._now()
        if safe_response_snapshot:
            row.safe_response_snapshot = {
                **dict(row.safe_response_snapshot or {}),
                **dict(safe_response_snapshot),
            }
        order_row = self.db.get(ExchangeOrder, row.exchange_order_row_id)
        if order_row is None:
            self.db.rollback()
            raise OkxDemoWriteBlocked("writer attempt order is missing")
        if exchange_order_id is not None:
            order_row.exchange_order_id = exchange_order_id
        order_row.status = order_state or row.state
        order_row.response_snapshot = dict(row.safe_response_snapshot or {})
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise OkxDemoWriteBlocked("writer transition violated persistence") from None
        return self._record(row)

    def order_for_attempt(self, attempt: WriteAttemptRecord) -> ManagedOrder:
        row = self.db.get(ExchangeOrder, attempt.exchange_order_row_id)
        if row is None:
            self.db.rollback()
            raise OkxDemoWriteBlocked("writer attempt order is missing")
        intent = self.db.get(TradeIntent, row.trade_intent_id)
        if intent is None:
            self.db.rollback()
            raise OkxDemoWriteBlocked("writer attempt intent is missing")
        managed = self._managed_order(
            row,
            intent,
            self._attempt(attempt.attempt_id).approval_id,
        )
        self.db.commit()
        return managed

    def _new_attempt(
        self,
        order: ExchangeOrder,
        approval_id: int,
        *,
        operation: WriterOperation,
        operation_id: str,
        request_digest: str,
        safe_request_snapshot: Mapping[str, Any],
        now: datetime,
        parent_attempt_id: Optional[int] = None,
        close_sequence: int = 0,
        recovery_grant_database_id: Optional[int] = None,
    ) -> OkxOrderWriteAttempt:
        attempt = OkxOrderWriteAttempt(
            execution_target_id=OKX_DEMO,
            exchange_order_row_id=order.id,
            approval_id=approval_id,
            recovery_grant_database_id=recovery_grant_database_id,
            operation=operation,
            operation_id=operation_id,
            client_order_id=order.client_order_id,
            instrument_id=self.db.get(TradeIntent, order.trade_intent_id).instrument_id,
            state=WriteState.PREPARED.value,
            request_digest=request_digest,
            safe_request_snapshot=dict(safe_request_snapshot),
            safe_response_snapshot={},
            attempt_count=1,
            lease_generation=self._lease_generation,
            parent_attempt_id=parent_attempt_id,
            close_sequence=close_sequence,
            last_attempt_at=now,
        )
        self.db.add(attempt)
        self.db.flush()
        if recovery_grant_database_id is None:
            order.status = WriteState.PREPARED.value
            order.request_snapshot = dict(safe_request_snapshot)
        return attempt

    def _approval_lineage(
        self,
        approval_id: int,
        *,
        for_update: bool,
    ) -> tuple[ApprovedExecution, TradeIntent, RiskDecision]:
        statement = (
            select(ApprovedExecution, TradeIntent, RiskDecision)
            .join(TradeIntent, TradeIntent.id == ApprovedExecution.trade_intent_id)
            .join(RiskDecision, RiskDecision.id == ApprovedExecution.risk_decision_id)
            .where(ApprovedExecution.id == approval_id)
        )
        if for_update and self.db.get_bind().dialect.name != "postgresql":
            statement = statement.with_for_update()
        row = self.db.execute(statement).first()
        if row is None:
            raise OkxDemoWriteBlocked("approved execution lineage is missing")
        return row[0], row[1], row[2]

    def _claim_active_approval(self, approval_id: int, *, now: datetime) -> None:
        if self.db.get_bind().dialect.name != "postgresql":
            try:
                claimed = RiskChainService(self.db).claim_active_approval(
                    approval_id,
                    now=now,
                )
            except RiskChainBlocked as exc:
                raise OkxDemoWriteBlocked(
                    "approved execution claim could not be verified"
                ) from exc
            if claimed is None:
                raise OkxDemoWriteBlocked(
                    "approved execution claim is no longer active"
                )
            return
        approved, intent, decision = self._approval_lineage(
            approval_id,
            for_update=False,
        )
        expiries = (approved.expires_at, intent.expires_at)
        if any(
            value is None or _aware_utc(value) <= now
            for value in expiries
        ):
            if any(
                value is not None and _aware_utc(value) <= now
                for value in expiries
            ):
                self.db.execute(
                    text(
                        "SELECT release_expired_okx_demo_approval("
                        ":approval_id)"
                    ),
                    {"approval_id": approval_id},
                )
                # The controlled function owns the complete release mutation.
                # Persist it before reporting the fail-closed claim result.
                self.db.commit()
            raise OkxDemoWriteBlocked(
                "approved execution claim is no longer active"
            )
        self._validate_approval(
            approved,
            intent,
            decision,
            now=now,
        )

    def _require_reconciliation_allows(self, operation: WriterOperation) -> None:
        state = self.db.scalars(
            select(OkxDemoReconciliationState).where(
                OkxDemoReconciliationState.execution_target_id == OKX_DEMO
            )
        ).first()
        if (
            state is not None
            and state.opening_frozen
            and operation not in {"CANCEL", "CLOSE"}
        ):
            raise OkxDemoWriteBlocked(
                "reconciliation gate freezes opening-risk operations"
            )

    def _validate_approval(
        self,
        approved: ApprovedExecution,
        intent: TradeIntent,
        decision: RiskDecision,
        *,
        now: datetime,
    ) -> None:
        self._require_target_contract()
        try:
            RiskChainService(self.db).require_completed_full_chain_binding(
                approved=approved,
                intent=intent,
                decision=decision,
            )
        except RiskChainBlocked as exc:
            raise OkxDemoWriteBlocked(
                "approved execution full-chain RISK checkpoint is incomplete"
            ) from exc
        values = (
            approved.execution_target_id,
            intent.execution_target_id,
            decision.execution_target_id,
        )
        if any(value != OKX_DEMO for value in values):
            raise OkxDemoWriteBlocked("approved lineage target is inconsistent")
        if (
            approved.trade_intent_id != intent.id
            or approved.risk_decision_id != decision.id
            or decision.trade_intent_id != intent.id
            or approved.intent_id != intent.intent_id
            or approved.client_order_id != intent.client_order_id
            or approved.decision != "APPROVED"
            or approved.intent_status != "APPROVED"
            or approved.status != "ACTIVE"
            or approved.authorization_schema_version != "RISK_V1"
            or intent.authorization_schema_version != "RISK_V1"
            or decision.authorization_schema_version != "RISK_V1"
            or approved.canonical_hash != intent.canonical_hash
            or approved.policy_digest != intent.policy_digest
            or approved.policy_digest != decision.policy_digest
            or approved.approved_payload_hash != intent.approved_payload_hash
            or intent.status != "APPROVED"
            or decision.decision != "APPROVED"
            or approved.claim_required is not True
            or approved.order_submission_authorized is not False
        ):
            raise OkxDemoWriteBlocked("approved execution lineage is not active")
        expiries = [approved.expires_at, intent.expires_at]
        if any(value is None or _aware_utc(value) <= now for value in expiries):
            raise OkxDemoWriteBlocked("approved execution lineage is expired")
        required = (
            intent.intent_id,
            intent.policy_digest,
            intent.idempotency_key_digest,
            intent.instrument_id,
            intent.side,
            intent.position_side,
            intent.order_type,
            intent.quantity,
            intent.margin_mode,
            intent.leverage,
        )
        if any(value is None for value in required):
            raise OkxDemoWriteBlocked("approved execution fields are incomplete")
        self._validate_trusted_snapshots(approved, intent, now=now)
        self._validate_approved_payload(approved, intent, decision)

    @staticmethod
    def _validate_approved_payload(
        approved: ApprovedExecution,
        intent: TradeIntent,
        decision: RiskDecision,
    ) -> None:
        request_snapshot = intent.request_snapshot or {}
        canonical_input = request_snapshot.get("canonical_input")
        snapshot_evidence = request_snapshot.get("snapshot_evidence")
        lineage = (decision.evidence_snapshot or {}).get("lineage")
        notional_value = (decision.evidence_snapshot or {}).get("notional")
        if (
            not isinstance(canonical_input, Mapping)
            or not isinstance(snapshot_evidence, Mapping)
            or not isinstance(lineage, Mapping)
            or notional_value is None
            or canonical_digest(canonical_input) != intent.canonical_hash
        ):
            raise OkxDemoWriteBlocked(
                "approved canonical payload evidence is incomplete"
            )
        try:
            notional = Decimal(str(notional_value))
        except (TypeError, ValueError):
            raise OkxDemoWriteBlocked(
                "approved payload notional is malformed"
            ) from None
        if (
            approved.approved_payload_hash != intent.approved_payload_hash
            or approved.canonical_hash != canonical_digest(canonical_input)
            or approved.policy_digest != decision.policy_digest
            or notional <= 0
            or Decimal(approved.reserved_notional) <= 0
        ):
            raise OkxDemoWriteBlocked("approved payload identity does not verify")

    @staticmethod
    def _validate_command(
        command: NormalizedOrderCommand,
        approved: ApprovedExecution,
        intent: TradeIntent,
        decision: RiskDecision,
    ) -> None:
        if (
            command.execution_target_id != OKX_DEMO
            or command.authorization_schema_version != "RISK_V1"
            or command.canonical_hash != approved.canonical_hash
            or command.policy_digest != approved.policy_digest
            or command.approved_payload_hash != approved.approved_payload_hash
            or command.approval_id != approved.id
            or command.trade_intent_id != intent.id
            or command.risk_decision_id != decision.id
            or command.client_order_id != intent.client_order_id
            or command.instrument_id != intent.instrument_id
            or command.side != intent.side
            or command.position_side != intent.position_side
            or command.order_type != intent.order_type
            or command.contracts != intent.quantity
            or command.limit_price != intent.limit_price
            or command.reduce_only is not bool(intent.reduce_only)
            or command.margin_mode != intent.margin_mode
            or command.leverage != intent.leverage
        ):
            raise OkxDemoWriteBlocked(
                "writer command differs from persisted approval"
            )

    @staticmethod
    def _validate_cleanup_command(
        command: NormalizedOrderCommand,
        approved: ApprovedExecution,
        intent: TradeIntent,
        decision: RiskDecision,
    ) -> None:
        if (
            command.execution_target_id != OKX_DEMO
            or command.authorization_schema_version != "RISK_V1"
            or command.canonical_hash != approved.canonical_hash
            or command.policy_digest != approved.policy_digest
            or command.approved_payload_hash != approved.approved_payload_hash
            or command.approval_id != approved.id
            or command.trade_intent_id != intent.id
            or command.risk_decision_id != decision.id
            or command.instrument_id != intent.instrument_id
            or command.side != intent.side
            or command.position_side != intent.position_side
            or command.contracts > intent.quantity
        ):
            raise OkxDemoWriteBlocked("cleanup command lineage is inconsistent")
        if (
            command.order_type != "market"
            or command.reduce_only is not True
            or command.margin_mode != "isolated"
            or command.leverage != intent.leverage
            or command.contracts <= 0
        ):
            raise OkxDemoWriteBlocked("cleanup command is not risk reducing")

    def _validate_trusted_snapshots(
        self,
        approved: ApprovedExecution,
        intent: TradeIntent,
        *,
        now: datetime,
    ) -> None:
        evidence = (intent.request_snapshot or {}).get("snapshot_evidence")
        if not isinstance(evidence, Mapping) or set(evidence) != {
            "instrument",
            "market",
            "account",
        }:
            raise OkxDemoWriteBlocked("trusted snapshot evidence is incomplete")
        approved_snapshot_ids = {
            "instrument": approved.instrument_snapshot_id,
            "market": approved.market_snapshot_id,
            "account": approved.account_snapshot_id,
        }
        for kind in ("instrument", "market", "account"):
            item = evidence.get(kind)
            if not isinstance(item, Mapping):
                raise OkxDemoWriteBlocked("trusted snapshot evidence is malformed")
            row = self.db.get(OkxDemoTrustedSnapshot, item.get("database_id"))
            attested_session = (
                self.db.get(OkxDemoAttestedSession, row.attested_session_id)
                if row is not None
                else None
            )
            if (
                row is None
                or row.kind != kind
                or row.snapshot_id != approved_snapshot_ids[kind]
                or row.snapshot_id != item.get("snapshot_id")
                or row.digest != item.get("digest")
                or row.digest != canonical_digest(row.content_json)
                or row.execution_target_id != OKX_DEMO
                or row.source_type != "api_aggregate"
                or row.core_data is not True
                or _aware_utc(row.expires_at) <= now
                or attested_session is None
                or attested_session.execution_target_id != OKX_DEMO
                or attested_session.revoked_at is not None
                or _aware_utc(attested_session.expires_at) <= now
                or (
                    row.attestation_fingerprint_sha256
                    != attested_session.pinned_fingerprint_sha256
                )
                or (
                    _aware_utc(row.attested_session_expires_at)
                    != _aware_utc(attested_session.expires_at)
                )
            ):
                raise OkxDemoWriteBlocked(
                    "trusted snapshot evidence is missing, stale, or changed"
                )

    def _require_target_contract(self) -> None:
        scope = self.db.get(ExecutionScope, OKX_DEMO)
        if (
            scope is None
            or scope.scope_kind != "EXCHANGE_TARGET"
            or scope.exchange_capable is not True
            or scope.executable is not False
            or scope.exchange_writes is not False
            or scope.order_submission_authorized is not False
        ):
            raise OkxDemoWriteBlocked(
                "OKX_DEMO target contract is missing or unsafe"
            )

    def _require_lease(self) -> OkxOrderWriterLease:
        self._require_pinned_connection()
        if self._holder_digest is None:
            raise OkxDemoWriteBlocked("writer database lease was not acquired")
        lease = self.db.get(
            OkxOrderWriterLease,
            OKX_DEMO,
            populate_existing=True,
        )
        if (
            lease is None
            or lease.holder_token_digest != self._holder_digest
            or lease.generation != self._lease_generation
            or _aware_utc(lease.expires_at) <= self._now()
        ):
            raise OkxDemoWriteBlocked("writer database lease is missing or expired")
        return lease

    def _require_pinned_connection(self) -> None:
        if self._pinned_connection is not None and (
            self._pinned_connection.closed
            or self.db.get_bind() is not self._pinned_connection
            or self.db.connection() is not self._pinned_connection
        ):
            raise OkxDemoWriteBlocked(
                "writer database connection identity changed"
            )

    def _lock_lease_key(self) -> None:
        self._require_pinned_connection()
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('OKX_DEMO-order-writer'))")
            )

    def _attempt(self, attempt_id: int) -> OkxOrderWriteAttempt:
        row = self.db.get(OkxOrderWriteAttempt, attempt_id)
        if row is None:
            raise OkxDemoWriteBlocked("writer attempt is missing")
        return row

    def _managed_order(
        self,
        order: ExchangeOrder,
        intent: TradeIntent,
        approval_id: int,
    ) -> ManagedOrder:
        return ManagedOrder(
            exchange_order_row_id=order.id,
            trade_intent_id=intent.id,
            approval_id=approval_id,
            canonical_hash=intent.canonical_hash,
            policy_digest=intent.policy_digest,
            approved_payload_hash=intent.approved_payload_hash,
            instrument_id=intent.instrument_id,
            client_order_id=order.client_order_id,
            exchange_order_id=order.exchange_order_id,
            side=intent.side,
            position_side=intent.position_side,
            order_type=intent.order_type,
            contracts=intent.quantity,
            limit_price=intent.limit_price,
            reduce_only=bool(intent.reduce_only),
        )

    @staticmethod
    def _record(row: OkxOrderWriteAttempt) -> WriteAttemptRecord:
        return WriteAttemptRecord(
            attempt_id=row.id,
            exchange_order_row_id=row.exchange_order_row_id,
            operation=row.operation,
            operation_id=row.operation_id,
            client_order_id=row.client_order_id,
            instrument_id=row.instrument_id,
            state=WriteState(row.state),
            request_digest=row.request_digest,
            safe_request_snapshot=dict(row.safe_request_snapshot),
            attempt_count=row.attempt_count,
            close_sequence=row.close_sequence,
            parent_attempt_id=row.parent_attempt_id,
            lease_generation=row.lease_generation,
            recovery_grant_database_id=row.recovery_grant_database_id,
        )

    def _now(self) -> datetime:
        return _aware_utc(self._now_provider())


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise OkxDemoWriteBlocked("writer timestamp is invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
