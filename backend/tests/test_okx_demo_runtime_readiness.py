from datetime import datetime, timedelta, timezone
import fcntl
import json
import os

from app.services.okx_demo_runtime_readiness import (
    MAX_HEARTBEAT_AGE,
    blocked_runtime_readiness,
    read_okx_demo_runtime_readiness,
)

def _write_private(path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def test_private_current_runtime_evidence_reports_ready(tmp_path) -> None:
    ready_path = tmp_path / "okx-runtime.ready.json"
    lock_path = tmp_path / "okx-demo-order-writer.lock"
    _write_private(
        ready_path,
        json.dumps(
            {
                "status": "READY",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": "RECONCILED",
                "writer": "UNIQUE",
                "pid": os.getpid(),
            }
        ),
    )
    _write_private(lock_path, str(os.getpid()))
    with lock_path.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = read_okx_demo_runtime_readiness(
            now=datetime.now(timezone.utc),
            runtime_dir=tmp_path,
        )

    assert result.status == "READY"
    assert result.target_ready is True
    assert result.credentials_ready is True
    assert result.writer_ready is True
    assert result.observed_at is not None


def test_runtime_readiness_fails_closed_for_stale_or_unsafe_evidence(
    tmp_path,
) -> None:
    ready_path = tmp_path / "okx-runtime.ready.json"
    lock_path = tmp_path / "okx-demo-order-writer.lock"
    _write_private(
        ready_path,
        json.dumps(
            {
                "status": "READY",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": "RECONCILED",
                "writer": "UNIQUE",
                "pid": os.getpid(),
            }
        ),
    )
    _write_private(lock_path, str(os.getpid()))
    with lock_path.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        stale = read_okx_demo_runtime_readiness(
            now=datetime.now(timezone.utc) + MAX_HEARTBEAT_AGE + timedelta(seconds=1),
            runtime_dir=tmp_path,
        )
    assert stale == blocked_runtime_readiness()

    ready_path.chmod(0o644)
    assert read_okx_demo_runtime_readiness(
        now=datetime.now(timezone.utc),
        runtime_dir=tmp_path,
    ) == blocked_runtime_readiness()


def test_runtime_readiness_accepts_a_complete_reconciliation_cycle_age(
    tmp_path,
) -> None:
    ready_path = tmp_path / "okx-runtime.ready.json"
    lock_path = tmp_path / "okx-demo-order-writer.lock"
    _write_private(
        ready_path,
        json.dumps(
            {
                "status": "READY",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": "RECOVERED",
                "writer": "UNIQUE",
                "pid": os.getpid(),
            }
        ),
    )
    _write_private(lock_path, str(os.getpid()))
    with lock_path.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = read_okx_demo_runtime_readiness(
            now=datetime.now(timezone.utc) + timedelta(seconds=30),
            runtime_dir=tmp_path,
        )

    assert MAX_HEARTBEAT_AGE == timedelta(seconds=90)
    assert result.status == "READY"
    assert result.credentials_ready is True
    assert result.writer_ready is True


def test_blocked_openings_keeps_writer_target_but_not_credentials_ready(
    tmp_path,
) -> None:
    ready_path = tmp_path / "okx-runtime.ready.json"
    lock_path = tmp_path / "okx-demo-order-writer.lock"
    _write_private(
        ready_path,
        json.dumps(
            {
                "status": "BLOCKED_OPENINGS",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": "DRIFTED",
                "writer": "UNIQUE",
                "pid": os.getpid(),
            }
        ),
    )
    _write_private(lock_path, str(os.getpid()))
    with lock_path.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = read_okx_demo_runtime_readiness(
            now=datetime.now(timezone.utc),
            runtime_dir=tmp_path,
        )

    assert result.status == "BLOCKED_OPENINGS"
    assert result.target_ready is True
    assert result.credentials_ready is False
    assert result.writer_ready is True
