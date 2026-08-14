"""Post-qualification optimization records and controlled-submission boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import (
    OPTIMIZATION_RUNS_TABLE,
    OPTIMIZATION_TRIALS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    STRATEGY_VERSIONS_TABLE,
)
from app.canonical_v13.research_evaluation import gate_optimization


class CanonicalOptimizationBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class OptimizationRunResult:
    optimization_run_id: UUID
    baseline_qualification_decision_id: UUID
    status: str
    request_digest: str
    receipt_digest: str
    repeat_noop: bool


@dataclass(frozen=True)
class OptimizationTrialResult:
    optimization_trial_id: UUID
    optimization_run_id: UUID
    trial_number: int
    request_digest: str
    result_digest: str


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_NON_CANONICAL_OPTIMIZATION_EVIDENCE",
            "optimization evidence must be finite canonical JSON",
        ) from exc
    return sha256(encoded).hexdigest()


def _effective(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _require_canonical(connection: Connection) -> Connection:
    effective = _effective(connection)
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    return effective


def create_optimization_run(
    connection: Connection,
    *,
    baseline_qualification_decision_id: UUID | None,
    actor_identity: str,
    objective_json: Mapping[str, object],
) -> OptimizationRunResult:
    effective = _require_canonical(connection)
    gate = gate_optimization(
        effective,
        baseline_qualification_decision_id=baseline_qualification_decision_id,
    )
    if gate.status != "READY" or baseline_qualification_decision_id is None:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_QUALIFIED_BASELINE_REQUIRED", gate.reason_code
        )
    if (
        not actor_identity
        or actor_identity != actor_identity.strip()
        or not objective_json
    ):
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_REQUEST_UNSET", "actor and objectives are required"
        )
    decision = effective.execute(
        select(QUALIFICATION_DECISIONS_TABLE).where(
            QUALIFICATION_DECISIONS_TABLE.c.id
            == baseline_qualification_decision_id
        )
    ).mappings().one()
    request = {
        "contract": "canonical-v13-optimization-run-v1",
        "baseline_qualification_decision_id": str(
            baseline_qualification_decision_id
        ),
        "lineage": {
            key: str(decision[key])
            for key in (
                "strategy_version_id",
                "research_target_id",
                "configuration_bundle_id",
                "market_snapshot_id",
                "validation_plan_id",
            )
        },
        "lineage_digests": {
            key: decision[key]
            for key in (
                "configuration_bundle_digest",
                "market_snapshot_digest",
                "validation_plan_digest",
            )
        },
        "actor_identity": actor_identity,
        "objective_json": dict(objective_json),
    }
    request_digest = _digest(request)
    existing = effective.execute(
        select(OPTIMIZATION_RUNS_TABLE).where(
            OPTIMIZATION_RUNS_TABLE.c.baseline_qualification_decision_id
            == baseline_qualification_decision_id,
            OPTIMIZATION_RUNS_TABLE.c.request_digest == request_digest,
        )
    ).mappings().one_or_none()
    if existing is not None:
        return OptimizationRunResult(
            optimization_run_id=existing["id"],
            baseline_qualification_decision_id=baseline_qualification_decision_id,
            status=existing["status"],
            request_digest=request_digest,
            receipt_digest=existing["receipt_digest"],
            repeat_noop=True,
        )
    run_id = uuid4()
    receipt_digest = _digest(
        {"optimization_run_id": str(run_id), "request_digest": request_digest}
    )
    effective.execute(
        OPTIMIZATION_RUNS_TABLE.insert().values(
            id=run_id,
            baseline_qualification_decision_id=baseline_qualification_decision_id,
            status="NOT_STARTED",
            actor_identity=actor_identity,
            objective_json=dict(objective_json),
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            created_at=datetime.now(timezone.utc),
            completed_at=None,
        )
    )
    return OptimizationRunResult(
        optimization_run_id=run_id,
        baseline_qualification_decision_id=baseline_qualification_decision_id,
        status="NOT_STARTED",
        request_digest=request_digest,
        receipt_digest=receipt_digest,
        repeat_noop=False,
    )


def record_isolated_optimization_trial(
    connection: Connection,
    *,
    optimization_run_id: UUID,
    trial_number: int,
    actor_identity: str,
    parameters_json: Mapping[str, object],
    metrics_json: Mapping[str, object],
) -> OptimizationTrialResult:
    effective = _require_canonical(connection)
    run = effective.execute(
        select(OPTIMIZATION_RUNS_TABLE).where(
            OPTIMIZATION_RUNS_TABLE.c.id == optimization_run_id
        )
    ).mappings().one_or_none()
    if run is None or run["status"] not in {"NOT_STARTED", "RUNNING"}:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_RUN_NOT_WRITABLE", str(optimization_run_id)
        )
    if (
        trial_number <= 0
        or not actor_identity
        or actor_identity != actor_identity.strip()
        or not parameters_json
        or not metrics_json
    ):
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_TRIAL_UNSET", "trial inputs must be explicit"
        )
    if effective.execute(
        select(OPTIMIZATION_TRIALS_TABLE.c.id).where(
            OPTIMIZATION_TRIALS_TABLE.c.optimization_run_id == optimization_run_id,
            OPTIMIZATION_TRIALS_TABLE.c.trial_number == trial_number,
        )
    ).scalar_one_or_none() is not None:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_TRIAL_REWRITE", "trial is immutable"
        )
    trial_id = uuid4()
    request = {
        "optimization_run_id": str(optimization_run_id),
        "run_request_digest": run["request_digest"],
        "trial_number": trial_number,
        "actor_identity": actor_identity,
        "parameters_json": dict(parameters_json),
        "environment_class": "ISOLATED_TEST",
    }
    request_digest = _digest(request)
    result_digest = _digest({**request, "metrics_json": dict(metrics_json)})
    effective.execute(
        OPTIMIZATION_TRIALS_TABLE.insert().values(
            id=trial_id,
            optimization_run_id=optimization_run_id,
            trial_number=trial_number,
            actor_identity=actor_identity,
            environment_class="ISOLATED_TEST",
            parameters_json=dict(parameters_json),
            metrics_json=dict(metrics_json),
            request_digest=request_digest,
            result_digest=result_digest,
            submitted_strategy_version_id=None,
            submission_link_digest=None,
            created_at=datetime.now(timezone.utc),
        )
    )
    effective.execute(
        OPTIMIZATION_RUNS_TABLE.update()
        .where(OPTIMIZATION_RUNS_TABLE.c.id == optimization_run_id)
        .values(status="RUNNING")
    )
    return OptimizationTrialResult(
        optimization_trial_id=trial_id,
        optimization_run_id=optimization_run_id,
        trial_number=trial_number,
        request_digest=request_digest,
        result_digest=result_digest,
    )


def link_controlled_submission_version(
    connection: Connection,
    *,
    optimization_trial_id: UUID,
    submitted_strategy_version_id: UUID,
) -> None:
    """Link only a fresh controlled-submission UNVALIDATED version; never promote it."""

    effective = _require_canonical(connection)
    trial = effective.execute(
        select(OPTIMIZATION_TRIALS_TABLE).where(
            OPTIMIZATION_TRIALS_TABLE.c.id == optimization_trial_id
        )
    ).mappings().one_or_none()
    version = effective.execute(
        select(STRATEGY_VERSIONS_TABLE).where(
            STRATEGY_VERSIONS_TABLE.c.id == submitted_strategy_version_id
        )
    ).mappings().one_or_none()
    run = (
        effective.execute(
            select(OPTIMIZATION_RUNS_TABLE).where(
                OPTIMIZATION_RUNS_TABLE.c.id == trial["optimization_run_id"]
            )
        ).mappings().one()
        if trial is not None
        else None
    )
    baseline = (
        effective.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == run["baseline_qualification_decision_id"]
            )
        ).mappings().one()
        if run is not None
        else None
    )
    if trial is None or trial["submitted_strategy_version_id"] is not None:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_TRIAL_NOT_LINKABLE", str(optimization_trial_id)
        )
    if (
        version is None
        or version["validation_status"] != "UNVALIDATED"
        or version["execution_authorized"] is not False
        or baseline is None
        or version["id"] == baseline["strategy_version_id"]
        or version["created_at"] < trial["created_at"]
    ):
        raise CanonicalOptimizationBlocked(
            "BLOCKED_CONTROLLED_SUBMISSION_REQUIRED",
            "selected trial must link a new UNVALIDATED controlled submission",
        )
    link_digest = _digest(
        {
            "optimization_trial_id": str(optimization_trial_id),
            "trial_result_digest": trial["result_digest"],
            "submitted_strategy_version_id": str(submitted_strategy_version_id),
            "submitted_artifact_id": str(version["artifact_id"]),
            "validation_status": version["validation_status"],
        }
    )
    updated = effective.execute(
        OPTIMIZATION_TRIALS_TABLE.update()
        .where(
            OPTIMIZATION_TRIALS_TABLE.c.id == optimization_trial_id,
            OPTIMIZATION_TRIALS_TABLE.c.submitted_strategy_version_id.is_(None),
        )
        .values(
            submitted_strategy_version_id=submitted_strategy_version_id,
            submission_link_digest=link_digest,
        )
    )
    if updated.rowcount != 1:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_TRIAL_NOT_LINKABLE", str(optimization_trial_id)
        )


__all__ = [
    "CanonicalOptimizationBlocked",
    "OptimizationRunResult",
    "OptimizationTrialResult",
    "create_optimization_run",
    "link_controlled_submission_version",
    "record_isolated_optimization_trial",
]
