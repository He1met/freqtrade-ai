from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.okx_demo.read_adapter import OkxDemoReadClient
from app.adapters.okx_demo.writer_models import (
    ClaimedApprovedExecution,
    OrderSubmissionAuthorization,
)
from app.core.config import get_settings
from app.services.okx_demo_reconciliation import (
    DEFAULT_ALLOWED_EVIDENCE_ROOT,
    DEFAULT_EVIDENCE_ROOT,
    OkxDemoReconciliationBlocked,
    OkxDemoReconciliationService,
    RECOVERY_STREAMS,
    SCHEMA_VERSION,
)
from app.models.okx_demo_reconciliation import (
    OkxDemoExchangeEvent,
    OkxDemoReconciliationState,
    OkxDemoRecoveryGrant,
)
from app.models.execution_lineage import (
    ApprovedExecution,
    ReconciliationRun,
    RiskDecision,
    TradeIntent,
)
from app.models.full_chain import FullChainRun, FullChainStageRun
from app.models.order_writer import OkxOrderWriteAttempt
from app.models.strategy_deployment import SignalEvaluation, StrategyDeployment
from app.repositories.strategy_deployments import StrategyDeploymentRepository
from app.services.okx_demo_execution_orchestrator import (
    OkxDemoExecutionOrchestrator,
)


PAGE_LIMIT = 100
MAX_PAGES = 100
MAX_RUNTIME_RECONCILIATION_AGE_SECONDS = 30
DEMO_GRANT_TTL_SECONDS = 10
SIGNAL_EVALUATION_LEASE_SECONDS = 30
RUNTIME_DATABASE_ID_KEYS = (
    "reconciliation_run",
    "exchange_events",
    "order_snapshots",
    "fill_snapshots",
    "position_snapshots",
    "account_snapshots",
    "repaired_exchange_orders",
    "recovery_batches",
    "reconciliation_state",
)


