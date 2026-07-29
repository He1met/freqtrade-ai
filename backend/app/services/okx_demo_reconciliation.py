from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import REPO_ROOT
from app.models import (
    ExchangeFill,
    ExchangeOrder,
    ExchangePosition,
    OkxDemoAccountSnapshot,
    OkxDemoExchangeEvent,
    OkxDemoFillSnapshot,
    OkxDemoOrderSnapshot,
    OkxDemoPositionSnapshot,
    OkxDemoReconciliationState,
    OkxDemoRecoveryBatch,
    OkxDemoRecoveryGrant,
    ReconciliationRun,
    TradeIntent,
)
from app.models.execution_lineage import OKX_DEMO_TARGET_ID
from app.repositories.execution_lineage import (
    ExecutionLineageRepository,
    ensure_execution_scope_catalog,
)


SCHEMA_VERSION = "OKX_DEMO_RECON_V1"
VALID_STATUSES = frozenset(
    {"RECONCILED", "DRIFTED", "STALE", "UNKNOWN", "RECOVERED"}
)
RECOVERY_STREAMS = frozenset({"ORDER", "FILL", "POSITION", "ACCOUNT"})
DEFAULT_ALLOWED_EVIDENCE_ROOT = REPO_ROOT / ".freqtrade-ai" / "evidence"
DEFAULT_EVIDENCE_ROOT = DEFAULT_ALLOWED_EVIDENCE_ROOT / "okx-demo-reconciliation"
SAFE_PAYLOAD_FIELDS = {
    "ORDER": frozenset(
        {
            "ordId",
            "clOrdId",
            "instId",
            "state",
            "sz",
            "accFillSz",
            "avgPx",
            "reduceOnly",
        }
    ),
    "FILL": frozenset(
        {"fillId", "ordId", "instId", "fillPx", "fillSz", "fee"}
    ),
    "POSITION": frozenset({"instId", "posSide", "pos", "avgPx"}),
    "ACCOUNT": frozenset(
        {
            "accountFingerprint",
            "equity",
            "availableBalance",
            "marginBalance",
        }
    ),
}


class OkxDemoReconciliationBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class IngestedEvent:
    database_id: int
    snapshot_database_id: int
    event_key: str
    duplicate: bool
    out_of_order: bool


@dataclass(frozen=True)
class ReconciliationResult:
    reconciliation_run_database_id: int
    state_database_id: int
    status: str
    opening_frozen: bool
    database_ids: dict[str, Any]
    artifact_path: str
    artifact_sha256: str
    findings: tuple[dict[str, Any], ...]


