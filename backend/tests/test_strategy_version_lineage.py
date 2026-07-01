import pytest
from sqlalchemy.orm import Session

from app.db.session import create_database_engine, create_session_factory
from app.models import Base
from app.repositories import StrategyRepository
from app.schemas import StrategyCreate, StrategyVersionCreate
from app.services.strategy_version_lineage import StrategyVersionLineageService


@pytest.fixture()
def db_session() -> Session:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        yield session


def create_strategy(repository: StrategyRepository, slug: str = "lineage-rsi"):
    return repository.create(
        StrategyCreate(
            name="Lineage RSI",
            slug=slug,
            description="Strategy used to verify version lineage.",
        )
    )


def test_records_parent_lineage_and_diff_summary(db_session: Session) -> None:
    repository = StrategyRepository(db_session)
    strategy = create_strategy(repository)

    parent = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": "LineageRsi"},
            generated_code="class LineageRsi: pass",
            file_path="user_data/strategies/generated/lineage_rsi_v1.py",
            change_summary="Initial generated version.",
        )
    )
    assert parent is not None

    child = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            parent_version_id=parent.id,
            blueprint={"class_name": "LineageRsi", "risk": "lower"},
            generated_code="class LineageRsiV2: pass",
            file_path="user_data/strategies/generated/lineage_rsi_v2.py",
            change_summary="Lowered risk by tightening RSI entry.",
            diff_snapshot={
                "changed_fields": ["entry_rules[0].value"],
                "before": {"entry_rules[0].value": 30},
                "after": {"entry_rules[0].value": 25},
            },
        )
    )

    assert child is not None
    assert child.parent_version_id == parent.id
    assert child.version_number == 2

    lineage = repository.list_version_lineage(strategy.id)
    assert [entry.id for entry in lineage] == [parent.id, child.id]
    assert lineage[0].parent_version_id is None
    assert lineage[1].parent_version_id == parent.id
    assert lineage[1].change_summary == "Lowered risk by tightening RSI entry."

    diff = repository.get_version_diff(child.id)
    assert diff is not None
    assert diff.has_parent is True
    assert diff.parent_version_id == parent.id
    assert diff.diff_snapshot["changed_fields"] == ["entry_rules[0].value"]


def test_no_parent_version_has_clear_empty_diff(db_session: Session) -> None:
    repository = StrategyRepository(db_session)
    strategy = create_strategy(repository, slug="lineage-empty-parent")

    version = repository.create_version(
        StrategyVersionCreate(
            strategy_id=strategy.id,
            blueprint={"class_name": "NoParent"},
            generated_code="class NoParent: pass",
            file_path="user_data/strategies/generated/no_parent.py",
        )
    )

    assert version is not None
    diff = StrategyVersionLineageService(db_session).get_diff(version.id)
    assert diff is not None
    assert diff.has_parent is False
    assert diff.parent_version_id is None
    assert diff.change_summary is None
    assert diff.diff_snapshot == {}


def test_rejects_parent_from_another_strategy(db_session: Session) -> None:
    repository = StrategyRepository(db_session)
    first_strategy = create_strategy(repository, slug="first-lineage")
    second_strategy = create_strategy(repository, slug="second-lineage")

    parent = repository.create_version(
        StrategyVersionCreate(
            strategy_id=first_strategy.id,
            blueprint={"class_name": "FirstLineage"},
            generated_code="class FirstLineage: pass",
            file_path="user_data/strategies/generated/first_lineage.py",
        )
    )
    assert parent is not None

    child = repository.create_version(
        StrategyVersionCreate(
            strategy_id=second_strategy.id,
            parent_version_id=parent.id,
            blueprint={"class_name": "InvalidLineage"},
            generated_code="class InvalidLineage: pass",
            file_path="user_data/strategies/generated/invalid_lineage.py",
        )
    )

    assert child is None
    assert repository.list_version_lineage(second_strategy.id) == []


def test_missing_version_diff_returns_none(db_session: Session) -> None:
    service = StrategyVersionLineageService(db_session)

    assert service.get_diff(999) is None
