from __future__ import annotations

import pytest

from app.services.strategy_platform_configuration_validation import (
    ConfigurationPayloadValidationError,
    adapter_keys_in_payload,
    infer_closed_json_schema,
    validate_closed_json_schema,
)


def test_inferred_schema_is_closed_and_revalidates_payload() -> None:
    payload = {
        "demo_only": True,
        "allow_real_funds": False,
        "rules": [{"adapter_key": "threshold-comparison-v1", "limit": 3.5}],
    }
    schema = infer_closed_json_schema(payload)

    validate_closed_json_schema(payload, schema)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["demo_only"]["const"] is True
    assert adapter_keys_in_payload(payload) == ("threshold-comparison-v1",)


def test_unknown_keys_and_safety_weakening_are_rejected() -> None:
    payload = {"demo_only": True, "allow_real_funds": False}
    schema = infer_closed_json_schema(payload)
    with pytest.raises(ConfigurationPayloadValidationError, match="keys mismatch"):
        validate_closed_json_schema({**payload, "fallback": True}, schema)
    with pytest.raises(ConfigurationPayloadValidationError, match="safety"):
        validate_closed_json_schema(
            {"demo_only": True, "allow_real_funds": True}, schema
        )


def test_empty_array_requires_a_new_schema_before_items_can_be_added() -> None:
    schema = infer_closed_json_schema({"rules": []})
    with pytest.raises(ConfigurationPayloadValidationError, match="too many"):
        validate_closed_json_schema({"rules": [1]}, schema)


def test_optional_adapter_reference_may_be_null_but_not_empty() -> None:
    assert adapter_keys_in_payload(
        {
            "exchange_adapter_key": None,
            "runtime_adapter_key": "docker-runtime-v1",
        }
    ) == ("docker-runtime-v1",)
    with pytest.raises(ConfigurationPayloadValidationError, match="non-empty"):
        adapter_keys_in_payload({"exchange_adapter_key": ""})