class OkxDemoReconciliationService:
    """Append authoritative OKX Demo evidence and compare it with local lineage."""

    def __init__(
        self,
        db: Session,
        *,
        evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
        allowed_evidence_root: Path = DEFAULT_ALLOWED_EVIDENCE_ROOT,
    ) -> None:
        self.db = db
        self._allowed_evidence_root = Path(allowed_evidence_root).expanduser()
        self._evidence_root = Path(evidence_root).expanduser()

    def ingest_event(self, raw_event: Mapping[str, Any]) -> IngestedEvent:
        event = _normalize_event(raw_event)
        ensure_execution_scope_catalog(self.db)
        self._lock_event_identity(event)
        if event["observed_at"] > event["received_at"] + timedelta(seconds=30):
            self._mark_unknown("AUTHORITATIVE_EVENT_FROM_FUTURE")
            raise OkxDemoReconciliationBlocked(
                "authoritative event timestamp is in the future"
            )
        same_timestamp = self.db.scalars(
            select(OkxDemoExchangeEvent).where(
                OkxDemoExchangeEvent.execution_target_id == OKX_DEMO_TARGET_ID,
                OkxDemoExchangeEvent.entity_kind == event["entity_kind"],
                OkxDemoExchangeEvent.entity_key == event["entity_key"],
                OkxDemoExchangeEvent.observed_at == event["observed_at"],
            )
        ).first()
        if (
            same_timestamp is not None
            and same_timestamp.payload_digest != event["payload_digest"]
        ):
            self._mark_unknown("SAME_TIMESTAMP_DIGEST_CONFLICT")
            raise OkxDemoReconciliationBlocked(
                "same-timestamp authoritative evidence has conflicting digests"
            )
        latest_generation = self.db.scalars(
            select(OkxDemoExchangeEvent.stream_generation)
            .where(
                OkxDemoExchangeEvent.execution_target_id == OKX_DEMO_TARGET_ID,
                OkxDemoExchangeEvent.source == event["source"],
                OkxDemoExchangeEvent.entity_kind == event["entity_kind"],
            )
            .order_by(OkxDemoExchangeEvent.stream_generation.desc())
            .limit(1)
        ).first()
        if (
            latest_generation is not None
            and event["stream_generation"] < latest_generation
        ):
            self._mark_unknown("STALE_STREAM_GENERATION")
            raise OkxDemoReconciliationBlocked(
                "exchange event belongs to an old stream generation"
            )
        existing = self.db.scalars(
            select(OkxDemoExchangeEvent).where(
                OkxDemoExchangeEvent.execution_target_id == OKX_DEMO_TARGET_ID,
                OkxDemoExchangeEvent.event_key == event["event_key"],
            )
        ).first()
        if existing is not None:
            if (
                existing.payload_digest != event["payload_digest"]
                or existing.entity_kind != event["entity_kind"]
                or existing.entity_key != event["entity_key"]
                or _aware(existing.observed_at) != event["observed_at"]
            ):
                raise OkxDemoReconciliationBlocked(
                    "event key replay differs from persisted authoritative evidence"
                )
            snapshot_database_id = self._snapshot_database_id(existing)
            return IngestedEvent(
                database_id=existing.database_id,
                snapshot_database_id=snapshot_database_id,
                event_key=existing.event_key,
                duplicate=True,
                out_of_order=self._is_out_of_order(existing),
            )
        latest_observed_at = self.db.scalars(
            select(OkxDemoExchangeEvent.observed_at)
            .where(
                OkxDemoExchangeEvent.execution_target_id == OKX_DEMO_TARGET_ID,
                OkxDemoExchangeEvent.entity_kind == event["entity_kind"],
                OkxDemoExchangeEvent.entity_key == event["entity_key"],
            )
            .order_by(
                OkxDemoExchangeEvent.observed_at.desc(),
                OkxDemoExchangeEvent.database_id.desc(),
            )
            .limit(1)
        ).first()
        try:
            with self.db.begin_nested():
                row = OkxDemoExchangeEvent(**event)
                self.db.add(row)
                self.db.flush()
                snapshot = self._persist_snapshot(row)
        except (IntegrityError, OkxDemoReconciliationBlocked) as exc:
            raise OkxDemoReconciliationBlocked(
                "authoritative event could not be persisted exactly once"
            ) from exc
        return IngestedEvent(
            database_id=row.database_id,
            snapshot_database_id=snapshot.database_id,
            event_key=row.event_key,
            duplicate=False,
            out_of_order=(
                latest_observed_at is not None
                and _aware(latest_observed_at) > event["observed_at"]
            ),
        )

    def ingest_recovery_batch(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        recovery_batch_id: str,
        authenticated: bool = True,
        pagination_complete: bool = True,
        complete_streams: Iterable[str] = RECOVERY_STREAMS,
        high_watermarks: Optional[Mapping[str, str]] = None,
        overlap_started_at: Optional[datetime] = None,
        observed_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
    ) -> list[IngestedEvent]:
        if not recovery_batch_id or len(recovery_batch_id) > 64:
            raise OkxDemoReconciliationBlocked("recovery batch identity is invalid")
        stream_set = {str(item).upper() for item in complete_streams}
        if (
            authenticated is not True
            or pagination_complete is not True
            or stream_set != RECOVERY_STREAMS
        ):
            raise OkxDemoReconciliationBlocked(
                "REST recovery requires authenticated complete coverage"
            )
        waters = dict(high_watermarks or {})
        if set(waters) != RECOVERY_STREAMS or any(
            not str(value) for value in waters.values()
        ):
            raise OkxDemoReconciliationBlocked(
                "REST recovery high-water evidence is incomplete"
            )
        completed = _aware(completed_at or datetime.now(timezone.utc))
        observed = _aware(observed_at or completed)
        overlap = _aware(overlap_started_at or observed)
        if overlap > observed or observed > completed:
            raise OkxDemoReconciliationBlocked(
                "REST recovery overlap/freshness evidence is invalid"
            )
        event_list = [dict(item) for item in events]
        evidence = {
            "recovery_batch_id": recovery_batch_id,
            "complete_streams": sorted(stream_set),
            "high_watermarks": waters,
            "overlap_started_at": overlap.isoformat(),
            "observed_at": observed.isoformat(),
            "completed_at": completed.isoformat(),
            "event_count": len(event_list),
        }
        batch = OkxDemoRecoveryBatch(
            execution_target_id=OKX_DEMO_TARGET_ID,
            recovery_batch_id=recovery_batch_id,
            authenticated=True,
            pagination_complete=True,
            complete_streams=sorted(stream_set),
            high_watermarks=waters,
            overlap_started_at=overlap,
            observed_at=observed,
            completed_at=completed,
            event_count=len(event_list),
            evidence_digest=_digest(evidence),
        )
        self.db.add(batch)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise OkxDemoReconciliationBlocked(
                "REST recovery batch identity already exists"
            ) from exc
        results = []
        for item in event_list:
            normalized = dict(item)
            normalized["source"] = "REST"
            normalized["recovery_batch_id"] = recovery_batch_id
            normalized["recovery_batch_database_id"] = batch.database_id
            results.append(self.ingest_event(normalized))
        return results

    def mark_stream_stale(self, *, observed_at: datetime, reason: str) -> int:
        if self.db.get_bind().dialect.name == "postgresql":
            return self.db.execute(
                text(
                    "SELECT freeze_okx_demo_reconciliation_gate("
                    "'STALE', :reason, :observed_at)"
                ),
                {
                    "reason": _short_reason(
                        reason or "OKX websocket disconnected"
                    ),
                    "observed_at": _aware(observed_at),
                },
            ).scalar_one()
        state = self._lock_state()
        state.status = "STALE"
        state.opening_frozen = True
        state.block_reason = _short_reason(reason or "OKX websocket disconnected")
        state.last_event_observed_at = _aware(observed_at)
        self.db.flush()
        return state.database_id

    def claim_recovery_grant(
        self,
        grant_database_id: int,
        *,
        action: str,
        now: datetime,
        quantity: Decimal = Decimal("0"),
    ) -> dict[str, Any]:
        now = _aware(now)
        action = str(action).upper()
        grant = self.db.scalars(
            select(OkxDemoRecoveryGrant)
            .where(
                OkxDemoRecoveryGrant.database_id == grant_database_id,
                OkxDemoRecoveryGrant.execution_target_id
                == OKX_DEMO_TARGET_ID,
            )
            .with_for_update()
        ).first()
        if (
            grant is None
            or grant.status != "ACTIVE"
            or _aware(grant.expires_at) <= now
            or grant.action != action
            or quantity < 0
            or (
                action == "CANCEL"
                and (
                    grant.exchange_order_row_id is None
                    or quantity != 0
                )
            )
            or (
                action == "REDUCE_ONLY"
                and (quantity <= 0 or quantity > grant.max_quantity)
            )
        ):
            if grant is not None and _aware(grant.expires_at) <= now:
                grant.status = "EXPIRED"
            raise OkxDemoReconciliationBlocked(
                "recovery grant is not valid for the requested risk-reducing action"
            )
        state = self.db.scalars(
            select(OkxDemoReconciliationState)
            .where(
                OkxDemoReconciliationState.execution_target_id
                == OKX_DEMO_TARGET_ID
            )
            .with_for_update()
        ).one()
        if (
            state.last_reconciliation_run_id != grant.reconciliation_run_id
            or state.status not in {"DRIFTED", "STALE"}
            or not state.opening_frozen
        ):
            raise OkxDemoReconciliationBlocked(
                "recovery grant is not bound to the current frozen state"
            )
        grant.status = "CONSUMED"
        grant.consumed_at = now
        self.db.flush()
        return {
            "recovery_grant_database_id": grant.database_id,
            "reconciliation_run_database_id": grant.reconciliation_run_id,
            "execution_target": OKX_DEMO_TARGET_ID,
            "action": grant.action,
            "exchange_order_row_id": grant.exchange_order_row_id,
            "instrument_id": grant.instrument_id,
            "position_side": grant.position_side,
            "max_quantity": format(grant.max_quantity, "f"),
            "grant_digest": grant.grant_digest,
        }

    def reconcile(
        self,
        *,
        now: datetime,
        stale_after: timedelta = timedelta(minutes=2),
        recovered: bool = False,
    ) -> ReconciliationResult:
        now = _aware(now)
        _cleanup_orphan_artifacts(
            self.db,
            self._evidence_root,
            self._allowed_evidence_root,
        )
        if stale_after <= timedelta(0):
            raise OkxDemoReconciliationBlocked("stale_after must be positive")
        ensure_execution_scope_catalog(self.db)
        complete_batch = self._latest_complete_recovery_batch(
            now=now,
            stale_after=stale_after,
        )
        latest_event_at = self.db.scalars(
            select(OkxDemoExchangeEvent.observed_at)
            .where(
                OkxDemoExchangeEvent.execution_target_id == OKX_DEMO_TARGET_ID,
                OkxDemoExchangeEvent.recovery_batch_database_id
                == (
                    complete_batch.database_id
                    if complete_batch is not None
                    else -1
                ),
            )
            .order_by(
                OkxDemoExchangeEvent.observed_at.desc(),
                OkxDemoExchangeEvent.database_id.desc(),
            )
            .limit(1)
        ).first()
        findings: list[dict[str, Any]] = []
        repaired_order_ids: list[int] = []
        batch_database_id = (
            complete_batch.database_id if complete_batch is not None else -1
        )
        latest_orders = self._latest_orders(batch_database_id)
        latest_positions = self._latest_positions(batch_database_id)
        latest_account = self._latest_account(batch_database_id)
        latest_fills = self._latest_fills(batch_database_id)

        if complete_batch is None or latest_event_at is None:
            status = "UNKNOWN"
            findings.append(
                _finding("COMPLETE_BASELINE_MISSING_OR_STALE", "BLOCKED", "all")
            )
        else:
            self._compare_orders(
                latest_orders,
                findings,
                repaired_order_ids,
                complete_snapshot=complete_batch is not None,
            )
            self._bridge_managed_fills(latest_fills, findings)
            self._compare_fills(latest_fills, findings)
            self._compare_positions(latest_positions, findings)
            self._compare_account(latest_account, latest_positions, findings)
            if any(item["severity"] == "BLOCKED" for item in findings):
                status = "DRIFTED"
            elif recovered and complete_batch is not None:
                status = "RECOVERED"
            elif recovered:
                status = "UNKNOWN"
                findings.append(
                    _finding("RECOVERY_BASELINE_INCOMPLETE", "BLOCKED", "all")
                )
            else:
                status = "RECONCILED"

        opening_frozen = status in {"DRIFTED", "STALE", "UNKNOWN"}
        run = ReconciliationRun(
            execution_target_id=OKX_DEMO_TARGET_ID,
            status=status,
            summary_snapshot={},
            database_ids={},
            authoritative_observed_at=latest_event_at,
            source_type="api_aggregate",
            core_data=True,
            artifact_status="PENDING",
            started_at=now,
            completed_at=now,
        )
        self.db.add(run)
        self.db.flush()
        state = self._read_state()
        database_ids = {
            "reconciliation_run": [run.id],
            "exchange_events": sorted(
                {
                    row.event_database_id
                    for row in (
                        list(latest_orders.values())
                        + list(latest_positions.values())
                        + list(latest_fills.values())
                        + ([latest_account] if latest_account is not None else [])
                    )
                }
            ),
            "order_snapshots": sorted(row.database_id for row in latest_orders.values()),
            "fill_snapshots": sorted(row.database_id for row in latest_fills.values()),
            "position_snapshots": sorted(
                row.database_id for row in latest_positions.values()
            ),
            "account_snapshots": (
                [latest_account.database_id] if latest_account is not None else []
            ),
            "repaired_exchange_orders": sorted(repaired_order_ids),
            "recovery_batches": (
                [complete_batch.database_id] if complete_batch is not None else []
            ),
            "reconciliation_state": [state.database_id],
        }
        recovery_grants = self._issue_recovery_grants(
            run,
            status=status,
            authoritative_orders=latest_orders,
            authoritative_positions=latest_positions,
            now=now,
            expires_at=(
                min(
                    now + stale_after,
                    _aware(complete_batch.completed_at) + stale_after,
                )
                if complete_batch is not None
                else now
            ),
            complete_snapshot=complete_batch is not None,
        )
        database_ids["recovery_grants"] = [
            row.database_id for row in recovery_grants
        ]
        evidence = {
            "schema_version": SCHEMA_VERSION,
            "execution_target": OKX_DEMO_TARGET_ID,
            "source_type": "api_aggregate",
            "core_data": True,
            "status": status,
            "opening_frozen": opening_frozen,
            "database_ids": database_ids,
            "authoritative_observed_at": (
                _aware(latest_event_at).isoformat()
                if latest_event_at is not None
                else None
            ),
            "completed_at": now.isoformat(),
            "findings": findings,
        }
        artifact_path, artifact_sha256 = _write_artifact(
            self._evidence_root,
            self._allowed_evidence_root,
            run.id,
            evidence,
        )
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(
                text(
                    "SELECT finalize_okx_demo_reconciliation_run("
                    ":run_id, CAST(:summary AS jsonb), "
                    "CAST(:database_ids AS jsonb), :artifact_path, "
                    ":artifact_sha256)"
                ),
                {
                    "run_id": run.id,
                    "summary": json.dumps(evidence, sort_keys=True),
                    "database_ids": json.dumps(database_ids, sort_keys=True),
                    "artifact_path": artifact_path,
                    "artifact_sha256": artifact_sha256,
                },
            ).scalar_one()
            self.db.expire(run)
            self.db.refresh(run)
            state_id = self.db.execute(
                text(
                    "SELECT apply_okx_demo_reconciliation_gate(:run_id)"
                ),
                {"run_id": run.id},
            ).scalar_one()
            if state_id != state.database_id:
                raise OkxDemoReconciliationBlocked(
                    "controlled reconciliation gate returned the wrong state"
                )
            self.db.expire(state)
            self.db.refresh(state)
        else:
            run.summary_snapshot = evidence
            run.database_ids = database_ids
            run.artifact_path = artifact_path
            run.artifact_sha256 = artifact_sha256
            run.artifact_status = "READY"
            state.status = status
            state.opening_frozen = opening_frozen
            state.block_reason = (
                findings[0]["code"] if opening_frozen and findings else None
            )
            state.last_event_observed_at = latest_event_at
            state.last_reconciliation_run_id = run.id
            self.db.flush()
        return ReconciliationResult(
            reconciliation_run_database_id=run.id,
            state_database_id=state.database_id,
            status=status,
            opening_frozen=opening_frozen,
            database_ids=database_ids,
            artifact_path=artifact_path,
            artifact_sha256=artifact_sha256,
            findings=tuple(findings),
        )

    def _persist_snapshot(self, event: OkxDemoExchangeEvent) -> Any:
        payload = event.payload
        common = {
            "execution_target_id": OKX_DEMO_TARGET_ID,
            "event_database_id": event.database_id,
            "authoritative_snapshot": payload,
            "observed_at": event.observed_at,
        }
        if event.entity_kind == "ORDER":
            row = OkxDemoOrderSnapshot(
                **common,
                exchange_order_id=_text(payload, "ordId"),
                client_order_id=_optional_text(payload.get("clOrdId")),
                instrument_id=_text(payload, "instId"),
                status=_text(payload, "state"),
                quantity=_decimal(payload, "sz"),
                filled_quantity=_decimal(payload, "accFillSz"),
                average_price=_optional_decimal(payload.get("avgPx")),
                reduce_only=_boolean(payload.get("reduceOnly", False)),
            )
        elif event.entity_kind == "FILL":
            row = OkxDemoFillSnapshot(
                **common,
                exchange_fill_id=_text(payload, "fillId"),
                exchange_order_id=_text(payload, "ordId"),
                instrument_id=_text(payload, "instId"),
                price=_decimal(payload, "fillPx"),
                quantity=_decimal(payload, "fillSz"),
                fee=_optional_decimal(payload.get("fee")),
            )
        elif event.entity_kind == "POSITION":
            position_side = _text(payload, "posSide")
            if position_side not in {"long", "short"}:
                raise OkxDemoReconciliationBlocked(
                    "OKX long_short_mode requires position posSide=long or short"
                )
            row = OkxDemoPositionSnapshot(
                **common,
                instrument_id=_text(payload, "instId"),
                position_side=position_side,
                quantity=_decimal(payload, "pos"),
                average_price=_optional_decimal(payload.get("avgPx")),
            )
        else:
            fingerprint = _text(payload, "accountFingerprint")
            if len(fingerprint) != 64:
                raise OkxDemoReconciliationBlocked(
                    "account fingerprint must be a sha256 digest"
                )
            row = OkxDemoAccountSnapshot(
                **common,
                account_fingerprint_sha256=fingerprint,
                equity=_decimal(payload, "equity"),
                available_balance=_decimal(payload, "availableBalance"),
                margin_balance=_decimal(payload, "marginBalance"),
            )
        self.db.add(row)
        self.db.flush()
        return row

    def _snapshot_database_id(self, event: OkxDemoExchangeEvent) -> int:
        model = {
            "ORDER": OkxDemoOrderSnapshot,
            "FILL": OkxDemoFillSnapshot,
            "POSITION": OkxDemoPositionSnapshot,
            "ACCOUNT": OkxDemoAccountSnapshot,
        }[event.entity_kind]
        value = self.db.scalars(
            select(model.database_id).where(
                model.event_database_id == event.database_id
            )
        ).one()
        return int(value)

    def _is_out_of_order(self, event: OkxDemoExchangeEvent) -> bool:
        latest = self.db.scalars(
            select(OkxDemoExchangeEvent.database_id)
            .where(
                OkxDemoExchangeEvent.execution_target_id == OKX_DEMO_TARGET_ID,
                OkxDemoExchangeEvent.entity_kind == event.entity_kind,
                OkxDemoExchangeEvent.entity_key == event.entity_key,
            )
            .order_by(
                OkxDemoExchangeEvent.observed_at.desc(),
                OkxDemoExchangeEvent.database_id.desc(),
            )
            .limit(1)
        ).first()
        return latest is not None and latest != event.database_id

    def _latest_orders(
        self,
        batch_database_id: int,
    ) -> dict[str, OkxDemoOrderSnapshot]:
        rows = self.db.scalars(
            select(OkxDemoOrderSnapshot)
            .join(
                OkxDemoExchangeEvent,
                OkxDemoExchangeEvent.database_id
                == OkxDemoOrderSnapshot.event_database_id,
            )
            .where(OkxDemoOrderSnapshot.execution_target_id == OKX_DEMO_TARGET_ID)
            .where(
                OkxDemoExchangeEvent.recovery_batch_database_id
                == batch_database_id
            )
            .order_by(
                OkxDemoOrderSnapshot.observed_at.desc(),
                OkxDemoOrderSnapshot.database_id.desc(),
            )
        ).all()
        return _latest_by(rows, lambda row: row.exchange_order_id)

    def _latest_fills(
        self,
        batch_database_id: int,
    ) -> dict[str, OkxDemoFillSnapshot]:
        rows = self.db.scalars(
            select(OkxDemoFillSnapshot)
            .join(
                OkxDemoExchangeEvent,
                OkxDemoExchangeEvent.database_id
                == OkxDemoFillSnapshot.event_database_id,
            )
            .where(OkxDemoFillSnapshot.execution_target_id == OKX_DEMO_TARGET_ID)
            .where(
                OkxDemoExchangeEvent.recovery_batch_database_id
                == batch_database_id
            )
            .order_by(
                OkxDemoFillSnapshot.observed_at.desc(),
                OkxDemoFillSnapshot.database_id.desc(),
            )
        ).all()
        return _latest_by(rows, lambda row: row.exchange_fill_id)

    def _latest_positions(
        self,
        batch_database_id: int,
    ) -> dict[str, OkxDemoPositionSnapshot]:
        rows = self.db.scalars(
            select(OkxDemoPositionSnapshot)
            .join(
                OkxDemoExchangeEvent,
                OkxDemoExchangeEvent.database_id
                == OkxDemoPositionSnapshot.event_database_id,
            )
            .where(OkxDemoPositionSnapshot.execution_target_id == OKX_DEMO_TARGET_ID)
            .where(
                OkxDemoExchangeEvent.recovery_batch_database_id
                == batch_database_id
            )
            .order_by(
                OkxDemoPositionSnapshot.observed_at.desc(),
                OkxDemoPositionSnapshot.database_id.desc(),
            )
        ).all()
        return _latest_by(
            rows,
            lambda row: "{}:{}".format(row.instrument_id, row.position_side),
        )

    def _latest_account(
        self,
        batch_database_id: int,
    ) -> Optional[OkxDemoAccountSnapshot]:
        return self.db.scalars(
            select(OkxDemoAccountSnapshot)
            .join(
                OkxDemoExchangeEvent,
                OkxDemoExchangeEvent.database_id
                == OkxDemoAccountSnapshot.event_database_id,
            )
            .where(OkxDemoAccountSnapshot.execution_target_id == OKX_DEMO_TARGET_ID)
            .where(
                OkxDemoExchangeEvent.recovery_batch_database_id
                == batch_database_id
            )
            .order_by(
                OkxDemoAccountSnapshot.observed_at.desc(),
                OkxDemoAccountSnapshot.database_id.desc(),
            )
            .limit(1)
        ).first()

    def _compare_orders(
        self,
        authoritative: Mapping[str, OkxDemoOrderSnapshot],
        findings: list[dict[str, Any]],
        repaired_order_ids: list[int],
        *,
        complete_snapshot: bool,
    ) -> None:
        local_orders = self.db.scalars(
            select(ExchangeOrder).where(
                ExchangeOrder.execution_target_id == OKX_DEMO_TARGET_ID
            )
        ).all()
        local_by_exchange_id = {
            row.exchange_order_id: row
            for row in local_orders
            if row.exchange_order_id is not None
        }
        for exchange_order_id, snapshot in authoritative.items():
            local = local_by_exchange_id.get(exchange_order_id)
            if local is None:
                if snapshot.status not in {"filled", "canceled", "mmp_canceled"}:
                    findings.append(
                        _finding(
                            "AUTHORITATIVE_OPEN_ORDER_MISSING_LOCALLY",
                            "BLOCKED",
                            exchange_order_id,
                        )
                    )
                continue
            local_state = str(
                (local.response_snapshot or {}).get("state")
                or local.status
            ).lower()
            if local_state != snapshot.status:
                # Status is derived from an identity-bound authoritative order row.
                local.status = snapshot.status
                local.response_snapshot = {
                    **dict(local.response_snapshot or {}),
                    "state": snapshot.status,
                    "reconciled_snapshot_database_id": snapshot.database_id,
                }
                repaired_order_ids.append(local.id)
        if complete_snapshot:
            authoritative_ids = set(authoritative)
            for local in local_orders:
                local_status = str(local.status).lower()
                if (
                    local.exchange_order_id is not None
                    and local_status
                    not in {"filled", "canceled", "mmp_canceled", "reconciled"}
                    and local.exchange_order_id not in authoritative_ids
                ):
                    findings.append(
                        _finding(
                            "LOCAL_OPEN_ORDER_MISSING_AUTHORITATIVELY",
                            "BLOCKED",
                            local.exchange_order_id,
                        )
                    )

    def _compare_fills(
        self,
        authoritative: Mapping[str, OkxDemoFillSnapshot],
        findings: list[dict[str, Any]],
    ) -> None:
        managed_order_ids = set(
            self.db.scalars(
                select(ExchangeOrder.exchange_order_id).where(
                    ExchangeOrder.execution_target_id == OKX_DEMO_TARGET_ID,
                    ExchangeOrder.exchange_order_id.is_not(None),
                )
            ).all()
        )
        local_ids = set(
            self.db.scalars(
                select(ExchangeFill.exchange_fill_id).where(
                    ExchangeFill.execution_target_id == OKX_DEMO_TARGET_ID
                )
            ).all()
        )
        managed_authoritative_ids = {
            exchange_fill_id
            for exchange_fill_id, snapshot in authoritative.items()
            if snapshot.exchange_order_id in managed_order_ids
        }
        for exchange_fill_id in sorted(managed_authoritative_ids - local_ids):
            findings.append(
                _finding(
                    "AUTHORITATIVE_FILL_MISSING_LOCALLY",
                    "BLOCKED",
                    exchange_fill_id,
                )
            )

    def _compare_positions(
        self,
        authoritative: Mapping[str, OkxDemoPositionSnapshot],
        findings: list[dict[str, Any]],
    ) -> None:
        local_rows = self.db.scalars(
            select(ExchangePosition).where(
                ExchangePosition.execution_target_id == OKX_DEMO_TARGET_ID
            )
        ).all()
        local = {
            "{}:{}".format(row.instrument_id, row.position_side): Decimal(row.quantity)
            for row in local_rows
        }
        remote = {key: Decimal(row.quantity) for key, row in authoritative.items()}
        for identity in sorted(set(local) | set(remote)):
            if local.get(identity, Decimal("0")) != remote.get(
                identity, Decimal("0")
            ):
                findings.append(
                    _finding("POSITION_DRIFT", "BLOCKED", identity)
                )

    def _bridge_managed_fills(
        self,
        authoritative: Mapping[str, OkxDemoFillSnapshot],
        findings: list[dict[str, Any]],
    ) -> None:
        """Copy authoritative managed fills into execution lineage exactly once."""

        repository = ExecutionLineageRepository(self.db, OKX_DEMO_TARGET_ID)
        for exchange_fill_id in sorted(authoritative):
            fill_snapshot = authoritative[exchange_fill_id]
            orders = list(
                self.db.scalars(
                    select(ExchangeOrder).where(
                        ExchangeOrder.execution_target_id == OKX_DEMO_TARGET_ID,
                        ExchangeOrder.exchange_order_id
                        == fill_snapshot.exchange_order_id,
                    )
                ).all()
            )
            if not orders:
                # Account-level history also includes orders outside this project.
                continue
            if len(orders) != 1:
                findings.append(
                    _finding(
                        "AUTHORITATIVE_FILL_ORDER_AMBIGUOUS",
                        "BLOCKED",
                        exchange_fill_id,
                    )
                )
                continue
            event = self.db.get(
                OkxDemoExchangeEvent,
                fill_snapshot.event_database_id,
            )
            if event is None or event.payload_digest != _digest(
                dict(fill_snapshot.authoritative_snapshot)
            ):
                findings.append(
                    _finding(
                        "AUTHORITATIVE_FILL_EVIDENCE_INVALID",
                        "BLOCKED",
                        exchange_fill_id,
                    )
                )
                continue
            evidence = {
                "source": "okx_demo_reconciliation",
                "fill_snapshot_database_id": fill_snapshot.database_id,
                "event_database_id": event.database_id,
                "payload_digest": event.payload_digest,
                "authoritative_snapshot": dict(
                    fill_snapshot.authoritative_snapshot
                ),
                "observed_at": _aware(fill_snapshot.observed_at).isoformat(),
            }
            try:
                repository.record_fill_idempotently(
                    exchange_order_row_id=orders[0].id,
                    exchange_fill_id=fill_snapshot.exchange_fill_id,
                    price=Decimal(fill_snapshot.price),
                    quantity=Decimal(fill_snapshot.quantity),
                    fee=(
                        None
                        if fill_snapshot.fee is None
                        else Decimal(fill_snapshot.fee)
                    ),
                    snapshot=evidence,
                )
            except ValueError:
                findings.append(
                    _finding(
                        "AUTHORITATIVE_FILL_LINEAGE_CONFLICT",
                        "BLOCKED",
                        exchange_fill_id,
                    )
                )

    def _compare_account(
        self,
        account: Optional[OkxDemoAccountSnapshot],
        positions: Mapping[str, OkxDemoPositionSnapshot],
        findings: list[dict[str, Any]],
    ) -> None:
        if account is None:
            findings.append(_finding("ACCOUNT_STATE_MISSING", "BLOCKED", "account"))
            return
        if (
            account.equity < 0
            or account.available_balance < 0
            or account.margin_balance < 0
            or account.available_balance > account.equity
            or account.margin_balance > account.equity
        ):
            findings.append(
                _finding("ACCOUNT_FUNDS_DRIFT", "BLOCKED", "account")
            )
        if any(row.quantity != 0 for row in positions.values()) and account.margin_balance == 0:
            findings.append(
                _finding("POSITION_MARGIN_DRIFT", "BLOCKED", "account")
            )

    def _lock_state(self) -> OkxDemoReconciliationState:
        state = self.db.scalars(
            select(OkxDemoReconciliationState)
            .where(
                OkxDemoReconciliationState.execution_target_id
                == OKX_DEMO_TARGET_ID
            )
            .with_for_update()
        ).first()
        if state is None:
            state = OkxDemoReconciliationState(
                execution_target_id=OKX_DEMO_TARGET_ID,
                status="UNKNOWN",
                opening_frozen=True,
                block_reason="RECONCILIATION_REQUIRED",
            )
            self.db.add(state)
            self.db.flush()
        return state

    def _read_state(self) -> OkxDemoReconciliationState:
        state = self.db.scalars(
            select(OkxDemoReconciliationState).where(
                OkxDemoReconciliationState.execution_target_id
                == OKX_DEMO_TARGET_ID
            )
        ).first()
        if state is None:
            if self.db.get_bind().dialect.name == "postgresql":
                raise OkxDemoReconciliationBlocked(
                    "controlled reconciliation state is missing"
                )
            return self._lock_state()
        return state

    def _latest_complete_recovery_batch(
        self,
        *,
        now: datetime,
        stale_after: timedelta,
    ) -> Optional[OkxDemoRecoveryBatch]:
        return self.db.scalars(
            select(OkxDemoRecoveryBatch)
            .where(
                OkxDemoRecoveryBatch.execution_target_id
                == OKX_DEMO_TARGET_ID,
                OkxDemoRecoveryBatch.authenticated.is_(True),
                OkxDemoRecoveryBatch.pagination_complete.is_(True),
                OkxDemoRecoveryBatch.completed_at > now - stale_after,
                OkxDemoRecoveryBatch.completed_at <= now,
            )
            .order_by(
                OkxDemoRecoveryBatch.completed_at.desc(),
                OkxDemoRecoveryBatch.database_id.desc(),
            )
            .limit(1)
        ).first()

    def _lock_event_identity(self, event: Mapping[str, Any]) -> None:
        if self.db.get_bind().dialect.name != "postgresql":
            return
        identity = "|".join(
            (
                OKX_DEMO_TARGET_ID,
                str(event["source"]),
                str(event["entity_kind"]),
                str(event["entity_key"]),
                str(event["observed_at"].isoformat()),
            )
        )
        self.db.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:identity, 448))"
            ),
            {"identity": identity},
        )

    def _issue_recovery_grants(
        self,
        run: ReconciliationRun,
        *,
        status: str,
        authoritative_orders: Mapping[str, OkxDemoOrderSnapshot],
        authoritative_positions: Mapping[str, OkxDemoPositionSnapshot],
        now: datetime,
        expires_at: datetime,
        complete_snapshot: bool,
    ) -> list[OkxDemoRecoveryGrant]:
        if (
            status != "DRIFTED"
            or not complete_snapshot
            or expires_at <= now
        ):
            return []
        grants = []
        local_orders = self.db.scalars(
            select(ExchangeOrder).where(
                ExchangeOrder.execution_target_id == OKX_DEMO_TARGET_ID
            )
        ).all()
        for order in local_orders:
            authoritative = authoritative_orders.get(
                order.exchange_order_id or ""
            )
            if (
                authoritative is None
                or authoritative.status
                not in {"live", "partially_filled"}
            ):
                continue
            intent = self.db.get(TradeIntent, order.trade_intent_id)
            if intent is None or intent.instrument_id != authoritative.instrument_id:
                continue
            grants.append(
                OkxDemoRecoveryGrant(
                    execution_target_id=OKX_DEMO_TARGET_ID,
                    reconciliation_run_id=run.id,
                    exchange_order_row_id=order.id,
                    grant_digest=_digest(
                        {
                            "run": run.id,
                            "action": "CANCEL",
                            "order": order.id,
                            "exchange_order": authoritative.exchange_order_id,
                        }
                    ),
                    action="CANCEL",
                    instrument_id=intent.instrument_id,
                    position_side=intent.position_side,
                    max_quantity=Decimal("0"),
                    status="ACTIVE",
                    expires_at=expires_at,
                )
            )
        for identity, position in authoritative_positions.items():
            quantity = abs(Decimal(position.quantity))
            if quantity == 0:
                continue
            grants.append(
                OkxDemoRecoveryGrant(
                    execution_target_id=OKX_DEMO_TARGET_ID,
                    reconciliation_run_id=run.id,
                    exchange_order_row_id=None,
                    grant_digest=_digest(
                        {
                            "run": run.id,
                            "action": "REDUCE_ONLY",
                            "position": identity,
                            "max_quantity": format(quantity, "f"),
                        }
                    ),
                    action="REDUCE_ONLY",
                    instrument_id=position.instrument_id,
                    position_side=position.position_side,
                    max_quantity=quantity,
                    status="ACTIVE",
                    expires_at=expires_at,
                )
            )
        self.db.add_all(grants)
        self.db.flush()
        return grants

    def _mark_unknown(self, reason: str) -> None:
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(
                text(
                    "SELECT freeze_okx_demo_reconciliation_gate("
                    "'UNKNOWN', :reason, CURRENT_TIMESTAMP)"
                ),
                {"reason": _short_reason(reason)},
            ).scalar_one()
            return
        state = self._lock_state()
        state.status = "UNKNOWN"
        state.opening_frozen = True
        state.block_reason = _short_reason(reason)
        self.db.flush()


