import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "local_runtime.py"


def test_make_runtime_commands_use_the_one_project_virtualenv():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "python3 scripts/local_runtime.py" not in makefile
    assert (
        makefile.count(
            "backend/.venv/bin/python scripts/local_runtime.py"
        )
        == 13
    )
    assert "python3 scripts/okx_demo_e2e.py" not in makefile
    assert (
        makefile.count(
            "backend/.venv/bin/python scripts/okx_demo_e2e.py"
        )
        == 2
    )


def load_runtime_module():
    spec = importlib.util.spec_from_file_location("local_runtime", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def loaded_supervisor_control_module(runtime):
    return sys.modules[runtime.suspend_supervisor_control.__module__]


def configure_maintenance_repo(monkeypatch, runtime, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir(mode=0o755)
    state_dir = repo_root / ".freqtrade-ai" / "runtime"
    monkeypatch.setattr(runtime, "REPO_ROOT", repo_root)
    monkeypatch.setattr(runtime, "DEFAULT_RUNTIME_DIR", state_dir)
    return repo_root, state_dir


def fake_supervisor_identity(control, repo_root, *, pid=43210):
    return {
        "supervisor_pid": pid,
        "supervisor_start_token": "1" * 64,
        "supervisor_command_sha256": "2" * 64,
        "supervisor_cwd": str(repo_root),
        "supervisor_launchd_label": control.LAUNCHD_LABEL,
    }


def fake_legacy_child_identity(runtime, service="backend", *, pid=7201):
    return {
        "service": service,
        "pid": pid,
        "pgid": pid,
        "start_token": "4" * 64,
        "command_sha256": "5" * 64,
        "cwd": str(runtime.SERVICE_WORKING_DIRECTORIES[service]),
    }


def enable_v47_supervisor_receipt(monkeypatch, control, repo_root):
    identity = fake_supervisor_identity(control, repo_root)
    monkeypatch.setattr(
        control,
        "SUPERVISOR_RUNTIME_SCHEMA_VERSION",
        "20260813_47",
    )
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(identity),
    )
    return identity


def write_legacy_child_snapshot(
    control,
    state_dir,
    generation,
    request_id,
    identity,
    *,
    children=(),
):
    observation = json.loads(
        (state_dir / control.CONTROL_OBSERVATION_FILE).read_text(
            encoding="utf-8"
        )
    )
    payload = {
        "schema_version": control.LEGACY_CHILD_SNAPSHOT_SCHEMA_VERSION,
        "cutover_generation": generation,
        "request_id": request_id,
        "supervisor_pid": identity["supervisor_pid"],
        "supervisor_start_token": identity["supervisor_start_token"],
        "supervisor_command_sha256": identity["supervisor_command_sha256"],
        "supervisor_cwd": identity["supervisor_cwd"],
        "supervisor_release_sha256": control.SUPERVISOR_RELEASE_SHA256,
        "supervisor_runtime_schema_version": (
            control.SUPERVISOR_RUNTIME_SCHEMA_VERSION
        ),
        "children": list(children),
        "captured_at": observation["observed_at"],
    }
    path = state_dir / control.LEGACY_CHILD_SNAPSHOT_FILE
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return payload


def observed_v46_retirement_fence(monkeypatch, runtime, repo_root, state_dir):
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-retire-legacy-direct",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    control = loaded_supervisor_control_module(runtime)
    legacy_identity = fake_supervisor_identity(control, repo_root, pid=43209)
    identity = {
        **fake_supervisor_identity(control, repo_root, pid=43210),
        "supervisor_start_token": "3" * 64,
    }
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(identity),
    )
    control.observe_supervisor_control(state_dir, trusted_root=repo_root)
    write_legacy_child_snapshot(
        control,
        state_dir,
        suspended["control"]["cutover_generation"],
        "task1-retire-legacy-direct",
        legacy_identity,
    )
    return control, suspended["control"]["cutover_generation"], identity


def test_missing_supervisor_maintenance_state_defaults_active_without_writes(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    _repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )

    status = runtime.read_supervisor_maintenance_status(state_dir)

    assert status["mode"] == "ACTIVE"
    assert status["control"]["source"] == "DEFAULT_MISSING"
    assert status["observed_matches_control"] is True
    assert not state_dir.exists()


def test_maintenance_cli_rejects_noncanonical_state_before_any_action(
    monkeypatch,
    tmp_path,
    capsys,
):
    runtime = load_runtime_module()
    repo_root, _state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    wrong_state = repo_root / "other" / "runtime"
    monkeypatch.setattr(
        runtime,
        "suspend_supervisor_for_migration",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("noncanonical maintenance action executed")
        ),
    )
    monkeypatch.setattr(
        runtime,
        "load_runtime_environment",
        lambda: (_ for _ in ()).throw(
            AssertionError("noncanonical maintenance action read runtime.env")
        ),
    )

    assert runtime.main(
        [
            "supervisor-maintenance-suspend",
            "--runtime-dir",
            str(wrong_state),
            "--request-id",
            "task1-wrong-state",
            "--operator-identity",
            "task1",
            "--reason",
            "retire-legacy-runtime",
            "--target-schema-version",
            "20260813_47",
            "--json",
        ]
    ) == 2
    assert "canonical owner state" in capsys.readouterr().out
    assert not wrong_state.exists()


def test_maintenance_python_contract_rejects_noncanonical_state_before_probe(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, _state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    wrong_state = repo_root / "other" / "runtime"
    control = loaded_supervisor_control_module(runtime)
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("noncanonical wrapper probed canonical owner")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="canonical owner state"):
        runtime.reload_supervisor_for_migration(
            wrong_state,
            cutover_generation="00000000000000000001",
            request_id="task1-wrong-wrapper-state",
        )

    assert not wrong_state.exists()


def test_supervisor_maintenance_cli_never_loads_runtime_environment(
    monkeypatch,
    tmp_path,
    capsys,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    monkeypatch.setattr(
        runtime,
        "load_runtime_environment",
        lambda: (_ for _ in ()).throw(
            AssertionError("maintenance CLI touched runtime.env")
        ),
    )

    assert runtime.main(
        [
            "supervisor-maintenance-suspend",
            "--runtime-dir",
            str(state_dir),
            "--request-id",
            "task1-v47-no-runtime-env",
            "--operator-identity",
            "task1",
            "--reason",
            "strategy-platform-v13-cutover",
            "--target-schema-version",
            "20260813_47",
            "--json",
        ]
    ) == 0
    suspended = json.loads(capsys.readouterr().out)
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    enable_v47_supervisor_receipt(monkeypatch, control, repo_root)
    control.observe_supervisor_control(state_dir, trusted_root=repo_root)

    assert runtime.main(
        [
            "supervisor-maintenance-status",
            "--runtime-dir",
            str(state_dir),
            "--json",
        ]
    ) == 0
    capsys.readouterr()
    assert runtime.main(
        [
            "supervisor-maintenance-resume",
            "--runtime-dir",
            str(state_dir),
            "--cutover-generation",
            generation,
            "--request-id",
            "task1-v47-no-runtime-env",
            "--json",
        ]
    ) == 0
    capsys.readouterr()


def test_all_maintenance_cli_commands_skip_runtime_environment(
    monkeypatch,
    tmp_path,
    capsys,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    monkeypatch.setattr(
        runtime,
        "load_runtime_environment",
        lambda: (_ for _ in ()).throw(
            AssertionError("maintenance CLI touched runtime.env")
        ),
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-no-env-all-commands",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: fake_supervisor_identity(control, repo_root),
    )
    control.observe_supervisor_control(state_dir, trusted_root=repo_root)
    monkeypatch.setattr(
        runtime,
        "reload_supervisor_for_migration",
        lambda *_args, **_kwargs: {"status": "RELOADED"},
    )
    monkeypatch.setattr(
        runtime,
        "stop_legacy_runtime_for_migration",
        lambda *_args, **_kwargs: {"status": "STOPPED"},
    )
    common = [
        "--runtime-dir",
        str(state_dir),
        "--cutover-generation",
        generation,
        "--request-id",
        "task1-no-env-all-commands",
        "--json",
    ]
    assert runtime.main(["supervisor-maintenance-reload-owner", *common]) == 0
    capsys.readouterr()
    assert runtime.main(["supervisor-maintenance-stop-legacy", *common]) == 0
    capsys.readouterr()


def test_supervisor_maintenance_cli_suspend_status_and_resume_cas(
    monkeypatch,
    tmp_path,
    capsys,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    monkeypatch.setattr(
        runtime,
        "load_runtime_environment",
        lambda: (_ for _ in ()).throw(
            AssertionError("suspended action touched runtime.env")
        ),
    )

    suspend_args = [
        "supervisor-maintenance-suspend",
        "--runtime-dir",
        str(state_dir),
        "--request-id",
        "task1-v47-cutover-1",
        "--operator-identity",
        "task1",
        "--reason",
        "strategy-platform-v13-cutover",
        "--target-schema-version",
        "20260813_47",
        "--json",
    ]
    assert runtime.main(suspend_args) == 0
    suspended = json.loads(capsys.readouterr().out)
    generation = suspended["control"]["cutover_generation"]
    assert generation == "00000000000000000001"
    assert suspended["status"] == "MIGRATION_SUSPENDED"
    assert suspended["observed_matches_control"] is False

    state_path = state_dir / "supervisor-control.json"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    control = loaded_supervisor_control_module(runtime)
    assert set(stored) == control.CONTROL_FIELDS
    assert stored["target_schema_version"] == "20260813_47"
    assert "requested_at" in stored
    assert "created_at" not in stored
    ledger_path = state_dir / "supervisor-control.generation.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["last_issued_generation"] == generation
    assert ledger["commit_state"] == "COMMITTED"
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600

    assert runtime.main(
        [
            "supervisor-maintenance-status",
            "--runtime-dir",
            str(state_dir),
            "--json",
        ]
    ) == 0
    before_observation = json.loads(capsys.readouterr().out)
    assert before_observation["mode"] == "MIGRATION_SUSPENDED"
    assert before_observation["observed_matches_control"] is False

    stale_resume = [
        "supervisor-maintenance-resume",
        "--runtime-dir",
        str(state_dir),
        "--cutover-generation",
        generation,
        "--request-id",
        "stale-request",
        "--json",
    ]
    assert runtime.main(stale_resume) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "BLOCKED"
    assert json.loads(state_path.read_text(encoding="utf-8"))["mode"] == (
        "MIGRATION_SUSPENDED"
    )

    matching_without_receipt = [
        "supervisor-maintenance-resume",
        "--runtime-dir",
        str(state_dir),
        "--cutover-generation",
        generation,
        "--request-id",
        "task1-v47-cutover-1",
        "--json",
    ]
    assert runtime.main(matching_without_receipt) == 2
    no_receipt = json.loads(capsys.readouterr().out)
    assert no_receipt["status"] == "BLOCKED"
    assert "OBSERVATION_TUPLE_MISMATCH" in no_receipt["reason"]

    enable_v47_supervisor_receipt(monkeypatch, control, repo_root)
    control.observe_supervisor_control(state_dir, trusted_root=repo_root)

    assert runtime.main(
        [
            "supervisor-maintenance-resume",
            "--runtime-dir",
            str(state_dir),
            "--cutover-generation",
            generation,
            "--request-id",
            "task1-v47-cutover-1",
            "--json",
        ]
    ) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "ACTIVE"
    assert resumed["resume_cas"] == "MATCHED"
    assert resumed["control"]["cutover_generation"] == generation
    assert resumed["control"]["request_id"] == "task1-v47-cutover-1"
    assert resumed["control"]["requested_at"] == stored["requested_at"]

    assert runtime.main(
        [
            "supervisor-maintenance-resume",
            "--runtime-dir",
            str(state_dir),
            "--cutover-generation",
            generation,
            "--request-id",
            "task1-v47-cutover-1",
            "--json",
        ]
    ) == 2
    already_active = json.loads(capsys.readouterr().out)
    assert already_active["status"] == "BLOCKED"


def test_supervisor_maintenance_receipt_is_durable_and_generation_scoped(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-v47-cutover-2",
        operator_identity="task1",
        reason="strategy-platform-v13-cutover",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    with pytest.raises(runtime.RuntimeBlocked, match="already suspended"):
        runtime.suspend_supervisor_for_migration(
            state_dir,
            request_id="task1-v47-cutover-reused",
            operator_identity="task1",
            reason="strategy-platform-v13-cutover",
            target_schema_version="20260813_47",
        )

    identity = enable_v47_supervisor_receipt(monkeypatch, control, repo_root)
    observation = control.observe_supervisor_control(
        state_dir,
        trusted_root=repo_root,
    )
    assert observation["mode"] == "MIGRATION_SUSPENDED"
    assert observation["cutover_generation"] == generation
    assert observation["observed_matches_control"] is True
    receipt_path = state_dir / "supervisor-control.observed.json"
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "supervisor-control-observation-v2"
    assert receipt["supervisor_pid"] == identity["supervisor_pid"]
    assert receipt["supervisor_start_token"] == identity[
        "supervisor_start_token"
    ]
    assert receipt["observed_generation"] == generation
    assert receipt["observed_request_id"] == "task1-v47-cutover-2"
    assert receipt["observed_mode"] == "MIGRATION_SUSPENDED"
    assert receipt["supervisor_runtime_schema_version"] == "20260813_47"
    status = runtime.read_supervisor_maintenance_status(state_dir)
    assert status["observed_matches_control"] is True
    assert status["resume_eligible"] is True

    runtime.resume_supervisor_after_migration(
        state_dir,
        cutover_generation=generation,
        request_id="task1-v47-cutover-2",
    )
    next_suspend = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-v47-cutover-3",
        operator_identity="task1",
        reason="strategy-platform-v13-cutover",
        target_schema_version="20260813_47",
    )
    assert next_suspend["control"]["cutover_generation"] == (
        "00000000000000000002"
    )


def test_v46_supervisor_can_ack_suspend_but_cannot_resume_v47(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-retire-old-v46",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: fake_supervisor_identity(control, repo_root),
    )

    observed = control.observe_supervisor_control(
        state_dir,
        trusted_root=repo_root,
    )

    assert observed["observed_matches_control"] is True
    assert observed["resume_eligible"] is False
    status = runtime.read_supervisor_maintenance_status(state_dir)
    assert status["resume_block_reason"] == (
        "SUPERVISOR_SCHEMA_CAPABILITY_MISMATCH"
    )
    with pytest.raises(runtime.RuntimeBlocked, match="SCHEMA_CAPABILITY_MISMATCH"):
        runtime.resume_supervisor_after_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-retire-old-v46",
        )


def test_suspended_fence_blocks_runtime_up_and_thaw_before_sensitive_actions(
    monkeypatch,
    tmp_path,
    capsys,
):
    runtime = load_runtime_module()
    _repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-block-manual-recovery",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    monkeypatch.setattr(
        runtime,
        "load_runtime_environment",
        lambda: (_ for _ in ()).throw(
            AssertionError("suspended action touched runtime.env")
        ),
    )
    monkeypatch.setattr(
        runtime,
        "start",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("suspended runtime attempted start")
        ),
    )
    monkeypatch.setattr(
        runtime,
        "thaw_okx_openings",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("suspended runtime attempted thaw")
        ),
    )

    alternate_state = state_dir.parent / "alternate-runtime"
    for command in ("up", "supervisor-thaw-openings"):
        assert runtime.main(
            [command, "--runtime-dir", str(alternate_state), "--json"]
        ) == 2
        blocked = json.loads(capsys.readouterr().out)
        assert blocked["status"] == "BLOCKED"
        assert "MIGRATION_SUSPENDED" in blocked["reason"]
    assert not alternate_state.exists()


