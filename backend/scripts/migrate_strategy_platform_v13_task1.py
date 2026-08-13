#!/usr/bin/env python3
"""Run the V1.3 Task 1 migration only against the isolated design-lab DB.

The command consumes the append-only, read-only market evidence contract emitted
by ``strategy_platform_v13_market_data_inventory.py``.  It independently
rehashes every bound artifact and rescans the six candle files before opening a
database transaction.  A PASSED source receipt proves historical source
consistency only; this command deliberately persists current freshness as
UNKNOWN.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
from typing import Any, Mapping

import pandas as pd
from sqlalchemy import create_engine, text

from app.db.migrations import (
    SCHEMA_VERSION,
    schema_problems,
    strategy_platform_v13_owner_schema_problems,
    upgrade_database,
)
from app.db.strategy_platform_v13_task1 import (
    MarketFileEvidence,
    _WINDOW_SPECS,
    canonical_json,
    execute_strategy_platform_v13_task1,
)

try:  # Direct script invocation adds backend/scripts, tests add backend.
    import strategy_platform_v13_market_data_inventory as evidence_contract
except ImportError:  # pragma: no cover - import shape depends on entrypoint
    from scripts import strategy_platform_v13_market_data_inventory as evidence_contract

evidence_canonical_sha256 = evidence_contract.canonical_sha256
evidence_sha256_file = evidence_contract.sha256_file


EVIDENCE_CONTRACT = "strategy-platform-v13-migration-market-evidence-v1"
SNAPSHOT_CONTRACT = "strategy-platform-v13-market-data-evidence-v1"
TARGET_MANIFEST_CONTRACT = "strategy-platform-v13-market-target-manifest-v1"
PATH_CONTRACT = "canonical-market-data-root-relative-v1"
FILE_IDENTITY_CONTRACT = "market-data-file-identity-v1"
SOURCE_IDENTITY_CONTRACT = "market-data-source-identity-v1"
SOURCE_RECEIPT_CONTRACT = "okx-public-candle-file-source-v1"
AGGREGATE_RECEIPT_CONTRACT = "okx-public-candle-source-receipt-v1"
DESIGN_LAB_DATABASE = "freqtrade_ai_design_lab"
STATUS_SCOPE = "MIGRATION_SOURCE_CONSISTENCY_AS_OF_SOURCE_RECEIPT"
FRESHNESS_BASIS = "ORIGINAL_AGGREGATE_RECEIPT_DOWNLOADED_AT"
QUALITY_DECISION = "NOT_STRATEGY_QUALIFICATION"
CANONICAL_ROOT_KEY = "freqtrade-market-data"
PUBLIC_ENDPOINT = "https://www.okx.com/api/v5/market/history-candles"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class Task1CommandBlocked(RuntimeError):
    """The CLI cannot prove a required immutable or safety fact."""


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Task1CommandBlocked(f"{field} must be an object")
    return value


def _require_list(value: Any, *, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise Task1CommandBlocked(f"{field} must be a list")
    return value


def _require_digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise Task1CommandBlocked(f"{field} must be a lowercase SHA-256")
    return value


def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise Task1CommandBlocked(f"{field} must be an integer >= {minimum}")
    return value


def _instant(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise Task1CommandBlocked(f"{field} must be an ISO-8601 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Task1CommandBlocked(f"{field} is not ISO-8601") from exc
    if result.tzinfo is None:
        raise Task1CommandBlocked(f"{field} must include a timezone")
    return result.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _regular_file(raw: Any, *, field: str) -> Path:
    if not isinstance(raw, (str, Path)) or not str(raw):
        raise Task1CommandBlocked(f"{field} must name a file")
    lexical = Path(raw).expanduser()
    if not lexical.is_absolute():
        lexical = (Path.cwd() / lexical).absolute()
    if lexical.is_symlink() or not lexical.is_file():
        raise Task1CommandBlocked(f"{field} is not a regular non-symlink file")
    return lexical.resolve(strict=True)


def _repository_root(path: Path) -> Path:
    for candidate in (path.parent, *path.parents):
        if (candidate / ".git").exists() and (candidate / "backend").is_dir():
            return candidate.resolve(strict=True)
    raise Task1CommandBlocked("evidence file is not inside a repository worktree")


def _canonical_artifact_digest(payload: Mapping[str, Any]) -> str:
    without_digest = dict(payload)
    without_digest.pop("artifact_digest", None)
    return evidence_canonical_sha256(without_digest)


def _safe_relative_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise Task1CommandBlocked(f"{field} is not a canonical POSIX relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise Task1CommandBlocked(f"{field} is not a canonical POSIX relative path")
    if path.as_posix() != value:
        raise Task1CommandBlocked(f"{field} is not canonical")
    return value


def _same_instant(left: Any, right: Any, *, left_field: str, right_field: str) -> bool:
    return _instant(left, field=left_field) == _instant(right, field=right_field)


def _verify_envelope(payload: Mapping[str, Any]) -> tuple[datetime, datetime, int]:
    if (
        payload.get("schema_version") != EVIDENCE_CONTRACT
        or payload.get("status") != "PASSED"
        or payload.get("reason_codes") != []
        or payload.get("status_scope") != STATUS_SCOPE
        or payload.get("freshness_basis") != FRESHNESS_BASIS
    ):
        raise Task1CommandBlocked("market evidence envelope is not acceptance-grade")
    observed_at = _instant(payload.get("observed_at"), field="observed_at")
    generated_at = _instant(payload.get("generated_at"), field="generated_at")
    delay = _integer(
        payload.get("artifact_generation_delay_seconds"),
        field="artifact_generation_delay_seconds",
    )
    if int((generated_at - observed_at).total_seconds()) != delay:
        raise Task1CommandBlocked("artifact generation delay is not bound to its timestamps")
    safety = _require_mapping(payload.get("safety"), field="safety")
    expected_safety = {
        "market_files_modified": False,
        "production_receipt_modified": False,
        "database_writes": False,
        "network_access": False,
        "credentials": "NOT_ACCESSED",
        "okx_live": "NOT_ACCESSED",
        "acl": "NOT_ACCESSED",
        "strategies_validation_jobs_orders_runtime": "NOT_CREATED_OR_INVOKED",
    }
    if dict(safety) != expected_safety:
        raise Task1CommandBlocked("market evidence safety boundary changed")
    path_contract = _require_mapping(payload.get("path_contract"), field="path_contract")
    if dict(path_contract) != {
        "version": PATH_CONTRACT,
        "canonical_data_root_key": CANONICAL_ROOT_KEY,
        "persistent_path_form": "POSIX_RELATIVE_TO_CANONICAL_DATA_ROOT",
        "absolute_paths_are_observed_locators_only": True,
    }:
        raise Task1CommandBlocked("market evidence path contract changed")
    return observed_at, generated_at, delay


def _load_bound_json(
    path_value: Any, digest_value: Any, *, field: str
) -> tuple[Path, dict[str, Any]]:
    path = _regular_file(path_value, field=field)
    expected = _require_digest(digest_value, field=f"{field}.sha256")
    before = path.stat()
    if evidence_sha256_file(path) != expected:
        raise Task1CommandBlocked(f"{field} bytes do not match evidence")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Task1CommandBlocked(f"{field} is not parseable JSON") from exc
    if not isinstance(payload, dict):
        raise Task1CommandBlocked(f"{field} must contain one JSON object")
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or evidence_sha256_file(path) != expected
    ):
        raise Task1CommandBlocked(f"{field} changed during Task 1 preflight")
    return path, payload


def _verify_aggregate_evidence(
    payload: Mapping[str, Any],
    *,
    source_observed_at: datetime,
    repository_root: Path,
) -> tuple[dict[str, Any], str, Mapping[str, Any], Mapping[str, Any]]:
    legacy = _require_mapping(
        payload.get("source_legacy_classification"),
        field="source_legacy_classification",
    )
    legacy_receipt = _require_mapping(
        legacy.get("aggregate_receipt"), field="source_legacy_classification.aggregate_receipt"
    )
    legacy_digest = _require_digest(legacy_receipt.get("sha256"), field="legacy.sha256")
    if (
        legacy.get("status") != "BLOCKED"
        or legacy_receipt.get("status") != "BLOCKED"
        or not legacy.get("reason_codes")
        or not legacy_receipt.get("reason_codes")
    ):
        raise Task1CommandBlocked("legacy aggregate receipt must remain BLOCKED")
    _, legacy_payload = _load_bound_json(
        legacy_receipt.get("observed_absolute_path"), legacy_digest, field="legacy receipt"
    )
    if (
        legacy_payload.get("schema_version") != AGGREGATE_RECEIPT_CONTRACT
        or legacy_payload.get("execution_scope") != "PUBLIC_MARKET_DATA_ONLY"
        or legacy_payload.get("credentials_used") is not False
        or legacy_payload.get("account_endpoint_used") is not False
        or legacy_payload.get("orders_submitted") is not False
        or _instant(legacy_payload.get("downloaded_at"), field="legacy.downloaded_at")
        != source_observed_at
        or _instant(legacy_receipt.get("downloaded_at"), field="legacy evidence downloaded_at")
        != source_observed_at
    ):
        raise Task1CommandBlocked("legacy aggregate receipt safety contract is invalid")

    corrected = _require_mapping(payload.get("corrected_matrix"), field="corrected_matrix")
    corrected_receipt = _require_mapping(
        corrected.get("aggregate_receipt"), field="corrected_matrix.aggregate_receipt"
    )
    corrected_digest = _require_digest(
        corrected_receipt.get("sha256"), field="corrected_matrix.aggregate_receipt.sha256"
    )
    corrected_relative = _safe_relative_path(
        corrected_receipt.get("canonical_relative_path"),
        field="corrected_matrix.aggregate_receipt.canonical_relative_path",
    )
    if (
        corrected.get("status") != "PASSED"
        or corrected.get("reason_codes") != []
        or corrected_receipt.get("status") != "PASSED"
        or corrected_receipt.get("reason_codes") != []
        or corrected_receipt.get("schema_version") != AGGREGATE_RECEIPT_CONTRACT
        or corrected_receipt.get("path_contract_version") != PATH_CONTRACT
        or corrected_receipt.get("canonical_data_root_key") != CANONICAL_ROOT_KEY
    ):
        raise Task1CommandBlocked("corrected aggregate matrix is not PASSED")
    _, corrected_payload = _load_bound_json(
        repository_root / corrected_relative,
        corrected_digest,
        field="corrected aggregate receipt",
    )
    if (
        corrected_payload.get("schema_version") != AGGREGATE_RECEIPT_CONTRACT
        or corrected_payload.get("path_contract_version") != PATH_CONTRACT
        or corrected_payload.get("canonical_data_root_key") != CANONICAL_ROOT_KEY
        or corrected_payload.get("execution_scope") != "PUBLIC_MARKET_DATA_ONLY"
        or corrected_payload.get("credentials_used") is not False
        or corrected_payload.get("account_endpoint_used") is not False
        or corrected_payload.get("orders_submitted") is not False
        or _instant(
            corrected_payload.get("downloaded_at"), field="corrected.downloaded_at"
        )
        != source_observed_at
        or _instant(
            corrected_receipt.get("downloaded_at"),
            field="corrected evidence downloaded_at",
        )
        != source_observed_at
    ):
        raise Task1CommandBlocked("corrected aggregate payload safety contract is invalid")
    correction = _require_mapping(payload.get("correction_contract"), field="correction_contract")
    expected_payload_digest = _require_digest(
        correction.get("expected_corrected_payload_digest"),
        field="correction_contract.expected_corrected_payload_digest",
    )
    if (
        correction.get("status") != "PASSED"
        or correction.get("reason_codes") != []
        or correction.get("blocked_reason_codes") != []
        or correction.get("unknown_reason_codes") != []
        or correction.get("allowed_changes")
        != [
            "path_contract_version",
            "canonical_data_root_key",
            "sources[].five_minute_path",
            "sources[].fifteen_minute_path",
        ]
        or correction.get("transformation_count") != 6
        or correction.get("observed_corrected_payload_digest") != expected_payload_digest
        or evidence_canonical_sha256(corrected_payload) != expected_payload_digest
    ):
        raise Task1CommandBlocked("corrected aggregate is not the audited path-only transform")
    legacy_manifest = {
        "status": "BLOCKED",
        "sha256": legacy_digest,
        "snapshot_digest": _require_digest(legacy.get("snapshot_digest"), field="legacy.snapshot_digest"),
        "report_digest": _require_digest(legacy.get("report_digest"), field="legacy.report_digest"),
    }
    corrected_manifest = {
        "status": "PASSED",
        "artifact_sha256": corrected_digest,
        "snapshot_digest": _require_digest(
            corrected.get("snapshot_digest"), field="corrected_matrix.snapshot_digest"
        ),
        "report_digest": _require_digest(
            corrected.get("report_digest"), field="corrected_matrix.report_digest"
        ),
    }
    return corrected_payload, corrected_digest, legacy_manifest, corrected_manifest


def _classification_windows(
    *, frame: pd.DataFrame, timestamps: pd.Series, pair: str, file_sha256: str
) -> dict[str, dict[str, Any]]:
    numeric_close = pd.to_numeric(frame["close"], errors="coerce")
    result: dict[str, dict[str, Any]] = {}
    for key, spec in _WINDOW_SPECS.items():
        bounds = spec.get("sol") if pair.startswith("SOL/") else None
        start_raw, end_raw = bounds or spec["default"]
        start = _instant(start_raw, field=f"{key}.start_at")
        end = _instant(end_raw, field=f"{key}.end_at")
        selected = numeric_close.loc[(timestamps >= start) & (timestamps < end)]
        if len(selected) < 2 or selected.isna().any():
            raise Task1CommandBlocked(f"classification window {pair} {key} is incomplete")
        first_close = float(selected.iloc[0])
        last_close = float(selected.iloc[-1])
        if not math.isfinite(first_close) or not math.isfinite(last_close) or first_close <= 0:
            raise Task1CommandBlocked(f"classification window {pair} {key} has invalid prices")
        close_return = last_close / first_close - 1.0
        actual = "bull" if close_return >= 0.05 else "bear" if close_return <= -0.05 else "range"
        expected = spec.get("expected")
        if expected is not None and actual != expected:
            raise Task1CommandBlocked(
                f"classification window {pair} {key} is {actual}, expected {expected}"
            )
        result[key] = {
            "start_at": start_raw,
            "end_at": end_raw,
            "row_count": int(len(selected)),
            "first_close": first_close,
            "last_close": last_close,
            "close_return": close_return,
            "actual_regime": actual,
            "market_data_digest": file_sha256,
        }
    return result


def _rescan_market_artifact(
    *, path: Path, target: Mapping[str, Any], evidence: Mapping[str, Any]
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    before = path.stat()
    digest_before = evidence_sha256_file(path)
    if digest_before != evidence.get("sha256") or before.st_size != evidence.get("size_bytes"):
        raise Task1CommandBlocked(f"market file bytes changed: {target.get('pair')} {target.get('timeframe')}")
    if evidence.get("format") == "feather":
        frame = pd.read_feather(path)
    elif evidence.get("format") == "parquet":
        frame = pd.read_parquet(path)
    else:
        raise Task1CommandBlocked("Task 1 accepts only Feather/Parquet market files")
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns) or len(frame) != evidence.get("row_count"):
        raise Task1CommandBlocked("market file row/schema evidence mismatch")
    timestamps = pd.to_datetime(frame["date"], utc=True, errors="coerce")
    invalid_timestamp_count = int(timestamps.isna().sum())
    valid = timestamps.dropna()
    interval = _integer(
        evidence.get("expected_interval_seconds"), field="expected_interval_seconds", minimum=1
    )
    if valid.empty:
        raise Task1CommandBlocked("market file has no valid timestamps")
    diffs = valid.diff().dt.total_seconds().dropna()
    duplicate_count = int(valid.duplicated().sum())
    out_of_order_count = int((diffs < 0).sum())
    sorted_unique = valid.drop_duplicates().sort_values()
    sorted_diffs = sorted_unique.diff().dt.total_seconds().dropna()
    missing_count = int(
        sum(max(0, math.floor(float(delta) / interval) - 1) for delta in sorted_diffs)
    )
    cadence_misaligned = sum(
        float(delta) <= 0 or float(delta) % interval != 0 for delta in sorted_diffs
    )
    boundary_misaligned = sum(
        int(timestamp.value) % (interval * 1_000_000_000) != 0 for timestamp in sorted_unique
    )
    scanner_misaligned = int(cadence_misaligned + boundary_misaligned)
    core_misaligned = int(
        (timestamps.array.as_unit("ns").asi8 % (interval * 1_000_000_000) != 0).sum()
    )
    numeric = frame[["open", "high", "low", "close", "volume"]].apply(
        pd.to_numeric, errors="coerce"
    )
    null_count = int(numeric.isna().any(axis=1).sum())
    invalid_ohlc = int(
        (
            (numeric["high"] < numeric["low"])
            | (numeric["open"] > numeric["high"])
            | (numeric["open"] < numeric["low"])
            | (numeric["close"] > numeric["high"])
            | (numeric["close"] < numeric["low"])
        ).sum()
    )
    negative_volume = int((numeric["volume"] < 0).sum())
    infinite_count = int(numeric.isin([math.inf, -math.inf]).any(axis=1).sum())
    nonpositive_price_count = int(
        (numeric[["open", "high", "low", "close"]] <= 0).any(axis=1).sum()
    )
    first = valid.min().to_pydatetime()
    last = valid.max().to_pydatetime()
    if (
        _iso(first) != evidence.get("first_open_at")
        or _iso(last) != evidence.get("last_open_at")
        or _iso(last + timedelta(seconds=interval)) != evidence.get("last_close_at")
    ):
        raise Task1CommandBlocked("market file time bounds do not match evidence")
    expected_metrics = {
        "invalid_timestamp_count": invalid_timestamp_count,
        "duplicate_timestamp_count": duplicate_count,
        "out_of_order_count": out_of_order_count,
        "missing_interval_count": missing_count,
        "misaligned_interval_count": scanner_misaligned,
        "null_ohlcv_row_count": null_count,
    }
    if any(evidence.get(key) != value for key, value in expected_metrics.items()):
        raise Task1CommandBlocked("market file scan metrics do not match evidence")
    if any(
        (
            invalid_timestamp_count,
            duplicate_count,
            out_of_order_count,
            missing_count,
            scanner_misaligned,
            null_count,
            invalid_ohlc,
            negative_volume,
            infinite_count,
            nonpositive_price_count,
        )
    ):
        raise Task1CommandBlocked("market file has non-zero Task 1 quality defects")
    after = path.stat()
    if (
        after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or evidence_sha256_file(path) != digest_before
    ):
        raise Task1CommandBlocked("market file changed during Task 1 preflight")
    return {
        "gap_count": missing_count,
        "duplicate_count": duplicate_count,
        "null_count": null_count,
        "out_of_order_count": out_of_order_count,
        "misaligned_timestamp_count": core_misaligned,
        "invalid_ohlc_count": invalid_ohlc,
        "negative_volume_count": negative_volume,
    }, _classification_windows(
        frame=frame,
        timestamps=timestamps,
        pair=str(target["pair"]),
        file_sha256=digest_before,
    )


def _verify_source_sidecar(
    *,
    source: Mapping[str, Any],
    file_evidence: Mapping[str, Any],
    root: Path,
    expected_parent_digest: str | None,
) -> tuple[str, str, str, str, str]:
    relative = _safe_relative_path(
        source.get("canonical_relative_path"), field="source_evidence.canonical_relative_path"
    )
    observed = _regular_file(source.get("observed_absolute_path"), field="source sidecar")
    official_resolution = evidence_contract.resolve_market_data_path(
        observed,
        canonical_data_root=root,
    )
    if (
        official_resolution.status != "PASSED"
        or official_resolution.canonical_relative_path != relative
        or official_resolution.path is None
        or official_resolution.path.resolve(strict=True) != observed
        or observed != (root / relative).resolve(strict=True)
    ):
        raise Task1CommandBlocked("source sidecar locator does not match canonical identity")
    source_digest = _require_digest(source.get("sha256"), field="source_evidence.sha256")
    if evidence_sha256_file(observed) != source_digest:
        raise Task1CommandBlocked("source sidecar bytes changed")
    before = observed.stat()
    try:
        payload = json.loads(observed.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Task1CommandBlocked("source sidecar is unreadable") from exc
    if not isinstance(payload, dict):
        raise Task1CommandBlocked("source sidecar is not an object")
    required_source_type = (
        "OKX_PUBLIC_REST" if file_evidence.get("file_identity", {}).get("timeframe") == "5m"
        else "DERIVED_FROM_OKX_PUBLIC_REST"
    )
    comparisons = {
        "schema_version": SOURCE_RECEIPT_CONTRACT,
        "endpoint": PUBLIC_ENDPOINT,
        "instrument_id": source.get("instrument_id"),
        "timeframe": source.get("timeframe"),
        "source_type": required_source_type,
        "data_file_sha256": file_evidence.get("sha256"),
        "credentials_used": False,
        "account_endpoint_used": False,
        "orders_submitted": False,
    }
    if any(payload.get(key) != value for key, value in comparisons.items()):
        raise Task1CommandBlocked("source sidecar safety/content binding is invalid")
    if (
        source.get("status") != "PASSED"
        or source.get("reason_codes") != []
        or source.get("blocked_reason_codes") != []
        or source.get("unknown_reason_codes") != []
        or source.get("schema_version") != SOURCE_RECEIPT_CONTRACT
        or source.get("source_type") != required_source_type
        or source.get("endpoint") != PUBLIC_ENDPOINT
        or source.get("instrument_id")
        != file_evidence.get("file_identity", {}).get("instrument_id")
        or source.get("timeframe")
        != file_evidence.get("file_identity", {}).get("timeframe")
        or source.get("data_file_sha256") != file_evidence.get("sha256")
        or source.get("downloaded_at") != _iso(_instant(payload.get("downloaded_at"), field="sidecar.downloaded_at"))
    ):
        raise Task1CommandBlocked("source evidence disagrees with its sidecar")
    response_digest = _require_digest(
        payload.get("response_chain_sha256"), field="sidecar.response_chain_sha256"
    )
    if source.get("response_chain_sha256") != response_digest:
        raise Task1CommandBlocked("source response-chain digest mismatch")
    if (
        payload.get("parent_five_minute_sha256") != expected_parent_digest
        or source.get("parent_five_minute_sha256") != expected_parent_digest
    ):
        raise Task1CommandBlocked("source parent-file digest mismatch")
    identity = _require_mapping(source.get("source_identity"), field="source_identity")
    identity_digest = _require_digest(
        source.get("source_identity_digest"), field="source_identity_digest"
    )
    if (
        identity.get("version") != SOURCE_IDENTITY_CONTRACT
        or identity.get("file_identity_digest") != file_evidence.get("file_identity_digest")
        or identity.get("content_sha256") != file_evidence.get("sha256")
        or identity.get("sidecar_schema_version") != SOURCE_RECEIPT_CONTRACT
        or identity.get("response_chain_sha256") != response_digest
        or evidence_canonical_sha256(identity) != identity_digest
    ):
        raise Task1CommandBlocked("source identity digest is invalid")
    after = observed.stat()
    if (
        after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or evidence_sha256_file(observed) != source_digest
    ):
        raise Task1CommandBlocked("source sidecar changed during Task 1 preflight")
    return source_digest, identity_digest, relative, response_digest, required_source_type


def _verify_record(
    *,
    row: Mapping[str, Any],
    root: Path,
    aggregate_payload: Mapping[str, Any],
    aggregate_digest: str,
    artifact_digest: str,
    inspected_at: datetime,
) -> MarketFileEvidence:
    if row.get("status") != "PASSED" or row.get("reason_codes") != []:
        raise Task1CommandBlocked("market target record is not PASSED")
    target = _require_mapping(row.get("target_contract"), field="target_contract")
    target_copy = _require_mapping(row.get("target"), field="target")
    identity_fields = (
        "exchange", "market_type", "pair", "instrument_id", "timeframe", "data_kind",
        "max_close_lag_seconds",
    )
    if any(target.get(key) != target_copy.get(key) for key in identity_fields):
        raise Task1CommandBlocked("target and target_contract disagree")
    expected_instrument = {
        "BTC/USDT:USDT": "BTC-USDT-SWAP",
        "ETH/USDT:USDT": "ETH-USDT-SWAP",
        "SOL/USDT:USDT": "SOL-USDT-SWAP",
    }.get(target.get("pair"))
    expected_interval = {"5m": 300, "15m": 900}.get(target.get("timeframe"))
    expected_lag = {"5m": 1200, "15m": 1200}.get(target.get("timeframe"))
    if (
        target.get("exchange") != "okx"
        or target.get("market_type") != "futures"
        or target.get("data_kind") != "futures"
        or target.get("instrument_id") != expected_instrument
        or target.get("max_close_lag_seconds") != expected_lag
        or expected_interval is None
    ):
        raise Task1CommandBlocked("target is outside the exact Task 1 market manifest")
    target_key = "|".join(str(target.get(key, "")) for key in (
        "exchange", "market_type", "pair", "timeframe", "data_kind"
    ))
    if row.get("target_key") != target_key:
        raise Task1CommandBlocked("target_key does not bind the target contract")
    relative = _safe_relative_path(
        target.get("canonical_relative_path"), field=f"{target_key}.canonical_relative_path"
    )
    file_evidence = _require_mapping(row.get("file_evidence"), field=f"{target_key}.file_evidence")
    observed = _regular_file(
        file_evidence.get("observed_absolute_path"), field=f"{target_key}.market_file"
    )
    official_resolution = evidence_contract.resolve_market_data_path(
        observed,
        canonical_data_root=root,
    )
    if (
        official_resolution.status != "PASSED"
        or official_resolution.canonical_relative_path != relative
        or official_resolution.path is None
        or official_resolution.path.resolve(strict=True) != observed
        or observed != (root / relative).resolve(strict=True)
    ):
        raise Task1CommandBlocked("market file locator does not match canonical identity")
    if target_copy.get("path") != relative:
        raise Task1CommandBlocked("target path is not its canonical identity")
    if (
        file_evidence.get("status") != "PASSED"
        or file_evidence.get("reason_codes") != []
        or file_evidence.get("blocked_reason_codes") != []
        or file_evidence.get("unknown_reason_codes") != []
        or file_evidence.get("quality_decision") != QUALITY_DECISION
        or file_evidence.get("canonical_relative_path") != relative
        or file_evidence.get("expected_interval_seconds") != expected_interval
    ):
        raise Task1CommandBlocked("market file evidence is not acceptance-grade")
    file_identity = _require_mapping(file_evidence.get("file_identity"), field="file_identity")
    file_identity_digest = _require_digest(
        file_evidence.get("file_identity_digest"), field="file_identity_digest"
    )
    expected_identity = {
        "version": FILE_IDENTITY_CONTRACT,
        "canonical_data_root_key": CANONICAL_ROOT_KEY,
        "path_contract_version": PATH_CONTRACT,
        "canonical_relative_path": relative,
        "exchange": target.get("exchange"),
        "market_type": target.get("market_type"),
        "pair": target.get("pair"),
        "instrument_id": target.get("instrument_id"),
        "timeframe": target.get("timeframe"),
        "data_kind": target.get("data_kind"),
        "format": file_evidence.get("format"),
    }
    if dict(file_identity) != expected_identity or evidence_canonical_sha256(file_identity) != file_identity_digest:
        raise Task1CommandBlocked("market file identity digest is invalid")
    binding = _require_mapping(row.get("aggregate_binding"), field="aggregate_binding")
    sources = _require_list(aggregate_payload.get("sources"), field="aggregate.sources")
    matches = [item for item in sources if isinstance(item, Mapping) and item.get("pair") == target.get("pair")]
    if len(matches) != 1:
        raise Task1CommandBlocked("aggregate matrix does not uniquely bind the pair")
    aggregate_source = matches[0]
    prefix = "five_minute" if target.get("timeframe") == "5m" else "fifteen_minute"
    row_count_field = "row_count" if prefix == "five_minute" else "fifteen_minute_row_count"
    first_field = "first_open_at" if prefix == "five_minute" else "installed_fifteen_minute_first_open_at"
    last_field = "last_open_at" if prefix == "five_minute" else "installed_fifteen_minute_last_open_at"
    resolution = _require_mapping(binding.get("receipt_path_resolution"), field="aggregate_binding.receipt_path_resolution")
    if (
        binding.get("status") != "PASSED"
        or binding.get("reason_codes") != []
        or binding.get("blocked_reason_codes") != []
        or binding.get("unknown_reason_codes") != []
        or resolution.get("status") != "PASSED"
        or resolution.get("canonical_relative_path") != relative
        or binding.get("receipt_raw_path") != relative
        or binding.get("receipt_file_sha256") != file_evidence.get("sha256")
        or binding.get("receipt_row_count") != file_evidence.get("row_count")
        or aggregate_source.get(f"{prefix}_path") != relative
        or aggregate_source.get(f"{prefix}_sha256") != file_evidence.get("sha256")
        or aggregate_source.get(row_count_field) != file_evidence.get("row_count")
        or not _same_instant(binding.get("receipt_first_open_at"), aggregate_source.get(first_field), left_field="binding.first", right_field="aggregate.first")
        or not _same_instant(binding.get("receipt_last_open_at"), aggregate_source.get(last_field), left_field="binding.last", right_field="aggregate.last")
    ):
        raise Task1CommandBlocked("aggregate binding disagrees with corrected matrix")
    metrics, windows = _rescan_market_artifact(
        path=observed, target=target, evidence=file_evidence
    )
    source = _require_mapping(row.get("source_evidence"), field="source_evidence")
    expected_parent = (
        None
        if target.get("timeframe") == "5m"
        else _require_digest(
            aggregate_source.get("five_minute_sha256"),
            field="aggregate.five_minute_sha256",
        )
    )
    source_digest, source_identity_digest, source_path, response_digest, source_type = (
        _verify_source_sidecar(
            source=source,
            file_evidence=file_evidence,
            root=root,
            expected_parent_digest=expected_parent,
        )
    )
    if response_digest != aggregate_source.get("response_chain_sha256"):
        raise Task1CommandBlocked("source response chain disagrees with aggregate matrix")
    return MarketFileEvidence(
        exchange=str(target["exchange"]),
        market_type=str(target["market_type"]),
        pair=str(target["pair"]),
        instrument_id=str(target["instrument_id"]),
        timeframe=str(target["timeframe"]),
        data_kind=str(target["data_kind"]),
        absolute_path=str(observed),
        relative_path=relative,
        file_format=str(file_evidence["format"]),
        size_bytes=_integer(file_evidence.get("size_bytes"), field="size_bytes", minimum=1),
        sha256=_require_digest(file_evidence.get("sha256"), field="file.sha256"),
        row_count=_integer(file_evidence.get("row_count"), field="row_count", minimum=1),
        first_open_at=_instant(file_evidence.get("first_open_at"), field="first_open_at"),
        last_open_at=_instant(file_evidence.get("last_open_at"), field="last_open_at"),
        last_close_at=_instant(file_evidence.get("last_close_at"), field="last_close_at"),
        expected_interval_seconds=_integer(
            file_evidence.get("expected_interval_seconds"), field="expected_interval_seconds", minimum=1
        ),
        freshness_status="UNKNOWN",
        observed_at=inspected_at,
        inspected_at=inspected_at,
        receipt_id=None,
        source_receipt_digest=source_digest,
        classification_windows=windows,
        file_identity_digest=file_identity_digest,
        source_identity_digest=source_identity_digest,
        aggregate_receipt_digest=aggregate_digest,
        migration_artifact_digest=artifact_digest,
        source_type=source_type,
        source_receipt_path=source_path,
        source_response_chain_digest=response_digest,
        quality_scope=STATUS_SCOPE,
        quality_decision=QUALITY_DECISION,
        freshness_basis=FRESHNESS_BASIS,
        **metrics,
    )


def load_market_evidence(
    path: Path,
    *,
    expected_file_sha256: str,
) -> tuple[str, tuple[MarketFileEvidence, ...], dict[str, Any]]:
    evidence_path = _regular_file(path, field="evidence_file")
    expected_bytes_digest = _require_digest(
        expected_file_sha256, field="evidence_file_sha256"
    )
    evidence_before = evidence_path.stat()
    actual_bytes_digest = evidence_sha256_file(evidence_path)
    if actual_bytes_digest != expected_bytes_digest:
        raise Task1CommandBlocked("evidence file bytes do not match --evidence-file-sha256")
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Task1CommandBlocked("evidence file is unreadable") from exc
    if not isinstance(payload, dict):
        raise Task1CommandBlocked("evidence file must contain one JSON object")
    evidence_after = evidence_path.stat()
    if (
        evidence_before.st_size != evidence_after.st_size
        or evidence_before.st_mtime_ns != evidence_after.st_mtime_ns
        or evidence_sha256_file(evidence_path) != actual_bytes_digest
    ):
        raise Task1CommandBlocked("evidence file changed during Task 1 preflight")
    artifact_digest = _require_digest(payload.get("artifact_digest"), field="artifact_digest")
    if _canonical_artifact_digest(payload) != artifact_digest:
        raise Task1CommandBlocked("artifact canonical digest is invalid")
    source_observed_at, generated_at, generation_delay = _verify_envelope(payload)
    aggregate_payload, aggregate_digest, legacy_manifest, corrected_manifest = (
        _verify_aggregate_evidence(
            payload,
            source_observed_at=source_observed_at,
            repository_root=_repository_root(evidence_path),
        )
    )
    snapshot = _require_mapping(payload.get("market_snapshot"), field="market_snapshot")
    if (
        snapshot.get("schema_version") != SNAPSHOT_CONTRACT
        or snapshot.get("report_schema_version") != SNAPSHOT_CONTRACT
        or snapshot.get("status") != "PASSED"
        or snapshot.get("reason_codes") != []
    ):
        raise Task1CommandBlocked("market snapshot is not PASSED")
    integrity = evidence_contract._snapshot_integrity(snapshot)
    if integrity.status != "PASSED":
        raise Task1CommandBlocked(
            "market snapshot digest/integrity is invalid: "
            + ",".join(sorted(integrity.blocked | integrity.unknown))
        )
    if _instant(snapshot.get("observed_at"), field="market_snapshot.observed_at") != source_observed_at:
        raise Task1CommandBlocked("market snapshot is not bound to source downloaded_at")
    summary = _require_mapping(snapshot.get("summary"), field="market_snapshot.summary")
    expected_summary = {
        "expected_target_count": 6,
        "file_scan_status": "PASSED",
        "file_status_counts": {"BLOCKED": 0, "PASSED": 6, "UNKNOWN": 0},
        "matrix_status_counts": {"BLOCKED": 0, "PASSED": 6, "UNKNOWN": 0},
        "reason_codes": [],
        "source_matrix_status": "PASSED",
        "source_sidecar_status": "PASSED",
        "status": "PASSED",
        "strategy_qualification_performed": False,
        "unknown_values_are_not_zero": True,
    }
    if dict(summary) != expected_summary:
        raise Task1CommandBlocked("market snapshot summary changed")
    snapshot_scope = _require_mapping(snapshot.get("scope"), field="market_snapshot.scope")
    if dict(snapshot_scope) != {
        "filesystem_operation": "READ_ONLY_FULL_SCAN",
        "database_writes": False,
        "network_access": False,
        "credentials": "NOT_ACCESSED",
        "okx_live": "NOT_ACCESSED",
        "acl": "NOT_ACCESSED",
        "strategies_validation_jobs_orders_runtime": "NOT_CREATED_OR_INVOKED",
    }:
        raise Task1CommandBlocked("market snapshot safety boundary changed")
    target_manifest = _require_mapping(
        snapshot.get("expected_target_manifest"), field="expected_target_manifest"
    )
    if (
        target_manifest.get("schema_version") != TARGET_MANIFEST_CONTRACT
        or target_manifest.get("target_count") != 6
    ):
        raise Task1CommandBlocked("expected target manifest is invalid")
    snapshot_path = _require_mapping(snapshot.get("path_contract"), field="market_snapshot.path_contract")
    if (
        snapshot_path.get("version") != PATH_CONTRACT
        or snapshot_path.get("canonical_data_root_key") != CANONICAL_ROOT_KEY
        or snapshot_path.get("persistent_path_form") != "POSIX_RELATIVE_TO_CANONICAL_DATA_ROOT"
        or snapshot_path.get("absolute_paths_are_identity") is not False
        or snapshot_path.get("symlink_aliases_allowed") is not False
    ):
        raise Task1CommandBlocked("market snapshot path contract is invalid")
    root = Path(str(snapshot_path.get("canonical_data_root_observed") or ""))
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise Task1CommandBlocked("canonical market-data root is unavailable")
    root = root.resolve(strict=True)
    rows = _require_list(snapshot.get("files"), field="market_snapshot.files")
    records = tuple(
        _verify_record(
            row=_require_mapping(row, field=f"market_snapshot.files[{index}]"),
            root=root,
            aggregate_payload=aggregate_payload,
            aggregate_digest=aggregate_digest,
            artifact_digest=artifact_digest,
            inspected_at=generated_at,
        )
        for index, row in enumerate(rows)
    )
    expected_targets = {
        (pair, timeframe)
        for pair in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT")
        for timeframe in ("5m", "15m")
    }
    if len(records) != 6 or {(item.pair, item.timeframe) for item in records} != expected_targets:
        raise Task1CommandBlocked("market snapshot must contain exactly BTC/ETH/SOL x 5m/15m")
    if len({item.file_identity_digest for item in records}) != 6:
        raise Task1CommandBlocked("market file identities are not unique")
    full_scan = _require_mapping(
        payload.get("full_scan_classification"), field="full_scan_classification"
    )
    if (
        full_scan.get("target_count") != 6
        or full_scan.get("file_status_counts")
        != {"BLOCKED": 0, "PASSED": 6, "UNKNOWN": 0}
        or full_scan.get("source_sidecar_status") != "PASSED"
        or full_scan.get("source_matrix_status") != "PASSED"
    ):
        raise Task1CommandBlocked("full-scan classification is not PASSED")
    historical_freshness = _require_list(
        full_scan.get("freshness"), field="full_scan_classification.freshness"
    )
    historical_by_target = {
        item.get("target_key"): item
        for item in historical_freshness
        if isinstance(item, Mapping) and isinstance(item.get("target_key"), str)
    }
    if len(historical_freshness) != 6 or len(historical_by_target) != 6:
        raise Task1CommandBlocked("historical freshness evidence is incomplete")
    for item in records:
        target_key = "|".join(
            (item.exchange, item.market_type, item.pair, item.timeframe, item.data_kind)
        )
        freshness = historical_by_target.get(target_key)
        if (
            freshness is None
            or freshness.get("status") != "PASSED"
            or freshness.get("last_open_at") != _iso(item.last_open_at)
            or freshness.get("last_close_at") != _iso(item.last_close_at)
            or freshness.get("close_lag_seconds")
            != int((source_observed_at - item.last_close_at).total_seconds())
        ):
            raise Task1CommandBlocked("historical freshness record is not source-bound")
    snapshot_aggregate = _require_mapping(
        snapshot.get("aggregate_receipt"), field="market_snapshot.aggregate_receipt"
    )
    if (
        snapshot_aggregate.get("status") != "PASSED"
        or snapshot_aggregate.get("sha256") != aggregate_digest
        or snapshot_aggregate.get("schema_version") != AGGREGATE_RECEIPT_CONTRACT
        or snapshot_aggregate.get("path_contract_version") != PATH_CONTRACT
        or snapshot_aggregate.get("canonical_data_root_key") != CANONICAL_ROOT_KEY
    ):
        raise Task1CommandBlocked("market snapshot aggregate receipt is invalid")
    snapshot_manifest = {
        "status": "PASSED",
        "snapshot_digest": _require_digest(
            snapshot.get("snapshot_digest"), field="market_snapshot.snapshot_digest"
        ),
        "report_digest": _require_digest(
            snapshot.get("report_digest"), field="market_snapshot.report_digest"
        ),
    }
    if (
        snapshot_manifest["snapshot_digest"] != corrected_manifest["snapshot_digest"]
        or snapshot_manifest["report_digest"] != corrected_manifest["report_digest"]
    ):
        raise Task1CommandBlocked("corrected matrix and market snapshot digests disagree")
    evidence_manifest = {
        "schema_version": EVIDENCE_CONTRACT,
        "artifact_digest": artifact_digest,
        "artifact_file_sha256": actual_bytes_digest,
        "status_scope": STATUS_SCOPE,
        "freshness_basis": FRESHNESS_BASIS,
        "artifact_generation_delay_seconds": generation_delay,
        "legacy_aggregate_receipt": legacy_manifest,
        "corrected_matrix": corrected_manifest,
        "market_snapshot": snapshot_manifest,
        "files": [
            {
                "target_key": "|".join(
                    (item.exchange, item.market_type, item.pair, item.timeframe, item.data_kind)
                ),
                "file_identity_digest": item.file_identity_digest,
                "source_identity_digest": item.source_identity_digest,
                "file_sha256": item.sha256,
                "source_receipt_digest": item.source_receipt_digest,
            }
            for item in sorted(
                records,
                key=lambda row: "|".join(
                    (
                        row.exchange,
                        row.market_type,
                        row.pair,
                        row.timeframe,
                        row.data_kind,
                    )
                ),
            )
        ],
    }
    return "PASSED", records, evidence_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate V1.3 Task 1 only in freqtrade_ai_design_lab."
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--evidence-file-sha256", required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    parser.add_argument("--report-identity", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--actor", required=True)
    return parser


def _validated_report_identity(value: str) -> str:
    """Return a stable audit identity that cannot expose a local file locator."""

    if "\\" in value:
        raise Task1CommandBlocked("--report-identity must use POSIX separators")
    identity = PurePosixPath(value)
    if (
        identity.is_absolute()
        or not identity.parts
        or any(part in {".", ".."} for part in identity.parts)
        or identity.parts[:2] != ("reports", "migrations")
        or identity.suffix != ".json"
    ):
        raise Task1CommandBlocked(
            "--report-identity must be a repo-relative reports/migrations/*.json path"
        )
    return identity.as_posix()


def _preflight_atomic_report_path(path: Path) -> None:
    """Prove the report directory supports private atomic artifacts."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.preflight-",
        dir=path.parent,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, b"task1-report-preflight\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        Path(temporary_name).unlink(missing_ok=True)


