import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.services.operator_status import OperatorStatusService


FIXED_NOW = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)


def service(environ=None) -> OperatorStatusService:
    return OperatorStatusService(now_provider=lambda: FIXED_NOW, environ=environ or {})


def settings_for() -> Settings:
    return Settings(
        freqtrade_user_data=Path("user_data"),
        strategy_output_dir=Path("user_data/strategies/generated"),
        market_data_dir=Path("user_data/data"),
        backtest_result_dir=Path("reports/backtests"),
        log_dir=Path("logs"),
        tmp_freqtrade_config_dir=Path("tmp/freqtrade_configs"),
        allow_live_trading=False,
        allow_dry_run_trading=False,
    )


def prepare_repo_fixture(repo_root: Path) -> None:
    (repo_root / ".git").mkdir(parents=True)
    for path in (
        repo_root / "config",
        repo_root / "user_data" / "strategies" / "generated",
        repo_root / "user_data" / "data",
        repo_root / "reports" / "backtests",
        repo_root / "reports" / "runtime",
        repo_root / "logs",
        repo_root / "tmp" / "freqtrade_configs",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (repo_root / "config" / "app.yaml").write_text("app:\n  name: freqtrade-ai\n", encoding="utf-8")
    (repo_root / "config" / "exchange.yaml").write_text(
        "credentials:\n  api_key_env: OKX_DEMO_API_KEY\n",
        encoding="utf-8",
    )
    (repo_root / "config" / "llm.yaml").write_text(
        "models:\n  fixture:\n    api_key_env: OPENAI_API_KEY\n",
        encoding="utf-8",
    )
    (repo_root / ".env.example").write_text("DATABASE_URL=placeholder\n", encoding="utf-8")


def write_runtime_fixtures(repo_root: Path) -> dict[str, Path]:
    runtime_dir = repo_root / "reports" / "runtime"
    paths = {
        "dry_run_status_path": runtime_dir / "dry-run-status.json",
        "dry_run_manifest_path": runtime_dir / "missing-dry-run-manifest.json",
        "live_candidate_monitoring_path": runtime_dir / "live-candidate-monitoring.json",
        "live_candidate_monitoring_manifest_path": runtime_dir / "missing-monitoring-manifest.json",
        "phase7_smoke_summary_path": runtime_dir / "phase7-smoke-summary.json",
    }
    paths["dry_run_status_path"].write_text(
        json.dumps(
            {
                "status": "running",
                "profile_name": "phase7-operator-status",
                "strategy_version_id": 198,
                "strategy_name": "MvpRsiStrategy",
                "exchange": "okx",
                "pair": "BTC/USDT:USDT",
                "timeframe": "15m",
                "dry_run": True,
                "last_updated": "2026-07-05T13:59:00Z",
            }
        ),
        encoding="utf-8",
    )
    paths["live_candidate_monitoring_path"].write_text(
        json.dumps(
            {
                "status": "ok",
                "profile_name": "phase6-live-candidate-btc-15m",
                "profile_hash": "a" * 64,
                "deployment_record_id": "deployment-record-abc123",
                "deployment_status": "PLANNED",
                "approval_status": "APPROVED_FOR_DEPLOYMENT_RECORD",
                "preflight_status": "APPROVED_FOR_REVIEW",
                "pair": "BTC/USDT:USDT",
                "timeframe": "15m",
                "source_ref": "reports/runtime/live-candidate-monitoring.json",
                "last_updated": "2026-07-05T13:59:00Z",
            }
        ),
        encoding="utf-8",
    )
    paths["phase7_smoke_summary_path"].write_text(
        json.dumps({"status": "PASS", "generated_at": "2026-07-05T13:59:30Z"}),
        encoding="utf-8",
    )
    return paths


def test_operator_status_reports_ready_like_local_diagnostics(tmp_path) -> None:
    prepare_repo_fixture(tmp_path)
    runtime_paths = write_runtime_fixtures(tmp_path)

    report = service(environ={"DATABASE_URL": "should-not-render"}).build_status(
        repo_root=tmp_path,
        settings=settings_for(),
        env_names=("DATABASE_URL", "OKX_DEMO_API_SECRET"),
        **runtime_paths,
    )
    rendered = report.model_dump_json()

    assert report.schema_version == "1"
    assert report.status == "READY"
    assert report.runtime_contract.status == "READY"
    assert report.runtime_contract.fallback_active is False
    assert {check.name for check in report.checks} >= {
        "repo_root",
        "config_app_yaml",
        "runtime_control_disabled",
        "runtime_read_only_contract",
        "env_presence",
    }
    assert {artifact.name for artifact in report.artifacts} >= {
        "dry_run_status_json",
        "live_candidate_monitoring_json",
        "phase7_smoke_summary",
    }
    assert report.env_presence[0].name == "DATABASE_URL"
    assert report.env_presence[0].present is True
    assert report.env_presence[0].value_rendered is False
    assert "should-not-render" not in rendered
    assert "start_live" not in rendered
    assert "deploy_command" not in rendered


def test_operator_status_returns_blocked_and_unavailable_reasons(tmp_path) -> None:
    report = service().build_status(
        repo_root=tmp_path,
        settings=settings_for(),
        env_names=("DATABASE_URL",),
        required_env_names=("DATABASE_URL",),
    )

    check_statuses = {check.status for check in report.checks}

    assert report.status == "BLOCKED"
    assert "BLOCKED" in check_statuses
    assert "UNAVAILABLE" in check_statuses
    assert any("Required config file is missing" in reason for reason in report.blocked_reasons)
    assert any("Phase 7 smoke summary does not exist" in reason for reason in report.unavailable_reasons)
    assert any("Required ENV variable is missing: DATABASE_URL" in reason for reason in report.blocked_reasons)


def test_operator_status_blocks_unsafe_runtime_flags(tmp_path) -> None:
    prepare_repo_fixture(tmp_path)
    runtime_paths = write_runtime_fixtures(tmp_path)
    unsafe_settings = settings_for().model_copy(update={"allow_live_trading": True})

    report = service().build_status(
        repo_root=tmp_path,
        settings=unsafe_settings,
        **runtime_paths,
    )

    assert report.status == "BLOCKED"
    assert any("allow_live_trading=true" in reason for reason in report.blocked_reasons)


def test_operator_status_endpoint_returns_read_only_shape() -> None:
    client = TestClient(app)

    response = client.get("/runtime/operator-status")

    assert response.status_code == 200
    payload = response.json()
    rendered = json.dumps(payload, sort_keys=True)
    assert payload["schema_version"] == "1"
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["reports_env_values"] is False
    assert payload["safety"]["allow_live_trading"] is False
    assert "checks" in payload
    assert "env_presence" in payload
    assert "runtime_contract" in payload
    assert "start_live" not in rendered
    assert "stop_live" not in rendered