def test_supervisor_maintenance_generation_is_monotonic_and_target_is_exact(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control = loaded_supervisor_control_module(runtime)
    enable_v47_supervisor_receipt(monkeypatch, control, repo_root)
    generations = []
    for index in range(1, 6):
        request_id = "task1-v47-series-{}".format(index)
        suspended = runtime.suspend_supervisor_for_migration(
            state_dir,
            request_id=request_id,
            operator_identity="task1",
            reason="strategy-platform-v13-cutover",
            target_schema_version="20260813_47",
        )
        generation = suspended["control"]["cutover_generation"]
        generations.append(generation)
        control.observe_supervisor_control(state_dir, trusted_root=repo_root)
        runtime.resume_supervisor_after_migration(
            state_dir,
            cutover_generation=generation,
            request_id=request_id,
        )

    assert generations == [str(value).zfill(20) for value in range(1, 6)]
    with pytest.raises(runtime.RuntimeBlocked, match="must be 20260813_47"):
        runtime.suspend_supervisor_for_migration(
            state_dir,
            request_id="task1-wrong-target",
            operator_identity="task1",
            reason="strategy-platform-v13-cutover",
            target_schema_version="20260813_48",
        )


def test_pending_generation_is_repaired_only_by_the_same_request(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    _repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control = loaded_supervisor_control_module(runtime)
    real_write = control._write_json_file
    failed = False

    def fail_control_once(directory, name, payload):
        nonlocal failed
        if name == control.CONTROL_STATE_FILE and not failed:
            failed = True
            raise control.SupervisorControlBlocked("injected control commit failure")
        return real_write(directory, name, payload)

    monkeypatch.setattr(control, "_write_json_file", fail_control_once)
    with pytest.raises(runtime.RuntimeBlocked, match="injected"):
        runtime.suspend_supervisor_for_migration(
            state_dir,
            request_id="task1-crash-repair",
            operator_identity="task1",
            reason="strategy-platform-v13-cutover",
            target_schema_version="20260813_47",
        )
    ledger = json.loads(
        (state_dir / "supervisor-control.generation.json").read_text(
            encoding="utf-8"
        )
    )
    assert ledger["commit_state"] == "PENDING_CONTROL"
    assert ledger["last_issued_generation"] == "00000000000000000001"
    with pytest.raises(runtime.RuntimeBlocked, match="different generation"):
        runtime.suspend_supervisor_for_migration(
            state_dir,
            request_id="task1-different-request",
            operator_identity="task1",
            reason="strategy-platform-v13-cutover",
            target_schema_version="20260813_47",
        )
    repaired = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-crash-repair",
        operator_identity="task1",
        reason="strategy-platform-v13-cutover",
        target_schema_version="20260813_47",
    )
    assert repaired["control"]["cutover_generation"] == (
        "00000000000000000001"
    )


def test_missing_control_with_history_and_malformed_receipt_fail_closed(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    _repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-missing-control",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    (state_dir / "supervisor-control.json").unlink()
    with pytest.raises(runtime.RuntimeBlocked, match="history is incomplete"):
        runtime.read_supervisor_maintenance_status(state_dir)

    state_dir_2 = tmp_path / "repo" / "second" / "runtime"
    state_dir_2.mkdir(parents=True, mode=0o700)
    receipt = state_dir_2 / "supervisor-control.observed.json"
    receipt.write_text("{malformed\n", encoding="utf-8")
    receipt.chmod(0o600)
    control = loaded_supervisor_control_module(runtime)
    with pytest.raises(control.SupervisorControlBlocked, match="malformed"):
        control.supervisor_control_status(state_dir_2, trusted_root=tmp_path / "repo")


def test_supervisor_maintenance_state_rejects_symlink_and_unsafe_parent(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    state_dir.mkdir(parents=True, mode=0o700)
    target = repo_root / "outside.json"
    target.write_text("outside-must-remain\n", encoding="utf-8")
    target.chmod(0o600)
    (state_dir / "supervisor-control.json").symlink_to(target)

    with pytest.raises(runtime.RuntimeBlocked, match="0600 regular file"):
        runtime.read_supervisor_maintenance_status(state_dir)
    assert target.read_text(encoding="utf-8") == "outside-must-remain\n"

    (state_dir / "supervisor-control.json").unlink()
    state_dir.chmod(0o770)
    with pytest.raises(runtime.RuntimeBlocked, match="owner-controlled"):
        runtime.suspend_supervisor_for_migration(
            state_dir,
            request_id="task1-v47-cutover-4",
            operator_identity="task1",
            reason="strategy-platform-v13-cutover",
            target_schema_version="20260813_47",
        )

    state_dir.chmod(0o700)
    outside = repo_root / "outside-runtime"
    outside.mkdir(mode=0o700)
    symlink_parent = repo_root / "linked-runtime"
    symlink_parent.symlink_to(outside, target_is_directory=True)
    control = loaded_supervisor_control_module(runtime)
    with pytest.raises(control.SupervisorControlBlocked, match="symlink"):
        control.suspend_supervisor_control(
            symlink_parent / "runtime",
            request_id="task1-v47-cutover-symlink",
            operator_identity="task1",
            reason="strategy-platform-v13-cutover",
            target_schema_version="20260813_47",
            trusted_root=repo_root,
        )


def test_supervisor_maintenance_atomic_write_fsyncs_file_and_directory(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    _repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control = loaded_supervisor_control_module(runtime)
    real_fsync = control.os.fsync
    fsynced = []

    def tracking_fsync(descriptor):
        fsynced.append(os.fstat(descriptor).st_mode)
        return real_fsync(descriptor)

    monkeypatch.setattr(control.os, "fsync", tracking_fsync)
    runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-v47-cutover-5",
        operator_identity="task1",
        reason="strategy-platform-v13-cutover",
        target_schema_version="20260813_47",
    )

    assert any(stat.S_ISREG(mode) for mode in fsynced)
    assert any(stat.S_ISDIR(mode) for mode in fsynced)
    assert not list(state_dir.glob(".supervisor-control.json.*.tmp"))
    assert not list(state_dir.glob(".supervisor-control.generation.json.*.tmp"))


def test_writer_lock_probe_fails_closed_for_unsafe_or_malformed_evidence(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    lock_path = tmp_path / runtime.OKX_WRITER_LOCK_FILE
    target = tmp_path / "target-lock"
    target.write_text("123\n", encoding="utf-8")
    target.chmod(0o600)
    lock_path.symlink_to(target)
    with pytest.raises(runtime.RuntimeBlocked, match="inspected safely"):
        runtime._writer_lock_holder(tmp_path)

    lock_path.unlink()
    lock_path.write_text("not-a-pid\n", encoding="utf-8")
    lock_path.chmod(0o600)
    real_flock = runtime.fcntl.flock

    def held_lock(descriptor, operation):
        if operation & runtime.fcntl.LOCK_NB:
            raise BlockingIOError()
        return real_flock(descriptor, operation)

    monkeypatch.setattr(runtime.fcntl, "flock", held_lock)
    with pytest.raises(runtime.RuntimeBlocked, match="invalid owner"):
        runtime._writer_lock_holder(tmp_path)


def test_supervisor_maintenance_files_are_forced_to_0600(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control = loaded_supervisor_control_module(runtime)
    enable_v47_supervisor_receipt(monkeypatch, control, repo_root)
    runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-hostile-umask",
        operator_identity="task1",
        reason="strategy-platform-v13-cutover",
        target_schema_version="20260813_47",
    )
    control.observe_supervisor_control(state_dir, trusted_root=repo_root)
    for name in (
        "supervisor-control.json",
        "supervisor-control.generation.json",
        "supervisor-control.observed.json",
        "supervisor-control.lock",
    ):
        assert stat.S_IMODE((state_dir / name).stat().st_mode) == 0o600


def test_stop_legacy_commits_immutable_retirement_and_blocks_recovery(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control, generation, _identity = observed_v46_retirement_fence(
        monkeypatch,
        runtime,
        repo_root,
        state_dir,
    )
    stopped = {
        "services": [
            {"service": service, "status": "stopped", "pid": 7000 + index}
            for index, service in enumerate(runtime.SERVICE_STOP_ORDER)
        ]
    }
    calls = []
    monkeypatch.setattr(
        runtime,
        "stop_legacy_snapshot_processes",
        lambda _state_dir, children: (
            calls.append(("stop", list(children))) or stopped
        ),
    )
    monkeypatch.setattr(
        runtime,
        "prove_legacy_snapshot_retirement",
        lambda _state_dir, children: calls.append(("proof", list(children))),
    )

    result = runtime.stop_legacy_runtime_for_migration(
        state_dir,
        cutover_generation=generation,
        request_id="task1-retire-legacy-direct",
    )

    assert result["status"] == "LEGACY_RUNTIME_STOPPED"
    assert result["retirement_committed"] is True
    assert calls == [("stop", []), ("proof", [])]
    retirement_path = state_dir / control.LEGACY_RETIREMENT_FILE
    assert stat.S_IMODE(retirement_path.stat().st_mode) == 0o600
    retirement = json.loads(retirement_path.read_text(encoding="utf-8"))
    assert retirement["cutover_generation"] == generation
    assert retirement["services_terminal"] is True
    assert retirement["managed_orphans_absent"] is True
    assert retirement["service_ports_unbound"] is True
    assert retirement["writer_lock_unheld"] is True
    snapshot = json.loads(
        (state_dir / control.LEGACY_CHILD_SNAPSHOT_FILE).read_text(
            encoding="utf-8"
        )
    )
    assert retirement["legacy_child_snapshot_sha256"] == (
        control._legacy_child_snapshot_sha256(snapshot)
    )
    observation = json.loads(
        (state_dir / control.CONTROL_OBSERVATION_FILE).read_text(
            encoding="utf-8"
        )
    )
    assert retirement["supervisor_observation_sha256"] == (
        control._supervisor_observation_sha256(observation)
    )

    status = runtime.read_supervisor_maintenance_status(state_dir)
    assert status["status"] == "LEGACY_RETIRED"
    assert status["legacy_retired"] is True
    assert status["resume_eligible"] is False
    assert status["resume_block_reason"] == "LEGACY_RETIRED"
    assert status["legacy_retirement_receipt"] == retirement

    with pytest.raises(runtime.RuntimeBlocked, match="permanently retired"):
        runtime.resume_supervisor_after_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-retire-legacy-direct",
        )
    with pytest.raises(runtime.RuntimeBlocked, match="permanently retired"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-retire-legacy-direct",
        )
    with pytest.raises(runtime.RuntimeBlocked, match="permanently retired"):
        runtime.suspend_supervisor_for_migration(
            state_dir,
            request_id="task1-restart-retired",
            operator_identity="task1",
            reason="retire-legacy-runtime",
            target_schema_version="20260813_47",
        )
    with pytest.raises(runtime.RuntimeBlocked, match="permanently retired"):
        runtime.stop_legacy_runtime_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-retire-legacy-direct",
        )
    assert json.loads(retirement_path.read_text(encoding="utf-8")) == retirement


@pytest.mark.parametrize(
    "tamper_field",
    (
        None,
        "supervisor_pid",
        "supervisor_start_token",
        "supervisor_release_sha256",
        "legacy_child_snapshot_sha256",
        "supervisor_observation_sha256",
    ),
)
def test_retirement_status_requires_complete_static_observation_provenance(
    monkeypatch,
    tmp_path,
    tamper_field,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control, generation, _identity = observed_v46_retirement_fence(
        monkeypatch,
        runtime,
        repo_root,
        state_dir,
    )
    stopped = {"services": []}
    monkeypatch.setattr(
        runtime,
        "stop_legacy_snapshot_processes",
        lambda _state_dir, _children: stopped,
    )
    monkeypatch.setattr(
        runtime,
        "prove_legacy_snapshot_retirement",
        lambda _state_dir, _children: None,
    )
    runtime.stop_legacy_runtime_for_migration(
        state_dir,
        cutover_generation=generation,
        request_id="task1-retire-legacy-direct",
    )
    observation_path = state_dir / control.CONTROL_OBSERVATION_FILE
    if tamper_field is None:
        observation_path.unlink()
    else:
        retirement_path = state_dir / control.LEGACY_RETIREMENT_FILE
        retirement = json.loads(retirement_path.read_text(encoding="utf-8"))
        retirement[tamper_field] = (
            retirement[tamper_field] + 1
            if tamper_field == "supervisor_pid"
            else "f" * 64
        )
        retirement_path.write_text(
            json.dumps(retirement, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        retirement_path.chmod(0o600)

    with pytest.raises(runtime.RuntimeBlocked, match="provenance is incomplete"):
        runtime.read_supervisor_maintenance_status(state_dir)


@pytest.mark.parametrize("tamper_kind", ("children", "release", "time"))
def test_retirement_status_rehashes_and_validates_child_snapshot_provenance(
    monkeypatch,
    tmp_path,
    tamper_kind,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control, generation, _identity = observed_v46_retirement_fence(
        monkeypatch,
        runtime,
        repo_root,
        state_dir,
    )
    monkeypatch.setattr(
        runtime,
        "stop_legacy_snapshot_processes",
        lambda _state_dir, _children: {"services": []},
    )
    monkeypatch.setattr(
        runtime,
        "prove_legacy_snapshot_retirement",
        lambda _state_dir, _children: None,
    )
    runtime.stop_legacy_runtime_for_migration(
        state_dir,
        cutover_generation=generation,
        request_id="task1-retire-legacy-direct",
    )
    snapshot_path = state_dir / control.LEGACY_CHILD_SNAPSHOT_FILE
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if tamper_kind == "children":
        snapshot["children"] = [
            fake_legacy_child_identity(runtime, pid=7991)
        ]
    elif tamper_kind == "release":
        snapshot["supervisor_release_sha256"] = "f" * 64
    else:
        snapshot["captured_at"] = "2099-01-01T00:00:00Z"
    snapshot_path.write_text(
        json.dumps(snapshot, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    snapshot_path.chmod(0o600)

    with pytest.raises(runtime.RuntimeBlocked, match="provenance is incomplete"):
        runtime.read_supervisor_maintenance_status(state_dir)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("supervisor_command_sha256", "e" * 64),
        ("supervisor_cwd", "/tmp/replaced-supervisor-cwd"),
        ("supervisor_instance_id", "12345678-1234-4234-8234-123456789abc"),
        ("observed_at", "2001-01-01T00:00:00Z"),
    ),
)
def test_retirement_status_rehashes_complete_replacement_observation(
    monkeypatch,
    tmp_path,
    field,
    value,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control, generation, _identity = observed_v46_retirement_fence(
        monkeypatch,
        runtime,
        repo_root,
        state_dir,
    )
    monkeypatch.setattr(
        runtime,
        "stop_legacy_snapshot_processes",
        lambda _state_dir, _children: {"services": []},
    )
    monkeypatch.setattr(
        runtime,
        "prove_legacy_snapshot_retirement",
        lambda _state_dir, _children: None,
    )
    runtime.stop_legacy_runtime_for_migration(
        state_dir,
        cutover_generation=generation,
        request_id="task1-retire-legacy-direct",
    )
    observation_path = state_dir / control.CONTROL_OBSERVATION_FILE
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    observation[field] = value
    observation_path.write_text(
        json.dumps(observation, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    observation_path.chmod(0o600)

    with pytest.raises(runtime.RuntimeBlocked, match="provenance is incomplete"):
        runtime.read_supervisor_maintenance_status(state_dir)


def test_stop_legacy_failure_or_read_only_authorization_never_writes_retirement(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control, generation, _identity = observed_v46_retirement_fence(
        monkeypatch,
        runtime,
        repo_root,
        state_dir,
    )
    authorization = control.authorize_legacy_runtime_stop(
        state_dir,
        cutover_generation=generation,
        request_id="task1-retire-legacy-direct",
        trusted_root=repo_root,
    )
    assert authorization["status"] == "AUTHORIZED"
    retirement_path = state_dir / control.LEGACY_RETIREMENT_FILE
    assert not retirement_path.exists()

    monkeypatch.setattr(
        runtime,
        "stop_legacy_snapshot_processes",
        lambda _state_dir, _children: {
            "services": [
                {"service": "backend", "status": "BLOCKED", "pid": 7010}
            ]
        },
    )
    monkeypatch.setattr(
        runtime,
        "prove_legacy_snapshot_retirement",
        lambda *_args: (_ for _ in ()).throw(
            runtime.RuntimeBlocked("injected terminal proof failure")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="terminal proof failure"):
        runtime.stop_legacy_runtime_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-retire-legacy-direct",
        )
    assert not retirement_path.exists()


def test_stop_legacy_refuses_target_schema_owner_before_signaling(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control = loaded_supervisor_control_module(runtime)
    monkeypatch.setattr(
        control,
        "SUPERVISOR_RUNTIME_SCHEMA_VERSION",
        "20260813_47",
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-target-owner",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    identity = fake_supervisor_identity(control, repo_root)
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(identity),
    )
    control.observe_supervisor_control(state_dir, trusted_root=repo_root)
    write_legacy_child_snapshot(
        control,
        state_dir,
        generation,
        "task1-target-owner",
        {
            **identity,
            "supervisor_pid": identity["supervisor_pid"] - 1,
            "supervisor_start_token": "a" * 64,
        },
    )
    monkeypatch.setattr(
        runtime,
        "stop_legacy_snapshot_processes",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("target-schema owner was signaled")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="pre-target"):
        runtime.stop_legacy_runtime_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-target-owner",
        )


def test_stop_legacy_requires_generation_snapshot_before_any_signal(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control, generation, _identity = observed_v46_retirement_fence(
        monkeypatch,
        runtime,
        repo_root,
        state_dir,
    )
    (state_dir / control.LEGACY_CHILD_SNAPSHOT_FILE).unlink()
    monkeypatch.setattr(
        runtime,
        "stop_legacy_snapshot_processes",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("missing snapshot reached signal phase")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="generation-bound"):
        runtime.stop_legacy_runtime_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-retire-legacy-direct",
        )


def test_generation_child_snapshot_permanently_refuses_resume(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    _control, generation, _identity = observed_v46_retirement_fence(
        monkeypatch,
        runtime,
        repo_root,
        state_dir,
    )

    with pytest.raises(runtime.RuntimeBlocked, match="old generation resume"):
        runtime.resume_supervisor_after_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-retire-legacy-direct",
        )


def test_stop_requires_replacement_owner_start_token_after_bootstrap(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control, generation, _identity = observed_v46_retirement_fence(
        monkeypatch,
        runtime,
        repo_root,
        state_dir,
    )
    observation = json.loads(
        (state_dir / control.CONTROL_OBSERVATION_FILE).read_text(
            encoding="utf-8"
        )
    )
    snapshot_path = state_dir / control.LEGACY_CHILD_SNAPSHOT_FILE
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["supervisor_start_token"] = observation[
        "supervisor_start_token"
    ]
    snapshot_path.write_text(
        json.dumps(snapshot, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    snapshot_path.chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "stop_legacy_snapshot_processes",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("same-owner snapshot reached signal path")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="provenance"):
        runtime.stop_legacy_runtime_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-retire-legacy-direct",
        )


def test_snapshot_stop_blocks_unexpected_candidate_before_any_signal(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    state_dir = tmp_path / "runtime"
    state_dir.mkdir()
    expected = fake_legacy_child_identity(runtime, pid=7201)
    unexpected = fake_legacy_child_identity(runtime, pid=7202)
    monkeypatch.setattr(
        runtime,
        "_legacy_child_process_snapshot_once",
        lambda *_args, **_kwargs: [dict(unexpected)],
    )
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("unexpected candidate caused a signal")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="unexpected.*zero signals"):
        runtime.stop_legacy_snapshot_processes(state_dir, [expected])


def test_snapshot_stop_blocks_start_token_change_before_any_signal(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    state_dir = tmp_path / "runtime"
    state_dir.mkdir()
    expected = fake_legacy_child_identity(runtime, pid=7203)
    reused = {**expected, "start_token": "6" * 64}
    monkeypatch.setattr(
        runtime,
        "_legacy_child_process_snapshot_once",
        lambda *_args, **_kwargs: [dict(reused)],
    )
    signals = []
    monkeypatch.setattr(runtime.os, "killpg", lambda *_args: signals.append(_args))

    with pytest.raises(runtime.RuntimeBlocked, match="unexpected.*zero signals"):
        runtime.stop_legacy_snapshot_processes(state_dir, [expected])

    assert signals == []


def test_snapshot_stop_can_signal_snapshotted_zombie_leader_live_group(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    state_dir = tmp_path / "runtime"
    state_dir.mkdir()
    expected = fake_legacy_child_identity(runtime, pid=7204)
    monkeypatch.setattr(
        runtime,
        "_legacy_child_process_snapshot_once",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_ZOMBIE,
    )
    group_states = iter(
        (
            runtime.PROCESS_STATE_RUNNING,
            runtime.PROCESS_STATE_RUNNING,
            runtime.PROCESS_STATE_EXITED,
            runtime.PROCESS_STATE_EXITED,
        )
    )
    monkeypatch.setattr(
        runtime,
        "process_group_state",
        lambda _pid: next(group_states),
    )
    signals = []
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda pgid, sent_signal: signals.append((pgid, sent_signal)),
    )

    result = runtime.stop_legacy_snapshot_processes(state_dir, [expected])

    assert signals == [(7204, runtime.signal.SIGTERM)]
    assert result["services"][3] == {
        "service": "backend",
        "status": "stopped",
        "pid": 7204,
    }


def test_retirement_proof_blocks_nonempty_legacy_service_port(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    state_dir = tmp_path / "runtime"
    state_dir.mkdir()
    monkeypatch.setattr(
        runtime,
        "_legacy_child_process_snapshot_once",
        lambda *_args, **_kwargs: [],
    )
    port_snapshots = iter(
        (
            {runtime.BACKEND_PORT: [7301], runtime.FRONTEND_PORT: []},
            {runtime.BACKEND_PORT: [7301], runtime.FRONTEND_PORT: []},
        )
    )
    monkeypatch.setattr(
        runtime,
        "legacy_service_port_owners",
        lambda: next(port_snapshots),
    )
    monkeypatch.setattr(
        runtime,
        "_writer_lock_holder",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("port proof did not block before receipt")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="port ownership"):
        runtime.prove_legacy_snapshot_retirement(state_dir, [])


def test_legacy_service_port_proof_uses_bounded_local_listener_metadata(
    monkeypatch,
):
    runtime = load_runtime_module()
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    assert runtime.legacy_service_port_owners() == {
        runtime.BACKEND_PORT: [],
        runtime.FRONTEND_PORT: [],
    }
    assert [item[0] for item in commands] == [
        [
            "/usr/sbin/lsof",
            "-nP",
            "-iTCP:8000",
            "-sTCP:LISTEN",
            "-Fp",
        ],
        [
            "/usr/sbin/lsof",
            "-nP",
            "-iTCP:5173",
            "-sTCP:LISTEN",
            "-Fp",
        ],
    ]
    assert all(
        item[1]["timeout"] == runtime.PROCESS_STATE_PROBE_TIMEOUT_SECONDS
        and item[1]["env"] == runtime.SAFE_PROCESS_PROBE_ENV
        for item in commands
    )


def test_reload_owner_drains_exact_supervisor_without_touching_children(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-reload-old-owner",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    previous = fake_supervisor_identity(control, repo_root, pid=7101)
    replacement = {
        **fake_supervisor_identity(control, repo_root, pid=7102),
        "supervisor_start_token": "3" * 64,
    }
    identities = iter((previous, previous, replacement))
    calls = []
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(next(identities)),
    )
    monkeypatch.setattr(
        control,
        "_pause_and_drain_supervisor",
        lambda identity, root: calls.append(("drain", identity, root)),
    )
    monkeypatch.setattr(
        control,
        "_kickstart_supervisor_owner",
        lambda: calls.append(("kickstart", control.LAUNCHD_LABEL)),
    )
    monkeypatch.setattr(
        runtime,
        "capture_legacy_child_process_snapshot",
        lambda observed_state_dir: (
            calls.append(("snapshot", observed_state_dir)) or []
        ),
    )

    result = runtime.reload_supervisor_for_migration(
        state_dir,
        cutover_generation=generation,
        request_id="task1-reload-old-owner",
    )

    assert result["status"] == "SUPERVISOR_OWNER_RELOADED"
    assert result["previous_supervisor_pid"] == 7101
    assert result["supervisor_pid"] == 7102
    assert result["bootstrap_without_observation"] is True
    assert calls == [
        ("drain", previous, repo_root),
        ("snapshot", state_dir),
        ("kickstart", control.LAUNCHD_LABEL),
    ]
    snapshot_path = state_dir / control.LEGACY_CHILD_SNAPSHOT_FILE
    assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o600
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["cutover_generation"] == generation
    assert snapshot["request_id"] == "task1-reload-old-owner"
    assert snapshot["supervisor_pid"] == 7101
    assert snapshot["children"] == []


def test_reload_pause_waits_for_runtime_child_without_reading_job_environment(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    control = loaded_supervisor_control_module(runtime)
    repo_root = tmp_path / "Repo With Spaces"
    repo_root.mkdir(mode=0o755)
    identity = fake_supervisor_identity(control, repo_root, pid=7110)
    probe_commands = []
    children = iter(([7111], []))
    monkeypatch.setattr(
        control,
        "_run_probe",
        lambda command: (
            probe_commands.append(command)
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(identity),
    )
    monkeypatch.setattr(
        control,
        "_supervisor_runtime_children",
        lambda *_args: next(children),
    )
    monkeypatch.setattr(control.time, "sleep", lambda _seconds: None)

    control._pause_and_drain_supervisor(identity, repo_root)

    assert probe_commands == [
        [
            "/bin/launchctl",
            "kill",
            "SIGSTOP",
            "gui/{}/{}".format(os.getuid(), control.LAUNCHD_LABEL),
        ]
    ]

    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout="7110\t0\t{}\n".format(control.LAUNCHD_LABEL),
            stderr="",
        )

    monkeypatch.setattr(control.subprocess, "run", fake_run)
    monkeypatch.setattr(
        control,
        "_run_probe",
        lambda command: fake_run(
            command,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        ),
    )
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "must-not-be-inherited")

    assert control._launchd_snapshot() == (control.LAUNCHD_LABEL, 7110)
    assert observed["command"] == ["/bin/launchctl", "list"]
    assert observed["environment"] == {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LC_ALL": "C",
    }


def test_reload_child_snapshot_reads_command_only_for_exact_direct_child(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    control = loaded_supervisor_control_module(runtime)
    repo_root = tmp_path / "Repo With Spaces"
    repo_root.mkdir(mode=0o755)
    commands = []
    runtime_script = repo_root / "scripts" / "local_runtime.py"

    def fake_probe(command):
        commands.append(command)
        if command == ["/bin/ps", "-ww", "-axo", "pid=,ppid=,state="]:
            return SimpleNamespace(
                returncode=0,
                stdout="8001 8000 S\n9001 1 S\n",
                stderr="",
            )
        if command == ["/bin/ps", "-ww", "-p", "8001", "-o", "ppid=,state="]:
            return SimpleNamespace(returncode=0, stdout="8000 S\n", stderr="")
        pytest.fail("unexpected process snapshot: {}".format(command))

    monkeypatch.setattr(control, "_run_probe", fake_probe)
    monkeypatch.setattr(
        control,
        "_ps_value",
        lambda pid, field: (
            "python {} up --json".format(runtime_script)
            if pid == 8001 and field == "command"
            else pytest.fail("unexpected child command probe")
        ),
    )
    monkeypatch.setattr(control, "_process_cwd", lambda _pid: str(repo_root))

    assert control._supervisor_runtime_children(8000, repo_root) == [8001]
    assert commands == [
        ["/bin/ps", "-ww", "-axo", "pid=,ppid=,state="],
        ["/bin/ps", "-ww", "-p", "8001", "-o", "ppid=,state="],
    ]
    assert all("command" not in item[-1] for item in commands)

    monkeypatch.setattr(control, "_process_cwd", lambda _pid: "/tmp")
    with pytest.raises(
        control.SupervisorControlBlocked,
        match="unexpected live child",
    ):
        control._supervisor_runtime_children(8000, repo_root)


def test_reload_drain_timeout_keeps_pre_fence_owner_paused(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    control = loaded_supervisor_control_module(runtime)
    repo_root = tmp_path / "Repo With Spaces"
    repo_root.mkdir(mode=0o755)
    identity = fake_supervisor_identity(control, repo_root, pid=7115)
    probe_commands = []
    times = iter((0.0, control.SUPERVISOR_DRAIN_TIMEOUT_SECONDS + 1.0))
    monkeypatch.setattr(
        control,
        "_run_probe",
        lambda command: (
            probe_commands.append(command)
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(identity),
    )
    monkeypatch.setattr(
        control,
        "_supervisor_runtime_children",
        lambda *_args: [7116],
    )
    monkeypatch.setattr(control.time, "monotonic", lambda: next(times))

    with pytest.raises(
        control.SupervisorControlBlocked,
        match="owner remains paused",
    ):
        control._pause_and_drain_supervisor(identity, repo_root)

    assert probe_commands == [
        [
            "/bin/launchctl",
            "kill",
            "SIGSTOP",
            "gui/{}/{}".format(os.getuid(), control.LAUNCHD_LABEL),
        ]
    ]


def test_reload_kickstart_failure_never_resumes_pre_fence_owner(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-reload-failure",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    previous = fake_supervisor_identity(control, repo_root, pid=7117)
    pauses = []
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(previous),
    )
    control.observe_supervisor_control(state_dir, trusted_root=repo_root)

    def pause_owner(identity, _root):
        pauses.append(identity["supervisor_pid"])

    monkeypatch.setattr(control, "_pause_and_drain_supervisor", pause_owner)
    monkeypatch.setattr(
        runtime,
        "capture_legacy_child_process_snapshot",
        lambda _state_dir: [],
    )
    monkeypatch.setattr(
        control,
        "_kickstart_supervisor_owner",
        lambda: (_ for _ in ()).throw(
            control.SupervisorControlBlocked("kickstart failed")
        ),
    )
    with pytest.raises(runtime.RuntimeBlocked, match="kickstart failed"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-reload-failure",
        )

    assert pauses == [7117]


def test_reload_snapshot_failure_keeps_owner_paused_and_never_kickstarts(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-snapshot-failure",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    previous = fake_supervisor_identity(control, repo_root, pid=7118)
    calls = []
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(previous),
    )
    control.observe_supervisor_control(state_dir, trusted_root=repo_root)
    monkeypatch.setattr(
        control,
        "_pause_and_drain_supervisor",
        lambda *_args: calls.append("paused"),
    )
    monkeypatch.setattr(
        runtime,
        "capture_legacy_child_process_snapshot",
        lambda _state_dir: (_ for _ in ()).throw(
            runtime.RuntimeBlocked("snapshot race")
        ),
    )
    monkeypatch.setattr(
        control,
        "_kickstart_supervisor_owner",
        lambda: calls.append("kickstart"),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="snapshot race"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-snapshot-failure",
        )

    assert calls == ["paused"]
    assert not (state_dir / control.LEGACY_CHILD_SNAPSHOT_FILE).exists()


def test_reload_bootstrap_retry_reuses_snapshot_for_same_paused_owner(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-bootstrap-retry",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    previous = fake_supervisor_identity(control, repo_root, pid=7119)
    replacement = {
        **fake_supervisor_identity(control, repo_root, pid=7120),
        "supervisor_start_token": "7" * 64,
    }
    probes = iter((previous, previous, previous, previous, replacement))
    calls = []
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(next(probes)),
    )
    monkeypatch.setattr(
        control,
        "_pause_and_drain_supervisor",
        lambda identity, _root: calls.append(("pause", identity["supervisor_pid"])),
    )
    monkeypatch.setattr(
        runtime,
        "capture_legacy_child_process_snapshot",
        lambda _state_dir: calls.append(("capture", 7119)) or [],
    )
    kickstarts = {"count": 0}

    def kickstart():
        kickstarts["count"] += 1
        calls.append(("kickstart", kickstarts["count"]))
        if kickstarts["count"] == 1:
            raise control.SupervisorControlBlocked("injected kickstart failure")

    monkeypatch.setattr(control, "_kickstart_supervisor_owner", kickstart)

    with pytest.raises(runtime.RuntimeBlocked, match="kickstart failure"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-bootstrap-retry",
        )
    result = runtime.reload_supervisor_for_migration(
        state_dir,
        cutover_generation=generation,
        request_id="task1-bootstrap-retry",
    )

    assert result["supervisor_pid"] == 7120
    assert calls == [
        ("pause", 7119),
        ("capture", 7119),
        ("kickstart", 1),
        ("pause", 7119),
        ("kickstart", 2),
    ]


def test_reload_bootstrap_retry_rejects_changed_capability_before_pause(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-bootstrap-release-change",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    previous = fake_supervisor_identity(control, repo_root, pid=7121)
    probes = iter((previous, previous, previous))
    calls = []
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(next(probes)),
    )
    monkeypatch.setattr(
        control,
        "_pause_and_drain_supervisor",
        lambda identity, _root: calls.append(
            ("pause", identity["supervisor_pid"])
        ),
    )
    monkeypatch.setattr(
        runtime,
        "capture_legacy_child_process_snapshot",
        lambda _state_dir: calls.append(("capture", 7121)) or [],
    )
    monkeypatch.setattr(
        control,
        "_kickstart_supervisor_owner",
        lambda: (_ for _ in ()).throw(
            control.SupervisorControlBlocked("injected kickstart failure")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="kickstart failure"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-bootstrap-release-change",
        )

    monkeypatch.setattr(control, "SUPERVISOR_RELEASE_SHA256", "f" * 64)
    with pytest.raises(runtime.RuntimeBlocked, match="capability changed"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-bootstrap-release-change",
        )

    assert calls == [("pause", 7121), ("capture", 7121)]


def test_reload_bootstrap_snapshot_blocks_changed_owner_before_pause(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-bootstrap-owner-change",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    previous = fake_supervisor_identity(control, repo_root, pid=7121)
    changed = {
        **fake_supervisor_identity(control, repo_root, pid=7122),
        "supervisor_start_token": "8" * 64,
    }
    probes = iter((previous, previous, changed))
    calls = []
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(next(probes)),
    )
    monkeypatch.setattr(
        control,
        "_pause_and_drain_supervisor",
        lambda identity, _root: calls.append(("pause", identity["supervisor_pid"])),
    )
    monkeypatch.setattr(
        runtime,
        "capture_legacy_child_process_snapshot",
        lambda _state_dir: calls.append("capture") or [],
    )
    monkeypatch.setattr(
        control,
        "_kickstart_supervisor_owner",
        lambda: (_ for _ in ()).throw(
            control.SupervisorControlBlocked("injected kickstart failure")
        ),
    )
    with pytest.raises(runtime.RuntimeBlocked, match="kickstart failure"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-bootstrap-owner-change",
        )

    with pytest.raises(runtime.RuntimeBlocked, match="different supervisor owner"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-bootstrap-owner-change",
        )

    assert calls == [("pause", 7121), "capture"]


def test_reload_reprobes_exact_owner_after_snapshot_before_kickstart(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-post-snapshot-owner-race",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    previous = fake_supervisor_identity(control, repo_root, pid=7126)
    changed = {**previous, "supervisor_start_token": "b" * 64}
    probes = iter((previous, changed))
    expected_pids = []
    calls = []

    def probe(_root, **kwargs):
        expected_pids.append(kwargs.get("expected_pid"))
        return dict(next(probes))

    monkeypatch.setattr(control, "_probe_canonical_supervisor", probe)
    monkeypatch.setattr(
        control,
        "_pause_and_drain_supervisor",
        lambda *_args: calls.append("pause"),
    )
    monkeypatch.setattr(
        runtime,
        "capture_legacy_child_process_snapshot",
        lambda _state_dir: calls.append("snapshot") or [],
    )
    monkeypatch.setattr(
        control,
        "_kickstart_supervisor_owner",
        lambda: calls.append("kickstart"),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="owner remains paused"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-post-snapshot-owner-race",
        )

    assert expected_pids == [None, 7126]
    assert calls == ["pause", "snapshot"]
    assert (state_dir / control.LEGACY_CHILD_SNAPSHOT_FILE).exists()


@pytest.mark.parametrize(
    "receipt_content,reason",
    (
        ("{malformed\n", "malformed"),
        ('{"schema_version":"supervisor-control-observation-v2"}\n', "fields"),
    ),
)
def test_reload_existing_invalid_receipt_blocks_before_owner_probe(
    monkeypatch,
    tmp_path,
    receipt_content,
    reason,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-invalid-receipt",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    receipt_path = state_dir / control.CONTROL_OBSERVATION_FILE
    receipt_path.write_text(receipt_content, encoding="utf-8")
    receipt_path.chmod(0o600)
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid receipt reached owner probe")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match=reason):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-invalid-receipt",
        )


def test_generation_two_missing_receipt_cannot_downgrade_to_bootstrap(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control = loaded_supervisor_control_module(runtime)
    enable_v47_supervisor_receipt(monkeypatch, control, repo_root)
    first = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-bootstrap-generation-one",
        operator_identity="task1",
        reason="strategy-platform-v13-cutover",
        target_schema_version="20260813_47",
    )
    control.observe_supervisor_control(state_dir, trusted_root=repo_root)
    runtime.resume_supervisor_after_migration(
        state_dir,
        cutover_generation=first["control"]["cutover_generation"],
        request_id="task1-bootstrap-generation-one",
    )
    second = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-bootstrap-generation-two",
        operator_identity="task1",
        reason="strategy-platform-v13-cutover",
        target_schema_version="20260813_47",
    )
    assert second["control"]["cutover_generation"] == (
        "00000000000000000002"
    )
    (state_dir / control.CONTROL_OBSERVATION_FILE).unlink()
    monkeypatch.setattr(
        control,
        "SUPERVISOR_RUNTIME_SCHEMA_VERSION",
        "20260813_46",
    )
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("generation two missing receipt reached owner probe")
        ),
    )
    monkeypatch.setattr(
        control,
        "_pause_and_drain_supervisor",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("generation two missing receipt reached pause")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="only for the first generation"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=second["control"]["cutover_generation"],
            request_id="task1-bootstrap-generation-two",
        )


def test_reload_existing_stale_receipt_blocks_before_pause(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-stale-receipt",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    identity = fake_supervisor_identity(control, repo_root, pid=7123)
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(identity),
    )
    control.observe_supervisor_control(state_dir, trusted_root=repo_root)
    receipt_path = state_dir / control.CONTROL_OBSERVATION_FILE
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["observed_at"] = "2000-01-01T00:00:00Z"
    receipt_path.write_text(
        json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)
    monkeypatch.setattr(
        control,
        "_pause_and_drain_supervisor",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("stale receipt reached pause")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="stale"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-stale-receipt",
        )


def test_bootstrap_requires_new_owner_observation_before_stop(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-bootstrap-stop-gate",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    control = loaded_supervisor_control_module(runtime)
    previous = fake_supervisor_identity(control, repo_root, pid=7124)
    replacement = {
        **fake_supervisor_identity(control, repo_root, pid=7125),
        "supervisor_start_token": "9" * 64,
    }
    probes = iter((previous, previous, replacement))
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(next(probes)),
    )
    monkeypatch.setattr(control, "_pause_and_drain_supervisor", lambda *_args: None)
    monkeypatch.setattr(control, "_kickstart_supervisor_owner", lambda: None)
    monkeypatch.setattr(
        runtime,
        "capture_legacy_child_process_snapshot",
        lambda _state_dir: [],
    )
    runtime.reload_supervisor_for_migration(
        state_dir,
        cutover_generation=generation,
        request_id="task1-bootstrap-stop-gate",
    )
    monkeypatch.setattr(
        runtime,
        "stop_legacy_snapshot_processes",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("missing new-owner receipt reached signal path")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="durable suspended receipt"):
        runtime.stop_legacy_runtime_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-bootstrap-stop-gate",
        )

    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: dict(replacement),
    )
    control.observe_supervisor_control(state_dir, trusted_root=repo_root)
    authorization = control.authorize_legacy_runtime_stop(
        state_dir,
        cutover_generation=generation,
        request_id="task1-bootstrap-stop-gate",
        trusted_root=repo_root,
    )
    assert authorization["status"] == "AUTHORIZED"


def test_target_schema_owner_cannot_use_missing_receipt_bootstrap(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control = loaded_supervisor_control_module(runtime)
    monkeypatch.setattr(
        control,
        "SUPERVISOR_RUNTIME_SCHEMA_VERSION",
        "20260813_47",
    )
    suspended = runtime.suspend_supervisor_for_migration(
        state_dir,
        request_id="task1-target-bootstrap",
        operator_identity="task1",
        reason="retire-legacy-runtime",
        target_schema_version="20260813_47",
    )
    generation = suspended["control"]["cutover_generation"]
    monkeypatch.setattr(
        control,
        "_probe_canonical_supervisor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("target-schema bootstrap reached owner probe")
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="pre-target"):
        runtime.reload_supervisor_for_migration(
            state_dir,
            cutover_generation=generation,
            request_id="task1-target-bootstrap",
        )


def test_supervisor_identity_accepts_canonical_space_path_and_rejects_interpreter(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    control = loaded_supervisor_control_module(runtime)
    repo_root = tmp_path / "Repo With Spaces"
    (repo_root / "backend" / ".venv" / "bin").mkdir(parents=True)
    (repo_root / "scripts").mkdir()
    (repo_root / "backend" / ".venv" / "bin" / "python").symlink_to(
        Path(sys.executable)
    )
    venv_python = repo_root / "backend" / ".venv" / "bin" / "python"
    pid = 7120
    command = {
        "value": "{} {}".format(
            venv_python,
            repo_root / "scripts" / "local_supervisor.py",
        )
    }
    monkeypatch.setattr(
        control,
        "_launchd_snapshot",
        lambda: (control.LAUNCHD_LABEL, pid),
    )
    monkeypatch.setattr(
        control,
        "_ps_value",
        lambda _pid, field: {
            "state": "S",
            "lstart": "Thu Aug 13 13:00:00 2026",
            "command": command["value"],
        }[field],
    )
    monkeypatch.setattr(control, "_process_cwd", lambda _pid: str(repo_root))

    identity = control._probe_canonical_supervisor(repo_root)
    assert identity["supervisor_pid"] == pid
    assert identity["supervisor_cwd"] == str(repo_root)

    command["value"] = "/tmp/python {}".format(
        repo_root / "scripts" / "local_supervisor.py"
    )
    with pytest.raises(
        control.SupervisorControlBlocked,
        match="command or working directory",
    ):
        control._probe_canonical_supervisor(repo_root)


def test_active_runtime_fence_serializes_suspend_against_inflight_mutation(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    repo_root, state_dir = configure_maintenance_repo(
        monkeypatch,
        runtime,
        tmp_path,
    )
    control = loaded_supervisor_control_module(runtime)
    active_entered = threading.Event()
    release_active = threading.Event()
    suspend_finished = threading.Event()
    failures = []

    def active_worker():
        try:
            with control.active_supervisor_automation_fence(
                state_dir,
                trusted_root=repo_root,
            ):
                active_entered.set()
                assert release_active.wait(2)
        except Exception as exc:
            failures.append(exc)

    def suspend_worker():
        try:
            runtime.suspend_supervisor_for_migration(
                state_dir,
                request_id="task1-concurrent-suspend",
                operator_identity="task1",
                reason="retire-legacy-runtime",
                target_schema_version="20260813_47",
            )
        except Exception as exc:
            failures.append(exc)
        finally:
            suspend_finished.set()

    active_thread = threading.Thread(target=active_worker)
    suspend_thread = threading.Thread(target=suspend_worker)
    active_thread.start()
    assert active_entered.wait(2)
    suspend_thread.start()
    assert not suspend_finished.wait(0.1)
    release_active.set()
    active_thread.join(2)
    suspend_thread.join(2)

    assert failures == []
    assert suspend_finished.is_set()
    assert runtime.read_supervisor_maintenance_status(state_dir)["mode"] == (
        "MIGRATION_SUSPENDED"
    )


def test_zombie_leader_with_live_process_group_member_blocks_terminal_proof(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    stopped = {
        "services": [
            {
                "service": "backend",
                "status": "stale-pid-removed",
                "pid": 7201,
            }
        ]
    }
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, service: {
            "service": service,
            "pid": None,
            "running": False,
            "process_state": runtime.PROCESS_STATE_EXITED,
        },
    )
    monkeypatch.setattr(
        runtime,
        "process_group_state",
        lambda pid: (
            runtime.PROCESS_STATE_RUNNING
            if pid == 7201
            else runtime.PROCESS_STATE_EXITED
        ),
    )
    monkeypatch.setattr(
        runtime,
        "orphaned_managed_process_map",
        lambda _state_dir, services: {service: [] for service in services},
    )
    monkeypatch.setattr(runtime, "_writer_lock_holder", lambda _path: None)

    with pytest.raises(runtime.RuntimeBlocked, match="nonterminal_groups"):
        runtime.require_complete_startup_cleanup(tmp_path, stopped)


def test_wait_for_url_uses_a_bounded_slow_probe_timeout(monkeypatch):
    runtime = load_runtime_module()
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(url, *, timeout):
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr(runtime, "urlopen", fake_urlopen)

    runtime.wait_for_url(
        "http://127.0.0.1:8000/readyz",
        "backend readiness",
        timeout_seconds=45,
    )

    assert calls == [
        (
            "http://127.0.0.1:8000/readyz",
            runtime.READINESS_PROBE_TIMEOUT_SECONDS,
        )
    ]


def test_wait_for_url_accepts_readiness_after_legacy_twenty_second_budget(
    monkeypatch,
):
    runtime = load_runtime_module()
    elapsed = [0.0]

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(runtime.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        runtime.time,
        "sleep",
        lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )

    def delayed_urlopen(_url, *, timeout):
        assert 0 < timeout <= runtime.READINESS_PROBE_TIMEOUT_SECONDS
        if elapsed[0] <= 20:
            raise runtime.URLError("backend still starting")
        return Response()

    monkeypatch.setattr(runtime, "urlopen", delayed_urlopen)

    runtime.wait_for_url(
        "http://127.0.0.1:8000/readyz",
        "backend readiness",
        timeout_seconds=runtime.BACKEND_STARTUP_TIMEOUT_SECONDS,
    )

    assert elapsed[0] > 20


def test_wait_for_url_still_fails_closed_at_explicit_budget(monkeypatch):
    runtime = load_runtime_module()
    elapsed = [0.0]

    monkeypatch.setattr(runtime.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        runtime.time,
        "sleep",
        lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )
    monkeypatch.setattr(
        runtime,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            runtime.URLError("backend unavailable")
        ),
    )

    with pytest.raises(
        runtime.RuntimeBlocked,
        match="backend readiness did not become reachable within 20 seconds",
    ):
        runtime.wait_for_url(
            "http://127.0.0.1:8000/readyz",
            "backend readiness",
            timeout_seconds=20,
        )

    assert elapsed[0] == 20


def test_supervisor_capability_short_process_avoids_heavy_app_imports_and_gc():
    harness = """
import importlib.util
import json
from pathlib import Path
import sys

script_path = Path({script_path!r})
spec = importlib.util.spec_from_file_location("isolated_local_runtime", script_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ClearingBundle(dict):
    cleared = False

    def clear(self):
        self.cleared = True
        super().clear()

bundle = ClearingBundle({{
    "OKX_DEMO_API_KEY": "must-not-be-printed",
    "OKX_DEMO_API_SECRET": "must-not-be-printed",
}})
module.DEFAULT_RUNTIME_ENV_FILE = script_path.parent / "missing-runtime.env"
module.validate_okx_demo_execution_target()
module.read_okx_runtime_capability = lambda: (
    bundle,
    {{
        "status": "READY",
        "configured": True,
        "source": "keychain",
        "_generation": "fixture-generation",
    }},
)
exit_code = module.main(["supervisor-capability", "--json"])
print("RESULT:" + json.dumps({{
    "exit_code": exit_code,
    "cleared": bundle.cleared,
    "app_imported": any(
        name == "app" or name.startswith("app.") for name in sys.modules
    ),
    "pydantic_imported": any(
        name == "pydantic" or name.startswith("pydantic.") for name in sys.modules
    ),
}}))
raise SystemExit(exit_code)
""".format(script_path=str(SCRIPT_PATH))

    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", harness],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 5
    assert "must-not-be-printed" not in completed.stdout
    result_line = next(
        line for line in completed.stdout.splitlines() if line.startswith("RESULT:")
    )
    result = json.loads(result_line.removeprefix("RESULT:"))
    assert result == {
        "exit_code": 0,
        "cleared": True,
        "app_imported": False,
        "pydantic_imported": False,
    }


def test_down_main_releases_control_lock_on_normal_return(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    runtime.REPO_ROOT = tmp_path
    runtime.DEFAULT_RUNTIME_ENV_FILE = tmp_path / "missing-runtime.env"
    state_dir = tmp_path / "runtime"
    monkeypatch.setattr(
        runtime,
        "stop_all",
        lambda _state_dir: {"status": "STOPPED", "services": []},
    )

    assert (
        runtime.main(
            ["down", "--runtime-dir", str(state_dir), "--json"]
        )
        == 0
    )

    lock_path = state_dir / runtime.CONTROL_LOCK_FILE
    with lock_path.open("r+") as handle:
        runtime.fcntl.flock(
            handle.fileno(),
            runtime.fcntl.LOCK_EX | runtime.fcntl.LOCK_NB,
        )
        runtime.fcntl.flock(handle.fileno(), runtime.fcntl.LOCK_UN)


def test_lightweight_constants_match_backend_okx_contract():
    runtime = load_runtime_module()
    from app.adapters.okx_demo import attestation_proof, credential_preflight
    from app.adapters.okx_demo import demo_canary

    assert runtime.EXECUTION_TARGET_ENV == credential_preflight.EXECUTION_TARGET_ENV
    assert runtime.ALLOW_REAL_FUNDS_ENV == credential_preflight.ALLOW_REAL_FUNDS_ENV
    assert runtime.REST_URL_ENV == credential_preflight.REST_URL_ENV
    assert runtime.OKX_DEMO_REST_URL == credential_preflight.OKX_DEMO_REST_URL
    assert (
        runtime.OKX_DEMO_REQUIRED_ENV_NAMES
        == credential_preflight.OKX_DEMO_REQUIRED_ENV_NAMES
    )
    assert (
        runtime.SAFE_OPERATOR_PREFLIGHT_REASONS
        == credential_preflight.SAFE_OPERATOR_PREFLIGHT_REASONS
    )
    assert runtime.ALLOW_DEMO_ORDER_ENV == demo_canary.ALLOW_DEMO_ORDER_ENV
    assert (
        runtime.OKX_DEMO_CANARY_ALLOWED_INSTRUMENTS
        == demo_canary.ALLOWED_INSTRUMENTS
    )
    assert (
        runtime.ATTESTATION_PROOF_KEY_ENV
        == attestation_proof.ATTESTATION_PROOF_KEY_ENV
    )


def install_ready_okx_runtime(monkeypatch, runtime):
    bundle = {
        "OKX_DEMO_API_KEY": "runtime-key",
        "OKX_DEMO_API_SECRET": "runtime-secret",
        "OKX_DEMO_API_PASSPHRASE": "runtime-passphrase",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "a" * 64,
        "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY": "74" * 32,
    }
    monkeypatch.setattr(
        runtime,
        "read_okx_runtime_capability",
        lambda: (
            dict(bundle),
            {
                "status": "READY",
                "configured": True,
                "source": "keychain",
                "_generation": "generation-test-1",
            },
        ),
    )
    monkeypatch.setattr(
        runtime,
        "validate_okx_demo_execution_target",
        lambda: None,
    )
    monkeypatch.setattr(
        runtime,
        "wait_for_okx_runtime",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime,
        "cleanup_orphaned_managed_processes",
        lambda _state_dir: None,
    )
    monkeypatch.setattr(
        runtime,
        "read_operator_token",
        lambda: (
            "test-operator-token-with-at-least-32-characters",
            {
                "status": "READY",
                "configured": True,
                "source": "keychain",
            },
        ),
    )
    return bundle


def test_runtime_database_defaults_to_one_canonical_postgres(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert runtime.runtime_database_url() == runtime.DEFAULT_DATABASE_URL


def test_runtime_environment_file_loads_only_non_secret_selectors(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    config = tmp_path / "runtime.env"
    config.write_text(
        "DATABASE_URL=postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai\n"
        "FREQTRADE_BINARY=/Users/local/freqtrade_venv/bin/freqtrade\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FREQTRADE_BINARY", raising=False)

    runtime.load_runtime_environment(config)

    assert runtime.os.environ["DATABASE_URL"].endswith("/freqtrade_ai")
    assert runtime.os.environ["FREQTRADE_BINARY"].endswith("/bin/freqtrade")


def test_runtime_environment_file_rejects_secret_or_unknown_keys(tmp_path):
    runtime = load_runtime_module()
    config = tmp_path / "runtime.env"
    config.write_text("DEEPSEEK_API_KEY=not-allowed\n", encoding="utf-8")

    with pytest.raises(runtime.RuntimeBlocked, match="not allowed"):
        runtime.load_runtime_environment(config)


def test_runtime_rejects_remote_or_noncanonical_database(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://freqtrade:change_me@example.com:5432/freqtrade_ai",
    )
    with pytest.raises(runtime.RuntimeBlocked, match="localhost PostgreSQL"):
        runtime.runtime_database_url()

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://freqtrade:change_me@localhost:5432/another_database",
    )
    with pytest.raises(runtime.RuntimeBlocked, match="canonical freqtrade_ai"):
        runtime.runtime_database_url()


def test_log_redaction_does_not_echo_secret_values():
    runtime = load_runtime_module()

    redacted = runtime.redact_line("DEEPSEEK_API_KEY=should-not-appear password: also-hidden")

    assert "should-not-appear" not in redacted
    assert "also-hidden" not in redacted
    assert redacted.count("***") == 2


@pytest.mark.parametrize(
    "line",
    [
        "postgresql://runtime:database-password@localhost/freqtrade_ai",
        (
            '{"database":"postgresql+psycopg://runtime:database-password'
            '@127.0.0.1/freqtrade_ai"}'
        ),
    ],
)
def test_log_redaction_hides_postgresql_passwords(line):
    runtime = load_runtime_module()

    redacted = runtime.redact_line(line)

    assert "database-password" not in redacted
    assert ":***@" in redacted


def test_read_deepseek_api_key_uses_fixed_macos_keychain_contract(monkeypatch):
    runtime = load_runtime_module()
    sentinel = "test-keychain-value-not-for-logs"
    observed = {}

    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(runtime.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        runtime.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="local-user") if uid == 501 else None,
    )

    def fake_run(command, **kwargs):
        observed["command"] = list(command)
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=sentinel + "\n", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    value, metadata = runtime.read_deepseek_api_key()

    assert value == sentinel
    assert metadata == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }
    assert sentinel not in str(metadata)
    assert observed["command"] == [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        "local-user",
        "-s",
        runtime.DEEPSEEK_KEYCHAIN_SERVICE,
        "-w",
    ]
    assert observed["kwargs"] == {
        "cwd": str(REPO_ROOT),
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": runtime.KEYCHAIN_TIMEOUT_SECONDS,
        "stdin": runtime.subprocess.DEVNULL,
    }


@pytest.mark.parametrize(
    ("result", "raised"),
    [
        (SimpleNamespace(returncode=44, stdout="", stderr="item not found"), None),
        (SimpleNamespace(returncode=0, stdout="\n", stderr=""), None),
        (None, TimeoutError),
    ],
)
def test_read_deepseek_api_key_fails_closed_without_exposing_keychain_errors(
    monkeypatch,
    result,
    raised,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(runtime.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        runtime.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_name="local-user"),
    )

    def fake_run(*_args, **_kwargs):
        if raised is TimeoutError:
            raise runtime.subprocess.TimeoutExpired(
                cmd="/usr/bin/security",
                timeout=runtime.KEYCHAIN_TIMEOUT_SECONDS,
                output="must-not-be-rendered",
                stderr="keychain-error-must-not-be-rendered",
            )
        return result

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    value, metadata = runtime.read_deepseek_api_key()

    assert value is None
    assert metadata == {
        "status": "UNAVAILABLE",
        "configured": False,
        "source": "keychain",
        "reason": "Keychain item is missing or inaccessible",
    }
    assert "must-not-be-rendered" not in str(metadata)
    assert "item not found" not in str(metadata)


def test_read_operator_token_uses_only_the_dedicated_keychain_item(
    monkeypatch,
):
    runtime = load_runtime_module()
    sentinel = "operator-token-with-at-least-32-characters"
    observed = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setenv(runtime.OPERATOR_TOKEN_ENV, "stale-shell-value")
    monkeypatch.setattr(
        runtime,
        "_read_macos_keychain_item",
        lambda service: observed.append(service) or sentinel,
    )

    value, metadata = runtime.read_operator_token()

    assert value == sentinel
    assert metadata == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }
    assert observed == [runtime.OPERATOR_TOKEN_KEYCHAIN_SERVICE]
    assert "stale-shell-value" not in str(metadata)
    assert sentinel not in str(metadata)


def test_operator_token_init_uses_interactive_keychain_prompt_without_argv_secret(
    monkeypatch,
):
    runtime = load_runtime_module()
    results = iter(
        (
            (None, {"status": "UNAVAILABLE"}),
            (
                "operator-token-with-at-least-32-characters",
                {"status": "READY"},
            ),
        )
    )
    observed = {}
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "read_operator_token", lambda: next(results))
    monkeypatch.setattr(runtime.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(runtime.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(runtime.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        runtime.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_name="local-user"),
    )

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    payload = runtime.configure_operator_token()

    assert payload["status"] == "READY"
    assert payload["changed"] is True
    assert observed["command"] == [
        "/usr/bin/security",
        "add-generic-password",
        "-a",
        "local-user",
        "-s",
        runtime.OPERATOR_TOKEN_KEYCHAIN_SERVICE,
        "-w",
    ]
    assert observed["kwargs"] == {
        "cwd": str(REPO_ROOT),
        "check": False,
    }


def test_service_environment_limits_credentials_to_required_services(monkeypatch):
    runtime = load_runtime_module()
    database_url = runtime.DEFAULT_DATABASE_URL
    sentinel = "test-deepseek-key"
    inherited_secrets = {
        "DEEPSEEK_API_KEY": "stale-shell-key",
        "FREQTRADE_AI_OPERATOR_TOKEN": "operator-token",
        "BINANCE_API_KEY": "binance-key",
        "BINANCE_API_SECRET": "binance-secret",
        "OKX_API_KEY": "okx-key",
        "OKX_API_SECRET": "okx-secret",
        "OKX_API_PASSPHRASE": "okx-passphrase",
        "OKX_DEMO_API_KEY": "okx-demo-key",
        "OKX_DEMO_API_SECRET": "okx-demo-secret",
        "OKX_DEMO_API_PASSPHRASE": "okx-demo-passphrase",
        "MIMO_API_KEY": "mimo-key",
        "OPENAI_API_KEY": "openai-key",
        "UNRELATED_SECRET_TOKEN": "unknown-secret-must-not-inherit",
        "STRATEGY_BLUEPRINT_PROVIDER": "fake",
        "STRATEGY_BLUEPRINT_MODEL": "shell-model",
        "STRATEGY_BLUEPRINT_BASE_URL": "https://attacker.invalid",
        "STRATEGY_BLUEPRINT_API_KEY_ENV": "FREQTRADE_AI_OPERATOR_TOKEN",
    }
    for key, value in inherited_secrets.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://stale.invalid/other")
    monkeypatch.setenv("PATH", "/safe/path")

    backend = runtime.service_environment(
        "backend",
        database_url,
        sentinel,
        operator_token="operator-token-from-keychain-123456",
    )
    worker = runtime.service_environment("worker", database_url, sentinel)
    frontend = runtime.service_environment("frontend", database_url, sentinel)

    assert backend["DATABASE_URL"] == database_url
    assert backend["DEEPSEEK_API_KEY"] == sentinel
    assert backend["STRATEGY_BLUEPRINT_PROVIDER"] == "deepseek"
    assert backend["STRATEGY_BLUEPRINT_MODEL"] == "deepseek-v4-pro"
    assert (
        backend["FREQTRADE_AI_OPERATOR_TOKEN"]
        == "operator-token-from-keychain-123456"
    )
    assert not (
        set(inherited_secrets)
        - {
            "DEEPSEEK_API_KEY",
            "FREQTRADE_AI_OPERATOR_TOKEN",
            "STRATEGY_BLUEPRINT_PROVIDER",
            "STRATEGY_BLUEPRINT_MODEL",
        }
    ) & set(backend)

    assert worker["DATABASE_URL"] == database_url
    assert worker["DEEPSEEK_API_KEY"] == sentinel
    assert worker["STRATEGY_BLUEPRINT_PROVIDER"] == "deepseek"
    assert worker["STRATEGY_BLUEPRINT_MODEL"] == "deepseek-v4-pro"
    assert not (
        set(inherited_secrets)
        - {
            "DEEPSEEK_API_KEY",
            "STRATEGY_BLUEPRINT_PROVIDER",
            "STRATEGY_BLUEPRINT_MODEL",
        }
    ) & set(worker)

    assert "DATABASE_URL" not in frontend
    assert not set(inherited_secrets) & set(frontend)
    assert frontend["APP_ENV"] == "local"
    assert frontend["VITE_ENABLE_DEV_FIXTURES"] == "false"
    assert frontend["VITE_FREQUI_URL"] == ""
    assert frontend["PATH"] == "/safe/path"
    assert all(
        environment["FREQTRADE_AI_DISABLE_ENV_FILE"] == "1"
        for environment in (backend, worker, frontend)
    )
    assert runtime.os.environ["DEEPSEEK_API_KEY"] == "stale-shell-key"


def test_clean_environment_remains_database_only(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setenv("STRATEGY_BLUEPRINT_PROVIDER", "deepseek")
    monkeypatch.setenv("STRATEGY_BLUEPRINT_MODEL", "shell-model")

    environment = runtime.clean_environment(runtime.DEFAULT_DATABASE_URL)

    assert environment["DATABASE_URL"] == runtime.DEFAULT_DATABASE_URL
    assert environment["APP_ENV"] == "local"
    assert "STRATEGY_BLUEPRINT_PROVIDER" not in environment
    assert "STRATEGY_BLUEPRINT_MODEL" not in environment


def test_managed_child_environment_never_reads_or_reconstructs_repo_dotenv(
    monkeypatch,
):
    runtime = load_runtime_module()
    forbidden = {
        "STRATEGY_BLUEPRINT_PROVIDER": "deepseek",
        "STRATEGY_BLUEPRINT_BASE_URL": "https://attacker.invalid",
        "STRATEGY_BLUEPRINT_API_KEY_ENV": "OKX_DEMO_API_SECRET",
        "OKX_DEMO_API_KEY": "must-not-load",
        "OKX_DEMO_API_SECRET": "must-not-load",
        "OKX_DEMO_API_PASSPHRASE": "must-not-load",
    }
    for name, value in forbidden.items():
        monkeypatch.setenv(name, value)

    environment = runtime.base_service_environment()

    assert not set(forbidden) & set(environment)
    assert "must-not-load" not in str(environment)


def test_okx_adapter_is_the_only_environment_receiving_complete_bundle(monkeypatch):
    runtime = load_runtime_module()
    bundle = {
        "OKX_DEMO_API_KEY": "adapter-key",
        "OKX_DEMO_API_SECRET": "adapter-secret",
        "OKX_DEMO_API_PASSPHRASE": "adapter-passphrase",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "a" * 64,
        "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY": "74" * 32,
    }
    for key, value in bundle.items():
        monkeypatch.setenv(key, "stale-" + value)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:1080")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/untrusted-ca.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/untrusted-requests-ca.pem")

    adapter = runtime.service_environment(
        "okx_adapter",
        runtime.DEFAULT_DATABASE_URL,
        None,
        bundle,
    )
    backend = runtime.service_environment(
        "backend",
        runtime.DEFAULT_DATABASE_URL,
        None,
        bundle,
    )

    assert {name: adapter[name] for name in bundle} == bundle
    assert adapter["FREQTRADE_AI_EXECUTION_TARGET"] == "OKX_DEMO"
    assert adapter["FREQTRADE_AI_ALLOW_REAL_FUNDS"] == "false"
    assert adapter["FREQTRADE_AI_OKX_DEMO_REST_URL"] == "https://openapi.okx.com"
    assert "DATABASE_URL" not in adapter
    assert not {
        "HTTPS_PROXY",
        "ALL_PROXY",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    } & set(adapter)
    assert not set(bundle) & set(backend)


@pytest.mark.parametrize(
    "bundle",
    [
        None,
        {"OKX_DEMO_API_KEY": "key"},
        {
            "OKX_DEMO_API_KEY": "key",
            "OKX_DEMO_API_SECRET": "secret",
            "OKX_DEMO_API_PASSPHRASE": "passphrase",
            "OKX_DEMO_ACCOUNT_FINGERPRINT": "a" * 64,
            "OKX_API_KEY": "unexpected",
        },
    ],
)
def test_okx_adapter_environment_rejects_non_exact_bundle(bundle):
    runtime = load_runtime_module()

    with pytest.raises(runtime.RuntimeBlocked, match="bundle is incomplete"):
        runtime.service_environment(
            "okx_adapter",
            runtime.DEFAULT_DATABASE_URL,
            None,
            bundle,
        )


def test_read_okx_demo_credentials_uses_four_fixed_keychain_items(monkeypatch):
    runtime = load_runtime_module()
    observed_services = []
    values = {
        service: "value-{}".format(index)
        for index, service in enumerate(
            runtime.OKX_DEMO_KEYCHAIN_SERVICES.values(),
            start=1,
        )
    }
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "validate_okx_demo_execution_target", lambda: None)

    def fake_read(service):
        observed_services.append(service)
        return values[service]

    monkeypatch.setattr(runtime, "_read_macos_keychain_item", fake_read)

    credentials, metadata = runtime.read_okx_demo_credentials()

    assert observed_services == list(runtime.OKX_DEMO_KEYCHAIN_SERVICES.values())
    assert credentials == {
        name: values[service]
        for name, service in runtime.OKX_DEMO_KEYCHAIN_SERVICES.items()
    }
    assert metadata == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }
    assert not any(value in str(metadata) for value in values.values())


def test_onboarding_reader_uses_only_three_signing_keychain_items(monkeypatch):
    runtime = load_runtime_module()
    observed_services = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "validate_okx_demo_execution_target", lambda: None)

    def fake_read(service):
        observed_services.append(service)
        return "value-{}".format(len(observed_services))

    monkeypatch.setattr(runtime, "_read_macos_keychain_item", fake_read)

    credentials, metadata = runtime.read_okx_demo_onboarding_credentials()

    assert observed_services == [
        runtime.OKX_DEMO_KEYCHAIN_SERVICES[name]
        for name in runtime.OKX_DEMO_CREDENTIAL_ENV_NAMES
    ]
    assert set(credentials or {}) == set(runtime.OKX_DEMO_CREDENTIAL_ENV_NAMES)
    assert (
        runtime.OKX_DEMO_KEYCHAIN_SERVICES["OKX_DEMO_ACCOUNT_FINGERPRINT"]
        not in observed_services
    )
    assert metadata == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }


def test_read_okx_demo_credentials_fails_atomically_without_env_fallback(monkeypatch):
    runtime = load_runtime_module()
    sentinels = {
        "OKX_DEMO_API_KEY": "shell-key-must-be-ignored",
        "OKX_DEMO_API_SECRET": "shell-secret-must-be-ignored",
        "OKX_DEMO_API_PASSPHRASE": "shell-passphrase-must-be-ignored",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "b" * 64,
    }
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "validate_okx_demo_execution_target", lambda: None)
    responses = iter(("keychain-key", None))
    monkeypatch.setattr(
        runtime,
        "_read_macos_keychain_item",
        lambda _service: next(responses),
    )

    credentials, metadata = runtime.read_okx_demo_credentials()

    assert credentials is None
    assert metadata["status"] == "BLOCKED"
    assert metadata["configured"] is False
    assert not any(value in str(metadata) for value in sentinels.values())


def test_runtime_capability_uses_non_secret_keychain_generation(monkeypatch):
    runtime = load_runtime_module()
    bundle = {
        name: "value-{}".format(index)
        for index, name in enumerate(
            runtime.OKX_DEMO_REQUIRED_ENV_NAMES,
            start=1,
        )
    }
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: (
            dict(bundle),
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_read_macos_keychain_item",
        lambda service: (
            "74" * 32
            if service == runtime.ATTESTATION_PROOF_KEYCHAIN_SERVICE
            else "generation-9"
        ),
    )

    credentials, metadata = runtime.read_okx_runtime_capability()

    assert credentials is not None
    assert metadata["_generation"] == "generation-9"
    assert "_revision" not in metadata
    assert not any(value in str(metadata) for value in bundle.values())
    credentials.clear()


def test_explicit_generation_rotation_writes_only_non_secret_metadata(
    monkeypatch,
):
    runtime = load_runtime_module()
    captured = {}
    secret_bundle = {
        name: "secret-{}".format(index)
        for index, name in enumerate(
            runtime.OKX_DEMO_REQUIRED_ENV_NAMES,
            start=1,
        )
    }
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: (
            secret_bundle,
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )
    monkeypatch.setattr(runtime.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        runtime.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_name="local-user"),
    )
    monkeypatch.setattr(
        runtime,
        "uuid4",
        lambda: SimpleNamespace(hex="generationvalue123"),
    )

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    payload = runtime.configure_okx_credential_generation()

    assert captured["command"][-2:] == ["-w", "generationvalue123"]
    assert runtime.OKX_DEMO_CREDENTIAL_GENERATION_SERVICE in captured["command"]
    assert payload["credential_generation"] == "UPDATED"
    assert "generationvalue123" not in str(payload)
    assert secret_bundle == {}


def test_macos_keychain_reader_uses_service_without_rendering_errors(monkeypatch):
    runtime = load_runtime_module()
    sentinel = "keychain-value-not-for-output"
    observed = {}
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(runtime.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        runtime.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="local-user") if uid == 501 else None,
    )

    def fake_run(command, **kwargs):
        observed["command"] = list(command)
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=sentinel + "\n", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    value = runtime._read_macos_keychain_item(
        runtime.OKX_DEMO_KEYCHAIN_SERVICES["OKX_DEMO_API_KEY"]
    )

    assert value == sentinel
    assert observed["command"] == [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        "local-user",
        "-s",
        "freqtrade-ai/okx-demo-api-key",
        "-w",
    ]
    assert observed["kwargs"]["timeout"] == runtime.KEYCHAIN_TIMEOUT_SECONDS
    assert observed["kwargs"]["stdin"] is runtime.subprocess.DEVNULL


def test_okx_preflight_child_receives_bundle_and_parent_returns_only_attestation(
    monkeypatch,
):
    runtime = load_runtime_module()
    credential_bundle = {
        "OKX_DEMO_API_KEY": "child-key",
        "OKX_DEMO_API_SECRET": "child-secret",
        "OKX_DEMO_API_PASSPHRASE": "child-passphrase",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "c" * 64,
    }
    captured = {}
    ready = {
        "status": "READY",
        "execution_target": "OKX_DEMO",
        "remote_account_evidence": {
            "authenticated_demo_response": True,
            "identity_present": True,
            "fingerprint_match": True,
            "permissions": {"read": True, "trade": True, "withdraw": False},
            "account_level": "2",
            "position_mode": "long_short_mode",
        },
        "local_target_contract": {
            "product_type": "SWAP",
            "margin_mode": "isolated",
            "allow_real_funds": False,
        },
        "request_contract": {
            "method": "GET",
            "path": "/api/v5/account/config",
            "simulated_trading_header": True,
        },
        "unexpected_remote_field": "child-output-must-not-be-forwarded",
    }
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: (
            credential_bundle,
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(
        runtime,
        "_read_macos_keychain_item",
        lambda service: "74" * 32,
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["environment"] = dict(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout=json.dumps(ready), stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    payload = runtime.run_okx_demo_preflight()

    assert captured["command"] == [
        "/venv/bin/python",
        "-m",
        "app.adapters.okx_demo.credential_preflight",
    ]
    assert {
        name: captured["environment"][name]
        for name in runtime.OKX_DEMO_REQUIRED_ENV_NAMES
    } == {
        "OKX_DEMO_API_KEY": "child-key",
        "OKX_DEMO_API_SECRET": "child-secret",
        "OKX_DEMO_API_PASSPHRASE": "child-passphrase",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "c" * 64,
    }
    assert (
        captured["environment"][
            "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY"
        ]
        == "74" * 32
    )
    assert payload["status"] == "READY"
    assert payload["credentials"]["source"] == "keychain"
    assert not any(
        value in str(payload)
        for value in (
            "child-key",
            "child-secret",
            "child-passphrase",
            "c" * 64,
        )
    )
    assert "child-output-must-not-be-forwarded" not in str(payload)
    assert credential_bundle == {}
    assert "okx_adapter" not in runtime.PID_FILES


def test_okx_preflight_does_not_spawn_when_keychain_bundle_is_missing(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: (
            None,
            {
                "status": "BLOCKED",
                "configured": False,
                "source": "keychain",
                "reason": "bundle unavailable",
            },
        ),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("preflight child must not start"),
    )

    payload = runtime.run_okx_demo_preflight()

    assert payload["status"] == "BLOCKED"
    assert payload["credentials"]["configured"] is False


def test_okx_preflight_surfaces_only_allowlisted_safe_child_reason(monkeypatch):
    runtime = load_runtime_module()
    credential_bundle = {
        "OKX_DEMO_API_KEY": "child-key",
        "OKX_DEMO_API_SECRET": "child-secret",
        "OKX_DEMO_API_PASSPHRASE": "child-passphrase",
        "OKX_DEMO_ACCOUNT_FINGERPRINT": "c" * 64,
    }
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: (
            credential_bundle,
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(
        runtime,
        "_read_macos_keychain_item",
        lambda _service: "74" * 32,
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2,
            stdout=json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": runtime.IP_WHITELIST_REJECTED_REASON,
                }
            ),
            stderr="untrusted-child-output",
        ),
    )

    payload = runtime.run_okx_demo_preflight()

    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == runtime.IP_WHITELIST_REJECTED_REASON
    assert "untrusted-child-output" not in str(payload)
    assert credential_bundle == {}


def test_okx_account_pin_child_receives_only_signing_bundle_and_is_redacted(
    monkeypatch,
):
    runtime = load_runtime_module()
    credential_bundle = {
        "OKX_DEMO_API_KEY": "onboarding-key",
        "OKX_DEMO_API_SECRET": "onboarding-secret",
        "OKX_DEMO_API_PASSPHRASE": "onboarding-passphrase",
    }
    captured = {}
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_onboarding_credentials",
        lambda: (
            credential_bundle,
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["environment"] = dict(kwargs["env"])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "READY",
                    "execution_target": "OKX_DEMO",
                    "account_fingerprint_pinned": True,
                }
            ),
            stderr="untrusted-child-output",
        )

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    payload = runtime.run_okx_demo_account_pin()

    assert captured["command"] == [
        "/venv/bin/python",
        "-m",
        "app.adapters.okx_demo.credential_preflight",
        "--pin-account",
    ]
    assert {
        name: captured["environment"][name]
        for name in runtime.OKX_DEMO_CREDENTIAL_ENV_NAMES
    } == {
        "OKX_DEMO_API_KEY": "onboarding-key",
        "OKX_DEMO_API_SECRET": "onboarding-secret",
        "OKX_DEMO_API_PASSPHRASE": "onboarding-passphrase",
    }
    assert "OKX_DEMO_ACCOUNT_FINGERPRINT" not in captured["environment"]
    assert payload["account_fingerprint_pinned"] is True
    assert not any(
        value in str(payload)
        for value in (
            "onboarding-key",
            "onboarding-secret",
            "onboarding-passphrase",
            "untrusted-child-output",
        )
    )
    assert credential_bundle == {}
    assert "okx_onboarding" not in runtime.PID_FILES


def test_okx_canary_without_explicit_flag_is_zero_keychain_and_zero_child(
    monkeypatch,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: pytest.fail("Keychain must not be read without explicit authorization"),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "child must not start without explicit authorization"
        ),
    )

    payload = runtime.run_okx_demo_canary(
        allow_demo_order=False,
        instrument="BTC-USDT-SWAP",
    )

    assert payload == {
        "status": "BLOCKED",
        "execution_target": "OKX_DEMO",
        "reason": "direct OKX Demo canary is permanently disabled; use canonical runtime one-shot grant",
    }


def test_okx_canary_cli_tombstone_is_exit_two_and_zero_capability(
    monkeypatch,
    capsys,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: pytest.fail("tombstone must not read Keychain"),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "tombstone must not start a child or network path"
        ),
    )

    exit_code = runtime.main(
        [
            "okx-demo-canary",
            "--allow-demo-order",
            "--instrument",
            "NOT-ALLOWLISTED",
            "--json",
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"


def test_okx_canary_with_missing_keychain_bundle_is_zero_child(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: (
            None,
            {
                "status": "BLOCKED",
                "configured": False,
                "source": "keychain",
                "reason": "bundle unavailable",
            },
        ),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "child must not start without complete Keychain bundle"
        ),
    )

    payload = runtime.run_okx_demo_canary(
        allow_demo_order=True,
        instrument="BTC-USDT-SWAP",
    )

    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == (
        "direct OKX Demo canary is permanently disabled; "
        "use canonical runtime one-shot grant"
    )


def test_okx_canary_child_receives_exact_bundle_and_returns_only_safe_evidence(
    monkeypatch,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "read_okx_demo_credentials",
        lambda: pytest.fail("retired canary must never read Keychain"),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "retired canary must never start a subprocess or network child"
        ),
    )
    assert runtime.run_okx_demo_canary(
        allow_demo_order=True,
        instrument="NOT-ALLOWLISTED",
    )["status"] == "BLOCKED"


def test_okx_canary_parent_rejects_extra_or_raw_child_evidence(monkeypatch):
    runtime = load_runtime_module()
    payload = runtime._validate_okx_demo_canary_payload(
        {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "artifact_id": "b" * 32,
            "instrument": "BTC-USDT-SWAP",
            "evidence": {
                "cl_ord_id_sha256": "c" * 64,
                "order_id_sha256": None,
                "cleanup_cl_ord_id_sha256": None,
                "simulated_trading_header": True,
                "sequence": [],
            },
            "reason_code": "HISTORICAL_VALIDATOR_ONLY",
        }
    )
    assert payload["status"] == "BLOCKED"


def test_doctor_uses_explicit_freqtrade_binary(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    binary = tmp_path / "freqtrade"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("FREQTRADE_BINARY", str(binary))

    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    payload = runtime.doctor(REPO_ROOT / ".freqtrade-ai" / "runtime-not-created")

    assert payload["checks"]["freqtrade_binary"] is True
    assert payload["database"]["kind"] == "postgresql"
    assert payload["schema"]["status"] == "READY"
    assert payload["freqtrade"]["status"] == "READY"
    assert payload["freqtrade"]["resolved_path"] == str(binary.resolve())


def test_worker_has_dedicated_pid_log_and_backend_working_directory():
    runtime = load_runtime_module()

    assert runtime.PID_FILES["worker"] == "worker.pid"
    assert runtime.LOG_FILES["worker"] == "worker.log"
    assert runtime.SERVICE_PROCESS_MARKERS["worker"] == "app.workers.deepseek_backtest_worker"
    assert runtime.SERVICE_WORKING_DIRECTORIES["worker"] == REPO_ROOT / "backend"


@pytest.mark.parametrize(
    ("ps_state", "expected"),
    [
        ("R", "RUNNING"),
        ("S+", "RUNNING"),
        ("Z", "ZOMBIE"),
        ("Z+", "ZOMBIE"),
        ("X", "EXITED"),
        ("?", "INACCESSIBLE"),
    ],
)
def test_process_state_distinguishes_live_zombie_and_dead_markers(
    monkeypatch,
    ps_state,
    expected,
):
    runtime = load_runtime_module()
    observed = {}
    monkeypatch.setattr(runtime.os, "kill", lambda _pid, _signal: None)

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=ps_state + "\n")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    assert runtime.process_state(4321) == expected
    assert observed["command"] == ["/bin/ps", "-p", "4321", "-o", "state="]
    assert observed["kwargs"]["timeout"] == 5
    assert observed["kwargs"]["env"] == runtime.SAFE_PROCESS_PROBE_ENV


def test_process_state_distinguishes_exited_and_inaccessible_pids(monkeypatch):
    runtime = load_runtime_module()

    monkeypatch.setattr(
        runtime.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("ps must not run for an exited PID"),
    )
    assert runtime.process_state(4321) == runtime.PROCESS_STATE_EXITED

    monkeypatch.setattr(
        runtime.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(PermissionError),
    )
    assert runtime.process_state(4321) == runtime.PROCESS_STATE_INACCESSIBLE


def test_process_state_fails_closed_when_ps_is_unavailable(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setattr(runtime.os, "kill", lambda *_args: None)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no ps")),
    )

    assert runtime.process_state(4321) == runtime.PROCESS_STATE_INACCESSIBLE


def test_process_state_rechecks_exit_race_after_empty_ps(monkeypatch):
    runtime = load_runtime_module()
    probes = []

    def fake_kill(*_args):
        probes.append(True)
        if len(probes) == 2:
            raise ProcessLookupError

    monkeypatch.setattr(runtime.os, "kill", fake_kill)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    assert runtime.process_state(4321) == runtime.PROCESS_STATE_EXITED
    assert len(probes) == 2


def test_process_status_does_not_report_zombie_as_running(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    pid_path = tmp_path / runtime.PID_FILES["backend"]
    pid_path.write_text("4321\n", encoding="utf-8")
    pid_path.chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_ZOMBIE,
    )

    assert runtime.process_status(tmp_path, "backend") == {
        "service": "backend",
        "pid": 4321,
        "running": False,
        "process_state": "ZOMBIE",
        "pid_file": str(pid_path),
    }


def test_worker_pid_validation_requires_command_marker_and_backend_cwd(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setattr(runtime.os, "getpgid", lambda pid: pid)
    responses = iter(
        (
            SimpleNamespace(
                returncode=0,
                stdout="/usr/bin/python3 -m app.workers.deepseek_backtest_worker\n",
            ),
            SimpleNamespace(
                returncode=0,
                stdout="n{}\n".format(REPO_ROOT / "backend"),
            ),
        )
    )
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: next(responses))

    assert runtime.is_managed_process(1234, "worker") is True


def test_worker_pid_validation_rejects_unrelated_process(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="python unrelated.py\n",
        ),
    )

    assert runtime.is_managed_process(1234, "worker") is False


def test_managed_process_snapshot_binds_start_command_cwd_and_detects_race(
    monkeypatch,
):
    runtime = load_runtime_module()
    pid = 1236
    started = "Thu Aug 13 13:00:00 2026"
    command = "/usr/bin/python3 " + " ".join(
        runtime.SERVICE_EXACT_ARGUMENTS["worker"]
    )
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_RUNNING,
    )
    monkeypatch.setattr(runtime.os, "getpgid", lambda candidate: candidate)
    ps_values = iter((started, command, started, command))
    monkeypatch.setattr(
        runtime,
        "_managed_process_ps_value",
        lambda *_args: (runtime.MANAGED_PROCESS_MATCH, next(ps_values)),
    )
    monkeypatch.setattr(
        runtime,
        "_managed_process_cwd",
        lambda _pid: (
            runtime.MANAGED_PROCESS_MATCH,
            str(runtime.SERVICE_WORKING_DIRECTORIES["worker"]),
        ),
    )

    status, snapshot = runtime.managed_process_snapshot(pid, "worker")

    assert status == runtime.MANAGED_PROCESS_MATCH
    assert snapshot is not None
    assert snapshot["pid"] == pid
    assert snapshot["pgid"] == pid
    assert snapshot["service"] == "worker"
    assert set(snapshot) == {
        "service",
        "pid",
        "pgid",
        "start_token",
        "command_sha256",
        "cwd",
    }
    assert command not in json.dumps(snapshot)

    raced_values = iter((started, command, started + " changed", command))
    monkeypatch.setattr(
        runtime,
        "_managed_process_ps_value",
        lambda *_args: (runtime.MANAGED_PROCESS_MATCH, next(raced_values)),
    )
    assert runtime.managed_process_snapshot(pid, "worker") == (
        runtime.MANAGED_PROCESS_INACCESSIBLE,
        None,
    )


def test_managed_process_identity_handles_space_path_and_requires_exact_cwd(
    monkeypatch,
):
    runtime = load_runtime_module()
    pid = 1235
    monkeypatch.setattr(runtime.os, "getpgid", lambda candidate: candidate)
    command = (
        "/Users/example/Repo With Spaces/backend/.venv/bin/python "
        + " ".join(runtime.SERVICE_EXACT_ARGUMENTS["backend"])
    )
    cwd = {"value": str(runtime.SERVICE_WORKING_DIRECTORIES["backend"])}
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs.get("env")))
        if arguments[:3] == ["/bin/ps", "-ww", "-p"]:
            return SimpleNamespace(returncode=0, stdout=command + "\n")
        return SimpleNamespace(returncode=0, stdout="n{}\n".format(cwd["value"]))

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    assert runtime.managed_process_identity(pid, "backend") == (
        runtime.MANAGED_PROCESS_MATCH
    )
    assert calls[0][0] == ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="]
    assert all(env == runtime.SAFE_PROCESS_PROBE_ENV for _command, env in calls)

    cwd["value"] = str(runtime.SERVICE_WORKING_DIRECTORIES["backend"]) + "-other"
    assert runtime.managed_process_identity(pid, "backend") == (
        runtime.MANAGED_PROCESS_NO_MATCH
    )


def test_managed_process_identity_fails_closed_when_cwd_is_unavailable(
    monkeypatch,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(runtime.os, "getpgid", lambda pid: pid)
    responses = iter(
        (
            SimpleNamespace(
                returncode=0,
                stdout="/usr/bin/python3 -m app.workers.deepseek_backtest_worker\n",
            ),
            SimpleNamespace(returncode=1, stdout=""),
        )
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_RUNNING,
    )

    assert (
        runtime.managed_process_identity(1234, "worker")
        == runtime.MANAGED_PROCESS_INACCESSIBLE
    )


def test_down_stops_worker_before_frontend_and_backend(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    observed = []

    def fake_stop_service(state_dir, service):
        observed.append((state_dir, service))
        return {"service": service, "status": "stopped"}

    monkeypatch.setattr(runtime, "stop_service", fake_stop_service)

    payload = runtime.stop_all(tmp_path)

    assert [service for _, service in observed] == [
        "okx_runtime",
        "frontend",
        "worker",
        "backend",
    ]
    assert [service["service"] for service in payload["services"]] == [
        "okx_runtime",
        "frontend",
        "worker",
        "backend",
    ]


def test_status_includes_backend_worker_and_frontend(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda state_dir, service: {"service": service, "running": True},
    )
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)

    payload = runtime.current_status(tmp_path)

    assert [service["service"] for service in payload["services"]] == [
        "backend",
        "worker",
        "frontend",
        "okx_runtime",
    ]


def test_start_launches_worker_with_expected_module(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    observed = []
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(runtime, "frontend_vite", lambda: Path("/frontend/vite"))
    monkeypatch.setattr(runtime, "port_available", lambda port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "wait_for_process", lambda *args, **kwargs: None)
    install_ready_okx_runtime(monkeypatch, runtime)

    def fake_start_service(service, command, **kwargs):
        observed.append((service, list(command), kwargs["cwd"]))

    monkeypatch.setattr(runtime, "start_service", fake_start_service)

    runtime.start(tmp_path)

    worker = next(item for item in observed if item[0] == "worker")
    assert worker[1] == [
        "/venv/bin/python",
        "-m",
        "app.workers.deepseek_backtest_worker",
    ]
    assert worker[2] == REPO_ROOT / "backend"


def test_start_uses_explicit_per_stage_readiness_budgets(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    url_waits = []
    process_waits = []
    monkeypatch.setattr(
        runtime,
        "backend_python",
        lambda: Path("/venv/bin/python"),
    )
    monkeypatch.setattr(
        runtime,
        "frontend_vite",
        lambda: Path("/frontend/vite"),
    )
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(
        runtime,
        "ensure_worker_queue_idle",
        lambda _url: None,
    )
    monkeypatch.setattr(
        runtime,
        "wait_for_url",
        lambda url, description, timeout_seconds=20: url_waits.append(
            (url, description, timeout_seconds)
        ),
    )
    monkeypatch.setattr(
        runtime,
        "wait_for_process",
        lambda state_dir, service, timeout_seconds=2.0: process_waits.append(
            (state_dir, service, timeout_seconds)
        ),
    )
    install_ready_okx_runtime(monkeypatch, runtime)
    monkeypatch.setattr(
        runtime,
        "start_service",
        lambda *_args, **_kwargs: None,
    )

    payload = runtime.start(tmp_path)

    assert [item[2] for item in url_waits] == [
        runtime.BACKEND_STARTUP_TIMEOUT_SECONDS,
        runtime.FRONTEND_STARTUP_TIMEOUT_SECONDS,
    ]
    assert process_waits == [
        (tmp_path, "worker", runtime.WORKER_STARTUP_TIMEOUT_SECONDS)
    ]
    assert runtime.BACKEND_STARTUP_TIMEOUT_SECONDS == 240
    assert runtime.STARTUP_COMMAND_BUDGET_SECONDS == 830
    assert payload["startup"]["status"] == "READY"
    assert set(payload["startup"]["stage_elapsed_ms"]) == (
        runtime.SAFE_STARTUP_STAGES
    )


def test_start_injects_keychain_key_only_into_backend_and_worker(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    sentinel = "test-keychain-runtime-value"
    observed = {}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-inherited-value")
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(runtime, "frontend_vite", lambda: Path("/frontend/vite"))
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "wait_for_process", lambda *args, **kwargs: None)
    install_ready_okx_runtime(monkeypatch, runtime)
    monkeypatch.setattr(
        runtime,
        "read_deepseek_api_key",
        lambda: (
            sentinel,
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )

    def fake_start_service(service, _command, **kwargs):
        observed[service] = kwargs["environment"]

    monkeypatch.setattr(runtime, "start_service", fake_start_service)

    payload = runtime.start(tmp_path)

    assert observed["backend"]["DEEPSEEK_API_KEY"] == sentinel
    assert observed["worker"]["DEEPSEEK_API_KEY"] == sentinel
    assert "DEEPSEEK_API_KEY" not in observed["frontend"]
    assert "FREQTRADE_AI_OPERATOR_TOKEN" in observed["backend"]
    assert "FREQTRADE_AI_OPERATOR_TOKEN" not in observed["worker"]
    assert "FREQTRADE_AI_OPERATOR_TOKEN" not in observed["frontend"]
    assert payload["credentials"]["deepseek_provider"] == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }
    assert payload["credentials"]["local_action"] == {
        "status": "READY",
        "configured": True,
        "source": "keychain",
    }
    assert sentinel not in str(payload)
    assert runtime.os.environ["DEEPSEEK_API_KEY"] == "stale-inherited-value"


def test_start_omits_stale_inherited_key_when_keychain_is_unavailable(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    observed = {}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-inherited-value")
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(runtime, "frontend_vite", lambda: Path("/frontend/vite"))
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "wait_for_process", lambda *args, **kwargs: None)
    install_ready_okx_runtime(monkeypatch, runtime)
    monkeypatch.setattr(
        runtime,
        "read_deepseek_api_key",
        lambda: (
            None,
            {
                "status": "UNAVAILABLE",
                "configured": False,
                "source": "keychain",
                "reason": "Keychain item is missing or inaccessible",
            },
        ),
    )

    def fake_start_service(service, _command, **kwargs):
        observed[service] = kwargs["environment"]

    monkeypatch.setattr(runtime, "start_service", fake_start_service)

    payload = runtime.start(tmp_path)

    assert all("DEEPSEEK_API_KEY" not in environment for environment in observed.values())
    assert payload["status"] == "RUNNING"
    assert payload["credentials"]["deepseek_provider"]["status"] == "UNAVAILABLE"
    assert "stale-inherited-value" not in str(payload)


def test_verify_fails_closed_when_worker_is_not_running(monkeypatch, capsys):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "current_status",
        lambda state_dir: {
            "environment": "local",
            "services": [
                {"service": "backend", "running": True},
                {"service": "worker", "running": False},
                {"service": "frontend", "running": True},
            ],
        },
    )
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)

    exit_code = runtime.main(["verify"])

    assert exit_code == 2
    assert (
        "backend, worker, frontend, and OKX runtime must all be running"
        in capsys.readouterr().out
    )


def test_verify_uses_canonical_readiness_budgets(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    runtime.REPO_ROOT = tmp_path.resolve()
    runtime.DEFAULT_RUNTIME_ENV_FILE = tmp_path / "missing-runtime.env"
    state_dir = runtime.REPO_ROOT / "runtime"
    waits = []
    services = [
        {"service": service, "running": True}
        for service in ("backend", "worker", "frontend", "okx_runtime")
    ]
    monkeypatch.setattr(
        runtime,
        "current_status",
        lambda _state_dir: {
            "environment": "local",
            "services": services,
            "execution_target": {"status": "READY", "active": "OKX_DEMO"},
            "credentials": {
                "okx_demo": {"status": "READY"},
                "local_action": {"status": "READY"},
            },
            "database": {"schema": "verified"},
            "okx_runtime": {"status": "READY"},
        },
    )
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(
        runtime,
        "wait_for_url",
        lambda url, description, timeout_seconds=20: waits.append(
            (url, description, timeout_seconds)
        ),
    )

    assert runtime.main(["verify", "--runtime-dir", str(state_dir)]) == 0

    assert waits == [
        (
            "http://127.0.0.1:{}/readyz".format(runtime.BACKEND_PORT),
            "backend readiness",
            runtime.BACKEND_STARTUP_TIMEOUT_SECONDS,
        ),
        (
            "http://127.0.0.1:{}/".format(runtime.FRONTEND_PORT),
            "frontend",
            runtime.FRONTEND_STARTUP_TIMEOUT_SECONDS,
        ),
    ]


def test_worker_queue_must_be_idle(monkeypatch):
    runtime = load_runtime_module()
    calls = []
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        fake_run,
    )

    with pytest.raises(runtime.RuntimeBlocked, match="worker queue is not idle"):
        runtime.ensure_worker_queue_idle(runtime.DEFAULT_DATABASE_URL)

    command = calls[0][0][0]
    assert "status IN ('PENDING','RUNNING')" in command[2]
    assert "status IN ('pending','running')" not in command[2]


def test_worker_queue_read_failure_reports_acl_or_schema_problem(monkeypatch):
    runtime = load_runtime_module()
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(
        runtime.RuntimeBlocked,
        match="worker queue read failed; verify runtime database ACL and schema",
    ):
        runtime.ensure_worker_queue_idle(runtime.DEFAULT_DATABASE_URL)


def test_start_missing_okx_keychain_is_zero_process(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    started = []
    monkeypatch.setattr(
        runtime,
        "read_operator_token",
        lambda: (
            "test-operator-token-with-at-least-32-characters",
            {"status": "READY", "configured": True, "source": "keychain"},
        ),
    )
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(runtime, "frontend_vite", lambda: Path("/frontend/vite"))
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(
        runtime,
        "validate_okx_demo_execution_target",
        lambda: None,
    )
    monkeypatch.setattr(
        runtime,
        "read_okx_runtime_capability",
        lambda: (
            None,
            {
                "status": "BLOCKED",
                "configured": False,
                "source": "keychain",
                "reason": "bundle unavailable",
            },
        ),
    )
    monkeypatch.setattr(
        runtime,
        "start_service",
        lambda service, *_args, **_kwargs: started.append(service),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="bundle unavailable"):
        runtime.start(tmp_path)

    assert started == []


def test_partial_start_failure_cleans_all_services(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    install_ready_okx_runtime(monkeypatch, runtime)
    started = []
    stopped = []
    monkeypatch.setattr(runtime, "backend_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(runtime, "frontend_vite", lambda: Path("/frontend/vite"))
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "wait_for_process", lambda *_args, **_kwargs: None)

    def fake_start(service, *_args, **_kwargs):
        started.append(service)
        if service == "frontend":
            raise RuntimeError("crash")

    monkeypatch.setattr(runtime, "start_service", fake_start)
    monkeypatch.setattr(
        runtime,
        "stop_service",
        lambda _state_dir, service: (
            stopped.append(service)
            or {"service": service, "status": "stopped"}
        ),
    )

    with pytest.raises(
        runtime.RuntimeBlocked,
        match="managed stage.*cleaned up",
    ) as raised:
        runtime.start(tmp_path)

    assert raised.value.safe_stage == "frontend-readiness"
    assert isinstance(raised.value.elapsed_ms, int)
    assert started == ["backend", "worker", "frontend"]
    assert stopped == list(runtime.SERVICE_STOP_ORDER)


def test_okx_startup_failure_diagnostic_survives_cleanup(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    install_ready_okx_runtime(monkeypatch, runtime)
    started = []
    stopped = []
    monkeypatch.setattr(
        runtime,
        "backend_python",
        lambda: Path("/venv/bin/python"),
    )
    monkeypatch.setattr(
        runtime,
        "frontend_vite",
        lambda: Path("/frontend/vite"),
    )
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "wait_for_process",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime,
        "start_service",
        lambda service, *_args, **_kwargs: started.append(service),
    )
    monkeypatch.setattr(
        runtime,
        "wait_for_okx_runtime",
        lambda _state_dir: (_ for _ in ()).throw(
            runtime.RuntimeBlocked(
                "safe generic failure",
                okx_runtime_failure_stage="writer-capability",
                okx_runtime_failure_category="WRITER",
                okx_runtime_failure_type="IntegrityError",
            )
        ),
    )
    monkeypatch.setattr(
        runtime,
        "stop_service",
        lambda _state_dir, service: (
            stopped.append(service)
            or {"service": service, "status": "stopped"}
        ),
    )

    with pytest.raises(runtime.RuntimeBlocked) as raised:
        runtime.start(tmp_path)

    assert raised.value.safe_stage == "okx-runtime-readiness"
    assert (
        raised.value.okx_runtime_failure_stage
        == "writer-capability"
    )
    assert raised.value.okx_runtime_failure_type == "IntegrityError"
    assert raised.value.okx_runtime_failure_category == "WRITER"
    assert started == list(runtime.SERVICE_START_ORDER)
    assert stopped == list(runtime.SERVICE_STOP_ORDER)


def test_zombie_cleanup_preserves_terminal_attestation_diagnostic(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    install_ready_okx_runtime(monkeypatch, runtime)
    pids = {
        service: 4100 + index
        for index, service in enumerate(runtime.SERVICE_START_ORDER)
    }
    signals = []
    monkeypatch.setattr(
        runtime,
        "backend_python",
        lambda: Path("/venv/bin/python"),
    )
    monkeypatch.setattr(
        runtime,
        "frontend_vite",
        lambda: Path("/frontend/vite"),
    )
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "wait_for_process",
        lambda *_args, **_kwargs: None,
    )

    def fake_start_service(service, *_args, **_kwargs):
        path = tmp_path / runtime.PID_FILES[service]
        path.write_text("{}\n".format(pids[service]), encoding="utf-8")
        path.chmod(0o600)

    monkeypatch.setattr(runtime, "start_service", fake_start_service)
    monkeypatch.setattr(
        runtime,
        "wait_for_okx_runtime",
        lambda _state_dir: (_ for _ in ()).throw(
            runtime.RuntimeBlocked(
                "safe generic failure",
                okx_runtime_failure_stage="read-attestation",
                okx_runtime_failure_category="ATTESTATION",
                okx_runtime_failure_type="OkxDemoCredentialsUnavailable",
            )
        ),
    )
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda pid: (
            runtime.PROCESS_STATE_ZOMBIE
            if pid in pids.values()
            else runtime.PROCESS_STATE_EXITED
        ),
    )
    monkeypatch.setattr(
        runtime,
        "orphaned_managed_process_map",
        lambda _state_dir, services: {service: [] for service in services},
    )
    monkeypatch.setattr(runtime, "_writer_lock_holder", lambda _path: None)
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )

    with pytest.raises(runtime.RuntimeBlocked) as raised:
        runtime.start(tmp_path)

    assert "cleaned up" in str(raised.value)
    assert "cleanup is incomplete" not in str(raised.value)
    assert raised.value.okx_runtime_failure_stage == "read-attestation"
    assert raised.value.okx_runtime_failure_category == "ATTESTATION"
    assert (
        raised.value.okx_runtime_failure_type
        == "OkxDemoCredentialsUnavailable"
    )
    assert signals == []
    assert all(
        not (tmp_path / runtime.PID_FILES[service]).exists()
        for service in runtime.SERVICE_START_ORDER
    )


def test_parent_clears_stale_failure_before_child_can_exit_without_main(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    install_ready_okx_runtime(monkeypatch, runtime)
    stale_path = tmp_path / runtime.OKX_RUNTIME_FAILURE_FILE
    stale_path.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "stage": "read-attestation",
                "category": "ATTESTATION",
                "cause_type": "OkxDemoCredentialsUnavailable",
            }
        ),
        encoding="utf-8",
    )
    stale_path.chmod(0o600)
    started = []
    monkeypatch.setattr(
        runtime,
        "backend_python",
        lambda: Path("/venv/bin/python"),
    )
    monkeypatch.setattr(
        runtime,
        "frontend_vite",
        lambda: Path("/frontend/vite"),
    )
    monkeypatch.setattr(runtime, "port_available", lambda _port: True)
    monkeypatch.setattr(runtime, "ensure_schema", lambda _url: None)
    monkeypatch.setattr(runtime, "ensure_worker_queue_idle", lambda _url: None)
    monkeypatch.setattr(runtime, "wait_for_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "wait_for_process",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime,
        "start_service",
        lambda service, *_args, **_kwargs: started.append(service),
    )

    def child_exited_before_main(_state_dir):
        assert not stale_path.exists()
        assert runtime.okx_runtime_failure(tmp_path) == {}
        raise runtime.RuntimeBlocked("child exited before main")

    monkeypatch.setattr(
        runtime,
        "wait_for_okx_runtime",
        child_exited_before_main,
    )
    monkeypatch.setattr(
        runtime,
        "stop_service",
        lambda _state_dir, service: {
            "service": service,
            "status": "stopped",
        },
    )

    with pytest.raises(runtime.RuntimeBlocked) as captured:
        runtime.start(tmp_path)

    assert started == list(runtime.SERVICE_START_ORDER)
    assert captured.value.okx_runtime_failure_stage is None
    assert captured.value.okx_runtime_failure_category is None
    assert captured.value.okx_runtime_failure_type is None


