from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
from pathlib import Path
from typing import Any

import pytest

from app.db import strategy_platform_v13_task1 as task1
from app.db.strategy_platform_v13_task1 import (
    MarketFileEvidence,
    StrategyPlatformTask1Blocked,
    canonical_digest,
    validate_task1_evidence_manifest,
)


_QUALITY_SCOPE = "MIGRATION_SOURCE_CONSISTENCY_AS_OF_SOURCE_RECEIPT"
_QUALITY_DECISION = "NOT_STRATEGY_QUALIFICATION"
_FRESHNESS_BASIS = "ORIGINAL_AGGREGATE_RECEIPT_DOWNLOADED_AT"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _inventory() -> tuple[MarketFileEvidence, ...]:
    observed_at = datetime(2026, 8, 13, 4, 44, tzinfo=timezone.utc)
    first_open = datetime(2023, 7, 1, tzinfo=timezone.utc)
    last_close = datetime(2026, 2, 1, tzinfo=timezone.utc)
    aggregate_digest = _digest("corrected-aggregate")
    artifact_digest = _digest("migration-artifact")
    records: list[MarketFileEvidence] = []
    for pair in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"):
        instrument = f"{pair.split('/')[0]}-USDT-SWAP"
        for timeframe, interval in (("5m", 300), ("15m", 900)):
            target = f"{pair}:{timeframe}"
            records.append(
                MarketFileEvidence(
                    exchange="okx",
                    market_type="futures",
                    pair=pair,
                    instrument_id=instrument,
                    timeframe=timeframe,
                    data_kind="futures",
                    absolute_path=f"/design-lab/{instrument}-{timeframe}.feather",
                    relative_path=f"futures/{instrument}-{timeframe}.feather",
                    file_format="feather",
                    size_bytes=4096,
                    sha256=_digest(f"file:{target}"),
                    row_count=100,
                    first_open_at=first_open,
                    last_open_at=last_close - timedelta(seconds=interval),
                    last_close_at=last_close,
                    expected_interval_seconds=interval,
                    gap_count=0,
                    duplicate_count=0,
                    null_count=0,
                    freshness_status="UNKNOWN",
                    observed_at=observed_at,
                    source_receipt_digest=_digest(f"source-receipt:{target}"),
                    inspected_at=observed_at,
                    file_identity_digest=_digest(f"file-identity:{target}"),
                    source_identity_digest=_digest(f"source-identity:{target}"),
                    aggregate_receipt_digest=aggregate_digest,
                    migration_artifact_digest=artifact_digest,
                    source_type="OKX_PUBLIC_MARKET_DATA",
                    source_receipt_path=f"receipts/{instrument}-{timeframe}.json",
                    source_response_chain_digest=_digest(f"response-chain:{target}"),
                    quality_scope=_QUALITY_SCOPE,
                    quality_decision=_QUALITY_DECISION,
                    freshness_basis=_FRESHNESS_BASIS,
                    out_of_order_count=0,
                    misaligned_timestamp_count=0,
                    invalid_ohlc_count=0,
                    negative_volume_count=0,
                )
            )
    return tuple(records)


def _evidence_manifest(
    inventory: tuple[MarketFileEvidence, ...],
) -> dict[str, Any]:
    return {
        "schema_version": "strategy-platform-v13-migration-market-evidence-v1",
        "status_scope": _QUALITY_SCOPE,
        "freshness_basis": _FRESHNESS_BASIS,
        "artifact_generation_delay_seconds": 55027,
        "artifact_digest": _digest("migration-artifact"),
        "artifact_file_sha256": _digest("artifact-file"),
        "legacy_aggregate_receipt": {
            "status": "BLOCKED",
            "sha256": _digest("legacy-aggregate"),
            "snapshot_digest": _digest("legacy-snapshot"),
            "report_digest": _digest("legacy-report"),
        },
        "corrected_matrix": {
            "status": "PASSED",
            "artifact_sha256": _digest("corrected-aggregate"),
            "snapshot_digest": _digest("corrected-snapshot"),
            "report_digest": _digest("corrected-report"),
        },
        "market_snapshot": {
            "status": "PASSED",
            "snapshot_digest": _digest("market-snapshot"),
            "report_digest": _digest("market-report"),
        },
        "files": [
            {
                "target_key": (
                    f"{item.exchange.lower()}|{item.market_type.lower()}|"
                    f"{item.pair}|{item.timeframe}|{item.data_kind.lower()}"
                ),
                "file_identity_digest": item.file_identity_digest,
                "source_identity_digest": item.source_identity_digest,
                "file_sha256": item.sha256,
                "source_receipt_digest": item.source_receipt_digest,
            }
            for item in inventory
        ],
    }


