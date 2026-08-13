from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.services.frozen_configuration_bundle import VerifiedConfigurationBundle


@dataclass(frozen=True)
class GenerationProfileContract:
    version_id: int
    candidates_per_target: int
    target_count: int
    candidate_count: int
    family_version_ids: tuple[int, ...]


@dataclass(frozen=True)
class DiversityProfileContract:
    version_id: int
    generation_profile_version_id: int
    adapter_key: str
    required_family_version_ids: tuple[int, ...]
    thresholds: Mapping[str, float]


@dataclass(frozen=True)
class ScoringProfileContract:
    version_id: int
    adapter_key: str
    component_weights: Mapping[str, float]
    normalization_rules: Mapping[str, Mapping[str, Any]]
    quality_components: tuple[Mapping[str, Any], ...]
    elimination_rules: tuple[Mapping[str, Any], ...]
    warning_rules: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class DiversityEvaluation:
    status: str
    validated_candidate_count: int
    validated_target_count: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ScoreEvaluation:
    total_score: float
    components: Mapping[str, float]
    quality_components: Mapping[str, float]
    eliminated_by: tuple[str, ...]
    warnings: tuple[str, ...]


def generation_profile(
    bundle: VerifiedConfigurationBundle,
) -> GenerationProfileContract:
    version = bundle.require_single_version("generation-profile")
    payload = version.payload_json
    candidates_per_target = _positive_int(payload, "candidates_per_target")
    target_count = _positive_int(payload, "total_target_count")
    candidate_count = _positive_int(payload, "total_candidate_count")
    if candidate_count != candidates_per_target * target_count:
        _blocked(
            "GENERATION_PROFILE_COUNT_MISMATCH",
            "Generation profile candidate total does not match its target allocation.",
            version_id=version.id,
        )
    family_ids = _positive_int_sequence(payload, "strategy_family_version_ids")
    if len(set(family_ids)) != len(family_ids):
        _blocked(
            "GENERATION_PROFILE_FAMILY_DUPLICATED",
            "Generation profile contains duplicated strategy family versions.",
            version_id=version.id,
        )
    resolved_family_ids = {
        item.id
        for item in bundle.versions_by_type.get("strategy-family-definition", ())
    }
    if set(family_ids) != resolved_family_ids:
        _blocked(
            "GENERATION_PROFILE_FAMILY_GRAPH_MISMATCH",
            "Generation profile family ids do not match the frozen dependency graph.",
            version_id=version.id,
        )
    return GenerationProfileContract(
        version_id=version.id,
        candidates_per_target=candidates_per_target,
        target_count=target_count,
        candidate_count=candidate_count,
        family_version_ids=family_ids,
    )


def diversity_profile(bundle: VerifiedConfigurationBundle) -> DiversityProfileContract:
    version = bundle.require_single_version("diversity-profile")
    payload = version.payload_json
    adapter_key = _required_text(payload, "evaluation_adapter_key")
    if adapter_key != "diversity-threshold-v2":
        _blocked(
            "DYNAMIC_DIVERSITY_ADAPTER_REQUIRED",
            "Profile-bound research cannot reuse the fixed historical "
            "diversity adapter.",
            version_id=version.id,
            adapter_key=adapter_key,
        )
    generation_version_id = _positive_int(
        payload, "generation_profile_version_id"
    )
    family_ids = _positive_int_sequence(
        payload, "required_family_version_ids"
    )
    raw_thresholds = _required_mapping(payload, "thresholds")
    thresholds = {
        str(metric_key): _ratio(value, f"thresholds.{metric_key}")
        for metric_key, value in raw_thresholds.items()
    }
    if not thresholds:
        _blocked(
            "DIVERSITY_PROFILE_THRESHOLDS_REQUIRED",
            "Diversity profile must persist at least one metric threshold.",
        )
    return DiversityProfileContract(
        version_id=version.id,
        generation_profile_version_id=generation_version_id,
        adapter_key=adapter_key,
        required_family_version_ids=family_ids,
        thresholds=thresholds,
    )


