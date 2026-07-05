from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ConfigurationError
from app.db.session import create_database_engine, create_session_factory, redact_database_url
from app.models import (
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Base,
    LocalTestBatch,
    LocalTestDbEvent,
    Strategy,
    StrategyFailureReason,
    StrategyGenerationRun,
    StrategyScore,
    StrategyVersion,
)
from app.schemas.data_source import DataSourceTrace, fixture_source, unknown_source


SAFE_ENVIRONMENT_LABELS = {"local", "dev", "test", "debug", "phase8", "local-test", "phase8-local"}
SAFE_POSTGRES_TOKENS = ("local", "test", "debug", "phase8")
UNSAFE_DATABASE_TOKENS = ("prod", "production", "shared", "remote", "live")
LOCAL_POSTGRES_HOSTS = {None, "", "localhost", "127.0.0.1", "::1"}
SAFE_SQLITE_ROOT = Path("/tmp").resolve()
SEED_VERSION = "phase8-local-test-db-v1"
SOURCE_LABEL = "phase8-local-test-db"
DEFAULT_SQLITE_PATH = Path("/tmp/freqtrade-ai-phase8-local-test.sqlite")


@dataclass(frozen=True)
class SafeDatabaseTarget:
    database_url: str
    dialect: str
    environment_label: str
    reason: str
    redacted_url: str


class LocalTestDatabaseGuard:
    """Fail-closed guard for destructive local/dev/test database operations."""

    def validate(self, database_url: str, environment_label: str) -> SafeDatabaseTarget:
        if environment_label not in SAFE_ENVIRONMENT_LABELS:
            raise ConfigurationError(
                "Refusing local test DB operation: environment label must be one of "
                f"{sorted(SAFE_ENVIRONMENT_LABELS)}."
            )

        try:
            url = make_url(database_url)
        except ArgumentError as exc:
            raise ConfigurationError("Refusing local test DB operation: invalid DATABASE_URL.") from exc

        if url.get_backend_name() == "sqlite":
            reason = self._validate_sqlite(url.database)
            return SafeDatabaseTarget(
                database_url=database_url,
                dialect="sqlite",
                environment_label=environment_label,
                reason=reason,
                redacted_url=redact_database_url(database_url),
            )

        if url.get_backend_name().startswith("postgresql"):
            reason = self._validate_postgres(url.host, url.database or "")
            return SafeDatabaseTarget(
                database_url=database_url,
                dialect="postgresql",
                environment_label=environment_label,
                reason=reason,
                redacted_url=redact_database_url(database_url),
            )

        raise ConfigurationError(
            "Refusing local test DB operation: only safe /tmp SQLite or explicit "
            "local/test/debug/phase8 PostgreSQL URLs are allowed."
        )

    def _validate_sqlite(self, raw_database: Optional[str]) -> str:
        if not raw_database or raw_database == ":memory:":
            raise ConfigurationError(
                "Refusing local test DB operation: SQLite must be a file under /tmp/freqtrade-ai-*."
            )

        path = Path(raw_database).expanduser()
        if not path.is_absolute():
            raise ConfigurationError(
                "Refusing local test DB operation: SQLite path must be absolute and under /tmp."
            )

        resolved_parent = path.parent.resolve()
        try:
            resolved_parent.relative_to(SAFE_SQLITE_ROOT)
        except ValueError as exc:
            raise ConfigurationError(
                "Refusing local test DB operation: SQLite path must stay under /tmp/freqtrade-ai-*."
            ) from exc

        if not path.name.startswith("freqtrade-ai-"):
            raise ConfigurationError(
                "Refusing local test DB operation: SQLite filename must start with freqtrade-ai-."
            )

        return "safe local SQLite path under /tmp/freqtrade-ai-*"

    def _validate_postgres(self, host: Optional[str], database_name: str) -> str:
        if host not in LOCAL_POSTGRES_HOSTS:
            raise ConfigurationError(
                "Refusing local test DB operation: PostgreSQL host must be localhost, 127.0.0.1, ::1, "
                "or a local socket."
            )

        normalized_name = database_name.lower()
        if any(token in normalized_name for token in UNSAFE_DATABASE_TOKENS):
            raise ConfigurationError(
                "Refusing local test DB operation: PostgreSQL database name looks production/shared/remote."
            )
        if not any(token in normalized_name for token in SAFE_POSTGRES_TOKENS):
            raise ConfigurationError(
                "Refusing local test DB operation: PostgreSQL database name must explicitly include "
                "local, test, debug, or phase8."
            )

        return "explicit localhost PostgreSQL database marked local/test/debug/phase8"


