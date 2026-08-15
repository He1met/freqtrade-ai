#!/usr/bin/env python3
"""Plan or apply one public-only market refresh through the canonical API."""

from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID


API_ROOT = "http://127.0.0.1:8011/api/canonical-v13"


class CanonicalFreshMarketCommandBlocked(RuntimeError):
    pass


def _post(path: str, body: dict[str, object]) -> dict[str, object]:
    request = Request(
        API_ROOT + path,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise CanonicalFreshMarketCommandBlocked(
            f"BLOCKED_CANONICAL_API_HTTP_{exc.code}:{detail}"
        ) from exc
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise CanonicalFreshMarketCommandBlocked(
            "BLOCKED_CANONICAL_API_UNAVAILABLE"
        ) from exc
    if not isinstance(payload, dict):
        raise CanonicalFreshMarketCommandBlocked("BLOCKED_CANONICAL_API_RESPONSE")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply"))
    parser.add_argument("--target-snapshot-id", type=UUID, required=True)
    parser.add_argument("--target-snapshot-digest", required=True)
    parser.add_argument("--window-snapshot-id", type=UUID, required=True)
    parser.add_argument("--window-snapshot-digest", required=True)
    parser.add_argument("--target-key", required=True)
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--profile-key")
    parser.add_argument("--scope-key")
    args = parser.parse_args(argv)
    body: dict[str, object] = {
        "target_snapshot_id": str(args.target_snapshot_id),
        "target_snapshot_digest": args.target_snapshot_digest,
        "window_snapshot_id": str(args.window_snapshot_id),
        "window_snapshot_digest": args.window_snapshot_digest,
        "target_key": args.target_key,
    }
    if args.command == "apply":
        if not all((args.expected_plan_digest, args.profile_key, args.scope_key)):
            parser.error(
                "apply requires --expected-plan-digest, --profile-key, and --scope-key"
            )
        body.update(
            {
                "expected_plan_digest": args.expected_plan_digest,
                "profile_key": args.profile_key,
                "scope_key": args.scope_key,
            }
        )
    try:
        result = _post(f"/market-data/acquisitions/{args.command}", body)
    except CanonicalFreshMarketCommandBlocked as exc:
        print(
            json.dumps(
                {"status": "BLOCKED", "reason_code": str(exc).split(":", 1)[0]}
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
