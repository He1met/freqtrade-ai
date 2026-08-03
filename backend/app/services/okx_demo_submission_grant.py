from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
import hashlib
import json
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.execution_lineage import (
    ApprovedExecution,
    OkxDemoAttestedSession,
    OkxDemoTrustedSnapshot,
    ReconciliationRun,
    RiskDecision,
    TradeIntent,
)
from app.models.okx_demo_reconciliation import OkxDemoReconciliationState
from app.models.order_writer import OkxDemoSubmissionGrant, OkxOrderWriteAttempt
from app.services.risk_chain import (
    RiskChainBlocked,
    RiskChainService,
    canonical_digest,
    _trusted_snapshot_id,
)


GRANT_TTL_SECONDS = 10
MAX_RECONCILIATION_AGE_SECONDS = 30
CANARY_PROVENANCE = "CONTROLLED_CANARY_NON_PRODUCTION"
CANARY_INSTRUMENTS = frozenset({"BTC-USDT-SWAP"})
CANARY_NOTIONAL_CAP = Decimal("20")
ONE_SHOT_COORDINATION_LOCK_KEY = 0x4654414F4E455348
UNRESOLVED_WRITER_STATES = (
    "PREPARED",
    "DISPATCHED",
    "ACKNOWLEDGED",
    "RECOVERY_REQUIRED",
    "RESIDUAL_CLOSE_REQUIRED",
)


def canary_lineage_read_query(db: Session, statement, *, for_update: bool):
    """Apply a compatibility row lock only on dialects that can use it.

    The canonical PostgreSQL runtime role deliberately has ``SELECT`` (and,
    where needed, narrow column ``UPDATE``) on the canary lineage tables, but
    not table-level ``UPDATE``.  PostgreSQL therefore rejects every
    ``SELECT .. FOR UPDATE``/``FOR SHARE`` variant for those tables.  Every
    production caller here already holds either the transaction-scoped
    ``ONE_SHOT_COORDINATION_LOCK_KEY`` or the writer advisory lock, so the
    advisory lock is the least-privilege serialization boundary on PostgreSQL.
    Keep the row-lock behavior for SQLite and other test/non-production
    dialects so their existing concurrency assertions remain meaningful.
    """

    if not for_update:
        return statement
    bind = getattr(db, "bind", None)
    if bind is None:
        try:
            bind = db.get_bind()
        except Exception:
            bind = None
    if bind is not None and getattr(bind.dialect, "name", None) == "postgresql":
        return statement
    return statement.with_for_update()


class OkxDemoSubmissionGrantBlocked(Exception):
    pass


