from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.canonical_v13.phase9_runtime_supervisor import (
    CanonicalPhase9SupervisorBlocked,
    Phase9Lease,
    RuntimeImagePlanAuthority,
    build_launch_plan,
    build_lifecycle_receipt,
    build_production_runtime_observation,
    validate_supervised_worker_receipt,
)
from app.canonical_v13.phase9_runtime_worker import (
    ActiveRuntimeLineage,
    CanonicalPhase9RuntimeWorker,
    CanonicalPhase9RuntimeWorkerBlocked,
    NaturalMarketEvidence,
    NaturalSignalEvaluation,
    natural_market_evidence_digest,
    verify_runtime_worker_receipt,
)
from app.canonical_v13.runtime_contract import (
    FrozenRuntimeLaunchSpec,
    verify_runtime_observation_receipt,
)


NOW = datetime(2026, 8, 21, 2, 3, 4, tzinfo=timezone.utc)
PLAN_DIGEST = "a" * 64
RELEASE_DIGEST = "8" * 64
IMAGE_DIGEST = "9" * 64
IMAGE_ACCEPTANCE_ID = UUID("00000000-0000-4000-8000-000000000099")
IMAGE_ACCEPTANCE_RECEIPT = "7" * 64
SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "canonical_v13_phase9_service.py"
)


