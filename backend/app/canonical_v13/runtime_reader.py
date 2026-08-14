"""Narrow, canonical-only readers for frozen no-trade research bundles.

The active-pointer resolver is a control/API-side read contract.  Research and
runtime consumers receive the resulting explicit bundle identity and digest;
they never discover mutable activation state themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from sqlalchemy import Connection, select

from app.canonical_v13.bundles import (
    RESEARCH_BUNDLE_CAPABILITIES,
    preview_research_bundle,
)
from app.canonical_v13.control_plane import canonical_digest
from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    P0_CONFIGURATION_KINDS,
)
from app.canonical_v13.models import (
    CONFIGURATION_ACTIVATIONS_TABLE,
    CONFIGURATION_BUNDLE_MEMBERS_TABLE,
    CONFIGURATION_BUNDLES_TABLE,
    CONFIGURATION_SNAPSHOT_MEMBERS_TABLE,
    CONFIGURATION_SNAPSHOTS_TABLE,
    RESEARCH_TARGET_ALLOCATIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
)


class CanonicalFrozenReaderBlocked(RuntimeError):
    """A stable fail-closed result from a canonical frozen reader."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ActiveResearchBinding:
    scope_key: str
    workflow_key: str
    configuration_bundle_id: UUID
    configuration_bundle_digest: str


@dataclass(frozen=True)
class FrozenConfigurationBinding:
    configuration_kind: str
    snapshot_id: UUID
    snapshot_digest: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class FrozenResearchTarget:
    research_target_id: UUID
    target_key: str
    instrument: str
    pair: str
    timeframe: str
    data_kind: str
    target_digest: str


@dataclass(frozen=True)
class FrozenTargetAllocation:
    research_target_id: UUID
    target_key: str
    allocation_count: int
    candidate_cap: int
    allocation_digest: str


@dataclass(frozen=True)
class FrozenRequiredWindow:
    snapshot_member_id: UUID
    window_key: str
    required: bool
    start_at: str
    end_at: str
    minimum_closed_candles: int
    member_digest: str


@dataclass(frozen=True)
class FrozenResearchBundle:
    status: str
    reason_codes: tuple[str, ...]
    scope_key: str
    workflow_key: str
    configuration_bundle_id: UUID
    configuration_bundle_digest: str
    market_snapshot_id: UUID
    market_snapshot_digest: str
    capability: Mapping[str, object]
    configurations: tuple[FrozenConfigurationBinding, ...]
    targets: tuple[FrozenResearchTarget, ...]
    allocations: tuple[FrozenTargetAllocation, ...]
    windows: tuple[FrozenRequiredWindow, ...]


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
        raise CanonicalFrozenReaderBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    return effective


def resolve_active_research_binding(
    connection: Connection,
    *,
    scope_key: str,
    workflow_key: str,
) -> ActiveResearchBinding:
    """Resolve one mutable pointer for an API/control-side caller, without writes."""

    if not scope_key or not workflow_key:
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_SCOPE_INCOMPLETE", "scope_key and workflow_key are required"
        )
    effective = _require_canonical(connection)
    rows = effective.execute(
        select(CONFIGURATION_ACTIVATIONS_TABLE).where(
            CONFIGURATION_ACTIVATIONS_TABLE.c.scope_key == scope_key,
            CONFIGURATION_ACTIVATIONS_TABLE.c.workflow_key == workflow_key,
        )
    ).mappings().all()
    if not rows:
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_BUNDLE_UNSET", "no active canonical research bundle"
        )
    if len(rows) != 1:
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_ACTIVATION_AMBIGUOUS", "active pointer is not unique"
        )
    activation = rows[0]
    bundle = effective.execute(
        select(CONFIGURATION_BUNDLES_TABLE).where(
            CONFIGURATION_BUNDLES_TABLE.c.id
            == activation["configuration_bundle_id"]
        )
    ).mappings().one_or_none()
    if (
        bundle is None
        or bundle["bundle_digest"] != activation["bundle_digest"]
        or bundle["scope_key"] != scope_key
        or bundle["workflow_key"] != workflow_key
    ):
        raise CanonicalFrozenReaderBlocked(
            "ACTIVE_BUNDLE_LINEAGE_DRIFT",
            "active pointer does not bind the requested immutable bundle",
        )
    return ActiveResearchBinding(
        scope_key=scope_key,
        workflow_key=workflow_key,
        configuration_bundle_id=bundle["id"],
        configuration_bundle_digest=bundle["bundle_digest"],
    )


