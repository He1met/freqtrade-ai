"""Canonical V1.3 P0 configuration control plane.

The caller supplies and owns one SQLAlchemy ``Connection`` transaction.  This module
does not discover databases, activate bundles, or provide any business defaults.  It
creates explicit drafts, validates their dependency graph and kind-specific payload,
then freezes one immutable snapshot plus normalized member rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import Connection, func, select

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    P0_CONFIGURATION_KINDS,
)
from app.canonical_v13.models import (
    CONFIGURATION_DEPENDENCIES_TABLE,
    CONFIGURATION_PROFILES_TABLE,
    CONFIGURATION_SNAPSHOT_MEMBERS_TABLE,
    CONFIGURATION_SNAPSHOTS_TABLE,
    CONFIGURATION_VERSIONS_TABLE,
    RESEARCH_TARGET_ALLOCATIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
)


_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_P0_KIND_SET: Final = frozenset(P0_CONFIGURATION_KINDS)
_AGGREGATE_DEPENDENCY_KINDS: Final = frozenset(
    kind for kind in P0_CONFIGURATION_KINDS if kind != "RESEARCH_AGGREGATE"
)
_DERIVED_TOTAL_KEYS: Final = frozenset(
    {"target_count", "candidate_count", "total_target_count", "total_candidate_count"}
)


class CanonicalControlPlaneBlocked(RuntimeError):
    """Stable fail-closed control-plane error."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ConfigurationDependencyInput:
    version_id: UUID
    expected_kind: str
    relation_key: str


@dataclass(frozen=True)
class ConfigurationDraftResult:
    profile_id: UUID
    version_id: UUID
    version_number: int
    configuration_kind: str
    lifecycle_status: str
    schema_digest: str
    payload_digest: str


@dataclass(frozen=True)
class ConfigurationSnapshotResult:
    snapshot_id: UUID
    version_id: UUID
    configuration_kind: str
    snapshot_digest: str
    dependency_digest: str
    member_count: int
    target_count: int
    total_candidate_count: int
    repeat_noop: bool


@dataclass(frozen=True)
class P0Readiness:
    status: str
    reason_codes: tuple[str, ...]
    snapshot_ids: Mapping[str, UUID]
    target_count: int
    total_candidate_count: int


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
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_NON_CANONICAL_JSON", "configuration contains non-JSON data"
        ) from exc


def canonical_digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identity(value: str, *, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_INVALID_CONFIGURATION_ENVELOPE", f"{field} is invalid"
        )
    return value


def _kind(value: str) -> str:
    if value not in _P0_KIND_SET:
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_CONFIGURATION_KIND", f"unknown P0 configuration kind {value!r}"
        )
    return value


def _digest(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _HEX_DIGEST.fullmatch(value):
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_INVALID_CONFIGURATION_ENVELOPE",
            f"{field} must be lowercase SHA-256",
        )
    return value


def _effective_connection(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _require_canonical_database(connection: Connection) -> Connection:
    effective = _effective_connection(connection)
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    return effective


def _mapping(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_INVALID_CONFIGURATION_ENVELOPE", f"{field} must be an object"
        )
    copied = dict(value)
    _canonical_json(copied)
    return copied


def _version_context(connection: Connection, version_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        select(
            CONFIGURATION_VERSIONS_TABLE,
            CONFIGURATION_PROFILES_TABLE.c.configuration_kind,
            CONFIGURATION_PROFILES_TABLE.c.scope_key,
            CONFIGURATION_PROFILES_TABLE.c.workflow_key,
            CONFIGURATION_PROFILES_TABLE.c.profile_key,
        ).join(
            CONFIGURATION_PROFILES_TABLE,
            CONFIGURATION_PROFILES_TABLE.c.id
            == CONFIGURATION_VERSIONS_TABLE.c.profile_id,
        ).where(CONFIGURATION_VERSIONS_TABLE.c.id == version_id)
    ).mappings().one_or_none()
    if row is None:
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_CONFIGURATION_VERSION_NOT_FOUND", "configuration version is absent"
        )
    return dict(row)


