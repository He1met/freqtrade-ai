from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from app.schemas.audit_log import GovernanceEvent, GovernanceEventStatus, GovernanceEventType


class GovernanceEventArchiveRepository:
    """Local JSONL archive for governance events, not production audit storage."""

    def __init__(self, archive_path: Path) -> None:
        self.archive_path = archive_path

    def append(self, event: GovernanceEvent) -> GovernanceEvent:
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        with self.archive_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(event.to_archive_record(), sort_keys=True, separators=(",", ":"))
            )
            handle.write("\n")
        return event

    def list(
        self,
        status: Optional[GovernanceEventStatus] = None,
        event_type: Optional[GovernanceEventType] = None,
        source_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[GovernanceEvent]:
        events: list[GovernanceEvent] = []
        if not self.archive_path.exists():
            return events

        with self.archive_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                clean = line.strip()
                if not clean:
                    continue
                payload = json.loads(clean)
                payload.pop("event_hash", None)
                event = GovernanceEvent.model_validate(payload)
                if status is not None and event.status != status:
                    continue
                if event_type is not None and event.event_type != event_type:
                    continue
                if source_name is not None and event.source.name != source_name:
                    continue
                events.append(event)

        if limit is not None:
            return events[-limit:]
        return events

    def get(self, event_id: str) -> Optional[GovernanceEvent]:
        for event in self.list():
            if event.event_id == event_id:
                return event
        return None
