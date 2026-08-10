from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from app.models.strategy_research import MarketDataQualityReceipt


QUALITY_CONTRACT_VERSION = "market-data-quality-v1"
REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")
DEFAULT_MAX_FRESHNESS_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class SourceMatrixVerification:
    status: str
    reason_codes: tuple[str, ...]
    receipt_path: str | None = None
    receipt_digest: str | None = None
    downloaded_at: datetime | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def verify_public_source_matrix(
    *,
    repository_root: Path,
    qualities: list[MarketDataQualityReceipt],
    inspected_at: datetime,
) -> SourceMatrixVerification:
    """Bind current candle contents and last opens to one public OKX receipt.

    Per-file sidecars prove each file's response-chain lineage.  This matrix
    check additionally requires all sidecars to name one download timestamp
    and binds that timestamp to the aggregate receipt, its digest, all six
    current file digests, row counts, and actual last-open timestamps.
    """

    inspected_at = _aware(inspected_at)
    reasons: set[str] = set()
    sidecar_downloads: set[datetime] = set()
    for quality in qualities:
        if not quality.source_receipt_path:
            reasons.add("SOURCE_MATRIX_SIDECAR_MISSING")
            continue
        sidecar_path = repository_root / quality.source_receipt_path
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            reasons.add("SOURCE_MATRIX_SIDECAR_INVALID")
            continue
        downloaded_at = _parse_datetime(sidecar.get("downloaded_at"))
        if downloaded_at is None:
            reasons.add("SOURCE_MATRIX_DOWNLOADED_AT_INVALID")
        else:
            sidecar_downloads.add(downloaded_at)
    if len(sidecar_downloads) != 1:
        reasons.add("SOURCE_MATRIX_DOWNLOAD_MISMATCH")
        return SourceMatrixVerification("BLOCKED", tuple(sorted(reasons)))
    downloaded_at = next(iter(sidecar_downloads))
    if downloaded_at > inspected_at:
        reasons.add("SOURCE_MATRIX_RECEIPT_FROM_FUTURE")

    matches: list[tuple[Path, dict[str, object]]] = []
    for path in (repository_root / "reports" / "research").glob(
        "okx-public-candle-source-*.json"
    ):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if _parse_datetime(payload.get("downloaded_at")) == downloaded_at:
            matches.append((path, payload))
    if len(matches) != 1:
        reasons.add(
            "SOURCE_MATRIX_RECEIPT_MISSING"
            if not matches
            else "SOURCE_MATRIX_RECEIPT_AMBIGUOUS"
        )
        return SourceMatrixVerification(
            "BLOCKED", tuple(sorted(reasons)), downloaded_at=downloaded_at
        )

    receipt_path, payload = matches[0]
    relative_receipt_path = str(
        receipt_path.resolve().relative_to(repository_root.resolve())
    )
    receipt_digest = _sha256(receipt_path)
    if (
        payload.get("schema_version") != "okx-public-candle-source-receipt-v1"
        or payload.get("execution_scope") != "PUBLIC_MARKET_DATA_ONLY"
        or payload.get("credentials_used") is not False
        or payload.get("account_endpoint_used") is not False
        or payload.get("orders_submitted") is not False
    ):
        reasons.add("SOURCE_MATRIX_RECEIPT_INVALID")
    sources = payload.get("sources")
    if not isinstance(sources, list):
        sources = []
        reasons.add("SOURCE_MATRIX_RECEIPT_INVALID")
    by_pair = {
        item.get("pair"): item
        for item in sources
        if isinstance(item, dict) and isinstance(item.get("pair"), str)
    }
    expected_pairs = {quality.pair for quality in qualities}
    if set(by_pair) != expected_pairs or len(sources) != len(expected_pairs):
        reasons.add("SOURCE_MATRIX_TARGET_SET_MISMATCH")

    for quality in qualities:
        source = by_pair.get(quality.pair)
        if not isinstance(source, dict):
            reasons.add("SOURCE_MATRIX_TARGET_MISSING")
            continue
        prefix = "five_minute" if quality.timeframe == "5m" else "fifteen_minute"
        if (
            source.get(f"{prefix}_path") != quality.relative_path
            or source.get(f"{prefix}_sha256") != quality.file_sha256
            or source.get(
                "row_count" if quality.timeframe == "5m" else "fifteen_minute_row_count"
            )
            != quality.row_count
        ):
            reasons.add("SOURCE_MATRIX_FILE_MISMATCH")
        source_first = _parse_datetime(source.get("first_open_at"))
        source_last = _parse_datetime(source.get("last_open_at"))
        if source_first != quality.first_open_at or source_last is None:
            reasons.add("SOURCE_MATRIX_TIME_RANGE_MISMATCH")
            continue
        expected_last = source_last
        if quality.timeframe == "15m":
            bucket = source_last.replace(
                minute=(source_last.minute // 15) * 15, second=0, microsecond=0
            )
            expected_last = (
                bucket
                if source_last >= bucket + timedelta(minutes=10)
                else bucket - timedelta(minutes=15)
            )
        if quality.last_open_at != expected_last or source_last > downloaded_at:
            reasons.add("SOURCE_MATRIX_TIME_RANGE_MISMATCH")

    return SourceMatrixVerification(
        "PASSED" if not reasons else "BLOCKED",
        tuple(sorted(reasons)),
        receipt_path=relative_receipt_path,
        receipt_digest=receipt_digest,
        downloaded_at=downloaded_at,
    )


def inspect_market_data(
    path: Path,
    *,
    repository_root: Path,
    exchange: str,
    pair: str,
    timeframe: str,
    expected_interval_seconds: int,
    inspected_at: datetime,
    max_freshness_seconds: int = DEFAULT_MAX_FRESHNESS_SECONDS,
    require_source_receipt: bool = False,
) -> MarketDataQualityReceipt:
    """Inspect one exact candle file and return an unsaved immutable receipt."""

    inspected_at = _aware(inspected_at)
    relative_path = str(path.resolve().relative_to(repository_root.resolve()))
    file_size = path.stat().st_size
    digest_before = _sha256(path)
    reasons: set[str] = set()
    row_count = 0
    first_open_at = None
    last_open_at = None
    missing = duplicate = out_of_order = misaligned = 0
    null_ohlcv = invalid_ohlc = negative_volume = 0
    freshness_seconds = None
    source_type = source_receipt_path = source_receipt_digest = None
    source_response_chain_digest = None
    source_path = path.with_suffix(path.suffix + ".source.json")
    if source_path.is_file():
        try:
            source_payload = json.loads(source_path.read_text(encoding="utf-8"))
            source_receipt_digest = _sha256(source_path)
            source_receipt_path = str(
                source_path.resolve().relative_to(repository_root.resolve())
            )
            source_type = source_payload.get("source_type")
            source_response_chain_digest = source_payload.get(
                "response_chain_sha256"
            )
            if (
                source_payload.get("schema_version")
                != "okx-public-candle-file-source-v1"
                or source_payload.get("credentials_used") is not False
                or source_payload.get("account_endpoint_used") is not False
                or source_payload.get("orders_submitted") is not False
                or source_payload.get("data_file_sha256") != digest_before
                or source_type not in {"OKX_PUBLIC_REST", "DERIVED_FROM_OKX_PUBLIC_REST"}
                or not isinstance(source_response_chain_digest, str)
                or len(source_response_chain_digest) != 64
            ):
                reasons.add("SOURCE_RECEIPT_INVALID_OR_MISMATCHED")
        except (OSError, ValueError, TypeError):
            reasons.add("SOURCE_RECEIPT_INVALID_OR_MISMATCHED")
    elif require_source_receipt:
        reasons.add("SOURCE_RECEIPT_MISSING")
    try:
        suffix = path.suffix.lower()
        if suffix == ".feather":
            frame = pd.read_feather(path)
        elif suffix == ".parquet":
            frame = pd.read_parquet(path)
        elif suffix == ".csv":
            frame = pd.read_csv(path)
        elif suffix == ".json":
            frame = pd.read_json(path)
        else:
            raise ValueError("unsupported market data format")
        missing_columns = sorted(set(REQUIRED_COLUMNS) - set(frame.columns))
        if missing_columns:
            reasons.add("REQUIRED_COLUMNS_MISSING")
        else:
            row_count = len(frame)
            timestamps = pd.to_datetime(frame["date"], utc=True, errors="coerce")
            if timestamps.isna().any():
                reasons.add("INVALID_TIMESTAMP")
            valid_timestamps = timestamps.dropna()
            if not valid_timestamps.empty:
                first_open_at = valid_timestamps.min().to_pydatetime()
                last_open_at = valid_timestamps.max().to_pydatetime()
                # Feather commonly preserves UTC timestamps at millisecond
                # precision.  ``astype("int64")`` then returns milliseconds,
                # so normalize explicitly before applying nanosecond intervals.
                nanoseconds = valid_timestamps.array.as_unit("ns").asi8
                interval_ns = expected_interval_seconds * 1_000_000_000
                misaligned = int((nanoseconds % interval_ns != 0).sum())
                diffs = valid_timestamps.diff().dt.total_seconds().dropna()
                out_of_order = int((diffs < 0).sum())
                duplicate = int(valid_timestamps.duplicated().sum())
                unique_sorted = valid_timestamps.drop_duplicates().sort_values()
                sorted_diffs = unique_sorted.diff().dt.total_seconds().dropna()
                missing = int(
                    sum(max(0, math.floor(value / expected_interval_seconds) - 1) for value in sorted_diffs)
                )
                expected_last_open = pd.Timestamp(inspected_at).floor(
                    f"{expected_interval_seconds}s"
                ) - pd.Timedelta(seconds=expected_interval_seconds)
                freshness_seconds = int(
                    (expected_last_open - valid_timestamps.max()).total_seconds()
                )
                if freshness_seconds < 0:
                    reasons.add("FUTURE_CANDLE")
                elif freshness_seconds > max_freshness_seconds:
                    reasons.add("STALE_LAST_CLOSED_CANDLE")
            numeric = frame[["open", "high", "low", "close", "volume"]].apply(
                pd.to_numeric, errors="coerce"
            )
            finite = numeric.map(lambda value: math.isfinite(value) if pd.notna(value) else False)
            null_ohlcv = int((~finite).any(axis=1).sum())
            valid = finite.all(axis=1)
            prices = numeric.loc[valid]
            invalid_ohlc = int(
                (
                    (prices["high"] < prices[["open", "close", "low"]].max(axis=1))
                    | (prices["low"] > prices[["open", "close", "high"]].min(axis=1))
                    | (prices[["open", "high", "low", "close"]] <= 0).any(axis=1)
                ).sum()
            )
            negative_volume = int((prices["volume"] < 0).sum())
            if row_count == 0:
                reasons.add("EMPTY_DATASET")
            if duplicate:
                reasons.add("DUPLICATE_TIMESTAMP")
            if out_of_order:
                reasons.add("OUT_OF_ORDER_TIMESTAMP")
            if missing:
                reasons.add("MISSING_INTERVAL")
            if misaligned:
                reasons.add("MISALIGNED_TIMESTAMP")
            if null_ohlcv:
                reasons.add("NULL_OR_NONFINITE_OHLCV")
            if invalid_ohlc:
                reasons.add("INVALID_OHLC")
            if negative_volume:
                reasons.add("NEGATIVE_VOLUME")
    except Exception:
        reasons.add("DATA_FILE_UNREADABLE")
    digest_after = _sha256(path)
    if digest_after != digest_before:
        reasons.add("FILE_CHANGED_DURING_INSPECTION")
    payload = {
        "contract_version": QUALITY_CONTRACT_VERSION,
        "exchange": exchange,
        "pair": pair,
        "timeframe": timeframe,
        "relative_path": relative_path,
        "file_format": path.suffix.lower().lstrip("."),
        "file_size": file_size,
        "file_sha256": digest_after,
        "source_type": source_type,
        "source_receipt_path": source_receipt_path,
        "source_receipt_digest": source_receipt_digest,
        "source_response_chain_digest": source_response_chain_digest,
        "inspected_at": inspected_at.isoformat(),
        "row_count": row_count,
        "first_open_at": first_open_at.isoformat() if first_open_at else None,
        "last_open_at": last_open_at.isoformat() if last_open_at else None,
        "expected_interval_seconds": expected_interval_seconds,
        "missing_interval_count": missing,
        "duplicate_timestamp_count": duplicate,
        "out_of_order_count": out_of_order,
        "misaligned_timestamp_count": misaligned,
        "null_ohlcv_count": null_ohlcv,
        "invalid_ohlc_count": invalid_ohlc,
        "negative_volume_count": negative_volume,
        "freshness_seconds": freshness_seconds,
        "status": "PASSED" if not reasons else "BLOCKED",
        "reason_codes": sorted(reasons),
    }
    evidence_digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload["inspected_at"] = inspected_at
    payload["first_open_at"] = first_open_at
    payload["last_open_at"] = last_open_at
    return MarketDataQualityReceipt(**payload, evidence_digest=evidence_digest)
