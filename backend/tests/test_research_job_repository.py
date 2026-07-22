from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Lock, Thread

import pytest
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import ResearchJobRepository
from app.schemas import DeepSeekBacktestLoopRequest
from app.services.research_job_queue import (
    ResearchJobConflict,
    ResearchJobQueueService,
)


FIXED_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session_factory(tmp_path: Path):
    database_path = tmp_path / "research-jobs.sqlite"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def loop_request(prompt: str = "Generate one local research strategy.") -> DeepSeekBacktestLoopRequest:
    return DeepSeekBacktestLoopRequest(
        prompt_summary=prompt,
        allow_real_call=True,
        backtest_profile={
            "schema_version": "2",
            "profile_name": "research-job-test",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "timerange": "20240101-20240201",
            "strategy": {"name": "ResearchJobTestStrategy"},
            "data_source": {
                "kind": "local",
                "exchange": "okx",
                "datadir": "user_data/data",
                "trading_mode": "futures",
                "margin_mode": "isolated",
            },
            "safety": {
                "allow_download": False,
                "allow_exchange_connection": False,
                "allow_dry_run": False,
                "allow_live_trading": False,
                "allow_hyperopt": False,
            },
        },
        timeout_seconds=60,
    )


def enqueue(
    db: Session,
    key: str,
    *,
    prompt: str = "Generate one local research strategy.",
):
    return ResearchJobQueueService(db).enqueue_deepseek_backtest(
        loop_request(prompt),
        idempotency_key=key,
    )


def test_idempotency_survives_new_session_and_does_not_persist_raw_key(session_factory) -> None:
    raw_key = "durable-research-job-key"
    with session_factory() as first_db:
        first = enqueue(first_db, raw_key)
        first_id = first.id
        first_digest = first.idempotency_key_digest

    with session_factory() as restarted_db:
        replay = enqueue(restarted_db, raw_key)

        assert replay.id == first_id
        assert replay.request_hash == first.request_hash
        assert replay.idempotency_key_digest == first_digest
        assert replay.idempotency_key_digest != raw_key
        assert raw_key not in str(replay.request_payload)
        assert restarted_db.query(type(replay)).count() == 1


def test_idempotency_key_reuse_with_different_payload_is_rejected(session_factory) -> None:
    with session_factory() as db:
        original = enqueue(db, "payload-bound-research-key", prompt="First research prompt")

    with session_factory() as restarted_db:
        with pytest.raises(ResearchJobConflict, match="different research job payload"):
            enqueue(
                restarted_db,
                "payload-bound-research-key",
                prompt="Different research prompt",
            )

        persisted = ResearchJobRepository(restarted_db).get(original.id)
        assert persisted is not None
        assert persisted.request_payload["prompt_summary"] == "First research prompt"
        assert ResearchJobRepository(restarted_db).status_counts() == {"PENDING": 1}


