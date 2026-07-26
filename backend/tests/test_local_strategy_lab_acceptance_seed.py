import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "seed_local_strategy_lab_acceptance.py"
SPEC = importlib.util.spec_from_file_location("acceptance_seed", SCRIPT)
assert SPEC and SPEC.loader
SEED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEED)
sys.modules["seed_local_strategy_lab_acceptance"] = SEED
WRAPPER_PATH = REPO_ROOT / "scripts" / "run_local_strategy_lab_acceptance_server.py"
WRAPPER_SPEC = importlib.util.spec_from_file_location("acceptance_server", WRAPPER_PATH)
assert WRAPPER_SPEC and WRAPPER_SPEC.loader
WRAPPER = importlib.util.module_from_spec(WRAPPER_SPEC)
WRAPPER_SPEC.loader.exec_module(WRAPPER)


def run_seed(tmp_path: Path, profile: str = "complete-current") -> dict:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--parent",
            str(tmp_path),
            "--profile",
            profile,
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_complete_current_seed_creates_reconciled_rows_and_real_files(tmp_path: Path) -> None:
    manifest = run_seed(tmp_path)
    root = Path(manifest["canonical_root"])
    ids = manifest["database_ids"]
    assert root.parent == tmp_path.resolve()
    assert root.name.startswith("freqtrade-ai-issue-433-")
    assert set(ids) == {
        "strategy_generation_run_id",
        "strategy_id",
        "strategy_version_id",
        "backtest_run_id",
        "backtest_task_id",
        "backtest_result_id",
        "strategy_score_id",
    }
    assert all(isinstance(value, int) and value > 0 for value in ids.values())
    assert all(value is False for value in manifest["safety"].values())
    for key in ("strategy_file", "backtest_result"):
        path = Path(manifest["artifacts"][key])
        assert path.is_file()
        assert path.is_relative_to(root)
    assert Path(manifest["manifest_path"]).is_file()

    engine = create_engine(manifest["database_url"])
    with engine.connect() as connection:
        checks = {
            "strategy_generation_runs": "strategy_generation_run_id",
            "strategies": "strategy_id",
            "strategy_versions": "strategy_version_id",
            "backtest_runs": "backtest_run_id",
            "backtest_tasks": "backtest_task_id",
            "backtest_results": "backtest_result_id",
            "strategy_scores": "strategy_score_id",
        }
        for table, key in checks.items():
            assert connection.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE id = :id"),
                {"id": ids[key]},
            ).scalar_one() == 1


@pytest.mark.parametrize(
    ("profile", "table", "expected"),
    [
        ("empty", "strategies", 0),
        ("missing-result", "backtest_results", 0),
        ("missing-strategy", "strategy_versions", 1),
        ("long-evidence", "backtest_results", 1),
    ],
)
def test_negative_profiles_are_real_database_states(
    tmp_path: Path, profile: str, table: str, expected: int
) -> None:
    manifest = run_seed(tmp_path, profile)
    engine = create_engine(manifest["database_url"])
    with engine.connect() as connection:
        assert connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == expected
    if profile == "missing-strategy":
        assert not Path(manifest["artifacts"]["strategy_file"]).exists()


def test_explicit_or_existing_root_and_database_are_rejected(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    cli = subprocess.run(
        [sys.executable, str(SCRIPT), "--parent", str(tmp_path), "--root", str(existing)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert cli.returncode != 0
    assert "explicit acceptance roots are forbidden" in cli.stderr

    root = SEED.allocate_root(tmp_path)
    database_url, database_path = SEED.guarded_database_url(root)
    database_path.touch()
    with pytest.raises(FileExistsError, match="already exists"):
        SEED.seed_profile(root, "empty")
    assert database_url.startswith("sqlite+pysqlite:///")


def test_symlink_component_cannot_write_outside_root(tmp_path: Path) -> None:
    root = SEED.allocate_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "reports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        SEED.safe_write(root, "reports/backtests/result.json", b"unsafe")
    assert list(outside.iterdir()) == []


def test_real_canonical_and_symlink_parents_are_rejected(tmp_path: Path) -> None:
    canonical = Path("~/Developer/Freqtrade Ai").expanduser()
    if canonical.exists():
        with pytest.raises(ValueError, match="real canonical"):
            SEED.allocate_root(canonical)
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        SEED.allocate_root(link)


def test_seed_does_not_inherit_runtime_database_or_provider_secrets(tmp_path: Path) -> None:
    manifest = run_seed(tmp_path)
    assert manifest["database_url"].startswith("sqlite+pysqlite:///")
    assert os.environ.get("DATABASE_URL") != manifest["database_url"]
    assert "DEEPSEEK_API_KEY" not in json.dumps(manifest)


def test_backend_env_builder_keeps_only_minimal_non_secret_values(tmp_path: Path) -> None:
    manifest = run_seed(tmp_path)
    environment = WRAPPER.build_backend_env(
        manifest,
        {
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp/test-home",
            "LANG": "en_US.UTF-8",
            "DEEPSEEK_API_KEY": "must-not-pass",
            "OPERATOR_TOKEN": "must-not-pass",
            "EXCHANGE_SECRET": "must-not-pass",
            "FREQTRADE_BINARY": "/real/freqtrade",
            "ALLOW_LIVE_TRADING": "true",
        },
    )
    assert set(environment) == {
        "PATH",
        "HOME",
        "LANG",
        "APP_ENV",
        "DATABASE_URL",
        "FREQTRADE_AI_CANONICAL_REPO_ROOT",
        "E2E_ACCEPTANCE_MANIFEST",
        "PYTHONUNBUFFERED",
    }
    rendered = json.dumps(environment)
    for secret in ("must-not-pass", "/real/freqtrade", "ALLOW_LIVE_TRADING"):
        assert secret not in rendered


def test_cleanup_removes_only_manifest_owned_exclusive_root(tmp_path: Path) -> None:
    manifest = run_seed(tmp_path)
    root = Path(manifest["canonical_root"])
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    WRAPPER.cleanup_seed_root(manifest, tmp_path)
    assert not root.exists()
    assert unrelated.is_dir()

    forged = {**manifest, "canonical_root": str(unrelated)}
    with pytest.raises(RuntimeError, match="unowned"):
        WRAPPER.cleanup_seed_root(forged, tmp_path)
    assert unrelated.is_dir()
