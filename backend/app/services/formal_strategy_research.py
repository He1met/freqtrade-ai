from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Optional
import uuid

from sqlalchemy.orm import Session

from app.adapters.freqtrade.binary import resolve_freqtrade_binary
from app.core.config import Settings, get_settings
from app.core.strategy_research_matrix import (
    ALLOWED_RESEARCH_TIMEFRAMES,
    RESEARCH_TARGETS,
)
from app.repositories.strategy_research import StrategyResearchRepository
from app.schemas.strategy_research import FormalResearchRunRead
from app.models.strategy_research import StrategyResearchAttemptEvent
from app.services.market_data_quality import (
    inspect_market_data,
    verify_public_source_matrix,
)
from app.services.strategy_blueprint_equivalence import (
    StrategyBlueprintEquivalenceBlocked,
    prove_blueprint_code_equivalence,
)


EXPECTED_SOURCE_CANDIDATE_COUNT_PER_TIMEFRAME = 10
EXPECTED_SOURCE_CANDIDATE_COUNT = (
    EXPECTED_SOURCE_CANDIDATE_COUNT_PER_TIMEFRAME
    * len(ALLOWED_RESEARCH_TIMEFRAMES)
)
EXPECTED_CANDIDATE_COUNT = 60
OWNERSHIP_SCHEMA = "freqtrade-ai-formal-research-ownership-v1"
STATE_SCHEMA = "freqtrade-ai-formal-research-state-v1"
WORKER_DEADLINE_SECONDS = 60 * 60
WORKER_HEARTBEAT_INTERVAL_SECONDS = 5
WORKER_HEARTBEAT_STALE_SECONDS = 20
WORKER_TERMINATION_GRACE_SECONDS = 10
NON_REPLAYABLE_TERMINAL_CODES = {
    "COMPLETED",
    "RESEARCH_PROCESS_FAILED",
    "WORKER_DEADLINE_EXCEEDED",
    "WORKER_INTERRUPTED",
    "WORKER_INTERNAL_ERROR",
    "PROCESS_GROUP_CLEANUP_UNCONFIRMED",
}
VALID_PHASES = {"STARTING", "RUNNING", "TERMINATING", "FINISHED"}
VALID_CLEANUP_STATUSES = {
    "NOT_REQUIRED",
    "TERM_CONFIRMED",
    "KILL_CONFIRMED",
    "UNCONFIRMED",
}


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