def _load_configuration_bindings(
    connection: Connection,
    *,
    bundle_id: UUID,
) -> tuple[tuple[FrozenConfigurationBinding, ...], dict[str, UUID]]:
    members = connection.execute(
        select(CONFIGURATION_BUNDLE_MEMBERS_TABLE).where(
            CONFIGURATION_BUNDLE_MEMBERS_TABLE.c.configuration_bundle_id
            == bundle_id
        )
    ).mappings().all()
    by_kind = {row["configuration_kind"]: row for row in members}
    if len(members) != len(P0_CONFIGURATION_KINDS) or set(by_kind) != set(
        P0_CONFIGURATION_KINDS
    ):
        raise CanonicalFrozenReaderBlocked(
            "ACTIVE_BUNDLE_MEMBER_SET_INVALID",
            "bundle must contain exactly one member for each canonical P0 kind",
        )
    bindings: list[FrozenConfigurationBinding] = []
    snapshot_ids: dict[str, UUID] = {}
    for kind in P0_CONFIGURATION_KINDS:
        member = by_kind[kind]
        snapshot = connection.execute(
            select(CONFIGURATION_SNAPSHOTS_TABLE).where(
                CONFIGURATION_SNAPSHOTS_TABLE.c.id
                == member["configuration_snapshot_id"]
            )
        ).mappings().one_or_none()
        if (
            snapshot is None
            or snapshot["configuration_kind"] != kind
            or snapshot["snapshot_digest"] != member["snapshot_digest"]
            or member["member_key"] != f"{kind}:{snapshot['id']}"
            or canonical_digest(snapshot["snapshot_json"])
            != snapshot["snapshot_digest"]
        ):
            raise CanonicalFrozenReaderBlocked(
                "ACTIVE_BUNDLE_SNAPSHOT_DRIFT",
                f"{kind} bundle member no longer matches its frozen snapshot",
            )
        snapshot_ids[kind] = snapshot["id"]
        bindings.append(
            FrozenConfigurationBinding(
                configuration_kind=kind,
                snapshot_id=snapshot["id"],
                snapshot_digest=snapshot["snapshot_digest"],
                payload=MappingProxyType(
                    dict(snapshot["snapshot_json"]["payload_json"])
                ),
            )
        )
    return tuple(bindings), snapshot_ids


def _load_targets(
    connection: Connection,
    *,
    target_snapshot_id: UUID,
) -> tuple[FrozenResearchTarget, ...]:
    rows = connection.execute(
        select(RESEARCH_TARGETS_TABLE).where(
            RESEARCH_TARGETS_TABLE.c.target_snapshot_id == target_snapshot_id
        )
    ).mappings().all()
    targets: list[FrozenResearchTarget] = []
    for row in rows:
        normalized = {
            "target_key": row["target_key"],
            "instrument": row["instrument"],
            "pair": row["pair"],
            "timeframe": row["timeframe"],
            "data_kind": row["data_kind"],
        }
        if canonical_digest(normalized) != row["target_digest"]:
            raise CanonicalFrozenReaderBlocked(
                "RESEARCH_TARGET_DIGEST_DRIFT", row["target_key"]
            )
        targets.append(
            FrozenResearchTarget(
                research_target_id=row["id"],
                target_digest=row["target_digest"],
                **normalized,
            )
        )
    if not targets:
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_TARGETS_UNSET", "target snapshot has no canonical members"
        )
    return tuple(sorted(targets, key=lambda item: item.target_key))