def _uuid(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def _runtime_image_authority() -> RuntimeImagePlanAuthority:
    return RuntimeImagePlanAuthority(
        acceptance_id=IMAGE_ACCEPTANCE_ID,
        image_manifest_digest=IMAGE_DIGEST,
        image_config_digest="8" * 64,
        acceptance_receipt_digest=IMAGE_ACCEPTANCE_RECEIPT,
        release_digest=RELEASE_DIGEST,
    )


def _lineage() -> ActiveRuntimeLineage:
    return ActiveRuntimeLineage(
        qualification_decision_id=_uuid(11),
        qualification_decision_digest="6" * 64,
        deployment_approval_id=_uuid(12),
        deployment_approval_digest="7" * 64,
        deployment_id=_uuid(1),
        runtime_instance_id=_uuid(2),
        strategy_version_id=_uuid(3),
        research_target_id=_uuid(4),
        configuration_bundle_id=_uuid(5),
        configuration_bundle_digest="1" * 64,
        market_snapshot_id=_uuid(6),
        market_snapshot_digest="2" * 64,
        deployment_capability_digest="3" * 64,
        runtime_launch_spec_digest="4" * 64,
        runtime_receipt_digest="5" * 64,
        runtime_receipt_observed_at=NOW - timedelta(seconds=5),
        runtime_identity="canonical-v13-long-lived-runtime-v1",
        runtime_service_account="canonical_runtime_reader",
        deployment_status="ACTIVE",
        runtime_status="HEALTHY",
        runtime_evidence_class="PRODUCTION_DEMO_RUNTIME",
        demo_only=True,
        allow_real_funds=False,
        runtime_order_writer_capability=False,
    )


class LineageReader:
    def __init__(self, lineage: ActiveRuntimeLineage | None = None) -> None:
        self.lineage = lineage or _lineage()
        self.calls = 0

    def read_active_runtime_lineage(self) -> ActiveRuntimeLineage:
        self.calls += 1
        return self.lineage


class EvidencePort:
    def __init__(self, evidence: NaturalMarketEvidence | None = None) -> None:
        payload = {"close": "64000.0", "closed_candle": True, "source": "injected"}
        self.evidence = evidence or _evidence(
            evidence_id="market-evidence-1",
            observed_at=NOW - timedelta(seconds=10),
            payload=payload,
        )
        self.calls = 0

    def read_market_evidence(self, **_kwargs) -> NaturalMarketEvidence:
        self.calls += 1
        return self.evidence


class Evaluator:
    def __init__(
        self, outcome: str = "SIGNAL", *, evaluated_at: datetime = NOW
    ) -> None:
        self.outcome = outcome
        self.evaluated_at = evaluated_at
        self.calls = 0

    def evaluate_natural_signal(self, **_kwargs) -> NaturalSignalEvaluation:
        self.calls += 1
        return NaturalSignalEvaluation(
            outcome=self.outcome,
            evaluated_at=self.evaluated_at,
            evaluator_identity="canonical-natural-signal-evaluator-v1",
            evaluation_payload={"direction": "LONG", "rule": "closed-candle-cross"},
        )


class SignerVerifier:
    key_id = "canonical-runtime-worker-signing-v1"
    algorithm = "TEST_SHA256_SIGNATURE"

    def sign_digest(self, digest: str) -> str:
        return sha256(f"signed:{digest}".encode()).hexdigest()

    def verify_digest(
        self, *, key_id: str, algorithm: str, digest: str, signature: str
    ) -> bool:
        return (
            key_id == self.key_id
            and algorithm == self.algorithm
            and signature == self.sign_digest(digest)
        )


def _worker(
    *,
    lineage: ActiveRuntimeLineage | None = None,
    evidence: NaturalMarketEvidence | None = None,
    outcome: str = "SIGNAL",
):
    reader = LineageReader(lineage)
    evidence_port = EvidencePort(evidence)
    evaluator = Evaluator(outcome)
    signer = SignerVerifier()
    return (
        CanonicalPhase9RuntimeWorker(
            lineage_reader=reader,
            market_evidence=evidence_port,
            evaluator=evaluator,
            signer=signer,
        ),
        reader,
        evidence_port,
        evaluator,
        signer,
    )


def _evidence(
    *, evidence_id: str, observed_at: datetime, payload: dict[str, object]
) -> NaturalMarketEvidence:
    draft = NaturalMarketEvidence(
        evidence_id=evidence_id,
        evidence_digest="0" * 64,
        instrument="BTC-USDT-SWAP",
        observed_at=observed_at,
        payload=payload,
    )
    return replace(draft, evidence_digest=natural_market_evidence_digest(draft))


def test_no_order_soak_never_reads_market_or_evaluates_signal() -> None:
    worker, reader, evidence, evaluator, signer = _worker()
    receipt = worker.heartbeat(
        stage="NO_ORDER_SOAK", plan_digest=PLAN_DIGEST, observed_at=NOW
    )

    assert reader.calls == 1
    assert evidence.calls == evaluator.calls == 0
    assert receipt.status == "HEALTHY"
    assert receipt.reason_code == "NO_ORDER_SOAK_SIGNAL_EVALUATION_DISABLED"
    assert receipt.signal_candidate is None
    assert receipt.signal_candidate_digest is None
    assert receipt.order_submission_enabled is False
    assert receipt.persistence_target == "canonical_signal_writer"
    assert verify_runtime_worker_receipt(receipt, verifier=signer)


@pytest.mark.parametrize("stage", ["SIGNAL_RISK_SHADOW", "OKX_DEMO_CANARY"])
def test_signal_stages_emit_signed_exact_lineage_candidate_without_persistence(
    stage: str,
) -> None:
    worker, reader, evidence, evaluator, signer = _worker()
    receipt = worker.heartbeat(stage=stage, plan_digest=PLAN_DIGEST, observed_at=NOW)

    assert reader.calls == evidence.calls == evaluator.calls == 1
    assert receipt.reason_code == "NATURAL_SIGNAL_CANDIDATE_READY"
    assert receipt.order_submission_enabled is False
    candidate = receipt.signal_candidate
    assert candidate is not None
    assert candidate.deployment_id == _lineage().deployment_id
    assert candidate.runtime_instance_id == _lineage().runtime_instance_id
    assert candidate.qualification_decision_id == _lineage().qualification_decision_id
    assert candidate.deployment_approval_id == _lineage().deployment_approval_id
    assert candidate.configuration_bundle_digest == "1" * 64
    assert candidate.market_snapshot_digest == "2" * 64
    assert candidate.runtime_receipt_digest == "5" * 64
    assert candidate.deployment_capability_digest == "3" * 64
    assert candidate.runtime_launch_spec_digest == "4" * 64
    assert candidate.signal_json["evidence_class"] == "PRODUCTION_OKX_DEMO"
    assert candidate.signal_json["natural_signal"] is True
    assert candidate.signal_json["allow_real_funds"] is False
    assert receipt.signal_candidate_digest == candidate.candidate_digest
    assert verify_runtime_worker_receipt(receipt, verifier=signer)
    assert not hasattr(worker, "connection")
    assert not hasattr(worker, "signal_writer")
    assert not hasattr(worker, "order_writer")


def test_no_action_emits_signed_heartbeat_without_signal_candidate() -> None:
    worker, _reader, evidence, evaluator, signer = _worker(outcome="NO_ACTION")
    receipt = worker.heartbeat(
        stage="SIGNAL_RISK_SHADOW", plan_digest=PLAN_DIGEST, observed_at=NOW
    )
    assert evidence.calls == evaluator.calls == 1
    assert receipt.reason_code == "NATURAL_SIGNAL_NO_ACTION"
    assert receipt.signal_candidate is None
    assert verify_runtime_worker_receipt(receipt, verifier=signer)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deployment_status", "PENDING"),
        ("runtime_status", "DEGRADED"),
        ("runtime_evidence_class", "TEST_SIMULATED"),
        ("demo_only", False),
        ("allow_real_funds", True),
        ("runtime_order_writer_capability", True),
        ("runtime_service_account", "canonical_order_writer"),
        ("runtime_identity", "legacy-runtime"),
    ],
)
def test_runtime_lineage_drift_fails_before_market_evaluation(
    field: str, value: object
) -> None:
    worker, _reader, evidence, evaluator, _signer = _worker(
        lineage=replace(_lineage(), **{field: value})
    )
    with pytest.raises(
        CanonicalPhase9RuntimeWorkerBlocked, match="BLOCKED_RUNTIME_WORKER_LINEAGE"
    ):
        worker.heartbeat(
            stage="SIGNAL_RISK_SHADOW",
            plan_digest=PLAN_DIGEST,
            observed_at=NOW,
        )
    assert evidence.calls == evaluator.calls == 0


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence(
            evidence_id="market-evidence-stale",
            observed_at=NOW - timedelta(minutes=3),
            payload={"close": "1"},
        ),
        _evidence(
            evidence_id="market-evidence-future",
            observed_at=NOW + timedelta(seconds=1),
            payload={"close": "1"},
        ),
        NaturalMarketEvidence(
            evidence_id="market-evidence-drift",
            evidence_digest="f" * 64,
            instrument="BTC-USDT-SWAP",
            observed_at=NOW,
            payload={"close": "1"},
        ),
    ],
)
def test_stale_future_or_digest_drifted_market_evidence_fails_closed(
    evidence: NaturalMarketEvidence,
) -> None:
    worker, _reader, _evidence, evaluator, _signer = _worker(evidence=evidence)
    with pytest.raises(
        CanonicalPhase9RuntimeWorkerBlocked,
        match="BLOCKED_RUNTIME_WORKER_MARKET_EVIDENCE",
    ):
        worker.heartbeat(
            stage="SIGNAL_RISK_SHADOW",
            plan_digest=PLAN_DIGEST,
            observed_at=NOW,
        )
    assert evaluator.calls == 0


