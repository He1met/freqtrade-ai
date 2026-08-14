"""Pure Phase 7 market/window/freshness readiness authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping
from uuid import UUID

from sqlalchemy import Connection, select

from app.canonical_v13.market_acquisition import (
    MarketAcquisitionReceipt,
    verify_market_acquisition_receipt,
)
from app.canonical_v13.models import (
    MARKET_INSPECTIONS_TABLE,
    MARKET_RECEIPTS_TABLE,
    MARKET_SNAPSHOT_MEMBERS_TABLE,
)
from app.canonical_v13.runtime_reader import (
    CanonicalFrozenReaderBlocked,
    read_frozen_research_bundle,
)


@dataclass(frozen=True)
class RequiredWindowEvidence:
    window_key: str
    required: bool
    start_at: datetime
    end_at: datetime
    minimum_closed_candles: int


@dataclass(frozen=True)
class MarketTargetEvidence:
    target_key: str
    instrument: str
    pair: str
    timeframe: str
    data_kind: str
    coverage_start: datetime
    coverage_end: datetime
    inspection_first_open: datetime
    inspection_last_close: datetime
    inspection_row_count: int
    acquired_at: datetime | None
    provenance_class: str
    acquisition_receipt_valid: bool


@dataclass(frozen=True)
class ProductionActivationReadiness:
    status: str
    reason_codes: tuple[str, ...]


def _aware(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None


def assess_production_activation_readiness(
    *,
    target_facts: Mapping[str, tuple[str, str, str, str]],
    required_windows: tuple[RequiredWindowEvidence, ...],
    market_evidence: tuple[MarketTargetEvidence, ...],
    evaluated_at: datetime,
    maximum_age: timedelta,
) -> ProductionActivationReadiness:
    """Join exact target/window/market evidence without mutating receipts."""

    reasons: list[str] = []
    if not _aware(evaluated_at) or maximum_age <= timedelta(0):
        reasons.append("MARKET_FRESHNESS_POLICY_INVALID")
    required = tuple(window for window in required_windows if window.required)
    if not required:
        reasons.append("REQUIRED_WINDOWS_UNSET")
    valid_required: list[RequiredWindowEvidence] = []
    for window in required:
        if (
            not window.window_key
            or not _aware(window.start_at)
            or not _aware(window.end_at)
            or window.end_at <= window.start_at
            or isinstance(window.minimum_closed_candles, bool)
            or window.minimum_closed_candles <= 0
        ):
            reasons.append("REQUIRED_WINDOW_CONTRACT_INVALID")
        else:
            valid_required.append(window)
    by_target: dict[str, MarketTargetEvidence] = {}
    for evidence in market_evidence:
        if evidence.target_key in by_target:
            reasons.append("MARKET_TARGET_COVERAGE_AMBIGUOUS")
        by_target[evidence.target_key] = evidence
    if set(by_target) != set(target_facts):
        reasons.append("MARKET_TARGET_COVERAGE_MISMATCH")
    for target_key, expected in target_facts.items():
        evidence = by_target.get(target_key)
        if evidence is None:
            continue
        if (
            evidence.instrument,
            evidence.pair,
            evidence.timeframe,
            evidence.data_kind,
        ) != expected:
            reasons.append("MARKET_TARGET_IDENTITY_MISMATCH")
        if not all(
            _aware(value)
            for value in (
                evidence.coverage_start,
                evidence.coverage_end,
                evidence.inspection_first_open,
                evidence.inspection_last_close,
            )
        ):
            reasons.append("MARKET_COVERAGE_TIMEZONE_UNSET")
            continue
        if (
            evidence.coverage_start != evidence.inspection_first_open
            or evidence.coverage_end != evidence.inspection_last_close
        ):
            reasons.append("MARKET_INSPECTION_COVERAGE_MISMATCH")
        for window in valid_required:
            if (
                evidence.coverage_start > window.start_at
                or evidence.coverage_end < window.end_at
            ):
                reasons.append(f"REQUIRED_WINDOW_COVERAGE_MISSING:{window.window_key}")
            if evidence.inspection_row_count < window.minimum_closed_candles:
                reasons.append(f"REQUIRED_WINDOW_CANDLE_COUNT_LOW:{window.window_key}")
        if evidence.provenance_class != "PRODUCTION_PUBLIC_MARKET_DATA":
            reasons.append("MARKET_PROVENANCE_NOT_PRODUCTION")
        if evidence.acquisition_receipt_valid is not True:
            reasons.append("MARKET_ACQUISITION_RECEIPT_INVALID")
        if not _aware(evidence.acquired_at):
            reasons.append("MARKET_ACQUISITION_RECEIPT_UNSET")
        elif _aware(evaluated_at) and evaluated_at - evidence.acquired_at > maximum_age:
            reasons.append("MARKET_EVIDENCE_STALE")
        elif _aware(evaluated_at) and evidence.acquired_at > evaluated_at:
            reasons.append("MARKET_ACQUISITION_IN_FUTURE")
    unique = tuple(dict.fromkeys(reasons))
    return ProductionActivationReadiness(
        status="BLOCKED" if unique else "READY", reason_codes=unique
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _as_utc(value: datetime) -> datetime:
    # SQLite drops timezone metadata in isolated tests; canonical writers use UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def assess_persisted_bundle_activation_readiness(
    connection: Connection,
    *,
    configuration_bundle_id: UUID,
    expected_bundle_digest: str,
    evaluated_at: datetime,
    maximum_age: timedelta,
) -> ProductionActivationReadiness:
    """Evaluate exact persisted canonical evidence; never mutate history."""

    try:
        frozen = read_frozen_research_bundle(
            connection,
            configuration_bundle_id=configuration_bundle_id,
            expected_bundle_digest=expected_bundle_digest,
        )
    except CanonicalFrozenReaderBlocked as exc:
        return ProductionActivationReadiness(
            status="BLOCKED", reason_codes=(exc.code,)
        )
    target_by_id = {
        target.research_target_id: target for target in frozen.targets
    }
    target_facts = {
        target.target_key: (
            target.instrument,
            target.pair,
            target.timeframe,
            target.data_kind,
        )
        for target in frozen.targets
    }
    required_windows = tuple(
        RequiredWindowEvidence(
            window_key=window.window_key,
            required=window.required,
            start_at=datetime.fromisoformat(window.start_at),
            end_at=datetime.fromisoformat(window.end_at),
            minimum_closed_candles=window.minimum_closed_candles,
        )
        for window in frozen.windows
    )
    evidence_rows: list[MarketTargetEvidence] = []
    reasons: list[str] = []
    members = connection.execute(
        select(MARKET_SNAPSHOT_MEMBERS_TABLE).where(
            MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_snapshot_id
            == frozen.market_snapshot_id
        )
    ).mappings().all()
    for member in members:
        target = target_by_id.get(member["research_target_id"])
        receipt = connection.execute(
            select(MARKET_RECEIPTS_TABLE).where(
                MARKET_RECEIPTS_TABLE.c.id == member["market_receipt_id"]
            )
        ).mappings().one_or_none()
        inspection = (
            connection.execute(
                select(MARKET_INSPECTIONS_TABLE).where(
                    MARKET_INSPECTIONS_TABLE.c.id
                    == receipt["market_inspection_id"]
                )
            ).mappings().one_or_none()
            if receipt is not None
            else None
        )
        if target is None or receipt is None or inspection is None:
            reasons.append("MARKET_EVIDENCE_LINEAGE_INCOMPLETE")
            continue
        facts = inspection["inspection_json"]
        acquisition_json = facts.get("acquisition_receipt_json")
        acquisition_valid = False
        if isinstance(acquisition_json, dict):
            try:
                acquisition = MarketAcquisitionReceipt(**acquisition_json)
            except TypeError:
                acquisition = None
            acquisition_valid = bool(
                acquisition is not None
                and verify_market_acquisition_receipt(acquisition)
                and acquisition.receipt_digest
                == facts.get("acquisition_receipt_digest")
                and acquisition.credential_access == "NONE"
                and acquisition.network_access == "PUBLIC_MARKET_DATA_ONLY"
            )
        first_open = _parse_time(facts.get("first_open_at"))
        last_close = _parse_time(facts.get("last_close_at"))
        acquired_at = _parse_time(facts.get("acquired_at"))
        if first_open is None or last_close is None:
            reasons.append("MARKET_INSPECTION_TIME_INVALID")
            continue
        if not all(
            facts.get(key)
            for key in ("target_key", "instrument", "pair", "timeframe", "data_kind")
        ):
            reasons.append("MARKET_INSPECTION_TARGET_IDENTITY_UNSET")
        evidence_rows.append(
            MarketTargetEvidence(
                target_key=str(facts.get("target_key") or target.target_key),
                instrument=str(facts.get("instrument") or "UNSET"),
                pair=str(facts.get("pair") or "UNSET"),
                timeframe=str(facts.get("timeframe") or "UNSET"),
                data_kind=str(facts.get("data_kind") or "UNSET"),
                coverage_start=_as_utc(member["coverage_start"]),
                coverage_end=_as_utc(member["coverage_end"]),
                inspection_first_open=first_open,
                inspection_last_close=last_close,
                inspection_row_count=int(facts.get("row_count") or 0),
                acquired_at=acquired_at,
                provenance_class=str(facts.get("provenance_class") or "UNSET"),
                acquisition_receipt_valid=acquisition_valid,
            )
        )
    assessment = assess_production_activation_readiness(
        target_facts=target_facts,
        required_windows=required_windows,
        market_evidence=tuple(evidence_rows),
        evaluated_at=evaluated_at,
        maximum_age=maximum_age,
    )
    combined = tuple(dict.fromkeys((*reasons, *assessment.reason_codes)))
    return ProductionActivationReadiness(
        status="BLOCKED" if combined else "READY", reason_codes=combined
    )


__all__ = [
    "MarketTargetEvidence",
    "ProductionActivationReadiness",
    "RequiredWindowEvidence",
    "assess_production_activation_readiness",
    "assess_persisted_bundle_activation_readiness",
]
