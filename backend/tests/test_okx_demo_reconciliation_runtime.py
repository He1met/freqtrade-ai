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
from app.adapters.okx_demo.writer_models import ClaimedApprovedExecution
from app.adapters.okx_demo.models import Balance, FillQuery, OrderQuery, Position
from app.models import Base
from app.models.execution_lineage import ExecutionScope, ReconciliationRun
from app.models.okx_demo_reconciliation import (
    OkxDemoExchangeEvent,
    OkxDemoReconciliationState,
    OkxDemoRecoveryGrant,
)
from app.models.order_writer import OkxDemoCanaryLifecycle, OkxOrderWriteAttempt
from app.models.strategy_deployment import StrategyDeployment
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

    def fills_history(
        self,
        *,
        after=None,
        begin=None,
        end=None,
        limit=100,
    ):
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


def test_runtime_restart_stops_history_at_persisted_overlap_watermark(
    db,
    tmp_path,
) -> None:
    def adapter(now):
        return OkxDemoRuntimeReconciliationAdapter(
            evidence_root=tmp_path / "managed" / "reconciliation",
            allowed_evidence_root=tmp_path / "managed",
            account_fingerprint_sha256="a" * 64,
            now_provider=lambda: now,
        )

    adapter(NOW).reconcile_before_writer(
        read_client=CompleteReadClient(),
        db=db,
    )
    db.commit()

    restarted_at = NOW + timedelta(minutes=1)
    recent_at = NOW + timedelta(seconds=30)
    old_at = NOW - timedelta(seconds=10)

    def order(order_id, updated_at):
        return {
            "order_id": str(order_id),
            "client_order_id": "FAI{:029d}".format(order_id),
            "inst_id": "BTC-USDT-SWAP",
            "state": "filled",
            "size": "1",
            "accumulated_fill_size": "1",
            "average_price": "50000",
            "reduce_only": False,
            "updated_at": updated_at.isoformat(),
        }

    class IncrementalReadClient(CompleteReadClient):
        history_calls = 0

        def _fresh_snapshot(self, items):
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    authenticated=True,
                    stale=False,
                    fetched_at=restarted_at,
                    exchange_timestamp=restarted_at,
                    expires_at=restarted_at + timedelta(seconds=30),
                ),
                items=items,
            )

        def pending_orders(self, *, after=None, limit=100):
            return self._fresh_snapshot([])

        def orders_history(self, *, after=None, limit=100):
            self.history_calls += 1
            if after is not None:
                assert after == "901"
                return self._fresh_snapshot([])
            return self._fresh_snapshot(
                [
                    order(998, old_at),
                    order(1000, recent_at),
                    order(999, recent_at),
                    *(
                        order(order_id, old_at - timedelta(seconds=index))
                        for index, order_id in enumerate(range(997, 900, -1))
                    ),
                ]
            )

        def fills_history(
            self,
            *,
            after=None,
            begin=None,
            end=None,
            limit=100,
        ):
            assert begin == str(
                int((NOW - timedelta(seconds=5)).timestamp() * 1000)
            )
            assert end == str(int(restarted_at.timestamp() * 1000))
            return self._fresh_snapshot([])

        def positions(self):
            return self._fresh_snapshot([])

        def balance(self):
            return self._fresh_snapshot(
                [
                    {
                        "currency": "USDT",
                        "total_equity": "10000",
                        "available_balance": "9000",
                        "equity": "10000",
                        "timestamp": restarted_at.isoformat(),
                    }
                ]
            )

    client = IncrementalReadClient()
    adapter(restarted_at).reconcile_before_writer(
        read_client=client,
        db=db,
    )
    db.commit()

    # A cutoff found in an unordered full page cannot prove that the next page
    # contains only old rows.  The adapter must consume the terminal page.
    assert client.history_calls == 2
    persisted_order_ids = set(
        db.scalars(
            select(OkxDemoExchangeEvent.entity_key).where(
                OkxDemoExchangeEvent.entity_kind == "ORDER",
                OkxDemoExchangeEvent.stream_generation == 2,
            )
        ).all()
    )
    assert persisted_order_ids == {"1000", "999"}


