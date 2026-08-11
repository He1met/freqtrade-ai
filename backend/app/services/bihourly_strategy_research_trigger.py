from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
from typing import Any, Callable, Literal

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.services.bihourly_strategy_research import (
    BihourlyStrategyResearchBlocked,
    BihourlyStrategyResearchService,
)


ResearchTrigger = Literal["manual", "automation"]


@dataclass(frozen=True)
class BihourlyStrategyResearchTriggerResult:
    schema_version: str
    status: Literal["GENERATED", "NO_OP", "FAILED"]
    reason_code: str
    trigger: ResearchTrigger
    run_id: str
    persisted_count: int
    runtime_status: str
    opening_guard: Literal["RUNNING", "BLOCKED"]
    generation_only: bool = True
    serial_consumer_separate: bool = True
    backtest_started: bool = False
    deployment_started: bool = False
    signal_or_order_started: bool = False
    real_orders: bool = False
    allow_real_funds: bool = False
    exchange_access: str = "PUBLIC_MARKET_DATA_ONLY"

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class BihourlyStrategyResearchTrigger:
    """Shared fail-closed entry point for manual and scheduled generation.

    The trigger refreshes public candles and persists a complete batch.  It does
    not claim validation jobs and never calls a trading or order path.
    """

    def __init__(
        self,
        db: Session,
        *,
        canonical_root: Path,
        datadir: Path,
        receipt_path: Path,
        runtime_snapshot: Callable[[], dict[str, Any]] | None = None,
        repository_state: Callable[[], tuple[str, str]] | None = None,
        refresh: Callable[[], None] | None = None,
        generation_service: BihourlyStrategyResearchService | None = None,
    ) -> None:
        self.db = db
        self.canonical_root = canonical_root.resolve()
        self.datadir = datadir.resolve()
        self.receipt_path = receipt_path.resolve()
        self._runtime_snapshot = runtime_snapshot or self._read_runtime_snapshot
        self._repository_state = repository_state or self._read_repository_state
        self._refresh = refresh or self._refresh_public_market_data
        self._generation_service = generation_service

    def run(
        self,
        *,
        trigger: ResearchTrigger,
        run_id: str | None = None,
        owner_task_id: str | None = None,
        now: datetime | None = None,
    ) -> BihourlyStrategyResearchTriggerResult:
        current = _as_utc(now or datetime.now(timezone.utc))
        resolved_run_id = run_id or _run_id(current)
        runtime_status = "UNKNOWN"
        opening_guard: Literal["RUNNING", "BLOCKED"] = "BLOCKED"
        try:
            snapshot = self._runtime_snapshot()
            runtime_status, opening_guard = _validate_runtime_snapshot(snapshot)
            branch, repository_commit = self._repository_state()
            if branch != "main":
                return self._result(
                    status="NO_OP",
                    reason_code="CANONICAL_BRANCH_IS_NOT_MAIN",
                    trigger=trigger,
                    run_id=resolved_run_id,
                    runtime_status=runtime_status,
                    opening_guard=opening_guard,
                )
            if len(repository_commit) != 40:
                return self._result(
                    status="NO_OP",
                    reason_code="CANONICAL_COMMIT_INVALID",
                    trigger=trigger,
                    run_id=resolved_run_id,
                    runtime_status=runtime_status,
                    opening_guard=opening_guard,
                )
            service = self._generation_service or BihourlyStrategyResearchService(
                self.db, canonical_root=self.canonical_root, datadir=self.datadir
            )
            generated = service.run_generation_only(
                run_id=resolved_run_id,
                repository_commit=repository_commit,
                owner_task_id=(
                    owner_task_id
                    or f"bihourly-strategy-research:{socket.gethostname()}"
                ),
                refresh=self._refresh,
                now=current,
            )
            return self._result(
                status=generated.status,
                reason_code=generated.reason_code,
                trigger=trigger,
                run_id=resolved_run_id,
                persisted_count=generated.persisted_count,
                runtime_status=runtime_status,
                opening_guard=opening_guard,
            )
        except _RuntimePrecheckBlocked as exc:
            return self._result(
                status="NO_OP",
                reason_code=str(exc),
                trigger=trigger,
                run_id=resolved_run_id,
                runtime_status=runtime_status,
                opening_guard=opening_guard,
            )
        except BihourlyStrategyResearchBlocked as exc:
            return self._result(
                status="FAILED",
                reason_code=str(exc)[:500],
                trigger=trigger,
                run_id=resolved_run_id,
                runtime_status=runtime_status,
                opening_guard=opening_guard,
            )

    def _read_runtime_snapshot(self) -> dict[str, Any]:
        python = self.canonical_root / "backend/.venv/bin/python"
        completed = subprocess.run(
            [str(python), "scripts/local_runtime.py", "--json", "verify"],
            cwd=self.canonical_root,
            check=False,
            capture_output=True,
            text=True,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise _RuntimePrecheckBlocked("RUNTIME_VERIFY_EVIDENCE_INVALID") from exc
        if completed.returncode != 0 or not isinstance(payload, dict):
            raise _RuntimePrecheckBlocked("RUNTIME_VERIFY_FAILED")
        return payload

    def _read_repository_state(self) -> tuple[str, str]:
        return self._git_output("rev-parse", "--abbrev-ref", "HEAD"), self._git_output(
            "rev-parse", "HEAD"
        )

    def _git_output(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.canonical_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise _RuntimePrecheckBlocked("CANONICAL_GIT_STATE_UNAVAILABLE")
        return completed.stdout.strip()

    def _refresh_public_market_data(self) -> None:
        python = self.canonical_root / "backend/.venv/bin/python"
        completed = subprocess.run(
            [
                str(python),
                "scripts/download_okx_research_market_data.py",
                "--datadir",
                str(self.datadir),
                "--receipt",
                str(self.receipt_path),
                "--incremental",
            ],
            cwd=self.canonical_root,
            check=False,
        )
        if completed.returncode != 0:
            raise BihourlyStrategyResearchBlocked(
                "PUBLIC_MARKET_DATA_REFRESH_FAILED"
            )

    @staticmethod
    def _result(
        *,
        status: Literal["GENERATED", "NO_OP", "FAILED"],
        reason_code: str,
        trigger: ResearchTrigger,
        run_id: str,
        runtime_status: str,
        opening_guard: Literal["RUNNING", "BLOCKED"],
        persisted_count: int = 0,
    ) -> BihourlyStrategyResearchTriggerResult:
        return BihourlyStrategyResearchTriggerResult(
            schema_version="bihourly-strategy-research-trigger-v1",
            status=status,
            reason_code=reason_code,
            trigger=trigger,
            run_id=run_id,
            persisted_count=persisted_count,
            runtime_status=runtime_status,
            opening_guard=opening_guard,
        )


class OwnerMediatedBihourlyStrategyResearchTrigger:
    """Open only the canonical local peer-owner connection for queue writes."""

    def __init__(
        self,
        *,
        canonical_root: Path,
        datadir: Path,
        receipt_path: Path,
        database_url: str,
    ) -> None:
        self.canonical_root = canonical_root.resolve()
        self.datadir = datadir.resolve()
        self.receipt_path = receipt_path.resolve()
        self.database_url = database_url

    def run(
        self,
        *,
        trigger: ResearchTrigger,
        run_id: str | None = None,
        owner_task_id: str | None = None,
        now: datetime | None = None,
    ) -> BihourlyStrategyResearchTriggerResult:
        current = _as_utc(now or datetime.now(timezone.utc))
        resolved_run_id = run_id or _run_id(current)
        try:
            owner_url = _canonical_peer_owner_url(self.database_url)
            engine = create_engine(owner_url, pool_pre_ping=True)
            factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
            try:
                with factory() as db:
                    identity = db.execute(
                        text(
                            "SELECT current_user, current_database(), current_schema()"
                        )
                    ).one()
                    if tuple(identity)[1:] != (
                        "freqtrade_ai",
                        "public",
                    ) or identity[0] in {"freqtrade", "freqtrade_ai_attestor"}:
                        raise _OwnerDatabaseBlocked
                    result = BihourlyStrategyResearchTrigger(
                        db,
                        canonical_root=self.canonical_root,
                        datadir=self.datadir,
                        receipt_path=self.receipt_path,
                    ).run(
                        trigger=trigger,
                        run_id=resolved_run_id,
                        owner_task_id=owner_task_id,
                        now=current,
                    )
                    if result.status == "FAILED":
                        db.rollback()
                    else:
                        db.commit()
                    return result
            finally:
                engine.dispose()
        except (OSError, ValueError, SQLAlchemyError, _OwnerDatabaseBlocked):
            return BihourlyStrategyResearchTrigger._result(
                status="NO_OP",
                reason_code="RESEARCH_OWNER_DATABASE_UNAVAILABLE",
                trigger=trigger,
                run_id=resolved_run_id,
                runtime_status="UNKNOWN",
                opening_guard="BLOCKED",
            )


class _RuntimePrecheckBlocked(RuntimeError):
    pass


class _OwnerDatabaseBlocked(RuntimeError):
    pass


def _validate_runtime_snapshot(
    snapshot: dict[str, Any],
) -> tuple[str, Literal["RUNNING", "BLOCKED"]]:
    runtime_status = snapshot.get("status")
    execution_target = snapshot.get("execution_target")
    trading = snapshot.get("trading")
    database = snapshot.get("database")
    okx_runtime = snapshot.get("okx_runtime")
    services = snapshot.get("services")
    if runtime_status not in {"VERIFIED", "BLOCKED_OPENINGS"}:
        raise _RuntimePrecheckBlocked("RUNTIME_NOT_SAFE_FOR_RESEARCH")
    if not isinstance(execution_target, dict) or (
        execution_target.get("active"), execution_target.get("status")
    ) != ("OKX_DEMO", "READY"):
        raise _RuntimePrecheckBlocked("RUNTIME_NOT_OKX_DEMO_READY")
    if not isinstance(trading, dict) or any(
        trading.get(key) is not False for key in ("live", "dry_run", "real_orders")
    ):
        raise _RuntimePrecheckBlocked("RUNTIME_TRADING_SAFETY_INVALID")
    if not isinstance(database, dict) or (
        database.get("kind"), database.get("schema")
    ) != ("postgresql", "verified"):
        raise _RuntimePrecheckBlocked("RUNTIME_DATABASE_NOT_VERIFIED")
    if not isinstance(okx_runtime, dict) or (
        okx_runtime.get("execution_target") != "OKX_DEMO"
        or okx_runtime.get("adapter") != "ATTESTED"
        or okx_runtime.get("writer") != "UNIQUE"
        or okx_runtime.get("reconciliation") not in {"RECOVERED", "RECONCILED"}
        or okx_runtime.get("automation_guard") not in {"RUNNING", "BLOCKED"}
    ):
        raise _RuntimePrecheckBlocked("RUNTIME_WRITER_OR_GUARD_INVALID")
    if not isinstance(services, list) or {
        item.get("service")
        for item in services
        if isinstance(item, dict) and item.get("running") is True
    } != {"backend", "worker", "frontend", "okx_runtime"}:
        raise _RuntimePrecheckBlocked("RUNTIME_SERVICE_SET_NOT_HEALTHY")
    guard = okx_runtime["automation_guard"]
    if runtime_status == "VERIFIED" and guard != "RUNNING":
        raise _RuntimePrecheckBlocked("RUNTIME_GUARD_STATE_INCONSISTENT")
    if runtime_status == "BLOCKED_OPENINGS" and guard != "BLOCKED":
        raise _RuntimePrecheckBlocked("RUNTIME_GUARD_STATE_INCONSISTENT")
    return runtime_status, guard


def _run_id(now: datetime) -> str:
    floored = now.replace(
        hour=now.hour - now.hour % 2,
        minute=0,
        second=0,
        microsecond=0,
    )
    return floored.strftime("%Y%m%d%H")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_peer_owner_url(database_url: str) -> URL:
    parsed = make_url(database_url)
    if (
        parsed.get_backend_name() != "postgresql"
        or parsed.host not in {"localhost", "127.0.0.1", "::1"}
        or parsed.database != "freqtrade_ai"
    ):
        raise _OwnerDatabaseBlocked
    return URL.create(
        drivername="postgresql+psycopg",
        database="freqtrade_ai",
        query={"host": "/tmp", "port": str(parsed.port or 5432)},
    )


def get_owner_mediated_bihourly_strategy_research_trigger(
) -> OwnerMediatedBihourlyStrategyResearchTrigger:
    root = Path(__file__).resolve().parents[3]
    return OwnerMediatedBihourlyStrategyResearchTrigger(
        canonical_root=root,
        datadir=root / "user_data/data/okx",
        receipt_path=root
        / "reports/research/okx-public-candle-source-latest.json",
        database_url=get_settings().database_url,
    )
