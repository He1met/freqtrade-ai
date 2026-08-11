import hashlib
import json
import os
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest
from app.adapters.freqtrade.backtest_runner import FreqtradeBacktestRunner
from app.adapters.freqtrade.cli_runner import FreqtradeCliRunner
from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.core.config import Settings
from app.core.execution_target import okx_demo_execution_target_manifest
from app.db.session import create_database_engine, create_session_factory
from app.models import (
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    FullChainRun,
    FullChainStageRun,
    ResearchJobAttempt,
    Strategy,
    StrategyCandidateApproval,
    StrategyDeployment,
    StrategyGenerationRun,
    StrategyScore,
    StrategyValidationPlan,
    StrategyValidationWindow,
    StrategyVersion,
)
from app.repositories import ResearchJobRepository
from app.schemas import (
    DeepSeekBacktestLoopRequest,
    OperationEvidence,
    operation_error_evidence,
)
from app.schemas.deepseek_backtest_loop import DeepSeekBacktestLoopResponse
from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.deepseek_backtest_loop import DeepSeekBacktestLoopService
from app.services.research_full_chain_orchestrator import (
    ResearchFullChainBlocked,
    ResearchFullChainOrchestrator,
)
from app.services.research_job_queue import ResearchJobQueueService
from app.services.strategy_generation import (
    LLMProviderConfig,
    OpenAICompatibleStrategyBlueprintProvider,
    StrategyGenerationService,
)
from app.services.strategy_renderer import StrategyCodeRenderer
from app.services.strategy_blueprint_equivalence import prove_blueprint_code_equivalence
from app.services.strategy_candidate_validation_queue import (
    GeneratedCandidate,
    StrategyCandidateValidationQueueService,
)
from app.workers.deepseek_backtest_worker import DeepSeekBacktestWorker


def session_factory(tmp_path: Path):
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'worker.sqlite'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def request(*, allow_real_call: bool) -> DeepSeekBacktestLoopRequest:
    return DeepSeekBacktestLoopRequest(
        prompt_summary="Generate one safe local research strategy.",
        allow_real_call=allow_real_call,
        backtest_profile={},
    )


class BlockedService:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def run(self, payload: DeepSeekBacktestLoopRequest) -> DeepSeekBacktestLoopResponse:
        self.calls.append(payload.prompt_summary)
        return DeepSeekBacktestLoopResponse(
            overall_status="blocked",
            evidence=operation_error_evidence(
                status="BLOCKED",
                reason="Local market data is missing.",
                next_action="Provide approved local market data and enqueue a new job.",
            ),
        )


class FailingService:
    def run(self, payload: DeepSeekBacktestLoopRequest) -> DeepSeekBacktestLoopResponse:
        raise RuntimeError("provider token=synthetic-sensitive-value failed without safe response")


class FakeHeartbeat:
    def __init__(self) -> None:
        self.lease_lost = Event()


class CancelingService:
    def __init__(self, db, job_id: int) -> None:
        self.db = db
        self.job_id = job_id

    def run(self, payload: DeepSeekBacktestLoopRequest) -> DeepSeekBacktestLoopResponse:
        ResearchJobRepository(self.db).cancel(
            self.job_id,
            "Cancelled after provider response.",
        )
        return DeepSeekBacktestLoopResponse(
            overall_status="blocked",
            evidence=operation_error_evidence(
                status="BLOCKED",
                reason="Cancellation checkpoint response.",
                next_action="No action.",
            ),
        )


class MissingLinksService:
    def run(self, payload: DeepSeekBacktestLoopRequest) -> DeepSeekBacktestLoopResponse:
        return DeepSeekBacktestLoopResponse(
            overall_status="succeeded",
            evidence=OperationEvidence(
                status="SUCCESS",
                ids={"strategy_generation_run_id": 1},
                next_action="Validate durable lineage.",
                acceptance_ready=False,
            ),
        )


class CompletingContinuation:
    def __init__(self, db, calls: list[int]) -> None:
        self.db = db
        self.calls = calls

    def run(self, job_id: int, lease_token: str) -> None:
        self.calls.append(job_id)
        ResearchJobRepository(self.db).complete(
            job_id,
            lease_token,
            status="SUCCESS",
            stage="COMPLETED",
            links={},
            evidence_snapshot={
                "status": "SUCCESS",
                "acceptance_ready": True,
                "continuation": "completed",
            },
            error_message=None,
            provider_completed=False,
        )