def test_runtime_pagination_accepts_reverse_and_out_of_order_pages(
    tmp_path,
) -> None:
    adapter = OkxDemoRuntimeReconciliationAdapter(
        evidence_root=tmp_path / "managed" / "reconciliation",
        allowed_evidence_root=tmp_path / "managed",
        account_fingerprint_sha256="a" * 64,
        now_provider=lambda: NOW,
    )
    first_page = list(range(300, 200, -1))
    # This is neither newest-first nor oldest-first. Cursor selection must not
    # depend on the position of a row in the response.
    first_page = first_page[::2] + first_page[1::2]

    class UnorderedClient:
        calls = []

        def orders_history(self, *, after=None, limit=100):
            self.calls.append(after)
            if after is None:
                return _snapshot([{"order_id": str(value)} for value in first_page])
            assert after == "201"
            return _snapshot([{"order_id": "200"}, {"order_id": "199"}])

    client = UnorderedClient()
    items, _watermark, _observed = adapter._pages(
        client,
        "orders_history",
        identity_field="order_id",
    )

    assert client.calls == [None, "201"]
    assert {item["order_id"] for item in items} == {
        str(value) for value in range(199, 301)
    }


def test_runtime_pagination_deduplicates_a_boundary_identity(
    tmp_path,
) -> None:
    adapter = OkxDemoRuntimeReconciliationAdapter(
        evidence_root=tmp_path / "managed" / "reconciliation",
        allowed_evidence_root=tmp_path / "managed",
        account_fingerprint_sha256="a" * 64,
        now_provider=lambda: NOW,
    )

    class BoundaryClient:
        def orders_history(self, *, after=None, limit=100):
            if after is None:
                return _snapshot(
                    [{"order_id": str(value)} for value in range(300, 200, -1)]
                )
            assert after == "201"
            return _snapshot(
                [
                    {"order_id": "201"},
                    {"order_id": "200"},
                    {"order_id": "199"},
                ]
            )

    items, _watermark, _observed = adapter._pages(
        BoundaryClient(),
        "orders_history",
        identity_field="order_id",
    )

    identities = [item["order_id"] for item in items]
    assert len(identities) == len(set(identities)) == 102
    assert identities.count("201") == 1


def test_fill_pagination_uses_bill_id_without_changing_trade_identity(
    tmp_path,
) -> None:
    adapter = OkxDemoRuntimeReconciliationAdapter(
        evidence_root=tmp_path / "managed" / "reconciliation",
        allowed_evidence_root=tmp_path / "managed",
        account_fingerprint_sha256="a" * 64,
        now_provider=lambda: NOW,
    )

    class FillClient:
        calls = []

        def fills_history(
            self,
            *,
            after=None,
            begin=None,
            end=None,
            limit=100,
        ):
            self.calls.append((after, begin, end, limit))
            if after is None:
                return _snapshot(
                    [
                        {
                            "fill_id": str(10000 + value),
                            "bill_id": str(value),
                        }
                        for value in range(500, 400, -1)
                    ]
                )
            assert after == "401"
            return _snapshot(
                [{"fill_id": "10400", "bill_id": "400"}]
            )

    client = FillClient()
    items, _watermark, _observed = adapter._pages(
        client,
        "fills_history",
        identity_field="fill_id",
        cursor_field="bill_id",
        request_kwargs={"begin": "1000", "end": "2000"},
    )

    assert client.calls == [
        (None, "1000", "2000", 100),
        ("401", "1000", "2000", 100),
    ]
    assert len(items) == 101
    assert {item["fill_id"] for item in items} >= {
        "10500",
        "10400",
    }


@pytest.mark.parametrize(
    "second_page, match",
    [
        ([{"order_id": "202"}], "escapes the requested cursor window"),
        ([{"order_id": "201"}] * 100, "cursor did not advance"),
    ],
)
def test_runtime_pagination_blocks_a_contradictory_or_looping_cursor(
    tmp_path,
    second_page,
    match,
) -> None:
    adapter = OkxDemoRuntimeReconciliationAdapter(
        evidence_root=tmp_path / "managed" / "reconciliation",
        allowed_evidence_root=tmp_path / "managed",
        account_fingerprint_sha256="a" * 64,
        now_provider=lambda: NOW,
    )

    class InvalidCursorClient:
        def orders_history(self, *, after=None, limit=100):
            if after is None:
                return _snapshot(
                    [{"order_id": str(value)} for value in range(300, 200, -1)]
                )
            assert after == "201"
            return _snapshot(second_page)

    with pytest.raises(OkxDemoReconciliationBlocked, match=match):
        adapter._pages(
            InvalidCursorClient(),
            "orders_history",
            identity_field="order_id",
        )


