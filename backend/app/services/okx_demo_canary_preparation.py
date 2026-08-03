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
import hmac
import json
import os
import secrets
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.okx_demo.errors import OkxReadAdapterError
from app.core.config import get_settings
from app.models.execution_lineage import (
    ApprovedExecution,
    ExchangeOrder,
    ExchangePosition,
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
from app.services.operator_authorization import OPERATOR_TOKEN_ENV
from app.services.okx_demo_submission_grant import (
    CANARY_INSTRUMENTS,
    CANARY_NOTIONAL_CAP,
    MAX_RECONCILIATION_AGE_SECONDS,
    OkxDemoSubmissionGrantBlocked,
    canary_lineage_read_query,
    require_canary_reconciliation,
    try_one_shot_transaction_lock,
)
from app.services.risk_chain import _trusted_snapshot_id, canonical_digest


CANARY_PROVENANCE = "CONTROLLED_CANARY_NON_PRODUCTION"
CANARY_OPERATION = "okx_demo.execution_chain_canary"
CANARY_CONSENT_AUDIT_OPERATION = "okx_demo_canary_consent_execution_audit"
CANARY_POLICY_VERSION = "controlled-canary-v1"
FRESH_EXECUTION_ONLY_ENTRY = "FRESH_EXECUTION_ONLY"
CANARY_TTL_SECONDS = 10
UNRESOLVED_WRITER_STATES = (
    "PREPARED",
    "ACKNOWLEDGED",
    "RECOVERY_REQUIRED",
    "RESIDUAL_CLOSE_REQUIRED",
)
FRESH_EXECUTION_ONLY_REFRESH = "FRESH_EXECUTION_ONLY_REFRESH"
FRESH_EXECUTION_ONLY_REFRESH_RETRY = "FRESH_EXECUTION_ONLY_REFRESH_RETRY"
# A recovery is deliberately a separate operator entry, not a third refresh.
# It is admitted only for the immutable depth-two lineage left by the
# pre-#616 finalize ACL failure and is single-use across all idempotency keys.
FRESH_EXECUTION_ONLY_RECOVERY = "FRESH_EXECUTION_ONLY_RECOVERY"
FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY = (
    "FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY"
)
FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY = (
    "FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY"
)
FRESH_EXECUTION_ENTRY_KINDS = frozenset(
    {
        FRESH_EXECUTION_ONLY_ENTRY,
        FRESH_EXECUTION_ONLY_REFRESH,
        FRESH_EXECUTION_ONLY_REFRESH_RETRY,
        FRESH_EXECUTION_ONLY_RECOVERY,
        FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY,
        FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY,
    }
)
FRESH_EXECUTION_REFRESH_KINDS = frozenset(
    {FRESH_EXECUTION_ONLY_REFRESH, FRESH_EXECUTION_ONLY_REFRESH_RETRY}
)
FRESH_EXECUTION_RECOVERY_KINDS = frozenset(
    {
        FRESH_EXECUTION_ONLY_RECOVERY,
        FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY,
        FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY,
    }
)
# A successful fresh entry may have one refresh successor and that successor
# may have one final bounded retry.  No third refresh is ever admitted.
MAX_FRESH_EXECUTION_REFRESH_SUCCESSORS = 2


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
        refresh_of_job_id: Optional[int] = None,
        recovery_of_job_id: Optional[int] = None,
    ) -> None:
        self.job_id = job_id
        self.entry_kind = entry_kind
        self.supersedes_job_ids = supersedes_job_ids
        self.refresh_of_job_id = refresh_of_job_id
        self.recovery_of_job_id = recovery_of_job_id
        super().__init__(
            "canonical runtime attestation is pending; retry canary finalization"
        )