def create_configuration_draft(
    connection: Connection,
    *,
    profile_key: str,
    configuration_kind: str,
    scope_key: str,
    workflow_key: str,
    schema_json: Mapping[str, Any],
    payload_json: Mapping[str, Any],
    adapter_identity: str,
    adapter_digest: str,
    dependencies: Sequence[ConfigurationDependencyInput] = (),
) -> ConfigurationDraftResult:
    """Create one explicit DRAFT; no target, count, cap, or profile is inferred."""

    profile_key = _identity(profile_key, field="profile_key", maximum=160)
    configuration_kind = _kind(configuration_kind)
    scope_key = _identity(scope_key, field="scope_key", maximum=200)
    workflow_key = _identity(workflow_key, field="workflow_key", maximum=160)
    adapter_identity = _identity(
        adapter_identity, field="adapter_identity", maximum=200
    )
    adapter_digest = _digest(adapter_digest, field="adapter_digest")
    schema = _mapping(schema_json, field="schema_json")
    payload = _mapping(payload_json, field="payload_json")
    if _DERIVED_TOTAL_KEYS.intersection(payload):
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_DERIVED_TOTAL_PERSISTENCE",
            "target/candidate totals are derived and cannot be configuration facts",
        )

    effective = _require_canonical_database(connection)
    normalized_dependencies: list[tuple[ConfigurationDependencyInput, dict[str, Any]]] = []
    seen_relations: set[str] = set()
    for dependency in dependencies:
        relation_key = _identity(
            dependency.relation_key, field="relation_key", maximum=120
        )
        expected_kind = _kind(dependency.expected_kind)
        if relation_key in seen_relations:
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_DUPLICATE_DEPENDENCY", "dependency relation keys must be unique"
            )
        context = _version_context(effective, dependency.version_id)
        if context["configuration_kind"] != expected_kind:
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_DEPENDENCY_TYPE_MISMATCH",
                f"{relation_key} expected {expected_kind}, observed "
                f"{context['configuration_kind']}",
            )
        seen_relations.add(relation_key)
        normalized_dependencies.append((dependency, context))

    profile_statement = select(CONFIGURATION_PROFILES_TABLE).where(
            CONFIGURATION_PROFILES_TABLE.c.profile_key == profile_key
        )
    if effective.dialect.name != "sqlite":
        profile_statement = profile_statement.with_for_update()
    existing_profile = effective.execute(profile_statement).mappings().one_or_none()
    now = datetime.now(timezone.utc)
    if existing_profile is None:
        profile_id = uuid4()
        effective.execute(
            CONFIGURATION_PROFILES_TABLE.insert().values(
                id=profile_id,
                profile_key=profile_key,
                configuration_kind=configuration_kind,
                scope_key=scope_key,
                workflow_key=workflow_key,
                created_at=now,
            )
        )
        version_number = 1
    else:
        profile = dict(existing_profile)
        if (
            profile["configuration_kind"] != configuration_kind
            or profile["scope_key"] != scope_key
            or profile["workflow_key"] != workflow_key
        ):
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_PROFILE_IDENTITY_DRIFT",
                "profile key is bound to another kind, scope, or workflow",
            )
        profile_id = profile["id"]
        version_number = int(
            effective.execute(
                select(func.max(CONFIGURATION_VERSIONS_TABLE.c.version_number)).where(
                    CONFIGURATION_VERSIONS_TABLE.c.profile_id == profile_id
                )
            ).scalar_one()
            or 0
        ) + 1

    version_id = uuid4()
    schema_digest = canonical_digest(schema)
    payload_digest = canonical_digest(payload)
    effective.execute(
        CONFIGURATION_VERSIONS_TABLE.insert().values(
            id=version_id,
            profile_id=profile_id,
            version_number=version_number,
            lifecycle_status="DRAFT",
            schema_json=schema,
            payload_json=payload,
            schema_digest=schema_digest,
            payload_digest=payload_digest,
            adapter_identity=adapter_identity,
            adapter_digest=adapter_digest,
            created_at=now,
            validated_at=None,
            retired_at=None,
        )
    )
    for dependency, _context in normalized_dependencies:
        effective.execute(
            CONFIGURATION_DEPENDENCIES_TABLE.insert().values(
                id=uuid4(),
                configuration_version_id=version_id,
                depends_on_version_id=dependency.version_id,
                relation_key=dependency.relation_key,
            )
        )
    return ConfigurationDraftResult(
        profile_id=profile_id,
        version_id=version_id,
        version_number=version_number,
        configuration_kind=configuration_kind,
        lifecycle_status="DRAFT",
        schema_digest=schema_digest,
        payload_digest=payload_digest,
    )


