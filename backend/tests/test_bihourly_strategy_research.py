from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, ResearchJob
from app.core.strategy_research_matrix import RESEARCH_TARGETS
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.schemas.deepseek_backtest_loop import DeepSeekBacktestLoopRequest
from app.services.bihourly_strategy_research import (
    BihourlyStrategyResearchBlocked,
    BihourlyStrategyResearchService,
)
from app.services.bihourly_strategy_research_trigger import (
    BihourlyStrategyResearchTrigger,
    _canonical_peer_owner_url,
)
from app.services.candidate_validation_queue_read import (
    CandidateValidationQueueReadService,
)
from app.services.strategy_candidate_validation_queue import (
    CANDIDATE_VALIDATION_OPERATION,
)


NOW = datetime(2026, 8, 11, 8, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def database():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        ensure_execution_scope_catalog(db)
        db.commit()
        yield db
    engine.dispose()


def _root(tmp_path: Path) -> Path:
    target = tmp_path / "canonical"
    shutil.copytree(
        REPO_ROOT / "research/strategy_candidates",
        target / "research/strategy_candidates",
    )
    return target


def _market() -> dict[str, dict]:
    result = {}
    for pair in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"):
        for timeframe in ("5m", "15m"):
            result[f"{pair}|{timeframe}"] = {
                "pair": pair,
                "timeframe": timeframe,
                "path": f"/isolated/{pair}-{timeframe}.feather",
                "file_sha256": "a" * 64,
                "validation_lineage_digest": "b" * 64,
                "first_open_at": "2023-07-01T00:00:00+00:00",
                "last_open_at": NOW.isoformat(),
                "source_receipt_path": f"/isolated/{pair}-{timeframe}.source.json",
                "source_receipt_sha256": "c" * 64,
            }
    return result


def _runtime_snapshot(*, blocked_openings: bool = False) -> dict:
    return {
        "status": "BLOCKED_OPENINGS" if blocked_openings else "VERIFIED",
        "execution_target": {"active": "OKX_DEMO", "status": "READY"},
        "trading": {"live": False, "dry_run": False, "real_orders": False},
        "database": {"kind": "postgresql", "schema": "verified"},
        "okx_runtime": {
            "execution_target": "OKX_DEMO",
            "adapter": "ATTESTED",
            "writer": "UNIQUE",
            "reconciliation": "RECOVERED",
            "automation_guard": "BLOCKED" if blocked_openings else "RUNNING",
        },
        "services": [
            {"service": name, "running": True}
            for name in ("backend", "worker", "frontend", "okx_runtime")
        ],
    }


def _install_market_receipts(
    root: Path,
    *,
    now: datetime,
) -> Path:
    datadir = root / "market"
    for target in RESEARCH_TARGETS:
        path = target.market_path(datadir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{target.key}\n".encode())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sidecar = {
            "schema_version": "okx-public-candle-file-source-v1",
            "source_type": (
                "OKX_PUBLIC_REST"
                if target.timeframe == "5m"
                else "DERIVED_FROM_OKX_PUBLIC_REST"
            ),
            "timeframe": target.timeframe,
            "credentials_used": False,
            "account_endpoint_used": False,
            "orders_submitted": False,
            "downloaded_at": now.isoformat(),
            "data_file_sha256": digest,
        }
        if target.timeframe == "15m":
            five = next(
                item
                for item in RESEARCH_TARGETS
                if item.pair == target.pair and item.timeframe == "5m"
            )
            sidecar["parent_five_minute_sha256"] = hashlib.sha256(
                five.market_path(datadir).read_bytes()
            ).hexdigest()
        path.with_suffix(path.suffix + ".source.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )
    return datadir


def test_market_freshness_uses_closed_candle_time_without_relaxing_gate(
    database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    now = datetime(2026, 8, 11, 17, 36, 29, tzinfo=timezone.utc)
    five_open = now.replace(minute=30, second=0, microsecond=0)
    fifteen_open = now.replace(minute=15, second=0, microsecond=0)
    datadir = _install_market_receipts(
        root,
        now=now,
    )
    service = BihourlyStrategyResearchService(
        database, canonical_root=root, datadir=datadir
    )
    monkeypatch.setattr(
        "app.services.bihourly_strategy_research._data_bounds",
        lambda path: (
            datetime(2023, 7, 1, tzinfo=timezone.utc),
            fifteen_open if "-15m-" in path.name else five_open,
        ),
    )
    monkeypatch.setattr(
        "app.services.bihourly_strategy_research._matching_market_data_files",
        lambda datadir, pair, timeframe: [
            next(
                target.market_path(datadir)
                for target in RESEARCH_TARGETS
                if target.pair == pair and target.timeframe == timeframe
            )
        ],
    )
    monkeypatch.setattr(
        "app.services.bihourly_strategy_research._market_data_files_digest",
        lambda _paths: "a" * 64,
    )

    evidence = service._validate_market_data(now=now)
    assert {item["last_close_at"] for item in evidence.values()} == {
        "2026-08-11T17:30:00+00:00",
        "2026-08-11T17:35:00+00:00",
    }

    stale_open = datetime(2026, 8, 11, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.services.bihourly_strategy_research._data_bounds",
        lambda path: (
            datetime(2023, 7, 1, tzinfo=timezone.utc),
            stale_open if "-15m-" in path.name else five_open,
        ),
    )
    with pytest.raises(BihourlyStrategyResearchBlocked, match="MARKET_DATA_NOT_FRESH"):
        service._validate_market_data(now=now)

    not_closed_open = datetime(2026, 8, 11, 17, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.services.bihourly_strategy_research._data_bounds",
        lambda path: (
            datetime(2023, 7, 1, tzinfo=timezone.utc),
            not_closed_open if "-15m-" in path.name else five_open,
        ),
    )
    with pytest.raises(BihourlyStrategyResearchBlocked, match="MARKET_DATA_NOT_FRESH"):
        service._validate_market_data(now=now)


def test_manual_and_automation_share_fail_closed_generation_trigger(
    database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    generation = BihourlyStrategyResearchService(
        database, canonical_root=root, datadir=tmp_path / "market"
    )
    monkeypatch.setattr(generation, "_validate_market_data", lambda **_kwargs: _market())
    refresh_calls: list[str] = []
    trigger = BihourlyStrategyResearchTrigger(
        database,
        canonical_root=root,
        datadir=tmp_path / "market",
        receipt_path=tmp_path / "receipt.json",
        runtime_snapshot=lambda: _runtime_snapshot(blocked_openings=True),
        repository_state=lambda: ("main", "d" * 40),
        refresh=lambda: refresh_calls.append("refresh"),
        generation_service=generation,
    )

    manual = trigger.run(
        trigger="manual", run_id="2026081108", owner_task_id="shared-owner", now=NOW
    )
    scheduled = trigger.run(
        trigger="automation",
        run_id="2026081108",
        owner_task_id="shared-owner",
        now=NOW + timedelta(minutes=1),
    )

    assert manual.status == "GENERATED"
    assert manual.persisted_count == 60
    assert manual.runtime_status == "BLOCKED_OPENINGS"
    assert manual.opening_guard == "BLOCKED"
    assert manual.backtest_started is False
    assert scheduled.status == "NO_OP"
    assert scheduled.reason_code == "RESEARCH_BATCH_ALREADY_PERSISTED"
    assert refresh_calls == ["refresh"]


def test_trigger_no_ops_before_refresh_when_runtime_contract_is_unsafe(
    database, tmp_path: Path
) -> None:
    root = _root(tmp_path)
    unsafe = _runtime_snapshot()
    unsafe["trading"]["real_orders"] = True
    calls: list[str] = []
    result = BihourlyStrategyResearchTrigger(
        database,
        canonical_root=root,
        datadir=tmp_path / "market",
        receipt_path=tmp_path / "receipt.json",
        runtime_snapshot=lambda: unsafe,
        repository_state=lambda: ("main", "d" * 40),
        refresh=lambda: calls.append("unexpected"),
    ).run(trigger="manual", now=NOW)

    assert result.status == "NO_OP"
    assert result.reason_code == "RUNTIME_TRADING_SAFETY_INVALID"
    assert calls == []
    assert database.scalar(select(ResearchJob).limit(1)) is None


def test_owner_mediated_url_is_local_peer_only_and_drops_runtime_credentials() -> None:
    peer = _canonical_peer_owner_url(
        "postgresql+psycopg://freqtrade:secret@localhost:5432/freqtrade_ai"
    )
    assert peer.username is None
    assert peer.password is None
    assert peer.database == "freqtrade_ai"
    assert peer.query == {"host": "/tmp", "port": "5432"}
    with pytest.raises(RuntimeError):
        _canonical_peer_owner_url(
            "postgresql+psycopg://freqtrade:secret@example.com/freqtrade_ai"
        )


def test_generation_persists_exactly_sixty_pending_without_starting_backtests(
    database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    service = BihourlyStrategyResearchService(
        database,
        canonical_root=root,
        datadir=tmp_path / "market",
    )
    monkeypatch.setattr(service, "_validate_market_data", lambda **_kwargs: _market())
    refresh_calls: list[str] = []
    result = service.run_generation_only(
        run_id="2026081108",
        repository_commit="d" * 40,
        owner_task_id="test-owner",
        refresh=lambda: refresh_calls.append("refreshed"),
        now=NOW,
    )
    jobs = tuple(database.scalars(select(ResearchJob).order_by(ResearchJob.id)).all())
    assert result.status == "GENERATED"
    assert result.persisted_count == 60
    assert refresh_calls == ["refreshed"]
    assert len(jobs) == 60
    assert {job.operation for job in jobs} == {CANDIDATE_VALIDATION_OPERATION}
    assert {job.status for job in jobs} == {"PENDING"}
    assert {job.stage for job in jobs} == {"GENERATED_QUEUED"}
    assert all(job.started_at is None and job.backtest_run_id is None for job in jobs)
    assert {
        (job.request_payload["pair"], job.request_payload["timeframe"])
        for job in jobs
    } == {
        (pair, timeframe)
        for pair in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT")
        for timeframe in ("5m", "15m")
    }
    assert all(
        len(DeepSeekBacktestLoopRequest.model_validate(
            job.request_payload["validation_request"]
        ).validation_windows) == 4
        for job in jobs
    )
    profiles = [
        DeepSeekBacktestLoopRequest.model_validate(
            job.request_payload["validation_request"]
        ).backtest_profile
        for job in jobs
    ]
    assert all("path" not in profile["strategy"] for profile in profiles)
    assert {profile["data_source"]["datadir"] for profile in profiles} == {
        "user_data/data"
    }

    replay = service.run_generation_only(
        run_id="2026081108",
        repository_commit="d" * 40,
        owner_task_id="test-owner",
        refresh=lambda: refresh_calls.append("unexpected"),
        now=NOW + timedelta(minutes=1),
    )
    assert replay.status == "NO_OP"
    assert replay.reason_code == "RESEARCH_BATCH_ALREADY_PERSISTED"
    assert refresh_calls == ["refreshed"]


def test_valid_other_owner_causes_no_op_before_refresh(
    database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    ownership = root / ".freqtrade-ai/research/formal-strategy-research-ownership.json"
    ownership.parent.mkdir(parents=True)
    ownership.write_text(
        json.dumps(
            {
                "schema_version": "freqtrade-ai-formal-research-ownership-v1",
                "scope": "FORMAL_STRATEGY_RESEARCH",
                "canonical_root": str(root.resolve()),
                "owner_task_id": "other-owner",
                "confirmed_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    service = BihourlyStrategyResearchService(
        database, canonical_root=root, datadir=tmp_path / "market"
    )
    refresh_calls: list[str] = []
    result = service.run_generation_only(
        run_id="2026081110",
        repository_commit="d" * 40,
        owner_task_id="this-owner",
        refresh=lambda: refresh_calls.append("unexpected"),
        now=NOW,
    )
    assert result.status == "NO_OP"
    assert result.reason_code == "RESEARCH_OWNED_BY_ANOTHER_EXECUTOR"
    assert refresh_calls == []


def test_queue_read_exposes_pending_claimed_and_qualified_deployment_states(
    database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    service = BihourlyStrategyResearchService(
        database, canonical_root=root, datadir=tmp_path / "market"
    )
    monkeypatch.setattr(service, "_validate_market_data", lambda **_kwargs: _market())
    service.run_generation_only(
        run_id="2026081112",
        repository_commit="e" * 40,
        owner_task_id="test-owner",
        refresh=lambda: None,
        now=NOW,
    )
    first = database.scalar(select(ResearchJob).order_by(ResearchJob.id).limit(1))
    first.status = "PENDING"
    first.stage = "CANDIDATE_APPROVED"
    database.commit()
    projection = CandidateValidationQueueReadService(database).read()
    assert projection["serial_execution"] is True
    assert projection["batch"]["generated_count"] == 60
    assert projection["batch"]["active_count"] == 0
    assert {item["status"] for item in projection["waiting_candidates"]} == {
        "PENDING",
        "QUALIFIED_PENDING_DEPLOYMENT",
    }
    assert projection["completed_candidates"] == []


def test_partial_existing_batch_fails_closed_without_refresh(
    database, tmp_path: Path
) -> None:
    database.add(
        ResearchJob(
            execution_scope_id="LOCAL_DRY_RUN",
            job_type="formal_candidate_validation",
            operation=CANDIDATE_VALIDATION_OPERATION,
            idempotency_key_digest="1" * 64,
            request_hash="2" * 64,
            request_payload={"research_run_id": "2026081114"},
            status="PENDING",
            stage="GENERATED_QUEUED",
            max_attempts=2,
            evidence_snapshot={},
        )
    )
    database.commit()
    calls: list[str] = []
    with pytest.raises(
        BihourlyStrategyResearchBlocked,
        match="RESEARCH_BATCH_PARTIAL_OR_CONFLICTING",
    ):
        BihourlyStrategyResearchService(
            database,
            canonical_root=_root(tmp_path),
            datadir=tmp_path / "market",
        ).run_generation_only(
            run_id="2026081114",
            repository_commit="e" * 40,
            owner_task_id="test-owner",
            refresh=lambda: calls.append("unexpected"),
            now=NOW,
        )
    assert calls == []


def test_ownership_loss_before_commit_rolls_back_and_returns_no_op(
    database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    service = BihourlyStrategyResearchService(
        database, canonical_root=root, datadir=tmp_path / "market"
    )
    monkeypatch.setattr(service, "_validate_market_data", lambda **_kwargs: _market())
    monkeypatch.setattr(service, "_ownership_matches", lambda _expected: False)
    result = service.run_generation_only(
        run_id="2026081116",
        repository_commit="f" * 40,
        owner_task_id="losing-owner",
        refresh=lambda: None,
        now=NOW,
    )
    assert result.status == "NO_OP"
    assert result.reason_code == "RESEARCH_OWNERSHIP_LOST_BEFORE_QUEUE_COMMIT"
    assert database.scalar(select(ResearchJob).limit(1)) is None
