from __future__ import annotations

import ast
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.schemas.strategy_platform import ConfigurationBundleSnapshotRead
from app.services.frozen_configuration_bundle import (
    validate_frozen_configuration_bundle,
)


_REGISTRY_DIGEST = hashlib.sha256(b"trusted-adapter-registry").hexdigest()


def test_validator_has_no_database_or_owner_resolver_dependency() -> None:
    source_path = (
        Path(__file__).parents[1]
        / "app"
        / "services"
        / "frozen_configuration_bundle.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any(module.startswith("sqlalchemy") for module in imported_modules)
    assert "app.repositories.strategy_platform" not in imported_modules
    assert "app.services.configuration_resolver" not in imported_modules


def _digest(value: dict[str, Any]) -> str:
    serialized = json.dumps(
        value,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _version(
    version_id: int,
    type_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    schema_version = f"{type_key}-v1"
    config_digest = _digest(
        {
            "contract": "configuration-version-digest-v1",
            "config_type": type_key,
            "schema_version": schema_version,
            "payload_json": payload,
        }
    )
    return {
        "id": version_id,
        "type_key": type_key,
        "version_number": 1,
        "lifecycle_status": "VALIDATED",
        "payload_json": payload,
        "schema_version": schema_version,
        "config_digest": config_digest,
        "change_summary": None,
        "created_by": "test-owner",
        "created_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
        "validated_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
    }


def _snapshot_payload() -> dict[str, Any]:
    versions = [
        _version(
            101,
            "research-profile",
            {
                "demo_only": True,
                "allow_real_funds": False,
                "single_writer_required": True,
            },
        ),
        _version(201, "metric-definition", {"metric_key": "profit"}),
        _version(202, "metric-definition", {"metric_key": "risk"}),
    ]
    version_map = {
        f"{item['type_key']}:{item['id']}": item["id"] for item in versions
    }
    digest_map = {
        f"{item['type_key']}:{item['id']}": item["config_digest"]
        for item in versions
    }
    capability = {
        "demo_only": True,
        "allow_real_funds": False,
        "single_writer_required": True,
        "resolution_contract": "strategy-platform-owner-resolver-v1",
        "adapter_registry_contract": "strategy-platform-adapter-registry-v1",
        "adapter_registry_digest": _REGISTRY_DIGEST,
        "adapter_registry_keys": ["metric-v1"],
        "resolved_adapter_count": 1,
    }
    payload: dict[str, Any] = {
        "schema_version": "configuration-bundle-resolution-v1",
        "persisted": True,
        "snapshot_id": 901,
        "workflow_kind": "RESEARCH",
        "scope_type": "PLATFORM",
        "scope_key": "DEFAULT",
        "aggregate_profile_version_id": 101,
        "resolved_versions": versions,
        "dependencies": [
            {
                "configuration_version_id": 101,
                "configuration_type": "research-profile",
                "depends_on_version_id": 201,
                "depends_on_type": "metric-definition",
                "relation_key": "profit_metric",
            },
            {
                "configuration_version_id": 101,
                "configuration_type": "research-profile",
                "depends_on_version_id": 202,
                "depends_on_type": "metric-definition",
                "relation_key": "risk_metric",
            },
        ],
        "resolved_versions_json": version_map,
        "resolved_digests_json": digest_map,
        "capability_snapshot": capability,
        "created_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
    }
    payload["bundle_digest"] = _digest(
        {
            "digest_contract": "configuration-bundle-digest-v1",
            "workflow_kind": payload["workflow_kind"],
            "scope_type": payload["scope_type"],
            "scope_key": payload["scope_key"],
            "aggregate_profile_version_id": payload[
                "aggregate_profile_version_id"
            ],
            "resolved_versions_json": dict(sorted(version_map.items())),
            "resolved_digests_json": dict(sorted(digest_map.items())),
            "capability_snapshot": capability,
        }
    )
    return payload


def _snapshot(
    payload: dict[str, Any] | None = None,
) -> ConfigurationBundleSnapshotRead:
    return ConfigurationBundleSnapshotRead.model_validate(
        payload if payload is not None else _snapshot_payload()
    )


def _assert_blocked(payload: dict[str, Any], expected_code: str) -> None:
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        validate_frozen_configuration_bundle(
            _snapshot(payload),
            expected_adapter_registry_digest=_REGISTRY_DIGEST,
        )
    assert exc_info.value.code == expected_code
    assert exc_info.value.detail()["operation_status"] == "BLOCKED"


def test_accepts_composite_keys_and_multiple_versions_of_same_type() -> None:
    verified = validate_frozen_configuration_bundle(
        _snapshot(),
        expected_adapter_registry_digest=_REGISTRY_DIGEST,
    )

    assert verified.snapshot.snapshot_id == 901
    assert [
        item.payload_json["metric_key"]
        for item in verified.versions_by_type["metric-definition"]
    ] == ["profit", "risk"]
    assert (
        verified.require_single_version("research-profile").payload_json["demo_only"]
        is True
    )
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        verified.require_single_version("metric-definition")
    assert exc_info.value.code == "BUNDLE_CONFIGURATION_CARDINALITY_INVALID"


def test_rejects_legacy_bare_type_key_in_production_contract() -> None:
    payload = _snapshot_payload()
    version_id = payload["resolved_versions_json"].pop("research-profile:101")
    digest = payload["resolved_digests_json"].pop("research-profile:101")
    payload["resolved_versions_json"]["research-profile"] = version_id
    payload["resolved_digests_json"]["research-profile"] = digest

    _assert_blocked(payload, "BUNDLE_CONFIGURATION_MAP_KEY_INVALID")


@pytest.mark.parametrize(
    ("capability", "value", "expected_code"),
    [
        ("demo_only", False, "BUNDLE_CAPABILITY_INVALID"),
        ("allow_real_funds", True, "BUNDLE_CAPABILITY_INVALID"),
        ("single_writer_required", False, "BUNDLE_CAPABILITY_INVALID"),
        ("resolution_contract", "legacy", "BUNDLE_CAPABILITY_INVALID"),
        (
            "adapter_registry_digest",
            hashlib.sha256(b"drifted").hexdigest(),
            "ADAPTER_REGISTRY_DIGEST_MISMATCH",
        ),
    ],
)
def test_rejects_missing_or_drifted_fail_closed_capabilities(
    capability: str,
    value: Any,
    expected_code: str,
) -> None:
    payload = _snapshot_payload()
    payload["capability_snapshot"][capability] = value

    _assert_blocked(payload, expected_code)


def test_rejects_version_payload_drift_even_when_map_keeps_stored_digest() -> None:
    payload = _snapshot_payload()
    payload["resolved_versions"][1]["payload_json"]["metric_key"] = "changed"

    _assert_blocked(payload, "BUNDLE_CONFIGURATION_VERSION_DIGEST_INVALID")


def test_rejects_orphaned_version_graph() -> None:
    payload = _snapshot_payload()
    payload["dependencies"] = payload["dependencies"][:1]

    _assert_blocked(payload, "BUNDLE_CONFIGURATION_VERSION_ORPHANED")


def test_rejects_nested_payload_that_weakens_demo_only_safety() -> None:
    payload = _snapshot_payload()
    aggregate = payload["resolved_versions"][0]
    aggregate["payload_json"]["runtime"] = {"allow_real_funds": True}
    aggregate["config_digest"] = _digest(
        {
            "contract": "configuration-version-digest-v1",
            "config_type": aggregate["type_key"],
            "schema_version": aggregate["schema_version"],
            "payload_json": aggregate["payload_json"],
        }
    )
    payload["resolved_digests_json"]["research-profile:101"] = aggregate[
        "config_digest"
    ]

    _assert_blocked(payload, "CONFIGURATION_SAFETY_INVARIANT_VIOLATION")


def test_rejects_bundle_digest_drift() -> None:
    payload = deepcopy(_snapshot_payload())
    payload["bundle_digest"] = hashlib.sha256(b"drifted-bundle").hexdigest()

    _assert_blocked(payload, "BUNDLE_DIGEST_MISMATCH")


def test_rejects_non_persisted_preview() -> None:
    payload = _snapshot_payload()
    payload["persisted"] = False

    _assert_blocked(payload, "PERSISTED_CONFIGURATION_BUNDLE_REQUIRED")


def test_rejects_missing_snapshot_instead_of_falling_back() -> None:
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        validate_frozen_configuration_bundle(
            None,
            expected_adapter_registry_digest=_REGISTRY_DIGEST,
        )

    assert exc_info.value.code == "PERSISTED_CONFIGURATION_BUNDLE_REQUIRED"


def test_rejects_missing_trusted_adapter_registry_digest() -> None:
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        validate_frozen_configuration_bundle(
            _snapshot(),
            expected_adapter_registry_digest="",
        )

    assert exc_info.value.code == "EXPECTED_ADAPTER_REGISTRY_DIGEST_INVALID"
