from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from app.services.strategy_platform_adapter_registry import (
    AdapterManifestValidationError,
    DECLARED_ADAPTER_KEYS,
    INSTALLED_ADAPTER_MANIFEST,
    UNMAPPED_ADAPTERS,
    canonical_manifest_payload,
    installed_adapter_manifest_digest,
    validate_declared_adapter_coverage,
    validate_installed_adapter_manifest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_installed_manifest_sources_schemas_and_symbols_are_current() -> None:
    validated = validate_installed_adapter_manifest(PROJECT_ROOT)

    assert tuple(item.adapter_key for item in validated) == tuple(
        sorted(item.adapter_key for item in INSTALLED_ADAPTER_MANIFEST)
    )
    assert len(validated) == 14
    assert len(installed_adapter_manifest_digest()) == 64
    assert [row["adapter_key"] for row in canonical_manifest_payload()] == sorted(
        item.adapter_key for item in INSTALLED_ADAPTER_MANIFEST
    )


def test_declared_coverage_is_complete_and_verified() -> None:
    assert UNMAPPED_ADAPTERS == ()
    assert set(DECLARED_ADAPTER_KEYS) == {
        item.adapter_key for item in INSTALLED_ADAPTER_MANIFEST
    }
    assert validate_declared_adapter_coverage(PROJECT_ROOT) == tuple(
        sorted(INSTALLED_ADAPTER_MANIFEST, key=lambda item: item.adapter_key)
    )


def test_duplicate_adapter_key_is_rejected() -> None:
    duplicate = (
        INSTALLED_ADAPTER_MANIFEST[0],
        replace(
            INSTALLED_ADAPTER_MANIFEST[1],
            adapter_key=INSTALLED_ADAPTER_MANIFEST[0].adapter_key,
        ),
    )

    with pytest.raises(AdapterManifestValidationError, match="duplicate adapter key"):
        validate_installed_adapter_manifest(PROJECT_ROOT, manifest=duplicate)


def test_missing_source_file_is_rejected() -> None:
    invalid = replace(
        INSTALLED_ADAPTER_MANIFEST[0],
        source_ref="backend/app/services/not_installed.py#adapter",
    )

    with pytest.raises(AdapterManifestValidationError, match="source file is missing"):
        validate_installed_adapter_manifest(PROJECT_ROOT, manifest=(invalid,))


def test_missing_ast_symbol_is_rejected() -> None:
    invalid = replace(
        INSTALLED_ADAPTER_MANIFEST[0],
        source_ref=(
            "backend/app/services/strategy_validation_matrix.py"
            "#not_an_installed_symbol"
        ),
    )

    with pytest.raises(AdapterManifestValidationError, match="source symbol is missing"):
        validate_installed_adapter_manifest(PROJECT_ROOT, manifest=(invalid,))


def test_source_digest_drift_is_rejected() -> None:
    invalid = replace(INSTALLED_ADAPTER_MANIFEST[0], source_sha256="0" * 64)

    with pytest.raises(AdapterManifestValidationError, match="source digest drift"):
        validate_installed_adapter_manifest(PROJECT_ROOT, manifest=(invalid,))


@pytest.mark.parametrize("schema_field", ["input_schema", "output_schema"])
def test_generic_object_schema_is_rejected(schema_field: str) -> None:
    invalid = replace(
        INSTALLED_ADAPTER_MANIFEST[0],
        **{schema_field: {"type": "object"}},
    )

    with pytest.raises(AdapterManifestValidationError, match="generic object schema"):
        validate_installed_adapter_manifest(PROJECT_ROOT, manifest=(invalid,))


def test_validator_parses_ast_without_importing_or_executing_source(tmp_path: Path) -> None:
    source = tmp_path / "adapter.py"
    content = b"raise RuntimeError('must not execute')\n\ndef adapter():\n    return 1\n"
    source.write_bytes(content)
    adapter = replace(
        INSTALLED_ADAPTER_MANIFEST[0],
        source_ref="adapter.py#adapter",
        source_sha256=hashlib.sha256(content).hexdigest(),
    )

    assert validate_installed_adapter_manifest(tmp_path, manifest=(adapter,)) == (adapter,)
