"""Deterministic owner control-plane plan for the V1.3 research bundle.

This module deliberately performs no database I/O.  It defines the complete,
reviewable activation input and a narrow port that an owner-only transaction
must implement later.  Runtime code consumes only the resulting immutable
bundle through :class:`RuntimeConfigurationBundleReader`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.services.strategy_platform_adapter_registry import (
    INSTALLED_ADAPTER_MANIFEST,
    InstalledAdapter,
    installed_adapter_manifest_digest,
)


ACTIVATION_SCOPE_TYPE = "WORKFLOW"
ACTIVATION_SCOPE_KEY = "production-research-v13"
ACTIVATION_WORKFLOW_KIND = "RESEARCH"
ACTIVATION_CONTRACT = "strategy-platform-v13-owner-activation-v1"


@dataclass(frozen=True)
class ExistingResearchBindings:
    provider_model_config_version_id: int
    research_target_config_set_id: int
    validation_window_config_set_id: int
    market_data_policy_version_id: int
    evidence_freshness_profile_version_id: int
    scheduler_profile_version_id: int
    worker_execution_profile_version_id: int
    strategy_family_version_ids: tuple[int, ...]
    metric_version_ids: Mapping[str, int]


@dataclass(frozen=True)
class ConfigurationPlan:
    plan_key: str
    type_key: str
    schema_version: str
    payload: Mapping[str, Any]
    dependencies: tuple[tuple[str, int | str], ...]
    specialized_table: str
    specialized_rows: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class OwnerResearchActivationPlan:
    contract: str
    scope_type: str
    scope_key: str
    workflow_kind: str
    adapters: tuple[InstalledAdapter, ...]
    configurations: tuple[ConfigurationPlan, ...]
    installed_adapter_manifest_digest: str
    request_id: str
    plan_digest: str


@dataclass(frozen=True)
class OwnerActivationResult:
    version_ids: Mapping[str, int]
    bundle_id: int
    bundle_digest: str
    repeat_noop: bool


class OwnerActivationPort(Protocol):
    """Owner-only persistence boundary; never implemented by runtime code."""

    def register_adapter(self, adapter: InstalledAdapter) -> None: ...

    def create_draft(
        self,
        configuration: ConfigurationPlan,
        *,
        resolved_payload: Mapping[str, Any],
        resolved_dependencies: Sequence[tuple[str, int]],
        resolved_specialized_rows: Sequence[Mapping[str, Any]],
        request_id: str,
    ) -> int: ...

    def validate_version(
        self, *, type_key: str, version_id: int, request_id: str
    ) -> None: ...

    def activate_version(
        self,
        *,
        type_key: str,
        version_id: int,
        scope_type: str,
        scope_key: str,
        request_id: str,
    ) -> None: ...

    def materialize_bundle(
        self,
        *,
        workflow_kind: str,
        scope_type: str,
        scope_key: str,
        aggregate_version_id: int,
        installed_adapter_manifest_digest: str,
        request_id: str,
    ) -> tuple[int, str, bool]: ...


def build_owner_research_activation_plan(
    bindings: ExistingResearchBindings,
    *,
    candidates_per_target: int,
    target_count: int,
    candidate_count: int,
) -> OwnerResearchActivationPlan:
    """Build the exact v2 activation plan without defaults or side effects."""

    _positive(candidates_per_target, "candidates_per_target")
    _positive(target_count, "target_count")
    _positive(candidate_count, "candidate_count")
    if candidate_count != candidates_per_target * target_count:
        _blocked(
            "GENERATION_PROFILE_COUNT_MISMATCH",
            "Explicit candidate count must equal candidates per target times targets.",
        )
    family_ids = _positive_ids(
        bindings.strategy_family_version_ids, "strategy_family_version_ids"
    )
    required_metrics = {
        "profit_score",
        "risk_score",
        "stability_score",
        "quality_score",
        "required_windows_score",
        "static_quality_score",
        "net_profit",
        "max_drawdown",
        "total_trades",
        "win_rate",
        "quality_error_count",
        "quality_warning_count",
        "all_metrics_missing",
        "validation_error",
    }
    metric_ids = {
        key: _positive(value, f"metric_version_ids.{key}")
        for key, value in bindings.metric_version_ids.items()
    }
    missing = sorted(required_metrics - set(metric_ids))
    extra = sorted(set(metric_ids) - required_metrics)
    if missing or extra:
        _blocked(
            "ACTIVATION_METRIC_SET_INVALID",
            "The profile-bound score contract requires one exact metric set.",
            missing=missing,
            extra=extra,
        )
    for field in (
        "provider_model_config_version_id",
        "research_target_config_set_id",
        "validation_window_config_set_id",
        "market_data_policy_version_id",
        "evidence_freshness_profile_version_id",
        "scheduler_profile_version_id",
        "worker_execution_profile_version_id",
    ):
        _positive(getattr(bindings, field), field)

    adapters_by_key = {item.adapter_key: item for item in INSTALLED_ADAPTER_MANIFEST}
    adapter_keys = ("diversity-threshold-v2", "profile-bound-score-v2")
    if any(key not in adapters_by_key for key in adapter_keys):
        _blocked(
            "ACTIVATION_ADAPTER_NOT_INSTALLED",
            "The v2 profile-bound adapters must be present in the installed manifest.",
        )
    adapters = tuple(adapters_by_key[key] for key in adapter_keys)

    quality_components = _quality_components()
    elimination_rules = _elimination_rules()
    warning_rules = _warning_rules()
    generation = ConfigurationPlan(
        plan_key="generation",
        type_key="generation-profile",
        schema_version="generation-profile-v2",
        payload={
            "candidates_per_target": candidates_per_target,
            "total_target_count": target_count,
            "total_candidate_count": candidate_count,
            "strategy_family_version_ids": list(family_ids),
            "provider_model_config_version_id": bindings.provider_model_config_version_id,
            "demo_only": True,
            "allow_real_funds": False,
            "single_writer_required": True,
        },
        dependencies=(
            ("provider_model", bindings.provider_model_config_version_id),
            *tuple((f"strategy_family:{index}", value) for index, value in enumerate(family_ids)),
        ),
        specialized_table="generation_profile_versions",
        specialized_rows=({
            "provider_model_config_version_id": bindings.provider_model_config_version_id,
            "candidates_per_target": candidates_per_target,
            "structure_slot_count": candidates_per_target,
            "model_selection_policy": {"mode": "explicit_version"},
            "generation_limits": {
                "total_targets": target_count,
                "total_candidates": candidate_count,
            },
            "blueprint_requirements": {"contract": "canonical-blueprint-v2"},
        },),
    )
    diversity = ConfigurationPlan(
        plan_key="diversity",
        type_key="diversity-profile",
        schema_version="diversity-profile-v2",
        payload={
            "evaluation_adapter_key": "diversity-threshold-v2",
            "generation_profile_version_id": "$generation",
            "required_family_version_ids": list(family_ids),
            "thresholds": {
                "max_signal_similarity": 0.90,
                "max_abs_pnl_correlation": 0.85,
            },
        },
        dependencies=(
            ("generation", "$generation"),
            *tuple((f"strategy_family:{index}", value) for index, value in enumerate(family_ids)),
        ),
        specialized_table="diversity_profile_versions",
        specialized_rows=({
            "evaluation_adapter_key": "diversity-threshold-v2",
            "evaluation_scope": "per_target_batch",
            "parameters": {
                "generation_profile_version_id": "$generation",
                "required_family_version_ids": list(family_ids),
            },
        },),
    )
    quality = ConfigurationPlan(
        plan_key="quality",
        type_key="quality-gate-profile",
        schema_version="quality-gate-profile-v2",
        payload={
            "profile_key": "profile-bound-score-v2-quality",
            "quality_components": quality_components,
            "elimination_rules": elimination_rules,
            "warning_rules": warning_rules,
        },
        dependencies=tuple(
            (f"metric:{key}", metric_ids[key]) for key in sorted(metric_ids)
        ),
        specialized_table="quality_gate_profile_versions",
        specialized_rows=({
            "profile_key": "profile-bound-score-v2-quality",
            "rules": [*elimination_rules, *warning_rules],
        },),
    )
    scoring = ConfigurationPlan(
        plan_key="scoring",
        type_key="scoring-profile",
        schema_version="scoring-profile-v2",
        payload={
            "scoring_adapter_key": "profile-bound-score-v2",
            "component_weights": {
                "profit_score": 0.35,
                "risk_score": 0.25,
                "stability_score": 0.15,
                "quality_score": 0.25,
            },
            "normalization_rules": {
                "profit_score": {"transform": "linear", "slope": 500, "intercept": 50},
                "risk_score": {"transform": "absolute-linear", "slope": -500, "intercept": 100},
                "stability_score": {"transform": "linear", "slope": 100, "intercept": 0},
                "quality_score": {"transform": "linear", "slope": 1, "intercept": 0},
            },
            "quality_components": quality_components,
            "elimination_rules": elimination_rules,
            "warning_rules": warning_rules,
            "quality_gate_profile_version_id": "$quality",
        },
        dependencies=(
            ("quality", "$quality"),
            *tuple((f"metric:{key}", metric_ids[key]) for key in sorted(metric_ids)),
        ),
        specialized_table="scoring_profile_versions",
        specialized_rows=({
            "scoring_adapter_key": "profile-bound-score-v2",
            "algorithm_version": "profile-bound-score-v2",
            "aggregation_method": "weighted_sum",
            "primary_window_selector": {"mode": "profile_required_windows"},
            "score_bounds": {"minimum": 0, "maximum": 100},
        },),
    )
    research = ConfigurationPlan(
        plan_key="research",
        type_key="research-profile",
        schema_version="research-profile-v2",
        payload={
            "research_target_config_set_id": bindings.research_target_config_set_id,
            "validation_window_config_set_id": bindings.validation_window_config_set_id,
            "quality_gate_profile_version_id": "$quality",
            "scoring_profile_version_id": "$scoring",
            "diversity_profile_version_id": "$diversity",
            "generation_profile_version_id": "$generation",
            "provider_model_config_version_id": bindings.provider_model_config_version_id,
            "market_data_policy_version_id": bindings.market_data_policy_version_id,
            "evidence_freshness_profile_version_id": bindings.evidence_freshness_profile_version_id,
            "scheduler_profile_version_id": bindings.scheduler_profile_version_id,
            "worker_execution_profile_version_id": bindings.worker_execution_profile_version_id,
            "demo_only": True,
            "allow_real_funds": False,
            "single_writer_required": True,
        },
        dependencies=(
            ("generation", "$generation"),
            ("diversity", "$diversity"),
            ("quality", "$quality"),
            ("scoring", "$scoring"),
            ("provider", bindings.provider_model_config_version_id),
            ("targets", bindings.research_target_config_set_id),
            ("windows", bindings.validation_window_config_set_id),
            ("market_data", bindings.market_data_policy_version_id),
            ("freshness", bindings.evidence_freshness_profile_version_id),
            ("scheduler", bindings.scheduler_profile_version_id),
            ("worker", bindings.worker_execution_profile_version_id),
        ),
        specialized_table="research_profile_versions",
        specialized_rows=({
            "research_target_config_set_id": bindings.research_target_config_set_id,
            "validation_window_config_set_id": bindings.validation_window_config_set_id,
            "quality_gate_profile_version_id": "$quality",
            "scoring_profile_version_id": "$scoring",
            "diversity_profile_version_id": "$diversity",
            "generation_profile_version_id": "$generation",
            "provider_model_config_version_id": bindings.provider_model_config_version_id,
            "market_data_policy_version_id": bindings.market_data_policy_version_id,
            "evidence_freshness_profile_version_id": bindings.evidence_freshness_profile_version_id,
            "scheduler_profile_version_id": bindings.scheduler_profile_version_id,
            "worker_execution_profile_version_id": bindings.worker_execution_profile_version_id,
        },),
    )
    configurations = (generation, diversity, quality, scoring, research)
    unsigned = {
        "contract": ACTIVATION_CONTRACT,
        "scope_type": ACTIVATION_SCOPE_TYPE,
        "scope_key": ACTIVATION_SCOPE_KEY,
        "workflow_kind": ACTIVATION_WORKFLOW_KIND,
        "adapters": [asdict(item) for item in adapters],
        "configurations": [asdict(item) for item in configurations],
        "installed_adapter_manifest_digest": installed_adapter_manifest_digest(),
    }
    plan_digest = _digest(unsigned)
    return OwnerResearchActivationPlan(
        contract=ACTIVATION_CONTRACT,
        scope_type=ACTIVATION_SCOPE_TYPE,
        scope_key=ACTIVATION_SCOPE_KEY,
        workflow_kind=ACTIVATION_WORKFLOW_KIND,
        adapters=adapters,
        configurations=configurations,
        installed_adapter_manifest_digest=installed_adapter_manifest_digest(),
        request_id=f"issue-707:{plan_digest}",
        plan_digest=plan_digest,
    )


def execute_owner_research_activation(
    plan: OwnerResearchActivationPlan,
    port: OwnerActivationPort,
) -> OwnerActivationResult:
    """Execute the reviewed plan through an explicitly owner-only port."""

    if plan.contract != ACTIVATION_CONTRACT or plan.plan_digest != _plan_digest(plan):
        _blocked("ACTIVATION_PLAN_DIGEST_INVALID", "Activation plan identity drifted.")
    for adapter in plan.adapters:
        port.register_adapter(adapter)
    version_ids: dict[str, int] = {}
    for configuration in plan.configurations:
        resolved_payload = _resolve_refs(configuration.payload, version_ids)
        dependencies = tuple(
            (relation, _resolve_id(reference, version_ids))
            for relation, reference in configuration.dependencies
        )
        specialized = tuple(
            _resolve_refs(row, version_ids) for row in configuration.specialized_rows
        )
        version_id = port.create_draft(
            configuration,
            resolved_payload=resolved_payload,
            resolved_dependencies=dependencies,
            resolved_specialized_rows=specialized,
            request_id=f"{plan.request_id}:{configuration.plan_key}:draft",
        )
        version_ids[configuration.plan_key] = _positive(
            version_id, f"version_ids.{configuration.plan_key}"
        )
        port.validate_version(
            type_key=configuration.type_key,
            version_id=version_id,
            request_id=f"{plan.request_id}:{configuration.plan_key}:validate",
        )
    for configuration in plan.configurations:
        port.activate_version(
            type_key=configuration.type_key,
            version_id=version_ids[configuration.plan_key],
            scope_type=plan.scope_type,
            scope_key=plan.scope_key,
            request_id=f"{plan.request_id}:{configuration.plan_key}:activate",
        )
    bundle_id, bundle_digest, repeat_noop = port.materialize_bundle(
        workflow_kind=plan.workflow_kind,
        scope_type=plan.scope_type,
        scope_key=plan.scope_key,
        aggregate_version_id=version_ids["research"],
        installed_adapter_manifest_digest=plan.installed_adapter_manifest_digest,
        request_id=f"{plan.request_id}:bundle",
    )
    if not isinstance(bundle_digest, str) or len(bundle_digest) != 64:
        _blocked("ACTIVATION_BUNDLE_DIGEST_INVALID", "Bundle digest is invalid.")
    return OwnerActivationResult(
        version_ids=dict(version_ids),
        bundle_id=_positive(bundle_id, "bundle_id"),
        bundle_digest=bundle_digest,
        repeat_noop=repeat_noop,
    )


def _quality_components() -> list[dict[str, Any]]:
    return [
        {"component_key": "required_windows", "metric_key": "required_windows_score", "transform": "direct", "weight": 0.35},
        {"component_key": "trade_activity", "metric_key": "total_trades", "transform": "ratio-cap", "denominator": 30, "weight": 0.20},
        {"component_key": "static_quality", "metric_key": "static_quality_score", "transform": "direct", "weight": 0.15},
        {"component_key": "metric_completeness", "metric_key": "ignored", "transform": "completeness", "required_metric_keys": ["net_profit", "max_drawdown", "total_trades", "win_rate"], "weight": 0.20},
        {"component_key": "quality_signals", "metric_key": "ignored", "transform": "signal-penalty", "base": 100, "error_metric_key": "quality_error_count", "warning_metric_key": "quality_warning_count", "error_penalty": 40, "warning_penalty": 15, "weight": 0.10},
    ]


def _elimination_rules() -> list[dict[str, Any]]:
    return [
        {"rule_key": "MAX_DRAWDOWN_FATAL", "metric_key": "max_drawdown", "operator": "gte", "threshold": 0.35},
        {"rule_key": "TOO_FEW_TRADES_FATAL", "metric_key": "total_trades", "operator": "lt", "threshold": 3},
        {"rule_key": "ALL_METRICS_MISSING", "metric_key": "all_metrics_missing", "operator": "eq", "threshold": True, "match_when_missing": False},
        {"rule_key": "VALIDATION_ERROR", "metric_key": "validation_error", "operator": "eq", "threshold": True, "match_when_missing": False},
    ]


def _warning_rules() -> list[dict[str, Any]]:
    return [
        {"rule_key": "DRAWDOWN_WARNING", "metric_key": "max_drawdown", "operator": "gte", "threshold": 0.20},
        {"rule_key": "TRADE_COUNT_WARNING", "metric_key": "total_trades", "operator": "lt", "threshold": 10},
        {"rule_key": "WIN_RATE_WARNING", "metric_key": "win_rate", "operator": "lt", "threshold": 0.35},
    ]


def _resolve_refs(value: Any, version_ids: Mapping[str, int]) -> Any:
    if isinstance(value, Mapping):
        return {key: _resolve_refs(item, version_ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(item, version_ids) for item in value]
    if isinstance(value, tuple):
        return tuple(_resolve_refs(item, version_ids) for item in value)
    if isinstance(value, str) and value.startswith("$"):
        return _resolve_id(value, version_ids)
    return value


def _resolve_id(reference: int | str, version_ids: Mapping[str, int]) -> int:
    if isinstance(reference, str) and reference.startswith("$"):
        key = reference[1:]
        if key not in version_ids:
            _blocked(
                "ACTIVATION_DEPENDENCY_ORDER_INVALID",
                "Activation dependency is not available in topological order.",
                reference=reference,
            )
        return version_ids[key]
    return _positive(reference, "dependency_version_id")


def _plan_digest(plan: OwnerResearchActivationPlan) -> str:
    return _digest({
        "contract": plan.contract,
        "scope_type": plan.scope_type,
        "scope_key": plan.scope_key,
        "workflow_kind": plan.workflow_kind,
        "adapters": [asdict(item) for item in plan.adapters],
        "configurations": [asdict(item) for item in plan.configurations],
        "installed_adapter_manifest_digest": plan.installed_adapter_manifest_digest,
    })


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _blocked("ACTIVATION_INPUT_INVALID", f"{field} must be a positive integer.")
    return value


def _positive_ids(values: Sequence[int], field: str) -> tuple[int, ...]:
    result = tuple(_positive(value, field) for value in values)
    if not result or len(set(result)) != len(result):
        _blocked("ACTIVATION_INPUT_INVALID", f"{field} must be non-empty and unique.")
    return result


def _digest(value: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        value, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _blocked(code: str, message: str, **context: Any) -> None:
    raise StrategyPlatformReadError(code, message, context=context)