class OkxDemoRuntimeReconciliationAdapter:
    """The single #449 runtime bridge from authenticated REST to #448 evidence."""

    def __init__(
        self,
        *,
        evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
        allowed_evidence_root: Path = DEFAULT_ALLOWED_EVIDENCE_ROOT,
        account_fingerprint_sha256: Optional[str] = None,
        order_submission_enabled: Optional[bool] = None,
        now_provider=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._evidence_root = Path(evidence_root)
        self._allowed_evidence_root = Path(allowed_evidence_root)
        self._fingerprint = (
            account_fingerprint_sha256
            or os.environ.get("OKX_DEMO_ACCOUNT_FINGERPRINT", "")
        )
        if (
            len(self._fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in self._fingerprint)
        ):
            raise OkxDemoReconciliationBlocked(
                "runtime reconciliation requires the pinned account fingerprint digest"
            )
        self._now_provider = now_provider
        manifest_submission_enabled = (
            get_settings()
            .execution_target_manifest.active_target.order_submission_enabled
        )
        self._order_submission_enabled = (
            manifest_submission_enabled
            and order_submission_enabled is not False
        )
        self._writer_instance_id = "Runtime{}".format(uuid4().hex)
        self._signal_lease_owner = "RuntimeSignal{}".format(uuid4().hex)
        self._last_completed_at: Optional[datetime] = None
        self._stream_generation = 0
        self._closed = False

    def reconcile_before_writer(
        self,
        *,
        read_client: OkxDemoReadClient,
        db: Session,
    ) -> Mapping[str, Any]:
        return self._full_rest_reconciliation(read_client=read_client, db=db)

    def observe(
        self,
        *,
        read_client: OkxDemoReadClient,
        db: Session,
    ) -> Mapping[str, Any]:
        return self._full_rest_reconciliation(read_client=read_client, db=db)

    def run_cycle(self, *, read_client, writer, db: Session) -> None:
        pending = list(
            db.scalars(
                select(OkxOrderWriteAttempt)
                .where(
                    OkxOrderWriteAttempt.execution_target_id == "OKX_DEMO",
                    OkxOrderWriteAttempt.state.in_(
                        (
                            "PREPARED",
                            "ACKNOWLEDGED",
                            "RECOVERY_REQUIRED",
                            "RESIDUAL_CLOSE_REQUIRED",
                        )
                    ),
                )
                .order_by(OkxOrderWriteAttempt.id)
            )
        )
        if len(pending) > 1:
            raise OkxDemoReconciliationBlocked(
                "multiple unresolved runtime recovery attempts exist"
            )
        if pending:
            attempt = pending[0]
            if (
                attempt.recovery_grant_database_id is not None
                and attempt.operation == "CANCEL"
            ):
                writer.recovery_cancel(
                    recovery_grant_database_id=(
                        attempt.recovery_grant_database_id
                    )
                )
            elif (
                attempt.recovery_grant_database_id is not None
                and attempt.operation == "CLOSE"
            ):
                writer.recovery_reduce_only(
                    recovery_grant_database_id=(
                        attempt.recovery_grant_database_id
                    )
                )
            elif attempt.recovery_grant_database_id is not None:
                raise OkxDemoReconciliationBlocked(
                    "unresolved runtime recovery operation is unsupported"
                )
            elif attempt.operation in {"PLACE", "CLOSE", "SET_LEVERAGE"}:
                if not getattr(self, "_order_submission_enabled", False):
                    return
                approved = _approved_execution_by_id(
                    db,
                    approval_id=attempt.approval_id,
                )
                if approved is None:
                    raise OkxDemoReconciliationBlocked(
                        "unresolved runtime placement approval is unavailable"
                    )
                writer.place(
                    approved,
                    submission_grant=self._submission_authorization(approved),
                )
            else:
                raise OkxDemoReconciliationBlocked(
                    "unresolved runtime placement operation is unsupported"
                )
            return
        grants = list(
            db.scalars(
                select(OkxDemoRecoveryGrant)
                .join(
                    OkxDemoReconciliationState,
                    OkxDemoReconciliationState.execution_target_id
                    == OkxDemoRecoveryGrant.execution_target_id,
                )
                .where(
                    OkxDemoRecoveryGrant.execution_target_id == "OKX_DEMO",
                    OkxDemoRecoveryGrant.status == "ACTIVE",
                    OkxDemoRecoveryGrant.reconciliation_run_id
                    == OkxDemoReconciliationState.last_reconciliation_run_id,
                )
                .order_by(OkxDemoRecoveryGrant.database_id)
            )
        )
        for grant in grants:
            if grant.action == "CANCEL":
                writer.recovery_cancel(
                    recovery_grant_database_id=grant.database_id
                )
            elif grant.action == "REDUCE_ONLY":
                writer.recovery_reduce_only(
                    recovery_grant_database_id=grant.database_id
                )
            else:
                raise OkxDemoReconciliationBlocked(
                    "runtime recovery grant action is unsupported"
                )
        if grants:
            return
        if self._process_one_signal_evaluation(
            read_client=read_client,
            db=db,
        ):
            return
        if not getattr(self, "_order_submission_enabled", False):
            return
        now = _aware(self._now_provider())
        if not _fresh_reconciliation_allows_opening(db, now=now):
            raise OkxDemoReconciliationBlocked(
                "fresh reconciled runtime state is required before new submission"
            )
        approved = _next_unconsumed_approved_execution(db, now=now)
        if approved is None:
            return
        writer.place(
            approved,
            submission_grant=self._submission_authorization(approved),
        )

    def _process_one_signal_evaluation(
        self,
        *,
        read_client: Any,
        db: Session,
    ) -> bool:
        now = _aware(self._now_provider())
        repository = StrategyDeploymentRepository(db)
        deployments = list(
            db.scalars(
                select(StrategyDeployment)
                .where(
                    StrategyDeployment.execution_target_id == "OKX_DEMO",
                    StrategyDeployment.status == "ACTIVE",
                )
                .order_by(StrategyDeployment.id)
            )
        )
        for deployment in deployments:
            repository.enqueue_evaluation(
                deployment.id,
                closed_candle_at=_latest_closed_candle_open(
                    now,
                    deployment.timeframe,
                ),
            )
        claimed = repository.claim_next(
            owner=self._signal_lease_owner,
            lease_seconds=SIGNAL_EVALUATION_LEASE_SECONDS,
            now=now,
        )
        if claimed is None:
            return False
        if not claimed.lease_token:
            raise OkxDemoReconciliationBlocked(
                "claimed signal evaluation lacks a lease token"
            )
        OkxDemoExecutionOrchestrator(
            db,
            read_client=read_client,
            deployment_repository=repository,
        ).process(
            claimed.id,
            lease_token=claimed.lease_token,
            fencing_sequence=claimed.fencing_sequence,
            now=now,
        )
        return True

    def _submission_authorization(
        self,
        approved: ClaimedApprovedExecution,
    ) -> OrderSubmissionAuthorization:
        now = _aware(self._now_provider())
        expires_at = min(
            approved.expires_at,
            now + timedelta(seconds=DEMO_GRANT_TTL_SECONDS),
        )
        if expires_at <= now:
            raise OkxDemoReconciliationBlocked(
                "approved execution expired before runtime submission"
            )
        return OrderSubmissionAuthorization(
            execution_target_id="OKX_DEMO",
            authorization_schema_version="RISK_V1",
            canonical_hash=approved.canonical_hash,
            policy_digest=approved.policy_digest,
            approved_payload_hash=approved.approved_payload_hash,
            allow_real_funds=False,
            simulated_trading=True,
            order_submission_enabled=True,
            writer_instance_id=self._writer_instance_id,
            approval_id=approved.approval_id,
            expires_at=expires_at,
        )

    def close(self) -> None:
        self._closed = True

    def _full_rest_reconciliation(
        self,
        *,
        read_client: OkxDemoReadClient,
        db: Session,
    ) -> Mapping[str, Any]:
        if self._closed:
            raise OkxDemoReconciliationBlocked(
                "runtime reconciliation adapter is closed"
            )
        started_at = _aware(self._now_provider())
        persisted_generation = db.scalar(
            select(func.max(OkxDemoExchangeEvent.stream_generation)).where(
                OkxDemoExchangeEvent.execution_target_id == "OKX_DEMO",
                OkxDemoExchangeEvent.source == "REST",
            )
        )
        self._stream_generation = max(
            self._stream_generation + 1,
            int(persisted_generation or 0) + 1,
        )
        persisted_completed_at = db.scalar(
            select(ReconciliationRun.completed_at)
            .where(
                ReconciliationRun.execution_target_id == "OKX_DEMO",
                ReconciliationRun.completed_at.is_not(None),
            )
            .order_by(
                ReconciliationRun.completed_at.desc(),
                ReconciliationRun.id.desc(),
            )
            .limit(1)
        )
        if persisted_completed_at is not None:
            persisted_completed_at = (
                persisted_completed_at.replace(tzinfo=timezone.utc)
                if persisted_completed_at.tzinfo is None
                else _aware(persisted_completed_at)
            )
            if (
                self._last_completed_at is None
                or persisted_completed_at > self._last_completed_at
            ):
                self._last_completed_at = persisted_completed_at
        history_floor = (
            self._last_completed_at - timedelta(seconds=5)
            if self._last_completed_at is not None
            else None
        )
        pending, pending_water, pending_observed = self._pages(
            read_client,
            "pending_orders",
            identity_field="order_id",
        )
        history, history_water, history_observed = self._pages(
            read_client,
            "orders_history",
            identity_field="order_id",
            stop_at=history_floor,
            timestamp_field="updated_at",
        )
        fills, fill_water, fills_observed = self._pages(
            read_client,
            "fills_history",
            identity_field="fill_id",
            cursor_field="bill_id",
            stop_at=history_floor,
            timestamp_field="timestamp",
            request_kwargs=(
                {
                    "begin": _epoch_millis(history_floor),
                    "end": _epoch_millis(started_at),
                }
                if history_floor is not None
                else None
            ),
        )
        positions_snapshot = read_client.positions()
        balance_snapshot = read_client.balance()
        snapshots = [
            positions_snapshot,
            balance_snapshot,
        ]
        if any(
            snapshot.metadata.authenticated is not True
            or snapshot.metadata.stale is not False
            or _aware(snapshot.metadata.expires_at) <= started_at
            for snapshot in snapshots
        ):
            raise OkxDemoReconciliationBlocked(
                "runtime REST baseline is unauthenticated or stale"
            )
        observed_at = min(
            [
                pending_observed,
                history_observed,
                fills_observed,
                *(_aware(snapshot.metadata.fetched_at) for snapshot in snapshots),
            ]
        )
        events = []
        orders = {}
        for item in pending + history:
            orders[str(item["order_id"])] = item
        for index, item in enumerate(orders.values()):
            item_observed = _item_time(item, "updated_at", observed_at)
            events.append(
                _event(
                    "ORDER",
                    str(item["order_id"]),
                    {
                        "ordId": str(item["order_id"]),
                        "clOrdId": item.get("client_order_id") or "",
                        "instId": item["inst_id"],
                        "state": item["state"],
                        "sz": item["size"],
                        "accFillSz": item["accumulated_fill_size"],
                        "avgPx": item.get("average_price") or "",
                        "reduceOnly": bool(item.get("reduce_only", False)),
                    },
                    item_observed,
                    index,
                    self._stream_generation,
                )
            )
        for index, item in enumerate(fills):
            item_observed = _item_time(item, "timestamp", observed_at)
            events.append(
                _event(
                    "FILL",
                    str(item["fill_id"]),
                    {
                        "fillId": str(item["fill_id"]),
                        "ordId": str(item["order_id"]),
                        "instId": item["inst_id"],
                        "fillPx": item["price"],
                        "fillSz": item["size"],
                        "fee": item.get("fee"),
                    },
                    item_observed,
                    index,
                    self._stream_generation,
                )
            )
        for index, raw_item in enumerate(positions_snapshot.items):
            item = _item_mapping(raw_item)
            item_observed = _item_time(item, "timestamp", observed_at)
            identity = "{}:{}".format(item["inst_id"], item["position_side"])
            events.append(
                _event(
                    "POSITION",
                    identity,
                    {
                        "instId": item["inst_id"],
                        "posSide": item["position_side"],
                        "pos": item["contracts"],
                        "avgPx": item.get("average_price") or "",
                    },
                    item_observed,
                    index,
                    self._stream_generation,
                )
            )
        account_payload, account_observed = self._account_payload(
            [_item_mapping(item) for item in balance_snapshot.items],
            observed_at,
        )
        events.append(
            _event(
                "ACCOUNT",
                "account",
                account_payload,
                account_observed,
                0,
                self._stream_generation,
            )
        )
        completed_at = max(_aware(self._now_provider()), observed_at)
        batch_id = "runtime-{}".format(uuid4().hex)
        high_watermarks = {
            "ORDER": hashlib.sha256(
                "{}|{}".format(pending_water, history_water).encode()
            ).hexdigest(),
            "FILL": fill_water,
            "POSITION": hashlib.sha256(
                str(len(positions_snapshot.items)).encode()
            ).hexdigest(),
            "ACCOUNT": hashlib.sha256(
                str(len(balance_snapshot.items)).encode()
            ).hexdigest(),
        }
        service = OkxDemoReconciliationService(
            db,
            evidence_root=self._evidence_root,
            allowed_evidence_root=self._allowed_evidence_root,
        )
        service.ingest_recovery_batch(
            events,
            recovery_batch_id=batch_id,
            authenticated=True,
            pagination_complete=True,
            complete_streams=RECOVERY_STREAMS,
            high_watermarks=high_watermarks,
            overlap_started_at=(
                history_floor if history_floor is not None else started_at
            ),
            observed_at=observed_at,
            completed_at=completed_at,
        )
        result = service.reconcile(
            now=completed_at,
            recovered=self._last_completed_at is not None,
        )
        self._last_completed_at = completed_at
        return {
            "status": result.status,
            "execution_target": "OKX_DEMO",
            "reconciliation_run_id": result.reconciliation_run_database_id,
            "database_ids": {
                key: result.database_ids[key]
                for key in RUNTIME_DATABASE_ID_KEYS
            },
            "observed_at": observed_at.isoformat(),
            "safe_to_open": not result.opening_frozen,
        }

    def _pages(
        self,
        read_client: OkxDemoReadClient,
        method_name: str,
        *,
        identity_field: str,
        cursor_field: Optional[str] = None,
        stop_at: Optional[datetime] = None,
        timestamp_field: Optional[str] = None,
        request_kwargs: Optional[Mapping[str, str]] = None,
    ) -> tuple[list[dict[str, Any]], str, datetime]:
        if stop_at is not None and timestamp_field is None:
            raise OkxDemoReconciliationBlocked(
                "incremental pagination requires both a cutoff and timestamp field"
            )
        cutoff = _aware(stop_at) if stop_at is not None else None
        method = getattr(read_client, method_name, None)
        if not callable(method):
            raise OkxDemoReconciliationBlocked(
                "runtime read client lacks complete {} pagination".format(
                    method_name
                )
            )
        cursor = None
        seen_cursors = set()
        items_by_identity: dict[str, dict[str, Any]] = {}
        retained_by_identity: dict[str, dict[str, Any]] = {}
        oldest_fetched_at: Optional[datetime] = None
        page_count = 0
        for _page in range(MAX_PAGES):
            snapshot = method(
                after=cursor,
                limit=PAGE_LIMIT,
                **dict(request_kwargs or {}),
            )
            page_count += 1
            fetched_at = _aware(snapshot.metadata.fetched_at)
            expires_at = _aware(snapshot.metadata.expires_at)
            if (
                snapshot.metadata.authenticated is not True
                or snapshot.metadata.stale is not False
                or expires_at <= _aware(self._now_provider())
            ):
                raise OkxDemoReconciliationBlocked(
                    "{} page is unauthenticated or stale".format(method_name)
                )
            page_items = [
                _item_mapping(item) for item in snapshot.items
            ]
            oldest_fetched_at = (
                fetched_at
                if oldest_fetched_at is None
                else min(oldest_fetched_at, fetched_at)
            )
            for item in page_items:
                identity = _item_identity(item, identity_field, method_name)
                identity_cursor = _pagination_cursor(
                    item,
                    cursor_field or identity_field,
                    method_name,
                )
                if cursor is not None and identity_cursor > int(cursor):
                    raise OkxDemoReconciliationBlocked(
                        "{} pagination item escapes the requested cursor window".format(
                            method_name
                        )
                    )
                existing = items_by_identity.get(identity)
                if existing is not None and existing != item:
                    raise OkxDemoReconciliationBlocked(
                        "{} repeats an identity with conflicting payload".format(
                            method_name
                        )
                    )
                items_by_identity[identity] = item
                if cutoff is None or timestamp_field is None:
                    retained_by_identity[identity] = item
                    continue
                item_time = _required_item_time(
                    item,
                    timestamp_field,
                    method_name,
                )
                if item_time >= cutoff:
                    retained_by_identity[identity] = item
            # A timestamp cutoff cannot prove that a subsequent page contains
            # only older records: the exchange may return a page in reverse or
            # arbitrary order.  Only a short/empty page proves that this cursor
            # traversal is complete, so keep paging even after crossing it.
            if len(page_items) < PAGE_LIMIT:
                if oldest_fetched_at is None:
                    raise OkxDemoReconciliationBlocked(
                        "{} pagination returned no freshness evidence".format(
                            method_name
                        )
                    )
                return (
                    list(retained_by_identity.values()),
                    _pagination_watermark(
                        method_name=method_name,
                        cutoff=cutoff,
                        terminal_cursor=cursor,
                        page_count=page_count,
                        items_by_identity=items_by_identity,
                    ),
                    oldest_fetched_at,
                )
            next_cursor = str(
                min(
                    _pagination_cursor(
                        item,
                        cursor_field or identity_field,
                        method_name,
                    )
                    for item in page_items
                )
            )
            if (
                next_cursor in seen_cursors
                or (cursor is not None and int(next_cursor) >= int(cursor))
            ):
                raise OkxDemoReconciliationBlocked(
                    "{} pagination cursor did not advance".format(method_name)
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise OkxDemoReconciliationBlocked(
            "{} pagination exceeded the bounded page count".format(method_name)
        )

    def _account_payload(
        self,
        balances: list[dict[str, Any]],
        fallback_time: datetime,
    ) -> tuple[dict[str, Any], datetime]:
        if not balances:
            raise OkxDemoReconciliationBlocked(
                "authenticated account baseline contained no balance rows"
            )
        total_values = [
            Decimal(str(item["total_equity"]))
            for item in balances
            if item.get("total_equity") not in (None, "")
        ]
        equity = max(total_values) if total_values else sum(
            (
                Decimal(str(item.get("equity") or "0"))
                for item in balances
            ),
            Decimal("0"),
        )
        available = sum(
            (
                Decimal(str(item.get("available_balance") or "0"))
                for item in balances
            ),
            Decimal("0"),
        )
        margin = max(equity - available, Decimal("0"))
        observed = max(
            (_item_time(item, "timestamp", fallback_time) for item in balances),
            default=fallback_time,
        )
        return (
            {
                "accountFingerprint": self._fingerprint,
                "equity": format(equity, "f"),
                "availableBalance": format(available, "f"),
                "marginBalance": format(margin, "f"),
            },
            observed,
        )


def create_runtime_reconciliation_adapter() -> OkxDemoRuntimeReconciliationAdapter:
    return OkxDemoRuntimeReconciliationAdapter()


def _event(
    kind: str,
    entity_key: str,
    payload: Mapping[str, Any],
    observed_at: datetime,
    source_sequence: int,
    stream_generation: int,
) -> dict[str, Any]:
    observed = _aware(observed_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_target": "OKX_DEMO",
        "source": "REST",
        "entity_kind": kind,
        "entity_key": entity_key,
        "source_sequence": source_sequence,
        "stream_generation": stream_generation,
        "observed_at": observed.isoformat(),
        "received_at": observed.isoformat(),
        "payload": dict(payload),
    }


def _item_time(
    item: Mapping[str, Any],
    field: str,
    fallback: datetime,
) -> datetime:
    value = item.get(field)
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str):
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return fallback


def _required_item_time(
    item: Mapping[str, Any],
    field: str,
    method_name: str,
) -> datetime:
    value = item.get(field)
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str):
        try:
            return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            pass
    raise OkxDemoReconciliationBlocked(
        "{} pagination item lacks a valid {} timestamp".format(
            method_name,
            field,
        )
    )


