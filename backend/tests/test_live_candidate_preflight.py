import json

from app.services.live_candidate_preflight import run_live_candidate_preflight


def valid_profile_payload() -> dict:
    return {
        "name": "phase6-live-candidate-btc-15m",
        "description": "Candidate profile for governance review only.",
        "strategy": {
            "version_id": 321,
            "name": "MvpRsiStrategyOptimized",
            "file_path": "user_data/strategies/generated/MvpRsiStrategyOptimized.py",
        },
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "exchange": {
            "name": "okx",
            "market_type": "futures",
            "settlement_currency": "USDT",
        },
        "capital_limits": {
            "stake_currency": "USDT",
            "max_stake_amount": 100,
            "max_total_exposure": 300,
            "max_open_trades": 3,
        },
        "risk_limits": {
            "max_drawdown_pct": 12.5,
            "max_daily_loss_pct": 3.0,
            "max_position_pct": 20.0,
            "stop_loss_required": True,
            "emergency_stop_required": True,
        },
        "evidence": {
            "backtest": {
                "artifact_ref": "reports/backtests/phase6_candidate_backtest.json",
                "source": "artifact",
                "passed": True,
                "summary_path": "reports/backtests/phase6_candidate_backtest_summary.md",
            },
            "hyperopt": {
                "artifact_ref": "reports/hyperopt/phase6_candidate_hyperopt.json",
                "source": "artifact",
                "passed": True,
            },
            "dry_run": {
                "artifact_ref": "reports/dry_run/phase6_candidate_manifest.json",
                "source": "manifest",
                "passed": True,
            },
        },
        "entry_conditions": {
            "require_backtest_evidence": True,
            "require_hyperopt_evidence": True,
            "require_dry_run_evidence": True,
            "require_risk_limits": True,
            "require_human_approval": True,
        },
        "approval": {
            "requires_human_approval": True,
            "minimum_approvers": 2,
            "approval_scope": "live-candidate-review",
        },
        "locked_variables": {
            "profile_name": "phase6-live-candidate-btc-15m",
            "strategy_version_id": 321,
            "strategy": "MvpRsiStrategyOptimized",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "exchange": "okx",
            "market_type": "futures",
            "stake_currency": "USDT",
            "max_stake_amount": 100,
            "max_total_exposure": 300,
            "max_open_trades": 3,
            "max_drawdown_pct": 12.5,
            "max_daily_loss_pct": 3.0,
            "max_position_pct": 20.0,
            "backtest_evidence": "reports/backtests/phase6_candidate_backtest.json",
            "hyperopt_evidence": "reports/hyperopt/phase6_candidate_hyperopt.json",
            "dry_run_evidence": "reports/dry_run/phase6_candidate_manifest.json",
            "requires_human_approval": True,
            "minimum_approvers": 2,
        },
        "tags": ["phase-6", "governance-only"],
    }


def available_evidence_refs(payload: dict) -> set[str]:
    evidence = payload["evidence"]
    refs = {
        evidence["backtest"]["artifact_ref"],
        evidence["hyperopt"]["artifact_ref"],
        evidence["dry_run"]["artifact_ref"],
    }
    refs.add(evidence["backtest"]["summary_path"])
    return refs


def test_live_candidate_preflight_approves_only_for_human_review() -> None:
    payload = valid_profile_payload()
    result = run_live_candidate_preflight(payload, available_evidence_refs(payload))

    assert result.status == "APPROVED_FOR_REVIEW"
    assert result.can_enter_human_approval is True
    assert result.approval_scope == "live-candidate-review"
    assert "not live-trading or deployment authorization" in result.summary
    assert {check.name for check in result.checks} == {
        "strategy_version",
        "offline_evidence",
        "capital_limits",
        "trading_scope",
        "risk_limits",
        "abnormal_stop_conditions",
        "human_approval_boundary",
        "phase6_safety_boundary",
    }
    assert all(check.status == "PASS" for check in result.checks)


def test_live_candidate_preflight_defaults_to_blocked_without_evidence_manifest() -> None:
    result = run_live_candidate_preflight(valid_profile_payload())

    assert result.status == "BLOCKED"
    assert result.can_enter_human_approval is False
    assert "offline evidence availability manifest is missing" in result.blockers


def test_live_candidate_preflight_blocks_missing_required_evidence_ref() -> None:
    payload = valid_profile_payload()
    refs = available_evidence_refs(payload)
    refs.remove(payload["evidence"]["dry_run"]["artifact_ref"])

    result = run_live_candidate_preflight(payload, refs)

    assert result.status == "BLOCKED"
    assert "required offline evidence is missing" in result.blockers
    evidence_check = next(check for check in result.checks if check.name == "offline_evidence")
    assert evidence_check.status == "BLOCKED"
    assert payload["evidence"]["dry_run"]["artifact_ref"] in evidence_check.details["missing_refs"]


def test_live_candidate_preflight_blocks_secret_shaped_payload_without_rendering_value() -> None:
    payload = valid_profile_payload()
    payload["api_key"] = "super-secret-value-that-must-not-render"

    result = run_live_candidate_preflight(payload, available_evidence_refs(payload))
    rendered = json.dumps(result.to_audit_summary(), sort_keys=True)

    assert result.status == "BLOCKED"
    assert "secret-shaped input was rejected" in result.blockers
    assert "super-secret-value-that-must-not-render" not in rendered


def test_live_candidate_preflight_blocks_safety_boundary_conflict() -> None:
    payload = valid_profile_payload()
    payload["safety"] = {"allow_live_trading": True}

    result = run_live_candidate_preflight(payload, available_evidence_refs(payload))

    assert result.status == "BLOCKED"
    assert "Phase 6 safety boundary conflict was rejected" in result.blockers


def test_live_candidate_preflight_fails_when_risk_thresholds_exceed_review_limits() -> None:
    payload = valid_profile_payload()
    payload["risk_limits"]["max_drawdown_pct"] = 45.0
    payload["risk_limits"]["max_daily_loss_pct"] = 12.0
    payload["locked_variables"]["max_drawdown_pct"] = 45.0
    payload["locked_variables"]["max_daily_loss_pct"] = 12.0

    result = run_live_candidate_preflight(payload, available_evidence_refs(payload))

    assert result.status == "FAILED"
    assert result.can_enter_human_approval is False
    assert "max_drawdown_pct exceeds governance review threshold" in result.failures
    assert "max_daily_loss_pct exceeds governance review threshold" in result.failures
