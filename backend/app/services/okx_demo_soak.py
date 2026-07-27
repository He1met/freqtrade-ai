from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Mapping, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.execution_lineage import OKX_DEMO_TARGET_ID
from app.models.okx_demo_soak import (
    OkxDemoSoakEvent,
    OkxDemoSoakProbe,
    OkxDemoSoakRun,
)
from app.repositories.execution_lineage import ensure_execution_scope_catalog


MINIMUM_SOAK_SECONDS = 7 * 24 * 60 * 60
DEFAULT_PROBE_INTERVAL_SECONDS = 300
DEFAULT_MAX_PROBE_GAP_SECONDS = 900
OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SoakRunBlocked(RuntimeError):
    status = "BLOCKED"


@dataclass(frozen=True)
class SoakStartGate:
    e2e_evidence_id: str
    e2e_status: str
    execution_target_id: str
    repository_instances: int
    runtime_instances: int
    database_instances: int
    virtualenv_instances: int
    writer_instances: int
    completed_order_lifecycles: int
    reconciled_order_lifecycles: int
    cleanup_cycles: int
    environment_fingerprint: str


@dataclass(frozen=True)
class SoakProbeInput:
    observed_at: datetime
    repository_instances: int
    runtime_instances: int
    database_instances: int
    virtualenv_instances: int
    writer_instances: int
    reconciliation_status: str
    open_orders: int
    open_positions: int
    duplicate_orders: int
    unknown_positions: int
    queue_depth: int
    database_bytes: int
    log_bytes: int
    credentials_exposed: bool
    runtime_healthy: bool
    websocket_healthy: bool
    evidence_refs: Mapping[str, str]


@dataclass(frozen=True)
class SoakFinalEvidence:
    cleanup_completed: bool
    open_orders: int
    open_positions: int
    reconciliation_status: str
    repository_instances: int
    runtime_instances: int
    database_instances: int
    virtualenv_instances: int
    writer_instances: int
    api_evidence_ref: str
    database_evidence_ref: str
    artifact_evidence_ref: str
    okx_orders_evidence_ref: str
    okx_fills_evidence_ref: str
    okx_positions_evidence_ref: str
    runtime_log_evidence_ref: str
    report_sha256: str


@dataclass(frozen=True)
class SoakAssessment:
    status: str
    reason_codes: tuple[str, ...]
    duration_seconds: int
    probe_count: int
    max_observed_probe_gap_seconds: int


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SoakRunBlocked("{} must be timezone-aware".format(name))
    return value.astimezone(timezone.utc)


def _persisted_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_reference(value: str, name: str) -> str:
    if not isinstance(value, str) or OPAQUE_REFERENCE.fullmatch(value) is None:
        raise SoakRunBlocked("{} must be an opaque reference".format(name))
    return value


