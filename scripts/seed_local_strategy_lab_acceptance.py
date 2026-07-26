#!/usr/bin/env python3
"""Create an exclusive, isolated Local Strategy Lab acceptance seed."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.engine import make_url


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
REAL_CANONICAL_ROOT = Path("~/Developer/Freqtrade Ai").expanduser().resolve(strict=False)
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db.session import create_database_engine, create_session_factory  # noqa: E402
from app.models import Base  # noqa: E402
from app.repositories import (  # noqa: E402
    BacktestRepository,
    StrategyGenerationRunRepository,
    StrategyRepository,
)
from app.schemas import (  # noqa: E402
    BacktestResultCreate,
    BacktestRunCreate,
    BacktestRunStatusUpdate,
    BacktestTaskCreate,
    BacktestTaskStatusUpdate,
    StrategyCreate,
    StrategyGenerationRunCreate,
    StrategyGenerationRunStatusUpdate,
    StrategyVersionCreate,
)
from app.services.strategy_scoring import StrategyScoringService  # noqa: E402


SeedProfile = str


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _assert_safe_parent(parent: Path) -> Path:
    expanded = parent.expanduser()
    if stat.S_ISLNK(os.lstat(expanded).st_mode):
        raise ValueError("acceptance parent must not be a symlink")
    resolved = expanded.resolve(strict=True)
    if not resolved.is_dir() or resolved in {Path("/"), REPO_ROOT, REPO_ROOT.parent}:
        raise ValueError("unsafe acceptance parent")
    forbidden = {
        REAL_CANONICAL_ROOT,
        (REAL_CANONICAL_ROOT / "user_data").resolve(strict=False),
        (REAL_CANONICAL_ROOT / "reports").resolve(strict=False),
    }
    if resolved in forbidden or _is_relative_to(resolved, REAL_CANONICAL_ROOT):
        raise ValueError("acceptance parent must not be the real canonical repository or artifact roots")
    return resolved


def allocate_root(parent: Path) -> Path:
    safe_parent = _assert_safe_parent(parent)
    root = Path(tempfile.mkdtemp(prefix="freqtrade-ai-issue-433-", dir=str(safe_parent)))
    if stat.S_ISLNK(os.lstat(root).st_mode) or root.resolve(strict=True).parent != safe_parent:
        raise RuntimeError("exclusive acceptance root escaped its parent")
    return root


def _safe_destination(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("acceptance destination must be a relative path")
    current = root
    for component in Path(relative).parts[:-1]:
        current = current / component
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            os.mkdir(current, mode=0o700)
            mode = os.lstat(current).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError("acceptance destination contains a symlink or non-directory component")
        if not _is_relative_to(current.resolve(strict=True), root):
            raise ValueError("acceptance destination escaped root")
    destination = root / relative
    try:
        os.lstat(destination)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"acceptance destination already exists: {destination}")
    if not _is_relative_to(destination.parent.resolve(strict=True), root):
        raise ValueError("acceptance destination escaped root")
    return destination


def safe_write(root: Path, relative: str, content: bytes) -> Path:
    destination = _safe_destination(root, relative)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
    finally:
        os.close(descriptor)
    if stat.S_ISLNK(os.lstat(destination).st_mode):
        raise RuntimeError("acceptance destination became a symlink")
    if not _is_relative_to(destination.resolve(strict=True), root):
        raise RuntimeError("acceptance write escaped root")
    return destination


def safe_write_json(root: Path, relative: str, payload: dict[str, Any]) -> Path:
    return safe_write(
        root,
        relative,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def guarded_database_url(root: Path) -> tuple[str, Path]:
    path = _safe_destination(root, "database/acceptance.sqlite")
    url = f"sqlite+pysqlite:///{path}"
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or parsed.database != str(path):
        raise ValueError("acceptance seed requires its allocated file-backed sqlite database")
    return url, path


def _create_generation_and_version(
    session_factory: Any,
    root: Path,
) -> tuple[dict[str, int], Any, Any, Any]:
    strategy_code = b'class AcceptanceCurrentStrategy:\n    minimal_roi = {"0": 0.01}\n'
    strategy_path = safe_write(
        root,
        "user_data/strategies/generated/AcceptanceCurrentStrategy.py",
        strategy_code,
    )
    checksum = hashlib.sha256(strategy_code).hexdigest()
    with session_factory() as session:
        generation_repo = StrategyGenerationRunRepository(session)
        generation = generation_repo.create(
            StrategyGenerationRunCreate(
                provider="qa-seed",
                model="no-call",
                prompt_hash="issue-433-controlled-seed",
                prompt_summary="Controlled persistence evidence; no Provider was executed.",
                params_snapshot={
                    "controlled_acceptance_seed": True,
                    "provider_executed": False,
                    "freqtrade_executed": False,
                },
                requested_count=1,
            )
        )
        generation_repo.update_status(
            generation.id,
            StrategyGenerationRunStatusUpdate(
                status="succeeded", generated_count=1, accepted_count=1, failed_count=0
            ),
        )
        strategies = StrategyRepository(session)
        strategy = strategies.create(
            StrategyCreate(name="Acceptance Current Strategy", slug="acceptance-current-strategy")
        )
        version = strategies.create_version(
            StrategyVersionCreate(
                strategy_id=strategy.id,
                generation_run_id=generation.id,
                blueprint={"class_name": "AcceptanceCurrentStrategy"},
                generated_code=strategy_code.decode(),
                code_hash=checksum,
                file_path=str(strategy_path),
                validation_status="passed",
                diff_snapshot={
                    "strategy_file_validation": {
                        "approved_root": str(root / "user_data/strategies/generated"),
                        "checksum": checksum,
                        "validation_status": "passed",
                        "write_status": "written",
                    },
                    "controlled_acceptance_seed": {"provider_executed": False},
                },
            )
        )
        if version is None:
            raise RuntimeError("failed to persist StrategyVersion")
        return {
            "strategy_generation_run_id": generation.id,
            "strategy_id": strategy.id,
            "strategy_version_id": version.id,
        }, strategy_path, version, checksum


def seed_profile(root: Path, profile: SeedProfile) -> dict[str, Any]:
    database_url, database_path = guarded_database_url(root)
    if database_path.exists():
        raise FileExistsError("acceptance database must not already exist")
    created_database = safe_write(root, "database/acceptance.sqlite", b"")
    if created_database != database_path or not _is_relative_to(
        created_database.resolve(strict=True), root
    ):
        raise RuntimeError("acceptance database escaped its exclusive root")
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    ids: dict[str, int] = {}
    artifacts: dict[str, str] = {}

    if profile != "empty":
        ids, strategy_path, version, checksum = _create_generation_and_version(session_factory, root)
        artifacts.update(strategy_file=str(strategy_path), strategy_sha256=checksum)
        with session_factory() as session:
            backtests = BacktestRepository(session)
            run = backtests.create_run(
                BacktestRunCreate(
                    strategy_version_id=version.id,
                    profile_name="issue-433-controlled",
                    config_snapshot={"dry_run": True, "controlled_acceptance_seed": True},
                )
            )
            if run is None:
                raise RuntimeError("failed to persist BacktestRun")
            error_message = (
                "controlled long diagnostic " + ("X" * 900)
                if profile == "long-evidence"
                else None
            )
            task = backtests.create_task(
                run.id,
                BacktestTaskCreate(
                    pair="BTC/USDT:USDT",
                    timeframe="15m",
                    config_path=str(root / "reports/backtests/acceptance-config.json"),
                ),
            )
            if task is None:
                raise RuntimeError("failed to persist BacktestTask")
            ids.update(backtest_run_id=run.id, backtest_task_id=task.id)
            if profile == "missing-strategy":
                strategy_path.unlink()
            if profile in {"missing-result", "missing-strategy"}:
                backtests.update_task_status(
                    task.id,
                    BacktestTaskStatusUpdate(status="succeeded", error_message=error_message),
                )
                backtests.update_run_status(run.id, BacktestRunStatusUpdate(status="succeeded"))
            else:
                result_payload = {
                    "metadata": {"controlled_acceptance_seed": True, "provider_executed": False},
                    "strategy": "AcceptanceCurrentStrategy",
                    "metrics": {
                        "profit_pct": 0.08,
                        "max_drawdown_pct": 0.03,
                        "win_rate": 0.62,
                        "total_trades": 48,
                    },
                }
                result_relative = (
                    "reports/backtests/"
                    + ("nested-evidence-" * 12)
                    + "/acceptance-result.json"
                    if profile == "long-evidence"
                    else "reports/backtests/acceptance-result.json"
                )
                result_path = safe_write_json(root, result_relative, result_payload)
                result = backtests.save_result(
                    task.id,
                    BacktestResultCreate(
                        result_path=str(result_path),
                        metrics_snapshot=result_payload["metrics"],
                        profit_pct=0.08,
                        max_drawdown_pct=0.03,
                        win_rate=0.62,
                        total_trades=48,
                        timerange="20240101-20240301",
                    ),
                )
                if result is None:
                    raise RuntimeError("failed to persist BacktestResult")
                backtests.update_task_status(
                    task.id,
                    BacktestTaskStatusUpdate(
                        status="succeeded",
                        result_path=str(result_path),
                        error_message=error_message,
                    ),
                )
                backtests.update_run_status(run.id, BacktestRunStatusUpdate(status="succeeded"))
                score = StrategyScoringService(session).score_backtest_result(result.id)
                if score is None:
                    raise RuntimeError("failed to persist StrategyScore")
                ids.update(backtest_result_id=result.id, strategy_score_id=score.id)
                artifacts["backtest_result"] = str(result_path)

    manifest = {
        "schema_version": 1,
        "profile": profile,
        "database": str(database_path),
        "database_url": database_url,
        "canonical_root": str(root),
        "database_ids": ids,
        "artifacts": artifacts,
        "safety": {
            "provider_executed": False,
            "freqtrade_executed": False,
            "exchange_connected": False,
            "dry_run_started": False,
            "live_trading": False,
            "orders_created": False,
        },
    }
    manifest_path = safe_write_json(root, "acceptance-seed-manifest.json", manifest)
    return {**manifest, "manifest_path": str(manifest_path)}


def create_seed(parent: Path, profile: SeedProfile) -> dict[str, Any]:
    root = allocate_root(parent)
    try:
        return seed_profile(root, profile)
    except BaseException:
        # Allocation ownership starts here, before a manifest exists.  The
        # wrapper cannot clean a root that create_seed never returned.
        shutil.rmtree(root)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--root", help=argparse.SUPPRESS)
    parser.add_argument(
        "--profile",
        choices=("empty", "complete-current", "missing-result", "missing-strategy", "long-evidence"),
        default="complete-current",
    )
    args = parser.parse_args()
    if args.root is not None:
        raise ValueError("explicit acceptance roots are forbidden; use --parent for mkdtemp allocation")
    print(json.dumps(create_seed(Path(args.parent), args.profile), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
