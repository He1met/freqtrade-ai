"""Atomic canonical registration for one freshly acquired public market artifact."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import os
from uuid import UUID

from sqlalchemy import Connection, select

from app.canonical_v13.market import (
    MarketInspectionFacts,
    accept_market_artifact,
    create_market_profile_draft,
    seal_market_snapshot,
    validate_market_profile,
)
from app.canonical_v13.market_acquisition import (
    CanonicalMarketAcquisitionBlocked,
    MarketAcquisitionRequest,
    MarketDownloaderPort,
    acquire_market_evidence,
)
from app.canonical_v13.market_planning import FreshMarketPlan
from app.canonical_v13.models import (
    MARKET_ARTIFACTS_TABLE,
    MARKET_INSPECTIONS_TABLE,
    MARKET_RECEIPTS_TABLE,
    MARKET_SNAPSHOTS_TABLE,
    MARKET_SNAPSHOT_MEMBERS_TABLE,
)


class CanonicalFreshMarketRolloutBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class FreshMarketRolloutResult:
    market_profile_version_id: UUID
    artifact_id: UUID
    receipt_id: UUID
    market_snapshot_id: UUID
    market_snapshot_digest: str
    artifact_locator: str
    artifact_digest: str
    artifact_file_replay: bool
    database_replay: bool


def persist_immutable_market_artifact(
    *, root: Path, locator: str, content: bytes
) -> tuple[Path, bool]:
    """Create one digest-named artifact without following symlinked parents."""

    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise CanonicalFreshMarketRolloutBlocked(
            "BLOCKED_MARKET_ARTIFACT_ROOT", "root must be an existing absolute directory"
        )
    parts = locator.split("/")
    if (
        not content
        or not locator
        or locator.startswith("/")
        or "\\" in locator
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise CanonicalFreshMarketRolloutBlocked(
            "BLOCKED_MARKET_ARTIFACT_LOCATOR", "locator is not root-relative POSIX"
        )
    expected_digest = sha256(content).hexdigest()
    if not parts[-1].endswith(f"-{expected_digest}.jsonl"):
        raise CanonicalFreshMarketRolloutBlocked(
            "BLOCKED_MARKET_ARTIFACT_DIGEST_PATH", "locator does not bind content"
        )
    parent = root
    for part in parts[:-1]:
        parent = parent / part
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise CanonicalFreshMarketRolloutBlocked(
                "BLOCKED_MARKET_ARTIFACT_PATH", "artifact parent is unsafe"
            )
        parent.mkdir(mode=0o700, exist_ok=True)
    destination = parent / parts[-1]
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if destination.is_symlink() or destination.read_bytes() != content:
            raise CanonicalFreshMarketRolloutBlocked(
                "BLOCKED_MARKET_ARTIFACT_IMMUTABILITY", "existing artifact differs"
            )
        return destination, True
    complete = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        complete = True
    finally:
        if not complete:
            destination.unlink(missing_ok=True)
    return destination, False


def acquire_register_and_seal_fresh_market(
    connection: Connection,
    *,
    plan: FreshMarketPlan,
    downloader: MarketDownloaderPort,
    artifact_root: Path,
    observed_at: datetime,
    profile_key: str,
    scope_key: str,
    inspector_identity: str,
) -> FreshMarketRolloutResult:
    """Acquire through the port, persist immutable bytes, then append canonical evidence."""

    if observed_at.tzinfo is None:
        raise CanonicalFreshMarketRolloutBlocked(
            "BLOCKED_MARKET_TIMEZONE_UNSET", "observed_at must be timezone-aware"
        )
    request = MarketAcquisitionRequest(
        source_identity="okx-public-history-candles-v1",
        target_key=plan.target_key,
        instrument=plan.instrument,
        pair=plan.pair,
        timeframe=plan.timeframe,
        data_kind=plan.data_kind,
        requested_start=plan.requested_start,
        requested_end=plan.requested_end,
    )
    try:
        payload, receipt = acquire_market_evidence(
            request, downloader=downloader, observed_at=observed_at
        )
    except CanonicalMarketAcquisitionBlocked:
        raise
    required_count = int(
        (plan.requested_end - plan.requested_start) / plan.interval
    )
    if (
        payload.observed_closed_candles != required_count
        or payload.observed_closed_candles
        < plan.minimum_closed_candles
        + plan.warmup_closed_candles
        + plan.integrity_margin_closed_candles
    ):
        raise CanonicalFreshMarketRolloutBlocked(
            "BLOCKED_MARKET_CANDLE_COUNT", "artifact does not satisfy the frozen plan"
        )
    acquired_at = observed_at.astimezone(timezone.utc)
    last_close = payload.observed_last_close.astimezone(timezone.utc)
    age_seconds = (acquired_at - last_close).total_seconds()
    if age_seconds < 0 or age_seconds > plan.freshness_max_age_seconds:
        raise CanonicalFreshMarketRolloutBlocked(
            "BLOCKED_MARKET_EVIDENCE_STALE", f"age_seconds={int(age_seconds)}"
        )
    _path, file_replay = persist_immutable_market_artifact(
        root=artifact_root, locator=payload.locator, content=payload.content
    )
    profile_payload = {
        "contract": "canonical-v13-fresh-public-market-profile-v1",
        "source_identity": request.source_identity,
        "provenance_class": downloader.provenance_class,
        "network_access": downloader.network_access,
        "credential_access": downloader.credential_access,
        "target_snapshot_id": str(plan.target_snapshot_id),
        "target_snapshot_digest": plan.target_snapshot_digest,
        "window_snapshot_id": str(plan.window_snapshot_id),
        "window_snapshot_digest": plan.window_snapshot_digest,
        "target_key": plan.target_key,
        "requested_start": plan.requested_start.astimezone(timezone.utc).isoformat(),
        "requested_end": plan.requested_end.astimezone(timezone.utc).isoformat(),
        "minimum_closed_candles": plan.minimum_closed_candles,
        "warmup_closed_candles": plan.warmup_closed_candles,
        "integrity_margin_closed_candles": plan.integrity_margin_closed_candles,
        "freshness_max_age_seconds": plan.freshness_max_age_seconds,
    }
    _profile_id, profile_version_id, _profile_digest = create_market_profile_draft(
        connection,
        profile_key=profile_key,
        scope_key=scope_key,
        payload=profile_payload,
    )
    validate_market_profile(connection, version_id=profile_version_id)
    content_digest = sha256(payload.content).hexdigest()
    prior = connection.execute(
        select(
            MARKET_ARTIFACTS_TABLE.c.id.label("artifact_id"),
            MARKET_ARTIFACTS_TABLE.c.locator,
            MARKET_ARTIFACTS_TABLE.c.size_bytes,
            MARKET_ARTIFACTS_TABLE.c.media_type,
            MARKET_RECEIPTS_TABLE.c.id.label("receipt_id"),
            MARKET_RECEIPTS_TABLE.c.status.label("receipt_status"),
            MARKET_INSPECTIONS_TABLE.c.status.label("inspection_status"),
            MARKET_SNAPSHOTS_TABLE.c.id.label("snapshot_id"),
            MARKET_SNAPSHOTS_TABLE.c.snapshot_digest,
        )
        .select_from(
            MARKET_ARTIFACTS_TABLE.join(
                MARKET_RECEIPTS_TABLE,
                MARKET_RECEIPTS_TABLE.c.market_artifact_id
                == MARKET_ARTIFACTS_TABLE.c.id,
            )
            .join(
                MARKET_INSPECTIONS_TABLE,
                MARKET_INSPECTIONS_TABLE.c.id
                == MARKET_RECEIPTS_TABLE.c.market_inspection_id,
            )
            .join(
                MARKET_SNAPSHOT_MEMBERS_TABLE,
                MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_artifact_id
                == MARKET_ARTIFACTS_TABLE.c.id,
            )
            .join(
                MARKET_SNAPSHOTS_TABLE,
                MARKET_SNAPSHOTS_TABLE.c.id
                == MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_snapshot_id,
            )
        )
        .where(
            MARKET_ARTIFACTS_TABLE.c.content_digest == content_digest,
            MARKET_SNAPSHOTS_TABLE.c.market_profile_version_id
            == profile_version_id,
            MARKET_SNAPSHOT_MEMBERS_TABLE.c.research_target_id
            == plan.research_target_id,
            MARKET_SNAPSHOT_MEMBERS_TABLE.c.coverage_start
            == payload.observed_first_open,
            MARKET_SNAPSHOT_MEMBERS_TABLE.c.coverage_end
            == payload.observed_last_close,
        )
    ).mappings().all()
    if len(prior) > 1:
        raise CanonicalFreshMarketRolloutBlocked(
            "BLOCKED_MARKET_REPLAY_AMBIGUOUS", "multiple accepted lineages match"
        )
    if prior:
        row = prior[0]
        if (
            row["locator"] != payload.locator
            or row["size_bytes"] != len(payload.content)
            or row["media_type"] != payload.media_type
            or row["receipt_status"] != "ACCEPTED"
            or row["inspection_status"] != "ACCEPTED"
        ):
            raise CanonicalFreshMarketRolloutBlocked(
                "BLOCKED_MARKET_REPLAY_DRIFT", "persisted lineage differs"
            )
        return FreshMarketRolloutResult(
            market_profile_version_id=profile_version_id,
            artifact_id=row["artifact_id"],
            receipt_id=row["receipt_id"],
            market_snapshot_id=row["snapshot_id"],
            market_snapshot_digest=row["snapshot_digest"],
            artifact_locator=payload.locator,
            artifact_digest=content_digest,
            artifact_file_replay=file_replay,
            database_replay=True,
        )
    evidence = accept_market_artifact(
        connection,
        locator=payload.locator,
        content=payload.content,
        media_type=payload.media_type,
        inspector_identity=inspector_identity,
        facts=MarketInspectionFacts(
            row_count=payload.observed_closed_candles,
            first_open_at=payload.observed_first_open,
            last_close_at=payload.observed_last_close,
            gap_count=0,
            duplicate_count=0,
            null_count=0,
            monotonic=True,
            source_identity=receipt.source_identity,
            provenance_class=receipt.provenance_class,
            target_key=receipt.target_key,
            instrument=receipt.instrument,
            pair=receipt.pair,
            timeframe=receipt.timeframe,
            data_kind=receipt.data_kind,
            acquired_at=acquired_at,
            acquisition_receipt_digest=receipt.receipt_digest,
        ),
        acquisition_receipt=receipt,
    )
    snapshot = seal_market_snapshot(
        connection,
        market_profile_version_id=profile_version_id,
        members=(
            (
                evidence.artifact_id,
                evidence.receipt_id,
                plan.research_target_id,
                payload.observed_first_open,
                payload.observed_last_close,
            ),
        ),
    )
    return FreshMarketRolloutResult(
        market_profile_version_id=profile_version_id,
        artifact_id=evidence.artifact_id,
        receipt_id=evidence.receipt_id,
        market_snapshot_id=snapshot.snapshot_id,
        market_snapshot_digest=snapshot.snapshot_digest,
        artifact_locator=payload.locator,
        artifact_digest=evidence.content_digest,
        artifact_file_replay=file_replay,
        database_replay=evidence.idempotent_replay and snapshot.idempotent_replay,
    )


__all__ = [
    "CanonicalFreshMarketRolloutBlocked",
    "FreshMarketRolloutResult",
    "acquire_register_and_seal_fresh_market",
    "persist_immutable_market_artifact",
]
