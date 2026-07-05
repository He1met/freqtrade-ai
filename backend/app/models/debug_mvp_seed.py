from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class DebugMvpSeedPayload(Base):
    """Stores local-only seeded payloads for frontend API debugging."""

    __tablename__ = "debug_mvp_seed_payloads"

    endpoint_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
