from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


LIVE_CANDIDATE_PROFILE_SCHEMA_VERSION = "1"
LIVE_CANDIDATE_PREFLIGHT_SCHEMA_VERSION = "1"
REQUIRED_LOCKED_VARIABLES = frozenset(
    {
        "profile_name",
        "strategy_version_id",
        "strategy",
        "pair",
        "timeframe",
        "exchange",
        "market_type",
        "stake_currency",
        "max_stake_amount",
        "max_total_exposure",
        "max_open_trades",
        "max_drawdown_pct",
        "max_daily_loss_pct",
        "max_position_pct",
        "backtest_evidence",
        "hyperopt_evidence",
        "dry_run_evidence",
        "requires_human_approval",
        "minimum_approvers",
    }
)
SECRET_KEY_NAMES = frozenset(
    {
        "api_key",
        "api_secret",
        "key",
        "password",
        "passphrase",
        "secret",
        "token",
    }
)
SECRET_VALUE_TOKENS = frozenset({"secret", "passphrase", "password", "token"})
FORBIDDEN_RUNTIME_KEYS = frozenset(
    {
        "api_server",
        "auto_approve",
        "bot_start",
        "bot_stop",
        "command",
        "command_options",
        "deploy",
        "deploy_command",
        "deployment_command",
        "deployment_executor",
        "executor",
        "force_entry_enable",
        "freqtrade_command",
        "live",
        "real_order",
        "real_orders",
        "restart_live",
        "runmode",
        "start_live",
        "stop_live",
        "telegram",
        "trade",
        "webhook",
    }
)


class LiveCandidateStrategy(BaseModel):
    version_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    file_path: Optional[str] = Field(default=None, min_length=1, max_length=500)

    model_config = {"extra": "forbid"}


class LiveCandidateExchange(BaseModel):
    name: str = Field(default="okx", min_length=1, max_length=80)
    market_type: Literal["spot", "futures"] = "futures"
    settlement_currency: Optional[str] = Field(default=None, min_length=1, max_length=16)

    model_config = {"extra": "forbid"}


class LiveCandidateCapitalLimits(BaseModel):
    stake_currency: str = Field(default="USDT", min_length=1, max_length=16)
    max_stake_amount: float = Field(gt=0)
    max_total_exposure: float = Field(gt=0)
    max_open_trades: int = Field(ge=1, le=100)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_exposure_limits(self) -> "LiveCandidateCapitalLimits":
        if self.max_total_exposure < self.max_stake_amount:
            raise ValueError("max_total_exposure must be greater than or equal to max_stake_amount")
        return self


class LiveCandidateRiskLimits(BaseModel):
    max_drawdown_pct: float = Field(gt=0, le=100)
    max_daily_loss_pct: float = Field(gt=0, le=100)
    max_position_pct: float = Field(gt=0, le=100)
    stop_loss_required: Literal[True] = True
    emergency_stop_required: Literal[True] = True

    model_config = {"extra": "forbid"}


