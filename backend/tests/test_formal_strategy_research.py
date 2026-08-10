from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models import (
    Base,
    MarketDataQualityReceipt,
    StrategyResearchAttemptEvent,
    StrategyResearchBatch,
)
from app.services.formal_strategy_research import FormalStrategyResearchCoordinator


NOW = datetime(2026, 8, 9, 5, 22, tzinfo=timezone.utc)


def build_coordinator(tmp_path, monkeypatch, *, ownership=True, allow_dry_run=False):
    repo = tmp_path / "repo"
    (repo / "research/strategy_candidates").mkdir(parents=True)
    for index in range(10):
        (repo / f"research/strategy_candidates/{index:02d}_candidate.py").write_text("pass\n")
    for asset in ("BTC", "ETH", "SOL"):
        for timeframe, frequency in (("5m", "5min"), ("15m", "15min")):
            data = repo / (
                f"user_data/data/futures/{asset}_USDT_USDT-{timeframe}-futures.feather"
            )
            data.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                {
                    "date": pd.date_range("2026-08-09T04:00:00Z", periods=5, freq=frequency),
                    "open": [100.0] * 5,
                    "high": [102.0] * 5,
                    "low": [99.0] * 5,
                    "close": [101.0] * 5,
                    "volume": [2.0] * 5,
                }
            ).to_feather(data)
    freqtrade = repo / ".venv/bin/freqtrade"
    freqtrade.parent.mkdir(parents=True)
    freqtrade.write_text("#!/bin/sh\n")
    worker = repo / "scripts/formal_strategy_research_worker.py"
    worker.parent.mkdir(parents=True)
    worker.write_text("pass\n")
    freqtrade.chmod(0o755)
    monkeypatch.setenv("FREQTRADE_BINARY", str(freqtrade))
    settings = get_settings().model_copy(
        update={
            "canonical_repo_root": repo,
            "market_data_dir": Path("user_data/data"),
            "allow_dry_run_trading": allow_dry_run,
        }
    )
    coordinator = FormalStrategyResearchCoordinator(
        settings=settings, clock=lambda: NOW, popen=lambda *args, **kwargs: object()
    )
    if ownership:
        coordinator.runtime_dir.mkdir(parents=True)
        coordinator.ownership_path.write_text(
            json.dumps(
                {
                    "schema_version": "freqtrade-ai-formal-research-ownership-v1",
                    "scope": "FORMAL_STRATEGY_RESEARCH",
                    "canonical_root": str(repo.resolve()),
                    "owner_task_id": "task-1",
                    "confirmed_at": (NOW - timedelta(minutes=1)).isoformat(),
                    "expires_at": (NOW + timedelta(minutes=19)).isoformat(),
                }
            )
        )
    return coordinator


def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_formal_research_fails_closed_without_fresh_ownership(tmp_path, monkeypatch):
    coordinator = build_coordinator(tmp_path, monkeypatch, ownership=False)
    with db_session() as db:
        result = coordinator.start(db, trigger="manual")
        events = db.query(StrategyResearchAttemptEvent).all()
    assert result.status == "BLOCKED"
    assert result.reason_code == "OWNERSHIP_EVIDENCE_MISSING_OR_STALE"
    assert result.requested_count == 0
    assert result.generated_count == 0
    assert result.persisted_count == 0
    assert len(events) == 1
    assert events[0].outcome == "NOT_GENERATED"
    assert events[0].requested_count == 0


def test_formal_research_rejects_unsafe_dry_run_configuration(tmp_path, monkeypatch):
    coordinator = build_coordinator(
        tmp_path, monkeypatch, ownership=True, allow_dry_run=True
    )
    with db_session() as db:
        result = coordinator.start(db, trigger="manual")
    assert result.reason_code == "UNSAFE_EXECUTION_TARGET"


def test_formal_research_fails_closed_when_target_matrix_is_incomplete(
    tmp_path, monkeypatch
):
    coordinator = build_coordinator(tmp_path, monkeypatch)
    (
        coordinator.repo
        / "user_data/data/futures/SOL_USDT_USDT-5m-futures.feather"
    ).unlink()

    with db_session() as db:
        result = coordinator.start(db, trigger="manual")
        events = db.query(StrategyResearchAttemptEvent).all()

    assert result.reason_code == "MARKET_DATA_MATRIX_MISSING"
    assert result.requested_count == 0
    assert events[0].outcome == "NOT_GENERATED"


