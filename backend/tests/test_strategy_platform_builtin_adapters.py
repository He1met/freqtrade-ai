from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

import pytest

from app.services.strategy_platform_builtin_adapters import (
    BUILTIN_ADAPTER_JSON_SCHEMAS,
    BuiltinAdapterValidationError,
    build_demo_runtime_launch_spec,
    evaluate_threshold_comparison,
    initialize_simulated_runtime_metadata,
    validate_strategy_import_metadata,
    validate_structure_optimization_trial,
)


_A = "a" * 64
_B = "b" * 64
_C = "c" * 64


def _structure_trial() -> dict[str, Any]:
    return {
        "trial_id": "trial-7",
        "optimization_run_reference": "optimization-run-3",
        "strategy_version_id": 11,
        "trial_index": 7,
        "input_structure_digest": _A,
        "proposed_structure_digest": _B,
        "objective_snapshot_digest": _C,
        "proposal_reference": "artifact:structure-proposal-7",
        "requested_by": "strategy-platform",
        "execution_requested": False,
        "auto_deploy": False,
        "metadata": {
            "hypothesis": "Reduce entry coupling without changing risk limits.",
            "change_summary": "Metadata-only structure proposal.",
            "parameter_count": 3,
        },
    }


def _docker_request() -> dict[str, Any]:
    return {
        "deployment_id": 2,
        "strategy_version_id": 11,
        "strategy_target_id": 19,
        "configuration_bundle_snapshot_id": 23,
        "runtime_instance_id": "runtime-v13-2",
        "container_name": "freqtrade-demo-v13-2",
        "image_digest": f"sha256:{_A}",
        "config_digest": _B,
        "writer_lease_evidence_digest": _C,
        "cutover_generation": "cutover-v47-1",
        "execution_target": "OKX_DEMO",
        "allow_real_funds": False,
        "single_writer_required": True,
        "credential_attestation": "OUT_OF_SCOPE_UNKNOWN",
    }


def _simulated_request() -> dict[str, Any]:
    return {
        "runtime_instance_id": "simulation-v13-1",
        "strategy_version_id": 11,
        "strategy_target_id": 19,
        "configuration_bundle_snapshot_id": 23,
        "config_digest": _A,
        "simulation_scope_digest": _B,
        "execution_target": "SIMULATED",
        "allow_real_funds": False,
        "single_writer_required": True,
        "exchange_connection": False,
        "order_submission": False,
    }


def _import_request() -> dict[str, Any]:
    return {
        "idempotency_key": "manual-import-20260813-1",
        "strategy_artifact_digest": _A,
        "blueprint_digest": _B,
        "metadata": {
            "display_name": "Imported strategy metadata",
            "description": "Redacted audit metadata; artifact content is not included.",
            "source_label": "operator-controlled-import",
            "redacted": True,
        },
        "reference": {
            "reference_type": "AUDIT_ARTIFACT",
            "reference_id": "artifact:manual-import-20260813-1",
            "immutable_digest": _C,
        },
        "contains_secret_material": False,
        "contains_executable_payload": False,
        "execution_requested": False,
        "auto_deploy": False,
    }


