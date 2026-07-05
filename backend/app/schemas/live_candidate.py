from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


LIVE_CANDIDATE_PROFILE_SCHEMA_VERSION = "1"
LIVE_CANDIDATE_PREFLIGHT_SCHEMA_VERSION = "1"
LIVE_CANDIDATE_APPROVAL_SCHEMA_VERSION = "1"
LIVE_CANDIDATE_DEPLOYMENT_SCHEMA_VERSION = "1"
LIVE_CANDIDATE_MONITORING_SCHEMA_VERSION = "1"
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
LiveCandidateApprovalStatus = Literal[
    "BLOCKED_BY_PREFLIGHT",
    "PENDING_HUMAN_APPROVAL",
    "APPROVED_FOR_DEPLOYMENT_RECORD",
    "REJECTED",
    "REVOKED",
    "EXPIRED",
]
LiveCandidateApprovalDecisionType = Literal["APPROVE", "REJECT", "REVOKE", "EXPIRE"]
LiveCandidateApprovalActorRole = Literal["maintainer", "operator", "risk-owner", "reviewer"]
LiveCandidateDeploymentEnvironment = Literal["manual-review", "staging", "production-candidate"]
LiveCandidateDeploymentStatus = Literal[
    "PLANNED",
    "BLOCKED",
    "MANUAL_RESULT_RECORDED",
    "ROLLBACK_RECORDED",
    "CANCELLED",
]
LiveCandidateDeploymentResultStatus = Literal[
    "MANUAL_SUCCESS",
    "MANUAL_FAILED",
    "ROLLBACK_RECORDED",
    "CANCELLED",
]
LiveCandidateMonitoringStatus = Literal[
    "OK",
    "WARNING",
    "STALE",
    "UNAVAILABLE",
    "BLOCKED",
]
LiveCandidateMonitoringSourceType = Literal["fixture", "artifact", "controlled-local-json"]
LiveCandidateAlertSeverity = Literal["INFO", "WARNING", "ERROR", "CRITICAL"]

MONITORING_FORBIDDEN_CONTROL_KEYS = frozenset(
    {
        "control_action",
        "control_actions",
        "control_url",
        "deploy_live",
        "freqtrade_live_rest",
        "live_rest",
        "start",
        "start_bot",
        "start_live",
        "stop",
        "stop_bot",
        "stop_live",
    }
)


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


class LiveCandidateApprovalActor(BaseModel):
    actor_id: str = Field(min_length=1, max_length=120)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    role: LiveCandidateApprovalActorRole = "reviewer"

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_runtime_keys_and_secret_values(cls, value: Any) -> Any:
        LiveCandidateProfile._reject_forbidden_input(value)
        return value

    @field_validator("actor_id", "display_name")
    @classmethod
    def validate_actor_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        clean = value.strip()
        if not clean:
            raise ValueError("approval actor fields must not be blank")
        return clean


class LiveCandidateApprovalDecision(BaseModel):
    decision: LiveCandidateApprovalDecisionType
    actor: LiveCandidateApprovalActor
    decided_at: datetime
    basis: str = Field(min_length=1, max_length=1000)
    revocation_reason: Optional[str] = Field(default=None, min_length=1, max_length=1000)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_runtime_keys_and_secret_values(cls, value: Any) -> Any:
        LiveCandidateProfile._reject_forbidden_input(value)
        return value

    @field_validator("decided_at")
    @classmethod
    def validate_decided_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval decision timestamps must include timezone")
        return value

    @field_validator("basis", "revocation_reason")
    @classmethod
    def validate_decision_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        clean = value.strip()
        if not clean:
            raise ValueError("approval decision text must not be blank")
        return clean

    @model_validator(mode="after")
    def validate_revocation_reason(self) -> "LiveCandidateApprovalDecision":
        if self.decision == "REVOKE" and not self.revocation_reason:
            raise ValueError("revocation_reason is required for revoke decisions")
        if self.decision != "REVOKE" and self.revocation_reason:
            raise ValueError("revocation_reason is only allowed for revoke decisions")
        return self


