from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.services.runtime_configuration_bundle_reader import (
    RuntimeConfigurationBundleReader,
)


MANIFEST_DIGEST = hashlib.sha256(b"installed-manifest").hexdigest()


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _adapter_rows() -> list[dict[str, Any]]:
    return [
        {
            "adapter_key": "profile-bound-score-v2",
            "adapter_kind": "SCORER",
            "implementation_version": "profile-bound-score-v2",
            "input_schema_version": "profile-bound-score-input-v2",
            "output_schema_version": "profile-bound-score-output-v2",
            "capabilities": {"profile_bound": True},
            "display_metadata": {
                "installed_manifest_digest": MANIFEST_DIGEST,
            },
            "enabled": True,
            "registry_metadata_only": True,
            "contains_secret_material": False,
            "contains_executable_payload": False,
        }
    ]


def _rows() -> dict[str, Any]:
    payload = {
        "demo_only": True,
        "allow_real_funds": False,
        "single_writer_required": True,
    }
    version_digest = _digest(
        {
            "contract": "configuration-version-digest-v1",
            "config_type": "research-profile",
            "schema_version": "research-profile-v2",
            "payload_json": payload,
        }
    )
    version_map = {"research-profile:100": 100}
    digest_map = {"research-profile:100": version_digest}
    registry_digest = _digest(_adapter_rows())
    capability = {
        "demo_only": True,
        "allow_real_funds": False,
        "single_writer_required": True,
        "resolution_contract": "strategy-platform-owner-resolver-v1",
        "adapter_registry_contract": "strategy-platform-adapter-registry-v1",
        "adapter_registry_digest": registry_digest,
        "installed_adapter_manifest_digest": MANIFEST_DIGEST,
    }
    bundle_digest = _digest(
        {
            "digest_contract": "configuration-bundle-digest-v1",
            "workflow_kind": "RESEARCH",
            "scope_type": "PLATFORM",
            "scope_key": "DEFAULT",
            "aggregate_profile_version_id": 100,
            "resolved_versions_json": version_map,
            "resolved_digests_json": digest_map,
            "capability_snapshot": capability,
        }
    )
    return {
        "snapshot": {
            "id": 900,
            "workflow_kind": "RESEARCH",
            "scope_type": "PLATFORM",
            "scope_key": "DEFAULT",
            "aggregate_profile_version_id": 100,
            "resolved_versions_json": version_map,
            "resolved_digests_json": digest_map,
            "bundle_digest": bundle_digest,
            "capability_snapshot": capability,
            "created_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
        },
        "versions": [
            {
                "id": 100,
                "type_key": "research-profile",
                "version_number": 2,
                "lifecycle_status": "VALIDATED",
                "payload_json": payload,
                "schema_version": "research-profile-v2",
                "config_digest": version_digest,
                "change_summary": None,
                "created_by": "control-plane",
                "created_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
                "validated_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
            }
        ],
        "dependencies": [],
        "adapters": _adapter_rows(),
    }


class _Mappings:
    def __init__(self, rows):
        self.rows = rows

    def one_or_none(self):
        if not self.rows:
            return None
        assert len(self.rows) == 1
        return self.rows[0]

    def all(self):
        return self.rows


class _Result:
    def __init__(self, rows=None, scalar=None):
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self):
        return _Mappings(self.rows)

    def scalar_one(self):
        return self.scalar

    def scalar_one_or_none(self):
        return self.scalar


class _Connection:
    def __init__(
        self,
        rows=None,
        *,
        database="freqtrade_ai_design_lab",
        schema_version="20260813_47",
    ):
        self.rows = rows or _rows()
        self.database = database
        self.schema_version = schema_version
        self.statements: list[str] = []

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        if sql == "SELECT current_database()":
            return _Result(scalar=self.database)
        if "FROM strategy_platform_migration_runs" in sql:
            return _Result(scalar=self.schema_version)
        if "FROM configuration_bundle_snapshots" in sql:
            return _Result(rows=[self.rows["snapshot"]])
        if "FROM configuration_dependencies" in sql:
            return _Result(rows=self.rows["dependencies"])
        if "FROM configuration_versions" in sql:
            return _Result(rows=self.rows["versions"])
        if "FROM adapter_definitions" in sql:
            return _Result(rows=self.rows["adapters"])
        raise AssertionError(f"unexpected SQL: {sql}")


def test_reader_uses_only_narrow_select_projection_and_validates_bundle() -> None:
    connection = _Connection()
    verified = RuntimeConfigurationBundleReader(
        connection,
        installed_adapter_manifest_digest=MANIFEST_DIGEST,
    ).read_validated(900)

    assert verified.snapshot.snapshot_id == 900
    assert verified.require_single_version("research-profile").id == 100
    assert len(connection.statements) == 6
    assert all(statement.startswith("SELECT ") for statement in connection.statements)
    assert not any(
        token in " ".join(connection.statements).upper()
        for token in ("INSERT ", "UPDATE ", "DELETE ", "GRANT ", "SET ROLE")
    )


def test_reader_rejects_every_database_except_design_lab() -> None:
    connection = _Connection(database="freqtrade_ai")

    with pytest.raises(StrategyPlatformReadError) as exc_info:
        RuntimeConfigurationBundleReader(
            connection,
            installed_adapter_manifest_digest=MANIFEST_DIGEST,
        ).read_validated(900)

    assert exc_info.value.code == "RUNTIME_CONFIGURATION_DATABASE_INVALID"
    assert connection.statements == ["SELECT current_database()"]


def test_reader_rejects_missing_or_non_v47_migration_receipt() -> None:
    for version in (None, "20260813_46"):
        with pytest.raises(StrategyPlatformReadError) as exc_info:
            RuntimeConfigurationBundleReader(
                _Connection(schema_version=version),
                installed_adapter_manifest_digest=MANIFEST_DIGEST,
            ).read_validated(900)

        assert exc_info.value.code == "RUNTIME_CONFIGURATION_SCHEMA_VERSION_INVALID"


def test_reader_rejects_adapter_registry_or_manifest_drift() -> None:
    rows = _rows()
    rows["adapters"][0]["capabilities"] = {"profile_bound": False}
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        RuntimeConfigurationBundleReader(
            _Connection(rows),
            installed_adapter_manifest_digest=MANIFEST_DIGEST,
        ).read_validated(900)
    assert exc_info.value.code == "ADAPTER_REGISTRY_DIGEST_MISMATCH"

    with pytest.raises(StrategyPlatformReadError) as exc_info:
        RuntimeConfigurationBundleReader(
            _Connection(),
            installed_adapter_manifest_digest=hashlib.sha256(b"other").hexdigest(),
        ).read_validated(900)
    assert exc_info.value.code == "INSTALLED_ADAPTER_MANIFEST_DIGEST_MISMATCH"


def test_reader_has_no_owner_resolver_repository_or_orm_import() -> None:
    source_path = (
        Path(__file__).parents[1]
        / "app"
        / "services"
        / "runtime_configuration_bundle_reader.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert "app.services.configuration_resolver" not in imported_modules
    assert "app.repositories.strategy_platform" not in imported_modules
    assert not any(module.startswith("app.models") for module in imported_modules)
