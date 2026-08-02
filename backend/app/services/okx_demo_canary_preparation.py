"""Prepare one strategy-independent, non-production OKX Demo canary.

The normal risk chain is intentionally coupled to a validated strategy
candidate.  A controlled execution-chain canary must not manufacture such a
candidate merely to exercise the writer.  This service therefore records a
small, explicit canary lineage in the existing durable lineage tables.  The
lineage is marked as non-production at every boundary and is consumed only by
the already-gated #595 one-shot grant/runtime path.

This module never talks to OKX and never enables global order submission.  The
canonical runtime remains the only component allowed to consume the resulting
grant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
import hashlib
from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.okx_demo.errors import OkxReadAdapterError
from app.core.config import get_settings
from app.models.execution_lineage import (
    ApprovedExecution,
    LOCAL_DRY_RUN_SCOPE_ID,
    OKX_DEMO_TARGET_ID,
    OkxDemoAttestedSession,
    OkxDemoTrustedSnapshot,
    ResearchJobAttempt,
    RiskDecision,
    TradeIntent,
)
from app.models.full_chain import FullChainRun, FullChainStageRun
from app.models.okx_demo_reconciliation import OkxDemoReconciliationState
from app.models.order_writer import OkxDemoSubmissionGrant, OkxOrderWriteAttempt
from app.models.research_job import ResearchJob
from app.services.okx_demo_submission_grant import (
    CANARY_INSTRUMENTS,
    CANARY_NOTIONAL_CAP,
    MAX_RECONCILIATION_AGE_SECONDS,
    OkxDemoSubmissionGrantBlocked,
    require_canary_reconciliation,
    try_one_shot_transaction_lock,
)
from app.services.risk_chain import _trusted_snapshot_id, canonical_digest


CANARY_PROVENANCE = "CONTROLLED_CANARY_NON_PRODUCTION"
CANARY_OPERATION = "okx_demo.execution_chain_canary"
CANARY_POLICY_VERSION = "controlled-canary-v1"
FRESH_EXECUTION_ONLY_ENTRY = "FRESH_EXECUTION_ONLY"
CANARY_TTL_SECONDS = 10
UNRESOLVED_WRITER_STATES = (
    "PREPARED",
    "ACKNOWLEDGED",
    "RECOVERY_REQUIRED",
    "RESIDUAL_CLOSE_REQUIRED",
)


class OkxDemoCanaryPreparationBlocked(RuntimeError):
    """The canary preparation contract was not safe to satisfy."""


class OkxDemoCanaryPreparationWaiting(OkxDemoCanaryPreparationBlocked):
    """The canonical runtime was asked to capture a fresh attested bundle."""

    def __init__(
        self,
        job_id: int,
        *,
        entry_kind: Optional[str] = None,
        supersedes_job_ids: tuple[int, ...] = (),
    ) -> None:
        self.job_id = job_id
        self.entry_kind = entry_kind
        self.supersedes_job_ids = supersedes_job_ids
        super().__init__(
            "canonical runtime attestation is pending; retry canary finalization"
        )


@dataclass(frozen=True)
class CanaryPreparationResult:
    operation_status: str
    provenance: str
    approval_id: int
    trade_intent_id: int
    risk_decision_id: int
    full_chain_run_id: int
    research_job_id: int
    research_job_attempt_id: int
    reconciliation_run_id: int
    canonical_hash: str
    policy_digest: str
    approved_payload_hash: str
    client_order_id: str
    instrument_id: str
    quantity: Decimal
    notional: Decimal
    expires_at: datetime
    idempotency_key_digest: str
    entry_kind: Optional[str] = None
    supersedes_job_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class CanaryAttestationRetryResult:
    """Durable successor request created after a retryable read failure."""

    operation_status: str
    research_job_id: int
    retry_of_job_id: int
    idempotency_key_digest: str


class OkxDemoCanaryPreparationService:
    """Create exactly one bounded canary lineage without strategy promotion."""

    def __init__(
        self,
        db: Session,
        *,
        now_provider=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.db = db
        self._now_provider = now_provider

    def prepare(self, *, idempotency_key: str) -> CanaryPreparationResult:
        """Finalize the original supported execution-only canary entry."""

        return self._prepare(
            idempotency_key=idempotency_key,
            allow_terminal_history=False,
        )

    def prepare_fresh_execution_only(
        self,
        *,
        idempotency_key: str,
    ) -> CanaryPreparationResult:
        """Start/finalize one fresh execution-only entry after old failures.

        This is intentionally a separate operator entry.  The original
        ``/canary/prepare`` remains fail-closed when any other ResearchJob is
        present.  This path only admits immutable, terminal attestation
        failures from the old strategy-coupled canary chain and records their
        ids in the new request payload; it never rewrites or retries them.
        """

        return self._prepare(
            idempotency_key=idempotency_key,
            allow_terminal_history=True,
        )

    def _prepare(
        self,
        *,
        idempotency_key: str,
        allow_terminal_history: bool,
    ) -> CanaryPreparationResult:
        key = _safe_key(idempotency_key)
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        now = _aware(self._now_provider())

        # FastAPI gives this service a fresh session, but direct callers/tests
        # may have inspected the session first.  Never nest an implicit
        # transaction around the one-shot coordination lock.
        if self.db.in_transaction():
            self.db.rollback()

        manifest = get_settings().execution_target_manifest
        target = manifest.active_target
        if (
            manifest.active_target_id != OKX_DEMO_TARGET_ID
            or target.simulated_trading is not True
            or target.allow_real_funds is not False
            or target.order_submission_enabled is not False
        ):
            raise OkxDemoCanaryPreparationBlocked(
                "controlled canary requires OKX_DEMO with global submission disabled"
            )

        # Do not enqueue a fresh runtime request when the durable execution
        # boundary is already occupied.  Once this exact key has a prepared
        # canary lineage, the normal idempotent replay path below may return
        # it; a different key must fail before any new handoff is written.
        if allow_terminal_history and not self._has_trade_intent_for_key(key_digest):
            self._require_no_previous_canary()
            self._require_no_unresolved_writer_attempt()

        # The backend API does not own OKX credentials.  Ask the already
        # running canonical runtime to capture the live attested bundle and
        # return a typed waiting result until that handoff is durable.
        self._ensure_runtime_snapshot_request(
            key_digest=key_digest,
            now=now,
            allow_terminal_history=allow_terminal_history,
        )

        try:
            with self.db.begin():
                if not try_one_shot_transaction_lock(self.db):
                    raise OkxDemoCanaryPreparationBlocked(
                        "canonical runtime is reconciling; retry canary preparation"
                    )

                existing = self._existing_canary(key_digest)
                if existing is not None:
                    return self._result_from_lineage(existing, now=now)

                self._require_no_previous_canary()
                self._require_no_unresolved_writer_attempt()
                reconciliation_run_id = self._fresh_empty_reconciliation(now)
                snapshots = self._fresh_snapshots(now)
                order = self._derive_order(snapshots, now)
                result = self._persist_lineage(
                    key_digest=key_digest,
                    now=now,
                    reconciliation_run_id=reconciliation_run_id,
                    snapshots=snapshots,
                    order=order,
                )
                return result
        except OkxDemoCanaryPreparationBlocked:
            self.db.rollback()
            raise
        except IntegrityError:
            self.db.rollback()
            # A concurrent request with the same idempotency key may have won
            # the unique key.  Re-read it and return the durable winner; any
            # different concurrent canary remains fail-closed.
            winner = self._existing_canary(key_digest)
            if winner is not None:
                return self._result_from_lineage(winner, now=now)
            raise OkxDemoCanaryPreparationBlocked(
                "canary preparation raced with another idempotency key"
            ) from None

    def retry_attestation(self, *, idempotency_key: str) -> CanaryAttestationRetryResult:
        """Queue one auditable successor for a transient runtime read failure.

        The original blocked ResearchJob is intentionally never changed or
        deleted.  Only an ``OkxReadAdapterError`` that the adapter marked as
        retryable can be succeeded by this explicit operator action.  A
        successor is unique to the new idempotency key and a second successor,
        any pending request, grant, or canary lineage remains fail-closed.
        """

        key = _safe_key(idempotency_key)
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        now = _aware(self._now_provider())
        if self.db.in_transaction():
            self.db.rollback()

        manifest = get_settings().execution_target_manifest
        target = manifest.active_target
        if (
            manifest.active_target_id != OKX_DEMO_TARGET_ID
            or target.simulated_trading is not True
            or target.allow_real_funds is not False
            or target.order_submission_enabled is not False
        ):
            raise OkxDemoCanaryPreparationBlocked(
                "controlled canary retry requires OKX_DEMO with global submission disabled"
            )

        with self.db.begin():
            if not try_one_shot_transaction_lock(self.db):
                raise OkxDemoCanaryPreparationBlocked(
                    "canonical runtime is reconciling; retry canary attestation later"
                )
            existing = self._canary_job_for_key(key_digest)
            if existing is not None:
                return self._retry_result(existing, key_digest=key_digest)

            # No strategy-independent retry may coexist with an already
            # prepared/placed canary or an unresolved writer attempt.
            self._require_no_previous_canary()
            self._require_no_unresolved_writer_attempt()
            source = self._retryable_blocked_job()
            if source is None:
                raise OkxDemoCanaryPreparationBlocked(
                    "no retryable canary attestation failure is available"
                )
            if self._retry_successor(source.id) is not None:
                raise OkxDemoCanaryPreparationBlocked(
                    "a retry successor already exists for the blocked canary request"
                )
            if self._has_pending_canary_request():
                raise OkxDemoCanaryPreparationBlocked(
                    "another controlled canary request is still pending"
                )

            payload = dict(source.request_payload or {})
            payload.update(
                {
                    "retry_of_job_id": source.id,
                    "retry_attempt": 1,
                    "bundle_kind": "EXECUTION_ONLY",
                }
            )
            payload.pop("timeframe", None)
            payload.pop("candle_limit", None)
            successor = ResearchJob(
                execution_scope_id=LOCAL_DRY_RUN_SCOPE_ID,
                job_type="okx_demo_controlled_canary",
                operation=CANARY_OPERATION,
                idempotency_key_digest=key_digest,
                request_hash=canonical_digest(payload),
                request_payload=payload,
                status="AWAITING_APPROVAL",
                stage="CANARY_SNAPSHOT_REQUESTED",
                attempt_count=0,
                max_attempts=1,
                evidence_snapshot={
                    "provenance": CANARY_PROVENANCE,
                    "non_production": True,
                    "retry_of_job_id": source.id,
                    "retry_attempt": 1,
                },
                started_at=now,
            )
            self.db.add(successor)
            self.db.flush()
            return CanaryAttestationRetryResult(
                operation_status="WAITING_FOR_RUNTIME_ATTESTATION",
                research_job_id=successor.id,
                retry_of_job_id=source.id,
                idempotency_key_digest=key_digest,
            )

    def _canary_job_for_key(self, key_digest: str) -> Optional[ResearchJob]:
        return self.db.scalars(
            select(ResearchJob).where(
                ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                ResearchJob.operation == CANARY_OPERATION,
                ResearchJob.idempotency_key_digest == key_digest,
            )
        ).first()

    def _retryable_blocked_job(self) -> Optional[ResearchJob]:
        jobs = self.db.scalars(
            select(ResearchJob)
            .where(
                ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                ResearchJob.operation == CANARY_OPERATION,
                ResearchJob.status == "BLOCKED",
                ResearchJob.stage == "CANARY_SNAPSHOT_BLOCKED",
            )
            .order_by(ResearchJob.created_at.desc(), ResearchJob.id.desc())
        ).all()
        for job in jobs:
            payload = job.request_payload if isinstance(job.request_payload, dict) else {}
            if payload.get("provenance") != CANARY_PROVENANCE:
                continue
            # A successor is deliberately one-shot: do not chain retries from
            # a retry itself, even when its later error was transient.
            if payload.get("retry_of_job_id") is not None:
                continue
            evidence = job.evidence_snapshot if isinstance(job.evidence_snapshot, dict) else {}
            error = evidence.get("attestation_error")
            if isinstance(error, dict):
                if error.get("retryable") is not True:
                    continue
            elif job.error_message == OkxReadAdapterError.__name__:
                # #603 predated the redacted retryability fields and only
                # persisted the safe exception type.  Permit this one legacy
                # successor after all current Demo/lineage gates pass; the
                # canonical runtime will classify the live read on retry and
                # persist kind/status without exposing provider payloads.
                pass
            else:
                continue
            return job
        return None

    def _retry_successor(self, source_job_id: int) -> Optional[ResearchJob]:
        jobs = self.db.scalars(
            select(ResearchJob).where(
                ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                ResearchJob.operation == CANARY_OPERATION,
            )
        ).all()
        for job in jobs:
            payload = job.request_payload if isinstance(job.request_payload, dict) else {}
            if payload.get("retry_of_job_id") == source_job_id:
                return job
        return None

    def _has_pending_canary_request(self) -> bool:
        jobs = self.db.scalars(
            select(ResearchJob).where(
                ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                ResearchJob.operation == CANARY_OPERATION,
            )
        ).all()
        for job in jobs:
            if job.status in {"PENDING", "RUNNING", "AWAITING_APPROVAL"}:
                return True
            if job.stage == "CANARY_SNAPSHOT_REQUESTED" and job.status not in {
                "BLOCKED",
                "FAILED",
                "CANCELLED",
                "STALE",
            }:
                return True
        return False

    @staticmethod
    def _retry_result(job: ResearchJob, *, key_digest: str) -> CanaryAttestationRetryResult:
        payload = job.request_payload if isinstance(job.request_payload, dict) else {}
        source_id = payload.get("retry_of_job_id")
        if not isinstance(source_id, int) or source_id <= 0:
            raise OkxDemoCanaryPreparationBlocked(
                "canary retry idempotency lineage is incomplete"
            )
        if job.status == "AWAITING_APPROVAL" and job.stage == "CANARY_SNAPSHOT_REQUESTED":
            status = "WAITING_FOR_RUNTIME_ATTESTATION"
        elif job.status == "SUCCESS" and job.stage == "CANARY_SNAPSHOTS_READY":
            status = "CANARY_SNAPSHOTS_READY"
        else:
            raise OkxDemoCanaryPreparationBlocked(
                "canary retry request is terminal: {}".format(job.stage)
            )
        return CanaryAttestationRetryResult(
            operation_status=status,
            research_job_id=job.id,
            retry_of_job_id=source_id,
            idempotency_key_digest=key_digest,
        )

    def _ensure_runtime_snapshot_request(
        self,
        *,
        key_digest: str,
        now: datetime,
        allow_terminal_history: bool = False,
    ) -> None:
        has_fresh_snapshots = self._has_fresh_snapshot_rows(now)
        if self.db.in_transaction():
            self.db.rollback()
        existing = self.db.scalars(
            select(ResearchJob).where(
                ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                ResearchJob.operation == CANARY_OPERATION,
                ResearchJob.idempotency_key_digest == key_digest,
            )
        ).first()
        other_requests = self.db.scalars(
            select(ResearchJob)
            .where(
                ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                ResearchJob.operation == CANARY_OPERATION,
                ResearchJob.idempotency_key_digest != key_digest,
            )
            .order_by(ResearchJob.created_at, ResearchJob.id)
        ).all()
        supersedes_job_ids: list[int] = []
        if other_requests and allow_terminal_history:
            supersedes_job_ids = self._terminal_history_for_fresh_entry(other_requests)
        elif other_requests:
            self.db.rollback()
            raise OkxDemoCanaryPreparationBlocked(
                "another controlled canary request already owns the idempotency boundary"
            )
        if existing is not None:
            if existing.status == "SUCCESS" and existing.stage == "CANARY_SNAPSHOTS_READY":
                if not has_fresh_snapshots:
                    self.db.rollback()
                    raise OkxDemoCanaryPreparationBlocked(
                        "runtime canary attestation evidence is no longer fresh"
                    )
                self.db.rollback()
                return
            if existing.status == "SUCCESS" and existing.stage == "CANARY_PREPARED":
                # Let the transaction below return the durable lineage on a
                # cross-process/restart replay of the same idempotency key.
                self.db.rollback()
                return
            if existing.status in {"FAILED", "BLOCKED", "CANCELLED", "STALE"}:
                self.db.rollback()
                raise OkxDemoCanaryPreparationBlocked(
                    "canonical runtime attestation request is terminal: {}".format(existing.stage)
                )
            self.db.rollback()
            entry_kind, supersedes = _fresh_entry_metadata(existing.request_payload)
            raise OkxDemoCanaryPreparationWaiting(
                existing.id,
                entry_kind=entry_kind,
                supersedes_job_ids=supersedes,
            )
        # A fresh entry always asks the sole runtime for a new execution-only
        # handoff, even if ordinary reconciliation rows happen to be fresh;
        # this makes the new lineage auditable and avoids borrowing a prior
        # request's snapshots.  Preserve the legacy fast path otherwise.
        if has_fresh_snapshots and not allow_terminal_history:
            self.db.rollback()
            return
        # Serialize the request boundary with the canonical runtime.  The
        # lock is transaction-scoped, so the check and durable enqueue below
        # are one atomic ownership decision; a second idempotency key cannot
        # create a competing pending request during a runtime handoff.
        if not try_one_shot_transaction_lock(self.db):
            self.db.rollback()
            raise OkxDemoCanaryPreparationBlocked(
                "canonical runtime is reconciling; retry canary preparation"
            )
        payload = {
            "provenance": CANARY_PROVENANCE,
            "execution_target": OKX_DEMO_TARGET_ID,
            "instrument_id": "BTC-USDT-SWAP",
            "bundle_kind": "EXECUTION_ONLY",
            "non_production": True,
        }
        if allow_terminal_history:
            payload.update(
                {
                    "entry_kind": FRESH_EXECUTION_ONLY_ENTRY,
                    "supersedes_job_ids": supersedes_job_ids,
                }
            )
        job = ResearchJob(
            execution_scope_id=LOCAL_DRY_RUN_SCOPE_ID,
            job_type="okx_demo_controlled_canary",
            operation=CANARY_OPERATION,
            idempotency_key_digest=key_digest,
            request_hash=canonical_digest(payload),
            request_payload=payload,
            status="AWAITING_APPROVAL",
            stage="CANARY_SNAPSHOT_REQUESTED",
            attempt_count=0,
            max_attempts=1,
            evidence_snapshot={"provenance": CANARY_PROVENANCE},
            started_at=now,
        )
        self.db.add(job)
        self.db.commit()
        entry_kind, supersedes = _fresh_entry_metadata(payload)
        raise OkxDemoCanaryPreparationWaiting(
            job.id,
            entry_kind=entry_kind,
            supersedes_job_ids=supersedes,
        )

    def _terminal_history_for_fresh_entry(
        self,
        jobs: list[ResearchJob],
    ) -> list[int]:
        """Return immutable old attestation failures eligible for supersession.

        The historical #603 signal-bundle request and its #605 execution-only
        retry are both retained as evidence.  No other terminal state is
        eligible: a prior fresh entry, successful snapshot handoff, pending
        request, or unknown failure must remain a hard stop.
        """

        supersedes: list[int] = []
        for job in jobs:
            payload = job.request_payload if isinstance(job.request_payload, dict) else {}
            if payload.get("entry_kind") == FRESH_EXECUTION_ONLY_ENTRY:
                raise OkxDemoCanaryPreparationBlocked(
                    "a fresh execution-only canary entry already exists"
                )
            if not self._is_terminal_attestation_failure(job, payload):
                raise OkxDemoCanaryPreparationBlocked(
                    "another controlled canary request already owns the idempotency boundary"
                )
            supersedes.append(job.id)
        return supersedes

    @staticmethod
    def _is_terminal_attestation_failure(
        job: ResearchJob,
        payload: Mapping[str, Any],
    ) -> bool:
        if job.status != "BLOCKED" or job.stage != "CANARY_SNAPSHOT_BLOCKED":
            return False
        if (
            payload.get("provenance") != CANARY_PROVENANCE
            or payload.get("execution_target") != OKX_DEMO_TARGET_ID
            or payload.get("instrument_id") not in CANARY_INSTRUMENTS
        ):
            return False
        # Accept both the old signal-bundle request and the #607
        # execution-only request as immutable history.  The new request below
        # is always execution-only and never copies the old fields.
        is_execution_only = payload.get("bundle_kind") == "EXECUTION_ONLY"
        is_legacy_signal = (
            payload.get("timeframe") in {"1m", "5m", "15m"}
            and isinstance(payload.get("candle_limit"), int)
            and payload.get("candle_limit", 0) > 0
        )
        if not (is_execution_only or is_legacy_signal):
            return False
        evidence = job.evidence_snapshot if isinstance(job.evidence_snapshot, dict) else {}
        error = evidence.get("attestation_error")
        if isinstance(error, dict):
            return (
                error.get("kind") == "INVALID_SIGNAL_BUNDLE"
                and error.get("retryable") is False
            )
        # #603 stored only the redacted exception type.  It is eligible only
        # when no richer error evidence exists, preserving that source row.
        return job.error_message == OkxReadAdapterError.__name__

    def _has_fresh_snapshot_rows(self, now: datetime) -> bool:
        for kind in ("instrument", "market", "account"):
            row = self.db.scalars(
                select(OkxDemoTrustedSnapshot)
                .where(
                    OkxDemoTrustedSnapshot.execution_target_id == OKX_DEMO_TARGET_ID,
                    OkxDemoTrustedSnapshot.kind == kind,
                    OkxDemoTrustedSnapshot.source_type == "api_aggregate",
                    OkxDemoTrustedSnapshot.core_data.is_(True),
                )
                .order_by(OkxDemoTrustedSnapshot.observed_at.desc())
                .limit(1)
            ).first()
            if row is None or _aware(row.expires_at) <= now:
                return False
        return True

    def _existing_canary(self, key_digest: str) -> Optional[tuple[TradeIntent, RiskDecision, ApprovedExecution, FullChainRun, ResearchJob, ResearchJobAttempt]]:
        intent = self.db.scalars(
            select(TradeIntent).where(
                TradeIntent.execution_target_id == OKX_DEMO_TARGET_ID,
                TradeIntent.idempotency_key_digest == key_digest,
            )
        ).first()
        if intent is None or (intent.request_snapshot or {}).get("provenance") != CANARY_PROVENANCE:
            return None
        decision = self.db.scalars(
            select(RiskDecision).where(RiskDecision.trade_intent_id == intent.id)
        ).first()
        approved = self.db.scalars(
            select(ApprovedExecution).where(ApprovedExecution.trade_intent_id == intent.id)
        ).first()
        chain = self.db.scalars(
            select(FullChainRun).where(FullChainRun.trade_intent_id == intent.id)
        ).first()
        if decision is None or approved is None or chain is None:
            raise OkxDemoCanaryPreparationBlocked(
                "existing canary idempotency lineage is incomplete"
            )
        job = self.db.get(ResearchJob, chain.research_job_id)
        attempt = self.db.get(ResearchJobAttempt, chain.research_job_attempt_id)
        if job is None or attempt is None:
            raise OkxDemoCanaryPreparationBlocked(
                "existing canary research lineage is incomplete"
            )
        return intent, decision, approved, chain, job, attempt

    def _has_trade_intent_for_key(self, key_digest: str) -> bool:
        return (
            self.db.scalars(
                select(TradeIntent.id).where(
                    TradeIntent.execution_target_id == OKX_DEMO_TARGET_ID,
                    TradeIntent.idempotency_key_digest == key_digest,
                )
            ).first()
            is not None
        )

    def _require_no_previous_canary(self) -> None:
        rows = self.db.scalars(
            select(TradeIntent)
            .where(TradeIntent.execution_target_id == OKX_DEMO_TARGET_ID)
            .order_by(TradeIntent.id)
        )
        for intent in rows:
            if (intent.request_snapshot or {}).get("provenance") == CANARY_PROVENANCE:
                raise OkxDemoCanaryPreparationBlocked(
                    "a prior controlled canary lineage already exists"
                )
        active_grant = self.db.scalars(
            select(OkxDemoSubmissionGrant).where(
                OkxDemoSubmissionGrant.execution_target_id == OKX_DEMO_TARGET_ID,
                OkxDemoSubmissionGrant.status == "ACTIVE",
            )
        ).first()
        if active_grant is not None:
            raise OkxDemoCanaryPreparationBlocked(
                "another one-shot grant is already active"
            )

    def _require_no_unresolved_writer_attempt(self) -> None:
        unresolved = self.db.scalars(
            select(OkxOrderWriteAttempt.id)
            .where(
                OkxOrderWriteAttempt.execution_target_id == OKX_DEMO_TARGET_ID,
                OkxOrderWriteAttempt.state.in_(UNRESOLVED_WRITER_STATES),
            )
            .limit(1)
        ).first()
        if unresolved is not None:
            raise OkxDemoCanaryPreparationBlocked(
                "unresolved writer attempt blocks controlled canary"
            )

    def _fresh_empty_reconciliation(self, now: datetime) -> int:
        state = self.db.scalars(
            select(OkxDemoReconciliationState).where(
                OkxDemoReconciliationState.execution_target_id == OKX_DEMO_TARGET_ID
            )
        ).first()
        if state is None or state.last_reconciliation_run_id is None:
            raise OkxDemoCanaryPreparationBlocked(
                "fresh empty reconciliation is required"
            )
        try:
            run = require_canary_reconciliation(
                self.db,
                reconciliation_run_id=state.last_reconciliation_run_id,
                now=now,
                for_update=True,
            )
        except OkxDemoSubmissionGrantBlocked as exc:
            raise OkxDemoCanaryPreparationBlocked(str(exc)) from None
        return run.id

    def _fresh_snapshots(self, now: datetime) -> dict[str, OkxDemoTrustedSnapshot]:
        rows: dict[str, OkxDemoTrustedSnapshot] = {}
        for kind in ("instrument", "market", "account"):
            row = self.db.scalars(
                select(OkxDemoTrustedSnapshot)
                .where(
                    OkxDemoTrustedSnapshot.execution_target_id == OKX_DEMO_TARGET_ID,
                    OkxDemoTrustedSnapshot.kind == kind,
                    OkxDemoTrustedSnapshot.source_type == "api_aggregate",
                    OkxDemoTrustedSnapshot.core_data.is_(True),
                )
                .order_by(
                    OkxDemoTrustedSnapshot.observed_at.desc(),
                    OkxDemoTrustedSnapshot.database_id.desc(),
                )
                .limit(1)
            ).first()
            if row is None or _aware(row.expires_at) <= now or _aware(row.observed_at) > now:
                raise OkxDemoCanaryPreparationBlocked(
                    "fresh attested {} snapshot is required".format(kind)
                )
            rows[kind] = row
        if len({row.attested_session_id for row in rows.values()}) != 1:
            raise OkxDemoCanaryPreparationBlocked(
                "instrument, market and account snapshots must share one attested session"
            )
        for row in rows.values():
            session = self.db.get(OkxDemoAttestedSession, row.attested_session_id)
            if (
                session is None
                or session.revoked_at is not None
                or _aware(session.expires_at) <= now
                or
                row.digest != canonical_digest(row.content_json)
                or row.snapshot_id != _trusted_snapshot_id(row)
                or row.attestation_fingerprint_sha256 == ""
                or row.attestation_fingerprint_sha256 != session.pinned_fingerprint_sha256
                or _aware(row.attested_session_expires_at) != _aware(session.expires_at)
            ):
                raise OkxDemoCanaryPreparationBlocked(
                    "attested canary snapshot identity or digest is invalid"
                )
        instrument = rows["instrument"].content_json
        market = rows["market"].content_json
        account = rows["account"].content_json
        if (
            instrument.get("execution_target") != OKX_DEMO_TARGET_ID
            or market.get("execution_target") != OKX_DEMO_TARGET_ID
            or account.get("execution_target") != OKX_DEMO_TARGET_ID
            or instrument.get("source") != "okx_demo_rest"
            or market.get("source") != "okx_demo_rest"
            or account.get("source") != "okx_demo_rest"
            or any(content.get("stale") is not False for content in (instrument, market, account))
            or account.get("authenticated") is not True
        ):
            raise OkxDemoCanaryPreparationBlocked(
                "attested canary snapshots are not fresh authenticated Demo data"
            )
        if market.get("instrument_id") != instrument.get("instId"):
            raise OkxDemoCanaryPreparationBlocked(
                "market snapshot instrument does not match instrument snapshot"
            )
        return rows

    def _derive_order(
        self,
        snapshots: Mapping[str, OkxDemoTrustedSnapshot],
        now: datetime,
    ) -> dict[str, Any]:
        instrument = snapshots["instrument"].content_json
        market = snapshots["market"].content_json
        account = snapshots["account"].content_json
        instrument_id = str(instrument.get("instId") or "")
        if instrument_id not in CANARY_INSTRUMENTS:
            raise OkxDemoCanaryPreparationBlocked(
                "instrument is not the fixed controlled-canary allowlist"
            )
        if instrument.get("contract_shape") not in {"linear", "inverse"} or instrument.get("state") != "live":
            raise OkxDemoCanaryPreparationBlocked("allowlisted canary instrument is not live SWAP")
        try:
            min_size = _decimal(instrument.get("minSz"), "instrument minSz", positive=True)
            lot_size = _decimal(instrument.get("lotSz"), "instrument lotSz", positive=True)
            contract_value = _decimal(instrument.get("ctVal"), "instrument ctVal", positive=True)
            tick_size = _decimal(instrument.get("tickSz"), "instrument tickSz", positive=True)
            reference_price = _decimal(market.get("reference_price"), "market reference_price", positive=True)
            best_ask = _decimal((market.get("bbo") or {}).get("ask_price"), "market ask", positive=True)
            best_bid = _decimal((market.get("bbo") or {}).get("bid_price"), "market bid", positive=True)
            mark_price = _decimal((market.get("mark") or {}).get("price"), "market mark", positive=True)
            leverage = _decimal((account.get("leverage_by_position_side") or {}).get("long"), "account leverage", positive=True)
        except (InvalidOperation, TypeError, ValueError, AttributeError):
            raise OkxDemoCanaryPreparationBlocked("canary market/instrument/account evidence is malformed") from None
        if mark_price != reference_price or now - _aware(market.get("as_of")) > timedelta(seconds=MAX_RECONCILIATION_AGE_SECONDS):
            raise OkxDemoCanaryPreparationBlocked("canary market quote is stale or inconsistent")
        quantity = (min_size / lot_size).to_integral_value(rounding=ROUND_UP) * lot_size
        if quantity < min_size or quantity % lot_size != 0:
            raise OkxDemoCanaryPreparationBlocked("canary quantity violates exchange lot size")
        # Buy at the current bid to make the bounded canary unlikely to fill;
        # the runtime still owns timeout/cancel/reconciliation if it does.
        limit_price = (best_bid / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size
        if limit_price <= 0:
            raise OkxDemoCanaryPreparationBlocked("canary bid cannot be quantized")
        notional = quantity * contract_value * max(reference_price, best_ask, limit_price)
        if notional <= 0 or notional > CANARY_NOTIONAL_CAP:
            raise OkxDemoCanaryPreparationBlocked(
                "exchange minimum canary quantity exceeds the fixed notional cap"
            )
        expiry = min(
            _aware(snapshots[name].expires_at)
            for name in ("instrument", "market", "account")
        )
        expiry = min(expiry, now + timedelta(seconds=CANARY_TTL_SECONDS))
        if expiry <= now:
            raise OkxDemoCanaryPreparationBlocked("canary evidence expires before grant")
        return {
            "instrument_id": instrument_id,
            "quantity": quantity,
            "notional": notional,
            "limit_price": limit_price,
            "reference_price": reference_price,
            "leverage": leverage,
            "expires_at": expiry,
            "side": "buy",
            "position_side": "long",
            "order_type": "limit",
            "margin_mode": "isolated",
            "reduce_only": False,
            "stop_loss": reference_price * Decimal("0.95"),
            "take_profit": reference_price * Decimal("1.05"),
        }

    def _persist_lineage(
        self,
        *,
        key_digest: str,
        now: datetime,
        reconciliation_run_id: int,
        snapshots: Mapping[str, OkxDemoTrustedSnapshot],
        order: Mapping[str, Any],
    ) -> CanaryPreparationResult:
        evidence = {
            kind: {
                "snapshot_id": row.snapshot_id,
                "database_id": row.database_id,
                "digest": row.digest,
                "expires_at": _aware(row.expires_at).isoformat(),
            }
            for kind, row in snapshots.items()
        }
        policy = {
            "provenance": CANARY_PROVENANCE,
            "allowed_instruments": [order["instrument_id"]],
            "allowed_sides": [order["side"]],
            "allowed_order_types": [order["order_type"]],
            "max_leverage": format(order["leverage"], "f"),
            "max_order_notional": format(CANARY_NOTIONAL_CAP, "f"),
            "max_total_exposure": format(CANARY_NOTIONAL_CAP, "f"),
            "max_positions": 1,
            "max_price_deviation_pct": "0.01",
            "min_strategy_score": "0",
            "scoring_version": CANARY_POLICY_VERSION,
        }
        policy_digest = canonical_digest(policy)
        research_payload = {
            "provenance": CANARY_PROVENANCE,
            "operation": CANARY_OPERATION,
            "execution_target": OKX_DEMO_TARGET_ID,
            "instrument_id": order["instrument_id"],
            "quantity": format(order["quantity"], "f"),
            "notional": format(order["notional"], "f"),
            "snapshot_evidence": evidence,
        }
        job = self.db.scalars(
            select(ResearchJob).where(
                ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                ResearchJob.operation == CANARY_OPERATION,
                ResearchJob.idempotency_key_digest == key_digest,
            )
        ).first()
        # Preserve the fresh-entry lineage when the runtime handoff job is
        # promoted to the prepared lineage.  Historical jobs retain their
        # original request shape; no old row is rewritten.
        if job is not None:
            handoff_payload = job.request_payload if isinstance(job.request_payload, dict) else {}
            if handoff_payload.get("entry_kind") == FRESH_EXECUTION_ONLY_ENTRY:
                research_payload["entry_kind"] = FRESH_EXECUTION_ONLY_ENTRY
                research_payload["supersedes_job_ids"] = list(
                    handoff_payload.get("supersedes_job_ids") or []
                )
        entry_kind, supersedes_job_ids = _fresh_entry_metadata(
            job.request_payload if job is not None else research_payload
        )
        request_hash = canonical_digest(research_payload)
        if job is None:
            job = ResearchJob(
                execution_scope_id=LOCAL_DRY_RUN_SCOPE_ID,
                job_type="okx_demo_controlled_canary",
                operation=CANARY_OPERATION,
                idempotency_key_digest=key_digest,
                request_hash=request_hash,
                request_payload=research_payload,
                status="SUCCESS",
                stage="CANARY_PREPARED",
                attempt_count=1,
                max_attempts=1,
                started_at=now,
                completed_at=now,
                evidence_snapshot={"provenance": CANARY_PROVENANCE, "snapshot_evidence": evidence},
            )
            self.db.add(job)
            self.db.flush()
        else:
            job.request_hash = request_hash
            job.request_payload = research_payload
            job.status = "SUCCESS"
            job.stage = "CANARY_PREPARED"
            job.attempt_count = 1
            job.completed_at = now
            job.evidence_snapshot = {
                **(job.evidence_snapshot or {}),
                "provenance": CANARY_PROVENANCE,
                "snapshot_evidence": evidence,
            }
        attempt = self.db.scalars(
            select(ResearchJobAttempt).where(
                ResearchJobAttempt.research_job_id == job.id,
                ResearchJobAttempt.attempt_number == 1,
            )
        ).first()
        if attempt is None:
            attempt = ResearchJobAttempt(
                research_job_id=job.id,
                attempt_number=1,
                execution_scope_id=LOCAL_DRY_RUN_SCOPE_ID,
                status="SUCCESS",
                started_at=now,
                completed_at=now,
                evidence_snapshot={"provenance": CANARY_PROVENANCE, "request_hash": request_hash},
            )
            self.db.add(attempt)
            self.db.flush()
        else:
            attempt.status = "SUCCESS"
            attempt.completed_at = now
            attempt.evidence_snapshot = {
                **(attempt.evidence_snapshot or {}),
                "provenance": CANARY_PROVENANCE,
                "request_hash": request_hash,
            }
        chain = FullChainRun(
            research_job_id=job.id,
            research_job_attempt_id=attempt.id,
            run_kind="RESEARCH",
            research_scope_id=LOCAL_DRY_RUN_SCOPE_ID,
            execution_target_id=OKX_DEMO_TARGET_ID,
            status="EXECUTING",
            current_stage="EXECUTION",
            started_at=now,
        )
        self.db.add(chain)
        self.db.flush()
        signal_digest = canonical_digest({
            "provenance": CANARY_PROVENANCE,
            "full_chain_run_id": chain.id,
            "instrument_id": order["instrument_id"],
            "side": order["side"],
            "quantity": format(order["quantity"], "f"),
        })
        lineage = {
            "provenance": CANARY_PROVENANCE,
            "strategy_id": None,
            "strategy_version_id": None,
            "backtest_run_id": None,
            "backtest_task_id": None,
            "backtest_result_id": None,
            "strategy_score_id": None,
        }
        canonical_input = {
            "execution_target": OKX_DEMO_TARGET_ID,
            "full_chain_run_id": chain.id,
            "candidate_approval_id": None,
            "signal_snapshot_id": None,
            "signal_digest": signal_digest,
            "lineage": lineage,
            "snapshot_ids": {name: row.snapshot_id for name, row in snapshots.items()},
            "instrument_id": order["instrument_id"],
            "side": order["side"],
            "position_side": order["position_side"],
            "order_type": order["order_type"],
            "quantity": format(order["quantity"], "f"),
            "limit_price": format(order["limit_price"], "f"),
            "reference_price": format(order["reference_price"], "f"),
            "leverage": format(order["leverage"], "f"),
            "margin_mode": order["margin_mode"],
            "stop_loss": format(order["stop_loss"], "f"),
            "take_profit": format(order["take_profit"], "f"),
            "reduce_only": order["reduce_only"],
            "provenance": CANARY_PROVENANCE,
        }
        canonical_hash = canonical_digest(canonical_input)
        intent_id = canonical_digest({
            "provenance": CANARY_PROVENANCE,
            "idempotency_key_digest": key_digest,
            "canonical_hash": canonical_hash,
        })
        client_order_id = "FAICANARY" + intent_id[:23]
        intent = TradeIntent(
            execution_target_id=OKX_DEMO_TARGET_ID,
            authorization_schema_version="RISK_V1",
            intent_id=intent_id,
            canonical_hash=canonical_hash,
            policy_digest=policy_digest,
            idempotency_key_digest=key_digest,
            client_order_id=client_order_id,
            instrument_id=order["instrument_id"],
            side=order["side"],
            position_side=order["position_side"],
            order_type=order["order_type"],
            quantity=order["quantity"],
            limit_price=order["limit_price"],
            reference_price=order["reference_price"],
            leverage=order["leverage"],
            margin_mode=order["margin_mode"],
            stop_loss=order["stop_loss"],
            take_profit=order["take_profit"],
            reduce_only=order["reduce_only"],
            status="APPROVED",
            request_snapshot={
                "canonical_input": canonical_input,
                "snapshot_evidence": evidence,
                "provenance": CANARY_PROVENANCE,
                "non_production": True,
            },
            expires_at=order["expires_at"],
        )
        intent.approved_payload_hash = canonical_digest({
            "canonical_input": canonical_input,
            "notional": format(order["notional"], "f"),
            "provenance": CANARY_PROVENANCE,
        })
        self.db.add(intent)
        self.db.flush()
        decision = RiskDecision(
            execution_target_id=OKX_DEMO_TARGET_ID,
            trade_intent_id=intent.id,
            authorization_schema_version="RISK_V1",
            policy_digest=policy_digest,
            decision="APPROVED",
            policy_version=CANARY_POLICY_VERSION,
            evidence_snapshot={
                "reasons": [],
                "input_digest": canonical_hash,
                "policy_digest": policy_digest,
                "lineage": lineage,
                "notional": format(order["notional"], "f"),
                "provenance": CANARY_PROVENANCE,
                "non_production": True,
                "llm_authority": False,
            },
        )
        self.db.add(decision)
        self.db.flush()
        approved = ApprovedExecution(
            execution_target_id=OKX_DEMO_TARGET_ID,
            trade_intent_id=intent.id,
            risk_decision_id=decision.id,
            intent_id=intent.intent_id,
            client_order_id=client_order_id,
            authorization_schema_version="RISK_V1",
            canonical_hash=canonical_hash,
            policy_digest=policy_digest,
            approved_payload_hash=intent.approved_payload_hash,
            instrument_snapshot_id=snapshots["instrument"].snapshot_id,
            market_snapshot_id=snapshots["market"].snapshot_id,
            account_snapshot_id=snapshots["account"].snapshot_id,
            decision="APPROVED",
            intent_status="APPROVED",
            reserved_notional=order["notional"],
            order_submission_authorized=False,
            claim_required=True,
            status="ACTIVE",
            expires_at=order["expires_at"],
            evidence_snapshot={
                "provenance": CANARY_PROVENANCE,
                "non_production": True,
                "lineage": lineage,
                "snapshot_evidence": evidence,
            },
        )
        self.db.add(approved)
        self.db.flush()
        chain.trade_intent_id = intent.id
        chain.risk_decision_id = decision.id
        chain.approved_execution_id = approved.id
        checkpoint = FullChainStageRun(
            full_chain_run_id=chain.id,
            stage="RISK",
            status="SUCCESS",
            idempotency_key_digest=canonical_digest(
                {"provenance": CANARY_PROVENANCE, "key_digest": key_digest, "stage": "RISK"}
            ),
            input_digest=request_hash,
            input_snapshot={"provenance": CANARY_PROVENANCE, "non_production": True},
            output_snapshot={"provenance": CANARY_PROVENANCE, "non_production": True},
            database_ids={
                "trade_intent_id": intent.id,
                "risk_decision_id": decision.id,
                "approved_execution_id": approved.id,
            },
            prepared_at=now,
            completed_at=now,
        )
        self.db.add(checkpoint)
        self.db.flush()
        return CanaryPreparationResult(
            operation_status="PREPARED",
            provenance=CANARY_PROVENANCE,
            approval_id=approved.id,
            trade_intent_id=intent.id,
            risk_decision_id=decision.id,
            full_chain_run_id=chain.id,
            research_job_id=job.id,
            research_job_attempt_id=attempt.id,
            reconciliation_run_id=reconciliation_run_id,
            canonical_hash=canonical_hash,
            policy_digest=policy_digest,
            approved_payload_hash=intent.approved_payload_hash,
            client_order_id=client_order_id,
            instrument_id=order["instrument_id"],
            quantity=order["quantity"],
            notional=order["notional"],
            expires_at=order["expires_at"],
            idempotency_key_digest=key_digest,
            entry_kind=entry_kind,
            supersedes_job_ids=supersedes_job_ids,
        )

    def _result_from_lineage(
        self,
        lineage: tuple[TradeIntent, RiskDecision, ApprovedExecution, FullChainRun, ResearchJob, ResearchJobAttempt],
        *,
        now: datetime,
    ) -> CanaryPreparationResult:
        intent, decision, approved, chain, job, attempt = lineage
        if approved.status != "ACTIVE" or intent.status != "APPROVED" or decision.decision != "APPROVED":
            raise OkxDemoCanaryPreparationBlocked("canary idempotency lineage is no longer active")
        if approved.expires_at is None or _aware(approved.expires_at) <= now:
            raise OkxDemoCanaryPreparationBlocked("canary idempotency lineage has expired")
        notional = Decimal(str(approved.reserved_notional))
        entry_kind, supersedes_job_ids = _fresh_entry_metadata(job.request_payload)
        return CanaryPreparationResult(
            operation_status="PREPARED",
            provenance=CANARY_PROVENANCE,
            approval_id=approved.id,
            trade_intent_id=intent.id,
            risk_decision_id=decision.id,
            full_chain_run_id=chain.id,
            research_job_id=job.id,
            research_job_attempt_id=attempt.id,
            reconciliation_run_id=self._reconciliation_run_id_for_approval(approved.id),
            canonical_hash=approved.canonical_hash,
            policy_digest=approved.policy_digest,
            approved_payload_hash=approved.approved_payload_hash,
            client_order_id=approved.client_order_id,
            instrument_id=intent.instrument_id or "",
            quantity=Decimal(str(intent.quantity)),
            notional=notional,
            expires_at=_aware(approved.expires_at),
            idempotency_key_digest=intent.idempotency_key_digest or "",
            entry_kind=entry_kind,
            supersedes_job_ids=supersedes_job_ids,
        )

    def _reconciliation_run_id_for_approval(self, approval_id: int) -> int:
        grant = self.db.scalars(
            select(OkxDemoSubmissionGrant.reconciliation_run_id)
            .where(OkxDemoSubmissionGrant.approval_id == approval_id)
        ).first()
        if grant is not None:
            return int(grant)
        state = self.db.scalars(
            select(OkxDemoReconciliationState).where(
                OkxDemoReconciliationState.execution_target_id == OKX_DEMO_TARGET_ID
            )
        ).first()
        if state is None or state.last_reconciliation_run_id is None:
            raise OkxDemoCanaryPreparationBlocked("canary reconciliation binding is missing")
        return state.last_reconciliation_run_id


def process_pending_canary_attestation(
    *,
    read_client: Any,
    db: Session,
    now: Optional[datetime] = None,
) -> bool:
    """Let the sole credential-bearing runtime capture a pending canary bundle."""

    # Some runtime contract tests use a deliberately narrow fake DB session;
    # no canary request can exist on that object, so preserve the no-op cycle.
    if not hasattr(db, "scalars"):
        return False
    current = _aware(now or datetime.now(timezone.utc))
    job = db.scalars(
        select(ResearchJob)
        .where(
            ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
            ResearchJob.operation == CANARY_OPERATION,
            ResearchJob.status == "AWAITING_APPROVAL",
            ResearchJob.stage == "CANARY_SNAPSHOT_REQUESTED",
        )
        .order_by(ResearchJob.created_at, ResearchJob.id)
        .with_for_update()
    ).first()
    if job is None:
        return False
    payload = job.request_payload if isinstance(job.request_payload, dict) else {}
    if (
        payload.get("provenance") != CANARY_PROVENANCE
        or payload.get("execution_target") != OKX_DEMO_TARGET_ID
        or payload.get("instrument_id") not in CANARY_INSTRUMENTS
        or payload.get("bundle_kind") != "EXECUTION_ONLY"
    ):
        job.status = "BLOCKED"
        job.stage = "CANARY_SNAPSHOT_BLOCKED"
        job.error_message = "canary attestation request payload is invalid"
        return True
    try:
        bundle = read_client.capture_execution_attestation(
            db,
            inst_id=payload["instrument_id"],
        )
        references = {
            name: {
                "database_id": getattr(getattr(bundle, name), "database_id", None),
                "snapshot_id": getattr(getattr(bundle, name), "snapshot_id", None),
                "digest": getattr(getattr(bundle, name), "digest", None),
                "expires_at": _aware(bundle.expires_at).isoformat(),
            }
            for name in ("instrument", "market", "account")
        }
        if any(
            not reference["snapshot_id"] or not reference["digest"]
            for reference in references.values()
        ):
            raise RuntimeError("runtime returned incomplete trusted snapshot references")
        job.status = "SUCCESS"
        job.stage = "CANARY_SNAPSHOTS_READY"
        job.attempt_count = 1
        job.completed_at = current
        job.evidence_snapshot = {
            "provenance": CANARY_PROVENANCE,
            "non_production": True,
            **_fresh_entry_evidence(payload),
            "snapshot_evidence": references,
            "bundle_observed_at": _aware(bundle.observed_at).isoformat(),
        }
        return True
    except OkxReadAdapterError as exc:
        job.status = "BLOCKED"
        job.stage = "CANARY_SNAPSHOT_BLOCKED"
        job.completed_at = current
        # Keep credential/provider payloads out of the durable audit record.
        job.error_message = type(exc).__name__
        job.evidence_snapshot = {
            "provenance": CANARY_PROVENANCE,
            "non_production": True,
            **_fresh_entry_evidence(payload),
            "attestation_error": {
                "error_type": type(exc).__name__,
                "kind": exc.kind,
                "status": exc.status,
                "retryable": bool(exc.retryable),
            },
        }
        return True
    except Exception as exc:
        job.status = "BLOCKED"
        job.stage = "CANARY_SNAPSHOT_BLOCKED"
        job.completed_at = current
        # Keep credential/provider payloads out of the durable audit record.
        job.error_message = type(exc).__name__
        job.evidence_snapshot = {
            "provenance": CANARY_PROVENANCE,
            "non_production": True,
            **_fresh_entry_evidence(payload),
            "attestation_error": {
                "error_type": type(exc).__name__,
                "retryable": False,
            },
        }
        return True


def _fresh_entry_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only non-sensitive fresh-entry lineage into runtime evidence."""

    if payload.get("entry_kind") != FRESH_EXECUTION_ONLY_ENTRY:
        return {}
    supersedes = payload.get("supersedes_job_ids")
    if not isinstance(supersedes, list) or any(
        not isinstance(job_id, int) or job_id <= 0 for job_id in supersedes
    ):
        return {"entry_kind": FRESH_EXECUTION_ONLY_ENTRY, "supersedes_job_ids": []}
    return {
        "entry_kind": FRESH_EXECUTION_ONLY_ENTRY,
        "supersedes_job_ids": list(supersedes),
    }


