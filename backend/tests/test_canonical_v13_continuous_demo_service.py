from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID


NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
DEPLOYMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "canonical_v13_continuous_demo_soak.py"
)


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configure_roots(module, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(module, "SUPPORT_ROOT", tmp_path / "support")
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "support" / "state.json")
    monkeypatch.setattr(module, "PLAN_PATH", tmp_path / "support" / "plan.json")
    monkeypatch.setattr(
        module, "RECEIPTS_PATH", tmp_path / "support" / "receipts.jsonl"
    )
    monkeypatch.setattr(module, "PLIST_PATH", tmp_path / "agents" / "soak.plist")
    monkeypatch.setattr(module, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(module, "PYTHON", tmp_path / "venv" / "python")
    monkeypatch.setattr(module, "_now", lambda: NOW)


def test_prepare_freezes_natural_signal_and_single_position_boundaries(
    tmp_path, monkeypatch
) -> None:
    service = _load_script("canonical_continuous_demo_prepare")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_release", lambda: ("a" * 40, "b" * 64))
    monkeypatch.setattr(service, "_active_deployment_id", lambda: DEPLOYMENT_ID)

    plan = service.prepare()

    assert plan["execution_target"] == "OKX_DEMO"
    assert plan["instrument"] == "BTC-USDT-SWAP"
    assert plan["signal_timeframe"] == "15m"
    assert plan["natural_signals_only"] is True
    assert plan["single_position"] is True
    assert plan["same_signal_dispatch_maximum"] == 1
    assert plan["drain_until_flat"] is True
    assert plan["allow_real_funds"] is False
    assert set(plan) == {
        "contract",
        "status",
        "release_sha",
        "release_digest",
        "deployment_id",
        "execution_target",
        "instrument",
        "signal_timeframe",
        "natural_signals_only",
        "single_position",
        "same_signal_dispatch_maximum",
        "allow_real_funds",
        "prepared_at",
        "starts_at",
        "openings_end_at",
        "drain_until_flat",
        "repeat_noop",
    }
    assert service.PLIST_PATH.exists()
    assert "EnvironmentVariables" in service.plistlib.loads(
        service.PLIST_PATH.read_bytes()
    )


def test_prepare_replaces_a_stopped_blocked_worker_with_current_release(
    tmp_path, monkeypatch
) -> None:
    service = _load_script("canonical_continuous_demo_prepare_recovery")
    _configure_roots(service, tmp_path, monkeypatch)
    service._atomic(
        service.PLAN_PATH,
        {
            "contract": service.CONTRACT,
            "status": "RUNNING",
            "release_sha": "0" * 40,
            "release_digest": "1" * 64,
            "deployment_id": str(DEPLOYMENT_ID),
            "allow_real_funds": False,
        },
    )
    service._atomic(
        service.STATE_PATH,
        {
            "contract": service.CONTRACT,
            "status": "BLOCKED",
            "reason_code": "BLOCKED_CONTINUOUS_EXIT_GUARD_DRIFT",
            "allow_real_funds": False,
        },
    )
    monkeypatch.setattr(service, "_release", lambda: ("a" * 40, "b" * 64))
    monkeypatch.setattr(service, "_active_deployment_id", lambda: DEPLOYMENT_ID)
    calls = []
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command)
        or SimpleNamespace(returncode=0),
    )

    plan = service.prepare()

    assert plan["status"] == "PREPARED"
    assert plan["release_sha"] == "a" * 40
    assert plan["release_digest"] == "b" * 64
    assert plan["repeat_noop"] is False
    assert service._read(service.STATE_PATH)["status"] == "PREPARED"
    assert calls == [
        ["launchctl", "bootout", f"gui/{service.os.getuid()}/{service.LABEL}"]
    ]


