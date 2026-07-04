import json

from app.services.dry_run_status import DryRunStatusParser, DryRunStatusSnapshotService


def status_payload() -> dict:
    return {
        "status": "running",
        "profile_name": "phase5-local-dry-run",
        "strategy_version_id": 123,
        "strategy_name": "MvpRsiStrategy",
        "exchange": "okx",
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "dry_run": True,
        "balance": {
            "currency": "USDT",
            "total": 1000,
            "free": 900,
            "used": 100,
            "unrealized_profit": 1.25,
        },
        "open_trades": [
            {"pair": "BTC/USDT:USDT", "stake_amount": 100, "profit_abs": 1.25},
            {"pair": "ETH/USDT:USDT", "stake_amount": 50, "profit_abs": -0.5},
            {"pair": "SOL/USDT:USDT", "is_open": False, "profit_abs": 99},
        ],
        "events": [
            {
                "timestamp": "2026-07-04T15:10:00Z",
                "event_type": "bot_status",
                "severity": "info",
                "message": "dry-run started api_secret=real-secret",
                "source": "fixture",
                "details": {"api_key": "real-key", "note": "Bearer abc123"},
            }
        ],
        "last_updated": "2026-07-04T15:11:00Z",
    }


def test_parser_reads_controlled_status_json_and_redacts_secret_values(tmp_path) -> None:
    path = tmp_path / "status.json"
    path.write_text(json.dumps(status_payload()), encoding="utf-8")

    snapshot = DryRunStatusSnapshotService().snapshot_from_controlled_json(path)

    assert snapshot.status == "RUNNING"
    assert snapshot.profile_name == "phase5-local-dry-run"
    assert snapshot.strategy_version_id == 123
    assert snapshot.strategy_name == "MvpRsiStrategy"
    assert snapshot.exchange == "okx"
    assert snapshot.pair == "BTC/USDT:USDT"
    assert snapshot.timeframe == "15m"
    assert snapshot.dry_run is True
    assert snapshot.balance_summary.currency == "USDT"
    assert snapshot.balance_summary.total == 1000
    assert snapshot.open_trades_summary.total_open_trades == 2
    assert snapshot.open_trades_summary.pair_count == 2
    assert snapshot.open_trades_summary.total_profit_abs == 0.75
    assert snapshot.recent_events[0].severity == "INFO"
    assert snapshot.recent_events[0].message == "dry-run started api_secret=[REDACTED]"
    assert snapshot.recent_events[0].details["api_key"] == "[REDACTED]"
    assert snapshot.recent_events[0].details["note"] == "Bearer [REDACTED]"
    assert "real-secret" not in snapshot.model_dump_json()
    assert "real-key" not in snapshot.model_dump_json()


def test_parser_reads_fixture_status_json(tmp_path) -> None:
    path = tmp_path / "fixture-status.json"
    payload = status_payload()
    payload["status"] = "success"
    path.write_text(json.dumps(payload), encoding="utf-8")

    snapshot = DryRunStatusSnapshotService().snapshot_from_fixture_json(path)

    assert snapshot.status == "SUCCESS"
    assert snapshot.dry_run is True
    assert snapshot.last_updated.isoformat().startswith("2026-07-04T15:11:00")


def test_parser_reads_latest_snapshot_from_artifact_manifest(tmp_path) -> None:
    manifest_path = tmp_path / "dry-run-manifest.json"
    manifest = {
        "manifest_version": 1,
        "status": "SUCCESS",
        "profile_name": "phase5-local-dry-run",
        "strategy_version_id": 123,
        "strategy_name": "MvpRsiStrategy",
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "profile_snapshot": {
            "exchange": {"name": "okx", "trading_mode": "futures"},
            "safety": {"dry_run": True},
        },
        "status_snapshots": [
            {
                "status": "running",
                "balance_summary": {"currency": "USDT", "total": 100},
                "open_trades_summary": {"total_open_trades": 0, "pair_count": 0},
                "recent_events": [
                    {
                        "timestamp": "2026-07-04T15:00:00Z",
                        "event_type": "startup",
                        "severity": "info",
                        "message": "started",
                        "source": "artifact",
                    }
                ],
            },
            {
                "status": "running",
                "balance_summary": {"currency": "USDT", "total": 125},
                "open_trades": [{"pair": "BTC/USDT:USDT", "profit_abs": 2.5}],
                "events": [
                    {
                        "timestamp": "2026-07-04T15:05:00Z",
                        "event_type": "trade_open",
                        "severity": "warning",
                        "message": "trade opened",
                        "source": "artifact",
                    }
                ],
            },
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    snapshot = DryRunStatusSnapshotService().snapshot_from_artifact_manifest(manifest_path)

    assert snapshot.status == "RUNNING"
    assert snapshot.exchange == "okx"
    assert snapshot.dry_run is True
    assert snapshot.balance_summary.total == 125
    assert snapshot.open_trades_summary.total_open_trades == 1
    assert snapshot.recent_events[0].event_type == "trade_open"
    assert snapshot.artifact_manifest_path == str(manifest_path)


def test_manifest_without_status_snapshots_returns_explicit_empty_state(tmp_path) -> None:
    manifest_path = tmp_path / "dry-run-empty-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "status": "SUCCESS",
                "profile_name": "phase5-local-dry-run",
                "strategy_version_id": 123,
                "strategy_name": "MvpRsiStrategy",
                "pair": "BTC/USDT:USDT",
                "timeframe": "15m",
                "profile_snapshot": {
                    "exchange": {"name": "okx"},
                    "safety": {"dry_run": True},
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = DryRunStatusSnapshotService().snapshot_from_artifact_manifest(manifest_path)

    assert snapshot.status == "SKIPPED"
    assert snapshot.skipped_reason == "dry-run artifact manifest does not contain status_snapshots"
    assert snapshot.open_trades_summary.total_open_trades == 0
    assert snapshot.recent_events[0].event_type == "status_snapshots_empty"
    assert snapshot.artifact_manifest_path == str(manifest_path)


def test_missing_status_file_returns_blocked_snapshot(tmp_path) -> None:
    missing_path = tmp_path / "missing-status.json"

    snapshot = DryRunStatusSnapshotService().snapshot_from_controlled_json(missing_path)

    assert snapshot.status == "BLOCKED"
    assert "does not exist" in (snapshot.blocked_reason or "")
    assert snapshot.failed_reason is None


def test_corrupt_status_file_returns_failed_snapshot(tmp_path) -> None:
    path = tmp_path / "broken-status.json"
    path.write_text("{not-json", encoding="utf-8")

    snapshot = DryRunStatusSnapshotService().snapshot_from_controlled_json(path)

    assert snapshot.status == "FAILED"
    assert snapshot.failed_reason == "dry-run status JSON is not valid JSON"
    assert snapshot.blocked_reason is None


def test_dry_run_false_is_failed_closed() -> None:
    payload = status_payload()
    payload["dry_run"] = False

    snapshot = DryRunStatusParser().parse_controlled_json_payload(payload)

    assert snapshot.status == "FAILED"
    assert snapshot.failed_reason == "dry-run status payload reported dry_run=false"
    assert snapshot.dry_run is None
