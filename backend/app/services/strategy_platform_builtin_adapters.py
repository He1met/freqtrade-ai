"""Pure, fail-closed built-in adapters for Strategy Platform V1.3.

These adapters validate and normalize metadata only.  They do not import or
execute strategy code, read credentials, connect to an exchange, start a
process, submit an order, approve a strategy, or deploy a runtime.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import re
from typing import Any


_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RUNTIME_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_ALLOWED_COMPARISON_OPERATORS = frozenset({">", ">=", "<", "<=", "==", "!="})
_FORBIDDEN_FIELDS = frozenset(
    {
        "api_key",
        "api_secret",
        "access_token",
        "password",
        "passphrase",
        "private_key",
        "secret",
        "secret_value",
        "credentials",
        "python_code",
        "source_code",
        "callable_source",
        "executable_code",
        "shell_command",
        "command",
        "argv",
        "environment",
        "env",
    }
)


class BuiltinAdapterValidationError(ValueError):
    """A built-in adapter input cannot be accepted safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _closed_object_schema(
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


_DIGEST_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_NONEMPTY_STRING_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 512}


THRESHOLD_COMPARISON_INPUT_SCHEMA = _closed_object_schema(
    required=("observed_status", "observed_value", "threshold_value", "operator"),
    properties={
        "observed_status": {"type": "string", "enum": ["KNOWN", "UNKNOWN"]},
        "observed_value": {"type": ["number", "boolean", "null"]},
        "threshold_value": {"type": ["number", "boolean"]},
        "operator": {
            "type": "string",
            "enum": sorted(_ALLOWED_COMPARISON_OPERATORS),
        },
    },
)
THRESHOLD_COMPARISON_OUTPUT_SCHEMA = _closed_object_schema(
    required=(
        "adapter_key",
        "contract_version",
        "status",
        "matched",
        "reason_code",
        "operator",
    ),
    properties={
        "adapter_key": {"type": "string", "const": "threshold-comparison-v1"},
        "contract_version": {
            "type": "string",
            "const": "threshold-comparison-result-v1",
        },
        "status": {"type": "string", "enum": ["PASSED", "FAILED", "UNKNOWN"]},
        "matched": {"type": "boolean"},
        "reason_code": {"type": ["string", "null"]},
        "operator": {
            "type": "string",
            "enum": sorted(_ALLOWED_COMPARISON_OPERATORS),
        },
    },
)