def prepare_approved_candidate_resume(factory, *, allow_real_call: bool) -> int:
    with factory() as db:
        repository = ResearchJobRepository(db)
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=allow_real_call),
            idempotency_key=f"approved-resume-{allow_real_call}",
        ).id
        claimed = repository.claim_next(owner="research-worker", lease_seconds=60)
        assert claimed is not None and claimed.lease_token
        if allow_real_call:
            assert repository.mark_provider_attempt(job_id, claimed.lease_token)
        waiting = repository.wait_for_candidate_approval(
            job_id,
            claimed.lease_token,
            evidence_snapshot={
                "status": "AWAITING_APPROVAL",
                "acceptance_ready": False,
            },
        )
        assert waiting is not None
        resumed = repository.resume_after_candidate_approval(
            job_id,
            evidence_snapshot={
                "status": "PENDING",
                "acceptance_ready": False,
            },
        )
        assert resumed is not None
        return job_id


class MockLLMResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "blueprints": [
                {
                    "schema_version": "2",
                    "name": "Worker DeepSeek RSI Strategy",
                    "slug": "worker-deepseek-rsi",
                    "class_name": "WorkerDeepseekRsiStrategy",
                    "description": "Controlled provider fixture for the DB-backed worker test.",
                    "timeframe": "15m",
                    "indicators": [{"name": "rsi", "kind": "rsi", "period": 14}],
                    "entry_rules": [{"indicator": "rsi", "operator": "<", "value": 32}],
                    "exit_rules": [{"indicator": "rsi", "operator": ">", "value": 68}],
                    "tags": ["phase-9", "worker-integration-test"],
                }
            ]
        }


class MockLLMClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def post(self, url: str, *, headers: dict, json: dict, timeout: float) -> MockLLMResponse:
        self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return MockLLMResponse()


def write_market_data(tmp_path: Path) -> Path:
    datadir = tmp_path / "user_data" / "data"
    exchange_dir = datadir / "okx" / "futures"
    exchange_dir.mkdir(parents=True)
    exchange_dir.joinpath("BTC_USDT_USDT-15m-futures.json").write_text(
        json.dumps(
            [
                {"date": "2024-01-01", "close": 100.0},
                {"date": "2024-01-31", "close": 102.0},
                {"date": "2024-02-01", "close": 100.0},
                {"date": "2024-02-28", "close": 102.0},
                {"date": "2024-03-01", "close": 100.0},
                {"date": "2024-03-28", "close": 110.0},
                {"date": "2024-04-01", "close": 100.0},
                {"date": "2024-04-28", "close": 90.0},
                {"date": "2024-05-01", "close": 100.0},
                {"date": "2024-05-28", "close": 101.0},
            ]
        ),
        encoding="utf-8",
    )
    return datadir


def market_data_digest(datadir: Path) -> str:
    root = datadir / "okx"
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    assert files
    for path in files:
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def validation_windows(datadir: Path) -> list[dict[str, str]]:
    digest = market_data_digest(datadir)
    return [
        {
            "window_kind": "OOS",
            "timerange": "20240201-20240301",
            "expected_market_data_digest": digest,
        },
        {
            "window_kind": "WALK_FORWARD",
            "timerange": "20240301-20240401",
            "expected_market_data_digest": digest,
            "market_state": "bull",
        },
        {
            "window_kind": "WALK_FORWARD",
            "timerange": "20240401-20240501",
            "expected_market_data_digest": digest,
            "market_state": "bear",
        },
        {
            "window_kind": "WALK_FORWARD",
            "timerange": "20240501-20240601",
            "expected_market_data_digest": digest,
            "market_state": "range",
        },
    ]


def install_fake_freqtrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "freqtrade"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("FREQTRADE_BINARY", raising=False)


def local_profile(datadir: Path) -> dict:
    return {
        "schema_version": "2",
        "profile_name": "phase9-worker-local",
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "timerange": "20240101-20240201",
        "strategy": {"name": "WorkerDeepseekRsiStrategy"},
        "data_source": {
            "kind": "local",
            "exchange": "okx",
            "datadir": str(datadir),
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
    }


def test_worker_persists_terminal_response_and_never_reexecutes_terminal_job(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=False),
            idempotency_key="worker-blocked-job",
        ).id

    calls: list[str] = []
    worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=lambda db: BlockedService(calls),
        owner="test-worker",
        lease_seconds=60,
        heartbeat_interval_seconds=10,
    )

    assert worker.run_once() == job_id
    assert worker.run_once() is None
    assert calls == ["Generate one safe local research strategy."]
    with factory() as db:
        job = ResearchJobRepository(db).get(job_id)
        assert job is not None
        assert job.status == "BLOCKED"
        assert job.stage == "BLOCKED"
        assert job.error_message == "Local market data is missing."
        assert job.lease_owner is None
        assert job.lease_token is None
        assert job.evidence_snapshot["status"] == "BLOCKED"