def _atomic_write_private(path: Path, content: str) -> str:
    """Atomically persist a mode-0600 artifact and return its SHA-256."""

    encoded = content.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(encoded).hexdigest()


def _report_artifact_path(first_report_path: Path, *, repeat_noop: bool) -> Path:
    if not repeat_noop:
        return first_report_path
    suffix = first_report_path.suffix
    if suffix:
        return first_report_path.with_name(
            f"{first_report_path.stem}.repeat{suffix}"
        )
    return first_report_path.with_name(f"{first_report_path.name}.repeat")


def _persist_command_report(
    first_report_path: Path,
    first_report_identity: str,
    report: Mapping[str, Any],
    *,
    repeat_noop: bool,
) -> tuple[Path, str, str]:
    artifact_path = _report_artifact_path(
        first_report_path,
        repeat_noop=repeat_noop,
    )
    artifact_identity = _report_artifact_path(
        Path(first_report_identity),
        repeat_noop=repeat_noop,
    ).as_posix()
    enriched_report = {
        **dict(report),
        "report_artifact_identity": artifact_identity,
        "first_report_identity": first_report_identity,
    }
    content = canonical_json(enriched_report) + "\n"
    digest = _atomic_write_private(artifact_path, content)
    sidecar_path = Path(f"{artifact_path}.sha256")
    _atomic_write_private(sidecar_path, f"{digest}  {artifact_path.name}\n")
    return artifact_path, digest, artifact_identity


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report_identity = _validated_report_identity(args.report_identity)
        matrix_status, inventory, evidence_manifest = load_market_evidence(
            args.evidence_file,
            expected_file_sha256=args.evidence_file_sha256,
        )
        _preflight_atomic_report_path(args.report_file)
        engine = create_engine(args.database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            database_name = connection.execute(
                text("SELECT current_database()")
            ).scalar_one()
            source_schema_version = connection.execute(
                text(
                    "SELECT version FROM freqtrade_ai_schema_migrations "
                    "ORDER BY applied_at DESC,version DESC LIMIT 1"
                )
            ).scalar_one()
            predecessor_schema_version = connection.execute(
                text(
                    "SELECT version FROM freqtrade_ai_schema_migrations "
                    "WHERE version<>:current "
                    "ORDER BY applied_at DESC,version DESC LIMIT 1"
                ),
                {"current": SCHEMA_VERSION},
            ).scalar_one_or_none()
            recorded_sources: set[str] = set()
            problems: list[str] = []
            if source_schema_version == SCHEMA_VERSION:
                recorded_sources = {
                    str(value)
                    for value in connection.execute(
                        text(
                            "SELECT DISTINCT source_schema_version FROM "
                            "strategy_platform_migration_runs WHERE "
                            "migration_key='strategy-platform-v13-task1-real-data-v1' "
                            "AND execution_scope='DESIGN_LAB'"
                        )
                    ).scalars()
                }
                completed_owner_run = connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM "
                        "strategy_platform_migration_runs WHERE "
                        "migration_key='strategy-platform-v13-task1-real-data-v1' "
                        "AND execution_scope='DESIGN_LAB' AND status='SUCCEEDED')"
                    )
                ).scalar_one()
                if completed_owner_run is True:
                    problems = strategy_platform_v13_owner_schema_problems(
                        connection,
                        expected_database=DESIGN_LAB_DATABASE,
                    )
                else:
                    # A process may stop after the v47 schema commit but before
                    # the first data run.  Before a terminal owner run exists,
                    # retain the pre-ACL structural recovery verifier.
                    problems = schema_problems(connection)
        if database_name != DESIGN_LAB_DATABASE:
            raise Task1CommandBlocked(
                f"database must be {DESIGN_LAB_DATABASE}; got {database_name}"
            )
        original_source_schema_version = str(source_schema_version)
        if source_schema_version == SCHEMA_VERSION:
            if problems:
                raise Task1CommandBlocked(
                    "v47 design lab schema is incomplete: " + "; ".join(problems)
                )
            if predecessor_schema_version not in {"20260811_45", "20260813_46"}:
                raise Task1CommandBlocked(
                    "v47 recovery/repeat has no allowed predecessor source marker"
                )
            if recorded_sources and recorded_sources != {str(predecessor_schema_version)}:
                raise Task1CommandBlocked(
                    "v47 migration runs disagree with the predecessor source marker"
                )
            original_source_schema_version = str(predecessor_schema_version)
            schema_version = SCHEMA_VERSION
        else:
            schema_version = upgrade_database(
                engine, strategy_platform_v13_controlled=True
            )
        result = execute_strategy_platform_v13_task1(
            engine,
            market_inventory=inventory,
            actor=args.actor,
            request_id=args.request_id,
            execution_scope="DESIGN_LAB",
            aggregate_source_matrix_status=matrix_status,
            report_path=report_identity,
            source_schema_version=original_source_schema_version,
            evidence_manifest=evidence_manifest,
        )
        report = {
            "contract": "strategy-platform-v13-task1-command-report-v1",
            "database": database_name,
            "source_schema_version": original_source_schema_version,
            "entry_schema_version": source_schema_version,
            "schema_version": schema_version,
            "migration_run_id": result.migration_run_id,
            "repeat_noop": result.repeat_noop,
            "source_snapshot_digest": result.source_snapshot_digest,
            "target_snapshot_digest": result.target_snapshot_digest,
            "counts": {
                "strategy_targets": result.strategy_target_count,
                "mapped_versions": result.mapped_version_count,
                "mapped_plans": result.mapped_plan_count,
                "mapped_windows": result.mapped_window_count,
                "blocked_summaries": result.blocked_summary_count,
                "market_files": result.market_file_count,
                "unmapped": result.unmapped_count,
                "conflicts": result.conflict_count,
            },
            "market_evidence_manifest": evidence_manifest,
            "market_freshness": "UNKNOWN",
            "credential_attestation": "OUT_OF_SCOPE",
            "runtime_execution_evidence": "UNKNOWN",
            "reconciliation": result.report,
        }
        _, artifact_digest, artifact_identity = _persist_command_report(
            args.report_file,
            report_identity,
            report,
            repeat_noop=result.repeat_noop,
        )
        output = {
            **report,
            "report_artifact_identity": artifact_identity,
            "first_report_identity": report_identity,
            "report_artifact_sha256": artifact_digest,
        }
        print(canonical_json(output))
        return 0
    except Exception as exc:
        print(f"Task 1 design-lab migration blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