def _item_identity(
    item: Mapping[str, Any],
    identity_field: str,
    method_name: str,
) -> str:
    value = item.get(identity_field)
    identity = str(value) if value is not None else ""
    if not identity:
        raise OkxDemoReconciliationBlocked(
            "{} page contains an item without identity".format(method_name)
        )
    return identity


def _pagination_cursor(
    item: Mapping[str, Any],
    cursor_field: str,
    method_name: str,
) -> int:
    """Return the authoritative OKX cursor without conflating business identity."""
    value = item.get(cursor_field)
    cursor = str(value) if value is not None else ""
    if not cursor:
        raise OkxDemoReconciliationBlocked(
            "{} page contains an item without pagination cursor".format(
                method_name
            )
        )
    if not cursor.isdigit() or cursor != str(int(cursor)):
        raise OkxDemoReconciliationBlocked(
            "{} pagination cursor is not canonical numeric".format(
                method_name
            )
        )
    return int(cursor)


def _epoch_millis(value: datetime) -> str:
    return str(int(_aware(value).timestamp() * 1000))


def _pagination_watermark(
    *,
    method_name: str,
    cutoff: Optional[datetime],
    terminal_cursor: Optional[str],
    page_count: int,
    items_by_identity: Mapping[str, Mapping[str, Any]],
) -> str:
    """Bind the terminal cursor to every observed identity and payload."""
    material = {
        "method": method_name,
        "cutoff": cutoff.isoformat() if cutoff is not None else None,
        "terminal_cursor": terminal_cursor,
        "page_count": page_count,
        "items": [
            [identity, dict(items_by_identity[identity])]
            for identity in sorted(items_by_identity, key=int)
        ],
    }
    return hashlib.sha256(
        json.dumps(
            material,
            default=str,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _latest_closed_candle_open(now: datetime, timeframe: str) -> datetime:
    if len(timeframe) < 2 or not timeframe[:-1].isdigit():
        raise OkxDemoReconciliationBlocked(
            "strategy deployment timeframe is invalid"
        )
    count = int(timeframe[:-1])
    units = {
        "m": timedelta(minutes=count),
        "h": timedelta(hours=count),
        "d": timedelta(days=count),
        "w": timedelta(weeks=count),
    }
    interval = units.get(timeframe[-1])
    if count < 1 or interval is None:
        raise OkxDemoReconciliationBlocked(
            "strategy deployment timeframe is invalid"
        )
    active_now = _aware(now)
    anchor = (
        datetime(1970, 1, 5, tzinfo=timezone.utc)
        if timeframe[-1] == "w"
        else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )
    elapsed = active_now - anchor
    completed_intervals = elapsed // interval
    return anchor + (completed_intervals - 1) * interval


def _fresh_reconciliation_allows_opening(
    db: Session,
    *,
    now: datetime,
) -> bool:
    state = db.scalars(
        select(OkxDemoReconciliationState).where(
            OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
        )
    ).first()
    run = (
        db.get(ReconciliationRun, state.last_reconciliation_run_id)
        if state is not None and state.last_reconciliation_run_id is not None
        else None
    )
    if (
        state is None
        or state.status not in {"RECONCILED", "RECOVERED"}
        or state.opening_frozen
        or run is None
        or run.execution_target_id != "OKX_DEMO"
        or run.status not in {"RECONCILED", "RECOVERED"}
        or run.completed_at is None
        or run.authoritative_observed_at is None
    ):
        return False
    maximum_age = timedelta(seconds=MAX_RUNTIME_RECONCILIATION_AGE_SECONDS)
    completed_age = now - _database_aware(run.completed_at)
    observed_age = now - _database_aware(run.authoritative_observed_at)
    return (
        -timedelta(seconds=5) <= completed_age <= maximum_age
        and -timedelta(seconds=5) <= observed_age <= maximum_age
    )


def _next_unconsumed_approved_execution(
    db: Session,
    *,
    now: datetime,
) -> Optional[ClaimedApprovedExecution]:
    consumed = (
        select(OkxOrderWriteAttempt.id)
        .where(
            OkxOrderWriteAttempt.approval_id == ApprovedExecution.id,
            OkxOrderWriteAttempt.operation.in_(("PLACE", "CLOSE")),
        )
        .exists()
    )
    completed_risk = (
        select(FullChainStageRun.id)
        .where(
            FullChainStageRun.full_chain_run_id == FullChainRun.id,
            FullChainStageRun.stage == "RISK",
            FullChainStageRun.status == "SUCCESS",
        )
        .exists()
    )
    row = db.execute(
        select(
            ApprovedExecution,
            TradeIntent,
            RiskDecision,
            FullChainRun,
            SignalEvaluation,
        )
        .join(
            TradeIntent,
            TradeIntent.id == ApprovedExecution.trade_intent_id,
        )
        .join(
            RiskDecision,
            RiskDecision.id == ApprovedExecution.risk_decision_id,
        )
        .join(
            FullChainRun,
            FullChainRun.approved_execution_id == ApprovedExecution.id,
        )
        .join(
            SignalEvaluation,
            SignalEvaluation.id == FullChainRun.signal_evaluation_id,
        )
        .where(
            ApprovedExecution.execution_target_id == "OKX_DEMO",
            ApprovedExecution.status == "ACTIVE",
            ApprovedExecution.claim_required.is_(True),
            ApprovedExecution.order_submission_authorized.is_(False),
            ApprovedExecution.expires_at > now,
            TradeIntent.execution_target_id == "OKX_DEMO",
            TradeIntent.status == "APPROVED",
            TradeIntent.expires_at > now,
            RiskDecision.execution_target_id == "OKX_DEMO",
            RiskDecision.decision == "APPROVED",
            FullChainRun.execution_target_id == "OKX_DEMO",
            FullChainRun.run_kind == "EXECUTION",
            FullChainRun.signal_evaluation_id.is_not(None),
            FullChainRun.trade_intent_id == ApprovedExecution.trade_intent_id,
            FullChainRun.risk_decision_id == ApprovedExecution.risk_decision_id,
            FullChainRun.status == "EXECUTING",
            FullChainRun.current_stage == "EXECUTION",
            SignalEvaluation.execution_target_id == "OKX_DEMO",
            SignalEvaluation.status == "ACTIONABLE",
            completed_risk,
            ~consumed,
        )
        .order_by(ApprovedExecution.created_at, ApprovedExecution.id)
        .limit(1)
    ).first()
    if row is None:
        return None
    approved, intent, decision, chain, evaluation = row
    result = evaluation.result_snapshot or {}
    expected = {
        "full_chain_run_id": chain.id,
        "trade_intent_id": intent.id,
        "risk_decision_id": decision.id,
        "approved_execution_id": approved.id,
    }
    if any(result.get(key) != value for key, value in expected.items()):
        raise OkxDemoReconciliationBlocked(
            "ACTIONABLE evaluation result does not bind exact execution lineage"
        )
    return _project_approved_execution(approved, intent, decision)


def _approved_execution_by_id(
    db: Session,
    *,
    approval_id: int,
) -> Optional[ClaimedApprovedExecution]:
    row = db.execute(
        select(ApprovedExecution, TradeIntent, RiskDecision)
        .join(
            TradeIntent,
            TradeIntent.id == ApprovedExecution.trade_intent_id,
        )
        .join(
            RiskDecision,
            RiskDecision.id == ApprovedExecution.risk_decision_id,
        )
        .where(
            ApprovedExecution.id == approval_id,
            ApprovedExecution.execution_target_id == "OKX_DEMO",
        )
    ).first()
    return _project_approved_execution(*row) if row is not None else None


def _project_approved_execution(
    approved: ApprovedExecution,
    intent: TradeIntent,
    decision: RiskDecision,
) -> ClaimedApprovedExecution:
    expires_at = min(
        _database_aware(value)
        for value in (approved.expires_at, intent.expires_at)
        if value is not None
    )
    return ClaimedApprovedExecution(
        approval_id=approved.id,
        trade_intent_id=intent.id,
        risk_decision_id=decision.id,
        execution_target_id="OKX_DEMO",
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
        approved_at=_database_aware(approved.created_at),
        expires_at=expires_at,
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


def _database_aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise OkxDemoReconciliationBlocked(
            "runtime reconciliation timestamp must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _item_mapping(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        value = model_dump(mode="json")
        if isinstance(value, Mapping):
            return dict(value)
    raise OkxDemoReconciliationBlocked(
        "runtime normalized item is not a stable mapping"
    )