def scoring_profile(bundle: VerifiedConfigurationBundle) -> ScoringProfileContract:
    version = bundle.require_single_version("scoring-profile")
    payload = version.payload_json
    adapter_key = _required_text(payload, "scoring_adapter_key")
    if adapter_key != "profile-bound-score-v2":
        _blocked(
            "COMPLETE_SCORING_ADAPTER_REQUIRED",
            "Scoring requires the complete profile-bound v2 adapter contract.",
            version_id=version.id,
            adapter_key=adapter_key,
        )
    component_weights = _weight_map(payload, "component_weights")
    normalization_rules = _mapping_of_mappings(payload, "normalization_rules")
    if set(component_weights) != set(normalization_rules):
        _blocked(
            "SCORING_NORMALIZATION_SET_MISMATCH",
            "Every scoring component must have one persisted normalization rule.",
            version_id=version.id,
        )
    quality_components = _rule_sequence(payload, "quality_components")
    quality_weights: dict[str, float] = {}
    for rule in quality_components:
        component_key = _required_text(rule, "component_key")
        _required_text(rule, "metric_key")
        _required_text(rule, "transform")
        if component_key in quality_weights:
            _blocked(
                "SCORING_QUALITY_COMPONENT_DUPLICATED",
                "Scoring profile contains a duplicated quality component.",
                component_key=component_key,
            )
        quality_weights[component_key] = _ratio(
            rule.get("weight"), "quality_components.weight"
        )
    _require_weight_sum(quality_weights, "quality components")
    elimination_rules = _rule_sequence(payload, "elimination_rules")
    warning_rules = _rule_sequence(payload, "warning_rules")
    rule_keys: set[str] = set()
    for rule in (*elimination_rules, *warning_rules):
        rule_key = _required_text(rule, "rule_key")
        if rule_key in rule_keys:
            _blocked(
                "SCORING_RULE_DUPLICATED",
                "Scoring profile contains a duplicated rule key.",
                rule_key=rule_key,
            )
        rule_keys.add(rule_key)
        _required_text(rule, "metric_key")
        _comparison(rule)
    return ScoringProfileContract(
        version_id=version.id,
        adapter_key=adapter_key,
        component_weights=component_weights,
        normalization_rules=normalization_rules,
        quality_components=quality_components,
        elimination_rules=elimination_rules,
        warning_rules=warning_rules,
    )


def evaluate_profile_bound_diversity(
    bundle: VerifiedConfigurationBundle,
    evidence: Mapping[str, Any],
) -> DiversityEvaluation:
    generation = generation_profile(bundle)
    diversity = diversity_profile(bundle)
    if diversity.generation_profile_version_id != generation.version_id:
        _blocked(
            "DIVERSITY_GENERATION_PROFILE_MISMATCH",
            "Diversity profile does not bind the resolved generation profile.",
        )
    if set(diversity.required_family_version_ids) != set(
        generation.family_version_ids
    ):
        _blocked(
            "DIVERSITY_FAMILY_PROFILE_MISMATCH",
            "Diversity and generation profiles do not bind the same families.",
        )

    candidate_count = _positive_int(evidence, "candidate_count")
    target_count = _positive_int(evidence, "target_count")
    observed_families = set(
        _positive_int_sequence(evidence, "observed_family_version_ids")
    )
    reasons: list[str] = []
    if candidate_count != generation.candidate_count:
        reasons.append("CANDIDATE_COUNT_MISMATCH")
    if target_count != generation.target_count:
        reasons.append("TARGET_COUNT_MISMATCH")
    if not set(diversity.required_family_version_ids).issubset(observed_families):
        reasons.append("REQUIRED_FAMILY_MISSING")
    metrics = _required_mapping(evidence, "metrics")
    for metric_key, threshold in diversity.thresholds.items():
        actual = _finite_number(metrics.get(metric_key), f"metrics.{metric_key}")
        if actual > threshold:
            reasons.append(f"THRESHOLD_EXCEEDED:{metric_key}")
    return DiversityEvaluation(
        status="PASSED" if not reasons else "BLOCKED",
        validated_candidate_count=candidate_count,
        validated_target_count=target_count,
        reasons=tuple(reasons),
    )