def test_formal_research_starts_exact_ten_candidate_shared_worker(tmp_path, monkeypatch):
    calls = []
    coordinator = build_coordinator(tmp_path, monkeypatch)
    coordinator.popen = lambda command, **kwargs: calls.append((command, kwargs)) or object()
    coordinator._repository_commit = lambda: "a" * 40
    with db_session() as db:
        result = coordinator.start(db, trigger="manual")
        events = db.query(StrategyResearchAttemptEvent).all()
        receipts = db.query(MarketDataQualityReceipt).all()
    assert result.status == "RUNNING"
    assert result.run_id == "202608090515"
    assert result.requested_count == 10
    assert len(calls) == 1
    assert len(events) == 1
    assert len(receipts) == 6
    assert events[0].outcome == "RUNNING"
    assert events[0].market_data_quality_receipt_id == receipts[0].id
    assert receipts[0].status == "PASSED"
    command = calls[0][0]
    assert "formal_strategy_research_worker.py" in command[1]
    assert command[command.index("--trigger") + 1] == "manual"
    assert command[command.index("--deadline-seconds") + 1] == "3600"
    assert command[command.index("--heartbeat-seconds") + 1] == "5"
    assert command[command.index("--attempt-id") + 1] == result.attempt_id
    manifest = json.loads(command[command.index("--expected-market-data-manifest") + 1])
    assert len(manifest) == 6
    assert {item["sha256"] for item in manifest} == {receipt.file_sha256 for receipt in receipts}
    state = json.loads(coordinator.state_path.read_text())
    assert state["phase"] == "STARTING"
    assert state["cleanup_status"] == "NOT_REQUIRED"
    assert state["heartbeat_at"] == NOW.isoformat()
    assert state["deadline_at"] == (NOW + timedelta(hours=1)).isoformat()
    assert calls[0][1]["pass_fds"]


def test_status_marks_held_lock_with_stale_heartbeat_as_blocked(tmp_path, monkeypatch):
    coordinator = build_coordinator(tmp_path, monkeypatch)
    coordinator.state_path.write_text(
        json.dumps(
            {
                "schema_version": "freqtrade-ai-formal-research-state-v1",
                "status": "RUNNING",
                "run_id": "202608090515",
                "trigger": "automation",
                "started_at": (NOW - timedelta(minutes=2)).isoformat(),
                "heartbeat_at": (NOW - timedelta(seconds=21)).isoformat(),
                "deadline_at": (NOW + timedelta(minutes=58)).isoformat(),
                "phase": "RUNNING",
                "cleanup_status": "NOT_REQUIRED",
            }
        )
    )
    held_lock = coordinator._try_lock()
    assert held_lock is not None
    try:
        with db_session() as db:
            result = coordinator.status(db)
    finally:
        import fcntl

        fcntl.flock(held_lock.fileno(), fcntl.LOCK_UN)
        held_lock.close()
    assert result.status == "BLOCKED"
    assert result.active is True
    assert result.reason_code == "WORKER_HEARTBEAT_STALE"
    assert result.phase == "RUNNING"


def test_status_marks_passed_deadline_as_cleanup_pending(tmp_path, monkeypatch):
    coordinator = build_coordinator(tmp_path, monkeypatch)
    coordinator.state_path.write_text(
        json.dumps(
            {
                "schema_version": "freqtrade-ai-formal-research-state-v1",
                "status": "RUNNING",
                "run_id": "202608090515",
                "heartbeat_at": NOW.isoformat(),
                "deadline_at": (NOW - timedelta(seconds=1)).isoformat(),
                "phase": "TERMINATING",
                "cleanup_status": "NOT_REQUIRED",
            }
        )
    )
    held_lock = coordinator._try_lock()
    assert held_lock is not None
    try:
        with db_session() as db:
            result = coordinator.status(db)
    finally:
        import fcntl

        fcntl.flock(held_lock.fileno(), fcntl.LOCK_UN)
        held_lock.close()
    assert result.status == "BLOCKED"
    assert result.active is True
    assert result.reason_code == "WORKER_DEADLINE_CLEANUP_PENDING"


def test_deadline_terminal_state_is_not_replayed_in_same_slot(tmp_path, monkeypatch):
    coordinator = build_coordinator(tmp_path, monkeypatch)
    calls = []
    coordinator.popen = lambda command, **kwargs: calls.append((command, kwargs)) or object()
    coordinator.state_path.write_text(
        json.dumps(
            {
                "schema_version": "freqtrade-ai-formal-research-state-v1",
                "status": "FAILED",
                "reason_code": "WORKER_DEADLINE_EXCEEDED",
                "run_id": "202608090515",
                "cleanup_status": "TERM_CONFIRMED",
                "phase": "FINISHED",
            }
        )
    )
    with db_session() as db:
        result = coordinator.start(db, trigger="manual")
    assert result.status == "BLOCKED"
    assert result.reason_code == "DUPLICATE_SLOT_STATE"
    assert calls == []


