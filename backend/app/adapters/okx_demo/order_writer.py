from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import secrets
import threading
import time
from typing import Any, Literal, Mapping, Optional, Protocol

from app.adapters.okx_demo.models import InstrumentSpec
from app.adapters.okx_demo.read_adapter import OkxDemoReadClient
from app.adapters.okx_demo.write_semantics import (
    OkxDemoRecoveryRequired,
    OkxDemoTransportError,
    OkxDemoWriteBlocked,
    OkxDemoWriteRejected,
    validate_client_order_id,
    validate_write_item,
)
from app.adapters.okx_demo.write_transport import OkxDemoWriteTransport
from app.adapters.okx_demo.writer_models import (
    ApprovedExecution,
    NormalizedOrderCommand,
    OrderSubmissionAuthorization,
    approved_execution_view,
    normalize_order_command,
)
from app.adapters.okx_demo.writer_state import WriteEvent, WriteState


WriterOperation = Literal["SET_LEVERAGE", "PLACE", "CANCEL", "AMEND", "CLOSE"]
TERMINAL_ORDER_STATES = frozenset({"filled", "canceled", "mmp_canceled"})
MAX_CLOSE_CLEANUP_ATTEMPTS = 3
ATOMIC_POST_START_RESERVE_MS = 100


@dataclass(frozen=True)
class WriteAttemptRecord:
    attempt_id: int
    exchange_order_row_id: int
    operation: WriterOperation
    operation_id: str
    client_order_id: str
    instrument_id: str
    state: WriteState
    request_digest: str
    safe_request_snapshot: Mapping[str, Any]
    attempt_count: int
    close_sequence: int = 0
    parent_attempt_id: Optional[int] = None
    lease_generation: int = 1
    recovery_grant_database_id: Optional[int] = None


@dataclass(frozen=True)
class ManagedOrder:
    exchange_order_row_id: int
    trade_intent_id: int
    approval_id: int
    canonical_hash: str
    policy_digest: str
    approved_payload_hash: str
    instrument_id: str
    client_order_id: str
    exchange_order_id: Optional[str]
    side: Literal["buy", "sell"]
    position_side: Literal["long", "short"]
    order_type: Literal["limit", "post_only", "market"]
    contracts: Decimal
    limit_price: Optional[Decimal]
    reduce_only: bool


@dataclass(frozen=True)
class WriterResult:
    status: Literal[
        "RECONCILED",
        "REJECTED",
        "RECOVERY_REQUIRED",
        "RESIDUAL_CLOSE_REQUIRED",
    ]
    operation: WriterOperation
    attempt_id: int
    exchange_order_row_id: int
    client_order_id_sha256: str
    exchange_order_id_sha256: Optional[str]
    order_state: Optional[str]
    reason_code: Optional[str]


@dataclass(frozen=True)
class _AtomicDispatchCapability:
    issuer_nonce: str
    runtime_instance_id: str
    receipt: Mapping[str, Any]


