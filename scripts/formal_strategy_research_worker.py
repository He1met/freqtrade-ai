#!/usr/bin/env python3
"""Execute formal research with a heartbeat, deadline, and bounded process cleanup."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional


STATE_SCHEMA = "freqtrade-ai-formal-research-state-v1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def safe(value: object) -> str:
    return re.sub(
        r"(?i)\b(api[_-]?key|secret|password|passphrase|token)(\s*[:=]\s*)\S+",
        r"\1\2[REDACTED]",
        str(value),
    )[:2000]


def write_state(path: Path, payload: dict) -> None:
    value = {"schema_version": STATE_SCHEMA, **payload}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary, path)


def _base_state(args: argparse.Namespace, *, now: datetime) -> dict:
    return {
        "run_id": args.run_id,
        "trigger": args.trigger,
        "started_at": args.started_at,
        "heartbeat_at": now.isoformat(),
        "deadline_at": args.deadline_at,
        "attempt_id": args.attempt_id,
        "market_data_quality_receipt_id": args.market_data_quality_receipt_id,
    }


def _tail(handle, *, limit: int = 2000) -> str:
    try:
        handle.flush()
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit * 2))
        lines = handle.read().strip().splitlines()
    except (OSError, ValueError):
        return "research process failed"
    return safe(lines[-1] if lines else "research process failed")


def terminate_process_group(
    child: subprocess.Popen,
    *,
    grace_seconds: float,
    killpg: Callable[[int, int], None] = os.killpg,
) -> str:
    """Terminate exactly the research child's process group and prove it was reaped."""

    if child.poll() is not None:
        child.wait()
        return "TERM_CONFIRMED"
    try:
        killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            child.wait(timeout=grace_seconds)
        except (subprocess.TimeoutExpired, OSError):
            return "UNCONFIRMED"
        return "TERM_CONFIRMED"
    except (PermissionError, OSError):
        return "UNCONFIRMED"
    try:
        child.wait(timeout=grace_seconds)
        return "TERM_CONFIRMED"
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return "UNCONFIRMED"
    try:
        killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=grace_seconds)
        return "KILL_CONFIRMED"
    except ProcessLookupError:
        try:
            child.wait(timeout=grace_seconds)
        except (subprocess.TimeoutExpired, OSError):
            return "UNCONFIRMED"
        return "KILL_CONFIRMED"
    except (PermissionError, OSError, subprocess.TimeoutExpired):
        return "UNCONFIRMED"


def record_terminal_event(
    args: argparse.Namespace, *, outcome: str, reason_code: str, reason: str
) -> None:
    """Append terminal evidence through the ordinary research persistence role."""

    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "backend"))
    from app.db.session import SessionLocal
    from app.models.strategy_research import StrategyResearchAttemptEvent
    from app.repositories.strategy_research import StrategyResearchRepository

    with SessionLocal() as db:
        repository = StrategyResearchRepository(db)
        if repository.get_attempt_event(attempt_id=args.attempt_id, sequence=2) is not None:
            return
        batch = repository.get_batch_by_run_id(args.run_id)
        generated = batch.generated_count if batch is not None else 0
        persisted = batch.persisted_count if batch is not None else 0
        qualified = batch.qualified_count if batch is not None else 0
        rejected = batch.rejected_count if batch is not None else 0
        validated = qualified + rejected
        if outcome == "COMPLETED" and batch is None:
            raise RuntimeError("completed research has no persisted batch")
        identity = {
            "attempt_id": args.attempt_id,
            "sequence": 2,
            "run_id": args.run_id,
            "batch_id": batch.id if batch is not None else None,
            "outcome": outcome,
            "reason_code": reason_code,
            "quality_receipt_id": args.market_data_quality_receipt_id,
            "counts": [10, generated, validated, persisted, qualified, rejected],
        }
        event_digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        repository.append_attempt_event(
            StrategyResearchAttemptEvent(
                attempt_id=args.attempt_id,
                sequence=2,
                run_id=args.run_id,
                batch_id=batch.id if batch is not None else None,
                market_data_quality_receipt_id=args.market_data_quality_receipt_id,
                trigger=args.trigger,
                phase="TERMINAL",
                outcome=outcome,
                reason_code=reason_code,
                redacted_reason=safe(reason),
                requested_count=10,
                generated_count=generated,
                validated_count=validated,
                persisted_count=persisted,
                qualified_count=qualified,
                rejected_count=rejected,
                evidence_snapshot={
                    "execution_target": "OKX_DEMO",
                    "allow_real_funds": False,
                    "real_orders": False,
                },
                event_digest=event_digest,
            )
        )