_STRUCTURE_TRIAL_METADATA_SCHEMA = _closed_object_schema(
    required=("hypothesis", "change_summary", "parameter_count"),
    properties={
        "hypothesis": _NONEMPTY_STRING_SCHEMA,
        "change_summary": _NONEMPTY_STRING_SCHEMA,
        "parameter_count": {"type": "integer", "minimum": 0},
    },
)
STRUCTURE_OPTIMIZATION_INPUT_SCHEMA = _closed_object_schema(
    required=(
        "trial_id",
        "optimization_run_reference",
        "strategy_version_id",
        "trial_index",
        "input_structure_digest",
        "proposed_structure_digest",
        "objective_snapshot_digest",
        "proposal_reference",
        "requested_by",
        "execution_requested",
        "auto_deploy",
        "metadata",
    ),
    properties={
        "trial_id": _NONEMPTY_STRING_SCHEMA,
        "optimization_run_reference": _NONEMPTY_STRING_SCHEMA,
        "strategy_version_id": {"type": "integer", "minimum": 1},
        "trial_index": {"type": "integer", "minimum": 0},
        "input_structure_digest": _DIGEST_SCHEMA,
        "proposed_structure_digest": _DIGEST_SCHEMA,
        "objective_snapshot_digest": _DIGEST_SCHEMA,
        "proposal_reference": _NONEMPTY_STRING_SCHEMA,
        "requested_by": _NONEMPTY_STRING_SCHEMA,
        "execution_requested": {"type": "boolean", "const": False},
        "auto_deploy": {"type": "boolean", "const": False},
        "metadata": _STRUCTURE_TRIAL_METADATA_SCHEMA,
    },
)
_NORMALIZED_STRUCTURE_TRIAL_SCHEMA = _closed_object_schema(
    required=(
        "trial_id",
        "optimization_run_reference",
        "strategy_version_id",
        "trial_index",
        "input_structure_digest",
        "proposed_structure_digest",
        "objective_snapshot_digest",
        "proposal_reference",
        "requested_by",
        "hypothesis",
        "change_summary",
        "parameter_count",
    ),
    properties={
        "trial_id": _NONEMPTY_STRING_SCHEMA,
        "optimization_run_reference": _NONEMPTY_STRING_SCHEMA,
        "strategy_version_id": {"type": "integer", "minimum": 1},
        "trial_index": {"type": "integer", "minimum": 0},
        "input_structure_digest": _DIGEST_SCHEMA,
        "proposed_structure_digest": _DIGEST_SCHEMA,
        "objective_snapshot_digest": _DIGEST_SCHEMA,
        "proposal_reference": _NONEMPTY_STRING_SCHEMA,
        "requested_by": _NONEMPTY_STRING_SCHEMA,
        "hypothesis": _NONEMPTY_STRING_SCHEMA,
        "change_summary": _NONEMPTY_STRING_SCHEMA,
        "parameter_count": {"type": "integer", "minimum": 0},
    },
)
STRUCTURE_OPTIMIZATION_OUTPUT_SCHEMA = _closed_object_schema(
    required=(
        "adapter_key",
        "contract_version",
        "status",
        "trial_metadata",
        "trial_metadata_digest",
        "code_executed",
        "deployment_requested",
    ),
    properties={
        "adapter_key": {
            "type": "string",
            "const": "ai-structure-optimization-v1",
        },
        "contract_version": {
            "type": "string",
            "const": "structure-optimization-trial-envelope-v1",
        },
        "status": {"type": "string", "const": "VALIDATED_METADATA_ONLY"},
        "trial_metadata": _NORMALIZED_STRUCTURE_TRIAL_SCHEMA,
        "trial_metadata_digest": _DIGEST_SCHEMA,
        "code_executed": {"type": "boolean", "const": False},
        "deployment_requested": {"type": "boolean", "const": False},
    },
)


DOCKER_RUNTIME_INPUT_SCHEMA = _closed_object_schema(
    required=(
        "deployment_id",
        "strategy_version_id",
        "strategy_target_id",
        "configuration_bundle_snapshot_id",
        "runtime_instance_id",
        "container_name",
        "image_digest",
        "config_digest",
        "writer_lease_evidence_digest",
        "cutover_generation",
        "execution_target",
        "allow_real_funds",
        "single_writer_required",
        "credential_attestation",
    ),
    properties={
        "deployment_id": {"type": "integer", "minimum": 1},
        "strategy_version_id": {"type": "integer", "minimum": 1},
        "strategy_target_id": {"type": "integer", "minimum": 1},
        "configuration_bundle_snapshot_id": {"type": "integer", "minimum": 1},
        "runtime_instance_id": {
            "type": "string",
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        },
        "container_name": {
            "type": "string",
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        },
        "image_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "config_digest": _DIGEST_SCHEMA,
        "writer_lease_evidence_digest": _DIGEST_SCHEMA,
        "cutover_generation": _NONEMPTY_STRING_SCHEMA,
        "execution_target": {"type": "string", "const": "OKX_DEMO"},
        "allow_real_funds": {"type": "boolean", "const": False},
        "single_writer_required": {"type": "boolean", "const": True},
        "credential_attestation": {
            "type": "string",
            "const": "OUT_OF_SCOPE_UNKNOWN",
        },
    },
)
_DOCKER_LAUNCH_SAFETY_SCHEMA = _closed_object_schema(
    required=(
        "execution_target",
        "demo_only",
        "allow_real_funds",
        "single_writer_required",
        "credential_attestation",
        "launch_authorized",
        "blocked_reason",
    ),
    properties={
        "execution_target": {"type": "string", "const": "OKX_DEMO"},
        "demo_only": {"type": "boolean", "const": True},
        "allow_real_funds": {"type": "boolean", "const": False},
        "single_writer_required": {"type": "boolean", "const": True},
        "credential_attestation": {
            "type": "string",
            "const": "OUT_OF_SCOPE_UNKNOWN",
        },
        "launch_authorized": {"type": "boolean", "const": False},
        "blocked_reason": {
            "type": "string",
            "const": "CREDENTIAL_ATTESTATION_OUT_OF_SCOPE_UNKNOWN",
        },
    },
)
_DOCKER_LAUNCH_SPEC_SCHEMA = _closed_object_schema(
    required=(
        "deployment_id",
        "strategy_version_id",
        "strategy_target_id",
        "configuration_bundle_snapshot_id",
        "runtime_instance_id",
        "container_name",
        "image_digest",
        "config_digest",
        "writer_lease_evidence_digest",
        "cutover_generation",
        "safety_contract",
    ),
    properties={
        "deployment_id": {"type": "integer", "minimum": 1},
        "strategy_version_id": {"type": "integer", "minimum": 1},
        "strategy_target_id": {"type": "integer", "minimum": 1},
        "configuration_bundle_snapshot_id": {"type": "integer", "minimum": 1},
        "runtime_instance_id": {
            "type": "string",
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        },
        "container_name": {
            "type": "string",
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        },
        "image_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "config_digest": _DIGEST_SCHEMA,
        "writer_lease_evidence_digest": _DIGEST_SCHEMA,
        "cutover_generation": _NONEMPTY_STRING_SCHEMA,
        "safety_contract": _DOCKER_LAUNCH_SAFETY_SCHEMA,
    },
)
DOCKER_RUNTIME_OUTPUT_SCHEMA = _closed_object_schema(
    required=(
        "adapter_key",
        "contract_version",
        "status",
        "launch_specification",
        "launch_specification_digest",
        "process_started",
        "credential_material_accessed",
    ),
    properties={
        "adapter_key": {"type": "string", "const": "docker-runtime-v1"},
        "contract_version": {
            "type": "string",
            "const": "controlled-demo-runtime-launch-spec-v1",
        },
        "status": {"type": "string", "const": "BLOCKED_ATTESTATION_UNKNOWN"},
        "launch_specification": _DOCKER_LAUNCH_SPEC_SCHEMA,
        "launch_specification_digest": _DIGEST_SCHEMA,
        "process_started": {"type": "boolean", "const": False},
        "credential_material_accessed": {"type": "boolean", "const": False},
    },
)


