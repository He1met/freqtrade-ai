from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.strategy_research_contract import official_research_policy
from app.models import (
    Base,
    MarketDataQualityReceipt,
    ResearchJob,
    StrategyDeployment,
    StrategyResearchAttemptEvent,
    StrategyResearchBatch,
    StrategyResearchCandidate,
)
from app.repositories.execution_lineage import ensure_execution_scope_catalog
from app.repositories.research_jobs import ResearchJobRepository
from app.services.qualified_demo_deployment_queue import (
    QUALIFIED_DEMO_DEPLOYMENT_OPERATION,
    QualifiedDemoDeploymentQueueBlocked,
    QualifiedDemoDeploymentQueueService,
)
from app.services.research_job_queue import DEEPSEEK_BACKTEST_OPERATION
from app.services.strategy_candidate_validation_queue import (
    CANDIDATE_VALIDATION_OPERATION,
    GeneratedCandidate,
    StrategyCandidateValidationQueueBlocked,
    StrategyCandidateValidationQueueService,
)


NOW = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        ensure_execution_scope_catalog(session)
        session.commit()
        yield session
    engine.dispose()


def _ownership(root: Path, *, expires_at: datetime | None = None) -> dict:
    return {
        "schema_version": "freqtrade-ai-formal-research-ownership-v1",
        "scope": "FORMAL_STRATEGY_RESEARCH",
        "canonical_root": str(root.resolve()),
        "owner_task_id": "task-qualified-demo-queue",
        "confirmed_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (expires_at or NOW + timedelta(minutes=20)).isoformat(),
    }


