from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional


FRONTEND_MVP_ENDPOINT_ALIASES: dict[str, str] = {
    "/strategies": "strategies",
    "/mvp/strategies": "strategies",
    "/generation-runs": "generation_runs",
    "/strategy-generation-runs": "generation_runs",
    "/mvp/generation-runs": "generation_runs",
    "/backtest-runs": "backtest_runs",
    "/mvp/backtest-runs": "backtest_runs",
    "/backtest-tasks": "backtest_tasks",
    "/mvp/backtest-tasks": "backtest_tasks",
    "/hyperopt-runs": "hyperopt_runs",
    "/mvp/hyperopt-runs": "hyperopt_runs",
    "/dry-run/management": "dry_run_management",
    "/dry-run/status": "dry_run_management",
    "/mvp/dry-run": "dry_run_management",
    "/live-candidates/governance": "live_candidate_governance",
    "/live-candidates": "live_candidate_governance",
    "/mvp/live-candidates": "live_candidate_governance",
    "/runtime/read-only": "runtime_read_only",
    "/mvp/runtime/read-only": "runtime_read_only",
    "/runtime/operator-status": "operator_status",
    "/mvp/runtime/operator-status": "operator_status",
    "/governance-events": "governance_events",
    "/audit-log/governance-events": "governance_events",
    "/mvp/governance-events": "governance_events",
    "/strategy-ranking": "ranking",
    "/mvp/ranking": "ranking",
    "/strategy-failure-reasons": "strategy_failure_reasons",
    "/mvp/strategy-failure-reasons": "strategy_failure_reasons",
    "/strategy-version-lineage": "strategy_version_lineage",
    "/strategy-versions/lineage": "strategy_version_lineage",
    "/mvp/strategy-version-lineage": "strategy_version_lineage",
}

DEBUG_SEED_SOURCE_REF = "backend-seeded-sqlite-debug"
DEBUG_SEED_TIMESTAMP = "2026-07-05T18:00:00Z"


def resolve_frontend_mvp_payload_key(path: str) -> Optional[str]:
    normalized = "/" + path.strip("/")
    return FRONTEND_MVP_ENDPOINT_ALIASES.get(normalized)


