"""Canonical V1.3 no-trade research validation contracts.

The module deliberately separates parsing/receipt validation from persistence.  It
never imports, compiles, or executes submitted strategy source.  The only executor
provided here is a deterministic metrics-envelope simulator; it has no filesystem,
network, credential, exchange, order, or database-writer capability.

All persistence functions receive a caller-owned SQLAlchemy ``Connection``.  They
record validation plans, attempts, and raw window metrics only after proving exact
strategy/target/bundle/market/window lineage.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import Connection, func, select

from app.canonical_v13.bundles import RESEARCH_BUNDLE_CAPABILITIES
from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    P0_CONFIGURATION_KINDS,
)
from app.canonical_v13.models import (
    CONFIGURATION_ACTIVATIONS_TABLE,
    CONFIGURATION_BUNDLE_MEMBERS_TABLE,
    CONFIGURATION_BUNDLES_TABLE,
    CONFIGURATION_SNAPSHOT_MEMBERS_TABLE,
    CONFIGURATION_SNAPSHOTS_TABLE,
    MARKET_SNAPSHOT_MEMBERS_TABLE,
    RESEARCH_TARGETS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_VERSIONS_TABLE,
    VALIDATION_ATTEMPTS_TABLE,
    VALIDATION_PLANS_TABLE,
    VALIDATION_PLAN_WINDOWS_TABLE,
    VALIDATION_WINDOW_RESULTS_TABLE,
)


_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_ATTEMPT_STATUSES: Final = frozenset({"SUCCEEDED", "FAILED", "BLOCKED"})
_SAFE_NETWORK_MODE: Final = "none"
_STATIC_VALIDATOR_CONTRACT: Final = "canonical-v13-static-validator-v1"
_LOOKAHEAD_CONTRACT: Final = "canonical-v13-lookahead-receipt-v1"
_EMPTY_DIGEST: Final = sha256(b"").hexdigest()
LOOKAHEAD_FAILURE_DETAILS: Final = {
    "LOOKAHEAD_EXPORT_MISSING": "Freqtrade lookahead export is unavailable",
    "LOOKAHEAD_INSUFFICIENT_TRADES": "Freqtrade observed fewer trades than required",
    "LOOKAHEAD_LOG_LIMIT_EXCEEDED": "Freqtrade lookahead log exceeded the safe limit",
    "LOOKAHEAD_PROCESS_FAILED": "Freqtrade lookahead did not complete successfully",
    "LOOKAHEAD_RESULT_AMBIGUOUS": "Freqtrade lookahead result does not uniquely match the strategy",
    "LOOKAHEAD_WORKER_BLOCKED": "lookahead worker rejected the frozen input",
    "LOOKAHEAD_WORKER_INTERNAL_ERROR": "lookahead worker failed closed",
}
LOOKAHEAD_FAILURE_STAGES: Final = frozenset(
    {"FREQTRADE_PROCESS", "OUTPUT_INTERPRETATION", "WORKER"}
)
LOOKAHEAD_FAILURE_STAGE_BY_CODE: Final = {
    "LOOKAHEAD_EXPORT_MISSING": "OUTPUT_INTERPRETATION",
    "LOOKAHEAD_INSUFFICIENT_TRADES": "OUTPUT_INTERPRETATION",
    "LOOKAHEAD_LOG_LIMIT_EXCEEDED": "FREQTRADE_PROCESS",
    "LOOKAHEAD_PROCESS_FAILED": "FREQTRADE_PROCESS",
    "LOOKAHEAD_RESULT_AMBIGUOUS": "OUTPUT_INTERPRETATION",
    "LOOKAHEAD_WORKER_BLOCKED": "WORKER",
    "LOOKAHEAD_WORKER_INTERNAL_ERROR": "WORKER",
}
_PLAN_CONTRACT: Final = "canonical-v13-validation-plan-v1"
_EXECUTOR_CONTRACT: Final = "canonical-v13-ephemeral-no-exchange-v1"
_WINDOW_RESULT_CONTRACT: Final = "canonical-v13-window-metrics-receipt-v1"
_ATTEMPT_RECEIPT_CONTRACT: Final = "canonical-v13-attempt-receipt-v1"
STATIC_VALIDATOR_IDENTITY: Final = _STATIC_VALIDATOR_CONTRACT
STATIC_VALIDATOR_RULE_IDS: Final = (
    "capability.credential_access",
    "capability.external_side_effect",
    "capability.network_or_exchange_import",
    "lookahead.iloc_negative",
    "lookahead.shift_negative",
    "syntax.invalid_python",
    "unsafe.dynamic_execution",
)
LOOKAHEAD_BLOCK_REASON_CODES: Final = frozenset(LOOKAHEAD_FAILURE_DETAILS)


class CanonicalResearchValidationBlocked(RuntimeError):
    """Stable fail-closed error for the Phase 6 research boundary."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ResearchLineage:
    strategy_version_id: UUID
    research_target_id: UUID
    configuration_bundle_id: UUID
    configuration_bundle_digest: str
    market_snapshot_id: UUID
    market_snapshot_digest: str


@dataclass(frozen=True)
class StaticFinding:
    rule_id: str
    line: int
    column: int
    message: str


@dataclass(frozen=True)
class StaticValidationReceipt:
    strategy_version_id: UUID
    artifact_digest: str
    validator_identity: str
    validator_digest: str
    status: str
    findings: tuple[StaticFinding, ...]
    request_digest: str
    receipt_digest: str


@dataclass(frozen=True)
class LookaheadAnalysisReceipt:
    lineage: ResearchLineage
    artifact_digest: str
    analyzer_identity: str
    analyzer_digest: str
    evidence_digest: str
    status: str
    has_bias: bool | None
    observed_signal_count: int
    blocked_observed_trade_count: int | None
    blocked_required_trade_count: int | None
    request_digest: str
    receipt_digest: str
    failure_stage: str | None = None
    failure_code: str | None = None
    tool_return_code: int | None = 0
    stdout_digest: str = _EMPTY_DIGEST
    stderr_digest: str = _EMPTY_DIGEST
    redacted_detail: str | None = None


@dataclass(frozen=True)
class ValidatorDecision:
    status: str
    reason_codes: tuple[str, ...]
    receipt_digest: str


@dataclass(frozen=True)
class PlanWindowBinding:
    validation_plan_window_id: UUID
    window_snapshot_member_id: UUID
    window_key: str
    window_member_digest: str
    required: bool
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True)
class ValidationPlanResult:
    validation_plan_id: UUID
    validation_plan_digest: str
    status: str
    window_count: int
    required_window_count: int
    repeat_noop: bool


@dataclass(frozen=True)
class EphemeralLaunchSpec:
    lineage: ResearchLineage
    validation_plan_id: UUID
    validation_plan_digest: str
    artifact_id: UUID
    artifact_digest: str
    executor_identity: str
    executor_image_digest: str
    windows: tuple[PlanWindowBinding, ...]
    container_class: str = "EPHEMERAL_RESEARCH_EXECUTOR"
    filesystem_mode: str = "EPHEMERAL"
    long_lived_runtime: bool = False
    network_mode: str = _SAFE_NETWORK_MODE
    credential_mounts: tuple[str, ...] = ()
    exchange_capabilities: tuple[str, ...] = ()
    order_capabilities: tuple[str, ...] = ()
    writer_capabilities: tuple[str, ...] = ()
    order_submission: bool = False


@dataclass(frozen=True)
class RunningValidationAttempt:
    validation_attempt_id: UUID
    attempt_number: int
    status: str
    request_digest: str
    launch_spec: EphemeralLaunchSpec


@dataclass(frozen=True)
class WindowMetricsReceipt:
    validation_attempt_id: UUID
    validation_plan_window_id: UUID
    window_snapshot_member_id: UUID
    window_key: str
    window_member_digest: str
    metrics_json: Mapping[str, object]
    metrics_digest: str
    receipt_digest: str


@dataclass(frozen=True)
class EphemeralAttemptReceipt:
    validation_attempt_id: UUID
    validation_plan_id: UUID
    validation_plan_digest: str
    lineage: ResearchLineage
    executor_identity: str
    executor_image_digest: str
    request_digest: str
    status: str
    window_results: tuple[WindowMetricsReceipt, ...]
    receipt_digest: str


@dataclass(frozen=True)
class TerminalAttemptResult:
    validation_attempt_id: UUID
    validation_plan_id: UUID
    attempt_status: str
    plan_status: str
    receipt_digest: str
    window_result_count: int


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_NON_CANONICAL_JSON", "research evidence must be canonical JSON"
        ) from exc


def canonical_research_digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def static_validator_digest() -> str:
    """Return the accepted digest of the exact non-executing AST rule set."""

    return canonical_research_digest(
        {
            "contract": _STATIC_VALIDATOR_CONTRACT,
            "rule_ids": list(STATIC_VALIDATOR_RULE_IDS),
            "source_execution": False,
        }
    )


