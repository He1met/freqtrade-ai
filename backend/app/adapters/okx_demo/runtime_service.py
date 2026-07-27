from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.okx_demo.order_writer import (
    ManagedOrder,
    OkxDemoOrderWriter,
    WriterResult,
)
from app.adapters.okx_demo.credentials import OkxDemoCredentialsUnavailable
from app.adapters.okx_demo.read_adapter import OkxDemoReadClient
from app.adapters.okx_demo.server_factory import create_okx_demo_server_session
from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.adapters.okx_demo.writer_models import (
    ApprovedExecution,
    OrderSubmissionAuthorization,
    approved_execution_view,
)
from app.services.okx_demo_reconciliation import OkxDemoReconciliationBlocked


RECONCILIATION_MODULE = "app.adapters.okx_demo.reconciliation_runtime"
READY_FILENAME = "okx-runtime.ready.json"
WRITER_LOCK_FILENAME = "okx-demo-order-writer.lock"
OPENINGS_FREEZE_FILENAME = "okx-runtime.freeze-openings"
POLL_SECONDS = 1.0
MAX_RECONCILIATION_AGE_SECONDS = 30
STOP_EVENT = threading.Event()


class OkxDemoRuntimeBlocked(Exception):
    """The credential-bearing runtime cannot safely expose writer capability."""


@dataclass(frozen=True)
class _ValidatedReconciliation:
    status: str
    safe_to_open: bool


class RuntimeReconciliationAdapter(Protocol):
    """Narrow #448 boundary; #449 owns processes, not reconciliation models."""

    def reconcile_before_writer(
        self,
        *,
        read_client: OkxDemoReadClient,
        db: Session,
    ) -> Mapping[str, Any]: ...

    def observe(
        self,
        *,
        read_client: OkxDemoReadClient,
        db: Session,
    ) -> Mapping[str, Any]: ...

    def run_cycle(
        self,
        *,
        read_client: OkxDemoReadClient,
        writer: "_RuntimeWriterCapability",
        db: Session,
    ) -> None: ...

    def close(self) -> None: ...


ReconciliationFactory = Callable[[], RuntimeReconciliationAdapter]


def load_reconciliation_factory() -> ReconciliationFactory:
    """Load the one #448 adapter without guessing or copying its persistence."""

    try:
        module = importlib.import_module(RECONCILIATION_MODULE)
        factory = getattr(module, "create_runtime_reconciliation_adapter")
    except (ImportError, AttributeError):
        raise OkxDemoRuntimeBlocked(
            "OKX_DEMO runtime reconciliation adapter is unavailable"
        ) from None
    if not callable(factory):
        raise OkxDemoRuntimeBlocked(
            "OKX_DEMO runtime reconciliation adapter is unavailable"
        )
    return factory


def _validate_reconciliation(
    result: Any,
    *,
    now: datetime,
) -> _ValidatedReconciliation:
    if not isinstance(result, Mapping) or set(result) != {
        "status",
        "execution_target",
        "reconciliation_run_id",
        "database_ids",
        "observed_at",
        "safe_to_open",
    }:
        raise OkxDemoRuntimeBlocked(
            "OKX_DEMO reconciliation returned an invalid readiness contract"
        )
    status = result.get("status")
    if status not in {
        "RECONCILED",
        "RECOVERED",
        "DRIFTED",
        "STALE",
        "UNKNOWN",
    }:
        raise OkxDemoRuntimeBlocked(
            "OKX_DEMO reconciliation returned an invalid status"
        )
    if result.get("execution_target") != "OKX_DEMO":
        raise OkxDemoRuntimeBlocked(
            "OKX_DEMO reconciliation target does not match"
        )
    run_id = result.get("reconciliation_run_id")
    database_ids = result.get("database_ids")
    expected_database_id_keys = {
        "reconciliation_run",
        "exchange_events",
        "order_snapshots",
        "fill_snapshots",
        "position_snapshots",
        "account_snapshots",
        "repaired_exchange_orders",
        "recovery_batches",
        "reconciliation_state",
    }
    if (
        not isinstance(run_id, int)
        or isinstance(run_id, bool)
        or run_id <= 0
        or not isinstance(database_ids, Mapping)
        or set(database_ids) != expected_database_id_keys
        or database_ids.get("reconciliation_run") != [run_id]
        or not isinstance(database_ids.get("reconciliation_state"), list)
        or len(database_ids["reconciliation_state"]) != 1
        or any(
            not isinstance(values, list)
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                for value in values
            )
            for values in database_ids.values()
        )
    ):
        raise OkxDemoRuntimeBlocked(
            "OKX_DEMO reconciliation database identity is invalid"
        )
    try:
        observed_at = datetime.fromisoformat(
            str(result.get("observed_at")).replace("Z", "+00:00")
        )
    except ValueError:
        raise OkxDemoRuntimeBlocked(
            "OKX_DEMO reconciliation freshness is invalid"
        ) from None
    if observed_at.tzinfo is None:
        raise OkxDemoRuntimeBlocked(
            "OKX_DEMO reconciliation freshness is invalid"
        )
    age = now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)
    fresh = -timedelta(seconds=5) <= age <= timedelta(
        seconds=MAX_RECONCILIATION_AGE_SECONDS
    )
    safe_to_open = result.get("safe_to_open")
    if not isinstance(safe_to_open, bool):
        raise OkxDemoRuntimeBlocked(
            "OKX_DEMO reconciliation opening policy is invalid"
        )
    if status in {"RECONCILED", "RECOVERED"}:
        if not fresh or safe_to_open is not True:
            raise OkxDemoRuntimeBlocked(
                "OKX_DEMO reconciled evidence is stale or blocks openings"
            )
    elif safe_to_open:
        raise OkxDemoRuntimeBlocked(
            "OKX_DEMO drifted evidence cannot authorize openings"
        )
    return _ValidatedReconciliation(
        status=status,
        safe_to_open=status in {"RECONCILED", "RECOVERED"},
    )


