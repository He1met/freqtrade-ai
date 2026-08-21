#!/usr/bin/env python3
"""Render or verify canonical V1.3 PostgreSQL bootstrap evidence."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re
import sys
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.canonical_v13.bootstrap import (
    LOCAL_DATABASE_NAME,
    LOCAL_RESEARCH_SERVICE_PRINCIPALS,
    LOCAL_PHASE9_SERVICE_PRINCIPALS,
    LOCAL_RUNTIME_SERVICE_PRINCIPALS,
    LOCAL_ROLE_PREFIX,
    LOCAL_SERVICE_PRINCIPALS,
    expected_postgresql_owner_table_grants,
    local_legacy_research_writer_role,
    local_role_mapping,
    postgresql_owner_table_grants,
    verify_postgresql_bootstrap,
)
from app.canonical_v13.authority_upgrade import (
    CanonicalAuthorityUpgradeBlocked,
    RESEARCH_AUTHORITY_TABLES,
    apply_authority_upgrade,
    render_authority_upgrade_plan,
    rollback_authority_upgrade,
    verify_authority_upgrade_state,
)
from app.canonical_v13.gate_receipt_upgrade import (
    CanonicalGateReceiptUpgradeBlocked,
    apply_gate_receipt_upgrade,
    verify_gate_receipt_upgrade,
)
from app.canonical_v13.phase9_readiness import (
    PHASE9_ACCEPTANCE_STAGES,
    CanonicalPhase9ReadinessBlocked,
    Phase9QualificationHandoff,
    inspect_phase9_readiness,
)
from app.canonical_v13.phase9_schema_upgrade import (
    CanonicalPhase9SchemaUpgradeBlocked,
    apply_phase9_schema_upgrade,
    rollback_phase9_schema_upgrade,
    verify_phase9_schema_upgrade,
)
from app.canonical_v13.genesis import (
    assert_postgresql_acl_sql,
    postgresql_owner_table_grant_statements,
    render_postgresql_acl_sql,
    render_postgresql_owner_sql,
)
from app.canonical_v13.manifest import (
    CANONICAL_MANIFEST_DIGEST,
    TABLE_MANIFEST_BY_NAME,
)
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    CANONICAL_TABLES,
    QUALIFICATION_DECISIONS_TABLE,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


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
    require_phase9_principals: bool = False,
) -> dict[str, object]:
    mapping = local_role_mapping()
    service_principals = dict(LOCAL_SERVICE_PRINCIPALS)
    if require_research_principals:
        service_principals.update(LOCAL_RESEARCH_SERVICE_PRINCIPALS)
    if require_phase9_principals:
        service_principals.update(LOCAL_RESEARCH_SERVICE_PRINCIPALS)
        service_principals.update(LOCAL_PHASE9_SERVICE_PRINCIPALS)
        service_principals.update(LOCAL_RUNTIME_SERVICE_PRINCIPALS)
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
        "require_phase9_principals": require_phase9_principals,
        "capability_role_count": result.capability_role_count,
        "explicit_acl_count": result.explicit_acl_count,
    }


def authority_plan() -> dict[str, object]:
    return render_authority_upgrade_plan(
        role_mapping=local_role_mapping(),
        legacy_research_writer_role=local_legacy_research_writer_role(),
    )


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    return value


def _legacy_owner_table_grants(
    mapping: CanonicalRoleMapping,
) -> frozenset[tuple[str, str, str]]:
    owner = mapping.physical("canonical_schema_owner")
    return frozenset(
        (owner, "schema_metadata", privilege)
        for privilege in TABLE_MANIFEST_BY_NAME["schema_metadata"].writer_privileges
    )


def owner_table_acl_plan() -> dict[str, object]:
    mapping = local_role_mapping()
    statements = postgresql_owner_table_grant_statements(mapping)
    return {
        "status": "READY",
        "contract": "canonical-v13-owner-table-acl-v1",
        "database_name": LOCAL_DATABASE_NAME,
        "manifest_digest": CANONICAL_MANIFEST_DIGEST,
        "role_mapping_digest": mapping.mapping_digest,
        "owner_role": mapping.physical("canonical_schema_owner"),
        "table_statement_count": len(statements),
        "target_privilege_fact_count": len(
            expected_postgresql_owner_table_grants(mapping)
        ),
        "legacy_privilege_fact_count": len(_legacy_owner_table_grants(mapping)),
        "owner_acl_digest": sha256(
            (";\n".join(statements) + ";\n").encode("utf-8")
        ).hexdigest(),
        "destructive_table_operations": [],
        "requires_zero_research_rows": True,
    }


def owner_table_acl_repair() -> dict[str, object]:
    mapping = local_role_mapping()
    plan = owner_table_acl_plan()
    statements = postgresql_owner_table_grant_statements(mapping)
    target = expected_postgresql_owner_table_grants(mapping)
    legacy = _legacy_owner_table_grants(mapping)
    service_principals = dict(LOCAL_SERVICE_PRINCIPALS)
    service_principals.update(LOCAL_RESEARCH_SERVICE_PRINCIPALS)
    allowed_research_memberships = frozenset(
        (mapping.physical(logical_role), principal)
        for principal, logical_role in LOCAL_RESEARCH_SERVICE_PRINCIPALS.items()
    )
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            base = verify_postgresql_bootstrap(
                connection,
                role_mapping=mapping,
                require_zero_business_rows=False,
                service_principals=service_principals,
                require_owner_table_grants=False,
            )
            if not base.accepted:
                raise BootstrapBlocked(
                    "BLOCKED_OWNER_TABLE_ACL_REPAIR_BASELINE: "
                    + "; ".join(base.problems)
                )
            authority = verify_authority_upgrade_state(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=local_legacy_research_writer_role(),
                require_no_research_rows=True,
                allowed_isolated_memberships=allowed_research_memberships,
            )
            if not authority.accepted or authority.state != "CURRENT":
                raise BootstrapBlocked(
                    "BLOCKED_OWNER_TABLE_ACL_REPAIR_AUTHORITY: "
                    f"state={authority.state}; " + "; ".join(authority.problems)
                )
            actual = postgresql_owner_table_grants(
                connection,
                role_mapping=mapping,
            )
            if actual == target:
                return {**plan, "status": "NO_OP_ALREADY_CURRENT"}
            if actual != legacy:
                raise BootstrapBlocked(
                    "BLOCKED_OWNER_TABLE_ACL_REPAIR_PARTIAL: "
                    f"observed_privilege_fact_count={len(actual)}"
                )
            research_row_count = sum(
                int(
                    connection.execute(
                        select(func.count()).select_from(CANONICAL_TABLES[table])
                    ).scalar_one()
                )
                for table in RESEARCH_AUTHORITY_TABLES
            )
            if research_row_count:
                raise BootstrapBlocked("BLOCKED_OWNER_TABLE_ACL_REPAIR_RESEARCH_ROWS")
            for statement in statements:
                connection.exec_driver_sql(statement)

            applied_at = datetime.now(timezone.utc)
            evidence = {
                "contract": plan["contract"],
                "manifest_digest": plan["manifest_digest"],
                "role_mapping_digest": plan["role_mapping_digest"],
                "owner_role": plan["owner_role"],
                "table_statement_count": plan["table_statement_count"],
                "target_privilege_fact_count": plan["target_privilege_fact_count"],
                "owner_acl_digest": plan["owner_acl_digest"],
                "verified_research_row_count": research_row_count,
                "actor_identity": _upgrade_actor(),
                "applied_at": applied_at.isoformat(),
                "destructive_table_operations": [],
            }
            request_digest = _digest({**evidence, "applied_at": None})
            aggregate_id = str(plan["contract"])
            receipt_digest = _digest(
                {
                    "aggregate_id": aggregate_id,
                    "event_type": "CANONICAL_OWNER_TABLE_ACL_REPAIRED",
                    "request_digest": request_digest,
                    "applied_at": applied_at.isoformat(),
                }
            )
            connection.execute(
                AUDIT_EVENTS_TABLE.insert().values(
                    id=uuid4(),
                    event_type="CANONICAL_OWNER_TABLE_ACL_REPAIRED",
                    aggregate_type="canonical_owner_table_acl_repair",
                    aggregate_id=aggregate_id,
                    actor_identity=evidence["actor_identity"],
                    request_digest=request_digest,
                    receipt_digest=receipt_digest,
                    evidence_json=evidence,
                    created_at=applied_at,
                )
            )
            after = verify_postgresql_bootstrap(
                connection,
                role_mapping=mapping,
                require_zero_business_rows=False,
                service_principals=service_principals,
            )
            if not after.accepted:
                raise BootstrapBlocked(
                    "BLOCKED_OWNER_TABLE_ACL_REPAIR_POSTVERIFY: "
                    + "; ".join(after.problems)
                )
            authority_after = verify_authority_upgrade_state(
                connection,
                role_mapping=mapping,
                legacy_research_writer_role=local_legacy_research_writer_role(),
                require_no_research_rows=True,
                allowed_isolated_memberships=allowed_research_memberships,
            )
            if not authority_after.accepted or authority_after.state != "CURRENT":
                raise BootstrapBlocked(
                    "BLOCKED_OWNER_TABLE_ACL_REPAIR_POSTAUTHORITY: "
                    f"state={authority_after.state}; "
                    + "; ".join(authority_after.problems)
                )
    finally:
        engine.dispose()
    return {
        **plan,
        "status": "REPAIRED",
        "request_digest": request_digest,
        "receipt_digest": receipt_digest,
        "research_row_count": research_row_count,
    }


def authority_verify(*, restore_database_name: str | None = None) -> dict[str, object]:
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


def gate_receipt_upgrade(*, apply: bool) -> dict[str, object]:
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        context = engine.begin() if apply else engine.connect()
        with context as connection:
            result = (
                apply_gate_receipt_upgrade(
                    connection,
                    role_mapping=local_role_mapping(),
                    actor_identity=_upgrade_actor(),
                )
                if apply
                else verify_gate_receipt_upgrade(connection)
            )
    finally:
        engine.dispose()
    return {**asdict(result), "status": result.status}


def phase9_readiness(
    *,
    stage: str,
    qualification_decision_id: UUID,
    strategy_version_id: UUID,
    configuration_bundle_id: UUID,
    market_snapshot_id: UUID,
) -> dict[str, object]:
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                decision = (
                    connection.execute(
                        select(QUALIFICATION_DECISIONS_TABLE).where(
                            QUALIFICATION_DECISIONS_TABLE.c.id
                            == qualification_decision_id
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if decision is None:
                    raise CanonicalPhase9ReadinessBlocked(
                        "EXACT_QUALIFICATION_DECISION_NOT_FOUND",
                        str(qualification_decision_id),
                    )
                if (
                    decision["strategy_version_id"] != strategy_version_id
                    or decision["configuration_bundle_id"] != configuration_bundle_id
                    or decision["market_snapshot_id"] != market_snapshot_id
                ):
                    raise CanonicalPhase9ReadinessBlocked(
                        "EXACT_QUALIFICATION_HANDOFF_ID_MISMATCH",
                        "explicit Phase 9 handoff IDs differ from the decision",
                    )
                handoff = Phase9QualificationHandoff(
                    qualification_decision_id=decision["id"],
                    qualification_decision_digest=decision["decision_digest"],
                    strategy_version_id=decision["strategy_version_id"],
                    research_target_id=decision["research_target_id"],
                    configuration_bundle_id=decision["configuration_bundle_id"],
                    configuration_bundle_digest=decision["configuration_bundle_digest"],
                    market_snapshot_id=decision["market_snapshot_id"],
                    market_snapshot_digest=decision["market_snapshot_digest"],
                    validation_plan_id=decision["validation_plan_id"],
                    validation_plan_digest=decision["validation_plan_digest"],
                )
                result = inspect_phase9_readiness(
                    connection, qualification_handoff=handoff, stage=stage
                )
    finally:
        engine.dispose()
    return _json_safe(asdict(result))


def phase9_schema(*, operation: str) -> dict[str, object]:
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        if operation == "verify":
            with engine.connect() as connection:
                with connection.begin():
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    result = verify_phase9_schema_upgrade(connection)
        else:
            actor_identity = _upgrade_actor()
            with engine.begin() as connection:
                result = (
                    apply_phase9_schema_upgrade(
                        connection,
                        actor_identity=actor_identity,
                        role_mapping=local_role_mapping(),
                    )
                    if operation == "apply"
                    else rollback_phase9_schema_upgrade(
                        connection, actor_identity=actor_identity
                    )
                )
    finally:
        engine.dispose()
    payload = asdict(result)
    if operation != "verify":
        payload["actor_identity"] = actor_identity
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "render",
            "verify",
            "verify-current",
            "verify-research-provisioned",
            "verify-phase9-provisioned",
            "authority-plan",
            "owner-table-acl-plan",
            "owner-table-acl-repair",
            "authority-verify",
            "authority-verify-restore",
            "authority-apply",
            "authority-rollback",
            "gate-receipts-verify",
            "gate-receipts-apply",
            "phase9-readiness",
            "phase9-schema-verify",
            "phase9-schema-apply",
            "phase9-schema-rollback",
        ),
    )
    parser.add_argument(
        "--stage",
        choices=PHASE9_ACCEPTANCE_STAGES,
        default="QUALIFICATION_HANDOFF",
    )
    parser.add_argument("--qualification-decision-id", type=UUID)
    parser.add_argument("--strategy-version-id", type=UUID)
    parser.add_argument("--configuration-bundle-id", type=UUID)
    parser.add_argument("--market-snapshot-id", type=UUID)
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            payload = render_plan()
        elif args.command in {
            "verify",
            "verify-current",
            "verify-research-provisioned",
            "verify-phase9-provisioned",
        }:
            payload = verify(
                require_zero_business_rows=args.command == "verify",
                require_research_principals=(
                    args.command
                    in {
                        "verify-research-provisioned",
                        "verify-phase9-provisioned",
                    }
                ),
                require_phase9_principals=(args.command == "verify-phase9-provisioned"),
            )
        elif args.command == "authority-plan":
            payload = authority_plan()
        elif args.command == "owner-table-acl-plan":
            payload = owner_table_acl_plan()
        elif args.command == "owner-table-acl-repair":
            payload = owner_table_acl_repair()
        elif args.command in {"authority-verify", "authority-verify-restore"}:
            payload = authority_verify(
                restore_database_name=(
                    _restore_database_name()
                    if args.command == "authority-verify-restore"
                    else None
                )
            )
        elif args.command in {"gate-receipts-verify", "gate-receipts-apply"}:
            payload = gate_receipt_upgrade(apply=args.command == "gate-receipts-apply")
        elif args.command == "phase9-readiness":
            handoff_ids = (
                args.qualification_decision_id,
                args.strategy_version_id,
                args.configuration_bundle_id,
                args.market_snapshot_id,
            )
            if any(value is None for value in handoff_ids):
                raise BootstrapBlocked("BLOCKED_EXACT_PHASE9_HANDOFF_IDS_REQUIRED")
            payload = phase9_readiness(
                stage=args.stage,
                qualification_decision_id=args.qualification_decision_id,
                strategy_version_id=args.strategy_version_id,
                configuration_bundle_id=args.configuration_bundle_id,
                market_snapshot_id=args.market_snapshot_id,
            )
        elif args.command.startswith("phase9-schema-"):
            payload = phase9_schema(
                operation=args.command.removeprefix("phase9-schema-")
            )
        else:
            payload = authority_apply(rollback=args.command == "authority-rollback")
    except BootstrapBlocked as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
    except CanonicalAuthorityUpgradeBlocked as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
    except CanonicalGateReceiptUpgradeBlocked as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
    except CanonicalPhase9ReadinessBlocked as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
    except CanonicalPhase9SchemaUpgradeBlocked as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
    except (SQLAlchemyError, ValueError):
        payload = {"status": "BLOCKED", "reason": "bootstrap verification failed"}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return (
        0
        if payload["status"]
        in {
            "READY",
            "ACCEPTED",
            "UPGRADED",
            "REPAIRED",
            "ROLLED_BACK",
            "NO_OP_ALREADY_CURRENT",
        }
        else 2
    )


if __name__ == "__main__":
    sys.exit(main())
