#!/usr/bin/env python3
"""Reset, seed, and inspect guarded local-only test databases."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"

if (
    os.environ.get("FREQTRADE_AI_PHASE8_DB_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_PHASE8_DB_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.core.exceptions import ConfigurationError  # noqa: E402
from app.services.local_test_db import Phase8LocalTestDbService, default_database_url  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Guard, reset, seed, dirty-seed, and summarize a Phase 8/9 local/dev/test database. "
            "The command refuses production/shared/remote/unknown targets."
        )
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL") or default_database_url(),
        help=(
            "SQLAlchemy URL. Defaults to DATABASE_URL, then "
            "sqlite+pysqlite:////tmp/freqtrade-ai-phase8-local-test.sqlite."
        ),
    )
    parser.add_argument(
        "--environment",
        default=os.environ.get("APP_ENV", "local"),
        choices=[
            "local",
            "dev",
            "test",
            "debug",
            "phase8",
            "phase9",
            "local-test",
            "phase8-local",
            "phase9-local",
        ],
        help="Required safety label for destructive operations.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("guard", help="Validate that the target database is safe.")
    subparsers.add_parser("reset", help="Drop and recreate all app tables in a guarded local/test DB.")
    subparsers.add_parser("seed-baseline", help="Insert success/failed/BLOCKED/unknown/missing/partial seed data.")
    subparsers.add_parser("dirty-scenario", help="Insert intentionally dirty QA scenarios.")
    subparsers.add_parser(
        "seed-operational-readiness",
        help="Reset and seed Phase 9 success/failure/BLOCKED/dirty/unknown-source QA scenarios.",
    )
    subparsers.add_parser("acceptance-report", help="Print the Phase 9 local-test acceptance coverage report.")
    summary = subparsers.add_parser("summary", help="Print local-test batch summaries.")
    summary.add_argument("--limit", type=int, default=20, help="Maximum batch summaries to return.")
    return parser.parse_args()


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    service = Phase8LocalTestDbService(
        args.database_url,
        environment_label=args.environment,
    )
    if args.command == "guard":
        target = service.validate_target()
        return {
            "status": "ok",
            "database": target.redacted_url,
            "dialect": target.dialect,
            "environment_label": target.environment_label,
            "reason": target.reason,
        }
    if args.command == "reset":
        return {"status": "ok", "operation": "reset", "summary": service.reset_database()}
    if args.command == "seed-baseline":
        return {"status": "ok", "operation": "seed-baseline", "summary": service.seed_baseline()}
    if args.command == "dirty-scenario":
        return {"status": "ok", "operation": "dirty-scenario", "summary": service.seed_dirty_scenarios()}
    if args.command == "seed-operational-readiness":
        return {
            "status": "ok",
            "operation": "seed-operational-readiness",
            "summary": service.seed_operational_readiness(),
        }
    if args.command == "acceptance-report":
        return {"status": "ok", "operation": "acceptance-report", "summary": service.operational_readiness_report()}
    if args.command == "summary":
        return {"status": "ok", "operation": "summary", "summary": service.summarize_batches(limit=args.limit)}
    raise ConfigurationError(f"Unsupported command: {args.command}")


def print_result(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    print(f"Status: {result['status']}")
    operation = result.get("operation")
    if operation:
        print(f"Operation: {operation}")
    summary = result.get("summary")
    if isinstance(summary, dict):
        if "batch_key" in summary:
            print(f"Batch: {summary['batch_key']}")
            print(f"Scenario set: {summary['scenario_set']}")
            print(f"Source counts: {summary['source_counts']}")
            print(f"Scenario counts: {summary['scenario_counts']}")
        elif "batches" in summary:
            print(f"Batch count: {len(summary['batches'])}")
            for batch in summary["batches"]:
                print(f"- {batch['batch_key']} [{batch['scenario_set']}] {batch.get('source_counts', {})}")
    else:
        print(f"Database: {result.get('database')}")
        print(f"Dialect: {result.get('dialect')}")
        print(f"Reason: {result.get('reason')}")


def main() -> int:
    args = parse_args()
    try:
        print_result(run_command(args), as_json=args.json)
    except ConfigurationError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
