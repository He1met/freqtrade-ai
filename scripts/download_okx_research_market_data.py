#!/usr/bin/env python3
"""Download public OKX closed 5m candles and deterministically derive 15m data.

No account endpoint, credential, database, runtime, or order path is used.  The
script writes each pair atomically only after the complete interval and digest
checks pass, and emits a source receipt containing the response-chain digest.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


ENDPOINT = "https://www.okx.com/api/v5/market/history-candles"
INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")
INTERVAL_MS = 5 * 60 * 1000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _request_page(instrument: str, after: int | None) -> tuple[list[list[str]], bytes]:
    params = {"instId": instrument, "bar": "5m", "limit": "100"}
    if after is not None:
        params["after"] = str(after)
    url = ENDPOINT + "?" + urlencode(params)
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            request = Request(url, headers={"User-Agent": "freqtrade-ai-market-audit/1"})
            with urlopen(request, timeout=30) as response:
                body = response.read()
            payload = json.loads(body)
            if payload.get("code") != "0" or not isinstance(payload.get("data"), list):
                raise RuntimeError("OKX returned a non-success candle response")
            return payload["data"], body
        except Exception as exc:  # network and API failures are retryable, never accepted
            last_error = exc
            time.sleep(min(8.0, 0.5 * (2**attempt)))
    raise RuntimeError(f"OKX candle request failed after retries: {type(last_error).__name__}")


def download_pair(instrument: str, start_ms: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows: dict[int, list[str]] = {}
    response_chain = hashlib.sha256()
    after: int | None = None
    page_count = 0
    while True:
        page, body = _request_page(instrument, after)
        if not page:
            break
        page_count += 1
        response_chain.update(hashlib.sha256(body).digest())
        timestamps = []
        for row in page:
            if not isinstance(row, list) or len(row) != 9:
                raise RuntimeError("OKX candle row shape changed")
            timestamp = int(row[0])
            timestamps.append(timestamp)
            if row[8] == "1" and timestamp >= start_ms:
                rows[timestamp] = row
        oldest = min(timestamps)
        if oldest <= start_ms:
            break
        if after is not None and oldest >= after:
            raise RuntimeError("OKX candle pagination did not move backwards")
        after = oldest
        if page_count % 100 == 0:
            print(f"{instrument}: pages={page_count} oldest={oldest}", flush=True)
        time.sleep(0.11)
    if not rows:
        raise RuntimeError(f"OKX returned no closed candles for {instrument}")
    ordered = [rows[key] for key in sorted(rows)]
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime([int(row[0]) for row in ordered], unit="ms", utc=True),
            "open": [float(row[1]) for row in ordered],
            "high": [float(row[2]) for row in ordered],
            "low": [float(row[3]) for row in ordered],
            "close": [float(row[4]) for row in ordered],
            # Freqtrade's OKX futures adapter stores volCcy (base currency).
            "volume": [float(row[6]) for row in ordered],
        }
    )
    diffs = frame["date"].diff().dropna().dt.total_seconds()
    if frame["date"].duplicated().any() or (diffs != 300).any():
        raise RuntimeError(f"OKX {instrument} 5m interval is incomplete")
    return frame, {
        "endpoint": ENDPOINT,
        "instrument_id": instrument,
        "bar": "5m",
        "closed_candles_only": True,
        "page_count": page_count,
        "response_chain_sha256": response_chain.hexdigest(),
        "row_count": len(frame),
        "first_open_at": frame["date"].iloc[0].isoformat(),
        "last_open_at": frame["date"].iloc[-1].isoformat(),
    }


def refresh_pair(
    instrument: str,
    start_ms: int,
    current_path: Path,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Merge a bounded public refresh without ever replacing newer data.

    Six hours of overlap makes the boundary independently checkable while the
    retained historical frame continues to cover all official OOS windows.
    """

    current: pd.DataFrame | None = None
    request_start_ms = start_ms
    if current_path.is_file():
        current = pd.read_feather(current_path)
        _validate_frame(current, instrument=instrument)
        last_ms = int(current["date"].iloc[-1].timestamp() * 1000)
        request_start_ms = max(start_ms, last_ms - 6 * 60 * 60 * 1000)
    refreshed, source = download_pair(instrument, request_start_ms)
    if current is not None:
        if refreshed["date"].iloc[-1] < current["date"].iloc[-1]:
            raise RuntimeError(f"OKX {instrument} refresh is older than installed data")
        refreshed = (
            pd.concat([current, refreshed], ignore_index=True)
            .drop_duplicates(subset=["date"], keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
    _validate_frame(refreshed, instrument=instrument)
    source.update(
        {
            "refresh_mode": "incremental_overlap" if current is not None else "full",
            "request_start_ms": request_start_ms,
        }
    )
    return refreshed, source


def _validate_frame(frame: pd.DataFrame, *, instrument: str) -> None:
    required = {"date", "open", "high", "low", "close", "volume"}
    if set(frame.columns) != required or frame.empty:
        raise RuntimeError(f"OKX {instrument} frame contract is invalid")
    dates = pd.to_datetime(frame["date"], utc=True)
    diffs = dates.diff().dropna().dt.total_seconds()
    if dates.duplicated().any() or not dates.is_monotonic_increasing or (diffs != 300).any():
        raise RuntimeError(f"OKX {instrument} 5m interval is incomplete")


def derive_15m(frame: pd.DataFrame) -> pd.DataFrame:
    indexed = frame.set_index("date")
    derived = indexed.resample("15min", origin="epoch", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    counts = indexed["close"].resample(
        "15min", origin="epoch", label="left", closed="left"
    ).count()
    derived = derived[counts == 3].dropna().reset_index()
    if derived.empty:
        raise RuntimeError("15m derivation produced no complete buckets")
    return derived


def _write_atomic(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_feather(temporary)
    os.replace(temporary, path)


def _write_json_atomic(payload: dict[str, Any], path: Path) -> None:
    content = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _promote_complete_refresh(staged: list[tuple[Path, Path]]) -> None:
    """Promote the exact refreshed file set with rollback on any failure."""

    backups: list[tuple[Path, Path]] = []
    promoted: list[Path] = []
    try:
        for source, target in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = target.with_suffix(target.suffix + ".refresh.bak")
            if backup.exists():
                raise RuntimeError(f"stale refresh backup exists: {backup}")
            if target.exists():
                os.replace(target, backup)
                backups.append((backup, target))
            os.replace(source, target)
            promoted.append(target)
    except BaseException:
        for target in reversed(promoted):
            if target.exists():
                target.unlink()
        for backup, target in reversed(backups):
            if backup.exists():
                os.replace(backup, target)
        raise
    for backup, _target in backups:
        backup.unlink()


def _freqtrade_stem(instrument: str) -> str:
    base, quote, product = instrument.split("-")
    if quote != "USDT" or product != "SWAP":
        raise RuntimeError("only the locked USDT perpetual instruments are supported")
    return f"{base}_{quote}_{quote}"


def _install_freqtrade_paths(receipt_path: Path) -> None:
    """Recoverably replace stale Freqtrade files with one downloaded receipt."""

    saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    moves: list[tuple[Path, Path, Path]] = []
    for source in saved.get("sources", []):
        stem = _freqtrade_stem(source["instrument_id"])
        for key, timeframe in (
            ("five_minute_path", "5m"),
            ("fifteen_minute_path", "15m"),
        ):
            current = Path(source[key])
            target = current.parent / f"{stem}-{timeframe}-futures.feather"
            backup = target.with_suffix(target.suffix + ".pre-v40.bak")
            if not current.is_file():
                raise RuntimeError(f"downloaded source is missing: {current}")
            if target.exists() and backup.exists():
                raise RuntimeError(f"refusing to replace an existing v40 backup: {backup}")
            moves.append((current, target, backup))
    for current, target, backup in moves:
        if target.exists():
            os.replace(target, backup)
        os.replace(current, target)
        current_sidecar = current.with_suffix(current.suffix + ".source.json")
        if current_sidecar.exists():
            os.replace(
                current_sidecar,
                target.with_suffix(target.suffix + ".source.json"),
            )
    for source in saved["sources"]:
        stem = _freqtrade_stem(source["instrument_id"])
        parent = Path(source["five_minute_path"]).parent
        source["five_minute_path"] = str(parent / f"{stem}-5m-futures.feather")
        source["fifteen_minute_path"] = str(parent / f"{stem}-15m-futures.feather")
    _write_json_atomic(saved, receipt_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datadir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--start", default="2023-07-01T00:00:00+00:00")
    parser.add_argument("--sidecars-only", action="store_true")
    parser.add_argument("--install-freqtrade-paths", action="store_true")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="merge a six-hour public overlap and atomically promote all six files",
    )
    args = parser.parse_args()
    if args.install_freqtrade_paths:
        _install_freqtrade_paths(args.receipt)
        return 0
    if args.sidecars_only:
        saved = json.loads(args.receipt.read_text(encoding="utf-8"))
        for source in saved.get("sources", []):
            common = {
                "schema_version": "okx-public-candle-file-source-v1",
                "endpoint": source["endpoint"],
                "instrument_id": source["instrument_id"],
                "credentials_used": False,
                "account_endpoint_used": False,
                "orders_submitted": False,
                "response_chain_sha256": source["response_chain_sha256"],
                "downloaded_at": saved["downloaded_at"],
            }
            path_5m = Path(source["five_minute_path"])
            path_15m = Path(source["fifteen_minute_path"])
            if (
                _sha256(path_5m) != source["five_minute_sha256"]
                or _sha256(path_15m) != source["fifteen_minute_sha256"]
            ):
                raise RuntimeError("data digest changed before source sidecar creation")
            _write_json_atomic(
                {
                    **common,
                    "source_type": "OKX_PUBLIC_REST",
                    "timeframe": "5m",
                    "data_file_sha256": source["five_minute_sha256"],
                },
                path_5m.with_suffix(path_5m.suffix + ".source.json"),
            )
            _write_json_atomic(
                {
                    **common,
                    "source_type": "DERIVED_FROM_OKX_PUBLIC_REST",
                    "timeframe": "15m",
                    "derivation": source["fifteen_minute_derivation"],
                    "parent_five_minute_sha256": source["five_minute_sha256"],
                    "data_file_sha256": source["fifteen_minute_sha256"],
                },
                path_15m.with_suffix(path_15m.suffix + ".source.json"),
            )
        return 0
    start = datetime.fromisoformat(args.start).astimezone(timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    receipt: dict[str, Any] = {
        "schema_version": "okx-public-candle-source-receipt-v1",
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "execution_scope": "PUBLIC_MARKET_DATA_ONLY",
        "credentials_used": False,
        "account_endpoint_used": False,
        "orders_submitted": False,
        "sources": [],
    }
    futures_dir = args.datadir / "futures"
    with ThreadPoolExecutor(max_workers=len(INSTRUMENTS)) as executor:
        downloaded = dict(
            zip(
                INSTRUMENTS,
                executor.map(
                    lambda item: refresh_pair(
                        item,
                        start_ms,
                        futures_dir / f"{_freqtrade_stem(item)}-5m-futures.feather",
                    )
                    if args.incremental
                    else download_pair(item, start_ms),
                    INSTRUMENTS,
                ),
            )
        )
    staging_root = Path(tempfile.mkdtemp(prefix="okx-research-refresh-", dir=args.datadir))
    promotions: list[tuple[Path, Path]] = []
    try:
        for instrument in INSTRUMENTS:
            frame_5m, source = downloaded[instrument]
            frame_15m = derive_15m(frame_5m)
            stem = _freqtrade_stem(instrument)
            path_5m = futures_dir / f"{stem}-5m-futures.feather"
            path_15m = futures_dir / f"{stem}-15m-futures.feather"
            staged_5m = staging_root / path_5m.name
            staged_15m = staging_root / path_15m.name
            _write_atomic(frame_5m, staged_5m)
            _write_atomic(frame_15m, staged_15m)
            digest_5m = _sha256(staged_5m)
            digest_15m = _sha256(staged_15m)
            common_source = {
                "schema_version": "okx-public-candle-file-source-v1",
                "endpoint": ENDPOINT,
                "instrument_id": instrument,
                "credentials_used": False,
                "account_endpoint_used": False,
                "orders_submitted": False,
                "response_chain_sha256": source["response_chain_sha256"],
                "downloaded_at": receipt["downloaded_at"],
            }
            staged_5m_sidecar = staged_5m.with_suffix(staged_5m.suffix + ".source.json")
            staged_15m_sidecar = staged_15m.with_suffix(staged_15m.suffix + ".source.json")
            _write_json_atomic(
                {
                    **common_source,
                    "source_type": "OKX_PUBLIC_REST",
                    "timeframe": "5m",
                    "data_file_sha256": digest_5m,
                },
                staged_5m_sidecar,
            )
            _write_json_atomic(
                {
                    **common_source,
                    "source_type": "DERIVED_FROM_OKX_PUBLIC_REST",
                    "timeframe": "15m",
                    "derivation": "UTC epoch-aligned 3x5m OHLCV aggregation",
                    "parent_five_minute_sha256": digest_5m,
                    "data_file_sha256": digest_15m,
                },
                staged_15m_sidecar,
            )
            source.update(
                {
                    "pair": instrument.replace("-", "/", 1).replace("-SWAP", ":USDT"),
                    "five_minute_path": str(path_5m),
                    "five_minute_sha256": digest_5m,
                    "fifteen_minute_derivation": "UTC epoch-aligned 3x5m OHLCV aggregation",
                    "fifteen_minute_path": str(path_15m),
                    "fifteen_minute_sha256": digest_15m,
                    "fifteen_minute_row_count": len(frame_15m),
                }
            )
            receipt["sources"].append(source)
            promotions.extend(
                [
                    (staged_5m, path_5m),
                    (staged_15m, path_15m),
                    (staged_5m_sidecar, path_5m.with_suffix(path_5m.suffix + ".source.json")),
                    (staged_15m_sidecar, path_15m.with_suffix(path_15m.suffix + ".source.json")),
                ]
            )
            print(f"{instrument}: complete rows={len(frame_5m)}", flush=True)
        staged_receipt = staging_root / args.receipt.name
        _write_json_atomic(receipt, staged_receipt)
        promotions.append((staged_receipt, args.receipt))
        _promote_complete_refresh(promotions)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    print(args.receipt, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