def score_profile_bound_candidate(
    bundle: VerifiedConfigurationBundle,
    metrics: Mapping[str, Any],
) -> ScoreEvaluation:
    profile = scoring_profile(bundle)
    quality_components = {
        _required_text(rule, "component_key"): _quality_component(rule, metrics)
        for rule in profile.quality_components
    }
    source_metrics = dict(metrics)
    source_metrics["quality_score"] = sum(
        quality_components[_required_text(rule, "component_key")]
        * _ratio(rule.get("weight"), "quality_components.weight")
        for rule in profile.quality_components
    )
    components = {
        component: _normalize(
            source_metrics.get(component),
            profile.normalization_rules[component],
            component,
        )
        for component in profile.component_weights
    }
    total_score = sum(
        components[component] * weight
        for component, weight in profile.component_weights.items()
    )
    eliminated_by = tuple(
        _required_text(rule, "rule_key")
        for rule in profile.elimination_rules
        if _rule_matches(rule, source_metrics)
    )
    warnings = tuple(
        _required_text(rule, "rule_key")
        for rule in profile.warning_rules
        if _rule_matches(rule, source_metrics)
    )
    return ScoreEvaluation(
        total_score=_clamp(total_score),
        components=components,
        quality_components=quality_components,
        eliminated_by=eliminated_by,
        warnings=warnings,
    )


def _quality_component(rule: Mapping[str, Any], metrics: Mapping[str, Any]) -> float:
    transform = _required_text(rule, "transform")
    if transform == "direct":
        return _clamp(_finite_number(metrics.get(rule["metric_key"]), "quality metric"))
    if transform == "ratio-cap":
        denominator = _positive_number(rule.get("denominator"), "denominator")
        value = _finite_number(metrics.get(rule["metric_key"]), "quality metric")
        return _clamp((value / denominator) * 100.0)
    if transform == "completeness":
        required_keys = _text_sequence(rule, "required_metric_keys")
        observed = sum(metrics.get(key) is not None for key in required_keys)
        return _clamp((observed / len(required_keys)) * 100.0)
    if transform == "signal-penalty":
        base = _finite_number(rule.get("base"), "base")
        error_penalty = _positive_number(rule.get("error_penalty"), "error_penalty")
        warning_penalty = _positive_number(
            rule.get("warning_penalty"), "warning_penalty"
        )
        errors = _non_negative_int(metrics, _required_text(rule, "error_metric_key"))
        warnings = _non_negative_int(
            metrics, _required_text(rule, "warning_metric_key")
        )
        return _clamp(base - errors * error_penalty - warnings * warning_penalty)
    _blocked(
        "SCORING_QUALITY_TRANSFORM_UNSUPPORTED",
        "Scoring profile contains an unsupported quality transform.",
        transform=transform,
    )


def _normalize(value: Any, rule: Mapping[str, Any], component: str) -> float:
    transform = _required_text(rule, "transform")
    number = _finite_number(value, component)
    if transform == "linear":
        slope = _finite_number(rule.get("slope"), f"{component}.slope")
        intercept = _finite_number(rule.get("intercept"), f"{component}.intercept")
        return _clamp(number * slope + intercept)
    if transform == "absolute-linear":
        slope = _finite_number(rule.get("slope"), f"{component}.slope")
        intercept = _finite_number(rule.get("intercept"), f"{component}.intercept")
        return _clamp(abs(number) * slope + intercept)
    _blocked(
        "SCORING_NORMALIZATION_UNSUPPORTED",
        "Scoring profile contains an unsupported normalization transform.",
        component=component,
        transform=transform,
    )


def _rule_matches(rule: Mapping[str, Any], metrics: Mapping[str, Any]) -> bool:
    metric_key = _required_text(rule, "metric_key")
    operator, threshold = _comparison(rule)
    actual = metrics.get(metric_key)
    if actual is None:
        return bool(rule.get("match_when_missing") is True)
    if operator == "eq":
        return actual == threshold
    actual_number = _finite_number(actual, metric_key)
    threshold_number = _finite_number(threshold, f"{metric_key}.threshold")
    return {
        "gte": actual_number >= threshold_number,
        "gt": actual_number > threshold_number,
        "lte": actual_number <= threshold_number,
        "lt": actual_number < threshold_number,
    }[operator]


