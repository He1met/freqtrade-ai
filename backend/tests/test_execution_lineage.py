from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    ResearchJob,
    ResearchWorkerControl,
    StrategyGenerationRun,
)
from app.models.execution_lineage import (
    LOCAL_DRY_RUN_SCOPE_ID,
    OKX_DEMO_TARGET_ID,
    UNKNOWN_LEGACY_SCOPE_ID,
    ExecutionManifest,
    ExchangeFill,
    ExchangeOrder,
    ExecutionScope,
    ResearchJobAttempt,
    RiskDecision,
    TradeIntent,
)
from app.repositories import (
    BacktestRepository,
    ExecutionLineageRepository,
    ResearchJobLinkageBlocked,
    ResearchJobRepository,
    StrategyGenerationRunRepository,
    StrategyRepository,
    ensure_execution_scope_catalog,
    list_execution_manifests,
    record_execution_manifest,
)
from app.schemas import (
    BacktestResultCreate,
    BacktestRunCreate,
    BacktestRunStatusUpdate,
    BacktestTaskCreate,
    BacktestTaskStatusUpdate,
    StrategyCreate,
    StrategyVersionCreate,
)
from app.schemas.strategy_generation_run import (
    StrategyGenerationRunCreate,
    StrategyGenerationRunStatusUpdate,
)
from app.services.execution_lineage import ExecutionLineagePersistenceService


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


def test_unknown_legacy_is_read_only_for_every_repository_mutation(db) -> None:
    ensure_execution_scope_catalog(db)
    strategy_repository = StrategyRepository(db)
    strategy = strategy_repository.create(
        StrategyCreate(name="Legacy mutation gate", slug="legacy-mutation-gate")
    )
    version = strategy_repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={},
            generated_code="class LegacyMutationGate: pass",
            file_path="/tmp/legacy-mutation-gate.py",
        )
    )
    assert version is not None
    generation = StrategyGenerationRun(
        execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID,
        provider="historical",
        model="unknown",
        params_snapshot={},
        status="pending",
    )
    db.add(generation)
    db.flush()
    run = BacktestRun(
        execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID,
        strategy_version_id=version.id,
        config_snapshot={},
        status="pending",
    )
    db.add(run)
    db.flush()
    task = BacktestTask(
        backtest_run_id=run.id,
        pair="BTC/USDT:USDT",
        timeframe="15m",
        status="pending",
    )
    db.add(task)
    db.flush()
    result = BacktestResult(
        backtest_run_id=run.id,
        backtest_task_id=task.id,
        result_path="/historical/result.json",
        metrics_snapshot={},
    )
    db.add(result)
    job = ResearchJob(
        execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID,
        job_type="deepseek_backtest",
        operation="historical",
        idempotency_key_digest="legacy-digest",
        request_hash="legacy-hash",
        request_payload={},
        status="RUNNING",
        stage="GENERATION",
        lease_owner="legacy-worker",
        lease_token="legacy-token",
        lease_expires_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()

    generation_repository = StrategyGenerationRunRepository(
        db, execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID
    )
    backtest_repository = BacktestRepository(
        db, execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID
    )
    job_repository = ResearchJobRepository(
        db, execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID
    )
    blocked_calls = (
        lambda: generation_repository.create(
            StrategyGenerationRunCreate(provider="new", model="new")
        ),
        lambda: generation_repository.update_status(
            generation.id,
            StrategyGenerationRunStatusUpdate(status="running"),
        ),
        lambda: backtest_repository.create_run(
            BacktestRunCreate(strategy_version_id=version.id)
        ),
        lambda: backtest_repository.update_run_status(
            run.id, BacktestRunStatusUpdate(status="running")
        ),
        lambda: backtest_repository.create_task(
            run.id, BacktestTaskCreate(pair="ETH/USDT:USDT", timeframe="15m")
        ),
        lambda: backtest_repository.claim_next_pending_task(run.id),
        lambda: backtest_repository.update_task_status(
            task.id, BacktestTaskStatusUpdate(status="running")
        ),
        lambda: backtest_repository.save_result(
            task.id,
            BacktestResultCreate(
                result_path="/tmp/new.json",
                metrics_snapshot={},
            ),
        ),
        lambda: job_repository.create(
            job_type="new",
            operation="new",
            idempotency_key_digest="new",
            request_hash="new",
            request_payload={},
        ),
        lambda: job_repository.create_or_get(
            job_type="new",
            operation="new-or-get",
            idempotency_key_digest="new-or-get",
            request_hash="new-or-get",
            request_payload={},
        ),
        lambda: job_repository.get_control(),
        lambda: job_repository.set_paused(True, "must not mutate"),
        lambda: job_repository.claim_next(owner="worker", lease_seconds=60),
        lambda: job_repository.heartbeat(
            job.id, "legacy-token", lease_seconds=60
        ),
        lambda: job_repository.mark_provider_attempt(job.id, "legacy-token"),
        lambda: job_repository.complete(
            job.id,
            "legacy-token",
            status="SUCCESS",
            stage="COMPLETED",
            links={},
            evidence_snapshot={},
            error_message=None,
            provider_completed=False,
        ),
        lambda: job_repository.cancel(job.id, "must not mutate"),
        lambda: job_repository.cancel_at_checkpoint(job.id, "legacy-token"),
        lambda: job_repository.expire_stale(),
    )
    for blocked_call in blocked_calls:
        with pytest.raises(ValueError, match="read-only"):
            blocked_call()

    db.expire_all()
    persisted_generation = db.get(StrategyGenerationRun, generation.id)
    persisted_run = db.get(BacktestRun, run.id)
    persisted_task = db.get(BacktestTask, task.id)
    persisted_result = db.get(BacktestResult, result.id)
    persisted_job = db.get(ResearchJob, job.id)
    assert persisted_generation is not None and persisted_generation.status == "pending"
    assert persisted_run is not None and persisted_run.status == "pending"
    assert persisted_run.requested_task_count == 0
    assert persisted_task is not None and persisted_task.status == "pending"
    assert persisted_task.result_path is None
    assert persisted_result is not None
    assert persisted_result.result_path == "/historical/result.json"
    assert persisted_job is not None and persisted_job.status == "RUNNING"
    assert persisted_job.strategy_generation_run_id is None
    assert db.get(ResearchWorkerControl, 1) is None
    assert db.query(ResearchJobAttempt).count() == 0


def _create_local_completion_chain(db, suffix: str):
    generation = StrategyGenerationRunRepository(db).create(
        StrategyGenerationRunCreate(provider="deepseek", model=f"model-{suffix}")
    )
    strategy_repository = StrategyRepository(db)
    strategy = strategy_repository.create(
        StrategyCreate(name=f"Completion {suffix}", slug=f"completion-{suffix}")
    )
    version = strategy_repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            generation_run_id=generation.id,
            blueprint={},
            generated_code=f"class Completion{suffix}: pass",
            file_path=f"/tmp/completion-{suffix}.py",
        )
    )
    assert version is not None
    backtests = BacktestRepository(db)
    run = backtests.create_run(BacktestRunCreate(strategy_version_id=version.id))
    assert run is not None
    task = backtests.create_task(
        run.id,
        BacktestTaskCreate(pair="BTC/USDT:USDT", timeframe="15m"),
    )
    assert task is not None
    result = backtests.save_result(
        task.id,
        BacktestResultCreate(
            result_path=f"/tmp/completion-{suffix}.json",
            metrics_snapshot={},
        ),
    )
    assert result is not None
    return generation, strategy, version, run, task, result


