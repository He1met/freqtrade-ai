from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

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
from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_renderer import StrategyCodeRenderer


NOW = datetime(2026, 8, 9, 5, 22, tzinfo=timezone.utc)


def build_coordinator(tmp_path, monkeypatch, *, ownership=True, allow_dry_run=False):
    repo = tmp_path / "repo"
    for timeframe in ("5m", "15m"):
        candidate_root = repo / "research/strategy_candidates" / timeframe
        candidate_root.mkdir(parents=True)
        for index in range(1, 11):
            blueprint = StrategyBlueprint(
                name=f"Candidate {index} {timeframe}",
                slug=f"candidate-{index}-{timeframe}",
                class_name=f"Candidate{index}{timeframe.upper()}",
                timeframe=timeframe,
                indicators=[{"name": "rsi", "kind": "rsi", "period": 14}],
                entry_rules=[{"indicator": "rsi", "operator": "<", "value": 35.0}],
            )
            stem = candidate_root / f"{index:02d}_candidate"
            stem.with_suffix(".py").write_text(StrategyCodeRenderer().render(blueprint))
            stem.with_suffix(".blueprint.json").write_text(
                json.dumps(blueprint.model_dump(mode="json"))
            )
    source_matrix = []
    downloaded_at = NOW.isoformat()
    for asset in ("BTC", "ETH", "SOL"):
        frames = {
            "5m": pd.DataFrame(
                {
                    "date": pd.date_range(
                        "2026-08-09T04:00:00Z", periods=13, freq="5min"
                    ),
                    "open": [100.0] * 13,
                    "high": [102.0] * 13,
                    "low": [99.0] * 13,
                    "close": [101.0] * 13,
                    "volume": [2.0] * 13,
                }
            )
        }
        indexed = frames["5m"].iloc[:12].set_index("date")
        frames["15m"] = indexed.resample("15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).reset_index()
        source = {
            "pair": f"{asset}/USDT:USDT",
            "instrument_id": f"{asset}-USDT-SWAP",
            "first_open_at": frames["5m"]["date"].iloc[0].isoformat(),
            "last_open_at": frames["5m"]["date"].iloc[-1].isoformat(),
            "row_count": len(frames["5m"]),
            "fifteen_minute_row_count": len(frames["15m"]),
        }
        for timeframe in ("5m", "15m"):
            data = repo / (
                f"user_data/data/futures/{asset}_USDT_USDT-{timeframe}-futures.feather"
            )
            data.parent.mkdir(parents=True, exist_ok=True)
            frames[timeframe].to_feather(data)
            digest = hashlib.sha256(data.read_bytes()).hexdigest()
            data.with_suffix(data.suffix + ".source.json").write_text(json.dumps({
                "schema_version": "okx-public-candle-file-source-v1",
                "source_type": (
                    "OKX_PUBLIC_REST" if timeframe == "5m"
                    else "DERIVED_FROM_OKX_PUBLIC_REST"
                ),
                "credentials_used": False,
                "account_endpoint_used": False,
                "orders_submitted": False,
                "data_file_sha256": digest,
                "response_chain_sha256": "a" * 64,
                "downloaded_at": downloaded_at,
            }))
            prefix = "five_minute" if timeframe == "5m" else "fifteen_minute"
            source[f"{prefix}_path"] = str(data.relative_to(repo))
            source[f"{prefix}_sha256"] = digest
        source_matrix.append(source)
    aggregate = repo / "reports/research/okx-public-candle-source-20260809.json"
    aggregate.parent.mkdir(parents=True)
    aggregate.write_text(json.dumps({
        "schema_version": "okx-public-candle-source-receipt-v1",
        "downloaded_at": downloaded_at,
        "execution_scope": "PUBLIC_MARKET_DATA_ONLY",
        "credentials_used": False,
        "account_endpoint_used": False,
        "orders_submitted": False,
        "sources": source_matrix,
    }))
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


def test_formal_research_starts_exact_sixty_candidate_shared_worker(tmp_path, monkeypatch):
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
    assert result.requested_count == 60
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


@pytest.mark.parametrize("field", ["five_minute_sha256", "last_open_at"])
def test_formal_research_binds_actual_file_to_aggregate_source_receipt(
    tmp_path, monkeypatch, field
):
    coordinator = build_coordinator(tmp_path, monkeypatch)
    receipt_path = next(
        (coordinator.repo / "reports/research").glob(
            "okx-public-candle-source-*.json"
        )
    )
    receipt = json.loads(receipt_path.read_text())
    receipt["sources"][0][field] = (
        "0" * 64 if field.endswith("sha256") else "2026-08-09T03:55:00+00:00"
    )
    receipt_path.write_text(json.dumps(receipt))

    with db_session() as db:
        result = coordinator.start(db, trigger="manual")
        events = db.query(StrategyResearchAttemptEvent).all()
        qualities = db.query(MarketDataQualityReceipt).all()

    assert result.status == "BLOCKED"
    assert result.reason_code == "MARKET_DATA_SOURCE_RECEIPT_BLOCKED"
    assert result.requested_count == 0
    assert len(qualities) == 6
    assert all(quality.status == "PASSED" for quality in qualities)
    assert events[0].outcome == "NOT_GENERATED"


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
    next((coordinator.repo / "research/strategy_candidates/5m").glob("*.py")).unlink()
    with db_session() as db:
        result = coordinator.start(db, trigger="manual")
    assert result.reason_code == "CANDIDATE_SET_INCOMPLETE"
    assert "5m=9、15m=10" in result.reason


def test_formal_research_rejects_non_equivalent_candidate_blueprint(tmp_path, monkeypatch):
    coordinator = build_coordinator(tmp_path, monkeypatch)
    candidate = next(
        (coordinator.repo / "research/strategy_candidates/15m").glob("*.py")
    )
    candidate.write_text(candidate.read_text() + "\n# drift\n")
    with db_session() as db:
        result = coordinator.start(db, trigger="manual")
    assert result.reason_code == "CANDIDATE_BLUEPRINT_INVALID"


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