def test_stale_or_future_runtime_heartbeat_fails_before_market_read() -> None:
    for heartbeat_at in (
        NOW - timedelta(minutes=6),
        NOW + timedelta(seconds=1),
    ):
        worker, _reader, evidence, evaluator, _signer = _worker(
            lineage=replace(_lineage(), runtime_receipt_observed_at=heartbeat_at)
        )
        with pytest.raises(
            CanonicalPhase9RuntimeWorkerBlocked,
            match="BLOCKED_RUNTIME_WORKER_HEARTBEAT",
        ):
            worker.heartbeat(
                stage="SIGNAL_RISK_SHADOW",
                plan_digest=PLAN_DIGEST,
                observed_at=NOW,
            )
        assert evidence.calls == evaluator.calls == 0


def test_long_lived_worker_pins_successfully_accepted_runtime_receipt() -> None:
    worker, reader, evidence, evaluator, _signer = _worker(outcome="NO_ACTION")
    first = worker.heartbeat(
        stage="SIGNAL_RISK_SHADOW", plan_digest=PLAN_DIGEST, observed_at=NOW
    )
    later = NOW + timedelta(minutes=6)
    evidence.evidence = _evidence(
        evidence_id="market-evidence-2",
        observed_at=later - timedelta(seconds=10),
        payload={"close": "64001.0", "closed_candle": True, "source": "injected"},
    )
    evaluator.evaluated_at = later

    repeated = worker.heartbeat(
        stage="SIGNAL_RISK_SHADOW", plan_digest=PLAN_DIGEST, observed_at=later
    )

    assert first.runtime_receipt_digest == repeated.runtime_receipt_digest
    assert repeated.reason_code == "NATURAL_SIGNAL_NO_ACTION"
    assert reader.calls == evidence.calls == evaluator.calls == 2


