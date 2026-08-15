"""Canonical target scoring, qualification, and optimization baseline gates.

The scorer and qualifier are intentionally separate capabilities.  Both consume an
exact completed validation plan and one explicit successful attempt; neither chooses
an attempt, window list, configuration snapshot, or threshold implicitly.  The
optimizer gate is projection-only and never creates an optimization run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
import re
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    P0_CONFIGURATION_KINDS,
)
from app.canonical_v13.models import (
    CONFIGURATION_BUNDLE_MEMBERS_TABLE,
    CONFIGURATION_BUNDLES_TABLE,
    CONFIGURATION_SNAPSHOT_MEMBERS_TABLE,
    CONFIGURATION_SNAPSHOTS_TABLE,
    MARKET_SNAPSHOTS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    QUALIFICATION_WINDOW_EVIDENCE_TABLE,
    RESEARCH_TARGETS_TABLE,
    STRATEGY_VERSIONS_TABLE,
    TARGET_SCORES_TABLE,
    VALIDATION_ATTEMPTS_TABLE,
    VALIDATION_PLANS_TABLE,
    VALIDATION_PLAN_WINDOWS_TABLE,
    VALIDATION_WINDOW_RESULTS_TABLE,
)


SCORER_CAPABILITY: Final = "canonical-v13-target-scorer-v1"
QUALIFIER_CAPABILITY: Final = "canonical-v13-target-qualifier-v1"
OPTIMIZATION_GATE_CAPABILITY: Final = "canonical-v13-optimization-gate-v1"

_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SCORE_QUANTUM = Decimal("0.00000001")
_WEIGHT_TOLERANCE = Decimal("0.000000001")
_OPERATORS = frozenset({">=", ">", "<=", "<", "=="})


class CanonicalEvaluationBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class TargetScoreResult:
    target_score_id: UUID
    validation_plan_id: UUID
    validation_attempt_id: UUID
    scoring_snapshot_id: UUID
    overall_score: Decimal
    required_window_result_set_digest: str
    score_digest: str
    required_window_count: int
    repeat_noop: bool


@dataclass(frozen=True)
class QualificationResult:
    qualification_decision_id: UUID
    target_score_id: UUID
    validation_plan_id: UUID
    validation_attempt_id: UUID
    quality_snapshot_id: UUID
    status: str
    reason_code: str
    decision_digest: str
    evidence_count: int
    repeat_noop: bool


@dataclass(frozen=True)
class OptimizationGateResult:
    status: str
    projection_status: str
    reason_code: str
    baseline_qualification_decision_id: UUID | None


@dataclass(frozen=True)
class _RequiredWindowResult:
    plan_window: Mapping[str, Any]
    result: Mapping[str, Any]


@dataclass(frozen=True)
class _EvaluationContext:
    plan: Mapping[str, Any]
    bundle: Mapping[str, Any]
    snapshots: Mapping[str, Mapping[str, Any]]
    attempt: Mapping[str, Any]
    required_results: tuple[_RequiredWindowResult, ...]


@dataclass(frozen=True)
class _ScoreCalculation:
    overall_score: Decimal
    result_set_digest: str
    required_window_count: int


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_NON_CANONICAL_EVALUATION_EVIDENCE",
            "evaluation evidence must be finite canonical JSON",
        ) from exc


def _digest_json(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _HEX_DIGEST.fullmatch(value):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_DIGEST_UNSET",
            f"{field} must be a lowercase SHA-256 digest",
        )
    return value


def _identity(value: object, *, field: str, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
    ):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_IDENTITY_UNSET", f"{field} is required"
        )
    return value


def _number(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_METRIC_UNSET", f"{field} must be numeric"
        )
    try:
        normalized = Decimal(str(value))
    except InvalidOperation as exc:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_METRIC_INVALID", f"{field} is not numeric"
        ) from exc
    if not normalized.is_finite():
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_METRIC_INVALID", f"{field} must be finite"
        )
    return normalized


def _effective_connection(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _require_canonical(connection: Connection) -> Connection:
    effective = _effective_connection(connection)
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    return effective


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:  # SQLite drops timezone metadata in isolated tests.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: object, *, field: str) -> datetime:
    text = _identity(value, field=field, maximum=64)
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError as exc:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_WINDOW_SNAPSHOT_DRIFT", f"{field} is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_WINDOW_SNAPSHOT_DRIFT", f"{field} has no timezone"
        )
    return parsed.astimezone(timezone.utc)


def _load_snapshot(
    connection: Connection, *, snapshot_id: UUID, expected_kind: str
) -> dict[str, Any]:
    row = connection.execute(
        select(CONFIGURATION_SNAPSHOTS_TABLE).where(
            CONFIGURATION_SNAPSHOTS_TABLE.c.id == snapshot_id
        )
    ).mappings().one_or_none()
    if row is None or row["configuration_kind"] != expected_kind:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_CONFIGURATION_MISMATCH",
            f"{expected_kind} snapshot is missing or has another kind",
        )
    snapshot = dict(row)
    payload = snapshot["snapshot_json"]
    if not isinstance(payload, Mapping):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_SNAPSHOT_DRIFT", "snapshot JSON is not an object"
        )
    payload = dict(payload)
    if (
        payload.get("configuration_kind") != expected_kind
        or payload.get("payload_digest") != snapshot["payload_digest"]
        or not isinstance(payload.get("payload_json"), Mapping)
        or _digest_json(payload["payload_json"]) != snapshot["payload_digest"]
        or _digest_json(payload) != snapshot["snapshot_digest"]
    ):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_SNAPSHOT_DRIFT",
            f"{expected_kind} snapshot content or digest drifted",
        )
    snapshot["snapshot_json"] = payload
    return snapshot


def _load_plan_base(
    connection: Connection, *, validation_plan_id: UUID
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    plan_row = connection.execute(
        select(VALIDATION_PLANS_TABLE).where(
            VALIDATION_PLANS_TABLE.c.id == validation_plan_id
        )
    ).mappings().one_or_none()
    if plan_row is None:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_VALIDATION_PLAN_NOT_FOUND", "validation plan is absent"
        )
    plan = dict(plan_row)
    if plan["status"] != "COMPLETE":
        raise CanonicalEvaluationBlocked(
            "BLOCKED_VALIDATION_PLAN_INCOMPLETE",
            "target evaluation requires a COMPLETE validation plan",
        )
    _require_digest(plan["validation_plan_digest"], field="validation_plan_digest")
    _require_digest(plan["configuration_bundle_digest"], field="bundle_digest")
    _require_digest(plan["market_snapshot_digest"], field="market_snapshot_digest")

    if connection.execute(
        select(STRATEGY_VERSIONS_TABLE.c.id).where(
            STRATEGY_VERSIONS_TABLE.c.id == plan["strategy_version_id"]
        )
    ).scalar_one_or_none() is None:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_STRATEGY_LINEAGE",
            "validation plan strategy version is absent",
        )

    bundle_row = connection.execute(
        select(CONFIGURATION_BUNDLES_TABLE).where(
            CONFIGURATION_BUNDLES_TABLE.c.id == plan["configuration_bundle_id"]
        )
    ).mappings().one_or_none()
    if bundle_row is None or bundle_row["bundle_digest"] != plan["configuration_bundle_digest"]:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_BUNDLE_LINEAGE", "plan bundle lineage drifted"
        )
    bundle = dict(bundle_row)
    capability = bundle["capability_json"]
    if not isinstance(capability, Mapping) or (
        capability.get("trading") != "TRADING_DISABLED"
        or capability.get("exchange_access") != "NONE"
        or capability.get("order_submission") != "DISABLED"
    ):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_CAPABILITY_DRIFT",
            "research bundle is not the frozen no-trade capability",
        )

    market = connection.execute(
        select(MARKET_SNAPSHOTS_TABLE).where(
            MARKET_SNAPSHOTS_TABLE.c.id == plan["market_snapshot_id"]
        )
    ).mappings().one_or_none()
    if (
        market is None
        or bundle["market_snapshot_id"] != plan["market_snapshot_id"]
        or bundle["market_snapshot_digest"] != plan["market_snapshot_digest"]
        or market["snapshot_digest"] != plan["market_snapshot_digest"]
    ):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_MARKET_LINEAGE", "plan market lineage drifted"
        )

    member_rows = connection.execute(
        select(CONFIGURATION_BUNDLE_MEMBERS_TABLE).where(
            CONFIGURATION_BUNDLE_MEMBERS_TABLE.c.configuration_bundle_id
            == bundle["id"]
        )
    ).mappings().all()
    if len(member_rows) != len(P0_CONFIGURATION_KINDS):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_BUNDLE_MEMBER_SET",
            "bundle does not contain the exact seven P0 snapshots",
        )
    by_kind: dict[str, dict[str, Any]] = {}
    for member_row in member_rows:
        member = dict(member_row)
        kind = member["configuration_kind"]
        if kind in by_kind or kind not in P0_CONFIGURATION_KINDS:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_EVALUATION_BUNDLE_MEMBER_SET",
                "bundle P0 snapshot kinds are ambiguous",
            )
        snapshot = _load_snapshot(
            connection,
            snapshot_id=member["configuration_snapshot_id"],
            expected_kind=kind,
        )
        if (
            member["member_key"] != f"{kind}:{snapshot['id']}"
            or member["snapshot_digest"] != snapshot["snapshot_digest"]
        ):
            raise CanonicalEvaluationBlocked(
                "BLOCKED_EVALUATION_BUNDLE_MEMBER_DRIFT",
                f"{kind} bundle member drifted",
            )
        by_kind[kind] = snapshot
    if set(by_kind) != set(P0_CONFIGURATION_KINDS):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_BUNDLE_MEMBER_SET",
            "bundle P0 snapshot set is incomplete",
        )

    target = connection.execute(
        select(RESEARCH_TARGETS_TABLE).where(
            RESEARCH_TARGETS_TABLE.c.id == plan["research_target_id"]
        )
    ).mappings().one_or_none()
    if target is None or target["target_snapshot_id"] != by_kind["TARGET"]["id"]:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_TARGET_LINEAGE", "plan target is not in the bundle"
        )
    if plan["window_snapshot_id"] != by_kind["WINDOW"]["id"]:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_WINDOW_LINEAGE",
            "plan window snapshot differs from the bundle",
        )
    return plan, bundle, by_kind


def _load_plan_windows(
    connection: Connection,
    *,
    plan: Mapping[str, Any],
    window_snapshot: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    payload = window_snapshot["snapshot_json"]["payload_json"]
    if set(payload) != {"windows"} or not isinstance(payload["windows"], list):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_WINDOW_SNAPSHOT_DRIFT", "WINDOW payload shape drifted"
        )
    configured: dict[str, dict[str, Any]] = {}
    for raw_window in payload["windows"]:
        if not isinstance(raw_window, Mapping) or set(raw_window) != {
            "window_key",
            "required",
            "start_at",
            "end_at",
            "coverage",
        }:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_WINDOW_SNAPSHOT_DRIFT", "WINDOW member shape drifted"
            )
        window = dict(raw_window)
        key = _identity(window["window_key"], field="window_key", maximum=160)
        if key in configured or not isinstance(window["required"], bool):
            raise CanonicalEvaluationBlocked(
                "BLOCKED_WINDOW_SNAPSHOT_DRIFT", "WINDOW identities are ambiguous"
            )
        start = _parse_timestamp(window["start_at"], field="start_at")
        end = _parse_timestamp(window["end_at"], field="end_at")
        if end <= start:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_WINDOW_SNAPSHOT_DRIFT", "WINDOW interval is invalid"
            )
        configured[key] = window
    if not configured:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_WINDOW_SNAPSHOT_DRIFT", "WINDOW snapshot has no members"
        )

    member_rows = connection.execute(
        select(CONFIGURATION_SNAPSHOT_MEMBERS_TABLE).where(
            CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.configuration_snapshot_id
            == window_snapshot["id"],
            CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.member_key.like("window:%"),
        )
    ).mappings().all()
    members = {row["member_key"]: dict(row) for row in member_rows}
    if len(members) != len(configured):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_WINDOW_SNAPSHOT_DRIFT",
            "WINDOW snapshot members differ from its payload",
        )

    plan_rows = connection.execute(
        select(VALIDATION_PLAN_WINDOWS_TABLE).where(
            VALIDATION_PLAN_WINDOWS_TABLE.c.validation_plan_id == plan["id"]
        )
    ).mappings().all()
    plan_windows = {row["window_key"]: dict(row) for row in plan_rows}
    if len(plan_windows) != len(configured) or set(plan_windows) != set(configured):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_VALIDATION_PLAN_WINDOW_SET",
            "plan must copy the exact dynamic WINDOW member set",
        )

    normalized_rows: list[dict[str, Any]] = []
    for key in sorted(configured):
        configured_window = configured[key]
        snapshot_member = members.get(f"window:{key}")
        plan_window = plan_windows[key]
        expected_member_digest = _digest_json(
            {
                **configured_window,
                "start_at": _parse_timestamp(
                    configured_window["start_at"], field="start_at"
                ).isoformat(),
                "end_at": _parse_timestamp(
                    configured_window["end_at"], field="end_at"
                ).isoformat(),
            }
        )
        if (
            snapshot_member is None
            or snapshot_member["member_digest"] != expected_member_digest
            or plan_window["window_snapshot_member_id"] != snapshot_member["id"]
            or plan_window["window_member_digest"] != expected_member_digest
            or plan_window["required"] is not configured_window["required"]
            or _utc_iso(plan_window["window_start"])
            != _parse_timestamp(configured_window["start_at"], field="start_at").isoformat()
            or _utc_iso(plan_window["window_end"])
            != _parse_timestamp(configured_window["end_at"], field="end_at").isoformat()
        ):
            raise CanonicalEvaluationBlocked(
                "BLOCKED_VALIDATION_PLAN_WINDOW_DRIFT",
                f"plan window {key} differs from its immutable snapshot member",
            )
        normalized_rows.append(plan_window)
    if not any(row["required"] is True for row in normalized_rows):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_REQUIRED_WINDOW_SET_EMPTY",
            "target evaluation requires at least one required window",
        )
    return tuple(normalized_rows)


def _load_context(
    connection: Connection,
    *,
    validation_plan_id: UUID,
    validation_attempt_id: UUID,
) -> _EvaluationContext:
    plan, bundle, snapshots = _load_plan_base(
        connection, validation_plan_id=validation_plan_id
    )
    windows = _load_plan_windows(
        connection, plan=plan, window_snapshot=snapshots["WINDOW"]
    )
    attempt_row = connection.execute(
        select(VALIDATION_ATTEMPTS_TABLE).where(
            VALIDATION_ATTEMPTS_TABLE.c.id == validation_attempt_id
        )
    ).mappings().one_or_none()
    if (
        attempt_row is None
        or attempt_row["validation_plan_id"] != validation_plan_id
        or attempt_row["status"] != "SUCCEEDED"
    ):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_VALIDATION_ATTEMPT_INCOMPLETE",
            "an explicit SUCCEEDED attempt for the plan is required",
        )
    attempt = dict(attempt_row)
    _require_digest(attempt["request_digest"], field="attempt request_digest")
    _require_digest(attempt["receipt_digest"], field="attempt receipt_digest")
    _require_digest(attempt["executor_image_digest"], field="executor_image_digest")

    result_rows = connection.execute(
        select(VALIDATION_WINDOW_RESULTS_TABLE).where(
            VALIDATION_WINDOW_RESULTS_TABLE.c.validation_attempt_id
            == validation_attempt_id
        )
    ).mappings().all()
    results = {row["validation_plan_window_id"]: dict(row) for row in result_rows}
    plan_window_ids = {row["id"] for row in windows}
    if len(results) != len(result_rows) or set(results) - plan_window_ids:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_VALIDATION_RESULT_LINEAGE",
            "attempt results reference an ambiguous or foreign plan window",
        )
    required_results: list[_RequiredWindowResult] = []
    for window in windows:
        if window["required"] is not True:
            continue
        result = results.get(window["id"])
        if result is None:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_REQUIRED_WINDOW_RESULT_MISSING",
                f"required window {window['window_key']} has no raw result",
            )
        metrics = result["metrics_json"]
        if not isinstance(metrics, Mapping) or (
            _digest_json(metrics) != result["metrics_digest"]
        ):
            raise CanonicalEvaluationBlocked(
                "BLOCKED_VALIDATION_RESULT_DIGEST_DRIFT",
                f"required window {window['window_key']} metrics drifted",
            )
        _require_digest(result["receipt_digest"], field="window result receipt_digest")
        required_results.append(
            _RequiredWindowResult(plan_window=window, result=result)
        )
    return _EvaluationContext(
        plan=plan,
        bundle=bundle,
        snapshots=snapshots,
        attempt=attempt,
        required_results=tuple(required_results),
    )


def _scoring_contract(snapshot: Mapping[str, Any]) -> tuple[str, tuple[dict[str, Any], ...]]:
    payload = snapshot["snapshot_json"]["payload_json"]
    if set(payload) != {"components", "window_aggregation"}:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_SCORING_CONTRACT_DRIFT", "SCORING payload shape drifted"
        )
    aggregation = payload["window_aggregation"]
    if aggregation not in {"MEAN", "MINIMUM", "MAXIMUM"}:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_SCORING_AGGREGATION_UNSET",
            "window_aggregation must be explicit",
        )
    raw_components = payload["components"]
    if not isinstance(raw_components, list) or not raw_components:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_SCORING_COMPONENTS_UNSET", "SCORING components are required"
        )
    components: list[dict[str, Any]] = []
    seen: set[str] = set()
    weight_total = Decimal("0")
    for raw in raw_components:
        if not isinstance(raw, Mapping) or set(raw) != {
            "component_key",
            "metric",
            "weight",
            "direction",
            "minimum",
            "maximum",
        }:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_SCORING_CONTRACT_DRIFT", "scoring component shape drifted"
            )
        component = dict(raw)
        key = _identity(component["component_key"], field="component_key", maximum=160)
        metric = _identity(component["metric"], field="metric", maximum=160)
        weight = _number(component["weight"], field=f"{key}.weight")
        minimum = _number(component["minimum"], field=f"{key}.minimum")
        maximum = _number(component["maximum"], field=f"{key}.maximum")
        if (
            key in seen
            or weight <= 0
            or maximum <= minimum
            or component["direction"] not in {"maximize", "minimize"}
        ):
            raise CanonicalEvaluationBlocked(
                "BLOCKED_SCORING_CONTRACT_DRIFT",
                "scoring keys, weights, direction, or bounds are invalid",
            )
        component.update(
            metric=metric,
            weight=weight,
            minimum=minimum,
            maximum=maximum,
        )
        components.append(component)
        seen.add(key)
        weight_total += weight
    if abs(weight_total - Decimal("1")) > _WEIGHT_TOLERANCE:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_SCORING_WEIGHT_TOTAL",
            "explicit component weights must sum to 1.0",
        )
    return str(aggregation), tuple(components)


def _quality_contract(snapshot: Mapping[str, Any]) -> tuple[Decimal, tuple[dict[str, Any], ...]]:
    payload = snapshot["snapshot_json"]["payload_json"]
    if set(payload) != {"minimum_score", "required_window_gates"}:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_QUALIFICATION_CONTRACT_DRIFT", "QUALITY payload shape drifted"
        )
    minimum_score = _number(payload["minimum_score"], field="minimum_score")
    if minimum_score != Decimal("50"):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_QUALIFICATION_THRESHOLD_UNSET",
            "canonical minimum_score must be explicitly persisted as 50",
        )
    raw_gates = payload["required_window_gates"]
    if not isinstance(raw_gates, list) or not raw_gates:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_QUALIFICATION_GATES_UNSET",
            "required-window hard gates must be explicit",
        )
    gates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_gates:
        if not isinstance(raw, Mapping) or set(raw) != {
            "gate_key",
            "metric",
            "operator",
            "threshold",
        }:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_QUALIFICATION_CONTRACT_DRIFT", "hard-gate shape drifted"
            )
        gate = dict(raw)
        key = _identity(gate["gate_key"], field="gate_key", maximum=160)
        gate["metric"] = _identity(gate["metric"], field="metric", maximum=160)
        gate["threshold"] = _number(gate["threshold"], field=f"{key}.threshold")
        if key in seen or gate["operator"] not in _OPERATORS:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_QUALIFICATION_CONTRACT_DRIFT",
                "hard-gate keys or operators are invalid",
            )
        gates.append(gate)
        seen.add(key)
    return minimum_score, tuple(gates)


def _result_set_payload(context: _EvaluationContext) -> dict[str, Any]:
    return {
        "contract": "canonical-v13-required-window-result-set-v1",
        "validation_plan_id": str(context.plan["id"]),
        "validation_attempt_id": str(context.attempt["id"]),
        "windows": [
            {
                "window_key": item.plan_window["window_key"],
                "window_snapshot_member_id": str(
                    item.plan_window["window_snapshot_member_id"]
                ),
                "window_member_digest": item.plan_window["window_member_digest"],
                "validation_plan_window_id": str(item.plan_window["id"]),
                "validation_window_result_id": str(item.result["id"]),
                "metrics_digest": item.result["metrics_digest"],
                "receipt_digest": item.result["receipt_digest"],
            }
            for item in context.required_results
        ],
    }


def _calculate_score(
    context: _EvaluationContext, scoring_snapshot: Mapping[str, Any]
) -> _ScoreCalculation:
    aggregation, components = _scoring_contract(scoring_snapshot)
    window_scores: list[Decimal] = []
    for item in context.required_results:
        metrics = item.result["metrics_json"]
        window_score = Decimal("0")
        for component in components:
            metric = component["metric"]
            if metric not in metrics:
                raise CanonicalEvaluationBlocked(
                    "BLOCKED_SCORING_METRIC_MISSING",
                    f"{item.plan_window['window_key']} lacks metric {metric}",
                )
            observed = _number(
                metrics[metric],
                field=f"{item.plan_window['window_key']}.{metric}",
            )
            minimum = component["minimum"]
            maximum = component["maximum"]
            if component["direction"] == "maximize":
                normalized = (observed - minimum) / (maximum - minimum)
            else:
                normalized = (maximum - observed) / (maximum - minimum)
            normalized = min(Decimal("1"), max(Decimal("0"), normalized))
            window_score += component["weight"] * normalized
        window_scores.append(window_score * Decimal("100"))
    if aggregation == "MEAN":
        overall = sum(window_scores, Decimal("0")) / Decimal(len(window_scores))
    elif aggregation == "MINIMUM":
        overall = min(window_scores)
    else:
        overall = max(window_scores)
    return _ScoreCalculation(
        overall_score=overall.quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP),
        result_set_digest=_digest_json(_result_set_payload(context)),
        required_window_count=len(context.required_results),
    )


def _lineage_payload(plan: Mapping[str, Any]) -> dict[str, str]:
    return {
        "strategy_version_id": str(plan["strategy_version_id"]),
        "research_target_id": str(plan["research_target_id"]),
        "configuration_bundle_id": str(plan["configuration_bundle_id"]),
        "configuration_bundle_digest": plan["configuration_bundle_digest"],
        "market_snapshot_id": str(plan["market_snapshot_id"]),
        "market_snapshot_digest": plan["market_snapshot_digest"],
        "validation_plan_id": str(plan["id"]),
        "validation_plan_digest": plan["validation_plan_digest"],
    }


def _score_digest(
    *,
    context: _EvaluationContext,
    scoring_snapshot: Mapping[str, Any],
    calculation: _ScoreCalculation,
    scorer_identity: str,
) -> str:
    return _digest_json(
        {
            "contract": "canonical-v13-target-score-v1",
            **_lineage_payload(context.plan),
            "validation_attempt_id": str(context.attempt["id"]),
            "scoring_snapshot_id": str(scoring_snapshot["id"]),
            "scoring_snapshot_digest": scoring_snapshot["snapshot_digest"],
            "required_window_result_set_digest": calculation.result_set_digest,
            "overall_score": format(calculation.overall_score, ".8f"),
            "scorer_identity": scorer_identity,
        }
    )


def _exact_lineage_predicate(table: Any, plan: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        table.c.strategy_version_id == plan["strategy_version_id"],
        table.c.research_target_id == plan["research_target_id"],
        table.c.configuration_bundle_id == plan["configuration_bundle_id"],
        table.c.market_snapshot_id == plan["market_snapshot_id"],
        table.c.validation_plan_id == plan["id"],
    )


def _validate_score_row(
    context: _EvaluationContext, score: Mapping[str, Any]
) -> _ScoreCalculation:
    scoring_snapshot = context.snapshots["SCORING"]
    if score["scoring_snapshot_id"] != scoring_snapshot["id"]:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_SCORE_LINEAGE_DRIFT", "score uses another SCORING snapshot"
        )
    for field in (
        "strategy_version_id",
        "research_target_id",
        "configuration_bundle_id",
        "configuration_bundle_digest",
        "market_snapshot_id",
        "market_snapshot_digest",
        "validation_plan_id",
        "validation_plan_digest",
    ):
        expected = context.plan["id"] if field == "validation_plan_id" else context.plan[field]
        if score[field] != expected:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_SCORE_LINEAGE_DRIFT", f"score {field} drifted"
            )
    calculation = _calculate_score(context, scoring_snapshot)
    scorer_identity = _identity(score["scorer_identity"], field="scorer_identity")
    expected_digest = _score_digest(
        context=context,
        scoring_snapshot=scoring_snapshot,
        calculation=calculation,
        scorer_identity=scorer_identity,
    )
    if (
        Decimal(score["overall_score"]).quantize(_SCORE_QUANTUM)
        != calculation.overall_score
        or score["required_window_result_set_digest"] != calculation.result_set_digest
        or score["score_digest"] != expected_digest
    ):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_SCORE_DIGEST_DRIFT", "target score evidence drifted"
        )
    return calculation


def score_target(
    connection: Connection,
    *,
    validation_plan_id: UUID,
    validation_attempt_id: UUID,
    scorer_identity: str,
) -> TargetScoreResult:
    """Write one immutable target score and no qualification/optimization rows."""

    scorer_identity = _identity(scorer_identity, field="scorer_identity")
    effective = _require_canonical(connection)
    context = _load_context(
        effective,
        validation_plan_id=validation_plan_id,
        validation_attempt_id=validation_attempt_id,
    )
    scoring_snapshot = context.snapshots["SCORING"]
    calculation = _calculate_score(context, scoring_snapshot)
    digest = _score_digest(
        context=context,
        scoring_snapshot=scoring_snapshot,
        calculation=calculation,
        scorer_identity=scorer_identity,
    )
    existing = effective.execute(
        select(TARGET_SCORES_TABLE).where(
            *_exact_lineage_predicate(TARGET_SCORES_TABLE, context.plan)
        )
    ).mappings().one_or_none()
    if existing is not None:
        existing = dict(existing)
        _validate_score_row(context, existing)
        if existing["scorer_identity"] != scorer_identity or existing["score_digest"] != digest:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_SCORE_AUTHORITY_DRIFT",
                "exact lineage already has a score from another authority",
            )
        return TargetScoreResult(
            target_score_id=existing["id"],
            validation_plan_id=validation_plan_id,
            validation_attempt_id=validation_attempt_id,
            scoring_snapshot_id=scoring_snapshot["id"],
            overall_score=calculation.overall_score,
            required_window_result_set_digest=calculation.result_set_digest,
            score_digest=digest,
            required_window_count=calculation.required_window_count,
            repeat_noop=True,
        )

    score_id = uuid4()
    effective.execute(
        TARGET_SCORES_TABLE.insert().values(
            id=score_id,
            strategy_version_id=context.plan["strategy_version_id"],
            research_target_id=context.plan["research_target_id"],
            configuration_bundle_id=context.plan["configuration_bundle_id"],
            configuration_bundle_digest=context.plan["configuration_bundle_digest"],
            market_snapshot_id=context.plan["market_snapshot_id"],
            market_snapshot_digest=context.plan["market_snapshot_digest"],
            validation_plan_id=context.plan["id"],
            validation_plan_digest=context.plan["validation_plan_digest"],
            scoring_snapshot_id=scoring_snapshot["id"],
            overall_score=calculation.overall_score,
            required_window_result_set_digest=calculation.result_set_digest,
            score_digest=digest,
            scorer_identity=scorer_identity,
            created_at=datetime.now(timezone.utc),
        )
    )
    return TargetScoreResult(
        target_score_id=score_id,
        validation_plan_id=validation_plan_id,
        validation_attempt_id=validation_attempt_id,
        scoring_snapshot_id=scoring_snapshot["id"],
        overall_score=calculation.overall_score,
        required_window_result_set_digest=calculation.result_set_digest,
        score_digest=digest,
        required_window_count=calculation.required_window_count,
        repeat_noop=False,
    )


def _operator_passes(observed: Decimal, operator: str, threshold: Decimal) -> bool:
    if operator == ">=":
        return observed >= threshold
    if operator == ">":
        return observed > threshold
    if operator == "<=":
        return observed <= threshold
    if operator == "<":
        return observed < threshold
    return observed == threshold


def _qualification_evidence(
    *,
    context: _EvaluationContext,
    quality_snapshot: Mapping[str, Any],
    gates: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    evidence: list[dict[str, Any]] = []
    for item in context.required_results:
        metrics = item.result["metrics_json"]
        evaluations: list[dict[str, Any]] = []
        for gate in gates:
            metric = gate["metric"]
            if metric not in metrics:
                raise CanonicalEvaluationBlocked(
                    "BLOCKED_QUALIFICATION_METRIC_MISSING",
                    f"{item.plan_window['window_key']} lacks gate metric {metric}",
                )
            observed = _number(
                metrics[metric],
                field=f"{item.plan_window['window_key']}.{metric}",
            )
            passed = _operator_passes(observed, gate["operator"], gate["threshold"])
            evaluations.append(
                {
                    "gate_key": gate["gate_key"],
                    "metric": metric,
                    "operator": gate["operator"],
                    "threshold": str(gate["threshold"]),
                    "observed": str(observed),
                    "passed": passed,
                }
            )
        payload = {
            "contract": "canonical-v13-qualification-window-evidence-v1",
            "validation_plan_id": str(context.plan["id"]),
            "validation_attempt_id": str(context.attempt["id"]),
            "quality_snapshot_id": str(quality_snapshot["id"]),
            "quality_snapshot_digest": quality_snapshot["snapshot_digest"],
            "validation_plan_window_id": str(item.plan_window["id"]),
            "window_snapshot_member_id": str(
                item.plan_window["window_snapshot_member_id"]
            ),
            "window_key": item.plan_window["window_key"],
            "window_member_digest": item.plan_window["window_member_digest"],
            "validation_window_result_id": str(item.result["id"]),
            "metrics_digest": item.result["metrics_digest"],
            "receipt_digest": item.result["receipt_digest"],
            "gates": evaluations,
            "hard_gate_passed": all(item["passed"] for item in evaluations),
        }
        evidence.append(
            {
                "plan_window_id": item.plan_window["id"],
                "result_id": item.result["id"],
                "hard_gate_passed": payload["hard_gate_passed"],
                "payload": payload,
                "digest": _digest_json(payload),
            }
        )
    return tuple(evidence)


def _decision_digest(
    *,
    context: _EvaluationContext,
    score: Mapping[str, Any],
    quality_snapshot: Mapping[str, Any],
    status: str,
    reason_code: str,
    qualifier_identity: str,
    evidence: tuple[dict[str, Any], ...],
) -> str:
    return _digest_json(
        {
            "contract": "canonical-v13-qualification-decision-v1",
            **_lineage_payload(context.plan),
            "validation_attempt_id": str(context.attempt["id"]),
            "target_score_id": str(score["id"]),
            "target_score_digest": score["score_digest"],
            "quality_snapshot_id": str(quality_snapshot["id"]),
            "quality_snapshot_digest": quality_snapshot["snapshot_digest"],
            "status": status,
            "reason_code": reason_code,
            "qualifier_identity": qualifier_identity,
            "evidence_set_digest": _digest_json(
                [item["digest"] for item in evidence]
            ),
        }
    )


def _verify_existing_decision(
    connection: Connection,
    *,
    context: _EvaluationContext,
    decision: Mapping[str, Any],
    score: Mapping[str, Any],
    quality_snapshot: Mapping[str, Any],
    status: str,
    reason_code: str,
    qualifier_identity: str,
    evidence: tuple[dict[str, Any], ...],
) -> None:
    if decision["status"] == "PENDING":
        raise CanonicalEvaluationBlocked(
            "BLOCKED_PERSISTED_QUALIFICATION_PENDING",
            "PENDING is projection-only and cannot be persisted",
        )
    expected_digest = _decision_digest(
        context=context,
        score=score,
        quality_snapshot=quality_snapshot,
        status=status,
        reason_code=reason_code,
        qualifier_identity=qualifier_identity,
        evidence=evidence,
    )
    if (
        decision["target_score_id"] != score["id"]
        or decision["quality_snapshot_id"] != quality_snapshot["id"]
        or decision["status"] != status
        or decision["reason_code"] != reason_code
        or decision["qualifier_identity"] != qualifier_identity
        or decision["decision_digest"] != expected_digest
    ):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_QUALIFICATION_DECISION_DRIFT",
            "existing terminal qualification differs from current evidence",
        )
    rows = connection.execute(
        select(QUALIFICATION_WINDOW_EVIDENCE_TABLE).where(
            QUALIFICATION_WINDOW_EVIDENCE_TABLE.c.qualification_decision_id
            == decision["id"]
        )
    ).mappings().all()
    by_window = {row["validation_plan_window_id"]: dict(row) for row in rows}
    if len(by_window) != len(evidence):
        raise CanonicalEvaluationBlocked(
            "BLOCKED_QUALIFICATION_EVIDENCE_DRIFT",
            "terminal qualification evidence set is incomplete",
        )
    for expected in evidence:
        row = by_window.get(expected["plan_window_id"])
        if (
            row is None
            or row["validation_window_result_id"] != expected["result_id"]
            or row["hard_gate_passed"] is not expected["hard_gate_passed"]
            or row["evidence_json"] != expected["payload"]
            or row["evidence_digest"] != expected["digest"]
        ):
            raise CanonicalEvaluationBlocked(
                "BLOCKED_QUALIFICATION_EVIDENCE_DRIFT",
                "terminal qualification window evidence drifted",
            )


def qualify_target(
    connection: Connection,
    *,
    validation_plan_id: UUID,
    validation_attempt_id: UUID,
    qualifier_identity: str,
) -> QualificationResult:
    """Evaluate hard gates first, then insert one terminal qualification decision."""

    qualifier_identity = _identity(qualifier_identity, field="qualifier_identity")
    effective = _require_canonical(connection)
    context = _load_context(
        effective,
        validation_plan_id=validation_plan_id,
        validation_attempt_id=validation_attempt_id,
    )
    quality_snapshot = context.snapshots["QUALITY_QUALIFICATION"]
    minimum_score, gates = _quality_contract(quality_snapshot)

    # Hard gates are deliberately evaluated before the target-level score is read.
    evidence = _qualification_evidence(
        context=context, quality_snapshot=quality_snapshot, gates=gates
    )
    score_row = effective.execute(
        select(TARGET_SCORES_TABLE).where(
            *_exact_lineage_predicate(TARGET_SCORES_TABLE, context.plan)
        )
    ).mappings().one_or_none()
    if score_row is None:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_TARGET_SCORE_UNSET",
            "exact-lineage target score is required after hard-gate evaluation",
        )
    score = dict(score_row)
    _validate_score_row(context, score)
    if score["scorer_identity"] == qualifier_identity:
        raise CanonicalEvaluationBlocked(
            "BLOCKED_EVALUATION_CAPABILITY_OVERLAP",
            "scorer and qualifier identities must be separate",
        )

    hard_gates_passed = all(item["hard_gate_passed"] for item in evidence)
    overall_score = Decimal(score["overall_score"])
    if not hard_gates_passed:
        status = "REJECTED"
        reason_code = "REQUIRED_WINDOW_GATE_FAILED"
    elif overall_score < minimum_score:
        status = "REJECTED"
        reason_code = "OVERALL_SCORE_BELOW_MINIMUM"
    else:
        status = "QUALIFIED"
        reason_code = "ALL_REQUIRED_WINDOWS_AND_SCORE_PASSED"
    digest = _decision_digest(
        context=context,
        score=score,
        quality_snapshot=quality_snapshot,
        status=status,
        reason_code=reason_code,
        qualifier_identity=qualifier_identity,
        evidence=evidence,
    )
    existing = effective.execute(
        select(QUALIFICATION_DECISIONS_TABLE).where(
            *_exact_lineage_predicate(QUALIFICATION_DECISIONS_TABLE, context.plan)
        )
    ).mappings().one_or_none()
    if existing is not None:
        existing = dict(existing)
        _verify_existing_decision(
            effective,
            context=context,
            decision=existing,
            score=score,
            quality_snapshot=quality_snapshot,
            status=status,
            reason_code=reason_code,
            qualifier_identity=qualifier_identity,
            evidence=evidence,
        )
        return QualificationResult(
            qualification_decision_id=existing["id"],
            target_score_id=score["id"],
            validation_plan_id=validation_plan_id,
            validation_attempt_id=validation_attempt_id,
            quality_snapshot_id=quality_snapshot["id"],
            status=status,
            reason_code=reason_code,
            decision_digest=digest,
            evidence_count=len(evidence),
            repeat_noop=True,
        )

    decision_id = uuid4()
    now = datetime.now(timezone.utc)
    effective.execute(
        QUALIFICATION_DECISIONS_TABLE.insert().values(
            id=decision_id,
            strategy_version_id=context.plan["strategy_version_id"],
            research_target_id=context.plan["research_target_id"],
            configuration_bundle_id=context.plan["configuration_bundle_id"],
            configuration_bundle_digest=context.plan["configuration_bundle_digest"],
            market_snapshot_id=context.plan["market_snapshot_id"],
            market_snapshot_digest=context.plan["market_snapshot_digest"],
            validation_plan_id=context.plan["id"],
            validation_plan_digest=context.plan["validation_plan_digest"],
            target_score_id=score["id"],
            quality_snapshot_id=quality_snapshot["id"],
            status=status,
            reason_code=reason_code,
            decision_digest=digest,
            qualifier_identity=qualifier_identity,
            created_at=now,
        )
    )
    for item in evidence:
        effective.execute(
            QUALIFICATION_WINDOW_EVIDENCE_TABLE.insert().values(
                id=uuid4(),
                qualification_decision_id=decision_id,
                validation_plan_window_id=item["plan_window_id"],
                validation_window_result_id=item["result_id"],
                hard_gate_passed=item["hard_gate_passed"],
                evidence_json=item["payload"],
                evidence_digest=item["digest"],
            )
        )
    return QualificationResult(
        qualification_decision_id=decision_id,
        target_score_id=score["id"],
        validation_plan_id=validation_plan_id,
        validation_attempt_id=validation_attempt_id,
        quality_snapshot_id=quality_snapshot["id"],
        status=status,
        reason_code=reason_code,
        decision_digest=digest,
        evidence_count=len(evidence),
        repeat_noop=False,
    )


def _blocked_optimization(
    reason_code: str, baseline_id: UUID | None
) -> OptimizationGateResult:
    return OptimizationGateResult(
        status="BLOCKED",
        projection_status="PENDING_FIRST_BACKTEST",
        reason_code=reason_code,
        baseline_qualification_decision_id=baseline_id,
    )


def gate_optimization(
    connection: Connection,
    *,
    baseline_qualification_decision_id: UUID | None,
) -> OptimizationGateResult:
    """Read-only baseline gate; it never inserts an optimization run or trial."""

    effective = _require_canonical(connection)
    if baseline_qualification_decision_id is None:
        return _blocked_optimization("QUALIFIED_BASELINE_UNSET", None)
    decision_row = effective.execute(
        select(QUALIFICATION_DECISIONS_TABLE).where(
            QUALIFICATION_DECISIONS_TABLE.c.id
            == baseline_qualification_decision_id
        )
    ).mappings().one_or_none()
    if decision_row is None:
        return _blocked_optimization(
            "QUALIFIED_BASELINE_NOT_FOUND", baseline_qualification_decision_id
        )
    decision = dict(decision_row)
    if decision["status"] != "QUALIFIED":
        return _blocked_optimization(
            "QUALIFIED_BASELINE_REQUIRED", baseline_qualification_decision_id
        )

    try:
        evidence_rows = effective.execute(
            select(QUALIFICATION_WINDOW_EVIDENCE_TABLE).where(
                QUALIFICATION_WINDOW_EVIDENCE_TABLE.c.qualification_decision_id
                == decision["id"]
            )
        ).mappings().all()
        if not evidence_rows or any(row["hard_gate_passed"] is not True for row in evidence_rows):
            raise CanonicalEvaluationBlocked(
                "BLOCKED_QUALIFIED_BASELINE_EVIDENCE",
                "QUALIFIED baseline lacks an all-pass required-window evidence set",
            )
        result_ids = {row["validation_window_result_id"] for row in evidence_rows}
        attempt_ids = set(
            effective.execute(
                select(VALIDATION_WINDOW_RESULTS_TABLE.c.validation_attempt_id).where(
                    VALIDATION_WINDOW_RESULTS_TABLE.c.id.in_(result_ids)
                )
            ).scalars()
        )
        if len(attempt_ids) != 1:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_QUALIFIED_BASELINE_LINEAGE",
                "qualification evidence mixes validation attempts",
            )
        context = _load_context(
            effective,
            validation_plan_id=decision["validation_plan_id"],
            validation_attempt_id=next(iter(attempt_ids)),
        )
        score_row = effective.execute(
            select(TARGET_SCORES_TABLE).where(
                TARGET_SCORES_TABLE.c.id == decision["target_score_id"]
            )
        ).mappings().one_or_none()
        if score_row is None:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_QUALIFIED_BASELINE_SCORE", "baseline score is absent"
            )
        score = dict(score_row)
        _validate_score_row(context, score)
        quality_snapshot = context.snapshots["QUALITY_QUALIFICATION"]
        minimum_score, gates = _quality_contract(quality_snapshot)
        evidence = _qualification_evidence(
            context=context, quality_snapshot=quality_snapshot, gates=gates
        )
        if Decimal(score["overall_score"]) < minimum_score:
            raise CanonicalEvaluationBlocked(
                "BLOCKED_QUALIFIED_BASELINE_SCORE",
                "QUALIFIED baseline score is below its explicit minimum",
            )
        _verify_existing_decision(
            effective,
            context=context,
            decision=decision,
            score=score,
            quality_snapshot=quality_snapshot,
            status="QUALIFIED",
            reason_code="ALL_REQUIRED_WINDOWS_AND_SCORE_PASSED",
            qualifier_identity=decision["qualifier_identity"],
            evidence=evidence,
        )
    except CanonicalEvaluationBlocked as exc:
        return _blocked_optimization(exc.code, baseline_qualification_decision_id)
    return OptimizationGateResult(
        status="READY",
        projection_status="BASELINE_ACCEPTED",
        reason_code="QUALIFIED_BASELINE_ACCEPTED",
        baseline_qualification_decision_id=baseline_qualification_decision_id,
    )


__all__ = [
    "CanonicalEvaluationBlocked",
    "OPTIMIZATION_GATE_CAPABILITY",
    "OptimizationGateResult",
    "QUALIFIER_CAPABILITY",
    "QualificationResult",
    "SCORER_CAPABILITY",
    "TargetScoreResult",
    "gate_optimization",
    "qualify_target",
    "score_target",
]