def test_validate_task1_evidence_manifest_accepts_six_historical_files() -> None:
    inventory = _inventory()
    manifest = _evidence_manifest(inventory)

    validated = validate_task1_evidence_manifest(manifest, inventory)

    assert len(validated["files"]) == 6
    assert validated["legacy_aggregate_receipt"]["status"] == "BLOCKED"
    assert validated["corrected_matrix"]["status"] == "PASSED"
    assert validated["freshness_basis"] == _FRESHNESS_BASIS
    assert validated["artifact_generation_delay_seconds"] == 55027
    assert validated["installed_adapter_manifest"]["adapter_count"] == 16
    assert len(validated["installed_adapter_manifest"]["digest"]) == 64


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("current_fresh", "market provenance is invalid"),
        ("preexisting_receipt", "market provenance is invalid"),
        ("identity_mismatch", "market provenance is invalid"),
        ("source_digest_mismatch", "market provenance is invalid"),
        ("malformed_artifact_digest", "contains an invalid digest"),
        ("legacy_not_blocked", "safety/provenance shape is invalid"),
    ),
)
def test_validate_task1_evidence_manifest_rejects_unsafe_provenance(
    mutation: str, message: str
) -> None:
    inventory = _inventory()
    manifest = _evidence_manifest(inventory)
    if mutation == "current_fresh":
        inventory = (replace(inventory[0], freshness_status="PASSED"), *inventory[1:])
    elif mutation == "preexisting_receipt":
        inventory = (replace(inventory[0], receipt_id=47), *inventory[1:])
    elif mutation == "identity_mismatch":
        manifest["files"][0]["file_identity_digest"] = _digest("wrong-identity")
    elif mutation == "source_digest_mismatch":
        manifest["files"][0]["source_receipt_digest"] = _digest("wrong-source")
    elif mutation == "malformed_artifact_digest":
        manifest["artifact_digest"] = "not-a-sha256"
    elif mutation == "legacy_not_blocked":
        manifest["legacy_aggregate_receipt"]["status"] = "PASSED"
    else:  # pragma: no cover - the parameter table is exhaustive
        raise AssertionError(mutation)

    with pytest.raises(StrategyPlatformTask1Blocked, match=message):
        validate_task1_evidence_manifest(manifest, inventory)


def test_market_scan_identity_ignores_observed_absolute_locator() -> None:
    item = _inventory()[0]
    relocated = replace(
        item,
        absolute_path=f"/relocated-owner-root/{Path(item.absolute_path).name}",
    )

    assert task1._market_file_scan_payload(item) == task1._market_file_scan_payload(
        relocated
    )
    assert task1._market_file_scan_digest(item) == task1._market_file_scan_digest(
        relocated
    )
    assert "absolute_path" not in task1._market_file_scan_payload(item)


def test_market_quality_receipt_payload_and_idempotency_are_recomputable() -> None:
    item = _inventory()[0]

    payload = task1._market_quality_receipt_payload(item)
    evidence_digest = canonical_digest(payload)
    idempotency_key = task1._market_quality_receipt_idempotency_key(item)

    expected_idempotency_payload = {
        "contract": "market-data-quality-v13-idempotency-v1",
        "file_identity_digest": item.file_identity_digest,
        "source_identity_digest": item.source_identity_digest,
        "aggregate_receipt_digest": item.aggregate_receipt_digest,
        "migration_artifact_digest": item.migration_artifact_digest,
    }
    assert idempotency_key == canonical_digest(expected_idempotency_payload)
    assert evidence_digest == canonical_digest(deepcopy(payload))
    assert payload["contract"] == "market-data-quality-v13-receipt-digest-v1"
    assert payload["contract_version"] == "market-data-quality-v13-v1"
    assert payload["quality_scope"] == _QUALITY_SCOPE
    assert payload["quality_decision"] == "NOT_STRATEGY_QUALIFICATION"
    assert payload["freshness_seconds"] is None
    assert payload["status"] == "PASSED"
    assert payload["reason_codes"] == []
    assert payload["file_identity_digest"] == item.file_identity_digest
    assert payload["source_identity_digest"] == item.source_identity_digest