def test_long_lived_worker_rejects_stale_replacement_runtime_receipt() -> None:
    worker, reader, evidence, evaluator, _signer = _worker(outcome="NO_ACTION")
    worker.heartbeat(
        stage="SIGNAL_RISK_SHADOW", plan_digest=PLAN_DIGEST, observed_at=NOW
    )
    later = NOW + timedelta(minutes=6)
    reader.lineage = replace(
        reader.lineage,
        runtime_receipt_digest="6" * 64,
        runtime_receipt_observed_at=NOW,
    )

    with pytest.raises(
        CanonicalPhase9RuntimeWorkerBlocked,
        match="BLOCKED_RUNTIME_WORKER_HEARTBEAT",
    ):
        worker.heartbeat(
            stage="SIGNAL_RISK_SHADOW", plan_digest=PLAN_DIGEST, observed_at=later
        )

    assert reader.calls == 2
    assert evidence.calls == evaluator.calls == 1


def test_long_lived_worker_rejects_future_pinned_runtime_receipt() -> None:
    worker, reader, evidence, evaluator, _signer = _worker(outcome="NO_ACTION")
    worker.heartbeat(
        stage="SIGNAL_RISK_SHADOW", plan_digest=PLAN_DIGEST, observed_at=NOW
    )
    reader.lineage = replace(
        reader.lineage,
        runtime_receipt_observed_at=NOW + timedelta(minutes=2),
    )

    with pytest.raises(
        CanonicalPhase9RuntimeWorkerBlocked,
        match="BLOCKED_RUNTIME_WORKER_HEARTBEAT",
    ):
        worker.heartbeat(
            stage="SIGNAL_RISK_SHADOW",
            plan_digest=PLAN_DIGEST,
            observed_at=NOW + timedelta(minutes=1),
        )

    assert reader.calls == 2
    assert evidence.calls == evaluator.calls == 1


def test_failed_heartbeat_does_not_pin_runtime_receipt() -> None:
    stale_evidence = _evidence(
        evidence_id="market-evidence-stale",
        observed_at=NOW - timedelta(minutes=3),
        payload={"close": "64000.0", "closed_candle": True, "source": "injected"},
    )
    worker, _reader, evidence, evaluator, _signer = _worker(
        evidence=stale_evidence, outcome="NO_ACTION"
    )
    with pytest.raises(
        CanonicalPhase9RuntimeWorkerBlocked,
        match="BLOCKED_RUNTIME_WORKER_MARKET_EVIDENCE",
    ):
        worker.heartbeat(
            stage="SIGNAL_RISK_SHADOW", plan_digest=PLAN_DIGEST, observed_at=NOW
        )

    later = NOW + timedelta(minutes=6)
    evidence.evidence = _evidence(
        evidence_id="market-evidence-fresh",
        observed_at=later - timedelta(seconds=10),
        payload={"close": "64001.0", "closed_candle": True, "source": "injected"},
    )
    evaluator.evaluated_at = later
    with pytest.raises(
        CanonicalPhase9RuntimeWorkerBlocked,
        match="BLOCKED_RUNTIME_WORKER_HEARTBEAT",
    ):
        worker.heartbeat(
            stage="SIGNAL_RISK_SHADOW", plan_digest=PLAN_DIGEST, observed_at=later
        )