class LiveCandidateApprovalRecord(BaseModel):
    schema_version: Literal["1"] = LIVE_CANDIDATE_APPROVAL_SCHEMA_VERSION
    status: LiveCandidateApprovalStatus
    profile_name: str = Field(min_length=1, max_length=120)
    profile_hash: str = Field(min_length=64, max_length=64)
    approval_scope: Literal["live-candidate-review"] = "live-candidate-review"
    risk_summary_ref: str = Field(min_length=1, max_length=500)
    preflight_status: LiveCandidatePreflightStatus
    submitted_by: LiveCandidateApprovalActor
    submitted_at: datetime
    required_approvals: int = Field(ge=1, le=10)
    decisions: list[LiveCandidateApprovalDecision] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    revocation_reason: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    safety_boundary: str = (
        "Manual governance approval record only; not live-trading, real-order, "
        "live-bot startup, or deployment execution authorization."
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_runtime_keys_and_secret_values(cls, value: Any) -> Any:
        LiveCandidateProfile._reject_forbidden_input(value)
        return value

    @field_validator("profile_hash")
    @classmethod
    def validate_profile_hash(cls, value: str) -> str:
        clean = value.strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", clean):
            raise ValueError("profile_hash must be a lowercase sha256 hex digest")
        return clean

    @field_validator("risk_summary_ref")
    @classmethod
    def validate_risk_summary_ref(cls, value: str) -> str:
        ref = value.strip()
        if ref.startswith(("http://", "https://")):
            raise ValueError("risk_summary_ref must not be a URL")
        if ref.startswith("/"):
            raise ValueError("risk_summary_ref must be repository-relative")
        if ".." in ref.split("/"):
            raise ValueError("risk_summary_ref must not contain parent traversal")
        return ref

    @field_validator("submitted_at")
    @classmethod
    def validate_submitted_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval submission timestamps must include timezone")
        return value

    @field_validator("blockers", "failures")
    @classmethod
    def validate_reason_lists(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            clean = item.strip()
            if not clean:
                raise ValueError("approval record reasons must not be blank")
            normalized.append(clean)
        return normalized

    @field_validator("revocation_reason")
    @classmethod
    def validate_record_revocation_reason(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        clean = value.strip()
        if not clean:
            raise ValueError("revocation_reason must not be blank")
        return clean

    @model_validator(mode="after")
    def validate_state_invariants(self) -> "LiveCandidateApprovalRecord":
        approval_actor_ids = {
            decision.actor.actor_id
            for decision in self.decisions
            if decision.decision == "APPROVE"
        }
        if self.status == "APPROVED_FOR_DEPLOYMENT_RECORD" and (
            self.preflight_status != "APPROVED_FOR_REVIEW"
            or len(approval_actor_ids) < self.required_approvals
        ):
            raise ValueError("deployment-record approval requires passing preflight and human approvals")
        if self.status == "BLOCKED_BY_PREFLIGHT" and self.preflight_status == "APPROVED_FOR_REVIEW":
            raise ValueError("BLOCKED_BY_PREFLIGHT requires a non-passing preflight status")
        if self.status == "REVOKED" and not self.revocation_reason:
            raise ValueError("revocation_reason is required for revoked approval records")
        if self.status != "REVOKED" and self.revocation_reason:
            raise ValueError("revocation_reason is only allowed for revoked approval records")
        return self

    @property
    def can_create_deployment_record(self) -> bool:
        return self.status == "APPROVED_FOR_DEPLOYMENT_RECORD"

    def to_audit_summary(self) -> dict[str, Any]:
        summary = self.model_dump(mode="json", exclude_none=True)
        summary["can_create_deployment_record"] = self.can_create_deployment_record
        return summary


class LiveCandidateRollbackStep(BaseModel):
    order: int = Field(ge=1, le=100)
    action: str = Field(min_length=1, max_length=500)
    expected_outcome: str = Field(min_length=1, max_length=500)
    verification_ref: Optional[str] = Field(default=None, min_length=1, max_length=500)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_runtime_keys_and_secret_values(cls, value: Any) -> Any:
        LiveCandidateProfile._reject_forbidden_input(value)
        return value

    @field_validator("action", "expected_outcome")
    @classmethod
    def validate_text(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("rollback step text must not be blank")
        return clean

    @field_validator("verification_ref")
    @classmethod
    def validate_verification_ref(cls, value: Optional[str]) -> Optional[str]:
        return _validate_optional_repo_relative_ref(value, "verification_ref")


class LiveCandidateRollbackPlan(BaseModel):
    plan_id: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1000)
    owner: LiveCandidateApprovalActor
    trigger_conditions: list[str] = Field(min_length=1)
    steps: list[LiveCandidateRollbackStep] = Field(min_length=1)
    verification_steps: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_runtime_keys_and_secret_values(cls, value: Any) -> Any:
        LiveCandidateProfile._reject_forbidden_input(value)
        return value

    @field_validator("plan_id", "summary")
    @classmethod
    def validate_plan_text(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("rollback plan text fields must not be blank")
        return clean

    @field_validator("trigger_conditions", "verification_steps")
    @classmethod
    def validate_required_text_list(cls, value: list[str]) -> list[str]:
        return _normalize_non_empty_text_list(value, "rollback plan text list")

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        refs = _normalize_non_empty_text_list(value, "rollback plan evidence refs")
        return [_validate_repo_relative_ref(ref, "evidence_refs") for ref in refs]

    @model_validator(mode="after")
    def validate_step_order(self) -> "LiveCandidateRollbackPlan":
        orders = [step.order for step in self.steps]
        if len(set(orders)) != len(orders):
            raise ValueError("rollback plan step order values must be unique")
        return self

    def to_audit_summary(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class LiveCandidateDeploymentResult(BaseModel):
    status: LiveCandidateDeploymentResultStatus
    recorded_by: LiveCandidateApprovalActor
    recorded_at: datetime
    summary: str = Field(min_length=1, max_length=1000)
    evidence_ref: Optional[str] = Field(default=None, min_length=1, max_length=500)
    source: Literal["manual-record"] = "manual-record"

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_runtime_keys_and_secret_values(cls, value: Any) -> Any:
        LiveCandidateProfile._reject_forbidden_input(value)
        return value

    @field_validator("recorded_at")
    @classmethod
    def validate_recorded_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deployment result timestamps must include timezone")
        return value

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("deployment result summary must not be blank")
        return clean

    @field_validator("evidence_ref")
    @classmethod
    def validate_evidence_ref(cls, value: Optional[str]) -> Optional[str]:
        return _validate_optional_repo_relative_ref(value, "evidence_ref")


class LiveCandidateDeploymentRecord(BaseModel):
    schema_version: Literal["1"] = LIVE_CANDIDATE_DEPLOYMENT_SCHEMA_VERSION
    record_id: str = Field(min_length=1, max_length=120)
    status: LiveCandidateDeploymentStatus
    profile_name: str = Field(min_length=1, max_length=120)
    profile_hash: str = Field(min_length=64, max_length=64)
    approval_record_ref: str = Field(min_length=1, max_length=500)
    approval_status: LiveCandidateApprovalStatus
    preflight_status: LiveCandidatePreflightStatus
    planned_environment: LiveCandidateDeploymentEnvironment
    planned_by: LiveCandidateApprovalActor
    planned_at: datetime
    rollback_plan: LiveCandidateRollbackPlan
    result: Optional[LiveCandidateDeploymentResult] = None
    blockers: list[str] = Field(default_factory=list)
    safety_boundary: str = (
        "Deployment governance record only; does not execute deployment, live trading, "
        "real orders, or live-bot control."
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_runtime_keys_and_secret_values(cls, value: Any) -> Any:
        LiveCandidateProfile._reject_forbidden_input(value)
        return value

    @field_validator("record_id", "profile_name")
    @classmethod
    def validate_identity_text(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("deployment record identity fields must not be blank")
        return clean

    @field_validator("profile_hash")
    @classmethod
    def validate_profile_hash(cls, value: str) -> str:
        clean = value.strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", clean):
            raise ValueError("profile_hash must be a lowercase sha256 hex digest")
        return clean

    @field_validator("approval_record_ref")
    @classmethod
    def validate_approval_record_ref(cls, value: str) -> str:
        return _validate_repo_relative_ref(value, "approval_record_ref")

    @field_validator("planned_at")
    @classmethod
    def validate_planned_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deployment plan timestamps must include timezone")
        return value

    @field_validator("blockers")
    @classmethod
    def validate_blockers(cls, value: list[str]) -> list[str]:
        return _normalize_non_empty_text_list(value, "deployment record blockers")

    @model_validator(mode="after")
    def validate_deployment_state(self) -> "LiveCandidateDeploymentRecord":
        if self.status != "BLOCKED" and self.approval_status != "APPROVED_FOR_DEPLOYMENT_RECORD":
            raise ValueError("deployment records require completed human approval")
        if self.approval_status == "APPROVED_FOR_DEPLOYMENT_RECORD" and self.preflight_status != "APPROVED_FOR_REVIEW":
            raise ValueError("deployment records require passing preflight")
        if self.status == "BLOCKED" and not self.blockers:
            raise ValueError("blocked deployment records must include blockers")
        if self.status == "PLANNED" and self.result is not None:
            raise ValueError("planned deployment records must not include a result")
        if self.status != "PLANNED" and self.status != "BLOCKED" and self.result is None:
            raise ValueError("result status records must include a manual result")
        if self.result is not None:
            expected_statuses = {
                "MANUAL_RESULT_RECORDED": {"MANUAL_SUCCESS", "MANUAL_FAILED"},
                "ROLLBACK_RECORDED": {"ROLLBACK_RECORDED"},
                "CANCELLED": {"CANCELLED"},
            }
            if self.result.status not in expected_statuses.get(self.status, set()):
                raise ValueError("deployment record status must match manual result status")
        return self

    @property
    def can_record_manual_result(self) -> bool:
        return self.status == "PLANNED"

    def to_audit_summary(self) -> dict[str, Any]:
        summary = self.model_dump(mode="json", exclude_none=True)
        summary["can_record_manual_result"] = self.can_record_manual_result
        return summary


class LiveCandidateMonitoringDataSource(BaseModel):
    source: LiveCandidateMonitoringSourceType
    ref: str = Field(min_length=1, max_length=1000)
    generated_at: Optional[datetime] = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_runtime_keys_and_secret_values(cls, value: Any) -> Any:
        _reject_forbidden_monitoring_input(value)
        return value

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        ref = value.strip()
        if not ref:
            raise ValueError("monitoring source ref must not be blank")
        if ref.startswith(("http://", "https://")):
            raise ValueError("monitoring source ref must not be a URL")
        if ".." in ref.split("/"):
            raise ValueError("monitoring source ref must not contain parent traversal")
        return ref

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _validate_optional_timezone(value, "monitoring source timestamps")


class LiveCandidateAlertSummary(BaseModel):
    alert_id: str = Field(min_length=1, max_length=120)
    status: LiveCandidateMonitoringStatus
    severity: LiveCandidateAlertSeverity = "INFO"
    message: str = Field(min_length=1, max_length=1000)
    source: LiveCandidateMonitoringDataSource
    last_updated: datetime
    evidence_ref: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    details: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_runtime_keys_and_secret_values(cls, value: Any) -> Any:
        _reject_forbidden_monitoring_input(value)
        return value

    @field_validator("last_updated")
    @classmethod
    def validate_last_updated(cls, value: datetime) -> datetime:
        return _validate_timezone(value, "alert summary timestamps")

    @field_validator("evidence_ref")
    @classmethod
    def validate_evidence_ref(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        ref = value.strip()
        if not ref:
            raise ValueError("alert evidence_ref must not be blank")
        if ref.startswith(("http://", "https://")):
            raise ValueError("alert evidence_ref must not be a URL")
        if ".." in ref.split("/"):
            raise ValueError("alert evidence_ref must not contain parent traversal")
        return ref


class LiveCandidateMonitoringSnapshot(BaseModel):
    schema_version: Literal["1"] = LIVE_CANDIDATE_MONITORING_SCHEMA_VERSION
    status: LiveCandidateMonitoringStatus
    source: LiveCandidateMonitoringDataSource
    last_updated: datetime
    profile_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    profile_hash: Optional[str] = Field(default=None, min_length=64, max_length=64)
    deployment_record_id: Optional[str] = Field(default=None, min_length=1, max_length=120)
    deployment_status: Optional[LiveCandidateDeploymentStatus] = None
    approval_status: Optional[LiveCandidateApprovalStatus] = None
    preflight_status: Optional[LiveCandidatePreflightStatus] = None
    pair: Optional[str] = Field(default=None, min_length=1, max_length=80)
    timeframe: Optional[str] = Field(default=None, min_length=1, max_length=32)
    alerts: list[LiveCandidateAlertSummary] = Field(default_factory=list, max_length=100)
    blockers: list[str] = Field(default_factory=list)
    unavailable_reason: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    stale_reason: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    warnings: list[str] = Field(default_factory=list)
    safety_boundary: str = (
        "Read-only live-candidate governance summary; not live-trading, real-order, "
        "live-bot control, Freqtrade live REST control, or production deployment execution."
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_runtime_keys_and_secret_values(cls, value: Any) -> Any:
        _reject_forbidden_monitoring_input(value)
        return value

    @field_validator("last_updated")
    @classmethod
    def validate_last_updated(cls, value: datetime) -> datetime:
        return _validate_timezone(value, "monitoring snapshot timestamps")

    @field_validator("profile_hash")
    @classmethod
    def validate_profile_hash(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        clean = value.strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", clean):
            raise ValueError("profile_hash must be a lowercase sha256 hex digest")
        return clean

    @field_validator("blockers", "warnings")
    @classmethod
    def validate_reason_lists(cls, value: list[str]) -> list[str]:
        return _normalize_non_empty_text_list(value, "monitoring reason list")

    @field_validator("unavailable_reason", "stale_reason")
    @classmethod
    def validate_optional_reason(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        clean = value.strip()
        if not clean:
            raise ValueError("monitoring reason must not be blank")
        return clean

    @model_validator(mode="after")
    def validate_state_reasons(self) -> "LiveCandidateMonitoringSnapshot":
        if self.status == "BLOCKED" and not self.blockers:
            raise ValueError("blocked monitoring snapshots must include blockers")
        if self.status == "UNAVAILABLE" and not self.unavailable_reason:
            raise ValueError("unavailable monitoring snapshots must include unavailable_reason")
        if self.status == "STALE" and not self.stale_reason:
            raise ValueError("stale monitoring snapshots must include stale_reason")
        if self.status == "WARNING" and not self.warnings and not _has_warning_alert(self.alerts):
            raise ValueError("warning monitoring snapshots must include warnings or warning alerts")
        return self

    def to_readonly_summary(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def _normalize_non_empty_text_list(value: list[str], field_name: str) -> list[str]:
    normalized: list[str] = []
    for item in value:
        clean = item.strip()
        if not clean:
            raise ValueError(f"{field_name} must not contain blank values")
        normalized.append(clean)
    return normalized


def _validate_optional_repo_relative_ref(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return value
    return _validate_repo_relative_ref(value, field_name)


def _validate_repo_relative_ref(value: str, field_name: str) -> str:
    ref = value.strip()
    if not ref:
        raise ValueError(f"{field_name} must not be blank")
    if ref.startswith(("http://", "https://")):
        raise ValueError(f"{field_name} must not be a URL")
    if ref.startswith("/"):
        raise ValueError(f"{field_name} must be repository-relative")
    if ".." in ref.split("/"):
        raise ValueError(f"{field_name} must not contain parent traversal")
    return ref


def _reject_forbidden_monitoring_input(value: Any) -> None:
    _reject_monitoring_control_keys(value)
    LiveCandidateProfile._reject_forbidden_input(value)


def _reject_monitoring_control_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in MONITORING_FORBIDDEN_CONTROL_KEYS:
                raise ValueError(f"live candidate monitoring contains forbidden control key: {key}")
            _reject_monitoring_control_keys(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_monitoring_control_keys(item)


def _validate_optional_timezone(value: Optional[datetime], field_name: str) -> Optional[datetime]:
    if value is None:
        return value
    return _validate_timezone(value, field_name)


def _validate_timezone(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone")
    return value


def _has_warning_alert(alerts: list[LiveCandidateAlertSummary]) -> bool:
    return any(alert.status == "WARNING" or alert.severity == "WARNING" for alert in alerts)