def _digest(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _HEX_DIGEST.fullmatch(value):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_INVALID_RESEARCH_ENVELOPE",
            f"{field} must be lowercase SHA-256",
        )
    return value


def _identity(value: str, *, field: str, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_INVALID_RESEARCH_ENVELOPE", f"{field} is invalid"
        )
    return value


def _lineage_payload(lineage: ResearchLineage) -> dict[str, str]:
    _digest(lineage.configuration_bundle_digest, field="configuration_bundle_digest")
    _digest(lineage.market_snapshot_digest, field="market_snapshot_digest")
    return {
        "strategy_version_id": str(lineage.strategy_version_id),
        "research_target_id": str(lineage.research_target_id),
        "configuration_bundle_id": str(lineage.configuration_bundle_id),
        "configuration_bundle_digest": lineage.configuration_bundle_digest,
        "market_snapshot_id": str(lineage.market_snapshot_id),
        "market_snapshot_digest": lineage.market_snapshot_digest,
    }


def _finding_payload(finding: StaticFinding) -> dict[str, object]:
    return {
        "rule_id": finding.rule_id,
        "line": finding.line,
        "column": finding.column,
        "message": finding.message,
    }


def _static_request_payload(receipt: StaticValidationReceipt) -> dict[str, object]:
    return {
        "contract": _STATIC_VALIDATOR_CONTRACT,
        "strategy_version_id": str(receipt.strategy_version_id),
        "artifact_digest": receipt.artifact_digest,
        "validator_identity": receipt.validator_identity,
        "validator_digest": receipt.validator_digest,
    }


def _static_receipt_payload(receipt: StaticValidationReceipt) -> dict[str, object]:
    return {
        "request_digest": receipt.request_digest,
        "status": receipt.status,
        "findings": [_finding_payload(finding) for finding in receipt.findings],
    }


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _negative_number(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    )


class _StaticVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[StaticFinding] = []

    def _add(self, node: ast.AST, rule_id: str, message: str) -> None:
        self.findings.append(
            StaticFinding(
                rule_id=rule_id,
                line=int(getattr(node, "lineno", 0) or 0),
                column=int(getattr(node, "col_offset", 0) or 0),
                message=message,
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root in {"ccxt", "requests", "httpx", "socket"}:
                self._add(
                    node,
                    "capability.network_or_exchange_import",
                    f"network/exchange module {root!r} is forbidden in no-trade research",
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".", 1)[0]
        if root in {"ccxt", "requests", "httpx", "socket"}:
            self._add(
                node,
                "capability.network_or_exchange_import",
                f"network/exchange module {root!r} is forbidden in no-trade research",
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _qualified_name(node.func) or ""
        if name in {"eval", "exec", "compile", "__import__"}:
            self._add(
                node,
                "unsafe.dynamic_execution",
                f"dynamic execution call {name!r} is forbidden",
            )
        if name in {"open", "os.system", "os.popen"} or name.startswith(
            ("subprocess.", "requests.", "httpx.", "socket.", "ccxt.")
        ):
            self._add(
                node,
                "capability.external_side_effect",
                f"external side-effect call {name!r} is forbidden",
            )
        if name in {"os.getenv", "os.environ.get"}:
            self._add(
                node,
                "capability.credential_access",
                f"credential/environment access {name!r} is forbidden",
            )
        if name.endswith(".shift") and node.args and _negative_number(node.args[0]):
            self._add(
                node,
                "lookahead.shift_negative",
                "negative shift can read future observations",
            )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.value, ast.Attribute) and node.value.attr == "iloc":
            if _negative_number(node.slice):
                self._add(
                    node,
                    "lookahead.iloc_negative",
                    "negative iloc access can read a future-relative observation",
                )
        self.generic_visit(node)


def validate_static_source(
    source: str,
    *,
    strategy_version_id: UUID,
    expected_artifact_digest: str,
    validator_identity: str,
    validator_digest: str,
) -> StaticValidationReceipt:
    """Parse source into an AST and emit an immutable receipt without executing it."""

    if not isinstance(source, str):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_STATIC_SOURCE_TYPE", "static validator requires normalized text"
        )
    expected_artifact_digest = _digest(
        expected_artifact_digest, field="expected_artifact_digest"
    )
    validator_identity = _identity(
        validator_identity, field="validator_identity", maximum=200
    )
    validator_digest = _digest(validator_digest, field="validator_digest")
    observed_digest = sha256(source.encode("utf-8")).hexdigest()
    if observed_digest != expected_artifact_digest:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_ARTIFACT_DIGEST_DRIFT",
            "static source bytes differ from the canonical artifact digest",
        )

    findings: list[StaticFinding] = []
    try:
        tree = ast.parse(source, filename="<canonical-artifact>", mode="exec")
    except SyntaxError as exc:
        findings.append(
            StaticFinding(
                rule_id="syntax.invalid_python",
                line=int(exc.lineno or 0),
                column=max(int(exc.offset or 1) - 1, 0),
                message="strategy source is not valid Python syntax",
            )
        )
    else:
        visitor = _StaticVisitor()
        visitor.visit(tree)
        findings.extend(visitor.findings)
    normalized = tuple(
        sorted(findings, key=lambda item: (item.line, item.column, item.rule_id))
    )
    provisional = StaticValidationReceipt(
        strategy_version_id=strategy_version_id,
        artifact_digest=expected_artifact_digest,
        validator_identity=validator_identity,
        validator_digest=validator_digest,
        status="FAILED" if normalized else "PASSED",
        findings=normalized,
        request_digest="",
        receipt_digest="",
    )
    request_digest = canonical_research_digest(_static_request_payload(provisional))
    provisional = StaticValidationReceipt(
        **{
            **provisional.__dict__,
            "request_digest": request_digest,
        }
    )
    return StaticValidationReceipt(
        **{
            **provisional.__dict__,
            "receipt_digest": canonical_research_digest(
                _static_receipt_payload(provisional)
            ),
        }
    )


def _verify_static_receipt(
    receipt: StaticValidationReceipt,
    *,
    strategy_version_id: UUID,
    artifact_digest: str,
) -> None:
    if (
        receipt.strategy_version_id != strategy_version_id
        or receipt.artifact_digest != artifact_digest
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_MIXED_LINEAGE", "static receipt belongs to another artifact/version"
        )
    _digest(receipt.artifact_digest, field="static.artifact_digest")
    _digest(receipt.validator_digest, field="static.validator_digest")
    _identity(receipt.validator_identity, field="static.validator_identity")
    expected_request = canonical_research_digest(_static_request_payload(receipt))
    expected_receipt = canonical_research_digest(_static_receipt_payload(receipt))
    if (
        receipt.request_digest != expected_request
        or receipt.receipt_digest != expected_receipt
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_STATIC_RECEIPT_DIGEST_DRIFT", "static receipt digest drifted"
        )
    if receipt.status != "PASSED" or receipt.findings:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_STATIC_VALIDATION_FAILED", "static validation did not pass"
        )


def _lookahead_request_payload(receipt: LookaheadAnalysisReceipt) -> dict[str, object]:
    return {
        "contract": _LOOKAHEAD_CONTRACT,
        "lineage": _lineage_payload(receipt.lineage),
        "artifact_digest": receipt.artifact_digest,
        "analyzer_identity": receipt.analyzer_identity,
        "analyzer_digest": receipt.analyzer_digest,
        "evidence_digest": receipt.evidence_digest,
    }


def _lookahead_receipt_payload(receipt: LookaheadAnalysisReceipt) -> dict[str, object]:
    return {
        "request_digest": receipt.request_digest,
        "status": receipt.status,
        "has_bias": receipt.has_bias,
        "observed_signal_count": receipt.observed_signal_count,
        "failure_stage": receipt.failure_stage,
        "failure_code": receipt.failure_code,
        "tool_return_code": receipt.tool_return_code,
        "stdout_digest": receipt.stdout_digest,
        "stderr_digest": receipt.stderr_digest,
        "redacted_detail": receipt.redacted_detail,
        "blocked_observed_trade_count": receipt.blocked_observed_trade_count,
        "blocked_required_trade_count": receipt.blocked_required_trade_count,
    }


def _validate_lookahead_block_reason(
    *,
    status: str,
    has_bias: bool | None,
    observed_signal_count: int,
    failure_code: str | None,
    blocked_observed_trade_count: int | None,
    blocked_required_trade_count: int | None,
) -> None:
    blocked_counts = (blocked_observed_trade_count, blocked_required_trade_count)
    if status == "BLOCKED":
        if (
            has_bias is not None
            or observed_signal_count != 0
            or failure_code not in LOOKAHEAD_BLOCK_REASON_CODES
        ):
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_LOOKAHEAD_REASON", "blocked lookahead needs one safe reason"
            )
        if failure_code == "LOOKAHEAD_INSUFFICIENT_TRADES":
            if (
                any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in blocked_counts
                )
                or blocked_observed_trade_count < 0
                or blocked_required_trade_count <= blocked_observed_trade_count
            ):
                raise CanonicalResearchValidationBlocked(
                    "BLOCKED_LOOKAHEAD_REASON", "insufficient-trade counts are invalid"
                )
        elif any(value is not None for value in blocked_counts):
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_LOOKAHEAD_REASON", "blocked counts belong only to insufficient trades"
            )
    elif failure_code is not None or any(value is not None for value in blocked_counts):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_LOOKAHEAD_REASON", "non-blocked lookahead cannot carry a blocked reason"
        )


