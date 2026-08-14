#!/usr/bin/env python3
"""Render or verify canonical V1.3 PostgreSQL bootstrap evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.canonical_v13.bootstrap import (
    LOCAL_DATABASE_NAME,
    LOCAL_ROLE_PREFIX,
    LOCAL_SERVICE_PRINCIPALS,
    local_role_mapping,
    verify_postgresql_bootstrap,
)
from app.canonical_v13.genesis import (
    assert_postgresql_acl_sql,
    render_postgresql_acl_sql,
    render_postgresql_owner_sql,
)


DATABASE_URL_ENV = "FREQTRADE_AI_CANONICAL_V13_PROVISIONER_DATABASE_URL"


class BootstrapBlocked(RuntimeError):
    pass


def _database_url() -> str:
    raw = os.environ.get(DATABASE_URL_ENV, "")
    if not raw:
        raise BootstrapBlocked(f"BLOCKED_DATABASE_URL_UNSET: {DATABASE_URL_ENV}")
    parsed = make_url(raw)
    if parsed.drivername != "postgresql+psycopg":
        raise BootstrapBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    if parsed.database != LOCAL_DATABASE_NAME:
        raise BootstrapBlocked("BLOCKED_CANONICAL_DATABASE_NAME_MISMATCH")
    return raw


def render_plan() -> dict[str, object]:
    mapping = local_role_mapping()
    acl = render_postgresql_acl_sql(mapping)
    assert_postgresql_acl_sql(acl, mapping)
    return {
        "status": "READY",
        "database_name": LOCAL_DATABASE_NAME,
        "role_prefix": LOCAL_ROLE_PREFIX,
        "role_mapping_digest": mapping.mapping_digest,
        "capability_role_count": len(mapping.roles),
        "acl_statement_count": acl.count(";"),
        "owner_statement_count": render_postgresql_owner_sql(mapping).count(";"),
    }


def verify() -> dict[str, object]:
    mapping = local_role_mapping()
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            result = verify_postgresql_bootstrap(
                connection,
                role_mapping=mapping,
                require_zero_business_rows=True,
                service_principals=LOCAL_SERVICE_PRINCIPALS,
            )
    finally:
        engine.dispose()
    return {
        "status": "ACCEPTED" if result.accepted else "BLOCKED",
        "problems": list(result.problems),
        "database_name": LOCAL_DATABASE_NAME,
        "role_mapping_digest": mapping.mapping_digest,
        "table_count": result.table_count,
        "business_row_count": result.business_row_count,
        "capability_role_count": result.capability_role_count,
        "explicit_acl_count": result.explicit_acl_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "verify"))
    args = parser.parse_args(argv)
    try:
        payload = render_plan() if args.command == "render" else verify()
    except BootstrapBlocked as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
    except (SQLAlchemyError, ValueError):
        payload = {"status": "BLOCKED", "reason": "bootstrap verification failed"}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] in {"READY", "ACCEPTED"} else 2


if __name__ == "__main__":
    sys.exit(main())
