#!/usr/bin/env python3
"""Persist one completed offline candidate report without touching deployment/runtime."""

import argparse
import json
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    outcome = parser.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--report", type=Path)
    outcome.add_argument("--failure-report", type=Path)
    outcome.add_argument("--failure-reason")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repository-commit", required=True)
    parser.add_argument("--stage", default="REPORT_PERSISTENCE")
    parser.add_argument("--generated-count", type=int, default=0)
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "backend"))
    from app.db.session import session_scope
    from app.services.strategy_research import StrategyResearchPersistenceService

    with session_scope() as db:
        service = StrategyResearchPersistenceService(db)
        if args.report is not None:
            report_path = args.report if args.report.is_absolute() else repo / args.report
            batch = service.persist_report(
                report_path.resolve(),
                run_id=args.run_id,
                repository_commit=args.repository_commit,
            )
            service.attach_persistence_receipt(report_path.resolve(), batch)
        elif args.failure_report is not None:
            failure_path = (
                args.failure_report
                if args.failure_report.is_absolute()
                else repo / args.failure_report
            ).resolve()
            payload = json.loads(failure_path.read_text(encoding="utf-8"))
            if (
                payload.get("schema_version")
                != "freqtrade-ai-strategy-candidate-research-failure-v1"
            ):
                raise RuntimeError("unsupported research failure report schema")
            batch = service.record_failed_batch(
                run_id=args.run_id,
                repository_commit=args.repository_commit,
                stage=str(payload.get("failed_stage") or args.stage),
                failure_reason=str(payload.get("failure_reason") or "unspecified failure"),
                requested_count=int(payload.get("requested_count") or 10),
                candidate_evidence=payload.get("candidates") or [],
                report_path=str(failure_path),
            )
            service.attach_persistence_receipt(failure_path, batch)
        else:
            batch = service.record_failed_batch(
                run_id=args.run_id,
                repository_commit=args.repository_commit,
                stage=args.stage,
                failure_reason=args.failure_reason,
                generated_count=args.generated_count,
            )
        result = {
            "batch_id": batch.id,
            "run_id": batch.run_id,
            "status": batch.status,
            "generated_count": batch.generated_count,
            "persisted_count": batch.persisted_count,
            "qualified_count": batch.qualified_count,
            "rejected_count": batch.rejected_count,
            "failure_reason": batch.failure_reason,
            "allow_real_funds": False,
            "real_orders": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
