from typing import Callable, TypeVar

from sqlalchemy.orm import Session

from app.repositories.execution_lineage import ExecutionLineageRepository


ResultT = TypeVar("ResultT")


class ExecutionLineagePersistenceService:
    """Run one persistence use-case in one caller-visible database transaction."""

    def __init__(self, db: Session, execution_target_id: str) -> None:
        self.db = db
        self.execution_target_id = execution_target_id

    def run(
        self,
        operation: Callable[[ExecutionLineageRepository, Session], ResultT],
    ) -> ResultT:
        if self.db.in_transaction():
            raise RuntimeError(
                "execution lineage use-case requires a clean session transaction boundary"
            )
        with self.db.begin():
            repository = ExecutionLineageRepository(
                self.db,
                self.execution_target_id,
            )
            return operation(repository, self.db)