SIMULATED_RUNTIME_INPUT_SCHEMA = _closed_object_schema(
    required=(
        "runtime_instance_id",
        "strategy_version_id",
        "strategy_target_id",
        "configuration_bundle_snapshot_id",
        "config_digest",
        "simulation_scope_digest",
        "execution_target",
        "allow_real_funds",
        "single_writer_required",
        "exchange_connection",
        "order_submission",
    ),
    properties={
        "runtime_instance_id": {
            "type": "string",
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        },
        "strategy_version_id": {"type": "integer", "minimum": 1},
        "strategy_target_id": {"type": "integer", "minimum": 1},
        "configuration_bundle_snapshot_id": {"type": "integer", "minimum": 1},
        "config_digest": _DIGEST_SCHEMA,
        "simulation_scope_digest": _DIGEST_SCHEMA,
        "execution_target": {"type": "string", "const": "SIMULATED"},
        "allow_real_funds": {"type": "boolean", "const": False},
        "single_writer_required": {"type": "boolean", "const": True},
        "exchange_connection": {"type": "boolean", "const": False},
        "order_submission": {"type": "boolean", "const": False},
    },
)
_SIMULATED_RUNTIME_STATE_SCHEMA = _closed_object_schema(
    required=(
        "runtime_instance_id",
        "strategy_version_id",
        "strategy_target_id",
        "configuration_bundle_snapshot_id",
        "config_digest",
        "simulation_scope_digest",
        "state",
        "exchange_connected",
        "orders_submitted",
        "persistent_side_effects",
    ),
    properties={
        "runtime_instance_id": {
            "type": "string",
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        },
        "strategy_version_id": {"type": "integer", "minimum": 1},
        "strategy_target_id": {"type": "integer", "minimum": 1},
        "configuration_bundle_snapshot_id": {"type": "integer", "minimum": 1},
        "config_digest": _DIGEST_SCHEMA,
        "simulation_scope_digest": _DIGEST_SCHEMA,
        "state": {"type": "string", "const": "CREATED"},
        "exchange_connected": {"type": "boolean", "const": False},
        "orders_submitted": {"type": "integer", "const": 0},
        "persistent_side_effects": {"type": "boolean", "const": False},
    },
)
SIMULATED_RUNTIME_OUTPUT_SCHEMA = _closed_object_schema(
    required=(
        "adapter_key",
        "contract_version",
        "status",
        "runtime_state",
        "runtime_state_digest",
    ),
    properties={
        "adapter_key": {"type": "string", "const": "simulated-runtime-v1"},
        "contract_version": {
            "type": "string",
            "const": "simulated-runtime-metadata-v1",
        },
        "status": {"type": "string", "const": "VALIDATED_METADATA_ONLY"},
        "runtime_state": _SIMULATED_RUNTIME_STATE_SCHEMA,
        "runtime_state_digest": _DIGEST_SCHEMA,
    },
)


