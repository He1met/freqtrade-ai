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
import tempfile
import threading
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.core.config import get_settings

from app.adapters.okx_demo.order_writer import (
    ManagedOrder,
    OkxDemoOrderWriter,
    WriterResult,
)
from app.adapters.okx_demo.credentials import OkxDemoCredentialsUnavailable
from app.adapters.okx_demo.read_adapter import OkxDemoReadClient
from app.adapters.okx_demo.server_factory import (
    OkxDemoServerSessionBlocked,
    create_okx_demo_server_session,
)
from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.adapters.okx_demo.writer_models import (
    ApprovedExecution,
    OrderSubmissionAuthorization,
    approved_execution_view,
)
from app.services.okx_demo_reconciliation import OkxDemoReconciliationBlocked
from app.services.okx_demo_submission_grant import (
    acquire_one_shot_runtime_lock,
    arm_finalized_canary_consent,
    fail_canary_grant_before_prepare,
    release_one_shot_runtime_lock,
    settle_canary_consent_handoff,
)
from app.services.okx_demo_canary_preparation import (
    OkxDemoCanaryConsentCaptureFailed,
    process_pending_canary_attestation,
    process_pending_canary_consent_handoff,
)


RECONCILIATION_MODULE = "app.adapters.okx_demo.reconciliation_runtime"
READY_FILENAME = "okx-runtime.ready.json"
FAILURE_FILENAME = "okx-runtime.failure.json"
WRITER_LOCK_FILENAME = "okx-demo-order-writer.lock"
OPENINGS_FREEZE_FILENAME = "okx-runtime.freeze-openings"
POLL_SECONDS = 1.0
MAX_RECONCILIATION_AGE_SECONDS = 30
STOP_EVENT = threading.Event()
SAFE_STARTUP_FAILURE_STAGES = frozenset(
    {
        "reconciliation-adapter-load",
        "reconciliation-adapter-create",
        "writer-lock",
        "read-attestation",
        "writer-credential-bridge",
        "database-engine",
        "database-connect",
        "database-session",
        "startup-reconciliation",
        "writer-capability",
        "runtime",
    }
)
SAFE_STARTUP_FAILURE_CATEGORIES = frozenset(
    {
        "PREFLIGHT",
        "ATTESTATION",
        "DATABASE",
        "RECONCILIATION",
        "WRITER",
        "RUNTIME",
        "UNEXPECTED",
    }
)
SAFE_STARTUP_FAILURE_TYPES = frozenset(
    {
        "DatabaseError",
        "IntegrityError",
        "InterfaceError",
        "OkxDemoCredentialsUnavailable",
        "OkxDemoPreflightBlocked",
        "OkxDemoReconciliationBlocked",
        "OkxDemoRuntimeBlocked",
        "OkxDemoWriteBlocked",
        "OperationalError",
        "ProgrammingError",
        "UnexpectedError",
    }
)


class OkxDemoRuntimeBlocked(Exception):
    """The credential-bearing runtime cannot safely expose writer capability."""


class OkxDemoRuntimeStartupBlocked(OkxDemoRuntimeBlocked):
    """Safe startup-stage failure that never renders the original exception."""

    def __init__(
        self,
        *,
        stage: str,
        category: str,
        cause_type: str,
    ) -> None:
        if stage not in SAFE_STARTUP_FAILURE_STAGES:
            stage = "runtime"
        if category not in SAFE_STARTUP_FAILURE_CATEGORIES:
            category = "UNEXPECTED"
        if cause_type not in SAFE_STARTUP_FAILURE_TYPES:
            cause_type = "UnexpectedError"
            category = "UNEXPECTED"
        self.stage = stage
        self.category = category
        self.cause_type = cause_type
        super().__init__(
            "OKX_DEMO runtime startup blocked "
            "[stage={}, category={}, cause_type={}]".format(
                stage,
                category,
                cause_type,
            )
        )


def _runtime_failure_category(stage: str) -> str:
    if stage.startswith("database-"):
        return "DATABASE"
    if stage == "startup-reconciliation":
        return "RECONCILIATION"
    if stage == "writer-capability":
        return "WRITER"
    return "RUNTIME"


