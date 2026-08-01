from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.okx_demo import runtime_service
from app.adapters.okx_demo.credentials import OkxDemoCredentialsUnavailable
from app.services.okx_demo_reconciliation import OkxDemoReconciliationBlocked
from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def reconciliation(
    status: str = "RECONCILED",
    *,
    observed_at: datetime = NOW,
):
    return {
        "status": status,
        "execution_target": "OKX_DEMO",
        "reconciliation_run_id": 41,
        "database_ids": {
            "reconciliation_run": [41],
            "exchange_events": [52],
            "order_snapshots": [],
            "fill_snapshots": [],
            "position_snapshots": [],
            "account_snapshots": [],
            "repaired_exchange_orders": [],
            "recovery_batches": [],
            "reconciliation_state": [61],
        },
        "observed_at": observed_at.isoformat(),
        "safe_to_open": status == "RECONCILED",
    }


@pytest.mark.parametrize("status", ["DRIFTED", "STALE", "UNKNOWN"])
def test_reconciliation_contract_preserves_safe_degraded_states(status):
    result = runtime_service._validate_reconciliation(
        reconciliation(status),
        now=NOW,
    )

    assert result.status == status
    assert result.safe_to_open is False


def test_recovered_nested_database_identity_can_authorize_startup():
    payload = reconciliation("RECOVERED")
    payload["safe_to_open"] = True
    payload["database_ids"] = {
        "reconciliation_run": [41],
        "exchange_events": [52, 53],
        "order_snapshots": [],
        "fill_snapshots": [],
        "position_snapshots": [],
        "account_snapshots": [],
        "repaired_exchange_orders": [],
        "recovery_batches": [],
        "reconciliation_state": [61],
    }

    result = runtime_service._validate_reconciliation(payload, now=NOW)

    assert result.status == "RECOVERED"
    assert result.safe_to_open is True


@pytest.mark.parametrize(
    "mutation",
    [
        {"execution_target": "OKX_LIVE"},
        {"reconciliation_run_id": 0},
        {"database_ids": {}},
        {"observed_at": (NOW - timedelta(minutes=2)).isoformat()},
        {"safe_to_open": False},
    ],
)
def test_reconciled_contract_requires_target_database_ids_and_freshness(
    mutation,
):
    payload = reconciliation()
    payload.update(mutation)

    with pytest.raises(runtime_service.OkxDemoRuntimeBlocked):
        runtime_service._validate_reconciliation(payload, now=NOW)


def test_reconciliation_loader_fails_closed_when_448_adapter_is_absent(
    monkeypatch,
):
    monkeypatch.setattr(
        runtime_service.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError),
    )

    with pytest.raises(
        runtime_service.OkxDemoRuntimeBlocked,
        match="adapter is unavailable",
    ):
        runtime_service.load_reconciliation_factory()


def test_runtime_main_records_known_block_reason_without_traceback(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        runtime_service,
        "serve",
        lambda **_kwargs: (_ for _ in ()).throw(
            OkxDemoReconciliationBlocked("pending_orders pagination is incomplete")
        ),
    )
    monkeypatch.setattr(
        runtime_service,
        "_runtime_path",
        lambda _value: Path("/tmp/freqtrade-ai-runtime"),
    )

    assert runtime_service.main(["--runtime-dir", "/tmp/runtime"]) == 2
    assert capsys.readouterr().err == (
        "OKX_DEMO runtime blocked: pending_orders pagination is incomplete\n"
    )


class FakeWriter:
    def __init__(self):
        self.calls = []

    def place(self, approved, **kwargs):
        self.calls.append(("place", approved, kwargs))
        return "placed"

    def cancel(self, order, **kwargs):
        self.calls.append(("cancel", order, kwargs))
        return "cancelled"

    def amend(self, order, **kwargs):
        self.calls.append(("amend", order, kwargs))
        return "amended"


def test_blocked_openings_capability_allows_cancel_and_reduction_only():
    writer = FakeWriter()
    capability = runtime_service._RuntimeWriterCapability(writer)
    grant = SimpleNamespace()
    opening = SimpleNamespace()
    closing = SimpleNamespace()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        runtime_service,
        "approved_execution_view",
        lambda approved: SimpleNamespace(reduce_only=approved is closing),
    )
    order = SimpleNamespace(contracts=Decimal("5"))
    try:
        with pytest.raises(OkxDemoWriteBlocked, match="openings are frozen"):
            capability.place(opening, submission_grant=grant)
        assert capability.place(closing, submission_grant=grant) == "placed"
        assert capability.cancel(order, submission_grant=grant) == "cancelled"
        assert (
            capability.amend(
                order,
                submission_grant=grant,
                request_id="ReduceOnly01",
                new_contracts=Decimal("4"),
            )
            == "amended"
        )
        with pytest.raises(OkxDemoWriteBlocked, match="risk-increasing"):
            capability.amend(
                order,
                submission_grant=grant,
                request_id="Increase01",
                new_contracts=Decimal("6"),
            )
    finally:
        monkey.undo()