def _load_allocations(
    connection: Connection,
    *,
    generation_snapshot_id: UUID,
    targets: tuple[FrozenResearchTarget, ...],
) -> tuple[FrozenTargetAllocation, ...]:
    by_id = {target.research_target_id: target for target in targets}
    rows = connection.execute(
        select(RESEARCH_TARGET_ALLOCATIONS_TABLE).where(
            RESEARCH_TARGET_ALLOCATIONS_TABLE.c.generation_snapshot_id
            == generation_snapshot_id
        )
    ).mappings().all()
    allocations: list[FrozenTargetAllocation] = []
    for row in rows:
        target = by_id.get(row["research_target_id"])
        if target is None or row["candidate_cap"] is None:
            raise CanonicalFrozenReaderBlocked(
                "RESEARCH_ALLOCATION_TARGET_DRIFT",
                "allocation target or explicit cap is missing",
            )
        normalized = {
            "target_key": target.target_key,
            "allocation_count": row["allocation_count"],
            "candidate_cap": row["candidate_cap"],
        }
        if canonical_digest(normalized) != row["allocation_digest"]:
            raise CanonicalFrozenReaderBlocked(
                "RESEARCH_ALLOCATION_DIGEST_DRIFT", target.target_key
            )
        allocations.append(
            FrozenTargetAllocation(
                research_target_id=target.research_target_id,
                allocation_digest=row["allocation_digest"],
                **normalized,
            )
        )
    if {item.research_target_id for item in allocations} != set(by_id):
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_ALLOCATION_SET_MISMATCH",
            "allocation targets must exactly equal frozen targets",
        )
    return tuple(sorted(allocations, key=lambda item: item.target_key))


def _load_windows(
    connection: Connection,
    *,
    window_binding: FrozenConfigurationBinding,
) -> tuple[FrozenRequiredWindow, ...]:
    payload_windows = window_binding.payload.get("windows")
    if not isinstance(payload_windows, list) or not payload_windows:
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_WINDOWS_UNSET", "WINDOW payload has no explicit members"
        )
    member_rows = connection.execute(
        select(CONFIGURATION_SNAPSHOT_MEMBERS_TABLE).where(
            CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.configuration_snapshot_id
            == window_binding.snapshot_id,
            CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.member_key.like("window:%"),
        )
    ).mappings().all()
    by_key = {row["member_key"].removeprefix("window:"): row for row in member_rows}
    windows: list[FrozenRequiredWindow] = []
    for raw in payload_windows:
        if not isinstance(raw, dict) or not isinstance(raw.get("window_key"), str):
            raise CanonicalFrozenReaderBlocked(
                "RESEARCH_WINDOW_MEMBER_INVALID", "WINDOW payload shape drifted"
            )
        member = by_key.get(raw["window_key"])
        coverage = raw.get("coverage")
        try:
            start_at = datetime.fromisoformat(str(raw.get("start_at")))
            end_at = datetime.fromisoformat(str(raw.get("end_at")))
        except ValueError as exc:
            raise CanonicalFrozenReaderBlocked(
                "RESEARCH_WINDOW_MEMBER_INVALID",
                "WINDOW timestamps must be ISO-8601 text",
            ) from exc
        if (
            member is None
            or not isinstance(coverage, dict)
            or start_at.tzinfo is None
            or end_at.tzinfo is None
        ):
            raise CanonicalFrozenReaderBlocked(
                "RESEARCH_WINDOW_MEMBER_DIGEST_DRIFT", raw["window_key"]
            )
        normalized = {
            "window_key": raw["window_key"],
            "required": raw["required"],
            "start_at": start_at.astimezone(timezone.utc).isoformat(),
            "end_at": end_at.astimezone(timezone.utc).isoformat(),
            "coverage": dict(coverage),
        }
        if canonical_digest(normalized) != member["member_digest"]:
            raise CanonicalFrozenReaderBlocked(
                "RESEARCH_WINDOW_MEMBER_DIGEST_DRIFT", raw["window_key"]
            )
        windows.append(
            FrozenRequiredWindow(
                snapshot_member_id=member["id"],
                window_key=raw["window_key"],
                required=raw["required"],
                start_at=normalized["start_at"],
                end_at=normalized["end_at"],
                minimum_closed_candles=coverage["minimum_closed_candles"],
                member_digest=member["member_digest"],
            )
        )
    if len(windows) != len(member_rows):
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_WINDOW_SET_MISMATCH",
            "WINDOW payload and snapshot members differ",
        )
    return tuple(sorted(windows, key=lambda item: item.window_key))