def build_lookahead_receipt(
    *,
    lineage: ResearchLineage,
    artifact_digest: str,
    analyzer_identity: str,
    analyzer_digest: str,
    evidence_digest: str,
    status: str,
    has_bias: bool | None,
    observed_signal_count: int,
    failure_stage: str | None = None,
    failure_code: str | None = None,
    tool_return_code: int | None = 0,
    stdout_digest: str = _EMPTY_DIGEST,
    stderr_digest: str = _EMPTY_DIGEST,
    redacted_detail: str | None = None,
    blocked_observed_trade_count: int | None = None,
    blocked_required_trade_count: int | None = None,
) -> LookaheadAnalysisReceipt:
    """Build an envelope around already-produced evidence; no analysis is run here."""

    _lineage_payload(lineage)
    artifact_digest = _digest(artifact_digest, field="artifact_digest")
    analyzer_identity = _identity(analyzer_identity, field="analyzer_identity")
    analyzer_digest = _digest(analyzer_digest, field="analyzer_digest")
    evidence_digest = _digest(evidence_digest, field="evidence_digest")
    stdout_digest = _digest(stdout_digest, field="stdout_digest")
    stderr_digest = _digest(stderr_digest, field="stderr_digest")
    if status not in {"PASSED", "FAILED", "BLOCKED"}:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_LOOKAHEAD_STATUS", "lookahead status is invalid"
        )
    if (
        isinstance(observed_signal_count, bool)
        or not isinstance(observed_signal_count, int)
        or observed_signal_count < 0
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_LOOKAHEAD_OBSERVATIONS", "observation count must be nonnegative"
        )
    _validate_lookahead_block_reason(
        status=status,
        has_bias=has_bias,
        observed_signal_count=observed_signal_count,
        failure_code=failure_code,
        blocked_observed_trade_count=blocked_observed_trade_count,
        blocked_required_trade_count=blocked_required_trade_count,
    )
    provisional = LookaheadAnalysisReceipt(
        lineage=lineage,
        artifact_digest=artifact_digest,
        analyzer_identity=analyzer_identity,
        analyzer_digest=analyzer_digest,
        evidence_digest=evidence_digest,
        status=status,
        has_bias=has_bias,
        observed_signal_count=observed_signal_count,
        blocked_observed_trade_count=blocked_observed_trade_count,
        blocked_required_trade_count=blocked_required_trade_count,
        request_digest="",
        receipt_digest="",
        failure_stage=failure_stage,
        failure_code=failure_code,
        tool_return_code=tool_return_code,
        stdout_digest=stdout_digest,
        stderr_digest=stderr_digest,
        redacted_detail=redacted_detail,
    )
    request_digest = canonical_research_digest(_lookahead_request_payload(provisional))
    provisional = LookaheadAnalysisReceipt(
        **{**provisional.__dict__, "request_digest": request_digest}
    )
    return LookaheadAnalysisReceipt(
        **{
            **provisional.__dict__,
            "receipt_digest": canonical_research_digest(
                _lookahead_receipt_payload(provisional)
            ),
        }
    )


def validate_lookahead_receipt(
    receipt: LookaheadAnalysisReceipt,
    *,
    expected_lineage: ResearchLineage,
    expected_artifact_digest: str,
) -> ValidatorDecision:
    """Validate lineage/digests and interpret explicit lookahead evidence."""

    if receipt.lineage != expected_lineage or receipt.artifact_digest != expected_artifact_digest:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_MIXED_LINEAGE", "lookahead receipt belongs to another lineage"
        )
    _digest(receipt.artifact_digest, field="lookahead.artifact_digest")
    _digest(receipt.analyzer_digest, field="lookahead.analyzer_digest")
    _digest(receipt.evidence_digest, field="lookahead.evidence_digest")
    _digest(receipt.stdout_digest, field="lookahead.stdout_digest")
    _digest(receipt.stderr_digest, field="lookahead.stderr_digest")
    _identity(receipt.analyzer_identity, field="lookahead.analyzer_identity")
    expected_request = canonical_research_digest(_lookahead_request_payload(receipt))
    expected_receipt = canonical_research_digest(_lookahead_receipt_payload(receipt))
    if (
        receipt.request_digest != expected_request
        or receipt.receipt_digest != expected_receipt
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_LOOKAHEAD_RECEIPT_DIGEST_DRIFT",
            "lookahead request or receipt digest drifted",
        )
    _validate_lookahead_block_reason(
        status=receipt.status,
        has_bias=receipt.has_bias,
        observed_signal_count=receipt.observed_signal_count,
        failure_code=receipt.failure_code,
        blocked_observed_trade_count=receipt.blocked_observed_trade_count,
        blocked_required_trade_count=receipt.blocked_required_trade_count,
    )
    if receipt.status == "BLOCKED":
        if (
            receipt.failure_stage not in LOOKAHEAD_FAILURE_STAGES
            or receipt.failure_code not in LOOKAHEAD_FAILURE_DETAILS
            or receipt.failure_stage
            != LOOKAHEAD_FAILURE_STAGE_BY_CODE.get(receipt.failure_code or "")
            or receipt.redacted_detail
            != LOOKAHEAD_FAILURE_DETAILS.get(receipt.failure_code or "")
            or (
                receipt.tool_return_code is not None
                and (
                    isinstance(receipt.tool_return_code, bool)
                    or not isinstance(receipt.tool_return_code, int)
                    or not -255 <= receipt.tool_return_code <= 255
                )
            )
        ):
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_LOOKAHEAD_FAILURE_DIAGNOSTIC",
                "blocked lookahead receipt lacks an allowlisted diagnostic",
            )
    elif (
        receipt.failure_stage is not None
        or receipt.failure_code is not None
        or receipt.redacted_detail is not None
        or receipt.tool_return_code != 0
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_LOOKAHEAD_FAILURE_DIAGNOSTIC",
            "non-blocked lookahead receipt contains failure diagnostics",
        )
    reasons: list[str] = []
    if receipt.status == "BLOCKED":
        assert receipt.failure_code is not None
        reasons.extend((receipt.failure_code, "LOOKAHEAD_EVIDENCE_BLOCKED"))
    elif receipt.status != "PASSED" or receipt.has_bias is not False:
        reasons.append("LOOKAHEAD_BIAS_DETECTED")
    if receipt.observed_signal_count <= 0:
        reasons.append("LOOKAHEAD_OBSERVATIONS_UNSET")
    if not reasons:
        status = "PASSED"
    elif "LOOKAHEAD_BIAS_DETECTED" in reasons:
        status = "FAILED"
    else:
        status = "BLOCKED"
    return ValidatorDecision(
        status=status,
        reason_codes=tuple(reasons),
        receipt_digest=receipt.receipt_digest,
    )


def _effective_connection(connection: Connection) -> Connection:
    if connection.dialect.name == "sqlite":
        return connection.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
    return connection


def _require_canonical(connection: Connection) -> Connection:
    effective = _effective_connection(connection)
    verification = verify_canonical_genesis(effective)
    if not verification.accepted:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_WRONG_CANONICAL_DATABASE", "; ".join(verification.problems)
        )
    return effective


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_WINDOW_SNAPSHOT_DRIFT", f"{field} must be ISO-8601 text"
        )
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as exc:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_WINDOW_SNAPSHOT_DRIFT", f"{field} is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_WINDOW_SNAPSHOT_DRIFT", f"{field} lacks timezone"
        )
    return parsed.astimezone(timezone.utc)