class FakeStopEvent:
    def __init__(self):
        self.calls = 0

    def wait(self, _seconds):
        self.calls += 1
        return self.calls > 1


class FakeAdapter:
    def __init__(self):
        self.events = []
        self.closed = False

    def reconcile_before_writer(self, **_kwargs):
        self.events.append("startup-reconcile")
        return reconciliation()

    def observe(self, **_kwargs):
        self.events.append("observe")
        return reconciliation("DRIFTED")

    def run_active_one_shot(self, **_kwargs):
        self.events.append("one-shot-check")
        return "NONE"

    def run_cycle(self, *, writer, **_kwargs):
        self.events.append(("cycle", writer._openings_allowed))

    def close(self):
        self.closed = True


class FakeServerSession:
    def __init__(self, events):
        self.read = object()
        self.events = events
        self.closed = False

    def create_order_writer(self, _db):
        self.events.append("writer-created")
        return FakeWriter()

    def close(self):
        self.closed = True


class FakeDatabaseSession:
    def __init__(self):
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def in_transaction(self):
        return False

    def close(self):
        self.closed = True

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_runtime_orders_reconciliation_before_writer_and_keeps_drift_alive(
    monkeypatch,
    tmp_path: Path,
):
    events = []
    adapter = FakeAdapter()
    server = FakeServerSession(events)
    db = FakeDatabaseSession()
    readiness = []
    connection = SimpleNamespace(close=lambda: events.append("connection-closed"))
    engine = SimpleNamespace(
        connect=lambda: connection,
        dispose=lambda: events.append("engine-disposed"),
    )
    monkeypatch.setattr(runtime_service, "STOP_EVENT", FakeStopEvent())
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(
        runtime_service,
        "acquire_one_shot_runtime_lock",
        lambda _db: (adapter.events.append("coordination-lock") or True),
    )
    monkeypatch.setattr(
        runtime_service,
        "release_one_shot_runtime_lock",
        lambda _db: (adapter.events.append("coordination-unlock") or False),
    )
    monkeypatch.setattr(
        runtime_service,
        "create_okx_demo_server_session",
        lambda _environment, lock_path: (
            events.append(("session-created", lock_path)) or server
        ),
    )
    monkeypatch.setattr(
        runtime_service,
        "_write_readiness",
        lambda _path, payload: readiness.append(dict(payload)),
    )

    runtime_service.serve(
        environment={"DATABASE_URL": "postgresql+psycopg:///freqtrade_ai"},
        runtime_path=tmp_path,
        reconciliation_factory=lambda: adapter,
        engine_factory=lambda *_args, **_kwargs: engine,
        now_provider=lambda: NOW,
    )

    assert adapter.events == [
        "startup-reconcile",
        "coordination-lock",
        "one-shot-check",
        "observe",
        ("cycle", False),
        "coordination-unlock",
    ]
    assert events[0][0] == "session-created"
    assert events[1] == "writer-created"
    assert [item["status"] for item in readiness] == [
        "READY",
        "BLOCKED_OPENINGS",
    ]
    assert adapter.closed is True
    assert server.closed is True
    assert db.closed is True
    assert db.commits == 3
    assert db.rollbacks == 0


def test_runtime_cleanup_does_not_mask_primary_failure(
    monkeypatch,
    tmp_path: Path,
):
    class FailingServerSession(FakeServerSession):
        def close(self):
            raise RuntimeError("cleanup failure")

    class FailingAdapter(FakeAdapter):
        def reconcile_before_writer(self, **_kwargs):
            raise runtime_service.OkxDemoRuntimeBlocked("primary failure")

    events = []
    server = FailingServerSession(events)
    db = FakeDatabaseSession()
    connection = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(connect=lambda: connection, dispose=lambda: None)
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(
        runtime_service,
        "create_okx_demo_server_session",
        lambda _environment, lock_path: server,
    )

    with pytest.raises(
        runtime_service.OkxDemoRuntimeStartupBlocked,
        match=(
            r"stage=startup-reconciliation, "
            r"category=RECONCILIATION, "
            r"cause_type=OkxDemoRuntimeBlocked"
        ),
    ):
        runtime_service.serve(
            environment={"DATABASE_URL": "postgresql+psycopg:///freqtrade_ai"},
            runtime_path=tmp_path,
            reconciliation_factory=FailingAdapter,
            engine_factory=lambda *_args, **_kwargs: engine,
            now_provider=lambda: NOW,
        )


