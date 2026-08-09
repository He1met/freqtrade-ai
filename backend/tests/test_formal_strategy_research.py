from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models import Base
from app.services.formal_strategy_research import FormalStrategyResearchCoordinator


NOW = datetime(2026, 8, 9, 5, 22, tzinfo=timezone.utc)


def build_coordinator(tmp_path, monkeypatch, *, ownership=True, allow_dry_run=False):
    repo = tmp_path / "repo"
    (repo / "research/strategy_candidates").mkdir(parents=True)
    for index in range(10):
        (repo / f"research/strategy_candidates/{index:02d}_candidate.py").write_text("pass\n")
    data = repo / "user_data/data/futures/BTC_USDT_USDT-15m-futures.feather"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"fixture")
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
    assert result.status == "BLOCKED"
    assert result.reason_code == "OWNERSHIP_EVIDENCE_MISSING_OR_STALE"


def test_formal_research_rejects_unsafe_dry_run_configuration(tmp_path, monkeypatch):
    coordinator = build_coordinator(
        tmp_path, monkeypatch, ownership=True, allow_dry_run=True
    )
    with db_session() as db:
        result = coordinator.start(db, trigger="manual")
    assert result.reason_code == "UNSAFE_EXECUTION_TARGET"


def test_formal_research_starts_exact_ten_candidate_shared_worker(tmp_path, monkeypatch):
    calls = []
    coordinator = build_coordinator(tmp_path, monkeypatch)
    coordinator.popen = lambda command, **kwargs: calls.append((command, kwargs)) or object()
    coordinator._repository_commit = lambda: "a" * 40
    with db_session() as db:
        result = coordinator.start(db, trigger="manual")
    assert result.status == "RUNNING"
    assert result.run_id == "202608090515"
    assert result.requested_count == 10
    assert len(calls) == 1
    command = calls[0][0]
    assert "formal_strategy_research_worker.py" in command[1]
    assert command[command.index("--trigger") + 1] == "manual"
    assert calls[0][1]["pass_fds"]


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
        result = coordinator.start(db, trigger="manual")
    assert result.status == "BLOCKED"
    assert result.reason_code == "RUN_STATE_INCONSISTENT"
