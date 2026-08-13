from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.strategy_platform_v13_contract import (
    V13_RESEARCH_SCOPE_KEY,
    V13_RESEARCH_SCOPE_TYPE,
    V13_RESEARCH_WORKFLOW_KIND,
)
from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.services.profile_bound_adapters import (
    diversity_profile,
    generation_profile,
    scoring_profile,
)
from app.services.runtime_configuration_bundle_reader import (
    ReadOnlyBundleConnection,
    RuntimeConfigurationBundleReader,
)
from app.services.strategy_platform_adapter_registry import (
    installed_adapter_manifest_digest,
)


_DATABASE_NAME = "freqtrade_ai_design_lab"
_SCHEMA_VERSION = "20260813_47"
_RUNTIME_ROLE = "freqtrade"
_PROFILE_SCHEMAS = {
    "generation-profile": "generation-profile-v2",
    "diversity-profile": "diversity-profile-v2",
    "quality-gate-profile": "quality-gate-profile-v2",
    "scoring-profile": "scoring-profile-v2",
    "research-profile": "research-profile-v2",
}


@dataclass(frozen=True)
class RuntimeResearchBundleBinding:
    bundle_id: int
    bundle_digest: str
    generation_profile_version_id: int
    diversity_profile_version_id: int
    quality_gate_profile_version_id: int
    scoring_profile_version_id: int
    research_profile_version_id: int
    candidates_per_target: int
    target_count: int
    candidate_count: int

    def sanitized_readiness(self) -> dict[str, Any]:
        return {
            "ready": True,
            "runtime_mode": "V13_NO_TRADE",
            "database": _DATABASE_NAME,
            "schema_version": _SCHEMA_VERSION,
            "runtime_role": _RUNTIME_ROLE,
            "workflow_kind": V13_RESEARCH_WORKFLOW_KIND,
            "scope_type": V13_RESEARCH_SCOPE_TYPE,
            "scope_key": V13_RESEARCH_SCOPE_KEY,
            "bundle_id": self.bundle_id,
            "bundle_digest": self.bundle_digest,
            "generation_profile_version_id": self.generation_profile_version_id,
            "diversity_profile_version_id": self.diversity_profile_version_id,
            "quality_gate_profile_version_id": self.quality_gate_profile_version_id,
            "scoring_profile_version_id": self.scoring_profile_version_id,
            "research_profile_version_id": self.research_profile_version_id,
            "candidates_per_target": self.candidates_per_target,
            "target_count": self.target_count,
            "candidate_count": self.candidate_count,
            "credential_attestation": "UNKNOWN_OUT_OF_SCOPE",
            "worker_execution": "DISABLED",
            "backtest_execution": "DISABLED",
            "signal_generation": "DISABLED",
            "order_submission": "DISABLED",
            "allow_real_funds": False,
        }


