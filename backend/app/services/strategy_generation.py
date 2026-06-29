from typing import Optional, Protocol

from sqlalchemy.orm import Session

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.repositories import StrategyGenerationRunRepository, StrategyRepository
from app.schemas import (
    StrategyCreate,
    StrategyGenerationRunCreate,
    StrategyGenerationRunStatusUpdate,
    StrategyVersionCreate,
)
from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_renderer import StrategyCodeRenderer


class StrategyBlueprintProvider(Protocol):
    provider_name: str
    model_name: str

    def generate(self, prompt_summary: str, requested_count: int) -> list[StrategyBlueprint]:
        ...


class FakeStrategyBlueprintProvider:
    provider_name = "fake"
    model_name = "offline-fixture"

    def __init__(self, blueprints: Optional[list[StrategyBlueprint]] = None) -> None:
        self.blueprints = blueprints or [
            StrategyBlueprint(
                name="MVP RSI Strategy",
                slug="mvp-rsi-strategy",
                class_name="MvpRsiStrategy",
                description="Offline fixture strategy generated for Phase 1 smoke coverage.",
                indicators=[
                    {"name": "rsi", "kind": "rsi", "period": 14},
                    {"name": "ema_fast", "kind": "ema", "period": 12},
                ],
                entry_rules=[{"indicator": "rsi", "operator": "<", "value": 30}],
                exit_rules=[{"indicator": "rsi", "operator": ">", "value": 70}],
                tags=["phase-1", "fake-provider"],
            )
        ]

    def generate(self, prompt_summary: str, requested_count: int) -> list[StrategyBlueprint]:
        return self.blueprints[:requested_count]


class StrategyGenerationService:
    def __init__(
        self,
        db: Session,
        provider: StrategyBlueprintProvider,
        renderer: Optional[StrategyCodeRenderer] = None,
        file_manager: Optional[StrategyFileManager] = None,
    ) -> None:
        self.run_repository = StrategyGenerationRunRepository(db)
        self.strategy_repository = StrategyRepository(db)
        self.provider = provider
        self.renderer = renderer or StrategyCodeRenderer()
        self.file_manager = file_manager or StrategyFileManager()

    def run_once(self, prompt_summary: str, requested_count: int = 1) -> list[int]:
        run = self.run_repository.create(
            StrategyGenerationRunCreate(
                provider=self.provider.provider_name,
                model=self.provider.model_name,
                prompt_summary=prompt_summary,
                params_snapshot={"mode": "offline"},
                requested_count=requested_count,
            )
        )
        self.run_repository.update_status(
            run.id,
            StrategyGenerationRunStatusUpdate(status="running"),
        )

        try:
            blueprints = self.provider.generate(prompt_summary, requested_count)
            version_ids = self._persist_blueprints(run.id, blueprints)
        except Exception as exc:
            self.run_repository.update_status(
                run.id,
                StrategyGenerationRunStatusUpdate(
                    status="failed",
                    failed_count=requested_count,
                    error_message=str(exc),
                ),
            )
            raise

        self.run_repository.update_status(
            run.id,
            StrategyGenerationRunStatusUpdate(
                status="succeeded",
                generated_count=len(blueprints),
                accepted_count=len(version_ids),
                failed_count=max(0, len(blueprints) - len(version_ids)),
            ),
        )
        return version_ids

    def _persist_blueprints(
        self,
        run_id: int,
        blueprints: list[StrategyBlueprint],
    ) -> list[int]:
        version_ids: list[int] = []
        for index, blueprint in enumerate(blueprints, start=1):
            code = self.renderer.render(blueprint)
            path = self.file_manager.write_strategy_file(
                blueprint.class_name,
                code,
                file_stem=f"{blueprint.slug}_run_{run_id}_{index}",
            )
            strategy = self.strategy_repository.get_by_slug(blueprint.slug)
            if strategy is None:
                strategy = self.strategy_repository.create(
                    StrategyCreate(
                        name=blueprint.name,
                        slug=blueprint.slug,
                        description=blueprint.description,
                        tags=blueprint.tags,
                    )
                )
            version = self.strategy_repository.create_version(
                StrategyVersionCreate(
                    strategy_id=strategy.id,
                    generation_run_id=run_id,
                    blueprint=blueprint.model_dump(),
                    generated_code=code,
                    file_path=str(path),
                    validation_status="passed",
                )
            )
            if version is not None:
                version_ids.append(version.id)
        return version_ids
