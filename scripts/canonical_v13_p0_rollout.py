#!/usr/bin/env python3
"""Plan or idempotently apply the initial no-trade canonical P0 contract."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_ORIGIN = "http://127.0.0.1:8011"
API_ROOT = API_ORIGIN + "/api/canonical-v13"
ACTOR = "canonical-v13-p0-rollout"
SCOPE = "production-research-v13"
WORKFLOW = "research"
SCHEMA = {"type": "object", "additionalProperties": False}
ADAPTER_IDENTITY = "canonical-v13-p0-contract-v1"
ADAPTER_DIGEST = sha256(ADAPTER_IDENTITY.encode()).hexdigest()
ADAPTER_MANIFEST_DIGEST = sha256(
    b"canonical-v13-p0-contract-manifest-v1"
).hexdigest()
KINDS = (
    "TARGET",
    "WINDOW",
    "GENERATION",
    "DIVERSITY",
    "QUALITY_QUALIFICATION",
    "SCORING",
    "RESEARCH_AGGREGATE",
)


class P0RolloutBlocked(RuntimeError):
    """Fail-closed rollout error."""


def _canonical_digest(value: object) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def p0_payloads() -> dict[str, dict[str, Any]]:
    """Return the frozen initial contract without environment-derived defaults."""

    return {
        "TARGET": {
            "targets": [
                {
                    "target_key": "btc-usdt-swap-15m",
                    "instrument": "BTC-USDT-SWAP",
                    "pair": "BTC/USDT:USDT",
                    "timeframe": "15m",
                    "data_kind": "futures",
                }
            ]
        },
        "WINDOW": {
            "windows": [
                {
                    "window_key": "required-recent-30d",
                    "required": True,
                    "start_at": "2026-07-15T00:00:00Z",
                    "end_at": "2026-08-14T00:00:00Z",
                    "coverage": {"minimum_closed_candles": 2880},
                }
            ]
        },
        "GENERATION": {
            "allocations": [
                {
                    "target_key": "btc-usdt-swap-15m",
                    "allocation_count": 6,
                    "candidate_cap": 6,
                }
            ]
        },
        "DIVERSITY": {
            "rules": [
                {
                    "rule_key": "code-digest-unique",
                    "algorithm": "exact-duplicate-count-v1",
                    "metric": "code_digest_duplicate_count",
                    "operator": "==",
                    "threshold": 0,
                },
                {
                    "rule_key": "strategy-family-unique",
                    "algorithm": "exact-duplicate-count-v1",
                    "metric": "strategy_family_duplicate_count",
                    "operator": "==",
                    "threshold": 0,
                },
                {
                    "rule_key": "target-window-combination-unique",
                    "algorithm": "exact-duplicate-count-v1",
                    "metric": "target_window_duplicate_count",
                    "operator": "==",
                    "threshold": 0,
                },
            ]
        },
        "QUALITY_QUALIFICATION": {
            "minimum_score": 50,
            "required_window_gates": [
                {
                    "gate_key": "minimum-trades",
                    "metric": "trade_count",
                    "operator": ">=",
                    "threshold": 30,
                },
                {
                    "gate_key": "positive-net-return-after-cost",
                    "metric": "net_return_after_cost",
                    "operator": ">",
                    "threshold": 0,
                },
                {
                    "gate_key": "maximum-drawdown-cap",
                    "metric": "maximum_drawdown",
                    "operator": "<=",
                    "threshold": 0.15,
                },
                {
                    "gate_key": "minimum-fee-assumption",
                    "metric": "fee_rate",
                    "operator": ">=",
                    "threshold": 0.0005,
                },
                {
                    "gate_key": "minimum-slippage-assumption",
                    "metric": "slippage_rate",
                    "operator": ">=",
                    "threshold": 0.0002,
                },
                {
                    "gate_key": "lookahead-pass",
                    "metric": "lookahead_failure_count",
                    "operator": "==",
                    "threshold": 0,
                },
            ],
        },
        "SCORING": {
            "window_aggregation": "MEAN",
            "components": [
                {
                    "component_key": "profit",
                    "metric": "net_return_after_cost",
                    "weight": 0.35,
                    "direction": "maximize",
                    "minimum": 0,
                    "maximum": 1,
                },
                {
                    "component_key": "risk",
                    "metric": "maximum_drawdown",
                    "weight": 0.25,
                    "direction": "minimize",
                    "minimum": 0,
                    "maximum": 0.15,
                },
                {
                    "component_key": "stability",
                    "metric": "return_stability",
                    "weight": 0.15,
                    "direction": "maximize",
                    "minimum": 0,
                    "maximum": 1,
                },
                {
                    "component_key": "quality",
                    "metric": "quality_gate_pass_ratio",
                    "weight": 0.25,
                    "direction": "maximize",
                    "minimum": 0,
                    "maximum": 1,
                },
            ],
        },
        "RESEARCH_AGGREGATE": {
            "assembly_key": (
                "required-windows-hard-gates-overall-score-50-"
                "mandatory-validations-v1"
            )
        },
    }


def _request(path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = Request(
        API_ROOT + path,
        data=(
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        ),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise P0RolloutBlocked(f"BLOCKED_CANONICAL_API_HTTP_{exc.code}:{detail}") from exc
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise P0RolloutBlocked("BLOCKED_CANONICAL_API_UNAVAILABLE") from exc
    if not isinstance(payload, dict):
        raise P0RolloutBlocked("BLOCKED_CANONICAL_API_RESPONSE")
    return payload


def _dependencies(
    kind: str, versions: dict[str, str]
) -> list[dict[str, str]]:
    if kind == "GENERATION":
        selected = ("TARGET",)
    elif kind == "RESEARCH_AGGREGATE":
        selected = KINDS[:6]
    else:
        selected = ()
    return [
        {
            "version_id": versions[item],
            "expected_kind": item,
            "relation_key": f"snapshot:{item.lower()}",
        }
        for item in selected
    ]


def apply() -> dict[str, Any]:
    try:
        with urlopen(API_ORIGIN + "/healthz", timeout=5) as response:
            health = json.loads(response.read())
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise P0RolloutBlocked("BLOCKED_CANONICAL_API_UNAVAILABLE") from exc
    if health.get("status") != "HEALTHY" or health.get("trading_capability") != "TRADING_DISABLED":
        raise P0RolloutBlocked("BLOCKED_CANONICAL_API_NOT_NO_TRADE")
    payloads = p0_payloads()
    versions: dict[str, str] = {}
    snapshots: dict[str, str] = {}
    receipts: dict[str, dict[str, str]] = {}
    for kind in KINDS:
        slug = kind.lower().replace("_", "-")
        draft = _request(
            f"/configurations/{kind}/drafts",
            body={
                "actor_identity": ACTOR,
                "idempotency_key": f"initial-p0-{slug}-draft-v1",
                "profile_key": f"production-v13-{slug}",
                "scope_key": SCOPE,
                "workflow_key": WORKFLOW,
                "schema_json": SCHEMA,
                "payload_json": payloads[kind],
                "adapter_identity": ADAPTER_IDENTITY,
                "adapter_digest": ADAPTER_DIGEST,
                "dependencies": _dependencies(kind, versions),
            },
        )
        versions[kind] = str(draft["version_id"])
        validated = _request(
            f"/configurations/{kind}/{versions[kind]}/validate",
            body={
                "actor_identity": ACTOR,
                "idempotency_key": f"initial-p0-{slug}-validate-v1",
                "adapter_manifest_digest": ADAPTER_MANIFEST_DIGEST,
            },
        )
        snapshots[kind] = str(validated["snapshot_id"])
        receipts[kind] = {
            "draft": str(draft["receipt_digest"]),
            "validate": str(validated["receipt_digest"]),
        }
    preview = _request(
        "/research-bundles/preview",
        body={
            "scope_key": SCOPE,
            "workflow_key": WORKFLOW,
            "snapshot_ids": snapshots,
            "market_snapshot_id": None,
        },
    )
    if preview.get("status") != "BLOCKED" or "MARKET_SNAPSHOT_UNSET" not in preview.get(
        "reason_codes", []
    ):
        raise P0RolloutBlocked("BLOCKED_UNEXPECTED_P0_PREVIEW_STATE")
    return {
        "status": "P0_SNAPSHOTS_VALIDATED_BUNDLE_BLOCKED",
        "actor_identity": ACTOR,
        "scope_key": SCOPE,
        "workflow_key": WORKFLOW,
        "payload_digest": _canonical_digest(payloads),
        "versions": versions,
        "snapshots": snapshots,
        "receipts": receipts,
        "bundle_preview": preview,
        "trading_capability": "TRADING_DISABLED",
        "execution_side_effects": 0,
    }


def plan() -> dict[str, Any]:
    payloads = p0_payloads()
    return {
        "status": "PLANNED",
        "api_root": API_ROOT,
        "actor_identity": ACTOR,
        "scope_key": SCOPE,
        "workflow_key": WORKFLOW,
        "kinds": list(KINDS),
        "payload_digest": _canonical_digest(payloads),
        "payloads": payloads,
        "trading_capability": "TRADING_DISABLED",
        "execution_side_effects": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply"), default="plan", nargs="?")
    args = parser.parse_args(argv)
    try:
        result = apply() if args.command == "apply" else plan()
    except P0RolloutBlocked as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
