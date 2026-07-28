#!/usr/bin/env python3
"""Plan/apply fail-closed compaction of duplicated OKX Demo REST snapshots.

Default invocation is dry-run.  Apply requires a declared maintenance window,
creates a fresh secret-excluding logical backup, then performs one transaction.
Run ``make down`` before apply and ``make up && make verify`` afterwards.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.db.session import create_database_engine
from app.services.okx_demo_reconciliation_compaction import (
    ReconciliationCompactionBlocked,
    apply_compaction,
    build_compaction_plan,
    post_compaction_maintenance,
    verify_post_compaction,
)
from postgres_backup import BackupBlocked, create_backup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--retain-generations", type=int, default=100)
    parser.add_argument("--apply", action="store_true", help="perform the reviewed delete")
    parser.add_argument(
        "--maintenance-stopped",
        action="store_true",
        help="attest that the managed runtime has been stopped",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=REPO_ROOT / ".freqtrade-ai" / "backups",
    )
    parser.add_argument("--verify", action="store_true", help="only run post-maintenance verification")
    return parser.parse_args()


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    args = parse_args()
    if args.apply and not args.maintenance_stopped:
        _print({"status": "BLOCKED", "reason": "--maintenance-stopped is required"})
        return 2
    if args.apply and args.verify:
        _print({"status": "BLOCKED", "reason": "--apply and --verify are exclusive"})
        return 2
    engine = create_database_engine(args.database_url)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        if args.verify:
            with factory() as db:
                _print(verify_post_compaction(db))
            return 0
        with factory() as db:
            plan = build_compaction_plan(
                db, retain_generations=args.retain_generations
            )
            _print({"status": "DRY_RUN", "plan": plan.as_dict()})
        if not args.apply:
            return 0
        backup, manifest = create_backup(
            database_url=args.database_url,
            output_dir=args.backup_dir,
        )
        with factory.begin() as db:
            deleted = apply_compaction(db, plan)
        with factory.begin() as db:
            post_compaction_maintenance(db)
        with factory() as db:
            verification = verify_post_compaction(db)
        _print(
            {
                "status": "READY",
                "backup": str(backup),
                "backup_manifest": str(manifest),
                "deleted": deleted,
                "verification": verification,
                "next_action": "make up && make verify",
            }
        )
        return 0
    except (BackupBlocked, ReconciliationCompactionBlocked) as exc:
        _print({"status": "BLOCKED", "reason": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
