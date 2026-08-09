#!/usr/bin/env python3
"""Record short-lived ownership after the caller completes the Codex task gate."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-task-id", required=True)
    parser.add_argument("--ttl-minutes", type=int, default=20, choices=range(1, 31))
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    now = datetime.now(timezone.utc)
    path = repo / ".freqtrade-ai/research/formal-strategy-research-ownership.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "freqtrade-ai-formal-research-ownership-v1",
        "scope": "FORMAL_STRATEGY_RESEARCH",
        "canonical_root": str(repo.resolve()),
        "owner_task_id": args.owner_task_id,
        "confirmed_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=args.ttl_minutes)).isoformat(),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