@pytest.mark.parametrize(
    "item, match",
    [
        ({}, "without identity"),
        ({"order_id": "not-a-cursor"}, "pagination cursor is not canonical numeric"),
        ({"order_id": "001"}, "pagination cursor is not canonical numeric"),
    ],
)
def test_runtime_pagination_blocks_an_unprovable_identity(
    tmp_path,
    item,
    match,
) -> None:
    adapter = OkxDemoRuntimeReconciliationAdapter(
        evidence_root=tmp_path / "managed" / "reconciliation",
        allowed_evidence_root=tmp_path / "managed",
        account_fingerprint_sha256="a" * 64,
        now_provider=lambda: NOW,
    )

    class MissingIdentityClient:
        def orders_history(self, *, after=None, limit=100):
            assert after is None
            return _snapshot([item])

    with pytest.raises(OkxDemoReconciliationBlocked, match=match):
        adapter._pages(
            MissingIdentityClient(),
            "orders_history",
            identity_field="order_id",
        )


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
                position_side="long",
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
        position_side="long",
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
        bill_id="3001",
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
        position_side="long",
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

        def fills_history(
            self,
            *,
            after=None,
            begin=None,
            end=None,
            limit=100,
        ):
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


def _runtime_approved_execution() -> ClaimedApprovedExecution:
    return ClaimedApprovedExecution(
        approval_id=41,
        trade_intent_id=31,
        risk_decision_id=37,
        execution_target_id="OKX_DEMO",
        authorization_schema_version="RISK_V1",
        canonical_hash="1" * 64,
        policy_digest="2" * 64,
        approved_payload_hash="3" * 64,
        client_order_id="RuntimeApproval00000000000000001",
        instrument_id="BTC-USDT-SWAP",
        side="buy",
        position_side="long",
        order_type="limit",
        contracts=Decimal("1"),
        limit_price=Decimal("50000"),
        reduce_only=False,
        margin_mode="isolated",
        leverage=Decimal("2"),
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        policy_version="risk-v1",
        idempotency_digest="4" * 64,
        take_profit_trigger_price=None,
        take_profit_order_price=None,
        stop_loss_trigger_price=None,
        stop_loss_order_price=None,
    )


def _allow_fresh_runtime_opening(db) -> None:
    run = ReconciliationRun(
        execution_target_id="OKX_DEMO",
        status="RECONCILED",
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
    db.add(
        OkxDemoReconciliationState(
            execution_target_id="OKX_DEMO",
            status="RECONCILED",
            opening_frozen=False,
            last_event_observed_at=NOW,
            last_reconciliation_run_id=run.id,
        )
    )
    db.commit()


def test_runtime_submission_disabled_never_calls_writer(
    db,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime.get_settings",
        lambda: SimpleNamespace(
            execution_target_manifest=SimpleNamespace(
                active_target=SimpleNamespace(
                    order_submission_enabled=False,
                )
            )
        ),
    )
    adapter = OkxDemoRuntimeReconciliationAdapter(
        evidence_root=tmp_path / "managed" / "reconciliation",
        allowed_evidence_root=tmp_path / "managed",
        account_fingerprint_sha256="a" * 64,
        order_submission_enabled=True,
        now_provider=lambda: NOW,
    )
    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "_next_unconsumed_approved_execution",
        lambda *_args, **_kwargs: _runtime_approved_execution(),
    )

    class Writer:
        calls = []

        def place(self, approved, *, submission_grant):
            self.calls.append((approved, submission_grant))

    writer = Writer()
    adapter.run_cycle(read_client=object(), writer=writer, db=db)

    assert writer.calls == []


