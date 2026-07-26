import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.schemas import (
    EnvironmentIdentity,
    StrategyVersionRead,
    classify_artifact_environment,
    classify_strategy_environment,
    configured_environment_identity,
    database_record_source,
)
import app.schemas.environment_evidence as environment_evidence


def environment_identity(
    canonical_repo: Path,
    *,
    artifact_roots: tuple[Path, ...] = (),
    historical_roots: tuple[Path, ...] = (),
) -> EnvironmentIdentity:
    return EnvironmentIdentity(
        canonical_repo_root=canonical_repo.resolve(),
        artifact_roots=tuple(path.resolve() for path in artifact_roots),
        historical_roots=tuple(path.resolve() for path in historical_roots),
    )


def test_current_environment_artifact_is_runnable(tmp_path: Path) -> None:
    canonical_repo = tmp_path / "canonical"
    artifact = canonical_repo / "generated" / "current.py"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("class CurrentStrategy:\n    pass\n", encoding="utf-8")

    evidence = classify_artifact_environment(
        {"strategy_file_path": str(artifact)},
        identity=environment_identity(canonical_repo),
    )

    assert evidence.scope == "current"
    assert evidence.runnable is True
    assert evidence.migration_verified is False


def test_configured_historical_artifact_stays_read_only_when_missing(tmp_path: Path) -> None:
    canonical_repo = tmp_path / "canonical"
    historical_root = tmp_path / "retired-checkout"
    historical = historical_root / "generated" / "old.py"

    evidence = classify_artifact_environment(
        {"strategy_file_path": str(historical)},
        identity=environment_identity(
            canonical_repo,
            historical_roots=(historical_root,),
        ),
    )

    assert evidence.scope == "historical"
    assert evidence.runnable is False
    assert "只读审计" in evidence.reason


def test_missing_current_environment_artifact_is_not_runnable(tmp_path: Path) -> None:
    canonical_repo = tmp_path / "canonical"
    missing = canonical_repo / "generated" / "missing.py"

    evidence = classify_artifact_environment(
        {"strategy_file_path": str(missing)},
        identity=environment_identity(canonical_repo),
    )

    assert evidence.scope == "current"
    assert evidence.runnable is False


def test_verified_migration_reconciles_file_database_ids_and_checksum(tmp_path: Path) -> None:
    code = "class MigratedStrategy:\n    pass\n"
    checksum = hashlib.sha256(code.encode("utf-8")).hexdigest()
    canonical_repo = tmp_path / "canonical"
    external_strategy_root = tmp_path / "trusted-strategies"
    historical_root = tmp_path / "retired-checkout"
    current = external_strategy_root / "migrated.py"
    current.parent.mkdir()
    current.write_text(code, encoding="utf-8")
    database_ids = {"strategy_id": 7, "strategy_version_id": 11}
    manifest = {
        "source_path": str(
            historical_root / "generated" / "migrated.py"
        ),
        "current_path": str(current),
        "database_ids": database_ids,
        "artifact_checksum": checksum,
    }

    evidence = classify_strategy_environment(
        file_path=str(current),
        database_ids=database_ids,
        expected_checksum=checksum,
        migration_manifest=manifest,
        identity=environment_identity(
            canonical_repo,
            artifact_roots=(external_strategy_root,),
            historical_roots=(historical_root,),
        ),
    )

    assert evidence.scope == "current"
    assert evidence.runnable is True
    assert evidence.migration_verified is True


def test_trusted_repo_external_artifact_root_is_current(tmp_path: Path) -> None:
    canonical_repo = tmp_path / "canonical"
    external_backtests = tmp_path / "external-backtests"
    artifact = external_backtests / "result.json"
    artifact.parent.mkdir()
    artifact.write_text("{}", encoding="utf-8")

    evidence = classify_artifact_environment(
        {"result_path": str(artifact)},
        identity=environment_identity(
            canonical_repo,
            artifact_roots=(external_backtests,),
        ),
    )

    assert evidence.scope == "current"
    assert evidence.runnable is True


