from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.execution_lineage import ExecutionScope, ReconciliationRun
from app.models.okx_demo_reconciliation import (
    OkxDemoAccountSnapshot,
    OkxDemoExchangeEvent,
    OkxDemoReconciliationState,
    OkxDemoRecoveryGrant,
)
from app.services.okx_demo_reconciliation_compaction import (
    ReconciliationCompactionBlocked,
    apply_compaction,
    build_compaction_plan,
)


NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)


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
        session.add(
            ExecutionScope(
                scope_id="OKX_DEMO",
                scope_kind="EXCHANGE_TARGET",
                exchange_capable=True,
                executable=False,
                exchange_writes=False,
                order_submission_authorized=False,
            )
        )
        session.flush()
        yield session
    engine.dispose()


def _add_account_event(db, *, generation, source="REST"):
    event = OkxDemoExchangeEvent(
        execution_target_id="OKX_DEMO",
        event_key=("{:064x}".format(generation + (1000 if source == "WS" else 0))),
        source=source,
        entity_kind="ACCOUNT",
        entity_key="account:USDT",
        source_sequence=generation if source == "WS" else None,
        stream_generation=generation,
        payload={"accountFingerprint": "a" * 64, "equity": "100", "availableBalance": "90", "marginBalance": "100"},
        payload_digest="b" * 64,
        observed_at=NOW + timedelta(minutes=generation),
        received_at=NOW + timedelta(minutes=generation),
    )
    db.add(event)
    db.flush()
    snapshot = OkxDemoAccountSnapshot(
        execution_target_id="OKX_DEMO",
        event_database_id=event.database_id,
        account_fingerprint_sha256="a" * 64,
        equity="100",
        available_balance="90",
        margin_balance="100",
        authoritative_snapshot=event.payload,
        observed_at=event.observed_at,
    )
    db.add(snapshot)
    db.flush()
    run = ReconciliationRun(
        execution_target_id="OKX_DEMO",
        status="RECONCILED",
        summary_snapshot={},
        database_ids={
            "exchange_events": [event.database_id],
            "account_snapshots": [snapshot.database_id],
        },
        artifact_path="/evidence/run-{}.json".format(generation),
        artifact_sha256="c" * 64,
        artifact_status="READY",
        source_type="api_aggregate",
        core_data=True,
        started_at=NOW,
        completed_at=NOW,
    )
    db.add(run)
    db.flush()
    return event, snapshot, run


def test_dry_run_only_selects_old_rest_duplicate_and_keeps_current_evidence(db):
    old_event, old_snapshot, old_run = _add_account_event(db, generation=1)
    current_event, _current_snapshot, current_run = _add_account_event(db, generation=2)
    db.add(
        OkxDemoReconciliationState(
            execution_target_id="OKX_DEMO",
            status="RECONCILED",
            opening_frozen=False,
            last_reconciliation_run_id=current_run.id,
            last_event_observed_at=current_event.observed_at,
        )
    )
    db.flush()

    plan = build_compaction_plan(db, retain_generations=1)

    assert plan.delete_event_ids == (old_event.database_id,)
    assert plan.delete_run_ids == (old_run.id,)
    assert plan.delete_snapshot_counts == {
        "order_snapshots": 0,
        "fill_snapshots": 0,
        "position_snapshots": 0,
        "account_snapshots": 1,
    }
    assert current_run.id in plan.protected_run_ids
    assert current_event.database_id in plan.protected_event_ids
    assert plan.retained_artifact_paths == (
        "/evidence/run-1.json",
        "/evidence/run-2.json",
    )
    # Planning is strictly read-only.
    assert db.get(OkxDemoExchangeEvent, old_event.database_id) is not None
    assert db.get(OkxDemoAccountSnapshot, old_snapshot.database_id) is not None
    assert db.get(ReconciliationRun, old_run.id) is not None


def test_recovery_lineage_closes_over_its_reconciliation_run(db):
    old_event, _old_snapshot, old_run = _add_account_event(db, generation=1)
    current_event, _current_snapshot, current_run = _add_account_event(db, generation=2)
    db.add(
        OkxDemoReconciliationState(
            execution_target_id="OKX_DEMO",
            status="RECONCILED",
            opening_frozen=False,
            last_reconciliation_run_id=current_run.id,
            last_event_observed_at=current_event.observed_at,
        )
    )
    db.add(
        OkxDemoRecoveryGrant(
            execution_target_id="OKX_DEMO",
            reconciliation_run_id=old_run.id,
            grant_digest="d" * 64,
            action="CANCEL",
            instrument_id="BTC-USDT-SWAP",
            position_side="long",
            max_quantity="0",
            status="CONSUMED",
            expires_at=NOW + timedelta(days=1),
            consumed_at=NOW,
        )
    )
    db.flush()

    plan = build_compaction_plan(db, retain_generations=1)

    assert old_run.id in plan.protected_run_ids
    assert old_event.database_id in plan.protected_event_ids
    assert plan.delete_run_ids == ()
    assert plan.delete_event_ids == ()


def test_websocket_or_unproven_history_is_never_selected(db):
    old_event, _old_snapshot, old_run = _add_account_event(
        db, generation=1, source="WS"
    )
    current_event, _current_snapshot, current_run = _add_account_event(db, generation=2)
    db.add(
        OkxDemoReconciliationState(
            execution_target_id="OKX_DEMO",
            status="RECONCILED",
            opening_frozen=False,
            last_reconciliation_run_id=current_run.id,
            last_event_observed_at=current_event.observed_at,
        )
    )
    db.flush()

    plan = build_compaction_plan(db, retain_generations=1)

    assert old_event.database_id not in plan.delete_event_ids
    assert old_run.id not in plan.delete_run_ids


def test_apply_refuses_non_postgresql_even_with_a_valid_plan(db):
    _add_account_event(db, generation=1)
    plan = build_compaction_plan(db)

    with pytest.raises(ReconciliationCompactionBlocked, match="PostgreSQL"):
        apply_compaction(db, plan)
