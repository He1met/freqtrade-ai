"""Post-qualification optimization records and controlled-submission boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Mapping, Sequence
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
    repeat_noop: bool


@dataclass(frozen=True)
class OptimizationCompletionResult:
    optimization_run_id: UUID
    status: str
    trial_count: int
    selected_trial_numbers: tuple[int, ...]
    terminal_reason_codes: tuple[str, ...]
    result_count: int
    submitted_strategy_count: int
    result_digest: str
    repeat_noop: bool


@dataclass(frozen=True)
class OptimizationSubmissionLinkResult:
    optimization_trial_id: UUID
    submitted_strategy_version_id: UUID
    submission_link_digest: str
    repeat_noop: bool


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


def derive_optimization_terminal_reason_codes(
    *, terminal_status: str, trials: Sequence[Mapping[str, object]]
) -> tuple[str, ...]:
    """Derive terminal reasons only from persisted canonical trial evidence."""

    if terminal_status == "SUCCEEDED":
        return ()
    reasons: set[str] = set()
    for row in trials:
        metrics = row.get("metrics_json")
        if not isinstance(metrics, Mapping):
            raise CanonicalOptimizationBlocked(
                "BLOCKED_OPTIMIZATION_TERMINAL_EVIDENCE_INVALID",
                "trial metrics must be canonical mappings",
            )
        reason = metrics.get("reason_code")
        if reason is not None:
            if not isinstance(reason, str) or re.fullmatch(r"[A-Z0-9_]{1,120}", reason) is None:
                raise CanonicalOptimizationBlocked(
                    "BLOCKED_OPTIMIZATION_TERMINAL_EVIDENCE_INVALID",
                    "trial reason code is not canonical",
                )
            reasons.add(reason)
    if not reasons and trials and all(
        isinstance(row.get("metrics_json"), Mapping)
        and row["metrics_json"].get("eligible") is False  # type: ignore[index]
        and row["metrics_json"].get("selected_finalist") is False  # type: ignore[index]
        for row in trials
    ):
        reasons.add("ZERO_TRAIN_VALIDATION_ELIGIBLE_FINALISTS")
    if not reasons:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_TERMINAL_REASON_UNPROVABLE",
            "blocked optimization requires a reason derivable from canonical trials",
        )
    return tuple(sorted(reasons))


def optimization_selection_digest(
    *,
    optimization_run_id: UUID,
    run_request_digest: str,
    actor_identity: str,
    selected_trial_numbers: Sequence[int],
    trials: Sequence[Mapping[str, object]],
) -> str:
    """Digest selection from result evidence without creating a circular hash."""

    trial_evidence_digests = []
    for row in sorted(trials, key=lambda item: int(item["trial_number"])):
        metrics = dict(row["metrics_json"])  # type: ignore[arg-type]
        metrics.pop("selection_digest", None)
        metrics.pop("selected_finalist", None)
        trial_evidence_digests.append(
            _digest(
                {
                    "trial_number": int(row["trial_number"]),
                    "parameters_json": dict(row["parameters_json"]),  # type: ignore[arg-type]
                    "metrics_json": metrics,
                }
            )
        )
    return _digest(
        {
            "contract": "canonical-v13-optimization-selection-v1",
            "optimization_run_id": str(optimization_run_id),
            "run_request_digest": run_request_digest,
            "actor_identity": actor_identity,
            "selected_trial_numbers": sorted(set(selected_trial_numbers)),
            "trial_evidence_digests": trial_evidence_digests,
        }
    )


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
            trial_count=None,
            result_count=None,
            submitted_strategy_count=None,
            result_digest=None,
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
    if run is None:
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
    existing = effective.execute(
        select(OPTIMIZATION_TRIALS_TABLE).where(
            OPTIMIZATION_TRIALS_TABLE.c.optimization_run_id == optimization_run_id,
            OPTIMIZATION_TRIALS_TABLE.c.trial_number == trial_number,
        )
    ).mappings().one_or_none()
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
    if existing is not None:
        if (
            existing["request_digest"] != request_digest
            or existing["result_digest"] != result_digest
        ):
            raise CanonicalOptimizationBlocked(
                "BLOCKED_OPTIMIZATION_TRIAL_REWRITE", "trial is immutable"
            )
        return OptimizationTrialResult(
            optimization_trial_id=existing["id"],
            optimization_run_id=optimization_run_id,
            trial_number=trial_number,
            request_digest=request_digest,
            result_digest=result_digest,
            repeat_noop=True,
        )
    if run["status"] not in {"NOT_STARTED", "RUNNING"}:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_RUN_NOT_WRITABLE", str(optimization_run_id)
        )
    trial_id = uuid4()
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
        repeat_noop=False,
    )


def complete_optimization_run(
    connection: Connection,
    *,
    optimization_run_id: UUID,
    actor_identity: str,
    selected_trial_numbers: Sequence[int],
    terminal_status: str = "SUCCEEDED",
) -> OptimizationCompletionResult:
    """Terminally seal one bounded run from its immutable trial evidence."""

    effective = _require_canonical(connection)
    run = effective.execute(
        select(OPTIMIZATION_RUNS_TABLE).where(
            OPTIMIZATION_RUNS_TABLE.c.id == optimization_run_id
        )
    ).mappings().one_or_none()
    if run is None:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_RUN_NOT_FOUND", str(optimization_run_id)
        )
    if not actor_identity or actor_identity != actor_identity.strip():
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_REQUEST_UNSET", "actor is required"
        )
    normalized = tuple(sorted(set(selected_trial_numbers)))
    if terminal_status not in {"SUCCEEDED", "BLOCKED"}:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_TERMINAL_STATUS", terminal_status
        )
    if (
        len(normalized) > 3
        or any(number <= 0 for number in normalized)
        or (terminal_status == "SUCCEEDED" and not normalized)
        or (terminal_status == "BLOCKED" and normalized)
    ):
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_SELECTION_INVALID",
            "one to three positive finalist trial numbers are required",
        )
    trials = effective.execute(
        select(OPTIMIZATION_TRIALS_TABLE)
        .where(OPTIMIZATION_TRIALS_TABLE.c.optimization_run_id == optimization_run_id)
        .order_by(OPTIMIZATION_TRIALS_TABLE.c.trial_number)
    ).mappings().all()
    recorded_trial_numbers = {row["trial_number"] for row in trials}
    if not trials or not recorded_trial_numbers.issuperset(normalized):
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_SELECTION_INVALID", "selected trial is absent"
        )
    selection_digest = optimization_selection_digest(
        optimization_run_id=optimization_run_id,
        run_request_digest=run["request_digest"],
        actor_identity=actor_identity,
        selected_trial_numbers=normalized,
        trials=trials,
    )
    selected_rows = {row["trial_number"]: row for row in trials if row["trial_number"] in normalized}
    terminal_reason_codes = derive_optimization_terminal_reason_codes(
        terminal_status=terminal_status,
        trials=trials,
    )
    result_count = sum(1 for row in trials if row["result_digest"] is not None)
    submitted_strategy_count = sum(
        1 for row in trials if row["submitted_strategy_version_id"] is not None
    )
    for number in normalized:
        metrics = selected_rows[number]["metrics_json"]
        if (
            not isinstance(metrics, dict)
            or metrics.get("selected_finalist") is not True
            or metrics.get("selection_digest") != selection_digest
        ):
            raise CanonicalOptimizationBlocked(
                "BLOCKED_OPTIMIZATION_SELECTION_EVIDENCE",
                "selected trials must carry the exact frozen selection digest",
            )
    if terminal_status == "BLOCKED" and any(
        not isinstance(row["metrics_json"], dict)
        or row["metrics_json"].get("selected_finalist") is not False
        or row["metrics_json"].get("selection_digest") != selection_digest
        for row in trials
    ):
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_SELECTION_EVIDENCE",
            "all terminal trials must carry the exact empty-selection digest",
        )
    if run["status"] == terminal_status:
        if (
            run["terminal_reason_codes"] != list(terminal_reason_codes)
            or run["trial_count"] != len(trials)
            or run["result_count"] != result_count
            or run["submitted_strategy_count"] != submitted_strategy_count
            or run["result_digest"] != selection_digest
        ):
            raise CanonicalOptimizationBlocked(
                "BLOCKED_OPTIMIZATION_TERMINAL_REPLAY_DRIFT",
                str(optimization_run_id),
            )
        return OptimizationCompletionResult(
            optimization_run_id=optimization_run_id,
            status=terminal_status,
            trial_count=len(trials),
            selected_trial_numbers=normalized,
            terminal_reason_codes=terminal_reason_codes,
            result_count=result_count,
            submitted_strategy_count=submitted_strategy_count,
            result_digest=selection_digest,
            repeat_noop=True,
        )
    if run["status"] not in {"RUNNING", "NOT_STARTED"}:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_RUN_NOT_WRITABLE", str(optimization_run_id)
        )
    effective.execute(
        OPTIMIZATION_RUNS_TABLE.update()
        .where(
            OPTIMIZATION_RUNS_TABLE.c.id == optimization_run_id,
            OPTIMIZATION_RUNS_TABLE.c.status.in_(("RUNNING", "NOT_STARTED")),
        )
        .values(
            status=terminal_status,
            terminal_reason_codes=list(terminal_reason_codes),
            trial_count=len(trials),
            result_count=result_count,
            submitted_strategy_count=submitted_strategy_count,
            result_digest=selection_digest,
            completed_at=datetime.now(timezone.utc),
        )
    )
    return OptimizationCompletionResult(
        optimization_run_id=optimization_run_id,
        status=terminal_status,
        trial_count=len(trials),
        selected_trial_numbers=normalized,
        terminal_reason_codes=terminal_reason_codes,
        result_count=result_count,
        submitted_strategy_count=submitted_strategy_count,
        result_digest=selection_digest,
        repeat_noop=False,
    )


def link_controlled_submission_version(
    connection: Connection,
    *,
    optimization_trial_id: UUID,
    submitted_strategy_version_id: UUID,
) -> OptimizationSubmissionLinkResult:
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
    if trial is None:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_TRIAL_NOT_LINKABLE", str(optimization_trial_id)
        )
    if trial["submitted_strategy_version_id"] is not None:
        if trial["submitted_strategy_version_id"] != submitted_strategy_version_id:
            raise CanonicalOptimizationBlocked(
                "BLOCKED_OPTIMIZATION_TRIAL_NOT_LINKABLE", str(optimization_trial_id)
            )
        return OptimizationSubmissionLinkResult(
            optimization_trial_id=optimization_trial_id,
            submitted_strategy_version_id=submitted_strategy_version_id,
            submission_link_digest=trial["submission_link_digest"],
            repeat_noop=True,
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
    run_updated = effective.execute(
        OPTIMIZATION_RUNS_TABLE.update()
        .where(
            OPTIMIZATION_RUNS_TABLE.c.id == trial["optimization_run_id"],
            OPTIMIZATION_RUNS_TABLE.c.status.in_(("SUCCEEDED", "BLOCKED")),
        )
        .values(
            submitted_strategy_count=(
                OPTIMIZATION_RUNS_TABLE.c.submitted_strategy_count + 1
            )
        )
    )
    if run["status"] in {"SUCCEEDED", "BLOCKED"} and run_updated.rowcount != 1:
        raise CanonicalOptimizationBlocked(
            "BLOCKED_OPTIMIZATION_SUBMISSION_COUNT_DRIFT",
            str(trial["optimization_run_id"]),
        )
    return OptimizationSubmissionLinkResult(
        optimization_trial_id=optimization_trial_id,
        submitted_strategy_version_id=submitted_strategy_version_id,
        submission_link_digest=link_digest,
        repeat_noop=False,
    )


__all__ = [
    "CanonicalOptimizationBlocked",
    "OptimizationCompletionResult",
    "OptimizationRunResult",
    "OptimizationSubmissionLinkResult",
    "OptimizationTrialResult",
    "complete_optimization_run",
    "create_optimization_run",
    "derive_optimization_terminal_reason_codes",
    "link_controlled_submission_version",
    "optimization_selection_digest",
    "record_isolated_optimization_trial",
]