def _startup_call(stage: str, callback: Callable[[], Any]) -> Any:
    try:
        return callback()
    except (
        OkxDemoRuntimeStartupBlocked,
        KeyboardInterrupt,
        SystemExit,
    ):
        raise
    except OkxDemoServerSessionBlocked as exc:
        raise OkxDemoRuntimeStartupBlocked(
            stage=exc.stage,
            category=exc.category,
            cause_type=exc.cause_type,
        ) from None
    except BaseException as exc:
        cause_type = type(exc).__name__
        category = _runtime_failure_category(stage)
        if cause_type not in SAFE_STARTUP_FAILURE_TYPES:
            cause_type = "UnexpectedError"
            category = "UNEXPECTED"
        raise OkxDemoRuntimeStartupBlocked(
            stage=stage,
            category=category,
            cause_type=cause_type,
        ) from None


@dataclass(frozen=True)
class _ValidatedReconciliation:
    status: str
    safe_to_open: bool
    reconciliation_run_id: int


class RuntimeReconciliationAdapter(Protocol):
    """Narrow #448 boundary; #449 owns processes, not reconciliation models."""

    @property
    def runtime_instance_id(self) -> str: ...

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

    def run_active_one_shot(
        self,
        *,
        writer: "_RuntimeWriterCapability",
        db: Session,
        openings_allowed: bool,
    ) -> str: ...

    def can_resume_controlled_canary(
        self,
        db: Session,
        *,
        reconciliation_run_id: int,
    ) -> bool: ...

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
        reconciliation_run_id=run_id,
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

    def reconcile_unresolved(self, attempt_id: int) -> WriterResult:
        return self._writer.reconcile_unresolved(attempt_id)

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


