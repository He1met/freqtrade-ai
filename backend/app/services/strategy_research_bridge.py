from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.strategy_research_contract import matches_official_research_policy
from app.models import (
    FullChainRun,
    FullChainStageRun,
    MarketDataQualityReceipt,
    ResearchJob,
    ResearchJobAttempt,
    Strategy,
    StrategyGenerationRun,
    StrategyResearchAttemptEvent,
    StrategyResearchCandidate,
    StrategyResearchCandidateBridgeEvent,
    StrategyVersion,
)
from app.services.strategy_blueprint_equivalence import (
    StrategyBlueprintEquivalenceBlocked,
    canonical_json_digest,
    prove_blueprint_code_equivalence,
)
from app.services.strategy_file_validation import StrategyFileValidationService
from app.services.strategy_renderer import STRATEGY_RENDERER_VERSION


BRIDGE_CONTRACT_VERSION = "formal-candidate-blueprint-bridge-v1"
BRIDGE_OPERATION = "strategy_research.canonical_bridge_v1"


class StrategyResearchBridgeBlocked(RuntimeError):
    """The candidate cannot safely enter the canonical lifecycle."""


def _safe_reason(reason: str) -> str:
    return re.sub(
        r"(?i)\b(api[_-]?key|secret|password|passphrase|token)(\s*[:=]\s*)\S+",
        r"\1\2[REDACTED]",
        reason,
    )[:1000]