def test_research_completion_rejects_missing_legacy_and_inconsistent_links_atomically(
    db,
) -> None:
    job_repository = ResearchJobRepository(db)
    job = job_repository.create(
        job_type="deepseek_backtest",
        operation="completion-validation",
        idempotency_key_digest="completion-validation",
        request_hash="completion-validation",
        request_payload={},
    )
    claimed = job_repository.claim_next(owner="worker", lease_seconds=60)
    assert claimed is not None and claimed.lease_token is not None
    lease_token = claimed.lease_token
    first = _create_local_completion_chain(db, "one")
    second = _create_local_completion_chain(db, "two")
    ensure_execution_scope_catalog(db)
    legacy_generation = StrategyGenerationRun(
        execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID,
        provider="historical",
        model="unknown",
        params_snapshot={},
    )
    db.add(legacy_generation)
    db.commit()

    invalid_links = (
        {"strategy_generation_run_id": 999999},
        {"strategy_generation_run_id": legacy_generation.id},
        {"backtest_run_id": first[3].id, "backtest_task_id": second[4].id},
        {
            "backtest_run_id": first[3].id,
            "backtest_task_id": first[4].id,
            "backtest_result_id": second[5].id,
        },
    )
    for links in invalid_links:
        with pytest.raises(ResearchJobLinkageBlocked, match="BLOCKED"):
            job_repository.complete(
                job.id,
                lease_token,
                status="SUCCESS",
                stage="COMPLETED",
                links=links,
                evidence_snapshot={"status": "SUCCESS"},
                error_message=None,
                provider_completed=False,
            )
        db.expire_all()
        persisted = db.get(ResearchJob, job.id)
        assert persisted is not None and persisted.status == "RUNNING"
        assert persisted.strategy_generation_run_id is None
        assert persisted.backtest_run_id is None
        assert persisted.backtest_task_id is None
        assert persisted.backtest_result_id is None

    completed = job_repository.complete(
        job.id,
        lease_token,
        status="SUCCESS",
        stage="COMPLETED",
        links={
            "strategy_generation_run_id": first[0].id,
            "strategy_id": first[1].id,
            "strategy_version_id": first[2].id,
            "backtest_run_id": first[3].id,
            "backtest_task_id": first[4].id,
            "backtest_result_id": first[5].id,
        },
        evidence_snapshot={"status": "SUCCESS"},
        error_message=None,
        provider_completed=False,
    )
    assert completed is not None and completed.status == "SUCCESS"
    assert completed.backtest_result_id == first[5].id


