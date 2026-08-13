"""Static, auditable registry of adapter implementations installed in this tree.

The registry deliberately validates source files with :mod:`ast`; it never imports
or calls an adapter.  That keeps migration preflight independent of credentials,
network access, runtime state, and executable configuration payloads.

Only adapters with an implementation whose declared semantics can be located in
the repository are listed in ``INSTALLED_ADAPTER_MANIFEST``.  Database declarations
without an equivalent implementation remain in ``UNMAPPED_ADAPTERS`` and therefore
fail a complete-coverage check instead of being represented by placeholders.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from app.services.strategy_platform_builtin_adapters import (
    BUILTIN_ADAPTER_JSON_SCHEMAS,
)


_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class AdapterManifestValidationError(ValueError):
    """The installed adapter manifest cannot be trusted."""

    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = tuple(sorted(set(issues)))
        super().__init__("adapter manifest validation failed: " + "; ".join(self.issues))


@dataclass(frozen=True)
class InstalledAdapter:
    adapter_key: str
    adapter_kind: str
    implementation_version: str
    input_schema_version: str
    output_schema_version: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    capabilities: Mapping[str, Any]
    source_ref: str
    source_sha256: str


@dataclass(frozen=True)
class UnmappedAdapter:
    adapter_key: str
    adapter_kind: str
    reason: str


def _object_schema(
    *,
    required: Sequence[str],
    properties: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "type": "object",
        "required": list(required),
        "properties": dict(properties),
        "additionalProperties": False,
    }


INSTALLED_ADAPTER_MANIFEST: tuple[InstalledAdapter, ...] = (
    InstalledAdapter(
        adapter_key="window-close-return-v1",
        adapter_kind="MARKET_CLASSIFIER",
        implementation_version="1.0.0",
        input_schema_version="window-close-return-input-v1",
        output_schema_version="market-regime-classification-v1",
        input_schema=_object_schema(
            required=("closes", "window_start", "window_end"),
            properties={
                "closes": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 2,
                },
                "window_start": {"type": "string", "format": "date-time"},
                "window_end": {"type": "string", "format": "date-time"},
            },
        ),
        output_schema=_object_schema(
            required=("regime", "window_return"),
            properties={
                "regime": {"type": "string", "enum": ["bull", "bear", "range"]},
                "window_return": {"type": "number"},
            },
        ),
        capabilities={"closed_candles": True, "classification_basis": "window_return"},
        source_ref=(
            "backend/app/services/strategy_validation_matrix.py"
            "#_classify_market_regime"
        ),
        source_sha256="02bc856ba58e371acb40bd3f23b6b1172e7307ed9a17e80881ad6f8274e60185",
    ),
    InstalledAdapter(
        adapter_key="weighted-component-score-v1",
        adapter_kind="SCORER",
        implementation_version="phase2-quality-v1",
        input_schema_version="phase2-component-scores-v1",
        output_schema_version="phase2-weighted-score-v1",
        input_schema=_object_schema(
            required=(
                "profit_score",
                "risk_score",
                "stability_score",
                "quality_score",
            ),
            properties={
                "profit_score": {"type": "number", "minimum": 0, "maximum": 100},
                "risk_score": {"type": "number", "minimum": 0, "maximum": 100},
                "stability_score": {"type": "number", "minimum": 0, "maximum": 100},
                "quality_score": {"type": "number", "minimum": 0, "maximum": 100},
            },
        ),
        output_schema=_object_schema(
            required=("total_score", "scoring_version"),
            properties={
                "total_score": {"type": "number", "minimum": 0, "maximum": 100},
                "scoring_version": {"type": "string", "const": "phase2-quality-v1"},
            },
        ),
        capabilities={"bounded_score": True, "aggregation": "weighted_sum"},
        source_ref=(
            "backend/app/services/strategy_scoring.py"
            "#StrategyScoringService._weighted_score"
        ),
        source_sha256="516509b7c09cc250b080f58fabb43c7206ce3b68a49169318433ddaff529fcd6",
    ),
    InstalledAdapter(
        adapter_key="linear-normalization-v1",
        adapter_kind="NORMALIZER",
        implementation_version="phase2-quality-v1",
        input_schema_version="bounded-component-value-v1",
        output_schema_version="bounded-component-value-v1",
        input_schema=_object_schema(
            required=("value", "minimum", "maximum"),
            properties={
                "value": {"type": "number"},
                "minimum": {"type": "number", "const": 0},
                "maximum": {"type": "number", "const": 100},
            },
        ),
        output_schema=_object_schema(
            required=("normalized_value",),
            properties={
                "normalized_value": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 100,
                }
            },
        ),
        capabilities={"bounded_score": True, "minimum": 0, "maximum": 100},
        source_ref=(
            "backend/app/services/strategy_scoring.py#StrategyScoringService._clamp"
        ),
        source_sha256="516509b7c09cc250b080f58fabb43c7206ce3b68a49169318433ddaff529fcd6",
    ),
    InstalledAdapter(
        adapter_key="diversity-threshold-v1",
        adapter_kind="DIVERSITY_EVALUATOR",
        implementation_version="research-diversity-contract-v1",
        input_schema_version="research-candidate-diversity-evidence-v1",
        output_schema_version="validated-research-candidate-matrix-v1",
        input_schema=_object_schema(
            required=("candidate_count", "target_count", "evidence_contract"),
            properties={
                "candidate_count": {"type": "integer", "const": 60},
                "target_count": {"type": "integer", "const": 6},
                "evidence_contract": {
                    "type": "string",
                    "const": "research-candidate-diversity-evidence-v1",
                },
            },
        ),
        output_schema=_object_schema(
            required=("validated_candidate_count", "status"),
            properties={
                "validated_candidate_count": {"type": "integer", "const": 60},
                "status": {"type": "string", "const": "PASSED"},
            },
        ),
        capabilities={
            "metrics": ["signal_similarity", "pnl_correlation"],
            "exact_candidate_matrix": True,
        },
        source_ref=(
            "backend/app/core/strategy_research_diversity.py"
            "#validate_research_diversity_contract"
        ),
        source_sha256="1cd8e5d6b3bb0c4aa015def61801afc9335477db383d9f6cc0f3fa6a04913816",
    ),
    InstalledAdapter(
        adapter_key="diversity-threshold-v2",
        adapter_kind="DIVERSITY_EVALUATOR",
        implementation_version="profile-bound-diversity-v2",
        input_schema_version="profile-bound-diversity-input-v2",
        output_schema_version="profile-bound-diversity-output-v2",
        input_schema=_object_schema(
            required=(
                "candidate_count",
                "target_count",
                "observed_family_version_ids",
                "metrics",
            ),
            properties={
                "candidate_count": {"type": "integer", "minimum": 1},
                "target_count": {"type": "integer", "minimum": 1},
                "observed_family_version_ids": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 1,
                },
                "metrics": {
                    "type": "object",
                    "required": [
                        "max_signal_similarity",
                        "max_abs_pnl_correlation",
                    ],
                    "properties": {
                        "max_signal_similarity": {"type": "number"},
                        "max_abs_pnl_correlation": {"type": "number"},
                    },
                    "additionalProperties": False,
                },
            },
        ),
        output_schema=_object_schema(
            required=(
                "status",
                "validated_candidate_count",
                "validated_target_count",
                "reasons",
            ),
            properties={
                "status": {"type": "string", "enum": ["PASSED", "BLOCKED"]},
                "validated_candidate_count": {"type": "integer", "minimum": 1},
                "validated_target_count": {"type": "integer", "minimum": 1},
                "reasons": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        ),
        capabilities={
            "profile_bound": True,
            "candidate_count_default": False,
            "target_count_default": False,
            "threshold_default": False,
        },
        source_ref=(
            "backend/app/services/profile_bound_adapters.py"
            "#evaluate_profile_bound_diversity"
        ),
        source_sha256="626f33dbd5b647ca92a54ff7c0770911ea6d96e58fcc1b1170c59d9113a018cf",
    ),
    InstalledAdapter(
        adapter_key="profile-bound-score-v2",
        adapter_kind="SCORER",
        implementation_version="profile-bound-score-v2",
        input_schema_version="profile-bound-score-input-v2",
        output_schema_version="profile-bound-score-output-v2",
        input_schema=_object_schema(
            required=(
                "profit_score",
                "risk_score",
                "stability_score",
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
            ),
            properties={
                "profit_score": {"type": "number"},
                "risk_score": {"type": "number"},
                "stability_score": {"type": "number"},
                "required_windows_score": {"type": "number"},
                "static_quality_score": {"type": "number"},
                "net_profit": {"type": "number"},
                "max_drawdown": {"type": "number"},
                "total_trades": {"type": "integer", "minimum": 0},
                "win_rate": {"type": "number"},
                "quality_error_count": {"type": "integer", "minimum": 0},
                "quality_warning_count": {"type": "integer", "minimum": 0},
                "all_metrics_missing": {"type": "boolean"},
                "validation_error": {"type": "boolean"},
            },
        ),
        output_schema=_object_schema(
            required=(
                "total_score",
                "components",
                "quality_components",
                "eliminated_by",
                "warnings",
            ),
            properties={
                "total_score": {"type": "number", "minimum": 0, "maximum": 100},
                "components": _object_schema(
                    required=(
                        "profit_score",
                        "risk_score",
                        "stability_score",
                        "quality_score",
                    ),
                    properties={
                        "profit_score": {"type": "number"},
                        "risk_score": {"type": "number"},
                        "stability_score": {"type": "number"},
                        "quality_score": {"type": "number"},
                    },
                ),
                "quality_components": _object_schema(
                    required=(
                        "required_windows",
                        "trade_activity",
                        "static_quality",
                        "metric_completeness",
                        "quality_signals",
                    ),
                    properties={
                        "required_windows": {"type": "number"},
                        "trade_activity": {"type": "number"},
                        "static_quality": {"type": "number"},
                        "metric_completeness": {"type": "number"},
                        "quality_signals": {"type": "number"},
                    },
                ),
                "eliminated_by": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
        ),
        capabilities={
            "profile_bound": True,
            "normalization_default": False,
            "quality_weight_default": False,
            "threshold_default": False,
            "bounded_score": True,
        },
        source_ref=(
            "backend/app/services/profile_bound_adapters.py"
            "#score_profile_bound_candidate"
        ),
        source_sha256="626f33dbd5b647ca92a54ff7c0770911ea6d96e58fcc1b1170c59d9113a018cf",
    ),
    InstalledAdapter(
        adapter_key="okx-public-candles-v1",
        adapter_kind="MARKET_DATA_DOWNLOADER",
        implementation_version="okx-public-candle-source-receipt-v1",
        input_schema_version="okx-public-candle-refresh-request-v1",
        output_schema_version="okx-public-candle-source-receipt-v1",
        input_schema=_object_schema(
            required=("instrument", "start_ms", "public_only"),
            properties={
                "instrument": {"type": "string", "pattern": "^[A-Z0-9-]+$"},
                "start_ms": {"type": "integer", "minimum": 0},
                "public_only": {"type": "boolean", "const": True},
            },
        ),
        output_schema=_object_schema(
            required=("receipt_schema_version", "credentials_used", "orders_submitted"),
            properties={
                "receipt_schema_version": {
                    "type": "string",
                    "const": "okx-public-candle-source-receipt-v1",
                },
                "credentials_used": {"type": "boolean", "const": False},
                "orders_submitted": {"type": "boolean", "const": False},
            },
        ),
        capabilities={
            "public_only": True,
            "closed_candles": True,
            "credentials_used": False,
        },
        source_ref="scripts/download_okx_research_market_data.py#main",
        source_sha256="7c03a6e1def396547fc82fca5d92abc108d8066fb9baf90eaef046106225f33e",
    ),
    InstalledAdapter(
        adapter_key="freqtrade-backtest-v1",
        adapter_kind="BACKTEST_RUNNER",
        implementation_version="freqtrade-backtest-artifact-v1",
        input_schema_version="freqtrade-backtest-task-v1",
        output_schema_version="freqtrade-backtest-artifact-v1",
        input_schema=_object_schema(
            required=("config_path", "strategy_name", "result_path", "exchange_connection"),
            properties={
                "config_path": {"type": "string", "minLength": 1},
                "strategy_name": {"type": "string", "minLength": 1},
                "result_path": {"type": "string", "minLength": 1},
                "exchange_connection": {"type": "boolean", "const": False},
            },
        ),
        output_schema=_object_schema(
            required=("status", "manifest_path", "execution_scope_id"),
            properties={
                "status": {"type": "string", "enum": ["SUCCESS", "FAILED", "BLOCKED"]},
                "manifest_path": {"type": "string", "minLength": 1},
                "execution_scope_id": {"type": "string", "minLength": 1},
            },
        ),
        capabilities={"exchange_connection": False, "artifact_manifest": True},
        source_ref=(
            "backend/app/adapters/freqtrade/backtest_runner.py#FreqtradeBacktestRunner"
        ),
        source_sha256="c390c30f9629d7f669ca28865521dd45978d16d4cb4e20437a9cd2b5f65039f0",
    ),
    InstalledAdapter(
        adapter_key="deepseek-generation-v1",
        adapter_kind="GENERATION_PROVIDER",
        implementation_version="canonical-blueprint-v2",
        input_schema_version="strategy-generation-request-v1",
        output_schema_version="canonical-blueprint-v2",
        input_schema=_object_schema(
            required=("prompt_summary", "requested_count", "provider_name"),
            properties={
                "prompt_summary": {"type": "string", "minLength": 1},
                "requested_count": {"type": "integer", "minimum": 1, "maximum": 60},
                "provider_name": {"type": "string", "const": "deepseek"},
            },
        ),
        output_schema=_object_schema(
            required=("blueprint_contract", "generated_count"),
            properties={
                "blueprint_contract": {"type": "string", "const": "canonical-blueprint-v2"},
                "generated_count": {"type": "integer", "minimum": 1, "maximum": 60},
            },
        ),
        capabilities={"external_model": True, "persistence_separated": True},
        source_ref=(
            "backend/app/services/strategy_generation.py"
            "#build_deepseek_single_provider_from_env"
        ),
        source_sha256="8ec45b2a9ee137e1966c4a99a9d0de5e8c962734796b6f51a1da29921d1aaf65",
    ),
    InstalledAdapter(
        adapter_key="freqtrade-hyperopt-v1",
        adapter_kind="OPTIMIZER",
        implementation_version="freqtrade-hyperopt-artifact-v1",
        input_schema_version="freqtrade-hyperopt-task-v1",
        output_schema_version="freqtrade-hyperopt-artifact-v1",
        input_schema=_object_schema(
            required=("profile_name", "strategy_version_id", "config_path", "datadir"),
            properties={
                "profile_name": {"type": "string", "minLength": 1},
                "strategy_version_id": {"type": "integer", "minimum": 1},
                "config_path": {"type": "string", "minLength": 1},
                "datadir": {"type": "string", "minLength": 1},
            },
        ),
        output_schema=_object_schema(
            required=("status", "manifest_path", "execution_scope_id"),
            properties={
                "status": {"type": "string", "enum": ["SUCCESS", "FAILED", "BLOCKED"]},
                "manifest_path": {"type": "string", "minLength": 1},
                "execution_scope_id": {"type": "string", "minLength": 1},
            },
        ),
        capabilities={"category": "parameter", "artifact_manifest": True},
        source_ref=(
            "backend/app/adapters/freqtrade/hyperopt_runner.py#FreqtradeHyperoptRunner"
        ),
        source_sha256="26dbb01ca09151dc6a2b7151d69627d2cd206433207c5c60d9a37863aa3d3a42",
    ),
    InstalledAdapter(
        adapter_key="okx-demo-exchange-v1",
        adapter_kind="EXCHANGE_PROVIDER",
        implementation_version="okx-demo-server-session-v1",
        input_schema_version="okx-demo-session-request-v1",
        output_schema_version="okx-demo-server-session-v1",
        input_schema=_object_schema(
            required=("execution_target", "allow_real_funds", "single_writer_required"),
            properties={
                "execution_target": {"type": "string", "const": "OKX_DEMO"},
                "allow_real_funds": {"type": "boolean", "const": False},
                "single_writer_required": {"type": "boolean", "const": True},
            },
        ),
        output_schema=_object_schema(
            required=("execution_target", "demo_only", "writer_fenced"),
            properties={
                "execution_target": {"type": "string", "const": "OKX_DEMO"},
                "demo_only": {"type": "boolean", "const": True},
                "writer_fenced": {"type": "boolean", "const": True},
            },
        ),
        capabilities={
            "demo_only": True,
            "allow_real_funds": False,
            "single_writer_required": True,
        },
        source_ref=(
            "backend/app/adapters/okx_demo/server_factory.py"
            "#create_okx_demo_server_session"
        ),
        source_sha256="29ae74a1ae2a8bc6d95b397030bab1abd39c69933e773ffdfb962ecacfdf11bf",
    ),
    *tuple(
        InstalledAdapter(
            adapter_key=adapter_key,
            adapter_kind=adapter_kind,
            implementation_version="1.0.0",
            input_schema_version=contract["input_schema_version"],
            output_schema_version=contract["output_schema_version"],
            input_schema=contract["input_schema"],
            output_schema=contract["output_schema"],
            capabilities=capabilities,
            source_ref=(
                "backend/app/services/strategy_platform_builtin_adapters.py#"
                + symbol
            ),
            source_sha256=(
                "68ca433b2b3796b9e8617ac51e4e91ad8f29d3511a0c5852ee1597789e4df9fd"
            ),
        )
        for adapter_key, adapter_kind, symbol, capabilities, contract in (
            (
                "threshold-comparison-v1",
                "QUALITY_EVALUATOR",
                "evaluate_threshold_comparison",
                {"operators": [">", ">=", "<", "<=", "==", "!="], "unknown_fail_closed": True},
                BUILTIN_ADAPTER_JSON_SCHEMAS["threshold-comparison-v1"],
            ),
            (
                "ai-structure-optimization-v1",
                "OPTIMIZER",
                "validate_structure_optimization_trial",
                {"category": "structure", "metadata_only": True, "auto_deploy": False},
                BUILTIN_ADAPTER_JSON_SCHEMAS["ai-structure-optimization-v1"],
            ),
            (
                "docker-runtime-v1",
                "RUNTIME_PROVIDER",
                "build_demo_runtime_launch_spec",
                {"demo_only": True, "allow_real_funds": False, "attestation_unknown_blocks_launch": True},
                BUILTIN_ADAPTER_JSON_SCHEMAS["docker-runtime-v1"],
            ),
            (
                "simulated-runtime-v1",
                "RUNTIME_PROVIDER",
                "initialize_simulated_runtime_metadata",
                {"demo_only": True, "exchange_connection": False, "order_submission": False},
                BUILTIN_ADAPTER_JSON_SCHEMAS["simulated-runtime-v1"],
            ),
            (
                "strategy-import-v1",
                "SUBMISSION_SOURCE",
                "validate_strategy_import_metadata",
                {"metadata_only": True, "contains_secret_material": False, "contains_executable_payload": False},
                BUILTIN_ADAPTER_JSON_SCHEMAS["strategy-import-v1"],
            ),
        )
    ),
)


UNMAPPED_ADAPTERS: tuple[UnmappedAdapter, ...] = (
)


DECLARED_ADAPTER_KEYS: tuple[str, ...] = tuple(
    adapter.adapter_key for adapter in INSTALLED_ADAPTER_MANIFEST
) + tuple(adapter.adapter_key for adapter in UNMAPPED_ADAPTERS)


def canonical_manifest_payload(
    manifest: Sequence[InstalledAdapter] = INSTALLED_ADAPTER_MANIFEST,
) -> list[dict[str, Any]]:
    """Return a stable, JSON-compatible snapshot suitable for audit storage."""

    return [asdict(adapter) for adapter in sorted(manifest, key=lambda item: item.adapter_key)]


def installed_adapter_manifest_digest(
    manifest: Sequence[InstalledAdapter] = INSTALLED_ADAPTER_MANIFEST,
) -> str:
    payload = json.dumps(
        canonical_manifest_payload(manifest),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_installed_adapter_manifest(
    project_root: Path,
    *,
    manifest: Sequence[InstalledAdapter] = INSTALLED_ADAPTER_MANIFEST,
    expected_adapter_keys: Iterable[str] | None = None,
) -> tuple[InstalledAdapter, ...]:
    """Verify manifest identity, schemas, source digests, and AST symbols.

    ``expected_adapter_keys`` is the explicit completeness boundary.  Passing the
    database-declared keys makes any unavailable implementation a hard failure.
    Omitting it validates only the implementations that are actually installed.
    """

    issues: list[str] = []
    try:
        root = project_root.resolve(strict=True)
    except OSError:
        raise AdapterManifestValidationError(("project root does not exist",)) from None
    if not root.is_dir():
        raise AdapterManifestValidationError(("project root is not a directory",))

    keys = [item.adapter_key for item in manifest]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    issues.extend(f"duplicate adapter key: {key}" for key in duplicates)

    if expected_adapter_keys is not None:
        expected = tuple(expected_adapter_keys)
        expected_duplicates = sorted({key for key in expected if expected.count(key) > 1})
        issues.extend(f"duplicate expected adapter key: {key}" for key in expected_duplicates)
        installed = set(keys)
        expected_set = set(expected)
        issues.extend(
            f"missing installed adapter: {key}" for key in sorted(expected_set - installed)
        )
        issues.extend(
            f"undeclared installed adapter: {key}" for key in sorted(installed - expected_set)
        )

    for item in manifest:
        prefix = item.adapter_key or "<empty-adapter-key>"
        if not item.adapter_key.strip():
            issues.append("adapter key is empty")
        if not item.adapter_kind.strip():
            issues.append(f"{prefix}: adapter kind is empty")
        for field_name, value in (
            ("implementation_version", item.implementation_version),
            ("input_schema_version", item.input_schema_version),
            ("output_schema_version", item.output_schema_version),
        ):
            if not value.strip():
                issues.append(f"{prefix}: {field_name} is empty")
        _validate_schema(item.input_schema, f"{prefix}: input schema", issues)
        _validate_schema(item.output_schema, f"{prefix}: output schema", issues)
        _validate_json_mapping(item.capabilities, f"{prefix}: capabilities", issues)
        if not item.capabilities:
            issues.append(f"{prefix}: capabilities are empty")
        _validate_source(root, item, issues)

    if issues:
        raise AdapterManifestValidationError(issues)
    return tuple(sorted(manifest, key=lambda item: item.adapter_key))


def validate_declared_adapter_coverage(project_root: Path) -> tuple[InstalledAdapter, ...]:
    """Fail unless every Task 1 adapter declaration has a verified implementation."""

    return validate_installed_adapter_manifest(
        project_root,
        expected_adapter_keys=DECLARED_ADAPTER_KEYS,
    )


def _validate_json_mapping(
    value: Mapping[str, Any],
    label: str,
    issues: list[str],
) -> None:
    if not isinstance(value, Mapping):
        issues.append(f"{label} is not an object")
        return
    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        issues.append(f"{label} is not canonical JSON")


def _validate_schema(
    schema: Mapping[str, Any],
    label: str,
    issues: list[str],
) -> None:
    _validate_json_mapping(schema, label, issues)
    if not isinstance(schema, Mapping):
        return
    if schema.get("type") != "object":
        issues.append(f"{label} root type must be object")
    _walk_schema(schema, label, issues)


def _walk_schema(value: Any, label: str, issues: list[str]) -> None:
    if isinstance(value, Mapping):
        if value.get("type") == "object":
            properties = value.get("properties")
            required = value.get("required")
            if not isinstance(properties, Mapping) or not properties:
                issues.append(f"{label} contains a generic object schema")
            if value.get("additionalProperties") is not False:
                issues.append(f"{label} object schema must fail closed on unknown fields")
            if not isinstance(required, list) or not required:
                issues.append(f"{label} object schema must declare required fields")
            elif isinstance(properties, Mapping) and any(
                not isinstance(field, str) or field not in properties for field in required
            ):
                issues.append(f"{label} required fields are not declared properties")
        for key, child in value.items():
            _walk_schema(child, f"{label}.{key}", issues)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_schema(child, f"{label}[{index}]", issues)


def _validate_source(
    root: Path,
    item: InstalledAdapter,
    issues: list[str],
) -> None:
    prefix = item.adapter_key or "<empty-adapter-key>"
    if item.source_ref.count("#") != 1:
        issues.append(f"{prefix}: source_ref must be relative/path.py#qualified.symbol")
        return
    raw_path, qualified_symbol = item.source_ref.split("#", 1)
    relative_path = PurePosixPath(raw_path)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or ".." in relative_path.parts
        or relative_path.suffix != ".py"
    ):
        issues.append(f"{prefix}: source path is not a safe relative Python path")
        return
    if not qualified_symbol or any(not part.isidentifier() for part in qualified_symbol.split(".")):
        issues.append(f"{prefix}: source symbol is not a qualified Python identifier")
        return
    candidate = root.joinpath(*relative_path.parts)
    try:
        source_path = candidate.resolve(strict=True)
        source_path.relative_to(root)
    except (OSError, ValueError):
        issues.append(f"{prefix}: source file is missing or outside project root")
        return
    if not source_path.is_file() or candidate.is_symlink():
        issues.append(f"{prefix}: source path is not a regular non-symlink file")
        return
    if _DIGEST_RE.fullmatch(item.source_sha256) is None:
        issues.append(f"{prefix}: source digest is not lowercase SHA-256")
        return
    try:
        content = source_path.read_bytes()
    except OSError:
        issues.append(f"{prefix}: source file cannot be read")
        return
    actual_digest = hashlib.sha256(content).hexdigest()
    if actual_digest != item.source_sha256:
        issues.append(f"{prefix}: source digest drift")
    try:
        tree = ast.parse(content, filename=str(source_path))
    except (SyntaxError, ValueError):
        issues.append(f"{prefix}: source file is not valid Python AST")
        return
    if not _has_qualified_symbol(tree, qualified_symbol):
        issues.append(f"{prefix}: source symbol is missing: {qualified_symbol}")


def _has_qualified_symbol(tree: ast.Module, qualified_symbol: str) -> bool:
    body: Sequence[ast.stmt] = tree.body
    parts = qualified_symbol.split(".")
    for index, part in enumerate(parts):
        node = next(
            (
                candidate
                for candidate in body
                if isinstance(candidate, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and candidate.name == part
            ),
            None,
        )
        if node is None:
            return False
        if index < len(parts) - 1:
            if not isinstance(node, ast.ClassDef):
                return False
            body = node.body
    return True
