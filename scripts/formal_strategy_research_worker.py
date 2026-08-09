#!/usr/bin/env python3
"""Execute the formal ten-candidate path while holding the shared research lock."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def safe(value: object) -> str:
    return re.sub(
        r"(?i)\b(api[_-]?key|secret|password|passphrase|token)(\s*[:=]\s*)\S+",
        r"\1\2[REDACTED]", str(value),
    )[:2000]


def write_state(path: Path, payload: dict) -> None:
    value = {"schema_version": "freqtrade-ai-formal-research-state-v1", **payload}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--trigger", choices=("manual", "automation"), required=True)
    parser.add_argument("--freqtrade", type=Path, required=True)
    parser.add_argument("--datadir", type=Path, required=True)
    parser.add_argument("--repository-commit", required=True)
    parser.add_argument("--state-path", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    started_at = datetime.now(timezone.utc).isoformat()
    command = [
        sys.executable, str(repo / "scripts/run_strategy_candidate_research.py"),
        "--freqtrade", str(args.freqtrade), "--datadir", str(args.datadir),
        "--run-id", args.run_id, "--persist-database",
        "--repository-commit", args.repository_commit,
    ]
    completed = subprocess.run(command, cwd=repo, check=False, capture_output=True, text=True)
    completed_at = datetime.now(timezone.utc).isoformat()
    if completed.returncode == 0:
        write_state(args.state_path, {
            "status": "COMPLETED", "reason_code": "COMPLETED",
            "reason": "正式路径已完成 10 条候选的生成、验证与全量持久化。",
            "run_id": args.run_id, "trigger": args.trigger,
            "started_at": started_at, "completed_at": completed_at,
        })
        return 0
    detail = safe(completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "research process failed")
    write_state(args.state_path, {
        "status": "FAILED", "reason_code": "RESEARCH_PROCESS_FAILED",
        "reason": f"正式研究进程失败：{detail}", "run_id": args.run_id,
        "trigger": args.trigger, "started_at": started_at, "completed_at": completed_at,
    })
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