def test_signature_or_candidate_tamper_is_rejected() -> None:
    worker, _reader, _evidence, _evaluator, signer = _worker()
    receipt = worker.heartbeat(
        stage="SIGNAL_RISK_SHADOW", plan_digest=PLAN_DIGEST, observed_at=NOW
    )
    assert not verify_runtime_worker_receipt(
        replace(receipt, signature="tampered"), verifier=signer
    )
    assert not verify_runtime_worker_receipt(
        replace(receipt, signal_candidate_digest="f" * 64), verifier=signer
    )
    assert receipt.signal_candidate is not None
    assert not verify_runtime_worker_receipt(
        replace(
            receipt,
            signal_candidate=replace(
                receipt.signal_candidate,
                deployment_id=_uuid(99),
            ),
        ),
        verifier=signer,
    )


class WorkerSupervisorAdapter:
    def __init__(self, worker, verifier) -> None:
        self.worker = worker
        self.verifier = verifier

    def heartbeat(self, **kwargs):
        return self.worker.heartbeat(**kwargs)

    def verify(self, receipt) -> bool:
        return verify_runtime_worker_receipt(receipt, verifier=self.verifier)


def test_supervisor_accepts_only_exact_verified_worker_receipt() -> None:
    plan = build_launch_plan(
        service_key="long_lived_runtime",
        stage="SIGNAL_RISK_SHADOW",
        generation=1,
        prepared_at=NOW,
        release_digest=RELEASE_DIGEST,
        deployment_id=_lineage().deployment_id,
        deployment_capability_digest=_lineage().deployment_capability_digest,
        runtime_image_authority=_runtime_image_authority(),
        plan_id=_uuid(10),
    )
    worker, _reader, _evidence, _evaluator, signer = _worker()
    port = WorkerSupervisorAdapter(worker, signer)
    receipt = port.heartbeat(
        stage=plan.stage, plan_digest=plan.plan_digest, observed_at=NOW
    )
    validate_supervised_worker_receipt(plan=plan, receipt=receipt, port=port)

    with pytest.raises(
        CanonicalPhase9SupervisorBlocked, match="BLOCKED_PHASE9_WORKER_RECEIPT"
    ):
        validate_supervised_worker_receipt(
            plan=plan,
            receipt=replace(receipt, plan_digest="f" * 64),
            port=port,
        )


def _release_bound_plan():
    return build_launch_plan(
        service_key="long_lived_runtime",
        stage="NO_ORDER_SOAK",
        generation=7,
        prepared_at=NOW - timedelta(minutes=1),
        release_digest=RELEASE_DIGEST,
        deployment_id=_lineage().deployment_id,
        deployment_capability_digest=_lineage().deployment_capability_digest,
        runtime_image_authority=_runtime_image_authority(),
        plan_id=_uuid(20),
    )


def _launch_spec(plan) -> FrozenRuntimeLaunchSpec:
    lineage = _lineage()
    return FrozenRuntimeLaunchSpec(
        deployment_id=lineage.deployment_id,
        approval_id=lineage.deployment_approval_id,
        qualification_decision_id=lineage.qualification_decision_id,
        strategy_version_id=lineage.strategy_version_id,
        configuration_bundle_id=lineage.configuration_bundle_id,
        configuration_bundle_digest=lineage.configuration_bundle_digest,
        market_snapshot_id=lineage.market_snapshot_id,
        market_snapshot_digest=lineage.market_snapshot_digest,
        deployment_capability_digest=lineage.deployment_capability_digest,
        runtime_identity=plan.process_identity,
        image_digest=IMAGE_DIGEST,
        service_account="canonical_runtime_reader",
        network_policy="DEMO_EXCHANGE_ONLY",
        credential_reference="keychain-reference-name-only",
    )


