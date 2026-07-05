import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.services.runtime_contract import RuntimeReadOnlyContractService


FIXED_NOW = datetime(2026, 7, 5, 13, 0, tzinfo=timezone.utc)


def service() -> RuntimeReadOnlyContractService:
    return RuntimeReadOnlyContractService(now_provider=lambda: FIXED_NOW)


def dry_run_payload(status: str = "running") -> dict:
    return {
        "status": status,
        "profile_name": "phase7-runtime-contract",
        "strategy_version_id": 197,
        "strategy_name": "MvpRsiStrategy",
        "exchange": "okx",
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "dry_run": True,
        "balance": {"currency": "USDT", "total": 1000},
        "open_trades": [],
        "last_updated": "2026-07-05T12:59:00Z",
    }


def monitoring_payload(status: str = "ok") -> dict:
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
        "last_updated": "2026-07-05T12:58:00Z",
    }


def test_contract_reports_missing_runtime_sources_as_fallback_state(tmp_path) -> None:
    contract = service().build_contract(
        dry_run_status_path=tmp_path / "missing-dry-run-status.json",
        dry_run_manifest_path=tmp_path / "missing-dry-run-manifest.json",
        live_candidate_monitoring_path=tmp_path / "missing-monitoring.json",
        live_candidate_monitoring_manifest_path=tmp_path / "missing-monitoring-manifest.json",
        phase7_smoke_summary_path=tmp_path / "missing-phase7-smoke.json",
    )

    rendered = contract.model_dump_json()

    assert contract.schema_version == "1"
    assert contract.status == "BLOCKED"
    assert contract.runtime_readiness.status == "BLOCKED"
    assert contract.fallback_status.active is True
    assert "dry-run status JSON file does not exist" in contract.blocked_reasons[0]
    assert contract.smoke_status.status == "UNAVAILABLE"
    assert contract.safety.read_only is True
    assert contract.safety.allow_live_trading is False
    assert contract.safety.allow_real_orders is False
    assert "start_live" not in rendered
    assert "stop_live" not in rendered
    assert "deploy_command" not in rendered


def test_contract_combines_existing_read_only_runtime_fixtures(tmp_path) -> None:
    dry_run_status_path = tmp_path / "dry-run-status.json"
    monitoring_path = tmp_path / "live-candidate-monitoring.json"
    smoke_path = tmp_path / "phase7-smoke-summary.json"
    dry_run_status_path.write_text(json.dumps(dry_run_payload()), encoding="utf-8")
    monitoring_path.write_text(json.dumps(monitoring_payload()), encoding="utf-8")
    smoke_path.write_text(
        json.dumps(
            {
                "status": "PASS",
                "generated_at": "2026-07-05T12:59:30Z",
                "summary": "offline runtime contract fixture passed",
            }
        ),
        encoding="utf-8",
    )

    contract = service().build_contract(
        dry_run_status_path=dry_run_status_path,
        dry_run_manifest_path=tmp_path / "missing-dry-run-manifest.json",
        live_candidate_monitoring_path=monitoring_path,
        live_candidate_monitoring_manifest_path=tmp_path / "missing-monitoring-manifest.json",
        phase7_smoke_summary_path=smoke_path,
    )

    assert contract.status == "READY"
    assert contract.runtime_readiness.status == "READY"
    assert contract.fallback_status.active is False
    assert contract.dry_run_status.status == "RUNNING"
    assert contract.live_candidate_monitoring.status == "OK"
    assert contract.smoke_status.status == "READY"
    assert {link.name for link in contract.artifact_links} >= {
        "dry_run_status_json",
        "live_candidate_monitoring_json",
        "phase7_smoke_summary",
    }
    assert any(link.exists for link in contract.artifact_links)


def test_contract_redacts_or_blocks_secret_shaped_runtime_payloads(tmp_path) -> None:
    dry_run_status_path = tmp_path / "dry-run-status.json"
    monitoring_path = tmp_path / "live-candidate-monitoring.json"
    smoke_path = tmp_path / "phase7-smoke-summary.json"
    dry_payload = dry_run_payload()
    dry_payload["events"] = [
        {
            "timestamp": "2026-07-05T12:59:00Z",
            "event_type": "status",
            "severity": "info",
            "message": "status read api_secret=fixture-sensitive-value",
            "source": "fixture",
        }
    ]
    monitoring = monitoring_payload()
    monitoring["alerts"] = [
        {
            "alert_id": "unsafe-alert",
            "severity": "warning",
            "message": "api_secret=fixture-sensitive-value",
            "last_updated": "2026-07-05T12:59:00Z",
        }
    ]
    dry_run_status_path.write_text(json.dumps(dry_payload), encoding="utf-8")
    monitoring_path.write_text(json.dumps(monitoring), encoding="utf-8")
    smoke_path.write_text(json.dumps({"status": "PASS"}), encoding="utf-8")

    contract = service().build_contract(
        dry_run_status_path=dry_run_status_path,
        dry_run_manifest_path=tmp_path / "missing-dry-run-manifest.json",
        live_candidate_monitoring_path=monitoring_path,
        live_candidate_monitoring_manifest_path=tmp_path / "missing-monitoring-manifest.json",
        phase7_smoke_summary_path=smoke_path,
    )
    rendered = contract.model_dump_json()

    assert contract.status == "BLOCKED"
    assert "live-candidate monitoring sensitive input was rejected" in contract.blocked_reasons
    assert "fixture-sensitive-value" not in rendered
    assert "api_secret=[REDACTED]" in rendered


def test_runtime_read_only_endpoint_returns_stable_contract_shape() -> None:
    client = TestClient(app)

    response = client.get("/runtime/read-only")

    assert response.status_code == 200
    payload = response.json()
    rendered = json.dumps(payload, sort_keys=True)
    assert payload["schema_version"] == "1"
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["allow_live_trading"] is False
    assert payload["safety"]["allow_exchange_connection"] is False
    assert payload["system_status"]["name"] == "system_status"
    assert payload["runtime_readiness"]["name"] == "runtime_readiness"
    assert payload["smoke_status"]["name"] == "phase7_smoke"
    assert "artifact_links" in payload
    assert "start_live" not in rendered
    assert "stop_live" not in rendered
    assert "deploy_command" not in rendered
