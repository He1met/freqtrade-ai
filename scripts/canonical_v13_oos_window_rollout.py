#!/usr/bin/env python3
"""Create immutable TRAIN/VALIDATION/HOLDOUT configuration evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import canonical_v13_market_window_rollout as base


ACTOR = "canonical-v13-oos-window-rollout-v1"


def _plan(path: Path) -> dict[str, object]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise base.MarketWindowRolloutBlocked("BLOCKED_PLAN_PATH")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("contract") != "canonical-v13-cost-aware-oos-optimization-plan-v1":
        raise base.MarketWindowRolloutBlocked("BLOCKED_PLAN_CONTRACT")
    return value


def window_payload(plan: dict[str, object]) -> dict[str, object]:
    isolation = plan["data_isolation"]
    if not isinstance(isolation, dict):
        raise base.MarketWindowRolloutBlocked("BLOCKED_PLAN_WINDOWS")
    warmup = int(isolation["warmup_closed_candles"])
    margin = int(isolation["integrity_margin_closed_candles"])
    windows = []
    for key, minimum, required in (
        ("train", 11520, False),
        ("validation", 2880, False),
        ("holdout", 2880, True),
    ):
        item = isolation[key]
        windows.append(
            {
                "window_key": f"optimization-{key}",
                "required": required,
                "start_at": item["start_at"],
                "end_at": item["end_at"],
                "coverage": {
                    "minimum_closed_candles": minimum,
                    "warmup_closed_candles": warmup,
                    "integrity_margin_closed_candles": margin,
                    "freshness_max_age_seconds": 3600,
                },
            }
        )
    return {"windows": windows}


def apply(plan: dict[str, object]) -> dict[str, object]:
    health = base._health()
    if health.get("status") != "HEALTHY" or health.get("trading_capability") != "TRADING_DISABLED":
        raise base.MarketWindowRolloutBlocked("BLOCKED_CANONICAL_API_NOT_NO_TRADE")
    payload = window_payload(plan)
    catalog = base._request("/configurations")
    base_versions = {
        kind: base._latest_validated(catalog, kind)
        for kind in base.DEPENDENCY_KINDS
        if kind != "WINDOW"
    }
    base._latest_validated(catalog, "WINDOW")
    base._latest_validated(catalog, "RESEARCH_AGGREGATE")
    original_actor = base.ACTOR
    base.ACTOR = ACTOR
    try:
        window_draft, window_snapshot = base._draft_and_validate(
            base._request,
            kind="WINDOW",
            profile_key=base.WINDOW_PROFILE,
            payload=payload,
            dependencies=[],
        )
        dependency_versions = {
            **{kind: str(item["version_id"]) for kind, item in base_versions.items()},
            "WINDOW": str(window_draft["version_id"]),
        }
        dependencies = [
            {
                "version_id": dependency_versions[kind],
                "expected_kind": kind,
                "relation_key": f"snapshot:{kind.lower()}",
            }
            for kind in base.DEPENDENCY_KINDS
        ]
        aggregate_draft, aggregate_snapshot = base._draft_and_validate(
            base._request,
            kind="RESEARCH_AGGREGATE",
            profile_key=base.AGGREGATE_PROFILE,
            payload={"assembly_key": base.ASSEMBLY_KEY},
            dependencies=dependencies,
        )
    finally:
        base.ACTOR = original_actor
    snapshot_ids = {
        **{kind: str(item["snapshot_id"]) for kind, item in base_versions.items()},
        "WINDOW": str(window_snapshot["snapshot_id"]),
        "RESEARCH_AGGREGATE": str(aggregate_snapshot["snapshot_id"]),
    }
    preview = base._request(
        "/research-bundles/preview",
        body={
            "scope_key": base.SCOPE,
            "workflow_key": base.WORKFLOW,
            "snapshot_ids": snapshot_ids,
            "market_snapshot_id": None,
        },
    )
    if preview.get("status") != "BLOCKED" or "MARKET_SNAPSHOT_UNSET" not in preview.get("reason_codes", []):
        raise base.MarketWindowRolloutBlocked("BLOCKED_UNEXPECTED_WINDOW_PREVIEW_STATE")
    return {
        "status": "OOS_WINDOWS_AND_AGGREGATE_VALIDATED_MARKET_BLOCKED",
        "actor_identity": ACTOR,
        "plan_digest": base._canonical_digest(plan),
        "window_payload_digest": base._canonical_digest(payload),
        "window": {"draft": window_draft, "snapshot": window_snapshot},
        "research_aggregate": {"draft": aggregate_draft, "snapshot": aggregate_snapshot},
        "snapshot_ids": snapshot_ids,
        "bundle_preview": preview,
        "trading_capability": "TRADING_DISABLED",
        "execution_side_effects": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "apply"))
    parser.add_argument("--plan-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        plan = _plan(args.plan_file)
        value = (
            {
                "status": "PLANNED",
                "actor_identity": ACTOR,
                "plan_digest": base._canonical_digest(plan),
                "window_payload": window_payload(plan),
                "window_payload_digest": base._canonical_digest(window_payload(plan)),
                "trading_capability": "TRADING_DISABLED",
                "execution_side_effects": 0,
            }
            if args.command == "plan"
            else apply(plan)
        )
    except (base.MarketWindowRolloutBlocked, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason_code": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
