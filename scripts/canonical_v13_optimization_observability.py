#!/usr/bin/env python3
"""Verify or apply the canonical optimization observability upgrade."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from sqlalchemy import create_engine


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.canonical_v13.bootstrap import local_role_mapping  # noqa: E402
from app.canonical_v13.optimization_observability_upgrade import (  # noqa: E402
    CanonicalOptimizationObservabilityUpgradeBlocked,
    apply_optimization_observability_upgrade,
    verify_optimization_observability_upgrade,
)


DATABASE_URL_ENV = "FREQTRADE_AI_CANONICAL_V13_PROVISIONER_DATABASE_URL"


def _engine():
    url = os.environ.get(DATABASE_URL_ENV, "")
    if not url.startswith("postgresql+psycopg://"):
        raise CanonicalOptimizationObservabilityUpgradeBlocked(
            f"BLOCKED_OPTIMIZATION_OBSERVABILITY_DATABASE:{DATABASE_URL_ENV}"
        )
    return create_engine(url, pool_pre_ping=True)


def run(command: str) -> dict[str, object]:
    engine = _engine()
    try:
        if command == "verify":
            with engine.connect() as connection:
                with connection.begin():
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    result = verify_optimization_observability_upgrade(connection)
        else:
            with engine.begin() as connection:
                result = apply_optimization_observability_upgrade(
                    connection, role_mapping=local_role_mapping()
                )
    finally:
        engine.dispose()
    return asdict(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "apply"))
    args = parser.parse_args(argv)
    try:
        payload = run(args.command)
    except CanonicalOptimizationObservabilityUpgradeBlocked as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0 if payload["status"] in {"PREVIOUS_READY", "ACCEPTED", "UPGRADED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