@pytest.mark.parametrize("allow_real_call", [True, False])
def test_approved_candidate_resume_dispatches_continuation_without_repeating_research(
    tmp_path,
    allow_real_call,
) -> None:
    factory = session_factory(tmp_path)
    job_id = prepare_approved_candidate_resume(
        factory,
        allow_real_call=allow_real_call,
    )
    research_factory_calls: list[bool] = []
    continuation_calls: list[int] = []

    def forbidden_research_factory(db):
        research_factory_calls.append(True)
        raise AssertionError("approved candidate must not repeat research")

    worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=forbidden_research_factory,
        continuation_factory=lambda db: CompletingContinuation(db, continuation_calls),
        owner="signal-worker",
        lease_seconds=60,
    )

    assert worker.run_once() == job_id
    assert research_factory_calls == []
    assert continuation_calls == [job_id]
    with factory() as db:
        repository = ResearchJobRepository(db)
        job = repository.get(job_id)
        control = repository.get_control()
        assert job is not None
        assert job.status == "SUCCESS"
        assert job.stage == "COMPLETED"
        assert job.attempt_count == 1
        assert job.lease_token is None
        assert control.active_job_id is None
        if allow_real_call:
            assert job.provider_attempted_at is not None
            assert job.provider_completed_at is not None
        else:
            assert job.provider_attempted_at is None
            assert job.provider_completed_at is None


def test_approved_candidate_without_continuation_blocks_and_releases_lease(tmp_path) -> None:
    factory = session_factory(tmp_path)
    job_id = prepare_approved_candidate_resume(factory, allow_real_call=True)
    research_factory_calls: list[bool] = []

    def forbidden_research_factory(db):
        research_factory_calls.append(True)
        raise AssertionError("approved candidate must not repeat research")

    worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=forbidden_research_factory,
        continuation_factory=None,
        owner="unconfigured-signal-worker",
        lease_seconds=60,
    )

    assert worker.run_once() == job_id
    assert research_factory_calls == []
    with factory() as db:
        repository = ResearchJobRepository(db)
        job = repository.get(job_id)
        control = repository.get_control()
        assert job is not None
        assert job.status == "BLOCKED"
        assert job.stage == "SIGNAL"
        assert "continuation is not configured" in (job.error_message or "")
        assert job.lease_token is None
        assert control.active_job_id is None


def test_restart_marks_unknown_provider_outcome_stale_without_calling_provider(tmp_path) -> None:
    factory = session_factory(tmp_path)
    fixed_now = datetime.now(timezone.utc)
    with factory() as db:
        repository = ResearchJobRepository(db)
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=True),
            idempotency_key="provider-crash-window",
        ).id
        claimed = repository.claim_next(
            owner="crashed-worker",
            lease_seconds=10,
            now=fixed_now,
        )
        assert claimed is not None and claimed.lease_token
        ResearchFullChainOrchestrator(db).begin(job_id, claimed.lease_token)
        assert repository.mark_provider_attempt(
            job_id,
            claimed.lease_token,
            now=fixed_now,
        )

    with factory() as restarted_db:
        stale = ResearchJobRepository(restarted_db).expire_stale(
            fixed_now + timedelta(seconds=10)
        )
        assert stale is not None
        assert stale.status == "STALE"
        assert "automatic retry is forbidden" in (stale.error_message or "")
        assert restarted_db.query(FullChainRun).one().status == "STALE"
        assert restarted_db.query(FullChainStageRun).one().status == "STALE"
        assert (
            restarted_db.query(ResearchJobAttempt).one().evidence_snapshot
            == stale.evidence_snapshot
        )

    calls: list[str] = []
    restarted_worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=lambda db: BlockedService(calls),
        owner="restarted-worker",
        lease_seconds=60,
    )
    assert restarted_worker.run_once() is None
    assert calls == []
    with factory() as db:
        assert db.query(FullChainRun).one().status == "STALE"


