from __future__ import annotations

from datetime import datetime, timezone
import json
import stat
from pathlib import Path

import pandas as pd
import pytest

from scripts import migrate_strategy_platform_v13_task1 as command


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_REAL_EVIDENCE = (
    _REPOSITORY_ROOT
    / "reports/migrations/strategy-platform-v13-task1-market-evidence.json"
)


def _real_evidence_dependencies_available() -> bool:
    if not _REAL_EVIDENCE.is_file():
        return False
    payload = json.loads(_REAL_EVIDENCE.read_text(encoding="utf-8"))
    legacy_path = Path(
        payload["source_legacy_classification"]["aggregate_receipt"]
        ["observed_absolute_path"]
    )
    root = Path(
        payload["market_snapshot"]["path_contract"]
        ["canonical_data_root_observed"]
    )
    paths = [
        Path(row["file_evidence"]["observed_absolute_path"])
        for row in payload["market_snapshot"]["files"]
    ]
    paths.extend(
        Path(row["source_evidence"]["observed_absolute_path"])
        for row in payload["market_snapshot"]["files"]
    )
    return legacy_path.is_file() and root.is_dir() and all(path.is_file() for path in paths)


def test_canonical_artifact_digest_excludes_only_digest_field() -> None:
    payload = {"schema_version": command.EVIDENCE_CONTRACT, "value": [1, 2]}
    expected = command.evidence_canonical_sha256(payload)
    payload["artifact_digest"] = expected

    assert command._canonical_artifact_digest(payload) == expected

    payload["value"].append(3)
    assert command._canonical_artifact_digest(payload) != expected


