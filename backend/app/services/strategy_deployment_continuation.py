from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.backtest import BacktestTask
from app.models.full_chain import (
    FullChainRun,
    FullChainStageRun,
    StrategyCandidateApproval,
)
from app.models.strategy import StrategyVersion
from app.repositories.research_jobs import ResearchJobRepository
from app.repositories.strategy_deployments import StrategyDeploymentRepository
from app.schemas.strategy_blueprint import StrategyBlueprint


class StrategyDeploymentContinuationBlocked(ValueError):
    """Persistent candidate evidence cannot safely produce a deployment."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_digest(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _instrument_from_pair(pair: object) -> str:
    instruments = {
        "BTC/USDT:USDT": "BTC-USDT-SWAP",
        "ETH/USDT:USDT": "ETH-USDT-SWAP",
        "SOL/USDT:USDT": "SOL-USDT-SWAP",
    }
    if pair not in instruments:
        raise StrategyDeploymentContinuationBlocked(
            "persisted backtest pair is not in the locked OKX Demo research allowlist"
        )
    return instruments[pair]


class StrategyDeploymentContinuation:
    """Publish an approved research candidate without repeating research."""

    def __init__(
        self,
        db: Session,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.db = db
        self.clock = clock

    def run(self, job_id: int, lease_token: str) -> None:
        now = _as_utc(self.clock())
        jobs = ResearchJobRepository(self.db)
        job = jobs.get(job_id)
        if (
            job is None
            or job.execution_scope_id != "LOCAL_DRY_RUN"
            or job.status != "RUNNING"
            or job.stage != "SIGNAL"
            or job.lease_token != lease_token
            or job.cancel_requested
            or job.lease_expires_at is None
            or _as_utc(job.lease_expires_at) <= now
        ):
            raise StrategyDeploymentContinuationBlocked(
                "research job lease is absent, expired, cancelled, or inconsistent"
            )

        chains = list(
            self.db.scalars(
                select(FullChainRun).where(
                    FullChainRun.research_job_id == job.id,
                    FullChainRun.run_kind == "RESEARCH",
                )
            ).all()
        )
        if len(chains) != 1:
            raise StrategyDeploymentContinuationBlocked(
                "exactly one persisted research full-chain run is required"
            )
        chain = chains[0]
        job_evidence = job.evidence_snapshot
        if (
            chain.research_scope_id != "LOCAL_DRY_RUN"
            or chain.execution_target_id != "OKX_DEMO"
            or chain.status != "APPROVED"
            or chain.current_stage != "SIGNAL"
            or chain.candidate_approval_id is None
            or chain.strategy_id is None
            or chain.strategy_version_id is None
            or chain.backtest_task_id is None
            or not isinstance(job_evidence, dict)
            or job_evidence.get("full_chain_run_id") != chain.id
            or job_evidence.get("candidate_approval_id")
            != chain.candidate_approval_id
        ):
            raise StrategyDeploymentContinuationBlocked(
                "research full-chain is not an approved OKX Demo candidate"
            )

        approval = self.db.get(
            StrategyCandidateApproval,
            chain.candidate_approval_id,
        )
        if (
            approval is None
            or approval.full_chain_run_id != chain.id
            or approval.execution_target_id != "OKX_DEMO"
            or approval.status != "APPROVED"
            or approval.strategy_version_id != chain.strategy_version_id
            or _as_utc(approval.expires_at) <= now
        ):
            raise StrategyDeploymentContinuationBlocked(
                "candidate approval is missing, expired, or inconsistent"
            )
        checkpoint = self.db.scalar(
            select(FullChainStageRun).where(
                FullChainStageRun.full_chain_run_id == chain.id,
                FullChainStageRun.stage == "CANDIDATE_APPROVAL",
            )
        )
        if (
            checkpoint is None
            or checkpoint.status != "SUCCESS"
            or checkpoint.database_ids.get("candidate_approval_id") != approval.id
            or checkpoint.output_snapshot.get("status") != "APPROVED"
        ):
            raise StrategyDeploymentContinuationBlocked(
                "candidate approval checkpoint is missing or incomplete"
            )

        profile = job.request_payload.get("backtest_profile")
        if not isinstance(profile, dict):
            raise StrategyDeploymentContinuationBlocked(
                "research job has no persisted backtest profile"
            )
        task = self.db.get(BacktestTask, chain.backtest_task_id)
        version = self.db.get(StrategyVersion, chain.strategy_version_id)
        if (
            task is None
            or version is None
            or version.strategy_id != chain.strategy_id
            or task.backtest_run_id != chain.backtest_run_id
            or profile.get("pair") != task.pair
            or profile.get("timeframe") != task.timeframe
        ):
            raise StrategyDeploymentContinuationBlocked(
                "research job, backtest task, and strategy lineage are inconsistent"
            )
        try:
            blueprint = StrategyBlueprint.model_validate(version.blueprint)
        except ValueError as exc:
            raise StrategyDeploymentContinuationBlocked(
                "persisted strategy blueprint is invalid"
            ) from exc
        if blueprint.timeframe != task.timeframe:
            raise StrategyDeploymentContinuationBlocked(
                "strategy blueprint and backtest timeframe are inconsistent"
            )
        instrument_id = _instrument_from_pair(task.pair)

        promotion = approval.promotion_evidence
        policy = promotion.get("policy") if isinstance(promotion, dict) else None
        if (
            not isinstance(policy, dict)
            or policy.get("policy_version") != approval.promotion_policy_version
        ):
            raise StrategyDeploymentContinuationBlocked(
                "candidate promotion policy evidence is missing or inconsistent"
            )
        policy_binding = {
            "schema_version": "1",
            "execution_target_id": "OKX_DEMO",
            "instrument_id": instrument_id,
            "timeframe": task.timeframe,
            "candidate_digest": approval.candidate_digest,
            "approval_policy_evidence": checkpoint.output_snapshot,
            "promotion_policy": policy,
            "promotion_policy_version": approval.promotion_policy_version,
        }
        deployment = StrategyDeploymentRepository(self.db).publish(
            candidate_approval_id=approval.id,
            instrument_id=instrument_id,
            timeframe=task.timeframe,
            deployment_policy_digest=_stable_digest(policy_binding),
            now=now,
        )

        # publish() commits intentionally.  Re-read the lease before terminal
        # completion so a fenced worker cannot release another owner's job.
        current = jobs.get(job.id)
        if (
            current is None
            or current.status != "RUNNING"
            or current.lease_token != lease_token
            or current.lease_expires_at is None
            or _as_utc(current.lease_expires_at) <= _as_utc(self.clock())
        ):
            raise StrategyDeploymentContinuationBlocked(
                "research job lease was lost after idempotent deployment publish"
            )
        links = {
            key: getattr(chain, key)
            for key in (
                "strategy_generation_run_id",
                "strategy_id",
                "strategy_version_id",
                "backtest_run_id",
                "backtest_task_id",
                "backtest_result_id",
                "strategy_score_id",
            )
        }
        completed = jobs.complete(
            job.id,
            lease_token,
            status="SUCCESS",
            stage="DEPLOYED",
            links=links,
            evidence_snapshot={
                **(job.evidence_snapshot or {}),
                "status": "SUCCESS",
                "acceptance_ready": True,
                "execution_target_id": "OKX_DEMO",
                "full_chain_run_id": chain.id,
                "candidate_approval_id": approval.id,
                "strategy_deployment_id": deployment.id,
                "deployment_policy_digest": deployment.deployment_policy_digest,
                "instrument_id": deployment.instrument_id,
                "timeframe": deployment.timeframe,
                "research_repeated": False,
                "allow_real_funds": False,
            },
            error_message=None,
            provider_completed=False,
            now=now,
        )
        if completed is None:
            raise StrategyDeploymentContinuationBlocked(
                "research job could not persist its DEPLOYED terminal checkpoint"
            )


def default_strategy_deployment_continuation_factory(
    db: Session,
) -> StrategyDeploymentContinuation:
    return StrategyDeploymentContinuation(db)