class LiveCandidateEvidenceReference(BaseModel):
    artifact_ref: str = Field(min_length=1, max_length=500)
    source: Literal["artifact", "manifest", "fixture", "report"]
    passed: Literal[True] = True
    summary_path: Optional[str] = Field(default=None, min_length=1, max_length=500)

    model_config = {"extra": "forbid"}

    @field_validator("artifact_ref", "summary_path")
    @classmethod
    def validate_reference(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value

        ref = value.strip()
        if ref.startswith(("http://", "https://")):
            raise ValueError("live candidate evidence references must not be URLs")
        if ref.startswith("/"):
            raise ValueError("live candidate evidence references must be repository-relative")
        if ".." in ref.split("/"):
            raise ValueError("live candidate evidence references must not contain parent traversal")
        return ref


class LiveCandidateEvidence(BaseModel):
    backtest: LiveCandidateEvidenceReference
    hyperopt: LiveCandidateEvidenceReference
    dry_run: LiveCandidateEvidenceReference

    model_config = {"extra": "forbid"}


class LiveCandidateEntryConditions(BaseModel):
    require_backtest_evidence: Literal[True] = True
    require_hyperopt_evidence: Literal[True] = True
    require_dry_run_evidence: Literal[True] = True
    require_risk_limits: Literal[True] = True
    require_human_approval: Literal[True] = True

    model_config = {"extra": "forbid"}


class LiveCandidateApprovalRequirements(BaseModel):
    requires_human_approval: Literal[True] = True
    minimum_approvers: int = Field(default=1, ge=1, le=10)
    approval_scope: Literal["live-candidate-review"] = "live-candidate-review"

    model_config = {"extra": "forbid"}


class LiveCandidateSafety(BaseModel):
    allow_exchange_connection: Literal[False] = False
    allow_live_trading: Literal[False] = False
    allow_real_orders: Literal[False] = False
    allow_production_deployment: Literal[False] = False
    can_start_live_bot: Literal[False] = False
    governance_only: Literal[True] = True

    model_config = {"extra": "forbid"}


class LiveCandidateProfile(BaseModel):
    schema_version: Literal["1"] = LIVE_CANDIDATE_PROFILE_SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    strategy: LiveCandidateStrategy
    pair: str = Field(min_length=1, max_length=80)
    timeframe: str = Field(min_length=1, max_length=32, pattern=r"^[1-9][0-9]*[mhdw]$")
    exchange: LiveCandidateExchange = Field(default_factory=LiveCandidateExchange)
    capital_limits: LiveCandidateCapitalLimits
    risk_limits: LiveCandidateRiskLimits
    evidence: LiveCandidateEvidence
    entry_conditions: LiveCandidateEntryConditions = Field(default_factory=LiveCandidateEntryConditions)
    approval: LiveCandidateApprovalRequirements = Field(default_factory=LiveCandidateApprovalRequirements)
    safety: LiveCandidateSafety = Field(default_factory=LiveCandidateSafety)
    locked_variables: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_runtime_keys_and_secret_values(cls, value: Any) -> Any:
        cls._reject_forbidden_input(value)
        return value

    @field_validator("pair")
    @classmethod
    def validate_pair(cls, value: str) -> str:
        pair = value.strip()
        if "/" not in pair:
            raise ValueError("pair must use Freqtrade pair notation such as BTC/USDT")
        return pair

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for tag in value:
            clean = tag.strip()
            if not clean:
                raise ValueError("tags must not contain blank values")
            if clean not in seen:
                normalized.append(clean)
                seen.add(clean)
        return normalized

    @model_validator(mode="after")
    def validate_locked_variables(self) -> "LiveCandidateProfile":
        missing_locks = sorted(REQUIRED_LOCKED_VARIABLES - set(self.locked_variables))
        if missing_locks:
            raise ValueError(f"locked_variables missing required keys: {', '.join(missing_locks)}")

        expected_locks = self._locked_variable_snapshot()
        for key, expected_value in expected_locks.items():
            if self.locked_variables.get(key) != expected_value:
                raise ValueError(f"locked_variables.{key} must match profile input")

        return self

    def to_snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def to_input_snapshot(self) -> dict[str, Any]:
        snapshot = self.to_snapshot()
        snapshot["locked_variables"] = self._locked_variable_snapshot()
        snapshot["profile_hash"] = self.profile_hash()
        return snapshot

    def to_governance_snapshot(self) -> dict[str, Any]:
        return {
            "profile_name": self.name,
            "strategy": self.strategy.name,
            "strategy_version_id": self.strategy.version_id,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "exchange": self.exchange.name,
            "market_type": self.exchange.market_type,
            "stake_currency": self.capital_limits.stake_currency,
            "max_stake_amount": self.capital_limits.max_stake_amount,
            "max_total_exposure": self.capital_limits.max_total_exposure,
            "max_open_trades": self.capital_limits.max_open_trades,
            "risk_limits": {
                "max_drawdown_pct": self.risk_limits.max_drawdown_pct,
                "max_daily_loss_pct": self.risk_limits.max_daily_loss_pct,
                "max_position_pct": self.risk_limits.max_position_pct,
                "stop_loss_required": self.risk_limits.stop_loss_required,
                "emergency_stop_required": self.risk_limits.emergency_stop_required,
            },
            "evidence": {
                "backtest": self.evidence.backtest.artifact_ref,
                "hyperopt": self.evidence.hyperopt.artifact_ref,
                "dry_run": self.evidence.dry_run.artifact_ref,
            },
            "approval": {
                "requires_human_approval": self.approval.requires_human_approval,
                "minimum_approvers": self.approval.minimum_approvers,
                "approval_scope": self.approval.approval_scope,
            },
            "safety": self.safety.model_dump(mode="json"),
        }

    def profile_hash(self) -> str:
        payload = self.to_snapshot()
        payload.pop("locked_variables", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _locked_variable_snapshot(self) -> dict[str, Any]:
        return {
            "profile_name": self.name,
            "strategy_version_id": self.strategy.version_id,
            "strategy": self.strategy.name,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "exchange": self.exchange.name,
            "market_type": self.exchange.market_type,
            "stake_currency": self.capital_limits.stake_currency,
            "max_stake_amount": self.capital_limits.max_stake_amount,
            "max_total_exposure": self.capital_limits.max_total_exposure,
            "max_open_trades": self.capital_limits.max_open_trades,
            "max_drawdown_pct": self.risk_limits.max_drawdown_pct,
            "max_daily_loss_pct": self.risk_limits.max_daily_loss_pct,
            "max_position_pct": self.risk_limits.max_position_pct,
            "backtest_evidence": self.evidence.backtest.artifact_ref,
            "hyperopt_evidence": self.evidence.hyperopt.artifact_ref,
            "dry_run_evidence": self.evidence.dry_run.artifact_ref,
            "requires_human_approval": self.approval.requires_human_approval,
            "minimum_approvers": self.approval.minimum_approvers,
        }

    @classmethod
    def _reject_forbidden_input(cls, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if cls._is_secret_key(normalized):
                    raise ValueError(f"live candidate profile contains forbidden secret key: {key}")
                if normalized in FORBIDDEN_RUNTIME_KEYS:
                    raise ValueError(f"live candidate profile contains forbidden runtime key: {key}")
                cls._reject_forbidden_input(item)
            return

        if isinstance(value, list):
            for item in value:
                cls._reject_forbidden_input(item)
            return

        if isinstance(value, str) and cls._is_secret_value(value):
            raise ValueError("live candidate profile contains forbidden secret-shaped value")

    @staticmethod
    def _is_secret_key(normalized_key: str) -> bool:
        return (
            normalized_key in SECRET_KEY_NAMES
            or normalized_key.endswith("_secret")
            or "api_key" in normalized_key
            or "api_secret" in normalized_key
        )

    @staticmethod
    def _is_secret_value(value: str) -> bool:
        clean = value.strip().lower()
        normalized = clean.replace("-", "_").replace(" ", "_")
        tokens = set(re.split(r"[^a-z0-9]+", clean))
        return (
            normalized.startswith(("sk_", "ghp_", "gho_"))
            or "api_key" in normalized
            or "api_secret" in normalized
            or "apikey" in normalized
            or bool(tokens & SECRET_VALUE_TOKENS)
        )


LiveCandidateRiskCheckStatus = Literal["PASS", "BLOCKED", "FAILED"]
LiveCandidatePreflightStatus = Literal["APPROVED_FOR_REVIEW", "BLOCKED", "FAILED"]


class LiveCandidateRiskCheck(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    status: LiveCandidateRiskCheckStatus
    summary: str = Field(min_length=1, max_length=500)
    blockers: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class LiveCandidatePreflightResult(BaseModel):
    schema_version: Literal["1"] = LIVE_CANDIDATE_PREFLIGHT_SCHEMA_VERSION
    status: LiveCandidatePreflightStatus
    profile_name: Optional[str] = None
    profile_hash: Optional[str] = None
    approval_scope: Literal["live-candidate-review"] = "live-candidate-review"
    can_enter_human_approval: bool = False
    summary: str = Field(min_length=1, max_length=800)
    checks: list[LiveCandidateRiskCheck] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    safety_boundary: str = (
        "Governance preflight only; not live-trading, real-order, or deployment authorization."
    )

    model_config = {"extra": "forbid"}

    def to_audit_summary(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)