def test_unconfirmed_cleanup_blocks_every_new_slot(tmp_path, monkeypatch):
    coordinator = build_coordinator(tmp_path, monkeypatch)
    coordinator.state_path.write_text(
        json.dumps(
            {
                "schema_version": "freqtrade-ai-formal-research-state-v1",
                "status": "BLOCKED",
                "reason_code": "PROCESS_GROUP_CLEANUP_UNCONFIRMED",
                "run_id": "202608090500",
                "cleanup_status": "UNCONFIRMED",
                "phase": "FINISHED",
            }
        )
    )
    with db_session() as db:
        result = coordinator.start(db, trigger="automation")
    assert result.status == "BLOCKED"
    assert result.reason_code == "PROCESS_GROUP_CLEANUP_UNCONFIRMED"


def test_formal_research_uses_nested_okx_data_when_root_futures_also_exists(
    tmp_path, monkeypatch
):
    coordinator = build_coordinator(tmp_path, monkeypatch)
    root_market = (
        coordinator.repo
        / "user_data/data/futures/BTC_USDT_USDT-15m-futures.feather"
    )
    nested_market = (
        coordinator.repo
        / "user_data/data/okx/futures/BTC_USDT_USDT-15m-futures.feather"
    )
    root_market.unlink()
    nested_market.parent.mkdir(parents=True)
    nested_market.write_bytes(b"fixture")

    _, datadir = coordinator._paths()

    assert datadir == coordinator.repo / "user_data/data/okx"


def test_formal_research_reports_exact_candidate_count_blocker(tmp_path, monkeypatch):
    coordinator = build_coordinator(tmp_path, monkeypatch)
    next((coordinator.repo / "research/strategy_candidates").glob("*.py")).unlink()
    with db_session() as db:
        result = coordinator.start(db, trigger="manual")
    assert result.reason_code == "CANDIDATE_SET_INCOMPLETE"
    assert "当前为 9 条" in result.reason


def test_formal_research_does_not_replay_inconsistent_running_state(tmp_path, monkeypatch):
    coordinator = build_coordinator(tmp_path, monkeypatch)
    calls = []
    coordinator.popen = lambda command, **kwargs: calls.append((command, kwargs)) or object()
    coordinator._repository_commit = lambda: "a" * 40
    coordinator.state_path.write_text(
        json.dumps(
            {
                "schema_version": "freqtrade-ai-formal-research-state-v1",
                "status": "RUNNING",
                "run_id": "202608090515",
                "trigger": "automation",
            }
        )
    )
    with db_session() as db:
        observed = coordinator.status(db)
        assert observed.reason_code == "RUN_STATE_INCONSISTENT"
        assert json.loads(coordinator.state_path.read_text())["status"] == "RUNNING"

        result = coordinator.start(db, trigger="manual")
        assert result.status == "BLOCKED"
        assert result.reason_code == "ORPHANED_RUN_STATE"
        assert result.requested_count == 0
        assert calls == []
        terminal = json.loads(coordinator.state_path.read_text())
        assert terminal["status"] == "BLOCKED"
        assert terminal["reason_code"] == "ORPHANED_RUN_STATE"

        restarted = coordinator.start(db, trigger="manual")

    assert restarted.status == "RUNNING"
    assert restarted.run_id == "202608090515"
    assert len(calls) == 1


def test_formal_research_recovers_persisted_batch_without_replay(tmp_path, monkeypatch):
    coordinator = build_coordinator(tmp_path, monkeypatch)
    calls = []
    coordinator.popen = lambda command, **kwargs: calls.append((command, kwargs)) or object()
    coordinator.state_path.write_text(
        json.dumps(
            {
                "schema_version": "freqtrade-ai-formal-research-state-v1",
                "status": "RUNNING",
                "run_id": "202608090515",
                "trigger": "automation",
                "started_at": (NOW - timedelta(minutes=4)).isoformat(),
            }
        )
    )
    with db_session() as db:
        db.add(
            StrategyResearchBatch(
                run_id="202608090515",
                source_type="codex",
                repository_commit="a" * 40,
                report_schema_version="freqtrade-ai-strategy-candidate-research-v1",
                report_path="reports/research/test.json",
                report_digest="b" * 64,
                status="VALIDATED",
                requested_count=10,
                generated_count=10,
                persisted_count=10,
                qualified_count=0,
                rejected_count=10,
                safety_snapshot={"allow_real_funds": False, "real_orders": False},
                selection_policy={
                    "max_drawdown_per_validation_window": 0.10,
                    "validation_requires_positive_net_profit": True,
                },
                window_evidence=[],
                completed_at=NOW - timedelta(minutes=1),
            )
        )
        db.commit()

        result = coordinator.start(db, trigger="manual")

    assert result.status == "COMPLETED"
    assert result.reason_code == "RECOVERED_PERSISTED_BATCH"
    assert result.requested_count == 10
    assert result.generated_count == 10
    assert result.persisted_count == 10
    assert result.quality_contract == {
        "max_drawdown_per_validation_window": 0.10,
        "validation_requires_positive_net_profit": True,
    }
    assert calls == []
    terminal = json.loads(coordinator.state_path.read_text())
    assert terminal["status"] == "COMPLETED"
