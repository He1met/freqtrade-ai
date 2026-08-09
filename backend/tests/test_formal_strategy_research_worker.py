from argparse import Namespace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from scripts.formal_strategy_research_worker import execute, terminate_process_group


NOW = datetime(2026, 8, 9, 5, 22, tzinfo=timezone.utc)


class FakeChild:
    def __init__(self, returncode=None, *, pid=43210):
        self.returncode = returncode
        self.pid = pid

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("research", timeout)
        return self.returncode


def args_for(tmp_path: Path, **updates) -> Namespace:
    data_file = tmp_path / "data/futures/BTC_USDT_USDT-15m-futures.feather"
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"fixture")
    values = {
        "lock_fd": 3,
        "run_id": "202608090515",
        "trigger": "manual",
        "freqtrade": tmp_path / "freqtrade",
        "datadir": tmp_path / "data",
        "repository_commit": "a" * 40,
        "state_path": tmp_path / "state.json",
        "started_at": NOW.isoformat(),
        "deadline_at": (NOW + timedelta(hours=1)).isoformat(),
        "deadline_seconds": 3600.0,
        "heartbeat_seconds": 5.0,
        "termination_grace_seconds": 10.0,
        "attempt_id": "00000000-0000-4000-8000-000000000001",
        "market_data_quality_receipt_id": 1,
        "expected_market_data_sha256": hashlib.sha256(b"fixture").hexdigest(),
    }
    values.update(updates)
    return Namespace(**values)


def test_worker_records_success_with_terminal_heartbeat(tmp_path):
    args = args_for(tmp_path)
    terminal = []
    result = execute(
        args,
        popen=lambda *unused_args, **unused_kwargs: FakeChild(0),
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda unused: None,
        terminal_recorder=lambda *unused_args, **kwargs: terminal.append(kwargs),
    )
    state = json.loads(args.state_path.read_text())
    assert result == 0
    assert state["status"] == "COMPLETED"
    assert state["phase"] == "FINISHED"
    assert state["heartbeat_at"] == NOW.isoformat()
    assert state["cleanup_status"] == "NOT_REQUIRED"
    assert terminal == [
        {
            "outcome": "COMPLETED",
            "reason_code": "COMPLETED",
            "reason": "正式路径已完成 10 条候选的生成、验证与全量持久化。",
        }
    ]


def test_worker_deadline_terminates_only_child_process_group(tmp_path):
    args = args_for(tmp_path, deadline_seconds=1.0)
    child = FakeChild()
    calls = []
    moments = iter((0.0, 2.0))

    def killpg(pid, signum):
        calls.append((pid, signum))
        child.returncode = -signum

    result = execute(
        args,
        popen=lambda *unused_args, **unused_kwargs: child,
        clock=lambda: NOW,
        monotonic=lambda: next(moments),
        sleep=lambda unused: None,
        killpg=killpg,
        terminal_recorder=lambda *unused_args, **unused_kwargs: None,
    )
    state = json.loads(args.state_path.read_text())
    assert result == 124
    assert calls == [(child.pid, signal.SIGTERM)]
    assert state["status"] == "FAILED"
    assert state["reason_code"] == "WORKER_DEADLINE_EXCEEDED"
    assert state["cleanup_status"] == "TERM_CONFIRMED"


def test_worker_blocks_when_process_group_cleanup_cannot_be_proven(tmp_path):
    args = args_for(tmp_path, deadline_seconds=1.0)
    child = FakeChild()
    moments = iter((0.0, 2.0))

    def denied(_pid, _signum):
        raise PermissionError("denied")

    result = execute(
        args,
        popen=lambda *unused_args, **unused_kwargs: child,
        clock=lambda: NOW,
        monotonic=lambda: next(moments),
        sleep=lambda unused: None,
        killpg=denied,
        terminal_recorder=lambda *unused_args, **unused_kwargs: None,
    )
    state = json.loads(args.state_path.read_text())
    assert result == 2
    assert state["status"] == "BLOCKED"
    assert state["reason_code"] == "PROCESS_GROUP_CLEANUP_UNCONFIRMED"
    assert state["cleanup_status"] == "UNCONFIRMED"


def test_process_group_cleanup_escalates_from_term_to_kill():
    child = FakeChild()
    calls = []

    def killpg(pid, signum):
        calls.append((pid, signum))
        if signum == signal.SIGKILL:
            child.returncode = -signal.SIGKILL

    cleanup = terminate_process_group(child, grace_seconds=0.01, killpg=killpg)

    assert cleanup == "KILL_CONFIRMED"
    assert calls == [
        (child.pid, signal.SIGTERM),
        (child.pid, signal.SIGKILL),
    ]


def test_process_group_cleanup_reaps_real_child_and_grandchild():
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        cleanup = terminate_process_group(child, grace_seconds=2.0)
        assert cleanup in {"TERM_CONFIRMED", "KILL_CONFIRMED"}
        assert child.poll() is not None
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("research process group still exists after confirmed cleanup")
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=2)