def build_atomic_canary_payloads(
    db: Session,
    *,
    handoff_id: str,
    runtime_instance_id: str,
    approval_id: int,
    holder_token_digest: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build data for the owner-only atomic prepare coordinator without committing."""

    now = OkxDemoSubmissionGrantService._aware(now)
    row = db.execute(
        select(ApprovedExecution, TradeIntent, RiskDecision)
        .join(TradeIntent, TradeIntent.id == ApprovedExecution.trade_intent_id)
        .join(RiskDecision, RiskDecision.id == ApprovedExecution.risk_decision_id)
        .where(ApprovedExecution.id == approval_id)
    ).first()
    state = db.scalars(
        select(OkxDemoReconciliationState).where(
            OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
        )
    ).first()
    if row is None or state is None or state.last_reconciliation_run_id is None:
        raise OkxDemoSubmissionGrantBlocked("atomic canary lineage is unavailable")
    approved, intent, decision = row
    run = db.get(ReconciliationRun, state.last_reconciliation_run_id)
    if (
        approved.status != "ACTIVE"
        or intent.status != "APPROVED"
        or decision.decision != "APPROVED"
        or intent.order_type != "limit"
        or intent.limit_price is None
        or intent.reduce_only
        or run is None
    ):
        raise OkxDemoSubmissionGrantBlocked("atomic canary lineage is unsafe")
    canary_quantity, canary_notional = _require_minimum_canary_risk(
        db, approved=approved, intent=intent, now=now
    )
    expires_at = min(
        OkxDemoSubmissionGrantService._aware(approved.expires_at),
        OkxDemoSubmissionGrantService._aware(intent.expires_at),
        now + timedelta(seconds=GRANT_TTL_SECONDS),
    )
    grant_id = uuid4().hex
    grant_request_digest = submission_grant_request_digest(
        approval_id=approved.id,
        reconciliation_run_id=run.id,
        canonical_hash=approved.canonical_hash,
        policy_digest=approved.policy_digest,
        approved_payload_hash=approved.approved_payload_hash,
        client_order_id=approved.client_order_id,
        instrument_id=intent.instrument_id,
        canary_quantity=canary_quantity,
        canary_notional=canary_notional,
    )
    body: dict[str, Any] = {
        "instId": intent.instrument_id,
        "tdMode": "isolated",
        "side": intent.side,
        "posSide": intent.position_side,
        "ordType": intent.order_type,
        "sz": _decimal_text(intent.quantity),
        "clOrdId": intent.client_order_id,
        "px": _decimal_text(intent.limit_price),
    }
    if intent.take_profit is not None or intent.stop_loss is not None:
        attached: dict[str, str] = {}
        if intent.take_profit is not None:
            attached.update(
                attachAlgoClOrdId=intent.client_order_id[:30] + "TP",
                tpTriggerPx=_decimal_text(intent.take_profit),
                tpOrdPx="-1",
                tpTriggerPxType="mark",
            )
        if intent.stop_loss is not None:
            attached.update(
                attachAlgoClOrdId=attached.get(
                    "attachAlgoClOrdId", intent.client_order_id[:30] + "EX"
                ),
                slTriggerPx=_decimal_text(intent.stop_loss),
                slOrdPx="-1",
                slTriggerPxType="mark",
            )
        body["attachAlgoOrds"] = [attached]
    request_digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    grant_payload = {
        "grant_id": grant_id,
        "handoff_id": handoff_id,
        "runtime_instance_id": runtime_instance_id,
        "approval_id": approved.id,
        "reconciliation_run_id": run.id,
        "canonical_hash": approved.canonical_hash,
        "policy_digest": approved.policy_digest,
        "approved_payload_hash": approved.approved_payload_hash,
        "client_order_id": approved.client_order_id,
        "instrument_id": intent.instrument_id,
        "canary_quantity": _decimal_text(canary_quantity),
        "canary_notional": _decimal_text(canary_notional),
        "request_digest": grant_request_digest,
        "expires_at": expires_at.isoformat(),
    }
    prepare_payload = {
        "grant_id": grant_id,
        "handoff_id": handoff_id,
        "runtime_instance_id": runtime_instance_id,
        "holder_token_digest": holder_token_digest,
        "request_digest": request_digest,
        "request_body": body,
    }
    return grant_payload, prepare_payload


class OkxDemoSubmissionGrantService:
    """Arm one exact DB-backed capability; the runtime writer consumes it."""

    def __init__(self, db: Session, *, now_provider=lambda: datetime.now(timezone.utc)):
        self.db = db
        self._now_provider = now_provider

    def arm(
        self,
        *,
        approval_id: int,
        canonical_hash: str,
        policy_digest: str,
        approved_payload_hash: str,
        client_order_id: str,
        handoff_id: Optional[str] = None,
        runtime_instance_id: Optional[str] = None,
    ) -> OkxDemoSubmissionGrant:
        manifest = get_settings().execution_target_manifest
        target = manifest.active_target
        if (
            manifest.active_target_id != "OKX_DEMO"
            or target.simulated_trading is not True
            or target.allow_real_funds is not False
            or target.order_submission_enabled is not False
        ):
            raise OkxDemoSubmissionGrantBlocked(
                "one-shot grant requires OKX_DEMO with global submission disabled"
            )
        now = self._aware(self._now_provider())
        try:
            if self.db.get_bind().dialect.name == "postgresql" and (
                not handoff_id or not runtime_instance_id
            ):
                raise OkxDemoSubmissionGrantBlocked(
                    "all controlled canary grants require finalized operator consent"
                )
            if not try_one_shot_transaction_lock(self.db):
                raise OkxDemoSubmissionGrantBlocked(
                    "canonical runtime is reconciling; retry the one-shot grant"
                )
            self._expire_stale(now)
            if self.db.scalars(
                select(OkxOrderWriteAttempt.id).where(
                    OkxOrderWriteAttempt.execution_target_id == "OKX_DEMO",
                    OkxOrderWriteAttempt.state.in_(UNRESOLVED_WRITER_STATES),
                ).limit(1)
            ).first() is not None:
                raise OkxDemoSubmissionGrantBlocked(
                    "unresolved writer attempt blocks one-shot grant"
                )
            row_statement = (
                select(ApprovedExecution, TradeIntent, RiskDecision)
                .join(TradeIntent, TradeIntent.id == ApprovedExecution.trade_intent_id)
                .join(RiskDecision, RiskDecision.id == ApprovedExecution.risk_decision_id)
                .where(ApprovedExecution.id == approval_id)
            )
            row = self.db.execute(
                canary_lineage_read_query(self.db, row_statement, for_update=True)
            ).first()
            if row is None:
                raise OkxDemoSubmissionGrantBlocked("approved execution is unavailable")
            approved, intent, decision = row
            state = self.db.scalars(
                canary_lineage_read_query(
                    self.db,
                    select(OkxDemoReconciliationState).where(
                        OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
                    ),
                    for_update=True,
                )
            ).first()
            if state is None or state.last_reconciliation_run_id is None:
                raise OkxDemoSubmissionGrantBlocked(
                    "fresh empty reconciliation is required"
                )
            run = require_canary_reconciliation(
                self.db,
                reconciliation_run_id=state.last_reconciliation_run_id,
                now=now,
                for_update=True,
            )
            expires_at = min(
                self._aware(approved.expires_at),
                self._aware(intent.expires_at),
                now + timedelta(seconds=GRANT_TTL_SECONDS),
            )
            if (
                approved.execution_target_id != "OKX_DEMO"
                or intent.execution_target_id != "OKX_DEMO"
                or decision.execution_target_id != "OKX_DEMO"
                or approved.status != "ACTIVE"
                or intent.status != "APPROVED"
                or decision.decision != "APPROVED"
                or expires_at <= now
                or approved.claim_required is not True
                or approved.order_submission_authorized is not False
            ):
                raise OkxDemoSubmissionGrantBlocked(
                    "approved execution is not active OKX_DEMO lineage"
                )
            expected = (
                approved.canonical_hash,
                approved.policy_digest,
                approved.approved_payload_hash,
                approved.client_order_id,
            )
            supplied = (
                canonical_hash,
                policy_digest,
                approved_payload_hash,
                client_order_id,
            )
            if supplied != expected:
                raise OkxDemoSubmissionGrantBlocked(
                    "one-shot grant payload does not match persisted approval"
                )
            try:
                RiskChainService(self.db).require_completed_full_chain_binding(
                    approved=approved,
                    intent=intent,
                    decision=decision,
                )
            except RiskChainBlocked:
                raise OkxDemoSubmissionGrantBlocked(
                    "approved execution full-chain binding is incomplete"
                ) from None
            canary_quantity, canary_notional = _require_minimum_canary_risk(
                self.db,
                approved=approved,
                intent=intent,
                now=now,
            )
            decision_notional = (decision.evidence_snapshot or {}).get("notional")
            try:
                decision_notional_matches = (
                    decision_notional is not None
                    and _decimal_text(Decimal(str(decision_notional)))
                    == _decimal_text(canary_notional)
                )
            except InvalidOperation:
                decision_notional_matches = False
            if not decision_notional_matches:
                raise OkxDemoSubmissionGrantBlocked(
                    "risk decision evidence does not match canary notional"
                )
            grant = OkxDemoSubmissionGrant(
                grant_id=uuid4().hex,
                execution_target_id="OKX_DEMO",
                approval_id=approved.id,
                reconciliation_run_id=run.id,
                canonical_hash=approved.canonical_hash,
                policy_digest=approved.policy_digest,
                approved_payload_hash=approved.approved_payload_hash,
                client_order_id=approved.client_order_id,
                instrument_id=intent.instrument_id,
                canary_quantity=canary_quantity,
                canary_notional=canary_notional,
                request_digest=submission_grant_request_digest(
                    approval_id=approved.id,
                    reconciliation_run_id=run.id,
                    canonical_hash=approved.canonical_hash,
                    policy_digest=approved.policy_digest,
                    approved_payload_hash=approved.approved_payload_hash,
                    client_order_id=approved.client_order_id,
                    instrument_id=intent.instrument_id,
                    canary_quantity=canary_quantity,
                    canary_notional=canary_notional,
                ),
                provenance=CANARY_PROVENANCE,
                status="ACTIVE",
                issued_at=now,
                expires_at=expires_at,
            )
            if self.db.get_bind().dialect.name == "postgresql":
                payload = {
                    "grant_id": grant.grant_id,
                    "approval_id": grant.approval_id,
                    "reconciliation_run_id": grant.reconciliation_run_id,
                    "canonical_hash": grant.canonical_hash,
                    "policy_digest": grant.policy_digest,
                    "approved_payload_hash": grant.approved_payload_hash,
                    "client_order_id": grant.client_order_id,
                    "instrument_id": grant.instrument_id,
                    "canary_quantity": _decimal_text(grant.canary_quantity),
                    "canary_notional": _decimal_text(grant.canary_notional),
                    "request_digest": grant.request_digest,
                    "expires_at": expires_at.isoformat(),
                }
                payload.update(
                    {
                        "handoff_id": handoff_id,
                        "runtime_instance_id": runtime_instance_id,
                    }
                )
                grant_id = self.db.execute(
                    text(
                        "SELECT issue_okx_demo_submission_grant("
                        "CAST(:payload AS jsonb))"
                    ),
                    {"payload": json.dumps(payload, sort_keys=True)},
                ).scalar_one()
                grant = self.db.get(OkxDemoSubmissionGrant, grant_id)
                if grant is None:
                    raise OkxDemoSubmissionGrantBlocked(
                        "one-shot grant owner function did not persist"
                    )
            else:
                self.db.add(grant)
            self.db.commit()
            return grant
        except OkxDemoSubmissionGrantBlocked:
            self.db.rollback()
            raise
        except IntegrityError:
            self.db.rollback()
            raise OkxDemoSubmissionGrantBlocked(
                "another one-shot grant already exists"
            ) from None

    def _expire_stale(self, now: datetime) -> None:
        # ``arm`` already owns ONE_SHOT_COORDINATION_LOCK_KEY for this
        # transaction.  Keep expiry and its guarded column update atomic, but
        # do not request a PostgreSQL row lock that the runtime role cannot
        # acquire under its least-privilege ACL.
        rows = list(
            self.db.scalars(
                canary_lineage_read_query(
                    self.db,
                    select(OkxDemoSubmissionGrant).where(
                        OkxDemoSubmissionGrant.execution_target_id == "OKX_DEMO",
                        OkxDemoSubmissionGrant.status == "ACTIVE",
                    ),
                    for_update=True,
                )
            )
        )
        for row in rows:
            if self._aware(row.expires_at) <= now:
                row.status = "EXPIRED"
                row.consumed_at = now

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def arm_finalized_canary_consent(
    db: Session, *, runtime_instance_id: str
) -> Optional[OkxDemoSubmissionGrant]:
    """Issue v28's grant after the independently committed finalization."""

    if db.get_bind().dialect.name != "postgresql":
        return None
    pending = db.execute(
        text("SELECT finalized_okx_demo_canary_consent(:runtime)"),
        {"runtime": runtime_instance_id},
    ).scalar_one()
    if pending is None or pending.get("status") != "FINALIZED":
        # Owner reconciliation may have terminalized an expired A/B or B/TTL
        # gap.  Commit that fail-closed fact instead of rolling it back.
        db.commit()
        return None
    return OkxDemoSubmissionGrantService(db).arm(
        approval_id=int(pending["approval_id"]),
        canonical_hash=str(pending["canonical_hash"]),
        policy_digest=str(pending["policy_digest"]),
        approved_payload_hash=str(pending["approved_payload_hash"]),
        client_order_id=str(pending["client_order_id"]),
        handoff_id=str(pending["handoff_id"]),
        runtime_instance_id=runtime_instance_id,
    )


def revoke_restarted_canary_grant(
    db: Session, *, grant_id: str, runtime_instance_id: str
) -> bool:
    """Fail an ACTIVE consent grant observed by any later runtime identity."""

    if db.get_bind().dialect.name != "postgresql":
        return False
    return bool(
        db.execute(
            text(
                "SELECT revoke_restarted_okx_demo_canary_grant("
                ":grant,:runtime)"
            ),
            {"grant": grant_id, "runtime": runtime_instance_id},
        ).scalar_one()
    )


def fail_canary_grant_before_prepare(db: Session, *, grant_id: str) -> bool:
    """Terminalize a committed grant only while no placement journal exists."""

    if db.get_bind().dialect.name != "postgresql":
        return False
    return bool(
        db.execute(
            text("SELECT fail_okx_demo_canary_grant_before_prepare(:grant)"),
            {"grant": grant_id},
        ).scalar_one()
    )


def settle_canary_consent_handoff(db: Session, *, grant_id: str) -> Optional[str]:
    """Close the owner-managed handoff from exact persisted grant state."""

    if db.get_bind().dialect.name != "postgresql":
        return None
    value = db.execute(
        text("SELECT settle_okx_demo_canary_handoff(:grant_id)"),
        {"grant_id": grant_id},
    ).scalar_one_or_none()
    return None if value is None else str(value)


def require_canary_reconciliation(
    db: Session,
    *,
    reconciliation_run_id: int,
    now: datetime,
    for_update: bool,
) -> ReconciliationRun:
    # ``for_update`` is retained at call sites as a statement of intent: on
    # PostgreSQL the caller's coordination advisory lock provides the conflict
    # boundary, while this read-only assertion must stay executable by the
    # SELECT-only runtime role.
    state_query = select(OkxDemoReconciliationState).where(
        OkxDemoReconciliationState.execution_target_id == "OKX_DEMO"
    )
    run_query = select(ReconciliationRun).where(
        ReconciliationRun.id == reconciliation_run_id,
        ReconciliationRun.execution_target_id == "OKX_DEMO",
    )
    if for_update:
        state_query = canary_lineage_read_query(
            db,
            state_query,
            for_update=True,
        )
        run_query = canary_lineage_read_query(
            db,
            run_query,
            for_update=True,
        )
    state = db.scalars(state_query).first()
    run = db.scalars(run_query).first()
    active_now = OkxDemoSubmissionGrantService._aware(now)
    if (
        state is None
        or run is None
        or state.last_reconciliation_run_id != reconciliation_run_id
        or state.status not in {"RECONCILED", "RECOVERED"}
        or state.opening_frozen
        or run.status not in {"RECONCILED", "RECOVERED"}
        or run.artifact_status != "READY"
        or run.source_type != "api_aggregate"
        or run.core_data is not True
        or run.completed_at is None
        or run.authoritative_observed_at is None
    ):
        raise OkxDemoSubmissionGrantBlocked(
            "one-shot grant reconciliation binding is no longer safe"
        )
    maximum_age = timedelta(seconds=MAX_RECONCILIATION_AGE_SECONDS)
    completed_age = active_now - OkxDemoSubmissionGrantService._aware(run.completed_at)
    observed_age = active_now - OkxDemoSubmissionGrantService._aware(
        run.authoritative_observed_at
    )
    ids = run.database_ids if isinstance(run.database_ids, dict) else {}
    if (
        not (-timedelta(seconds=5) <= completed_age <= maximum_age)
        or not (-timedelta(seconds=5) <= observed_age <= maximum_age)
        or ids.get("reconciliation_run") != [run.id]
        or ids.get("order_snapshots") != []
        or ids.get("position_snapshots") != []
    ):
        raise OkxDemoSubmissionGrantBlocked(
            "one-shot grant requires fresh empty order and position snapshots"
        )
    return run


def submission_grant_request_digest(
    *,
    approval_id: int,
    reconciliation_run_id: int,
    canonical_hash: str,
    policy_digest: str,
    approved_payload_hash: str,
    client_order_id: str,
    instrument_id: str,
    canary_quantity: Decimal,
    canary_notional: Decimal,
) -> str:
    material = {
        "approval_id": approval_id,
        "reconciliation_run_id": reconciliation_run_id,
        "canonical_hash": canonical_hash,
        "policy_digest": policy_digest,
        "approved_payload_hash": approved_payload_hash,
        "client_order_id": client_order_id,
        "instrument_id": instrument_id,
        "canary_quantity": _decimal_text(canary_quantity),
        "canary_notional": _decimal_text(canary_notional),
        "provenance": CANARY_PROVENANCE,
    }
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def try_one_shot_transaction_lock(db: Session) -> bool:
    """Serialize API arming with the canonical runtime observation window."""

    bind = getattr(db, "bind", None)
    if bind is None or bind.dialect.name != "postgresql":
        return True
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": ONE_SHOT_COORDINATION_LOCK_KEY},
        ).scalar_one()
    )