def test_safe_restart_temporarily_marks_chain_stale_then_resumes_it(tmp_path) -> None:
    factory = session_factory(tmp_path)
    fixed_now = datetime.now(timezone.utc)
    with factory() as db:
        repository = ResearchJobRepository(db)
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=False),
            idempotency_key="safe-chain-recovery",
        ).id
        claimed = repository.claim_next(
            owner="crashed-before-provider",
            lease_seconds=60,
            now=fixed_now,
        )
        assert claimed is not None and claimed.lease_token
        ResearchFullChainOrchestrator(db).begin(job_id, claimed.lease_token)
        stale = repository.expire_stale(
            fixed_now + timedelta(seconds=60),
        )
        assert stale is not None
        assert db.query(FullChainRun).one().status == "STALE"
        assert db.query(FullChainStageRun).one().status == "STALE"

    with factory() as db:
        assert ResearchFullChainOrchestrator(db).recover_one_stale() == job_id
        job = ResearchJobRepository(db).get(job_id)
        chain = db.query(FullChainRun).one()
        assert job is not None
        assert job.status == "PENDING"
        assert job.stage == "GENERATION_RETRY"
        assert chain.status == "RUNNING"
        assert db.query(FullChainStageRun).one().status == "PREPARED"
        assert chain.completed_at is None
        assert chain.terminal_reason is None


def test_restart_reuses_same_attempt_when_provider_was_never_called(tmp_path) -> None:
    factory = session_factory(tmp_path)
    fixed_now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    with factory() as db:
        repository = ResearchJobRepository(db)
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=False),
            idempotency_key="crash-before-provider",
        ).id
        claimed = repository.claim_next(
            owner="crashed-before-provider",
            lease_seconds=10,
            now=fixed_now,
        )
        assert claimed is not None
        assert claimed.attempt_count == 1

    calls: list[str] = []
    restarted_worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=lambda db: BlockedService(calls),
        owner="restarted-worker",
        lease_seconds=60,
    )

    assert restarted_worker.run_once() == job_id
    assert calls == ["Generate one safe local research strategy."]
    with factory() as db:
        job = ResearchJobRepository(db).get(job_id)
        assert job is not None
        assert job.status == "BLOCKED"
        assert job.attempt_count == 1
        assert job.provider_attempted_at is None
        assert job.provider_completed_at is None


def test_cancel_before_provider_closes_job_and_attempt_without_creating_chain(
    tmp_path,
) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        repository = ResearchJobRepository(db)
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=True),
            idempotency_key="cancel-before-provider",
        ).id
        claimed = repository.claim_next(
            owner="cancel-before-provider-worker",
            lease_seconds=60,
        )
        assert claimed is not None and claimed.lease_token
        lease_token = claimed.lease_token
        repository.cancel(job_id, "Cancelled before provider call.")

    worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=lambda db: FailingService(),
        owner="cancel-before-provider-worker",
        lease_seconds=60,
    )
    worker._execute(job_id, lease_token, FakeHeartbeat())

    with factory() as db:
        job = ResearchJobRepository(db).get(job_id)
        attempt = db.query(ResearchJobAttempt).one()
        assert job is not None
        assert job.status == "CANCELLED"
        assert attempt.status == "CANCELLED"
        assert job.provider_attempted_at is None
        assert db.query(FullChainRun).count() == 0


def test_cancel_after_provider_closes_job_attempt_chain_and_checkpoint(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=True),
            idempotency_key="cancel-after-provider",
        ).id

    worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=lambda db: CancelingService(db, job_id),
        owner="cancel-after-provider-worker",
        lease_seconds=60,
    )
    assert worker.run_once() == job_id

    with factory() as db:
        job = ResearchJobRepository(db).get(job_id)
        attempt = db.query(ResearchJobAttempt).one()
        chain = db.query(FullChainRun).one()
        checkpoint = db.query(FullChainStageRun).one()
        assert job is not None
        assert job.status == "CANCELLED"
        assert attempt.status == "CANCELLED"
        assert chain.status == "CANCELLED"
        assert checkpoint.status == "CANCELLED"
        assert job.provider_attempted_at is not None
        assert job.provider_completed_at is not None