def test_runtime_submits_at_most_one_fresh_approval_with_ephemeral_demo_grant(
    db,
    monkeypatch,
    tmp_path,
) -> None:
    _allow_fresh_runtime_opening(db)
    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime.get_settings",
        lambda: SimpleNamespace(
            execution_target_manifest=SimpleNamespace(
                active_target=SimpleNamespace(
                    order_submission_enabled=True,
                )
            )
        ),
    )
    adapter = OkxDemoRuntimeReconciliationAdapter(
        evidence_root=tmp_path / "managed" / "reconciliation",
        allowed_evidence_root=tmp_path / "managed",
        account_fingerprint_sha256="a" * 64,
        order_submission_enabled=True,
        now_provider=lambda: NOW,
    )
    selected = []

    def next_approval(*_args, **_kwargs):
        selected.append(True)
        return _runtime_approved_execution()

    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "_next_unconsumed_approved_execution",
        next_approval,
    )

    class Writer:
        calls = []

        def place(self, approved, *, submission_grant):
            self.calls.append((approved, submission_grant))

    writer = Writer()
    adapter.run_cycle(read_client=object(), writer=writer, db=db)

    assert selected == [True]
    assert len(writer.calls) == 1
    approved, grant = writer.calls[0]
    assert grant.approval_id == approved.approval_id
    assert grant.execution_target_id == "OKX_DEMO"
    assert grant.simulated_trading is True
    assert grant.allow_real_funds is False
    assert grant.order_submission_enabled is True
    assert grant.writer_instance_id.startswith("Runtime")
    assert grant.canonical_hash == approved.canonical_hash
    assert grant.policy_digest == approved.policy_digest
    assert grant.approved_payload_hash == approved.approved_payload_hash
    assert grant.expires_at == NOW + timedelta(seconds=10)


def test_runtime_resumes_unresolved_placement_before_selecting_new_approval(
    db,
    monkeypatch,
) -> None:
    db.add(
        OkxOrderWriteAttempt(
            execution_target_id="OKX_DEMO",
            exchange_order_row_id=901,
            approval_id=41,
            operation="PLACE",
            operation_id="RuntimeApproval00000000000000001",
            client_order_id="RuntimeApproval00000000000000001",
            instrument_id="BTC-USDT-SWAP",
            state="PREPARED",
            request_digest="5" * 64,
            safe_request_snapshot={},
            safe_response_snapshot={},
            attempt_count=1,
            lease_generation=1,
            close_sequence=0,
            last_attempt_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db.commit()
    adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)
    adapter._order_submission_enabled = True
    adapter._now_provider = lambda: NOW
    adapter._writer_instance_id = "RuntimeWriter01"
    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "_approved_execution_by_id",
        lambda *_args, **_kwargs: _runtime_approved_execution(),
    )
    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "_next_unconsumed_approved_execution",
        lambda *_args, **_kwargs: pytest.fail(
            "new approval selection must not run before unresolved recovery"
        ),
    )
    monkeypatch.setattr(
        adapter,
        "_advance_controlled_canary",
        lambda *_args, **_kwargs: pytest.fail(
            "lifecycle advancement must not run before unresolved recovery"
        ),
    )

    class Writer:
        calls = []

        def reconcile_unresolved(self, attempt_id):
            self.calls.append(attempt_id)

    writer = Writer()
    adapter.run_cycle(read_client=object(), writer=writer, db=db)

    assert len(writer.calls) == 1
    assert writer.calls == [1]


def test_runtime_stops_after_one_controlled_canary_transition(
    db,
    monkeypatch,
) -> None:
    adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)
    adapter._now_provider = lambda: NOW
    transitions = []
    monkeypatch.setattr(
        adapter,
        "_advance_controlled_canary",
        lambda *_args, **_kwargs: transitions.append("advanced") or True,
    )
    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "_next_unconsumed_approved_execution",
        lambda *_args, **_kwargs: pytest.fail(
            "opening selection must not follow a lifecycle transition"
        ),
    )

    class Writer:
        def __getattr__(self, name):
            pytest.fail("writer action {} must wait for the next cycle".format(name))

    adapter.run_cycle(read_client=object(), writer=Writer(), db=db)
    assert transitions == ["advanced"]


