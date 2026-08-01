"""Persistent, fail-closed OOS and walk-forward validation evidence.

This service never infers validation from trades inside one result.  Every
window is bound to its own BacktestRun/Task/Result and a Freqtrade v2 manifest.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.freqtrade.backtest_runner import (
    _matching_market_data_files,
    _market_data_files_digest,
)
from app.models import (
    BacktestResult,
    BacktestRun,
    BacktestTask,
    StrategyValidationPlan,
    StrategyValidationWindow,
    StrategyVersion,
)
from app.schemas.backtest import LocalBacktestTriggerRequest
from app.services.backtest_artifact_ingest import backtest_ingest_receipt
from app.services.local_backtest_trigger import LocalBacktestTriggerService


WindowKind = Literal["OOS", "WALK_FORWARD"]
REQUIRED_MARKET_STATES = {"bull", "bear", "range"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TIMERANGE_RE = re.compile(r"^(?P<start>\d{8})-(?P<end>\d{8})$")


class StrategyValidationBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class ValidationWindowSpec:
    window_kind: WindowKind
    timerange: str
    profile: dict[str, object]
    expected_market_data_digest: str
    market_state: Optional[str] = None


class StrategyValidationMatrixService:
    def __init__(
        self,
        db: Session,
        *,
        trigger_service: Optional[LocalBacktestTriggerService] = None,
    ) -> None:
        self.db = db
        self.trigger_service = trigger_service or LocalBacktestTriggerService(db)

    def declare(
        self,
        *,
        promotion_backtest_result_id: int,
        strategy_version_id: int,
        windows: list[ValidationWindowSpec],
        provider_name: str = "freqtrade",
    ) -> StrategyValidationPlan:
        """Persist the complete immutable plan before any validation run starts."""

        version = self.db.get(StrategyVersion, strategy_version_id)
        promotion_result = self.db.get(BacktestResult, promotion_backtest_result_id)
        if version is None or promotion_result is None:
            raise StrategyValidationBlocked("strategy version or promotion result is missing")
        if promotion_result.run.strategy_version_id != strategy_version_id:
            raise StrategyValidationBlocked("promotion result does not belong to strategy version")
        if provider_name != "freqtrade":
            raise StrategyValidationBlocked("validation provider must be real Freqtrade")

        normalized = self._validate_specs(
            windows,
            primary_timerange=promotion_result.timerange,
        )
        code_digest = self._strategy_code_digest(version)
        snapshot = {
            "schema_version": "strategy-validation-matrix-v1",
            "strategy_version_id": strategy_version_id,
            "promotion_backtest_result_id": promotion_backtest_result_id,
            "provider_name": provider_name,
            "strategy_code_digest": code_digest,
            "windows": [asdict(spec) for spec in normalized],
        }
        plan_digest = _stable_digest(snapshot)
        existing = self.db.scalar(
            select(StrategyValidationPlan).where(
                StrategyValidationPlan.promotion_backtest_result_id
                == promotion_backtest_result_id
            )
        )
        if existing is not None:
            if existing.plan_digest != plan_digest:
                raise StrategyValidationBlocked(
                    "validation plan is immutable once declared"
                )
            return existing

        plan = StrategyValidationPlan(
            strategy_version_id=strategy_version_id,
            promotion_backtest_result_id=promotion_backtest_result_id,
            provider_name=provider_name,
            strategy_code_digest=code_digest,
            plan_digest=plan_digest,
            plan_snapshot=snapshot,
            status="DECLARED",
        )
        self.db.add(plan)
        self.db.flush()
        for ordinal, spec in enumerate(normalized, start=1):
            self.db.add(
                StrategyValidationWindow(
                    validation_plan_id=plan.id,
                    ordinal=ordinal,
                    window_kind=spec.window_kind,
                    required_market_state=spec.market_state,
                    timerange=spec.timerange,
                    profile_snapshot=spec.profile,
                    expected_market_data_digest=spec.expected_market_data_digest,
                    status="DECLARED",
                )
            )
        self.db.commit()
        self.db.refresh(plan)
        return plan

    def prepare_runs(self, plan_id: int) -> StrategyValidationPlan:
        """Reuse the existing local trigger to create one run/task per window."""

        plan = self._require_plan(plan_id)
        if plan.status in {"PASSED", "BLOCKED"}:
            return plan
        plan.status = "RUNNING"
        self.db.commit()
        for window in plan.windows:
            if window.backtest_run_id is not None:
                continue
            deterministic_profile_name = (
                f"validation-{plan.plan_digest[:16]}-{window.ordinal}"
            )
            recovered = self._recover_prepared_run(
                plan,
                window,
                deterministic_profile_name,
            )
            if recovered:
                continue
            payload = dict(window.profile_snapshot)
            payload["timerange"] = window.timerange
            payload["profile_name"] = deterministic_profile_name
            response = self.trigger_service.trigger(
                LocalBacktestTriggerRequest(
                    strategy_version_id=plan.strategy_version_id,
                    profile=payload,
                )
            )
            if response is None or len(response.tasks) != 1:
                return self._block(plan, "local backtest trigger did not persist one run/task")
            run, task = response.run, response.tasks[0]
            window.backtest_run_id = run.id
            window.backtest_task_id = task.id
            window.status = "READY" if response.preflight_status == "ready" else "BLOCKED"
            window.blocked_reason = "; ".join(response.blocked_reasons) or None
            if task.config_path:
                config_path = Path(task.config_path)
                if config_path.is_file():
                    window.expected_config_digest = _sha256_file(config_path)
            self.db.commit()
            if window.status == "BLOCKED":
                return self._block(
                    plan,
                    f"window {window.ordinal} preflight blocked: {window.blocked_reason}",
                )
        self.db.refresh(plan)
        return plan

    def _recover_prepared_run(
        self,
        plan: StrategyValidationPlan,
        window: StrategyValidationWindow,
        profile_name: str,
    ) -> bool:
        runs = list(
            self.db.scalars(
                select(BacktestRun).where(
                    BacktestRun.strategy_version_id == plan.strategy_version_id,
                    BacktestRun.profile_name == profile_name,
                )
            ).all()
        )
        if not runs:
            return False
        if len(runs) != 1 or len(runs[0].tasks) != 1:
            self._block(
                plan,
                f"window {window.ordinal} has ambiguous recovered backtest lineage",
            )
            return True
        run, task = runs[0], runs[0].tasks[0]
        profile = (run.config_snapshot or {}).get("profile")
        if not isinstance(profile, dict) or profile.get("timerange") != window.timerange:
            self._block(
                plan,
                f"window {window.ordinal} recovered run does not match declared timerange",
            )
            return True
        window.backtest_run_id = run.id
        window.backtest_task_id = task.id
        window.status = "READY" if run.status != "blocked" else "BLOCKED"
        window.blocked_reason = task.error_message if run.status == "blocked" else None
        if task.config_path and Path(task.config_path).is_file():
            window.expected_config_digest = _sha256_file(Path(task.config_path))
        self.db.commit()
        return True

    def evaluate(self, plan_id: int) -> StrategyValidationPlan:
        """Aggregate only independent persisted, checksummed Freqtrade results."""

        plan = self._require_plan(plan_id)
        try:
            payload = self._collect_current_evidence(plan, persist=True)
        except StrategyValidationBlocked as exc:
            return self._block(plan, str(exc))
        evidence_digest = _stable_digest(payload)
        plan.status = "PASSED"
        plan.promotion_evidence = payload
        plan.evidence_digest = evidence_digest
        plan.blocked_reason = None
        plan.completed_at = datetime.now(timezone.utc)

        result = plan.promotion_result
        oos = payload["out_of_sample"]
        metrics = dict(result.metrics_snapshot or {})
        metrics["promotion_evidence"] = {
            "net_of_costs": True,
            "out_of_sample": {
                "passed": True,
                "profit_pct": oos["profit_pct"],
                "total_trades": oos["total_trades"],
            },
            "walk_forward": {
                "passed": True,
                "market_states": payload["market_states"],
            },
            "validation_matrix": {
                "plan_id": plan.id,
                "plan_digest": plan.plan_digest,
                "evidence_digest": evidence_digest,
                "window_result_ids": payload["window_result_ids"],
                "provider": plan.provider_name,
            },
        }
        result.metrics_snapshot = metrics
        self.db.commit()
        self.db.refresh(plan)
        return plan

    def assert_current_for_promotion(
        self,
        plan: StrategyValidationPlan,
    ) -> dict[str, object]:
        """Re-read every DB row and artifact even when the plan says PASSED."""

        plan_id = plan.id
        self.db.expire_all()
        plan = self._require_plan(plan_id)
        if plan.status != "PASSED":
            raise StrategyValidationBlocked(
                "promotion requires a passing persisted validation matrix"
            )
        payload = self._collect_current_evidence(plan, persist=False)
        digest = _stable_digest(payload)
        if plan.promotion_evidence != payload or plan.evidence_digest != digest:
            raise StrategyValidationBlocked(
                "persisted promotion evidence no longer matches current validation lineage"
            )
        if any(window.status != "PASSED" for window in plan.windows):
            raise StrategyValidationBlocked(
                "persisted validation windows are not all PASSED"
            )
        return payload

    def _collect_current_evidence(
        self,
        plan: StrategyValidationPlan,
        *,
        persist: bool,
    ) -> dict[str, object]:
        if _stable_digest(plan.plan_snapshot) != plan.plan_digest:
            raise StrategyValidationBlocked(
                "pre-declared validation plan checksum drift detected"
            )
        if self._strategy_code_digest(plan.strategy_version) != plan.strategy_code_digest:
            raise StrategyValidationBlocked("strategy version checksum drift detected")
        primary = plan.promotion_result
        primary_ids = {
            "run": primary.backtest_run_id,
            "task": primary.backtest_task_id,
            "result": primary.id,
        }
        blockers: list[str] = []
        seen_runs: set[int] = set()
        seen_tasks: set[int] = set()
        seen_results: set[int] = set()
        seen_execution_ids: set[str] = set()
        window_evidence: list[dict[str, object]] = []

        for window in plan.windows:
            blocker, evidence = self._validate_window(
                plan,
                window,
                seen_runs=seen_runs,
                seen_tasks=seen_tasks,
                seen_results=seen_results,
                seen_execution_ids=seen_execution_ids,
                primary_ids=primary_ids,
                primary_timerange=primary.timerange,
                persist=persist,
            )
            if blocker:
                if persist:
                    window.status = "BLOCKED"
                    window.blocked_reason = blocker
                blockers.append(f"window {window.ordinal}: {blocker}")
            else:
                if persist:
                    window.status = "PASSED"
                    window.blocked_reason = None
                window_evidence.append(evidence)

        if blockers:
            raise StrategyValidationBlocked("; ".join(blockers))

        oos = next(item for item in window_evidence if item["window_kind"] == "OOS")
        walk_forward = [
            item for item in window_evidence if item["window_kind"] == "WALK_FORWARD"
        ]
        return {
            "schema_version": "strategy-validation-matrix-v1",
            "status": "PASSED",
            "validation_plan_id": plan.id,
            "plan_digest": plan.plan_digest,
            "provider": plan.provider_name,
            "strategy_version_id": plan.strategy_version_id,
            "strategy_code_digest": plan.strategy_code_digest,
            "window_result_ids": [item["backtest_result_id"] for item in window_evidence],
            "out_of_sample": oos,
            "walk_forward": walk_forward,
            "market_states": sorted(str(item["market_state"]) for item in walk_forward),
        }

    def _validate_window(
        self,
        plan: StrategyValidationPlan,
        window: StrategyValidationWindow,
        *,
        seen_runs: set[int],
        seen_tasks: set[int],
        seen_results: set[int],
        seen_execution_ids: set[str],
        primary_ids: dict[str, int],
        primary_timerange: Optional[str],
        persist: bool,
    ) -> tuple[Optional[str], dict[str, object]]:
        if window.backtest_run_id is None or window.backtest_task_id is None:
            return "independent run/task is missing", {}
        declared_windows = plan.plan_snapshot.get("windows")
        if not isinstance(declared_windows, list) or window.ordinal > len(declared_windows):
            return "pre-declared window lineage is missing", {}
        declared = declared_windows[window.ordinal - 1]
        if not isinstance(declared, dict) or any(
            declared.get(name) != value
            for name, value in (
                ("window_kind", window.window_kind),
                ("market_state", window.required_market_state),
                ("timerange", window.timerange),
                ("profile", window.profile_snapshot),
                ("expected_market_data_digest", window.expected_market_data_digest),
            )
        ):
            return "pre-declared window checksum lineage drift detected", {}
        run = self.db.get(BacktestRun, window.backtest_run_id)
        task = self.db.get(BacktestTask, window.backtest_task_id)
        if run is None or task is None or task.backtest_run_id != run.id:
            return "persisted run/task lineage is invalid", {}
        if run.strategy_version_id != plan.strategy_version_id:
            return "run strategy version does not match plan", {}
        if run.id == primary_ids["run"] or task.id == primary_ids["task"]:
            return "validation run/task reuses the primary promotion lineage", {}
        if run.id in seen_runs or task.id in seen_tasks:
            return "run/task is reused by another validation window", {}
        seen_runs.add(run.id)
        seen_tasks.add(task.id)
        if run.status != "succeeded" or task.status != "succeeded":
            return "run/task is not succeeded", {}

        result = self.db.scalar(
            select(BacktestResult).where(
                BacktestResult.backtest_run_id == run.id,
                BacktestResult.backtest_task_id == task.id,
            )
        )
        if result is None:
            return "independent BacktestResult is missing", {}
        if result.id in seen_results:
            return "BacktestResult is reused by another validation window", {}
        seen_results.add(result.id)
        if result.id == primary_ids["result"]:
            return "validation result reuses the primary promotion result", {}
        if result.timerange != window.timerange:
            return "result timerange does not match pre-declared window", {}
        if _timeranges_overlap(primary_timerange, result.timerange):
            return "validation timerange overlaps the primary promotion timerange", {}
        if result.total_trades is None or result.total_trades < 30:
            return "validation result has fewer than 30 trades", {}
        if result.profit_pct is None or result.profit_pct <= 0:
            return "validation result is not profitable net of costs", {}

        parser_metadata = (result.metrics_snapshot or {}).get("parser_metadata")
        if not isinstance(parser_metadata, dict):
            return "result parser provenance is missing", {}
        if parser_metadata.get("ingest_source") != "local_backtest_artifact_ingest":
            return "fixture/offline/source-unknown result is forbidden", {}
        manifest = parser_metadata.get("artifact_manifest")
        if not isinstance(manifest, dict) or manifest.get("provider") != "freqtrade":
            return "real Freqtrade provider evidence is missing", {}
        if manifest.get("status") != "SUCCESS" or manifest.get("manifest_version") != 2:
            return "Freqtrade success manifest v2 is required", {}
        execution_id = manifest.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            return "execution_id is missing", {}
        if execution_id in seen_execution_ids:
            return "execution_id is reused by another validation window", {}
        seen_execution_ids.add(execution_id)

        checksums = manifest.get("checksums")
        if not isinstance(checksums, dict):
            return "artifact checksums are missing", {}
        for name in ("config", "result", "strategy", "market_data"):
            if not _valid_digest(checksums.get(name)):
                return f"{name} checksum is missing or invalid", {}
        manifest_path = manifest.get("manifest_path")
        result_path = manifest.get("result_path")
        config_path = manifest.get("config_path")
        strategy_path = plan.strategy_version.file_path
        datadir = manifest.get("datadir")
        if not all(isinstance(value, str) and value for value in (manifest_path, result_path, config_path, datadir)):
            return "artifact paths are incomplete", {}
        if manifest.get("pair") != task.pair or manifest.get("timeframe") != task.timeframe:
            return "artifact manifest pair/timeframe does not match the persisted task", {}
        market_data_files = manifest.get("market_data_files")
        if not isinstance(market_data_files, list) or not market_data_files:
            return "exact market-data file lineage is missing", {}
        actual_market_files = _market_data_lineage(
            Path(datadir), pair=task.pair, timeframe=task.timeframe
        )
        if market_data_files != actual_market_files:
            return "market-data pair/timeframe file lineage drift detected", {}
        actual_manifest_checksum = _sha256_file_or_none(Path(manifest_path))
        if actual_manifest_checksum != manifest.get("manifest_checksum"):
            return "artifact manifest checksum drift detected", {}
        expected_receipt = backtest_ingest_receipt(
            manifest_checksum=actual_manifest_checksum,
            backtest_run_id=run.id,
            backtest_task_id=task.id,
            strategy_version_id=plan.strategy_version_id,
            execution_id=execution_id,
        )
        if parser_metadata.get("ingest_receipt") != expected_receipt:
            return "session-backed artifact ingest receipt is missing or invalid", {}
        actual_manifest = _read_json_object(Path(manifest_path))
        if actual_manifest is None:
            return "artifact manifest is missing or not valid JSON", {}
        expected_manifest_lineage = {
            "manifest_version": 2,
            "status": "SUCCESS",
            "execution_scope_id": run.execution_scope_id,
            "run_id": run.id,
            "task_id": task.id,
            "strategy_version_id": plan.strategy_version_id,
            "execution_id": execution_id,
            "result_path": result_path,
            "config_path": config_path,
            "strategy_path": strategy_path,
            "datadir": datadir,
            "pair": task.pair,
            "timeframe": task.timeframe,
            "market_data_files": market_data_files,
            "checksums": checksums,
            "return_code": 0,
        }
        if any(
            actual_manifest.get(key) != value
            for key, value in expected_manifest_lineage.items()
        ):
            return "session-backed Freqtrade manifest lineage does not match", {}
        command_args = actual_manifest.get("command_args")
        if (
            not isinstance(command_args, list)
            or "backtesting" not in command_args
            or "--export" not in command_args
        ):
            return "Freqtrade backtesting command provenance is missing", {}
        actual = {
            "config": _sha256_file_or_none(Path(config_path)),
            "result": _sha256_file_or_none(Path(result_path)),
            "strategy": _sha256_file_or_none(Path(strategy_path)),
            "market_data": _market_data_files_digest(market_data_files),
        }
        for name, digest in actual.items():
            if digest != checksums.get(name):
                return f"{name} checksum drift detected", {}
        if window.expected_config_digest != checksums["config"]:
            return "config checksum does not match pre-declared run", {}
        if window.expected_market_data_digest != checksums["market_data"]:
            return "market-data checksum does not match pre-declared plan", {}
        if plan.strategy_code_digest != checksums["strategy"]:
            return "strategy code checksum does not match immutable version", {}

        regime = _classify_market_regime(
            Path(datadir),
            timerange=window.timerange,
            market_data_digest=checksums["market_data"],
            pair=task.pair,
            timeframe=task.timeframe,
            market_data_files=market_data_files,
        )
        if regime is None:
            return "market-regime classification evidence is unavailable", {}
        if (
            window.window_kind == "WALK_FORWARD"
            and regime["market_state"] != window.required_market_state
        ):
            return "computed market regime does not match the pre-declared window", {}
        if persist:
            window.backtest_result_id = result.id
            window.execution_id = execution_id
            window.artifact_manifest_checksum = actual_manifest_checksum
            window.result_checksum = checksums["result"]
            window.market_state = str(regime["market_state"])
            window.market_state_source = str(regime["source"])
            window.market_state_algorithm = str(regime["algorithm"])
            window.market_state_parameters = dict(regime["parameters"])
            window.market_state_evidence = dict(regime)
            window.market_state_evidence_digest = _stable_digest(regime)
        elif (
            window.backtest_result_id != result.id
            or window.execution_id != execution_id
            or window.artifact_manifest_checksum != actual_manifest_checksum
            or window.result_checksum != checksums["result"]
            or window.market_state != regime["market_state"]
            or window.market_state_source != regime["source"]
            or window.market_state_algorithm != regime["algorithm"]
            or window.market_state_parameters != regime["parameters"]
            or window.market_state_evidence != regime
            or window.market_state_evidence_digest != _stable_digest(regime)
        ):
            return "persisted window evidence no longer matches current artifacts", {}
        return None, {
            "window_id": window.id,
            "window_kind": window.window_kind,
            "market_state": regime["market_state"],
            "market_state_evidence_digest": _stable_digest(regime),
            "timerange": window.timerange,
            "backtest_run_id": run.id,
            "backtest_task_id": task.id,
            "backtest_result_id": result.id,
            "execution_id": execution_id,
            "artifact_manifest_checksum": actual_manifest_checksum,
            "result_checksum": checksums["result"],
            "config_digest": checksums["config"],
            "market_data_digest": checksums["market_data"],
            "profit_pct": result.profit_pct,
            "total_trades": result.total_trades,
        }

    def _validate_specs(
        self,
        windows: list[ValidationWindowSpec],
        *,
        primary_timerange: Optional[str],
    ) -> list[ValidationWindowSpec]:
        if len(windows) < 4:
            raise StrategyValidationBlocked("plan requires one OOS and at least three walk-forward windows")
        if sum(spec.window_kind == "OOS" for spec in windows) != 1:
            raise StrategyValidationBlocked("plan requires exactly one OOS window")
        walk_forward = [spec for spec in windows if spec.window_kind == "WALK_FORWARD"]
        states = {spec.market_state for spec in walk_forward}
        if len(walk_forward) < 3 or not REQUIRED_MARKET_STATES.issubset(states):
            raise StrategyValidationBlocked("walk-forward windows must cover bull, bear, and range")
        intervals: list[tuple[str, str]] = []
        for spec in windows:
            match = TIMERANGE_RE.fullmatch(spec.timerange)
            if match is None or match["start"] >= match["end"]:
                raise StrategyValidationBlocked("validation timerange is invalid")
            if not _valid_digest(spec.expected_market_data_digest):
                raise StrategyValidationBlocked("market-data digest is missing or invalid")
            if spec.profile.get("timerange") not in (None, spec.timerange):
                raise StrategyValidationBlocked("profile timerange conflicts with declared window")
            intervals.append((match["start"], match["end"]))
        ordered = sorted(intervals)
        if any(current[0] < previous[1] for previous, current in zip(ordered, ordered[1:])):
            raise StrategyValidationBlocked("validation windows overlap")
        if any(
            _timeranges_overlap(primary_timerange, spec.timerange)
            for spec in windows
        ):
            raise StrategyValidationBlocked(
                "validation windows overlap the primary promotion timerange"
            )
        return list(windows)

    def _strategy_code_digest(self, version: StrategyVersion) -> str:
        path_digest = _sha256_file_or_none(Path(version.file_path))
        declared = version.code_hash or hashlib.sha256(
            version.generated_code.encode("utf-8")
        ).hexdigest()
        if path_digest != declared:
            raise StrategyValidationBlocked("strategy file checksum does not match version")
        return declared

    def _require_plan(self, plan_id: int) -> StrategyValidationPlan:
        plan = self.db.get(StrategyValidationPlan, plan_id)
        if plan is None:
            raise StrategyValidationBlocked("validation plan not found")
        return plan

    def _block(
        self, plan: StrategyValidationPlan, reason: str
    ) -> StrategyValidationPlan:
        plan.status = "BLOCKED"
        plan.blocked_reason = reason
        plan.promotion_evidence = {}
        plan.evidence_digest = None
        plan.completed_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(plan)
        return plan


def _stable_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_file_or_none(path: Path) -> Optional[str]:
    return _sha256_file(path) if path.is_file() else None


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _market_data_lineage(
    datadir: Path,
    *,
    pair: str,
    timeframe: str,
) -> list[dict[str, str]]:
    return _matching_market_data_files(datadir, pair, timeframe)


def _market_data_digest(
    datadir: Path,
    *,
    pair: str,
    timeframe: str,
) -> Optional[str]:
    return _market_data_files_digest(
        _market_data_lineage(datadir, pair=pair, timeframe=timeframe)
    )


def _read_json_object(path: Path) -> Optional[dict[str, object]]:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _timeranges_overlap(left: Optional[str], right: Optional[str]) -> bool:
    left_match = TIMERANGE_RE.fullmatch(left or "")
    right_match = TIMERANGE_RE.fullmatch(right or "")
    if left_match is None or right_match is None:
        return True
    return (
        left_match["start"] < right_match["end"]
        and right_match["start"] < left_match["end"]
    )


def _classify_market_regime(
    datadir: Path,
    *,
    timerange: str,
    market_data_digest: str,
    pair: str,
    timeframe: str,
    market_data_files: list[dict[str, str]],
) -> Optional[dict[str, object]]:
    """Derive the regime from persisted market files, never from a caller label."""

    match = TIMERANGE_RE.fullmatch(timerange)
    if match is None:
        return None
    points: list[tuple[str, float]] = []
    source_paths: list[str] = []
    if market_data_files != _market_data_lineage(
        datadir, pair=pair, timeframe=timeframe
    ):
        return None
    for item in market_data_files:
        path = datadir / item["path"]
        loaded = _load_market_points(path)
        if loaded:
            source_paths.append(str(path.relative_to(datadir)))
            points.extend(loaded)
    selected = sorted(
        (
            timestamp,
            close,
        )
        for timestamp, close in points
        if match["start"] <= timestamp[:8] < match["end"]
    )
    if len(selected) < 2 or selected[0][1] <= 0:
        return None
    parameters = {
        "bull_threshold": 0.05,
        "bear_threshold": -0.05,
        "minimum_observations": 2,
    }
    net_return = selected[-1][1] / selected[0][1] - 1.0
    if net_return >= parameters["bull_threshold"]:
        state = "bull"
    elif net_return <= parameters["bear_threshold"]:
        state = "bear"
    else:
        state = "range"
    return {
        "source": "persisted_market_data",
        "source_artifacts": source_paths,
        "pair": pair,
        "timeframe": timeframe,
        "algorithm": "window-close-return-v1",
        "parameters": parameters,
        "market_data_digest": market_data_digest,
        "timerange": timerange,
        "observation_count": len(selected),
        "first_timestamp": selected[0][0],
        "last_timestamp": selected[-1][0],
        "first_close": selected[0][1],
        "last_close": selected[-1][1],
        "net_return": net_return,
        "market_state": state,
    }


def _load_market_points(path: Path) -> list[tuple[str, float]]:
    name = path.name.lower()
    rows: object
    try:
        if name.endswith(".json"):
            rows = json.loads(path.read_text(encoding="utf-8"))
        elif name.endswith(".json.gz"):
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                rows = json.load(handle)
        else:
            import pandas as pd  # type: ignore[import-not-found]

            if name.endswith(".feather"):
                rows = pd.read_feather(path).to_dict("records")
            elif name.endswith(".parquet"):
                rows = pd.read_parquet(path).to_dict("records")
            elif name.endswith(".csv"):
                rows = pd.read_csv(path).to_dict("records")
            else:
                return []
    except Exception:
        return []
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("candles") or rows.get("rows")
    if not isinstance(rows, list):
        return []
    points: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        timestamp = row.get("date", row.get("datetime", row.get("timestamp")))
        close = row.get("close")
        try:
            close_value = float(close)
        except (TypeError, ValueError):
            continue
        normalized = _normalize_market_timestamp(timestamp)
        if normalized is not None:
            points.append((normalized, close_value))
    return points


def _normalize_market_timestamp(value: object) -> Optional[str]:
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y%m%d%H%M%S")  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y%m%d%H%M%S")
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    digits = "".join(character for character in value if character.isdigit())
    return digits[:14].ljust(14, "0") if len(digits) >= 8 else None
