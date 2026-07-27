from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


EXECUTION_TARGET_SCHEMA_VERSION = "1"
ONLY_EXCHANGE_EXECUTION_TARGET_ID = "OKX_DEMO"
LOCAL_DRY_RUN_SCOPE_ID = "LOCAL_DRY_RUN"


class ExecutionTargetConfigurationError(RuntimeError):
    """Raised when execution routing cannot be resolved without ambiguity."""


class ExecutionTargetDefinition(BaseModel):
    target_id: str = Field(min_length=1, max_length=64)
    status: Literal["ACTIVE", "DISABLED"]
    exchange: str = Field(min_length=1, max_length=64)
    product_type: str = Field(min_length=1, max_length=64)
    margin_mode: str = Field(min_length=1, max_length=64)
    position_mode: str = Field(min_length=1, max_length=64)
    account_mode: str = Field(min_length=1, max_length=64)
    simulated_trading: bool
    credential_source: str = Field(min_length=1, max_length=64)
    write_policy: str = Field(min_length=1, max_length=64)
    order_submission_enabled: bool
    allow_real_funds: bool

    model_config = {"extra": "forbid"}


class NonExchangeExecutionScope(BaseModel):
    scope_id: str = Field(min_length=1, max_length=64)
    scope_type: str = Field(min_length=1, max_length=64)
    exchange_order_execution: Literal[False] = False
    write_policy: Literal["NO_EXCHANGE_WRITES"] = "NO_EXCHANGE_WRITES"

    model_config = {"extra": "forbid"}


class ExecutionTargetManifest(BaseModel):
    schema_version: Literal["1"] = EXECUTION_TARGET_SCHEMA_VERSION
    implicit_fallback: Literal[False]
    targets: list[ExecutionTargetDefinition] = Field(min_length=1, max_length=20)
    non_exchange_scopes: list[NonExchangeExecutionScope] = Field(min_length=1, max_length=1)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def enforce_single_okx_demo_target(self) -> "ExecutionTargetManifest":
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("duplicate execution target IDs are forbidden")

        active_targets = [target for target in self.targets if target.status == "ACTIVE"]
        if len(active_targets) != 1:
            raise ValueError("exactly one ACTIVE execution target is required")
        if target_ids != [ONLY_EXCHANGE_EXECUTION_TARGET_ID]:
            raise ValueError(
                "OKX_DEMO must be the only configured execution target; unknown, "
                "fallback, and live targets are forbidden"
            )

        target = active_targets[0]
        expected = {
            "target_id": ONLY_EXCHANGE_EXECUTION_TARGET_ID,
            "exchange": "okx",
            "product_type": "SWAP",
            "margin_mode": "isolated",
            "position_mode": "long_short_mode",
            "account_mode": "demo",
            "simulated_trading": True,
            "credential_source": "macos_keychain",
            "write_policy": "SOLE_EXCHANGE_ORDER_TARGET",
            "order_submission_enabled": False,
            "allow_real_funds": False,
        }
        actual = {field: getattr(target, field) for field in expected}
        mismatches = [
            f"{field}={actual[field]!r}"
            for field in expected
            if actual[field] != expected[field]
        ]
        if mismatches:
            raise ValueError(
                "only the fail-closed OKX_DEMO contract is allowed; invalid fields: "
                + ", ".join(mismatches)
            )

        scope = self.non_exchange_scopes[0]
        if scope.scope_id != LOCAL_DRY_RUN_SCOPE_ID or scope.scope_type != "local_simulation":
            raise ValueError(
                "LOCAL_DRY_RUN must be the only non-exchange local_simulation scope"
            )
        return self

    @property
    def active_target(self) -> ExecutionTargetDefinition:
        return self.targets[0]

    @property
    def active_target_id(self) -> str:
        return self.active_target.target_id


def parse_execution_target_manifest(raw: Any) -> ExecutionTargetManifest:
    if not isinstance(raw, dict) or not raw:
        raise ExecutionTargetConfigurationError(
            "execution target configuration is missing; implicit fallback is forbidden"
        )
    try:
        return ExecutionTargetManifest.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ExecutionTargetConfigurationError(
            f"execution target configuration is blocked: {exc}"
        ) from exc


def okx_demo_execution_target_manifest() -> ExecutionTargetManifest:
    """Build the explicit safe contract for dependency-injected tests and smoke runs."""

    return ExecutionTargetManifest(
        implicit_fallback=False,
        targets=[
            ExecutionTargetDefinition(
                target_id=ONLY_EXCHANGE_EXECUTION_TARGET_ID,
                status="ACTIVE",
                exchange="okx",
                product_type="SWAP",
                margin_mode="isolated",
                position_mode="long_short_mode",
                account_mode="demo",
                simulated_trading=True,
                credential_source="macos_keychain",
                write_policy="SOLE_EXCHANGE_ORDER_TARGET",
                order_submission_enabled=False,
                allow_real_funds=False,
            )
        ],
        non_exchange_scopes=[
            NonExchangeExecutionScope(
                scope_id=LOCAL_DRY_RUN_SCOPE_ID,
                scope_type="local_simulation",
            )
        ],
    )
