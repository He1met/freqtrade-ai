#!/usr/bin/env python3
"""Offline Phase 1 MVP smoke check.

This script uses only temporary local files, SQLite, fake strategy generation,
and fixture backtest output. It does not contact LLM providers, exchanges, or
Freqtrade runtime services.
"""

import argparse
import json
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
    os.environ.get("FREQTRADE_AI_SMOKE_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_SMOKE_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:////tmp/freqtrade-ai-smoke-import.sqlite")

from sqlalchemy.orm import Session  # noqa: E402

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager  # noqa: E402
from app.db.session import create_database_engine, create_session_factory  # noqa: E402
from app.models import Base  # noqa: E402
from app.models.strategy import StrategyVersion  # noqa: E402
from app.repositories import BacktestRepository, StrategyRepository, StrategyScoreRepository  # noqa: E402
from app.schemas import BacktestRunCreate, BacktestTaskCreate  # noqa: E402
from app.services.backtest_execution import BacktestTaskExecutionService  # noqa: E402
from app.services.strategy_generation import FakeStrategyBlueprintProvider, StrategyGenerationService  # noqa: E402
from app.services.strategy_scoring import StrategyScoringService  # noqa: E402


@dataclass
class SmokeContext:
    tmp_dir: Path
    db_session: Session
    strategy_version_id: Optional[int] = None
    strategy_class_name: Optional[str] = None
    backtest_run_id: Optional[int] = None
    backtest_result_id: Optional[int] = None


class FixtureBacktestRunner:
    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir

    def run_backtest(
        self,
        config_path: Path,
        strategy_name: str,
        result_path: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Path:
        if not config_path.exists():
            raise RuntimeError(f"Fixture config file is missing: {config_path}")

        output_path = result_path or self.tmp_dir / "backtest-result.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "strategy": {
                strategy_name: {
                    "profit_total_abs": 123.45,
                    "profit_total_pct": 12.3,
                    "max_drawdown_pct": 4.5,
                    "wins": 30,
                    "losses": 10,
                    "draws": 2,
                    "total_trades": 42,
                    "backtest_start": "20240101",
                    "backtest_end": "20240201",
                }
            }
        }
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return output_path


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


def create_context(tmp_dir: Path) -> SmokeContext:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_dir / 'smoke.sqlite'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    return SmokeContext(tmp_dir=tmp_dir, db_session=session_factory())


def generate_strategy(context: SmokeContext) -> None:
    strategy_output_dir = context.tmp_dir / "strategies"
    strategy_output_dir.mkdir(parents=True, exist_ok=True)
    service = StrategyGenerationService(
        context.db_session,
        provider=FakeStrategyBlueprintProvider(),
        file_manager=StrategyFileManager(output_dir=strategy_output_dir),
    )

    version_ids = service.run_once("Generate one offline MVP smoke strategy.", requested_count=1)
    if len(version_ids) != 1:
        raise RuntimeError(f"Expected one generated strategy version, got {len(version_ids)}")

    version = context.db_session.get(StrategyVersion, version_ids[0])
    if version is None:
        raise RuntimeError("Generated strategy version was not persisted")
    if not Path(version.file_path).exists():
        raise RuntimeError(f"Generated strategy file does not exist: {version.file_path}")

    context.strategy_version_id = version.id
    context.strategy_class_name = str(version.blueprint["class_name"])
    log(f"  generated version_id={version.id} file={version.file_path}")


def run_fixture_backtest(context: SmokeContext) -> None:
    if context.strategy_version_id is None or context.strategy_class_name is None:
        raise RuntimeError("Strategy generation must complete before backtest")

    config_path = context.tmp_dir / "freqtrade-config.json"
    result_path = context.tmp_dir / "backtest-result.json"
    config_path.write_text(
        json.dumps(
            {
                "mode": "offline-fixture",
                "dry_run": False,
                "exchange": "fixture",
                "pair": "BTC/USDT",
                "timeframe": "15m",
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    repository = BacktestRepository(context.db_session)
    run = repository.create_run(
        BacktestRunCreate(
            strategy_version_id=context.strategy_version_id,
            profile_name="offline-fixture",
            config_snapshot={"source": "smoke_mvp", "offline": True},
        )
    )
    if run is None:
        raise RuntimeError("Backtest run was not created")
    task = repository.create_task(
        run.id,
        BacktestTaskCreate(pair="BTC/USDT", timeframe="15m", config_path=str(config_path)),
    )
    if task is None:
        raise RuntimeError("Backtest task was not created")

    updated = BacktestTaskExecutionService(
        context.db_session,
        FixtureBacktestRunner(context.tmp_dir),
    ).execute_next_pending(
        run.id,
        context.strategy_class_name,
        result_path=result_path,
        timeout_seconds=30,
    )
    if updated is None or updated.status != "succeeded":
        raise RuntimeError(f"Fixture backtest did not succeed: {getattr(updated, 'status', None)}")

    results = repository.list_results(run.id)
    if len(results) != 1:
        raise RuntimeError(f"Expected one parsed backtest result, got {len(results)}")

    context.backtest_run_id = run.id
    context.backtest_result_id = results[0].id
    log(f"  backtest_run_id={run.id} result_id={results[0].id} result={result_path}")


def score_and_rank(context: SmokeContext) -> None:
    if context.backtest_result_id is None:
        raise RuntimeError("Backtest result must exist before scoring")

    score = StrategyScoringService(context.db_session).score_backtest_result(context.backtest_result_id)
    if score is None:
        raise RuntimeError("Strategy score was not created")

    ranking = StrategyScoreRepository(context.db_session).list_ranking()
    if not ranking:
        raise RuntimeError("Ranking is empty after scoring")
    top = ranking[0]
    if top.strategy_slug != "mvp-rsi-strategy":
        raise RuntimeError(f"Unexpected top-ranked strategy: {top.strategy_slug}")

    log(
        "  ranking_top="
        f"{top.strategy_slug} total_score={top.total_score:.2f} "
        f"profit={top.profit_score:.2f} risk={top.risk_score:.2f}"
    )


def run_frontend_build() -> None:
    frontend_dir = REPO_ROOT / "frontend"
    if not frontend_dir.exists():
        raise RuntimeError("frontend directory does not exist")
    subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the offline Phase 1 MVP smoke check.")
    parser.add_argument("--offline", action="store_true", help="Required; confirms offline fixture mode.")
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("/tmp/freqtrade-ai-smoke"),
        help="Temporary workspace for SQLite, generated strategy, and fixture result files.",
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
    log("[INFO] mode=offline-fixture; no LLM, exchange, dry-run, live trading, or production DB")

    context = create_context(tmp_dir)
    try:
        run_step("initialize temporary SQLite database", lambda: None)
        run_step("generate strategy with fake provider", lambda: generate_strategy(context))
        run_step("run fixture backtest and parse result", lambda: run_fixture_backtest(context))
        run_step("score result and build ranking", lambda: score_and_rank(context))
        if args.skip_frontend_build:
            log("[SKIP] frontend build")
        else:
            run_step("build frontend", run_frontend_build)
    finally:
        context.db_session.close()

    log("[PASS] MVP smoke completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
