from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app.canonical_v13.intake import CanonicalIntakeBlocked
from app.canonical_v13.latest_intake_manifest import (
    LATEST_MANIFEST_CONTRACT,
    build_latest_strategy_manifest,
)


def _source(class_name: str, *, extra_import: str = "") -> str:
    return (
        f"{extra_import}from freqtrade.strategy import IStrategy\n"
        f"class {class_name}(IStrategy):\n"
        "    pass\n"
    )


def _write(root: Path, name: str, content: str) -> None:
    path = root / "generated" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_manifest_selects_exact_highest_run_per_top_level_class(tmp_path: Path) -> None:
    _write(tmp_path, "alpha_run_1_1.py", _source("Alpha"))
    _write(tmp_path, "alpha_run_3_1.py", _source("Alpha"))
    _write(tmp_path, "beta_run_2_1.py", _source("Beta"))

    first = build_latest_strategy_manifest(tmp_path)
    second = build_latest_strategy_manifest(tmp_path)

    assert first.archive_snapshot_digest == second.archive_snapshot_digest
    assert first.visible_file_count == 3
    assert [(entry.strategy_class, entry.selected_run) for entry in first.entries] == [
        ("Alpha", 3),
        ("Beta", 2),
    ]
    alpha = first.entries[0]
    assert alpha.selected_path == "generated/alpha_run_3_1.py"
    assert alpha.inspection.checks["static_validation"] == "PASSED"
    assert len(alpha.snapshot.versions) == 2
    command = alpha.api_command(caller_identity="manifest-test")
    assert command["current_version_id"] == alpha.snapshot.current_version_id
    assert base64.b64decode(command["versions"][1]["artifact_base64"]) == _source(
        "Alpha"
    ).encode()
    evidence = first.evidence()
    assert evidence["contract"] == LATEST_MANIFEST_CONTRACT
    assert evidence["selected_strategy_count"] == 2
    assert evidence["legacy_database_access"] == "NONE"
    assert evidence["backtest"] == "NOT_RUN"
    assert "source_root" not in evidence


@pytest.mark.parametrize(
    ("files", "code"),
    [
        (
            {
                "alpha_run_1_1.py": _source("Alpha"),
                "alpha-copy_run_1_1.py": _source("Alpha"),
            },
            "BLOCKED_AMBIGUOUS_LATEST_SOURCE",
        ),
        (
            {"alpha.py": _source("Alpha")},
            "BLOCKED_AMBIGUOUS_SOURCE_VERSION",
        ),
        (
            {"alpha_run_1_1.py": _source("Alpha", extra_import="import os\n")},
            "REJECTED_IMPORT_NOT_ALLOWED",
        ),
        (
            {
                "alpha_run_1_1.py": (
                    _source("Alpha") + "\nclass Hidden(IStrategy):\n    pass\n"
                )
            },
            "BLOCKED_AMBIGUOUS_SOURCE_CLASS",
        ),
    ],
)
def test_manifest_fails_closed_without_selecting_ambiguous_or_unsafe_source(
    tmp_path: Path, files: dict[str, str], code: str
) -> None:
    for name, content in files.items():
        _write(tmp_path, name, content)
    with pytest.raises(CanonicalIntakeBlocked) as raised:
        build_latest_strategy_manifest(tmp_path)
    assert raised.value.code == code


def test_manifest_requires_explicit_absolute_source_root(tmp_path: Path) -> None:
    with pytest.raises(CanonicalIntakeBlocked) as raised:
        build_latest_strategy_manifest(Path("relative/source"))
    assert raised.value.code == "BLOCKED_INVALID_SOURCE_ROOT"