def _normalize_event(raw_event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw_event, Mapping):
        raise OkxDemoReconciliationBlocked("exchange event must be an object")
    if raw_event.get("schema_version") != SCHEMA_VERSION:
        raise OkxDemoReconciliationBlocked("exchange event schema version is unsupported")
    if raw_event.get("execution_target") != OKX_DEMO_TARGET_ID:
        raise OkxDemoReconciliationBlocked("exchange event target must be OKX_DEMO")
    source = str(raw_event.get("source", "")).upper()
    entity_kind = str(raw_event.get("entity_kind", "")).upper()
    if source not in {"REST", "WS"}:
        raise OkxDemoReconciliationBlocked("exchange event source is unsupported")
    if entity_kind not in {"ORDER", "FILL", "POSITION", "ACCOUNT"}:
        raise OkxDemoReconciliationBlocked("exchange event kind is unsupported")
    entity_key = str(raw_event.get("entity_key", "")).strip()
    if not entity_key or len(entity_key) > 160:
        raise OkxDemoReconciliationBlocked("exchange event entity key is invalid")
    payload = raw_event.get("payload")
    if not isinstance(payload, Mapping):
        raise OkxDemoReconciliationBlocked("exchange event payload must be an object")
    unexpected_fields = set(payload) - SAFE_PAYLOAD_FIELDS[entity_kind]
    if unexpected_fields:
        raise OkxDemoReconciliationBlocked(
            "authoritative snapshot contains unsafe or unsupported fields"
        )
    payload_copy = {
        field: payload[field]
        for field in sorted(SAFE_PAYLOAD_FIELDS[entity_kind])
        if field in payload
    }
    payload_digest = _digest(payload_copy)
    observed_at = _parse_datetime(raw_event.get("observed_at"), "observed_at")
    received_at = _parse_datetime(
        raw_event.get("received_at", observed_at),
        "received_at",
    )
    if received_at < observed_at - timedelta(minutes=5):
        raise OkxDemoReconciliationBlocked(
            "exchange event receive time precedes observation window"
        )
    source_sequence = raw_event.get("source_sequence")
    if source_sequence is not None:
        try:
            source_sequence = int(source_sequence)
        except (TypeError, ValueError) as exc:
            raise OkxDemoReconciliationBlocked(
                "exchange event source sequence is invalid"
            ) from exc
        if source_sequence < 0:
            raise OkxDemoReconciliationBlocked(
                "exchange event source sequence is invalid"
            )
    if source == "WS" and source_sequence is None:
        raise OkxDemoReconciliationBlocked(
            "websocket exchange event requires a source sequence"
        )
    try:
        stream_generation = int(raw_event.get("stream_generation", 1))
    except (TypeError, ValueError) as exc:
        raise OkxDemoReconciliationBlocked(
            "exchange event stream generation is invalid"
        ) from exc
    if stream_generation < 1:
        raise OkxDemoReconciliationBlocked(
            "exchange event stream generation is invalid"
        )
    identity = {
        "schema_version": SCHEMA_VERSION,
        "execution_target": OKX_DEMO_TARGET_ID,
        "source": source,
        "entity_kind": entity_kind,
        "entity_key": entity_key,
        "source_sequence": source_sequence,
        "stream_generation": stream_generation,
        "observed_at": observed_at.isoformat(),
        "payload_digest": payload_digest,
    }
    event_key = str(raw_event.get("event_key") or _digest(identity))
    if len(event_key) != 64 or any(character not in "0123456789abcdef" for character in event_key):
        raise OkxDemoReconciliationBlocked("exchange event key must be a sha256 digest")
    recovery_batch_id = raw_event.get("recovery_batch_id")
    return {
        "execution_target_id": OKX_DEMO_TARGET_ID,
        "event_key": event_key,
        "source": source,
        "entity_kind": entity_kind,
        "entity_key": entity_key,
        "source_sequence": source_sequence,
        "stream_generation": stream_generation,
        "payload": payload_copy,
        "payload_digest": payload_digest,
        "observed_at": observed_at,
        "received_at": received_at,
        "recovery_batch_id": (
            str(recovery_batch_id) if recovery_batch_id is not None else None
        ),
        "recovery_batch_database_id": raw_event.get(
            "recovery_batch_database_id"
        ),
    }


