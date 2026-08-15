"""Canonical planless static/lookahead gate persistence.

Freshness is checked once when an attempt is created.  The attempt and both
terminal receipts then remain bound to that immutable lineage; active pointer
changes and wall-clock passage do not rewrite historical eligibility evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from typing import Any, Final, Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, select, update

from app.canonical_v13.market_acquisition import (
    MarketAcquisitionReceipt,
    verify_market_acquisition_receipt,
)
from app.canonical_v13.models import (
    CONFIGURATION_ACTIVATIONS_TABLE,
    CONFIGURATION_BUNDLE_MEMBERS_TABLE,
    CONFIGURATION_BUNDLES_TABLE,
    CONFIGURATION_SNAPSHOTS_TABLE,
    MARKET_ARTIFACTS_TABLE,
    MARKET_INSPECTIONS_TABLE,
    MARKET_PROFILE_VERSIONS_TABLE,
    MARKET_RECEIPTS_TABLE,
    MARKET_SNAPSHOT_MEMBERS_TABLE,
    MARKET_SNAPSHOTS_TABLE,
    RESEARCH_GATE_ATTEMPTS_TABLE,
    RESEARCH_GATE_RECEIPTS_TABLE,
    RESEARCH_TARGETS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_VERSIONS_TABLE,
)
from app.canonical_v13.research_validation import (
    LookaheadAnalysisReceipt,
    ResearchLineage,
    StaticValidationReceipt,
    canonical_gate_receipt_digest,
    canonical_research_digest,
    validate_lookahead_receipt,
    validate_static_source,
)
from app.canonical_v13.runtime_reader import read_frozen_research_bundle


GATE_ATTEMPT_CONTRACT: Final = "canonical-v13-planless-gate-attempt-v3"
STATIC_GATE_CONTRACT: Final = "canonical-v13-static-gate-receipt-v3"
LOOKAHEAD_GATE_CONTRACT: Final = "canonical-v13-lookahead-gate-receipt-v3"
GATE_WRITER_IDENTITY: Final = "canonical_validation_writer"
LEASE_SECONDS: Final = 1200
_HEX = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TERMINAL = frozenset({"PASSED", "FAILED", "BLOCKED"})


class CanonicalGateBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class GateAttemptReceipt:
    gate_attempt_id: UUID
    request_digest: str
    status: str
    repeat_noop: bool
    lineage: ResearchLineage


@dataclass(frozen=True)
class GateLeaseReceipt:
    gate_attempt_id: UUID
    status: str
    lease_token: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class GateProjection:
    gate_attempt_id: UUID
    strategy_version_id: UUID
    research_target_id: UUID
    configuration_bundle_id: UUID
    configuration_bundle_digest: str
    market_snapshot_id: UUID
    market_snapshot_digest: str
    status: str
    terminal_reason_code: str | None
    static_status: str | None
    static_reason_code: str | None
    static_receipt_id: UUID | None
    static_receipt_digest: str | None
    lookahead_status: str | None
    lookahead_reason_code: str | None
    lookahead_receipt_id: UUID | None
    lookahead_receipt_digest: str | None
    observed_signal_count: int | None
    observed_trade_count: int | None
    required_trade_count: int | None
    validation_eligible: bool
    created_at: datetime
    completed_at: datetime | None


def _utc(value: datetime | None = None) -> datetime:
    observed = value or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise CanonicalGateBlocked("BLOCKED_GATE_TIMESTAMP", "timezone is required")
    return observed.astimezone(timezone.utc)


def _digest(value: str, field: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise CanonicalGateBlocked("BLOCKED_GATE_DIGEST", f"{field} must be SHA-256")
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _bundle_bindings(connection: Connection, bundle_id: UUID) -> dict[str, Mapping[str, Any]]:
    rows = connection.execute(
        select(CONFIGURATION_BUNDLE_MEMBERS_TABLE).where(
            CONFIGURATION_BUNDLE_MEMBERS_TABLE.c.configuration_bundle_id == bundle_id
        )
    ).mappings().all()
    by_kind = {str(row["configuration_kind"]): row for row in rows}
    if "TARGET" not in by_kind or "WINDOW" not in by_kind:
        raise CanonicalGateBlocked("BLOCKED_GATE_BUNDLE_MEMBERS", "TARGET/WINDOW bindings are required")
    return by_kind


def _freshness_limit(frozen: object) -> int:
    binding = next(
        (item for item in getattr(frozen, "configurations", ()) if item.configuration_kind == "WINDOW"),
        None,
    )
    windows = None if binding is None else binding.payload.get("windows")
    limits = {
        int(item["coverage"]["freshness_max_age_seconds"])
        for item in windows or ()
        if isinstance(item, dict)
        and item.get("required") is True
        and isinstance(item.get("coverage"), dict)
        and isinstance(item["coverage"].get("freshness_max_age_seconds"), int)
        and not isinstance(item["coverage"].get("freshness_max_age_seconds"), bool)
    }
    if len(limits) != 1 or next(iter(limits)) <= 0:
        raise CanonicalGateBlocked("BLOCKED_MARKET_FRESHNESS_CONTRACT", "required windows need one freshness limit")
    return next(iter(limits))


def _verify_fresh_market(connection: Connection, lineage: ResearchLineage, *, now: datetime, limit: int) -> None:
    rows = connection.execute(
        select(
            MARKET_ARTIFACTS_TABLE.c.content_digest,
            MARKET_INSPECTIONS_TABLE.c.status.label("inspection_status"),
            MARKET_INSPECTIONS_TABLE.c.inspection_json,
            MARKET_INSPECTIONS_TABLE.c.inspection_digest,
            MARKET_RECEIPTS_TABLE.c.status.label("receipt_status"),
            MARKET_RECEIPTS_TABLE.c.artifact_digest,
            MARKET_RECEIPTS_TABLE.c.inspection_digest.label("receipt_inspection_digest"),
            MARKET_RECEIPTS_TABLE.c.receipt_digest,
        )
        .select_from(
            MARKET_SNAPSHOT_MEMBERS_TABLE.join(
                MARKET_RECEIPTS_TABLE,
                MARKET_RECEIPTS_TABLE.c.id == MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_receipt_id,
            ).join(
                MARKET_INSPECTIONS_TABLE,
                MARKET_INSPECTIONS_TABLE.c.id == MARKET_RECEIPTS_TABLE.c.market_inspection_id,
            ).join(
                MARKET_ARTIFACTS_TABLE,
                MARKET_ARTIFACTS_TABLE.c.id == MARKET_RECEIPTS_TABLE.c.market_artifact_id,
            )
        )
        .where(
            MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_snapshot_id == lineage.market_snapshot_id,
            MARKET_SNAPSHOT_MEMBERS_TABLE.c.research_target_id == lineage.research_target_id,
        )
    ).mappings().all()
    if not rows:
        raise CanonicalGateBlocked("BLOCKED_MARKET_RECEIPT_NOT_ACCEPTED", "accepted target evidence is unavailable")
    for row in rows:
        evidence = row["inspection_json"]
        expected_receipt_digest = canonical_research_digest(
            {
                "artifact_digest": row["content_digest"],
                "inspection_digest": row["inspection_digest"],
                "status": "ACCEPTED",
            }
        )
        if (
            row["inspection_status"] != "ACCEPTED"
            or row["receipt_status"] != "ACCEPTED"
            or row["artifact_digest"] != row["content_digest"]
            or row["receipt_inspection_digest"] != row["inspection_digest"]
            or row["receipt_digest"] != expected_receipt_digest
            or not isinstance(evidence, dict)
            or canonical_research_digest(evidence) != row["inspection_digest"]
        ):
            raise CanonicalGateBlocked("BLOCKED_MARKET_RECEIPT_DIGEST_DRIFT", "market inspection digest drifted")
        raw_acquisition = evidence.get("acquisition_receipt_json")
        try:
            acquisition = MarketAcquisitionReceipt(**raw_acquisition)
        except (TypeError, ValueError):
            raise CanonicalGateBlocked(
                "BLOCKED_MARKET_ACQUISITION_RECEIPT",
                "public acquisition evidence is unavailable",
            ) from None
        if (
            not verify_market_acquisition_receipt(acquisition)
            or evidence.get("acquisition_receipt_digest") != acquisition.receipt_digest
            or acquisition.content_digest != row["content_digest"]
            or evidence.get("acquired_at") != acquisition.acquired_at
        ):
            raise CanonicalGateBlocked(
                "BLOCKED_MARKET_ACQUISITION_RECEIPT_DRIFT",
                "public acquisition evidence differs",
            )
        try:
            acquired = datetime.fromisoformat(str(evidence["acquired_at"]))
        except (KeyError, ValueError) as exc:
            raise CanonicalGateBlocked("BLOCKED_MARKET_FRESHNESS_CONTRACT", "market acquisition timestamp is invalid") from exc
        if acquired.tzinfo is None or not 0 <= (now - acquired.astimezone(timezone.utc)).total_seconds() <= limit:
            raise CanonicalGateBlocked("BLOCKED_MARKET_FRESHNESS_EXPIRED", "new gate attempts require fresh market evidence")


def _bound_attempt_fields(connection: Connection, lineage: ResearchLineage, *, observed_at: datetime) -> dict[str, Any]:
    frozen = read_frozen_research_bundle(
        connection,
        configuration_bundle_id=lineage.configuration_bundle_id,
        expected_bundle_digest=lineage.configuration_bundle_digest,
    )
    if frozen.market_snapshot_id != lineage.market_snapshot_id or frozen.market_snapshot_digest != lineage.market_snapshot_digest:
        raise CanonicalGateBlocked("BLOCKED_GATE_MIXED_LINEAGE", "bundle and market lineage differ")
    if {item.research_target_id for item in frozen.targets} != {lineage.research_target_id}:
        raise CanonicalGateBlocked("BLOCKED_GATE_TARGET_LINEAGE", "target is not the frozen bundle target")
    activation = connection.execute(
        select(CONFIGURATION_ACTIVATIONS_TABLE).where(
            CONFIGURATION_ACTIVATIONS_TABLE.c.configuration_bundle_id == lineage.configuration_bundle_id,
            CONFIGURATION_ACTIVATIONS_TABLE.c.bundle_digest == lineage.configuration_bundle_digest,
        )
    ).mappings().one_or_none()
    if activation is None:
        raise CanonicalGateBlocked("BLOCKED_GATE_BUNDLE_NOT_ACCEPTED", "bundle was not accepted before attempt creation")
    bindings = _bundle_bindings(connection, lineage.configuration_bundle_id)
    target = bindings["TARGET"]
    window = bindings["WINDOW"]
    market = connection.execute(
        select(MARKET_SNAPSHOTS_TABLE, MARKET_PROFILE_VERSIONS_TABLE.c.payload_digest.label("market_profile_digest"))
        .select_from(MARKET_SNAPSHOTS_TABLE.join(
            MARKET_PROFILE_VERSIONS_TABLE,
            MARKET_PROFILE_VERSIONS_TABLE.c.id == MARKET_SNAPSHOTS_TABLE.c.market_profile_version_id,
        ))
        .where(MARKET_SNAPSHOTS_TABLE.c.id == lineage.market_snapshot_id)
    ).mappings().one_or_none()
    if market is None or market["snapshot_digest"] != lineage.market_snapshot_digest:
        raise CanonicalGateBlocked("BLOCKED_GATE_MARKET_LINEAGE", "market snapshot digest drifted")
    _verify_fresh_market(connection, lineage, now=observed_at, limit=_freshness_limit(frozen))
    return {
        "target_snapshot_id": target["configuration_snapshot_id"],
        "target_snapshot_digest": target["snapshot_digest"],
        "window_snapshot_id": window["configuration_snapshot_id"],
        "window_snapshot_digest": window["snapshot_digest"],
        "market_profile_version_id": market["market_profile_version_id"],
        "market_profile_digest": market["market_profile_digest"],
    }


def create_gate_attempt(
    connection: Connection,
    *,
    lineage: ResearchLineage,
    idempotency_key: str,
    release_commit: str,
    executor_image_digest: str,
    worker_source_digest: str,
    writer_identity: str = GATE_WRITER_IDENTITY,
    observed_at: datetime | None = None,
) -> GateAttemptReceipt:
    now = _utc(observed_at)
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key
        or idempotency_key.strip() != idempotency_key
        or len(idempotency_key) > 200
        or any(ord(character) < 32 for character in idempotency_key)
    ):
        raise CanonicalGateBlocked("BLOCKED_GATE_IDEMPOTENCY_KEY", "idempotency key is invalid")
    if not _COMMIT.fullmatch(release_commit):
        raise CanonicalGateBlocked("BLOCKED_GATE_RELEASE_COMMIT", "release commit must be a full SHA-1")
    _digest(executor_image_digest, "executor_image_digest")
    _digest(worker_source_digest, "worker_source_digest")
    existing = connection.execute(
        select(RESEARCH_GATE_ATTEMPTS_TABLE).where(
            RESEARCH_GATE_ATTEMPTS_TABLE.c.writer_identity == writer_identity,
            RESEARCH_GATE_ATTEMPTS_TABLE.c.idempotency_key == idempotency_key,
        )
    ).mappings().one_or_none()
    if existing is not None:
        requested = {
            "strategy_version_id": lineage.strategy_version_id,
            "research_target_id": lineage.research_target_id,
            "configuration_bundle_id": lineage.configuration_bundle_id,
            "configuration_bundle_digest": lineage.configuration_bundle_digest,
            "market_snapshot_id": lineage.market_snapshot_id,
            "market_snapshot_digest": lineage.market_snapshot_digest,
            "release_commit": release_commit,
            "executor_image_digest": executor_image_digest,
            "worker_source_digest": worker_source_digest,
            "gate_contract_version": GATE_ATTEMPT_CONTRACT,
        }
        if any(existing[key] != value for key, value in requested.items()):
            raise CanonicalGateBlocked("BLOCKED_GATE_IDEMPOTENCY_CONFLICT", "attempt replay differs")
        return GateAttemptReceipt(existing["id"], existing["request_digest"], existing["status"], True, lineage)
    artifact = connection.execute(
        select(STRATEGY_ARTIFACTS_TABLE)
        .select_from(STRATEGY_VERSIONS_TABLE.join(
            STRATEGY_ARTIFACTS_TABLE,
            STRATEGY_ARTIFACTS_TABLE.c.id == STRATEGY_VERSIONS_TABLE.c.artifact_id,
        ))
        .where(STRATEGY_VERSIONS_TABLE.c.id == lineage.strategy_version_id)
    ).mappings().one_or_none()
    if artifact is None:
        raise CanonicalGateBlocked("BLOCKED_GATE_ARTIFACT_UNAVAILABLE", "strategy artifact is unavailable")
    bound = _bound_attempt_fields(connection, lineage, observed_at=now)
    payload = {
        "contract": GATE_ATTEMPT_CONTRACT,
        "idempotency_key": idempotency_key,
        "lineage": {key: str(value) for key, value in asdict(lineage).items()},
        "artifact_digest": artifact["content_digest"],
        "release_commit": release_commit,
        "executor_image_digest": executor_image_digest,
        "worker_source_digest": worker_source_digest,
        **{key: str(value) for key, value in bound.items()},
    }
    request_digest = canonical_research_digest(payload)
    attempt_id = uuid4()
    connection.execute(RESEARCH_GATE_ATTEMPTS_TABLE.insert().values(
        id=attempt_id,
        strategy_version_id=lineage.strategy_version_id,
        research_target_id=lineage.research_target_id,
        artifact_digest=artifact["content_digest"],
        gate_contract_version=GATE_ATTEMPT_CONTRACT,
        release_commit=release_commit,
        executor_image_digest=executor_image_digest,
        worker_source_digest=worker_source_digest,
        configuration_bundle_id=lineage.configuration_bundle_id,
        configuration_bundle_digest=lineage.configuration_bundle_digest,
        market_snapshot_id=lineage.market_snapshot_id,
        market_snapshot_digest=lineage.market_snapshot_digest,
        idempotency_key=idempotency_key,
        request_digest=request_digest,
        status="PENDING",
        terminal_reason_code=None,
        writer_identity=writer_identity,
        lease_token_digest=None,
        lease_expires_at=None,
        created_at=now,
        started_at=None,
        completed_at=None,
        **bound,
    ))
    return GateAttemptReceipt(attempt_id, request_digest, "PENDING", False, lineage)


def claim_gate_attempt(connection: Connection, *, gate_attempt_id: UUID, observed_at: datetime | None = None) -> GateLeaseReceipt:
    now = _utc(observed_at)
    lease_nonce = uuid4().hex + uuid4().hex
    token_digest = sha256(lease_nonce.encode()).hexdigest()
    updated = connection.execute(
        update(RESEARCH_GATE_ATTEMPTS_TABLE)
        .where(RESEARCH_GATE_ATTEMPTS_TABLE.c.id == gate_attempt_id, RESEARCH_GATE_ATTEMPTS_TABLE.c.status == "PENDING")
        .values(status="RUNNING", lease_token_digest=token_digest, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS), started_at=now)
    )
    if updated.rowcount != 1:
        raise CanonicalGateBlocked("BLOCKED_GATE_ATTEMPT_NOT_CLAIMABLE", "attempt is not pending")
    return GateLeaseReceipt(gate_attempt_id, "RUNNING", lease_nonce, now + timedelta(seconds=LEASE_SECONDS))


def _attempt_for_lease(connection: Connection, gate_attempt_id: UUID, lease_token: str, now: datetime) -> Mapping[str, Any]:
    row = connection.execute(select(RESEARCH_GATE_ATTEMPTS_TABLE).where(RESEARCH_GATE_ATTEMPTS_TABLE.c.id == gate_attempt_id)).mappings().one_or_none()
    expires = None if row is None else row["lease_expires_at"]
    if isinstance(expires, datetime) and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if row is None or row["status"] != "RUNNING" or row["lease_token_digest"] != sha256(lease_token.encode()).hexdigest() or expires is None or expires < now:
        raise CanonicalGateBlocked("BLOCKED_GATE_LEASE_INVALID", "gate writer lease is unavailable or expired")
    return row


def _receipt_base(attempt: Mapping[str, Any], gate_type: str, request_digest: str) -> dict[str, Any]:
    keys = (
        "strategy_version_id", "research_target_id", "artifact_digest", "release_commit",
        "executor_image_digest", "worker_source_digest", "target_snapshot_id", "target_snapshot_digest",
        "window_snapshot_id", "window_snapshot_digest", "market_profile_version_id", "market_profile_digest",
        "configuration_bundle_id", "configuration_bundle_digest", "market_snapshot_id", "market_snapshot_digest",
    )
    return {
        "gate_attempt_id": attempt["id"],
        "gate_type": gate_type,
        "gate_contract_version": STATIC_GATE_CONTRACT if gate_type == "STATIC" else LOOKAHEAD_GATE_CONTRACT,
        **{key: attempt[key] for key in keys},
        "request_digest": request_digest,
    }


def persist_static_gate_receipt(connection: Connection, *, gate_attempt_id: UUID, lease_token: str, receipt: StaticValidationReceipt, observed_at: datetime | None = None) -> str:
    now = _utc(observed_at)
    attempt = connection.execute(
        select(RESEARCH_GATE_ATTEMPTS_TABLE).where(
            RESEARCH_GATE_ATTEMPTS_TABLE.c.id == gate_attempt_id
        )
    ).mappings().one_or_none()
    if attempt is None:
        raise CanonicalGateBlocked("BLOCKED_GATE_ATTEMPT_NOT_FOUND", "gate attempt is unavailable")
    if receipt.strategy_version_id != attempt["strategy_version_id"] or receipt.artifact_digest != attempt["artifact_digest"]:
        raise CanonicalGateBlocked("BLOCKED_GATE_MIXED_LINEAGE", "static receipt lineage differs")
    artifact = connection.execute(select(STRATEGY_ARTIFACTS_TABLE.c.normalized_content).select_from(
        STRATEGY_VERSIONS_TABLE.join(STRATEGY_ARTIFACTS_TABLE, STRATEGY_ARTIFACTS_TABLE.c.id == STRATEGY_VERSIONS_TABLE.c.artifact_id)
    ).where(STRATEGY_VERSIONS_TABLE.c.id == attempt["strategy_version_id"])).scalar_one()
    recomputed = validate_static_source(
        artifact,
        strategy_version_id=attempt["strategy_version_id"],
        expected_artifact_digest=attempt["artifact_digest"],
        validator_identity=receipt.validator_identity,
        validator_digest=receipt.validator_digest,
    )
    if recomputed != receipt:
        raise CanonicalGateBlocked("BLOCKED_GATE_RECEIPT_DIGEST_DRIFT", "static receipt does not recompute")
    existing = connection.execute(
        select(RESEARCH_GATE_RECEIPTS_TABLE).where(
            RESEARCH_GATE_RECEIPTS_TABLE.c.gate_attempt_id == gate_attempt_id,
            RESEARCH_GATE_RECEIPTS_TABLE.c.gate_type == "STATIC",
        )
    ).mappings().one_or_none()
    if existing is not None:
        evidence = existing["evidence_json"]
        if (
            existing["request_digest"] == receipt.request_digest
            and isinstance(evidence, dict)
            and evidence.get("source_receipt_digest") == receipt.receipt_digest
        ):
            return str(existing["receipt_digest"])
        raise CanonicalGateBlocked("BLOCKED_GATE_IDEMPOTENCY_CONFLICT", "static receipt replay differs")
    attempt = _attempt_for_lease(connection, gate_attempt_id, lease_token, now)
    evidence = {
        "findings": [asdict(item) for item in receipt.findings],
        "validator_identity": receipt.validator_identity,
        "validator_digest": receipt.validator_digest,
        "source_receipt_digest": receipt.receipt_digest,
    }
    values = {
        **_receipt_base(attempt, "STATIC", receipt.request_digest),
        "terminal_status": receipt.status,
        "reason_code": None if receipt.status == "PASSED" else "STATIC_FINDINGS_PRESENT",
        "failure_stage": None if receipt.status == "PASSED" else "STATIC_ANALYSIS",
        "tool_return_code": 0,
        "evidence_digest": canonical_research_digest(evidence),
        "stdout_digest": sha256(b"").hexdigest(),
        "stderr_digest": sha256(b"").hexdigest(),
        "evidence_json": evidence,
        "observed_signal_count": None,
        "observed_trade_count": None,
        "required_trade_count": None,
        "created_at": now,
    }
    values["receipt_digest"] = canonical_gate_receipt_digest(values)
    connection.execute(RESEARCH_GATE_RECEIPTS_TABLE.insert().values(**values))
    if receipt.status != "PASSED":
        connection.execute(update(RESEARCH_GATE_ATTEMPTS_TABLE).where(RESEARCH_GATE_ATTEMPTS_TABLE.c.id == gate_attempt_id).values(status="FAILED", terminal_reason_code="STATIC_FINDINGS_PRESENT", completed_at=now, lease_token_digest=None, lease_expires_at=None))
    return values["receipt_digest"]


def persist_lookahead_gate_receipt(connection: Connection, *, gate_attempt_id: UUID, lease_token: str, receipt: LookaheadAnalysisReceipt, observed_at: datetime | None = None) -> str:
    now = _utc(observed_at)
    attempt = connection.execute(
        select(RESEARCH_GATE_ATTEMPTS_TABLE).where(
            RESEARCH_GATE_ATTEMPTS_TABLE.c.id == gate_attempt_id
        )
    ).mappings().one_or_none()
    if attempt is None:
        raise CanonicalGateBlocked("BLOCKED_GATE_ATTEMPT_NOT_FOUND", "gate attempt is unavailable")
    lineage = ResearchLineage(
        strategy_version_id=attempt["strategy_version_id"], research_target_id=attempt["research_target_id"],
        configuration_bundle_id=attempt["configuration_bundle_id"], configuration_bundle_digest=attempt["configuration_bundle_digest"],
        market_snapshot_id=attempt["market_snapshot_id"], market_snapshot_digest=attempt["market_snapshot_digest"],
    )
    decision = validate_lookahead_receipt(receipt, expected_lineage=lineage, expected_artifact_digest=attempt["artifact_digest"])
    existing = connection.execute(
        select(RESEARCH_GATE_RECEIPTS_TABLE).where(
            RESEARCH_GATE_RECEIPTS_TABLE.c.gate_attempt_id == gate_attempt_id,
            RESEARCH_GATE_RECEIPTS_TABLE.c.gate_type == "LOOKAHEAD",
        )
    ).mappings().one_or_none()
    if existing is not None:
        evidence = existing["evidence_json"]
        if (
            existing["request_digest"] == receipt.request_digest
            and isinstance(evidence, dict)
            and evidence.get("source_receipt_digest") == receipt.receipt_digest
        ):
            return str(existing["receipt_digest"])
        raise CanonicalGateBlocked("BLOCKED_GATE_IDEMPOTENCY_CONFLICT", "lookahead receipt replay differs")
    attempt = _attempt_for_lease(connection, gate_attempt_id, lease_token, now)
    static = connection.execute(select(RESEARCH_GATE_RECEIPTS_TABLE).where(RESEARCH_GATE_RECEIPTS_TABLE.c.gate_attempt_id == gate_attempt_id, RESEARCH_GATE_RECEIPTS_TABLE.c.gate_type == "STATIC")).mappings().one_or_none()
    if static is None or static["terminal_status"] != "PASSED":
        raise CanonicalGateBlocked("BLOCKED_GATE_STATIC_PREREQUISITE", "lookahead requires persisted static PASS")
    evidence = {
        "analyzer_identity": receipt.analyzer_identity, "analyzer_digest": receipt.analyzer_digest,
        "source_evidence_digest": receipt.evidence_digest, "has_bias": receipt.has_bias,
        "redacted_detail": receipt.redacted_detail, "source_receipt_digest": receipt.receipt_digest,
    }
    reason = receipt.failure_code if receipt.status == "BLOCKED" else ("LOOKAHEAD_BIAS_DETECTED" if receipt.status == "FAILED" else None)
    values = {
        **_receipt_base(attempt, "LOOKAHEAD", receipt.request_digest),
        "terminal_status": receipt.status,
        "reason_code": reason,
        "failure_stage": receipt.failure_stage,
        "tool_return_code": receipt.tool_return_code,
        "evidence_digest": canonical_research_digest(evidence),
        "stdout_digest": receipt.stdout_digest,
        "stderr_digest": receipt.stderr_digest,
        "evidence_json": evidence,
        "observed_signal_count": receipt.observed_signal_count,
        "observed_trade_count": receipt.blocked_observed_trade_count,
        "required_trade_count": receipt.blocked_required_trade_count,
        "created_at": now,
    }
    values["receipt_digest"] = canonical_gate_receipt_digest(values)
    connection.execute(RESEARCH_GATE_RECEIPTS_TABLE.insert().values(**values))
    terminal = "PASSED" if decision.status == "PASSED" else ("FAILED" if receipt.status == "FAILED" else "BLOCKED")
    connection.execute(update(RESEARCH_GATE_ATTEMPTS_TABLE).where(RESEARCH_GATE_ATTEMPTS_TABLE.c.id == gate_attempt_id).values(status=terminal, terminal_reason_code=reason, completed_at=now, lease_token_digest=None, lease_expires_at=None))
    return values["receipt_digest"]


def recover_expired_gate_attempts(connection: Connection, *, observed_at: datetime | None = None) -> int:
    now = _utc(observed_at)
    result = connection.execute(update(RESEARCH_GATE_ATTEMPTS_TABLE).where(
        RESEARCH_GATE_ATTEMPTS_TABLE.c.status == "RUNNING",
        RESEARCH_GATE_ATTEMPTS_TABLE.c.lease_expires_at < now,
    ).values(status="BLOCKED", terminal_reason_code="GATE_LEASE_EXPIRED", completed_at=now, lease_token_digest=None, lease_expires_at=None))
    return int(result.rowcount or 0)


def read_gate_projection(connection: Connection, *, gate_attempt_id: UUID) -> GateProjection:
    attempt = connection.execute(select(RESEARCH_GATE_ATTEMPTS_TABLE).where(RESEARCH_GATE_ATTEMPTS_TABLE.c.id == gate_attempt_id)).mappings().one_or_none()
    if attempt is None:
        raise CanonicalGateBlocked("BLOCKED_GATE_ATTEMPT_NOT_FOUND", "gate attempt is unavailable")
    receipts = connection.execute(select(RESEARCH_GATE_RECEIPTS_TABLE).where(RESEARCH_GATE_RECEIPTS_TABLE.c.gate_attempt_id == gate_attempt_id)).mappings().all()
    for receipt in receipts:
        payload = {key: value for key, value in receipt.items() if key not in {"id", "receipt_digest"}}
        if canonical_gate_receipt_digest(payload) != receipt["receipt_digest"]:
            raise CanonicalGateBlocked("BLOCKED_GATE_RECEIPT_DIGEST_DRIFT", "persisted gate receipt digest drifted")
    by_type = {row["gate_type"]: row for row in receipts}
    static, lookahead = by_type.get("STATIC"), by_type.get("LOOKAHEAD")
    eligible = bool(attempt["status"] == "PASSED" and static and lookahead and static["terminal_status"] == "PASSED" and lookahead["terminal_status"] == "PASSED")
    return GateProjection(
        gate_attempt_id=attempt["id"], strategy_version_id=attempt["strategy_version_id"], research_target_id=attempt["research_target_id"],
        configuration_bundle_id=attempt["configuration_bundle_id"], configuration_bundle_digest=attempt["configuration_bundle_digest"],
        market_snapshot_id=attempt["market_snapshot_id"], market_snapshot_digest=attempt["market_snapshot_digest"], status=attempt["status"], terminal_reason_code=attempt["terminal_reason_code"],
        static_status=None if static is None else static["terminal_status"], static_reason_code=None if static is None else static["reason_code"], static_receipt_id=None if static is None else static["id"], static_receipt_digest=None if static is None else static["receipt_digest"],
        lookahead_status=None if lookahead is None else lookahead["terminal_status"], lookahead_reason_code=None if lookahead is None else lookahead["reason_code"], lookahead_receipt_id=None if lookahead is None else lookahead["id"], lookahead_receipt_digest=None if lookahead is None else lookahead["receipt_digest"],
        observed_signal_count=None if lookahead is None else lookahead["observed_signal_count"], observed_trade_count=None if lookahead is None else lookahead["observed_trade_count"], required_trade_count=None if lookahead is None else lookahead["required_trade_count"],
        validation_eligible=eligible, created_at=attempt["created_at"], completed_at=attempt["completed_at"],
    )


def list_gate_projections(connection: Connection, *, limit: int = 200) -> tuple[GateProjection, ...]:
    ids = connection.execute(select(RESEARCH_GATE_ATTEMPTS_TABLE.c.id).order_by(RESEARCH_GATE_ATTEMPTS_TABLE.c.created_at.desc()).limit(limit)).scalars().all()
    return tuple(read_gate_projection(connection, gate_attempt_id=item) for item in ids)


__all__ = [name for name in tuple(globals()) if name.startswith(("Gate", "Canonical", "create_", "claim_", "persist_", "recover_", "read_", "list_"))]
