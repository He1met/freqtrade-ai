#!/usr/bin/env python3
"""Acquire and register one fresh OKX public candle artifact in canonical V1.3."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from canonical_v13_api_service import (
    CanonicalServiceBlocked,
    canonical_control_database_url,
    require_release_checkout,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.canonical_v13.fresh_market_rollout import (  # noqa: E402
    CanonicalFreshMarketRolloutBlocked,
    acquire_register_and_seal_fresh_market,
)
from app.canonical_v13.market_acquisition import (  # noqa: E402
    CanonicalMarketAcquisitionBlocked,
)
from app.canonical_v13.market_planning import (  # noqa: E402
    CanonicalMarketPlanningBlocked,
    plan_fresh_market_acquisition,
)
from app.canonical_v13.okx_public_market import (  # noqa: E402
    OkxPublicHistoryCandleDownloader,
)


class CanonicalFreshMarketCommandBlocked(RuntimeError):
    pass


def apply(args: argparse.Namespace) -> dict[str, object]:
    require_release_checkout()
    artifact_root = args.artifact_root.resolve(strict=True)
    if not artifact_root.is_dir() or artifact_root.is_symlink():
        raise CanonicalFreshMarketCommandBlocked("BLOCKED_MARKET_ARTIFACT_ROOT")
    engine = create_engine(canonical_control_database_url(), pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            plan = plan_fresh_market_acquisition(
                connection,
                target_snapshot_id=args.target_snapshot_id,
                expected_target_snapshot_digest=args.target_snapshot_digest,
                window_snapshot_id=args.window_snapshot_id,
                expected_window_snapshot_digest=args.window_snapshot_digest,
                target_key=args.target_key,
            )
            result = acquire_register_and_seal_fresh_market(
                connection,
                plan=plan,
                downloader=OkxPublicHistoryCandleDownloader(),
                artifact_root=artifact_root,
                observed_at=datetime.now(timezone.utc),
                profile_key=args.profile_key,
                scope_key=args.scope_key,
                inspector_identity="canonical-v13-okx-public-inspector-v1",
            )
    finally:
        engine.dispose()
    return {
        "status": "ACCEPTED",
        "source": "OKX_PUBLIC_MARKET_DATA_ONLY",
        "credential_access": "NONE",
        "target_key": args.target_key,
        "target_snapshot_id": str(args.target_snapshot_id),
        "window_snapshot_id": str(args.window_snapshot_id),
        "market_profile_version_id": str(result.market_profile_version_id),
        "artifact_id": str(result.artifact_id),
        "artifact_locator": result.artifact_locator,
        "artifact_digest": result.artifact_digest,
        "receipt_id": str(result.receipt_id),
        "market_snapshot_id": str(result.market_snapshot_id),
        "market_snapshot_digest": result.market_snapshot_digest,
        "artifact_file_replay": result.artifact_file_replay,
        "database_replay": result.database_replay,
        "trading_capability": "TRADING_DISABLED",
        "execution_side_effects": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--target-snapshot-id", type=UUID, required=True)
    parser.add_argument("--target-snapshot-digest", required=True)
    parser.add_argument("--window-snapshot-id", type=UUID, required=True)
    parser.add_argument("--window-snapshot-digest", required=True)
    parser.add_argument("--target-key", required=True)
    parser.add_argument("--profile-key", required=True)
    parser.add_argument("--scope-key", required=True)
    args = parser.parse_args(argv)
    try:
        payload = apply(args)
    except (
        CanonicalFreshMarketCommandBlocked,
        CanonicalFreshMarketRolloutBlocked,
        CanonicalMarketAcquisitionBlocked,
        CanonicalMarketPlanningBlocked,
        CanonicalServiceBlocked,
        SQLAlchemyError,
        OSError,
    ) as exc:
        code = getattr(exc, "code", None) or str(exc).split(":", 1)[0]
        print(json.dumps({"status": "BLOCKED", "reason_code": code}, sort_keys=True))
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
