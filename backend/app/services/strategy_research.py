from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from sqlalchemy.orm import Session

from app.core.strategy_research_contract import (
    MIN_FEE_PER_SIDE,
    MIN_SLIPPAGE_PER_SIDE,
    matches_official_research_policy,
)
from app.core.strategy_research_matrix import (
    ALLOWED_RESEARCH_PAIRS,
    ALLOWED_RESEARCH_TIMEFRAMES,
)
from app.core.strategy_research_diversity import (
    MAX_ABS_PNL_CORRELATION,
    MAX_SIGNAL_SIMILARITY,
    RESEARCH_CANDIDATE_COUNT,
    validate_research_diversity_contract,
)
from app.models.strategy_research import StrategyResearchBatch, StrategyResearchCandidate
from app.repositories.strategy_research import StrategyResearchRepository


EXPECTED_SCHEMA = "freqtrade-ai-strategy-candidate-research-v1"
EXPECTED_CANDIDATE_COUNT = 10
V40_EXPECTED_SCHEMA = "freqtrade-ai-strategy-candidate-research-v2"
class StrategyResearchReportError(ValueError):
    pass


def _digest_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_failure(reason: str) -> str:
    redacted = re.sub(
        r"(?i)\b(api[_-]?key|secret|password|passphrase|token)(\s*[:=]\s*)\S+",
        r"\1\2[REDACTED]",
        reason,
    )
    return redacted[:2000]


def _reason(code: str, message: str, **evidence: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "evidence": evidence}


def _validate_hard_gate_contract(report: dict[str, Any]) -> None:
    """Reject reports that weaken the project-owned qualification contract."""
    policy = report.get("selection_policy") or {}
    if not matches_official_research_policy(policy):
        raise StrategyResearchReportError(
            "research report does not use the exact project-owned quality contract"
        )

    environment = report.get("environment") or {}
    if (
        not isinstance(environment.get("fee_per_side"), (int, float))
        or environment["fee_per_side"] < MIN_FEE_PER_SIDE
        or not isinstance(environment.get("slippage_per_side"), (int, float))
        or environment["slippage_per_side"] < MIN_SLIPPAGE_PER_SIDE
    ):
        raise StrategyResearchReportError("research report weakens fee or slippage stress")
    if (
        environment.get("pairs") != list(ALLOWED_RESEARCH_PAIRS)
        or environment.get("timeframes") != list(ALLOWED_RESEARCH_TIMEFRAMES)
    ):
        raise StrategyResearchReportError("research report does not cover the official target matrix")
    market_data = environment.get("market_data")
    expected_targets = {
        (pair, timeframe)
        for pair in ALLOWED_RESEARCH_PAIRS
        for timeframe in ALLOWED_RESEARCH_TIMEFRAMES
    }
    if (
        not isinstance(market_data, list)
        or len(market_data) != len(expected_targets)
        or {
            (item.get("pair"), item.get("timeframe"))
            for item in market_data if isinstance(item, dict)
        } != expected_targets
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            for item in market_data
        )
    ):
        raise StrategyResearchReportError("research report market-data evidence is incomplete")


