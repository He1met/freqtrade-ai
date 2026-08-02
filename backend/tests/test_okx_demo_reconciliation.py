from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    ExchangeFill,
    ExchangeOrder,
    ExchangePosition,
    OkxDemoCanaryLifecycle,
    OkxDemoExchangeEvent,
    OkxDemoPositionSnapshot,
    OkxDemoReconciliationState,
    OkxDemoRecoveryBatch,
    OkxDemoRecoveryGrant,
    OkxOrderWriteAttempt,
    ReconciliationRun,
)
from app.models.execution_lineage import OKX_DEMO_TARGET_ID
from app.api.okx_demo_reconciliation import (
    exchange_state,
    latest_reconciliation,
    reconciliation_run,
)
from app.repositories.execution_lineage import (
    ExecutionLineageRepository,
    ensure_execution_scope_catalog,
)
from app.services.okx_demo_reconciliation import (
    OkxDemoReconciliationBlocked,
    OkxDemoReconciliationService,
    SCHEMA_VERSION,
)


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def _event(
    kind: str,
    entity_key: str,
    payload: dict,
    observed_at: datetime,
    *,
    source: str = "REST",
    source_sequence: int = 1,
    stream_generation: int = 1,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_target": OKX_DEMO_TARGET_ID,
        "source": source,
        "entity_kind": kind,
        "entity_key": entity_key,
        "source_sequence": source_sequence,
        "stream_generation": stream_generation,
        "observed_at": observed_at.isoformat(),
        "received_at": (observed_at + timedelta(seconds=1)).isoformat(),
        "payload": payload,
    }


def _position(quantity: str) -> dict:
    return {
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "pos": quantity,
        "avgPx": "50000" if Decimal(quantity) else "",
    }


def _account() -> dict:
    return {
        "accountFingerprint": "a" * 64,
        "equity": "10000",
        "availableBalance": "9000",
        "marginBalance": "1000",
    }


def _fill() -> dict:
    return {
        "fillId": "external-fill-1",
        "ordId": "external-order-1",
        "instId": "BTC-USDT-SWAP",
        "fillPx": "50000",
        "fillSz": "1",
        "fee": "-0.01",
    }


def _order(
    *,
    order_id: str = "managed-order-1",
    client_order_id: str = "ManagedFill1",
) -> dict:
    return {
        "ordId": order_id,
        "clOrdId": client_order_id,
        "instId": "BTC-USDT-SWAP",
        "state": "filled",
        "sz": "1",
        "accFillSz": "1",
        "avgPx": "50000",
        "reduceOnly": False,
    }


def _managed_order(
    db,
    *,
    order_id: str = "managed-order-1",
    client_order_id: str = "ManagedFill1",
) -> ExchangeOrder:
    repository = ExecutionLineageRepository(db, OKX_DEMO_TARGET_ID)
    intent = repository.create_trade_intent(
        client_order_id=client_order_id,
        instrument_id="BTC-USDT-SWAP",
        side="buy",
        position_side="long",
        order_type="market",
        quantity=Decimal("1"),
    )
    return repository.record_order(
        trade_intent_id=intent.id,
        client_order_id=client_order_id,
        exchange_order_id=order_id,
        status="filled",
    )


