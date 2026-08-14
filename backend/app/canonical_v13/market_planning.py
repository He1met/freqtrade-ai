"""Derive fresh-market acquisition ranges from frozen canonical P0 snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from uuid import UUID

from sqlalchemy import Connection, select

from app.canonical_v13.control_plane import canonical_digest
from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import (
    CONFIGURATION_SNAPSHOTS_TABLE,
    RESEARCH_TARGETS_TABLE,
)


_TIMEFRAME = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>[mH])$")


class CanonicalMarketPlanningBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class FreshMarketPlan:
    target_snapshot_id: UUID
    target_snapshot_digest: str
    window_snapshot_id: UUID
    window_snapshot_digest: str
    research_target_id: UUID
    target_key: str
    instrument: str
    pair: str
    timeframe: str
    data_kind: str
    requested_start: datetime
    requested_end: datetime
    interval: timedelta
    minimum_closed_candles: int
    warmup_closed_candles: int
    integrity_margin_closed_candles: int
    freshness_max_age_seconds: int


def _effective(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise CanonicalMarketPlanningBlocked(
            "BLOCKED_WINDOW_RANGE_INVALID", f"{field} must be ISO-8601 text"
        )
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as exc:
        raise CanonicalMarketPlanningBlocked(
            "BLOCKED_WINDOW_RANGE_INVALID", f"{field} is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise CanonicalMarketPlanningBlocked(
            "BLOCKED_WINDOW_RANGE_INVALID", f"{field} lacks timezone"
        )
    return parsed.astimezone(timezone.utc)


def _nonnegative_int(value: object, *, field: str, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CanonicalMarketPlanningBlocked(
            "BLOCKED_WINDOW_ACQUISITION_MARGIN_UNSET", f"{field} must be an integer"
        )
    if value < (1 if positive else 0):
        raise CanonicalMarketPlanningBlocked(
            "BLOCKED_WINDOW_ACQUISITION_MARGIN_UNSET", f"{field} is out of range"
        )
    return value


def _interval(timeframe: str) -> timedelta:
    match = _TIMEFRAME.fullmatch(timeframe)
    if match is None:
        raise CanonicalMarketPlanningBlocked(
            "BLOCKED_MARKET_TIMEFRAME_UNSUPPORTED", timeframe
        )
    count = int(match.group("count"))
    return timedelta(minutes=count) if match.group("unit") == "m" else timedelta(hours=count)


def plan_fresh_market_acquisition(
    connection: Connection,
    *,
    target_snapshot_id: UUID,
    expected_target_snapshot_digest: str,
    window_snapshot_id: UUID,
    expected_window_snapshot_digest: str,
    target_key: str,
) -> FreshMarketPlan:
    """Bind one target to required windows; absent explicit margins is BLOCKED."""

    effective = _effective(connection)
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalMarketPlanningBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    snapshots = {}
    for kind, snapshot_id, expected_digest in (
        ("TARGET", target_snapshot_id, expected_target_snapshot_digest),
        ("WINDOW", window_snapshot_id, expected_window_snapshot_digest),
    ):
        row = effective.execute(
            select(CONFIGURATION_SNAPSHOTS_TABLE).where(
                CONFIGURATION_SNAPSHOTS_TABLE.c.id == snapshot_id
            )
        ).mappings().one_or_none()
        if (
            row is None
            or row["configuration_kind"] != kind
            or row["snapshot_digest"] != expected_digest
        ):
            raise CanonicalMarketPlanningBlocked(
                "BLOCKED_CONFIGURATION_SNAPSHOT_DRIFT", kind
            )
        snapshots[kind] = dict(row)
    target = effective.execute(
        select(RESEARCH_TARGETS_TABLE).where(
            RESEARCH_TARGETS_TABLE.c.target_snapshot_id == target_snapshot_id,
            RESEARCH_TARGETS_TABLE.c.target_key == target_key,
        )
    ).mappings().one_or_none()
    if target is None:
        raise CanonicalMarketPlanningBlocked("BLOCKED_TARGET_UNSET", target_key)
    payload = snapshots["WINDOW"]["snapshot_json"].get("payload_json")
    windows = payload.get("windows") if isinstance(payload, dict) else None
    if not isinstance(windows, list):
        raise CanonicalMarketPlanningBlocked(
            "BLOCKED_REQUIRED_WINDOWS_UNSET", "WINDOW payload is invalid"
        )
    required = [
        item
        for item in windows
        if isinstance(item, dict) and item.get("required") is True
    ]
    if not required:
        raise CanonicalMarketPlanningBlocked(
            "BLOCKED_REQUIRED_WINDOWS_UNSET", "no required windows"
        )
    starts: list[datetime] = []
    ends: list[datetime] = []
    minimums: list[int] = []
    warmups: list[int] = []
    margins: list[int] = []
    freshness_limits: list[int] = []
    for item in required:
        coverage = item.get("coverage")
        if not isinstance(coverage, dict):
            raise CanonicalMarketPlanningBlocked(
                "BLOCKED_WINDOW_ACQUISITION_MARGIN_UNSET", "coverage is invalid"
            )
        starts.append(_utc(item.get("start_at"), field="start_at"))
        ends.append(_utc(item.get("end_at"), field="end_at"))
        minimums.append(
            _nonnegative_int(
                coverage.get("minimum_closed_candles"),
                field="minimum_closed_candles",
                positive=True,
            )
        )
        warmups.append(
            _nonnegative_int(
                coverage.get("warmup_closed_candles"),
                field="warmup_closed_candles",
            )
        )
        margins.append(
            _nonnegative_int(
                coverage.get("integrity_margin_closed_candles"),
                field="integrity_margin_closed_candles",
            )
        )
        freshness_limits.append(
            _nonnegative_int(
                coverage.get("freshness_max_age_seconds"),
                field="freshness_max_age_seconds",
                positive=True,
            )
        )
    interval = _interval(str(target["timeframe"]))
    warmup = max(warmups)
    margin = max(margins)
    requested_start = min(starts) - interval * (warmup + margin)
    requested_end = max(ends)
    if (
        requested_start.timestamp() % interval.total_seconds() != 0
        or requested_end.timestamp() % interval.total_seconds() != 0
    ):
        raise CanonicalMarketPlanningBlocked(
            "BLOCKED_WINDOW_INTERVAL_ALIGNMENT", "window bounds must align to timeframe"
        )
    # Recompute stored target identity so a corrupted row cannot drive a request.
    target_facts = {
        key: target[key]
        for key in ("target_key", "instrument", "pair", "timeframe", "data_kind")
    }
    if canonical_digest(target_facts) != target["target_digest"]:
        raise CanonicalMarketPlanningBlocked("BLOCKED_TARGET_DIGEST_DRIFT", target_key)
    return FreshMarketPlan(
        target_snapshot_id=target_snapshot_id,
        target_snapshot_digest=expected_target_snapshot_digest,
        window_snapshot_id=window_snapshot_id,
        window_snapshot_digest=expected_window_snapshot_digest,
        research_target_id=target["id"],
        target_key=target["target_key"],
        instrument=target["instrument"],
        pair=target["pair"],
        timeframe=target["timeframe"],
        data_kind=target["data_kind"],
        requested_start=requested_start,
        requested_end=requested_end,
        interval=interval,
        minimum_closed_candles=max(minimums),
        warmup_closed_candles=warmup,
        integrity_margin_closed_candles=margin,
        freshness_max_age_seconds=min(freshness_limits),
    )


__all__ = [
    "CanonicalMarketPlanningBlocked",
    "FreshMarketPlan",
    "plan_fresh_market_acquisition",
]
