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
    assert evidence["checks"]["api_reconciliation"]["refresh_stable"] is True
    assert evidence["checks"]["api_reconciliation"]["db_api_source_contract_match"] is True
    assert (
        evidence["checks"]["api_reconciliation"]["core_snapshot_before_refresh"]
        == evidence["checks"]["api_reconciliation"]["core_snapshot_after_refresh"]
    )
    table_queries = evidence["checks"]["database_reconciliation"]["table_queries"]
    assert set(table_queries) == {
        "strategies",
        "strategy_generation_runs",
        "strategy_versions",
        "backtest_runs",
        "backtest_tasks",
        "backtest_results",
        "strategy_scores",
    }
    assert all(item["status"] == "PASS" for item in table_queries.values())
    assert all("WHERE id = :" in item["sql"] for item in table_queries.values())
    assert evidence["acceptance"]["status"] == "PASS"
    assert evidence["acceptance"]["acceptance_ready"] is True
    assert evidence["acceptance"]["next_action"]
    assert len(evidence["page_evidence_points"]) == 3
    assert evidence["core_ids"]["strategy_version_id"] > 0
    assert evidence["core_ids"]["backtest_result_id"] > 0
    assert evidence["core_ids"]["strategy_score_id"] > 0
    assert evidence["safety"]["local_only"] is True
    assert evidence["safety"]["live_trading"] is False
    assert evidence["safety"]["real_orders"] is False


def test_phase8_smoke_persists_blocked_evidence_for_unsafe_database_target(tmp_path) -> None:
    smoke_dir = tmp_path / "phase8-blocked"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_phase8.py",
            "--offline",
            "--tmp-dir",
            str(smoke_dir),
            "--database-url",
            "sqlite+pysqlite:////tmp/not-freqtrade.sqlite",
            "--skip-frontend",
        ],
        check=False,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "[BLOCKED] Phase 8 E2E reconciliation incomplete" in result.stdout
    evidence = json.loads((smoke_dir / "reports" / "phase8-e2e-evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "BLOCKED"
    assert evidence["acceptance"]["status"] == "BLOCKED"
    assert evidence["acceptance"]["acceptance_ready"] is False
    assert evidence["acceptance"]["blocked_reason"]
    assert evidence["acceptance"]["next_action"]