def _latest_by(rows: Iterable[Any], identity) -> dict[str, Any]:
    result = {}
    for row in rows:
        key = identity(row)
        if key not in result:
            result[key] = row
    return result


def _finding(code: str, severity: str, identity: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "identity": identity}


def _write_artifact(
    artifact_root: Path,
    allowed_root: Path,
    run_id: int,
    evidence: Mapping[str, Any],
) -> tuple[str, str]:
    root = _managed_artifact_root(artifact_root, allowed_root)
    supplied_root = Path(artifact_root).expanduser()
    supplied_allowed_root = Path(allowed_root).expanduser()
    if not supplied_root.is_absolute() or not supplied_allowed_root.is_absolute():
        raise OkxDemoReconciliationBlocked(
            "artifact root must be an absolute managed evidence path"
        )
    for candidate in (
        supplied_root,
        supplied_allowed_root,
    ) + tuple(supplied_root.parents) + tuple(supplied_allowed_root.parents):
        if candidate.exists() and candidate.is_symlink():
            raise OkxDemoReconciliationBlocked(
                "artifact root cannot traverse a symbolic link"
            )
    payload = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    target = root / "okx-demo-reconciliation-{}.json".format(run_id)
    temporary = root / ".okx-demo-reconciliation-{}.tmp".format(run_id)
    if target.exists() or target.is_symlink() or temporary.exists():
        raise OkxDemoReconciliationBlocked(
            "artifact destination is not a clean managed file"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(temporary), flags, 0o600)
    try:
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        os.replace(str(temporary), str(target))
        directory_descriptor = os.open(str(root), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return str(target), digest


def _cleanup_orphan_artifacts(
    db: Session,
    artifact_root: Path,
    allowed_root: Path,
) -> None:
    root = _managed_artifact_root(artifact_root, allowed_root)
    for temporary in root.glob(".okx-demo-reconciliation-*.tmp"):
        if temporary.is_symlink():
            raise OkxDemoReconciliationBlocked(
                "managed artifact temporary file is a symbolic link"
            )
        temporary.unlink()
    for candidate in root.glob("okx-demo-reconciliation-*.json"):
        if candidate.is_symlink():
            raise OkxDemoReconciliationBlocked(
                "managed artifact file is a symbolic link"
            )
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            run_ids = payload["database_ids"]["reconciliation_run"]
            run_id = int(run_ids[0]) if len(run_ids) == 1 else -1
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            payload.get("schema_version") != SCHEMA_VERSION
            or payload.get("execution_target") != OKX_DEMO_TARGET_ID
            or candidate.name
            != "okx-demo-reconciliation-{}.json".format(run_id)
        ):
            continue
        run = db.get(ReconciliationRun, run_id)
        if run is None or run.artifact_status != "READY":
            candidate.unlink()


