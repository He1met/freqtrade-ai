from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import os
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.okx_demo.read_adapter import OkxDemoReadClient
from app.services.okx_demo_reconciliation import (
    DEFAULT_ALLOWED_EVIDENCE_ROOT,
    DEFAULT_EVIDENCE_ROOT,
    OkxDemoReconciliationBlocked,
    OkxDemoReconciliationService,
    RECOVERY_STREAMS,
    SCHEMA_VERSION,
)
from app.models.okx_demo_reconciliation import (
    OkxDemoReconciliationState,
    OkxDemoRecoveryGrant,
)
from app.models.order_writer import OkxOrderWriteAttempt


PAGE_LIMIT = 100
MAX_PAGES = 100
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
        del read_client
        pending = list(
            db.scalars(
                select(OkxOrderWriteAttempt)
                .where(
                    OkxOrderWriteAttempt.execution_target_id == "OKX_DEMO",
                    OkxOrderWriteAttempt.recovery_grant_database_id.is_not(
                        None
                    ),
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
            if attempt.operation == "CANCEL":
                writer.recovery_cancel(
                    recovery_grant_database_id=(
                        attempt.recovery_grant_database_id
                    )
                )
            elif attempt.operation == "CLOSE":
                writer.recovery_reduce_only(
                    recovery_grant_database_id=(
                        attempt.recovery_grant_database_id
                    )
                )
            else:
                raise OkxDemoReconciliationBlocked(
                    "unresolved runtime recovery operation is unsupported"
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
        self._stream_generation += 1
        pending, pending_water, pending_observed = self._pages(
            read_client,
            "pending_orders",
            identity_field="order_id",
        )
        history, history_water, history_observed = self._pages(
            read_client,
            "orders_history",
            identity_field="order_id",
        )
        fills, fill_water, fills_observed = self._pages(
            read_client,
            "fills_history",
            identity_field="fill_id",
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
                self._last_completed_at - timedelta(seconds=5)
                if self._last_completed_at is not None
                else started_at
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
    ) -> tuple[list[dict[str, Any]], str, datetime]:
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
        oldest_fetched_at: Optional[datetime] = None
        for _page in range(MAX_PAGES):
            snapshot = method(after=cursor, limit=PAGE_LIMIT)
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
                identity = str(item.get(identity_field, ""))
                if not identity:
                    raise OkxDemoReconciliationBlocked(
                        "{} page contains an item without identity".format(
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
            if len(page_items) < PAGE_LIMIT:
                watermark = cursor or "EMPTY"
                if oldest_fetched_at is None:
                    raise OkxDemoReconciliationBlocked(
                        "{} pagination returned no freshness evidence".format(
                            method_name
                        )
                    )
                return (
                    list(items_by_identity.values()),
                    hashlib.sha256(str(watermark).encode()).hexdigest(),
                    oldest_fetched_at,
                )
            next_cursor = str(page_items[-1].get(identity_field, ""))
            if not next_cursor or next_cursor in seen_cursors:
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
