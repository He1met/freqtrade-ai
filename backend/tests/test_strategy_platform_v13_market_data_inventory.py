from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "strategy_platform_v13_market_data_inventory.py"
)
SPEC = spec_from_file_location("strategy_platform_v13_market_data_inventory", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


ASSETS = ("BTC", "ETH", "SOL")
OBSERVED_AT = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
DOWNLOADED_AT = "2026-01-01T00:31:00Z"


def _candles(minutes: list[int]) -> list[dict[str, object]]:
    return [
        {
            "date": f"2026-01-01T00:{minute:02d}:00Z",
            "open": 100 + index,
            "high": 102 + index,
            "low": 99 + index,
            "close": 101 + index,
            "volume": 10 + index,
        }
        for index, minute in enumerate(minutes)
    ]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        MODULE.canonical_json({"invalid": float("nan")})


def _build_evidence_tree(
    base: Path,
    *,
    receipt_path_mode: str = "canonical",
    receipt_contract: bool = True,
) -> tuple[Path, Path, dict[str, object], Path]:
    repo = base / "repo"
    data_root = repo / "user_data" / "data"
    futures = data_root / "okx" / "futures"
    reports = repo / "reports" / "research"
    futures.mkdir(parents=True)
    reports.mkdir(parents=True)
    targets: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    for asset in ASSETS:
        instrument = f"{asset}-USDT-SWAP"
        pair = f"{asset}/USDT:USDT"
        stem = f"{asset}_USDT_USDT"
        response_chain = hashlib.sha256(asset.encode()).hexdigest()
        paths: dict[str, Path] = {}
        digests: dict[str, str] = {}
        row_counts: dict[str, int] = {}
        for timeframe, minutes in (("5m", [0, 5, 10, 15, 20, 25]), ("15m", [0, 15])):
            path = futures / f"{stem}-{timeframe}-futures.json"
            _write_json(path, _candles(minutes))
            digest = MODULE.sha256_file(path)
            paths[timeframe] = path
            digests[timeframe] = digest
            row_counts[timeframe] = len(minutes)
            sidecar = {
                "schema_version": "okx-public-candle-file-source-v1",
                "endpoint": MODULE._PUBLIC_ENDPOINT,
                "instrument_id": instrument,
                "credentials_used": False,
                "account_endpoint_used": False,
                "orders_submitted": False,
                "response_chain_sha256": response_chain,
                "downloaded_at": DOWNLOADED_AT,
                "source_type": (
                    "OKX_PUBLIC_REST" if timeframe == "5m" else "DERIVED_FROM_OKX_PUBLIC_REST"
                ),
                "timeframe": timeframe,
                "data_file_sha256": digest,
            }
            if timeframe == "15m":
                sidecar["parent_five_minute_sha256"] = digests["5m"]
                sidecar["derivation"] = "UTC epoch-aligned 3x5m OHLCV aggregation"
            _write_json(path.with_suffix(path.suffix + ".source.json"), sidecar)
            locator: str
            if receipt_path_mode == "absolute":
                locator = str(path)
            elif receipt_path_mode == "repo-relative":
                locator = path.relative_to(repo).as_posix()
            else:
                locator = path.relative_to(data_root).as_posix()
            targets.append(
                {
                    "exchange": "okx",
                    "market_type": "futures",
                    "pair": pair,
                    "instrument_id": instrument,
                    "timeframe": timeframe,
                    "data_kind": "futures",
                    "path": path.relative_to(data_root).as_posix(),
                    "max_close_lag_seconds": 3600,
                }
            )
            paths[f"receipt-{timeframe}"] = Path(locator)
        sources.append(
            {
                "endpoint": MODULE._PUBLIC_ENDPOINT,
                "instrument_id": instrument,
                "pair": pair,
                "bar": "5m",
                "closed_candles_only": True,
                "response_chain_sha256": response_chain,
                "first_open_at": "2026-01-01T00:00:00Z",
                "last_open_at": "2026-01-01T00:25:00Z",
                "row_count": row_counts["5m"],
                "five_minute_path": str(paths["receipt-5m"]),
                "five_minute_sha256": digests["5m"],
                "fifteen_minute_path": str(paths["receipt-15m"]),
                "fifteen_minute_sha256": digests["15m"],
                "fifteen_minute_row_count": row_counts["15m"],
                "fifteen_minute_derivation": "UTC epoch-aligned 3x5m OHLCV aggregation",
            }
        )
    manifest: dict[str, object] = {
        "schema_version": MODULE.TARGET_MANIFEST_SCHEMA_VERSION,
        "targets": targets,
    }
    receipt: dict[str, object] = {
        "schema_version": "okx-public-candle-source-receipt-v1",
        "downloaded_at": DOWNLOADED_AT,
        "execution_scope": "PUBLIC_MARKET_DATA_ONLY",
        "credentials_used": False,
        "account_endpoint_used": False,
        "orders_submitted": False,
        "sources": sources,
    }
    if receipt_contract:
        receipt.update(
            {
                "path_contract_version": MODULE.PATH_CONTRACT_VERSION,
                "canonical_data_root_key": MODULE.CANONICAL_DATA_ROOT_KEY,
            }
        )
    receipt_path = reports / "aggregate.json"
    _write_json(receipt_path, receipt)
    return repo, data_root, manifest, receipt_path


def _snapshot(
    repo: Path,
    data_root: Path,
    manifest: dict[str, object],
    receipt_path: Path,
) -> dict[str, object]:
    return MODULE.collect_snapshot(
        canonical_data_root=data_root,
        repository_root=repo,
        target_manifest=manifest,
        aggregate_receipt=receipt_path,
        observed_at=OBSERVED_AT,
    )


def test_resolves_absolute_and_relative_to_one_canonical_identity(tmp_path) -> None:
    repo, data_root, manifest, _ = _build_evidence_tree(tmp_path)
    target = manifest["targets"][0]
    assert isinstance(target, dict)
    relative = MODULE.resolve_market_data_path(
        target["path"], canonical_data_root=data_root, repository_root=repo
    )
    absolute = MODULE.resolve_market_data_path(
        data_root / str(target["path"]), canonical_data_root=data_root, repository_root=repo
    )
    legacy = MODULE.resolve_market_data_path(
        (data_root / str(target["path"])).relative_to(repo),
        canonical_data_root=data_root,
        repository_root=repo,
    )

    assert relative.status == absolute.status == legacy.status == "PASSED"
    assert relative.canonical_relative_path == absolute.canonical_relative_path
    assert relative.canonical_relative_path == legacy.canonical_relative_path
    assert relative.canonical_relative_path == target["path"]


@pytest.mark.parametrize("raw", ["../outside.json", "okx/../../outside.json"])
def test_blocks_parent_traversal(tmp_path, raw: str) -> None:
    _, data_root, _, _ = _build_evidence_tree(tmp_path)

    result = MODULE.resolve_market_data_path(raw, canonical_data_root=data_root)

    assert result.status == "BLOCKED"
    assert result.reason_codes == ("PATH_TRAVERSAL_NOT_ALLOWED",)


def test_blocks_outside_root_prefix_lookalike_and_symlink_alias(tmp_path) -> None:
    _, data_root, _, _ = _build_evidence_tree(tmp_path)
    outside = tmp_path / "repo" / "user_data" / "database" / "candle.json"
    _write_json(outside, _candles([0]))
    outside_result = MODULE.resolve_market_data_path(
        outside, canonical_data_root=data_root
    )
    assert outside_result.status == "BLOCKED"
    assert "PATH_OUTSIDE_CANONICAL_ROOT" in outside_result.reason_codes

    real = data_root / "okx" / "futures" / "BTC_USDT_USDT-5m-futures.json"
    alias = data_root / "okx" / "futures" / "alias.json"
    alias.symlink_to(real)
    alias_result = MODULE.resolve_market_data_path(alias, canonical_data_root=data_root)
    assert alias_result.status == "BLOCKED"
    assert alias_result.reason_codes == ("SYMLINK_ALIAS_NOT_ALLOWED",)


def test_complete_canonical_six_target_matrix_passes(tmp_path) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(tmp_path)

    report = _snapshot(repo, data_root, manifest, receipt_path)

    assert report["summary"]["status"] == "PASSED"
    assert report["summary"]["file_scan_status"] == "PASSED"
    assert report["summary"]["source_matrix_status"] == "PASSED"
    assert report["summary"]["file_status_counts"] == {
        "PASSED": 6,
        "BLOCKED": 0,
        "UNKNOWN": 0,
    }
    assert all(item["file_evidence"]["last_close_at"] == "2026-01-01T00:30:00Z" for item in report["files"])
    assert all(item["file_evidence"]["close_lag_seconds"] == 1800 for item in report["files"])
    assert all(item["file_evidence"]["observed_absolute_path"] for item in report["files"])
    assert all(item["file_evidence"]["canonical_relative_path"].startswith("okx/futures/") for item in report["files"])


def test_legacy_absolute_receipt_stays_blocked_while_files_pass(tmp_path) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(
        tmp_path,
        receipt_path_mode="absolute",
        receipt_contract=False,
    )

    report = _snapshot(repo, data_root, manifest, receipt_path)

    assert report["schema_version"] == MODULE.REPORT_SCHEMA_VERSION
    assert report["status"] == "BLOCKED"
    assert report["reason_codes"] == report["summary"]["reason_codes"]
    assert report["summary"]["file_scan_status"] == "PASSED"
    assert report["summary"]["file_status_counts"]["PASSED"] == 6
    assert report["summary"]["source_matrix_status"] == "BLOCKED"
    assert report["summary"]["status"] == "BLOCKED"
    assert "AGGREGATE_PATH_CONTRACT_MISSING_OR_MISMATCHED" in report["summary"]["reason_codes"]
    assert "AGGREGATE_SOURCE_PATH_NOT_CANONICAL_RELATIVE" in report["summary"]["reason_codes"]
    # The locator is resolved for audit, but that safe resolution does not
    # silently satisfy the persistent receipt contract.
    bindings = [item["aggregate_binding"] for item in report["files"]]
    assert all(item["receipt_path_resolution"]["status"] == "PASSED" for item in bindings)
    assert all(item["status"] == "BLOCKED" for item in bindings)
    assert all(
        "AGGREGATE_PATH_CONTRACT_MISSING_OR_MISMATCHED" in item["reason_codes"]
        for item in bindings
    )


@pytest.mark.parametrize(
    "path_alias",
    (
        "./okx/futures/BTC_USDT_USDT-5m-futures.json",
        "okx/./futures/BTC_USDT_USDT-5m-futures.json",
        "okx//futures/BTC_USDT_USDT-5m-futures.json",
    ),
)
def test_receipt_path_alias_is_not_canonical(tmp_path, path_alias: str) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(tmp_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["sources"][0]["five_minute_path"] = path_alias
    _write_json(receipt_path, receipt)

    report = _snapshot(repo, data_root, manifest, receipt_path)
    binding = next(
        item["aggregate_binding"]
        for item in report["files"]
        if item["target"]["pair"] == "BTC/USDT:USDT"
        and item["target"]["timeframe"] == "5m"
    )

    assert binding["receipt_path_resolution"]["status"] == "PASSED"
    assert binding["status"] == "BLOCKED"
    assert "AGGREGATE_SOURCE_PATH_NOT_CANONICAL_RELATIVE" in binding["reason_codes"]


@pytest.mark.parametrize("minutes", ([0, 4], [0, 9], [1, 6]))
def test_misaligned_candle_cadence_is_blocked(tmp_path, minutes: list[int]) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(tmp_path)
    path = data_root / "okx/futures/BTC_USDT_USDT-5m-futures.json"
    _write_json(path, _candles(minutes))

    report = _snapshot(repo, data_root, manifest, receipt_path)
    evidence = next(
        item["file_evidence"]
        for item in report["files"]
        if item["target"]["pair"] == "BTC/USDT:USDT"
        and item["target"]["timeframe"] == "5m"
    )

    assert evidence["status"] == "BLOCKED"
    assert evidence["misaligned_interval_count"] > 0
    assert "MISALIGNED_INTERVAL_PRESENT" in evidence["reason_codes"]


def test_repo_relative_receipt_is_not_misrepresented_as_data_root_relative(tmp_path) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(
        tmp_path,
        receipt_path_mode="repo-relative",
        receipt_contract=True,
    )

    report = _snapshot(repo, data_root, manifest, receipt_path)

    assert report["summary"]["status"] == "BLOCKED"
    assert "AGGREGATE_SOURCE_PATH_NOT_CANONICAL_RELATIVE" in report["summary"]["reason_codes"]
    assert all(
        item["aggregate_binding"]["receipt_path_resolution"]["canonical_relative_path"]
        == item["file_evidence"]["canonical_relative_path"]
        for item in report["files"]
    )


def test_missing_aggregate_receipt_is_unknown_not_zero_or_pass(tmp_path) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(tmp_path)
    receipt_path.unlink()

    report = _snapshot(repo, data_root, manifest, receipt_path)

    assert report["summary"]["file_scan_status"] == "PASSED"
    assert report["summary"]["source_matrix_status"] == "UNKNOWN"
    assert report["summary"]["status"] == "UNKNOWN"
    assert report["summary"]["unknown_values_are_not_zero"] is True
    assert report["aggregate_receipt"]["sha256"] is None
    assert report["summary"]["matrix_status_counts"]["UNKNOWN"] == 6


def test_known_digest_conflict_is_blocked(tmp_path) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(tmp_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["sources"][0]["five_minute_sha256"] = "0" * 64
    _write_json(receipt_path, receipt)

    report = _snapshot(repo, data_root, manifest, receipt_path)

    assert report["summary"]["status"] == "BLOCKED"
    assert "AGGREGATE_SOURCE_DIGEST_MISMATCH" in report["summary"]["reason_codes"]


def test_file_identity_and_snapshot_digest_do_not_depend_on_absolute_root(tmp_path) -> None:
    repo_a, data_a, manifest_a, receipt_a = _build_evidence_tree(tmp_path / "a")
    repo_b, data_b, manifest_b, receipt_b = _build_evidence_tree(tmp_path / "b")

    before = _snapshot(repo_a, data_a, manifest_a, receipt_a)
    after = _snapshot(repo_b, data_b, manifest_b, receipt_b)
    comparison = MODULE.compare_snapshots(before, after)

    assert before["snapshot_digest"] == after["snapshot_digest"]
    assert comparison["summary"]["status"] == "PASSED"
    assert [item["file_evidence"]["file_identity_digest"] for item in before["files"]] == [
        item["file_evidence"]["file_identity_digest"] for item in after["files"]
    ]
    assert before["path_contract"]["canonical_data_root_observed"] != after["path_contract"]["canonical_data_root_observed"]


def test_absolute_manifest_locators_normalize_across_different_roots(tmp_path) -> None:
    repo_a, data_a, manifest_a, receipt_a = _build_evidence_tree(tmp_path / "a")
    repo_b, data_b, manifest_b, receipt_b = _build_evidence_tree(tmp_path / "b")
    for target in manifest_a["targets"]:
        target["path"] = str(data_a / target["path"])
    for target in manifest_b["targets"]:
        target["path"] = str(data_b / target["path"])

    before = _snapshot(repo_a, data_a, manifest_a, receipt_a)
    after = _snapshot(repo_b, data_b, manifest_b, receipt_b)
    comparison = MODULE.compare_snapshots(before, after)

    assert before["expected_target_manifest"]["input_digest"] != after["expected_target_manifest"]["input_digest"]
    assert before["expected_target_manifest"]["digest"] == after["expected_target_manifest"]["digest"]
    assert before["snapshot_digest"] == after["snapshot_digest"]
    assert comparison["summary"]["status"] == "PASSED"


def test_reconciliation_blocks_content_or_coverage_change(tmp_path) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(tmp_path)
    before = _snapshot(repo, data_root, manifest, receipt_path)
    after = deepcopy(before)
    after["files"][0]["file_evidence"]["sha256"] = "f" * 64
    after["files"][0]["file_evidence"]["row_count"] += 1

    comparison = MODULE.compare_snapshots(before, after)

    assert comparison["schema_version"] == MODULE.COMPARISON_SCHEMA_VERSION
    assert comparison["status"] == "BLOCKED"
    assert comparison["reason_codes"] == comparison["summary"]["reason_codes"]
    assert comparison["summary"]["status"] == "BLOCKED"
    reasons = comparison["comparisons"][0]["reason_codes"]
    assert "RECONCILIATION_SHA256_MISMATCH" in reasons
    assert "RECONCILIATION_ROW_COUNT_MISMATCH" in reasons


def test_reconciliation_blocks_manifest_or_aggregate_receipt_drift(tmp_path) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(tmp_path)
    before = _snapshot(repo, data_root, manifest, receipt_path)
    after = deepcopy(before)
    after["expected_target_manifest"]["digest"] = "1" * 64
    after["aggregate_receipt"]["sha256"] = "2" * 64

    comparison = MODULE.compare_snapshots(before, after)

    assert comparison["summary"]["status"] == "BLOCKED"
    assert "EXPECTED_TARGET_MANIFEST_CHANGED" in comparison["summary"]["reason_codes"]
    assert "AGGREGATE_RECEIPT_SHA256_MISMATCH" in comparison["summary"]["reason_codes"]
    assert comparison["aggregate_receipt_comparison"]["status"] == "BLOCKED"
    assert comparison["aggregate_receipt_comparison"]["differences"]["sha256"] == {
        "before": before["aggregate_receipt"]["sha256"],
        "after": "2" * 64,
    }


def test_reconciliation_validates_snapshot_and_identity_digests(tmp_path) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(tmp_path)
    before = _snapshot(repo, data_root, manifest, receipt_path)
    after = deepcopy(before)
    before["snapshot_digest"] = "0" * 64
    after["files"][0]["file_evidence"]["file_identity"]["pair"] = "TAMPERED"

    comparison = MODULE.compare_snapshots(before, after)

    assert comparison["status"] == "BLOCKED"
    assert "BEFORE_SNAPSHOT_DIGEST_MISMATCH" in comparison["reason_codes"]
    assert "AFTER_FILE_IDENTITY_DIGEST_MISMATCH" in comparison["reason_codes"]
    assert "AFTER_REPORT_DIGEST_MISMATCH" in comparison["reason_codes"]


def test_reconciliation_compares_per_file_aggregate_binding(tmp_path) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(tmp_path)
    before = _snapshot(repo, data_root, manifest, receipt_path)
    after = deepcopy(before)
    after["files"][0]["aggregate_binding"]["receipt_row_count"] += 1

    comparison = MODULE.compare_snapshots(before, after)

    assert comparison["status"] == "BLOCKED"
    reasons = comparison["comparisons"][0]["reason_codes"]
    assert "RECONCILIATION_AGGREGATE_BINDING_RECEIPT_ROW_COUNT_MISMATCH" in reasons


def test_migration_evidence_audits_path_only_correction(tmp_path) -> None:
    repo, data_root, manifest, original_path = _build_evidence_tree(
        tmp_path,
        receipt_path_mode="absolute",
        receipt_contract=False,
    )
    original = json.loads(original_path.read_text(encoding="utf-8"))
    corrected, transformations, findings = MODULE.build_corrected_aggregate_receipt(
        original,
        canonical_data_root=data_root,
        repository_root=repo,
    )
    corrected_path = repo / "reports/migrations/corrected-aggregate.json"
    _write_json(corrected_path, corrected)

    report = MODULE.build_migration_market_evidence(
        canonical_data_root=data_root,
        repository_root=repo,
        target_manifest=manifest,
        original_aggregate_receipt=original_path,
        corrected_aggregate_receipt=corrected_path,
        observed_at=datetime.fromisoformat(DOWNLOADED_AT.replace("Z", "+00:00")),
        generated_at=OBSERVED_AT,
    )

    assert findings.status == "PASSED"
    assert len(transformations) == 6
    assert report["schema_version"] == MODULE.MIGRATION_EVIDENCE_SCHEMA_VERSION
    assert report["status"] == "PASSED"
    assert report["source_legacy_classification"]["status"] == "BLOCKED"
    assert report["correction_contract"]["status"] == "PASSED"
    assert report["corrected_matrix"]["status"] == "PASSED"
    assert report["full_scan_classification"]["file_status_counts"]["PASSED"] == 6
    digest_payload = dict(report)
    digest = digest_payload.pop("artifact_digest")
    assert digest == MODULE.canonical_sha256(digest_payload)


def test_migration_evidence_blocks_non_path_correction(tmp_path) -> None:
    repo, data_root, manifest, original_path = _build_evidence_tree(
        tmp_path,
        receipt_path_mode="absolute",
        receipt_contract=False,
    )
    original = json.loads(original_path.read_text(encoding="utf-8"))
    corrected, _, _ = MODULE.build_corrected_aggregate_receipt(
        original,
        canonical_data_root=data_root,
        repository_root=repo,
    )
    corrected["sources"][0]["five_minute_sha256"] = "0" * 64
    corrected_path = repo / "reports/migrations/corrected-aggregate.json"
    _write_json(corrected_path, corrected)

    report = MODULE.build_migration_market_evidence(
        canonical_data_root=data_root,
        repository_root=repo,
        target_manifest=manifest,
        original_aggregate_receipt=original_path,
        corrected_aggregate_receipt=corrected_path,
        observed_at=datetime.fromisoformat(DOWNLOADED_AT.replace("Z", "+00:00")),
        generated_at=OBSERVED_AT,
    )

    assert report["status"] == "BLOCKED"
    assert "CORRECTED_AGGREGATE_NOT_PATH_ONLY_TRANSFORMATION" in report["reason_codes"]
    assert report["corrected_matrix"]["status"] == "BLOCKED"


def test_migration_evidence_rejects_freshness_basis_override(tmp_path) -> None:
    repo, data_root, manifest, original_path = _build_evidence_tree(
        tmp_path,
        receipt_path_mode="absolute",
        receipt_contract=False,
    )
    original = json.loads(original_path.read_text(encoding="utf-8"))
    corrected, _, _ = MODULE.build_corrected_aggregate_receipt(
        original,
        canonical_data_root=data_root,
        repository_root=repo,
    )
    corrected_path = repo / "reports/migrations/corrected-aggregate.json"
    _write_json(corrected_path, corrected)

    report = MODULE.build_migration_market_evidence(
        canonical_data_root=data_root,
        repository_root=repo,
        target_manifest=manifest,
        original_aggregate_receipt=original_path,
        corrected_aggregate_receipt=corrected_path,
        observed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        generated_at=OBSERVED_AT,
    )

    assert report["status"] == "BLOCKED"
    assert (
        "FRESHNESS_BASIS_NOT_SOURCE_RECEIPT_DOWNLOADED_AT"
        in report["reason_codes"]
    )


def test_cli_emits_one_json_object_and_nonzero_for_unknown(tmp_path, capsys) -> None:
    repo, data_root, manifest, receipt_path = _build_evidence_tree(tmp_path)
    manifest_path = repo / "expected-targets.json"
    _write_json(manifest_path, manifest)
    receipt_path.unlink()

    exit_code = MODULE.main(
        [
            "--compact",
            "snapshot",
            "--canonical-data-root",
            str(data_root),
            "--repository-root",
            str(repo),
            "--expected-targets",
            str(manifest_path),
            "--aggregate-receipt",
            str(receipt_path),
            "--observed-at",
            "2026-01-01T01:00:00Z",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["summary"]["status"] == "UNKNOWN"
    assert captured.out.endswith("\n")
