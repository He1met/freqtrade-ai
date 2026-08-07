import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.repositories.strategy_research import StrategyResearchRepository
from app.services.strategy_research import (
    StrategyResearchPersistenceService,
    StrategyResearchReportError,
)


REPORT = (
    Path(__file__).resolve().parents[2]
    / "reports"
    / "research"
    / "strategy-candidates-20260806.json"
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        yield session


def test_persists_all_rejected_candidates_without_formal_strategies(db):
    batch = StrategyResearchPersistenceService(db).persist_report(
        REPORT, run_id="20260806", repository_commit="a" * 40
    )

    assert batch.status == "VALIDATED"
    assert batch.generated_count == 10
    assert batch.persisted_count == 10
    assert batch.qualified_count == 0
    assert batch.rejected_count == 10
    assert len(batch.candidates) == 10
    assert {candidate.status for candidate in batch.candidates} == {"REJECTED"}
    assert all(candidate.rejection_reasons for candidate in batch.candidates)
    assert db.execute(text("SELECT count(*) FROM strategies")).scalar() == 0


def test_report_persistence_is_idempotent_by_run_and_digest(db):
    service = StrategyResearchPersistenceService(db)
    first = service.persist_report(REPORT, run_id="20260806", repository_commit="a" * 40)
    second = service.persist_report(REPORT, run_id="20260806", repository_commit="a" * 40)

    assert second.id == first.id
    assert len(StrategyResearchRepository(db).list_batches()) == 1
    assert len(StrategyResearchRepository(db).list_candidates()) == 10


def test_same_run_rejects_changed_report(db, tmp_path):
    service = StrategyResearchPersistenceService(db)
    service.persist_report(REPORT, run_id="20260806", repository_commit="a" * 40)
    changed = tmp_path / "changed.json"
    payload = json.loads(REPORT.read_text())
    payload["limitations"].append("changed")
    changed.write_text(json.dumps(payload))

    with pytest.raises(StrategyResearchReportError, match="different report digest"):
        service.persist_report(changed, run_id="20260806", repository_commit="a" * 40)


def test_claimed_qualification_cannot_bypass_hard_gates(db, tmp_path):
    payload = json.loads(REPORT.read_text())
    name = next(iter(payload["candidates"]))
    payload["qualified_candidates"] = [name]
    payload["candidates"][name]["deployable_candidate"] = True
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(payload))

    with pytest.raises(StrategyResearchReportError, match="claims qualification"):
        StrategyResearchPersistenceService(db).persist_report(
            forged, run_id="forged", repository_commit="a" * 40
        )


def test_rejects_non_demo_safe_report(db, tmp_path):
    payload = copy.deepcopy(json.loads(REPORT.read_text()))
    payload["safety"]["allow_real_funds"] = True
    unsafe = tmp_path / "unsafe.json"
    unsafe.write_text(json.dumps(payload))

    with pytest.raises(StrategyResearchReportError, match="not OKX_DEMO/offline safe"):
        StrategyResearchPersistenceService(db).persist_report(
            unsafe, run_id="unsafe", repository_commit="a" * 40
        )


def test_missing_independent_window_is_an_auditable_rejection(db, tmp_path):
    payload = json.loads(REPORT.read_text())
    name = next(iter(payload["candidates"]))
    del payload["candidates"][name]["windows"]["oos"]
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps(payload))

    batch = StrategyResearchPersistenceService(db).persist_report(
        incomplete, run_id="incomplete", repository_commit="a" * 40
    )
    candidate = next(item for item in batch.candidates if item.candidate_name == name)
    assert any(reason["code"] == "WINDOW_MISSING" for reason in candidate.rejection_reasons)


def test_failed_batch_preserves_stage_without_candidate_rejection_claims(db):
    batch = StrategyResearchPersistenceService(db).record_failed_batch(
        run_id="failed-run",
        repository_commit="a" * 40,
        stage="LOOKAHEAD",
        failure_reason="freqtrade failed token=should-not-leak",
    )

    assert batch.status == "FAILED"
    assert batch.generated_count == 0
    assert batch.persisted_count == 0
    assert batch.qualified_count == 0
    assert batch.rejected_count == 0
    assert batch.safety_snapshot["failed_stage"] == "LOOKAHEAD"
    assert "should-not-leak" not in batch.failure_reason
