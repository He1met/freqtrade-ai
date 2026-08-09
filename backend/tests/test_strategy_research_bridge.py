from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.core.strategy_research_contract import official_research_policy
from app.models import (
    Base,
    ExecutionScope,
    FullChainRun,
    FullChainStageRun,
    MarketDataQualityReceipt,
    ResearchJob,
    Strategy,
    StrategyGenerationRun,
    StrategyResearchAttemptEvent,
    StrategyResearchBatch,
    StrategyResearchCandidate,
    StrategyResearchCandidateBridgeEvent,
    StrategyVersion,
)
from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_file_validation import StrategyFileValidationService
from app.services.formal_read_models import StrategyResearchWorkspaceService
from app.services.strategy_renderer import StrategyCodeRenderer
from app.services.strategy_research_bridge import StrategyResearchBridgeService


NOW = datetime(2026, 8, 9, 8, 30, tzinfo=timezone.utc)


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                ExecutionScope(
                    scope_id="LOCAL_DRY_RUN",
                    scope_kind="NON_EXCHANGE",
                    exchange_capable=False,
                    executable=True,
                    exchange_writes=False,
                    order_submission_authorized=False,
                ),
                ExecutionScope(
                    scope_id="OKX_DEMO",
                    scope_kind="EXCHANGE_TARGET",
                    exchange_capable=True,
                    executable=False,
                    exchange_writes=False,
                    order_submission_authorized=False,
                ),
            ]
        )
        session.commit()
        yield session


def _blueprint_payload() -> dict:
    return {
        "schema_version": "2",
        "name": "Qualified Blueprint Candidate",
        "slug": "qualified-blueprint-candidate",
        "class_name": "QualifiedBlueprintCandidate",
        "description": "Exact deterministic research candidate.",
        "timeframe": "15m",
        "stoploss": -0.1,
        "minimal_roi": {"0": 0.03},
        "indicators": [{"name": "rsi", "kind": "rsi", "period": 14}],
        "entry_rules": [{"indicator": "rsi", "operator": "<", "value": 35.0}],
        "exit_rules": [{"indicator": "rsi", "operator": ">", "value": 70.0}],
        "can_short": False,
        "short_entry_rules": [],
        "short_exit_rules": [],
        "regime_rules": [],
        "tags": ["formal-research"],
    }


