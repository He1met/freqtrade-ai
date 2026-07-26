import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

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
        "FREQTRADE_AI_TEST_DISABLE_ENV_FILE",
        "E2E_ACCEPTANCE_MANIFEST",
        "PYTHONUNBUFFERED",
    }
    assert environment["FREQTRADE_AI_TEST_DISABLE_ENV_FILE"] == "1"
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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_json(url: str, timeout: float = 20.0) -> tuple[int, dict]:
    deadline = time.monotonic() + timeout
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return response.status, json.load(response)
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"backend did not become ready: {last_error}")


def _copy_acceptance_runtime(destination: Path) -> Path:
    isolated_repo = destination / "isolated-repo"
    shutil.copytree(
        REPO_ROOT / "backend",
        isolated_repo / "backend",
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    shutil.copytree(REPO_ROOT / "config", isolated_repo / "config")
    scripts = isolated_repo / "scripts"
    scripts.mkdir()
    for name in (
        "run_local_strategy_lab_acceptance_server.py",
        "seed_local_strategy_lab_acceptance.py",
    ):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
    shutil.copy2(REPO_ROOT / ".env.example", isolated_repo / ".env.example")
    return isolated_repo


def _start_wrapper(
    repo_root: Path,
    parent: Path,
    registry: Path,
    port: int,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_local_strategy_lab_acceptance_server.py"),
            "--parent",
            str(parent),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--profile",
            "complete-current",
            "--registry",
            str(registry),
        ],
        cwd=repo_root / "backend",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_real_backend_ignores_repository_dotenv_and_blocks_credentials(
    tmp_path: Path,
) -> None:
    isolated_repo = _copy_acceptance_runtime(tmp_path)
    fake_values = {
        "DEEPSEEK_API_KEY": "fake-deepseek-from-dotenv",
        "OKX_DEMO_API_KEY": "fake-okx-key-from-dotenv",
        "OKX_DEMO_API_SECRET": "fake-okx-secret-from-dotenv",
        "OKX_DEMO_API_PASSPHRASE": "fake-okx-passphrase-from-dotenv",
        "FREQTRADE_AI_OPERATOR_TOKEN": "fake-operator-from-dotenv",
        "FREQTRADE_BINARY": "/bin/true",
    }
    (isolated_repo / ".env").write_text(
        "".join(f"{name}={value}\n" for name, value in fake_values.items()),
        encoding="utf-8",
    )
    parent = tmp_path / "acceptance"
    parent.mkdir()
    registry = parent / "freqtrade-ai-issue-433-registry-dotenv.json"
    port = _free_port()
    process = _start_wrapper(isolated_repo, parent, registry, port)
    try:
        status, health = _wait_for_json(f"http://127.0.0.1:{port}/health")
        assert status == 200
        assert health["allow_live_trading"] is False
        assert health["allow_dry_run_trading"] is False

        _, operator = _wait_for_json(f"http://127.0.0.1:{port}/api/runtime/operator-status")
        presence = {item["name"]: item["present"] for item in operator["env_presence"]}
        assert presence["DEEPSEEK_API_KEY"] is False
        assert presence["OKX_DEMO_API_KEY"] is False
        assert presence["OKX_DEMO_API_SECRET"] is False
        assert presence["OKX_DEMO_API_PASSPHRASE"] is False

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/strategy-generation-runs/deepseek-single",
            data=json.dumps(
                {"prompt_summary": "must remain blocked", "allow_real_call": True}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Operator-Token": fake_values["FREQTRADE_AI_OPERATOR_TOKEN"],
                "Idempotency-Key": "issue-433-dotenv-isolation",
                "X-Provider-Authorization": "once",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(request, timeout=2)
        assert blocked.value.code == 503
        payload = json.load(blocked.value)
        assert payload["detail"]["operation_status"] == "BLOCKED"
        assert "not configured" in payload["detail"]["message"]
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 128 + signal.SIGTERM, (stdout, stderr)
    assert not registry.exists()
    assert list(parent.glob("freqtrade-ai-issue-433-*")) == []


def test_sigterm_after_child_launch_removes_seed_and_stops_backend(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "acceptance"
    parent.mkdir()
    registry = parent / "freqtrade-ai-issue-433-registry-running.json"
    port = _free_port()
    process = _start_wrapper(REPO_ROOT, parent, registry, port)
    _, health = _wait_for_json(f"http://127.0.0.1:{port}/health")
    assert health["status"] == "ok"
    manifest = json.loads(registry.read_text(encoding="utf-8"))
    root = Path(manifest["canonical_root"])
    assert root.is_dir()

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 128 + signal.SIGTERM, (stdout, stderr)
    assert not registry.exists()
    assert not root.exists()
    with pytest.raises((OSError, urllib.error.URLError)):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5)


def test_sigterm_during_seed_window_cleans_root_without_launching_child(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "acceptance"
    parent.mkdir()
    marker = tmp_path / "seed-created"
    registry = parent / "freqtrade-ai-issue-433-registry-seeding.json"
    port = _free_port()
    code = f"""
import signal
import sys
import time
from pathlib import Path
sys.path.insert(0, {str(REPO_ROOT / "scripts")!r})
import run_local_strategy_lab_acceptance_server as wrapper
original = wrapper.create_seed
def delayed_seed(parent, profile):
    manifest = original(parent, profile)
    Path({str(marker)!r}).write_text(manifest["canonical_root"], encoding="utf-8")
    time.sleep(0.75)
    return manifest
wrapper.create_seed = delayed_seed
raise SystemExit(wrapper.run_server(
    parent=Path({str(parent)!r}),
    host="127.0.0.1",
    port={port},
    profile="complete-current",
    registry=Path({str(registry)!r}),
))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT / "backend",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.02)
    assert marker.exists()
    root = Path(marker.read_text(encoding="utf-8"))
    assert root.is_dir()

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 128 + signal.SIGTERM, (stdout, stderr)
    assert not root.exists()
    assert not registry.exists()
    with pytest.raises((OSError, urllib.error.URLError)):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5)