@pytest.mark.parametrize("terminal_state", ["EXITED", "ZOMBIE"])
def test_cleanup_stale_runtime_removes_terminal_pid_and_readiness(
    monkeypatch,
    tmp_path,
    terminal_state,
):
    runtime = load_runtime_module()
    pid_path = tmp_path / runtime.PID_FILES["okx_runtime"]
    pid_path.write_text(
        "12345\n",
        encoding="utf-8",
    )
    pid_path.chmod(0o600)
    readiness_path = tmp_path / runtime.OKX_RUNTIME_READY_FILE
    readiness_path.write_text(
        "{}\n",
        encoding="utf-8",
    )
    readiness_path.chmod(0o600)
    monkeypatch.setattr(runtime, "process_state", lambda _pid: terminal_state)
    monkeypatch.setattr(
        runtime,
        "process_group_state",
        lambda _pid: runtime.PROCESS_STATE_EXITED,
    )

    runtime.cleanup_stale_runtime_state(tmp_path)

    assert not pid_path.exists()
    assert not readiness_path.exists()


def test_cleanup_stale_runtime_blocks_terminal_leader_with_live_group(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid_path = tmp_path / runtime.PID_FILES["backend"]
    pid_path.write_text("12346\n", encoding="utf-8")
    pid_path.chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_ZOMBIE,
    )
    monkeypatch.setattr(
        runtime,
        "process_group_state",
        lambda _pid: runtime.PROCESS_STATE_RUNNING,
    )

    with pytest.raises(runtime.RuntimeBlocked, match="refusing a competing start"):
        runtime.cleanup_stale_runtime_state(tmp_path)

    assert pid_path.exists()


