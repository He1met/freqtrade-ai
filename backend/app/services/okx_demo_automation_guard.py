from __future__ import annotations

import hashlib
from typing import Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.risk_chain import canonical_digest


class OkxDemoAutomationGuard:
    """Narrow runtime client for the owner-controlled Demo automation functions."""

    @staticmethod
    def policy_digest() -> str:
        policy = get_settings().demo_automation_policy.demo_risk_policy.model_dump(
            mode="json"
        )
        policy.pop("schema_version", None)
        return canonical_digest(policy)

    @classmethod
    def opening_allowed(cls, db: Session) -> bool:
        if not hasattr(db, "get_bind") or db.get_bind().dialect.name != "postgresql":
            return False
        return bool(
            db.execute(
                text("SELECT okx_demo_continuous_opening_allowed(:digest)"),
                {"digest": cls.policy_digest()},
            ).scalar_one()
        )

    @classmethod
    def claim_dispatch(cls, db: Session, *, approved_execution_id: int) -> bool:
        if not hasattr(db, "get_bind") or db.get_bind().dialect.name != "postgresql":
            return False
        return bool(
            db.execute(
                text("SELECT claim_okx_demo_continuous_dispatch(:approval,:digest)"),
                {"approval": approved_execution_id, "digest": cls.policy_digest()},
            ).scalar_one()
        )

    @classmethod
    def record_health(cls, db: Session, *, reconciliation_run_id: int) -> str:
        if not hasattr(db, "get_bind") or db.get_bind().dialect.name != "postgresql":
            return "BLOCKED"
        return str(
            db.execute(
                text("SELECT record_okx_demo_automation_health(:run,:digest)"),
                {"run": reconciliation_run_id, "digest": cls.policy_digest()},
            ).scalar_one()
        )

    @classmethod
    def record_failure(
        cls,
        db: Session,
        *,
        failure_class: str,
        reconciliation_run_id: Optional[int] = None,
        identity: Optional[str] = None,
    ) -> str:
        if not hasattr(db, "get_bind") or db.get_bind().dialect.name != "postgresql":
            return "BLOCKED"
        event_key = hashlib.sha256(
            "|".join(
                (
                    failure_class,
                    identity or uuid4().hex,
                    str(reconciliation_run_id or 0),
                )
            ).encode("utf-8")
        ).hexdigest()
        return str(
            db.execute(
                text(
                    "SELECT record_okx_demo_automation_failure("
                    ":failure,:event_key,:run,:digest)"
                ),
                {
                    "failure": failure_class,
                    "event_key": event_key,
                    "run": reconciliation_run_id,
                    "digest": cls.policy_digest(),
                },
            ).scalar_one()
        )
