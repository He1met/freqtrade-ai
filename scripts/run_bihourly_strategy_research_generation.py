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
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.services.bihourly_strategy_research_trigger import (  # noqa: E402
    OwnerMediatedBihourlyStrategyResearchTrigger,
)


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
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    run_id = args.run_id
    report_run_id = run_id or now.replace(
        hour=now.hour - now.hour % 2, minute=0, second=0, microsecond=0
    ).strftime("%Y%m%d%H")
    report_path = REPO_ROOT / f"reports/research/bihourly-generation-{report_run_id}.json"
    base_report = {
        "schema_version": "bihourly-strategy-research-generation-v1",
        "as_of": now.isoformat(),
    }
    trigger = OwnerMediatedBihourlyStrategyResearchTrigger(
        canonical_root=REPO_ROOT,
        datadir=args.datadir,
        receipt_path=args.receipt,
        database_url=get_settings().database_url,
    )
    result = trigger.run(
        trigger="automation",
        run_id=run_id,
        owner_task_id=args.owner_task_id,
        now=now,
    )
    report = {**base_report, **result.model_dump()}
    _write_report(report_path, report)
    print(json.dumps(report, sort_keys=True))
    return 2 if result.status == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