def test_configured_identity_resolves_relative_and_repo_external_roots(
    tmp_path: Path,
) -> None:
    canonical_repo = tmp_path / "canonical"
    external_backtests = tmp_path / "external-backtests"
    identity = configured_environment_identity(
        Settings(
            canonical_repo_root=canonical_repo,
            strategy_output_dir=Path("generated"),
            backtest_result_dir=external_backtests,
            historical_read_only_roots=[tmp_path / "retired"],
        )
    )

    assert identity.canonical_repo_root == canonical_repo.resolve()
    assert identity.artifact_roots == (
        (canonical_repo / "generated").resolve(),
        external_backtests.resolve(),
    )
    assert identity.historical_roots == ((tmp_path / "retired").resolve(),)


def test_noncanonical_worktree_and_record_claim_cannot_expand_trusted_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_repo = tmp_path / "canonical"
    worktree_artifact = tmp_path / "worktree" / "generated" / "claim.py"
    worktree_artifact.parent.mkdir(parents=True)
    worktree_artifact.write_text("class Claim:\n    pass\n", encoding="utf-8")

    evidence = classify_artifact_environment(
        {"strategy_file_path": str(worktree_artifact)},
        identity=environment_identity(canonical_repo),
    )

    assert evidence.scope == "unknown"
    assert evidence.runnable is False

    identity = environment_identity(canonical_repo)
    monkeypatch.setattr(
        environment_evidence,
        "configured_environment_identity",
        lambda settings=None: identity,
    )
    version = StrategyVersionRead(
        id=13,
        strategy_id=8,
        generation_run_id=None,
        parent_version_id=None,
        version_number=1,
        blueprint={"class_name": "Claim"},
        generated_code="class Claim:\n    pass\n",
        code_hash=hashlib.sha256(worktree_artifact.read_bytes()).hexdigest(),
        file_path=str(worktree_artifact),
        validation_status="passed",
        validation_errors=[],
        change_summary=None,
        diff_snapshot={
            "strategy_file_validation": {
                "approved_root": str(worktree_artifact.parent),
            }
        },
        created_at=datetime.now(timezone.utc),
    )

    assert version.file_state.status == "READY"
    assert version.data_source.environment.scope == "unknown"
    assert version.data_source.core_data is False


def test_historical_missing_strategy_uses_short_main_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_repo = Path("/Users/example/Developer/Freqtrade Ai")
    historical_root = Path("/Users/example/Documents/Freqtrade Ai")
    identity = environment_identity(
        canonical_repo,
        historical_roots=(historical_root,),
    )
    monkeypatch.setattr(
        environment_evidence,
        "configured_environment_identity",
        lambda settings=None: identity,
    )
    historical_file = historical_root / "generated" / "missing.py"

    version = StrategyVersionRead(
        id=11,
        strategy_id=7,
        generation_run_id=None,
        parent_version_id=None,
        version_number=1,
        blueprint={"class_name": "Missing"},
        generated_code="class Missing:\n    pass\n",
        code_hash=None,
        file_path=str(historical_file),
        validation_status="passed",
        validation_errors=[],
        change_summary=None,
        diff_snapshot={
            "strategy_file_validation": {
                "approved_root": str(historical_root),
            }
        },
        created_at=datetime.now(timezone.utc),
    )

    assert version.data_source.environment.scope == "historical"
    assert version.data_source.core_data is False
    assert version.data_source.blocked_reason == "证据属于历史环境，仅保留只读审计。"
    assert "/Users" not in (version.data_source.blocked_reason or "")
    assert str(historical_file) in (version.file_state.blocked_reason or "")

    generic_source = database_record_source(
        "backtest_result",
        {"backtest_result_id": 31},
        artifact_refs={"result_path": str(historical_root / "backtest.json")},
    )
    assert generic_source.environment.scope == "historical"
    assert generic_source.core_data is False