def test_two_threads_and_sessions_claim_one_pending_job_once(session_factory) -> None:
    with session_factory() as setup_db:
        job_id = enqueue(setup_db, "concurrent-claim-key").id

    start = Barrier(3)
    results = []
    errors = []
    result_lock = Lock()

    def claim(owner: str) -> None:
        try:
            with session_factory() as db:
                start.wait(timeout=5)
                claimed = ResearchJobRepository(db).claim_next(
                    owner=owner,
                    lease_seconds=30,
                    now=FIXED_NOW,
                )
                with result_lock:
                    results.append(None if claimed is None else (claimed.id, claimed.lease_owner))
        except Exception as exc:  # pragma: no cover - asserted through errors below
            with result_lock:
                errors.append(exc)

    threads = [Thread(target=claim, args=(f"worker-{index}",)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert all(thread.is_alive() is False for thread in threads)
    assert sum(result is not None for result in results) == 1
    assert {result[0] for result in results if result is not None} == {job_id}

    with session_factory() as verify_db:
        job = ResearchJobRepository(verify_db).get(job_id)
        control = ResearchJobRepository(verify_db).get_control()
        assert job is not None
        assert job.status == "RUNNING"
        assert job.attempt_count == 1
        assert job.lease_owner in {"worker-1", "worker-2"}
        assert job.lease_token
        assert control.active_job_id == job_id
        assert control.active_lease_token == job.lease_token


def test_global_single_flight_blocks_a_second_pending_job(session_factory) -> None:
    with session_factory() as setup_db:
        first_id = enqueue(setup_db, "global-single-flight-first").id
        second_id = enqueue(setup_db, "global-single-flight-second").id

    with session_factory() as first_worker_db:
        first = ResearchJobRepository(first_worker_db).claim_next(
            owner="worker-one",
            lease_seconds=30,
            now=FIXED_NOW,
        )
        assert first is not None
        assert first.id == first_id

    with session_factory() as second_worker_db:
        second_claim = ResearchJobRepository(second_worker_db).claim_next(
            owner="worker-two",
            lease_seconds=30,
            now=FIXED_NOW,
        )
        queued = ResearchJobRepository(second_worker_db).get(second_id)

        assert second_claim is None
        assert queued is not None
        assert queued.status == "PENDING"
        assert queued.attempt_count == 0
        assert queued.lease_token is None


def test_pause_and_resume_are_persistent_claim_gates(session_factory) -> None:
    with session_factory() as setup_db:
        job_id = enqueue(setup_db, "persistent-pause-key").id
        paused = ResearchJobQueueService(setup_db).pause("Operator maintenance window.")
        assert paused.paused is True
        assert paused.reason == "Operator maintenance window."

    with session_factory() as restarted_db:
        repository = ResearchJobRepository(restarted_db)
        assert repository.get_control().paused is True
        assert repository.claim_next(
            owner="paused-worker",
            lease_seconds=30,
            now=FIXED_NOW,
        ) is None
        job = repository.get(job_id)
        assert job is not None
        assert job.status == "PENDING"
        assert job.attempt_count == 0
        resumed = ResearchJobQueueService(restarted_db).resume()
        assert resumed.paused is False
        assert resumed.reason is None

    with session_factory() as resumed_db:
        claimed = ResearchJobRepository(resumed_db).claim_next(
            owner="resumed-worker",
            lease_seconds=30,
            now=FIXED_NOW,
        )
        assert claimed is not None
        assert claimed.id == job_id


def test_heartbeat_expiry_and_lease_token_fencing(session_factory) -> None:
    with session_factory() as setup_db:
        job_id = enqueue(setup_db, "heartbeat-and-fencing-key").id
        repository = ResearchJobRepository(setup_db)
        claimed = repository.claim_next(
            owner="lease-owner",
            lease_seconds=30,
            now=FIXED_NOW,
        )
        assert claimed is not None
        assert claimed.lease_token
        lease_token = claimed.lease_token

    heartbeat_time = FIXED_NOW + timedelta(seconds=10)
    with session_factory() as heartbeat_db:
        repository = ResearchJobRepository(heartbeat_db)
        assert repository.heartbeat(
            job_id,
            "wrong-lease-token",
            lease_seconds=30,
            now=heartbeat_time,
        ) is False
        assert repository.complete(
            job_id,
            "wrong-lease-token",
            status="SUCCESS",
            stage="COMPLETED",
            links={},
            evidence_snapshot={"status": "SUCCESS"},
            error_message=None,
            provider_completed=False,
            now=heartbeat_time,
        ) is None
        assert repository.heartbeat(
            job_id,
            lease_token,
            lease_seconds=30,
            now=heartbeat_time,
        ) is True

    expiry_time = heartbeat_time + timedelta(seconds=30)
    with session_factory() as expiry_db:
        expired = ResearchJobRepository(expiry_db).expire_stale(expiry_time)
        assert expired is not None
        assert expired.id == job_id
        assert expired.status == "STALE"
        assert expired.stage == "LEASE_EXPIRED"
        assert expired.evidence_snapshot["status"] == "STALE"
        assert expired.evidence_snapshot["acceptance_ready"] is False
        assert expired.evidence_snapshot["failed_reason"] == expired.error_message
        assert expired.lease_owner is None
        assert expired.lease_token is None
        assert expired.heartbeat_at is None
        control = ResearchJobRepository(expiry_db).get_control()
        assert control.active_job_id is None
        assert control.active_lease_token is None

    with session_factory() as fenced_db:
        repository = ResearchJobRepository(fenced_db)
        assert repository.heartbeat(
            job_id,
            lease_token,
            lease_seconds=30,
            now=expiry_time,
        ) is False
        assert repository.complete(
            job_id,
            lease_token,
            status="SUCCESS",
            stage="COMPLETED",
            links={},
            evidence_snapshot={"status": "SUCCESS"},
            error_message=None,
            provider_completed=False,
            now=expiry_time,
        ) is None


def test_pending_cancel_is_terminal_without_a_lease(session_factory) -> None:
    with session_factory() as db:
        job_id = enqueue(db, "cancel-pending-key").id
        cancelled = ResearchJobRepository(db).cancel(job_id, "Cancelled before claim.")

        assert cancelled is not None
        assert cancelled.status == "CANCELLED"
        assert cancelled.stage == "CANCELLED"
        assert cancelled.cancel_requested is True
        assert cancelled.completed_at is not None
        assert cancelled.lease_token is None
        assert cancelled.evidence_snapshot == {
            "status": "CANCELLED",
            "acceptance_ready": False,
            "failed_reason": "Cancelled before claim.",
        }
        assert ResearchJobRepository(db).claim_next(
            owner="late-worker",
            lease_seconds=30,
            now=FIXED_NOW,
        ) is None


def test_running_cancel_waits_for_owned_checkpoint_then_releases_global_lease(
    session_factory,
) -> None:
    with session_factory() as setup_db:
        running_id = enqueue(setup_db, "cancel-running-key").id
        next_id = enqueue(setup_db, "cancel-running-next-key").id
        repository = ResearchJobRepository(setup_db)
        claimed = repository.claim_next(
            owner="cancellable-worker",
            lease_seconds=30,
            now=FIXED_NOW,
        )
        assert claimed is not None
        assert claimed.id == running_id
        assert claimed.lease_token
        lease_token = claimed.lease_token

    with session_factory() as operator_db:
        requested = ResearchJobRepository(operator_db).cancel(
            running_id,
            "Cancelled during local execution.",
        )
        assert requested is not None
        assert requested.status == "RUNNING"
        assert requested.cancel_requested is True
        assert requested.lease_token == lease_token

    with session_factory() as wrong_worker_db:
        assert ResearchJobRepository(wrong_worker_db).cancel_at_checkpoint(
            running_id,
            "wrong-lease-token",
            now=FIXED_NOW + timedelta(seconds=1),
        ) is None

    with session_factory() as owning_worker_db:
        cancelled = ResearchJobRepository(owning_worker_db).cancel_at_checkpoint(
            running_id,
            lease_token,
            now=FIXED_NOW + timedelta(seconds=1),
        )
        assert cancelled is not None
        assert cancelled.status == "CANCELLED"
        assert cancelled.lease_owner is None
        assert cancelled.lease_token is None

    with session_factory() as next_worker_db:
        next_job = ResearchJobRepository(next_worker_db).claim_next(
            owner="next-worker",
            lease_seconds=30,
            now=FIXED_NOW + timedelta(seconds=2),
        )
        assert next_job is not None
        assert next_job.id == next_id
