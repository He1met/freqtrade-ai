#!/usr/bin/env python3
"""Build and reconcile read-only V1.3 market-data migration evidence.

The persistent file identity emitted by this tool is always relative to one
explicit canonical data root.  Absolute paths are accepted only as transient
input locators when they resolve inside that root; they never participate in a
stable identity digest.  Existing receipts that persist an absolute or
otherwise non-canonical path remain BLOCKED even when the locator can be
resolved safely.  This distinction makes the current absolute/relative
contract mismatch auditable instead of silently treating it as a pass.

The command does not download, repair, rewrite, index in PostgreSQL, create a
strategy, or invoke a validation/runtime/order path.  Snapshot and comparison
results are written to stdout only.

An expected-target manifest is explicit rather than inferred from observed
files.  Its ``schema_version`` is
``strategy-platform-v13-market-target-manifest-v1`` and every target supplies
``exchange``, ``market_type``, ``pair``, ``instrument_id``, ``timeframe``,
``data_kind``, ``path``, and ``max_close_lag_seconds``.  ``path`` may be a safe
input locator, but emitted identity is always the canonical data-root-relative
POSIX path.  A main Task 1 migrator can consume the emitted ``target_contract``,
``file_evidence``, ``source_evidence``, and ``aggregate_binding`` records; it
must not use ``observed_absolute_path`` as a persistent identity.

``migration-evidence`` preserves the legacy receipt's BLOCKED classification
and separately validates a materialized correction.  That correction may add
only the root-contract fields and replace source path locators with their exact
canonical relative identities.  Its freshness basis is the source receipt's
``downloaded_at``; generation delay is disclosed and is not currentness proof.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Iterable, Literal, Mapping, Sequence

import pandas as pd


REPORT_SCHEMA_VERSION = "strategy-platform-v13-market-data-evidence-v1"
COMPARISON_SCHEMA_VERSION = "strategy-platform-v13-market-data-reconciliation-v1"
MIGRATION_EVIDENCE_SCHEMA_VERSION = "strategy-platform-v13-migration-market-evidence-v1"
TARGET_MANIFEST_SCHEMA_VERSION = "strategy-platform-v13-market-target-manifest-v1"
PATH_CONTRACT_VERSION = "canonical-market-data-root-relative-v1"
FILE_IDENTITY_VERSION = "market-data-file-identity-v1"
SOURCE_IDENTITY_VERSION = "market-data-source-identity-v1"
CANONICAL_DATA_ROOT_KEY = "freqtrade-market-data"

Status = Literal["PASSED", "BLOCKED", "UNKNOWN"]
_STATUS_RANK: dict[Status, int] = {"PASSED": 0, "UNKNOWN": 1, "BLOCKED": 2}
_SUPPORTED_SUFFIXES = (".json.gz", ".feather", ".parquet", ".json", ".csv")
_TIMESTAMP_COLUMNS = ("date", "timestamp", "time", "open_time", "opentime")
_REQUIRED_OHLCV = ("open", "high", "low", "close", "volume")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TIMEFRAME = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>[smhdw])$")
_PUBLIC_ENDPOINT = "https://www.okx.com/api/v5/market/history-candles"


class EvidenceInputError(RuntimeError):
    """Raised when the command cannot safely interpret its input contract."""


class NonCandleJson(ValueError):
    """Raised when a JSON artifact is metadata rather than candle rows."""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime | pd.Timestamp | None) -> str | None:
    if value is None:
        return None
    converted = value.to_pydatetime() if isinstance(value, pd.Timestamp) else value
    return _utc(converted).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def canonical_json(value: Any, *, pretty: bool = False) -> str:
    """Serialize evidence deterministically without machine-specific helpers."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _combine_status(statuses: Iterable[Status]) -> Status:
    result: Status = "PASSED"
    for status_value in statuses:
        if _STATUS_RANK[status_value] > _STATUS_RANK[result]:
            result = status_value
    return result


@dataclass
class Findings:
    blocked: set[str]
    unknown: set[str]

    @classmethod
    def empty(cls) -> "Findings":
        return cls(set(), set())

    def block(self, reason: str) -> None:
        self.blocked.add(reason)

    def mark_unknown(self, reason: str) -> None:
        self.unknown.add(reason)

    @property
    def status(self) -> Status:
        if self.blocked:
            return "BLOCKED"
        if self.unknown:
            return "UNKNOWN"
        return "PASSED"

    def as_dict(self) -> dict[str, Any]:
        reasons = sorted(self.blocked | self.unknown)
        return {
            "status": self.status,
            "reason_codes": reasons,
            "blocked_reason_codes": sorted(self.blocked),
            "unknown_reason_codes": sorted(self.unknown),
        }


@dataclass(frozen=True)
class PathResolution:
    status: Status
    reason_codes: tuple[str, ...]
    input_reference_kind: str
    canonical_relative_path: str | None
    observed_absolute_path: str | None
    path: Path | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "input_reference_kind": self.input_reference_kind,
            "canonical_relative_path": self.canonical_relative_path,
            "observed_absolute_path": self.observed_absolute_path,
        }


def _contains_parent_reference(path: Path) -> bool:
    return any(part == ".." for part in path.parts)


def _relative_if_contained(path: Path, root: Path) -> Path | None:
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def _symlink_reason(root: Path, candidate: Path) -> str | None:
    """Return a reason if any path component below root is a symlink.

    Symlinks are rejected even when their target remains under the canonical
    root.  Otherwise two textual locators could become aliases for one file
    identity, and a later retarget could silently change that identity.
    """

    relative = _relative_if_contained(candidate, root)
    if relative is None:
        return "PATH_OUTSIDE_CANONICAL_ROOT"
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            return "PATH_COMPONENT_UNREADABLE"
        if stat.S_ISLNK(mode):
            return "SYMLINK_ALIAS_NOT_ALLOWED"
    return None


def resolve_market_data_path(
    raw_path: str | os.PathLike[str],
    *,
    canonical_data_root: Path,
    repository_root: Path | None = None,
) -> PathResolution:
    """Resolve an absolute, canonical-relative, or legacy repo-relative path.

    The returned stable identifier is always relative to
    ``canonical_data_root``.  Missing evidence is UNKNOWN.  A traversal,
    outside-root locator, ambiguous base, or symlink alias is BLOCKED.
    """

    root = canonical_data_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise EvidenceInputError("canonical data root is not a directory")
    repo = repository_root.expanduser().resolve(strict=True) if repository_root else None
    raw = Path(raw_path).expanduser()
    if _contains_parent_reference(raw):
        return PathResolution(
            "BLOCKED", ("PATH_TRAVERSAL_NOT_ALLOWED",), "INVALID", None, None, None
        )

    if raw.is_absolute():
        candidates = [("ABSOLUTE_INPUT", raw)]
    else:
        candidates = [("CANONICAL_DATA_ROOT_RELATIVE", root / raw)]
        if repo is not None:
            candidates.append(("REPOSITORY_ROOT_RELATIVE_LEGACY", repo / raw))

    safe: list[tuple[str, Path, Path]] = []
    outside = False
    for kind, lexical in candidates:
        absolute_lexical = Path(os.path.abspath(lexical))
        resolved = absolute_lexical.resolve(strict=False)
        if _relative_if_contained(resolved, root) is None:
            outside = True
            continue
        if _relative_if_contained(absolute_lexical, root) is None:
            # The locator reached the root through a symlinked/aliased prefix.
            outside = True
            continue
        symlink_reason = _symlink_reason(root, absolute_lexical)
        if symlink_reason:
            return PathResolution(
                "BLOCKED",
                (symlink_reason,),
                kind,
                None,
                str(absolute_lexical),
                None,
            )
        safe.append((kind, absolute_lexical, resolved))

    if not safe:
        reason = "PATH_OUTSIDE_CANONICAL_ROOT" if outside else "PATH_CANNOT_BE_RESOLVED"
        return PathResolution("BLOCKED", (reason,), "INVALID", None, None, None)

    existing = [item for item in safe if item[1].is_file()]
    distinct_existing = {str(item[2]) for item in existing}
    if len(distinct_existing) > 1:
        return PathResolution(
            "BLOCKED", ("PATH_BASE_AMBIGUOUS",), "AMBIGUOUS", None, None, None
        )
    selected = existing[0] if existing else safe[0]
    kind, lexical, resolved = selected
    relative = _relative_if_contained(resolved, root)
    assert relative is not None
    canonical_relative = relative.as_posix()
    if not existing:
        return PathResolution(
            "UNKNOWN",
            ("DATA_FILE_MISSING",),
            kind,
            canonical_relative,
            str(lexical),
            lexical,
        )
    try:
        with lexical.open("rb"):
            pass
    except OSError:
        return PathResolution(
            "UNKNOWN",
            ("DATA_FILE_UNREADABLE",),
            kind,
            canonical_relative,
            str(lexical),
            lexical,
        )
    return PathResolution(
        "PASSED", (), kind, canonical_relative, str(lexical), lexical
    )


