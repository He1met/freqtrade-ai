import pytest
from pydantic import ValidationError

from app.schemas.live_candidate import LiveCandidateProfile


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
        "tags": ["phase-6", "phase-6", "governance-only"],
    }


def test_live_candidate_profile_validates_and_exports_governance_snapshot() -> None:
    profile = LiveCandidateProfile.model_validate(valid_profile_payload())

    full_snapshot = profile.to_snapshot()
    input_snapshot = profile.to_input_snapshot()
    governance_snapshot = profile.to_governance_snapshot()

    assert full_snapshot["schema_version"] == "1"
    assert full_snapshot["tags"] == ["phase-6", "governance-only"]
    assert full_snapshot["safety"] == {
        "allow_exchange_connection": False,
        "allow_live_trading": False,
        "allow_real_orders": False,
        "allow_production_deployment": False,
        "can_start_live_bot": False,
        "governance_only": True,
    }
    assert len(input_snapshot["profile_hash"]) == 64
    assert input_snapshot["locked_variables"]["requires_human_approval"] is True
    assert governance_snapshot == {
        "profile_name": "phase6-live-candidate-btc-15m",
        "strategy": "MvpRsiStrategyOptimized",
        "strategy_version_id": 321,
        "pair": "BTC/USDT:USDT",
        "timeframe": "15m",
        "exchange": "okx",
        "market_type": "futures",
        "stake_currency": "USDT",
        "max_stake_amount": 100,
        "max_total_exposure": 300,
        "max_open_trades": 3,
        "risk_limits": {
            "max_drawdown_pct": 12.5,
            "max_daily_loss_pct": 3.0,
            "max_position_pct": 20.0,
            "stop_loss_required": True,
            "emergency_stop_required": True,
        },
        "evidence": {
            "backtest": "reports/backtests/phase6_candidate_backtest.json",
            "hyperopt": "reports/hyperopt/phase6_candidate_hyperopt.json",
            "dry_run": "reports/dry_run/phase6_candidate_manifest.json",
        },
        "approval": {
            "requires_human_approval": True,
            "minimum_approvers": 2,
            "approval_scope": "live-candidate-review",
        },
        "safety": {
            "allow_exchange_connection": False,
            "allow_live_trading": False,
            "allow_real_orders": False,
            "allow_production_deployment": False,
            "can_start_live_bot": False,
            "governance_only": True,
        },
    }
    assert "command" not in governance_snapshot


@pytest.mark.parametrize(
    "missing_key",
    ["name", "strategy", "pair", "timeframe", "capital_limits", "risk_limits", "evidence"],
)
def test_live_candidate_profile_requires_core_fields(missing_key: str) -> None:
    payload = valid_profile_payload()
    payload.pop(missing_key)

    with pytest.raises(ValidationError):
        LiveCandidateProfile.model_validate(payload)


@pytest.mark.parametrize("missing_evidence", ["backtest", "hyperopt", "dry_run"])
def test_live_candidate_profile_fail_closes_when_required_evidence_is_missing(missing_evidence: str) -> None:
    payload = valid_profile_payload()
    payload["evidence"].pop(missing_evidence)

    with pytest.raises(ValidationError):
        LiveCandidateProfile.model_validate(payload)


def test_live_candidate_profile_rejects_failed_evidence() -> None:
    payload = valid_profile_payload()
    payload["evidence"]["dry_run"]["passed"] = False

    with pytest.raises(ValidationError):
        LiveCandidateProfile.model_validate(payload)


@pytest.mark.parametrize(
    "payload_update",
    [
        {"api_key": "real-key-must-not-enter-profile"},
        {"strategy": {"version_id": 321, "name": "MvpRsiStrategyOptimized", "password": "secret"}},
        {"evidence": {"backtest": {"artifact_ref": "OKX_SECRET_ENV", "source": "artifact", "passed": True}}},
    ],
)
def test_live_candidate_profile_rejects_secret_shaped_fields_and_values(payload_update: dict) -> None:
    payload = valid_profile_payload()
    payload.update(payload_update)

    with pytest.raises(ValidationError, match="forbidden secret"):
        LiveCandidateProfile.model_validate(payload)


@pytest.mark.parametrize(
    "payload_update",
    [
        {"runmode": "live"},
        {"live": True},
        {"trade": True},
        {"deploy_command": "blocked-live-control"},
        {"start_live": True},
        {"safety": {"allow_live_trading": True}},
        {"safety": {"allow_real_orders": True}},
        {"safety": {"allow_production_deployment": True}},
        {"approval": {"requires_human_approval": False}},
    ],
)
def test_live_candidate_profile_rejects_live_execution_or_auto_approval(payload_update: dict) -> None:
    payload = valid_profile_payload()
    payload.update(payload_update)

    with pytest.raises(ValidationError):
        LiveCandidateProfile.model_validate(payload)


def test_live_candidate_profile_rejects_evidence_path_escape() -> None:
    payload = valid_profile_payload()
    payload["evidence"]["backtest"]["artifact_ref"] = "../outside.json"
    payload["locked_variables"]["backtest_evidence"] = "../outside.json"

    with pytest.raises(ValidationError, match="parent traversal"):
        LiveCandidateProfile.model_validate(payload)


def test_live_candidate_profile_rejects_invalid_exposure_limits() -> None:
    payload = valid_profile_payload()
    payload["capital_limits"]["max_total_exposure"] = 50
    payload["locked_variables"]["max_total_exposure"] = 50

    with pytest.raises(ValidationError, match="max_total_exposure"):
        LiveCandidateProfile.model_validate(payload)


def test_live_candidate_profile_requires_locked_variables() -> None:
    payload = valid_profile_payload()
    payload["locked_variables"].pop("dry_run_evidence")

    with pytest.raises(ValidationError, match="locked_variables missing required keys"):
        LiveCandidateProfile.model_validate(payload)


def test_live_candidate_profile_rejects_locked_variable_drift() -> None:
    payload = valid_profile_payload()
    payload["locked_variables"]["max_open_trades"] = 1

    with pytest.raises(ValidationError, match="locked_variables.max_open_trades must match profile input"):
        LiveCandidateProfile.model_validate(payload)


def test_live_candidate_profile_hash_is_stable_and_changes_with_input() -> None:
    profile = LiveCandidateProfile.model_validate(valid_profile_payload())
    same_profile = LiveCandidateProfile.model_validate(valid_profile_payload())
    changed_payload = valid_profile_payload()
    changed_payload["risk_limits"]["max_drawdown_pct"] = 10.0
    changed_payload["locked_variables"]["max_drawdown_pct"] = 10.0
    changed_profile = LiveCandidateProfile.model_validate(changed_payload)

    assert profile.profile_hash() == same_profile.profile_hash()
    assert profile.profile_hash() != changed_profile.profile_hash()
