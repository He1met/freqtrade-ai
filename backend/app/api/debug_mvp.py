from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.debug_mvp_seed_data import DebugMvpSeedDataRepository
from app.schemas.data_source import attach_data_source_to_payload, fixture_source
from app.services.debug_mvp_seed_data import FRONTEND_MVP_ENDPOINT_ALIASES


router = APIRouter(prefix="/api", tags=["debug-mvp-data"])


def read_seeded_debug_mvp_payload(payload_key: str, db: Session) -> Any:
    payload = DebugMvpSeedDataRepository(db).get_payload(payload_key)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Seeded frontend debug data is missing. Run "
                "`python3 scripts/seed_debug_mvp_data.py` with the backend DATABASE_URL."
            ),
        )

    return attach_data_source_to_payload(
        payload,
        fixture_source(
            "backend-seeded-sqlite-debug fixture payload; not core Phase 8 database success"
        ),
    )


def build_seeded_debug_mvp_endpoint(payload_key: str, route_name: str):
    def endpoint(db: Session = Depends(get_db)) -> Any:
        return read_seeded_debug_mvp_payload(payload_key, db)

    endpoint.__name__ = route_name
    return endpoint


for endpoint_path, payload_key in FRONTEND_MVP_ENDPOINT_ALIASES.items():
    route_suffix = endpoint_path.strip("/").replace("/", "_").replace("-", "_")
    route_name = f"read_seeded_debug_mvp_{payload_key}_{route_suffix}"
    router.add_api_route(
        endpoint_path,
        build_seeded_debug_mvp_endpoint(payload_key, route_name),
        methods=["GET"],
        name=route_name,
    )
