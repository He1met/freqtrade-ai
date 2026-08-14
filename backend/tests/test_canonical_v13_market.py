from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select

from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.market import (
    CanonicalMarketBlocked,
    MarketInspectionFacts,
    accept_market_artifact,
    create_market_profile_draft,
    seal_market_snapshot,
    validate_market_profile,
)
from app.canonical_v13.models import (
    CONFIGURATION_SNAPSHOTS_TABLE,
    MARKET_ARTIFACTS_TABLE,
    MARKET_INSPECTIONS_TABLE,
    MARKET_RECEIPTS_TABLE,
    MARKET_SNAPSHOTS_TABLE,
    MARKET_SNAPSHOT_MEMBERS_TABLE,
    RESEARCH_TARGETS_TABLE,
)


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    raw = engine.connect()
    with raw.begin():
        install_canonical_genesis(raw, installer_identity="phase3-market-test")
    connection = raw.execution_options(
        schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
    )
    return engine, raw, connection


def _facts(**overrides):
    values = dict(
        row_count=100,
        first_open_at=NOW,
        last_close_at=NOW + timedelta(hours=10),
        gap_count=0,
        duplicate_count=0,
        null_count=0,
        monotonic=True,
    )
    values.update(overrides)
    return MarketInspectionFacts(**values)


def _count(connection, table):
    return connection.execute(select(func.count()).select_from(table)).scalar_one()


def _seed_target(connection):
    snapshot_id = uuid4()
    target_id = uuid4()
    connection.execute(
        CONFIGURATION_SNAPSHOTS_TABLE.insert().values(
            id=snapshot_id,
            configuration_version_id=uuid4(),
            configuration_kind="TARGET",
            schema_digest="1" * 64,
            payload_digest="2" * 64,
            dependency_digest="3" * 64,
            adapter_manifest_digest="4" * 64,
            snapshot_digest="5" * 64,
            snapshot_json={},
            created_at=NOW,
        )
    )
    connection.execute(
        RESEARCH_TARGETS_TABLE.insert().values(
            id=target_id,
            target_snapshot_id=snapshot_id,
            target_key="test-target",
            instrument="TEST-USDT-SWAP",
            pair="TEST/USDT:USDT",
            timeframe="5m",
            data_kind="futures",
            target_digest="6" * 64,
        )
    )
    return target_id


def test_profile_evidence_and_snapshot_are_idempotent_and_exact():
    engine, raw, connection = _db()
    try:
        with connection.begin():
            _profile_id, version_id, _digest = create_market_profile_draft(
                connection,
                profile_key="isolated-market",
                scope_key="test",
                payload={"downloader": "fixture-only", "freshness": "explicit"},
            )
            validate_market_profile(connection, version_id=version_id)
            target_id = _seed_target(connection)
            evidence = accept_market_artifact(
                connection,
                locator="test-data/TEST-USDT-SWAP-5m.parquet",
                content=b"isolated fixture bytes",
                media_type="application/x-parquet",
                inspector_identity="fixture-inspector-v1",
                facts=_facts(),
            )
            replay = accept_market_artifact(
                connection,
                locator="another-observed-locator/file.parquet",
                content=b"isolated fixture bytes",
                media_type="application/x-parquet",
                inspector_identity="fixture-inspector-v1",
                facts=_facts(),
            )
            snapshot = seal_market_snapshot(
                connection,
                market_profile_version_id=version_id,
                members=((evidence.artifact_id, evidence.receipt_id, target_id, NOW, NOW + timedelta(hours=10)),),
            )
            snapshot_replay = seal_market_snapshot(
                connection,
                market_profile_version_id=version_id,
                members=((evidence.artifact_id, evidence.receipt_id, target_id, NOW, NOW + timedelta(hours=10)),),
            )
        assert replay.idempotent_replay is True
        assert replay.receipt_id == evidence.receipt_id
        assert snapshot.idempotent_replay is False
        assert snapshot_replay.idempotent_replay is True
        assert snapshot_replay.snapshot_id == snapshot.snapshot_id
        assert _count(connection, MARKET_ARTIFACTS_TABLE) == 1
        assert _count(connection, MARKET_INSPECTIONS_TABLE) == 1
        assert _count(connection, MARKET_RECEIPTS_TABLE) == 1
        assert _count(connection, MARKET_SNAPSHOTS_TABLE) == 1
        assert _count(connection, MARKET_SNAPSHOT_MEMBERS_TABLE) == 1
    finally:
        raw.close()
        engine.dispose()

@pytest.mark.parametrize(
    ("locator", "facts", "code"),
    [
        ("../escape.parquet", _facts(), "BLOCKED_MARKET_LOCATOR"),
        ("/absolute.parquet", _facts(), "BLOCKED_MARKET_LOCATOR"),
        ("data/file.parquet", _facts(gap_count=1), "BLOCKED_MARKET_QUALITY"),
        ("data/file.parquet", _facts(duplicate_count=1), "BLOCKED_MARKET_QUALITY"),
        ("data/file.parquet", _facts(null_count=1), "BLOCKED_MARKET_QUALITY"),
        ("data/file.parquet", _facts(monotonic=False), "BLOCKED_MARKET_QUALITY"),
    ],
)
def test_market_evidence_fails_closed_before_writes(locator, facts, code):
    engine, raw, connection = _db()
    try:
        with pytest.raises(CanonicalMarketBlocked) as raised:
            with connection.begin():
                accept_market_artifact(
                    connection,
                    locator=locator,
                    content=b"fixture",
                    media_type="application/x-parquet",
                    inspector_identity="fixture-inspector",
                    facts=facts,
                )
        assert raised.value.code == code
        assert _count(connection, MARKET_ARTIFACTS_TABLE) == 0
        assert _count(connection, MARKET_RECEIPTS_TABLE) == 0
    finally:
        raw.close()
        engine.dispose()


def test_snapshot_requires_validated_profile_accepted_receipt_and_coverage():
    engine, raw, connection = _db()
    try:
        with connection.begin():
            _profile_id, version_id, _ = create_market_profile_draft(
                connection,
                profile_key="draft-market",
                scope_key="test",
                payload={"explicit": True},
            )
        with pytest.raises(CanonicalMarketBlocked) as raised:
            with connection.begin():
                seal_market_snapshot(connection, market_profile_version_id=version_id, members=())
        assert raised.value.code == "BLOCKED_MARKET_SNAPSHOT_EMPTY"
        assert _count(connection, MARKET_SNAPSHOTS_TABLE) == 0
    finally:
        raw.close()
        engine.dispose()
