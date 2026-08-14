#!/usr/bin/env python3
"""Render or verify canonical V1.3 PostgreSQL bootstrap evidence."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
import re
import sys

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.canonical_v13.bootstrap import (
    LOCAL_DATABASE_NAME,
    LOCAL_RESEARCH_SERVICE_PRINCIPALS,
    LOCAL_ROLE_PREFIX,
    LOCAL_SERVICE_PRINCIPALS,
    local_legacy_research_writer_role,
    local_role_mapping,
    verify_postgresql_bootstrap,
)
from app.canonical_v13.authority_upgrade import (
    CanonicalAuthorityUpgradeBlocked,
    apply_authority_upgrade,
    render_authority_upgrade_plan,
    rollback_authority_upgrade,
    verify_authority_upgrade_state,
)
from app.canonical_v13.genesis import (
    assert_postgresql_acl_sql,
    render_postgresql_acl_sql,
    render_postgresql_owner_sql,
)


DATABASE_URL_ENV = "FREQTRADE_AI_CANONICAL_V13_PROVISIONER_DATABASE_URL"
RESTORE_DATABASE_NAME_ENV = "FREQTRADE_AI_CANONICAL_V13_RESTORE_DATABASE_NAME"
UPGRADE_ACTOR_ENV = "FREQTRADE_AI_CANONICAL_V13_UPGRADE_ACTOR"


class BootstrapBlocked(RuntimeError):
    pass


def _database_url(*, expected_database_name: str = LOCAL_DATABASE_NAME) -> str:
    raw = os.environ.get(DATABASE_URL_ENV, "")
    if not raw:
        raise BootstrapBlocked(f"BLOCKED_DATABASE_URL_UNSET: {DATABASE_URL_ENV}")
    parsed = make_url(raw)
    if parsed.drivername != "postgresql+psycopg":
        raise BootstrapBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    if parsed.database != expected_database_name:
        raise BootstrapBlocked("BLOCKED_CANONICAL_DATABASE_NAME_MISMATCH")
    return raw


def _restore_database_name() -> str:
    database_name = os.environ.get(RESTORE_DATABASE_NAME_ENV, "")
    if not database_name:
        raise BootstrapBlocked(
            f"BLOCKED_RESTORE_DATABASE_NAME_UNSET: {RESTORE_DATABASE_NAME_ENV}"
        )
    pattern = rf"{re.escape(LOCAL_DATABASE_NAME)}_restore_[a-z0-9][a-z0-9_]*"
    if re.fullmatch(pattern, database_name) is None:
        raise BootstrapBlocked("BLOCKED_RESTORE_DATABASE_NAME_INVALID")
    return database_name


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


def verify(
    *,
    require_zero_business_rows: bool = True,
    require_research_principals: bool = False,
) -> dict[str, object]:
    mapping = local_role_mapping()
    service_principals = dict(LOCAL_SERVICE_PRINCIPALS)
    if require_research_principals:
        service_principals.update(LOCAL_RESEARCH_SERVICE_PRINCIPALS)
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            result = verify_postgresql_bootstrap(
                connection,
                role_mapping=mapping,
                require_zero_business_rows=require_zero_business_rows,
                service_principals=service_principals,
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
        "require_zero_business_rows": require_zero_business_rows,
        "require_research_principals": require_research_principals,
        "capability_role_count": result.capability_role_count,
        "explicit_acl_count": result.explicit_acl_count,
    }


def authority_plan() -> dict[str, object]:
    return render_authority_upgrade_plan(
        role_mapping=local_role_mapping(),
        legacy_research_writer_role=local_legacy_research_writer_role(),
    )


def authority_verify(
    *, restore_database_name: str | None = None
) -> dict[str, object]:
    expected_database_name = restore_database_name or LOCAL_DATABASE_NAME
    engine = create_engine(
        _database_url(expected_database_name=expected_database_name),
        pool_pre_ping=True,
    )
    try:
        with engine.connect() as connection:
            result = verify_authority_upgrade_state(
                connection,
                role_mapping=local_role_mapping(),
                legacy_research_writer_role=local_legacy_research_writer_role(),
                require_no_research_rows=True,
            )
    finally:
        engine.dispose()
    return {
        **asdict(result),
        "database_name": expected_database_name,
        "verification_scope": (
            "INDEPENDENT_RESTORE" if restore_database_name else "PRODUCTION"
        ),
        "status": "ACCEPTED" if result.accepted else "BLOCKED",
    }


def _upgrade_actor() -> str:
    actor = os.environ.get(UPGRADE_ACTOR_ENV, "")
    if not actor:
        raise BootstrapBlocked(f"BLOCKED_UPGRADE_ACTOR_UNSET: {UPGRADE_ACTOR_ENV}")
    return actor


def authority_apply(*, rollback: bool) -> dict[str, object]:
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            operation = (
                rollback_authority_upgrade if rollback else apply_authority_upgrade
            )
            result = operation(
                connection,
                role_mapping=local_role_mapping(),
                legacy_research_writer_role=local_legacy_research_writer_role(),
                actor_identity=_upgrade_actor(),
            )
    finally:
        engine.dispose()
    return {**asdict(result), "status": result.status}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "render",
            "verify",
            "verify-current",
            "verify-research-provisioned",
            "authority-plan",
            "authority-verify",
            "authority-verify-restore",
            "authority-apply",
            "authority-rollback",
        ),
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            payload = render_plan()
        elif args.command in {
            "verify",
            "verify-current",
            "verify-research-provisioned",
        }:
            payload = verify(
                require_zero_business_rows=args.command == "verify",
                require_research_principals=(
                    args.command == "verify-research-provisioned"
                ),
            )
        elif args.command == "authority-plan":
            payload = authority_plan()
        elif args.command in {"authority-verify", "authority-verify-restore"}:
            payload = authority_verify(
                restore_database_name=(
                    _restore_database_name()
                    if args.command == "authority-verify-restore"
                    else None
                )
            )
        else:
            payload = authority_apply(rollback=args.command == "authority-rollback")
    except BootstrapBlocked as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
    except CanonicalAuthorityUpgradeBlocked as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
    except (SQLAlchemyError, ValueError):
        payload = {"status": "BLOCKED", "reason": "bootstrap verification failed"}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return (
        0
        if payload["status"]
        in {"READY", "ACCEPTED", "UPGRADED", "ROLLED_BACK", "NO_OP_ALREADY_CURRENT"}
        else 2
    )


if __name__ == "__main__":
    sys.exit(main())