_IMPORT_METADATA_SCHEMA = _closed_object_schema(
    required=("display_name", "description", "source_label", "redacted"),
    properties={
        "display_name": {"type": "string", "minLength": 1, "maxLength": 200},
        "description": {"type": "string", "minLength": 1, "maxLength": 2000},
        "source_label": {"type": "string", "minLength": 1, "maxLength": 200},
        "redacted": {"type": "boolean", "const": True},
    },
)
_IMPORT_REFERENCE_SCHEMA = _closed_object_schema(
    required=("reference_type", "reference_id", "immutable_digest"),
    properties={
        "reference_type": {
            "type": "string",
            "enum": ["AUDIT_ARTIFACT", "EXTERNAL_CATALOG", "MANUAL_METADATA"],
        },
        "reference_id": _NONEMPTY_STRING_SCHEMA,
        "immutable_digest": _DIGEST_SCHEMA,
    },
)
STRATEGY_IMPORT_INPUT_SCHEMA = _closed_object_schema(
    required=(
        "idempotency_key",
        "strategy_artifact_digest",
        "blueprint_digest",
        "metadata",
        "reference",
        "contains_secret_material",
        "contains_executable_payload",
        "execution_requested",
        "auto_deploy",
    ),
    properties={
        "idempotency_key": _NONEMPTY_STRING_SCHEMA,
        "strategy_artifact_digest": _DIGEST_SCHEMA,
        "blueprint_digest": _DIGEST_SCHEMA,
        "metadata": _IMPORT_METADATA_SCHEMA,
        "reference": _IMPORT_REFERENCE_SCHEMA,
        "contains_secret_material": {"type": "boolean", "const": False},
        "contains_executable_payload": {"type": "boolean", "const": False},
        "execution_requested": {"type": "boolean", "const": False},
        "auto_deploy": {"type": "boolean", "const": False},
    },
)
STRATEGY_IMPORT_OUTPUT_SCHEMA = _closed_object_schema(
    required=(
        "adapter_key",
        "contract_version",
        "status",
        "idempotency_key",
        "strategy_artifact_digest",
        "blueprint_digest",
        "metadata",
        "metadata_digest",
        "reference",
        "execution_performed",
        "deployment_requested",
    ),
    properties={
        "adapter_key": {"type": "string", "const": "strategy-import-v1"},
        "contract_version": {
            "type": "string",
            "const": "strategy-import-metadata-envelope-v1",
        },
        "status": {"type": "string", "const": "VALIDATED_METADATA_ONLY"},
        "idempotency_key": _NONEMPTY_STRING_SCHEMA,
        "strategy_artifact_digest": _DIGEST_SCHEMA,
        "blueprint_digest": _DIGEST_SCHEMA,
        "metadata": _IMPORT_METADATA_SCHEMA,
        "metadata_digest": _DIGEST_SCHEMA,
        "reference": _IMPORT_REFERENCE_SCHEMA,
        "execution_performed": {"type": "boolean", "const": False},
        "deployment_requested": {"type": "boolean", "const": False},
    },
)