class OrderWriterStore(Protocol):
    @property
    def atomic_process_token_digest(self) -> str: ...

    @property
    def atomic_process_token(self) -> str: ...

    def validate_atomic_dispatch_authority(
        self,
        *,
        attempt_id: int,
        runtime_instance_id: str,
        lease_generation: int,
        request_digest: str,
        bundle_digest: str,
    ) -> int: ...

    def load_atomic_attempt(self, attempt_id: int) -> WriteAttemptRecord: ...

    def claim_unresolved_for_reconciliation(
        self,
        attempt_id: int,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> WriteAttemptRecord: ...

    def acquire_recovery_lease(
        self,
        grant_database_id: int,
        *,
        now: datetime,
    ) -> None: ...

    def load_recovery_order(self, grant_database_id: int) -> ManagedOrder: ...

    def prepare_recovery_close(
        self,
        grant_database_id: int,
    ) -> tuple[ManagedOrder, WriteAttemptRecord, Mapping[str, Any]]: ...

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
        grant_id: str = "",
        authorization_mode: str = "MANIFEST",
    ) -> None: ...

    def unresolved(self) -> Optional[WriteAttemptRecord]: ...

    def prepare_place(
        self,
        command: NormalizedOrderCommand,
        *,
        operation: WriterOperation,
        operation_id: str,
        request_digest: str,
        safe_request_snapshot: Mapping[str, Any],
    ) -> tuple[ManagedOrder, WriteAttemptRecord]: ...

    def prepare_existing(
        self,
        order: ManagedOrder,
        *,
        operation: WriterOperation,
        operation_id: str,
        request_digest: str,
        safe_request_snapshot: Mapping[str, Any],
        recovery_grant_database_id: Optional[int] = None,
    ) -> WriteAttemptRecord: ...

    def prepare_close_cleanup(
        self,
        parent: WriteAttemptRecord,
        command: NormalizedOrderCommand,
        *,
        operation_id: str,
        request_digest: str,
        safe_request_snapshot: Mapping[str, Any],
    ) -> tuple[ManagedOrder, WriteAttemptRecord]: ...

    def prepare_recovery_close_cleanup(
        self,
        parent: WriteAttemptRecord,
        recovery_grant_database_id: int,
    ) -> tuple[ManagedOrder, WriteAttemptRecord, Mapping[str, Any]]: ...

    def transition(
        self,
        attempt: WriteAttemptRecord,
        *,
        event: WriteEvent,
        exchange_order_id: Optional[str] = None,
        order_state: Optional[str] = None,
        reason_code: Optional[str] = None,
        safe_response_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> WriteAttemptRecord: ...

    def order_for_attempt(self, attempt: WriteAttemptRecord) -> ManagedOrder: ...


class OkxDemoOrderWriter:
    """One lifecycle coordinator; persistence is committed before each POST."""

    def __init__(
        self,
        *,
        read_client: OkxDemoReadClient,
        write_transport: Optional[OkxDemoWriteTransport],
        store: OrderWriterStore,
        now_provider=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._read = read_client
        self._store = store
        self._now_provider = now_provider
        self.__offline_write_transport = write_transport
        self._atomic_capability_nonce = secrets.token_hex(32)
        self._atomic_dispatch_lock = threading.Lock()
        self._atomic_consumed_attempts: set[int] = set()

    @property
    def atomic_holder_token_digest(self) -> str:
        return self._store.atomic_process_token_digest

    @property
    def atomic_holder_token(self) -> str:
        return self._store.atomic_process_token

    def seal_atomic_dispatch(
        self,
        receipt: Mapping[str, Any],
        *,
        runtime_instance_id: str,
    ) -> _AtomicDispatchCapability:
        """Bind a DB-claimed receipt to this exact writer process."""
        required = {
            "attempt_id",
            "exchange_order_row_id",
            "client_order_id",
            "instrument_id",
            "request_body",
            "request_digest",
            "lease_generation",
            "runtime_instance_id",
            "holder_token_digest",
            "bundle_digest",
            "dispatch_remaining_ms",
            "dispatch_claimed_at",
        }
        if not required.issubset(receipt):
            raise OkxDemoWriteBlocked("atomic dispatch receipt is incomplete")
        if receipt["runtime_instance_id"] != runtime_instance_id:
            raise OkxDemoWriteBlocked("atomic dispatch runtime identity changed")
        if receipt["holder_token_digest"] != self.atomic_holder_token_digest:
            raise OkxDemoWriteBlocked("atomic dispatch process identity changed")
        remaining_ms = int(receipt["dispatch_remaining_ms"])
        if remaining_ms < 500 or remaining_ms > 1000:
            raise OkxDemoWriteBlocked("atomic dispatch claim budget is invalid")
        body = receipt["request_body"]
        if not isinstance(body, Mapping) or _digest(body) != receipt["request_digest"]:
            raise OkxDemoWriteBlocked("atomic dispatch receipt digest changed")
        return _AtomicDispatchCapability(
            issuer_nonce=self._atomic_capability_nonce,
            runtime_instance_id=runtime_instance_id,
            receipt=dict(receipt),
        )

    def dispatch_atomic_once(
        self,
        capability: _AtomicDispatchCapability,
    ) -> WriterResult:
        """Consume one process-bound capability and start at most one POST."""

        if not isinstance(capability, _AtomicDispatchCapability):
            raise OkxDemoWriteBlocked("atomic dispatch capability is invalid")
        receipt = capability.receipt
        body = receipt["request_body"]
        attempt_id = int(receipt["attempt_id"])
        with self._atomic_dispatch_lock:
            if (
                capability.issuer_nonce != self._atomic_capability_nonce
                or attempt_id in self._atomic_consumed_attempts
            ):
                raise OkxDemoWriteBlocked("atomic dispatch capability was already consumed")
            # Consume before any database or network operation.  A failure is
            # deliberately terminal for this process and can only recover GET-only.
            self._atomic_consumed_attempts.add(attempt_id)
        validation_started = time.monotonic()
        remaining_ms = self._store.validate_atomic_dispatch_authority(
            attempt_id=attempt_id,
            runtime_instance_id=capability.runtime_instance_id,
            lease_generation=int(receipt["lease_generation"]),
            request_digest=str(receipt["request_digest"]),
            bundle_digest=str(receipt["bundle_digest"]),
        )
        attempt = self._store.load_atomic_attempt(int(receipt["attempt_id"]))
        if (
            attempt.state != WriteState.DISPATCHED
            or attempt.exchange_order_row_id != int(receipt["exchange_order_row_id"])
            or attempt.client_order_id != str(receipt["client_order_id"])
            or attempt.instrument_id != str(receipt["instrument_id"])
            or attempt.request_digest != str(receipt["request_digest"])
            or dict(attempt.safe_request_snapshot) != dict(body)
        ):
            raise OkxDemoWriteBlocked("atomic dispatch journal identity changed")
        order = self._store.order_for_attempt(attempt)
        elapsed_ms = int((time.monotonic() - validation_started) * 1000)
        if remaining_ms - elapsed_ms < ATOMIC_POST_START_RESERVE_MS:
            raise OkxDemoWriteBlocked("atomic dispatch capability expired before POST")
        return self._post_and_reconcile(
            attempt=attempt,
            order=order,
            path="/api/v5/trade/order",
            body=body,
        )

    def place(
        self,
        approved: ApprovedExecution,
        *,
        submission_grant: OrderSubmissionAuthorization,
    ) -> WriterResult:
        view = approved_execution_view(approved)
        now = self._now()
        submission_grant.consume(
            approval_id=view.approval_id,
            canonical_hash=view.canonical_hash,
            policy_digest=view.policy_digest,
            approved_payload_hash=view.approved_payload_hash,
            client_order_id=view.client_order_id,
            now=now,
        )
        if now >= view.expires_at.astimezone(timezone.utc):
            raise OkxDemoWriteBlocked("approved execution is expired")
        self._acquire_lease(submission_grant, now)
        unresolved = self._store.unresolved()
        if unresolved is not None:
            unresolved_order = self._store.order_for_attempt(unresolved)
            self._authorize_existing(
                unresolved_order,
                submission_grant,
                now=now,
            )
            if unresolved_order.approval_id != view.approval_id:
                raise OkxDemoWriteBlocked(
                    "unresolved attempt belongs to another approval"
                )
            if (
                unresolved.operation == "CLOSE"
                and unresolved.state == WriteState.RESIDUAL_CLOSE_REQUIRED
            ):
                return self._cleanup_close(unresolved, view, submission_grant)
            return self._recover(unresolved)
        spec = self._instrument(view.instrument_id)
        command = normalize_order_command(
            view,
            submission_grant=submission_grant,
            instrument=spec,
            now=now,
        )
        operation: WriterOperation = "CLOSE" if command.reduce_only else "PLACE"
        if operation == "CLOSE":
            self._require_exact_close_position(command)
        leverage_result, prepared_order = self._ensure_leverage(command)
        if leverage_result is not None:
            return leverage_result
        operation_id = command.client_order_id
        if prepared_order is None:
            order, attempt = self._store.prepare_place(
                command,
                operation=operation,
                operation_id=operation_id,
                request_digest=_digest(command.request_body),
                safe_request_snapshot=_safe_request(command.request_body),
            )
        else:
            order = prepared_order
            attempt = self._store.prepare_existing(
                order,
                operation=operation,
                operation_id=operation_id,
                request_digest=_digest(command.request_body),
                safe_request_snapshot=_safe_request(command.request_body),
            )
        return self._post_and_reconcile(
            attempt=attempt,
            order=order,
            path="/api/v5/trade/order",
            body=command.request_body,
            require_terminal=operation == "CLOSE",
        )

    def reconcile_unresolved(self, attempt_id: int) -> WriterResult:
        """Claim one durable journal row and perform GET-only reconciliation."""

        now = self._now()
        attempt = self._store.claim_unresolved_for_reconciliation(
            attempt_id,
            now=now,
            expires_at=now + timedelta(seconds=10),
        )
        if attempt.attempt_id != attempt_id:
            raise OkxDemoWriteBlocked(
                "unresolved reconciliation claim changed identity"
            )
        return self._recover(attempt)

    def cancel(
        self,
        order: ManagedOrder,
        *,
        submission_grant: OrderSubmissionAuthorization,
    ) -> WriterResult:
        now = self._now()
        submission_grant.consume(
            approval_id=order.approval_id,
            canonical_hash=order.canonical_hash,
            policy_digest=order.policy_digest,
            approved_payload_hash=order.approved_payload_hash,
            client_order_id=order.client_order_id,
            now=now,
        )
        self._authorize_existing(order, submission_grant, now=now)
        self._acquire_lease(submission_grant, now)
        unresolved = self._store.unresolved()
        if unresolved is not None:
            unresolved_order = self._store.order_for_attempt(unresolved)
            self._authorize_existing(
                unresolved_order,
                submission_grant,
                now=now,
            )
            if unresolved_order.approval_id != order.approval_id:
                raise OkxDemoWriteBlocked(
                    "unresolved attempt belongs to another approval"
                )
            return self._recover(unresolved)
        body = {
            "instId": order.instrument_id,
            "clOrdId": validate_client_order_id(order.client_order_id),
        }
        attempt = self._store.prepare_existing(
            order,
            operation="CANCEL",
            operation_id=order.client_order_id,
            request_digest=_digest(body),
            safe_request_snapshot=_safe_request(body),
        )
        return self._post_and_reconcile(
            attempt=attempt,
            order=order,
            path="/api/v5/trade/cancel-order",
            body=body,
            require_terminal=True,
        )

    def recovery_cancel(
        self,
        *,
        recovery_grant_database_id: int,
    ) -> WriterResult:
        """Cancel under a reconciliation grant without reviving an approval."""

        self._store.acquire_recovery_lease(
            recovery_grant_database_id,
            now=self._now(),
        )
        unresolved = self._store.unresolved()
        if unresolved is not None:
            if (
                unresolved.recovery_grant_database_id
                != recovery_grant_database_id
            ):
                raise OkxDemoWriteBlocked(
                    "unresolved attempt belongs to another recovery grant"
                )
            return self._recover(unresolved)
        order = self._store.load_recovery_order(recovery_grant_database_id)
        body = {
            "instId": order.instrument_id,
            "clOrdId": validate_client_order_id(order.client_order_id),
        }
        attempt = self._store.prepare_existing(
            order,
            operation="CANCEL",
            operation_id=order.client_order_id,
            request_digest=_digest(body),
            safe_request_snapshot=_safe_request(body),
            recovery_grant_database_id=recovery_grant_database_id,
        )
        return self._post_and_reconcile(
            attempt=attempt,
            order=order,
            path="/api/v5/trade/cancel-order",
            body=body,
            require_terminal=True,
        )

    def recovery_reduce_only(
        self,
        *,
        recovery_grant_database_id: int,
    ) -> WriterResult:
        """Close the exact authoritative position under a run-bound grant."""

        self._store.acquire_recovery_lease(
            recovery_grant_database_id,
            now=self._now(),
        )
        unresolved = self._store.unresolved()
        if unresolved is not None:
            if (
                unresolved.operation == "CLOSE"
                and unresolved.state == WriteState.RESIDUAL_CLOSE_REQUIRED
            ):
                order, attempt, body = self._store.prepare_recovery_close_cleanup(
                    unresolved,
                    recovery_grant_database_id,
                )
                return self._post_and_reconcile(
                    attempt=attempt,
                    order=order,
                    path="/api/v5/trade/order",
                    body=body,
                    require_terminal=True,
                )
            if (
                unresolved.recovery_grant_database_id
                != recovery_grant_database_id
            ):
                raise OkxDemoWriteBlocked(
                    "unresolved attempt belongs to another recovery grant"
                )
            return self._recover(unresolved)
        order, attempt, body = self._store.prepare_recovery_close(
            recovery_grant_database_id
        )
        return self._post_and_reconcile(
            attempt=attempt,
            order=order,
            path="/api/v5/trade/order",
            body=body,
            require_terminal=True,
        )

    def amend(
        self,
        order: ManagedOrder,
        *,
        submission_grant: OrderSubmissionAuthorization,
        request_id: str,
        new_contracts: Optional[Decimal] = None,
        new_price: Optional[Decimal] = None,
    ) -> WriterResult:
        now = self._now()
        submission_grant.consume(
            approval_id=order.approval_id,
            canonical_hash=order.canonical_hash,
            policy_digest=order.policy_digest,
            approved_payload_hash=order.approved_payload_hash,
            client_order_id=order.client_order_id,
            now=now,
        )
        self._authorize_existing(order, submission_grant, now=now)
        self._acquire_lease(submission_grant, now)
        unresolved = self._store.unresolved()
        if unresolved is not None:
            unresolved_order = self._store.order_for_attempt(unresolved)
            self._authorize_existing(
                unresolved_order,
                submission_grant,
                now=now,
            )
            if unresolved_order.approval_id != order.approval_id:
                raise OkxDemoWriteBlocked(
                    "unresolved attempt belongs to another approval"
                )
            return self._recover(unresolved)
        request_id = validate_client_order_id(request_id)
        spec = self._instrument(order.instrument_id)
        if new_contracts is None and new_price is None:
            raise OkxDemoWriteBlocked("amend requires new contracts or price")
        if new_contracts is not None and (
            new_contracts < spec.min_size or new_contracts % spec.lot_size != 0
        ):
            raise OkxDemoWriteBlocked("amend contracts violate minSz/lotSz")
        if new_price is not None and (
            new_price <= 0 or new_price % spec.tick_size != 0
        ):
            raise OkxDemoWriteBlocked("amend price violates tickSz")
        body: dict[str, Any] = {
            "instId": order.instrument_id,
            "clOrdId": order.client_order_id,
            "reqId": request_id,
            "cxlOnFail": True,
        }
        if new_contracts is not None:
            body["newSz"] = _decimal_text(new_contracts)
        if new_price is not None:
            body["newPx"] = _decimal_text(new_price)
        attempt = self._store.prepare_existing(
            order,
            operation="AMEND",
            operation_id=request_id,
            request_digest=_digest(body),
            safe_request_snapshot=_safe_request(body),
        )
        return self._post_and_reconcile(
            attempt=attempt,
            order=order,
            path="/api/v5/trade/amend-order",
            body=body,
            expected_contracts=new_contracts,
            expected_price=new_price,
        )

    def _post_and_reconcile(
        self,
        *,
        attempt: WriteAttemptRecord,
        order: ManagedOrder,
        path: str,
        body: Mapping[str, Any],
        require_terminal: bool = False,
        expected_contracts: Optional[Decimal] = None,
        expected_price: Optional[Decimal] = None,
    ) -> WriterResult:
        try:
            payload = self._post(path=path, body=body)
            item = validate_write_item(
                payload,
                expected_client_order_id=order.client_order_id,
                reason="{}_WRITE_FAILED".format(attempt.operation),
            )
            if attempt.operation == "AMEND" and item.get("reqId") not in (
                None,
                attempt.operation_id,
            ):
                raise OkxDemoRecoveryRequired("AMEND_REQUEST_ID_MISMATCH")
            attempt = self._store.transition(
                attempt,
                event=WriteEvent.ACKNOWLEDGE,
                exchange_order_id=str(item["ordId"]),
                safe_response_snapshot=_safe_response(item),
            )
        except OkxDemoWriteRejected:
            attempt = self._store.transition(
                attempt,
                event=WriteEvent.EXPLICIT_REJECTION,
                reason_code="{}_WRITE_REJECTED".format(attempt.operation),
            )
            return _result(attempt, order, reason_code="EXPLICIT_WRITE_REJECTION")
        except (OkxDemoRecoveryRequired, OkxDemoTransportError):
            attempt = self._store.transition(
                attempt,
                event=WriteEvent.OUTCOME_UNKNOWN,
                reason_code="{}_OUTCOME_UNKNOWN".format(attempt.operation),
            )
        return self._reconcile(
            attempt,
            order,
            require_terminal=require_terminal,
            expected_contracts=expected_contracts,
            expected_price=expected_price,
        )

    def _recover(self, attempt: WriteAttemptRecord) -> WriterResult:
        order = self._store.order_for_attempt(attempt)
        if attempt.operation == "SET_LEVERAGE":
            return self._reconcile_leverage(attempt, order)
        snapshot = attempt.safe_request_snapshot
        expected_contracts = (
            Decimal(str(snapshot["newSz"]))
            if attempt.operation == "AMEND" and "newSz" in snapshot
            else None
        )
        expected_price = (
            Decimal(str(snapshot["newPx"]))
            if attempt.operation == "AMEND" and "newPx" in snapshot
            else None
        )
        return self._reconcile(
            attempt,
            order,
            require_terminal=attempt.operation in {"CANCEL", "CLOSE"},
            expected_contracts=expected_contracts,
            expected_price=expected_price,
        )

    def _reconcile(
        self,
        attempt: WriteAttemptRecord,
        order: ManagedOrder,
        *,
        require_terminal: bool = False,
        expected_contracts: Optional[Decimal] = None,
        expected_price: Optional[Decimal] = None,
    ) -> WriterResult:
        try:
            item = self._order(order)
            state = str(item["state"])
            if require_terminal and state not in TERMINAL_ORDER_STATES:
                raise OkxDemoRecoveryRequired("ORDER_NOT_TERMINAL")
            if expected_contracts is not None and Decimal(str(item["size"])) != expected_contracts:
                raise OkxDemoRecoveryRequired("AMEND_SIZE_NOT_RECONCILED")
            if expected_price is not None and Decimal(str(item["price"])) != expected_price:
                raise OkxDemoRecoveryRequired("AMEND_PRICE_NOT_RECONCILED")
            if attempt.state in {WriteState.PREPARED, WriteState.DISPATCHED}:
                attempt = self._store.transition(
                    attempt,
                    event=WriteEvent.ACKNOWLEDGE,
                    exchange_order_id=str(item["order_id"]),
                    safe_response_snapshot=_safe_reconciliation(item),
                )
            if attempt.operation == "CLOSE" and not self._position_is_zero(order):
                attempt = self._store.transition(
                    attempt,
                    event=WriteEvent.RESIDUAL_DETECTED,
                    exchange_order_id=str(item["order_id"]),
                    order_state=state,
                    reason_code="CLOSE_TERMINAL_WITH_RESIDUAL_POSITION",
                    safe_response_snapshot=_safe_reconciliation(item),
                )
                return _result(
                    attempt,
                    order,
                    order_item=item,
                    reason_code="RESIDUAL_CLOSE_REQUIRED",
                )
            attempt = self._store.transition(
                attempt,
                event=WriteEvent.RECONCILE,
                exchange_order_id=str(item["order_id"]),
                order_state=state,
                safe_response_snapshot=_safe_reconciliation(item),
            )
            return _result(attempt, order, order_item=item)
        except (
            KeyError,
            TypeError,
            ValueError,
            OkxDemoRecoveryRequired,
            OkxDemoWriteBlocked,
            RuntimeError,
        ):
            if attempt.state != WriteState.RECOVERY_REQUIRED:
                attempt = self._store.transition(
                    attempt,
                    event=WriteEvent.OUTCOME_UNKNOWN,
                    reason_code="READ_RECONCILIATION_FAILED",
                )
            else:
                attempt = self._store.transition(
                    attempt,
                    event=WriteEvent.RECOVERY_STILL_UNKNOWN,
                    reason_code="READ_RECONCILIATION_FAILED",
                )
            return _result(
                attempt,
                order,
                reason_code="NONTERMINAL_OUTCOME_REQUIRES_RECOVERY",
            )

    def _cleanup_close(
        self,
        parent: WriteAttemptRecord,
        approved: ApprovedExecutionView,
        submission_grant: OrderSubmissionAuthorization,
    ) -> WriterResult:
        if (
            approved.approval_id != self._store.order_for_attempt(parent).approval_id
            or not approved.reduce_only
            or approved.order_type != "market"
        ):
            raise OkxDemoWriteBlocked(
                "residual close cleanup must use the original approved close plan"
            )
        next_sequence = parent.close_sequence + 1
        if next_sequence > MAX_CLOSE_CLEANUP_ATTEMPTS:
            return _result(
                parent,
                self._store.order_for_attempt(parent),
                reason_code="CLOSE_CLEANUP_LIMIT_REACHED",
            )
        contracts, side = self._current_close_position(
            approved.instrument_id,
            approved.position_side,
        )
        if contracts == 0:
            parent = self._store.transition(
                parent,
                event=WriteEvent.RECONCILE,
                order_state="position_zero",
            )
            return _result(parent, self._store.order_for_attempt(parent))
        cleanup_id = _derived_operation_id(
            approved.client_order_id,
            "C{}".format(next_sequence),
        )
        cleanup_view = approved.model_copy(
            update={
                "client_order_id": cleanup_id,
                "contracts": abs(contracts),
                "side": side,
            }
        )
        spec = self._instrument(approved.instrument_id)
        command = normalize_order_command(
            cleanup_view,
            submission_grant=submission_grant,
            instrument=spec,
            now=self._now(),
        )
        if not self._leverage_matches(
            command.instrument_id,
            command.margin_mode,
            command.position_side,
            command.leverage,
        ):
            raise OkxDemoWriteBlocked(
                "residual close cleanup leverage no longer matches approval"
            )
        order, attempt = self._store.prepare_close_cleanup(
            parent,
            command,
            operation_id=cleanup_id,
            request_digest=_digest(command.request_body),
            safe_request_snapshot=_safe_request(command.request_body),
        )
        return self._post_and_reconcile(
            attempt=attempt,
            order=order,
            path="/api/v5/trade/order",
            body=command.request_body,
            require_terminal=True,
        )

    def _order(self, order: ManagedOrder) -> Mapping[str, Any]:
        snapshot = self._read.order(
            order.instrument_id,
            client_order_id=order.client_order_id,
        )
        if len(snapshot.items) != 1:
            raise OkxDemoRecoveryRequired("ORDER_QUERY_NOT_UNIQUE")
        item = snapshot.items[0]
        if (
            item.get("inst_id") != order.instrument_id
            or item.get("client_order_id") != order.client_order_id
            or item.get("position_side") != order.position_side
            or item.get("margin_mode") != "isolated"
            or item.get("side") != order.side
            or item.get("order_type") != order.order_type
            or item.get("reduce_only") is not order.reduce_only
        ):
            raise OkxDemoRecoveryRequired("ORDER_QUERY_IDENTITY_MISMATCH")
        return item

    def _instrument(self, instrument_id: str) -> InstrumentSpec:
        snapshot = self._read.instruments(instrument_id)
        if len(snapshot.items) != 1:
            raise OkxDemoWriteBlocked("instrument read did not return one record")
        try:
            return InstrumentSpec.model_validate(snapshot.items[0])
        except (TypeError, ValueError):
            raise OkxDemoWriteBlocked("instrument read did not match schema") from None

    def _ensure_leverage(
        self,
        command: NormalizedOrderCommand,
    ) -> tuple[Optional[WriterResult], Optional[ManagedOrder]]:
        if self._leverage_matches(
            command.instrument_id,
            command.margin_mode,
            command.position_side,
            command.leverage,
        ):
            return None, None
        if command.authorization_mode == "ONE_SHOT":
            raise OkxDemoWriteBlocked(
                "one-shot submission requires preconfigured leverage"
            )
        body = {
            "instId": command.instrument_id,
            "lever": _decimal_text(command.leverage),
            "mgnMode": command.margin_mode,
            "posSide": command.position_side,
        }
        order, attempt = self._store.prepare_place(
            command,
            operation="SET_LEVERAGE",
            operation_id=_derived_operation_id(command.client_order_id, "LEV"),
            request_digest=_digest(body),
            safe_request_snapshot=_safe_request(body),
        )
        try:
            payload = self._post(
                path="/api/v5/account/set-leverage",
                body=body,
            )
            item = validate_write_item(
                payload,
                expected_client_order_id=None,
                reason="SET_LEVERAGE_FAILED",
                require_order_id=False,
            )
            if (
                item.get("instId") != command.instrument_id
                or item.get("mgnMode") != command.margin_mode
                or item.get("posSide") != command.position_side
                or Decimal(str(item.get("lever"))) != command.leverage
            ):
                raise OkxDemoRecoveryRequired("SET_LEVERAGE_ACK_MISMATCH")
            attempt = self._store.transition(
                attempt,
                event=WriteEvent.ACKNOWLEDGE,
                safe_response_snapshot=_safe_response(item),
            )
        except OkxDemoWriteRejected:
            attempt = self._store.transition(
                attempt,
                event=WriteEvent.EXPLICIT_REJECTION,
                reason_code="SET_LEVERAGE_WRITE_REJECTED",
            )
            return (
                _result(
                    attempt,
                    order,
                    reason_code="EXPLICIT_WRITE_REJECTION",
                ),
                order,
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            OkxDemoRecoveryRequired,
            OkxDemoTransportError,
        ):
            attempt = self._store.transition(
                attempt,
                event=WriteEvent.OUTCOME_UNKNOWN,
                reason_code="SET_LEVERAGE_OUTCOME_UNKNOWN",
            )
        result = self._reconcile_leverage(attempt, order)
        return (
            None if result.status == "RECONCILED" else result,
            order,
        )

    def _reconcile_leverage(
        self,
        attempt: WriteAttemptRecord,
        order: ManagedOrder,
    ) -> WriterResult:
        try:
            expected = Decimal(str(attempt.safe_request_snapshot["lever"]))
            margin_mode = str(attempt.safe_request_snapshot["mgnMode"])
            if not self._leverage_matches(
                order.instrument_id,
                margin_mode,
                str(attempt.safe_request_snapshot["posSide"]),
                expected,
            ):
                raise OkxDemoRecoveryRequired("LEVERAGE_NOT_RECONCILED")
            if attempt.state == WriteState.PREPARED:
                attempt = self._store.transition(
                    attempt,
                    event=WriteEvent.ACKNOWLEDGE,
                )
            attempt = self._store.transition(
                attempt,
                event=WriteEvent.RECONCILE,
                order_state="leverage_confirmed",
            )
            return _result(attempt, order)
        except (
            KeyError,
            TypeError,
            ValueError,
            OkxDemoRecoveryRequired,
            OkxDemoWriteBlocked,
            RuntimeError,
        ):
            event = (
                WriteEvent.RECOVERY_STILL_UNKNOWN
                if attempt.state == WriteState.RECOVERY_REQUIRED
                else WriteEvent.OUTCOME_UNKNOWN
            )
            attempt = self._store.transition(
                attempt,
                event=event,
                reason_code="LEVERAGE_RECONCILIATION_FAILED",
            )
            return _result(
                attempt,
                order,
                reason_code="NONTERMINAL_OUTCOME_REQUIRES_RECOVERY",
            )

    def _leverage_matches(
        self,
        instrument_id: str,
        margin_mode: str,
        position_side: str,
        leverage: Decimal,
    ) -> bool:
        snapshot = self._read.leverage(instrument_id)
        if len(snapshot.items) != 1:
            return False
        item = snapshot.items[0]
        try:
            observed = Decimal(str(item["leverage"]))
        except (KeyError, TypeError, ValueError):
            return False
        return (
            item.get("inst_id") == instrument_id
            and item.get("margin_mode") == margin_mode
            and item.get("position_side") == position_side
            and observed == leverage
        )

    def _require_exact_close_position(
        self,
        command: NormalizedOrderCommand,
    ) -> None:
        snapshot = self._read.positions(command.instrument_id)
        matches = [
            item
            for item in snapshot.items
            if item.get("inst_id") == command.instrument_id
            and item.get("position_side") == command.position_side
        ]
        if len(matches) != 1:
            raise OkxDemoWriteBlocked("close requires exactly one current position side")
        item = matches[0]
        try:
            contracts = Decimal(str(item["contracts"]))
        except (KeyError, TypeError, ValueError):
            raise OkxDemoWriteBlocked("close position evidence is malformed") from None
        expected_side = "sell" if command.position_side == "long" else "buy"
        if (
            item.get("inst_id") != command.instrument_id
            or item.get("margin_mode") != "isolated"
            or item.get("position_side") != command.position_side
            or contracts == 0
            or abs(contracts) != command.contracts
            or command.side != expected_side
        ):
            raise OkxDemoWriteBlocked(
                "close must exactly offset the current isolated net position"
            )

    def _position_is_zero(self, order: ManagedOrder) -> bool:
        snapshot = self._read.positions(order.instrument_id)
        if not snapshot.items:
            return True
        matches = [
            item
            for item in snapshot.items
            if item.get("inst_id") == order.instrument_id
            and item.get("position_side") == order.position_side
        ]
        if len(matches) != 1:
            return False
        item = matches[0]
        try:
            contracts = Decimal(str(item["contracts"]))
        except (KeyError, TypeError, ValueError):
            return False
        return (
            item.get("inst_id") == order.instrument_id
            and item.get("margin_mode") == "isolated"
            and item.get("position_side") == order.position_side
            and contracts == 0
        )

    def _current_close_position(
        self,
        instrument_id: str,
        position_side: Literal["long", "short"],
    ) -> tuple[Decimal, Literal["buy", "sell"]]:
        snapshot = self._read.positions(instrument_id)
        if not snapshot.items:
            return Decimal("0"), "sell"
        matches = [
            item
            for item in snapshot.items
            if item.get("inst_id") == instrument_id
            and item.get("position_side") == position_side
        ]
        if len(matches) != 1:
            raise OkxDemoWriteBlocked("close cleanup position is not unique")
        item = matches[0]
        try:
            contracts = Decimal(str(item["contracts"]))
        except (KeyError, TypeError, ValueError):
            raise OkxDemoWriteBlocked("close cleanup position is malformed") from None
        if (
            item.get("inst_id") != instrument_id
            or item.get("margin_mode") != "isolated"
            or item.get("position_side") != position_side
        ):
            raise OkxDemoWriteBlocked("close cleanup position identity mismatched")
        return contracts, ("sell" if position_side == "long" else "buy")

    def _authorize_existing(
        self,
        order: ManagedOrder,
        submission_grant: OrderSubmissionAuthorization,
        *,
        now: datetime,
    ) -> None:
        submission_grant.require_active(
            approval_id=order.approval_id,
            canonical_hash=order.canonical_hash,
            policy_digest=order.policy_digest,
            approved_payload_hash=order.approved_payload_hash,
            client_order_id=order.client_order_id,
            now=now,
        )

    def _post(self, *, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        transport = self.__offline_write_transport
        if transport is None:
            raise OkxDemoWriteBlocked("writer transport capability is unavailable")
        return transport.post(path=path, body=body)

    def _acquire_lease(
        self,
        submission_grant: OrderSubmissionAuthorization,
        now: datetime,
    ) -> None:
        self._store.acquire_lease(
            grant_id=submission_grant.grant_id,
            authorization_mode=submission_grant.authorization_mode,
            writer_instance_id=submission_grant.writer_instance_id,
            approval_id=submission_grant.approval_id,
            canonical_hash=submission_grant.canonical_hash,
            policy_digest=submission_grant.policy_digest,
            approved_payload_hash=submission_grant.approved_payload_hash,
            now=now,
            expires_at=submission_grant.expires_at,
        )

    def _now(self) -> datetime:
        value = self._now_provider()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise OkxDemoWriteBlocked("writer clock must be timezone-aware")
        return value.astimezone(timezone.utc)


def _safe_request(body: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "instId",
        "tdMode",
        "side",
        "posSide",
        "ordType",
        "sz",
        "px",
        "reduceOnly",
        "newSz",
        "newPx",
        "cxlOnFail",
        "attachAlgoOrds",
        "lever",
        "mgnMode",
        "posSide",
    }
    return {key: value for key, value in body.items() if key in allowed}


def _digest(body: Mapping[str, Any]) -> str:
    rendered = json.dumps(
        dict(body),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(rendered.encode()).hexdigest()


def _safe_response(item: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "ordId",
        "clOrdId",
        "reqId",
        "sCode",
        "instId",
        "lever",
        "mgnMode",
        "posSide",
    }
    return {key: item[key] for key in allowed if key in item}


def _safe_reconciliation(item: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "inst_id",
        "order_id",
        "client_order_id",
        "state",
        "side",
        "position_side",
        "margin_mode",
        "order_type",
        "reduce_only",
        "price",
        "size",
        "accumulated_fill_size",
    }
    return {
        key: (
            format(value, "f")
            if isinstance(value, Decimal)
            else value
        )
        for key, value in item.items()
        if key in allowed
    }


def _hash(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()


def _result(
    attempt: WriteAttemptRecord,
    order: ManagedOrder,
    *,
    order_item: Optional[Mapping[str, Any]] = None,
    reason_code: Optional[str] = None,
) -> WriterResult:
    exchange_order_id = (
        str(order_item["order_id"])
        if order_item is not None
        else order.exchange_order_id
    )
    return WriterResult(
        status=attempt.state.value,
        operation=attempt.operation,
        attempt_id=attempt.attempt_id,
        exchange_order_row_id=attempt.exchange_order_row_id,
        client_order_id_sha256=_hash(order.client_order_id) or "",
        exchange_order_id_sha256=_hash(exchange_order_id),
        order_state=(
            str(order_item["state"]) if order_item is not None else None
        ),
        reason_code=reason_code,
    )


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _derived_operation_id(parent: str, suffix: str) -> str:
    return validate_client_order_id(parent[: 32 - len(suffix)] + suffix)
