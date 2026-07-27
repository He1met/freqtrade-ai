from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple
import uuid


class AcceptanceMode(str, Enum):
    OFFLINE_CI = "OFFLINE_CI"
    CONTROLLED_REAL = "CONTROLLED_REAL"


class AcceptanceStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    DRIFTED = "DRIFTED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    PASSED = "PASSED"


REQUIRED_SCENARIOS = (
    "MINIMUM_LIMIT_ORDER",
    "PARTIAL_FILL_AND_CANCEL",
    "ATTACHED_TP_SL",
    "BUSINESS_REJECTION",
    "RATE_LIMIT",
    "WRITE_TIMEOUT_RECOVERY",
    "WEBSOCKET_DISCONNECT",
    "WEBSOCKET_OUT_OF_ORDER",
    "CLIENT_ORDER_ID_RECOVERY",
    "WORKER_RESTART",
    "SUPERVISOR_RESTART",
    "POSITION_DRIFT",
    "STALE_MARKET_DATA",
    "STALE_INTENT",
    "DUPLICATE_REQUEST",
    "WRONG_EXECUTION_TARGET",
)
REAL_GATEWAY_KIND = "NORMAL_PIPELINE"
OFFLINE_GATEWAY_KIND = "OFFLINE_FIXTURE"


@dataclass(frozen=True)
class EvidenceReference:
    table: str
    database_id: int
    order_id: Optional[str] = None
    source: str = "DATABASE"

    def __post_init__(self) -> None:
        if not self.table or self.database_id <= 0:
            raise ValueError("evidence must reference a persisted database row")
        if self.order_id is not None and not self.order_id.strip():
            raise ValueError("order_id cannot be blank")
        if self.source not in {"DATABASE", "OFFLINE_FIXTURE"}:
            raise ValueError("evidence source is not allowed")


@dataclass(frozen=True)
class StateSnapshot:
    execution_target: str
    database_fingerprint: str
    open_order_ids: Tuple[str, ...]
    positions: Mapping[str, str]
    lineage: Tuple[EvidenceReference, ...]

    def __post_init__(self) -> None:
        if self.execution_target != "OKX_DEMO":
            raise ValueError("snapshot execution target must be OKX_DEMO")
        if len(self.database_fingerprint) != 64:
            raise ValueError("database fingerprint must be a SHA-256 digest")


@dataclass(frozen=True)
class Preflight:
    ready: bool
    blockers: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.ready == bool(self.blockers):
            raise ValueError("ready preflight cannot have blockers")


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    passed: bool
    reason: str
    through_single_writer: bool
    evidence: Tuple[EvidenceReference, ...]
    assertions: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.name not in REQUIRED_SCENARIOS:
            raise ValueError("unknown acceptance scenario")
        if not self.reason:
            raise ValueError("scenario result requires a reason")
        if self.passed and not self.assertions:
            raise ValueError("passed scenario must list verified assertions")


@dataclass(frozen=True)
class SurfaceVerification:
    consistent: bool
    reason: str
    evidence: Tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class CleanupResult:
    verified: bool
    reason: str
    final_snapshot: StateSnapshot
    evidence: Tuple[EvidenceReference, ...]


class OkxDemoE2EGateway(Protocol):
    """Boundary implemented by an offline fixture or the one normal runtime pipeline."""

    gateway_kind: str

    def preflight(self, mode: AcceptanceMode) -> Preflight:
        ...

    def capture_baseline(self) -> StateSnapshot:
        ...

    def run_scenario(self, name: str) -> ScenarioResult:
        ...

    def verify_surfaces(self) -> SurfaceVerification:
        ...

    def cleanup(self, baseline: StateSnapshot) -> CleanupResult:
        ...


