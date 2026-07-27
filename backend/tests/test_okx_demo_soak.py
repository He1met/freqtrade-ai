from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.okx_demo_soak import OkxDemoSoakEvent, OkxDemoSoakProbe
from app.services.okx_demo_soak import (
    MINIMUM_SOAK_SECONDS,
    OkxDemoSoakService,
    SoakFinalEvidence,
    SoakProbeInput,
    SoakRunBlocked,
    SoakStartGate,
    environment_fingerprint,
)


NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def fingerprint():
    return environment_fingerprint(
        {
            "repository": "repo-main-commit",
            "runtime": "launchagent-generation-1",
            "database": "postgres-primary",
            "virtualenv": "backend-venv",
            "writer": "okx-writer-generation-1",
        }
    )


def gate(**changes):
    values = {
        "e2e_evidence_id": "okx-demo-e2e-run-452",
        "e2e_status": "PASSED",
        "execution_target_id": "OKX_DEMO",
        "repository_instances": 1,
        "runtime_instances": 1,
        "database_instances": 1,
        "virtualenv_instances": 1,
        "writer_instances": 1,
        "completed_order_lifecycles": 1,
        "reconciled_order_lifecycles": 1,
        "cleanup_cycles": 1,
        "environment_fingerprint": fingerprint(),
    }
    values.update(changes)
    return SoakStartGate(**values)


def probe(at, **changes):
    values = {
        "observed_at": at,
        "repository_instances": 1,
        "runtime_instances": 1,
        "database_instances": 1,
        "virtualenv_instances": 1,
        "writer_instances": 1,
        "reconciliation_status": "RECONCILED",
        "open_orders": 0,
        "open_positions": 0,
        "duplicate_orders": 0,
        "unknown_positions": 0,
        "queue_depth": 0,
        "database_bytes": 1024,
        "log_bytes": 2048,
        "credentials_exposed": False,
        "runtime_healthy": True,
        "websocket_healthy": True,
        "evidence_refs": {"runtime": "runtime-check-1", "database": "db-check-1"},
    }
    values.update(changes)
    return SoakProbeInput(**values)


def final_evidence(**changes):
    values = {
        "cleanup_completed": True,
        "open_orders": 0,
        "open_positions": 0,
        "reconciliation_status": "RECONCILED",
        "repository_instances": 1,
        "runtime_instances": 1,
        "database_instances": 1,
        "virtualenv_instances": 1,
        "writer_instances": 1,
        "api_evidence_ref": "api-final",
        "database_evidence_ref": "database-final",
        "artifact_evidence_ref": "artifact-final",
        "okx_orders_evidence_ref": "orders-final",
        "okx_fills_evidence_ref": "fills-final",
        "okx_positions_evidence_ref": "positions-final",
        "runtime_log_evidence_ref": "runtime-log-final",
        "report_sha256": "a" * 64,
    }
    values.update(changes)
    return SoakFinalEvidence(**values)


def test_run_stays_blocked_until_452_pass_and_coverage_are_real(db):
    service = OkxDemoSoakService(db)
    run = service.plan(environment_fingerprint_sha256=fingerprint(), now=NOW)

    service.start(
        run.id,
        gate=gate(
            e2e_status="NOT_RUN",
            completed_order_lifecycles=0,
            reconciled_order_lifecycles=0,
            cleanup_cycles=0,
        ),
        now=NOW,
    )

    assert run.status == "BLOCKED"
    assert run.started_at is None
    event_types = list(
        db.scalars(
            select(OkxDemoSoakEvent.event_type)
            .where(OkxDemoSoakEvent.soak_run_id == run.id)
            .order_by(OkxDemoSoakEvent.sequence)
        )
    )
    assert event_types == ["PLANNED", "BLOCKED"]


def test_probe_drift_freezes_openings_and_requires_recovery(db):
    service = OkxDemoSoakService(db)
    run = service.plan(environment_fingerprint_sha256=fingerprint(), now=NOW)
    service.start(run.id, gate=gate(), now=NOW)

    recorded = service.record_probe(
        run.id,
        probe(
            NOW + timedelta(minutes=5),
            reconciliation_status="DRIFTED",
            unknown_positions=1,
        ),
    )

    assert recorded.reconciliation_status == "DRIFTED"
    assert run.status == "RECOVERY_REQUIRED"
    assert run.operational_state == "FROZEN"

    service.begin_recovery(
        run.id,
        recovery_evidence_ref="recovery-batch-1",
        now=NOW + timedelta(minutes=6),
    )
    assert run.operational_state == "RECOVERING"
    service.complete_recovery(
        run.id,
        reconciliation_status="RECOVERED",
        open_orders=0,
        unknown_positions=0,
        recovery_evidence_ref="recovery-batch-1",
        now=NOW + timedelta(minutes=7),
    )
    assert run.status == "RUNNING"
    assert run.operational_state == "ACTIVE"