def _candidate_rejection_reasons(candidate: dict[str, Any], policy: dict[str, Any]) -> list[dict]:
    reasons: list[dict] = []
    blueprint_evidence = candidate.get("canonical_blueprint_v2")
    if candidate.get("unit_slot") is not None and (
        not isinstance(blueprint_evidence, dict)
        or blueprint_evidence.get("exact_render_match") is not True
        or not isinstance(blueprint_evidence.get("blueprint_digest"), str)
        or not isinstance(blueprint_evidence.get("rendered_code_digest"), str)
        or blueprint_evidence.get("rendered_code_digest") != candidate.get("sha256")
    ):
        reasons.append(
            _reason(
                "CANONICAL_BLUEPRINT_V2_MISSING",
                "候选缺少可精确重现已研究源码的 Blueprint v2 证据。",
            )
        )
    if not candidate.get("loadable"):
        reasons.append(_reason("NOT_LOADABLE", "策略无法由 Freqtrade 加载。"))
    if candidate.get("static_check") != "PASSED":
        reasons.append(_reason("STATIC_CHECK_FAILED", "静态审查未通过。"))
    lookahead = candidate.get("lookahead_analysis") or {}
    if (
        lookahead.get("status") != "PASSED"
        or lookahead.get("has_bias") is not False
    ):
        reasons.append(
            _reason("LOOKAHEAD_FAILED", "lookahead 检查缺失或发现未来函数。")
        )
    score = (candidate.get("primary_score") or {}).get("total_score")
    threshold = policy.get("min_strategy_score")
    if (
        not isinstance(score, (int, float))
        or not isinstance(threshold, (int, float))
        or score < threshold
    ):
        reasons.append(
            _reason(
                "SCORE_BELOW_THRESHOLD",
                "主窗评分未达到门槛。",
                score=score,
                threshold=threshold,
            )
        )
    minimum_trades = policy.get("min_trades_per_validation_window")
    max_drawdown = policy.get("max_drawdown_per_validation_window")
    windows = candidate.get("windows") or {}
    required_windows = {"wf_bull", "wf_range", "oos", "wf_bear"}
    for missing_window in sorted(required_windows - set(windows)):
        reasons.append(
            _reason(
                "WINDOW_MISSING",
                "缺少必需的独立验证窗口。",
                window=missing_window,
            )
        )
    for window_name, window in windows.items():
        if window_name == "primary_bear":
            continue
        if window.get("status") != "SUCCESS":
            reasons.append(
                _reason("WINDOW_FAILED", "独立验证窗口未成功。", window=window_name)
            )
            continue
        if (
            window.get("net_of_fee_and_slippage") is not True
            or not isinstance(window.get("fee_per_side"), (int, float))
            or window["fee_per_side"] < MIN_FEE_PER_SIDE
            or not isinstance(window.get("slippage_per_side"), (int, float))
            or window["slippage_per_side"] < MIN_SLIPPAGE_PER_SIDE
        ):
            reasons.append(
                _reason(
                    "COST_STRESS_MISSING",
                    "独立验证窗口缺少最低费用或滑点压力。",
                    window=window_name,
                    fee_per_side=window.get("fee_per_side"),
                    slippage_per_side=window.get("slippage_per_side"),
                )
            )
        if not isinstance(minimum_trades, int) or window.get("total_trades", 0) < minimum_trades:
            reasons.append(
                _reason(
                    "INSUFFICIENT_TRADES",
                    "独立验证窗口交易数不足。",
                    window=window_name,
                    total_trades=window.get("total_trades"),
                    minimum=minimum_trades,
                )
            )
        if (
            policy.get("validation_requires_positive_net_profit") is not True
            or window.get("profit_pct", 0) <= 0
        ):
            reasons.append(
                _reason(
                    "NON_POSITIVE_POST_COST_RETURN",
                    "独立验证窗口成本后收益不为正。",
                    window=window_name,
                    profit_pct=window.get("profit_pct"),
                )
            )
        if (
            not isinstance(max_drawdown, (int, float))
            or window.get("max_drawdown_pct", 1) > max_drawdown
        ):
            reasons.append(
                _reason(
                    "DRAWDOWN_EXCEEDED",
                    "独立验证窗口回撤超过门槛。",
                    window=window_name,
                    max_drawdown_pct=window.get("max_drawdown_pct"),
                    maximum=max_drawdown,
                )
            )
    return reasons