def _dependency_rows(connection: Connection, version_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        select(
            CONFIGURATION_DEPENDENCIES_TABLE.c.depends_on_version_id,
            CONFIGURATION_DEPENDENCIES_TABLE.c.relation_key,
            CONFIGURATION_PROFILES_TABLE.c.configuration_kind,
            CONFIGURATION_SNAPSHOTS_TABLE.c.id.label("snapshot_id"),
            CONFIGURATION_SNAPSHOTS_TABLE.c.snapshot_digest,
        )
        .join(
            CONFIGURATION_VERSIONS_TABLE,
            CONFIGURATION_VERSIONS_TABLE.c.id
            == CONFIGURATION_DEPENDENCIES_TABLE.c.depends_on_version_id,
        )
        .join(
            CONFIGURATION_PROFILES_TABLE,
            CONFIGURATION_PROFILES_TABLE.c.id
            == CONFIGURATION_VERSIONS_TABLE.c.profile_id,
        )
        .outerjoin(
            CONFIGURATION_SNAPSHOTS_TABLE,
            CONFIGURATION_SNAPSHOTS_TABLE.c.configuration_version_id
            == CONFIGURATION_DEPENDENCIES_TABLE.c.depends_on_version_id,
        )
        .where(
            CONFIGURATION_DEPENDENCIES_TABLE.c.configuration_version_id == version_id
        )
        .order_by(CONFIGURATION_DEPENDENCIES_TABLE.c.relation_key)
    ).mappings().all()
    return [dict(row) for row in rows]


def _assert_acyclic(connection: Connection) -> None:
    edges: dict[UUID, list[UUID]] = {}
    for parent, child in connection.execute(
        select(
            CONFIGURATION_DEPENDENCIES_TABLE.c.configuration_version_id,
            CONFIGURATION_DEPENDENCIES_TABLE.c.depends_on_version_id,
        )
    ):
        edges.setdefault(parent, []).append(child)
    visiting: set[UUID] = set()
    visited: set[UUID] = set()

    def visit(node: UUID) -> None:
        if node in visiting:
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_DEPENDENCY_CYCLE", "configuration dependency graph has a cycle"
            )
        if node in visited:
            return
        visiting.add(node)
        for child in edges.get(node, ()):  # pragma: no branch - tiny adjacency loop
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in tuple(edges):
        visit(node)


def _sequence(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_CONFIGURATION_VALUE_UNSET", f"{key} must be a non-empty array"
        )
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_INVALID_CONFIGURATION_PAYLOAD", f"{key} members must be objects"
            )
        result.append(dict(item))
    return result


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
    field: str,
) -> None:
    optional = optional or set()
    observed = set(value)
    if not required.issubset(observed) or observed - required - optional:
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_INVALID_CONFIGURATION_PAYLOAD",
            f"{field} requires {sorted(required)} and permits only {sorted(optional)} extras",
        )


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_CONFIGURATION_VALUE_UNSET", f"{field} must be an explicit number"
        )
    normalized = float(value)
    if not math.isfinite(normalized):
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_INVALID_CONFIGURATION_PAYLOAD", f"{field} must be finite"
        )
    return normalized


def _timestamp(value: object, *, field: str) -> datetime:
    text = _identity(value, field=field, maximum=64)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_INVALID_WINDOW_MEMBER", f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_INVALID_WINDOW_MEMBER", f"{field} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _member(
    connection: Connection,
    *,
    snapshot_id: UUID,
    member_key: str,
    member_identity: str,
    member_value: object,
) -> None:
    connection.execute(
        CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.insert().values(
            id=uuid4(),
            configuration_snapshot_id=snapshot_id,
            member_key=member_key,
            member_identity=member_identity,
            member_digest=canonical_digest(member_value),
        )
    )