def test_incomplete_recovery_fails_instead_of_resuming(db):
    service = OkxDemoSoakService(db)
    run = service.plan(environment_fingerprint_sha256=fingerprint(), now=NOW)
    service.start(run.id, gate=gate(), now=NOW)
    service.record_probe(
        run.id,
        probe(NOW + timedelta(minutes=5), runtime_healthy=False),
    )
    service.begin_recovery(
        run.id,
        recovery_evidence_ref="recovery-batch-2",
        now=NOW + timedelta(minutes=6),
    )

    service.complete_recovery(
        run.id,
        reconciliation_status="UNKNOWN",
        open_orders=1,
        unknown_positions=0,
        recovery_evidence_ref="recovery-batch-2",
        now=NOW + timedelta(minutes=7),
    )

    assert run.status == "FAILED"
    assert run.operational_state == "STOPPED"


def test_less_than_seven_days_can_never_pass(db):
    service = OkxDemoSoakService(db)
    run = service.plan(
        environment_fingerprint_sha256=fingerprint(),
        now=NOW,
        probe_interval_seconds=3600,
        max_probe_gap_seconds=3600,
    )
    service.start(run.id, gate=gate(), now=NOW)
    service.record_probe(run.id, probe(NOW + timedelta(hours=1)))

    service.finalize(
        run.id,
        evidence=final_evidence(),
        now=NOW + timedelta(hours=1),
    )

    assert run.status == "FAILED"
    assert run.final_evidence_json is None


def test_real_seven_day_timeline_needs_continuous_probes_and_cleanup(db):
    service = OkxDemoSoakService(db)
    run = service.plan(
        environment_fingerprint_sha256=fingerprint(),
        now=NOW,
        required_duration_seconds=MINIMUM_SOAK_SECONDS,
        probe_interval_seconds=3600,
        max_probe_gap_seconds=3600,
    )
    service.start(run.id, gate=gate(), now=NOW)
    for hour in range(1, 7 * 24 + 1):
        service.record_probe(run.id, probe(NOW + timedelta(hours=hour)))

    assessment = service.assess(run.id, now=NOW + timedelta(days=7))
    assert assessment.reason_codes == ()
    assert assessment.probe_count == 168
    assert assessment.duration_seconds == MINIMUM_SOAK_SECONDS
    assert assessment.max_observed_probe_gap_seconds == 3600

    service.finalize(
        run.id,
        evidence=final_evidence(),
        now=NOW + timedelta(days=7),
    )
    assert run.status == "PASSED"
    assert run.completed_at == NOW + timedelta(days=7)
    assert run.final_evidence_json["probe_count"] == 168


def test_probe_gap_or_single_environment_drift_cannot_be_hidden(db):
    service = OkxDemoSoakService(db)
    run = service.plan(
        environment_fingerprint_sha256=fingerprint(),
        now=NOW,
        probe_interval_seconds=300,
        max_probe_gap_seconds=900,
    )
    service.start(run.id, gate=gate(), now=NOW)

    service.record_probe(
        run.id,
        probe(NOW + timedelta(minutes=16), writer_instances=2),
    )

    assert run.status == "RECOVERY_REQUIRED"
    assert db.scalar(
        select(OkxDemoSoakProbe.writer_instances).where(
            OkxDemoSoakProbe.soak_run_id == run.id
        )
    ) == 2


def test_credential_exposure_is_terminal_and_cannot_be_recovered(db):
    service = OkxDemoSoakService(db)
    run = service.plan(environment_fingerprint_sha256=fingerprint(), now=NOW)
    service.start(run.id, gate=gate(), now=NOW)

    service.record_probe(
        run.id,
        probe(NOW + timedelta(minutes=5), credentials_exposed=True),
    )

    assert run.status == "FAILED"
    assert run.operational_state == "STOPPED"
    with pytest.raises(SoakRunBlocked, match="frozen"):
        service.begin_recovery(
            run.id,
            recovery_evidence_ref="invalid-recovery",
            now=NOW + timedelta(minutes=6),
        )


def test_only_one_active_soak_and_refs_are_opaque(db):
    service = OkxDemoSoakService(db)
    run = service.plan(environment_fingerprint_sha256=fingerprint(), now=NOW)
    service.start(run.id, gate=gate(), now=NOW)
    with pytest.raises(SoakRunBlocked, match="another"):
        service.plan(environment_fingerprint_sha256=fingerprint(), now=NOW)
    with pytest.raises(SoakRunBlocked, match="opaque"):
        service.record_probe(
            run.id,
            probe(
                NOW + timedelta(minutes=5),
                evidence_refs={"runtime": "/Users/person/secret.log"},
            ),
        )