class StrategyResearchBridgeService:
    """Bridge an exact Blueprint v2 rendering into canonical research only.

    A successful bridge deliberately stops at the independent canonical
    validation gate.  It never creates an approval, deployment, signal, order,
    grant, or any other execution capability.
    """

    def __init__(
        self,
        db: Session,
        *,
        project_root: Path,
        file_validation: StrategyFileValidationService | None = None,
    ) -> None:
        self.db = db
        self.project_root = project_root.resolve()
        self.file_validation = file_validation or StrategyFileValidationService()

    def bridge(
        self,
        candidate_id: int,
        *,
        blueprint_payload: dict[str, Any] | None,
        requested_by: str,
        now: datetime | None = None,
    ) -> StrategyResearchCandidateBridgeEvent:
        current = now or datetime.now(timezone.utc)
        if not requested_by.strip():
            raise StrategyResearchBridgeBlocked("requested_by is required")
        if self.db.get_bind().dialect.name == "postgresql":
            if self.db.execute(text("SELECT current_user = 'freqtrade'" )).scalar_one():
                raise StrategyResearchBridgeBlocked("runtime role cannot write canonical bridges")
            self.db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 543000100 + candidate_id})

        candidate = self.db.scalar(
            select(StrategyResearchCandidate)
            .where(StrategyResearchCandidate.id == candidate_id)
            .with_for_update()
        )
        if candidate is None:
            raise StrategyResearchBridgeBlocked("research candidate does not exist")
        existing_bridge = self.db.scalar(
            select(StrategyResearchCandidateBridgeEvent).where(
                StrategyResearchCandidateBridgeEvent.research_candidate_id == candidate.id,
                StrategyResearchCandidateBridgeEvent.outcome == "BRIDGED",
            )
        )
        if existing_bridge is not None:
            return existing_bridge

        persisted_blueprint_evidence = (candidate.evidence_snapshot or {}).get(
            "canonical_blueprint_v2"
        )
        if blueprint_payload is None and isinstance(persisted_blueprint_evidence, dict):
            persisted_blueprint = persisted_blueprint_evidence.get("blueprint")
            if (
                persisted_blueprint_evidence.get("exact_render_match") is True
                and isinstance(persisted_blueprint, dict)
            ):
                blueprint_payload = persisted_blueprint

        request_snapshot = {
            "bridge_contract_version": BRIDGE_CONTRACT_VERSION,
            "candidate_id": candidate.id,
            "candidate_code_digest": candidate.code_digest,
            "blueprint": blueprint_payload,
            "renderer_version": STRATEGY_RENDERER_VERSION,
            "requested_by": requested_by,
            "execution_scope_id": "LOCAL_DRY_RUN",
            "execution_target_id": "OKX_DEMO",
            "allow_real_funds": False,
            "real_orders": False,
        }
        request_digest = canonical_json_digest(request_snapshot)
        prior = self.db.scalar(
            select(StrategyResearchCandidateBridgeEvent).where(
                StrategyResearchCandidateBridgeEvent.research_candidate_id == candidate.id,
                StrategyResearchCandidateBridgeEvent.request_digest == request_digest,
            )
        )
        if prior is not None:
            return prior

        evidence_snapshot, quality = self._validated_research_evidence(candidate)
        if (
            evidence_snapshot["official_policy_matches"] is not True
            or evidence_snapshot["terminal_attempt_event_id"] is None
            or quality is None
            or quality.status != "PASSED"
        ):
            return self._blocked_event(
                candidate=candidate,
                request_digest=request_digest,
                evidence_snapshot=evidence_snapshot,
                quality=quality,
                reason_code="RESEARCH_EVIDENCE_INCOMPLETE",
                reason="Official policy, terminal attempt, or PASSED market-data evidence is missing.",
            )
        if candidate.status != "QUALIFIED" or not candidate.validation_passed or not candidate.deployable_candidate:
            return self._blocked_event(
                candidate=candidate,
                request_digest=request_digest,
                evidence_snapshot=evidence_snapshot,
                quality=quality,
                reason_code="CANDIDATE_NOT_QUALIFIED",
                reason="Only a persisted QUALIFIED candidate can enter canonical research.",
            )
        if blueprint_payload is None:
            return self._blocked_event(
                candidate=candidate,
                request_digest=request_digest,
                evidence_snapshot=evidence_snapshot,
                quality=quality,
                reason_code="CANONICAL_BLUEPRINT_V2_MISSING",
                reason="Candidate has no deterministic Blueprint v2 evidence.",
            )

        source_path = (self.project_root / candidate.source_path).resolve()
        if (
            self.project_root not in source_path.parents
            or source_path.is_symlink()
            or not source_path.is_file()
        ):
            return self._blocked_event(
                candidate=candidate,
                request_digest=request_digest,
                evidence_snapshot=evidence_snapshot,
                quality=quality,
                reason_code="CANDIDATE_SOURCE_UNAVAILABLE",
                reason="Candidate source is missing, unsafe, or outside the project root.",
            )
        try:
            equivalence = prove_blueprint_code_equivalence(
                blueprint_payload=blueprint_payload,
                source_bytes=source_path.read_bytes(),
                expected_source_digest=candidate.code_digest,
                expected_class_name=candidate.candidate_name,
                expected_timeframe="15m",
            )
        except (OSError, StrategyBlueprintEquivalenceBlocked, ValueError) as exc:
            return self._blocked_event(
                candidate=candidate,
                request_digest=request_digest,
                evidence_snapshot=evidence_snapshot,
                quality=quality,
                reason_code="BLUEPRINT_CODE_EQUIVALENCE_FAILED",
                reason=str(exc),
                blueprint_digest=(
                    canonical_json_digest(blueprint_payload)
                    if isinstance(blueprint_payload, dict)
                    else None
                ),
            )

        conflict = self.db.scalar(select(Strategy).where(Strategy.slug == equivalence.blueprint.slug))
        if conflict is not None:
            return self._blocked_event(
                candidate=candidate,
                request_digest=request_digest,
                evidence_snapshot=evidence_snapshot,
                quality=quality,
                reason_code="CANONICAL_SLUG_CONFLICT",
                reason="Blueprint slug already belongs to a canonical strategy.",
                blueprint_digest=equivalence.blueprint_digest,
                rendered_code_digest=equivalence.rendered_code_digest,
            )

        file_result = self.file_validation.write_validated_strategy_file(
            class_name=equivalence.blueprint.class_name,
            code=equivalence.rendered_code,
            file_stem=(
                f"formal_bridge_{candidate.id}_{equivalence.rendered_code_digest[:12]}"
            ),
        )
        if file_result.code_hash != equivalence.rendered_code_digest or not file_result.file_path:
            raise StrategyResearchBridgeBlocked("canonical strategy file digest changed during write")

        evidence_digest = canonical_json_digest(evidence_snapshot)
        idempotency_digest = hashlib.sha256(
            f"{BRIDGE_OPERATION}|{candidate.id}|{request_digest}|{evidence_digest}".encode("utf-8")
        ).hexdigest()
        generation = StrategyGenerationRun(
            execution_scope_id="LOCAL_DRY_RUN",
            provider="formal_research_bridge",
            model=STRATEGY_RENDERER_VERSION,
            prompt_hash=None,
            prompt_summary="Deterministic Blueprint v2 equivalence bridge; no provider call.",
            params_snapshot={
                "bridge_contract_version": BRIDGE_CONTRACT_VERSION,
                "research_candidate_id": candidate.id,
                "renderer_version": STRATEGY_RENDERER_VERSION,
                "provider_call_attempted": False,
                "allow_real_funds": False,
                "real_orders": False,
            },
            status="succeeded",
            requested_count=1,
            generated_count=1,
            accepted_count=1,
            failed_count=0,
            started_at=current,
            completed_at=current,
        )
        self.db.add(generation)
        self.db.flush()
        strategy = Strategy(
            name=equivalence.blueprint.name,
            slug=equivalence.blueprint.slug,
            description=equivalence.blueprint.description,
            status="draft",
            source="ai_generated",
            tags=[*equivalence.blueprint.tags, "formal-research-bridge"],
        )
        self.db.add(strategy)
        self.db.flush()
        version = StrategyVersion(
            strategy_id=strategy.id,
            generation_run_id=generation.id,
            version_number=1,
            blueprint=equivalence.blueprint.model_dump(mode="json"),
            generated_code=equivalence.rendered_code,
            code_hash=equivalence.rendered_code_digest,
            file_path=str(file_result.file_path),
            validation_status="pending",
            validation_errors=[],
            change_summary="Deterministic formal candidate bridge; canonical validation required.",
            diff_snapshot={
                "bridge_contract_version": BRIDGE_CONTRACT_VERSION,
                "research_candidate_id": candidate.id,
                "blueprint_digest": equivalence.blueprint_digest,
                "renderer_version": equivalence.renderer_version,
                "strategy_file_validation": file_result.to_snapshot(),
            },
        )
        self.db.add(version)
        self.db.flush()
        strategy.current_version_id = version.id
        job = ResearchJob(
            execution_scope_id="LOCAL_DRY_RUN",
            job_type="formal_candidate_bridge",
            operation=BRIDGE_OPERATION,
            idempotency_key_digest=idempotency_digest,
            request_hash=request_digest,
            request_payload={
                "research_candidate_id": candidate.id,
                "source_code_digest": candidate.code_digest,
                "blueprint_digest": equivalence.blueprint_digest,
                "renderer_version": equivalence.renderer_version,
                "allow_real_funds": False,
                "real_orders": False,
            },
            status="BLOCKED",
            stage="CANONICAL_VALIDATION_REQUIRED",
            attempt_count=1,
            max_attempts=1,
            strategy_generation_run_id=generation.id,
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            evidence_snapshot=evidence_snapshot,
            error_message="Canonical validation must run independently before scoring or approval.",
            started_at=current,
            completed_at=current,
        )
        self.db.add(job)
        self.db.flush()
        attempt = ResearchJobAttempt(
            research_job_id=job.id,
            attempt_number=1,
            execution_scope_id="LOCAL_DRY_RUN",
            status="BLOCKED",
            started_at=current,
            completed_at=current,
            evidence_snapshot={
                "reason_code": "CANONICAL_VALIDATION_REQUIRED",
                "research_candidate_id": candidate.id,
            },
        )
        self.db.add(attempt)
        self.db.flush()
        chain = FullChainRun(
            research_job_id=job.id,
            research_job_attempt_id=attempt.id,
            run_kind="RESEARCH",
            research_scope_id="LOCAL_DRY_RUN",
            execution_target_id="OKX_DEMO",
            status="BLOCKED",
            current_stage="BACKTEST",
            strategy_generation_run_id=generation.id,
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            terminal_reason="CANONICAL_VALIDATION_REQUIRED",
            started_at=current,
            completed_at=current,
        )
        self.db.add(chain)
        self.db.flush()
        self.db.add_all(
            [
                FullChainStageRun(
                    full_chain_run_id=chain.id,
                    stage="GENERATION",
                    status="SUCCESS",
                    idempotency_key_digest=hashlib.sha256(f"{idempotency_digest}|GENERATION".encode()).hexdigest(),
                    input_digest=request_digest,
                    input_snapshot={"research_candidate_id": candidate.id},
                    output_snapshot={"strategy_version_id": version.id},
                    database_ids={
                        "strategy_generation_run_id": generation.id,
                        "strategy_id": strategy.id,
                        "strategy_version_id": version.id,
                    },
                    prepared_at=current,
                    completed_at=current,
                ),
                FullChainStageRun(
                    full_chain_run_id=chain.id,
                    stage="BACKTEST",
                    status="BLOCKED",
                    idempotency_key_digest=hashlib.sha256(f"{idempotency_digest}|BACKTEST".encode()).hexdigest(),
                    input_digest=evidence_digest,
                    input_snapshot={
                        "reason_code": "CANONICAL_VALIDATION_REQUIRED",
                        "research_evidence_is_not_canonical_validation": True,
                    },
                    output_snapshot={},
                    database_ids={},
                    error_code="CANONICAL_VALIDATION_REQUIRED",
                    error_message="Independent canonical validation has not run.",
                    prepared_at=current,
                    completed_at=current,
                ),
            ]
        )
        event = self._event(
            candidate=candidate,
            request_digest=request_digest,
            evidence_digest=evidence_digest,
            quality=quality,
            outcome="BRIDGED",
            reason_code="CANONICAL_VALIDATION_REQUIRED",
            reason="Blueprint equivalence proven; canonical validation is still required.",
            blueprint_digest=equivalence.blueprint_digest,
            rendered_code_digest=equivalence.rendered_code_digest,
            canonical_ids={
                "canonical_research_job_id": job.id,
                "canonical_research_job_attempt_id": attempt.id,
                "canonical_full_chain_run_id": chain.id,
                "strategy_generation_run_id": generation.id,
                "strategy_id": strategy.id,
                "strategy_version_id": version.id,
            },
            evidence_snapshot={
                **evidence_snapshot,
                "equivalence": {
                    "blueprint_digest": equivalence.blueprint_digest,
                    "renderer_version": equivalence.renderer_version,
                    "rendered_code_digest": equivalence.rendered_code_digest,
                },
            },
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        return event

    def _validated_research_evidence(
        self, candidate: StrategyResearchCandidate
    ) -> tuple[dict[str, Any], MarketDataQualityReceipt | None]:
        batch = candidate.batch
        policy_ok = matches_official_research_policy(batch.selection_policy)
        terminal = self.db.scalar(
            select(StrategyResearchAttemptEvent)
            .where(
                StrategyResearchAttemptEvent.batch_id == batch.id,
                StrategyResearchAttemptEvent.outcome == "COMPLETED",
            )
            .order_by(StrategyResearchAttemptEvent.created_at.desc(), StrategyResearchAttemptEvent.id.desc())
        )
        quality = (
            self.db.get(MarketDataQualityReceipt, terminal.market_data_quality_receipt_id)
            if terminal is not None and terminal.market_data_quality_receipt_id is not None
            else None
        )
        evidence = {
            "research_batch_id": batch.id,
            "research_run_id": batch.run_id,
            "research_report_digest": batch.report_digest,
            "repository_commit": batch.repository_commit,
            "research_candidate_id": candidate.id,
            "source_code_digest": candidate.code_digest,
            "official_policy_matches": policy_ok,
            "terminal_attempt_event_id": terminal.id if terminal is not None else None,
            "market_data_quality_receipt_id": quality.id if quality is not None else None,
            "market_data_quality_status": quality.status if quality is not None else "UNKNOWN",
            "candidate_validation": candidate.evidence_snapshot,
        }
        return evidence, quality

    def _blocked_event(
        self,
        *,
        candidate: StrategyResearchCandidate,
        request_digest: str,
        evidence_snapshot: dict[str, Any],
        quality: MarketDataQualityReceipt | None,
        reason_code: str,
        reason: str,
        blueprint_digest: str | None = None,
        rendered_code_digest: str | None = None,
    ) -> StrategyResearchCandidateBridgeEvent:
        event = self._event(
            candidate=candidate,
            request_digest=request_digest,
            evidence_digest=canonical_json_digest(evidence_snapshot),
            quality=quality,
            outcome="REVALIDATION_REQUIRED",
            reason_code=reason_code,
            reason=reason,
            blueprint_digest=blueprint_digest,
            rendered_code_digest=rendered_code_digest,
            canonical_ids={},
            evidence_snapshot=evidence_snapshot,
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        return event

    def _event(
        self,
        *,
        candidate: StrategyResearchCandidate,
        request_digest: str,
        evidence_digest: str,
        quality: MarketDataQualityReceipt | None,
        outcome: str,
        reason_code: str,
        reason: str,
        blueprint_digest: str | None,
        rendered_code_digest: str | None,
        canonical_ids: dict[str, int],
        evidence_snapshot: dict[str, Any],
    ) -> StrategyResearchCandidateBridgeEvent:
        identity = {
            "candidate_id": candidate.id,
            "request_digest": request_digest,
            "evidence_digest": evidence_digest,
            "outcome": outcome,
            "reason_code": reason_code,
            **canonical_ids,
        }
        event_digest = canonical_json_digest(identity)
        return StrategyResearchCandidateBridgeEvent(
            bridge_attempt_id=str(uuid5(NAMESPACE_URL, event_digest)),
            sequence=1,
            research_candidate_id=candidate.id,
            market_data_quality_receipt_id=quality.id if quality is not None else None,
            outcome=outcome,
            reason_code=reason_code,
            redacted_reason=_safe_reason(reason),
            bridge_contract_version=BRIDGE_CONTRACT_VERSION,
            execution_scope_id="LOCAL_DRY_RUN",
            execution_target_id="OKX_DEMO",
            allow_real_funds=False,
            real_orders=False,
            source_code_digest=candidate.code_digest,
            blueprint_digest=blueprint_digest,
            rendered_code_digest=rendered_code_digest,
            request_digest=request_digest,
            evidence_digest=evidence_digest,
            evidence_snapshot=evidence_snapshot,
            event_digest=event_digest,
            **canonical_ids,
        )