BUILTIN_ADAPTER_JSON_SCHEMAS: dict[str, dict[str, Any]] = {
    "threshold-comparison-v1": {
        "input_schema_version": "threshold-comparison-input-v1",
        "output_schema_version": "threshold-comparison-result-v1",
        "input_schema": THRESHOLD_COMPARISON_INPUT_SCHEMA,
        "output_schema": THRESHOLD_COMPARISON_OUTPUT_SCHEMA,
    },
    "ai-structure-optimization-v1": {
        "input_schema_version": "structure-optimization-trial-metadata-v1",
        "output_schema_version": "structure-optimization-trial-envelope-v1",
        "input_schema": STRUCTURE_OPTIMIZATION_INPUT_SCHEMA,
        "output_schema": STRUCTURE_OPTIMIZATION_OUTPUT_SCHEMA,
    },
    "docker-runtime-v1": {
        "input_schema_version": "controlled-demo-runtime-launch-request-v1",
        "output_schema_version": "controlled-demo-runtime-launch-spec-v1",
        "input_schema": DOCKER_RUNTIME_INPUT_SCHEMA,
        "output_schema": DOCKER_RUNTIME_OUTPUT_SCHEMA,
    },
    "simulated-runtime-v1": {
        "input_schema_version": "simulated-runtime-metadata-request-v1",
        "output_schema_version": "simulated-runtime-metadata-v1",
        "input_schema": SIMULATED_RUNTIME_INPUT_SCHEMA,
        "output_schema": SIMULATED_RUNTIME_OUTPUT_SCHEMA,
    },
    "strategy-import-v1": {
        "input_schema_version": "strategy-import-metadata-request-v1",
        "output_schema_version": "strategy-import-metadata-envelope-v1",
        "input_schema": STRATEGY_IMPORT_INPUT_SCHEMA,
        "output_schema": STRATEGY_IMPORT_OUTPUT_SCHEMA,
    },
}