def _persist_qualified_candidate(
    db: Session,
    project_root: Path,
    *,
    blueprint_payload: dict,
) -> StrategyResearchCandidate:
    rendered = StrategyCodeRenderer().render(
        StrategyBlueprint.model_validate(blueprint_payload)
    )
    relative_source = Path("research_candidates") / "qualified_blueprint_candidate.py"
    source_path = project_root / relative_source
    source_path.parent.mkdir(parents=True)
    source_path.write_text(rendered, encoding="utf-8")
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()

    quality = MarketDataQualityReceipt(
        contract_version="market-data-quality-v1",
        exchange="okx",
        pair="BTC/USDT:USDT",
        timeframe="15m",
        relative_path="futures/BTC_USDT_USDT-15m-futures.feather",
        file_format="feather",
        file_size=4096,
        file_sha256="1" * 64,
        inspected_at=NOW,
        row_count=1000,
        first_open_at=NOW - timedelta(minutes=15 * 999),
        last_open_at=NOW,
        expected_interval_seconds=900,
        missing_interval_count=0,
        duplicate_timestamp_count=0,
        out_of_order_count=0,
        misaligned_timestamp_count=0,
        null_ohlcv_count=0,
        invalid_ohlc_count=0,
        negative_volume_count=0,
        freshness_seconds=0,
        status="PASSED",
        reason_codes=[],
        evidence_digest="2" * 64,
    )
    batch = StrategyResearchBatch(
        run_id="historical-qualified-run",
        source_type="codex",
        repository_commit="a" * 40,
        report_schema_version="formal-strategy-research-v1",
        report_path="reports/research/historical-qualified-run.json",
        report_digest="3" * 64,
        status="VALIDATED",
        requested_count=1,
        generated_count=1,
        persisted_count=1,
        qualified_count=1,
        rejected_count=0,
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
    candidate = StrategyResearchCandidate(
        batch_id=batch.id,
        candidate_name=blueprint_payload["class_name"],
        source_path=str(relative_source),
        code_digest=source_digest,
        status="QUALIFIED",
        loadable=True,
        static_check="PASSED",
        lookahead_status="PASSED",
        score=75.0,
        validation_passed=True,
        deployable_candidate=True,
        rejection_reasons=[],
        evidence_snapshot={"independent_oos": True, "lookahead": "PASSED"},
    )
    db.add(candidate)
    db.flush()
    db.add(
        StrategyResearchAttemptEvent(
            attempt_id="00000000-0000-4000-8000-000000000099",
            sequence=1,
            run_id=batch.run_id,
            batch_id=batch.id,
            market_data_quality_receipt_id=quality.id,
            trigger="manual",
            phase="TERMINAL",
            outcome="COMPLETED",
            reason_code="RESEARCH_COMPLETED",
            redacted_reason="Formal research completed.",
            requested_count=1,
            generated_count=1,
            validated_count=1,
            persisted_count=1,
            qualified_count=1,
            rejected_count=0,
            evidence_snapshot={"execution_target": "OKX_DEMO"},
            event_digest="4" * 64,
        )
    )
    db.commit()
    return candidate


def _bridge_service(db: Session, project_root: Path) -> StrategyResearchBridgeService:
    output_dir = project_root / "generated"
    output_dir.mkdir()
    validator = StrategyFileValidationService(
        file_manager=StrategyFileManager(
            output_dir=output_dir,
            approved_roots=[output_dir],
        )
    )
    return StrategyResearchBridgeService(
        db,
        project_root=project_root,
        file_validation=validator,
    )


def _count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def _assert_no_execution_side_effects(db: Session) -> None:
    for table_name in (
        "strategy_candidate_approvals",
        "strategy_deployments",
        "signal_evaluations",
        "trade_intents",
        "approved_executions",
        "exchange_orders",
        "exchange_fills",
    ):
        table = Base.metadata.tables[table_name]
        assert db.scalar(select(func.count()).select_from(table)) == 0


def test_historical_qualified_candidate_without_blueprint_requires_revalidation(
    db: Session,
    tmp_path: Path,
) -> None:
    payload = _blueprint_payload()
    candidate = _persist_qualified_candidate(db, tmp_path, blueprint_payload=payload)

    event_row = _bridge_service(db, tmp_path).bridge(
        candidate.id,
        blueprint_payload=None,
        requested_by="test-reviewer",
        now=NOW,
    )

    assert event_row.outcome == "REVALIDATION_REQUIRED"
    assert event_row.reason_code == "CANONICAL_BLUEPRINT_V2_MISSING"
    assert event_row.canonical_research_job_id is None
    assert event_row.canonical_full_chain_run_id is None
    assert event_row.strategy_id is None
    assert event_row.strategy_version_id is None
    assert event_row.execution_scope_id == "LOCAL_DRY_RUN"
    assert event_row.execution_target_id == "OKX_DEMO"
    assert event_row.allow_real_funds is False
    assert event_row.real_orders is False
    assert _count(db, Strategy) == 0
    assert _count(db, StrategyVersion) == 0
    assert _count(db, StrategyGenerationRun) == 0
    assert _count(db, ResearchJob) == 0
    assert _count(db, FullChainRun) == 0
    _assert_no_execution_side_effects(db)


def test_exact_blueprint_bridges_once_and_stops_at_canonical_validation(
    db: Session,
    tmp_path: Path,
) -> None:
    payload = _blueprint_payload()
    candidate = _persist_qualified_candidate(db, tmp_path, blueprint_payload=payload)
    service = _bridge_service(db, tmp_path)

    first = service.bridge(
        candidate.id,
        blueprint_payload=payload,
        requested_by="test-reviewer",
        now=NOW,
    )
    first_ids = {
        "event": first.id,
        "job": first.canonical_research_job_id,
        "attempt": first.canonical_research_job_attempt_id,
        "chain": first.canonical_full_chain_run_id,
        "generation": first.strategy_generation_run_id,
        "strategy": first.strategy_id,
        "version": first.strategy_version_id,
    }

    strategy = db.get(Strategy, first.strategy_id)
    version = db.get(StrategyVersion, first.strategy_version_id)
    job = db.get(ResearchJob, first.canonical_research_job_id)
    chain = db.get(FullChainRun, first.canonical_full_chain_run_id)
    stages = db.scalars(
        select(FullChainStageRun)
        .where(FullChainStageRun.full_chain_run_id == chain.id)
        .order_by(FullChainStageRun.id)
    ).all()

    assert first.outcome == "BRIDGED"
    assert first.reason_code == "CANONICAL_VALIDATION_REQUIRED"
    assert first.rendered_code_digest == candidate.code_digest
    assert first.allow_real_funds is False
    assert first.real_orders is False
    assert strategy.status == "draft"
    assert strategy.current_version_id == version.id
    assert version.validation_status == "pending"
    assert version.code_hash == candidate.code_digest
    assert job.status == "BLOCKED"
    assert job.stage == "CANONICAL_VALIDATION_REQUIRED"
    assert job.request_payload["allow_real_funds"] is False
    assert job.request_payload["real_orders"] is False
    assert chain.status == "BLOCKED"
    assert chain.current_stage == "BACKTEST"
    assert chain.terminal_reason == "CANONICAL_VALIDATION_REQUIRED"
    assert [(stage.stage, stage.status) for stage in stages] == [
        ("GENERATION", "SUCCESS"),
        ("BACKTEST", "BLOCKED"),
    ]
    _assert_no_execution_side_effects(db)

    workspace = StrategyResearchWorkspaceService(db).build(attempt_limit=10)
    assert workspace.lifecycle_summary.status == "BRIDGED_PENDING_CANONICAL_VALIDATION"
    assert workspace.lifecycle_summary.pending_canonical_validation_count == 1
    assert workspace.candidate_lifecycles[0].lifecycle_status == (
        "BRIDGED_PENDING_CANONICAL_VALIDATION"
    )
    assert workspace.candidate_lifecycles[0].candidate_approval_id is None
    assert workspace.candidate_lifecycles[0].deployment_id is None

    replay = service.bridge(
        candidate.id,
        blueprint_payload=payload,
        requested_by="test-reviewer",
        now=NOW + timedelta(minutes=5),
    )
    replay_ids = {
        "event": replay.id,
        "job": replay.canonical_research_job_id,
        "attempt": replay.canonical_research_job_attempt_id,
        "chain": replay.canonical_full_chain_run_id,
        "generation": replay.strategy_generation_run_id,
        "strategy": replay.strategy_id,
        "version": replay.strategy_version_id,
    }

    assert replay_ids == first_ids
    assert _count(db, StrategyResearchCandidateBridgeEvent) == 1
    assert _count(db, StrategyGenerationRun) == 1
    assert _count(db, Strategy) == 1
    assert _count(db, StrategyVersion) == 1
    assert _count(db, ResearchJob) == 1
    assert _count(db, FullChainRun) == 1
    assert _count(db, FullChainStageRun) == 2
    _assert_no_execution_side_effects(db)


@pytest.mark.parametrize(
    ("failed_section", "reason_code"),
    [
        ("bridge", "CANDIDATE_BRIDGE_EVENTS_UNAVAILABLE"),
        ("approval", "CANDIDATE_APPROVALS_UNAVAILABLE"),
        ("deployment", "CANDIDATE_DEPLOYMENTS_UNAVAILABLE"),
    ],
)
def test_workspace_marks_only_failed_lifecycle_query_unknown(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_section: str,
    reason_code: str,
) -> None:
    payload = _blueprint_payload()
    candidate = _persist_qualified_candidate(db, tmp_path, blueprint_payload=payload)
    bridged = _bridge_service(db, tmp_path).bridge(
        candidate.id,
        blueprint_payload=payload,
        requested_by="test-reviewer",
        now=NOW,
    )
    original_scalars = db.scalars

    if failed_section == "bridge":
        monkeypatch.setattr(
            "app.repositories.strategy_research.StrategyResearchRepository."
            "list_latest_candidate_bridge_events",
            lambda _repository, *, candidate_ids: (_ for _ in ()).throw(
                SQLAlchemyError("bridge receipts unavailable")
            ),
        )
    else:
        def _scalars(statement, *args, **kwargs):
            sql = str(statement)
            if "strategy_candidate_approvals" in sql:
                if failed_section == "approval":
                    raise SQLAlchemyError("approval receipts unavailable")
                return SimpleNamespace(
                    all=lambda: [
                        SimpleNamespace(
                            id=987,
                            full_chain_run_id=bridged.canonical_full_chain_run_id,
                            status="APPROVED",
                        )
                    ]
                )
            if "strategy_deployments" in sql and failed_section == "deployment":
                raise SQLAlchemyError("deployment receipts unavailable")
            return original_scalars(statement, *args, **kwargs)

        monkeypatch.setattr(db, "scalars", _scalars)

    workspace = StrategyResearchWorkspaceService(db).build(attempt_limit=10)

    assert workspace.evidence_status == "PARTIAL"
    assert getattr(workspace.sections, failed_section).status == "UNKNOWN"
    assert getattr(workspace.sections, failed_section).reason_code == reason_code
    for preserved in {"attempts", "quality", "batch", "bridge", "approval", "deployment"} - {
        failed_section
    }:
        assert getattr(workspace.sections, preserved).status == "AVAILABLE"
    assert workspace.candidate_lifecycles[0].lifecycle_status == "UNKNOWN"
    assert workspace.candidate_lifecycles[0].reason_code == reason_code
    assert workspace.execution_target_id == "OKX_DEMO"
    assert workspace.allow_real_funds is False
    assert workspace.real_orders is False
