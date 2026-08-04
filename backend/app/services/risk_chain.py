from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re
import secrets
from types import MappingProxyType
from typing import Any, Mapping, Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    ApprovedExecution,
    BacktestResult,
    BacktestRun,
    BacktestTask,
    FullChainRun,
    FullChainSignalSnapshot,
    FullChainStageRun,
    OkxDemoAttestedSession,
    OkxDemoTrustedSnapshot,
    RiskBudget,
    RiskDecision,
    Strategy,
    StrategyCandidateApproval,
    StrategyScore,
    StrategyVersion,
    TradeIntent,
)
from app.models.execution_lineage import LOCAL_DRY_RUN_SCOPE_ID, OKX_DEMO_TARGET_ID
from app.repositories.execution_lineage import ensure_execution_scope_catalog


POLICY_VERSION = "risk-chain-v2"
SNAPSHOT_NAMES = ("instrument", "market", "account")
ORDER_TYPES = {"limit", "market"}
SIDE_VALUES = {"buy", "sell"}
OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
INSTRUMENT_ID = re.compile(r"^[A-Z0-9]{2,20}-[A-Z0-9]{2,20}-SWAP$")
SAFE_CURRENCY = re.compile(r"^[A-Z0-9]{2,20}$")
SECRET_LIKE = re.compile(
    r"(api[_-]?key|secret|passphrase|password|bearer|token|"
    r"-----BEGIN|sk-[A-Za-z0-9]{12,})",
    re.IGNORECASE,
)
_CAPABILITY_SENTINEL = object()
_NORMALIZED_SNAPSHOT_SENTINEL = object()


class RiskChainBlocked(RuntimeError):
    status = "BLOCKED"


@dataclass(frozen=True)
class RiskChainResult:
    status: str
    trade_intent_id: int
    risk_decision_id: int
    approved_execution_id: Optional[int]
    intent_id: str
    client_order_id: str
    order_submission_authorized: bool


@dataclass(frozen=True)
class TrustedRiskInput:
    lineage: dict[str, Any]
    snapshot_evidence: dict[str, Any]
    expires_at: datetime
    instrument_id: str
    side: str
    position_side: str
    order_type: str
    quantity: Decimal
    limit_price: Optional[Decimal]
    reference_price: Decimal
    leverage: Decimal
    margin_mode: str
    stop_loss: Decimal
    take_profit: Decimal
    reduce_only: bool
    notional: Decimal
    account_exposure: Decimal
    account_positions: int


@dataclass(frozen=True)
class _AttestedSessionIdentity:
    session_id: str
    execution_target: str
    pinned_fingerprint_sha256: str
    created_at: datetime
    expires_at: datetime
    nonce: str


class _AttestedSessionCapability:
    __slots__ = ("_identity", "_proof", "_revoked")

    def __init__(
        self,
        sentinel: object,
        identity: _AttestedSessionIdentity,
        proof: str,
    ) -> None:
        if sentinel is not _CAPABILITY_SENTINEL:
            raise TypeError("attested session capability is private")
        self._identity = identity
        self._proof = proof
        self._revoked = False

    def __reduce__(self):
        raise TypeError("attested session capability cannot be serialized")

    def _open(self, sentinel: object, now: datetime) -> tuple[_AttestedSessionIdentity, str]:
        if (
            sentinel is not _CAPABILITY_SENTINEL
            or self._revoked
            or now < self._identity.created_at
            or now >= self._identity.expires_at
        ):
            raise RiskChainBlocked("attested session is expired or revoked")
        return self._identity, self._proof

    def _revoke(self, sentinel: object) -> None:
        if sentinel is not _CAPABILITY_SENTINEL:
            raise TypeError("attested session capability is private")
        self._revoked = True

    def _revoke_material(
        self, sentinel: object
    ) -> tuple[_AttestedSessionIdentity, str]:
        if sentinel is not _CAPABILITY_SENTINEL:
            raise TypeError("attested session capability is private")
        return self._identity, self._proof


class _NormalizedAttestedSnapshot:
    __slots__ = ("kind", "content", "observed_at", "expires_at")

    def __init__(
        self,
        sentinel: object,
        *,
        kind: str,
        content: Mapping[str, Any],
        observed_at: datetime,
        expires_at: datetime,
    ) -> None:
        if sentinel is not _NORMALIZED_SNAPSHOT_SENTINEL:
            raise TypeError("attested snapshot product is private")
        self.kind = kind
        self.content = MappingProxyType(dict(content))
        self.observed_at = observed_at
        self.expires_at = expires_at

    def __reduce__(self):
        raise TypeError("attested snapshot product cannot be serialized")


def _canonical(value: Any) -> str:
    def encode(item: Any) -> str:
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, datetime):
            return _aware(item, "canonical datetime").isoformat()
        raise TypeError("unsupported canonical value: {}".format(type(item).__name__))

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=encode,
        )
    except (TypeError, ValueError) as exc:
        raise RiskChainBlocked("input is not canonicalizable") from exc


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _trusted_snapshot_id(row: OkxDemoTrustedSnapshot) -> str:
    return "{}:{}".format(
        row.kind,
        canonical_digest(
            {
                "digest": row.digest,
                "fingerprint": row.attestation_fingerprint_sha256,
                "kind": row.kind,
                "observed_at": _persisted_aware(row.observed_at),
                "session_id": row.attested_session_id,
            }
        )[:48],
    )


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RiskChainBlocked("{} is invalid".format(name)) from None
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise RiskChainBlocked("{} is invalid".format(name))
    return parsed


