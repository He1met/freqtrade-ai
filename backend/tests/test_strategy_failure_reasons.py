import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import StrategyFailureReasonRepository, StrategyRepository
from app.schemas import StrategyCreate, StrategyFailureReasonCreate, StrategyVersionCreate
from app.services.strategy_failure_reasons import StrategyFailureReasonService


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


def create_strategy_version(db_session: Session, slug: str = "failed-strategy") -> tuple[int, int]:
    repository = StrategyRepository(db_session)
    strategy = repository.create(
        StrategyCreate(
            name=slug.replace("-", " ").title(),
            slug=slug,
        )
    )
    version = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": slug.title().replace("-", "")},
            generated_code=f"class {slug.title().replace('-', '')}: pass",
            file_path=f"user_data/strategies/generated/{slug}.py",
            validation_status="failed",
            validation_errors=[{"message": "missing stoploss"}],
        )
    )
    assert version is not None
    return strategy.id, version.id


def test_records_structured_failure_reason(db_session: Session) -> None:
    strategy_id, version_id = create_strategy_version(db_session)
    repository = StrategyFailureReasonRepository(db_session)

    reason = repository.record(
        StrategyFailureReasonCreate(
            strategy_id=strategy_id,
            strategy_version_id=version_id,
            stage="validation",
            reason_type="validation_error",
            message="Blueprint validation failed.",
            details={"field": "risk.stoploss", "code": "missing_required"},
        )
    )

    assert reason is not None
    assert reason.strategy_id == strategy_id
    assert reason.strategy_version_id == version_id
    assert reason.stage == "validation"
    assert reason.reason_type == "validation_error"
    assert reason.severity == "error"
    assert reason.details["field"] == "risk.stoploss"


def test_queries_failures_by_strategy_version_stage_and_type(db_session: Session) -> None:
    strategy_id, version_id = create_strategy_version(db_session)
    repository = StrategyFailureReasonRepository(db_session)

    for stage, reason_type in (
        ("generation", "blueprint_schema_error"),
        ("validation", "validation_error"),
        ("static_check", "static_policy_violation"),
        ("backtest_probe", "backtest_probe_failed"),
    ):
        saved = repository.record(
            StrategyFailureReasonCreate(
                strategy_id=strategy_id,
                strategy_version_id=version_id,
                stage=stage,
                reason_type=reason_type,
                message=f"{stage} failed.",
            )
        )
        assert saved is not None

    strategy_reasons = repository.list_for_strategy(strategy_id)
    validation_reasons = repository.list_for_version(version_id, stage="validation")
    static_reasons = repository.list_for_version(
        version_id,
        reason_type="static_policy_violation",
    )

    assert {reason.stage for reason in strategy_reasons} == {
        "generation",
        "validation",
        "static_check",
        "backtest_probe",
    }
    assert len(validation_reasons) == 1
    assert validation_reasons[0].reason_type == "validation_error"
    assert len(static_reasons) == 1
    assert static_reasons[0].stage == "static_check"


def test_service_returns_empty_results_for_missing_filters(db_session: Session) -> None:
    strategy_id, version_id = create_strategy_version(db_session)
    service = StrategyFailureReasonService(db_session)

    recorded = service.record_failure(
        StrategyFailureReasonCreate(
            strategy_id=strategy_id,
            strategy_version_id=version_id,
            stage="generation",
            reason_type="unknown",
            severity="warning",
            message="Provider returned an unclassified failure.",
        )
    )

    assert recorded is not None
    assert service.list_version_failures(version_id, reason_type="render_error") == []
    assert service.list_strategy_failures(999) == []


def test_record_rejects_missing_or_mismatched_version(db_session: Session) -> None:
    strategy_id, version_id = create_strategy_version(db_session)
    other_strategy_id, _ = create_strategy_version(db_session, slug="other-strategy")
    repository = StrategyFailureReasonRepository(db_session)

    missing = repository.record(
        StrategyFailureReasonCreate(
            strategy_id=strategy_id,
            strategy_version_id=999,
            stage="validation",
            reason_type="validation_error",
            message="Missing version.",
        )
    )
    mismatched = repository.record(
        StrategyFailureReasonCreate(
            strategy_id=other_strategy_id,
            strategy_version_id=version_id,
            stage="validation",
            reason_type="validation_error",
            message="Mismatched version.",
        )
    )

    assert missing is None
    assert mismatched is None


def test_schema_rejects_unknown_stage_and_reason_type(db_session: Session) -> None:
    strategy_id, version_id = create_strategy_version(db_session)

    with pytest.raises(ValidationError):
        StrategyFailureReasonCreate(
            strategy_id=strategy_id,
            strategy_version_id=version_id,
            stage="not_a_stage",
            reason_type="validation_error",
            message="Bad stage.",
        )

    with pytest.raises(ValidationError):
        StrategyFailureReasonCreate(
            strategy_id=strategy_id,
            strategy_version_id=version_id,
            stage="validation",
            reason_type="not_a_type",
            message="Bad reason type.",
        )