def _running_evidence(plan):
    holder_digest = "d" * 64
    lease = Phase9Lease(
        service_key="long_lived_runtime",
        generation=plan.generation,
        plan_digest=plan.plan_digest,
        release_digest=plan.release_digest,
        deployment_id=plan.deployment_id,
        deployment_capability_digest=plan.deployment_capability_digest,
        image_digest=plan.image_digest,
        runtime_image_acceptance_id=plan.runtime_image_acceptance_id,
        runtime_image_acceptance_receipt_digest=plan.runtime_image_acceptance_receipt_digest,
        runtime_image_config_digest=plan.runtime_image_config_digest,
        holder_token_digest=holder_digest,
        pid=4321,
        acquired_at=NOW - timedelta(seconds=10),
        heartbeat_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=34),
    )
    receipt = build_lifecycle_receipt(
        service_key="long_lived_runtime",
        action="HEARTBEAT",
        status="RUNNING",
        generation=plan.generation,
        observed_at=NOW - timedelta(seconds=1),
        plan_digest=plan.plan_digest,
        holder_token_digest=holder_digest,
        details={
            "pid": 4321,
            "expires_at": lease.expires_at,
            "release_digest": plan.release_digest,
            "deployment_id": str(plan.deployment_id),
            "deployment_capability_digest": plan.deployment_capability_digest,
            "image_digest": plan.image_digest,
            "runtime_image_acceptance_id": str(plan.runtime_image_acceptance_id),
            "runtime_image_acceptance_receipt_digest": plan.runtime_image_acceptance_receipt_digest,
            "runtime_image_config_digest": plan.runtime_image_config_digest,
        },
    )
    return lease, receipt


def test_verified_running_release_bound_plan_builds_production_runtime_observation() -> (
    None
):
    plan = _release_bound_plan()
    lease, running = _running_evidence(plan)
    observation = build_production_runtime_observation(
        plan=plan,
        launch_spec=_launch_spec(plan),
        runtime_instance_id=_lineage().runtime_instance_id,
        lease=lease,
        running_receipt=running,
        observed_at=NOW,
    )
    assert observation.status == "HEALTHY"
    assert observation.evidence_class == "PRODUCTION_DEMO_RUNTIME"
    assert observation.capability_digest == plan.deployment_capability_digest
    assert observation.order_writer_capability is False
    assert verify_runtime_observation_receipt(observation)