def _freeze_kind_members(
    connection: Connection,
    *,
    snapshot_id: UUID,
    kind: str,
    payload: Mapping[str, Any],
    dependencies: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    """Persist normalized members and return derived target/candidate totals."""

    _member(
        connection,
        snapshot_id=snapshot_id,
        member_key=f"profile:{kind.lower()}",
        member_identity=kind,
        member_value=payload,
    )
    if kind == "TARGET":
        _exact_keys(payload, required={"targets"}, field="TARGET payload")
        targets = _sequence(payload, "targets")
        seen: set[str] = set()
        for target in targets:
            _exact_keys(
                target,
                required={"target_key", "instrument", "pair", "timeframe", "data_kind"},
                field="target",
            )
            target_key = _identity(target.get("target_key"), field="target_key", maximum=200)
            if target_key in seen:
                raise CanonicalControlPlaneBlocked(
                    "BLOCKED_DUPLICATE_TARGET", f"duplicate target {target_key}"
                )
            normalized = {
                "target_key": target_key,
                "instrument": _identity(target.get("instrument"), field="instrument", maximum=120),
                "pair": _identity(target.get("pair"), field="pair", maximum=120),
                "timeframe": _identity(target.get("timeframe"), field="timeframe", maximum=32),
                "data_kind": _identity(target.get("data_kind"), field="data_kind", maximum=80),
            }
            digest = canonical_digest(normalized)
            connection.execute(
                RESEARCH_TARGETS_TABLE.insert().values(
                    id=uuid4(), target_snapshot_id=snapshot_id, target_digest=digest, **normalized
                )
            )
            _member(
                connection,
                snapshot_id=snapshot_id,
                member_key=f"target:{target_key}",
                member_identity=target_key,
                member_value=normalized,
            )
            seen.add(target_key)
        return len(targets), 0

    if kind == "WINDOW":
        _exact_keys(payload, required={"windows"}, field="WINDOW payload")
        windows = _sequence(payload, "windows")
        seen: set[str] = set()
        for window in windows:
            _exact_keys(
                window,
                required={"window_key", "required", "start_at", "end_at", "coverage"},
                field="window",
            )
            window_key = _identity(
                window.get("window_key"), field="window_key", maximum=160
            )
            coverage = _mapping(window.get("coverage"), field="coverage")
            _exact_keys(
                coverage,
                required={"minimum_closed_candles"},
                field="window.coverage",
            )
            minimum_closed_candles = coverage["minimum_closed_candles"]
            start_at = _timestamp(window.get("start_at"), field="start_at")
            end_at = _timestamp(window.get("end_at"), field="end_at")
            if (
                window_key in seen
                or not isinstance(window.get("required"), bool)
                or isinstance(minimum_closed_candles, bool)
                or not isinstance(minimum_closed_candles, int)
                or minimum_closed_candles <= 0
                or end_at <= start_at
            ):
                raise CanonicalControlPlaneBlocked(
                    "BLOCKED_INVALID_WINDOW_MEMBER",
                    "window identity, order, required flag, and coverage must be explicit",
                )
            normalized_window = {
                "window_key": window_key,
                "required": window["required"],
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "coverage": {"minimum_closed_candles": minimum_closed_candles},
            }
            _member(
                connection,
                snapshot_id=snapshot_id,
                member_key=f"window:{window_key}",
                member_identity=(
                    f"{window_key}:required={str(window['required']).lower()}"
                ),
                member_value=normalized_window,
            )
            seen.add(window_key)
        return 0, 0

    if kind == "GENERATION":
        _exact_keys(
            payload,
            required={"allocations"},
            optional={"provider"},
            field="GENERATION payload",
        )
        if len(dependencies) != 1 or dependencies[0]["configuration_kind"] != "TARGET":
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_GENERATION_TARGET_DEPENDENCY",
                "GENERATION requires exactly one frozen TARGET dependency",
            )
        target_snapshot_id = dependencies[0]["snapshot_id"]
        targets = {
            row.target_key: row.id
            for row in connection.execute(
                select(RESEARCH_TARGETS_TABLE.c.id, RESEARCH_TARGETS_TABLE.c.target_key).where(
                    RESEARCH_TARGETS_TABLE.c.target_snapshot_id == target_snapshot_id
                )
            )
        }
        allocations = _sequence(payload, "allocations")
        by_key: dict[str, dict[str, Any]] = {}
        for allocation in allocations:
            target_key = _identity(
                allocation.get("target_key"), field="target_key", maximum=200
            )
            count = allocation.get("allocation_count")
            cap = allocation.get("candidate_cap")
            if (
                target_key in by_key
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count <= 0
                or isinstance(cap, bool)
                or not isinstance(cap, int)
                or cap < count
            ):
                raise CanonicalControlPlaneBlocked(
                    "BLOCKED_ALLOCATION_OR_CAP_UNSET",
                    "each target needs one positive allocation and explicit cap >= allocation",
                )
            by_key[target_key] = {
                "target_key": target_key,
                "allocation_count": count,
                "candidate_cap": cap,
            }
        if set(by_key) != set(targets):
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_TARGET_ALLOCATION_MISMATCH",
                "allocation target keys must exactly equal frozen TARGET members",
            )
        for target_key in sorted(by_key):
            allocation = by_key[target_key]
            digest = canonical_digest(allocation)
            connection.execute(
                RESEARCH_TARGET_ALLOCATIONS_TABLE.insert().values(
                    id=uuid4(),
                    generation_snapshot_id=snapshot_id,
                    research_target_id=targets[target_key],
                    allocation_count=allocation["allocation_count"],
                    candidate_cap=allocation["candidate_cap"],
                    allocation_digest=digest,
                )
            )
            _member(
                connection,
                snapshot_id=snapshot_id,
                member_key=f"allocation:{target_key}",
                member_identity=target_key,
                member_value=allocation,
            )
        # Target cardinality belongs to the TARGET snapshot.  The GENERATION
        # snapshot exposes only its independently derived candidate total.
        return 0, sum(item["allocation_count"] for item in by_key.values())

    if kind == "DIVERSITY":
        _exact_keys(payload, required={"rules"}, field="DIVERSITY payload")
        rules = _sequence(payload, "rules")
        seen: set[str] = set()
        for rule in rules:
            _exact_keys(
                rule,
                required={"rule_key", "algorithm", "metric", "operator", "threshold"},
                field="diversity rule",
            )
            rule_key = _identity(rule.get("rule_key"), field="rule_key", maximum=160)
            algorithm = _identity(rule.get("algorithm"), field="algorithm", maximum=160)
            metric = _identity(rule.get("metric"), field="metric", maximum=160)
            operator = rule.get("operator")
            threshold = _number(rule.get("threshold"), field="threshold")
            if (
                rule_key in seen
                or operator not in {">=", ">", "<=", "<", "=="}
            ):
                raise CanonicalControlPlaneBlocked(
                    "BLOCKED_INVALID_DIVERSITY_RULE",
                    "each unique rule requires algorithm, metric, operator, and threshold",
                )
            normalized_rule = {
                "rule_key": rule_key,
                "algorithm": algorithm,
                "metric": metric,
                "operator": operator,
                "threshold": threshold,
            }
            _member(
                connection,
                snapshot_id=snapshot_id,
                member_key=f"diversity:{rule_key}",
                member_identity=f"{algorithm}:{metric}",
                member_value=normalized_rule,
            )
            seen.add(rule_key)
        return 0, 0

    if kind == "QUALITY_QUALIFICATION":
        _exact_keys(
            payload,
            required={"minimum_score", "required_window_gates"},
            field="QUALITY_QUALIFICATION payload",
        )
        minimum_score = _number(payload.get("minimum_score"), field="minimum_score")
        if minimum_score != 50:
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_INVALID_QUALIFICATION_THRESHOLD",
                "canonical target-level minimum_score must be explicitly persisted as 50",
            )
        _member(
            connection,
            snapshot_id=snapshot_id,
            member_key="qualification:minimum_score",
            member_identity="target-level-overall-score",
            member_value={"minimum_score": minimum_score},
        )
        gates = _sequence(payload, "required_window_gates")
        seen: set[str] = set()
        for gate in gates:
            _exact_keys(
                gate,
                required={"gate_key", "metric", "operator", "threshold"},
                field="required window gate",
            )
            gate_key = _identity(gate.get("gate_key"), field="gate_key", maximum=160)
            metric = _identity(gate.get("metric"), field="metric", maximum=160)
            operator = gate.get("operator")
            threshold = _number(gate.get("threshold"), field="threshold")
            if gate_key in seen or operator not in {">=", ">", "<=", "<", "=="}:
                raise CanonicalControlPlaneBlocked(
                    "BLOCKED_INVALID_QUALIFICATION_GATE",
                    "gate keys must be unique and operator must be explicit",
                )
            normalized_gate = {
                "gate_key": gate_key,
                "metric": metric,
                "operator": operator,
                "threshold": threshold,
            }
            _member(
                connection,
                snapshot_id=snapshot_id,
                member_key=f"qualification_gate:{gate_key}",
                member_identity=metric,
                member_value=normalized_gate,
            )
            seen.add(gate_key)
        return 0, 0

    if kind == "SCORING":
        _exact_keys(
            payload,
            required={"components", "window_aggregation"},
            field="SCORING payload",
        )
        window_aggregation = payload.get("window_aggregation")
        if window_aggregation not in {"MEAN", "MINIMUM", "MAXIMUM"}:
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_SCORING_AGGREGATION_UNSET",
                "window_aggregation must be explicitly MEAN, MINIMUM, or MAXIMUM",
            )
        _member(
            connection,
            snapshot_id=snapshot_id,
            member_key="scoring:window_aggregation",
            member_identity=str(window_aggregation),
            member_value={"window_aggregation": window_aggregation},
        )
        components = _sequence(payload, "components")
        seen: set[str] = set()
        weight_total = 0.0
        for component in components:
            _exact_keys(
                component,
                required={
                    "component_key",
                    "metric",
                    "weight",
                    "direction",
                    "minimum",
                    "maximum",
                },
                field="scoring component",
            )
            component_key = _identity(
                component.get("component_key"), field="component_key", maximum=160
            )
            metric = _identity(component.get("metric"), field="metric", maximum=160)
            weight = _number(component.get("weight"), field="weight")
            direction = component.get("direction")
            minimum = _number(component.get("minimum"), field="minimum")
            maximum = _number(component.get("maximum"), field="maximum")
            if (
                component_key in seen
                or weight <= 0
                or direction not in {"maximize", "minimize"}
                or maximum <= minimum
            ):
                raise CanonicalControlPlaneBlocked(
                    "BLOCKED_INVALID_SCORING_COMPONENT",
                    "component key, weight, direction, and ordered normalization bounds are required",
                )
            normalized_component = {
                "component_key": component_key,
                "metric": metric,
                "weight": weight,
                "direction": direction,
                "minimum": minimum,
                "maximum": maximum,
            }
            _member(
                connection,
                snapshot_id=snapshot_id,
                member_key=f"score_component:{component_key}",
                member_identity=metric,
                member_value=normalized_component,
            )
            seen.add(component_key)
            weight_total += weight
        if not math.isclose(weight_total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_SCORING_WEIGHT_TOTAL",
                "explicit scoring weights must sum to 1.0",
            )
        return 0, 0

    if kind == "RESEARCH_AGGREGATE":
        _exact_keys(
            payload,
            required={"assembly_key"},
            field="RESEARCH_AGGREGATE payload",
        )
        _identity(payload.get("assembly_key"), field="assembly_key", maximum=160)
        for dependency in dependencies:
            dependency_kind = dependency["configuration_kind"]
            _member(
                connection,
                snapshot_id=snapshot_id,
                member_key=f"snapshot:{dependency_kind.lower()}",
                member_identity=str(dependency["snapshot_id"]),
                member_value={
                    "configuration_kind": dependency_kind,
                    "snapshot_id": str(dependency["snapshot_id"]),
                    "snapshot_digest": dependency["snapshot_digest"],
                },
            )
    return 0, 0