def test_exchange_repository_rejects_every_non_okx_target(db) -> None:
    for scope_id in (LOCAL_DRY_RUN_SCOPE_ID, UNKNOWN_LEGACY_SCOPE_ID, "OKX_LIVE"):
        with pytest.raises(ValueError, match="OKX_DEMO"):
            ExecutionLineageRepository(db, scope_id)


def test_okx_demo_scope_is_capability_only_and_not_currently_authorized(db) -> None:
    ensure_execution_scope_catalog(db)
    scope = db.get(ExecutionScope, OKX_DEMO_TARGET_ID)

    assert scope is not None
    assert scope.exchange_capable is True
    assert scope.executable is False
    assert scope.exchange_writes is False
    assert scope.order_submission_authorized is False


@pytest.mark.parametrize("failure_kind", ["integrity", "exception"])
def test_execution_lineage_use_case_rolls_back_the_entire_chain(
    db,
    failure_kind: str,
) -> None:
    service = ExecutionLineagePersistenceService(db, OKX_DEMO_TARGET_ID)

    def persist(repository, session):
        intent = repository.create_trade_intent(
            client_order_id="Atomic1",
            instrument_id="BTC-USDT-SWAP",
            side="buy",
            position_side="long",
            order_type="market",
            quantity=Decimal("1"),
        )
        repository.record_risk_decision(
            trade_intent_id=intent.id,
            decision="BLOCKED",
            policy_version="test",
        )
        order = repository.record_order(
            trade_intent_id=intent.id,
            client_order_id=intent.client_order_id,
            status="PERSISTED_NOT_AUTHORIZED",
        )
        repository.record_fill(
            exchange_order_row_id=order.id,
            exchange_fill_id="AtomicFill1",
            price=Decimal("1"),
            quantity=Decimal("1"),
        )
        record_execution_manifest(
            session,
            execution_scope_id=OKX_DEMO_TARGET_ID,
            manifest_kind="persistence-test",
            schema_version="1",
            artifact_path="/tmp/atomic.json",
            artifact_sha256="d" * 64,
            database_ids={"trade_intent_id": intent.id},
            executable_evidence=False,
        )
        if failure_kind == "integrity":
            repository.record_fill(
                exchange_order_row_id=order.id,
                exchange_fill_id="AtomicFill1",
                price=Decimal("1"),
                quantity=Decimal("1"),
            )
        raise RuntimeError("injected failure after every persistence stage")

    expected_error = IntegrityError if failure_kind == "integrity" else RuntimeError
    with pytest.raises(expected_error):
        service.run(persist)

    assert db.query(TradeIntent).count() == 0
    assert db.query(RiskDecision).count() == 0
    assert db.query(ExchangeOrder).count() == 0
    assert db.query(ExchangeFill).count() == 0
    assert db.query(ExecutionManifest).count() == 0


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


@pytest.mark.parametrize("illegal", ["has-hyphen", "空", "x" * 33])
def test_sqlite_database_rejects_illegal_client_order_id(db, illegal: str) -> None:
    ensure_execution_scope_catalog(db)
    db.commit()
    db.add(
        TradeIntent(
            execution_target_id=OKX_DEMO_TARGET_ID,
            client_order_id=illegal,
            instrument_id="BTC-USDT-SWAP",
            side="buy",
            position_side="long",
            order_type="market",
            quantity=Decimal("1"),
            request_snapshot={},
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


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
    with pytest.raises(ValueError, match="read-only"):
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


def test_okx_demo_manifest_cannot_claim_executable_evidence(db) -> None:
    with pytest.raises(ValueError, match="authorization is false"):
        record_execution_manifest(
            db,
            execution_scope_id=OKX_DEMO_TARGET_ID,
            manifest_kind="order",
            schema_version="1",
            artifact_path="/tmp/order.json",
            artifact_sha256="e" * 64,
            database_ids={},
            executable_evidence=True,
        )


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
    ensure_execution_scope_catalog(db)
    legacy = ExecutionManifest(
        execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID,
        manifest_kind="backtest",
        schema_version="1",
        artifact_path="/historical/legacy.json",
        artifact_sha256="c" * 64,
        database_ids={},
        executable_evidence=False,
    )
    db.add(legacy)
    db.commit()

    assert [row.id for row in list_execution_manifests(
        db, execution_scope_id=LOCAL_DRY_RUN_SCOPE_ID
    )] == [local.id]
    assert [row.id for row in list_execution_manifests(
        db, execution_scope_id=UNKNOWN_LEGACY_SCOPE_ID
    )] == [legacy.id]