def _managed_artifact_root(artifact_root: Path, allowed_root: Path) -> Path:
    supplied_root = Path(artifact_root).expanduser()
    supplied_allowed_root = Path(allowed_root).expanduser()
    if not supplied_root.is_absolute() or not supplied_allowed_root.is_absolute():
        raise OkxDemoReconciliationBlocked(
            "artifact root must be an absolute managed evidence path"
        )
    for candidate in (
        supplied_root,
        supplied_allowed_root,
    ) + tuple(supplied_root.parents) + tuple(supplied_allowed_root.parents):
        if candidate.exists() and candidate.is_symlink():
            raise OkxDemoReconciliationBlocked(
                "artifact root cannot traverse a symbolic link"
            )
    root = supplied_root.resolve()
    allowed = supplied_allowed_root.resolve()
    try:
        root.relative_to(allowed)
    except ValueError as exc:
        raise OkxDemoReconciliationBlocked(
            "artifact root is outside the managed evidence boundary"
        ) from exc
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise OkxDemoReconciliationBlocked("timestamp must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str):
        try:
            return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError as exc:
            raise OkxDemoReconciliationBlocked(
                "{} must be an ISO timestamp".format(field)
            ) from exc
    raise OkxDemoReconciliationBlocked("{} must be an ISO timestamp".format(field))


def _text(payload: Mapping[str, Any], field: str) -> str:
    value = str(payload.get(field, "")).strip()
    if not value:
        raise OkxDemoReconciliationBlocked(
            "authoritative snapshot field {} is required".format(field)
        )
    return value


def _optional_text(value: Any) -> Optional[str]:
    rendered = str(value or "").strip()
    return rendered or None


def _decimal(payload: Mapping[str, Any], field: str) -> Decimal:
    try:
        return Decimal(str(payload[field]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise OkxDemoReconciliationBlocked(
            "authoritative snapshot field {} must be decimal".format(field)
        ) from exc


def _optional_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OkxDemoReconciliationBlocked(
            "optional authoritative value must be decimal"
        ) from exc


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "1"}:
        return True
    if str(value).lower() in {"false", "0"}:
        return False
    raise OkxDemoReconciliationBlocked("authoritative boolean is invalid")


def _short_reason(value: str) -> str:
    rendered = " ".join(str(value).split())
    return rendered[:160]