def _snapshot_result(
    connection: Connection, snapshot: Mapping[str, Any], *, repeat_noop: bool
) -> ConfigurationSnapshotResult:
    snapshot_id = snapshot["id"]
    kind = snapshot["configuration_kind"]
    member_count = int(
        connection.execute(
            select(func.count()).select_from(CONFIGURATION_SNAPSHOT_MEMBERS_TABLE).where(
                CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.configuration_snapshot_id
                == snapshot_id
            )
        ).scalar_one()
    )
    target_count = int(
        connection.execute(
            select(func.count()).select_from(RESEARCH_TARGETS_TABLE).where(
                RESEARCH_TARGETS_TABLE.c.target_snapshot_id == snapshot_id
            )
        ).scalar_one()
    )
    total = int(
        connection.execute(
            select(
                func.coalesce(
                    func.sum(RESEARCH_TARGET_ALLOCATIONS_TABLE.c.allocation_count), 0
                )
            ).where(
                RESEARCH_TARGET_ALLOCATIONS_TABLE.c.generation_snapshot_id == snapshot_id
            )
        ).scalar_one()
    )
    return ConfigurationSnapshotResult(
        snapshot_id=snapshot_id,
        version_id=snapshot["configuration_version_id"],
        configuration_kind=kind,
        snapshot_digest=snapshot["snapshot_digest"],
        dependency_digest=snapshot["dependency_digest"],
        member_count=member_count,
        target_count=target_count,
        total_candidate_count=total,
        repeat_noop=repeat_noop,
    )