@pytest.mark.parametrize("contents", ("", "not-a-pid\n", "-1\n"))
def test_invalid_pid_evidence_fails_closed(monkeypatch, tmp_path, contents):
    runtime = load_runtime_module()
    pid_path = tmp_path / runtime.PID_FILES["backend"]
    pid_path.write_text(contents, encoding="utf-8")
    pid_path.chmod(0o600)

    with pytest.raises(runtime.RuntimeBlocked, match="PID evidence is malformed"):
        runtime.cleanup_stale_runtime_state(tmp_path)

    assert pid_path.exists()


def test_cleanup_stale_runtime_preserves_inaccessible_pid_evidence(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid_path = tmp_path / runtime.PID_FILES["okx_runtime"]
    pid_path.write_text("12345\n", encoding="utf-8")
    pid_path.chmod(0o600)
    readiness_path = tmp_path / runtime.OKX_RUNTIME_READY_FILE
    readiness_path.write_text("{}\n", encoding="utf-8")
    readiness_path.chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_INACCESSIBLE,
    )

    runtime.cleanup_stale_runtime_state(tmp_path)

    assert pid_path.exists()
    assert readiness_path.exists()


def test_okx_runtime_readiness_reports_blocked_openings_without_secrets(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid = 4321
    (tmp_path / runtime.OKX_RUNTIME_READY_FILE).write_text(
        json.dumps(
            {
                "status": "BLOCKED_OPENINGS",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": "DRIFTED",
                "writer": "UNIQUE",
                "automation_guard": "MANUAL_RESET_REQUIRED",
                "pid": pid,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / runtime.OKX_RUNTIME_READY_FILE).chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, _service: {
            "service": "okx_runtime",
            "pid": pid,
            "running": True,
        },
    )
    monkeypatch.setattr(
        runtime,
        "is_managed_process",
        lambda candidate, service: (
            candidate == pid and service == "okx_runtime"
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_writer_lock_holder",
        lambda _state_dir: pid,
    )

    assert runtime.okx_runtime_readiness(tmp_path) == {
        "status": "BLOCKED_OPENINGS",
        "execution_target": "OKX_DEMO",
        "adapter": "ATTESTED",
        "reconciliation": "DRIFTED",
        "writer": "UNIQUE",
        "automation_guard": "MANUAL_RESET_REQUIRED",
    }


def test_okx_runtime_readiness_accepts_recovery_only_without_opening_ready(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid = 4322
    (tmp_path / runtime.OKX_RUNTIME_READY_FILE).write_text(
        json.dumps(
            {
                "status": "RECOVERY_ONLY",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": "DRIFTED",
                "writer": "UNIQUE",
                "automation_guard": "BLOCKED",
                "pid": pid,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / runtime.OKX_RUNTIME_READY_FILE).chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda *_args: {"pid": pid, "running": True},
    )
    monkeypatch.setattr(runtime, "is_managed_process", lambda *_args: True)
    monkeypatch.setattr(runtime, "_writer_lock_holder", lambda *_args: pid)

    readiness = runtime.okx_runtime_readiness(tmp_path)
    assert readiness["status"] == "RECOVERY_ONLY"
    assert readiness["reconciliation"] == "DRIFTED"


def test_okx_runtime_startup_returns_for_guard_blocked_openings(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    moments = iter((0.0, 1.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        runtime,
        "okx_runtime_readiness",
        lambda _state_dir: {
            "status": "BLOCKED_OPENINGS",
            "reconciliation": "RECOVERED",
            "automation_guard": "BLOCKED",
        },
    )
    runtime.wait_for_okx_runtime(tmp_path)


def test_okx_runtime_startup_returns_for_recovery_only(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    moments = iter((0.0, 1.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        runtime,
        "okx_runtime_readiness",
        lambda _state_dir: {"status": "RECOVERY_ONLY"},
    )
    runtime.wait_for_okx_runtime(tmp_path)


def test_okx_runtime_startup_allows_authenticated_recovery_after_twenty_seconds(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    moments = iter((0.0, 21.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        runtime,
        "okx_runtime_readiness",
        lambda _state_dir: {"status": "READY"},
    )

    runtime.wait_for_okx_runtime(tmp_path)

    assert runtime.OKX_RUNTIME_STARTUP_TIMEOUT_SECONDS == 300


def test_okx_runtime_startup_fails_closed_when_child_exits(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    moments = iter((0.0, 1.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        runtime,
        "okx_runtime_readiness",
        lambda _state_dir: {"status": "BLOCKED"},
    )
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, _service: {"running": False},
    )

    with pytest.raises(runtime.RuntimeBlocked, match="did not establish"):
        runtime.wait_for_okx_runtime(tmp_path)


def test_okx_runtime_startup_propagates_only_safe_failure_stage_and_type(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    moments = iter((0.0, 1.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        runtime,
        "okx_runtime_readiness",
        lambda _state_dir: {"status": "BLOCKED"},
    )
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, _service: {"running": False},
    )
    failure_path = tmp_path / runtime.OKX_RUNTIME_FAILURE_FILE
    failure_path.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "stage": "read-attestation",
                "category": "ATTESTATION",
                "cause_type": "OkxDemoCredentialsUnavailable",
            }
        ),
        encoding="utf-8",
    )
    failure_path.chmod(0o600)

    with pytest.raises(runtime.RuntimeBlocked) as captured:
        runtime.wait_for_okx_runtime(tmp_path)

    assert captured.value.okx_runtime_failure_stage == "read-attestation"
    assert captured.value.okx_runtime_failure_category == "ATTESTATION"
    assert (
        captured.value.okx_runtime_failure_type
        == "OkxDemoCredentialsUnavailable"
    )


def test_okx_runtime_failure_rejects_unsafe_or_unexpected_evidence(tmp_path):
    runtime = load_runtime_module()
    failure_path = tmp_path / runtime.OKX_RUNTIME_FAILURE_FILE
    failure_path.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "stage": "writer-capability",
                "category": "WRITER",
                "cause_type": "IntegrityError",
                "secret": "must-not-be-read",
            }
        ),
        encoding="utf-8",
    )
    failure_path.chmod(0o600)
    assert runtime.okx_runtime_failure(tmp_path) == {}

    failure_path.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "stage": "writer-capability",
                "category": "WRITER",
                "cause_type": "IntegrityError",
            }
        ),
        encoding="utf-8",
    )
    failure_path.chmod(0o644)
    assert runtime.okx_runtime_failure(tmp_path) == {}

    failure_path.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "stage": "read-attestation",
                "category": "ATTESTATION",
                "cause_type": "SensitiveButValidIdentifier",
            }
        ),
        encoding="utf-8",
    )
    failure_path.chmod(0o600)
    assert runtime.okx_runtime_failure(tmp_path) == {}


def test_okx_runtime_startup_fails_closed_after_bounded_timeout(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    moments = iter((0.0, 300.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))

    with pytest.raises(runtime.RuntimeBlocked, match="did not establish"):
        runtime.wait_for_okx_runtime(tmp_path)


def test_repeated_start_refuses_without_stopping_healthy_runtime(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    stopped = []
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, service: {
            "service": service,
            "pid": 9001 if service == "backend" else None,
            "running": service == "backend",
        },
    )
    monkeypatch.setattr(
        runtime,
        "stop_all",
        lambda _state_dir: stopped.append(True),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="repeated up was refused"):
        runtime.start(tmp_path)

    assert stopped == []


def test_start_refuses_inaccessible_tracked_process(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    discovered = []
    monkeypatch.setattr(runtime, "cleanup_stale_runtime_state", lambda _path: None)
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, service: {
            "service": service,
            "pid": 9002 if service == "backend" else None,
            "running": False,
            "process_state": (
                runtime.PROCESS_STATE_INACCESSIBLE
                if service == "backend"
                else runtime.PROCESS_STATE_EXITED
            ),
        },
    )
    monkeypatch.setattr(
        runtime,
        "cleanup_orphaned_managed_processes",
        lambda _path: discovered.append(True),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="repeated up was refused"):
        runtime.start(tmp_path)

    assert discovered == []


def test_orphan_cleanup_signals_only_marker_and_cwd_verified_process(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    signals = []
    process_snapshots = []
    discoveries = iter(
        (
            SimpleNamespace(returncode=0, stdout="321\n"),
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=1, stdout=""),
        )
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: (
            process_snapshots.append(True)
            or next(discoveries)
        ),
    )
    monkeypatch.setattr(
        runtime,
        "is_managed_process",
        lambda pid, service: pid == 321 and service == "okx_runtime",
    )
    monkeypatch.setattr(
        runtime,
        "managed_process_identity",
        lambda pid, service: (
            runtime.MANAGED_PROCESS_MATCH
            if pid == 321 and service == "okx_runtime"
            else runtime.MANAGED_PROCESS_NO_MATCH
        ),
    )
    states = iter(
        (runtime.PROCESS_STATE_RUNNING, runtime.PROCESS_STATE_EXITED)
    )
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: next(states),
    )
    monkeypatch.setattr(
        runtime,
        "process_group_state",
        lambda _pid: runtime.PROCESS_STATE_EXITED,
    )
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    runtime.cleanup_orphaned_managed_processes(tmp_path)

    assert signals == [(321, runtime.signal.SIGTERM)]
    assert process_snapshots == [True] * len(runtime.SERVICE_STOP_ORDER)


def test_orphan_cleanup_rejects_terminal_leader_with_live_group_member(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "orphaned_managed_process_map",
        lambda _state_dir, services: {
            service: ([322] if service == "okx_runtime" else [])
            for service in services
        },
    )
    monkeypatch.setattr(
        runtime,
        "managed_process_identity",
        lambda _pid, _service: runtime.MANAGED_PROCESS_MATCH,
    )
    states = iter(
        (runtime.PROCESS_STATE_RUNNING, runtime.PROCESS_STATE_ZOMBIE)
    )
    monkeypatch.setattr(runtime, "process_state", lambda _pid: next(states))
    monkeypatch.setattr(
        runtime,
        "process_group_state",
        lambda _pid: runtime.PROCESS_STATE_RUNNING,
    )
    monkeypatch.setattr(runtime.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    with pytest.raises(runtime.RuntimeBlocked, match="still has live members"):
        runtime.cleanup_orphaned_managed_processes(tmp_path)


def test_orphan_discovery_fails_closed_on_process_snapshot_error(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=2, stdout=""),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="discovery is unavailable"):
        runtime.orphaned_managed_process_map(tmp_path, ("backend",))


def test_orphan_discovery_fails_closed_on_inaccessible_ownership(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="321\n",
        ),
    )
    monkeypatch.setattr(
        runtime,
        "managed_process_identity",
        lambda *_args: runtime.MANAGED_PROCESS_INACCESSIBLE,
    )

    with pytest.raises(
        runtime.RuntimeBlocked,
        match="ownership could not be established",
    ):
        runtime.orphaned_managed_process_map(tmp_path, ("backend",))


