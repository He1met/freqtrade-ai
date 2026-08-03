#!/usr/bin/env python3
"""Publish the fixed validated Demo strategies and optionally enable the guard."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.db.session import create_database_engine, create_session_factory  # noqa: E402
from app.services.okx_demo_automation_guard import OkxDemoAutomationGuard  # noqa: E402
from app.services.okx_demo_strategy_selection import (  # noqa: E402
    OkxDemoStrategySelectionService,
)


STRATEGIES = (
    "DeepSeekRegimeCrossoverCandidateB",
    "CodexOkxDemoDualRsiStrategy",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enable-continuous",
        action="store_true",
        help="owner-enable CONTINUOUS_DEMO_V1 after both deployments exist",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    factory = create_session_factory(engine)
    try:
        with factory() as db:
            service = OkxDemoStrategySelectionService(db, project_root=ROOT)
            deployments = [service.publish(name) for name in STRATEGIES]
            enabled = False
            if args.enable_continuous:
                run_id = db.execute(
                    text(
                        "SELECT last_reconciliation_run_id FROM "
                        "okx_demo_reconciliation_states "
                        "WHERE execution_target_id='OKX_DEMO'"
                    )
                ).scalar_one()
                enabled = bool(
                    db.execute(
                        text(
                            "SELECT enable_okx_demo_continuous_automation("
                            ":digest,:run)"
                        ),
                        {
                            "digest": OkxDemoAutomationGuard.policy_digest(),
                            "run": run_id,
                        },
                    ).scalar_one()
                )
                if not enabled:
                    db.rollback()
                    raise SystemExit("continuous Demo authorization was blocked")
                db.commit()
            print(
                json.dumps(
                    {
                        "execution_target": "OKX_DEMO",
                        "allow_real_funds": False,
                        "strategies": [
                            {"name": name, "active_slot": deployment.active_slot}
                            for name, deployment in zip(STRATEGIES, deployments)
                        ],
                        "continuous_guard_enabled": enabled,
                    },
                    sort_keys=True,
                )
            )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