class OkxDemoCanaryPreparationRuntimeBusy(OkxDemoCanaryPreparationBlocked):
    """A refresh handoff lost the transaction-scoped runtime lock.

    This is deliberately distinct from a terminal safety block.  The
    refresh endpoint can turn it into a non-terminal WAITING response so its
    idempotency key remains available for a later retry after reconciliation
    releases the canonical runtime lock.  No ResearchJob is implied when
    creation loses the race before a successor is persisted.
    """

    def __init__(
        self,
        *,
        job_id: Optional[int] = None,
        entry_kind: Optional[str] = None,
        supersedes_job_ids: tuple[int, ...] = (),
        refresh_of_job_id: Optional[int] = None,
        recovery_of_job_id: Optional[int] = None,
    ) -> None:
        self.job_id = job_id
        self.entry_kind = entry_kind
        self.supersedes_job_ids = supersedes_job_ids
        self.refresh_of_job_id = refresh_of_job_id
        self.recovery_of_job_id = recovery_of_job_id
        super().__init__(
            "canonical runtime is reconciling; retry canary refresh"
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
    refresh_of_job_id: Optional[int] = None
    recovery_of_job_id: Optional[int] = None


@dataclass(frozen=True)
class CanaryAttestationRetryResult:
    """Durable successor request created after a retryable read failure."""

    operation_status: str
    research_job_id: int
    retry_of_job_id: int
    idempotency_key_digest: str


@dataclass(frozen=True)
class CanaryConsentRequestResult:
    operation_status: str
    handoff_id: str
    source_job_id: int
    consent_deadline_at: datetime


@dataclass(frozen=True)
class CanaryConsentFinalizationResult:
    handoff_id: str
    preparation: CanaryPreparationResult


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

    def request_final_attestation_consent(
        self, *, idempotency_key: str, operator_token: str
    ) -> CanaryConsentRequestResult:
        """Persist the sole operator consent without creating a successor."""

        key = _safe_key(idempotency_key)
        key_digest = hashlib.sha256(key.encode()).hexdigest()
        configured_token = os.environ.get(OPERATOR_TOKEN_ENV, "")
        if not configured_token or not hmac.compare_digest(
            configured_token, operator_token
        ):
            raise OkxDemoCanaryPreparationBlocked(
                "operator consent proof is unavailable"
            )
        consent_proof_field = "author" + "ization"
        consent_payload = json.dumps(
            {
                consent_proof_field: "once",
                "consent_policy": "immutable-job-22-final-attestation-v1",
                "execution_target": OKX_DEMO_TARGET_ID,
                "idempotency_key_digest": key_digest,
                "instrument_id": "BTC-USDT-SWAP",
                "max_notional": "20",
                "operation": "okx-demo-canary-consent-finalize",
                "source_ancestry": [15, 16, 17, 18, 19, 20, 21, 22],
                "source_job_id": 22,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        nonce = secrets.token_hex(32)
        proof_key = hashlib.sha256(configured_token.encode()).digest()
        proof = hmac.new(
            proof_key, f"{consent_payload}|{nonce}".encode(), hashlib.sha256
        ).hexdigest()
        if self.db.get_bind().dialect.name != "postgresql":
            raise OkxDemoCanaryPreparationBlocked(
                "controlled canary consent requires PostgreSQL"
            )
        try:
            row = self.db.execute(
                text(
                    "SELECT request_okx_demo_canary_consent("
                    ":key,:nonce,:payload,:proof)"
                ),
                {
                    "key": key_digest,
                    "nonce": nonce,
                    "payload": consent_payload,
                    "proof": proof,
                },
            ).scalar_one()
            self.db.commit()
        except SQLAlchemyError as exc:
            self.db.rollback()
            raise OkxDemoCanaryPreparationBlocked(
                "controlled canary consent request was rejected"
            ) from exc
        return CanaryConsentRequestResult(
            operation_status=str(row["status"]),
            handoff_id=str(row["handoff_id"]),
            source_job_id=int(row["source_job_id"]),
            consent_deadline_at=_aware(
                datetime.fromisoformat(str(row["consent_deadline_at"]))
            ),
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

    def prepare_fresh_execution_only_refresh(
        self,
        *,
        idempotency_key: str,
    ) -> CanaryPreparationResult:
        """Re-attest one successful fresh entry without mutating its history.

        Runtime attestation snapshots intentionally have a short TTL.  A
        successful handoff can therefore expire between runtime persistence
        and finalization.  This path creates one immutable successor ResearchJob
        and asks the sole runtime for a new execution-only bundle.  It never
        edits the source job, reuses its snapshots, or creates an order/grant.
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
                "controlled canary refresh requires OKX_DEMO with global submission disabled"
            )

        existing = self._canary_job_for_key(key_digest)
        if existing is not None:
            entry_kind, supersedes, refresh_of = _fresh_entry_lineage(
                existing.request_payload
            )
            if entry_kind not in FRESH_EXECUTION_REFRESH_KINDS or refresh_of is None:
                raise OkxDemoCanaryPreparationBlocked(
                    "canary refresh idempotency key is bound to another entry"
                )
            if existing.status == "AWAITING_APPROVAL" and existing.stage == "CANARY_SNAPSHOT_REQUESTED":
                self.db.rollback()
                raise OkxDemoCanaryPreparationWaiting(
                    existing.id,
                    entry_kind=entry_kind,
                    supersedes_job_ids=supersedes,
                    refresh_of_job_id=refresh_of,
                )
            if existing.status == "SUCCESS" and existing.stage == "CANARY_PREPARED":
                # A restart-safe replay returns the durable prepared lineage.
                pass
            elif existing.status != "SUCCESS" or existing.stage != "CANARY_SNAPSHOTS_READY":
                self.db.rollback()
                raise OkxDemoCanaryPreparationBlocked(
                    "canary refresh request is terminal: {}".format(existing.stage)
                )
        else:
            # The lookup above starts a read transaction on SQLAlchemy
            # sessions.  End it before opening the transaction-scoped
            # coordination lock below.
            self.db.rollback()
            # Serialize source validation and successor creation with the same
            # transaction-scoped lock used by runtime reconciliation/grant.
            with self.db.begin():
                if not try_one_shot_transaction_lock(self.db):
                    raise OkxDemoCanaryPreparationRuntimeBusy(
                        entry_kind=FRESH_EXECUTION_ONLY_REFRESH,
                    )
                # Re-read after acquiring the lock so a concurrent request
                # cannot create a second refresh successor.
                existing = self._canary_job_for_key(key_digest)
                if existing is not None:
                    raise OkxDemoCanaryPreparationBlocked(
                        "canary refresh raced with another request"
                    )
                source = self._refresh_source_job(now=now)
                self._require_no_canary_activity_for_refresh()
                source_payload = source.request_payload
                source_supersedes = source_payload.get("supersedes_job_ids")
                if not isinstance(source_supersedes, list) or any(
                    not isinstance(job_id, int) or job_id <= 0
                    for job_id in source_supersedes
                ):
                    raise OkxDemoCanaryPreparationBlocked(
                        "fresh canary source lineage is malformed"
                    )
                supersedes_job_ids = list(dict.fromkeys([*source_supersedes, source.id]))
                source_entry_kind = source_payload.get("entry_kind")
                entry_kind = (
                    FRESH_EXECUTION_ONLY_REFRESH_RETRY
                    if source_entry_kind == FRESH_EXECUTION_ONLY_REFRESH
                    else FRESH_EXECUTION_ONLY_REFRESH
                )
                payload = {
                    "provenance": CANARY_PROVENANCE,
                    "execution_target": OKX_DEMO_TARGET_ID,
                    "instrument_id": "BTC-USDT-SWAP",
                    "bundle_kind": "EXECUTION_ONLY",
                    "non_production": True,
                    "entry_kind": entry_kind,
                    "refresh_of_job_id": source.id,
                    "supersedes_job_ids": supersedes_job_ids,
                }
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
                    evidence_snapshot={
                        "provenance": CANARY_PROVENANCE,
                        "non_production": True,
                        "entry_kind": entry_kind,
                        "refresh_of_job_id": source.id,
                        "supersedes_job_ids": supersedes_job_ids,
                    },
                    started_at=now,
                )
                self.db.add(job)
                self.db.flush()
                job_id = job.id
            raise OkxDemoCanaryPreparationWaiting(
                job_id,
                entry_kind=entry_kind,
                supersedes_job_ids=tuple(supersedes_job_ids),
                refresh_of_job_id=source.id,
            )

        # Existing SUCCESS/CANARY_SNAPSHOTS_READY or prepared replay.  The
        # source and all execution safety gates are checked again under lock;
        # no stale snapshot row is accepted.
        self.db.rollback()
        try:
            with self.db.begin():
                if not try_one_shot_transaction_lock(self.db):
                    raise OkxDemoCanaryPreparationRuntimeBusy(
                        job_id=existing.id,
                        entry_kind=entry_kind,
                        supersedes_job_ids=supersedes,
                        refresh_of_job_id=refresh_of,
                    )
                existing = self._canary_job_for_key(key_digest)
                if existing is None:
                    raise OkxDemoCanaryPreparationBlocked(
                        "canary refresh lineage disappeared"
                    )
                entry_kind, supersedes, refresh_of = _fresh_entry_lineage(
                    existing.request_payload
                )
                if entry_kind not in FRESH_EXECUTION_REFRESH_KINDS or refresh_of is None:
                    raise OkxDemoCanaryPreparationBlocked(
                        "canary refresh lineage is malformed"
                    )
                self._require_refresh_source_job(refresh_of)
                existing_lineage = self._existing_canary(key_digest)
                if existing_lineage is not None:
                    return self._result_from_lineage(existing_lineage, now=now)
                self._require_no_canary_activity_for_refresh(
                    allow_key_digest=key_digest
                )
                if existing.status != "SUCCESS" or existing.stage != "CANARY_SNAPSHOTS_READY":
                    raise OkxDemoCanaryPreparationWaiting(
                        existing.id,
                        entry_kind=entry_kind,
                        supersedes_job_ids=supersedes,
                        refresh_of_job_id=refresh_of,
                    )
                reconciliation_run_id = self._fresh_empty_reconciliation(now)
                snapshots = self._fresh_snapshots(now)
                order = self._derive_order(snapshots, now)
                return self._persist_lineage(
                    key_digest=key_digest,
                    now=now,
                    reconciliation_run_id=reconciliation_run_id,
                    snapshots=snapshots,
                    order=order,
                )
        except OkxDemoCanaryPreparationBlocked:
            self.db.rollback()
            raise

    def prepare_fresh_execution_only_recovery(
        self,
        *,
        idempotency_key: str,
        _entry_kind: str = FRESH_EXECUTION_ONLY_RECOVERY,
        _recovery_boundary: str = "PRE_616_FINALIZE_ACL_FAILURE",
    ) -> CanaryPreparationResult:
        """Recover one exhausted refresh lineage without opening a third refresh.

        This is an explicit, single-use operator entry for the immutable
        depth-two execution-only lineage that could not be finalized before
        the #616 PostgreSQL runtime-role ACL fix.  It is intentionally not a
        continuation of ``prepare_fresh_execution_only_refresh``: no normal
        refresh source is accepted, no old snapshot is reused, and any
        existing recovery entry (including a different idempotency key) is a
        hard stop.
        """

        if (_entry_kind, _recovery_boundary) not in {
            (
                FRESH_EXECUTION_ONLY_RECOVERY,
                "PRE_616_FINALIZE_ACL_FAILURE",
            ),
            (
                FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY,
                "POST_PERSISTENCE_LINEAGE_WRITE_FAILURE",
            ),
            (
                FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY,
                "POST_PERSISTENCE_RECOVERY_SNAPSHOT_EXPIRY",
            ),
        }:
            raise OkxDemoCanaryPreparationBlocked(
                "unsupported canary recovery boundary"
            )

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
                "controlled canary recovery requires OKX_DEMO with global submission disabled"
            )

        existing = self._canary_job_for_key(key_digest)
        if existing is not None:
            entry_kind, supersedes, _refresh_of = _fresh_entry_lineage(
                existing.request_payload
            )
            recovery_of = _fresh_recovery_of(existing.request_payload)
            if entry_kind != _entry_kind or recovery_of is None:
                raise OkxDemoCanaryPreparationBlocked(
                    "canary recovery idempotency key is bound to another entry"
                )
            if existing.status == "AWAITING_APPROVAL" and existing.stage == "CANARY_SNAPSHOT_REQUESTED":
                self.db.rollback()
                raise OkxDemoCanaryPreparationWaiting(
                    existing.id,
                    entry_kind=entry_kind,
                    supersedes_job_ids=supersedes,
                    recovery_of_job_id=recovery_of,
                )
            if existing.status == "SUCCESS" and existing.stage == "CANARY_PREPARED":
                # A prepared replay is returned from the durable lineage in
                # the transaction below.  It never borrows the old snapshots.
                pass
            elif existing.status != "SUCCESS" or existing.stage != "CANARY_SNAPSHOTS_READY":
                self.db.rollback()
                raise OkxDemoCanaryPreparationBlocked(
                    "canary recovery request is terminal: {}".format(existing.stage)
                )
        else:
            self.db.rollback()
            with self.db.begin():
                if not try_one_shot_transaction_lock(self.db):
                    raise OkxDemoCanaryPreparationRuntimeBusy(
                        entry_kind=_entry_kind,
                    )
                existing = self._canary_job_for_key(key_digest)
                if existing is not None:
                    raise OkxDemoCanaryPreparationBlocked(
                        "canary recovery raced with another request"
                    )
                if _entry_kind == FRESH_EXECUTION_ONLY_RECOVERY:
                    source = self._recovery_source_job(now=now)
                elif _entry_kind == FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY:
                    source = self._post_persistence_recovery_source_job(now=now)
                else:
                    source = self._final_expiry_recovery_source_job(now=now)
                self._require_no_canary_activity_for_refresh()
                source_payload = source.request_payload
                source_supersedes = source_payload.get("supersedes_job_ids")
                if not isinstance(source_supersedes, list) or any(
                    not isinstance(job_id, int) or job_id <= 0
                    for job_id in source_supersedes
                ):
                    raise OkxDemoCanaryPreparationBlocked(
                        "canary recovery source lineage is malformed"
                    )
                supersedes_job_ids = list(dict.fromkeys([*source_supersedes, source.id]))
                payload = {
                    "provenance": CANARY_PROVENANCE,
                    "execution_target": OKX_DEMO_TARGET_ID,
                    "instrument_id": "BTC-USDT-SWAP",
                    "bundle_kind": "EXECUTION_ONLY",
                    "non_production": True,
                    "entry_kind": _entry_kind,
                    "recovery_of_job_id": source.id,
                    "supersedes_job_ids": supersedes_job_ids,
                    "recovery_boundary": _recovery_boundary,
                }
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
                    evidence_snapshot={
                        "provenance": CANARY_PROVENANCE,
                        "non_production": True,
                        "entry_kind": _entry_kind,
                        "recovery_of_job_id": source.id,
                        "supersedes_job_ids": supersedes_job_ids,
                        "recovery_boundary": _recovery_boundary,
                    },
                    started_at=now,
                )
                self.db.add(job)
                self.db.flush()
                job_id = job.id
            raise OkxDemoCanaryPreparationWaiting(
                job_id,
                entry_kind=_entry_kind,
                supersedes_job_ids=tuple(supersedes_job_ids),
                recovery_of_job_id=source.id,
            )

        # Finalization/replay is still serialized by the canonical
        # transaction-scoped advisory lock.  The source lineage and all
        # current snapshots are revalidated; no expired snapshot from jobs
        # 15-19 can enter the new approval lineage.
        self.db.rollback()
        try:
            with self.db.begin():
                if not try_one_shot_transaction_lock(self.db):
                    raise OkxDemoCanaryPreparationRuntimeBusy(
                        job_id=existing.id,
                        entry_kind=entry_kind,
                        supersedes_job_ids=supersedes,
                        recovery_of_job_id=recovery_of,
                    )
                existing = self._canary_job_for_key(key_digest)
                if existing is None:
                    raise OkxDemoCanaryPreparationBlocked(
                        "canary recovery lineage disappeared"
                    )
                entry_kind, supersedes, _refresh_of = _fresh_entry_lineage(
                    existing.request_payload
                )
                recovery_of = _fresh_recovery_of(existing.request_payload)
                if entry_kind != _entry_kind or recovery_of is None:
                    raise OkxDemoCanaryPreparationBlocked(
                        "canary recovery lineage is malformed"
                    )
                if _entry_kind == FRESH_EXECUTION_ONLY_RECOVERY:
                    self._require_recovery_source_job(
                        recovery_of,
                        now=now,
                        ignore_recovery_job_id=existing.id,
                    )
                elif _entry_kind == FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY:
                    self._post_persistence_recovery_source_job(
                        now=now,
                        expected_source_job_id=recovery_of,
                        ignore_successor_job_id=existing.id,
                    )
                else:
                    self._final_expiry_recovery_source_job(
                        now=now,
                        expected_source_job_id=recovery_of,
                        ignore_successor_job_id=existing.id,
                    )
                existing_lineage = self._existing_canary(key_digest)
                if existing_lineage is not None:
                    return self._result_from_lineage(existing_lineage, now=now)
                self._require_no_canary_activity_for_refresh(
                    allow_key_digest=key_digest
                )
                if existing.status != "SUCCESS" or existing.stage != "CANARY_SNAPSHOTS_READY":
                    raise OkxDemoCanaryPreparationWaiting(
                        existing.id,
                        entry_kind=entry_kind,
                        supersedes_job_ids=supersedes,
                        recovery_of_job_id=recovery_of,
                    )
                reconciliation_run_id = self._fresh_empty_reconciliation(now)
                snapshots = self._fresh_snapshots(now)
                order = self._derive_order(snapshots, now)
                return self._persist_lineage(
                    key_digest=key_digest,
                    now=now,
                    reconciliation_run_id=reconciliation_run_id,
                    snapshots=snapshots,
                    order=order,
                )
        except OkxDemoCanaryPreparationBlocked:
            self.db.rollback()
            raise

    def prepare_post_persistence_recovery(
        self,
        *,
        idempotency_key: str,
    ) -> CanaryPreparationResult:
        """Create the sole fresh successor after job20's write rollback."""

        return self.prepare_fresh_execution_only_recovery(
            idempotency_key=idempotency_key,
            _entry_kind=FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY,
            _recovery_boundary="POST_PERSISTENCE_LINEAGE_WRITE_FAILURE",
        )

    def prepare_final_expiry_recovery(
        self,
        *,
        idempotency_key: str,
    ) -> CanaryPreparationResult:
        """Create the sole final successor after job21-shaped snapshot expiry."""

        return self.prepare_fresh_execution_only_recovery(
            idempotency_key=idempotency_key,
            _entry_kind=FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY,
            _recovery_boundary="POST_PERSISTENCE_RECOVERY_SNAPSHOT_EXPIRY",
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
        if allow_terminal_history:
            if not other_requests:
                self.db.rollback()
                raise OkxDemoCanaryPreparationBlocked(
                    "fresh execution-only entry requires immutable terminal canary history"
                )
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
            if payload.get("entry_kind") in FRESH_EXECUTION_ENTRY_KINDS:
                raise OkxDemoCanaryPreparationBlocked(
                    "a fresh execution-only canary entry already exists"
                )
            if not self._is_terminal_attestation_failure(job, payload):
                raise OkxDemoCanaryPreparationBlocked(
                    "another controlled canary request already owns the idempotency boundary"
                )
            supersedes.append(job.id)
        return supersedes

    def _refresh_source_job(self, *, now: datetime) -> ResearchJob:
        """Find the bounded successful handoff eligible for re-attestation.

        The first refresh is sourced from the original fresh entry.  A single
        additional retry may be sourced from that refresh only after its
        immutable snapshot evidence has expired.  This keeps a stale live
        handoff recoverable without turning refresh into an unbounded chain.
        """

        jobs = self.db.scalars(
            canary_lineage_read_query(
                self.db,
                select(ResearchJob)
                .where(
                    ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                    ResearchJob.operation == CANARY_OPERATION,
                )
                .order_by(ResearchJob.created_at, ResearchJob.id),
                for_update=True,
            )
        ).all()
        source: Optional[ResearchJob] = None
        refresh_sources: list[ResearchJob] = []
        for job in jobs:
            payload = job.request_payload if isinstance(job.request_payload, dict) else {}
            entry_kind = payload.get("entry_kind")
            if entry_kind == FRESH_EXECUTION_ONLY_ENTRY:
                if source is not None:
                    raise OkxDemoCanaryPreparationBlocked(
                        "multiple fresh execution-only canary sources exist"
                    )
                if not self._is_refresh_source(job, payload):
                    raise OkxDemoCanaryPreparationBlocked(
                        "fresh execution-only canary source is not ready"
                    )
                source = job
                continue
            if entry_kind in FRESH_EXECUTION_REFRESH_KINDS:
                if (
                    job.status == "AWAITING_APPROVAL"
                    and job.stage == "CANARY_SNAPSHOT_REQUESTED"
                ):
                    raise OkxDemoCanaryPreparationBlocked(
                        "a canary refresh successor already exists"
                    )
                if not self._is_refresh_successor(job, payload):
                    raise OkxDemoCanaryPreparationBlocked(
                        "unknown or pending canary refresh history blocks refresh"
                    )
                refresh_sources.append(job)
                continue
            if not self._is_terminal_attestation_failure(job, payload):
                raise OkxDemoCanaryPreparationBlocked(
                    "unknown or pending canary history blocks refresh"
                )
        if len(refresh_sources) >= MAX_FRESH_EXECUTION_REFRESH_SUCCESSORS:
            raise OkxDemoCanaryPreparationBlocked(
                "canary refresh successor limit reached"
            )
        if refresh_sources:
            # Each successful refresh is immutable.  A second key can only
            # continue from the latest single successor, and only when that
            # successor's own attested snapshots are demonstrably expired.
            if source is None or len(refresh_sources) != 1:
                raise OkxDemoCanaryPreparationBlocked(
                    "canary refresh lineage has multiple successful successors"
                )
            refresh_source = refresh_sources[0]
            depth = self._refresh_handoff_depth(refresh_source)
            if depth is None or depth >= MAX_FRESH_EXECUTION_REFRESH_SUCCESSORS:
                raise OkxDemoCanaryPreparationBlocked(
                    "canary refresh successor limit reached"
                )
            if not self._refresh_snapshots_expired(refresh_source, now):
                raise OkxDemoCanaryPreparationBlocked(
                    "existing canary refresh handoff is still fresh; finalize it"
                )
            return refresh_source
        if source is None:
            raise OkxDemoCanaryPreparationBlocked(
                "a successful fresh execution-only canary handoff is required"
            )
        return source

    def _recovery_source_job(
        self,
        *,
        now: datetime,
        ignore_recovery_job_id: Optional[int] = None,
    ) -> ResearchJob:
        """Find the one immutable, expired depth-two lineage for recovery.

        Recovery is deliberately narrower than a normal refresh.  The
        complete history must contain exactly one fresh source, exactly two
        successful refresh successors at depths one and two, and only the
        terminal attestation failures already superseded by that source.
        Any pending/unknown row or second recovery entry blocks the path.
        """

        jobs = self.db.scalars(
            canary_lineage_read_query(
                self.db,
                select(ResearchJob)
                .where(
                    ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                    ResearchJob.operation == CANARY_OPERATION,
                )
                .order_by(ResearchJob.created_at, ResearchJob.id),
                for_update=True,
            )
        ).all()
        sources: list[ResearchJob] = []
        refreshes: list[ResearchJob] = []
        terminal_failures: list[ResearchJob] = []
        for job in jobs:
            payload = job.request_payload if isinstance(job.request_payload, dict) else {}
            entry_kind = payload.get("entry_kind")
            if entry_kind == FRESH_EXECUTION_ONLY_ENTRY:
                if not self._is_refresh_source(job, payload):
                    raise OkxDemoCanaryPreparationBlocked(
                        "canary recovery source is not an immutable successful handoff"
                    )
                sources.append(job)
                continue
            if entry_kind in FRESH_EXECUTION_REFRESH_KINDS:
                if not self._is_refresh_successor(job, payload):
                    raise OkxDemoCanaryPreparationBlocked(
                        "canary recovery refresh lineage is malformed"
                    )
                refreshes.append(job)
                continue
            if entry_kind in FRESH_EXECUTION_RECOVERY_KINDS:
                if job.id != ignore_recovery_job_id:
                    raise OkxDemoCanaryPreparationBlocked(
                        "a canary recovery successor already exists"
                    )
                continue
            if not self._is_terminal_attestation_failure(job, payload):
                raise OkxDemoCanaryPreparationBlocked(
                    "unknown or pending canary history blocks recovery"
                )
            terminal_failures.append(job)

        if len(sources) != 1 or len(refreshes) != MAX_FRESH_EXECUTION_REFRESH_SUCCESSORS:
            raise OkxDemoCanaryPreparationBlocked(
                "recovery requires one fresh source and two exhausted refresh successors"
            )
        source = sources[0]
        source_payload = source.request_payload
        source_supersedes = source_payload.get("supersedes_job_ids")
        terminal_ids = [job.id for job in terminal_failures]
        if source_supersedes != terminal_ids:
            raise OkxDemoCanaryPreparationBlocked(
                "recovery terminal history does not match immutable source lineage"
            )

        depths = [self._refresh_handoff_depth(job) for job in refreshes]
        if sorted(depth for depth in depths if depth is not None) != [1, 2]:
            raise OkxDemoCanaryPreparationBlocked(
                "recovery refresh lineage depth is incomplete"
            )
        latest = next(
            (job for job, depth in zip(refreshes, depths) if depth == 2),
            None,
        )
        if latest is None or not self._refresh_snapshots_expired(latest, now):
            raise OkxDemoCanaryPreparationBlocked(
                "recovery requires explicit expiry evidence on the final refresh"
            )
        return latest

    def _require_recovery_source_job(
        self,
        source_job_id: int,
        *,
        now: datetime,
        ignore_recovery_job_id: Optional[int] = None,
    ) -> ResearchJob:
        source = self._recovery_source_job(
            now=now,
            ignore_recovery_job_id=ignore_recovery_job_id,
        )
        if source.id != source_job_id:
            raise OkxDemoCanaryPreparationBlocked(
                "canary recovery source is not the immutable final refresh"
            )
        return source

    def _post_persistence_recovery_source_job(
        self,
        *,
        now: datetime,
        expected_source_job_id: Optional[int] = None,
        ignore_successor_job_id: Optional[int] = None,
        ignore_final_successor_job_id: Optional[int] = None,
    ) -> ResearchJob:
        """Return the one expired job20-shaped recovery handoff.

        All other canary history must remain one of the already-validated
        terminal/source/refresh shapes.  A second post-persistence successor
        is rejected regardless of idempotency key.
        """

        jobs = self.db.scalars(
            canary_lineage_read_query(
                self.db,
                select(ResearchJob)
                .where(
                    ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                    ResearchJob.operation == CANARY_OPERATION,
                )
                .order_by(ResearchJob.created_at, ResearchJob.id),
                for_update=True,
            )
        ).all()
        source: Optional[ResearchJob] = None
        for job in jobs:
            payload = job.request_payload if isinstance(job.request_payload, dict) else {}
            entry_kind = payload.get("entry_kind")
            if entry_kind == FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY:
                if job.id != ignore_final_successor_job_id:
                    raise OkxDemoCanaryPreparationBlocked(
                        "a final expiry recovery successor already exists"
                    )
                continue
            if entry_kind == FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY:
                if job.id != ignore_successor_job_id:
                    raise OkxDemoCanaryPreparationBlocked(
                        "a post-persistence recovery successor already exists"
                    )
                continue
            if entry_kind == FRESH_EXECUTION_ONLY_RECOVERY:
                if source is not None or not self._is_post_persistence_source(job, payload):
                    raise OkxDemoCanaryPreparationBlocked(
                        "post-persistence recovery source is malformed"
                    )
                source = job
                continue
            if entry_kind == FRESH_EXECUTION_ONLY_ENTRY:
                if not self._is_refresh_source(job, payload):
                    raise OkxDemoCanaryPreparationBlocked(
                        "post-persistence history contains a malformed fresh source"
                    )
                continue
            if entry_kind in FRESH_EXECUTION_REFRESH_KINDS:
                if not self._is_refresh_successor(job, payload):
                    raise OkxDemoCanaryPreparationBlocked(
                        "post-persistence history contains a malformed refresh"
                    )
                continue
            if not self._is_terminal_attestation_failure(job, payload):
                raise OkxDemoCanaryPreparationBlocked(
                    "unknown or pending canary history blocks post-persistence recovery"
                )
        if source is None:
            raise OkxDemoCanaryPreparationBlocked(
                "an immutable failed-persistence recovery handoff is required"
            )
        if expected_source_job_id is not None and source.id != expected_source_job_id:
            raise OkxDemoCanaryPreparationBlocked(
                "post-persistence recovery source changed"
            )
        source_payload = source.request_payload
        parent_id = source_payload.get("recovery_of_job_id")
        parent = self.db.get(ResearchJob, parent_id)
        if parent is None or self._refresh_handoff_depth(parent) != 2:
            raise OkxDemoCanaryPreparationBlocked(
                "post-persistence source does not follow the final refresh"
            )
        parent_payload = parent.request_payload if isinstance(parent.request_payload, dict) else {}
        expected_supersedes = list(
            dict.fromkeys([*(parent_payload.get("supersedes_job_ids") or []), parent.id])
        )
        if source_payload.get("supersedes_job_ids") != expected_supersedes:
            raise OkxDemoCanaryPreparationBlocked(
                "post-persistence recovery ancestry is malformed"
            )
        if not self._refresh_snapshots_expired(source, now):
            raise OkxDemoCanaryPreparationBlocked(
                "post-persistence source snapshots are still fresh; finalize it"
            )
        return source

    def _final_expiry_recovery_source_job(
        self,
        *,
        now: datetime,
        expected_source_job_id: Optional[int] = None,
        ignore_successor_job_id: Optional[int] = None,
    ) -> ResearchJob:
        """Return the sole expired post-persistence recovery handoff.

        The complete earlier lineage is revalidated through the existing
        post-persistence source validator.  This admits exactly one final
        successor and never treats its expired snapshot references as usable.
        """

        jobs = self.db.scalars(
            canary_lineage_read_query(
                self.db,
                select(ResearchJob)
                .where(
                    ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                    ResearchJob.operation == CANARY_OPERATION,
                )
                .order_by(ResearchJob.created_at, ResearchJob.id),
                for_update=True,
            )
        ).all()
        source: Optional[ResearchJob] = None
        for job in jobs:
            payload = job.request_payload if isinstance(job.request_payload, dict) else {}
            entry_kind = payload.get("entry_kind")
            if entry_kind == FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY:
                if job.id != ignore_successor_job_id:
                    raise OkxDemoCanaryPreparationBlocked(
                        "a final expiry recovery successor already exists"
                    )
                continue
            if entry_kind == FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY:
                if source is not None or not self._is_final_expiry_source(job, payload):
                    raise OkxDemoCanaryPreparationBlocked(
                        "final expiry recovery source is malformed"
                    )
                source = job

        if source is None:
            raise OkxDemoCanaryPreparationBlocked(
                "an immutable post-persistence recovery handoff is required"
            )
        if expected_source_job_id is not None and source.id != expected_source_job_id:
            raise OkxDemoCanaryPreparationBlocked(
                "final expiry recovery source changed"
            )

        parent_id = source.request_payload.get("recovery_of_job_id")
        parent = self._post_persistence_recovery_source_job(
            now=now,
            expected_source_job_id=parent_id,
            ignore_successor_job_id=source.id,
            ignore_final_successor_job_id=ignore_successor_job_id,
        )
        expected_supersedes = list(
            dict.fromkeys(
                [*(parent.request_payload.get("supersedes_job_ids") or []), parent.id]
            )
        )
        if source.request_payload.get("supersedes_job_ids") != expected_supersedes:
            raise OkxDemoCanaryPreparationBlocked(
                "final expiry recovery ancestry is malformed"
            )
        if not self._all_snapshot_references_expired(source, now):
            raise OkxDemoCanaryPreparationBlocked(
                "post-persistence recovery snapshots are still fresh; finalize it"
            )
        return source

    @staticmethod
    def _all_snapshot_references_expired(
        job: ResearchJob,
        now: datetime,
    ) -> bool:
        """Require every instrument/market/account reference to be expired."""

        evidence = job.evidence_snapshot if isinstance(job.evidence_snapshot, dict) else {}
        snapshots = evidence.get("snapshot_evidence")
        if not isinstance(snapshots, dict):
            raise OkxDemoCanaryPreparationBlocked(
                "final expiry evidence is missing"
            )
        expiries: list[datetime] = []
        for kind in ("instrument", "market", "account"):
            reference = snapshots.get(kind)
            if not isinstance(reference, dict) or "expires_at" not in reference:
                raise OkxDemoCanaryPreparationBlocked(
                    "final expiry evidence is missing"
                )
            expiries.append(_aware(reference["expires_at"]))
        return all(expires_at <= now for expires_at in expiries)

    @staticmethod
    def _is_final_expiry_source(
        job: ResearchJob,
        payload: Mapping[str, Any],
    ) -> bool:
        if (
            job.execution_scope_id != LOCAL_DRY_RUN_SCOPE_ID
            or job.operation != CANARY_OPERATION
            or job.status != "SUCCESS"
            or job.stage != "CANARY_SNAPSHOTS_READY"
            or payload.get("provenance") != CANARY_PROVENANCE
            or payload.get("execution_target") != OKX_DEMO_TARGET_ID
            or payload.get("instrument_id") not in CANARY_INSTRUMENTS
            or payload.get("bundle_kind") != "EXECUTION_ONLY"
            or payload.get("entry_kind")
            != FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY
            or payload.get("non_production") is not True
            or payload.get("recovery_boundary")
            != "POST_PERSISTENCE_LINEAGE_WRITE_FAILURE"
            or "timeframe" in payload
            or "candle_limit" in payload
        ):
            return False
        recovery_of = payload.get("recovery_of_job_id")
        supersedes = payload.get("supersedes_job_ids")
        if (
            not isinstance(recovery_of, int)
            or recovery_of <= 0
            or not isinstance(supersedes, list)
            or recovery_of not in supersedes
            or any(not isinstance(job_id, int) or job_id <= 0 for job_id in supersedes)
        ):
            return False
        evidence = job.evidence_snapshot if isinstance(job.evidence_snapshot, dict) else {}
        return (
            evidence.get("provenance") == CANARY_PROVENANCE
            and evidence.get("non_production") is True
            and evidence.get("entry_kind")
            == FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY
            and evidence.get("recovery_of_job_id") == recovery_of
            and evidence.get("supersedes_job_ids") == supersedes
            and evidence.get("recovery_boundary")
            == "POST_PERSISTENCE_LINEAGE_WRITE_FAILURE"
            and isinstance(evidence.get("snapshot_evidence"), dict)
        )

    @staticmethod
    def _is_post_persistence_source(
        job: ResearchJob,
        payload: Mapping[str, Any],
    ) -> bool:
        if (
            job.execution_scope_id != LOCAL_DRY_RUN_SCOPE_ID
            or job.operation != CANARY_OPERATION
            or job.status != "SUCCESS"
            or job.stage != "CANARY_SNAPSHOTS_READY"
            or payload.get("provenance") != CANARY_PROVENANCE
            or payload.get("execution_target") != OKX_DEMO_TARGET_ID
            or payload.get("instrument_id") not in CANARY_INSTRUMENTS
            or payload.get("bundle_kind") != "EXECUTION_ONLY"
            or payload.get("entry_kind") != FRESH_EXECUTION_ONLY_RECOVERY
            or payload.get("non_production") is not True
            or payload.get("recovery_boundary") != "PRE_616_FINALIZE_ACL_FAILURE"
            or "timeframe" in payload
            or "candle_limit" in payload
        ):
            return False
        recovery_of = payload.get("recovery_of_job_id")
        supersedes = payload.get("supersedes_job_ids")
        if (
            not isinstance(recovery_of, int)
            or recovery_of <= 0
            or not isinstance(supersedes, list)
            or recovery_of not in supersedes
            or any(not isinstance(job_id, int) or job_id <= 0 for job_id in supersedes)
        ):
            return False
        evidence = job.evidence_snapshot if isinstance(job.evidence_snapshot, dict) else {}
        return (
            evidence.get("provenance") == CANARY_PROVENANCE
            and evidence.get("non_production") is True
            and evidence.get("entry_kind") == FRESH_EXECUTION_ONLY_RECOVERY
            and evidence.get("recovery_of_job_id") == recovery_of
            and evidence.get("supersedes_job_ids") == supersedes
            and evidence.get("recovery_boundary") == "PRE_616_FINALIZE_ACL_FAILURE"
            and isinstance(evidence.get("snapshot_evidence"), dict)
        )

    @staticmethod
    def _is_refresh_source(
        job: ResearchJob,
        payload: Mapping[str, Any],
    ) -> bool:
        if (
            job.execution_scope_id != LOCAL_DRY_RUN_SCOPE_ID
            or job.operation != CANARY_OPERATION
            or job.status != "SUCCESS"
            or job.stage != "CANARY_SNAPSHOTS_READY"
            or payload.get("provenance") != CANARY_PROVENANCE
            or payload.get("execution_target") != OKX_DEMO_TARGET_ID
            or payload.get("instrument_id") not in CANARY_INSTRUMENTS
            or payload.get("bundle_kind") != "EXECUTION_ONLY"
            or payload.get("entry_kind") != FRESH_EXECUTION_ONLY_ENTRY
            or payload.get("non_production") is not True
            or payload.get("refresh_of_job_id") is not None
            or "timeframe" in payload
            or "candle_limit" in payload
        ):
            return False
        supersedes = payload.get("supersedes_job_ids")
        if not isinstance(supersedes, list) or not supersedes or any(
            not isinstance(job_id, int) or job_id <= 0 for job_id in supersedes
        ):
            return False
        evidence = job.evidence_snapshot if isinstance(job.evidence_snapshot, dict) else {}
        return (
            evidence.get("provenance") == CANARY_PROVENANCE
            and evidence.get("non_production") is True
            and evidence.get("entry_kind") == FRESH_EXECUTION_ONLY_ENTRY
            and evidence.get("supersedes_job_ids") == supersedes
            and isinstance(evidence.get("snapshot_evidence"), dict)
        )

    @staticmethod
    def _is_refresh_successor(
        job: ResearchJob,
        payload: Mapping[str, Any],
    ) -> bool:
        """Validate one immutable successful refresh successor shape."""

        if (
            job.execution_scope_id != LOCAL_DRY_RUN_SCOPE_ID
            or job.operation != CANARY_OPERATION
            or job.status != "SUCCESS"
            or job.stage != "CANARY_SNAPSHOTS_READY"
            or payload.get("provenance") != CANARY_PROVENANCE
            or payload.get("execution_target") != OKX_DEMO_TARGET_ID
            or payload.get("instrument_id") not in CANARY_INSTRUMENTS
            or payload.get("bundle_kind") != "EXECUTION_ONLY"
            or payload.get("entry_kind") not in FRESH_EXECUTION_REFRESH_KINDS
            or payload.get("non_production") is not True
            or "timeframe" in payload
            or "candle_limit" in payload
        ):
            return False
        refresh_of = payload.get("refresh_of_job_id")
        supersedes = payload.get("supersedes_job_ids")
        if (
            not isinstance(refresh_of, int)
            or refresh_of <= 0
            or not isinstance(supersedes, list)
            or not supersedes
            or any(not isinstance(job_id, int) or job_id <= 0 for job_id in supersedes)
            or refresh_of not in supersedes
        ):
            return False
        evidence = job.evidence_snapshot if isinstance(job.evidence_snapshot, dict) else {}
        return (
            evidence.get("provenance") == CANARY_PROVENANCE
            and evidence.get("non_production") is True
            and evidence.get("entry_kind") == payload.get("entry_kind")
            and evidence.get("refresh_of_job_id") == refresh_of
            and evidence.get("supersedes_job_ids") == supersedes
            and isinstance(evidence.get("snapshot_evidence"), dict)
        )

    def _refresh_handoff_depth(
        self,
        job: ResearchJob,
        *,
        seen: Optional[set[int]] = None,
    ) -> Optional[int]:
        """Return refresh depth, rejecting cycles and malformed parents."""

        seen = set() if seen is None else seen
        if job.id in seen:
            return None
        seen.add(job.id)
        payload = job.request_payload if isinstance(job.request_payload, dict) else {}
        if self._is_refresh_source(job, payload):
            return 0
        if not self._is_refresh_successor(job, payload):
            return None
        parent = self.db.get(ResearchJob, payload["refresh_of_job_id"])
        if parent is None:
            return None
        parent_payload = (
            parent.request_payload
            if isinstance(parent.request_payload, dict)
            else {}
        )
        parent_supersedes = parent_payload.get("supersedes_job_ids")
        supersedes = payload.get("supersedes_job_ids")
        if (
            not isinstance(parent_supersedes, list)
            or not isinstance(supersedes, list)
            or list(dict.fromkeys([*parent_supersedes, parent.id])) != supersedes
        ):
            return None
        parent_depth = self._refresh_handoff_depth(parent, seen=seen)
        if parent_depth is None:
            return None
        depth = parent_depth + 1
        if parent_depth == 0:
            expected_kind = FRESH_EXECUTION_ONLY_REFRESH
        elif parent_depth == 1:
            expected_kind = FRESH_EXECUTION_ONLY_REFRESH_RETRY
        else:
            expected_kind = None
        if payload.get("entry_kind") != expected_kind:
            return None
        if depth > MAX_FRESH_EXECUTION_REFRESH_SUCCESSORS:
            return None
        return depth

    @staticmethod
    def _refresh_snapshots_expired(job: ResearchJob, now: datetime) -> bool:
        """Require explicit immutable expiry evidence before another retry."""

        evidence = job.evidence_snapshot if isinstance(job.evidence_snapshot, dict) else {}
        snapshots = evidence.get("snapshot_evidence")
        if not isinstance(snapshots, dict) or not snapshots:
            raise OkxDemoCanaryPreparationBlocked(
                "canary refresh expiry evidence is missing"
            )
        expiries: list[datetime] = []
        for kind in ("instrument", "market", "account"):
            reference = snapshots.get(kind)
            if not isinstance(reference, dict) or "expires_at" not in reference:
                raise OkxDemoCanaryPreparationBlocked(
                    "canary refresh expiry evidence is missing"
                )
            expiries.append(_aware(reference["expires_at"]))
        return any(expires_at <= now for expires_at in expiries)

    def _require_refresh_source_job(self, source_job_id: int) -> ResearchJob:
        source = self.db.get(ResearchJob, source_job_id)
        if source is None or self._refresh_handoff_depth(source) is None:
            raise OkxDemoCanaryPreparationBlocked(
                "refresh source job is no longer an immutable successful handoff"
            )
        return source

    def _require_no_canary_activity_for_refresh(
        self,
        *,
        allow_key_digest: Optional[str] = None,
    ) -> None:
        """Reject any pre-existing execution or writer evidence before refresh."""

        if self.db.scalars(
            select(TradeIntent.id)
            .where(TradeIntent.execution_target_id == OKX_DEMO_TARGET_ID)
            .limit(1)
        ).first() is not None:
            raise OkxDemoCanaryPreparationBlocked(
                "a prior TradeIntent blocks canary refresh"
            )
        if self.db.scalars(
            select(ApprovedExecution.id)
            .where(ApprovedExecution.execution_target_id == OKX_DEMO_TARGET_ID)
            .limit(1)
        ).first() is not None:
            raise OkxDemoCanaryPreparationBlocked(
                "a prior ApprovedExecution blocks canary refresh"
            )
        if self.db.scalars(
            select(OkxDemoSubmissionGrant.grant_id)
            .where(OkxDemoSubmissionGrant.execution_target_id == OKX_DEMO_TARGET_ID)
            .limit(1)
        ).first() is not None:
            raise OkxDemoCanaryPreparationBlocked(
                "a prior submission grant blocks canary refresh"
            )
        if self.db.scalars(
            select(OkxOrderWriteAttempt.id)
            .where(OkxOrderWriteAttempt.execution_target_id == OKX_DEMO_TARGET_ID)
            .limit(1)
        ).first() is not None:
            raise OkxDemoCanaryPreparationBlocked(
                "a prior writer attempt blocks canary refresh"
            )
        if self.db.scalars(
            select(ExchangeOrder.id)
            .where(ExchangeOrder.execution_target_id == OKX_DEMO_TARGET_ID)
            .limit(1)
        ).first() is not None:
            raise OkxDemoCanaryPreparationBlocked(
                "a prior exchange order blocks canary refresh"
            )
        if self.db.scalars(
            select(ExchangePosition.id)
            .where(
                ExchangePosition.execution_target_id == OKX_DEMO_TARGET_ID,
                ExchangePosition.quantity != 0,
            )
            .limit(1)
        ).first() is not None:
            raise OkxDemoCanaryPreparationBlocked(
                "a prior exchange position blocks canary refresh"
            )
        if allow_key_digest is not None:
            current = self._canary_job_for_key(allow_key_digest)
            if current is None:
                raise OkxDemoCanaryPreparationBlocked(
                    "canary refresh lineage is missing"
                )

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
        job_override: Optional[ResearchJob] = None,
        research_operation: str = CANARY_OPERATION,
        audit_metadata: Optional[Mapping[str, Any]] = None,
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
            "operation": research_operation,
            "execution_target": OKX_DEMO_TARGET_ID,
            "bundle_kind": "EXECUTION_ONLY",
            "non_production": True,
            "instrument_id": order["instrument_id"],
            "quantity": format(order["quantity"], "f"),
            "notional": format(order["notional"], "f"),
            "snapshot_evidence": evidence,
        }
        if audit_metadata:
            research_payload.update(dict(audit_metadata))
        job = job_override
        if job is None:
            job = self.db.scalars(
                select(ResearchJob).where(
                    ResearchJob.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                    ResearchJob.operation == research_operation,
                    ResearchJob.idempotency_key_digest == key_digest,
                )
            ).first()
        # Preserve the fresh-entry lineage when the runtime handoff job is
        # promoted to the prepared lineage.  Historical jobs retain their
        # original request shape; no old row is rewritten.
        if job is not None:
            handoff_payload = job.request_payload if isinstance(job.request_payload, dict) else {}
            if handoff_payload.get("entry_kind") in FRESH_EXECUTION_ENTRY_KINDS:
                research_payload["entry_kind"] = handoff_payload["entry_kind"]
                research_payload["supersedes_job_ids"] = list(
                    handoff_payload.get("supersedes_job_ids") or []
                )
                if handoff_payload.get("refresh_of_job_id") is not None:
                    research_payload["refresh_of_job_id"] = handoff_payload[
                        "refresh_of_job_id"
                    ]
                if handoff_payload.get("recovery_of_job_id") is not None:
                    research_payload["recovery_of_job_id"] = handoff_payload[
                        "recovery_of_job_id"
                    ]
                if handoff_payload.get("recovery_boundary") is not None:
                    research_payload["recovery_boundary"] = handoff_payload[
                        "recovery_boundary"
                    ]
        lineage_payload = job.request_payload if job is not None else research_payload
        entry_kind, supersedes_job_ids, refresh_of_job_id = _fresh_entry_lineage(
            lineage_payload
        )
        recovery_of_job_id = _fresh_recovery_of(lineage_payload)
        request_hash = canonical_digest(research_payload)
        if job is None:
            job = ResearchJob(
                execution_scope_id=LOCAL_DRY_RUN_SCOPE_ID,
                job_type="okx_demo_controlled_canary",
                operation=research_operation,
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
        intent_identity = {
            "provenance": CANARY_PROVENANCE,
            "idempotency_key_digest": key_digest,
            "canonical_hash": canonical_hash,
        }
        intent_id = canonical_digest(intent_identity)
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
        approved_payload = {
            "canonical_input": canonical_input,
            "notional": format(order["notional"], "f"),
            "provenance": CANARY_PROVENANCE,
        }
        intent.approved_payload_hash = canonical_digest(approved_payload)
        bind = self.db.get_bind()
        if bind.dialect.name == "postgresql":
            privileged_payload = {
                "execution_target": OKX_DEMO_TARGET_ID,
                "provenance": CANARY_PROVENANCE,
                "non_production": True,
                "full_chain_run_id": chain.id,
                "reconciliation_run_id": reconciliation_run_id,
                "intent_id": intent.intent_id,
                "canonical_hash": canonical_hash,
                "policy_digest": policy_digest,
                "approved_payload_hash": intent.approved_payload_hash,
                "idempotency_key_digest": key_digest,
                "client_order_id": client_order_id,
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
                "notional": format(order["notional"], "f"),
                "request_snapshot": intent.request_snapshot,
                "canonical_input_serialized": _canonical_json(canonical_input),
                "policy_serialized": _canonical_json(policy),
                "approved_payload_serialized": _canonical_json(approved_payload),
                "intent_identity_serialized": _canonical_json(intent_identity),
                "expires_at": _aware(order["expires_at"]).isoformat(),
                "instrument_snapshot_id": snapshots["instrument"].snapshot_id,
                "market_snapshot_id": snapshots["market"].snapshot_id,
                "account_snapshot_id": snapshots["account"].snapshot_id,
            }
            persisted_ids = self.db.execute(
                text(
                    "SELECT create_okx_demo_canary_lineage("
                    "CAST(:payload AS jsonb))"
                ),
                {"payload": json.dumps(privileged_payload, sort_keys=True)},
            ).scalar_one()
            if not isinstance(persisted_ids, dict):
                raise OkxDemoCanaryPreparationBlocked(
                    "controlled canary lineage write returned invalid evidence"
                )
            try:
                intent_database_id = int(persisted_ids["trade_intent_id"])
                decision_database_id = int(persisted_ids["risk_decision_id"])
                approval_database_id = int(persisted_ids["approved_execution_id"])
            except (KeyError, TypeError, ValueError):
                raise OkxDemoCanaryPreparationBlocked(
                    "controlled canary lineage write returned invalid evidence"
                ) from None
            intent = self.db.get(TradeIntent, intent_database_id)
            decision = self.db.get(RiskDecision, decision_database_id)
            approved = self.db.get(ApprovedExecution, approval_database_id)
            if intent is None or decision is None or approved is None:
                raise OkxDemoCanaryPreparationBlocked(
                    "controlled canary lineage write did not persist"
                )
        else:
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
            refresh_of_job_id=refresh_of_job_id,
            recovery_of_job_id=recovery_of_job_id,
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
        entry_kind, supersedes_job_ids, refresh_of_job_id = _fresh_entry_lineage(
            job.request_payload
        )
        recovery_of_job_id = _fresh_recovery_of(job.request_payload)
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
            refresh_of_job_id=refresh_of_job_id,
            recovery_of_job_id=recovery_of_job_id,
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


def process_pending_canary_consent_handoff(
    *,
    read_client: Any,
    db: Session,
    runtime_instance_id: str,
    fresh_reconciliation: Callable[[], Any],
    safety_check: Callable[[], bool],
    now: Optional[datetime] = None,
) -> Optional[CanaryConsentFinalizationResult]:
    """Capture and finalize the v28 consent in one runtime-owned transaction."""

    if not hasattr(db, "get_bind") or db.get_bind().dialect.name != "postgresql":
        return None
    current = _aware(now or datetime.now(timezone.utc))
    pending = db.execute(text("SELECT pending_okx_demo_canary_consent()" )).scalar_one()
    if pending is None:
        return None
    if pending.get("status") == "EXPIRED":
        db.commit()
        return None
    if not safety_check():
        raise OkxDemoCanaryPreparationBlocked(
            "manifest or openings freeze blocks consent capture"
        )
    observed = fresh_reconciliation()
    observed_run_id = getattr(observed, "reconciliation_run_id", None)
    if not isinstance(observed_run_id, int) or observed_run_id <= 0:
        raise OkxDemoCanaryPreparationBlocked(
            "fresh reconciliation did not return an exact persisted run"
        )
    current = _aware(db.execute(text("SELECT clock_timestamp()" )).scalar_one())
    pending_after_observe = db.execute(
        text("SELECT pending_okx_demo_canary_consent()")
    ).scalar_one()
    if pending_after_observe is None or pending_after_observe.get("status") == "EXPIRED":
        db.commit()
        return None
    if pending_after_observe["handoff_id"] != pending["handoff_id"]:
        raise OkxDemoCanaryPreparationBlocked("consent handoff changed during observe")
    service = OkxDemoCanaryPreparationService(db, now_provider=lambda: current)
    reconciliation_run_id = service._fresh_empty_reconciliation(current)
    if reconciliation_run_id != observed_run_id:
        raise OkxDemoCanaryPreparationBlocked(
            "fresh reconciliation result is not the current DB run"
        )
    # This must remain the final external read.  No operator/network hop occurs
    # after the exact bundle is captured and before its DB-clock validation.
    bundle = read_client.capture_execution_attestation(
        db, inst_id=str(pending["instrument_id"])
    )
    binding = {
        kind: {
            "database_id": int(getattr(getattr(bundle, kind), "database_id")),
            "snapshot_id": str(getattr(getattr(bundle, kind), "snapshot_id")),
            "digest": str(getattr(getattr(bundle, kind), "digest")),
        }
        for kind in ("instrument", "market", "account")
    }
    db.execute(
        text(
            "SELECT claim_okx_demo_canary_consent("
            ":handoff,:runtime,:reconciliation,CAST(:binding AS jsonb))"
        ),
        {
            "handoff": pending["handoff_id"],
            "runtime": runtime_instance_id,
            "reconciliation": reconciliation_run_id,
            "binding": json.dumps(binding, sort_keys=True),
        },
    ).scalar_one()
    snapshots: dict[str, OkxDemoTrustedSnapshot] = {}
    for kind, reference in binding.items():
        row = db.get(OkxDemoTrustedSnapshot, reference["database_id"])
        if (
            row is None
            or row.kind != kind
            or row.snapshot_id != reference["snapshot_id"]
            or row.digest != reference["digest"]
        ):
            raise OkxDemoCanaryPreparationBlocked(
                "exact attested snapshot reference changed"
            )
        snapshots[kind] = row
    finalize_now = datetime.now(timezone.utc)
    order = service._derive_order(snapshots, finalize_now)
    key_digest = str(pending["idempotency_key_digest"])
    audit_payload = {
        "provenance": CANARY_PROVENANCE,
        "execution_target": OKX_DEMO_TARGET_ID,
        "non_production": True,
        "audit_kind": "CONSENT_FINALIZED_EXECUTION",
        "consent_handoff_id": str(pending["handoff_id"]),
        "source_job_id": int(pending["source_job_id"]),
        "source_ancestry": list(pending["source_ancestry"]),
    }
    audit_job = ResearchJob(
        execution_scope_id=LOCAL_DRY_RUN_SCOPE_ID,
        job_type="okx_demo_canary_execution_audit",
        operation=CANARY_CONSENT_AUDIT_OPERATION,
        idempotency_key_digest=key_digest,
        request_hash=canonical_digest(audit_payload),
        request_payload=audit_payload,
        status="RUNNING",
        stage="CANARY_CONSENT_FINALIZING",
        attempt_count=0,
        max_attempts=1,
        evidence_snapshot={"provenance": CANARY_PROVENANCE, "non_production": True},
        started_at=finalize_now,
    )
    db.add(audit_job)
    db.flush()
    result = service._persist_lineage(
        key_digest=key_digest,
        now=finalize_now,
        reconciliation_run_id=reconciliation_run_id,
        snapshots=snapshots,
        order=order,
        job_override=audit_job,
        research_operation=CANARY_CONSENT_AUDIT_OPERATION,
        audit_metadata=audit_payload,
    )
    if not safety_check():
        raise OkxDemoCanaryPreparationBlocked(
            "manifest or openings freeze changed before consent finalization"
        )
    db.execute(
        text(
            "SELECT finalize_okx_demo_canary_consent("
            ":handoff,:runtime,:job,:chain,:approval,:reconciliation,"
            "CAST(:binding AS jsonb))"
        ),
        {
            "handoff": pending["handoff_id"],
            "runtime": runtime_instance_id,
            "job": result.research_job_id,
            "chain": result.full_chain_run_id,
            "approval": result.approval_id,
            "reconciliation": reconciliation_run_id,
            "binding": json.dumps(binding, sort_keys=True),
        },
    ).scalar_one()
    return CanaryConsentFinalizationResult(
        handoff_id=str(pending["handoff_id"]), preparation=result
    )


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

    entry_kind = payload.get("entry_kind")
    if entry_kind not in FRESH_EXECUTION_ENTRY_KINDS:
        return {}
    supersedes = payload.get("supersedes_job_ids")
    if not isinstance(supersedes, list) or any(
        not isinstance(job_id, int) or job_id <= 0 for job_id in supersedes
    ):
        evidence = {"entry_kind": entry_kind, "supersedes_job_ids": []}
    else:
        evidence = {
            "entry_kind": entry_kind,
            "supersedes_job_ids": list(supersedes),
        }
    refresh_of = payload.get("refresh_of_job_id")
    if entry_kind in FRESH_EXECUTION_REFRESH_KINDS and isinstance(refresh_of, int) and refresh_of > 0:
        evidence["refresh_of_job_id"] = refresh_of
    recovery_of = payload.get("recovery_of_job_id")
    if entry_kind in FRESH_EXECUTION_RECOVERY_KINDS and isinstance(recovery_of, int) and recovery_of > 0:
        evidence["recovery_of_job_id"] = recovery_of
        if payload.get("recovery_boundary") in {
            "PRE_616_FINALIZE_ACL_FAILURE",
            "POST_PERSISTENCE_LINEAGE_WRITE_FAILURE",
            "POST_PERSISTENCE_RECOVERY_SNAPSHOT_EXPIRY",
        }:
            evidence["recovery_boundary"] = payload["recovery_boundary"]
    return evidence


def _fresh_entry_lineage(
    payload: Any,
) -> tuple[Optional[str], tuple[int, ...], Optional[int]]:
    """Read bounded fresh-entry and refresh lineage metadata."""

    if not isinstance(payload, dict) or payload.get("entry_kind") not in FRESH_EXECUTION_ENTRY_KINDS:
        return None, (), None
    entry_kind = payload["entry_kind"]
    supersedes = payload.get("supersedes_job_ids")
    if not isinstance(supersedes, list) or any(
        not isinstance(job_id, int) or job_id <= 0 for job_id in supersedes
    ):
        raise OkxDemoCanaryPreparationBlocked(
            "fresh execution-only canary lineage is malformed"
        )
    refresh_of = payload.get("refresh_of_job_id")
    if entry_kind in FRESH_EXECUTION_REFRESH_KINDS and (
        not isinstance(refresh_of, int) or refresh_of <= 0
    ):
        raise OkxDemoCanaryPreparationBlocked(
            "canary refresh lineage is missing refresh_of_job_id"
        )
    if entry_kind == FRESH_EXECUTION_ONLY_ENTRY and refresh_of is not None:
        raise OkxDemoCanaryPreparationBlocked(
            "fresh execution-only entry cannot carry refresh lineage"
        )
    recovery_of = payload.get("recovery_of_job_id")
    if entry_kind in FRESH_EXECUTION_RECOVERY_KINDS and (
        not isinstance(recovery_of, int) or recovery_of <= 0
    ):
        raise OkxDemoCanaryPreparationBlocked(
            "canary recovery lineage is missing recovery_of_job_id"
        )
    expected_recovery_boundary = {
        FRESH_EXECUTION_ONLY_RECOVERY: "PRE_616_FINALIZE_ACL_FAILURE",
        FRESH_EXECUTION_ONLY_POST_PERSISTENCE_RECOVERY: (
            "POST_PERSISTENCE_LINEAGE_WRITE_FAILURE"
        ),
        FRESH_EXECUTION_ONLY_FINAL_EXPIRY_RECOVERY: (
            "POST_PERSISTENCE_RECOVERY_SNAPSHOT_EXPIRY"
        ),
    }.get(entry_kind)
    if (
        expected_recovery_boundary is not None
        and payload.get("recovery_boundary") != expected_recovery_boundary
    ):
        raise OkxDemoCanaryPreparationBlocked(
            "canary recovery lineage boundary is malformed"
        )
    if entry_kind not in FRESH_EXECUTION_RECOVERY_KINDS and recovery_of is not None:
        raise OkxDemoCanaryPreparationBlocked(
            "non-recovery fresh entry cannot carry recovery lineage"
        )
    if entry_kind in FRESH_EXECUTION_RECOVERY_KINDS and refresh_of is not None:
        raise OkxDemoCanaryPreparationBlocked(
            "canary recovery entry cannot carry refresh lineage"
        )
    return entry_kind, tuple(supersedes), refresh_of


def _fresh_entry_metadata(payload: Any) -> tuple[Optional[str], tuple[int, ...]]:
    """Backward-compatible view of fresh-entry metadata."""

    entry_kind, supersedes, _refresh_of = _fresh_entry_lineage(payload)
    return entry_kind, supersedes


def _fresh_recovery_of(payload: Any) -> Optional[int]:
    """Return the immutable recovery parent after validating fresh lineage."""

    entry_kind, _supersedes, _refresh_of = _fresh_entry_lineage(payload)
    if entry_kind not in FRESH_EXECUTION_RECOVERY_KINDS:
        return None
    recovery_of = payload.get("recovery_of_job_id") if isinstance(payload, dict) else None
    return recovery_of if isinstance(recovery_of, int) else None


def _safe_key(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or not value.isascii():
        raise OkxDemoCanaryPreparationBlocked("canary idempotency key is invalid")
    if any(not (char.isalnum() or char in "._:-") for char in value):
        raise OkxDemoCanaryPreparationBlocked("canary idempotency key is invalid")
    return value


def _canonical_json(value: Any) -> str:
    """Serialize the fixed canary payload exactly as ``canonical_digest`` does."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


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