def _bundle_context(
    connection: Connection,
    *,
    lineage: ResearchLineage,
) -> tuple[dict[str, Any], dict[str, Any], tuple[PlanWindowBinding, ...]]:
    bundle = connection.execute(
        select(CONFIGURATION_BUNDLES_TABLE).where(
            CONFIGURATION_BUNDLES_TABLE.c.id == lineage.configuration_bundle_id
        )
    ).mappings().one_or_none()
    if bundle is None or bundle["bundle_digest"] != lineage.configuration_bundle_digest:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_RESEARCH_BUNDLE_INVALID", "bundle identity or digest is absent"
        )
    bundle = dict(bundle)
    if (
        bundle["market_snapshot_id"] != lineage.market_snapshot_id
        or bundle["market_snapshot_digest"] != lineage.market_snapshot_digest
        or dict(bundle["capability_json"]) != dict(RESEARCH_BUNDLE_CAPABILITIES)
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_RESEARCH_BUNDLE_DIGEST_DRIFT",
            "bundle market/capability evidence drifted",
        )
    members = connection.execute(
        select(CONFIGURATION_BUNDLE_MEMBERS_TABLE).where(
            CONFIGURATION_BUNDLE_MEMBERS_TABLE.c.configuration_bundle_id
            == lineage.configuration_bundle_id
        )
    ).mappings().all()
    by_kind = {row["configuration_kind"]: dict(row) for row in members}
    if len(members) != len(P0_CONFIGURATION_KINDS) or set(by_kind) != set(
        P0_CONFIGURATION_KINDS
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_RESEARCH_BUNDLE_MEMBER_SET",
            "bundle must contain each P0 configuration kind exactly once",
        )
    snapshot_rows: dict[str, dict[str, Any]] = {}
    for kind in P0_CONFIGURATION_KINDS:
        member = by_kind[kind]
        snapshot = connection.execute(
            select(CONFIGURATION_SNAPSHOTS_TABLE).where(
                CONFIGURATION_SNAPSHOTS_TABLE.c.id
                == member["configuration_snapshot_id"]
            )
        ).mappings().one_or_none()
        if (
            snapshot is None
            or snapshot["configuration_kind"] != kind
            or snapshot["snapshot_digest"] != member["snapshot_digest"]
            or canonical_research_digest(snapshot["snapshot_json"])
            != snapshot["snapshot_digest"]
        ):
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_CONFIGURATION_SNAPSHOT_DRIFT",
                f"bundle member {kind} is not an exact immutable snapshot",
            )
        snapshot_rows[kind] = dict(snapshot)
    expected_bundle_digest = canonical_research_digest(
        {
            "contract": "canonical-v13-research-bundle-v1",
            "scope_key": bundle["scope_key"],
            "workflow_key": bundle["workflow_key"],
            "snapshots": [
                {
                    "configuration_kind": kind,
                    "snapshot_id": str(by_kind[kind]["configuration_snapshot_id"]),
                    "snapshot_digest": by_kind[kind]["snapshot_digest"],
                }
                for kind in P0_CONFIGURATION_KINDS
            ],
            "market_snapshot_id": str(bundle["market_snapshot_id"]),
            "market_snapshot_digest": bundle["market_snapshot_digest"],
            "capabilities": dict(RESEARCH_BUNDLE_CAPABILITIES),
        }
    )
    if expected_bundle_digest != bundle["bundle_digest"]:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_RESEARCH_BUNDLE_DIGEST_DRIFT", "bundle digest does not recompute"
        )
    activation = connection.execute(
        select(CONFIGURATION_ACTIVATIONS_TABLE).where(
            CONFIGURATION_ACTIVATIONS_TABLE.c.scope_key == bundle["scope_key"],
            CONFIGURATION_ACTIVATIONS_TABLE.c.workflow_key == bundle["workflow_key"],
        )
    ).mappings().one_or_none()
    if (
        activation is None
        or activation["configuration_bundle_id"] != lineage.configuration_bundle_id
        or activation["bundle_digest"] != lineage.configuration_bundle_digest
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_RESEARCH_BUNDLE_UNSET",
            "the requested frozen research bundle is not the active pointer",
        )

    target = connection.execute(
        select(RESEARCH_TARGETS_TABLE).where(
            RESEARCH_TARGETS_TABLE.c.id == lineage.research_target_id
        )
    ).mappings().one_or_none()
    if (
        target is None
        or target["target_snapshot_id"]
        != by_kind["TARGET"]["configuration_snapshot_id"]
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_TARGET_BUNDLE_MISMATCH", "target is not a frozen bundle member"
        )
    coverage = connection.execute(
        select(MARKET_SNAPSHOT_MEMBERS_TABLE).where(
            MARKET_SNAPSHOT_MEMBERS_TABLE.c.market_snapshot_id
            == lineage.market_snapshot_id,
            MARKET_SNAPSHOT_MEMBERS_TABLE.c.research_target_id
            == lineage.research_target_id,
        )
    ).mappings().all()
    if len(coverage) != 1:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_MARKET_TARGET_COVERAGE",
            "market snapshot must contain one exact target coverage member",
        )
    windows = _snapshot_windows(
        connection,
        snapshot=snapshot_rows["WINDOW"],
        coverage_start=_utc(coverage[0]["coverage_start"]),
        coverage_end=_utc(coverage[0]["coverage_end"]),
    )
    return bundle, snapshot_rows["WINDOW"], windows


def _snapshot_windows(
    connection: Connection,
    *,
    snapshot: Mapping[str, Any],
    coverage_start: datetime,
    coverage_end: datetime,
) -> tuple[PlanWindowBinding, ...]:
    snapshot_payload = snapshot["snapshot_json"]
    payload = (
        snapshot_payload.get("payload_json")
        if isinstance(snapshot_payload, Mapping)
        else None
    )
    raw_windows = payload.get("windows") if isinstance(payload, Mapping) else None
    if not isinstance(raw_windows, list) or not raw_windows:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_REQUIRED_WINDOWS_UNSET", "WINDOW snapshot contains no windows"
        )
    member_rows = connection.execute(
        select(CONFIGURATION_SNAPSHOT_MEMBERS_TABLE).where(
            CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.configuration_snapshot_id
            == snapshot["id"],
            CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.member_key.like("window:%"),
        )
    ).mappings().all()
    members = {row["member_key"]: dict(row) for row in member_rows}
    if len(members) != len(member_rows):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_WINDOW_SNAPSHOT_DRIFT", "duplicate window member identity"
        )
    bindings: list[PlanWindowBinding] = []
    seen: set[str] = set()
    for raw in raw_windows:
        if not isinstance(raw, Mapping):
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_WINDOW_SNAPSHOT_DRIFT", "window payload member is not an object"
            )
        window_key = raw.get("window_key")
        required = raw.get("required")
        coverage = raw.get("coverage")
        if (
            not isinstance(window_key, str)
            or not window_key
            or window_key in seen
            or not isinstance(required, bool)
            or not isinstance(coverage, Mapping)
        ):
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_WINDOW_SNAPSHOT_DRIFT", "window identity/required flag drifted"
            )
        minimum_closed_candles = coverage.get("minimum_closed_candles")
        allowed_coverage = {
            "minimum_closed_candles",
            "warmup_closed_candles",
            "integrity_margin_closed_candles",
            "freshness_max_age_seconds",
        }
        optional_values = {
            key: coverage[key] for key in allowed_coverage - {"minimum_closed_candles"}
            if key in coverage
        }
        if (
            bool(set(coverage) - allowed_coverage)
            or isinstance(minimum_closed_candles, bool)
            or not isinstance(minimum_closed_candles, int)
            or minimum_closed_candles <= 0
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < (1 if key == "freshness_max_age_seconds" else 0)
                for key, value in optional_values.items()
            )
        ):
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_WINDOW_SNAPSHOT_DRIFT", "window coverage contract drifted"
            )
        start_at = _parse_timestamp(raw.get("start_at"), field="window.start_at")
        end_at = _parse_timestamp(raw.get("end_at"), field="window.end_at")
        normalized = {
            "window_key": window_key,
            "required": required,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "coverage": {
                "minimum_closed_candles": minimum_closed_candles,
                **optional_values,
            },
        }
        member = members.get(f"window:{window_key}")
        expected_digest = canonical_research_digest(normalized)
        if member is None or member["member_digest"] != expected_digest:
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_WINDOW_MEMBER_DIGEST_DRIFT",
                f"window {window_key!r} is not bound to its immutable member digest",
            )
        if required and (coverage_start > start_at or coverage_end < end_at):
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_REQUIRED_WINDOW_COVERAGE",
                f"market evidence does not cover required window {window_key!r}",
            )
        bindings.append(
            PlanWindowBinding(
                validation_plan_window_id=UUID(int=0),
                window_snapshot_member_id=member["id"],
                window_key=window_key,
                window_member_digest=expected_digest,
                required=required,
                window_start=start_at,
                window_end=end_at,
            )
        )
        seen.add(window_key)
    if set(members) != {f"window:{key}" for key in seen}:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_WINDOW_SNAPSHOT_DRIFT", "WINDOW payload/member sets differ"
        )
    if not any(binding.required for binding in bindings):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_REQUIRED_WINDOWS_UNSET", "at least one required window is needed"
        )
    return tuple(sorted(bindings, key=lambda item: item.window_key))


