#!/usr/bin/env python3
"""Refresh public research data and persist one generation-only 60-item batch.

This command never starts a backtest, deployment, signal, exchange, or order
path.  The existing lease-protected long-running research worker consumes the
persisted jobs separately and serially.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.db.session import session_scope  # noqa: E402
from app.services.bihourly_strategy_research import (  # noqa: E402
    BihourlyStrategyResearchBlocked,
    BihourlyStrategyResearchService,
)


def _run_id(now: datetime) -> str:
    floored = now.replace(hour=now.hour - now.hour % 2, minute=0, second=0, microsecond=0)
    return floored.strftime("%Y%m%d%H")


def _run_checked(command: list[str]) -> None:
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    if completed.returncode != 0:
        raise BihourlyStrategyResearchBlocked(
            f"CONTROLLED_COMMAND_FAILED:{Path(command[0]).name}"
        )


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise BihourlyStrategyResearchBlocked("CANONICAL_GIT_STATE_UNAVAILABLE")
    return completed.stdout.strip()


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datadir", type=Path, default=REPO_ROOT / "user_data/data/okx")
    parser.add_argument(
        "--receipt",
        type=Path,
        default=REPO_ROOT / "reports/research/okx-public-candle-source-latest.json",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--owner-task-id")
    parser.add_argument("--skip-runtime-verify", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-refresh", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    run_id = args.run_id or _run_id(now)
    report_path = REPO_ROOT / f"reports/research/bihourly-generation-{run_id}.json"
    base_report = {
        "schema_version": "bihourly-strategy-research-generation-v1",
        "run_id": run_id,
        "as_of": now.isoformat(),
        "generation_only": True,
        "serial_consumer_separate": True,
        "allow_real_funds": False,
        "real_orders": False,
        "exchange_access": "PUBLIC_MARKET_DATA_ONLY",
        "backtest_started": False,
        "deployment_started": False,
        "signal_or_order_started": False,
    }
    try:
        if _git_output("rev-parse", "--abbrev-ref", "HEAD") != "main":
            raise BihourlyStrategyResearchBlocked("CANONICAL_BRANCH_IS_NOT_MAIN")
        repository_commit = _git_output("rev-parse", "HEAD")
        if len(repository_commit) != 40:
            raise BihourlyStrategyResearchBlocked("CANONICAL_COMMIT_INVALID")
        if not args.skip_runtime_verify:
            _run_checked(
                [
                    str(REPO_ROOT / "backend/.venv/bin/python"),
                    str(REPO_ROOT / "scripts/local_runtime.py"),
                    "verify",
                ]
            )

        def refresh() -> None:
            if args.skip_refresh:
                return
            _run_checked(
                [
                    str(REPO_ROOT / "backend/.venv/bin/python"),
                    str(REPO_ROOT / "scripts/download_okx_research_market_data.py"),
                    "--datadir",
                    str(args.datadir),
                    "--receipt",
                    str(args.receipt),
                    "--incremental",
                ]
            )

        with session_scope() as db:
            result = BihourlyStrategyResearchService(
                db,
                canonical_root=REPO_ROOT,
                datadir=args.datadir,
            ).run_generation_only(
                run_id=run_id,
                repository_commit=repository_commit,
                owner_task_id=(
                    args.owner_task_id
                    or f"bihourly-strategy-research:{socket.gethostname()}"
                ),
                refresh=refresh,
                now=now,
            )
        report = {**base_report, **result.__dict__}
        _write_report(report_path, report)
        print(json.dumps(report, sort_keys=True))
        return 0
    except BihourlyStrategyResearchBlocked as exc:
        report = {**base_report, "status": "FAILED", "reason_code": str(exc)[:500]}
        _write_report(report_path, report)
        print(json.dumps(report, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
