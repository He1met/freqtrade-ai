from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.models.strategy import Strategy, StrategyVersion
from app.models.strategy_platform import (
    ExecutionTargetDefinition,
    QualityRuleEvaluation,
    StrategyEvaluationSummary,
    StrategyTarget,
    ValidationWindowConfig,
    ValidationWindowScore,
)
from app.models.strategy_validation import (
    StrategyValidationPlan,
    StrategyValidationWindow,
)
from app.schemas.strategy_platform import (
    DynamicValidationWindowRead,
    StrategyCatalogCurrentVersionRead,
    StrategyCatalogItemRead,
    StrategyCatalogPageRead,
    StrategyTargetProjectionRead,
    StrategyValidationCycleRead,
    StrategyValidationHistoryRead,
    ValidationFailureReasonRead,
)


class StrategyPlatformReadService:
    """Database-backed V1.3 catalog and dynamic validation projections."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def strategy_catalog(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> StrategyCatalogPageRead:
        statement = select(Strategy, StrategyVersion).outerjoin(
            StrategyVersion, Strategy.current_version_id == StrategyVersion.id
        )
        if cursor is not None:
            created_at, strategy_id = _decode_cursor(cursor)
            statement = statement.where(
                or_(
                    Strategy.created_at < created_at,
                    and_(Strategy.created_at == created_at, Strategy.id < strategy_id),
                )
            )
        rows = self.db.execute(
            statement.order_by(Strategy.created_at.desc(), Strategy.id.desc()).limit(
                limit + 1
            )
        ).all()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        versions = [version for _, version in page_rows if version is not None]
        targets_by_version, latest_plan_by_target = self._load_targets(
            [version.id for version in versions]
        )
        items = []
        for strategy, version in page_rows:
            targets = (
                [
                    self._target_read(target, target_key, latest_plan_by_target)
                    for target, target_key in targets_by_version.get(version.id, [])
                ]
                if version is not None
                else []
            )
            current_version = (
                StrategyCatalogCurrentVersionRead(
                    id=version.id,
                    version_number=version.version_number,
                    static_validation_status=version.validation_status.upper(),
                    created_at=version.created_at,
                )
                if version is not None
                else None
            )
            items.append(
                StrategyCatalogItemRead(
                    id=strategy.id,
                    name=strategy.name,
                    slug=strategy.slug,
                    description=strategy.description,
                    source=strategy.source,
                    tags=strategy.tags,
                    catalog_status=strategy.status.upper(),
                    current_version=current_version,
                    targets=targets,
                    target_count=len(targets),
                    created_at=strategy.created_at,
                    updated_at=strategy.updated_at,
                )
            )
        next_cursor = None
        if has_more and page_rows:
            last_strategy = page_rows[-1][0]
            next_cursor = _encode_cursor(last_strategy.created_at, last_strategy.id)
        return StrategyCatalogPageRead(items=items, next_cursor=next_cursor)

    def validation_history(
        self, *, strategy_id: int, limit: int = 50
    ) -> StrategyValidationHistoryRead:
        if self.db.get(Strategy, strategy_id) is None:
            raise StrategyPlatformReadError(
                "STRATEGY_NOT_FOUND",
                "Strategy does not exist.",
                status_code=404,
                context={"strategy_id": strategy_id},
            )
        plans = list(
            self.db.scalars(
                select(StrategyValidationPlan)
                .join(
                    StrategyVersion,
                    StrategyValidationPlan.strategy_version_id == StrategyVersion.id,
                )
                .where(StrategyVersion.strategy_id == strategy_id)
                .order_by(
                    StrategyValidationPlan.created_at.desc(),
                    StrategyValidationPlan.id.desc(),
                )
                .limit(limit)
            ).all()
        )
        if not plans:
            return StrategyValidationHistoryRead(strategy_id=strategy_id, cycles=[])

        plan_ids = [plan.id for plan in plans]
        windows = list(
            self.db.scalars(
                select(StrategyValidationWindow)
                .where(StrategyValidationWindow.validation_plan_id.in_(plan_ids))
                .order_by(
                    StrategyValidationWindow.validation_plan_id,
                    StrategyValidationWindow.ordinal,
                    StrategyValidationWindow.attempt_number,
                    StrategyValidationWindow.id,
                )
            ).all()
        )
        window_config_ids = {
            row.window_config_id for row in windows if row.window_config_id is not None
        }
        window_configs = {
            row.id: row
            for row in (
                self.db.scalars(
                    select(ValidationWindowConfig).where(
                        ValidationWindowConfig.id.in_(window_config_ids)
                    )
                ).all()
                if window_config_ids
                else []
            )
        }
        window_ids = [row.id for row in windows]
        scores = list(
            self.db.scalars(
                select(ValidationWindowScore).where(
                    ValidationWindowScore.validation_window_id.in_(window_ids)
                )
            ).all()
        )
        score_by_window = {row.validation_window_id: row for row in scores}
        score_ids = [row.id for row in scores]
        rule_evaluations = (
            list(
                self.db.scalars(
                    select(QualityRuleEvaluation)
                    .where(
                        QualityRuleEvaluation.validation_window_score_id.in_(score_ids),
                        QualityRuleEvaluation.passed.is_(False),
                    )
                    .order_by(
                        QualityRuleEvaluation.validation_window_score_id,
                        QualityRuleEvaluation.id,
                    )
                ).all()
            )
            if score_ids
            else []
        )
        rules_by_score: dict[int, list[QualityRuleEvaluation]] = {}
        for rule in rule_evaluations:
            rules_by_score.setdefault(rule.validation_window_score_id, []).append(rule)

        summaries = {
            row.validation_plan_id: row
            for row in self.db.scalars(
                select(StrategyEvaluationSummary).where(
                    StrategyEvaluationSummary.validation_plan_id.in_(plan_ids)
                )
            ).all()
        }
        windows_by_plan: dict[int, list[StrategyValidationWindow]] = {}
        for window in windows:
            windows_by_plan.setdefault(window.validation_plan_id, []).append(window)

        target_ids = {
            plan.strategy_target_id for plan in plans if plan.strategy_target_id
        }
        target_rows, latest_plan_by_target = self._load_target_rows(target_ids)
        targets_by_id = {
            target.id: self._target_read(target, target_key, latest_plan_by_target)
            for target, target_key in target_rows
        }

        cycles = []
        for plan in plans:
            summary = summaries.get(plan.id)
            window_reads = [
                self._window_read(
                    window,
                    window_configs.get(window.window_config_id),
                    score_by_window.get(window.id),
                    rules_by_score,
                )
                for window in windows_by_plan.get(plan.id, [])
            ]
            cycles.append(
                StrategyValidationCycleRead(
                    id=plan.id,
                    strategy_version_id=plan.strategy_version_id,
                    strategy_target_id=plan.strategy_target_id,
                    target=targets_by_id.get(plan.strategy_target_id),
                    cycle_number=plan.cycle_number,
                    status=plan.status,
                    required_window_count=(
                        summary.required_window_count if summary is not None else None
                    ),
                    passed_window_count=(
                        summary.passed_window_count if summary is not None else None
                    ),
                    failed_window_count=(
                        summary.failed_window_count if summary is not None else None
                    ),
                    overall_score=(
                        summary.overall_score if summary is not None else None
                    ),
                    reason_codes=summary.reason_codes if summary is not None else [],
                    configuration_bundle_snapshot_id=plan.configuration_bundle_snapshot_id,
                    validation_window_config_set_id=plan.validation_window_config_set_id,
                    created_at=plan.created_at,
                    started_at=plan.started_at,
                    completed_at=plan.completed_at,
                    windows=window_reads,
                )
            )
        return StrategyValidationHistoryRead(strategy_id=strategy_id, cycles=cycles)

    def _load_targets(self, strategy_version_ids: list[int]) -> tuple[
        dict[int, list[tuple[StrategyTarget, str]]],
        dict[int, StrategyValidationPlan],
    ]:
        if not strategy_version_ids:
            return {}, {}
        rows, latest_plan_by_target = self._load_target_rows_for_versions(
            strategy_version_ids
        )
        targets_by_version: dict[int, list[tuple[StrategyTarget, str]]] = {}
        for target, target_key in rows:
            targets_by_version.setdefault(target.strategy_version_id, []).append(
                (target, target_key)
            )
        return targets_by_version, latest_plan_by_target

    def _load_target_rows_for_versions(
        self, version_ids: list[int]
    ) -> tuple[list[tuple[StrategyTarget, str]], dict[int, StrategyValidationPlan]]:
        rows = list(
            self.db.execute(
                select(StrategyTarget, ExecutionTargetDefinition.target_key)
                .join(
                    ExecutionTargetDefinition,
                    StrategyTarget.execution_target_id == ExecutionTargetDefinition.id,
                )
                .where(StrategyTarget.strategy_version_id.in_(version_ids))
                .order_by(
                    StrategyTarget.strategy_version_id,
                    StrategyTarget.validation_priority,
                    StrategyTarget.id,
                )
            ).all()
        )
        target_ids = {target.id for target, _ in rows}
        return rows, self._latest_plans(target_ids)

    def _load_target_rows(
        self, target_ids: set[int]
    ) -> tuple[list[tuple[StrategyTarget, str]], dict[int, StrategyValidationPlan]]:
        if not target_ids:
            return [], {}
        rows = list(
            self.db.execute(
                select(StrategyTarget, ExecutionTargetDefinition.target_key)
                .join(
                    ExecutionTargetDefinition,
                    StrategyTarget.execution_target_id == ExecutionTargetDefinition.id,
                )
                .where(StrategyTarget.id.in_(target_ids))
                .order_by(StrategyTarget.id)
            ).all()
        )
        return rows, self._latest_plans(target_ids)

    def _latest_plans(self, target_ids: set[int]) -> dict[int, StrategyValidationPlan]:
        if not target_ids:
            return {}
        plans = self.db.scalars(
            select(StrategyValidationPlan)
            .where(StrategyValidationPlan.strategy_target_id.in_(target_ids))
            .order_by(
                StrategyValidationPlan.strategy_target_id,
                StrategyValidationPlan.created_at.desc(),
                StrategyValidationPlan.id.desc(),
            )
        ).all()
        latest: dict[int, StrategyValidationPlan] = {}
        for plan in plans:
            if plan.strategy_target_id is not None:
                latest.setdefault(plan.strategy_target_id, plan)
        return latest

    @staticmethod
    def _target_read(
        target: StrategyTarget,
        target_key: str,
        latest_plan_by_target: dict[int, StrategyValidationPlan],
    ) -> StrategyTargetProjectionRead:
        latest_plan = latest_plan_by_target.get(target.id)
        return StrategyTargetProjectionRead(
            id=target.id,
            strategy_version_id=target.strategy_version_id,
            execution_target_id=target.execution_target_id,
            execution_target_key=target_key,
            instrument_id=target.instrument_id,
            pair=target.pair,
            timeframe=target.timeframe,
            status=target.status,
            validation_priority=target.validation_priority,
            latest_validation_plan_id=(
                latest_plan.id if latest_plan is not None else None
            ),
            research_status=(
                latest_plan.status if latest_plan is not None else "NOT_QUEUED"
            ),
            last_completed_validation_at=target.last_completed_validation_at,
            next_validation_not_before=target.next_validation_not_before,
            created_at=target.created_at,
            updated_at=target.updated_at,
        )

    @staticmethod
    def _window_read(
        window: StrategyValidationWindow,
        config: ValidationWindowConfig | None,
        score: ValidationWindowScore | None,
        rules_by_score: dict[int, list[QualityRuleEvaluation]],
    ) -> DynamicValidationWindowRead:
        window_key = window.window_key_snapshot or (
            config.window_key if config is not None else None
        )
        name_zh = window.name_zh_snapshot or (
            config.name_zh if config is not None else None
        )
        description_zh = window.description_zh_snapshot or (
            config.description_zh if config is not None else None
        )
        projection_status = (
            "AVAILABLE"
            if window.window_config_id is not None and window_key and name_zh
            else "LEGACY_INCOMPLETE"
        )
        failure_reasons = []
        if window.failure_code or window.failure_message or window.blocked_reason:
            failure_reasons.append(
                ValidationFailureReasonRead(
                    code=window.failure_code or "VALIDATION_WINDOW_BLOCKED",
                    message=window.failure_message or window.blocked_reason,
                )
            )
        if score is not None:
            failure_reasons.extend(
                ValidationFailureReasonRead(
                    code=rule.failure_code or "QUALITY_RULE_FAILED",
                    message=rule.explanation,
                    quality_gate_rule_id=rule.quality_gate_rule_id,
                    actual_value=(
                        float(rule.actual_value)
                        if rule.actual_value is not None
                        else None
                    ),
                    operator=rule.operator,
                    threshold_snapshot=rule.threshold_snapshot,
                )
                for rule in rules_by_score.get(score.id, [])
            )
        return DynamicValidationWindowRead(
            id=window.id,
            window_config_id=window.window_config_id,
            window_key=window_key,
            ordinal=window.ordinal,
            attempt_number=window.attempt_number,
            name_zh=name_zh,
            description_zh=description_zh,
            projection_status=projection_status,
            score=score.total_score if score is not None else None,
            status=window.status,
            net_profit_after_cost=window.net_profit_after_cost,
            max_drawdown=window.max_drawdown,
            volatility=window.volatility,
            total_trades=window.total_trades,
            failure_reasons=failure_reasons,
        )


def _encode_cursor(created_at: datetime, strategy_id: int) -> str:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    raw = json.dumps(
        {"created_at": created_at.isoformat(), "id": strategy_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
        created_at = datetime.fromisoformat(payload["created_at"])
        strategy_id = payload["id"]
        if (
            created_at.tzinfo is None
            or isinstance(strategy_id, bool)
            or not isinstance(strategy_id, int)
        ):
            raise ValueError("cursor fields are invalid")
        return created_at, strategy_id
    except (
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise StrategyPlatformReadError(
            "INVALID_STRATEGY_CATALOG_CURSOR",
            "Strategy catalog cursor is invalid.",
            status_code=422,
        ) from exc
