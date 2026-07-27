from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.okx_demo.reconciliation_runtime import (
    OkxDemoRuntimeReconciliationAdapter,
    RUNTIME_DATABASE_ID_KEYS,
)
from app.adapters.okx_demo.models import Balance, FillQuery, OrderQuery, Position
from app.models import Base
from app.models.execution_lineage import ExecutionScope, ReconciliationRun
from app.models.okx_demo_reconciliation import (
    OkxDemoExchangeEvent,
    OkxDemoReconciliationState,
    OkxDemoRecoveryGrant,
)
from app.services.okx_demo_reconciliation import OkxDemoReconciliationBlocked


NOW = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)


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


def _snapshot(items, *, expires_at=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            authenticated=True,
            stale=False,
            fetched_at=NOW,
            exchange_timestamp=NOW,
            expires_at=expires_at or NOW + timedelta(seconds=30),
        ),
        items=items,
    )


class CompleteReadClient:
    def pending_orders(self, *, after=None, limit=100):
        return _snapshot([])

    def orders_history(self, *, after=None, limit=100):
        return _snapshot([])

    def fills_history(self, *, after=None, limit=100):
        return _snapshot([])

    def positions(self):
        return _snapshot([])

    def balance(self):
        return _snapshot(
            [
                {
                    "currency": "USDT",
                    "total_equity": "10000",
                    "available_balance": "9000",
                    "equity": "10000",
                    "timestamp": NOW.isoformat(),
                }
            ]
        )


def test_runtime_factory_contract_runs_complete_rest_baseline(
    db,
    tmp_path,
) -> None:
    adapter = OkxDemoRuntimeReconciliationAdapter(
        evidence_root=tmp_path / "managed" / "reconciliation",
        allowed_evidence_root=tmp_path / "managed",
        account_fingerprint_sha256="a" * 64,
        now_provider=lambda: NOW,
    )
    result = adapter.reconcile_before_writer(
        read_client=CompleteReadClient(),
        db=db,
    )
    db.commit()
    assert set(result) == {
        "status",
        "execution_target",
        "reconciliation_run_id",
        "database_ids",
        "observed_at",
        "safe_to_open",
    }
    assert result["status"] == "RECONCILED"
    assert result["execution_target"] == "OKX_DEMO"
    assert result["safe_to_open"] is True
    assert result["reconciliation_run_id"] in result["database_ids"][
        "reconciliation_run"
    ]
    assert tuple(result["database_ids"]) == RUNTIME_DATABASE_ID_KEYS


def test_runtime_restart_allocates_generation_after_persisted_rest_events(
    db,
    tmp_path,
) -> None:
    def adapter():
        return OkxDemoRuntimeReconciliationAdapter(
            evidence_root=tmp_path / "managed" / "reconciliation",
            allowed_evidence_root=tmp_path / "managed",
            account_fingerprint_sha256="a" * 64,
            now_provider=lambda: NOW,
        )

    adapter().reconcile_before_writer(read_client=CompleteReadClient(), db=db)
    db.commit()
    adapter().reconcile_before_writer(read_client=CompleteReadClient(), db=db)
    db.commit()

    generations = set(
        db.scalars(select(OkxDemoExchangeEvent.stream_generation)).all()
    )
    assert generations == {1, 2}


def test_runtime_cycle_executes_only_current_run_recovery_grants(db) -> None:
    db.add(
        ExecutionScope(
            scope_id="OKX_DEMO",
            scope_kind="EXCHANGE_TARGET",
            exchange_capable=True,
            executable=False,
            exchange_writes=False,
            order_submission_authorized=False,
        )
    )
    runs = []
    for _ in range(2):
        run = ReconciliationRun(
            execution_target_id="OKX_DEMO",
            status="DRIFTED",
            summary_snapshot={},
            database_ids={},
            artifact_status="PENDING",
            authoritative_observed_at=NOW,
            source_type="api_aggregate",
            core_data=True,
            started_at=NOW,
            completed_at=NOW,
        )
        db.add(run)
        db.flush()
        runs.append(run)
    state = OkxDemoReconciliationState(
        execution_target_id="OKX_DEMO",
        status="DRIFTED",
        opening_frozen=True,
        block_reason="test",
        last_reconciliation_run_id=runs[-1].id,
    )
    db.add(state)
    for run, action in (
        (runs[0], "CANCEL"),
        (runs[1], "CANCEL"),
        (runs[1], "REDUCE_ONLY"),
    ):
        db.add(
            OkxDemoRecoveryGrant(
                execution_target_id="OKX_DEMO",
                reconciliation_run_id=run.id,
                exchange_order_row_id=1 if action == "CANCEL" else None,
                grant_digest=("{:064x}".format(run.id * 10 + len(action))),
                action=action,
                instrument_id="BTC-USDT-SWAP",
                position_side="net",
                max_quantity=(
                    Decimal("0") if action == "CANCEL" else Decimal("1")
                ),
                status="ACTIVE",
                expires_at=NOW + timedelta(minutes=1),
            )
        )
    db.commit()

    class RecoveryWriter:
        def __init__(self):
            self.calls = []

        def recovery_cancel(self, *, recovery_grant_database_id):
            self.calls.append(("CANCEL", recovery_grant_database_id))

        def recovery_reduce_only(self, *, recovery_grant_database_id):
            self.calls.append(("REDUCE_ONLY", recovery_grant_database_id))

    writer = RecoveryWriter()
    adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)
    adapter.run_cycle(read_client=object(), writer=writer, db=db)

    assert [action for action, _ in writer.calls] == [
        "CANCEL",
        "REDUCE_ONLY",
    ]