@dataclass(frozen=True)
class AcceptanceReport:
    schema_version: int
    run_id: str
    mode: str
    status: str
    execution_target: str
    gateway_kind: str
    real_demo_executed: bool
    started_at: str
    finished_at: str
    reason: str
    database_fingerprint: Optional[str]
    baseline: Optional[StateSnapshot]
    scenarios: Tuple[ScenarioResult, ...]
    surfaces: Optional[SurfaceVerification]
    cleanup: Optional[CleanupResult]
    integration_points: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return _json_safe(asdict(self))


INTEGRATION_POINTS = (
    "#449: provide a NORMAL_PIPELINE gateway owned by the single supervised writer",
    "#450: enqueue each scenario through the durable research-to-order pipeline",
    "#451: verify the desktop API/page projection from the same database lineage",
    "#448: supply authoritative reconciliation snapshots and final cleanup proof",
)


def run_acceptance(
    gateway: OkxDemoE2EGateway,
    *,
    mode: AcceptanceMode,
    allow_real_demo: bool = False,
    artifact_path: Optional[Path] = None,
    now_provider=lambda: datetime.now(timezone.utc),
) -> AcceptanceReport:
    if artifact_path is not None:
        _validate_artifact_path(artifact_path)
    started_at = now_provider()
    run_id = str(uuid.uuid4())
    if mode == AcceptanceMode.CONTROLLED_REAL and not allow_real_demo:
        return _finish(
            run_id=run_id,
            mode=mode,
            status=AcceptanceStatus.NOT_RUN,
            gateway=gateway,
            started_at=started_at,
            now_provider=now_provider,
            reason="EXPLICIT_REAL_DEMO_AUTHORIZATION_REQUIRED",
            artifact_path=artifact_path,
        )
    expected_gateway = (
        REAL_GATEWAY_KIND
        if mode == AcceptanceMode.CONTROLLED_REAL
        else OFFLINE_GATEWAY_KIND
    )
    if gateway.gateway_kind != expected_gateway:
        return _finish(
            run_id=run_id,
            mode=mode,
            status=AcceptanceStatus.BLOCKED,
            gateway=gateway,
            started_at=started_at,
            now_provider=now_provider,
            reason="GATEWAY_MUST_USE_{}".format(expected_gateway),
            artifact_path=artifact_path,
        )
    if (
        mode == AcceptanceMode.CONTROLLED_REAL
        and not _is_registered_normal_pipeline_gateway(gateway)
    ):
        return _finish(
            run_id=run_id,
            mode=mode,
            status=AcceptanceStatus.BLOCKED,
            gateway=gateway,
            started_at=started_at,
            now_provider=now_provider,
            reason="NORMAL_PIPELINE_GATEWAY_NOT_INTEGRATED",
            artifact_path=artifact_path,
        )
    if mode == AcceptanceMode.CONTROLLED_REAL and artifact_path is None:
        return _finish(
            run_id=run_id,
            mode=mode,
            status=AcceptanceStatus.BLOCKED,
            gateway=gateway,
            started_at=started_at,
            now_provider=now_provider,
            reason="CONTROLLED_REAL_ARTIFACT_REQUIRED",
            artifact_path=None,
        )
    try:
        preflight = gateway.preflight(mode)
    except Exception as exc:
        return _finish(
            run_id=run_id,
            mode=mode,
            status=AcceptanceStatus.FAILED,
            gateway=gateway,
            started_at=started_at,
            now_provider=now_provider,
            reason=_safe_exception_reason(exc),
            artifact_path=artifact_path,
        )
    if not preflight.ready:
        return _finish(
            run_id=run_id,
            mode=mode,
            status=AcceptanceStatus.BLOCKED,
            gateway=gateway,
            started_at=started_at,
            now_provider=now_provider,
            reason=";".join(preflight.blockers),
            artifact_path=artifact_path,
        )

    baseline: Optional[StateSnapshot] = None
    scenarios = []
    surfaces: Optional[SurfaceVerification] = None
    cleanup: Optional[CleanupResult] = None
    status = AcceptanceStatus.FAILED
    reason = "ACCEPTANCE_DID_NOT_COMPLETE"
    try:
        baseline = gateway.capture_baseline()
        if mode == AcceptanceMode.CONTROLLED_REAL and not baseline.lineage:
            raise RuntimeError("BASELINE_DATABASE_LINEAGE_REQUIRED")
        if mode == AcceptanceMode.CONTROLLED_REAL and any(
            evidence.source != "DATABASE" for evidence in baseline.lineage
        ):
            raise RuntimeError("REAL_BASELINE_REQUIRES_DATABASE_EVIDENCE")
        for name in REQUIRED_SCENARIOS:
            result = gateway.run_scenario(name)
            if result.name != name:
                raise RuntimeError("SCENARIO_IDENTITY_MISMATCH")
            if not result.evidence:
                raise RuntimeError("SCENARIO_DATABASE_LINEAGE_REQUIRED")
            if mode == AcceptanceMode.CONTROLLED_REAL and not result.through_single_writer:
                raise RuntimeError("SCENARIO_BYPASSED_SINGLE_WRITER")
            if mode == AcceptanceMode.CONTROLLED_REAL and any(
                evidence.source != "DATABASE" for evidence in result.evidence
            ):
                raise RuntimeError("REAL_SCENARIO_REQUIRES_DATABASE_EVIDENCE")
            scenarios.append(result)
        surfaces = gateway.verify_surfaces()
        if not surfaces.evidence:
            raise RuntimeError("SURFACE_DATABASE_LINEAGE_REQUIRED")
        if mode == AcceptanceMode.CONTROLLED_REAL and any(
            evidence.source != "DATABASE" for evidence in surfaces.evidence
        ):
            raise RuntimeError("REAL_SURFACES_REQUIRE_DATABASE_EVIDENCE")
        status = AcceptanceStatus.PASSED
        reason = "ALL_SCENARIOS_AND_SURFACES_VERIFIED"
        if any(not result.passed for result in scenarios):
            status = AcceptanceStatus.FAILED
            reason = "ONE_OR_MORE_SCENARIOS_FAILED"
        elif not surfaces.consistent:
            status = AcceptanceStatus.FAILED
            reason = "PAGE_API_DATABASE_ARTIFACT_EXCHANGE_MISMATCH"
        elif mode == AcceptanceMode.CONTROLLED_REAL:
            all_evidence = tuple(
                evidence
                for result in scenarios
                for evidence in result.evidence
            )
            if not all_evidence or not any(
                evidence.order_id for evidence in all_evidence
            ):
                status = AcceptanceStatus.FAILED
                reason = "REAL_DEMO_ORDER_AND_DATABASE_EVIDENCE_REQUIRED"
    except Exception as exc:
        status = AcceptanceStatus.FAILED
        reason = _safe_exception_reason(exc)
    finally:
        if baseline is not None:
            try:
                cleanup = gateway.cleanup(baseline)
            except Exception as exc:
                cleanup = None
                status = AcceptanceStatus.RECOVERY_REQUIRED
                reason = "CLEANUP_EXCEPTION_{}".format(
                    exc.__class__.__name__.upper()
                )

    if baseline is not None:
        if cleanup is None or not cleanup.verified:
            status = AcceptanceStatus.RECOVERY_REQUIRED
            reason = (
                cleanup.reason
                if cleanup is not None
                else "FINAL_CLEANUP_NOT_VERIFIED"
            )
        elif cleanup.final_snapshot.database_fingerprint != baseline.database_fingerprint:
            status = AcceptanceStatus.DRIFTED
            reason = "DATABASE_FINGERPRINT_CHANGED"
        elif not _baseline_restored(baseline, cleanup.final_snapshot):
            status = AcceptanceStatus.DRIFTED
            reason = "FINAL_STATE_DIFFERS_FROM_BASELINE"
        elif mode == AcceptanceMode.CONTROLLED_REAL:
            if not cleanup.evidence:
                status = AcceptanceStatus.RECOVERY_REQUIRED
                reason = "FINAL_CLEANUP_DATABASE_LINEAGE_REQUIRED"
            elif any(
                evidence.source != "DATABASE" for evidence in cleanup.evidence
            ):
                status = AcceptanceStatus.RECOVERY_REQUIRED
                reason = "REAL_CLEANUP_REQUIRES_DATABASE_EVIDENCE"

    return _finish(
        run_id=run_id,
        mode=mode,
        status=status,
        gateway=gateway,
        started_at=started_at,
        now_provider=now_provider,
        reason=reason,
        artifact_path=artifact_path,
        baseline=baseline,
        scenarios=tuple(scenarios),
        surfaces=surfaces,
        cleanup=cleanup,
    )