def _write_startup_failure(
    path: Path,
    exc: BaseException,
) -> bool:
    stage = getattr(exc, "stage", "runtime")
    category = getattr(exc, "category", "UNEXPECTED")
    cause_type = getattr(exc, "cause_type", type(exc).__name__)
    if stage not in SAFE_STARTUP_FAILURE_STAGES:
        stage = "runtime"
    if category not in SAFE_STARTUP_FAILURE_CATEGORIES:
        category = "UNEXPECTED"
    if cause_type not in SAFE_STARTUP_FAILURE_TYPES:
        cause_type = "UnexpectedError"
        category = "UNEXPECTED"
    temporary = None
    descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="{}.".format(path.name),
            suffix=".tmp",
            dir=str(path.parent),
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(
                {
                    "status": "BLOCKED",
                    "stage": stage,
                    "category": category,
                    "cause_type": cause_type,
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return False
    return True


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
        factory = reconciliation_factory or _startup_call(
            "reconciliation-adapter-load",
            load_reconciliation_factory,
        )
        adapter = _startup_call("reconciliation-adapter-create", factory)
        server_session = _startup_call(
            "server-session",
            lambda: create_okx_demo_server_session(
                environment,
                lock_path=writer_lock_path,
            ),
        )
        engine = _startup_call(
            "database-engine",
            lambda: engine_factory(database_url, pool_pre_ping=True),
        )
        connection = _startup_call("database-connect", engine.connect)
        db = _startup_call(
            "database-session",
            lambda: Session(bind=connection),
        )
        startup = _startup_call(
            "startup-reconciliation",
            lambda: _reconcile_transaction(
                db,
                lambda: adapter.reconcile_before_writer(
                    read_client=server_session.read,
                    db=db,
                ),
                now=now_provider(),
            ),
        )
        recovery_only = (
            startup.status == "DRIFTED"
            and adapter.can_resume_controlled_canary(
                db,
                reconciliation_run_id=startup.reconciliation_run_id,
            )
        )
        if startup.status not in {"RECONCILED", "RECOVERED"} and not recovery_only:
            raise OkxDemoRuntimeBlocked(
                "OKX_DEMO startup requires exact reconciliation"
            )
        if db.in_transaction():
            db.rollback()
        writer = _startup_call(
            "writer-capability",
            lambda: _RuntimeWriterCapability(
                server_session.create_order_writer(db)
            ),
        )
        writer.set_openings_allowed(not recovery_only)
        _write_readiness(
            ready_path,
            {
                "status": "RECOVERY_ONLY" if recovery_only else "READY",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": startup.status,
                "writer": "UNIQUE",
                "pid": os.getpid(),
            },
        )
        while not STOP_EVENT.wait(POLL_SECONDS):
            externally_frozen = (
                runtime_path / OPENINGS_FREEZE_FILENAME
            ).is_file()
            coordination_acquired = acquire_one_shot_runtime_lock(db)
            if not coordination_acquired:
                db.rollback()
                writer.set_openings_allowed(False)
                raise OkxDemoRuntimeBlocked(
                    "one-shot coordination lock is busy"
                )
            coordination_lock_released = False
            try:
                def consent_safety_check() -> bool:
                    current_manifest = get_settings().execution_target_manifest
                    current_target = current_manifest.active_target
                    return (
                        not (runtime_path / OPENINGS_FREEZE_FILENAME).is_file()
                        and current_manifest.active_target_id == "OKX_DEMO"
                        and current_target.simulated_trading is True
                        and current_target.allow_real_funds is False
                        and current_target.order_submission_enabled is False
                    )

                recovered_grant = arm_finalized_canary_consent(
                    db, runtime_instance_id=adapter.runtime_instance_id
                )
                if recovered_grant is not None:
                    try:
                        recovered_result = adapter.run_active_one_shot(
                            writer=writer,
                            db=db,
                            openings_allowed=consent_safety_check(),
                        )
                    except Exception:
                        # The recovered Commit B path has the same boundary as
                        # the immediate path: revoke only before a durable
                        # placement journal exists.  PREPARED remains GET-only.
                        db.rollback()
                        fail_canary_grant_before_prepare(
                            db, grant_id=recovered_grant.grant_id
                        )
                        db.commit()
                        writer.set_openings_allowed(False)
                        raise
                    recovered_status = settle_canary_consent_handoff(
                        db, grant_id=recovered_grant.grant_id
                    )
                    db.commit()
                    if recovered_result != "CONSUMED" or recovered_status != "CONSUMED":
                        writer.set_openings_allowed(False)
                        raise OkxDemoRuntimeBlocked(
                            "recovered consent grant failed closed"
                        )
                    continue
                try:
                    consent_finalized = process_pending_canary_consent_handoff(
                        read_client=server_session.read,
                        db=db,
                        runtime_instance_id=adapter.runtime_instance_id,
                        fresh_reconciliation=lambda: adapter.observe(
                            read_client=server_session.read,
                            db=db,
                        ),
                        safety_check=consent_safety_check,
                        now=now_provider(),
                    )
                except OkxDemoCanaryConsentCaptureFailed as exc:
                    db.rollback()
                    terminalized = db.execute(
                        text(
                            "SELECT fail_requested_okx_demo_canary_consent("
                            ":handoff,:stage,:category)"
                        ),
                        {
                            "handoff": exc.handoff_id,
                            "stage": exc.stage,
                            "category": exc.category,
                        },
                    ).scalar_one()
                    db.commit()
                    writer.set_openings_allowed(False)
                    if terminalized is not True:
                        raise OkxDemoRuntimeBlocked(
                            "consent capture failure was not terminalized"
                        ) from exc
                    raise OkxDemoRuntimeBlocked(
                        "consent capture failed closed "
                        "[stage={}, category={}]".format(
                            exc.stage, exc.category
                        )
                    ) from exc
                if consent_finalized is not None:
                    # Commit A makes the exact fresh lineage durable with no
                    # grant.  Only this same runtime identity may issue and
                    # immediately consume the post-commit grant.
                    db.commit()
                    grant = arm_finalized_canary_consent(
                        db,
                        runtime_instance_id=adapter.runtime_instance_id,
                    )
                    if grant is None:
                        raise OkxDemoRuntimeBlocked(
                            "finalized canary consent did not issue a grant"
                        )
                    try:
                        one_shot_result = adapter.run_active_one_shot(
                            writer=writer,
                            db=db,
                            openings_allowed=consent_safety_check(),
                        )
                    except Exception:
                        # Commit B already made the grant durable.  Revoke it
                        # only if no placement journal was committed.  A
                        # PREPARED attempt is deliberately left for GET-only
                        # recovery by this or a restarted runtime.
                        db.rollback()
                        fail_canary_grant_before_prepare(
                            db, grant_id=grant.grant_id
                        )
                        db.commit()
                        writer.set_openings_allowed(False)
                        raise
                    handoff_status = settle_canary_consent_handoff(
                        db, grant_id=grant.grant_id
                    )
                    if one_shot_result != "CONSUMED":
                        db.commit()
                        writer.set_openings_allowed(False)
                        raise OkxDemoRuntimeBlocked(
                            "consent-bound one-shot grant failed before submission"
                        )
                    if handoff_status != "CONSUMED":
                        raise OkxDemoRuntimeBlocked(
                            "consent-bound one-shot handoff did not close"
                        )
                    db.commit()
                    continue
                # The backend API never owns OKX credentials.  A controlled
                # canary preparation is a DB-backed request that this sole
                # attested runtime fulfills before the one-shot grant path.
                canary_attestation_processed = process_pending_canary_attestation(
                    read_client=server_session.read,
                    db=db,
                    now=now_provider(),
                )
                if canary_attestation_processed:
                    db.commit()
                    # A fresh execution-only attestation has a deliberately
                    # short TTL.  Hand the coordination window back to the
                    # operator immediately after persisting it instead of
                    # holding the session lock across the normal network
                    # reconciliation cycle.  The next loop iteration restores
                    # the usual observe/cycle behavior under the same single
                    # runtime and writer.
                    try:
                        lock_released = release_one_shot_runtime_lock(db)
                    finally:
                        # The unlock call either released the session lock or
                        # raised after discovering that ownership was lost.
                        # In both cases the outer cleanup must not issue a
                        # second unlock on the same handoff.
                        coordination_lock_released = True
                    if not lock_released:
                        raise OkxDemoRuntimeBlocked(
                            "canonical runtime lost one-shot coordination lock"
                        )
                    db.commit()
                    writer.set_openings_allowed(False)
                    continue
                one_shot_result = adapter.run_active_one_shot(
                    writer=writer,
                    db=db,
                    openings_allowed=not externally_frozen,
                )
                if one_shot_result == "FAILED":
                    db.commit()
                    writer.set_openings_allowed(False)
                    raise OkxDemoRuntimeBlocked(
                        "one-shot grant failed closed before submission"
                    )
                if one_shot_result == "CONSUMED":
                    db.commit()
                    continue
                observed = _reconcile_transaction(
                    db,
                    lambda: adapter.observe(
                        read_client=server_session.read,
                        db=db,
                    ),
                    now=now_provider(),
                )
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
                if not coordination_lock_released and release_one_shot_runtime_lock(db):
                    db.commit()
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
    runtime_path = _runtime_path(args.runtime_dir)
    failure_path = runtime_path / FAILURE_FILENAME
    failure_path.unlink(missing_ok=True)
    STOP_EVENT.clear()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        serve(
            environment=os.environ,
            runtime_path=runtime_path,
        )
        return 0
    except (
        OkxDemoRuntimeBlocked,
        OkxDemoReconciliationBlocked,
        OkxDemoWriteBlocked,
        OkxDemoCredentialsUnavailable,
    ) as exc:
        _write_startup_failure(failure_path, exc)
        # This process writes to a mode-0600 runtime log.  Known domain errors
        # are deliberately safe to retain there and make a fail-closed startup
        # diagnosable without printing credentials or request signatures.
        print("OKX_DEMO runtime blocked: {}".format(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        _write_startup_failure(failure_path, exc)
        print(
            "OKX_DEMO runtime failed unexpectedly; inspect the private runtime log",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
