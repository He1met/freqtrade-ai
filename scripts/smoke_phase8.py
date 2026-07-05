#!/usr/bin/env python3
"""Phase 8 page/API/database reconciliation smoke check.

The default path starts a local backend and frontend against a guarded local
database, then proves that core Phase 8 evidence is backed by API and DB state.
It also seeds fixture, dirty, and unknown-source rows and verifies that those
rows remain non-core. It never starts Freqtrade, connects to an exchange,
downloads market data, places orders, or writes secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
FRONTEND_PATH = REPO_ROOT / "frontend"
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"
DEFAULT_DATABASE_URL = "sqlite+pysqlite:////tmp/freqtrade-ai-phase8-e2e.sqlite"

os.environ.setdefault("DATABASE_URL", DEFAULT_DATABASE_URL)
os.environ.setdefault("APP_ENV", "phase8")

if (
    os.environ.get("FREQTRADE_AI_PHASE8_SMOKE_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_PHASE8_SMOKE_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from sqlalchemy import select  # noqa: E402

from app.db.session import create_database_engine, create_session_factory, get_db  # noqa: E402
from app.models import Base  # noqa: E402
from app.models.backtest import BacktestResult  # noqa: E402
from app.models.strategy import StrategyVersion  # noqa: E402
from app.repositories import (  # noqa: E402
    BacktestRepository,
    StrategyGenerationRunRepository,
    StrategyRepository,
    StrategyScoreRepository,
)
from app.schemas import (  # noqa: E402
    BacktestResultCreate,
    BacktestResultRead,
    BacktestRunCreate,
    BacktestRunStatusUpdate,
    BacktestTaskCreate,
    BacktestTaskStatusUpdate,
    StrategyCreate,
    StrategyGenerationRunCreate,
    StrategyGenerationRunStatusUpdate,
    StrategyVersionCreate,
    StrategyVersionRead,
)
from app.services.local_test_db import Phase8LocalTestDbService  # noqa: E402
from app.services.strategy_scoring import StrategyScoringService  # noqa: E402


KEY_API_PATHS = (
    "/health",
    "/api/strategies",
    "/api/strategy-versions",
    "/api/strategy-generation-runs",
    "/api/backtest-runs",
    "/api/backtest-tasks",
    "/api/backtest-results",
    "/api/ranking",
)


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[str]

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


@dataclass
class Phase8SmokeContext:
    tmp_dir: Path
    database_url: str
    backend_url: str
    frontend_url: str
    evidence_path: Path
    session_factory: Any = None
    core_ids: dict[str, int] = field(default_factory=dict)
    local_test_summary: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, Any] = field(default_factory=dict)
    processes: list[ManagedProcess] = field(default_factory=list)


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Phase 8 local-only page/API/database reconciliation. "
            "Default mode starts backend and frontend; --offline uses TestClient "
            "and skips browser/static frontend checks."
        )
    )
    parser.add_argument("--offline", action="store_true", help="Use in-process API checks and skip service startup.")
    parser.add_argument(
        "--tmp-dir",
        default="/tmp/freqtrade-ai-phase8-e2e",
        help="Local evidence directory. Refuses unsafe repo/root paths.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="Guarded local SQLAlchemy URL. Defaults to sqlite+pysqlite:////tmp/freqtrade-ai-phase8-e2e.sqlite.",
    )
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5173")
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--skip-backend-start", action="store_true", help="Use an already running backend.")
    parser.add_argument("--skip-frontend-start", action="store_true", help="Use an already running frontend.")
    parser.add_argument("--skip-frontend", action="store_true", help="Do not check or start the frontend page.")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--json", action="store_true", help="Print final evidence JSON.")
    parser.add_argument("--evidence-path", default=None, help="Override evidence JSON output path.")
    return parser.parse_args()


def prepare_tmp_dir(tmp_dir: Path) -> None:
    if tmp_dir in {Path("/"), REPO_ROOT, BACKEND_PATH, FRONTEND_PATH, REPO_ROOT.parent}:
        raise RuntimeError(f"Refusing unsafe smoke tmp-dir: {tmp_dir}")
    tmp_dir.mkdir(parents=True, exist_ok=True)


def create_context(args: argparse.Namespace) -> Phase8SmokeContext:
    tmp_dir = Path(args.tmp_dir).expanduser().resolve()
    evidence_path = (
        Path(args.evidence_path).expanduser().resolve()
        if args.evidence_path
        else tmp_dir / "reports" / "phase8-e2e-evidence.json"
    )
    return Phase8SmokeContext(
        tmp_dir=tmp_dir,
        database_url=args.database_url,
        backend_url=args.backend_url.rstrip("/"),
        frontend_url=args.frontend_url.rstrip("/"),
        evidence_path=evidence_path,
    )


def setup_database(context: Phase8SmokeContext) -> None:
    service = Phase8LocalTestDbService(context.database_url, environment_label="phase8")
    target = service.validate_target()
    reset_summary = service.reset_database()
    baseline_summary = service.seed_baseline()
    dirty_summary = service.seed_dirty_scenarios()
    engine = create_database_engine(context.database_url)
    Base.metadata.create_all(engine)
    context.session_factory = create_session_factory(engine)
    context.local_test_summary = {
        "target": {
            "database": target.redacted_url,
            "dialect": target.dialect,
            "environment_label": target.environment_label,
            "reason": target.reason,
        },
        "reset": reset_summary,
        "baseline": baseline_summary,
        "dirty": dirty_summary,
    }
    log(f"  database={target.redacted_url}")


def create_core_flow(context: Phase8SmokeContext) -> None:
    with context.session_factory() as session:
        generation_repo = StrategyGenerationRunRepository(session)
        generation_run = generation_repo.create(
            StrategyGenerationRunCreate(
                provider="local-phase8-e2e",
                model="deterministic",
                prompt_hash="phase8-e2e-core-flow",
                prompt_summary="Phase 8 UI/API/DB reconciliation core flow",
                requested_count=1,
            )
        )
        generation_repo.update_status(
            generation_run.id,
            StrategyGenerationRunStatusUpdate(
                status="succeeded",
                generated_count=1,
                accepted_count=1,
                failed_count=0,
            ),
        )

        strategy_repo = StrategyRepository(session)
        strategy = strategy_repo.create(StrategyCreate(name="Phase8 E2E Core", slug="phase8-e2e-core"))
        version = strategy_repo.create_version(
            StrategyVersionCreate(
                strategy_id=strategy.id,
                generation_run_id=generation_run.id,
                blueprint={"class_name": "Phase8E2ECore"},
                generated_code="class Phase8E2ECore: pass",
                code_hash="phase8-e2e-core-hash",
                file_path="user_data/strategies/generated/phase8_e2e_core.py",
                validation_status="passed",
                change_summary="Phase 8 E2E database-backed core version",
            )
        )
        if version is None:
            raise RuntimeError("StrategyVersion was not created")

        backtest_repo = BacktestRepository(session)
        run = backtest_repo.create_run(
            BacktestRunCreate(strategy_version_id=version.id, profile_name="phase8-e2e-local")
        )
        if run is None:
            raise RuntimeError("BacktestRun was not created")
        task = backtest_repo.create_task(
            run.id,
            BacktestTaskCreate(
                pair="BTC/USDT:USDT",
                timeframe="15m",
                config_path="reports/backtests/phase8-e2e-config.json",
            ),
        )
        if task is None:
            raise RuntimeError("BacktestTask was not created")
        result = backtest_repo.save_result(
            task.id,
            BacktestResultCreate(
                result_path="reports/backtests/phase8-e2e-result.json",
                profit_pct=0.11,
                max_drawdown_pct=0.025,
                win_rate=0.64,
                total_trades=52,
                timerange="20240101-20240301",
            ),
        )
        if result is None:
            raise RuntimeError("BacktestResult was not created")
        backtest_repo.update_task_status(
            task.id,
            BacktestTaskStatusUpdate(status="succeeded", result_path=result.result_path),
        )
        backtest_repo.update_run_status(run.id, BacktestRunStatusUpdate(status="succeeded"))
        score = StrategyScoringService(session).score_backtest_result(result.id)
        if score is None:
            raise RuntimeError("StrategyScore was not created")

        context.core_ids = {
            "strategy_id": strategy.id,
            "strategy_generation_run_id": generation_run.id,
            "strategy_version_id": version.id,
            "backtest_run_id": run.id,
            "backtest_task_id": task.id,
            "backtest_result_id": result.id,
            "strategy_score_id": score.id,
        }
    log(f"  core_ids={context.core_ids}")


def require_core_source(source: Any, required_ids: dict[str, int], source_types: set[str]) -> None:
    if isinstance(source, dict):
        source_type = source.get("source_type")
        core_data = source.get("core_data")
        database_ids = source.get("database_ids") or {}
    else:
        source_type = source.source_type
        core_data = source.core_data
        database_ids = source.database_ids
    if source_type not in source_types or core_data is not True:
        raise RuntimeError(f"Expected core source {source_types}, got {source}")
    for key, value in required_ids.items():
        if database_ids.get(key) != value:
            raise RuntimeError(f"Expected source id {key}={value}, got {database_ids}")


def is_non_core_source(source: Any) -> bool:
    if isinstance(source, dict):
        return source.get("source_type") in {"fixture", "fallback", "unknown"} and source.get("core_data") is False
    return source.source_type in {"fixture", "fallback", "unknown"} and source.core_data is False


def reconcile_database(context: Phase8SmokeContext) -> None:
    with context.session_factory() as session:
        version = session.get(StrategyVersion, context.core_ids["strategy_version_id"])
        result = session.get(BacktestResult, context.core_ids["backtest_result_id"])
        if version is None or result is None:
            raise RuntimeError("Core StrategyVersion or BacktestResult disappeared")

        version_read = StrategyVersionRead.model_validate(version)
        result_read = BacktestResultRead.model_validate(result)
        require_core_source(
            version_read.data_source,
            {
                "strategy_version_id": context.core_ids["strategy_version_id"],
                "strategy_id": context.core_ids["strategy_id"],
                "generation_run_id": context.core_ids["strategy_generation_run_id"],
            },
            {"database"},
        )
        require_core_source(
            result_read.data_source,
            {
                "backtest_result_id": context.core_ids["backtest_result_id"],
                "backtest_run_id": context.core_ids["backtest_run_id"],
                "backtest_task_id": context.core_ids["backtest_task_id"],
            },
            {"database"},
        )

        ranking = StrategyScoreRepository(session).list_ranking(limit=20)
        rank_entry = next(
            (entry for entry in ranking if entry.score_id == context.core_ids["strategy_score_id"]),
            None,
        )
        if rank_entry is None:
            raise RuntimeError("Core StrategyScore was not present in ranking")
        require_core_source(
            rank_entry.data_source,
            {
                "strategy_score_id": context.core_ids["strategy_score_id"],
                "strategy_id": context.core_ids["strategy_id"],
                "strategy_version_id": context.core_ids["strategy_version_id"],
                "backtest_result_id": context.core_ids["backtest_result_id"],
            },
            {"api_aggregate"},
        )

        all_versions = [StrategyVersionRead.model_validate(item) for item in session.scalars(select(StrategyVersion)).all()]
        all_results = [BacktestResultRead.model_validate(item) for item in session.scalars(select(BacktestResult)).all()]
        non_core_versions = [item for item in all_versions if is_non_core_source(item.data_source)]
        non_core_results = [item for item in all_results if is_non_core_source(item.data_source)]
        source_counts: dict[str, int] = {}
        for item in [*all_versions, *all_results]:
            source_type = item.data_source.source_type
            source_counts[source_type] = source_counts.get(source_type, 0) + 1
        if not non_core_versions:
            raise RuntimeError("Expected fixture/fallback/unknown StrategyVersion rows")
        if not non_core_results:
            raise RuntimeError("Expected fixture/fallback/unknown BacktestResult rows")

        context.checks["database_reconciliation"] = {
            "status": "PASS",
            "core_ids": context.core_ids,
            "ranking_entries": len(ranking),
            "source_counts": source_counts,
            "non_core_strategy_versions": len(non_core_versions),
            "non_core_backtest_results": len(non_core_results),
        }
        log(
            "  db_reconciliation=PASS "
            f"ranking_entries={len(ranking)} non_core_versions={len(non_core_versions)}"
        )


def fetch_json_http(base_url: str, path: str, timeout_seconds: int) -> tuple[int, Any]:
    request = urllib.request.Request(f"{base_url}{path}", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path} returned HTTP {exc.code}: {body[:240]}") from exc


def fetch_text_http(url: str, timeout_seconds: int) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {body[:240]}") from exc


def assert_api_payload(context: Phase8SmokeContext, payloads: dict[str, Any]) -> None:
    versions = payloads["/api/strategy-versions"]
    results = payloads["/api/backtest-results"]
    ranking = payloads["/api/ranking"]

    core_version = next((item for item in versions if item["id"] == context.core_ids["strategy_version_id"]), None)
    core_result = next((item for item in results if item["id"] == context.core_ids["backtest_result_id"]), None)
    core_rank = next((item for item in ranking if item["score_id"] == context.core_ids["strategy_score_id"]), None)
    if core_version is None or core_result is None or core_rank is None:
        raise RuntimeError("Core ids were not visible through API responses")

    require_core_source(
        core_version["data_source"],
        {
            "strategy_version_id": context.core_ids["strategy_version_id"],
            "strategy_id": context.core_ids["strategy_id"],
            "generation_run_id": context.core_ids["strategy_generation_run_id"],
        },
        {"database"},
    )
    require_core_source(
        core_result["data_source"],
        {
            "backtest_result_id": context.core_ids["backtest_result_id"],
            "backtest_run_id": context.core_ids["backtest_run_id"],
            "backtest_task_id": context.core_ids["backtest_task_id"],
        },
        {"database"},
    )
    require_core_source(
        core_rank["data_source"],
        {
            "strategy_score_id": context.core_ids["strategy_score_id"],
            "strategy_id": context.core_ids["strategy_id"],
            "strategy_version_id": context.core_ids["strategy_version_id"],
            "backtest_result_id": context.core_ids["backtest_result_id"],
        },
        {"api_aggregate"},
    )

    non_core_sources = [
        item["data_source"]
        for item in [*versions, *results, *ranking]
        if "data_source" in item and is_non_core_source(item["data_source"])
    ]
    if not non_core_sources:
        raise RuntimeError("Expected fixture/fallback/unknown API rows to remain non-core")


def probe_api(context: Phase8SmokeContext, fetcher: Callable[[str], tuple[int, Any]]) -> None:
    payloads: dict[str, Any] = {}
    statuses: dict[str, int] = {}
    counts: dict[str, int] = {}
    for path in KEY_API_PATHS:
        status, payload = fetcher(path)
        if status >= 400:
            raise RuntimeError(f"Key API {path} returned HTTP {status}")
        statuses[path] = status
        payloads[path] = payload
        counts[path] = len(payload) if isinstance(payload, list) else 1

    assert_api_payload(context, payloads)
    context.checks["api_reconciliation"] = {
        "status": "PASS",
        "http_statuses": statuses,
        "item_counts": counts,
    }
    log(f"  api_reconciliation=PASS key_paths={len(KEY_API_PATHS)}")


def probe_api_http(context: Phase8SmokeContext, timeout_seconds: int) -> None:
    probe_api(context, lambda path: fetch_json_http(context.backend_url, path, timeout_seconds))


def probe_api_offline(context: Phase8SmokeContext) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    def override_get_db() -> Any:
        db = context.session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)

        def fetcher(path: str) -> tuple[int, Any]:
            response = client.get(path)
            return response.status_code, response.json()

        probe_api(context, fetcher)
    finally:
        app.dependency_overrides.clear()


def start_backend(context: Phase8SmokeContext, port: int) -> None:
    env = {**os.environ, "DATABASE_URL": context.database_url, "APP_ENV": "phase8"}
    process = subprocess.Popen(
        [
            str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=BACKEND_PATH,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    context.processes.append(ManagedProcess("backend", process))


def start_frontend(context: Phase8SmokeContext, port: int) -> None:
    process = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(port)],
        cwd=FRONTEND_PATH,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    context.processes.append(ManagedProcess("frontend", process))


def wait_for_json(base_url: str, path: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            fetch_json_http(base_url, path, 3)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {base_url}{path}: {last_error}")


def wait_for_text(url: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            fetch_text_http(url, 3)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def start_services(context: Phase8SmokeContext, args: argparse.Namespace) -> None:
    if not args.skip_backend_start:
        start_backend(context, args.backend_port)
    wait_for_json(context.backend_url, "/health", args.timeout_seconds)
    if args.skip_frontend:
        return
    if not args.skip_frontend_start:
        start_frontend(context, args.frontend_port)
    wait_for_text(f"{context.frontend_url}/local-strategy-lab", args.timeout_seconds)


def probe_frontend(context: Phase8SmokeContext, timeout_seconds: int) -> None:
    status, html = fetch_text_http(f"{context.frontend_url}/local-strategy-lab", timeout_seconds)
    if status != 200:
        raise RuntimeError(f"Expected frontend HTTP 200, got {status}")
    if '<div id="root">' not in html and '<div id="root"></div>' not in html:
        raise RuntimeError("Frontend page did not contain React root")
    if "src=\"/src/main.tsx\"" not in html and "type=\"module\"" not in html:
        raise RuntimeError("Frontend page did not include a module script")
    context.checks["frontend_page"] = {
        "status": "PASS",
        "http_status": status,
        "html_bytes": len(html.encode("utf-8")),
        "browser_runtime_note": (
            "The script validates frontend startup and page delivery. "
            "Use the browser field in QA evidence for console/DOM runtime observations."
        ),
    }
    log(f"  frontend_page=PASS html_bytes={len(html.encode('utf-8'))}")


def safety_boundary() -> dict[str, bool]:
    return {
        "local_only": True,
        "database_guarded": True,
        "starts_freqtrade": False,
        "exchange_connection": False,
        "market_data_download": False,
        "live_trading": False,
        "real_orders": False,
        "stores_sensitive_values": False,
        "production_database": False,
    }


def write_evidence(context: Phase8SmokeContext, *, frontend_checked: bool) -> None:
    payload = {
        "status": "PASS",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "phase": "Phase 8",
        "scope": "page/api/database reconciliation",
        "core_ids": context.core_ids,
        "local_test_summary": context.local_test_summary,
        "checks": context.checks,
        "frontend_checked": frontend_checked,
        "safety": safety_boundary(),
    }
    context.evidence_path.parent.mkdir(parents=True, exist_ok=True)
    context.evidence_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"  evidence_path={context.evidence_path}")


def stop_processes(context: Phase8SmokeContext) -> None:
    for managed in reversed(context.processes):
        managed.stop()


def main() -> int:
    args = parse_args()
    context = create_context(args)
    try:
        run_step("prepare local evidence directory", lambda: prepare_tmp_dir(context.tmp_dir))
        run_step("guard, reset, and seed Phase 8 database", lambda: setup_database(context))
        run_step("insert database-backed core strategy/backtest/scoring flow", lambda: create_core_flow(context))
        run_step("reconcile direct database rows and source metadata", lambda: reconcile_database(context))
        if args.offline:
            run_step("reconcile backend API with TestClient", lambda: probe_api_offline(context))
        else:
            run_step("start local backend/frontend services", lambda: start_services(context, args))
            run_step("reconcile backend API over HTTP", lambda: probe_api_http(context, args.timeout_seconds))
            if not args.skip_frontend:
                run_step("check Local Strategy Lab page delivery", lambda: probe_frontend(context, args.timeout_seconds))
        run_step("write QA evidence", lambda: write_evidence(context, frontend_checked=(not args.offline and not args.skip_frontend)))
    finally:
        stop_processes(context)

    if args.json:
        print(context.evidence_path.read_text(encoding="utf-8"), end="")
    log("[PASS] Phase 8 E2E reconciliation completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
