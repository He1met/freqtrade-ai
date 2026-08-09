from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.adapters.okx_demo import runtime_service
from app.adapters.okx_demo.credentials import OkxDemoCredentialsUnavailable
from app.services.okx_demo_reconciliation import OkxDemoReconciliationBlocked
from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.services.okx_demo_canary_preparation import (
    OkxDemoCanaryConsentCaptureFailed,
)


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def test_runtime_module_imports_in_a_fresh_interpreter() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import app.adapters.okx_demo.runtime_service"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "guard_state", ["BLOCKED", "COOLDOWN", "MANUAL_RESET_REQUIRED"]
)
def test_runtime_readiness_blocks_when_automation_guard_is_not_running(
    guard_state,
) -> None:
    assert runtime_service._automation_openings_ready(
        reconciliation_safe=True,
        guard_state=guard_state,
        guard_opening_allowed=True,
        externally_frozen=False,
    ) is False


def test_runtime_readiness_requires_complete_database_opening_guard() -> None:
    assert runtime_service._automation_openings_ready(
        reconciliation_safe=True,
        guard_state="RUNNING",
        guard_opening_allowed=False,
        externally_frozen=False,
    ) is False


@pytest.fixture(autouse=True)
def _no_recoverable_consent(monkeypatch):
    monkeypatch.setattr(
        runtime_service,
        "arm_finalized_canary_consent",
        lambda _db, *, runtime_instance_id: None,
    )


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


class MultiRoundStopEvent:
    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = 0

    def wait(self, _seconds):
        self.calls += 1
        return self.calls > self.rounds


class FakeAdapter:
    def __init__(self, *, startup_status="RECONCILED", resumable=False):
        self.events = []
        self.closed = False
        self.startup_status = startup_status
        self.resumable = resumable
        self.runtime_instance_id = "Runtime00000001"

    def reconcile_before_writer(self, **_kwargs):
        self.events.append("startup-reconcile")
        return reconciliation(self.startup_status)

    def observe(self, **_kwargs):
        self.events.append("observe")
        return reconciliation("DRIFTED")

    def run_active_one_shot(self, **_kwargs):
        self.events.append("one-shot-check")
        return "NONE"

    def can_resume_controlled_canary(self, _db, *, reconciliation_run_id):
        self.events.append(("can-resume", reconciliation_run_id))
        return self.resumable

    def run_cycle(self, *, writer, **_kwargs):
        self.events.append(("cycle", writer._openings_allowed))

    def close(self):
        self.closed = True


class FakeServerSession:
    def __init__(self, events):
        self.read = object()
        self.events = events
        self.closed = False
        self.writer = FakeWriter()

    def create_order_writer(self, _db):
        self.events.append("writer-created")
        return self.writer

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
        "BLOCKED_OPENINGS",
        "BLOCKED_OPENINGS",
    ]
    assert adapter.closed is True
    assert server.closed is True
    assert db.closed is True
    assert db.commits == 4
    assert db.rollbacks == 0