class RuntimeResearchBundleBindingService:
    """Bind one immutable owner-produced bundle to the least-privilege runtime."""

    def __init__(self, connection: ReadOnlyBundleConnection) -> None:
        self._connection = connection

    def read(self, bundle_id: int) -> RuntimeResearchBundleBinding:
        current_user = self._connection.execute(text("SELECT current_user")).scalar_one()
        if current_user != _RUNTIME_ROLE:
            _blocked(
                "V13_RUNTIME_ROLE_INVALID",
                "V1.3 runtime bundle consumption requires the least-privilege runtime role.",
            )
        bundle = RuntimeConfigurationBundleReader(
            self._connection,
            installed_adapter_manifest_digest=installed_adapter_manifest_digest(),
        ).read_validated(bundle_id)
        snapshot = bundle.snapshot
        if (
            snapshot.workflow_kind != V13_RESEARCH_WORKFLOW_KIND
            or snapshot.scope_type != V13_RESEARCH_SCOPE_TYPE
            or snapshot.scope_key != V13_RESEARCH_SCOPE_KEY
        ):
            _blocked(
                "V13_RESEARCH_BUNDLE_SCOPE_INVALID",
                "Runtime bundle must be the explicitly activated production research scope.",
            )

        versions = {
            type_key: bundle.require_single_version(type_key)
            for type_key in _PROFILE_SCHEMAS
        }
        for type_key, expected_schema in _PROFILE_SCHEMAS.items():
            if versions[type_key].schema_version != expected_schema:
                _blocked(
                    "V13_RESEARCH_PROFILE_SCHEMA_INVALID",
                    "Runtime bundle contains a non-v2 research profile.",
                    config_type=type_key,
                    schema_version=versions[type_key].schema_version,
                )
        research = versions["research-profile"]
        if snapshot.aggregate_profile_version_id != research.id:
            _blocked(
                "V13_RESEARCH_AGGREGATE_INVALID",
                "Frozen bundle aggregate must be the resolved v2 research profile.",
            )

        generation = generation_profile(bundle)
        diversity = diversity_profile(bundle)
        scoring = scoring_profile(bundle)
        quality = versions["quality-gate-profile"]
        research_payload = _mapping(research.payload_json, "research profile")
        scoring_payload = _mapping(
            versions["scoring-profile"].payload_json, "scoring profile"
        )
        quality_payload = _mapping(quality.payload_json, "quality profile")

        expected_bindings = {
            "generation_profile_version_id": generation.version_id,
            "diversity_profile_version_id": diversity.version_id,
            "quality_gate_profile_version_id": quality.id,
            "scoring_profile_version_id": scoring.version_id,
        }
        for key, expected in expected_bindings.items():
            if research_payload.get(key) != expected:
                _blocked(
                    "V13_RESEARCH_PROFILE_BINDING_INVALID",
                    "Research profile does not bind the exact resolved v2 profile graph.",
                    binding=key,
                )
        if diversity.generation_profile_version_id != generation.version_id:
            _blocked(
                "V13_DIVERSITY_GENERATION_BINDING_INVALID",
                "Diversity profile does not bind the resolved generation profile.",
            )
        if set(diversity.required_family_version_ids) != set(
            generation.family_version_ids
        ):
            _blocked(
                "V13_DIVERSITY_FAMILY_BINDING_INVALID",
                "Diversity and generation profiles do not bind the same families.",
            )
        if scoring_payload.get("quality_gate_profile_version_id") != quality.id:
            _blocked(
                "V13_SCORING_QUALITY_BINDING_INVALID",
                "Scoring profile does not bind the resolved quality profile.",
            )
        for key in ("quality_components", "elimination_rules", "warning_rules"):
            if scoring_payload.get(key) != quality_payload.get(key):
                _blocked(
                    "V13_SCORING_QUALITY_CONTRACT_MISMATCH",
                    "Scoring and quality profiles do not contain the same rule contract.",
                    contract_key=key,
                )
        for payload in (research_payload, versions["generation-profile"].payload_json):
            safety = _mapping(payload, "profile safety")
            if (
                safety.get("demo_only") is not True
                or safety.get("allow_real_funds") is not False
                or safety.get("single_writer_required") is not True
            ):
                _blocked(
                    "V13_RESEARCH_SAFETY_INVALID",
                    "V1.3 research profiles must remain Demo-only and single-writer.",
                )

        return RuntimeResearchBundleBinding(
            bundle_id=snapshot.snapshot_id,
            bundle_digest=snapshot.bundle_digest,
            generation_profile_version_id=generation.version_id,
            diversity_profile_version_id=diversity.version_id,
            quality_gate_profile_version_id=quality.id,
            scoring_profile_version_id=scoring.version_id,
            research_profile_version_id=research.id,
            candidates_per_target=generation.candidates_per_target,
            target_count=generation.target_count,
            candidate_count=generation.candidate_count,
        )


def read_runtime_research_bundle_binding(
    engine: Engine,
    bundle_id: int,
) -> RuntimeResearchBundleBinding:
    """Read through one explicit read-only transaction; never commit DB state."""

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            binding = RuntimeResearchBundleBindingService(connection).read(bundle_id)
            transaction.rollback()
            return binding
        except BaseException:
            if transaction.is_active:
                transaction.rollback()
            raise


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _blocked(
            "V13_PROFILE_PAYLOAD_INVALID",
            f"Resolved {label} payload must be an object.",
        )
    return value


def _blocked(code: str, message: str, **context: Any) -> None:
    raise StrategyPlatformReadError(
        code,
        message,
        status_code=503,
        context=context,
    )
