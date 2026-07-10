#!/usr/bin/env python3
"""Single controlled DeepSeek E2E evidence runner for Phase 9."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATH = REPO_ROOT / "backend"
VENV_PYTHON = BACKEND_PATH / ".venv" / "bin" / "python"

if (
    os.environ.get("FREQTRADE_AI_PHASE9_DEEPSEEK_E2E_REEXEC") != "1"
    and VENV_PYTHON.exists()
    and Path(sys.executable).absolute() != VENV_PYTHON
):
    os.environ["FREQTRADE_AI_PHASE9_DEEPSEEK_E2E_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.core.exceptions import ConfigurationError  # noqa: E402
from app.db.session import create_database_engine, create_session_factory, redact_database_url  # noqa: E402
from app.models import (  # noqa: E402
    BacktestResult,
    BacktestRun,
    BacktestTask,
    Strategy,
    StrategyGenerationRun,
    StrategyScore,
    StrategyVersion,
)
from app.repositories import StrategyGenerationRunRepository, StrategyRepository  # noqa: E402
from app.schemas import (  # noqa: E402
    BacktestArtifactIngestRequest,
    LocalBacktestTriggerRequest,
    StrategyGenerationRunCreate,
    StrategyGenerationRunRead,
    StrategyGenerationRunStatusUpdate,
    StrategyVersionRead,
    operation_error_evidence,
)
from app.schemas.dry_run_status import redact_secret_text  # noqa: E402
from app.services.backtest_artifact_ingest import BacktestArtifactIngestService  # noqa: E402
from app.services.local_backtest_trigger import LocalBacktestTriggerService  # noqa: E402
from app.services.local_test_db import Phase8LocalTestDbService  # noqa: E402
from app.services.strategy_generation import (  # noqa: E402
    LLMProviderConfig,
    OpenAICompatibleStrategyBlueprintProvider,
    StrategyGenerationExecutionError,
    StrategyGenerationService,
)


DEFAULT_DATABASE_URL = "sqlite+pysqlite:////tmp/freqtrade-ai-phase9-deepseek-e2e.sqlite"
DEFAULT_TMP_DIR = Path("/tmp/freqtrade-ai-phase9-deepseek-e2e")
DEFAULT_PROMPT = (
    "Generate one conservative Freqtrade StrategyBlueprint v2 for local validation. "
    "Use simple RSI/EMA indicators, one BTC/USDT pair assumption, and no secrets."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one controlled Phase 9 DeepSeek E2E validation, or fail closed "
            "with durable DB evidence when explicit authorization or prerequisites are missing."
        )
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL,
        help="Guarded local SQLAlchemy URL. Defaults to /tmp SQLite.",
    )
    parser.add_argument(
        "--environment",
        default=os.environ.get("APP_ENV", "phase9"),
        choices=["local", "dev", "test", "debug", "phase9", "phase9-local"],
        help="Safety label for destructive local DB setup.",
    )
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        default=DEFAULT_TMP_DIR,
        help="Local directory for the evidence JSON and Markdown acceptance reports.",
    )
    parser.add_argument(
        "--prompt-summary",
        default=DEFAULT_PROMPT,
        help="Prompt summary sent to DeepSeek only when --allow-real-call is present.",
    )
    parser.add_argument(
        "--allow-real-call",
        action="store_true",
        help="Authorize exactly one DeepSeek API request if DEEPSEEK_API_KEY is present.",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=None,
        help="Optional real local backtest manifest to ingest after generation/preflight succeeds.",
    )
    parser.add_argument(
        "--result-path",
        type=Path,
        default=None,
        help="Optional real local backtest result JSON to ingest after generation/preflight succeeds.",
    )
    parser.add_argument("--json", action="store_true", help="Print the evidence report as JSON.")
    return parser.parse_args()


def deepseek_config_from_env() -> LLMProviderConfig:
    return LLMProviderConfig.deepseek_from_env()


def _optional_int_from_env(name: str) -> Optional[int]:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return int(value)


def main() -> int:
    args = parse_args()
    try:
        report = run_e2e(args)
    except ConfigurationError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human_report(report)
    return 0 if report["status"] in {"READY_FOR_REVIEW", "BLOCKED", "FAILED"} else 1


def run_e2e(args: argparse.Namespace) -> dict[str, Any]:
    config = deepseek_config_from_env()
    key_present = bool(os.environ.get(config.api_key_env))
    local_db = Phase8LocalTestDbService(args.database_url, environment_label=args.environment)
    reset_summary = local_db.reset_database()

    engine = create_database_engine(args.database_url)
    session_factory = create_session_factory(engine)
    args.tmp_dir.mkdir(parents=True, exist_ok=True)

    with session_factory() as session:
        if not args.allow_real_call:
            generation_run = record_blocked_generation_run(
                session,
                config=config,
                prompt_summary=args.prompt_summary,
                reason="BLOCKED: real DeepSeek call requires --allow-real-call for this single-run E2E.",
                allow_real_call=False,
                key_present=key_present,
            )
            report = build_report(
                status="BLOCKED",
                reason="Real DeepSeek call was not authorized; no provider request was sent.",
                config=config,
                database_url=args.database_url,
                environment_label=args.environment,
                reset_summary=reset_summary,
                allow_real_call=False,
                real_call_attempted=False,
                key_present=key_present,
                generation_run=generation_run,
                session=session,
            )
            return write_report(args.tmp_dir, report)

        provider = OpenAICompatibleStrategyBlueprintProvider(config=config)
        service = StrategyGenerationService(session, provider=provider)
        try:
            result = service.run_once_with_result(args.prompt_summary, requested_count=1)
        except StrategyGenerationExecutionError as exc:
            generation_run = StrategyGenerationRunRepository(session).get(exc.run_id)
            report = build_report(
                status=provider_error_status(str(exc)),
                reason=redact_secret_text(str(exc)),
                config=config,
                database_url=args.database_url,
                environment_label=args.environment,
                reset_summary=reset_summary,
                allow_real_call=True,
                real_call_attempted=key_present,
                key_present=key_present,
                generation_run=generation_run,
                session=session,
            )
            return write_report(args.tmp_dir, report)

        version = StrategyRepository(session).get_version(result.version_ids[0])
        if version is None:
            raise RuntimeError("Strategy version disappeared after generation.")

        backtest_response = LocalBacktestTriggerService(session).trigger(
            LocalBacktestTriggerRequest(
                strategy_version_id=version.id,
                profile=default_backtest_profile(version),
            )
        )
        if backtest_response is None:
            report = build_report(
                status="FAILED",
                reason="Local backtest trigger did not return a response.",
                config=config,
                database_url=args.database_url,
                environment_label=args.environment,
                reset_summary=reset_summary,
                allow_real_call=True,
                real_call_attempted=True,
                key_present=key_present,
                generation_run=StrategyGenerationRunRepository(session).get(result.run_id),
                strategy_version=version,
                session=session,
            )
            return write_report(args.tmp_dir, report)

        if backtest_response.preflight_status == "blocked":
            report = build_report(
                status="BLOCKED",
                reason="Local backtest preflight is BLOCKED: " + "; ".join(backtest_response.blocked_reasons),
                config=config,
                database_url=args.database_url,
                environment_label=args.environment,
                reset_summary=reset_summary,
                allow_real_call=True,
                real_call_attempted=True,
                key_present=key_present,
                generation_run=StrategyGenerationRunRepository(session).get(result.run_id),
                strategy_version=version,
                backtest_response=backtest_response.model_dump(mode="json"),
                session=session,
            )
            return write_report(args.tmp_dir, report)

        task_id = backtest_response.tasks[0].id
        if args.manifest_path is None and args.result_path is None:
            report = build_report(
                status="BLOCKED",
                reason="Local backtest preflight is READY, but no real manifest_path or result_path was supplied.",
                config=config,
                database_url=args.database_url,
                environment_label=args.environment,
                reset_summary=reset_summary,
                allow_real_call=True,
                real_call_attempted=True,
                key_present=key_present,
                generation_run=StrategyGenerationRunRepository(session).get(result.run_id),
                strategy_version=version,
                backtest_response=backtest_response.model_dump(mode="json"),
                session=session,
            )
            return write_report(args.tmp_dir, report)

        ingest_response = BacktestArtifactIngestService(session).ingest_task_artifact(
            task_id,
            BacktestArtifactIngestRequest(
                manifest_path=str(args.manifest_path) if args.manifest_path is not None else None,
                result_path=str(args.result_path) if args.result_path is not None else None,
                strategy_name=version.blueprint.get("class_name") if isinstance(version.blueprint, dict) else None,
            ),
        )
        if ingest_response is None:
            report = build_report(
                status="FAILED",
                reason="Backtest artifact ingest did not return a response.",
                config=config,
                database_url=args.database_url,
                environment_label=args.environment,
                reset_summary=reset_summary,
                allow_real_call=True,
                real_call_attempted=True,
                key_present=key_present,
                generation_run=StrategyGenerationRunRepository(session).get(result.run_id),
                strategy_version=version,
                backtest_response=backtest_response.model_dump(mode="json"),
                session=session,
            )
            return write_report(args.tmp_dir, report)

        status = "READY_FOR_REVIEW" if ingest_response.ingest_status == "succeeded" else ingest_response.ingest_status.upper()
        reason = (
            "Single DeepSeek E2E produced provider, DB, file, local backtest artifact, result, and score evidence."
            if ingest_response.ingest_status == "succeeded"
            else ingest_response.reason or "Backtest artifact ingest did not succeed."
        )
        report = build_report(
            status=status,
            reason=reason,
            config=config,
            database_url=args.database_url,
            environment_label=args.environment,
            reset_summary=reset_summary,
            allow_real_call=True,
            real_call_attempted=True,
            key_present=key_present,
            generation_run=StrategyGenerationRunRepository(session).get(result.run_id),
            strategy_version=version,
            backtest_response=backtest_response.model_dump(mode="json"),
            ingest_response=ingest_response.model_dump(mode="json"),
            session=session,
        )
        return write_report(args.tmp_dir, report)


def record_blocked_generation_run(
    session: Any,
    *,
    config: LLMProviderConfig,
    prompt_summary: str,
    reason: str,
    allow_real_call: bool,
    key_present: bool,
) -> StrategyGenerationRun:
    repository = StrategyGenerationRunRepository(session)
    params_snapshot = config.metadata_snapshot()
    params_snapshot.update(
        {
            "mode": "blocked_preflight",
            "real_call_authorized": allow_real_call,
            "real_call_attempted": False,
            "credential_env_present": key_present,
            "credential_values_recorded": False,
        }
    )
    run = repository.create(
        StrategyGenerationRunCreate(
            provider=config.provider_name,
            model=config.model_name,
            prompt_summary=prompt_summary,
            params_snapshot=params_snapshot,
            requested_count=1,
        )
    )
    updated = repository.update_status(
        run.id,
        StrategyGenerationRunStatusUpdate(
            status="failed",
            failed_count=1,
            error_message=reason,
        ),
    )
    if updated is None:
        raise RuntimeError(f"Generation run disappeared after creation: {run.id}")
    return updated


def default_backtest_profile(version: StrategyVersion) -> dict[str, Any]:
    class_name = "GeneratedStrategy"
    if isinstance(version.blueprint, dict) and isinstance(version.blueprint.get("class_name"), str):
        class_name = version.blueprint["class_name"]
    data_source: dict[str, Any] = {
        "kind": "local",
        "exchange": os.environ.get("PHASE9_E2E_EXCHANGE", "okx"),
        "datadir": os.environ.get("PHASE9_E2E_MARKET_DATA_DIR", "user_data/data"),
    }
    trading_mode = os.environ.get("PHASE9_E2E_TRADING_MODE", "futures").strip()
    margin_mode = os.environ.get("PHASE9_E2E_MARGIN_MODE", "isolated").strip()
    if trading_mode:
        data_source["trading_mode"] = trading_mode
    if margin_mode:
        data_source["margin_mode"] = margin_mode

    return {
        "schema_version": "2",
        "profile_name": "phase9-deepseek-single-e2e",
        "pair": os.environ.get("PHASE9_E2E_PAIR", "BTC/USDT:USDT"),
        "timeframe": os.environ.get("PHASE9_E2E_TIMEFRAME", "15m"),
        "timerange": os.environ.get("PHASE9_E2E_TIMERANGE", "20240101-20240201"),
        "strategy": {
            "name": class_name,
            "path": version.file_path,
        },
        "data_source": data_source,
        "safety": {
            "allow_download": False,
            "allow_exchange_connection": False,
            "allow_dry_run": False,
            "allow_live_trading": False,
            "allow_hyperopt": False,
        },
        "tags": ["phase9", "deepseek-single-e2e", "local-only"],
    }


def provider_error_status(message: str) -> str:
    if "missing LLM API key environment variable" in message:
        return "BLOCKED"
    return "FAILED"


def build_report(
    *,
    status: str,
    reason: str,
    config: LLMProviderConfig,
    database_url: str,
    environment_label: str,
    reset_summary: dict[str, Any],
    allow_real_call: bool,
    real_call_attempted: bool,
    key_present: bool,
    generation_run: Optional[StrategyGenerationRun],
    session: Any,
    strategy_version: Optional[StrategyVersion] = None,
    backtest_response: Optional[dict[str, Any]] = None,
    ingest_response: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    generation_payload = (
        StrategyGenerationRunRead.model_validate(generation_run).model_dump(mode="json")
        if generation_run is not None
        else None
    )
    version_payload = (
        StrategyVersionRead.model_validate(strategy_version).model_dump(mode="json")
        if strategy_version is not None
        else None
    )
    can_accept = (
        status == "READY_FOR_REVIEW"
        and generation_payload is not None
        and generation_payload["status"] == "succeeded"
        and generation_payload["provider"] == "deepseek"
        and version_payload is not None
        and version_payload["data_source"]["core_data"] is True
        and ingest_response is not None
        and ingest_response.get("ingest_status") == "succeeded"
        and ingest_response.get("result", {}).get("data_source", {}).get("core_data") is True
        and ingest_response.get("score", {}).get("data_source", {}).get("core_data") is True
    )

    next_commands = recommended_commands()
    missing_conditions = collect_missing_conditions(
        status=status,
        allow_real_call=allow_real_call,
        key_present=key_present,
        backtest_response=backtest_response,
        ingest_response=ingest_response,
    )
    return {
        "phase": "Phase 9",
        "issue": "#334",
        "status": status,
        "can_accept_as_real_run": can_accept,
        "reason": redact_secret_text(reason),
        "database": redact_database_url(database_url),
        "environment_label": environment_label,
        "reset_batch_key": reset_summary.get("batch_key"),
        "provider": {
            "name": config.provider_name,
            "model": config.model_name,
            "base_url": config.metadata_snapshot()["base_url"],
            "endpoint_path": config.endpoint_path,
            "credential_env": config.api_key_env,
            "credential_env_present": key_present,
            "credential_values_recorded": False,
        },
        "execution": {
            "allow_real_call": allow_real_call,
            "requested_count": 1,
            "real_call_attempted": real_call_attempted,
            "live_trading": False,
            "real_orders": False,
            "production_deploy": False,
            "market_data_download": False,
            "freqtrade_trade": False,
        },
        "generation_run": generation_payload,
        "strategy_version": version_payload,
        "backtest": backtest_response,
        "artifact_ingest": ingest_response,
        "db_counts": {
            "strategy_generation_runs": session.query(StrategyGenerationRun).count(),
            "strategies": session.query(Strategy).count(),
            "strategy_versions": session.query(StrategyVersion).count(),
            "backtest_runs": session.query(BacktestRun).count(),
            "backtest_tasks": session.query(BacktestTask).count(),
            "backtest_results": session.query(BacktestResult).count(),
            "strategy_scores": session.query(StrategyScore).count(),
        },
        "ui_routes_to_verify": [
            "/generation-runs",
            "/local-strategy-lab",
            "/strategies",
            "/backtest-runs",
            "/backtest-tasks",
            "/ranking",
        ],
        "missing_conditions": missing_conditions,
        "commands": {
            "safe_default": next_commands["safe_default"],
            "authorized_real_call": next_commands["authorized_real_call"],
            "authorized_with_artifacts": next_commands["authorized_with_artifacts"],
        },
        "next_steps": next_steps_for_status(
            status=status,
            allow_real_call=allow_real_call,
            key_present=key_present,
            backtest_response=backtest_response,
            ingest_response=ingest_response,
        ),
        "required_action": required_action(status),
        "evidence": operation_error_evidence(
            status="BLOCKED" if status == "BLOCKED" else "FAILED",
            reason=redact_secret_text(reason),
            next_action=required_action(status),
            ids=(
                {"strategy_generation_run_id": generation_run.id}
                if generation_run is not None
                else {}
            ),
        ).model_dump(mode="json") if status != "READY_FOR_REVIEW" else None,
    }


def required_action(status: str) -> str:
    if status == "READY_FOR_REVIEW":
        return "Review browser/API/DB evidence and confirm the SourceMarker rows are core database/api_aggregate data."
    if status == "BLOCKED":
        return (
            "Set DEEPSEEK_API_KEY in the local environment, pass --allow-real-call, "
            "provide local market data/Freqtrade prerequisites, and supply a real backtest artifact if preflight is READY."
        )
    return "Inspect the durable failed generation/backtest records and open a Bug if the failure is unexpected."


def recommended_commands() -> dict[str, str]:
    script = "python3 scripts/phase9_deepseek_single_e2e.py"
    return {
        "safe_default": f"{script} --json",
        "authorized_real_call": f"{script} --allow-real-call --json",
        "authorized_with_artifacts": (
            f"{script} --allow-real-call "
            "--manifest-path /absolute/path/to/manifest.json "
            "--result-path /absolute/path/to/result.json "
            "--json"
        ),
    }


def collect_missing_conditions(
    *,
    status: str,
    allow_real_call: bool,
    key_present: bool,
    backtest_response: Optional[dict[str, Any]],
    ingest_response: Optional[dict[str, Any]],
) -> list[str]:
    missing: list[str] = []
    if not allow_real_call:
        missing.append("Missing explicit operator approval: rerun with --allow-real-call to authorize one DeepSeek request.")
    if allow_real_call and not key_present:
        missing.append("Missing local DEEPSEEK_API_KEY in the shell environment.")
    if backtest_response is not None and backtest_response.get("preflight_status") == "blocked":
        for reason in backtest_response.get("blocked_reasons", []):
            missing.append(redact_secret_text(f"Backtest preflight blocker: {reason}"))
    if allow_real_call and backtest_response is not None and ingest_response is None and status == "BLOCKED":
        if not any("manifest" in item.lower() for item in missing):
            missing.append("Missing real local backtest manifest/result artifacts for ingestion.")
    return missing


def next_steps_for_status(
    *,
    status: str,
    allow_real_call: bool,
    key_present: bool,
    backtest_response: Optional[dict[str, Any]],
    ingest_response: Optional[dict[str, Any]],
) -> list[str]:
    steps: list[str] = []
    if status == "READY_FOR_REVIEW":
        return [
            "Refresh the listed UI routes and confirm the persisted database IDs remain visible after reload.",
            "Cross-check the JSON and Markdown reports with the database rows before review sign-off.",
        ]
    if not allow_real_call:
        steps.append("Obtain explicit approval for one real DeepSeek request, then rerun with --allow-real-call.")
    if allow_real_call and not key_present:
        steps.append("Export DEEPSEEK_API_KEY only in the local shell before retrying.")
    if backtest_response is not None and backtest_response.get("preflight_status") == "blocked":
        steps.append("Satisfy every listed local backtest preflight blocker before treating the run as acceptance evidence.")
    if allow_real_call and backtest_response is not None and ingest_response is None and status == "BLOCKED":
        steps.append("When preflight becomes READY, supply real --manifest-path and --result-path artifacts from the local backtest.")
    if status == "FAILED":
        steps.append("Inspect the persisted failed generation/backtest record and open a Bug if the failure is not environmental.")
    return steps


def write_report(tmp_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    report_path = tmp_dir / "phase9-deepseek-single-e2e-evidence.json"
    markdown_path = tmp_dir / "phase9-deepseek-single-e2e-acceptance.md"
    safe_report = json.loads(json.dumps(report, sort_keys=True))
    safe_report["report_path"] = str(report_path)
    safe_report["report_markdown_path"] = str(markdown_path)
    report_path.write_text(json.dumps(safe_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown_report(safe_report), encoding="utf-8")
    return safe_report


def print_human_report(report: dict[str, Any]) -> None:
    print(f"Status: {report['status']}")
    print(f"Can accept as real run: {report['can_accept_as_real_run']}")
    print(f"Reason: {report['reason']}")
    print(f"Report: {report['report_path']}")


def render_markdown_report(report: dict[str, Any]) -> str:
    artifact_ingest = report.get("artifact_ingest")
    result_payload = artifact_ingest.get("result") if isinstance(artifact_ingest, dict) else None
    score_payload = artifact_ingest.get("score") if isinstance(artifact_ingest, dict) else None
    lines = [
        "# DeepSeek Single E2E Acceptance Report",
        "",
        f"- Issue: `{report['issue']}`",
        f"- Phase: `{report['phase']}`",
        f"- Verdict: `{report['status']}`",
        f"- Acceptable as real run: `{str(report['can_accept_as_real_run']).lower()}`",
        f"- Database: `{report['database']}`",
        f"- Environment: `{report['environment_label']}`",
        "",
        "## Summary",
        "",
        redact_secret_text(report["reason"]),
        "",
        "## Missing Conditions",
        "",
    ]
    missing_conditions = report.get("missing_conditions") or ["None."]
    lines.extend([f"- {item}" for item in missing_conditions])
    lines.extend(
        [
            "",
            "## Commands",
            "",
            "```bash",
            report["commands"]["safe_default"],
            report["commands"]["authorized_real_call"],
            report["commands"]["authorized_with_artifacts"],
            "```",
            "",
            "## Next Steps",
            "",
        ]
    )
    next_steps = report.get("next_steps") or ["Review the report artifacts."]
    lines.extend([f"- {item}" for item in next_steps])
    lines.extend(
        [
            "",
            "## Evidence Snapshot",
            "",
            f"- `generation_run_id`: `{nested_id(report.get('generation_run'), 'id')}`",
            f"- `strategy_id`: `{nested_id(report.get('strategy_version'), 'strategy_id')}`",
            f"- `strategy_version_id`: `{nested_id(report.get('strategy_version'), 'id')}`",
            f"- `backtest_run_id`: `{nested_id(report.get('backtest'), 'backtest_run_id')}`",
            f"- `backtest_task_id`: `{first_backtest_task_id(report.get('backtest'))}`",
            f"- `backtest_result_id`: `{nested_id(result_payload, 'id')}`",
            f"- `strategy_score_id`: `{nested_id(score_payload, 'id')}`",
            f"- `json_report_path`: `{report['report_path']}`",
            "",
            "## UI Routes To Verify",
            "",
        ]
    )
    lines.extend([f"- `{route}`" for route in report.get("ui_routes_to_verify", [])])
    lines.append("")
    lines.append("No API key values were recorded in this report.")
    lines.append("")
    return "\n".join(lines)


def nested_id(payload: Optional[dict[str, Any]], key: str) -> str:
    if not isinstance(payload, dict):
        return "n/a"
    value = payload.get(key)
    return str(value) if value is not None else "n/a"


def first_backtest_task_id(payload: Optional[dict[str, Any]]) -> str:
    if not isinstance(payload, dict):
        return "n/a"
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return "n/a"
    first = tasks[0]
    if not isinstance(first, dict):
        return "n/a"
    value = first.get("id")
    return str(value) if value is not None else "n/a"


if __name__ == "__main__":
    raise SystemExit(main())
