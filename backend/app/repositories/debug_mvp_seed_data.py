from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.debug_mvp_seed import DebugMvpSeedPayload


class DebugMvpSeedDataRepository:
    """Persists local-only MVP debug payloads keyed by frontend endpoint group."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert_payloads(self, payloads: dict[str, Any]) -> int:
        for endpoint_key, payload in payloads.items():
            record = self.db.get(DebugMvpSeedPayload, endpoint_key)
            if record is None:
                self.db.add(DebugMvpSeedPayload(endpoint_key=endpoint_key, payload=payload))
            else:
                record.payload = payload
        self.db.commit()
        return len(payloads)

    def get_payload(self, endpoint_key: str) -> Optional[Any]:
        record = self.db.get(DebugMvpSeedPayload, endpoint_key)
        return None if record is None else record.payload