def test_runtime_residual_canary_waits_for_fresh_grant_then_uses_it(
    db,
    monkeypatch,
) -> None:
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
    run = ReconciliationRun(
        execution_target_id="OKX_DEMO",
        status="DRIFTED",
        summary_snapshot={},
        database_ids={},
        artifact_status="READY",
        artifact_sha256="a" * 64,
        authoritative_observed_at=NOW,
        source_type="api_aggregate",
        core_data=True,
        started_at=NOW,
        completed_at=NOW,
    )
    db.add(run)
    db.flush()
    state = OkxDemoReconciliationState(
        execution_target_id="OKX_DEMO",
        status="DRIFTED",
        opening_frozen=True,
        block_reason="controlled canary cleanup",
        last_reconciliation_run_id=run.id,
    )
    old_grant = OkxDemoRecoveryGrant(
        execution_target_id="OKX_DEMO",
        reconciliation_run_id=run.id,
        lifecycle_id="L" * 32,
        grant_digest="b" * 64,
        action="REDUCE_ONLY",
        instrument_id="BTC-USDT-SWAP",
        position_side="long",
        max_quantity=Decimal("1"),
        status="CONSUMED",
        expires_at=NOW + timedelta(minutes=1),
        consumed_at=NOW,
    )
    db.add_all((state, old_grant))
    db.flush()
    db.add(
        OkxOrderWriteAttempt(
            execution_target_id="OKX_DEMO",
            exchange_order_row_id=901,
            approval_id=41,
            recovery_grant_database_id=old_grant.database_id,
            operation="CLOSE",
            operation_id="rcv00000000000000000001",
            client_order_id="rcv00000000000000000001",
            instrument_id="BTC-USDT-SWAP",
            state="RESIDUAL_CLOSE_REQUIRED",
            request_digest="c" * 64,
            safe_request_snapshot={},
            safe_response_snapshot={"accumulated_fill_size": "0.4"},
            attempt_count=1,
            lease_generation=1,
            close_sequence=0,
            last_attempt_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db.commit()
    adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)
    advances = []
    monkeypatch.setattr(
        adapter,
        "_advance_controlled_canary",
        lambda *_args, **_kwargs: advances.append("issued") or True,
    )

    class Writer:
        calls = []

        def recovery_reduce_only(self, *, recovery_grant_database_id):
            self.calls.append(recovery_grant_database_id)

    writer = Writer()
    adapter.run_cycle(read_client=object(), writer=writer, db=db)
    assert advances == ["issued"]
    assert writer.calls == []

    fresh_grant = OkxDemoRecoveryGrant(
        execution_target_id="OKX_DEMO",
        reconciliation_run_id=run.id,
        lifecycle_id="L" * 32,
        grant_digest="d" * 64,
        action="REDUCE_ONLY",
        instrument_id="BTC-USDT-SWAP",
        position_side="long",
        max_quantity=Decimal("0.6"),
        status="ACTIVE",
        expires_at=NOW + timedelta(minutes=1),
    )
    db.add(fresh_grant)
    db.commit()
    adapter.run_cycle(read_client=object(), writer=writer, db=db)
    assert writer.calls == [fresh_grant.database_id]


def test_runtime_recovery_exhausted_restart_is_handled_noop(db) -> None:
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
    run = ReconciliationRun(
        execution_target_id="OKX_DEMO",
        status="DRIFTED",
        summary_snapshot={},
        database_ids={},
        artifact_status="READY",
        artifact_sha256="a" * 64,
        authoritative_observed_at=NOW,
        source_type="api_aggregate",
        core_data=True,
        started_at=NOW,
        completed_at=NOW,
    )
    db.add(run)
    db.flush()
    lifecycle_id = "E" * 32
    db.add(
        OkxDemoCanaryLifecycle(
            lifecycle_id=lifecycle_id,
            execution_target_id="OKX_DEMO",
            submission_grant_id="S" * 32,
            opening_approval_id=1,
            opening_trade_intent_id=1,
            opening_exchange_order_row_id=1,
            baseline_reconciliation_run_id=run.id,
            baseline_position_quantity=Decimal("0"),
            baseline_evidence_digest="b" * 64,
            opening_order_identity_digest="c" * 64,
            fill_attribution_digest="d" * 64,
            attributed_fill_quantity=Decimal("1"),
            max_quantity=Decimal("1"),
            cleanup_trade_intent_id=2,
            cleanup_approval_id=2,
            cleanup_exchange_order_row_id=2,
            outcome="FAILED",
            cleanup_phase="RECOVERY_EXHAUSTED",
            failure_code="CLEANUP_LIMIT_REACHED",
            deadline_at=NOW - timedelta(seconds=1),
            fencing_version=9,
            created_at=NOW - timedelta(minutes=1),
            updated_at=NOW,
        )
    )
    grant = OkxDemoRecoveryGrant(
        execution_target_id="OKX_DEMO",
        reconciliation_run_id=run.id,
        lifecycle_id=lifecycle_id,
        grant_digest="f" * 64,
        action="REDUCE_ONLY",
        instrument_id="BTC-USDT-SWAP",
        position_side="long",
        max_quantity=Decimal("0.1"),
        status="CONSUMED",
        expires_at=NOW + timedelta(minutes=1),
        consumed_at=NOW,
    )
    state = OkxDemoReconciliationState(
        execution_target_id="OKX_DEMO",
        status="DRIFTED",
        opening_frozen=True,
        block_reason="CLEANUP_LIMIT_REACHED",
        last_reconciliation_run_id=run.id,
    )
    db.add_all((grant, state))
    db.flush()
    db.add(
        OkxOrderWriteAttempt(
            execution_target_id="OKX_DEMO",
            exchange_order_row_id=3,
            approval_id=2,
            recovery_grant_database_id=grant.database_id,
            operation="CLOSE",
            operation_id="rcv00000000000000000003C3",
            client_order_id="rcv00000000000000000003C3",
            instrument_id="BTC-USDT-SWAP",
            state="RESIDUAL_CLOSE_REQUIRED",
            request_digest="1" * 64,
            safe_request_snapshot={},
            safe_response_snapshot={"accumulated_fill_size": "0.1"},
            attempt_count=1,
            lease_generation=1,
            close_sequence=3,
            last_attempt_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db.commit()
    adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)

    class Writer:
        def __getattr__(self, name):
            pytest.fail("RECOVERY_EXHAUSTED restart must not call {}".format(name))

    adapter.run_cycle(read_client=object(), writer=Writer(), db=db)
    adapter.run_cycle(read_client=object(), writer=Writer(), db=db)
    assert db.scalars(
        select(OkxDemoRecoveryGrant).where(
            OkxDemoRecoveryGrant.status == "ACTIVE"
        )
    ).all() == []


def _active_runtime_deployment(db) -> StrategyDeployment:
    deployment = StrategyDeployment(
        execution_target_id="OKX_DEMO",
        candidate_approval_id=71,
        strategy_id=72,
        strategy_version_id=73,
        candidate_digest="6" * 64,
        promotion_policy_version="promotion-v1",
        deployment_policy_digest="7" * 64,
        instrument_id="BTC-USDT-SWAP",
        timeframe="5m",
        status="ACTIVE",
        evidence_snapshot={},
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(deployment)
    db.commit()
    return deployment


def test_runtime_enqueues_same_closed_candle_once_and_no_action_does_not_place(
    db,
    monkeypatch,
) -> None:
    deployment = _active_runtime_deployment(db)

    class Repository:
        identities = {}
        claimed = False

        def __init__(self, _db):
            pass

        def enqueue_evaluation(self, deployment_id, *, closed_candle_at):
            identity = (deployment_id, closed_candle_at)
            self.identities.setdefault(identity, len(self.identities) + 1)

        def claim_next(self, *, owner, lease_seconds, now):
            assert owner.startswith("RuntimeSignal")
            assert lease_seconds == 30
            if self.claimed:
                return None
            self.__class__.claimed = True
            return SimpleNamespace(
                id=81,
                lease_token="lease-token",
                fencing_sequence=1,
            )

    class Orchestrator:
        calls = []

        def __init__(self, _db, *, read_client, deployment_repository):
            pass

        def process(self, evaluation_id, **kwargs):
            self.calls.append((evaluation_id, kwargs))
            return SimpleNamespace(status="NO_ACTION")

    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "StrategyDeploymentRepository",
        Repository,
    )
    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "OkxDemoExecutionOrchestrator",
        Orchestrator,
    )
    adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)
    adapter._order_submission_enabled = False
    adapter._now_provider = lambda: NOW + timedelta(minutes=3)
    adapter._writer_instance_id = "RuntimeWriter01"
    adapter._signal_lease_owner = "RuntimeSignal01"

    class Writer:
        calls = []

        def place(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    writer = Writer()
    adapter.run_cycle(read_client=object(), writer=writer, db=db)
    adapter.run_cycle(read_client=object(), writer=writer, db=db)

    assert len(Repository.identities) == 1
    assert next(iter(Repository.identities)) == (
        deployment.id,
        datetime(2026, 7, 27, 11, 55, tzinfo=timezone.utc),
    )
    assert len(Orchestrator.calls) == 1
    assert writer.calls == []


def test_runtime_actionable_evaluation_defers_approval_to_next_cycle(
    db,
    monkeypatch,
) -> None:
    _active_runtime_deployment(db)
    _allow_fresh_runtime_opening(db)

    class Repository:
        claimed = False

        def __init__(self, _db):
            pass

        def enqueue_evaluation(self, deployment_id, *, closed_candle_at):
            pass

        def claim_next(self, **_kwargs):
            if self.claimed:
                return None
            self.__class__.claimed = True
            return SimpleNamespace(
                id=82,
                lease_token="lease-token",
                fencing_sequence=1,
            )

    class Orchestrator:
        def __init__(self, _db, **_kwargs):
            pass

        def process(self, *_args, **_kwargs):
            return SimpleNamespace(status="ACTIONABLE")

    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "StrategyDeploymentRepository",
        Repository,
    )
    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "OkxDemoExecutionOrchestrator",
        Orchestrator,
    )
    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "_next_unconsumed_approved_execution",
        lambda *_args, **_kwargs: _runtime_approved_execution(),
    )
    adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)
    adapter._order_submission_enabled = True
    adapter._now_provider = lambda: NOW
    adapter._writer_instance_id = "RuntimeWriter01"
    adapter._signal_lease_owner = "RuntimeSignal01"

    class Writer:
        calls = []

        def place(self, approved, *, submission_grant):
            self.calls.append((approved, submission_grant))

    writer = Writer()
    adapter.run_cycle(read_client=object(), writer=writer, db=db)
    assert writer.calls == []
    adapter.run_cycle(read_client=object(), writer=writer, db=db)
    assert len(writer.calls) == 1


