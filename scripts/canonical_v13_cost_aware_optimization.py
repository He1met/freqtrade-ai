#!/usr/bin/env python3
"""Canonical API control for one frozen cost-aware OOS optimization batch."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.canonical_v13.optimization import optimization_selection_digest  # noqa: E402


API_ROOT = "http://127.0.0.1:8011/api/canonical-v13"
ACTOR = "canonical-v13-cost-aware-oos-optimizer-v1"


class Blocked(RuntimeError):
    pass


def canonical_digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def read_json(path: Path) -> dict[str, object]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise Blocked("BLOCKED_COMMAND_FILE_PATH")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Blocked("BLOCKED_COMMAND_FILE_INVALID")
    return value


def request(path: str, body: dict[str, object]) -> dict[str, object]:
    command = Request(
        API_ROOT + path,
        data=json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(command, timeout=30) as response:
            value = json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise Blocked(f"BLOCKED_CANONICAL_API_HTTP_{exc.code}:{detail}") from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise Blocked("BLOCKED_CANONICAL_API_UNAVAILABLE") from exc
    if not isinstance(value, dict):
        raise Blocked("BLOCKED_CANONICAL_API_RESPONSE")
    return value


def objective(
    plan: dict[str, object],
    *,
    market_snapshot_id: str,
    market_snapshot_digest: str,
    market_artifact_digest: str,
    executor_image_digest: str,
) -> dict[str, object]:
    return {
        "contract": "canonical-v13-cost-aware-oos-optimization-objective-v1",
        "plan_digest": canonical_digest(plan),
        "market_snapshot_id": market_snapshot_id,
        "market_snapshot_digest": market_snapshot_digest,
        "market_artifact_digest": market_artifact_digest,
        "executor_image_digest": executor_image_digest,
        "data_isolation": plan["data_isolation"],
        "execution": plan["execution"],
        "costs": plan["costs"],
        "hard_gates": plan["hard_gates"],
        "objective": plan["objective"],
        "families": plan["families"],
        "holdout_results_observed": False,
        "trading_capability": "TRADING_DISABLED",
        "execution_side_effects": 0,
    }


def create(args: argparse.Namespace) -> dict[str, object]:
    plan = read_json(args.plan)
    payload = request(
        "/optimizations",
        {
            "baseline_qualification_decision_id": args.baseline_qualification_decision_id,
            "actor_identity": ACTOR,
            "objective_json": objective(
                plan,
                market_snapshot_id=args.market_snapshot_id,
                market_snapshot_digest=args.market_snapshot_digest,
                market_artifact_digest=args.market_artifact_digest,
                executor_image_digest=args.executor_image_digest,
            ),
        },
    )
    return {**payload, "plan_digest": canonical_digest(plan)}


def persist(args: argparse.Namespace) -> dict[str, object]:
    run = read_json(args.run_receipt)
    result = read_json(args.worker_result)
    if result.get("contract") != "canonical-v13-cost-aware-oos-optimization-result-v1":
        raise Blocked("BLOCKED_OPTIMIZATION_RESULT_CONTRACT")
    trials = result.get("trials")
    selected = result.get("selected_trial_numbers")
    if not isinstance(trials, list) or not isinstance(selected, list) or len(trials) != 96:
        raise Blocked("BLOCKED_OPTIMIZATION_RESULT_BUDGET")
    evidence = [
        {
            "trial_number": row["trial_number"],
            "parameters_json": row["parameters_json"],
            "metrics_json": row["metrics_json"],
        }
        for row in trials
        if isinstance(row, dict)
    ]
    digest = optimization_selection_digest(
        optimization_run_id=UUID(str(run["optimization_run_id"])),
        run_request_digest=str(run["request_digest"]),
        actor_identity=ACTOR,
        selected_trial_numbers=[int(value) for value in selected],
        trials=evidence,
    )
    receipts = []
    selected_set = {int(value) for value in selected}
    for row in evidence:
        metrics = {
            **row["metrics_json"],
            "selected_finalist": int(row["trial_number"]) in selected_set,
            "selection_digest": digest,
        }
        receipts.append(
            request(
                f"/optimizations/{run['optimization_run_id']}/trials",
                {
                    "trial_number": row["trial_number"],
                    "actor_identity": ACTOR,
                    "parameters_json": row["parameters_json"],
                    "metrics_json": metrics,
                },
            )
        )
    completion = request(
        f"/optimizations/{run['optimization_run_id']}/complete",
        {
            "actor_identity": ACTOR,
            "terminal_status": "SUCCEEDED" if selected else "BLOCKED",
            "selected_trial_numbers": selected,
        },
    )
    if completion.get("result_digest") != digest:
        raise Blocked("BLOCKED_OPTIMIZATION_SELECTION_DIGEST_DRIFT")
    return {
        "status": completion["status"],
        "optimization_run_id": run["optimization_run_id"],
        "trial_count": len(receipts),
        "trial_receipt_set_digest": canonical_digest(receipts),
        "selected_trial_numbers": selected,
        "selection_digest": digest,
        "completion": completion,
        "all_trial_replays": all(item.get("repeat_noop") is True for item in receipts),
        "execution_side_effects": 0,
        "trading_capability": "TRADING_DISABLED",
    }


def render(args: argparse.Namespace) -> dict[str, object]:
    result = read_json(args.worker_result)
    selected = {int(value) for value in result.get("selected_trial_numbers", [])}
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifacts = []
    for row in result.get("trials", []):
        if not isinstance(row, dict) or int(row["trial_number"]) not in selected:
            continue
        path = output / f"{row['strategy_class']}.py"
        source = str(row["strategy_source"])
        if sha256(source.encode()).hexdigest() != row["strategy_source_digest"]:
            raise Blocked("BLOCKED_FINALIST_SOURCE_DIGEST_DRIFT")
        path.write_text(source, encoding="utf-8")
        artifacts.append(
            {
                "trial_number": row["trial_number"],
                "family_key": row["family_key"],
                "strategy_class": row["strategy_class"],
                "source_path": str(path),
                "source_digest": row["strategy_source_digest"],
            }
        )
    return {"status": "RENDERED", "artifacts": artifacts, "execution_side_effects": 0}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create_parser = sub.add_parser("create")
    create_parser.add_argument("--plan", type=Path, required=True)
    create_parser.add_argument("--baseline-qualification-decision-id", required=True)
    create_parser.add_argument("--market-snapshot-id", required=True)
    create_parser.add_argument("--market-snapshot-digest", required=True)
    create_parser.add_argument("--market-artifact-digest", required=True)
    create_parser.add_argument("--executor-image-digest", required=True)
    persist_parser = sub.add_parser("persist")
    persist_parser.add_argument("--run-receipt", type=Path, required=True)
    persist_parser.add_argument("--worker-result", type=Path, required=True)
    render_parser = sub.add_parser("render")
    render_parser.add_argument("--worker-result", type=Path, required=True)
    render_parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        value = {"create": create, "persist": persist, "render": render}[args.command](args)
    except (Blocked, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason_code": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
