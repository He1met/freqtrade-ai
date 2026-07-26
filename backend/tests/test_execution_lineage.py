from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, StrategyGenerationRun
from app.models.execution_lineage import (
    LOCAL_DRY_RUN_SCOPE_ID,
    OKX_DEMO_TARGET_ID,
    UNKNOWN_LEGACY_SCOPE_ID,
    ExecutionManifest,
    ResearchJobAttempt,
)
from app.repositories import (
    ExecutionLineageRepository,
    ResearchJobRepository,
    StrategyGenerationRunRepository,
    ensure_execution_scope_catalog,
    list_execution_manifests,
    record_execution_manifest,
)
from app.schemas.strategy_generation_run import StrategyGenerationRunCreate


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    engine.dispose()


def test_new_local_execution_roots_have_explicit_scope_and_queries_hide_legacy(db) -> None:
    local_repository = StrategyGenerationRunRepository(db)
    local = local_repository.create(
        StrategyGenerationRunCreate(provider="deepseek", model="test")
    )
    ensure_execution_scope_catalog(db)
    legacy = StrategyGenerationRun(
        execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID,
        provider="historical",
        model="unknown",
        params_snapshot={},
    )
    db.add(legacy)
    db.commit()

    assert local.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID
    assert [row.id for row in local_repository.list()] == [local.id]
    assert local_repository.get(legacy.id) is None
    assert StrategyGenerationRunRepository(
        db, execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID
    ).get(legacy.id) is legacy


def test_research_attempt_inherits_job_scope(db) -> None:
    repository = ResearchJobRepository(db)
    job = repository.create(
        job_type="deepseek_backtest",
        operation="lineage-test",
        idempotency_key_digest="digest",
        request_hash="hash",
        request_payload={},
    )

    claimed = repository.claim_next(
        owner="test-worker",
        lease_seconds=60,
        now=datetime.now(timezone.utc),
    )

    assert claimed is not None
    attempt = db.query(ResearchJobAttempt).one()
    assert attempt.research_job_id == job.id
    assert attempt.execution_scope_id == LOCAL_DRY_RUN_SCOPE_ID


def test_unknown_legacy_research_scope_is_read_only(db) -> None:
    repository = ResearchJobRepository(db, execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID)

    with pytest.raises(ValueError, match="read-only"):
        repository.create(
            job_type="deepseek_backtest",
            operation="must-not-create",
            idempotency_key_digest="digest",
            request_hash="hash",
            request_payload={},
        )
    with pytest.raises(ValueError, match="read-only"):
        repository.claim_next(owner="worker", lease_seconds=60)


def test_exchange_repository_rejects_every_non_okx_target(db) -> None:
    for scope_id in (LOCAL_DRY_RUN_SCOPE_ID, UNKNOWN_LEGACY_SCOPE_ID, "OKX_LIVE"):
        with pytest.raises(ValueError, match="OKX_DEMO"):
            ExecutionLineageRepository(db, scope_id)


def test_same_target_client_order_id_is_unique(db) -> None:
    repository = ExecutionLineageRepository(db, OKX_DEMO_TARGET_ID)
    first_intent = repository.create_trade_intent(
        client_order_id="client1",
        instrument_id="BTC-USDT-SWAP",
        side="buy",
        position_side="long",
        order_type="market",
        quantity=Decimal("1"),
    )
    first_order = repository.record_order(
        trade_intent_id=first_intent.id,
        client_order_id="client1",
        status="PENDING",
    )

    with pytest.raises(IntegrityError):
        repository.record_order(
            trade_intent_id=first_intent.id,
            client_order_id="client1",
            status="PENDING",
        )
    db.rollback()
    assert first_order.client_order_id == first_intent.client_order_id


