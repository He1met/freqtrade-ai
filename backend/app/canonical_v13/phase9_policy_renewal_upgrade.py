"""Reversible PostgreSQL upgrade for append-only Phase 9 policy renewal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from hashlib import sha256
import json
from typing import Final

from sqlalchemy import Connection, inspect, select, text

from app.canonical_v13.execution_common import canonical_execution_digest
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
)
from app.canonical_v13.models import (
    EXECUTION_CANARY_RISK_POLICIES_TABLE,
    EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE,
    SCHEMA_METADATA_TABLE,
)


CONTRACT: Final = "canonical-v13-phase9-policy-renewal-upgrade-v1"
OLD_QUALIFICATION_UNIQUE: Final = (
    "uq_execution_canary_risk_policies_qualification_decision_id"
)
OLD_APPROVAL_UNIQUE: Final = (
    "uq_execution_canary_risk_policies_deployment_approval_id"
)
ACTIVE_QUALIFICATION_UNIQUE: Final = (
    "execution_canary_risk_policies_active_qualification_unique"
)
ACTIVE_APPROVAL_UNIQUE: Final = (
    "execution_canary_risk_policies_active_approval_unique"
)


class CanonicalPhase9PolicyRenewalUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Phase9PolicyRenewalUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    active_indexes_present: tuple[str, ...]
    old_constraints_present: tuple[str, ...]
    active_policy_count: int
    expired_policy_count: int
    backfilled_policy_count: int
    policy_lineage_digest: str
    repeat_noop: bool
    receipt_digest: str


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _manifest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise CanonicalPhase9PolicyRenewalUpgradeBlocked(
            "BLOCKED_PHASE9_POLICY_RENEWAL_SCHEMA_METADATA"
        )
    return value


def _constraint_names(connection: Connection) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(item["name"])
            for item in inspect(connection).get_unique_constraints(
                "execution_canary_risk_policies", schema=CANONICAL_BUSINESS_SCHEMA
            )
            if item.get("name") in {OLD_QUALIFICATION_UNIQUE, OLD_APPROVAL_UNIQUE}
        )
    )


def _index_names(connection: Connection) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(item["name"])
            for item in inspect(connection).get_indexes(
                "execution_canary_risk_policies", schema=CANONICAL_BUSINESS_SCHEMA
            )
            if item.get("name")
            in {ACTIVE_QUALIFICATION_UNIQUE, ACTIVE_APPROVAL_UNIQUE}
            and item.get("unique") is True
        )
    )


def _lineage_digest(connection: Connection) -> str:
    rows = connection.execute(
        text(
            f"SELECT id, qualification_decision_id, deployment_approval_id, "
            f"idempotency_key, status, expires_at, terminated_at, receipt_digest, "
            f"termination_digest FROM {CANONICAL_BUSINESS_SCHEMA}."
            "execution_canary_risk_policies ORDER BY accepted_at, id"
        )
    ).mappings()
    return _digest(
        [
            {
                key: str(value) if value is not None else None
                for key, value in row.items()
            }
            for row in rows
        ]
    )


def _result(
    connection: Connection,
    *,
    status: str,
    repeat_noop: bool,
    backfilled_policy_count: int = 0,
) -> Phase9PolicyRenewalUpgradeResult:
    counts = dict(
        connection.execute(
            text(
                f"SELECT status, count(*) FROM {CANONICAL_BUSINESS_SCHEMA}."
                "execution_canary_risk_policies GROUP BY status"
            )
        ).all()
    )
    payload = {
        "contract": CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "active_indexes_present": _index_names(connection),
        "old_constraints_present": _constraint_names(connection),
        "active_policy_count": int(counts.get("ACTIVE", 0)),
        "expired_policy_count": int(counts.get("EXPIRED", 0)),
        "backfilled_policy_count": backfilled_policy_count,
        "policy_lineage_digest": _lineage_digest(connection),
        "repeat_noop": repeat_noop,
    }
    return Phase9PolicyRenewalUpgradeResult(**payload, receipt_digest=_digest(payload))


def verify_phase9_policy_renewal_upgrade(
    connection: Connection,
) -> Phase9PolicyRenewalUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalPhase9PolicyRenewalUpgradeBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    indexes = _index_names(connection)
    constraints = _constraint_names(connection)
    if not indexes and len(constraints) == 2:
        return _result(connection, status="PREVIOUS_READY", repeat_noop=True)
    problems: list[str] = []
    if set(indexes) != {ACTIVE_QUALIFICATION_UNIQUE, ACTIVE_APPROVAL_UNIQUE}:
        problems.append(f"active_indexes={list(indexes)}")
    if constraints:
        problems.append(f"old_constraints={list(constraints)}")
    duplicate_active = connection.execute(
        text(
            "SELECT count(*) FROM ("
            f"SELECT qualification_decision_id FROM {CANONICAL_BUSINESS_SCHEMA}."
            "execution_canary_risk_policies WHERE status='ACTIVE' "
            "GROUP BY qualification_decision_id HAVING count(*)>1 UNION ALL "
            f"SELECT deployment_approval_id FROM {CANONICAL_BUSINESS_SCHEMA}."
            "execution_canary_risk_policies WHERE status='ACTIVE' "
            "GROUP BY deployment_approval_id HAVING count(*)>1) duplicates"
        )
    ).scalar_one()
    if int(duplicate_active):
        problems.append(f"duplicate_active={duplicate_active}")
    incomplete_terminal = connection.execute(
        text(
            f"SELECT count(*) FROM {CANONICAL_BUSINESS_SCHEMA}."
            "execution_canary_risk_policies WHERE status IN ('EXPIRED','TERMINATED') "
            "AND (terminated_at IS NULL OR termination_digest IS NULL)"
        )
    ).scalar_one()
    if int(incomplete_terminal):
        problems.append(f"incomplete_terminal={incomplete_terminal}")
    if _manifest(connection) != CANONICAL_MANIFEST_DIGEST:
        problems.append("manifest_digest_mismatch")
    if problems:
        raise CanonicalPhase9PolicyRenewalUpgradeBlocked(
            "BLOCKED_PARTIAL_PHASE9_POLICY_RENEWAL_UPGRADE: " + "; ".join(problems)
        )
    return _result(connection, status="ACCEPTED", repeat_noop=True)


def apply_phase9_policy_renewal_upgrade(
    connection: Connection,
) -> Phase9PolicyRenewalUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1308202608240902}
    )
    before = verify_phase9_policy_renewal_upgrade(connection)
    if before.status == "ACCEPTED":
        return before
    schema = CANONICAL_BUSINESS_SCHEMA
    expired_rows = (
        connection.execute(
            text(
                f"SELECT policy.* FROM {schema}.execution_canary_risk_policies policy "
                "WHERE policy.status='ACTIVE' AND policy.expires_at <= now() "
                "ORDER BY policy.accepted_at FOR UPDATE"
            )
        )
        .mappings()
        .all()
    )
    for row in expired_rows:
        budget = connection.execute(
            select(EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c.id).where(
                EXECUTION_RISK_BUDGET_AUTHORIZATIONS_TABLE.c
                .execution_canary_risk_policy_id
                == row["id"]
            )
        ).scalar_one_or_none()
        if budget is not None:
            raise CanonicalPhase9PolicyRenewalUpgradeBlocked(
                "BLOCKED_EXPIRED_POLICY_HAS_DOWNSTREAM_AUTHORITY"
            )
        expired_at = row["expires_at"]
        if expired_at.tzinfo is None:
            expired_at = expired_at.replace(tzinfo=timezone.utc)
        payload = {
            "contract": "canonical-v13-canary-risk-policy-expiration-v1",
            "policy_id": str(row["id"]),
            "policy_receipt_digest": row["receipt_digest"],
            "reason_code": "POLICY_TTL_EXPIRED_BEFORE_BUDGET",
            "expired_at": expired_at.astimezone(timezone.utc).isoformat(),
        }
        connection.execute(
            EXECUTION_CANARY_RISK_POLICIES_TABLE.update()
            .where(
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.id == row["id"],
                EXECUTION_CANARY_RISK_POLICIES_TABLE.c.status == "ACTIVE",
            )
            .values(
                status="EXPIRED",
                terminated_at=expired_at,
                termination_digest=canonical_execution_digest(payload),
            )
        )
    for name in (OLD_QUALIFICATION_UNIQUE, OLD_APPROVAL_UNIQUE):
        connection.execute(
            text(
                f"ALTER TABLE {schema}.execution_canary_risk_policies "
                f"DROP CONSTRAINT {name}"
            )
        )
    connection.execute(
        text(
            f"CREATE UNIQUE INDEX {ACTIVE_QUALIFICATION_UNIQUE} ON {schema}."
            "execution_canary_risk_policies (qualification_decision_id) "
            "WHERE status='ACTIVE'"
        )
    )
    connection.execute(
        text(
            f"CREATE UNIQUE INDEX {ACTIVE_APPROVAL_UNIQUE} ON {schema}."
            "execution_canary_risk_policies (deployment_approval_id) "
            "WHERE status='ACTIVE'"
        )
    )
    verify_phase9_policy_renewal_upgrade(connection)
    return _result(
        connection,
        status="UPGRADED",
        repeat_noop=False,
        backfilled_policy_count=len(expired_rows),
    )


def rollback_phase9_policy_renewal_upgrade(
    connection: Connection,
) -> Phase9PolicyRenewalUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1308202608240902}
    )
    before = verify_phase9_policy_renewal_upgrade(connection)
    if before.status == "PREVIOUS_READY":
        return before
    schema = CANONICAL_BUSINESS_SCHEMA
    duplicates = connection.execute(
        text(
            "SELECT count(*) FROM ("
            f"SELECT qualification_decision_id FROM {schema}."
            "execution_canary_risk_policies "
            "GROUP BY qualification_decision_id HAVING count(*)>1 UNION ALL "
            f"SELECT deployment_approval_id FROM {schema}."
            "execution_canary_risk_policies "
            "GROUP BY deployment_approval_id HAVING count(*)>1) duplicates"
        )
    ).scalar_one()
    if int(duplicates):
        raise CanonicalPhase9PolicyRenewalUpgradeBlocked(
            "BLOCKED_POLICY_RENEWAL_ROLLBACK_MULTIPLE_HISTORY"
        )
    connection.execute(text(f"DROP INDEX {schema}.{ACTIVE_QUALIFICATION_UNIQUE}"))
    connection.execute(text(f"DROP INDEX {schema}.{ACTIVE_APPROVAL_UNIQUE}"))
    connection.execute(
        text(
            f"ALTER TABLE {schema}.execution_canary_risk_policies "
            f"ADD CONSTRAINT {OLD_QUALIFICATION_UNIQUE} "
            "UNIQUE (qualification_decision_id), "
            f"ADD CONSTRAINT {OLD_APPROVAL_UNIQUE} UNIQUE (deployment_approval_id)"
        )
    )
    return _result(connection, status="ROLLED_BACK", repeat_noop=False)


__all__ = [
    "ACTIVE_APPROVAL_UNIQUE",
    "ACTIVE_QUALIFICATION_UNIQUE",
    "CanonicalPhase9PolicyRenewalUpgradeBlocked",
    "Phase9PolicyRenewalUpgradeResult",
    "apply_phase9_policy_renewal_upgrade",
    "rollback_phase9_policy_renewal_upgrade",
    "verify_phase9_policy_renewal_upgrade",
]
