import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_phase6_smoke_runs_offline_and_records_fail_closed_paths(tmp_path) -> None:
    smoke_dir = tmp_path / "phase6-smoke"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_phase6.py",
            "--offline",
            "--tmp-dir",
            str(smoke_dir),
        ],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] Phase 6 smoke completed" in result.stdout
    assert "blocked_paths=missing_risk_evidence,missing_manual_approval,missing_rollback_plan" in result.stdout
    assert "failed_paths=risk_thresholds:FAILED" in result.stdout

    summary = json.loads((smoke_dir / "phase6-smoke-summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PASS"
    assert summary["success_path"] == {
        "preflight": "APPROVED_FOR_REVIEW",
        "approval": "APPROVED_FOR_DEPLOYMENT_RECORD",
        "deployment_planned": "PLANNED",
        "deployment_manual_result": "MANUAL_RESULT_RECORDED",
        "monitoring": "OK",
    }
    assert summary["blocked_paths"]["missing_risk_evidence"] == "BLOCKED"
    assert summary["blocked_paths"]["missing_manual_approval"] == "BLOCKED"
    assert summary["blocked_paths"]["missing_rollback_plan"] == "BLOCKED"
    assert summary["failed_paths"]["risk_thresholds"] == "FAILED"
    assert summary["safety"]["exchange_connection"] is False
    assert summary["safety"]["market_data_download"] is False
    assert summary["safety"]["live_trading"] is False
    assert summary["safety"]["real_orders"] is False
    assert summary["safety"]["production_deployment"] is False