def _strategy_artifact(
    connection: Connection, strategy_version_id: UUID
) -> tuple[dict[str, Any], dict[str, Any]]:
    version = connection.execute(
        select(STRATEGY_VERSIONS_TABLE).where(
            STRATEGY_VERSIONS_TABLE.c.id == strategy_version_id
        )
    ).mappings().one_or_none()
    if version is None:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_STRATEGY_VERSION_UNSET", "strategy version is absent"
        )
    artifact = connection.execute(
        select(STRATEGY_ARTIFACTS_TABLE).where(
            STRATEGY_ARTIFACTS_TABLE.c.id == version["artifact_id"]
        )
    ).mappings().one_or_none()
    if artifact is None:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_STRATEGY_ARTIFACT_UNSET", "strategy artifact is absent"
        )
    if sha256(artifact["normalized_content"].encode("utf-8")).hexdigest() != artifact[
        "content_digest"
    ]:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_ARTIFACT_DIGEST_DRIFT", "persisted strategy artifact drifted"
        )
    return dict(version), dict(artifact)


def _plan_payload(
    *,
    lineage: ResearchLineage,
    artifact_digest: str,
    window_snapshot_id: UUID,
    window_snapshot_digest: str,
    windows: Sequence[PlanWindowBinding],
    static_receipt_digest: str,
    lookahead_receipt_digest: str,
    orchestrator_identity: str,
) -> dict[str, object]:
    return {
        "contract": _PLAN_CONTRACT,
        "lineage": _lineage_payload(lineage),
        "artifact_digest": artifact_digest,
        "window_snapshot_id": str(window_snapshot_id),
        "window_snapshot_digest": window_snapshot_digest,
        "windows": [
            {
                "window_snapshot_member_id": str(window.window_snapshot_member_id),
                "window_key": window.window_key,
                "window_member_digest": window.window_member_digest,
                "required": window.required,
                "window_start": _utc_text(window.window_start),
                "window_end": _utc_text(window.window_end),
            }
            for window in sorted(windows, key=lambda item: item.window_key)
        ],
        "static_receipt_digest": static_receipt_digest,
        "lookahead_receipt_digest": lookahead_receipt_digest,
        "orchestrator_identity": orchestrator_identity,
    }


def declare_validation_plan(
    connection: Connection,
    *,
    lineage: ResearchLineage,
    static_receipt: StaticValidationReceipt,
    lookahead_receipt: LookaheadAnalysisReceipt,
    orchestrator_identity: str,
) -> ValidationPlanResult:
    """Declare a plan by copying exact dynamic WINDOW snapshot member identities."""

    orchestrator_identity = _identity(
        orchestrator_identity, field="orchestrator_identity"
    )
    effective = _require_canonical(connection)
    _bundle, window_snapshot, windows = _bundle_context(effective, lineage=lineage)
    _version, artifact = _strategy_artifact(effective, lineage.strategy_version_id)
    _verify_static_receipt(
        static_receipt,
        strategy_version_id=lineage.strategy_version_id,
        artifact_digest=artifact["content_digest"],
    )
    lookahead_decision = validate_lookahead_receipt(
        lookahead_receipt,
        expected_lineage=lineage,
        expected_artifact_digest=artifact["content_digest"],
    )
    if lookahead_decision.status != "PASSED":
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_LOOKAHEAD_VALIDATION_FAILED",
            ",".join(lookahead_decision.reason_codes),
        )
    plan_digest = canonical_research_digest(
        _plan_payload(
            lineage=lineage,
            artifact_digest=artifact["content_digest"],
            window_snapshot_id=window_snapshot["id"],
            window_snapshot_digest=window_snapshot["snapshot_digest"],
            windows=windows,
            static_receipt_digest=static_receipt.receipt_digest,
            lookahead_receipt_digest=lookahead_receipt.receipt_digest,
            orchestrator_identity=orchestrator_identity,
        )
    )
    existing = effective.execute(
        select(VALIDATION_PLANS_TABLE).where(
            VALIDATION_PLANS_TABLE.c.strategy_version_id
            == lineage.strategy_version_id,
            VALIDATION_PLANS_TABLE.c.research_target_id
            == lineage.research_target_id,
            VALIDATION_PLANS_TABLE.c.configuration_bundle_id
            == lineage.configuration_bundle_id,
            VALIDATION_PLANS_TABLE.c.market_snapshot_id == lineage.market_snapshot_id,
            VALIDATION_PLANS_TABLE.c.validation_plan_digest == plan_digest,
        )
    ).mappings().one_or_none()
    if existing is not None:
        persisted = _persisted_plan_windows(effective, existing["id"])
        _assert_window_bindings_match(windows, persisted, ignore_plan_window_ids=True)
        return ValidationPlanResult(
            validation_plan_id=existing["id"],
            validation_plan_digest=plan_digest,
            status=existing["status"],
            window_count=len(persisted),
            required_window_count=sum(item.required for item in persisted),
            repeat_noop=True,
        )

    plan_id = uuid4()
    now = datetime.now(timezone.utc)
    effective.execute(
        VALIDATION_PLANS_TABLE.insert().values(
            id=plan_id,
            strategy_version_id=lineage.strategy_version_id,
            research_target_id=lineage.research_target_id,
            configuration_bundle_id=lineage.configuration_bundle_id,
            configuration_bundle_digest=lineage.configuration_bundle_digest,
            market_snapshot_id=lineage.market_snapshot_id,
            market_snapshot_digest=lineage.market_snapshot_digest,
            window_snapshot_id=window_snapshot["id"],
            validation_plan_digest=plan_digest,
            status="DECLARED",
            created_at=now,
        )
    )
    for window in windows:
        effective.execute(
            VALIDATION_PLAN_WINDOWS_TABLE.insert().values(
                id=uuid4(),
                validation_plan_id=plan_id,
                window_snapshot_member_id=window.window_snapshot_member_id,
                window_key=window.window_key,
                window_member_digest=window.window_member_digest,
                required=window.required,
                window_start=window.window_start,
                window_end=window.window_end,
            )
        )
    return ValidationPlanResult(
        validation_plan_id=plan_id,
        validation_plan_digest=plan_digest,
        status="DECLARED",
        window_count=len(windows),
        required_window_count=sum(item.required for item in windows),
        repeat_noop=False,
    )


def _plan_lineage(plan: Mapping[str, Any]) -> ResearchLineage:
    return ResearchLineage(
        strategy_version_id=plan["strategy_version_id"],
        research_target_id=plan["research_target_id"],
        configuration_bundle_id=plan["configuration_bundle_id"],
        configuration_bundle_digest=plan["configuration_bundle_digest"],
        market_snapshot_id=plan["market_snapshot_id"],
        market_snapshot_digest=plan["market_snapshot_digest"],
    )


def _persisted_plan_windows(
    connection: Connection, plan_id: UUID
) -> tuple[PlanWindowBinding, ...]:
    rows = connection.execute(
        select(VALIDATION_PLAN_WINDOWS_TABLE).where(
            VALIDATION_PLAN_WINDOWS_TABLE.c.validation_plan_id == plan_id
        )
    ).mappings().all()
    return tuple(
        sorted(
            (
                PlanWindowBinding(
                    validation_plan_window_id=row["id"],
                    window_snapshot_member_id=row["window_snapshot_member_id"],
                    window_key=row["window_key"],
                    window_member_digest=row["window_member_digest"],
                    required=bool(row["required"]),
                    window_start=_utc(row["window_start"]),
                    window_end=_utc(row["window_end"]),
                )
                for row in rows
            ),
            key=lambda item: item.window_key,
        )
    )


def _assert_window_bindings_match(
    expected: Sequence[PlanWindowBinding],
    observed: Sequence[PlanWindowBinding],
    *,
    ignore_plan_window_ids: bool,
) -> None:
    def payload(item: PlanWindowBinding) -> tuple[object, ...]:
        return (
            None if ignore_plan_window_ids else item.validation_plan_window_id,
            item.window_snapshot_member_id,
            item.window_key,
            item.window_member_digest,
            item.required,
            _utc_text(item.window_start),
            _utc_text(item.window_end),
        )

    if [payload(item) for item in expected] != [payload(item) for item in observed]:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_PLAN_WINDOW_BINDING_DRIFT",
            "persisted plan windows differ from frozen WINDOW members",
        )