def test_trade_intent_client_order_id_is_unique_and_okx_legal(db) -> None:
    repository = ExecutionLineageRepository(db, OKX_DEMO_TARGET_ID)
    repository.create_trade_intent(
        client_order_id="Legal123",
        instrument_id="BTC-USDT-SWAP",
        side="buy",
        position_side="long",
        order_type="market",
        quantity=Decimal("1"),
    )

    with pytest.raises(IntegrityError):
        repository.create_trade_intent(
            client_order_id="Legal123",
            instrument_id="BTC-USDT-SWAP",
            side="buy",
            position_side="long",
            order_type="market",
            quantity=Decimal("1"),
        )
    db.rollback()
    for illegal in ("", "has-hyphen", "x" * 33, "空"):
        with pytest.raises(ValueError, match="clOrdId"):
            repository.create_trade_intent(
                client_order_id=illegal,
                instrument_id="BTC-USDT-SWAP",
                side="buy",
                position_side="long",
                order_type="market",
                quantity=Decimal("1"),
            )


def test_child_records_require_a_parent_on_the_same_target(db) -> None:
    repository = ExecutionLineageRepository(db, OKX_DEMO_TARGET_ID)

    with pytest.raises(ValueError, match="parent intent"):
        repository.record_risk_decision(
            trade_intent_id=999,
            decision="BLOCKED",
            policy_version="test",
        )
    with pytest.raises(ValueError, match="parent intent"):
        repository.record_order(
            trade_intent_id=999,
            client_order_id="Missing1",
            status="PENDING",
        )
    with pytest.raises(ValueError, match="parent order"):
        repository.record_fill(
            exchange_order_row_id=999,
            exchange_fill_id="fill-1",
            price=Decimal("1"),
            quantity=Decimal("1"),
        )


def test_order_queries_are_bound_to_the_repository_target(db) -> None:
    repository = ExecutionLineageRepository(db, OKX_DEMO_TARGET_ID)
    intent = repository.create_trade_intent(
        client_order_id="isolated1",
        instrument_id="ETH-USDT-SWAP",
        side="sell",
        position_side="short",
        order_type="limit",
        quantity=Decimal("2"),
        limit_price=Decimal("3000"),
    )
    order = repository.record_order(
        trade_intent_id=intent.id,
        client_order_id="isolated1",
        status="PENDING",
    )

    assert [row.id for row in repository.list_orders()] == [order.id]


def test_unknown_legacy_manifest_cannot_be_executable(db) -> None:
    with pytest.raises(ValueError, match="cannot be executable"):
        record_execution_manifest(
            db,
            execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID,
            manifest_kind="historical-backtest",
            schema_version="1",
            artifact_path="/historical/manifest.json",
            artifact_sha256="a" * 64,
            database_ids={},
            executable_evidence=True,
        )

    ensure_execution_scope_catalog(db)
    db.add(
        ExecutionManifest(
            execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID,
            manifest_kind="historical-backtest",
            schema_version="1",
            artifact_path="/historical/manifest.json",
            artifact_sha256="a" * 64,
            database_ids={},
            executable_evidence=True,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_manifest_queries_require_explicit_scope(db) -> None:
    local = record_execution_manifest(
        db,
        execution_scope_id=LOCAL_DRY_RUN_SCOPE_ID,
        manifest_kind="backtest",
        schema_version="2",
        artifact_path="/tmp/local.json",
        artifact_sha256="b" * 64,
        database_ids={"backtest_run_id": 1},
        executable_evidence=True,
    )
    legacy = record_execution_manifest(
        db,
        execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID,
        manifest_kind="backtest",
        schema_version="1",
        artifact_path="/historical/legacy.json",
        artifact_sha256="c" * 64,
        database_ids={},
        executable_evidence=False,
    )

    assert [row.id for row in list_execution_manifests(
        db, execution_scope_id=LOCAL_DRY_RUN_SCOPE_ID
    )] == [local.id]
    assert [row.id for row in list_execution_manifests(
        db, execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID
    )] == [legacy.id]