def _validate_candidate_target_matrix(
    candidate_name: str,
    candidate: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    declared = candidate.get("declared_timeframe")
    if declared not in ALLOWED_RESEARCH_TIMEFRAMES:
        raise StrategyResearchReportError(
            f"candidate {candidate_name!r} has an unsupported declared timeframe"
        )
    targets = candidate.get("targets")
    expected_keys = {f"{pair}|{declared}" for pair in ALLOWED_RESEARCH_PAIRS}
    if not isinstance(targets, dict) or set(targets) != expected_keys:
        raise StrategyResearchReportError(
            f"candidate {candidate_name!r} does not cover every required pair"
        )
    decisions: list[dict[str, Any]] = []
    qualified: list[dict[str, Any]] = []
    for key in sorted(targets):
        target = targets[key]
        if (
            not isinstance(target, dict)
            or target.get("timeframe") != declared
            or key != f"{target.get('pair')}|{target.get('timeframe')}"
        ):
            raise StrategyResearchReportError(
                f"candidate {candidate_name!r} has inconsistent target evidence"
            )
        reasons = _candidate_rejection_reasons(
            {
                **target,
                "loadable": candidate.get("loadable"),
                "static_check": candidate.get("static_check"),
            },
            policy,
        )
        decision = {
            "pair": target["pair"],
            "timeframe": target["timeframe"],
            "status": "QUALIFIED" if not reasons else "REJECTED",
            "rejection_reasons": reasons,
        }
        decisions.append(decision)
        if not reasons:
            qualified.append(decision)
    selected = candidate.get("deployment_target")
    selected_identity = (
        selected.get("pair"), selected.get("timeframe")
    ) if isinstance(selected, dict) else (None, None)
    if not isinstance(selected, dict) or selected_identity not in {
        (item["pair"], item["timeframe"]) for item in decisions
    }:
        raise StrategyResearchReportError(
            f"candidate {candidate_name!r} has no valid deployment target"
        )
    if qualified and selected_identity not in {
        (item["pair"], item["timeframe"]) for item in qualified
    }:
        raise StrategyResearchReportError(
            f"candidate {candidate_name!r} selects a target that failed hard gates"
        )
    selected_evidence = targets[f"{selected['pair']}|{selected['timeframe']}"]
    if any(
        candidate.get(field) != selected_evidence.get(field)
        for field in (
            "lookahead_analysis",
            "windows",
            "primary_score",
            "validation_passed",
            "score_threshold_passed",
        )
    ):
        raise StrategyResearchReportError(
            f"candidate {candidate_name!r} selected-target projection is inconsistent"
        )
    return decisions, qualified


class StrategyResearchPersistenceService:
    def __init__(self, db: Session) -> None:
        self.repository = StrategyResearchRepository(db)

    def persist_report(
        self, report_path: Path, *, run_id: str, repository_commit: str
    ) -> StrategyResearchBatch:
        content = report_path.read_bytes()
        digest = _digest_bytes(content)
        existing = self.repository.get_batch_by_run_id(run_id)
        if existing is not None:
            if existing.report_digest != digest:
                raise StrategyResearchReportError(
                    f"run_id {run_id!r} already exists with a different report digest"
                )
            return existing

        try:
            report = json.loads(content)
        except json.JSONDecodeError as exc:
            raise StrategyResearchReportError("research report is not valid JSON") from exc
        report_schema = report.get("schema_version")
        if report_schema not in {EXPECTED_SCHEMA, V40_EXPECTED_SCHEMA}:
            raise StrategyResearchReportError("unsupported research report schema")
        is_v40 = report_schema == V40_EXPECTED_SCHEMA
        safety = report.get("safety") or {}
        if safety.get("allow_real_funds") is not False or safety.get("real_orders") is not False:
            raise StrategyResearchReportError("research report is not OKX_DEMO/offline safe")
        _validate_hard_gate_contract(report)
        candidates = report.get("candidates")
        expected_count = RESEARCH_CANDIDATE_COUNT if is_v40 else EXPECTED_CANDIDATE_COUNT
        if not isinstance(candidates, dict) or len(candidates) != expected_count:
            raise StrategyResearchReportError(
                f"research report must contain exactly {expected_count} candidates"
            )
        if is_v40:
            validate_research_diversity_contract(candidates.values())
        policy = report.get("selection_policy") or {}
        qualified = set(report.get("qualified_candidates") or [])
        candidate_models: list[StrategyResearchCandidate] = []
        for candidate_name, evidence in sorted(candidates.items()):
            source_path = evidence.get("file")
            code_digest = evidence.get("sha256")
            if not isinstance(source_path, str) or not source_path:
                raise StrategyResearchReportError(
                    f"candidate {candidate_name!r} is missing its source path"
                )
            if not isinstance(code_digest, str) or len(code_digest) != 64:
                raise StrategyResearchReportError(
                    f"candidate {candidate_name!r} is missing its code digest"
                )
            if is_v40:
                direct_reasons = _candidate_rejection_reasons(evidence, policy)
                similarity = evidence.get("similarity_evidence") or {}
                correlation = evidence.get("correlation_evidence") or {}
                if similarity.get("status") != "PASSED":
                    direct_reasons.append(_reason(
                        "SIGNAL_SIMILARITY_EVIDENCE_BLOCKED",
                        "信号相似度证据不足，候选不得部署。",
                    ))
                elif similarity.get("max_signal_similarity", 1) > MAX_SIGNAL_SIMILARITY:
                    direct_reasons.append(_reason(
                        "SIGNAL_SIMILARITY_EXCEEDED",
                        "候选与同研究单元策略的信号过度相似。",
                        maximum=MAX_SIGNAL_SIMILARITY,
                        observed=similarity.get("max_signal_similarity"),
                    ))
                if correlation.get("status") != "PASSED":
                    direct_reasons.append(_reason(
                        "PNL_CORRELATION_EVIDENCE_BLOCKED",
                        "OOS 收益相关性证据不足，候选不得部署。",
                    ))
                elif correlation.get("max_abs_pnl_correlation", 1) > MAX_ABS_PNL_CORRELATION:
                    direct_reasons.append(_reason(
                        "PNL_CORRELATION_EXCEEDED",
                        "候选与同研究单元策略的 OOS 收益过度相关。",
                        maximum=MAX_ABS_PNL_CORRELATION,
                        observed=correlation.get("max_abs_pnl_correlation"),
                    ))
                evidence = {
                    **evidence,
                    "deployment_target": {
                        "pair": evidence["pair"],
                        "timeframe": evidence["timeframe"],
                    },
                }
                target_decisions = [{
                    "pair": evidence["pair"],
                    "timeframe": evidence["timeframe"],
                    "status": "QUALIFIED" if not direct_reasons else "REJECTED",
                    "rejection_reasons": direct_reasons,
                }]
                qualified_targets = [] if direct_reasons else target_decisions
            else:
                target_decisions, qualified_targets = _validate_candidate_target_matrix(
                    candidate_name, evidence, policy
                )
            evidence = {**evidence, "target_decisions": target_decisions}
            reasons = []
            if not qualified_targets:
                for decision in target_decisions:
                    for reason in decision["rejection_reasons"]:
                        reasons.append(
                            {
                                **reason,
                                "evidence": {
                                    **(reason.get("evidence") or {}),
                                    "pair": decision["pair"],
                                    "timeframe": decision["timeframe"],
                                },
                            }
                        )
            claims_qualified = (
                candidate_name in qualified
                or evidence.get("deployable_candidate") is True
            )
            actually_qualified = not reasons and claims_qualified
            if claims_qualified and reasons:
                raise StrategyResearchReportError(
                    f"candidate {candidate_name!r} claims qualification but failed hard gates"
                )
            if not reasons and not claims_qualified:
                reasons.append(
                    _reason(
                        "QUALIFICATION_NOT_ATTESTED",
                        "全部可计算质量门通过，但报告未将候选列入合格清单。",
                    )
                )
            candidate_models.append(
                StrategyResearchCandidate(
                    candidate_name=candidate_name,
                    source_path=source_path,
                    code_digest=code_digest,
                    pair=evidence.get("pair") if is_v40 else None,
                    timeframe=evidence.get("timeframe") if is_v40 else None,
                    unit_slot=evidence.get("unit_slot") if is_v40 else None,
                    strategy_family=evidence.get("strategy_family") if is_v40 else None,
                    regime_hypothesis=evidence.get("regime_hypothesis") if is_v40 else None,
                    expected_holding_period=(
                        evidence.get("expected_holding_period") if is_v40 else None
                    ),
                    expected_trade_frequency=(
                        evidence.get("expected_trade_frequency") if is_v40 else None
                    ),
                    structure_fingerprint=(
                        evidence.get("structure_fingerprint") if is_v40 else None
                    ),
                    similarity_evidence=(
                        evidence.get("similarity_evidence") if is_v40 else {}
                    ),
                    correlation_evidence=(
                        evidence.get("correlation_evidence") if is_v40 else {}
                    ),
                    status="QUALIFIED" if actually_qualified else "REJECTED",
                    loadable=evidence.get("loadable") is True,
                    static_check=str(evidence.get("static_check") or "MISSING"),
                    lookahead_status=str(
                        (evidence.get("lookahead_analysis") or {}).get("status") or "MISSING"
                    ),
                    score=(evidence.get("primary_score") or {}).get("total_score"),
                    validation_passed=evidence.get("validation_passed") is True,
                    deployable_candidate=actually_qualified,
                    rejection_reasons=reasons,
                    evidence_snapshot=evidence,
                )
            )
        qualified_count = sum(item.status == "QUALIFIED" for item in candidate_models)
        batch = StrategyResearchBatch(
            run_id=run_id,
            source_type="codex",
            repository_commit=repository_commit,
            report_schema_version=report_schema,
            report_path=str(report_path),
            report_digest=digest,
            status="VALIDATED",
            requested_count=expected_count,
            generated_count=len(candidate_models),
            persisted_count=len(candidate_models),
            qualified_count=qualified_count,
            rejected_count=len(candidate_models) - qualified_count,
            safety_snapshot=safety,
            selection_policy=policy,
            window_evidence=report.get("windows") or [],
            completed_at=datetime.now(timezone.utc),
            candidates=candidate_models,
        )
        return self.repository.add_batch(batch)

    def attach_persistence_receipt(
        self, report_path: Path, batch: StrategyResearchBatch
    ) -> None:
        """Make the durable report agree with the committed candidate batch."""
        report = json.loads(report_path.read_text(encoding="utf-8"))
        safety = dict(report.get("safety") or {})
        safety["database_used"] = True
        report["safety"] = safety
        report["persistence_receipt"] = {
            "status": "PERSISTED",
            "research_batch_id": batch.id,
            "run_id": batch.run_id,
            "generated_count": batch.generated_count,
            "persisted_count": batch.persisted_count,
            "qualified_count": batch.qualified_count,
            "rejected_count": batch.rejected_count,
            "repository_commit": batch.repository_commit,
            "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
        }
        content = json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        with tempfile.NamedTemporaryFile(
            dir=report_path.parent, prefix=f".{report_path.name}.", delete=False
        ) as handle:
            handle.write(content)
            temporary_path = Path(handle.name)
        try:
            os.replace(temporary_path, report_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        batch.report_digest = _digest_bytes(content)
        batch.safety_snapshot = safety

    def record_failed_batch(
        self,
        *,
        run_id: str,
        repository_commit: str,
        stage: str,
        failure_reason: str,
        requested_count: int = RESEARCH_CANDIDATE_COUNT,
        generated_count: int = 0,
        candidate_evidence: list[dict[str, Any]] | None = None,
        report_path: str = "",
    ) -> StrategyResearchBatch:
        safe_reason = _safe_failure(failure_reason)
        evidence_items = candidate_evidence or []
        candidate_models: list[StrategyResearchCandidate] = []
        for item in evidence_items:
            candidate_name = item.get("candidate_name")
            source_path = item.get("source_path")
            code_digest = item.get("code_digest")
            if not isinstance(candidate_name, str) or not candidate_name:
                raise StrategyResearchReportError("failed candidate is missing its name")
            if not isinstance(source_path, str) or not source_path:
                raise StrategyResearchReportError(
                    f"failed candidate {candidate_name!r} is missing its source path"
                )
            if not isinstance(code_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", code_digest):
                raise StrategyResearchReportError(
                    f"failed candidate {candidate_name!r} is missing its code digest"
                )
            snapshot = item.get("evidence_snapshot") or {}
            candidate_models.append(
                StrategyResearchCandidate(
                    candidate_name=candidate_name,
                    source_path=source_path,
                    code_digest=code_digest,
                    pair=item.get("pair"),
                    timeframe=item.get("timeframe"),
                    unit_slot=item.get("unit_slot"),
                    strategy_family=item.get("strategy_family"),
                    structure_fingerprint=item.get("structure_fingerprint"),
                    status="VALIDATION_FAILED",
                    loadable=snapshot.get("loadable") is True,
                    static_check=str(snapshot.get("static_check") or "INCOMPLETE"),
                    lookahead_status=str(
                        (snapshot.get("lookahead_analysis") or {}).get("status") or "INCOMPLETE"
                    ),
                    score=(snapshot.get("primary_score") or {}).get("total_score"),
                    validation_passed=False,
                    deployable_candidate=False,
                    rejection_reasons=[
                        _reason(
                            "VALIDATION_PIPELINE_FAILED",
                            "验证流水线未完成，候选保留且不得进入部署评审。",
                            stage=stage,
                        )
                    ],
                    evidence_snapshot=snapshot,
                )
            )
        if candidate_models:
            generated_count = len(candidate_models)
        failure_evidence = {
            "stage": stage,
            "failure_reason": safe_reason,
            "requested_count": requested_count,
            "generated_count": generated_count,
            "candidates": [
                {
                    "candidate_name": item.candidate_name,
                    "source_path": item.source_path,
                    "code_digest": item.code_digest,
                    "evidence_snapshot": item.evidence_snapshot,
                }
                for item in candidate_models
            ],
            "allow_real_funds": False,
            "real_orders": False,
        }
        failure_path = Path(report_path) if report_path else None
        digest = (
            _digest_bytes(failure_path.read_bytes())
            if failure_path is not None and failure_path.is_file()
            else _digest_bytes(json.dumps(failure_evidence, sort_keys=True).encode("utf-8"))
        )
        existing = self.repository.get_batch_by_run_id(run_id)
        if existing is not None:
            if existing.report_digest != digest:
                raise StrategyResearchReportError(
                    f"run_id {run_id!r} already exists with different failure evidence"
                )
            return existing
        return self.repository.add_batch(
            StrategyResearchBatch(
                run_id=run_id,
                source_type="codex",
                repository_commit=repository_commit,
                report_schema_version="freqtrade-ai-strategy-candidate-research-failure-v2",
                report_path=report_path,
                report_digest=digest,
                status="FAILED",
                requested_count=requested_count,
                generated_count=generated_count,
                persisted_count=len(candidate_models),
                qualified_count=0,
                rejected_count=0,
                failure_reason=safe_reason,
                safety_snapshot={
                    "execution_scope": "LOCAL_BACKTEST_ONLY",
                    "allow_real_funds": False,
                    "real_orders": False,
                    "runtime_or_writer_touched": False,
                    "failed_stage": stage,
                },
                selection_policy={},
                window_evidence=[],
                completed_at=datetime.now(timezone.utc),
                candidates=candidate_models,
            )
        )