def _assert_every_object_schema_is_closed(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            assert value.get("additionalProperties") is False, path
            assert value.get("properties"), path
            assert value.get("required"), path
        for key, child in value.items():
            _assert_every_object_schema_is_closed(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_every_object_schema_is_closed(child, f"{path}[{index}]")


def _assert_exact_schema_keys(value: dict[str, Any], schema: dict[str, Any]) -> None:
    assert set(value) == set(schema["properties"])
    assert set(value) == set(schema["required"])


def test_all_five_builtin_adapter_contracts_are_closed_json_schemas() -> None:
    assert set(BUILTIN_ADAPTER_JSON_SCHEMAS) == {
        "threshold-comparison-v1",
        "ai-structure-optimization-v1",
        "docker-runtime-v1",
        "simulated-runtime-v1",
        "strategy-import-v1",
    }

    for adapter_key, contract in BUILTIN_ADAPTER_JSON_SCHEMAS.items():
        assert set(contract) == {
            "input_schema_version",
            "output_schema_version",
            "input_schema",
            "output_schema",
        }, adapter_key
        _assert_every_object_schema_is_closed(contract["input_schema"])
        _assert_every_object_schema_is_closed(contract["output_schema"])


@pytest.mark.parametrize(
    ("observed", "threshold", "operator", "matched"),
    [
        (5, 4, ">", True),
        (5.0, 5, ">=", True),
        (4, 5, "<", True),
        (5, 5.0, "<=", True),
        (False, False, "==", True),
        (True, False, "!=", True),
        (1, 2, ">", False),
    ],
)
def test_threshold_comparison_requires_explicit_typed_operator(
    observed: bool | int | float,
    threshold: bool | int | float,
    operator: str,
    matched: bool,
) -> None:
    result = evaluate_threshold_comparison(
        {
            "observed_status": "KNOWN",
            "observed_value": observed,
            "threshold_value": threshold,
            "operator": operator,
        }
    )

    assert result["matched"] is matched
    assert result["status"] == ("PASSED" if matched else "FAILED")
    assert result["reason_code"] == (None if matched else "PREDICATE_NOT_MET")
    _assert_exact_schema_keys(
        result,
        BUILTIN_ADAPTER_JSON_SCHEMAS["threshold-comparison-v1"]["output_schema"],
    )


def test_threshold_unknown_is_explicit_and_fail_closed() -> None:
    result = evaluate_threshold_comparison(
        {
            "observed_status": "UNKNOWN",
            "observed_value": None,
            "threshold_value": 10,
            "operator": ">=",
        }
    )

    assert result["status"] == "UNKNOWN"
    assert result["matched"] is False
    assert result["reason_code"] == "OBSERVATION_UNKNOWN"


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"operator": "contains"}, "UNSUPPORTED_OPERATOR"),
        ({"observed_value": math.nan}, "INVALID_COMPARISON_VALUE"),
        ({"observed_value": True, "threshold_value": 1}, "COMPARISON_DOMAIN_MISMATCH"),
        ({"observed_value": True, "threshold_value": False, "operator": ">"},
         "BOOLEAN_OPERATOR_NOT_ALLOWED"),
        ({"observed_status": "UNKNOWN", "observed_value": 1},
         "UNKNOWN_OBSERVATION_HAS_VALUE"),
    ],
)
def test_threshold_comparison_rejects_ambiguous_or_unsafe_inputs(
    patch: dict[str, Any], code: str
) -> None:
    payload: dict[str, Any] = {
        "observed_status": "KNOWN",
        "observed_value": 2,
        "threshold_value": 1,
        "operator": ">",
    }
    payload.update(patch)

    with pytest.raises(BuiltinAdapterValidationError) as exc_info:
        evaluate_threshold_comparison(payload)

    assert exc_info.value.code == code


def test_threshold_comparison_has_no_implicit_operator_or_unknown_fields() -> None:
    missing_operator = {
        "observed_status": "KNOWN",
        "observed_value": 2,
        "threshold_value": 1,
    }
    with pytest.raises(BuiltinAdapterValidationError) as exc_info:
        evaluate_threshold_comparison(missing_operator)
    assert exc_info.value.code == "MISSING_FIELDS"

    missing_operator["operator"] = ">"
    missing_operator["fallback"] = True
    with pytest.raises(BuiltinAdapterValidationError) as exc_info:
        evaluate_threshold_comparison(missing_operator)
    assert exc_info.value.code == "UNKNOWN_FIELDS"


