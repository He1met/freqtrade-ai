from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class RuntimeResearchConfigurationReadiness(BaseModel):
    ready: Literal[True] = True
    runtime_mode: Literal["V13_NO_TRADE"] = "V13_NO_TRADE"
    database: Literal["freqtrade_ai_design_lab"] = "freqtrade_ai_design_lab"
    schema_version: Literal["20260813_47"] = "20260813_47"
    runtime_role: Literal["freqtrade"] = "freqtrade"
    workflow_kind: Literal["RESEARCH"] = "RESEARCH"
    scope_type: Literal["WORKFLOW"] = "WORKFLOW"
    scope_key: Literal["production-research-v13"] = "production-research-v13"
    bundle_id: int
    bundle_digest: str
    generation_profile_version_id: int
    diversity_profile_version_id: int
    quality_gate_profile_version_id: int
    scoring_profile_version_id: int
    research_profile_version_id: int
    candidates_per_target: int
    target_count: int
    candidate_count: int
    credential_attestation: Literal["UNKNOWN_OUT_OF_SCOPE"] = "UNKNOWN_OUT_OF_SCOPE"
    worker_execution: Literal["DISABLED"] = "DISABLED"
    backtest_execution: Literal["DISABLED"] = "DISABLED"
    signal_generation: Literal["DISABLED"] = "DISABLED"
    order_submission: Literal["DISABLED"] = "DISABLED"
    allow_real_funds: Literal[False] = False