@pytest.mark.parametrize("failure_point", ["lease", "service"])
def test_runtime_signal_failure_is_fail_closed_and_never_places(
    db,
    monkeypatch,
    failure_point,
) -> None:
    _active_runtime_deployment(db)

    class Repository:
        def __init__(self, _db):
            pass

        def enqueue_evaluation(self, deployment_id, *, closed_candle_at):
            pass

        def claim_next(self, **_kwargs):
            if failure_point == "lease":
                raise RuntimeError("lease failed")
            return SimpleNamespace(
                id=83,
                lease_token="lease-token",
                fencing_sequence=1,
            )

    class Orchestrator:
        def __init__(self, _db, **_kwargs):
            pass

        def process(self, *_args, **_kwargs):
            raise RuntimeError("evaluation completion failed")

    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "StrategyDeploymentRepository",
        Repository,
    )
    monkeypatch.setattr(
        "app.adapters.okx_demo.reconciliation_runtime."
        "OkxDemoExecutionOrchestrator",
        Orchestrator,
    )
    adapter = object.__new__(OkxDemoRuntimeReconciliationAdapter)
    adapter._order_submission_enabled = True
    adapter._now_provider = lambda: NOW
    adapter._writer_instance_id = "RuntimeWriter01"
    adapter._signal_lease_owner = "RuntimeSignal01"

    class Writer:
        calls = []

        def place(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    writer = Writer()
    with pytest.raises(RuntimeError):
        adapter.run_cycle(read_client=object(), writer=writer, db=db)
    assert writer.calls == []