def _resolve_repository_artifact(raw_path: str | os.PathLike[str], repository_root: Path) -> PathResolution:
    repo = repository_root.expanduser().resolve(strict=True)
    raw = Path(raw_path).expanduser()
    if _contains_parent_reference(raw):
        return PathResolution("BLOCKED", ("ARTIFACT_PATH_TRAVERSAL",), "INVALID", None, None, None)
    lexical = Path(os.path.abspath(raw if raw.is_absolute() else repo / raw))
    resolved = lexical.resolve(strict=False)
    relative = _relative_if_contained(resolved, repo)
    if relative is None or _relative_if_contained(lexical, repo) is None:
        return PathResolution("BLOCKED", ("ARTIFACT_OUTSIDE_REPOSITORY_ROOT",), "INVALID", None, None, None)
    symlink_reason = _symlink_reason(repo, lexical)
    if symlink_reason:
        return PathResolution("BLOCKED", (symlink_reason,), "REPOSITORY_ARTIFACT", None, str(lexical), None)
    if not lexical.is_file():
        return PathResolution(
            "UNKNOWN", ("ARTIFACT_MISSING",), "REPOSITORY_ARTIFACT", relative.as_posix(), str(lexical), lexical
        )
    try:
        with lexical.open("rb"):
            pass
    except OSError:
        return PathResolution(
            "UNKNOWN", ("ARTIFACT_UNREADABLE",), "REPOSITORY_ARTIFACT", relative.as_posix(), str(lexical), lexical
        )
    return PathResolution("PASSED", (), "REPOSITORY_ARTIFACT", relative.as_posix(), str(lexical), lexical)


def _timeframe_seconds(timeframe: object) -> int | None:
    if not isinstance(timeframe, str):
        return None
    match = _TIMEFRAME.fullmatch(timeframe)
    if match is None:
        return None
    multiplier = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }[match.group("unit")]
    return int(match.group("count")) * multiplier


def _supported_format(path: Path) -> str | None:
    lowered = path.name.lower()
    for suffix in _SUPPORTED_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix.lstrip(".")
    return None