def acquire_one_shot_runtime_lock(db: Session) -> bool:
    """Acquire the session lock held across grant choice or observation."""

    bind = getattr(db, "bind", None)
    if bind is None or bind.dialect.name != "postgresql":
        return True
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": ONE_SHOT_COORDINATION_LOCK_KEY},
        ).scalar_one()
    )


def release_one_shot_runtime_lock(db: Session) -> bool:
    bind = getattr(db, "bind", None)
    if bind is not None and bind.dialect.name == "postgresql":
        unlocked = db.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": ONE_SHOT_COORDINATION_LOCK_KEY},
        ).scalar_one()
        if not unlocked:
            raise OkxDemoSubmissionGrantBlocked(
                "canonical runtime lost one-shot coordination lock"
            )
        return True
    return False


def _decimal_text(value: Decimal) -> str:
    # SQLite's NUMERIC adapter round-trips through binary float in tests.  The
    # controlled canary contract only permits 12-place quantities/notionals,
    # so canonicalize that representation before hashing on every dialect.
    canonical = Decimal(value).quantize(Decimal("0.000000000001"))
    rendered = format(canonical, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _require_minimum_canary_risk(
    db: Session,
    *,
    approved: ApprovedExecution,
    intent: TradeIntent,
    now: datetime,
) -> tuple[Decimal, Decimal]:
    instrument = db.scalars(
        select(OkxDemoTrustedSnapshot).where(
            OkxDemoTrustedSnapshot.snapshot_id == approved.instrument_snapshot_id,
            OkxDemoTrustedSnapshot.kind == "instrument",
            OkxDemoTrustedSnapshot.execution_target_id == "OKX_DEMO",
        )
    ).first()
    market = db.scalars(
        select(OkxDemoTrustedSnapshot).where(
            OkxDemoTrustedSnapshot.snapshot_id == approved.market_snapshot_id,
            OkxDemoTrustedSnapshot.kind == "market",
            OkxDemoTrustedSnapshot.execution_target_id == "OKX_DEMO",
        )
    ).first()
    account = db.scalars(
        select(OkxDemoTrustedSnapshot).where(
            OkxDemoTrustedSnapshot.snapshot_id == approved.account_snapshot_id,
            OkxDemoTrustedSnapshot.kind == "account",
            OkxDemoTrustedSnapshot.execution_target_id == "OKX_DEMO",
        )
    ).first()
    try:
        instrument_content = instrument.content_json
        market_content = market.content_json
        account_content = account.content_json
        instrument_id = str(
            instrument_content.get("instId")
            or instrument_content["instrument_id"]
        )
        min_size = Decimal(
            str(instrument_content.get("minSz") or instrument_content["min_size"])
        )
        lot_size = Decimal(
            str(instrument_content.get("lotSz") or instrument_content["lot_size"])
        )
        contract_value = Decimal(
            str(
                instrument_content.get("ctVal")
                or instrument_content["contract_value"]
            )
        )
        reference_price = Decimal(str(market_content["reference_price"]))
        best_ask = Decimal(str(market_content["bbo"]["ask_price"]))
        mark_price = Decimal(str(market_content["mark"]["price"]))
        market_as_of = OkxDemoSubmissionGrantService._aware(
            datetime.fromisoformat(str(market_content["as_of"]).replace("Z", "+00:00"))
        )
        content_expires = {
            row.kind: OkxDemoSubmissionGrantService._aware(
                datetime.fromisoformat(
                    str(row.content_json["expires_at"]).replace("Z", "+00:00")
                )
            )
            for row in (instrument, market, account)
        }
        if not all(
            value.is_finite()
            for value in (
                min_size,
                lot_size,
                contract_value,
                reference_price,
                best_ask,
                mark_price,
            )
        ):
            raise OkxDemoSubmissionGrantBlocked(
                "trusted canary snapshot contains non-finite values"
            )
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, InvalidOperation):
        raise OkxDemoSubmissionGrantBlocked(
            "trusted canary instrument or market snapshot is malformed"
        ) from None
    if (
        instrument_id not in CANARY_INSTRUMENTS
        or instrument_id
        not in set(
            get_settings().demo_automation_policy.demo_risk_policy.allowed_instruments
        )
        or intent.instrument_id != instrument_id
        or market_content.get("instrument_id") != instrument_id
        or OkxDemoSubmissionGrantService._aware(instrument.expires_at) <= now
        or OkxDemoSubmissionGrantService._aware(market.expires_at) <= now
        or OkxDemoSubmissionGrantService._aware(account.expires_at) <= now
        or min_size <= 0
        or lot_size <= 0
        or instrument.source_type != "api_aggregate"
        or market.source_type != "api_aggregate"
        or account.source_type != "api_aggregate"
        or instrument.core_data is not True
        or market.core_data is not True
        or account.core_data is not True
        or instrument.digest != canonical_digest(instrument_content)
        or market.digest != canonical_digest(market_content)
        or account.digest != canonical_digest(account_content)
        or instrument_content.get("execution_target") != "OKX_DEMO"
        or market_content.get("execution_target") != "OKX_DEMO"
        or account_content.get("execution_target") != "OKX_DEMO"
        or instrument_content.get("source") != "okx_demo_rest"
        or market_content.get("source") != "okx_demo_rest"
        or account_content.get("source") != "okx_demo_rest"
        or instrument_content.get("stale") is not False
        or market_content.get("stale") is not False
        or account_content.get("stale") is not False
        or _trusted_snapshot_id(instrument) != instrument.snapshot_id
        or _trusted_snapshot_id(market) != market.snapshot_id
        or _trusted_snapshot_id(account) != account.snapshot_id
        or any(
            content_expires[row.kind]
            != OkxDemoSubmissionGrantService._aware(row.expires_at)
            for row in (instrument, market, account)
        )
        or any(
            OkxDemoSubmissionGrantService._aware(row.observed_at) > now
            for row in (instrument, market, account)
        )
        or now - market_as_of > timedelta(seconds=MAX_RECONCILIATION_AGE_SECONDS)
        or market_as_of > now + timedelta(seconds=5)
        or reference_price <= 0
        or best_ask <= 0
        or mark_price <= 0
        or mark_price != reference_price
        or instrument_content.get("state") != "live"
        or instrument_content.get("contract_shape") != "linear"
        or instrument_content.get("ctValCcy")
        not in {instrument_id.split("-")[0], None}
    ):
        raise OkxDemoSubmissionGrantBlocked(
            "approval is not the allowlisted minimum-size controlled canary"
        )
    quantity = (min_size / lot_size).to_integral_value(rounding=ROUND_UP) * lot_size
    conservative_price = max(
        reference_price,
        best_ask,
        Decimal(intent.limit_price or 0),
    )
    notional = quantity * contract_value * conservative_price
    evidence = (intent.request_snapshot or {}).get("snapshot_evidence")
    session = db.get(OkxDemoAttestedSession, instrument.attested_session_id)
    if (
        Decimal(intent.quantity) != quantity
        or _decimal_text(Decimal(approved.reserved_notional))
        != _decimal_text(notional)
        or not isinstance(evidence, dict)
        or session is None
        or session.revoked_at is not None
        or OkxDemoSubmissionGrantService._aware(session.expires_at) <= now
        or instrument.attested_session_id != market.attested_session_id
        or instrument.attested_session_id != account.attested_session_id
        or instrument.attestation_fingerprint_sha256
        != session.pinned_fingerprint_sha256
        or market.attestation_fingerprint_sha256
        != session.pinned_fingerprint_sha256
        or account.attestation_fingerprint_sha256
        != session.pinned_fingerprint_sha256
        or any(
            OkxDemoSubmissionGrantService._aware(row.attested_session_expires_at)
            != OkxDemoSubmissionGrantService._aware(session.expires_at)
            for row in (instrument, market, account)
        )
    ):
        raise OkxDemoSubmissionGrantBlocked(
            "controlled canary trusted bundle or reserved notional changed"
        )
    for kind, row in (
        ("instrument", instrument),
        ("market", market),
        ("account", account),
    ):
        item = evidence.get(kind)
        if (
            not isinstance(item, dict)
            or item.get("snapshot_id") != row.snapshot_id
            or item.get("database_id") != row.database_id
            or item.get("digest") != row.digest
        ):
            raise OkxDemoSubmissionGrantBlocked(
                "controlled canary approval snapshot references changed"
            )
    if notional <= 0 or notional > CANARY_NOTIONAL_CAP:
        raise OkxDemoSubmissionGrantBlocked(
            "controlled canary notional exceeds its fixed cap"
        )
    return quantity, notional
