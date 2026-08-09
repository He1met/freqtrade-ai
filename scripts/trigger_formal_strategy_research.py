#!/usr/bin/env python3
"""Trigger the same formal research coordinator used by the strategy-factory page."""

from pathlib import Path
import sys


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "backend"))
    from app.db.session import session_scope
    from app.services.formal_strategy_research import FormalStrategyResearchCoordinator

    with session_scope() as db:
        result = FormalStrategyResearchCoordinator().start(db, trigger="automation")
    print(result.model_dump_json())
    return 0 if result.status == "RUNNING" else 2


if __name__ == "__main__":
    raise SystemExit(main())