def _reconcile_transaction(
    db: Session,
    operation: Callable[[], Any],
    *,
    now: datetime,
) -> _ValidatedReconciliation:
    try:
        result = _validate_reconciliation(operation(), now=now)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


class _RuntimeWriterCapability:
    """Freeze new risk while preserving cancellation and reduce-only exits."""

    def __init__(self, writer: OkxDemoOrderWriter) -> None:
        self._writer = writer
        self._openings_allowed = False

    def set_openings_allowed(self, allowed: bool) -> None:
        self._openings_allowed = allowed

    def place(
        self,
        approved: ApprovedExecution,
        *,
        submission_grant: OrderSubmissionAuthorization,
    ) -> WriterResult:
        view = approved_execution_view(approved)
        if not self._openings_allowed and not view.reduce_only:
            raise OkxDemoWriteBlocked(
                "OKX_DEMO openings are frozen pending reconciliation"
            )
        return self._writer.place(
            approved,
            submission_grant=submission_grant,
        )

    def cancel(
        self,
        order: ManagedOrder,
        *,
        submission_grant: OrderSubmissionAuthorization,
    ) -> WriterResult:
        return self._writer.cancel(order, submission_grant=submission_grant)

    def recovery_cancel(
        self,
        *,
        recovery_grant_database_id: int,
    ) -> WriterResult:
        return self._writer.recovery_cancel(
            recovery_grant_database_id=recovery_grant_database_id
        )

    def recovery_reduce_only(
        self,
        *,
        recovery_grant_database_id: int,
    ) -> WriterResult:
        return self._writer.recovery_reduce_only(
            recovery_grant_database_id=recovery_grant_database_id
        )

    def amend(
        self,
        order: ManagedOrder,
        *,
        submission_grant: OrderSubmissionAuthorization,
        request_id: str,
        new_contracts: Optional[Decimal] = None,
        new_price: Optional[Decimal] = None,
    ) -> WriterResult:
        if not self._openings_allowed and (
            new_contracts is None
            or new_contracts >= order.contracts
            or new_price is not None
        ):
            raise OkxDemoWriteBlocked(
                "OKX_DEMO risk-increasing amend is frozen pending reconciliation"
            )
        return self._writer.amend(
            order,
            submission_grant=submission_grant,
            request_id=request_id,
            new_contracts=new_contracts,
            new_price=new_price,
        )


def _runtime_path(raw_path: str) -> Path:
    repo_root = Path(__file__).resolve().parents[4]
    candidate = Path(raw_path).expanduser().resolve()
    try:
        candidate.relative_to(repo_root / ".freqtrade-ai")
    except ValueError as exc:
        raise OkxDemoRuntimeBlocked(
            "OKX_DEMO runtime directory must stay inside .freqtrade-ai"
        ) from exc
    candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
    return candidate


