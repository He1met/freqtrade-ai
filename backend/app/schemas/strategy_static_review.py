from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


StrategyStaticReviewSeverity = Literal["warning", "error"]
StrategyStaticReviewCategory = Literal[
    "syntax_error",
    "forbidden_import",
    "dangerous_call",
    "secret_access",
    "file_access",
    "network_access",
    "lookahead_bias",
]


class StrategyStaticReviewFinding(BaseModel):
    rule_id: str = Field(min_length=1, max_length=80)
    category: StrategyStaticReviewCategory
    severity: StrategyStaticReviewSeverity
    message: str = Field(min_length=1, max_length=1000)
    line: Optional[int] = Field(default=None, ge=1)
    column: Optional[int] = Field(default=None, ge=0)
    details: dict[str, Any] = Field(default_factory=dict)


class StrategyStaticReviewResult(BaseModel):
    passed: bool
    findings: list[StrategyStaticReviewFinding] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