def test_structure_optimization_only_wraps_validated_trial_metadata() -> None:
    result = validate_structure_optimization_trial(_structure_trial())
    repeated = validate_structure_optimization_trial(_structure_trial())

    assert result == repeated
    assert result["status"] == "VALIDATED_METADATA_ONLY"
    assert result["code_executed"] is False
    assert result["deployment_requested"] is False
    assert result["trial_metadata"]["trial_id"] == "trial-7"
    assert len(result["trial_metadata_digest"]) == 64
    _assert_exact_schema_keys(
        result,
        BUILTIN_ADAPTER_JSON_SCHEMAS["ai-structure-optimization-v1"]["output_schema"],
    )


@pytest.mark.parametrize("field", ["execution_requested", "auto_deploy"])
def test_structure_optimization_rejects_execution_or_auto_deploy(field: str) -> None:
    payload = _structure_trial()
    payload[field] = True

    with pytest.raises(BuiltinAdapterValidationError) as exc_info:
        validate_structure_optimization_trial(payload)

    assert exc_info.value.code == "SAFETY_INVARIANT_VIOLATION"


def test_structure_optimization_rejects_code_or_secret_fields_recursively() -> None:
    for field in ("python_code", "api_key"):
        payload = _structure_trial()
        payload["metadata"][field] = "must-not-be-consumed"
        with pytest.raises(BuiltinAdapterValidationError) as exc_info:
            validate_structure_optimization_trial(payload)
        assert exc_info.value.code == "FORBIDDEN_FIELD"


def test_docker_runtime_builds_blocked_demo_only_spec_without_side_effects() -> None:
    result = build_demo_runtime_launch_spec(_docker_request())
    repeated = build_demo_runtime_launch_spec(_docker_request())

    assert result == repeated
    assert result["status"] == "BLOCKED_ATTESTATION_UNKNOWN"
    assert result["process_started"] is False
    assert result["credential_material_accessed"] is False
    safety = result["launch_specification"]["safety_contract"]
    assert safety == {
        "execution_target": "OKX_DEMO",
        "demo_only": True,
        "allow_real_funds": False,
        "single_writer_required": True,
        "credential_attestation": "OUT_OF_SCOPE_UNKNOWN",
        "launch_authorized": False,
        "blocked_reason": "CREDENTIAL_ATTESTATION_OUT_OF_SCOPE_UNKNOWN",
    }
    assert "command" not in result["launch_specification"]
    assert "environment" not in result["launch_specification"]
    assert len(result["launch_specification_digest"]) == 64
    _assert_exact_schema_keys(
        result,
        BUILTIN_ADAPTER_JSON_SCHEMAS["docker-runtime-v1"]["output_schema"],
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("execution_target", "OKX_LIVE", "EXECUTION_TARGET_NOT_DEMO"),
        ("allow_real_funds", True, "SAFETY_INVARIANT_VIOLATION"),
        ("single_writer_required", False, "SAFETY_INVARIANT_VIOLATION"),
        ("credential_attestation", "VERIFIED", "CREDENTIAL_ATTESTATION_NOT_OUT_OF_SCOPE"),
        ("image_digest", "freqtrade:latest", "MUTABLE_IMAGE_REFERENCE"),
    ],
)
def test_docker_runtime_rejects_weakened_safety_contract(
    field: str, value: Any, code: str
) -> None:
    payload = _docker_request()
    payload[field] = value

    with pytest.raises(BuiltinAdapterValidationError) as exc_info:
        build_demo_runtime_launch_spec(payload)

    assert exc_info.value.code == code


def test_docker_runtime_rejects_command_credentials_and_unknown_fields() -> None:
    for field in ("command", "credentials"):
        payload = _docker_request()
        payload[field] = "must-not-be-consumed"
        with pytest.raises(BuiltinAdapterValidationError) as exc_info:
            build_demo_runtime_launch_spec(payload)
        assert exc_info.value.code == "FORBIDDEN_FIELD"

    payload = _docker_request()
    payload["restart_policy"] = "always"
    with pytest.raises(BuiltinAdapterValidationError) as exc_info:
        build_demo_runtime_launch_spec(payload)
    assert exc_info.value.code == "UNKNOWN_FIELDS"


