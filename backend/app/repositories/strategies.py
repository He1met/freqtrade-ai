from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.strategy import Strategy, StrategyVersion
from app.schemas.strategy import (
    StrategyCreate,
    StrategyVersionCreate,
    StrategyVersionDiffRead,
    StrategyVersionLineageEntry,
)


class StrategyRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, payload: StrategyCreate) -> Strategy:
        strategy = Strategy(
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            status=payload.status,
            source=payload.source,
            tags=payload.tags,
        )
        self.db.add(strategy)
        self.db.commit()
        self.db.refresh(strategy)
        return strategy

    def get(self, strategy_id: int) -> Optional[Strategy]:
        return self.db.get(Strategy, strategy_id)

    def get_by_slug(self, slug: str) -> Optional[Strategy]:
        statement = select(Strategy).where(Strategy.slug == slug)
        return self.db.scalars(statement).first()

    def create_version(self, payload: StrategyVersionCreate) -> Optional[StrategyVersion]:
        strategy = self.get(payload.strategy_id)
        if strategy is None:
            return None
        if payload.parent_version_id is not None:
            parent = self.db.get(StrategyVersion, payload.parent_version_id)
            if parent is None or parent.strategy_id != strategy.id:
                return None

        version_number = payload.version_number or self._next_version_number(payload.strategy_id)
        version = StrategyVersion(
            strategy_id=payload.strategy_id,
            generation_run_id=payload.generation_run_id,
            parent_version_id=payload.parent_version_id,
            version_number=version_number,
            blueprint=payload.blueprint,
            generated_code=payload.generated_code,
            code_hash=payload.code_hash,
            file_path=payload.file_path,
            validation_status=payload.validation_status,
            validation_errors=payload.validation_errors,
            change_summary=payload.change_summary,
            diff_snapshot=payload.diff_snapshot,
        )
        self.db.add(version)
        self.db.flush()
        strategy.current_version_id = version.id
        self.db.commit()
        self.db.refresh(version)
        return version

    def get_latest_version(self, strategy_id: int) -> Optional[StrategyVersion]:
        statement = (
            select(StrategyVersion)
            .where(StrategyVersion.strategy_id == strategy_id)
            .order_by(StrategyVersion.version_number.desc())
            .limit(1)
        )
        return self.db.scalars(statement).first()

    def get_version(self, version_id: int) -> Optional[StrategyVersion]:
        return self.db.get(StrategyVersion, version_id)

    def list_version_lineage(self, strategy_id: int) -> list[StrategyVersionLineageEntry]:
        statement = (
            select(StrategyVersion)
            .where(StrategyVersion.strategy_id == strategy_id)
            .order_by(StrategyVersion.version_number.asc())
        )
        return [
            StrategyVersionLineageEntry.model_validate(version)
            for version in self.db.scalars(statement).all()
        ]

    def get_version_diff(self, version_id: int) -> Optional[StrategyVersionDiffRead]:
        version = self.get_version(version_id)
        if version is None:
            return None

        return StrategyVersionDiffRead(
            id=version.id,
            strategy_id=version.strategy_id,
            parent_version_id=version.parent_version_id,
            version_number=version.version_number,
            change_summary=version.change_summary,
            diff_snapshot=version.diff_snapshot,
            has_parent=version.parent_version_id is not None,
        )

    def _next_version_number(self, strategy_id: int) -> int:
        statement = select(func.max(StrategyVersion.version_number)).where(
            StrategyVersion.strategy_id == strategy_id
        )
        current_max = self.db.scalar(statement)
        return int(current_max or 0) + 1
