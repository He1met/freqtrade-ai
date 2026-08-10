from datetime import datetime, timezone
import hashlib
import json

import pandas as pd
import pytest

from app.services.market_data_quality import inspect_market_data


NOW = datetime(2026, 8, 9, 5, 22, tzinfo=timezone.utc)


def write_frame(tmp_path, rows):
    path = tmp_path / "data" / "candles.feather"
    path.parent.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_feather(path)
    return path


def good_rows():
    dates = pd.date_range("2026-08-09T04:00:00Z", periods=5, freq="15min")
    return [
        {"date": date, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 2}
        for date in dates
    ]


def inspect(path, tmp_path):
    return inspect_market_data(
        path,
        repository_root=tmp_path,
        exchange="okx",
        pair="BTC/USDT:USDT",
        timeframe="15m",
        expected_interval_seconds=900,
        inspected_at=NOW,
    )


def test_complete_aligned_feather_passes(tmp_path):
    receipt = inspect(write_frame(tmp_path, good_rows()), tmp_path)
    assert receipt.status == "PASSED"
    assert receipt.reason_codes == []
    assert receipt.row_count == 5
    assert receipt.relative_path == "data/candles.feather"
    assert len(receipt.file_sha256) == len(receipt.evidence_digest) == 64


def test_millisecond_precision_feather_is_normalized_before_alignment_check(tmp_path):
    rows = good_rows()
    frame = pd.DataFrame(rows)
    frame["date"] = frame["date"].astype("datetime64[ms, UTC]")
    path = tmp_path / "data" / "candles.feather"
    path.parent.mkdir(exist_ok=True)
    frame.to_feather(path)

    receipt = inspect(path, tmp_path)

    assert receipt.status == "PASSED"
    assert receipt.misaligned_timestamp_count == 0


def test_required_public_source_receipt_is_digest_bound(tmp_path):
    path = write_frame(tmp_path, good_rows())
    missing = inspect_market_data(
        path,
        repository_root=tmp_path,
        exchange="okx",
        pair="BTC/USDT:USDT",
        timeframe="15m",
        expected_interval_seconds=900,
        inspected_at=NOW,
        require_source_receipt=True,
    )
    assert "SOURCE_RECEIPT_MISSING" in missing.reason_codes

    file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    source_path = path.with_suffix(path.suffix + ".source.json")
    source_path.write_text(json.dumps({
        "schema_version": "okx-public-candle-file-source-v1",
        "source_type": "DERIVED_FROM_OKX_PUBLIC_REST",
        "credentials_used": False,
        "account_endpoint_used": False,
        "orders_submitted": False,
        "data_file_sha256": file_digest,
        "response_chain_sha256": "a" * 64,
    }))
    passed = inspect_market_data(
        path,
        repository_root=tmp_path,
        exchange="okx",
        pair="BTC/USDT:USDT",
        timeframe="15m",
        expected_interval_seconds=900,
        inspected_at=NOW,
        require_source_receipt=True,
    )
    assert passed.status == "PASSED"
    assert passed.source_receipt_digest == hashlib.sha256(source_path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda rows: rows.pop(2), "MISSING_INTERVAL"),
        (lambda rows: rows.__setitem__(2, dict(rows[1])), "DUPLICATE_TIMESTAMP"),
        (lambda rows: rows.reverse(), "OUT_OF_ORDER_TIMESTAMP"),
        (lambda rows: rows[2].update(date=pd.Timestamp("2026-08-09T04:31:00Z")), "MISALIGNED_TIMESTAMP"),
        (lambda rows: rows[2].update(high=98), "INVALID_OHLC"),
        (lambda rows: rows[2].update(volume=-1), "NEGATIVE_VOLUME"),
        (lambda rows: rows[2].update(close=float("nan")), "NULL_OR_NONFINITE_OHLCV"),
    ],
)
def test_structural_defects_fail_closed(tmp_path, mutation, reason):
    rows = good_rows()
    mutation(rows)
    receipt = inspect(write_frame(tmp_path, rows), tmp_path)
    assert receipt.status == "BLOCKED"
    assert reason in receipt.reason_codes


def test_missing_columns_and_stale_last_candle_fail_closed(tmp_path):
    path = tmp_path / "data" / "candles.feather"
    path.parent.mkdir(exist_ok=True)
    pd.DataFrame({"date": [NOW], "close": [1]}).to_feather(path)
    missing = inspect(path, tmp_path)
    stale_path = write_frame(tmp_path, [
        {"date": pd.Timestamp("2025-01-01T00:00:00Z"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}
    ])
    stale = inspect(stale_path, tmp_path)
    assert "REQUIRED_COLUMNS_MISSING" in missing.reason_codes
    assert "STALE_LAST_CLOSED_CANDLE" in stale.reason_codes
