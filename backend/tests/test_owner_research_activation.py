from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.services.owner_research_activation import (
    ACTIVATION_SCOPE_KEY,
    ACTIVATION_SCOPE_TYPE,
    ExistingResearchBindings,
    build_owner_research_activation_plan,
    execute_owner_research_activation,
)


REQUIRED_METRICS = (
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
)


def _bindings() -> ExistingResearchBindings:
    return ExistingResearchBindings(
        provider_model_config_version_id=10,
        research_target_config_set_id=11,
        validation_window_config_set_id=12,
        market_data_policy_version_id=13,
        evidence_freshness_profile_version_id=14,
        scheduler_profile_version_id=15,
        worker_execution_profile_version_id=16,
        strategy_family_version_ids=(31, 32, 33),
        metric_version_ids={
            key: version_id
            for version_id, key in enumerate(REQUIRED_METRICS, start=101)
        },
    )


class _OwnerPort:
    def __init__(self) -> None:
        self.adapters = []
        self.drafts = []
        self.validations = []
        self.activations = []
        self.version_ids: dict[str, int] = {}
        self.bundle_calls = []

    def register_adapter(self, adapter):
        self.adapters.append(adapter)

    def create_draft(
        self,
        configuration,
        *,
        resolved_payload,
        resolved_dependencies,
        resolved_specialized_rows,
        request_id,
    ):
        version_id = self.version_ids.setdefault(
            configuration.plan_key, 1000 + len(self.version_ids)
        )
        self.drafts.append(
            (
                configuration,
                resolved_payload,
                tuple(resolved_dependencies),
                tuple(resolved_specialized_rows),
                request_id,
            )
        )
        return version_id

    def validate_version(self, **kwargs):
        self.validations.append(kwargs)

    def activate_version(self, **kwargs):
        self.activations.append(kwargs)

    def materialize_bundle(self, **kwargs):
        self.bundle_calls.append(kwargs)
        return 5000, hashlib.sha256(b"owner-bundle").hexdigest(), False


def test_plan_persists_complete_v2_contract_without_defaults() -> None:
    plan = build_owner_research_activation_plan(
        _bindings(),
        candidates_per_target=7,
        target_count=4,
        candidate_count=28,
    )

    assert {item.adapter_key for item in plan.adapters} == {
        "diversity-threshold-v2",
        "profile-bound-score-v2",
    }
    assert all(
        value is False
        for item in plan.adapters
        for key, value in item.capabilities.items()
        if key.endswith("_default")
    )
    profiles = {item.plan_key: item for item in plan.configurations}
    assert tuple(profiles) == (
        "generation",
        "diversity",
        "quality",
        "scoring",
        "research",
    )
    assert profiles["generation"].payload["total_candidate_count"] == 28
    assert profiles["generation"].payload["total_target_count"] == 4
    assert profiles["diversity"].payload["thresholds"] == {
        "max_signal_similarity": 0.90,
        "max_abs_pnl_correlation": 0.85,
    }
    scoring = profiles["scoring"].payload
    assert scoring["scoring_adapter_key"] == "profile-bound-score-v2"
    assert scoring["component_weights"] == {
        "profit_score": 0.35,
        "risk_score": 0.25,
        "stability_score": 0.15,
        "quality_score": 0.25,
    }
    assert scoring["normalization_rules"]["profit_score"] == {
        "transform": "linear",
        "slope": 500,
        "intercept": 50,
    }
    assert scoring["quality_components"][-1]["error_penalty"] == 40
    assert scoring["quality_components"][-1]["warning_penalty"] == 15
    assert [item["threshold"] for item in scoring["warning_rules"]] == [
        0.20,
        10,
        0.35,
    ]
    assert plan.scope_type == ACTIVATION_SCOPE_TYPE
    assert plan.scope_key == ACTIVATION_SCOPE_KEY
    assert len(plan.plan_digest) == 64
    assert plan.request_id == f"issue-707:{plan.plan_digest}"


def test_plan_requires_explicit_dynamic_counts_and_exact_metric_set() -> None:
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        build_owner_research_activation_plan(
            _bindings(),
            candidates_per_target=7,
            target_count=4,
            candidate_count=29,
        )
    assert exc_info.value.code == "GENERATION_PROFILE_COUNT_MISMATCH"

    bindings = replace(
        _bindings(),
        metric_version_ids={"profit_score": 101},
    )
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        build_owner_research_activation_plan(
            bindings,
            candidates_per_target=7,
            target_count=4,
            candidate_count=28,
        )
    assert exc_info.value.code == "ACTIVATION_METRIC_SET_INVALID"


def test_another_generation_allocation_needs_no_v1_adapter_change() -> None:
    plan = build_owner_research_activation_plan(
        _bindings(),
        candidates_per_target=7,
        target_count=4,
        candidate_count=28,
    )
    generation = plan.configurations[0]
    assert generation.payload["total_candidate_count"] == 28
    assert generation.payload["total_target_count"] == 4
    assert {item.adapter_key for item in plan.adapters} == {
        "diversity-threshold-v2",
        "profile-bound-score-v2",
    }


def test_owner_execution_is_topological_explicit_scope_and_repeatable() -> None:
    plan = build_owner_research_activation_plan(
        _bindings(),
        candidates_per_target=7,
        target_count=4,
        candidate_count=28,
    )
    port = _OwnerPort()
    first = execute_owner_research_activation(plan, port)
    second = execute_owner_research_activation(plan, port)

    assert first.version_ids == second.version_ids
    assert list(first.version_ids) == [
        "generation",
        "diversity",
        "quality",
        "scoring",
        "research",
    ]
    assert port.drafts[1][1]["generation_profile_version_id"] == (
        first.version_ids["generation"]
    )
    assert port.drafts[3][1]["quality_gate_profile_version_id"] == (
        first.version_ids["quality"]
    )
    assert port.drafts[4][1]["scoring_profile_version_id"] == (
        first.version_ids["scoring"]
    )
    assert len(port.validations) == 10
    assert len(port.activations) == 10
    assert all(
        call["scope_type"] == ACTIVATION_SCOPE_TYPE
        and call["scope_key"] == ACTIVATION_SCOPE_KEY
        for call in port.activations
    )
    assert port.bundle_calls[-1]["aggregate_version_id"] == (
        first.version_ids["research"]
    )


def test_plan_digest_tampering_blocks_before_owner_port_calls() -> None:
    plan = build_owner_research_activation_plan(
        _bindings(),
        candidates_per_target=7,
        target_count=4,
        candidate_count=28,
    )
    tampered = replace(plan, plan_digest="0" * 64)
    port = _OwnerPort()

    with pytest.raises(StrategyPlatformReadError) as exc_info:
        execute_owner_research_activation(tampered, port)

    assert exc_info.value.code == "ACTIVATION_PLAN_DIGEST_INVALID"
    assert port.adapters == []
    assert port.drafts == []