@pytest.mark.parametrize("drift", ["image", "capability", "lease", "release"])
def test_production_runtime_observation_blocks_plan_launch_or_lease_drift(
    drift: str,
) -> None:
    plan = _release_bound_plan()
    launch_spec = _launch_spec(plan)
    lease, running = _running_evidence(plan)
    if drift == "image":
        launch_spec = replace(launch_spec, image_digest="f" * 64)
    elif drift == "capability":
        launch_spec = replace(launch_spec, deployment_capability_digest="e" * 64)
    elif drift == "lease":
        lease = replace(lease, generation=plan.generation + 1)
    else:
        plan = replace(plan, release_digest="f" * 64)
    with pytest.raises(CanonicalPhase9SupervisorBlocked):
        build_production_runtime_observation(
            plan=plan,
            launch_spec=launch_spec,
            runtime_instance_id=_lineage().runtime_instance_id,
            lease=lease,
            running_receipt=running,
            observed_at=NOW,
        )


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_supervisor_runtime_defaults_fail_closed_without_composed_worker(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_worker_unset_test")
    monkeypatch.setattr(service, "SUPPORT_ROOT", tmp_path / "support")
    monkeypatch.setattr(service, "LAUNCH_AGENT_ROOT", tmp_path / "agents")
    monkeypatch.setattr(service, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(service, "_require_release_checkout", lambda: RELEASE_DIGEST)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_load_runtime_image_authority", lambda _id: _runtime_image_authority())
    prepared = service.prepare(
        "long_lived_runtime",
        "SIGNAL_RISK_SHADOW",
        release_digest=RELEASE_DIGEST,
        deployment_id=_lineage().deployment_id,
        deployment_capability_digest=_lineage().deployment_capability_digest,
        runtime_image_acceptance_id=IMAGE_ACCEPTANCE_ID,
        enable_order_writer=False,
    )
    plan, state = service._load_plan("long_lived_runtime")
    service._atomic_json(
        service._state_path("long_lived_runtime"),
        {**state, "status": "CONFIRMED", "confirmed_at": NOW.isoformat()},
    )
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_PHASE9_RUNTIME_WORKER_UNSET",
    ):
        service.supervise("long_lived_runtime", plan.plan_digest)
    assert (
        service.FileLeasePort(service.SUPPORT_ROOT).read("long_lived_runtime") is None
    )
    receipts = [
        json.loads(line)
        for line in service._receipt_path("long_lived_runtime").read_text().splitlines()
    ]
    assert receipts[-1]["status"] == "BLOCKED"
    assert receipts[-1]["details"] == {
        "order_submission_enabled": False,
        "reason_code": "RUNTIME_WORKER_PORT_UNSET",
    }
    assert prepared["plan_digest"] == plan.plan_digest


def test_no_order_soak_supervisor_needs_no_market_worker(monkeypatch, tmp_path) -> None:
    service = _load_script("canonical_phase9_no_order_supervisor_test")
    monkeypatch.setattr(service, "SUPPORT_ROOT", tmp_path / "support")
    monkeypatch.setattr(service, "LAUNCH_AGENT_ROOT", tmp_path / "agents")
    monkeypatch.setattr(service, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(service, "_require_release_checkout", lambda: RELEASE_DIGEST)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_load_runtime_image_authority", lambda _id: _runtime_image_authority())
    monkeypatch.setattr(service.signal, "signal", lambda *_args: None)
    service._STOP = False
    prepared = service.prepare(
        "long_lived_runtime",
        "NO_ORDER_SOAK",
        release_digest=RELEASE_DIGEST,
        deployment_id=_lineage().deployment_id,
        deployment_capability_digest=_lineage().deployment_capability_digest,
        runtime_image_acceptance_id=IMAGE_ACCEPTANCE_ID,
        enable_order_writer=False,
    )
    _plan, state = service._load_plan("long_lived_runtime")
    service._atomic_json(
        service._state_path("long_lived_runtime"),
        {**state, "status": "CONFIRMED", "confirmed_at": NOW.isoformat()},
    )
    monkeypatch.setattr(
        service.time, "sleep", lambda _seconds: setattr(service, "_STOP", True)
    )
    service.supervise("long_lived_runtime", prepared["plan_digest"])
    receipts = [
        json.loads(line)
        for line in service._receipt_path("long_lived_runtime").read_text().splitlines()
    ]
    assert [receipt["action"] for receipt in receipts] == [
        "PREPARE",
        "CLAIM_LEASE",
        "RELEASE_LEASE",
    ]
    assert (
        service.FileLeasePort(service.SUPPORT_ROOT).read("long_lived_runtime") is None
    )


def test_production_no_order_soak_starts_container_before_runtime_activation(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_production_no_order_bootstrap_test")
    monkeypatch.setattr(service, "SUPPORT_ROOT", tmp_path / "support")
    monkeypatch.setattr(service, "LAUNCH_AGENT_ROOT", tmp_path / "agents")
    monkeypatch.setattr(service, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(service, "_require_release_checkout", lambda: RELEASE_DIGEST)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(
        service,
        "_load_runtime_image_authority",
        lambda _id: _runtime_image_authority(),
    )
    monkeypatch.setattr(service.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        service,
        "_production_runtime_worker_factory",
        lambda: pytest.fail("NO_ORDER_SOAK must not compose the ACTIVE-lineage worker"),
    )
    service._STOP = False

    prepared = service.prepare(
        "long_lived_runtime",
        "NO_ORDER_SOAK",
        release_digest=RELEASE_DIGEST,
        deployment_id=_lineage().deployment_id,
        deployment_capability_digest=_lineage().deployment_capability_digest,
        runtime_image_acceptance_id=IMAGE_ACCEPTANCE_ID,
        enable_order_writer=False,
    )
    plan, state = service._load_plan("long_lived_runtime")
    service._atomic_json(
        service._state_path("long_lived_runtime"),
        {**state, "status": "CONFIRMED", "confirmed_at": NOW.isoformat()},
    )
    container_calls = []
    container_port = SimpleNamespace(
        start=lambda observed_plan: container_calls.append(("start", observed_plan))
        or "container-id",
        verify=lambda observed_plan, container_id: container_calls.append(
            ("verify", observed_plan, container_id)
        )
        or "9" * 64,
        stop=lambda observed_plan, container_id: container_calls.append(
            ("stop", observed_plan, container_id)
        ),
    )
    monkeypatch.setattr(
        service.time, "sleep", lambda _seconds: setattr(service, "_STOP", True)
    )

    service.supervise(
        "long_lived_runtime",
        prepared["plan_digest"],
        production_compose=True,
        runtime_container_port=container_port,
    )

    assert [call[0] for call in container_calls] == ["start", "verify", "stop"]
    receipts = [
        json.loads(line)
        for line in service._receipt_path("long_lived_runtime").read_text().splitlines()
    ]
    assert [receipt["action"] for receipt in receipts] == [
        "PREPARE",
        "CLAIM_LEASE",
        "RUNTIME_CONTAINER_START",
        "RELEASE_LEASE",
    ]
    assert receipts[2]["details"]["network"] == "NONE"
    assert receipts[2]["details"]["runtime_image_acceptance_id"] == str(
        IMAGE_ACCEPTANCE_ID
    )
    assert (
        service.FileLeasePort(service.SUPPORT_ROOT).read("long_lived_runtime") is None
    )


def test_supervisor_loop_accepts_injected_worker_receipt_and_releases_lease(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_worker_composed_test")
    monkeypatch.setattr(service, "SUPPORT_ROOT", tmp_path / "support")
    monkeypatch.setattr(service, "LAUNCH_AGENT_ROOT", tmp_path / "agents")
    monkeypatch.setattr(service, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(service, "_require_release_checkout", lambda: RELEASE_DIGEST)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_load_runtime_image_authority", lambda _id: _runtime_image_authority())
    monkeypatch.setattr(service.signal, "signal", lambda *_args: None)
    service._STOP = False

    prepared = service.prepare(
        "long_lived_runtime",
        "NO_ORDER_SOAK",
        release_digest=RELEASE_DIGEST,
        deployment_id=_lineage().deployment_id,
        deployment_capability_digest=_lineage().deployment_capability_digest,
        runtime_image_acceptance_id=IMAGE_ACCEPTANCE_ID,
        enable_order_writer=False,
    )
    plan, state = service._load_plan("long_lived_runtime")
    service._atomic_json(
        service._state_path("long_lived_runtime"),
        {**state, "status": "CONFIRMED", "confirmed_at": NOW.isoformat()},
    )
    worker, _reader, evidence, evaluator, signer = _worker()
    port = WorkerSupervisorAdapter(worker, signer)

    def finish_after_initial_heartbeat(_seconds: float) -> None:
        service._STOP = True

    monkeypatch.setattr(service.time, "sleep", finish_after_initial_heartbeat)
    service.supervise(
        "long_lived_runtime",
        prepared["plan_digest"],
        worker_port=port,
    )

    assert evidence.calls == evaluator.calls == 0
    assert (
        service.FileLeasePort(service.SUPPORT_ROOT).read("long_lived_runtime") is None
    )
    receipts = [
        json.loads(line)
        for line in service._receipt_path("long_lived_runtime").read_text().splitlines()
    ]
    assert [receipt["action"] for receipt in receipts] == [
        "PREPARE",
        "CLAIM_LEASE",
        "WORKER_HEARTBEAT",
        "RELEASE_LEASE",
    ]
    worker_receipt = receipts[2]
    assert worker_receipt["details"]["signal_candidate_digest"] is None
    assert worker_receipt["details"]["persistence_target"] == "canonical_signal_writer"
    assert worker_receipt["details"]["order_submission_enabled"] is False