def test_success_response_with_missing_links_blocks_all_durable_states(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=False),
            idempotency_key="missing-success-links",
        ).id

    worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=lambda db: MissingLinksService(),
        owner="missing-links-worker",
        lease_seconds=60,
    )
    assert worker.run_once() == job_id

    with factory() as db:
        job = ResearchJobRepository(db).get(job_id)
        attempt = db.query(ResearchJobAttempt).one()
        chain = db.query(FullChainRun).one()
        checkpoint = db.query(FullChainStageRun).one()
        assert job is not None
        assert job.status == "BLOCKED"
        assert attempt.status == "BLOCKED"
        assert chain.status == "BLOCKED"
        assert checkpoint.status == "BLOCKED"
        assert "missing persisted strategy_id" in (job.error_message or "")


def test_terminal_chain_status_cannot_be_rewritten_by_job_evidence(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        repository = ResearchJobRepository(db)
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=False),
            idempotency_key="terminal-chain-conflict",
        ).id
        claimed = repository.claim_next(
            owner="terminal-conflict-worker",
            lease_seconds=60,
        )
        assert claimed is not None and claimed.lease_token
        orchestrator = ResearchFullChainOrchestrator(db)
        chain = orchestrator.begin(job_id, claimed.lease_token)
        chain.status = "BLOCKED"
        chain.terminal_reason = "prior immutable terminal status"
        chain.completed_at = datetime.now(timezone.utc)
        db.commit()

        with pytest.raises(
            ResearchFullChainBlocked,
            match="terminal full-chain status cannot be rewritten",
        ):
            orchestrator.terminalize_owned(
                job_id,
                claimed.lease_token,
                status="FAILED",
                stage="FAILED",
                reason="conflicting terminal evidence",
                provider_completed=False,
            )

        unchanged = repository.get(job_id)
        assert unchanged is not None
        assert unchanged.status == "RUNNING"
        assert unchanged.evidence_snapshot.get("full_chain_status") is None
        assert db.get(FullChainRun, chain.id).status == "BLOCKED"


def test_invalid_stale_job_is_quarantined_and_does_not_stop_next_job(tmp_path) -> None:
    factory = session_factory(tmp_path)
    fixed_now = datetime.now(timezone.utc)
    with factory() as db:
        repository = ResearchJobRepository(db)
        malformed_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=True),
            idempotency_key="malformed-stale-job",
        ).id
        claimed = repository.claim_next(
            owner="malformed-worker",
            lease_seconds=10,
            now=fixed_now,
        )
        assert claimed is not None and claimed.lease_token
        ResearchFullChainOrchestrator(db).begin(
            malformed_id,
            claimed.lease_token,
        )
        assert repository.mark_provider_attempt(
            malformed_id,
            claimed.lease_token,
            now=fixed_now,
        )
        malformed = repository.get(malformed_id)
        assert malformed is not None
        malformed.provider_completed_at = fixed_now + timedelta(seconds=1)
        db.commit()
        assert repository.expire_stale(
            fixed_now + timedelta(seconds=10),
        ) is not None
        next_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=False),
            idempotency_key="job-after-malformed-stale",
        ).id

    calls: list[str] = []
    worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=lambda db: BlockedService(calls),
        owner="healthy-worker",
        lease_seconds=60,
    )
    assert worker.run_once() == next_id
    assert calls == ["Generate one safe local research strategy."]

    with factory() as db:
        malformed = ResearchJobRepository(db).get(malformed_id)
        assert malformed is not None
        assert malformed.status == "BLOCKED"
        assert malformed.stage == "RECOVERY_BLOCKED"
        assert malformed.evidence_snapshot["recovery_allowed"] is False
        assert db.query(ResearchJobAttempt).filter_by(
            research_job_id=malformed_id
        ).one().status == "BLOCKED"
        malformed_chain = db.query(FullChainRun).filter_by(
            research_job_id=malformed_id
        ).one()
        assert malformed_chain.status == "STALE"
        assert db.query(FullChainStageRun).filter_by(
            full_chain_run_id=malformed_chain.id
        ).one().status == "STALE"


def test_worker_exception_is_stale_and_closes_chain_without_claiming_provider_completion(
    tmp_path,
) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=True),
            idempotency_key="worker-failed-job",
        ).id

    worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=lambda db: FailingService(),
        owner="failing-worker",
        lease_seconds=60,
        heartbeat_interval_seconds=10,
    )
    assert worker.run_once() == job_id

    with factory() as db:
        job = ResearchJobRepository(db).get(job_id)
        assert job is not None
        assert job.status == "STALE"
        assert job.stage == "PROVIDER_OUTCOME_UNKNOWN"
        assert job.provider_attempted_at is not None
        assert job.provider_completed_at is None
        assert job.error_message == "provider token=[REDACTED] failed without safe response"
        assert "synthetic-sensitive-value" not in str(job.evidence_snapshot)
        chain = db.query(FullChainRun).one()
        checkpoint = db.query(FullChainStageRun).one()
        attempt = db.query(ResearchJobAttempt).one()
        assert chain.status == "STALE"
        assert checkpoint.status == "STALE"
        assert attempt.status == "STALE"
        assert chain.completed_at is not None
        assert checkpoint.completed_at is not None
        assert attempt.completed_at is not None