def test_orphan_cleanup_revalidates_identity_before_sigterm(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "orphaned_managed_process_map",
        lambda _state_dir, services: {
            service: ([321] if service == "backend" else [])
            for service in services
        },
    )
    monkeypatch.setattr(
        runtime,
        "managed_process_identity",
        lambda *_args: runtime.MANAGED_PROCESS_NO_MATCH,
    )
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_RUNNING,
    )
    signals = []
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="changed before signaling"):
        runtime.cleanup_orphaned_managed_processes(tmp_path)

    assert signals == []


def test_orphan_cleanup_blocks_when_sigkill_does_not_terminate_process(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "orphaned_managed_process_map",
        lambda _state_dir, services: {
            service: ([321] if service == "backend" else [])
            for service in services
        },
    )
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_RUNNING,
    )
    monkeypatch.setattr(
        runtime,
        "managed_process_identity",
        lambda *_args: runtime.MANAGED_PROCESS_MATCH,
    )
    monkeypatch.setattr(runtime, "is_managed_process", lambda *_args: True)
    moments = iter((0.0, 11.0, 20.0, 26.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    signals = []
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )

    with pytest.raises(runtime.RuntimeBlocked, match="terminal state"):
        runtime.cleanup_orphaned_managed_processes(tmp_path)

    assert signals == [
        (321, runtime.signal.SIGTERM),
        (321, runtime.signal.SIGKILL),
    ]


def test_stop_service_preserves_pid_when_process_group_cannot_be_signaled(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid = 321
    pid_path = tmp_path / runtime.PID_FILES["backend"]
    pid_path.write_text("321\n", encoding="utf-8")
    pid_path.chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_RUNNING,
    )
    monkeypatch.setattr(
        runtime,
        "is_managed_process",
        lambda candidate, service: candidate == pid and service == "backend",
    )
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError),
    )

    result = runtime.stop_service(tmp_path, "backend")

    assert result == {
        "service": "backend",
        "status": "BLOCKED",
        "pid": pid,
        "reason": "managed process group could not be signaled safely",
    }
    assert pid_path.exists()