def _seed_terminal_batch(db: Session, *, qualified: bool) -> StrategyResearchBatch:
    quality = MarketDataQualityReceipt(
        contract_version="market-data-quality-v1",
        exchange="okx",
        pair="BTC/USDT:USDT",
        timeframe="5m",
        relative_path="futures/BTC_USDT_USDT-5m-futures.feather",
        file_format="feather",
        file_size=4096,
        file_sha256="a" * 64,
        inspected_at=NOW - timedelta(minutes=30),
        row_count=1000,
        first_open_at=NOW - timedelta(days=4),
        last_open_at=NOW - timedelta(minutes=5),
        expected_interval_seconds=300,
        missing_interval_count=0,
        duplicate_timestamp_count=0,
        out_of_order_count=0,
        misaligned_timestamp_count=0,
        null_ohlcv_count=0,
        invalid_ohlc_count=0,
        negative_volume_count=0,
        freshness_seconds=300,
        status="PASSED",
        reason_codes=[],
        evidence_digest="b" * 64,
    )
    batch = StrategyResearchBatch(
        run_id=f"formal-queue-{'qualified' if qualified else 'no-action'}",
        source_type="codex",
        repository_commit="c" * 40,
        report_schema_version="freqtrade-ai-strategy-candidate-research-v2",
        report_path="reports/research/formal-queue.json",
        report_digest=("d" if qualified else "e") * 64,
        status="VALIDATED",
        requested_count=60,
        generated_count=60,
        persisted_count=60,
        qualified_count=1 if qualified else 0,
        rejected_count=59 if qualified else 60,
        safety_snapshot={
            "execution_target": "OKX_DEMO",
            "allow_real_funds": False,
            "real_orders": False,
        },
        selection_policy=official_research_policy(),
        window_evidence=[],
        completed_at=NOW,
    )
    db.add_all([quality, batch])
    db.flush()
    pairs = ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT")
    timeframes = ("5m", "15m")
    for index in range(60):
        pair = pairs[index // 20]
        timeframe = timeframes[(index // 10) % 2]
        slot = index % 10 + 1
        is_qualified = qualified and index == 0
        digest = hashlib.sha256(f"candidate-{index}".encode()).hexdigest()
        evidence = {
            "deployment_target": {"pair": pair, "timeframe": timeframe},
        }
        if is_qualified:
            evidence["canonical_blueprint_v2"] = {
                "exact_render_match": True,
                "source_code_digest": digest,
                "rendered_code_digest": digest,
                "blueprint": {"schema_version": "2", "timeframe": timeframe},
            }
        db.add(
            StrategyResearchCandidate(
                batch_id=batch.id,
                candidate_name=f"Candidate{index}",
                source_path=f"research/strategy_candidates/candidate_{index}.py",
                code_digest=digest,
                pair=pair,
                timeframe=timeframe,
                unit_slot=slot,
                strategy_family="MEAN_REVERSION",
                regime_hypothesis="test",
                expected_holding_period="test",
                expected_trade_frequency="test",
                structure_fingerprint=digest,
                similarity_evidence={},
                correlation_evidence={},
                status="QUALIFIED" if is_qualified else "REJECTED",
                loadable=True,
                static_check="PASSED",
                lookahead_status="PASSED",
                score=75.0 if is_qualified else 20.0,
                validation_passed=is_qualified,
                deployable_candidate=is_qualified,
                rejection_reasons=[] if is_qualified else [{"code": "QUALITY_GATE"}],
                evidence_snapshot=evidence,
            )
        )
    db.flush()
    db.add(
        StrategyResearchAttemptEvent(
            attempt_id=f"00000000-0000-4000-8000-{'1' if qualified else '2':0>12}",
            sequence=2,
            run_id=batch.run_id,
            batch_id=batch.id,
            market_data_quality_receipt_id=quality.id,
            trigger="automation",
            phase="TERMINAL",
            outcome="COMPLETED",
            reason_code="COMPLETED",
            redacted_reason="Formal research completed.",
            requested_count=60,
            generated_count=60,
            validated_count=60,
            persisted_count=60,
            qualified_count=1 if qualified else 0,
            rejected_count=59 if qualified else 60,
            evidence_snapshot={
                "execution_target": "OKX_DEMO",
                "allow_real_funds": False,
                "real_orders": False,
                "market_data_bindings": [
                    {
                        "pair": quality.pair,
                        "timeframe": quality.timeframe,
                        "receipt_id": quality.id,
                        "sha256": quality.file_sha256,
                    }
                ],
            },
            event_digest=("f" if qualified else "0") * 64,
            created_at=NOW,
        )
    )
    db.commit()
    return batch


def test_not_qualified_is_a_successful_no_action_and_never_deploys(
    db: Session, tmp_path: Path
) -> None:
    batch = _seed_terminal_batch(db, qualified=False)
    result = QualifiedDemoDeploymentQueueService(
        db, canonical_root=tmp_path
    ).enqueue_completed_batch(
        run_id=batch.run_id,
        ownership_evidence=_ownership(tmp_path),
        now=NOW,
    )

    assert result.status == "NO_ACTION"
    assert result.reason_code == "NOT_QUEUED_NO_QUALIFIED"
    assert result.queued_job_ids == ()
    assert db.scalar(select(func.count()).select_from(ResearchJob)) == 0
    assert db.scalar(select(func.count()).select_from(StrategyDeployment)) == 0


def test_qualified_candidate_enqueues_exactly_once_with_demo_only_contract(
    db: Session, tmp_path: Path
) -> None:
    batch = _seed_terminal_batch(db, qualified=True)
    service = QualifiedDemoDeploymentQueueService(db, canonical_root=tmp_path)

    first = service.enqueue_completed_batch(
        run_id=batch.run_id, ownership_evidence=_ownership(tmp_path), now=NOW
    )
    replay = service.enqueue_completed_batch(
        run_id=batch.run_id, ownership_evidence=_ownership(tmp_path), now=NOW
    )

    assert first.status == replay.status == "QUEUED"
    assert first.queued_job_ids == replay.queued_job_ids
    assert db.scalar(select(func.count()).select_from(ResearchJob)) == 1
    job = db.scalar(
        select(ResearchJob).where(
            ResearchJob.operation == QUALIFIED_DEMO_DEPLOYMENT_OPERATION
        )
    )
    assert job is not None
    assert job.status == "PENDING"
    assert job.stage == "QUALIFIED_PENDING_CANONICAL_VALIDATION"
    assert job.request_payload["execution_target_id"] == "OKX_DEMO"
    assert job.request_payload["allow_real_funds"] is False
    assert job.request_payload["real_orders"] is False
    assert job.request_payload["signal_source"] == "NATURAL_CLOSED_CANDLE_ONLY"
    assert job.request_payload["no_action_is_terminal_success"] is True
    assert db.scalar(select(func.count()).select_from(StrategyDeployment)) == 0
    assert (
        ResearchJobRepository(db).claim_next(
            owner="deepseek-worker",
            lease_seconds=60,
            now=NOW,
            operations={DEEPSEEK_BACKTEST_OPERATION},
        )
        is None
    )
    db.refresh(job)
    assert job.status == "PENDING"


def test_missing_or_stale_ownership_and_evidence_fail_closed(
    db: Session, tmp_path: Path
) -> None:
    batch = _seed_terminal_batch(db, qualified=True)
    service = QualifiedDemoDeploymentQueueService(db, canonical_root=tmp_path)
    with pytest.raises(
        QualifiedDemoDeploymentQueueBlocked,
        match="OWNERSHIP_EVIDENCE_MISSING_OR_STALE",
    ):
        service.enqueue_completed_batch(
            run_id=batch.run_id, ownership_evidence=None, now=NOW
        )
    with pytest.raises(
        QualifiedDemoDeploymentQueueBlocked,
        match="OWNERSHIP_EVIDENCE_MISSING_OR_STALE",
    ):
        service.enqueue_completed_batch(
            run_id=batch.run_id,
            ownership_evidence=_ownership(
                tmp_path, expires_at=NOW - timedelta(seconds=1)
            ),
            now=NOW,
        )
    terminal = db.scalar(
        select(StrategyResearchAttemptEvent).where(
            StrategyResearchAttemptEvent.batch_id == batch.id
        )
    )
    terminal.created_at = NOW - timedelta(minutes=11)
    db.commit()
    with pytest.raises(
        QualifiedDemoDeploymentQueueBlocked,
        match="RESEARCH_EVIDENCE_MISSING_OR_STALE",
    ):
        service.enqueue_completed_batch(
            run_id=batch.run_id,
            ownership_evidence=_ownership(tmp_path),
            now=NOW,
        )
    assert db.scalar(select(func.count()).select_from(ResearchJob)) == 0


def test_generated_candidate_is_persisted_before_validation_and_claimed_by_lease(
    db: Session, tmp_path: Path
) -> None:
    source = tmp_path / "research/strategy_candidates/5m/01_candidate.py"
    source.parent.mkdir(parents=True)
    source.write_text("class Candidate: pass\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    candidate = GeneratedCandidate(
        candidate_key="BTC-USDT-5m-01",
        source_path=str(source.relative_to(tmp_path)),
        source_code_digest=digest,
        pair="BTC/USDT:USDT",
        timeframe="5m",
        blueprint_evidence={
            "exact_render_match": True,
            "source_code_digest": digest,
            "rendered_code_digest": digest,
            "blueprint": {"schema_version": "2", "timeframe": "5m"},
        },
    )
    service = StrategyCandidateValidationQueueService(db, canonical_root=tmp_path)
    first = service.enqueue_generated(
        run_id="202608110800",
        repository_commit="a" * 40,
        candidates=[candidate],
        ownership_evidence=_ownership(tmp_path),
        now=NOW,
    )
    replay = service.enqueue_generated(
        run_id="202608110800",
        repository_commit="a" * 40,
        candidates=[candidate],
        ownership_evidence=_ownership(tmp_path),
        now=NOW,
    )
    assert first[0].id == replay[0].id
    assert first[0].stage == "GENERATED_QUEUED"
    assert first[0].evidence_snapshot["backtest_started"] is False
    assert first[0].request_payload["quality_contract"][
        "max_drawdown_per_validation_window"
    ] == 0.15
    assert db.scalar(select(func.count()).select_from(ResearchJob)) == 1

    claimed = service.claim_next(
        owner="formal-validation-worker", lease_seconds=60, now=NOW
    )
    assert claimed is not None
    assert claimed.operation == CANDIDATE_VALIDATION_OPERATION
    assert claimed.status == "RUNNING"
    assert claimed.lease_token


def test_generated_candidate_queue_rejects_missing_ownership(
    db: Session, tmp_path: Path
) -> None:
    service = StrategyCandidateValidationQueueService(db, canonical_root=tmp_path)
    with pytest.raises(
        StrategyCandidateValidationQueueBlocked,
        match="OWNERSHIP_EVIDENCE_MISSING_OR_STALE",
    ):
        service.enqueue_generated(
            run_id="202608110800",
            repository_commit="a" * 40,
            candidates=[],
            ownership_evidence=None,
            now=NOW,
        )