def test_transient_reconciliation_failure_freezes_openings_and_continues_next_round(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events = []
    readiness = []
    guard_failures = []
    recovered_grant_checks = []
    grant_terminalizations = []
    grant_settlements = []
    lock_releases = []

    class TransientThenHealthyAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.observations = 0

        def observe(self, **_kwargs):
            self.observations += 1
            self.events.append(("observe", self.observations))
            if self.observations == 1:
                raise OkxDemoReconciliationBlocked(
                    "pending_orders pagination is temporarily incomplete"
                )
            return reconciliation("RECONCILED")

        def run_active_one_shot(self, **_kwargs):
            self.events.append("one-shot-check")
            return "NONE"

    class RuntimeDatabase(FakeDatabaseSession):
        def execute(self, _statement, _parameters=None):
            return SimpleNamespace(scalar_one=lambda: 41)

    adapter = TransientThenHealthyAdapter()
    server = FakeServerSession(events)
    db = RuntimeDatabase()
    connection = SimpleNamespace(close=lambda: events.append("connection-closed"))
    engine = SimpleNamespace(
        connect=lambda: connection,
        dispose=lambda: events.append("engine-disposed"),
    )
    stop = MultiRoundStopEvent(rounds=2)
    monkeypatch.setattr(runtime_service, "STOP_EVENT", stop)
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(
        runtime_service,
        "create_okx_demo_server_session",
        lambda *_args, **_kwargs: server,
    )
    monkeypatch.setattr(
        runtime_service,
        "acquire_one_shot_runtime_lock",
        lambda _db: True,
    )
    monkeypatch.setattr(
        runtime_service,
        "release_one_shot_runtime_lock",
        lambda _db: (lock_releases.append("released") or True),
    )
    monkeypatch.setattr(
        runtime_service,
        "arm_finalized_canary_consent",
        lambda _db, *, runtime_instance_id: (
            recovered_grant_checks.append(runtime_instance_id) or None
        ),
    )
    monkeypatch.setattr(
        runtime_service,
        "process_pending_canary_consent_handoff",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime_service,
        "process_pending_canary_attestation",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        runtime_service,
        "settle_canary_consent_handoff",
        lambda *_args, **_kwargs: grant_settlements.append("settled"),
    )
    monkeypatch.setattr(
        runtime_service,
        "fail_canary_grant_before_prepare",
        lambda *_args, **_kwargs: grant_terminalizations.append("failed"),
    )
    monkeypatch.setattr(
        runtime_service.OkxDemoAutomationGuard,
        "record_health",
        lambda *_args, **_kwargs: "RUNNING",
    )
    monkeypatch.setattr(
        runtime_service.OkxDemoAutomationGuard,
        "opening_allowed",
        lambda *_args, **_kwargs: True,
    )

    def _record_failure(_db, *, failure_class, reconciliation_run_id=None, **_kwargs):
        guard_failures.append((failure_class, reconciliation_run_id))
        return "BLOCKED"

    monkeypatch.setattr(
        runtime_service.OkxDemoAutomationGuard,
        "record_failure",
        _record_failure,
    )
    monkeypatch.setattr(
        runtime_service,
        "_write_readiness",
        lambda _path, payload: readiness.append(dict(payload)),
    )

    runtime_service.serve(
        environment={"DATABASE_URL": "postgresql+psycopg:///isolated"},
        runtime_path=tmp_path,
        reconciliation_factory=lambda: adapter,
        engine_factory=lambda *_args, **_kwargs: engine,
        now_provider=lambda: NOW,
    )

    transient = [
        item
        for item in readiness
        if item["status"] == "BLOCKED_OPENINGS"
        and item["reconciliation"] == "UNKNOWN"
    ]
    assert len(transient) == 1
    assert transient[0]["automation_guard"] == "BLOCKED"
    assert guard_failures == [("RECONCILIATION_TRANSIENT", 41)]
    assert adapter.events == [
        "startup-reconcile",
        "one-shot-check",
        ("observe", 1),
        "one-shot-check",
        ("observe", 2),
        ("cycle", True),
    ]
    assert readiness[-1]["status"] == "READY"
    assert readiness[-1]["reconciliation"] == "RECONCILED"
    assert server.writer.calls == []
    assert recovered_grant_checks == ["Runtime00000001", "Runtime00000001"]
    assert grant_terminalizations == []
    assert grant_settlements == []
    assert lock_releases == ["released", "released"]
    assert stop.calls == 3
    assert adapter.closed is True
    assert server.closed is True
    assert db.closed is True
    assert "connection-closed" in events
    assert "engine-disposed" in events


@pytest.mark.parametrize(
    "failure",
    [
        OkxDemoReconciliationBlocked("exchange order drift mismatch"),
        OkxDemoWriteBlocked("writer invariant failed"),
        runtime_service.OkxReadAdapterError(
            kind="UNAUTHORIZED",
            status="BLOCKED",
            message="authentication failed",
        ),
    ],
    ids=["reconciliation", "writer", "authentication"],
)
def test_non_transient_runtime_failures_still_exit_fail_closed(
    monkeypatch,
    tmp_path: Path,
    failure: Exception,
) -> None:
    events = []
    guard_failures = []

    class FailingObservationAdapter(FakeAdapter):
        def observe(self, **_kwargs):
            self.events.append("observe-failed")
            raise failure

    class RuntimeDatabase(FakeDatabaseSession):
        def execute(self, _statement, _parameters=None):
            return SimpleNamespace(scalar_one=lambda: 41)

    adapter = FailingObservationAdapter()
    server = FakeServerSession(events)
    db = RuntimeDatabase()
    connection = SimpleNamespace(close=lambda: events.append("connection-closed"))
    engine = SimpleNamespace(
        connect=lambda: connection,
        dispose=lambda: events.append("engine-disposed"),
    )
    monkeypatch.setattr(runtime_service, "STOP_EVENT", MultiRoundStopEvent(rounds=2))
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(
        runtime_service,
        "create_okx_demo_server_session",
        lambda *_args, **_kwargs: server,
    )
    monkeypatch.setattr(
        runtime_service,
        "acquire_one_shot_runtime_lock",
        lambda _db: True,
    )
    monkeypatch.setattr(
        runtime_service,
        "release_one_shot_runtime_lock",
        lambda _db: True,
    )
    monkeypatch.setattr(
        runtime_service,
        "process_pending_canary_consent_handoff",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime_service,
        "process_pending_canary_attestation",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        runtime_service.OkxDemoAutomationGuard,
        "record_health",
        lambda *_args, **_kwargs: "RUNNING",
    )
    monkeypatch.setattr(
        runtime_service.OkxDemoAutomationGuard,
        "opening_allowed",
        lambda *_args, **_kwargs: True,
    )

    def _record_failure(_db, *, failure_class, reconciliation_run_id=None, **_kwargs):
        guard_failures.append((failure_class, reconciliation_run_id))
        return "BLOCKED"

    monkeypatch.setattr(
        runtime_service.OkxDemoAutomationGuard,
        "record_failure",
        _record_failure,
    )
    monkeypatch.setattr(runtime_service, "_write_readiness", lambda *_args: None)

    with pytest.raises(type(failure)):
        runtime_service.serve(
            environment={"DATABASE_URL": "postgresql+psycopg:///isolated"},
            runtime_path=tmp_path,
            reconciliation_factory=lambda: adapter,
            engine_factory=lambda *_args, **_kwargs: engine,
            now_provider=lambda: NOW,
        )

    assert len(guard_failures) == 1
    assert guard_failures[0][1] == 41
    assert adapter.events == ["startup-reconcile", "one-shot-check", "observe-failed"]
    assert server.writer.calls == []
    assert adapter.closed is True
    assert server.closed is True
    assert db.closed is True
    assert "connection-closed" in events
    assert "engine-disposed" in events


def test_runtime_restart_allows_only_exact_controlled_canary_recovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events = []
    adapter = FakeAdapter(startup_status="DRIFTED", resumable=True)
    server = FakeServerSession(events)
    db = FakeDatabaseSession()
    readiness = []
    connection = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(connect=lambda: connection, dispose=lambda: None)
    monkeypatch.setattr(runtime_service, "STOP_EVENT", FakeStopEvent())
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(
        runtime_service,
        "acquire_one_shot_runtime_lock",
        lambda _db: True,
    )
    monkeypatch.setattr(
        runtime_service,
        "release_one_shot_runtime_lock",
        lambda _db: False,
    )
    monkeypatch.setattr(
        runtime_service,
        "create_okx_demo_server_session",
        lambda _environment, lock_path: server,
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

    assert ("can-resume", 41) in adapter.events
    assert ("cycle", False) in adapter.events
    assert readiness[0]["status"] == "RECOVERY_ONLY"
    assert server.writer.calls == []


def test_runtime_releases_coordination_window_after_canary_attestation(
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
    canary_calls = []
    monkeypatch.setattr(runtime_service, "STOP_EVENT", FakeStopEvent())
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(
        runtime_service,
        "acquire_one_shot_runtime_lock",
        lambda _db: (adapter.events.append("coordination-lock") or True),
    )
    monkeypatch.setattr(
        runtime_service,
        "process_pending_canary_attestation",
        lambda **_kwargs: (canary_calls.append("attest") or True),
    )
    monkeypatch.setattr(
        runtime_service,
        "release_one_shot_runtime_lock",
        lambda _db: (adapter.events.append("coordination-unlock") or True),
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

    assert canary_calls == ["attest"]
    assert adapter.events == [
        "startup-reconcile",
        "coordination-lock",
        "coordination-unlock",
    ]
    assert "one-shot-check" not in adapter.events
    assert "observe" not in adapter.events
    assert not any(
        isinstance(event, tuple) and event[0] == "cycle"
        for event in adapter.events
    )
    # One commit persists the attestation and one completes the unlock query;
    # the next polling turn then exits without a long reconciliation cycle.
    assert db.commits == 4
    assert db.rollbacks == 0


def test_runtime_commits_consent_lineage_before_same_identity_grant(monkeypatch, tmp_path):
    events = []

    class ConsentAdapter(FakeAdapter):
        def run_active_one_shot(self, **_kwargs):
            self.events.append("one-shot-consumed")
            return "CONSUMED"

    adapter = ConsentAdapter()
    server = FakeServerSession(events)
    db = FakeDatabaseSession()
    connection = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(connect=lambda: connection, dispose=lambda: None)
    monkeypatch.setattr(runtime_service, "STOP_EVENT", FakeStopEvent())
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(runtime_service, "acquire_one_shot_runtime_lock", lambda _db: True)
    monkeypatch.setattr(runtime_service, "release_one_shot_runtime_lock", lambda _db: False)
    monkeypatch.setattr(runtime_service, "create_okx_demo_server_session", lambda *_args, **_kwargs: server)
    monkeypatch.setattr(runtime_service, "_write_readiness", lambda *_args, **_kwargs: None)
    def finalize_consent(**kwargs):
        kwargs["fresh_reconciliation"]()
        events.append(("finalized", kwargs["runtime_instance_id"]))
        return SimpleNamespace(handoff_id="a" * 32)

    monkeypatch.setattr(
        runtime_service,
        "process_pending_canary_consent_handoff",
        finalize_consent,
    )
    arm_calls = []
    def arm_after_commit(_db, *, runtime_instance_id):
        arm_calls.append(runtime_instance_id)
        if len(arm_calls) == 1:
            return None
        events.append(("armed", runtime_instance_id))
        return SimpleNamespace(grant_id="b" * 32)

    monkeypatch.setattr(
        runtime_service,
        "arm_finalized_canary_consent",
        arm_after_commit,
    )
    monkeypatch.setattr(
        runtime_service,
        "settle_canary_consent_handoff",
        lambda _db, *, grant_id: "CONSUMED",
    )

    runtime_service.serve(
        environment={"DATABASE_URL": "postgresql+psycopg:///isolated"},
        runtime_path=tmp_path,
        reconciliation_factory=lambda: adapter,
        engine_factory=lambda *_args, **_kwargs: engine,
        now_provider=lambda: NOW,
    )

    consent_events = [event for event in events if isinstance(event, tuple)]
    assert consent_events[:2] == [
        ("finalized", "Runtime00000001"),
        ("armed", "Runtime00000001"),
    ]
    assert adapter.events == ["startup-reconcile", "observe", "one-shot-consumed"]
    assert db.commits >= 3


def test_runtime_terminalizes_pre_commit_consent_failure_without_writer_post(
    monkeypatch, tmp_path
):
    events = []

    class TerminalizingDatabase(FakeDatabaseSession):
        def execute(self, _statement, parameters):
            events.append(("terminalized", dict(parameters)))
            return SimpleNamespace(scalar_one=lambda: True)

    adapter = FakeAdapter()
    server = FakeServerSession(events)
    db = TerminalizingDatabase()
    engine = SimpleNamespace(
        connect=lambda: SimpleNamespace(close=lambda: None),
        dispose=lambda: None,
    )
    monkeypatch.setattr(runtime_service, "STOP_EVENT", FakeStopEvent())
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(
        runtime_service, "acquire_one_shot_runtime_lock", lambda _db: True
    )
    monkeypatch.setattr(
        runtime_service, "release_one_shot_runtime_lock", lambda _db: True
    )
    monkeypatch.setattr(
        runtime_service,
        "create_okx_demo_server_session",
        lambda *_args, **_kwargs: server,
    )
    monkeypatch.setattr(
        runtime_service, "_write_readiness", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        runtime_service,
        "process_pending_canary_consent_handoff",
        lambda **_kwargs: (_ for _ in ()).throw(
            OkxDemoCanaryConsentCaptureFailed(
                handoff_id="a" * 32,
                stage="ATTESTATION_CAPTURE",
                category="EXCHANGE_READ",
            )
        ),
    )

    with pytest.raises(
        runtime_service.OkxDemoRuntimeBlocked,
        match=r"stage=ATTESTATION_CAPTURE, category=EXCHANGE_READ",
    ):
        runtime_service.serve(
            environment={"DATABASE_URL": "postgresql+psycopg:///isolated"},
            runtime_path=tmp_path,
            reconciliation_factory=lambda: adapter,
            engine_factory=lambda *_args, **_kwargs: engine,
            now_provider=lambda: NOW,
        )

    assert events[-1] == (
        "terminalized",
        {
            "handoff": "a" * 32,
            "stage": "ATTESTATION_CAPTURE",
            "category": "EXCHANGE_READ",
        },
    )
    assert "one-shot-check" not in adapter.events
    assert db.rollbacks >= 1
    assert db.commits >= 2


@pytest.mark.parametrize(
    ("journal_state", "terminalized"),
    [("BEFORE_PREPARED", True), ("PREPARED", False)],
)
def test_recovered_grant_writer_exception_uses_owner_terminalization_boundary(
    monkeypatch, tmp_path, journal_state, terminalized, capsys
):
    events = []
    failure_calls = []

    class FailingRecoveredAdapter(FakeAdapter):
        def run_active_one_shot(self, **_kwargs):
            raise RuntimeError("synthetic recovered writer failure")

    adapter = FailingRecoveredAdapter()
    server = FakeServerSession(events)
    db = FakeDatabaseSession()
    engine = SimpleNamespace(
        connect=lambda: SimpleNamespace(close=lambda: None),
        dispose=lambda: None,
    )
    safe_target = SimpleNamespace(
        simulated_trading=True,
        allow_real_funds=False,
        order_submission_enabled=False,
    )
    monkeypatch.setattr(
        runtime_service,
        "get_settings",
        lambda: SimpleNamespace(execution_target_manifest=SimpleNamespace(
            active_target_id="OKX_DEMO", active_target=safe_target
        )),
    )
    monkeypatch.setattr(runtime_service, "STOP_EVENT", FakeStopEvent())
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(runtime_service, "acquire_one_shot_runtime_lock", lambda _db: True)
    monkeypatch.setattr(runtime_service, "release_one_shot_runtime_lock", lambda _db: True)
    monkeypatch.setattr(runtime_service, "create_okx_demo_server_session", lambda *_a, **_k: server)
    monkeypatch.setattr(runtime_service, "_write_readiness", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runtime_service,
        "arm_finalized_canary_consent",
        lambda _db, *, runtime_instance_id: SimpleNamespace(grant_id="c" * 32),
    )
    def terminalize(_db, *, grant_id):
        failure_calls.append((journal_state, grant_id))
        return terminalized

    monkeypatch.setattr(
        runtime_service, "fail_canary_grant_before_prepare", terminalize
    )
    settle_calls = []
    monkeypatch.setattr(
        runtime_service,
        "settle_canary_consent_handoff",
        lambda *_a, **_k: settle_calls.append("settled"),
    )

    with pytest.raises(RuntimeError, match="synthetic recovered writer failure"):
        runtime_service.serve(
            environment={"DATABASE_URL": "postgresql+psycopg:///isolated"},
            runtime_path=tmp_path,
            reconciliation_factory=lambda: adapter,
            engine_factory=lambda *_a, **_k: engine,
            now_provider=lambda: NOW,
        )

    assert failure_calls == [(journal_state, "c" * 32)]
    assert settle_calls == []
    assert db.rollbacks >= 1
    assert db.commits >= 2
    diagnostic = capsys.readouterr().err
    assert diagnostic == (
        "OKX_DEMO safe runtime diagnostic "
        "stage=runtime-loop exception_type=RuntimeError\n"
    )
    assert "synthetic recovered writer failure" not in diagnostic


@pytest.mark.parametrize("unsafe_source", ["manifest", "openings-freeze"])
def test_runtime_commit_a_safety_change_blocks_immediate_canary_post(
    monkeypatch, tmp_path, unsafe_source
):
    events = []
    post_calls = []
    safe_target = SimpleNamespace(
        simulated_trading=True,
        allow_real_funds=False,
        order_submission_enabled=False,
    )
    unsafe_target = SimpleNamespace(
        simulated_trading=True,
        allow_real_funds=True,
        order_submission_enabled=False,
    )
    manifest = {"value": SimpleNamespace(
        active_target_id="OKX_DEMO", active_target=safe_target
    )}

    class ConsentAdapter(FakeAdapter):
        def run_active_one_shot(self, *, openings_allowed, **_kwargs):
            if openings_allowed:
                post_calls.append("POST")
            return "NONE"

    adapter = ConsentAdapter()
    server = FakeServerSession(events)
    db = FakeDatabaseSession()
    engine = SimpleNamespace(
        connect=lambda: SimpleNamespace(close=lambda: None),
        dispose=lambda: None,
    )
    monkeypatch.setattr(
        runtime_service,
        "get_settings",
        lambda: SimpleNamespace(execution_target_manifest=manifest["value"]),
    )
    monkeypatch.setattr(runtime_service, "STOP_EVENT", FakeStopEvent())
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(runtime_service, "acquire_one_shot_runtime_lock", lambda _db: True)
    monkeypatch.setattr(runtime_service, "release_one_shot_runtime_lock", lambda _db: True)
    monkeypatch.setattr(runtime_service, "create_okx_demo_server_session", lambda *_a, **_k: server)
    monkeypatch.setattr(runtime_service, "_write_readiness", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runtime_service,
        "process_pending_canary_consent_handoff",
        lambda **_kwargs: SimpleNamespace(handoff_id="a" * 32),
    )
    arm_calls = []
    def arm_after_commit(_db, *, runtime_instance_id):
        arm_calls.append(runtime_instance_id)
        if len(arm_calls) == 1:
            return None
        if unsafe_source == "manifest":
            manifest["value"] = SimpleNamespace(
                active_target_id="OKX_DEMO", active_target=unsafe_target
            )
        else:
            (tmp_path / runtime_service.OPENINGS_FREEZE_FILENAME).touch()
        return SimpleNamespace(grant_id="b" * 32)

    monkeypatch.setattr(runtime_service, "arm_finalized_canary_consent", arm_after_commit)
    monkeypatch.setattr(runtime_service, "settle_canary_consent_handoff", lambda *_a, **_k: "FAILED")

    with pytest.raises(
        runtime_service.OkxDemoRuntimeBlocked,
        match="failed before submission",
    ):
        runtime_service.serve(
            environment={"DATABASE_URL": "postgresql+psycopg:///isolated"},
            runtime_path=tmp_path,
            reconciliation_factory=lambda: adapter,
            engine_factory=lambda *_a, **_k: engine,
            now_provider=lambda: NOW,
        )

    assert post_calls == []


@pytest.mark.parametrize("unsafe_source", ["manifest", "openings-freeze"])
def test_runtime_consent_safety_callback_rechecks_both_finalize_gates(
    monkeypatch, tmp_path, unsafe_source
):
    events = []
    adapter = FakeAdapter()
    server = FakeServerSession(events)
    db = FakeDatabaseSession()
    connection = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(connect=lambda: connection, dispose=lambda: None)
    safe_target = SimpleNamespace(
        simulated_trading=True,
        allow_real_funds=False,
        order_submission_enabled=False,
    )
    unsafe_target = SimpleNamespace(
        simulated_trading=True,
        allow_real_funds=True,
        order_submission_enabled=False,
    )
    manifest = {"value": SimpleNamespace(
        active_target_id="OKX_DEMO", active_target=safe_target
    )}
    monkeypatch.setattr(
        runtime_service,
        "get_settings",
        lambda: SimpleNamespace(execution_target_manifest=manifest["value"]),
    )
    monkeypatch.setattr(runtime_service, "STOP_EVENT", FakeStopEvent())
    monkeypatch.setattr(runtime_service, "Session", lambda bind: db)
    monkeypatch.setattr(
        runtime_service, "acquire_one_shot_runtime_lock", lambda _db: True
    )
    monkeypatch.setattr(
        runtime_service, "release_one_shot_runtime_lock", lambda _db: True
    )
    monkeypatch.setattr(
        runtime_service,
        "create_okx_demo_server_session",
        lambda *_args, **_kwargs: server,
    )
    monkeypatch.setattr(
        runtime_service, "_write_readiness", lambda *_args, **_kwargs: None
    )

    safety_results = []
    def exercise_both_gates(**kwargs):
        safety_results.append(kwargs["safety_check"]())
        if unsafe_source == "manifest":
            manifest["value"] = SimpleNamespace(
                active_target_id="OKX_DEMO", active_target=unsafe_target
            )
        else:
            (tmp_path / runtime_service.OPENINGS_FREEZE_FILENAME).touch()
        safety_results.append(kwargs["safety_check"]())
        return None

    monkeypatch.setattr(
        runtime_service,
        "process_pending_canary_consent_handoff",
        exercise_both_gates,
    )
    monkeypatch.setattr(
        runtime_service,
        "process_pending_canary_attestation",
        lambda **_kwargs: False,
    )

    runtime_service.serve(
        environment={"DATABASE_URL": "postgresql+psycopg:///isolated"},
        runtime_path=tmp_path,
        reconciliation_factory=lambda: adapter,
        engine_factory=lambda *_args, **_kwargs: engine,
        now_provider=lambda: NOW,
    )

    assert safety_results == [True, False]


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
    capsys,
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
    diagnostic = capsys.readouterr().err
    assert diagnostic == (
        "OKX_DEMO safe startup diagnostic "
        "stage=writer-capability exception_type=SensitiveWriterFailure\n"
    )
    assert "secret" not in diagnostic
    assert "signature" not in diagnostic
    assert "password" not in diagnostic

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


def test_writer_risk_chain_failure_keeps_safe_typed_diagnostic() -> None:
    failure = runtime_service.OkxDemoRuntimeStartupBlocked(
        stage="writer-capability",
        category="WRITER",
        cause_type="RiskChainBlocked",
    )

    assert failure.stage == "writer-capability"
    assert failure.category == "WRITER"
    assert failure.cause_type == "RiskChainBlocked"
    assert str(failure) == (
        "OKX_DEMO runtime startup blocked "
        "[stage=writer-capability, category=WRITER, "
        "cause_type=RiskChainBlocked]"
    )


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