def default_database_url() -> str:
    return f"sqlite+pysqlite:///{DEFAULT_SQLITE_PATH}"


class Phase8LocalTestDbService:
    def __init__(
        self,
        database_url: str,
        *,
        environment_label: str = "local",
        engine: Optional[Engine] = None,
        session_factory: Optional[sessionmaker] = None,
    ) -> None:
        self.database_url = database_url
        self.environment_label = environment_label
        self._guard = LocalTestDatabaseGuard()
        self._engine = engine
        self._session_factory = session_factory

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_database_engine(self.database_url)
        return self._engine

    @property
    def session_factory(self) -> sessionmaker:
        if self._session_factory is None:
            self._session_factory = create_session_factory(self.engine)
        return self._session_factory

    def validate_target(self) -> SafeDatabaseTarget:
        return self._guard.validate(self.database_url, self.environment_label)

    def reset_database(self) -> dict[str, Any]:
        target = self.validate_target()
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        with self.session_factory() as session:
            batch = self._create_batch(session, "reset", "reset")
            self._record_event(
                session,
                batch,
                "reset",
                None,
                "api_generated",
                {"target": target.redacted_url, "guard": target.reason},
            )
            session.commit()
            return self.summarize_batch(batch.batch_key, session=session)

    def ensure_schema(self) -> None:
        self.validate_target()
        Base.metadata.create_all(self.engine)

    def seed_baseline(self) -> dict[str, Any]:
        self.ensure_schema()
        with self.session_factory() as session:
            batch = self._create_batch(session, "baseline", "baseline_seed")
            scenarios = [
                "success",
                "failed",
                "blocked",
                "unknown-source",
                "missing-artifact",
                "partial-completion",
            ]
            for scenario_name in scenarios:
                self._seed_scenario(session, batch, scenario_name, source_kind="seed_generated")
            session.commit()
            return self.summarize_batch(batch.batch_key, session=session)

    def seed_dirty_scenarios(self) -> dict[str, Any]:
        self.ensure_schema()
        with self.session_factory() as session:
            batch = self._create_batch(session, "dirty", "dirty_seed")
            for scenario_name in [
                "dirty-score-without-result",
                "dirty-stale-running-backtest",
                "dirty-task-result-path-without-result-row",
            ]:
                self._seed_scenario(
                    session,
                    batch,
                    scenario_name,
                    source_kind="dirty_seed_generated",
                )
            session.commit()
            return self.summarize_batch(batch.batch_key, session=session)

    def summarize_batches(self, limit: int = 20) -> dict[str, Any]:
        self.ensure_schema()
        with self.session_factory() as session:
            statement = (
                select(LocalTestBatch)
                .order_by(LocalTestBatch.created_at.desc(), LocalTestBatch.id.desc())
                .limit(limit)
            )
            return {
                "database": redact_database_url(self.database_url),
                "environment_label": self.environment_label,
                "batches": [self.summarize_batch(batch.batch_key, session=session) for batch in session.scalars(statement)],
            }

    def summarize_batch(self, batch_key: str, *, session: Optional[Session] = None) -> dict[str, Any]:
        owns_session = session is None
        active_session = session or self.session_factory()
        try:
            batch = active_session.scalars(
                select(LocalTestBatch).where(LocalTestBatch.batch_key == batch_key)
            ).first()
            if batch is None:
                raise ConfigurationError(f"Local test batch not found: {batch_key}")

            event_rows = active_session.scalars(
                select(LocalTestDbEvent)
                .where(LocalTestDbEvent.batch_id == batch.id)
                .order_by(LocalTestDbEvent.id.asc())
            ).all()
            scenario_counts: dict[str, int] = {}
            source_counts: dict[str, int] = {}
            for event in event_rows:
                if event.scenario_name:
                    scenario_counts[event.scenario_name] = scenario_counts.get(event.scenario_name, 0) + 1
                source_counts[event.source_kind] = source_counts.get(event.source_kind, 0) + 1

            return {
                "batch_id": batch.id,
                "batch_key": batch.batch_key,
                "scenario_set": batch.scenario_set,
                "source_label": batch.source_label,
                "environment_label": batch.environment_label,
                "database": batch.database_url,
                "seed_version": batch.seed_version,
                "created_at": batch.created_at.isoformat() if batch.created_at else None,
                "scenario_counts": scenario_counts,
                "source_counts": source_counts,
                "events": [
                    {
                        "id": event.id,
                        "event_type": event.event_type,
                        "scenario_name": event.scenario_name,
                        "source_kind": event.source_kind,
                        "details": event.details,
                    }
                    for event in event_rows
                ],
            }
        finally:
            if owns_session:
                active_session.close()

    def _create_batch(self, session: Session, scenario_set: str, action: str) -> LocalTestBatch:
        now = datetime.now(timezone.utc)
        key = f"phase8-{scenario_set}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
        batch = LocalTestBatch(
            batch_key=key,
            scenario_set=scenario_set,
            source_label=SOURCE_LABEL,
            environment_label=self.environment_label,
            database_url=redact_database_url(self.database_url),
            seed_version=SEED_VERSION,
            batch_metadata={
                "phase8_local_test": True,
                "action": action,
                "source_label": SOURCE_LABEL,
                "not_core_success": True,
            },
        )
        session.add(batch)
        session.flush()
        return batch

    def _seed_scenario(
        self,
        session: Session,
        batch: LocalTestBatch,
        scenario_name: str,
        *,
        source_kind: str,
    ) -> None:
        metadata = self._source_metadata(batch, scenario_name, source_kind)
        slug = f"{batch.batch_key}-{scenario_name}".replace("_", "-")
        generation_run = self._create_generation_run(session, metadata, scenario_name, source_kind)
        strategy = Strategy(
            name=f"Phase8 Local Test {scenario_name}",
            slug=slug[:180],
            description=(
                f"Phase 8 local-test {scenario_name} seed row. This is QA fixture data "
                "and is not production or core-flow evidence."
            ),
            status="draft" if "failed" not in scenario_name else "archived",
            source="manual",
            tags=self._strategy_tags(metadata),
        )
        session.add(strategy)
        session.flush()

        validation_status = "failed" if scenario_name in {"failed", "blocked", "unknown-source"} else "passed"
        version = StrategyVersion(
            strategy_id=strategy.id,
            generation_run_id=generation_run.id,
            version_number=1,
            blueprint={
                "class_name": f"Phase8LocalTest{scenario_name.title().replace('-', '')}",
                "phase8_local_test": metadata,
            },
            generated_code=(
                "# Phase 8 local-test fixture only; not runnable production evidence.\n"
                f"class Phase8LocalTest{scenario_name.title().replace('-', '')}:\n"
                "    pass\n"
            ),
            code_hash=f"phase8-fixture-{uuid4().hex[:16]}",
            file_path=f"user_data/strategies/generated/phase8-local-test/{batch.batch_key}/{scenario_name}.py",
            validation_status=validation_status,
            validation_errors=self._validation_errors(metadata, scenario_name),
            change_summary=f"Phase 8 local-test {scenario_name} seed; not core success.",
            diff_snapshot={"phase8_local_test": metadata, "not_core_success": True},
        )
        session.add(version)
        session.flush()
        strategy.current_version_id = version.id

        self._seed_backtest_state(session, batch, strategy, version, metadata, scenario_name, source_kind)
        self._record_event(
            session,
            batch,
            "dirty_seed" if source_kind == "dirty_seed_generated" else "baseline_seed",
            scenario_name,
            source_kind,
            {
                "strategy_id": strategy.id,
                "strategy_version_id": version.id,
                "data_source": metadata["data_source"],
                "not_core_success": True,
            },
        )

    def _create_generation_run(
        self,
        session: Session,
        metadata: dict[str, Any],
        scenario_name: str,
        source_kind: str,
    ) -> StrategyGenerationRun:
        if scenario_name == "success":
            status = "succeeded"
            generated_count = accepted_count = 1
            failed_count = 0
            error_message = None
        elif scenario_name in {"partial-completion", "dirty-stale-running-backtest"}:
            status = "running"
            generated_count = 1
            accepted_count = 0
            failed_count = 0
            error_message = None
        else:
            status = "failed"
            generated_count = 0
            accepted_count = 0
            failed_count = 1
            error_message = self._scenario_error_message(scenario_name)

        run = StrategyGenerationRun(
            provider=f"{SOURCE_LABEL}:{source_kind}",
            model="local-seed-fixture",
            prompt_hash=f"phase8-{scenario_name}",
            prompt_summary=f"Phase 8 {scenario_name} local-test seed; not core success.",
            params_snapshot={"phase8_local_test": metadata, "not_core_success": True},
            status=status,
            requested_count=1,
            generated_count=generated_count,
            accepted_count=accepted_count,
            failed_count=failed_count,
            error_message=error_message,
            started_at=datetime.now(timezone.utc),
            completed_at=None if status == "running" else datetime.now(timezone.utc),
        )
        session.add(run)
        session.flush()
        return run

    def _seed_backtest_state(
        self,
        session: Session,
        batch: LocalTestBatch,
        strategy: Strategy,
        version: StrategyVersion,
        metadata: dict[str, Any],
        scenario_name: str,
        source_kind: str,
    ) -> None:
        run_status = self._backtest_run_status(scenario_name)
        requested_task_count = 2 if scenario_name == "partial-completion" else 1
        backtest_run = BacktestRun(
            strategy_version_id=version.id,
            profile_name=f"phase8-local-test-{scenario_name}",
            config_snapshot={
                "phase8_local_test": metadata,
                "not_core_success": True,
                "source_kind": source_kind,
            },
            status=run_status,
            requested_task_count=requested_task_count,
            started_at=datetime.now(timezone.utc),
            completed_at=None if run_status == "running" else datetime.now(timezone.utc),
        )
        session.add(backtest_run)
        session.flush()

        task = self._create_task(session, backtest_run, metadata, scenario_name)
        if scenario_name in {"success", "missing-artifact", "partial-completion"}:
            result = self._create_result(session, backtest_run, task, metadata, scenario_name)
            self._create_score(session, strategy, version, result.id, metadata, scenario_name)
        elif scenario_name == "dirty-score-without-result":
            self._create_score(session, strategy, version, None, metadata, scenario_name)
        elif scenario_name == "dirty-task-result-path-without-result-row":
            task.status = "succeeded"
            task.result_path = self._artifact_path(batch, scenario_name, "missing-result-row.json")
            task.completed_at = datetime.now(timezone.utc)
        elif scenario_name in {"failed", "blocked", "unknown-source"}:
            self._create_failure_reason(session, strategy, version, metadata, scenario_name)

        if scenario_name == "partial-completion":
            pending_task = BacktestTask(
                backtest_run_id=backtest_run.id,
                pair="ETH/USDT",
                timeframe="15m",
                status="pending",
                config_path=self._artifact_path(batch, scenario_name, "eth-config.json"),
                error_message=None,
            )
            session.add(pending_task)

    def _create_task(
        self,
        session: Session,
        backtest_run: BacktestRun,
        metadata: dict[str, Any],
        scenario_name: str,
    ) -> BacktestTask:
        status = "succeeded" if scenario_name in {"success", "missing-artifact", "partial-completion"} else "failed"
        if scenario_name == "dirty-stale-running-backtest":
            status = "running"
        task = BacktestTask(
            backtest_run_id=backtest_run.id,
            pair="BTC/USDT",
            timeframe="15m",
            status=status,
            config_path=self._artifact_path_for_metadata(metadata, scenario_name, "config.json"),
            result_path=(
                self._artifact_path_for_metadata(metadata, scenario_name, "result.json")
                if status == "succeeded"
                else None
            ),
            error_message=None if status == "succeeded" else self._scenario_error_message(scenario_name),
            started_at=datetime.now(timezone.utc),
            completed_at=None if status == "running" else datetime.now(timezone.utc),
        )
        session.add(task)
        session.flush()
        return task

    def _create_result(
        self,
        session: Session,
        backtest_run: BacktestRun,
        task: BacktestTask,
        metadata: dict[str, Any],
        scenario_name: str,
    ) -> BacktestResult:
        result_path = task.result_path or self._artifact_path_for_metadata(metadata, scenario_name, "result.json")
        if scenario_name == "missing-artifact":
            result_path = self._artifact_path_for_metadata(metadata, scenario_name, "missing-artifact.json")
            task.result_path = result_path
        result = BacktestResult(
            backtest_run_id=backtest_run.id,
            backtest_task_id=task.id,
            result_path=result_path,
            metrics_snapshot={
                "phase8_local_test": metadata,
                "profit_total": 42.0 if scenario_name == "success" else 0.0,
                "not_core_success": True,
            },
            profit_total=42.0 if scenario_name == "success" else 0.0,
            profit_pct=0.042 if scenario_name == "success" else 0.0,
            max_drawdown_pct=0.015 if scenario_name == "success" else None,
            win_rate=0.61 if scenario_name == "success" else None,
            total_trades=12 if scenario_name == "success" else 0,
            timerange="20240101-20240131",
        )
        session.add(result)
        session.flush()
        return result

    def _create_score(
        self,
        session: Session,
        strategy: Strategy,
        version: StrategyVersion,
        backtest_result_id: Optional[int],
        metadata: dict[str, Any],
        scenario_name: str,
    ) -> None:
        score = StrategyScore(
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            backtest_result_id=backtest_result_id,
            scoring_version=f"phase8-local-test-{scenario_name}",
            total_score=72.0 if scenario_name == "success" else 0.0,
            profit_score=70.0 if scenario_name == "success" else 0.0,
            risk_score=74.0 if scenario_name == "success" else 0.0,
            stability_score=72.0 if scenario_name == "success" else 0.0,
            quality_score=72.0 if scenario_name == "success" else 0.0,
            metrics_snapshot={"phase8_local_test": metadata, "not_core_success": True},
        )
        session.add(score)

    def _create_failure_reason(
        self,
        session: Session,
        strategy: Strategy,
        version: StrategyVersion,
        metadata: dict[str, Any],
        scenario_name: str,
    ) -> None:
        reason = StrategyFailureReason(
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            stage="backtest_probe" if scenario_name in {"blocked", "failed"} else "generation",
            reason_type="backtest_probe_failed" if scenario_name in {"blocked", "failed"} else "unknown",
            severity="warning" if scenario_name == "blocked" else "error",
            message=self._scenario_error_message(scenario_name),
            details={"phase8_local_test": metadata, "not_core_success": True},
        )
        session.add(reason)

    def _record_event(
        self,
        session: Session,
        batch: LocalTestBatch,
        event_type: str,
        scenario_name: Optional[str],
        source_kind: str,
        details: dict[str, Any],
    ) -> None:
        session.add(
            LocalTestDbEvent(
                batch_id=batch.id,
                event_type=event_type,
                scenario_name=scenario_name,
                source_kind=source_kind,
                details=details,
            )
        )

    def _source_metadata(
        self,
        batch: LocalTestBatch,
        scenario_name: str,
        source_kind: str,
    ) -> dict[str, Any]:
        blocked_reason = self._blocked_reason(scenario_name)
        source_trace = self._scenario_data_source(scenario_name, blocked_reason)
        return {
            "phase8_local_test": True,
            "test_batch": {
                "batch_id": batch.id,
                "batch_key": batch.batch_key,
                "scenario": scenario_name,
                "scenario_set": batch.scenario_set,
                "source_kind": source_kind,
                "source_label": batch.source_label,
                "environment_label": batch.environment_label,
                "seed_version": batch.seed_version,
            },
            "data_source": source_trace.model_dump(mode="json"),
            "blocked_reason": blocked_reason,
            "not_core_success": True,
        }

    def _scenario_data_source(self, scenario_name: str, blocked_reason: Optional[str]) -> DataSourceTrace:
        if scenario_name == "unknown-source":
            return unknown_source(
                "Phase 8 local-test unknown-source seed; cannot prove core success.",
                blocked_reason=blocked_reason or "seed intentionally models unknown data origin",
            )
        return fixture_source(
            f"Phase 8 local-test {scenario_name} seed from {SOURCE_LABEL}; not core success.",
            blocked_reason=blocked_reason,
        )

    def _strategy_tags(self, metadata: dict[str, Any]) -> list[str]:
        batch = metadata["test_batch"]
        return [
            "phase8-local-test",
            f"test-batch:{batch['batch_key']}",
            f"scenario:{batch['scenario']}",
            f"source-kind:{batch['source_kind']}",
            "not-core-success",
        ]

    def _validation_errors(self, metadata: dict[str, Any], scenario_name: str) -> list[dict[str, Any]]:
        if scenario_name not in {"failed", "blocked", "unknown-source"}:
            return []
        return [
            {
                "message": self._scenario_error_message(scenario_name),
                "phase8_local_test": metadata,
                "not_core_success": True,
            }
        ]

    def _backtest_run_status(self, scenario_name: str) -> str:
        if scenario_name in {"success", "missing-artifact"}:
            return "succeeded"
        if scenario_name in {"partial-completion", "dirty-stale-running-backtest"}:
            return "running"
        return "failed"

    def _scenario_error_message(self, scenario_name: str) -> str:
        if scenario_name == "blocked":
            return "BLOCKED: local market data fixture missing; no download was attempted."
        if scenario_name == "unknown-source":
            return "UNKNOWN_SOURCE: seed intentionally lacks a trusted core-flow origin."
        if scenario_name == "dirty-score-without-result":
            return "DIRTY_DATA: score exists without a backtest result row."
        if scenario_name == "dirty-task-result-path-without-result-row":
            return "DIRTY_DATA: task has a result path but no parsed result row."
        if scenario_name == "dirty-stale-running-backtest":
            return "DIRTY_DATA: running state is intentionally stale for QA inspection."
        return "FAILED: local-test fixture failure; not core-flow evidence."

    def _blocked_reason(self, scenario_name: str) -> Optional[str]:
        if scenario_name in {"blocked", "unknown-source", "missing-artifact"} or scenario_name.startswith("dirty-"):
            return self._scenario_error_message(scenario_name)
        return None

    def _artifact_path_for_metadata(self, metadata: dict[str, Any], scenario_name: str, filename: str) -> str:
        batch_key = metadata["test_batch"]["batch_key"]
        return f"/tmp/freqtrade-ai-phase8-local-test/{batch_key}/{scenario_name}/{filename}"

    def _artifact_path(self, batch: LocalTestBatch, scenario_name: str, filename: str) -> str:
        return f"/tmp/freqtrade-ai-phase8-local-test/{batch.batch_key}/{scenario_name}/{filename}"