def _write_readiness(path: Path, payload: Mapping[str, Any]) -> None:
    allowed = {
        "status",
        "execution_target",
        "adapter",
        "reconciliation",
        "writer",
        "pid",
    }
    if set(payload) != allowed:
        raise OkxDemoRuntimeBlocked("runtime readiness contains unexpected fields")
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)


def _stop(_signum: int, _frame: Optional[object]) -> None:
    STOP_EVENT.set()


def serve(
    *,
    environment: Mapping[str, str],
    runtime_path: Path,
    reconciliation_factory: Optional[ReconciliationFactory] = None,
    engine_factory: Callable[..., Any] = create_engine,
    now_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> None:
    """Own the only credential-bearing adapter/reconciler/writer lifecycle."""

    database_url = environment.get("DATABASE_URL", "")
    if not database_url:
        raise OkxDemoRuntimeBlocked("OKX_DEMO runtime database is missing")
    ready_path = runtime_path / READY_FILENAME
    writer_lock_path = runtime_path / WRITER_LOCK_FILENAME
    ready_path.unlink(missing_ok=True)
    adapter: Optional[RuntimeReconciliationAdapter] = None
    server_session = None
    engine = None
    connection = None
    db = None
    try:
        factory = reconciliation_factory or load_reconciliation_factory()
        adapter = factory()
        server_session = create_okx_demo_server_session(
            environment,
            lock_path=writer_lock_path,
        )
        engine = engine_factory(database_url, pool_pre_ping=True)
        connection = engine.connect()
        db = Session(bind=connection)
        startup = _reconcile_transaction(
            db,
            lambda: adapter.reconcile_before_writer(
                read_client=server_session.read,
                db=db,
            ),
            now=now_provider(),
        )
        if startup.status not in {"RECONCILED", "RECOVERED"}:
            raise OkxDemoRuntimeBlocked(
                "OKX_DEMO startup requires exact reconciliation"
            )
        if db.in_transaction():
            db.rollback()
        writer = _RuntimeWriterCapability(
            server_session.create_order_writer(db)
        )
        writer.set_openings_allowed(True)
        _write_readiness(
            ready_path,
            {
                "status": "READY",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": startup.status,
                "writer": "UNIQUE",
                "pid": os.getpid(),
            },
        )
        while not STOP_EVENT.wait(POLL_SECONDS):
            observed = _reconcile_transaction(
                db,
                lambda: adapter.observe(
                    read_client=server_session.read,
                    db=db,
                ),
                now=now_provider(),
            )
            externally_frozen = (
                runtime_path / OPENINGS_FREEZE_FILENAME
            ).is_file()
            writer.set_openings_allowed(
                observed.safe_to_open and not externally_frozen
            )
            _write_readiness(
                ready_path,
                {
                    "status": (
                        "READY"
                        if observed.safe_to_open and not externally_frozen
                        else "BLOCKED_OPENINGS"
                    ),
                    "execution_target": "OKX_DEMO",
                    "adapter": "ATTESTED",
                    "reconciliation": (
                        observed.status if not externally_frozen else "UNKNOWN"
                    ),
                    "writer": "UNIQUE",
                    "pid": os.getpid(),
                },
            )
            try:
                adapter.run_cycle(
                    read_client=server_session.read,
                    writer=writer,
                    db=db,
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
    finally:
        primary_error = sys.exc_info()[1]
        cleanup_error = None
        cleanup_actions = [
            lambda: ready_path.unlink(missing_ok=True),
        ]
        if adapter is not None:
            cleanup_actions.append(adapter.close)
        if server_session is not None:
            cleanup_actions.append(server_session.close)
        if db is not None:
            cleanup_actions.append(db.close)
        if connection is not None:
            cleanup_actions.append(connection.close)
        if engine is not None:
            cleanup_actions.append(engine.dispose)
        for cleanup_action in cleanup_actions:
            try:
                cleanup_action()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    STOP_EVENT.clear()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        serve(
            environment=os.environ,
            runtime_path=_runtime_path(args.runtime_dir),
        )
        return 0
    except (
        OkxDemoRuntimeBlocked,
        OkxDemoReconciliationBlocked,
        OkxDemoWriteBlocked,
        OkxDemoCredentialsUnavailable,
    ) as exc:
        # This process writes to a mode-0600 runtime log.  Known domain errors
        # are deliberately safe to retain there and make a fail-closed startup
        # diagnosable without printing credentials or request signatures.
        print("OKX_DEMO runtime blocked: {}".format(exc), file=sys.stderr)
        return 2
    except Exception:
        print(
            "OKX_DEMO runtime failed unexpectedly; inspect the private runtime log",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
