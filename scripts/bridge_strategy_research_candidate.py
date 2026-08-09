#!/usr/bin/env python3
"""Explicitly bridge one exact Blueprint v2 research candidate.

This command never approves, deploys, starts a runtime, reads trading
credentials, or creates an order.  A successful equivalence proof creates a
draft canonical StrategyVersion and stops at canonical validation required.
"""

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.db.session import session_scope  # noqa: E402
from app.services.strategy_research_bridge import (  # noqa: E402
    StrategyResearchBridgeService,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-id", type=int, required=True)
    parser.add_argument("--blueprint-json", type=Path)
    parser.add_argument("--requested-by", required=True)
    args = parser.parse_args()
    blueprint = None
    if args.blueprint_json is not None:
        blueprint = json.loads(args.blueprint_json.read_text(encoding="utf-8"))
        if not isinstance(blueprint, dict):
            raise RuntimeError("Blueprint JSON must contain one object")
    with session_scope() as db:
        event = StrategyResearchBridgeService(
            db,
            project_root=REPO_ROOT,
        ).bridge(
            args.candidate_id,
            blueprint_payload=blueprint,
            requested_by=args.requested_by,
        )
        print(
            json.dumps(
                {
                    "bridge_event_id": event.id,
                    "research_candidate_id": event.research_candidate_id,
                    "outcome": event.outcome,
                    "reason_code": event.reason_code,
                    "canonical_strategy_id": event.strategy_id,
                    "canonical_strategy_version_id": event.strategy_version_id,
                    "canonical_full_chain_run_id": event.canonical_full_chain_run_id,
                    "execution_target_id": event.execution_target_id,
                    "allow_real_funds": event.allow_real_funds,
                    "real_orders": event.real_orders,
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
