"""Network-free canonical Phase 9 runtime worker contracts.

The runtime has no persistence port.  It reads one exact ACTIVE/HEALTHY lineage,
evaluates injected market evidence, and emits a signed candidate receipt for the
separately identified ``canonical_signal_writer``.  It can never write a signal,
intent, risk decision, or order itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Final, Mapping, Protocol
from uuid import UUID


class CanonicalPhase9RuntimeWorkerBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ActiveRuntimeLineage:
    qualification_decision_id: UUID
    qualification_decision_digest: str
    deployment_approval_id: UUID
    deployment_approval_digest: str
    deployment_id: UUID
    runtime_instance_id: UUID
    strategy_version_id: UUID
    research_target_id: UUID
    configuration_bundle_id: UUID
    configuration_bundle_digest: str
    market_snapshot_id: UUID
    market_snapshot_digest: str
    deployment_capability_digest: str
    runtime_launch_spec_digest: str
    runtime_receipt_digest: str
    runtime_receipt_observed_at: datetime
    runtime_identity: str
    runtime_service_account: str
    deployment_status: str
    runtime_status: str
    runtime_evidence_class: str
    demo_only: bool
    allow_real_funds: bool
    runtime_order_writer_capability: bool
    strategy_artifact_digest: str | None = None
    strategy_artifact_source: str | None = None
    target_instrument: str | None = None
    target_pair: str | None = None
    target_timeframe: str | None = None
    target_data_kind: str | None = None


@dataclass(frozen=True)
class NaturalMarketEvidence:
    evidence_id: str
    evidence_digest: str
    instrument: str
    observed_at: datetime
    payload: Mapping[str, object]
    evidence_class: str = "PRODUCTION_OKX_DEMO_MARKET_EVIDENCE"


@dataclass(frozen=True)
class NaturalSignalEvaluation:
    outcome: str
    evaluated_at: datetime
    evaluator_identity: str
    evaluation_payload: Mapping[str, object]


@dataclass(frozen=True)
class RuntimeSignalCandidate:
    contract: str
    qualification_decision_id: UUID
    qualification_decision_digest: str
    deployment_approval_id: UUID
    deployment_approval_digest: str
    deployment_id: UUID
    runtime_instance_id: UUID
    strategy_version_id: UUID
    research_target_id: UUID
    configuration_bundle_id: UUID
    configuration_bundle_digest: str
    market_snapshot_id: UUID
    market_snapshot_digest: str
    runtime_receipt_digest: str
    deployment_capability_digest: str
    runtime_launch_spec_digest: str
    market_evidence_id: str
    market_evidence_digest: str
    instrument: str
    evaluated_at: datetime
    evaluator_identity: str
    signal_json: Mapping[str, object]
    candidate_digest: str


@dataclass(frozen=True)
class RuntimeWorkerReceipt:
    contract: str
    stage: str
    status: str
    reason_code: str
    plan_digest: str
    runtime_instance_id: UUID
    runtime_receipt_digest: str
    observed_at: datetime
    order_submission_enabled: bool
    persistence_target: str
    signal_candidate: RuntimeSignalCandidate | None
    signal_candidate_digest: str | None
    signer_key_id: str
    signature_algorithm: str
    receipt_digest: str
    signature: str


class RuntimeLineageReaderPort(Protocol):
    def read_active_runtime_lineage(self) -> ActiveRuntimeLineage: ...


class MarketEvidencePort(Protocol):
    def read_market_evidence(
        self, *, lineage: ActiveRuntimeLineage, observed_at: datetime
    ) -> NaturalMarketEvidence: ...


class NaturalSignalEvaluatorPort(Protocol):
    def evaluate_natural_signal(
        self, *, lineage: ActiveRuntimeLineage, evidence: NaturalMarketEvidence
    ) -> NaturalSignalEvaluation: ...


class ReceiptSignerPort(Protocol):
    key_id: str
    algorithm: str

    def sign_digest(self, digest: str) -> str: ...


class ReceiptVerifierPort(Protocol):
    def verify_digest(
        self, *, key_id: str, algorithm: str, digest: str, signature: str
    ) -> bool: ...


_SIGNAL_STAGES: Final[frozenset[str]] = frozenset(
    {"SIGNAL_RISK_SHADOW", "OKX_DEMO_CANARY"}
)
_ALL_STAGES: Final[frozenset[str]] = frozenset({"NO_ORDER_SOAK", *_SIGNAL_STAGES})
_DIGEST_LENGTH: Final[int] = 64


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise CanonicalPhase9RuntimeWorkerBlocked(
            "BLOCKED_RUNTIME_WORKER_TIMEZONE", f"{field} must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value, field="digest_timestamp").isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            _jsonable(value),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _require_digest(value: str, *, field: str) -> None:
    if len(value) != _DIGEST_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise CanonicalPhase9RuntimeWorkerBlocked(
            "BLOCKED_RUNTIME_WORKER_DIGEST", f"{field} is not lowercase sha256"
        )


def validate_active_runtime_lineage(lineage: ActiveRuntimeLineage) -> None:
    for field in (
        "qualification_decision_digest",
        "deployment_approval_digest",
        "configuration_bundle_digest",
        "market_snapshot_digest",
        "deployment_capability_digest",
        "runtime_launch_spec_digest",
        "runtime_receipt_digest",
    ):
        _require_digest(str(getattr(lineage, field)), field=field)
    if (
        lineage.deployment_status != "ACTIVE"
        or lineage.runtime_status != "HEALTHY"
        or lineage.runtime_evidence_class != "PRODUCTION_DEMO_RUNTIME"
        or lineage.demo_only is not True
        or lineage.allow_real_funds is not False
        or lineage.runtime_order_writer_capability is not False
        or lineage.runtime_service_account != "canonical_runtime_reader"
        or lineage.runtime_identity != "canonical-v13-long-lived-runtime-v1"
    ):
        raise CanonicalPhase9RuntimeWorkerBlocked(
            "BLOCKED_RUNTIME_WORKER_LINEAGE",
            "exact ACTIVE Demo deployment and HEALTHY non-order runtime are required",
        )


def natural_market_evidence_digest(evidence: NaturalMarketEvidence) -> str:
    return _digest(
        {
            "contract": "canonical-v13-natural-market-evidence-v1",
            "evidence_id": evidence.evidence_id,
            "instrument": evidence.instrument,
            "observed_at": evidence.observed_at,
            "payload": dict(evidence.payload),
            "evidence_class": evidence.evidence_class,
        }
    )


def _build_candidate(
    *,
    lineage: ActiveRuntimeLineage,
    evidence: NaturalMarketEvidence,
    evaluation: NaturalSignalEvaluation,
) -> RuntimeSignalCandidate:
    evaluated_at = _utc(evaluation.evaluated_at, field="evaluated_at")
    signal_json = {
        "evidence_class": "PRODUCTION_OKX_DEMO",
        "natural_signal": True,
        "allow_real_funds": False,
        "qualification_decision_id": str(lineage.qualification_decision_id),
        "qualification_decision_digest": lineage.qualification_decision_digest,
        "deployment_approval_id": str(lineage.deployment_approval_id),
        "deployment_approval_digest": lineage.deployment_approval_digest,
        "deployment_id": str(lineage.deployment_id),
        "runtime_instance_id": str(lineage.runtime_instance_id),
        "strategy_version_id": str(lineage.strategy_version_id),
        "research_target_id": str(lineage.research_target_id),
        "configuration_bundle_id": str(lineage.configuration_bundle_id),
        "configuration_bundle_digest": lineage.configuration_bundle_digest,
        "market_snapshot_id": str(lineage.market_snapshot_id),
        "market_snapshot_digest": lineage.market_snapshot_digest,
        "runtime_receipt_digest": lineage.runtime_receipt_digest,
        "deployment_capability_digest": lineage.deployment_capability_digest,
        "runtime_launch_spec_digest": lineage.runtime_launch_spec_digest,
        "market_evidence_id": evidence.evidence_id,
        "market_evidence_digest": evidence.evidence_digest,
        "instrument": evidence.instrument,
        "evaluated_at": evaluated_at.isoformat(),
        "evaluator_identity": evaluation.evaluator_identity,
        "evaluation": dict(evaluation.evaluation_payload),
    }
    payload = {
        "contract": "canonical-v13-natural-signal-candidate-v1",
        **signal_json,
    }
    candidate_digest = _digest(payload)
    return RuntimeSignalCandidate(
        contract=str(payload["contract"]),
        qualification_decision_id=lineage.qualification_decision_id,
        qualification_decision_digest=lineage.qualification_decision_digest,
        deployment_approval_id=lineage.deployment_approval_id,
        deployment_approval_digest=lineage.deployment_approval_digest,
        deployment_id=lineage.deployment_id,
        runtime_instance_id=lineage.runtime_instance_id,
        strategy_version_id=lineage.strategy_version_id,
        research_target_id=lineage.research_target_id,
        configuration_bundle_id=lineage.configuration_bundle_id,
        configuration_bundle_digest=lineage.configuration_bundle_digest,
        market_snapshot_id=lineage.market_snapshot_id,
        market_snapshot_digest=lineage.market_snapshot_digest,
        runtime_receipt_digest=lineage.runtime_receipt_digest,
        deployment_capability_digest=lineage.deployment_capability_digest,
        runtime_launch_spec_digest=lineage.runtime_launch_spec_digest,
        market_evidence_id=evidence.evidence_id,
        market_evidence_digest=evidence.evidence_digest,
        instrument=evidence.instrument,
        evaluated_at=evaluated_at,
        evaluator_identity=evaluation.evaluator_identity,
        signal_json=signal_json,
        candidate_digest=candidate_digest,
    )


def _build_receipt(
    *,
    stage: str,
    plan_digest: str,
    lineage: ActiveRuntimeLineage,
    observed_at: datetime,
    status: str,
    reason_code: str,
    candidate: RuntimeSignalCandidate | None,
    signer: ReceiptSignerPort,
) -> RuntimeWorkerReceipt:
    _require_digest(plan_digest, field="plan_digest")
    observed = _utc(observed_at, field="observed_at")
    unsigned = {
        "contract": "canonical-v13-runtime-worker-receipt-v1",
        "stage": stage,
        "status": status,
        "reason_code": reason_code,
        "plan_digest": plan_digest,
        "runtime_instance_id": lineage.runtime_instance_id,
        "runtime_receipt_digest": lineage.runtime_receipt_digest,
        "observed_at": observed,
        "order_submission_enabled": False,
        "persistence_target": "canonical_signal_writer",
        "signal_candidate_digest": candidate.candidate_digest if candidate else None,
        "signer_key_id": signer.key_id,
        "signature_algorithm": signer.algorithm,
    }
    receipt_digest = _digest(unsigned)
    signature = signer.sign_digest(receipt_digest)
    if not signer.key_id or not signer.algorithm or not signature:
        raise CanonicalPhase9RuntimeWorkerBlocked(
            "BLOCKED_RUNTIME_WORKER_SIGNATURE", "signer returned incomplete evidence"
        )
    return RuntimeWorkerReceipt(
        contract=str(unsigned["contract"]),
        stage=stage,
        status=status,
        reason_code=reason_code,
        plan_digest=plan_digest,
        runtime_instance_id=lineage.runtime_instance_id,
        runtime_receipt_digest=lineage.runtime_receipt_digest,
        observed_at=observed,
        order_submission_enabled=False,
        persistence_target="canonical_signal_writer",
        signal_candidate=candidate,
        signal_candidate_digest=candidate.candidate_digest if candidate else None,
        signer_key_id=signer.key_id,
        signature_algorithm=signer.algorithm,
        receipt_digest=receipt_digest,
        signature=signature,
    )


def verify_runtime_worker_receipt(
    receipt: RuntimeWorkerReceipt, *, verifier: ReceiptVerifierPort
) -> bool:
    unsigned = {
        "contract": receipt.contract,
        "stage": receipt.stage,
        "status": receipt.status,
        "reason_code": receipt.reason_code,
        "plan_digest": receipt.plan_digest,
        "runtime_instance_id": receipt.runtime_instance_id,
        "runtime_receipt_digest": receipt.runtime_receipt_digest,
        "observed_at": receipt.observed_at,
        "order_submission_enabled": receipt.order_submission_enabled,
        "persistence_target": receipt.persistence_target,
        "signal_candidate_digest": receipt.signal_candidate_digest,
        "signer_key_id": receipt.signer_key_id,
        "signature_algorithm": receipt.signature_algorithm,
    }
    candidate = receipt.signal_candidate
    candidate_lineage_matches = bool(
        candidate
        and candidate.signal_json.get("qualification_decision_id")
        == str(candidate.qualification_decision_id)
        and candidate.signal_json.get("qualification_decision_digest")
        == candidate.qualification_decision_digest
        and candidate.signal_json.get("deployment_approval_id")
        == str(candidate.deployment_approval_id)
        and candidate.signal_json.get("deployment_approval_digest")
        == candidate.deployment_approval_digest
        and candidate.signal_json.get("deployment_id") == str(candidate.deployment_id)
        and candidate.signal_json.get("runtime_instance_id")
        == str(candidate.runtime_instance_id)
        and candidate.signal_json.get("strategy_version_id")
        == str(candidate.strategy_version_id)
        and candidate.signal_json.get("research_target_id")
        == str(candidate.research_target_id)
        and candidate.signal_json.get("configuration_bundle_id")
        == str(candidate.configuration_bundle_id)
        and candidate.signal_json.get("configuration_bundle_digest")
        == candidate.configuration_bundle_digest
        and candidate.signal_json.get("market_snapshot_id")
        == str(candidate.market_snapshot_id)
        and candidate.signal_json.get("market_snapshot_digest")
        == candidate.market_snapshot_digest
        and candidate.signal_json.get("runtime_receipt_digest")
        == candidate.runtime_receipt_digest
        and candidate.signal_json.get("deployment_capability_digest")
        == candidate.deployment_capability_digest
        and candidate.signal_json.get("runtime_launch_spec_digest")
        == candidate.runtime_launch_spec_digest
        and candidate.signal_json.get("market_evidence_id")
        == candidate.market_evidence_id
        and candidate.signal_json.get("market_evidence_digest")
        == candidate.market_evidence_digest
        and candidate.signal_json.get("instrument") == candidate.instrument
        and candidate.signal_json.get("evaluated_at")
        == candidate.evaluated_at.isoformat()
        and candidate.signal_json.get("evaluator_identity")
        == candidate.evaluator_identity
    )
    candidate_matches = (
        receipt.signal_candidate is None and receipt.signal_candidate_digest is None
    ) or (
        candidate is not None
        and candidate_lineage_matches
        and candidate.candidate_digest == receipt.signal_candidate_digest
        and _digest(
            {
                "contract": candidate.contract,
                **dict(candidate.signal_json),
            }
        )
        == receipt.signal_candidate_digest
    )
    digest = _digest(unsigned)
    return (
        receipt.contract == "canonical-v13-runtime-worker-receipt-v1"
        and receipt.stage in _ALL_STAGES
        and receipt.order_submission_enabled is False
        and receipt.persistence_target == "canonical_signal_writer"
        and digest == receipt.receipt_digest
        and candidate_matches
        and verifier.verify_digest(
            key_id=receipt.signer_key_id,
            algorithm=receipt.signature_algorithm,
            digest=digest,
            signature=receipt.signature,
        )
    )


class CanonicalPhase9RuntimeWorker:
    def __init__(
        self,
        *,
        lineage_reader: RuntimeLineageReaderPort,
        market_evidence: MarketEvidencePort,
        evaluator: NaturalSignalEvaluatorPort,
        signer: ReceiptSignerPort,
        maximum_evidence_age: timedelta = timedelta(minutes=2),
        maximum_runtime_heartbeat_age: timedelta = timedelta(minutes=5),
    ) -> None:
        if maximum_evidence_age <= timedelta(
            0
        ) or maximum_runtime_heartbeat_age <= timedelta(0):
            raise CanonicalPhase9RuntimeWorkerBlocked(
                "BLOCKED_RUNTIME_WORKER_FRESHNESS", "evidence TTL must be positive"
            )
        self._lineage_reader = lineage_reader
        self._market_evidence = market_evidence
        self._evaluator = evaluator
        self._signer = signer
        self._maximum_evidence_age = maximum_evidence_age
        self._maximum_runtime_heartbeat_age = maximum_runtime_heartbeat_age
        self._accepted_runtime_receipt_digest: str | None = None

    def heartbeat(
        self, *, stage: str, plan_digest: str, observed_at: datetime
    ) -> RuntimeWorkerReceipt:
        if stage not in _ALL_STAGES:
            raise CanonicalPhase9RuntimeWorkerBlocked(
                "BLOCKED_RUNTIME_WORKER_STAGE", f"unsupported stage {stage!r}"
            )
        now = _utc(observed_at, field="observed_at")
        lineage = self._lineage_reader.read_active_runtime_lineage()
        validate_active_runtime_lineage(lineage)
        runtime_heartbeat_at = _utc(
            lineage.runtime_receipt_observed_at,
            field="runtime_receipt_observed_at",
        )
        if runtime_heartbeat_at > now or (
            lineage.runtime_receipt_digest != self._accepted_runtime_receipt_digest
            and now - runtime_heartbeat_at > self._maximum_runtime_heartbeat_age
        ):
            raise CanonicalPhase9RuntimeWorkerBlocked(
                "BLOCKED_RUNTIME_WORKER_HEARTBEAT",
                "production runtime observation is stale or in the future",
            )
        if stage == "NO_ORDER_SOAK":
            receipt = _build_receipt(
                stage=stage,
                plan_digest=plan_digest,
                lineage=lineage,
                observed_at=now,
                status="HEALTHY",
                reason_code="NO_ORDER_SOAK_SIGNAL_EVALUATION_DISABLED",
                candidate=None,
                signer=self._signer,
            )
            self._accepted_runtime_receipt_digest = lineage.runtime_receipt_digest
            return receipt

        evidence = self._market_evidence.read_market_evidence(
            lineage=lineage, observed_at=now
        )
        evidence_at = _utc(evidence.observed_at, field="market_evidence.observed_at")
        _require_digest(
            evidence.evidence_digest, field="market_evidence.evidence_digest"
        )
        if (
            evidence.evidence_class != "PRODUCTION_OKX_DEMO_MARKET_EVIDENCE"
            or not evidence.evidence_id
            or not evidence.instrument
            or evidence_at > now
            or now - evidence_at > self._maximum_evidence_age
            or natural_market_evidence_digest(evidence) != evidence.evidence_digest
        ):
            raise CanonicalPhase9RuntimeWorkerBlocked(
                "BLOCKED_RUNTIME_WORKER_MARKET_EVIDENCE",
                "market evidence is stale, future, non-Demo, or digest-drifted",
            )
        evaluation = self._evaluator.evaluate_natural_signal(
            lineage=lineage, evidence=evidence
        )
        evaluated_at = _utc(evaluation.evaluated_at, field="evaluation.evaluated_at")
        if (
            evaluation.outcome not in {"NO_ACTION", "SIGNAL"}
            or not evaluation.evaluator_identity
            or evaluated_at < evidence_at
            or evaluated_at > now
        ):
            raise CanonicalPhase9RuntimeWorkerBlocked(
                "BLOCKED_RUNTIME_WORKER_EVALUATION", "natural evaluation is invalid"
            )
        candidate = (
            _build_candidate(
                lineage=lineage,
                evidence=evidence,
                evaluation=evaluation,
            )
            if evaluation.outcome == "SIGNAL"
            else None
        )
        receipt = _build_receipt(
            stage=stage,
            plan_digest=plan_digest,
            lineage=lineage,
            observed_at=now,
            status="HEALTHY",
            reason_code=(
                "NATURAL_SIGNAL_CANDIDATE_READY"
                if candidate
                else "NATURAL_SIGNAL_NO_ACTION"
            ),
            candidate=candidate,
            signer=self._signer,
        )
        # The immutable runtime observation is an activation authority, not the
        # live heartbeat transport.  A new worker process (or a replacement DB
        # receipt) must still present it inside the strict freshness window.
        # Once a complete signed heartbeat succeeds, the supervisor's fenced,
        # renewing lease proves liveness while this process pins the exact
        # accepted receipt digest.  This keeps a long-lived runtime from aging
        # out its own immutable activation evidence without weakening restart
        # or lineage-drift checks.
        self._accepted_runtime_receipt_digest = lineage.runtime_receipt_digest
        return receipt


__all__ = [
    "ActiveRuntimeLineage",
    "CanonicalPhase9RuntimeWorker",
    "CanonicalPhase9RuntimeWorkerBlocked",
    "MarketEvidencePort",
    "NaturalMarketEvidence",
    "NaturalSignalEvaluation",
    "NaturalSignalEvaluatorPort",
    "ReceiptSignerPort",
    "ReceiptVerifierPort",
    "RuntimeLineageReaderPort",
    "RuntimeSignalCandidate",
    "RuntimeWorkerReceipt",
    "validate_active_runtime_lineage",
    "natural_market_evidence_digest",
    "verify_runtime_worker_receipt",
]
