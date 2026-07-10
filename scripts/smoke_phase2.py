#!/usr/bin/env python3
"""Offline Phase 2 strategy-research smoke check.

The smoke path uses temporary SQLite, fake strategy generation, deterministic
static review, fixture backtest metrics, and an optional frontend build. It does
not contact LLM providers, exchanges, Freqtrade runtime services, or production
databases.
"""

import argparse
import os
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"
if (
    os.environ.get("FREQTRADE_AI_PHASE2_SMOKE_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_PHASE2_SMOKE_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:////tmp/freqtrade-ai-phase2-smoke-import.sqlite")

from sqlalchemy.orm import Session  # noqa: E402

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager  # noqa: E402
from app.db.session import create_database_engine, create_session_factory  # noqa: E402
from app.models import BacktestResult, Base  # noqa: E402
from app.models.strategy import StrategyVersion  # noqa: E402
from app.repositories import BacktestRepository, StrategyRepository, StrategyScoreRepository  # noqa: E402
from app.schemas import (  # noqa: E402
    BacktestResultCreate,
    BacktestRunCreate,
    BacktestTaskCreate,
    StrategyFailureReasonCreate,
    StrategyVersionCreate,
)
from app.schemas.strategy_blueprint import BLUEPRINT_SCHEMA_VERSION, StrategyBlueprint  # noqa: E402
from app.services.strategy_failure_reasons import StrategyFailureReasonService  # noqa: E402
from app.services.strategy_generation import FakeStrategyBlueprintProvider, StrategyGenerationService  # noqa: E402
from app.services.strategy_scoring import SCORING_VERSION, StrategyScoringService  # noqa: E402
from app.services.strategy_static_review import StrategyStaticReviewService  # noqa: E402
from app.services.strategy_version_lineage import StrategyVersionLineageService  # noqa: E402


@dataclass
class Phase2SmokeContext:
    tmp_dir: Path
    db_session: Session
    strategy_id: Optional[int] = None
    parent_version_id: Optional[int] = None
    child_version_id: Optional[int] = None
    backtest_result_id: Optional[int] = None


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


def create_context(tmp_dir: Path) -> Phase2SmokeContext:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_dir / 'phase2-smoke.sqlite'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    return Phase2SmokeContext(tmp_dir=tmp_dir, db_session=session_factory())


def generate_schema_v2_strategy(context: Phase2SmokeContext) -> None:
    strategy_output_dir = context.tmp_dir / "strategies"
    strategy_output_dir.mkdir(parents=True, exist_ok=True)
    service = StrategyGenerationService(
        context.db_session,
        provider=FakeStrategyBlueprintProvider(),
        file_manager=StrategyFileManager(output_dir=strategy_output_dir),
    )
    version_ids = service.run_once(
        "Generate one offline Phase 2 smoke strategy blueprint.",
        requested_count=1,
    )
    if len(version_ids) != 1:
        raise RuntimeError(f"Expected one generated strategy version, got {len(version_ids)}")

    version = context.db_session.get(StrategyVersion, version_ids[0])
    if version is None:
        raise RuntimeError("Generated strategy version was not persisted")

    blueprint = StrategyBlueprint.model_validate(version.blueprint)
    if blueprint.schema_version != BLUEPRINT_SCHEMA_VERSION:
        raise RuntimeError(f"Expected schema v{BLUEPRINT_SCHEMA_VERSION}, got {blueprint.schema_version}")
    if not Path(version.file_path).exists():
        raise RuntimeError(f"Generated strategy file does not exist: {version.file_path}")

    context.strategy_id = version.strategy_id
    context.parent_version_id = version.id
    log(f"  schema_version={blueprint.schema_version} strategy_id={version.strategy_id} parent_version_id={version.id}")


def review_generated_strategy(context: Phase2SmokeContext) -> None:
    if context.parent_version_id is None:
        raise RuntimeError("Strategy generation must complete before static review")

    version = context.db_session.get(StrategyVersion, context.parent_version_id)
    if version is None:
        raise RuntimeError("Generated strategy version is missing")

    review = StrategyStaticReviewService().review_code(version.generated_code, filename=version.file_path)
    if not review.passed:
        raise RuntimeError(f"Generated strategy failed static review: {review.model_dump()}")

    blocked_review = StrategyStaticReviewService().review_code("import requests\nrequests.get('https://example.invalid')\n")
    if blocked_review.passed or blocked_review.summary["errors"] < 1:
        raise RuntimeError("Static review did not detect fixture network access")

    log(
        "  generated_static_review="
        f"errors={review.summary['errors']} warnings={review.summary['warnings']} "
        f"blocked_fixture_errors={blocked_review.summary['errors']}"
    )


def create_child_version_with_lineage(context: Phase2SmokeContext) -> None:
    if context.strategy_id is None or context.parent_version_id is None:
        raise RuntimeError("Strategy generation must complete before lineage check")

    parent = context.db_session.get(StrategyVersion, context.parent_version_id)
    if parent is None:
        raise RuntimeError("Parent strategy version is missing")

    repository = StrategyRepository(context.db_session)
    child_path = context.tmp_dir / "strategies" / "mvp_rsi_strategy_phase2_child.py"
    child_code = f"{parent.generated_code}\n# Phase 2 smoke lineage child\n"
    child_path.write_text(child_code, encoding="utf-8")
    child = repository.create_version(
        StrategyVersionCreate(
            strategy_id=context.strategy_id,
            parent_version_id=parent.id,
            blueprint={
                **parent.blueprint,
                "tags": [*parent.blueprint.get("tags", []), "phase-2-smoke-child"],
            },
            generated_code=child_code,
            file_path=str(child_path),
            validation_status="passed",
            validation_errors=[],
            change_summary="Phase 2 smoke child version with lower risk metadata.",
            diff_snapshot={
                "changed_fields": ["tags"],
                "before": {"tags": parent.blueprint.get("tags", [])},
                "after": {"tags": [*parent.blueprint.get("tags", []), "phase-2-smoke-child"]},
            },
        )
    )
    if child is None:
        raise RuntimeError("Child strategy version was not created")

    diff = StrategyVersionLineageService(context.db_session).get_diff(child.id)
    if diff is None or not diff.has_parent or diff.parent_version_id != parent.id:
        raise RuntimeError("Version diff/lineage did not record the parent relationship")
    if diff.diff_snapshot.get("changed_fields") != ["tags"]:
        raise RuntimeError(f"Unexpected diff snapshot: {diff.diff_snapshot}")

    context.child_version_id = child.id
    log(f"  child_version_id={child.id} parent_version_id={parent.id} changed_fields={diff.diff_snapshot['changed_fields']}")


def record_failure_reason_warning(context: Phase2SmokeContext) -> None:
    if context.strategy_id is None or context.child_version_id is None:
        raise RuntimeError("Lineage check must complete before failure reason check")

    service = StrategyFailureReasonService(context.db_session)
    reason = service.record_failure(
        StrategyFailureReasonCreate(
            strategy_id=context.strategy_id,
            strategy_version_id=context.child_version_id,
            stage="static_check",
            reason_type="static_policy_violation",
            severity="warning",
            message="Phase 2 smoke recorded a warning-level policy finding fixture.",
            details={"source": "smoke_phase2", "offline": True},
        )
    )
    if reason is None:
        raise RuntimeError("Failure reason warning was not recorded")

    reasons = service.list_version_failures(context.child_version_id)
    if len(reasons) != 1 or reasons[0].severity != "warning":
        raise RuntimeError(f"Unexpected failure reason query result: {reasons}")

    log(f"  failure_reasons={len(reasons)} severity={reasons[0].severity}")


def create_fixture_backtest_result(context: Phase2SmokeContext) -> None:
    if context.child_version_id is None:
        raise RuntimeError("Lineage check must complete before fixture backtest result")

    repository = BacktestRepository(context.db_session)
    run = repository.create_run(
        BacktestRunCreate(
            strategy_version_id=context.child_version_id,
            profile_name="phase2-offline-fixture",
            config_snapshot={"source": "smoke_phase2", "offline": True},
        )
    )
    if run is None:
        raise RuntimeError("Backtest run was not created")

    task = repository.create_task(
        run.id,
        BacktestTaskCreate(
            pair="BTC/USDT",
            timeframe="15m",
            config_path=str(context.tmp_dir / "freqtrade-config.json"),
        ),
    )
    if task is None:
        raise RuntimeError("Backtest task was not created")

    result = repository.save_result(
        task.id,
        BacktestResultCreate(
            result_path=str(context.tmp_dir / "phase2-backtest-result.json"),
            metrics_snapshot={
                "source": "smoke_phase2",
                "validation": {"passed": True, "warnings": []},
                "static_review": {"passed": True, "findings": []},
            },
            profit_total=87.0,
            profit_pct=0.087,
            max_drawdown_pct=0.045,
            win_rate=0.64,
            total_trades=36,
            timerange="20240101-20240201",
        ),
    )
    if result is None:
        raise RuntimeError("Fixture backtest result was not saved")

    context.backtest_result_id = result.id
    log(f"  backtest_run_id={run.id} result_id={result.id} metrics=fixture")


def score_and_verify_breakdown(context: Phase2SmokeContext) -> None:
    if context.backtest_result_id is None:
        raise RuntimeError("Fixture backtest result must exist before scoring")

    score = StrategyScoringService(context.db_session).score_backtest_result(context.backtest_result_id)
    if score is None:
        raise RuntimeError("Strategy score was not created")
    if score.scoring_version != SCORING_VERSION:
        raise RuntimeError(f"Unexpected scoring version: {score.scoring_version}")

    breakdown = score.metrics_snapshot.get("score_breakdown", [])
    expected_names = {"profit_score", "risk_score", "stability_score", "quality_score"}
    if {item.get("name") for item in breakdown} != expected_names:
        raise RuntimeError(f"Score breakdown missing expected components: {breakdown}")
    if score.metrics_snapshot["quality_breakdown"]["failure_history"]["warning_count"] != 1:
        raise RuntimeError("Failure reason warning was not included in quality scoring")

    ranking = StrategyScoreRepository(context.db_session).list_ranking()
    if len(ranking) != 1:
        raise RuntimeError(f"Expected one ranking entry, got {len(ranking)}")
    result = context.db_session.get(BacktestResult, context.backtest_result_id)
    if result is None:
        raise RuntimeError("Backtest result disappeared before ranking verification")
    if ranking[0].strategy_version_id != result.run.strategy_version_id:
        raise RuntimeError("Ranking entry does not reference the scored strategy version")

    log(
        "  ranking_top="
        f"{ranking[0].strategy_slug} total_score={ranking[0].total_score:.2f} "
        f"quality_score={ranking[0].quality_score:.2f} components={len(breakdown)}"
    )


def run_frontend_build() -> None:
    frontend_dir = REPO_ROOT / "frontend"
    if not frontend_dir.exists():
        raise RuntimeError("frontend directory does not exist")
    subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the offline Phase 2 strategy-research smoke check.")
    parser.add_argument("--offline", action="store_true", help="Required; confirms offline fixture mode.")
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("/tmp/freqtrade-ai-phase2-smoke"),
        help="Temporary workspace for SQLite and generated fixture files.",
    )
    parser.add_argument(
        "--skip-frontend-build",
        action="store_true",
        help="Skip npm frontend build; use only when running a backend-only diagnostic.",
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
    log("[INFO] mode=offline-fixture; no LLM, exchange, K-line download, dry-run, live trading, or production DB")

    context = create_context(tmp_dir)
    try:
        run_step("initialize temporary SQLite database", lambda: None)
        run_step("generate and validate StrategyBlueprint schema v2", lambda: generate_schema_v2_strategy(context))
        run_step("run static review coverage", lambda: review_generated_strategy(context))
        run_step("record strategy version diff and lineage", lambda: create_child_version_with_lineage(context))
        run_step("record and query failure reason fixture", lambda: record_failure_reason_warning(context))
        run_step("save fixture backtest metrics", lambda: create_fixture_backtest_result(context))
        run_step("score result and verify ranking breakdown", lambda: score_and_verify_breakdown(context))
        if args.skip_frontend_build:
            log("[SKIP] frontend build")
        else:
            run_step("build frontend", run_frontend_build)
    finally:
        context.db_session.close()

    log("[PASS] Phase 2 smoke completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