def _fresh_entry_metadata(payload: Any) -> tuple[Optional[str], tuple[int, ...]]:
    """Read the bounded, non-sensitive fresh-entry lineage metadata."""

    if not isinstance(payload, dict) or payload.get("entry_kind") != FRESH_EXECUTION_ONLY_ENTRY:
        return None, ()
    supersedes = payload.get("supersedes_job_ids")
    if not isinstance(supersedes, list) or any(
        not isinstance(job_id, int) or job_id <= 0 for job_id in supersedes
    ):
        raise OkxDemoCanaryPreparationBlocked(
            "fresh execution-only canary lineage is malformed"
        )
    return FRESH_EXECUTION_ONLY_ENTRY, tuple(supersedes)


def _safe_key(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or not value.isascii():
        raise OkxDemoCanaryPreparationBlocked("canary idempotency key is invalid")
    if any(not (char.isalnum() or char in "._:-") for char in value):
        raise OkxDemoCanaryPreparationBlocked("canary idempotency key is invalid")
    return value


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise OkxDemoCanaryPreparationBlocked("{} is malformed".format(name)) from None
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise OkxDemoCanaryPreparationBlocked("{} is malformed".format(name))
    return parsed


def _aware(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise OkxDemoCanaryPreparationBlocked("snapshot timestamp is malformed") from None
    if not isinstance(value, datetime):
        raise OkxDemoCanaryPreparationBlocked("snapshot timestamp is malformed")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
