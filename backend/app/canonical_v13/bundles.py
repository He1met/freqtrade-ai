"""Research bundle preview, materialization, and side-effect-free activation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Final, Mapping
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import Connection, select

from app.canonical_v13.control_plane import (
    CanonicalControlPlaneBlocked,
    assess_research_configuration_readiness,
)
from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    P0_CONFIGURATION_KINDS,
)
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    CONFIGURATION_ACTIVATIONS_TABLE,
    CONFIGURATION_BUNDLE_MEMBERS_TABLE,
    CONFIGURATION_BUNDLES_TABLE,
    CONFIGURATION_SNAPSHOTS_TABLE,
    MARKET_ARTIFACTS_TABLE,
    MARKET_INSPECTIONS_TABLE,
    MARKET_RECEIPTS_TABLE,
    MARKET_SNAPSHOTS_TABLE,
    MARKET_SNAPSHOT_MEMBERS_TABLE,
    RESEARCH_TARGETS_TABLE,
)


_CAPABILITIES = {
    "contract": "canonical-v13-no-trade-research-bundle-v1",
    "demo_only": True,
    "allow_real_funds": False,
    "single_writer_required": True,
    "exchange_access": "NONE",
    "order_submission": "DISABLED",
    "trading": "TRADING_DISABLED",
}
RESEARCH_BUNDLE_CAPABILITIES: Final[Mapping[str, object]] = MappingProxyType(
    _CAPABILITIES
)


class CanonicalBundleBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ResearchBundlePreview:
    status: str
    reason_codes: tuple[str, ...]
    scope_key: str
    workflow_key: str
    snapshot_ids: Mapping[str, UUID]
    snapshot_digests: Mapping[str, str]
    market_snapshot_id: UUID | None
    market_snapshot_digest: str | None
    target_count: int
    total_candidate_count: int
    capability_json: Mapping[str, object]
    bundle_digest: str | None
    prospective_bundle_id: UUID | None


@dataclass(frozen=True)
class ResearchBundleActivation:
    configuration_bundle_id: UUID
    configuration_activation_id: UUID
    bundle_digest: str
    previous_bundle_id: UUID | None
    repeat_noop: bool
    created_bundle: bool


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest_json(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    # SQLite drops timezone metadata from DateTime values in isolated tests.
    # Canonical writers persist UTC, so a naive round-trip is interpreted as UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


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
        raise CanonicalBundleBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    return effective


def preview_research_bundle(
    connection: Connection,
    *,
    scope_key: str,
    workflow_key: str,
    snapshot_ids: Mapping[str, UUID],
    market_snapshot_id: UUID | None,
) -> ResearchBundlePreview:
    """Build a deterministic no-write preview with exact blocker reasons."""

    if not scope_key or not workflow_key:
        raise CanonicalBundleBlocked(
            "BLOCKED_BUNDLE_SCOPE_UNSET", "scope_key and workflow_key are required"
        )
    effective = _require_canonical(connection)
    try:
        readiness = assess_research_configuration_readiness(
            effective, snapshot_ids=snapshot_ids
        )
    except CanonicalControlPlaneBlocked as exc:
        raise CanonicalBundleBlocked(exc.code, exc.detail) from exc
    reasons = list(readiness.reason_codes)
    snapshot_digests: dict[str, str] = {}
    snapshot_payloads: dict[str, Mapping[str, object]] = {}
    for kind in P0_CONFIGURATION_KINDS:
        snapshot_id = snapshot_ids.get(kind)
        if snapshot_id is None:
            continue
        row = effective.execute(
            select(CONFIGURATION_SNAPSHOTS_TABLE).where(
                CONFIGURATION_SNAPSHOTS_TABLE.c.id == snapshot_id
            )
        ).mappings().one_or_none()
        if row is None or row["configuration_kind"] != kind:
            continue
        if _digest_json(row["snapshot_json"]) != row["snapshot_digest"]:
            reasons.append(f"{kind}_SNAPSHOT_DIGEST_DRIFT")
        snapshot_payload = row["snapshot_json"]
        if snapshot_payload.get("scope_key") != scope_key:
            reasons.append(f"{kind}_SCOPE_MISMATCH")
        if snapshot_payload.get("workflow_key") != workflow_key:
            reasons.append(f"{kind}_WORKFLOW_MISMATCH")
        snapshot_digests[kind] = row["snapshot_digest"]
        snapshot_payloads[kind] = row["snapshot_json"]

    market_digest: str | None = None
    if market_snapshot_id is None:
        reasons.append("MARKET_SNAPSHOT_UNSET")
    else:
        market = effective.execute(
            select(MARKET_SNAPSHOTS_TABLE).where(
                MARKET_SNAPSHOTS_TABLE.c.id == market_snapshot_id
            )
        ).mappings().one_or_none()
        if market is None:
            reasons.append("MARKET_SNAPSHOT_INVALID")
        else:
            market_digest = market["snapshot_digest"]
            target_snapshot_id = snapshot_ids.get("TARGET")
            target_ids = set(
                effective.execute(
                    select(RESEARCH_TARGETS_TABLE.c.id).where(
                        RESEARCH_TARGETS_TABLE.c.target_snapshot_id
                        == target_snapshot_id
                    )
                ).scalars()
            ) if target_snapshot_id is not None else set()
            member_rows = effective.execute(
                select(MARKET_SNAPSHOT_MEMBERS_TABLE).where(
                    MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_snapshot_id
                    == market_snapshot_id
                )
            ).mappings().all()
            member_targets = {row["research_target_id"] for row in member_rows}
            if not target_ids or member_targets != target_ids:
                reasons.append("MARKET_TARGET_COVERAGE_MISMATCH")
            normalized_market_members: list[dict[str, object]] = []
            window_snapshot = snapshot_payloads.get("WINDOW", {})
            window_payload = window_snapshot.get("payload_json", {})
            required_windows = (
                tuple(
                    window
                    for window in window_payload.get("windows", ())
                    if isinstance(window, dict) and window.get("required") is True
                )
                if isinstance(window_payload, dict)
                else ()
            )
            if not required_windows:
                reasons.append("REQUIRED_WINDOWS_UNSET")
            for member in member_rows:
                receipt = effective.execute(
                    select(MARKET_RECEIPTS_TABLE).where(
                        MARKET_RECEIPTS_TABLE.c.id == member["market_receipt_id"]
                    )
                ).mappings().one_or_none()
                if (
                    receipt is None
                    or receipt["status"] != "ACCEPTED"
                    or receipt["market_artifact_id"] != member["market_artifact_id"]
                ):
                    reasons.append("MARKET_RECEIPT_NOT_ACCEPTED")
                    break
                artifact = effective.execute(
                    select(MARKET_ARTIFACTS_TABLE).where(
                        MARKET_ARTIFACTS_TABLE.c.id == member["market_artifact_id"]
                    )
                ).mappings().one_or_none()
                inspection = effective.execute(
                    select(MARKET_INSPECTIONS_TABLE).where(
                        MARKET_INSPECTIONS_TABLE.c.id == receipt["market_inspection_id"]
                    )
                ).mappings().one_or_none()
                expected_receipt_digest = _digest_json(
                    {
                        "artifact_digest": receipt["artifact_digest"],
                        "inspection_digest": receipt["inspection_digest"],
                        "status": receipt["status"],
                    }
                )
                if (
                    artifact is None
                    or inspection is None
                    or inspection["status"] != "ACCEPTED"
                    or artifact["content_digest"] != receipt["artifact_digest"]
                    or _digest_json(inspection["inspection_json"])
                    != inspection["inspection_digest"]
                    or inspection["inspection_digest"] != receipt["inspection_digest"]
                    or expected_receipt_digest != receipt["receipt_digest"]
                ):
                    reasons.append("MARKET_EVIDENCE_DIGEST_DRIFT")
                    break
                inspection_json = inspection["inspection_json"]
                member_start = _utc_iso(member["coverage_start"])
                member_end = _utc_iso(member["coverage_end"])
                if (
                    inspection_json.get("first_open_at") != member_start
                    or inspection_json.get("last_close_at") != member_end
                ):
                    reasons.append("MARKET_INSPECTION_COVERAGE_MISMATCH")
                for window in required_windows:
                    coverage = window.get("coverage")
                    if not isinstance(coverage, dict):
                        reasons.append("REQUIRED_WINDOW_CONTRACT_INVALID")
                        continue
                    try:
                        window_start = datetime.fromisoformat(str(window["start_at"]))
                        window_end = datetime.fromisoformat(str(window["end_at"]))
                        minimum_candles = int(coverage["minimum_closed_candles"])
                        inspection_rows = int(inspection_json["row_count"])
                    except (KeyError, TypeError, ValueError):
                        reasons.append("REQUIRED_WINDOW_CONTRACT_INVALID")
                        continue
                    member_start_at = member["coverage_start"]
                    member_end_at = member["coverage_end"]
                    if member_start_at.tzinfo is None:
                        member_start_at = member_start_at.replace(tzinfo=timezone.utc)
                    if member_end_at.tzinfo is None:
                        member_end_at = member_end_at.replace(tzinfo=timezone.utc)
                    if window_start.tzinfo is None or window_end.tzinfo is None:
                        reasons.append("REQUIRED_WINDOW_TIMEZONE_UNSET")
                    elif (
                        member_start_at > window_start
                        or member_end_at < window_end
                    ):
                        reasons.append(
                            f"REQUIRED_WINDOW_COVERAGE_MISSING:{window['window_key']}"
                        )
                    if inspection_rows < minimum_candles:
                        reasons.append(
                            f"REQUIRED_WINDOW_CANDLE_COUNT_LOW:{window['window_key']}"
                        )
                normalized_member = {
                    "artifact_id": str(member["market_artifact_id"]),
                    "receipt_id": str(member["market_receipt_id"]),
                    "receipt_digest": receipt["receipt_digest"],
                    "target_id": str(member["research_target_id"]),
                    "coverage_start": _utc_iso(member["coverage_start"]),
                    "coverage_end": _utc_iso(member["coverage_end"]),
                }
                if _digest_json(normalized_member) != member["coverage_digest"]:
                    reasons.append("MARKET_MEMBER_DIGEST_DRIFT")
                    break
                normalized_market_members.append(normalized_member)
            normalized_market_members.sort(key=lambda item: item["target_id"])
            expected_market_digest = _digest_json(
                {
                    "market_profile_version_id": str(
                        market["market_profile_version_id"]
                    ),
                    "members": normalized_market_members,
                }
            )
            if (
                len(normalized_market_members) == len(member_rows)
                and expected_market_digest != market["snapshot_digest"]
            ):
                reasons.append("MARKET_SNAPSHOT_DIGEST_DRIFT")

    unique_reasons = tuple(dict.fromkeys(reasons))
    bundle_digest: str | None = None
    prospective_bundle_id: UUID | None = None
    if not unique_reasons:
        bundle_digest = _digest_json(
            {
                "contract": "canonical-v13-research-bundle-v1",
                "scope_key": scope_key,
                "workflow_key": workflow_key,
                "snapshots": [
                    {
                        "configuration_kind": kind,
                        "snapshot_id": str(snapshot_ids[kind]),
                        "snapshot_digest": snapshot_digests[kind],
                    }
                    for kind in P0_CONFIGURATION_KINDS
                ],
                "market_snapshot_id": str(market_snapshot_id),
                "market_snapshot_digest": market_digest,
                "capabilities": dict(RESEARCH_BUNDLE_CAPABILITIES),
            }
        )
        prospective_bundle_id = uuid5(
            NAMESPACE_URL, f"urn:freqtrade-ai:canonical-v13:bundle:{bundle_digest}"
        )
    return ResearchBundlePreview(
        status="BLOCKED" if unique_reasons else "READY",
        reason_codes=unique_reasons,
        scope_key=scope_key,
        workflow_key=workflow_key,
        snapshot_ids=dict(snapshot_ids),
        snapshot_digests=snapshot_digests,
        market_snapshot_id=market_snapshot_id,
        market_snapshot_digest=market_digest,
        target_count=readiness.target_count,
        total_candidate_count=readiness.total_candidate_count,
        capability_json=dict(RESEARCH_BUNDLE_CAPABILITIES),
        bundle_digest=bundle_digest,
        prospective_bundle_id=prospective_bundle_id,
    )


def activate_research_bundle(
    connection: Connection,
    *,
    scope_key: str,
    workflow_key: str,
    snapshot_ids: Mapping[str, UUID],
    market_snapshot_id: UUID | None,
    actor_identity: str,
    expected_bundle_digest: str,
    expected_bundle_id: UUID,
) -> ResearchBundleActivation:
    """Materialize and point control state only; it cannot create execution rows."""

    if not actor_identity or not expected_bundle_digest:
        raise CanonicalBundleBlocked(
            "BLOCKED_ACTIVATION_AUTHORITY_UNSET",
            "actor and exact preview digest are required",
        )
    effective = _require_canonical(connection)
    preview = preview_research_bundle(
        effective,
        scope_key=scope_key,
        workflow_key=workflow_key,
        snapshot_ids=snapshot_ids,
        market_snapshot_id=market_snapshot_id,
    )
    if (
        preview.status != "READY"
        or preview.bundle_digest is None
        or preview.prospective_bundle_id is None
    ):
        raise CanonicalBundleBlocked(
            "BLOCKED_RESEARCH_BUNDLE_NOT_READY", ",".join(preview.reason_codes)
        )
    if preview.bundle_digest != expected_bundle_digest:
        raise CanonicalBundleBlocked(
            "BLOCKED_PREVIEW_DIGEST_DRIFT",
            "activation inputs differ from the reviewed preview",
        )
    if preview.prospective_bundle_id != expected_bundle_id:
        raise CanonicalBundleBlocked(
            "BLOCKED_BUNDLE_ID_DRIFT",
            "bundle path identity differs from the deterministic preview identity",
        )
    bundle_row = effective.execute(
        select(CONFIGURATION_BUNDLES_TABLE).where(
            CONFIGURATION_BUNDLES_TABLE.c.bundle_digest == preview.bundle_digest
        )
    ).mappings().one_or_none()
    created_bundle = bundle_row is None
    if bundle_row is None:
        bundle_id = preview.prospective_bundle_id
        effective.execute(
            CONFIGURATION_BUNDLES_TABLE.insert().values(
                id=bundle_id,
                scope_key=scope_key,
                workflow_key=workflow_key,
                market_snapshot_id=market_snapshot_id,
                market_snapshot_digest=preview.market_snapshot_digest,
                bundle_digest=preview.bundle_digest,
                capability_json=dict(RESEARCH_BUNDLE_CAPABILITIES),
                created_at=datetime.now(timezone.utc),
            )
        )
        for kind in P0_CONFIGURATION_KINDS:
            effective.execute(
                CONFIGURATION_BUNDLE_MEMBERS_TABLE.insert().values(
                    id=uuid4(),
                    configuration_bundle_id=bundle_id,
                    configuration_snapshot_id=snapshot_ids[kind],
                    configuration_kind=kind,
                    member_key=f"{kind}:{snapshot_ids[kind]}",
                    snapshot_digest=preview.snapshot_digests[kind],
                )
            )
    else:
        bundle_id = bundle_row["id"]
        if bundle_id != preview.prospective_bundle_id:
            raise CanonicalBundleBlocked(
                "BLOCKED_BUNDLE_ID_DRIFT",
                "persisted bundle identity differs from its content identity",
            )

    activation_statement = select(CONFIGURATION_ACTIVATIONS_TABLE).where(
            CONFIGURATION_ACTIVATIONS_TABLE.c.scope_key == scope_key,
            CONFIGURATION_ACTIVATIONS_TABLE.c.workflow_key == workflow_key,
        )
    if effective.dialect.name != "sqlite":
        activation_statement = activation_statement.with_for_update()
    activation = effective.execute(activation_statement).mappings().one_or_none()
    if activation is not None and activation["configuration_bundle_id"] == bundle_id:
        return ResearchBundleActivation(
            configuration_bundle_id=bundle_id,
            configuration_activation_id=activation["id"],
            bundle_digest=preview.bundle_digest,
            previous_bundle_id=activation["previous_bundle_id"],
            repeat_noop=True,
            created_bundle=created_bundle,
        )
    previous_bundle_id = (
        activation["configuration_bundle_id"] if activation is not None else None
    )
    activation_id = activation["id"] if activation is not None else uuid4()
    now = datetime.now(timezone.utc)
    values = dict(
        configuration_bundle_id=bundle_id,
        bundle_digest=preview.bundle_digest,
        previous_bundle_id=previous_bundle_id,
        activated_by=actor_identity,
        activated_at=now,
    )
    if activation is None:
        effective.execute(
            CONFIGURATION_ACTIVATIONS_TABLE.insert().values(
                id=activation_id,
                scope_key=scope_key,
                workflow_key=workflow_key,
                **values,
            )
        )
    else:
        effective.execute(
            CONFIGURATION_ACTIVATIONS_TABLE.update()
            .where(CONFIGURATION_ACTIVATIONS_TABLE.c.id == activation_id)
            .values(**values)
        )
    effective.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=uuid4(),
            event_type="RESEARCH_BUNDLE_ACTIVATED",
            aggregate_type="configuration_activation",
            aggregate_id=str(activation_id),
            actor_identity=actor_identity,
            request_digest=preview.bundle_digest,
            receipt_digest=_digest_json(
                {
                    "activation_id": str(activation_id),
                    "bundle_id": str(bundle_id),
                    "bundle_digest": preview.bundle_digest,
                    "previous_bundle_id": (
                        str(previous_bundle_id) if previous_bundle_id else None
                    ),
                }
            ),
            evidence_json={
                "bundle_id": str(bundle_id),
                "bundle_digest": preview.bundle_digest,
                "side_effect_contract": "CONTROL_POINTER_ONLY",
            },
            created_at=now,
        )
    )
    return ResearchBundleActivation(
        configuration_bundle_id=bundle_id,
        configuration_activation_id=activation_id,
        bundle_digest=preview.bundle_digest,
        previous_bundle_id=previous_bundle_id,
        repeat_noop=False,
        created_bundle=created_bundle,
    )


__all__ = [
    "CanonicalBundleBlocked",
    "RESEARCH_BUNDLE_CAPABILITIES",
    "ResearchBundleActivation",
    "ResearchBundlePreview",
    "activate_research_bundle",
    "preview_research_bundle",
]