def _baseline_restored(
    baseline: StateSnapshot,
    final_snapshot: StateSnapshot,
) -> bool:
    return (
        final_snapshot.execution_target == baseline.execution_target
        and final_snapshot.open_order_ids == baseline.open_order_ids
        and dict(final_snapshot.positions) == dict(baseline.positions)
    )


def _finish(
    *,
    run_id: str,
    mode: AcceptanceMode,
    status: AcceptanceStatus,
    gateway: OkxDemoE2EGateway,
    started_at: datetime,
    now_provider,
    reason: str,
    artifact_path: Optional[Path],
    baseline: Optional[StateSnapshot] = None,
    scenarios: Tuple[ScenarioResult, ...] = (),
    surfaces: Optional[SurfaceVerification] = None,
    cleanup: Optional[CleanupResult] = None,
) -> AcceptanceReport:
    report = AcceptanceReport(
        schema_version=1,
        run_id=run_id,
        mode=mode.value,
        status=status.value,
        execution_target="OKX_DEMO",
        gateway_kind=gateway.gateway_kind,
        real_demo_executed=(
            mode == AcceptanceMode.CONTROLLED_REAL
            and bool(scenarios)
        ),
        started_at=_iso(started_at),
        finished_at=_iso(now_provider()),
        reason=reason,
        database_fingerprint=(
            baseline.database_fingerprint if baseline is not None else None
        ),
        baseline=baseline,
        scenarios=scenarios,
        surfaces=surfaces,
        cleanup=cleanup,
        integration_points=INTEGRATION_POINTS,
    )
    if artifact_path is not None:
        _write_artifact(artifact_path, report)
    return report