def mark_validation_plan_ready(
    connection: Connection,
    *,
    validation_plan_id: UUID,
    expected_plan_digest: str,
    static_receipt: StaticValidationReceipt,
    lookahead_receipt: LookaheadAnalysisReceipt,
    orchestrator_identity: str,
) -> ValidationPlanResult:
    expected_plan_digest = _digest(expected_plan_digest, field="expected_plan_digest")
    orchestrator_identity = _identity(
        orchestrator_identity, field="orchestrator_identity"
    )
    effective = _require_canonical(connection)
    plan = effective.execute(
        select(VALIDATION_PLANS_TABLE).where(
            VALIDATION_PLANS_TABLE.c.id == validation_plan_id
        )
    ).mappings().one_or_none()
    if plan is None:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_VALIDATION_PLAN_UNSET", "validation plan is absent"
        )
    plan = dict(plan)
    if plan["validation_plan_digest"] != expected_plan_digest:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_VALIDATION_PLAN_DIGEST_DRIFT", "plan digest differs from review"
        )
    if plan["status"] not in {"DECLARED", "READY"}:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_VALIDATION_PLAN_TRANSITION",
            f"cannot mark plan {plan['status']} READY",
        )
    lineage = _plan_lineage(plan)
    _bundle, window_snapshot, current_windows = _bundle_context(
        effective, lineage=lineage
    )
    persisted_windows = _persisted_plan_windows(effective, validation_plan_id)
    _assert_window_bindings_match(
        current_windows, persisted_windows, ignore_plan_window_ids=True
    )
    _version, artifact = _strategy_artifact(effective, lineage.strategy_version_id)
    _verify_static_receipt(
        static_receipt,
        strategy_version_id=lineage.strategy_version_id,
        artifact_digest=artifact["content_digest"],
    )
    decision = validate_lookahead_receipt(
        lookahead_receipt,
        expected_lineage=lineage,
        expected_artifact_digest=artifact["content_digest"],
    )
    if decision.status != "PASSED":
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_LOOKAHEAD_VALIDATION_FAILED", ",".join(decision.reason_codes)
        )
    recomputed = canonical_research_digest(
        _plan_payload(
            lineage=lineage,
            artifact_digest=artifact["content_digest"],
            window_snapshot_id=window_snapshot["id"],
            window_snapshot_digest=window_snapshot["snapshot_digest"],
            windows=current_windows,
            static_receipt_digest=static_receipt.receipt_digest,
            lookahead_receipt_digest=lookahead_receipt.receipt_digest,
            orchestrator_identity=orchestrator_identity,
        )
    )
    if recomputed != plan["validation_plan_digest"]:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_VALIDATION_PLAN_DIGEST_DRIFT", "plan inputs no longer recompute"
        )
    repeat_noop = plan["status"] == "READY"
    if not repeat_noop:
        effective.execute(
            VALIDATION_PLANS_TABLE.update()
            .where(VALIDATION_PLANS_TABLE.c.id == validation_plan_id)
            .values(status="READY")
        )
    return ValidationPlanResult(
        validation_plan_id=validation_plan_id,
        validation_plan_digest=plan["validation_plan_digest"],
        status="READY",
        window_count=len(persisted_windows),
        required_window_count=sum(item.required for item in persisted_windows),
        repeat_noop=repeat_noop,
    )


def _launch_spec_payload(spec: EphemeralLaunchSpec) -> dict[str, object]:
    return {
        "contract": _EXECUTOR_CONTRACT,
        "lineage": _lineage_payload(spec.lineage),
        "validation_plan_id": str(spec.validation_plan_id),
        "validation_plan_digest": spec.validation_plan_digest,
        "artifact_id": str(spec.artifact_id),
        "artifact_digest": spec.artifact_digest,
        "executor_identity": spec.executor_identity,
        "executor_image_digest": spec.executor_image_digest,
        "windows": [
            {
                "validation_plan_window_id": str(item.validation_plan_window_id),
                "window_snapshot_member_id": str(item.window_snapshot_member_id),
                "window_key": item.window_key,
                "window_member_digest": item.window_member_digest,
                "required": item.required,
                "window_start": _utc_text(item.window_start),
                "window_end": _utc_text(item.window_end),
            }
            for item in spec.windows
        ],
        "capabilities": {
            "container_class": spec.container_class,
            "filesystem_mode": spec.filesystem_mode,
            "long_lived_runtime": spec.long_lived_runtime,
            "network_mode": spec.network_mode,
            "credential_mounts": list(spec.credential_mounts),
            "exchange_capabilities": list(spec.exchange_capabilities),
            "order_capabilities": list(spec.order_capabilities),
            "writer_capabilities": list(spec.writer_capabilities),
            "order_submission": spec.order_submission,
        },
    }


def validate_ephemeral_launch_spec(spec: EphemeralLaunchSpec) -> str:
    """Validate the fixed no-exchange launch contract and return its request digest."""

    _lineage_payload(spec.lineage)
    _digest(spec.validation_plan_digest, field="validation_plan_digest")
    _digest(spec.artifact_digest, field="artifact_digest")
    _digest(spec.executor_image_digest, field="executor_image_digest")
    _identity(spec.executor_identity, field="executor_identity")
    if (
        spec.container_class != "EPHEMERAL_RESEARCH_EXECUTOR"
        or spec.filesystem_mode != "EPHEMERAL"
        or spec.long_lived_runtime
        or spec.network_mode != _SAFE_NETWORK_MODE
        or spec.credential_mounts
        or spec.exchange_capabilities
        or spec.order_capabilities
        or spec.writer_capabilities
        or spec.order_submission is not False
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_EXECUTOR_CAPABILITY",
            "ephemeral executor must have network disabled and no "
            "credential/exchange/order/writer capability",
        )
    if not spec.windows or not any(item.required for item in spec.windows):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_REQUIRED_WINDOWS_UNSET", "launch spec has no required windows"
        )
    keys = [item.window_key for item in spec.windows]
    ids = [item.validation_plan_window_id for item in spec.windows]
    member_ids = [item.window_snapshot_member_id for item in spec.windows]
    if (
        len(keys) != len(set(keys))
        or len(ids) != len(set(ids))
        or len(member_ids) != len(set(member_ids))
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_AMBIGUOUS_PLAN_WINDOWS", "launch windows are duplicated"
        )
    for item in spec.windows:
        _identity(item.window_key, field="window_key", maximum=160)
        _digest(item.window_member_digest, field="window_member_digest")
        if _utc(item.window_end) <= _utc(item.window_start):
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_PLAN_WINDOW_BINDING_DRIFT", "launch window order is invalid"
            )
    return canonical_research_digest(_launch_spec_payload(spec))


def build_ephemeral_launch_spec(
    connection: Connection,
    *,
    validation_plan_id: UUID,
    expected_plan_digest: str,
    executor_identity: str,
    executor_image_digest: str,
) -> EphemeralLaunchSpec:
    effective = _require_canonical(connection)
    expected_plan_digest = _digest(expected_plan_digest, field="expected_plan_digest")
    plan = effective.execute(
        select(VALIDATION_PLANS_TABLE).where(
            VALIDATION_PLANS_TABLE.c.id == validation_plan_id
        )
    ).mappings().one_or_none()
    if (
        plan is None
        or plan["status"] != "READY"
        or plan["validation_plan_digest"] != expected_plan_digest
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_VALIDATION_PLAN_NOT_READY", "launch requires the reviewed READY plan"
        )
    lineage = _plan_lineage(plan)
    _bundle_context(effective, lineage=lineage)
    _version, artifact = _strategy_artifact(effective, lineage.strategy_version_id)
    spec = EphemeralLaunchSpec(
        lineage=lineage,
        validation_plan_id=validation_plan_id,
        validation_plan_digest=plan["validation_plan_digest"],
        artifact_id=artifact["id"],
        artifact_digest=artifact["content_digest"],
        executor_identity=_identity(executor_identity, field="executor_identity"),
        executor_image_digest=_digest(
            executor_image_digest, field="executor_image_digest"
        ),
        windows=_persisted_plan_windows(effective, validation_plan_id),
    )
    validate_ephemeral_launch_spec(spec)
    return spec