def validate_configuration_version(
    connection: Connection,
    *,
    version_id: UUID,
    adapter_manifest_digest: str,
) -> ConfigurationSnapshotResult:
    """Validate one draft and atomically freeze its immutable snapshot."""

    adapter_manifest_digest = _digest(
        adapter_manifest_digest, field="adapter_manifest_digest"
    )
    effective = _require_canonical_database(connection)
    context = _version_context(effective, version_id)
    existing = effective.execute(
        select(CONFIGURATION_SNAPSHOTS_TABLE).where(
            CONFIGURATION_SNAPSHOTS_TABLE.c.configuration_version_id == version_id
        )
    ).mappings().one_or_none()
    if existing is not None:
        existing = dict(existing)
        if context["lifecycle_status"] != "VALIDATED":
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_SNAPSHOT_LIFECYCLE_DRIFT",
                "snapshot exists for a non-VALIDATED version",
            )
        if (
            canonical_digest(context["schema_json"]) != context["schema_digest"]
            or canonical_digest(context["payload_json"]) != context["payload_digest"]
            or existing["schema_digest"] != context["schema_digest"]
            or existing["payload_digest"] != context["payload_digest"]
            or canonical_digest(existing["snapshot_json"])
            != existing["snapshot_digest"]
        ):
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_SNAPSHOT_DIGEST_DRIFT",
                "validated version or immutable snapshot content drifted",
            )
        if existing["adapter_manifest_digest"] != adapter_manifest_digest:
            raise CanonicalControlPlaneBlocked(
                "BLOCKED_ADAPTER_MANIFEST_DRIFT",
                "validated snapshot is bound to another adapter manifest",
            )
        return _snapshot_result(effective, existing, repeat_noop=True)
    if context["lifecycle_status"] != "DRAFT":
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_CONFIGURATION_TRANSITION", "only DRAFT can be validated"
        )
    if canonical_digest(context["schema_json"]) != context["schema_digest"] or canonical_digest(
        context["payload_json"]
    ) != context["payload_digest"]:
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_CONFIGURATION_DIGEST_DRIFT", "draft schema or payload digest drifted"
        )

    _assert_acyclic(effective)
    dependencies = _dependency_rows(effective, version_id)
    if any(row["snapshot_id"] is None for row in dependencies):
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_DEPENDENCY_NOT_FROZEN",
            "all dependencies must have immutable validated snapshots",
        )
    kind = context["configuration_kind"]
    dependency_kinds = [row["configuration_kind"] for row in dependencies]
    if kind == "RESEARCH_AGGREGATE" and (
        len(dependency_kinds) != 6
        or set(dependency_kinds) != _AGGREGATE_DEPENDENCY_KINDS
    ):
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_AGGREGATE_DEPENDENCIES",
            "RESEARCH_AGGREGATE requires exactly one snapshot from each prior P0 kind",
        )

    dependency_payload = [
        {
            "relation_key": row["relation_key"],
            "version_id": str(row["depends_on_version_id"]),
            "configuration_kind": row["configuration_kind"],
            "snapshot_id": str(row["snapshot_id"]),
            "snapshot_digest": row["snapshot_digest"],
        }
        for row in dependencies
    ]
    dependency_digest = canonical_digest(dependency_payload)
    snapshot_payload = {
        "contract": "canonical-v13-configuration-snapshot-v1",
        "configuration_kind": kind,
        "profile_key": context["profile_key"],
        "scope_key": context["scope_key"],
        "workflow_key": context["workflow_key"],
        "version_id": str(version_id),
        "version_number": context["version_number"],
        "schema_json": context["schema_json"],
        "payload_json": context["payload_json"],
        "schema_digest": context["schema_digest"],
        "payload_digest": context["payload_digest"],
        "adapter_identity": context["adapter_identity"],
        "adapter_digest": context["adapter_digest"],
        "adapter_manifest_digest": adapter_manifest_digest,
        "dependencies": dependency_payload,
    }
    snapshot_digest = canonical_digest(snapshot_payload)
    snapshot_id = uuid4()
    now = datetime.now(timezone.utc)
    effective.execute(
        CONFIGURATION_SNAPSHOTS_TABLE.insert().values(
            id=snapshot_id,
            configuration_version_id=version_id,
            configuration_kind=kind,
            schema_digest=context["schema_digest"],
            payload_digest=context["payload_digest"],
            dependency_digest=dependency_digest,
            adapter_manifest_digest=adapter_manifest_digest,
            snapshot_digest=snapshot_digest,
            snapshot_json=snapshot_payload,
            created_at=now,
        )
    )
    target_count, total_candidate_count = _freeze_kind_members(
        effective,
        snapshot_id=snapshot_id,
        kind=kind,
        payload=context["payload_json"],
        dependencies=dependencies,
    )
    effective.execute(
        CONFIGURATION_VERSIONS_TABLE.update()
        .where(CONFIGURATION_VERSIONS_TABLE.c.id == version_id)
        .values(lifecycle_status="VALIDATED", validated_at=now)
    )
    result = _snapshot_result(
        effective,
        {
            "id": snapshot_id,
            "configuration_version_id": version_id,
            "configuration_kind": kind,
            "snapshot_digest": snapshot_digest,
            "dependency_digest": dependency_digest,
        },
        repeat_noop=False,
    )
    if result.target_count != target_count or result.total_candidate_count != total_candidate_count:
        raise CanonicalControlPlaneBlocked(
            "BLOCKED_SNAPSHOT_MEMBER_DRIFT", "derived snapshot totals disagree"
        )
    return result


