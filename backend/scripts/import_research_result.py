#!/usr/bin/env python3
"""Plan or apply one research_result.json import."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.canonical_v13.bootstrap import local_role_mapping
from app.canonical_v13.research_catalog import (
    ResearchCatalogBlocked,
    apply_research_import,
    load_research_result,
    plan_research_import,
)
from app.canonical_v13.research_catalog_upgrade import (
    ResearchCatalogUpgradeBlocked,
    apply_research_catalog_upgrade,
    verify_research_catalog_upgrade,
)


DATABASE_URL_ENV = "FREQTRADE_AI_CANONICAL_V13_PROVISIONER_DATABASE_URL"


def _database_url() -> str:
    raw = os.environ.get(DATABASE_URL_ENV, "")
    if not raw:
        raise ResearchCatalogBlocked(f"BLOCKED_DATABASE_URL_UNSET: {DATABASE_URL_ENV}")
    parsed = make_url(raw)
    if parsed.drivername != "postgresql+psycopg" or not parsed.database:
        raise ResearchCatalogBlocked("BLOCKED_POSTGRESQL_DATABASE_URL_REQUIRED")
    return raw


def run(command: str, input_path: Path) -> dict[str, object]:
    result = load_research_result(input_path)
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        if command == "plan":
            with engine.connect() as connection:
                verify_research_catalog_upgrade(
                    connection, role_mapping=local_role_mapping()
                )
                plan = plan_research_import(connection, result)
                connection.rollback()
                payload = asdict(plan)
                payload.update(
                    mode="PLAN",
                    database_name=engine.url.database,
                    committed=False,
                )
                return payload
        with engine.begin() as connection:
            plan = plan_research_import(connection, result)
            if plan.action == "CREATE_TABLE_AND_INSERT":
                apply_research_catalog_upgrade(
                    connection, role_mapping=local_role_mapping()
                )
            else:
                verify_research_catalog_upgrade(
                    connection, role_mapping=local_role_mapping()
                )
            applied = apply_research_import(connection, result)
            payload = asdict(applied)
            payload.update(mode="APPLY", database_name=engine.url.database)
            return payload
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply"))
    parser.add_argument(
        "--input", type=Path, default=Path("research_result.json")
    )
    args = parser.parse_args(argv)
    try:
        payload = run(args.command, args.input)
    except (ResearchCatalogBlocked, ResearchCatalogUpgradeBlocked) as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
        print(json.dumps(payload, sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