def start_validation_attempt(
    connection: Connection,
    *,
    launch_spec: EphemeralLaunchSpec,
    validation_attempt_id: UUID | None = None,
) -> RunningValidationAttempt:
    effective = _require_canonical(connection)
    request_digest = validate_ephemeral_launch_spec(launch_spec)
    if validation_attempt_id is not None:
        existing = effective.execute(
            select(VALIDATION_ATTEMPTS_TABLE).where(
                VALIDATION_ATTEMPTS_TABLE.c.id == validation_attempt_id
            )
        ).mappings().one_or_none()
        if existing is not None:
            plan_status = effective.execute(
                select(VALIDATION_PLANS_TABLE.c.status).where(
                    VALIDATION_PLANS_TABLE.c.id == launch_spec.validation_plan_id
                )
            ).scalar_one_or_none()
            if (
                existing["validation_plan_id"] != launch_spec.validation_plan_id
                or existing["status"] != "RUNNING"
                or existing["executor_identity"] != launch_spec.executor_identity
                or existing["executor_image_digest"]
                != launch_spec.executor_image_digest
                or existing["request_digest"] != request_digest
                or plan_status != "RUNNING"
            ):
                raise CanonicalResearchValidationBlocked(
                    "BLOCKED_ATTEMPT_IDENTITY_DRIFT",
                    "reserved attempt identity already binds another state",
                )
            return RunningValidationAttempt(
                validation_attempt_id=validation_attempt_id,
                attempt_number=existing["attempt_number"],
                status="RUNNING",
                request_digest=request_digest,
                launch_spec=launch_spec,
            )
    plan_statement = select(VALIDATION_PLANS_TABLE).where(
            VALIDATION_PLANS_TABLE.c.id == launch_spec.validation_plan_id
        )
    if effective.dialect.name != "sqlite":
        plan_statement = plan_statement.with_for_update()
    plan = effective.execute(plan_statement).mappings().one_or_none()
    if (
        plan is None
        or plan["status"] != "READY"
        or _plan_lineage(plan) != launch_spec.lineage
        or plan["validation_plan_digest"] != launch_spec.validation_plan_digest
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_VALIDATION_PLAN_NOT_READY", "attempt lineage/plan is not READY"
        )
    persisted_windows = _persisted_plan_windows(
        effective, launch_spec.validation_plan_id
    )
    _assert_window_bindings_match(
        launch_spec.windows, persisted_windows, ignore_plan_window_ids=False
    )
    running_count = int(
        effective.execute(
            select(func.count()).select_from(VALIDATION_ATTEMPTS_TABLE).where(
                VALIDATION_ATTEMPTS_TABLE.c.validation_plan_id
                == launch_spec.validation_plan_id,
                VALIDATION_ATTEMPTS_TABLE.c.status.in_(("PENDING", "RUNNING")),
            )
        ).scalar_one()
    )
    if running_count:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_ATTEMPT_ALREADY_RUNNING", "plan already has a live attempt"
        )
    attempt_number = int(
        effective.execute(
            select(func.max(VALIDATION_ATTEMPTS_TABLE.c.attempt_number)).where(
                VALIDATION_ATTEMPTS_TABLE.c.validation_plan_id
                == launch_spec.validation_plan_id
            )
        ).scalar_one()
        or 0
    ) + 1
    attempt_id = validation_attempt_id or uuid4()
    now = datetime.now(timezone.utc)
    effective.execute(
        VALIDATION_ATTEMPTS_TABLE.insert().values(
            id=attempt_id,
            validation_plan_id=launch_spec.validation_plan_id,
            attempt_number=attempt_number,
            status="PENDING",
            executor_identity=launch_spec.executor_identity,
            executor_image_digest=launch_spec.executor_image_digest,
            request_digest=request_digest,
            receipt_digest=None,
            created_at=now,
            completed_at=None,
        )
    )
    effective.execute(
        VALIDATION_ATTEMPTS_TABLE.update()
        .where(VALIDATION_ATTEMPTS_TABLE.c.id == attempt_id)
        .values(status="RUNNING")
    )
    effective.execute(
        VALIDATION_PLANS_TABLE.update()
        .where(VALIDATION_PLANS_TABLE.c.id == launch_spec.validation_plan_id)
        .values(status="RUNNING")
    )
    return RunningValidationAttempt(
        validation_attempt_id=attempt_id,
        attempt_number=attempt_number,
        status="RUNNING",
        request_digest=request_digest,
        launch_spec=launch_spec,
    )


def load_running_validation_attempt(
    connection: Connection,
    *,
    validation_attempt_id: UUID,
    expected_plan_digest: str,
) -> RunningValidationAttempt:
    """Rebuild one exact RUNNING launch envelope without choosing mutable state."""

    effective = _require_canonical(connection)
    expected_plan_digest = _digest(expected_plan_digest, field="expected_plan_digest")
    attempt = effective.execute(
        select(VALIDATION_ATTEMPTS_TABLE).where(
            VALIDATION_ATTEMPTS_TABLE.c.id == validation_attempt_id
        )
    ).mappings().one_or_none()
    if attempt is None or attempt["status"] != "RUNNING":
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_VALIDATION_ATTEMPT_NOT_RUNNING",
            "worker requires one exact RUNNING attempt",
        )
    plan = effective.execute(
        select(VALIDATION_PLANS_TABLE).where(
            VALIDATION_PLANS_TABLE.c.id == attempt["validation_plan_id"]
        )
    ).mappings().one_or_none()
    if (
        plan is None
        or plan["status"] != "RUNNING"
        or plan["validation_plan_digest"] != expected_plan_digest
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_VALIDATION_PLAN_NOT_RUNNING",
            "attempt plan is absent, terminal, or has another digest",
        )
    lineage = _plan_lineage(plan)
    _bundle_context(effective, lineage=lineage)
    _version, artifact = _strategy_artifact(effective, lineage.strategy_version_id)
    spec = EphemeralLaunchSpec(
        lineage=lineage,
        validation_plan_id=plan["id"],
        validation_plan_digest=plan["validation_plan_digest"],
        artifact_id=artifact["id"],
        artifact_digest=artifact["content_digest"],
        executor_identity=attempt["executor_identity"],
        executor_image_digest=attempt["executor_image_digest"],
        windows=_persisted_plan_windows(effective, plan["id"]),
    )
    request_digest = validate_ephemeral_launch_spec(spec)
    if request_digest != attempt["request_digest"]:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_ATTEMPT_REQUEST_DIGEST_DRIFT",
            "persisted RUNNING attempt no longer recomputes",
        )
    return RunningValidationAttempt(
        validation_attempt_id=attempt["id"],
        attempt_number=attempt["attempt_number"],
        status="RUNNING",
        request_digest=request_digest,
        launch_spec=spec,
    )


def _window_metrics_receipt_payload(
    result: WindowMetricsReceipt,
) -> dict[str, object]:
    return {
        "contract": _WINDOW_RESULT_CONTRACT,
        "validation_attempt_id": str(result.validation_attempt_id),
        "validation_plan_window_id": str(result.validation_plan_window_id),
        "window_snapshot_member_id": str(result.window_snapshot_member_id),
        "window_key": result.window_key,
        "window_member_digest": result.window_member_digest,
        "metrics_digest": result.metrics_digest,
    }


def _attempt_receipt_payload(receipt: EphemeralAttemptReceipt) -> dict[str, object]:
    return {
        "contract": _ATTEMPT_RECEIPT_CONTRACT,
        "validation_attempt_id": str(receipt.validation_attempt_id),
        "validation_plan_id": str(receipt.validation_plan_id),
        "validation_plan_digest": receipt.validation_plan_digest,
        "lineage": _lineage_payload(receipt.lineage),
        "executor_identity": receipt.executor_identity,
        "executor_image_digest": receipt.executor_image_digest,
        "request_digest": receipt.request_digest,
        "status": receipt.status,
        "window_receipt_digests": [
            result.receipt_digest
            for result in sorted(receipt.window_results, key=lambda item: item.window_key)
        ],
    }


def ephemeral_attempt_receipt_digest(receipt: EphemeralAttemptReceipt) -> str:
    """Return the canonical digest for an explicit terminal attempt envelope."""

    return canonical_research_digest(_attempt_receipt_payload(receipt))


def build_ephemeral_attempt_receipt(
    running_attempt: RunningValidationAttempt,
    *,
    metrics_by_window_key: Mapping[str, Mapping[str, object]],
    status: str = "SUCCEEDED",
) -> EphemeralAttemptReceipt:
    """Wrap an already-produced exact metrics envelope in immutable receipts."""

    if status not in _TERMINAL_ATTEMPT_STATUSES:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_ATTEMPT_STATUS", "receipt builder requires a terminal attempt status"
        )
    spec = running_attempt.launch_spec
    expected_request = validate_ephemeral_launch_spec(spec)
    if running_attempt.request_digest != expected_request:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_ATTEMPT_REQUEST_DIGEST_DRIFT", "running attempt request drifted"
        )
    required = {item.window_key: item for item in spec.windows if item.required}
    supplied = set(metrics_by_window_key)
    if status == "SUCCEEDED" and supplied != set(required):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_REQUIRED_WINDOW_RESULT_SET",
            "successful receipt needs the exact dynamic required-window set",
        )
    if status != "SUCCEEDED" and supplied:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_PARTIAL_TERMINAL_RESULTS",
            "failed/blocked attempts cannot publish partial window results",
        )
    results: list[WindowMetricsReceipt] = []
    for window_key in sorted(supplied):
        metrics = metrics_by_window_key[window_key]
        if not isinstance(metrics, Mapping) or not metrics:
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_WINDOW_METRICS_UNSET", "window metrics must be a non-empty object"
            )
        copied = dict(metrics)
        metrics_digest = canonical_research_digest(copied)
        binding = required[window_key]
        provisional = WindowMetricsReceipt(
            validation_attempt_id=running_attempt.validation_attempt_id,
            validation_plan_window_id=binding.validation_plan_window_id,
            window_snapshot_member_id=binding.window_snapshot_member_id,
            window_key=binding.window_key,
            window_member_digest=binding.window_member_digest,
            metrics_json=copied,
            metrics_digest=metrics_digest,
            receipt_digest="",
        )
        results.append(
            WindowMetricsReceipt(
                **{
                    **provisional.__dict__,
                    "receipt_digest": canonical_research_digest(
                        _window_metrics_receipt_payload(provisional)
                    ),
                }
            )
        )
    provisional_receipt = EphemeralAttemptReceipt(
        validation_attempt_id=running_attempt.validation_attempt_id,
        validation_plan_id=spec.validation_plan_id,
        validation_plan_digest=spec.validation_plan_digest,
        lineage=spec.lineage,
        executor_identity=spec.executor_identity,
        executor_image_digest=spec.executor_image_digest,
        request_digest=running_attempt.request_digest,
        status=status,
        window_results=tuple(results),
        receipt_digest="",
    )
    return EphemeralAttemptReceipt(
        **{
            **provisional_receipt.__dict__,
            "receipt_digest": ephemeral_attempt_receipt_digest(provisional_receipt),
        }
    )


