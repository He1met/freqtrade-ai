from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.canonical_v13 import api as canonical_api
from app.canonical_v13.api import API_PREFIX, create_canonical_v13_app
from app.canonical_v13.fresh_market_rollout import FreshMarketRolloutResult
from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.market_planning import (
    FreshMarketPlan,
    fresh_market_plan_digest,
)


def test_market_plan_digest_must_be_reviewed_before_public_only_apply(
    monkeypatch, tmp_path
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        install_canonical_genesis(connection, installer_identity="market-api-test")

    @contextmanager
    def connection_factory():
        with engine.connect() as connection:
            yield connection

    target_snapshot_id = uuid4()
    window_snapshot_id = uuid4()
    research_target_id = uuid4()
    plan = FreshMarketPlan(
        target_snapshot_id=target_snapshot_id,
        target_snapshot_digest="a" * 64,
        window_snapshot_id=window_snapshot_id,
        window_snapshot_digest="b" * 64,
        research_target_id=research_target_id,
        target_key="btc-usdt-swap-15m",
        instrument="BTC-USDT-SWAP",
        pair="BTC/USDT:USDT",
        timeframe="15m",
        data_kind="futures",
        requested_start=datetime(2026, 7, 11, tzinfo=timezone.utc),
        requested_end=datetime(2026, 8, 14, tzinfo=timezone.utc),
        interval=timedelta(minutes=15),
        minimum_closed_candles=2880,
        warmup_closed_candles=400,
        integrity_margin_closed_candles=8,
        freshness_max_age_seconds=3600,
    )
    calls = {"acquire": 0}

    def fake_plan(*_args, **_kwargs):
        return plan

    def fake_acquire(*_args, **_kwargs):
        calls["acquire"] += 1
        return FreshMarketRolloutResult(
            market_profile_version_id=uuid4(),
            artifact_id=uuid4(),
            receipt_id=uuid4(),
            market_snapshot_id=uuid4(),
            market_snapshot_digest="c" * 64,
            artifact_locator="canonical_v13/okx-public/artifact.jsonl",
            artifact_digest="d" * 64,
            artifact_file_replay=False,
            database_replay=False,
            exchange_metadata_artifact_id=uuid4(),
            exchange_metadata_receipt_id=uuid4(),
            exchange_metadata_locator="canonical_v13/okx-public/metadata.json",
            exchange_metadata_digest="f" * 64,
            exchange_metadata_receipt_digest="1" * 64,
        )

    monkeypatch.setattr(canonical_api, "plan_fresh_market_acquisition", fake_plan)
    monkeypatch.setattr(
        canonical_api, "acquire_register_and_seal_fresh_market", fake_acquire
    )
    app = create_canonical_v13_app(
        reader_connection_factory=connection_factory,
        control_connection_factory=connection_factory,
        market_artifact_root=tmp_path,
        market_downloader_factory=lambda: object(),
        exchange_metadata_downloader_factory=lambda: object(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    command = {
        "target_snapshot_id": str(target_snapshot_id),
        "target_snapshot_digest": "a" * 64,
        "window_snapshot_id": str(window_snapshot_id),
        "window_snapshot_digest": "b" * 64,
        "target_key": "btc-usdt-swap-15m",
    }

    planned = client.post(
        f"{API_PREFIX}/market-data/acquisitions/plan", json=command
    )
    assert planned.status_code == 200
    planned_payload = planned.json()
    assert planned_payload["plan_digest"] == fresh_market_plan_digest(plan)
    assert planned_payload["source"] == "OKX_PUBLIC_MARKET_DATA_ONLY"
    assert planned_payload["credential_access"] == "NONE"
    assert planned_payload["execution_side_effects"] == 0

    drifted = client.post(
        f"{API_PREFIX}/market-data/acquisitions/apply",
        json={
            **command,
            "expected_plan_digest": "e" * 64,
            "profile_key": "production-v13-okx-public-btc-usdt-swap-15m",
            "scope_key": "production-research-v13",
        },
    )
    assert drifted.status_code == 409
    assert drifted.json()["error"]["code"] == "BLOCKED_MARKET_PLAN_DIGEST_DRIFT"
    assert calls["acquire"] == 0

    accepted = client.post(
        f"{API_PREFIX}/market-data/acquisitions/apply",
        json={
            **command,
            "expected_plan_digest": planned_payload["plan_digest"],
            "profile_key": "production-v13-okx-public-btc-usdt-swap-15m",
            "scope_key": "production-research-v13",
        },
    )
    assert accepted.status_code == 201
    assert accepted.json()["source"] == "OKX_PUBLIC_MARKET_DATA_ONLY"
    assert accepted.json()["credential_access"] == "NONE"
    assert accepted.json()["trading_capability"] == "TRADING_DISABLED"
    assert accepted.json()["execution_side_effects"] == 0
    assert calls["acquire"] == 1