def test_stop_service_removes_zombie_pid_without_signaling(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    pid = 322
    pid_path = tmp_path / runtime.PID_FILES["backend"]
    pid_path.write_text("{}\n".format(pid), encoding="utf-8")
    pid_path.chmod(0o600)
    signals = []
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_ZOMBIE,
    )
    monkeypatch.setattr(
        runtime,
        "is_managed_process",
        lambda *_args: pytest.fail("zombies are terminal before ownership checks"),
    )
    monkeypatch.setattr(
        runtime,
        "process_group_state",
        lambda _pid: runtime.PROCESS_STATE_EXITED,
    )
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda *args: signals.append(args),
    )

    result = runtime.stop_service(tmp_path, "backend")

    assert result == {
        "service": "backend",
        "status": "stale-pid-removed",
        "pid": pid,
    }
    assert signals == []
    assert not pid_path.exists()


def test_stop_service_keeps_zombie_pid_when_group_has_live_member(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid = 322
    pid_path = tmp_path / runtime.PID_FILES["backend"]
    pid_path.write_text("{}\n".format(pid), encoding="utf-8")
    pid_path.chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_ZOMBIE,
    )
    monkeypatch.setattr(
        runtime,
        "process_group_state",
        lambda _pid: runtime.PROCESS_STATE_RUNNING,
    )
    signals = []
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda *args: signals.append(args),
    )

    result = runtime.stop_service(tmp_path, "backend")

    assert result["status"] == "BLOCKED"
    assert "still has live members" in result["reason"]
    assert signals == []
    assert pid_path.exists()