def _open_json(path: Path) -> Any:
    opener = gzip.open if path.name.lower().endswith(".json.gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _json_frame(path: Path) -> pd.DataFrame:
    payload = _open_json(path)
    if isinstance(payload, dict):
        rows: Any | None = None
        for key in ("data", "candles", "rows"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        if rows is None:
            lowered = {str(key).lower() for key in payload}
            if lowered.intersection(_TIMESTAMP_COLUMNS):
                try:
                    return pd.DataFrame(payload)
                except ValueError:
                    return pd.DataFrame([payload])
            raise NonCandleJson("JSON artifact is not candle rows")
    elif isinstance(payload, list):
        rows = payload
    else:
        raise NonCandleJson("JSON artifact is not a row collection")
    frame = pd.DataFrame(rows)
    if len(frame.columns) >= 6 and all(isinstance(column, int) for column in frame.columns):
        frame = frame.rename(
            columns={index: name for index, name in enumerate(("date", *_REQUIRED_OHLCV))}
        )
    return frame


def _read_frame(path: Path, data_format: str) -> pd.DataFrame:
    if data_format == "feather":
        return pd.read_feather(path)
    if data_format == "parquet":
        return pd.read_parquet(path)
    if data_format == "csv":
        return pd.read_csv(path)
    return _json_frame(path)


def _timestamp_series(frame: pd.DataFrame) -> tuple[str | None, pd.Series | None]:
    by_lower = {str(column).lower(): column for column in frame.columns}
    selected: Any | None = next(
        (by_lower[name] for name in _TIMESTAMP_COLUMNS if name in by_lower), None
    )
    if selected is None and 0 in frame.columns:
        selected = 0
    if selected is None and "0" in frame.columns:
        selected = "0"
    if selected is None:
        return None, None
    raw = frame[selected]
    numeric = pd.to_numeric(raw, errors="coerce")
    nonnull = int(raw.notna().sum())
    if nonnull and int(numeric.notna().sum()) >= max(1, math.ceil(nonnull * 0.9)):
        median = float(numeric.dropna().abs().median()) if numeric.notna().any() else 0.0
        unit = "ns" if median >= 1e17 else "us" if median >= 1e14 else "ms" if median >= 1e11 else "s"
        parsed = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
    else:
        parsed = pd.to_datetime(raw, utc=True, errors="coerce")
    return str(selected), pd.Series(parsed, index=frame.index)


def _target_key(target: Mapping[str, Any]) -> str:
    return "|".join(
        str(target.get(key, ""))
        for key in ("exchange", "market_type", "pair", "timeframe", "data_kind")
    )


def _validate_target(target: Mapping[str, Any]) -> Findings:
    findings = Findings.empty()
    for key in ("exchange", "market_type", "pair", "instrument_id", "timeframe", "data_kind", "path"):
        if not isinstance(target.get(key), str) or not str(target[key]).strip():
            findings.mark_unknown(f"TARGET_{key.upper()}_MISSING")
    if _timeframe_seconds(target.get("timeframe")) is None:
        findings.mark_unknown("TARGET_TIMEFRAME_UNSUPPORTED")
    freshness = target.get("max_close_lag_seconds")
    if not isinstance(freshness, int) or isinstance(freshness, bool) or freshness < 0:
        findings.mark_unknown("TARGET_FRESHNESS_LIMIT_MISSING")
    return findings


def load_target_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise EvidenceInputError("expected-target manifest is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != TARGET_MANIFEST_SCHEMA_VERSION:
        raise EvidenceInputError("expected-target manifest schema is unsupported")
    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise EvidenceInputError("expected-target manifest must contain a non-empty targets list")
    if not all(isinstance(item, dict) for item in targets):
        raise EvidenceInputError("every expected target must be an object")
    return payload


def _inspect_candle_file(
    *,
    target: Mapping[str, Any],
    resolution: PathResolution,
    observed_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    findings = _validate_target(target)
    for reason in resolution.reason_codes:
        (findings.block if resolution.status == "BLOCKED" else findings.mark_unknown)(reason)
    evidence: dict[str, Any] = {
        **resolution.as_dict(),
        "format": None,
        "size_bytes": None,
        "sha256": None,
        "row_count": None,
        "timestamp_column": None,
        "first_open_at": None,
        "last_open_at": None,
        "last_close_at": None,
        "close_lag_seconds": None,
        "expected_interval_seconds": _timeframe_seconds(target.get("timeframe")),
        "invalid_timestamp_count": None,
        "duplicate_timestamp_count": None,
        "out_of_order_count": None,
        "missing_interval_count": None,
        "null_ohlcv_row_count": None,
        "file_identity_digest": None,
        "quality_decision": "NOT_STRATEGY_QUALIFICATION",
    }
    if resolution.status != "PASSED" or resolution.path is None:
        evidence.update(findings.as_dict())
        return evidence, None

    path = resolution.path
    data_format = _supported_format(path)
    if data_format is None:
        findings.mark_unknown("DATA_FORMAT_UNSUPPORTED")
        evidence.update(findings.as_dict())
        return evidence, None

    try:
        before = path.stat()
        digest_before = sha256_file(path)
        frame = _read_frame(path, data_format)
    except (OSError, ValueError, TypeError, ImportError):
        findings.mark_unknown("DATA_FILE_UNREADABLE")
        evidence.update(findings.as_dict())
        return evidence, None
    try:
        digest_after = sha256_file(path)
        after = path.stat()
    except OSError:
        findings.mark_unknown("DATA_FILE_UNREADABLE_AFTER_SCAN")
        evidence.update(findings.as_dict())
        return evidence, None
    if (
        digest_before != digest_after
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        findings.block("FILE_CHANGED_DURING_SCAN")

    timestamp_column, timestamps = _timestamp_series(frame)
    interval = evidence["expected_interval_seconds"]
    invalid = duplicates = out_of_order = missing = misaligned = None
    first = last = last_close = None
    close_lag = None
    if timestamp_column is None or timestamps is None:
        findings.mark_unknown("TIMESTAMP_COLUMN_UNKNOWN")
    else:
        invalid = int(timestamps.isna().sum())
        valid = timestamps.dropna()
        if invalid:
            findings.block("INVALID_TIMESTAMP_PRESENT")
        if valid.empty:
            findings.block("EMPTY_TIMESTAMP_COVERAGE")
        else:
            first = valid.min()
            last = valid.max()
            source_diffs = valid.diff().dt.total_seconds().dropna()
            duplicates = int(valid.duplicated().sum())
            out_of_order = int((source_diffs < 0).sum())
            if duplicates:
                findings.block("DUPLICATE_TIMESTAMP_PRESENT")
            if out_of_order:
                findings.block("OUT_OF_ORDER_TIMESTAMP_PRESENT")
            if interval is None:
                findings.mark_unknown("EXPECTED_INTERVAL_UNKNOWN")
            else:
                sorted_unique = valid.drop_duplicates().sort_values()
                gaps = sorted_unique.diff().dt.total_seconds().dropna()
                cadence_errors = sum(
                    float(delta) <= 0 or float(delta) % interval != 0
                    for delta in gaps
                )
                boundary_errors = sum(
                    int(timestamp.value) % (interval * 1_000_000_000) != 0
                    for timestamp in sorted_unique
                )
                misaligned = int(cadence_errors + boundary_errors)
                if misaligned:
                    findings.block("MISALIGNED_INTERVAL_PRESENT")
                missing = int(
                    sum(
                        max(0, math.floor(float(delta) / interval) - 1)
                        for delta in gaps
                        if float(delta) >= interval
                    )
                )
                if missing:
                    findings.block("MISSING_INTERVAL_PRESENT")
                last_close = last + pd.Timedelta(seconds=interval)
                close_lag = int((_utc(observed_at) - last_close.to_pydatetime()).total_seconds())
                if close_lag < 0:
                    findings.block("UNCLOSED_OR_FUTURE_CANDLE")
                freshness_limit = target.get("max_close_lag_seconds")
                if isinstance(freshness_limit, int) and not isinstance(freshness_limit, bool):
                    if close_lag > freshness_limit:
                        findings.block("LAST_CLOSE_STALE")

    lower_columns = {str(column).lower(): column for column in frame.columns}
    missing_ohlcv = [name for name in _REQUIRED_OHLCV if name not in lower_columns]
    if missing_ohlcv:
        null_ohlcv = None
        findings.block("REQUIRED_OHLCV_COLUMNS_MISSING")
    else:
        ohlcv = frame[[lower_columns[name] for name in _REQUIRED_OHLCV]]
        null_ohlcv = int(ohlcv.isna().any(axis=1).sum())
        if null_ohlcv:
            findings.block("NULL_OHLCV_PRESENT")
    if len(frame) == 0:
        findings.block("EMPTY_DATASET")

    file_identity_payload = {
        "version": FILE_IDENTITY_VERSION,
        "canonical_data_root_key": CANONICAL_DATA_ROOT_KEY,
        "path_contract_version": PATH_CONTRACT_VERSION,
        "canonical_relative_path": resolution.canonical_relative_path,
        "exchange": target.get("exchange"),
        "market_type": target.get("market_type"),
        "pair": target.get("pair"),
        "instrument_id": target.get("instrument_id"),
        "timeframe": target.get("timeframe"),
        "data_kind": target.get("data_kind"),
        "format": data_format,
    }
    evidence.update(
        {
            "format": data_format,
            "size_bytes": int(after.st_size),
            "sha256": digest_after,
            "row_count": int(len(frame)),
            "timestamp_column": timestamp_column,
            "first_open_at": _iso(first),
            "last_open_at": _iso(last),
            "last_close_at": _iso(last_close),
            "close_lag_seconds": close_lag,
            "invalid_timestamp_count": invalid,
            "duplicate_timestamp_count": duplicates,
            "out_of_order_count": out_of_order,
            "missing_interval_count": missing,
            "misaligned_interval_count": misaligned,
            "null_ohlcv_row_count": null_ohlcv,
            "file_identity_digest": canonical_sha256(file_identity_payload),
            "file_identity": file_identity_payload,
        }
    )
    evidence.update(findings.as_dict())
    return evidence, {"frame": frame}


def _inspect_sidecar(
    *,
    target: Mapping[str, Any],
    file_evidence: Mapping[str, Any],
    data_path: Path | None,
    canonical_data_root: Path,
) -> dict[str, Any]:
    findings = Findings.empty()
    result: dict[str, Any] = {
        "status": "UNKNOWN",
        "reason_codes": [],
        "blocked_reason_codes": [],
        "unknown_reason_codes": [],
        "canonical_relative_path": None,
        "observed_absolute_path": None,
        "sha256": None,
        "schema_version": None,
        "source_type": None,
        "endpoint": None,
        "instrument_id": None,
        "timeframe": None,
        "response_chain_sha256": None,
        "downloaded_at": None,
        "data_file_sha256": None,
        "parent_five_minute_sha256": None,
        "source_identity_digest": None,
        "source_identity": None,
    }
    if data_path is None:
        findings.mark_unknown("SOURCE_SIDECAR_UNAVAILABLE_WITHOUT_DATA_FILE")
        result.update(findings.as_dict())
        return result
    sidecar_path = data_path.with_suffix(data_path.suffix + ".source.json")
    resolution = resolve_market_data_path(
        sidecar_path,
        canonical_data_root=canonical_data_root,
    )
    result["canonical_relative_path"] = resolution.canonical_relative_path
    result["observed_absolute_path"] = resolution.observed_absolute_path
    if resolution.status != "PASSED" or resolution.path is None:
        for reason in resolution.reason_codes:
            findings.mark_unknown("SOURCE_SIDECAR_MISSING" if reason == "DATA_FILE_MISSING" else reason)
        result.update(findings.as_dict())
        return result
    try:
        payload = json.loads(resolution.path.read_text(encoding="utf-8"))
        digest = sha256_file(resolution.path)
    except (OSError, ValueError, TypeError):
        findings.mark_unknown("SOURCE_SIDECAR_UNREADABLE")
        result.update(findings.as_dict())
        return result
    if not isinstance(payload, dict):
        findings.mark_unknown("SOURCE_SIDECAR_SCHEMA_UNKNOWN")
        result.update(findings.as_dict())
        return result

    required_source_type = (
        "OKX_PUBLIC_REST" if target.get("timeframe") == "5m" else "DERIVED_FROM_OKX_PUBLIC_REST"
    )
    comparisons = {
        "SOURCE_SIDECAR_SCHEMA_MISMATCH": payload.get("schema_version") == "okx-public-candle-file-source-v1",
        "SOURCE_ENDPOINT_MISMATCH": payload.get("endpoint") == _PUBLIC_ENDPOINT,
        "SOURCE_INSTRUMENT_MISMATCH": payload.get("instrument_id") == target.get("instrument_id"),
        "SOURCE_TIMEFRAME_MISMATCH": payload.get("timeframe") == target.get("timeframe"),
        "SOURCE_TYPE_MISMATCH": payload.get("source_type") == required_source_type,
        "SOURCE_DATA_DIGEST_MISMATCH": payload.get("data_file_sha256") == file_evidence.get("sha256"),
        "SOURCE_CREDENTIAL_SCOPE_INVALID": payload.get("credentials_used") is False,
        "SOURCE_ACCOUNT_SCOPE_INVALID": payload.get("account_endpoint_used") is False,
        "SOURCE_ORDER_SCOPE_INVALID": payload.get("orders_submitted") is False,
    }
    for reason, matched in comparisons.items():
        if not matched:
            findings.block(reason)
    response_chain = payload.get("response_chain_sha256")
    if not isinstance(response_chain, str) or _SHA256.fullmatch(response_chain) is None:
        findings.mark_unknown("SOURCE_RESPONSE_CHAIN_DIGEST_MISSING")
    downloaded_at = _parse_datetime(payload.get("downloaded_at"))
    if downloaded_at is None:
        findings.mark_unknown("SOURCE_DOWNLOADED_AT_MISSING")
    parent = payload.get("parent_five_minute_sha256")
    if target.get("timeframe") != "5m" and (
        not isinstance(parent, str) or _SHA256.fullmatch(parent) is None
    ):
        findings.mark_unknown("SOURCE_PARENT_DIGEST_MISSING")

    source_identity = {
        "version": SOURCE_IDENTITY_VERSION,
        "file_identity_digest": file_evidence.get("file_identity_digest"),
        "content_sha256": file_evidence.get("sha256"),
        "sidecar_schema_version": payload.get("schema_version"),
        "endpoint": payload.get("endpoint"),
        "instrument_id": payload.get("instrument_id"),
        "timeframe": payload.get("timeframe"),
        "source_type": payload.get("source_type"),
        "response_chain_sha256": response_chain,
        "downloaded_at": _iso(downloaded_at),
        "parent_five_minute_sha256": parent,
    }
    result.update(
        {
            "sha256": digest,
            "schema_version": payload.get("schema_version"),
            "source_type": payload.get("source_type"),
            "endpoint": payload.get("endpoint"),
            "instrument_id": payload.get("instrument_id"),
            "timeframe": payload.get("timeframe"),
            "response_chain_sha256": response_chain,
            "downloaded_at": _iso(downloaded_at),
            "data_file_sha256": payload.get("data_file_sha256"),
            "parent_five_minute_sha256": parent,
            "source_identity_digest": canonical_sha256(source_identity),
            "source_identity": source_identity,
        }
    )
    result.update(findings.as_dict())
    return result


def _load_aggregate_receipt(
    *, receipt_path: Path | str, repository_root: Path
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    findings = Findings.empty()
    resolution = _resolve_repository_artifact(receipt_path, repository_root)
    result: dict[str, Any] = {
        **resolution.as_dict(),
        "sha256": None,
        "schema_version": None,
        "path_contract_version": None,
        "canonical_data_root_key": None,
        "downloaded_at": None,
    }
    if resolution.status != "PASSED" or resolution.path is None:
        for reason in resolution.reason_codes:
            (findings.block if resolution.status == "BLOCKED" else findings.mark_unknown)(reason)
        result.update(findings.as_dict())
        return result, None
    try:
        payload = json.loads(resolution.path.read_text(encoding="utf-8"))
        digest = sha256_file(resolution.path)
    except (OSError, ValueError, TypeError):
        findings.mark_unknown("AGGREGATE_RECEIPT_UNREADABLE")
        result.update(findings.as_dict())
        return result, None
    if not isinstance(payload, dict):
        findings.mark_unknown("AGGREGATE_RECEIPT_SCHEMA_UNKNOWN")
        result.update(findings.as_dict())
        return result, None
    if payload.get("schema_version") != "okx-public-candle-source-receipt-v1":
        findings.block("AGGREGATE_RECEIPT_SCHEMA_MISMATCH")
    for field, reason in (
        ("credentials_used", "AGGREGATE_CREDENTIAL_SCOPE_INVALID"),
        ("account_endpoint_used", "AGGREGATE_ACCOUNT_SCOPE_INVALID"),
        ("orders_submitted", "AGGREGATE_ORDER_SCOPE_INVALID"),
    ):
        if payload.get(field) is not False:
            findings.block(reason)
    if payload.get("execution_scope") != "PUBLIC_MARKET_DATA_ONLY":
        findings.block("AGGREGATE_EXECUTION_SCOPE_INVALID")
    # The legacy v1 receipt did not declare how its path strings were rooted.
    # We still resolve those strings for diagnostics, but do not call them a pass.
    if payload.get("path_contract_version") != PATH_CONTRACT_VERSION:
        findings.block("AGGREGATE_PATH_CONTRACT_MISSING_OR_MISMATCHED")
    if payload.get("canonical_data_root_key") != CANONICAL_DATA_ROOT_KEY:
        findings.block("AGGREGATE_DATA_ROOT_KEY_MISSING_OR_MISMATCHED")
    downloaded_at = _parse_datetime(payload.get("downloaded_at"))
    if downloaded_at is None:
        findings.mark_unknown("AGGREGATE_DOWNLOADED_AT_MISSING")
    if not isinstance(payload.get("sources"), list):
        findings.mark_unknown("AGGREGATE_SOURCES_MISSING")
    result.update(
        {
            "sha256": digest,
            "schema_version": payload.get("schema_version"),
            "path_contract_version": payload.get("path_contract_version"),
            "canonical_data_root_key": payload.get("canonical_data_root_key"),
            "downloaded_at": _iso(downloaded_at),
        }
    )
    result.update(findings.as_dict())
    return result, payload


def _canonical_receipt_path_compliant(raw_path: object, canonical_relative: str | None) -> bool:
    if not isinstance(raw_path, str) or not raw_path:
        return False
    candidate = Path(raw_path)
    return (
        not candidate.is_absolute()
        and not _contains_parent_reference(candidate)
        and "\\" not in raw_path
        and raw_path == canonical_relative
    )


def build_corrected_aggregate_receipt(
    original: Mapping[str, Any],
    *,
    canonical_data_root: Path,
    repository_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], Findings]:
    """Build the only permitted legacy correction: root contract plus paths."""

    corrected = json.loads(canonical_json(original))
    findings = Findings.empty()
    transformations: list[dict[str, Any]] = []
    sources = corrected.get("sources")
    if not isinstance(sources, list):
        findings.mark_unknown("AGGREGATE_SOURCES_MISSING")
        return corrected, transformations, findings
    for source in sources:
        if not isinstance(source, dict):
            findings.block("AGGREGATE_SOURCE_ENTRY_INVALID")
            continue
        for field in ("five_minute_path", "fifteen_minute_path"):
            raw_path = source.get(field)
            if not isinstance(raw_path, str):
                findings.mark_unknown("AGGREGATE_SOURCE_PATH_MISSING")
                continue
            resolution = resolve_market_data_path(
                raw_path,
                canonical_data_root=canonical_data_root,
                repository_root=repository_root,
            )
            if resolution.status != "PASSED" or resolution.canonical_relative_path is None:
                for reason in resolution.reason_codes:
                    (findings.block if resolution.status == "BLOCKED" else findings.mark_unknown)(
                        reason
                    )
                continue
            source[field] = resolution.canonical_relative_path
            transformations.append(
                {
                    "pair": source.get("pair"),
                    "field": field,
                    "original_path": raw_path,
                    "canonical_relative_path": resolution.canonical_relative_path,
                }
            )
    corrected["path_contract_version"] = PATH_CONTRACT_VERSION
    corrected["canonical_data_root_key"] = CANONICAL_DATA_ROOT_KEY
    return corrected, transformations, findings


def _aggregate_bindings(
    *,
    targets: Sequence[Mapping[str, Any]],
    records: Sequence[dict[str, Any]],
    aggregate_meta: Mapping[str, Any],
    aggregate_payload: Mapping[str, Any] | None,
    canonical_data_root: Path,
    repository_root: Path,
) -> tuple[dict[str, dict[str, Any]], Findings]:
    matrix_findings = Findings.empty()
    for reason in aggregate_meta.get("blocked_reason_codes", []):
        matrix_findings.block(str(reason))
    for reason in aggregate_meta.get("unknown_reason_codes", []):
        matrix_findings.mark_unknown(str(reason))

    def envelope_findings() -> Findings:
        inherited = Findings.empty()
        inherited.blocked.update(matrix_findings.blocked)
        inherited.unknown.update(matrix_findings.unknown)
        return inherited

    bindings: dict[str, dict[str, Any]] = {}
    if aggregate_payload is None:
        for target in targets:
            findings = envelope_findings()
            findings.mark_unknown("AGGREGATE_RECEIPT_UNAVAILABLE")
            bindings[_target_key(target)] = findings.as_dict()
        return bindings, matrix_findings

    sources = aggregate_payload.get("sources")
    if not isinstance(sources, list):
        sources = []
    by_pair: dict[str, list[Mapping[str, Any]]] = {}
    for item in sources:
        if isinstance(item, dict) and isinstance(item.get("pair"), str):
            by_pair.setdefault(item["pair"], []).append(item)
        else:
            matrix_findings.block("AGGREGATE_SOURCE_ENTRY_INVALID")
    expected_pairs = {str(target.get("pair")) for target in targets}
    if set(by_pair) != expected_pairs:
        matrix_findings.block("AGGREGATE_TARGET_SET_MISMATCH")
    if any(len(items) != 1 for items in by_pair.values()):
        matrix_findings.block("AGGREGATE_TARGET_DUPLICATE")

    record_by_key = {record["target_key"]: record for record in records}
    record_by_pair_tf = {
        (str(record["target"].get("pair")), str(record["target"].get("timeframe"))): record
        for record in records
    }
    receipt_downloaded = _parse_datetime(aggregate_payload.get("downloaded_at"))
    for target in targets:
        key = _target_key(target)
        findings = envelope_findings()
        detail: dict[str, Any] = {
            "status": "UNKNOWN",
            "reason_codes": [],
            "blocked_reason_codes": [],
            "unknown_reason_codes": [],
            "receipt_raw_path": None,
            "receipt_path_resolution": None,
            "receipt_file_sha256": None,
            "receipt_row_count": None,
            "receipt_first_open_at": None,
            "receipt_last_open_at": None,
        }
        pair_sources = by_pair.get(str(target.get("pair")), [])
        if len(pair_sources) != 1:
            if not pair_sources:
                findings.block("AGGREGATE_TARGET_MISSING")
            else:
                findings.block("AGGREGATE_TARGET_DUPLICATE")
            detail.update(findings.as_dict())
            bindings[key] = detail
            continue
        source = pair_sources[0]
        timeframe = target.get("timeframe")
        if timeframe not in {"5m", "15m"}:
            findings.mark_unknown("AGGREGATE_TIMEFRAME_UNSUPPORTED")
            detail.update(findings.as_dict())
            bindings[key] = detail
            matrix_findings.unknown.update(findings.unknown)
            continue
        prefix = "five_minute" if timeframe == "5m" else "fifteen_minute"
        raw_path = source.get(f"{prefix}_path")
        if not isinstance(raw_path, str):
            findings.mark_unknown("AGGREGATE_SOURCE_PATH_MISSING")
            path_resolution = None
        else:
            path_resolution = resolve_market_data_path(
                raw_path,
                canonical_data_root=canonical_data_root,
                repository_root=repository_root,
            )
            for reason in path_resolution.reason_codes:
                (findings.block if path_resolution.status == "BLOCKED" else findings.mark_unknown)(reason)
            record = record_by_key[key]
            expected_relative = record["file_evidence"].get("canonical_relative_path")
            if path_resolution.canonical_relative_path != expected_relative:
                findings.block("AGGREGATE_SOURCE_PATH_IDENTITY_MISMATCH")
            if not _canonical_receipt_path_compliant(raw_path, path_resolution.canonical_relative_path):
                findings.block("AGGREGATE_SOURCE_PATH_NOT_CANONICAL_RELATIVE")

        record = record_by_key[key]
        file_evidence = record["file_evidence"]
        source_evidence = record["source_evidence"]
        receipt_sha = source.get(f"{prefix}_sha256")
        receipt_rows = source.get(
            "row_count" if timeframe == "5m" else "fifteen_minute_row_count"
        )
        if receipt_sha != file_evidence.get("sha256"):
            findings.block("AGGREGATE_SOURCE_DIGEST_MISMATCH")
        if receipt_rows != file_evidence.get("row_count"):
            findings.block("AGGREGATE_SOURCE_ROW_COUNT_MISMATCH")
        response_chain = source.get("response_chain_sha256")
        if response_chain != source_evidence.get("response_chain_sha256"):
            findings.block("AGGREGATE_SOURCE_RESPONSE_CHAIN_MISMATCH")
        source_downloaded = _parse_datetime(source.get("downloaded_at"))
        sidecar_downloaded = _parse_datetime(source_evidence.get("downloaded_at"))
        if receipt_downloaded is None or sidecar_downloaded is None:
            findings.mark_unknown("AGGREGATE_SOURCE_DOWNLOADED_AT_UNVERIFIABLE")
        elif sidecar_downloaded != receipt_downloaded:
            findings.block("AGGREGATE_SOURCE_DOWNLOADED_AT_MISMATCH")
        elif source_downloaded is not None and source_downloaded != receipt_downloaded:
            findings.block("AGGREGATE_SOURCE_DOWNLOADED_AT_MISMATCH")

        source_first = _parse_datetime(
            source.get(
                f"{prefix}_first_open_at",
                source.get(f"installed_{prefix}_first_open_at", source.get("first_open_at")),
            )
        )
        source_last = _parse_datetime(
            source.get(
                f"{prefix}_last_open_at",
                source.get(f"installed_{prefix}_last_open_at", source.get("last_open_at")),
            )
        )
        observed_first = _parse_datetime(file_evidence.get("first_open_at"))
        observed_last = _parse_datetime(file_evidence.get("last_open_at"))
        if (
            timeframe != "5m"
            and source_last is not None
            and f"{prefix}_last_open_at" not in source
            and f"installed_{prefix}_last_open_at" not in source
        ):
            interval = _timeframe_seconds(timeframe)
            if interval:
                epoch = int(source_last.timestamp())
                bucket = datetime.fromtimestamp(epoch - (epoch % interval), tz=timezone.utc)
                # A 15m candle is present only after all three source 5m opens.
                source_last = bucket if source_last >= bucket + timedelta(seconds=interval - 300) else bucket - timedelta(seconds=interval)
        if source_first is None or source_last is None:
            findings.mark_unknown("AGGREGATE_SOURCE_COVERAGE_MISSING")
        elif source_first != observed_first or source_last != observed_last:
            findings.block("AGGREGATE_SOURCE_COVERAGE_MISMATCH")

        if timeframe != "5m":
            parent = record_by_pair_tf.get((str(target.get("pair")), "5m"))
            if parent is None:
                findings.mark_unknown("PARENT_FIVE_MINUTE_TARGET_MISSING")
            elif source_evidence.get("parent_five_minute_sha256") != parent["file_evidence"].get("sha256"):
                findings.block("SOURCE_PARENT_FIVE_MINUTE_DIGEST_MISMATCH")

        detail.update(
            {
                "receipt_raw_path": raw_path,
                "receipt_path_resolution": path_resolution.as_dict() if path_resolution else None,
                "receipt_file_sha256": receipt_sha,
                "receipt_row_count": receipt_rows,
                "receipt_first_open_at": _iso(source_first),
                "receipt_last_open_at": _iso(source_last),
            }
        )
        detail.update(findings.as_dict())
        bindings[key] = detail
        for reason in findings.blocked:
            matrix_findings.block(reason)
        for reason in findings.unknown:
            matrix_findings.mark_unknown(reason)
    return bindings, matrix_findings


def collect_snapshot(
    *,
    canonical_data_root: Path,
    repository_root: Path,
    target_manifest: Mapping[str, Any],
    aggregate_receipt: Path | str,
    observed_at: datetime,
) -> dict[str, Any]:
    """Return a complete, read-only file/source/aggregate evidence snapshot."""

    canonical_data_root = canonical_data_root.expanduser().resolve(strict=True)
    repository_root = repository_root.expanduser().resolve(strict=True)
    targets_raw = target_manifest.get("targets")
    if target_manifest.get("schema_version") != TARGET_MANIFEST_SCHEMA_VERSION or not isinstance(targets_raw, list):
        raise EvidenceInputError("target manifest contract is invalid")
    targets: list[Mapping[str, Any]] = []
    for item in targets_raw:
        if not isinstance(item, dict):
            raise EvidenceInputError("target manifest contains a non-object target")
        targets.append(item)
    if not targets:
        raise EvidenceInputError("target manifest is empty")

    report_findings = Findings.empty()
    keys = [_target_key(target) for target in targets]
    if any(not part for key in keys for part in key.split("|")):
        report_findings.mark_unknown("EXPECTED_TARGET_IDENTITY_INCOMPLETE")
    if len(set(keys)) != len(keys):
        report_findings.block("EXPECTED_TARGET_DUPLICATE")

    records: list[dict[str, Any]] = []
    for target in sorted(targets, key=_target_key):
        resolution = resolve_market_data_path(
            str(target.get("path", "")),
            canonical_data_root=canonical_data_root,
            repository_root=repository_root,
        )
        file_evidence, _ = _inspect_candle_file(
            target=target,
            resolution=resolution,
            observed_at=_utc(observed_at),
        )
        source_evidence = _inspect_sidecar(
            target=target,
            file_evidence=file_evidence,
            data_path=resolution.path if resolution.status == "PASSED" else None,
            canonical_data_root=canonical_data_root,
        )
        records.append(
            {
                "target_key": _target_key(target),
                "target": dict(target),
                "file_evidence": file_evidence,
                "source_evidence": source_evidence,
                "aggregate_binding": None,
            }
        )

    aggregate_meta, aggregate_payload = _load_aggregate_receipt(
        receipt_path=aggregate_receipt,
        repository_root=repository_root,
    )
    bindings, matrix_findings = _aggregate_bindings(
        targets=targets,
        records=records,
        aggregate_meta=aggregate_meta,
        aggregate_payload=aggregate_payload,
        canonical_data_root=canonical_data_root,
        repository_root=repository_root,
    )
    for record in records:
        record["aggregate_binding"] = bindings[record["target_key"]]
        record["target_contract"] = {
            **{key: value for key, value in record["target"].items() if key != "path"},
            "canonical_relative_path": record["file_evidence"].get(
                "canonical_relative_path"
            ),
        }
        record["status"] = _combine_status(
            (
                record["file_evidence"]["status"],
                record["source_evidence"]["status"],
                record["aggregate_binding"]["status"],
            )
        )
        record["reason_codes"] = sorted(
            set(record["file_evidence"]["reason_codes"])
            | set(record["source_evidence"]["reason_codes"])
            | set(record["aggregate_binding"]["reason_codes"])
        )

    canonical_target_manifest = {
        "schema_version": TARGET_MANIFEST_SCHEMA_VERSION,
        "path_contract_version": PATH_CONTRACT_VERSION,
        "canonical_data_root_key": CANONICAL_DATA_ROOT_KEY,
        "targets": sorted(
            (record["target_contract"] for record in records),
            key=lambda target: "|".join(
                str(target.get(key, ""))
                for key in ("exchange", "market_type", "pair", "timeframe", "data_kind")
            ),
        ),
    }
    canonical_target_manifest_digest = canonical_sha256(canonical_target_manifest)

    for reason in matrix_findings.blocked:
        report_findings.block(reason)
    for reason in matrix_findings.unknown:
        report_findings.mark_unknown(reason)
    file_scan_status = _combine_status(record["file_evidence"]["status"] for record in records)
    source_sidecar_status = _combine_status(record["source_evidence"]["status"] for record in records)
    matrix_status = matrix_findings.status
    overall = _combine_status(
        (report_findings.status, file_scan_status, source_sidecar_status, matrix_status)
    )

    stable_files = [_stable_file_record(record, aggregate_meta) for record in records]
    snapshot_digest_payload = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "path_contract_version": PATH_CONTRACT_VERSION,
        "canonical_data_root_key": CANONICAL_DATA_ROOT_KEY,
        "expected_target_manifest_digest": canonical_target_manifest_digest,
        "observed_at": _iso(observed_at),
        "files": stable_files,
        "status": overall,
    }
    overall_reason_codes = sorted(
        report_findings.blocked
        | report_findings.unknown
        | {reason for record in records for reason in record["reason_codes"]}
    )
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "status": overall,
        "reason_codes": overall_reason_codes,
        "path_contract": {
            "version": PATH_CONTRACT_VERSION,
            "canonical_data_root_key": CANONICAL_DATA_ROOT_KEY,
            "persistent_path_form": "POSIX_RELATIVE_TO_CANONICAL_DATA_ROOT",
            "absolute_paths_are_identity": False,
            "symlink_aliases_allowed": False,
            "canonical_data_root_observed": str(canonical_data_root),
            "repository_root_observed": str(repository_root),
        },
        "scope": {
            "filesystem_operation": "READ_ONLY_FULL_SCAN",
            "database_writes": False,
            "network_access": False,
            "credentials": "NOT_ACCESSED",
            "okx_live": "NOT_ACCESSED",
            "acl": "NOT_ACCESSED",
            "strategies_validation_jobs_orders_runtime": "NOT_CREATED_OR_INVOKED",
        },
        "observed_at": _iso(observed_at),
        "expected_target_manifest": {
            "schema_version": target_manifest.get("schema_version"),
            "digest": canonical_target_manifest_digest,
            "input_digest": canonical_sha256(target_manifest),
            "target_count": len(targets),
        },
        "aggregate_receipt": aggregate_meta,
        "files": records,
        "summary": {
            "status": overall,
            "file_scan_status": file_scan_status,
            "source_sidecar_status": source_sidecar_status,
            "source_matrix_status": matrix_status,
            "expected_target_count": len(targets),
            "file_status_counts": {
                status: sum(record["file_evidence"]["status"] == status for record in records)
                for status in ("PASSED", "BLOCKED", "UNKNOWN")
            },
            "matrix_status_counts": {
                status: sum(record["aggregate_binding"]["status"] == status for record in records)
                for status in ("PASSED", "BLOCKED", "UNKNOWN")
            },
            "reason_codes": overall_reason_codes,
            "unknown_values_are_not_zero": True,
            "strategy_qualification_performed": False,
        },
        "snapshot_digest": canonical_sha256(snapshot_digest_payload),
    }
    report["report_digest"] = canonical_sha256(_report_digest_payload(report))
    return report


def _stable_file_record(
    record: Mapping[str, Any], aggregate_meta: Mapping[str, Any]
) -> dict[str, Any]:
    file_evidence = record.get("file_evidence")
    source_evidence = record.get("source_evidence")
    binding = record.get("aggregate_binding")
    file_evidence = file_evidence if isinstance(file_evidence, dict) else {}
    source_evidence = source_evidence if isinstance(source_evidence, dict) else {}
    binding = binding if isinstance(binding, dict) else {}
    return {
        "target_key": record.get("target_key"),
        "target_contract": record.get("target_contract"),
        "file_identity_digest": file_evidence.get("file_identity_digest"),
        "canonical_relative_path": file_evidence.get("canonical_relative_path"),
        "format": file_evidence.get("format"),
        "size_bytes": file_evidence.get("size_bytes"),
        "sha256": file_evidence.get("sha256"),
        "row_count": file_evidence.get("row_count"),
        "first_open_at": file_evidence.get("first_open_at"),
        "last_open_at": file_evidence.get("last_open_at"),
        "last_close_at": file_evidence.get("last_close_at"),
        "source_identity_digest": source_evidence.get("source_identity_digest"),
        "source_sidecar_sha256": source_evidence.get("sha256"),
        "aggregate_receipt_sha256": aggregate_meta.get("sha256"),
        "file_status": file_evidence.get("status"),
        "source_status": source_evidence.get("status"),
        "aggregate_status": binding.get("status"),
    }


def _snapshot_digest_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    path_contract = snapshot.get("path_contract")
    manifest = snapshot.get("expected_target_manifest")
    aggregate_meta = snapshot.get("aggregate_receipt")
    files = snapshot.get("files")
    summary = snapshot.get("summary")
    path_contract = path_contract if isinstance(path_contract, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    aggregate_meta = aggregate_meta if isinstance(aggregate_meta, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    records = [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []
    return {
        "report_schema_version": snapshot.get("report_schema_version"),
        "path_contract_version": path_contract.get("version"),
        "canonical_data_root_key": path_contract.get("canonical_data_root_key"),
        "expected_target_manifest_digest": manifest.get("digest"),
        "observed_at": snapshot.get("observed_at"),
        "files": sorted(
            (_stable_file_record(record, aggregate_meta) for record in records),
            key=lambda item: str(item.get("target_key", "")),
        ),
        "status": snapshot.get("status", summary.get("status")),
    }


def _report_digest_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in report.items() if key != "report_digest"}
    path_contract = payload.get("path_contract")
    if isinstance(path_contract, dict):
        payload["path_contract"] = {
            key: value
            for key, value in path_contract.items()
            if not key.endswith("_observed")
        }
    return payload


def _snapshot_records(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if snapshot.get("report_schema_version") != REPORT_SCHEMA_VERSION:
        raise EvidenceInputError("snapshot schema is unsupported")
    files = snapshot.get("files")
    if not isinstance(files, list):
        raise EvidenceInputError("snapshot files are missing")
    records: dict[str, Mapping[str, Any]] = {}
    for record in files:
        if not isinstance(record, dict) or not isinstance(record.get("target_key"), str):
            raise EvidenceInputError("snapshot contains an invalid file record")
        if record["target_key"] in records:
            raise EvidenceInputError("snapshot contains duplicate target keys")
        records[record["target_key"]] = record
    return records


def _snapshot_integrity(snapshot: Mapping[str, Any]) -> Findings:
    findings = Findings.empty()
    if snapshot.get("schema_version") != REPORT_SCHEMA_VERSION:
        findings.block("SNAPSHOT_SCHEMA_VERSION_MISMATCH")
    path_contract = snapshot.get("path_contract")
    if not isinstance(path_contract, dict):
        findings.mark_unknown("SNAPSHOT_PATH_CONTRACT_UNKNOWN")
    else:
        if path_contract.get("version") != PATH_CONTRACT_VERSION:
            findings.block("SNAPSHOT_PATH_CONTRACT_MISMATCH")
        if path_contract.get("canonical_data_root_key") != CANONICAL_DATA_ROOT_KEY:
            findings.block("SNAPSHOT_DATA_ROOT_KEY_MISMATCH")

    snapshot_digest = snapshot.get("snapshot_digest")
    if not isinstance(snapshot_digest, str) or _SHA256.fullmatch(snapshot_digest) is None:
        findings.mark_unknown("SNAPSHOT_DIGEST_UNKNOWN")
    elif snapshot_digest != canonical_sha256(_snapshot_digest_payload(snapshot)):
        findings.block("SNAPSHOT_DIGEST_MISMATCH")
    report_digest = snapshot.get("report_digest")
    if not isinstance(report_digest, str) or _SHA256.fullmatch(report_digest) is None:
        findings.mark_unknown("REPORT_DIGEST_UNKNOWN")
    elif report_digest != canonical_sha256(_report_digest_payload(snapshot)):
        findings.block("REPORT_DIGEST_MISMATCH")

    files = snapshot.get("files")
    if not isinstance(files, list):
        findings.mark_unknown("SNAPSHOT_FILES_UNKNOWN")
        return findings
    component_statuses: list[Status] = []
    for record in files:
        if not isinstance(record, dict):
            findings.mark_unknown("SNAPSHOT_RECORD_UNKNOWN")
            continue
        file_evidence = record.get("file_evidence")
        source_evidence = record.get("source_evidence")
        aggregate_binding = record.get("aggregate_binding")
        if not all(
            isinstance(value, dict)
            for value in (file_evidence, source_evidence, aggregate_binding)
        ):
            findings.mark_unknown("SNAPSHOT_RECORD_COMPONENT_UNKNOWN")
            continue

        file_identity = file_evidence.get("file_identity")
        file_identity_digest = file_evidence.get("file_identity_digest")
        if not isinstance(file_identity, dict) or not isinstance(file_identity_digest, str):
            findings.mark_unknown("FILE_IDENTITY_DIGEST_UNKNOWN")
        elif canonical_sha256(file_identity) != file_identity_digest:
            findings.block("FILE_IDENTITY_DIGEST_MISMATCH")
        elif (
            file_identity.get("canonical_relative_path")
            != file_evidence.get("canonical_relative_path")
        ):
            findings.block("FILE_IDENTITY_PATH_MISMATCH")

        source_identity = source_evidence.get("source_identity")
        source_identity_digest = source_evidence.get("source_identity_digest")
        if not isinstance(source_identity, dict) or not isinstance(source_identity_digest, str):
            findings.mark_unknown("SOURCE_IDENTITY_DIGEST_UNKNOWN")
        elif canonical_sha256(source_identity) != source_identity_digest:
            findings.block("SOURCE_IDENTITY_DIGEST_MISMATCH")
        elif (
            source_identity.get("file_identity_digest") != file_identity_digest
            or source_identity.get("content_sha256") != file_evidence.get("sha256")
        ):
            findings.block("SOURCE_IDENTITY_FILE_BINDING_MISMATCH")

        target_contract = record.get("target_contract")
        if not isinstance(target_contract, dict):
            findings.mark_unknown("TARGET_CONTRACT_UNKNOWN")
        elif (
            target_contract.get("canonical_relative_path")
            != file_evidence.get("canonical_relative_path")
        ):
            findings.block("TARGET_CONTRACT_PATH_MISMATCH")

        statuses: list[Status] = []
        for component in (file_evidence, source_evidence, aggregate_binding):
            status_value = component.get("status")
            if status_value not in _STATUS_RANK:
                findings.mark_unknown("SNAPSHOT_COMPONENT_STATUS_UNKNOWN")
                statuses.append("UNKNOWN")
            else:
                statuses.append(status_value)
        recomputed_record_status = _combine_status(statuses)
        component_statuses.append(recomputed_record_status)
        if record.get("status") != recomputed_record_status:
            findings.block("SNAPSHOT_RECORD_STATUS_MISMATCH")
        if aggregate_binding.get("status") == "PASSED":
            resolution = aggregate_binding.get("receipt_path_resolution")
            if (
                not isinstance(resolution, dict)
                or resolution.get("canonical_relative_path")
                != file_evidence.get("canonical_relative_path")
                or not _canonical_receipt_path_compliant(
                    aggregate_binding.get("receipt_raw_path"),
                    file_evidence.get("canonical_relative_path"),
                )
            ):
                findings.block("AGGREGATE_BINDING_PATH_MISMATCH")

    summary = snapshot.get("summary")
    top_status = snapshot.get("status")
    summary_status = summary.get("status") if isinstance(summary, dict) else None
    if top_status not in _STATUS_RANK or summary_status not in _STATUS_RANK:
        findings.mark_unknown("SNAPSHOT_SUMMARY_STATUS_UNKNOWN")
    elif top_status != summary_status:
        findings.block("SNAPSHOT_SUMMARY_STATUS_MISMATCH")
    elif top_status == "PASSED" and _combine_status(component_statuses) != "PASSED":
        findings.block("SNAPSHOT_PASSED_WITH_NONPASSED_COMPONENT")
    return findings


def compare_snapshots(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Reconcile two snapshots without treating UNKNOWN as zero or equality."""

    before_records = _snapshot_records(before)
    after_records = _snapshot_records(after)
    findings = Findings.empty()
    for label, snapshot in (("BEFORE", before), ("AFTER", after)):
        integrity = _snapshot_integrity(snapshot)
        for reason in integrity.blocked:
            findings.block(f"{label}_{reason}")
        for reason in integrity.unknown:
            findings.mark_unknown(f"{label}_{reason}")
    before_keys = set(before_records)
    after_keys = set(after_records)
    missing = sorted(before_keys - after_keys)
    added = sorted(after_keys - before_keys)
    if missing:
        findings.block("FILES_MISSING_AFTER_MIGRATION")
    if added:
        findings.block("FILES_ADDED_AFTER_MIGRATION")
    before_manifest = before.get("expected_target_manifest")
    after_manifest = after.get("expected_target_manifest")
    before_manifest_digest = (
        before_manifest.get("digest") if isinstance(before_manifest, dict) else None
    )
    after_manifest_digest = (
        after_manifest.get("digest") if isinstance(after_manifest, dict) else None
    )
    if before_manifest_digest is None or after_manifest_digest is None:
        findings.mark_unknown("EXPECTED_TARGET_MANIFEST_DIGEST_UNKNOWN")
    elif before_manifest_digest != after_manifest_digest:
        findings.block("EXPECTED_TARGET_MANIFEST_CHANGED")

    before_receipt = before.get("aggregate_receipt")
    after_receipt = after.get("aggregate_receipt")
    receipt_fields = (
        "sha256",
        "schema_version",
        "path_contract_version",
        "canonical_data_root_key",
        "canonical_relative_path",
    )
    receipt_differences: dict[str, dict[str, Any]] = {}
    if not isinstance(before_receipt, dict) or not isinstance(after_receipt, dict):
        findings.mark_unknown("AGGREGATE_RECEIPT_RECONCILIATION_UNKNOWN")
    else:
        for field in receipt_fields:
            left = before_receipt.get(field)
            right = after_receipt.get(field)
            if left is None or right is None:
                findings.mark_unknown(f"AGGREGATE_RECEIPT_{field.upper()}_UNKNOWN")
            elif left != right:
                receipt_differences[field] = {"before": left, "after": right}
                findings.block(f"AGGREGATE_RECEIPT_{field.upper()}_MISMATCH")
    comparisons: list[dict[str, Any]] = []
    fields = (
        "canonical_relative_path",
        "file_identity_digest",
        "format",
        "size_bytes",
        "sha256",
        "row_count",
        "first_open_at",
        "last_open_at",
        "last_close_at",
    )
    for key in sorted(before_keys & after_keys):
        before_record = before_records[key]
        after_record = after_records[key]
        before_file = before_record.get("file_evidence")
        after_file = after_record.get("file_evidence")
        before_source = before_record.get("source_evidence")
        after_source = after_record.get("source_evidence")
        before_binding = before_record.get("aggregate_binding")
        after_binding = after_record.get("aggregate_binding")
        item_findings = Findings.empty()
        differences: dict[str, dict[str, Any]] = {}
        if not all(isinstance(value, dict) for value in (before_file, after_file, before_source, after_source)):
            item_findings.mark_unknown("RECONCILIATION_RECORD_INCOMPLETE")
        else:
            for field in fields:
                left = before_file.get(field)
                right = after_file.get(field)
                if left is None or right is None:
                    item_findings.mark_unknown(f"RECONCILIATION_{field.upper()}_UNKNOWN")
                elif left != right:
                    differences[field] = {"before": left, "after": right}
                    item_findings.block(f"RECONCILIATION_{field.upper()}_MISMATCH")
            for field in ("source_identity_digest", "sha256"):
                left = before_source.get(field)
                right = after_source.get(field)
                label = "source_sidecar_sha256" if field == "sha256" else field
                if left is None or right is None:
                    item_findings.mark_unknown(f"RECONCILIATION_{label.upper()}_UNKNOWN")
                elif left != right:
                    differences[label] = {"before": left, "after": right}
                    item_findings.block(f"RECONCILIATION_{label.upper()}_MISMATCH")
            if not isinstance(before_binding, dict) or not isinstance(after_binding, dict):
                item_findings.mark_unknown("RECONCILIATION_AGGREGATE_BINDING_UNKNOWN")
            else:
                binding_fields = (
                    "status",
                    "reason_codes",
                    "receipt_raw_path",
                    "receipt_file_sha256",
                    "receipt_row_count",
                    "receipt_first_open_at",
                    "receipt_last_open_at",
                )
                for field in binding_fields:
                    left = before_binding.get(field)
                    right = after_binding.get(field)
                    label = f"aggregate_binding_{field}"
                    if left is None or right is None:
                        item_findings.mark_unknown(
                            f"RECONCILIATION_{label.upper()}_UNKNOWN"
                        )
                    elif left != right:
                        differences[label] = {"before": left, "after": right}
                        item_findings.block(
                            f"RECONCILIATION_{label.upper()}_MISMATCH"
                        )
                before_resolution = before_binding.get("receipt_path_resolution")
                after_resolution = after_binding.get("receipt_path_resolution")
                for field in ("status", "canonical_relative_path"):
                    left = (
                        before_resolution.get(field)
                        if isinstance(before_resolution, dict)
                        else None
                    )
                    right = (
                        after_resolution.get(field)
                        if isinstance(after_resolution, dict)
                        else None
                    )
                    label = f"aggregate_binding_path_{field}"
                    if left is None or right is None:
                        item_findings.mark_unknown(
                            f"RECONCILIATION_{label.upper()}_UNKNOWN"
                        )
                    elif left != right:
                        differences[label] = {"before": left, "after": right}
                        item_findings.block(
                            f"RECONCILIATION_{label.upper()}_MISMATCH"
                        )
        comparisons.append(
            {
                "target_key": key,
                **item_findings.as_dict(),
                "differences": differences,
                "close_time_freshness": {
                    "before_close_lag_seconds": (
                        before_file.get("close_lag_seconds")
                        if isinstance(before_file, dict)
                        else None
                    ),
                    "after_close_lag_seconds": (
                        after_file.get("close_lag_seconds")
                        if isinstance(after_file, dict)
                        else None
                    ),
                    "before_file_status": (
                        before_file.get("status") if isinstance(before_file, dict) else "UNKNOWN"
                    ),
                    "after_file_status": (
                        after_file.get("status") if isinstance(after_file, dict) else "UNKNOWN"
                    ),
                },
            }
        )
        findings.blocked.update(item_findings.blocked)
        findings.unknown.update(item_findings.unknown)

    for label, snapshot in (("BEFORE", before), ("AFTER", after)):
        summary = snapshot.get("summary")
        status_value = summary.get("status") if isinstance(summary, dict) else None
        if status_value == "BLOCKED":
            findings.block(f"{label}_SNAPSHOT_BLOCKED")
        elif status_value != "PASSED":
            findings.mark_unknown(f"{label}_SNAPSHOT_NOT_PASSED")

    finding_summary = findings.as_dict()
    report: dict[str, Any] = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "report_schema_version": COMPARISON_SCHEMA_VERSION,
        "status": finding_summary["status"],
        "reason_codes": finding_summary["reason_codes"],
        "scope": {
            "operation": "READ_ONLY_EVIDENCE_COMPARISON",
            "database_writes": False,
            "files_modified": False,
            "unknown_values_are_not_zero": True,
        },
        "before_snapshot_digest": before.get("snapshot_digest"),
        "after_snapshot_digest": after.get("snapshot_digest"),
        "expected_target_manifest_comparison": {
            "before_digest": before_manifest_digest,
            "after_digest": after_manifest_digest,
            "matched": (
                before_manifest_digest is not None
                and before_manifest_digest == after_manifest_digest
            ),
        },
        "aggregate_receipt_comparison": {
            "status": (
                "BLOCKED"
                if receipt_differences
                else "UNKNOWN"
                if not isinstance(before_receipt, dict)
                or not isinstance(after_receipt, dict)
                or any(
                    before_receipt.get(field) is None or after_receipt.get(field) is None
                    for field in receipt_fields
                )
                else "PASSED"
            ),
            "differences": receipt_differences,
        },
        "missing_target_keys": missing,
        "added_target_keys": added,
        "comparisons": comparisons,
        "summary": {
            **finding_summary,
            "matched_target_count": len(before_keys & after_keys),
            "missing_target_count": len(missing),
            "added_target_count": len(added),
        },
    }
    report["report_digest"] = canonical_sha256(report)
    return report


def build_migration_market_evidence(
    *,
    canonical_data_root: Path,
    repository_root: Path,
    source_repository_root: Path | None = None,
    target_manifest: Mapping[str, Any],
    original_aggregate_receipt: Path | str,
    corrected_aggregate_receipt: Path | str,
    observed_at: datetime,
    generated_at: datetime,
) -> dict[str, Any]:
    """Bind a legacy receipt, its path-only correction, and one full scan."""

    source_repository_root = source_repository_root or repository_root
    original_meta, original_payload = _load_aggregate_receipt(
        receipt_path=original_aggregate_receipt,
        repository_root=source_repository_root,
    )
    corrected_meta, corrected_payload = _load_aggregate_receipt(
        receipt_path=corrected_aggregate_receipt,
        repository_root=repository_root,
    )
    correction_findings = Findings.empty()
    expected_corrected: dict[str, Any] | None = None
    transformations: list[dict[str, Any]] = []
    if original_payload is None:
        correction_findings.mark_unknown("ORIGINAL_AGGREGATE_RECEIPT_UNAVAILABLE")
    else:
        source_downloaded_at = _parse_datetime(original_payload.get("downloaded_at"))
        if source_downloaded_at is None:
            correction_findings.mark_unknown("ORIGINAL_AGGREGATE_DOWNLOADED_AT_UNKNOWN")
        elif _utc(observed_at) != source_downloaded_at:
            correction_findings.block("FRESHNESS_BASIS_NOT_SOURCE_RECEIPT_DOWNLOADED_AT")
        expected_corrected, transformations, expected_findings = (
            build_corrected_aggregate_receipt(
                original_payload,
                canonical_data_root=canonical_data_root,
                repository_root=source_repository_root,
            )
        )
        correction_findings.blocked.update(expected_findings.blocked)
        correction_findings.unknown.update(expected_findings.unknown)
    if corrected_payload is None:
        correction_findings.mark_unknown("CORRECTED_AGGREGATE_RECEIPT_UNAVAILABLE")
    elif expected_corrected is None:
        correction_findings.mark_unknown("CORRECTED_AGGREGATE_EXPECTATION_UNKNOWN")
    elif corrected_payload != expected_corrected:
        correction_findings.block("CORRECTED_AGGREGATE_NOT_PATH_ONLY_TRANSFORMATION")

    original_snapshot = collect_snapshot(
        canonical_data_root=canonical_data_root,
        repository_root=source_repository_root,
        target_manifest=target_manifest,
        aggregate_receipt=original_aggregate_receipt,
        observed_at=observed_at,
    )
    corrected_snapshot = collect_snapshot(
        canonical_data_root=canonical_data_root,
        repository_root=repository_root,
        target_manifest=target_manifest,
        aggregate_receipt=corrected_aggregate_receipt,
        observed_at=observed_at,
    )
    corrected_summary = corrected_snapshot.get("summary")
    corrected_status = (
        corrected_summary.get("status")
        if isinstance(corrected_summary, dict)
        and corrected_summary.get("status") in _STATUS_RANK
        else "UNKNOWN"
    )
    overall = _combine_status((correction_findings.status, corrected_status))
    corrected_reasons = (
        corrected_summary.get("reason_codes", [])
        if isinstance(corrected_summary, dict)
        else ["CORRECTED_MATRIX_SUMMARY_UNKNOWN"]
    )
    reasons = sorted(
        correction_findings.blocked
        | correction_findings.unknown
        | {str(reason) for reason in corrected_reasons}
    )
    observed_at_utc = _utc(observed_at)
    generated_at_utc = _utc(generated_at)
    freshness_records = [
        {
            "target_key": record.get("target_key"),
            "last_open_at": record.get("file_evidence", {}).get("last_open_at"),
            "last_close_at": record.get("file_evidence", {}).get("last_close_at"),
            "close_lag_seconds": record.get("file_evidence", {}).get(
                "close_lag_seconds"
            ),
            "status": record.get("file_evidence", {}).get("status", "UNKNOWN"),
        }
        for record in corrected_snapshot.get("files", [])
        if isinstance(record, dict)
    ]
    original_summary = original_snapshot.get("summary")
    report: dict[str, Any] = {
        "schema_version": MIGRATION_EVIDENCE_SCHEMA_VERSION,
        "status": overall,
        "reason_codes": reasons,
        "status_scope": "MIGRATION_SOURCE_CONSISTENCY_AS_OF_SOURCE_RECEIPT",
        "generated_at": _iso(generated_at_utc),
        "observed_at": _iso(observed_at_utc),
        "freshness_basis": "ORIGINAL_AGGREGATE_RECEIPT_DOWNLOADED_AT",
        "artifact_generation_delay_seconds": int(
            (generated_at_utc - observed_at_utc).total_seconds()
        ),
        "path_contract": {
            "version": PATH_CONTRACT_VERSION,
            "canonical_data_root_key": CANONICAL_DATA_ROOT_KEY,
            "persistent_path_form": "POSIX_RELATIVE_TO_CANONICAL_DATA_ROOT",
            "absolute_paths_are_observed_locators_only": True,
        },
        "source_legacy_classification": {
            "status": (
                original_summary.get("status")
                if isinstance(original_summary, dict)
                else "UNKNOWN"
            ),
            "reason_codes": (
                original_summary.get("reason_codes", [])
                if isinstance(original_summary, dict)
                else ["ORIGINAL_MATRIX_SUMMARY_UNKNOWN"]
            ),
            "aggregate_receipt": original_meta,
            "snapshot_digest": original_snapshot.get("snapshot_digest"),
            "report_digest": original_snapshot.get("report_digest"),
        },
        "correction_contract": {
            **correction_findings.as_dict(),
            "allowed_changes": [
                "path_contract_version",
                "canonical_data_root_key",
                "sources[].five_minute_path",
                "sources[].fifteen_minute_path",
            ],
            "transformation_count": len(transformations),
            "transformations": transformations,
            "expected_corrected_payload_digest": (
                canonical_sha256(expected_corrected)
                if expected_corrected is not None
                else None
            ),
            "observed_corrected_payload_digest": (
                canonical_sha256(corrected_payload)
                if corrected_payload is not None
                else None
            ),
        },
        "corrected_matrix": {
            "status": corrected_status,
            "reason_codes": corrected_reasons,
            "aggregate_receipt": corrected_meta,
            "snapshot_digest": corrected_snapshot.get("snapshot_digest"),
            "report_digest": corrected_snapshot.get("report_digest"),
        },
        "full_scan_classification": {
            "target_count": len(corrected_snapshot.get("files", [])),
            "file_status_counts": (
                corrected_summary.get("file_status_counts", {})
                if isinstance(corrected_summary, dict)
                else {}
            ),
            "source_sidecar_status": (
                corrected_summary.get("source_sidecar_status", "UNKNOWN")
                if isinstance(corrected_summary, dict)
                else "UNKNOWN"
            ),
            "source_matrix_status": (
                corrected_summary.get("source_matrix_status", "UNKNOWN")
                if isinstance(corrected_summary, dict)
                else "UNKNOWN"
            ),
            "freshness": freshness_records,
        },
        "market_snapshot": corrected_snapshot,
        "safety": {
            "market_files_modified": False,
            "production_receipt_modified": False,
            "database_writes": False,
            "network_access": False,
            "credentials": "NOT_ACCESSED",
            "okx_live": "NOT_ACCESSED",
            "acl": "NOT_ACCESSED",
            "strategies_validation_jobs_orders_runtime": "NOT_CREATED_OR_INVOKED",
        },
    }
    report["artifact_digest"] = canonical_sha256(report)
    return report


def _read_snapshot(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise EvidenceInputError("snapshot is unreadable") from exc
    if not isinstance(payload, dict):
        raise EvidenceInputError("snapshot must be one JSON object")
    return payload


def _parse_observed_at(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = _parse_datetime(value)
    if parsed is None:
        raise EvidenceInputError("--observed-at must be an ISO-8601 timestamp")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or reconcile read-only V1.3 market-data evidence."
    )
    parser.add_argument("--compact", action="store_true", help="emit compact canonical JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot", help="scan expected files and source receipts")
    snapshot.add_argument("--canonical-data-root", type=Path, required=True)
    snapshot.add_argument("--repository-root", type=Path, required=True)
    snapshot.add_argument("--expected-targets", type=Path, required=True)
    snapshot.add_argument("--aggregate-receipt", type=Path, required=True)
    snapshot.add_argument("--observed-at")
    compare = commands.add_parser("compare", help="compare two emitted snapshots")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    migration = commands.add_parser(
        "migration-evidence",
        help="bind a legacy aggregate receipt to its audited canonical correction",
    )
    migration.add_argument("--canonical-data-root", type=Path, required=True)
    migration.add_argument("--repository-root", type=Path, required=True)
    migration.add_argument("--source-repository-root", type=Path)
    migration.add_argument("--expected-targets", type=Path, required=True)
    migration.add_argument("--original-aggregate-receipt", type=Path, required=True)
    migration.add_argument("--corrected-aggregate-receipt", type=Path, required=True)
    migration.add_argument("--generated-at")
    return parser


def _result_status(report: Mapping[str, Any]) -> Status:
    top_level_status = report.get("status")
    if top_level_status in _STATUS_RANK:
        return top_level_status
    summary = report.get("summary")
    status_value = summary.get("status") if isinstance(summary, dict) else None
    return status_value if status_value in _STATUS_RANK else "UNKNOWN"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            manifest = load_target_manifest(args.expected_targets)
            report = collect_snapshot(
                canonical_data_root=args.canonical_data_root,
                repository_root=args.repository_root,
                target_manifest=manifest,
                aggregate_receipt=args.aggregate_receipt,
                observed_at=_parse_observed_at(args.observed_at),
            )
        elif args.command == "compare":
            report = compare_snapshots(
                _read_snapshot(args.before),
                _read_snapshot(args.after),
            )
        else:
            manifest = load_target_manifest(args.expected_targets)
            source_repository_root = (
                args.source_repository_root or args.repository_root
            ).expanduser().resolve(strict=True)
            _, original_payload = _load_aggregate_receipt(
                receipt_path=args.original_aggregate_receipt,
                repository_root=source_repository_root,
            )
            source_observed_at = (
                _parse_datetime(original_payload.get("downloaded_at"))
                if isinstance(original_payload, dict)
                else None
            )
            if source_observed_at is None:
                raise EvidenceInputError(
                    "original aggregate downloaded_at is required as freshness basis"
                )
            report = build_migration_market_evidence(
                canonical_data_root=args.canonical_data_root,
                repository_root=args.repository_root,
                source_repository_root=args.source_repository_root,
                target_manifest=manifest,
                original_aggregate_receipt=args.original_aggregate_receipt,
                corrected_aggregate_receipt=args.corrected_aggregate_receipt,
                observed_at=source_observed_at,
                generated_at=_parse_observed_at(args.generated_at),
            )
    except Exception as exc:  # secret-safe command boundary
        print(f"market-data evidence blocked: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    print(canonical_json(report, pretty=not args.compact))
    return 0 if _result_status(report) == "PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
