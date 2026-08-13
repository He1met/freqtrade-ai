from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import pytest

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.schemas.strategy_platform import ConfigurationBundleSnapshotRead
from app.services.frozen_configuration_bundle import (
    validate_frozen_configuration_bundle,
)
from app.services.profile_bound_adapters import (
    evaluate_profile_bound_diversity,
    score_profile_bound_candidate,
)


REGISTRY_DIGEST = hashlib.sha256(b"profile-adapters").hexdigest()


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _version(version_id: int, type_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    schema_version = f"{type_key}-v2"
    return {
        "id": version_id,
        "type_key": type_key,
        "version_number": 2,
        "lifecycle_status": "VALIDATED",
        "payload_json": payload,
        "schema_version": schema_version,
        "config_digest": _digest(
            {
                "contract": "configuration-version-digest-v1",
                "config_type": type_key,
                "schema_version": schema_version,
                "payload_json": payload,
            }
        ),
        "change_summary": "profile-bound adapter test",
        "created_by": "control-plane",
        "created_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
        "validated_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
    }


def _profile_payloads(*, target_count: int = 6, candidate_count: int = 60):
    generation = {
        "candidates_per_target": candidate_count // target_count,
        "total_target_count": target_count,
        "total_candidate_count": candidate_count,
        "strategy_family_version_ids": [301, 302, 303],
    }
    diversity = {
        "evaluation_adapter_key": "diversity-threshold-v2",
        "generation_profile_version_id": 101,
        "required_family_version_ids": [301, 302, 303],
        "thresholds": {
            "max_signal_similarity": 0.90,
            "max_abs_pnl_correlation": 0.85,
        },
    }
    scoring = {
        "scoring_adapter_key": "profile-bound-score-v2",
        "component_weights": {
            "profit_score": 0.35,
            "risk_score": 0.25,
            "stability_score": 0.15,
            "quality_score": 0.25,
        },
        "normalization_rules": {
            "profit_score": {
                "transform": "linear",
                "slope": 500,
                "intercept": 50,
            },
            "risk_score": {
                "transform": "absolute-linear",
                "slope": -500,
                "intercept": 100,
            },
            "stability_score": {
                "transform": "linear",
                "slope": 100,
                "intercept": 0,
            },
            "quality_score": {
                "transform": "linear",
                "slope": 1,
                "intercept": 0,
            },
        },
        "quality_components": [
            {
                "component_key": "required_windows",
                "metric_key": "required_windows_score",
                "transform": "direct",
                "weight": 0.35,
            },
            {
                "component_key": "trade_activity",
                "metric_key": "total_trades",
                "transform": "ratio-cap",
                "denominator": 30,
                "weight": 0.20,
            },
            {
                "component_key": "static_quality",
                "metric_key": "static_quality_score",
                "transform": "direct",
                "weight": 0.15,
            },
            {
                "component_key": "metric_completeness",
                "metric_key": "ignored",
                "transform": "completeness",
                "required_metric_keys": [
                    "net_profit",
                    "max_drawdown",
                    "total_trades",
                    "win_rate",
                ],
                "weight": 0.20,
            },
            {
                "component_key": "quality_signals",
                "metric_key": "ignored",
                "transform": "signal-penalty",
                "base": 100,
                "error_metric_key": "quality_error_count",
                "warning_metric_key": "quality_warning_count",
                "error_penalty": 40,
                "warning_penalty": 15,
                "weight": 0.10,
            },
        ],
        "elimination_rules": [
            {
                "rule_key": "MAX_DRAWDOWN_FATAL",
                "metric_key": "max_drawdown",
                "operator": "gte",
                "threshold": 0.35,
            },
            {
                "rule_key": "TOO_FEW_TRADES_FATAL",
                "metric_key": "total_trades",
                "operator": "lt",
                "threshold": 3,
            },
            {
                "rule_key": "ALL_METRICS_MISSING",
                "metric_key": "all_metrics_missing",
                "operator": "eq",
                "threshold": True,
                "match_when_missing": False,
            },
            {
                "rule_key": "VALIDATION_ERROR",
                "metric_key": "validation_error",
                "operator": "eq",
                "threshold": True,
                "match_when_missing": False,
            },
        ],
        "warning_rules": [
            {
                "rule_key": "DRAWDOWN_WARNING",
                "metric_key": "max_drawdown",
                "operator": "gte",
                "threshold": 0.20,
            },
            {
                "rule_key": "TRADE_COUNT_WARNING",
                "metric_key": "total_trades",
                "operator": "lt",
                "threshold": 10,
            },
            {
                "rule_key": "WIN_RATE_WARNING",
                "metric_key": "win_rate",
                "operator": "lt",
                "threshold": 0.35,
            },
        ],
    }
    return generation, diversity, scoring


def _bundle(*, target_count: int = 6, candidate_count: int = 60):
    generation, diversity, scoring = _profile_payloads(
        target_count=target_count,
        candidate_count=candidate_count,
    )
    versions = [
        _version(100, "research-profile", {"demo_only": True}),
        _version(101, "generation-profile", generation),
        _version(102, "diversity-profile", diversity),
        _version(103, "scoring-profile", scoring),
        _version(301, "strategy-family-definition", {"family_key": "a"}),
        _version(302, "strategy-family-definition", {"family_key": "b"}),
        _version(303, "strategy-family-definition", {"family_key": "c"}),
    ]
    version_map = {
        f"{item['type_key']}:{item['id']}": item["id"] for item in versions
    }
    digest_map = {
        f"{item['type_key']}:{item['id']}": item["config_digest"]
        for item in versions
    }
    dependencies = [
        {
            "configuration_version_id": 100,
            "configuration_type": "research-profile",
            "depends_on_version_id": child_id,
            "depends_on_type": child_type,
            "relation_key": relation,
        }
        for child_id, child_type, relation in (
            (101, "generation-profile", "generation"),
            (102, "diversity-profile", "diversity"),
            (103, "scoring-profile", "scoring"),
        )
    ]
    dependencies.extend(
        {
            "configuration_version_id": 101,
            "configuration_type": "generation-profile",
            "depends_on_version_id": family_id,
            "depends_on_type": "strategy-family-definition",
            "relation_key": f"strategy_family:{family_id}",
        }
        for family_id in (301, 302, 303)
    )
    capability = {
        "demo_only": True,
        "allow_real_funds": False,
        "single_writer_required": True,
        "resolution_contract": "strategy-platform-owner-resolver-v1",
        "adapter_registry_digest": REGISTRY_DIGEST,
    }
    raw: dict[str, Any] = {
        "persisted": True,
        "snapshot_id": 900,
        "workflow_kind": "RESEARCH",
        "scope_type": "PLATFORM",
        "scope_key": "DEFAULT",
        "aggregate_profile_version_id": 100,
        "resolved_versions": versions,
        "dependencies": dependencies,
        "resolved_versions_json": version_map,
        "resolved_digests_json": digest_map,
        "bundle_digest": "",
        "capability_snapshot": capability,
        "created_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
    }
    raw["bundle_digest"] = _digest(
        {
            "digest_contract": "configuration-bundle-digest-v1",
            "workflow_kind": "RESEARCH",
            "scope_type": "PLATFORM",
            "scope_key": "DEFAULT",
            "aggregate_profile_version_id": 100,
            "resolved_versions_json": dict(sorted(version_map.items())),
            "resolved_digests_json": dict(sorted(digest_map.items())),
            "capability_snapshot": capability,
        }
    )
    snapshot = ConfigurationBundleSnapshotRead.model_validate(raw)
    return validate_frozen_configuration_bundle(
        snapshot,
        expected_adapter_registry_digest=REGISTRY_DIGEST,
    )


def test_historical_sixty_by_six_is_bound_to_profiles_not_adapter_defaults() -> None:
    result = evaluate_profile_bound_diversity(
        _bundle(),
        {
            "candidate_count": 60,
            "target_count": 6,
            "observed_family_version_ids": [301, 302, 303],
            "metrics": {
                "max_signal_similarity": 0.80,
                "max_abs_pnl_correlation": 0.70,
            },
        },
    )

    assert result.status == "PASSED"
    assert result.validated_candidate_count == 60
    assert result.validated_target_count == 6


def test_dynamic_adapter_accepts_another_profile_without_code_changes() -> None:
    result = evaluate_profile_bound_diversity(
        _bundle(target_count=4, candidate_count=28),
        {
            "candidate_count": 28,
            "target_count": 4,
            "observed_family_version_ids": [301, 302, 303],
            "metrics": {
                "max_signal_similarity": 0.80,
                "max_abs_pnl_correlation": 0.70,
            },
        },
    )

    assert result.status == "PASSED"
    assert result.validated_candidate_count == 28
    assert result.validated_target_count == 4


def test_diversity_evidence_must_match_profile_counts_and_thresholds() -> None:
    result = evaluate_profile_bound_diversity(
        _bundle(),
        {
            "candidate_count": 59,
            "target_count": 5,
            "observed_family_version_ids": [301],
            "metrics": {
                "max_signal_similarity": 0.95,
                "max_abs_pnl_correlation": 0.70,
            },
        },
    )

    assert result.status == "BLOCKED"
    assert set(result.reasons) == {
        "CANDIDATE_COUNT_MISMATCH",
        "TARGET_COUNT_MISMATCH",
        "REQUIRED_FAMILY_MISSING",
        "THRESHOLD_EXCEEDED:max_signal_similarity",
    }


def test_scoring_uses_persisted_normalization_quality_and_rule_thresholds() -> None:
    result = score_profile_bound_candidate(
        _bundle(),
        {
            "profit_score": 0.10,
            "risk_score": 0.10,
            "stability_score": 0.80,
            "required_windows_score": 100,
            "static_quality_score": 100,
            "net_profit": 0.10,
            "max_drawdown": 0.20,
            "total_trades": 30,
            "win_rate": 0.34,
            "quality_error_count": 1,
            "quality_warning_count": 1,
            "all_metrics_missing": False,
            "validation_error": False,
        },
    )

    assert result.components == {
        "profit_score": 100.0,
        "risk_score": 50.0,
        "stability_score": 80.0,
        "quality_score": 94.5,
    }
    assert result.quality_components["quality_signals"] == 45.0
    assert result.total_score == pytest.approx(83.125)
    assert result.eliminated_by == ()
    assert result.warnings == ("DRAWDOWN_WARNING", "WIN_RATE_WARNING")


def test_missing_scoring_contract_field_blocks_without_fallback() -> None:
    bundle = _bundle()
    scoring = bundle.require_single_version("scoring-profile")
    incomplete = deepcopy(scoring.payload_json)
    incomplete.pop("normalization_rules")
    scoring.payload_json.clear()
    scoring.payload_json.update(incomplete)

    with pytest.raises(StrategyPlatformReadError) as exc_info:
        score_profile_bound_candidate(bundle, {})

    assert exc_info.value.code == "PROFILE_FIELD_INVALID"
