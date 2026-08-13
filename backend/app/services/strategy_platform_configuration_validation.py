"""Closed JSON configuration contracts used by Strategy Platform V1.3.

The validator intentionally implements the small, deterministic JSON Schema
subset emitted by :func:`infer_closed_json_schema`.  It does not execute code,
resolve secrets, perform I/O, or apply hidden defaults.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any


HANDLER_KEY = "strategy-platform-closed-json-schema-v1"


class ConfigurationPayloadValidationError(ValueError):
    pass


_SAFETY_CONSTANTS: Mapping[str, object] = {
    "demo_only": True,
    "allow_real_funds": False,
    "single_writer_required": True,
    "fail_closed": True,
    "contains_secret_material": False,
    "contains_executable_payload": False,
    "secret_material_present": False,
    "executable_payload_present": False,
    "executable_payload_allowed": False,
    "execution_requested": False,
}


def infer_closed_json_schema(value: Any, *, field_name: str | None = None) -> dict[str, Any]:
    """Infer a strict schema from the first accepted version of a config type."""

    if isinstance(value, Mapping):
        properties = {
            str(key): infer_closed_json_schema(child, field_name=str(key))
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
        schema: dict[str, Any] = {
            "type": "object",
            "required": list(properties),
            "properties": properties,
            "additionalProperties": False,
        }
    elif isinstance(value, list):
        schema = {"type": "array"}
        if not value:
            # An empty initial collection is explicitly empty.  Expanding its
            # element contract requires a new configuration schema version.
            schema["maxItems"] = 0
        else:
            candidates = [infer_closed_json_schema(item) for item in value]
            distinct: list[dict[str, Any]] = []
            for candidate in candidates:
                if candidate not in distinct:
                    distinct.append(candidate)
            schema["items"] = distinct[0] if len(distinct) == 1 else {"anyOf": distinct}
    elif isinstance(value, bool):
        schema = {"type": "boolean"}
    elif isinstance(value, int):
        schema = {"type": "integer"}
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigurationPayloadValidationError("non-finite numbers are forbidden")
        schema = {"type": "number"}
    elif isinstance(value, str):
        schema = {"type": "string"}
    elif value is None:
        schema = {"type": "null"}
    else:
        raise ConfigurationPayloadValidationError(
            f"unsupported configuration value: {type(value).__name__}"
        )
    if field_name in _SAFETY_CONSTANTS:
        required = _SAFETY_CONSTANTS[field_name]
        if value != required:
            raise ConfigurationPayloadValidationError(
                f"safety invariant {field_name} must be {required!r}"
            )
        schema["const"] = required
    return schema


def validate_closed_json_schema(value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
    """Validate the exact fail-closed schema subset without coercion/defaults."""

    if "anyOf" in schema:
        failures = 0
        for candidate in schema["anyOf"]:
            try:
                validate_closed_json_schema(value, candidate, path=path)
                return
            except ConfigurationPayloadValidationError:
                failures += 1
        raise ConfigurationPayloadValidationError(
            f"{path} matches none of {failures} declared schemas"
        )
    expected = schema.get("type")
    valid_type = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "null": value is None,
    }.get(str(expected), False)
    if not valid_type:
        raise ConfigurationPayloadValidationError(
            f"{path} must be {expected}; got {type(value).__name__}"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigurationPayloadValidationError(f"{path} cannot be non-finite")
    if "const" in schema and value != schema["const"]:
        raise ConfigurationPayloadValidationError(
            f"{path} violates immutable safety value"
        )
    if expected == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if (
            not isinstance(properties, Mapping)
            or not isinstance(required, list)
            or schema.get("additionalProperties") is not False
        ):
            raise ConfigurationPayloadValidationError(
                f"{path} uses an open or generic object schema"
            )
        missing = [key for key in required if key not in value]
        unexpected = sorted(set(value) - set(properties))
        if missing or unexpected:
            raise ConfigurationPayloadValidationError(
                f"{path} keys mismatch: missing={missing} unexpected={unexpected}"
            )
        for key, child in value.items():
            validate_closed_json_schema(child, properties[key], path=f"{path}.{key}")
    elif expected == "array":
        maximum = schema.get("maxItems")
        if maximum is not None and len(value) > int(maximum):
            raise ConfigurationPayloadValidationError(f"{path} has too many items")
        item_schema = schema.get("items")
        if value and not isinstance(item_schema, Mapping):
            raise ConfigurationPayloadValidationError(
                f"{path} has no declared item schema"
            )
        for index, child in enumerate(value):
            validate_closed_json_schema(child, item_schema, path=f"{path}[{index}]")


def adapter_keys_in_payload(value: Any) -> tuple[str, ...]:
    keys: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                if str(key).endswith("adapter_key"):
                    # Registry-backed adapters are optional on non-executing
                    # targets such as RESEARCH_ONLY.  NULL is the explicit
                    # absence of a capability; any supplied value must still
                    # name one exact installed adapter.
                    if child is None:
                        continue
                    if not isinstance(child, str) or not child:
                        raise ConfigurationPayloadValidationError(
                            f"{key} must be a non-empty installed adapter key"
                        )
                    keys.add(child)
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return tuple(sorted(keys))