def test_drained_soak_fails_closed_when_runtime_stop_fails(
    tmp_path, monkeypatch
) -> None:
    service = _load_script("canonical_continuous_demo_stop_failure")
    _configure_roots(service, tmp_path, monkeypatch)
    stopped = {
        "contract": service.CONTRACT,
        "status": "STOPPED",
        "allow_real_funds": False,
    }
    monkeypatch.setattr(service, "tick", lambda: stopped)
    monkeypatch.setattr(
        service,
        "_phase9",
        lambda: SimpleNamespace(
            stop=lambda _service: (_ for _ in ()).throw(
                RuntimeError("BLOCKED_RUNTIME_STOP")
            )
        ),
    )

    assert service.run() == 1
    state = service._read(service.STATE_PATH)
    assert state["status"] == "BLOCKED"
    assert state["reason_code"] == "BLOCKED_RUNTIME_STOP"
    assert state["allow_real_funds"] is False


def test_confirm_running_plan_is_exact_noop(tmp_path, monkeypatch) -> None:
    service = _load_script("canonical_continuous_demo_confirm_replay")
    _configure_roots(service, tmp_path, monkeypatch)
    running = {
        "contract": service.CONTRACT,
        "status": "RUNNING",
        "release_sha": "a" * 40,
        "release_digest": "b" * 64,
        "deployment_id": str(DEPLOYMENT_ID),
        "allow_real_funds": False,
    }
    service._atomic(service.PLAN_PATH, running)
    monkeypatch.setattr(service, "_release", lambda: ("a" * 40, "b" * 64))
    monkeypatch.setattr(
        service,
        "status",
        lambda: {"status": "RUNNING", "launch_agent_loaded": True},
    )
    monkeypatch.setattr(
        service,
        "_runtime_ready_for_openings",
        lambda: (_ for _ in ()).throw(AssertionError("must not reset the window")),
    )

    replay = service.confirm()

    assert replay["repeat_noop"] is True
    assert replay["status"] == "RUNNING"
    assert service._read(service.PLAN_PATH) == running


def test_finalize_requires_drained_state_and_unloads_agent(
    tmp_path, monkeypatch
) -> None:
    service = _load_script("canonical_continuous_demo_finalize")
    _configure_roots(service, tmp_path, monkeypatch)
    service.PLIST_PATH.parent.mkdir(parents=True)
    service.PLIST_PATH.write_text("secret-free", encoding="utf-8")
    service._atomic(
        service.STATE_PATH,
        {"contract": service.CONTRACT, "status": "STOPPED", "allow_real_funds": False},
    )
    service._atomic(
        service.PLAN_PATH,
        {"contract": service.CONTRACT, "status": "DRAINING", "allow_real_funds": False},
    )
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(service.subprocess, "run", run)

    result = service.finalize()

    assert result["status"] == "STOPPED"
    assert result["launch_agent_loaded"] is False
    assert result["allow_real_funds"] is False
    assert not service.PLIST_PATH.exists()
    assert service._read(service.PLAN_PATH)["status"] == "STOPPED"
    assert calls == [["launchctl", "bootout", f"gui/{service.os.getuid()}/{service.LABEL}"]]


def test_tick_rejects_prepared_plan_before_opening_private_session(
    tmp_path, monkeypatch
) -> None:
    service = _load_script("canonical_continuous_demo_tick_prepared")
    _configure_roots(service, tmp_path, monkeypatch)
    service._atomic(
        service.PLAN_PATH,
        {
            "contract": service.CONTRACT,
            "status": "PREPARED",
            "openings_end_at": None,
            "allow_real_funds": False,
        },
    )
    monkeypatch.setattr(
        service,
        "_operator",
        lambda: (_ for _ in ()).throw(AssertionError("operator must not open")),
    )

    try:
        service.tick()
    except service.ContinuousDemoSoakServiceBlocked as exc:
        assert str(exc) == "BLOCKED_SOAK_PLAN_NOT_RUNNING"
    else:
        raise AssertionError("prepared plan must fail closed")