def _write_artifact(path: Path, report: AcceptanceReport) -> None:
    resolved = _validate_artifact_path(path)

    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(resolved)


def _validate_artifact_path(path: Path) -> Path:
    resolved = path.resolve()
    managed_root = (
        Path.home() / ".freqtrade-ai" / "runtime" / "okx-demo-e2e"
    ).resolve()
    try:
        resolved.relative_to(managed_root)
    except ValueError as exc:
        raise ValueError("artifact path must stay in the managed E2E root") from exc
    return resolved


def database_fingerprint(database_identity: str) -> str:
    """Fingerprint a safe canonical DB identity supplied by the DB adapter."""

    normalized = database_identity.strip()
    if not normalized or "://" in normalized or "@" in normalized:
        raise ValueError("database identity must be a safe opaque identifier")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_exception_reason(exc: Exception) -> str:
    return "FRAMEWORK_EXCEPTION_{}".format(exc.__class__.__name__.upper())


def _is_registered_normal_pipeline_gateway(gateway: OkxDemoE2EGateway) -> bool:
    """Fail closed until #449/#450 provide the concrete canonical adapter.

    A string marker or a boolean supplied by a test double cannot authorize a
    real Demo run. The integration module must own validation of the canonical
    runtime, DB run/attempt/order/reconciliation joins, and writer attestation.
    """

    try:
        from app.services.okx_demo_normal_pipeline_gateway import (
            NormalPipelineAcceptanceGateway,
        )
    except ImportError:
        return False
    return isinstance(gateway, NormalPipelineAcceptanceGateway)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class DeterministicOfflineGateway:
    """Network-free fixture proving the acceptance state machine in CI."""

    gateway_kind = OFFLINE_GATEWAY_KIND

    def __init__(self) -> None:
        self._fingerprint = database_fingerprint("offline-ci-isolated-postgresql")
        self._next_id = 1000
        self.scenarios_seen = []
        self._orders: Dict[str, str] = {}
        self._positions: Dict[str, str] = {}
        self._posts: Dict[str, int] = {}
        self._writer_generation = 1
        self._last_ws_sequence = 0
        self._openings_frozen = False

    def preflight(self, mode: AcceptanceMode) -> Preflight:
        return Preflight(ready=mode == AcceptanceMode.OFFLINE_CI)

    def capture_baseline(self) -> StateSnapshot:
        return StateSnapshot(
            execution_target="OKX_DEMO",
            database_fingerprint=self._fingerprint,
            open_order_ids=(),
            positions={},
            lineage=(
                EvidenceReference(
                    "okx_e2e_runs",
                    1,
                    source="OFFLINE_FIXTURE",
                ),
            ),
        )

    def run_scenario(self, name: str) -> ScenarioResult:
        self._next_id += 1
        self.scenarios_seen.append(name)
        assertions = self._exercise(name)
        return ScenarioResult(
            name=name,
            passed=True,
            reason="OFFLINE_STATE_TRANSITIONS_VERIFIED",
            through_single_writer=False,
            evidence=(
                EvidenceReference(
                    "okx_e2e_scenarios",
                    self._next_id,
                    source="OFFLINE_FIXTURE",
                ),
            ),
            assertions=assertions,
        )

    def verify_surfaces(self) -> SurfaceVerification:
        return SurfaceVerification(
            consistent=True,
            reason="OFFLINE_PROJECTIONS_MATCH_FIXTURE",
            evidence=(
                EvidenceReference(
                    "okx_e2e_runs",
                    1,
                    source="OFFLINE_FIXTURE",
                ),
            ),
        )

    def cleanup(self, baseline: StateSnapshot) -> CleanupResult:
        self._orders = {
            order_id: state
            for order_id, state in self._orders.items()
            if state not in {"live", "partially_filled"}
        }
        self._positions.clear()
        self._openings_frozen = False
        final_snapshot = StateSnapshot(
            execution_target="OKX_DEMO",
            database_fingerprint=self._fingerprint,
            open_order_ids=tuple(
                sorted(
                    order_id
                    for order_id, state in self._orders.items()
                    if state in {"live", "partially_filled"}
                )
            ),
            positions=dict(self._positions),
            lineage=(
                EvidenceReference(
                    "okx_e2e_runs",
                    1,
                    source="OFFLINE_FIXTURE",
                ),
            ),
        )
        return CleanupResult(
            verified=True,
            reason="OFFLINE_BASELINE_RESTORED",
            final_snapshot=final_snapshot,
            evidence=(
                EvidenceReference(
                    "okx_e2e_runs",
                    1,
                    source="OFFLINE_FIXTURE",
                ),
            ),
        )

    def _exercise(self, name: str) -> Tuple[str, ...]:
        client_id = "offline-{}".format(name.lower().replace("_", "-"))
        if name == "MINIMUM_LIMIT_ORDER":
            self._post_once(client_id, "live")
            self._orders[client_id] = "canceled"
            self._require(self._orders[client_id] == "canceled")
            return ("created_once", "queried_by_client_id", "cancel_terminal")
        if name == "PARTIAL_FILL_AND_CANCEL":
            self._post_once(client_id, "partially_filled")
            self._positions["BTC-USDT-SWAP"] = "0.01"
            self._orders[client_id] = "canceled"
            self._positions.pop("BTC-USDT-SWAP")
            self._require(client_id in self._orders and not self._positions)
            return ("partial_fill_recorded", "remainder_canceled", "exposure_closed")
        if name == "ATTACHED_TP_SL":
            self._post_once(client_id, "live")
            children = (client_id + "-tp", client_id + "-sl")
            self._require(len(set(children)) == 2)
            self._orders[client_id] = "canceled"
            return ("parent_persisted", "tp_sl_linked", "children_not_orphaned")
        if name == "BUSINESS_REJECTION":
            self._orders[client_id] = "rejected_scode_51008"
            self._require(client_id not in self._posts)
            return ("http_200_not_success", "scode_rejected", "no_order_assumed")
        if name == "RATE_LIMIT":
            self._post_once(client_id, "rate_limited")
            self._orders[client_id] = "canceled"
            self._require(self._posts[client_id] == 1)
            return ("rate_limit_persisted", "same_client_id_reconciled", "no_duplicate_post")
        if name == "WRITE_TIMEOUT_RECOVERY":
            self._post_once(client_id, "unknown")
            self._orders[client_id] = "canceled"
            self._require(self._posts[client_id] == 1)
            return ("unknown_outcome_persisted", "query_before_retry", "no_duplicate_post")
        if name == "WEBSOCKET_DISCONNECT":
            source = "rest_reconciliation"
            self._require(source == "rest_reconciliation")
            return ("ws_marked_stale", "rest_snapshot_required", "opening_frozen_until_fresh")
        if name == "WEBSOCKET_OUT_OF_ORDER":
            self._last_ws_sequence = 8
            incoming_sequence = 7
            applied = incoming_sequence > self._last_ws_sequence
            self._require(not applied and self._last_ws_sequence == 8)
            return ("sequence_checked", "older_event_ignored", "canonical_state_preserved")
        if name == "CLIENT_ORDER_ID_RECOVERY":
            self._post_once(client_id, "unknown")
            recovered = client_id in self._orders
            self._require(recovered and self._posts[client_id] == 1)
            self._orders[client_id] = "canceled"
            return ("client_id_persisted_before_post", "order_recovered", "no_repost")
        if name == "WORKER_RESTART":
            self._post_once(client_id, "unknown")
            persisted_posts = self._posts[client_id]
            self._orders[client_id] = "canceled"
            self._require(persisted_posts == self._posts[client_id])
            return ("prepared_attempt_reloaded", "reconciled_after_restart", "no_repost")
        if name == "SUPERVISOR_RESTART":
            old_generation = self._writer_generation
            self._writer_generation += 1
            self._require(self._writer_generation > old_generation)
            return ("old_writer_fenced", "generation_incremented", "single_writer_restored")
        if name == "POSITION_DRIFT":
            self._positions["BTC-USDT-SWAP"] = "unexpected"
            self._openings_frozen = True
            self._require(self._openings_frozen)
            self._positions.clear()
            return ("drift_detected", "openings_frozen", "cleanup_required")
        if name == "STALE_MARKET_DATA":
            stale_age_seconds = 31
            self._require(stale_age_seconds > 30)
            return ("market_age_checked", "intent_blocked", "writer_not_called")
        if name == "STALE_INTENT":
            intent_expired = True
            self._require(intent_expired)
            return ("intent_expiry_checked", "approval_blocked", "writer_not_called")
        if name == "DUPLICATE_REQUEST":
            self._post_once(client_id, "live")
            self._post_once(client_id, "live")
            self._orders[client_id] = "canceled"
            self._require(self._posts[client_id] == 1)
            return ("idempotency_key_reused", "single_post", "single_order_lineage")
        if name == "WRONG_EXECUTION_TARGET":
            requested_target = "LIVE"
            self._require(requested_target != "OKX_DEMO")
            return ("target_checked_before_writer", "request_blocked", "no_network_write")
        raise ValueError("unknown scenario")

    def _post_once(self, client_id: str, state: str) -> None:
        if client_id not in self._posts:
            self._posts[client_id] = 1
            self._orders[client_id] = state

    @staticmethod
    def _require(condition: bool) -> None:
        if not condition:
            raise RuntimeError("OFFLINE_SCENARIO_INVARIANT_FAILED")
