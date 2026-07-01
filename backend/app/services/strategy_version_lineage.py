from typing import Optional

from sqlalchemy.orm import Session

from app.repositories import StrategyRepository
from app.schemas import (
    StrategyVersionDiffRead,
    StrategyVersionLineageEntry,
)


class StrategyVersionLineageService:
    def __init__(self, db: Session) -> None:
        self.repository = StrategyRepository(db)

    def list_lineage(self, strategy_id: int) -> list[StrategyVersionLineageEntry]:
        return self.repository.list_version_lineage(strategy_id)

    def get_diff(self, version_id: int) -> Optional[StrategyVersionDiffRead]:
        return self.repository.get_version_diff(version_id)
