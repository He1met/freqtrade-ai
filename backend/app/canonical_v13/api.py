"""Independent canonical-only V1.3 FastAPI application factory.

The factory requires an explicit connection provider and never imports or mounts the
legacy application.  Command handlers delegate only to canonical domain services;
projection handlers are read-only and derive their result from canonical tables.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol, TypeVar
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, func, select
from sqlalchemy.exc import IntegrityError

from app.canonical_v13.bundles import (
    CanonicalBundleBlocked,
    activate_research_bundle,
    preview_research_bundle,
)
from app.canonical_v13.control_plane import (
    CanonicalControlPlaneBlocked,
    ConfigurationDependencyInput,
)
from app.canonical_v13.configuration_governance import (
    CanonicalConfigurationGovernanceBlocked,
    create_audited_configuration_draft,
    validate_audited_configuration_version,
)
from app.canonical_v13.dto import (
    CanonicalErrorDetailDTO,
    CanonicalErrorResponseDTO,
    ConfigurationCatalogProjectionDTO,
    ConfigurationDraftCommandDTO,
    ConfigurationDraftResultDTO,
    ConfigurationProfileProjectionDTO,
    ConfigurationValidateCommandDTO,
    ConfigurationValidationResultDTO,
    ConfigurationVersionProjectionDTO,
    FreshMarketApplyCommandDTO,
    FreshMarketPlanCommandDTO,
    FreshMarketPlanDTO,
    FreshMarketReceiptDTO,
    GateAttemptCommandDTO,
    GateAttemptReceiptDTO,
    GateLeaseCommandDTO,
    GateLeaseReceiptDTO,
    GateRecoveryCommandDTO,
    GateRecoveryReceiptDTO,
    GateListProjectionDTO,
    GatePersistedReceiptDTO,
    GateProjectionDTO,
    LookaheadGateReceiptCommandDTO,
    MarketInventoryProjectionDTO,
    MarketProfileVersionProjectionDTO,
    MarketSnapshotMemberProjectionDTO,
    MarketSnapshotProjectionDTO,
    MarketSnapshotSummaryDTO,
    OptimizationListProjectionDTO,
    OptimizationCompleteCommandDTO,
    OptimizationCompletionReceiptDTO,
    OptimizationProjectionDTO,
    OptimizationRunCommandDTO,
    OptimizationRunReceiptDTO,
    OptimizationSubmissionLinkReceiptDTO,
    OptimizationTrialCommandDTO,
    OptimizationTrialReceiptDTO,
    Phase9ReadinessProjectionDTO,
    Phase9ApprovalCommandDTO,
    Phase9ApprovalReceiptDTO,
    Phase9CanaryRiskPolicyCommandDTO,
    Phase9CanaryRiskPolicyReceiptDTO,
    Phase9CanaryRiskPolicyTerminationCommandDTO,
    Phase9CanaryRiskPolicyTerminationReceiptDTO,
    Phase9DeploymentCommandDTO,
    Phase9DeploymentDisableCommandDTO,
    Phase9DeploymentDisableReceiptDTO,
    Phase9DeploymentReceiptDTO,
    Phase9IntentCommandDTO,
    Phase9IntentReceiptDTO,
    Phase9RiskBudgetCommandDTO,
    Phase9RiskBudgetReceiptDTO,
    Phase9RiskDecisionCommandDTO,
    Phase9RiskDecisionReceiptDTO,
    Phase9ShadowRiskDecisionCommandDTO,
    Phase9ShadowRiskDecisionReceiptDTO,
    ReadinessProjectionDTO,
    ResearchAttemptStartCommandDTO,
    ResearchAttemptStartReceiptDTO,
    ResearchAuthorizationCommandDTO,
    ResearchAuthorizationConsumeCommandDTO,
    ResearchAuthorizationConsumptionReceiptDTO,
    ResearchAuthorizationReceiptDTO,
    ResearchAuthorizationRevokeCommandDTO,
    ResearchAuthorizationRevokeReceiptDTO,
    ResearchBundleActivateCommandDTO,
    ResearchBundleActivationDTO,
    ResearchBundlePreviewCommandDTO,
    ResearchBundlePreviewDTO,
    ResearchChainProjectionDTO,
    ResearchAttemptProjectionDTO,
    ResearchGateEvaluationProjectionDTO,
    ResearchQualificationProjectionDTO,
    ResearchQualificationWindowEvidenceProjectionDTO,
    ResearchPlanCatalogProjectionDTO,
    ResearchResultsProjectionDTO,
    ResearchScoreProjectionDTO,
    ResearchWindowProjectionDTO,
    ResearchWindowResultProjectionDTO,
    ResearchLineageDTO,
    ResearchQualificationCommandDTO,
    ResearchQualificationReceiptDTO,
    ResearchScoreCommandDTO,
    ResearchScoreReceiptDTO,
    ValidationPlanCommandDTO,
    ValidationPlanReceiptDTO,
    StrategyCatalogProjectionDTO,
    StrategyProjectionDTO,
    SubmissionCommandDTO,
    SubmissionReceiptDTO,
    StaticGateReceiptCommandDTO,
)
from app.canonical_v13.deployment_approval import (
    CanonicalDeploymentApprovalBlocked,
    approve_demo_deployment,
    deployment_approval_digest,
)
from app.canonical_v13.deployment_control import (
    CanonicalDeploymentBlocked,
    create_demo_deployment,
    disable_demo_deployment,
    deployment_capability_digest,
)
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.phase9_execution_authority import (
    authorize_demo_risk_budget,
    decide_central_demo_risk,
    decide_signal_risk_shadow,
)
from app.canonical_v13.phase9_canary_policy import (
    authorize_canary_risk_policy,
    terminate_canary_risk_policy,
)
from app.canonical_v13.risk_service import create_production_demo_intent
from app.canonical_v13.genesis import (
    CanonicalGenesisBlocked,
    verify_canonical_genesis,
)
from app.canonical_v13.intake import (
    CanonicalIntakeBlocked,
    ExternalSourceEntrySnapshot,
    ExternalVersionSnapshot,
    controlled_submit_latest,
)
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    P0_CONFIGURATION_KINDS,
)
from app.canonical_v13.market import CanonicalMarketBlocked
from app.canonical_v13.fresh_market_rollout import (
    CanonicalFreshMarketRolloutBlocked,
    acquire_register_and_seal_fresh_market,
)
from app.canonical_v13.market_acquisition import (
    CanonicalMarketAcquisitionBlocked,
    MarketDownloaderPort,
)
from app.canonical_v13.market_planning import (
    CanonicalMarketPlanningBlocked,
    FreshMarketPlan,
    fresh_market_plan_digest,
    fresh_market_plan_facts,
    plan_fresh_market_acquisition,
)
from app.canonical_v13.offline_exchange_metadata import (
    OkxPublicOfflineExchangeMetadataDownloader,
)
from app.canonical_v13.optimization import (
    CanonicalOptimizationBlocked,
    complete_optimization_run,
    create_optimization_run,
    link_controlled_submission_version,
    record_isolated_optimization_trial,
)
from app.canonical_v13.research_evaluation import (
    CanonicalEvaluationBlocked,
    gate_optimization,
)
from app.canonical_v13.phase9_readiness import (
    PHASE9_ACCEPTANCE_STAGES,
    CanonicalPhase9ReadinessBlocked,
    Phase9QualificationHandoff,
    inspect_phase9_readiness,
)
from app.canonical_v13.research_authorization import (
    CanonicalResearchAuthorizationBlocked,
    ResearchAuthorizationConsumption,
    authorize_research_execution,
    consume_research_execution_authorization,
    revoke_research_execution_authorization,
    verify_persisted_research_authorization_consumption,
)
from app.canonical_v13.research_execution import (
    CanonicalResearchExecutionBlocked,
    start_consumed_research_attempt,
)
from app.canonical_v13.research_orchestration import (
    CanonicalResearchOrchestrationBlocked,
    read_research_chain_projection,
)
from app.canonical_v13.research_qualification import persist_qualification_receipt
from app.canonical_v13.research_scoring import persist_scoring_receipt
from app.canonical_v13.research_gates import (
    CanonicalGateBlocked,
    claim_gate_attempt,
    create_gate_attempt,
    list_gate_projections,
    persist_lookahead_gate_receipt,
    persist_static_gate_receipt,
    read_gate_projection,
    recover_expired_gate_attempts,
)
from app.canonical_v13.research_validation import (
    CanonicalResearchValidationBlocked,
    ResearchLineage,
    StaticFinding,
    StaticValidationReceipt,
    build_ephemeral_launch_spec,
    build_lookahead_receipt,
    declare_validation_plan,
    mark_validation_plan_ready,
    validate_static_source,
)
from app.canonical_v13.runtime_contract import (
    CanonicalRuntimeContractBlocked,
    FrozenRuntimeLaunchSpec,
    RuntimeObservationReceipt,
    frozen_runtime_launch_spec_digest,
    verify_runtime_observation_receipt,
)
from app.canonical_v13.models import (
    CONFIGURATION_ACTIVATIONS_TABLE,
    CONFIGURATION_BUNDLE_MEMBERS_TABLE,
    CONFIGURATION_BUNDLES_TABLE,
    CONFIGURATION_PROFILES_TABLE,
    CONFIGURATION_SNAPSHOTS_TABLE,
    CONFIGURATION_VERSIONS_TABLE,
    DEPLOYMENTS_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    MARKET_ARTIFACTS_TABLE,
    MARKET_PROFILES_TABLE,
    MARKET_PROFILE_VERSIONS_TABLE,
    MARKET_RECEIPTS_TABLE,
    MARKET_SNAPSHOTS_TABLE,
    MARKET_SNAPSHOT_MEMBERS_TABLE,
    OPTIMIZATION_RUNS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    QUALIFICATION_WINDOW_EVIDENCE_TABLE,
    RESEARCH_GATE_RECEIPTS_TABLE,
    RESEARCH_TARGETS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    STRATEGIES_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_SUBMISSIONS_TABLE,
    STRATEGY_VERSIONS_TABLE,
    TARGET_SCORES_TABLE,
    VALIDATION_ATTEMPTS_TABLE,
    VALIDATION_PLANS_TABLE,
    VALIDATION_PLAN_WINDOWS_TABLE,
    VALIDATION_WINDOW_RESULTS_TABLE,
)


API_PREFIX = "/api/canonical-v13"
_T = TypeVar("_T")


class CanonicalConnectionFactory(Protocol):
    def __call__(self) -> AbstractContextManager[Connection]: ...


class CanonicalAPIBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


_CANONICAL_DOMAIN_ERRORS = (
    CanonicalAPIBlocked,
    CanonicalBundleBlocked,
    CanonicalControlPlaneBlocked,
    CanonicalConfigurationGovernanceBlocked,
    CanonicalGenesisBlocked,
    CanonicalIntakeBlocked,
    CanonicalMarketBlocked,
    CanonicalFreshMarketRolloutBlocked,
    CanonicalMarketAcquisitionBlocked,
    CanonicalMarketPlanningBlocked,
    CanonicalOptimizationBlocked,
    CanonicalResearchAuthorizationBlocked,
    CanonicalResearchExecutionBlocked,
    CanonicalResearchOrchestrationBlocked,
    CanonicalGateBlocked,
    CanonicalResearchValidationBlocked,
    CanonicalEvaluationBlocked,
    CanonicalPhase9ReadinessBlocked,
    CanonicalDeploymentApprovalBlocked,
    CanonicalDeploymentBlocked,
    CanonicalExecutionChainBlocked,
)


def _error_status(code: str) -> int:
    if code == "BLOCKED_WRONG_CANONICAL_DATABASE":
        return 503
    if "NOT_FOUND" in code or code.endswith("_MISSING"):
        return 404
    if "INVALID" in code or code.startswith("REJECTED_"):
        return 422
    return 409


def _error_response(code: str, detail: str, *, status_code: int) -> JSONResponse:
    payload = CanonicalErrorResponseDTO(
        error=CanonicalErrorDetailDTO(code=code, detail=detail)
    )
    return JSONResponse(
        status_code=status_code, content=payload.model_dump(mode="json")
    )


def _effective_connection(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _research_lineage(value: ResearchLineageDTO) -> ResearchLineage:
    return ResearchLineage(**value.model_dump())


def _authorization_consumption(
    value: ResearchAuthorizationConsumptionReceiptDTO,
) -> ResearchAuthorizationConsumption:
    return ResearchAuthorizationConsumption(
        authorization_id=value.authorization_id,
        consumption_id=value.consumption_id,
        attempt_id=value.attempt_id,
        lineage=_research_lineage(value.lineage),
        validation_plan_id=value.validation_plan_id,
        validation_plan_digest=value.validation_plan_digest,
        actor_identity=value.actor_identity,
        authorization_receipt_digest=value.authorization_receipt_digest,
        request_digest=value.request_digest,
        receipt_digest=value.receipt_digest,
        consumed_at=value.consumed_at,
        environment_class=value.environment_class,
    )


def _require_exact_ready_validation_plan(
    connection: Connection,
    *,
    lineage: ResearchLineage,
    validation_plan_id: UUID,
    validation_plan_digest: str,
) -> None:
    plan = (
        connection.execute(
            select(VALIDATION_PLANS_TABLE).where(
                VALIDATION_PLANS_TABLE.c.id == validation_plan_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        plan is None
        or plan["status"] != "READY"
        or plan["validation_plan_digest"] != validation_plan_digest
        or plan["strategy_version_id"] != lineage.strategy_version_id
        or plan["research_target_id"] != lineage.research_target_id
        or plan["configuration_bundle_id"] != lineage.configuration_bundle_id
        or plan["configuration_bundle_digest"] != lineage.configuration_bundle_digest
        or plan["market_snapshot_id"] != lineage.market_snapshot_id
        or plan["market_snapshot_digest"] != lineage.market_snapshot_digest
    ):
        raise CanonicalAPIBlocked(
            "BLOCKED_AUTHORIZATION_PLAN_NOT_READY",
            "authorization requires one exact READY validation plan",
        )


def _qualification_status(connection: Connection, strategy_version_id: UUID) -> str:
    status = connection.execute(
        select(QUALIFICATION_DECISIONS_TABLE.c.status)
        .where(
            QUALIFICATION_DECISIONS_TABLE.c.strategy_version_id == strategy_version_id
        )
        .order_by(QUALIFICATION_DECISIONS_TABLE.c.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return str(status) if status is not None else "NOT_EVALUATED"


def _strategy_projection(
    connection: Connection, strategy: dict[str, Any]
) -> StrategyProjectionDTO:
    submission = (
        connection.execute(
            select(STRATEGY_SUBMISSIONS_TABLE).where(
                STRATEGY_SUBMISSIONS_TABLE.c.id == strategy["source_submission_id"]
            )
        )
        .mappings()
        .one()
    )
    version = (
        connection.execute(
            select(STRATEGY_VERSIONS_TABLE)
            .where(STRATEGY_VERSIONS_TABLE.c.strategy_id == strategy["id"])
            .order_by(STRATEGY_VERSIONS_TABLE.c.version_number.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if version is None:
        raise CanonicalAPIBlocked(
            "BLOCKED_STRATEGY_VERSION_MISSING",
            "canonical strategy has no current version",
        )
    artifact = (
        connection.execute(
            select(STRATEGY_ARTIFACTS_TABLE).where(
                STRATEGY_ARTIFACTS_TABLE.c.id == version["artifact_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    if artifact is None:
        raise CanonicalAPIBlocked(
            "BLOCKED_STRATEGY_ARTIFACT_MISSING",
            "current strategy version has no canonical artifact",
        )
    return StrategyProjectionDTO(
        strategy_id=strategy["id"],
        display_name=strategy["display_name"],
        catalog_status=strategy["catalog_status"],
        intake_status=submission["status"],
        current_version_id=version["id"],
        version_number=version["version_number"],
        artifact_id=artifact["id"],
        artifact_digest=artifact["content_digest"],
        validation_status=version["validation_status"],
        qualification_status=_qualification_status(connection, version["id"]),
        execution_authorized=version["execution_authorized"],
        created_at=strategy["created_at"],
    )


def _configuration_catalog(connection: Connection) -> ConfigurationCatalogProjectionDTO:
    profiles = (
        connection.execute(
            select(CONFIGURATION_PROFILES_TABLE).order_by(
                CONFIGURATION_PROFILES_TABLE.c.configuration_kind,
                CONFIGURATION_PROFILES_TABLE.c.profile_key,
            )
        )
        .mappings()
        .all()
    )
    items: list[ConfigurationProfileProjectionDTO] = []
    for profile in profiles:
        version_rows = (
            connection.execute(
                select(CONFIGURATION_VERSIONS_TABLE)
                .where(CONFIGURATION_VERSIONS_TABLE.c.profile_id == profile["id"])
                .order_by(CONFIGURATION_VERSIONS_TABLE.c.version_number)
            )
            .mappings()
            .all()
        )
        versions: list[ConfigurationVersionProjectionDTO] = []
        for version in version_rows:
            snapshot = (
                connection.execute(
                    select(CONFIGURATION_SNAPSHOTS_TABLE).where(
                        CONFIGURATION_SNAPSHOTS_TABLE.c.configuration_version_id
                        == version["id"]
                    )
                )
                .mappings()
                .one_or_none()
            )
            versions.append(
                ConfigurationVersionProjectionDTO(
                    version_id=version["id"],
                    version_number=version["version_number"],
                    lifecycle_status=version["lifecycle_status"],
                    configuration_schema=version["schema_json"],
                    payload_json=version["payload_json"],
                    schema_digest=version["schema_digest"],
                    payload_digest=version["payload_digest"],
                    adapter_identity=version["adapter_identity"],
                    adapter_digest=version["adapter_digest"],
                    snapshot_id=snapshot["id"] if snapshot else None,
                    snapshot_digest=snapshot["snapshot_digest"] if snapshot else None,
                    created_at=version["created_at"],
                    validated_at=version["validated_at"],
                )
            )
        items.append(
            ConfigurationProfileProjectionDTO(
                profile_id=profile["id"],
                profile_key=profile["profile_key"],
                configuration_kind=profile["configuration_kind"],
                scope_key=profile["scope_key"],
                workflow_key=profile["workflow_key"],
                versions=versions,
            )
        )
    configured = sorted({item.configuration_kind for item in items})
    unset = [kind for kind in P0_CONFIGURATION_KINDS if kind not in configured]
    return ConfigurationCatalogProjectionDTO(
        status="AVAILABLE" if items else "UNSET",
        configured_kinds=configured,
        unset_kinds=unset,
        items=items,
    )


def _market_inventory(connection: Connection) -> MarketInventoryProjectionDTO:
    profile_count = int(
        connection.execute(
            select(func.count()).select_from(MARKET_PROFILES_TABLE)
        ).scalar_one()
    )
    validated_profile_count = int(
        connection.execute(
            select(func.count())
            .select_from(MARKET_PROFILE_VERSIONS_TABLE)
            .where(MARKET_PROFILE_VERSIONS_TABLE.c.lifecycle_status == "VALIDATED")
        ).scalar_one()
    )
    artifact_count = int(
        connection.execute(
            select(func.count()).select_from(MARKET_ARTIFACTS_TABLE)
        ).scalar_one()
    )
    accepted_receipt_count = int(
        connection.execute(
            select(func.count())
            .select_from(MARKET_RECEIPTS_TABLE)
            .where(MARKET_RECEIPTS_TABLE.c.status == "ACCEPTED")
        ).scalar_one()
    )
    rows = (
        connection.execute(
            select(MARKET_SNAPSHOTS_TABLE).order_by(
                MARKET_SNAPSHOTS_TABLE.c.created_at,
                MARKET_SNAPSHOTS_TABLE.c.id,
            )
        )
        .mappings()
        .all()
    )
    profile_rows = (
        connection.execute(
            select(MARKET_PROFILES_TABLE).order_by(
                MARKET_PROFILES_TABLE.c.profile_key,
                MARKET_PROFILES_TABLE.c.id,
            )
        )
        .mappings()
        .all()
    )
    profiles: list[MarketProfileVersionProjectionDTO] = []
    for profile in profile_rows:
        version_rows = (
            connection.execute(
                select(MARKET_PROFILE_VERSIONS_TABLE)
                .where(
                    MARKET_PROFILE_VERSIONS_TABLE.c.market_profile_id == profile["id"]
                )
                .order_by(
                    MARKET_PROFILE_VERSIONS_TABLE.c.version_number,
                    MARKET_PROFILE_VERSIONS_TABLE.c.id,
                )
            )
            .mappings()
            .all()
        )
        profiles.extend(
            MarketProfileVersionProjectionDTO(
                market_profile_id=profile["id"],
                profile_key=profile["profile_key"],
                scope_key=profile["scope_key"],
                version_id=version["id"],
                version_number=version["version_number"],
                lifecycle_status=version["lifecycle_status"],
                payload_digest=version["payload_digest"],
                created_at=version["created_at"],
                validated_at=version["validated_at"],
            )
            for version in version_rows
        )
    snapshots = [
        MarketSnapshotSummaryDTO(
            snapshot_id=row["id"],
            snapshot_digest=row["snapshot_digest"],
            market_profile_version_id=row["market_profile_version_id"],
            member_count=int(
                connection.execute(
                    select(func.count())
                    .select_from(MARKET_SNAPSHOT_MEMBERS_TABLE)
                    .where(
                        MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_snapshot_id == row["id"]
                    )
                ).scalar_one()
            ),
            created_at=row["created_at"],
        )
        for row in rows
    ]
    return MarketInventoryProjectionDTO(
        status="AVAILABLE" if snapshots else "MARKET_SNAPSHOT_UNSET",
        profile_count=profile_count,
        validated_profile_count=validated_profile_count,
        artifact_count=artifact_count,
        accepted_receipt_count=accepted_receipt_count,
        profiles=profiles,
        snapshots=snapshots,
    )


def _research_plan_catalog(
    connection: Connection,
) -> ResearchPlanCatalogProjectionDTO:
    plan_ids = (
        connection.execute(
            select(VALIDATION_PLANS_TABLE.c.id).order_by(
                VALIDATION_PLANS_TABLE.c.created_at,
                VALIDATION_PLANS_TABLE.c.id,
            )
        )
        .scalars()
        .all()
    )
    items = [
        ResearchChainProjectionDTO(
            **read_research_chain_projection(
                connection, validation_plan_id=validation_plan_id
            ).__dict__
        )
        for validation_plan_id in plan_ids
    ]
    return ResearchPlanCatalogProjectionDTO(
        status="AVAILABLE" if items else "EMPTY",
        items=items,
    )


def _research_results_projection(
    connection: Connection, validation_plan_id: UUID
) -> ResearchResultsProjectionDTO:
    plan = (
        connection.execute(
            select(VALIDATION_PLANS_TABLE).where(
                VALIDATION_PLANS_TABLE.c.id == validation_plan_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if plan is None:
        raise CanonicalAPIBlocked(
            "BLOCKED_VALIDATION_PLAN_NOT_FOUND",
            "canonical validation plan is absent",
        )
    target_key = connection.execute(
        select(RESEARCH_TARGETS_TABLE.c.target_key).where(
            RESEARCH_TARGETS_TABLE.c.id == plan["research_target_id"]
        )
    ).scalar_one()
    attempt = (
        connection.execute(
            select(VALIDATION_ATTEMPTS_TABLE)
            .where(VALIDATION_ATTEMPTS_TABLE.c.validation_plan_id == validation_plan_id)
            .order_by(
                VALIDATION_ATTEMPTS_TABLE.c.attempt_number.desc(),
                VALIDATION_ATTEMPTS_TABLE.c.id.desc(),
            )
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    plan_windows = (
        connection.execute(
            select(VALIDATION_PLAN_WINDOWS_TABLE)
            .where(
                VALIDATION_PLAN_WINDOWS_TABLE.c.validation_plan_id == validation_plan_id
            )
            .order_by(
                VALIDATION_PLAN_WINDOWS_TABLE.c.window_start,
                VALIDATION_PLAN_WINDOWS_TABLE.c.window_key,
                VALIDATION_PLAN_WINDOWS_TABLE.c.id,
            )
        )
        .mappings()
        .all()
    )
    result_rows = (
        []
        if attempt is None
        else connection.execute(
            select(VALIDATION_WINDOW_RESULTS_TABLE).where(
                VALIDATION_WINDOW_RESULTS_TABLE.c.validation_attempt_id == attempt["id"]
            )
        )
        .mappings()
        .all()
    )
    results_by_window = {row["validation_plan_window_id"]: row for row in result_rows}
    plan_window_ids = {window["id"] for window in plan_windows}
    if not set(results_by_window).issubset(plan_window_ids):
        raise CanonicalAPIBlocked(
            "BLOCKED_RESEARCH_RESULT_LINEAGE",
            "validation attempt contains a result from another plan",
        )
    score = (
        connection.execute(
            select(TARGET_SCORES_TABLE).where(
                TARGET_SCORES_TABLE.c.validation_plan_id == validation_plan_id,
                TARGET_SCORES_TABLE.c.validation_plan_digest
                == plan["validation_plan_digest"],
            )
        )
        .mappings()
        .one_or_none()
    )
    qualification = (
        connection.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.validation_plan_id
                == validation_plan_id,
                QUALIFICATION_DECISIONS_TABLE.c.validation_plan_digest
                == plan["validation_plan_digest"],
            )
        )
        .mappings()
        .one_or_none()
    )
    if (score is not None or qualification is not None) and attempt is None:
        raise CanonicalAPIBlocked(
            "BLOCKED_RESEARCH_RESULT_LINEAGE",
            "score or qualification exists without a validation attempt",
        )
    if qualification is not None and (
        score is None or qualification["target_score_id"] != score["id"]
    ):
        raise CanonicalAPIBlocked(
            "BLOCKED_RESEARCH_RESULT_LINEAGE",
            "qualification does not reference the exact target score",
        )
    evidence_rows = (
        []
        if qualification is None
        else connection.execute(
            select(QUALIFICATION_WINDOW_EVIDENCE_TABLE).where(
                QUALIFICATION_WINDOW_EVIDENCE_TABLE.c.qualification_decision_id
                == qualification["id"]
            )
        )
        .mappings()
        .all()
    )
    evidence_by_window = {
        row["validation_plan_window_id"]: row for row in evidence_rows
    }
    if not set(evidence_by_window).issubset(plan_window_ids):
        raise CanonicalAPIBlocked(
            "BLOCKED_RESEARCH_RESULT_LINEAGE",
            "qualification contains evidence from another plan",
        )
    for window_id, evidence in evidence_by_window.items():
        result = results_by_window.get(window_id)
        if result is None or evidence["validation_window_result_id"] != result["id"]:
            raise CanonicalAPIBlocked(
                "BLOCKED_RESEARCH_RESULT_LINEAGE",
                "qualification evidence does not reference the displayed window result",
            )
    windows: list[ResearchWindowProjectionDTO] = []
    for window in plan_windows:
        result = results_by_window.get(window["id"])
        evidence = evidence_by_window.get(window["id"])
        evidence_projection = None
        if evidence is not None:
            payload = evidence["evidence_json"]
            gates = payload.get("gates") if isinstance(payload, dict) else None
            if not isinstance(gates, list) or not all(
                isinstance(gate, dict) for gate in gates
            ):
                raise CanonicalAPIBlocked(
                    "BLOCKED_QUALIFICATION_EVIDENCE_CONTRACT",
                    "qualification window evidence has no canonical gates",
                )
            evidence_projection = ResearchQualificationWindowEvidenceProjectionDTO(
                qualification_window_evidence_id=evidence["id"],
                hard_gate_passed=evidence["hard_gate_passed"],
                gates=[ResearchGateEvaluationProjectionDTO(**gate) for gate in gates],
                evidence_digest=evidence["evidence_digest"],
            )
        windows.append(
            ResearchWindowProjectionDTO(
                validation_plan_window_id=window["id"],
                window_key=window["window_key"],
                required=window["required"],
                window_start=window["window_start"],
                window_end=window["window_end"],
                window_member_digest=window["window_member_digest"],
                result=None
                if result is None
                else ResearchWindowResultProjectionDTO(
                    validation_window_result_id=result["id"],
                    metrics_json=result["metrics_json"],
                    metrics_digest=result["metrics_digest"],
                    receipt_digest=result["receipt_digest"],
                    created_at=result["created_at"],
                ),
                qualification_evidence=evidence_projection,
            )
        )
    return ResearchResultsProjectionDTO(
        validation_plan_id=plan["id"],
        validation_plan_digest=plan["validation_plan_digest"],
        strategy_version_id=plan["strategy_version_id"],
        research_target_id=plan["research_target_id"],
        target_key=target_key,
        configuration_bundle_id=plan["configuration_bundle_id"],
        configuration_bundle_digest=plan["configuration_bundle_digest"],
        market_snapshot_id=plan["market_snapshot_id"],
        market_snapshot_digest=plan["market_snapshot_digest"],
        plan_status=plan["status"],
        attempt=None
        if attempt is None
        else ResearchAttemptProjectionDTO(
            validation_attempt_id=attempt["id"],
            attempt_number=attempt["attempt_number"],
            status=attempt["status"],
            executor_identity=attempt["executor_identity"],
            executor_image_digest=attempt["executor_image_digest"],
            receipt_digest=attempt["receipt_digest"],
            created_at=attempt["created_at"],
            completed_at=attempt["completed_at"],
        ),
        windows=windows,
        score=None
        if score is None
        else ResearchScoreProjectionDTO(
            target_score_id=score["id"],
            scoring_snapshot_id=score["scoring_snapshot_id"],
            overall_score=str(score["overall_score"]),
            required_window_result_set_digest=score[
                "required_window_result_set_digest"
            ],
            score_digest=score["score_digest"],
            scorer_identity=score["scorer_identity"],
            created_at=score["created_at"],
        ),
        qualification=None
        if qualification is None
        else ResearchQualificationProjectionDTO(
            qualification_decision_id=qualification["id"],
            target_score_id=qualification["target_score_id"],
            quality_snapshot_id=qualification["quality_snapshot_id"],
            status=qualification["status"],
            reason_code=qualification["reason_code"],
            decision_digest=qualification["decision_digest"],
            qualifier_identity=qualification["qualifier_identity"],
            evidence_count=len(evidence_rows),
            created_at=qualification["created_at"],
        ),
    )


def _market_snapshot(
    connection: Connection, snapshot_id: UUID
) -> MarketSnapshotProjectionDTO:
    snapshot = (
        connection.execute(
            select(MARKET_SNAPSHOTS_TABLE).where(
                MARKET_SNAPSHOTS_TABLE.c.id == snapshot_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if snapshot is None:
        raise CanonicalAPIBlocked(
            "BLOCKED_MARKET_SNAPSHOT_NOT_FOUND", "canonical market snapshot is absent"
        )
    member_rows = (
        connection.execute(
            select(MARKET_SNAPSHOT_MEMBERS_TABLE)
            .where(MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_snapshot_id == snapshot_id)
            .order_by(MARKET_SNAPSHOT_MEMBERS_TABLE.c.research_target_id)
        )
        .mappings()
        .all()
    )
    reasons: list[str] = []
    members: list[MarketSnapshotMemberProjectionDTO] = []
    if not member_rows:
        reasons.append("MARKET_SNAPSHOT_EMPTY")
    for member in member_rows:
        artifact = (
            connection.execute(
                select(MARKET_ARTIFACTS_TABLE).where(
                    MARKET_ARTIFACTS_TABLE.c.id == member["market_artifact_id"]
                )
            )
            .mappings()
            .one_or_none()
        )
        receipt = (
            connection.execute(
                select(MARKET_RECEIPTS_TABLE).where(
                    MARKET_RECEIPTS_TABLE.c.id == member["market_receipt_id"]
                )
            )
            .mappings()
            .one_or_none()
        )
        target = (
            connection.execute(
                select(RESEARCH_TARGETS_TABLE).where(
                    RESEARCH_TARGETS_TABLE.c.id == member["research_target_id"]
                )
            )
            .mappings()
            .one_or_none()
        )
        if artifact is None or receipt is None or target is None:
            raise CanonicalAPIBlocked(
                "BLOCKED_MARKET_SNAPSHOT_LINEAGE_MISSING",
                "market snapshot member lineage is incomplete",
            )
        if (
            receipt["status"] != "ACCEPTED"
            or receipt["market_artifact_id"] != artifact["id"]
            or receipt["artifact_digest"] != artifact["content_digest"]
        ):
            reasons.append("MARKET_RECEIPT_NOT_ACCEPTED")
        members.append(
            MarketSnapshotMemberProjectionDTO(
                market_artifact_id=artifact["id"],
                artifact_digest=artifact["content_digest"],
                market_receipt_id=receipt["id"],
                receipt_digest=receipt["receipt_digest"],
                receipt_status=receipt["status"],
                research_target_id=target["id"],
                target_key=target["target_key"],
                coverage_start=member["coverage_start"],
                coverage_end=member["coverage_end"],
                coverage_digest=member["coverage_digest"],
            )
        )
    unique_reasons = list(dict.fromkeys(reasons))
    return MarketSnapshotProjectionDTO(
        snapshot_id=snapshot["id"],
        snapshot_digest=snapshot["snapshot_digest"],
        market_profile_version_id=snapshot["market_profile_version_id"],
        status="BLOCKED" if unique_reasons else "ACCEPTED",
        reason_codes=unique_reasons,
        members=members,
        created_at=snapshot["created_at"],
    )


def _research_readiness(
    connection: Connection,
    *,
    scope_key: Optional[str],
    workflow_key: Optional[str],
) -> ReadinessProjectionDTO:
    if (scope_key is None) != (workflow_key is None):
        return ReadinessProjectionDTO(
            status="BLOCKED", reason_codes=["RESEARCH_SCOPE_INCOMPLETE"]
        )
    query = select(CONFIGURATION_ACTIVATIONS_TABLE)
    if scope_key is not None and workflow_key is not None:
        query = query.where(
            CONFIGURATION_ACTIVATIONS_TABLE.c.scope_key == scope_key,
            CONFIGURATION_ACTIVATIONS_TABLE.c.workflow_key == workflow_key,
        )
    activations = connection.execute(query).mappings().all()
    if not activations:
        return ReadinessProjectionDTO(
            status="BLOCKED",
            reason_codes=["RESEARCH_BUNDLE_UNSET"],
            scope_key=scope_key,
            workflow_key=workflow_key,
        )
    if len(activations) != 1:
        return ReadinessProjectionDTO(
            status="BLOCKED",
            reason_codes=["RESEARCH_ACTIVATION_AMBIGUOUS"],
            scope_key=scope_key,
            workflow_key=workflow_key,
        )
    activation = activations[0]
    bundle = (
        connection.execute(
            select(CONFIGURATION_BUNDLES_TABLE).where(
                CONFIGURATION_BUNDLES_TABLE.c.id
                == activation["configuration_bundle_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    if bundle is None or bundle["bundle_digest"] != activation["bundle_digest"]:
        return ReadinessProjectionDTO(
            status="BLOCKED",
            reason_codes=["ACTIVE_BUNDLE_LINEAGE_DRIFT"],
            scope_key=activation["scope_key"],
            workflow_key=activation["workflow_key"],
        )
    member_rows = (
        connection.execute(
            select(CONFIGURATION_BUNDLE_MEMBERS_TABLE).where(
                CONFIGURATION_BUNDLE_MEMBERS_TABLE.c.configuration_bundle_id
                == bundle["id"]
            )
        )
        .mappings()
        .all()
    )
    snapshot_ids = {
        row["configuration_kind"]: row["configuration_snapshot_id"]
        for row in member_rows
    }
    reasons: list[str] = []
    if len(member_rows) != len(P0_CONFIGURATION_KINDS) or set(snapshot_ids) != set(
        P0_CONFIGURATION_KINDS
    ):
        reasons.append("ACTIVE_BUNDLE_MEMBER_SET_INVALID")
    preview = preview_research_bundle(
        connection,
        scope_key=activation["scope_key"],
        workflow_key=activation["workflow_key"],
        snapshot_ids=snapshot_ids,
        market_snapshot_id=bundle["market_snapshot_id"],
    )
    reasons.extend(preview.reason_codes)
    if preview.bundle_digest != bundle["bundle_digest"]:
        reasons.append("ACTIVE_BUNDLE_DIGEST_DRIFT")
    unique_reasons = list(dict.fromkeys(reasons))
    completed_qualification_count = int(
        connection.execute(
            select(func.count())
            .select_from(QUALIFICATION_DECISIONS_TABLE)
            .where(
                QUALIFICATION_DECISIONS_TABLE.c.configuration_bundle_id == bundle["id"],
                QUALIFICATION_DECISIONS_TABLE.c.configuration_bundle_digest
                == bundle["bundle_digest"],
                QUALIFICATION_DECISIONS_TABLE.c.market_snapshot_id
                == bundle["market_snapshot_id"],
                QUALIFICATION_DECISIONS_TABLE.c.market_snapshot_digest
                == bundle["market_snapshot_digest"],
                QUALIFICATION_DECISIONS_TABLE.c.status != "PENDING",
            )
        ).scalar_one()
    )
    if unique_reasons:
        status = "BLOCKED"
        readiness_reasons = unique_reasons
    elif completed_qualification_count:
        status = "READY"
        readiness_reasons = []
    else:
        status = "PENDING_FIRST_BACKTEST"
        readiness_reasons = ["PENDING_FIRST_BACKTEST"]
    return ReadinessProjectionDTO(
        status=status,
        reason_codes=readiness_reasons,
        scope_key=activation["scope_key"],
        workflow_key=activation["workflow_key"],
        configuration_bundle_id=bundle["id"],
        bundle_digest=bundle["bundle_digest"],
        market_snapshot_id=bundle["market_snapshot_id"],
        target_count=preview.target_count,
        total_candidate_count=preview.total_candidate_count,
    )


def _runtime_readiness(connection: Connection) -> ReadinessProjectionDTO:
    deployments = (
        connection.execute(
            select(DEPLOYMENTS_TABLE)
            .where(DEPLOYMENTS_TABLE.c.status == "ACTIVE")
            .order_by(DEPLOYMENTS_TABLE.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    if not deployments:
        return ReadinessProjectionDTO(
            status="BLOCKED",
            reason_codes=["TRADING_DISABLED", "ACTIVE_DEPLOYMENT_UNSET"],
        )
    if len(deployments) != 1:
        return ReadinessProjectionDTO(
            status="BLOCKED", reason_codes=["ACTIVE_DEPLOYMENT_AMBIGUOUS"]
        )
    deployment = deployments[0]
    reasons: list[str] = []
    if deployment["demo_only"] is not True:
        reasons.append("DEMO_ONLY_INVARIANT_FAILED")
    if deployment["allow_real_funds"] is not False:
        reasons.append("REAL_FUNDS_INVARIANT_FAILED")
    approval = (
        connection.execute(
            select(DEPLOYMENT_APPROVALS_TABLE).where(
                DEPLOYMENT_APPROVALS_TABLE.c.id == deployment["deployment_approval_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    qualification = (
        connection.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == approval["qualification_decision_id"]
            )
        )
        .mappings()
        .one_or_none()
        if approval is not None
        else None
    )
    if approval is None or approval["status"] != "APPROVED":
        reasons.append("ACTIVE_APPROVAL_REQUIRED")
    if qualification is None or qualification["status"] != "QUALIFIED":
        reasons.append("QUALIFIED_DECISION_REQUIRED")
    elif (
        qualification["strategy_version_id"] != deployment["strategy_version_id"]
        or qualification["configuration_bundle_id"]
        != deployment["configuration_bundle_id"]
        or qualification["configuration_bundle_digest"]
        != deployment["configuration_bundle_digest"]
        or qualification["market_snapshot_id"] != deployment["market_snapshot_id"]
        or qualification["market_snapshot_digest"]
        != deployment["market_snapshot_digest"]
    ):
        reasons.append("DEPLOYMENT_QUALIFICATION_LINEAGE_DRIFT")
    if qualification is not None:
        try:
            qualification_gate = gate_optimization(
                connection,
                baseline_qualification_decision_id=qualification["id"],
            )
        except CanonicalEvaluationBlocked:
            reasons.append("QUALIFICATION_RECEIPT_DIGEST_DRIFT")
        else:
            if qualification_gate.status != "READY":
                reasons.append("QUALIFICATION_RECEIPT_DIGEST_DRIFT")
    bundle = (
        connection.execute(
            select(CONFIGURATION_BUNDLES_TABLE).where(
                CONFIGURATION_BUNDLES_TABLE.c.id
                == deployment["configuration_bundle_id"]
            )
        )
        .mappings()
        .one_or_none()
    )
    if approval is not None and qualification is not None:
        expected_approval_digest = deployment_approval_digest(
            approval_id=approval["id"],
            qualification_decision_id=approval["qualification_decision_id"],
            decision=qualification,
            actor_identity=approval["actor_identity"],
            reason=approval["reason"],
        )
        if expected_approval_digest != approval["approval_digest"]:
            reasons.append("APPROVAL_RECEIPT_DIGEST_DRIFT")
    if bundle is None or qualification is None or approval is None:
        reasons.append("DEPLOYMENT_CAPABILITY_LINEAGE_INCOMPLETE")
    else:
        expected_capability_digest = deployment_capability_digest(
            deployment_approval_id=approval["id"],
            approval_digest=approval["approval_digest"],
            decision=qualification,
            bundle=bundle,
        )
        if expected_capability_digest != deployment["capability_digest"]:
            reasons.append("DEPLOYMENT_CAPABILITY_DIGEST_DRIFT")
    runtimes = (
        connection.execute(
            select(RUNTIME_INSTANCES_TABLE).where(
                RUNTIME_INSTANCES_TABLE.c.deployment_id == deployment["id"],
                RUNTIME_INSTANCES_TABLE.c.status == "HEALTHY",
            )
        )
        .mappings()
        .all()
    )
    if len(runtimes) != 1:
        reasons.append(
            "RUNTIME_NOT_HEALTHY" if not runtimes else "HEALTHY_RUNTIME_AMBIGUOUS"
        )
        runtime = None
    else:
        runtime = runtimes[0]
        if runtime["order_writer_capability"] is not False:
            reasons.append("RUNTIME_ORDER_WRITER_FORBIDDEN")
        try:
            expected_launch_digest = frozen_runtime_launch_spec_digest(
                FrozenRuntimeLaunchSpec(
                    deployment_id=deployment["id"],
                    approval_id=approval["id"]
                    if approval
                    else deployment["deployment_approval_id"],
                    qualification_decision_id=(
                        qualification["id"] if qualification else UUID(int=0)
                    ),
                    strategy_version_id=deployment["strategy_version_id"],
                    configuration_bundle_id=deployment["configuration_bundle_id"],
                    configuration_bundle_digest=deployment[
                        "configuration_bundle_digest"
                    ],
                    market_snapshot_id=deployment["market_snapshot_id"],
                    market_snapshot_digest=deployment["market_snapshot_digest"],
                    deployment_capability_digest=deployment["capability_digest"],
                    runtime_identity=runtime["runtime_identity"],
                    image_digest=runtime["image_digest"],
                    service_account=runtime["service_account"],
                    network_policy=runtime["network_policy"],
                    credential_reference=runtime["credential_reference"],
                    runtime_class=runtime["runtime_class"],
                    filesystem_mode=runtime["filesystem_mode"],
                    research_executor_capability=runtime[
                        "research_executor_capability"
                    ],
                    order_writer_capability=runtime["order_writer_capability"],
                )
            )
        except CanonicalRuntimeContractBlocked:
            reasons.append("RUNTIME_LAUNCH_CAPABILITY_DRIFT")
            expected_launch_digest = None
        if expected_launch_digest != runtime["launch_spec_digest"]:
            reasons.append("RUNTIME_LAUNCH_SPEC_DIGEST_DRIFT")
        receipt = (
            connection.execute(
                select(RUNTIME_RECEIPTS_TABLE)
                .where(RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime["id"])
                .order_by(RUNTIME_RECEIPTS_TABLE.c.observed_at.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if receipt is None or receipt["status"] != "HEALTHY":
            reasons.append("RUNTIME_HEALTH_RECEIPT_MISSING")
        elif (
            receipt["launch_spec_digest"] != runtime["launch_spec_digest"]
            or receipt["capability_digest"] != deployment["capability_digest"]
            or receipt["network_policy"] != "DEMO_EXCHANGE_ONLY"
            or receipt["service_account"] != runtime["service_account"]
            or receipt["order_writer_capability"] is not False
            or receipt["evidence_class"] != "PRODUCTION_DEMO_RUNTIME"
        ):
            reasons.append("RUNTIME_RECEIPT_CAPABILITY_DRIFT")
        elif receipt["observed_at"] is None:
            reasons.append("RUNTIME_HEARTBEAT_UNSET")
        else:
            observed = receipt["observed_at"]
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            typed_receipt = RuntimeObservationReceipt(
                runtime_instance_id=runtime["id"],
                launch_spec_digest=receipt["launch_spec_digest"],
                capability_digest=receipt["capability_digest"],
                status=receipt["status"],
                observed_at=observed,
                network_policy=receipt["network_policy"],
                service_account=receipt["service_account"],
                order_writer_capability=receipt["order_writer_capability"],
                evidence_class=receipt["evidence_class"],
                observation_digest=receipt["observation_digest"],
                receipt_digest=receipt["receipt_digest"],
            )
            if not verify_runtime_observation_receipt(typed_receipt):
                reasons.append("RUNTIME_RECEIPT_DIGEST_DRIFT")
            if datetime.now(timezone.utc) - observed > timedelta(minutes=5):
                reasons.append("RUNTIME_HEARTBEAT_STALE")
            elif observed > datetime.now(timezone.utc):
                reasons.append("RUNTIME_HEARTBEAT_IN_FUTURE")
    return ReadinessProjectionDTO(
        status="BLOCKED" if reasons else "READY",
        reason_codes=list(dict.fromkeys(reasons)),
        configuration_bundle_id=deployment["configuration_bundle_id"],
        bundle_digest=deployment["configuration_bundle_digest"],
        market_snapshot_id=deployment["market_snapshot_id"],
        deployment_id=deployment["id"],
        runtime_instance_id=runtime["id"] if runtime else None,
    )


def create_canonical_v13_app(
    *,
    reader_connection_factory: CanonicalConnectionFactory,
    control_connection_factory: CanonicalConnectionFactory,
    validation_connection_factory: CanonicalConnectionFactory | None = None,
    scoring_connection_factory: CanonicalConnectionFactory | None = None,
    qualification_connection_factory: CanonicalConnectionFactory | None = None,
    optimization_connection_factory: CanonicalConnectionFactory | None = None,
    approval_connection_factory: CanonicalConnectionFactory | None = None,
    deployment_connection_factory: CanonicalConnectionFactory | None = None,
    signal_connection_factory: CanonicalConnectionFactory | None = None,
    risk_connection_factory: CanonicalConnectionFactory | None = None,
    market_artifact_root: Path | None = None,
    market_downloader_factory: Callable[[], MarketDownloaderPort] | None = None,
    exchange_metadata_downloader_factory: Callable[
        [], OkxPublicOfflineExchangeMetadataDownloader
    ]
    | None = None,
) -> FastAPI:
    """Create a standalone app with capability-separated database identities."""

    if not callable(reader_connection_factory):
        raise TypeError("reader_connection_factory must be callable")
    if not callable(control_connection_factory):
        raise TypeError("control_connection_factory must be callable")
    if market_downloader_factory is not None and not callable(
        market_downloader_factory
    ):
        raise TypeError("market_downloader_factory must be callable")
    if exchange_metadata_downloader_factory is not None and not callable(
        exchange_metadata_downloader_factory
    ):
        raise TypeError("exchange_metadata_downloader_factory must be callable")
    app = FastAPI(
        title="Freqtrade AI canonical V1.3 API",
        version="canonical-v13-phase4-v1",
    )

    def run(
        factory: CanonicalConnectionFactory, handler: Callable[[Connection], _T]
    ) -> _T:
        with factory() as supplied:
            if not isinstance(supplied, Connection) or supplied.closed:
                raise CanonicalAPIBlocked(
                    "BLOCKED_CANONICAL_CONNECTION_FACTORY",
                    "connection factory did not yield an open SQLAlchemy Connection",
                )
            if supplied.in_transaction():
                raise CanonicalAPIBlocked(
                    "BLOCKED_CANONICAL_TRANSACTION_OWNERSHIP",
                    "connection factory must yield an idle connection",
                )
            with supplied.begin():
                connection = _effective_connection(supplied)
                verification = verify_canonical_genesis(connection)
                if not verification.accepted:
                    raise CanonicalAPIBlocked(
                        "BLOCKED_WRONG_CANONICAL_DATABASE",
                        "; ".join(verification.problems),
                    )
                return handler(connection)

    def run_read(handler: Callable[[Connection], _T]) -> _T:
        return run(reader_connection_factory, handler)

    def run_control(handler: Callable[[Connection], _T]) -> _T:
        return run(control_connection_factory, handler)

    def run_research(
        factory: CanonicalConnectionFactory | None,
        capability: str,
        handler: Callable[[Connection], _T],
    ) -> _T:
        if factory is None:
            raise CanonicalAPIBlocked(
                "BLOCKED_RESEARCH_CAPABILITY_UNPROVISIONED",
                f"{capability} connection factory is not provisioned",
            )
        return run(factory, handler)

    def run_phase9(
        factory: CanonicalConnectionFactory | None,
        capability: str,
        handler: Callable[[Connection], _T],
    ) -> _T:
        if factory is None:
            raise CanonicalAPIBlocked(
                "BLOCKED_PHASE9_CAPABILITY_UNPROVISIONED",
                f"{capability} connection factory is not provisioned",
            )
        return run(factory, handler)

    async def domain_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        code = getattr(exc, "code", "BLOCKED_CANONICAL_API_FAILURE")
        detail = getattr(exc, "detail", "canonical operation failed closed")
        return _error_response(code, detail, status_code=_error_status(code))

    async def validation_error_handler(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            "BLOCKED_INVALID_COMMAND_DTO",
            "request does not match the canonical DTO contract",
            status_code=422,
        )

    async def unexpected_error_handler(
        _request: Request, _exc: Exception
    ) -> JSONResponse:
        return _error_response(
            "BLOCKED_CANONICAL_API_FAILURE",
            "canonical operation failed closed",
            status_code=500,
        )

    async def integrity_error_handler(
        _request: Request, _exc: IntegrityError
    ) -> JSONResponse:
        # Concurrent create/idempotency races are expected to be rejected by
        # canonical UNIQUE constraints.  Keep the database detail private while
        # exposing a stable retry/read-after-conflict contract.
        return _error_response(
            "BLOCKED_CANONICAL_CONCURRENT_CONFLICT",
            "canonical state changed concurrently; re-read before retrying",
            status_code=409,
        )

    for error_type in _CANONICAL_DOMAIN_ERRORS:
        app.add_exception_handler(error_type, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)

    @app.post(
        f"{API_PREFIX}/submissions",
        response_model=SubmissionReceiptDTO,
        status_code=201,
    )
    def submit_strategy(command: SubmissionCommandDTO) -> SubmissionReceiptDTO:
        versions: list[ExternalVersionSnapshot] = []
        for item in command.versions:
            try:
                artifact_bytes = base64.b64decode(
                    item.artifact_base64.encode("ascii"), validate=True
                )
            except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
                raise CanonicalAPIBlocked(
                    "BLOCKED_INVALID_COMMAND_DTO",
                    "artifact_base64 must be canonical base64",
                ) from exc
            versions.append(
                ExternalVersionSnapshot(
                    source_strategy_key=item.source_strategy_key,
                    version_id=item.version_id,
                    version_number=item.version_number,
                    artifact_bytes=artifact_bytes,
                )
            )

        def execute(connection: Connection) -> SubmissionReceiptDTO:
            result = controlled_submit_latest(
                connection,
                caller_identity=command.caller_identity,
                idempotency_key=command.idempotency_key,
                display_name=command.display_name,
                snapshot=ExternalSourceEntrySnapshot(
                    archive_snapshot_digest=command.archive_snapshot_digest,
                    source_entry_key=command.source_entry_key,
                    source_strategy_key=command.source_strategy_key,
                    current_version_id=command.current_version_id,
                    versions=tuple(versions),
                ),
            )
            return SubmissionReceiptDTO(
                submission_id=result.submission_id,
                artifact_id=result.artifact_id,
                strategy_id=result.strategy_id,
                strategy_version_id=result.strategy_version_id,
                intake_receipt_id=result.intake_receipt_id,
                request_digest=result.request_digest,
                artifact_digest=result.artifact_digest,
                receipt_digest=result.receipt_digest,
                intake_status=result.status,
                catalog_status=result.catalog_status,
                validation_status=result.validation_status,
                execution_authorized=result.execution_authorized,
                idempotent_replay=result.idempotent_replay,
            )

        return run_control(execute)

    @app.get(f"{API_PREFIX}/strategies", response_model=StrategyCatalogProjectionDTO)
    def list_strategies(
        limit: int = Query(default=100, ge=1, le=200),
    ) -> StrategyCatalogProjectionDTO:
        def execute(connection: Connection) -> StrategyCatalogProjectionDTO:
            rows = (
                connection.execute(
                    select(STRATEGIES_TABLE)
                    .order_by(STRATEGIES_TABLE.c.created_at, STRATEGIES_TABLE.c.id)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            items = [_strategy_projection(connection, dict(row)) for row in rows]
            return StrategyCatalogProjectionDTO(
                status="AVAILABLE" if items else "EMPTY", items=items
            )

        return run_read(execute)

    @app.get(
        f"{API_PREFIX}/strategies/{{strategy_id}}",
        response_model=StrategyProjectionDTO,
    )
    def get_strategy(strategy_id: UUID) -> StrategyProjectionDTO:
        def execute(connection: Connection) -> StrategyProjectionDTO:
            row = (
                connection.execute(
                    select(STRATEGIES_TABLE).where(STRATEGIES_TABLE.c.id == strategy_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise CanonicalAPIBlocked(
                    "BLOCKED_STRATEGY_NOT_FOUND", "canonical strategy is absent"
                )
            return _strategy_projection(connection, dict(row))

        return run_read(execute)

    @app.get(
        f"{API_PREFIX}/configurations",
        response_model=ConfigurationCatalogProjectionDTO,
    )
    def list_configurations() -> ConfigurationCatalogProjectionDTO:
        return run_read(_configuration_catalog)

    @app.post(
        f"{API_PREFIX}/configurations/{{kind}}/drafts",
        response_model=ConfigurationDraftResultDTO,
        status_code=201,
    )
    def draft_configuration(
        kind: str, command: ConfigurationDraftCommandDTO
    ) -> ConfigurationDraftResultDTO:
        def execute(connection: Connection) -> ConfigurationDraftResultDTO:
            result = create_audited_configuration_draft(
                connection,
                actor_identity=command.actor_identity,
                idempotency_key=command.idempotency_key,
                profile_key=command.profile_key,
                configuration_kind=kind,
                scope_key=command.scope_key,
                workflow_key=command.workflow_key,
                schema_json=command.configuration_schema,
                payload_json=command.payload_json,
                adapter_identity=command.adapter_identity,
                adapter_digest=command.adapter_digest,
                dependencies=tuple(
                    ConfigurationDependencyInput(
                        version_id=item.version_id,
                        expected_kind=item.expected_kind,
                        relation_key=item.relation_key,
                    )
                    for item in command.dependencies
                ),
            )
            return ConfigurationDraftResultDTO(**result)

        return run_control(execute)

    @app.post(
        f"{API_PREFIX}/configurations/{{kind}}/{{version_id}}/validate",
        response_model=ConfigurationValidationResultDTO,
    )
    def validate_configuration(
        kind: str, version_id: UUID, command: ConfigurationValidateCommandDTO
    ) -> ConfigurationValidationResultDTO:
        def execute(connection: Connection) -> ConfigurationValidationResultDTO:
            result = validate_audited_configuration_version(
                connection,
                actor_identity=command.actor_identity,
                idempotency_key=command.idempotency_key,
                configuration_kind=kind,
                version_id=version_id,
                adapter_manifest_digest=command.adapter_manifest_digest,
            )
            return ConfigurationValidationResultDTO(**result)

        return run_control(execute)

    @app.post(
        f"{API_PREFIX}/research-bundles/preview",
        response_model=ResearchBundlePreviewDTO,
    )
    def preview_bundle(
        command: ResearchBundlePreviewCommandDTO,
    ) -> ResearchBundlePreviewDTO:
        def execute(connection: Connection) -> ResearchBundlePreviewDTO:
            result = preview_research_bundle(
                connection,
                scope_key=command.scope_key,
                workflow_key=command.workflow_key,
                snapshot_ids=command.snapshot_ids,
                market_snapshot_id=command.market_snapshot_id,
            )
            return ResearchBundlePreviewDTO(
                status=result.status,
                reason_codes=list(result.reason_codes),
                scope_key=result.scope_key,
                workflow_key=result.workflow_key,
                snapshot_ids=dict(result.snapshot_ids),
                snapshot_digests=dict(result.snapshot_digests),
                market_snapshot_id=result.market_snapshot_id,
                market_snapshot_digest=result.market_snapshot_digest,
                target_count=result.target_count,
                total_candidate_count=result.total_candidate_count,
                capability_json=dict(result.capability_json),
                bundle_digest=result.bundle_digest,
                prospective_bundle_id=result.prospective_bundle_id,
            )

        return run_control(execute)

    @app.post(
        f"{API_PREFIX}/research-bundles/{{bundle_id}}/activate",
        response_model=ResearchBundleActivationDTO,
    )
    def activate_bundle(
        bundle_id: UUID, command: ResearchBundleActivateCommandDTO
    ) -> ResearchBundleActivationDTO:
        def execute(connection: Connection) -> ResearchBundleActivationDTO:
            result = activate_research_bundle(
                connection,
                scope_key=command.scope_key,
                workflow_key=command.workflow_key,
                snapshot_ids=command.snapshot_ids,
                market_snapshot_id=command.market_snapshot_id,
                actor_identity=command.actor_identity,
                expected_bundle_digest=command.expected_bundle_digest,
                expected_bundle_id=bundle_id,
            )
            return ResearchBundleActivationDTO(**result.__dict__)

        return run_control(execute)

    @app.post(
        f"{API_PREFIX}/research/gates/attempts",
        response_model=GateAttemptReceiptDTO,
        status_code=201,
    )
    def create_planless_gate_attempt(
        command: GateAttemptCommandDTO,
    ) -> GateAttemptReceiptDTO:
        result = run_research(
            validation_connection_factory,
            "validation",
            lambda connection: create_gate_attempt(
                connection,
                lineage=_research_lineage(command.lineage),
                idempotency_key=command.idempotency_key,
                release_commit=command.release_commit,
                executor_image_digest=command.executor_image_digest,
                worker_source_digest=command.worker_source_digest,
            ),
        )
        return GateAttemptReceiptDTO(
            gate_attempt_id=result.gate_attempt_id,
            request_digest=result.request_digest,
            status=result.status,
            repeat_noop=result.repeat_noop,
            lineage=ResearchLineageDTO(**result.lineage.__dict__),
        )

    @app.post(
        f"{API_PREFIX}/research/gates/attempts/{{gate_attempt_id}}/claim",
        response_model=GateLeaseReceiptDTO,
    )
    def claim_planless_gate_attempt(
        gate_attempt_id: UUID, command: GateLeaseCommandDTO
    ) -> GateLeaseReceiptDTO:
        del command
        result = run_research(
            validation_connection_factory,
            "validation",
            lambda connection: claim_gate_attempt(
                connection, gate_attempt_id=gate_attempt_id
            ),
        )
        return GateLeaseReceiptDTO(**result.__dict__)

    @app.post(
        f"{API_PREFIX}/research/gates/recover-expired",
        response_model=GateRecoveryReceiptDTO,
    )
    def recover_planless_gate_attempts(
        command: GateRecoveryCommandDTO,
    ) -> GateRecoveryReceiptDTO:
        del command
        recovered_count = run_research(
            validation_connection_factory,
            "validation",
            recover_expired_gate_attempts,
        )
        return GateRecoveryReceiptDTO(
            status="ACCEPTED", recovered_count=recovered_count
        )

    @app.post(
        f"{API_PREFIX}/research/gates/attempts/{{gate_attempt_id}}/static-receipts",
        response_model=GatePersistedReceiptDTO,
        status_code=201,
    )
    def persist_planless_static_receipt(
        gate_attempt_id: UUID, command: StaticGateReceiptCommandDTO
    ) -> GatePersistedReceiptDTO:
        receipt = StaticValidationReceipt(
            strategy_version_id=command.strategy_version_id,
            artifact_digest=command.artifact_digest,
            validator_identity=command.validator_identity,
            validator_digest=command.validator_digest,
            status=command.status,
            findings=tuple(
                StaticFinding(**item.model_dump()) for item in command.findings
            ),
            request_digest=command.request_digest,
            receipt_digest=command.receipt_digest,
        )
        digest = run_research(
            validation_connection_factory,
            "validation",
            lambda connection: persist_static_gate_receipt(
                connection,
                gate_attempt_id=gate_attempt_id,
                lease_token=command.lease_token,
                receipt=receipt,
            ),
        )
        return GatePersistedReceiptDTO(
            gate_attempt_id=gate_attempt_id, gate_type="STATIC", receipt_digest=digest
        )

    @app.post(
        f"{API_PREFIX}/research/gates/attempts/{{gate_attempt_id}}/lookahead-receipts",
        response_model=GatePersistedReceiptDTO,
        status_code=201,
    )
    def persist_planless_lookahead_receipt(
        gate_attempt_id: UUID, command: LookaheadGateReceiptCommandDTO
    ) -> GatePersistedReceiptDTO:
        projection = run_read(
            lambda connection: read_gate_projection(
                connection, gate_attempt_id=gate_attempt_id
            )
        )
        lineage = ResearchLineage(
            strategy_version_id=projection.strategy_version_id,
            research_target_id=projection.research_target_id,
            configuration_bundle_id=projection.configuration_bundle_id,
            configuration_bundle_digest=projection.configuration_bundle_digest,
            market_snapshot_id=projection.market_snapshot_id,
            market_snapshot_digest=projection.market_snapshot_digest,
        )
        receipt = build_lookahead_receipt(
            lineage=lineage,
            artifact_digest=command.artifact_digest,
            analyzer_identity=command.analyzer_identity,
            analyzer_digest=command.analyzer_digest,
            evidence_digest=command.evidence_digest,
            status=command.status,
            has_bias=command.has_bias,
            observed_signal_count=command.observed_signal_count,
            failure_stage=command.failure_stage,
            failure_code=command.failure_code,
            tool_return_code=command.tool_return_code,
            stdout_digest=command.stdout_digest,
            stderr_digest=command.stderr_digest,
            redacted_detail=command.redacted_detail,
            blocked_observed_trade_count=command.blocked_observed_trade_count,
            blocked_required_trade_count=command.blocked_required_trade_count,
        )
        if (
            receipt.request_digest != command.request_digest
            or receipt.receipt_digest != command.receipt_digest
        ):
            raise CanonicalGateBlocked(
                "BLOCKED_GATE_RECEIPT_DIGEST_DRIFT",
                "lookahead receipt does not recompute",
            )
        digest = run_research(
            validation_connection_factory,
            "validation",
            lambda connection: persist_lookahead_gate_receipt(
                connection,
                gate_attempt_id=gate_attempt_id,
                lease_token=command.lease_token,
                receipt=receipt,
            ),
        )
        return GatePersistedReceiptDTO(
            gate_attempt_id=gate_attempt_id,
            gate_type="LOOKAHEAD",
            receipt_digest=digest,
        )

    @app.get(f"{API_PREFIX}/research/gates", response_model=GateListProjectionDTO)
    def planless_gate_list(
        limit: int = Query(default=200, ge=1, le=200),
    ) -> GateListProjectionDTO:
        items = run_read(
            lambda connection: list_gate_projections(connection, limit=limit)
        )
        return GateListProjectionDTO(
            status="AVAILABLE" if items else "EMPTY",
            items=[GateProjectionDTO(**item.__dict__) for item in items],
        )

    @app.get(
        f"{API_PREFIX}/research/gates/{{gate_attempt_id}}",
        response_model=GateProjectionDTO,
    )
    def planless_gate_status(gate_attempt_id: UUID) -> GateProjectionDTO:
        item = run_read(
            lambda connection: read_gate_projection(
                connection, gate_attempt_id=gate_attempt_id
            )
        )
        return GateProjectionDTO(**item.__dict__)

    @app.post(
        f"{API_PREFIX}/research/validation-plans",
        response_model=ValidationPlanReceiptDTO,
        status_code=201,
    )
    def create_validation_plan(
        command: ValidationPlanCommandDTO,
    ) -> ValidationPlanReceiptDTO:
        def execute(connection: Connection) -> ValidationPlanReceiptDTO:
            lineage = _research_lineage(command.lineage)
            artifact = (
                connection.execute(
                    select(STRATEGY_ARTIFACTS_TABLE)
                    .select_from(
                        STRATEGY_VERSIONS_TABLE.join(
                            STRATEGY_ARTIFACTS_TABLE,
                            STRATEGY_ARTIFACTS_TABLE.c.id
                            == STRATEGY_VERSIONS_TABLE.c.artifact_id,
                        )
                    )
                    .where(STRATEGY_VERSIONS_TABLE.c.id == lineage.strategy_version_id)
                )
                .mappings()
                .one_or_none()
            )
            if artifact is None:
                raise CanonicalAPIBlocked(
                    "BLOCKED_STRATEGY_ARTIFACT_MISSING",
                    "validation plan requires one canonical strategy artifact",
                )
            rows = (
                connection.execute(
                    select(RESEARCH_GATE_RECEIPTS_TABLE).where(
                        RESEARCH_GATE_RECEIPTS_TABLE.c.id.in_(
                            (
                                command.static_gate_receipt_id,
                                command.lookahead_gate_receipt_id,
                            )
                        )
                    )
                )
                .mappings()
                .all()
            )
            by_id = {row["id"]: row for row in rows}
            static_row = by_id.get(command.static_gate_receipt_id)
            lookahead_row = by_id.get(command.lookahead_gate_receipt_id)
            if static_row is None or lookahead_row is None:
                raise CanonicalResearchValidationBlocked(
                    "BLOCKED_GATE_RECEIPT_UNAVAILABLE",
                    "validation plan requires persisted v3 gate receipts",
                )
            static_evidence = static_row["evidence_json"]
            lookahead_evidence = lookahead_row["evidence_json"]
            if not isinstance(static_evidence, dict) or not isinstance(
                lookahead_evidence, dict
            ):
                raise CanonicalResearchValidationBlocked(
                    "BLOCKED_GATE_RECEIPT_DIGEST_DRIFT", "gate evidence shape drifted"
                )
            static_receipt = validate_static_source(
                artifact["normalized_content"],
                strategy_version_id=lineage.strategy_version_id,
                expected_artifact_digest=artifact["content_digest"],
                validator_identity=str(static_evidence.get("validator_identity")),
                validator_digest=str(static_evidence.get("validator_digest")),
            )
            lookahead = build_lookahead_receipt(
                lineage=lineage,
                artifact_digest=lookahead_row["artifact_digest"],
                analyzer_identity=str(lookahead_evidence.get("analyzer_identity")),
                analyzer_digest=str(lookahead_evidence.get("analyzer_digest")),
                evidence_digest=str(lookahead_evidence.get("source_evidence_digest")),
                status=lookahead_row["terminal_status"],
                has_bias=lookahead_evidence.get("has_bias"),
                observed_signal_count=int(lookahead_row["observed_signal_count"] or 0),
                failure_stage=lookahead_row["failure_stage"],
                failure_code=lookahead_row["reason_code"]
                if lookahead_row["terminal_status"] == "BLOCKED"
                else None,
                tool_return_code=lookahead_row["tool_return_code"],
                stdout_digest=lookahead_row["stdout_digest"],
                stderr_digest=lookahead_row["stderr_digest"],
                redacted_detail=lookahead_evidence.get("redacted_detail"),
                blocked_observed_trade_count=lookahead_row["observed_trade_count"],
                blocked_required_trade_count=lookahead_row["required_trade_count"],
            )
            declared = declare_validation_plan(
                connection,
                lineage=lineage,
                static_receipt=static_receipt,
                lookahead_receipt=lookahead,
                static_gate_receipt_id=command.static_gate_receipt_id,
                lookahead_gate_receipt_id=command.lookahead_gate_receipt_id,
                orchestrator_identity=command.orchestrator_identity,
            )
            ready = declared
            if declared.status in {"DECLARED", "READY"}:
                ready = mark_validation_plan_ready(
                    connection,
                    validation_plan_id=declared.validation_plan_id,
                    expected_plan_digest=declared.validation_plan_digest,
                    static_receipt=static_receipt,
                    lookahead_receipt=lookahead,
                    static_gate_receipt_id=command.static_gate_receipt_id,
                    lookahead_gate_receipt_id=command.lookahead_gate_receipt_id,
                    orchestrator_identity=command.orchestrator_identity,
                )
            return ValidationPlanReceiptDTO(
                validation_plan_id=ready.validation_plan_id,
                validation_plan_digest=ready.validation_plan_digest,
                status="READY",
                window_count=ready.window_count,
                required_window_count=ready.required_window_count,
                static_receipt_digest=static_row["receipt_digest"],
                lookahead_receipt_digest=lookahead_row["receipt_digest"],
                repeat_noop=declared.repeat_noop and ready.repeat_noop,
            )

        return run_research(validation_connection_factory, "validation", execute)

    @app.post(
        f"{API_PREFIX}/research/authorizations",
        response_model=ResearchAuthorizationReceiptDTO,
        status_code=201,
    )
    def authorize_research(
        command: ResearchAuthorizationCommandDTO,
    ) -> ResearchAuthorizationReceiptDTO:
        lineage = _research_lineage(command.lineage)
        run_read(
            lambda connection: _require_exact_ready_validation_plan(
                connection,
                lineage=lineage,
                validation_plan_id=command.validation_plan_id,
                validation_plan_digest=command.validation_plan_digest,
            )
        )

        def execute(connection: Connection) -> ResearchAuthorizationReceiptDTO:
            authorized_at = datetime.now(timezone.utc)
            result = authorize_research_execution(
                connection,
                lineage=lineage,
                attempt_id=command.attempt_id,
                validation_plan_id=command.validation_plan_id,
                validation_plan_digest=command.validation_plan_digest,
                actor_identity=command.actor_identity,
                purpose=command.purpose,
                authorized_at=authorized_at,
                expires_at=authorized_at + timedelta(seconds=command.ttl_seconds),
                environment_class="PRODUCTION_RESEARCH",
            )
            return ResearchAuthorizationReceiptDTO(
                authorization_id=result.authorization_id,
                attempt_id=result.attempt_id,
                validation_plan_id=result.validation_plan_id,
                validation_plan_digest=result.validation_plan_digest,
                actor_identity=result.actor_identity,
                purpose=result.purpose,
                request_digest=result.request_digest,
                receipt_digest=result.receipt_digest,
                authorized_at=result.authorized_at,
                expires_at=result.expires_at,
                one_shot=True,
                environment_class="PRODUCTION_RESEARCH",
            )

        return run_control(execute)

    @app.post(
        f"{API_PREFIX}/research/authorizations/{{authorization_id}}/consume",
        response_model=ResearchAuthorizationConsumptionReceiptDTO,
    )
    def consume_research_authorization(
        authorization_id: UUID,
        command: ResearchAuthorizationConsumeCommandDTO,
    ) -> ResearchAuthorizationConsumptionReceiptDTO:
        def execute(
            connection: Connection,
        ) -> ResearchAuthorizationConsumptionReceiptDTO:
            lineage = _research_lineage(command.lineage)
            result = consume_research_execution_authorization(
                connection,
                authorization_id=authorization_id,
                expected_lineage=lineage,
                validation_plan_id=command.validation_plan_id,
                validation_plan_digest=command.validation_plan_digest,
                attempt_id=command.attempt_id,
                actor_identity=command.actor_identity,
                consumed_at=datetime.now(timezone.utc),
            )
            if result.environment_class != "PRODUCTION_RESEARCH":
                raise CanonicalAPIBlocked(
                    "BLOCKED_AUTHORIZATION_ENVIRONMENT",
                    "production control surface received another environment",
                )
            return ResearchAuthorizationConsumptionReceiptDTO(
                authorization_id=result.authorization_id,
                consumption_id=result.consumption_id,
                attempt_id=result.attempt_id,
                lineage=ResearchLineageDTO(**result.lineage.__dict__),
                validation_plan_id=result.validation_plan_id,
                validation_plan_digest=result.validation_plan_digest,
                actor_identity=result.actor_identity,
                authorization_receipt_digest=result.authorization_receipt_digest,
                request_digest=result.request_digest,
                receipt_digest=result.receipt_digest,
                consumed_at=result.consumed_at,
                environment_class="PRODUCTION_RESEARCH",
            )

        return run_control(execute)

    @app.post(
        f"{API_PREFIX}/research/authorizations/{{authorization_id}}/revoke",
        response_model=ResearchAuthorizationRevokeReceiptDTO,
    )
    def revoke_research_authorization(
        authorization_id: UUID,
        command: ResearchAuthorizationRevokeCommandDTO,
    ) -> ResearchAuthorizationRevokeReceiptDTO:
        event_id = run_control(
            lambda connection: revoke_research_execution_authorization(
                connection,
                authorization_id=authorization_id,
                actor_identity=command.actor_identity,
                reason=command.reason,
                revoked_at=datetime.now(timezone.utc),
            )
        )
        return ResearchAuthorizationRevokeReceiptDTO(
            authorization_id=authorization_id,
            revocation_event_id=event_id,
        )

    @app.post(
        f"{API_PREFIX}/research/attempts",
        response_model=ResearchAttemptStartReceiptDTO,
        status_code=201,
    )
    def start_research_attempt(
        command: ResearchAttemptStartCommandDTO,
    ) -> ResearchAttemptStartReceiptDTO:
        consumption = _authorization_consumption(command.authorization_consumption)
        run_read(
            lambda connection: verify_persisted_research_authorization_consumption(
                connection, consumption=consumption
            )
        )

        def execute(connection: Connection) -> ResearchAttemptStartReceiptDTO:
            spec = build_ephemeral_launch_spec(
                connection,
                validation_plan_id=command.validation_plan_id,
                expected_plan_digest=command.validation_plan_digest,
                executor_identity=command.executor_identity,
                executor_image_digest=command.executor_image_digest,
            )
            running = start_consumed_research_attempt(
                connection,
                launch_spec=spec,
                authorization_consumption=consumption,
            )
            return ResearchAttemptStartReceiptDTO(
                validation_attempt_id=running.validation_attempt_id,
                validation_plan_id=spec.validation_plan_id,
                attempt_number=running.attempt_number,
                status="RUNNING",
                request_digest=running.request_digest,
            )

        return run_research(validation_connection_factory, "validation", execute)

    @app.post(
        f"{API_PREFIX}/research/scores",
        response_model=ResearchScoreReceiptDTO,
        status_code=201,
    )
    def score_research_target(
        command: ResearchScoreCommandDTO,
    ) -> ResearchScoreReceiptDTO:
        def execute(connection: Connection) -> ResearchScoreReceiptDTO:
            receipt = persist_scoring_receipt(
                connection,
                validation_plan_id=command.validation_plan_id,
                validation_attempt_id=command.validation_attempt_id,
                scorer_identity=command.scorer_identity,
            )
            return ResearchScoreReceiptDTO(
                **{
                    **receipt.__dict__,
                    "overall_score": str(receipt.overall_score),
                }
            )

        return run_research(scoring_connection_factory, "scoring", execute)

    @app.post(
        f"{API_PREFIX}/research/qualifications",
        response_model=ResearchQualificationReceiptDTO,
        status_code=201,
    )
    def qualify_research_target(
        command: ResearchQualificationCommandDTO,
    ) -> ResearchQualificationReceiptDTO:
        def execute(connection: Connection) -> ResearchQualificationReceiptDTO:
            receipt = persist_qualification_receipt(
                connection,
                validation_plan_id=command.validation_plan_id,
                validation_attempt_id=command.validation_attempt_id,
                qualifier_identity=command.qualifier_identity,
            )
            return ResearchQualificationReceiptDTO(**receipt.__dict__)

        return run_research(qualification_connection_factory, "qualification", execute)

    @app.get(
        f"{API_PREFIX}/research/validation-plans",
        response_model=ResearchPlanCatalogProjectionDTO,
    )
    def research_plan_catalog() -> ResearchPlanCatalogProjectionDTO:
        return run_read(_research_plan_catalog)

    @app.get(
        f"{API_PREFIX}/research/validation-plans/{{validation_plan_id}}",
        response_model=ResearchChainProjectionDTO,
    )
    def research_chain_status(
        validation_plan_id: UUID,
    ) -> ResearchChainProjectionDTO:
        projection = run_read(
            lambda connection: read_research_chain_projection(
                connection, validation_plan_id=validation_plan_id
            )
        )
        return ResearchChainProjectionDTO(**projection.__dict__)

    @app.get(
        f"{API_PREFIX}/research/validation-plans/{{validation_plan_id}}/results",
        response_model=ResearchResultsProjectionDTO,
    )
    def research_results(
        validation_plan_id: UUID,
    ) -> ResearchResultsProjectionDTO:
        return run_read(
            lambda connection: _research_results_projection(
                connection, validation_plan_id
            )
        )

    @app.get(f"{API_PREFIX}/market-data", response_model=MarketInventoryProjectionDTO)
    def market_inventory() -> MarketInventoryProjectionDTO:
        return run_read(_market_inventory)

    def build_market_plan(
        connection: Connection, command: FreshMarketPlanCommandDTO
    ) -> FreshMarketPlan:
        return plan_fresh_market_acquisition(
            connection,
            target_snapshot_id=command.target_snapshot_id,
            expected_target_snapshot_digest=command.target_snapshot_digest,
            window_snapshot_id=command.window_snapshot_id,
            expected_window_snapshot_digest=command.window_snapshot_digest,
            target_key=command.target_key,
        )

    def market_plan_dto(plan: FreshMarketPlan) -> FreshMarketPlanDTO:
        facts = fresh_market_plan_facts(plan)
        facts.pop("contract")
        return FreshMarketPlanDTO(
            **facts,
            plan_digest=fresh_market_plan_digest(plan),
        )

    @app.post(
        f"{API_PREFIX}/market-data/acquisitions/plan",
        response_model=FreshMarketPlanDTO,
    )
    def plan_market_acquisition(
        command: FreshMarketPlanCommandDTO,
    ) -> FreshMarketPlanDTO:
        return run_control(
            lambda connection: market_plan_dto(build_market_plan(connection, command))
        )

    @app.post(
        f"{API_PREFIX}/market-data/acquisitions/apply",
        response_model=FreshMarketReceiptDTO,
        status_code=201,
    )
    def apply_market_acquisition(
        command: FreshMarketApplyCommandDTO,
    ) -> FreshMarketReceiptDTO:
        if (
            market_artifact_root is None
            or market_downloader_factory is None
            or exchange_metadata_downloader_factory is None
        ):
            raise CanonicalAPIBlocked(
                "BLOCKED_MARKET_ACQUISITION_NOT_CONFIGURED",
                "production market artifact root/downloader is unavailable",
            )
        if not market_artifact_root.is_absolute() or market_artifact_root.is_symlink():
            raise CanonicalAPIBlocked(
                "BLOCKED_MARKET_ARTIFACT_ROOT", "artifact root is not a real directory"
            )
        try:
            root = market_artifact_root.resolve(strict=True)
        except OSError as exc:
            raise CanonicalAPIBlocked(
                "BLOCKED_MARKET_ARTIFACT_ROOT", "artifact root is unavailable"
            ) from exc
        if not root.is_dir() or root.is_symlink():
            raise CanonicalAPIBlocked(
                "BLOCKED_MARKET_ARTIFACT_ROOT", "artifact root is not a real directory"
            )

        def execute(connection: Connection) -> FreshMarketReceiptDTO:
            plan = build_market_plan(connection, command)
            digest = fresh_market_plan_digest(plan)
            if digest != command.expected_plan_digest:
                raise CanonicalAPIBlocked(
                    "BLOCKED_MARKET_PLAN_DIGEST_DRIFT",
                    "fresh-market plan differs from reviewed digest",
                )
            result = acquire_register_and_seal_fresh_market(
                connection,
                plan=plan,
                downloader=market_downloader_factory(),
                artifact_root=root,
                observed_at=datetime.now(timezone.utc),
                profile_key=command.profile_key,
                scope_key=command.scope_key,
                inspector_identity="canonical-v13-okx-public-inspector-v1",
                metadata_downloader=exchange_metadata_downloader_factory(),
            )
            return FreshMarketReceiptDTO(
                plan_digest=digest,
                market_profile_version_id=result.market_profile_version_id,
                artifact_id=result.artifact_id,
                artifact_locator=result.artifact_locator,
                artifact_digest=result.artifact_digest,
                receipt_id=result.receipt_id,
                market_snapshot_id=result.market_snapshot_id,
                market_snapshot_digest=result.market_snapshot_digest,
                artifact_file_replay=result.artifact_file_replay,
                database_replay=result.database_replay,
                exchange_metadata_artifact_id=result.exchange_metadata_artifact_id,
                exchange_metadata_receipt_id=result.exchange_metadata_receipt_id,
                exchange_metadata_locator=result.exchange_metadata_locator,
                exchange_metadata_digest=result.exchange_metadata_digest,
                exchange_metadata_receipt_digest=result.exchange_metadata_receipt_digest,
            )

        return run_control(execute)

    @app.get(
        f"{API_PREFIX}/market-data/snapshots/{{snapshot_id}}",
        response_model=MarketSnapshotProjectionDTO,
    )
    def market_snapshot(snapshot_id: UUID) -> MarketSnapshotProjectionDTO:
        return run_read(lambda connection: _market_snapshot(connection, snapshot_id))

    @app.get(f"{API_PREFIX}/readiness/research", response_model=ReadinessProjectionDTO)
    def research_readiness(
        scope_key: Optional[str] = Query(default=None, min_length=1, max_length=200),
        workflow_key: Optional[str] = Query(default=None, min_length=1, max_length=160),
    ) -> ReadinessProjectionDTO:
        return run_read(
            lambda connection: _research_readiness(
                connection, scope_key=scope_key, workflow_key=workflow_key
            )
        )

    @app.get(f"{API_PREFIX}/readiness/runtime", response_model=ReadinessProjectionDTO)
    def runtime_readiness() -> ReadinessProjectionDTO:
        return run_read(_runtime_readiness)

    @app.get(
        f"{API_PREFIX}/phase9/readiness",
        response_model=Phase9ReadinessProjectionDTO,
    )
    def phase9_readiness(
        qualification_decision_id: UUID,
        strategy_version_id: UUID,
        configuration_bundle_id: UUID,
        market_snapshot_id: UUID,
        stage: str = Query(default="QUALIFICATION_HANDOFF"),
    ) -> Phase9ReadinessProjectionDTO:
        if stage not in PHASE9_ACCEPTANCE_STAGES:
            raise CanonicalPhase9ReadinessBlocked(
                "BLOCKED_PHASE9_STAGE", f"unsupported stage {stage!r}"
            )

        def execute(connection: Connection) -> Phase9ReadinessProjectionDTO:
            decision = (
                connection.execute(
                    select(QUALIFICATION_DECISIONS_TABLE).where(
                        QUALIFICATION_DECISIONS_TABLE.c.id == qualification_decision_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if decision is None:
                raise CanonicalPhase9ReadinessBlocked(
                    "EXACT_QUALIFICATION_DECISION_NOT_FOUND",
                    str(qualification_decision_id),
                )
            if (
                decision["strategy_version_id"] != strategy_version_id
                or decision["configuration_bundle_id"] != configuration_bundle_id
                or decision["market_snapshot_id"] != market_snapshot_id
            ):
                raise CanonicalPhase9ReadinessBlocked(
                    "EXACT_QUALIFICATION_HANDOFF_ID_MISMATCH",
                    "explicit Phase 9 handoff IDs differ from the decision",
                )
            handoff = Phase9QualificationHandoff(
                qualification_decision_id=decision["id"],
                qualification_decision_digest=decision["decision_digest"],
                strategy_version_id=decision["strategy_version_id"],
                research_target_id=decision["research_target_id"],
                configuration_bundle_id=decision["configuration_bundle_id"],
                configuration_bundle_digest=decision["configuration_bundle_digest"],
                market_snapshot_id=decision["market_snapshot_id"],
                market_snapshot_digest=decision["market_snapshot_digest"],
                validation_plan_id=decision["validation_plan_id"],
                validation_plan_digest=decision["validation_plan_digest"],
            )
            receipt = inspect_phase9_readiness(
                connection, qualification_handoff=handoff, stage=stage
            )
            return Phase9ReadinessProjectionDTO(**asdict(receipt))

        return run_read(execute)

    @app.post(
        f"{API_PREFIX}/phase9/approvals",
        response_model=Phase9ApprovalReceiptDTO,
        status_code=201,
    )
    def phase9_approval(command: Phase9ApprovalCommandDTO) -> Phase9ApprovalReceiptDTO:
        def execute(connection: Connection) -> Phase9ApprovalReceiptDTO:
            return Phase9ApprovalReceiptDTO(
                **asdict(
                    approve_demo_deployment(
                        connection,
                        qualification_decision_id=command.qualification_decision_id,
                        actor_identity=command.actor_identity,
                        reason=command.reason,
                    )
                )
            )

        return run_phase9(
            approval_connection_factory, "canonical_approval_writer", execute
        )

    @app.post(
        f"{API_PREFIX}/phase9/deployments",
        response_model=Phase9DeploymentReceiptDTO,
        status_code=201,
    )
    def phase9_deployment(
        command: Phase9DeploymentCommandDTO,
    ) -> Phase9DeploymentReceiptDTO:
        def execute(connection: Connection) -> Phase9DeploymentReceiptDTO:
            return Phase9DeploymentReceiptDTO(
                **asdict(
                    create_demo_deployment(
                        connection,
                        deployment_approval_id=command.deployment_approval_id,
                    )
                )
            )

        return run_phase9(
            deployment_connection_factory, "canonical_deployment_writer", execute
        )

    @app.post(
        f"{API_PREFIX}/phase9/deployments/{{deployment_id}}/disable",
        response_model=Phase9DeploymentDisableReceiptDTO,
    )
    def phase9_disable_deployment(
        deployment_id: UUID,
        command: Phase9DeploymentDisableCommandDTO,
    ) -> Phase9DeploymentDisableReceiptDTO:
        def execute(connection: Connection) -> Phase9DeploymentDisableReceiptDTO:
            return Phase9DeploymentDisableReceiptDTO(
                **asdict(
                    disable_demo_deployment(
                        connection,
                        deployment_id=deployment_id,
                        superseded_by_qualification_decision_id=(
                            command.superseded_by_qualification_decision_id
                        ),
                        actor_identity=command.actor_identity,
                        reason=command.reason,
                    )
                )
            )

        return run_phase9(
            deployment_connection_factory, "canonical_deployment_writer", execute
        )

    @app.post(
        f"{API_PREFIX}/phase9/canary-risk-policies",
        response_model=Phase9CanaryRiskPolicyReceiptDTO,
        status_code=201,
    )
    def phase9_canary_risk_policy(
        command: Phase9CanaryRiskPolicyCommandDTO,
    ) -> Phase9CanaryRiskPolicyReceiptDTO:
        def execute(connection: Connection) -> Phase9CanaryRiskPolicyReceiptDTO:
            result = authorize_canary_risk_policy(
                connection,
                qualification_decision_id=command.qualification_decision_id,
                deployment_approval_id=command.deployment_approval_id,
                probe_receipt_id=command.probe_receipt_id,
                actor_identity=command.actor_identity,
                idempotency_key=command.idempotency_key,
                reason=command.reason,
            )
            return Phase9CanaryRiskPolicyReceiptDTO(
                policy_id=result.policy_id,
                request_digest=result.request_digest,
                policy_digest=result.policy_digest,
                receipt_digest=result.receipt_digest,
                max_notional=format(result.max_notional, "f"),
                effective_leverage=format(result.effective_leverage, "f"),
                accepted_at=result.accepted_at,
                expires_at=result.expires_at,
                repeat_noop=result.repeat_noop,
            )

        return run_phase9(
            approval_connection_factory, "canonical_approval_writer", execute
        )

    @app.post(
        f"{API_PREFIX}/phase9/canary-risk-policies/{{policy_id}}/terminate",
        response_model=Phase9CanaryRiskPolicyTerminationReceiptDTO,
    )
    def phase9_terminate_canary_risk_policy(
        policy_id: UUID,
        command: Phase9CanaryRiskPolicyTerminationCommandDTO,
    ) -> Phase9CanaryRiskPolicyTerminationReceiptDTO:
        def execute(
            connection: Connection,
        ) -> Phase9CanaryRiskPolicyTerminationReceiptDTO:
            return Phase9CanaryRiskPolicyTerminationReceiptDTO(
                **asdict(
                    terminate_canary_risk_policy(
                        connection,
                        policy_id=policy_id,
                        reconciliation_run_id=command.reconciliation_run_id,
                        actor_identity=command.actor_identity,
                    )
                )
            )

        return run_phase9(
            approval_connection_factory, "canonical_approval_writer", execute
        )

    @app.post(
        f"{API_PREFIX}/phase9/risk-budgets",
        response_model=Phase9RiskBudgetReceiptDTO,
        status_code=201,
    )
    def phase9_risk_budget(
        command: Phase9RiskBudgetCommandDTO,
    ) -> Phase9RiskBudgetReceiptDTO:
        def execute(connection: Connection) -> Phase9RiskBudgetReceiptDTO:
            return Phase9RiskBudgetReceiptDTO(
                **asdict(
                    authorize_demo_risk_budget(
                        connection,
                        deployment_approval_id=command.deployment_approval_id,
                        actor_identity=command.actor_identity,
                        reason=command.reason,
                        policy_source_receipt_digest=(
                            command.policy_source_receipt_digest
                        ),
                    )
                )
            )

        return run_phase9(
            approval_connection_factory, "canonical_approval_writer", execute
        )

    @app.post(
        f"{API_PREFIX}/phase9/intents",
        response_model=Phase9IntentReceiptDTO,
        status_code=201,
    )
    def phase9_intent(command: Phase9IntentCommandDTO) -> Phase9IntentReceiptDTO:
        def execute(connection: Connection) -> Phase9IntentReceiptDTO:
            intent_id = create_production_demo_intent(
                connection, signal_id=command.signal_id, intent_json=command.intent_json
            )
            return Phase9IntentReceiptDTO(trade_intent_id=intent_id)

        return run_phase9(risk_connection_factory, "canonical_risk_writer", execute)

    @app.post(
        f"{API_PREFIX}/phase9/shadow-risk-decisions",
        response_model=Phase9ShadowRiskDecisionReceiptDTO,
        status_code=201,
    )
    def phase9_shadow_risk_decision(
        command: Phase9ShadowRiskDecisionCommandDTO,
    ) -> Phase9ShadowRiskDecisionReceiptDTO:
        def execute(connection: Connection) -> Phase9ShadowRiskDecisionReceiptDTO:
            return Phase9ShadowRiskDecisionReceiptDTO(
                **asdict(
                    decide_signal_risk_shadow(
                        connection,
                        trade_intent_id=command.trade_intent_id,
                    )
                )
            )

        return run_phase9(risk_connection_factory, "canonical_risk_writer", execute)

    @app.post(
        f"{API_PREFIX}/phase9/risk-decisions",
        response_model=Phase9RiskDecisionReceiptDTO,
        status_code=201,
    )
    def phase9_risk_decision(
        command: Phase9RiskDecisionCommandDTO,
    ) -> Phase9RiskDecisionReceiptDTO:
        def execute(connection: Connection) -> Phase9RiskDecisionReceiptDTO:
            return Phase9RiskDecisionReceiptDTO(
                **asdict(
                    decide_central_demo_risk(
                        connection,
                        trade_intent_id=command.trade_intent_id,
                        risk_budget_authorization_id=command.risk_budget_authorization_id,
                    )
                )
            )

        return run_phase9(risk_connection_factory, "canonical_risk_writer", execute)

    @app.get(
        f"{API_PREFIX}/optimizations", response_model=OptimizationListProjectionDTO
    )
    def optimizations() -> OptimizationListProjectionDTO:
        def execute(connection: Connection) -> OptimizationListProjectionDTO:
            rows = (
                connection.execute(
                    select(OPTIMIZATION_RUNS_TABLE).order_by(
                        OPTIMIZATION_RUNS_TABLE.c.created_at,
                        OPTIMIZATION_RUNS_TABLE.c.id,
                    )
                )
                .mappings()
                .all()
            )
            items = [
                OptimizationProjectionDTO(
                    optimization_run_id=row["id"],
                    baseline_qualification_decision_id=row[
                        "baseline_qualification_decision_id"
                    ],
                    status=row["status"],
                    request_digest=row["request_digest"],
                    receipt_digest=row["receipt_digest"],
                    created_at=row["created_at"],
                    completed_at=row["completed_at"],
                )
                for row in rows
            ]
            return OptimizationListProjectionDTO(
                status="AVAILABLE" if items else "PENDING_FIRST_BACKTEST",
                items=items,
            )

        return run_read(execute)

    @app.post(
        f"{API_PREFIX}/optimizations",
        response_model=OptimizationRunReceiptDTO,
        status_code=201,
    )
    def create_optimization(command: OptimizationRunCommandDTO) -> OptimizationRunReceiptDTO:
        result = run_research(
            optimization_connection_factory,
            "canonical_optimization_writer",
            lambda connection: create_optimization_run(
                connection,
                baseline_qualification_decision_id=(
                    command.baseline_qualification_decision_id
                ),
                actor_identity=command.actor_identity,
                objective_json=command.objective_json,
            ),
        )
        return OptimizationRunReceiptDTO(**asdict(result))

    @app.post(
        f"{API_PREFIX}/optimizations/{{optimization_run_id}}/trials",
        response_model=OptimizationTrialReceiptDTO,
        status_code=201,
    )
    def record_optimization_trial(
        optimization_run_id: UUID, command: OptimizationTrialCommandDTO
    ) -> OptimizationTrialReceiptDTO:
        result = run_research(
            optimization_connection_factory,
            "canonical_optimization_writer",
            lambda connection: record_isolated_optimization_trial(
                connection,
                optimization_run_id=optimization_run_id,
                trial_number=command.trial_number,
                actor_identity=command.actor_identity,
                parameters_json=command.parameters_json,
                metrics_json=command.metrics_json,
            ),
        )
        return OptimizationTrialReceiptDTO(**asdict(result))

    @app.post(
        f"{API_PREFIX}/optimizations/{{optimization_run_id}}/complete",
        response_model=OptimizationCompletionReceiptDTO,
    )
    def complete_optimization(
        optimization_run_id: UUID, command: OptimizationCompleteCommandDTO
    ) -> OptimizationCompletionReceiptDTO:
        result = run_research(
            optimization_connection_factory,
            "canonical_optimization_writer",
            lambda connection: complete_optimization_run(
                connection,
                optimization_run_id=optimization_run_id,
                actor_identity=command.actor_identity,
                selected_trial_numbers=command.selected_trial_numbers,
                terminal_status=command.terminal_status,
            ),
        )
        payload = asdict(result)
        payload["selected_trial_numbers"] = list(result.selected_trial_numbers)
        return OptimizationCompletionReceiptDTO(**payload)

    @app.post(
        f"{API_PREFIX}/optimizations/trials/{{optimization_trial_id}}/submissions/"
        "{submitted_strategy_version_id}",
        response_model=OptimizationSubmissionLinkReceiptDTO,
    )
    def link_optimization_submission(
        optimization_trial_id: UUID, submitted_strategy_version_id: UUID
    ) -> OptimizationSubmissionLinkReceiptDTO:
        result = run_research(
            optimization_connection_factory,
            "canonical_optimization_writer",
            lambda connection: link_controlled_submission_version(
                connection,
                optimization_trial_id=optimization_trial_id,
                submitted_strategy_version_id=submitted_strategy_version_id,
            ),
        )
        return OptimizationSubmissionLinkReceiptDTO(**asdict(result))

    return app


__all__ = [
    "API_PREFIX",
    "CanonicalAPIBlocked",
    "CanonicalConnectionFactory",
    "create_canonical_v13_app",
]