def execute(
    args: argparse.Namespace,
    *,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    clock: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    killpg: Callable[[int, int], None] = os.killpg,
    terminal_recorder: Callable[..., None] = record_terminal_event,
) -> int:
    repo = Path(__file__).resolve().parents[1]
    data_file = args.datadir / "futures" / "BTC_USDT_USDT-15m-futures.feather"
    digest = hashlib.sha256(data_file.read_bytes()).hexdigest()
    if digest != args.expected_market_data_sha256:
        now = clock()
        terminal_recorder(
            args,
            outcome="BLOCKED",
            reason_code="MARKET_DATA_DIGEST_CHANGED",
            reason="数据文件在质量检查后发生变化；本轮未启动研究子进程。",
        )
        write_state(
            args.state_path,
            {
                "status": "BLOCKED",
                "reason_code": "MARKET_DATA_DIGEST_CHANGED",
                "reason": "数据文件在质量检查后发生变化；本轮未启动研究子进程。",
                **_base_state(args, now=now),
                "completed_at": now.isoformat(),
                "phase": "FINISHED",
                "cleanup_status": "NOT_REQUIRED",
            },
        )
        return 2
    command = [
        sys.executable,
        str(repo / "scripts/run_strategy_candidate_research.py"),
        "--freqtrade",
        str(args.freqtrade),
        "--datadir",
        str(args.datadir),
        "--run-id",
        args.run_id,
        "--persist-database",
        "--repository-commit",
        args.repository_commit,
    ]
    interrupted: dict[str, Optional[int]] = {"signal": None}

    def request_stop(signum, _frame) -> None:
        interrupted["signal"] = int(signum)

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    child: Optional[subprocess.Popen] = None
    started_monotonic = monotonic()
    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as output:
            try:
                child = popen(
                    command,
                    cwd=repo,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                now = clock()
                write_state(
                    args.state_path,
                    {
                        "status": "BLOCKED",
                        "reason_code": "WORKER_CHILD_START_FAILED",
                        "reason": f"无法启动正式研究子进程：{safe(exc)}",
                        **_base_state(args, now=now),
                        "completed_at": now.isoformat(),
                        "phase": "FINISHED",
                        "cleanup_status": "NOT_REQUIRED",
                    },
                )
                return 1

            write_state(
                args.state_path,
                {
                    "status": "RUNNING",
                    "reason_code": "ACTIVE_RESEARCH",
                    "reason": "正式研究后台 worker 正在运行。",
                    **_base_state(args, now=clock()),
                    "phase": "RUNNING",
                    "cleanup_status": "NOT_REQUIRED",
                },
            )
            while child.poll() is None:
                elapsed = monotonic() - started_monotonic
                if interrupted["signal"] is not None or elapsed >= args.deadline_seconds:
                    now = clock()
                    write_state(
                        args.state_path,
                        {
                            "status": "RUNNING",
                            "reason_code": "WORKER_TERMINATING",
                            "reason": "正式研究已停止接收进度，正在清理其专属子进程组。",
                            **_base_state(args, now=now),
                            "phase": "TERMINATING",
                            "cleanup_status": "NOT_REQUIRED",
                        },
                    )
                    cleanup = terminate_process_group(
                        child,
                        grace_seconds=args.termination_grace_seconds,
                        killpg=killpg,
                    )
                    completed_at = clock()
                    if cleanup == "UNCONFIRMED":
                        terminal_recorder(
                            args, outcome="BLOCKED",
                            reason_code="PROCESS_GROUP_CLEANUP_UNCONFIRMED",
                            reason="研究子进程组退出状态无法确认；人工核对前禁止重放。",
                        )
                        write_state(
                            args.state_path,
                            {
                                "status": "BLOCKED",
                                "reason_code": "PROCESS_GROUP_CLEANUP_UNCONFIRMED",
                                "reason": "研究子进程组退出状态无法确认；人工核对前禁止重放。",
                                **_base_state(args, now=completed_at),
                                "completed_at": completed_at.isoformat(),
                                "phase": "FINISHED",
                                "cleanup_status": cleanup,
                            },
                        )
                        return 2
                    reason_code = (
                        "WORKER_INTERRUPTED"
                        if interrupted["signal"] is not None
                        else "WORKER_DEADLINE_EXCEEDED"
                    )
                    reason = (
                        "正式研究 worker 收到停止信号，子进程组已受控退出；本轮不会自动重放。"
                        if interrupted["signal"] is not None
                        else "正式研究超过执行时限，子进程组已受控退出；本轮不会自动重放。"
                    )
                    write_state(
                        args.state_path,
                        {
                            "status": "FAILED",
                            "reason_code": reason_code,
                            "reason": reason,
                            **_base_state(args, now=completed_at),
                            "completed_at": completed_at.isoformat(),
                            "phase": "FINISHED",
                            "cleanup_status": cleanup,
                        },
                    )
                    terminal_recorder(
                        args, outcome="FAILED", reason_code=reason_code, reason=reason
                    )
                    return 124 if interrupted["signal"] is None else 128 + int(interrupted["signal"])
                write_state(
                    args.state_path,
                    {
                        "status": "RUNNING",
                        "reason_code": "ACTIVE_RESEARCH",
                        "reason": "正式研究后台 worker 正在运行。",
                        **_base_state(args, now=clock()),
                        "phase": "RUNNING",
                        "cleanup_status": "NOT_REQUIRED",
                    },
                )
                sleep(args.heartbeat_seconds)

            returncode = child.wait()
            completed_at = clock()
            if returncode == 0:
                terminal_recorder(
                    args,
                    outcome="COMPLETED",
                    reason_code="COMPLETED",
                    reason="正式路径已完成 10 条候选的生成、验证与全量持久化。",
                )
                write_state(
                    args.state_path,
                    {
                        "status": "COMPLETED",
                        "reason_code": "COMPLETED",
                        "reason": "正式路径已完成 10 条候选的生成、验证与全量持久化。",
                        **_base_state(args, now=completed_at),
                        "completed_at": completed_at.isoformat(),
                        "phase": "FINISHED",
                        "cleanup_status": "NOT_REQUIRED",
                    },
                )
                return 0
            detail = _tail(output)
            terminal_recorder(
                args,
                outcome="FAILED",
                reason_code="RESEARCH_PROCESS_FAILED",
                reason=f"正式研究进程失败：{detail}",
            )
            write_state(
                args.state_path,
                {
                    "status": "FAILED",
                    "reason_code": "RESEARCH_PROCESS_FAILED",
                    "reason": f"正式研究进程失败：{detail}",
                    **_base_state(args, now=completed_at),
                    "completed_at": completed_at.isoformat(),
                    "phase": "FINISHED",
                    "cleanup_status": "NOT_REQUIRED",
                },
            )
            return returncode
    except Exception as exc:
        completed_at = clock()
        cleanup = "NOT_REQUIRED"
        if child is not None and child.poll() is None:
            cleanup = terminate_process_group(
                child,
                grace_seconds=args.termination_grace_seconds,
                killpg=killpg,
            )
        cleanup_confirmed = cleanup != "UNCONFIRMED"
        write_state(
            args.state_path,
            {
                "status": "FAILED" if cleanup_confirmed else "BLOCKED",
                "reason_code": (
                    "WORKER_INTERNAL_ERROR"
                    if cleanup_confirmed
                    else "PROCESS_GROUP_CLEANUP_UNCONFIRMED"
                ),
                "reason": (
                    f"正式研究 worker 内部失败，子进程已停止：{safe(exc)}"
                    if cleanup_confirmed
                    else "正式研究 worker 内部失败，且子进程组退出状态无法确认；人工核对前禁止重放。"
                ),
                **_base_state(args, now=completed_at),
                "completed_at": completed_at.isoformat(),
                "phase": "FINISHED",
                "cleanup_status": cleanup,
            },
        )
        return 1 if cleanup_confirmed else 2
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--trigger", choices=("manual", "automation"), required=True)
    parser.add_argument("--freqtrade", type=Path, required=True)
    parser.add_argument("--datadir", type=Path, required=True)
    parser.add_argument("--repository-commit", required=True)
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--deadline-at", required=True)
    parser.add_argument("--deadline-seconds", type=float, required=True)
    parser.add_argument("--heartbeat-seconds", type=float, required=True)
    parser.add_argument("--termination-grace-seconds", type=float, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--market-data-quality-receipt-id", type=int, required=True)
    parser.add_argument("--expected-market-data-sha256", required=True)
    args = parser.parse_args(argv)
    if args.deadline_seconds <= 0 or args.heartbeat_seconds <= 0 or args.termination_grace_seconds <= 0:
        parser.error("deadline, heartbeat, and termination grace must be positive")
    return args


def main() -> int:
    return execute(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