def read_frozen_research_bundle(
    connection: Connection,
    *,
    configuration_bundle_id: UUID,
    expected_bundle_digest: str,
) -> FrozenResearchBundle:
    """Read an explicit immutable bundle; never resolve or mutate active state."""

    if not expected_bundle_digest:
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_BUNDLE_DIGEST_UNSET", "an exact reviewed digest is required"
        )
    effective = _require_canonical(connection)
    bundle = effective.execute(
        select(CONFIGURATION_BUNDLES_TABLE).where(
            CONFIGURATION_BUNDLES_TABLE.c.id == configuration_bundle_id
        )
    ).mappings().one_or_none()
    if bundle is None:
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_BUNDLE_NOT_FOUND", str(configuration_bundle_id)
        )
    if bundle["bundle_digest"] != expected_bundle_digest:
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_BUNDLE_DIGEST_DRIFT",
            "persisted bundle does not match the supplied immutable identity",
        )
    if bundle["capability_json"] != dict(RESEARCH_BUNDLE_CAPABILITIES):
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_BUNDLE_CAPABILITY_DRIFT",
            "no-trade capability envelope does not match canonical authority",
        )
    configurations, snapshot_ids = _load_configuration_bindings(
        effective, bundle_id=configuration_bundle_id
    )
    preview = preview_research_bundle(
        effective,
        scope_key=bundle["scope_key"],
        workflow_key=bundle["workflow_key"],
        snapshot_ids=snapshot_ids,
        market_snapshot_id=bundle["market_snapshot_id"],
    )
    if preview.status != "READY" or preview.bundle_digest != expected_bundle_digest:
        reasons = preview.reason_codes or ("ACTIVE_BUNDLE_DIGEST_DRIFT",)
        raise CanonicalFrozenReaderBlocked(
            "RESEARCH_BUNDLE_EVIDENCE_BLOCKED", ",".join(reasons)
        )
    targets = _load_targets(
        effective, target_snapshot_id=snapshot_ids["TARGET"]
    )
    allocations = _load_allocations(
        effective,
        generation_snapshot_id=snapshot_ids["GENERATION"],
        targets=targets,
    )
    window_binding = next(
        item for item in configurations if item.configuration_kind == "WINDOW"
    )
    windows = _load_windows(effective, window_binding=window_binding)
    return FrozenResearchBundle(
        status="PENDING_FIRST_BACKTEST",
        reason_codes=("PENDING_FIRST_BACKTEST", "TRADING_DISABLED"),
        scope_key=bundle["scope_key"],
        workflow_key=bundle["workflow_key"],
        configuration_bundle_id=bundle["id"],
        configuration_bundle_digest=bundle["bundle_digest"],
        market_snapshot_id=bundle["market_snapshot_id"],
        market_snapshot_digest=bundle["market_snapshot_digest"],
        capability=MappingProxyType(dict(bundle["capability_json"])),
        configurations=configurations,
        targets=targets,
        allocations=allocations,
        windows=windows,
    )


__all__ = [
    "ActiveResearchBinding",
    "CanonicalFrozenReaderBlocked",
    "FrozenConfigurationBinding",
    "FrozenRequiredWindow",
    "FrozenResearchBundle",
    "FrozenResearchTarget",
    "FrozenTargetAllocation",
    "read_frozen_research_bundle",
    "resolve_active_research_binding",
]