def _safe_references(values: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(values, Mapping) or len(values) > 16:
        raise SoakRunBlocked("evidence refs are invalid")
    return {
        _safe_reference(str(key), "evidence ref name"): _safe_reference(
            value, "evidence ref value"
        )
        for key, value in sorted(values.items())
    }


def environment_fingerprint(components: Mapping[str, str]) -> str:
    """Hash identities only; paths, URLs and credentials never enter soak evidence."""

    normalized = _safe_references(components)
    payload = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def start_gate_problems(gate: SoakStartGate) -> tuple[str, ...]:
    problems = []
    if gate.e2e_status != "PASSED":
        problems.append("E2E_NOT_PASSED")
    if gate.execution_target_id != OKX_DEMO_TARGET_ID:
        problems.append("EXECUTION_TARGET_MISMATCH")
    cardinalities = (
        gate.repository_instances,
        gate.runtime_instances,
        gate.database_instances,
        gate.virtualenv_instances,
        gate.writer_instances,
    )
    if any(value != 1 for value in cardinalities):
        problems.append("SINGLE_ENVIRONMENT_VIOLATION")
    if gate.completed_order_lifecycles < 1:
        problems.append("ORDER_LIFECYCLE_COVERAGE_MISSING")
    if gate.reconciled_order_lifecycles < 1:
        problems.append("RECONCILIATION_COVERAGE_MISSING")
    if gate.cleanup_cycles < 1:
        problems.append("CLEANUP_COVERAGE_MISSING")
    if OPAQUE_REFERENCE.fullmatch(gate.e2e_evidence_id) is None:
        problems.append("E2E_EVIDENCE_ID_INVALID")
    if SHA256.fullmatch(gate.environment_fingerprint) is None:
        problems.append("ENVIRONMENT_FINGERPRINT_INVALID")
    return tuple(problems)


class OkxDemoSoakService:
    """Durable state machine driven by the existing supervised OKX runtime."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def plan(
        self,
        *,
        environment_fingerprint_sha256: str,
        now: datetime,
        required_duration_seconds: int = MINIMUM_SOAK_SECONDS,
        probe_interval_seconds: int = DEFAULT_PROBE_INTERVAL_SECONDS,
        max_probe_gap_seconds: int = DEFAULT_MAX_PROBE_GAP_SECONDS,
    ) -> OkxDemoSoakRun:
        now = _aware(now, "now")
        if SHA256.fullmatch(environment_fingerprint_sha256) is None:
            raise SoakRunBlocked("environment fingerprint is invalid")
        if required_duration_seconds < MINIMUM_SOAK_SECONDS:
            raise SoakRunBlocked("soak duration cannot be shorter than seven days")
        if (
            probe_interval_seconds < 30
            or probe_interval_seconds > 3600
            or max_probe_gap_seconds < probe_interval_seconds
        ):
            raise SoakRunBlocked("probe cadence is invalid")
        ensure_execution_scope_catalog(self.db)
        active = self.db.scalar(
            select(OkxDemoSoakRun.id).where(
                OkxDemoSoakRun.status.in_(("RUNNING", "RECOVERY_REQUIRED"))
            )
        )
        if active is not None:
            raise SoakRunBlocked("another OKX_DEMO soak run is active")
        run = OkxDemoSoakRun(
            execution_target_id=OKX_DEMO_TARGET_ID,
            status="NOT_RUN",
            operational_state="PLANNED",
            environment_fingerprint=environment_fingerprint_sha256,
            required_duration_seconds=required_duration_seconds,
            probe_interval_seconds=probe_interval_seconds,
            max_probe_gap_seconds=max_probe_gap_seconds,
        )
        self.db.add(run)
        self.db.flush()
        self._event(run, "PLANNED", "AWAITING_START_GATE", now, {})
        return run

    def start(
        self, run_id: int, *, gate: SoakStartGate, now: datetime
    ) -> OkxDemoSoakRun:
        now = _aware(now, "now")
        run = self._run(run_id)
        if run.status not in ("NOT_RUN", "BLOCKED"):
            raise SoakRunBlocked("soak run is not awaiting its start gate")
        problems = start_gate_problems(gate)
        gate_snapshot = asdict(gate)
        gate_snapshot.pop("environment_fingerprint", None)
        run.e2e_evidence_id = (
            gate.e2e_evidence_id
            if OPAQUE_REFERENCE.fullmatch(gate.e2e_evidence_id)
            else None
        )
        run.gate_evidence_json = gate_snapshot
        if gate.environment_fingerprint != run.environment_fingerprint:
            problems = problems + ("ENVIRONMENT_FINGERPRINT_DRIFT",)
        if problems:
            run.status = "BLOCKED"
            run.operational_state = "PLANNED"
            self._event(
                run,
                "BLOCKED",
                problems[0],
                now,
                {"gate_problem_count": str(len(problems))},
            )
            self.db.flush()
            return run
        run.status = "RUNNING"
        run.operational_state = "ACTIVE"
        run.started_at = now
        self._event(
            run,
            "STARTED",
            "E2E_AND_COVERAGE_GATE_PASSED",
            now,
            {"e2e_evidence_id": gate.e2e_evidence_id},
        )
        self.db.flush()
        return run

    def record_probe(
        self, run_id: int, probe: SoakProbeInput
    ) -> OkxDemoSoakProbe:
        observed_at = _aware(probe.observed_at, "probe observed_at")
        run = self._run(run_id)
        if run.status not in ("RUNNING", "RECOVERY_REQUIRED"):
            raise SoakRunBlocked("probe requires an active soak run")
        previous = self.db.scalars(
            select(OkxDemoSoakProbe)
            .where(OkxDemoSoakProbe.soak_run_id == run.id)
            .order_by(OkxDemoSoakProbe.sequence.desc())
            .limit(1)
        ).first()
        if run.started_at is None or observed_at < _persisted_aware(run.started_at):
            raise SoakRunBlocked("probe predates the soak run")
        if previous is not None and observed_at <= _persisted_aware(previous.observed_at):
            raise SoakRunBlocked("probe time must increase monotonically")
        sequence = 1 if previous is None else previous.sequence + 1
        row = OkxDemoSoakProbe(
            soak_run_id=run.id,
            execution_target_id=OKX_DEMO_TARGET_ID,
            sequence=sequence,
            observed_at=observed_at,
            repository_instances=probe.repository_instances,
            runtime_instances=probe.runtime_instances,
            database_instances=probe.database_instances,
            virtualenv_instances=probe.virtualenv_instances,
            writer_instances=probe.writer_instances,
            reconciliation_status=probe.reconciliation_status,
            open_orders=probe.open_orders,
            open_positions=probe.open_positions,
            duplicate_orders=probe.duplicate_orders,
            unknown_positions=probe.unknown_positions,
            queue_depth=probe.queue_depth,
            database_bytes=probe.database_bytes,
            log_bytes=probe.log_bytes,
            credentials_exposed=probe.credentials_exposed,
            runtime_healthy=probe.runtime_healthy,
            websocket_healthy=probe.websocket_healthy,
            evidence_refs_json=_safe_references(probe.evidence_refs),
        )
        self.db.add(row)
        self.db.flush()
        reasons = self._probe_failure_reasons(run, previous, probe, observed_at)
        self._event(
            run,
            "PROBE",
            "HEALTHY" if not reasons else reasons[0],
            observed_at,
            {"probe_id": str(row.id), "probe_sequence": str(sequence)},
        )
        terminal_reasons = tuple(
            reason
            for reason in reasons
            if reason in ("CREDENTIAL_EXPOSURE", "DUPLICATE_ORDER")
        )
        if terminal_reasons:
            self._fail(
                run,
                terminal_reasons[0],
                observed_at,
                {"probe_id": str(row.id)},
            )
        elif reasons:
            run.status = "RECOVERY_REQUIRED"
            run.operational_state = "FROZEN"
            self._event(
                run,
                "FROZEN",
                reasons[0],
                observed_at,
                {"probe_id": str(row.id)},
            )
            self._event(
                run,
                "RECOVERY_REQUIRED",
                reasons[0],
                observed_at,
                {"probe_id": str(row.id)},
            )
        self.db.flush()
        return row

    def begin_recovery(
        self, run_id: int, *, recovery_evidence_ref: str, now: datetime
    ) -> OkxDemoSoakRun:
        now = _aware(now, "now")
        run = self._run(run_id)
        if run.status != "RECOVERY_REQUIRED" or run.operational_state != "FROZEN":
            raise SoakRunBlocked("recovery requires a frozen run")
        run.operational_state = "RECONCILING"
        self._event(
            run,
            "RECONCILING",
            "OPENINGS_REMAIN_FROZEN",
            now,
            {"recovery_evidence_ref": _safe_reference(
                recovery_evidence_ref, "recovery evidence ref"
            )},
        )
        run.operational_state = "RECOVERING"
        self._event(
            run,
            "RECOVERY_STARTED",
            "CANCEL_REDUCE_ONLY_PATH",
            now,
            {"recovery_evidence_ref": recovery_evidence_ref},
        )
        self.db.flush()
        return run

    def complete_recovery(
        self,
        run_id: int,
        *,
        reconciliation_status: str,
        open_orders: int,
        unknown_positions: int,
        recovery_evidence_ref: str,
        now: datetime,
    ) -> OkxDemoSoakRun:
        now = _aware(now, "now")
        run = self._run(run_id)
        if run.status != "RECOVERY_REQUIRED" or run.operational_state != "RECOVERING":
            raise SoakRunBlocked("recovery completion is out of sequence")
        if (
            reconciliation_status not in ("RECONCILED", "RECOVERED")
            or open_orders != 0
            or unknown_positions != 0
        ):
            self._fail(
                run,
                "RECOVERY_EVIDENCE_INCOMPLETE",
                now,
                {"recovery_evidence_ref": _safe_reference(
                    recovery_evidence_ref, "recovery evidence ref"
                )},
            )
            return run
        run.status = "RUNNING"
        run.operational_state = "ACTIVE"
        self._event(
            run,
            "RECOVERED",
            "RECONCILED_AND_CLEAN",
            now,
            {"recovery_evidence_ref": _safe_reference(
                recovery_evidence_ref, "recovery evidence ref"
            )},
        )
        self.db.flush()
        return run

    def fail(
        self, run_id: int, *, reason_code: str, evidence_refs: Mapping[str, str], now: datetime
    ) -> OkxDemoSoakRun:
        run = self._run(run_id)
        self._fail(
            run,
            _safe_reference(reason_code, "failure reason code"),
            _aware(now, "now"),
            _safe_references(evidence_refs),
        )
        return run

    def assess(self, run_id: int, *, now: datetime) -> SoakAssessment:
        run = self._run(run_id)
        now = _aware(now, "now")
        probes = list(
            self.db.scalars(
                select(OkxDemoSoakProbe)
                .where(OkxDemoSoakProbe.soak_run_id == run.id)
                .order_by(OkxDemoSoakProbe.observed_at, OkxDemoSoakProbe.id)
            ).all()
        )
        reasons = []
        if run.status == "NOT_RUN":
            reasons.append("NOT_STARTED")
        elif run.status == "BLOCKED":
            reasons.append("START_GATE_BLOCKED")
        elif run.status == "FAILED":
            reasons.append("RUN_FAILED")
        elif run.status == "RECOVERY_REQUIRED":
            reasons.append("RECOVERY_REQUIRED")
        if run.started_at is None:
            duration = 0
        else:
            duration = max(0, int((now - _persisted_aware(run.started_at)).total_seconds()))
            if duration < run.required_duration_seconds:
                reasons.append("DURATION_INCOMPLETE")
        max_gap = self._max_probe_gap(run, probes, now)
        if not probes:
            reasons.append("PROBES_MISSING")
        elif max_gap > run.max_probe_gap_seconds:
            reasons.append("PROBE_GAP_EXCEEDED")
        return SoakAssessment(
            status=run.status,
            reason_codes=tuple(dict.fromkeys(reasons)),
            duration_seconds=duration,
            probe_count=len(probes),
            max_observed_probe_gap_seconds=max_gap,
        )

    def finalize(
        self, run_id: int, *, evidence: SoakFinalEvidence, now: datetime
    ) -> OkxDemoSoakRun:
        now = _aware(now, "now")
        run = self._run(run_id)
        if run.status != "RUNNING" or run.operational_state != "ACTIVE":
            raise SoakRunBlocked("only a healthy running soak can finalize")
        run.operational_state = "CLEANUP"
        self._event(run, "CLEANUP_STARTED", "CONTROLLED_STOP", now, {})
        assessment = self.assess(run.id, now=now)
        final_problems = list(assessment.reason_codes)
        cardinalities = (
            evidence.repository_instances,
            evidence.runtime_instances,
            evidence.database_instances,
            evidence.virtualenv_instances,
            evidence.writer_instances,
        )
        if any(value != 1 for value in cardinalities):
            final_problems.append("SINGLE_ENVIRONMENT_VIOLATION")
        if (
            not evidence.cleanup_completed
            or evidence.open_orders != 0
            or evidence.open_positions != 0
        ):
            final_problems.append("FINAL_CLEANUP_INCOMPLETE")
        if evidence.reconciliation_status not in ("RECONCILED", "RECOVERED"):
            final_problems.append("FINAL_RECONCILIATION_INCOMPLETE")
        if SHA256.fullmatch(evidence.report_sha256) is None:
            final_problems.append("REPORT_DIGEST_INVALID")
        refs = {
            name: _safe_reference(getattr(evidence, name), name)
            for name in (
                "api_evidence_ref",
                "database_evidence_ref",
                "artifact_evidence_ref",
                "okx_orders_evidence_ref",
                "okx_fills_evidence_ref",
                "okx_positions_evidence_ref",
                "runtime_log_evidence_ref",
            )
        }
        if final_problems:
            self._fail(
                run,
                final_problems[0],
                now,
                {"problem_count": str(len(final_problems)), **refs},
            )
            return run
        run.final_evidence_json = {
            **refs,
            "cleanup_completed": True,
            "open_orders": 0,
            "open_positions": 0,
            "reconciliation_status": evidence.reconciliation_status,
            "report_sha256": evidence.report_sha256,
            "probe_count": assessment.probe_count,
            "duration_seconds": assessment.duration_seconds,
            "max_observed_probe_gap_seconds": assessment.max_observed_probe_gap_seconds,
        }
        run.status = "PASSED"
        run.operational_state = "STOPPED"
        run.completed_at = now
        self._event(
            run,
            "PASSED",
            "SEVEN_DAY_EVIDENCE_AND_CLEANUP_VERIFIED",
            now,
            {"report_sha256": evidence.report_sha256, **refs},
        )
        self.db.flush()
        return run

    def _probe_failure_reasons(
        self,
        run: OkxDemoSoakRun,
        previous: Optional[OkxDemoSoakProbe],
        probe: SoakProbeInput,
        observed_at: datetime,
    ) -> tuple[str, ...]:
        reasons = []
        if any(
            value != 1
            for value in (
                probe.repository_instances,
                probe.runtime_instances,
                probe.database_instances,
                probe.virtualenv_instances,
                probe.writer_instances,
            )
        ):
            reasons.append("SINGLE_ENVIRONMENT_VIOLATION")
        if probe.credentials_exposed:
            reasons.append("CREDENTIAL_EXPOSURE")
        if probe.duplicate_orders:
            reasons.append("DUPLICATE_ORDER")
        if probe.unknown_positions:
            reasons.append("UNKNOWN_POSITION")
        if probe.reconciliation_status not in ("RECONCILED", "RECOVERED"):
            reasons.append("RECONCILIATION_DRIFT")
        if not probe.runtime_healthy:
            reasons.append("RUNTIME_UNHEALTHY")
        if not probe.websocket_healthy:
            reasons.append("WEBSOCKET_UNHEALTHY")
        previous_time = (
            _persisted_aware(previous.observed_at)
            if previous is not None
            else _persisted_aware(run.started_at)
        )
        if int((observed_at - previous_time).total_seconds()) > run.max_probe_gap_seconds:
            reasons.append("PROBE_GAP_EXCEEDED")
        return tuple(reasons)

    def _max_probe_gap(
        self,
        run: OkxDemoSoakRun,
        probes: list[OkxDemoSoakProbe],
        end: datetime,
    ) -> int:
        if run.started_at is None:
            return 0
        points = [_persisted_aware(run.started_at)]
        points.extend(_persisted_aware(probe.observed_at) for probe in probes)
        points.append(end)
        return max(
            int((right - left).total_seconds())
            for left, right in zip(points, points[1:])
        )

    def _event(
        self,
        run: OkxDemoSoakRun,
        event_type: str,
        reason_code: str,
        occurred_at: datetime,
        evidence_refs: Mapping[str, str],
    ) -> OkxDemoSoakEvent:
        previous_time = self.db.scalar(
            select(func.max(OkxDemoSoakEvent.occurred_at)).where(
                OkxDemoSoakEvent.soak_run_id == run.id
            )
        )
        if (
            previous_time is not None
            and occurred_at < _persisted_aware(previous_time)
        ):
            raise SoakRunBlocked("event time must increase monotonically")
        sequence = (
            self.db.scalar(
                select(func.max(OkxDemoSoakEvent.sequence)).where(
                    OkxDemoSoakEvent.soak_run_id == run.id
                )
            )
            or 0
        ) + 1
        event = OkxDemoSoakEvent(
            soak_run_id=run.id,
            execution_target_id=OKX_DEMO_TARGET_ID,
            sequence=sequence,
            event_type=event_type,
            reason_code=_safe_reference(reason_code, "event reason code"),
            occurred_at=occurred_at,
            evidence_refs_json=_safe_references(evidence_refs),
        )
        self.db.add(event)
        self.db.flush()
        return event

    def _fail(
        self,
        run: OkxDemoSoakRun,
        reason_code: str,
        now: datetime,
        evidence_refs: Mapping[str, str],
    ) -> None:
        if run.status in ("FAILED", "PASSED"):
            raise SoakRunBlocked("terminal soak run cannot transition")
        run.status = "FAILED"
        run.operational_state = "STOPPED"
        run.completed_at = now
        self._event(run, "FAILED", reason_code, now, evidence_refs)
        self.db.flush()

    def _run(self, run_id: int) -> OkxDemoSoakRun:
        run = self.db.get(OkxDemoSoakRun, run_id)
        if run is None or run.execution_target_id != OKX_DEMO_TARGET_ID:
            raise SoakRunBlocked("OKX_DEMO soak run was not found")
        return run
