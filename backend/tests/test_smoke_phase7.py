import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_phase7_smoke_runs_offline_without_frontend_build(tmp_path) -> None:
    smoke_dir = tmp_path / "phase7-smoke"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_phase7.py",
            "--offline",
            "--tmp-dir",
            str(smoke_dir),
            "--skip-frontend-build",
        ],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] Phase 7 smoke completed" in result.stdout
    assert "runtime_contract=READY" in result.stdout
    assert "operator_status=READY blocked_path=missing_env_and_smoke_summary:BLOCKED" in result.stdout
    assert "security_scan=PASS" in result.stdout
    assert "dashboard_fallback=PASS" in result.stdout

    summary_path = smoke_dir / "repo-fixture" / "reports" / "runtime" / "phase7-smoke-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "PASS"
    assert summary["success_path"] == {
        "dashboard_fallback": "PASS",
        "operator_status": "READY",
        "runtime_contract": "READY",
        "security_scan": "PASS",
    }
    assert summary["blocked_paths"]["missing_env_and_smoke_summary"] == "BLOCKED"
    assert summary["audit_event_ids"] == ["phase7-smoke-accepted"]
    assert summary["safety"]["production_ready"] is False
    assert summary["safety"]["exchange_connection"] is False
    assert summary["safety"]["market_data_download"] is False
    assert summary["safety"]["live_trading"] is False
    assert summary["safety"]["real_orders"] is False
    assert summary["safety"]["deployment_execution"] is False