def test_event_ingest_is_idempotent_and_out_of_order_never_rewinds_projection(
    session_factory,
    tmp_path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    newer = _event(
        "POSITION",
        "BTC-USDT-SWAP:long",
        _position("0"),
        now,
        source="WS",
        source_sequence=2,
    )
    older = _event(
        "POSITION",
        "BTC-USDT-SWAP:long",
        _position("1"),
        now - timedelta(seconds=1),
        source="WS",
        source_sequence=1,
    )
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(
            db,
            evidence_root=tmp_path / "evidence",
            allowed_evidence_root=tmp_path,
        )
        first = service.ingest_event(newer)
        duplicate = service.ingest_event(newer)
        delayed = service.ingest_event(older)
        service.ingest_event(_event("ACCOUNT", "account", _account(), now))
        result = service.reconcile(now=now)
        assert duplicate.database_id == first.database_id
        assert duplicate.duplicate is True
        assert delayed.out_of_order is True
        assert result.status == "UNKNOWN"
    with session_factory() as db:
        assert len(db.scalars(select(OkxDemoExchangeEvent)).all()) == 3
        latest = db.scalars(
            select(OkxDemoPositionSnapshot).order_by(
                OkxDemoPositionSnapshot.observed_at.desc()
            )
        ).first()
        assert latest is not None and latest.quantity == 0


@pytest.mark.parametrize(
    "conflict",
    ["SAME_TIMESTAMP", "OLD_GENERATION", "FUTURE"],
)
def test_ambiguous_event_identity_freezes_opening_gate(
    session_factory,
    conflict,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(db)
        service.ingest_event(
            _event(
                "POSITION",
                "BTC-USDT-SWAP:long",
                _position("0"),
                now,
                source="WS",
                stream_generation=2,
            )
        )
        if conflict == "SAME_TIMESTAMP":
            invalid = _event(
                "POSITION",
                "BTC-USDT-SWAP:long",
                _position("1"),
                now,
                source="WS",
                stream_generation=2,
            )
        elif conflict == "OLD_GENERATION":
            invalid = _event(
                "POSITION",
                "BTC-USDT-SWAP:long",
                _position("0"),
                now + timedelta(seconds=1),
                source="WS",
                stream_generation=1,
            )
        else:
            invalid = _event(
                "ACCOUNT",
                "account",
                _account(),
                now + timedelta(minutes=2),
            )
            invalid["received_at"] = now.isoformat()
        with pytest.raises(OkxDemoReconciliationBlocked):
            service.ingest_event(invalid)
    with session_factory() as db:
        state = db.scalar(select(OkxDemoReconciliationState))
        assert state is not None
        assert state.status == "UNKNOWN"
        assert state.opening_frozen is True


def test_position_drift_is_blocked_without_overwriting_local_funds_state(
    session_factory,
    tmp_path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    with session_factory.begin() as db:
        ensure_execution_scope_catalog(db)
        local = ExchangePosition(
            execution_target_id=OKX_DEMO_TARGET_ID,
            instrument_id="BTC-USDT-SWAP",
            position_side="long",
            quantity=Decimal("1"),
            average_price=Decimal("50000"),
            snapshot={"source": "local-writer"},
            observed_at=now - timedelta(seconds=1),
        )
        db.add(local)
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(
            db,
            evidence_root=tmp_path / "evidence",
            allowed_evidence_root=tmp_path,
        )
        service.ingest_recovery_batch(
            [
                _event(
                    "POSITION",
                    "BTC-USDT-SWAP:long",
                    _position("2"),
                    now,
                ),
                _event("ACCOUNT", "account", _account(), now),
            ],
            recovery_batch_id="drift-complete-baseline",
            high_watermarks=_high_watermarks(),
            overlap_started_at=now - timedelta(minutes=1),
            observed_at=now,
            completed_at=now,
        )
        result = service.reconcile(now=now)
        assert result.status == "DRIFTED"
        assert result.opening_frozen is True
        assert any(item["code"] == "POSITION_DRIFT" for item in result.findings)
        grant_database_id = result.database_ids["recovery_grants"][0]
    with session_factory() as db:
        grant = db.get(OkxDemoRecoveryGrant, grant_database_id)
        assert grant.status == "ACTIVE"
        assert db.scalar(select(OkxOrderWriteAttempt)) is None
    with session_factory() as db:
        local = db.scalar(select(ExchangePosition))
        run = db.scalar(select(ReconciliationRun))
        state = db.scalar(select(OkxDemoReconciliationState))
        assert local is not None and local.quantity == 1
        assert state is not None and state.opening_frozen is True
        assert run is not None
        artifact = json.loads(open(run.artifact_path, encoding="utf-8").read())
        payload = open(run.artifact_path, "rb").read()
        assert hashlib.sha256(payload).hexdigest() == run.artifact_sha256
        assert artifact["database_ids"] == run.database_ids
        assert artifact["execution_target"] == OKX_DEMO_TARGET_ID


def test_controlled_canary_finding_is_artifact_backed_and_suppresses_generic_grants(
    session_factory,
    tmp_path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    lifecycle_id = "9" * 32
    with session_factory.begin() as db:
        ensure_execution_scope_catalog(db)
        order = _managed_order(db)
        db.add(
            OkxDemoCanaryLifecycle(
                lifecycle_id=lifecycle_id,
                execution_target_id="OKX_DEMO",
                submission_grant_id="8" * 32,
                opening_approval_id=71,
                opening_trade_intent_id=order.trade_intent_id,
                opening_exchange_order_row_id=order.id,
                baseline_reconciliation_run_id=72,
                baseline_position_quantity=Decimal("0"),
                baseline_evidence_digest="a" * 64,
                opening_order_identity_digest="b" * 64,
                attributed_fill_quantity=Decimal("0"),
                max_quantity=Decimal("1"),
                outcome="PENDING",
                cleanup_phase="OPENING_SUBMITTED",
                deadline_at=now - timedelta(seconds=1),
                fencing_version=2,
                created_at=now - timedelta(seconds=2),
                updated_at=now - timedelta(seconds=1),
            )
        )
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(
            db,
            evidence_root=tmp_path / "evidence",
            allowed_evidence_root=tmp_path,
        )
        service.ingest_recovery_batch(
            [
                _event(
                    "ORDER",
                    "managed-order-1",
                    {
                        **_order(),
                        "state": "partially_filled",
                        "accFillSz": "0.4",
                    },
                    now,
                ),
                _event(
                    "FILL",
                    "raw-canary-fill-id",
                    {
                        "fillId": "raw-canary-fill-id",
                        "ordId": "managed-order-1",
                        "instId": "BTC-USDT-SWAP",
                        "fillPx": "50000",
                        "fillSz": "0.4",
                        "fee": "-0.01",
                    },
                    now,
                ),
                _event(
                    "ORDER",
                    "raw-foreign-canary-order",
                    {
                        **_order(
                            order_id="raw-foreign-canary-order",
                            client_order_id="RawForeignCanaryClient",
                        ),
                        "state": "live",
                        "accFillSz": "0",
                    },
                    now,
                    source_sequence=2,
                ),
                _event(
                    "POSITION",
                    "BTC-USDT-SWAP:long",
                    _position("0"),
                    now,
                ),
                _event("ACCOUNT", "account", _account(), now),
            ],
            recovery_batch_id="controlled-canary-deadline",
            high_watermarks=_high_watermarks(),
            overlap_started_at=now - timedelta(seconds=1),
            observed_at=now,
            completed_at=now,
        )
        result = service.reconcile(now=now)
        assert result.status == "DRIFTED"
        assert result.database_ids["recovery_grants"] == []
        expected_identity = "canary:{}".format(
            hashlib.sha256(lifecycle_id.encode()).hexdigest()[:16]
        )
        assert {
            "code": "CONTROLLED_CANARY_DEADLINE_CANCEL_REQUIRED",
            "severity": "BLOCKED",
            "identity": expected_identity,
        } in result.findings
        artifact = json.loads(open(result.artifact_path, encoding="utf-8").read())
        assert artifact["findings"] == list(result.findings)
        artifact_text = json.dumps(artifact)
        assert lifecycle_id not in artifact_text
        assert "managed-order-1" not in artifact_text
        assert "ManagedFill1" not in artifact_text
        assert "raw-canary-fill-id" not in artifact_text
        assert "raw-foreign-canary-order" not in artifact_text
        assert "RawForeignCanaryClient" not in artifact_text
        assert any(
            item["code"] == "AUTHORITATIVE_OPEN_ORDER_MISSING_LOCALLY"
            and item["identity"].startswith("canary-ref:")
            for item in artifact["findings"]
        )
        assert db.scalars(select(OkxDemoRecoveryGrant)).all() == []


def test_net_mode_position_snapshot_is_rejected_before_persistence(
    session_factory,
    tmp_path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    payload = _position("1")
    payload["posSide"] = "net"
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(
            db,
            evidence_root=tmp_path / "evidence",
            allowed_evidence_root=tmp_path,
        )
        with pytest.raises(
            OkxDemoReconciliationBlocked,
            match="could not be persisted exactly once",
        ):
            service.ingest_event(
                _event("POSITION", "BTC-USDT-SWAP:net", payload, now)
            )
        assert db.scalar(select(OkxDemoPositionSnapshot)) is None


def test_complete_rest_recovery_batch_restores_gate_after_restart(
    session_factory,
    tmp_path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    events = [
        _event(
            "POSITION",
            "BTC-USDT-SWAP:long",
            _position("0"),
            now,
        ),
        _event("ACCOUNT", "account", _account(), now),
        _event("FILL", "external-fill-1", _fill(), now),
    ]
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(
            db,
            evidence_root=tmp_path / "evidence",
            allowed_evidence_root=tmp_path,
        )
        service.mark_stream_stale(
            observed_at=now - timedelta(seconds=1),
            reason="private websocket disconnected",
        )
        ingested = service.ingest_recovery_batch(
            events,
            recovery_batch_id="restart-rest-baseline",
            high_watermarks={
                **_high_watermarks(),
            },
            overlap_started_at=now - timedelta(minutes=1),
            observed_at=now,
            completed_at=now,
        )
        result = service.reconcile(
            now=now,
            recovered=True,
        )
        assert len(ingested) == 3
        assert result.status == "RECOVERED"
        assert result.opening_frozen is False
        assert len(result.database_ids["fill_snapshots"]) == 1
        assert db.scalar(select(ExchangeFill)) is None


def test_fill_drift_only_applies_to_locally_managed_orders() -> None:
    class Result:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

    results = iter((Result(["managed-order"]), Result([])))
    service = OkxDemoReconciliationService(
        SimpleNamespace(scalars=lambda _query: next(results))
    )
    findings = []

    service._compare_fills(
        {
            "managed-fill": SimpleNamespace(exchange_order_id="managed-order"),
            "external-fill": SimpleNamespace(exchange_order_id="external-order"),
        },
        findings,
    )

    assert [item["identity"] for item in findings] == ["managed-fill"]


def test_empty_complete_streams_are_durable_but_cannot_fake_account_readiness(
    session_factory,
    tmp_path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(
            db,
            evidence_root=tmp_path / "managed" / "evidence",
            allowed_evidence_root=tmp_path / "managed",
        )
        assert service.ingest_recovery_batch(
            [],
            recovery_batch_id="complete-empty-streams",
            high_watermarks=_high_watermarks(),
            overlap_started_at=now - timedelta(minutes=1),
            observed_at=now,
            completed_at=now,
        ) == []
        result = service.reconcile(now=now, recovered=True)
        assert result.status == "UNKNOWN"
        assert result.opening_frozen is True
        assert result.database_ids["recovery_batches"]
    with session_factory() as db:
        assert db.scalar(select(ExchangeFill)) is None
        batch = db.scalar(select(OkxDemoRecoveryBatch))
        assert batch is not None and batch.event_count == 0
        assert set(batch.complete_streams) == {
            "ORDER",
            "FILL",
            "POSITION",
            "ACCOUNT",
        }


def test_managed_fill_is_bridged_exactly_once_before_drift_comparison(
    session_factory,
    tmp_path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    fill = _fill()
    fill.update({"fillId": "managed-fill-1", "ordId": "managed-order-1"})
    with session_factory.begin() as db:
        _managed_order(db)
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(
            db,
            evidence_root=tmp_path / "evidence",
            allowed_evidence_root=tmp_path,
        )
        service.ingest_recovery_batch(
            [
                _event("ORDER", "managed-order-1", _order(), now),
                _event("FILL", "managed-fill-1", fill, now),
                _event("POSITION", "BTC-USDT-SWAP:long", _position("0"), now),
                _event("ACCOUNT", "account", _account(), now),
            ],
            recovery_batch_id="managed-fill-baseline",
            high_watermarks=_high_watermarks(),
            overlap_started_at=now - timedelta(minutes=1),
            observed_at=now,
            completed_at=now,
        )
        first = service.reconcile(now=now)
        first_fill = db.scalar(select(ExchangeFill))
        first_provenance = dict(first_fill.snapshot)
        service.ingest_recovery_batch(
            [
                _event(
                    "ORDER",
                    "managed-order-1",
                    _order(),
                    now + timedelta(seconds=1),
                    stream_generation=2,
                ),
                _event(
                    "FILL",
                    "managed-fill-1",
                    fill,
                    now + timedelta(seconds=1),
                    stream_generation=2,
                ),
                _event(
                    "POSITION",
                    "BTC-USDT-SWAP:long",
                    _position("0"),
                    now + timedelta(seconds=1),
                    stream_generation=2,
                ),
                _event(
                    "ACCOUNT",
                    "account",
                    _account(),
                    now + timedelta(seconds=1),
                    stream_generation=2,
                ),
            ],
            recovery_batch_id="managed-fill-next-generation",
            high_watermarks=_high_watermarks(),
            overlap_started_at=now,
            observed_at=now + timedelta(seconds=1),
            completed_at=now + timedelta(seconds=1),
        )
        second = service.reconcile(now=now + timedelta(seconds=1))
        assert first.status == "RECONCILED"
        assert second.status == "RECONCILED"
        rows = list(db.scalars(select(ExchangeFill)).all())
        assert len(rows) == 1
        assert rows[0].snapshot == first_provenance
        assert rows[0].exchange_fill_id == "managed-fill-1"
        assert rows[0].exchange_order_row_id == db.scalar(
            select(ExchangeOrder.id).where(
                ExchangeOrder.exchange_order_id == "managed-order-1"
            )
        )
        assert rows[0].snapshot["fill_snapshot_database_id"] > 0
        assert len(rows[0].snapshot["payload_digest"]) == 64


@pytest.mark.parametrize("conflict_kind", ["content", "parent"])
def test_managed_fill_lineage_conflict_freezes_openings(
    session_factory,
    tmp_path,
    conflict_kind,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    fill = _fill()
    fill.update({"fillId": "managed-fill-1", "ordId": "managed-order-1"})
    with session_factory.begin() as db:
        expected_order = _managed_order(db)
        other_order = _managed_order(
            db,
            order_id="managed-order-2",
            client_order_id="ManagedFill2",
        )
        db.add(
            ExchangeFill(
                execution_target_id=OKX_DEMO_TARGET_ID,
                exchange_order_row_id=(
                    other_order.id if conflict_kind == "parent" else expected_order.id
                ),
                exchange_fill_id="managed-fill-1",
                price=Decimal("50001" if conflict_kind == "content" else "50000"),
                quantity=Decimal("1"),
                fee=Decimal("-0.01"),
                snapshot={"source": "conflicting-local-evidence"},
            )
        )
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(
            db,
            evidence_root=tmp_path / conflict_kind / "evidence",
            allowed_evidence_root=tmp_path,
        )
        service.ingest_recovery_batch(
            [
                _event("ORDER", "managed-order-1", _order(), now),
                _event("FILL", "managed-fill-1", fill, now),
                _event("POSITION", "BTC-USDT-SWAP:long", _position("0"), now),
                _event("ACCOUNT", "account", _account(), now),
            ],
            recovery_batch_id="managed-fill-conflict-{}".format(conflict_kind),
            high_watermarks=_high_watermarks(),
            overlap_started_at=now - timedelta(minutes=1),
            observed_at=now,
            completed_at=now,
        )
        result = service.reconcile(now=now)
        assert result.status == "DRIFTED"
        assert result.opening_frozen is True
        assert any(
            item["code"] == "AUTHORITATIVE_FILL_LINEAGE_CONFLICT"
            and item["identity"] == "managed-fill-1"
            for item in result.findings
        )
        assert db.scalar(select(ExchangeFill)).snapshot == {
            "source": "conflicting-local-evidence"
        }


def test_only_one_fresh_authenticated_four_stream_batch_can_unlock(
    session_factory,
    tmp_path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    events = [
        _event(
            "POSITION",
            "BTC-USDT-SWAP:long",
            _position("0"),
            now - timedelta(minutes=3),
        ),
        _event(
            "ACCOUNT",
            "account",
            _account(),
            now - timedelta(minutes=3),
            source_sequence=2,
        ),
    ]
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(
            db,
            evidence_root=tmp_path / "managed" / "evidence",
            allowed_evidence_root=tmp_path / "managed",
        )
        with pytest.raises(OkxDemoReconciliationBlocked):
            service.ingest_recovery_batch(
                events,
                recovery_batch_id="missing-account-stream",
                complete_streams={"ORDER", "FILL", "POSITION"},
                high_watermarks={
                    "ORDER": "orders-end",
                    "FILL": "fills-end",
                    "POSITION": "positions-end",
                },
                overlap_started_at=now - timedelta(minutes=4),
                observed_at=now - timedelta(minutes=3),
                completed_at=now - timedelta(minutes=3),
            )
        service.ingest_recovery_batch(
            events,
            recovery_batch_id="complete-but-stale",
            high_watermarks=_high_watermarks(),
            overlap_started_at=now - timedelta(minutes=4),
            observed_at=now - timedelta(minutes=3),
            completed_at=now - timedelta(minutes=3),
        )
        result = service.reconcile(
            now=now,
            stale_after=timedelta(minutes=2),
        )
        assert result.status == "UNKNOWN"
        assert result.opening_frozen is True
        assert result.database_ids["exchange_events"] == []
        assert result.database_ids["recovery_batches"] == []


def test_payload_whitelist_and_snapshot_savepoint_leave_no_partial_event(
    session_factory,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    unsafe = _event("ACCOUNT", "account", _account(), now)
    unsafe["payload"]["apiSecret"] = "not-persistable"
    invalid_order = _event(
        "ORDER",
        "order-1",
        {
            "ordId": "1",
            "clOrdId": "FAI00000000000000000000000000001",
            "instId": "BTC-USDT-SWAP",
            "state": "unsupported",
            "sz": "1",
            "accFillSz": "0",
            "avgPx": "",
            "reduceOnly": False,
        },
        now,
    )
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(db)
        with pytest.raises(OkxDemoReconciliationBlocked):
            service.ingest_event(unsafe)
        with pytest.raises(OkxDemoReconciliationBlocked):
            service.ingest_event(invalid_order)
    with session_factory() as db:
        assert db.scalar(select(OkxDemoExchangeEvent)) is None


@pytest.mark.parametrize("boundary", ["OUTSIDE", "SYMLINK", "EXISTING"])
def test_artifact_writer_is_confined_and_never_overwrites(
    session_factory,
    tmp_path,
    boundary,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    allowed = tmp_path / "managed"
    allowed.mkdir()
    if boundary == "OUTSIDE":
        evidence = tmp_path / "outside"
    elif boundary == "SYMLINK":
        real = allowed / "real"
        real.mkdir()
        evidence = allowed / "linked"
        evidence.symlink_to(real, target_is_directory=True)
    else:
        evidence = allowed / "evidence"
        evidence.mkdir()
        target = evidence / "okx-demo-reconciliation-1.json"
        target.write_text("do-not-overwrite", encoding="utf-8")
    with pytest.raises(OkxDemoReconciliationBlocked):
        with session_factory.begin() as db:
            service = OkxDemoReconciliationService(
                db,
                evidence_root=evidence,
                allowed_evidence_root=allowed,
            )
            service.ingest_event(
                _event(
                    "POSITION",
                    "BTC-USDT-SWAP:long",
                    _position("0"),
                    now,
                )
            )
            service.ingest_event(
                _event("ACCOUNT", "account", _account(), now)
            )
            service.reconcile(now=now)
    if boundary == "EXISTING":
        assert target.read_text(encoding="utf-8") == "do-not-overwrite"


def test_read_api_reconciles_database_ids_without_raw_account_payload(
    session_factory,
    tmp_path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    with session_factory.begin() as db:
        service = OkxDemoReconciliationService(
            db,
            evidence_root=tmp_path / "managed" / "evidence",
            allowed_evidence_root=tmp_path / "managed",
        )
        service.ingest_event(
            _event(
                "POSITION",
                "BTC-USDT-SWAP:long",
                _position("0"),
                now,
            )
        )
        service.ingest_event(_event("ACCOUNT", "account", _account(), now))
        result = service.reconcile(now=now)
        run_id = result.reconciliation_run_database_id
    with session_factory() as db:
        latest = latest_reconciliation(db=db)
        run = reconciliation_run(run_id=run_id, db=db)
        state = exchange_state(event_limit=50, db=db)
        assert latest["execution_target_id"] == "OKX_DEMO"
        assert latest["database_ids"] == run["database_ids"]
        assert latest["data_source"] == {
            "source_type": "api_aggregate",
            "core_data": True,
        }
        assert state["data_source"] == {
            "source_type": "database",
            "core_data": True,
        }
        assert state["raw_exchange_payloads_exposed"] is False
        assert "accountFingerprint" not in str(state)
        assert latest["artifact"]["status"] == "READY"
        assert latest["artifact"]["artifact_id"].startswith(
            "okx-demo-reconciliation-"
        )
        assert "path" not in latest["artifact"]
        assert "path" not in run["artifact"]
        assert str(tmp_path) not in str(latest)
        assert str(tmp_path) not in str(run)


def _high_watermarks() -> dict[str, str]:
    return {
        "ORDER": "orders-end",
        "FILL": "fills-end",
        "POSITION": "positions-end",
        "ACCOUNT": "account-end",
    }
