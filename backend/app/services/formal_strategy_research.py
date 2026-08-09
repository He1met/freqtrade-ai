from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app.adapters.freqtrade.binary import resolve_freqtrade_binary
from app.core.config import Settings, get_settings
from app.repositories.strategy_research import StrategyResearchRepository
from app.schemas.strategy_research import FormalResearchRunRead


EXPECTED_CANDIDATE_COUNT = 10
OWNERSHIP_SCHEMA = "freqtrade-ai-formal-research-ownership-v1"
STATE_SCHEMA = "freqtrade-ai-formal-research-state-v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _safe_text(value: object) -> str:
    return re.sub(
        r"(?i)\b(api[_-]?key|secret|password|passphrase|token)(\s*[:=]\s*)\S+",
        r"\1\2[REDACTED]",
        str(value),
    )[:2000]


def _parse_datetime(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


class FormalStrategyResearchCoordinator:
    """One fail-closed entry shared by the page and the scheduled automation."""

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        clock: Callable[[], datetime] = _utc_now,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.settings = settings or get_settings()
        self.clock = clock
        self.popen = popen
        self.repo = self.settings.canonical_repo_root.expanduser().resolve()
        self.runtime_dir = self.repo / ".freqtrade-ai" / "research"
        self.lock_path = self.runtime_dir / "formal-strategy-research.lock"
        self.state_path = self.runtime_dir / "formal-strategy-research-state.json"
        self.ownership_path = self.runtime_dir / "formal-strategy-research-ownership.json"

    def _paths(self) -> tuple[Optional[Path], Path]:
        resolution = resolve_freqtrade_binary(
            runtime_env_path=self.repo / ".freqtrade-ai" / "runtime.env"
        )
        configured_data = self.settings.market_data_dir.expanduser()
        datadir = (
            configured_data if configured_data.is_absolute() else self.repo / configured_data
        ).resolve()
        market_filename = "BTC_USDT_USDT-15m-futures.feather"
        primary_market = datadir / "futures" / market_filename
        nested_market = datadir / "okx" / "futures" / market_filename
        if not primary_market.is_file() and nested_market.is_file():
            datadir = datadir / "okx"
        return resolution.resolved_path, datadir

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": STATE_SCHEMA, **state}
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _blocked(self, code: str, reason: str) -> FormalResearchRunRead:
        return FormalResearchRunRead(
            status="BLOCKED", reason_code=code, reason=reason, active=False
        )

    def _preflight(self) -> Optional[FormalResearchRunRead]:
        target = self.settings.execution_target_manifest.active_target
        if (
            target.target_id != "OKX_DEMO"
            or target.account_mode != "demo"
            or target.simulated_trading is not True
            or target.allow_real_funds is not False
            or target.order_submission_enabled is not False
            or self.settings.allow_live_trading
            or self.settings.allow_dry_run_trading
        ):
            return self._blocked(
                "UNSAFE_EXECUTION_TARGET",
                "正式研究入口仅允许 OKX_DEMO，且 allow_real_funds=false、real_orders=false、Dry-run 交易关闭。",
            )
        ownership = self._read_json(self.ownership_path)
        now = _aware(self.clock())
        confirmed_at = _parse_datetime(ownership.get("confirmed_at"))
        expires_at = _parse_datetime(ownership.get("expires_at"))
        if (
            ownership.get("schema_version") != OWNERSHIP_SCHEMA
            or ownership.get("scope") != "FORMAL_STRATEGY_RESEARCH"
            or ownership.get("canonical_root") != str(self.repo)
            or not isinstance(ownership.get("owner_task_id"), str)
            or not ownership.get("owner_task_id")
            or confirmed_at is None
            or expires_at is None
            or confirmed_at > now
            or expires_at <= now
            or expires_at - confirmed_at > timedelta(minutes=30)
        ):
            return self._blocked(
                "OWNERSHIP_EVIDENCE_MISSING_OR_STALE",
                "缺少当前 30 分钟内、指向唯一正式研究所有者的完整所有权证据。",
            )
        freqtrade, datadir = self._paths()
        data_file = datadir / "futures" / "BTC_USDT_USDT-15m-futures.feather"
        candidates = sorted((self.repo / "research/strategy_candidates").glob("[0-9][0-9]_*.py"))
        if freqtrade is None or not freqtrade.is_file():
            return self._blocked("FREQTRADE_BINARY_MISSING", "正式研究 Freqtrade 可执行文件不存在。")
        if not data_file.is_file():
            return self._blocked("MARKET_DATA_MISSING", "BTC/USDT:USDT 15m 正式研究数据不存在。")
        if len(candidates) != EXPECTED_CANDIDATE_COUNT:
            return self._blocked(
                "CANDIDATE_SET_INCOMPLETE",
                f"正式研究候选集合必须恰好为 10 条，当前为 {len(candidates)} 条。",
            )
        return None

    def _try_lock(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    def _latest_counts(self, db: Session, state: dict[str, Any]) -> dict[str, Any]:
        run_id = state.get("run_id")
        batch = (
            StrategyResearchRepository(db).get_batch_by_run_id(run_id)
            if isinstance(run_id, str) and run_id
            else None
        )
        if batch is None:
            return {}
        validated = sum(
            candidate.status in {"QUALIFIED", "REJECTED"} for candidate in batch.candidates
        )
        handoff = (
            "QUEUED_FOR_EXISTING_AUTOMATION"
            if batch.qualified_count > 0 and validated == batch.generated_count == batch.persisted_count
            else "NOT_QUEUED_NO_QUALIFIED"
        )
        return {
            "requested_count": batch.requested_count,
            "generated_count": batch.generated_count,
            "validated_count": validated,
            "persisted_count": batch.persisted_count,
            "qualified_count": batch.qualified_count,
            "rejected_count": batch.rejected_count,
            "deployment_handoff_status": handoff,
        }

    def status(self, db: Session) -> FormalResearchRunRead:
        state = self._read_json(self.state_path)
        lock = self._try_lock()
        active = lock is None
        if lock is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        if active:
            status = "RUNNING"
            code = "ACTIVE_RESEARCH"
            reason = "已有正式研究正在运行；防重入门禁拒绝重复启动。"
        elif state.get("status") == "RUNNING":
            status = "BLOCKED"
            code = "RUN_STATE_INCONSISTENT"
            reason = "状态记录为 RUNNING，但唯一研究锁未被持有；拒绝猜测或自动重放。"
        elif state.get("status") in {"COMPLETED", "FAILED", "BLOCKED"}:
            status = state["status"]
            code = str(state.get("reason_code") or status)
            reason = _safe_text(state.get("reason") or "正式研究状态已持久化。")
        else:
            blocked = self._preflight()
            if blocked is not None:
                return blocked
            status, code, reason = "READY", "READY", "所有正式研究门禁已通过，可以运行一轮。"
        return FormalResearchRunRead(
            status=status,
            reason_code=code,
            reason=reason,
            active=active,
            run_id=state.get("run_id"),
            trigger=state.get("trigger"),
            started_at=_parse_datetime(state.get("started_at")),
            completed_at=_parse_datetime(state.get("completed_at")),
            **self._latest_counts(db, state),
        )

    def start(self, db: Session, *, trigger: str) -> FormalResearchRunRead:
        blocked = self._preflight()
        if blocked is not None:
            return blocked
        lock = self._try_lock()
        if lock is None:
            return self._blocked("ACTIVE_RESEARCH", "已有正式研究正在运行；防重入门禁拒绝重复启动。")
        state = self._read_json(self.state_path)
        if state.get("status") == "RUNNING":
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            return self._blocked(
                "RUN_STATE_INCONSISTENT",
                "状态记录为 RUNNING，但唯一研究锁未被持有；拒绝猜测或自动重放。",
            )
        now = _aware(self.clock())
        slot = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        run_id = slot.strftime("%Y%m%d%H%M")
        if StrategyResearchRepository(db).get_batch_by_run_id(run_id) is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            return self._blocked("DUPLICATE_SLOT", f"15 分钟槽位 {run_id} 已有持久化批次，拒绝重复运行。")
        freqtrade, datadir = self._paths()
        if freqtrade is None:  # guarded by _preflight; retain a fail-closed type boundary
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            return self._blocked("FREQTRADE_BINARY_MISSING", "正式研究 Freqtrade 可执行文件不存在。")
        started_at = now.isoformat()
        self._write_state(
            {
                "status": "RUNNING",
                "reason_code": "STARTED",
                "reason": "正式研究已进入后台执行。",
                "run_id": run_id,
                "trigger": trigger,
                "started_at": started_at,
            }
        )
        worker = self.repo / "scripts/formal_strategy_research_worker.py"
        command = [
            sys.executable,
            str(worker),
            "--lock-fd", str(lock.fileno()),
            "--run-id", run_id,
            "--trigger", trigger,
            "--freqtrade", str(freqtrade),
            "--datadir", str(datadir),
            "--repository-commit", self._repository_commit(),
            "--state-path", str(self.state_path),
        ]
        try:
            self.popen(
                command,
                cwd=self.repo,
                pass_fds=(lock.fileno(),),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            reason = f"无法启动正式研究后台进程：{_safe_text(exc)}"
            self._write_state({
                "status": "BLOCKED", "reason_code": "WORKER_START_FAILED", "reason": reason,
                "run_id": run_id, "trigger": trigger, "started_at": started_at,
                "completed_at": _aware(self.clock()).isoformat(),
            })
            return self._blocked("WORKER_START_FAILED", reason)
        lock.close()
        return FormalResearchRunRead(
            status="RUNNING",
            reason_code="STARTED",
            reason="正式研究已进入后台执行；页面轮询同一持久化状态，不会重复提交。",
            active=True,
            run_id=run_id,
            trigger=trigger,
            started_at=now,
        )

    def _repository_commit(self) -> str:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True
        )
        return completed.stdout.strip()


def get_formal_strategy_research_coordinator() -> FormalStrategyResearchCoordinator:
    return FormalStrategyResearchCoordinator()