def _aware(value: Any, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise RiskChainBlocked("{} is invalid".format(name)) from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RiskChainBlocked("{} must be timezone-aware".format(name))
    return value.astimezone(timezone.utc)


def _persisted_aware(value: datetime) -> datetime:
    # SQLite drops timezone information in tests; PostgreSQL returns aware values.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise RiskChainBlocked("{} is invalid".format(name))
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise RiskChainBlocked("{} is invalid".format(name)) from None
    if parsed < minimum or str(parsed) != str(value):
        raise RiskChainBlocked("{} is invalid".format(name))
    return parsed


def _safe_string(
    value: Any,
    name: str,
    *,
    maximum: int,
    pattern: Optional[re.Pattern[str]] = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or SECRET_LIKE.search(value)
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise RiskChainBlocked("{} is unsafe".format(name))
    return value


TRUSTED_CANDLE_SEQUENCE_PATHS = frozenset(
    {
        "market trusted snapshot.confirmed_candles",
        "market snapshot.confirmed_candles",
    }
)


def _reject_unsafe_content(value: Any, path: str = "snapshot") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _safe_string(key, "{} key".format(path), maximum=80)
            _reject_unsafe_content(item, "{}.{}".format(path, key))
        return
    if isinstance(value, (list, tuple)):
        maximum_items = (
            300
            if path in TRUSTED_CANDLE_SEQUENCE_PATHS
            else 100
        )
        if len(value) > maximum_items:
            raise RiskChainBlocked("{} is too large".format(path))
        for index, item in enumerate(value):
            _reject_unsafe_content(item, "{}[{}]".format(path, index))
        return
    if isinstance(value, str):
        _safe_string(value, path, maximum=256)
        return
    if value is not None and not isinstance(value, (bool, int, float, Decimal, datetime)):
        raise RiskChainBlocked("{} contains an unsupported value".format(path))


def _session_signature_payload(identity: _AttestedSessionIdentity) -> bytes:
    created_micros = int(identity.created_at.timestamp() * 1_000_000)
    expires_micros = int(identity.expires_at.timestamp() * 1_000_000)
    return "|".join(
        (
            identity.session_id,
            identity.execution_target,
            identity.pinned_fingerprint_sha256,
            str(created_micros),
            str(expires_micros),
            identity.nonce,
        )
    ).encode("ascii")


def _issue_attested_session_capability(
    *,
    attestation_hmac_key: bytes,
    pinned_fingerprint_sha256: str,
    created_at: datetime,
    expires_at: datetime,
) -> _AttestedSessionCapability:
    if not isinstance(attestation_hmac_key, bytes) or len(attestation_hmac_key) != 32:
        raise RiskChainBlocked("attestation proof key is unavailable")
    fingerprint = _safe_string(
        pinned_fingerprint_sha256,
        "pinned account fingerprint",
        maximum=64,
        pattern=re.compile(r"^[0-9a-f]{64}$"),
    )
    created = _aware(created_at, "attested session created_at")
    expiry = _aware(expires_at, "attested session expires_at")
    if created >= expiry:
        raise RiskChainBlocked("attested session time window is invalid")
    identity = _AttestedSessionIdentity(
        session_id="okx-demo-{}".format(secrets.token_hex(24)),
        execution_target=OKX_DEMO_TARGET_ID,
        pinned_fingerprint_sha256=fingerprint,
        created_at=created,
        expires_at=expiry,
        nonce=secrets.token_hex(32),
    )
    proof = hmac.new(
        attestation_hmac_key,
        _session_signature_payload(identity),
        hashlib.sha256,
    ).hexdigest()
    return _AttestedSessionCapability(_CAPABILITY_SENTINEL, identity, proof)


def _normalize_attested_snapshot(
    capability: _AttestedSessionCapability,
    *,
    kind: str,
    content: Mapping[str, Any],
    observed_at: datetime,
    expires_at: datetime,
) -> _NormalizedAttestedSnapshot:
    observed = _aware(observed_at, "snapshot observed_at")
    identity, _proof = capability._open(_CAPABILITY_SENTINEL, observed)
    normalized = dict(content)
    if kind == "account":
        normalized["pinned_account_fingerprint"] = identity.pinned_fingerprint_sha256
    return _NormalizedAttestedSnapshot(
        _NORMALIZED_SNAPSHOT_SENTINEL,
        kind=kind,
        content=normalized,
        observed_at=observed,
        expires_at=_aware(expires_at, "snapshot expires_at"),
    )


def _revoke_attested_session_capability(
    capability: _AttestedSessionCapability,
) -> None:
    capability._revoke(_CAPABILITY_SENTINEL)


def _revoke_attested_session(
    db: Session,
    capability: _AttestedSessionCapability,
    *,
    reason: str,
    revoked_at: datetime,
) -> None:
    if reason not in {
        "IDENTITY_DRIFT",
        "EXPIRED",
        "FACTORY_CLOSE",
        "WRITE_FAILURE",
    }:
        raise RiskChainBlocked("attested session revoke reason is invalid")
    when = _aware(revoked_at, "attested session revoked_at")
    identity, proof = capability._revoke_material(_CAPABILITY_SENTINEL)
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text(
                "SELECT revoke_okx_demo_attested_session("
                "CAST(:session_id AS text), CAST(:proof AS text), "
                "CAST(:reason AS text), :revoked_micros)"
            ),
            {
                "session_id": identity.session_id,
                "proof": proof,
                "reason": reason,
                "revoked_micros": int(when.timestamp() * 1_000_000),
            },
        )
        db.expire_all()
        persisted = db.get(OkxDemoAttestedSession, identity.session_id)
        if (
            persisted is None
            or persisted.revoked_at is None
            or persisted.revoke_reason != reason
        ):
            raise RiskChainBlocked("attested session revoke did not persist")
    else:
        session = db.get(OkxDemoAttestedSession, identity.session_id)
        if session is None:
            raise RiskChainBlocked("attested session revoke target is missing")
        if session.capability_proof_digest != hashlib.sha256(
            proof.encode("ascii")
        ).hexdigest():
            raise RiskChainBlocked("attested session revoke proof is invalid")
        if session.revoked_at is None:
            session.revoked_at = when
            session.revoke_reason = reason
            db.flush()
        elif session.revoke_reason != reason:
            raise RiskChainBlocked("attested session revoke conflicts")


def _persist_attested_session(
    db: Session,
    capability: _AttestedSessionCapability,
    *,
    now: datetime,
) -> OkxDemoAttestedSession:
    """Bind an attested capability durably before any writer can use it."""

    active_now = _aware(now, "attested session bind now")
    identity, proof = capability._open(_CAPABILITY_SENTINEL, active_now)
    proof_digest = hashlib.sha256(proof.encode("utf-8")).hexdigest()
    session = db.get(OkxDemoAttestedSession, identity.session_id)
    if session is None:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db.execute(
                text(
                    "SELECT write_okx_demo_attested_session("
                    "CAST(:session_id AS text), CAST(:target AS text), "
                    "CAST(:fingerprint AS text), :created_micros, "
                    ":expires_micros, CAST(:nonce AS text), "
                    "CAST(:signature AS text))"
                ),
                {
                    "session_id": identity.session_id,
                    "target": identity.execution_target,
                    "fingerprint": identity.pinned_fingerprint_sha256,
                    "created_micros": int(
                        identity.created_at.timestamp() * 1_000_000
                    ),
                    "expires_micros": int(
                        identity.expires_at.timestamp() * 1_000_000
                    ),
                    "nonce": identity.nonce,
                    "signature": proof,
                },
            )
            session = db.get(OkxDemoAttestedSession, identity.session_id)
        else:
            session = OkxDemoAttestedSession(
                session_id=identity.session_id,
                execution_target_id=identity.execution_target,
                pinned_fingerprint_sha256=identity.pinned_fingerprint_sha256,
                capability_proof_digest=proof_digest,
                attestation_nonce=identity.nonce,
                created_at=identity.created_at,
                expires_at=identity.expires_at,
            )
            db.add(session)
            db.flush()
    if (
        session is None
        or session.execution_target_id != identity.execution_target
        or session.pinned_fingerprint_sha256
        != identity.pinned_fingerprint_sha256
        or session.capability_proof_digest != proof_digest
        or session.attestation_nonce != identity.nonce
        or _persisted_aware(session.created_at) != identity.created_at
        or _persisted_aware(session.expires_at) != identity.expires_at
        or session.revoked_at is not None
    ):
        raise RiskChainBlocked("attested session database binding is invalid")
    return session


def _write_attested_snapshot(
    db: Session,
    capability: _AttestedSessionCapability,
    normalized: _NormalizedAttestedSnapshot,
    *,
    now: datetime,
) -> OkxDemoTrustedSnapshot:
    """Private #445 closure target; request/evaluate code has no write entrypoint."""

    if not isinstance(capability, _AttestedSessionCapability):
        raise RiskChainBlocked("trusted snapshot capability is missing")
    if not isinstance(normalized, _NormalizedAttestedSnapshot):
        raise RiskChainBlocked("trusted snapshot normalizer product is missing")
    active_now = _aware(now, "snapshot write now")
    identity, proof = capability._open(_CAPABILITY_SENTINEL, active_now)
    kind = normalized.kind
    content = dict(normalized.content)
    observed = _aware(normalized.observed_at, "snapshot observed_at")
    expiry = _aware(normalized.expires_at, "snapshot expires_at")

    if kind not in SNAPSHOT_NAMES:
        raise RiskChainBlocked("trusted snapshot kind is invalid")
    if not isinstance(content, Mapping):
        raise RiskChainBlocked("trusted snapshot content is invalid")
    _reject_unsafe_content(content, "{} trusted snapshot".format(kind))
    if (
        content.get("execution_target") != OKX_DEMO_TARGET_ID
        or content.get("source") != "okx_demo_rest"
        or content.get("resource") != kind
        or content.get("stale") is not False
        or (kind == "account" and content.get("authenticated") is not True)
        or (
            kind == "account"
            and content.get("pinned_account_fingerprint")
            != identity.pinned_fingerprint_sha256
        )
    ):
        raise RiskChainBlocked("trusted snapshot attestation is invalid")
    if (
        observed < identity.created_at
        or observed >= expiry
        or expiry > identity.expires_at
        or _aware(content.get("expires_at"), "content expires_at") != expiry
    ):
        raise RiskChainBlocked("trusted snapshot time window is invalid")
    digest = canonical_digest(content)
    session = _persist_attested_session(
        db,
        capability,
        now=active_now,
    )
    snapshot_id = _trusted_snapshot_id(
        OkxDemoTrustedSnapshot(
            kind=kind,
            digest=digest,
            attestation_fingerprint_sha256=identity.pinned_fingerprint_sha256,
            observed_at=observed,
            attested_session_id=identity.session_id,
        )
    )
    existing = db.scalars(
        select(OkxDemoTrustedSnapshot).where(
            OkxDemoTrustedSnapshot.snapshot_id == snapshot_id
        )
    ).first()
    if existing is not None:
        if existing.digest != digest:
            raise RiskChainBlocked("trusted snapshot identity conflict")
        return existing
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        database_id = db.execute(
            text(
                "SELECT write_okx_demo_trusted_snapshot("
                "CAST(:session_id AS text), CAST(:proof AS text), "
                "CAST(:snapshot_id AS text), CAST(:kind AS text), "
                "CAST(:content AS jsonb), CAST(:digest AS text), "
                ":observed_at, :expires_at)"
            ),
            {
                "session_id": identity.session_id,
                "proof": proof,
                "snapshot_id": snapshot_id,
                "kind": kind,
                "content": _canonical(content),
                "digest": digest,
                "observed_at": observed,
                "expires_at": expiry,
            },
        ).scalar_one()
        row = db.get(OkxDemoTrustedSnapshot, database_id)
    else:
        row = OkxDemoTrustedSnapshot(
            snapshot_id=snapshot_id,
            kind=kind,
            execution_target_id=OKX_DEMO_TARGET_ID,
            content_json=dict(content),
            digest=digest,
            source_type="api_aggregate",
            core_data=True,
            attested_session_id=identity.session_id,
            attestation_fingerprint_sha256=identity.pinned_fingerprint_sha256,
            attested_session_expires_at=identity.expires_at,
            observed_at=observed,
            expires_at=expiry,
        )
        db.add(row)
        db.flush()
    if row is None:
        raise RiskChainBlocked("trusted snapshot write did not persist")
    return row


class RiskChainService:
    """Persist one deterministic, non-submitting OKX Demo risk chain."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def evaluate(
        self,
        *,
        idempotency_key: str,
        request: Mapping[str, Any],
        policy: Mapping[str, Any],
        now: Optional[datetime] = None,
    ) -> RiskChainResult:
        if self.db.in_transaction():
            raise RiskChainBlocked("risk chain requires a clean transaction")
        key = _safe_string(
            idempotency_key,
            "idempotency key",
            maximum=128,
            pattern=OPAQUE_REFERENCE,
        )
        active_now = _aware(now or datetime.now(timezone.utc), "now")
        request_input = self._authorization_input(request)
        input_digest = canonical_digest(request_input)
        policy_digest = canonical_digest(policy)
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        intent_id = canonical_digest(
            {
                "execution_target": OKX_DEMO_TARGET_ID,
                "input_digest": input_digest,
                "policy_digest": policy_digest,
                "idempotency_digest": key_digest,
            }
        )
        client_order_id = "FAI" + intent_id[:29]

        with self.db.begin():
            self._lock_idempotency(key_digest)
            existing = self.db.scalars(
                select(TradeIntent).where(
                    TradeIntent.execution_target_id == OKX_DEMO_TARGET_ID,
                    TradeIntent.idempotency_key_digest == key_digest,
                )
            ).first()
            if existing is not None:
                return self._existing_result(
                    existing,
                    input_digest=input_digest,
                    policy_digest=policy_digest,
                    now=active_now,
                )

            ensure_execution_scope_catalog(self.db)
            try:
                trusted = self._validate(request_input, policy, active_now)
            except RiskChainBlocked as exc:
                return self._persist_blocked(
                    intent_id=intent_id,
                    client_order_id=client_order_id,
                    input_digest=input_digest,
                    policy_digest=policy_digest,
                    key_digest=key_digest,
                    reason=str(exc),
                )

            intent = TradeIntent(
                execution_target_id=OKX_DEMO_TARGET_ID,
                authorization_schema_version="RISK_V1",
                intent_id=intent_id,
                canonical_hash=input_digest,
                policy_digest=policy_digest,
                idempotency_key_digest=key_digest,
                client_order_id=client_order_id,
                strategy_id=trusted.lineage["strategy_id"],
                strategy_version_id=trusted.lineage["strategy_version_id"],
                backtest_run_id=trusted.lineage["backtest_run_id"],
                backtest_result_id=trusted.lineage["backtest_result_id"],
                strategy_score_id=trusted.lineage["strategy_score_id"],
                instrument_id=trusted.instrument_id,
                side=trusted.side,
                position_side=trusted.position_side,
                order_type=trusted.order_type,
                quantity=trusted.quantity,
                limit_price=trusted.limit_price,
                reference_price=trusted.reference_price,
                leverage=trusted.leverage,
                margin_mode=trusted.margin_mode,
                stop_loss=trusted.stop_loss,
                take_profit=trusted.take_profit,
                reduce_only=trusted.reduce_only,
                status="PENDING_RISK",
                request_snapshot={
                    "canonical_input": request_input,
                    "snapshot_evidence": trusted.snapshot_evidence,
                },
                expires_at=trusted.expires_at,
            )
            self.db.add(intent)
            self.db.flush()

            status, reasons = self._evaluate_policy(trusted, policy)
            if trusted.expires_at <= active_now:
                status, reasons = "EXPIRED", ["snapshot evidence expired"]
            if status == "APPROVED":
                budget = self._locked_budget(
                    trusted.account_exposure,
                    trusted.account_positions,
                )
                projected = budget.reserved_notional + trusted.notional
                projected_positions = budget.approved_positions + 1
                if projected > _decimal(
                    policy["max_total_exposure"],
                    "max_total_exposure",
                    positive=True,
                ):
                    status, reasons = "REJECTED", ["maximum total exposure exceeded"]
                elif projected_positions > _integer(
                    policy["max_positions"], "max_positions", minimum=1
                ):
                    status, reasons = "REJECTED", ["maximum position count exceeded"]
                else:
                    budget.reserved_notional = projected
                    budget.approved_positions = projected_positions
                    intent.approved_payload_hash = self._approved_payload_hash(
                        intent=intent,
                        trusted=trusted,
                        policy_digest=policy_digest,
                    )

            return self._persist_decision(
                intent=intent,
                status=status,
                reasons=reasons,
                policy_digest=policy_digest,
                trusted=trusted,
            )

    def claim_active_approval(
        self,
        approval_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[ApprovedExecution]:
        """Lock and revalidate one permission before an execution-side claim."""

        if self.db.in_transaction():
            raise RiskChainBlocked("approval claim requires a clean transaction")
        identifier = _integer(approval_id, "approval_id", minimum=1)
        active_now = _aware(now or datetime.now(timezone.utc), "now")
        with self.db.begin():
            approved = self.db.scalars(
                select(ApprovedExecution)
                .where(ApprovedExecution.id == identifier)
                .with_for_update()
            ).first()
            if approved is None:
                return None
            intent = self.db.scalars(
                select(TradeIntent)
                .where(TradeIntent.id == approved.trade_intent_id)
                .with_for_update()
            ).one()
            decision = self.db.scalars(
                select(RiskDecision)
                .where(RiskDecision.id == approved.risk_decision_id)
                .with_for_update()
            ).one()
            if (
                approved.status != "ACTIVE"
                or intent.status != "APPROVED"
                or decision.decision != "APPROVED"
            ):
                self._invalidate_approval(
                    approved,
                    intent,
                    decision,
                    status="BLOCKED",
                    reason="approval state is no longer active",
                )
                return None
            if min(
                _persisted_aware(approved.expires_at),
                _persisted_aware(intent.expires_at),
            ) <= active_now:
                self._invalidate_approval(
                    approved,
                    intent,
                    decision,
                    status="EXPIRED",
                    reason="authorization evidence expired",
                )
                return None
            invalid_state = self._existing_snapshot_state(approved, active_now)
            if invalid_state is not None:
                status, reason = invalid_state
                self._invalidate_approval(
                    approved,
                    intent,
                    decision,
                    status=status,
                    reason=reason,
                )
                return None
            self.require_completed_full_chain_binding(
                approved=approved,
                intent=intent,
                decision=decision,
            )
            return approved

    def require_completed_full_chain_binding(
        self,
        *,
        approved: ApprovedExecution,
        intent: TradeIntent,
        decision: RiskDecision,
    ) -> None:
        """Require the durable RISK checkpoint before a writer-side claim."""

        chains = list(
            self.db.scalars(
                select(FullChainRun).where(
                    FullChainRun.trade_intent_id == intent.id,
                    FullChainRun.execution_target_id == OKX_DEMO_TARGET_ID,
                    FullChainRun.research_scope_id == LOCAL_DRY_RUN_SCOPE_ID,
                )
            ).all()
        )
        if len(chains) != 1:
            raise RiskChainBlocked(
                "approval is not bound to exactly one completed full-chain risk stage"
            )
        chain = chains[0]
        expected_ids = {
            "trade_intent_id": intent.id,
            "risk_decision_id": decision.id,
            "approved_execution_id": approved.id,
        }
        checkpoints = list(
            self.db.scalars(
                select(FullChainStageRun).where(
                    FullChainStageRun.full_chain_run_id == chain.id,
                    FullChainStageRun.stage == "RISK",
                )
            ).all()
        )
        if (
            chain.status != "EXECUTING"
            or chain.current_stage != "EXECUTION"
            or chain.trade_intent_id != intent.id
            or chain.risk_decision_id != decision.id
            or chain.approved_execution_id != approved.id
            or len(checkpoints) != 1
            or checkpoints[0].status != "SUCCESS"
            or checkpoints[0].database_ids != expected_ids
        ):
            raise RiskChainBlocked(
                "approval full-chain RISK checkpoint is incomplete or inconsistent"
            )

    @staticmethod
    def _authorization_input(request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise RiskChainBlocked("request must be an object")
        names = (
            "execution_target",
            "full_chain_run_id",
            "candidate_approval_id",
            "signal_snapshot_id",
            "signal_digest",
            "lineage",
            "snapshot_ids",
            "instrument_id",
            "side",
            "position_side",
            "order_type",
            "quantity",
            "limit_price",
            "reference_price",
            "leverage",
            "margin_mode",
            "stop_loss",
            "take_profit",
            "reduce_only",
        )
        return {name: request.get(name) for name in names}

    def _validate(
        self,
        request: Mapping[str, Any],
        policy: Mapping[str, Any],
        now: datetime,
    ) -> TrustedRiskInput:
        if request.get("execution_target") != OKX_DEMO_TARGET_ID:
            raise RiskChainBlocked("execution target must be OKX_DEMO")
        _reject_unsafe_content(request)
        self._validate_policy(policy)
        lineage = self._validate_lineage(request.get("lineage"), policy)
        self._validate_full_chain_binding(request, lineage, now)
        snapshots, expires_at, instrument, market, account = self._validate_snapshots(
            request.get("snapshot_ids"), request, now
        )
        instrument_id = _safe_string(
            request.get("instrument_id"),
            "instrument_id",
            maximum=80,
            pattern=INSTRUMENT_ID,
        )
        side = self._enum(request.get("side"), "side", SIDE_VALUES)
        position_side = self._enum(
            request.get("position_side"), "position_side", {"long", "short"}
        )
        margin_mode = self._enum(request.get("margin_mode"), "margin_mode", {"isolated"})
        order_type = self._enum(request.get("order_type"), "order_type", ORDER_TYPES)
        if order_type not in set(policy["allowed_order_types"]):
            raise RiskChainBlocked("order type is not allowlisted")

        quantity = _decimal(request.get("quantity"), "quantity", positive=True)
        reference_price = _decimal(
            market["reference_price"], "market reference_price", positive=True
        )
        supplied_reference = _decimal(
            request.get("reference_price"), "reference_price", positive=True
        )
        if supplied_reference != reference_price:
            raise RiskChainBlocked("request reference price does not match market snapshot")
        if order_type == "limit":
            limit_price = _decimal(
                request.get("limit_price"), "limit_price", positive=True
            )
            tick_size = _decimal(instrument["tickSz"], "tickSz", positive=True)
            if limit_price % tick_size != 0:
                raise RiskChainBlocked("limit price violates OKX tick size")
        else:
            if request.get("limit_price") is not None:
                raise RiskChainBlocked("market order must not carry limit_price")
            limit_price = None
        leverage = _decimal(request.get("leverage"), "leverage", positive=True)
        leverage_by_position_side = account["leverage_by_position_side"]
        if leverage != _decimal(
            leverage_by_position_side[position_side],
            "account leverage for position side",
            positive=True,
        ):
            raise RiskChainBlocked("request leverage does not match account snapshot")
        stop_loss = _decimal(request.get("stop_loss"), "stop_loss", positive=True)
        take_profit = _decimal(request.get("take_profit"), "take_profit", positive=True)
        reduce_only = request.get("reduce_only")
        if not isinstance(reduce_only, bool):
            raise RiskChainBlocked("reduce_only is invalid")
        expected_position_side = (
            "long" if (side == "buy") != reduce_only else "short"
        )
        if position_side != expected_position_side:
            raise RiskChainBlocked(
                "side, position_side and reduce_only do not describe one long/short action"
            )

        lot_size = _decimal(instrument["lotSz"], "lotSz", positive=True)
        minimum_size = _decimal(instrument["minSz"], "minSz", positive=True)
        if quantity < minimum_size or quantity % lot_size != 0:
            raise RiskChainBlocked("quantity violates OKX contract size")
        contract_value = _decimal(instrument["ctVal"], "ctVal", positive=True)
        pricing = limit_price or reference_price
        notional = (
            quantity * contract_value * pricing
            if instrument["contract_shape"] == "linear"
            else quantity * contract_value
        )
        return TrustedRiskInput(
            lineage=lineage,
            snapshot_evidence=snapshots,
            expires_at=expires_at,
            instrument_id=instrument_id,
            side=side,
            position_side=position_side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            reference_price=reference_price,
            leverage=leverage,
            margin_mode=margin_mode,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reduce_only=reduce_only,
            notional=notional,
            account_exposure=sum(
                (
                    _decimal(
                        account["exposure_by_position_side"][position_side],
                        "account {} exposure".format(position_side),
                    )
                    for position_side in ("long", "short")
                ),
                Decimal("0"),
            ),
            account_positions=sum(
                (
                    _integer(
                        account["open_positions_by_position_side"][position_side],
                        "account {} position count".format(position_side),
                    )
                    for position_side in ("long", "short")
                )
            ),
        )

    def _validate_full_chain_binding(
        self,
        request: Mapping[str, Any],
        lineage: Mapping[str, Any],
        now: datetime,
    ) -> None:
        chain_id = _integer(
            request.get("full_chain_run_id"),
            "full_chain_run_id",
            minimum=1,
        )
        candidate_approval_id = _integer(
            request.get("candidate_approval_id"),
            "candidate_approval_id",
            minimum=1,
        )
        signal_snapshot_id = _integer(
            request.get("signal_snapshot_id"),
            "signal_snapshot_id",
            minimum=1,
        )
        signal_digest = _safe_string(
            request.get("signal_digest"),
            "signal_digest",
            maximum=64,
            pattern=re.compile(r"^[0-9a-f]{64}$"),
        )
        chain = self.db.get(FullChainRun, chain_id)
        approval = self.db.get(StrategyCandidateApproval, candidate_approval_id)
        signal = self.db.get(FullChainSignalSnapshot, signal_snapshot_id)
        if (
            chain is None
            or chain.execution_target_id != OKX_DEMO_TARGET_ID
            or chain.research_scope_id != LOCAL_DRY_RUN_SCOPE_ID
            or chain.status != "EXECUTING"
            or chain.current_stage != "RISK"
            or chain.candidate_approval_id != candidate_approval_id
            or chain.signal_snapshot_id != signal_snapshot_id
        ):
            raise RiskChainBlocked("full-chain risk binding is not active")
        expected_lineage = {
            "strategy_id": chain.strategy_id,
            "strategy_version_id": chain.strategy_version_id,
            "backtest_run_id": chain.backtest_run_id,
            "backtest_task_id": chain.backtest_task_id,
            "backtest_result_id": chain.backtest_result_id,
            "strategy_score_id": chain.strategy_score_id,
        }
        if any(
            value is None or lineage.get(name) != value
            for name, value in expected_lineage.items()
        ):
            raise RiskChainBlocked("full-chain research lineage is inconsistent")
        if (
            approval is None
            or approval.full_chain_run_id != chain.id
            or approval.execution_target_id != OKX_DEMO_TARGET_ID
            or approval.status != "APPROVED"
            or approval.strategy_version_id != chain.strategy_version_id
            or approval.backtest_result_id != chain.backtest_result_id
            or approval.strategy_score_id != chain.strategy_score_id
            or _persisted_aware(approval.expires_at) <= now
        ):
            raise RiskChainBlocked("full-chain candidate approval is not current")
        if (
            signal is None
            or signal.full_chain_run_id != chain.id
            or signal.candidate_approval_id != approval.id
            or signal.execution_target_id != OKX_DEMO_TARGET_ID
            or signal.core_data is not True
            or signal.source_type not in {"database", "api_aggregate"}
            or signal.signal_digest != signal_digest
            or signal.instrument_id != request.get("instrument_id")
            or _persisted_aware(signal.observed_at) > now
            or _persisted_aware(signal.expires_at) <= now
        ):
            raise RiskChainBlocked("full-chain signal binding is invalid or stale")
        signal_checkpoint = self.db.scalars(
            select(FullChainStageRun).where(
                FullChainStageRun.full_chain_run_id == chain.id,
                FullChainStageRun.stage == "SIGNAL",
            )
        ).first()
        if (
            signal_checkpoint is None
            or signal_checkpoint.status != "SUCCESS"
            or signal_checkpoint.database_ids
            != {"signal_snapshot_id": signal.id}
        ):
            raise RiskChainBlocked("full-chain signal checkpoint is incomplete")

    def _validate_lineage(
        self,
        lineage: Any,
        policy: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(lineage, Mapping):
            raise RiskChainBlocked("lineage is missing")
        names = (
            "strategy_id",
            "strategy_version_id",
            "backtest_run_id",
            "backtest_task_id",
            "backtest_result_id",
            "strategy_score_id",
        )
        identifiers = {
            name: _integer(lineage.get(name), name, minimum=1) for name in names
        }
        strategy = self.db.get(Strategy, identifiers["strategy_id"])
        version = self.db.get(StrategyVersion, identifiers["strategy_version_id"])
        run = self.db.get(BacktestRun, identifiers["backtest_run_id"])
        task = self.db.get(BacktestTask, identifiers["backtest_task_id"])
        result = self.db.get(BacktestResult, identifiers["backtest_result_id"])
        score = self.db.get(StrategyScore, identifiers["strategy_score_id"])
        if not all((strategy, version, run, task, result, score)):
            raise RiskChainBlocked("lineage record is missing")
        if not (
            version.strategy_id == strategy.id
            and version.validation_status == "passed"
            and run.strategy_version_id == version.id
            and run.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID
            and run.status == "succeeded"
            and task.backtest_run_id == run.id
            and task.status == "succeeded"
            and result.backtest_run_id == run.id
            and result.backtest_task_id == task.id
            and score.strategy_id == strategy.id
            and score.strategy_version_id == version.id
            and score.backtest_result_id == result.id
        ):
            raise RiskChainBlocked("lineage is incomplete, failed, or inconsistent")
        threshold = float(
            _decimal(policy["min_strategy_score"], "min_strategy_score")
        )
        if score.total_score < threshold:
            raise RiskChainBlocked("strategy score is below policy threshold")
        expected_scoring_version = _safe_string(
            policy["scoring_version"],
            "policy scoring_version",
            maximum=80,
        )
        if score.scoring_version != expected_scoring_version:
            raise RiskChainBlocked("strategy scoring version is not authorized")
        return {
            **identifiers,
            "version_validation_status": version.validation_status,
            "run_status": run.status,
            "task_status": task.status,
            "score": score.total_score,
            "minimum_score": threshold,
            "scoring_version": _safe_string(
                score.scoring_version,
                "scoring_version",
                maximum=80,
            ),
        }

    def _validate_snapshots(
        self,
        snapshot_ids: Any,
        request: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], datetime, Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
        if not isinstance(snapshot_ids, Mapping):
            raise RiskChainBlocked("trusted snapshot ids are missing")
        evidence: dict[str, Any] = {}
        content_by_name: dict[str, Mapping[str, Any]] = {}
        expiries = []
        for name in SNAPSHOT_NAMES:
            snapshot_id = _safe_string(
                snapshot_ids.get(name),
                "{} snapshot id".format(name),
                maximum=80,
                pattern=OPAQUE_REFERENCE,
            )
            row = self.db.scalars(
                select(OkxDemoTrustedSnapshot).where(
                    OkxDemoTrustedSnapshot.snapshot_id == snapshot_id
                )
            ).first()
            if row is None:
                raise RiskChainBlocked("{} trusted snapshot is missing".format(name))
            attested_session = self.db.get(
                OkxDemoAttestedSession, row.attested_session_id
            )
            content = row.content_json
            _reject_unsafe_content(content, "{} snapshot".format(name))
            if (
                attested_session is None
                or attested_session.execution_target_id != OKX_DEMO_TARGET_ID
                or attested_session.revoked_at is not None
                or _persisted_aware(attested_session.expires_at) <= now
                or row.attestation_fingerprint_sha256
                != attested_session.pinned_fingerprint_sha256
                or _persisted_aware(row.attested_session_expires_at)
                != _persisted_aware(attested_session.expires_at)
                or row.kind != name
                or row.execution_target_id != OKX_DEMO_TARGET_ID
                or row.source_type != "api_aggregate"
                or row.core_data is not True
                or row.digest != canonical_digest(content)
                or row.snapshot_id != _trusted_snapshot_id(row)
            ):
                raise RiskChainBlocked("{} snapshot digest mismatch".format(name))
            if (
                content.get("execution_target") != OKX_DEMO_TARGET_ID
                or content.get("source") != "okx_demo_rest"
                or content.get("resource") != name
                or content.get("stale") is not False
                or (name == "account" and content.get("authenticated") is not True)
                or (
                    name == "account"
                    and content.get("pinned_account_fingerprint")
                    != attested_session.pinned_fingerprint_sha256
                )
                or (
                    name != "account"
                    and not isinstance(content.get("authenticated"), bool)
                )
            ):
                raise RiskChainBlocked("{} snapshot attestation is invalid".format(name))
            expires_at = _persisted_aware(row.expires_at)
            observed_at = _persisted_aware(row.observed_at)
            content_expiry = _aware(
                content.get("expires_at"), "{} content expires_at".format(name)
            )
            if observed_at > now or observed_at >= expires_at or expires_at != content_expiry:
                raise RiskChainBlocked("{} snapshot expiry mismatch".format(name))
            evidence[name] = {
                "snapshot_id": snapshot_id,
                "database_id": row.database_id,
                "digest": row.digest,
                "expires_at": expires_at.isoformat(),
            }
            content_by_name[name] = content
            expiries.append(expires_at)

        instrument = content_by_name["instrument"]
        required_instrument = {
            "instId",
            "instrument_type",
            "ctVal",
            "ctValCcy",
            "lotSz",
            "minSz",
            "tickSz",
            "contract_shape",
            "expires_at",
            "execution_target",
            "source",
            "resource",
            "stale",
            "authenticated",
        }
        if not required_instrument.issubset(instrument):
            raise RiskChainBlocked("instrument snapshot specification is incomplete")
        if (
            instrument["instId"] != request.get("instrument_id")
            or instrument["instrument_type"] != "SWAP"
            or instrument["contract_shape"] not in {"linear", "inverse"}
        ):
            raise RiskChainBlocked("instrument snapshot identity is invalid")
        _safe_string(
            instrument["ctValCcy"], "ctValCcy", maximum=20, pattern=SAFE_CURRENCY
        )
        for field in ("ctVal", "lotSz", "minSz", "tickSz"):
            _decimal(instrument[field], field, positive=True)
        base_currency, quote_currency, _ = instrument["instId"].split("-")
        allowed_value_currencies = (
            {base_currency}
            if instrument["contract_shape"] == "linear"
            else {quote_currency, "USD"}
        )
        if instrument["ctValCcy"] not in allowed_value_currencies:
            raise RiskChainBlocked("contract value currency does not prove contract shape")

        market = content_by_name["market"]
        required_market = {
            "instrument_id",
            "reference_price",
            "as_of",
            "expires_at",
            "execution_target",
            "source",
            "resource",
            "stale",
            "authenticated",
        }
        if not required_market.issubset(market):
            raise RiskChainBlocked("market snapshot is incomplete")
        market_as_of = _aware(market["as_of"], "market as_of")
        if (
            market["instrument_id"] != request.get("instrument_id")
            or market_as_of > now
            or market_as_of >= _aware(market["expires_at"], "market expires_at")
        ):
            raise RiskChainBlocked("market snapshot binding is invalid")

        account = content_by_name["account"]
        required_account = {
            "execution_target",
            "account_mode",
            "margin_mode",
            "current_exposure",
            "open_positions",
            "exposure_by_position_side",
            "open_positions_by_position_side",
            "leverage_by_position_side",
            "as_of",
            "expires_at",
            "source",
            "resource",
            "stale",
            "authenticated",
        }
        if not required_account.issubset(account):
            raise RiskChainBlocked("account snapshot is incomplete")
        account_as_of = _aware(account["as_of"], "account as_of")
        if (
            account["execution_target"] != OKX_DEMO_TARGET_ID
            or account["account_mode"] != "long_short_mode"
            or account["margin_mode"] != "isolated"
            or account_as_of > now
            or account_as_of >= _aware(account["expires_at"], "account expires_at")
        ):
            raise RiskChainBlocked("account snapshot binding is invalid")
        if _decimal(account["current_exposure"], "current_exposure") < 0:
            raise RiskChainBlocked("current exposure is invalid")
        _integer(account["open_positions"], "open_positions")
        exposure_by_position_side = account["exposure_by_position_side"]
        position_count_by_side = account["open_positions_by_position_side"]
        if (
            not isinstance(exposure_by_position_side, Mapping)
            or set(exposure_by_position_side) != {"long", "short"}
            or not isinstance(position_count_by_side, Mapping)
            or set(position_count_by_side) != {"long", "short"}
        ):
            raise RiskChainBlocked("account side exposure evidence is invalid")
        gross_exposure = sum(
            (
                _decimal(
                    exposure_by_position_side[position_side],
                    "account {} exposure".format(position_side),
                )
                for position_side in ("long", "short")
            ),
            Decimal("0"),
        )
        if gross_exposure < 0:
            raise RiskChainBlocked("account side exposure is invalid")
        gross_positions = sum(
            (
                _integer(
                    position_count_by_side[position_side],
                    "account {} position count".format(position_side),
                )
                for position_side in ("long", "short")
            )
        )
        if _decimal(account["current_exposure"], "current_exposure") != gross_exposure:
            raise RiskChainBlocked("account gross exposure does not match side evidence")
        if _integer(account["open_positions"], "open_positions") != gross_positions:
            raise RiskChainBlocked("account position count does not match side evidence")
        leverage_by_position_side = account["leverage_by_position_side"]
        if (
            not isinstance(leverage_by_position_side, Mapping)
            or set(leverage_by_position_side) != {"long", "short"}
        ):
            raise RiskChainBlocked("account leverage-by-position-side is invalid")
        for position_side, leverage in leverage_by_position_side.items():
            _decimal(leverage, "account leverage for {}".format(position_side), positive=True)
        return evidence, min(expiries), instrument, market, account

    @staticmethod
    def _validate_policy(policy: Mapping[str, Any]) -> None:
        required = (
            "allowed_instruments",
            "allowed_sides",
            "allowed_order_types",
            "max_leverage",
            "max_order_notional",
            "max_total_exposure",
            "max_positions",
            "max_price_deviation_pct",
            "min_strategy_score",
            "scoring_version",
        )
        if not isinstance(policy, Mapping) or any(name not in policy for name in required):
            raise RiskChainBlocked("risk policy is incomplete")
        for name in ("allowed_instruments", "allowed_sides", "allowed_order_types"):
            if not isinstance(policy[name], (list, tuple)) or not policy[name]:
                raise RiskChainBlocked("{} is invalid".format(name))
        if not set(policy["allowed_sides"]).issubset(SIDE_VALUES):
            raise RiskChainBlocked("allowed sides are invalid")
        if not set(policy["allowed_order_types"]).issubset(ORDER_TYPES):
            raise RiskChainBlocked("allowed order types are invalid")
        for instrument in policy["allowed_instruments"]:
            _safe_string(
                instrument,
                "allowed instrument",
                maximum=80,
                pattern=INSTRUMENT_ID,
            )
        for name in ("max_leverage", "max_order_notional", "max_total_exposure"):
            _decimal(policy[name], name, positive=True)
        if _decimal(policy["max_price_deviation_pct"], "max_price_deviation_pct") < 0:
            raise RiskChainBlocked("maximum price deviation is invalid")
        if _decimal(policy["min_strategy_score"], "min_strategy_score") < 0:
            raise RiskChainBlocked("minimum strategy score is invalid")
        _safe_string(policy["scoring_version"], "scoring_version", maximum=80)
        _integer(policy["max_positions"], "max_positions", minimum=1)

    @staticmethod
    def _enum(value: Any, name: str, allowed: set[str]) -> str:
        if value not in allowed:
            raise RiskChainBlocked("{} is unsupported".format(name))
        return _safe_string(value, name, maximum=32)

    @staticmethod
    def _evaluate_policy(
        trusted: TrustedRiskInput,
        policy: Mapping[str, Any],
    ) -> tuple[str, list[str]]:
        reasons = []
        if trusted.instrument_id not in set(policy["allowed_instruments"]):
            reasons.append("instrument is not allowlisted")
        if trusted.side not in set(policy["allowed_sides"]):
            reasons.append("side is not allowlisted")
        if trusted.position_side not in {"long", "short"} or trusted.margin_mode != "isolated":
            reasons.append("long/short isolated mode is required")
        if trusted.leverage > _decimal(policy["max_leverage"], "max_leverage"):
            reasons.append("maximum leverage exceeded")
        if trusted.notional > _decimal(
            policy["max_order_notional"], "max_order_notional"
        ):
            reasons.append("maximum order notional exceeded")
        pricing = trusted.limit_price or trusted.reference_price
        deviation = abs(pricing - trusted.reference_price) / trusted.reference_price
        if deviation > _decimal(
            policy["max_price_deviation_pct"], "max_price_deviation_pct"
        ):
            reasons.append("maximum price deviation exceeded")
        if trusted.side == "buy" and not (
            trusted.stop_loss < trusted.reference_price < trusted.take_profit
        ):
            reasons.append("buy SL/TP ordering is invalid")
        if trusted.side == "sell" and not (
            trusted.take_profit < trusted.reference_price < trusted.stop_loss
        ):
            reasons.append("sell SL/TP ordering is invalid")
        return ("REJECTED" if reasons else "APPROVED", reasons)

    def _persist_blocked(
        self,
        *,
        intent_id: str,
        client_order_id: str,
        input_digest: str,
        policy_digest: str,
        key_digest: str,
        reason: str,
    ) -> RiskChainResult:
        intent = TradeIntent(
            execution_target_id=OKX_DEMO_TARGET_ID,
            authorization_schema_version="RISK_V1",
            intent_id=intent_id,
            canonical_hash=input_digest,
            policy_digest=policy_digest,
            idempotency_key_digest=key_digest,
            client_order_id=client_order_id,
            status="BLOCKED",
            request_snapshot={
                "input_digest": input_digest,
                "policy_digest": policy_digest,
                "blocked_input_redacted": True,
            },
        )
        self.db.add(intent)
        self.db.flush()
        decision = RiskDecision(
            execution_target_id=OKX_DEMO_TARGET_ID,
            trade_intent_id=intent.id,
            authorization_schema_version="RISK_V1",
            policy_digest=policy_digest,
            decision="BLOCKED",
            policy_version=POLICY_VERSION,
            evidence_snapshot={
                "reasons": [reason],
                "input_digest": input_digest,
                "policy_digest": policy_digest,
                "llm_authority": False,
            },
        )
        self.db.add(decision)
        self.db.flush()
        return self._result(intent, decision, None)

    def _persist_decision(
        self,
        *,
        intent: TradeIntent,
        status: str,
        reasons: list[str],
        policy_digest: str,
        trusted: TrustedRiskInput,
    ) -> RiskChainResult:
        intent.status = status
        decision = RiskDecision(
            execution_target_id=OKX_DEMO_TARGET_ID,
            trade_intent_id=intent.id,
            authorization_schema_version="RISK_V1",
            policy_digest=policy_digest,
            decision=status,
            policy_version=POLICY_VERSION,
            evidence_snapshot={
                "reasons": reasons,
                "input_digest": intent.canonical_hash,
                "policy_digest": policy_digest,
                "lineage": trusted.lineage,
                "notional": format(trusted.notional, "f"),
                "llm_authority": False,
            },
        )
        self.db.add(decision)
        self.db.flush()
        approved = None
        if status == "APPROVED":
            approved = ApprovedExecution(
                execution_target_id=OKX_DEMO_TARGET_ID,
                trade_intent_id=intent.id,
                risk_decision_id=decision.id,
                intent_id=intent.intent_id or "",
                client_order_id=intent.client_order_id,
                authorization_schema_version="RISK_V1",
                canonical_hash=intent.canonical_hash or "",
                policy_digest=policy_digest,
                approved_payload_hash=intent.approved_payload_hash or "",
                instrument_snapshot_id=trusted.snapshot_evidence["instrument"][
                    "snapshot_id"
                ],
                market_snapshot_id=trusted.snapshot_evidence["market"][
                    "snapshot_id"
                ],
                account_snapshot_id=trusted.snapshot_evidence["account"][
                    "snapshot_id"
                ],
                decision="APPROVED",
                intent_status="APPROVED",
                reserved_notional=trusted.notional,
                order_submission_authorized=False,
                claim_required=True,
                status="ACTIVE",
                expires_at=trusted.expires_at,
                evidence_snapshot={
                    "offline_execution_permission": True,
                    "claim_revalidation_required": True,
                    "notional": format(trusted.notional, "f"),
                },
            )
            self.db.add(approved)
            self.db.flush()
        return self._result(intent, decision, approved)

    @staticmethod
    def _approved_payload_hash(
        *,
        intent: TradeIntent,
        trusted: TrustedRiskInput,
        policy_digest: str,
    ) -> str:
        return canonical_digest(
            {
                "authorization_schema_version": "RISK_V1",
                "canonical_hash": intent.canonical_hash,
                "policy_digest": policy_digest,
                "lineage": trusted.lineage,
                "snapshots": trusted.snapshot_evidence,
                "order": {
                    "instrument_id": trusted.instrument_id,
                    "side": trusted.side,
                    "position_side": trusted.position_side,
                    "order_type": trusted.order_type,
                    "quantity": trusted.quantity,
                    "limit_price": trusted.limit_price,
                    "reference_price": trusted.reference_price,
                    "leverage": trusted.leverage,
                    "margin_mode": trusted.margin_mode,
                    "stop_loss": trusted.stop_loss,
                    "take_profit": trusted.take_profit,
                    "reduce_only": trusted.reduce_only,
                    "notional": trusted.notional,
                },
            }
        )

    def _existing_result(
        self,
        intent: TradeIntent,
        *,
        input_digest: str,
        policy_digest: str,
        now: datetime,
    ) -> RiskChainResult:
        decision = self.db.scalars(
            select(RiskDecision).where(RiskDecision.trade_intent_id == intent.id)
        ).one()
        approved = self.db.scalars(
            select(ApprovedExecution).where(
                ApprovedExecution.trade_intent_id == intent.id
            ).with_for_update()
        ).first()
        if (
            intent.canonical_hash != input_digest
            or intent.policy_digest != policy_digest
        ):
            retained_approval = False
            if approved is not None:
                retained_approval = self._revoke_approval(
                    approved,
                    reason="idempotency key input or policy conflict",
                )
            if not retained_approval:
                intent.status = "BLOCKED"
                decision.decision = "BLOCKED"
            decision.evidence_snapshot = {
                "reasons": ["idempotency key input or policy conflict"],
                "input_digest": input_digest,
                "policy_digest": policy_digest,
                "llm_authority": False,
            }
            self.db.flush()
            return self._result(intent, decision, None)
        if decision.decision == "BLOCKED":
            return self._result(intent, decision, None)
        expiry = min(
            _persisted_aware(value)
            for value in (intent.expires_at, None if approved is None else approved.expires_at)
            if value is not None
        )
        if expiry <= now:
            retained_approval = False
            if approved is not None:
                retained_approval = self._revoke_approval(
                    approved,
                    reason="authorization evidence expired",
                )
            if not retained_approval:
                intent.status = "EXPIRED"
                decision.decision = "EXPIRED"
            decision.evidence_snapshot = {
                **decision.evidence_snapshot,
                "reasons": ["authorization evidence expired"],
            }
            self.db.flush()
            return self._result(intent, decision, None)
        if approved is not None and approved.status != "ACTIVE":
            return self._result(intent, decision, None)
        if approved is not None:
            invalid_state = self._existing_snapshot_state(approved, now)
            if invalid_state is not None:
                status, reason = invalid_state
                retained_approval = self._revoke_approval(
                    approved,
                    reason=reason,
                )
                if not retained_approval:
                    intent.status = status
                    decision.decision = status
                decision.evidence_snapshot = {
                    **decision.evidence_snapshot,
                    "reasons": [reason],
                }
                self.db.flush()
                return self._result(intent, decision, None)
        return self._result(intent, decision, approved)

    def _existing_snapshot_state(
        self,
        approved: ApprovedExecution,
        now: datetime,
    ) -> Optional[tuple[str, str]]:
        if self.db.get_bind().dialect.name == "postgresql":
            from app.db.migrations import schema_problems

            problems = schema_problems(self.db.get_bind())
            if problems:
                return "BLOCKED", "database readiness no longer permits authorization"
        snapshot_ids = {
            "instrument": approved.instrument_snapshot_id,
            "market": approved.market_snapshot_id,
            "account": approved.account_snapshot_id,
        }
        for kind, snapshot_id in snapshot_ids.items():
            snapshot = self.db.scalars(
                select(OkxDemoTrustedSnapshot)
                .where(OkxDemoTrustedSnapshot.snapshot_id == snapshot_id)
                .with_for_update()
            ).first()
            if snapshot is None:
                return "BLOCKED", "{} trusted snapshot is missing".format(kind)
            session = self.db.scalars(
                select(OkxDemoAttestedSession)
                .where(
                    OkxDemoAttestedSession.session_id
                    == snapshot.attested_session_id
                )
                .with_for_update()
            ).first()
            if (
                _persisted_aware(snapshot.expires_at) <= now
                or (
                    session is not None
                    and _persisted_aware(session.expires_at) <= now
                )
            ):
                return "EXPIRED", "{} authorization evidence expired".format(kind)
            if (
                session is None
                or session.revoked_at is not None
                or session.execution_target_id != OKX_DEMO_TARGET_ID
                or snapshot.execution_target_id != OKX_DEMO_TARGET_ID
                or snapshot.kind != kind
                or snapshot.attestation_fingerprint_sha256
                != session.pinned_fingerprint_sha256
                or _persisted_aware(snapshot.attested_session_expires_at)
                != _persisted_aware(session.expires_at)
                or snapshot.digest != canonical_digest(snapshot.content_json)
                or snapshot.snapshot_id != _trusted_snapshot_id(snapshot)
            ):
                reason = (
                    session.revoke_reason
                    if session is not None and session.revoke_reason
                    else "{} authorization evidence is invalid".format(kind)
                )
                return "BLOCKED", reason
        return None

    def _revoke_approval(
        self,
        approved: ApprovedExecution,
        *,
        reason: str = "authorization permission revoked",
    ) -> bool:
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            self.db.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('OKX_DEMO-risk-budget'))"
                )
            )
        budget = self.db.scalars(
            select(RiskBudget)
            .where(RiskBudget.execution_target_id == OKX_DEMO_TARGET_ID)
            .with_for_update()
        ).first()
        if budget is not None:
            budget.reserved_notional = max(
                Decimal("0"),
                budget.reserved_notional - approved.reserved_notional,
            )
            budget.approved_positions = max(0, budget.approved_positions - 1)
        bound_chains = list(
            self.db.scalars(
                select(FullChainRun)
                .where(FullChainRun.approved_execution_id == approved.id)
                .with_for_update()
            ).all()
        )
        if bound_chains:
            approved.status = "EXPIRED"
            approved.evidence_snapshot = {
                **dict(approved.evidence_snapshot or {}),
                "approval_active": False,
                "invalidation_reason": reason,
            }
            for chain in bound_chains:
                chain.status = "BLOCKED"
                chain.terminal_reason = reason
                chain.completed_at = datetime.now(timezone.utc)
            self.db.flush()
            return True
        self.db.delete(approved)
        self.db.flush()
        return False

    def _invalidate_approval(
        self,
        approved: ApprovedExecution,
        intent: TradeIntent,
        decision: RiskDecision,
        *,
        status: str,
        reason: str,
    ) -> None:
        retained_approval = self._revoke_approval(
            approved,
            reason=reason,
        )
        if not retained_approval:
            intent.status = status
            decision.decision = status
        decision.evidence_snapshot = {
            **decision.evidence_snapshot,
            "reasons": [reason],
        }
        self.db.flush()

    def _lock_idempotency(self, key_digest: str) -> None:
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:digest))"),
                {"digest": key_digest},
            )

    def _locked_budget(
        self,
        account_exposure: Decimal,
        account_positions: int,
    ) -> RiskBudget:
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            self.db.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('OKX_DEMO-risk-budget'))"
                )
            )
        budget = self.db.scalars(
            select(RiskBudget)
            .where(RiskBudget.execution_target_id == OKX_DEMO_TARGET_ID)
            .with_for_update()
        ).first()
        if budget is None:
            budget = RiskBudget(
                execution_target_id=OKX_DEMO_TARGET_ID,
                reserved_notional=account_exposure,
                approved_positions=account_positions,
            )
            self.db.add(budget)
            self.db.flush()
        else:
            budget.reserved_notional = max(
                budget.reserved_notional,
                account_exposure,
            )
            budget.approved_positions = max(
                budget.approved_positions,
                account_positions,
            )
        return budget

    @staticmethod
    def _result(
        intent: TradeIntent,
        decision: RiskDecision,
        approved: Optional[ApprovedExecution],
    ) -> RiskChainResult:
        return RiskChainResult(
            status=decision.decision,
            trade_intent_id=intent.id,
            risk_decision_id=decision.id,
            approved_execution_id=None if approved is None else approved.id,
            intent_id=intent.intent_id or "",
            client_order_id=intent.client_order_id,
            order_submission_authorized=False,
        )