@pytest.mark.parametrize("formal_mode", [False, True])
def test_worker_runs_controlled_service_chain_and_reconciles_all_database_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    formal_mode: bool,
) -> None:
    monkeypatch.delenv("FREQTRADE_AI_CI_OFFLINE", raising=False)
    factory = session_factory(tmp_path)
    datadir = write_market_data(tmp_path)
    install_fake_freqtrade(tmp_path, monkeypatch)
    monkeypatch.setenv("TEST_LLM_API_KEY", "synthetic-worker-test-value")
    settings = Settings(
        execution_target_manifest=okx_demo_execution_target_manifest(),
        freqtrade_user_data=tmp_path / "user_data",
        strategy_output_dir=tmp_path / "strategies",
        market_data_dir=datadir,
        backtest_result_dir=tmp_path / "reports" / "backtests",
        log_dir=tmp_path / "logs",
        tmp_freqtrade_config_dir=tmp_path / "freqtrade-configs",
    )
    settings.strategy_output_dir.mkdir(parents=True)
    monkeypatch.setattr("app.services.deepseek_backtest_loop.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.local_backtest_trigger.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.backtest_artifact_ingest.get_settings", lambda: settings)
    monkeypatch.setattr("app.adapters.freqtrade.config_builder.get_settings", lambda: settings)

    observed_args: list[str] = []

    def fake_executor(args, cwd, timeout_seconds):
        observed_args.extend(args)
        config_path = Path(args[args.index("--config") + 1])
        timerange = json.loads(config_path.read_text(encoding="utf-8"))[
            "timerange"
        ]
        result_dir = Path(args[args.index("--backtest-directory") + 1])
        result_dir.mkdir(parents=True, exist_ok=True)
        zip_path = result_dir / "backtest-result-2026-07-22_12-00-00.zip"
        trades = []
        for index in range(90):
            open_rate, close_rate = {
                0: (100.0, 101.0),
                1: (100.0, 99.0),
                2: (100.0, 100.1),
            }[index % 3]
            opened = 1_704_067_200_000 + index * 300_000
            trades.append(
                {
                    "open_timestamp": opened,
                    "close_timestamp": opened + 240_000,
                    "open_rate": open_rate,
                    "close_rate": close_rate,
                    "fee_open": 0.0005,
                    "fee_close": 0.0005,
                    "funding_fees": 0.0,
                    "profit_abs": 1.0,
                    "profit_ratio": 0.001,
                }
            )
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr(
                "backtest-result-2026-07-22_12-00-00.json",
                json.dumps(
                    {
                        "strategy": {
                            "WorkerDeepseekRsiStrategy": {
                                "profit_total_abs": 123.4,
                                "profit_total_pct": 12.5,
                                "max_drawdown_pct": 4.2,
                                "winrate": 61.0,
                                "total_trades": len(trades),
                                "timerange": timerange,
                                "starting_balance": 1000.0,
                                "trades": trades,
                            }
                        }
                    }
                ),
            )
            archive.writestr("backtest-result-2026-07-22_12-00-00_config.json", "{}")
        result_dir.joinpath(".last_result.json").write_text(
            json.dumps({"latest_backtest": zip_path.name}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="controlled backtesting complete",
            stderr="",
        )

    http_client = MockLLMClient()

    def service_factory(db):
        provider = OpenAICompatibleStrategyBlueprintProvider(
            LLMProviderConfig(
                provider_name="deepseek",
                model_name="deepseek-test-model",
                base_url="https://api.deepseek.com",
                api_key_env="TEST_LLM_API_KEY",
            ),
            http_client=http_client,
        )
        return DeepSeekBacktestLoopService(
            db,
            generation_service=StrategyGenerationService(
                db,
                provider=provider,
                file_manager=StrategyFileManager(
                    output_dir=settings.strategy_output_dir,
                    approved_roots=[settings.strategy_output_dir],
                ),
            ),
            backtest_runner=FreqtradeBacktestRunner(
                FreqtradeCliRunner(executor=fake_executor)
            ),
        )

    with factory() as db:
        request_payload = DeepSeekBacktestLoopRequest(
            prompt_summary="Generate one controlled strategy and run the local worker chain.",
            allow_real_call=not formal_mode,
            backtest_profile=local_profile(datadir),
            validation_windows=validation_windows(datadir),
            timeout_seconds=60,
            **(
                {
                    "persisted_blueprint": StrategyBlueprint.model_validate(
                        MockLLMResponse().json()["blueprints"][0]
                    ).model_dump(mode="json"),
                    "formal_provenance": {
                        "contract_version": "formal-candidate-validation-provenance-v1",
                        "execution_target_id": "OKX_DEMO",
                        "allow_real_funds": False,
                        "real_orders": False,
                        "provider_call_attempted": False,
                        "credential_values_recorded": False,
                    },
                }
                if formal_mode
                else {}
            ),
        )
        if formal_mode:
            blueprint = StrategyBlueprint.model_validate(
                request_payload.persisted_blueprint
            )
            source_path = tmp_path / "research/strategy_candidates/15m/01_worker.py"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(
                StrategyCodeRenderer().render(blueprint), encoding="utf-8"
            )
            digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
            equivalence = prove_blueprint_code_equivalence(
                blueprint_payload=blueprint.model_dump(mode="json"),
                source_bytes=source_path.read_bytes(),
                expected_source_digest=digest,
                expected_class_name=blueprint.class_name,
                expected_timeframe=blueprint.timeframe,
            )
            ownership = {
                "schema_version": "freqtrade-ai-formal-research-ownership-v1",
                "scope": "FORMAL_STRATEGY_RESEARCH",
                "canonical_root": str(tmp_path.resolve()),
                "owner_task_id": "formal-worker-integration",
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=20)
                ).isoformat(),
            }
            job_id = StrategyCandidateValidationQueueService(
                db, canonical_root=tmp_path
            ).enqueue_generated(
                run_id="formal-worker-full-chain",
                repository_commit="f" * 40,
                candidates=[
                    GeneratedCandidate(
                        candidate_key="formal-worker-btc-15m-01",
                        source_path=str(source_path.relative_to(tmp_path)),
                        source_code_digest=digest,
                        pair="BTC/USDT:USDT",
                        timeframe="15m",
                        blueprint_evidence={
                            "exact_render_match": True,
                            "source_code_digest": digest,
                            "rendered_code_digest": digest,
                            "blueprint_digest": equivalence.blueprint_digest,
                            "renderer_version": equivalence.renderer_version,
                            "blueprint": blueprint.model_dump(mode="json"),
                        },
                        validation_request=request_payload.model_dump(mode="json"),
                    )
                ],
                ownership_evidence=ownership,
            )[0].id
        else:
            job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
                request_payload,
                idempotency_key="worker-full-chain",
            ).id

    worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=service_factory,
        owner="controlled-integration-worker",
        lease_seconds=60,
        heartbeat_interval_seconds=10,
    )
    original_run_with_manifest = (
        FreqtradeBacktestRunner.run_backtest_with_artifact_manifest
    )
    crashed_validation = False

    def crash_after_first_validation_manifest(self, *args, **kwargs):
        nonlocal crashed_validation
        manifest = original_run_with_manifest(self, *args, **kwargs)
        if (
            str(kwargs.get("execution_id", "")).startswith("validation-run-")
            and not crashed_validation
        ):
            crashed_validation = True
            raise SystemExit("synthetic crash after validation manifest")
        return manifest

    monkeypatch.setattr(
        FreqtradeBacktestRunner,
        "run_backtest_with_artifact_manifest",
        crash_after_first_validation_manifest,
    )
    with pytest.raises(SystemExit, match="synthetic crash after validation manifest"):
        worker.run_once()
    monkeypatch.setattr(
        FreqtradeBacktestRunner,
        "run_backtest_with_artifact_manifest",
        original_run_with_manifest,
    )
    with factory() as db:
        repository = ResearchJobRepository(db)
        validation_crash = repository.get(job_id)
        assert validation_crash is not None
        assert validation_crash.status == "RUNNING"
        assert validation_crash.stage == "PERSISTED_RESULT"
        assert validation_crash.lease_expires_at is not None
        plan = db.query(StrategyValidationPlan).one()
        assert plan.status == "RUNNING"
        running = [
            window
            for window in db.query(StrategyValidationWindow).all()
            if window.backtest_task.status == "running"
        ]
        assert len(running) == 1
        assert repository.expire_stale(
            validation_crash.lease_expires_at.replace(tzinfo=timezone.utc)
            + timedelta(microseconds=1)
        ) is not None

    assert worker.run_once() == job_id
    with factory() as db:
        approval_ready = ResearchJobRepository(db).get(job_id)
        assert approval_ready is not None
        assert (approval_ready.status, approval_ready.stage) == (
            "PENDING",
            "CANDIDATE_APPROVED",
        ), (approval_ready.error_message, approval_ready.evidence_snapshot)
    original_complete = ResearchJobRepository.complete

    def crash_after_deployment_publish(self, *args, **kwargs):
        if kwargs.get("stage") == "DEPLOYED":
            raise SystemExit("synthetic crash after idempotent deployment publish")
        return original_complete(self, *args, **kwargs)

    monkeypatch.setattr(
        ResearchJobRepository,
        "complete",
        crash_after_deployment_publish,
    )
    with pytest.raises(SystemExit, match="synthetic crash"):
        worker.run_once()
    monkeypatch.setattr(
        ResearchJobRepository,
        "complete",
        original_complete,
    )
    with factory() as db:
        repository = ResearchJobRepository(db)
        crashed = repository.get(job_id)
        assert crashed is not None
        assert crashed.status == "RUNNING"
        assert crashed.stage == "SIGNAL"
        assert db.query(StrategyDeployment).count() == 1
        assert crashed.lease_expires_at is not None
        assert repository.expire_stale(
            crashed.lease_expires_at.replace(tzinfo=timezone.utc)
            + timedelta(microseconds=1)
        ) is not None
        assert db.query(FullChainRun).one().status == "STALE"

    assert worker.run_once() == job_id
    assert worker.run_once() is None
    assert len(http_client.requests) == (0 if formal_mode else 1)
    if not formal_mode:
        assert http_client.requests[0]["url"] == "https://api.deepseek.com/chat/completions"

    with factory() as db:
        job = ResearchJobRepository(db).get(job_id)
        assert job is not None
        assert job.status == "SUCCESS", (job.error_message, job.evidence_snapshot)
        assert job.stage == "DEPLOYED"
        assert (job.provider_attempted_at is not None) is (not formal_mode)
        assert job.provider_completed_at is not None
        assert job.evidence_snapshot["acceptance_ready"] is True
        assert all(
            getattr(job, field) is not None
            for field in (
                "strategy_generation_run_id",
                "strategy_id",
                "strategy_version_id",
                "backtest_run_id",
                "backtest_task_id",
                "backtest_result_id",
                "strategy_score_id",
            )
        )
        assert db.query(StrategyGenerationRun).count() == 1
        generation = db.query(StrategyGenerationRun).one()
        assert generation.provider == ("formal_research" if formal_mode else "deepseek")
        if formal_mode:
            assert generation.params_snapshot["provider_call_attempted"] is False
            assert generation.params_snapshot["credential_values_recorded"] is False
        assert db.query(Strategy).count() == 1
        assert db.query(StrategyVersion).count() == 1
        assert db.query(BacktestRun).count() == 5
        assert db.query(BacktestTask).count() == 5
        assert db.query(BacktestResult).count() == 5
        assert db.query(StrategyScore).count() == 1
        assert db.query(FullChainRun).count() == 1
        assert db.query(StrategyCandidateApproval).count() == 1
        assert db.query(StrategyDeployment).count() == 1
        plan = db.query(StrategyValidationPlan).one()
        assert plan.status == "PASSED"
        assert db.query(StrategyValidationWindow).count() == 4
        assert all(
            window.status == "PASSED"
            for window in db.query(StrategyValidationWindow).all()
        )
        primary_result = db.get(BacktestResult, job.backtest_result_id)
        assert primary_result is not None
        assert (
            primary_result.metrics_snapshot["promotion_evidence"][
                "validation_matrix"
            ]["plan_id"]
            == plan.id
        )
        assert all(
            run.status == "succeeded" for run in db.query(BacktestRun).all()
        )
        assert all(
            task.status == "succeeded" for task in db.query(BacktestTask).all()
        )
    assert observed_args.count("backtesting") == 5
    assert observed_args[observed_args.index("--datadir") + 1] == str(datadir / "okx")
