"""One-shot, exact-lineage research execution authorization receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import AUDIT_EVENTS_TABLE
from app.canonical_v13.research_validation import ResearchLineage


AUTHORIZATION_EVENT = "RESEARCH_EXECUTION_AUTHORIZED"
CONSUMED_EVENT = "RESEARCH_EXECUTION_AUTHORIZATION_CONSUMED"
REVOKED_EVENT = "RESEARCH_EXECUTION_AUTHORIZATION_REVOKED"


class CanonicalResearchAuthorizationBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ResearchExecutionAuthorization:
    authorization_id: UUID
    lineage: ResearchLineage
    validation_plan_id: UUID
    validation_plan_digest: str
    actor_identity: str
    purpose: str
    request_digest: str
    receipt_digest: str
    authorized_at: datetime
    expires_at: datetime
    one_shot: bool = True
    environment_class: str = "ISOLATED_TEST"


@dataclass(frozen=True)
class ResearchAuthorizationConsumption:
    authorization_id: UUID
    consumption_id: UUID
    attempt_id: UUID
    lineage: ResearchLineage
    validation_plan_id: UUID
    validation_plan_digest: str
    actor_identity: str
    authorization_receipt_digest: str
    request_digest: str
    receipt_digest: str
    consumed_at: datetime
    environment_class: str


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


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise CanonicalResearchAuthorizationBlocked(
            "BLOCKED_AUTHORIZATION_TIMEZONE_UNSET", "timestamps require timezone"
        )
    return value.astimezone(timezone.utc)


def _effective(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _require_canonical(connection: Connection) -> Connection:
    effective = _effective(connection)
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalResearchAuthorizationBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    return effective


def _lineage(lineage: ResearchLineage) -> dict[str, str]:
    return {
        "strategy_version_id": str(lineage.strategy_version_id),
        "research_target_id": str(lineage.research_target_id),
        "configuration_bundle_id": str(lineage.configuration_bundle_id),
        "configuration_bundle_digest": lineage.configuration_bundle_digest,
        "market_snapshot_id": str(lineage.market_snapshot_id),
        "market_snapshot_digest": lineage.market_snapshot_digest,
    }


def _authorization_evidence(
    *,
    authorization_id: UUID,
    lineage: ResearchLineage,
    validation_plan_id: UUID,
    validation_plan_digest: str,
    actor_identity: str,
    purpose: str,
    authorized_at: datetime,
    expires_at: datetime,
    environment_class: str,
) -> dict[str, object]:
    return {
        "contract": "canonical-v13-one-shot-research-authorization-v1",
        "authorization_id": str(authorization_id),
        "lineage": _lineage(lineage),
        "validation_plan_id": str(validation_plan_id),
        "validation_plan_digest": validation_plan_digest,
        "actor_identity": actor_identity,
        "purpose": purpose,
        "authorized_at": authorized_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "one_shot": True,
        "environment_class": environment_class,
        "capabilities": {
            "network": "NONE",
            "credential_mounts": [],
            "exchange": [],
            "order": [],
            "writer": [],
        },
    }


def authorize_research_execution(
    connection: Connection,
    *,
    lineage: ResearchLineage,
    validation_plan_id: UUID,
    validation_plan_digest: str,
    actor_identity: str,
    purpose: str,
    authorized_at: datetime,
    expires_at: datetime,
    environment_class: str = "ISOLATED_TEST",
) -> ResearchExecutionAuthorization:
    """Create an immutable one-shot grant. No version boolean is mutated."""

    if not actor_identity or not purpose:
        raise CanonicalResearchAuthorizationBlocked(
            "BLOCKED_AUTHORIZATION_ACTOR_UNSET", "actor and purpose are required"
        )
    if environment_class not in {"ISOLATED_TEST", "PRODUCTION_RESEARCH"}:
        raise CanonicalResearchAuthorizationBlocked(
            "BLOCKED_AUTHORIZATION_ENVIRONMENT", "environment class is unknown"
        )
    authorized_at = _utc(authorized_at)
    expires_at = _utc(expires_at)
    if expires_at <= authorized_at:
        raise CanonicalResearchAuthorizationBlocked(
            "BLOCKED_AUTHORIZATION_EXPIRY", "authorization must expire in the future"
        )
    effective = _require_canonical(connection)
    authorization_id = uuid4()
    evidence = _authorization_evidence(
        authorization_id=authorization_id,
        lineage=lineage,
        validation_plan_id=validation_plan_id,
        validation_plan_digest=validation_plan_digest,
        actor_identity=actor_identity,
        purpose=purpose,
        authorized_at=authorized_at,
        expires_at=expires_at,
        environment_class=environment_class,
    )
    request_digest = _digest({**evidence, "authorization_id": None})
    receipt_digest = _digest(
        {"authorization_id": str(authorization_id), "request_digest": request_digest}
    )
    effective.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=authorization_id,
            event_type=AUTHORIZATION_EVENT,
            aggregate_type="research_execution_authorization",
            aggregate_id=str(authorization_id),
            actor_identity=actor_identity,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            evidence_json=evidence,
            created_at=authorized_at,
        )
    )
    return ResearchExecutionAuthorization(
        authorization_id=authorization_id,
        lineage=lineage,
        validation_plan_id=validation_plan_id,
        validation_plan_digest=validation_plan_digest,
        actor_identity=actor_identity,
        purpose=purpose,
        request_digest=request_digest,
        receipt_digest=receipt_digest,
        authorized_at=authorized_at,
        expires_at=expires_at,
        environment_class=environment_class,
    )


def _persisted_authorization(
    connection: Connection, authorization_id: UUID, *, lock: bool = False
) -> Mapping[str, object]:
    statement = select(AUDIT_EVENTS_TABLE).where(
            AUDIT_EVENTS_TABLE.c.id == authorization_id,
            AUDIT_EVENTS_TABLE.c.event_type == AUTHORIZATION_EVENT,
        )
    if lock and connection.dialect.name != "sqlite":
        statement = statement.with_for_update()
    row = connection.execute(statement).mappings().one_or_none()
    if row is None:
        raise CanonicalResearchAuthorizationBlocked(
            "BLOCKED_EXECUTION_AUTHORIZATION_UNSET", str(authorization_id)
        )
    evidence = row["evidence_json"]
    if (
        _digest({**evidence, "authorization_id": None}) != row["request_digest"]
        or _digest(
            {
                "authorization_id": str(authorization_id),
                "request_digest": row["request_digest"],
            }
        )
        != row["receipt_digest"]
    ):
        raise CanonicalResearchAuthorizationBlocked(
            "BLOCKED_EXECUTION_AUTHORIZATION_DIGEST_DRIFT",
            "authorization audit receipt drifted",
        )
    return row


def consume_research_execution_authorization(
    connection: Connection,
    *,
    authorization_id: UUID,
    expected_lineage: ResearchLineage,
    validation_plan_id: UUID,
    validation_plan_digest: str,
    attempt_id: UUID,
    actor_identity: str,
    consumed_at: datetime,
) -> ResearchAuthorizationConsumption:
    """Consume one exact grant once, immediately before the isolated attempt."""

    consumed_at = _utc(consumed_at)
    effective = _require_canonical(connection)
    # Serialize consume against revoke on PostgreSQL.  The partial unique index
    # on authorization audit events is the final duplicate-consumption guard.
    authorization = _persisted_authorization(
        effective, authorization_id, lock=True
    )
    evidence = authorization["evidence_json"]
    expected = _authorization_evidence(
        authorization_id=authorization_id,
        lineage=expected_lineage,
        validation_plan_id=validation_plan_id,
        validation_plan_digest=validation_plan_digest,
        actor_identity=evidence["actor_identity"],
        purpose=evidence["purpose"],
        authorized_at=datetime.fromisoformat(evidence["authorized_at"]),
        expires_at=datetime.fromisoformat(evidence["expires_at"]),
        environment_class=evidence["environment_class"],
    )
    if evidence != expected:
        raise CanonicalResearchAuthorizationBlocked(
            "BLOCKED_EXECUTION_AUTHORIZATION_LINEAGE",
            "authorization does not match the requested execution lineage",
        )
    if consumed_at > datetime.fromisoformat(evidence["expires_at"]):
        raise CanonicalResearchAuthorizationBlocked(
            "BLOCKED_EXECUTION_AUTHORIZATION_EXPIRED", "authorization expired"
        )
    terminal = effective.execute(
        select(AUDIT_EVENTS_TABLE.c.event_type).where(
            AUDIT_EVENTS_TABLE.c.aggregate_type
            == "research_execution_authorization",
            AUDIT_EVENTS_TABLE.c.aggregate_id == str(authorization_id),
            AUDIT_EVENTS_TABLE.c.event_type.in_((CONSUMED_EVENT, REVOKED_EVENT)),
        )
    ).scalar_one_or_none()
    if terminal is not None:
        code = (
            "BLOCKED_EXECUTION_AUTHORIZATION_ALREADY_CONSUMED"
            if terminal == CONSUMED_EVENT
            else "BLOCKED_EXECUTION_AUTHORIZATION_REVOKED"
        )
        raise CanonicalResearchAuthorizationBlocked(code, str(authorization_id))
    consumption_id = uuid4()
    consumption_evidence = {
        "contract": "canonical-v13-research-authorization-consumption-v1",
        "authorization_id": str(authorization_id),
        "attempt_id": str(attempt_id),
        "lineage": _lineage(expected_lineage),
        "validation_plan_id": str(validation_plan_id),
        "validation_plan_digest": validation_plan_digest,
        "actor_identity": actor_identity,
        "consumed_at": consumed_at.isoformat(),
        "authorization_receipt_digest": authorization["receipt_digest"],
        "environment_class": evidence["environment_class"],
    }
    request_digest = _digest(consumption_evidence)
    receipt_digest = _digest(
        {"consumption_id": str(consumption_id), "request_digest": request_digest}
    )
    effective.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=consumption_id,
            event_type=CONSUMED_EVENT,
            aggregate_type="research_execution_authorization",
            aggregate_id=str(authorization_id),
            actor_identity=actor_identity,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            evidence_json=consumption_evidence,
            created_at=consumed_at,
        )
    )
    return ResearchAuthorizationConsumption(
        authorization_id=authorization_id,
        consumption_id=consumption_id,
        attempt_id=attempt_id,
        lineage=expected_lineage,
        validation_plan_id=validation_plan_id,
        validation_plan_digest=validation_plan_digest,
        actor_identity=actor_identity,
        authorization_receipt_digest=authorization["receipt_digest"],
        request_digest=request_digest,
        receipt_digest=receipt_digest,
        consumed_at=consumed_at,
        environment_class=evidence["environment_class"],
    )


def verify_research_authorization_consumption(
    consumption: ResearchAuthorizationConsumption,
) -> None:
    """Verify the immutable control-plane receipt at the research boundary."""

    evidence = {
        "contract": "canonical-v13-research-authorization-consumption-v1",
        "authorization_id": str(consumption.authorization_id),
        "attempt_id": str(consumption.attempt_id),
        "lineage": _lineage(consumption.lineage),
        "validation_plan_id": str(consumption.validation_plan_id),
        "validation_plan_digest": consumption.validation_plan_digest,
        "actor_identity": consumption.actor_identity,
        "consumed_at": _utc(consumption.consumed_at).isoformat(),
        "authorization_receipt_digest": consumption.authorization_receipt_digest,
        "environment_class": consumption.environment_class,
    }
    request_digest = _digest(evidence)
    receipt_digest = _digest(
        {
            "consumption_id": str(consumption.consumption_id),
            "request_digest": request_digest,
        }
    )
    if (
        request_digest != consumption.request_digest
        or receipt_digest != consumption.receipt_digest
    ):
        raise CanonicalResearchAuthorizationBlocked(
            "BLOCKED_EXECUTION_AUTHORIZATION_DIGEST_DRIFT",
            "authorization consumption receipt drifted",
        )
def revoke_research_execution_authorization(
    connection: Connection,
    *,
    authorization_id: UUID,
    actor_identity: str,
    reason: str,
    revoked_at: datetime,
) -> UUID:
    effective = _require_canonical(connection)
    _persisted_authorization(effective, authorization_id, lock=True)
    terminal = effective.execute(
        select(AUDIT_EVENTS_TABLE.c.id).where(
            AUDIT_EVENTS_TABLE.c.aggregate_id == str(authorization_id),
            AUDIT_EVENTS_TABLE.c.event_type.in_((CONSUMED_EVENT, REVOKED_EVENT)),
        )
    ).scalar_one_or_none()
    if terminal is not None:
        raise CanonicalResearchAuthorizationBlocked(
            "BLOCKED_EXECUTION_AUTHORIZATION_TERMINAL", str(authorization_id)
        )
    revoked_at = _utc(revoked_at)
    event_id = uuid4()
    evidence = {"authorization_id": str(authorization_id), "reason": reason}
    request_digest = _digest(evidence)
    effective.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=event_id,
            event_type=REVOKED_EVENT,
            aggregate_type="research_execution_authorization",
            aggregate_id=str(authorization_id),
            actor_identity=actor_identity,
            request_digest=request_digest,
            receipt_digest=_digest(
                {"event_id": str(event_id), "request_digest": request_digest}
            ),
            evidence_json=evidence,
            created_at=revoked_at,
        )
    )
    return event_id


__all__ = [
    "CanonicalResearchAuthorizationBlocked",
    "ResearchAuthorizationConsumption",
    "ResearchExecutionAuthorization",
    "authorize_research_execution",
    "consume_research_execution_authorization",
    "revoke_research_execution_authorization",
    "verify_research_authorization_consumption",
]
