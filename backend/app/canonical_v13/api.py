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
    MarketInventoryProjectionDTO,
    MarketSnapshotMemberProjectionDTO,
    MarketSnapshotProjectionDTO,
    MarketSnapshotSummaryDTO,
    OptimizationListProjectionDTO,
    OptimizationProjectionDTO,
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
)
from app.canonical_v13.deployment_approval import deployment_approval_digest
from app.canonical_v13.deployment_control import deployment_capability_digest
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
from app.canonical_v13.research_evaluation import (
    CanonicalEvaluationBlocked,
    gate_optimization,
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
from app.canonical_v13.research_validation import (
    CanonicalResearchValidationBlocked,
    ResearchLineage,
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
    RESEARCH_TARGETS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    STRATEGIES_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_SUBMISSIONS_TABLE,
    STRATEGY_VERSIONS_TABLE,
    VALIDATION_PLANS_TABLE,
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
    CanonicalResearchAuthorizationBlocked,
    CanonicalResearchExecutionBlocked,
    CanonicalResearchOrchestrationBlocked,
    CanonicalResearchValidationBlocked,
    CanonicalEvaluationBlocked,
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
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


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
    plan = connection.execute(
        select(VALIDATION_PLANS_TABLE).where(
            VALIDATION_PLANS_TABLE.c.id == validation_plan_id
        )
    ).mappings().one_or_none()
    if (
        plan is None
        or plan["status"] != "READY"
        or plan["validation_plan_digest"] != validation_plan_digest
        or plan["strategy_version_id"] != lineage.strategy_version_id
        or plan["research_target_id"] != lineage.research_target_id
        or plan["configuration_bundle_id"] != lineage.configuration_bundle_id
        or plan["configuration_bundle_digest"]
        != lineage.configuration_bundle_digest
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
            QUALIFICATION_DECISIONS_TABLE.c.strategy_version_id
            == strategy_version_id
        )
        .order_by(QUALIFICATION_DECISIONS_TABLE.c.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return str(status) if status is not None else "NOT_EVALUATED"


def _strategy_projection(
    connection: Connection, strategy: dict[str, Any]
) -> StrategyProjectionDTO:
    submission = connection.execute(
        select(STRATEGY_SUBMISSIONS_TABLE).where(
            STRATEGY_SUBMISSIONS_TABLE.c.id == strategy["source_submission_id"]
        )
    ).mappings().one()
    version = connection.execute(
        select(STRATEGY_VERSIONS_TABLE)
        .where(STRATEGY_VERSIONS_TABLE.c.strategy_id == strategy["id"])
        .order_by(STRATEGY_VERSIONS_TABLE.c.version_number.desc())
        .limit(1)
    ).mappings().one_or_none()
    if version is None:
        raise CanonicalAPIBlocked(
            "BLOCKED_STRATEGY_VERSION_MISSING",
            "canonical strategy has no current version",
        )
    artifact = connection.execute(
        select(STRATEGY_ARTIFACTS_TABLE).where(
            STRATEGY_ARTIFACTS_TABLE.c.id == version["artifact_id"]
        )
    ).mappings().one_or_none()
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
    profiles = connection.execute(
        select(CONFIGURATION_PROFILES_TABLE).order_by(
            CONFIGURATION_PROFILES_TABLE.c.configuration_kind,
            CONFIGURATION_PROFILES_TABLE.c.profile_key,
        )
    ).mappings().all()
    items: list[ConfigurationProfileProjectionDTO] = []
    for profile in profiles:
        version_rows = connection.execute(
            select(CONFIGURATION_VERSIONS_TABLE)
            .where(CONFIGURATION_VERSIONS_TABLE.c.profile_id == profile["id"])
            .order_by(CONFIGURATION_VERSIONS_TABLE.c.version_number)
        ).mappings().all()
        versions: list[ConfigurationVersionProjectionDTO] = []
        for version in version_rows:
            snapshot = connection.execute(
                select(CONFIGURATION_SNAPSHOTS_TABLE).where(
                    CONFIGURATION_SNAPSHOTS_TABLE.c.configuration_version_id
                    == version["id"]
                )
            ).mappings().one_or_none()
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
    rows = connection.execute(
        select(MARKET_SNAPSHOTS_TABLE).order_by(
            MARKET_SNAPSHOTS_TABLE.c.created_at,
            MARKET_SNAPSHOTS_TABLE.c.id,
        )
    ).mappings().all()
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
                        MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_snapshot_id
                        == row["id"]
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
        snapshots=snapshots,
    )


def _market_snapshot(
    connection: Connection, snapshot_id: UUID
) -> MarketSnapshotProjectionDTO:
    snapshot = connection.execute(
        select(MARKET_SNAPSHOTS_TABLE).where(
            MARKET_SNAPSHOTS_TABLE.c.id == snapshot_id
        )
    ).mappings().one_or_none()
    if snapshot is None:
        raise CanonicalAPIBlocked(
            "BLOCKED_MARKET_SNAPSHOT_NOT_FOUND", "canonical market snapshot is absent"
        )
    member_rows = connection.execute(
        select(MARKET_SNAPSHOT_MEMBERS_TABLE)
        .where(MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_snapshot_id == snapshot_id)
        .order_by(MARKET_SNAPSHOT_MEMBERS_TABLE.c.research_target_id)
    ).mappings().all()
    reasons: list[str] = []
    members: list[MarketSnapshotMemberProjectionDTO] = []
    if not member_rows:
        reasons.append("MARKET_SNAPSHOT_EMPTY")
    for member in member_rows:
        artifact = connection.execute(
            select(MARKET_ARTIFACTS_TABLE).where(
                MARKET_ARTIFACTS_TABLE.c.id == member["market_artifact_id"]
            )
        ).mappings().one_or_none()
        receipt = connection.execute(
            select(MARKET_RECEIPTS_TABLE).where(
                MARKET_RECEIPTS_TABLE.c.id == member["market_receipt_id"]
            )
        ).mappings().one_or_none()
        target = connection.execute(
            select(RESEARCH_TARGETS_TABLE).where(
                RESEARCH_TARGETS_TABLE.c.id == member["research_target_id"]
            )
        ).mappings().one_or_none()
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
    bundle = connection.execute(
        select(CONFIGURATION_BUNDLES_TABLE).where(
            CONFIGURATION_BUNDLES_TABLE.c.id
            == activation["configuration_bundle_id"]
        )
    ).mappings().one_or_none()
    if bundle is None or bundle["bundle_digest"] != activation["bundle_digest"]:
        return ReadinessProjectionDTO(
            status="BLOCKED",
            reason_codes=["ACTIVE_BUNDLE_LINEAGE_DRIFT"],
            scope_key=activation["scope_key"],
            workflow_key=activation["workflow_key"],
        )
    member_rows = connection.execute(
        select(CONFIGURATION_BUNDLE_MEMBERS_TABLE).where(
            CONFIGURATION_BUNDLE_MEMBERS_TABLE.c.configuration_bundle_id
            == bundle["id"]
        )
    ).mappings().all()
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
    return ReadinessProjectionDTO(
        status="BLOCKED" if unique_reasons else "PENDING_FIRST_BACKTEST",
        reason_codes=(
            unique_reasons if unique_reasons else ["PENDING_FIRST_BACKTEST"]
        ),
        scope_key=activation["scope_key"],
        workflow_key=activation["workflow_key"],
        configuration_bundle_id=bundle["id"],
        bundle_digest=bundle["bundle_digest"],
        market_snapshot_id=bundle["market_snapshot_id"],
        target_count=preview.target_count,
        total_candidate_count=preview.total_candidate_count,
    )


def _runtime_readiness(connection: Connection) -> ReadinessProjectionDTO:
    deployments = connection.execute(
        select(DEPLOYMENTS_TABLE)
        .where(DEPLOYMENTS_TABLE.c.status == "ACTIVE")
        .order_by(DEPLOYMENTS_TABLE.c.created_at.desc())
    ).mappings().all()
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
    approval = connection.execute(
        select(DEPLOYMENT_APPROVALS_TABLE).where(
            DEPLOYMENT_APPROVALS_TABLE.c.id
            == deployment["deployment_approval_id"]
        )
    ).mappings().one_or_none()
    qualification = (
        connection.execute(
            select(QUALIFICATION_DECISIONS_TABLE).where(
                QUALIFICATION_DECISIONS_TABLE.c.id
                == approval["qualification_decision_id"]
            )
        ).mappings().one_or_none()
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
    bundle = connection.execute(
        select(CONFIGURATION_BUNDLES_TABLE).where(
            CONFIGURATION_BUNDLES_TABLE.c.id
            == deployment["configuration_bundle_id"]
        )
    ).mappings().one_or_none()
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
    runtimes = connection.execute(
        select(RUNTIME_INSTANCES_TABLE).where(
            RUNTIME_INSTANCES_TABLE.c.deployment_id == deployment["id"],
            RUNTIME_INSTANCES_TABLE.c.status == "HEALTHY",
        )
    ).mappings().all()
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
                    approval_id=approval["id"] if approval else deployment["deployment_approval_id"],
                    qualification_decision_id=(
                        qualification["id"] if qualification else UUID(int=0)
                    ),
                    strategy_version_id=deployment["strategy_version_id"],
                    configuration_bundle_id=deployment["configuration_bundle_id"],
                    configuration_bundle_digest=deployment["configuration_bundle_digest"],
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
                    research_executor_capability=runtime["research_executor_capability"],
                    order_writer_capability=runtime["order_writer_capability"],
                )
            )
        except CanonicalRuntimeContractBlocked:
            reasons.append("RUNTIME_LAUNCH_CAPABILITY_DRIFT")
            expected_launch_digest = None
        if expected_launch_digest != runtime["launch_spec_digest"]:
            reasons.append("RUNTIME_LAUNCH_SPEC_DIGEST_DRIFT")
        receipt = connection.execute(
            select(RUNTIME_RECEIPTS_TABLE)
            .where(RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime["id"])
            .order_by(RUNTIME_RECEIPTS_TABLE.c.observed_at.desc())
            .limit(1)
        ).mappings().one_or_none()
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
    market_artifact_root: Path | None = None,
    market_downloader_factory: Callable[[], MarketDownloaderPort] | None = None,
    exchange_metadata_downloader_factory: Callable[
        [], OkxPublicOfflineExchangeMetadataDownloader
    ] | None = None,
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

    async def unexpected_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
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

    @app.get(
        f"{API_PREFIX}/strategies", response_model=StrategyCatalogProjectionDTO
    )
    def list_strategies(
        limit: int = Query(default=100, ge=1, le=200),
    ) -> StrategyCatalogProjectionDTO:
        def execute(connection: Connection) -> StrategyCatalogProjectionDTO:
            rows = connection.execute(
                select(STRATEGIES_TABLE)
                .order_by(STRATEGIES_TABLE.c.created_at, STRATEGIES_TABLE.c.id)
                .limit(limit)
            ).mappings().all()
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
            row = connection.execute(
                select(STRATEGIES_TABLE).where(STRATEGIES_TABLE.c.id == strategy_id)
            ).mappings().one_or_none()
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
        f"{API_PREFIX}/research/validation-plans",
        response_model=ValidationPlanReceiptDTO,
        status_code=201,
    )
    def create_validation_plan(
        command: ValidationPlanCommandDTO,
    ) -> ValidationPlanReceiptDTO:
        def execute(connection: Connection) -> ValidationPlanReceiptDTO:
            lineage = _research_lineage(command.lineage)
            artifact = connection.execute(
                select(STRATEGY_ARTIFACTS_TABLE)
                .select_from(
                    STRATEGY_VERSIONS_TABLE.join(
                        STRATEGY_ARTIFACTS_TABLE,
                        STRATEGY_ARTIFACTS_TABLE.c.id
                        == STRATEGY_VERSIONS_TABLE.c.artifact_id,
                    )
                )
                .where(STRATEGY_VERSIONS_TABLE.c.id == lineage.strategy_version_id)
            ).mappings().one_or_none()
            if artifact is None:
                raise CanonicalAPIBlocked(
                    "BLOCKED_STRATEGY_ARTIFACT_MISSING",
                    "validation plan requires one canonical strategy artifact",
                )
            static_receipt = validate_static_source(
                artifact["normalized_content"],
                strategy_version_id=lineage.strategy_version_id,
                expected_artifact_digest=artifact["content_digest"],
                validator_identity=command.static_validator_identity,
                validator_digest=command.static_validator_digest,
            )
            supplied = command.lookahead_receipt
            lookahead = build_lookahead_receipt(
                lineage=lineage,
                artifact_digest=supplied.artifact_digest,
                analyzer_identity=supplied.analyzer_identity,
                analyzer_digest=supplied.analyzer_digest,
                evidence_digest=supplied.evidence_digest,
                status=supplied.status,
                has_bias=supplied.has_bias,
                observed_signal_count=supplied.observed_signal_count,
                blocked_reason_code=supplied.blocked_reason_code,
                blocked_observed_trade_count=supplied.blocked_observed_trade_count,
                blocked_required_trade_count=supplied.blocked_required_trade_count,
            )
            if (
                lookahead.request_digest != supplied.request_digest
                or lookahead.receipt_digest != supplied.receipt_digest
            ):
                raise CanonicalResearchValidationBlocked(
                    "BLOCKED_LOOKAHEAD_RECEIPT_DIGEST_DRIFT",
                    "submitted lookahead receipt does not recompute",
                )
            declared = declare_validation_plan(
                connection,
                lineage=lineage,
                static_receipt=static_receipt,
                lookahead_receipt=lookahead,
                orchestrator_identity=command.orchestrator_identity,
            )
            ready = mark_validation_plan_ready(
                connection,
                validation_plan_id=declared.validation_plan_id,
                expected_plan_digest=declared.validation_plan_digest,
                static_receipt=static_receipt,
                lookahead_receipt=lookahead,
                orchestrator_identity=command.orchestrator_identity,
            )
            return ValidationPlanReceiptDTO(
                validation_plan_id=ready.validation_plan_id,
                validation_plan_digest=ready.validation_plan_digest,
                status="READY",
                window_count=ready.window_count,
                required_window_count=ready.required_window_count,
                static_receipt_digest=static_receipt.receipt_digest,
                lookahead_receipt_digest=lookahead.receipt_digest,
                repeat_noop=declared.repeat_noop and ready.repeat_noop,
            )

        return run_research(
            validation_connection_factory, "validation", execute
        )

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
        consumption = _authorization_consumption(
            command.authorization_consumption
        )
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

        return run_research(
            validation_connection_factory, "validation", execute
        )

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

        return run_research(
            qualification_connection_factory, "qualification", execute
        )

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
        f"{API_PREFIX}/market-data", response_model=MarketInventoryProjectionDTO
    )
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

    @app.get(
        f"{API_PREFIX}/readiness/research", response_model=ReadinessProjectionDTO
    )
    def research_readiness(
        scope_key: Optional[str] = Query(default=None, min_length=1, max_length=200),
        workflow_key: Optional[str] = Query(
            default=None, min_length=1, max_length=160
        ),
    ) -> ReadinessProjectionDTO:
        return run_read(
            lambda connection: _research_readiness(
                connection, scope_key=scope_key, workflow_key=workflow_key
            )
        )

    @app.get(
        f"{API_PREFIX}/readiness/runtime", response_model=ReadinessProjectionDTO
    )
    def runtime_readiness() -> ReadinessProjectionDTO:
        return run_read(_runtime_readiness)

    @app.get(
        f"{API_PREFIX}/optimizations", response_model=OptimizationListProjectionDTO
    )
    def optimizations() -> OptimizationListProjectionDTO:
        def execute(connection: Connection) -> OptimizationListProjectionDTO:
            rows = connection.execute(
                select(OPTIMIZATION_RUNS_TABLE).order_by(
                    OPTIMIZATION_RUNS_TABLE.c.created_at,
                    OPTIMIZATION_RUNS_TABLE.c.id,
                )
            ).mappings().all()
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

    return app


__all__ = [
    "API_PREFIX",
    "CanonicalAPIBlocked",
    "CanonicalConnectionFactory",
    "create_canonical_v13_app",
]
