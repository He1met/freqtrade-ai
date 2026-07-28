"""Fail-closed maintenance compaction for duplicated OKX Demo reconciliation data.

This module intentionally never touches the canonical current state, recovery
grants, full-chain lineage, or reconciliation evidence files.  It only removes
old REST snapshots when a later retained event has exactly the same immutable
authoritative identity and payload digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.db.migrations import verify_schema
from app.models import (
    FullChainRun,
    FullChainStageRun,
    OkxDemoAccountSnapshot,
    OkxDemoExchangeEvent,
    OkxDemoFillSnapshot,
    OkxDemoOrderSnapshot,
    OkxDemoPositionSnapshot,
    OkxDemoReconciliationState,
    OkxDemoRecoveryGrant,
    OkxOrderWriterLease,
    ReconciliationRun,
)


OKX_DEMO_TARGET_ID = "OKX_DEMO"
DEFAULT_RETAIN_GENERATIONS = 100
ADVISORY_LOCK_KEY = 524_202_607_29


class ReconciliationCompactionBlocked(RuntimeError):
    """Raised when maintenance cannot prove that a delete is safe."""


@dataclass(frozen=True)
class ReconciliationCompactionPlan:
    """A serialisable, reviewable deletion plan; it does not mutate the DB."""

    retain_generations: int
    protected_run_ids: tuple[int, ...]
    protected_event_ids: tuple[int, ...]
    delete_run_ids: tuple[int, ...]
    delete_event_ids: tuple[int, ...]
    delete_snapshot_counts: Mapping[str, int]
    retained_artifact_paths: tuple[str, ...]
    baseline_counts: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        # Historical databases can contain millions of duplicates.  The plan is
        # reviewable by count and bounded samples; dumping every primary key
        # would turn a safe dry-run into another multi-gigabyte artifact.
        return {
            "retain_generations": self.retain_generations,
            "protected_run_count": len(self.protected_run_ids),
            "protected_event_count": len(self.protected_event_ids),
            "delete_run_count": len(self.delete_run_ids),
            "delete_event_count": len(self.delete_event_ids),
            "delete_run_id_sample": list(self.delete_run_ids[:20]),
            "delete_event_id_sample": list(self.delete_event_ids[:20]),
            "delete_snapshot_counts": dict(self.delete_snapshot_counts),
            "retained_artifact_count": len(self.retained_artifact_paths),
            "retained_artifact_sample": list(self.retained_artifact_paths[:20]),
            "baseline_counts": dict(self.baseline_counts),
        }


SNAPSHOT_MODELS = (
    ("order_snapshots", OkxDemoOrderSnapshot),
    ("fill_snapshots", OkxDemoFillSnapshot),
    ("position_snapshots", OkxDemoPositionSnapshot),
    ("account_snapshots", OkxDemoAccountSnapshot),
)


def build_compaction_plan(
    db: Session,
    *,
    retain_generations: int = DEFAULT_RETAIN_GENERATIONS,
) -> ReconciliationCompactionPlan:
    """Build an immutable dry-run plan without deleting a row or artifact."""

    if retain_generations < 1:
        raise ReconciliationCompactionBlocked("retain_generations must be positive")

    runs = list(
        db.scalars(
            select(ReconciliationRun)
            .where(ReconciliationRun.execution_target_id == OKX_DEMO_TARGET_ID)
            .order_by(ReconciliationRun.id.desc())
        )
    )

    retained_generations = {
        int(value)
        for value in db.scalars(
            select(OkxDemoExchangeEvent.stream_generation)
            .where(OkxDemoExchangeEvent.execution_target_id == OKX_DEMO_TARGET_ID)
            .distinct()
            .order_by(OkxDemoExchangeEvent.stream_generation.desc())
            .limit(retain_generations)
        )
    }
    protected_run_ids = _protected_run_ids(db, runs)
    protected_event_ids = {
        int(value)
        for value in db.scalars(
            select(OkxDemoExchangeEvent.database_id).where(
                OkxDemoExchangeEvent.execution_target_id == OKX_DEMO_TARGET_ID,
                OkxDemoExchangeEvent.stream_generation.in_(retained_generations),
            )
        )
    }
    # Retention by generation alone is insufficient after the incremental
    # cutover: a quiet recent window may contain no order/fill snapshots. Keep
    # the newest immutable row for every REST identity as the canonical proof
    # against which older rows are compared.
    protected_event_ids.update(_latest_event_per_identity(db))
    for run in runs:
        if run.id in protected_run_ids:
            protected_event_ids.update(_database_ids(run.database_ids, "exchange_events"))
            protected_event_ids.update(
                _event_ids_for_snapshot_ids(db, run.database_ids)
            )

    # A retained canonical event also retains the run that records it.  Iterate
    # to a reference closure so a remaining run never points at a compacted
    # snapshot/event row.
    changed = True
    while changed:
        changed = False
        for run in runs:
            event_ids = _database_ids(run.database_ids, "exchange_events")
            if run.id not in protected_run_ids and event_ids.intersection(protected_event_ids):
                protected_run_ids.add(run.id)
                changed = True
            if run.id in protected_run_ids:
                before = len(protected_event_ids)
                protected_event_ids.update(event_ids)
                protected_event_ids.update(
                    _event_ids_for_snapshot_ids(db, run.database_ids)
                )
                changed = changed or len(protected_event_ids) != before

    # A candidate must have an identical, later retained REST event.  This is
    # deliberately stricter than comparing exchange IDs or observed timestamps.
    retained_signatures = _event_signatures(db, protected_event_ids)
    delete_event_ids = set()
    candidate_rows = db.execute(
        select(
            OkxDemoExchangeEvent.database_id,
            OkxDemoExchangeEvent.source,
            OkxDemoExchangeEvent.entity_kind,
            OkxDemoExchangeEvent.entity_key,
            OkxDemoExchangeEvent.payload_digest,
        ).where(
            OkxDemoExchangeEvent.execution_target_id == OKX_DEMO_TARGET_ID,
            OkxDemoExchangeEvent.source == "REST",
        )
    )
    for row in candidate_rows.yield_per(10_000):
        if (
            int(row.database_id) not in protected_event_ids
            and _row_signature(row) in retained_signatures
        ):
            delete_event_ids.add(int(row.database_id))

    # A run can be compacted only if every authoritative event it cites is an
    # independently safe duplicate.  Empty or malformed lineage is preserved.
    delete_run_ids = set()
    for run in runs:
        event_ids = _database_ids(run.database_ids, "exchange_events")
        if (
            run.id not in protected_run_ids
            and event_ids
            and event_ids.issubset(delete_event_ids)
        ):
            delete_run_ids.add(run.id)

    # Do not delete an event merely because an old run points at it.  The run
    # must also be removable; this keeps every persisted run internally valid.
    referenced_by_retained_run = set()
    for run in runs:
        if run.id not in delete_run_ids:
            referenced_by_retained_run.update(_database_ids(run.database_ids, "exchange_events"))
            referenced_by_retained_run.update(
                _event_ids_for_snapshot_ids(db, run.database_ids)
            )
    delete_event_ids.difference_update(referenced_by_retained_run)
    delete_run_ids = {
        run_id
        for run_id in delete_run_ids
        if _database_ids(next(run.database_ids for run in runs if run.id == run_id), "exchange_events").issubset(delete_event_ids)
    }

    snapshot_counts = {
        label: _count_snapshots_for_events(db, model, delete_event_ids)
        for label, model in SNAPSHOT_MODELS
    }
    baseline_counts = {
        "reconciliation_runs": len(runs),
        "exchange_events": int(
            db.scalar(select(func.count()).select_from(OkxDemoExchangeEvent)) or 0
        ),
        **{
            label: int(db.scalar(select(func.count()).select_from(model)) or 0)
            for label, model in SNAPSHOT_MODELS
        },
    }
    # Artifacts are evidence files, not a cache.  They intentionally remain on
    # disk even for a compacted, unreferenced historical DB run.
    artifacts = tuple(
        sorted(
            str(run.artifact_path)
            for run in runs
            if run.artifact_path
        )
    )
    return ReconciliationCompactionPlan(
        retain_generations=retain_generations,
        protected_run_ids=tuple(sorted(protected_run_ids)),
        protected_event_ids=tuple(sorted(protected_event_ids)),
        delete_run_ids=tuple(sorted(delete_run_ids)),
        delete_event_ids=tuple(sorted(delete_event_ids)),
        delete_snapshot_counts=snapshot_counts,
        retained_artifact_paths=artifacts,
        baseline_counts=baseline_counts,
    )


def apply_compaction(
    db: Session,
    plan: ReconciliationCompactionPlan,
) -> dict[str, int]:
    """Execute a reviewed plan in one PostgreSQL transaction.

    The caller must create a logical backup and stop the managed runtime first.
    This function takes its own advisory transaction lock and rejects an active
    writer lease, so an accidental concurrent invocation fails closed.
    """

    if db.get_bind().dialect.name != "postgresql":
        raise ReconciliationCompactionBlocked(
            "compaction apply is restricted to the canonical PostgreSQL database"
        )
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
    if _active_writer_lease(db):
        raise ReconciliationCompactionBlocked(
            "OKX_DEMO writer lease is active; stop the managed runtime first"
        )
    current = build_compaction_plan(
        db, retain_generations=plan.retain_generations
    )
    if current != plan:
        raise ReconciliationCompactionBlocked(
            "reconciliation data changed after dry-run; review a fresh plan"
        )

    deleted = {"reconciliation_runs": 0, "exchange_events": 0}
    if plan.delete_run_ids:
        deleted["reconciliation_runs"] = _delete_in_chunks(
            db, ReconciliationRun, ReconciliationRun.id, plan.delete_run_ids
        )
    for label, model in SNAPSHOT_MODELS:
        deleted[label] = _delete_in_chunks(
            db, model, model.event_database_id, plan.delete_event_ids
        )
    if plan.delete_event_ids:
        deleted["exchange_events"] = _delete_in_chunks(
            db,
            OkxDemoExchangeEvent,
            OkxDemoExchangeEvent.database_id,
            plan.delete_event_ids,
        )
    return deleted


def post_compaction_maintenance(db: Session) -> None:
    """Run PostgreSQL statistics/index maintenance after the delete commits."""

    if db.get_bind().dialect.name != "postgresql":
        raise ReconciliationCompactionBlocked("PostgreSQL maintenance is required")
    for table in ("reconciliation_runs", "okx_demo_exchange_events"):
        db.execute(text("ANALYZE {}".format(table)))
        db.execute(text("REINDEX TABLE {}".format(table)))


def verify_post_compaction(db: Session) -> dict[str, Any]:
    """Verify schema plus current reconciliation state after a restart."""

    if db.get_bind().dialect.name != "postgresql":
        raise ReconciliationCompactionBlocked("verification is restricted to PostgreSQL")
    readiness = verify_schema(db.get_bind())
    if not readiness.ready:
        raise ReconciliationCompactionBlocked(
            "schema verification failed: {}".format("; ".join(readiness.problems))
        )
    state = db.scalars(
        select(OkxDemoReconciliationState).where(
            OkxDemoReconciliationState.execution_target_id == OKX_DEMO_TARGET_ID
        )
    ).one_or_none()
    if state is None or state.last_reconciliation_run_id is None:
        raise ReconciliationCompactionBlocked("current reconciliation state is missing")
    run = db.get(ReconciliationRun, state.last_reconciliation_run_id)
    if run is None:
        raise ReconciliationCompactionBlocked("current reconciliation run is missing")
    event_ids = _database_ids(run.database_ids, "exchange_events")
    retained = int(
        db.scalar(
            select(func.count()).select_from(OkxDemoExchangeEvent).where(
                OkxDemoExchangeEvent.database_id.in_(event_ids)
            )
        )
        or 0
    )
    if retained != len(event_ids):
        raise ReconciliationCompactionBlocked("current reconciliation evidence is incomplete")
    return {
        "status": "READY",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "current_reconciliation_run_id": run.id,
        "current_event_count": retained,
    }


def _protected_run_ids(db: Session, runs: Iterable[ReconciliationRun]) -> set[int]:
    result = set()
    result.update(
        int(value)
        for value in db.scalars(
            select(OkxDemoReconciliationState.last_reconciliation_run_id).where(
                OkxDemoReconciliationState.execution_target_id == OKX_DEMO_TARGET_ID,
                OkxDemoReconciliationState.last_reconciliation_run_id.is_not(None),
            )
        )
    )
    # Preserve every recovery lineage, including expired/consumed grants: the
    # writer journal may still need it during an audit or recovery investigation.
    result.update(
        int(value)
        for value in db.scalars(
            select(OkxDemoRecoveryGrant.reconciliation_run_id).where(
                OkxDemoRecoveryGrant.execution_target_id == OKX_DEMO_TARGET_ID
            )
        )
    )
    result.update(
        int(value)
        for value in db.scalars(
            select(FullChainRun.reconciliation_run_id).where(
                FullChainRun.execution_target_id == OKX_DEMO_TARGET_ID,
                FullChainRun.reconciliation_run_id.is_not(None),
            )
        )
    )
    for checkpoint in db.scalars(select(FullChainStageRun.database_ids)):
        result.update(_database_ids(checkpoint, "reconciliation_run_id"))
        result.update(_database_ids(checkpoint, "reconciliation_run"))
    known_ids = {run.id for run in runs}
    return result.intersection(known_ids)


def _event_ids_for_snapshot_ids(db: Session, database_ids: Mapping[str, Any]) -> set[int]:
    result = set()
    labels = {
        "order_snapshots": OkxDemoOrderSnapshot,
        "fill_snapshots": OkxDemoFillSnapshot,
        "position_snapshots": OkxDemoPositionSnapshot,
        "account_snapshots": OkxDemoAccountSnapshot,
    }
    for label, model in labels.items():
        snapshot_ids = _database_ids(database_ids, label)
        if snapshot_ids:
            result.update(
                int(value)
                for value in db.scalars(
                    select(model.event_database_id).where(model.database_id.in_(snapshot_ids))
                )
            )
    return result


def _database_ids(payload: Any, key: str) -> set[int]:
    """Read one lineage key without treating arbitrary JSON values as IDs."""

    if not isinstance(payload, Mapping):
        return set()
    value = payload.get(key)
    values = value if isinstance(value, list) else [value]
    result = set()
    for item in values:
        if isinstance(item, int) and item > 0:
            result.add(item)
    return result


def _event_signatures(
    db: Session, event_ids: set[int]
) -> set[tuple[str, str, str, str]]:
    """Load only index columns, never historical JSON payloads, for planning."""

    result = set()
    for chunk in _chunks(sorted(event_ids)):
        rows = db.execute(
            select(
                OkxDemoExchangeEvent.source,
                OkxDemoExchangeEvent.entity_kind,
                OkxDemoExchangeEvent.entity_key,
                OkxDemoExchangeEvent.payload_digest,
            ).where(OkxDemoExchangeEvent.database_id.in_(chunk))
        )
        result.update(_row_signature(row) for row in rows)
    return result


def _latest_event_per_identity(db: Session) -> set[int]:
    """Return one latest canonical event ID for every immutable REST identity.

    The ordered streaming query is portable to SQLite tests and keeps only the
    roughly two thousand identity keys in memory on the current database.
    """

    result = set()
    previous = None
    rows = db.execute(
        select(
            OkxDemoExchangeEvent.database_id,
            OkxDemoExchangeEvent.source,
            OkxDemoExchangeEvent.entity_kind,
            OkxDemoExchangeEvent.entity_key,
            OkxDemoExchangeEvent.payload_digest,
            OkxDemoExchangeEvent.stream_generation,
            OkxDemoExchangeEvent.observed_at,
        )
        .where(
            OkxDemoExchangeEvent.execution_target_id == OKX_DEMO_TARGET_ID,
            OkxDemoExchangeEvent.source == "REST",
        )
        .order_by(
            OkxDemoExchangeEvent.source,
            OkxDemoExchangeEvent.entity_kind,
            OkxDemoExchangeEvent.entity_key,
            OkxDemoExchangeEvent.payload_digest,
            OkxDemoExchangeEvent.stream_generation.desc(),
            OkxDemoExchangeEvent.observed_at.desc(),
            OkxDemoExchangeEvent.database_id.desc(),
        )
    )
    for row in rows.yield_per(10_000):
        signature = _row_signature(row)
        if signature != previous:
            result.add(int(row.database_id))
            previous = signature
    return result


def _row_signature(row: Any) -> tuple[str, str, str, str]:
    return (row.source, row.entity_kind, row.entity_key, row.payload_digest)


def _chunks(values: list[int], size: int = 10_000) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _count_snapshots_for_events(db: Session, model: Any, event_ids: set[int]) -> int:
    return sum(
        int(
            db.scalar(
                select(func.count()).select_from(model).where(
                    model.event_database_id.in_(chunk)
                )
            )
            or 0
        )
        for chunk in _chunks(sorted(event_ids))
    )


def _delete_in_chunks(db: Session, _model: Any, column: Any, values: Iterable[int]) -> int:
    return sum(
        int(db.execute(delete(_model).where(column.in_(chunk))).rowcount or 0)
        for chunk in _chunks(sorted(values))
    )


def _active_writer_lease(db: Session) -> bool:
    now = datetime.now(timezone.utc)
    return (
        db.scalars(
            select(OkxOrderWriterLease).where(
                OkxOrderWriterLease.execution_target_id == OKX_DEMO_TARGET_ID,
                OkxOrderWriterLease.expires_at > now,
            )
        ).first()
        is not None
    )