def test_factory_failure_preserves_fine_stage_category_and_allowed_type(
    monkeypatch,
    tmp_path: Path,
):
    adapter = FakeAdapter()
    monkeypatch.setattr(
        runtime_service,
        "create_okx_demo_server_session",
        lambda _environment, lock_path: (_ for _ in ()).throw(
            runtime_service.OkxDemoServerSessionBlocked(
                stage="read-attestation",
                category="ATTESTATION",
                cause=OkxDemoCredentialsUnavailable("must-not-cross"),
            )
        ),
    )

    with pytest.raises(
        runtime_service.OkxDemoRuntimeStartupBlocked,
    ) as captured:
        runtime_service.serve(
            environment={"DATABASE_URL": "postgresql+psycopg:///freqtrade_ai"},
            runtime_path=tmp_path,
            reconciliation_factory=lambda: adapter,
        )

    assert captured.value.stage == "read-attestation"
    assert captured.value.category == "ATTESTATION"
    assert (
        captured.value.cause_type
        == "OkxDemoCredentialsUnavailable"
    )
    assert "must-not-cross" not in str(captured.value)
    assert adapter.closed is True


def test_runtime_writer_startup_failure_preserves_stage_without_secret(
    monkeypatch,
    tmp_path: Path,
):
    class SensitiveWriterFailure(RuntimeError):
        pass

    class FailingServerSession(FakeServerSession):
        def create_order_writer(self, _db):
            raise SensitiveWriterFailure(
                "api-key=secret signature=private "
                "postgresql://operator:password@localhost/db"
            )

    events = []
    server = FailingServerSession(events)
    db = FakeDatabaseSession()
    connection = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(connect=lambda: connection, dispose=lambda: None)
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(
        runtime_service,
        "create_okx_demo_server_session",
        lambda _environment, lock_path: server,
    )

    with pytest.raises(
        runtime_service.OkxDemoRuntimeStartupBlocked,
    ) as captured:
        runtime_service.serve(
            environment={"DATABASE_URL": "postgresql+psycopg:///freqtrade_ai"},
            runtime_path=tmp_path,
            reconciliation_factory=FakeAdapter,
            engine_factory=lambda *_args, **_kwargs: engine,
            now_provider=lambda: NOW,
        )

    assert captured.value.stage == "writer-capability"
    assert captured.value.category == "UNEXPECTED"
    assert captured.value.cause_type == "UnexpectedError"
    rendered = str(captured.value)
    assert "secret" not in rendered
    assert "signature" not in rendered
    assert "password" not in rendered

    failure_path = tmp_path / runtime_service.FAILURE_FILENAME
    legacy_temporary = failure_path.with_suffix(".tmp")
    legacy_temporary.write_text("unsafe legacy temporary\n", encoding="utf-8")
    legacy_temporary.chmod(0o644)
    assert runtime_service._write_startup_failure(
        failure_path,
        captured.value,
    )
    assert failure_path.stat().st_mode & 0o077 == 0
    assert json.loads(failure_path.read_text(encoding="utf-8")) == {
        "status": "BLOCKED",
        "stage": "writer-capability",
        "category": "UNEXPECTED",
        "cause_type": "UnexpectedError",
    }
    assert legacy_temporary.stat().st_mode & 0o077 == 0o044
    assert legacy_temporary.read_text(encoding="utf-8") == (
        "unsafe legacy temporary\n"
    )
    assert "secret" not in failure_path.read_text(encoding="utf-8")


def test_invalid_reconciliation_rolls_back_persisted_evidence():
    db = FakeDatabaseSession()
    invalid = reconciliation()
    invalid["execution_target"] = "OKX_LIVE"

    with pytest.raises(runtime_service.OkxDemoRuntimeBlocked):
        runtime_service._reconcile_transaction(
            db,
            lambda: invalid,
            now=NOW,
        )

    assert db.commits == 0
    assert db.rollbacks == 1
