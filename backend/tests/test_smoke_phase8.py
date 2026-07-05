import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_phase8_smoke_reconciles_api_db_and_non_core_sources_offline(tmp_path) -> None:
    smoke_dir = tmp_path / "phase8-smoke"
    database_path = Path("/tmp") / f"freqtrade-ai-pytest-phase8-e2e-{uuid4().hex}.sqlite"
    database_url = f"sqlite+pysqlite:///{database_path}"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_phase8.py",
            "--offline",
            "--tmp-dir",
            str(smoke_dir),
            "--database-url",
            database_url,
            "--skip-frontend",
        ],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    database_path.unlink(missing_ok=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] Phase 8 E2E reconciliation completed" in result.stdout
    assert "db_reconciliation=PASS" in result.stdout
    assert "api_reconciliation=PASS" in result.stdout

    evidence_path = smoke_dir / "reports" / "phase8-e2e-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "PASS"
    assert evidence["checks"]["database_reconciliation"]["status"] == "PASS"
    assert evidence["checks"]["api_reconciliation"]["status"] == "PASS"
    assert evidence["checks"]["database_reconciliation"]["non_core_strategy_versions"] > 0
    assert evidence["checks"]["database_reconciliation"]["non_core_backtest_results"] > 0
    assert evidence["checks"]["api_reconciliation"]["http_statuses"]["/api/ranking"] == 200
    assert evidence["core_ids"]["strategy_version_id"] > 0
    assert evidence["core_ids"]["backtest_result_id"] > 0
    assert evidence["core_ids"]["strategy_score_id"] > 0
    assert evidence["safety"]["local_only"] is True
    assert evidence["safety"]["live_trading"] is False
    assert evidence["safety"]["real_orders"] is False