def test_evidence_envelope_preserves_historical_freshness_scope() -> None:
    payload = {
        "schema_version": command.EVIDENCE_CONTRACT,
        "status": "PASSED",
        "reason_codes": [],
        "status_scope": command.STATUS_SCOPE,
        "freshness_basis": command.FRESHNESS_BASIS,
        "observed_at": "2026-08-12T13:27:18Z",
        "generated_at": "2026-08-13T04:44:25Z",
        "artifact_generation_delay_seconds": 55027,
        "path_contract": {
            "version": command.PATH_CONTRACT,
            "canonical_data_root_key": command.CANONICAL_ROOT_KEY,
            "persistent_path_form": "POSIX_RELATIVE_TO_CANONICAL_DATA_ROOT",
            "absolute_paths_are_observed_locators_only": True,
        },
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

    observed_at, generated_at, delay = command._verify_envelope(payload)

    assert observed_at == datetime(2026, 8, 12, 13, 27, 18, tzinfo=timezone.utc)
    assert generated_at == datetime(2026, 8, 13, 4, 44, 25, tzinfo=timezone.utc)
    assert delay == 55027

    payload["status_scope"] = "CURRENT_FRESHNESS"
    with pytest.raises(command.Task1CommandBlocked):
        command._verify_envelope(payload)


@pytest.mark.parametrize(
    "value",
    ("/absolute/file.feather", "../escape.feather", "a/../b.feather", "a\\b.feather"),
)
def test_canonical_relative_path_rejects_aliases(value: str) -> None:
    with pytest.raises(command.Task1CommandBlocked):
        command._safe_relative_path(value, field="path")


def test_classification_windows_use_formal_specs_and_real_close_values() -> None:
    timestamps = pd.Series(
        pd.date_range(
            "2023-07-01T00:00:00Z",
            "2026-02-01T00:00:00Z",
            freq="15min",
            inclusive="left",
        )
    )
    close = pd.Series(100.0, index=timestamps.index)

    def trend(start: str, end: str, first: float, last: float) -> None:
        selected = (timestamps >= pd.Timestamp(start)) & (timestamps < pd.Timestamp(end))
        close.loc[selected] = pd.Series(
            pd.array(
                [
                    first + (last - first) * index / (int(selected.sum()) - 1)
                    for index in range(int(selected.sum()))
                ],
                dtype="float64",
            ),
            index=close.loc[selected].index,
        )

    trend("2023-07-01T00:00:00Z", "2023-10-01T00:00:00Z", 100.0, 90.0)
    trend("2023-10-01T00:00:00Z", "2024-03-01T00:00:00Z", 100.0, 110.0)
    trend("2024-03-01T00:00:00Z", "2024-06-29T00:00:00Z", 100.0, 102.0)
    trend("2025-10-01T00:00:00Z", "2026-02-01T00:00:00Z", 100.0, 90.0)
    frame = pd.DataFrame({"date": timestamps, "close": close})

    result = command._classification_windows(
        frame=frame,
        timestamps=timestamps,
        pair="BTC/USDT:USDT",
        file_sha256="a" * 64,
    )

    assert set(result) == set(command._WINDOW_SPECS)
    assert result["primary_bear"]["actual_regime"] == "bear"
    assert result["wf_bull"]["actual_regime"] == "bull"
    assert result["wf_range"]["actual_regime"] == "range"
    assert result["wf_bear"]["actual_regime"] == "bear"
    assert result["oos"]["actual_regime"] == "range"
    assert result["wf_bull"]["market_data_digest"] == "a" * 64
    assert result["wf_bull"]["row_count"] == 14592


def test_rescan_rehashes_feather_and_computes_ohlcv_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "BTC_USDT_USDT-5m-futures.feather"
    timestamps = pd.date_range(
        "2026-01-01T00:00:00Z", periods=4, freq="5min"
    )
    frame = pd.DataFrame(
        {
            "date": timestamps,
            "open": [100.0, 101.0, 102.0, 103.0],
            "high": [102.0, 103.0, 104.0, 105.0],
            "low": [99.0, 100.0, 101.0, 102.0],
            "close": [101.0, 102.0, 103.0, 104.0],
            "volume": [10.0, 11.0, 12.0, 13.0],
        }
    )
    frame.to_feather(path)
    monkeypatch.setattr(command, "_classification_windows", lambda **_: {"checked": {}})
    evidence = {
        "sha256": command.evidence_sha256_file(path),
        "size_bytes": path.stat().st_size,
        "format": "feather",
        "row_count": 4,
        "expected_interval_seconds": 300,
        "first_open_at": "2026-01-01T00:00:00Z",
        "last_open_at": "2026-01-01T00:15:00Z",
        "last_close_at": "2026-01-01T00:20:00Z",
        "invalid_timestamp_count": 0,
        "duplicate_timestamp_count": 0,
        "out_of_order_count": 0,
        "missing_interval_count": 0,
        "misaligned_interval_count": 0,
        "null_ohlcv_row_count": 0,
    }

    metrics, windows = command._rescan_market_artifact(
        path=path,
        target={"pair": "BTC/USDT:USDT", "timeframe": "5m"},
        evidence=evidence,
    )

    assert metrics == {
        "gap_count": 0,
        "duplicate_count": 0,
        "null_count": 0,
        "out_of_order_count": 0,
        "misaligned_timestamp_count": 0,
        "invalid_ohlc_count": 0,
        "negative_volume_count": 0,
    }
    assert windows == {"checked": {}}

    frame.loc[0, "volume"] = -1.0
    frame.to_feather(path)
    with pytest.raises(command.Task1CommandBlocked):
        command._rescan_market_artifact(
            path=path,
            target={"pair": "BTC/USDT:USDT", "timeframe": "5m"},
            evidence=evidence,
        )


def test_source_sidecar_rejects_credential_use(tmp_path: Path) -> None:
    relative = "okx/futures/BTC_USDT_USDT-5m-futures.feather.source.json"
    sidecar = tmp_path / relative
    sidecar.parent.mkdir(parents=True)
    payload = {
        "schema_version": command.SOURCE_RECEIPT_CONTRACT,
        "endpoint": command.PUBLIC_ENDPOINT,
        "instrument_id": "BTC-USDT-SWAP",
        "timeframe": "5m",
        "source_type": "OKX_PUBLIC_REST",
        "data_file_sha256": "a" * 64,
        "response_chain_sha256": "b" * 64,
        "downloaded_at": "2026-08-12T13:27:18Z",
        "credentials_used": True,
        "account_endpoint_used": False,
        "orders_submitted": False,
    }
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    source = {
        "canonical_relative_path": relative,
        "observed_absolute_path": str(sidecar),
        "sha256": command.evidence_sha256_file(sidecar),
    }
    file_evidence = {
        "sha256": "a" * 64,
        "file_identity": {"timeframe": "5m"},
    }

    with pytest.raises(command.Task1CommandBlocked, match="safety/content"):
        command._verify_source_sidecar(
            source=source,
            file_evidence=file_evidence,
            root=tmp_path,
            expected_parent_digest=None,
        )


def test_loader_requires_expected_evidence_file_digest(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(command.Task1CommandBlocked, match="--evidence-file-sha256"):
        command.load_market_evidence(path, expected_file_sha256="0" * 64)


def test_cli_requires_evidence_file_sha256() -> None:
    with pytest.raises(SystemExit):
        command._parser().parse_args(
            [
                "--database-url",
                "postgresql://example",
                "--evidence-file",
                "evidence.json",
                "--report-file",
                "report.json",
                "--report-identity",
                "reports/migrations/report.json",
                "--request-id",
                "request",
                "--actor",
                "tester",
            ]
        )


@pytest.mark.parametrize(
    "identity",
    (
        "/tmp/task1.json",
        "../reports/migrations/task1.json",
        "reports/task1.json",
        "reports/migrations/task1.txt",
        r"reports\migrations\task1.json",
    ),
)
def test_report_identity_rejects_local_or_noncanonical_paths(identity: str) -> None:
    with pytest.raises(command.Task1CommandBlocked, match="report-identity"):
        command._validated_report_identity(identity)


def test_command_reports_are_private_atomic_and_repeat_does_not_overwrite_first(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "task1-first.json"
    command._preflight_atomic_report_path(first_path)

    first_artifact, first_digest, first_identity = command._persist_command_report(
        first_path,
        "reports/migrations/task1-first.json",
        {"contract": "command-report", "repeat_noop": False},
        repeat_noop=False,
    )
    first_bytes = first_artifact.read_bytes()
    assert first_artifact == first_path
    assert stat.S_IMODE(first_artifact.stat().st_mode) == 0o600
    assert (tmp_path / "task1-first.json.sha256").read_text().startswith(
        first_digest
    )
    assert first_identity == "reports/migrations/task1-first.json"
    assert str(tmp_path) not in first_artifact.read_text(encoding="utf-8")

    repeat_artifact, repeat_digest, repeat_identity = command._persist_command_report(
        first_path,
        "reports/migrations/task1-first.json",
        {"contract": "command-report", "repeat_noop": True},
        repeat_noop=True,
    )

    assert repeat_artifact == tmp_path / "task1-first.repeat.json"
    assert repeat_artifact != first_artifact
    assert first_artifact.read_bytes() == first_bytes
    assert stat.S_IMODE(repeat_artifact.stat().st_mode) == 0o600
    assert (tmp_path / "task1-first.repeat.json.sha256").read_text().startswith(
        repeat_digest
    )
    assert repeat_identity == "reports/migrations/task1-first.repeat.json"
    assert str(tmp_path) not in repeat_artifact.read_text(encoding="utf-8")


@pytest.mark.skipif(
    not _real_evidence_dependencies_available(),
    reason="canonical local market-data evidence is unavailable",
)
def test_checked_in_real_evidence_is_rescanned_with_unknown_current_freshness() -> None:
    status, records, manifest = command.load_market_evidence(
        _REAL_EVIDENCE,
        expected_file_sha256=(
            "4d83e33a8fcecaa8e5a27c919b4e633127c98cefa38c33fc9e4f8668e7eefb76"
        ),
    )

    assert status == "PASSED"
    assert len(records) == 6
    assert {record.freshness_status for record in records} == {"UNKNOWN"}
    assert {record.receipt_id for record in records} == {None}
    assert {record.quality_decision for record in records} == {
        "NOT_STRATEGY_QUALIFICATION"
    }
    assert {
        record.observed_at.isoformat() for record in records
    } == {"2026-08-13T04:44:25.773920+00:00"}
    assert all(set(record.classification_windows or {}) == set(command._WINDOW_SPECS) for record in records)
    assert manifest["artifact_digest"] == (
        "16d283c86a8e724c7f5a44c019e4e3643847d9f139ed8d03af6a32c45af1c5f8"
    )
    assert manifest["artifact_generation_delay_seconds"] == 55027
    assert manifest["legacy_aggregate_receipt"]["status"] == "BLOCKED"
    assert manifest["corrected_matrix"]["status"] == "PASSED"
    assert len(manifest["files"]) == 6