def test_simulated_runtime_is_metadata_only_and_has_no_exchange_or_orders() -> None:
    result = initialize_simulated_runtime_metadata(_simulated_request())
    repeated = initialize_simulated_runtime_metadata(_simulated_request())

    assert result == repeated
    assert result["status"] == "VALIDATED_METADATA_ONLY"
    assert result["runtime_state"]["state"] == "CREATED"
    assert result["runtime_state"]["exchange_connected"] is False
    assert result["runtime_state"]["orders_submitted"] == 0
    assert result["runtime_state"]["persistent_side_effects"] is False
    assert len(result["runtime_state_digest"]) == 64
    _assert_exact_schema_keys(
        result,
        BUILTIN_ADAPTER_JSON_SCHEMAS["simulated-runtime-v1"]["output_schema"],
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_target", "OKX_DEMO"),
        ("allow_real_funds", True),
        ("single_writer_required", False),
        ("exchange_connection", True),
        ("order_submission", True),
    ],
)
def test_simulated_runtime_rejects_external_or_unsafe_behavior(field: str, value: Any) -> None:
    payload = _simulated_request()
    payload[field] = value

    with pytest.raises(BuiltinAdapterValidationError):
        initialize_simulated_runtime_metadata(payload)


def test_strategy_import_returns_only_redacted_metadata_digests_and_reference() -> None:
    result = validate_strategy_import_metadata(_import_request())
    repeated = validate_strategy_import_metadata(_import_request())

    assert result == repeated
    assert result["status"] == "VALIDATED_METADATA_ONLY"
    assert result["metadata"]["redacted"] is True
    assert result["strategy_artifact_digest"] == _A
    assert result["blueprint_digest"] == _B
    assert result["reference"]["immutable_digest"] == _C
    assert result["execution_performed"] is False
    assert result["deployment_requested"] is False
    assert len(result["metadata_digest"]) == 64
    _assert_exact_schema_keys(
        result,
        BUILTIN_ADAPTER_JSON_SCHEMAS["strategy-import-v1"]["output_schema"],
    )


@pytest.mark.parametrize(
    "field",
    [
        "contains_secret_material",
        "contains_executable_payload",
        "execution_requested",
        "auto_deploy",
    ],
)
def test_strategy_import_rejects_unsafe_claims(field: str) -> None:
    payload = _import_request()
    payload[field] = True

    with pytest.raises(BuiltinAdapterValidationError) as exc_info:
        validate_strategy_import_metadata(payload)

    assert exc_info.value.code == "SAFETY_INVARIANT_VIOLATION"


def test_strategy_import_rejects_code_secrets_and_non_redacted_metadata() -> None:
    for field in ("python_code", "api_key"):
        payload = _import_request()
        payload["metadata"][field] = "must-not-be-consumed"
        with pytest.raises(BuiltinAdapterValidationError) as exc_info:
            validate_strategy_import_metadata(payload)
        assert exc_info.value.code == "FORBIDDEN_FIELD"

    payload = _import_request()
    payload["metadata"]["redacted"] = False
    with pytest.raises(BuiltinAdapterValidationError) as exc_info:
        validate_strategy_import_metadata(payload)
    assert exc_info.value.code == "SAFETY_INVARIANT_VIOLATION"


def test_all_adapters_reject_unknown_top_level_fields() -> None:
    cases = (
        (validate_structure_optimization_trial, _structure_trial()),
        (build_demo_runtime_launch_spec, _docker_request()),
        (initialize_simulated_runtime_metadata, _simulated_request()),
        (validate_strategy_import_metadata, _import_request()),
    )
    for adapter, original in cases:
        payload = deepcopy(original)
        payload["unexpected"] = True
        with pytest.raises(BuiltinAdapterValidationError) as exc_info:
            adapter(payload)
        assert exc_info.value.code == "UNKNOWN_FIELDS"
