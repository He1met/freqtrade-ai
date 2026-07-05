from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import uuid4

from app.core.config import REPO_ROOT
from app.repositories.audit_log import GovernanceEventArchiveRepository
from app.schemas.audit_log import (
    GovernanceArtifactLink,
    GovernanceEvent,
    GovernanceEventActor,
    GovernanceEventSource,
    GovernanceEventStatus,
    GovernanceEventType,
)


class GovernanceAuditLogService:
    """Records local governance events without external audit storage or runtime control."""

    def __init__(
        self,
        repository: Optional[GovernanceEventArchiveRepository] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._repository = repository or GovernanceEventArchiveRepository(
            REPO_ROOT / "reports" / "governance" / "governance-events.jsonl"
        )
        self._now_provider = now_provider

    def record_event(
        self,
        event_type: GovernanceEventType,
        status: GovernanceEventStatus,
        actor: GovernanceEventActor | dict[str, Any],
        source: GovernanceEventSource | dict[str, Any],
        summary: str,
        reason: Optional[str] = None,
        artifact_links: Optional[list[GovernanceArtifactLink | dict[str, Any]]] = None,
        payload: Optional[dict[str, Any]] = None,
        tags: Optional[list[str]] = None,
        event_id: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> GovernanceEvent:
        event = GovernanceEvent(
            event_id=event_id or f"governance-event-{uuid4().hex}",
            event_type=event_type,
            status=status,
            actor=actor,
            source=source,
            created_at=created_at or self._now(),
            summary=summary,
            reason=reason,
            artifact_links=artifact_links or [],
            payload=payload or {},
            tags=tags or [],
        )
        return self._repository.append(event)

    def query_events(
        self,
        status: Optional[GovernanceEventStatus] = None,
        event_type: Optional[GovernanceEventType] = None,
        source_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[GovernanceEvent]:
        return self._repository.list(
            status=status,
            event_type=event_type,
            source_name=source_name,
            limit=limit,
        )

    def get_event(self, event_id: str) -> Optional[GovernanceEvent]:
        return self._repository.get(event_id)

    def _now(self) -> datetime:
        if self._now_provider is not None:
            return self._now_provider()
        return datetime.now(timezone.utc)
