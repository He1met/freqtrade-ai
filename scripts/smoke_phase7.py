#!/usr/bin/env python3
"""Offline Phase 7 engineering smoke check.

The smoke path validates read-only runtime evidence, operator diagnostics,
governance event archival, repo secret scanning, and dashboard fallback
contracts with local fixtures only. It does not start Freqtrade, connect to an
exchange, download market data, place orders, read real credentials, or perform
deployment control.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"
if (
    os.environ.get("FREQTRADE_AI_PHASE7_SMOKE_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_PHASE7_SMOKE_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.core.config import Settings  # noqa: E402
from app.repositories.audit_log import GovernanceEventArchiveRepository  # noqa: E402
from app.services.audit_log import GovernanceAuditLogService  # noqa: E402
from app.services.operator_status import OperatorStatusService  # noqa: E402
from app.services.runtime_contract import RuntimeReadOnlyContractService  # noqa: E402
from app.schemas.runtime_contract import RuntimeStatusSummary  # noqa: E402
from app.services.secret_scanning import scan_repo_for_secrets  # noqa: E402


FIXED_NOW = datetime(2026, 7, 5, 15, 30, tzinfo=timezone.utc)
PHASE7_SUMMARY_REF = "reports/runtime/phase7-smoke-summary.json"


class OfflineResearchReadiness:
    """The Phase 7 offline smoke owns only fixture evidence, never real readiness."""

    def build(self) -> RuntimeStatusSummary:
        return RuntimeStatusSummary(
            name="research_readiness",
            status="READY",
            source="fixture",
            summary="Offline Phase 7 fixture provides complete research evidence.",
        )


@dataclass
class Phase7SmokeContext:
    tmp_dir: Path
    repo_fixture: Path
    runtime_dir: Path
    governance_dir: Path
    settings: Settings
    runtime_paths: dict[str, Path] = field(default_factory=dict)
    success_path: dict[str, str] = field(default_factory=dict)
    blocked_paths: dict[str, str] = field(default_factory=dict)
    artifact_links: list[dict[str, Any]] = field(default_factory=list)
    audit_event_ids: list[str] = field(default_factory=list)
    secret_scan_status: Optional[str] = None
    dashboard_fallback_status: Optional[str] = None


def log(message: str) -> None:
    print(message, flush=True)


def run_step(name: str, action: Callable[[], None]) -> None:
    log(f"[RUN] {name}")
    try:
        action()
    except Exception as exc:
        log(f"[FAIL] {name}: {exc}")
        traceback.print_exc()
        raise
    log(f"[PASS] {name}")


def prepare_tmp_dir(tmp_dir: Path) -> None:
    if tmp_dir in {Path("/"), REPO_ROOT, BACKEND_PATH, REPO_ROOT.parent}:
        raise RuntimeError(f"Refusing unsafe smoke tmp-dir: {tmp_dir}")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def create_context(tmp_dir: Path) -> Phase7SmokeContext:
    repo_fixture = tmp_dir / "repo-fixture"
    return Phase7SmokeContext(
        tmp_dir=tmp_dir,
        repo_fixture=repo_fixture,
        runtime_dir=repo_fixture / "reports" / "runtime",
        governance_dir=repo_fixture / "reports" / "governance",
        settings=Settings(
            freqtrade_user_data=Path("user_data"),
            strategy_output_dir=Path("user_data/strategies/generated"),
            market_data_dir=Path("user_data/data"),
            backtest_result_dir=Path("reports/backtests"),
            log_dir=Path("logs"),
            tmp_freqtrade_config_dir=Path("tmp/freqtrade_configs"),
            allow_live_trading=False,
            allow_dry_run_trading=False,
        ),
    )


def prepare_repo_fixture(context: Phase7SmokeContext) -> None:
    (context.repo_fixture / ".git").mkdir(parents=True)
    for path in (
        context.repo_fixture / "config",
        context.repo_fixture / "user_data" / "strategies" / "generated",
        context.repo_fixture / "user_data" / "data",
        context.repo_fixture / "reports" / "backtests",
        context.runtime_dir,
        context.governance_dir,
        context.repo_fixture / "logs",
        context.repo_fixture / "tmp" / "freqtrade_configs",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (context.repo_fixture / "config" / "app.yaml").write_text(
        "app:\n  name: freqtrade-ai\nsecurity:\n  allow_live_trading: false\n",
        encoding="utf-8",
    )
    (context.repo_fixture / "config" / "exchange.yaml").write_text(
        "credentials:\n  api_key_env: OKX_DEMO_API_KEY\n  api_secret_env: OKX_DEMO_API_SECRET\n",
        encoding="utf-8",
    )
    (context.repo_fixture / "config" / "llm.yaml").write_text(
        "models:\n  fixture:\n    api_key_env: OPENAI_API_KEY\n",
        encoding="utf-8",
    )
    (context.repo_fixture / ".env.example").write_text(
        "DATABASE_URL=placeholder\nOKX_DEMO_API_SECRET=<OKX_DEMO_API_SECRET>\n",
        encoding="utf-8",
    )
    log(f"  repo_fixture={context.repo_fixture}")


def write_runtime_artifacts(context: Phase7SmokeContext) -> None:
    paths = {
        "dry_run_status_path": context.runtime_dir / "dry-run-status.json",
        "dry_run_manifest_path": context.runtime_dir / "missing-dry-run-manifest.json",
        "live_candidate_monitoring_path": context.runtime_dir / "live-candidate-monitoring.json",
        "live_candidate_monitoring_manifest_path": context.runtime_dir / "missing-monitoring-manifest.json",
        "phase7_smoke_summary_path": context.runtime_dir / "phase7-smoke-summary.json",
    }
    write_json(
        paths["dry_run_status_path"],
        {
            "status": "running",
            "profile_name": "phase7-offline-smoke",
            "strategy_version_id": 204,
            "strategy_name": "Phase7ReadOnlyFixture",
            "exchange": "fixture-only",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "dry_run": True,
            "last_updated": "2026-07-05T15:29:00Z",
            "recent_events": [
                {
                    "timestamp": "2026-07-05T15:29:00Z",
                    "event_type": "status_read",
                    "severity": "INFO",
                    "message": "Offline fixture status was parsed.",
                    "source": "phase7-smoke",
                }
            ],
        },
    )
    write_json(
        paths["live_candidate_monitoring_path"],
        {
            "status": "ok",
            "profile_name": "phase7-live-candidate-fixture",
            "profile_hash": "b" * 64,
            "deployment_record_id": "phase7-offline-record",
            "deployment_status": "PLANNED",
            "approval_status": "APPROVED_FOR_DEPLOYMENT_RECORD",
            "preflight_status": "APPROVED_FOR_REVIEW",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "source_ref": "reports/runtime/live-candidate-monitoring.json",
            "last_updated": "2026-07-05T15:29:00Z",
            "alerts": [],
        },
    )
    write_json(
        paths["phase7_smoke_summary_path"],
        {
            "status": "PASS",
            "generated_at": FIXED_NOW.isoformat().replace("+00:00", "Z"),
            "summary": "Phase 7 offline smoke summary seed for read-only contract validation.",
            "safety": safety_boundary(),
        },
    )
    context.runtime_paths = paths
    context.artifact_links.append(
        {
            "name": "phase7_smoke_summary",
            "path": PHASE7_SUMMARY_REF,
            "source": "artifact",
            "status": "READY",
            "exists": True,
        }
    )
    log(f"  phase7_summary={paths['phase7_smoke_summary_path']}")


def runtime_contract_service() -> RuntimeReadOnlyContractService:
    return RuntimeReadOnlyContractService(
        now_provider=lambda: FIXED_NOW,
        research_service=OfflineResearchReadiness(),
    )


def operator_status_service(environ: Optional[dict[str, str]] = None) -> OperatorStatusService:
    return OperatorStatusService(
        runtime_contract_service=runtime_contract_service(),
        now_provider=lambda: FIXED_NOW,
        environ=environ or {},
    )


def validate_runtime_contract(context: Phase7SmokeContext) -> None:
    contract = runtime_contract_service().build_contract(**context.runtime_paths)
    if contract.status != "READY":
        raise RuntimeError(f"Expected READY runtime contract, got {contract.status}")
    if contract.runtime_readiness.status != "READY" or contract.fallback_status.active:
        raise RuntimeError("Runtime contract did not reach read-only ready state without fallback")
    if not contract.safety.read_only:
        raise RuntimeError("Runtime contract safety boundary must stay read-only")
    if (
        contract.safety.allow_live_trading
        or contract.safety.allow_real_orders
        or contract.safety.allow_exchange_connection
        or contract.safety.allow_deploy_control
        or contract.safety.can_start_stop_bot
    ):
        raise RuntimeError("Runtime contract exposed a forbidden runtime control capability")

    write_json(
        context.runtime_dir / "runtime-read-only-contract.json",
        contract.model_dump(mode="json", exclude_none=True),
    )
    context.success_path["runtime_contract"] = contract.status
    log(f"  runtime_contract={contract.status} fallback_active={contract.fallback_status.active}")


def validate_operator_status(context: Phase7SmokeContext) -> None:
    env_value = "fixture-env-value-that-must-not-render"
    report = operator_status_service(environ={"DATABASE_URL": env_value}).build_status(
        repo_root=context.repo_fixture,
        settings=context.settings,
        env_names=("DATABASE_URL", "OKX_DEMO_API_SECRET", "OPENAI_API_KEY"),
        required_env_names=(),
        **context.runtime_paths,
    )
    rendered = report.model_dump_json()
    if report.status != "READY":
        raise RuntimeError(f"Expected READY operator status, got {report.status}: {report.blocked_reasons}")
    if report.runtime_contract.status != "READY" or report.runtime_contract.fallback_active:
        raise RuntimeError("Operator runtime summary did not reflect ready read-only evidence")
    if env_value in rendered:
        raise RuntimeError("Operator status rendered an ENV value")

    blocked_report = operator_status_service().build_status(
        repo_root=context.repo_fixture,
        settings=context.settings,
        env_names=("DATABASE_URL",),
        required_env_names=("DATABASE_URL",),
        dry_run_status_path=context.runtime_paths["dry_run_status_path"],
        dry_run_manifest_path=context.runtime_paths["dry_run_manifest_path"],
        live_candidate_monitoring_path=context.runtime_paths["live_candidate_monitoring_path"],
        live_candidate_monitoring_manifest_path=context.runtime_paths[
            "live_candidate_monitoring_manifest_path"
        ],
        phase7_smoke_summary_path=context.runtime_dir / "missing-phase7-smoke-summary.json",
    )
    if blocked_report.status != "BLOCKED":
        raise RuntimeError(f"Expected BLOCKED operator status, got {blocked_report.status}")
    if not any("Required ENV variable is missing: DATABASE_URL" in item for item in blocked_report.blocked_reasons):
        raise RuntimeError("Blocked operator status did not preserve the missing ENV reason")
    if not any("Phase 7 smoke summary does not exist" in item for item in blocked_report.unavailable_reasons):
        raise RuntimeError("Blocked operator status did not preserve the missing smoke summary reason")

    write_json(context.runtime_dir / "operator-status.json", report.model_dump(mode="json", exclude_none=True))
    write_json(
        context.runtime_dir / "operator-status-blocked.json",
        blocked_report.model_dump(mode="json", exclude_none=True),
    )
    context.success_path["operator_status"] = report.status
    context.blocked_paths["missing_env_and_smoke_summary"] = blocked_report.status
    log("  operator_status=READY blocked_path=missing_env_and_smoke_summary:BLOCKED")


def record_governance_event(context: Phase7SmokeContext) -> None:
    archive_path = context.governance_dir / "governance-events.jsonl"
    service = GovernanceAuditLogService(
        repository=GovernanceEventArchiveRepository(archive_path),
        now_provider=lambda: FIXED_NOW,
    )
    event = service.record_event(
        event_type="smoke_run",
        status="ACCEPTED",
        actor={"actor_id": "codex-phase7-smoke", "role": "automation"},
        source={
            "name": "scripts/smoke_phase7.py",
            "source_type": "smoke",
            "ref": "scripts/smoke_phase7.py",
        },
        summary="Phase 7 offline smoke recorded read-only runtime and operator evidence.",
        artifact_links=[
            {
                "name": "phase7_smoke_summary",
                "path": PHASE7_SUMMARY_REF,
                "kind": "summary",
                "required": True,
            },
            {
                "name": "operator_status_json",
                "path": "reports/runtime/operator-status.json",
                "kind": "report",
                "required": True,
            },
        ],
        payload={
            "runtime_contract": context.success_path.get("runtime_contract"),
            "operator_status": context.success_path.get("operator_status"),
            "blocked_paths": context.blocked_paths,
            "secret_token": "gho_fixture_value_must_not_render",
        },
        tags=["phase-7", "smoke", "offline-fixture"],
        event_id="phase7-smoke-accepted",
        created_at=FIXED_NOW,
    )
    events = service.query_events(limit=10)
    rendered = "\n".join(item.model_dump_json() for item in events)
    if event.event_id not in {item.event_id for item in events}:
        raise RuntimeError("Governance smoke event was not archived")
    if "gho_fixture_value_must_not_render" in rendered:
        raise RuntimeError("Governance event archive leaked a secret-shaped value")
    if not event.artifact_links or event.artifact_links[0].path != PHASE7_SUMMARY_REF:
        raise RuntimeError("Governance event did not preserve the Phase 7 artifact link")

    context.audit_event_ids.append(event.event_id)
    context.artifact_links.append(
        {
            "name": "governance_events_jsonl",
            "path": "reports/governance/governance-events.jsonl",
            "source": "artifact",
            "status": "READY",
            "exists": True,
        }
    )
    log(f"  audit_event={event.event_id} archive={archive_path}")


def validate_secret_scanning(context: Phase7SmokeContext) -> None:
    tracked_report = scan_repo_for_secrets(REPO_ROOT, tracked_only=True)
    script_report = scan_repo_for_secrets(
        REPO_ROOT,
        scan_paths=("scripts/smoke_phase7.py",),
        tracked_only=False,
    )
    if tracked_report.findings or script_report.findings:
        payload = {
            "tracked": tracked_report.to_dict(),
            "smoke_script": script_report.to_dict(),
        }
        write_json(context.runtime_dir / "secret-scan-report.json", payload)
        raise RuntimeError(f"Secret scan blocked: {payload}")

    write_json(
        context.runtime_dir / "secret-scan-report.json",
        {
            "status": "PASS",
            "tracked": tracked_report.to_dict(),
            "smoke_script": script_report.to_dict(),
        },
    )
    context.secret_scan_status = "PASS"
    context.success_path["security_scan"] = "PASS"
    log(
        "  security_scan=PASS "
        f"tracked_files={tracked_report.scanned_files} smoke_script_files={script_report.scanned_files}"
    )


def validate_dashboard_fallback(context: Phase7SmokeContext) -> None:
    dashboard_path = REPO_ROOT / "frontend" / "src" / "pages" / "OperatorDashboard.tsx"
    notice_path = REPO_ROOT / "frontend" / "src" / "pages" / "operatorDashboardNotice.ts"
    mock_path = REPO_ROOT / "frontend" / "src" / "data" / "mock.ts"
    client_path = REPO_ROOT / "frontend" / "src" / "api" / "client.ts"
    dashboard_text = dashboard_path.read_text(encoding="utf-8")
    notice_text = notice_path.read_text(encoding="utf-8")
    mock_text = mock_path.read_text(encoding="utf-8")
    client_text = client_path.read_text(encoding="utf-8")

    required_fragments = (
        "operatorDashboardNotice",
        "Backend API 已连接；下方状态来自只读运行契约。",
        "未使用 Backend API 数据，不能作为运行验收依据。",
        "runtimeContract",
        "operatorStatus",
        "auditEvents",
        "artifactLinks",
        "reportsEnvValues",
        "allowLiveTrading",
        "allowExchangeConnection",
        "canStartStopBot",
    )
    combined = "\n".join((dashboard_text, notice_text, mock_text, client_text))
    missing = [fragment for fragment in required_fragments if fragment not in combined]
    if missing:
        raise RuntimeError(f"Operator Dashboard fallback contract is missing: {missing}")

    retired_notice = "Backend API unavailable; showing controlled Phase 7 operator fallback data."
    if retired_notice in "\n".join((dashboard_text, notice_text)):
        raise RuntimeError("Operator Dashboard must not claim fallback data while its source is unknown or failed")

    forbidden_fragments = ("start_live", "stop_live", "deploy_command", "place_order", "<button")
    present_forbidden = [fragment for fragment in forbidden_fragments if fragment in dashboard_text]
    if present_forbidden:
        raise RuntimeError(f"Operator Dashboard exposed forbidden control UI: {present_forbidden}")
    if '"allowLiveTrading: true"' in mock_text or "allowLiveTrading: true" in mock_text:
        raise RuntimeError("Operator Dashboard mock data must not enable live trading")

    context.dashboard_fallback_status = "PASS"
    context.success_path["dashboard_fallback"] = "PASS"
    log("  dashboard_fallback=PASS read_only_controls=absent")


def safety_boundary() -> dict[str, bool]:
    return {
        "offline_fixture": True,
        "production_ready": False,
        "real_freqtrade": False,
        "exchange_connection": False,
        "market_data_download": False,
        "dry_run_started": False,
        "live_trading": False,
        "real_orders": False,
        "real_credentials_read": False,
        "credentials_persisted": False,
        "production_deployment": False,
        "deployment_execution": False,
        "freqtrade_source_modified": False,
    }


def write_phase7_summary(context: Phase7SmokeContext) -> None:
    required_success = {
        "runtime_contract",
        "operator_status",
        "security_scan",
        "dashboard_fallback",
    }
    missing = required_success - set(context.success_path)
    if missing:
        raise RuntimeError(f"missing required success paths: {sorted(missing)}")
    if context.blocked_paths.get("missing_env_and_smoke_summary") != "BLOCKED":
        raise RuntimeError("operator blocked path was not recorded")
    if not context.audit_event_ids:
        raise RuntimeError("governance audit event was not recorded")

    write_json(
        context.runtime_paths["phase7_smoke_summary_path"],
        {
            "status": "PASS",
            "generated_at": FIXED_NOW.isoformat().replace("+00:00", "Z"),
            "success_path": context.success_path,
            "blocked_paths": context.blocked_paths,
            "audit_event_ids": context.audit_event_ids,
            "artifact_links": context.artifact_links,
            "summary": (
                "Offline Phase 7 engineering smoke passed. Missing evidence and missing "
                "ENV paths remain fail-closed; this does not authorize production readiness."
            ),
            "safety": safety_boundary(),
        },
    )
    log(f"  summary_path={context.runtime_paths['phase7_smoke_summary_path']}")


def run_frontend_build() -> None:
    frontend_dir = REPO_ROOT / "frontend"
    if not frontend_dir.exists():
        raise RuntimeError("frontend directory does not exist")
    subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the offline Phase 7 engineering smoke check.")
    parser.add_argument("--offline", action="store_true", help="Required; confirms offline fixture mode.")
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("/tmp/freqtrade-ai-phase7-smoke"),
        help="Temporary workspace for generated Phase 7 runtime and governance artifacts.",
    )
    parser.add_argument(
        "--skip-frontend-build",
        action="store_true",
        help="Skip npm frontend build; use only for backend-only diagnostics or unit tests.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.offline:
        log("[FAIL] --offline is required; this smoke command only supports fixture mode.")
        return 2

    tmp_dir = args.tmp_dir.expanduser().resolve()
    prepare_tmp_dir(tmp_dir)
    log(f"[INFO] tmp_dir={tmp_dir}")
    log(
        "[INFO] mode=offline-fixture; no real Freqtrade, exchange connection, "
        "market-data download, dry-run start, live trading, real orders, secret output, "
        "or deployment execution"
    )

    context = create_context(tmp_dir)
    run_step("prepare repo-local runtime fixture", lambda: prepare_repo_fixture(context))
    run_step("write Phase 7 runtime artifacts", lambda: write_runtime_artifacts(context))
    run_step("validate runtime read-only contract", lambda: validate_runtime_contract(context))
    run_step("validate operator status and blocked path", lambda: validate_operator_status(context))
    run_step("record governance audit event", lambda: record_governance_event(context))
    run_step("run secret scanning safe path", lambda: validate_secret_scanning(context))
    run_step("validate Operator Dashboard fallback contract", lambda: validate_dashboard_fallback(context))
    run_step("write Phase 7 smoke summary", lambda: write_phase7_summary(context))
    if args.skip_frontend_build:
        log("[SKIP] frontend build")
    else:
        run_step("build frontend", run_frontend_build)

    log("[PASS] Phase 7 smoke completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