def simulate_ephemeral_attempt(
    running_attempt: RunningValidationAttempt,
    *,
    metrics_by_window_key: Mapping[str, Mapping[str, object]],
    status: str = "SUCCEEDED",
) -> EphemeralAttemptReceipt:
    """Test-only alias; it never executes strategy or Freqtrade code."""

    return build_ephemeral_attempt_receipt(
        running_attempt,
        metrics_by_window_key=metrics_by_window_key,
        status=status,
    )


def _validate_metrics_values(value: object) -> None:
    if isinstance(value, Mapping):
        for child in value.values():
            _validate_metrics_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_metrics_values(child)
    elif isinstance(value, float) and not math.isfinite(value):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_WINDOW_METRICS_DIGEST_DRIFT", "metrics contain a non-finite value"
        )


def record_terminal_attempt(
    connection: Connection,
    *,
    receipt: EphemeralAttemptReceipt,
) -> TerminalAttemptResult:
    """Atomically insert raw required-window results and close attempt/plan state."""

    effective = _require_canonical(connection)
    attempt_statement = select(VALIDATION_ATTEMPTS_TABLE).where(
            VALIDATION_ATTEMPTS_TABLE.c.id == receipt.validation_attempt_id
        )
    if effective.dialect.name != "sqlite":
        attempt_statement = attempt_statement.with_for_update()
    attempt = effective.execute(attempt_statement).mappings().one_or_none()
    if attempt is None:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_VALIDATION_ATTEMPT_UNSET", "validation attempt is absent"
        )
    attempt = dict(attempt)
    if attempt["status"] in _TERMINAL_ATTEMPT_STATUSES:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_TERMINAL_ATTEMPT_REWRITE", "terminal attempt evidence is immutable"
        )
    if attempt["status"] != "RUNNING":
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_VALIDATION_ATTEMPT_TRANSITION",
            f"cannot close attempt {attempt['status']}",
        )
    plan_statement = select(VALIDATION_PLANS_TABLE).where(
            VALIDATION_PLANS_TABLE.c.id == attempt["validation_plan_id"]
        )
    if effective.dialect.name != "sqlite":
        plan_statement = plan_statement.with_for_update()
    plan = effective.execute(plan_statement).mappings().one_or_none()
    if plan is None or plan["status"] != "RUNNING":
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_VALIDATION_PLAN_TRANSITION", "attempt plan is not RUNNING"
        )
    plan = dict(plan)
    if (
        receipt.validation_plan_id != plan["id"]
        or receipt.validation_plan_digest != plan["validation_plan_digest"]
        or receipt.lineage != _plan_lineage(plan)
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_MIXED_LINEAGE", "attempt receipt lineage differs from its plan"
        )
    if (
        receipt.executor_identity != attempt["executor_identity"]
        or receipt.executor_image_digest != attempt["executor_image_digest"]
        or receipt.request_digest != attempt["request_digest"]
    ):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_ATTEMPT_REQUEST_DIGEST_DRIFT",
            "executor identity/image/request differs from the running attempt",
        )
    if receipt.status not in _TERMINAL_ATTEMPT_STATUSES:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_ATTEMPT_STATUS", "receipt status is not terminal"
        )
    if ephemeral_attempt_receipt_digest(receipt) != receipt.receipt_digest:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_ATTEMPT_RECEIPT_DIGEST_DRIFT", "attempt receipt digest drifted"
        )
    windows = _persisted_plan_windows(effective, plan["id"])
    by_id = {item.validation_plan_window_id: item for item in windows}
    required_ids = {
        item.validation_plan_window_id for item in windows if item.required
    }
    result_ids = [item.validation_plan_window_id for item in receipt.window_results]
    if receipt.status == "SUCCEEDED" and set(result_ids) != required_ids:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_REQUIRED_WINDOW_RESULT_SET",
            "successful attempt lacks the exact required-window result set",
        )
    if receipt.status != "SUCCEEDED" and result_ids:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_PARTIAL_TERMINAL_RESULTS",
            "failed/blocked attempts cannot publish partial results",
        )
    if len(result_ids) != len(set(result_ids)):
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_AMBIGUOUS_WINDOW_RESULTS", "window result IDs are duplicated"
        )
    for result in receipt.window_results:
        binding = by_id.get(result.validation_plan_window_id)
        if (
            binding is None
            or not binding.required
            or result.validation_attempt_id != receipt.validation_attempt_id
            or result.window_snapshot_member_id != binding.window_snapshot_member_id
            or result.window_key != binding.window_key
            or result.window_member_digest != binding.window_member_digest
        ):
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_MIXED_LINEAGE", "window result differs from its plan member"
            )
        _validate_metrics_values(result.metrics_json)
        if canonical_research_digest(dict(result.metrics_json)) != result.metrics_digest:
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_WINDOW_METRICS_DIGEST_DRIFT", "window metrics digest drifted"
            )
        if (
            canonical_research_digest(_window_metrics_receipt_payload(result))
            != result.receipt_digest
        ):
            raise CanonicalResearchValidationBlocked(
                "BLOCKED_WINDOW_RECEIPT_DIGEST_DRIFT", "window receipt digest drifted"
            )
    existing_results = int(
        effective.execute(
            select(func.count()).select_from(VALIDATION_WINDOW_RESULTS_TABLE).where(
                VALIDATION_WINDOW_RESULTS_TABLE.c.validation_attempt_id
                == receipt.validation_attempt_id
            )
        ).scalar_one()
    )
    if existing_results:
        raise CanonicalResearchValidationBlocked(
            "BLOCKED_TERMINAL_ATTEMPT_REWRITE", "attempt already has immutable results"
        )
    now = datetime.now(timezone.utc)
    for result in receipt.window_results:
        effective.execute(
            VALIDATION_WINDOW_RESULTS_TABLE.insert().values(
                id=uuid4(),
                validation_attempt_id=receipt.validation_attempt_id,
                validation_plan_window_id=result.validation_plan_window_id,
                metrics_json=dict(result.metrics_json),
                metrics_digest=result.metrics_digest,
                receipt_digest=result.receipt_digest,
                created_at=now,
            )
        )
    plan_status = {
        "SUCCEEDED": "COMPLETE",
        "FAILED": "FAILED",
        "BLOCKED": "BLOCKED",
    }[receipt.status]
    effective.execute(
        VALIDATION_ATTEMPTS_TABLE.update()
        .where(VALIDATION_ATTEMPTS_TABLE.c.id == receipt.validation_attempt_id)
        .values(
            status=receipt.status,
            receipt_digest=receipt.receipt_digest,
            completed_at=now,
        )
    )
    effective.execute(
        VALIDATION_PLANS_TABLE.update()
        .where(VALIDATION_PLANS_TABLE.c.id == plan["id"])
        .values(status=plan_status)
    )
    return TerminalAttemptResult(
        validation_attempt_id=receipt.validation_attempt_id,
        validation_plan_id=plan["id"],
        attempt_status=receipt.status,
        plan_status=plan_status,
        receipt_digest=receipt.receipt_digest,
        window_result_count=len(receipt.window_results),
    )


__all__ = [
    "CanonicalResearchValidationBlocked",
    "EphemeralAttemptReceipt",
    "EphemeralLaunchSpec",
    "LookaheadAnalysisReceipt",
    "LOOKAHEAD_BLOCK_REASON_CODES",
    "PlanWindowBinding",
    "ResearchLineage",
    "RunningValidationAttempt",
    "StaticFinding",
    "StaticValidationReceipt",
    "STATIC_VALIDATOR_IDENTITY",
    "STATIC_VALIDATOR_RULE_IDS",
    "TerminalAttemptResult",
    "ValidationPlanResult",
    "ValidatorDecision",
    "WindowMetricsReceipt",
    "build_ephemeral_attempt_receipt",
    "build_ephemeral_launch_spec",
    "build_lookahead_receipt",
    "canonical_research_digest",
    "declare_validation_plan",
    "ephemeral_attempt_receipt_digest",
    "mark_validation_plan_ready",
    "load_running_validation_attempt",
    "record_terminal_attempt",
    "simulate_ephemeral_attempt",
    "start_validation_attempt",
    "static_validator_digest",
    "validate_ephemeral_launch_spec",
    "validate_lookahead_receipt",
    "validate_static_source",
]
