#!/usr/bin/env python3
"""Plan or apply an audited no-trade canonical market WINDOW refresh."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_ORIGIN = "http://127.0.0.1:8011"
API_ROOT = API_ORIGIN + "/api/canonical-v13"
ACTOR = "canonical-v13-market-window-rollout"
SCOPE = "production-research-v13"
WORKFLOW = "research"
SCHEMA = {"type": "object", "additionalProperties": False}
ADAPTER_IDENTITY = "canonical-v13-market-window-contract-v1"
ADAPTER_DIGEST = sha256(ADAPTER_IDENTITY.encode()).hexdigest()
ADAPTER_MANIFEST_DIGEST = sha256(
    b"canonical-v13-market-window-contract-manifest-v1"
).hexdigest()
WINDOW_PROFILE = "production-v13-window"
AGGREGATE_PROFILE = "production-v13-research-aggregate"
DEPENDENCY_KINDS = (
    "TARGET",
    "WINDOW",
    "GENERATION",
    "DIVERSITY",
    "QUALITY_QUALIFICATION",
    "SCORING",
)
PROFILE_KEYS = {
    kind: f"production-v13-{kind.lower().replace('_', '-')}"
    for kind in (*DEPENDENCY_KINDS, "RESEARCH_AGGREGATE")
}
ASSEMBLY_KEY = (
    "required-windows-hard-gates-overall-score-50-mandatory-validations-v1"
)


class MarketWindowRolloutBlocked(RuntimeError):
    """Fail-closed market-window rollout error."""


def _canonical_digest(value: object) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_end_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except (TypeError, ValueError) as exc:
        raise MarketWindowRolloutBlocked("BLOCKED_WINDOW_END_AT_INVALID") from exc
    if parsed.tzinfo is None:
        raise MarketWindowRolloutBlocked("BLOCKED_WINDOW_END_AT_TIMEZONE_UNSET")
    normalized = parsed.astimezone(timezone.utc)
    if (
        normalized.minute % 15
        or normalized.second
        or normalized.microsecond
    ):
        raise MarketWindowRolloutBlocked("BLOCKED_WINDOW_END_AT_NOT_15M_ALIGNED")
    return normalized


def window_payload(end_at: str) -> dict[str, Any]:
    """Build the explicit 30-day acquisition contract from a reviewed end time."""

    end = _parse_end_at(end_at)
    start = end - timedelta(days=30)
    return {
        "windows": [
            {
                "window_key": "required-recent-30d",
                "required": True,
                "start_at": _utc_text(start),
                "end_at": _utc_text(end),
                "coverage": {
                    "minimum_closed_candles": 2880,
                    "warmup_closed_candles": 400,
                    "integrity_margin_closed_candles": 8,
                    "freshness_max_age_seconds": 3600,
                },
            }
        ]
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
        raise MarketWindowRolloutBlocked(
            f"BLOCKED_CANONICAL_API_HTTP_{exc.code}:{detail}"
        ) from exc
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise MarketWindowRolloutBlocked("BLOCKED_CANONICAL_API_UNAVAILABLE") from exc
    if not isinstance(payload, dict):
        raise MarketWindowRolloutBlocked("BLOCKED_CANONICAL_API_RESPONSE")
    return payload


def _health() -> dict[str, Any]:
    try:
        with urlopen(API_ORIGIN + "/healthz", timeout=5) as response:
            payload = json.loads(response.read())
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise MarketWindowRolloutBlocked("BLOCKED_CANONICAL_API_UNAVAILABLE") from exc
    if not isinstance(payload, dict):
        raise MarketWindowRolloutBlocked("BLOCKED_CANONICAL_API_RESPONSE")
    return payload


def _profile(catalog: dict[str, Any], kind: str) -> dict[str, Any]:
    items = catalog.get("items")
    if catalog.get("status") != "AVAILABLE" or not isinstance(items, list):
        raise MarketWindowRolloutBlocked("BLOCKED_P0_CONFIGURATION_CATALOG_UNSET")
    matches = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("configuration_kind") == kind
        and item.get("profile_key") == PROFILE_KEYS[kind]
        and item.get("scope_key") == SCOPE
        and item.get("workflow_key") == WORKFLOW
    ]
    if len(matches) != 1:
        raise MarketWindowRolloutBlocked(
            f"BLOCKED_{kind}_PROFILE_AUTHORITY_AMBIGUOUS"
        )
    return matches[0]


def _latest_validated(catalog: dict[str, Any], kind: str) -> dict[str, Any]:
    profile = _profile(catalog, kind)
    versions = profile.get("versions")
    if not isinstance(versions, list) or not versions:
        raise MarketWindowRolloutBlocked(f"BLOCKED_{kind}_VERSION_UNSET")
    try:
        ordered = sorted(versions, key=lambda item: int(item["version_number"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketWindowRolloutBlocked(
            f"BLOCKED_{kind}_VERSION_CATALOG_INVALID"
        ) from exc
    latest = ordered[-1]
    if (
        not isinstance(latest, dict)
        or latest.get("lifecycle_status") != "VALIDATED"
        or not latest.get("version_id")
        or not latest.get("snapshot_id")
        or not latest.get("snapshot_digest")
    ):
        raise MarketWindowRolloutBlocked(
            f"BLOCKED_{kind}_LATEST_VERSION_NOT_VALIDATED"
        )
    return latest


def _idempotency_key(label: str, payload: object) -> str:
    return f"market-window:{label}:{_canonical_digest(payload)}"


def _draft_and_validate(
    request: Callable[..., dict[str, Any]],
    *,
    kind: str,
    profile_key: str,
    payload: dict[str, Any],
    dependencies: list[dict[str, str]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    identity = {
        "kind": kind,
        "profile_key": profile_key,
        "scope_key": SCOPE,
        "workflow_key": WORKFLOW,
        "payload_json": payload,
        "dependencies": dependencies,
    }
    draft = request(
        f"/configurations/{kind}/drafts",
        body={
            "actor_identity": ACTOR,
            "idempotency_key": _idempotency_key(f"{kind.lower()}-draft-v1", identity),
            "profile_key": profile_key,
            "scope_key": SCOPE,
            "workflow_key": WORKFLOW,
            "schema_json": SCHEMA,
            "payload_json": payload,
            "adapter_identity": ADAPTER_IDENTITY,
            "adapter_digest": ADAPTER_DIGEST,
            "dependencies": dependencies,
        },
    )
    if draft.get("configuration_kind") != kind or not draft.get("version_id"):
        raise MarketWindowRolloutBlocked(f"BLOCKED_{kind}_DRAFT_RESPONSE")
    validation_identity = {
        "kind": kind,
        "version_id": str(draft["version_id"]),
        "adapter_manifest_digest": ADAPTER_MANIFEST_DIGEST,
    }
    validated = request(
        f"/configurations/{kind}/{draft['version_id']}/validate",
        body={
            "actor_identity": ACTOR,
            "idempotency_key": _idempotency_key(
                f"{kind.lower()}-validate-v1", validation_identity
            ),
            "adapter_manifest_digest": ADAPTER_MANIFEST_DIGEST,
        },
    )
    if (
        validated.get("configuration_kind") != kind
        or validated.get("version_id") != draft.get("version_id")
        or not validated.get("snapshot_id")
        or not validated.get("snapshot_digest")
    ):
        raise MarketWindowRolloutBlocked(f"BLOCKED_{kind}_VALIDATION_RESPONSE")
    return draft, validated


def apply(end_at: str) -> dict[str, Any]:
    health = _health()
    if (
        health.get("status") != "HEALTHY"
        or health.get("trading_capability") != "TRADING_DISABLED"
    ):
        raise MarketWindowRolloutBlocked("BLOCKED_CANONICAL_API_NOT_NO_TRADE")
    payload = window_payload(end_at)
    catalog = _request("/configurations")
    base_versions = {
        kind: _latest_validated(catalog, kind)
        for kind in DEPENDENCY_KINDS
        if kind != "WINDOW"
    }
    # A pending draft on either mutable profile is a distinct authority and must
    # be reviewed before this command can safely create another version.
    _latest_validated(catalog, "WINDOW")
    _latest_validated(catalog, "RESEARCH_AGGREGATE")

    window_draft, window_snapshot = _draft_and_validate(
        _request,
        kind="WINDOW",
        profile_key=WINDOW_PROFILE,
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
        for kind in DEPENDENCY_KINDS
    ]
    aggregate_draft, aggregate_snapshot = _draft_and_validate(
        _request,
        kind="RESEARCH_AGGREGATE",
        profile_key=AGGREGATE_PROFILE,
        payload={"assembly_key": ASSEMBLY_KEY},
        dependencies=dependencies,
    )
    snapshot_ids = {
        **{kind: str(item["snapshot_id"]) for kind, item in base_versions.items()},
        "WINDOW": str(window_snapshot["snapshot_id"]),
        "RESEARCH_AGGREGATE": str(aggregate_snapshot["snapshot_id"]),
    }
    preview = _request(
        "/research-bundles/preview",
        body={
            "scope_key": SCOPE,
            "workflow_key": WORKFLOW,
            "snapshot_ids": snapshot_ids,
            "market_snapshot_id": None,
        },
    )
    if (
        preview.get("status") != "BLOCKED"
        or "MARKET_SNAPSHOT_UNSET" not in preview.get("reason_codes", [])
        or preview.get("bundle_digest") is not None
        or preview.get("prospective_bundle_id") is not None
    ):
        raise MarketWindowRolloutBlocked("BLOCKED_UNEXPECTED_WINDOW_PREVIEW_STATE")
    return {
        "status": "WINDOW_AND_AGGREGATE_VALIDATED_MARKET_BLOCKED",
        "actor_identity": ACTOR,
        "scope_key": SCOPE,
        "workflow_key": WORKFLOW,
        "end_at": payload["windows"][0]["end_at"],
        "window": {"draft": window_draft, "snapshot": window_snapshot},
        "research_aggregate": {
            "draft": aggregate_draft,
            "snapshot": aggregate_snapshot,
        },
        "snapshot_ids": snapshot_ids,
        "bundle_preview": preview,
        "trading_capability": "TRADING_DISABLED",
        "execution_side_effects": 0,
    }


def plan(end_at: str) -> dict[str, Any]:
    payload = window_payload(end_at)
    return {
        "status": "PLANNED",
        "api_root": API_ROOT,
        "actor_identity": ACTOR,
        "scope_key": SCOPE,
        "workflow_key": WORKFLOW,
        "window_profile_key": WINDOW_PROFILE,
        "aggregate_profile_key": AGGREGATE_PROFILE,
        "window_payload": payload,
        "window_payload_digest": _canonical_digest(payload),
        "aggregate_payload": {"assembly_key": ASSEMBLY_KEY},
        "dependency_kinds": list(DEPENDENCY_KINDS),
        "market_snapshot_id": None,
        "trading_capability": "TRADING_DISABLED",
        "execution_side_effects": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply"), nargs="?", default="plan")
    parser.add_argument(
        "--end-at",
        required=True,
        help="reviewed timezone-aware end bound aligned to a fully closed 15m candle",
    )
    args = parser.parse_args(argv)
    try:
        result = apply(args.end_at) if args.command == "apply" else plan(args.end_at)
    except MarketWindowRolloutBlocked as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