def build_debug_mvp_seed_payloads() -> dict[str, Any]:
    """Build deterministic, local-only fake payloads for frontend API debugging."""

    runtime_status = {
        "name": "runtime_readiness",
        "status": "READY",
        "summary": "Seeded backend API data is available from local SQLite.",
        "source": DEBUG_SEED_SOURCE_REF,
        "source_ref": "scripts/seed_debug_mvp_data.py",
        "last_updated": DEBUG_SEED_TIMESTAMP,
        "blocked_reason": None,
        "unavailable_reason": None,
        "stale_reason": None,
        "warnings": ["Seeded debug data is not evidence of a running trading process."],
    }
    safety = {
        "read_only": True,
        "allow_live_trading": False,
        "allow_real_orders": False,
        "allow_exchange_connection": False,
        "allow_deploy_control": False,
        "can_start_stop_bot": False,
        "boundary": (
            "Seeded debug API data is read-only local fixture data; it cannot start bots, "
            "connect to exchanges, deploy services, or place orders."
        ),
    }
    runtime_contract = {
        "schema_version": "1",
        "status": "READY",
        "generated_at": DEBUG_SEED_TIMESTAMP,
        "system_status": {
            **runtime_status,
            "name": "system_status",
            "summary": "Backend API and seeded debug database are reachable.",
        },
        "runtime_readiness": runtime_status,
        "fallback_status": {
            "active": False,
            "status": "READY",
            "reason": None,
            "sources": [DEBUG_SEED_SOURCE_REF],
        },
        "smoke_status": {
            **runtime_status,
            "name": "seeded_frontend_api_debug",
            "summary": "Frontend API debug seed has been loaded.",
        },
        "artifact_links": [
            {
                "name": "debug_seed_database",
                "path": "/tmp/freqtrade-ai-debug.sqlite",
                "source": DEBUG_SEED_SOURCE_REF,
                "status": "READY",
                "exists": True,
            }
        ],
        "blocked_reasons": [],
        "unavailable_reasons": [],
        "safety": safety,
    }
    operator_status = {
        "schema_version": "1",
        "status": "READY",
        "generated_at": DEBUG_SEED_TIMESTAMP,
        "repo_root": "/Users/shenjianpeng/Documents/Freqtrade Ai",
        "checks": [
            {
                "name": "seeded_debug_database",
                "area": "database",
                "status": "READY",
                "source": DEBUG_SEED_SOURCE_REF,
                "summary": "Local SQLite debug payload table has seeded rows.",
                "path": "/tmp/freqtrade-ai-debug.sqlite",
                "exists": True,
                "required": True,
                "blocked_reason": None,
                "unavailable_reason": None,
                "warnings": [],
            }
        ],
        "artifacts": [
            {
                "name": "debug_seed_database",
                "path": "/tmp/freqtrade-ai-debug.sqlite",
                "status": "READY",
                "source": DEBUG_SEED_SOURCE_REF,
                "exists": True,
            }
        ],
        "env_presence": [
            {"name": "DATABASE_URL", "present": True, "required": False, "source": "env", "value_rendered": False}
        ],
        "runtime_contract": {
            "status": "READY",
            "runtime_readiness_status": "READY",
            "fallback_active": False,
            "smoke_status": "READY",
            "artifact_count": 1,
            "blocked_reasons": [],
            "unavailable_reasons": [],
        },
        "blocked_reasons": [],
        "unavailable_reasons": [],
        "warnings": ["This is fake local debug data, not live runtime evidence."],
        "safety": {
            **safety,
            "reports_env_values": False,
            "boundary": (
                "Operator status is seeded read-only debug data and never reports ENV values "
                "or provides trading controls."
            ),
        },
    }
    return {
        "strategies": [
            {
                "id": "seeded-backend-rsi-001",
                "name": "SeededBackendRsi001",
                "status": "candidate",
                "timeframe": "15m",
                "source": "backend_seeded_api",
                "description": "Local SQLite seeded strategy row used to verify frontend API rendering.",
                "tags": ["seeded-api", "debug"],
                "currentVersion": {
                    "id": "seeded-version-001",
                    "versionNumber": 1,
                    "filePath": "user_data/strategies/generated/SeededBackendRsi001.py",
                    "validationStatus": "passed",
                    "validationErrors": [],
                },
            }
        ],
        "generation_runs": [
            {
                "id": "seeded-generation-run-001",
                "status": "succeeded",
                "provider": "seeded-debug-provider",
                "model": "local-sqlite-fixture",
                "requestedCount": 1,
                "generatedCount": 1,
                "acceptedCount": 1,
                "failedCount": 0,
                "errorMessage": None,
            }
        ],
        "backtest_runs": [
            {
                "id": "seeded-backtest-run-001",
                "strategy_name": "SeededBackendRsi001",
                "status": "succeeded",
                "profile_name": "seeded-local-debug",
                "requested_task_count": 1,
                "completed_task_count": 1,
                "profit_pct": 3.21,
                "max_drawdown_pct": 1.23,
                "metrics_snapshot": {
                    "normalized_metrics": {
                        "profit_total": 123.45,
                        "profit_pct": 3.21,
                        "max_drawdown_pct": 1.23,
                        "win_rate": 0.62,
                        "total_trades": 8,
                        "timerange": "20260101-20260201",
                    }
                },
                "artifact_manifest": {
                    "manifest_version": 1,
                    "status": "SUCCESS",
                    "config_path": "/tmp/freqtrade-ai-debug/config.json",
                    "strategy_name": "SeededBackendRsi001",
                    "result_path": "/tmp/freqtrade-ai-debug/backtest-result.json",
                    "manifest_path": "/tmp/freqtrade-ai-debug/backtest-artifact.json",
                    "command_args": ["freqtrade", "backtesting", "--config", "/tmp/freqtrade-ai-debug/config.json"],
                    "return_code": 0,
                    "stdout": "Seeded backend debug result, no real Freqtrade CLI was executed.",
                    "stderr": "",
                    "datadir": "user_data/data",
                    "strategy_path": "user_data/strategies/generated",
                    "userdir": "user_data",
                    "blocked_reason": None,
                    "failed_reason": None,
                },
            }
        ],
        "backtest_tasks": [
            {
                "id": "seeded-backtest-task-001",
                "run_id": "seeded-backtest-run-001",
                "strategy_name": "SeededBackendRsi001",
                "pair": "BTC/USDT:USDT",
                "timeframe": "15m",
                "status": "succeeded",
                "config_path": "/tmp/freqtrade-ai-debug/config.json",
                "result_path": "/tmp/freqtrade-ai-debug/backtest-result.json",
                "profit_pct": 3.21,
                "error_message": None,
                "metrics_snapshot": {
                    "normalized_metrics": {
                        "profit_total": 123.45,
                        "profit_pct": 3.21,
                        "max_drawdown_pct": 1.23,
                        "win_rate": 0.62,
                        "total_trades": 8,
                    }
                },
            }
        ],
        "hyperopt_runs": [
            {
                "id": "seeded-hyperopt-run-001",
                "strategy_name": "SeededBackendRsi001",
                "status": "SUCCESS",
                "profile_name": "seeded-hyperopt-debug",
                "spaces": ["buy", "sell"],
                "best_params": {"buy_rsi": 31, "sell_rsi": 71},
                "best_loss": -0.42,
                "score": 77.7,
                "epoch": 12,
                "result_path": "/tmp/freqtrade-ai-debug/hyperopt-result.json",
                "manifest_path": "/tmp/freqtrade-ai-debug/hyperopt-artifact.json",
                "blocked_reason": None,
                "failed_reason": None,
            }
        ],
        "dry_run_management": {
            "manifest": {
                "manifest_version": 1,
                "status": "SKIPPED",
                "profile_name": "seeded-debug-no-dry-run",
                "strategy_version_id": 1,
                "strategy_name": "SeededBackendRsi001",
                "pair": "BTC/USDT:USDT",
                "timeframe": "15m",
                "config_path": "/tmp/freqtrade-ai-debug/dry-run-config.json",
                "manifest_path": "/tmp/freqtrade-ai-debug/dry-run-artifact.json",
                "command_args": ["freqtrade", "trade", "--dry-run", "--config", "[debug-only]"],
                "return_code": None,
                "stdout": "",
                "stderr": "",
                "userdir": "user_data",
                "strategy_path": "user_data/strategies/generated",
                "blocked_reason": None,
                "failed_reason": None,
                "skipped_reason": "Seeded debug data does not start dry-run trading.",
            },
            "snapshot": {
                "status": "SKIPPED",
                "profile_name": "seeded-debug-no-dry-run",
                "strategy_version_id": 1,
                "strategy_name": "SeededBackendRsi001",
                "exchange": "debug-only",
                "pair": "BTC/USDT:USDT",
                "timeframe": "15m",
                "dry_run": True,
                "balance_summary": {
                    "currency": "USDT",
                    "total": 10000.0,
                    "free": 10000.0,
                    "used": 0.0,
                    "realized_profit": 0.0,
                    "unrealized_profit": 0.0,
                },
                "open_trades_summary": {
                    "total_open_trades": 0,
                    "pair_count": 1,
                    "pairs": ["BTC/USDT:USDT"],
                    "total_stake_amount": 0.0,
                    "total_profit_abs": 0.0,
                    "total_profit_pct": 0.0,
                },
                "recent_events": [
                    {
                        "timestamp": DEBUG_SEED_TIMESTAMP,
                        "event_type": "seeded_backend_api_loaded",
                        "severity": "INFO",
                        "message": "Frontend received this row from backend seeded SQLite data.",
                        "source": DEBUG_SEED_SOURCE_REF,
                    }
                ],
                "blocked_reason": None,
                "failed_reason": None,
                "skipped_reason": "No dry-run process was started.",
                "last_updated": DEBUG_SEED_TIMESTAMP,
                "artifact_manifest_path": "/tmp/freqtrade-ai-debug/dry-run-artifact.json",
            },
            "freq_ui_link": {
                "enabled": False,
                "base_url": None,
                "environment_label": "seeded-backend-api",
                "blocked_reason": "Seeded debug data does not configure an external FreqUI URL.",
                "access_mode": "read-only-link",
            },
        },
        "live_candidate_governance": {
            "source_ref": DEBUG_SEED_SOURCE_REF,
            "read_only": True,
            "safety_boundary": "Seeded governance data is fake and does not authorize live trading.",
            "profiles": [
                {
                    "id": "seeded-live-candidate-disabled",
                    "profile_name": "seeded-live-candidate-disabled",
                    "strategy_name": "SeededBackendRsi001",
                    "pair": "BTC/USDT:USDT",
                    "timeframe": "15m",
                    "status": "BLOCKED",
                    "profile_hash": "debug-seeded-profile",
                    "can_enter_human_approval": False,
                    "evidence_refs": ["scripts/seed_debug_mvp_data.py"],
                    "blockers": ["Seeded debug data cannot enter live approval."],
                    "warnings": [],
                    "risk_checks": [
                        {
                            "name": "debug_seed_boundary",
                            "status": "BLOCKED",
                            "summary": "Seeded data is display-only.",
                            "evidence_ref": "scripts/seed_debug_mvp_data.py",
                            "blocked_reason": "Debug data is not deployment evidence.",
                        }
                    ],
                    "source_ref": DEBUG_SEED_SOURCE_REF,
                    "updated_at": DEBUG_SEED_TIMESTAMP,
                }
            ],
            "approvals": [],
            "deployments": [],
            "monitoring_snapshots": [],
        },
        "runtime_read_only": deepcopy(runtime_contract),
        "operator_status": deepcopy(operator_status),
        "governance_events": [
            {
                "event_id": "seeded-api-debug-loaded",
                "event_type": "operator_dashboard_render",
                "status": "ACCEPTED",
                "actor": "codex-debug-seed",
                "source_name": "seeded-backend-api",
                "summary": "Frontend debug data was loaded from backend API.",
                "reason": None,
                "artifact_links": runtime_contract["artifact_links"],
                "created_at": DEBUG_SEED_TIMESTAMP,
            }
        ],
        "ranking": [
            {
                "rank": 1,
                "strategy_id": "seeded-backend-rsi-001",
                "strategy_name": "SeededBackendRsi001",
                "version_number": 1,
                "file_path": "user_data/strategies/generated/SeededBackendRsi001.py",
                "scoring_version": "seeded-debug-v1",
                "total_score": 88.8,
                "raw_total_score": 88.8,
                "profit_score": 81,
                "risk_score": 92,
                "stability_score": 84,
                "quality_score": 90,
                "score_breakdown": [
                    {"name": "profit_score", "score": 81, "weight": 0.35, "contribution": 28.35},
                    {"name": "risk_score", "score": 92, "weight": 0.25, "contribution": 23.0},
                    {"name": "stability_score", "score": 84, "weight": 0.15, "contribution": 12.6},
                    {"name": "quality_score", "score": 90, "weight": 0.25, "contribution": 22.5},
                ],
                "elimination": {"eliminated": False, "reasons": []},
                "warnings": [],
            }
        ],
        "strategy_failure_reasons": [
            {
                "id": "seeded-failure-info-001",
                "strategy_id": "seeded-backend-rsi-001",
                "strategy_version_id": "seeded-version-001",
                "stage": "debug_seed",
                "reason_type": "info",
                "severity": "info",
                "message": "This row proves failure reason data came from backend seeded API.",
                "details": {"source": DEBUG_SEED_SOURCE_REF},
                "created_at": DEBUG_SEED_TIMESTAMP,
            }
        ],
        "strategy_version_lineage": [
            {
                "id": "seeded-version-001",
                "strategy_id": "seeded-backend-rsi-001",
                "parent_version_id": None,
                "version_number": 1,
                "change_summary": "Initial seeded backend debug version.",
                "diff_snapshot": {"source": DEBUG_SEED_SOURCE_REF},
                "has_parent": False,
                "created_at": DEBUG_SEED_TIMESTAMP,
            }
        ],
    }
