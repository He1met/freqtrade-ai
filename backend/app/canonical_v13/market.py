"""Canonical V1.3 market evidence domain.

All content and inspection facts are supplied explicitly.  This module never scans a
filesystem, calls a downloader/exchange, or consults legacy market receipts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.market_acquisition import (
    MarketAcquisitionReceipt,
    verify_market_acquisition_receipt,
)
from app.canonical_v13.models import (
    MARKET_ARTIFACTS_TABLE,
    MARKET_INSPECTIONS_TABLE,
    MARKET_PROFILES_TABLE,
    MARKET_PROFILE_VERSIONS_TABLE,
    MARKET_RECEIPTS_TABLE,
    MARKET_SNAPSHOTS_TABLE,
    MARKET_SNAPSHOT_MEMBERS_TABLE,
    RESEARCH_TARGETS_TABLE,
)


_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class CanonicalMarketBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class MarketInspectionFacts:
    row_count: int
    first_open_at: datetime
    last_close_at: datetime
    gap_count: int
    duplicate_count: int
    null_count: int
    monotonic: bool
    source_identity: str | None = None
    provenance_class: str | None = None
    target_key: str | None = None
    instrument: str | None = None
    pair: str | None = None
    timeframe: str | None = None
    data_kind: str | None = None
    acquired_at: datetime | None = None
    acquisition_receipt_digest: str | None = None


@dataclass(frozen=True)
class MarketEvidenceResult:
    artifact_id: UUID
    inspection_id: UUID
    receipt_id: UUID
    content_digest: str
    receipt_digest: str
    idempotent_replay: bool


@dataclass(frozen=True)
class MarketSnapshotResult:
    snapshot_id: UUID
    snapshot_digest: str
    member_count: int
    idempotent_replay: bool


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest_json(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _effective_connection(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _require_canonical(connection: Connection) -> Connection:
    effective = _effective_connection(connection)
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalMarketBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    return effective


def _safe_locator(locator: str) -> str:
    if (
        not locator
        or locator.strip() != locator
        or locator.startswith("/")
        or "\\" in locator
        or any(part in {"", ".", ".."} for part in locator.split("/"))
    ):
        raise CanonicalMarketBlocked(
            "BLOCKED_MARKET_LOCATOR",
            "locator must be a normalized root-relative POSIX path",
        )
    return locator


def create_market_profile_draft(
    connection: Connection,
    *,
    profile_key: str,
    scope_key: str,
    payload: dict[str, object],
) -> tuple[UUID, UUID, str]:
    if not profile_key or not scope_key or not payload:
        raise CanonicalMarketBlocked(
            "BLOCKED_MARKET_PROFILE_UNSET", "profile identity and payload are required"
        )
    effective = _require_canonical(connection)
    payload_digest = _digest_json(payload)
    existing_profile = effective.execute(
        select(MARKET_PROFILES_TABLE).where(
            MARKET_PROFILES_TABLE.c.profile_key == profile_key
        )
    ).mappings().one_or_none()
    if existing_profile is None:
        profile_id = uuid4()
        effective.execute(
            MARKET_PROFILES_TABLE.insert().values(
                id=profile_id,
                profile_key=profile_key,
                scope_key=scope_key,
                created_at=datetime.now(timezone.utc),
            )
        )
        next_number = 1
    else:
        profile = dict(existing_profile)
        profile_id = profile["id"]
        if profile["scope_key"] != scope_key:
            raise CanonicalMarketBlocked(
                "BLOCKED_MARKET_PROFILE_SCOPE_DRIFT",
                "profile key is already bound to another scope",
            )
        versions = effective.execute(
            select(MARKET_PROFILE_VERSIONS_TABLE).where(
                MARKET_PROFILE_VERSIONS_TABLE.c.market_profile_id == profile_id
            )
        ).mappings().all()
        for row in versions:
            if row["payload_digest"] == payload_digest:
                return profile_id, row["id"], payload_digest
        next_number = max(int(row["version_number"]) for row in versions) + 1
    version_id = uuid4()
    effective.execute(
        MARKET_PROFILE_VERSIONS_TABLE.insert().values(
            id=version_id,
            market_profile_id=profile_id,
            version_number=next_number,
            lifecycle_status="DRAFT",
            payload_json=payload,
            payload_digest=payload_digest,
            created_at=datetime.now(timezone.utc),
            validated_at=None,
        )
    )
    return profile_id, version_id, payload_digest


def validate_market_profile(
    connection: Connection, *, version_id: UUID
) -> str:
    effective = _require_canonical(connection)
    row = effective.execute(
        select(MARKET_PROFILE_VERSIONS_TABLE).where(
            MARKET_PROFILE_VERSIONS_TABLE.c.id == version_id
        )
    ).mappings().one_or_none()
    if row is None:
        raise CanonicalMarketBlocked(
            "BLOCKED_MARKET_PROFILE_MISSING", "market profile version is missing"
        )
    row = dict(row)
    observed_digest = _digest_json(row["payload_json"])
    if observed_digest != row["payload_digest"]:
        raise CanonicalMarketBlocked(
            "BLOCKED_MARKET_PROFILE_DIGEST_DRIFT", "market profile payload drifted"
        )
    if row["lifecycle_status"] == "RETIRED":
        raise CanonicalMarketBlocked(
            "BLOCKED_MARKET_PROFILE_RETIRED", "retired version cannot validate"
        )
    if row["lifecycle_status"] == "DRAFT":
        effective.execute(
            MARKET_PROFILE_VERSIONS_TABLE.update()
            .where(MARKET_PROFILE_VERSIONS_TABLE.c.id == version_id)
            .values(lifecycle_status="VALIDATED", validated_at=datetime.now(timezone.utc))
        )
    return observed_digest


def accept_market_artifact(
    connection: Connection,
    *,
    locator: str,
    content: bytes,
    media_type: str,
    inspector_identity: str,
    facts: MarketInspectionFacts,
    acquisition_receipt: MarketAcquisitionReceipt | None = None,
) -> MarketEvidenceResult:
    locator = _safe_locator(locator)
    if not content or not media_type or not inspector_identity:
        raise CanonicalMarketBlocked(
            "BLOCKED_MARKET_ARTIFACT_ENVELOPE", "content and identities are required"
        )
    if (
        facts.row_count <= 0
        or facts.first_open_at.tzinfo is None
        or facts.last_close_at.tzinfo is None
        or facts.last_close_at <= facts.first_open_at
        or facts.gap_count < 0
        or facts.duplicate_count < 0
        or facts.null_count < 0
    ):
        raise CanonicalMarketBlocked(
            "BLOCKED_MARKET_INSPECTION_INVALID", "inspection facts are invalid"
        )
    if (
        facts.monotonic is not True
        or facts.gap_count != 0
        or facts.duplicate_count != 0
        or facts.null_count != 0
    ):
        raise CanonicalMarketBlocked(
            "BLOCKED_MARKET_QUALITY",
            "artifact does not satisfy monotonic/gap/duplicate/null gates",
        )
    effective = _require_canonical(connection)
    content_digest = sha256(content).hexdigest()
    if acquisition_receipt is not None:
        receipt_matches = (
            verify_market_acquisition_receipt(acquisition_receipt)
            and acquisition_receipt.content_digest == content_digest
            and acquisition_receipt.observed_closed_candles == facts.row_count
            and acquisition_receipt.observed_first_open
            == facts.first_open_at.astimezone(timezone.utc).isoformat()
            and acquisition_receipt.observed_last_close
            == facts.last_close_at.astimezone(timezone.utc).isoformat()
            and acquisition_receipt.source_identity == facts.source_identity
            and acquisition_receipt.provenance_class == facts.provenance_class
            and acquisition_receipt.target_key == facts.target_key
            and acquisition_receipt.instrument == facts.instrument
            and acquisition_receipt.pair == facts.pair
            and acquisition_receipt.timeframe == facts.timeframe
            and acquisition_receipt.data_kind == facts.data_kind
            and acquisition_receipt.acquired_at
            == (
                facts.acquired_at.astimezone(timezone.utc).isoformat()
                if facts.acquired_at is not None and facts.acquired_at.tzinfo is not None
                else None
            )
            and facts.acquisition_receipt_digest
            == acquisition_receipt.receipt_digest
        )
        if not receipt_matches:
            raise CanonicalMarketBlocked(
                "BLOCKED_MARKET_ACQUISITION_RECEIPT_DRIFT",
                "inspection facts do not exactly bind the acquisition receipt",
            )
    existing_artifact = effective.execute(
        select(MARKET_ARTIFACTS_TABLE).where(
            MARKET_ARTIFACTS_TABLE.c.content_digest == content_digest
        )
    ).mappings().one_or_none()
    inspection_payload = {
        "contract": "canonical-v13-market-inspection-v2",
        "row_count": facts.row_count,
        "first_open_at": facts.first_open_at.astimezone(timezone.utc).isoformat(),
        "last_close_at": facts.last_close_at.astimezone(timezone.utc).isoformat(),
        "gap_count": facts.gap_count,
        "duplicate_count": facts.duplicate_count,
        "null_count": facts.null_count,
        "monotonic": facts.monotonic,
        "source_identity": facts.source_identity,
        "provenance_class": facts.provenance_class,
        "target_key": facts.target_key,
        "instrument": facts.instrument,
        "pair": facts.pair,
        "timeframe": facts.timeframe,
        "data_kind": facts.data_kind,
        "acquired_at": (
            facts.acquired_at.astimezone(timezone.utc).isoformat()
            if facts.acquired_at is not None and facts.acquired_at.tzinfo is not None
            else None
        ),
        "acquisition_receipt_digest": facts.acquisition_receipt_digest,
        "acquisition_receipt_json": (
            asdict(acquisition_receipt) if acquisition_receipt is not None else None
        ),
    }
    inspection_digest = _digest_json(inspection_payload)
    if existing_artifact is not None:
        artifact = dict(existing_artifact)
        if artifact["size_bytes"] != len(content) or artifact["media_type"] != media_type:
            raise CanonicalMarketBlocked(
                "BLOCKED_MARKET_ARTIFACT_DIGEST_COLLISION",
                "artifact metadata disagrees with content digest",
            )
        existing_inspection = effective.execute(
            select(MARKET_INSPECTIONS_TABLE).where(
                MARKET_INSPECTIONS_TABLE.c.market_artifact_id == artifact["id"],
                MARKET_INSPECTIONS_TABLE.c.inspection_digest == inspection_digest,
            )
        ).mappings().one_or_none()
        if existing_inspection is not None:
            receipt = effective.execute(
                select(MARKET_RECEIPTS_TABLE).where(
                    MARKET_RECEIPTS_TABLE.c.market_inspection_id
                    == existing_inspection["id"]
                )
            ).mappings().one()
            if receipt["status"] != "ACCEPTED":
                raise CanonicalMarketBlocked(
                    "BLOCKED_MARKET_RECEIPT_DRIFT", "replayed receipt is not accepted"
                )
            return MarketEvidenceResult(
                artifact_id=artifact["id"],
                inspection_id=existing_inspection["id"],
                receipt_id=receipt["id"],
                content_digest=content_digest,
                receipt_digest=receipt["receipt_digest"],
                idempotent_replay=True,
            )
        artifact_id = artifact["id"]
    else:
        artifact_id = uuid4()
        effective.execute(
            MARKET_ARTIFACTS_TABLE.insert().values(
                id=artifact_id,
                content_digest=content_digest,
                locator=locator,
                size_bytes=len(content),
                media_type=media_type,
                created_at=datetime.now(timezone.utc),
            )
        )
    inspection_id = uuid4()
    receipt_id = uuid4()
    receipt_digest = _digest_json(
        {
            "artifact_digest": content_digest,
            "inspection_digest": inspection_digest,
            "status": "ACCEPTED",
        }
    )
    now = datetime.now(timezone.utc)
    effective.execute(
        MARKET_INSPECTIONS_TABLE.insert().values(
            id=inspection_id,
            market_artifact_id=artifact_id,
            status="ACCEPTED",
            inspection_json=inspection_payload,
            inspection_digest=inspection_digest,
            inspector_identity=inspector_identity,
            created_at=now,
        )
    )
    effective.execute(
        MARKET_RECEIPTS_TABLE.insert().values(
            id=receipt_id,
            market_artifact_id=artifact_id,
            market_inspection_id=inspection_id,
            status="ACCEPTED",
            artifact_digest=content_digest,
            inspection_digest=inspection_digest,
            receipt_digest=receipt_digest,
            created_at=now,
        )
    )
    return MarketEvidenceResult(
        artifact_id=artifact_id,
        inspection_id=inspection_id,
        receipt_id=receipt_id,
        content_digest=content_digest,
        receipt_digest=receipt_digest,
        idempotent_replay=False,
    )


def seal_market_snapshot(
    connection: Connection,
    *,
    market_profile_version_id: UUID,
    members: tuple[tuple[UUID, UUID, UUID, datetime, datetime], ...],
) -> MarketSnapshotResult:
    """Seal exact artifact/accepted-receipt/target coverage members."""

    if not members:
        raise CanonicalMarketBlocked(
            "BLOCKED_MARKET_SNAPSHOT_EMPTY", "at least one coverage member is required"
        )
    effective = _require_canonical(connection)
    profile = effective.execute(
        select(MARKET_PROFILE_VERSIONS_TABLE).where(
            MARKET_PROFILE_VERSIONS_TABLE.c.id == market_profile_version_id
        )
    ).mappings().one_or_none()
    if profile is None or profile["lifecycle_status"] != "VALIDATED":
        raise CanonicalMarketBlocked(
            "BLOCKED_MARKET_PROFILE_NOT_VALIDATED",
            "snapshot requires a validated market profile",
        )
    normalized: list[dict[str, object]] = []
    observed_targets: set[UUID] = set()
    for artifact_id, receipt_id, target_id, start_at, end_at in members:
        if target_id in observed_targets or start_at.tzinfo is None or end_at <= start_at:
            raise CanonicalMarketBlocked(
                "BLOCKED_MARKET_COVERAGE", "target coverage is invalid or ambiguous"
            )
        observed_targets.add(target_id)
        target = effective.execute(
            select(RESEARCH_TARGETS_TABLE).where(RESEARCH_TARGETS_TABLE.c.id == target_id)
        ).mappings().one_or_none()
        receipt = effective.execute(
            select(MARKET_RECEIPTS_TABLE).where(MARKET_RECEIPTS_TABLE.c.id == receipt_id)
        ).mappings().one_or_none()
        if target is None or receipt is None or receipt["status"] != "ACCEPTED":
            raise CanonicalMarketBlocked(
                "BLOCKED_MARKET_RECEIPT_OR_TARGET",
                "snapshot member needs a canonical target and accepted receipt",
            )
        if receipt["market_artifact_id"] != artifact_id:
            raise CanonicalMarketBlocked(
                "BLOCKED_MARKET_RECEIPT_DIGEST_DRIFT",
                "receipt does not bind the requested artifact",
            )
        inspection = effective.execute(
            select(MARKET_INSPECTIONS_TABLE).where(
                MARKET_INSPECTIONS_TABLE.c.id == receipt["market_inspection_id"]
            )
        ).mappings().one_or_none()
        if inspection is None or inspection["status"] != "ACCEPTED":
            raise CanonicalMarketBlocked(
                "BLOCKED_MARKET_INSPECTION_UNSET",
                "accepted receipt must bind an accepted inspection",
            )
        inspection_json = inspection["inspection_json"]
        expected_start = start_at.astimezone(timezone.utc).isoformat()
        expected_end = end_at.astimezone(timezone.utc).isoformat()
        if (
            inspection_json.get("first_open_at") != expected_start
            or inspection_json.get("last_close_at") != expected_end
        ):
            raise CanonicalMarketBlocked(
                "BLOCKED_MARKET_INSPECTION_COVERAGE_MISMATCH",
                "snapshot coverage must exactly equal inspected coverage",
            )
        identity_facts = {
            "target_key": target["target_key"],
            "instrument": target["instrument"],
            "pair": target["pair"],
            "timeframe": target["timeframe"],
            "data_kind": target["data_kind"],
        }
        for key, expected in identity_facts.items():
            observed = inspection_json.get(key)
            if observed is not None and observed != expected:
                raise CanonicalMarketBlocked(
                    "BLOCKED_MARKET_INSPECTION_TARGET_MISMATCH",
                    f"inspection {key} does not match the canonical target",
                )
        normalized.append(
            {
                "artifact_id": str(artifact_id),
                "receipt_id": str(receipt_id),
                "receipt_digest": receipt["receipt_digest"],
                "target_id": str(target_id),
                "coverage_start": start_at.astimezone(timezone.utc).isoformat(),
                "coverage_end": end_at.astimezone(timezone.utc).isoformat(),
            }
        )
    normalized.sort(key=lambda item: item["target_id"])
    snapshot_digest = _digest_json(
        {
            "market_profile_version_id": str(market_profile_version_id),
            "members": normalized,
        }
    )
    existing = effective.execute(
        select(MARKET_SNAPSHOTS_TABLE).where(
            MARKET_SNAPSHOTS_TABLE.c.snapshot_digest == snapshot_digest
        )
    ).mappings().one_or_none()
    if existing is not None:
        return MarketSnapshotResult(
            snapshot_id=existing["id"],
            snapshot_digest=snapshot_digest,
            member_count=len(normalized),
            idempotent_replay=True,
        )
    snapshot_id = uuid4()
    effective.execute(
        MARKET_SNAPSHOTS_TABLE.insert().values(
            id=snapshot_id,
            market_profile_version_id=market_profile_version_id,
            snapshot_digest=snapshot_digest,
            created_at=datetime.now(timezone.utc),
        )
    )
    for source, normalized_member in zip(
        sorted(members, key=lambda item: str(item[2])), normalized
    ):
        artifact_id, receipt_id, target_id, start_at, end_at = source
        effective.execute(
            MARKET_SNAPSHOT_MEMBERS_TABLE.insert().values(
                id=uuid4(),
                market_snapshot_id=snapshot_id,
                market_artifact_id=artifact_id,
                market_receipt_id=receipt_id,
                research_target_id=target_id,
                coverage_start=start_at,
                coverage_end=end_at,
                coverage_digest=_digest_json(normalized_member),
            )
        )
    return MarketSnapshotResult(
        snapshot_id=snapshot_id,
        snapshot_digest=snapshot_digest,
        member_count=len(normalized),
        idempotent_replay=False,
    )


__all__ = [
    "CanonicalMarketBlocked",
    "MarketEvidenceResult",
    "MarketInspectionFacts",
    "MarketSnapshotResult",
    "accept_market_artifact",
    "create_market_profile_draft",
    "seal_market_snapshot",
    "validate_market_profile",
]