def _state_enum(value: object, allowed: set[str]) -> Optional[str]:
    return value if isinstance(value, str) and value in allowed else None


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
            status="BLOCKED",
            reason_code=code,
            reason=reason,
            active=False,
            requested_count=0,
        )

    def _append_attempt_event(
        self,
        db: Session,
        *,
        attempt_id: str,
        sequence: int,
        trigger: str,
        phase: str,
        outcome: str,
        reason_code: str,
        reason: str,
        requested_count: int,
        run_id: Optional[str] = None,
        quality_receipt_id: Optional[int] = None,
    ) -> StrategyResearchAttemptEvent:
        evidence = {
            "execution_target": "OKX_DEMO",
            "allow_real_funds": False,
            "real_orders": False,
            "candidate_contract_count": EXPECTED_CANDIDATE_COUNT,
        }
        identity = {
            "attempt_id": attempt_id,
            "sequence": sequence,
            "run_id": run_id,
            "trigger": trigger,
            "phase": phase,
            "outcome": outcome,
            "reason_code": reason_code,
            "requested_count": requested_count,
            "market_data_quality_receipt_id": quality_receipt_id,
            "evidence_snapshot": evidence,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return StrategyResearchRepository(db).append_attempt_event(
            StrategyResearchAttemptEvent(
                attempt_id=attempt_id,
                sequence=sequence,
                run_id=run_id,
                market_data_quality_receipt_id=quality_receipt_id,
                trigger=trigger,
                phase=phase,
                outcome=outcome,
                reason_code=reason_code,
                redacted_reason=_safe_text(reason),
                requested_count=requested_count,
                generated_count=0,
                validated_count=0,
                persisted_count=0,
                qualified_count=0,
                rejected_count=0,
                evidence_snapshot=evidence,
                event_digest=digest,
            )
        )

    def _terminalize_orphaned_state(
        self, db: Session, state: dict[str, Any]
    ) -> FormalResearchRunRead:
        """Reconcile one unlocked RUNNING state without replaying its work."""

        run_id = state.get("run_id")
        batch = (
            StrategyResearchRepository(db).get_batch_by_run_id(run_id)
            if isinstance(run_id, str) and run_id
            else None
        )
        completed_at = batch.completed_at if batch is not None else _aware(self.clock())
        if batch is not None and batch.status == "VALIDATED":
            status = "COMPLETED"
            code = "RECOVERED_PERSISTED_BATCH"
            reason = (
                "旧运行锁已释放，但对应研究批次已完整持久化；"
                "已按数据库证据恢复终态，本次不重复启动。"
            )
        elif batch is not None and batch.status == "FAILED":
            status = "FAILED"
            code = "RECOVERED_FAILED_BATCH"
            reason = (
                "旧运行锁已释放，对应失败批次已持久化；"
                "已按数据库证据恢复终态，本次不重复启动。"
            )
        elif batch is not None:
            status = "BLOCKED"
            code = "RUN_DATABASE_BATCH_NON_TERMINAL"
            reason = "旧运行锁已释放，但数据库批次不是终态；拒绝猜测或自动重放。"
        else:
            status = "BLOCKED"
            code = "ORPHANED_RUN_STATE"
            reason = (
                "旧运行状态为 RUNNING，但唯一研究锁已释放且没有对应持久化批次；"
                "已终态化为阻塞，本次不自动重放。"
            )
        terminal = {
            "status": status,
            "reason_code": code,
            "reason": reason,
            "run_id": run_id,
            "trigger": state.get("trigger"),
            "started_at": state.get("started_at"),
            "completed_at": completed_at.isoformat(),
            "heartbeat_at": state.get("heartbeat_at"),
            "deadline_at": state.get("deadline_at"),
            "phase": "FINISHED",
            "cleanup_status": state.get("cleanup_status") or "NOT_REQUIRED",
        }
        counts = self._latest_counts(db, terminal)
        if not counts:
            counts = {"requested_count": 0}
        self._write_state(terminal)
        return FormalResearchRunRead(
            status=status,
            reason_code=code,
            reason=reason,
            active=False,
            run_id=run_id,
            trigger=state.get("trigger"),
            started_at=_parse_datetime(state.get("started_at")),
            heartbeat_at=_parse_datetime(state.get("heartbeat_at")),
            deadline_at=_parse_datetime(state.get("deadline_at")),
            completed_at=completed_at,
            phase="FINISHED",
            cleanup_status=state.get("cleanup_status") or "NOT_REQUIRED",
            **counts,
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
        missing_targets = [
            target.key for target in RESEARCH_TARGETS
            if not target.market_path(datadir).is_file()
        ]
        candidate_root = self.repo / "research/strategy_candidates"
        candidates_by_timeframe = {
            timeframe: sorted(
                (candidate_root / timeframe).glob("[0-9][0-9]_*.py")
            )
            for timeframe in ALLOWED_RESEARCH_TIMEFRAMES
        }
        candidates = [
            candidate
            for timeframe in ALLOWED_RESEARCH_TIMEFRAMES
            for candidate in candidates_by_timeframe[timeframe]
        ]
        legacy_candidates = sorted(candidate_root.glob("[0-9][0-9]_*.py"))
        if freqtrade is None or not freqtrade.is_file():
            return self._blocked("FREQTRADE_BINARY_MISSING", "正式研究 Freqtrade 可执行文件不存在。")
        if missing_targets:
            return self._blocked(
                "MARKET_DATA_MATRIX_MISSING",
                "正式研究数据矩阵不完整：" + ", ".join(missing_targets),
            )
        counts = {
            timeframe: len(candidates_by_timeframe[timeframe])
            for timeframe in ALLOWED_RESEARCH_TIMEFRAMES
        }
        if (
            len(candidates) != EXPECTED_SOURCE_CANDIDATE_COUNT
            or legacy_candidates
            or any(
                count != EXPECTED_SOURCE_CANDIDATE_COUNT_PER_TIMEFRAME
                for count in counts.values()
            )
        ):
            return self._blocked(
                "CANDIDATE_SET_INCOMPLETE",
                "正式研究必须由 5m/15m 各 10 个 timeframe-bound 蓝图生成"
                "六个研究单元各 10 条；"
                f"当前为 5m={counts.get('5m', 0)}、15m={counts.get('15m', 0)}，"
                f"旧顶层源码={len(legacy_candidates)}。",
            )
        for candidate in candidates:
            blueprint_path = candidate.with_suffix(".blueprint.json")
            try:
                blueprint_payload = json.loads(blueprint_path.read_text(encoding="utf-8"))
                if not isinstance(blueprint_payload, dict):
                    raise ValueError("blueprint payload must be an object")
                source_bytes = candidate.read_bytes()
                source_digest = hashlib.sha256(source_bytes).hexdigest()
                prove_blueprint_code_equivalence(
                    blueprint_payload=blueprint_payload,
                    source_bytes=source_bytes,
                    expected_source_digest=source_digest,
                    expected_class_name=str(blueprint_payload.get("class_name") or ""),
                    expected_timeframe=candidate.parent.name,
                )
            except (
                OSError,
                json.JSONDecodeError,
                StrategyBlueprintEquivalenceBlocked,
                ValueError,
            ):
                return self._blocked(
                    "CANDIDATE_BLUEPRINT_INVALID",
                    "正式研究候选缺少 timeframe-bound Blueprint 或源码不能由其逐字复现："
                    + str(candidate.relative_to(self.repo)),
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
            candidate.status in {"QUALIFIED", "REJECTED", "VALIDATION_FAILED"}
            for candidate in batch.candidates
        )
        handoff = (
            "CANONICAL_LINK_UNAVAILABLE"
            if batch.qualified_count > 0
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
            # A completed run is historical evidence.  Project the exact
            # contract persisted with that batch instead of silently applying
            # today's default contract to older research.
            "quality_contract": dict(batch.selection_policy or {}),
        }

    def status(self, db: Session) -> FormalResearchRunRead:
        state = self._read_json(self.state_path)
        lock = self._try_lock()
        active = lock is None
        if lock is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        if active:
            now = _aware(self.clock())
            heartbeat_at = _parse_datetime(state.get("heartbeat_at"))
            deadline_at = _parse_datetime(state.get("deadline_at"))
            if deadline_at is not None and deadline_at <= now:
                status = "BLOCKED"
                code = "WORKER_DEADLINE_CLEANUP_PENDING"
                reason = "正式研究已超过执行时限，后台 worker 正在清理子进程；清理确认前禁止重放。"
            elif (
                heartbeat_at is not None
                and now - heartbeat_at > timedelta(seconds=WORKER_HEARTBEAT_STALE_SECONDS)
            ):
                status = "BLOCKED"
                code = "WORKER_HEARTBEAT_STALE"
                reason = "正式研究锁仍被持有，但 worker 心跳已过期；保持阻塞并等待受控清理。"
            else:
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
            attempt_id=state.get("attempt_id"),
            market_data_quality_receipt_id=state.get("market_data_quality_receipt_id"),
            run_id=state.get("run_id"),
            trigger=state.get("trigger"),
            started_at=_parse_datetime(state.get("started_at")),
            heartbeat_at=_parse_datetime(state.get("heartbeat_at")),
            deadline_at=_parse_datetime(state.get("deadline_at")),
            completed_at=_parse_datetime(state.get("completed_at")),
            phase=_state_enum(state.get("phase"), VALID_PHASES),
            cleanup_status=_state_enum(
                state.get("cleanup_status"), VALID_CLEANUP_STATUSES
            ),
            **self._latest_counts(db, state),
        )

    def start(self, db: Session, *, trigger: str) -> FormalResearchRunRead:
        attempt_id = str(uuid.uuid4())
        blocked = self._preflight()
        if blocked is not None:
            self._append_attempt_event(
                db,
                attempt_id=attempt_id,
                sequence=1,
                trigger=trigger,
                phase="PRECHECK",
                outcome="NOT_GENERATED",
                reason_code=blocked.reason_code,
                reason=blocked.reason,
                requested_count=0,
            )
            blocked.attempt_id = attempt_id
            return blocked
        lock = self._try_lock()
        if lock is None:
            blocked = self._blocked("ACTIVE_RESEARCH", "已有正式研究正在运行；防重入门禁拒绝重复启动。")
            self._append_attempt_event(
                db, attempt_id=attempt_id, sequence=1, trigger=trigger,
                phase="PRECHECK", outcome="NOT_GENERATED",
                reason_code=blocked.reason_code, reason=blocked.reason, requested_count=0,
            )
            blocked.attempt_id = attempt_id
            return blocked
        state = self._read_json(self.state_path)
        if (
            state.get("cleanup_status") == "UNCONFIRMED"
            or state.get("reason_code") == "PROCESS_GROUP_CLEANUP_UNCONFIRMED"
        ):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            return self._blocked(
                "PROCESS_GROUP_CLEANUP_UNCONFIRMED",
                "上一轮研究子进程组未能证明已退出；人工核对前禁止启动任何新研究。",
            )
        if state.get("status") == "RUNNING":
            try:
                return self._terminalize_orphaned_state(db, state)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()
        now = _aware(self.clock())
        slot = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        run_id = slot.strftime("%Y%m%d%H%M")
        if (
            state.get("run_id") == run_id
            and state.get("reason_code") in NON_REPLAYABLE_TERMINAL_CODES
        ):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            return self._blocked(
                "DUPLICATE_SLOT_STATE",
                f"15 分钟槽位 {run_id} 已有不可自动重放的终态收据，拒绝再次运行。",
            )
        if StrategyResearchRepository(db).get_batch_by_run_id(run_id) is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            blocked = self._blocked("DUPLICATE_SLOT", f"15 分钟槽位 {run_id} 已有持久化批次，拒绝重复运行。")
            self._append_attempt_event(
                db, attempt_id=attempt_id, sequence=1, trigger=trigger,
                phase="PRECHECK", outcome="NOT_GENERATED",
                reason_code=blocked.reason_code, reason=blocked.reason,
                requested_count=0, run_id=run_id,
            )
            blocked.attempt_id = attempt_id
            return blocked
        freqtrade, datadir = self._paths()
        if freqtrade is None:  # guarded by _preflight; retain a fail-closed type boundary
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            return self._blocked("FREQTRADE_BINARY_MISSING", "正式研究 Freqtrade 可执行文件不存在。")
        inspected_qualities = []
        repository = StrategyResearchRepository(db)
        for target in RESEARCH_TARGETS:
            quality = inspect_market_data(
                target.market_path(datadir),
                repository_root=self.repo,
                exchange="okx",
                pair=target.pair,
                timeframe=target.timeframe,
                expected_interval_seconds=(5 if target.timeframe == "5m" else 15) * 60,
                inspected_at=now,
                require_source_receipt=True,
            )
            inspected_qualities.append(quality)
        source_matrix = verify_public_source_matrix(
            repository_root=self.repo,
            qualities=inspected_qualities,
            inspected_at=now,
        )
        qualities = [
            repository.append_market_data_quality_receipt(quality)
            for quality in inspected_qualities
        ]
        blocked_qualities = [quality for quality in qualities if quality.status != "PASSED"]
        quality = qualities[0]
        if blocked_qualities:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            blocked = self._blocked(
                "MARKET_DATA_QUALITY_BLOCKED",
                "正式研究数据质量门未通过：" + "; ".join(
                    f"{item.pair} {item.timeframe}: {','.join(item.reason_codes)}"
                    for item in blocked_qualities
                ),
            )
            self._append_attempt_event(
                db, attempt_id=attempt_id, sequence=1, trigger=trigger,
                phase="PRECHECK", outcome="NOT_GENERATED",
                reason_code=blocked.reason_code, reason=blocked.reason,
                requested_count=0, run_id=run_id, quality_receipt_id=quality.id,
            )
            blocked.attempt_id = attempt_id
            blocked.market_data_quality_receipt_id = quality.id
            return blocked
        if source_matrix.status != "PASSED":
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            blocked = self._blocked(
                "MARKET_DATA_SOURCE_RECEIPT_BLOCKED",
                "正式研究数据源矩阵收据未通过："
                + ",".join(source_matrix.reason_codes),
            )
            self._append_attempt_event(
                db, attempt_id=attempt_id, sequence=1, trigger=trigger,
                phase="PRECHECK", outcome="NOT_GENERATED",
                reason_code=blocked.reason_code, reason=blocked.reason,
                requested_count=0, run_id=run_id, quality_receipt_id=quality.id,
            )
            blocked.attempt_id = attempt_id
            blocked.market_data_quality_receipt_id = quality.id
            return blocked
        started_at = now.isoformat()
        deadline_at = (now + timedelta(seconds=WORKER_DEADLINE_SECONDS)).isoformat()
        self._write_state(
            {
                "status": "RUNNING",
                "reason_code": "STARTED",
                "reason": "正式研究已进入后台执行。",
                "run_id": run_id,
                "trigger": trigger,
                "started_at": started_at,
                "heartbeat_at": started_at,
                "deadline_at": deadline_at,
                "phase": "STARTING",
                "cleanup_status": "NOT_REQUIRED",
                "attempt_id": attempt_id,
                "market_data_quality_receipt_id": quality.id,
                "market_data_quality_receipt_ids": [item.id for item in qualities],
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
            "--started-at", started_at,
            "--deadline-at", deadline_at,
            "--deadline-seconds", str(WORKER_DEADLINE_SECONDS),
            "--heartbeat-seconds", str(WORKER_HEARTBEAT_INTERVAL_SECONDS),
            "--termination-grace-seconds", str(WORKER_TERMINATION_GRACE_SECONDS),
            "--attempt-id", attempt_id,
            "--market-data-quality-receipt-id", str(quality.id),
            "--expected-market-data-manifest", json.dumps(
                [
                    {
                        "pair": target.pair,
                        "timeframe": target.timeframe,
                        "relative_path": str(target.market_path(datadir).relative_to(datadir)),
                        "sha256": receipt.file_sha256,
                        "receipt_id": receipt.id,
                    }
                    for target, receipt in zip(RESEARCH_TARGETS, qualities)
                ],
                sort_keys=True,
            ),
        ]
        self._append_attempt_event(
            db, attempt_id=attempt_id, sequence=1, trigger=trigger,
            phase="STARTED", outcome="RUNNING", reason_code="STARTED",
            reason="正式研究已进入后台执行。", requested_count=EXPECTED_CANDIDATE_COUNT,
            run_id=run_id, quality_receipt_id=quality.id,
        )
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
                "heartbeat_at": _aware(self.clock()).isoformat(),
                "deadline_at": deadline_at,
                "phase": "FINISHED",
                "cleanup_status": "NOT_REQUIRED",
            })
            return self._blocked("WORKER_START_FAILED", reason)
        lock.close()
        return FormalResearchRunRead(
            status="RUNNING",
            reason_code="STARTED",
            reason="正式研究已进入后台执行；页面轮询同一持久化状态，不会重复提交。",
            active=True,
            attempt_id=attempt_id,
            market_data_quality_receipt_id=quality.id,
            run_id=run_id,
            trigger=trigger,
            started_at=now,
            heartbeat_at=now,
            deadline_at=_parse_datetime(deadline_at),
            phase="STARTING",
            cleanup_status="NOT_REQUIRED",
        )

    def _repository_commit(self) -> str:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True
        )
        return completed.stdout.strip()


def get_formal_strategy_research_coordinator() -> FormalStrategyResearchCoordinator:
    return FormalStrategyResearchCoordinator()
