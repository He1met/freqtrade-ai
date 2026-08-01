import json
import os
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    Strategy,
    StrategyCandidateApproval,
    StrategyDeployment,
    StrategyGenerationRun,
    StrategyScore,
    StrategyVersion,
)
from app.repositories import ResearchJobRepository
from app.schemas import DeepSeekBacktestLoopRequest, operation_error_evidence
from app.schemas.deepseek_backtest_loop import DeepSeekBacktestLoopResponse
from app.services.deepseek_backtest_loop import DeepSeekBacktestLoopService
from app.services.research_job_queue import ResearchJobQueueService
from app.services.strategy_generation import (
    LLMProviderConfig,
    OpenAICompatibleStrategyBlueprintProvider,
    StrategyGenerationService,
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
    exchange_dir.joinpath("BTC_USDT_USDT-15m-futures.feather").write_bytes(b"local candles")
    return datadir


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
    fixed_now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    with factory() as db:
        repository = ResearchJobRepository(db)
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            request(allow_real_call=True),
            idempotency_key="provider-crash-window",
        ).id
        claimed = repository.claim_next(owner="crashed-worker", lease_seconds=10, now=fixed_now)
        assert claimed is not None and claimed.lease_token
        assert repository.mark_provider_attempt(job_id, claimed.lease_token, now=fixed_now)

    with factory() as restarted_db:
        stale = ResearchJobRepository(restarted_db).expire_stale(
            fixed_now + timedelta(seconds=10)
        )
        assert stale is not None
        assert stale.status == "STALE"
        assert "automatic retry is forbidden" in (stale.error_message or "")

    calls: list[str] = []
    restarted_worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=lambda db: BlockedService(calls),
        owner="restarted-worker",
        lease_seconds=60,
    )
    assert restarted_worker.run_once() is None
    assert calls == []


def test_worker_exception_is_failed_redacted_and_does_not_claim_provider_completion(tmp_path) -> None:
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
        assert job.status == "FAILED"
        assert job.provider_attempted_at is not None
        assert job.provider_completed_at is None
        assert job.error_message == "provider token=[REDACTED] failed without safe response"
        assert "synthetic-sensitive-value" not in str(job.evidence_snapshot)


def test_worker_runs_controlled_service_chain_and_reconciles_all_database_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                                "timerange": "20240101-20240201",
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
        job_id = ResearchJobQueueService(db).enqueue_deepseek_backtest(
            DeepSeekBacktestLoopRequest(
                prompt_summary="Generate one controlled strategy and run the local worker chain.",
                allow_real_call=True,
                backtest_profile=local_profile(datadir),
                timeout_seconds=60,
            ),
            idempotency_key="worker-full-chain",
        ).id

    worker = DeepSeekBacktestWorker(
        session_factory=factory,
        service_factory=service_factory,
        owner="controlled-integration-worker",
        lease_seconds=60,
        heartbeat_interval_seconds=10,
    )
    assert worker.run_once() == job_id
    assert worker.run_once() == job_id
    assert worker.run_once() is None
    assert len(http_client.requests) == 1
    assert http_client.requests[0]["url"] == "https://api.deepseek.com/chat/completions"

    with factory() as db:
        job = ResearchJobRepository(db).get(job_id)
        assert job is not None
        assert job.status == "SUCCESS", (job.error_message, job.evidence_snapshot)
        assert job.stage == "DEPLOYED"
        assert job.provider_attempted_at is not None
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
        assert db.query(Strategy).count() == 1
        assert db.query(StrategyVersion).count() == 1
        assert db.query(BacktestRun).count() == 1
        assert db.query(BacktestTask).count() == 1
        assert db.query(BacktestResult).count() == 1
        assert db.query(StrategyScore).count() == 1
        assert db.query(FullChainRun).count() == 1
        assert db.query(StrategyCandidateApproval).count() == 1
        assert db.query(StrategyDeployment).count() == 1
        assert (
            db.query(BacktestResult)
            .one()
            .metrics_snapshot["promotion_evidence"]["source"]
            == "persisted_freqtrade_backtest_trades"
        )
        assert db.query(BacktestRun).one().status == "succeeded"
        assert db.query(BacktestTask).one().status == "succeeded"
    assert observed_args[observed_args.index("--datadir") + 1] == str(datadir / "okx")