def _comparison(rule: Mapping[str, Any]) -> tuple[str, Any]:
    operator = _required_text(rule, "operator")
    if operator not in {"eq", "gte", "gt", "lte", "lt"}:
        _blocked(
            "SCORING_RULE_OPERATOR_UNSUPPORTED",
            "Scoring rule contains an unsupported comparison operator.",
            operator=operator,
        )
    if "threshold" not in rule:
        _blocked(
            "SCORING_RULE_THRESHOLD_REQUIRED",
            "Scoring rule must persist an explicit threshold.",
        )
    return operator, rule["threshold"]


def _weight_map(payload: Mapping[str, Any], key: str) -> Mapping[str, float]:
    values = {
        str(item_key): _ratio(item, f"{key}.{item_key}")
        for item_key, item in _required_mapping(payload, key).items()
    }
    _require_weight_sum(values, key)
    return values


def _require_weight_sum(values: Mapping[str, float], label: str) -> None:
    if not values or not math.isclose(sum(values.values()), 1.0, abs_tol=1e-9):
        _blocked(
            "SCORING_WEIGHT_SUM_INVALID",
            f"Persisted {label} weights must sum to one.",
        )


def _mapping_of_mappings(
    payload: Mapping[str, Any], key: str
) -> Mapping[str, Mapping[str, Any]]:
    return {
        str(item_key): _required_mapping({"value": item}, "value")
        for item_key, item in _required_mapping(payload, key).items()
    }


def _rule_sequence(
    payload: Mapping[str, Any], key: str
) -> tuple[Mapping[str, Any], ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        _blocked(
            "PROFILE_RULES_REQUIRED",
            f"Profile must persist a non-empty {key} array.",
        )
    rules: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            _blocked("PROFILE_RULE_INVALID", f"Profile {key} item must be an object.")
        rules.append(item)
    return tuple(rules)


def _required_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        _blocked("PROFILE_FIELD_INVALID", f"Profile field {key} must be an object.")
    return value


def _positive_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _blocked("PROFILE_FIELD_INVALID", f"Profile field {key} must be positive.")
    return value


def _non_negative_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _blocked(
            "SCORING_METRIC_INVALID", f"Scoring metric {key} must be non-negative."
        )
    return value


def _positive_int_sequence(payload: Mapping[str, Any], key: str) -> tuple[int, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        _blocked("PROFILE_FIELD_INVALID", f"Profile field {key} must be a list.")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            _blocked(
                "PROFILE_FIELD_INVALID",
                f"Profile field {key} must contain positive ids.",
            )
        result.append(item)
    return tuple(result)


def _text_sequence(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        _blocked("PROFILE_FIELD_INVALID", f"Profile field {key} must be a list.")
    return tuple(_required_text({"value": item}, "value") for item in value)


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        _blocked("PROFILE_FIELD_INVALID", f"Profile field {key} must be explicit.")
    return value.strip()


def _ratio(value: Any, label: str) -> float:
    number = _finite_number(value, label)
    if number < 0 or number > 1:
        _blocked("PROFILE_FIELD_INVALID", f"Profile field {label} must be a ratio.")
    return number


def _positive_number(value: Any, label: str) -> float:
    number = _finite_number(value, label)
    if number <= 0:
        _blocked("PROFILE_FIELD_INVALID", f"Profile field {label} must be positive.")
    return number


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _blocked("PROFILE_FIELD_INVALID", f"Profile field {label} must be numeric.")
    number = float(value)
    if not math.isfinite(number):
        _blocked("PROFILE_FIELD_INVALID", f"Profile field {label} must be finite.")
    return number


def _clamp(value: float) -> float:
    return min(100.0, max(0.0, value))


def _blocked(code: str, message: str, **context: Any) -> None:
    raise StrategyPlatformReadError(code, message, context=context)