def test_stop_service_treats_post_term_zombie_as_stopped(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    pid = 323
    pid_path = tmp_path / runtime.PID_FILES["backend"]
    pid_path.write_text("{}\n".format(pid), encoding="utf-8")
    pid_path.chmod(0o600)
    states = iter(
        (runtime.PROCESS_STATE_RUNNING, runtime.PROCESS_STATE_ZOMBIE)
    )
    signals = []
    monkeypatch.setattr(runtime, "process_state", lambda _pid: next(states))
    monkeypatch.setattr(runtime, "is_managed_process", lambda *_args: True)
    monkeypatch.setattr(
        runtime,
        "process_group_state",
        lambda _pid: runtime.PROCESS_STATE_EXITED,
    )
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda signaled_pid, signum: signals.append((signaled_pid, signum)),
    )

    result = runtime.stop_service(tmp_path, "backend")

    assert result == {"service": "backend", "status": "stopped", "pid": pid}
    assert signals == [(pid, runtime.signal.SIGTERM)]
    assert not pid_path.exists()


def test_stop_service_rechecks_pid_after_sigkill_group_lookup_failure(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid = 325
    pid_path = tmp_path / runtime.PID_FILES["backend"]
    pid_path.write_text("{}\n".format(pid), encoding="utf-8")
    pid_path.chmod(0o600)
    moments = iter((0.0, 11.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_RUNNING,
    )
    monkeypatch.setattr(runtime, "is_managed_process", lambda *_args: True)
    signals = []

    def fake_killpg(signaled_pid, signum):
        signals.append((signaled_pid, signum))
        if signum == runtime.signal.SIGKILL:
            raise ProcessLookupError

    monkeypatch.setattr(runtime.os, "killpg", fake_killpg)

    result = runtime.stop_service(tmp_path, "backend")

    assert result["status"] == "BLOCKED"
    assert "did not reach a terminal state" in result["reason"]
    assert signals == [
        (pid, runtime.signal.SIGTERM),
        (pid, runtime.signal.SIGKILL),
    ]
    assert pid_path.exists()


def test_stop_service_accepts_exit_race_before_sigkill(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    pid = 326
    pid_path = tmp_path / runtime.PID_FILES["backend"]
    pid_path.write_text("{}\n".format(pid), encoding="utf-8")
    pid_path.chmod(0o600)
    moments = iter((0.0, 11.0))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(moments))
    states = iter(
        (
            runtime.PROCESS_STATE_RUNNING,
            runtime.PROCESS_STATE_RUNNING,
            runtime.PROCESS_STATE_ZOMBIE,
        )
    )
    monkeypatch.setattr(runtime, "process_state", lambda _pid: next(states))
    ownership = iter((True, False))
    monkeypatch.setattr(
        runtime,
        "is_managed_process",
        lambda *_args: next(ownership),
    )
    signals = []
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda signaled_pid, signum: signals.append((signaled_pid, signum)),
    )
    monkeypatch.setattr(
        runtime,
        "process_group_state",
        lambda _pid: runtime.PROCESS_STATE_EXITED,
    )

    result = runtime.stop_service(tmp_path, "backend")

    assert result == {"service": "backend", "status": "stopped", "pid": pid}
    assert signals == [(pid, runtime.signal.SIGTERM)]
    assert not pid_path.exists()


def test_stop_service_preserves_inaccessible_pid_without_signaling(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid = 324
    pid_path = tmp_path / runtime.PID_FILES["backend"]
    pid_path.write_text("{}\n".format(pid), encoding="utf-8")
    pid_path.chmod(0o600)
    signals = []
    monkeypatch.setattr(
        runtime,
        "process_state",
        lambda _pid: runtime.PROCESS_STATE_INACCESSIBLE,
    )
    monkeypatch.setattr(
        runtime.os,
        "killpg",
        lambda *args: signals.append(args),
    )

    result = runtime.stop_service(tmp_path, "backend")

    assert result["status"] == "BLOCKED"
    assert "could not be established" in result["reason"]
    assert signals == []
    assert pid_path.exists()


def test_credential_loss_freezes_openings_without_stopping_writer(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    stopped = []
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, _service: {
            "service": "okx_runtime",
            "pid": 123,
            "running": True,
        },
    )
    monkeypatch.setattr(
        runtime,
        "okx_runtime_readiness",
        lambda _state_dir: {"status": "BLOCKED_OPENINGS"},
    )
    monkeypatch.setattr(
        runtime,
        "stop_all",
        lambda _state_dir: stopped.append(True),
    )

    result = runtime.freeze_okx_openings(tmp_path)

    assert result["status"] == "BLOCKED_OPENINGS"
    assert (
        tmp_path / runtime.OPENINGS_FREEZE_FILE
    ).read_text(encoding="utf-8") == "BLOCKED_OPENINGS\n"
    assert stopped == []


def test_readiness_rejects_reused_pid_even_when_writer_lock_is_held(
    monkeypatch,
    tmp_path,
):
    runtime = load_runtime_module()
    pid = 777
    path = tmp_path / runtime.OKX_RUNTIME_READY_FILE
    path.write_text(
        json.dumps(
            {
                "status": "READY",
                "execution_target": "OKX_DEMO",
                "adapter": "ATTESTED",
                "reconciliation": "RECONCILED",
                "writer": "UNIQUE",
                "pid": pid,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda *_args: {
            "service": "okx_runtime",
            "pid": pid,
            "running": True,
        },
    )
    monkeypatch.setattr(
        runtime,
        "is_managed_process",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        runtime,
        "_writer_lock_holder",
        lambda _state_dir: pid,
    )

    assert runtime.okx_runtime_readiness(tmp_path)["status"] == "BLOCKED"


def test_incomplete_startup_cleanup_never_claims_clean(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, service: {
            "service": service,
            "pid": 88 if service == "okx_runtime" else None,
            "running": service == "okx_runtime",
        },
    )
    monkeypatch.setattr(
        runtime,
        "orphaned_managed_process_map",
        lambda _state_dir, services: {
            service: [] for service in services
        },
    )
    monkeypatch.setattr(runtime, "_writer_lock_holder", lambda _path: 88)

    with pytest.raises(runtime.RuntimeBlocked, match="cleanup is incomplete"):
        runtime.require_complete_startup_cleanup(
            tmp_path,
            {
                "services": [
                    {"service": "okx_runtime", "status": "BLOCKED"}
                ]
            },
        )


def test_startup_cleanup_accepts_zombie_as_terminal(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    monkeypatch.setattr(
        runtime,
        "process_status",
        lambda _state_dir, service: {
            "service": service,
            "pid": 89 if service == "backend" else None,
            "running": False,
            "process_state": (
                runtime.PROCESS_STATE_ZOMBIE
                if service == "backend"
                else runtime.PROCESS_STATE_EXITED
            ),
        },
    )
    monkeypatch.setattr(
        runtime,
        "orphaned_managed_process_map",
        lambda _state_dir, services: {service: [] for service in services},
    )
    monkeypatch.setattr(runtime, "_writer_lock_holder", lambda _path: None)

    runtime.require_complete_startup_cleanup(
        tmp_path,
        {
            "services": [
                {"service": "backend", "status": "stale-pid-removed"}
            ]
        },
    )


def test_recent_logs_refuses_symlink(monkeypatch, tmp_path):
    runtime = load_runtime_module()
    target = tmp_path / "private.txt"
    target.write_text("password=must-not-appear\n", encoding="utf-8")
    (tmp_path / runtime.LOG_FILES["backend"]).symlink_to(target)

    payload = runtime.recent_logs(tmp_path, 10)

    assert payload["backend"]["status"] == "BLOCKED"
    assert "must-not-appear" not in str(payload)