def test_legacy_snapshot_excludes_v13_receipts_and_new_provenance_columns() -> None:
    excluded = set(
        task1._LEGACY_SNAPSHOT_EXCLUDED_COLUMNS["market_data_quality_receipts"]
    )
    assert {
        "idempotency_key",
        "quality_scope",
        "quality_decision",
        "file_identity_digest",
        "source_identity_digest",
        "aggregate_receipt_digest",
        "migration_artifact_digest",
        "freshness_basis",
    }.issubset(excluded)
    assert "market_data_quality_receipts" in task1.LEGACY_ENTITY_TABLES

    table_fact_source = inspect.getsource(task1._table_fact)
    assert "legacy_content and table_name == \"market_data_quality_receipts\"" in table_fact_source
    assert "contract_version <> 'market-data-quality-v13-v1'" in table_fact_source

    target_snapshot_source = inspect.getsource(task1._collect_target_snapshot)
    assert "legacy_content=False" in target_snapshot_source
    assert "table_name in LEGACY_ENTITY_TABLES" not in target_snapshot_source


class _FakeResult:
    def __init__(self, *, first: Any = None, scalar: Any = None) -> None:
        self._first = first
        self._scalar = scalar

    def mappings(self) -> _FakeResult:
        return self

    def first(self) -> Any:
        return self._first

    def scalar_one(self) -> Any:
        return self._scalar


class _RecordingConnection:
    def __init__(self, results: list[_FakeResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self.calls.append((str(statement), dict(parameters or {})))
        if not self.results:
            return _FakeResult()
        return self.results.pop(0)


def test_planned_migration_run_binds_evidence_manifest_and_digest() -> None:
    manifest = _evidence_manifest(_inventory())
    connection = _RecordingConnection(
        [_FakeResult(first=None), _FakeResult(first=None), _FakeResult(scalar=101), _FakeResult()]
    )

    run_id, status = task1._ensure_migration_run(
        connection,  # type: ignore[arg-type]
        execution_scope="DESIGN_LAB",
        source_schema_version="strategy-platform-v1.3-45",
        source_snapshot_digest=_digest("source-snapshot"),
        actor="task1-test",
        request_id="task1-test-request",
        report_path="reports/task1.json",
        evidence_manifest=manifest,
    )

    insert_sql, parameters = next(
        (sql, params)
        for sql, params in connection.calls
        if "INSERT INTO strategy_platform_migration_runs" in sql
    )
    assert run_id == 101
    assert status == "RUNNING"
    assert "evidence_manifest,evidence_manifest_digest" in insert_sql
    assert parameters["evidence_manifest"] == task1._json_parameter(manifest)
    assert parameters["evidence_digest"] == canonical_digest(manifest)


@pytest.mark.parametrize(
    ("error", "expected_status"),
    (
        (StrategyPlatformTask1Blocked("EVIDENCE_BLOCKED: unsafe"), "BLOCKED"),
        (RuntimeError("unexpected failure"), "FAILED"),
    ),
)
def test_failed_or_blocked_migration_run_binds_evidence_manifest_and_digest(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: str,
) -> None:
    manifest = _evidence_manifest(_inventory())
    monkeypatch.setattr(task1, "collect_legacy_snapshot", lambda _connection: ())
    monkeypatch.setattr(task1, "_snapshot_digest", lambda _facts: _digest("source"))
    connection = _RecordingConnection(
        [_FakeResult(first=None), _FakeResult(scalar=202), _FakeResult()]
    )

    run_id = task1._record_blocked_migration_attempt(
        connection,  # type: ignore[arg-type]
        actor="task1-test",
        request_id=f"task1-{expected_status.lower()}",
        execution_scope="DESIGN_LAB",
        source_schema_version="strategy-platform-v1.3-45",
        report_path="reports/task1.json",
        evidence_manifest=manifest,
        error=error,
    )

    insert_sql, parameters = next(
        (sql, params)
        for sql, params in connection.calls
        if "INSERT INTO strategy_platform_migration_runs" in sql
    )
    assert run_id == 202
    assert "evidence_manifest," in insert_sql
    assert "evidence_manifest_digest" in insert_sql
    assert parameters["status"] == expected_status
    assert parameters["evidence"] == task1._json_parameter(manifest)
    assert parameters["evidence_digest"] == canonical_digest(manifest)