def evaluate_threshold_comparison(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one explicit numeric or boolean comparison.

    An unknown observation is represented explicitly and always returns a
    non-passing result.  Missing status or operands are never inferred.
    """

    required = {
        "observed_status",
        "observed_value",
        "threshold_value",
        "operator",
    }
    _validate_payload_shape(payload, required=required, label="threshold comparison")
    operator = _require_string(payload["operator"], "operator")
    if operator not in _ALLOWED_COMPARISON_OPERATORS:
        _raise("UNSUPPORTED_OPERATOR", f"operator {operator!r} is not supported")

    status = _require_string(payload["observed_status"], "observed_status")
    threshold = _require_comparable_value(payload["threshold_value"], "threshold_value")
    observed = payload["observed_value"]
    if status == "UNKNOWN":
        if observed is not None:
            _raise(
                "UNKNOWN_OBSERVATION_HAS_VALUE",
                "UNKNOWN observation must not carry an observed value",
            )
        return {
            "adapter_key": "threshold-comparison-v1",
            "contract_version": "threshold-comparison-result-v1",
            "status": "UNKNOWN",
            "matched": False,
            "reason_code": "OBSERVATION_UNKNOWN",
            "operator": operator,
        }
    if status != "KNOWN":
        _raise("INVALID_OBSERVED_STATUS", "observed_status must be KNOWN or UNKNOWN")
    observed = _require_comparable_value(observed, "observed_value")
    _require_same_comparison_domain(observed, threshold)
    if isinstance(observed, bool) and operator not in {"==", "!="}:
        _raise(
            "BOOLEAN_OPERATOR_NOT_ALLOWED",
            "boolean comparisons only support == and !=",
        )

    matched = _compare(observed, threshold, operator)
    return {
        "adapter_key": "threshold-comparison-v1",
        "contract_version": "threshold-comparison-result-v1",
        "status": "PASSED" if matched else "FAILED",
        "matched": matched,
        "reason_code": None if matched else "PREDICATE_NOT_MET",
        "operator": operator,
    }


def validate_structure_optimization_trial(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and envelope a structure-optimization trial without running it."""

    required = {
        "trial_id",
        "optimization_run_reference",
        "strategy_version_id",
        "trial_index",
        "input_structure_digest",
        "proposed_structure_digest",
        "objective_snapshot_digest",
        "proposal_reference",
        "requested_by",
        "execution_requested",
        "auto_deploy",
        "metadata",
    }
    _validate_payload_shape(payload, required=required, label="structure optimization trial")
    _require_false(payload["execution_requested"], "execution_requested")
    _require_false(payload["auto_deploy"], "auto_deploy")
    metadata = payload["metadata"]
    _validate_payload_shape(
        metadata,
        required={"hypothesis", "change_summary", "parameter_count"},
        label="structure optimization metadata",
    )

    trial_metadata = {
        "trial_id": _require_string(payload["trial_id"], "trial_id"),
        "optimization_run_reference": _require_string(
            payload["optimization_run_reference"], "optimization_run_reference"
        ),
        "strategy_version_id": _require_positive_int(
            payload["strategy_version_id"], "strategy_version_id"
        ),
        "trial_index": _require_nonnegative_int(payload["trial_index"], "trial_index"),
        "input_structure_digest": _require_digest(
            payload["input_structure_digest"], "input_structure_digest"
        ),
        "proposed_structure_digest": _require_digest(
            payload["proposed_structure_digest"], "proposed_structure_digest"
        ),
        "objective_snapshot_digest": _require_digest(
            payload["objective_snapshot_digest"], "objective_snapshot_digest"
        ),
        "proposal_reference": _require_string(
            payload["proposal_reference"], "proposal_reference"
        ),
        "requested_by": _require_string(payload["requested_by"], "requested_by"),
        "hypothesis": _require_string(metadata["hypothesis"], "metadata.hypothesis"),
        "change_summary": _require_string(
            metadata["change_summary"], "metadata.change_summary"
        ),
        "parameter_count": _require_nonnegative_int(
            metadata["parameter_count"], "metadata.parameter_count"
        ),
    }
    return {
        "adapter_key": "ai-structure-optimization-v1",
        "contract_version": "structure-optimization-trial-envelope-v1",
        "status": "VALIDATED_METADATA_ONLY",
        "trial_metadata": trial_metadata,
        "trial_metadata_digest": _canonical_digest(trial_metadata),
        "code_executed": False,
        "deployment_requested": False,
    }


def build_demo_runtime_launch_spec(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build a non-executable Demo-only runtime specification.

    Credential attestation is intentionally outside this adapter.  Therefore a
    valid specification remains blocked and cannot itself authorize a launch.
    """

    required = {
        "deployment_id",
        "strategy_version_id",
        "strategy_target_id",
        "configuration_bundle_snapshot_id",
        "runtime_instance_id",
        "container_name",
        "image_digest",
        "config_digest",
        "writer_lease_evidence_digest",
        "cutover_generation",
        "execution_target",
        "allow_real_funds",
        "single_writer_required",
        "credential_attestation",
    }
    _validate_payload_shape(payload, required=required, label="Docker runtime launch request")
    if payload["execution_target"] != "OKX_DEMO":
        _raise("EXECUTION_TARGET_NOT_DEMO", "execution_target must be OKX_DEMO")
    _require_false(payload["allow_real_funds"], "allow_real_funds")
    _require_true(payload["single_writer_required"], "single_writer_required")
    if payload["credential_attestation"] != "OUT_OF_SCOPE_UNKNOWN":
        _raise(
            "CREDENTIAL_ATTESTATION_NOT_OUT_OF_SCOPE",
            "this metadata adapter only accepts OUT_OF_SCOPE_UNKNOWN attestation",
        )

    image_digest = _require_string(payload["image_digest"], "image_digest")
    if _IMAGE_DIGEST_RE.fullmatch(image_digest) is None:
        _raise("MUTABLE_IMAGE_REFERENCE", "image_digest must be an immutable SHA-256 digest")
    launch_specification = {
        "deployment_id": _require_positive_int(payload["deployment_id"], "deployment_id"),
        "strategy_version_id": _require_positive_int(
            payload["strategy_version_id"], "strategy_version_id"
        ),
        "strategy_target_id": _require_positive_int(
            payload["strategy_target_id"], "strategy_target_id"
        ),
        "configuration_bundle_snapshot_id": _require_positive_int(
            payload["configuration_bundle_snapshot_id"],
            "configuration_bundle_snapshot_id",
        ),
        "runtime_instance_id": _require_runtime_identifier(
            payload["runtime_instance_id"], "runtime_instance_id"
        ),
        "container_name": _require_runtime_identifier(
            payload["container_name"], "container_name"
        ),
        "image_digest": image_digest,
        "config_digest": _require_digest(payload["config_digest"], "config_digest"),
        "writer_lease_evidence_digest": _require_digest(
            payload["writer_lease_evidence_digest"], "writer_lease_evidence_digest"
        ),
        "cutover_generation": _require_string(
            payload["cutover_generation"], "cutover_generation"
        ),
        "safety_contract": {
            "execution_target": "OKX_DEMO",
            "demo_only": True,
            "allow_real_funds": False,
            "single_writer_required": True,
            "credential_attestation": "OUT_OF_SCOPE_UNKNOWN",
            "launch_authorized": False,
            "blocked_reason": "CREDENTIAL_ATTESTATION_OUT_OF_SCOPE_UNKNOWN",
        },
    }
    return {
        "adapter_key": "docker-runtime-v1",
        "contract_version": "controlled-demo-runtime-launch-spec-v1",
        "status": "BLOCKED_ATTESTATION_UNKNOWN",
        "launch_specification": launch_specification,
        "launch_specification_digest": _canonical_digest(launch_specification),
        "process_started": False,
        "credential_material_accessed": False,
    }


def initialize_simulated_runtime_metadata(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Create a pure in-memory simulated-runtime metadata snapshot."""

    required = {
        "runtime_instance_id",
        "strategy_version_id",
        "strategy_target_id",
        "configuration_bundle_snapshot_id",
        "config_digest",
        "simulation_scope_digest",
        "execution_target",
        "allow_real_funds",
        "single_writer_required",
        "exchange_connection",
        "order_submission",
    }
    _validate_payload_shape(payload, required=required, label="simulated runtime request")
    if payload["execution_target"] != "SIMULATED":
        _raise("INVALID_SIMULATION_TARGET", "execution_target must be SIMULATED")
    _require_false(payload["allow_real_funds"], "allow_real_funds")
    _require_true(payload["single_writer_required"], "single_writer_required")
    _require_false(payload["exchange_connection"], "exchange_connection")
    _require_false(payload["order_submission"], "order_submission")
    runtime_state = {
        "runtime_instance_id": _require_runtime_identifier(
            payload["runtime_instance_id"], "runtime_instance_id"
        ),
        "strategy_version_id": _require_positive_int(
            payload["strategy_version_id"], "strategy_version_id"
        ),
        "strategy_target_id": _require_positive_int(
            payload["strategy_target_id"], "strategy_target_id"
        ),
        "configuration_bundle_snapshot_id": _require_positive_int(
            payload["configuration_bundle_snapshot_id"],
            "configuration_bundle_snapshot_id",
        ),
        "config_digest": _require_digest(payload["config_digest"], "config_digest"),
        "simulation_scope_digest": _require_digest(
            payload["simulation_scope_digest"], "simulation_scope_digest"
        ),
        "state": "CREATED",
        "exchange_connected": False,
        "orders_submitted": 0,
        "persistent_side_effects": False,
    }
    return {
        "adapter_key": "simulated-runtime-v1",
        "contract_version": "simulated-runtime-metadata-v1",
        "status": "VALIDATED_METADATA_ONLY",
        "runtime_state": runtime_state,
        "runtime_state_digest": _canonical_digest(runtime_state),
    }


def validate_strategy_import_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate redacted strategy-import metadata without loading an artifact."""

    required = {
        "idempotency_key",
        "strategy_artifact_digest",
        "blueprint_digest",
        "metadata",
        "reference",
        "contains_secret_material",
        "contains_executable_payload",
        "execution_requested",
        "auto_deploy",
    }
    _validate_payload_shape(payload, required=required, label="strategy import request")
    _require_false(payload["contains_secret_material"], "contains_secret_material")
    _require_false(payload["contains_executable_payload"], "contains_executable_payload")
    _require_false(payload["execution_requested"], "execution_requested")
    _require_false(payload["auto_deploy"], "auto_deploy")

    metadata = payload["metadata"]
    _validate_payload_shape(
        metadata,
        required={"display_name", "description", "source_label", "redacted"},
        label="strategy import metadata",
    )
    _require_true(metadata["redacted"], "metadata.redacted")
    safe_metadata = {
        "display_name": _require_string(metadata["display_name"], "metadata.display_name"),
        "description": _require_string(metadata["description"], "metadata.description"),
        "source_label": _require_string(metadata["source_label"], "metadata.source_label"),
        "redacted": True,
    }

    reference = payload["reference"]
    _validate_payload_shape(
        reference,
        required={"reference_type", "reference_id", "immutable_digest"},
        label="strategy import reference",
    )
    reference_type = _require_string(reference["reference_type"], "reference.reference_type")
    if reference_type not in {"AUDIT_ARTIFACT", "EXTERNAL_CATALOG", "MANUAL_METADATA"}:
        _raise("INVALID_REFERENCE_TYPE", "reference_type is not supported")
    safe_reference = {
        "reference_type": reference_type,
        "reference_id": _require_string(reference["reference_id"], "reference.reference_id"),
        "immutable_digest": _require_digest(
            reference["immutable_digest"], "reference.immutable_digest"
        ),
    }
    return {
        "adapter_key": "strategy-import-v1",
        "contract_version": "strategy-import-metadata-envelope-v1",
        "status": "VALIDATED_METADATA_ONLY",
        "idempotency_key": _require_string(payload["idempotency_key"], "idempotency_key"),
        "strategy_artifact_digest": _require_digest(
            payload["strategy_artifact_digest"], "strategy_artifact_digest"
        ),
        "blueprint_digest": _require_digest(payload["blueprint_digest"], "blueprint_digest"),
        "metadata": safe_metadata,
        "metadata_digest": _canonical_digest(safe_metadata),
        "reference": safe_reference,
        "execution_performed": False,
        "deployment_requested": False,
    }


def _validate_payload_shape(
    payload: Any,
    *,
    required: set[str],
    label: str,
) -> None:
    if not isinstance(payload, Mapping):
        _raise("PAYLOAD_NOT_OBJECT", f"{label} must be an object")
    _reject_forbidden_fields(payload)
    actual = set(payload)
    if not all(isinstance(key, str) for key in actual):
        _raise("NON_STRING_FIELD", f"{label} field names must be strings")
    missing = sorted(required - actual)
    unknown = sorted(actual - required)
    if missing:
        _raise("MISSING_FIELDS", f"{label} is missing fields: {', '.join(missing)}")
    if unknown:
        _raise("UNKNOWN_FIELDS", f"{label} has unknown fields: {', '.join(unknown)}")


def _reject_forbidden_fields(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_FIELDS:
                _raise("FORBIDDEN_FIELD", f"forbidden field at {path}.{key}")
            _reject_forbidden_fields(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_forbidden_fields(child, path=f"{path}[{index}]")


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        _raise("INVALID_STRING", f"{field} must be a non-empty string of at most 512 characters")
    if "\x00" in value or "\n" in value or "\r" in value:
        _raise("INVALID_STRING", f"{field} contains a prohibited control character")
    return value


def _require_runtime_identifier(value: Any, field: str) -> str:
    value = _require_string(value, field)
    if _RUNTIME_IDENTIFIER_RE.fullmatch(value) is None:
        _raise("INVALID_RUNTIME_IDENTIFIER", f"{field} is not a safe runtime identifier")
    return value


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _raise("INVALID_DIGEST", f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _raise("INVALID_POSITIVE_INTEGER", f"{field} must be a positive integer")
    return value


def _require_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _raise("INVALID_NONNEGATIVE_INTEGER", f"{field} must be a non-negative integer")
    return value


def _require_false(value: Any, field: str) -> None:
    if value is not False:
        _raise("SAFETY_INVARIANT_VIOLATION", f"{field} must be false")


def _require_true(value: Any, field: str) -> None:
    if value is not True:
        _raise("SAFETY_INVARIANT_VIOLATION", f"{field} must be true")


def _require_comparable_value(value: Any, field: str) -> bool | int | float:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    _raise("INVALID_COMPARISON_VALUE", f"{field} must be a finite number or boolean")


def _require_same_comparison_domain(left: Any, right: Any) -> None:
    if isinstance(left, bool) != isinstance(right, bool):
        _raise(
            "COMPARISON_DOMAIN_MISMATCH",
            "observed_value and threshold_value must both be boolean or both numeric",
        )


def _compare(left: bool | int | float, right: bool | int | float, operator: str) -> bool:
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    _raise("UNSUPPORTED_OPERATOR", f"operator {operator!r} is not supported")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _raise(code: str, message: str) -> None:
    raise BuiltinAdapterValidationError(code, message)
