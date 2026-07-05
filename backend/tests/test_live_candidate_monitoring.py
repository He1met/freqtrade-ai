import json
from datetime import datetime, timezone

from app.services.live_candidate_monitoring import (
    LiveCandidateMonitoringParser,
    LiveCandidateMonitoringSnapshotService,
)


FIXED_NOW = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)


def parser() -> LiveCandidateMonitoringParser:
    return LiveCandidateMonitoringParser(now_provider=lambda: FIXED_NOW)


def base_payload(status: str = "ok") -> dict:
    return {
        "status": status,
        "profile_name": "phase6-live-candidate-btc-15m",
        "profile_hash": "a" * 64,
        "deployment_record_id": "deployment-record-abc123",
        "deployment_status": "PLANNED",
        "approval_status": "APPROVED_FOR_DEPLOYMENT_RECORD",
        "preflight_status": "APPROVED_FOR_REVIEW",
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "source_ref": "reports/governance/live_candidate_monitoring.json",
        "last_updated": "2026-07-05T11:59:00Z",
    }


def test_controlled_json_status_and_alerts_are_read_only(tmp_path) -> None:
    payload = base_payload("warning")
    payload["warnings"] = ["manual review latency is elevated"]
    payload["alerts"] = [
        {
            "alert_id": "manual-review-latency",
            "severity": "warning",
            "message": "Manual approval review has exceeded the expected SLA.",
            "last_updated": "2026-07-05T11:58:00Z",
            "evidence_ref": "reports/governance/live_candidate_alerts.json",
            "details": {"queue": "manual-review"},
        }
    ]
    path = tmp_path / "live-candidate-monitoring.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    snapshot = LiveCandidateMonitoringSnapshotService(parser()).snapshot_from_controlled_json(path)
    summary = snapshot.to_readonly_summary()
    rendered = json.dumps(summary, sort_keys=True)

    assert snapshot.status == "WARNING"
    assert snapshot.source.source == "controlled-local-json"
    assert snapshot.source.ref == "reports/governance/live_candidate_monitoring.json"
    assert snapshot.last_updated.isoformat() == "2026-07-05T11:59:00+00:00"
    assert snapshot.alerts[0].status == "WARNING"
    assert snapshot.alerts[0].source.source == "controlled-local-json"
    assert "Read-only live-candidate governance summary" in snapshot.safety_boundary
    assert "start_live" not in rendered
    assert "stop_live" not in rendered
    assert "deploy_command" not in rendered


def test_fixture_and_artifact_manifest_sources_are_preserved(tmp_path) -> None:
    fixture_path = tmp_path / "fixture-monitoring.json"
    fixture_payload = base_payload("ok")
    fixture_payload["source_ref"] = "fixtures/phase6/live_candidate_monitoring.json"
    fixture_path.write_text(json.dumps(fixture_payload), encoding="utf-8")

    fixture_snapshot = LiveCandidateMonitoringSnapshotService(parser()).snapshot_from_fixture_json(
        fixture_path
    )

    assert fixture_snapshot.status == "OK"
    assert fixture_snapshot.source.source == "fixture"
    assert fixture_snapshot.source.ref == "fixtures/phase6/live_candidate_monitoring.json"

    manifest_path = tmp_path / "live-candidate-manifest.json"
    manifest_payload = base_payload("unavailable")
    manifest_payload["monitoring_snapshots"] = [
        {
            "status": "unavailable",
            "unavailable_reason": "no monitoring sample has been produced yet",
            "last_updated": "2026-07-05T11:50:00Z",
        },
        {
            "status": "ok",
            "last_updated": "2026-07-05T11:59:30Z",
        },
    ]
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    artifact_snapshot = LiveCandidateMonitoringSnapshotService(parser()).snapshot_from_artifact_manifest(
        manifest_path
    )

    assert artifact_snapshot.status == "OK"
    assert artifact_snapshot.source.source == "artifact"
    assert artifact_snapshot.source.ref == str(manifest_path)
    assert artifact_snapshot.last_updated.isoformat() == "2026-07-05T11:59:30+00:00"


def test_monitoring_dto_expresses_unavailable_blocked_stale_and_warning() -> None:
    unavailable_payload = base_payload("unavailable")
    unavailable_payload["unavailable_reason"] = "controlled JSON has not been generated"
    unavailable = parser().parse_fixture_payload(unavailable_payload)

    blocked_payload = base_payload("blocked")
    blocked_payload["blockers"] = ["manual approval record is missing"]
    blocked = parser().parse_fixture_payload(blocked_payload)

    stale_payload = base_payload("ok")
    stale_payload["last_updated"] = "2026-07-05T11:00:00Z"
    stale_payload["stale_after_seconds"] = 60
    stale = parser().parse_fixture_payload(stale_payload)

    warning_payload = base_payload("warning")
    warning_payload["warnings"] = ["alert summary contains non-blocking warnings"]
    warning = parser().parse_fixture_payload(warning_payload)

    assert unavailable.status == "UNAVAILABLE"
    assert unavailable.unavailable_reason == "controlled JSON has not been generated"
    assert blocked.status == "BLOCKED"
    assert blocked.blockers == ["manual approval record is missing"]
    assert stale.status == "STALE"
    assert stale.stale_reason == "monitoring data is older than 60 seconds"
    assert warning.status == "WARNING"
    assert warning.warnings == ["alert summary contains non-blocking warnings"]


def test_secret_shaped_payload_is_blocked_without_rendering_secret_value() -> None:
    payload = base_payload("ok")
    payload["alerts"] = [
        {
            "alert_id": "unsafe-alert",
            "severity": "warning",
            "message": "api_secret=fixture-sensitive-value",
            "last_updated": "2026-07-05T11:59:00Z",
        }
    ]

    snapshot = parser().parse_controlled_json_payload(payload)
    rendered = snapshot.model_dump_json()

    assert snapshot.status == "BLOCKED"
    assert snapshot.blockers == ["live-candidate monitoring sensitive input was rejected"]
    assert "fixture-sensitive-value" not in rendered


def test_control_action_fields_are_blocked() -> None:
    payload = base_payload("ok")
    payload["start"] = True

    snapshot = parser().parse_controlled_json_payload(payload)
    rendered = snapshot.model_dump_json()

    assert snapshot.status == "BLOCKED"
    assert snapshot.blockers == ["live-candidate monitoring control-shaped input was rejected"]
    assert "start_live" not in rendered
    assert "stop_live" not in rendered
