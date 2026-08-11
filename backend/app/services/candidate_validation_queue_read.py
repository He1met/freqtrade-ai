from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ResearchJob, ResearchWorkerControl
from app.services.strategy_candidate_validation_queue import (
    CANDIDATE_VALIDATION_OPERATION,
)


class CandidateValidationQueueReadService:
    """Bounded, read-only projection of the newest persisted 60-item batch."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def read(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        recent = tuple(
            self.db.scalars(
                select(ResearchJob)
                .where(ResearchJob.operation == CANDIDATE_VALIDATION_OPERATION)
                .order_by(ResearchJob.created_at.desc(), ResearchJob.id.desc())
                .limit(120)
            ).all()
        )
        latest_run = next(
            (
                row.request_payload.get("research_run_id")
                for row in recent
                if isinstance(row.request_payload, dict)
                and isinstance(row.request_payload.get("research_run_id"), str)
            ),
            None,
        )
        jobs = tuple(
            sorted(
                (
                    row
                    for row in recent
                    if isinstance(row.request_payload, dict)
                    and row.request_payload.get("research_run_id") == latest_run
                ),
                key=lambda row: (row.created_at, row.id),
            )
        )
        if len(jobs) > 60:
            jobs = jobs[:60]
        control = self.db.get(ResearchWorkerControl, 1)
        active = tuple(row for row in jobs if row.status == "RUNNING")
        control_consistent = (
            len(active) <= 1
            and (
                not active
                or (
                    control is not None
                    and control.active_job_id == active[0].id
                    and control.active_lease_token == active[0].lease_token
                )
            )
        )
        items = [self._item(row, position=index + 1, now=now) for index, row in enumerate(jobs)]
        active_items = [item for item in items if item["status"] in {"CLAIMED", "RUNNING", "DEPLOYING"}]
        waiting = [
            item
            for item in items
            if item["status"] in {"PENDING", "QUALIFIED_PENDING_DEPLOYMENT"}
        ]
        terminal = [
            item
            for item in items
            if item["status"] in {"VALIDATED", "REJECTED", "FAILED", "DEPLOYED"}
        ]
        if not control_consistent:
            health_status, health_reason = "UNKNOWN", "SERIAL_LEASE_STATE_INCONSISTENT"
        elif active_items:
            health_status, health_reason = "HEALTHY", "ONE_SERIAL_CANDIDATE_ACTIVE"
        elif waiting:
            health_status, health_reason = "IDLE", "AWAITING_SERIAL_WORKER"
        elif jobs:
            health_status, health_reason = "IDLE", "LATEST_BATCH_TERMINAL"
        else:
            health_status, health_reason = "IDLE", "NO_PERSISTED_CANDIDATE_BATCH"
        return {
            "schema_version": "formal-candidate-validation-queue-read-v1",
            "as_of": now.isoformat(),
            "availability": "AVAILABLE",
            "serial_execution": True,
            "batch": {
                "run_id": latest_run or "none",
                "expected_count": 60,
                "generation_status": "GENERATED" if len(jobs) == 60 else "NOT_GENERATED" if not jobs else "GENERATING",
                "generated_count": len(jobs),
                "enqueued_count": len(jobs),
                "active_count": len(active_items) if control_consistent else 0,
                "waiting_count": len(waiting),
                "completed_count": len(terminal),
                "remaining_count": len(waiting) + len(active_items),
            },
            "health": {
                "status": health_status,
                "reason_code": health_reason,
                "lease_owner_present": bool(active and active[0].lease_owner),
                "lease_expires_at": _iso(active[0].lease_expires_at) if active else None,
            },
            "active_candidate": active_items[0] if control_consistent and active_items else None,
            "waiting_candidates": waiting,
            "completed_candidates": terminal,
        }

    def _item(self, job: ResearchJob, *, position: int, now: datetime) -> dict[str, Any]:
        payload = job.request_payload if isinstance(job.request_payload, dict) else {}
        evidence = job.evidence_snapshot if isinstance(job.evidence_snapshot, dict) else {}
        status = _public_status(job)
        terminal = status in {"VALIDATED", "REJECTED", "FAILED", "DEPLOYED"}
        started = _aware(job.started_at)
        completed = _aware(job.completed_at)
        end = completed or now
        elapsed = max(0, int((end - started).total_seconds())) if started else None
        references = []
        for label, value in (
            ("候选源码", payload.get("source_path")),
            ("市场数据来源", (payload.get("market_data_evidence") or {}).get("source_receipt_path") if isinstance(payload.get("market_data_evidence"), dict) else None),
        ):
            if isinstance(value, str) and value:
                references.append({"label": label, "href": None, "reference": value})
        reason = job.error_message or evidence.get("failed_reason") or evidence.get("reason_code")
        return {
            "candidate_id": f"candidate-{job.id}",
            "candidate_name": str(payload.get("candidate_key") or f"candidate-{job.id}"),
            "pair": payload.get("pair") if isinstance(payload.get("pair"), str) else None,
            "timeframe": payload.get("timeframe") if isinstance(payload.get("timeframe"), str) else None,
            "generated_at": _iso(job.created_at),
            "queue_position": None if terminal else position,
            "status": status,
            "current_step": _current_step(status),
            "completed_steps": _completed_steps(status),
            "next_step": _next_step(status),
            "progress_percent": _progress(status),
            "started_at": _iso(job.started_at),
            "completed_at": _iso(job.completed_at),
            "elapsed_seconds": elapsed,
            "preceding_count": max(0, position - 1) if not terminal else None,
            "attempt": job.attempt_count,
            "reason_code": str(reason)[:2000] if reason else None,
            "reason_message": str(reason)[:2000] if reason else None,
            "evidence": references,
            "actions": {
                "cancel_available": False,
                "retry_available": False,
                "reason_code": "READ_ONLY_OWNER_LEASE_REQUIRED",
            },
        }


def _public_status(job: ResearchJob) -> str:
    if job.status == "PENDING" and job.stage == "CANDIDATE_APPROVED":
        return "QUALIFIED_PENDING_DEPLOYMENT"
    if job.status == "RUNNING" and job.stage == "SIGNAL":
        return "DEPLOYING"
    if job.status == "RUNNING" and job.stage in {"GENERATION", "GENERATION_RETRY", "VALIDATION_RETRY"}:
        return "CLAIMED"
    if job.status == "RUNNING":
        return "RUNNING"
    if job.status == "AWAITING_APPROVAL":
        return "VALIDATED"
    if job.status == "SUCCESS" and job.stage == "DEPLOYED":
        return "DEPLOYED"
    if job.status == "BLOCKED":
        return "REJECTED"
    if job.status in {"FAILED", "STALE", "CANCELLED"}:
        return "FAILED"
    return "PENDING"


def _current_step(status: str) -> str:
    return {
        "PENDING": "已持久化，等待 lease",
        "CLAIMED": "已领取，准备独立回测",
        "RUNNING": "独立回测与验证中",
        "VALIDATED": "验证完成，等待资格决策",
        "REJECTED": "质量或安全门拒绝",
        "FAILED": "执行失败，证据已持久化",
        "QUALIFIED_PENDING_DEPLOYMENT": "QUALIFIED，等待独立 Demo 部署验收",
        "DEPLOYING": "受控发布 OKX_DEMO deployment",
        "DEPLOYED": "Demo deployment 已持久化",
    }[status]


def _completed_steps(status: str) -> list[str]:
    order = ["PENDING", "CLAIMED", "RUNNING", "VALIDATED", "QUALIFIED_PENDING_DEPLOYMENT", "DEPLOYING", "DEPLOYED"]
    if status in {"REJECTED", "FAILED"}:
        return ["候选已持久化", "串行验证已终止"]
    index = order.index(status)
    labels = ["候选已持久化", "lease 已领取", "独立回测已执行", "质量验证已持久化", "QUALIFIED 已移交", "部署验收已执行", "Demo deployment 已发布"]
    return labels[:index]


def _next_step(status: str) -> str | None:
    return {
        "PENDING": "唯一串行 worker 领取一条",
        "CLAIMED": "运行隔离 Freqtrade 回测",
        "RUNNING": "持久化 OOS、质量和失败证据",
        "VALIDATED": "应用 QUALIFIED-only 门",
        "QUALIFIED_PENDING_DEPLOYMENT": "独立 Demo 部署验收",
        "DEPLOYING": "完成幂等 deployment 发布",
        "REJECTED": None,
        "FAILED": None,
        "DEPLOYED": None,
    }[status]


def _progress(status: str) -> int:
    return {
        "PENDING": 0,
        "CLAIMED": 10,
        "RUNNING": 50,
        "VALIDATED": 75,
        "QUALIFIED_PENDING_DEPLOYMENT": 85,
        "DEPLOYING": 95,
        "DEPLOYED": 100,
        "REJECTED": 100,
        "FAILED": 100,
    }[status]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    aware = _aware(value)
    return aware.isoformat() if aware is not None else None