def assess_research_configuration_readiness(
    connection: Connection,
    *,
    snapshot_ids: Mapping[str, UUID],
) -> P0Readiness:
    """Assess only seven-P0 readiness; this does not preview or activate a bundle."""

    effective = _require_canonical_database(connection)
    reasons: list[str] = []
    supplied = set(snapshot_ids)
    expected = set(P0_CONFIGURATION_KINDS)
    for kind in sorted(expected - supplied):
        reasons.append(f"{kind}_SNAPSHOT_UNSET")
    for kind in sorted(supplied - expected):
        reasons.append(f"UNKNOWN_SNAPSHOT_KIND:{kind}")

    snapshots: dict[str, dict[str, Any]] = {}
    for expected_kind, snapshot_id in snapshot_ids.items():
        if expected_kind not in expected:
            continue
        row = effective.execute(
            select(CONFIGURATION_SNAPSHOTS_TABLE).where(
                CONFIGURATION_SNAPSHOTS_TABLE.c.id == snapshot_id
            )
        ).mappings().one_or_none()
        if row is None or row["configuration_kind"] != expected_kind:
            reasons.append(f"{expected_kind}_SNAPSHOT_INVALID")
            continue
        snapshots[expected_kind] = dict(row)

    target_count = 0
    total_candidate_count = 0
    target = snapshots.get("TARGET")
    generation = snapshots.get("GENERATION")
    if target is not None:
        target_count = int(
            effective.execute(
                select(func.count()).select_from(RESEARCH_TARGETS_TABLE).where(
                    RESEARCH_TARGETS_TABLE.c.target_snapshot_id == target["id"]
                )
            ).scalar_one()
        )
        if target_count == 0:
            reasons.append("TARGET_SET_UNSET")
    if generation is not None:
        allocation_rows = effective.execute(
            select(
                RESEARCH_TARGET_ALLOCATIONS_TABLE.c.allocation_count,
                RESEARCH_TARGET_ALLOCATIONS_TABLE.c.candidate_cap,
            ).where(
                RESEARCH_TARGET_ALLOCATIONS_TABLE.c.generation_snapshot_id
                == generation["id"]
            )
        ).all()
        total_candidate_count = sum(int(row.allocation_count) for row in allocation_rows)
        if not allocation_rows:
            reasons.append("PER_TARGET_ALLOCATION_UNSET")
        if any(row.candidate_cap is None for row in allocation_rows):
            reasons.append("PER_TARGET_CAP_UNSET")
        if target_count and len(allocation_rows) != target_count:
            reasons.append("TARGET_ALLOCATION_CARDINALITY_MISMATCH")

    window = snapshots.get("WINDOW")
    if window is not None:
        window_count = int(
            effective.execute(
                select(func.count()).select_from(CONFIGURATION_SNAPSHOT_MEMBERS_TABLE).where(
                    CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.configuration_snapshot_id
                    == window["id"],
                    CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.member_key.like("window:%"),
                )
            ).scalar_one()
        )
        if window_count == 0:
            reasons.append("WINDOW_SET_UNSET")

    aggregate = snapshots.get("RESEARCH_AGGREGATE")
    if aggregate is not None and len(snapshots) == len(P0_CONFIGURATION_KINDS):
        dependencies = _dependency_rows(effective, aggregate["configuration_version_id"])
        expected_versions = {
            kind: snapshots[kind]["configuration_version_id"]
            for kind in _AGGREGATE_DEPENDENCY_KINDS
        }
        observed_versions = {
            row["configuration_kind"]: row["depends_on_version_id"] for row in dependencies
        }
        if observed_versions != expected_versions:
            reasons.append("AGGREGATE_SNAPSHOT_BINDING_MISMATCH")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return P0Readiness(
        status="BLOCKED" if unique_reasons else "READY",
        reason_codes=unique_reasons,
        snapshot_ids=dict(snapshot_ids),
        target_count=target_count,
        total_candidate_count=total_candidate_count,
    )


__all__ = [
    "CanonicalControlPlaneBlocked",
    "ConfigurationDependencyInput",
    "ConfigurationDraftResult",
    "ConfigurationSnapshotResult",
    "P0Readiness",
    "assess_research_configuration_readiness",
    "canonical_digest",
    "create_configuration_draft",
    "validate_configuration_version",
]