def test_runtime_rejects_stale_page_and_conflicting_page_identity(
    db,
    tmp_path,
) -> None:
    adapter = OkxDemoRuntimeReconciliationAdapter(
        evidence_root=tmp_path / "managed" / "reconciliation",
        allowed_evidence_root=tmp_path / "managed",
        account_fingerprint_sha256="a" * 64,
        now_provider=lambda: NOW,
    )

    class StaleClient(CompleteReadClient):
        def pending_orders(self, *, after=None, limit=100):
            return _snapshot([], expires_at=NOW)

    with pytest.raises(OkxDemoReconciliationBlocked):
        adapter.reconcile_before_writer(read_client=StaleClient(), db=db)
    db.rollback()

    duplicate_items = [
        {
            "order_id": "1",
            "client_order_id": "FAI00000000000000000000000000001",
            "inst_id": "BTC-USDT-SWAP",
            "state": "live",
            "size": "1",
            "accumulated_fill_size": "0",
            "updated_at": NOW.isoformat(),
        },
        {
            "order_id": "1",
            "client_order_id": "FAI00000000000000000000000000001",
            "inst_id": "BTC-USDT-SWAP",
            "state": "partially_filled",
            "size": "1",
            "accumulated_fill_size": "0.5",
            "updated_at": NOW.isoformat(),
        },
    ]

    class ConflictClient(CompleteReadClient):
        def pending_orders(self, *, after=None, limit=100):
            return _snapshot(duplicate_items)

    with pytest.raises(OkxDemoReconciliationBlocked):
        adapter.reconcile_before_writer(read_client=ConflictClient(), db=db)


def test_runtime_accepts_actual_normalized_pydantic_models_and_exact_449_ids(
    db,
    tmp_path,
) -> None:
    order = OrderQuery(
        inst_id="BTC-USDT-SWAP",
        order_id="1001",
        client_order_id="FAI00000000000000000000000000001",
        state="live",
        side="buy",
        position_side="net",
        margin_mode="cross",
        order_type="limit",
        reduce_only=False,
        price="50000",
        size="1",
        accumulated_fill_size="0",
        created_at=NOW,
        updated_at=NOW,
    )
    fill = FillQuery(
        fill_id="2001",
        order_id="1000",
        inst_id="BTC-USDT-SWAP",
        price="49900",
        size="0.1",
        fee="-0.01",
        timestamp=NOW,
    )
    position = Position(
        inst_id="BTC-USDT-SWAP",
        margin_mode="cross",
        position_side="net",
        contracts="0",
        available_contracts="0",
        timestamp=NOW,
    )
    balance = Balance(
        currency="USDT",
        total_equity="10000",
        available_balance="9000",
        equity="10000",
        timestamp=NOW,
    )

    class PydanticReadClient(CompleteReadClient):
        def pending_orders(self, *, after=None, limit=100):
            return _snapshot([order])

        def fills_history(self, *, after=None, limit=100):
            return _snapshot([fill])

        def positions(self):
            return _snapshot([position])

        def balance(self):
            return _snapshot([balance])

    adapter = OkxDemoRuntimeReconciliationAdapter(
        evidence_root=tmp_path / "managed" / "reconciliation",
        allowed_evidence_root=tmp_path / "managed",
        account_fingerprint_sha256="a" * 64,
        now_provider=lambda: NOW,
    )
    result = adapter.reconcile_before_writer(
        read_client=PydanticReadClient(),
        db=db,
    )
    db.commit()

    assert tuple(result["database_ids"]) == RUNTIME_DATABASE_ID_KEYS
    assert "recovery_grants" not in result["database_ids"]
    assert result["observed_at"] == NOW.isoformat()
